from dataclasses import replace

from foundry_opt.evaluation import (
    DatasetSplit,
    EvaluationFunnelRequest,
    EvaluationPolicy,
    EvaluationSubject,
    FunnelResult,
    MetricDirection,
    MetricPolicy,
    run_evaluation_funnel,
)

from test_evaluation_selection import _result


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
        baseline=EvaluationSubject("baseline"),
        candidates=(
            EvaluationSubject("winner"),
            EvaluationSubject("rejected"),
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
        baseline=EvaluationSubject("baseline"),
        candidates=(EvaluationSubject("candidate"),),
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
