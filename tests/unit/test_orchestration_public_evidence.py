from foundry_opt.orchestration import (
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
            "candidate-1: coverage 0.3; rejected as less effective",
            "candidate-2: coverage 0.5; selected",
        ),
        baseline_metrics={"advisory_safety": 1.0, "policy_coverage": 0.0},
        candidate_metrics={"advisory_safety": 1.0, "policy_coverage": 0.5},
        guardrails={"advisory_safety": "pass"},
        thresholds={"policy_coverage": 0.5},
        sample_count=6,
        split="development",
        foundry_operations=(
            FoundryOperation(
                kind="evaluation",
                identifier="evalrun-123",
                url="https://ai.azure.com/evaluations/evalrun-123",
                status="completed",
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
    )

    projection = PublicEvidenceRenderer().render_pr(report)

    assert projection.title == "[Optimize] #31 selected candidate"
    assert projection.draft is False
    for heading in (
        "## Copilot recommendation",
        "## Alternatives tested",
        "## Evaluation improvement",
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
    assert "[`evalrun-123`](https://ai.azure.com/evaluations/evalrun-123)" in (
        projection.body
    )
    assert "Merge this PR to select and deploy `candidate-2`." in projection.body


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
