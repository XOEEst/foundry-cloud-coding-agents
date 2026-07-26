from datetime import UTC, datetime

import pytest

from foundry_opt.evaluation import (
    AgentVersionRef,
    DatasetSplit,
    DatasetVersionRef,
    EvaluationItem,
    EvaluationPolicy,
    EvaluationRun,
    EvaluationScore,
    EvaluationStatus,
    EvaluatorDefinitionRef,
    MetricDirection,
    MetricPolicy,
    Outcome,
    TrajectoryMetadata,
    UndefinedBehavior,
    Usage,
    normalize_evaluation,
)


def _run(status: EvaluationStatus = EvaluationStatus.COMPLETED) -> EvaluationRun:
    return EvaluationRun(
        run_id="run-1",
        evaluation_id="evaluation-1",
        subject_id="candidate-1",
        split=DatasetSplit.DEVELOPMENT,
        agent=AgentVersionRef("agent-1", "draft-1", "3"),
        dataset=DatasetVersionRef("dataset-1", "8"),
        evaluator=EvaluatorDefinitionRef("definition-1", "2"),
        status=status,
        portal_url="https://portal.azure.com/evaluations/evaluation-1/runs/run-1",
        started_at=datetime(2026, 7, 26, 20, tzinfo=UTC),
        completed_at=datetime(2026, 7, 26, 20, 1, tzinfo=UTC),
        error=None,
    )


def _policy() -> EvaluationPolicy:
    return EvaluationPolicy(
        metrics=(
            MetricPolicy(
                name="quality",
                direction=MetricDirection.MAXIMIZE,
                threshold=0.75,
                materiality=0.05,
            ),
            MetricPolicy(
                name="latency",
                direction=MetricDirection.MINIMIZE,
                threshold=2.0,
                materiality=0.2,
                hard_guardrail=True,
            ),
        ),
        noisy_spread=0.25,
        borderline_distance=0.03,
    )


def test_normalize_evaluation_preserves_pins_and_aggregates_scores() -> None:
    items = (
        EvaluationItem(
            case_id="case-1",
            case_hash="sha256:case-1",
            response_ids=("response-1",),
            scores=(
                EvaluationScore("quality", 4, 0.8, "correct"),
                EvaluationScore("latency", 1.2, 1.2, "fast"),
            ),
            usage=Usage(input_tokens=10, output_tokens=4),
            trajectory=TrajectoryMetadata(
                trajectory_id="trajectory-1",
                turn_count=2,
                tool_calls=(),
            ),
            duration_ms=120,
        ),
        EvaluationItem(
            case_id="case-2",
            case_hash="sha256:case-2",
            response_ids=("response-2",),
            scores=(
                EvaluationScore("quality", 3, 0.7, "mostly correct"),
                EvaluationScore("latency", 1.8, 1.8, "acceptable"),
            ),
            usage=Usage(input_tokens=20, output_tokens=8, cached_tokens=2),
            duration_ms=180,
        ),
    )

    result = normalize_evaluation(_run(), items, _policy())

    assert result.run.agent.version == "3"
    assert result.run.dataset.version == "8"
    assert result.run.evaluator.version == "2"
    assert result.response_ids == ("response-1", "response-2")
    assert result.metrics["quality"].median == 0.75
    assert result.metrics["quality"].spread == 0.1
    assert result.metrics["quality"].outcome is Outcome.PASS
    assert result.metrics["latency"].median == 1.5
    assert result.metrics["latency"].outcome is Outcome.PASS
    assert result.usage == Usage(
        input_tokens=30,
        output_tokens=12,
        cached_tokens=2,
    )
    assert result.duration_ms == 300
    assert result.needs_repeat is True


def test_normalize_evaluation_marks_missing_metric_and_partial_run_undefined() -> None:
    item = EvaluationItem(
        case_id="case-1",
        case_hash="sha256:case-1",
        response_ids=("response-1",),
        scores=(EvaluationScore("quality", None, None, "evaluator failed"),),
        usage=Usage(),
        error="case execution failed",
        duration_ms=25,
    )

    result = normalize_evaluation(
        _run(EvaluationStatus.PARTIAL),
        (item,),
        _policy(),
    )

    assert result.complete is False
    assert result.metrics["quality"].outcome is Outcome.UNDEFINED
    assert result.metrics["latency"].outcome is Outcome.UNDEFINED
    assert result.errors == ("case execution failed",)
    assert result.needs_repeat is True


def test_metric_policy_rejects_negative_materiality() -> None:
    with pytest.raises(ValueError):
        MetricPolicy(
            name="quality",
            direction=MetricDirection.MAXIMIZE,
            threshold=0.75,
            materiality=-0.01,
        )


def test_ignored_undefined_metric_does_not_make_completed_run_incomplete() -> None:
    policy = EvaluationPolicy(
        metrics=(
            MetricPolicy(
                name="quality",
                direction=MetricDirection.MAXIMIZE,
                threshold=0.75,
                materiality=0.05,
            ),
            MetricPolicy(
                name="optional_style",
                direction=MetricDirection.MAXIMIZE,
                threshold=0.5,
                materiality=0.05,
                undefined_behavior=UndefinedBehavior.IGNORE,
            ),
        )
    )
    item = EvaluationItem(
        case_id="case-1",
        case_hash="sha256:case-1",
        response_ids=("response-1",),
        scores=(EvaluationScore("quality", 4, 0.8, "correct"),),
        usage=Usage(),
        duration_ms=10,
    )

    result = normalize_evaluation(_run(), (item,), policy)

    assert result.metrics["optional_style"].outcome is Outcome.UNDEFINED
    assert result.complete is True
    assert result.needs_repeat is False


def test_normalization_rejects_duplicate_scores_for_one_metric() -> None:
    item = EvaluationItem(
        case_id="case-1",
        case_hash="sha256:case-1",
        response_ids=("response-1",),
        scores=(
            EvaluationScore("quality", 4, 0.8, "first"),
            EvaluationScore("quality", 1, 0.2, "duplicate"),
            EvaluationScore("latency", 1.4, 1.4, "fast"),
        ),
        usage=Usage(),
    )

    with pytest.raises(ValueError, match="duplicate"):
        normalize_evaluation(_run(), (item,), _policy())


def test_evaluation_policy_rejects_duplicate_metric_names() -> None:
    metric = MetricPolicy(
        name="quality",
        direction=MetricDirection.MAXIMIZE,
        threshold=0.75,
        materiality=0.05,
    )

    with pytest.raises(ValueError):
        EvaluationPolicy(metrics=(metric, metric))
