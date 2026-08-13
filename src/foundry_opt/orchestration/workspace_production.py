from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
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
    WorkspaceCandidateProposal,
    WorkspaceIssue,
    WorkspaceOperation,
    WorkspacePhase,
    WorkspacePullRequest,
    WorkspaceReportContext,
    WorkspaceRequest,
    WorkspaceResult,
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
from foundry_opt.orchestration.workspace_verifier import (
    WorkspaceVerificationResult,
    WorkspaceVerifier,
)
from foundry_opt.orchestration.workspace_experiments import (
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
from foundry_opt.preflight.interfaces import CommandRunner
from foundry_opt.security import reject_secret_content


_COMMIT = re.compile(r"^[0-9a-fA-F]{40}$")
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
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


WorkspaceFactory = Callable[..., OptimizationWorkspace]
CopilotAssignerFactory = Callable[..., GhWorkspaceCopilotAssigner]


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
        pull_requests=GhWorkspacePullRequests(
            runner,
            repository=repository,
            base_branch=base_branch,
        ),
        candidate_coordinator=candidate_coordinator,
    )


def build_production_workspace_service() -> ProductionWorkspaceService:
    return ProductionWorkspaceService()


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
        copilot_assigner_factory: CopilotAssignerFactory = (
            GhWorkspaceCopilotAssigner
        ),
    ) -> None:
        self._commands = commands or SubprocessCommandRunner()
        self._workspace_factory = workspace_factory
        self._experiment_runner = experiment_runner
        self._experiment_request_builder = experiment_request_builder
        self._copilot_assigner_factory = copilot_assigner_factory

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
        workspace = self._workspace_factory(
            repository_root=root,
            repository=context.repository,
            base_branch=context.default_branch,
            commands=self._commands,
            candidate_count=request.candidate_count,
            selector=request.selector,
        )
        return workspace.advance(
            WorkspaceRequest(
                repository_root=root,
                issue=WorkspaceIssue(
                    number=request.issue_number,
                    title=issue["title"],
                    body=issue["body"],
                    base_commit=base_commit.lower(),
                ),
                trigger=request.trigger,
                workspace_pull_request=pull_request,
                candidates=request.candidates,
                report_context=request.report_context,
                operation=request.operation,
            )
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
        manifest = parse_workspace_experiment_manifest(
            payload,
            policy=policy,
        )
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
                report_context=manifest.report_context,
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
            or self._experiment_request_builder is None
        ):
            raise ProductionWorkspaceError(
                "workspace experiment executor is not configured"
            )
        return WorkspaceExperimentExecutor(
            store=GitWorkspaceStore(root),
            runner=self._experiment_runner,
            request_builder=self._experiment_request_builder,
        ).execute(
            repository_root=root,
            issue_number=manifest.issue_number,
            target=manifest.target,
            base_commit=manifest.base_commit,
            proposal=manifest.candidate,
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
            or record.idempotency_key != proposal.idempotency_key
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
                changed_paths=proposal.changed_paths,
                validation=proposal.validation,
                expected_tree=proposal.expected_tree,
            )
        )
    return tuple(candidates)


def _copilot_assignment_action(
    snapshot: WorkspaceSnapshot,
) -> tuple[str, bool]:
    if snapshot.phase is WorkspacePhase.SPECIFICATION:
        return "run_candidate_experiments", True
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
