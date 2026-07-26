from collections.abc import Iterable, Sequence
from statistics import median

from foundry_opt.evaluation.models import (
    EvaluationItem,
    EvaluationPolicy,
    EvaluationResult,
    EvaluationRun,
    EvaluationStatus,
    MetricAggregate,
    MetricPolicy,
    NormalizedCase,
    NormalizedCaseMetric,
    Outcome,
    Usage,
)


def normalize_evaluation(
    run: EvaluationRun,
    items: Iterable[EvaluationItem],
    policy: EvaluationPolicy | Sequence[MetricPolicy],
) -> EvaluationResult:
    resolved_policy = _coerce_policy(policy)
    item_tuple = tuple(items)
    normalized_cases = tuple(
        _normalize_case(item, resolved_policy) for item in item_tuple
    )
    aggregates = {
        metric.name: _aggregate_metric(metric, normalized_cases)
        for metric in resolved_policy.metrics
    }
    usage = sum((case.usage for case in normalized_cases), start=Usage())
    errors = tuple(
        error
        for error in (
            run.error,
            *(case.error for case in normalized_cases),
        )
        if error
    )
    complete = (
        run.status is EvaluationStatus.COMPLETED
        and bool(normalized_cases)
        and not errors
        and all(
            aggregate.outcome is not Outcome.UNDEFINED
            for aggregate in aggregates.values()
        )
    )
    needs_repeat = (
        not complete
        or any(
            resolved_policy.noisy_spread > 0
            and
            aggregate.spread is not None
            and aggregate.spread > resolved_policy.noisy_spread
            for aggregate in aggregates.values()
        )
        or any(
            resolved_policy.borderline_distance > 0
            and
            aggregate.median is not None
            and abs(aggregate.median - resolved_policy.metric(name).threshold)
            <= resolved_policy.borderline_distance
            for name, aggregate in aggregates.items()
        )
    )
    return EvaluationResult(
        run=run,
        cases=normalized_cases,
        metrics=aggregates,
        usage=usage,
        duration_ms=sum(case.duration_ms for case in normalized_cases),
        errors=errors,
        complete=complete,
        needs_repeat=needs_repeat,
        attempts=1,
        attempt_runs=(run,),
    )


def _normalize_case(
    item: EvaluationItem,
    policy: EvaluationPolicy,
) -> NormalizedCase:
    supplied_scores = {score.metric: score for score in item.scores}
    normalized_scores = []
    for metric_policy in policy.metrics:
        score = supplied_scores.get(metric_policy.name)
        if score is None:
            normalized_scores.append(
                NormalizedCaseMetric(
                    metric=metric_policy.name,
                    raw_score=None,
                    normalized_score=None,
                    reason="Metric was not returned by the evaluator.",
                    outcome=Outcome.UNDEFINED,
                )
            )
            continue
        value = score.normalized_score
        outcome = (
            Outcome.UNDEFINED
            if value is None
            else Outcome.PASS
            if metric_policy.passes(value)
            else Outcome.FAIL
        )
        normalized_scores.append(
            NormalizedCaseMetric(
                metric=score.metric,
                raw_score=score.raw_score,
                normalized_score=value,
                reason=score.reason,
                outcome=outcome,
            )
        )
    return NormalizedCase(
        case_id=item.case_id,
        case_hash=item.case_hash,
        response_ids=item.response_ids,
        scores=tuple(normalized_scores),
        usage=item.usage,
        trajectory=item.trajectory,
        error=item.error,
        duration_ms=item.duration_ms,
    )


def _aggregate_metric(
    policy: MetricPolicy,
    cases: tuple[NormalizedCase, ...],
) -> MetricAggregate:
    matching = tuple(
        score
        for case in cases
        for score in case.scores
        if score.metric == policy.name
    )
    values = tuple(
        score.normalized_score
        for score in matching
        if score.normalized_score is not None
    )
    has_undefined = any(
        score.outcome is Outcome.UNDEFINED for score in matching
    )
    if not values:
        return MetricAggregate(
            metric=policy.name,
            median=None,
            minimum=None,
            maximum=None,
            spread=None,
            outcome=Outcome.UNDEFINED,
            sample_count=0,
        )
    middle = float(median(values))
    minimum = min(values)
    maximum = max(values)
    outcome = (
        Outcome.UNDEFINED
        if has_undefined
        else Outcome.PASS
        if policy.passes(middle)
        else Outcome.FAIL
    )
    return MetricAggregate(
        metric=policy.name,
        median=middle,
        minimum=minimum,
        maximum=maximum,
        spread=round(maximum - minimum, 12),
        outcome=outcome,
        sample_count=len(values),
    )


def _coerce_policy(
    policy: EvaluationPolicy | Sequence[MetricPolicy],
) -> EvaluationPolicy:
    if isinstance(policy, EvaluationPolicy):
        return policy
    return EvaluationPolicy(metrics=tuple(policy))
