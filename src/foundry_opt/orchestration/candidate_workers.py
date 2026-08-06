from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
import hashlib
import json
from math import isfinite
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from types import MappingProxyType
from typing import Mapping, Protocol

from foundry_opt.campaign.models import CampaignLimits
from foundry_opt.campaign.protocols import (
    BundleBuilder,
    CampaignRepository,
    CampaignWorktree,
    Clock,
    EvidenceWriter,
    ValidationRunner,
)
from foundry_opt.drafts import DraftRecord
from foundry_opt.evaluation import (
    AgentVersionRef,
    DatasetSplit,
    EvaluationPolicy,
    EvaluationResult,
    EvaluationSubject,
    select_eligible_candidates,
)
from foundry_opt.evidence import EvaluationAssetReference, EvidenceRequest
from foundry_opt.orchestration.campaign import OptimizationCampaign
from foundry_opt.orchestration.git_state import (
    OutboxRecord,
    StateObject,
    StateRefConflictError,
    StateRefPushUnacknowledgedError,
    StateRefSnapshot,
)
from foundry_opt.orchestration.models import (
    AdvanceRequest,
    CampaignEvent,
    CampaignPhase,
    CampaignState,
    EventKind,
)
from foundry_opt.packaging import BundleArtifact
from foundry_opt.security import reject_secret_content


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_MAX_REDACTED_TEXT = 512
_SENSITIVE_TEXT_MARKERS = (
    "authorization: bearer ",
    "authorization=bearer ",
    "access_token=",
    "access-token=",
    "api_key=",
    "api-key=",
    "client_secret=",
    "clientsecret=",
    "sharedaccesskey=",
    "sharedaccesssignature=",
    "?sig=",
    "&sig=",
)


def _identifier(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field_name} must be an identifier")


def _redacted_text(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_REDACTED_TEXT
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field_name} must be bounded redacted text")
    if any(
        marker in value.casefold()
        for marker in _SENSITIVE_TEXT_MARKERS
    ):
        raise ValueError(f"{field_name} contains sensitive content")
    reject_secret_content(value)


def _goal_text(value: str) -> None:
    if (
        not isinstance(value, str)
        or not 20 <= len(value) <= 4000
        or any(
            (
                ord(character) < 32
                and character not in {"\n", "\t"}
            )
            or ord(character) == 127
            for character in value
        )
    ):
        raise ValueError("goal must be bounded redacted text")
    if any(
        marker in value.casefold()
        for marker in _SENSITIVE_TEXT_MARKERS
    ):
        raise ValueError("goal contains sensitive content")
    reject_secret_content(value)


def _repository_path(value: Path, field_name: str) -> Path:
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


def _metrics(values: Mapping[str, float], field_name: str) -> Mapping[str, float]:
    normalized: dict[str, float] = {}
    for name, value in values.items():
        _identifier(name, f"{field_name} metric")
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not isfinite(value)
        ):
            raise ValueError(f"{field_name} metrics must be finite numbers")
        normalized[name] = float(value)
    return MappingProxyType(normalized)


@dataclass(frozen=True)
class CandidateIterationFeedback:
    """Redacted aggregate feedback available to the next specialist."""

    candidate_id: str
    idea_id: str
    result: str
    metrics: Mapping[str, float]
    eligible: bool
    lessons: tuple[str, ...]
    complexity: str

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.candidate_id, "candidate_id"),
            (self.idea_id, "idea_id"),
            (self.result, "result"),
        ):
            _identifier(value, field_name)
        if type(self.eligible) is not bool:
            raise ValueError("eligible must be boolean")
        if not self.lessons:
            raise ValueError("feedback lessons must not be empty")
        for lesson in self.lessons:
            _redacted_text(lesson, "lesson")
        _redacted_text(self.complexity, "complexity")
        object.__setattr__(
            self,
            "metrics",
            _metrics(self.metrics, "feedback"),
        )


@dataclass(frozen=True)
class CandidateDesignIntent:
    """Transient, exact input passed to the candidate-designer specialist."""

    effect_id: str
    issue_number: int
    generation: int
    spec_sha256: str
    base_commit: str
    target: str
    candidate_id: str
    slot: int
    worktree: Path
    goal: str
    edit_paths: tuple[Path, ...]
    allowed_mutations: frozenset[str]
    restricted_opt_ins: Mapping[str, bool]
    baseline_metrics: Mapping[str, float]
    feedback: tuple[CandidateIterationFeedback, ...] = ()

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.effect_id, "effect_id"),
            (self.target, "target"),
            (self.candidate_id, "candidate_id"),
        ):
            _identifier(value, field_name)
        if self.issue_number < 1:
            raise ValueError("issue_number must be positive")
        if self.generation < 1:
            raise ValueError("generation must be positive")
        if self.slot < 1:
            raise ValueError("slot must be positive")
        if not _SHA256.fullmatch(self.spec_sha256):
            raise ValueError("spec_sha256 must be a SHA-256 digest")
        if not _COMMIT.fullmatch(self.base_commit):
            raise ValueError("base_commit must be a full Git commit")
        if not self.worktree.is_absolute():
            raise ValueError("worktree must be absolute")
        _goal_text(self.goal)
        if not self.edit_paths:
            raise ValueError("edit_paths must not be empty")
        if not self.allowed_mutations:
            raise ValueError("allowed_mutations must not be empty")
        for mutation in self.allowed_mutations:
            _identifier(mutation, "allowed mutation")
        opt_ins = dict(self.restricted_opt_ins)
        for name, enabled in opt_ins.items():
            _identifier(name, "restricted opt-in")
            if type(enabled) is not bool:
                raise ValueError("restricted opt-ins must be boolean")
        object.__setattr__(
            self,
            "restricted_opt_ins",
            MappingProxyType(opt_ins),
        )
        object.__setattr__(
            self,
            "baseline_metrics",
            _metrics(self.baseline_metrics, "baseline"),
        )
        object.__setattr__(
            self,
            "edit_paths",
            tuple(
                _repository_path(path, "edit_path")
                for path in self.edit_paths
            ),
        )
        if any(
            not isinstance(item, CandidateIterationFeedback)
            for item in self.feedback
        ):
            raise ValueError(
                "feedback must contain CandidateIterationFeedback values"
            )


@dataclass(frozen=True)
class CandidateDesignResult:
    """Privacy-safe specialist output; the steward computes the outcome."""

    effect_id: str
    result_id: str
    issue_number: int
    generation: int
    spec_sha256: str
    base_commit: str
    candidate_id: str
    slot: int
    idea_id: str
    mutation_class: str
    parent_idea_ids: tuple[str, ...] = ()
    required_opt_ins: frozenset[str] = frozenset()
    motivation: str = field(default="")
    lessons: tuple[str, ...] = ()
    complexity: str = field(default="")

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.effect_id, "effect_id"),
            (self.result_id, "result_id"),
            (self.candidate_id, "candidate_id"),
            (self.idea_id, "idea_id"),
            (self.mutation_class, "mutation_class"),
        ):
            _identifier(value, field_name)
        if self.issue_number < 1:
            raise ValueError("issue_number must be positive")
        if self.generation < 1:
            raise ValueError("generation must be positive")
        if self.slot < 1:
            raise ValueError("slot must be positive")
        if not _SHA256.fullmatch(self.spec_sha256):
            raise ValueError("spec_sha256 must be a SHA-256 digest")
        if not _COMMIT.fullmatch(self.base_commit):
            raise ValueError("base_commit must be a full Git commit")
        for parent in self.parent_idea_ids:
            _identifier(parent, "parent_idea_id")
        if len(set(self.parent_idea_ids)) != len(self.parent_idea_ids):
            raise ValueError("parent_idea_ids must be unique")
        for opt_in in self.required_opt_ins:
            _identifier(opt_in, "required_opt_in")
        _redacted_text(self.motivation, "motivation")
        if not self.lessons:
            raise ValueError("lessons must not be empty")
        for lesson in self.lessons:
            _redacted_text(lesson, "lesson")
        _redacted_text(self.complexity, "complexity")

    def require_matches(self, intent: CandidateDesignIntent) -> None:
        bindings = (
            ("effect_id", self.effect_id, intent.effect_id),
            ("issue_number", self.issue_number, intent.issue_number),
            ("generation", self.generation, intent.generation),
            ("spec_sha256", self.spec_sha256, intent.spec_sha256),
            ("base_commit", self.base_commit, intent.base_commit),
            ("candidate_id", self.candidate_id, intent.candidate_id),
            ("slot", self.slot, intent.slot),
        )
        for field_name, actual, expected in bindings:
            if actual != expected:
                raise ValueError(
                    f"candidate designer {field_name} does not match reservation"
                )


@dataclass(frozen=True)
class CandidateDesignArtifact:
    ref: str
    head_commit: str
    tree_sha: str
    changed_paths: tuple[Path, ...]

    def __post_init__(self) -> None:
        if re.fullmatch(
            r"refs/heads/foundry-opt/design/issue-[1-9][0-9]*/"
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
            self.ref,
        ) is None:
            raise ValueError("candidate design ref is invalid")
        if not _COMMIT.fullmatch(self.head_commit):
            raise ValueError("candidate design head_commit is invalid")
        if not _COMMIT.fullmatch(self.tree_sha):
            raise ValueError("candidate design tree_sha is invalid")
        paths = tuple(
            _repository_path(path, "changed_path")
            for path in self.changed_paths
        )
        if not paths or len(set(paths)) != len(paths):
            raise ValueError(
                "candidate design changed_paths must be non-empty and unique"
            )
        object.__setattr__(self, "changed_paths", paths)


class CandidateDesignPushUnacknowledgedError(RuntimeError):
    def __init__(self, artifact: CandidateDesignArtifact) -> None:
        self.artifact = artifact
        super().__init__(
            "candidate design ref push was not acknowledged"
        )


@dataclass(frozen=True)
class CandidateDesignSubmissionRequest:
    repository_root: Path
    issue_number: int
    effect_id: str
    worker_issue_number: int
    result_file: Path

    def __post_init__(self) -> None:
        if self.issue_number < 1:
            raise ValueError("issue_number must be positive")
        if self.worker_issue_number < 1:
            raise ValueError("worker_issue_number must be positive")
        _identifier(self.effect_id, "effect_id")
        if not self.result_file.is_absolute():
            raise ValueError("result_file must be absolute")


class CandidateDesignSubmissionStatus(StrEnum):
    RECORDED = "recorded"
    ALREADY_RECORDED = "already_recorded"
    WAITING = "waiting"
    CONFLICT = "conflict"
    FAILED = "failed"


@dataclass(frozen=True)
class CandidateDesignSubmissionResult:
    status: CandidateDesignSubmissionStatus
    snapshot: StateRefSnapshot
    code: str | None = None


class CandidateDesignRepository(Protocol):
    def capture(
        self,
        request: CandidateDesignSubmissionRequest,
        intent: CandidateDesignIntent,
        result: CandidateDesignResult,
    ) -> CandidateDesignArtifact: ...

    def cleanup(
        self,
        request: CandidateDesignSubmissionRequest,
        intent: CandidateDesignIntent,
    ) -> None: ...


class CandidateDesignHandoffs(Protocol):
    def persist_candidate_design(
        self,
        repository_root: Path,
        *,
        snapshot: StateRefSnapshot,
        request: CandidateDesignSubmissionRequest,
        intent: CandidateDesignIntent,
        result: CandidateDesignResult,
        artifact: CandidateDesignArtifact,
    ) -> object: ...

class CandidateDesignSubmissionService:
    """Persist one typed designer result and its exact remote Git artifact."""

    def __init__(
        self,
        *,
        ledger: CandidateWorkerLedger,
        repository: CandidateDesignRepository,
        handoffs: CandidateDesignHandoffs | None = None,
    ) -> None:
        self._ledger = ledger
        self._repository = repository
        self._handoffs = handoffs

    def submit(
        self,
        request: CandidateDesignSubmissionRequest,
    ) -> CandidateDesignSubmissionResult:
        snapshot = self._ledger.load(
            request.repository_root,
            request.issue_number,
        )
        if snapshot is None:
            raise ValueError("candidate design requires campaign state")
        planned = tuple(
            record
            for record in snapshot.outbox
            if (
                record.kind == "specialist_work_request"
                and record.generation == snapshot.state.generation
                and record.payload.get("effect_id") == request.effect_id
                and record.payload.get("issue_number")
                == request.issue_number
                and record.payload.get("specialist")
                == "foundry-candidate-designer"
                and record.payload.get("work_kind") == "design_candidate"
            )
        )
        if len(planned) != 1:
            return CandidateDesignSubmissionResult(
                CandidateDesignSubmissionStatus.FAILED,
                snapshot,
                "candidate_design_intent_unavailable",
            )
        intent = _candidate_design_intent(
            request.repository_root,
            planned[0],
        )
        try:
            result = _candidate_design_result(request.result_file)
            result.require_matches(intent)
        except (OSError, TypeError, ValueError):
            return CandidateDesignSubmissionResult(
                CandidateDesignSubmissionStatus.FAILED,
                snapshot,
                "candidate_design_result_invalid",
            )
        assignments = tuple(
            record
            for record in snapshot.outbox
            if (
                record.kind == "specialist_work_succeeded"
                and record.generation == snapshot.state.generation
                and record.payload.get("effect_id")
                == planned[0].record_id
                and record.payload.get("specialist")
                == "foundry-candidate-designer"
                and record.payload.get("work_kind") == "design_candidate"
            )
        )
        if len(assignments) > 1:
            return CandidateDesignSubmissionResult(
                CandidateDesignSubmissionStatus.FAILED,
                snapshot,
                "candidate_design_assignment_invalid",
            )
        acknowledged_issue = (
            assignments[0].payload.get("worker_issue_number")
            if assignments
            else request.worker_issue_number
        )
        if (
            type(acknowledged_issue) is not int
            or acknowledged_issue != request.worker_issue_number
        ):
            return CandidateDesignSubmissionResult(
                CandidateDesignSubmissionStatus.FAILED,
                snapshot,
                "candidate_design_assignment_invalid",
            )
        worker_issue_number = request.worker_issue_number
        record_id = f"{request.effect_id}-submitted"
        existing = _record(snapshot, record_id)
        if existing is not None:
            if not _submitted_design_matches(
                existing,
                result,
                worker_issue_number,
            ):
                return CandidateDesignSubmissionResult(
                    CandidateDesignSubmissionStatus.FAILED,
                    snapshot,
                    "candidate_design_result_conflict",
                )
            try:
                self._repository.cleanup(request, intent)
            except Exception:
                return CandidateDesignSubmissionResult(
                    CandidateDesignSubmissionStatus.FAILED,
                    snapshot,
                    "candidate_design_cleanup_failed",
                )
            return CandidateDesignSubmissionResult(
                CandidateDesignSubmissionStatus.ALREADY_RECORDED,
                snapshot,
            )
        try:
            artifact = self._repository.capture(request, intent, result)
            submitted = _candidate_design_submission_record(
                snapshot,
                record_id,
                result,
                artifact,
                worker_issue_number,
            )
            persisted = self._ledger.commit(
                request.repository_root,
                issue_number=request.issue_number,
                expected_revision=snapshot.revision,
                state=snapshot.state,
                outbox=(submitted,),
            )
        except CandidateDesignPushUnacknowledgedError as error:
            try:
                self._repository.cleanup(request, intent)
                if self._handoffs is None:
                    raise RuntimeError("candidate design handoff unavailable")
                self._handoffs.persist_candidate_design(
                    request.repository_root,
                    snapshot=snapshot,
                    request=request,
                    intent=intent,
                    result=result,
                    artifact=error.artifact,
                )
            except Exception:
                return CandidateDesignSubmissionResult(
                    CandidateDesignSubmissionStatus.FAILED,
                    snapshot,
                    "candidate_design_handoff_failed",
                )
            return CandidateDesignSubmissionResult(
                CandidateDesignSubmissionStatus.WAITING,
                snapshot,
                "candidate_design_handoff_created",
            )
        except StateRefPushUnacknowledgedError:
            try:
                self._repository.cleanup(request, intent)
                if self._handoffs is None:
                    raise RuntimeError(
                        "candidate design handoff unavailable"
                    )
                self._handoffs.persist_candidate_design(
                    request.repository_root,
                    snapshot=snapshot,
                    request=request,
                    intent=intent,
                    result=result,
                    artifact=artifact,
                )
            except Exception:
                return CandidateDesignSubmissionResult(
                    CandidateDesignSubmissionStatus.FAILED,
                    snapshot,
                    "candidate_design_handoff_failed",
                )
            return CandidateDesignSubmissionResult(
                CandidateDesignSubmissionStatus.WAITING,
                snapshot,
                "candidate_design_handoff_created",
            )
        except StateRefConflictError:
            return CandidateDesignSubmissionResult(
                CandidateDesignSubmissionStatus.CONFLICT,
                snapshot,
                "state_ref_conflict",
            )
        except Exception:
            return CandidateDesignSubmissionResult(
                CandidateDesignSubmissionStatus.FAILED,
                snapshot,
                "candidate_design_capture_failed",
            )
        try:
            self._repository.cleanup(request, intent)
        except Exception:
            return CandidateDesignSubmissionResult(
                CandidateDesignSubmissionStatus.FAILED,
                persisted,
                "candidate_design_cleanup_failed",
            )
        return CandidateDesignSubmissionResult(
            CandidateDesignSubmissionStatus.RECORDED,
            persisted,
        )


@dataclass(frozen=True)
class CandidateWorkerPlan:
    """Immutable, transient campaign inputs resolved by the steward."""

    issue_number: int
    generation: int
    spec_sha256: str
    base_commit: str
    target: str
    base_agent_version: int
    goal: str
    limits: CampaignLimits
    edit_paths: tuple[Path, ...]
    allowed_mutations: frozenset[str]
    restricted_opt_ins: Mapping[str, bool]
    evaluation_policy: EvaluationPolicy
    assets: tuple[EvaluationAssetReference, ...]
    evidence_root: Path

    def __post_init__(self) -> None:
        if self.issue_number < 1:
            raise ValueError("issue_number must be positive")
        if self.generation < 1:
            raise ValueError("generation must be positive")
        if not _SHA256.fullmatch(self.spec_sha256):
            raise ValueError("spec_sha256 must be a SHA-256 digest")
        if not _COMMIT.fullmatch(self.base_commit):
            raise ValueError("base_commit must be a full Git commit")
        _identifier(self.target, "target")
        if (
            type(self.base_agent_version) is not int
            or self.base_agent_version < 1
        ):
            raise ValueError("base_agent_version must be positive")
        _goal_text(self.goal)
        if not self.edit_paths:
            raise ValueError("edit_paths must not be empty")
        if not self.allowed_mutations:
            raise ValueError("allowed_mutations must not be empty")
        for mutation in self.allowed_mutations:
            _identifier(mutation, "allowed mutation")
        opt_ins = dict(self.restricted_opt_ins)
        for name, enabled in opt_ins.items():
            _identifier(name, "restricted opt-in")
            if type(enabled) is not bool:
                raise ValueError("restricted opt-ins must be boolean")
        if not self.assets:
            raise ValueError("assets must not be empty")
        object.__setattr__(
            self,
            "restricted_opt_ins",
            MappingProxyType(opt_ins),
        )
        object.__setattr__(self, "assets", tuple(self.assets))
        object.__setattr__(
            self,
            "edit_paths",
            tuple(
                _repository_path(path, "edit_path")
                for path in self.edit_paths
            ),
        )
        object.__setattr__(
            self,
            "evidence_root",
            _repository_path(self.evidence_root, "evidence_root"),
        )

    @property
    def campaign_id(self) -> str:
        return (
            f"issue-{self.issue_number}-g{self.generation}-"
            f"{self.spec_sha256[:8]}-{self.base_commit[:8]}"
        )

    @property
    def goal_sha256(self) -> str:
        return hashlib.sha256(self.goal.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CandidateDraftIntent:
    effect_id: str
    issue_number: int
    generation: int
    spec_sha256: str
    base_commit: str
    target: str
    subject_id: str
    base_agent_version: int
    idempotency_key: str
    bundle: BundleArtifact

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.effect_id, "effect_id"),
            (self.target, "target"),
            (self.subject_id, "subject_id"),
        ):
            _identifier(value, field_name)
        if self.issue_number < 1 or self.generation < 1:
            raise ValueError("draft intent issue and generation must be positive")
        if not _SHA256.fullmatch(self.spec_sha256):
            raise ValueError("draft intent spec_sha256 is invalid")
        if not _COMMIT.fullmatch(self.base_commit):
            raise ValueError("draft intent base_commit is invalid")
        if (
            type(self.base_agent_version) is not int
            or self.base_agent_version < 1
        ):
            raise ValueError("draft intent base version must be positive")
        if not _SHA256.fullmatch(self.idempotency_key):
            raise ValueError("draft intent idempotency key is invalid")


@dataclass(frozen=True)
class CandidateEvaluationIntent:
    effect_id: str
    issue_number: int
    generation: int
    spec_sha256: str
    base_commit: str
    subject: EvaluationSubject
    split: DatasetSplit
    policy: EvaluationPolicy

    def __post_init__(self) -> None:
        _identifier(self.effect_id, "effect_id")
        if self.issue_number < 1 or self.generation < 1:
            raise ValueError(
                "evaluation intent issue and generation must be positive"
            )
        if not _SHA256.fullmatch(self.spec_sha256):
            raise ValueError("evaluation intent spec_sha256 is invalid")
        if not _COMMIT.fullmatch(self.base_commit):
            raise ValueError("evaluation intent base_commit is invalid")
        if self.split is not DatasetSplit.DEVELOPMENT:
            raise ValueError(
                "candidate workers may only evaluate development data"
            )


class CandidateDesigner(Protocol):
    def reconcile(
        self,
        intent: CandidateDesignIntent,
    ) -> tuple[CandidateDesignResult, ...]: ...

    def invoke(self, intent: CandidateDesignIntent) -> CandidateDesignResult: ...


class CandidateDesignPending(RuntimeError):
    """Signal that a durable designer assignment must complete first."""


class CandidateEffectPending(RuntimeError):
    """Signal that trusted Actions must execute a persisted Foundry effect."""

    def __init__(self, effect_kind: str) -> None:
        if effect_kind not in {"foundry_draft", "foundry_evaluation"}:
            raise ValueError("candidate pending effect kind is invalid")
        self.effect_kind = effect_kind
        super().__init__(f"{effect_kind} awaits trusted capability execution")


class CandidateDraftEffects(Protocol):
    def reconcile(
        self,
        intent: CandidateDraftIntent,
    ) -> DraftRecord | None: ...

    def create(self, intent: CandidateDraftIntent) -> DraftRecord: ...


class CandidateEvaluationEffects(Protocol):
    def reconcile(
        self,
        intent: CandidateEvaluationIntent,
    ) -> EvaluationResult | None: ...

    def run(self, intent: CandidateEvaluationIntent) -> EvaluationResult: ...


@dataclass(frozen=True)
class CandidateAssetsRegistrationPlan:
    effect_id: str
    issue_number: int
    generation: int
    spec_sha256: str
    base_commit: str
    target: str
    environment: str
    max_attempts: int
    intent: StateObject

    def __post_init__(self) -> None:
        _identifier(self.effect_id, "asset registration effect_id")
        if self.issue_number < 1 or self.generation < 1:
            raise ValueError(
                "asset registration issue and generation must be positive"
            )
        if not _SHA256.fullmatch(self.spec_sha256):
            raise ValueError("asset registration spec_sha256 is invalid")
        if not _COMMIT.fullmatch(self.base_commit):
            raise ValueError("asset registration base_commit is invalid")
        _identifier(self.target, "asset registration target")
        _identifier(self.environment, "asset registration environment")
        if self.max_attempts < 1:
            raise ValueError(
                "asset registration max_attempts must be positive"
            )
        if self.intent.path != (
            f"objects/capabilities/{self.effect_id}.json"
        ):
            raise ValueError("asset registration intent path is invalid")

    @property
    def payload(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "base_commit": self.base_commit,
                "capability_path": self.intent.path,
                "capability_sha256": self.intent.sha256,
                "effect_id": self.effect_id,
                "effect_kind": "foundry_assets",
                "environment": self.environment,
                "issue_number": self.issue_number,
                "max_attempts": self.max_attempts,
                "spec_sha256": self.spec_sha256,
                "target": self.target,
            }
        )


class CandidateAssetsRegistrationPending(RuntimeError):
    """Signal that trusted Actions must materialize approved assets."""

    def __init__(self, plan: CandidateAssetsRegistrationPlan) -> None:
        self.plan = plan
        super().__init__("candidate assets await trusted capability execution")


class CandidateWorkerPlanResolver(Protocol):
    def resolve(
        self,
        request: CandidateWorkerRequest,
        state: CampaignState,
    ) -> CandidateWorkerPlan: ...


class CandidateWorkerLedger(Protocol):
    def load(
        self,
        repository_root: Path,
        issue_number: int,
    ) -> StateRefSnapshot | None: ...

    def commit(
        self,
        repository_root: Path,
        *,
        issue_number: int,
        expected_revision: str | None,
        state: CampaignState,
        inbox: tuple[CampaignEvent, ...] = (),
        outbox: tuple[OutboxRecord, ...] = (),
        objects: tuple[StateObject, ...] = (),
    ) -> StateRefSnapshot: ...


@dataclass(frozen=True)
class CandidateWorkerDependencies:
    repository: CampaignRepository
    designer: CandidateDesigner
    validate: ValidationRunner
    build_bundle: BundleBuilder
    drafts: CandidateDraftEffects
    evaluations: CandidateEvaluationEffects
    write_evidence: EvidenceWriter
    clock: Clock


@dataclass(frozen=True)
class CandidateWorkerRequest:
    repository_root: Path
    issue_number: int
    session_deadline: datetime | None = None

    def __post_init__(self) -> None:
        if self.issue_number < 1:
            raise ValueError("issue_number must be positive")
        if (
            self.session_deadline is not None
            and self.session_deadline.tzinfo is None
        ):
            raise ValueError("session_deadline must be timezone-aware")


class CandidateWorkerStatus(StrEnum):
    COMPLETE = "complete"
    WAITING = "waiting"
    BLOCKED = "blocked"
    FAILED = "failed"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class CandidateWorkerResult:
    status: CandidateWorkerStatus
    snapshot: StateRefSnapshot
    summary: str
    code: str | None = None


class _CandidateSessionTimeout(RuntimeError):
    def __init__(self, snapshot: StateRefSnapshot) -> None:
        self.snapshot = snapshot
        super().__init__("candidate worker session timed out")


class _CandidateDesignDeferred(RuntimeError):
    def __init__(self, snapshot: StateRefSnapshot) -> None:
        self.snapshot = snapshot
        super().__init__("candidate design is awaiting a specialist")


class _CandidateEffectDeferred(RuntimeError):
    def __init__(
        self,
        snapshot: StateRefSnapshot,
        effect_kind: str,
    ) -> None:
        self.snapshot = snapshot
        self.effect_kind = effect_kind
        super().__init__(
            f"{effect_kind} is awaiting trusted capability execution"
        )


class _CandidateRecoveryFailure(RuntimeError):
    def __init__(
        self,
        snapshot: StateRefSnapshot,
        code: str,
        summary: str,
    ) -> None:
        self.snapshot = snapshot
        self.code = code
        self.summary = summary
        super().__init__(summary)


class CandidateWorkerService:
    """Drive the baseline and changed-candidate loop from the steward ledger."""

    def __init__(
        self,
        *,
        ledger: CandidateWorkerLedger,
        resolver: CandidateWorkerPlanResolver,
        dependencies: CandidateWorkerDependencies,
    ) -> None:
        self._ledger = ledger
        self._resolver = resolver
        self._deps = dependencies

    def advance(self, request: CandidateWorkerRequest) -> CandidateWorkerResult:
        snapshot = self._ledger.load(
            request.repository_root,
            request.issue_number,
        )
        if snapshot is None:
            raise ValueError("candidate workers require campaign state")
        try:
            snapshot = self._reconcile_worktree_cleanups(
                request,
                snapshot,
            )
        except StateRefConflictError:
            return CandidateWorkerResult(
                CandidateWorkerStatus.CONFLICT,
                snapshot,
                "Candidate worker state changed concurrently.",
                "state_ref_conflict",
            )
        except _CandidateRecoveryFailure as failure:
            return CandidateWorkerResult(
                CandidateWorkerStatus.FAILED,
                failure.snapshot,
                failure.summary,
                failure.code,
            )
        if snapshot.state.phase not in {
            CampaignPhase.BASELINE,
            CampaignPhase.CANDIDATES,
        }:
            return CandidateWorkerResult(
                CandidateWorkerStatus.BLOCKED,
                snapshot,
                "Candidate workers are not valid in this campaign phase.",
                "candidate_workers_phase_invalid",
            )
        try:
            plan = self._resolver.resolve(request, snapshot.state)
        except CandidateAssetsRegistrationPending as pending:
            return self._defer_assets(request, snapshot, pending.plan)
        mismatch = _plan_mismatch(plan, snapshot, request.issue_number)
        if mismatch is not None:
            return CandidateWorkerResult(
                CandidateWorkerStatus.BLOCKED,
                snapshot,
                "Candidate worker inputs are stale.",
                mismatch,
            )
        try:
            pinned = self._deps.repository.pin_default_branch(
                request.repository_root
            )
        except Exception:
            return CandidateWorkerResult(
                CandidateWorkerStatus.FAILED,
                snapshot,
                "The candidate base could not be verified.",
                "candidate_base_unavailable",
            )
        if pinned.commit != plan.base_commit:
            return CandidateWorkerResult(
                CandidateWorkerStatus.BLOCKED,
                snapshot,
                "The candidate base no longer matches the default branch.",
                "candidate_base_stale",
            )
        try:
            snapshot = self._start(request, snapshot, plan)
            if _session_expired(request, self._deps.clock):
                return CandidateWorkerResult(
                    CandidateWorkerStatus.WAITING,
                    snapshot,
                    "The steward session ended before the next effect.",
                    "session_timeout",
                )
            if snapshot.state.phase is CampaignPhase.BASELINE:
                snapshot = self._baseline(request, snapshot, plan)
            snapshot = self._candidates(request, snapshot, plan)
        except StateRefConflictError:
            return CandidateWorkerResult(
                CandidateWorkerStatus.CONFLICT,
                snapshot,
                "Candidate worker state changed concurrently.",
                "state_ref_conflict",
            )
        except _CandidateSessionTimeout as timeout:
            return CandidateWorkerResult(
                CandidateWorkerStatus.WAITING,
                timeout.snapshot,
                "The steward session ended before the next effect.",
                "session_timeout",
            )
        except _CandidateDesignDeferred as deferred:
            return CandidateWorkerResult(
                CandidateWorkerStatus.WAITING,
                deferred.snapshot,
                "Candidate design is awaiting its assigned specialist.",
                "candidate_design_pending",
            )
        except _CandidateEffectDeferred as deferred:
            label = (
                "draft"
                if deferred.effect_kind == "foundry_draft"
                else "evaluation"
            )
            return CandidateWorkerResult(
                CandidateWorkerStatus.WAITING,
                deferred.snapshot,
                (
                    f"Candidate {label} is awaiting trusted "
                    "capability execution."
                ),
                f"candidate_{label}_pending",
            )
        except _CandidateRecoveryFailure as failure:
            return CandidateWorkerResult(
                CandidateWorkerStatus.FAILED,
                failure.snapshot,
                failure.summary,
                failure.code,
            )
        if snapshot.state.phase is CampaignPhase.BLOCKED:
            return CandidateWorkerResult(
                CandidateWorkerStatus.BLOCKED,
                snapshot,
                "No eligible candidate was produced.",
                snapshot.state.block_reason,
            )
        return CandidateWorkerResult(
            CandidateWorkerStatus.COMPLETE,
            snapshot,
            "Candidate worker loop completed.",
        )

    def _defer_assets(
        self,
        request: CandidateWorkerRequest,
        snapshot: StateRefSnapshot,
        plan: CandidateAssetsRegistrationPlan,
    ) -> CandidateWorkerResult:
        if (
            plan.issue_number != request.issue_number
            or plan.issue_number != snapshot.state.issue_number
            or plan.generation != snapshot.state.generation
            or plan.spec_sha256 != snapshot.state.spec_sha256
        ):
            return CandidateWorkerResult(
                CandidateWorkerStatus.BLOCKED,
                snapshot,
                "Candidate asset registration inputs are stale.",
                "candidate_assets_registration_stale",
            )
        existing = _record(snapshot, plan.effect_id)
        existing_object = next(
            (
                item
                for item in snapshot.objects
                if item.path == plan.intent.path
            ),
            None,
        )
        if existing is not None:
            if (
                existing.kind != "candidate_assets_registration_planned"
                or dict(existing.payload) != dict(plan.payload)
                or existing_object != plan.intent
            ):
                return CandidateWorkerResult(
                    CandidateWorkerStatus.FAILED,
                    snapshot,
                    "The persisted candidate asset intent changed.",
                    "candidate_assets_registration_mismatch",
                )
            persisted = snapshot
        elif existing_object is not None:
            return CandidateWorkerResult(
                CandidateWorkerStatus.FAILED,
                snapshot,
                "The persisted candidate asset intent changed.",
                "candidate_assets_registration_mismatch",
            )
        else:
            persisted = self._ledger.commit(
                request.repository_root,
                issue_number=request.issue_number,
                expected_revision=snapshot.revision,
                state=snapshot.state,
                outbox=(
                    _outbox(
                        snapshot,
                        plan.effect_id,
                        "candidate_assets_registration_planned",
                        plan.payload,
                    ),
                ),
                objects=(plan.intent,),
            )
        return CandidateWorkerResult(
            CandidateWorkerStatus.WAITING,
            persisted,
            "Candidate assets await trusted capability execution.",
            "candidate_assets_registration_pending",
        )

    def _start(
        self,
        request: CandidateWorkerRequest,
        snapshot: StateRefSnapshot,
        plan: CandidateWorkerPlan,
    ) -> StateRefSnapshot:
        record_id = f"workers-{plan.generation}-{_binding(plan)[:16]}"
        if _record(snapshot, record_id) is not None:
            return snapshot
        started = self._deps.clock.now()
        return self._append(
            request,
            snapshot,
            (
                _outbox(
                    snapshot,
                    record_id,
                    "candidate_campaign_started",
                    {
                        "base_commit": plan.base_commit,
                        "campaign_id": plan.campaign_id,
                        "cutoff_at": _time(
                            started
                            + timedelta(
                                minutes=plan.limits.candidate_cutoff_minutes
                            )
                        ),
                        "deadline_at": _time(
                            started
                            + timedelta(minutes=plan.limits.deadline_minutes)
                        ),
                        "goal_sha256": plan.goal_sha256,
                        "issue_number": plan.issue_number,
                        "max_changed_candidates": (
                            plan.limits.max_changed_candidates
                        ),
                        "spec_sha256": plan.spec_sha256,
                        "started_at": _time(started),
                        "target": plan.target,
                    },
                ),
            ),
        )

    def _baseline(
        self,
        request: CandidateWorkerRequest,
        snapshot: StateRefSnapshot,
        plan: CandidateWorkerPlan,
    ) -> StateRefSnapshot:
        worktree = self._reserve_worktree(
            request,
            snapshot,
            plan,
            "baseline",
            0,
        )
        snapshot = self._ledger.load(
            request.repository_root, request.issue_number
        ) or snapshot
        bundle = _build_fresh_bundle(
            self._deps.build_bundle,
            worktree.path,
            worktree.path / ".foundry-opt-baseline.zip",
        )
        draft_intent = _draft_intent(plan, "baseline", bundle)
        snapshot, draft = self._draft(
            request,
            snapshot,
            draft_intent,
            plan,
        )
        evaluation_intent = _evaluation_intent(plan, "baseline", draft)
        snapshot, evaluation = self._evaluate(
            request,
            snapshot,
            evaluation_intent,
            plan,
        )
        evaluation_identity = hashlib.sha256(
            evaluation.run.evaluation_id.encode("utf-8")
        ).hexdigest()[:16]
        event = CampaignEvent(
            event_id=(
                f"baseline-{plan.generation}-"
                f"{evaluation_identity}"
            ),
            kind=EventKind.BASELINE_COMPLETED,
            generation=plan.generation,
            occurred_at=self._deps.clock.now(),
            payload={"evaluation_id": evaluation.run.evaluation_id},
        )
        state = OptimizationCampaign().advance(
            AdvanceRequest(
                plan.issue_number,
                snapshot.state,
                (event,),
            )
        ).state
        baseline_record = _outbox(
            StateRefSnapshot(
                snapshot.revision,
                state,
                snapshot.inbox,
                snapshot.outbox,
            ),
            f"baseline-attestation-{plan.generation}",
            "candidate_baseline_attestation",
            {
                "base_commit": plan.base_commit,
                "bundle_sha256": bundle.sha256,
                "draft_id": draft.version_id,
                "evaluation_id": evaluation.run.evaluation_id,
                "issue_number": plan.issue_number,
                "metrics": _aggregate_metrics(evaluation),
                "spec_sha256": plan.spec_sha256,
            },
        )
        cleanup_record = _cleanup_record(
            StateRefSnapshot(
                snapshot.revision,
                state,
                snapshot.inbox,
                snapshot.outbox,
            ),
            plan,
            "baseline",
            0,
        )
        snapshot = self._ledger.commit(
            request.repository_root,
            issue_number=request.issue_number,
            expected_revision=snapshot.revision,
            state=state,
            inbox=(event,),
            outbox=(baseline_record, cleanup_record),
        )
        return self._reconcile_worktree_cleanups(
            request,
            snapshot,
        )

    def _candidates(
        self,
        request: CandidateWorkerRequest,
        snapshot: StateRefSnapshot,
        plan: CandidateWorkerPlan,
    ) -> StateRefSnapshot:
        stop_reason = _budget_stop_reason(
            snapshot,
            plan,
            self._deps.clock,
        )
        if stop_reason is not None:
            return self._complete(
                snapshot,
                request,
                plan,
                stop_reason,
            )
        if _session_expired(request, self._deps.clock):
            raise _CandidateSessionTimeout(snapshot)
        completed = {
            record.payload["candidate_id"]
            for record in snapshot.outbox
            if (
                record.generation == plan.generation
                and record.kind == "candidate_attestation"
            )
        }
        for slot in range(1, plan.limits.max_changed_candidates + 1):
            if _session_expired(request, self._deps.clock):
                raise _CandidateSessionTimeout(snapshot)
            stop_reason = _budget_stop_reason(
                snapshot,
                plan,
                self._deps.clock,
            )
            if stop_reason is not None:
                return self._complete(
                    snapshot,
                    request,
                    plan,
                    stop_reason,
                )
            candidate_id = f"candidate-{slot}"
            if candidate_id in completed:
                continue
            snapshot = self._candidate(
                request,
                snapshot,
                plan,
                candidate_id,
                slot,
            )
        return self._complete(snapshot, request, plan, "max_candidates")

    def _candidate(
        self,
        request: CandidateWorkerRequest,
        snapshot: StateRefSnapshot,
        plan: CandidateWorkerPlan,
        candidate_id: str,
        slot: int,
    ) -> StateRefSnapshot:
        worktree = self._reserve_worktree(
            request,
            snapshot,
            plan,
            candidate_id,
            slot,
        )
        snapshot = self._ledger.load(
            request.repository_root, request.issue_number
        ) or snapshot
        baseline = self._baseline_evaluation(snapshot, plan)
        intent = CandidateDesignIntent(
            effect_id=f"design-{plan.issue_number}-{plan.generation}-{slot}",
            issue_number=plan.issue_number,
            generation=plan.generation,
            spec_sha256=plan.spec_sha256,
            base_commit=plan.base_commit,
            target=plan.target,
            candidate_id=candidate_id,
            slot=slot,
            worktree=worktree.path,
            goal=plan.goal,
            edit_paths=plan.edit_paths,
            allowed_mutations=plan.allowed_mutations,
            restricted_opt_ins=plan.restricted_opt_ins,
            baseline_metrics=_aggregate_metrics(baseline),
            feedback=_feedback(snapshot, plan, slot),
        )
        snapshot = self._plan_design_effect(
            request,
            snapshot,
            intent,
            plan,
            worktree,
        )
        design_success_id = f"{intent.effect_id}-succeeded"
        persisted_design = _record(snapshot, design_success_id)
        results = _matching_design_results(
            intent,
            self._deps.designer.reconcile(intent),
        )
        if persisted_design is not None:
            design = _design_from_record(
                snapshot,
                intent,
                persisted_design,
            )
            if results and results[0] != design:
                raise _CandidateRecoveryFailure(
                    snapshot,
                    "designer_reconciliation_mismatch",
                    "The reconciled candidate design changed.",
                )
        else:
            if not results and _session_expired(
                request,
                self._deps.clock,
            ):
                raise _CandidateSessionTimeout(snapshot)
            try:
                design = (
                    results[0]
                    if results
                    else self._deps.designer.invoke(intent)
                )
            except CandidateDesignPending:
                raise _CandidateDesignDeferred(snapshot) from None
            design.require_matches(intent)
            snapshot = self._append(
                request,
                snapshot,
                (
                    _outbox(
                        snapshot,
                        design_success_id,
                        "candidate_design_succeeded",
                        {
                            "base_commit": plan.base_commit,
                            "candidate_id": candidate_id,
                            "complexity": design.complexity,
                            "effect_id": intent.effect_id,
                            "idea_id": design.idea_id,
                            "issue_number": plan.issue_number,
                            "lessons": list(design.lessons),
                            "motivation": design.motivation,
                            "mutation_class": design.mutation_class,
                            "parent_idea_ids": list(
                                design.parent_idea_ids
                            ),
                            "required_opt_ins": sorted(
                                design.required_opt_ins
                            ),
                            "result_id": design.result_id,
                            "slot": slot,
                            "spec_sha256": plan.spec_sha256,
                        },
                    ),
                ),
            )
        changed_paths = self._deps.repository.changed_paths(worktree)
        if _campaign_deadline_reached(
            snapshot,
            plan,
            self._deps.clock,
        ):
            return self._reject_candidate(
                request,
                snapshot,
                plan,
                worktree,
                design,
                changed_paths,
                "deadline_exceeded",
            )
        try:
            _enforce_design(
                plan,
                design,
                changed_paths,
                intent.feedback,
            )
        except ValueError as error:
            return self._reject_candidate(
                request,
                snapshot,
                plan,
                worktree,
                design,
                changed_paths,
                _guardrail_result(error),
            )
        validation = self._deps.validate(worktree.path)
        if not validation.passed:
            return self._reject_candidate(
                request,
                snapshot,
                plan,
                worktree,
                design,
                changed_paths,
                "validation_failed",
            )
        changed_paths = self._deps.repository.changed_paths(worktree)
        try:
            _enforce_paths(plan.edit_paths, changed_paths)
        except ValueError:
            return self._reject_candidate(
                request,
                snapshot,
                plan,
                worktree,
                design,
                changed_paths,
                "forbidden_paths",
            )
        (
            snapshot,
            result_commit,
            result_tree,
            patch_path,
            patch_sha256,
            bundle,
        ) = self._artifact(
            request,
            snapshot,
            plan,
            worktree,
            candidate_id,
            slot,
        )
        draft_intent = _draft_intent(plan, candidate_id, bundle)
        snapshot, draft = self._draft(
            request,
            snapshot,
            draft_intent,
            plan,
        )
        evaluation_intent = _evaluation_intent(
            plan,
            candidate_id,
            draft,
        )
        snapshot, evaluation = self._evaluate(
            request,
            snapshot,
            evaluation_intent,
            plan,
        )
        pareto = select_eligible_candidates(
            baseline,
            (evaluation,),
            plan.evaluation_policy,
        )
        eligible = candidate_id in pareto.eligible_ids
        evidence = self._deps.write_evidence(
            EvidenceRequest(
                output_path=(
                    request.repository_root
                    / plan.evidence_root
                    / plan.campaign_id
                    / candidate_id
                    / "development-evidence.json"
                ),
                campaign_id=plan.campaign_id,
                baseline=baseline,
                candidates=(evaluation,),
                pareto=pareto,
                metric_policies=plan.evaluation_policy,
                source_hash=bundle.sha256,
                goal=plan.goal,
                spec_sha256=plan.spec_sha256,
                assets=plan.assets,
                patch_hashes={candidate_id: patch_sha256},
                result_trees={candidate_id: result_tree},
            )
        )
        attestation = _attestation(
            plan,
            design,
            changed_paths,
            result_commit,
            result_tree,
            patch_path,
            patch_sha256,
            bundle.sha256,
            draft,
            evaluation,
            evidence.sha256,
            (
                plan.evidence_root
                / plan.campaign_id
                / candidate_id
                / "development-evidence.json"
            ),
            eligible,
        )
        event = CampaignEvent(
            event_id=(
                f"candidate-{plan.generation}-{slot}-"
                f"{attestation['attestation_sha256'][:16]}"
            ),
            kind=EventKind.CANDIDATE_EVALUATED,
            generation=plan.generation,
            occurred_at=self._deps.clock.now(),
            payload={
                "candidate_id": candidate_id,
                "eligible": eligible,
                "evidence_sha256": evidence.sha256,
            },
        )
        state = OptimizationCampaign().advance(
            AdvanceRequest(
                plan.issue_number,
                snapshot.state,
                (event,),
            )
        ).state
        attestation_record = _outbox(
            StateRefSnapshot(
                snapshot.revision,
                state,
                snapshot.inbox,
                snapshot.outbox,
            ),
            f"candidate-attestation-{plan.generation}-{slot}",
            "candidate_attestation",
            attestation,
        )
        cleanup_record = _cleanup_record(
            StateRefSnapshot(
                snapshot.revision,
                state,
                snapshot.inbox,
                snapshot.outbox,
            ),
            plan,
            candidate_id,
            slot,
        )
        snapshot = self._ledger.commit(
            request.repository_root,
            issue_number=request.issue_number,
            expected_revision=snapshot.revision,
            state=state,
            inbox=(event,),
            outbox=(attestation_record, cleanup_record),
            objects=_new_state_objects(
                snapshot,
                _candidate_objects(
                    request.repository_root,
                    plan,
                    attestation,
                    patch_path,
                    patch_sha256,
                    evidence.path,
                    evidence.sha256,
                ),
            ),
        )
        return self._reconcile_worktree_cleanups(
            request,
            snapshot,
        )

    def _artifact(
        self,
        request: CandidateWorkerRequest,
        snapshot: StateRefSnapshot,
        plan: CandidateWorkerPlan,
        worktree: CampaignWorktree,
        candidate_id: str,
        slot: int,
    ) -> tuple[
        StateRefSnapshot,
        str,
        str,
        Path,
        str,
        BundleArtifact,
    ]:
        record_id = f"candidate-artifact-{plan.generation}-{slot}"
        existing = _record(snapshot, record_id)
        output = worktree.path / f".foundry-opt-{candidate_id}.zip"
        if existing is not None:
            result_commit = str(existing.payload["result_commit"])
            result_tree = str(existing.payload["tree_sha"])
            patch_path = Path(str(existing.payload["patch_path"]))
            patch_sha256 = str(existing.payload["patch_sha256"])
            bundle_sha256 = str(existing.payload["bundle_sha256"])
            bundle = _restore_bundle(
                output,
                bundle_sha256,
                self._deps.build_bundle,
                worktree.path,
            )
            return (
                snapshot,
                result_commit,
                result_tree,
                patch_path,
                patch_sha256,
                bundle,
            )
        result_commit = self._deps.repository.commit_worktree(
            worktree,
            f"foundry-opt candidate {candidate_id}",
        )
        patch = self._deps.repository.export_patch(
            request.repository_root,
            plan.campaign_id,
            worktree,
            result_commit,
        )
        if patch.result_tree is None:
            raise ValueError("candidate patch is missing its exact result tree")
        bundle = _build_fresh_bundle(
            self._deps.build_bundle,
            worktree.path,
            output,
        )
        patch_content = (
            request.repository_root / patch.path
        ).read_bytes()
        if hashlib.sha256(patch_content).hexdigest() != patch.sha256:
            raise ValueError("candidate patch changed before persistence")
        snapshot = self._append(
            request,
            snapshot,
            (
                _outbox(
                    snapshot,
                    record_id,
                    "candidate_artifact_ready",
                    {
                        "base_commit": plan.base_commit,
                        "bundle_sha256": bundle.sha256,
                        "candidate_id": candidate_id,
                        "issue_number": plan.issue_number,
                        "patch_path": patch.path.as_posix(),
                        "patch_sha256": patch.sha256,
                        "result_commit": result_commit,
                        "slot": slot,
                        "spec_sha256": plan.spec_sha256,
                        "tree_sha": patch.result_tree,
                    },
                ),
            ),
            objects=(
                StateObject(
                    f"objects/patches/{patch.sha256}.patch",
                    patch_content,
                ),
            ),
        )
        return (
            snapshot,
            result_commit,
            patch.result_tree,
            patch.path,
            patch.sha256,
            bundle,
        )

    def _reject_candidate(
        self,
        request: CandidateWorkerRequest,
        snapshot: StateRefSnapshot,
        plan: CandidateWorkerPlan,
        worktree: CampaignWorktree,
        design: CandidateDesignResult,
        changed_paths: tuple[Path, ...],
        result: str,
    ) -> StateRefSnapshot:
        lineage = {
            "changed_paths": [
                path.as_posix() for path in changed_paths
            ],
            "idea_id": design.idea_id,
            "mutation_class": design.mutation_class,
            "parent_idea_ids": list(design.parent_idea_ids),
        }
        document: dict[str, object] = {
            "base_commit": plan.base_commit,
            "candidate_id": design.candidate_id,
            "changed_paths": lineage["changed_paths"],
            "complexity": design.complexity,
            "eligible": False,
            "idea_id": design.idea_id,
            "issue_number": plan.issue_number,
            "lessons": list(design.lessons),
            "lineage_sha256": _sha256(lineage),
            "metrics": {},
            "motivation": design.motivation,
            "mutation_class": design.mutation_class,
            "parent_idea_ids": list(design.parent_idea_ids),
            "reason": result,
            "result": result,
            "slot": design.slot,
            "spec_sha256": plan.spec_sha256,
        }
        document["evidence_sha256"] = _sha256(document)
        document["attestation_sha256"] = _sha256(document)
        event = CampaignEvent(
            event_id=(
                f"candidate-{plan.generation}-{design.slot}-"
                f"{str(document['attestation_sha256'])[:16]}"
            ),
            kind=EventKind.CANDIDATE_EVALUATED,
            generation=plan.generation,
            occurred_at=self._deps.clock.now(),
            payload={
                "candidate_id": design.candidate_id,
                "eligible": False,
                "evidence_sha256": document["evidence_sha256"],
            },
        )
        state = OptimizationCampaign().advance(
            AdvanceRequest(
                plan.issue_number,
                snapshot.state,
                (event,),
            )
        ).state
        record = _outbox(
            StateRefSnapshot(
                snapshot.revision,
                state,
                snapshot.inbox,
                snapshot.outbox,
            ),
            (
                f"candidate-attestation-{plan.generation}-"
                f"{design.slot}"
            ),
            "candidate_attestation",
            document,
        )
        cleanup_record = _cleanup_record(
            StateRefSnapshot(
                snapshot.revision,
                state,
                snapshot.inbox,
                snapshot.outbox,
            ),
            plan,
            design.candidate_id,
            design.slot,
        )
        persisted = self._ledger.commit(
            request.repository_root,
            issue_number=request.issue_number,
            expected_revision=snapshot.revision,
            state=state,
            inbox=(event,),
            outbox=(record, cleanup_record),
        )
        return self._reconcile_worktree_cleanups(
            request,
            persisted,
        )

    def _complete(
        self,
        snapshot: StateRefSnapshot,
        request: CandidateWorkerRequest,
        plan: CandidateWorkerPlan,
        stop_reason: str,
    ) -> StateRefSnapshot:
        event_id = f"candidate-workers-{plan.generation}-completed"
        if (
            event_id in snapshot.state.processed_event_ids
            or any(
                event.kind is EventKind.CANDIDATE_WORKERS_COMPLETED
                and event.generation == plan.generation
                for event in snapshot.inbox
            )
        ):
            return snapshot
        snapshot = self._revise_global_eligibility(
            request,
            snapshot,
            plan,
        )
        event = CampaignEvent(
            event_id=event_id,
            kind=EventKind.CANDIDATE_WORKERS_COMPLETED,
            generation=plan.generation,
            occurred_at=self._deps.clock.now(),
            payload={
                "attempted_count": len(snapshot.state.candidates),
                "eligible_count": sum(
                    1
                    for candidate in snapshot.state.candidates
                    if candidate.eligible
                ),
                "stop_reason": stop_reason,
            },
        )
        state = OptimizationCampaign().advance(
            AdvanceRequest(
                plan.issue_number,
                snapshot.state,
                (event,),
            )
        ).state
        return self._ledger.commit(
            request.repository_root,
            issue_number=request.issue_number,
            expected_revision=snapshot.revision,
            state=state,
            inbox=(event,),
        )

    def _revise_global_eligibility(
        self,
        request: CandidateWorkerRequest,
        snapshot: StateRefSnapshot,
        plan: CandidateWorkerPlan,
    ) -> StateRefSnapshot:
        evaluated_ids = tuple(
            candidate.candidate_id
            for candidate in snapshot.state.candidates
            if any(
                record.kind == "candidate_attestation"
                and record.generation == plan.generation
                and record.payload.get("candidate_id")
                == candidate.candidate_id
                and "evaluation_id" in record.payload
                for record in snapshot.outbox
            )
        )
        if not evaluated_ids:
            return snapshot
        baseline = self._baseline_evaluation(snapshot, plan)
        results = tuple(
            self._candidate_evaluation(
                snapshot,
                plan,
                candidate_id,
            )
            for candidate_id in evaluated_ids
        )
        pareto = select_eligible_candidates(
            baseline,
            results,
            plan.evaluation_policy,
        )
        eligible_ids = set(pareto.eligible_ids)
        events: list[CampaignEvent] = []
        for candidate in snapshot.state.candidates:
            if candidate.candidate_id not in evaluated_ids:
                continue
            eligible = candidate.candidate_id in eligible_ids
            if candidate.eligible == eligible:
                continue
            events.append(
                CampaignEvent(
                    event_id=(
                        f"candidate-revised-{plan.generation}-"
                        f"{candidate.candidate_id}-{int(eligible)}"
                    ),
                    kind=EventKind.CANDIDATE_ELIGIBILITY_REVISED,
                    generation=plan.generation,
                    occurred_at=self._deps.clock.now(),
                    payload={
                        "candidate_id": candidate.candidate_id,
                        "eligible": eligible,
                    },
                )
            )
        if not events:
            return snapshot
        state = OptimizationCampaign().advance(
            AdvanceRequest(
                plan.issue_number,
                snapshot.state,
                tuple(events),
            )
        ).state
        records = tuple(
            _outbox(
                StateRefSnapshot(
                    snapshot.revision,
                    state,
                    snapshot.inbox,
                    snapshot.outbox,
                ),
                f"{event.event_id}-record",
                "candidate_eligibility_revised",
                {
                    "candidate_id": event.payload["candidate_id"],
                    "eligible": event.payload["eligible"],
                    "issue_number": plan.issue_number,
                    "reason": (
                        "pareto_eligible"
                        if event.payload["eligible"]
                        else "dominated"
                    ),
                    "spec_sha256": plan.spec_sha256,
                },
            )
            for event in events
        )
        return self._ledger.commit(
            request.repository_root,
            issue_number=request.issue_number,
            expected_revision=snapshot.revision,
            state=state,
            inbox=tuple(events),
            outbox=records,
        )

    def _candidate_evaluation(
        self,
        snapshot: StateRefSnapshot,
        plan: CandidateWorkerPlan,
        candidate_id: str,
    ) -> EvaluationResult:
        draft_record = next(
            (
                record
                for record in snapshot.outbox
                if (
                    record.generation == plan.generation
                    and record.kind == "candidate_effect_succeeded"
                    and record.payload.get("candidate_id") == candidate_id
                    and record.payload.get("effect_kind") == "foundry_draft"
                )
            ),
            None,
        )
        evaluation_record = next(
            (
                record
                for record in snapshot.outbox
                if (
                    record.generation == plan.generation
                    and record.kind == "candidate_effect_succeeded"
                    and record.payload.get("candidate_id") == candidate_id
                    and record.payload.get("effect_kind")
                    == "foundry_evaluation"
                )
            ),
            None,
        )
        if draft_record is None or evaluation_record is None:
            raise _CandidateRecoveryFailure(
                snapshot,
                "candidate_checkpoint_missing",
                "A candidate evaluation checkpoint is missing.",
            )
        draft = DraftRecord(
            plan.target,
            str(draft_record.payload["draft_id"]),
            plan.base_agent_version,
            str(draft_record.payload["bundle_sha256"]),
            "draft",
        )
        intent = _evaluation_intent(plan, candidate_id, draft)
        result = self._deps.evaluations.reconcile(intent)
        if result is None:
            raise _CandidateRecoveryFailure(
                snapshot,
                "effect_reconciliation_failed",
                "A candidate evaluation could not be reconciled.",
            )
        if (
            evaluation_record.payload.get("evaluation_id")
            != result.run.evaluation_id
            or evaluation_record.payload.get("run_id")
            != result.run.run_id
        ):
            raise _CandidateRecoveryFailure(
                snapshot,
                "effect_reconciliation_mismatch",
                "Candidate evaluation identifiers changed.",
            )
        return result

    def _reserve_worktree(
        self,
        request: CandidateWorkerRequest,
        snapshot: StateRefSnapshot,
        plan: CandidateWorkerPlan,
        candidate_id: str,
        slot: int,
    ) -> CampaignWorktree:
        record_id = (
            f"worktree-{plan.generation}-"
            f"{'baseline' if slot == 0 else slot}"
        )
        if _record(snapshot, record_id) is None:
            snapshot = self._append(
                request,
                snapshot,
                (
                    _outbox(
                        snapshot,
                        record_id,
                        "candidate_worktree_reserved",
                        {
                            "base_commit": plan.base_commit,
                            "branch": (
                                f"foundry-opt/{plan.campaign_id}/"
                                f"{candidate_id}"
                            ),
                            "candidate_id": candidate_id,
                            "issue_number": plan.issue_number,
                            "slot": slot,
                            "spec_sha256": plan.spec_sha256,
                            "work_kind": (
                                "baseline" if slot == 0 else "candidate"
                            ),
                        },
                    ),
                ),
            )
        try:
            return self._deps.repository.open_worktree(
                request.repository_root,
                plan.campaign_id,
                candidate_id,
                plan.base_commit,
            )
        except (KeyError, ValueError):
            return self._deps.repository.reconcile_worktree(
                request.repository_root,
                plan.campaign_id,
                candidate_id,
                plan.base_commit,
            )

    def _draft(
        self,
        request: CandidateWorkerRequest,
        snapshot: StateRefSnapshot,
        intent: CandidateDraftIntent,
        plan: CandidateWorkerPlan,
    ) -> tuple[StateRefSnapshot, DraftRecord]:
        snapshot = self._plan_effect(
            request,
            snapshot,
            intent.effect_id,
            "foundry_draft",
            intent.subject_id,
            0 if intent.subject_id == "baseline" else int(
                intent.subject_id.rsplit("-", 1)[1]
            ),
            plan,
            bundle_sha256=intent.bundle.sha256,
            idempotency_key=intent.idempotency_key,
        )
        success_id = f"{intent.effect_id}-succeeded"
        succeeded = _record(snapshot, success_id)
        try:
            draft = self._deps.drafts.reconcile(intent)
        except CandidateEffectPending:
            raise _CandidateEffectDeferred(
                snapshot,
                "foundry_draft",
            ) from None
        if succeeded is not None:
            if draft is None:
                if (
                    succeeded.payload.get("bundle_sha256")
                    != intent.bundle.sha256
                ):
                    raise ValueError(
                        "persisted draft effect bundle does not match"
                    )
                draft = DraftRecord(
                    agent_name=intent.target,
                    version_id=str(succeeded.payload["draft_id"]),
                    base_version=intent.base_agent_version,
                    sha256=intent.bundle.sha256,
                    status="draft",
                )
            _validate_draft(intent, draft)
            if succeeded.payload.get("draft_id") != draft.version_id:
                raise _CandidateRecoveryFailure(
                    snapshot,
                    "effect_reconciliation_mismatch",
                    "The persisted draft identifier changed.",
                )
            return snapshot, draft
        if draft is None and _session_expired(
            request,
            self._deps.clock,
        ):
            raise _CandidateSessionTimeout(snapshot)
        if draft is None:
            try:
                draft = self._deps.drafts.create(intent)
            except CandidateEffectPending:
                raise _CandidateEffectDeferred(
                    snapshot,
                    "foundry_draft",
                ) from None
        _validate_draft(intent, draft)
        if _record(snapshot, success_id) is None:
            snapshot = self._append(
                request,
                snapshot,
                (
                    _outbox(
                        snapshot,
                        success_id,
                        "candidate_effect_succeeded",
                        {
                            "bundle_sha256": intent.bundle.sha256,
                            "candidate_id": intent.subject_id,
                            "draft_id": draft.version_id,
                            "effect_id": intent.effect_id,
                            "effect_kind": "foundry_draft",
                            "issue_number": intent.issue_number,
                            "spec_sha256": intent.spec_sha256,
                        },
                    ),
                ),
            )
        return snapshot, draft

    def _evaluate(
        self,
        request: CandidateWorkerRequest,
        snapshot: StateRefSnapshot,
        intent: CandidateEvaluationIntent,
        plan: CandidateWorkerPlan,
    ) -> tuple[StateRefSnapshot, EvaluationResult]:
        slot = (
            0
            if intent.subject.subject_id == "baseline"
            else int(intent.subject.subject_id.rsplit("-", 1)[1])
        )
        snapshot = self._plan_effect(
            request,
            snapshot,
            intent.effect_id,
            "foundry_evaluation",
            intent.subject.subject_id,
            slot,
            plan,
        )
        success_id = f"{intent.effect_id}-succeeded"
        succeeded = _record(snapshot, success_id)
        try:
            result = self._deps.evaluations.reconcile(intent)
        except CandidateEffectPending:
            raise _CandidateEffectDeferred(
                snapshot,
                "foundry_evaluation",
            ) from None
        if succeeded is not None:
            if result is None:
                raise _CandidateRecoveryFailure(
                    snapshot,
                    "effect_reconciliation_failed",
                    "A persisted evaluation could not be reconciled.",
                )
            if (
                succeeded.payload.get("evaluation_id")
                != result.run.evaluation_id
                or succeeded.payload.get("run_id") != result.run.run_id
            ):
                raise _CandidateRecoveryFailure(
                    snapshot,
                    "effect_reconciliation_mismatch",
                    "Persisted evaluation identifiers changed.",
                )
            return snapshot, result
        if result is None and _session_expired(
            request,
            self._deps.clock,
        ):
            raise _CandidateSessionTimeout(snapshot)
        if result is None:
            try:
                result = self._deps.evaluations.run(intent)
            except CandidateEffectPending:
                raise _CandidateEffectDeferred(
                    snapshot,
                    "foundry_evaluation",
                ) from None
        if _record(snapshot, success_id) is None:
            snapshot = self._append(
                request,
                snapshot,
                (
                    _outbox(
                        snapshot,
                        success_id,
                        "candidate_effect_succeeded",
                        {
                            "candidate_id": intent.subject.subject_id,
                            "effect_id": intent.effect_id,
                            "effect_kind": "foundry_evaluation",
                            "evaluation_id": result.run.evaluation_id,
                            "issue_number": intent.issue_number,
                            "metrics": _aggregate_metrics(result),
                            "run_id": result.run.run_id,
                            "spec_sha256": intent.spec_sha256,
                        },
                    ),
                ),
            )
        return snapshot, result

    def _plan_effect(
        self,
        request: CandidateWorkerRequest,
        snapshot: StateRefSnapshot,
        effect_id: str,
        effect_kind: str,
        candidate_id: str,
        slot: int,
        plan: CandidateWorkerPlan,
        *,
        bundle_sha256: str | None = None,
        idempotency_key: str | None = None,
    ) -> StateRefSnapshot:
        payload: dict[str, object] = {
            "base_commit": plan.base_commit,
            "candidate_id": candidate_id,
            "effect_id": effect_id,
            "effect_kind": effect_kind,
            "issue_number": plan.issue_number,
            "slot": slot,
            "spec_sha256": plan.spec_sha256,
        }
        if bundle_sha256 is not None:
            payload["bundle_sha256"] = bundle_sha256
        if idempotency_key is not None:
            payload["idempotency_key"] = idempotency_key
        if effect_kind in {"foundry_draft", "foundry_evaluation"}:
            payload["max_attempts"] = plan.limits.transient_retries + 1
        existing = _record(snapshot, effect_id)
        if existing is not None:
            if (
                existing.kind != "candidate_effect_planned"
                or dict(existing.payload) != payload
            ):
                raise _CandidateRecoveryFailure(
                    snapshot,
                    "effect_plan_mismatch",
                    "A persisted effect plan no longer matches its inputs.",
                )
            return snapshot
        return self._append(
            request,
            snapshot,
            (
                _outbox(
                    snapshot,
                    effect_id,
                    "candidate_effect_planned",
                    payload,
                ),
            ),
        )

    def _plan_design_effect(
        self,
        request: CandidateWorkerRequest,
        snapshot: StateRefSnapshot,
        intent: CandidateDesignIntent,
        plan: CandidateWorkerPlan,
        worktree: CampaignWorktree,
    ) -> StateRefSnapshot:
        snapshot = self._plan_effect(
            request,
            snapshot,
            intent.effect_id,
            "candidate_design",
            intent.candidate_id,
            intent.slot,
            plan,
        )
        record_id = f"{intent.effect_id}-worker"
        payload: dict[str, object] = {
            "allowed_mutations": sorted(intent.allowed_mutations),
            "allowed_paths": [
                path.as_posix() for path in intent.edit_paths
            ],
            "base_commit": intent.base_commit,
            "baseline_metrics": dict(intent.baseline_metrics),
            "branch": worktree.branch,
            "candidate_feedback": [
                {
                    "candidate_id": item.candidate_id,
                    "complexity": item.complexity,
                    "eligible": item.eligible,
                    "idea_id": item.idea_id,
                    "lessons": list(item.lessons),
                    "metrics": dict(item.metrics),
                    "result": item.result,
                }
                for item in intent.feedback
            ],
            "candidate_id": intent.candidate_id,
            "effect_id": intent.effect_id,
            "goal": intent.goal,
            "issue_number": intent.issue_number,
            "reason": "candidate_design_pending",
            "restricted_opt_ins": dict(intent.restricted_opt_ins),
            "slot": intent.slot,
            "spec_sha256": intent.spec_sha256,
            "specialist": "foundry-candidate-designer",
            "target": intent.target,
            "work_kind": "design_candidate",
        }
        existing = _record(snapshot, record_id)
        if existing is not None:
            if (
                existing.kind != "specialist_work_request"
                or dict(existing.payload) != payload
            ):
                raise _CandidateRecoveryFailure(
                    snapshot,
                    "designer_intent_mismatch",
                    "The persisted candidate designer intent changed.",
                )
            return snapshot
        return self._append(
            request,
            snapshot,
            (
                _outbox(
                    snapshot,
                    record_id,
                    "specialist_work_request",
                    payload,
                ),
            ),
        )

    def _baseline_evaluation(
        self,
        snapshot: StateRefSnapshot,
        plan: CandidateWorkerPlan,
    ) -> EvaluationResult:
        record = next(
            (
                item
                for item in snapshot.outbox
                if (
                    item.generation == plan.generation
                    and item.kind == "candidate_effect_succeeded"
                    and item.payload.get("candidate_id") == "baseline"
                    and item.payload.get("effect_kind")
                    == "foundry_evaluation"
                )
            ),
            None,
        )
        if record is None:
            raise _CandidateRecoveryFailure(
                snapshot,
                "baseline_checkpoint_missing",
                "The baseline evaluation checkpoint is missing.",
            )
        draft_record = next(
            (
                item
                for item in snapshot.outbox
                if (
                    item.generation == plan.generation
                    and item.kind == "candidate_effect_succeeded"
                    and item.payload.get("candidate_id") == "baseline"
                    and item.payload.get("effect_kind") == "foundry_draft"
                )
            ),
            None,
        )
        if draft_record is None:
            raise _CandidateRecoveryFailure(
                snapshot,
                "baseline_checkpoint_missing",
                "The baseline draft checkpoint is missing.",
            )
        draft = DraftRecord(
            plan.target,
            str(draft_record.payload["draft_id"]),
            plan.base_agent_version,
            str(draft_record.payload["bundle_sha256"]),
            "draft",
        )
        intent = _evaluation_intent(plan, "baseline", draft)
        result = self._deps.evaluations.reconcile(intent)
        if result is None:
            raise _CandidateRecoveryFailure(
                snapshot,
                "effect_reconciliation_failed",
                "The persisted baseline evaluation could not be reconciled.",
            )
        if (
            record.payload.get("evaluation_id")
            != result.run.evaluation_id
            or record.payload.get("run_id") != result.run.run_id
        ):
            raise _CandidateRecoveryFailure(
                snapshot,
                "effect_reconciliation_mismatch",
                "The persisted baseline evaluation identifiers changed.",
            )
        return result

    def _append(
        self,
        request: CandidateWorkerRequest,
        snapshot: StateRefSnapshot,
        outbox: tuple[OutboxRecord, ...],
        *,
        objects: tuple[StateObject, ...] = (),
    ) -> StateRefSnapshot:
        return self._ledger.commit(
            request.repository_root,
            issue_number=request.issue_number,
            expected_revision=snapshot.revision,
            state=snapshot.state,
            outbox=outbox,
            objects=objects,
        )

    def _reconcile_worktree_cleanups(
        self,
        request: CandidateWorkerRequest,
        snapshot: StateRefSnapshot,
    ) -> StateRefSnapshot:
        planned = tuple(
            record
            for record in snapshot.outbox
            if record.kind == "candidate_worktree_cleanup_planned"
        )
        for record in planned:
            success_id = f"{record.record_id}-succeeded"
            succeeded = _record(snapshot, success_id)
            if succeeded is not None:
                if (
                    succeeded.kind
                    != "candidate_worktree_cleanup_succeeded"
                    or dict(succeeded.payload) != dict(record.payload)
                ):
                    raise _CandidateRecoveryFailure(
                        snapshot,
                        "worktree_cleanup_binding_invalid",
                        "A persisted worktree cleanup acknowledgement is invalid.",
                    )
                continue
            try:
                worktree = _cleanup_worktree(
                    request,
                    record,
                )
            except (TypeError, ValueError) as error:
                raise _CandidateRecoveryFailure(
                    snapshot,
                    "worktree_cleanup_binding_invalid",
                    "A persisted worktree cleanup binding is invalid.",
                ) from error
            try:
                self._deps.repository.cleanup_worktree(
                    request.repository_root,
                    worktree,
                )
            except Exception as error:
                raise _CandidateRecoveryFailure(
                    snapshot,
                    "worktree_cleanup_failed",
                    "A terminal candidate worktree still requires cleanup.",
                ) from error
            snapshot = self._append(
                request,
                snapshot,
                (
                    _outbox(
                        snapshot,
                        success_id,
                        "candidate_worktree_cleanup_succeeded",
                        record.payload,
                    ),
                ),
            )
        return snapshot


def _plan_mismatch(
    plan: CandidateWorkerPlan,
    snapshot: StateRefSnapshot,
    issue_number: int,
) -> str | None:
    state = snapshot.state
    if plan.issue_number != issue_number or state.issue_number != issue_number:
        return "candidate_issue_stale"
    if plan.generation != state.generation:
        return "candidate_generation_stale"
    if plan.spec_sha256 != state.spec_sha256:
        return "candidate_spec_stale"
    started = tuple(
        record
        for record in snapshot.outbox
        if (
            record.generation == state.generation
            and record.kind == "candidate_campaign_started"
        )
    )
    if len(started) > 1:
        return "candidate_binding_conflict"
    if started:
        payload = started[0].payload
        if payload.get("base_commit") != plan.base_commit:
            return "candidate_base_stale"
        if payload.get("spec_sha256") != plan.spec_sha256:
            return "candidate_spec_stale"
        if payload.get("campaign_id") != plan.campaign_id:
            return "candidate_binding_stale"
        if payload.get("target") != plan.target:
            return "candidate_target_stale"
        if payload.get("goal_sha256") != plan.goal_sha256:
            return "candidate_goal_stale"
        if (
            payload.get("max_changed_candidates")
            != plan.limits.max_changed_candidates
        ):
            return "candidate_budget_stale"
    return None


def _session_expired(
    request: CandidateWorkerRequest,
    clock: Clock,
) -> bool:
    return (
        request.session_deadline is not None
        and clock.now() >= request.session_deadline
    )


def _budget_stop_reason(
    snapshot: StateRefSnapshot,
    plan: CandidateWorkerPlan,
    clock: Clock,
) -> str | None:
    started = next(
        (
            record
            for record in snapshot.outbox
            if (
                record.generation == plan.generation
                and record.kind == "candidate_campaign_started"
            )
        ),
        None,
    )
    if started is None:
        raise ValueError("candidate campaign start checkpoint is missing")
    try:
        cutoff_at = datetime.fromisoformat(
            str(started.payload["cutoff_at"])
        )
        deadline_at = datetime.fromisoformat(
            str(started.payload["deadline_at"])
        )
    except (KeyError, ValueError) as error:
        raise ValueError("candidate campaign timing is invalid") from error
    now = clock.now()
    if now >= deadline_at:
        return "campaign_deadline"
    if now >= cutoff_at:
        return "candidate_cutoff"
    return None


def _campaign_deadline_reached(
    snapshot: StateRefSnapshot,
    plan: CandidateWorkerPlan,
    clock: Clock,
) -> bool:
    return (
        _budget_stop_reason(snapshot, plan, clock)
        == "campaign_deadline"
    )


def _binding(plan: CandidateWorkerPlan) -> str:
    return hashlib.sha256(
        (
            f"{plan.issue_number}:{plan.generation}:"
            f"{plan.spec_sha256}:{plan.base_commit}"
        ).encode("ascii")
    ).hexdigest()


def _record(
    snapshot: StateRefSnapshot,
    record_id: str,
) -> OutboxRecord | None:
    return next(
        (
            record
            for record in snapshot.outbox
            if record.record_id == record_id
        ),
        None,
    )


def _outbox(
    snapshot: StateRefSnapshot,
    record_id: str,
    kind: str,
    payload: Mapping[str, object],
) -> OutboxRecord:
    return OutboxRecord(
        record_id,
        kind,
        snapshot.state.generation,
        snapshot.state.sequence,
        payload,
    )


def _cleanup_record(
    snapshot: StateRefSnapshot,
    plan: CandidateWorkerPlan,
    candidate_id: str,
    slot: int,
) -> OutboxRecord:
    record_id = (
        f"worktree-cleanup-{plan.generation}-"
        f"{'baseline' if slot == 0 else slot}"
    )
    return _outbox(
        snapshot,
        record_id,
        "candidate_worktree_cleanup_planned",
        {
            "base_commit": plan.base_commit,
            "branch": (
                f"foundry-opt/{plan.campaign_id}/{candidate_id}"
            ),
            "campaign_id": plan.campaign_id,
            "candidate_id": candidate_id,
            "effect_id": record_id,
            "effect_kind": "worktree_cleanup",
            "issue_number": plan.issue_number,
            "slot": slot,
            "spec_sha256": plan.spec_sha256,
            "work_kind": "baseline" if slot == 0 else "candidate",
        },
    )


def _cleanup_worktree(
    request: CandidateWorkerRequest,
    record: OutboxRecord,
) -> CampaignWorktree:
    payload = record.payload
    issue_number = payload.get("issue_number")
    slot = payload.get("slot")
    base_commit = payload.get("base_commit")
    spec_sha256 = payload.get("spec_sha256")
    candidate_id = payload.get("candidate_id")
    campaign_id = payload.get("campaign_id")
    branch = payload.get("branch")
    expected_record_id = (
        f"worktree-cleanup-{record.generation}-"
        f"{'baseline' if slot == 0 else slot}"
    )
    expected_candidate_id = (
        "baseline"
        if slot == 0
        else f"candidate-{slot}"
    )
    expected_work_kind = "baseline" if slot == 0 else "candidate"
    if (
        type(issue_number) is not int
        or issue_number != request.issue_number
        or type(slot) is not int
        or slot < 0
        or not isinstance(base_commit, str)
        or not _COMMIT.fullmatch(base_commit)
        or not isinstance(spec_sha256, str)
        or not _SHA256.fullmatch(spec_sha256)
        or candidate_id != expected_candidate_id
        or record.record_id != expected_record_id
        or payload.get("effect_id") != record.record_id
        or payload.get("effect_kind") != "worktree_cleanup"
        or payload.get("work_kind") != expected_work_kind
    ):
        raise ValueError("worktree cleanup checkpoint is invalid")
    expected_campaign_id = (
        f"issue-{issue_number}-g{record.generation}-"
        f"{spec_sha256[:8]}-{base_commit[:8]}"
    )
    expected_branch = (
        f"foundry-opt/{expected_campaign_id}/{candidate_id}"
    )
    if campaign_id != expected_campaign_id or branch != expected_branch:
        raise ValueError("worktree cleanup generation binding is invalid")
    path = (
        request.repository_root.expanduser().resolve()
        / ".foundry-optimizer"
        / "worktrees"
        / expected_campaign_id
        / expected_candidate_id
    )
    return CampaignWorktree(
        expected_candidate_id,
        path,
        expected_branch,
        base_commit,
    )


def _time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("candidate worker timestamps must be timezone-aware")
    return value.isoformat()


def _draft_intent(
    plan: CandidateWorkerPlan,
    subject_id: str,
    bundle: BundleArtifact,
) -> CandidateDraftIntent:
    effect_id = (
        f"draft-{plan.issue_number}-{plan.generation}-{subject_id}"
    )
    idempotency_key = hashlib.sha256(
        (
            f"{plan.campaign_id}:{subject_id}:{plan.base_commit}:"
            f"{bundle.sha256}"
        ).encode("ascii")
    ).hexdigest()
    return CandidateDraftIntent(
        effect_id,
        plan.issue_number,
        plan.generation,
        plan.spec_sha256,
        plan.base_commit,
        plan.target,
        subject_id,
        plan.base_agent_version,
        idempotency_key,
        bundle,
    )


def _evaluation_intent(
    plan: CandidateWorkerPlan,
    subject_id: str,
    draft: DraftRecord,
) -> CandidateEvaluationIntent:
    return CandidateEvaluationIntent(
        f"evaluation-{plan.issue_number}-{plan.generation}-{subject_id}",
        plan.issue_number,
        plan.generation,
        plan.spec_sha256,
        plan.base_commit,
        EvaluationSubject(
            subject_id,
            AgentVersionRef(plan.target, draft.version_id, draft.version_id),
        ),
        DatasetSplit.DEVELOPMENT,
        plan.evaluation_policy,
    )


def _validate_draft(
    intent: CandidateDraftIntent,
    draft: DraftRecord,
) -> None:
    if (
        not draft.version_id.startswith("draft-")
        or draft.sha256 != intent.bundle.sha256
        or draft.base_version != intent.base_agent_version
    ):
        raise ValueError("Foundry draft does not match the planned effect")


def _restore_bundle(
    output: Path,
    expected_sha256: str,
    build_bundle: BundleBuilder,
    worktree: Path,
) -> BundleArtifact:
    root = worktree.resolve()
    if not output.parent.resolve().is_relative_to(root):
        raise ValueError("candidate bundle path escapes the worktree")
    if output.is_symlink():
        raise ValueError("candidate bundle must not be a symlink")
    if output.is_file():
        content = output.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest != expected_sha256:
            raise ValueError("persisted candidate bundle hash changed")
        return BundleArtifact(
            output,
            digest,
            (),
            (),
            len(content),
            output.with_name(f"{output.name}.manifest.json"),
        )
    rebuilt = build_bundle(worktree, output)
    if rebuilt.sha256 != expected_sha256:
        raise ValueError("rebuilt candidate bundle hash changed")
    return rebuilt


def _build_fresh_bundle(
    build_bundle: BundleBuilder,
    worktree: Path,
    output: Path,
) -> BundleArtifact:
    candidates = {
        output,
        output.with_name(f"{output.name}.manifest.json"),
        output.with_suffix(".manifest.json"),
        output.with_suffix(f"{output.suffix}.partial"),
    }
    root = worktree.resolve()
    for path in candidates:
        if not path.parent.resolve().is_relative_to(root):
            raise ValueError("bundle output escapes the managed worktree")
        if not os.path.lexists(path):
            continue
        if path.is_dir() and not path.is_symlink():
            raise ValueError("bundle output path is a directory")
        path.unlink()
    return build_bundle(worktree, output)


def _aggregate_metrics(result: EvaluationResult) -> dict[str, float]:
    return {
        name: aggregate.median
        for name, aggregate in result.metrics.items()
        if aggregate.median is not None
    }


def _feedback(
    snapshot: StateRefSnapshot,
    plan: CandidateWorkerPlan,
    slot: int,
) -> tuple[CandidateIterationFeedback, ...]:
    records = sorted(
        (
            record
            for record in snapshot.outbox
            if (
                record.generation == plan.generation
                and record.kind == "candidate_attestation"
                and type(record.payload.get("slot")) is int
                and int(record.payload["slot"]) < slot
            )
        ),
        key=lambda record: int(record.payload["slot"]),
    )
    return tuple(
        CandidateIterationFeedback(
            candidate_id=str(record.payload["candidate_id"]),
            idea_id=str(record.payload["idea_id"]),
            result=str(record.payload["result"]),
            metrics=dict(record.payload["metrics"]),
            eligible=bool(record.payload["eligible"]),
            lessons=tuple(record.payload["lessons"]),
            complexity=str(record.payload["complexity"]),
        )
        for record in records
    )


def _matching_design_results(
    intent: CandidateDesignIntent,
    results: tuple[CandidateDesignResult, ...],
) -> tuple[CandidateDesignResult, ...]:
    matching: dict[str, CandidateDesignResult] = {}
    for result in results:
        try:
            result.require_matches(intent)
        except ValueError:
            continue
        existing = matching.get(result.result_id)
        if existing is not None and existing != result:
            raise ValueError(
                "candidate designer reused a result ID with different data"
            )
        matching[result.result_id] = result
    if len(matching) > 1:
        raise ValueError("candidate designer returned conflicting results")
    return tuple(matching.values())


def _design_from_record(
    snapshot: StateRefSnapshot,
    intent: CandidateDesignIntent,
    record: OutboxRecord,
) -> CandidateDesignResult:
    if (
        record.kind != "candidate_design_succeeded"
        or record.generation != intent.generation
        or record.payload.get("effect_id") != intent.effect_id
    ):
        raise _CandidateRecoveryFailure(
            snapshot,
            "designer_reconciliation_mismatch",
            "The persisted candidate design binding changed.",
        )
    try:
        result = CandidateDesignResult(
            effect_id=str(record.payload["effect_id"]),
            result_id=str(record.payload["result_id"]),
            issue_number=int(record.payload["issue_number"]),
            generation=record.generation,
            spec_sha256=str(record.payload["spec_sha256"]),
            base_commit=str(record.payload["base_commit"]),
            candidate_id=str(record.payload["candidate_id"]),
            slot=int(record.payload["slot"]),
            idea_id=str(record.payload["idea_id"]),
            mutation_class=str(record.payload["mutation_class"]),
            parent_idea_ids=tuple(record.payload["parent_idea_ids"]),
            required_opt_ins=frozenset(
                record.payload["required_opt_ins"]
            ),
            motivation=str(record.payload["motivation"]),
            lessons=tuple(record.payload["lessons"]),
            complexity=str(record.payload["complexity"]),
        )
        result.require_matches(intent)
    except (KeyError, TypeError, ValueError) as error:
        raise _CandidateRecoveryFailure(
            snapshot,
            "designer_reconciliation_mismatch",
            "The persisted candidate design is invalid.",
        ) from error
    return result


def _candidate_design_intent(
    repository_root: Path,
    record: OutboxRecord,
) -> CandidateDesignIntent:
    try:
        feedback = tuple(
            CandidateIterationFeedback(
                candidate_id=str(item["candidate_id"]),
                idea_id=str(item["idea_id"]),
                result=str(item["result"]),
                metrics=dict(item["metrics"]),
                eligible=bool(item["eligible"]),
                lessons=tuple(item["lessons"]),
                complexity=str(item["complexity"]),
            )
            for item in record.payload["candidate_feedback"]
        )
        return CandidateDesignIntent(
            effect_id=str(record.payload["effect_id"]),
            issue_number=int(record.payload["issue_number"]),
            generation=record.generation,
            spec_sha256=str(record.payload["spec_sha256"]),
            base_commit=str(record.payload["base_commit"]),
            target=str(record.payload["target"]),
            candidate_id=str(record.payload["candidate_id"]),
            slot=int(record.payload["slot"]),
            worktree=repository_root.expanduser().resolve(),
            goal=str(record.payload["goal"]),
            edit_paths=tuple(
                Path(path) for path in record.payload["allowed_paths"]
            ),
            allowed_mutations=frozenset(
                record.payload["allowed_mutations"]
            ),
            restricted_opt_ins=dict(
                record.payload["restricted_opt_ins"]
            ),
            baseline_metrics=dict(record.payload["baseline_metrics"]),
            feedback=feedback,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("candidate design intent is invalid") from error


def _candidate_design_result(path: Path) -> CandidateDesignResult:
    document = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "base_commit",
        "candidate_id",
        "complexity",
        "effect_id",
        "generation",
        "idea_id",
        "issue_number",
        "lessons",
        "motivation",
        "mutation_class",
        "parent_idea_ids",
        "required_opt_ins",
        "result_id",
        "slot",
        "spec_sha256",
    }
    if not isinstance(document, dict) or set(document) != expected:
        raise ValueError("candidate design result fields are invalid")
    return CandidateDesignResult(
        effect_id=document["effect_id"],
        result_id=document["result_id"],
        issue_number=document["issue_number"],
        generation=document["generation"],
        spec_sha256=document["spec_sha256"],
        base_commit=document["base_commit"],
        candidate_id=document["candidate_id"],
        slot=document["slot"],
        idea_id=document["idea_id"],
        mutation_class=document["mutation_class"],
        parent_idea_ids=tuple(document["parent_idea_ids"]),
        required_opt_ins=frozenset(document["required_opt_ins"]),
        motivation=document["motivation"],
        lessons=tuple(document["lessons"]),
        complexity=document["complexity"],
    )


def _candidate_design_submission_record(
    snapshot: StateRefSnapshot,
    record_id: str,
    result: CandidateDesignResult,
    artifact: CandidateDesignArtifact,
    worker_issue_number: int,
) -> OutboxRecord:
    return _outbox(
        snapshot,
        record_id,
        "candidate_design_submitted",
        {
            "base_commit": result.base_commit,
            "candidate_id": result.candidate_id,
            "changed_paths": [
                path.as_posix() for path in artifact.changed_paths
            ],
            "complexity": result.complexity,
            "effect_id": result.effect_id,
            "head_commit": artifact.head_commit,
            "idea_id": result.idea_id,
            "issue_number": result.issue_number,
            "lessons": list(result.lessons),
            "motivation": result.motivation,
            "mutation_class": result.mutation_class,
            "parent_idea_ids": list(result.parent_idea_ids),
            "ref": artifact.ref,
            "required_opt_ins": sorted(result.required_opt_ins),
            "result_id": result.result_id,
            "slot": result.slot,
            "spec_sha256": result.spec_sha256,
            "tree_sha": artifact.tree_sha,
            "worker_issue_number": worker_issue_number,
        },
    )


def _submitted_design_matches(
    record: OutboxRecord,
    result: CandidateDesignResult,
    worker_issue_number: int,
) -> bool:
    return (
        record.kind == "candidate_design_submitted"
        and record.generation == result.generation
        and record.payload.get("effect_id") == result.effect_id
        and record.payload.get("result_id") == result.result_id
        and record.payload.get("issue_number") == result.issue_number
        and record.payload.get("spec_sha256") == result.spec_sha256
        and record.payload.get("base_commit") == result.base_commit
        and record.payload.get("candidate_id") == result.candidate_id
        and record.payload.get("slot") == result.slot
        and record.payload.get("idea_id") == result.idea_id
        and record.payload.get("mutation_class") == result.mutation_class
        and tuple(record.payload.get("parent_idea_ids", ()))
        == result.parent_idea_ids
        and frozenset(record.payload.get("required_opt_ins", ()))
        == result.required_opt_ins
        and record.payload.get("motivation") == result.motivation
        and tuple(record.payload.get("lessons", ())) == result.lessons
        and record.payload.get("complexity") == result.complexity
        and record.payload.get("worker_issue_number")
        == worker_issue_number
    )


def _enforce_design(
    plan: CandidateWorkerPlan,
    design: CandidateDesignResult,
    changed_paths: tuple[Path, ...],
    feedback: tuple[CandidateIterationFeedback, ...],
) -> None:
    known_parents = {item.idea_id for item in feedback}
    if not set(design.parent_idea_ids).issubset(known_parents):
        raise ValueError("candidate lineage references an unknown parent")
    if design.mutation_class not in plan.allowed_mutations:
        raise ValueError("candidate mutation class is not allowed")
    missing = tuple(
        opt_in
        for opt_in in design.required_opt_ins
        if not plan.restricted_opt_ins.get(opt_in, False)
    )
    if missing:
        raise ValueError("candidate requires a disabled restricted opt-in")
    if not changed_paths:
        raise ValueError("candidate did not change any files")
    _enforce_paths(plan.edit_paths, changed_paths)


def _enforce_paths(
    allowed_roots: tuple[Path, ...],
    changed_paths: tuple[Path, ...],
) -> None:
    for changed in changed_paths:
        normalized = Path(str(changed).replace("\\", "/"))
        if not any(
            normalized == root or normalized.is_relative_to(root)
            for root in allowed_roots
        ):
            raise ValueError(f"candidate changed disallowed path: {changed}")


def _guardrail_result(error: ValueError) -> str:
    if "unknown parent" in str(error):
        return "invalid_lineage"
    if "did not change" in str(error):
        return "unchanged"
    if "disallowed path" in str(error):
        return "forbidden_paths"
    return "forbidden_mutation"


def _attestation(
    plan: CandidateWorkerPlan,
    design: CandidateDesignResult,
    changed_paths: tuple[Path, ...],
    result_commit: str,
    result_tree: str,
    patch_path: Path,
    patch_sha256: str,
    bundle_sha256: str,
    draft: DraftRecord,
    evaluation: EvaluationResult,
    evidence_sha256: str,
    evidence_path: Path,
    eligible: bool,
) -> dict[str, object]:
    lineage = {
        "idea_id": design.idea_id,
        "mutation_class": design.mutation_class,
        "parent_idea_ids": list(design.parent_idea_ids),
        "changed_paths": [
            path.as_posix() for path in changed_paths
        ],
    }
    lineage_sha256 = _sha256(lineage)
    document: dict[str, object] = {
        "base_commit": plan.base_commit,
        "allowed_paths": [
            path.as_posix() for path in plan.edit_paths
        ],
        "bundle_sha256": bundle_sha256,
        "candidate_id": design.candidate_id,
        "changed_paths": lineage["changed_paths"],
        "complexity": design.complexity,
        "draft_id": draft.version_id,
        "eligible": eligible,
        "evaluation_id": evaluation.run.evaluation_id,
        "evidence_path": evidence_path.as_posix(),
        "evidence_sha256": evidence_sha256,
        "idea_id": design.idea_id,
        "issue_number": plan.issue_number,
        "lessons": list(design.lessons),
        "lineage_sha256": lineage_sha256,
        "metrics": _aggregate_metrics(evaluation),
        "motivation": design.motivation,
        "mutation_class": design.mutation_class,
        "parent_idea_ids": list(design.parent_idea_ids),
        "patch_sha256": patch_sha256,
        "patch_path": patch_path.as_posix(),
        "result": "eligible" if eligible else "ineligible",
        "result_commit": result_commit,
        "run_id": evaluation.run.run_id,
        "slot": design.slot,
        "spec_sha256": plan.spec_sha256,
        "tree_sha": result_tree,
    }
    document["attestation_sha256"] = _sha256(document)
    return document


def _sha256(document: Mapping[str, object]) -> str:
    serialized = json.dumps(
        document,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _candidate_objects(
    repository_root: Path,
    plan: CandidateWorkerPlan,
    attestation: Mapping[str, object],
    patch_path: Path,
    patch_sha256: str,
    evidence_path: Path,
    evidence_sha256: str,
) -> tuple[StateObject, ...]:
    objects: list[StateObject] = [
        StateObject(
            (
                f"objects/candidates/g{plan.generation}-"
                f"{attestation['candidate_id']}.json"
            ),
            (
                json.dumps(
                    dict(attestation),
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8"),
        )
    ]
    root = repository_root.resolve()
    for kind, path, expected_sha256, suffix in (
        ("evidence", evidence_path, evidence_sha256, ".json"),
        ("patches", root / patch_path, patch_sha256, ".patch"),
    ):
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError:
            continue
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ValueError("candidate durable object path is unsafe")
        content = resolved.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected_sha256:
            raise ValueError("candidate durable object hash changed")
        objects.append(
            StateObject(
                f"objects/{kind}/{expected_sha256}{suffix}",
                content,
            )
        )
    return tuple(objects)


def _new_state_objects(
    snapshot: StateRefSnapshot,
    candidates: tuple[StateObject, ...],
) -> tuple[StateObject, ...]:
    known = {item.path: item for item in snapshot.objects}
    additions: dict[str, StateObject] = {}
    for item in candidates:
        existing = known.get(item.path) or additions.get(item.path)
        if existing is not None:
            if existing.content != item.content:
                raise ValueError("candidate state object content changed")
            continue
        additions[item.path] = item
    return tuple(
        additions[path] for path in sorted(additions)
    )
