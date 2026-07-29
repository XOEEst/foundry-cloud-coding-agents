from dataclasses import replace
from datetime import UTC, datetime

import pytest

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


def test_non_completed_candidate_run_is_not_eligible() -> None:
    baseline = _result("baseline", quality=0.70, latency=1.5)
    candidate = _result("candidate", quality=0.80, latency=1.4)
    candidate = replace(
        candidate,
        run=replace(candidate.run, status=EvaluationStatus.FAILED),
        complete=True,
    )

    result = select_eligible_candidates(baseline, (candidate,), POLICY)

    assert result.eligible_ids == ()
    assert "completed" in result.decision_for("candidate").reason


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


def test_worst_case_objective_improvement_is_material() -> None:
    baseline = _result("baseline", quality=0.90, latency=1.5)
    candidate = _result("candidate", quality=0.90, latency=1.5)
    baseline = replace(
        baseline,
        metrics={
            **baseline.metrics,
            "quality": replace(
                baseline.metrics["quality"],
                minimum=0.40,
                spread=0.50,
            ),
        },
    )
    candidate = replace(
        candidate,
        metrics={
            **candidate.metrics,
            "quality": replace(
                candidate.metrics["quality"],
                minimum=0.80,
                spread=0.10,
            ),
        },
    )

    result = select_eligible_candidates(
        baseline,
        (candidate,),
        POLICY,
    )

    assert result.eligible_ids == ("candidate",)


def test_incomplete_baseline_rejects_all_candidates() -> None:
    baseline = replace(
        _result("baseline", quality=0.70, latency=1.5),
        complete=False,
    )
    candidate = _result("candidate", quality=0.90, latency=1.2)

    result = select_eligible_candidates(baseline, (candidate,), POLICY)

    assert result.eligible_ids == ()
    reason = result.decision_for("candidate").reason.casefold()
    assert "baseline" in reason
    assert "incomplete" in reason


def test_failed_baseline_rejects_all_candidates() -> None:
    baseline = _result("baseline", quality=0.70, latency=1.5)
    baseline = replace(
        baseline,
        run=replace(baseline.run, status=EvaluationStatus.FAILED),
    )
    candidate = _result("candidate", quality=0.90, latency=1.2)

    result = select_eligible_candidates(baseline, (candidate,), POLICY)

    assert result.eligible_ids == ()
    assert "failed" in result.decision_for("candidate").reason.casefold()


def test_baseline_missing_required_metric_rejects_all_candidates() -> None:
    baseline = _result("baseline", quality=0.70, latency=1.5)
    baseline = replace(
        baseline,
        metrics={
            "quality": baseline.metrics["quality"],
        },
    )
    candidate = _result("candidate", quality=0.90, latency=1.2)

    result = select_eligible_candidates(baseline, (candidate,), POLICY)

    assert result.eligible_ids == ()
    reason = result.decision_for("candidate").reason.casefold()
    assert "baseline" in reason
    assert "undefined" in reason


def test_differing_ignored_metric_sets_are_incomparable_without_cycle() -> None:
    policy = EvaluationPolicy(
        metrics=tuple(
            MetricPolicy(
                name=name,
                direction=MetricDirection.MAXIMIZE,
                threshold=0,
                materiality=0.1,
                undefined_behavior=UndefinedBehavior.IGNORE,
            )
            for name in ("m1", "m2", "m3")
        )
    )

    def result(subject_id: str, values: tuple[float | None, ...]):
        evaluation = _result(subject_id, quality=0.7, latency=1.5)
        return replace(
            evaluation,
            metrics={
                name: MetricAggregate(
                    name,
                    value,
                    value,
                    value,
                    0 if value is not None else None,
                    Outcome.PASS if value is not None else Outcome.UNDEFINED,
                    1 if value is not None else 0,
                )
                for name, value in zip(("m1", "m2", "m3"), values)
            },
        )

    baseline = result("baseline", (0, 0, 0))
    candidates = (
        result("a", (3, None, 1)),
        result("b", (1, 3, None)),
        result("c", (None, 1, 3)),
    )

    pareto = select_eligible_candidates(baseline, candidates, policy)

    assert pareto.frontier_ids == ("a", "b", "c")
    assert pareto.eligible_ids == ("a", "b", "c")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda candidate: replace(
            candidate,
            run=replace(candidate.run, split=DatasetSplit.DEVELOPMENT),
        ),
        lambda candidate: replace(
            candidate,
            run=replace(
                candidate.run,
                dataset=DatasetVersionRef("other-dataset", "1"),
            ),
        ),
        lambda candidate: replace(
            candidate,
            run=replace(
                candidate.run,
                dataset=DatasetVersionRef("validation", "2"),
            ),
        ),
        lambda candidate: replace(
            candidate,
            run=replace(
                candidate.run,
                evaluator=EvaluatorDefinitionRef("other-definition", "1"),
            ),
        ),
        lambda candidate: replace(
            candidate,
            run=replace(
                candidate.run,
                evaluator=EvaluatorDefinitionRef("definition", "2"),
            ),
        ),
        lambda candidate: candidate.with_case_reason(
            case_id="other-case",
            case_hash="sha256:other-case",
            response_ids=("response-other",),
            reason="score",
            scores={"quality": (0.8, 0.8, "pass")},
            duration_ms=1,
        ),
        lambda candidate: replace(
            candidate,
            cases=(
                replace(
                    candidate.cases[0],
                    case_hash="sha256:different-case",
                ),
            ),
        ),
    ],
)
def test_candidate_requires_identical_evaluation_lineage(mutate) -> None:
    baseline = _result("baseline", quality=0.70, latency=1.5)
    baseline = baseline.with_case_reason(
        case_id="case-1",
        case_hash="sha256:case-1",
        response_ids=("response-baseline",),
        reason="score",
        scores={"quality": (0.7, 0.7, "pass")},
        duration_ms=1,
    )
    candidate = _result("candidate", quality=0.80, latency=1.4)
    candidate = candidate.with_case_reason(
        case_id="case-1",
        case_hash="sha256:case-1",
        response_ids=("response-candidate",),
        reason="score",
        scores={"quality": (0.8, 0.8, "pass")},
        duration_ms=1,
    )
    candidate = mutate(candidate)

    pareto = select_eligible_candidates(baseline, (candidate,), POLICY)

    assert pareto.eligible_ids == ()
    assert "lineage" in pareto.decision_for("candidate").reason.casefold()


def test_candidate_requires_compatible_metric_policy() -> None:
    baseline = _result("baseline", quality=0.70, latency=1.5)
    candidate = _result("candidate", quality=0.80, latency=1.4)
    candidate = replace(
        candidate,
        metrics={
            **candidate.metrics,
            "unconfigured": MetricAggregate(
                "unconfigured",
                1,
                1,
                1,
                0,
                Outcome.PASS,
                1,
            ),
        },
    )

    pareto = select_eligible_candidates(baseline, (candidate,), POLICY)

    assert pareto.eligible_ids == ()
    assert "policy" in pareto.decision_for("candidate").reason.casefold()


def test_candidate_metric_outcomes_must_match_supplied_policy() -> None:
    baseline = _result("baseline", quality=0.70, latency=1.5)
    candidate = _result("candidate", quality=0.90, latency=2.1)
    candidate = replace(
        candidate,
        metrics={
            **candidate.metrics,
            "latency": replace(
                candidate.metrics["latency"],
                outcome=Outcome.PASS,
            ),
        },
    )

    pareto = select_eligible_candidates(baseline, (candidate,), POLICY)

    assert pareto.eligible_ids == ()
    assert "policy" in pareto.decision_for("candidate").reason.casefold()
