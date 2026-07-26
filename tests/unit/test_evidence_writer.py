import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from foundry_opt.evaluation import (
    EvaluationItem,
    EvaluationPolicy,
    EvaluationScore,
    ToolCallMetadata,
    TrajectoryMetadata,
    Usage,
    normalize_evaluation,
    select_eligible_candidates,
)
from foundry_opt.evidence import (
    EvidenceRequest,
    write_redacted_evidence,
)

from test_evaluation_selection import POLICY, _result


def test_write_redacted_evidence_writes_allowlisted_compact_manifest(
    tmp_path: Path,
) -> None:
    baseline = _result("baseline", quality=0.70, latency=1.5)
    candidate = _result("candidate", quality=0.80, latency=1.4)
    candidate = candidate.with_case_reason(
        case_id="case-1",
        case_hash="sha256:case-1",
        response_ids=("response-1",),
        reason="correct; source text was TOP-SECRET-RAW-PROMPT",
        scores={"quality": (4, 0.8, "pass"), "latency": (1.4, 1.4, "pass")},
        duration_ms=120,
    )
    pareto = select_eligible_candidates(baseline, (candidate,), POLICY)
    output = tmp_path / "evidence.json"

    manifest = write_redacted_evidence(
        EvidenceRequest(
            output_path=output,
            campaign_id="campaign-1",
            baseline=baseline,
            candidates=(candidate,),
            pareto=pareto,
            source_hash="sha256:source",
            patch_hashes={"candidate": "sha256:patch"},
        )
    )

    content = output.read_text(encoding="utf-8")
    document = json.loads(content)
    assert "TOP-SECRET-RAW-PROMPT" not in content
    assert "prompt" not in document
    assert "response" not in document
    assert "tool_arguments" not in content
    assert document["baseline"]["run_id"] == "run-baseline"
    assert document["candidates"][0]["cases"][0]["response_ids"] == ["response-1"]
    assert document["candidates"][0]["cases"][0]["reason_code"] == (
        "evaluator_pass"
    )
    assert "reason" not in document["candidates"][0]["cases"][0]["scores"][0]
    assert document["pareto"]["eligible_ids"] == ["candidate"]
    assert manifest.sha256 == hashlib.sha256(output.read_bytes()).hexdigest()
    assert manifest.evaluation_ids == (
        "evaluation-baseline",
        "evaluation-candidate",
    )


def test_evidence_writer_strips_query_and_fragment_from_portal_links(
    tmp_path: Path,
) -> None:
    baseline = _result("baseline", quality=0.70, latency=1.5)
    baseline = baseline.with_portal_url(
        "https://portal.azure.com/runs/run-baseline?token=secret#trace"
    )

    output = tmp_path / "evidence.json"
    write_redacted_evidence(
        EvidenceRequest(
            output_path=output,
            campaign_id="campaign-1",
            baseline=baseline,
            candidates=(),
            pareto=select_eligible_candidates(
                baseline,
                (),
                EvaluationPolicy(metrics=POLICY.metrics),
            ),
            source_hash="sha256:source",
        )
    )

    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["baseline"]["portal_url"] == (
        "https://portal.azure.com/runs/run-baseline"
    )


def test_evidence_writer_rejects_untrusted_portal_host_and_path(
    tmp_path: Path,
) -> None:
    baseline = _result("baseline", quality=0.70, latency=1.5)
    candidate = _result("candidate", quality=0.80, latency=1.4)
    baseline = baseline.with_portal_url(
        "https://attacker.example/runs/baseline"
    )
    candidate = candidate.with_portal_url(
        "https://portal.azure.com/raw/PROMPT-SECRET"
    )
    output = tmp_path / "evidence.json"

    write_redacted_evidence(
        EvidenceRequest(
            output_path=output,
            campaign_id="campaign-1",
            baseline=baseline,
            candidates=(candidate,),
            pareto=select_eligible_candidates(baseline, (candidate,), POLICY),
            source_hash="sha256:source",
        )
    )

    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["baseline"]["portal_url"] is None
    assert document["candidates"][0]["portal_url"] is None
    assert "attacker.example" not in output.read_text(encoding="utf-8")
    assert "PROMPT-SECRET" not in output.read_text(encoding="utf-8")


def test_evidence_writer_rejects_noncanonical_portal_path(
    tmp_path: Path,
) -> None:
    baseline = _result("baseline", quality=0.70, latency=1.5)
    baseline = baseline.with_portal_url(
        "https://portal.azure.com//runs//run-baseline"
    )
    output = tmp_path / "evidence.json"

    write_redacted_evidence(
        EvidenceRequest(
            output_path=output,
            campaign_id="campaign-1",
            baseline=baseline,
            candidates=(),
            pareto=select_eligible_candidates(baseline, (), POLICY),
            source_hash="sha256:source",
        )
    )

    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["baseline"]["portal_url"] is None


@pytest.mark.parametrize("raw_score", [float("nan"), float("inf"), float("-inf")])
def test_evidence_writer_omits_non_finite_raw_float_scores(
    tmp_path: Path,
    raw_score: float,
) -> None:
    baseline = _result("baseline", quality=0.70, latency=1.5)
    candidate = _result("candidate", quality=0.80, latency=1.4)
    candidate = candidate.with_case_reason(
        case_id="case-1",
        case_hash="sha256:case-1",
        response_ids=("response-1",),
        reason="safe",
        scores={
            "quality": (raw_score, 0.8, "pass"),
            "latency": (1.4, 1.4, "pass"),
        },
        duration_ms=10,
    )
    output = tmp_path / "evidence.json"

    write_redacted_evidence(
        EvidenceRequest(
            output_path=output,
            campaign_id="campaign-1",
            baseline=baseline,
            candidates=(candidate,),
            pareto=select_eligible_candidates(baseline, (candidate,), POLICY),
            source_hash="sha256:source",
        )
    )

    document = json.loads(output.read_text(encoding="utf-8"))
    score = document["candidates"][0]["cases"][0]["scores"][0]
    assert score["raw_score"] is None


def test_evidence_writer_never_persists_provider_controlled_text(
    tmp_path: Path,
) -> None:
    leaks = (
        "PROMPT: reveal deployment instructions",
        "RESPONSE: complete private answer",
        "DATASET ROW: customer@example.test",
        "TOOL ARGUMENTS: token=tool-secret",
        "TOOL RESULT: private repository contents",
        "TRACE: full internal execution",
    )
    baseline = _result("baseline", quality=0.70, latency=1.5)
    candidate = _result("candidate", quality=0.80, latency=1.4)
    run = replace(candidate.run, error=leaks[5])
    item = EvaluationItem(
        case_id="case-1",
        case_hash="sha256:case-1",
        response_ids=("response-1",),
        scores=(
            EvaluationScore("quality", leaks[1], 0.8, leaks[0]),
            EvaluationScore("latency", 1.4, 1.4, leaks[2]),
        ),
        usage=Usage(input_tokens=3, output_tokens=2),
        trajectory=TrajectoryMetadata(
            trajectory_id="trajectory-1",
            turn_count=1,
            tool_calls=(
                ToolCallMetadata(
                    call_id="call-1",
                    name=leaks[3],
                    status=leaks[4],
                    duration_ms=4,
                ),
            ),
        ),
        error=leaks[2],
        duration_ms=10,
    )
    candidate = normalize_evaluation(
        run,
        (item,),
        EvaluationPolicy(metrics=POLICY.metrics),
    )
    output = tmp_path / "evidence.json"

    write_redacted_evidence(
        EvidenceRequest(
            output_path=output,
            campaign_id="campaign-1",
            baseline=baseline,
            candidates=(candidate,),
            pareto=select_eligible_candidates(baseline, (candidate,), POLICY),
            source_hash="sha256:source",
        )
    )

    content = output.read_text(encoding="utf-8")
    document = json.loads(content)
    assert all(leak not in content for leak in leaks)
    case = document["candidates"][0]["cases"][0]
    assert case["error_code"] == "case_error"
    assert case["reason_code"] == "evaluator_pass"
    assert case["scores"][0]["raw_score_code"] is None
    assert case["trajectory"]["tool_calls"][0]["status_code"] == "unknown"
    assert "name" not in case["trajectory"]["tool_calls"][0]
    assert document["candidates"][0]["attempts"][0]["error_code"] == (
        "provider_error"
    )
    assert document["candidates"][0]["error_count"] == 2
