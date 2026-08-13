from __future__ import annotations

from pathlib import Path

import pytest

from foundry_opt.deployment import DeploymentTrigger
from foundry_opt.evaluation import (
    EvaluationPolicy,
    MetricDirection,
    MetricPolicy,
    UndefinedBehavior,
)
from foundry_opt.evidence import EvaluationAssetReference
from foundry_opt.orchestration.candidate_experiments import (
    CandidateExperimentOperation,
    CandidateExperimentRequest,
    CandidateExperimentResult,
    PersistedCandidateExperimentOperation,
)
from foundry_opt.orchestration.workspace import (
    WorkspaceNextAction,
    WorkspaceNextActionKind,
    WorkspacePhase,
    WorkspaceResult,
    WorkspaceTrigger,
)
from foundry_opt.orchestration.workspace_operations_executor import (
    StoredCandidateExperimentResult,
    PendingCandidateExperimentExecution,
    TrustedWorkspaceArtifactContext,
    TrustedCandidateExecutionPlan,
    TrustedCandidatePackagingContract,
    TrustedWorkspaceExecutionContext,
    CandidateExperimentExecutionPlanner,
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


def _pending_candidate_execution(
    operation: PersistedCandidateExperimentOperation,
    *,
    request_payload: dict[str, object] | None = None,
) -> PendingCandidateExperimentExecution:
    return PendingCandidateExperimentExecution(
        operation=operation,
        request_payload=request_payload or {},
    )


def _candidate_plan(
    pending: PendingCandidateExperimentExecution,
    **overrides,
) -> TrustedCandidateExecutionPlan:
    operation = pending.operation
    values = {
        "operation": operation,
        "request": CandidateExperimentRequest(
            issue_number=operation.operation.issue_number,
            candidate_id=operation.operation.candidate_id,
            patch_sha256=operation.operation.patch_sha256,
            bundle_sha256=operation.operation.bundle_sha256,
            evidence_sha256=operation.operation.evidence_sha256,
            idempotency_key=operation.operation.idempotency_key,
        ),
        "base_commit": "a" * 40,
        "target_name": "support-agent",
        "base_agent_version": 7,
        "allowed_paths": ("src/agent.py", "tests/test_agent.py"),
        "allowed_mutations": frozenset({"python_logic"}),
        "validation_commands": (("python", "-m", "pytest", "-q"),),
        "packaging": TrustedCandidatePackagingContract(
            include=("src/**", "tests/**"),
            exclude=(".venv/**",),
            dependency_resolution="remote_build",
            evidence_paths=(".foundry-optimizer/campaigns",),
        ),
        "assets": (
            EvaluationAssetReference(
                asset_id="dataset-development",
                kind="dataset",
                source="foundry",
                role="development",
                name="support-dev",
                version="1",
                remote_id="foundry:dataset:support-dev:1",
            ),
            EvaluationAssetReference(
                asset_id="dataset-validation",
                kind="dataset",
                source="foundry",
                role="validation",
                name="support-val",
                version="1",
                remote_id="foundry:dataset:support-val:1",
            ),
            EvaluationAssetReference(
                asset_id="evaluator-quality",
                kind="evaluator",
                source="builtin",
                name="quality-evaluator",
                version="1",
                remote_id="builtin:quality-evaluator:1",
                metrics=("quality",),
            ),
        ),
        "evaluation_policy": EvaluationPolicy(
            metrics=(
                MetricPolicy(
                    name="quality",
                    direction=MetricDirection.MAXIMIZE,
                    threshold=0.7,
                    materiality=0.1,
                    hard_guardrail=False,
                    undefined_behavior=UndefinedBehavior.FAIL,
                ),
            )
        ),
        "candidate_limit": 3,
    }
    values.update(overrides)
    return TrustedCandidateExecutionPlan(**values)


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
        pending: PendingCandidateExperimentExecution | None,
    ) -> None:
        self.pending = pending
        self.result: StoredCandidateExperimentResult | None = None
        self.workspace: WorkspaceResult | None = None
        self.persist_calls = 0
        self.failures: list[Exception] = []

    def load_pending(
        self,
        issue_number: int,
    ) -> PendingCandidateExperimentExecution | None:
        return self.pending if issue_number == 31 else None

    def load_result(
        self,
        operation: PersistedCandidateExperimentOperation,
    ) -> StoredCandidateExperimentResult | None:
        return self.result

    def persist_result(
        self,
        operation: PersistedCandidateExperimentOperation,
        result: CandidateExperimentResult,
    ) -> StoredCandidateExperimentResult:
        self.persist_calls += 1
        if self.failures:
            raise self.failures.pop(0)
        self.result = StoredCandidateExperimentResult(
            result=result,
            workspace=self.workspace,
        )
        return self.result


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
        self.plans: list[TrustedCandidateExecutionPlan] = []

    def reconcile(
        self,
        plan: TrustedCandidateExecutionPlan,
    ) -> CandidateExperimentResult | None:
        self.reconcile_calls += 1
        self.plans.append(plan)
        return self._reconciled.pop(0)

    def execute(
        self,
        plan: TrustedCandidateExecutionPlan,
    ) -> CandidateExperimentResult | None:
        self.execute_calls += 1
        self.plans.append(plan)
        return self._executed.pop(0)


class RecordingCandidatePlanner:
    def __init__(
        self,
        plan: TrustedCandidateExecutionPlan | None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.plan = plan
        self.error = error
        self.calls: list[tuple[Path, PendingCandidateExperimentExecution]] = []

    def resolve(
        self,
        repository_root: Path,
        pending: PendingCandidateExperimentExecution,
    ) -> TrustedCandidateExecutionPlan:
        self.calls.append((repository_root, pending))
        if self.error is not None:
            raise self.error
        assert self.plan is not None
        return self.plan


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


def _workspace_result(
    phase: WorkspacePhase,
    *,
    recorded: bool,
    next_action: WorkspaceNextActionKind | None = None,
    workspace_pull_request_number: int | None = None,
) -> WorkspaceResult:
    return WorkspaceResult(
        phase=phase,
        workspace_pull_request=None,
        planned_effect_kinds=(),
        recorded=recorded,
        next_action=(
            WorkspaceNextAction(
                kind=next_action,
                issue_number=31,
                workspace_pull_request_number=(
                    workspace_pull_request_number
                ),
                trigger=WorkspaceTrigger.EXPERIMENTS_COMPLETED,
            )
            if next_action is not None
            else None
        ),
    )


def test_candidate_fallback_is_idempotent_across_retries(
    tmp_path: Path,
) -> None:
    persisted = _persisted_candidate_operation()
    pending = _pending_candidate_execution(persisted)
    store = RecordingCandidateStore(pending)
    planner = RecordingCandidatePlanner(_candidate_plan(pending))
    executor = RecordingCandidateExecutor(
        reconciled=[None],
        executed=[_candidate_result(persisted)],
    )
    service = WorkspaceOperationsService(
        candidate_store=store,
        candidate_planner=planner,
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
    assert len(planner.calls) == 2


def test_candidate_fallback_resumes_same_workspace_pull_request(
    tmp_path: Path,
) -> None:
    persisted = _persisted_candidate_operation()
    pending = _pending_candidate_execution(persisted)
    store = RecordingCandidateStore(pending)
    store.workspace = _workspace_result(
        WorkspacePhase.EVALUATING,
        recorded=True,
        next_action=WorkspaceNextActionKind.RUN_CANDIDATE_EXPERIMENTS,
        workspace_pull_request_number=104,
    )
    planner = RecordingCandidatePlanner(_candidate_plan(pending))
    service = WorkspaceOperationsService(
        candidate_store=store,
        candidate_planner=planner,
        candidate_executor=RecordingCandidateExecutor(
            reconciled=[None],
            executed=[_candidate_result(persisted)],
        ),
    )

    result = service.execute(_execute_request(tmp_path))

    assert result.status is WorkspaceOperationsStatus.CANDIDATE_RECORDED
    assert result.workspace_pull_request_number == 104
    assert result.resume is not None
    assert result.resume.workspace_pull_request_number == 104
    assert "@copilot" in result.resume.comment_body
    assert "same workspace pull request" in result.resume.comment_body


@pytest.mark.parametrize(
    ("phase", "next_action"),
    (
        (
            WorkspacePhase.AWAITING_SELECTION,
            WorkspaceNextActionKind.MERGE_WORKSPACE_PULL_REQUEST,
        ),
        (
            WorkspacePhase.COMPLETED,
            WorkspaceNextActionKind.NONE,
        ),
        (
            WorkspacePhase.RETENTION,
            WorkspaceNextActionKind.COMPLETE_RETENTION,
        ),
    ),
)
def test_candidate_fallback_skips_resume_outside_copilot_actions(
    tmp_path: Path,
    phase: WorkspacePhase,
    next_action: WorkspaceNextActionKind,
) -> None:
    persisted = _persisted_candidate_operation()
    pending = _pending_candidate_execution(persisted)
    store = RecordingCandidateStore(pending)
    store.workspace = _workspace_result(
        phase,
        recorded=True,
        next_action=next_action,
        workspace_pull_request_number=104,
    )
    planner = RecordingCandidatePlanner(_candidate_plan(pending))
    service = WorkspaceOperationsService(
        candidate_store=store,
        candidate_planner=planner,
        candidate_executor=RecordingCandidateExecutor(
            reconciled=[None],
            executed=[_candidate_result(persisted)],
        ),
    )

    result = service.execute(_execute_request(tmp_path))

    assert result.status is WorkspaceOperationsStatus.CANDIDATE_RECORDED
    assert result.resume is None


def test_candidate_fallback_recovers_after_persist_ack_loss(
    tmp_path: Path,
) -> None:
    persisted = _persisted_candidate_operation()
    result = _candidate_result(persisted)
    pending = _pending_candidate_execution(persisted)
    store = RecordingCandidateStore(pending)
    store.failures.append(RuntimeError("ack lost"))
    planner = RecordingCandidatePlanner(_candidate_plan(pending))
    executor = RecordingCandidateExecutor(
        reconciled=[None, result],
        executed=[result],
    )
    service = WorkspaceOperationsService(
        candidate_store=store,
        candidate_planner=planner,
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
    assert store.result is not None
    assert store.result.result == result


def test_candidate_fallback_rejects_tampered_result(
    tmp_path: Path,
) -> None:
    persisted = _persisted_candidate_operation()
    pending = _pending_candidate_execution(persisted)
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
        candidate_store=RecordingCandidateStore(pending),
        candidate_planner=RecordingCandidatePlanner(
            _candidate_plan(pending)
        ),
        candidate_executor=executor,
    )

    with pytest.raises(ValueError, match="lineage"):
        service.execute(_execute_request(tmp_path))

    assert executor.execute_calls == 0


def test_candidate_fallback_rejects_forged_untrusted_request_payload(
    tmp_path: Path,
) -> None:
    persisted = _persisted_candidate_operation()
    pending = _pending_candidate_execution(
        persisted,
        request_payload={
            "allowed_paths": ["src/agent.py", "../secrets.env"],
            "validation_commands": [
                ["python", "-m", "pytest", "-q"],
                ["powershell", "-File", "invoke-forged.ps1"],
            ],
            "asset_ids": [
                "dataset-development",
                "forged-customer-asset",
            ],
        },
    )
    store = RecordingCandidateStore(pending)

    class RejectingPlanner:
        def __init__(self) -> None:
            self.calls = 0

        def resolve(
            self,
            repository_root: Path,
            pending_execution: PendingCandidateExperimentExecution,
        ) -> TrustedCandidateExecutionPlan:
            self.calls += 1
            payload = pending_execution.request_payload
            assert payload["asset_ids"] == [
                "dataset-development",
                "forged-customer-asset",
            ]
            raise ValueError(
                "candidate execution request violates trusted config binding"
            )

    planner = RejectingPlanner()
    executor = RecordingCandidateExecutor(reconciled=[None], executed=[None])
    service = WorkspaceOperationsService(
        candidate_store=store,
        candidate_planner=planner,
        candidate_executor=executor,
    )

    with pytest.raises(ValueError, match="trusted config binding"):
        service.execute(_execute_request(tmp_path))

    assert planner.calls == 1
    assert executor.reconcile_calls == 0
    assert executor.execute_calls == 0
    assert store.persist_calls == 0
    assert store.result is None


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
