from dataclasses import replace

import pytest

from foundry_opt.orchestration import (
    AlternativeResult,
    EvidenceMergeGate,
    FoundryOperation,
    OptimizationReport,
    PublicEvidenceRenderer,
)


def test_final_pr_projection_makes_the_user_decision_obvious() -> None:
    report = OptimizationReport(
        issue_number=31,
        candidate_id="candidate-2",
        recommendation=(
            "Use the wording-only candidate because it improves coverage "
            "without changing the safety result."
        ),
        alternatives=(
            AlternativeResult(
                candidate_id="candidate-1",
                outcome="rejected",
                rejection_reason="less effective coverage improvement",
            ),
            AlternativeResult(
                candidate_id="candidate-2",
                outcome="selected",
            ),
        ),
        baseline_metrics={"advisory_safety": 1.0, "policy_coverage": 0.0},
        candidate_metrics={"advisory_safety": 1.0, "policy_coverage": 0.5},
        guardrails={"advisory_safety": "pass"},
        thresholds={"policy_coverage": 0.5},
        materiality={"policy_coverage": 0.1},
        sample_count=6,
        split="development",
        foundry_operations=(
            FoundryOperation(
                kind="evaluation",
                identifier="evalrun-123",
                url="https://ai.azure.com/evaluations/evalrun-123",
                status="completed",
                started_at="2026-08-12T18:00:00Z",
                completed_at="2026-08-12T18:02:00Z",
            ),
        ),
        changed_paths=("agent/main.py", "tests/test_agent.py"),
        validation=("uv run pytest -q: passed",),
        spec_sha256="1" * 64,
        base_commit="2" * 40,
        patch_sha256="3" * 64,
        evidence_sha256="4" * 64,
        bundle_sha256="5" * 64,
        expected_tree="6" * 40,
        required_checks={
            "Foundry exact candidate check": "success",
            "tests": "success",
        },
        merge_gate=EvidenceMergeGate.ELIGIBLE,
    )

    renderer = PublicEvidenceRenderer()
    projection = renderer.render_pr(report)
    check = renderer.render_check(report)

    assert projection.title == "[Optimize] #31 selected candidate"
    assert projection.draft is False
    for heading in (
        "## Copilot recommendation",
        "## Alternatives tested",
        "## Evaluation improvement",
        "## Evaluation policy",
        "## Guardrails",
        "## Foundry operations",
        "## Code changes",
        "## Validation",
        "## Exact lineage",
        "## Your action",
    ):
        assert heading in projection.body
    assert "policy_coverage | 0 | 0.5 | +0.5" in projection.body
    assert "advisory_safety | 1 | 1 | 0" in projection.body
    assert "Guardrail `advisory_safety`: **pass**" in projection.body
    assert "development split, 6 samples" in projection.body
    assert "policy_coverage | 0.5 | 0.1" in projection.body
    assert "[`evalrun-123`](https://ai.azure.com/evaluations/evalrun-123)" in (
        projection.body
    )
    assert "2026-08-12T18:00:00Z" in projection.body
    assert "2026-08-12T18:02:00Z" in projection.body
    assert (
        "candidate-1 | rejected | less effective coverage improvement"
        in projection.body
    )
    assert "Merge this PR to select and deploy `candidate-2`." in projection.body
    assert "<!-- foundry-opt:public-evidence:v1:issue-31:" in projection.body
    assert check.name == "Foundry exact candidate check"
    assert check.status == "completed"
    assert check.conclusion == "success"
    assert check.summary == projection.body


def test_pr_projection_handles_metrics_missing_from_one_side() -> None:
    report = OptimizationReport(
        issue_number=31,
        candidate_id="candidate-1",
        recommendation="Use the candidate.",
        alternatives=(),
        baseline_metrics={"retired_metric": 0.2},
        candidate_metrics={"new_metric": 0.8},
        guardrails={},
        thresholds={},
        materiality={},
        sample_count=1,
        split="development",
        foundry_operations=(),
        changed_paths=(),
        validation=(),
        spec_sha256="1" * 64,
        base_commit="2" * 40,
        patch_sha256="3" * 64,
        evidence_sha256="4" * 64,
        bundle_sha256="5" * 64,
        expected_tree="6" * 40,
    )

    projection = PublicEvidenceRenderer().render_pr(report)

    assert "new_metric | n/a | 0.8 | n/a" in projection.body
    assert "retired_metric | 0.2 | n/a | n/a" in projection.body


def test_issue_projection_preserves_evidence_and_derives_merge_gate() -> None:
    report = OptimizationReport(
        issue_number=31,
        candidate_id="candidate-1",
        recommendation=(
            "Use the candidate. "
            "<!-- foundry-opt:public-evidence:spoofed -->"
        ),
        alternatives=(),
        baseline_metrics={},
        candidate_metrics={},
        guardrails={},
        thresholds={},
        materiality={},
        sample_count=0,
        split="development",
        foundry_operations=(),
        changed_paths=(),
        validation=(),
        spec_sha256="1" * 64,
        base_commit="2" * 40,
        patch_sha256="3" * 64,
        evidence_sha256="4" * 64,
        bundle_sha256="5" * 64,
        expected_tree="6" * 40,
        required_checks={"Foundry exact candidate check": "pending"},
        merge_gate=EvidenceMergeGate.PENDING,
    )

    renderer = PublicEvidenceRenderer()
    pending = renderer.render_issue(report)
    deployed = renderer.render_issue(
        replace(
            report,
            merge_gate=EvidenceMergeGate.DEPLOYED,
            required_checks={
                "Foundry exact candidate check": "success",
            },
        )
    )

    assert "No aggregate metrics were reported." in pending.body
    assert "No alternatives were recorded." in pending.body
    assert "No Foundry operations were recorded." in pending.body
    assert "No changed paths were recorded." in pending.body
    assert "No validation results were recorded." in pending.body
    assert "Do not merge" in pending.body
    assert "spoofed -->" not in pending.body
    assert (
        renderer.render_issue(
            replace(report, recommendation="Different model prose.")
        ).marker
        == pending.marker
    )
    assert deployed.marker in deployed.body
    assert deployed.marker != pending.marker
    assert "## Copilot recommendation" in deployed.body
    assert "## Deployment milestone" in deployed.body
    assert "already selected and deployed" in deployed.body


def test_blocked_report_cannot_claim_a_mergeable_check() -> None:
    report = OptimizationReport(
        issue_number=31,
        candidate_id="candidate-1",
        recommendation="Merge immediately.",
        alternatives=(),
        baseline_metrics={"quality": 0.5},
        candidate_metrics={"quality": 0.9},
        guardrails={"safety": "fail"},
        thresholds={"quality": 0.8},
        materiality={"quality": 0.05},
        sample_count=5,
        split="development",
        foundry_operations=(),
        changed_paths=("agent/main.py",),
        validation=("tests: failed",),
        spec_sha256="1" * 64,
        base_commit="2" * 40,
        patch_sha256="3" * 64,
        evidence_sha256="4" * 64,
        bundle_sha256="5" * 64,
        expected_tree="6" * 40,
        required_checks={"Foundry exact candidate check": "failure"},
        merge_gate=EvidenceMergeGate.BLOCKED,
    )

    renderer = PublicEvidenceRenderer()
    projection = renderer.render_pr(report)
    check = renderer.render_check(report)

    assert projection.draft is True
    assert "Merge this PR to select" not in projection.body
    assert "Do not merge this PR" in projection.body
    assert check.status == "completed"
    assert check.conclusion == "failure"


def test_omitted_merge_gate_fails_closed() -> None:
    report = OptimizationReport(
        issue_number=31,
        candidate_id="candidate-1",
        recommendation="Use the candidate.",
        alternatives=(),
        baseline_metrics={"quality": 0.5},
        candidate_metrics={"quality": 0.9},
        guardrails={"safety": "pass"},
        thresholds={"quality": 0.8},
        materiality={"quality": 0.05},
        sample_count=5,
        split="development",
        foundry_operations=(),
        changed_paths=("agent/main.py",),
        validation=("tests: passed",),
        spec_sha256="1" * 64,
        base_commit="2" * 40,
        patch_sha256="3" * 64,
        evidence_sha256="4" * 64,
        bundle_sha256="5" * 64,
        expected_tree="6" * 40,
        required_checks={"Foundry exact candidate check": "success"},
    )

    renderer = PublicEvidenceRenderer()
    projection = renderer.render_pr(report)
    check = renderer.render_check(report)

    assert report.merge_gate is EvidenceMergeGate.PENDING
    assert projection.draft is True
    assert "Do not merge this PR" in projection.body
    assert check.status == "in_progress"
    assert check.conclusion is None


def test_eligible_merge_gate_rejects_failed_required_checks() -> None:
    with pytest.raises(ValueError, match="required checks"):
        OptimizationReport(
            issue_number=31,
            candidate_id="candidate-1",
            recommendation="Use the candidate.",
            alternatives=(),
            baseline_metrics={"quality": 0.5},
            candidate_metrics={"quality": 0.9},
            guardrails={"safety": "pass"},
            thresholds={"quality": 0.8},
            materiality={"quality": 0.05},
            sample_count=5,
            split="development",
            foundry_operations=(),
            changed_paths=("agent/main.py",),
            validation=("tests: passed",),
            spec_sha256="1" * 64,
            base_commit="2" * 40,
            patch_sha256="3" * 64,
            evidence_sha256="4" * 64,
            bundle_sha256="5" * 64,
            expected_tree="6" * 40,
            required_checks={
                "Foundry exact candidate check": "success",
                "tests": "failure",
            },
            merge_gate=EvidenceMergeGate.ELIGIBLE,
        )


def test_changed_paths_reject_markdown_injection() -> None:
    with pytest.raises(ValueError, match="repository path"):
        OptimizationReport(
            issue_number=31,
            candidate_id="candidate-1",
            recommendation="Use the candidate.",
            alternatives=(),
            baseline_metrics={"quality": 0.5},
            candidate_metrics={"quality": 0.9},
            guardrails={"safety": "pass"},
            thresholds={"quality": 0.8},
            materiality={"quality": 0.05},
            sample_count=5,
            split="development",
            foundry_operations=(),
            changed_paths=("agent.py`\n\n## Forged approval",),
            validation=("tests: passed",),
            spec_sha256="1" * 64,
            base_commit="2" * 40,
            patch_sha256="3" * 64,
            evidence_sha256="4" * 64,
            bundle_sha256="5" * 64,
            expected_tree="6" * 40,
        )
