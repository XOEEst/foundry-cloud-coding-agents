from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

from foundry_opt.adapters.commands import (
    CommandError,
    SubprocessCommandRunner,
)
from foundry_opt.adapters.github import github_repository_from_remote_url
from foundry_opt.config import load_config
from foundry_opt.config.models import (
    MetricDirection as ConfigMetricDirection,
    OptimizerConfig,
    UndefinedBehavior as ConfigUndefinedBehavior,
)
from foundry_opt.evaluation import (
    EvaluationPolicy,
    MetricDirection,
    MetricPolicy,
    UndefinedBehavior,
)
from foundry_opt.orchestration.candidate_experiments import (
    CandidateExperimentAdapter,
    CandidateExperimentRequest,
    CandidateExperimentResult,
)
from foundry_opt.orchestration.workspace import (
    OptimizationWorkspace,
    WorkspaceCandidate,
    WorkspaceCandidateWorkContract,
    WorkspaceCandidateProposal,
    WorkspaceIssue,
    WorkspaceOperation,
    WorkspaceNextAction,
    WorkspaceNextActionKind,
    WorkspacePhase,
    WorkspacePullRequest,
    WorkspacePriorExperiment,
    WorkspaceReportContext,
    WorkspaceRequest,
    WorkspaceResult,
    WorkspaceIssueStatusProjectionIntent,
    WorkspaceTrigger,
)
from foundry_opt.orchestration.workspace_assignment import (
    GhWorkspaceCopilotAssigner,
)
from foundry_opt.orchestration.workspace_coordinator import (
    GhWorkspacePullRequestFinalizer,
    GitWorkspaceExactBranchPublisher,
    TrustedWorkspaceSelector,
    WorkspaceCandidateCoordinator,
)
from foundry_opt.orchestration.workspace_github import (
    GhWorkspacePullRequests,
    workspace_pull_request_base_commit,
)
from foundry_opt.orchestration.workspace_git_store import GitWorkspaceStore
from foundry_opt.orchestration.workspace_store import (
    WorkspaceExperimentRecord,
    WorkspaceSnapshot,
    WorkspaceUpdate,
)
from foundry_opt.orchestration.workspace_baseline import (
    WorkspaceBaselineExecutionResult,
    WorkspaceBaselineExecutor,
    WorkspaceBaselineRequestBuilder,
)
from foundry_opt.orchestration.workspace_specification import (
    TrustedWorkspaceSpecificationResolver,
)
from foundry_opt.orchestration.workspace_intake import (
    NormalizedWorkspaceEvent,
    TrustedWorkspaceEventContext,
    normalize_workspace_event,
)
from foundry_opt.orchestration.workspace_manifest import (
    WorkspaceCandidateManifest,
    parse_workspace_candidate_manifest,
    parse_workspace_experiment_manifest,
)
from foundry_opt.orchestration.workspace_policy import (
    ConfiguredWorkspaceSelector,
)
from foundry_opt.orchestration.workspace_projection import (
    GhWorkspaceIssueProjector,
    WorkspaceIssueProjector,
)
from foundry_opt.orchestration.workspace_execution_production import (
    build_production_workspace_service_bindings,
)
from foundry_opt.orchestration.workspace_verifier import (
    WorkspaceVerificationResult,
    WorkspaceVerifier,
)
from foundry_opt.orchestration.workspace_experiments import (
    GitWorkspaceCandidatePreparer,
    TrustedWorkspaceExperimentResultContext,
    WorkspaceExperimentExecutionResult,
    WorkspaceExperimentExecutor,
    WorkspaceExperimentRequestBuilder,
    normalize_workspace_experiment_result,
)
from foundry_opt.orchestration.workspace_operations import (
    NormalizedWorkspaceOperation,
    TrustedWorkspaceOperationContext,
    normalize_workspace_operation,
)
from foundry_opt.orchestration.workspace_operation_store import (
    GitWorkspaceOperationStore,
)
from foundry_opt.preflight.interfaces import CommandRunner
from foundry_opt.security import reject_secret_content


_COMMIT = re.compile(r"^[0-9a-fA-F]{40}$")
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
)
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,199}$")
_COPILOT_MARKERS = (
    "COPILOT_AGENT_SOURCE_ENVIRONMENT",
    "COPILOT_AGENT_START_TIME_SEC",
    "COPILOT_AGENT_TIMEOUT_MIN",
    "COPILOT_AGENT_SESSION_ID",
)


class ProductionWorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkspaceAdvanceRequest:
    repository_root: Path
    issue_number: int
    trigger: WorkspaceTrigger = WorkspaceTrigger.CONTINUE
    base_commit: str | None = None
    workspace_pull_request: WorkspacePullRequest | None = None
    expected_repository: str | None = None
    trusted_repository_id: int | None = None
    candidates: tuple[WorkspaceCandidate, ...] = ()
    report_context: WorkspaceReportContext | None = None
    candidate_count: int | None = None
    selector: TrustedWorkspaceSelector | None = None
    operation: WorkspaceOperation | None = None

    def __post_init__(self) -> None:
        if type(self.issue_number) is not int or self.issue_number < 1:
            raise ValueError("workspace issue number is invalid")
        if (
            self.base_commit is not None
            and _COMMIT.fullmatch(self.base_commit) is None
        ):
            raise ValueError("workspace base commit is invalid")
        if (
            self.expected_repository is not None
            and _REPOSITORY.fullmatch(self.expected_repository) is None
        ):
            raise ValueError("workspace repository is invalid")
        if (
            self.trusted_repository_id is not None
            and (
                type(self.trusted_repository_id) is not int
                or self.trusted_repository_id < 1
            )
        ):
            raise ValueError("workspace repository ID is invalid")


@dataclass(frozen=True)
class WorkspaceIntakeResult:
    event: NormalizedWorkspaceEvent
    workspace: WorkspaceResult

    @property
    def exit_code(self) -> int:
        return 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": {
                "delivery_id": self.event.delivery_id,
                "kind": self.event.kind.value,
                "repository": self.event.repository,
                "repository_id": self.event.repository_id,
                "trigger": self.event.trigger.value,
            },
            "workspace": self.workspace.to_dict(),
        }


@dataclass(frozen=True)
class WorkspaceOperationIntakeResult:
    event: NormalizedWorkspaceOperation
    workspace: WorkspaceResult

    @property
    def exit_code(self) -> int:
        return 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": {
                "delivery_id": self.event.delivery_id,
                "operation_id": self.event.operation.operation_id,
                "repository": self.event.repository,
                "repository_id": self.event.repository_id,
                "trigger": self.event.operation.trigger.value,
            },
            "workspace": self.workspace.to_dict(),
        }


@dataclass(frozen=True)
class WorkspaceCopilotAssignmentResult:
    issue_number: int
    workspace_pull_request_number: int | None
    next_action: str
    status: str
    assigned: bool

    @property
    def exit_code(self) -> int:
        return 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "assigned": self.assigned,
            "issue_number": self.issue_number,
            "next_action": self.next_action,
            "status": self.status,
            "workspace_pull_request_number": (
                self.workspace_pull_request_number
            ),
        }


@dataclass(frozen=True)
class _RepositoryContext:
    repository: str
    default_branch: str


def _trusted_copilot_repository_context() -> (
    tuple[_RepositoryContext, int] | None
):
    if os.environ.get("FOUNDRY_OPT_COPILOT_GIT_PROXY") != "1":
        return None
    markers = tuple(os.environ.get(name, "") for name in _COPILOT_MARKERS)
    if (
        any(not value for value in markers)
        or not markers[1].isdigit()
        or not markers[2].isdigit()
        or len(markers[3]) < 16
    ):
        return None
    repository = os.environ.get("FOUNDRY_OPT_REPOSITORY", "")
    repository_id = os.environ.get("FOUNDRY_OPT_REPOSITORY_ID", "")
    default_branch = os.environ.get("FOUNDRY_OPT_DEFAULT_BRANCH", "")
    if (
        _REPOSITORY.fullmatch(repository) is None
        or not repository_id.isdigit()
        or int(repository_id) < 1
        or _BRANCH.fullmatch(default_branch) is None
        or ".." in default_branch
        or "//" in default_branch
        or default_branch.endswith("/")
    ):
        raise ProductionWorkspaceError(
            "trusted Copilot repository context is invalid"
        )
    return (
        _RepositoryContext(repository, default_branch),
        int(repository_id),
    )


WorkspaceFactory = Callable[..., OptimizationWorkspace]
CopilotAssignerFactory = Callable[..., GhWorkspaceCopilotAssigner]
IssueProjectorFactory = Callable[..., WorkspaceIssueProjector]


def build_production_workspace(
    repository_root: Path,
    *,
    repository: str,
    base_branch: str,
    commands: CommandRunner | None = None,
    candidate_count: int | None = None,
    selector: TrustedWorkspaceSelector | None = None,
) -> OptimizationWorkspace:
    runner = commands or SubprocessCommandRunner()
    store = GitWorkspaceStore(repository_root)
    if _trusted_copilot_repository_context() is not None:
        from foundry_opt.orchestration.workspace_runtime import (
            PlanningWorkspacePullRequests,
        )

        pull_requests = PlanningWorkspacePullRequests()
    else:
        pull_requests = GhWorkspacePullRequests(
            runner,
            repository=repository,
            base_branch=base_branch,
        )
    candidate_coordinator = None
    configured = (
        candidate_count,
        selector,
    )
    if any(item is not None for item in configured):
        if any(item is None for item in configured):
            raise ValueError(
                "workspace candidate production wiring is incomplete"
            )
        assert candidate_count is not None
        assert selector is not None
        candidate_coordinator = WorkspaceCandidateCoordinator(
            store=store,
            selector=selector,
            exact_publisher=GitWorkspaceExactBranchPublisher(runner),
            candidate_count=candidate_count,
            finalizer=GhWorkspacePullRequestFinalizer(
                runner,
                repository=repository,
            ),
        )
    return OptimizationWorkspace(
        store=store,
        pull_requests=pull_requests,
        candidate_coordinator=candidate_coordinator,
    )


def build_production_workspace_service(
    *,
    actions_execution: bool = False,
) -> ProductionWorkspaceService:
    return ProductionWorkspaceService(
        **build_production_workspace_service_bindings(
            Path.cwd(),
            actions_execution=actions_execution,
        )
    )


class ProductionWorkspaceService:
    def __init__(
        self,
        *,
        commands: CommandRunner | None = None,
        workspace_factory: WorkspaceFactory = build_production_workspace,
        experiment_runner: CandidateExperimentAdapter | None = None,
        experiment_request_builder: (
            WorkspaceExperimentRequestBuilder | None
        ) = None,
        baseline_request_builder: (
            WorkspaceBaselineRequestBuilder | None
        ) = None,
        specification_resolver: (
            TrustedWorkspaceSpecificationResolver | None
        ) = None,
        copilot_assigner_factory: CopilotAssignerFactory = (
            GhWorkspaceCopilotAssigner
        ),
        issue_projector_factory: IssueProjectorFactory = (
            GhWorkspaceIssueProjector
        ),
    ) -> None:
        self._commands = commands or SubprocessCommandRunner()
        self._workspace_factory = workspace_factory
        self._experiment_runner = experiment_runner
        self._experiment_request_builder = experiment_request_builder
        self._baseline_request_builder = baseline_request_builder
        self._specification_resolver = (
            specification_resolver
            or TrustedWorkspaceSpecificationResolver()
        )
        self._copilot_assigner_factory = copilot_assigner_factory
        self._issue_projector_factory = issue_projector_factory

    def assign_copilot(
        self,
        *,
        repository_root: Path,
        issue_number: int,
        assignment_token: str | None,
    ) -> WorkspaceCopilotAssignmentResult:
        if type(issue_number) is not int or issue_number < 1:
            raise ValueError("workspace assignment issue is invalid")
        root = repository_root.expanduser().resolve()
        snapshot = GitWorkspaceStore(root).load(issue_number)
        if snapshot is None:
            raise ProductionWorkspaceError(
                "workspace state is unavailable"
            )
        next_action, requires_copilot = _copilot_assignment_action(snapshot)
        pull_request_number = snapshot.workspace_pull_request_number
        if not requires_copilot:
            return WorkspaceCopilotAssignmentResult(
                issue_number=issue_number,
                workspace_pull_request_number=pull_request_number,
                next_action=next_action,
                status="not_required",
                assigned=False,
            )
        if pull_request_number is None:
            raise ProductionWorkspaceError(
                "workspace Copilot assignment requires its pull request"
            )
        if not assignment_token:
            raise ProductionWorkspaceError(
                "Copilot assignment token is required"
            )
        context = self._repository_context(root)
        existing = self._existing_workspace_pull_request(
            root,
            context.repository,
            issue_number,
        )
        if existing is None or existing[0] != pull_request_number:
            raise ProductionWorkspaceError(
                "workspace assignment pull request identity changed"
            )
        assigned = self._copilot_assigner_factory(
            commands=self._commands,
            repository_root=root,
            repository=context.repository,
            assignment_token=assignment_token,
        ).assign(
            issue_number=issue_number,
            pull_request_number=pull_request_number,
            assignment_key=snapshot.revision,
        )
        return WorkspaceCopilotAssignmentResult(
            issue_number=issue_number,
            workspace_pull_request_number=pull_request_number,
            next_action=next_action,
            status="assigned" if assigned else "already_assigned",
            assigned=assigned,
        )

    def advance(self, request: WorkspaceAdvanceRequest) -> WorkspaceResult:
        if (
            request.trigger is WorkspaceTrigger.PULL_REQUEST_MERGED
            and (
                request.workspace_pull_request is None
                or request.expected_repository is None
                or request.trusted_repository_id is None
            )
        ):
            raise ProductionWorkspaceError(
                "workspace merge requires trusted event intake"
            )
        if (
            request.trigger
            in {
                WorkspaceTrigger.DEPLOYMENT_COMPLETED,
                WorkspaceTrigger.RETENTION_COMPLETED,
            }
            and (
                request.operation is None
                or request.expected_repository is None
                or request.trusted_repository_id is None
            )
        ):
            raise ProductionWorkspaceError(
                "workspace lifecycle requires trusted operation intake"
            )
        root = request.repository_root.expanduser().resolve()
        context = self._repository_context(root)
        if (
            request.expected_repository is not None
            and request.expected_repository.casefold()
            != context.repository.casefold()
        ):
            raise ProductionWorkspaceError(
                "trusted workspace repository does not match origin"
            )
        if request.trusted_repository_id is not None:
            actual_repository_id = self._repository_id(
                root,
                context.repository,
            )
            if actual_repository_id != request.trusted_repository_id:
                raise ProductionWorkspaceError(
                    "trusted workspace repository ID does not match GitHub"
                )
        issue = self._issue(root, context.repository, request.issue_number)
        existing = self._existing_workspace_pull_request(
            root,
            context.repository,
            request.issue_number,
        )
        pull_request = request.workspace_pull_request
        if pull_request is not None:
            if (
                existing is not None
                and existing[0] != pull_request.number
            ):
                raise ProductionWorkspaceError(
                    "workspace pull request does not match recorded workspace"
                )
            base_commit = pull_request.base_commit
        elif existing is not None:
            number, base_commit = existing
            if (
                request.base_commit is not None
                and request.base_commit.casefold()
                != base_commit.casefold()
            ):
                raise ProductionWorkspaceError(
                    "workspace manifest base does not match workspace PR"
                )
            selected = request.trigger in {
                WorkspaceTrigger.PULL_REQUEST_MERGED,
                WorkspaceTrigger.DEPLOYMENT_COMPLETED,
                WorkspaceTrigger.RETENTION_COMPLETED,
            }
            pull_request = WorkspacePullRequest(
                number=number,
                issue_number=request.issue_number,
                branch=(
                    "foundry-opt/workspace/"
                    f"issue-{request.issue_number}"
                ),
                title=(
                    f"[Optimize] #{request.issue_number} selected candidate"
                    if selected
                    else (
                        f"[Optimize] #{request.issue_number} workspace - "
                        "draft, not yet selectable"
                    )
                ),
                draft=not selected,
                reuse_existing=True,
                base_commit=base_commit,
            )
        else:
            base_commit = request.base_commit or self._default_commit(
                root,
                context.default_branch,
            )
        workspace_issue = WorkspaceIssue(
            number=request.issue_number,
            title=issue["title"],
            body=issue["body"],
            base_commit=base_commit.lower(),
        )
        if request.trigger is WorkspaceTrigger.CONTINUE:
            snapshot = _load_workspace_snapshot(root, request.issue_number)
            if snapshot is not None:
                snapshot = self._ensure_trusted_specification(
                    root,
                    repository=context.repository,
                    base_branch=context.default_branch,
                    issue=workspace_issue,
                    snapshot=snapshot,
                )
                snapshot = self._ensure_trusted_baseline_planned(
                    root,
                    snapshot=snapshot,
                )
                gated = _trusted_intake_gate(snapshot, pull_request)
                if gated is not None:
                    gated = self._with_candidate_work(root, gated, snapshot)
                    self._project_workspace_result(
                        root,
                        repository=context.repository,
                        result=gated,
                    )
                    return gated
        workspace = self._workspace_factory(
            repository_root=root,
            repository=context.repository,
            base_branch=context.default_branch,
            commands=self._commands,
            candidate_count=request.candidate_count,
            selector=request.selector,
        )
        result = workspace.advance(
            WorkspaceRequest(
                repository_root=root,
                issue=workspace_issue,
                trigger=request.trigger,
                workspace_pull_request=pull_request,
                candidates=request.candidates,
                report_context=request.report_context,
                operation=request.operation,
            )
        )
        if request.trigger in {
            WorkspaceTrigger.ISSUE_CREATED,
            WorkspaceTrigger.CONTINUE,
        }:
            snapshot = _load_workspace_snapshot(root, request.issue_number)
            if snapshot is not None:
                snapshot = self._ensure_trusted_specification(
                    root,
                    repository=context.repository,
                    base_branch=context.default_branch,
                    issue=workspace_issue,
                    snapshot=snapshot,
                )
                snapshot = self._ensure_trusted_baseline_planned(
                    root,
                    snapshot=snapshot,
                )
                result = (
                    _trusted_intake_gate(
                        snapshot, result.workspace_pull_request
                    )
                    or result
                )
                result = self._with_candidate_work(root, result, snapshot)
        self._project_workspace_result(
            root,
            repository=context.repository,
            result=result,
        )
        return result

    def _with_candidate_work(
        self,
        root: Path,
        result: WorkspaceResult,
        snapshot: WorkspaceSnapshot,
    ) -> WorkspaceResult:
        action = result.next_action
        specification = snapshot.specification
        if (
            action is None
            or action.kind
            is not WorkspaceNextActionKind.RUN_CANDIDATE_EXPERIMENTS
            or specification is None
        ):
            return result
        config = load_config(
            root / ".github" / "foundry-optimizer.yaml"
        )
        target = config.targets.get(specification.target)
        if target is None:
            raise ProductionWorkspaceError(
                "workspace candidate target is not configured"
            )
        candidate_limit = _configured_candidate_limit(
            config,
            specification.target,
        )
        completed = tuple(
            experiment
            for experiment in snapshot.experiments
            if experiment.status == "completed"
        )
        candidate_number = len(snapshot.experiments) + 1
        if candidate_number > candidate_limit:
            raise ProductionWorkspaceError(
                "workspace candidate action exceeds configured limit"
            )
        contract = WorkspaceCandidateWorkContract(
            issue_number=snapshot.issue_number,
            target=specification.target,
            base_commit=specification.base_commit,
            candidate_id=f"candidate-{candidate_number}",
            candidate_number=candidate_number,
            candidate_limit=candidate_limit,
            allowed_mutations=tuple(
                getattr(item, "value", str(item))
                for item in target.allowed_mutations
            ),
            prior_experiments=tuple(
                WorkspacePriorExperiment(
                    candidate_id=experiment.candidate_id,
                    mutation_class=experiment.mutation_class,
                    metrics=experiment.metrics,
                    guardrails=experiment.guardrails,
                    changed_paths=experiment.changed_paths,
                )
                for experiment in completed
            ),
        )
        return replace(
            result,
            next_action=replace(action, candidate_work=contract),
        )

    def _ensure_trusted_specification(
        self,
        root: Path,
        *,
        repository: str,
        base_branch: str,
        issue: WorkspaceIssue,
        snapshot: WorkspaceSnapshot,
    ) -> WorkspaceSnapshot:
        if snapshot.specification is not None:
            if snapshot.specification.base_commit != issue.base_commit:
                raise ProductionWorkspaceError(
                    "trusted workspace specification base changed"
                )
            return snapshot
        config = load_config(
            root / ".github" / "foundry-optimizer.yaml"
        )
        specification = self._specification_resolver.resolve(
            repository_root=root,
            repository=repository,
            base_branch=base_branch,
            issue=issue,
            config=config,
        )
        return GitWorkspaceStore(root).commit(
            expected_revision=snapshot.revision,
            update=WorkspaceUpdate(
                issue_number=snapshot.issue_number,
                phase=snapshot.phase,
                workspace_pull_request_number=(
                    snapshot.workspace_pull_request_number
                ),
                semantic_event="trusted_specification_resolved",
                candidates=snapshot.candidates,
                selected_patch=snapshot.selected_patch,
                external_operation_ids=snapshot.external_operation_ids,
                experiments=snapshot.experiments,
                lineage=snapshot.lineage,
                specification=specification,
                baseline=snapshot.baseline,
            ),
        )

    def _ensure_trusted_baseline_planned(
        self,
        root: Path,
        *,
        snapshot: WorkspaceSnapshot,
    ) -> WorkspaceSnapshot:
        specification = snapshot.specification
        if (
            specification is None
            or specification.status != "policy_approved"
            or snapshot.baseline is not None
            or self._baseline_request_builder is None
        ):
            return snapshot
        WorkspaceBaselineExecutor(
            store=GitWorkspaceStore(root),
            runner=None,
            request_builder=self._baseline_request_builder,
        ).plan(
            repository_root=root,
            issue_number=snapshot.issue_number,
            target=specification.target,
            base_commit=specification.base_commit,
        )
        planned = GitWorkspaceStore(root).load(snapshot.issue_number)
        if planned is None or planned.baseline is None:
            raise ProductionWorkspaceError(
                "trusted baseline operation was not persisted"
            )
        return planned

    def _project_workspace_result(
        self,
        root: Path,
        *,
        repository: str,
        result: WorkspaceResult,
    ) -> None:
        if _trusted_copilot_repository_context() is not None:
            return
        intent = result.issue_status_projection_intent
        if intent is None:
            return
        pull_request = result.workspace_pull_request
        if (
            pull_request is None
            or pull_request.number
            != intent.workspace_pull_request_number
            or pull_request.issue_number != intent.issue_number
        ):
            raise ProductionWorkspaceError(
                "workspace issue projection identity changed"
            )
        self._issue_projector_factory(
            commands=self._commands,
            repository_root=root,
            repository=repository,
        ).project(
            intent,
            base_commit=pull_request.base_commit,
            report=result.report,
        )

    def complete_experiments(
        self,
        payload: Mapping[str, Any],
        *,
        repository_root: Path,
    ) -> WorkspaceResult:
        root = repository_root.expanduser().resolve()
        target_name = payload.get("target")
        if not isinstance(target_name, str):
            raise ProductionWorkspaceError(
                "workspace manifest target is invalid"
            )
        config = load_config(
            root / ".github" / "foundry-optimizer.yaml"
        )
        target = config.targets.get(target_name)
        if target is None:
            raise ProductionWorkspaceError(
                "workspace manifest target is not configured"
            )
        policy = _evaluation_policy(target.metrics)
        manifest = parse_workspace_experiment_manifest(payload)
        candidate_count = (
            target.campaign_overrides.max_changed_candidates
            if (
                target.campaign_overrides is not None
                and target.campaign_overrides.max_changed_candidates
                is not None
            )
            else config.campaign.max_changed_candidates
        )
        if len(manifest.candidates) != candidate_count:
            raise ProductionWorkspaceError(
                "workspace manifest does not contain configured candidates"
            )
        if not config.automation_policy.required_checks:
            raise ProductionWorkspaceError(
                "workspace selection requires configured checks"
            )
        context = self._repository_context(root)
        selector = ConfiguredWorkspaceSelector(
            self._commands,
            repository_root=root,
            repository=context.repository,
            required_checks=tuple(
                config.automation_policy.required_checks
            ),
        )
        snapshot = GitWorkspaceStore(root).load(manifest.issue_number)
        if snapshot is None:
            raise ProductionWorkspaceError(
                "workspace state is unavailable"
            )
        if (
            snapshot.specification is None
            or snapshot.specification.status != "policy_approved"
            or snapshot.specification.spec_sha256 is None
            or snapshot.specification.target != manifest.target
            or snapshot.specification.base_commit != manifest.base_commit
            or snapshot.baseline is None
            or snapshot.baseline.status != "completed"
        ):
            raise ProductionWorkspaceError(
                "trusted specification and baseline are incomplete"
            )
        report_context = WorkspaceReportContext(
            baseline_metrics=snapshot.baseline.metrics,
            policy=policy,
            sample_count=snapshot.baseline.sample_count,
            split=snapshot.baseline.split,
            spec_sha256=snapshot.specification.spec_sha256,
        )
        candidates = _trusted_candidates(
            manifest.issue_number,
            manifest.candidates,
            snapshot.experiments,
        )
        return self.advance(
            WorkspaceAdvanceRequest(
                repository_root=root,
                issue_number=manifest.issue_number,
                trigger=WorkspaceTrigger.EXPERIMENTS_COMPLETED,
                base_commit=manifest.base_commit,
                expected_repository=context.repository,
                candidates=candidates,
                report_context=report_context,
                candidate_count=candidate_count,
                selector=selector,
            )
        )

    def execute_experiment(
        self,
        payload: Mapping[str, Any],
        *,
        repository_root: Path,
    ) -> WorkspaceExperimentExecutionResult:
        root = repository_root.expanduser().resolve()
        manifest = parse_workspace_candidate_manifest(payload)
        config = load_config(
            root / ".github" / "foundry-optimizer.yaml"
        )
        if manifest.target not in config.targets:
            raise ProductionWorkspaceError(
                "workspace candidate target is not configured"
            )
        context = self._repository_context(root)
        existing = self._existing_workspace_pull_request(
            root,
            context.repository,
            manifest.issue_number,
        )
        if existing is None or existing[1] != manifest.base_commit:
            raise ProductionWorkspaceError(
                "workspace candidate base does not match workspace PR"
            )
        if (
            self._experiment_runner is None
        ):
            raise ProductionWorkspaceError(
                "workspace experiment executor is not configured"
            )
        if _trusted_copilot_repository_context() is not None:
            return self._write_candidate_proxy_envelope(
                root,
                payload=payload,
                manifest=manifest,
                config=config,
            )
        GitWorkspaceOperationStore(root).record_candidate_manifest(
            manifest.issue_number,
            payload,
        )
        request_builder = (
            self._experiment_request_builder
            or GitWorkspaceCandidatePreparer(
                commands=self._commands,
                config=config,
            )
        )
        return WorkspaceExperimentExecutor(
            store=GitWorkspaceStore(root),
            runner=self._experiment_runner,
            request_builder=request_builder,
        ).execute(
            repository_root=root,
            issue_number=manifest.issue_number,
            target=manifest.target,
            base_commit=manifest.base_commit,
            proposal=manifest.candidate,
        )

    def _write_candidate_proxy_envelope(
        self,
        root: Path,
        *,
        payload: Mapping[str, Any],
        manifest: WorkspaceCandidateManifest,
        config: OptimizerConfig,
    ) -> WorkspaceExperimentExecutionResult:
        snapshot = GitWorkspaceStore(root).load(manifest.issue_number)
        if (
            snapshot is None
            or snapshot.phase is not WorkspacePhase.EVALUATING
            or snapshot.specification is None
            or snapshot.specification.status != "policy_approved"
            or snapshot.specification.target != manifest.target
            or snapshot.specification.base_commit != manifest.base_commit
            or snapshot.baseline is None
            or snapshot.baseline.status != "completed"
            or any(
                experiment.status == "pending"
                for experiment in snapshot.experiments
            )
        ):
            raise ProductionWorkspaceError(
                "workspace candidate proxy state is unavailable"
            )
        candidate_number = len(snapshot.experiments) + 1
        candidate_limit = _configured_candidate_limit(
            config,
            manifest.target,
        )
        if (
            candidate_number > candidate_limit
            or manifest.candidate.candidate_id
            != f"candidate-{candidate_number}"
        ):
            raise ProductionWorkspaceError(
                "workspace candidate proxy slot is invalid"
            )
        document = {
            "expected_revision": snapshot.revision,
            "kind": "workspace_candidate_proposal",
            "manifest": dict(payload),
            "schema_version": 1,
        }
        content = (
            json.dumps(
                document,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        envelope_path = (
            root / ".foundry-optimizer" / "workspace-candidate.json"
        )
        envelope_path.parent.mkdir(parents=True, exist_ok=True)
        envelope_path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        return WorkspaceExperimentExecutionResult(
            issue_number=manifest.issue_number,
            candidate_id=manifest.candidate.candidate_id,
            status="proxy_import_required",
            recorded=False,
            operation_sha256=digest,
            idempotency_key=digest,
            next_action=(
                "commit_workspace_candidate_envelope:"
                ".foundry-optimizer/workspace-candidate.json"
            ),
        )

    def ingest_experiment_result(
        self,
        payload: Mapping[str, Any],
        context: TrustedWorkspaceExperimentResultContext,
        *,
        repository_root: Path,
    ) -> WorkspaceExperimentExecutionResult:
        event = normalize_workspace_experiment_result(payload, context)
        root = repository_root.expanduser().resolve()
        repository = self._repository_context(root).repository
        if repository.casefold() != event.repository.casefold():
            raise ProductionWorkspaceError(
                "trusted experiment repository does not match origin"
            )
        if self._repository_id(root, repository) != event.repository_id:
            raise ProductionWorkspaceError(
                "trusted experiment repository ID does not match GitHub"
            )
        return WorkspaceExperimentExecutor(
            store=GitWorkspaceStore(root),
            runner=None,
            request_builder=None,
        ).ingest_result(
            issue_number=event.issue_number,
            result=event.result,
        )

    def execute_baseline(
        self,
        *,
        repository_root: Path,
        issue_number: int,
    ) -> WorkspaceBaselineExecutionResult:
        root = repository_root.expanduser().resolve()
        snapshot = GitWorkspaceStore(root).load(issue_number)
        if (
            snapshot is None
            or snapshot.specification is None
            or snapshot.specification.status != "policy_approved"
        ):
            raise ProductionWorkspaceError(
                "trusted workspace specification is incomplete"
            )
        specification = snapshot.specification
        if (
            self._experiment_runner is None
            or self._baseline_request_builder is None
        ):
            raise ProductionWorkspaceError(
                "workspace baseline executor is not configured"
            )
        return WorkspaceBaselineExecutor(
            store=GitWorkspaceStore(root),
            runner=self._experiment_runner,
            request_builder=self._baseline_request_builder,
        ).execute(
            repository_root=root,
            issue_number=issue_number,
            target=specification.target,
            base_commit=specification.base_commit,
        )

    def ingest_baseline_result(
        self,
        payload: Mapping[str, Any],
        context: TrustedWorkspaceExperimentResultContext,
        *,
        repository_root: Path,
    ) -> WorkspaceBaselineExecutionResult:
        event = normalize_workspace_experiment_result(payload, context)
        if event.result.candidate_id != "baseline":
            raise ProductionWorkspaceError(
                "trusted baseline result identity is invalid"
            )
        root = repository_root.expanduser().resolve()
        repository = self._repository_context(root).repository
        if (
            repository.casefold() != event.repository.casefold()
            or self._repository_id(root, repository)
            != event.repository_id
        ):
            raise ProductionWorkspaceError(
                "trusted baseline repository does not match origin"
            )
        return WorkspaceBaselineExecutor(
            store=GitWorkspaceStore(root),
            runner=None,
            request_builder=None,
        ).ingest_result(
            issue_number=event.issue_number,
            result=event.result,
        )

    def verify(
        self,
        *,
        repository_root: Path,
        issue_number: int,
        pull_request_number: int,
    ) -> WorkspaceVerificationResult:
        root = repository_root.expanduser().resolve()
        if (
            type(issue_number) is not int
            or issue_number < 1
            or type(pull_request_number) is not int
            or pull_request_number < 1
        ):
            raise ValueError("workspace verification identity is invalid")
        context = self._repository_context(root)
        return WorkspaceVerifier(
            store=GitWorkspaceStore(root),
            commands=self._commands,
            repository=context.repository,
            base_branch=context.default_branch,
        ).verify(
            root,
            issue_number=issue_number,
            pull_request_number=pull_request_number,
        )

    def ingest(
        self,
        payload: Mapping[str, Any],
        context: TrustedWorkspaceEventContext,
        *,
        base_commit: str | None = None,
        repository_root: Path,
    ) -> WorkspaceIntakeResult:
        event = normalize_workspace_event(
            payload,
            context,
            base_commit=base_commit,
        )
        result = self.advance(
            WorkspaceAdvanceRequest(
                repository_root=repository_root,
                issue_number=event.issue_number,
                trigger=event.trigger,
                base_commit=event.base_commit,
                workspace_pull_request=event.workspace_pull_request,
                expected_repository=event.repository,
                trusted_repository_id=event.repository_id,
            )
        )
        return WorkspaceIntakeResult(event=event, workspace=result)

    def ingest_operation(
        self,
        payload: Mapping[str, Any],
        context: TrustedWorkspaceOperationContext,
        *,
        repository_root: Path,
    ) -> WorkspaceOperationIntakeResult:
        event = normalize_workspace_operation(payload, context)
        result = self.advance(
            WorkspaceAdvanceRequest(
                repository_root=repository_root,
                issue_number=event.issue_number,
                trigger=event.operation.trigger,
                expected_repository=event.repository,
                trusted_repository_id=event.repository_id,
                operation=event.operation,
            )
        )
        return WorkspaceOperationIntakeResult(
            event=event,
            workspace=result,
        )

    def _repository_context(self, root: Path) -> _RepositoryContext:
        copilot = _trusted_copilot_repository_context()
        if copilot is not None:
            return copilot[0]
        try:
            remote = self._commands.run(
                ("git", "remote", "get-url", "origin"),
                cwd=root,
            ).stdout.strip()
            origin = github_repository_from_remote_url(remote)
            if origin is None:
                raise ProductionWorkspaceError(
                    "workspace origin is not a GitHub repository"
                )
            document = self._json_object(
                (
                    "gh",
                    "repo",
                    "view",
                    origin,
                    "--json",
                    "nameWithOwner,defaultBranchRef",
                ),
                root,
            )
        except CommandError as error:
            raise ProductionWorkspaceError(
                "workspace repository metadata is unavailable"
            ) from error
        repository = document.get("nameWithOwner")
        default_ref = document.get("defaultBranchRef")
        default_branch = (
            default_ref.get("name")
            if isinstance(default_ref, Mapping)
            else None
        )
        if (
            not isinstance(repository, str)
            or repository.casefold() != origin.casefold()
            or not isinstance(default_branch, str)
            or not default_branch
        ):
            raise ProductionWorkspaceError(
                "workspace repository metadata is invalid"
            )
        return _RepositoryContext(repository, default_branch)

    def _repository_id(self, root: Path, repository: str) -> int:
        copilot = _trusted_copilot_repository_context()
        if copilot is not None:
            context, repository_id = copilot
            if context.repository.casefold() != repository.casefold():
                raise ProductionWorkspaceError(
                    "workspace repository ID context changed"
                )
            return repository_id
        try:
            value = self._commands.run(
                (
                    "gh",
                    "api",
                    f"repos/{repository}",
                    "--jq",
                    ".id",
                ),
                cwd=root,
            ).stdout.strip()
        except CommandError as error:
            raise ProductionWorkspaceError(
                "workspace repository ID is unavailable"
            ) from error
        if not value.isdecimal() or int(value) < 1:
            raise ProductionWorkspaceError(
                "workspace repository ID is invalid"
            )
        return int(value)

    def _issue(
        self,
        root: Path,
        repository: str,
        issue_number: int,
    ) -> dict[str, str]:
        try:
            value = self._json_object(
                (
                    "gh",
                    "issue",
                    "view",
                    str(issue_number),
                    "--repo",
                    repository,
                    "--json",
                    "number,title,body,state",
                ),
                root,
            )
        except CommandError as error:
            copilot = _trusted_copilot_repository_context()
            snapshot = GitWorkspaceStore(root).load(issue_number)
            if (
                copilot is not None
                and snapshot is not None
                and snapshot.specification is not None
            ):
                return {
                    "title": (
                        f"[Optimize] Persisted workspace issue "
                        f"#{issue_number}"
                    ),
                    "body": (
                        "Immutable specification already persisted by "
                        "trusted issue intake."
                    ),
                }
            raise ProductionWorkspaceError(
                "workspace issue is unavailable"
            ) from error
        title = value.get("title")
        body = value.get("body")
        if (
            value.get("number") != issue_number
            or value.get("state") != "OPEN"
            or not isinstance(title, str)
            or not title.startswith("[Optimize] ")
            or len(title) > 256
            or not isinstance(body, str)
            or len(body) > 262_144
        ):
            raise ProductionWorkspaceError(
                "workspace optimization issue is invalid"
            )
        reject_secret_content(title)
        reject_secret_content(body)
        return {"title": title, "body": body}

    def _existing_workspace_pull_request(
        self,
        root: Path,
        repository: str,
        issue_number: int,
    ) -> tuple[int, str] | None:
        copilot = _trusted_copilot_repository_context()
        if copilot is not None:
            snapshot = GitWorkspaceStore(root).load(issue_number)
            if (
                snapshot is not None
                and snapshot.specification is not None
                and snapshot.workspace_pull_request_number is not None
            ):
                return (
                    snapshot.workspace_pull_request_number,
                    snapshot.specification.base_commit,
                )
        branch = f"foundry-opt/workspace/issue-{issue_number}"
        commands = (
            (
                "gh",
                "pr",
                "list",
                "--repo",
                repository,
                "--state",
                "all",
                "--head",
                branch,
                "--json",
                "number,body",
                "--limit",
                "2",
            ),
            (
                "gh",
                "pr",
                "list",
                "--repo",
                repository,
                "--state",
                "all",
                "--search",
                (
                    '"foundry-opt:workspace-pr:'
                    f'issue-{issue_number}:v1" in:body'
                ),
                "--json",
                "number,body",
                "--limit",
                "2",
            ),
        )
        matches: dict[int, dict[str, Any]] = {}
        try:
            for command in commands:
                values = self._json_list(command, root)
                for item in values:
                    number = item.get("number")
                    if type(number) is not int or number < 1:
                        raise ProductionWorkspaceError(
                            "workspace pull request lookup is invalid"
                        )
                    previous = matches.get(number)
                    if previous is not None and previous != item:
                        raise ProductionWorkspaceError(
                            "workspace pull request lookup is inconsistent"
                        )
                    matches[number] = item
        except CommandError as error:
            raise ProductionWorkspaceError(
                "workspace pull request lookup failed"
            ) from error
        if len(matches) > 1:
            raise ProductionWorkspaceError(
                "multiple workspace pull requests found"
            )
        if not matches:
            return None
        number, match = next(iter(matches.items()))
        body = match.get("body")
        if type(number) is not int or number < 1 or not isinstance(body, str):
            raise ProductionWorkspaceError(
                "workspace pull request lookup is invalid"
            )
        try:
            base_commit = workspace_pull_request_base_commit(body)
        except ValueError as error:
            raise ProductionWorkspaceError(
                "workspace pull request base is invalid"
            ) from error
        return number, base_commit

    def _default_commit(self, root: Path, default_branch: str) -> str:
        try:
            raw = self._commands.run(
                (
                    "git",
                    "ls-remote",
                    "--heads",
                    "origin",
                    f"refs/heads/{default_branch}",
                ),
                cwd=root,
            ).stdout.strip()
        except CommandError as error:
            raise ProductionWorkspaceError(
                "workspace default commit is unavailable"
            ) from error
        fields = raw.split()
        if (
            len(fields) != 2
            or _COMMIT.fullmatch(fields[0]) is None
            or fields[1] != f"refs/heads/{default_branch}"
        ):
            raise ProductionWorkspaceError(
                "workspace default commit is invalid"
            )
        return fields[0].lower()

    def _json_object(
        self,
        command: Sequence[str],
        root: Path,
    ) -> dict[str, Any]:
        try:
            value = json.loads(
                self._commands.run(command, cwd=root).stdout
            )
        except json.JSONDecodeError as error:
            raise ProductionWorkspaceError(
                "workspace GitHub response is invalid"
            ) from error
        if not isinstance(value, dict):
            raise ProductionWorkspaceError(
                "workspace GitHub response is invalid"
            )
        return value

    def _json_list(
        self,
        command: Sequence[str],
        root: Path,
    ) -> list[dict[str, Any]]:
        try:
            value = json.loads(
                self._commands.run(command, cwd=root).stdout
            )
        except json.JSONDecodeError as error:
            raise ProductionWorkspaceError(
                "workspace GitHub response is invalid"
            ) from error
        if (
            not isinstance(value, list)
            or len(value) > 2
            or any(not isinstance(item, dict) for item in value)
        ):
            raise ProductionWorkspaceError(
                "workspace GitHub response is invalid"
            )
        return value


def _evaluation_policy(
    configured: Mapping[str, Any],
) -> EvaluationPolicy:
    return EvaluationPolicy(
        metrics=tuple(
            MetricPolicy(
                name=name,
                direction=(
                    MetricDirection.MAXIMIZE
                    if value.direction
                    is ConfigMetricDirection.MAXIMIZE
                    else MetricDirection.MINIMIZE
                ),
                threshold=value.threshold,
                materiality=value.materiality,
                hard_guardrail=value.hard_guardrail,
                undefined_behavior=(
                    UndefinedBehavior.FAIL
                    if value.undefined_behavior
                    is ConfigUndefinedBehavior.FAIL
                    else UndefinedBehavior.IGNORE
                ),
            )
            for name, value in configured.items()
        )
    )


def _trusted_candidates(
    issue_number: int,
    proposals: tuple[WorkspaceCandidateProposal, ...],
    records: tuple[WorkspaceExperimentRecord, ...],
) -> tuple[WorkspaceCandidate, ...]:
    by_id = {item.candidate_id: item for item in records}
    if len(by_id) != len(proposals) or set(by_id) != {
        item.candidate_id for item in proposals
    }:
        raise ProductionWorkspaceError(
            "workspace trusted experiment set is incomplete"
        )
    candidates: list[WorkspaceCandidate] = []
    for proposal in proposals:
        record = by_id[proposal.candidate_id]
        if (
            record.status != "completed"
            or record.patch_sha256 != proposal.patch_sha256
            or record.mutation_class != proposal.mutation_class
        ):
            raise ProductionWorkspaceError(
                "workspace proposal does not match trusted experiment"
            )
        request = CandidateExperimentRequest(
            issue_number=issue_number,
            candidate_id=record.candidate_id,
            patch_sha256=record.patch_sha256,
            bundle_sha256=record.bundle_sha256,
            evidence_sha256=record.evidence_sha256,
            idempotency_key=record.idempotency_key,
        )
        result = CandidateExperimentResult(
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
        candidates.append(
            WorkspaceCandidate(
                experiment=request,
                experiment_result=result,
                exact_patch=proposal.exact_patch,
                summary=proposal.summary,
                changed_paths=record.changed_paths,
                validation=record.validation,
                expected_tree=record.expected_tree,
            )
        )
    return tuple(candidates)


def _configured_candidate_limit(
    config: OptimizerConfig,
    target_name: str,
) -> int:
    target = config.targets.get(target_name)
    if target is None:
        raise ProductionWorkspaceError(
            "workspace candidate target is not configured"
        )
    if (
        target.campaign_overrides is not None
        and target.campaign_overrides.max_changed_candidates is not None
    ):
        return target.campaign_overrides.max_changed_candidates
    return config.campaign.max_changed_candidates


def _copilot_assignment_action(
    snapshot: WorkspaceSnapshot,
) -> tuple[str, bool]:
    if snapshot.phase is WorkspacePhase.SPECIFICATION:
        if snapshot.specification is None:
            return "resolve_trusted_specification", False
        if snapshot.specification.status == "human_review_required":
            return "review_specification", False
        if snapshot.baseline is None:
            return "establish_baseline", False
        if snapshot.baseline.status == "pending":
            return "await_trusted_actions_result", False
        return "design_candidates", True
    if snapshot.phase is WorkspacePhase.EVALUATING:
        if any(
            experiment.status == "pending"
            for experiment in snapshot.experiments
        ):
            return "await_trusted_actions_result", False
        return "run_candidate_experiments", True
    return {
        WorkspacePhase.AWAITING_SELECTION: (
            "merge_workspace_pull_request",
            False,
        ),
        WorkspacePhase.DEPLOYMENT: (
            "deploy_selected_candidate",
            False,
        ),
        WorkspacePhase.RETENTION: ("complete_retention", False),
        WorkspacePhase.COMPLETED: ("none", False),
    }[snapshot.phase]


def _load_workspace_snapshot(
    root: Path,
    issue_number: int,
) -> WorkspaceSnapshot | None:
    try:
        return GitWorkspaceStore(root).load(issue_number)
    except ValueError:
        return None


def _trusted_intake_gate(
    snapshot: WorkspaceSnapshot,
    pull_request: WorkspacePullRequest | None,
) -> WorkspaceResult | None:
    specification = snapshot.specification
    if specification is None:
        return None
    if specification.status == "human_review_required":
        kind = WorkspaceNextActionKind.REVIEW_SPECIFICATION
    elif snapshot.baseline is None:
        kind = WorkspaceNextActionKind.ESTABLISH_BASELINE
    elif snapshot.baseline.status == "pending":
        kind = WorkspaceNextActionKind.AWAIT_TRUSTED_ACTIONS_RESULT
    else:
        return None
    pull_request_number = snapshot.workspace_pull_request_number
    if pull_request_number is None:
        raise ProductionWorkspaceError(
            "trusted workspace gate requires its pull request"
        )
    if pull_request is None:
        pull_request = WorkspacePullRequest(
            number=pull_request_number,
            issue_number=snapshot.issue_number,
            branch=(
                f"foundry-opt/workspace/issue-{snapshot.issue_number}"
            ),
            title=(
                f"[Optimize] #{snapshot.issue_number} workspace - "
                "draft, not yet selectable"
            ),
            draft=True,
            reuse_existing=True,
            base_commit=specification.base_commit,
        )
    return WorkspaceResult(
        phase=snapshot.phase,
        workspace_pull_request=pull_request,
        planned_effect_kinds=(),
        recorded=True,
        issue_status_projection_intent=WorkspaceIssueStatusProjectionIntent(
            issue_number=snapshot.issue_number,
            phase=snapshot.phase,
            workspace_pull_request_number=pull_request_number,
        ),
        next_action=WorkspaceNextAction(
            kind=kind,
            issue_number=snapshot.issue_number,
            workspace_pull_request_number=pull_request_number,
            trigger=None,
        ),
    )
