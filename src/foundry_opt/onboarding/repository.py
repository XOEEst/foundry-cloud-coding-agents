from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import re
from uuid import uuid4

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


@dataclass
class _StagedFile:
    path: Path
    destination: Path
    temporary_path: Path
    temporary_name: str
    descriptor: int
    parent_descriptor: int | None
    identity: tuple[int, int]
    installed: bool = False


class SafeChangeSetWriter:
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
            if _is_link_like(destination):
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
        created_directories: list[Path] = []
        staged_files: list[_StagedFile] = []
        changes: list[OnboardingChange] = []
        conflict: Path | None = None
        try:
            for path, content in contents.items():
                destination = self._safe_destination(root, path)
                self._create_parents(
                    root,
                    destination.parent,
                    created_directories,
                )
                staged = self._stage_file(
                    root,
                    path,
                    destination,
                    content,
                )
                staged_files.append(staged)
            for staged in staged_files:
                try:
                    self._install(staged, root)
                except FileExistsError:
                    conflict = staged.path
                    raise
                changes.append(
                    OnboardingChange(
                        path=staged.path,
                        content=contents[staged.path],
                        status=ChangeStatus.CREATED,
                    )
                )
        except Exception as error:
            self._rollback(staged_files)
            for directory in reversed(created_directories):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            if conflict is not None:
                raise ChangeSetConflictError((conflict,)) from error
            raise ChangeSetWriteError(
                f"Generated change-set write failed: {error}"
            ) from error
        self._finish(staged_files)
        return tuple(changes)

    def _safe_destination(self, root: Path, path: Path) -> Path:
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise UnsafeChangePathError(
                f"Generated path is not repository-relative: {path}"
            )
        current = root
        for part in path.parent.parts:
            current = current / part
            if _is_link_like(current):
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
            if _is_link_like(current):
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

    def _stage_file(
        self,
        root: Path,
        path: Path,
        destination: Path,
        content: str,
    ) -> _StagedFile:
        self._safe_destination(root, path)
        parent_descriptor = self._open_parent_descriptor(
            root,
            destination.parent,
        )
        temporary_name = f".foundry-opt-{uuid4().hex}.tmp"
        temporary_path = destination.parent / temporary_name
        descriptor: int | None = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_BINARY", 0)
            if parent_descriptor is None:
                descriptor = os.open(temporary_path, flags, 0o600)
            else:
                descriptor = os.open(
                    temporary_name,
                    flags,
                    0o600,
                    dir_fd=parent_descriptor,
                )
            self._write_all(descriptor, content.encode("utf-8"))
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o644)
            os.fsync(descriptor)
            stat = os.fstat(descriptor)
            return _StagedFile(
                path=path,
                destination=destination,
                temporary_path=temporary_path,
                temporary_name=temporary_name,
                descriptor=descriptor,
                parent_descriptor=parent_descriptor,
                identity=(stat.st_dev, stat.st_ino),
            )
        except Exception:
            if descriptor is not None:
                os.close(descriptor)
            self._unlink_temporary(
                temporary_path,
                temporary_name,
                parent_descriptor,
            )
            if parent_descriptor is not None:
                os.close(parent_descriptor)
            raise

    def _install(self, staged: _StagedFile, root: Path) -> None:
        self._safe_destination(root, staged.path)
        if staged.parent_descriptor is None:
            os.link(
                staged.temporary_path,
                staged.destination,
                follow_symlinks=False,
            )
        else:
            os.link(
                staged.temporary_name,
                staged.destination.name,
                src_dir_fd=staged.parent_descriptor,
                dst_dir_fd=staged.parent_descriptor,
                follow_symlinks=False,
            )
        staged.installed = True

    def _rollback(self, staged_files: list[_StagedFile]) -> None:
        for staged in reversed(staged_files):
            if staged.installed and self._destination_is_staged(staged):
                self._unlink_destination(staged)
        self._finish(staged_files)

    def _finish(self, staged_files: list[_StagedFile]) -> None:
        for staged in reversed(staged_files):
            try:
                os.close(staged.descriptor)
            except OSError:
                pass
            self._unlink_temporary(
                staged.temporary_path,
                staged.temporary_name,
                staged.parent_descriptor,
            )
            if staged.parent_descriptor is not None:
                try:
                    os.close(staged.parent_descriptor)
                except OSError:
                    pass

    def _destination_is_staged(self, staged: _StagedFile) -> bool:
        try:
            if staged.parent_descriptor is None:
                stat = os.stat(
                    staged.destination,
                    follow_symlinks=False,
                )
            else:
                stat = os.stat(
                    staged.destination.name,
                    dir_fd=staged.parent_descriptor,
                    follow_symlinks=False,
                )
        except OSError:
            return False
        return (stat.st_dev, stat.st_ino) == staged.identity

    def _unlink_destination(self, staged: _StagedFile) -> None:
        try:
            if staged.parent_descriptor is None:
                staged.destination.unlink()
            else:
                os.unlink(
                    staged.destination.name,
                    dir_fd=staged.parent_descriptor,
                )
        except OSError:
            pass

    def _unlink_temporary(
        self,
        temporary_path: Path,
        temporary_name: str,
        parent_descriptor: int | None,
    ) -> None:
        try:
            if parent_descriptor is None:
                temporary_path.unlink()
            else:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
        except OSError:
            pass

    def _open_parent_descriptor(
        self,
        root: Path,
        parent: Path,
    ) -> int | None:
        required = (os.open, os.link, os.stat, os.unlink)
        if not all(operation in os.supports_dir_fd for operation in required):
            self._verify_resolved_parent(root, parent)
            return None

        flags = os.O_RDONLY
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(root, flags)
        try:
            for part in parent.relative_to(root).parts:
                next_descriptor = os.open(
                    part,
                    flags,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = next_descriptor
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _verify_resolved_parent(self, root: Path, parent: Path) -> None:
        current = root
        for part in parent.relative_to(root).parts:
            current = current / part
            if _is_link_like(current):
                raise UnsafeChangePathError(
                    f"Generated path has a symlinked parent: {parent}"
                )
        resolved = parent.resolve(strict=True)
        try:
            contained = os.path.commonpath((str(root), str(resolved))) == str(root)
        except ValueError:
            contained = False
        if not contained:
            raise UnsafeChangePathError(
                f"Generated path parent escapes the repository: {parent}"
            )

    def _write_all(self, descriptor: int, content: bytes) -> None:
        remaining = memoryview(content)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("temporary file write made no progress")
            remaining = remaining[written:]


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


def _is_link_like(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(
        callable(is_junction) and is_junction()
    )
