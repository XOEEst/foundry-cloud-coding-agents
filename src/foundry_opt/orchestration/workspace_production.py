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
from foundry_opt.orchestration.workspace import (
    OptimizationWorkspace,
    WorkspaceIssue,
    WorkspacePullRequest,
    WorkspaceRequest,
    WorkspaceResult,
    WorkspaceTrigger,
)
from foundry_opt.orchestration.workspace_github import (
    GhWorkspacePullRequests,
    workspace_pull_request_base_commit,
)
from foundry_opt.orchestration.workspace_git_store import GitWorkspaceStore
from foundry_opt.orchestration.workspace_intake import (
    NormalizedWorkspaceEvent,
    TrustedWorkspaceEventContext,
    normalize_workspace_event,
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
class _RepositoryContext:
    repository: str
    default_branch: str


WorkspaceFactory = Callable[..., OptimizationWorkspace]


def build_production_workspace(
    repository_root: Path,
    *,
    repository: str,
    base_branch: str,
    commands: CommandRunner | None = None,
) -> OptimizationWorkspace:
    runner = commands or SubprocessCommandRunner()
    return OptimizationWorkspace(
        store=GitWorkspaceStore(repository_root),
        pull_requests=GhWorkspacePullRequests(
            runner,
            repository=repository,
            base_branch=base_branch,
        ),
    )


def build_production_workspace_service() -> ProductionWorkspaceService:
    return ProductionWorkspaceService()


class ProductionWorkspaceService:
    def __init__(
        self,
        *,
        commands: CommandRunner | None = None,
        workspace_factory: WorkspaceFactory = build_production_workspace,
    ) -> None:
        self._commands = commands or SubprocessCommandRunner()
        self._workspace_factory = workspace_factory

    def advance(self, request: WorkspaceAdvanceRequest) -> WorkspaceResult:
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
            )
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
