from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from foundry_opt.campaign.models import CampaignLimits, PatchArtifact
from foundry_opt.drafts import DraftRecord
from foundry_opt.evaluation import (
    DatasetSplit,
    EvaluationPolicy,
    EvaluationResult,
    EvaluationSubject,
)
from foundry_opt.evidence import (
    EvaluationAssetReference,
    EvidenceManifest,
    EvidenceRequest,
)
from foundry_opt.packaging import (
    BundleArtifact,
    ValidationReport,
)
from foundry_opt.security import reject_secret_content


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _identifier(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field_name} is invalid")


def _git_branch(value: str) -> None:
    invalid = " ~^:?*[\\"
    if (
        not isinstance(value, str)
        or not value
        or value.startswith((".", "/"))
        or value.endswith(("/", "."))
        or ".." in value
        or "//" in value
        or "@{" in value
        or any(character in value for character in invalid)
        or any(
            not part or part.endswith(".lock")
            for part in value.split("/")
        )
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("default_branch is invalid")


def _relative_path(value: Path, field_name: str) -> Path:
    raw = str(value)
    windows = PureWindowsPath(raw)
    posix = PurePosixPath(raw.replace("\\", "/"))
    if (
        not raw
        or windows.drive
        or raw.startswith(("/", "\\"))
        or ".." in posix.parts
    ):
        raise ValueError(f"{field_name} must be repository-relative")
    return Path(posix.as_posix())


@dataclass(frozen=True)
class CampaignRequest:
    campaign_id: str
    target: str
    repository_root: Path
    limits: CampaignLimits
    edit_paths: tuple[Path, ...]
    allowed_mutations: frozenset[str]
    evaluation_policy: EvaluationPolicy
    goal: str
    spec_sha256: str
    assets: tuple[EvaluationAssetReference, ...]
    restricted_opt_ins: Mapping[str, bool] = field(default_factory=dict)
    evidence_root: Path = Path(".foundry-optimizer/campaigns")
    stale_after: timedelta = timedelta(hours=2)

    def __post_init__(self) -> None:
        _identifier(self.campaign_id, "campaign_id")
        _identifier(self.target, "target")
        if not self.edit_paths:
            raise ValueError("edit_paths must not be empty")
        if not self.allowed_mutations:
            raise ValueError("allowed_mutations must not be empty")
        if self.stale_after < timedelta(hours=2):
            raise ValueError("stale_after must be at least two hours")
        if not isinstance(self.goal, str) or not 20 <= len(self.goal) <= 4000:
            raise ValueError("goal must be between 20 and 4000 characters")
        reject_secret_content(self.goal)
        if not _SHA256.fullmatch(self.spec_sha256):
            raise ValueError("spec_sha256 is invalid")
        if not self.assets:
            raise ValueError("assets must not be empty")
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
            "edit_paths",
            tuple(_relative_path(path, "edit_paths") for path in self.edit_paths),
        )
        object.__setattr__(
            self,
            "evidence_root",
            _relative_path(self.evidence_root, "evidence_root"),
        )
        object.__setattr__(
            self,
            "restricted_opt_ins",
            MappingProxyType(dict(self.restricted_opt_ins)),
        )


@dataclass(frozen=True)
class PinnedRepository:
    default_branch: str
    commit: str

    def __post_init__(self) -> None:
        _git_branch(self.default_branch)
        if not _COMMIT.fullmatch(self.commit):
            raise ValueError("commit is invalid")


@dataclass(frozen=True)
class CampaignLock:
    campaign_id: str
    recovered_campaign_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.campaign_id, "campaign_id")
        if self.recovered_campaign_id is not None:
            _identifier(self.recovered_campaign_id, "recovered_campaign_id")


@dataclass(frozen=True)
class CampaignWorktree:
    candidate_id: str
    path: Path
    branch: str
    base_commit: str

    def __post_init__(self) -> None:
        _identifier(self.candidate_id, "candidate_id")
        if not self.path.is_absolute():
            raise ValueError("worktree path must be absolute")
        if not self.branch.startswith("foundry-opt/"):
            raise ValueError("worktree branch must be optimizer-owned")
        if not _COMMIT.fullmatch(self.base_commit):
            raise ValueError("base_commit is invalid")


@dataclass(frozen=True)
class CandidateFeedback:
    candidate_id: str
    idea_id: str
    status: str
    metrics: Mapping[str, float] = field(default_factory=dict)
    eligible: bool = False

    def __post_init__(self) -> None:
        _identifier(self.candidate_id, "candidate_id")
        _identifier(self.idea_id, "idea_id")
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))


@dataclass(frozen=True)
class CandidateContext:
    campaign_id: str
    target: str
    candidate_id: str
    slot: int
    worktree: Path
    base_commit: str
    edit_paths: tuple[Path, ...]
    allowed_mutations: frozenset[str]
    restricted_opt_ins: Mapping[str, bool]
    baseline_metrics: Mapping[str, float]
    history: tuple[CandidateFeedback, ...]
    goal: str
    spec_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.goal, str) or not 20 <= len(self.goal) <= 4000:
            raise ValueError("goal must be between 20 and 4000 characters")
        reject_secret_content(self.goal)
        if not _SHA256.fullmatch(self.spec_sha256):
            raise ValueError("spec_sha256 is invalid")


@dataclass(frozen=True)
class CandidateIdea:
    idea_id: str
    mutation_class: str
    parent_idea_ids: tuple[str, ...] = ()
    required_opt_ins: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        _identifier(self.idea_id, "idea_id")
        _identifier(self.mutation_class, "mutation_class")
        for value in self.parent_idea_ids:
            _identifier(value, "parent_idea_id")
        for value in self.required_opt_ins:
            _identifier(value, "required_opt_in")


class TransientCandidateError(RuntimeError):
    """A clearly transient coding-agent failure that may be retried once."""


class ActiveCampaignError(RuntimeError):
    """Another non-stale campaign owns the target lock."""


class UnsafeMutationError(RuntimeError):
    """A candidate changed a path or mutation class outside its allow-list."""


class CampaignStateError(RuntimeError):
    """A persisted campaign requires explicit inspection instead of restart."""

    def __init__(self, state: Any) -> None:
        self.state = state
        super().__init__(
            f"campaign {state.campaign_id} is {state.status}: "
            f"{state.error_code or 'unknown'}"
        )


class CandidateWorktreeFailureDetail(StrEnum):
    ARTIFACT_MISSING = "candidate_design_artifact_missing"
    ARTIFACT_TAMPERED = "candidate_design_artifact_tampered"
    ARTIFACT_STALE = "candidate_design_artifact_stale"
    FORBIDDEN_PATHS = "candidate_design_artifact_forbidden"
    WORKTREE_MISMATCH = "candidate_design_worktree_mismatch"


class CandidateWorktreeRehydrationError(RuntimeError):
    def __init__(self, detail: CandidateWorktreeFailureDetail) -> None:
        self.detail = detail
        super().__init__(detail.value)


class Clock(Protocol):
    def now(self) -> datetime: ...


class CampaignRepository(Protocol):
    def pin_default_branch(self, repository_root: Path) -> PinnedRepository: ...

    def acquire_lock(
        self,
        *,
        repository_root: Path,
        target: str,
        campaign_id: str,
        base_commit: str,
        now: datetime,
        stale_after: timedelta,
    ) -> CampaignLock: ...

    def release_lock(
        self,
        *,
        repository_root: Path,
        target: str,
        campaign_id: str,
    ) -> None: ...

    def create_worktree(
        self,
        repository_root: Path,
        campaign_id: str,
        candidate_id: str,
        base_commit: str,
    ) -> CampaignWorktree: ...

    def open_worktree(
        self,
        repository_root: Path,
        campaign_id: str,
        candidate_id: str,
        base_commit: str,
    ) -> CampaignWorktree: ...

    def reconcile_worktree(
        self,
        repository_root: Path,
        campaign_id: str,
        candidate_id: str,
        base_commit: str,
    ) -> CampaignWorktree: ...

    def rehydrate_worktree(
        self,
        repository_root: Path,
        campaign_id: str,
        candidate_id: str,
        base_commit: str,
        *,
        source_ref: str,
        result_commit: str,
        result_tree: str,
        changed_paths: tuple[Path, ...],
        allowed_paths: tuple[Path, ...],
    ) -> CampaignWorktree: ...

    def changed_paths(
        self,
        worktree: CampaignWorktree,
    ) -> tuple[Path, ...]: ...

    def reset_worktree(self, worktree: CampaignWorktree) -> None: ...

    def commit_worktree(
        self,
        worktree: CampaignWorktree,
        message: str,
    ) -> str: ...

    def export_patch(
        self,
        repository_root: Path,
        campaign_id: str,
        worktree: CampaignWorktree,
        result_commit: str,
    ) -> PatchArtifact: ...

    def cleanup_worktree(
        self,
        repository_root: Path,
        worktree: CampaignWorktree,
    ) -> None: ...


class CandidateGenerator(Protocol):
    def generate(self, context: CandidateContext) -> CandidateIdea: ...


ValidationRunner = Callable[[Path], ValidationReport]
BundleBuilder = Callable[[Path, Path], BundleArtifact]
DraftCreator = Callable[[str, str, str, BundleArtifact], DraftRecord]
EvaluationRunner = Callable[
    [EvaluationSubject, DatasetSplit, int],
    EvaluationResult,
]
EvidenceWriter = Callable[[EvidenceRequest], EvidenceManifest]
