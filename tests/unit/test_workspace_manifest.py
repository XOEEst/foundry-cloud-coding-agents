import base64
import hashlib

import pytest

from foundry_opt.orchestration import (
    parse_workspace_candidate_manifest,
    parse_workspace_experiment_manifest,
)


def _candidate() -> dict:
    patch = b"diff --git a/agent.py b/agent.py\n"
    return {
        "candidate_id": "candidate-1",
        "mutation_class": "instructions",
        "patch_base64": base64.b64encode(patch).decode("ascii"),
        "summary": "Improve policy coverage.",
    }


def _payload() -> dict:
    return {
        "schema_version": 4,
        "issue_number": 31,
        "target": "support-agent",
        "base_commit": "a" * 40,
        "candidates": [_candidate()],
    }


def test_manifest_contains_only_untrusted_candidate_proposal() -> None:
    manifest = parse_workspace_experiment_manifest(_payload())
    proposal = manifest.candidates[0]

    assert proposal.patch_sha256 == hashlib.sha256(
        proposal.exact_patch
    ).hexdigest()
    assert proposal.mutation_class == "instructions"
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
        "patch_sha256",
        "changed_paths",
        "validation",
        "expected_tree",
        "idempotency_key",
        "experiment_reference",
        "foundry_operations",
    ),
)
def test_manifest_rejects_model_supplied_result_fields(
    forged_field: str,
) -> None:
    payload = _payload()
    payload["candidates"][0][forged_field] = {"quality": 99.0}

    with pytest.raises(ValueError, match="candidate fields"):
        parse_workspace_experiment_manifest(payload)


@pytest.mark.parametrize(
    "forged_field",
    (
        "report_context",
        "spec_sha256",
        "baseline_metrics",
        "thresholds",
        "asset_ids",
        "policy",
    ),
)
def test_manifest_rejects_model_supplied_trust_context(
    forged_field: str,
) -> None:
    payload = _payload()
    payload[forged_field] = {"quality": 99.0}

    with pytest.raises(ValueError, match="manifest fields"):
        parse_workspace_experiment_manifest(payload)


def test_single_candidate_manifest_uses_same_proposal_contract() -> None:
    payload = {
        "schema_version": 3,
        "issue_number": 31,
        "target": "support-agent",
        "base_commit": "a" * 40,
        "candidate": _candidate(),
    }

    manifest = parse_workspace_candidate_manifest(payload)

    assert manifest.candidate.candidate_id == "candidate-1"
