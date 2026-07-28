"""Production GitHub adapter for issue-driven optimization specifications.

Every external call goes through :class:`~foundry_opt.preflight.interfaces.
CommandRunner`, which always executes ``gh``/``git`` as an argument list
(``shell=False``); untrusted issue content is therefore never interpolated
into a shell string. Long or attacker-controlled bodies are passed to the
child process through ``--body-file -`` (stdin) rather than as a single
command-line argument. Every GitHub response is parsed and shape-checked
before use, and command failures are reported without the raw stdout/stderr
(which may otherwise echo untrusted issue content back into logs).

:class:`GitSpecPublisher` is the dedicated "safe change-set/commit
publisher" seam: it assembles a commit purely through Git plumbing
(``read-tree``/``hash-object``/``write-tree``/``commit-tree``) against a
temporary index file, so it never touches HEAD, the real index, or the
working tree, then pushes the resulting commit object directly and opens a
draft pull request.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse
from uuid import uuid4

from foundry_opt.adapters.commands import CommandError
from foundry_opt.adapters.github import github_repository_from_remote_url
from foundry_opt.github_workflow.models import (
    GitHubCapabilities,
    GitHubPermissionReport,
    IssueReference,
    PullRequestReference,
    RepositoryState,
    git_branch,
    repository_path,
)
from foundry_opt.optimization.specification import (
    SpecBranchConflictError,
    spec_issue_marker,
)
from foundry_opt.preflight.interfaces import CommandRunner


class GhOptimizationGatewayError(RuntimeError):
    code = "github_optimization_gateway_failed"

    def __init__(self, operation: str) -> None:
        self.operation = operation
        super().__init__(f"GitHub optimization operation failed: {operation}")


class GhOptimizationResponseError(GhOptimizationGatewayError):
    code = "github_optimization_response_invalid"


class GitSpecPublisherError(RuntimeError):
    pass


# The spec commit must be byte-for-byte reproducible so that re-running spec
# preparation for identical inputs always yields the identical commit SHA
# (required for exact pull-request idempotency by head-commit comparison).
# ``git commit-tree`` otherwise stamps author/committer name, email, and the
# current wall-clock time from ambient config, which would make every run
# produce a different commit even when the tree and parent are unchanged.
_COMMIT_IDENTITY_ENV: Mapping[str, str] = {
    "GIT_AUTHOR_NAME": "foundry-opt-bot",
    "GIT_AUTHOR_EMAIL": "foundry-opt-bot@users.noreply.github.com",
    "GIT_AUTHOR_DATE": "1970-01-01T00:00:00+0000",
    "GIT_COMMITTER_NAME": "foundry-opt-bot",
    "GIT_COMMITTER_EMAIL": "foundry-opt-bot@users.noreply.github.com",
    "GIT_COMMITTER_DATE": "1970-01-01T00:00:00+0000",
}


class GhOptimizationGateway:
    """Reads issues/pull requests and writes issue comments and labels."""

    def __init__(
        self,
        command_runner: CommandRunner,
        *,
        granted_capabilities: GitHubCapabilities,
    ) -> None:
        self._commands = command_runner
        self._repository: str | None = None
        if not isinstance(granted_capabilities, GitHubCapabilities):
            raise ValueError("granted_capabilities must be explicit")
        self._granted_capabilities = granted_capabilities

    def verify_permissions(
        self,
        required: GitHubCapabilities,
    ) -> GitHubPermissionReport:
        return GitHubPermissionReport(self._granted_capabilities & required)

    def repository_state(self, repository_root: Path) -> RepositoryState:
        repository = self._repository_name(repository_root)
        default_branch = self._run(
            "repository_metadata",
            ("gh", "api", f"repos/{repository}", "--jq", ".default_branch"),
            cwd=repository_root,
        ).strip()
        if not default_branch:
            raise GhOptimizationResponseError("repository_metadata")
        commit = self._run(
            "default_commit",
            (
                "gh",
                "api",
                f"repos/{repository}/commits/{quote(default_branch, safe='')}",
                "--jq",
                ".sha",
            ),
            cwd=repository_root,
        ).strip()
        try:
            return RepositoryState(repository, default_branch, commit)
        except ValueError as error:
            raise GhOptimizationResponseError("default_commit") from error

    def get_issue(
        self,
        repository_root: Path,
        issue_number: int,
    ) -> IssueReference | None:
        repository = self._repository_name(repository_root)
        try:
            raw = self._run(
                "get_issue",
                ("gh", "api", f"repos/{repository}/issues/{issue_number}"),
                cwd=repository_root,
            )
        except GhOptimizationGatewayError:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise GhOptimizationResponseError("get_issue") from error
        if not isinstance(payload, dict) or "pull_request" in payload:
            # Never treat a pull request as an optimization issue request.
            return None
        try:
            number = int(payload["number"])
            url = str(payload["html_url"])
            title = str(payload["title"])
            body = str(payload.get("body") or "")
            state = str(payload["state"]).upper()
            labels_raw = payload.get("labels") or []
            labels = tuple(
                str(label["name"])
                for label in labels_raw
                if isinstance(label, dict) and "name" in label
            )
            if number != issue_number or len(labels) != len(labels_raw):
                raise ValueError("issue payload shape is invalid")
            return IssueReference(number, url, title, body, state, labels)
        except (KeyError, TypeError, ValueError) as error:
            raise GhOptimizationResponseError("get_issue") from error

    def find_spec_pull_request(
        self,
        repository_root: Path,
        issue_number: int,
    ) -> PullRequestReference | None:
        repository = self._repository_name(repository_root)
        marker = spec_issue_marker(issue_number)
        raw = self._run(
            "find_spec_pull_request",
            (
                "gh",
                "pr",
                "list",
                "--repo",
                repository,
                "--state",
                "all",
                "--search",
                f'"{marker}" in:body',
                "--json",
                (
                    "number,url,headRefName,headRefOid,isDraft,body,"
                    "baseRefName,state"
                ),
                "--limit",
                "5",
            ),
            cwd=repository_root,
        )
        items = _json_list(raw, "find_spec_pull_request")
        matches = [
            item
            for item in items
            if isinstance(item.get("body"), str) and marker in item["body"]
        ]
        if len(matches) > 1:
            raise GhOptimizationResponseError("find_spec_pull_request")
        return _pull_request_from_json(matches[0]) if matches else None

    def comment_issue(
        self,
        repository_root: Path,
        issue_number: int,
        body: str,
    ) -> None:
        repository = self._repository_name(repository_root)
        self._run(
            "comment_issue",
            (
                "gh",
                "issue",
                "comment",
                str(issue_number),
                "--repo",
                repository,
                "--body-file",
                "-",
            ),
            cwd=repository_root,
            input_text=body,
        )

    def has_issue_comment(
        self,
        repository_root: Path,
        issue_number: int,
        marker: str,
    ) -> bool:
        repository = self._repository_name(repository_root)
        raw = self._run(
            "list_issue_comments",
            (
                "gh",
                "api",
                f"repos/{repository}/issues/{issue_number}/comments",
                "--paginate",
                "--jq",
                ".[].body",
            ),
            cwd=repository_root,
        )
        return marker in raw

    def add_labels(
        self,
        repository_root: Path,
        issue_number: int,
        labels: tuple[str, ...],
    ) -> None:
        repository = self._repository_name(repository_root)
        arguments = [
            "gh",
            "issue",
            "edit",
            str(issue_number),
            "--repo",
            repository,
        ]
        for label in labels:
            arguments.extend(("--add-label", label))
        self._run("add_labels", tuple(arguments), cwd=repository_root)

    def remove_labels(
        self,
        repository_root: Path,
        issue_number: int,
        labels: tuple[str, ...],
    ) -> None:
        repository = self._repository_name(repository_root)
        arguments = [
            "gh",
            "issue",
            "edit",
            str(issue_number),
            "--repo",
            repository,
        ]
        for label in labels:
            arguments.extend(("--remove-label", label))
        self._run("remove_labels", tuple(arguments), cwd=repository_root)

    def _repository_name(self, repository_root: Path) -> str:
        if self._repository is not None:
            return self._repository
        remote = self._run(
            "origin",
            ("git", "remote", "get-url", "origin"),
            cwd=repository_root,
        ).strip()
        repository = github_repository_from_remote_url(remote)
        if repository is None:
            raise GhOptimizationResponseError("origin")
        self._repository = repository
        return repository

    def _run(
        self,
        operation: str,
        arguments: Sequence[str],
        *,
        cwd: Path,
        input_text: str | None = None,
    ) -> str:
        try:
            return self._commands.run(
                arguments,
                cwd=cwd,
                input_text=input_text,
            ).stdout
        except CommandError as error:
            raise GhOptimizationGatewayError(operation) from error


class GitSpecPublisher:
    """Builds and publishes a spec commit via Git plumbing, never checkout."""

    def __init__(self, command_runner: CommandRunner) -> None:
        self._commands = command_runner
        self._repository: str | None = None

    def prepare_commit(
        self,
        repository_root: Path,
        *,
        base_commit: str,
        files: Mapping[Path, bytes],
        message: str,
    ) -> str:
        if not files:
            raise ValueError("a spec commit requires at least one file")
        normalized = _validate_commit_paths(files)
        self._ensure_commit_available(repository_root, base_commit)
        index_path = self._temporary_index_path(repository_root)
        environment = {"GIT_INDEX_FILE": str(index_path)}
        try:
            self._run(
                ("git", "read-tree", base_commit),
                repository_root,
                environment=environment,
            )
            for path in normalized:
                blob = self._run(
                    ("git", "hash-object", "-w", "--stdin"),
                    repository_root,
                    environment=environment,
                    input_bytes=files[path],
                ).strip()
                self._run(
                    (
                        "git",
                        "update-index",
                        "--add",
                        "--cacheinfo",
                        "100644",
                        blob,
                        path.as_posix(),
                    ),
                    repository_root,
                    environment=environment,
                )
            tree = self._run(
                ("git", "write-tree"),
                repository_root,
                environment=environment,
            ).strip()
            commit_sha = self._run(
                (
                    "git",
                    "commit-tree",
                    tree,
                    "-p",
                    base_commit,
                    "-m",
                    message,
                ),
                repository_root,
                environment=_COMMIT_IDENTITY_ENV,
            ).strip()
        finally:
            _remove_temporary_index(index_path)
        return commit_sha

    def publish(
        self,
        repository_root: Path,
        *,
        base_branch: str,
        branch: str,
        commit_sha: str,
        title: str,
        body: str,
    ) -> PullRequestReference:
        git_branch(branch, "branch")
        branch_ref = f"refs/heads/{branch}"
        remote = self._run(
            ("git", "ls-remote", "--heads", "origin", branch_ref),
            repository_root,
        ).strip()
        if remote:
            fields = remote.split()
            if len(fields) != 2 or fields[1] != branch_ref:
                raise GitSpecPublisherError(
                    "remote branch metadata is invalid"
                )
            if fields[0] != commit_sha:
                raise SpecBranchConflictError(branch, fields[0])
        else:
            self._run(
                (
                    "git",
                    "push",
                    f"--force-with-lease={branch_ref}:",
                    "origin",
                    f"{commit_sha}:{branch_ref}",
                ),
                repository_root,
            )
        repository = self._repository_name(repository_root)
        url = self._run(
            (
                "gh",
                "pr",
                "create",
                "--repo",
                repository,
                "--draft",
                "--base",
                base_branch,
                "--head",
                branch,
                "--title",
                title,
                "--body-file",
                "-",
            ),
            repository_root,
            input_text=body,
        ).strip()
        number = _github_number(url, "pull", repository)
        return PullRequestReference(
            number,
            url,
            branch,
            commit_sha,
            True,
            body,
            base_branch,
            "OPEN",
        )

    def _ensure_commit_available(
        self,
        repository_root: Path,
        base_commit: str,
    ) -> None:
        try:
            self._run(
                ("git", "cat-file", "-e", f"{base_commit}^{{commit}}"),
                repository_root,
            )
            return
        except GitSpecPublisherError:
            pass
        self._run(
            ("git", "fetch", "--no-tags", "origin", base_commit),
            repository_root,
        )
        self._run(
            ("git", "cat-file", "-e", f"{base_commit}^{{commit}}"),
            repository_root,
        )

    def _temporary_index_path(self, repository_root: Path) -> Path:
        value = self._run(
            (
                "git",
                "rev-parse",
                "--git-path",
                f"foundry-opt-spec-index-{uuid4().hex}",
            ),
            repository_root,
        ).strip()
        path = Path(value)
        return path if path.is_absolute() else repository_root / path

    def _repository_name(self, repository_root: Path) -> str:
        if self._repository is not None:
            return self._repository
        remote = self._run(
            ("git", "remote", "get-url", "origin"),
            repository_root,
        ).strip()
        repository = github_repository_from_remote_url(remote)
        if repository is None:
            raise GitSpecPublisherError("origin remote is not a GitHub URL")
        self._repository = repository
        return repository

    def _run(
        self,
        arguments: Sequence[str],
        repository_root: Path,
        *,
        environment: Mapping[str, str] | None = None,
        input_text: str | None = None,
        input_bytes: bytes | None = None,
    ) -> str:
        try:
            return self._commands.run(
                arguments,
                cwd=repository_root,
                environment=environment,
                input_text=input_text,
                input_bytes=input_bytes,
            ).stdout
        except CommandError as error:
            operation = arguments[1] if len(arguments) > 1 else arguments[0]
            raise GitSpecPublisherError(
                f"git spec publisher command failed during {operation}"
            ) from error


def _validate_commit_paths(files: Mapping[Path, bytes]) -> tuple[Path, ...]:
    normalized: list[Path] = []
    portable_names: set[str] = set()
    for path in files:
        safe = repository_path(path, "spec file path")
        portable = safe.as_posix().casefold()
        if portable in portable_names:
            raise ValueError(f"generated path is not portable: {safe.as_posix()}")
        portable_names.add(portable)
        normalized.append(safe)
    return tuple(normalized)


def _remove_temporary_index(index_path: Path) -> None:
    for path in (index_path, Path(f"{index_path}.lock")):
        try:
            path.unlink()
        except FileNotFoundError:
            continue


def _json_list(raw: str, operation: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise GhOptimizationResponseError(operation) from error
    if not isinstance(value, list) or not all(
        isinstance(item, dict) for item in value
    ):
        raise GhOptimizationResponseError(operation)
    return value


def _pull_request_from_json(value: dict[str, Any]) -> PullRequestReference:
    try:
        return PullRequestReference(
            number=int(value["number"]),
            url=str(value["url"]),
            head_branch=str(value["headRefName"]),
            head_commit=str(value["headRefOid"]),
            draft=bool(value["isDraft"]),
            body=str(value.get("body") or ""),
            base_branch=str(value["baseRefName"]),
            state=str(value["state"]).upper(),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise GhOptimizationResponseError(
            "find_spec_pull_request"
        ) from error


def _github_number(url: str, kind: str, repository: str) -> int:
    try:
        parsed = urlparse(url)
        parts = tuple(part for part in parsed.path.split("/") if part)
        if (
            parsed.scheme != "https"
            or parsed.netloc.casefold() != "github.com"
            or parsed.query
            or parsed.fragment
            or len(parts) != 4
            or "/".join(parts[:2]).casefold() != repository.casefold()
            or parts[2] != kind
        ):
            raise ValueError("GitHub URL is invalid")
        return int(parts[3])
    except ValueError as error:
        raise GhOptimizationResponseError("create_spec_pull_request") from error
