from __future__ import annotations

from dataclasses import fields, replace
import json

import pytest

from foundry_opt.deployment import (
    DeploymentLineageMismatchError,
    DeploymentRecord,
    DeploymentRequest,
    DeploymentVerificationRequest,
    OptimizationDeploymentLineage,
    optimization_deployment_lineage_document,
    optimization_deployment_lineage_sha256,
    verify_optimization_deployment_lineage,
)
from foundry_opt.evaluation import (
    AgentVersionRef,
    EvaluationPolicy,
    MetricDirection,
    MetricPolicy,
)
from foundry_opt.packaging import BundleArtifact
from pathlib import Path


PATCH_SHA = "a" * 64
SPEC_SHA = "b" * 64
EVIDENCE_SHA = "c" * 64
BASELINE_SHA = "d" * 64
BUNDLE_SHA = "e" * 64
TREE = "f" * 40
COMMIT = "1" * 40
PROJECT_ENDPOINT = "https://example.services.ai.azure.com/api/projects/demo"


def _lineage(**overrides: object) -> OptimizationDeploymentLineage:
    defaults: dict[str, object] = {
        "parent_issue_number": 42,
        "spec_sha256": SPEC_SHA,
        "campaign_id": "campaign-1",
        "campaign_pull_request_number": 10,
        "candidate_issue_number": 11,
        "candidate_pull_request_number": 12,
        "candidate_id": "candidate-1",
        "selected_draft_id": "draft-candidate-1",
        "patch_sha256": PATCH_SHA,
        "evidence_sha256": EVIDENCE_SHA,
        "selected_tree_sha": TREE,
        "selected_merge_commit": COMMIT,
    }
    defaults.update(overrides)
    return OptimizationDeploymentLineage(**defaults)


def _bundle(sha256: str = BUNDLE_SHA) -> BundleArtifact:
    return BundleArtifact(
        path=Path("bundle.zip"),
        sha256=sha256,
        included_files=("main.py",),
        excluded_files=(),
        byte_size=10,
        manifest_path=Path("manifest.json"),
    )


def _request_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "project_endpoint": PROJECT_ENDPOINT,
        "agent_name": "demo-agent",
        "base_version": 7,
        "expected_baseline_source_sha256": BASELINE_SHA,
        "bundle": _bundle(),
        "runtime": "python_3_13",
        "entry_point": ("python", "main.py"),
        "dependency_resolution": "remote_build",
        "patch_sha256": PATCH_SHA,
        "tree_hash": TREE,
        "evidence_sha256": EVIDENCE_SHA,
    }
    kwargs.update(overrides)
    return kwargs


def _record_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "project_endpoint": PROJECT_ENDPOINT,
        "agent_name": "demo-agent",
        "version": 8,
        "base_version": 7,
        "baseline_source_sha256": BASELINE_SHA,
        "sha256": BUNDLE_SHA,
        "patch_sha256": PATCH_SHA,
        "tree_hash": TREE,
        "evidence_sha256": EVIDENCE_SHA,
        "runtime": "python_3_13",
        "entry_point": ("python", "main.py"),
        "dependency_resolution": "remote_build",
    }
    kwargs.update(overrides)
    return kwargs


# --- OptimizationDeploymentLineage: strict field validation ----------------


def test_lineage_round_trip() -> None:
    lineage = _lineage()

    document = optimization_deployment_lineage_document(lineage)
    digest_one = optimization_deployment_lineage_sha256(lineage)
    digest_two = optimization_deployment_lineage_sha256(_lineage())

    assert set(document) == {
        "parent_issue_number",
        "spec_sha256",
        "campaign_id",
        "campaign_pull_request_number",
        "candidate_issue_number",
        "candidate_pull_request_number",
        "candidate_id",
        "selected_draft_id",
        "patch_sha256",
        "evidence_sha256",
        "selected_tree_sha",
        "selected_merge_commit",
    }
    assert all(isinstance(value, str) for value in document.values())
    # Round-trips through JSON without loss (only strings/numbers-as-strings).
    assert json.loads(json.dumps(dict(document), sort_keys=True)) == dict(
        document
    )
    assert digest_one == digest_two
    assert len(digest_one) == 64
    assert replace(lineage) == lineage


def test_lineage_digest_changes_when_any_field_changes() -> None:
    baseline = optimization_deployment_lineage_sha256(_lineage())
    for field_def in fields(OptimizationDeploymentLineage):
        overrides: dict[str, object]
        if field_def.name in {
            "parent_issue_number",
            "campaign_pull_request_number",
            "candidate_issue_number",
            "candidate_pull_request_number",
        }:
            overrides = {field_def.name: 999}
        elif field_def.name in {"spec_sha256", "evidence_sha256"}:
            overrides = {field_def.name: "9" * 64}
        elif field_def.name == "patch_sha256":
            overrides = {field_def.name: "9" * 64}
        elif field_def.name == "selected_tree_sha":
            overrides = {field_def.name: "9" * 40}
        elif field_def.name == "selected_merge_commit":
            overrides = {field_def.name: "9" * 40}
        else:
            overrides = {field_def.name: "changed-value"}
        changed = optimization_deployment_lineage_sha256(_lineage(**overrides))
        assert changed != baseline, field_def.name


def test_lineage_has_no_raw_content_fields() -> None:
    # Guards against ever widening this contract to carry raw prompts,
    # responses, or dataset rows: every field must be a hash, id, or number.
    assert {field_def.name for field_def in fields(OptimizationDeploymentLineage)} == {
        "parent_issue_number",
        "spec_sha256",
        "campaign_id",
        "campaign_pull_request_number",
        "candidate_issue_number",
        "candidate_pull_request_number",
        "candidate_id",
        "selected_draft_id",
        "patch_sha256",
        "evidence_sha256",
        "selected_tree_sha",
        "selected_merge_commit",
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"parent_issue_number": 0},
        {"parent_issue_number": -1},
        {"parent_issue_number": True},
        {"campaign_pull_request_number": 0},
        {"candidate_issue_number": 0},
        {"candidate_pull_request_number": 0},
        {"spec_sha256": "not-a-hash"},
        {"spec_sha256": "A" * 64},
        {"evidence_sha256": "0" * 63},
        {"patch_sha256": "0" * 65},
        {"campaign_id": ""},
        {"campaign_id": "bad/campaign"},
        {"candidate_id": "-leading-dash"},
        {"selected_draft_id": "has space"},
        {"selected_tree_sha": "not-a-tree"},
        {"selected_tree_sha": "0" * 39},
        {"selected_merge_commit": "0" * 39},
        {"selected_merge_commit": "G" * 40},
    ],
)
def test_lineage_rejects_invalid_fields(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _lineage(**overrides)


def test_lineage_document_rejects_non_lineage_input() -> None:
    with pytest.raises(ValueError):
        optimization_deployment_lineage_document(object())  # type: ignore[arg-type]


# --- DeploymentRequest: optional lineage, strict cross-checks ---------------


def test_deployment_request_accepts_matching_lineage() -> None:
    lineage = _lineage()
    request = DeploymentRequest(**_request_kwargs(lineage=lineage))

    assert request.lineage == lineage


def test_deployment_request_defaults_lineage_to_none() -> None:
    request = DeploymentRequest(**_request_kwargs())

    assert request.lineage is None
    assert request.lineage_sha256 is None


def test_deployment_request_accepts_persisted_lineage_digest() -> None:
    request = DeploymentRequest(
        **_request_kwargs(lineage_sha256="9" * 64)
    )

    assert request.lineage is None
    assert request.lineage_sha256 == "9" * 64


@pytest.mark.parametrize(
    "lineage_sha256",
    ["", "9" * 63, "A" * 64, "not-a-digest"],
)
def test_deployment_request_rejects_invalid_persisted_lineage_digest(
    lineage_sha256: str,
) -> None:
    with pytest.raises(ValueError):
        DeploymentRequest(
            **_request_kwargs(lineage_sha256=lineage_sha256)
        )


def test_deployment_request_rejects_conflicting_lineage_digests() -> None:
    with pytest.raises(ValueError, match="lineage digests"):
        DeploymentRequest(
            **_request_kwargs(
                lineage=_lineage(),
                lineage_sha256="9" * 64,
            )
        )


def test_deployment_request_accepts_matching_lineage_digests() -> None:
    lineage = _lineage()
    digest = optimization_deployment_lineage_sha256(lineage)

    request = DeploymentRequest(
        **_request_kwargs(
            lineage=lineage,
            lineage_sha256=digest,
        )
    )

    assert request.lineage is lineage
    assert request.lineage_sha256 == digest


@pytest.mark.parametrize(
    "field_name",
    ["patch_sha256", "selected_tree_sha", "evidence_sha256"],
)
def test_deployment_request_rejects_mismatched_lineage(field_name: str) -> None:
    mismatched = replace(_lineage(), **{field_name: _other_hash(field_name)})
    with pytest.raises(ValueError):
        DeploymentRequest(**_request_kwargs(lineage=mismatched))


def _other_hash(field_name: str) -> str:
    if field_name == "selected_tree_sha":
        return "9" * 40
    return "9" * 64


def test_deployment_request_reserves_lineage_metadata_key_when_present() -> None:
    with pytest.raises(ValueError):
        DeploymentRequest(
            **_request_kwargs(
                lineage=_lineage(),
                metadata={"foundry-opt-lineage-sha256": "x"},
            )
        )


def test_deployment_request_reserves_lineage_metadata_key_when_absent() -> None:
    with pytest.raises(ValueError):
        DeploymentRequest(
            **_request_kwargs(
                metadata={"foundry-opt-lineage-sha256": "x"}
            )
        )


def test_deployment_request_enforces_lineage_metadata_slot_budget() -> None:
    with pytest.raises(ValueError, match="entry budget"):
        DeploymentRequest(
            **_request_kwargs(
                lineage=_lineage(),
                metadata={f"caller-{index}": "value" for index in range(10)},
            )
        )


# --- DeploymentRecord: optional lineage, strict cross-checks ----------------


def test_deployment_record_accepts_matching_lineage() -> None:
    lineage = _lineage()
    record = DeploymentRecord(**_record_kwargs(lineage=lineage))

    assert record.lineage == lineage


@pytest.mark.parametrize(
    "field_name",
    ["patch_sha256", "selected_tree_sha", "evidence_sha256"],
)
def test_deployment_record_rejects_mismatched_lineage(field_name: str) -> None:
    mismatched = replace(_lineage(), **{field_name: _other_hash(field_name)})
    with pytest.raises(ValueError):
        DeploymentRecord(**_record_kwargs(lineage=mismatched))


def test_deployment_record_rejects_non_lineage_type() -> None:
    with pytest.raises(ValueError):
        DeploymentRecord(**_record_kwargs(lineage="not-a-lineage"))


# --- verify_optimization_deployment_lineage: stable typed errors -----------


def test_verify_optimization_deployment_lineage_passes_on_exact_match() -> None:
    verify_optimization_deployment_lineage(_lineage(), _lineage())


def test_verify_optimization_deployment_lineage_raises_on_mismatch() -> None:
    with pytest.raises(DeploymentLineageMismatchError):
        verify_optimization_deployment_lineage(
            _lineage(parent_issue_number=1),
            _lineage(parent_issue_number=2),
        )


def test_verify_optimization_deployment_lineage_raises_when_actual_missing() -> None:
    with pytest.raises(DeploymentLineageMismatchError):
        verify_optimization_deployment_lineage(None, _lineage())


# --- DeploymentVerificationRequest: expected_lineage cross-checks ----------


RUN_URL = "https://github.com/octo-org/agents/actions/runs/123"
PORTAL_URL = "https://ai.azure.com/projects/demo/agents/demo-agent/versions/8"


def _verification_kwargs(**overrides: object) -> dict[str, object]:
    lineage = _lineage()
    record = DeploymentRecord(**_record_kwargs(lineage=lineage))
    kwargs: dict[str, object] = {
        "repository_root": Path("repo"),
        "candidate_id": "candidate-1",
        "patch_path": Path("artifacts/candidate.patch"),
        "expected_patch_sha256": PATCH_SHA,
        "expected_base_commit": "2" * 40,
        "expected_baseline_source_sha256": BASELINE_SHA,
        "expected_tree_hash": TREE,
        "deployed_tree_hash": TREE,
        "evidence_path": Path("evidence/result.json"),
        "expected_evidence_sha256": EVIDENCE_SHA,
        "expected_campaign_id": "campaign-1",
        "expected_baseline_subject_id": "baseline-1",
        "baseline_bundle": _bundle(BASELINE_SHA),
        "expected_baseline_bundle_sha256": BASELINE_SHA,
        "bundle": _bundle(BUNDLE_SHA),
        "expected_bundle_sha256": BUNDLE_SHA,
        "expected_project_endpoint": PROJECT_ENDPOINT,
        "expected_agent_name": "demo-agent",
        "expected_base_version": 7,
        "expected_version": 8,
        "expected_runtime": "python_3_13",
        "expected_entry_point": ("python", "main.py"),
        "expected_dependency_resolution": "remote_build",
        "expected_baseline_agent": AgentVersionRef(
            agent_id="demo-agent",
            draft_id="draft-baseline-1",
            version="draft-baseline-1",
        ),
        "expected_candidate_agents": {
            "candidate-1": AgentVersionRef(
                agent_id="demo-agent",
                draft_id="draft-candidate-1",
                version="draft-candidate-1",
            )
        },
        "expected_metric_policy": EvaluationPolicy(
            metrics=(
                MetricPolicy(
                    name="quality",
                    direction=MetricDirection.MAXIMIZE,
                    threshold=0.4,
                    materiality=0.1,
                ),
            )
        ),
        "expected_commit": COMMIT,
        "expected_run_url": RUN_URL,
        "expected_portal_url": PORTAL_URL,
        "record": record,
        "workflow": _workflow(),
        "workflow_run": None,
        "runtime": None,
        "bundle_exclude": ("artifacts/**", "evidence/**"),
        "expected_lineage": lineage,
    }
    kwargs.update(overrides)
    return kwargs


def _workflow():
    from foundry_opt.deployment import DeploymentTrigger, DeploymentWorkflow

    return DeploymentWorkflow(
        path=Path(".github/workflows/deploy.yml"),
        trigger=DeploymentTrigger.MERGE,
        exists=True,
        name="Deploy Foundry",
    )


def test_verification_request_accepts_matching_expected_lineage() -> None:
    request = DeploymentVerificationRequest(**_verification_kwargs())

    assert request.expected_lineage == _lineage()


def test_verification_request_defaults_expected_lineage_to_none() -> None:
    kwargs = _verification_kwargs()
    del kwargs["expected_lineage"]
    request = DeploymentVerificationRequest(**kwargs)

    assert request.expected_lineage is None


@pytest.mark.parametrize(
    "override",
    [
        {"candidate_id": "other-candidate"},
        {"campaign_id": "other-campaign"},
        {"patch_sha256": "9" * 64},
        {"selected_tree_sha": "9" * 40},
        {"evidence_sha256": "9" * 64},
        {"selected_merge_commit": "9" * 40},
        {"selected_draft_id": "draft-other"},
    ],
)
def test_verification_request_rejects_mismatched_expected_lineage(
    override: dict[str, object],
) -> None:
    mismatched = replace(_lineage(), **override)
    kwargs = _verification_kwargs()
    kwargs["expected_lineage"] = mismatched
    with pytest.raises(ValueError):
        DeploymentVerificationRequest(**kwargs)
