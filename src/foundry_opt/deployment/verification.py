from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import uuid

from foundry_opt.deployment.models import (
    DeploymentCheck,
    DeploymentTrigger,
    DeploymentVerification,
    DeploymentVerificationRequest,
    DeploymentVerificationStatus,
    WorkflowRunStatus,
)
from foundry_opt.github_workflow.errors import CampaignPublicationError
from foundry_opt.github_workflow.publication import (
    _verify_redacted_evidence,
)
from foundry_opt.packaging import BundleRequest, build_source_bundle


_SUCCESS_STATUSES = {"active", "completed", "ready", "succeeded"}


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
    bundle_content = _file_content(request.bundle.path)
    bundle_sha256 = (
        hashlib.sha256(bundle_content).hexdigest()
        if bundle_content is not None
        else None
    )
    baseline_bundle_content = _file_content(request.baseline_bundle.path)
    baseline_bundle_sha256 = (
        hashlib.sha256(baseline_bundle_content).hexdigest()
        if baseline_bundle_content is not None
        else None
    )
    reproduction = _reproduce_selection(request)
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
            "reproduced_tree",
            reproduction is not None
            and reproduction.tree_hash == request.expected_tree_hash,
        ),
        DeploymentCheck(
            "reproduced_bundle",
            reproduction is not None
            and bundle_content is not None
            and reproduction.bundle_bytes == bundle_content
            and reproduction.bundle_sha256
            == request.expected_bundle_sha256,
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
                request,
            ),
        ),
        DeploymentCheck(
            "baseline_bundle_sha256",
            baseline_bundle_sha256
            == request.expected_baseline_bundle_sha256
            == request.baseline_bundle.sha256
            and (
                baseline_bundle_content is not None
                and len(baseline_bundle_content)
                == request.baseline_bundle.byte_size
            ),
        ),
        DeploymentCheck(
            "published_terminal_status",
            (request.record.status or "").casefold()
            in _SUCCESS_STATUSES,
        ),
        DeploymentCheck(
            "service_provenance_record",
            request.record.project_endpoint
            == request.expected_project_endpoint
            and request.record.agent_name == request.expected_agent_name
            and request.record.base_version
            == request.expected_base_version
            and request.record.version == request.expected_version
            and request.record.sha256 == request.expected_bundle_sha256
            and request.record.patch_sha256
            == request.expected_patch_sha256
            and request.record.tree_hash == request.expected_tree_hash
            and request.record.evidence_sha256
            == request.expected_evidence_sha256
            and request.record.runtime == request.expected_runtime
            and request.record.entry_point
            == request.expected_entry_point
            and request.record.dependency_resolution
            == request.expected_dependency_resolution
            and _record_metadata_matches(request),
        ),
        DeploymentCheck(
            "workflow_identity",
            request.workflow.exists
            and run.path == request.workflow.path
            and run.trigger is request.workflow.trigger
            and run.head_commit == request.expected_commit,
        ),
        DeploymentCheck(
            "workflow_commit_tree",
            reproduction is not None
            and reproduction.workflow_commit_tree
            == request.expected_tree_hash
            == reproduction.tree_hash,
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


def _file_content(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _evidence_matches(
    content: bytes | None,
    request: DeploymentVerificationRequest,
) -> bool:
    if content is None:
        return False
    try:
        _verify_redacted_evidence(
            content,
            request.expected_campaign_id,
            request.candidate_id,
            request.expected_patch_sha256,
            (),
        )
        document = json.loads(content)
    except (
        CampaignPublicationError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return False
    if not isinstance(document, dict):
        return False
    candidates = document.get("candidates")
    pareto = document.get("pareto")
    baseline = document.get("baseline")
    if not isinstance(candidates, list) or not isinstance(pareto, dict):
        return False
    eligible = pareto.get("eligible_ids")
    matching = [
        candidate
        for candidate in candidates
        if isinstance(candidate, dict)
        and candidate.get("subject_id") == request.candidate_id
        and candidate.get("patch_hash") == request.expected_patch_sha256
    ]
    return (
        document.get("campaign_id") == request.expected_campaign_id
        and document.get("source_hash")
        == request.expected_baseline_bundle_sha256
        and isinstance(baseline, dict)
        and baseline.get("subject_id")
        == request.expected_baseline_subject_id
        and isinstance(eligible, list)
        and request.candidate_id in eligible
        and len(matching) == 1
    )


def _record_metadata_matches(
    request: DeploymentVerificationRequest,
) -> bool:
    expected = {
        "foundry-opt-base-version": str(request.expected_base_version),
        "foundry-opt-source-sha256": request.expected_bundle_sha256,
        "foundry-opt-patch-sha256": request.expected_patch_sha256,
        "foundry-opt-tree-hash": request.expected_tree_hash,
        "foundry-opt-evidence-sha256": request.expected_evidence_sha256,
    }
    return all(
        request.record.metadata.get(key) == value
        for key, value in expected.items()
    )


class _Reproduction:
    def __init__(
        self,
        tree_hash: str,
        workflow_commit_tree: str,
        bundle_bytes: bytes,
        bundle_sha256: str,
    ) -> None:
        self.tree_hash = tree_hash
        self.workflow_commit_tree = workflow_commit_tree
        self.bundle_bytes = bundle_bytes
        self.bundle_sha256 = bundle_sha256


def _reproduce_selection(
    request: DeploymentVerificationRequest,
) -> _Reproduction | None:
    root = request.repository_root.expanduser().resolve()
    patch = root / request.patch_path
    try:
        patch = patch.resolve(strict=True)
    except OSError:
        return None
    if not patch.is_relative_to(root) or not patch.is_file():
        return None
    scratch = root / f".fv-{uuid.uuid4().hex[:8]}"
    worktree = scratch / "w"
    absolute_worktree = worktree.resolve()
    added = False
    try:
        scratch.mkdir(parents=True, exist_ok=False)
        workflow_commit_tree = _git(
            root,
            "rev-parse",
            f"{request.expected_commit}^{{tree}}",
        )
        _git(
            root,
            "worktree",
            "add",
            "--detach",
            str(absolute_worktree),
            request.expected_base_commit,
        )
        added = True
        _git(
            absolute_worktree,
            "apply",
            "--index",
            "--binary",
            str(patch),
        )
        tree_hash = _git(worktree, "write-tree")
        artifact = build_source_bundle(
            BundleRequest(
                repository_root=absolute_worktree,
                output_path=scratch / "rebuilt.zip",
                include=request.bundle_include,
                exclude=request.bundle_exclude,
                dependency_resolution=(
                    request.bundle_dependency_resolution
                ),
                evidence_paths=request.bundle_evidence_paths,
            )
        )
        bundle_bytes = artifact.path.read_bytes()
        return _Reproduction(
            tree_hash,
            workflow_commit_tree,
            bundle_bytes,
            artifact.sha256,
        )
    except (OSError, RuntimeError, ValueError):
        return None
    finally:
        if added:
            subprocess.run(
                (
                    "git",
                    "worktree",
                    "remove",
                    "--force",
                    str(absolute_worktree),
                ),
                cwd=root,
                capture_output=True,
            )
        shutil.rmtree(scratch, ignore_errors=True)


def _git(root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ("git", *arguments),
            cwd=root,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise RuntimeError("git execution failed") from error
    if result.returncode != 0:
        raise RuntimeError("git verification failed")
    return result.stdout.strip()
