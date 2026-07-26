from collections.abc import Sequence

from foundry_opt.evaluation.models import (
    CandidateDecision,
    EvaluationPolicy,
    EvaluationResult,
    MetricPolicy,
    Outcome,
    ParetoResult,
    UndefinedBehavior,
)
from foundry_opt.evaluation.normalization import _coerce_policy


def select_eligible_candidates(
    baseline: EvaluationResult,
    candidates: Sequence[EvaluationResult],
    metric_policies: EvaluationPolicy | Sequence[MetricPolicy],
) -> ParetoResult:
    policy = _coerce_policy(metric_policies)
    initial_rejections: dict[str, str] = {}
    contenders: list[EvaluationResult] = []

    for candidate in candidates:
        if not candidate.complete:
            initial_rejections[candidate.run.subject_id] = (
                "Candidate evaluation remained incomplete after its repeat."
            )
            continue
        undefined = _undefined_metrics(candidate, policy)
        if undefined:
            initial_rejections[candidate.run.subject_id] = (
                f"Required metric is undefined: {', '.join(undefined)}."
            )
            continue
        failed_guardrails = tuple(
            metric.name
            for metric in policy.metrics
            if metric.hard_guardrail
            and metric.undefined_behavior is not UndefinedBehavior.IGNORE
            and (
                metric.name not in candidate.metrics
                or candidate.metrics[metric.name].outcome is not Outcome.PASS
            )
        )
        if failed_guardrails:
            initial_rejections[candidate.run.subject_id] = (
                "Candidate failed a hard guardrail: "
                + ", ".join(failed_guardrails)
                + "."
            )
            continue
        if not _materially_improves(baseline, candidate, policy):
            initial_rejections[candidate.run.subject_id] = (
                "Candidate has no material improvement over the baseline."
            )
            continue
        contenders.append(candidate)

    frontier = tuple(
        candidate
        for candidate in contenders
        if not any(
            other is not candidate and _dominates(other, candidate, policy)
            for other in contenders
        )
    )
    frontier_ids = tuple(candidate.run.subject_id for candidate in frontier)
    decisions = []
    for candidate in candidates:
        subject_id = candidate.run.subject_id
        if subject_id in initial_rejections:
            decisions.append(
                CandidateDecision(
                    subject_id=subject_id,
                    eligible=False,
                    reason=initial_rejections[subject_id],
                )
            )
        elif subject_id not in frontier_ids:
            decisions.append(
                CandidateDecision(
                    subject_id=subject_id,
                    eligible=False,
                    reason="Candidate is dominated by another eligible candidate.",
                )
            )
        else:
            decisions.append(
                CandidateDecision(
                    subject_id=subject_id,
                    eligible=True,
                    reason=(
                        "Candidate passes hard guardrails, is non-dominated, "
                        "and materially improves the baseline."
                    ),
                )
            )
    return ParetoResult(
        decisions=tuple(decisions),
        frontier_ids=frontier_ids,
        eligible_ids=frontier_ids,
    )


def _undefined_metrics(
    result: EvaluationResult,
    policy: EvaluationPolicy,
) -> tuple[str, ...]:
    return tuple(
        metric.name
        for metric in policy.metrics
        if metric.undefined_behavior is UndefinedBehavior.FAIL
        and (
            metric.name not in result.metrics
            or result.metrics[metric.name].outcome is Outcome.UNDEFINED
            or result.metrics[metric.name].median is None
        )
    )


def _materially_improves(
    baseline: EvaluationResult,
    candidate: EvaluationResult,
    policy: EvaluationPolicy,
) -> bool:
    for metric in policy.metrics:
        baseline_value = baseline.metrics.get(metric.name)
        candidate_value = candidate.metrics.get(metric.name)
        if (
            baseline_value is None
            or candidate_value is None
            or baseline_value.median is None
            or candidate_value.median is None
        ):
            continue
        if (
            metric.improvement(
                baseline_value.median,
                candidate_value.median,
            )
            >= metric.materiality
        ):
            return True
    return False


def _dominates(
    left: EvaluationResult,
    right: EvaluationResult,
    policy: EvaluationPolicy,
) -> bool:
    strict = False
    compared = False
    for metric in policy.metrics:
        left_aggregate = left.metrics.get(metric.name)
        right_aggregate = right.metrics.get(metric.name)
        if (
            left_aggregate is None
            or right_aggregate is None
            or left_aggregate.median is None
            or right_aggregate.median is None
        ):
            if metric.undefined_behavior is UndefinedBehavior.IGNORE:
                continue
            return False
        compared = True
        improvement = metric.improvement(
            right_aggregate.median,
            left_aggregate.median,
        )
        if improvement < 0:
            return False
        strict = strict or improvement > 0
    return compared and strict
