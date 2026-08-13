import base64
import hashlib

import pytest

from foundry_opt.evaluation import (
    EvaluationPolicy,
    MetricDirection,
    MetricPolicy,
)
from foundry_opt.orchestration import (
    parse_workspace_candidate_manifest,
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


def _candidate() -> dict:
    patch = b"diff --git a/agent.py b/agent.py\n"
    return {
        "candidate_id": "candidate-1",
        "idempotency_key": "e" * 64,
        "experiment_reference": "target:support-agent",
        "patch_base64": base64.b64encode(patch).decode("ascii"),
        "summary": "Improve policy coverage.",
        "changed_paths": ["agent.py"],
        "validation": ["pytest: passed"],
        "expected_tree": "f" * 40,
    }


def _payload() -> dict:
    return {
        "schema_version": 2,
        "issue_number": 31,
        "target": "support-agent",
        "base_commit": "a" * 40,
        "report_context": {
            "baseline_metrics": {"quality": 0.8},
            "sample_count": 12,
            "split": "development",
            "spec_sha256": "d" * 64,
        },
        "candidates": [_candidate()],
    }


def test_manifest_contains_only_untrusted_candidate_proposal() -> None:
    manifest = parse_workspace_experiment_manifest(
        _payload(),
        policy=_policy(),
    )
    proposal = manifest.candidates[0]

    assert proposal.patch_sha256 == hashlib.sha256(
        proposal.exact_patch
    ).hexdigest()
    assert proposal.experiment_reference == "target:support-agent"
    assert not hasattr(proposal, "experiment_result")


@pytest.mark.parametrize(
    "forged_field",
    (
        "metrics",
        "guardrails",
        "result",
        "bundle_sha256",
        "evidence_sha256",
        "draft_id",
        "evaluation_id",
        "run_id",
        "executor",
        "required_checks",
    ),
)
def test_manifest_rejects_model_supplied_result_fields(
    forged_field: str,
) -> None:
    payload = _payload()
    payload["candidates"][0][forged_field] = {"quality": 99.0}

    with pytest.raises(ValueError, match="candidate fields"):
        parse_workspace_experiment_manifest(payload, policy=_policy())


def test_single_candidate_manifest_uses_same_proposal_contract() -> None:
    payload = {
        "schema_version": 2,
        "issue_number": 31,
        "target": "support-agent",
        "base_commit": "a" * 40,
        "candidate": _candidate(),
    }

    manifest = parse_workspace_candidate_manifest(payload)

    assert manifest.candidate.candidate_id == "candidate-1"
