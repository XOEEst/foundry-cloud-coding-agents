from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import json
from math import isfinite
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping, Protocol
from urllib.parse import urlsplit, urlunsplit

from foundry_opt.evaluation import (
    AgentVersionRef,
    DatasetSplit,
    DatasetVersionRef,
    EvaluationResult,
    EvaluationRun,
    EvaluationStatus,
    EvaluatorDefinitionRef,
    MetricAggregate,
    NormalizedCase,
    NormalizedCaseMetric,
    Outcome,
    Usage,
)
from foundry_opt.orchestration.git_state import (
    OutboxRecord,
    StateObject,
    StateRefConflictError,
    StateRefPushUnacknowledgedError,
    StateRefSnapshot,
)


_PLANNED_KINDS = frozenset(
    {
        "candidate_assets_registration_planned",
        "candidate_effect_planned",
    }
)
_FOUNDRY_EFFECT_KINDS = frozenset(
    {"foundry_assets", "foundry_draft", "foundry_evaluation"}
)
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
)


class CandidateCapabilityExecutionError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        if (
            not isinstance(code, str)
            or not code
            or any(
                not (
                    character.isascii()
                    and (character.isalnum() or character in "._-")
                )
                for character in code
            )
        ):
            raise ValueError("candidate capability error code is invalid")
        if type(retryable) is not bool:
            raise ValueError("candidate capability retryable flag is invalid")
        self.code = code
        self.retryable = retryable
        super().__init__(code)


class CandidateCapabilityStatus(StrEnum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    WAITING = "waiting"
    RETRY = "retry"
    TERMINAL = "terminal"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class CandidateCapabilityExecution:
    record_kind: str
    payload: Mapping[str, object]
    objects: tuple[StateObject, ...] = ()

    def __post_init__(self) -> None:
        if self.record_kind not in {
            "candidate_assets_registration_succeeded",
            "candidate_effect_succeeded",
        }:
            raise ValueError("candidate capability result kind is invalid")
        if not isinstance(self.payload, Mapping):
            raise ValueError("candidate capability result payload is invalid")
        object.__setattr__(
            self,
            "payload",
            MappingProxyType(dict(self.payload)),
        )
        if not all(isinstance(item, StateObject) for item in self.objects):
            raise ValueError("candidate capability result objects are invalid")


@dataclass(frozen=True)
class CandidateCapabilityResult:
    status: CandidateCapabilityStatus
    snapshot: StateRefSnapshot
    effect_id: str | None = None
    code: str | None = None


class CandidateCapabilityLedger(Protocol):
    def load(
        self,
        repository_root: Path,
        issue_number: int,
    ) -> StateRefSnapshot | None: ...

    def commit(
        self,
        repository_root: Path,
        *,
        issue_number: int,
        expected_revision: str | None,
        state: object,
        outbox: tuple[OutboxRecord, ...],
        objects: tuple[StateObject, ...] = (),
    ) -> StateRefSnapshot: ...


class CandidateCapabilityExecutor(Protocol):
    def reconcile(
        self,
        repository_root: Path,
        snapshot: StateRefSnapshot,
        planned: OutboxRecord,
    ) -> CandidateCapabilityExecution | None: ...

    def execute(
        self,
        repository_root: Path,
        snapshot: StateRefSnapshot,
        planned: OutboxRecord,
    ) -> CandidateCapabilityExecution: ...


class CandidateCapabilityAssignments(Protocol):
    def resume(
        self,
        issue_number: int,
        idempotency_key: str,
    ) -> None: ...


class CandidateCapabilityBridge:
    """Execute only exact, steward-persisted Foundry capability intents."""

    def __init__(
        self,
        *,
        ledger: CandidateCapabilityLedger,
        executor: CandidateCapabilityExecutor,
        assignments: CandidateCapabilityAssignments,
    ) -> None:
        self._ledger = ledger
        self._executor = executor
        self._assignments = assignments

    def advance(
        self,
        repository_root: Path,
        issue_number: int,
    ) -> CandidateCapabilityResult:
        snapshot = self._ledger.load(repository_root, issue_number)
        if snapshot is None:
            raise ValueError("candidate capability requires campaign state")
        planned = _next_planned(snapshot)
        if planned is None:
            return CandidateCapabilityResult(
                CandidateCapabilityStatus.WAITING,
                snapshot,
                code="candidate_capability_not_pending",
            )
        effect_id = planned.record_id
        try:
            _validate_planned_capability(
                snapshot,
                planned,
                issue_number,
            )
        except ValueError:
            return self._record_failure(
                repository_root,
                issue_number,
                snapshot,
                planned,
                1,
                CandidateCapabilityExecutionError(
                    "candidate_capability_intent_invalid",
                    retryable=False,
                ),
            )
        succeeded = _record(snapshot, f"{effect_id}-succeeded")
        if succeeded is not None:
            self._resume(issue_number, effect_id, "succeeded")
            return CandidateCapabilityResult(
                CandidateCapabilityStatus.ALREADY_APPLIED,
                snapshot,
                effect_id,
            )
        failures = _failures(snapshot, effect_id)
        terminal = next(
            (
                record
                for record in reversed(failures)
                if record.payload.get("status") == "terminal"
            ),
            None,
        )
        if terminal is not None:
            self._resume(issue_number, effect_id, "failed")
            return CandidateCapabilityResult(
                CandidateCapabilityStatus.TERMINAL,
                snapshot,
                effect_id,
                str(terminal.payload["reason"]),
            )
        attempt = len(failures) + 1
        claim_id = f"{effect_id}-claimed-{attempt}"
        claim = _record(snapshot, claim_id)
        if claim is None:
            claim = OutboxRecord(
                claim_id,
                "candidate_capability_claimed",
                snapshot.state.generation,
                snapshot.state.sequence,
                {
                    "attempt": attempt,
                    "base_commit": planned.payload["base_commit"],
                    "effect_id": effect_id,
                    "effect_kind": planned.payload["effect_kind"],
                    "issue_number": issue_number,
                    "result": "claimed",
                    "spec_sha256": planned.payload["spec_sha256"],
                },
            )
            try:
                snapshot = self._ledger.commit(
                    repository_root,
                    issue_number=issue_number,
                    expected_revision=snapshot.revision,
                    state=snapshot.state,
                    outbox=(claim,),
                )
            except StateRefPushUnacknowledgedError:
                reloaded = self._ledger.load(repository_root, issue_number)
                observed = (
                    _record(reloaded, claim_id)
                    if reloaded is not None
                    else None
                )
                if observed != claim:
                    return CandidateCapabilityResult(
                        CandidateCapabilityStatus.CONFLICT,
                        snapshot,
                        effect_id,
                        "state_ref_ack_unresolved",
                    )
                snapshot = reloaded
            except StateRefConflictError:
                return CandidateCapabilityResult(
                    CandidateCapabilityStatus.CONFLICT,
                    snapshot,
                    effect_id,
                    "state_ref_conflict",
                )
        try:
            execution = self._executor.reconcile(
                repository_root,
                snapshot,
                planned,
            )
            if execution is None:
                execution = self._executor.execute(
                    repository_root,
                    snapshot,
                    planned,
                )
            try:
                _validate_execution(planned, execution)
            except ValueError as error:
                raise CandidateCapabilityExecutionError(
                    "candidate_capability_result_invalid",
                    retryable=False,
                ) from error
        except Exception as error:
            return self._record_failure(
                repository_root,
                issue_number,
                snapshot,
                planned,
                attempt,
                error,
            )
        success = OutboxRecord(
            f"{effect_id}-succeeded",
            execution.record_kind,
            snapshot.state.generation,
            snapshot.state.sequence,
            execution.payload,
        )
        try:
            snapshot = self._ledger.commit(
                repository_root,
                issue_number=issue_number,
                expected_revision=snapshot.revision,
                state=snapshot.state,
                outbox=(success,),
                objects=execution.objects,
            )
        except StateRefPushUnacknowledgedError:
            reloaded = self._ledger.load(repository_root, issue_number)
            observed = (
                _record(reloaded, success.record_id)
                if reloaded is not None
                else None
            )
            if (
                observed != success
                or any(
                    item not in reloaded.objects
                    for item in execution.objects
                )
            ):
                return CandidateCapabilityResult(
                    CandidateCapabilityStatus.CONFLICT,
                    snapshot,
                    effect_id,
                    "state_ref_ack_unresolved",
                )
            snapshot = reloaded
        except StateRefConflictError:
            return CandidateCapabilityResult(
                CandidateCapabilityStatus.CONFLICT,
                snapshot,
                effect_id,
                "state_ref_conflict",
            )
        self._resume(issue_number, effect_id, "succeeded")
        return CandidateCapabilityResult(
            CandidateCapabilityStatus.APPLIED,
            snapshot,
            effect_id,
        )

    def _record_failure(
        self,
        repository_root: Path,
        issue_number: int,
        snapshot: StateRefSnapshot,
        planned: OutboxRecord,
        attempt: int,
        error: Exception,
    ) -> CandidateCapabilityResult:
        effect_id = planned.record_id
        max_attempts = int(planned.payload.get("max_attempts", 1))
        if isinstance(error, CandidateCapabilityExecutionError):
            reason = error.code
            retryable = error.retryable
        else:
            reason = "candidate_capability_external_failed"
            retryable = True
        terminal = not retryable or attempt >= max_attempts
        outcome = "terminal" if terminal else "retryable"
        failed = OutboxRecord(
            f"{effect_id}-failed-{attempt}",
            "candidate_capability_failed",
            snapshot.state.generation,
            snapshot.state.sequence,
            {
                "attempt": attempt,
                "base_commit": planned.payload["base_commit"],
                "effect_id": effect_id,
                "effect_kind": planned.payload["effect_kind"],
                "issue_number": issue_number,
                "max_attempts": max_attempts,
                "reason": reason,
                "spec_sha256": planned.payload["spec_sha256"],
                "status": outcome,
            },
        )
        try:
            snapshot = self._ledger.commit(
                repository_root,
                issue_number=issue_number,
                expected_revision=snapshot.revision,
                state=snapshot.state,
                outbox=(failed,),
            )
        except StateRefPushUnacknowledgedError:
            reloaded = self._ledger.load(repository_root, issue_number)
            observed = (
                _record(reloaded, failed.record_id)
                if reloaded is not None
                else None
            )
            if observed != failed:
                return CandidateCapabilityResult(
                    CandidateCapabilityStatus.CONFLICT,
                    snapshot,
                    effect_id,
                    "state_ref_ack_unresolved",
                )
            snapshot = reloaded
        except StateRefConflictError:
            return CandidateCapabilityResult(
                CandidateCapabilityStatus.CONFLICT,
                snapshot,
                effect_id,
                "state_ref_conflict",
            )
        if terminal:
            self._resume(issue_number, effect_id, "failed")
        return CandidateCapabilityResult(
            (
                CandidateCapabilityStatus.TERMINAL
                if terminal
                else CandidateCapabilityStatus.RETRY
            ),
            snapshot,
            effect_id,
            reason,
        )

    def _resume(
        self,
        issue_number: int,
        effect_id: str,
        outcome: str,
    ) -> None:
        self._assignments.resume(
            issue_number,
            f"capability-{effect_id}-{outcome}",
        )


def evaluation_result_state_object(
    *,
    effect_id: str,
    issue_number: int,
    generation: int,
    spec_sha256: str,
    base_commit: str,
    idempotency_key: str,
    result: EvaluationResult,
) -> StateObject:
    if re.fullmatch(r"[0-9a-f]{64}", idempotency_key) is None:
        raise ValueError("evaluation idempotency_key is invalid")
    document = {
        "base_commit": base_commit,
        "effect_id": effect_id,
        "generation": generation,
        "idempotency_key": idempotency_key,
        "issue_number": issue_number,
        "kind": "candidate_evaluation_result",
        "result": {
            "attempt_runs": [
                _evaluation_run_document(run)
                for run in result.attempt_runs
            ],
            "attempts": result.attempts,
            "cases": [
                {
                    "case_hash": case.case_hash,
                    "case_id": case.case_id,
                    "duration_ms": case.duration_ms,
                    "error_code": (
                        "case_error" if case.error is not None else None
                    ),
                    "response_ids": list(case.response_ids),
                    "scores": [
                        {
                            "metric": score.metric,
                            "normalized_score": score.normalized_score,
                            "outcome": score.outcome.value,
                            "raw_score": _safe_raw_score(score.raw_score),
                        }
                        for score in case.scores
                    ],
                    "usage": _usage_document(case.usage),
                }
                for case in result.cases
            ],
            "complete": result.complete,
            "duration_ms": result.duration_ms,
            "error_count": len(result.errors),
            "metrics": {
                name: {
                    "maximum": aggregate.maximum,
                    "median": aggregate.median,
                    "metric": aggregate.metric,
                    "minimum": aggregate.minimum,
                    "outcome": aggregate.outcome.value,
                    "sample_count": aggregate.sample_count,
                    "spread": aggregate.spread,
                }
                for name, aggregate in sorted(result.metrics.items())
            },
            "needs_repeat": result.needs_repeat,
            "run": _evaluation_run_document(result.run),
            "usage": _usage_document(result.usage),
        },
        "schema_version": 2,
        "spec_sha256": spec_sha256,
    }
    return StateObject(
        f"objects/capabilities/{effect_id}-result.json",
        (
            json.dumps(
                document,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
    )


def evaluation_result_from_state_object(
    state_object: StateObject,
    *,
    effect_id: str,
    issue_number: int,
    generation: int,
    spec_sha256: str,
    base_commit: str,
    idempotency_key: str,
) -> EvaluationResult:
    if re.fullmatch(r"[0-9a-f]{64}", idempotency_key) is None:
        raise ValueError("evaluation idempotency_key is invalid")
    try:
        document = json.loads(state_object.content)
        version = (
            document.get("schema_version")
            if isinstance(document, dict)
            else None
        )
        expected_fields = {
            "base_commit",
            "effect_id",
            "generation",
            "idempotency_key",
            "issue_number",
            "kind",
            "result",
            "schema_version",
            "spec_sha256",
        }
        legacy_fields = expected_fields - {"idempotency_key"}
        if (
            not isinstance(document, dict)
            or (
                version == 2
                and (
                    set(document) != expected_fields
                    or document["idempotency_key"] != idempotency_key
                )
            )
            or (
                version == 1
                and set(document) != legacy_fields
            )
            or version not in {1, 2}
            or document["kind"] != "candidate_evaluation_result"
            or document["effect_id"] != effect_id
            or document["issue_number"] != issue_number
            or document["generation"] != generation
            or document["spec_sha256"] != spec_sha256
            or document["base_commit"] != base_commit
            or state_object.path
            != f"objects/capabilities/{effect_id}-result.json"
            or not isinstance(document["result"], dict)
        ):
            raise ValueError
        value = document["result"]
        if set(value) != {
            "attempt_runs",
            "attempts",
            "cases",
            "complete",
            "duration_ms",
            "error_count",
            "metrics",
            "needs_repeat",
            "run",
            "usage",
        }:
            raise ValueError
        if (
            not isinstance(value["cases"], list)
            or not isinstance(value["metrics"], dict)
            or not isinstance(value["attempt_runs"], list)
            or type(value["complete"]) is not bool
            or type(value["needs_repeat"]) is not bool
            or type(value["attempts"]) is not int
            or value["attempts"] < 1
            or type(value["duration_ms"]) is not int
            or value["duration_ms"] < 0
            or type(value["error_count"]) is not int
            or value["error_count"] < 0
        ):
            raise ValueError
        cases = tuple(_normalized_case(item) for item in value["cases"])
        metrics = {
            str(name): _metric_aggregate(str(name), item)
            for name, item in value["metrics"].items()
        }
        error_count = value["error_count"]
        return EvaluationResult(
            run=_evaluation_run(value["run"]),
            cases=cases,
            metrics=metrics,
            usage=_usage(value["usage"]),
            duration_ms=value["duration_ms"],
            errors=tuple("provider_error" for _ in range(error_count)),
            complete=value["complete"],
            needs_repeat=value["needs_repeat"],
            attempts=value["attempts"],
            attempt_runs=tuple(
                _evaluation_run(item) for item in value["attempt_runs"]
            ),
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        raise ValueError(
            "candidate evaluation result object is invalid"
        ) from error


def awaiting_candidate_capability_result(
    snapshot: StateRefSnapshot,
) -> bool:
    for planned in snapshot.outbox:
        if (
            planned.generation != snapshot.state.generation
            or planned.kind not in _PLANNED_KINDS
            or planned.payload.get("effect_kind")
            not in _FOUNDRY_EFFECT_KINDS
        ):
            continue
        if _record(snapshot, f"{planned.record_id}-succeeded") is not None:
            continue
        if any(
            failure.payload.get("status") == "terminal"
            for failure in _failures(snapshot, planned.record_id)
        ):
            continue
        return True
    return False


def _next_planned(snapshot: StateRefSnapshot) -> OutboxRecord | None:
    resolved: OutboxRecord | None = None
    for record in snapshot.outbox:
        if (
            record.generation == snapshot.state.generation
            and record.kind in _PLANNED_KINDS
            and record.payload.get("effect_kind") in _FOUNDRY_EFFECT_KINDS
        ):
            if (
                _record(snapshot, f"{record.record_id}-succeeded")
                is not None
                or any(
                    failure.payload.get("status") == "terminal"
                    for failure in _failures(snapshot, record.record_id)
                )
            ):
                resolved = record
                continue
            return record
    return resolved


def _record(
    snapshot: StateRefSnapshot,
    record_id: str,
) -> OutboxRecord | None:
    return next(
        (
            record
            for record in snapshot.outbox
            if record.record_id == record_id
        ),
        None,
    )


def _failures(
    snapshot: StateRefSnapshot,
    effect_id: str,
) -> tuple[OutboxRecord, ...]:
    return tuple(
        record
        for record in snapshot.outbox
        if (
            record.kind == "candidate_capability_failed"
            and record.payload.get("effect_id") == effect_id
        )
    )


def _validate_execution(
    planned: OutboxRecord,
    execution: CandidateCapabilityExecution,
) -> None:
    payload = execution.payload
    effect_kind = planned.payload["effect_kind"]
    expected_fields = {
        "foundry_assets": {
            "base_commit",
            "capability_path",
            "capability_sha256",
            "effect_id",
            "effect_kind",
            "issue_number",
            "result_id",
            "spec_sha256",
        },
        "foundry_draft": {
            "base_commit",
            "bundle_sha256",
            "candidate_id",
            "draft_id",
            "effect_id",
            "effect_kind",
            "issue_number",
            "spec_sha256",
        },
        "foundry_evaluation": {
            "base_commit",
            "candidate_id",
            "capability_path",
            "capability_sha256",
            "effect_id",
            "effect_kind",
            "evaluation_id",
            "idempotency_key",
            "issue_number",
            "metrics",
            "run_id",
            "spec_sha256",
        },
    }[effect_kind]
    if set(payload) != expected_fields:
        raise ValueError("candidate capability result schema is invalid")
    for key in (
        "base_commit",
        "effect_id",
        "effect_kind",
        "issue_number",
        "spec_sha256",
    ):
        if payload.get(key) != planned.payload.get(key):
            raise ValueError("candidate capability result changed its intent")
    if (
        effect_kind == "foundry_evaluation"
        and planned.payload.get("idempotency_key") is not None
        and payload.get("idempotency_key")
        != planned.payload.get("idempotency_key")
    ):
        raise ValueError(
            "candidate evaluation result changed its idempotency binding"
        )
    if effect_kind == "foundry_assets":
        if execution.record_kind != "candidate_assets_registration_succeeded":
            raise ValueError("candidate asset result kind is invalid")
    elif execution.record_kind != "candidate_effect_succeeded":
        raise ValueError("candidate effect result kind is invalid")
    if effect_kind == "foundry_draft" and execution.objects:
        raise ValueError("candidate draft result must not contain objects")
    if effect_kind != "foundry_draft" and len(execution.objects) != 1:
        raise ValueError("candidate capability result object is required")
    if execution.objects:
        paths = {item.path: item.sha256 for item in execution.objects}
        if (
            payload.get("capability_path") not in paths
            or paths[payload["capability_path"]]
            != payload.get("capability_sha256")
        ):
            raise ValueError("candidate capability result object is unbound")


def _validate_planned_capability(
    snapshot: StateRefSnapshot,
    planned: OutboxRecord,
    issue_number: int,
) -> None:
    effect_kind = planned.payload.get("effect_kind")
    expected_fields = {
        "foundry_assets": {
            "base_commit",
            "capability_path",
            "capability_sha256",
            "effect_id",
            "effect_kind",
            "environment",
            "issue_number",
            "max_attempts",
            "spec_sha256",
            "target",
        },
        "foundry_draft": {
            "base_commit",
            "bundle_sha256",
            "candidate_id",
            "effect_id",
            "effect_kind",
            "idempotency_key",
            "issue_number",
            "max_attempts",
            "slot",
            "spec_sha256",
        },
        "foundry_evaluation": {
            "base_commit",
            "candidate_id",
            "effect_id",
            "effect_kind",
            "idempotency_key",
            "issue_number",
            "max_attempts",
            "slot",
            "spec_sha256",
        },
    }.get(effect_kind)
    allowed_fields = (
        (
            expected_fields,
            expected_fields - {"idempotency_key"},
        )
        if effect_kind == "foundry_evaluation"
        and expected_fields is not None
        else (expected_fields,)
    )
    if (
        expected_fields is None
        or set(planned.payload) not in allowed_fields
        or planned.record_id != planned.payload.get("effect_id")
        or planned.payload.get("issue_number") != issue_number
        or planned.generation != snapshot.state.generation
        or planned.payload.get("spec_sha256")
        != snapshot.state.spec_sha256
    ):
        raise ValueError("candidate capability plan schema is invalid")
    if planned.kind == "candidate_assets_registration_planned":
        if effect_kind != "foundry_assets":
            raise ValueError("candidate asset effect kind is invalid")
        _validate_asset_intent_object(snapshot, planned)
    elif (
        planned.kind != "candidate_effect_planned"
        or effect_kind == "foundry_assets"
    ):
        raise ValueError("candidate effect plan kind is invalid")


def _validate_asset_intent_object(
    snapshot: StateRefSnapshot,
    planned: OutboxRecord,
) -> None:
    path = planned.payload["capability_path"]
    digest = planned.payload["capability_sha256"]
    objects = tuple(item for item in snapshot.objects if item.path == path)
    if len(objects) != 1 or objects[0].sha256 != digest:
        raise ValueError("candidate asset intent object is unavailable")
    document = json.loads(objects[0].content)
    if (
        not isinstance(document, dict)
        or set(document)
        != {
            "assets",
            "base_commit",
            "effect_id",
            "environment",
            "generation",
            "issue_number",
            "kind",
            "schema_version",
            "spec_sha256",
            "target",
        }
        or document["schema_version"] != 1
        or document["kind"] != "candidate_assets_registration"
        or document["effect_id"] != planned.record_id
        or document["generation"] != planned.generation
        or document["issue_number"] != planned.payload["issue_number"]
        or document["base_commit"] != planned.payload["base_commit"]
        or document["spec_sha256"] != planned.payload["spec_sha256"]
        or document["environment"] != planned.payload["environment"]
        or document["target"] != planned.payload["target"]
        or not isinstance(document["assets"], list)
        or not document["assets"]
    ):
        raise ValueError("candidate asset intent schema is invalid")
    asset_fields = {
        "approval_gate",
        "asset_id",
        "content_sha256",
        "created_by",
        "kind",
        "metrics",
        "name",
        "path",
        "remote_id",
        "role",
        "source",
        "version",
    }
    if any(
        not isinstance(asset, dict) or set(asset) != asset_fields
        for asset in document["assets"]
    ):
        raise ValueError("candidate asset entry schema is invalid")
    asset_ids = [asset["asset_id"] for asset in document["assets"]]
    if (
        any(not isinstance(asset_id, str) or not asset_id for asset_id in asset_ids)
        or len(asset_ids) != len(set(asset_ids))
    ):
        raise ValueError("candidate asset identities are invalid")


def _evaluation_run_document(run: EvaluationRun) -> dict[str, object]:
    return {
        "agent": {
            "agent_id": run.agent.agent_id,
            "draft_id": run.agent.draft_id,
            "version": run.agent.version,
        },
        "completed_at": (
            run.completed_at.isoformat()
            if run.completed_at is not None
            else None
        ),
        "dataset": {
            "dataset_id": run.dataset.dataset_id,
            "version": run.dataset.version,
        },
        "error_code": "provider_error" if run.error is not None else None,
        "evaluation_id": run.evaluation_id,
        "evaluator": {
            "definition_id": run.evaluator.definition_id,
            "version": run.evaluator.version,
        },
        "portal_url": _safe_portal_url(run.portal_url),
        "run_id": run.run_id,
        "split": run.split.value,
        "started_at": (
            run.started_at.isoformat() if run.started_at is not None else None
        ),
        "status": run.status.value,
        "subject_id": run.subject_id,
    }


def _evaluation_run(value: object) -> EvaluationRun:
    if not isinstance(value, dict) or set(value) != {
        "agent",
        "completed_at",
        "dataset",
        "error_code",
        "evaluation_id",
        "evaluator",
        "portal_url",
        "run_id",
        "split",
        "started_at",
        "status",
        "subject_id",
    }:
        raise ValueError
    agent = value["agent"]
    dataset = value["dataset"]
    evaluator = value["evaluator"]
    if (
        not isinstance(agent, dict)
        or set(agent) != {"agent_id", "draft_id", "version"}
        or not isinstance(dataset, dict)
        or set(dataset) != {"dataset_id", "version"}
        or not isinstance(evaluator, dict)
        or set(evaluator) != {"definition_id", "version"}
        or value["error_code"] not in {None, "provider_error"}
        or any(
            not isinstance(value[field], str) or not value[field]
            for field in ("evaluation_id", "run_id", "subject_id")
        )
    ):
        raise ValueError
    return EvaluationRun(
        run_id=str(value["run_id"]),
        evaluation_id=str(value["evaluation_id"]),
        subject_id=str(value["subject_id"]),
        split=DatasetSplit(value["split"]),
        agent=AgentVersionRef(
            str(agent["agent_id"]),
            str(agent["draft_id"]),
            str(agent["version"]),
        ),
        dataset=DatasetVersionRef(
            str(dataset["dataset_id"]),
            str(dataset["version"]),
        ),
        evaluator=EvaluatorDefinitionRef(
            str(evaluator["definition_id"]),
            str(evaluator["version"]),
        ),
        status=EvaluationStatus(value["status"]),
        portal_url=(
            str(value["portal_url"])
            if value["portal_url"] is not None
            else None
        ),
        started_at=_optional_datetime(value["started_at"]),
        completed_at=_optional_datetime(value["completed_at"]),
        error=(
            "provider_error"
            if value["error_code"] == "provider_error"
            else None
        ),
    )


def _normalized_case(value: object) -> NormalizedCase:
    if not isinstance(value, dict) or set(value) != {
        "case_hash",
        "case_id",
        "duration_ms",
        "error_code",
        "response_ids",
        "scores",
        "usage",
    }:
        raise ValueError
    if value["error_code"] not in {None, "case_error"}:
        raise ValueError
    scores = value["scores"]
    response_ids = value["response_ids"]
    if (
        not isinstance(scores, list)
        or not isinstance(response_ids, list)
        or any(
            not isinstance(item, str) or not item
            for item in response_ids
        )
        or type(value["duration_ms"]) is not int
        or value["duration_ms"] < 0
        or not isinstance(value["case_id"], str)
        or not value["case_id"]
        or not isinstance(value["case_hash"], str)
        or not value["case_hash"]
    ):
        raise ValueError
    parsed_scores: list[NormalizedCaseMetric] = []
    for item in scores:
        if (
            not isinstance(item, dict)
            or set(item)
            != {"metric", "normalized_score", "outcome", "raw_score"}
            or not isinstance(item["metric"], str)
            or not item["metric"]
            or not (
                item["raw_score"] is None
                or type(item["raw_score"]) in {bool, int, float, str}
            )
        ):
            raise ValueError
        parsed_scores.append(
            NormalizedCaseMetric(
                metric=item["metric"],
                raw_score=item["raw_score"],
                normalized_score=item["normalized_score"],
                reason=None,
                outcome=Outcome(item["outcome"]),
            )
        )
    return NormalizedCase(
        case_id=value["case_id"],
        case_hash=value["case_hash"],
        response_ids=tuple(response_ids),
        scores=tuple(parsed_scores),
        usage=_usage(value["usage"]),
        trajectory=None,
        error=(
            "case_error" if value["error_code"] == "case_error" else None
        ),
        duration_ms=value["duration_ms"],
    )


def _metric_aggregate(
    name: str,
    value: object,
) -> MetricAggregate:
    if not isinstance(value, dict) or set(value) != {
        "maximum",
        "median",
        "metric",
        "minimum",
        "outcome",
        "sample_count",
        "spread",
    }:
        raise ValueError
    if value["metric"] != name:
        raise ValueError
    if (
        type(value["sample_count"]) is not int
        or value["sample_count"] < 0
    ):
        raise ValueError
    return MetricAggregate(
        metric=name,
        median=value["median"],
        minimum=value["minimum"],
        maximum=value["maximum"],
        spread=value["spread"],
        outcome=Outcome(value["outcome"]),
        sample_count=value["sample_count"],
    )


def _usage_document(value: Usage) -> dict[str, int]:
    return {
        "cached_tokens": value.cached_tokens,
        "input_tokens": value.input_tokens,
        "output_tokens": value.output_tokens,
    }


def _usage(value: object) -> Usage:
    if not isinstance(value, dict) or set(value) != {
        "cached_tokens",
        "input_tokens",
        "output_tokens",
    }:
        raise ValueError
    if any(
        type(value[field]) is not int or value[field] < 0
        for field in (
            "cached_tokens",
            "input_tokens",
            "output_tokens",
        )
    ):
        raise ValueError
    return Usage(
        input_tokens=value["input_tokens"],
        output_tokens=value["output_tokens"],
        cached_tokens=value["cached_tokens"],
    )


def _safe_raw_score(value: object) -> object:
    if isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if isfinite(value) else None
    if isinstance(value, str) and value.strip().casefold() in {
        "pass",
        "fail",
        "undefined",
    }:
        return value.strip().casefold()
    return None


def _safe_portal_url(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold()
        not in {"ai.azure.com", "portal.azure.com"}
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    return urlunsplit(("https", parsed.netloc, parsed.path, "", ""))


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError
    return result


def candidate_capability_issue_numbers(
    *,
    requested_issue: str | None,
    state_ref: str | None,
    tracked: tuple[int, ...],
) -> tuple[int, ...]:
    if any(type(number) is not int or number < 1 for number in tracked):
        raise ValueError("tracked capability issue number is invalid")
    available = tuple(sorted(set(tracked)))
    if requested_issue:
        if re.fullmatch(r"[1-9][0-9]*", requested_issue) is None:
            raise ValueError("candidate capability issue number is invalid")
        issue_number = int(requested_issue)
        if issue_number not in available:
            raise ValueError(
                "candidate capability issue number is not tracked"
            )
        return (issue_number,)
    if state_ref:
        match = re.fullmatch(
            r"foundry-opt/state/issue-([1-9][0-9]*)",
            state_ref,
        )
        if match is not None:
            issue_number = int(match.group(1))
            if issue_number not in available:
                raise ValueError(
                    "candidate capability issue number is not tracked"
                )
            return (issue_number,)
    return available


def verify_active_optimizer_identity(
    commands: object,
    repository_root: Path,
    environment: Mapping[str, str],
) -> None:
    client_id = environment.get("AZURE_CLIENT_ID")
    tenant_id = environment.get("AZURE_TENANT_ID")
    subscription_id = environment.get("AZURE_SUBSCRIPTION_ID")
    deployment_client_id = environment.get("AZURE_DEPLOYMENT_CLIENT_ID")
    if (
        not client_id
        or not tenant_id
        or not subscription_id
        or (
            deployment_client_id is not None
            and deployment_client_id == client_id
        )
    ):
        raise ValueError("optimizer Azure account scope is unavailable")
    try:
        account = json.loads(
            commands.run(
                (
                    "az",
                    "account",
                    "show",
                    "--query",
                    "{tenant:tenantId,subscription:id,userName:user.name,"
                    "userType:user.type}",
                    "-o",
                    "json",
                ),
                cwd=repository_root,
            ).stdout
        )
    except Exception as error:
        raise ValueError(
            "active optimizer principal could not be verified"
        ) from error
    if (
        not isinstance(account, dict)
        or account.get("tenant") != tenant_id
        or account.get("subscription") != subscription_id
        or account.get("userName") != client_id
        or str(account.get("userType", "")).casefold()
        != "serviceprincipal"
    ):
        raise ValueError(
            "active Azure principal is not the optimizer identity"
        )


class _ProductionCapabilityAssignments:
    def __init__(self, assignments: object) -> None:
        self._assignments = assignments

    def resume(
        self,
        issue_number: int,
        idempotency_key: str,
    ) -> None:
        if self._assignments.has_live_lease(issue_number):
            return
        self._assignments.assign(issue_number, idempotency_key)


def main() -> None:
    from foundry_opt.adapters.commands import SubprocessCommandRunner
    from foundry_opt.adapters.github import (
        github_repository_from_remote_url,
    )
    from foundry_opt.optimization.production import (
        build_production_candidate_capability_bridge,
    )
    from foundry_opt.orchestration.git_state import GitStateRef
    from foundry_opt.orchestration.issue_intake import (
        GhStewardAssignments,
        GitIssueEventInbox,
        GitStateCampaignRecovery,
    )

    root = Path.cwd()
    repository = os.environ.get("TRUSTED_REPOSITORY", "")
    if not _REPOSITORY.fullmatch(repository):
        raise ValueError("trusted capability repository is invalid")
    assignment_token = os.environ.pop(
        "COPILOT_ASSIGNMENT_TOKEN",
        None,
    )
    if not assignment_token:
        raise ValueError(
            "required Copilot assignment token is unavailable"
        )
    commands = SubprocessCommandRunner()
    remote = commands.run(
        ("git", "remote", "get-url", "origin"),
        cwd=root,
    ).stdout.strip()
    if github_repository_from_remote_url(remote) != repository:
        raise ValueError("capability repository does not match origin")
    verify_active_optimizer_identity(commands, root, os.environ)
    inbox = GitIssueEventInbox(root)
    ledger = GitStateRef()
    recovery = GitStateCampaignRecovery(root, inbox, ledger)
    issues = candidate_capability_issue_numbers(
        requested_issue=os.environ.get("REQUESTED_ISSUE"),
        state_ref=os.environ.get("TRUSTED_STATE_REF"),
        tracked=inbox.issue_numbers(),
    )
    assignments = _ProductionCapabilityAssignments(
        GhStewardAssignments(
            commands,
            root,
            repository,
            assignment_token=assignment_token,
        )
    )
    bridge = build_production_candidate_capability_bridge(
        assignments=assignments,
        command_runner=commands,
        ledger=ledger,
    )
    results = []
    for issue_number in issues:
        if not recovery.can_reconcile_persisted_effects(issue_number):
            continue
        snapshot = ledger.load(root, issue_number)
        if snapshot is None or not any(
            record.generation == snapshot.state.generation
            and record.kind in _PLANNED_KINDS
            and record.payload.get("effect_kind")
            in _FOUNDRY_EFFECT_KINDS
            for record in snapshot.outbox
        ):
            continue
        if (
            not awaiting_candidate_capability_result(snapshot)
            and not recovery.should_recover(issue_number)
        ):
            continue
        result = bridge.advance(root, issue_number)
        results.append(
            {
                "code": result.code,
                "effect_id": result.effect_id,
                "issue_number": issue_number,
                "status": result.status.value,
            }
        )
    print(
        json.dumps(
            {"processed": len(results), "results": results},
            separators=(",", ":"),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
