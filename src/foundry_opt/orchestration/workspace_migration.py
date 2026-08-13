from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Mapping, Protocol

from foundry_opt.adapters.commands import CommandError, SubprocessCommandRunner
from foundry_opt.adapters.github import github_repository_from_remote_url
from foundry_opt.orchestration.git_transport import (
    atomic_compare_and_swap_delete,
    compare_and_swap_push,
    configured_remote_url,
    fetch_revision,
    GitTransportError,
    list_remote_heads,
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
_SAFE_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
_SAFE_SEGMENT_PATH = rf"{_SAFE_SEGMENT}(?:/{_SAFE_SEGMENT})*"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CLEANUP_STATE_OR_INBOX_REF = re.compile(
    r"^refs/heads/foundry-opt/(?P<kind>state|inbox)/"
    r"issue-(?P<issue>[1-9][0-9]*)$"
)
_CLEANUP_DESIGN_REF = re.compile(
    r"^refs/heads/foundry-opt/design/issue-(?P<issue>[1-9][0-9]*)/"
    rf"(?P<tail>{_SAFE_SEGMENT_PATH})$"
)
_CLEANUP_SPEC_REF = re.compile(
    r"^refs/heads/foundry-opt/spec/issue-(?P<issue>[1-9][0-9]*)/"
    r"(?P<spec>[0-9a-f]{12})"
    r"(?:/generation-(?P<generation>[1-9][0-9]*))?$"
)
_CLEANUP_MIGRATION_REF = re.compile(
    r"^refs/heads/foundry-opt/migration/issue-(?P<issue>[1-9][0-9]*)"
    rf"(?:/(?P<tail>{_SAFE_SEGMENT_PATH}))?$"
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


class WorkspaceLegacyRefKind(Enum):
    STATE = "state"
    INBOX = "inbox"
    DESIGN = "design"
    SPEC = "spec"
    MIGRATION = "migration"


@dataclass(frozen=True)
class WorkspaceLegacyRefRecord:
    issue_number: int
    issue_lifecycle: IssueLifecycle
    ref_kind: WorkspaceLegacyRefKind
    ref: str
    revision: str
    action: str
    reason: str
    state_schema_version: int | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "action": self.action,
            "issue_lifecycle": self.issue_lifecycle.value,
            "issue_number": self.issue_number,
            "reason": self.reason,
            "ref": self.ref,
            "ref_kind": self.ref_kind.value,
            "revision": self.revision,
        }
        if self.state_schema_version is not None:
            result["state_schema_version"] = self.state_schema_version
        return result


@dataclass(frozen=True)
class WorkspaceLegacyCleanupPlan:
    remote: str
    status: str
    content_hash: str
    items: tuple[WorkspaceLegacyRefRecord, ...]
    deletions: tuple[WorkspaceLegacyRefRecord, ...]
    reason: str | None = None
    apply: bool = False

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "apply": self.apply,
            "content_hash": self.content_hash,
            "deletions": [item.to_dict() for item in self.deletions],
            "items": [item.to_dict() for item in self.items],
            "remote": self.remote,
            "status": self.status,
        }
        if self.reason is not None:
            result["reason"] = self.reason
        return result

    @classmethod
    def from_dict(
        cls,
        document: Mapping[str, object],
    ) -> "WorkspaceLegacyCleanupPlan":
        payload = _cleanup_plan_document(document)
        if payload.get("apply") is not False:
            raise WorkspaceMigrationError(
                "workspace cleanup plan must be a dry-run plan"
            )
        items = tuple(
            _cleanup_ref_record(entry) for entry in payload["items"]
        )
        deletions = tuple(
            _cleanup_ref_record(entry) for entry in payload["deletions"]
        )
        if (
            len(items) != len({item.ref for item in items})
            or len(deletions) != len({item.ref for item in deletions})
        ):
            raise WorkspaceMigrationError(
                "workspace cleanup plan is invalid"
            )
        canonical = dict(payload)
        content_hash = canonical.pop("content_hash")
        expected = _cleanup_plan_content_hash(canonical)
        if content_hash != expected:
            raise WorkspaceMigrationError(
                "workspace cleanup plan content hash mismatch"
            )
        if not set(deletions).issubset(set(items)):
            raise WorkspaceMigrationError(
                "workspace cleanup plan deletions are invalid"
            )
        return cls(
            remote=payload["remote"],
            status=payload["status"],
            content_hash=content_hash,
            items=items,
            deletions=deletions,
            reason=payload.get("reason"),
            apply=False,
        )


@dataclass(frozen=True)
class WorkspaceLegacyCleanupResult:
    remote: str
    status: str
    content_hash: str
    deleted_refs: tuple[str, ...]
    audit_manifest: tuple[WorkspaceLegacyRefRecord, ...]
    reason: str | None = None
    apply: bool = True

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "apply": self.apply,
            "audit_manifest": [
                item.to_dict() for item in self.audit_manifest
            ],
            "content_hash": self.content_hash,
            "deleted_refs": list(self.deleted_refs),
            "remote": self.remote,
            "status": self.status,
        }
        if self.reason is not None:
            result["reason"] = self.reason
        return result


@dataclass(frozen=True)
class _WorkspaceLegacyRefSnapshot:
    issue_number: int
    issue_lifecycle: IssueLifecycle
    ref_kind: WorkspaceLegacyRefKind
    ref: str
    revision: str
    state_schema_version: int | None = None


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
        if revisions["migration"] not in {None, revisions["audit"]}:
            raise WorkspaceMigrationError(
                "workspace migration ref does not match audit"
            )
        if (
            revisions["state"] is None
            and revisions["inbox"] is None
            and revisions["migration"] is None
        ):
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
        if revisions["state"] is None and revisions["inbox"] is not None:
            return WorkspaceArchiveResult(
                issue_number=issue_number,
                issue_lifecycle=lifecycle,
                status="refused",
                expected_revisions=revisions,
                reason="legacy_state_missing",
            )
        if revisions["state"] is not None:
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
        for kind in ("state", "inbox", "migration"):
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
            _conversion_ref(issue_number): current["migration"],
            _state_ref(issue_number): current["state"],
        }
        present = {
            ref: revision
            for ref, revision in targets.items()
            if revision is not None
        }
        if (
            (current["state"] is not None or current["inbox"] is not None)
            and expected["state"] is None
        ):
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
            targets,
            audit_ref=_audit_ref(issue_number),
            audit_revision=expected["audit"],
        )
        after = self._issue_revisions(issue_number)
        if after["audit"] != expected["audit"]:
            raise WorkspaceMigrationError(
                "workspace audit ref changed during archival"
            )
        if any(
            after[kind] is not None
            for kind in ("inbox", "migration", "state")
        ):
            raise WorkspaceMigrationError(
                "workspace archival deletion was not verified"
            )
        return WorkspaceArchiveResult(
            issue_number=issue_number,
            issue_lifecycle=lifecycle,
            status="completed",
            expected_revisions=expected,
            deleted_refs=tuple(sorted(present)),
            apply=True,
        )

    def plan_cleanup_legacy(self) -> WorkspaceLegacyCleanupPlan:
        snapshots = self._cleanup_legacy_refs()
        if not snapshots:
            payload = _cleanup_plan_payload(
                remote=self._remote,
                status="already_completed",
                items=(),
                deletions=(),
                reason=None,
                apply=False,
            )
            return WorkspaceLegacyCleanupPlan(
                remote=self._remote,
                status=payload["status"],
                content_hash=_cleanup_plan_content_hash(payload),
                items=(),
                deletions=(),
                reason=None,
                apply=False,
            )
        items = _cleanup_plan_items(snapshots)
        deletions = tuple(
            item for item in items if item.action == "delete"
        )
        if deletions:
            status = "planned"
            reason = None
        else:
            status = "refused"
            reason = _cleanup_plan_reason(items)
        payload = _cleanup_plan_payload(
            remote=self._remote,
            status=status,
            items=items,
            deletions=deletions,
            reason=reason,
            apply=False,
        )
        return WorkspaceLegacyCleanupPlan(
            remote=self._remote,
            status=status,
            content_hash=_cleanup_plan_content_hash(payload),
            items=items,
            deletions=deletions,
            reason=reason,
            apply=False,
        )

    def apply_cleanup_legacy(
        self,
        plan: WorkspaceLegacyCleanupPlan,
    ) -> WorkspaceLegacyCleanupResult:
        if plan.apply is not False:
            raise WorkspaceMigrationError(
                "workspace cleanup plan must be a dry-run plan"
            )
        current_plan = self.plan_cleanup_legacy()
        if current_plan.to_dict() != plan.to_dict():
            raise WorkspaceMigrationError(
                "workspace cleanup plan changed after planning"
            )
        if not current_plan.deletions:
            return WorkspaceLegacyCleanupResult(
                remote=self._remote,
                status="already_completed",
                content_hash=current_plan.content_hash,
                deleted_refs=(),
                audit_manifest=(),
                reason=current_plan.reason,
                apply=True,
            )
        targets = {
            item.ref: item.revision for item in current_plan.deletions
        }
        safe_remote = resolve_safe_push_remote(
            self._root,
            self._remote,
        )
        if safe_remote is None:
            raise WorkspaceMigrationError(
                "workspace migration push destination is not trusted"
            )
        before = {
            record.ref: record.revision
            for record in self._cleanup_legacy_refs()
        }
        expected_before = {
            record.ref: record.revision
            for record in current_plan.items
        }
        if before != expected_before:
            raise WorkspaceMigrationError(
                "workspace cleanup plan changed after planning"
            )
        try:
            returncode = atomic_compare_and_swap_delete(
                self._root,
                safe_remote,
                refs=targets,
            )
        except GitTransportError as error:
            raise WorkspaceMigrationError(
                "workspace cleanup compare-and-swap failed"
            ) from error
        after_records = {
            record.ref: record.revision
            for record in self._cleanup_legacy_refs()
        }
        expected_after = {
            ref: revision
            for ref, revision in expected_before.items()
            if ref not in targets
        }
        if after_records != expected_after:
            raise WorkspaceMigrationError(
                "workspace cleanup deletion was not verified"
            )
        if returncode != 0 and after_records != expected_after:
            raise WorkspaceMigrationError(
                "workspace cleanup compare-and-swap failed"
            )
        return WorkspaceLegacyCleanupResult(
            remote=self._remote,
            status="completed",
            content_hash=current_plan.content_hash,
            deleted_refs=tuple(
                item.ref for item in current_plan.deletions
            ),
            audit_manifest=current_plan.deletions,
            apply=True,
        )

    def _cleanup_legacy_refs(
        self,
    ) -> tuple[_WorkspaceLegacyRefSnapshot, ...]:
        safe_remote = resolve_safe_fetch_remote(
            self._root,
            self._remote,
        )
        if safe_remote is None:
            raise WorkspaceMigrationError(
                "workspace migration fetch destination is not trusted"
            )
        try:
            entries = list_remote_heads(
                self._root,
                safe_remote,
                (
                    "refs/heads/foundry-opt/state/issue-*",
                    "refs/heads/foundry-opt/inbox/issue-*",
                    "refs/heads/foundry-opt/design/issue-*",
                    "refs/heads/foundry-opt/spec/issue-*",
                    "refs/heads/foundry-opt/migration/issue-*",
                ),
            )
        except GitTransportError as error:
            raise WorkspaceMigrationError(
                "workspace legacy ref inventory failed"
            ) from error
        snapshots: list[_WorkspaceLegacyRefSnapshot] = []
        issue_lifecycles: dict[int, IssueLifecycle] = {}
        state_plans: dict[int, object] = {}
        state_issues: set[int] = set()
        for revision, ref in entries:
            if _COMMIT.fullmatch(revision) is None:
                raise WorkspaceMigrationError(
                    "workspace legacy ref metadata is invalid"
                )
            kind, issue_number, state_schema_version = (
                _cleanup_ref_snapshot(ref)
            )
            if kind is WorkspaceLegacyRefKind.STATE:
                plan = state_plans.get(issue_number)
                if plan is None:
                    plan = detect_workspace_state_v3(
                        self._root,
                        issue_number,
                        remote=self._remote,
                    )
                    if plan is None or plan.source_revision != revision:
                        raise WorkspaceMigrationError(
                            "workspace state is not validated legacy v3"
                        )
                    state_plans[issue_number] = plan
            issue_lifecycle = issue_lifecycles.setdefault(
                issue_number,
                self._lifecycle.classify(issue_number),
            )
            if kind is WorkspaceLegacyRefKind.STATE:
                if issue_number in state_issues:
                    raise WorkspaceMigrationError(
                        "workspace legacy ref metadata is ambiguous"
                    )
                state_issues.add(issue_number)
            snapshots.append(
                _WorkspaceLegacyRefSnapshot(
                    issue_number=issue_number,
                    issue_lifecycle=issue_lifecycle,
                    ref_kind=kind,
                    ref=ref,
                    revision=revision,
                    state_schema_version=state_schema_version,
                )
            )
        return tuple(
            sorted(
                snapshots,
                key=_cleanup_snapshot_sort_key,
            )
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
        try:
            entries = list_remote_heads(
                self._root,
                safe_remote,
                (
                    "refs/heads/foundry-opt/state/issue-*",
                    "refs/heads/foundry-opt/inbox/issue-*",
                ),
            )
        except GitTransportError as error:
            raise WorkspaceMigrationError(
                "workspace legacy ref inventory failed"
            ) from error
        refs: dict[int, dict[str, str]] = {}
        for revision, ref in entries:
            if _COMMIT.fullmatch(revision) is None:
                raise WorkspaceMigrationError(
                    "workspace legacy ref metadata is invalid"
                )
            match = _LEGACY_REF.fullmatch(ref)
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
            issue_refs[kind] = revision
        return refs

    def _issue_revisions(
        self,
        issue_number: int,
    ) -> dict[str, str | None]:
        return {
            "audit": self._remote_revision(_audit_ref(issue_number)),
            "inbox": self._remote_revision(_inbox_ref(issue_number)),
            "migration": self._remote_revision(
                _conversion_ref(issue_number)
            ),
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
        refs: Mapping[str, str | None],
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
        try:
            returncode = atomic_compare_and_swap_delete(
                self._root,
                safe_remote,
                refs=refs,
                guard_ref=audit_ref,
                guard_revision=audit_revision,
            )
        except GitTransportError as error:
            raise WorkspaceMigrationError(
                "workspace archival compare-and-swap failed"
            ) from error
        remaining = {
            ref: self._remote_revision(ref) for ref in sorted(refs)
        }
        if returncode != 0 and any(remaining.values()):
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
    if set(value) != {"audit", "inbox", "migration", "state"}:
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


def _cleanup_ref_snapshot(
    ref: str,
) -> tuple[WorkspaceLegacyRefKind, int, int | None]:
    if (match := _CLEANUP_STATE_OR_INBOX_REF.fullmatch(ref)) is not None:
        return (
            WorkspaceLegacyRefKind(match.group("kind")),
            int(match.group("issue")),
            3 if match.group("kind") == "state" else None,
        )
    if (match := _CLEANUP_DESIGN_REF.fullmatch(ref)) is not None:
        return (
            WorkspaceLegacyRefKind.DESIGN,
            int(match.group("issue")),
            None,
        )
    if (match := _CLEANUP_SPEC_REF.fullmatch(ref)) is not None:
        return (
            WorkspaceLegacyRefKind.SPEC,
            int(match.group("issue")),
            None,
        )
    if (match := _CLEANUP_MIGRATION_REF.fullmatch(ref)) is not None:
        return (
            WorkspaceLegacyRefKind.MIGRATION,
            int(match.group("issue")),
            None,
        )
    raise WorkspaceMigrationError(
        "workspace legacy cleanup ref metadata is invalid"
    )


def _cleanup_snapshot_sort_key(
    snapshot: _WorkspaceLegacyRefSnapshot,
) -> tuple[int, int, str, str]:
    return (
        snapshot.issue_number,
        _cleanup_ref_kind_order(snapshot.ref_kind),
        snapshot.ref,
        snapshot.revision,
    )


def _cleanup_ref_kind_order(kind: WorkspaceLegacyRefKind) -> int:
    return {
        WorkspaceLegacyRefKind.STATE: 0,
        WorkspaceLegacyRefKind.INBOX: 1,
        WorkspaceLegacyRefKind.DESIGN: 2,
        WorkspaceLegacyRefKind.SPEC: 3,
        WorkspaceLegacyRefKind.MIGRATION: 4,
    }[kind]


def _cleanup_plan_items(
    snapshots: tuple[_WorkspaceLegacyRefSnapshot, ...],
) -> tuple[WorkspaceLegacyRefRecord, ...]:
    issue_has_state = {
        snapshot.issue_number
        for snapshot in snapshots
        if snapshot.ref_kind is WorkspaceLegacyRefKind.STATE
    }
    items: list[WorkspaceLegacyRefRecord] = []
    for snapshot in snapshots:
        action, reason = _cleanup_action_reason(
            snapshot,
            has_state=snapshot.issue_number in issue_has_state,
        )
        items.append(
            WorkspaceLegacyRefRecord(
                issue_number=snapshot.issue_number,
                issue_lifecycle=snapshot.issue_lifecycle,
                ref_kind=snapshot.ref_kind,
                ref=snapshot.ref,
                revision=snapshot.revision,
                action=action,
                reason=reason,
                state_schema_version=snapshot.state_schema_version,
            )
        )
    return tuple(items)


def _cleanup_action_reason(
    snapshot: _WorkspaceLegacyRefSnapshot,
    *,
    has_state: bool,
) -> tuple[str, str]:
    if snapshot.ref_kind is WorkspaceLegacyRefKind.STATE:
        return "retain", "state_requires_convert_audit"
    if snapshot.issue_lifecycle is not IssueLifecycle.CLOSED:
        return "retain", "issue_not_closed"
    if snapshot.ref_kind in {
        WorkspaceLegacyRefKind.INBOX,
        WorkspaceLegacyRefKind.MIGRATION,
    }:
        if has_state:
            return "retain", "state_requires_current_archive"
        return "delete", "closed_orphan_legacy_ref"
    if snapshot.ref_kind in {
        WorkspaceLegacyRefKind.DESIGN,
        WorkspaceLegacyRefKind.SPEC,
    }:
        return "delete", "closed_abandoned_legacy_ref"
    raise WorkspaceMigrationError(
        "workspace legacy cleanup ref metadata is invalid"
    )


def _cleanup_plan_reason(
    items: tuple[WorkspaceLegacyRefRecord, ...],
) -> str:
    reasons = [item.reason for item in items if item.action == "retain"]
    if "state_requires_convert_audit" in reasons:
        return "state_requires_convert_audit"
    if "state_requires_current_archive" in reasons:
        return "state_requires_current_archive"
    if "issue_not_closed" in reasons:
        return "issue_not_closed"
    return "no_eligible_cleanup_refs"


def _cleanup_plan_payload(
    *,
    remote: str,
    status: str,
    items: tuple[WorkspaceLegacyRefRecord, ...],
    deletions: tuple[WorkspaceLegacyRefRecord, ...],
    reason: str | None,
    apply: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "apply": apply,
        "deletions": [item.to_dict() for item in deletions],
        "items": [item.to_dict() for item in items],
        "remote": remote,
        "status": status,
    }
    if reason is not None:
        payload["reason"] = reason
    return payload


def _cleanup_plan_content_hash(payload: Mapping[str, object]) -> str:
    canonical = dict(payload)
    canonical.pop("content_hash", None)
    return hashlib.sha256(
        json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _cleanup_plan_document(
    document: Mapping[str, object],
) -> dict[str, object]:
    if type(document) is not dict:
        raise WorkspaceMigrationError(
            "workspace cleanup plan is invalid"
        )
    expected_keys = {
        "apply",
        "content_hash",
        "deletions",
        "items",
        "remote",
        "status",
    }
    if set(document) - (expected_keys | {"reason"}) != set():
        raise WorkspaceMigrationError(
            "workspace cleanup plan is invalid"
        )
    payload = dict(document)
    apply = payload.get("apply")
    content_hash = payload.get("content_hash")
    remote = payload.get("remote")
    status = payload.get("status")
    items = payload.get("items")
    deletions = payload.get("deletions")
    reason = payload.get("reason")
    if (
        type(apply) is not bool
        or type(content_hash) is not str
        or _SHA256.fullmatch(content_hash) is None
        or type(remote) is not str
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", remote)
        is None
        or type(status) is not str
        or status not in {"already_completed", "planned", "refused"}
        or type(items) is not list
        or type(deletions) is not list
        or (reason is not None and type(reason) is not str)
    ):
        raise WorkspaceMigrationError(
            "workspace cleanup plan is invalid"
        )
    payload["items"] = items
    payload["deletions"] = deletions
    return payload


def _cleanup_ref_record(
    document: Mapping[str, object],
) -> WorkspaceLegacyRefRecord:
    if type(document) is not dict:
        raise WorkspaceMigrationError(
            "workspace cleanup plan is invalid"
        )
    expected_keys = {
        "action",
        "issue_lifecycle",
        "issue_number",
        "reason",
        "ref",
        "ref_kind",
        "revision",
    }
    if set(document) - (expected_keys | {"state_schema_version"}) != set():
        raise WorkspaceMigrationError(
            "workspace cleanup plan is invalid"
        )
    action = document.get("action")
    issue_lifecycle = document.get("issue_lifecycle")
    issue_number = document.get("issue_number")
    reason = document.get("reason")
    ref = document.get("ref")
    ref_kind = document.get("ref_kind")
    revision = document.get("revision")
    state_schema_version = document.get("state_schema_version", None)
    if (
        type(action) is not str
        or action not in {"delete", "retain"}
        or type(issue_lifecycle) is not str
        or issue_lifecycle not in {value.value for value in IssueLifecycle}
        or type(issue_number) is not int
        or issue_number < 1
        or type(reason) is not str
        or type(ref) is not str
        or type(ref_kind) is not str
        or ref_kind not in {value.value for value in WorkspaceLegacyRefKind}
        or type(revision) is not str
        or _COMMIT.fullmatch(revision) is None
        or (
            state_schema_version is not None
            and not (
                type(state_schema_version) is int
                and state_schema_version >= 1
            )
        )
    ):
        raise WorkspaceMigrationError(
            "workspace cleanup plan is invalid"
        )
    return WorkspaceLegacyRefRecord(
        issue_number=issue_number,
        issue_lifecycle=IssueLifecycle(issue_lifecycle),
        ref_kind=WorkspaceLegacyRefKind(ref_kind),
        ref=ref,
        revision=revision,
        action=action,
        reason=reason,
        state_schema_version=state_schema_version,
    )
