from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any

from foundry_opt.orchestration.workspace_git_store import (
    GitWorkspaceStore,
    WorkspaceConflictError,
    WorkspaceCorruptionError,
    WorkspaceStoreError,
    _canonical_json,
    _read_document,
    _repository_root,
    _run,
)
from foundry_opt.orchestration.workspace_manifest import (
    parse_workspace_candidate_manifest,
)
from foundry_opt.orchestration.workspace_store import WorkspaceExperimentRecord
from foundry_opt.orchestration.git_transport import resolve_safe_push_remote


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MANIFEST_PATH = re.compile(
    r"^candidates/([A-Za-z0-9][A-Za-z0-9._-]{0,127})\.json$"
)


class GitWorkspaceOperationStore:
    """Trusted durable store for candidate operation manifests on Git refs."""

    def __init__(
        self,
        repository_root: Path,
        *,
        remote: str = "origin",
    ) -> None:
        self._root = _repository_root(repository_root)
        self._git = GitWorkspaceStore(self._root, remote=remote)
        self._remote = remote

    @staticmethod
    def ref_name(issue_number: int) -> str:
        _issue_number(issue_number)
        return f"refs/heads/foundry-opt/operations/issue-{issue_number}"

    def record_candidate_manifest(
        self,
        issue_number: int,
        payload: Mapping[str, Any],
    ) -> None:
        manifest = parse_workspace_candidate_manifest(payload)
        if manifest.issue_number != issue_number:
            raise ValueError(
                "workspace candidate manifest issue does not match state"
            )
        ref = self.ref_name(issue_number)
        candidate_id = manifest.candidate.candidate_id
        path = _manifest_path(candidate_id)
        content = _canonical_json(dict(payload))
        current = self._git._remote_revision(ref)
        files = self._candidate_files(issue_number)
        existing = files.get(path)
        if existing is not None:
            if existing != content:
                raise ValueError(
                    "workspace candidate manifest changed"
                )
            return
        files[path] = content
        revision = self._git._write_commit(
            parent=current,
            files=files,
            message=(
                "Record workspace candidate manifest for "
                f"issue-{issue_number}:{candidate_id}"
            ),
        )
        try:
            self._git._push(
                ref=ref,
                source_revision=revision,
                expected_revision=current,
            )
        except WorkspaceConflictError:
            files = self._candidate_files(issue_number)
            if files.get(path) != content:
                raise

    def load_candidate_manifest(
        self,
        issue_number: int,
        candidate_id: str,
    ) -> Mapping[str, Any] | None:
        _issue_number(issue_number)
        _candidate_id(candidate_id)
        return self.load_candidate_manifests(issue_number).get(candidate_id)

    def load_candidate_manifests(
        self,
        issue_number: int,
    ) -> Mapping[str, Mapping[str, Any]]:
        files = self._candidate_files(issue_number)
        manifests: dict[str, Mapping[str, Any]] = {}
        for path, content in files.items():
            match = _MANIFEST_PATH.fullmatch(path)
            if match is None:
                raise WorkspaceCorruptionError(
                    "workspace operations contain unexpected paths"
                )
            candidate_id = match.group(1)
            try:
                payload = _read_document(
                    content,
                    "workspace candidate manifest",
                )
            except WorkspaceCorruptionError:
                raise
            manifest = parse_workspace_candidate_manifest(payload)
            if (
                manifest.issue_number != issue_number
                or manifest.candidate.candidate_id != candidate_id
            ):
                raise WorkspaceCorruptionError(
                    "workspace candidate manifest is invalid"
                )
            manifests[candidate_id] = MappingProxyType(dict(payload))
        return MappingProxyType(manifests)

    def delete_ref(self, issue_number: int) -> bool:
        ref = self.ref_name(issue_number)
        current = self._git._remote_revision(ref)
        if current is None:
            return False
        safe_remote = resolve_safe_push_remote(self._root, self._remote)
        if safe_remote is None:
            raise WorkspaceConflictError(
                "workspace operation push destination is not trusted"
            )
        _run(self._root, "git", "push", safe_remote.url, f":{ref}")
        return True

    def _candidate_files(self, issue_number: int) -> dict[str, bytes]:
        ref = self.ref_name(issue_number)
        revision = self._git._remote_revision(ref)
        if revision is None:
            return {}
        self._git._fetch(ref, revision)
        paths = self._git._paths(revision)
        if any(_MANIFEST_PATH.fullmatch(path) is None for path in paths):
            raise WorkspaceCorruptionError(
                "workspace operations contain unexpected paths"
            )
        return {
            path: self._git._show(revision, path)
            for path in paths
        }


class WorkspaceExperimentRecordView:
    """Read completed/pending experiment records from compact v5 state."""

    def __init__(self, repository_root: Path) -> None:
        self._git = GitWorkspaceStore(repository_root)

    def load_experiment(
        self,
        issue_number: int,
        candidate_id: str,
    ) -> WorkspaceExperimentRecord | None:
        snapshot = self._git.load(issue_number)
        if snapshot is None:
            return None
        return next(
            (
                record
                for record in snapshot.experiments
                if record.candidate_id == candidate_id
            ),
            None,
        )


def _manifest_path(candidate_id: str) -> str:
    _candidate_id(candidate_id)
    return f"candidates/{candidate_id}.json"


def _issue_number(value: int) -> None:
    if type(value) is not int or value < 1:
        raise ValueError("workspace issue number is invalid")


def _candidate_id(value: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("workspace candidate is invalid")


__all__ = [
    "GitWorkspaceOperationStore",
    "WorkspaceExperimentRecordView",
]
