from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Protocol

from foundry_opt.github_workflow.errors import (
    CandidatePublicationError,
    GitHubPermissionDeniedError,
    PatchApplicationError,
    PatchTreeMismatchError,
    PatchTraversalError,
)
from foundry_opt.github_workflow.models import (
    AppliedPatch,
    ArtifactInspection,
    CandidateApplicationRequest,
    CandidateApplicationResult,
    CandidateApplicationStatus,
    ExactPatchRequest,
    GitHubCapabilities,
    GitHubPermissionReport,
    PullRequestReference,
    RepositoryState,
    WorkflowFailure,
)


class CandidateGateway(Protocol):
    def verify_permissions(
        self,
        required: GitHubCapabilities,
    ) -> GitHubPermissionReport: ...

    def repository_state(self, repository_root: Path) -> RepositoryState: ...

    def find_candidate_pull_request(
        self,
        repository_root: Path,
        head_branch: str,
    ) -> PullRequestReference | None: ...

    def create_candidate_pull_request(
        self,
        repository_root: Path,
        *,
        base_branch: str,
        head_branch: str,
        commit_sha: str,
        title: str,
        body: str,
    ) -> PullRequestReference: ...

    def comment_issue(
        self,
        repository_root: Path,
        issue_number: int,
        body: str,
    ) -> None: ...

    def close_issue(
        self,
        repository_root: Path,
        issue_number: int,
        comment: str,
    ) -> None: ...

    def close_pull_request(
        self,
        repository_root: Path,
        pull_request_number: int,
        comment: str,
    ) -> None: ...


class PatchApplier(Protocol):
    def inspect_artifact(
        self,
        repository_root: Path,
        path: Path,
    ) -> ArtifactInspection: ...

    def apply_exact(self, request: ExactPatchRequest) -> AppliedPatch: ...

    def resolve_tree(
        self,
        repository_root: Path,
        commit: str,
    ) -> str | None: ...

    def resolve_branch_commit(
        self,
        repository_root: Path,
        branch: str,
    ) -> str | None: ...

    def restore_after_publication_failure(
        self,
        repository_root: Path,
        base_commit: str,
        base_branch: str,
    ) -> None: ...


def verify_and_apply_candidate(
    request: CandidateApplicationRequest,
    gateway: CandidateGateway,
    patch_applier: PatchApplier,
) -> CandidateApplicationResult:
    required = GitHubCapabilities.CANDIDATE_PUBLICATION
    permissions = gateway.verify_permissions(required)
    missing = required & ~permissions.granted
    if missing:
        raise GitHubPermissionDeniedError(missing)

    branch = _candidate_branch(request)
    existing = gateway.find_candidate_pull_request(
        request.repository_root,
        branch,
    )

    state = gateway.repository_state(request.repository_root)
    if state.default_branch != request.expected_default_branch:
        return _rejected(
            request,
            gateway,
            "default_branch_changed",
        )
    if state.default_commit != request.candidate.patch.base_commit:
        return _rejected(request, gateway, "base_changed")

    try:
        patch = patch_applier.inspect_artifact(
            request.repository_root,
            request.candidate.patch.path,
        )
    except FileNotFoundError:
        return _rejected(request, gateway, "patch_missing")
    except ValueError:
        return _rejected(request, gateway, "path_traversal")
    if patch.sha256 != request.candidate.patch.sha256:
        return _rejected(request, gateway, "patch_mismatch")

    try:
        evidence = patch_applier.inspect_artifact(
            request.repository_root,
            request.candidate.evidence_path,
        )
    except FileNotFoundError:
        return _rejected(request, gateway, "evidence_missing")
    except ValueError:
        return _rejected(request, gateway, "path_traversal")
    if evidence.sha256 != request.evidence_sha256:
        return _rejected(request, gateway, "evidence_mismatch")
    lineage_valid, expected_tree = _evidence_lineage(request, evidence)
    if not lineage_valid:
        return _rejected(
            request,
            gateway,
            "evidence_lineage_mismatch",
        )
    if expected_tree is None:
        expected_tree = patch_applier.resolve_tree(
            request.repository_root,
            request.candidate.patch.result_commit,
        )
    if expected_tree is None:
        return _rejected(
            request,
            gateway,
            "result_tree_unavailable",
        )
    if existing is not None:
        expected_commit = request.expected_pull_request_head_commit
        if expected_commit is None:
            expected_commit = patch_applier.resolve_branch_commit(
                request.repository_root,
                branch,
            )
        if not _candidate_pr_matches(
            existing,
            request,
            branch,
            expected_commit,
        ):
            return _rejected(
                request,
                gateway,
                "existing_pr_mismatch",
            )
        return CandidateApplicationResult(
            status=CandidateApplicationStatus.ALREADY_APPLIED,
            candidate_id=request.candidate.candidate_id,
            pull_request=existing,
            commit_sha=existing.head_commit,
        )

    try:
        applied = patch_applier.apply_exact(
            ExactPatchRequest(
                repository_root=request.repository_root,
                base_commit=request.candidate.patch.base_commit,
                patch_path=request.candidate.patch.path,
                expected_patch_sha256=request.candidate.patch.sha256,
                expected_tree_sha=expected_tree,
                branch=branch,
                commit_message=(
                    f"Apply {request.campaign_id} "
                    f"{request.candidate.candidate_id}"
                ),
            )
        )
    except PatchTraversalError:
        return _rejected(request, gateway, "path_traversal")
    except PatchTreeMismatchError:
        return _rejected(request, gateway, "result_tree_mismatch")
    except PatchApplicationError:
        return _rejected(
            request,
            gateway,
            "patch_application_failed",
        )
    if (
        applied.branch != branch
        or not applied.exact
        or applied.substantive_repair
    ):
        return _rejected(request, gateway, "substantive_repair")
    if applied.tree_sha != expected_tree:
        return _rejected(request, gateway, "result_tree_mismatch")

    try:
        pull_request = gateway.create_candidate_pull_request(
            request.repository_root,
            base_branch=state.default_branch,
            head_branch=branch,
            commit_sha=applied.commit_sha,
            title=(
                f"[foundry-opt] {request.target} candidate "
                f"{request.candidate.candidate_id}"
            ),
            body=_candidate_pull_request_body(request, applied),
        )
    except RuntimeError:
        try:
            patch_applier.restore_after_publication_failure(
                request.repository_root,
                request.candidate.patch.base_commit,
                request.expected_default_branch,
            )
        except RuntimeError as restore_error:
            raise CandidatePublicationError(
                "Candidate PR publication failed and the local checkout "
                "could not be restored"
            ) from restore_error
        raise
    if not _candidate_pr_matches(
        pull_request,
        request,
        branch,
        applied.commit_sha,
    ):
        raise CandidatePublicationError(
            "Created candidate PR does not match the exact publication"
        )
    failures: list[WorkflowFailure] = []
    try:
        gateway.comment_issue(
            request.repository_root,
            request.candidate_issue_number,
            f"Exact candidate patch published as #{pull_request.number}.",
        )
    except RuntimeError:
        failures.append(
            WorkflowFailure(
                "comment_issue",
                request.candidate.candidate_id,
                "comment_failed",
                "Candidate PR was created but its issue comment failed.",
            )
        )
    return CandidateApplicationResult(
        status=CandidateApplicationStatus.APPLIED,
        candidate_id=request.candidate.candidate_id,
        pull_request=pull_request,
        commit_sha=applied.commit_sha,
        failures=tuple(failures),
    )


def _evidence_lineage(
    request: CandidateApplicationRequest,
    evidence: ArtifactInspection,
) -> tuple[bool, str | None]:
    try:
        document = json.loads(evidence.content)
        if (
            not isinstance(document, dict)
            or document.get("campaign_id") != request.campaign_id
        ):
            return False, None
        candidates = document.get("candidates")
        pareto = document.get("pareto")
        if (
            not isinstance(candidates, list)
            or not isinstance(pareto, dict)
            or not isinstance(pareto.get("eligible_ids"), list)
            or request.candidate.candidate_id
            not in pareto["eligible_ids"]
        ):
            return False, None
        matching = [
            item
            for item in candidates
            if isinstance(item, dict)
            and item.get("subject_id")
            == request.candidate.candidate_id
        ]
        matches_lineage = (
            len(matching) == 1
            and matching[0].get("patch_hash")
            == request.candidate.patch.sha256
            and isinstance(matching[0].get("agent"), dict)
            and matching[0]["agent"].get("draft_id")
            == request.candidate.draft_id
        )
        if not matches_lineage:
            return False, None
        result_tree = matching[0].get("result_tree")
        if result_tree is None:
            return True, None
        if not isinstance(result_tree, str) or not re.fullmatch(
            r"[0-9a-f]{40}",
            result_tree,
        ):
            return False, None
        return True, result_tree
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False, None


def _candidate_pull_request_body(
    request: CandidateApplicationRequest,
    applied: AppliedPatch,
) -> str:
    changed = "\n".join(
        f"- `{path.as_posix()}`" for path in applied.changed_paths
    )
    return "\n".join(
        (
            "<!-- foundry-opt:candidate-pr:"
            f"{request.campaign_id}:{request.candidate.candidate_id}:"
            f"{request.session_id} -->",
            f"Campaign: #{request.campaign_pull_request_number}",
            f"Candidate issue: #{request.candidate_issue_number}",
            f"Target: `{request.target}`",
            f"Base commit: `{request.candidate.patch.base_commit}`",
            f"Patch SHA-256: `{request.candidate.patch.sha256}`",
            f"Evidence SHA-256: `{request.evidence_sha256}`",
            "",
            "This PR contains only the exact verified patch. Automation must "
            "not merge or deploy it.",
            "",
            "## Changed paths",
            changed,
        )
    ) + "\n"


def _candidate_branch(request: CandidateApplicationRequest) -> str:
    return "/".join(
        (
            "foundry-opt",
            _slug(request.campaign_id),
            _slug(request.candidate.candidate_id),
            _slug(request.session_id),
        )
    )


def _candidate_pr_matches(
    pull_request: PullRequestReference,
    request: CandidateApplicationRequest,
    branch: str,
    expected_commit: str | None,
) -> bool:
    marker = (
        "<!-- foundry-opt:candidate-pr:"
        f"{request.campaign_id}:{request.candidate.candidate_id}:"
        f"{request.session_id} -->"
    )
    return (
        expected_commit is not None
        and pull_request.state == "OPEN"
        and not pull_request.draft
        and pull_request.base_branch == request.expected_default_branch
        and pull_request.head_branch == branch
        and pull_request.head_commit == expected_commit
        and marker in pull_request.body
        and f"Base commit: `{request.candidate.patch.base_commit}`"
        in pull_request.body
        and f"Patch SHA-256: `{request.candidate.patch.sha256}`"
        in pull_request.body
        and f"Evidence SHA-256: `{request.evidence_sha256}`"
        in pull_request.body
    )


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    if not slug:
        raise ValueError("GitHub workflow identifier is not branch-safe")
    return slug[:80]


def _rejected(
    request: CandidateApplicationRequest,
    gateway: CandidateGateway,
    reason_code: str,
) -> CandidateApplicationResult:
    failures: list[WorkflowFailure] = []
    if request.close_rejected:
        try:
            gateway.close_issue(
                request.repository_root,
                request.candidate_issue_number,
                "Candidate rejected by exact-patch verification; start a "
                "new campaign.",
            )
        except RuntimeError:
            failures.append(
                WorkflowFailure(
                    "close_issue",
                    request.candidate.candidate_id,
                    "close_issue_failed",
                    "Explicit rejected-issue closure failed.",
                )
            )
        if request.rejected_pull_request_number is not None:
            try:
                gateway.close_pull_request(
                    request.repository_root,
                    request.rejected_pull_request_number,
                    "Candidate rejected by exact-patch verification; start a "
                    "new campaign.",
                )
            except RuntimeError:
                failures.append(
                    WorkflowFailure(
                        "close_pull_request",
                        request.candidate.candidate_id,
                        "close_pr_failed",
                        "Explicit rejected-PR closure failed.",
                    )
                )
    return CandidateApplicationResult(
        status=CandidateApplicationStatus.REJECTED,
        candidate_id=request.candidate.candidate_id,
        reason_code=reason_code,
        failures=tuple(failures),
    )
