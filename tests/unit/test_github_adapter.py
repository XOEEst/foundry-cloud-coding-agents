from collections.abc import Sequence
from pathlib import Path

import pytest

from foundry_opt.adapters.commands import CommandExitError
from foundry_opt.adapters.github import (
    GhGitHubGateway,
    GitHubAuthenticationError,
    GitHubPermissionError,
    GitHubRepositoryError,
    GitHubRepositoryMismatchError,
    GitHubResponseError,
    github_repository_from_remote_url,
)
from foundry_opt.preflight.interfaces import CommandResult, GatewayResult
from foundry_opt.preflight.models import CheckStatus, PreflightRequest
from foundry_opt.preflight.runtime_checks import GitHubAccessCheck


class FakeCommandRunner:
    def __init__(
        self,
        responses: dict[tuple[str, ...], CommandResult | Exception],
    ) -> None:
        self._responses = responses
        self.invocations: list[tuple[tuple[str, ...], Path | None]] = []

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
    ) -> CommandResult:
        command = tuple(arguments)
        self.invocations.append((command, cwd))
        response = self._responses[command]
        if isinstance(response, Exception):
            raise response
        return response


class FakeGitHubGateway:
    def __init__(self, response: GatewayResult | Exception) -> None:
        self._response = response

    def verify_access(self, repository_root: Path) -> GatewayResult:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _success(stdout: str = "") -> CommandResult:
    return CommandResult(exit_code=0, stdout=stdout, stderr="")


def _exit(arguments: Sequence[str], stderr: str) -> CommandExitError:
    return CommandExitError(
        arguments,
        exit_code=1,
        stdout="",
        stderr=stderr,
    )


def _request() -> PreflightRequest:
    return PreflightRequest(
        repository_root=Path("repository"),
        config_path=Path(".github/foundry-optimizer.yaml"),
        environment="acceptance",
        target="support_agent",
    )


@pytest.mark.parametrize(
    "remote_url",
    [
        "https://github.com/octo-org/optimizer",
        "https://github.com/octo-org/optimizer.git",
        "ssh://git@github.com/octo-org/optimizer",
        "ssh://git@github.com/octo-org/optimizer.git",
        "git@github.com:octo-org/optimizer",
        "git@github.com:octo-org/optimizer.git",
    ],
)
def test_github_remote_parser_accepts_supported_transports(
    remote_url: str,
) -> None:
    assert (
        github_repository_from_remote_url(remote_url)
        == "octo-org/optimizer"
    )


@pytest.mark.parametrize(
    "remote_url",
    [
        "file://github.com/octo-org/optimizer.git",
        "ftp://github.com/octo-org/optimizer.git",
        "http://github.com/octo-org/optimizer.git",
        "git://github.com/octo-org/optimizer.git",
        "https://github.com.evil.example/octo-org/optimizer.git",
        "https://evil.example/github.com/octo-org/optimizer.git",
        "https://octocat@github.com/octo-org/optimizer.git",
        "https://github.com/octo-org/optimizer.git?ref=main",
        "https://github.com/octo-org/optimizer.git#readme",
        "https://github.com:443/octo-org/optimizer.git",
        "https://[github.com/octo-org/optimizer.git",
        "ssh://git@github.com:22/octo-org/optimizer.git",
        "ssh://octocat@github.com/octo-org/optimizer.git",
        "git@github.com:octo-org/optimizer/extra.git",
        "https://github.com/octo-org/optimizer/extra.git",
    ],
)
def test_github_remote_parser_rejects_unsupported_or_malformed_origins(
    remote_url: str,
) -> None:
    assert github_repository_from_remote_url(remote_url) is None


def test_gateway_verifies_authenticated_admin_access_with_read_only_commands() -> None:
    repository_root = Path("repository")
    runner = FakeCommandRunner(
        {
            ("gh", "api", "user", "--jq", ".login"): _success("octocat\n"),
            ("git", "remote", "get-url", "origin"): _success(
                "git@github.com:octo-org/optimizer.git\n"
            ),
            (
                "gh",
                "repo",
                "view",
                "octo-org/optimizer",
                "--json",
                "nameWithOwner,viewerPermission,defaultBranchRef",
            ): _success(
                '{"nameWithOwner":"octo-org/optimizer",'
                '"viewerPermission":"ADMIN",'
                '"defaultBranchRef":{"name":"main"}}'
            ),
        }
    )

    result = GhGitHubGateway(runner).verify_access(repository_root)

    assert result == GatewayResult(
        summary="GitHub access verified for octo-org/optimizer",
        detail="Authenticated as octocat; default branch: main",
    )
    assert runner.invocations == [
        (("git", "remote", "get-url", "origin"), repository_root),
        (
            (
                "gh",
                "repo",
                "view",
                "octo-org/optimizer",
                "--json",
                "nameWithOwner,viewerPermission,defaultBranchRef",
            ),
            repository_root,
        ),
        (("gh", "api", "user", "--jq", ".login"), repository_root),
    ]


def test_runtime_gateway_accepts_repository_scoped_installation_token() -> None:
    user_command = ("gh", "api", "user", "--jq", ".login")
    runner = FakeCommandRunner(
        {
            ("git", "remote", "get-url", "origin"): _success(
                "https://github.com/octo-org/optimizer.git\n"
            ),
            ("gh", "api", "repos/octo-org/optimizer"): _success(
                '{"full_name":"octo-org/optimizer",'
                '"default_branch":"main"}'
            ),
            user_command: _exit(
                user_command,
                "Resource not accessible by integration",
            ),
        }
    )

    result = GhGitHubGateway(
        runner,
        require_admin=False,
    ).verify_access(Path("repository"))

    assert result == GatewayResult(
        summary="GitHub access verified for octo-org/optimizer",
        detail="Default branch: main",
    )


def test_gateway_maps_unreachable_repository() -> None:
    command = (
        "gh",
        "repo",
        "view",
        "octo-org/optimizer",
        "--json",
        "nameWithOwner,viewerPermission,defaultBranchRef",
    )
    runner = FakeCommandRunner(
        {
            ("git", "remote", "get-url", "origin"): _success(
                "https://github.com/octo-org/optimizer.git\n"
            ),
            command: _exit(command, "repository not found"),
        }
    )

    with pytest.raises(GitHubRepositoryError):
        GhGitHubGateway(runner).verify_access(Path("repository"))


def test_gateway_requires_admin_permission() -> None:
    runner = FakeCommandRunner(
        {
            ("git", "remote", "get-url", "origin"): _success(
                "https://github.com/octo-org/optimizer.git\n"
            ),
            (
                "gh",
                "repo",
                "view",
                "octo-org/optimizer",
                "--json",
                "nameWithOwner,viewerPermission,defaultBranchRef",
            ): _success(
                '{"nameWithOwner":"octo-org/optimizer",'
                '"viewerPermission":"WRITE",'
                '"defaultBranchRef":{"name":"main"}}'
            ),
        }
    )

    with pytest.raises(GitHubPermissionError) as error:
        GhGitHubGateway(runner).verify_access(Path("repository"))

    assert error.value.permission == "WRITE"


def test_gateway_requires_explicit_admin_permission_for_user_onboarding() -> None:
    runner = FakeCommandRunner(
        {
            ("git", "remote", "get-url", "origin"): _success(
                "https://github.com/octo-org/optimizer.git\n"
            ),
            (
                "gh",
                "repo",
                "view",
                "octo-org/optimizer",
                "--json",
                "nameWithOwner,viewerPermission,defaultBranchRef",
            ): _success(
                '{"nameWithOwner":"octo-org/optimizer",'
                '"viewerPermission":null,'
                '"defaultBranchRef":{"name":"main"}}'
            ),
        }
    )

    with pytest.raises(GitHubPermissionError) as error:
        GhGitHubGateway(runner).verify_access(Path("repository"))

    assert error.value.permission == "UNKNOWN"


def test_gateway_rejects_repository_metadata_that_does_not_match_origin() -> None:
    runner = FakeCommandRunner(
        {
            ("git", "remote", "get-url", "origin"): _success(
                "https://github.com/octo-org/optimizer.git\n"
            ),
            (
                "gh",
                "repo",
                "view",
                "octo-org/optimizer",
                "--json",
                "nameWithOwner,viewerPermission,defaultBranchRef",
            ): _success(
                '{"nameWithOwner":"other-org/other-repository",'
                '"viewerPermission":"ADMIN",'
                '"defaultBranchRef":{"name":"main"}}'
            ),
        }
    )

    with pytest.raises(GitHubRepositoryMismatchError) as error:
        GhGitHubGateway(runner).verify_access(Path("repository"))

    assert error.value.origin_repository == "octo-org/optimizer"
    assert error.value.resolved_repository == "other-org/other-repository"


def test_gateway_matches_repository_metadata_case_insensitively() -> None:
    runner = FakeCommandRunner(
        {
            ("git", "remote", "get-url", "origin"): _success(
                "https://github.com/Octo-Org/Optimizer.git\n"
            ),
            (
                "gh",
                "repo",
                "view",
                "Octo-Org/Optimizer",
                "--json",
                "nameWithOwner,viewerPermission,defaultBranchRef",
            ): _success(
                '{"nameWithOwner":"octo-org/optimizer",'
                '"viewerPermission":"ADMIN",'
                '"defaultBranchRef":{"name":"main"}}'
            ),
        }
    )

    metadata = GhGitHubGateway(runner).repository_metadata(Path("repository"))

    assert metadata.repository == "octo-org/optimizer"


def test_gateway_rejects_malformed_repository_metadata() -> None:
    runner = FakeCommandRunner(
        {
            ("git", "remote", "get-url", "origin"): _success(
                "https://github.com/octo-org/optimizer.git\n"
            ),
            (
                "gh",
                "repo",
                "view",
                "octo-org/optimizer",
                "--json",
                "nameWithOwner,viewerPermission,defaultBranchRef",
            ): _success("not-json"),
        }
    )

    with pytest.raises(GitHubResponseError):
        GhGitHubGateway(runner).verify_access(Path("repository"))


@pytest.mark.parametrize(
    ("failure", "summary", "remediation"),
    [
        (
            GitHubAuthenticationError(),
            "GitHub CLI authentication failed",
            "Authenticate gh for GitHub.com and retry preflight.",
        ),
        (
            GitHubRepositoryError(),
            "GitHub repository is not reachable",
            "Confirm the origin repository exists and the authenticated user can read it.",
        ),
        (
            GitHubRepositoryMismatchError(
                "octo-org/optimizer",
                "other-org/other-repository",
            ),
            "GitHub repository does not match origin",
            "Correct origin to point to the intended GitHub repository.",
        ),
        (
            GitHubPermissionError("WRITE"),
            "Repository admin permission is required",
            "Grant the authenticated user admin permission on the repository.",
        ),
        (
            GitHubResponseError(),
            "GitHub CLI returned invalid repository metadata",
            "Update gh and retry preflight.",
        ),
    ],
)
def test_github_access_check_returns_actionable_failures(
    failure: Exception,
    summary: str,
    remediation: str,
) -> None:
    result = GitHubAccessCheck(FakeGitHubGateway(failure)).run(_request())

    assert result.check_id == "github.permission"
    assert result.status is CheckStatus.FAIL
    assert result.summary == summary
    assert result.remediation == remediation


def test_github_access_check_returns_gateway_success() -> None:
    result = GitHubAccessCheck(
        FakeGitHubGateway(
            GatewayResult(
                summary="GitHub access verified for octo-org/optimizer",
                detail="Authenticated as octocat; default branch: main",
            )
        )
    ).run(_request())

    assert result.check_id == "github.permission"
    assert result.status is CheckStatus.PASS
    assert result.summary == "GitHub access verified for octo-org/optimizer"
