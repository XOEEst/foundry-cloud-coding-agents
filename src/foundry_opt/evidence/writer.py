import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from foundry_opt.evaluation import (
    EvaluationResult,
    NormalizedCase,
    NormalizedCaseMetric,
)
from foundry_opt.evidence.models import EvidenceManifest, EvidenceRequest
from foundry_opt.preflight.redaction import redact


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
    sensitive_values = request.sensitive_values
    document: dict[str, object] = {
        "schema_version": 1,
        "campaign_id": _safe_text(request.campaign_id, sensitive_values),
        "source_hash": request.source_hash,
        "baseline": _result_document(request.baseline, sensitive_values),
        "candidates": [
            {
                **_result_document(candidate, sensitive_values),
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
                    "reason": _safe_text(decision.reason, sensitive_values),
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
    sensitive_values: tuple[str, ...],
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
                "error": _safe_text(attempt.error, sensitive_values),
            }
            for attempt in result.all_runs
        ],
        "split": run.split.value,
        "portal_url": _safe_portal_url(run.portal_url),
        "complete": result.complete,
        "repeat_count": max(result.attempts - 1, 0),
        "duration_ms": result.duration_ms,
        "usage": {
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "cached_tokens": result.usage.cached_tokens,
        },
        "errors": [
            _safe_text(error, sensitive_values) for error in result.errors
        ],
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
            _case_document(case, sensitive_values) for case in result.cases
        ],
    }


def _case_document(
    case: NormalizedCase,
    sensitive_values: tuple[str, ...],
) -> dict[str, object]:
    trajectory: dict[str, object] | None = None
    if case.trajectory is not None:
        trajectory = {
            "trajectory_id": case.trajectory.trajectory_id,
            "turn_count": case.trajectory.turn_count,
            "tool_calls": [
                {
                    "call_id": tool_call.call_id,
                    "name": _safe_text(tool_call.name, sensitive_values),
                    "status": _safe_text(tool_call.status, sensitive_values),
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
        "reason": _case_reason(case, sensitive_values),
        "error": _safe_text(case.error, sensitive_values),
        "scores": [
            _score_document(score, sensitive_values) for score in case.scores
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
    sensitive_values: tuple[str, ...],
) -> dict[str, object]:
    raw_score = score.raw_score
    if isinstance(raw_score, str):
        raw_score = _safe_text(raw_score, sensitive_values)
    return {
        "metric": score.metric,
        "raw_score": raw_score,
        "normalized_score": score.normalized_score,
        "outcome": score.outcome.value,
        "reason": _safe_text(score.reason, sensitive_values),
    }


def _case_reason(
    case: NormalizedCase,
    sensitive_values: tuple[str, ...],
) -> str | None:
    reasons = tuple(
        score.reason for score in case.scores if score.reason is not None
    )
    if not reasons:
        return None
    return _safe_text(" | ".join(dict.fromkeys(reasons)), sensitive_values)


def _safe_text(
    value: str | None,
    sensitive_values: tuple[str, ...],
) -> str | None:
    if value is None:
        return None
    redacted = redact(value, sensitive_values)
    if redacted is None:
        return None
    return redacted[:512]


def _safe_portal_url(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
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


def _unique(values: object) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
