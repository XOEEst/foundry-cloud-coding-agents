from __future__ import annotations

from pathlib import Path

import pytest

from foundry_opt.deployment import DeploymentTrigger
from foundry_opt.orchestration.candidate_experiments import (
    CandidateExperimentOperation,
    CandidateExperimentRequest,
    CandidateExperimentResult,
    PersistedCandidateExperimentOperation,
)
from foundry_opt.orchestration.workspace import (
    WorkspacePhase,
    WorkspaceResult,
)
from foundry_opt.orchestration.workspace_operations_executor import (
    TrustedWorkspaceArtifactContext,
    TrustedWorkspaceExecutionContext,
    WorkspaceCompletionRequest,
    WorkspaceDeploymentArtifact,
    WorkspaceDeploymentTarget,
    WorkspaceFinalizationEffect,
    WorkspaceOperationsExecuteRequest,
    WorkspaceOperationsReconcileRequest,
    WorkspaceOperationsService,
    WorkspaceOperationsStatus,
    WorkspaceReadyForHumanRequest,
    WorkspaceRetentionOutcome,
    WorkspaceRetentionStatus,
    render_workspace_completion_projection,
    render_workspace_ready_for_human_projection,
)


def _persisted_candidate_operation() -> (
    PersistedCandidateExperimentOperation
):
    request = CandidateExperimentRequest(
        issue_number=31,
        candidate_id="candidate-2",
        patch_sha256="1" * 64,
        bundle_sha256="2" * 64,
        evidence_sha256="3" * 64,
        idempotency_key="4" * 64,
    )
    operation = CandidateExperimentOperation.from_request(request)
    return PersistedCandidateExperimentOperation(
        operation=operation,
        reference=(
            "candidate-experiments/"
            f"{operation.idempotency_key}.json"
        ),
        sha256=operation.sha256,
    )


def _candidate_result(
    operation: PersistedCandidateExperimentOperation,
    **overrides,
) -> CandidateExperimentResult:
    values = {
        "candidate_id": operation.operation.candidate_id,
        "executor": "actions_oidc",
        "metrics": {"quality": 0.82},
        "guardrails": {"safety": "pass"},
        "draft_id": "draft-candidate-2",
        "evaluation_id": "eval-candidate-2",
        "run_id": "run-candidate-2",
        "bundle_sha256": operation.operation.bundle_sha256,
        "evidence_sha256": operation.operation.evidence_sha256,
        "operation_sha256": operation.sha256,
        "idempotency_key": operation.operation.idempotency_key,
    }
    values.update(overrides)
    return CandidateExperimentResult(**values)


def _deployment_target(
    *,
    phase: WorkspacePhase = WorkspacePhase.DEPLOYMENT,
) -> WorkspaceDeploymentTarget:
    return WorkspaceDeploymentTarget(
        issue_number=31,
        phase=phase,
        repository="octo-org/optimizer",
        repository_id=123,
        workspace_pull_request_number=104,
        candidate_id="candidate-2",
        patch_sha256="1" * 64,
        bundle_sha256="2" * 64,
        evidence_sha256="3" * 64,
        spec_sha256="4" * 64,
        merge_commit="a" * 40,
        tree_sha="b" * 40,
        workflow_name="Deploy support agent",
        workflow_path=Path(".github/workflows/deploy-foundry-agent.yml"),
        workflow_ref="refs/heads/main",
        workflow_trigger=DeploymentTrigger.MERGE,
        cleanup_refs=("refs/heads/foundry-opt/operations/31",),
        cleanup_drafts=("draft-candidate-2",),
        cleanup_artifacts=(".foundry-optimizer/operations/31.json",),
    )


def _deployment_payload(
    target: WorkspaceDeploymentTarget,
    **overrides,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 2,
        "kind": "deployment_result",
        "status": "completed",
        "issue_number": target.issue_number,
        "workspace_pull_request_number": (
            target.workspace_pull_request_number
        ),
        "operation_id": "deployment-123",
        "candidate_id": target.candidate_id,
        "patch_sha256": target.patch_sha256,
        "bundle_sha256": target.bundle_sha256,
        "evidence_sha256": target.evidence_sha256,
        "spec_sha256": target.spec_sha256,
        "merge_commit": target.merge_commit,
        "tree_sha": target.tree_sha,
        "artifact_name": target.artifact_name,
        "run_id": 991,
        "run_url": (
            "https://github.com/octo-org/optimizer/actions/runs/991"
        ),
        "deployment_version": 13,
        "portal_url": (
            "https://ai.azure.com/projects/demo/agents/demo/versions/13"
        ),
        "lineage_sha256": target.lineage_sha256,
        "repository": {
            "full_name": target.repository,
            "id": target.repository_id,
        },
    }
    payload.update(overrides)
    return payload


def _artifact_context() -> TrustedWorkspaceArtifactContext:
    return TrustedWorkspaceArtifactContext(
        repository="octo-org/optimizer",
        repository_id=123,
        run_id=991,
    )


def _execute_request(tmp_path: Path) -> WorkspaceOperationsExecuteRequest:
    return WorkspaceOperationsExecuteRequest(
        repository_root=tmp_path,
        issue_number=31,
        context=TrustedWorkspaceExecutionContext(
            event_name="workflow_dispatch",
            repository="octo-org/optimizer",
            repository_id=123,
        ),
    )


def _reconcile_request(
    tmp_path: Path,
    target: WorkspaceDeploymentTarget,
    **overrides,
) -> WorkspaceOperationsReconcileRequest:
    return WorkspaceOperationsReconcileRequest(
        repository_root=tmp_path,
        issue_number=31,
        payload=_deployment_payload(target, **overrides),
        context=_artifact_context(),
    )


class RecordingCandidateStore:
    def __init__(
        self,
        operation: PersistedCandidateExperimentOperation | None,
    ) -> None:
        self.operation = operation
        self.result: CandidateExperimentResult | None = None
        self.persist_calls = 0
        self.failures: list[Exception] = []

    def load_pending(
        self,
        issue_number: int,
    ) -> PersistedCandidateExperimentOperation | None:
        return self.operation if issue_number == 31 else None

    def load_result(
        self,
        operation: PersistedCandidateExperimentOperation,
    ) -> CandidateExperimentResult | None:
        return self.result

    def persist_result(
        self,
        operation: PersistedCandidateExperimentOperation,
        result: CandidateExperimentResult,
    ) -> CandidateExperimentResult:
        self.persist_calls += 1
        if self.failures:
            raise self.failures.pop(0)
        self.result = result
        return result


class RecordingCandidateExecutor:
    def __init__(
        self,
        *,
        reconciled: list[CandidateExperimentResult | None],
        executed: list[CandidateExperimentResult | None],
    ) -> None:
        self._reconciled = reconciled
        self._executed = executed
        self.reconcile_calls = 0
        self.execute_calls = 0

    def reconcile(
        self,
        operation: PersistedCandidateExperimentOperation,
    ) -> CandidateExperimentResult | None:
        self.reconcile_calls += 1
        return self._reconciled.pop(0)

    def execute(
        self,
        operation: PersistedCandidateExperimentOperation,
    ) -> CandidateExperimentResult | None:
        self.execute_calls += 1
        return self._executed.pop(0)


class StaticDeploymentLoader:
    def __init__(self, target: WorkspaceDeploymentTarget | None) -> None:
        self.target = target

    def load(self, issue_number: int) -> WorkspaceDeploymentTarget | None:
        return self.target if issue_number == 31 else None


class RecordingWorkspaceService:
    def __init__(self, results: list[WorkspaceResult]) -> None:
        self._results = results
        self.requests = []

    def advance(self, request):
        self.requests.append(request)
        return self._results.pop(0)


class RecordingRetentionEvaluator:
    def __init__(self, outcome: WorkspaceRetentionOutcome) -> None:
        self.outcome = outcome
        self.calls: list[tuple[Path, WorkspaceDeploymentTarget, WorkspaceDeploymentArtifact]] = []

    def evaluate(
        self,
        repository_root: Path,
        target: WorkspaceDeploymentTarget,
        artifact: WorkspaceDeploymentArtifact,
    ) -> WorkspaceRetentionOutcome:
        self.calls.append((repository_root, target, artifact))
        return self.outcome


class RecordingRunVerifier:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, WorkspaceDeploymentTarget, WorkspaceDeploymentArtifact]] = []

    def verify(
        self,
        repository_root: Path,
        target: WorkspaceDeploymentTarget,
        artifact: WorkspaceDeploymentArtifact,
    ) -> None:
        self.calls.append((repository_root, target, artifact))


class RecordingFinalizer:
    def __init__(self) -> None:
        self.complete_calls: list[WorkspaceCompletionRequest] = []
        self.ready_calls: list[WorkspaceReadyForHumanRequest] = []

    def complete(
        self,
        request: WorkspaceCompletionRequest,
    ) -> WorkspaceFinalizationEffect:
        self.complete_calls.append(request)
        return WorkspaceFinalizationEffect(
            finalized=True,
            closed_issue=True,
            cleaned_refs=request.target.cleanup_refs,
            cleaned_drafts=request.target.cleanup_drafts,
            cleaned_artifacts=request.target.cleanup_artifacts,
            projection=render_workspace_completion_projection(request),
        )

    def ready_for_human(
        self,
        request: WorkspaceReadyForHumanRequest,
    ) -> WorkspaceFinalizationEffect:
        self.ready_calls.append(request)
        return WorkspaceFinalizationEffect(
            finalized=False,
            closed_issue=False,
            projection=render_workspace_ready_for_human_projection(
                request
            ),
        )


def _workspace_result(phase: WorkspacePhase, *, recorded: bool) -> WorkspaceResult:
    return WorkspaceResult(
        phase=phase,
        workspace_pull_request=None,
        planned_effect_kinds=(),
        recorded=recorded,
    )


def test_candidate_fallback_is_idempotent_across_retries(
    tmp_path: Path,
) -> None:
    persisted = _persisted_candidate_operation()
    store = RecordingCandidateStore(persisted)
    executor = RecordingCandidateExecutor(
        reconciled=[None],
        executed=[_candidate_result(persisted)],
    )
    service = WorkspaceOperationsService(
        candidate_store=store,
        candidate_executor=executor,
    )

    first = service.execute(_execute_request(tmp_path))
    second = service.execute(_execute_request(tmp_path))

    assert first.status is WorkspaceOperationsStatus.CANDIDATE_RECORDED
    assert first.recorded is True
    assert second.status is WorkspaceOperationsStatus.CANDIDATE_RECORDED
    assert second.recorded is False
    assert executor.execute_calls == 1
    assert executor.reconcile_calls == 1
    assert store.persist_calls == 1


def test_candidate_fallback_recovers_after_persist_ack_loss(
    tmp_path: Path,
) -> None:
    persisted = _persisted_candidate_operation()
    result = _candidate_result(persisted)
    store = RecordingCandidateStore(persisted)
    store.failures.append(RuntimeError("ack lost"))
    executor = RecordingCandidateExecutor(
        reconciled=[None, result],
        executed=[result],
    )
    service = WorkspaceOperationsService(
        candidate_store=store,
        candidate_executor=executor,
    )

    with pytest.raises(RuntimeError, match="ack lost"):
        service.execute(_execute_request(tmp_path))

    recovered = service.execute(_execute_request(tmp_path))

    assert recovered.status is WorkspaceOperationsStatus.CANDIDATE_RECORDED
    assert recovered.recorded is True
    assert executor.execute_calls == 1
    assert executor.reconcile_calls == 2
    assert store.persist_calls == 2
    assert store.result == result


def test_candidate_fallback_rejects_tampered_result(
    tmp_path: Path,
) -> None:
    persisted = _persisted_candidate_operation()
    executor = RecordingCandidateExecutor(
        reconciled=[
            _candidate_result(
                persisted,
                bundle_sha256="9" * 64,
            )
        ],
        executed=[],
    )
    service = WorkspaceOperationsService(
        candidate_store=RecordingCandidateStore(persisted),
        candidate_executor=executor,
    )

    with pytest.raises(ValueError, match="lineage"):
        service.execute(_execute_request(tmp_path))

    assert executor.execute_calls == 0


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("bundle_sha256", "9" * 64),
        ("spec_sha256", "8" * 64),
        ("tree_sha", "c" * 40),
        ("lineage_sha256", "7" * 64),
        ("run_id", 992),
    ),
)
def test_reconcile_requires_exact_deployment_lineage(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    target = _deployment_target()
    service = WorkspaceOperationsService(
        deployment_loader=StaticDeploymentLoader(target),
        workspace_service=RecordingWorkspaceService(
            [_workspace_result(WorkspacePhase.RETENTION, recorded=True)]
        ),
    )

    with pytest.raises(ValueError, match="workspace deployment"):
        service.reconcile(
            _reconcile_request(tmp_path, target, **{field: value})
        )


def test_reconcile_retained_improvement_finalizes_and_cleans_up(
    tmp_path: Path,
) -> None:
    target = _deployment_target()
    workspace = RecordingWorkspaceService(
        [
            _workspace_result(WorkspacePhase.RETENTION, recorded=True),
            _workspace_result(WorkspacePhase.COMPLETED, recorded=True),
        ]
    )
    evaluator = RecordingRetentionEvaluator(
        WorkspaceRetentionOutcome(
            status=WorkspaceRetentionStatus.RETAINED_IMPROVEMENT,
            operation_id="retention-123",
            baseline_metrics={"quality": 0.70},
            selected_metrics={"quality": 0.90},
            deployed_metrics={"quality": 0.89},
        )
    )
    verifier = RecordingRunVerifier()
    finalizer = RecordingFinalizer()
    service = WorkspaceOperationsService(
        deployment_loader=StaticDeploymentLoader(target),
        deployment_verifier=verifier,
        workspace_service=workspace,
        retention_evaluator=evaluator,
        finalizer=finalizer,
    )

    result = service.reconcile(_reconcile_request(tmp_path, target))

    assert result.status is WorkspaceOperationsStatus.COMPLETED
    assert result.phase is WorkspacePhase.COMPLETED
    assert result.finalization is not None
    assert result.finalization.finalized is True
    assert result.finalization.closed_issue is True
    assert result.finalization.cleaned_refs == target.cleanup_refs
    assert result.finalization.cleaned_drafts == target.cleanup_drafts
    assert result.finalization.cleaned_artifacts == (
        target.cleanup_artifacts
    )
    assert len(workspace.requests) == 2
    assert workspace.requests[0].trigger.value == "deployment_completed"
    assert workspace.requests[0].operation.operation_id == "deployment-123"
    assert workspace.requests[1].trigger.value == "retention_completed"
    assert (
        workspace.requests[1].operation.predecessor_operation_id
        == "deployment-123"
    )
    assert len(evaluator.calls) == 1
    assert len(verifier.calls) == 1
    assert len(finalizer.complete_calls) == 1
    assert not finalizer.ready_calls


def test_reconcile_regression_leaves_issue_open_ready_for_human(
    tmp_path: Path,
) -> None:
    target = _deployment_target()
    workspace = RecordingWorkspaceService(
        [_workspace_result(WorkspacePhase.RETENTION, recorded=True)]
    )
    evaluator = RecordingRetentionEvaluator(
        WorkspaceRetentionOutcome(
            status=WorkspaceRetentionStatus.REGRESSED,
            operation_id="retention-123",
            baseline_metrics={"quality": 0.70},
            selected_metrics={"quality": 0.90},
            deployed_metrics={"quality": 0.60},
            reason="baseline_regression",
        )
    )
    finalizer = RecordingFinalizer()
    service = WorkspaceOperationsService(
        deployment_loader=StaticDeploymentLoader(target),
        deployment_verifier=RecordingRunVerifier(),
        workspace_service=workspace,
        retention_evaluator=evaluator,
        finalizer=finalizer,
    )

    result = service.reconcile(_reconcile_request(tmp_path, target))

    assert result.status is WorkspaceOperationsStatus.READY_FOR_HUMAN
    assert result.phase is WorkspacePhase.RETENTION
    assert result.finalization is not None
    assert result.finalization.closed_issue is False
    assert len(workspace.requests) == 1
    assert workspace.requests[0].trigger.value == "deployment_completed"
    assert not finalizer.complete_calls
    assert len(finalizer.ready_calls) == 1


def test_reconcile_duplicate_completed_deployment_skips_re_evaluation(
    tmp_path: Path,
) -> None:
    target = _deployment_target(phase=WorkspacePhase.COMPLETED)
    workspace = RecordingWorkspaceService(
        [_workspace_result(WorkspacePhase.COMPLETED, recorded=False)]
    )
    evaluator = RecordingRetentionEvaluator(
        WorkspaceRetentionOutcome(
            status=WorkspaceRetentionStatus.RETAINED_IMPROVEMENT,
            operation_id="retention-123",
        )
    )
    finalizer = RecordingFinalizer()
    service = WorkspaceOperationsService(
        deployment_loader=StaticDeploymentLoader(target),
        deployment_verifier=RecordingRunVerifier(),
        workspace_service=workspace,
        retention_evaluator=evaluator,
        finalizer=finalizer,
    )

    result = service.reconcile(_reconcile_request(tmp_path, target))

    assert result.status is WorkspaceOperationsStatus.COMPLETED
    assert result.recorded is False
    assert not evaluator.calls
    assert not finalizer.complete_calls
    assert not finalizer.ready_calls
