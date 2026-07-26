from dataclasses import replace
from datetime import UTC, datetime

from foundry_opt.evaluation import (
    AgentVersionRef,
    DatasetSplit,
    DatasetVersionRef,
    EvaluationPolicy,
    EvaluationResult,
    EvaluationRun,
    EvaluationStatus,
    EvaluatorDefinitionRef,
    MetricAggregate,
    MetricDirection,
    MetricPolicy,
    Outcome,
    ParetoResult,
    UndefinedBehavior,
    Usage,
    select_eligible_candidates,
)


POLICY = EvaluationPolicy(
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


def _result(subject_id: str, quality: float, latency: float) -> EvaluationResult:
    run = EvaluationRun(
        run_id=f"run-{subject_id}",
        evaluation_id=f"evaluation-{subject_id}",
        subject_id=subject_id,
        split=DatasetSplit.VALIDATION,
        agent=AgentVersionRef("agent-1", f"draft-{subject_id}", "1"),
        dataset=DatasetVersionRef("validation", "1"),
        evaluator=EvaluatorDefinitionRef("definition", "1"),
        status=EvaluationStatus.COMPLETED,
        portal_url=f"https://portal.azure.com/runs/{subject_id}",
        started_at=datetime(2026, 7, 26, tzinfo=UTC),
        completed_at=datetime(2026, 7, 26, 0, 1, tzinfo=UTC),
        error=None,
    )
    return EvaluationResult(
        run=run,
        cases=(),
        metrics={
            "quality": MetricAggregate(
                "quality", quality, quality, quality, 0, Outcome.PASS, 1
            ),
            "latency": MetricAggregate(
                "latency",
                latency,
                latency,
                latency,
                0,
                Outcome.PASS if latency <= 2 else Outcome.FAIL,
                1,
            ),
        },
        usage=Usage(),
        duration_ms=1,
        errors=(),
        complete=True,
        needs_repeat=False,
        attempts=1,
    )


def test_select_eligible_candidates_enforces_guardrails_and_materiality() -> None:
    baseline = _result("baseline", quality=0.70, latency=1.5)
    material = _result("material", quality=0.78, latency=1.6)
    guardrail_failure = _result("guardrail", quality=0.90, latency=2.1)
    immaterial = _result("immaterial", quality=0.73, latency=1.5)

    result = select_eligible_candidates(
        baseline,
        (material, guardrail_failure, immaterial),
        POLICY,
    )

    assert isinstance(result, ParetoResult)
    assert result.eligible_ids == ("material",)
    assert "hard guardrail" in result.decision_for("guardrail").reason
    assert "material improvement" in result.decision_for("immaterial").reason


def test_select_eligible_candidates_keeps_only_non_dominated_frontier() -> None:
    baseline = _result("baseline", quality=0.70, latency=1.5)
    dominated = _result("dominated", quality=0.76, latency=1.5)
    frontier = _result("frontier", quality=0.80, latency=1.4)

    result = select_eligible_candidates(
        baseline,
        (dominated, frontier),
        POLICY,
    )

    assert result.eligible_ids == ("frontier",)
    assert "dominated" in result.decision_for("dominated").reason


def test_undefined_hard_guardrail_is_not_eligible_by_default() -> None:
    baseline = _result("baseline", quality=0.70, latency=1.5)
    candidate = _result("candidate", quality=0.80, latency=1.5)
    candidate = replace(
        candidate,
        metrics={
            **candidate.metrics,
            "latency": MetricAggregate(
                "latency", None, None, None, None, Outcome.UNDEFINED, 0
            ),
        },
    )

    result = select_eligible_candidates(baseline, (candidate,), POLICY)

    assert result.eligible_ids == ()
    assert "undefined" in result.decision_for("candidate").reason


def test_incomplete_candidate_is_not_eligible_after_its_single_repeat() -> None:
    baseline = _result("baseline", quality=0.70, latency=1.5)
    candidate = replace(
        _result("candidate", quality=0.80, latency=1.4),
        complete=False,
        attempts=2,
    )

    result = select_eligible_candidates(baseline, (candidate,), POLICY)

    assert result.eligible_ids == ()
    assert "incomplete" in result.decision_for("candidate").reason


def test_explicit_ignore_behavior_excludes_undefined_metric_from_selection() -> None:
    policy = EvaluationPolicy(
        metrics=(
            POLICY.metrics[0],
            replace(
                POLICY.metrics[1],
                undefined_behavior=UndefinedBehavior.IGNORE,
            ),
        )
    )
    baseline = _result("baseline", quality=0.70, latency=1.5)
    candidate = _result("candidate", quality=0.80, latency=1.5)
    candidate = replace(
        candidate,
        metrics={
            **candidate.metrics,
            "latency": MetricAggregate(
                "latency", None, None, None, None, Outcome.UNDEFINED, 0
            ),
        },
    )

    result = select_eligible_candidates(baseline, (candidate,), policy)

    assert result.eligible_ids == ("candidate",)


def test_ignore_undefined_does_not_ignore_explicit_guardrail_failure() -> None:
    policy = EvaluationPolicy(
        metrics=(
            POLICY.metrics[0],
            replace(
                POLICY.metrics[1],
                undefined_behavior=UndefinedBehavior.IGNORE,
            ),
        )
    )
    baseline = _result("baseline", quality=0.70, latency=1.5)
    candidate = _result("candidate", quality=0.90, latency=2.1)

    result = select_eligible_candidates(baseline, (candidate,), policy)

    assert result.eligible_ids == ()
    assert "hard guardrail" in result.decision_for("candidate").reason


def test_baseline_dominated_candidate_is_not_eligible() -> None:
    baseline = _result("baseline", quality=0.80, latency=1.4)
    candidate = _result("candidate", quality=0.70, latency=1.6)

    result = select_eligible_candidates(baseline, (candidate,), POLICY)

    assert result.eligible_ids == ()
    assert "baseline" in result.decision_for("candidate").reason


def test_zero_materiality_still_requires_strict_positive_improvement() -> None:
    policy = EvaluationPolicy(
        metrics=tuple(replace(metric, materiality=0) for metric in POLICY.metrics)
    )
    baseline = _result("baseline", quality=0.70, latency=1.5)
    unchanged = _result("unchanged", quality=0.70, latency=1.5)

    result = select_eligible_candidates(baseline, (unchanged,), policy)

    assert result.eligible_ids == ()
    assert "material improvement" in result.decision_for("unchanged").reason
