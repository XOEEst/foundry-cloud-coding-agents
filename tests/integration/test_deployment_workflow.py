from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

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


RUN_URL = "https://github.com/octo-org/agents/actions/runs/123"
PORTAL_URL = (
    "https://ai.azure.com/projects/demo/agents/demo-agent/versions/8"
)
PROJECT_ENDPOINT = "https://example.services.ai.azure.com/api/projects/demo"


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _evidence_result(
    subject_id: str,
    *,
    patch_sha256: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "subject_id": subject_id,
        "agent": {
            "agent_id": f"agent-{subject_id}",
            "draft_id": f"draft-{subject_id}",
            "version": "1",
        },
        "dataset": {"dataset_id": "dataset-1", "version": "1"},
        "evaluator": {"definition_id": "evaluator-1", "version": "1"},
        "evaluation_id": f"evaluation-{subject_id}",
        "run_id": f"run-{subject_id}",
        "attempts": [],
        "split": "validation",
        "portal_url": None,
        "complete": True,
        "repeat_count": 0,
        "duration_ms": 1,
        "usage": {
            "input_tokens": 1,
            "output_tokens": 1,
            "cached_tokens": 0,
        },
        "error_count": 0,
        "metrics": {},
        "cases": [],
    }
    if patch_sha256 is not None:
        result["patch_hash"] = patch_sha256
    return result


def _request(tmp_path: Path) -> DeploymentVerificationRequest:
    repository = tmp_path.resolve() / "repo"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.email", "tests@example.invalid")
    _git(repository, "config", "user.name", "Deployment Tests")
    (repository / "main.py").write_text(
        "print('base')\n",
        encoding="utf-8",
    )
    _git(repository, "add", "main.py")
    _git(repository, "commit", "-m", "base")
    base_commit = _git(repository, "rev-parse", "HEAD")
    baseline_bundle = build_source_bundle(
        BundleRequest(
            repository,
            tmp_path / "baseline.zip",
            exclude=("artifacts/**", "evidence/**"),
        )
    )

    (repository / "main.py").write_text(
        "print('selected')\n",
        encoding="utf-8",
    )
    _git(repository, "add", "main.py")
    _git(repository, "commit", "-m", "selected")
    selected_commit = _git(repository, "rev-parse", "HEAD")
    selected_tree = _git(repository, "rev-parse", "HEAD^{tree}")

    patch = repository / "artifacts/candidate.patch"
    patch.parent.mkdir()
    patch.write_bytes(
        subprocess.run(
            ("git", "diff", "--binary", base_commit, selected_commit),
            cwd=repository,
            check=True,
            capture_output=True,
        ).stdout
    )
    patch_sha = hashlib.sha256(patch.read_bytes()).hexdigest()
    evidence = repository / "evidence/result.json"
    evidence.parent.mkdir()
    evidence.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "campaign_id": "campaign-1",
                "source_hash": baseline_bundle.sha256,
                "baseline": _evidence_result("baseline-1"),
                "candidates": [
                    _evidence_result(
                        "candidate-1",
                        patch_sha256=patch_sha,
                    ),
                ],
                "pareto": {
                    "frontier_ids": ["candidate-1"],
                    "eligible_ids": ["candidate-1"],
                    "decisions": [
                        {
                            "subject_id": "candidate-1",
                            "eligible": True,
                            "reason_code": "eligible",
                        }
                    ],
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    evidence_sha = hashlib.sha256(evidence.read_bytes()).hexdigest()
    bundle = build_source_bundle(
        BundleRequest(
            repository,
            tmp_path / "selected.zip",
            exclude=("artifacts/**", "evidence/**"),
        )
    )
    workflow = DeploymentWorkflow(
        path=Path(".github/workflows/deploy.yml"),
        trigger=DeploymentTrigger.MERGE,
        exists=True,
        name="Deploy Foundry",
    )
    record = DeploymentRecord(
        project_endpoint=PROJECT_ENDPOINT,
        agent_name="demo-agent",
        version=8,
        base_version=7,
        sha256=bundle.sha256,
        patch_sha256=patch_sha,
        tree_hash=selected_tree,
        evidence_sha256=evidence_sha,
        status="active",
        portal_url=PORTAL_URL,
        runtime="python_3_13",
        entry_point=("python", "main.py"),
        dependency_resolution="remote_build",
        metadata={
            "foundry-opt-base-version": "7",
            "foundry-opt-source-sha256": bundle.sha256,
            "foundry-opt-patch-sha256": patch_sha,
            "foundry-opt-tree-hash": selected_tree,
            "foundry-opt-evidence-sha256": evidence_sha,
        },
    )
    return DeploymentVerificationRequest(
        repository_root=repository,
        candidate_id="candidate-1",
        patch_path=Path("artifacts/candidate.patch"),
        expected_patch_sha256=patch_sha,
        expected_base_commit=base_commit,
        expected_tree_hash=selected_tree,
        deployed_tree_hash=selected_tree,
        evidence_path=Path("evidence/result.json"),
        expected_evidence_sha256=evidence_sha,
        expected_campaign_id="campaign-1",
        expected_baseline_subject_id="baseline-1",
        baseline_bundle=baseline_bundle,
        expected_baseline_bundle_sha256=baseline_bundle.sha256,
        bundle=bundle,
        expected_bundle_sha256=bundle.sha256,
        bundle_exclude=("artifacts/**", "evidence/**"),
        expected_project_endpoint=PROJECT_ENDPOINT,
        expected_agent_name="demo-agent",
        expected_base_version=7,
        expected_version=8,
        expected_runtime="python_3_13",
        expected_entry_point=("python", "main.py"),
        expected_dependency_resolution="remote_build",
        expected_commit=selected_commit,
        expected_run_url=RUN_URL,
        expected_portal_url=PORTAL_URL,
        record=record,
        workflow=workflow,
        workflow_run=DeploymentWorkflowRun(
            path=workflow.path,
            trigger=DeploymentTrigger.MERGE,
            status=WorkflowRunStatus.SUCCESS,
            head_commit=selected_commit,
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


def test_verify_deployed_selection_reproduces_exact_tree_and_bundle(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)

    verification = verify_deployed_selection(request)

    assert verification.verified is True, verification.failed_checks
    assert verification.status is DeploymentVerificationStatus.VERIFIED
    assert verification.version == 8
    assert verification.sha256 == request.bundle.sha256
    assert verification.run_url == RUN_URL
    assert verification.portal_url == PORTAL_URL
    assert all(check.passed for check in verification.checks)
    assert not list(request.repository_root.glob(".fv-*"))


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
        "reproduced_tree",
        "reproduced_bundle",
        "deployed_source_sha256",
        "workflow_run_url",
    }.issubset(verification.failed_checks)


def test_verify_rejects_self_consistent_hashes_for_wrong_patch(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    patch = request.repository_root / request.patch_path
    patch.write_text(
        "diff --git a/main.py b/main.py\n"
        "--- a/main.py\n"
        "+++ b/main.py\n"
        "@@ -1 +1 @@\n"
        "-print('base')\n"
        "+print('wrong')\n",
        encoding="utf-8",
    )
    patch_sha = hashlib.sha256(patch.read_bytes()).hexdigest()
    evidence = request.repository_root / request.evidence_path
    document = json.loads(evidence.read_text(encoding="utf-8"))
    document["candidates"][0]["patch_hash"] = patch_sha
    evidence.write_text(
        json.dumps(document, sort_keys=True),
        encoding="utf-8",
    )
    evidence_sha = hashlib.sha256(evidence.read_bytes()).hexdigest()
    request = replace(
        request,
        expected_patch_sha256=patch_sha,
        expected_evidence_sha256=evidence_sha,
        record=replace(
            request.record,
            patch_sha256=patch_sha,
            evidence_sha256=evidence_sha,
        ),
    )

    verification = verify_deployed_selection(request)

    assert verification.verified is False
    assert {
        "reproduced_tree",
        "reproduced_bundle",
    }.issubset(verification.failed_checks)


def test_verify_deployed_selection_rejects_pinned_base_mismatch(
    tmp_path: Path,
) -> None:
    request = replace(_request(tmp_path), expected_base_version=6)

    verification = verify_deployed_selection(request)

    assert verification.verified is False
    assert "service_provenance_record" in verification.failed_checks


def test_verify_rejects_nonterminal_published_record(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    request = replace(
        request,
        record=replace(request.record, status="creating"),
    )

    verification = verify_deployed_selection(request)

    assert verification.verified is False
    assert "published_terminal_status" in verification.failed_checks


@pytest.mark.parametrize(
    "record",
    [
        lambda value: replace(value, runtime="python_3_12"),
        lambda value: replace(
            value,
            entry_point=("python", "other.py"),
        ),
        lambda value: replace(
            value,
            dependency_resolution="bundled",
        ),
        lambda value: replace(
            value,
            metadata={
                **value.metadata,
                "foundry-opt-base-version": "6",
            },
        ),
    ],
)
def test_verify_rejects_effective_service_record_mismatch(
    tmp_path: Path,
    record,
) -> None:
    request = _request(tmp_path)
    request = replace(request, record=record(request.record))

    verification = verify_deployed_selection(request)

    assert verification.verified is False
    assert "service_provenance_record" in verification.failed_checks


def test_verify_rejects_workflow_commit_tree_mismatch(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    request = replace(
        request,
        expected_commit=request.expected_base_commit,
        workflow_run=replace(
            request.workflow_run,
            head_commit=request.expected_base_commit,
        ),
    )

    verification = verify_deployed_selection(request)

    assert verification.verified is False
    assert "workflow_commit_tree" in verification.failed_checks


@pytest.mark.parametrize(
    "mutation",
    [
        lambda document: document.update({"source_hash": "0" * 64}),
        lambda document: document["baseline"].update(
            {"subject_id": "wrong-baseline"}
        ),
        lambda document: document.pop("baseline"),
        lambda document: document.update({"unexpected": "field"}),
    ],
)
def test_verify_rejects_invalid_or_unbound_evidence(
    tmp_path: Path,
    mutation,
) -> None:
    request = _request(tmp_path)
    evidence = request.repository_root / request.evidence_path
    document = json.loads(evidence.read_text(encoding="utf-8"))
    mutation(document)
    evidence.write_text(
        json.dumps(document, sort_keys=True),
        encoding="utf-8",
    )
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    request = replace(
        request,
        expected_evidence_sha256=digest,
        record=replace(
            request.record,
            evidence_sha256=digest,
            metadata={
                **request.record.metadata,
                "foundry-opt-evidence-sha256": digest,
            },
        ),
    )

    verification = verify_deployed_selection(request)

    assert verification.verified is False
    assert "evidence_lineage" in verification.failed_checks


def test_verify_rejects_tampered_baseline_bundle(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    request.baseline_bundle.path.write_bytes(b"tampered")

    verification = verify_deployed_selection(request)

    assert verification.verified is False
    assert "baseline_bundle_sha256" in verification.failed_checks


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
