from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import re
import shlex
from typing import Any

from foundry_opt.adapters.commands import CommandError, SubprocessCommandRunner
from foundry_opt.adapters.github import github_repository_from_remote_url
from foundry_opt.adapters.optimization_deployment import (
    GhWorkflowRunGateway,
    WorkflowRunQuery,
)
from foundry_opt.config import load_config
from foundry_opt.deployment import DeploymentTrigger
from foundry_opt.evaluation import (
    AgentVersionRef,
    DatasetSplit,
    EvaluationSubject,
    MetricDirection,
    evaluate_with_repeat,
)
from foundry_opt.optimization.production import (
    _default_binder_factory,
    _default_credential_provider,
)
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
from foundry_opt.orchestration.workspace_execution_production import (
    _trusted_execution_inputs,
    _workspace_spec_base,
)
from foundry_opt.orchestration.workspace_git_store import GitWorkspaceStore
from foundry_opt.orchestration.workspace_manifest import (
    parse_workspace_candidate_manifest,
)
from foundry_opt.orchestration.workspace_operation_store import (
    GitWorkspaceOperationStore,
)
from foundry_opt.orchestration.workspace_operations_executor import (
    CandidateExperimentExecutionPlanner,
    CandidateExperimentOperationExecutor,
    PendingCandidateExperimentExecution,
    PendingCandidateExperimentStore,
    PendingWorkspaceBaselineExecution,
    PendingWorkspaceBaselineStore,
    PersistedWorkspaceBaselineOperation,
    StoredCandidateExperimentResult,
    StoredWorkspaceBaselineResult,
    TrustedCandidateExecutionPlan,
    TrustedCandidatePackagingContract,
    TrustedWorkspaceBaselinePlan,
    TrustedWorkspaceExecutionContext,
    WorkspaceBaselineCompletionRequest,
    WorkspaceBaselineCompletionService,
    WorkspaceBaselineExecutionPlanner,
    WorkspaceBaselineOperation,
    WorkspaceBaselineOperationExecutor,
    WorkspaceBaselineRequest,
    WorkspaceBaselineResult,
    WorkspaceCandidateSelectionRequest,
    WorkspaceCandidateSelectionService,
    WorkspaceCompletionFinalizer,
    WorkspaceCompletionRequest,
    WorkspaceDeploymentArtifact,
    WorkspaceDeploymentDispatchResult,
    WorkspaceDeploymentDispatchStatus,
    WorkspaceDeploymentRunVerifier,
    WorkspaceDeploymentStateLoader,
    WorkspaceDeploymentTarget,
    WorkspaceDeploymentWorkflowExecutor,
    WorkspaceFinalIssueProjection,
    WorkspaceFinalizationEffect,
    WorkspaceReadyForHumanRequest,
    WorkspaceRetentionEvaluator,
    WorkspaceRetentionOutcome,
    WorkspaceRetentionStatus,
    render_workspace_completion_projection,
    render_workspace_ready_for_human_projection,
)
from foundry_opt.orchestration.workspace_production import (
    ProductionWorkspaceService,
    WorkspaceAdvanceRequest,
)
from foundry_opt.orchestration.workspace_store import (
    WorkspaceBaselineRecord,
    WorkspaceExperimentRecord,
    WorkspaceUpdate,
)
from foundry_opt.preflight.interfaces import CommandRunner


class GitWorkspaceBaselineStore(PendingWorkspaceBaselineStore):
    def __init__(self, repository_root: Path) -> None:
        self._root = repository_root.expanduser().resolve(strict=True)
        self._store = GitWorkspaceStore(self._root)

    def load_pending(
        self,
        issue_number: int,
    ) -> PendingWorkspaceBaselineExecution | None:
        snapshot = self._store.load(issue_number)
        if (
            snapshot is None
            or snapshot.specification is None
            or snapshot.baseline is None
            or snapshot.baseline.status not in {"pending", "completed"}
            or (
                snapshot.baseline.status == "completed"
                and snapshot.phase is not WorkspacePhase.SPECIFICATION
            )
        ):
            return None
        operation = _baseline_operation(
            issue_number=issue_number,
            target_name=snapshot.specification.target,
            published_base_version=_published_base_version(
                self._root,
                snapshot.specification.target,
            ),
            idempotency_key=snapshot.baseline.idempotency_key,
        )
        return PendingWorkspaceBaselineExecution(
            PersistedWorkspaceBaselineOperation(
                operation=operation,
                reference=(
                    f"foundry-opt/state/issue-{issue_number}/baseline"
                ),
                sha256=operation.sha256,
            )
        )

    def load_result(
        self,
        operation: PersistedWorkspaceBaselineOperation,
    ) -> StoredWorkspaceBaselineResult | None:
        snapshot = self._store.load(operation.operation.issue_number)
        if (
            snapshot is None
            or snapshot.specification is None
            or snapshot.baseline is None
            or snapshot.baseline.status != "completed"
        ):
            return None
        result = _baseline_result(
            record=snapshot.baseline,
            specification=snapshot.specification,
            operation=operation,
        )
        return StoredWorkspaceBaselineResult(result=result)

    def persist_result(
        self,
        operation: PersistedWorkspaceBaselineOperation,
        result: WorkspaceBaselineResult,
    ) -> StoredWorkspaceBaselineResult:
        stored = self.load_result(operation)
        if stored is None or stored.result != result:
            raise ValueError("trusted workspace baseline result is unavailable")
        return stored


class ProductionWorkspaceBaselinePlanner(WorkspaceBaselineExecutionPlanner):
    def resolve(
        self,
        repository_root: Path,
        pending: PendingWorkspaceBaselineExecution,
    ) -> TrustedWorkspaceBaselinePlan:
        inputs = _trusted_execution_inputs(
            commands=SubprocessCommandRunner(),
            root=repository_root.expanduser().resolve(strict=True),
            config_path=Path(".github/foundry-optimizer.yaml"),
            issue_number=pending.operation.operation.issue_number,
            base_commit=_workspace_spec_base(
                repository_root,
                pending.operation.operation.issue_number,
            ),
        )
        operation = pending.operation
        request = WorkspaceBaselineRequest(
            issue_number=operation.operation.issue_number,
            target_name=inputs.request.target,
            published_base_version=inputs.target.base_agent_version,
            development_suite="development",
            idempotency_key=operation.operation.idempotency_key,
        )
        expected = WorkspaceBaselineOperation.from_request(request)
        if expected != operation.operation:
            raise ValueError("workspace baseline lineage changed")
        return TrustedWorkspaceBaselinePlan(
            operation=operation,
            request=request,
            base_commit=inputs.spec.base_commit,
            target_name=inputs.request.target,
            base_agent_version=int(inputs.target.base_agent_version),
            published_base_version=inputs.target.base_agent_version,
            development_suite="development",
            assets=inputs.assets,
            evaluation_policy=__import__(
                "foundry_opt.adapters.optimization_evaluation",
                fromlist=["build_evaluation_policy"],
            ).build_evaluation_policy(inputs.spec),
        )


class ProductionWorkspaceBaselineExecutor(
    WorkspaceBaselineOperationExecutor
):
    def __init__(
        self,
        *,
        repository_root: Path,
        workspace_service: ProductionWorkspaceService,
    ) -> None:
        self._root = repository_root.expanduser().resolve(strict=True)
        self._workspace_service = workspace_service
        self._store = GitWorkspaceStore(self._root)

    def reconcile(
        self,
        plan: TrustedWorkspaceBaselinePlan,
    ) -> WorkspaceBaselineResult | None:
        snapshot = self._store.load(plan.request.issue_number)
        if (
            snapshot is None
            or snapshot.specification is None
            or snapshot.baseline is None
            or snapshot.baseline.status != "completed"
        ):
            return None
        return _baseline_result(
            record=snapshot.baseline,
            specification=snapshot.specification,
            operation=plan.operation,
        )

    def execute(
        self,
        plan: TrustedWorkspaceBaselinePlan,
    ) -> WorkspaceBaselineResult | None:
        result = self._workspace_service.execute_baseline(
            repository_root=self._root,
            issue_number=plan.request.issue_number,
        )
        if result.status != "completed":
            return None
        return self.reconcile(plan)


class ProductionWorkspaceBaselineCompletion(
    WorkspaceBaselineCompletionService
):
    def __init__(self, workspace_service: ProductionWorkspaceService) -> None:
        self._workspace_service = workspace_service

    def complete(
        self,
        request: WorkspaceBaselineCompletionRequest,
    ):
        store = GitWorkspaceStore(request.repository_root)
        snapshot = store.load(request.issue_number)
        if (
            snapshot is not None
            and snapshot.phase is WorkspacePhase.SPECIFICATION
            and snapshot.baseline is not None
            and snapshot.baseline.status == "completed"
        ):
            snapshot = store.commit(
                expected_revision=snapshot.revision,
                update=WorkspaceUpdate(
                    issue_number=snapshot.issue_number,
                    phase=WorkspacePhase.EVALUATING,
                    workspace_pull_request_number=(
                        snapshot.workspace_pull_request_number
                    ),
                    semantic_event=(
                        "baseline_completion_acknowledged"
                    ),
                    candidates=snapshot.candidates,
                    selected_patch=snapshot.selected_patch,
                    external_operation_ids=(
                        snapshot.external_operation_ids
                    ),
                    experiments=snapshot.experiments,
                    lineage=snapshot.lineage,
                    specification=snapshot.specification,
                    baseline=snapshot.baseline,
                ),
            )
        return self._workspace_service.advance(
            WorkspaceAdvanceRequest(
                repository_root=request.repository_root,
                issue_number=request.issue_number,
                trigger=WorkspaceTrigger.CONTINUE,
            )
        )


class GitWorkspaceCandidateStore(PendingCandidateExperimentStore):
    def __init__(self, repository_root: Path) -> None:
        self._root = repository_root.expanduser().resolve(strict=True)
        self._store = GitWorkspaceStore(self._root)
        self._operations = GitWorkspaceOperationStore(self._root)

    def load_pending(
        self,
        issue_number: int,
    ) -> PendingCandidateExperimentExecution | None:
        snapshot = self._store.load(issue_number)
        if snapshot is None:
            return None
        pending = next(
            (
                item
                for item in snapshot.experiments
                if item.status == "pending"
            ),
            None,
        )
        if (
            pending is None
            and snapshot.phase
            in {
                WorkspacePhase.EVALUATING,
                WorkspacePhase.AWAITING_SELECTION,
            }
            and snapshot.specification is not None
            and snapshot.experiments
            and all(
                item.status == "completed"
                for item in snapshot.experiments
            )
        ):
            config = load_config(
                self._root / ".github" / "foundry-optimizer.yaml"
            )
            target = config.targets.get(snapshot.specification.target)
            if target is None:
                raise ValueError(
                    "workspace candidate target is not configured"
                )
            candidate_limit = (
                target.campaign_overrides.max_changed_candidates
                if (
                    target.campaign_overrides is not None
                    and target.campaign_overrides.max_changed_candidates
                    is not None
                )
                else config.campaign.max_changed_candidates
            )
            if len(snapshot.experiments) == candidate_limit:
                pending = snapshot.experiments[-1]
        if pending is None:
            return None
        payload = self._operations.load_candidate_manifest(
            issue_number,
            pending.candidate_id,
        )
        if payload is None:
            raise ValueError("workspace candidate manifest is unavailable")
        request = _candidate_request(issue_number, pending)
        operation = CandidateExperimentOperation.from_request(request)
        return PendingCandidateExperimentExecution(
            operation=PersistedCandidateExperimentOperation(
                operation=operation,
                reference=(
                    "foundry-opt/operations/"
                    f"issue-{issue_number}/candidates/"
                    f"{pending.candidate_id}.json"
                ),
                sha256=operation.sha256,
            ),
            request_payload=payload,
        )

    def load_result(
        self,
        operation: PersistedCandidateExperimentOperation,
    ) -> StoredCandidateExperimentResult | None:
        record = self._record(operation)
        if record is None or record.status != "completed":
            return None
        return StoredCandidateExperimentResult(
            result=_candidate_result(record)
        )

    def persist_result(
        self,
        operation: PersistedCandidateExperimentOperation,
        result: CandidateExperimentResult,
    ) -> StoredCandidateExperimentResult:
        stored = self.load_result(operation)
        if stored is None or stored.result != result:
            raise ValueError("trusted candidate experiment result is unavailable")
        return stored

    def _record(
        self,
        operation: PersistedCandidateExperimentOperation,
    ) -> WorkspaceExperimentRecord | None:
        snapshot = self._store.load(operation.operation.issue_number)
        if snapshot is None:
            return None
        return next(
            (
                item
                for item in snapshot.experiments
                if item.candidate_id == operation.operation.candidate_id
                and item.operation_sha256 == operation.sha256
            ),
            None,
        )


class ProductionWorkspaceCandidatePlanner(
    CandidateExperimentExecutionPlanner
):
    def resolve(
        self,
        repository_root: Path,
        pending: PendingCandidateExperimentExecution,
    ) -> TrustedCandidateExecutionPlan:
        manifest = parse_workspace_candidate_manifest(
            pending.request_payload,
        )
        inputs = _trusted_execution_inputs(
            commands=SubprocessCommandRunner(),
            root=repository_root.expanduser().resolve(strict=True),
            config_path=Path(".github/foundry-optimizer.yaml"),
            issue_number=pending.operation.operation.issue_number,
            base_commit=_workspace_spec_base(
                repository_root,
                pending.operation.operation.issue_number,
            ),
        )
        target = inputs.target
        if (
            manifest.issue_number != pending.operation.operation.issue_number
            or manifest.target != inputs.request.target
            or manifest.base_commit != inputs.spec.base_commit
            or manifest.candidate.candidate_id
            != pending.operation.operation.candidate_id
        ):
            raise ValueError("workspace candidate lineage changed")
        candidate_limit = (
            target.campaign_overrides.max_changed_candidates
            if (
                target.campaign_overrides is not None
                and target.campaign_overrides.max_changed_candidates
                is not None
            )
            else inputs.config.campaign.max_changed_candidates
        )
        operation = pending.operation.operation
        return TrustedCandidateExecutionPlan(
            operation=pending.operation,
            request=CandidateExperimentRequest(
                issue_number=operation.issue_number,
                candidate_id=operation.candidate_id,
                patch_sha256=operation.patch_sha256,
                bundle_sha256=operation.bundle_sha256,
                evidence_sha256=operation.evidence_sha256,
                idempotency_key=operation.idempotency_key,
            ),
            base_commit=inputs.spec.base_commit,
            target_name=inputs.request.target,
            base_agent_version=int(target.base_agent_version),
            allowed_paths=tuple(str(path) for path in target.edit_paths),
            allowed_mutations=frozenset(
                getattr(item, "value", str(item))
                for item in target.allowed_mutations
            ),
            validation_commands=tuple(
                tuple(shlex.split(command, posix=True))
                for command in target.validation_commands
            ),
            packaging=TrustedCandidatePackagingContract(
                include=tuple(
                    str(pattern) for pattern in target.package.include
                ),
                exclude=tuple(
                    str(pattern) for pattern in target.package.exclude
                ),
                dependency_resolution=(
                    target.runtime.dependency_resolution
                    or "remote_build"
                ),
                evidence_paths=(
                    str(inputs.config.campaign.evidence_path),
                ),
            ),
            assets=inputs.assets,
            evaluation_policy=__import__(
                "foundry_opt.adapters.optimization_evaluation",
                fromlist=["build_evaluation_policy"],
            ).build_evaluation_policy(inputs.spec),
            candidate_limit=candidate_limit,
        )


class ProductionWorkspaceCandidateExecutor(
    CandidateExperimentOperationExecutor
):
    def __init__(
        self,
        *,
        repository_root: Path,
        workspace_service: ProductionWorkspaceService,
    ) -> None:
        self._root = repository_root.expanduser().resolve(strict=True)
        self._workspace_service = workspace_service
        self._operations = GitWorkspaceOperationStore(self._root)
        self._store = GitWorkspaceStore(self._root)

    def reconcile(
        self,
        plan: TrustedCandidateExecutionPlan,
    ) -> CandidateExperimentResult | None:
        snapshot = self._store.load(plan.request.issue_number)
        if snapshot is None:
            return None
        record = next(
            (
                item
                for item in snapshot.experiments
                if item.candidate_id == plan.request.candidate_id
                and item.operation_sha256 == plan.operation.sha256
                and item.status == "completed"
            ),
            None,
        )
        return _candidate_result(record) if record is not None else None

    def execute(
        self,
        plan: TrustedCandidateExecutionPlan,
    ) -> CandidateExperimentResult | None:
        payload = self._operations.load_candidate_manifest(
            plan.request.issue_number,
            plan.request.candidate_id,
        )
        if payload is None:
            raise ValueError("workspace candidate manifest is unavailable")
        result = self._workspace_service.execute_experiment(
            payload,
            repository_root=self._root,
        )
        if result.status != "completed":
            return None
        return self.reconcile(plan)


class ProductionWorkspaceCandidateSelection(
    WorkspaceCandidateSelectionService
):
    def __init__(
        self,
        *,
        repository_root: Path,
        workspace_service: ProductionWorkspaceService,
    ) -> None:
        self._root = repository_root.expanduser().resolve(strict=True)
        self._workspace_service = workspace_service
        self._store = GitWorkspaceStore(self._root)
        self._operations = GitWorkspaceOperationStore(self._root)

    def complete(
        self,
        request: WorkspaceCandidateSelectionRequest,
    ):
        snapshot = self._store.load(request.issue_number)
        if snapshot is None:
            raise ValueError("workspace state is unavailable")
        experiments = snapshot.experiments
        pending = any(item.status == "pending" for item in experiments)
        if pending or len(experiments) < request.plan.candidate_limit:
            return self._workspace_service.advance(
                WorkspaceAdvanceRequest(
                    repository_root=request.repository_root,
                    issue_number=request.issue_number,
                    trigger=WorkspaceTrigger.CONTINUE,
                )
            )
        if len(experiments) != request.plan.candidate_limit:
            raise ValueError("workspace candidate set changed")
        manifests = self._operations.load_candidate_manifests(
            request.issue_number
        )
        if set(manifests) != {item.candidate_id for item in experiments}:
            raise ValueError("workspace candidate manifests are incomplete")
        payload = {
            "base_commit": request.plan.base_commit,
            "candidates": [
                dict(manifests[item.candidate_id]["candidate"])
                for item in experiments
            ],
            "issue_number": request.issue_number,
            "schema_version": 4,
            "target": request.plan.target_name,
        }
        try:
            return self._workspace_service.complete_experiments(
                payload,
                repository_root=request.repository_root,
            )
        except ValueError as error:
            if str(error) != "workspace policy found no eligible candidate":
                raise
            blocked = self._store.commit(
                expected_revision=snapshot.revision,
                update=WorkspaceUpdate(
                    issue_number=snapshot.issue_number,
                    phase=WorkspacePhase.BLOCKED,
                    workspace_pull_request_number=(
                        snapshot.workspace_pull_request_number
                    ),
                    semantic_event="candidate_selection_no_eligible_candidate",
                    candidates=snapshot.candidates,
                    selected_patch=snapshot.selected_patch,
                    external_operation_ids=snapshot.external_operation_ids,
                    experiments=snapshot.experiments,
                    lineage=snapshot.lineage,
                    specification=snapshot.specification,
                    baseline=snapshot.baseline,
                ),
            )
            return WorkspaceResult(
                phase=WorkspacePhase.BLOCKED,
                workspace_pull_request=None,
                planned_effect_kinds=(),
                recorded=True,
                next_action=WorkspaceNextAction(
                    kind=WorkspaceNextActionKind.NONE,
                    issue_number=blocked.issue_number,
                    workspace_pull_request_number=(
                        blocked.workspace_pull_request_number
                    ),
                    trigger=None,
                ),
            )


class GitWorkspaceDeploymentLoader(WorkspaceDeploymentStateLoader):
    def __init__(
        self,
        *,
        repository_root: Path,
        commands: CommandRunner | None = None,
    ) -> None:
        self._root = repository_root.expanduser().resolve(strict=True)
        self._commands = commands or SubprocessCommandRunner()
        self._store = GitWorkspaceStore(self._root)

    def load(
        self,
        issue_number: int,
    ) -> WorkspaceDeploymentTarget | None:
        snapshot = self._store.load(issue_number)
        if (
            snapshot is None
            or snapshot.phase
            not in {
                WorkspacePhase.DEPLOYMENT,
                WorkspacePhase.RETENTION,
                WorkspacePhase.COMPLETED,
            }
            or snapshot.lineage is None
            or snapshot.specification is None
            or snapshot.workspace_pull_request_number is None
        ):
            return None
        repository = _repository_name(self._commands, self._root)
        repository_id = _repository_id(
            self._commands,
            self._root,
            repository,
        )
        pull = _pull_request(
            self._commands,
            self._root,
            repository,
            snapshot.workspace_pull_request_number,
        )
        merge_commit = pull.get("merge_commit_sha")
        if (
            pull.get("number") != snapshot.workspace_pull_request_number
            or pull.get("state") != "closed"
            or not isinstance(pull.get("merged_at"), str)
            or not isinstance(merge_commit, str)
            or re.fullmatch(r"[0-9a-f]{40}", merge_commit) is None
        ):
            raise ValueError("workspace merged pull request is unavailable")
        tree_sha = _commit_tree(self._commands, self._root, merge_commit)
        config = load_config(self._root / ".github" / "foundry-optimizer.yaml")
        target = config.targets.get(snapshot.specification.target)
        if target is None:
            raise ValueError("workspace deployment target is not configured")
        workflow = config.environments[target.environment].deployment_workflow
        cleanup_refs = (
            f"refs/heads/foundry-opt/workspace/issue-{issue_number}",
            GitWorkspaceOperationStore.ref_name(issue_number),
        )
        cleanup_drafts = tuple(
            record.draft_id
            for record in _cleanup_records(snapshot, snapshot.lineage.selected_candidate_id)
            if record.draft_id
        )
        cleanup_artifacts = tuple(
            f"candidates/{candidate_id}.json"
            for candidate_id in sorted(
                GitWorkspaceOperationStore(self._root).load_candidate_manifests(
                    issue_number
                )
            )
        )
        return WorkspaceDeploymentTarget(
            issue_number=issue_number,
            phase=snapshot.phase,
            repository=repository,
            repository_id=repository_id,
            workspace_pull_request_number=(
                snapshot.workspace_pull_request_number
            ),
            candidate_id=snapshot.lineage.selected_candidate_id,
            patch_sha256=snapshot.lineage.patch_sha256,
            bundle_sha256=snapshot.lineage.bundle_sha256,
            evidence_sha256=snapshot.lineage.evidence_sha256,
            spec_sha256=snapshot.lineage.spec_sha256,
            merge_commit=merge_commit,
            tree_sha=tree_sha,
            workflow_name=_workflow_name(self._root / workflow.path),
            workflow_path=workflow.path,
            workflow_ref=f"refs/heads/{_default_branch(self._commands, self._root)}",
            workflow_trigger=DeploymentTrigger(workflow.trigger.value),
            cleanup_refs=cleanup_refs,
            cleanup_drafts=cleanup_drafts,
            cleanup_artifacts=cleanup_artifacts,
        )


class ProductionWorkspaceDeploymentExecutor(
    WorkspaceDeploymentWorkflowExecutor
):
    def __init__(
        self,
        *,
        repository_root: Path,
        commands: CommandRunner | None = None,
    ) -> None:
        self._root = repository_root.expanduser().resolve(strict=True)
        self._commands = commands or SubprocessCommandRunner()
        self._gateway = GhWorkflowRunGateway(self._commands)

    def execute(
        self,
        repository_root: Path,
        target: WorkspaceDeploymentTarget,
        context: TrustedWorkspaceExecutionContext,
    ) -> WorkspaceDeploymentDispatchResult:
        if context.event_name == "workflow_run":
            return WorkspaceDeploymentDispatchResult(
                status=WorkspaceDeploymentDispatchStatus.PENDING
            )
        if target.workflow_trigger is DeploymentTrigger.MANUAL:
            self._gateway.dispatch(
                self._root,
                workflow_path=target.workflow_path,
                input_name="selected_commit",
                commit=target.merge_commit,
                correlation_input_name="foundry_opt_effect_id",
                correlation_id=target.lineage_sha256,
            )
            return WorkspaceDeploymentDispatchResult(
                status=WorkspaceDeploymentDispatchStatus.DISPATCHED,
            )
        run = self._gateway.find_run(
            self._root,
            query=WorkflowRunQuery(
                workflow_path=target.workflow_path,
                events=("push", "workflow_run"),
                head_sha=target.merge_commit,
                trigger=target.workflow_trigger,
            ),
        )
        if run is None:
            return WorkspaceDeploymentDispatchResult(
                status=WorkspaceDeploymentDispatchStatus.PENDING
            )
        return WorkspaceDeploymentDispatchResult(
            status=WorkspaceDeploymentDispatchStatus.OBSERVED,
            run_id=_run_id(run.url),
            run_url=run.url,
        )


class ProductionWorkspaceDeploymentVerifier(WorkspaceDeploymentRunVerifier):
    def __init__(
        self,
        *,
        repository_root: Path,
        commands: CommandRunner | None = None,
    ) -> None:
        self._root = repository_root.expanduser().resolve(strict=True)
        self._commands = commands or SubprocessCommandRunner()

    def verify(
        self,
        repository_root: Path,
        target: WorkspaceDeploymentTarget,
        artifact: WorkspaceDeploymentArtifact,
    ) -> None:
        document = _workflow_run(
            self._commands,
            self._root,
            target.repository,
            artifact.run_id,
        )
        repository = document.get("repository")
        if (
            not isinstance(repository, Mapping)
            or repository.get("full_name") != target.repository
            or repository.get("id") != target.repository_id
            or document.get("name") != target.workflow_name
            or not isinstance(document.get("path"), str)
            or not str(document["path"]).startswith(
                target.workflow_path.as_posix()
            )
            or document.get("conclusion") != "success"
            or document.get("html_url") != artifact.run_url
        ):
            raise ValueError("workspace deployment workflow changed")
        if target.workflow_trigger is DeploymentTrigger.MANUAL:
            if document.get("display_title") != target.lineage_sha256:
                raise ValueError("workspace deployment workflow changed")
        elif document.get("head_sha") != target.merge_commit:
            raise ValueError("workspace deployment workflow changed")


class ProductionWorkspaceRetentionEvaluator(WorkspaceRetentionEvaluator):
    def __init__(
        self,
        *,
        repository_root: Path,
    ) -> None:
        self._root = repository_root.expanduser().resolve(strict=True)
        self._store = GitWorkspaceStore(self._root)

    def evaluate(
        self,
        repository_root: Path,
        target: WorkspaceDeploymentTarget,
        artifact: WorkspaceDeploymentArtifact,
    ) -> WorkspaceRetentionOutcome:
        inputs = _trusted_execution_inputs(
            commands=SubprocessCommandRunner(),
            root=self._root,
            config_path=Path(".github/foundry-optimizer.yaml"),
            issue_number=target.issue_number,
            base_commit=_workspace_spec_base(
                self._root,
                target.issue_number,
            ),
        )
        snapshot = self._store.load(target.issue_number)
        if snapshot is None or snapshot.baseline is None:
            raise ValueError("workspace baseline is unavailable")
        selected = next(
            (
                item
                for item in snapshot.experiments
                if item.candidate_id == target.candidate_id
                and item.status == "completed"
            ),
            None,
        )
        if selected is None:
            raise ValueError("workspace selected candidate is unavailable")
        endpoint = str(
            inputs.config.environments[inputs.target.environment].project_endpoint
        )
        evaluate = _default_binder_factory(
            _default_credential_provider(
                __import__(
                    "foundry_opt.adapters.environment",
                    fromlist=["OsEnvironmentReader"],
                ).OsEnvironmentReader()
            ),
            inputs.config,
        )(endpoint)(inputs.spec, inputs.assets)
        subject = EvaluationSubject(
            f"published-{target.candidate_id}",
            AgentVersionRef(
                inputs.spec.target,
                str(artifact.deployment_version),
                str(artifact.deployment_version),
            ),
            target.lineage_sha256,
        )
        result = evaluate_with_repeat(
            subject,
            DatasetSplit.VALIDATION,
            __import__(
                "foundry_opt.adapters.optimization_evaluation",
                fromlist=["build_evaluation_policy"],
            ).build_evaluation_policy(inputs.spec),
            evaluate,
        )
        if not result.complete:
            return WorkspaceRetentionOutcome(
                status=WorkspaceRetentionStatus.PENDING
            )
        deployed_metrics = {
            name: aggregate.median
            for name, aggregate in result.metrics.items()
            if aggregate.median is not None
        }
        baseline_metrics = dict(snapshot.baseline.metrics)
        selected_metrics = dict(selected.metrics)
        failure = _retention_failure(
            policy=__import__(
                "foundry_opt.adapters.optimization_evaluation",
                fromlist=["build_evaluation_policy"],
            ).build_evaluation_policy(inputs.spec),
            baseline_metrics=baseline_metrics,
            selected_metrics=selected_metrics,
            deployed_metrics=deployed_metrics,
            guardrail_outcomes={
                metric.name: result.metrics[metric.name].outcome.value
                for metric in __import__(
                    "foundry_opt.adapters.optimization_evaluation",
                    fromlist=["build_evaluation_policy"],
                ).build_evaluation_policy(inputs.spec).metrics
                if metric.hard_guardrail and metric.name in result.metrics
            },
        )
        if failure is not None:
            return WorkspaceRetentionOutcome(
                status=WorkspaceRetentionStatus.REGRESSED,
                operation_id=(
                    f"retention-{target.issue_number}-{artifact.deployment_version}"
                ),
                baseline_metrics=baseline_metrics,
                selected_metrics=selected_metrics,
                deployed_metrics=deployed_metrics,
                reason=failure,
            )
        return WorkspaceRetentionOutcome(
            status=WorkspaceRetentionStatus.RETAINED_IMPROVEMENT,
            operation_id=(
                f"retention-{target.issue_number}-{artifact.deployment_version}"
            ),
            baseline_metrics=baseline_metrics,
            selected_metrics=selected_metrics,
            deployed_metrics=deployed_metrics,
        )


class ProductionWorkspaceCompletionFinalizer(WorkspaceCompletionFinalizer):
    def __init__(
        self,
        *,
        repository_root: Path,
        commands: CommandRunner | None = None,
    ) -> None:
        self._root = repository_root.expanduser().resolve(strict=True)
        self._commands = commands or SubprocessCommandRunner()
        self._store = GitWorkspaceStore(self._root)
        self._operations = GitWorkspaceOperationStore(self._root)

    def complete(
        self,
        request: WorkspaceCompletionRequest,
    ) -> WorkspaceFinalizationEffect:
        projection = render_workspace_completion_projection(request)
        self._publish_projection(
            request.target.repository,
            request.target.issue_number,
            projection,
        )
        self._store.finalize(request.target.issue_number)
        self._operations.delete_ref(request.target.issue_number)
        _delete_remote_ref(
            self._commands,
            self._root,
            request.target.repository,
            f"refs/heads/foundry-opt/workspace/issue-{request.target.issue_number}",
        )
        _close_issue(
            self._commands,
            self._root,
            request.target.repository,
            request.target.issue_number,
        )
        return WorkspaceFinalizationEffect(
            finalized=True,
            closed_issue=True,
            projection=projection,
            cleaned_refs=request.target.cleanup_refs,
            cleaned_drafts=request.target.cleanup_drafts,
            cleaned_artifacts=request.target.cleanup_artifacts,
        )

    def ready_for_human(
        self,
        request: WorkspaceReadyForHumanRequest,
    ) -> WorkspaceFinalizationEffect:
        projection = render_workspace_ready_for_human_projection(request)
        self._publish_projection(
            request.target.repository,
            request.target.issue_number,
            projection,
        )
        self._store.finalize(request.target.issue_number)
        self._operations.delete_ref(request.target.issue_number)
        _delete_remote_ref(
            self._commands,
            self._root,
            request.target.repository,
            f"refs/heads/foundry-opt/workspace/issue-{request.target.issue_number}",
        )
        return WorkspaceFinalizationEffect(
            finalized=True,
            closed_issue=False,
            projection=projection,
            cleaned_refs=request.target.cleanup_refs,
            cleaned_drafts=request.target.cleanup_drafts,
            cleaned_artifacts=request.target.cleanup_artifacts,
        )

    def _publish_projection(
        self,
        repository: str,
        issue_number: int,
        projection: WorkspaceFinalIssueProjection,
    ) -> None:
        comments = _issue_comments(
            self._commands,
            self._root,
            repository,
            issue_number,
        )
        existing = next(
            (
                item
                for item in comments
                if isinstance(item.get("body"), str)
                and str(item["body"]).startswith(projection.marker)
            ),
            None,
        )
        payload = {"body": projection.body}
        if existing is None:
            _gh_api(
                self._commands,
                self._root,
                (
                    "gh",
                    "api",
                    "--method",
                    "POST",
                    f"repos/{repository}/issues/{issue_number}/comments",
                    "--input",
                    "-",
                ),
                input_document=payload,
            )
            return
        if existing.get("body") == projection.body:
            return
        _gh_api(
            self._commands,
            self._root,
            (
                "gh",
                "api",
                "--method",
                "PATCH",
                f"repos/{repository}/issues/comments/{existing['id']}",
                "--input",
                "-",
            ),
            input_document=payload,
        )


def build_production_workspace_operations_bindings(
    repository_root: Path,
    *,
    workspace_service: ProductionWorkspaceService,
) -> dict[str, object]:
    root = repository_root.expanduser().resolve(strict=True)
    commands = SubprocessCommandRunner()
    return {
        "baseline_store": GitWorkspaceBaselineStore(root),
        "baseline_planner": ProductionWorkspaceBaselinePlanner(),
        "baseline_executor": ProductionWorkspaceBaselineExecutor(
            repository_root=root,
            workspace_service=workspace_service,
        ),
        "baseline_completion": ProductionWorkspaceBaselineCompletion(
            workspace_service,
        ),
        "candidate_store": GitWorkspaceCandidateStore(root),
        "candidate_planner": ProductionWorkspaceCandidatePlanner(),
        "candidate_executor": ProductionWorkspaceCandidateExecutor(
            repository_root=root,
            workspace_service=workspace_service,
        ),
        "candidate_selection": ProductionWorkspaceCandidateSelection(
            repository_root=root,
            workspace_service=workspace_service,
        ),
        "deployment_loader": GitWorkspaceDeploymentLoader(
            repository_root=root,
            commands=commands,
        ),
        "deployment_executor": ProductionWorkspaceDeploymentExecutor(
            repository_root=root,
            commands=commands,
        ),
        "deployment_verifier": ProductionWorkspaceDeploymentVerifier(
            repository_root=root,
            commands=commands,
        ),
        "retention_evaluator": ProductionWorkspaceRetentionEvaluator(
            repository_root=root,
        ),
        "finalizer": ProductionWorkspaceCompletionFinalizer(
            repository_root=root,
            commands=commands,
        ),
        "workspace_service": workspace_service,
    }


def _baseline_operation(
    *,
    issue_number: int,
    target_name: str,
    published_base_version: str,
    idempotency_key: str,
) -> WorkspaceBaselineOperation:
    return WorkspaceBaselineOperation(
        issue_number=issue_number,
        target_name=target_name,
        published_base_version=published_base_version,
        development_suite="development",
        idempotency_key=idempotency_key,
    )


def _baseline_result(
    *,
    record: WorkspaceBaselineRecord,
    specification,
    operation: PersistedWorkspaceBaselineOperation,
) -> WorkspaceBaselineResult:
    return WorkspaceBaselineResult(
        target_name=specification.target,
        executor=record.executor or "",
        metrics=record.metrics,
        evaluation_id=record.evaluation_id or "",
        run_id=record.run_id or "",
        base_commit=specification.base_commit,
        published_base_version=operation.operation.published_base_version,
        development_suite=operation.operation.development_suite,
        operation_sha256=operation.sha256,
        idempotency_key=operation.operation.idempotency_key,
    )


def _candidate_request(
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


def _candidate_result(
    record: WorkspaceExperimentRecord | None,
) -> CandidateExperimentResult | None:
    if record is None:
        return None
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


def _published_base_version(root: Path, target_name: str) -> str:
    config = load_config(root / ".github" / "foundry-optimizer.yaml")
    target = config.targets.get(target_name)
    if target is None:
        raise ValueError("workspace target is not configured")
    return target.base_agent_version


def _cleanup_records(snapshot, selected_candidate_id: str):
    baseline = []
    if snapshot.baseline is not None:
        baseline = [snapshot.baseline]
    return (
        *baseline,
        *(
            item
            for item in snapshot.experiments
            if item.candidate_id != selected_candidate_id
        ),
    )


def _retention_failure(
    *,
    policy,
    baseline_metrics: Mapping[str, float],
    selected_metrics: Mapping[str, float],
    deployed_metrics: Mapping[str, float],
    guardrail_outcomes: Mapping[str, str],
) -> str | None:
    for metric in policy.metrics:
        if metric.hard_guardrail and guardrail_outcomes.get(metric.name) != "pass":
            return f"guardrail_{metric.name}"
        baseline = baseline_metrics.get(metric.name)
        deployed = deployed_metrics.get(metric.name)
        selected = selected_metrics.get(metric.name)
        if baseline is None or deployed is None or selected is None:
            return f"metric_{metric.name}_missing"
        if _materially_worse(deployed, selected, metric):
            return f"selected_drift_{metric.name}"
    if not any(
        _materially_better(
            deployed_metrics[metric.name],
            baseline_metrics[metric.name],
            metric,
        )
        for metric in policy.metrics
        if metric.name in deployed_metrics and metric.name in baseline_metrics
    ):
        return "no_material_improvement"
    return None


def _materially_better(current: float, baseline: float, metric) -> bool:
    delta = (
        current - baseline
        if metric.direction is MetricDirection.MAXIMIZE
        else baseline - current
    )
    return delta >= metric.materiality


def _materially_worse(current: float, reference: float, metric) -> bool:
    delta = (
        reference - current
        if metric.direction is MetricDirection.MAXIMIZE
        else current - reference
    )
    return delta > metric.materiality


def _repository_name(commands: CommandRunner, root: Path) -> str:
    remote = commands.run(
        ("git", "remote", "get-url", "origin"),
        cwd=root,
    ).stdout.strip()
    repository = github_repository_from_remote_url(remote)
    if repository is None:
        raise ValueError("workspace repository origin is invalid")
    return repository


def _repository_id(
    commands: CommandRunner,
    root: Path,
    repository: str,
) -> int:
    document = _gh_api(
        commands,
        root,
        ("gh", "api", f"repos/{repository}"),
    )
    value = document.get("id")
    if type(value) is not int or value < 1:
        raise ValueError("workspace repository ID is invalid")
    return value


def _default_branch(commands: CommandRunner, root: Path) -> str:
    document = _gh_api(
        commands,
        root,
        ("gh", "api", f"repos/{_repository_name(commands, root)}"),
    )
    value = document.get("default_branch")
    if not isinstance(value, str) or not value:
        raise ValueError("workspace default branch is invalid")
    return value


def _pull_request(
    commands: CommandRunner,
    root: Path,
    repository: str,
    number: int,
) -> Mapping[str, Any]:
    return _gh_api(
        commands,
        root,
        ("gh", "api", f"repos/{repository}/pulls/{number}"),
    )


def _workflow_run(
    commands: CommandRunner,
    root: Path,
    repository: str,
    run_id: int,
) -> Mapping[str, Any]:
    return _gh_api(
        commands,
        root,
        ("gh", "api", f"repos/{repository}/actions/runs/{run_id}"),
    )


def _issue_comments(
    commands: CommandRunner,
    root: Path,
    repository: str,
    issue_number: int,
) -> list[dict[str, Any]]:
    value = _gh_api(
        commands,
        root,
        (
            "gh",
            "api",
            "--paginate",
            "--slurp",
            f"repos/{repository}/issues/{issue_number}/comments?per_page=100",
        ),
    )
    if not isinstance(value, list):
        raise ValueError("workspace issue comments are invalid")
    return [
        item
        for page in value
        if isinstance(page, list)
        for item in page
        if isinstance(item, dict)
    ]


def _gh_api(
    commands: CommandRunner,
    root: Path,
    arguments: tuple[str, ...],
    *,
    input_document: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        raw = commands.run(
            arguments,
            cwd=root,
            input_text=(
                json.dumps(
                    input_document,
                    ensure_ascii=True,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                if input_document is not None
                else None
            ),
        ).stdout
    except CommandError as error:
        raise RuntimeError("trusted GitHub operation failed") from error
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("trusted GitHub response is invalid")
    return value


def _commit_tree(
    commands: CommandRunner,
    root: Path,
    commit: str,
) -> str:
    commands.run(
        ("git", "fetch", "--no-tags", "origin", commit),
        cwd=root,
    )
    value = commands.run(
        ("git", "rev-parse", "--verify", f"{commit}^{{tree}}"),
        cwd=root,
    ).stdout.strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ValueError("workspace merge tree is invalid")
    return value


def _workflow_name(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.fullmatch(r"\s*name\s*:\s*(.+?)\s*", line)
            if match is not None:
                return match.group(1).strip().strip("\"'")
    except OSError:
        pass
    return path.stem


def _run_id(url: str) -> int:
    match = re.search(r"/actions/runs/([1-9][0-9]*)", url)
    if match is None:
        raise ValueError("workflow run URL is invalid")
    return int(match.group(1))


def _delete_remote_ref(
    commands: CommandRunner,
    root: Path,
    repository: str,
    ref: str,
) -> None:
    try:
        commands.run(
            ("git", "push", "origin", f":{ref}"),
            cwd=root,
        )
    except CommandError:
        document = _gh_api(
            commands,
            root,
            ("gh", "api", f"repos/{repository}/git/ref/{ref.removeprefix('refs/')}"),
        )
        if document:
            raise


def _close_issue(
    commands: CommandRunner,
    root: Path,
    repository: str,
    issue_number: int,
) -> None:
    issue = _gh_api(
        commands,
        root,
        ("gh", "api", f"repos/{repository}/issues/{issue_number}"),
    )
    if issue.get("state") == "closed":
        return
    _gh_api(
        commands,
        root,
        (
            "gh",
            "api",
            "--method",
            "PATCH",
            f"repos/{repository}/issues/{issue_number}",
            "--input",
            "-",
        ),
        input_document={"state": "closed"},
    )


__all__ = ["build_production_workspace_operations_bindings"]
