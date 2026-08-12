import base64
import hashlib

import pytest

from foundry_opt.evaluation import (
    EvaluationPolicy,
    MetricDirection,
    MetricPolicy,
)
from foundry_opt.orchestration import (
    PreparedCandidateResultRunner,
    parse_workspace_experiment_manifest,
)


def _policy() -> EvaluationPolicy:
    return EvaluationPolicy(
        (
            MetricPolicy(
                "quality",
                MetricDirection.MAXIMIZE,
                0.8,
                0.05,
            ),
        )
    )


def _payload() -> dict:
    patch = b"diff --git a/agent.py b/agent.py\n"
    return {
        "schema_version": 1,
        "issue_number": 31,
        "target": "support-agent",
        "base_commit": "a" * 40,
        "report_context": {
            "baseline_metrics": {"quality": 0.8},
            "sample_count": 12,
            "split": "development",
            "spec_sha256": "d" * 64,
        },
        "candidates": [
            {
                "candidate_id": "candidate-1",
                "idempotency_key": "e" * 64,
                "patch_base64": base64.b64encode(patch).decode("ascii"),
                "bundle_sha256": "b" * 64,
                "evidence_sha256": "c" * 64,
                "summary": "Improve policy coverage.",
                "changed_paths": ["agent.py"],
                "validation": ["pytest: passed"],
                "expected_tree": "f" * 40,
                "foundry_operations": [],
                "result": {
                    "executor": "actions",
                    "metrics": {"quality": 0.9},
                    "guardrails": {"safety": "pass"},
                    "draft_id": "draft-1",
                    "evaluation_id": "evaluation-1",
                    "run_id": "run-1",
                    "operation_sha256": "1" * 64,
                },
            }
        ],
    }


def test_manifest_preserves_exact_patch_and_real_result_lineage() -> None:
    manifest = parse_workspace_experiment_manifest(
        _payload(),
        policy=_policy(),
    )
    candidate = manifest.candidates[0]

    assert candidate.experiment.patch_sha256 == hashlib.sha256(
        candidate.exact_patch
    ).hexdigest()
    assert candidate.experiment_result.bundle_sha256 == "b" * 64
    assert candidate.experiment_result.evidence_sha256 == "c" * 64
    assert (
        PreparedCandidateResultRunner(manifest.candidates).evaluate(
            candidate.experiment
        )
        == candidate.experiment_result
    )


def test_manifest_rejects_non_public_experiment_rows() -> None:
    payload = _payload()
    payload["candidates"][0]["raw_rows"] = [{"prompt": "private"}]

    with pytest.raises(ValueError, match="candidate fields"):
        parse_workspace_experiment_manifest(payload, policy=_policy())
