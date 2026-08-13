from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Mapping, Protocol

from foundry_opt.orchestration.candidate_experiments import (
    CandidateExperimentAdapter,
    CandidateExperimentOperation,
    CandidateExperimentPending,
    CandidateExperimentRequest,
    CandidateExperimentResult,
)
from foundry_opt.orchestration.workspace import (
    WorkspaceCandidateProposal,
    WorkspacePhase,
)
from foundry_opt.orchestration.workspace_runtime import WorkspaceStore
from foundry_opt.orchestration.workspace_store import (
    WorkspaceExperimentRecord,
    WorkspaceUpdate,
)
from foundry_opt.security import reject_secret_content


class WorkspaceExperimentRequestBuilder(Protocol):
    def build(
        self,
        *,
        repository_root: Path,
        issue_number: int,
        target: str,
        base_commit: str,
        proposal: WorkspaceCandidateProposal,
    ) -> CandidateExperimentRequest: ...


@dataclass(frozen=True)
class TrustedWorkspaceExperimentResultContext:
    delivery_id: str
    repository: str
    repository_id: int

    def __post_init__(self) -> None:
        if (
            re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}",
                self.delivery_id,
            )
            is None
            or re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/"
                r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}",
                self.repository,
            )
            is None
            or type(self.repository_id) is not int
            or self.repository_id < 1
        ):
            raise ValueError(
                "trusted workspace experiment context is invalid"
            )


@dataclass(frozen=True)
class NormalizedWorkspaceExperimentResult:
    issue_number: int
    repository: str
    repository_id: int
    delivery_id: str
    result: CandidateExperimentResult


def normalize_workspace_experiment_result(
    payload: Mapping[str, Any],
    context: TrustedWorkspaceExperimentResultContext,
) -> NormalizedWorkspaceExperimentResult:
    reject_secret_content(payload)
    expected = {
        "bundle_sha256",
        "candidate_id",
        "draft_id",
        "evaluation_id",
        "evidence_sha256",
        "executor",
        "guardrails",
        "idempotency_key",
        "issue_number",
        "metrics",
        "operation_sha256",
        "repository",
        "run_id",
        "schema_version",
    }
    if set(payload) != expected or payload["schema_version"] != 1:
        raise ValueError("trusted experiment result fields are invalid")
    repository = payload["repository"]
    if (
        not isinstance(repository, Mapping)
        or set(repository) != {"full_name", "id"}
        or repository["full_name"] != context.repository
        or repository["id"] != context.repository_id
    ):
        raise ValueError("trusted experiment repository changed")
    issue_number = payload["issue_number"]
    if type(issue_number) is not int or issue_number < 1:
        raise ValueError("trusted experiment issue is invalid")
    result = CandidateExperimentResult(
        candidate_id=payload["candidate_id"],
        executor=payload["executor"],
        metrics=payload["metrics"],
        guardrails=payload["guardrails"],
        draft_id=payload["draft_id"],
        evaluation_id=payload["evaluation_id"],
        run_id=payload["run_id"],
        bundle_sha256=payload["bundle_sha256"],
        evidence_sha256=payload["evidence_sha256"],
        operation_sha256=payload["operation_sha256"],
        idempotency_key=payload["idempotency_key"],
    )
    return NormalizedWorkspaceExperimentResult(
        issue_number=issue_number,
        repository=context.repository,
        repository_id=context.repository_id,
        delivery_id=context.delivery_id,
        result=result,
    )


@dataclass(frozen=True)
class WorkspaceExperimentExecutionResult:
    issue_number: int
    candidate_id: str
    status: str
    recorded: bool
    operation_sha256: str
    idempotency_key: str
    next_action: str

    @property
    def exit_code(self) -> int:
        return 0

    def to_dict(self) -> dict[str, bool | int | str]:
        return {
            "candidate_id": self.candidate_id,
            "idempotency_key": self.idempotency_key,
            "issue_number": self.issue_number,
            "next_action": self.next_action,
            "operation_sha256": self.operation_sha256,
            "recorded": self.recorded,
            "status": self.status,
        }


class WorkspaceExperimentExecutor:
    def __init__(
        self,
        *,
        store: WorkspaceStore,
        runner: CandidateExperimentAdapter | None,
        request_builder: WorkspaceExperimentRequestBuilder | None,
    ) -> None:
        self._store = store
        self._runner = runner
        self._request_builder = request_builder

    def execute(
        self,
        *,
        repository_root: Path,
        issue_number: int,
        target: str,
        base_commit: str,
        proposal: WorkspaceCandidateProposal,
    ) -> WorkspaceExperimentExecutionResult:
        snapshot = self._store.load(issue_number)
        if self._runner is None or self._request_builder is None:
            raise ValueError("workspace experiment executor is unavailable")
        if snapshot is None or snapshot.phase not in {
            WorkspacePhase.SPECIFICATION,
            WorkspacePhase.EVALUATING,
        }:
            raise ValueError("workspace experiment state is unavailable")
        existing = {
            item.candidate_id: item for item in snapshot.experiments
        }.get(proposal.candidate_id)
        pending_recorded = False
        if existing is not None:
            request = _request_from_record(issue_number, existing)
            _validate_proposal(proposal, request)
            if existing.status == "completed":
                return _execution_result(
                    issue_number,
                    existing,
                    recorded=False,
                )
        else:
            request = self._request_builder.build(
                repository_root=repository_root,
                issue_number=issue_number,
                target=target,
                base_commit=base_commit,
                proposal=proposal,
            )
            _validate_proposal(proposal, request)
            operation_sha256 = CandidateExperimentOperation.from_request(
                request
            ).sha256
            pending = WorkspaceExperimentRecord(
                candidate_id=request.candidate_id,
                patch_sha256=request.patch_sha256,
                bundle_sha256=request.bundle_sha256,
                evidence_sha256=request.evidence_sha256,
                idempotency_key=request.idempotency_key,
                operation_sha256=operation_sha256,
                status="pending",
            )
            snapshot = self._store.commit(
                expected_revision=snapshot.revision,
                update=WorkspaceUpdate(
                    issue_number=issue_number,
                    phase=WorkspacePhase.EVALUATING,
                    workspace_pull_request_number=(
                        snapshot.workspace_pull_request_number
                    ),
                    semantic_event=(
                        f"candidate_experiment_started_"
                        f"{proposal.candidate_id}"
                    ),
                    candidates=snapshot.candidates,
                    selected_patch=snapshot.selected_patch,
                    external_operation_ids=(
                        *snapshot.external_operation_ids,
                        f"experiment_operation:{operation_sha256}",
                    ),
                    experiments=(*snapshot.experiments, pending),
                    lineage=snapshot.lineage,
                ),
            )
            existing = pending
            pending_recorded = True
        try:
            result = self._runner.evaluate(request)
        except CandidateExperimentPending:
            return _execution_result(
                issue_number,
                existing,
                recorded=pending_recorded,
            )
        completed = _completed_record(request, result)
        records = tuple(
            completed if item.candidate_id == completed.candidate_id else item
            for item in snapshot.experiments
        )
        self._store.commit(
            expected_revision=snapshot.revision,
            update=WorkspaceUpdate(
                issue_number=issue_number,
                phase=WorkspacePhase.EVALUATING,
                workspace_pull_request_number=(
                    snapshot.workspace_pull_request_number
                ),
                semantic_event=(
                    f"candidate_experiment_completed_"
                    f"{proposal.candidate_id}"
                ),
                candidates=snapshot.candidates,
                selected_patch=snapshot.selected_patch,
                external_operation_ids=tuple(
                    dict.fromkeys(
                        (
                            *snapshot.external_operation_ids,
                            result.draft_id,
                            result.evaluation_id,
                            result.run_id,
                            (
                                f"{result.candidate_id}:bundle:"
                                f"{result.bundle_sha256}"
                            ),
                            (
                                f"{result.candidate_id}:evidence:"
                                f"{result.evidence_sha256}"
                            ),
                        )
                    )
                ),
                experiments=records,
                lineage=snapshot.lineage,
            ),
        )
        return _execution_result(
            issue_number,
            completed,
            recorded=True,
        )

    def ingest_result(
        self,
        *,
        issue_number: int,
        result: CandidateExperimentResult,
    ) -> WorkspaceExperimentExecutionResult:
        snapshot = self._store.load(issue_number)
        if snapshot is None:
            raise ValueError("workspace experiment state is unavailable")
        pending = next(
            (
                item
                for item in snapshot.experiments
                if item.candidate_id == result.candidate_id
            ),
            None,
        )
        if pending is None:
            raise ValueError("workspace pending experiment is missing")
        if pending.status == "completed":
            if _result_from_record(pending) != result:
                raise ValueError("trusted workspace experiment changed")
            return _execution_result(
                issue_number,
                pending,
                recorded=False,
            )
        request = _request_from_record(issue_number, pending)
        completed = _completed_record(request, result)
        records = tuple(
            completed if item.candidate_id == completed.candidate_id else item
            for item in snapshot.experiments
        )
        self._store.commit(
            expected_revision=snapshot.revision,
            update=WorkspaceUpdate(
                issue_number=issue_number,
                phase=WorkspacePhase.EVALUATING,
                workspace_pull_request_number=(
                    snapshot.workspace_pull_request_number
                ),
                semantic_event=(
                    f"candidate_experiment_ingested_"
                    f"{result.candidate_id}"
                ),
                candidates=snapshot.candidates,
                selected_patch=snapshot.selected_patch,
                external_operation_ids=tuple(
                    dict.fromkeys(
                        (
                            *snapshot.external_operation_ids,
                            result.draft_id,
                            result.evaluation_id,
                            result.run_id,
                            (
                                f"{result.candidate_id}:bundle:"
                                f"{result.bundle_sha256}"
                            ),
                            (
                                f"{result.candidate_id}:evidence:"
                                f"{result.evidence_sha256}"
                            ),
                        )
                    )
                ),
                experiments=records,
                lineage=snapshot.lineage,
            ),
        )
        return _execution_result(
            issue_number,
            completed,
            recorded=True,
        )


def _completed_record(
    request: CandidateExperimentRequest,
    result: CandidateExperimentResult,
) -> WorkspaceExperimentRecord:
    operation_sha256 = CandidateExperimentOperation.from_request(
        request
    ).sha256
    if (
        result.candidate_id != request.candidate_id
        or result.bundle_sha256 != request.bundle_sha256
        or result.evidence_sha256 != request.evidence_sha256
        or result.idempotency_key != request.idempotency_key
        or result.operation_sha256 != operation_sha256
    ):
        raise ValueError("trusted workspace experiment lineage changed")
    return WorkspaceExperimentRecord(
        candidate_id=request.candidate_id,
        patch_sha256=request.patch_sha256,
        bundle_sha256=request.bundle_sha256,
        evidence_sha256=request.evidence_sha256,
        idempotency_key=request.idempotency_key,
        operation_sha256=operation_sha256,
        status="completed",
        executor=result.executor,
        draft_id=result.draft_id,
        evaluation_id=result.evaluation_id,
        run_id=result.run_id,
        metrics=result.metrics,
        guardrails=result.guardrails,
    )


def _validate_proposal(
    proposal: WorkspaceCandidateProposal,
    request: CandidateExperimentRequest,
) -> None:
    if (
        request.candidate_id != proposal.candidate_id
        or request.patch_sha256 != proposal.patch_sha256
        or request.idempotency_key != proposal.idempotency_key
    ):
        raise ValueError("workspace experiment proposal binding changed")


def _request_from_record(
    issue_number: int,
    record: WorkspaceExperimentRecord,
) -> CandidateExperimentRequest:
    return CandidateExperimentRequest(
        issue_number=issue_number,
        candidate_id=record.candidate_id,
        patch_sha256=record.patch_sha256,
        bundle_sha256=record.bundle_sha256,
        evidence_sha256=record.evidence_sha256,
        idempotency_key=record.idempotency_key,
    )


def _result_from_record(
    record: WorkspaceExperimentRecord,
) -> CandidateExperimentResult:
    if record.status != "completed":
        raise ValueError("workspace experiment is not completed")
    return CandidateExperimentResult(
        candidate_id=record.candidate_id,
        executor=record.executor or "",
        metrics=record.metrics,
        guardrails=record.guardrails,
        draft_id=record.draft_id or "",
        evaluation_id=record.evaluation_id or "",
        run_id=record.run_id or "",
        bundle_sha256=record.bundle_sha256,
        evidence_sha256=record.evidence_sha256,
        operation_sha256=record.operation_sha256,
        idempotency_key=record.idempotency_key,
    )


def _execution_result(
    issue_number: int,
    record: WorkspaceExperimentRecord,
    *,
    recorded: bool,
) -> WorkspaceExperimentExecutionResult:
    return WorkspaceExperimentExecutionResult(
        issue_number=issue_number,
        candidate_id=record.candidate_id,
        status=record.status,
        recorded=recorded,
        operation_sha256=record.operation_sha256,
        idempotency_key=record.idempotency_key,
        next_action=(
            "experiments_complete"
            if record.status == "completed"
            else "await_trusted_actions_result"
        ),
    )


__all__ = [
    "NormalizedWorkspaceExperimentResult",
    "TrustedWorkspaceExperimentResultContext",
    "WorkspaceExperimentExecutionResult",
    "WorkspaceExperimentExecutor",
    "WorkspaceExperimentRequestBuilder",
    "normalize_workspace_experiment_result",
]
