from __future__ import annotations

from collections.abc import Mapping, Sequence
import ctypes
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import re
import stat
import sys
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
    def __init__(
        self,
        message: str,
        *,
        residual_paths: tuple[Path, ...] = (),
        cleanup_errors: tuple[str, ...] = (),
    ) -> None:
        self.residual_paths = residual_paths
        self.cleanup_errors = cleanup_errors
        super().__init__(message)


class AtomicWriteUnsupportedError(ChangeSetError):
    pass


class OnboardingPublishError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        phase: str,
        residual_state: tuple[str, ...] = (),
    ) -> None:
        self.phase = phase
        self.residual_state = residual_state
        super().__init__(message)


class UnavailableOnboardingPublisher:
    def publish(
        self,
        request: OnboardingRequest,
        discovery: RepositoryDiscovery,
        changes: tuple[OnboardingChange, ...],
        draft_pull_request: DraftPullRequest,
    ) -> DraftPullRequestPublication:
        raise OnboardingPublishError(
            "No GitHub onboarding publisher was configured.",
            phase="assemble",
        )


@dataclass
class _DirectoryHandle:
    parts: tuple[str, ...]
    descriptor: int
    parent: _DirectoryHandle | None
    name: str | None
    created: bool
    identity: tuple[int, int]


@dataclass
class _StagedFile:
    path: Path
    parent: _DirectoryHandle
    destination_name: str
    temporary_name: str
    descriptor: int | None
    identity: tuple[int, int]
    installed: bool = False


class SafeChangeSetWriter:
    def prevalidate(
        self,
        repository_root: Path,
        contents: Mapping[Path, str],
    ) -> tuple[OnboardingChange, ...]:
        if _is_link_like(repository_root):
            raise UnsafeChangePathError(
                "Repository root must not be a symlink or reparse point."
            )
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
        if not _secure_posix_writes_available():
            raise AtomicWriteUnsupportedError(
                "This platform lacks retained no-follow directory traversal "
                "and ownership-bound atomic cleanup; refusing to write."
            )
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
        tree = _DirectoryTree(root)
        staged_files: list[_StagedFile] = []
        changes: list[OnboardingChange] = []
        conflict: Path | None = None
        try:
            for path, content in contents.items():
                parent = tree.parent(path.parent)
                try:
                    self._ensure_destination_absent(parent, path.name)
                except FileExistsError:
                    conflict = path
                    raise
                staged = self._stage_file(
                    path,
                    parent,
                    content,
                )
                staged_files.append(staged)
            for staged in staged_files:
                try:
                    self._install(staged)
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
            residuals, cleanup_errors = self._rollback(staged_files, tree)
            if isinstance(error, ChangeSetWriteError):
                residuals = tuple(
                    dict.fromkeys((*error.residual_paths, *residuals))
                )
                cleanup_errors = (
                    *error.cleanup_errors,
                    *cleanup_errors,
                )
            if conflict is not None:
                if residuals or cleanup_errors:
                    raise ChangeSetWriteError(
                        "Destination race occurred and compensation was "
                        "incomplete.",
                        residual_paths=residuals,
                        cleanup_errors=cleanup_errors,
                    ) from error
                raise ChangeSetConflictError((conflict,)) from error
            raise ChangeSetWriteError(
                f"Generated change-set write failed: {error}",
                residual_paths=residuals,
                cleanup_errors=cleanup_errors,
            ) from error
        residuals, cleanup_errors = self._finish(
            staged_files,
            tree,
            close_tree=False,
        )
        if residuals or cleanup_errors:
            rollback_residuals, rollback_errors = self._rollback(
                staged_files,
                tree,
            )
            raise ChangeSetWriteError(
                "Generated files were installed but cleanup was incomplete.",
                residual_paths=tuple(
                    dict.fromkeys((*residuals, *rollback_residuals))
                ),
                cleanup_errors=(*cleanup_errors, *rollback_errors),
            )
        tree_residuals, tree_errors = tree.cleanup(remove_created=False)
        if tree_residuals or tree_errors:
            raise ChangeSetWriteError(
                "Generated files were installed but directory handle cleanup "
                "was incomplete.",
                residual_paths=tuple(change.path for change in changes),
                cleanup_errors=tree_errors,
            )
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

    def _stage_file(
        self,
        path: Path,
        parent: _DirectoryHandle,
        content: str,
    ) -> _StagedFile:
        temporary_name = f".foundry-opt-{uuid4().hex}.tmp"
        descriptor: int | None = None
        identity: tuple[int, int] | None = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= os.O_NOFOLLOW
            flags |= getattr(os, "O_BINARY", 0)
            descriptor = os.open(
                temporary_name,
                flags,
                0o600,
                dir_fd=parent.descriptor,
            )
            opened_stat = os.fstat(descriptor)
            identity = (opened_stat.st_dev, opened_stat.st_ino)
            self._write_all(descriptor, content.encode("utf-8"))
            os.fchmod(descriptor, 0o644)
            os.fsync(descriptor)
            file_stat = os.fstat(descriptor)
            return _StagedFile(
                path=path,
                parent=parent,
                destination_name=path.name,
                temporary_name=temporary_name,
                descriptor=descriptor,
                identity=(file_stat.st_dev, file_stat.st_ino),
            )
        except Exception as error:
            cleanup_errors: list[str] = []
            residual_paths: list[Path] = []
            if identity is not None:
                removed, residual, cleanup_error = self._remove_owned_file(
                    parent,
                    temporary_name,
                    identity,
                    path.parent / temporary_name,
                    prefix="staging",
                )
                if not removed and residual is not None:
                    residual_paths.append(residual)
                if cleanup_error is not None:
                    cleanup_errors.append(cleanup_error)
            else:
                residual_paths.append(path.parent / temporary_name)
                cleanup_errors.append(
                    "temporary file identity could not be established"
                )
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError as close_error:
                    cleanup_errors.append(str(close_error))
            if cleanup_errors:
                raise ChangeSetWriteError(
                    f"Temporary file staging failed: {error}",
                    residual_paths=tuple(residual_paths),
                    cleanup_errors=tuple(cleanup_errors),
                ) from error
            raise

    def _ensure_destination_absent(
        self,
        parent: _DirectoryHandle,
        name: str,
    ) -> None:
        try:
            os.stat(
                name,
                dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        raise FileExistsError(name)

    def _install(self, staged: _StagedFile) -> None:
        os.link(
            staged.temporary_name,
            staged.destination_name,
            src_dir_fd=staged.parent.descriptor,
            dst_dir_fd=staged.parent.descriptor,
            follow_symlinks=False,
        )
        staged.installed = True

    def _rollback(
        self,
        staged_files: list[_StagedFile],
        tree: _DirectoryTree,
        *,
        finish: bool = True,
    ) -> tuple[tuple[Path, ...], tuple[str, ...]]:
        residuals: list[Path] = []
        errors: list[str] = []
        for staged in reversed(staged_files):
            if not staged.installed:
                continue
            removed, residual, cleanup_error = self._remove_owned_file(
                staged.parent,
                staged.destination_name,
                staged.identity,
                staged.path,
                prefix="rollback",
            )
            if removed:
                staged.installed = False
            if residual is not None:
                residuals.append(residual)
            if cleanup_error is not None:
                errors.append(cleanup_error)
        if finish:
            finish_residuals, finish_errors = self._finish(
                staged_files,
                tree,
                cleanup_directories=True,
            )
            residuals.extend(finish_residuals)
            errors.extend(finish_errors)
        return tuple(dict.fromkeys(residuals)), tuple(errors)

    def _finish(
        self,
        staged_files: list[_StagedFile],
        tree: _DirectoryTree,
        *,
        cleanup_directories: bool = False,
        close_tree: bool = True,
    ) -> tuple[tuple[Path, ...], tuple[str, ...]]:
        residuals: list[Path] = []
        errors: list[str] = []
        for staged in reversed(staged_files):
            removed, residual, cleanup_error = self._remove_owned_file(
                staged.parent,
                staged.temporary_name,
                staged.identity,
                staged.path.parent / staged.temporary_name,
                prefix="temp-cleanup",
            )
            if residual is not None:
                residuals.append(residual)
            if cleanup_error is not None:
                errors.append(cleanup_error)
            if staged.descriptor is not None:
                try:
                    os.close(staged.descriptor)
                    staged.descriptor = None
                except OSError as close_error:
                    errors.append(
                        f"{staged.path}: close failed: {close_error}"
                    )
        if close_tree:
            directory_residuals, directory_errors = tree.cleanup(
                remove_created=cleanup_directories
            )
            residuals.extend(directory_residuals)
            errors.extend(directory_errors)
        return tuple(dict.fromkeys(residuals)), tuple(errors)

    def _remove_owned_file(
        self,
        parent: _DirectoryHandle,
        name: str,
        identity: tuple[int, int],
        logical_path: Path,
        *,
        prefix: str,
    ) -> tuple[bool, Path | None, str | None]:
        cleanup_name = f".foundry-opt-{prefix}-{uuid4().hex}.tmp"
        cleanup_path = logical_path.parent / cleanup_name
        try:
            _rename_noreplace(
                parent.descriptor,
                name,
                parent.descriptor,
                cleanup_name,
            )
        except FileNotFoundError:
            return True, None, None
        except OSError as error:
            return False, logical_path, f"{logical_path}: {error}"
        try:
            claimed = os.stat(
                cleanup_name,
                dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
            if (claimed.st_dev, claimed.st_ino) != identity:
                try:
                    _rename_noreplace(
                        parent.descriptor,
                        cleanup_name,
                        parent.descriptor,
                        name,
                    )
                except OSError as restore_error:
                    return (
                        False,
                        cleanup_path,
                        f"{logical_path}: ownership changed and restoration "
                        f"failed: {restore_error}",
                    )
                return (
                    False,
                    None,
                    f"{logical_path}: ownership changed; foreign entry "
                    "was restored",
                )
            os.unlink(cleanup_name, dir_fd=parent.descriptor)
        except OSError as error:
            return False, cleanup_path, f"{logical_path}: {error}"
        return True, None, None

    def _write_all(self, descriptor: int, content: bytes) -> None:
        remaining = memoryview(content)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("temporary file write made no progress")
            remaining = remaining[written:]


class _DirectoryTree:
    def __init__(self, root: Path) -> None:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(root, flags)
        root_stat = os.fstat(descriptor)
        if not stat.S_ISDIR(root_stat.st_mode):
            os.close(descriptor)
            raise UnsafeChangePathError(
                "Repository root handle is not a directory."
            )
        root_handle = _DirectoryHandle(
            parts=(),
            descriptor=descriptor,
            parent=None,
            name=None,
            created=False,
            identity=(root_stat.st_dev, root_stat.st_ino),
        )
        self._handles: dict[tuple[str, ...], _DirectoryHandle] = {
            (): root_handle
        }

    def parent(self, relative: Path) -> _DirectoryHandle:
        parts: tuple[str, ...] = ()
        current = self._handles[parts]
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        for part in relative.parts:
            parts = (*parts, part)
            cached = self._handles.get(parts)
            if cached is not None:
                current = cached
                continue
            created = False
            try:
                descriptor = os.open(
                    part,
                    flags,
                    dir_fd=current.descriptor,
                )
            except FileNotFoundError:
                temporary_name = (
                    f".foundry-opt-directory-{uuid4().hex}.tmp"
                )
                temporary_identity: tuple[int, int] | None = None
                temporary_descriptor: int | None = None
                try:
                    os.mkdir(
                        temporary_name,
                        0o755,
                        dir_fd=current.descriptor,
                    )
                    temporary_descriptor = os.open(
                        temporary_name,
                        flags,
                        dir_fd=current.descriptor,
                    )
                    descriptor = temporary_descriptor
                    directory_stat = os.fstat(temporary_descriptor)
                    temporary_identity = (
                        directory_stat.st_dev,
                        directory_stat.st_ino,
                    )
                    try:
                        _rename_noreplace(
                            current.descriptor,
                            temporary_name,
                            current.descriptor,
                            part,
                        )
                        created = True
                    except FileExistsError:
                        residual, cleanup_error = (
                            self._remove_owned_directory_entry(
                                current,
                                temporary_name,
                                temporary_identity,
                                Path(*parts[:-1], temporary_name),
                            )
                        )
                        os.close(descriptor)
                        temporary_descriptor = None
                        if residual is not None or cleanup_error is not None:
                            raise ChangeSetWriteError(
                                "Temporary generated directory cleanup "
                                "failed.",
                                residual_paths=(
                                    (residual,)
                                    if residual is not None
                                    else ()
                                ),
                                cleanup_errors=(
                                    (cleanup_error,)
                                    if cleanup_error is not None
                                    else ()
                                ),
                            )
                        descriptor = os.open(
                            part,
                            flags,
                            dir_fd=current.descriptor,
                        )
                        created = False
                except ChangeSetWriteError:
                    raise
                except OSError as open_error:
                    cleanup_errors = [str(open_error)]
                    residuals: tuple[Path, ...] = (
                        (Path(*parts),)
                        if created
                        else (Path(*parts[:-1], temporary_name),)
                    )
                    if temporary_identity is not None and not created:
                        residual, cleanup_error = (
                            self._remove_owned_directory_entry(
                                current,
                                temporary_name,
                                temporary_identity,
                                Path(*parts[:-1], temporary_name),
                            )
                        )
                        residuals = (
                            (residual,)
                            if residual is not None
                            else ()
                        )
                        if cleanup_error is not None:
                            cleanup_errors.append(cleanup_error)
                    if temporary_descriptor is not None:
                        try:
                            os.close(temporary_descriptor)
                        except OSError as close_error:
                            cleanup_errors.append(str(close_error))
                    raise ChangeSetWriteError(
                        "A generated parent changed during retained-handle "
                        "traversal.",
                        residual_paths=residuals,
                        cleanup_errors=tuple(cleanup_errors),
                    ) from open_error
            except OSError as open_error:
                if open_error.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise UnsafeChangePathError(
                        "Generated path parent is a symlink or not a directory: "
                        f"{relative.as_posix()}"
                    ) from open_error
                raise
            directory_stat = os.fstat(descriptor)
            current = _DirectoryHandle(
                parts=parts,
                descriptor=descriptor,
                parent=current,
                name=part,
                created=created,
                identity=(directory_stat.st_dev, directory_stat.st_ino),
            )
            self._handles[parts] = current
        return current

    def cleanup(
        self,
        *,
        remove_created: bool,
    ) -> tuple[tuple[Path, ...], tuple[str, ...]]:
        residuals: list[Path] = []
        errors: list[str] = []
        handles = sorted(
            self._handles.values(),
            key=lambda handle: len(handle.parts),
            reverse=True,
        )
        for handle in handles:
            if (
                remove_created
                and handle.created
                and handle.parent is not None
                and handle.name is not None
            ):
                residual, cleanup_error = (
                    self._remove_owned_directory_entry(
                        handle.parent,
                        handle.name,
                        handle.identity,
                        Path(*handle.parts),
                    )
                )
                if residual is not None:
                    residuals.append(residual)
                if cleanup_error is not None:
                    errors.append(cleanup_error)
            try:
                os.close(handle.descriptor)
            except OSError as close_error:
                errors.append(
                    f"directory handle {Path(*handle.parts)}: {close_error}"
                )
        return tuple(residuals), tuple(errors)

    def _remove_owned_directory_entry(
        self,
        parent: _DirectoryHandle,
        name: str,
        identity: tuple[int, int],
        logical_path: Path,
    ) -> tuple[Path | None, str | None]:
        cleanup_name = f".foundry-opt-directory-{uuid4().hex}.tmp"
        cleanup_path = logical_path.parent / cleanup_name
        try:
            _rename_noreplace(
                parent.descriptor,
                name,
                parent.descriptor,
                cleanup_name,
            )
        except FileNotFoundError:
            return None, None
        except OSError as error:
            return logical_path, f"{logical_path}: {error}"
        try:
            claimed = os.stat(
                cleanup_name,
                dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
            if (claimed.st_dev, claimed.st_ino) != identity:
                try:
                    _rename_noreplace(
                        parent.descriptor,
                        cleanup_name,
                        parent.descriptor,
                        name,
                    )
                except OSError as restore_error:
                    return (
                        cleanup_path,
                        f"{logical_path}: directory ownership changed and "
                        f"restoration failed: {restore_error}",
                    )
                return (
                    None,
                    f"{logical_path}: directory ownership changed; foreign "
                    "entry was restored",
                )
            os.rmdir(cleanup_name, dir_fd=parent.descriptor)
        except OSError as error:
            return cleanup_path, f"{logical_path}: {error}"
        return None, None


_RENAME_NOREPLACE = 1
_LIBC = (
    ctypes.CDLL(None, use_errno=True)
    if os.name == "posix"
    else None
)
_RENAMEAT2 = (
    getattr(_LIBC, "renameat2", None)
    if _LIBC is not None
    else None
)
if _RENAMEAT2 is not None:
    _RENAMEAT2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    _RENAMEAT2.restype = ctypes.c_int


def _rename_noreplace(
    old_directory: int,
    old_name: str,
    new_directory: int,
    new_name: str,
) -> None:
    if _RENAMEAT2 is None:
        raise OSError(
            errno.ENOTSUP,
            "renameat2(RENAME_NOREPLACE) is unavailable",
        )
    result = _RENAMEAT2(
        old_directory,
        os.fsencode(old_name),
        new_directory,
        os.fsencode(new_name),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _secure_posix_writes_available() -> bool:
    required = (
        os.open,
        os.mkdir,
        os.link,
        os.stat,
        os.unlink,
        os.rmdir,
    )
    return (
        os.name == "posix"
        and sys.platform.startswith("linux")
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
        and all(operation in os.supports_dir_fd for operation in required)
        and _RENAMEAT2 is not None
    )


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
        phase = "inspect"
        switched = False
        staged = False
        committed = False
        pushed = False
        commit_sha = ""
        original_branch = ""
        original_commit = ""
        try:
            original_branch = self._run(
                ("git", "branch", "--show-current"),
                request,
            ).strip()
            original_commit = self._run(
                ("git", "rev-parse", "HEAD"),
                request,
            ).strip()
            if self._run(
                ("git", "branch", "--list", branch),
                request,
            ).strip():
                raise _PublicationFailure(
                    "The onboarding branch already exists locally."
                )
            if self._run(
                (
                    "git",
                    "ls-remote",
                    "--heads",
                    "origin",
                    f"refs/heads/{branch}",
                ),
                request,
            ).strip():
                raise _PublicationFailure(
                    "The onboarding branch already exists remotely."
                )
            phase = "branch"
            self._run(("git", "switch", "-c", branch), request)
            switched = True
            phase = "stage"
            staged = True
            self._run(("git", "add", "--", *paths), request)
            phase = "commit"
            self._run(
                (
                    "git",
                    "commit",
                    "-m",
                    "Configure Foundry optimizer onboarding",
                ),
                request,
            )
            committed = True
            commit_sha = self._run(
                ("git", "rev-parse", "HEAD"),
                request,
            ).strip()
            phase = "push"
            self._run(
                (
                    "git",
                    "push",
                    "--set-upstream",
                    f"--force-with-lease=refs/heads/{branch}:",
                    "origin",
                    branch,
                ),
                request,
            )
            pushed = True
            phase = "draft_pr"
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
            expected_prefix = (
                f"https://github.com/{discovery.repository}/pull/"
            )
            if not commit_sha or not url.startswith(expected_prefix):
                raise _PublicationFailure(
                    "GitHub did not return the expected draft pull request URL."
                )
        except (CommandError, _PublicationFailure) as error:
            residual_state = self._compensate(
                request,
                branch=branch,
                paths=paths,
                original_branch=original_branch,
                original_commit=original_commit,
                switched=switched,
                staged=staged,
                committed=committed,
                pushed=pushed,
                commit_sha=commit_sha,
            )
            raise OnboardingPublishError(
                f"Draft pull request publication failed during {phase}: {error}",
                phase=phase,
                residual_state=residual_state,
            ) from error
        return DraftPullRequestPublication(
            url=url,
            branch=branch,
            commit_sha=commit_sha,
        )

    def _compensate(
        self,
        request: OnboardingRequest,
        *,
        branch: str,
        paths: tuple[str, ...],
        original_branch: str,
        original_commit: str,
        switched: bool,
        staged: bool,
        committed: bool,
        pushed: bool,
        commit_sha: str,
    ) -> tuple[str, ...]:
        residuals: list[str] = []
        remote_is_invocation = pushed
        if commit_sha and not pushed:
            try:
                remote = self._run(
                    (
                        "git",
                        "ls-remote",
                        "--heads",
                        "origin",
                        f"refs/heads/{branch}",
                    ),
                    request,
                ).strip()
                remote_is_invocation = bool(
                    remote and remote.split()[0] == commit_sha
                )
            except (CommandError, IndexError):
                residuals.append(
                    f"remote branch {branch} state could not be verified"
                )
        if remote_is_invocation:
            try:
                self._run(
                    (
                        "git",
                        "push",
                        "--force-with-lease="
                        f"refs/heads/{branch}:{commit_sha}",
                        "origin",
                        f":refs/heads/{branch}",
                    ),
                    request,
                )
            except CommandError:
                residuals.append(
                    f"remote branch {branch} may remain at {commit_sha}"
                )
        if not switched:
            try:
                switched = (
                    self._run(
                        ("git", "branch", "--show-current"),
                        request,
                    ).strip()
                    == branch
                )
            except CommandError:
                residuals.append(
                    "current checkout could not be verified after failure"
                )
        if staged and not committed:
            try:
                self._run(("git", "reset", "--", *paths), request)
            except CommandError:
                residuals.append(
                    "generated paths may remain staged: " + ", ".join(paths)
                )
        if not committed:
            try:
                self._run(("git", "clean", "-f", "--", *paths), request)
            except CommandError:
                residuals.append(
                    "generated paths may remain: " + ", ".join(paths)
                )
        restored = not switched
        if switched:
            checkout = (
                ("git", "switch", original_branch)
                if original_branch
                else ("git", "switch", "--detach", original_commit)
            )
            try:
                self._run(checkout, request)
                restored = True
            except CommandError:
                residuals.append(
                    f"checkout may remain on invocation branch {branch}"
                )
        if switched and restored:
            try:
                self._run(("git", "branch", "-D", branch), request)
            except CommandError:
                residuals.append(f"local branch {branch} may remain")
        return tuple(residuals)

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
            "Target name cannot produce a safe onboarding branch name.",
            phase="inspect",
        )
    return slug


def _is_link_like(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(
        callable(is_junction) and is_junction()
    )


class _PublicationFailure(RuntimeError):
    pass
