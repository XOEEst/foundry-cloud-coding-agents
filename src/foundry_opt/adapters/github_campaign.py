from __future__ import annotations

from collections.abc import Sequence
import hashlib
import json
from pathlib import Path
import shlex
from typing import Any
from urllib.parse import quote, urlparse

from foundry_opt.adapters.commands import CommandError
from foundry_opt.adapters.github import github_repository_from_remote_url
from foundry_opt.github_workflow import (
    AppliedPatch,
    ArtifactInspection,
    CommitBlob,
    CommitInspection,
    ExactPatchRequest,
    GitHubCapabilities,
    GitHubPermissionReport,
    IssueReference,
    PatchApplicationError,
    PatchTreeMismatchError,
    PatchTraversalError,
    PullRequestReference,
    RepositoryState,
)
from foundry_opt.github_workflow.models import repository_path
from foundry_opt.preflight.interfaces import CommandRunner


class GitHubCampaignGatewayError(RuntimeError):
    code = "github_campaign_gateway_failed"

    def __init__(
        self,
        operation: str,
        *,
        remote_branch: str | None = None,
        remote_commit: str | None = None,
        resumable: bool = False,
    ) -> None:
        self.operation = operation
        self.remote_branch = remote_branch
        self.remote_commit = remote_commit
        self.resumable = resumable
        super().__init__(f"GitHub campaign operation failed: {operation}")


class GitHubCampaignResponseError(GitHubCampaignGatewayError):
    code = "github_campaign_response_invalid"


class GhCampaignGateway:
    def __init__(
        self,
        command_runner: CommandRunner,
        repository_root: Path,
        *,
        granted_capabilities: GitHubCapabilities | None = None,
    ) -> None:
        self._commands = command_runner
        self._repository_root = repository_root
        self._repository: str | None = None
        self._metadata: dict[str, Any] | None = None
        if not isinstance(granted_capabilities, GitHubCapabilities):
            raise ValueError("granted_capabilities must be explicit")
        self._granted_capabilities = granted_capabilities

    def verify_permissions(
        self,
        required: GitHubCapabilities,
    ) -> GitHubPermissionReport:
        metadata = self._repository_metadata()
        if not metadata:
            raise GitHubCampaignResponseError("repository_metadata")
        return GitHubPermissionReport(
            self._granted_capabilities & required
        )

    def repository_state(self, repository_root: Path) -> RepositoryState:
        metadata = self._repository_metadata()
        default_branch = metadata.get("default_branch")
        if not isinstance(default_branch, str) or not default_branch:
            raise GitHubCampaignResponseError("repository_metadata")
        repository = self._repository_name()
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
            raise GitHubCampaignResponseError("default_commit") from error

    def inspect_commit(
        self,
        repository_root: Path,
        *,
        base_commit: str,
        head_commit: str,
        artifact_paths: tuple[Path, ...],
    ) -> CommitInspection:
        try:
            merge_base = self._commands.run(
                ("git", "merge-base", base_commit, head_commit),
                cwd=repository_root,
            ).stdout.strip()
        except CommandError:
            merge_base = ""
        changed_raw = self._run(
            "inspect_commit_paths",
            (
                "git",
                "diff",
                "--name-only",
                "-z",
                base_commit,
                head_commit,
            ),
            cwd=repository_root,
        )
        changed_paths = tuple(
            Path(path)
            for path in changed_raw.split("\0")
            if path
        )
        blobs: list[CommitBlob] = []
        for path in artifact_paths:
            content = self._run(
                "inspect_commit_blob",
                (
                    "git",
                    "show",
                    f"{head_commit}:{path.as_posix()}",
                ),
                cwd=repository_root,
            ).encode("utf-8")
            blobs.append(CommitBlob(path, content))
        return CommitInspection(
            base_commit=base_commit,
            head_commit=head_commit,
            base_is_ancestor=merge_base == base_commit,
            changed_paths=changed_paths,
            blobs=tuple(blobs),
        )

    def artifact_url(
        self,
        repository: str,
        commit: str,
        path: Path,
    ) -> str:
        if repository.casefold() != self._repository_name().casefold():
            raise GitHubCampaignResponseError("artifact_url")
        safe_path = repository_path(path, "artifact path").as_posix()
        self._run(
            "verify_artifact",
            ("git", "cat-file", "-e", f"{commit}:{safe_path}"),
            cwd=self._repository_root,
        )
        encoded = "/".join(quote(part, safe="") for part in safe_path.split("/"))
        return f"https://github.com/{repository}/blob/{commit}/{encoded}"

    def find_campaign_pull_request(
        self,
        repository_root: Path,
        head_branch: str,
    ) -> PullRequestReference | None:
        return self._find_pull_request(repository_root, head_branch)

    def find_candidate_pull_request(
        self,
        repository_root: Path,
        head_branch: str,
    ) -> PullRequestReference | None:
        return self._find_pull_request(repository_root, head_branch)

    def create_campaign_pull_request(
        self,
        repository_root: Path,
        *,
        base_branch: str,
        head_branch: str,
        head_commit: str,
        title: str,
        body: str,
    ) -> PullRequestReference:
        self._push_branch(
            repository_root,
            branch=head_branch,
            commit=head_commit,
        )
        try:
            url = self._run(
                "create_campaign_pr",
                (
                    "gh",
                    "pr",
                    "create",
                    "--repo",
                    self._repository_name(),
                    "--draft",
                    "--base",
                    base_branch,
                    "--head",
                    head_branch,
                    "--title",
                    title,
                    "--body",
                    body,
                ),
                cwd=repository_root,
            ).strip()
        except GitHubCampaignGatewayError as error:
            raise GitHubCampaignGatewayError(
                error.operation,
                remote_branch=head_branch,
                remote_commit=head_commit,
                resumable=True,
            ) from error
        return _created_pull_request(
            url,
            repository=self._repository_name(),
            base_branch=base_branch,
            head_branch=head_branch,
            head_commit=head_commit,
            draft=True,
            body=body,
        )

    def create_candidate_pull_request(
        self,
        repository_root: Path,
        *,
        base_branch: str,
        head_branch: str,
        commit_sha: str,
        title: str,
        body: str,
    ) -> PullRequestReference:
        self._push_branch(
            repository_root,
            branch=head_branch,
            commit=commit_sha,
        )
        try:
            url = self._run(
                "create_candidate_pr",
                (
                    "gh",
                    "pr",
                    "create",
                    "--repo",
                    self._repository_name(),
                    "--base",
                    base_branch,
                    "--head",
                    head_branch,
                    "--title",
                    title,
                    "--body",
                    body,
                ),
                cwd=repository_root,
            ).strip()
        except GitHubCampaignGatewayError as error:
            raise GitHubCampaignGatewayError(
                error.operation,
                remote_branch=head_branch,
                remote_commit=commit_sha,
                resumable=True,
            ) from error
        return _created_pull_request(
            url,
            repository=self._repository_name(),
            base_branch=base_branch,
            head_branch=head_branch,
            head_commit=commit_sha,
            draft=False,
            body=body,
        )

    def find_candidate_issue(
        self,
        repository_root: Path,
        marker: str,
    ) -> IssueReference | None:
        raw = self._run(
            "find_candidate_issue",
            (
                "gh",
                "issue",
                "list",
                "--repo",
                self._repository_name(),
                "--state",
                "all",
                "--search",
                f'"{marker}" in:body',
                "--json",
                "number,url,title,body,state,labels",
                "--limit",
                "20",
            ),
            cwd=repository_root,
        )
        items = _json_list(raw, "find_candidate_issue")
        matches = [
            _issue_from_json(item)
            for item in items
            if isinstance(item.get("body"), str)
            and marker in item["body"]
        ]
        if len(matches) > 1:
            raise GitHubCampaignResponseError("find_candidate_issue")
        return matches[0] if matches else None

    def create_issue(
        self,
        repository_root: Path,
        *,
        title: str,
        body: str,
    ) -> IssueReference:
        url = self._run(
            "create_issue",
            (
                "gh",
                "issue",
                "create",
                "--repo",
                self._repository_name(),
                "--title",
                title,
                "--body",
                body,
            ),
            cwd=repository_root,
        ).strip()
        number = _github_number(
            url,
            "issues",
            "create_issue",
            self._repository_name(),
        )
        return IssueReference(number, url, title, body)

    def add_labels(
        self,
        repository_root: Path,
        issue_number: int,
        labels: tuple[str, ...],
    ) -> None:
        arguments = [
            "gh",
            "issue",
            "edit",
            str(issue_number),
            "--repo",
            self._repository_name(),
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
        arguments = [
            "gh",
            "issue",
            "edit",
            str(issue_number),
            "--repo",
            self._repository_name(),
        ]
        for label in labels:
            arguments.extend(("--remove-label", label))
        self._run("remove_labels", tuple(arguments), cwd=repository_root)

    def reopen_issue(
        self,
        repository_root: Path,
        issue_number: int,
    ) -> None:
        self._run(
            "reopen_issue",
            (
                "gh",
                "issue",
                "reopen",
                str(issue_number),
                "--repo",
                self._repository_name(),
            ),
            cwd=repository_root,
        )

    def is_sub_issue(
        self,
        repository_root: Path,
        parent_number: int,
        child_number: int,
    ) -> bool:
        raw = self._run(
            "list_sub_issues",
            (
                "gh",
                "api",
                (
                    f"repos/{self._repository_name()}/issues/"
                    f"{parent_number}/sub_issues"
                ),
                "--paginate",
                "--jq",
                ".[].number",
            ),
            cwd=repository_root,
        )
        return str(child_number) in raw.splitlines()

    def link_sub_issue(
        self,
        repository_root: Path,
        parent_number: int,
        child_number: int,
    ) -> None:
        issue_id = self._issue_database_id(repository_root, child_number)
        self._run(
            "link_sub_issue",
            (
                "gh",
                "api",
                "--method",
                "POST",
                (
                    f"repos/{self._repository_name()}/issues/"
                    f"{parent_number}/sub_issues"
                ),
                "-F",
                f"sub_issue_id={issue_id}",
            ),
            cwd=repository_root,
        )

    def add_dependency(
        self,
        repository_root: Path,
        issue_number: int,
        blocker_number: int,
    ) -> None:
        blocker_id = self._issue_database_id(repository_root, blocker_number)
        self._run(
            "add_dependency",
            (
                "gh",
                "api",
                "--method",
                "POST",
                (
                    f"repos/{self._repository_name()}/issues/"
                    f"{issue_number}/dependencies/blocked_by"
                ),
                "-F",
                f"issue_id={blocker_id}",
            ),
            cwd=repository_root,
        )

    def update_issue_body(
        self,
        repository_root: Path,
        issue_number: int,
        body: str,
    ) -> None:
        self._run(
            "update_issue_body",
            (
                "gh",
                "issue",
                "edit",
                str(issue_number),
                "--repo",
                self._repository_name(),
                "--body",
                body,
            ),
            cwd=repository_root,
        )

    def update_pull_request_body(
        self,
        repository_root: Path,
        pull_request_number: int,
        body: str,
    ) -> None:
        self._run(
            "update_pr_body",
            (
                "gh",
                "pr",
                "edit",
                str(pull_request_number),
                "--repo",
                self._repository_name(),
                "--body",
                body,
            ),
            cwd=repository_root,
        )

    def comment_issue(
        self,
        repository_root: Path,
        issue_number: int,
        body: str,
    ) -> None:
        self._run(
            "comment_issue",
            (
                "gh",
                "issue",
                "comment",
                str(issue_number),
                "--repo",
                self._repository_name(),
                "--body",
                body,
            ),
            cwd=repository_root,
        )

    def close_issue(
        self,
        repository_root: Path,
        issue_number: int,
        comment: str,
    ) -> None:
        self._run(
            "close_issue",
            (
                "gh",
                "issue",
                "close",
                str(issue_number),
                "--repo",
                self._repository_name(),
                "--comment",
                comment,
            ),
            cwd=repository_root,
        )

    def close_pull_request(
        self,
        repository_root: Path,
        pull_request_number: int,
        comment: str,
    ) -> None:
        self._run(
            "close_pull_request",
            (
                "gh",
                "pr",
                "close",
                str(pull_request_number),
                "--repo",
                self._repository_name(),
                "--comment",
                comment,
            ),
            cwd=repository_root,
        )

    def _find_pull_request(
        self,
        repository_root: Path,
        head_branch: str,
    ) -> PullRequestReference | None:
        raw = self._run(
            "find_pull_request",
            (
                "gh",
                "pr",
                "list",
                "--repo",
                self._repository_name(),
                "--state",
                "open",
                "--head",
                head_branch,
                "--json",
                (
                    "number,url,headRefName,headRefOid,isDraft,body,"
                    "baseRefName,state"
                ),
                "--limit",
                "2",
            ),
            cwd=repository_root,
        )
        items = _json_list(raw, "find_pull_request")
        if len(items) > 1:
            raise GitHubCampaignResponseError("find_pull_request")
        return _pull_request_from_json(items[0]) if items else None

    def _push_branch(
        self,
        repository_root: Path,
        *,
        branch: str,
        commit: str,
    ) -> None:
        branch_ref = f"refs/heads/{branch}"
        remote = self._run(
            "inspect_remote_branch",
            (
                "git",
                "ls-remote",
                "--heads",
                "origin",
                branch_ref,
            ),
            cwd=repository_root,
        ).strip()
        if remote:
            fields = remote.split()
            if len(fields) != 2 or fields[1] != branch_ref:
                raise GitHubCampaignResponseError(
                    "inspect_remote_branch"
                )
            if fields[0] == commit:
                return
            raise GitHubCampaignGatewayError(
                "remote_branch_conflict",
                remote_branch=branch,
                remote_commit=fields[0],
                resumable=False,
            )
        self._run(
            "push_branch",
            (
                "git",
                "push",
                f"--force-with-lease={branch_ref}:",
                "origin",
                f"{commit}:{branch_ref}",
            ),
            cwd=repository_root,
        )

    def _issue_database_id(
        self,
        repository_root: Path,
        issue_number: int,
    ) -> str:
        value = self._run(
            "issue_database_id",
            (
                "gh",
                "api",
                (
                    f"repos/{self._repository_name()}/issues/"
                    f"{issue_number}"
                ),
                "--jq",
                ".id",
            ),
            cwd=repository_root,
        ).strip()
        if not value.isdigit():
            raise GitHubCampaignResponseError("issue_database_id")
        return value

    def _repository_name(self) -> str:
        if self._repository is not None:
            return self._repository
        remote = self._run(
            "origin",
            ("git", "remote", "get-url", "origin"),
            cwd=self._repository_root,
        ).strip()
        repository = github_repository_from_remote_url(remote)
        if repository is None:
            raise GitHubCampaignResponseError("origin")
        self._repository = repository
        return repository

    def _repository_metadata(self) -> dict[str, Any]:
        if self._metadata is not None:
            return self._metadata
        raw = self._run(
            "repository_metadata",
            ("gh", "api", f"repos/{self._repository_name()}"),
            cwd=self._repository_root,
        )
        try:
            metadata = json.loads(raw)
        except json.JSONDecodeError as error:
            raise GitHubCampaignResponseError(
                "repository_metadata"
            ) from error
        if (
            not isinstance(metadata, dict)
            or metadata.get("full_name", "").casefold()
            != self._repository_name().casefold()
        ):
            raise GitHubCampaignResponseError("repository_metadata")
        self._metadata = metadata
        return metadata

    def _run(
        self,
        operation: str,
        arguments: Sequence[str],
        *,
        cwd: Path,
    ) -> str:
        try:
            return self._commands.run(arguments, cwd=cwd).stdout
        except CommandError as error:
            raise GitHubCampaignGatewayError(operation) from error


class GitExactPatchApplier:
    def __init__(self, command_runner: CommandRunner) -> None:
        self._commands = command_runner

    def inspect_artifact(
        self,
        repository_root: Path,
        path: Path,
    ) -> ArtifactInspection:
        safe_path = repository_path(path, "artifact path")
        root = repository_root.resolve(strict=True)
        unresolved = root / safe_path
        current = root
        for part in safe_path.parts:
            current /= part
            if current.is_symlink():
                raise ValueError(
                    "artifact path cannot contain a symbolic link"
                )
        candidate = unresolved.resolve(strict=True)
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise ValueError(
                "artifact path escapes the repository"
            ) from error
        if not candidate.is_file():
            raise ValueError("artifact must be a regular repository file")
        content = candidate.read_bytes()
        return ArtifactInspection(
            path=safe_path,
            sha256=hashlib.sha256(content).hexdigest(),
            byte_count=len(content),
            content=content,
        )

    def apply_exact(self, request: ExactPatchRequest) -> AppliedPatch:
        inspection = self.inspect_artifact(
            request.repository_root,
            request.patch_path,
        )
        if inspection.sha256 != request.expected_patch_sha256:
            raise PatchApplicationError()
        changed_paths = _patch_paths(inspection.content)
        root = request.repository_root
        branch_created = False
        original_branch = ""
        original_head = ""
        try:
            head = self._git(root, "rev-parse", "--verify", "HEAD").strip()
            original_head = head
            original_branch = self._git(
                root,
                "branch",
                "--show-current",
            ).strip()
            status = self._git(
                root,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            )
            if head != request.base_commit or status:
                raise PatchApplicationError()
            existing = self._existing_exact_branch(
                request,
                changed_paths,
            )
            if existing is not None:
                return existing
            self._git(
                root,
                "switch",
                "--create",
                request.branch,
                request.base_commit,
            )
            branch_created = True
            patch_argument = request.patch_path.as_posix()
            self._git(
                root,
                "apply",
                "--check",
                "--binary",
                "--index",
                patch_argument,
            )
            self._git(
                root,
                "apply",
                "--binary",
                "--index",
                "--whitespace=nowarn",
                patch_argument,
            )
            staged_paths = tuple(
                Path(path)
                for path in self._git(
                    root,
                    "diff",
                    "--cached",
                    "--name-only",
                    "-z",
                ).split("\0")
                if path
            )
            if not staged_paths or set(staged_paths) != set(changed_paths):
                raise PatchApplicationError()
            expected_tree = self._git(
                root,
                "write-tree",
            ).strip()
            if expected_tree != request.expected_tree_sha:
                raise PatchTreeMismatchError()
            self._git(
                root,
                "commit",
                "--no-verify",
                "--no-gpg-sign",
                "-m",
                request.commit_message,
            )
            commit = self._git(
                root,
                "rev-parse",
                "--verify",
                "HEAD",
            ).strip()
            committed_tree = self._git(
                root,
                "rev-parse",
                "--verify",
                "HEAD^{tree}",
            ).strip()
            if (
                committed_tree != expected_tree
                or self._git(
                    root,
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                )
            ):
                raise PatchApplicationError()
            return AppliedPatch(
                branch=request.branch,
                commit_sha=commit,
                changed_paths=changed_paths,
                exact=True,
                substantive_repair=False,
                tree_sha=committed_tree,
            )
        except (CommandError, PatchApplicationError) as error:
            if branch_created:
                self._cleanup_failed_branch(
                    request,
                    original_branch=original_branch,
                    original_head=original_head,
                )
            if isinstance(error, PatchApplicationError):
                raise
            raise PatchApplicationError() from error

    def _cleanup_failed_branch(
        self,
        request: ExactPatchRequest,
        *,
        original_branch: str,
        original_head: str,
    ) -> None:
        restore = (
            ("switch", original_branch)
            if original_branch
            else ("switch", "--detach", original_head)
        )
        for arguments in (
            ("reset", "--hard", request.base_commit),
            restore,
            ("branch", "-D", request.branch),
        ):
            try:
                self._git(request.repository_root, *arguments)
            except CommandError:
                pass

    def resolve_tree(
        self,
        repository_root: Path,
        commit: str,
    ) -> str | None:
        try:
            tree = self._git(
                repository_root,
                "rev-parse",
                "--verify",
                f"{commit}^{{tree}}",
            ).strip()
        except CommandError:
            return None
        return tree if len(tree) == 40 else None

    def resolve_branch_commit(
        self,
        repository_root: Path,
        branch: str,
    ) -> str | None:
        try:
            commit = self._git(
                repository_root,
                "rev-parse",
                "--verify",
                f"refs/heads/{branch}",
            ).strip()
        except CommandError:
            return None
        return commit if len(commit) == 40 else None

    def restore_after_publication_failure(
        self,
        repository_root: Path,
        base_commit: str,
        base_branch: str,
    ) -> None:
        self._git(repository_root, "switch", base_branch)
        head = self._git(
            repository_root,
            "rev-parse",
            "--verify",
            "HEAD",
        ).strip()
        status = self._git(
            repository_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        if head != base_commit or status:
            raise PatchApplicationError()

    def _existing_exact_branch(
        self,
        request: ExactPatchRequest,
        changed_paths: tuple[Path, ...],
    ) -> AppliedPatch | None:
        try:
            commit = self._git(
                request.repository_root,
                "rev-parse",
                "--verify",
                f"refs/heads/{request.branch}",
            ).strip()
        except CommandError:
            return None
        try:
            parent = self._git(
                request.repository_root,
                "rev-parse",
                "--verify",
                f"{commit}^",
            ).strip()
            tree = self._git(
                request.repository_root,
                "rev-parse",
                "--verify",
                f"{commit}^{{tree}}",
            ).strip()
        except CommandError as error:
            raise PatchApplicationError() from error
        if parent != request.base_commit or tree != request.expected_tree_sha:
            raise PatchApplicationError()
        return AppliedPatch(
            branch=request.branch,
            commit_sha=commit,
            changed_paths=changed_paths,
            exact=True,
            substantive_repair=False,
            tree_sha=tree,
        )

    def _git(self, root: Path, *arguments: str) -> str:
        return self._commands.run(
            ("git", *arguments),
            cwd=root,
        ).stdout


def _patch_paths(content: bytes) -> tuple[Path, ...]:
    paths: list[Path] = []
    for raw_line in content.splitlines():
        if raw_line in {b"new file mode 120000", b"new mode 120000"}:
            raise PatchTraversalError()
        if raw_line.startswith((b"rename from ", b"copy from ")):
            _validate_patch_path(raw_line.split(b" ", 2)[2], "")
            continue
        if raw_line.startswith((b"rename to ", b"copy to ")):
            _validate_patch_path(raw_line.split(b" ", 2)[2], "")
            continue
        if raw_line.startswith(b"--- "):
            _validate_patch_path(raw_line[4:], "a/", allow_dev_null=True)
            continue
        if raw_line.startswith(b"+++ "):
            _validate_patch_path(raw_line[4:], "b/", allow_dev_null=True)
            continue
        if not raw_line.startswith(b"diff --git "):
            continue
        try:
            parts = shlex.split(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise PatchTraversalError() from error
        if len(parts) != 4 or parts[:2] != ["diff", "--git"]:
            raise PatchTraversalError()
        _repository_patch_path(parts[2], "a/")
        paths.append(_repository_patch_path(parts[3], "b/"))
    if not paths:
        raise PatchApplicationError()
    return tuple(dict.fromkeys(paths))


def _validate_patch_path(
    raw_path: bytes,
    prefix: str,
    *,
    allow_dev_null: bool = False,
) -> Path:
    try:
        decoded = raw_path.decode("utf-8")
        parts = shlex.split(decoded)
    except (UnicodeDecodeError, ValueError) as error:
        raise PatchTraversalError() from error
    if len(parts) != 1:
        raise PatchTraversalError()
    value = parts[0]
    if allow_dev_null and value == "/dev/null":
        return Path(".")
    return _repository_patch_path(value, prefix)


def _repository_patch_path(value: str, prefix: str) -> Path:
    if prefix and not value.startswith(prefix):
        raise PatchTraversalError()
    relative = value[len(prefix) :] if prefix else value
    try:
        return repository_path(Path(relative), "patch path")
    except ValueError as error:
        raise PatchTraversalError() from error


def _json_list(raw: str, operation: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise GitHubCampaignResponseError(operation) from error
    if not isinstance(value, list) or not all(
        isinstance(item, dict) for item in value
    ):
        raise GitHubCampaignResponseError(operation)
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
        raise GitHubCampaignResponseError(
            "find_pull_request"
        ) from error


def _issue_from_json(value: dict[str, Any]) -> IssueReference:
    try:
        return IssueReference(
            number=int(value["number"]),
            url=str(value["url"]),
            title=str(value["title"]),
            body=str(value["body"]),
            state=str(value.get("state") or "OPEN").upper(),
            labels=tuple(
                str(label["name"])
                for label in value.get("labels", ())
                if isinstance(label, dict) and "name" in label
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise GitHubCampaignResponseError(
            "find_candidate_issue"
        ) from error


def _created_pull_request(
    url: str,
    *,
    repository: str,
    base_branch: str,
    head_branch: str,
    head_commit: str,
    draft: bool,
    body: str,
) -> PullRequestReference:
    number = _github_number(
        url,
        "pull",
        "create_pull_request",
        repository,
    )
    return PullRequestReference(
        number,
        url,
        head_branch,
        head_commit,
        draft,
        body,
        base_branch,
        "OPEN",
    )


def _github_number(
    url: str,
    kind: str,
    operation: str,
    repository: str,
) -> int:
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
            raise ValueError
        return int(parts[3])
    except (ValueError, TypeError) as error:
        raise GitHubCampaignResponseError(operation) from error
