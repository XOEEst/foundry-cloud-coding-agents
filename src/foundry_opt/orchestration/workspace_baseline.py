from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from foundry_opt.orchestration.candidate_experiments import (
    CandidateExperimentAdapter,
    CandidateExperimentOperation,
    CandidateExperimentPending,
    CandidateExperimentRequest,
    CandidateExperimentResult,
)
from foundry_opt.orchestration.workspace import WorkspacePhase
from foundry_opt.orchestration.workspace_runtime import WorkspaceStore
from foundry_opt.orchestration.workspace_store import (
    WorkspaceBaselineRecord,
    WorkspaceSpecificationRecord,
    WorkspaceUpdate,
)


@dataclass(frozen=True)
class WorkspaceBaselinePlan:
    request: CandidateExperimentRequest
    dataset_ids: tuple[str, ...]
    evaluator_ids: tuple[str, ...]
    sample_count: int
    split: str = "development"

    def __post_init__(self) -> None:
        if self.request.candidate_id != "baseline":
            raise ValueError("workspace baseline candidate binding is invalid")
        WorkspaceBaselineRecord(
            status="pending",
            operation_sha256=CandidateExperimentOperation.from_request(
                self.request
            ).sha256,
            idempotency_key=self.request.idempotency_key,
            bundle_sha256=self.request.bundle_sha256,
            evidence_sha256=self.request.evidence_sha256,
            dataset_ids=self.dataset_ids,
            evaluator_ids=self.evaluator_ids,
            split=self.split,
            sample_count=self.sample_count,
        )


class WorkspaceBaselineRequestBuilder(Protocol):
    def build(
        self,
        *,
        repository_root: Path,
        issue_number: int,
        target: str,
        base_commit: str,
        specification: WorkspaceSpecificationRecord,
    ) -> WorkspaceBaselinePlan: ...


@dataclass(frozen=True)
class WorkspaceBaselineExecutionResult:
    issue_number: int
    status: str
    recorded: bool
    operation_sha256: str
    next_action: str

    def to_dict(self) -> dict[str, int | str | bool]:
        return {
            "issue_number": self.issue_number,
            "next_action": self.next_action,
            "operation_sha256": self.operation_sha256,
            "recorded": self.recorded,
            "status": self.status,
        }


class WorkspaceBaselineExecutor:
    def __init__(
        self,
        *,
        store: WorkspaceStore,
        runner: CandidateExperimentAdapter | None,
        request_builder: WorkspaceBaselineRequestBuilder | None,
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
    ) -> WorkspaceBaselineExecutionResult:
        snapshot = self._store.load(issue_number)
        if snapshot is None or snapshot.phase is not WorkspacePhase.SPECIFICATION:
            raise ValueError("workspace baseline state is unavailable")
        specification = snapshot.specification
        if (
            specification is None
            or specification.status != "policy_approved"
            or specification.target != target
            or specification.base_commit != base_commit
        ):
            raise ValueError("trusted workspace specification is required")
        if snapshot.baseline is not None:
            if snapshot.baseline.status == "completed":
                return _execution_result(
                    issue_number, snapshot.baseline, recorded=False
                )
            plan = _plan_from_record(
                issue_number, specification, snapshot.baseline
            )
            recorded = False
        else:
            if self._runner is None or self._request_builder is None:
                raise ValueError("workspace baseline executor is unavailable")
            plan = self._request_builder.build(
                repository_root=repository_root,
                issue_number=issue_number,
                target=target,
                base_commit=base_commit,
                specification=specification,
            )
            _validate_plan(plan, issue_number, specification)
            pending = _pending_record(plan)
            snapshot = self._store.commit(
                expected_revision=snapshot.revision,
                update=WorkspaceUpdate(
                    issue_number=issue_number,
                    phase=snapshot.phase,
                    workspace_pull_request_number=(
                        snapshot.workspace_pull_request_number
                    ),
                    semantic_event="trusted_baseline_started",
                    candidates=snapshot.candidates,
                    selected_patch=snapshot.selected_patch,
                    external_operation_ids=tuple(
                        dict.fromkeys(
                            (
                                *snapshot.external_operation_ids,
                                "baseline_operation:"
                                f"{pending.operation_sha256}",
                            )
                        )
                    ),
                    experiments=snapshot.experiments,
                    lineage=snapshot.lineage,
                    specification=specification,
                    baseline=pending,
                ),
            )
            recorded = True
        if self._runner is None:
            raise ValueError("workspace baseline executor is unavailable")
        try:
            result = self._runner.evaluate(plan.request)
        except CandidateExperimentPending:
            assert snapshot.baseline is not None
            return _execution_result(
                issue_number, snapshot.baseline, recorded=recorded
            )
        return self._complete(snapshot, plan, result)

    def ingest_result(
        self,
        *,
        issue_number: int,
        result: CandidateExperimentResult,
    ) -> WorkspaceBaselineExecutionResult:
        snapshot = self._store.load(issue_number)
        if (
            snapshot is None
            or snapshot.specification is None
            or snapshot.baseline is None
        ):
            raise ValueError("workspace pending baseline is missing")
        if snapshot.baseline.status == "completed":
            expected = _result_from_record(snapshot.baseline)
            if expected != result:
                raise ValueError("trusted workspace baseline changed")
            return _execution_result(
                issue_number, snapshot.baseline, recorded=False
            )
        plan = _plan_from_record(
            issue_number, snapshot.specification, snapshot.baseline
        )
        return self._complete(snapshot, plan, result)

    def _complete(
        self,
        snapshot,
        plan: WorkspaceBaselinePlan,
        result: CandidateExperimentResult,
    ) -> WorkspaceBaselineExecutionResult:
        specification = snapshot.specification
        assert specification is not None
        completed = _completed_record(plan, result, specification)
        committed = self._store.commit(
            expected_revision=snapshot.revision,
            update=WorkspaceUpdate(
                issue_number=snapshot.issue_number,
                phase=snapshot.phase,
                workspace_pull_request_number=(
                    snapshot.workspace_pull_request_number
                ),
                semantic_event="trusted_baseline_completed",
                candidates=snapshot.candidates,
                selected_patch=snapshot.selected_patch,
                external_operation_ids=tuple(
                    dict.fromkeys(
                        (
                            *snapshot.external_operation_ids,
                            result.draft_id,
                            result.evaluation_id,
                            result.run_id,
                            f"baseline:bundle:{result.bundle_sha256}",
                            f"baseline:evidence:{result.evidence_sha256}",
                        )
                    )
                ),
                experiments=snapshot.experiments,
                lineage=snapshot.lineage,
                specification=specification,
                baseline=completed,
            ),
        )
        assert committed.baseline is not None
        return _execution_result(
            snapshot.issue_number, committed.baseline, recorded=True
        )


def _validate_plan(
    plan: WorkspaceBaselinePlan,
    issue_number: int,
    specification: WorkspaceSpecificationRecord,
) -> None:
    if (
        plan.request.issue_number != issue_number
        or plan.request.patch_sha256 != specification.spec_sha256
    ):
        raise ValueError("workspace baseline plan binding changed")


def _pending_record(
    plan: WorkspaceBaselinePlan,
) -> WorkspaceBaselineRecord:
    return WorkspaceBaselineRecord(
        status="pending",
        operation_sha256=CandidateExperimentOperation.from_request(
            plan.request
        ).sha256,
        idempotency_key=plan.request.idempotency_key,
        bundle_sha256=plan.request.bundle_sha256,
        evidence_sha256=plan.request.evidence_sha256,
        dataset_ids=plan.dataset_ids,
        evaluator_ids=plan.evaluator_ids,
        split=plan.split,
        sample_count=plan.sample_count,
    )


def _completed_record(
    plan: WorkspaceBaselinePlan,
    result: CandidateExperimentResult,
    specification: WorkspaceSpecificationRecord,
) -> WorkspaceBaselineRecord:
    operation_sha256 = CandidateExperimentOperation.from_request(
        plan.request
    ).sha256
    if (
        result.candidate_id != "baseline"
        or result.bundle_sha256 != plan.request.bundle_sha256
        or result.evidence_sha256 != plan.request.evidence_sha256
        or result.operation_sha256 != operation_sha256
        or result.idempotency_key != plan.request.idempotency_key
        or set(result.metrics) != set(specification.metric_names)
    ):
        raise ValueError("trusted workspace baseline lineage changed")
    return WorkspaceBaselineRecord(
        status="completed",
        operation_sha256=operation_sha256,
        idempotency_key=plan.request.idempotency_key,
        bundle_sha256=result.bundle_sha256,
        evidence_sha256=result.evidence_sha256,
        dataset_ids=plan.dataset_ids,
        evaluator_ids=plan.evaluator_ids,
        split=plan.split,
        sample_count=plan.sample_count,
        executor=result.executor,
        draft_id=result.draft_id,
        evaluation_id=result.evaluation_id,
        run_id=result.run_id,
        metrics=result.metrics,
        guardrails=result.guardrails,
    )


def _plan_from_record(
    issue_number: int,
    specification: WorkspaceSpecificationRecord,
    record: WorkspaceBaselineRecord,
) -> WorkspaceBaselinePlan:
    return WorkspaceBaselinePlan(
        request=CandidateExperimentRequest(
            issue_number=issue_number,
            candidate_id="baseline",
            patch_sha256=specification.spec_sha256,
            bundle_sha256=record.bundle_sha256,
            evidence_sha256=record.evidence_sha256,
            idempotency_key=record.idempotency_key,
        ),
        dataset_ids=record.dataset_ids,
        evaluator_ids=record.evaluator_ids,
        sample_count=record.sample_count,
        split=record.split,
    )


def _result_from_record(
    record: WorkspaceBaselineRecord,
) -> CandidateExperimentResult:
    assert record.executor is not None
    assert record.draft_id is not None
    assert record.evaluation_id is not None
    assert record.run_id is not None
    return CandidateExperimentResult(
        candidate_id="baseline",
        executor=record.executor,
        metrics=record.metrics,
        guardrails=record.guardrails,
        draft_id=record.draft_id,
        evaluation_id=record.evaluation_id,
        run_id=record.run_id,
        bundle_sha256=record.bundle_sha256,
        evidence_sha256=record.evidence_sha256,
        operation_sha256=record.operation_sha256,
        idempotency_key=record.idempotency_key,
    )


def _execution_result(
    issue_number: int,
    record: WorkspaceBaselineRecord,
    *,
    recorded: bool,
) -> WorkspaceBaselineExecutionResult:
    return WorkspaceBaselineExecutionResult(
        issue_number=issue_number,
        status=record.status,
        recorded=recorded,
        operation_sha256=record.operation_sha256,
        next_action=(
            "continue_workspace"
            if record.status == "completed"
            else "await_trusted_actions_result"
        ),
    )


__all__ = [
    "WorkspaceBaselineExecutionResult",
    "WorkspaceBaselineExecutor",
    "WorkspaceBaselinePlan",
    "WorkspaceBaselineRequestBuilder",
]
