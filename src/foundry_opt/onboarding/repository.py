from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
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


_MANIFEST_PATH = Path(".github/foundry-optimizer.generated.json")
_LEGACY_PACKAGE_ENV_PATHS = frozenset(
    {
        ".github/workflows/foundry-optimization-issue-intake.yml",
    }
)
_LEGACY_PACKAGE_INSTALL_PATHS = frozenset(
    {
        ".github/workflows/copilot-setup-steps.yml",
        ".github/workflows/foundry-exact-candidate-check.yml",
        ".github/workflows/foundry-optimization-control.yml",
        ".github/workflows/foundry-post-deployment-check.yml",
    }
)


def normalize_legacy_generated_content(
    path: Path,
    content: str,
) -> str | None:
    name = path.as_posix()
    lines = content.splitlines(keepends=True)
    if name in _LEGACY_PACKAGE_ENV_PATHS:
        matches: list[tuple[int, str, str]] = []
        for index, line in enumerate(lines):
            ending = "\n" if line.endswith("\n") else ""
            value = line.removesuffix("\n").removesuffix("\r")
            match = re.fullmatch(
                r"(\s*OPTIMIZER_PACKAGE:\s*)(\"(?:[^\"\\]|\\.)*\")",
                value,
            )
            if match is None:
                continue
            try:
                install = json.loads(match.group(2))
            except json.JSONDecodeError:
                return None
            if not _is_pinned_product_install(install):
                return None
            matches.append((index, match.group(1), ending))
        if len(matches) != 1:
            return None
        index, prefix, ending = matches[0]
        lines[index] = (
            f"{prefix}<PINNED_PRODUCT_INSTALL>{ending}"
        )
        return "".join(lines)
    if name in _LEGACY_PACKAGE_INSTALL_PATHS:
        matches = []
        for index, line in enumerate(lines):
            ending = "\n" if line.endswith("\n") else ""
            value = line.removesuffix("\n").removesuffix("\r")
            match = re.fullmatch(
                r"(\s*run:\s*uv tool install\s+)'([^']+)'",
                value,
            )
            if match is None:
                continue
            if not _is_pinned_product_install(match.group(2)):
                return None
            matches.append((index, match.group(1), ending))
        if len(matches) != 1:
            return None
        index, prefix, ending = matches[0]
        lines[index] = (
            f"{prefix}<PINNED_PRODUCT_INSTALL>{ending}"
        )
        return "".join(lines)
    return None


def _is_pinned_product_install(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return bool(
        re.fullmatch(
            r"foundry-cloud-coding-agent=="
            r"[A-Za-z0-9][A-Za-z0-9._+-]*",
            value,
        )
        or re.fullmatch(
            r"foundry-cloud-coding-agent\s*@\s*"
            r"git\+https://github\.com/"
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?"
            r"@[0-9a-fA-F]{40}",
            value,
        )
    )


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
        obsolete = self._obsolete_hashes(contents)
        if any(path in contents for path in obsolete):
            raise UnsafeChangePathError(
                "Generated ownership manifest overlaps desired files."
            )
        all_paths = self._validate_paths(
            {
                **dict(contents),
                **{path: "" for path in obsolete},
            }
        )
        normalized = tuple(path for path in all_paths if path in contents)
        self._require_clean_head(repository_root)
        base_commit = self._run(
            ("git", "rev-parse", "--verify", "HEAD"),
            repository_root,
        ).strip()
        statuses = self._classify_paths(
            repository_root,
            base_commit,
            normalized,
            contents,
            obsolete,
            all_paths,
        )
        result_paths = (
            *normalized,
            *(
                path
                for path in all_paths
                if path in obsolete and path in statuses
            ),
        )
        return tuple(
            OnboardingChange(
                path=path,
                content=contents.get(path, ""),
                status=statuses[path],
                base_commit=base_commit,
            )
            for path in result_paths
        )

    def write(
        self,
        repository_root: Path,
        contents: Mapping[Path, str],
    ) -> tuple[OnboardingChange, ...]:
        obsolete = self._obsolete_hashes(contents)
        if any(path in contents for path in obsolete):
            raise UnsafeChangePathError(
                "Generated ownership manifest overlaps desired files."
            )
        all_paths = self._validate_paths(
            {
                **dict(contents),
                **{path: "" for path in obsolete},
            }
        )
        normalized = tuple(path for path in all_paths if path in contents)
        self._require_clean_head(repository_root)
        base_commit = self._run(
            ("git", "rev-parse", "--verify", "HEAD"),
            repository_root,
        ).strip()
        statuses = self._classify_paths(
            repository_root,
            base_commit,
            normalized,
            contents,
            obsolete,
            all_paths,
        )
        result_paths = (
            *normalized,
            *(
                path
                for path in all_paths
                if path in obsolete and path in statuses
            ),
        )
        changed = tuple(
            path
            for path in result_paths
            if statuses[path] is not ChangeStatus.UNCHANGED
        )
        if not changed:
            return tuple(
                OnboardingChange(
                    path=path,
                    content=contents.get(path, ""),
                    status=ChangeStatus.UNCHANGED,
                    base_commit=base_commit,
                    commit_sha=base_commit,
                )
                for path in result_paths
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
            for path in changed:
                if statuses[path] is ChangeStatus.REMOVED:
                    self._run(
                        (
                            "git",
                            "update-index",
                            "--force-remove",
                            "--",
                            path.as_posix(),
                        ),
                        repository_root,
                        environment=index_environment,
                    )
                    continue
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
                content=contents.get(path, ""),
                status=(
                    ChangeStatus.CREATED
                    if statuses[path] is ChangeStatus.PLANNED
                    else statuses[path]
                ),
                base_commit=base_commit,
                commit_sha=commit_sha,
            )
            for path in result_paths
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

    def _classify_paths(
        self,
        repository_root: Path,
        base_commit: str,
        paths: tuple[Path, ...],
        contents: Mapping[Path, str],
        obsolete: Mapping[Path, tuple[str, ...]],
        all_paths: tuple[Path, ...],
    ) -> dict[Path, ChangeStatus]:
        output = self._run(
            (
                "git",
                "ls-tree",
                "-r",
                "-t",
                "-z",
                base_commit,
            ),
            repository_root,
        )
        entries: dict[str, str] = {}
        conflicts: set[Path] = set()
        for entry in output.split("\0"):
            if not entry:
                continue
            metadata, name = entry.split("\t", 1)
            object_type = metadata.split()[1]
            entries[name] = object_type
        for path in all_paths:
            parts = path.parts
            for index in range(1, len(parts)):
                ancestor = Path(*parts[:index]).as_posix()
                if entries.get(ancestor) not in {None, "tree"}:
                    conflicts.add(path)
            exact = entries.get(path.as_posix())
            if exact is not None and exact != "blob":
                conflicts.add(path)
        existing_manifest = self._existing_manifest(
            repository_root,
            base_commit,
            entries,
        )
        accepted_previous = self._accepted_previous_hashes(contents)
        accepted_normalized = (
            self._accepted_previous_normalized_hashes(contents)
        )
        obsolete_normalized = self._obsolete_normalized_hashes(
            contents
        )
        statuses: dict[Path, ChangeStatus] = {}
        for path in paths:
            name = path.as_posix()
            if name not in entries:
                statuses[path] = ChangeStatus.PLANNED
                continue
            current = self._run(
                ("git", "show", f"{base_commit}:{name}"),
                repository_root,
            )
            current_sha = hashlib.sha256(
                current.encode("utf-8")
            ).hexdigest()
            desired_sha = hashlib.sha256(
                contents[path].encode("utf-8")
            ).hexdigest()
            if current_sha == desired_sha:
                statuses[path] = ChangeStatus.UNCHANGED
                continue
            managed_sha = (
                existing_manifest.get(name)
                if existing_manifest is not None
                else None
            )
            if (
                path == _MANIFEST_PATH
                and existing_manifest is not None
            ) or managed_sha == current_sha or current_sha in (
                accepted_previous.get(name, ())
            ):
                statuses[path] = ChangeStatus.UPDATED
                continue
            normalized = normalize_legacy_generated_content(
                path,
                current,
            )
            if (
                normalized is not None
                and hashlib.sha256(
                    normalized.encode("utf-8")
                ).hexdigest()
                in accepted_normalized.get(name, ())
            ):
                statuses[path] = ChangeStatus.UPDATED
                continue
            conflicts.add(path)
        for path, accepted in obsolete.items():
            name = path.as_posix()
            if name not in entries:
                continue
            if entries[name] != "blob":
                conflicts.add(path)
                continue
            current = self._run(
                ("git", "show", f"{base_commit}:{name}"),
                repository_root,
            )
            current_sha = hashlib.sha256(
                current.encode("utf-8")
            ).hexdigest()
            managed_sha = (
                existing_manifest.get(name)
                if existing_manifest is not None
                else None
            )
            if current_sha in accepted or managed_sha == current_sha:
                statuses[path] = ChangeStatus.REMOVED
                continue
            normalized = normalize_legacy_generated_content(
                path,
                current,
            )
            if (
                normalized is not None
                and hashlib.sha256(
                    normalized.encode("utf-8")
                ).hexdigest()
                in obsolete_normalized.get(name, ())
            ):
                statuses[path] = ChangeStatus.REMOVED
                continue
            conflicts.add(path)
        if conflicts:
            raise ChangeSetConflictError(
                tuple(path for path in all_paths if path in conflicts)
            )
        return statuses

    def _accepted_previous_hashes(
        self,
        contents: Mapping[Path, str],
    ) -> dict[str, tuple[str, ...]]:
        raw = contents.get(_MANIFEST_PATH)
        if raw is None:
            return {}
        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        accepted = (
            document.get("accepted_previous_sha256")
            if isinstance(document, dict)
            else None
        )
        if accepted is None:
            return {}
        if not isinstance(accepted, dict):
            raise UnsafeChangePathError(
                "Generated ownership manifest is invalid."
            )
        result: dict[str, tuple[str, ...]] = {}
        for path, values in accepted.items():
            if (
                not isinstance(path, str)
                or not isinstance(values, list)
                or not values
                or any(
                    not isinstance(value, str)
                    or re.fullmatch(r"[0-9a-f]{64}", value) is None
                    for value in values
                )
            ):
                raise UnsafeChangePathError(
                    "Generated ownership manifest is invalid."
                )
            result[path] = tuple(values)
        return result

    def _accepted_previous_normalized_hashes(
        self,
        contents: Mapping[Path, str],
    ) -> dict[str, tuple[str, ...]]:
        raw = contents.get(_MANIFEST_PATH)
        if raw is None:
            return {}
        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        accepted = (
            document.get("accepted_previous_normalized_sha256")
            if isinstance(document, dict)
            else None
        )
        if accepted is None:
            return {}
        if not isinstance(accepted, dict):
            raise UnsafeChangePathError(
                "Generated ownership manifest is invalid."
            )
        result: dict[str, tuple[str, ...]] = {}
        for path, values in accepted.items():
            if (
                not isinstance(path, str)
                or not isinstance(values, list)
                or not values
                or any(
                    not isinstance(value, str)
                    or re.fullmatch(r"[0-9a-f]{64}", value) is None
                    for value in values
                )
            ):
                raise UnsafeChangePathError(
                    "Generated ownership manifest is invalid."
                )
            result[path] = tuple(values)
        return result

    def _obsolete_hashes(
        self,
        contents: Mapping[Path, str],
    ) -> dict[Path, tuple[str, ...]]:
        raw = contents.get(_MANIFEST_PATH)
        if raw is None:
            return {}
        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        obsolete = (
            document.get("obsolete")
            if isinstance(document, dict)
            else None
        )
        if obsolete is None:
            return {}
        if not isinstance(obsolete, dict):
            raise UnsafeChangePathError(
                "Generated ownership manifest is invalid."
            )
        result: dict[Path, tuple[str, ...]] = {}
        for name, values in obsolete.items():
            path = Path(name) if isinstance(name, str) else Path()
            if (
                not isinstance(name, str)
                or not isinstance(values, list)
                or not values
                or any(
                    not isinstance(value, str)
                    or re.fullmatch(r"[0-9a-f]{64}", value) is None
                    for value in values
                )
            ):
                raise UnsafeChangePathError(
                    "Generated ownership manifest is invalid."
                )
            result[path] = tuple(values)
        return result

    def _obsolete_normalized_hashes(
        self,
        contents: Mapping[Path, str],
    ) -> dict[str, tuple[str, ...]]:
        raw = contents.get(_MANIFEST_PATH)
        if raw is None:
            return {}
        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        obsolete = (
            document.get("obsolete_normalized_sha256")
            if isinstance(document, dict)
            else None
        )
        if obsolete is None:
            return {}
        if not isinstance(obsolete, dict):
            raise UnsafeChangePathError(
                "Generated ownership manifest is invalid."
            )
        result: dict[str, tuple[str, ...]] = {}
        for path, values in obsolete.items():
            if (
                not isinstance(path, str)
                or not isinstance(values, list)
                or not values
                or any(
                    not isinstance(value, str)
                    or re.fullmatch(r"[0-9a-f]{64}", value) is None
                    for value in values
                )
            ):
                raise UnsafeChangePathError(
                    "Generated ownership manifest is invalid."
                )
            result[path] = tuple(values)
        return result

    def _existing_manifest(
        self,
        repository_root: Path,
        base_commit: str,
        entries: Mapping[str, str],
    ) -> dict[str, str] | None:
        name = _MANIFEST_PATH.as_posix()
        if entries.get(name) != "blob":
            return None
        raw = self._run(
            ("git", "show", f"{base_commit}:{name}"),
            repository_root,
        )
        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if (
            not isinstance(document, dict)
            or document.get("generator") != "foundry-opt init"
            or document.get("schema_version") != 1
            or not isinstance(document.get("files"), dict)
        ):
            return None
        files = document["files"]
        if any(
            not isinstance(path, str)
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            for path, digest in files.items()
        ):
            return None
        return dict(files)

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
        branch = (
            f"foundry-opt/onboarding-{_branch_slug(request.target_name)}-"
            f"{commit_sha[:12]}"
        )
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
                push_attempted=phase == "push",
                pr_creation_attempted=phase == "draft_pr",
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
        push_attempted: bool,
        pr_creation_attempted: bool,
    ) -> tuple[str, ...]:
        residuals: list[str] = []
        preserve_remote = False
        if pushed and pr_creation_attempted:
            branch = branch_ref.removeprefix("refs/heads/")
            try:
                raw = self._run(
                    (
                        "gh",
                        "pr",
                        "list",
                        "--state",
                        "all",
                        "--head",
                        branch,
                        "--json",
                        "url",
                        "--limit",
                        "2",
                    ),
                    request,
                )
                pull_requests = json.loads(raw)
                if not isinstance(pull_requests, list):
                    raise ValueError
                preserve_remote = bool(pull_requests)
            except (CommandError, json.JSONDecodeError, ValueError):
                preserve_remote = True
            if preserve_remote:
                residuals.append(
                    f"draft PR may already exist for {branch_ref}; "
                    "the remote ref was preserved"
                )
        if pushed and not preserve_remote:
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
        elif push_attempted:
            residuals.append(
                f"remote ref {branch_ref} was not deleted because the "
                "failed push did not prove invocation ownership"
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
