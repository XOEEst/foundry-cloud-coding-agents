import hashlib
import json
from pathlib import Path

from foundry_opt.evaluation import EvaluationPolicy, select_eligible_candidates
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
            sensitive_values=("TOP-SECRET-RAW-PROMPT",),
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
    assert document["candidates"][0]["cases"][0]["reason"].endswith("[REDACTED]")
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
        "https://portal.azure.com/runs/baseline?token=secret#trace"
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
        "https://portal.azure.com/runs/baseline"
    )
