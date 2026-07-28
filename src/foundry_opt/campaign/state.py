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

from foundry_opt.campaign.evaluation_state import (
    evaluation_result_from_document,
    evaluation_result_to_document,
)
from foundry_opt.campaign.lineage import IdeaLineage
from foundry_opt.campaign.models import CandidateArtifact, PatchArtifact
from foundry_opt.drafts import DraftRecord
from foundry_opt.evaluation import EvaluationResult
from foundry_opt.evidence import EvaluationAssetReference


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class FinalizedPublication:
    """Immutable record of the published campaign PR and candidate issues."""

    campaign_pull_request_number: int
    campaign_pull_request_url: str
    candidate_issue_numbers: Mapping[str, int]

    def __post_init__(self) -> None:
        if self.campaign_pull_request_number < 1:
            raise ValueError("campaign_pull_request_number must be positive")
        if not self.campaign_pull_request_url:
            raise ValueError("campaign_pull_request_url is required")
        numbers: dict[str, int] = {}
        for candidate_id, number in self.candidate_issue_numbers.items():
            if not _IDENTIFIER.fullmatch(candidate_id):
                raise ValueError("finalized candidate_id is invalid")
            if not isinstance(number, int) or isinstance(number, bool) or (
                number < 1
            ):
                raise ValueError("candidate issue numbers must be positive")
            numbers[candidate_id] = number
        object.__setattr__(
            self,
            "candidate_issue_numbers",
            MappingProxyType(numbers),
        )


@dataclass(frozen=True)
class DraftCreationIntent:
    subject_id: str
    idempotency_key: str
    status: str = "pending"

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.subject_id):
            raise ValueError("draft intent subject_id is invalid")
        if not _SHA256.fullmatch(self.idempotency_key):
            raise ValueError("draft intent idempotency_key is invalid")
        if self.status not in {"pending", "reconciled"}:
            raise ValueError("draft intent status is invalid")


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
    draft_intent: DraftCreationIntent | None = None
    context_path: str | None = None
    context_sha256: str | None = None
    idea_path: str | None = None
    idea_sha256: str | None = None
    result_commit: str | None = None
    patch: PatchArtifact | None = None
    provisional_eligible: bool = False
    development_result: EvaluationResult | None = None
    validation_result: EvaluationResult | None = None

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
        for value, name in (
            (self.context_sha256, "context_sha256"),
            (self.idea_sha256, "idea_sha256"),
        ):
            if value is not None and not _SHA256.fullmatch(value):
                raise ValueError(f"candidate {name} is invalid")
        if self.result_commit is not None and not _COMMIT.fullmatch(
            self.result_commit
        ):
            raise ValueError("candidate result_commit is invalid")
        if self.patch is not None and self.patch.candidate_id != (
            self.candidate_id
        ):
            raise ValueError("candidate patch identity does not match")


@dataclass(frozen=True)
class CampaignState:
    campaign_id: str
    target: str
    base_commit: str
    status: str
    started_at: datetime
    updated_at: datetime
    goal_sha256: str
    spec_sha256: str
    assets: tuple[EvaluationAssetReference, ...]
    baseline_draft_id: str | None = None
    baseline_metrics: Mapping[str, float] = field(default_factory=dict)
    candidates: tuple[CandidateState, ...] = ()
    launched_slots: int = 0
    transient_retries_used: int = 0
    pareto_candidate_ids: tuple[str, ...] = ()
    error_code: str | None = None
    baseline_draft: DraftMetadata | None = None
    baseline_draft_intent: DraftCreationIntent | None = None
    baseline_development: EvaluationResult | None = None
    baseline_validation: EvaluationResult | None = None
    awaiting_candidate_id: str | None = None
    finalized: FinalizedPublication | None = None

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.goal_sha256):
            raise ValueError("goal_sha256 is invalid")
        if not _SHA256.fullmatch(self.spec_sha256):
            raise ValueError("spec_sha256 is invalid")
        if not isinstance(self.assets, (tuple, list)) or not all(
            isinstance(asset, EvaluationAssetReference) for asset in self.assets
        ):
            raise ValueError(
                "assets must be a tuple of EvaluationAssetReference"
            )
        asset_ids = tuple(asset.asset_id for asset in self.assets)
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("asset IDs must be unique")
        object.__setattr__(self, "assets", tuple(self.assets))
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
        "assets": [_asset_document(asset) for asset in state.assets],
        "awaiting_candidate_id": state.awaiting_candidate_id,
        "baseline_development": _evaluation_document(state.baseline_development),
        "baseline_draft_id": state.baseline_draft_id,
        "baseline_draft": _draft_document(state.baseline_draft),
        "baseline_draft_intent": _intent_document(
            state.baseline_draft_intent
        ),
        "baseline_metrics": dict(state.baseline_metrics),
        "baseline_validation": _evaluation_document(state.baseline_validation),
        "base_commit": state.base_commit,
        "campaign_id": state.campaign_id,
        "candidates": [
            _candidate_document(candidate) for candidate in state.candidates
        ],
        "error_code": state.error_code,
        "finalized": _finalized_document(state.finalized),
        "goal_sha256": state.goal_sha256,
        "launched_slots": state.launched_slots,
        "pareto_candidate_ids": list(state.pareto_candidate_ids),
        "spec_sha256": state.spec_sha256,
        "started_at": state.started_at.isoformat(),
        "status": state.status,
        "target": state.target,
        "transient_retries_used": state.transient_retries_used,
        "updated_at": state.updated_at.isoformat(),
    }


def _asset_document(asset: EvaluationAssetReference) -> dict[str, object]:
    return {
        "approval_gate": asset.approval_gate,
        "asset_id": asset.asset_id,
        "content_sha256": asset.content_sha256,
        "kind": asset.kind,
        "metrics": list(asset.metrics),
        "name": asset.name,
        "remote_id": asset.remote_id,
        "role": asset.role,
        "source": asset.source,
        "version": asset.version,
    }


def _asset_from_document(document: object) -> EvaluationAssetReference:
    if not isinstance(document, dict):
        raise ValueError("asset reference document is invalid")
    return EvaluationAssetReference(
        asset_id=str(document["asset_id"]),
        kind=str(document["kind"]),
        source=str(document["source"]),
        role=(
            str(document["role"]) if document.get("role") is not None else None
        ),
        name=(
            str(document["name"]) if document.get("name") is not None else None
        ),
        version=(
            str(document["version"])
            if document.get("version") is not None
            else None
        ),
        remote_id=(
            str(document["remote_id"])
            if document.get("remote_id") is not None
            else None
        ),
        content_sha256=(
            str(document["content_sha256"])
            if document.get("content_sha256") is not None
            else None
        ),
        approval_gate=str(document.get("approval_gate", "policy")),
        metrics=tuple(str(metric) for metric in document.get("metrics") or ()),
    )


def _patch_document(patch: PatchArtifact) -> dict[str, object]:
    return {
        "base_commit": patch.base_commit,
        "candidate_id": patch.candidate_id,
        "path": patch.path.as_posix(),
        "result_commit": patch.result_commit,
        "sha256": patch.sha256,
    }


def _patch_from_document(document: object) -> PatchArtifact:
    if not isinstance(document, dict):
        raise ValueError("patch artifact document is invalid")
    return PatchArtifact(
        candidate_id=str(document["candidate_id"]),
        path=Path(document["path"]),
        sha256=str(document["sha256"]),
        base_commit=str(document["base_commit"]),
        result_commit=str(document["result_commit"]),
    )


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
                "patch": _patch_document(artifact.patch),
            }
            if artifact is not None
            else None
        ),
        "attempts": candidate.attempts,
        "candidate_id": candidate.candidate_id,
        "error_code": candidate.error_code,
        "draft": _draft_document(candidate.draft),
        "draft_intent": _intent_document(candidate.draft_intent),
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
        "context_path": candidate.context_path,
        "context_sha256": candidate.context_sha256,
        "development_result": _evaluation_document(
            candidate.development_result
        ),
        "idea_path": candidate.idea_path,
        "idea_sha256": candidate.idea_sha256,
        "patch": (
            _patch_document(candidate.patch)
            if candidate.patch is not None
            else None
        ),
        "provisional_eligible": candidate.provisional_eligible,
        "result_commit": candidate.result_commit,
        "slot": candidate.slot,
        "status": candidate.status,
        "timings": dict(candidate.timings),
        "validation_result": _evaluation_document(
            candidate.validation_result
        ),
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
        goal_sha256=str(document["goal_sha256"]),
        spec_sha256=str(document["spec_sha256"]),
        assets=tuple(
            _asset_from_document(asset)
            for asset in document.get("assets", ())
        ),
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
        baseline_draft_intent=_intent_from_document(
            document.get("baseline_draft_intent")
        ),
        baseline_development=_evaluation_from_document(
            document.get("baseline_development")
        ),
        baseline_validation=_evaluation_from_document(
            document.get("baseline_validation")
        ),
        awaiting_candidate_id=document.get("awaiting_candidate_id"),
        finalized=_finalized_from_document(document.get("finalized")),
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
        artifact = CandidateArtifact(
            candidate_id=str(artifact_document["candidate_id"]),
            patch=_patch_from_document(artifact_document["patch"]),
            draft_id=str(artifact_document["draft_id"]),
            evidence_path=Path(artifact_document["evidence_path"]),
            eligible=bool(artifact_document["eligible"]),
            metrics=dict(artifact_document["metrics"]),
        )
    patch_document = document.get("patch")
    patch = (
        _patch_from_document(patch_document)
        if isinstance(patch_document, dict)
        else None
    )
    draft = _draft_from_document(document.get("draft"))
    draft_intent = _intent_from_document(document.get("draft_intent"))
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
        draft_intent=draft_intent,
        context_path=document.get("context_path"),
        context_sha256=document.get("context_sha256"),
        idea_path=document.get("idea_path"),
        idea_sha256=document.get("idea_sha256"),
        result_commit=document.get("result_commit"),
        patch=patch,
        provisional_eligible=bool(document.get("provisional_eligible", False)),
        development_result=_evaluation_from_document(
            document.get("development_result")
        ),
        validation_result=_evaluation_from_document(
            document.get("validation_result")
        ),
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


def _intent_document(
    intent: DraftCreationIntent | None,
) -> dict[str, str] | None:
    if intent is None:
        return None
    return {
        "idempotency_key": intent.idempotency_key,
        "status": intent.status,
        "subject_id": intent.subject_id,
    }


def _intent_from_document(
    document: object,
) -> DraftCreationIntent | None:
    if document is None:
        return None
    if not isinstance(document, dict):
        raise ValueError("draft intent document is invalid")
    return DraftCreationIntent(
        subject_id=str(document["subject_id"]),
        idempotency_key=str(document["idempotency_key"]),
        status=str(document["status"]),
    )


def _evaluation_document(
    result: EvaluationResult | None,
) -> dict[str, object] | None:
    if result is None:
        return None
    return evaluation_result_to_document(result)


def _evaluation_from_document(document: object) -> EvaluationResult | None:
    if document is None:
        return None
    return evaluation_result_from_document(document)


def _finalized_document(
    finalized: FinalizedPublication | None,
) -> dict[str, object] | None:
    if finalized is None:
        return None
    return {
        "campaign_pull_request_number": (
            finalized.campaign_pull_request_number
        ),
        "campaign_pull_request_url": finalized.campaign_pull_request_url,
        "candidate_issue_numbers": dict(finalized.candidate_issue_numbers),
    }


def _finalized_from_document(document: object) -> FinalizedPublication | None:
    if document is None:
        return None
    if not isinstance(document, dict):
        raise ValueError("finalized publication document is invalid")
    numbers = document.get("candidate_issue_numbers", {})
    if not isinstance(numbers, dict):
        raise ValueError("candidate_issue_numbers document is invalid")
    return FinalizedPublication(
        campaign_pull_request_number=int(
            document["campaign_pull_request_number"]
        ),
        campaign_pull_request_url=str(document["campaign_pull_request_url"]),
        candidate_issue_numbers={
            str(candidate_id): int(number)
            for candidate_id, number in numbers.items()
        },
    )
