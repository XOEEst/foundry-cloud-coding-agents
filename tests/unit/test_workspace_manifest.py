import base64
import hashlib
import json

import pytest

from foundry_opt.orchestration import (
    parse_workspace_candidate_manifest,
    parse_workspace_experiment_manifest,
    WorkspaceCandidateProvenance,
    WorkspaceCandidateWorkContract,
    WorkspaceNextAction,
    WorkspaceNextActionKind,
    WorkspacePriorExperiment,
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


def test_candidate_action_exposes_an_executable_manifest_contract() -> None:
    action = WorkspaceNextAction(
        kind=WorkspaceNextActionKind.RUN_CANDIDATE_EXPERIMENTS,
        issue_number=31,
        workspace_pull_request_number=104,
        trigger=None,
        candidate_work=WorkspaceCandidateWorkContract(
            issue_number=31,
            target="support-agent",
            base_commit="a" * 40,
            candidate_id="candidate-2",
            candidate_number=2,
            candidate_limit=2,
            allowed_mutations=("instructions",),
            prior_experiments=(
                WorkspacePriorExperiment(
                    candidate_id="candidate-1",
                    mutation_class="instructions",
                    metrics={"policy_coverage": 0.5},
                    guardrails={"advisory_safety": "pass"},
                    changed_paths=("agent.py",),
                ),
            ),
        ),
    )

    contract = action.to_dict()["candidate_work"]

    assert contract["manifest_schema_version"] == 3
    assert contract["candidate_id"] == "candidate-2"
    assert contract["candidate_number"] == 2
    assert contract["candidate_limit"] == 2
    assert contract["allowed_mutations"] == ["instructions"]
    assert contract["prior_experiments"] == [
        {
            "candidate_id": "candidate-1",
            "changed_paths": ["agent.py"],
            "guardrails": {"advisory_safety": "pass"},
            "metrics": {"policy_coverage": 0.5},
            "mutation_class": "instructions",
        }
    ]
    assert contract["command"] == (
        "foundry-opt workspace experiment --issue 31 "
        "--candidate-manifest <manifest.json> --json"
    )


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
    assert manifest.provenance is None


def test_private_candidate_manifest_v4_requires_and_parses_provenance() -> None:
    provenance = WorkspaceCandidateProvenance(
        copilot_actor_id=198982749,
        copilot_actor_login="Copilot",
        candidate_source_commit_sha="b" * 40,
        candidate_source_commit_url=(
            "https://github.com/octo-org/optimizer/commit/" + "b" * 40
        ),
        acknowledgement_comment_id=None,
        acknowledgement_comment_url=None,
        assignment_marker_key="issue-31:assignment-a1:v1",
        workspace_pr_number=104,
        importer_workflow_run_id=9001,
        importer_workflow_run_url=(
            "https://github.com/octo-org/optimizer/actions/runs/9001"
        ),
        trusted_event_name="schedule",
    )
    payload = {
        "schema_version": 4,
        "issue_number": 31,
        "target": "support-agent",
        "base_commit": "a" * 40,
        "candidate": _candidate(),
        "provenance": json.loads(provenance.canonical_json),
    }

    manifest = parse_workspace_candidate_manifest(payload)

    assert manifest.provenance == provenance
