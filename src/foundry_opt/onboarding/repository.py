from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import os
from pathlib import Path
import re

from foundry_opt.adapters.commands import CommandError
from foundry_opt.onboarding.models import (
    ChangeStatus,
    DraftPullRequest,
    DraftPullRequestPublication,
    OnboardingChange,
    OnboardingRequest,
    RepositoryDiscovery,
)
from foundry_opt.preflight.interfaces import CommandRunner


class ChangeSetError(RuntimeError):
    pass


class ChangeSetConflictError(ChangeSetError):
    def __init__(self, paths: tuple[Path, ...]) -> None:
        self.paths = paths
        super().__init__("Generated paths already exist.")


class UnsafeChangePathError(ChangeSetError):
    pass


class ChangeSetWriteError(ChangeSetError):
    pass


class OnboardingPublishError(RuntimeError):
    pass


class UnavailableOnboardingPublisher:
    def publish(
        self,
        request: OnboardingRequest,
        discovery: RepositoryDiscovery,
        changes: tuple[OnboardingChange, ...],
        draft_pull_request: DraftPullRequest,
    ) -> DraftPullRequestPublication:
        raise OnboardingPublishError(
            "No GitHub onboarding publisher was configured."
        )


WriteFile = Callable[[Path, str], None]


def _write_exclusive(path: Path, content: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(content)


class SafeChangeSetWriter:
    def __init__(self, *, write_file: WriteFile = _write_exclusive) -> None:
        self._write_file = write_file

    def prevalidate(
        self,
        repository_root: Path,
        contents: Mapping[Path, str],
    ) -> tuple[OnboardingChange, ...]:
        root = repository_root.resolve(strict=True)
        if not root.is_dir():
            raise UnsafeChangePathError("Repository root is not a directory.")

        conflicts: list[Path] = []
        resolved_destinations: set[Path] = set()
        for path in contents:
            destination = self._safe_destination(root, path)
            if destination in resolved_destinations:
                raise UnsafeChangePathError(
                    f"Generated path resolves more than once: {path.as_posix()}"
                )
            resolved_destinations.add(destination)
            if destination.is_symlink():
                raise UnsafeChangePathError(
                    f"Generated path is a symlink: {path.as_posix()}"
                )
            if destination.exists():
                conflicts.append(path)
        if conflicts:
            raise ChangeSetConflictError(tuple(conflicts))
        return tuple(
            OnboardingChange(
                path=path,
                content=content,
                status=ChangeStatus.PLANNED,
            )
            for path, content in contents.items()
        )

    def write(
        self,
        repository_root: Path,
        contents: Mapping[Path, str],
    ) -> tuple[OnboardingChange, ...]:
        self.prevalidate(repository_root, contents)
        root = repository_root.resolve(strict=True)
        attempted_files: list[Path] = []
        created_directories: list[Path] = []
        changes: list[OnboardingChange] = []
        try:
            for path, content in contents.items():
                destination = self._safe_destination(root, path)
                self._create_parents(
                    root,
                    destination.parent,
                    created_directories,
                )
                self._safe_destination(root, path)
                attempted_files.append(destination)
                self._write_file(destination, content)
                changes.append(
                    OnboardingChange(
                        path=path,
                        content=content,
                        status=ChangeStatus.CREATED,
                    )
                )
        except Exception as error:
            for created_file in reversed(attempted_files):
                try:
                    created_file.unlink()
                except OSError:
                    pass
            for directory in reversed(created_directories):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            raise ChangeSetWriteError(
                f"Generated change-set write failed: {error}"
            ) from error
        return tuple(changes)

    def _safe_destination(self, root: Path, path: Path) -> Path:
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise UnsafeChangePathError(
                f"Generated path is not repository-relative: {path}"
            )
        current = root
        for part in path.parent.parts:
            current = current / part
            if current.is_symlink():
                raise UnsafeChangePathError(
                    f"Generated path has a symlinked parent: {path.as_posix()}"
                )
            if current.exists() and not current.is_dir():
                raise UnsafeChangePathError(
                    f"Generated path parent is not a directory: {path.as_posix()}"
                )
        destination = root.joinpath(*path.parts)
        resolved = destination.resolve(strict=False)
        try:
            contained = os.path.commonpath((str(root), str(resolved))) == str(root)
        except ValueError:
            contained = False
        if not contained:
            raise UnsafeChangePathError(
                f"Generated path escapes the repository: {path.as_posix()}"
            )
        return destination

    def _create_parents(
        self,
        root: Path,
        parent: Path,
        created: list[Path],
    ) -> None:
        relative = parent.relative_to(root)
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise UnsafeChangePathError(
                    f"Generated path has a symlinked parent: {relative}"
                )
            if current.exists():
                if not current.is_dir():
                    raise UnsafeChangePathError(
                        f"Generated path parent is not a directory: {relative}"
                    )
                continue
            current.mkdir()
            created.append(current)


class GhOnboardingPublisher:
    def __init__(self, command_runner: CommandRunner) -> None:
        self._commands = command_runner

    def publish(
        self,
        request: OnboardingRequest,
        discovery: RepositoryDiscovery,
        changes: tuple[OnboardingChange, ...],
        draft_pull_request: DraftPullRequest,
    ) -> DraftPullRequestPublication:
        branch = f"foundry-opt/onboarding-{_branch_slug(request.target_name)}"
        paths = tuple(change.path.as_posix() for change in changes)
        try:
            self._run(("git", "switch", "-c", branch), request)
            self._run(("git", "add", "--", *paths), request)
            self._run(
                (
                    "git",
                    "commit",
                    "-m",
                    "Configure Foundry optimizer onboarding",
                ),
                request,
            )
            commit_sha = self._run(
                ("git", "rev-parse", "HEAD"),
                request,
            ).strip()
            self._run(
                ("git", "push", "--set-upstream", "origin", branch),
                request,
            )
            url = self._run(
                (
                    "gh",
                    "pr",
                    "create",
                    "--draft",
                    "--base",
                    discovery.default_branch,
                    "--head",
                    branch,
                    "--title",
                    draft_pull_request.title,
                    "--body",
                    draft_pull_request.body,
                ),
                request,
            ).strip()
        except CommandError as error:
            raise OnboardingPublishError(
                f"Draft pull request publication failed during {error.arguments[0]}."
            ) from error
        expected_prefix = f"https://github.com/{discovery.repository}/pull/"
        if not commit_sha or not url.startswith(expected_prefix):
            raise OnboardingPublishError(
                "GitHub did not return the expected draft pull request URL."
            )
        return DraftPullRequestPublication(
            url=url,
            branch=branch,
            commit_sha=commit_sha,
        )

    def _run(
        self,
        arguments: Sequence[str],
        request: OnboardingRequest,
    ) -> str:
        return self._commands.run(
            arguments,
            cwd=request.repository_root,
        ).stdout


def _branch_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    if not slug:
        raise OnboardingPublishError(
            "Target name cannot produce a safe onboarding branch name."
        )
    return slug
