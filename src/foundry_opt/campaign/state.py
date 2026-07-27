from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from math import isfinite
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping, Protocol

from foundry_opt.campaign.lineage import IdeaLineage
from foundry_opt.campaign.models import CandidateArtifact, PatchArtifact
from foundry_opt.drafts import DraftRecord


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class DraftMetadata:
    agent_name: str
    version_id: str
    base_version: int
    sha256: str
    status: str | None
    probe: bool
    project_endpoint: str

    @classmethod
    def from_record(cls, record: DraftRecord) -> "DraftMetadata":
        return cls(
            agent_name=record.agent_name,
            version_id=record.version_id,
            base_version=record.base_version,
            sha256=record.sha256,
            status=record.status,
            probe=record.probe,
            project_endpoint=record.project_endpoint,
        )


@dataclass(frozen=True)
class CandidateState:
    candidate_id: str
    slot: int
    status: str
    attempts: int = 0
    lineage: IdeaLineage | None = None
    artifact: CandidateArtifact | None = None
    metrics: Mapping[str, float] = field(default_factory=dict)
    error_code: str | None = None
    timings: Mapping[str, float] = field(default_factory=dict)
    draft: DraftMetadata | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))
        timings = dict(self.timings)
        if any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not isfinite(value)
            or value < 0
            for value in timings.values()
        ):
            raise ValueError("candidate timings must be finite and non-negative")
        object.__setattr__(self, "timings", MappingProxyType(timings))


@dataclass(frozen=True)
class CampaignState:
    campaign_id: str
    target: str
    base_commit: str
    status: str
    started_at: datetime
    updated_at: datetime
    baseline_draft_id: str | None = None
    baseline_metrics: Mapping[str, float] = field(default_factory=dict)
    candidates: tuple[CandidateState, ...] = ()
    launched_slots: int = 0
    transient_retries_used: int = 0
    pareto_candidate_ids: tuple[str, ...] = ()
    error_code: str | None = None
    baseline_draft: DraftMetadata | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "baseline_metrics",
            MappingProxyType(dict(self.baseline_metrics)),
        )


class CampaignStateStore(Protocol):
    def load(
        self,
        repository_root: Path,
        campaign_id: str,
    ) -> CampaignState | None: ...

    def save(
        self,
        repository_root: Path,
        state: CampaignState,
    ) -> None: ...

    def mark_stale(
        self,
        repository_root: Path,
        campaign_id: str,
        now: datetime,
    ) -> None: ...


class MemoryCampaignStateStore:
    def __init__(self) -> None:
        self._states: dict[tuple[Path, str], CampaignState] = {}

    def load(
        self,
        repository_root: Path,
        campaign_id: str,
    ) -> CampaignState | None:
        return self._states.get((repository_root.resolve(), campaign_id))

    def save(
        self,
        repository_root: Path,
        state: CampaignState,
    ) -> None:
        self._states[(repository_root.resolve(), state.campaign_id)] = state

    def mark_stale(
        self,
        repository_root: Path,
        campaign_id: str,
        now: datetime,
    ) -> None:
        key = (repository_root.resolve(), campaign_id)
        state = self._states.get(key)
        if state is not None and state.status == "active":
            from dataclasses import replace

            self._states[key] = replace(
                state,
                status="stale",
                updated_at=now,
                error_code="stale_lock_recovered",
            )


class FileCampaignStateStore:
    def load(
        self,
        repository_root: Path,
        campaign_id: str,
    ) -> CampaignState | None:
        path = _state_path(repository_root, campaign_id)
        try:
            if path.is_symlink():
                raise ValueError("campaign state cannot be a symlink")
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        return _state_from_document(document)

    def save(
        self,
        repository_root: Path,
        state: CampaignState,
    ) -> None:
        path = _state_path(repository_root, state.campaign_id)
        _ensure_safe_directory(path.parent, repository_root)
        content = (
            json.dumps(
                _state_document(state),
                indent=2,
                sort_keys=True,
                separators=(",", ": "),
            )
            + "\n"
        ).encode("utf-8")
        temporary = path.with_name(f".state-{os.getpid()}.json")
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def mark_stale(
        self,
        repository_root: Path,
        campaign_id: str,
        now: datetime,
    ) -> None:
        state = self.load(repository_root, campaign_id)
        if state is not None and state.status == "active":
            from dataclasses import replace

            self.save(
                repository_root,
                replace(
                    state,
                    status="stale",
                    updated_at=now,
                    error_code="stale_lock_recovered",
                ),
            )


def _state_path(repository_root: Path, campaign_id: str) -> Path:
    if not _IDENTIFIER.fullmatch(campaign_id):
        raise ValueError("campaign_id is invalid")
    root = repository_root.expanduser().resolve()
    path = (
        root
        / ".foundry-optimizer"
        / "campaigns"
        / campaign_id
        / "state.json"
    )
    if not path.resolve().is_relative_to(root):
        raise ValueError("campaign state path escapes repository")
    return path


def _ensure_safe_directory(path: Path, repository_root: Path) -> None:
    root = repository_root.expanduser().resolve()
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if os.path.lexists(current) and current.is_symlink():
            raise ValueError("campaign state directory cannot contain symlinks")
        current.mkdir(exist_ok=True)


def _state_document(state: CampaignState) -> dict[str, object]:
    return {
        "baseline_draft_id": state.baseline_draft_id,
        "baseline_draft": _draft_document(state.baseline_draft),
        "baseline_metrics": dict(state.baseline_metrics),
        "base_commit": state.base_commit,
        "campaign_id": state.campaign_id,
        "candidates": [
            _candidate_document(candidate) for candidate in state.candidates
        ],
        "error_code": state.error_code,
        "launched_slots": state.launched_slots,
        "pareto_candidate_ids": list(state.pareto_candidate_ids),
        "started_at": state.started_at.isoformat(),
        "status": state.status,
        "target": state.target,
        "transient_retries_used": state.transient_retries_used,
        "updated_at": state.updated_at.isoformat(),
    }


def _candidate_document(candidate: CandidateState) -> dict[str, object]:
    lineage = candidate.lineage
    artifact = candidate.artifact
    return {
        "artifact": (
            {
                "candidate_id": artifact.candidate_id,
                "draft_id": artifact.draft_id,
                "eligible": artifact.eligible,
                "evidence_path": artifact.evidence_path.as_posix(),
                "metrics": dict(artifact.metrics),
                "patch": {
                    "base_commit": artifact.patch.base_commit,
                    "candidate_id": artifact.patch.candidate_id,
                    "path": artifact.patch.path.as_posix(),
                    "result_commit": artifact.patch.result_commit,
                    "sha256": artifact.patch.sha256,
                },
            }
            if artifact is not None
            else None
        ),
        "attempts": candidate.attempts,
        "candidate_id": candidate.candidate_id,
        "error_code": candidate.error_code,
        "draft": _draft_document(candidate.draft),
        "lineage": (
            {
                "changed_paths": [
                    path.as_posix() for path in lineage.changed_paths
                ],
                "idea_id": lineage.idea_id,
                "mutation_class": lineage.mutation_class,
                "parent_idea_ids": list(lineage.parent_idea_ids),
            }
            if lineage is not None
            else None
        ),
        "metrics": dict(candidate.metrics),
        "slot": candidate.slot,
        "status": candidate.status,
        "timings": dict(candidate.timings),
    }


def _state_from_document(document: object) -> CampaignState:
    if not isinstance(document, dict):
        raise ValueError("campaign state document is invalid")
    candidates = tuple(
        _candidate_from_document(candidate)
        for candidate in document.get("candidates", ())
    )
    return CampaignState(
        campaign_id=str(document["campaign_id"]),
        target=str(document["target"]),
        base_commit=str(document["base_commit"]),
        status=str(document["status"]),
        started_at=datetime.fromisoformat(str(document["started_at"])),
        updated_at=datetime.fromisoformat(str(document["updated_at"])),
        baseline_draft_id=document.get("baseline_draft_id"),
        baseline_metrics=dict(document.get("baseline_metrics", {})),
        candidates=candidates,
        launched_slots=int(document.get("launched_slots", 0)),
        transient_retries_used=int(
            document.get("transient_retries_used", 0)
        ),
        pareto_candidate_ids=tuple(
            document.get("pareto_candidate_ids", ())
        ),
        error_code=document.get("error_code"),
        baseline_draft=_draft_from_document(document.get("baseline_draft")),
    )


def _candidate_from_document(document: object) -> CandidateState:
    if not isinstance(document, dict):
        raise ValueError("candidate state document is invalid")
    lineage_document = document.get("lineage")
    lineage = (
        IdeaLineage(
            idea_id=str(lineage_document["idea_id"]),
            parent_idea_ids=tuple(lineage_document["parent_idea_ids"]),
            mutation_class=str(lineage_document["mutation_class"]),
            changed_paths=tuple(
                Path(path) for path in lineage_document["changed_paths"]
            ),
        )
        if isinstance(lineage_document, dict)
        else None
    )
    artifact_document = document.get("artifact")
    artifact = None
    if isinstance(artifact_document, dict):
        patch_document = artifact_document["patch"]
        patch = PatchArtifact(
            candidate_id=str(patch_document["candidate_id"]),
            path=Path(patch_document["path"]),
            sha256=str(patch_document["sha256"]),
            base_commit=str(patch_document["base_commit"]),
            result_commit=str(patch_document["result_commit"]),
        )
        artifact = CandidateArtifact(
            candidate_id=str(artifact_document["candidate_id"]),
            patch=patch,
            draft_id=str(artifact_document["draft_id"]),
            evidence_path=Path(artifact_document["evidence_path"]),
            eligible=bool(artifact_document["eligible"]),
            metrics=dict(artifact_document["metrics"]),
        )
    draft = _draft_from_document(document.get("draft"))
    return CandidateState(
        candidate_id=str(document["candidate_id"]),
        slot=int(document["slot"]),
        status=str(document["status"]),
        attempts=int(document.get("attempts", 0)),
        lineage=lineage,
        artifact=artifact,
        metrics=dict(document.get("metrics", {})),
        error_code=document.get("error_code"),
        timings=dict(document.get("timings", {})),
        draft=draft,
    )


def _draft_document(
    draft: DraftMetadata | None,
) -> dict[str, object] | None:
    if draft is None:
        return None
    return {
        "agent_name": draft.agent_name,
        "base_version": draft.base_version,
        "probe": draft.probe,
        "project_endpoint": draft.project_endpoint,
        "sha256": draft.sha256,
        "status": draft.status,
        "version_id": draft.version_id,
    }


def _draft_from_document(document: object) -> DraftMetadata | None:
    if document is None:
        return None
    if not isinstance(document, dict):
        raise ValueError("draft metadata document is invalid")
    return DraftMetadata(
        agent_name=str(document["agent_name"]),
        version_id=str(document["version_id"]),
        base_version=int(document["base_version"]),
        sha256=str(document["sha256"]),
        status=(
            str(document["status"])
            if document.get("status") is not None
            else None
        ),
        probe=bool(document["probe"]),
        project_endpoint=str(document["project_endpoint"]),
    )
