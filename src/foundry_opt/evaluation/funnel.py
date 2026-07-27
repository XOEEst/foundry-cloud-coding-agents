from collections.abc import Callable
from dataclasses import replace
from statistics import median

from foundry_opt.evaluation.models import (
    DatasetSplit,
    EvaluationFunnelRequest,
    EvaluationPolicy,
    EvaluationResult,
    EvaluationStatus,
    EvaluationSubject,
    FunnelResult,
    FunnelStageResult,
    MetricAggregate,
    Outcome,
    UndefinedBehavior,
    Usage,
)
from foundry_opt.evaluation.selection import select_eligible_candidates


EvaluationRunner = Callable[
    [EvaluationSubject, DatasetSplit, int],
    EvaluationResult,
]


def run_evaluation_funnel(
    request: EvaluationFunnelRequest,
    evaluate: EvaluationRunner,
) -> FunnelResult:
    development_subjects = (request.baseline, *request.candidates)
    development_results = {
        subject.subject_id: _evaluate_with_repeat(
            subject,
            DatasetSplit.DEVELOPMENT,
            request.policy,
            evaluate,
        )
        for subject in development_subjects
    }
    development_pareto = select_eligible_candidates(
        development_results[request.baseline.subject_id],
        tuple(
            development_results[candidate.subject_id]
            for candidate in request.candidates
        ),
        request.policy,
    )
    subjects_by_id = {
        subject.subject_id: subject for subject in development_subjects
    }
    validation_subjects = (
        request.baseline,
        *(
            subjects_by_id[subject_id]
            for subject_id in development_pareto.eligible_ids
        ),
    )
    validation_results = {
        subject.subject_id: _evaluate_with_repeat(
            subject,
            DatasetSplit.VALIDATION,
            request.policy,
            evaluate,
        )
        for subject in validation_subjects
    }
    validation_pareto = select_eligible_candidates(
        validation_results[request.baseline.subject_id],
        tuple(
            validation_results[subject_id]
            for subject_id in development_pareto.eligible_ids
        ),
        request.policy,
    )
    return FunnelResult(
        development=FunnelStageResult(
            results=development_results,
            pareto=development_pareto,
        ),
        validation=FunnelStageResult(
            results=validation_results,
            pareto=validation_pareto,
        ),
    )


def _evaluate_with_repeat(
    subject: EvaluationSubject,
    split: DatasetSplit,
    policy: EvaluationPolicy,
    evaluate: EvaluationRunner,
) -> EvaluationResult:
    first = evaluate(subject, split, 1)
    if not first.needs_repeat:
        return first
    second = evaluate(subject, split, 2)
    return _combine_attempts((first, second), policy)


def _combine_attempts(
    attempts: tuple[EvaluationResult, EvaluationResult],
    policy: EvaluationPolicy,
) -> EvaluationResult:
    first, second = attempts
    valid_attempts = tuple(
        result
        for result in attempts
        if result.complete
        and result.run.status is EvaluationStatus.COMPLETED
    )
    aggregates: dict[str, MetricAggregate] = {}
    for metric_policy in policy.metrics:
        attempt_aggregates = tuple(
            result.metrics[metric_policy.name] for result in valid_attempts
        )
        medians = tuple(
            aggregate.median
            for aggregate in attempt_aggregates
            if aggregate.median is not None
        )
        minima = tuple(
            aggregate.minimum
            for aggregate in attempt_aggregates
            if aggregate.minimum is not None
        )
        maxima = tuple(
            aggregate.maximum
            for aggregate in attempt_aggregates
            if aggregate.maximum is not None
        )
        if not medians:
            aggregates[metric_policy.name] = MetricAggregate(
                metric_policy.name,
                None,
                None,
                None,
                None,
                Outcome.UNDEFINED,
                0,
            )
            continue
        middle = float(median(medians))
        minimum = min(minima)
        maximum = max(maxima)
        aggregates[metric_policy.name] = MetricAggregate(
            metric=metric_policy.name,
            median=middle,
            minimum=minimum,
            maximum=maximum,
            spread=round(maximum - minimum, 12),
            outcome=(
                Outcome.PASS
                if metric_policy.passes(middle)
                else Outcome.FAIL
            ),
            sample_count=sum(
                aggregate.sample_count for aggregate in attempt_aggregates
            ),
        )
    return replace(
        second,
        cases=tuple(
            case
            for result in valid_attempts
            for case in result.cases
        ),
        metrics=aggregates,
        usage=first.usage + second.usage,
        duration_ms=first.duration_ms + second.duration_ms,
        errors=first.errors + second.errors,
        complete=second.complete
        and second.run.status is EvaluationStatus.COMPLETED
        and bool(valid_attempts)
        and all(
            metric.undefined_behavior is UndefinedBehavior.IGNORE
            or aggregates[metric.name].outcome is not Outcome.UNDEFINED
            for metric in policy.metrics
        ),
        needs_repeat=False,
        attempts=2,
        attempt_runs=first.all_runs + second.all_runs,
    )
