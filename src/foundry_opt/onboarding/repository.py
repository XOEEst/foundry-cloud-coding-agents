from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
import re
from uuid import uuid4

from foundry_opt.adapters.commands import (
    CommandError,
    SubprocessCommandRunner,
)
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
        super().__init__("Generated paths already exist in HEAD.")


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


class GitChangeSetWriter:
    def __init__(self, command_runner: CommandRunner | None = None) -> None:
        self._commands = command_runner or SubprocessCommandRunner()

    def prevalidate(
        self,
        repository_root: Path,
        contents: Mapping[Path, str],
    ) -> tuple[OnboardingChange, ...]:
        normalized = self._validate_paths(contents)
        self._require_clean_head(repository_root)
        base_commit = self._run(
            ("git", "rev-parse", "--verify", "HEAD"),
            repository_root,
        ).strip()
        self._require_absent_from_tree(
            repository_root,
            base_commit,
            normalized,
        )
        return tuple(
            OnboardingChange(
                path=path,
                content=contents[path],
                status=ChangeStatus.PLANNED,
                base_commit=base_commit,
            )
            for path in normalized
        )

    def write(
        self,
        repository_root: Path,
        contents: Mapping[Path, str],
    ) -> tuple[OnboardingChange, ...]:
        normalized = self._validate_paths(contents)
        self._require_clean_head(repository_root)
        base_commit = self._run(
            ("git", "rev-parse", "--verify", "HEAD"),
            repository_root,
        ).strip()
        self._require_absent_from_tree(
            repository_root,
            base_commit,
            normalized,
        )
        index_path = self._temporary_index_path(repository_root)
        index_environment = {"GIT_INDEX_FILE": str(index_path)}
        failure: ChangeSetError | None = None
        commit_sha = ""
        try:
            self._run(
                ("git", "read-tree", base_commit),
                repository_root,
                environment=index_environment,
            )
            for path in normalized:
                blob = self._run(
                    ("git", "hash-object", "-w", "--stdin"),
                    repository_root,
                    input_bytes=contents[path].encode("utf-8"),
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
                    environment=index_environment,
                )
            tree = self._run(
                ("git", "write-tree"),
                repository_root,
                environment=index_environment,
            ).strip()
            commit_sha = self._run(
                (
                    "git",
                    "commit-tree",
                    tree,
                    "-p",
                    base_commit,
                    "-m",
                    "Configure Foundry optimizer onboarding",
                ),
                repository_root,
            ).strip()
            current_head = self._run(
                ("git", "rev-parse", "--verify", "HEAD"),
                repository_root,
            ).strip()
            if current_head != base_commit:
                raise ChangeSetWriteError(
                    "HEAD changed while the onboarding commit was assembled."
                )
        except ChangeSetError as error:
            failure = error
        residuals, cleanup_errors = _remove_temporary_index(index_path)
        if failure is not None:
            if residuals or cleanup_errors:
                raise ChangeSetWriteError(
                    str(failure),
                    residual_paths=tuple(
                        dict.fromkeys(
                            (
                                *getattr(failure, "residual_paths", ()),
                                *residuals,
                            )
                        )
                    ),
                    cleanup_errors=(
                        *getattr(failure, "cleanup_errors", ()),
                        *cleanup_errors,
                    ),
                ) from failure
            raise failure
        if residuals or cleanup_errors:
            raise ChangeSetWriteError(
                "The onboarding commit was created but temporary index cleanup "
                "was incomplete.",
                residual_paths=residuals,
                cleanup_errors=cleanup_errors,
            )
        return tuple(
            OnboardingChange(
                path=path,
                content=contents[path],
                status=ChangeStatus.CREATED,
                base_commit=base_commit,
                commit_sha=commit_sha,
            )
            for path in normalized
        )

    def _validate_paths(
        self,
        contents: Mapping[Path, str],
    ) -> tuple[Path, ...]:
        if not contents:
            raise UnsafeChangePathError("Generated change set is empty.")
        normalized: list[Path] = []
        portable_names: set[str] = set()
        for path in contents:
            if (
                path.is_absolute()
                or not path.parts
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                raise UnsafeChangePathError(
                    f"Generated path is not repository-relative: {path}"
                )
            portable = path.as_posix().casefold()
            if portable in portable_names:
                raise UnsafeChangePathError(
                    f"Generated path is not portable: {path.as_posix()}"
                )
            portable_names.add(portable)
            normalized.append(path)
        return tuple(normalized)

    def _require_clean_head(self, repository_root: Path) -> None:
        status = self._run(
            (
                "git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ),
            repository_root,
        )
        if status:
            raise ChangeSetWriteError(
                "Git change-set assembly requires a clean HEAD and worktree."
            )

    def _require_absent_from_tree(
        self,
        repository_root: Path,
        base_commit: str,
        paths: tuple[Path, ...],
    ) -> None:
        prefixes: dict[str, Path] = {}
        destinations = {path.as_posix(): path for path in paths}
        for path in paths:
            parts: list[str] = []
            for part in path.parts:
                parts.append(part)
                prefixes["/".join(parts)] = path
        output = self._run(
            (
                "git",
                "ls-tree",
                "-z",
                base_commit,
                "--",
                *prefixes,
            ),
            repository_root,
        )
        conflicts: set[Path] = set()
        for entry in output.split("\0"):
            if not entry:
                continue
            metadata, name = entry.split("\t", 1)
            object_type = metadata.split()[1]
            if name in destinations or object_type != "tree":
                owner = destinations.get(name) or prefixes[name]
                conflicts.add(owner)
        if conflicts:
            raise ChangeSetConflictError(
                tuple(path for path in paths if path in conflicts)
            )

    def _temporary_index_path(self, repository_root: Path) -> Path:
        value = self._run(
            (
                "git",
                "rev-parse",
                "--git-path",
                f"foundry-opt-index-{uuid4().hex}",
            ),
            repository_root,
        ).strip()
        path = Path(value)
        return path if path.is_absolute() else repository_root / path

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
            operation = arguments[1] if len(arguments) > 1 else "command"
            raise ChangeSetWriteError(
                f"Git change-set command failed during {operation}."
            ) from error


SafeChangeSetWriter = GitChangeSetWriter


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
        base_commit, commit_sha = _prepared_commit(changes)
        branch = f"foundry-opt/onboarding-{_branch_slug(request.target_name)}"
        branch_ref = f"refs/heads/{branch}"
        phase = "inspect"
        local_created = False
        pushed = False
        try:
            current_head = self._run(
                ("git", "rev-parse", "--verify", "HEAD"),
                request,
            ).strip()
            status = self._run(
                (
                    "git",
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                ),
                request,
            )
            if current_head != base_commit or status:
                raise _PublicationFailure(
                    "The clean base HEAD changed after commit assembly."
                )
            if self._run(
                (
                    "git",
                    "for-each-ref",
                    "--format=%(objectname)",
                    branch_ref,
                ),
                request,
            ).strip():
                raise _PublicationFailure(
                    "The onboarding branch already exists locally."
                )
            if self._remote_sha(request, branch_ref):
                raise _PublicationFailure(
                    "The onboarding branch already exists remotely."
                )
            phase = "local_ref"
            self._run(
                (
                    "git",
                    "update-ref",
                    branch_ref,
                    commit_sha,
                    "0" * 40,
                ),
                request,
            )
            local_created = True
            phase = "push"
            self._run(
                (
                    "git",
                    "push",
                    f"--force-with-lease={branch_ref}:",
                    "origin",
                    f"{commit_sha}:{branch_ref}",
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
            if not url.startswith(expected_prefix):
                raise _PublicationFailure(
                    "GitHub did not return the expected draft pull request URL."
                )
        except (CommandError, _PublicationFailure) as error:
            residual_state = self._compensate(
                request,
                branch_ref=branch_ref,
                commit_sha=commit_sha,
                local_created=local_created,
                pushed=pushed,
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
        branch_ref: str,
        commit_sha: str,
        local_created: bool,
        pushed: bool,
    ) -> tuple[str, ...]:
        residuals: list[str] = []
        remote_sha: str | None = commit_sha if pushed else None
        if not pushed:
            try:
                remote_sha = self._remote_sha(request, branch_ref)
            except CommandError:
                residuals.append(
                    f"remote ref {branch_ref} could not be verified"
                )
        if remote_sha == commit_sha:
            try:
                self._run(
                    (
                        "git",
                        "push",
                        f"--force-with-lease={branch_ref}:{commit_sha}",
                        "origin",
                        f":{branch_ref}",
                    ),
                    request,
                )
            except CommandError:
                residuals.append(
                    f"remote ref {branch_ref} may remain at {commit_sha}"
                )
        elif remote_sha:
            residuals.append(
                f"remote ref {branch_ref} is owned by another commit "
                f"{remote_sha} and was not deleted"
            )
        if local_created:
            try:
                self._run(
                    (
                        "git",
                        "update-ref",
                        "-d",
                        branch_ref,
                        commit_sha,
                    ),
                    request,
                )
            except CommandError:
                residuals.append(
                    f"local ref {branch_ref} may remain at {commit_sha}"
                )
        return tuple(residuals)

    def _remote_sha(
        self,
        request: OnboardingRequest,
        branch_ref: str,
    ) -> str | None:
        output = self._run(
            (
                "git",
                "ls-remote",
                "--heads",
                "origin",
                branch_ref,
            ),
            request,
        ).strip()
        return output.split()[0] if output else None

    def _run(
        self,
        arguments: Sequence[str],
        request: OnboardingRequest,
    ) -> str:
        return self._commands.run(
            arguments,
            cwd=request.repository_root,
        ).stdout


class _PublicationFailure(RuntimeError):
    pass


def _prepared_commit(
    changes: tuple[OnboardingChange, ...],
) -> tuple[str, str]:
    commits = {change.commit_sha for change in changes}
    bases = {change.base_commit for change in changes}
    if (
        len(commits) != 1
        or None in commits
        or "" in commits
        or len(bases) != 1
        or None in bases
        or "" in bases
    ):
        raise OnboardingPublishError(
            "The change set does not identify one prepared Git commit and base.",
            phase="assemble",
        )
    base_commit = next(iter(bases))
    commit_sha = next(iter(commits))
    assert isinstance(base_commit, str)
    assert isinstance(commit_sha, str)
    return base_commit, commit_sha


def _remove_temporary_index(
    index_path: Path,
) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    residuals: list[Path] = []
    errors: list[str] = []
    for path in (index_path, Path(f"{index_path}.lock")):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as error:
            residuals.append(path)
            errors.append(f"{path}: {error}")
    return tuple(residuals), tuple(errors)


def _branch_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    if not slug:
        raise OnboardingPublishError(
            "The target name cannot produce a Git branch name.",
            phase="assemble",
        )
    return slug
