import hashlib
import json
from math import isfinite
from urllib.parse import urlsplit, urlunsplit

from foundry_opt.evaluation import (
    EvaluationResult,
    NormalizedCase,
    NormalizedCaseMetric,
    Outcome,
)
from foundry_opt.evidence.models import EvidenceManifest, EvidenceRequest


def write_redacted_evidence(request: EvidenceRequest) -> EvidenceManifest:
    document = _build_document(request)
    serialized = (
        json.dumps(
            document,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    output_path = request.output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.name}.writing")
    temporary_path.write_bytes(serialized)
    temporary_path.replace(output_path)
    runs = tuple(
        run
        for result in (request.baseline, *request.candidates)
        for run in result.all_runs
    )
    return EvidenceManifest(
        path=output_path,
        sha256=hashlib.sha256(serialized).hexdigest(),
        byte_count=len(serialized),
        evaluation_ids=_unique(run.evaluation_id for run in runs),
        run_ids=_unique(run.run_id for run in runs),
    )


def _build_document(request: EvidenceRequest) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": 1,
        "campaign_id": request.campaign_id,
        "source_hash": request.source_hash,
        "baseline": _result_document(request.baseline),
        "candidates": [
            {
                **_result_document(candidate),
                "patch_hash": (request.patch_hashes or {}).get(
                    candidate.run.subject_id
                ),
            }
            for candidate in request.candidates
        ],
        "pareto": {
            "frontier_ids": list(request.pareto.frontier_ids),
            "eligible_ids": list(request.pareto.eligible_ids),
            "decisions": [
                {
                    "subject_id": decision.subject_id,
                    "eligible": decision.eligible,
                    "reason_code": _decision_code(
                        decision.eligible,
                        decision.reason,
                    ),
                }
                for decision in request.pareto.decisions
            ],
        },
    }
    if request.generated_at is not None:
        document["generated_at"] = request.generated_at.isoformat()
    if request.telemetry:
        document["telemetry"] = [
            {
                "response_id": item.response_id,
                "request_count": item.request_count,
                "dependency_count": item.dependency_count,
                "exception_count": item.exception_count,
                "duration_ms": item.duration_ms,
                "success_rate": item.success_rate,
            }
            for item in request.telemetry
        ]
    return document


def _result_document(
    result: EvaluationResult,
) -> dict[str, object]:
    run = result.run
    return {
        "subject_id": run.subject_id,
        "agent": {
            "agent_id": run.agent.agent_id,
            "draft_id": run.agent.draft_id,
            "version": run.agent.version,
        },
        "dataset": {
            "dataset_id": run.dataset.dataset_id,
            "version": run.dataset.version,
        },
        "evaluator": {
            "definition_id": run.evaluator.definition_id,
            "version": run.evaluator.version,
        },
        "evaluation_id": run.evaluation_id,
        "run_id": run.run_id,
        "attempts": [
            {
                "evaluation_id": attempt.evaluation_id,
                "run_id": attempt.run_id,
                "status": attempt.status.value,
                "started_at": (
                    attempt.started_at.isoformat()
                    if attempt.started_at is not None
                    else None
                ),
                "completed_at": (
                    attempt.completed_at.isoformat()
                    if attempt.completed_at is not None
                    else None
                ),
                "error_code": (
                    "provider_error" if attempt.error is not None else None
                ),
            }
            for attempt in result.all_runs
        ],
        "split": run.split.value,
        "portal_url": _safe_portal_url(
            run.portal_url,
            evaluation_id=run.evaluation_id,
            run_id=run.run_id,
        ),
        "complete": result.complete,
        "repeat_count": max(result.attempts - 1, 0),
        "duration_ms": result.duration_ms,
        "usage": {
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "cached_tokens": result.usage.cached_tokens,
        },
        "error_count": len(result.errors),
        "metrics": {
            name: {
                "median": aggregate.median,
                "minimum": aggregate.minimum,
                "maximum": aggregate.maximum,
                "spread": aggregate.spread,
                "outcome": aggregate.outcome.value,
                "sample_count": aggregate.sample_count,
            }
            for name, aggregate in sorted(result.metrics.items())
        },
        "cases": [
            _case_document(case) for case in result.cases
        ],
    }


def _case_document(
    case: NormalizedCase,
) -> dict[str, object]:
    trajectory: dict[str, object] | None = None
    if case.trajectory is not None:
        trajectory = {
            "trajectory_id": case.trajectory.trajectory_id,
            "turn_count": case.trajectory.turn_count,
            "tool_calls": [
                {
                    "call_id": tool_call.call_id,
                    "status_code": _tool_status_code(tool_call.status),
                    "duration_ms": tool_call.duration_ms,
                }
                for tool_call in case.trajectory.tool_calls
            ],
        }
    return {
        "case_id": case.case_id,
        "case_hash": case.case_hash,
        "response_ids": list(case.response_ids),
        "duration_ms": case.duration_ms,
        "reason_code": _case_reason_code(case),
        "error_code": "case_error" if case.error is not None else None,
        "scores": [
            _score_document(score) for score in case.scores
        ],
        "usage": {
            "input_tokens": case.usage.input_tokens,
            "output_tokens": case.usage.output_tokens,
            "cached_tokens": case.usage.cached_tokens,
        },
        "trajectory": trajectory,
    }


def _score_document(
    score: NormalizedCaseMetric,
) -> dict[str, object]:
    raw_score = (
        score.raw_score
        if _is_finite_number(score.raw_score)
        else None
    )
    return {
        "metric": score.metric,
        "raw_score": raw_score,
        "raw_score_code": _raw_score_code(score.raw_score),
        "normalized_score": score.normalized_score,
        "outcome": score.outcome.value,
        "reason_code": _outcome_reason_code(score.outcome),
    }


def _case_reason_code(case: NormalizedCase) -> str:
    outcomes = {score.outcome for score in case.scores}
    if Outcome.FAIL in outcomes:
        return "evaluator_fail"
    if Outcome.UNDEFINED in outcomes:
        return "evaluator_undefined"
    return "evaluator_pass"


def _outcome_reason_code(outcome: Outcome) -> str:
    return f"evaluator_{outcome.value}"


def _raw_score_code(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    return normalized if normalized in {"pass", "fail", "undefined"} else None


def _tool_status_code(value: str) -> str:
    normalized = value.strip().casefold()
    if normalized in {
        "queued",
        "running",
        "completed",
        "succeeded",
        "failed",
        "cancelled",
    }:
        return normalized
    return "unknown"


def _decision_code(eligible: bool, reason: str) -> str:
    if eligible:
        return "eligible"
    normalized = reason.casefold()
    if "hard guardrail" in normalized:
        return "hard_guardrail_failed"
    if "undefined" in normalized:
        return "required_metric_undefined"
    if "incomplete" in normalized:
        return "evaluation_incomplete"
    if "dominated by the baseline" in normalized:
        return "baseline_dominated"
    if "dominated" in normalized:
        return "candidate_dominated"
    if "material improvement" in normalized:
        return "no_material_improvement"
    return "ineligible"


def _safe_portal_url(
    value: str | None,
    *,
    evaluation_id: str,
    run_id: str,
) -> str | None:
    if value is None:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    hostname = parsed.hostname.casefold() if parsed.hostname else None
    if (
        parsed.scheme.casefold() != "https"
        or hostname not in {"portal.azure.com", "ai.azure.com"}
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    path_parts = tuple(part for part in parsed.path.split("/") if part)
    if parsed.path != "/" + "/".join(path_parts):
        return None
    valid_paths = {
        ("runs", run_id),
        ("evaluations", evaluation_id, "runs", run_id),
    }
    valid_project_path = (
        len(path_parts) == 6
        and path_parts[0] == "projects"
        and _safe_identifier(path_parts[1])
        and path_parts[2:] == (
            "evaluations",
            evaluation_id,
            "runs",
            run_id,
        )
    )
    if path_parts not in valid_paths and not valid_project_path:
        return None
    return urlunsplit(
        (
            "https",
            parsed.netloc,
            parsed.path,
            "",
            "",
        )
    )


def _safe_identifier(value: str) -> bool:
    return (
        1 <= len(value) <= 128
        and all(
            character.isascii()
            and (character.isalnum() or character in "._:-")
            for character in value
        )
    )


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return True
    return isinstance(value, float) and isfinite(value)


def _unique(values: object) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
