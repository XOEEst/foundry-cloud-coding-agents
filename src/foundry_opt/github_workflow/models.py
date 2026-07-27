from __future__ import annotations

from dataclasses import dataclass
from enum import IntFlag, StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Mapping
from urllib.parse import urlparse

from foundry_opt.campaign import CampaignReport, CandidateArtifact


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def repository_path(value: Path, field: str) -> Path:
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


def git_branch(value: str, field: str) -> str:
    forbidden = "\\ ~^:?*["
    if (
        not value
        or value.startswith(("-", "/", "."))
        or value.endswith(("/", "."))
        or ".." in value
        or "@{" in value
        or "//" in value
        or any(character in value for character in forbidden)
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{field} is not a safe Git branch")
    return value


class GitHubCapabilities(IntFlag):
    NONE = 0
    ISSUES = 1
    LABELS = 2
    COMMENTS = 4
    PULL_REQUESTS = 8

    CAMPAIGN_PUBLICATION = ISSUES | LABELS | COMMENTS | PULL_REQUESTS
    CANDIDATE_PUBLICATION = COMMENTS | PULL_REQUESTS


@dataclass(frozen=True)
class GitHubPermissionReport:
    granted: GitHubCapabilities


@dataclass(frozen=True)
class RepositoryState:
    repository: str
    default_branch: str
    default_commit: str

    def __post_init__(self) -> None:
        if not re.fullmatch(
            r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+",
            self.repository,
        ):
            raise ValueError("repository is invalid")
        git_branch(self.default_branch, "default_branch")
        if not _COMMIT.fullmatch(self.default_commit):
            raise ValueError("default_commit is invalid")


@dataclass(frozen=True)
class ArtifactReference:
    path: Path
    sha256: str
    redacted: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", repository_path(self.path, "path"))
        if not _SHA256.fullmatch(self.sha256):
            raise ValueError("sha256 is invalid")


@dataclass(frozen=True)
class PullRequestReference:
    number: int
    url: str
    head_branch: str
    head_commit: str
    draft: bool
    body: str = ""

    def __post_init__(self) -> None:
        if self.number < 1:
            raise ValueError("pull request number must be positive")
        _github_reference_url(self.url, "pull", self.number)
        git_branch(self.head_branch, "head_branch")
        if not _COMMIT.fullmatch(self.head_commit):
            raise ValueError("head_commit is invalid")


@dataclass(frozen=True)
class IssueReference:
    number: int
    url: str
    title: str
    body: str

    def __post_init__(self) -> None:
        if self.number < 1:
            raise ValueError("issue number must be positive")
        _github_reference_url(self.url, "issues", self.number)
        if not self.title:
            raise ValueError("issue title is required")


@dataclass(frozen=True)
class WorkflowFailure:
    operation: str
    subject: str
    code: str
    message: str


@dataclass(frozen=True)
class CandidateIssuePublication:
    candidate_id: str
    issue: IssueReference


@dataclass(frozen=True)
class CampaignPublicationRequest:
    repository_root: Path
    report: CampaignReport
    head_branch: str
    head_commit: str
    manifests: tuple[ArtifactReference, ...]
    evidence_sha256: Mapping[str, str]
    reproduction_instructions: tuple[str, ...]
    dependencies: Mapping[str, tuple[str, ...]] | None = None
    sensitive_values: tuple[str, ...] = ()
    cleanup_requested: bool = False
    candidate_pull_requests: Mapping[str, PullRequestReference] | None = None

    def __post_init__(self) -> None:
        git_branch(self.head_branch, "head_branch")
        if not _COMMIT.fullmatch(self.head_commit):
            raise ValueError("head_commit is invalid")
        for candidate_id, digest in self.evidence_sha256.items():
            if not candidate_id or not _SHA256.fullmatch(digest):
                raise ValueError("evidence_sha256 is invalid")
        if not self.reproduction_instructions:
            raise ValueError("reproduction_instructions are required")
        if any(not manifest.redacted for manifest in self.manifests):
            raise ValueError("campaign manifests must be redacted")
        pareto_candidates = {
            candidate.candidate_id: candidate
            for candidate in self.report.candidates
            if candidate.candidate_id in self.report.pareto_candidate_ids
        }
        if set(self.evidence_sha256) != set(pareto_candidates):
            raise ValueError(
                "evidence_sha256 must exactly cover eligible Pareto candidates"
            )
        if any(
            candidate.patch.base_commit != self.report.base_commit
            for candidate in pareto_candidates.values()
        ):
            raise ValueError(
                "candidate patch bases must match the campaign base"
            )
        for instruction in self.reproduction_instructions:
            normalized = instruction.casefold()
            if (
                not instruction.strip()
                or "\n" in instruction
                or "\r" in instruction
                or any(
                    phrase in normalized
                    for phrase in (
                        "raw prompt",
                        "raw response",
                        "dataset row",
                        "tool payload",
                    )
                )
            ):
                raise ValueError(
                    "reproduction instructions must be redacted summaries"
                )


@dataclass(frozen=True)
class CampaignPublication:
    campaign_pull_request: PullRequestReference
    candidate_issues: tuple[CandidateIssuePublication, ...]
    failures: tuple[WorkflowFailure, ...] = ()
    campaign_closed: bool = False


class CandidateApplicationStatus(StrEnum):
    APPLIED = "applied"
    REJECTED = "rejected"
    ALREADY_APPLIED = "already_applied"


@dataclass(frozen=True)
class CandidateApplicationResult:
    status: CandidateApplicationStatus
    candidate_id: str
    reason_code: str | None = None
    pull_request: PullRequestReference | None = None
    commit_sha: str | None = None
    failures: tuple[WorkflowFailure, ...] = ()


@dataclass(frozen=True)
class ArtifactInspection:
    path: Path
    sha256: str
    byte_count: int
    content: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", repository_path(self.path, "path"))
        if not _SHA256.fullmatch(self.sha256):
            raise ValueError("sha256 is invalid")
        if self.byte_count != len(self.content):
            raise ValueError("byte_count does not match content")


@dataclass(frozen=True)
class ExactPatchRequest:
    repository_root: Path
    base_commit: str
    patch_path: Path
    expected_patch_sha256: str
    branch: str
    commit_message: str

    def __post_init__(self) -> None:
        if not _COMMIT.fullmatch(self.base_commit):
            raise ValueError("base_commit is invalid")
        object.__setattr__(
            self,
            "patch_path",
            repository_path(self.patch_path, "patch_path"),
        )
        if not _SHA256.fullmatch(self.expected_patch_sha256):
            raise ValueError("expected_patch_sha256 is invalid")
        git_branch(self.branch, "branch")
        if not self.commit_message:
            raise ValueError("commit_message is required")


@dataclass(frozen=True)
class AppliedPatch:
    branch: str
    commit_sha: str
    changed_paths: tuple[Path, ...]
    exact: bool
    substantive_repair: bool

    def __post_init__(self) -> None:
        git_branch(self.branch, "branch")
        if not _COMMIT.fullmatch(self.commit_sha):
            raise ValueError("applied patch identity is invalid")
        object.__setattr__(
            self,
            "changed_paths",
            tuple(
                repository_path(path, "changed_path")
                for path in self.changed_paths
            ),
        )


@dataclass(frozen=True)
class CandidateApplicationRequest:
    repository_root: Path
    campaign_id: str
    target: str
    expected_default_branch: str
    session_id: str
    campaign_pull_request_number: int
    candidate_issue_number: int
    candidate: CandidateArtifact
    evidence_sha256: str
    close_rejected: bool = False
    rejected_pull_request_number: int | None = None

    def __post_init__(self) -> None:
        if not all(
            (
                self.campaign_id,
                self.target,
                self.expected_default_branch,
                self.session_id,
            )
        ):
            raise ValueError(
                "campaign, target, branch, and session IDs are required"
            )
        git_branch(self.expected_default_branch, "expected_default_branch")
        if (
            self.campaign_pull_request_number < 1
            or self.candidate_issue_number < 1
        ):
            raise ValueError("GitHub numbers must be positive")
        if not isinstance(self.candidate, CandidateArtifact):
            raise ValueError("candidate must be a CandidateArtifact")
        if not self.candidate.eligible:
            raise ValueError("candidate must be eligible")
        if not _SHA256.fullmatch(self.evidence_sha256):
            raise ValueError("evidence_sha256 is invalid")
        if (
            self.rejected_pull_request_number is not None
            and self.rejected_pull_request_number < 1
        ):
            raise ValueError("rejected_pull_request_number must be positive")


def _github_reference_url(url: str, kind: str, number: int) -> None:
    try:
        parsed = urlparse(url)
    except ValueError as error:
        raise ValueError("GitHub URL is invalid") from error
    parts = tuple(part for part in parsed.path.split("/") if part)
    if (
        parsed.scheme != "https"
        or parsed.netloc.casefold() != "github.com"
        or parsed.query
        or parsed.fragment
        or len(parts) != 4
        or parts[2] != kind
        or parts[3] != str(number)
    ):
        raise ValueError("GitHub URL is invalid")
