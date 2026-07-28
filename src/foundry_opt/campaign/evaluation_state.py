"""Lossless (de)serialization of a full :class:`EvaluationResult`.

The filesystem candidate handoff persists whole evaluation results between
independent CLI invocations so that finalization can reconstruct Pareto
selection and evidence without re-running a completed development
evaluation. Only identity, metric, and usage metadata is stored; raw
prompts, responses, dataset rows, and tool payloads are never part of an
``EvaluationResult`` and therefore never enter this document.

Round-tripping through JSON preserves scalar ``raw_score`` types (``bool``
stays ``bool``, ``int`` stays ``int``, ``float`` stays ``float``) because
JSON encodes each distinctly; enums use their ``value`` and datetimes use
ISO-8601 text.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

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


def evaluation_result_to_document(result: EvaluationResult) -> dict[str, Any]:
    """Return a JSON-safe, order-stable document for ``result``."""
    return {
        "attempts": result.attempts,
        "attempt_runs": [_run_document(run) for run in result.attempt_runs],
        "cases": [_case_document(case) for case in result.cases],
        "complete": result.complete,
        "duration_ms": result.duration_ms,
        "errors": list(result.errors),
        "metrics": {
            name: _aggregate_document(aggregate)
            for name, aggregate in result.metrics.items()
        },
        "needs_repeat": result.needs_repeat,
        "run": _run_document(result.run),
        "usage": _usage_document(result.usage),
    }


def evaluation_result_from_document(document: Any) -> EvaluationResult:
    """Rebuild an :class:`EvaluationResult` from ``document``."""
    if not isinstance(document, dict):
        raise ValueError("evaluation result document is invalid")
    return EvaluationResult(
        run=_run_from_document(document["run"]),
        cases=tuple(
            _case_from_document(case) for case in document.get("cases", ())
        ),
        metrics={
            str(name): _aggregate_from_document(str(name), aggregate)
            for name, aggregate in document.get("metrics", {}).items()
        },
        usage=_usage_from_document(document["usage"]),
        duration_ms=int(document["duration_ms"]),
        errors=tuple(str(error) for error in document.get("errors", ())),
        complete=bool(document["complete"]),
        needs_repeat=bool(document["needs_repeat"]),
        attempts=int(document["attempts"]),
        attempt_runs=tuple(
            _run_from_document(run)
            for run in document.get("attempt_runs", ())
        ),
    )


def _run_document(run: EvaluationRun) -> dict[str, Any]:
    return {
        "agent": {
            "agent_id": run.agent.agent_id,
            "draft_id": run.agent.draft_id,
            "version": run.agent.version,
        },
        "completed_at": _datetime_text(run.completed_at),
        "dataset": {
            "dataset_id": run.dataset.dataset_id,
            "version": run.dataset.version,
        },
        "error": run.error,
        "evaluation_id": run.evaluation_id,
        "evaluator": {
            "definition_id": run.evaluator.definition_id,
            "version": run.evaluator.version,
        },
        "portal_url": run.portal_url,
        "run_id": run.run_id,
        "split": run.split.value,
        "started_at": _datetime_text(run.started_at),
        "status": run.status.value,
        "subject_id": run.subject_id,
    }


def _run_from_document(document: Any) -> EvaluationRun:
    if not isinstance(document, dict):
        raise ValueError("evaluation run document is invalid")
    agent = document["agent"]
    dataset = document["dataset"]
    evaluator = document["evaluator"]
    return EvaluationRun(
        run_id=str(document["run_id"]),
        evaluation_id=str(document["evaluation_id"]),
        subject_id=str(document["subject_id"]),
        split=DatasetSplit(str(document["split"])),
        agent=AgentVersionRef(
            agent_id=str(agent["agent_id"]),
            draft_id=str(agent["draft_id"]),
            version=str(agent["version"]),
        ),
        dataset=DatasetVersionRef(
            dataset_id=str(dataset["dataset_id"]),
            version=str(dataset["version"]),
        ),
        evaluator=EvaluatorDefinitionRef(
            definition_id=str(evaluator["definition_id"]),
            version=str(evaluator["version"]),
        ),
        status=EvaluationStatus(str(document["status"])),
        portal_url=(
            str(document["portal_url"])
            if document.get("portal_url") is not None
            else None
        ),
        started_at=_datetime_from_text(document.get("started_at")),
        completed_at=_datetime_from_text(document.get("completed_at")),
        error=(
            str(document["error"])
            if document.get("error") is not None
            else None
        ),
    )


def _case_document(case: NormalizedCase) -> dict[str, Any]:
    return {
        "case_hash": case.case_hash,
        "case_id": case.case_id,
        "duration_ms": case.duration_ms,
        "error": case.error,
        "response_ids": list(case.response_ids),
        "scores": [_score_document(score) for score in case.scores],
        "trajectory": _trajectory_document(case.trajectory),
        "usage": _usage_document(case.usage),
    }


def _case_from_document(document: Any) -> NormalizedCase:
    if not isinstance(document, dict):
        raise ValueError("normalized case document is invalid")
    return NormalizedCase(
        case_id=str(document["case_id"]),
        case_hash=str(document["case_hash"]),
        response_ids=tuple(
            str(value) for value in document.get("response_ids", ())
        ),
        scores=tuple(
            _score_from_document(score)
            for score in document.get("scores", ())
        ),
        usage=_usage_from_document(document["usage"]),
        trajectory=_trajectory_from_document(document.get("trajectory")),
        error=(
            str(document["error"])
            if document.get("error") is not None
            else None
        ),
        duration_ms=int(document["duration_ms"]),
    )


def _score_document(score: NormalizedCaseMetric) -> dict[str, Any]:
    return {
        "metric": score.metric,
        "normalized_score": score.normalized_score,
        "outcome": score.outcome.value,
        "raw_score": score.raw_score,
        "raw_score_type": _scalar_type(score.raw_score),
        "reason": score.reason,
    }


def _score_from_document(document: Any) -> NormalizedCaseMetric:
    if not isinstance(document, dict):
        raise ValueError("normalized score document is invalid")
    return NormalizedCaseMetric(
        metric=str(document["metric"]),
        raw_score=_scalar_from_document(
            document.get("raw_score"),
            document.get("raw_score_type"),
        ),
        normalized_score=_optional_float(document.get("normalized_score")),
        reason=(
            str(document["reason"])
            if document.get("reason") is not None
            else None
        ),
        outcome=Outcome(str(document["outcome"])),
    )


def _aggregate_document(aggregate: MetricAggregate) -> dict[str, Any]:
    return {
        "maximum": aggregate.maximum,
        "median": aggregate.median,
        "metric": aggregate.metric,
        "minimum": aggregate.minimum,
        "outcome": aggregate.outcome.value,
        "sample_count": aggregate.sample_count,
        "spread": aggregate.spread,
    }


def _aggregate_from_document(name: str, document: Any) -> MetricAggregate:
    if not isinstance(document, dict):
        raise ValueError("metric aggregate document is invalid")
    metric = str(document.get("metric", name))
    if metric != name:
        raise ValueError("metric aggregate key does not match its metric name")
    return MetricAggregate(
        metric=metric,
        median=_optional_float(document.get("median")),
        minimum=_optional_float(document.get("minimum")),
        maximum=_optional_float(document.get("maximum")),
        spread=_optional_float(document.get("spread")),
        outcome=Outcome(str(document["outcome"])),
        sample_count=int(document["sample_count"]),
    )


def _trajectory_document(
    trajectory: TrajectoryMetadata | None,
) -> dict[str, Any] | None:
    if trajectory is None:
        return None
    return {
        "tool_calls": [
            {
                "call_id": call.call_id,
                "duration_ms": call.duration_ms,
                "name": call.name,
                "status": call.status,
            }
            for call in trajectory.tool_calls
        ],
        "trajectory_id": trajectory.trajectory_id,
        "turn_count": trajectory.turn_count,
    }


def _trajectory_from_document(document: Any) -> TrajectoryMetadata | None:
    if document is None:
        return None
    if not isinstance(document, dict):
        raise ValueError("trajectory document is invalid")
    return TrajectoryMetadata(
        trajectory_id=str(document["trajectory_id"]),
        turn_count=int(document["turn_count"]),
        tool_calls=tuple(
            ToolCallMetadata(
                call_id=str(call["call_id"]),
                name=str(call["name"]),
                status=str(call["status"]),
                duration_ms=(
                    int(call["duration_ms"])
                    if call.get("duration_ms") is not None
                    else None
                ),
            )
            for call in document.get("tool_calls", ())
        ),
    )


def _usage_document(usage: Usage) -> dict[str, int]:
    return {
        "cached_tokens": usage.cached_tokens,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
    }


def _usage_from_document(document: Any) -> Usage:
    if not isinstance(document, dict):
        raise ValueError("usage document is invalid")
    return Usage(
        input_tokens=int(document["input_tokens"]),
        output_tokens=int(document["output_tokens"]),
        cached_tokens=int(document["cached_tokens"]),
    )


def _scalar_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    raise ValueError("raw_score must be a JSON scalar")


def _scalar_from_document(value: Any, declared: Any) -> Any:
    if declared is None:
        return value
    declared = str(declared)
    if declared == "null":
        return None
    if declared == "bool":
        return bool(value)
    if declared == "int":
        return int(value)
    if declared == "float":
        return float(value)
    if declared == "str":
        return str(value)
    raise ValueError("raw_score_type is invalid")


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("numeric metric fields must not be booleans")
    return float(value)


def _datetime_text(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _datetime_from_text(value: Any) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(str(value))
