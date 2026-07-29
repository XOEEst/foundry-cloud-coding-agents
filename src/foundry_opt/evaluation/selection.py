from collections.abc import Sequence

from foundry_opt.evaluation.models import (
    CandidateDecision,
    EvaluationPolicy,
    EvaluationResult,
    EvaluationStatus,
    MetricAggregate,
    MetricDirection,
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
    baseline_failure = _baseline_failure(baseline, policy)
    if baseline_failure is not None:
        decisions = tuple(
            CandidateDecision(
                subject_id=candidate.run.subject_id,
                eligible=False,
                reason=baseline_failure,
            )
            for candidate in candidates
        )
        return ParetoResult(
            decisions=decisions,
            frontier_ids=(),
            eligible_ids=(),
        )
    initial_rejections: dict[str, str] = {}
    contenders: list[EvaluationResult] = []

    for candidate in candidates:
        if candidate.run.status is not EvaluationStatus.COMPLETED:
            initial_rejections[candidate.run.subject_id] = (
                "Candidate run did not reach completed status."
            )
            continue
        if not candidate.complete:
            initial_rejections[candidate.run.subject_id] = (
                "Candidate evaluation remained incomplete after its repeat."
            )
            continue
        comparison_failure = _comparison_failure(
            baseline,
            candidate,
            policy,
        )
        if comparison_failure is not None:
            initial_rejections[candidate.run.subject_id] = comparison_failure
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
            and (
                (
                    metric.name in candidate.metrics
                    and candidate.metrics[metric.name].outcome is Outcome.FAIL
                )
                or (
                    metric.undefined_behavior is UndefinedBehavior.FAIL
                    and (
                        metric.name not in candidate.metrics
                        or candidate.metrics[metric.name].outcome
                        is Outcome.UNDEFINED
                    )
                )
            )
        )
        if failed_guardrails:
            initial_rejections[candidate.run.subject_id] = (
                "Candidate failed a hard guardrail: "
                + ", ".join(failed_guardrails)
                + "."
            )
            continue
        if _dominates(baseline, candidate, policy):
            initial_rejections[candidate.run.subject_id] = (
                "Candidate is dominated by the baseline."
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


def _baseline_failure(
    baseline: EvaluationResult,
    policy: EvaluationPolicy,
) -> str | None:
    if (
        not baseline.complete
        or baseline.run.status is not EvaluationStatus.COMPLETED
    ):
        return "Baseline evaluation is incomplete or failed."
    undefined = _undefined_metrics(baseline, policy)
    if undefined:
        return (
            "Baseline required metric is undefined: "
            + ", ".join(undefined)
            + "."
        )
    if not _policy_compatible(baseline, policy):
        return "Baseline metrics are incompatible with the metric policy."
    return None


def _comparison_failure(
    baseline: EvaluationResult,
    candidate: EvaluationResult,
    policy: EvaluationPolicy,
) -> str | None:
    if (
        baseline.run.split is not candidate.run.split
        or baseline.run.dataset != candidate.run.dataset
        or baseline.run.evaluator != candidate.run.evaluator
        or _case_lineage(baseline) != _case_lineage(candidate)
    ):
        return (
            "Candidate evaluation lineage differs from the pinned baseline."
        )
    if (
        not _policy_compatible(candidate, policy)
        or set(candidate.metrics) != set(baseline.metrics)
    ):
        return "Candidate metrics are incompatible with the metric policy."
    return None


def _case_lineage(result: EvaluationResult) -> frozenset[tuple[str, str]]:
    return frozenset(
        (case.case_id, case.case_hash) for case in result.cases
    )


def _policy_compatible(
    result: EvaluationResult,
    policy: EvaluationPolicy,
) -> bool:
    policy_names = {metric.name for metric in policy.metrics}
    return (
        policy_names <= set(result.metrics)
        and all(
            aggregate.metric == name
            for name, aggregate in result.metrics.items()
        )
        and all(
            _aggregate_matches_policy(
                result.metrics[metric.name],
                metric,
            )
            for metric in policy.metrics
        )
    )


def _aggregate_matches_policy(
    aggregate: MetricAggregate,
    policy: MetricPolicy,
) -> bool:
    if aggregate.median is None:
        return aggregate.outcome is Outcome.UNDEFINED
    expected = (
        Outcome.PASS
        if policy.passes(aggregate.median)
        else Outcome.FAIL
    )
    return aggregate.outcome is expected


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
        improvement = metric.improvement(
            baseline_value.median,
            candidate_value.median,
        )
        if improvement > 0 and improvement >= metric.materiality:
            return True
        if metric.hard_guardrail:
            continue
        baseline_worst = (
            baseline_value.minimum
            if metric.direction is MetricDirection.MAXIMIZE
            else baseline_value.maximum
        )
        candidate_worst = (
            candidate_value.minimum
            if metric.direction is MetricDirection.MAXIMIZE
            else candidate_value.maximum
        )
        if baseline_worst is None or candidate_worst is None:
            continue
        worst_case_improvement = metric.improvement(
            baseline_worst,
            candidate_worst,
        )
        if (
            worst_case_improvement > 0
            and worst_case_improvement >= metric.materiality
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
        left_value = (
            left_aggregate.median if left_aggregate is not None else None
        )
        right_value = (
            right_aggregate.median if right_aggregate is not None else None
        )
        if (left_value is None) != (right_value is None):
            return False
        if left_value is None or right_value is None:
            if metric.undefined_behavior is UndefinedBehavior.IGNORE:
                continue
            return False
        compared = True
        improvement = metric.improvement(
            right_value,
            left_value,
        )
        if improvement < 0:
            return False
        strict = strict or improvement > 0
    return compared and strict
