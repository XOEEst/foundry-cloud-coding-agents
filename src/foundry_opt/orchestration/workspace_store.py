from __future__ import annotations

from dataclasses import dataclass
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
class WorkspaceUpdate:
    issue_number: int
    phase: WorkspacePhase
    workspace_pull_request_number: int | None
    semantic_event: str
    candidates: tuple[CandidateSummary, ...] = ()
    selected_patch: bytes | None = None
    external_operation_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkspaceSnapshot:
    issue_number: int
    revision: str
    phase: WorkspacePhase
    workspace_pull_request_number: int | None
    candidates: tuple[CandidateSummary, ...]
    selected_patch: bytes | None
    external_operation_ids: tuple[str, ...]


@dataclass(frozen=True)
class AuditBundle:
    issue_number: int
    final_snapshot: WorkspaceSnapshot
    journal: tuple[str, ...]
    candidates: tuple[CandidateSummary, ...]
    selected_patch: bytes | None
    external_operation_ids: tuple[str, ...]
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
            retained_paths=tuple(retained_paths),
        )
