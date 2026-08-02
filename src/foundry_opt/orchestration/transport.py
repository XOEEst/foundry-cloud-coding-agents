from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Protocol

from foundry_opt.orchestration.git_state import (
    GitStateRef,
    OutboxRecord,
    StateRefConflictError,
    StateRefError,
    StateRefSnapshot,
)
from foundry_opt.preflight.interfaces import CommandRunner


_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
)


class GhSpecialistWorkerGateway:
    """GitHub transport for specialist worker issues, never pull requests."""

    def __init__(
        self,
        commands: CommandRunner,
        repository_root: Path,
        repository: str,
    ) -> None:
        if not _REPOSITORY.fullmatch(repository):
            raise ValueError("repository is invalid")
        self._commands = commands
        self._root = repository_root
        self._repository = repository

    def find_issue(self, marker: str) -> int | None:
        pages = self._json(
            (
                "gh",
                "api",
                "--paginate",
                "--slurp",
                (
                    f"repos/{self._repository}/issues"
                    "?state=all&per_page=100"
                ),
            )
        )
        if not isinstance(pages, list):
            raise RuntimeError("specialist issue response is invalid")
        matches: list[int] = []
        for page in pages:
            if not isinstance(page, list):
                raise RuntimeError("specialist issue response is invalid")
            for item in page:
                if (
                    not isinstance(item, dict)
                    or "pull_request" in item
                    or marker not in str(item.get("body", ""))
                ):
                    continue
                number = item.get("number")
                user = item.get("user")
                if (
                    type(number) is not int
                    or number < 1
                    or not isinstance(user, dict)
                    or user.get("login") != "github-actions[bot]"
                ):
                    raise RuntimeError(
                        "specialist issue identity is invalid"
                    )
                matches.append(number)
        if len(matches) > 1:
            raise RuntimeError("specialist issue marker is ambiguous")
        return matches[0] if matches else None

    def create_issue(
        self,
        *,
        title: str,
        body: str,
        marker: str,
    ) -> int:
        if marker not in body or not title.strip():
            raise ValueError("specialist issue content is invalid")
        response = self._write(
            "POST",
            f"repos/{self._repository}/issues",
            {"body": body, "title": title},
        )
        number = response.get("number") if isinstance(response, dict) else None
        if type(number) is not int or number < 1:
            raise RuntimeError("specialist issue number is invalid")
        return number

    def has_assignment_marker(
        self,
        issue_number: int,
        marker: str,
    ) -> bool:
        issue = self._json(
            (
                "gh",
                "api",
                f"repos/{self._repository}/issues/{issue_number}",
            )
        )
        assignees = issue.get("assignees") if isinstance(issue, dict) else None
        if not isinstance(assignees, list):
            raise RuntimeError("specialist assignees are invalid")
        if any(
            isinstance(item, dict)
            and item.get("login") == "copilot-swe-agent[bot]"
            for item in assignees
        ):
            return True
        assignment_marker = _assignment_marker(marker)
        pages = self._json(
            (
                "gh",
                "api",
                "--paginate",
                "--slurp",
                (
                    f"repos/{self._repository}/issues/"
                    f"{issue_number}/comments"
                ),
            )
        )
        if not isinstance(pages, list):
            raise RuntimeError("specialist comments are invalid")
        return any(
            isinstance(page, list)
            and any(
                isinstance(item, dict)
                and isinstance(item.get("user"), dict)
                and item["user"].get("login") == "github-actions[bot]"
                and assignment_marker in str(item.get("body", ""))
                for item in page
            )
            for page in pages
        )

    def assign_specialist(
        self,
        issue_number: int,
        *,
        specialist: str,
    ) -> None:
        if specialist != "foundry-optimization-planner":
            raise ValueError("specialist is invalid")
        endpoint = (
            f"repos/{self._repository}/issues/"
            f"{issue_number}/assignees"
        )
        assignees = {"assignees": ["copilot-swe-agent[bot]"]}
        self._write("DELETE", endpoint, assignees)
        self._write(
            "POST",
            endpoint,
            {
                **assignees,
                "agent_assignment": {
                    "custom_agent": specialist,
                    "custom_instructions": (
                        "Fulfil only the persisted "
                        "prepare_specification_pr intent."
                    ),
                    "target_repo": self._repository,
                },
            },
        )

    def record_assignment_marker(
        self,
        issue_number: int,
        marker: str,
    ) -> None:
        self._write(
            "POST",
            (
                f"repos/{self._repository}/issues/"
                f"{issue_number}/comments"
            ),
            {"body": _assignment_marker(marker)},
        )

    def _json(self, arguments: tuple[str, ...]) -> Any:
        result = self._commands.run(arguments, cwd=self._root)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                "GitHub response is not valid JSON"
            ) from error

    def _write(
        self,
        method: str,
        endpoint: str,
        body: dict[str, object],
    ) -> Any:
        result = self._commands.run(
            (
                "gh",
                "api",
                "--method",
                method,
                endpoint,
                "--input",
                "-",
            ),
            cwd=self._root,
            input_text=json.dumps(
                body,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        if not result.stdout:
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                "GitHub write response is not valid JSON"
            ) from error


class SpecialistWorkerGateway(Protocol):
    def find_issue(self, marker: str) -> int | None: ...

    def create_issue(
        self,
        *,
        title: str,
        body: str,
        marker: str,
    ) -> int: ...

    def has_assignment_marker(
        self,
        issue_number: int,
        marker: str,
    ) -> bool: ...

    def assign_specialist(
        self,
        issue_number: int,
        *,
        specialist: str,
    ) -> None: ...

    def record_assignment_marker(
        self,
        issue_number: int,
        marker: str,
    ) -> None: ...


@dataclass(frozen=True)
class SpecialistWorkResult:
    effect_id: str
    result_id: str
    issue_number: int
    worker_issue_number: int
    specialist: str
    work_kind: str
    created: bool
    assigned: bool


class SpecialistWorkBridgeStatus(StrEnum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    RETRY = "retry"
    INVALID = "invalid"


@dataclass(frozen=True)
class SpecialistWorkBridgeResult:
    status: SpecialistWorkBridgeStatus
    result: SpecialistWorkResult | None = None
    reason: str | None = None


class SpecialistWorkBridge:
    """Apply one steward-decided specialist worker issue effect."""

    def __init__(self, gateway: SpecialistWorkerGateway) -> None:
        self._gateway = gateway

    def apply(self, record: OutboxRecord) -> SpecialistWorkBridgeResult:
        try:
            issue_number, specialist, work_kind = _specialist_intent(record)
        except (TypeError, ValueError):
            return SpecialistWorkBridgeResult(
                SpecialistWorkBridgeStatus.INVALID,
                reason="specialist_work_intent_invalid",
            )
        marker = _specialist_marker(record, issue_number)
        created = False
        try:
            worker_issue = self._gateway.find_issue(marker)
            if worker_issue is None:
                worker_issue = self._gateway.create_issue(
                    title=f"[foundry-opt] {work_kind} for #{issue_number}",
                    body="\n".join(
                        (
                            marker,
                            f"Root optimization issue: #{issue_number}",
                            "State ref: "
                            "`refs/heads/foundry-opt/state/"
                            f"issue-{issue_number}`",
                            f"Specialist: `{specialist}`",
                            f"Work kind: `{work_kind}`",
                            f"Reason: `{record.payload['reason']}`",
                            "",
                        )
                    ),
                    marker=marker,
                )
                created = True
            if self._gateway.has_assignment_marker(worker_issue, marker):
                return SpecialistWorkBridgeResult(
                    SpecialistWorkBridgeStatus.ALREADY_APPLIED,
                    _specialist_result(
                        record,
                        issue_number,
                        worker_issue,
                        specialist,
                        work_kind,
                        created=True,
                    ),
                )
            self._gateway.assign_specialist(
                worker_issue,
                specialist=specialist,
            )
            self._gateway.record_assignment_marker(
                worker_issue,
                marker,
            )
        except RuntimeError:
            return SpecialistWorkBridgeResult(
                SpecialistWorkBridgeStatus.RETRY,
                reason="specialist_work_effect_unacknowledged",
            )
        return SpecialistWorkBridgeResult(
            SpecialistWorkBridgeStatus.APPLIED,
            _specialist_result(
                record,
                issue_number,
                worker_issue,
                specialist,
                work_kind,
                created=created,
            ),
        )


class SpecialistEffectRecordStatus(StrEnum):
    RECORDED = "recorded"
    ALREADY_RECORDED = "already_recorded"
    CONFLICT = "conflict"
    FAILED = "failed"


@dataclass(frozen=True)
class SpecialistEffectRecordResult:
    status: SpecialistEffectRecordStatus
    snapshot: StateRefSnapshot
    code: str | None = None


class SpecialistEffectLedger(Protocol):
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
    ) -> StateRefSnapshot: ...


class SpecialistEffectResultRecorder:
    """CAS-persist one GitHub specialist assignment acknowledgement."""

    def __init__(self, ledger: SpecialistEffectLedger) -> None:
        self._ledger = ledger

    def record(
        self,
        repository_root: Path,
        issue_number: int,
        result: SpecialistWorkResult | None,
    ) -> SpecialistEffectRecordResult:
        if result is None:
            raise ValueError("specialist effect result is required")
        snapshot = self._ledger.load(repository_root, issue_number)
        if snapshot is None:
            raise ValueError("specialist effect requires campaign state")
        planned = tuple(
            record
            for record in snapshot.outbox
            if record.record_id == result.effect_id
        )
        if len(planned) != 1:
            return SpecialistEffectRecordResult(
                SpecialistEffectRecordStatus.FAILED,
                snapshot,
                "specialist_effect_plan_unavailable",
            )
        expected_issue, specialist, work_kind = _specialist_intent(planned[0])
        if (
            result.issue_number != expected_issue
            or result.specialist != specialist
            or result.work_kind != work_kind
        ):
            return SpecialistEffectRecordResult(
                SpecialistEffectRecordStatus.FAILED,
                snapshot,
                "specialist_effect_result_mismatch",
            )
        success = OutboxRecord(
            record_id=f"{result.effect_id}-succeeded",
            kind="specialist_work_succeeded",
            generation=snapshot.state.generation,
            sequence=snapshot.state.sequence,
            payload={
                "assigned": result.assigned,
                "created": result.created,
                "effect_id": result.effect_id,
                "issue_number": result.issue_number,
                "result_id": result.result_id,
                "specialist": result.specialist,
                "work_kind": result.work_kind,
                "worker_issue_number": result.worker_issue_number,
            },
        )
        existing = tuple(
            record
            for record in snapshot.outbox
            if record.record_id == success.record_id
        )
        if existing:
            if (
                len(existing) == 1
                and existing[0].kind == success.kind
                and existing[0].generation == success.generation
                and dict(existing[0].payload) == dict(success.payload)
            ):
                return SpecialistEffectRecordResult(
                    SpecialistEffectRecordStatus.ALREADY_RECORDED,
                    snapshot,
                )
            return SpecialistEffectRecordResult(
                SpecialistEffectRecordStatus.FAILED,
                snapshot,
                "specialist_effect_result_conflict",
            )
        try:
            persisted = self._ledger.commit(
                repository_root,
                issue_number=issue_number,
                expected_revision=snapshot.revision,
                state=snapshot.state,
                outbox=(success,),
            )
        except StateRefConflictError:
            return SpecialistEffectRecordResult(
                SpecialistEffectRecordStatus.CONFLICT,
                snapshot,
                "state_ref_conflict",
            )
        except (StateRefError, TypeError, ValueError):
            return SpecialistEffectRecordResult(
                SpecialistEffectRecordStatus.FAILED,
                snapshot,
                "specialist_effect_persist_failed",
            )
        return SpecialistEffectRecordResult(
            SpecialistEffectRecordStatus.RECORDED,
            persisted,
        )


@dataclass(frozen=True)
class TransportEffectReconcileResult:
    issue_number: int
    specialist_statuses: tuple[SpecialistWorkBridgeStatus, ...] = ()
    applier_statuses: tuple[str, ...] = ()
    supersession_statuses: tuple[str, ...] = ()


class TransportEffectReconciler:
    """Apply only durable outbox effects and persist their acknowledgements."""

    def __init__(
        self,
        *,
        ledger: SpecialistEffectLedger,
        specialist: SpecialistWorkBridge,
        applier: Any | None = None,
        supersession: Any | None = None,
    ) -> None:
        self._ledger = ledger
        self._specialist = specialist
        self._applier = applier
        self._supersession = supersession

    def reconcile(
        self,
        repository_root: Path,
        issue_number: int,
    ) -> TransportEffectReconcileResult:
        snapshot = self._ledger.load(repository_root, issue_number)
        if snapshot is None:
            return TransportEffectReconcileResult(issue_number)
        acknowledged = {
            str(record.payload["effect_id"])
            for record in snapshot.outbox
            if (
                record.kind == "specialist_work_succeeded"
                and isinstance(record.payload.get("effect_id"), str)
            )
        }
        claimed = {
            str(record.payload["effect_id"])
            for record in snapshot.outbox
            if (
                record.kind == "specialist_work_claimed"
                and isinstance(record.payload.get("effect_id"), str)
            )
        }
        planned = tuple(
            record
            for record in snapshot.outbox
            if (
                record.kind == "specialist_work_request"
                and record.record_id not in acknowledged
                and (
                    record.generation == snapshot.state.generation
                    or record.record_id in claimed
                )
            )
        )
        statuses: list[SpecialistWorkBridgeStatus] = []
        recorder = SpecialistEffectResultRecorder(self._ledger)
        for record in planned:
            if (
                record.record_id not in claimed
                and not self._claim(
                    repository_root,
                    issue_number,
                    record,
                    "specialist_work_claimed",
                )
            ):
                continue
            applied = self._specialist.apply(record)
            statuses.append(applied.status)
            if (
                applied.status
                in {
                    SpecialistWorkBridgeStatus.APPLIED,
                    SpecialistWorkBridgeStatus.ALREADY_APPLIED,
                }
                and applied.result is not None
            ):
                recorder.record(
                    repository_root,
                    issue_number,
                    applied.result,
                )
        applier_statuses: list[str] = []
        if self._applier is not None:
            from foundry_opt.orchestration.candidate_slate import (
                ApplierWorkerBridgeStatus,
                CandidateEffectResultRecorder,
                applier_worker_intent,
            )

            snapshot = self._ledger.load(repository_root, issue_number)
            assert snapshot is not None
            candidate_acknowledged = {
                str(record.payload["effect_id"])
                for record in snapshot.outbox
                if (
                    record.kind == "applier_worker_issue_succeeded"
                    and isinstance(record.payload.get("effect_id"), str)
                )
            }
            candidate_claimed = {
                str(record.payload["effect_id"])
                for record in snapshot.outbox
                if (
                    record.kind == "applier_worker_issue_claimed"
                    and isinstance(
                        record.payload.get("effect_id"),
                        str,
                    )
                )
            }
            candidate_plans = tuple(
                record
                for record in snapshot.outbox
                if (
                    record.kind == "applier_worker_issue_planned"
                    and record.record_id not in candidate_acknowledged
                    and (
                        record.generation == snapshot.state.generation
                        or record.record_id in candidate_claimed
                    )
                )
            )
            candidate_recorder = CandidateEffectResultRecorder(
                self._ledger
            )
            for record in candidate_plans:
                if (
                    record.record_id not in candidate_claimed
                    and not self._claim(
                        repository_root,
                        issue_number,
                        record,
                        "applier_worker_issue_claimed",
                    )
                ):
                    continue
                applied = self._applier.apply(record)
                applier_statuses.append(applied.status.value)
                if applied.status in {
                    ApplierWorkerBridgeStatus.APPLIED,
                    ApplierWorkerBridgeStatus.ALREADY_APPLIED,
                }:
                    candidate_recorder.record(
                        repository_root,
                        issue_number,
                        applied.worker_result(
                            applier_worker_intent(record)
                        ),
                    )
        supersession_statuses: list[str] = []
        if self._supersession is not None:
            snapshot = self._ledger.load(repository_root, issue_number)
            assert snapshot is not None
            for record in snapshot.outbox:
                if (
                    record.generation == snapshot.state.generation
                    and record.kind
                    in {
                        "candidate_issue_supersede_planned",
                        "candidate_pr_supersede_planned",
                        "candidate_pr_reject_planned",
                    }
                ):
                    applied = self._supersession.apply(record)
                    supersession_statuses.append(applied.status.value)
        return TransportEffectReconcileResult(
            issue_number,
            tuple(statuses),
            tuple(applier_statuses),
            tuple(supersession_statuses),
        )

    def _claim(
        self,
        repository_root: Path,
        issue_number: int,
        record: OutboxRecord,
        kind: str,
    ) -> bool:
        snapshot = self._ledger.load(repository_root, issue_number)
        if snapshot is None:
            return False
        claim_id = f"{record.record_id}-claimed"
        existing = tuple(
            item
            for item in snapshot.outbox
            if item.record_id == claim_id
        )
        if existing:
            return (
                len(existing) == 1
                and existing[0].kind == kind
                and existing[0].payload.get("effect_id")
                == record.record_id
            )
        if record.generation != snapshot.state.generation:
            return False
        claim = OutboxRecord(
            record_id=claim_id,
            kind=kind,
            generation=record.generation,
            sequence=snapshot.state.sequence,
            payload={
                "effect_id": record.record_id,
                "issue_number": issue_number,
            },
        )
        try:
            self._ledger.commit(
                repository_root,
                issue_number=issue_number,
                expected_revision=snapshot.revision,
                state=snapshot.state,
                outbox=(claim,),
            )
        except StateRefConflictError:
            return False
        except (StateRefError, TypeError, ValueError):
            return False
        return True


def reconcile_github_transport_effects(
    repository_root: Path,
    issue_number: int,
    commands: CommandRunner,
    repository: str,
) -> TransportEffectReconcileResult:
    from foundry_opt.orchestration.candidate_bridge import (
        GhApplierWorkerGateway,
        GhCandidateSupersessionGateway,
    )
    from foundry_opt.orchestration.candidate_slate import (
        ApplierWorkerBridge,
        CandidateSupersessionBridge,
    )

    ledger = GitStateRef()
    return TransportEffectReconciler(
        ledger=ledger,
        specialist=SpecialistWorkBridge(
            GhSpecialistWorkerGateway(
                commands,
                repository_root,
                repository,
            )
        ),
        applier=ApplierWorkerBridge(
            GhApplierWorkerGateway(
                commands,
                repository_root,
                repository,
            )
        ),
        supersession=CandidateSupersessionBridge(
            GhCandidateSupersessionGateway(
                commands,
                repository_root,
                repository,
            )
        ),
    ).reconcile(repository_root, issue_number)


def _specialist_intent(record: OutboxRecord) -> tuple[int, str, str]:
    if record.kind != "specialist_work_request":
        raise ValueError("specialist work kind is invalid")
    issue_number = record.payload.get("issue_number")
    specialist = record.payload.get("specialist")
    work_kind = record.payload.get("work_kind")
    reason = record.payload.get("reason")
    if (
        type(issue_number) is not int
        or issue_number < 1
        or specialist != "foundry-optimization-planner"
        or work_kind != "prepare_specification_pr"
        or not isinstance(reason, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", reason)
        is None
    ):
        raise ValueError("specialist work binding is invalid")
    return issue_number, specialist, work_kind


def _specialist_result(
    record: OutboxRecord,
    issue_number: int,
    worker_issue_number: int,
    specialist: str,
    work_kind: str,
    *,
    created: bool,
) -> SpecialistWorkResult:
    return SpecialistWorkResult(
        effect_id=record.record_id,
        result_id=f"{record.record_id}-worker-{worker_issue_number}",
        issue_number=issue_number,
        worker_issue_number=worker_issue_number,
        specialist=specialist,
        work_kind=work_kind,
        created=created,
        assigned=True,
    )


def _assignment_marker(marker: str) -> str:
    digest = hashlib.sha256(marker.encode("utf-8")).hexdigest()[:20]
    return f"<!-- foundry-opt:specialist-assigned:{digest} -->"


def _specialist_marker(
    record: OutboxRecord,
    issue_number: int,
) -> str:
    identity = json.dumps(
        {
            "generation": record.generation,
            "issue_number": issue_number,
            "payload": dict(record.payload),
            "record_id": record.record_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return (
        "<!-- foundry-opt:specialist-work:"
        f"issue-{issue_number}:{record.record_id}:{digest} -->"
    )
