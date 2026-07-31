from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
import re
from types import MappingProxyType
from typing import Any, Mapping


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_PATH = re.compile(
    r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._/@+-]+(?:/[A-Za-z0-9._@+-]+)*$"
)


class CampaignPhase(StrEnum):
    SPECIFICATION = "specification"
    AWAITING_SPEC_APPROVAL = "awaiting_spec_approval"
    BASELINE = "baseline"
    CANDIDATES = "candidates"
    AWAITING_SELECTION = "awaiting_selection"
    DEPLOYMENT = "deployment"
    RETENTION = "retention"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class EventKind(StrEnum):
    ISSUE_CREATED = "issue_created"
    ISSUE_EDITED = "issue_edited"
    ISSUE_DECLASSIFIED = "issue_declassified"
    ISSUE_REOPENED = "issue_reopened"
    ISSUE_CLOSED = "issue_closed"
    SPEC_POLICY_APPROVED = "spec_policy_approved"
    SPEC_REVIEW_REQUIRED = "spec_review_required"
    SPEC_HUMAN_APPROVED = "spec_human_approved"
    BASELINE_COMPLETED = "baseline_completed"
    CANDIDATE_EVALUATED = "candidate_evaluated"
    SLATE_PUBLISHED = "slate_published"
    CANDIDATE_MERGED = "candidate_merged"
    DEPLOYMENT_COMPLETED = "deployment_completed"
    RETENTION_COMPLETED = "retention_completed"


class AdvanceDisposition(StrEnum):
    ADVANCE = "advance"
    DELEGATE = "delegate"
    WAIT = "wait"
    COMPLETE = "complete"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class CandidateRecord:
    candidate_id: str
    eligible: bool
    evidence_sha256: str

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.candidate_id):
            raise ValueError("candidate_id must be an identifier")
        if not _SHA256.fullmatch(self.evidence_sha256):
            raise ValueError("evidence_sha256 must be a SHA-256 digest")
        if type(self.eligible) is not bool:
            raise ValueError("eligible must be boolean")


@dataclass(frozen=True)
class SpecFileHash:
    path: str
    sha256: str

    def __post_init__(self) -> None:
        if not _REPOSITORY_PATH.fullmatch(self.path):
            raise ValueError("spec file path must be repository-relative")
        if not _SHA256.fullmatch(self.sha256):
            raise ValueError("spec file hash must be a SHA-256 digest")


@dataclass(frozen=True)
class CampaignEvent:
    event_id: str
    kind: EventKind
    generation: int
    occurred_at: datetime
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.event_id):
            raise ValueError("event_id must be an identifier")
        if self.generation < 1:
            raise ValueError("generation must be positive")
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        object.__setattr__(
            self,
            "payload",
            MappingProxyType(dict(self.payload)),
        )


@dataclass(frozen=True)
class CampaignState:
    issue_number: int
    generation: int
    sequence: int
    phase: CampaignPhase
    schema_version: int = 2
    processed_event_ids: tuple[str, ...] = ()
    spec_sha256: str | None = None
    spec_base_ref_name: str | None = None
    spec_head_commit: str | None = None
    spec_tree_sha: str | None = None
    spec_files: tuple[SpecFileHash | Mapping[str, Any], ...] = ()
    baseline_evaluation_id: str | None = None
    candidates: tuple[CandidateRecord | Mapping[str, Any], ...] = ()
    selected_candidate_id: str | None = None
    merge_commit: str | None = None
    deployment_version: int | None = None
    block_reason: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version not in {1, 2}:
            raise ValueError("unsupported campaign state schema_version")
        if self.issue_number < 1:
            raise ValueError("issue_number must be positive")
        if self.generation < 1:
            raise ValueError("generation must be positive")
        if self.sequence < 0:
            raise ValueError("sequence must not be negative")
        if len(set(self.processed_event_ids)) != len(
            self.processed_event_ids
        ):
            raise ValueError("processed event IDs must be unique")
        normalized = tuple(
            candidate
            if isinstance(candidate, CandidateRecord)
            else CandidateRecord(**candidate)
            for candidate in self.candidates
        )
        if len({item.candidate_id for item in normalized}) != len(normalized):
            raise ValueError("candidate IDs must be unique")
        object.__setattr__(self, "candidates", normalized)
        if self.spec_sha256 is not None and not _SHA256.fullmatch(
            self.spec_sha256
        ):
            raise ValueError("spec_sha256 must be a SHA-256 digest")
        spec_files = tuple(
            item if isinstance(item, SpecFileHash) else SpecFileHash(**item)
            for item in self.spec_files
        )
        if len({item.path for item in spec_files}) != len(spec_files):
            raise ValueError("spec file paths must be unique")
        object.__setattr__(self, "spec_files", spec_files)
        if self.spec_base_ref_name is not None and not _REPOSITORY_PATH.fullmatch(
            self.spec_base_ref_name
        ):
            raise ValueError("spec_base_ref_name must be a safe ref name")
        for value in (self.spec_head_commit, self.spec_tree_sha):
            if value is not None and not _COMMIT.fullmatch(value):
                raise ValueError(
                    "spec materialization commits must be full Git objects"
                )
        materialization = (
            self.spec_base_ref_name,
            self.spec_head_commit,
            self.spec_tree_sha,
            self.spec_files,
        )
        if any(materialization) and not all(materialization):
            raise ValueError("spec materialization metadata must be complete")
        if self.merge_commit is not None and not _COMMIT.fullmatch(
            self.merge_commit
        ):
            raise ValueError("merge_commit must be a full Git commit")
        if (
            self.deployment_version is not None
            and (
                type(self.deployment_version) is not int
                or self.deployment_version < 1
            )
        ):
            raise ValueError("deployment_version must be positive")
        if self.block_reason is not None and not _IDENTIFIER.fullmatch(
            self.block_reason
        ):
            raise ValueError("block_reason must be an identifier")
        if self.phase in {
            CampaignPhase.AWAITING_SPEC_APPROVAL,
            CampaignPhase.BASELINE,
            CampaignPhase.CANDIDATES,
            CampaignPhase.AWAITING_SELECTION,
            CampaignPhase.DEPLOYMENT,
            CampaignPhase.RETENTION,
            CampaignPhase.COMPLETED,
        } and self.spec_sha256 is None:
            raise ValueError("phase requires an approved specification")
        if self.phase in {
            CampaignPhase.CANDIDATES,
            CampaignPhase.AWAITING_SELECTION,
            CampaignPhase.DEPLOYMENT,
            CampaignPhase.RETENTION,
            CampaignPhase.COMPLETED,
        } and self.baseline_evaluation_id is None:
            raise ValueError("phase requires a baseline evaluation")
        if (
            self.phase is CampaignPhase.AWAITING_SELECTION
            and not any(item.eligible for item in normalized)
        ):
            raise ValueError(
                "awaiting selection requires an eligible candidate"
            )
        if self.selected_candidate_id is not None:
            selected = next(
                (
                    item
                    for item in normalized
                    if item.candidate_id == self.selected_candidate_id
                ),
                None,
            )
            if selected is None or not selected.eligible:
                raise ValueError(
                    "selected candidate must exist and be eligible"
                )
        if self.phase in {
            CampaignPhase.DEPLOYMENT,
            CampaignPhase.RETENTION,
            CampaignPhase.COMPLETED,
        } and (
            self.selected_candidate_id is None
            or self.merge_commit is None
        ):
            raise ValueError("phase requires deployment lineage")
        if self.phase in {
            CampaignPhase.RETENTION,
            CampaignPhase.COMPLETED,
        } and self.deployment_version is None:
            raise ValueError("phase requires a deployment version")
        if self.phase is CampaignPhase.BLOCKED:
            if self.block_reason is None:
                raise ValueError("blocked phase requires block_reason")
        elif self.block_reason is not None:
            raise ValueError(
                "block_reason is only valid for blocked campaigns"
            )


@dataclass(frozen=True)
class AdvanceRequest:
    issue_number: int
    state: CampaignState | None
    events: tuple[CampaignEvent, ...]

    def __post_init__(self) -> None:
        if self.issue_number < 1:
            raise ValueError("issue_number must be positive")
        if self.state is not None and (
            self.state.issue_number != self.issue_number
        ):
            raise ValueError("state issue does not match request issue")
        if not self.events:
            raise ValueError("at least one event is required")


@dataclass(frozen=True)
class AdvanceResult:
    state: CampaignState
    disposition: AdvanceDisposition
