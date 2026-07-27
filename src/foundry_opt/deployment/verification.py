from __future__ import annotations

import hashlib
import json
from pathlib import Path

from foundry_opt.deployment.models import (
    DeploymentCheck,
    DeploymentTrigger,
    DeploymentVerification,
    DeploymentVerificationRequest,
    DeploymentVerificationStatus,
    WorkflowRunStatus,
)


def verify_deployed_selection(
    request: DeploymentVerificationRequest,
) -> DeploymentVerification:
    run = request.workflow_run
    runtime = request.runtime
    if run is None:
        status = (
            DeploymentVerificationStatus.MANUAL_TRIGGER_REQUIRED
            if request.workflow.trigger is DeploymentTrigger.MANUAL
            else DeploymentVerificationStatus.MERGE_DEPLOYMENT_PENDING
        )
        return DeploymentVerification(
            verified=False,
            status=status,
            version=None,
            sha256=None,
            run_url=None,
            portal_url=None,
        )
    if run.status in {
        WorkflowRunStatus.QUEUED,
        WorkflowRunStatus.IN_PROGRESS,
    }:
        return DeploymentVerification(
            verified=False,
            status=DeploymentVerificationStatus.WORKFLOW_PENDING,
            version=None,
            sha256=None,
            run_url=run.url,
            portal_url=None,
        )
    if run.status is not WorkflowRunStatus.SUCCESS:
        return DeploymentVerification(
            verified=False,
            status=DeploymentVerificationStatus.WORKFLOW_FAILED,
            version=None,
            sha256=None,
            run_url=run.url,
            portal_url=None,
        )

    patch_sha256 = _contained_file_sha256(
        request.repository_root,
        request.patch_path,
    )
    evidence_content, evidence_sha256 = _contained_file(
        request.repository_root,
        request.evidence_path,
    )
    bundle_sha256 = _file_sha256(request.bundle.path)
    checks = (
        DeploymentCheck(
            "patch_sha256",
            patch_sha256 == request.expected_patch_sha256,
        ),
        DeploymentCheck(
            "tree_hash",
            request.deployed_tree_hash == request.expected_tree_hash,
        ),
        DeploymentCheck(
            "bundle_sha256",
            bundle_sha256 == request.expected_bundle_sha256
            == request.bundle.sha256
            == request.record.sha256,
        ),
        DeploymentCheck(
            "bundle_size",
            _file_size(request.bundle.path) == request.bundle.byte_size,
        ),
        DeploymentCheck(
            "evidence_sha256",
            evidence_sha256 == request.expected_evidence_sha256,
        ),
        DeploymentCheck(
            "evidence_lineage",
            _evidence_matches(
                evidence_content,
                request.candidate_id,
                request.expected_patch_sha256,
            ),
        ),
        DeploymentCheck(
            "record_lineage",
            request.record.patch_sha256 == request.expected_patch_sha256
            and request.record.tree_hash == request.expected_tree_hash
            and request.record.evidence_sha256
            == request.expected_evidence_sha256,
        ),
        DeploymentCheck(
            "workflow_identity",
            request.workflow.exists
            and run.path == request.workflow.path
            and run.trigger is request.workflow.trigger
            and run.head_commit == request.expected_commit,
        ),
        DeploymentCheck(
            "workflow_run_url",
            run.url == request.expected_run_url,
        ),
        DeploymentCheck(
            "runtime_present",
            runtime is not None,
        ),
        DeploymentCheck(
            "published_version_identity",
            runtime is not None
            and request.record.version == request.expected_version
            and request.record.version > request.record.base_version
            and runtime.deployed_version == request.expected_version
            and runtime.latest_version == request.expected_version
            and runtime.agent_name == request.record.agent_name,
        ),
        DeploymentCheck(
            "deployed_source_sha256",
            runtime is not None
            and runtime.source_sha256 == request.expected_bundle_sha256
            == request.record.sha256,
        ),
        DeploymentCheck(
            "portal_url",
            runtime is not None
            and runtime.portal_url == request.expected_portal_url
            and request.record.portal_url == request.expected_portal_url,
        ),
    )
    verified = all(check.passed for check in checks)
    return DeploymentVerification(
        verified=verified,
        status=(
            DeploymentVerificationStatus.VERIFIED
            if verified
            else DeploymentVerificationStatus.MISMATCH
        ),
        version=runtime.deployed_version if runtime is not None else None,
        sha256=runtime.source_sha256 if runtime is not None else None,
        run_url=run.url,
        portal_url=runtime.portal_url if runtime is not None else None,
        checks=checks,
    )


def _contained_file_sha256(root: Path, relative: Path) -> str | None:
    _, digest = _contained_file(root, relative)
    return digest


def _contained_file(root: Path, relative: Path) -> tuple[bytes | None, str | None]:
    root = root.expanduser().resolve()
    path = root / relative
    try:
        if path.is_symlink():
            return None, None
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            return None, None
        content = resolved.read_bytes()
    except OSError:
        return None, None
    return content, hashlib.sha256(content).hexdigest()


def _file_sha256(path: Path) -> str | None:
    try:
        content = path.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(content).hexdigest()


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _evidence_matches(
    content: bytes | None,
    candidate_id: str,
    patch_sha256: str,
) -> bool:
    if content is None:
        return False
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(document, dict):
        return False
    candidates = document.get("candidates")
    pareto = document.get("pareto")
    if not isinstance(candidates, list) or not isinstance(pareto, dict):
        return False
    eligible = pareto.get("eligible_ids")
    matching = [
        candidate
        for candidate in candidates
        if isinstance(candidate, dict)
        and candidate.get("subject_id") == candidate_id
        and candidate.get("patch_hash") == patch_sha256
    ]
    return (
        isinstance(eligible, list)
        and candidate_id in eligible
        and len(matching) == 1
    )
