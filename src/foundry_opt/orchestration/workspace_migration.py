from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Mapping, Protocol

from foundry_opt.adapters.commands import CommandError, SubprocessCommandRunner
from foundry_opt.adapters.github import github_repository_from_remote_url
from foundry_opt.orchestration.git_transport import (
    compare_and_swap_push,
    configured_remote_url,
    fetch_revision,
    GitTransportError,
    remote_revision,
    resolve_safe_fetch_remote,
    resolve_safe_push_remote,
)
from foundry_opt.orchestration.workspace_git_store import (
    GitWorkspaceStore,
    WorkspaceConflictError,
    WorkspaceStoreError,
)
from foundry_opt.orchestration.workspace_state_migration import (
    convert_workspace_state_v3,
    detect_workspace_state_v3,
    WorkspaceStateConversionError,
)
from foundry_opt.preflight.interfaces import CommandRunner


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_LEGACY_REF = re.compile(
    r"^refs/heads/foundry-opt/(?P<kind>state|inbox)/"
    r"issue-(?P<issue>[1-9][0-9]*)$"
)


class WorkspaceMigrationError(RuntimeError):
    pass


class IssueLifecycle(Enum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class IssueLifecycleReader(Protocol):
    def classify(self, issue_number: int) -> IssueLifecycle: ...


@dataclass(frozen=True)
class LegacyWorkspaceInventoryItem:
    issue_number: int
    issue_lifecycle: IssueLifecycle
    state_ref: str | None
    state_revision: str | None
    state_schema_version: int | None
    inbox_ref: str | None
    inbox_revision: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "inbox_ref": self.inbox_ref,
            "inbox_revision": self.inbox_revision,
            "issue_lifecycle": self.issue_lifecycle.value,
            "issue_number": self.issue_number,
            "state_ref": self.state_ref,
            "state_revision": self.state_revision,
            "state_schema_version": self.state_schema_version,
        }


@dataclass(frozen=True)
class WorkspaceMigrationInventory:
    items: tuple[LegacyWorkspaceInventoryItem, ...]

    def to_dict(self) -> dict[str, object]:
        counts = {
            lifecycle.value: sum(
                item.issue_lifecycle is lifecycle for item in self.items
            )
            for lifecycle in IssueLifecycle
        }
        return {
            "all_closed": bool(self.items)
            and all(
                item.issue_lifecycle is IssueLifecycle.CLOSED
                for item in self.items
            ),
            "counts": counts,
            "items": [item.to_dict() for item in self.items],
            "status": "ready",
        }


@dataclass(frozen=True)
class WorkspaceConversionResult:
    issue_number: int
    source_revision: str
    audit_revision: str
    status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "audit_ref": _audit_ref(self.issue_number),
            "audit_revision": self.audit_revision,
            "issue_number": self.issue_number,
            "source_ref": _state_ref(self.issue_number),
            "source_revision": self.source_revision,
            "status": self.status,
        }


@dataclass(frozen=True)
class WorkspaceArchiveResult:
    issue_number: int
    issue_lifecycle: IssueLifecycle
    status: str
    expected_revisions: Mapping[str, str | None]
    deleted_refs: tuple[str, ...] = ()
    reason: str | None = None
    apply: bool = False

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "apply": self.apply,
            "deleted_refs": list(self.deleted_refs),
            "expected_revisions": dict(self.expected_revisions),
            "issue_lifecycle": self.issue_lifecycle.value,
            "issue_number": self.issue_number,
            "status": self.status,
        }
        if self.reason is not None:
            result["reason"] = self.reason
        return result


class GhIssueLifecycleReader:
    def __init__(
        self,
        commands: CommandRunner,
        *,
        repository_root: Path,
        repository: str,
    ) -> None:
        self._commands = commands
        self._root = repository_root
        self._repository = repository

    def classify(self, issue_number: int) -> IssueLifecycle:
        _issue_number(issue_number)
        try:
            raw = self._commands.run(
                (
                    "gh",
                    "issue",
                    "view",
                    str(issue_number),
                    "--repo",
                    self._repository,
                    "--json",
                    "number,state",
                ),
                cwd=self._root,
            ).stdout
            document = json.loads(raw)
        except (CommandError, json.JSONDecodeError):
            return IssueLifecycle.UNKNOWN
        if (
            type(document) is not dict
            or document.get("number") != issue_number
        ):
            return IssueLifecycle.UNKNOWN
        return {
            "OPEN": IssueLifecycle.OPEN,
            "CLOSED": IssueLifecycle.CLOSED,
        }.get(document.get("state"), IssueLifecycle.UNKNOWN)


class WorkspaceMigrationService:
    def __init__(
        self,
        repository_root: Path,
        lifecycle: IssueLifecycleReader,
        *,
        remote: str = "origin",
    ) -> None:
        self._root = _repository_root(repository_root)
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", remote) is None:
            raise ValueError("remote is invalid")
        self._remote = remote
        self._lifecycle = lifecycle

    def inventory(self) -> WorkspaceMigrationInventory:
        refs = self._legacy_refs()
        items: list[LegacyWorkspaceInventoryItem] = []
        for issue_number in sorted(refs):
            issue_refs = refs[issue_number]
            state_revision = issue_refs.get("state")
            plan = (
                detect_workspace_state_v3(
                    self._root,
                    issue_number,
                    remote=self._remote,
                )
                if state_revision is not None
                else None
            )
            if plan is None and "inbox" not in issue_refs:
                continue
            items.append(
                LegacyWorkspaceInventoryItem(
                    issue_number=issue_number,
                    issue_lifecycle=self._lifecycle.classify(
                        issue_number
                    ),
                    state_ref=plan.source_ref if plan is not None else None,
                    state_revision=(
                        plan.source_revision if plan is not None else None
                    ),
                    state_schema_version=3 if plan is not None else None,
                    inbox_ref=(
                        _inbox_ref(issue_number)
                        if "inbox" in issue_refs
                        else None
                    ),
                    inbox_revision=issue_refs.get("inbox"),
                )
            )
        return WorkspaceMigrationInventory(tuple(items))

    def convert(
        self,
        issue_number: int,
        *,
        expected_source_revision: str,
    ) -> WorkspaceConversionResult:
        _issue_number(issue_number)
        _revision(expected_source_revision, "expected_source_revision")
        self._require_closed(issue_number)
        plan = detect_workspace_state_v3(
            self._root,
            issue_number,
            remote=self._remote,
        )
        if plan is None:
            raise WorkspaceMigrationError(
                "workspace state is not validated legacy v3"
            )
        if plan.source_revision != expected_source_revision:
            raise WorkspaceMigrationError(
                "workspace state changed after planning"
            )
        audit_ref = _audit_ref(issue_number)
        before = self._remote_revision(audit_ref)
        conversion_ref = _conversion_ref(issue_number)
        try:
            payload = convert_workspace_state_v3(
                self._root,
                issue_number,
                remote=self._remote,
            )
            if payload.source_revision != expected_source_revision:
                raise WorkspaceMigrationError(
                    "workspace state changed during conversion"
                )
            snapshot = GitWorkspaceStore(
                self._root,
                remote=self._remote,
            ).write_conversion(
                payload,
                target_ref=conversion_ref,
                expected_revision=self._remote_revision(conversion_ref),
            )
        except (
            WorkspaceConflictError,
            WorkspaceStateConversionError,
            WorkspaceStoreError,
        ) as error:
            raise WorkspaceMigrationError(
                "workspace conversion could not be verified"
            ) from error
        self._require_closed(issue_number)
        self._publish_audit(
            audit_ref=audit_ref,
            revision=snapshot.revision,
            expected_revision=before,
        )
        after = self._remote_revision(audit_ref)
        if after != snapshot.revision:
            raise WorkspaceMigrationError(
                "workspace audit ref verification failed"
            )
        return WorkspaceConversionResult(
            issue_number=issue_number,
            source_revision=expected_source_revision,
            audit_revision=after,
            status=(
                "already_converted"
                if before == after
                else "converted"
            ),
        )

    def plan_archive(self, issue_number: int) -> WorkspaceArchiveResult:
        _issue_number(issue_number)
        lifecycle = self._lifecycle.classify(issue_number)
        revisions = self._issue_revisions(issue_number)
        if lifecycle is not IssueLifecycle.CLOSED:
            return WorkspaceArchiveResult(
                issue_number=issue_number,
                issue_lifecycle=lifecycle,
                status="refused",
                expected_revisions=revisions,
                reason="issue_not_closed",
            )
        if revisions["audit"] is None:
            return WorkspaceArchiveResult(
                issue_number=issue_number,
                issue_lifecycle=lifecycle,
                status="refused",
                expected_revisions=revisions,
                reason="audit_ref_missing",
            )
        if revisions["state"] is None and revisions["inbox"] is None:
            self._verify_audit(
                issue_number,
                audit_revision=revisions["audit"],
                source_revision=None,
            )
            return WorkspaceArchiveResult(
                issue_number=issue_number,
                issue_lifecycle=lifecycle,
                status="already_completed",
                expected_revisions=revisions,
            )
        if revisions["state"] is None:
            return WorkspaceArchiveResult(
                issue_number=issue_number,
                issue_lifecycle=lifecycle,
                status="refused",
                expected_revisions=revisions,
                reason="legacy_state_missing",
            )
        plan = detect_workspace_state_v3(
            self._root,
            issue_number,
            remote=self._remote,
        )
        if plan is None or plan.source_revision != revisions["state"]:
            raise WorkspaceMigrationError(
                "workspace state is not validated legacy v3"
            )
        self._verify_audit(
            issue_number,
            audit_revision=revisions["audit"],
            source_revision=revisions["state"],
        )
        return WorkspaceArchiveResult(
            issue_number=issue_number,
            issue_lifecycle=lifecycle,
            status="planned",
            expected_revisions=revisions,
        )

    def apply_archive(
        self,
        issue_number: int,
        *,
        expected_revisions: Mapping[str, str | None],
    ) -> WorkspaceArchiveResult:
        _issue_number(issue_number)
        expected = _expected_revisions(expected_revisions)
        lifecycle = self._require_closed(issue_number)
        current = self._issue_revisions(issue_number)
        if current["audit"] != expected["audit"]:
            raise WorkspaceMigrationError(
                "workspace audit ref changed after planning"
            )
        if expected["audit"] is None:
            raise WorkspaceMigrationError(
                "workspace audit ref was not planned"
            )
        for kind in ("state", "inbox"):
            if current[kind] not in {expected[kind], None}:
                raise WorkspaceMigrationError(
                    f"workspace {kind} ref changed after planning"
                )
            if expected[kind] is None and current[kind] is not None:
                raise WorkspaceMigrationError(
                    f"workspace {kind} ref changed after planning"
                )
        if expected["state"] is None and current["state"] is not None:
            raise WorkspaceMigrationError(
                "workspace state ref changed after planning"
            )
        self._verify_audit(
            issue_number,
            audit_revision=expected["audit"],
            source_revision=expected["state"],
        )
        targets = {
            _inbox_ref(issue_number): current["inbox"],
            _state_ref(issue_number): current["state"],
        }
        present = {
            ref: revision
            for ref, revision in targets.items()
            if revision is not None
        }
        if present and expected["state"] is None:
            raise WorkspaceMigrationError(
                "workspace legacy state revision was not planned"
            )
        if not present:
            return WorkspaceArchiveResult(
                issue_number=issue_number,
                issue_lifecycle=lifecycle,
                status="already_completed",
                expected_revisions=expected,
                apply=True,
            )
        self._require_closed(issue_number)
        self._delete_refs(
            present,
            audit_ref=_audit_ref(issue_number),
            audit_revision=expected["audit"],
        )
        if self._remote_revision(_audit_ref(issue_number)) != expected["audit"]:
            raise WorkspaceMigrationError(
                "workspace audit ref changed during archival"
            )
        return WorkspaceArchiveResult(
            issue_number=issue_number,
            issue_lifecycle=lifecycle,
            status="completed",
            expected_revisions=expected,
            deleted_refs=tuple(sorted(present)),
            apply=True,
        )

    def _legacy_refs(self) -> dict[int, dict[str, str]]:
        safe_remote = resolve_safe_fetch_remote(
            self._root,
            self._remote,
        )
        if safe_remote is None:
            raise WorkspaceMigrationError(
                "workspace migration fetch destination is not trusted"
            )
        completed = subprocess.run(
            (
                "git",
                "ls-remote",
                "--heads",
                safe_remote.url,
                "refs/heads/foundry-opt/state/issue-*",
                "refs/heads/foundry-opt/inbox/issue-*",
            ),
            cwd=self._root,
            capture_output=True,
            check=False,
            text=True,
        )
        if completed.returncode != 0:
            raise WorkspaceMigrationError(
                "workspace legacy ref inventory failed"
            )
        refs: dict[int, dict[str, str]] = {}
        for line in completed.stdout.splitlines():
            fields = line.split()
            if len(fields) != 2 or _COMMIT.fullmatch(fields[0]) is None:
                raise WorkspaceMigrationError(
                    "workspace legacy ref metadata is invalid"
                )
            match = _LEGACY_REF.fullmatch(fields[1])
            if match is None:
                raise WorkspaceMigrationError(
                    "workspace legacy ref metadata is invalid"
                )
            issue_number = int(match.group("issue"))
            kind = match.group("kind")
            issue_refs = refs.setdefault(issue_number, {})
            if kind in issue_refs:
                raise WorkspaceMigrationError(
                    "workspace legacy ref metadata is ambiguous"
                )
            issue_refs[kind] = fields[0]
        return refs

    def _issue_revisions(
        self,
        issue_number: int,
    ) -> dict[str, str | None]:
        return {
            "audit": self._remote_revision(_audit_ref(issue_number)),
            "inbox": self._remote_revision(_inbox_ref(issue_number)),
            "state": self._remote_revision(_state_ref(issue_number)),
        }

    def _remote_revision(self, ref: str) -> str | None:
        safe_remote = resolve_safe_fetch_remote(
            self._root,
            self._remote,
        )
        if safe_remote is None:
            raise WorkspaceMigrationError(
                "workspace migration fetch destination is not trusted"
            )
        try:
            return remote_revision(self._root, safe_remote, ref)
        except GitTransportError as error:
            raise WorkspaceMigrationError(
                "workspace migration ref verification failed"
            ) from error

    def _verify_audit(
        self,
        issue_number: int,
        *,
        audit_revision: str,
        source_revision: str | None,
    ) -> None:
        safe_remote = resolve_safe_fetch_remote(
            self._root,
            self._remote,
        )
        if safe_remote is None:
            raise WorkspaceMigrationError(
                "workspace migration fetch destination is not trusted"
            )
        try:
            fetched = fetch_revision(
                self._root,
                safe_remote,
                _audit_ref(issue_number),
            )
        except GitTransportError as error:
            raise WorkspaceMigrationError(
                "workspace audit ref verification failed"
            ) from error
        if fetched != audit_revision:
            raise WorkspaceMigrationError(
                "workspace audit ref changed after planning"
            )
        if source_revision is not None:
            lineage = _git_text(
                self._root,
                "rev-list",
                "--parents",
                "-n",
                "1",
                audit_revision,
            ).split()
            if lineage != [audit_revision, source_revision]:
                raise WorkspaceMigrationError(
                    "workspace audit source lineage is invalid"
                )
        try:
            document = json.loads(
                _git_text(
                    self._root,
                    "show",
                    f"{audit_revision}:snapshot.json",
                )
            )
        except json.JSONDecodeError as error:
            raise WorkspaceMigrationError(
                "workspace audit snapshot is invalid"
            ) from error
        if type(document) is not dict:
            raise WorkspaceMigrationError(
                "workspace audit snapshot is invalid"
            )
        state = document.get("state")
        if (
            document.get("schema_version") != 4
            or type(state) is not dict
            or state.get("issue_number") != issue_number
        ):
            raise WorkspaceMigrationError(
                "workspace audit snapshot is invalid"
            )

    def _delete_refs(
        self,
        refs: Mapping[str, str],
        *,
        audit_ref: str,
        audit_revision: str,
    ) -> None:
        safe_remote = resolve_safe_push_remote(
            self._root,
            self._remote,
        )
        if safe_remote is None:
            raise WorkspaceMigrationError(
                "workspace migration push destination is not trusted"
            )
        arguments = [
            "git",
            "push",
            "--atomic",
            f"--force-with-lease={audit_ref}:{audit_revision}",
        ]
        for ref, revision in sorted(refs.items()):
            arguments.append(f"--force-with-lease={ref}:{revision}")
        arguments.append(safe_remote.url)
        arguments.append(f"{audit_revision}:{audit_ref}")
        arguments.extend(f":{ref}" for ref in sorted(refs))
        completed = subprocess.run(
            tuple(arguments),
            cwd=self._root,
            capture_output=True,
            check=False,
            text=True,
        )
        remaining = {
            ref: self._remote_revision(ref) for ref in sorted(refs)
        }
        if completed.returncode != 0 and any(remaining.values()):
            raise WorkspaceMigrationError(
                "workspace archival compare-and-swap failed"
            )
        if any(remaining.values()):
            raise WorkspaceMigrationError(
                "workspace archival deletion was not verified"
            )

    def _publish_audit(
        self,
        *,
        audit_ref: str,
        revision: str,
        expected_revision: str | None,
    ) -> None:
        if expected_revision == revision:
            return
        if expected_revision is not None:
            raise WorkspaceMigrationError(
                "workspace audit ref conflicts with conversion"
            )
        safe_remote = resolve_safe_push_remote(
            self._root,
            self._remote,
        )
        if safe_remote is None:
            raise WorkspaceMigrationError(
                "workspace migration push destination is not trusted"
            )
        try:
            pushed = compare_and_swap_push(
                self._root,
                safe_remote,
                source_revision=revision,
                destination_ref=audit_ref,
                expected_revision=None,
            )
        except GitTransportError as error:
            raise WorkspaceMigrationError(
                "workspace audit publication failed"
            ) from error
        if (
            pushed.before is not None
            or pushed.returncode != 0
            or pushed.after != revision
        ):
            acknowledged = self._remote_revision(audit_ref)
            if acknowledged != revision:
                raise WorkspaceMigrationError(
                    "workspace audit publication failed"
                )

    def _require_closed(self, issue_number: int) -> IssueLifecycle:
        lifecycle = self._lifecycle.classify(issue_number)
        if lifecycle is not IssueLifecycle.CLOSED:
            raise WorkspaceMigrationError(
                "workspace issue is not closed"
            )
        return lifecycle


def build_production_workspace_migration_service(
    repository_root: Path,
    *,
    remote: str = "origin",
) -> WorkspaceMigrationService:
    root = _repository_root(repository_root)
    remote_url = configured_remote_url(root, remote)
    repository = (
        github_repository_from_remote_url(remote_url)
        if remote_url is not None
        else None
    )
    if repository is None:
        raise WorkspaceMigrationError(
            "workspace migration GitHub repository is unavailable"
        )
    return WorkspaceMigrationService(
        root,
        GhIssueLifecycleReader(
            SubprocessCommandRunner(),
            repository_root=root,
            repository=repository,
        ),
        remote=remote,
    )


def _expected_revisions(
    value: Mapping[str, str | None],
) -> dict[str, str | None]:
    if set(value) != {"audit", "inbox", "state"}:
        raise ValueError("expected revisions are incomplete")
    result = dict(value)
    for kind, revision in result.items():
        if revision is not None:
            _revision(revision, f"expected {kind} revision")
    return result


def _repository_root(path: Path) -> Path:
    root = Path(os.path.abspath(path))
    completed = subprocess.run(
        ("git", "rev-parse", "--show-toplevel"),
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        raise ValueError("repository_root must be a Git worktree")
    discovered = Path(os.path.abspath(completed.stdout.strip()))
    if os.path.normcase(discovered) != os.path.normcase(root):
        raise ValueError("repository_root must be the Git worktree root")
    return root


def _git_text(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        raise WorkspaceMigrationError(
            "workspace migration Git verification failed"
        )
    return completed.stdout.strip()


def _issue_number(value: int) -> None:
    if type(value) is not int or value < 1:
        raise ValueError("issue_number must be a positive integer")


def _revision(value: str, name: str) -> None:
    if type(value) is not str or _COMMIT.fullmatch(value) is None:
        raise ValueError(f"{name} must be a commit SHA")


def _state_ref(issue_number: int) -> str:
    return f"refs/heads/foundry-opt/state/issue-{issue_number}"


def _inbox_ref(issue_number: int) -> str:
    return f"refs/heads/foundry-opt/inbox/issue-{issue_number}"


def _audit_ref(issue_number: int) -> str:
    return f"refs/heads/foundry-opt/audit/issue-{issue_number}"


def _conversion_ref(issue_number: int) -> str:
    return f"refs/heads/foundry-opt/migration/issue-{issue_number}"
