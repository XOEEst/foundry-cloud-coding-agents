from __future__ import annotations

from datetime import UTC, datetime
import json

from foundry_opt.campaign.evaluation_state import (
    evaluation_result_from_document,
    evaluation_result_to_document,
)
from foundry_opt.evaluation import (
    AgentVersionRef,
    DatasetSplit,
    DatasetVersionRef,
    EvaluationResult,
    EvaluationRun,
    EvaluationStatus,
    EvaluatorDefinitionRef,
    MetricAggregate,
    NormalizedCase,
    NormalizedCaseMetric,
    Outcome,
    ToolCallMetadata,
    TrajectoryMetadata,
    Usage,
)


def _run(subject_id: str, split: DatasetSplit) -> EvaluationRun:
    return EvaluationRun(
        run_id=f"run-{subject_id}",
        evaluation_id=f"eval-{subject_id}",
        subject_id=subject_id,
        split=split,
        agent=AgentVersionRef("agent", f"draft-{subject_id}", "7"),
        dataset=DatasetVersionRef("dataset", "3"),
        evaluator=EvaluatorDefinitionRef("quality", "2"),
        status=EvaluationStatus.COMPLETED,
        portal_url="https://portal.example.invalid/run",
        started_at=datetime(2026, 7, 26, 8, 30, tzinfo=UTC),
        completed_at=datetime(2026, 7, 26, 8, 45, tzinfo=UTC),
        error=None,
    )


def _rich_result() -> EvaluationResult:
    trajectory = TrajectoryMetadata(
        trajectory_id="trajectory-1",
        turn_count=3,
        tool_calls=(
            ToolCallMetadata("call-1", "search", "ok", 120),
            ToolCallMetadata("call-2", "write", "ok", None),
        ),
    )
    cases = (
        NormalizedCase(
            case_id="case-1",
            case_hash="hash-1",
            response_ids=("response-1", "response-2"),
            scores=(
                NormalizedCaseMetric(
                    "quality", 0.8, 0.8, "good", Outcome.PASS
                ),
                NormalizedCaseMetric(
                    "passed", True, 1.0, None, Outcome.PASS
                ),
                NormalizedCaseMetric(
                    "count", 5, None, None, Outcome.PASS
                ),
                NormalizedCaseMetric(
                    "label", "excellent", None, None, Outcome.PASS
                ),
                NormalizedCaseMetric(
                    "missing", None, None, "n/a", Outcome.UNDEFINED
                ),
            ),
            usage=Usage(3, 4, 1),
            trajectory=trajectory,
            error=None,
            duration_ms=42,
        ),
    )
    return EvaluationResult(
        run=_run("candidate-1", DatasetSplit.DEVELOPMENT),
        cases=cases,
        metrics={
            "quality": MetricAggregate(
                "quality", 0.8, 0.7, 0.9, 0.2, Outcome.PASS, 4
            ),
            "coverage": MetricAggregate(
                "coverage", None, None, None, None, Outcome.UNDEFINED, 0
            ),
        },
        usage=Usage(30, 40, 10),
        duration_ms=1234,
        errors=("transient blip",),
        complete=True,
        needs_repeat=False,
        attempts=2,
        attempt_runs=(
            _run("candidate-1", DatasetSplit.DEVELOPMENT),
            _run("candidate-1", DatasetSplit.DEVELOPMENT),
        ),
    )


def test_evaluation_result_round_trips_through_json_losslessly() -> None:
    original = _rich_result()

    document = evaluation_result_to_document(original)
    # Round-trip through JSON to exercise scalar-type preservation.
    serialized = json.dumps(document, sort_keys=True, allow_nan=False)
    restored = evaluation_result_from_document(json.loads(serialized))

    assert restored == original


def test_scalar_raw_score_types_survive_round_trip() -> None:
    original = _rich_result()
    restored = evaluation_result_from_document(
        json.loads(json.dumps(evaluation_result_to_document(original)))
    )

    scores = {score.metric: score.raw_score for score in restored.cases[0].scores}
    assert scores["passed"] is True
    assert isinstance(scores["count"], int) and scores["count"] == 5
    assert isinstance(scores["quality"], float) and scores["quality"] == 0.8
    assert scores["label"] == "excellent"
    assert scores["missing"] is None


def test_document_is_json_serializable_and_stable() -> None:
    document = evaluation_result_to_document(_rich_result())
    first = json.dumps(document, sort_keys=True, allow_nan=False)
    second = json.dumps(
        evaluation_result_to_document(
            evaluation_result_from_document(json.loads(first))
        ),
        sort_keys=True,
        allow_nan=False,
    )
    assert first == second
