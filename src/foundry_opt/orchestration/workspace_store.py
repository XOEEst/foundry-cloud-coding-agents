from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from types import MappingProxyType
from typing import Mapping

from foundry_opt.orchestration.workspace import WorkspacePhase


@dataclass(frozen=True)
class CandidateSummary:
    candidate_id: str
    metrics: Mapping[str, float]
    eligible: bool
    selected: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "metrics",
            MappingProxyType(dict(self.metrics)),
        )


@dataclass(frozen=True)
class WorkspaceLineage:
    spec_sha256: str
    base_commit: str
    patch_sha256: str
    evidence_sha256: str
    bundle_sha256: str
    expected_tree: str
    selected_candidate_id: str
    workspace_pull_request_number: int
    required_checks: Mapping[str, str]
    required_checks_provenance: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.spec_sha256, "spec"),
            (self.patch_sha256, "patch"),
            (self.evidence_sha256, "evidence"),
            (self.bundle_sha256, "bundle"),
        ):
            if re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError(
                    f"workspace lineage {name} digest is invalid"
                )
        for value, name in (
            (self.base_commit, "base commit"),
            (self.expected_tree, "expected tree"),
        ):
            if re.fullmatch(r"[0-9a-f]{40}", value) is None:
                raise ValueError(f"workspace lineage {name} is invalid")
        if re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
            self.selected_candidate_id,
        ) is None:
            raise ValueError(
                "workspace lineage selected candidate is invalid"
            )
        if (
            type(self.workspace_pull_request_number) is not int
            or self.workspace_pull_request_number < 1
        ):
            raise ValueError(
                "workspace lineage pull request is invalid"
            )
        checks = dict(self.required_checks)
        if (
            not checks
            or any(
                not isinstance(name, str)
                or not name
                or len(name) > 256
                or any(ord(character) < 32 for character in name)
                or status != "success"
                for name, status in checks.items()
            )
        ):
            raise ValueError(
                "workspace lineage required checks are invalid"
            )
        if re.fullmatch(
            r"trusted-selector:head:[0-9a-f]{40}",
            self.required_checks_provenance,
        ) is None:
            raise ValueError(
                "workspace lineage check provenance is invalid"
            )
        object.__setattr__(
            self,
            "required_checks",
            MappingProxyType(checks),
        )


@dataclass(frozen=True)
class WorkspaceUpdate:
    issue_number: int
    phase: WorkspacePhase
    workspace_pull_request_number: int | None
    semantic_event: str
    candidates: tuple[CandidateSummary, ...] = ()
    selected_patch: bytes | None = None
    external_operation_ids: tuple[str, ...] = ()
    lineage: WorkspaceLineage | None = None


@dataclass(frozen=True)
class WorkspaceSnapshot:
    issue_number: int
    revision: str
    phase: WorkspacePhase
    workspace_pull_request_number: int | None
    candidates: tuple[CandidateSummary, ...]
    selected_patch: bytes | None
    external_operation_ids: tuple[str, ...]
    lineage: WorkspaceLineage | None


@dataclass(frozen=True)
class AuditBundle:
    issue_number: int
    final_snapshot: WorkspaceSnapshot
    journal: tuple[str, ...]
    candidates: tuple[CandidateSummary, ...]
    selected_patch: bytes | None
    external_operation_ids: tuple[str, ...]
    lineage: WorkspaceLineage | None
    retained_paths: tuple[str, ...]


class InMemoryWorkspaceStore:
    def __init__(self) -> None:
        self._snapshots: dict[int, WorkspaceSnapshot] = {}
        self._journals: dict[int, tuple[str, ...]] = {}

    def load(self, issue_number: int) -> WorkspaceSnapshot | None:
        return self._snapshots.get(issue_number)

    def commit(
        self,
        *,
        expected_revision: str | None,
        update: WorkspaceUpdate,
    ) -> WorkspaceSnapshot:
        current = self.load(update.issue_number)
        current_revision = current.revision if current is not None else None
        if current_revision != expected_revision:
            raise ValueError("workspace revision changed")
        if (
            current is not None
            and current.lineage is not None
            and update.lineage != current.lineage
        ):
            raise ValueError("workspace lineage changed")
        _validate_lineage_update(update)
        revision = str(int(current_revision or "0") + 1)
        snapshot = WorkspaceSnapshot(
            issue_number=update.issue_number,
            revision=revision,
            phase=update.phase,
            workspace_pull_request_number=(
                update.workspace_pull_request_number
            ),
            candidates=update.candidates,
            selected_patch=update.selected_patch,
            external_operation_ids=update.external_operation_ids,
            lineage=update.lineage,
        )
        self._snapshots[update.issue_number] = snapshot
        self._journals[update.issue_number] = (
            *self._journals.get(update.issue_number, ()),
            update.semantic_event,
        )
        return snapshot

    def finalize(self, issue_number: int) -> AuditBundle:
        snapshot = self._snapshots.pop(issue_number)
        journal = self._journals.pop(issue_number)
        retained_paths = ["snapshot.json", "journal.jsonl"]
        if snapshot.candidates:
            retained_paths.append("evidence/candidates.json")
        if snapshot.selected_patch is not None:
            retained_paths.append("patches/selected.patch")
        return AuditBundle(
            issue_number=issue_number,
            final_snapshot=snapshot,
            journal=journal,
            candidates=snapshot.candidates,
            selected_patch=snapshot.selected_patch,
            external_operation_ids=snapshot.external_operation_ids,
            lineage=snapshot.lineage,
            retained_paths=tuple(retained_paths),
        )


def _validate_lineage_update(update: WorkspaceUpdate) -> None:
    lineage = update.lineage
    if lineage is None:
        return
    selected = tuple(
        item for item in update.candidates if item.selected
    )
    if (
        update.selected_patch is None
        or hashlib.sha256(update.selected_patch).hexdigest()
        != lineage.patch_sha256
        or update.workspace_pull_request_number
        != lineage.workspace_pull_request_number
        or len(selected) != 1
        or selected[0].candidate_id
        != lineage.selected_candidate_id
    ):
        raise ValueError("workspace lineage does not match state")
