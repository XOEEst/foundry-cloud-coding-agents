from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import re
from typing import Any

from foundry_opt.evaluation import EvaluationPolicy
from foundry_opt.orchestration.candidate_experiments import (
    CandidateExperimentRequest,
    CandidateExperimentResult,
)
from foundry_opt.orchestration.public_evidence import FoundryOperation
from foundry_opt.orchestration.workspace import (
    WorkspaceCandidate,
    WorkspaceReportContext,
)
from foundry_opt.security import reject_secret_content


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_PATCH_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class WorkspaceExperimentManifest:
    issue_number: int
    target: str
    base_commit: str
    candidates: tuple[WorkspaceCandidate, ...]
    report_context: WorkspaceReportContext


class PreparedCandidateResultRunner:
    def __init__(self, candidates: Sequence[WorkspaceCandidate]) -> None:
        self._results = {
            item.experiment.idempotency_key: item.experiment_result
            for item in candidates
        }
        if len(self._results) != len(tuple(candidates)):
            raise ValueError(
                "workspace manifest idempotency keys must be unique"
            )

    def evaluate(
        self,
        request: CandidateExperimentRequest,
    ) -> CandidateExperimentResult:
        result = self._results.get(request.idempotency_key)
        if result is None or result.candidate_id != request.candidate_id:
            raise ValueError(
                "workspace prepared experiment result is unavailable"
            )
        return result


def parse_workspace_experiment_manifest(
    payload: Mapping[str, Any],
    *,
    policy: EvaluationPolicy,
) -> WorkspaceExperimentManifest:
    reject_secret_content(payload)
    _exact_keys(
        payload,
        {
            "base_commit",
            "candidates",
            "issue_number",
            "report_context",
            "schema_version",
            "target",
        },
        "workspace manifest",
    )
    if payload["schema_version"] != 1:
        raise ValueError("workspace manifest schema version is invalid")
    issue_number = payload["issue_number"]
    target = payload["target"]
    base_commit = payload["base_commit"]
    if type(issue_number) is not int or issue_number < 1:
        raise ValueError("workspace manifest issue number is invalid")
    if not isinstance(target, str) or _IDENTIFIER.fullmatch(target) is None:
        raise ValueError("workspace manifest target is invalid")
    if (
        not isinstance(base_commit, str)
        or _COMMIT.fullmatch(base_commit) is None
    ):
        raise ValueError("workspace manifest base commit is invalid")
    raw_candidates = payload["candidates"]
    if (
        not isinstance(raw_candidates, list)
        or not raw_candidates
        or len(raw_candidates) > 32
    ):
        raise ValueError("workspace manifest candidates are invalid")
    candidates = tuple(
        _candidate(item, issue_number)
        for item in raw_candidates
    )
    ids = tuple(item.experiment.candidate_id for item in candidates)
    if len(ids) != len(set(ids)):
        raise ValueError("workspace manifest candidate IDs must be unique")
    context = _report_context(payload["report_context"], policy)
    return WorkspaceExperimentManifest(
        issue_number=issue_number,
        target=target,
        base_commit=base_commit,
        candidates=candidates,
        report_context=context,
    )


def _candidate(
    value: Any,
    issue_number: int,
) -> WorkspaceCandidate:
    if not isinstance(value, Mapping):
        raise ValueError("workspace manifest candidate is invalid")
    _exact_keys(
        value,
        {
            "bundle_sha256",
            "candidate_id",
            "changed_paths",
            "evidence_sha256",
            "expected_tree",
            "foundry_operations",
            "idempotency_key",
            "patch_base64",
            "result",
            "summary",
            "validation",
        },
        "workspace manifest candidate",
    )
    patch_value = value["patch_base64"]
    if not isinstance(patch_value, str):
        raise ValueError("workspace manifest patch is invalid")
    try:
        patch = base64.b64decode(patch_value, validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError("workspace manifest patch is invalid") from error
    if not patch or len(patch) > _MAX_PATCH_BYTES:
        raise ValueError("workspace manifest patch is invalid")
    try:
        reject_secret_content(patch.decode("utf-8"))
    except UnicodeDecodeError:
        pass
    candidate_id = value["candidate_id"]
    bundle_sha256 = value["bundle_sha256"]
    evidence_sha256 = value["evidence_sha256"]
    idempotency_key = value["idempotency_key"]
    request = CandidateExperimentRequest(
        issue_number=issue_number,
        candidate_id=candidate_id,
        patch_sha256=hashlib.sha256(patch).hexdigest(),
        bundle_sha256=bundle_sha256,
        evidence_sha256=evidence_sha256,
        idempotency_key=idempotency_key,
    )
    result_value = value["result"]
    if not isinstance(result_value, Mapping):
        raise ValueError("workspace manifest result is invalid")
    allowed_result_keys = {
        "draft_id",
        "evaluation_id",
        "executor",
        "guardrails",
        "metrics",
        "operation_sha256",
        "run_id",
    }
    if "operation_sha256" not in result_value:
        allowed_result_keys.remove("operation_sha256")
    _exact_keys(
        result_value,
        allowed_result_keys,
        "workspace manifest result",
    )
    operation_sha256 = result_value.get("operation_sha256")
    result = CandidateExperimentResult(
        candidate_id=candidate_id,
        executor=result_value["executor"],
        metrics=_mapping(result_value["metrics"], "result metrics"),
        guardrails=_mapping(
            result_value["guardrails"],
            "result guardrails",
        ),
        draft_id=result_value["draft_id"],
        evaluation_id=result_value["evaluation_id"],
        run_id=result_value["run_id"],
        bundle_sha256=bundle_sha256,
        evidence_sha256=evidence_sha256,
        operation_sha256=operation_sha256,
        idempotency_key=(
            idempotency_key if operation_sha256 is not None else None
        ),
    )
    return WorkspaceCandidate(
        experiment=request,
        experiment_result=result,
        exact_patch=patch,
        summary=value["summary"],
        changed_paths=_strings(
            value["changed_paths"],
            "changed paths",
        ),
        validation=_strings(value["validation"], "validation"),
        expected_tree=value["expected_tree"],
        foundry_operations=tuple(
            _foundry_operation(item)
            for item in _sequence(
                value["foundry_operations"],
                "Foundry operations",
            )
        ),
    )


def _report_context(
    value: Any,
    policy: EvaluationPolicy,
) -> WorkspaceReportContext:
    if not isinstance(value, Mapping):
        raise ValueError("workspace report context is invalid")
    _exact_keys(
        value,
        {
            "baseline_metrics",
            "sample_count",
            "spec_sha256",
            "split",
        },
        "workspace report context",
    )
    return WorkspaceReportContext(
        baseline_metrics=_mapping(
            value["baseline_metrics"],
            "baseline metrics",
        ),
        policy=policy,
        sample_count=value["sample_count"],
        split=value["split"],
        spec_sha256=value["spec_sha256"],
    )


def _foundry_operation(value: Any) -> FoundryOperation:
    if not isinstance(value, Mapping):
        raise ValueError("workspace Foundry operation is invalid")
    allowed = {
        "completed_at",
        "identifier",
        "kind",
        "started_at",
        "status",
        "url",
    }
    _exact_keys(value, allowed, "workspace Foundry operation")
    return FoundryOperation(
        kind=value["kind"],
        identifier=value["identifier"],
        url=value["url"],
        status=value["status"],
        started_at=value["started_at"],
        completed_at=value["completed_at"],
    )


def _exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    name: str,
) -> None:
    if set(value) != expected:
        raise ValueError(f"{name} fields are invalid")


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"workspace {name} are invalid")
    return value


def _sequence(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"workspace {name} are invalid")
    return value


def _strings(value: Any, name: str) -> tuple[str, ...]:
    values = _sequence(value, name)
    if any(not isinstance(item, str) for item in values):
        raise ValueError(f"workspace {name} are invalid")
    return tuple(values)


__all__ = [
    "PreparedCandidateResultRunner",
    "WorkspaceExperimentManifest",
    "parse_workspace_experiment_manifest",
]
