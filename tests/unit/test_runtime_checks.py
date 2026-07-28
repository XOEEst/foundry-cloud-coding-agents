from pathlib import Path
import subprocess
import sys
from collections.abc import Sequence
from typing import Callable

import pytest

from foundry_opt.adapters.commands import (
    CommandExitError,
    CommandNotFoundError,
    SubprocessCommandRunner,
)
from foundry_opt.adapters.github import GitHubRepositoryMetadata
from foundry_opt.preflight.interfaces import CommandResult
from foundry_opt.preflight.models import CheckStatus, PreflightRequest
from foundry_opt.preflight.runtime_checks import (
    CleanWorktreeCheck,
    DefaultBranchCheck,
    ExecutableCheck,
    GitHubRemoteCheck,
    GitRepositoryCheck,
    PythonRuntimeCheck,
)


class FakeCommandRunner:
    def __init__(
        self,
        responses: dict[
            tuple[str, ...],
            CommandResult | Exception,
        ],
    ) -> None:
        self._responses = responses
        self.invocations: list[tuple[str, ...]] = []

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
    ) -> CommandResult:
        command = tuple(arguments)
        self.invocations.append(command)
        response = self._responses[command]
        if isinstance(response, Exception):
            raise response
        return response


class FakeGitHubMetadataGateway:
    def __init__(self, metadata: GitHubRepositoryMetadata) -> None:
        self._metadata = metadata

    def repository_metadata(
        self,
        repository_root: Path,
    ) -> GitHubRepositoryMetadata:
        return self._metadata


def _request() -> PreflightRequest:
    return PreflightRequest(
        repository_root=Path("repository"),
        config_path=Path(".github/foundry-optimizer.yaml"),
        environment="acceptance",
        target="support_agent",
    )


def _success(stdout: str = "") -> CommandResult:
    return CommandResult(exit_code=0, stdout=stdout, stderr="")


def test_command_runner_passes_an_argument_array_without_a_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation: dict[str, object] = {}

    def fake_run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        invocation["arguments"] = arguments
        invocation.update(kwargs)
        return subprocess.CompletedProcess(arguments, 0, "ok\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = SubprocessCommandRunner().run(
        ["gh", "repo", "view"],
        cwd=Path("repository"),
    )

    assert result.exit_code == 0
    assert result.stdout == "ok\n"
    assert invocation == {
        "arguments": ["gh", "repo", "view"],
        "cwd": Path("repository"),
        "shell": False,
        "capture_output": True,
        "text": True,
        "check": False,
    }


def test_command_runner_maps_a_missing_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(
        arguments: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(CommandNotFoundError) as error:
        SubprocessCommandRunner().run(["missing"])

    assert error.value.executable == "missing"


def test_command_runner_maps_a_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(
        arguments: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 7, "output", "diagnostic")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(CommandExitError) as error:
        SubprocessCommandRunner().run(["git", "status"])

    assert error.value.exit_code == 7
    assert error.value.stdout == "output"
    assert error.value.stderr == "diagnostic"


def test_command_runner_preserves_binary_stdin_newlines() -> None:
    result = SubprocessCommandRunner().run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "sys.stdout.buffer.write(sys.stdin.buffer.read())"
            ),
        ],
        input_bytes=b"a\nb\n",
    )

    assert result.stdout == "a\nb\n"


def test_command_runner_rejects_text_and_binary_stdin_together() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        SubprocessCommandRunner().run(
            [sys.executable, "-c", "pass"],
            input_text="text",
            input_bytes=b"bytes",
        )


@pytest.mark.parametrize(
    ("version", "expected_status"),
    [
        ((3, 12, 0), CheckStatus.PASS),
        ((3, 11, 9), CheckStatus.FAIL),
        ((3, 13, 0), CheckStatus.FAIL),
    ],
)
def test_python_runtime_requires_python_312(
    version: tuple[int, int, int],
    expected_status: CheckStatus,
) -> None:
    result = PythonRuntimeCheck(version=version).run(_request())

    assert result.check_id == "runtime.python"
    assert result.status is expected_status
    if expected_status is CheckStatus.FAIL:
        assert result.remediation == "Run foundry-opt with Python 3.12."


def test_required_executable_discovery_reports_missing_tool() -> None:
    finder: Callable[[str], str | None] = lambda name: None

    result = ExecutableCheck("uv", finder=finder).run(_request())

    assert result.check_id == "runtime.uv"
    assert result.status is CheckStatus.FAIL
    assert result.remediation == "Install uv and ensure it is available on PATH."


def test_azd_discovery_is_skipped_when_not_required() -> None:
    result = ExecutableCheck("azd", required=False).run(_request())

    assert result.check_id == "runtime.azd"
    assert result.status is CheckStatus.SKIP
    assert result.summary == "azd is not required for this target"


@pytest.mark.parametrize(
    "remote_url",
    [
        "https://github.com/octo-org/optimizer.git",
        "ssh://git@github.com/octo-org/optimizer.git",
        "git@github.com:octo-org/optimizer.git",
    ],
)
def test_git_repository_and_supported_github_remote_are_resolved(
    remote_url: str,
) -> None:
    runner = FakeCommandRunner(
        {
            ("git", "rev-parse", "--is-inside-work-tree"): _success("true\n"),
            ("git", "remote", "get-url", "origin"): _success(
                f"{remote_url}\n"
            ),
        }
    )

    repository = GitRepositoryCheck(runner).run(_request())
    remote = GitHubRemoteCheck(runner).run(_request())

    assert repository.status is CheckStatus.PASS
    assert repository.check_id == "repository.git"
    assert remote.status is CheckStatus.PASS
    assert remote.check_id == "repository.github_remote"
    assert remote.detail == "octo-org/optimizer"


@pytest.mark.parametrize(
    "remote_url",
    [
        "https://example.com/octo-org/optimizer.git",
        "http://github.com/octo-org/optimizer.git",
        "https://octocat@github.com/octo-org/optimizer.git",
        "https://github.com/octo-org/optimizer/extra.git",
    ],
)
def test_invalid_github_remote_fails_with_stable_guidance(
    remote_url: str,
) -> None:
    runner = FakeCommandRunner(
        {
            ("git", "remote", "get-url", "origin"): _success(
                f"{remote_url}\n"
            )
        }
    )

    result = GitHubRemoteCheck(runner).run(_request())

    assert result.status is CheckStatus.FAIL
    assert result.summary == "A GitHub origin remote could not be resolved"
    assert result.remediation == "Configure origin to point to a GitHub repository."


def test_current_branch_must_equal_remote_default_branch() -> None:
    runner = FakeCommandRunner(
        {
            ("git", "branch", "--show-current"): _success("feature\n"),
        }
    )
    gateway = FakeGitHubMetadataGateway(
        GitHubRepositoryMetadata(
            repository="octo-org/optimizer",
            default_branch="main",
            viewer_permission="ADMIN",
        )
    )

    result = DefaultBranchCheck(runner, gateway).run(_request())

    assert result.check_id == "repository.default_branch"
    assert result.status is CheckStatus.FAIL
    assert result.detail == "Current branch: feature; GitHub default branch: main"
    assert result.remediation == "Switch to the GitHub default branch (main)."


def test_default_branch_uses_github_metadata_when_origin_head_is_stale() -> None:
    runner = FakeCommandRunner(
        {
            ("git", "branch", "--show-current"): _success("main\n"),
            (
                "git",
                "symbolic-ref",
                "--short",
                "refs/remotes/origin/HEAD",
            ): _success("origin/master\n"),
        }
    )
    gateway = FakeGitHubMetadataGateway(
        GitHubRepositoryMetadata(
            repository="octo-org/optimizer",
            default_branch="main",
            viewer_permission="ADMIN",
        )
    )

    result = DefaultBranchCheck(runner, gateway).run(_request())

    assert result.status is CheckStatus.PASS
    assert result.summary == "Current branch is the GitHub default branch (main)"
    assert runner.invocations == [("git", "branch", "--show-current")]


def test_worktree_check_includes_untracked_files() -> None:
    runner = FakeCommandRunner(
        {
            (
                "git",
                "status",
                "--porcelain",
                "--untracked-files=all",
            ): _success("?? new-file.txt\n"),
        }
    )

    result = CleanWorktreeCheck(runner).run(_request())

    assert result.check_id == "repository.worktree"
    assert result.status is CheckStatus.FAIL
    assert result.detail == "The worktree contains tracked or untracked changes."
    assert result.remediation == "Commit, stash, or remove all worktree changes."
