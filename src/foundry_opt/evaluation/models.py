from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from math import isfinite
from typing import TypeAlias


ScalarScore: TypeAlias = bool | int | float | str | None


class DatasetSplit(StrEnum):
    DEVELOPMENT = "development"
    VALIDATION = "validation"


class EvaluationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            EvaluationStatus.COMPLETED,
            EvaluationStatus.PARTIAL,
            EvaluationStatus.FAILED,
            EvaluationStatus.CANCELLED,
        }


class Outcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNDEFINED = "undefined"


class MetricDirection(StrEnum):
    MAXIMIZE = "maximize"
    MINIMIZE = "minimize"


class UndefinedBehavior(StrEnum):
    FAIL = "fail"
    IGNORE = "ignore"


@dataclass(frozen=True)
class AgentVersionRef:
    agent_id: str
    draft_id: str
    version: str


@dataclass(frozen=True)
class DatasetVersionRef:
    dataset_id: str
    version: str


@dataclass(frozen=True)
class EvaluatorDefinitionRef:
    definition_id: str
    version: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
        )


@dataclass(frozen=True)
class ToolCallMetadata:
    call_id: str
    name: str
    status: str
    duration_ms: int | None = None


@dataclass(frozen=True)
class TrajectoryMetadata:
    trajectory_id: str
    turn_count: int
    tool_calls: tuple[ToolCallMetadata, ...]


@dataclass(frozen=True)
class EvaluationScore:
    metric: str
    raw_score: ScalarScore
    normalized_score: float | None
    reason: str | None = None

    def __post_init__(self) -> None:
        _validate_optional_finite(
            self.normalized_score,
            "Normalized evaluation score",
        )


@dataclass(frozen=True)
class EvaluationItem:
    case_id: str
    case_hash: str
    response_ids: tuple[str, ...]
    scores: tuple[EvaluationScore, ...]
    usage: Usage
    trajectory: TrajectoryMetadata | None = None
    error: str | None = None
    duration_ms: int = 0


@dataclass(frozen=True)
class EvaluationRun:
    run_id: str
    evaluation_id: str
    subject_id: str
    split: DatasetSplit
    agent: AgentVersionRef
    dataset: DatasetVersionRef
    evaluator: EvaluatorDefinitionRef
    status: EvaluationStatus
    portal_url: str | None
    started_at: datetime | None
    completed_at: datetime | None
    error: str | None


@dataclass(frozen=True)
class MetricPolicy:
    name: str
    direction: MetricDirection
    threshold: float
    materiality: float
    hard_guardrail: bool = False
    undefined_behavior: UndefinedBehavior = UndefinedBehavior.FAIL

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Metric policy name is required.")
        if not isfinite(self.threshold):
            raise ValueError("Metric threshold must be finite.")
        if not isfinite(self.materiality) or self.materiality < 0:
            raise ValueError("Metric materiality must be finite and non-negative.")

    def passes(self, value: float) -> bool:
        if self.direction is MetricDirection.MAXIMIZE:
            return value >= self.threshold
        return value <= self.threshold

    def improvement(self, baseline: float, candidate: float) -> float:
        if self.direction is MetricDirection.MAXIMIZE:
            return candidate - baseline
        return baseline - candidate


@dataclass(frozen=True)
class EvaluationPolicy:
    metrics: tuple[MetricPolicy, ...]
    noisy_spread: float = 0.0
    borderline_distance: float = 0.0

    def __post_init__(self) -> None:
        names = tuple(metric.name for metric in self.metrics)
        if not self.metrics or len(names) != len(set(names)):
            raise ValueError("Metric policy names must be non-empty and unique.")
        if self.noisy_spread < 0 or self.borderline_distance < 0:
            raise ValueError("Repeat policy bounds cannot be negative.")

    def metric(self, name: str) -> MetricPolicy:
        for metric in self.metrics:
            if metric.name == name:
                return metric
        raise KeyError(name)


@dataclass(frozen=True)
class NormalizedCaseMetric:
    metric: str
    raw_score: ScalarScore
    normalized_score: float | None
    reason: str | None
    outcome: Outcome

    def __post_init__(self) -> None:
        _validate_optional_finite(
            self.normalized_score,
            "Normalized case score",
        )


@dataclass(frozen=True)
class NormalizedCase:
    case_id: str
    case_hash: str
    response_ids: tuple[str, ...]
    scores: tuple[NormalizedCaseMetric, ...]
    usage: Usage
    trajectory: TrajectoryMetadata | None
    error: str | None
    duration_ms: int


@dataclass(frozen=True)
class MetricAggregate:
    metric: str
    median: float | None
    minimum: float | None
    maximum: float | None
    spread: float | None
    outcome: Outcome
    sample_count: int

    def __post_init__(self) -> None:
        for name, value in (
            ("median", self.median),
            ("minimum", self.minimum),
            ("maximum", self.maximum),
            ("spread", self.spread),
        ):
            _validate_optional_finite(value, f"Metric aggregate {name}")


@dataclass(frozen=True)
class EvaluationResult:
    run: EvaluationRun
    cases: tuple[NormalizedCase, ...]
    metrics: dict[str, MetricAggregate]
    usage: Usage
    duration_ms: int
    errors: tuple[str, ...]
    complete: bool
    needs_repeat: bool
    attempts: int
    attempt_runs: tuple[EvaluationRun, ...] = field(default_factory=tuple)

    @property
    def response_ids(self) -> tuple[str, ...]:
        return tuple(
            response_id
            for case in self.cases
            for response_id in case.response_ids
        )

    @property
    def all_runs(self) -> tuple[EvaluationRun, ...]:
        return self.attempt_runs or (self.run,)

    def with_case_reason(
        self,
        *,
        case_id: str,
        case_hash: str,
        response_ids: tuple[str, ...],
        reason: str,
        scores: dict[str, tuple[ScalarScore, float | None, str]],
        duration_ms: int,
    ) -> "EvaluationResult":
        case_scores = tuple(
            NormalizedCaseMetric(
                metric=name,
                raw_score=raw,
                normalized_score=normalized,
                reason=reason,
                outcome=Outcome(outcome),
            )
            for name, (raw, normalized, outcome) in scores.items()
        )
        case = NormalizedCase(
            case_id=case_id,
            case_hash=case_hash,
            response_ids=response_ids,
            scores=case_scores,
            usage=Usage(),
            trajectory=None,
            error=None,
            duration_ms=duration_ms,
        )
        return replace(self, cases=(*self.cases, case))

    def with_portal_url(self, portal_url: str) -> "EvaluationResult":
        return replace(self, run=replace(self.run, portal_url=portal_url))


@dataclass(frozen=True)
class CandidateDecision:
    subject_id: str
    eligible: bool
    reason: str


@dataclass(frozen=True)
class ParetoResult:
    decisions: tuple[CandidateDecision, ...]
    frontier_ids: tuple[str, ...]
    eligible_ids: tuple[str, ...]

    def decision_for(self, subject_id: str) -> CandidateDecision:
        for decision in self.decisions:
            if decision.subject_id == subject_id:
                return decision
        raise KeyError(subject_id)


@dataclass(frozen=True)
class EvaluationSubject:
    subject_id: str
    agent: AgentVersionRef | None = None


@dataclass(frozen=True)
class EvaluationFunnelRequest:
    baseline: EvaluationSubject
    candidates: tuple[EvaluationSubject, ...]
    policy: EvaluationPolicy

    def __post_init__(self) -> None:
        subject_ids = (
            self.baseline.subject_id,
            *(candidate.subject_id for candidate in self.candidates),
        )
        if len(subject_ids) != len(set(subject_ids)):
            raise ValueError("Evaluation funnel subject IDs must be unique.")


@dataclass(frozen=True)
class FunnelStageResult:
    results: dict[str, EvaluationResult]
    pareto: ParetoResult


@dataclass(frozen=True)
class FunnelResult:
    development: FunnelStageResult
    validation: FunnelStageResult


def _validate_optional_finite(value: float | None, label: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number.")
    if not isfinite(value):
        raise ValueError(f"{label} must be finite.")
