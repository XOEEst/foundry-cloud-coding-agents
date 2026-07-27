from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from types import MappingProxyType
from typing import Mapping


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DRAFT = re.compile(r"^draft-[A-Za-z0-9][A-Za-z0-9._-]*$")


def _identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} is invalid")


def _repository_path(value: Path, field: str) -> Path:
    raw = str(value)
    windows = PureWindowsPath(raw)
    posix = PurePosixPath(raw.replace("\\", "/"))
    if (
        not raw
        or windows.drive
        or raw.startswith(("/", "\\"))
        or ".." in posix.parts
    ):
        raise ValueError(f"{field} must be repository-relative")
    return Path(posix.as_posix())


@dataclass(frozen=True)
class CampaignLimits:
    deadline_minutes: int
    candidate_cutoff_minutes: int
    max_changed_candidates: int
    transient_retries: int

    def __post_init__(self) -> None:
        if not 1 <= self.deadline_minutes <= 50:
            raise ValueError("deadline_minutes must be between 1 and 50")
        if not 1 <= self.candidate_cutoff_minutes <= 40:
            raise ValueError(
                "candidate_cutoff_minutes must be between 1 and 40"
            )
        if self.candidate_cutoff_minutes >= self.deadline_minutes:
            raise ValueError(
                "candidate_cutoff_minutes must precede deadline_minutes"
            )
        if not 1 <= self.max_changed_candidates <= 3:
            raise ValueError("max_changed_candidates must be between 1 and 3")
        if not 0 <= self.transient_retries <= 1:
            raise ValueError("transient_retries must be zero or one")


@dataclass(frozen=True)
class PatchArtifact:
    candidate_id: str
    path: Path
    sha256: str
    base_commit: str
    result_commit: str

    def __post_init__(self) -> None:
        _identifier(self.candidate_id, "candidate_id")
        object.__setattr__(
            self,
            "path",
            _repository_path(self.path, "path"),
        )
        if not _SHA256.fullmatch(self.sha256):
            raise ValueError("sha256 is invalid")
        if not _COMMIT.fullmatch(self.base_commit):
            raise ValueError("base_commit is invalid")
        if not _COMMIT.fullmatch(self.result_commit):
            raise ValueError("result_commit is invalid")


@dataclass(frozen=True)
class CandidateArtifact:
    candidate_id: str
    patch: PatchArtifact
    draft_id: str
    evidence_path: Path
    eligible: bool
    metrics: Mapping[str, float]

    def __post_init__(self) -> None:
        _identifier(self.candidate_id, "candidate_id")
        if self.patch.candidate_id != self.candidate_id:
            raise ValueError("patch candidate_id does not match")
        if not _DRAFT.fullmatch(self.draft_id):
            raise ValueError("draft_id must identify a draft")
        object.__setattr__(
            self,
            "evidence_path",
            _repository_path(self.evidence_path, "evidence_path"),
        )
        normalized_metrics: dict[str, float] = {}
        for name, value in self.metrics.items():
            _identifier(name, "metric name")
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError("metric values must be numeric")
            normalized_metrics[name] = float(value)
        object.__setattr__(
            self,
            "metrics",
            MappingProxyType(normalized_metrics),
        )


@dataclass(frozen=True)
class CampaignReport:
    campaign_id: str
    target: str
    base_commit: str
    baseline_draft_id: str
    candidates: tuple[CandidateArtifact, ...]
    pareto_candidate_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.campaign_id, "campaign_id")
        _identifier(self.target, "target")
        if not _COMMIT.fullmatch(self.base_commit):
            raise ValueError("base_commit is invalid")
        if not _DRAFT.fullmatch(self.baseline_draft_id):
            raise ValueError("baseline_draft_id must identify a draft")
        candidate_ids = tuple(
            candidate.candidate_id for candidate in self.candidates
        )
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate IDs must be unique")
        if len(self.pareto_candidate_ids) != len(
            set(self.pareto_candidate_ids)
        ):
            raise ValueError("pareto candidate IDs must be unique")
        if not set(self.pareto_candidate_ids).issubset(candidate_ids):
            raise ValueError("pareto candidate IDs must reference candidates")
