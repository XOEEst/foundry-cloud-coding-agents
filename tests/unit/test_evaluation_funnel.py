from dataclasses import replace

import pytest

from foundry_opt.evaluation import (
    AgentVersionRef,
    DatasetSplit,
    EvaluationFunnelRequest,
    EvaluationPolicy,
    EvaluationStatus,
    EvaluationSubject,
    FunnelResult,
    MetricDirection,
    MetricAggregate,
    MetricPolicy,
    Outcome,
    UndefinedBehavior,
    run_evaluation_funnel,
)

from test_evaluation_selection import _result


def _subject(subject_id: str) -> EvaluationSubject:
    return EvaluationSubject(
        subject_id,
        AgentVersionRef("agent-1", f"draft-{subject_id}", "1"),
    )


def test_funnel_repeats_noisy_results_once_and_validates_only_development_winners() -> None:
    policy = EvaluationPolicy(
        metrics=(
            MetricPolicy(
                "quality",
                MetricDirection.MAXIMIZE,
                threshold=0.6,
                materiality=0.05,
            ),
            MetricPolicy(
                "latency",
                MetricDirection.MINIMIZE,
                threshold=2.0,
                materiality=0.2,
                hard_guardrail=True,
            ),
        ),
        noisy_spread=0.2,
    )
    request = EvaluationFunnelRequest(
        baseline=_subject("baseline"),
        candidates=(
            _subject("winner"),
            _subject("rejected"),
        ),
        policy=policy,
    )
    calls: list[tuple[str, DatasetSplit, int]] = []

    def evaluate(
        subject: EvaluationSubject,
        split: DatasetSplit,
        attempt: int,
    ):
        calls.append((subject.subject_id, split, attempt))
        quality = {
            ("baseline", DatasetSplit.DEVELOPMENT): 0.70,
            ("winner", DatasetSplit.DEVELOPMENT): 0.80,
            ("rejected", DatasetSplit.DEVELOPMENT): 0.71,
            ("baseline", DatasetSplit.VALIDATION): 0.68,
            ("winner", DatasetSplit.VALIDATION): 0.77,
        }[(subject.subject_id, split)]
        result = _result(subject.subject_id, quality, 1.5)
        result = replace(result, run=replace(result.run, split=split))
        if subject.subject_id == "winner" and split is DatasetSplit.DEVELOPMENT:
            aggregate = replace(
                result.metrics["quality"],
                minimum=0.65,
                maximum=0.90,
                spread=0.25,
            )
            result = replace(
                result,
                metrics={**result.metrics, "quality": aggregate},
                needs_repeat=attempt == 1,
            )
        return result

    result = run_evaluation_funnel(request, evaluate)

    assert isinstance(result, FunnelResult)
    assert calls.count(("winner", DatasetSplit.DEVELOPMENT, 1)) == 1
    assert calls.count(("winner", DatasetSplit.DEVELOPMENT, 2)) == 1
    assert ("rejected", DatasetSplit.VALIDATION, 1) not in calls
    assert result.validation.pareto.eligible_ids == ("winner",)
    assert result.development.results["winner"].attempts == 2


def test_funnel_can_recover_when_partial_first_attempt_repeats_cleanly() -> None:
    policy = EvaluationPolicy(
        metrics=(
            MetricPolicy(
                "quality",
                MetricDirection.MAXIMIZE,
                threshold=0.6,
                materiality=0.05,
            ),
            MetricPolicy(
                "latency",
                MetricDirection.MINIMIZE,
                threshold=2.0,
                materiality=0.2,
                hard_guardrail=True,
            ),
        )
    )
    request = EvaluationFunnelRequest(
        baseline=_subject("baseline"),
        candidates=(_subject("candidate"),),
        policy=policy,
    )

    def evaluate(
        subject: EvaluationSubject,
        split: DatasetSplit,
        attempt: int,
    ):
        quality = 0.70 if subject.subject_id == "baseline" else 0.80
        result = _result(subject.subject_id, quality, 1.4)
        result = replace(result, run=replace(result.run, split=split))
        if subject.subject_id == "candidate" and attempt == 1:
            return replace(
                result,
                complete=False,
                needs_repeat=True,
                errors=("one case was partial",),
            )
        return result

    result = run_evaluation_funnel(request, evaluate)

    recovered = result.development.results["candidate"]
    assert recovered.complete is True
    assert recovered.attempts == 2
    assert result.validation.pareto.eligible_ids == ("candidate",)


def test_repeat_combination_ignores_configured_undefined_metric() -> None:
    policy = EvaluationPolicy(
        metrics=(
            MetricPolicy(
                "quality",
                MetricDirection.MAXIMIZE,
                threshold=0.6,
                materiality=0.05,
            ),
            MetricPolicy(
                "latency",
                MetricDirection.MINIMIZE,
                threshold=2.0,
                materiality=0.2,
                hard_guardrail=True,
            ),
            MetricPolicy(
                "optional_style",
                MetricDirection.MAXIMIZE,
                threshold=0.5,
                materiality=0.05,
                undefined_behavior=UndefinedBehavior.IGNORE,
            ),
        )
    )
    request = EvaluationFunnelRequest(
        baseline=_subject("baseline"),
        candidates=(_subject("candidate"),),
        policy=policy,
    )

    def evaluate(
        subject: EvaluationSubject,
        split: DatasetSplit,
        attempt: int,
    ):
        quality = 0.70 if subject.subject_id == "baseline" else 0.80
        result = _result(subject.subject_id, quality, 1.4)
        result = replace(
            result,
            run=replace(result.run, split=split),
            metrics={
                **result.metrics,
                "optional_style": MetricAggregate(
                    "optional_style",
                    None,
                    None,
                    None,
                    None,
                    Outcome.UNDEFINED,
                    0,
                ),
            },
        )
        if (
            subject.subject_id == "candidate"
            and split is DatasetSplit.DEVELOPMENT
            and attempt == 1
        ):
            return replace(result, needs_repeat=True)
        return result

    result = run_evaluation_funnel(request, evaluate)

    combined = result.development.results["candidate"]
    assert combined.attempts == 2
    assert combined.complete is True
    assert result.validation.pareto.eligible_ids == ("candidate",)


@pytest.mark.parametrize(
    "candidates",
    [
        (_subject("baseline"),),
        (_subject("candidate"), _subject("candidate")),
    ],
)
def test_funnel_request_rejects_duplicate_subject_ids(
    candidates: tuple[EvaluationSubject, ...],
) -> None:
    policy = EvaluationPolicy(
        metrics=(
            MetricPolicy(
                "quality",
                MetricDirection.MAXIMIZE,
                threshold=0.6,
                materiality=0.05,
            ),
        )
    )

    with pytest.raises(ValueError, match="subject"):
        EvaluationFunnelRequest(
            baseline=_subject("baseline"),
            candidates=candidates,
            policy=policy,
        )


def test_partial_attempt_metrics_do_not_influence_retry_eligibility() -> None:
    policy = EvaluationPolicy(
        metrics=(
            MetricPolicy(
                "quality",
                MetricDirection.MAXIMIZE,
                threshold=0.6,
                materiality=0.01,
            ),
        )
    )
    request = EvaluationFunnelRequest(
        baseline=_subject("baseline"),
        candidates=(_subject("candidate"),),
        policy=policy,
    )
    calls: list[tuple[str, DatasetSplit, int]] = []

    def evaluate(
        subject: EvaluationSubject,
        split: DatasetSplit,
        attempt: int,
    ):
        calls.append((subject.subject_id, split, attempt))
        if subject.subject_id == "baseline":
            quality = 0.70
        elif split is DatasetSplit.DEVELOPMENT and attempt == 1:
            quality = 1.0
        else:
            quality = 0.69
        result = _result(subject.subject_id, quality, 1.4)
        result = replace(result, run=replace(result.run, split=split))
        case_id = (
            "partial-case"
            if subject.subject_id == "candidate"
            and split is DatasetSplit.DEVELOPMENT
            and attempt == 1
            else "complete-case"
        )
        result = result.with_case_reason(
            case_id=case_id,
            case_hash=f"sha256:{case_id}",
            response_ids=(f"response-{case_id}",),
            reason="score",
            scores={"quality": (quality, quality, "pass")},
            duration_ms=1,
        )
        if (
            subject.subject_id == "candidate"
            and split is DatasetSplit.DEVELOPMENT
            and attempt == 1
        ):
            return replace(
                result,
                run=replace(result.run, status=EvaluationStatus.PARTIAL),
                complete=False,
                needs_repeat=True,
                errors=("partial attempt",),
            )
        return result

    result = run_evaluation_funnel(request, evaluate)

    combined = result.development.results["candidate"]
    assert combined.metrics["quality"].median == 0.69
    assert tuple(case.case_id for case in combined.cases) == ("complete-case",)
    assert combined.complete is True
    assert result.development.pareto.eligible_ids == ()
    assert ("candidate", DatasetSplit.VALIDATION, 1) not in calls


@pytest.mark.parametrize(
    "mutate",
    [
        lambda result: replace(
            result,
            run=replace(result.run, subject_id="stale-subject"),
        ),
        lambda result: replace(
            result,
            run=replace(
                result.run,
                agent=AgentVersionRef("agent-1", "stale-draft", "1"),
            ),
        ),
        lambda result: replace(
            result,
            run=replace(result.run, split=DatasetSplit.VALIDATION),
        ),
    ],
)
def test_funnel_rejects_attempts_outside_requested_subject_lineage(
    mutate,
) -> None:
    expected_agent = AgentVersionRef("agent-1", "draft-baseline", "1")
    request = EvaluationFunnelRequest(
        baseline=EvaluationSubject("baseline", expected_agent),
        candidates=(),
        policy=EvaluationPolicy(
            metrics=(
                MetricPolicy(
                    "quality",
                    MetricDirection.MAXIMIZE,
                    threshold=0.6,
                    materiality=0.05,
                ),
                MetricPolicy(
                    "latency",
                    MetricDirection.MINIMIZE,
                    threshold=2.0,
                    materiality=0.2,
                ),
            )
        ),
    )

    def evaluate(
        subject: EvaluationSubject,
        split: DatasetSplit,
        attempt: int,
    ):
        result = _result(subject.subject_id, quality=0.70, latency=1.5)
        result = replace(result, run=replace(result.run, split=split))
        return mutate(result)

    with pytest.raises(ValueError, match="lineage"):
        run_evaluation_funnel(request, evaluate)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda run: replace(
            run,
            dataset=replace(run.dataset, dataset_id="foreign-dataset"),
        ),
        lambda run: replace(
            run,
            dataset=replace(run.dataset, version="foreign-version"),
        ),
        lambda run: replace(
            run,
            evaluator=replace(
                run.evaluator,
                definition_id="foreign-definition",
            ),
        ),
        lambda run: replace(
            run,
            evaluator=replace(run.evaluator, version="foreign-version"),
        ),
    ],
)
def test_funnel_rejects_repeat_attempts_with_different_evaluation_lineage(
    mutate,
) -> None:
    request = EvaluationFunnelRequest(
        baseline=_subject("baseline"),
        candidates=(),
        policy=EvaluationPolicy(
            metrics=(
                MetricPolicy(
                    "quality",
                    MetricDirection.MAXIMIZE,
                    threshold=0.6,
                    materiality=0.05,
                ),
                MetricPolicy(
                    "latency",
                    MetricDirection.MINIMIZE,
                    threshold=2.0,
                    materiality=0.2,
                ),
            )
        ),
    )

    def evaluate(
        subject: EvaluationSubject,
        split: DatasetSplit,
        attempt: int,
    ):
        result = _result(subject.subject_id, quality=0.70, latency=1.5)
        result = replace(result, run=replace(result.run, split=split))
        if attempt == 1:
            return replace(result, needs_repeat=True)
        return replace(result, run=mutate(result.run))

    with pytest.raises(ValueError, match="lineage"):
        run_evaluation_funnel(request, evaluate)
