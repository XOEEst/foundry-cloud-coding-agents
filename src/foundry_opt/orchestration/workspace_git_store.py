from __future__ import annotations

import hashlib
import json
from math import isfinite
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping
from uuid import uuid4

from foundry_opt.orchestration.git_transport import (
    compare_and_swap_push,
    fetch_revision,
    GitTransportError,
    remote_revision,
    resolve_safe_fetch_remote,
    resolve_safe_push_remote,
)
from foundry_opt.orchestration.workspace import WorkspacePhase
from foundry_opt.orchestration.workspace_state_migration import (
    WorkspaceStateConversionPayload,
    WorkspaceStateMigrationPlan,
    validate_workspace_state_conversion_payload,
    workspace_state_v3_migration_plan,
)
from foundry_opt.orchestration.workspace_store import (
    AuditBundle,
    CandidateSummary,
    WorkspaceBaselineRecord,
    WorkspaceExperimentRecord,
    WorkspaceLineage,
    WorkspaceSpecificationRecord,
    WorkspaceSnapshot,
    WorkspaceUpdate,
)
from foundry_opt.security import reject_secret_content


_SCHEMA_VERSION = 4
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_TEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,255}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_ENVIRONMENT = {
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
    "GIT_AUTHOR_EMAIL": "foundry-opt@example.invalid",
    "GIT_AUTHOR_NAME": "Foundry Optimizer Steward",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
    "GIT_COMMITTER_EMAIL": "foundry-opt@example.invalid",
    "GIT_COMMITTER_NAME": "Foundry Optimizer Steward",
}


class WorkspaceStoreError(RuntimeError):
    pass


class WorkspaceConflictError(WorkspaceStoreError, ValueError):
    pass


class WorkspaceCompletedError(WorkspaceConflictError):
    pass


class WorkspaceCorruptionError(WorkspaceStoreError):
    pass


class WorkspacePrivacyError(WorkspaceCorruptionError):
    pass


class WorkspaceMigrationRequiredError(WorkspaceStoreError):
    def __init__(self, plan: WorkspaceStateMigrationPlan) -> None:
        self.plan = plan
        super().__init__(
            "workspace state v3 requires an explicit read-only migration"
        )


class GitWorkspaceStore:
    """Compact v4 workspace state stored on private Git refs."""

    def __init__(
        self,
        repository_root: Path,
        *,
        remote: str = "origin",
    ) -> None:
        self._root = _repository_root(repository_root)
        _identifier(remote, "remote")
        self._remote = remote

    def load(self, issue_number: int) -> WorkspaceSnapshot | None:
        _positive_integer(issue_number, "issue_number")
        ref = _state_ref(issue_number)
        revision = self._remote_revision(ref)
        if revision is None:
            return None
        self._fetch(ref, revision)
        paths = self._paths(revision)
        if paths == ("completion.json",):
            tombstone = self._load_tombstone(
                issue_number,
                revision,
            )
            audit_revision = self._remote_revision(
                _audit_ref(issue_number)
            )
            if (
                audit_revision is not None
                and audit_revision != tombstone["audit_revision"]
            ):
                raise WorkspaceCorruptionError(
                    "workspace audit ref does not match completion tombstone"
                )
            return None
        return self._load_active(issue_number, revision, paths)[0]

    def load_audit(self, issue_number: int) -> WorkspaceSnapshot | None:
        _positive_integer(issue_number, "issue_number")
        ref = _audit_ref(issue_number)
        revision = self._remote_revision(ref)
        if revision is None:
            return None
        self._fetch(ref, revision)
        return self._load_active(
            issue_number,
            revision,
            self._paths(revision),
        )[0]

    def commit(
        self,
        *,
        expected_revision: str | None,
        update: WorkspaceUpdate,
    ) -> WorkspaceSnapshot:
        _validate_update(update)
        if expected_revision is not None:
            if _COMMIT.fullmatch(expected_revision) is None:
                raise ValueError("expected_revision must be a commit SHA")
        ref = _state_ref(update.issue_number)
        current_revision = self._remote_revision(ref)
        if current_revision is not None:
            self._fetch(ref, current_revision)
            paths = self._paths(current_revision)
            if paths == ("completion.json",):
                self._load_tombstone(update.issue_number, current_revision)
                raise WorkspaceCompletedError(
                    "completed workspace cannot be updated"
                )
        if current_revision != expected_revision:
            raise WorkspaceConflictError(
                "workspace revision changed since it was loaded"
            )

        journal: tuple[dict[str, Any], ...] = ()
        if current_revision is not None:
            current_snapshot, journal = self._load_active(
                update.issue_number,
                current_revision,
                paths,
            )
            if (
                current_snapshot.lineage is not None
                and update.lineage != current_snapshot.lineage
            ):
                raise WorkspaceConflictError(
                    "workspace lineage changed"
                )
            _validate_experiment_transition(
                current_snapshot.experiments,
                update.experiments,
                WorkspaceConflictError,
            )
            if (
                current_snapshot.specification is not None
                and update.specification
                != current_snapshot.specification
            ):
                raise WorkspaceConflictError(
                    "workspace specification changed"
                )
            _validate_baseline_transition(
                current_snapshot.baseline,
                update.baseline,
                WorkspaceConflictError,
            )
        candidates_content = (
            _canonical_json(_candidates_to_document(update.candidates))
            if update.candidates
            else None
        )
        candidates_sha256 = (
            hashlib.sha256(candidates_content).hexdigest()
            if candidates_content is not None
            else None
        )
        patch_sha256 = (
            hashlib.sha256(update.selected_patch).hexdigest()
            if update.selected_patch is not None
            else None
        )
        state_document = _state_document(
            update,
            candidates_sha256=candidates_sha256,
            selected_patch_sha256=patch_sha256,
        )
        previous_hash = (
            journal[-1]["entry_sha256"] if journal else None
        )
        entry_without_hash = {
            **state_document,
            "index": len(journal) + 1,
            "previous_sha256": previous_hash,
            "schema_version": _SCHEMA_VERSION,
            "semantic_event": update.semantic_event,
        }
        entry = {
            **entry_without_hash,
            "entry_sha256": _document_sha256(entry_without_hash),
        }
        all_journal = (*journal, entry)
        snapshot_document = {
            "journal_head": entry["entry_sha256"],
            "schema_version": _SCHEMA_VERSION,
            "state": state_document,
        }
        files: dict[str, bytes] = {
            "journal.jsonl": b"".join(
                _canonical_json(item) for item in all_journal
            ),
            "snapshot.json": _canonical_json(snapshot_document),
        }
        if candidates_content is not None:
            files["evidence/candidates.json"] = candidates_content
        if update.selected_patch is not None:
            files["patches/selected.patch"] = update.selected_patch

        revision = self._write_commit(
            parent=current_revision,
            files=files,
            message=f"Update workspace state for issue-{update.issue_number}",
        )
        self._push(
            ref=ref,
            source_revision=revision,
            expected_revision=current_revision,
        )
        return WorkspaceSnapshot(
            issue_number=update.issue_number,
            revision=revision,
            phase=update.phase,
            workspace_pull_request_number=(
                update.workspace_pull_request_number
            ),
            candidates=update.candidates,
            selected_patch=update.selected_patch,
            external_operation_ids=update.external_operation_ids,
            experiments=update.experiments,
            lineage=update.lineage,
            specification=update.specification,
            baseline=update.baseline,
        )

    def finalize(self, issue_number: int) -> AuditBundle:
        _positive_integer(issue_number, "issue_number")
        state_ref = _state_ref(issue_number)
        audit_ref = _audit_ref(issue_number)
        active_revision = self._remote_revision(state_ref)
        if active_revision is None:
            raise WorkspaceConflictError("workspace state does not exist")
        self._fetch(state_ref, active_revision)
        paths = self._paths(active_revision)
        if paths == ("completion.json",):
            tombstone = self._load_tombstone(
                issue_number,
                active_revision,
            )
            final_revision = tombstone["audit_revision"]
        else:
            snapshot, _ = self._load_active(
                issue_number,
                active_revision,
                paths,
            )
            final_revision = snapshot.revision
            tombstone_document = {
                "audit_ref": audit_ref,
                "audit_revision": final_revision,
                "completed": True,
                "issue_number": issue_number,
                "schema_version": _SCHEMA_VERSION,
            }
            tombstone_revision = self._write_commit(
                parent=active_revision,
                files={
                    "completion.json": _canonical_json(tombstone_document)
                },
                message=f"Complete workspace state for issue-{issue_number}",
            )
            try:
                self._push(
                    ref=state_ref,
                    source_revision=tombstone_revision,
                    expected_revision=active_revision,
                )
            except WorkspaceConflictError:
                current = self._remote_revision(state_ref)
                if current is None:
                    raise
                self._fetch(state_ref, current)
                current_tombstone = self._load_tombstone(
                    issue_number,
                    current,
                )
                if current_tombstone["audit_revision"] != final_revision:
                    raise WorkspaceConflictError(
                        "workspace changed during finalization"
                    )

        existing_audit = self._remote_revision(audit_ref)
        if existing_audit is None:
            self._push(
                ref=audit_ref,
                source_revision=final_revision,
                expected_revision=None,
            )
        elif existing_audit != final_revision:
            raise WorkspaceCorruptionError(
                "workspace audit ref conflicts with completion tombstone"
            )
        self._fetch(audit_ref, final_revision)
        snapshot, journal = self._load_active(
            issue_number,
            final_revision,
            self._paths(final_revision),
        )
        retained_paths = ["snapshot.json", "journal.jsonl"]
        if snapshot.candidates:
            retained_paths.append("evidence/candidates.json")
        if snapshot.selected_patch is not None:
            retained_paths.append("patches/selected.patch")
        return AuditBundle(
            issue_number=issue_number,
            final_snapshot=snapshot,
            journal=tuple(
                str(entry["semantic_event"]) for entry in journal
            ),
            candidates=snapshot.candidates,
            selected_patch=snapshot.selected_patch,
            external_operation_ids=snapshot.external_operation_ids,
            experiments=snapshot.experiments,
            lineage=snapshot.lineage,
            specification=snapshot.specification,
            baseline=snapshot.baseline,
            retained_paths=tuple(retained_paths),
        )

    def write_conversion(
        self,
        payload: WorkspaceStateConversionPayload,
        *,
        target_ref: str,
        expected_revision: str | None,
    ) -> WorkspaceSnapshot:
        validate_workspace_state_conversion_payload(payload)
        _conversion_target_ref(self._root, target_ref)
        if target_ref == payload.source_ref:
            raise ValueError("conversion target must not be the v3 source ref")
        if expected_revision is not None and (
            _COMMIT.fullmatch(expected_revision) is None
        ):
            raise ValueError("expected_revision must be a commit SHA")

        source_revision = self._remote_revision(payload.source_ref)
        if source_revision != payload.source_revision:
            raise WorkspaceConflictError(
                "workspace state v3 changed before conversion write"
            )
        self._fetch(payload.source_ref, payload.source_revision)
        files, final_update = _conversion_files(payload.transitions)
        current_revision = self._remote_revision(target_ref)
        if current_revision is not None:
            self._fetch(target_ref, current_revision)
            if self._revision_matches_files(current_revision, files):
                if (
                    self._revision_parent(current_revision)
                    != payload.source_revision
                ):
                    raise WorkspaceConflictError(
                        "workspace conversion target source lineage changed"
                    )
                return self._load_active(
                    payload.issue_number,
                    current_revision,
                    self._paths(current_revision),
                )[0]
            raise WorkspaceConflictError(
                "workspace conversion target already exists"
            )
        if current_revision != expected_revision:
            raise WorkspaceConflictError(
                "workspace conversion target revision changed"
            )

        revision = self._write_commit(
            parent=payload.source_revision,
            files=files,
            message=(
                f"Convert workspace state v3 to v4 for "
                f"issue-{payload.issue_number}"
            ),
        )
        try:
            self._push(
                ref=target_ref,
                source_revision=revision,
                expected_revision=None,
            )
        except WorkspaceConflictError:
            acknowledged = self._remote_revision(target_ref)
            if acknowledged != revision:
                raise
        if self._remote_revision(payload.source_ref) != payload.source_revision:
            raise WorkspaceConflictError(
                "workspace state v3 changed during conversion write"
            )
        return WorkspaceSnapshot(
            issue_number=payload.issue_number,
            revision=revision,
            phase=final_update.phase,
            workspace_pull_request_number=(
                final_update.workspace_pull_request_number
            ),
            candidates=final_update.candidates,
            selected_patch=final_update.selected_patch,
            external_operation_ids=final_update.external_operation_ids,
            experiments=final_update.experiments,
            lineage=final_update.lineage,
        )

    def _load_active(
        self,
        issue_number: int,
        revision: str,
        paths: tuple[str, ...],
    ) -> tuple[WorkspaceSnapshot, tuple[dict[str, Any], ...]]:
        allowed = {
            "snapshot.json",
            "journal.jsonl",
            "evidence/candidates.json",
            "patches/selected.patch",
        }
        try:
            snapshot_bytes = self._show(revision, "snapshot.json")
            raw_snapshot = json.loads(snapshot_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WorkspaceCorruptionError(
                "workspace snapshot is not valid JSON"
            ) from error
        if (
            type(raw_snapshot) is dict
            and raw_snapshot.get("schema_version") == 3
        ):
            try:
                plan = workspace_state_v3_migration_plan(
                    issue_number=issue_number,
                    source_revision=revision,
                    source_paths=paths,
                )
            except ValueError as error:
                raise WorkspaceCorruptionError(
                    "legacy workspace state paths are invalid"
                ) from error
            raise WorkspaceMigrationRequiredError(plan)
        if (
            set(paths) - allowed
            or "snapshot.json" not in paths
            or "journal.jsonl" not in paths
            or len(paths) != len(set(paths))
        ):
            raise WorkspaceCorruptionError(
                "workspace state contains unexpected paths"
            )
        snapshot_document = _read_document(
            snapshot_bytes,
            "workspace snapshot",
        )
        _exact_keys(
            snapshot_document,
            {"journal_head", "schema_version", "state"},
            "workspace snapshot",
        )
        _version(snapshot_document["schema_version"], "workspace snapshot")
        _sha256(snapshot_document["journal_head"], "journal head")
        state_document = snapshot_document["state"]
        _validate_state_document(state_document, issue_number)

        journal_bytes = self._show(revision, "journal.jsonl")
        if not journal_bytes or not journal_bytes.endswith(b"\n"):
            raise WorkspaceCorruptionError("workspace journal is invalid")
        journal = tuple(
            _read_document(line, "workspace journal entry")
            for line in journal_bytes.splitlines(keepends=True)
        )
        previous_hash: str | None = None
        final_state: dict[str, Any] | None = None
        for index, entry in enumerate(journal, 1):
            _validate_journal_entry(
                entry,
                issue_number=issue_number,
                expected_index=index,
                previous_hash=previous_hash,
            )
            previous_hash = entry["entry_sha256"]
            final_state = {
                key: entry[key]
                for key in _STATE_KEYS
            }
        if not journal or final_state != state_document:
            raise WorkspaceCorruptionError(
                "workspace snapshot does not match journal replay"
            )
        if snapshot_document["journal_head"] != previous_hash:
            raise WorkspaceCorruptionError(
                "workspace journal head is invalid"
            )

        candidate_path = "evidence/candidates.json"
        candidate_hash = state_document["candidates_sha256"]
        if (candidate_path in paths) != (candidate_hash is not None):
            raise WorkspaceCorruptionError(
                "workspace candidate evidence presence is invalid"
            )
        candidates: tuple[CandidateSummary, ...] = ()
        if candidate_hash is not None:
            candidates_bytes = self._show(revision, candidate_path)
            if hashlib.sha256(candidates_bytes).hexdigest() != candidate_hash:
                raise WorkspaceCorruptionError(
                    "workspace candidate evidence hash is invalid"
                )
            candidates = _candidates_from_document(
                _read_document(
                    candidates_bytes,
                    "workspace candidate evidence",
                )
            )

        patch_path = "patches/selected.patch"
        patch_hash = state_document["selected_patch_sha256"]
        if (patch_path in paths) != (patch_hash is not None):
            raise WorkspaceCorruptionError(
                "workspace selected patch presence is invalid"
            )
        selected_patch: bytes | None = None
        if patch_hash is not None:
            selected_patch = self._show(revision, patch_path)
            if hashlib.sha256(selected_patch).hexdigest() != patch_hash:
                raise WorkspaceCorruptionError(
                    "workspace selected patch hash is invalid"
                )
            _validate_patch(selected_patch, WorkspaceCorruptionError)

        try:
            phase = WorkspacePhase(state_document["phase"])
        except ValueError as error:
            raise WorkspaceCorruptionError(
                "workspace phase is invalid"
            ) from error
        lineage = _lineage_from_document(state_document["lineage"])
        if lineage is not None:
            _validate_lineage_state(
                lineage,
                candidates=candidates,
                selected_patch=selected_patch,
                workspace_pull_request_number=(
                    state_document["workspace_pull_request_number"]
                ),
                error_type=WorkspaceCorruptionError,
            )
        return (
            WorkspaceSnapshot(
                issue_number=issue_number,
                revision=revision,
                phase=phase,
                workspace_pull_request_number=(
                    state_document["workspace_pull_request_number"]
                ),
                candidates=candidates,
                selected_patch=selected_patch,
                external_operation_ids=tuple(
                    state_document["external_operation_ids"]
                ),
                experiments=_experiments_from_document(
                    state_document["experiments"]
                ),
                lineage=lineage,
                specification=_specification_from_document(
                    state_document["specification"]
                ),
                baseline=_baseline_from_document(
                    state_document["baseline"]
                ),
            ),
            journal,
        )

    def _load_tombstone(
        self,
        issue_number: int,
        revision: str,
    ) -> dict[str, Any]:
        document = _read_document(
            self._show(revision, "completion.json"),
            "workspace completion tombstone",
        )
        _exact_keys(
            document,
            {
                "audit_ref",
                "audit_revision",
                "completed",
                "issue_number",
                "schema_version",
            },
            "workspace completion tombstone",
        )
        _version(
            document["schema_version"],
            "workspace completion tombstone",
        )
        try:
            _positive_integer(
                document["issue_number"],
                "workspace tombstone issue",
            )
        except ValueError as error:
            raise WorkspaceCorruptionError(
                "workspace completion tombstone is invalid"
            ) from error
        if (
            document["issue_number"] != issue_number
            or document["completed"] is not True
            or document["audit_ref"] != _audit_ref(issue_number)
        ):
            raise WorkspaceCorruptionError(
                "workspace completion tombstone is invalid"
            )
        _commit(document["audit_revision"], "audit_revision")
        _validate_privacy(document)
        return document

    def _paths(self, revision: str) -> tuple[str, ...]:
        return tuple(
            line
            for line in _git_text(
                self._root,
                "ls-tree",
                "-r",
                "--name-only",
                revision,
            ).splitlines()
            if line
        )

    def _show(self, revision: str, path: str) -> bytes:
        try:
            return _git_bytes(
                self._root,
                "show",
                f"{revision}:{path}",
            )
        except WorkspaceStoreError as error:
            raise WorkspaceCorruptionError(
                f"workspace state is missing {path}"
            ) from error

    def _revision_matches_files(
        self,
        revision: str,
        files: Mapping[str, bytes],
    ) -> bool:
        if set(self._paths(revision)) != set(files):
            return False
        return all(
            self._show(revision, path) == content
            for path, content in files.items()
        )

    def _revision_parent(self, revision: str) -> str:
        fields = _git_text(
            self._root,
            "rev-list",
            "--parents",
            "-n",
            "1",
            revision,
        ).split()
        if (
            len(fields) != 2
            or fields[0] != revision
            or _COMMIT.fullmatch(fields[1]) is None
        ):
            raise WorkspaceCorruptionError(
                "workspace conversion target parent is invalid"
            )
        return fields[1]

    def _remote_revision(self, ref: str) -> str | None:
        safe_remote = resolve_safe_fetch_remote(
            self._root,
            self._remote,
        )
        if safe_remote is None:
            raise WorkspaceStoreError(
                "workspace state fetch destination is not trusted"
            )
        try:
            revision = remote_revision(self._root, safe_remote, ref)
        except GitTransportError as error:
            raise WorkspaceStoreError(
                "workspace state ref query failed"
            ) from error
        if revision is not None:
            _commit(revision, "workspace revision")
        return revision

    def _fetch(self, ref: str, revision: str) -> None:
        safe_remote = resolve_safe_fetch_remote(
            self._root,
            self._remote,
        )
        if safe_remote is None:
            raise WorkspaceStoreError(
                "workspace state fetch destination is not trusted"
            )
        try:
            fetched = fetch_revision(self._root, safe_remote, ref)
        except GitTransportError as error:
            raise WorkspaceStoreError(
                "workspace state ref fetch failed"
            ) from error
        if fetched != revision:
            raise WorkspaceConflictError(
                "workspace state changed while it was loaded"
            )

    def _push(
        self,
        *,
        ref: str,
        source_revision: str,
        expected_revision: str | None,
    ) -> None:
        safe_remote = resolve_safe_push_remote(
            self._root,
            self._remote,
        )
        if safe_remote is None:
            raise WorkspaceConflictError(
                "workspace state push destination is not trusted"
            )
        try:
            pushed = compare_and_swap_push(
                self._root,
                safe_remote,
                source_revision=source_revision,
                destination_ref=ref,
                expected_revision=expected_revision,
            )
        except GitTransportError as error:
            raise WorkspaceConflictError(
                "workspace state transport failed"
            ) from error
        if (
            pushed.before != expected_revision
            or pushed.returncode != 0
            or pushed.after != source_revision
        ):
            raise WorkspaceConflictError(
                "workspace state compare-and-swap failed"
            )

    def _write_commit(
        self,
        *,
        parent: str | None,
        files: Mapping[str, bytes],
        message: str,
    ) -> str:
        git_dir_text = _git_text(
            self._root,
            "rev-parse",
            "--git-dir",
        )
        git_dir = Path(git_dir_text)
        if not git_dir.is_absolute():
            git_dir = self._root / git_dir
        index_path = (
            git_dir / f"foundry-workspace-{os.getpid()}-{uuid4().hex}"
        )
        environment = {"GIT_INDEX_FILE": str(index_path)}
        try:
            _run(
                self._root,
                "git",
                "read-tree",
                "--empty",
                environment=environment,
            )
            for path, content in sorted(files.items()):
                blob = _run(
                    self._root,
                    "git",
                    "hash-object",
                    "-w",
                    "--stdin",
                    input_bytes=content,
                ).stdout.decode("ascii").strip()
                _run(
                    self._root,
                    "git",
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    "100644",
                    blob,
                    path,
                    environment=environment,
                )
            tree = _git_text(
                self._root,
                "write-tree",
                environment=environment,
            )
            arguments = ["git", "commit-tree", tree]
            if parent is not None:
                arguments.extend(("-p", parent))
            arguments.extend(("-m", message))
            revision = _run(
                self._root,
                *arguments,
                environment={**environment, **_COMMIT_ENVIRONMENT},
            ).stdout.decode("ascii").strip()
        finally:
            index_path.unlink(missing_ok=True)
            Path(f"{index_path}.lock").unlink(missing_ok=True)
        _commit(revision, "workspace commit")
        return revision


_STATE_KEYS = (
    "baseline",
    "candidates_sha256",
    "external_operation_ids",
    "experiments",
    "issue_number",
    "lineage",
    "phase",
    "selected_patch_sha256",
    "specification",
    "workspace_pull_request_number",
)


def _state_document(
    update: WorkspaceUpdate,
    *,
    candidates_sha256: str | None,
    selected_patch_sha256: str | None,
) -> dict[str, Any]:
    return {
        "baseline": _baseline_to_document(update.baseline),
        "candidates_sha256": candidates_sha256,
        "external_operation_ids": list(update.external_operation_ids),
        "experiments": [
            _experiment_to_document(item)
            for item in update.experiments
        ],
        "issue_number": update.issue_number,
        "lineage": _lineage_to_document(update.lineage),
        "phase": update.phase.value,
        "selected_patch_sha256": selected_patch_sha256,
        "specification": _specification_to_document(
            update.specification
        ),
        "workspace_pull_request_number": (
            update.workspace_pull_request_number
        ),
    }


def _conversion_files(
    transitions: tuple[WorkspaceUpdate, ...],
) -> tuple[dict[str, bytes], WorkspaceUpdate]:
    if not transitions:
        raise ValueError("workspace conversion requires transitions")
    journal: list[dict[str, Any]] = []
    final_candidates: bytes | None = None
    final_patch: bytes | None = None
    final_state: dict[str, Any] | None = None
    for update in transitions:
        _validate_update(update)
        candidates_content = (
            _canonical_json(_candidates_to_document(update.candidates))
            if update.candidates
            else None
        )
        candidates_sha256 = (
            hashlib.sha256(candidates_content).hexdigest()
            if candidates_content is not None
            else None
        )
        patch_sha256 = (
            hashlib.sha256(update.selected_patch).hexdigest()
            if update.selected_patch is not None
            else None
        )
        state_document = _state_document(
            update,
            candidates_sha256=candidates_sha256,
            selected_patch_sha256=patch_sha256,
        )
        entry_without_hash = {
            **state_document,
            "index": len(journal) + 1,
            "previous_sha256": (
                journal[-1]["entry_sha256"] if journal else None
            ),
            "schema_version": _SCHEMA_VERSION,
            "semantic_event": update.semantic_event,
        }
        journal.append(
            {
                **entry_without_hash,
                "entry_sha256": _document_sha256(entry_without_hash),
            }
        )
        final_candidates = candidates_content
        final_patch = update.selected_patch
        final_state = state_document
    assert final_state is not None
    snapshot_document = {
        "journal_head": journal[-1]["entry_sha256"],
        "schema_version": _SCHEMA_VERSION,
        "state": final_state,
    }
    files = {
        "journal.jsonl": b"".join(
            _canonical_json(item) for item in journal
        ),
        "snapshot.json": _canonical_json(snapshot_document),
    }
    if final_candidates is not None:
        files["evidence/candidates.json"] = final_candidates
    if final_patch is not None:
        files["patches/selected.patch"] = final_patch
    return files, transitions[-1]


def _validate_update(update: WorkspaceUpdate) -> None:
    if type(update) is not WorkspaceUpdate:
        raise ValueError("update must be a WorkspaceUpdate")
    _positive_integer(update.issue_number, "issue_number")
    if type(update.phase) is not WorkspacePhase:
        raise ValueError("phase must be a WorkspacePhase")
    if update.workspace_pull_request_number is not None:
        _positive_integer(
            update.workspace_pull_request_number,
            "workspace_pull_request_number",
        )
    _identifier(update.semantic_event, "semantic_event")
    _validate_candidates(update.candidates, WorkspacePrivacyError)
    _validate_external_operation_ids(
        update.external_operation_ids,
        WorkspacePrivacyError,
    )
    _validate_experiments(
        update.experiments,
        WorkspacePrivacyError,
    )
    if (
        update.specification is not None
        and type(update.specification) is not WorkspaceSpecificationRecord
    ):
        raise WorkspacePrivacyError(
            "workspace specification record is invalid"
        )
    if (
        update.baseline is not None
        and type(update.baseline) is not WorkspaceBaselineRecord
    ):
        raise WorkspacePrivacyError("workspace baseline record is invalid")
    if update.selected_patch is not None:
        _validate_patch(update.selected_patch, WorkspacePrivacyError)
    if update.lineage is not None:
        _validate_lineage_state(
            update.lineage,
            candidates=update.candidates,
            selected_patch=update.selected_patch,
            workspace_pull_request_number=(
                update.workspace_pull_request_number
            ),
            error_type=WorkspacePrivacyError,
        )
    _validate_privacy(
        {
            "semantic_event": update.semantic_event,
            "external_operation_ids": list(
                update.external_operation_ids
            ),
            "candidates": [
                {
                    "candidate_id": item.candidate_id,
                    "metrics": dict(item.metrics),
                }
                for item in update.candidates
            ],
            "experiments": [
                _experiment_to_document(item)
                for item in update.experiments
            ],
        }
    )


def _candidates_to_document(
    candidates: tuple[CandidateSummary, ...],
) -> dict[str, Any]:
    return {
        "candidates": [
            {
                "candidate_id": item.candidate_id,
                "eligible": item.eligible,
                "metrics": dict(item.metrics),
                "selected": item.selected,
            }
            for item in candidates
        ],
        "schema_version": _SCHEMA_VERSION,
    }


def _candidates_from_document(
    document: Any,
) -> tuple[CandidateSummary, ...]:
    _exact_keys(
        document,
        {"candidates", "schema_version"},
        "workspace candidate evidence",
    )
    _version(document["schema_version"], "workspace candidate evidence")
    if type(document["candidates"]) is not list:
        raise WorkspaceCorruptionError(
            "workspace candidates must be a list"
        )
    try:
        candidates = tuple(
            CandidateSummary(
                candidate_id=item["candidate_id"],
                metrics=item["metrics"],
                eligible=item["eligible"],
                selected=item["selected"],
            )
            for item in document["candidates"]
            if _candidate_document(item)
        )
    except (KeyError, TypeError, ValueError) as error:
        raise WorkspaceCorruptionError(
            "workspace candidate evidence is invalid"
        ) from error
    _validate_candidates(candidates, WorkspaceCorruptionError)
    _validate_privacy(document)
    return candidates


def _candidate_document(value: Any) -> bool:
    _exact_keys(
        value,
        {"candidate_id", "eligible", "metrics", "selected"},
        "workspace candidate",
    )
    return True


def _validate_candidates(
    candidates: tuple[CandidateSummary, ...],
    error_type: type[WorkspaceCorruptionError],
) -> None:
    if type(candidates) is not tuple:
        raise error_type("workspace candidates must be a tuple")
    seen: set[str] = set()
    selected = 0
    for candidate in candidates:
        if type(candidate) is not CandidateSummary:
            raise error_type("workspace candidate is invalid")
        try:
            _identifier(candidate.candidate_id, "candidate_id")
        except ValueError as error:
            raise error_type("workspace candidate ID is invalid") from error
        if candidate.candidate_id in seen:
            raise error_type("workspace candidate IDs are not unique")
        seen.add(candidate.candidate_id)
        if type(candidate.eligible) is not bool or type(
            candidate.selected
        ) is not bool:
            raise error_type("workspace candidate flags are invalid")
        if candidate.selected and not candidate.eligible:
            raise error_type("selected workspace candidate is not eligible")
        selected += int(candidate.selected)
        if not isinstance(candidate.metrics, Mapping):
            raise error_type("workspace candidate metrics are invalid")
        for name, value in candidate.metrics.items():
            try:
                _identifier(name, "metric name")
            except ValueError as error:
                raise error_type(
                    "workspace candidate metric name is invalid"
                ) from error
            if (
                type(value) not in {int, float}
                or isinstance(value, bool)
                or not isfinite(value)
            ):
                raise error_type(
                    "workspace candidate metric value is invalid"
                )
    if selected > 1:
        raise error_type("multiple workspace candidates are selected")


def _validate_external_operation_ids(
    values: tuple[str, ...],
    error_type: type[WorkspaceCorruptionError],
) -> None:
    if type(values) is not tuple or len(values) != len(set(values)):
        raise error_type("external operation IDs are invalid")
    for value in values:
        if type(value) is not str or _SAFE_TEXT.fullmatch(value) is None:
            raise error_type("external operation ID is invalid")


def _validate_patch(
    content: bytes,
    error_type: type[WorkspaceCorruptionError],
) -> None:
    if type(content) is not bytes or not content or len(content) > 10_000_000:
        raise error_type("workspace selected patch is invalid")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise error_type(
            "workspace selected patch must be UTF-8"
        ) from error
    if "\x00" in text:
        raise error_type("workspace selected patch is invalid")
    try:
        reject_secret_content(text)
    except ValueError as error:
        raise error_type(
            "workspace selected patch contains sensitive content"
        ) from error


def _validate_state_document(document: Any, issue_number: int) -> None:
    _exact_keys(document, set(_STATE_KEYS), "workspace state")
    try:
        _positive_integer(document["issue_number"], "workspace issue")
    except ValueError as error:
        raise WorkspaceCorruptionError(
            "workspace state issue is invalid"
        ) from error
    if document["issue_number"] != issue_number:
        raise WorkspaceCorruptionError(
            "workspace state issue does not match ref"
        )
    try:
        WorkspacePhase(document["phase"])
    except (TypeError, ValueError) as error:
        raise WorkspaceCorruptionError(
            "workspace phase is invalid"
        ) from error
    pull_request = document["workspace_pull_request_number"]
    if pull_request is not None:
        try:
            _positive_integer(pull_request, "workspace pull request")
        except ValueError as error:
            raise WorkspaceCorruptionError(
                "workspace pull request number is invalid"
            ) from error
    for key in ("candidates_sha256", "selected_patch_sha256"):
        value = document[key]
        if value is not None:
            _sha256(value, key)
    external_ids = document["external_operation_ids"]
    if type(external_ids) is not list:
        raise WorkspaceCorruptionError(
            "external operation IDs are invalid"
        )
    _validate_external_operation_ids(
        tuple(external_ids),
        WorkspaceCorruptionError,
    )
    _experiments_from_document(document["experiments"])
    _lineage_from_document(document["lineage"])
    _specification_from_document(document["specification"])
    _baseline_from_document(document["baseline"])
    _validate_privacy(document)


def _specification_to_document(
    record: WorkspaceSpecificationRecord | None,
) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "asset_ids": list(record.asset_ids),
        "base_commit": record.base_commit,
        "environment": record.environment,
        "metric_names": list(record.metric_names),
        "policy_reason": record.policy_reason,
        "spec_sha256": record.spec_sha256,
        "status": record.status,
        "target": record.target,
    }


def _specification_from_document(
    value: Any,
) -> WorkspaceSpecificationRecord | None:
    if value is None:
        return None
    _exact_keys(
        value,
        {
            "asset_ids",
            "base_commit",
            "environment",
            "metric_names",
            "policy_reason",
            "spec_sha256",
            "status",
            "target",
        },
        "workspace specification",
    )
    try:
        return WorkspaceSpecificationRecord(
            status=value["status"],
            spec_sha256=value["spec_sha256"],
            base_commit=value["base_commit"],
            target=value["target"],
            environment=value["environment"],
            asset_ids=tuple(value["asset_ids"]),
            metric_names=tuple(value["metric_names"]),
            policy_reason=value["policy_reason"],
        )
    except (TypeError, ValueError) as error:
        raise WorkspaceCorruptionError(
            "workspace specification is invalid"
        ) from error


def _baseline_to_document(
    record: WorkspaceBaselineRecord | None,
) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "bundle_sha256": record.bundle_sha256,
        "dataset_ids": list(record.dataset_ids),
        "draft_id": record.draft_id,
        "evaluation_id": record.evaluation_id,
        "evaluator_ids": list(record.evaluator_ids),
        "evidence_sha256": record.evidence_sha256,
        "executor": record.executor,
        "guardrails": dict(record.guardrails),
        "idempotency_key": record.idempotency_key,
        "metrics": dict(record.metrics),
        "operation_sha256": record.operation_sha256,
        "run_id": record.run_id,
        "sample_count": record.sample_count,
        "split": record.split,
        "status": record.status,
    }


def _baseline_from_document(
    value: Any,
) -> WorkspaceBaselineRecord | None:
    if value is None:
        return None
    _exact_keys(
        value,
        {
            "bundle_sha256",
            "dataset_ids",
            "draft_id",
            "evaluation_id",
            "evaluator_ids",
            "evidence_sha256",
            "executor",
            "guardrails",
            "idempotency_key",
            "metrics",
            "operation_sha256",
            "run_id",
            "sample_count",
            "split",
            "status",
        },
        "workspace baseline",
    )
    try:
        return WorkspaceBaselineRecord(
            status=value["status"],
            operation_sha256=value["operation_sha256"],
            idempotency_key=value["idempotency_key"],
            bundle_sha256=value["bundle_sha256"],
            evidence_sha256=value["evidence_sha256"],
            dataset_ids=tuple(value["dataset_ids"]),
            evaluator_ids=tuple(value["evaluator_ids"]),
            split=value["split"],
            sample_count=value["sample_count"],
            executor=value["executor"],
            draft_id=value["draft_id"],
            evaluation_id=value["evaluation_id"],
            run_id=value["run_id"],
            metrics=value["metrics"],
            guardrails=value["guardrails"],
        )
    except (TypeError, ValueError) as error:
        raise WorkspaceCorruptionError(
            "workspace baseline is invalid"
        ) from error


def _experiment_to_document(
    record: WorkspaceExperimentRecord,
) -> dict[str, Any]:
    return {
        "bundle_sha256": record.bundle_sha256,
        "candidate_id": record.candidate_id,
        "changed_paths": list(record.changed_paths),
        "draft_id": record.draft_id,
        "evaluation_id": record.evaluation_id,
        "evidence_sha256": record.evidence_sha256,
        "executor": record.executor,
        "guardrails": dict(record.guardrails),
        "idempotency_key": record.idempotency_key,
        "metrics": dict(record.metrics),
        "mutation_class": record.mutation_class,
        "operation_sha256": record.operation_sha256,
        "patch_sha256": record.patch_sha256,
        "run_id": record.run_id,
        "status": record.status,
        "validation": list(record.validation),
        "expected_tree": record.expected_tree,
    }


def _experiments_from_document(
    value: Any,
) -> tuple[WorkspaceExperimentRecord, ...]:
    if type(value) is not list:
        raise WorkspaceCorruptionError(
            "workspace experiments are invalid"
        )
    try:
        records = tuple(
            WorkspaceExperimentRecord(
                candidate_id=item["candidate_id"],
                mutation_class=item["mutation_class"],
                patch_sha256=item["patch_sha256"],
                bundle_sha256=item["bundle_sha256"],
                evidence_sha256=item["evidence_sha256"],
                idempotency_key=item["idempotency_key"],
                operation_sha256=item["operation_sha256"],
                status=item["status"],
                changed_paths=tuple(item["changed_paths"]),
                validation=tuple(item["validation"]),
                expected_tree=item["expected_tree"],
                executor=item["executor"],
                draft_id=item["draft_id"],
                evaluation_id=item["evaluation_id"],
                run_id=item["run_id"],
                metrics=item["metrics"],
                guardrails=item["guardrails"],
            )
            for item in value
            if _experiment_document(item)
        )
    except (KeyError, TypeError, ValueError) as error:
        raise WorkspaceCorruptionError(
            "workspace experiment record is invalid"
        ) from error
    _validate_experiments(records, WorkspaceCorruptionError)
    return records


def _experiment_document(value: Any) -> bool:
    _exact_keys(
        value,
        {
            "bundle_sha256",
            "candidate_id",
            "changed_paths",
            "draft_id",
            "evaluation_id",
            "evidence_sha256",
            "executor",
            "guardrails",
            "idempotency_key",
            "metrics",
            "mutation_class",
            "operation_sha256",
            "patch_sha256",
            "run_id",
            "status",
            "validation",
            "expected_tree",
        },
        "workspace experiment",
    )
    return True


def _validate_experiments(
    records: tuple[WorkspaceExperimentRecord, ...],
    error_type: type[WorkspaceCorruptionError],
) -> None:
    if (
        type(records) is not tuple
        or any(type(item) is not WorkspaceExperimentRecord for item in records)
        or len({item.candidate_id for item in records}) != len(records)
        or len({item.idempotency_key for item in records}) != len(records)
        or len({item.operation_sha256 for item in records}) != len(records)
    ):
        raise error_type("workspace experiment records are invalid")


def _validate_experiment_transition(
    previous: tuple[WorkspaceExperimentRecord, ...],
    current: tuple[WorkspaceExperimentRecord, ...],
    error_type: type[WorkspaceCorruptionError],
) -> None:
    _validate_experiments(current, error_type)
    current_by_id = {item.candidate_id: item for item in current}
    for prior in previous:
        item = current_by_id.get(prior.candidate_id)
        if item is None:
            raise error_type("workspace experiment record was removed")
        if prior.status == "completed" and item != prior:
            raise error_type("completed workspace experiment changed")
        if prior.status == "pending" and (
            item.mutation_class != prior.mutation_class
            or item.patch_sha256 != prior.patch_sha256
            or item.bundle_sha256 != prior.bundle_sha256
            or item.evidence_sha256 != prior.evidence_sha256
            or item.idempotency_key != prior.idempotency_key
            or item.operation_sha256 != prior.operation_sha256
            or item.changed_paths != prior.changed_paths
            or item.validation != prior.validation
            or item.expected_tree != prior.expected_tree
        ):
            raise error_type("workspace experiment lineage changed")


def _validate_baseline_transition(
    previous: WorkspaceBaselineRecord | None,
    current: WorkspaceBaselineRecord | None,
    error_type: type[WorkspaceCorruptionError],
) -> None:
    if previous is None:
        return
    if current is None:
        raise error_type("workspace baseline was removed")
    if previous.status == "completed" and current != previous:
        raise error_type("completed workspace baseline changed")
    if previous.status == "pending" and (
        current.operation_sha256 != previous.operation_sha256
        or current.idempotency_key != previous.idempotency_key
        or current.bundle_sha256 != previous.bundle_sha256
        or current.evidence_sha256 != previous.evidence_sha256
        or current.dataset_ids != previous.dataset_ids
        or current.evaluator_ids != previous.evaluator_ids
        or current.split != previous.split
        or current.sample_count != previous.sample_count
    ):
        raise error_type("workspace baseline lineage changed")


def _lineage_to_document(
    lineage: WorkspaceLineage | None,
) -> dict[str, Any] | None:
    if lineage is None:
        return None
    return {
        "base_commit": lineage.base_commit,
        "bundle_sha256": lineage.bundle_sha256,
        "evidence_sha256": lineage.evidence_sha256,
        "expected_tree": lineage.expected_tree,
        "patch_sha256": lineage.patch_sha256,
        "required_checks": dict(lineage.required_checks),
        "required_checks_provenance": (
            lineage.required_checks_provenance
        ),
        "selected_candidate_id": lineage.selected_candidate_id,
        "spec_sha256": lineage.spec_sha256,
        "workspace_pull_request_number": (
            lineage.workspace_pull_request_number
        ),
    }


def _lineage_from_document(value: Any) -> WorkspaceLineage | None:
    if value is None:
        return None
    _exact_keys(
        value,
        {
            "base_commit",
            "bundle_sha256",
            "evidence_sha256",
            "expected_tree",
            "patch_sha256",
            "required_checks",
            "required_checks_provenance",
            "selected_candidate_id",
            "spec_sha256",
            "workspace_pull_request_number",
        },
        "workspace lineage",
    )
    try:
        return WorkspaceLineage(
            spec_sha256=value["spec_sha256"],
            base_commit=value["base_commit"],
            patch_sha256=value["patch_sha256"],
            evidence_sha256=value["evidence_sha256"],
            bundle_sha256=value["bundle_sha256"],
            expected_tree=value["expected_tree"],
            selected_candidate_id=value["selected_candidate_id"],
            workspace_pull_request_number=(
                value["workspace_pull_request_number"]
            ),
            required_checks=value["required_checks"],
            required_checks_provenance=(
                value["required_checks_provenance"]
            ),
        )
    except (TypeError, ValueError) as error:
        raise WorkspaceCorruptionError(
            "workspace lineage is invalid"
        ) from error


def _validate_lineage_state(
    lineage: WorkspaceLineage,
    *,
    candidates: tuple[CandidateSummary, ...],
    selected_patch: bytes | None,
    workspace_pull_request_number: int | None,
    error_type: type[WorkspaceCorruptionError],
) -> None:
    selected = tuple(item for item in candidates if item.selected)
    if (
        selected_patch is None
        or hashlib.sha256(selected_patch).hexdigest()
        != lineage.patch_sha256
        or workspace_pull_request_number
        != lineage.workspace_pull_request_number
        or len(selected) != 1
        or selected[0].candidate_id
        != lineage.selected_candidate_id
    ):
        raise error_type("workspace lineage does not match state")


def _validate_journal_entry(
    entry: Any,
    *,
    issue_number: int,
    expected_index: int,
    previous_hash: str | None,
) -> None:
    _exact_keys(
        entry,
        {
            *_STATE_KEYS,
            "entry_sha256",
            "index",
            "previous_sha256",
            "schema_version",
            "semantic_event",
        },
        "workspace journal entry",
    )
    _version(entry["schema_version"], "workspace journal entry")
    _validate_state_document(
        {key: entry[key] for key in _STATE_KEYS},
        issue_number,
    )
    if type(entry["index"]) is not int or entry["index"] != expected_index:
        raise WorkspaceCorruptionError(
            "workspace journal index is invalid"
        )
    if entry["previous_sha256"] is not None:
        _sha256(
            entry["previous_sha256"],
            "workspace journal previous hash",
        )
    if entry["previous_sha256"] != previous_hash:
        raise WorkspaceCorruptionError(
            "workspace journal hash chain is invalid"
        )
    try:
        _identifier(entry["semantic_event"], "semantic_event")
    except ValueError as error:
        raise WorkspaceCorruptionError(
            "workspace journal event is invalid"
        ) from error
    _sha256(entry["entry_sha256"], "workspace journal entry hash")
    entry_without_hash = {
        key: value
        for key, value in entry.items()
        if key != "entry_sha256"
    }
    if entry["entry_sha256"] != _document_sha256(entry_without_hash):
        raise WorkspaceCorruptionError(
            "workspace journal entry hash is invalid"
        )
    _validate_privacy(entry)


def _read_document(content: bytes, description: str) -> Any:
    try:
        document = json.loads(content)
        canonical = _canonical_json(document)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise WorkspaceCorruptionError(
            f"{description} is not valid JSON"
        ) from error
    if content != canonical:
        raise WorkspaceCorruptionError(
            f"{description} is not canonical JSON"
        )
    return document


def _canonical_json(document: Any) -> bytes:
    return (
        json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _document_sha256(document: Any) -> str:
    return hashlib.sha256(_canonical_json(document)).hexdigest()


def _validate_privacy(document: Any) -> None:
    try:
        reject_secret_content(document)
    except ValueError as error:
        raise WorkspacePrivacyError(
            "workspace state contains sensitive content"
        ) from error


def _exact_keys(value: Any, expected: set[str], description: str) -> None:
    if type(value) is not dict or set(value) != expected:
        raise WorkspaceCorruptionError(f"{description} fields are invalid")


def _version(value: Any, description: str) -> None:
    if type(value) is not int or value != _SCHEMA_VERSION:
        raise WorkspaceCorruptionError(
            f"unsupported {description} schema_version"
        )


def _identifier(value: Any, description: str) -> None:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{description} is invalid")


def _positive_integer(value: Any, description: str) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{description} must be a positive integer")


def _sha256(value: Any, description: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise WorkspaceCorruptionError(f"{description} is invalid")


def _commit(value: Any, description: str) -> None:
    if type(value) is not str or _COMMIT.fullmatch(value) is None:
        raise WorkspaceCorruptionError(f"{description} is invalid")


def _repository_root(path: Path) -> Path:
    root = Path(os.path.abspath(path))
    try:
        discovered = Path(
            os.path.abspath(
                _git_text(root, "rev-parse", "--show-toplevel")
            )
        )
    except WorkspaceStoreError as error:
        raise ValueError("repository_root must be a Git worktree") from error
    if os.path.normcase(discovered) != os.path.normcase(root):
        raise ValueError("repository_root must be the Git worktree root")
    return root


def _state_ref(issue_number: int) -> str:
    return f"refs/heads/foundry-opt/state/issue-{issue_number}"


def _audit_ref(issue_number: int) -> str:
    return f"refs/heads/foundry-opt/audit/issue-{issue_number}"


def _conversion_target_ref(root: Path, value: str) -> None:
    if (
        type(value) is not str
        or not value.startswith("refs/heads/foundry-opt/")
        or value.startswith(
            (
                "refs/heads/foundry-opt/state/",
                "refs/heads/foundry-opt/audit/",
            )
        )
    ):
        raise ValueError("conversion target ref is invalid")
    checked = subprocess.run(
        ("git", "check-ref-format", value),
        cwd=root,
        capture_output=True,
        check=False,
    )
    if checked.returncode != 0:
        raise ValueError("conversion target ref is invalid")


def _run(
    cwd: Path,
    *arguments: str,
    input_bytes: bytes | None = None,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    process_environment = os.environ.copy()
    if environment:
        process_environment.update(environment)
    completed = subprocess.run(
        arguments,
        cwd=cwd,
        env=process_environment,
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise WorkspaceStoreError("Git workspace state operation failed")
    return completed


def _git_text(
    cwd: Path,
    *arguments: str,
    environment: Mapping[str, str] | None = None,
) -> str:
    return _run(
        cwd,
        "git",
        *arguments,
        environment=environment,
    ).stdout.decode("utf-8").strip()


def _git_bytes(cwd: Path, *arguments: str) -> bytes:
    return _run(cwd, "git", *arguments).stdout
