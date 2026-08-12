from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse

from foundry_opt.adapters.commands import CommandError
from foundry_opt.adapters.github import github_repository_from_remote_url
from foundry_opt.orchestration.workspace import WorkspacePullRequest
from foundry_opt.preflight.interfaces import CommandRunner


_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
)
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
_COMMIT = re.compile(r"^[0-9a-fA-F]{40}$")
_PULL_REQUEST_FIELDS = (
    "number,headRefName,baseRefName,title,body,isDraft,state"
)


class GitHubWorkspacePullRequestError(RuntimeError):
    pass


def workspace_pull_request_marker(issue_number: int) -> str:
    if type(issue_number) is not int or issue_number < 1:
        raise ValueError("workspace issue number must be positive")
    return (
        "<!-- foundry-opt:workspace-pr:"
        f"issue-{issue_number}:v1 -->"
    )


def _workspace_pull_request_search(issue_number: int) -> str:
    workspace_pull_request_marker(issue_number)
    return (
        f'"foundry-opt:workspace-pr:issue-{issue_number}:v1" '
        "in:body"
    )


def _workspace_pull_request_body(issue_number: int) -> str:
    return (
        f"{workspace_pull_request_marker(issue_number)}\n\n"
        f"Persistent optimization workspace for issue #{issue_number}.\n"
    )


class GhWorkspacePullRequests:
    """Synchronize the single draft PR for an optimization workspace.

    Matching closed or merged pull requests fail closed and are not
    recreated while lifecycle policy for those states remains undefined.
    """

    def __init__(
        self,
        commands: CommandRunner,
        *,
        repository: str,
        base_branch: str,
    ) -> None:
        if _REPOSITORY.fullmatch(repository) is None:
            raise ValueError("workspace repository is invalid")
        if (
            _BRANCH.fullmatch(base_branch) is None
            or ".." in base_branch
            or "//" in base_branch
            or base_branch.endswith("/")
        ):
            raise ValueError("workspace base branch is invalid")
        self._commands = commands
        self._repository = repository
        self._base_branch = base_branch

    def synchronize(
        self,
        repository_root: Path,
        pull_request: WorkspacePullRequest,
    ) -> WorkspacePullRequest:
        self._validate_requested(pull_request)
        matches = self._find_pull_requests(
            repository_root,
            pull_request,
        )
        if matches:
            return self._reuse_existing(
                repository_root,
                pull_request,
                matches[0],
            )
        if pull_request.number is not None:
            raise GitHubWorkspacePullRequestError(
                "workspace pull request was not found"
            )
        self._ensure_branch(repository_root, pull_request)
        matches = self._find_pull_requests(
            repository_root,
            pull_request,
        )
        if matches:
            return self._reuse_existing(
                repository_root,
                pull_request,
                matches[0],
            )
        number = self._create_pull_request(
            repository_root,
            pull_request,
        )
        return replace(pull_request, number=number)

    def _reuse_existing(
        self,
        repository_root: Path,
        pull_request: WorkspacePullRequest,
        existing: dict[str, Any],
    ) -> WorkspacePullRequest:
        number = self._validate_existing(
            existing,
            pull_request,
        )
        if existing["title"] != pull_request.title:
            self._commands.run(
                (
                    "gh",
                    "pr",
                    "edit",
                    str(number),
                    "--repo",
                    self._repository,
                    "--title",
                    pull_request.title,
                ),
                cwd=repository_root,
            )
        return replace(pull_request, number=number)

    def _find_pull_requests(
        self,
        repository_root: Path,
        pull_request: WorkspacePullRequest,
    ) -> list[dict[str, Any]]:
        commands = (
            (
                "gh",
                "pr",
                "list",
                "--repo",
                self._repository,
                "--state",
                "all",
                "--head",
                pull_request.branch,
                "--json",
                _PULL_REQUEST_FIELDS,
                "--limit",
                "2",
            ),
            (
                "gh",
                "pr",
                "list",
                "--repo",
                self._repository,
                "--state",
                "all",
                "--search",
                _workspace_pull_request_search(
                    pull_request.issue_number
                ),
                "--json",
                _PULL_REQUEST_FIELDS,
                "--limit",
                "2",
            ),
        )
        matches: dict[int, dict[str, Any]] = {}
        for command in commands:
            value = self._json_list(
                command,
                repository_root,
            )
            for item in value:
                number = item.get("number")
                if type(number) is not int or number < 1:
                    raise GitHubWorkspacePullRequestError(
                        "workspace pull request response is invalid"
                    )
                previous = matches.get(number)
                if previous is not None and previous != item:
                    raise GitHubWorkspacePullRequestError(
                        "workspace pull request response is inconsistent"
                    )
                matches[number] = item
        if len(matches) > 1:
            raise GitHubWorkspacePullRequestError(
                "multiple workspace pull requests found"
            )
        return list(matches.values())

    def _ensure_branch(
        self,
        repository_root: Path,
        pull_request: WorkspacePullRequest,
    ) -> None:
        remote_name = self._verified_push_remote(repository_root)
        branch_ref = f"refs/heads/{pull_request.branch}"
        remote = self._commands.run(
            (
                "git",
                "ls-remote",
                "--heads",
                remote_name,
                branch_ref,
            ),
            cwd=repository_root,
        ).stdout.strip()
        if remote:
            fields = remote.split()
            if (
                len(fields) != 2
                or fields[1] != branch_ref
                or fields[0].casefold()
                != pull_request.base_commit.casefold()
            ):
                raise GitHubWorkspacePullRequestError(
                    "workspace branch does not match base commit"
                )
            return
        self._commands.run(
            (
                "git",
                "push",
                f"--force-with-lease={branch_ref}:",
                remote_name,
                f"{pull_request.base_commit}:{branch_ref}",
            ),
            cwd=repository_root,
        )

    def _verified_push_remote(
        self,
        repository_root: Path,
    ) -> str:
        remote_name = "origin"
        try:
            configured = self._commands.run(
                (
                    "git",
                    "config",
                    "--get-all",
                    f"remote.{remote_name}.url",
                ),
                cwd=repository_root,
            ).stdout.splitlines()
            push_urls = self._commands.run(
                (
                    "git",
                    "remote",
                    "get-url",
                    "--push",
                    "--all",
                    remote_name,
                ),
                cwd=repository_root,
            ).stdout.splitlines()
        except CommandError as error:
            raise GitHubWorkspacePullRequestError(
                "workspace push remote is unavailable"
            ) from error
        if (
            len(configured) != 1
            or push_urls != configured
            or (
                github_repository_from_remote_url(configured[0])
                or ""
            ).casefold()
            != self._repository.casefold()
        ):
            raise GitHubWorkspacePullRequestError(
                "workspace push remote does not match repository"
            )
        return remote_name

    def _create_pull_request(
        self,
        repository_root: Path,
        pull_request: WorkspacePullRequest,
    ) -> int:
        result = self._commands.run(
            (
                "gh",
                "pr",
                "create",
                "--repo",
                self._repository,
                "--draft",
                "--base",
                self._base_branch,
                "--head",
                pull_request.branch,
                "--title",
                pull_request.title,
                "--body-file",
                "-",
            ),
            cwd=repository_root,
            input_text=_workspace_pull_request_body(
                pull_request.issue_number
            ),
        )
        return self._pull_request_number(result.stdout)

    def _json_list(
        self,
        command: tuple[str, ...],
        repository_root: Path,
    ) -> list[dict[str, Any]]:
        raw = self._commands.run(
            command,
            cwd=repository_root,
        ).stdout
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise GitHubWorkspacePullRequestError(
                "workspace pull request response is invalid"
            ) from error
        if (
            not isinstance(value, list)
            or len(value) > 2
            or any(not isinstance(item, dict) for item in value)
        ):
            raise GitHubWorkspacePullRequestError(
                "workspace pull request response is invalid"
            )
        return value

    def _pull_request_number(self, raw_url: str) -> int:
        parsed = urlparse(raw_url.strip())
        expected_prefix = (
            f"/{self._repository}/pull/"
        )
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or not parsed.path.startswith(expected_prefix)
        ):
            raise GitHubWorkspacePullRequestError(
                "created workspace pull request URL is invalid"
            )
        suffix = parsed.path.removeprefix(expected_prefix).strip("/")
        if not suffix.isdigit() or int(suffix) < 1:
            raise GitHubWorkspacePullRequestError(
                "created workspace pull request URL is invalid"
            )
        return int(suffix)

    def _validate_existing(
        self,
        value: dict[str, Any],
        pull_request: WorkspacePullRequest,
    ) -> int:
        number = value.get("number")
        body = value.get("body")
        if (
            type(number) is not int
            or number < 1
            or value.get("headRefName") != pull_request.branch
            or value.get("baseRefName") != self._base_branch
            or not isinstance(value.get("title"), str)
            or not isinstance(body, str)
            or workspace_pull_request_marker(
                pull_request.issue_number
            )
            not in body
            or value.get("isDraft") is not True
            or value.get("state") != "OPEN"
            or (
                pull_request.number is not None
                and number != pull_request.number
            )
        ):
            raise GitHubWorkspacePullRequestError(
                "workspace pull request does not match"
            )
        return number

    def _validate_requested(
        self,
        pull_request: WorkspacePullRequest,
    ) -> None:
        expected_branch = (
            "foundry-opt/workspace/"
            f"issue-{pull_request.issue_number}"
        )
        if (
            type(pull_request.issue_number) is not int
            or pull_request.issue_number < 1
            or pull_request.branch != expected_branch
            or _BRANCH.fullmatch(pull_request.branch) is None
            or _COMMIT.fullmatch(pull_request.base_commit) is None
            or pull_request.draft is not True
            or pull_request.reuse_existing is not True
            or (
                pull_request.number is not None
                and (
                    type(pull_request.number) is not int
                    or pull_request.number < 1
                )
            )
        ):
            raise ValueError("workspace pull request is invalid")
