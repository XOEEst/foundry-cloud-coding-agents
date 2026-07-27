from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

from foundry_opt.deployment import (
    DeployedRuntime,
    DeploymentRecord,
    DeploymentTrigger,
    DeploymentVerificationRequest,
    DeploymentVerificationStatus,
    DeploymentWorkflow,
    DeploymentWorkflowRun,
    WorkflowRunStatus,
    verify_deployed_selection,
)
from foundry_opt.packaging import BundleRequest, build_source_bundle


SHA = "a" * 64
TREE = "b" * 40
EVIDENCE = "c" * 64
COMMIT = "d" * 40
RUN_URL = "https://github.com/octo-org/agents/actions/runs/123"
PORTAL_URL = (
    "https://ai.azure.com/projects/demo/agents/demo-agent/versions/8"
)


def _request(tmp_path: Path) -> DeploymentVerificationRequest:
    repository = tmp_path / "repo"
    repository.mkdir()
    patch = repository / "artifacts/candidate.patch"
    patch.parent.mkdir()
    patch.write_bytes(b"exact patch\n")
    patch_sha = hashlib.sha256(patch.read_bytes()).hexdigest()
    evidence = repository / "evidence/result.json"
    evidence.parent.mkdir()
    evidence.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "campaign_id": "campaign-1",
                "source_hash": "0" * 64,
                "candidates": [
                    {
                        "subject_id": "candidate-1",
                        "patch_hash": patch_sha,
                    }
                ],
                "pareto": {"eligible_ids": ["candidate-1"]},
            }
        ),
        encoding="utf-8",
    )
    evidence_sha = hashlib.sha256(evidence.read_bytes()).hexdigest()
    source = tmp_path / "selected-source"
    source.mkdir()
    (source / "main.py").write_text("print('selected')\n", encoding="utf-8")
    bundle = build_source_bundle(
        BundleRequest(source, tmp_path / "selected.zip")
    )
    workflow = DeploymentWorkflow(
        path=Path(".github/workflows/deploy.yml"),
        trigger=DeploymentTrigger.MERGE,
        exists=True,
        name="Deploy Foundry",
    )
    record = DeploymentRecord(
        project_endpoint=(
            "https://example.services.ai.azure.com/api/projects/demo"
        ),
        agent_name="demo-agent",
        version=8,
        base_version=7,
        sha256=bundle.sha256,
        patch_sha256=patch_sha,
        tree_hash=TREE,
        evidence_sha256=evidence_sha,
        status="creating",
        portal_url=PORTAL_URL,
    )
    return DeploymentVerificationRequest(
        repository_root=repository,
        candidate_id="candidate-1",
        patch_path=Path("artifacts/candidate.patch"),
        expected_patch_sha256=patch_sha,
        expected_tree_hash=TREE,
        deployed_tree_hash=TREE,
        evidence_path=Path("evidence/result.json"),
        expected_evidence_sha256=evidence_sha,
        bundle=bundle,
        expected_bundle_sha256=bundle.sha256,
        expected_version=8,
        expected_commit=COMMIT,
        expected_run_url=RUN_URL,
        expected_portal_url=PORTAL_URL,
        record=record,
        workflow=workflow,
        workflow_run=DeploymentWorkflowRun(
            path=workflow.path,
            trigger=DeploymentTrigger.MERGE,
            status=WorkflowRunStatus.SUCCESS,
            head_commit=COMMIT,
            url=RUN_URL,
        ),
        runtime=DeployedRuntime(
            agent_name="demo-agent",
            deployed_version=8,
            latest_version=8,
            source_sha256=bundle.sha256,
            portal_url=PORTAL_URL,
        ),
    )


def test_verify_deployed_selection_requires_every_exact_identity(
    tmp_path: Path,
) -> None:
    verification = verify_deployed_selection(_request(tmp_path))

    assert verification.verified is True
    assert verification.status is DeploymentVerificationStatus.VERIFIED
    assert verification.version == 8
    assert verification.sha256
    assert verification.run_url == RUN_URL
    assert verification.portal_url == PORTAL_URL
    assert all(check.passed for check in verification.checks)


def test_verify_deployed_selection_reports_manual_trigger_required(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    request = replace(
        request,
        workflow=replace(
            request.workflow,
            trigger=DeploymentTrigger.MANUAL,
        ),
        workflow_run=None,
        runtime=None,
    )

    verification = verify_deployed_selection(request)

    assert verification.verified is False
    assert (
        verification.status
        is DeploymentVerificationStatus.MANUAL_TRIGGER_REQUIRED
    )


def test_verify_deployed_selection_reports_merge_deployment_pending(
    tmp_path: Path,
) -> None:
    request = replace(
        _request(tmp_path),
        workflow_run=None,
        runtime=None,
    )

    verification = verify_deployed_selection(request)

    assert verification.verified is False
    assert (
        verification.status
        is DeploymentVerificationStatus.MERGE_DEPLOYMENT_PENDING
    )


def test_verify_deployed_selection_rejects_deployed_version_mismatch(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    request = replace(
        request,
        runtime=replace(
            request.runtime,
            deployed_version=9,
            latest_version=9,
        ),
    )

    verification = verify_deployed_selection(request)

    assert verification.verified is False
    assert verification.status is DeploymentVerificationStatus.MISMATCH
    assert "published_version_identity" in verification.failed_checks


def test_verify_deployed_selection_rejects_hash_or_link_mismatch(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    patch = request.repository_root / request.patch_path
    patch.unlink()
    patch.write_bytes(b"different patch\n")
    request = replace(
        request,
        runtime=replace(request.runtime, source_sha256="0" * 64),
        workflow_run=replace(
            request.workflow_run,
            url="https://github.com/octo-org/agents/actions/runs/999",
        ),
    )

    verification = verify_deployed_selection(request)

    assert verification.verified is False
    assert verification.status is DeploymentVerificationStatus.MISMATCH
    assert {
        "patch_sha256",
        "deployed_source_sha256",
        "workflow_run_url",
    }.issubset(verification.failed_checks)


def test_verify_deployed_selection_does_not_fallback_on_failed_run(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    request = replace(
        request,
        workflow_run=replace(
            request.workflow_run,
            status=WorkflowRunStatus.FAILURE,
        ),
    )

    verification = verify_deployed_selection(request)

    assert verification.verified is False
    assert (
        verification.status
        is DeploymentVerificationStatus.WORKFLOW_FAILED
    )
