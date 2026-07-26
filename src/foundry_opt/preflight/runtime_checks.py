from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import shutil
import sys
from typing import Protocol

from foundry_opt.adapters.commands import CommandError
from foundry_opt.adapters.github import (
    GitHubAuthenticationError,
    GitHubPermissionError,
    GitHubRepositoryError,
    GitHubRepositoryMetadata,
    GitHubRepositoryMismatchError,
    GitHubResponseError,
    github_repository_from_remote_url,
)
from foundry_opt.preflight.interfaces import CommandRunner, GitHubGateway
from foundry_opt.preflight.models import CheckResult, CheckStatus, PreflightRequest


class GitHubDefaultBranchGateway(Protocol):
    def repository_metadata(
        self,
        repository_root: Path,
    ) -> GitHubRepositoryMetadata: ...


class PythonRuntimeCheck:
    check_id = "runtime.python"

    def __init__(self, *, version: tuple[int, int, int] | None = None) -> None:
        self._version = version or (
            sys.version_info.major,
            sys.version_info.minor,
            sys.version_info.micro,
        )

    def run(self, request: PreflightRequest) -> CheckResult:
        del request
        version_text = ".".join(str(part) for part in self._version)
        if self._version[:2] == (3, 12):
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.PASS,
                summary=f"Python {version_text} is compatible",
            )
        return CheckResult(
            check_id=self.check_id,
            status=CheckStatus.FAIL,
            summary=f"Python {version_text} is not supported",
            remediation="Run foundry-opt with Python 3.12.",
        )


class ExecutableCheck:
    def __init__(
        self,
        executable: str,
        *,
        required: bool = True,
        finder: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self._executable = executable
        self._required = required
        self._finder = finder
        self.check_id = f"runtime.{executable}"

    def run(self, request: PreflightRequest) -> CheckResult:
        del request
        if not self._required:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.SKIP,
                summary=f"{self._executable} is not required for this target",
            )

        location = self._finder(self._executable)
        if location:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.PASS,
                summary=f"{self._executable} is available",
                detail=location,
            )
        return CheckResult(
            check_id=self.check_id,
            status=CheckStatus.FAIL,
            summary=f"{self._executable} was not found",
            remediation=(
                f"Install {self._executable} and ensure it is available on PATH."
            ),
        )


class GitRepositoryCheck:
    check_id = "repository.git"

    def __init__(self, command_runner: CommandRunner) -> None:
        self._command_runner = command_runner

    def run(self, request: PreflightRequest) -> CheckResult:
        try:
            result = self._command_runner.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=request.repository_root,
            )
        except CommandError:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAIL,
                summary="The selected path is not a Git repository",
                remediation="Run preflight from the root of a Git repository.",
            )

        if result.stdout.strip().casefold() != "true":
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAIL,
                summary="The selected path is not a Git worktree",
                remediation="Run preflight from the root of a Git repository.",
            )
        return CheckResult(
            check_id=self.check_id,
            status=CheckStatus.PASS,
            summary="Git repository detected",
        )


class GitHubRemoteCheck:
    check_id = "repository.github_remote"

    def __init__(self, command_runner: CommandRunner, *, remote: str = "origin") -> None:
        self._command_runner = command_runner
        self._remote = remote

    def run(self, request: PreflightRequest) -> CheckResult:
        try:
            result = self._command_runner.run(
                ["git", "remote", "get-url", self._remote],
                cwd=request.repository_root,
            )
        except CommandError:
            return self._failure()

        repository = github_repository_from_remote_url(result.stdout.strip())
        if repository is None:
            return self._failure()
        return CheckResult(
            check_id=self.check_id,
            status=CheckStatus.PASS,
            summary="GitHub remote resolved",
            detail=repository,
        )

    def _failure(self) -> CheckResult:
        return CheckResult(
            check_id=self.check_id,
            status=CheckStatus.FAIL,
            summary="A GitHub origin remote could not be resolved",
            remediation="Configure origin to point to a GitHub repository.",
        )


class DefaultBranchCheck:
    check_id = "repository.default_branch"

    def __init__(
        self,
        command_runner: CommandRunner,
        github_gateway: GitHubDefaultBranchGateway,
    ) -> None:
        self._command_runner = command_runner
        self._github_gateway = github_gateway

    def run(self, request: PreflightRequest) -> CheckResult:
        try:
            current = self._command_runner.run(
                ["git", "branch", "--show-current"],
                cwd=request.repository_root,
            ).stdout.strip()
        except CommandError:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAIL,
                summary="The current branch could not be resolved",
                remediation="Check out a local branch, then retry preflight.",
            )

        if not current:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAIL,
                summary="The current branch could not be resolved",
                remediation="Check out a local branch, then retry preflight.",
            )

        try:
            metadata = self._github_gateway.repository_metadata(
                request.repository_root
            )
        except GitHubRepositoryMismatchError as error:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAIL,
                summary="GitHub repository does not match origin",
                detail=(
                    f"Origin repository: {error.origin_repository}; "
                    f"resolved repository: {error.resolved_repository}"
                ),
                remediation=(
                    "Run preflight from the intended repository and correct its "
                    "origin remote."
                ),
            )
        except (
            GitHubAuthenticationError,
            GitHubRepositoryError,
            GitHubResponseError,
        ):
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAIL,
                summary="The GitHub default branch could not be resolved",
                remediation=(
                    "Confirm gh authentication and the origin repository, then "
                    "retry preflight."
                ),
            )
        default = metadata.default_branch
        if current != default:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAIL,
                summary="The current branch is not the GitHub default branch",
                detail=(
                    f"Current branch: {current}; GitHub default branch: {default}"
                ),
                remediation=f"Switch to the GitHub default branch ({default}).",
            )
        return CheckResult(
            check_id=self.check_id,
            status=CheckStatus.PASS,
            summary=f"Current branch is the GitHub default branch ({default})",
        )


class CleanWorktreeCheck:
    check_id = "repository.worktree"

    def __init__(self, command_runner: CommandRunner) -> None:
        self._command_runner = command_runner

    def run(self, request: PreflightRequest) -> CheckResult:
        try:
            output = self._command_runner.run(
                ["git", "status", "--porcelain", "--untracked-files=all"],
                cwd=request.repository_root,
            ).stdout
        except CommandError:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAIL,
                summary="Git worktree status could not be read",
                remediation="Resolve the Git error and retry preflight.",
            )

        if output:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAIL,
                summary="The Git worktree is not clean",
                detail="The worktree contains tracked or untracked changes.",
                remediation="Commit, stash, or remove all worktree changes.",
            )
        return CheckResult(
            check_id=self.check_id,
            status=CheckStatus.PASS,
            summary="Git worktree is clean",
        )


class GitHubAccessCheck:
    check_id = "github.permission"

    def __init__(self, gateway: GitHubGateway) -> None:
        self._gateway = gateway

    def run(self, request: PreflightRequest) -> CheckResult:
        try:
            result = self._gateway.verify_access(request.repository_root)
        except GitHubAuthenticationError:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAIL,
                summary="GitHub CLI authentication failed",
                remediation="Authenticate gh for GitHub.com and retry preflight.",
            )
        except GitHubRepositoryMismatchError:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAIL,
                summary="GitHub repository does not match origin",
                remediation=(
                    "Correct origin to point to the intended GitHub repository."
                ),
            )
        except GitHubRepositoryError:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAIL,
                summary="GitHub repository is not reachable",
                remediation=(
                    "Confirm the origin repository exists and the authenticated "
                    "user can read it."
                ),
            )
        except GitHubPermissionError:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAIL,
                summary="Repository admin permission is required",
                remediation=(
                    "Grant the authenticated user admin permission on the repository."
                ),
            )
        except GitHubResponseError:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAIL,
                summary="GitHub CLI returned invalid repository metadata",
                remediation="Update gh and retry preflight.",
            )
        return CheckResult(
            check_id=self.check_id,
            status=CheckStatus.PASS,
            summary=result.summary,
            detail=result.detail,
        )


def build_runtime_checks(
    command_runner: CommandRunner,
    *,
    github_gateway: GitHubDefaultBranchGateway,
    require_az: bool,
    require_azd: bool,
    finder: Callable[[str], str | None] | None = None,
) -> tuple[
    PythonRuntimeCheck,
    ExecutableCheck,
    ExecutableCheck,
    ExecutableCheck,
    ExecutableCheck,
    ExecutableCheck,
    GitRepositoryCheck,
    GitHubRemoteCheck,
    DefaultBranchCheck,
    CleanWorktreeCheck,
]:
    executable_finder = finder or shutil.which
    return (
        PythonRuntimeCheck(),
        ExecutableCheck("uv", finder=executable_finder),
        ExecutableCheck("git", finder=executable_finder),
        ExecutableCheck("gh", finder=executable_finder),
        ExecutableCheck("az", required=require_az, finder=executable_finder),
        ExecutableCheck("azd", required=require_azd, finder=executable_finder),
        GitRepositoryCheck(command_runner),
        GitHubRemoteCheck(command_runner),
        DefaultBranchCheck(command_runner, github_gateway),
        CleanWorktreeCheck(command_runner),
    )
