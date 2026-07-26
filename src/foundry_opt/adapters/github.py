from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse

from foundry_opt.adapters.commands import CommandError
from foundry_opt.preflight.interfaces import (
    CommandRunner,
    GatewayResult,
    GitHubGateway,
)


class GitHubGatewayError(RuntimeError):
    pass


class GitHubAuthenticationError(GitHubGatewayError):
    def __init__(self) -> None:
        super().__init__("GitHub CLI authentication failed")


class GitHubRepositoryError(GitHubGatewayError):
    def __init__(self) -> None:
        super().__init__("GitHub repository is not reachable")


class GitHubRepositoryMismatchError(GitHubGatewayError):
    def __init__(
        self,
        origin_repository: str,
        resolved_repository: str,
    ) -> None:
        self.origin_repository = origin_repository
        self.resolved_repository = resolved_repository
        super().__init__("GitHub repository does not match origin")


class GitHubPermissionError(GitHubGatewayError):
    def __init__(self, permission: str) -> None:
        self.permission = permission
        super().__init__("Repository admin permission is required")


class GitHubResponseError(GitHubGatewayError):
    def __init__(self) -> None:
        super().__init__("GitHub CLI returned invalid repository metadata")


@dataclass(frozen=True)
class GitHubRepositoryMetadata:
    repository: str
    default_branch: str
    viewer_permission: str


class GhGitHubGateway(GitHubGateway):
    def __init__(self, command_runner: CommandRunner) -> None:
        self._command_runner = command_runner

    def verify_access(self, repository_root: Path) -> GatewayResult:
        self._verify_authentication(repository_root)
        login = self._authenticated_login(repository_root)
        metadata = self.repository_metadata(repository_root)

        permission = metadata.viewer_permission
        if permission.casefold() != "admin":
            raise GitHubPermissionError(permission)

        return GatewayResult(
            summary=f"GitHub access verified for {metadata.repository}",
            detail=(
                f"Authenticated as {login}; default branch: "
                f"{metadata.default_branch}"
            ),
        )

    def repository_metadata(
        self,
        repository_root: Path,
    ) -> GitHubRepositoryMetadata:
        origin_repository = self._origin_repository(repository_root)
        metadata = self._repository_metadata(repository_root, origin_repository)
        return GitHubRepositoryMetadata(
            repository=metadata["nameWithOwner"],
            default_branch=metadata["defaultBranchRef"]["name"],
            viewer_permission=metadata["viewerPermission"],
        )

    def _verify_authentication(self, repository_root: Path) -> None:
        try:
            self._command_runner.run(
                ["gh", "auth", "status"],
                cwd=repository_root,
            )
        except CommandError as error:
            raise GitHubAuthenticationError() from error

    def _authenticated_login(self, repository_root: Path) -> str:
        try:
            login = self._command_runner.run(
                ["gh", "api", "user", "--jq", ".login"],
                cwd=repository_root,
            ).stdout.strip()
        except CommandError as error:
            raise GitHubAuthenticationError() from error
        if not login:
            raise GitHubAuthenticationError()
        return login

    def _origin_repository(self, repository_root: Path) -> str:
        try:
            remote_url = self._command_runner.run(
                ["git", "remote", "get-url", "origin"],
                cwd=repository_root,
            ).stdout.strip()
        except CommandError as error:
            raise GitHubRepositoryError() from error

        repository = github_repository_from_remote_url(remote_url)
        if repository is None:
            raise GitHubRepositoryError()
        return repository

    def _repository_metadata(
        self,
        repository_root: Path,
        origin_repository: str,
    ) -> dict[str, Any]:
        try:
            raw_metadata = self._command_runner.run(
                [
                    "gh",
                    "repo",
                    "view",
                    origin_repository,
                    "--json",
                    "nameWithOwner,viewerPermission,defaultBranchRef",
                ],
                cwd=repository_root,
            ).stdout
        except CommandError as error:
            raise GitHubRepositoryError() from error

        try:
            metadata = json.loads(raw_metadata)
            repository = metadata["nameWithOwner"]
            permission = metadata["viewerPermission"]
            default_branch = metadata["defaultBranchRef"]["name"]
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise GitHubResponseError() from error

        if not all(
            isinstance(value, str) and value
            for value in (repository, permission, default_branch)
        ):
            raise GitHubResponseError()
        if repository.casefold() != origin_repository.casefold():
            raise GitHubRepositoryMismatchError(
                origin_repository,
                repository,
            )
        return metadata


def github_repository_from_remote_url(remote_url: str) -> str | None:
    scp_match = re.fullmatch(
        r"git@github\.com:(?P<path>.+)",
        remote_url,
        flags=re.IGNORECASE,
    )
    if scp_match:
        return _github_repository_from_path(scp_match.group("path"))

    try:
        parsed = urlparse(remote_url)
    except ValueError:
        return None
    if parsed.query or parsed.fragment or parsed.params:
        return None

    scheme = parsed.scheme.casefold()
    authority = parsed.netloc.casefold()
    if scheme == "https":
        if authority != "github.com":
            return None
    elif scheme == "ssh":
        if authority != "git@github.com":
            return None
    else:
        return None

    if not parsed.path.startswith("/"):
        return None
    return _github_repository_from_path(parsed.path[1:])


def _github_repository_from_path(path: str) -> str | None:
    match = re.fullmatch(
        r"(?P<owner>[A-Za-z0-9-]+)/(?P<repository>[A-Za-z0-9._-]+)",
        path,
    )
    if match is None:
        return None

    repository = match.group("repository").removesuffix(".git")
    if repository in {"", ".", ".."}:
        return None
    return f"{match.group('owner')}/{repository}"
