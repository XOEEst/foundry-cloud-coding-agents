import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from foundry_opt.evaluation import (
    CandidateDecision,
    DatasetSplit,
    EvaluationItem,
    EvaluationPolicy,
    EvaluationScore,
    ToolCallMetadata,
    TrajectoryMetadata,
    Usage,
    ParetoResult,
    normalize_evaluation,
    select_eligible_candidates,
)
from foundry_opt.evidence import (
    EvidenceRequest,
    SensitiveEvidenceError,
    TelemetryEvidence,
    write_redacted_evidence,
)

from test_evaluation_selection import POLICY, _result


def test_write_redacted_evidence_writes_allowlisted_compact_manifest(
    tmp_path: Path,
) -> None:
    baseline = _result("baseline", quality=0.70, latency=1.5)
    baseline = baseline.with_case_reason(
        case_id="case-1",
        case_hash="sha256:case-1",
        response_ids=("response-baseline",),
        reason="baseline",
        scores={"quality": (3, 0.7, "pass"), "latency": (1.5, 1.5, "pass")},
        duration_ms=100,
    )
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
            metric_policies=POLICY,
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
            metric_policies=POLICY,
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
            metric_policies=POLICY,
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
            metric_policies=POLICY,
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
            metric_policies=POLICY,
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
            metric_policies=POLICY,
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


@pytest.mark.parametrize(
    "location",
    ["campaign", "source_hash", "run_id", "telemetry_response_id"],
)
def test_evidence_writer_rejects_sensitive_values_in_every_string_field(
    tmp_path: Path,
    location: str,
) -> None:
    sensitive = "DO-NOT-COMMIT"
    baseline = _result("baseline", quality=0.70, latency=1.5)
    campaign_id = "campaign-1"
    source_hash = "sha256:source"
    telemetry_response_id = "response-1"
    if location == "campaign":
        campaign_id = f"campaign-{sensitive}"
    elif location == "source_hash":
        source_hash = f"sha256:{sensitive}"
    elif location == "run_id":
        baseline = replace(
            baseline,
            run=replace(baseline.run, run_id=f"run-{sensitive}"),
        )
    else:
        telemetry_response_id = f"response-{sensitive}"
    baseline = baseline.with_case_reason(
        case_id="case-1",
        case_hash="sha256:case-1",
        response_ids=(telemetry_response_id,),
        reason="safe",
        scores={"quality": (3, 0.7, "pass"), "latency": (1.5, 1.5, "pass")},
        duration_ms=10,
    )
    output = tmp_path / "evidence.json"

    with pytest.raises(SensitiveEvidenceError):
        write_redacted_evidence(
            EvidenceRequest(
                output_path=output,
                campaign_id=campaign_id,
                baseline=baseline,
                candidates=(),
                pareto=select_eligible_candidates(baseline, (), POLICY),
                metric_policies=POLICY,
                source_hash=source_hash,
                telemetry=(
                    TelemetryEvidence(
                        response_id=telemetry_response_id,
                        request_count=1,
                        dependency_count=0,
                        exception_count=0,
                        duration_ms=10,
                        success_rate=1.0,
                    ),
                ),
                sensitive_values=(sensitive,),
            )
        )

    assert not output.exists()


def test_evidence_writer_rejects_telemetry_for_unserialized_response(
    tmp_path: Path,
) -> None:
    baseline = _result("baseline", quality=0.70, latency=1.5)
    baseline = baseline.with_case_reason(
        case_id="case-1",
        case_hash="sha256:case-1",
        response_ids=("response-baseline",),
        reason="safe",
        scores={"quality": (3, 0.7, "pass"), "latency": (1.5, 1.5, "pass")},
        duration_ms=10,
    )

    with pytest.raises(ValueError, match="telemetry response"):
        write_redacted_evidence(
            EvidenceRequest(
                output_path=tmp_path / "evidence.json",
                campaign_id="campaign-1",
                baseline=baseline,
                candidates=(),
                pareto=select_eligible_candidates(baseline, (), POLICY),
                metric_policies=POLICY,
                source_hash="sha256:source",
                telemetry=(
                    TelemetryEvidence(
                        response_id="response-production",
                        request_count=1,
                        dependency_count=0,
                        exception_count=0,
                        duration_ms=10,
                        success_rate=1.0,
                    ),
                ),
            )
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("duration_ms", float("nan")),
        ("duration_ms", float("inf")),
        ("success_rate", float("-inf")),
    ],
)
def test_telemetry_evidence_rejects_non_finite_floats(
    field: str,
    value: float,
) -> None:
    values: dict[str, object] = {
        "response_id": "response-1",
        "request_count": 1,
        "dependency_count": 0,
        "exception_count": 0,
        "duration_ms": 10.0,
        "success_rate": 1.0,
    }
    values[field] = value

    with pytest.raises(ValueError):
        TelemetryEvidence(**values)


def test_evidence_writer_revalidates_telemetry_model_boundary(
    tmp_path: Path,
) -> None:
    telemetry = TelemetryEvidence(
        response_id="response-1",
        request_count=1,
        dependency_count=0,
        exception_count=0,
        duration_ms=10,
        success_rate=1.0,
    )
    object.__setattr__(telemetry, "duration_ms", float("nan"))
    baseline = _result("baseline", quality=0.70, latency=1.5)

    with pytest.raises(ValueError):
        write_redacted_evidence(
            EvidenceRequest(
                output_path=tmp_path / "evidence.json",
                campaign_id="campaign-1",
                baseline=baseline,
                candidates=(),
                pareto=select_eligible_candidates(baseline, (), POLICY),
                metric_policies=POLICY,
                source_hash="sha256:source",
                telemetry=(telemetry,),
            )
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", {"nested": "payload"}),
        ("evaluation_id", ["evaluation-baseline"]),
        ("subject_id", "subject with spaces"),
    ],
)
def test_evidence_writer_rejects_unsafe_result_identifiers(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    baseline = _result("baseline", quality=0.70, latency=1.5)
    baseline = replace(
        baseline,
        run=replace(baseline.run, **{field: value}),
    )

    with pytest.raises(ValueError, match="identifier"):
        write_redacted_evidence(
            EvidenceRequest(
                output_path=tmp_path / "evidence.json",
                campaign_id="campaign-1",
                baseline=baseline,
                candidates=(),
                pareto=select_eligible_candidates(baseline, (), POLICY),
                metric_policies=POLICY,
                source_hash="sha256:source",
            )
        )


def test_evidence_writer_rejects_unsafe_case_and_trace_identifiers(
    tmp_path: Path,
) -> None:
    baseline = _result("baseline", quality=0.70, latency=1.5)
    candidate = _result("candidate", quality=0.80, latency=1.4)
    item = EvaluationItem(
        case_id="case with spaces",
        case_hash="sha256:case-1",
        response_ids=("response/unsafe",),
        scores=(
            EvaluationScore("quality", 4, 0.8, "correct"),
            EvaluationScore("latency", 1.4, 1.4, "fast"),
        ),
        usage=Usage(),
        trajectory=TrajectoryMetadata(
            trajectory_id="trajectory/unsafe",
            turn_count=1,
            tool_calls=(
                ToolCallMetadata(
                    call_id="call/unsafe",
                    name="search",
                    status="completed",
                ),
            ),
        ),
    )
    candidate = normalize_evaluation(
        candidate.run,
        (item,),
        EvaluationPolicy(metrics=POLICY.metrics),
    )

    with pytest.raises(ValueError, match="identifier"):
        write_redacted_evidence(
            EvidenceRequest(
                output_path=tmp_path / "evidence.json",
                campaign_id="campaign-1",
                baseline=baseline,
                candidates=(candidate,),
                pareto=select_eligible_candidates(
                    baseline,
                    (candidate,),
                    POLICY,
                ),
                metric_policies=POLICY,
                source_hash="sha256:source",
            )
        )


@pytest.mark.parametrize(
    "pareto_factory",
    [
        lambda valid: replace(valid, decisions=()),
        lambda valid: replace(
            valid,
            decisions=(valid.decisions[0], valid.decisions[0]),
        ),
        lambda valid: replace(valid, frontier_ids=("ghost",)),
        lambda valid: replace(
            valid,
            frontier_ids=(),
            eligible_ids=("candidate",),
        ),
        lambda valid: ParetoResult(
            decisions=(
                CandidateDecision("candidate", True, "eligible"),
                CandidateDecision("ghost", False, "missing"),
            ),
            frontier_ids=("candidate",),
            eligible_ids=("candidate",),
        ),
    ],
)
def test_evidence_writer_requires_pareto_exactly_bound_to_candidates(
    tmp_path: Path,
    pareto_factory,
) -> None:
    baseline = _result("baseline", quality=0.70, latency=1.5)
    candidate = _result("candidate", quality=0.80, latency=1.4)
    valid = select_eligible_candidates(baseline, (candidate,), POLICY)

    with pytest.raises(ValueError, match="Pareto"):
        write_redacted_evidence(
            EvidenceRequest(
                output_path=tmp_path / "evidence.json",
                campaign_id="campaign-1",
                baseline=baseline,
                candidates=(candidate,),
                pareto=pareto_factory(valid),
                metric_policies=POLICY,
                source_hash="sha256:source",
            )
        )


def test_evidence_writer_recomputes_and_rejects_fabricated_pareto(
    tmp_path: Path,
) -> None:
    baseline = _result("baseline", quality=0.70, latency=1.5)
    candidate = _result("candidate", quality=0.80, latency=1.4)
    fabricated = ParetoResult(
        decisions=(
            CandidateDecision("candidate", False, "caller fabricated"),
        ),
        frontier_ids=(),
        eligible_ids=(),
    )

    with pytest.raises(ValueError, match="recomputed"):
        write_redacted_evidence(
            EvidenceRequest(
                output_path=tmp_path / "evidence.json",
                campaign_id="campaign-1",
                baseline=baseline,
                candidates=(candidate,),
                pareto=fabricated,
                metric_policies=POLICY,
                source_hash="sha256:source",
            )
        )


def test_evidence_writer_rejects_duplicate_candidate_subject_ids(
    tmp_path: Path,
) -> None:
    baseline = _result("baseline", quality=0.70, latency=1.5)
    candidate = _result("candidate", quality=0.80, latency=1.4)
    pareto = select_eligible_candidates(baseline, (candidate,), POLICY)

    with pytest.raises(ValueError, match="candidate"):
        write_redacted_evidence(
            EvidenceRequest(
                output_path=tmp_path / "evidence.json",
                campaign_id="campaign-1",
                baseline=baseline,
                candidates=(candidate, candidate),
                pareto=pareto,
                metric_policies=POLICY,
                source_hash="sha256:source",
            )
        )


def test_evidence_writer_does_not_overwrite_existing_destination(
    tmp_path: Path,
) -> None:
    output = tmp_path / "evidence.json"
    output.write_text("external-content", encoding="utf-8")
    baseline = _result("baseline", quality=0.70, latency=1.5)

    with pytest.raises(FileExistsError):
        write_redacted_evidence(
            EvidenceRequest(
                output_path=output,
                campaign_id="campaign-1",
                baseline=baseline,
                candidates=(),
                pareto=select_eligible_candidates(baseline, (), POLICY),
                metric_policies=POLICY,
                source_hash="sha256:source",
            )
        )

    assert output.read_text(encoding="utf-8") == "external-content"


def test_evidence_writer_rejects_symlink_destination(tmp_path: Path) -> None:
    target = tmp_path / "external.json"
    target.write_text("external-content", encoding="utf-8")
    output = tmp_path / "evidence.json"
    try:
        output.symlink_to(target)
    except OSError:
        pytest.skip("Symlinks are unavailable in this environment.")
    baseline = _result("baseline", quality=0.70, latency=1.5)

    with pytest.raises(ValueError, match="symlink"):
        write_redacted_evidence(
            EvidenceRequest(
                output_path=output,
                campaign_id="campaign-1",
                baseline=baseline,
                candidates=(),
                pareto=select_eligible_candidates(baseline, (), POLICY),
                metric_policies=POLICY,
                source_hash="sha256:source",
            )
        )

    assert target.read_text(encoding="utf-8") == "external-content"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda run: replace(run, subject_id="foreign-subject"),
        lambda run: replace(
            run,
            agent=replace(run.agent, draft_id="foreign-draft"),
        ),
        lambda run: replace(
            run,
            dataset=replace(run.dataset, version="foreign-version"),
        ),
        lambda run: replace(
            run,
            evaluator=replace(run.evaluator, version="foreign-version"),
        ),
        lambda run: replace(run, split=DatasetSplit.DEVELOPMENT),
    ],
)
def test_evidence_writer_rejects_foreign_attempt_run_lineage(
    tmp_path: Path,
    mutate,
) -> None:
    baseline = _result("baseline", quality=0.70, latency=1.5)
    baseline = replace(
        baseline,
        attempt_runs=(mutate(baseline.run),),
    )

    with pytest.raises(ValueError, match="lineage"):
        write_redacted_evidence(
            EvidenceRequest(
                output_path=tmp_path / "evidence.json",
                campaign_id="campaign-1",
                baseline=baseline,
                candidates=(),
                pareto=select_eligible_candidates(baseline, (), POLICY),
                metric_policies=POLICY,
                source_hash="sha256:source",
            )
        )
