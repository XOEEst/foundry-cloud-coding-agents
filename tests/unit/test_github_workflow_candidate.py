from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from foundry_opt.campaign import CandidateArtifact, PatchArtifact
from foundry_opt.github_workflow import (
    AppliedPatch,
    ArtifactInspection,
    CandidateApplicationRequest,
    CandidateApplicationStatus,
    ExactPatchRequest,
    GitHubCapabilities,
    GitHubPermissionReport,
    IssueReference,
    PatchApplicationError,
    PatchTraversalError,
    PullRequestReference,
    RepositoryState,
    verify_and_apply_candidate,
)


class FakePatchApplier:
    def __init__(
        self,
        artifacts: dict[Path, bytes | Exception],
        applied: AppliedPatch | Exception | None = None,
    ) -> None:
        self.artifacts = artifacts
        self.applied = applied or AppliedPatch(
            branch="foundry-opt/campaign-1/candidate-1/session-1",
            commit_sha="d" * 40,
            changed_paths=(Path("agent.py"),),
            exact=True,
            substantive_repair=False,
        )
        self.apply_requests: list[ExactPatchRequest] = []

    def inspect_artifact(
        self,
        repository_root: Path,
        path: Path,
    ) -> ArtifactInspection:
        value = self.artifacts[path]
        if isinstance(value, Exception):
            raise value
        return ArtifactInspection(
            path=path,
            sha256=hashlib.sha256(value).hexdigest(),
            byte_count=len(value),
            content=value,
        )

    def apply_exact(self, request: ExactPatchRequest) -> AppliedPatch:
        self.apply_requests.append(request)
        if isinstance(self.applied, Exception):
            raise self.applied
        return self.applied


class FakeCandidateGateway:
    def __init__(
        self,
        *,
        default_branch: str = "main",
        default_commit: str = "b" * 40,
        existing_pr: PullRequestReference | None = None,
    ) -> None:
        self.default_branch = default_branch
        self.default_commit = default_commit
        self.existing_pr = existing_pr
        self.created_prs: list[tuple[str, str, str]] = []
        self.comments: list[tuple[int, str]] = []
        self.closed_issues: list[int] = []
        self.closed_prs: list[int] = []

    def verify_permissions(
        self,
        required: GitHubCapabilities,
    ) -> GitHubPermissionReport:
        return GitHubPermissionReport(required)

    def repository_state(self, repository_root: Path) -> RepositoryState:
        return RepositoryState(
            "octo-org/optimizer",
            self.default_branch,
            self.default_commit,
        )

    def find_candidate_pull_request(
        self,
        repository_root: Path,
        head_branch: str,
    ) -> PullRequestReference | None:
        return self.existing_pr

    def create_candidate_pull_request(
        self,
        repository_root: Path,
        *,
        base_branch: str,
        head_branch: str,
        commit_sha: str,
        title: str,
        body: str,
    ) -> PullRequestReference:
        self.created_prs.append((head_branch, title, body))
        return PullRequestReference(
            number=55,
            url="https://github.com/octo-org/optimizer/pull/55",
            head_branch=head_branch,
            head_commit=commit_sha,
            draft=False,
            body=body,
        )

    def comment_issue(
        self,
        repository_root: Path,
        issue_number: int,
        body: str,
    ) -> None:
        self.comments.append((issue_number, body))

    def close_issue(
        self,
        repository_root: Path,
        issue_number: int,
        comment: str,
    ) -> None:
        self.closed_issues.append(issue_number)

    def close_pull_request(
        self,
        repository_root: Path,
        pull_request_number: int,
        comment: str,
    ) -> None:
        self.closed_prs.append(pull_request_number)


def _candidate(patch_sha: str) -> CandidateArtifact:
    return CandidateArtifact(
        candidate_id="candidate-1",
        patch=PatchArtifact(
            candidate_id="candidate-1",
            path=Path(
                ".foundry-optimizer/campaigns/c1/candidate-1.patch"
            ),
            sha256=patch_sha,
            base_commit="b" * 40,
            result_commit="c" * 40,
        ),
        draft_id="draft-candidate-1",
        evidence_path=Path(
            ".foundry-optimizer/campaigns/c1/candidate-1.json"
        ),
        eligible=True,
        metrics={"quality": 0.9},
    )


def _inputs() -> tuple[
    CandidateApplicationRequest,
    dict[Path, bytes | Exception],
]:
    patch = b"diff --git a/agent.py b/agent.py\n"
    patch_sha = hashlib.sha256(patch).hexdigest()
    candidate = _candidate(patch_sha)
    evidence = json.dumps(
        {
            "campaign_id": "campaign-1",
            "pareto": {"eligible_ids": ["candidate-1"]},
            "candidates": [
                {
                    "subject_id": "candidate-1",
                    "patch_hash": patch_sha,
                    "agent": {"draft_id": "draft-candidate-1"},
                }
            ],
        },
        sort_keys=True,
    ).encode()
    evidence_sha = hashlib.sha256(evidence).hexdigest()
    request = CandidateApplicationRequest(
        repository_root=Path("repository"),
        campaign_id="campaign-1",
        target="support-agent",
        expected_default_branch="main",
        session_id="session-1",
        campaign_pull_request_number=42,
        candidate_issue_number=101,
        candidate=candidate,
        evidence_sha256=evidence_sha,
    )
    return request, {
        candidate.patch.path: patch,
        candidate.evidence_path: evidence,
    }


def test_exact_candidate_patch_creates_one_branch_commit_and_pr() -> None:
    request, artifacts = _inputs()
    gateway = FakeCandidateGateway()
    applier = FakePatchApplier(artifacts)

    result = verify_and_apply_candidate(request, gateway, applier)

    assert result.status is CandidateApplicationStatus.APPLIED
    assert result.pull_request is not None
    assert result.pull_request.number == 55
    assert result.commit_sha == "d" * 40
    assert len(applier.apply_requests) == 1
    assert len(gateway.created_prs) == 1
    _, _, body = gateway.created_prs[0]
    assert "Patch SHA-256: `" + request.candidate.patch.sha256 + "`" in body
    assert "Campaign: #42" in body
    assert gateway.comments == [
        (101, "Exact candidate patch published as #55.")
    ]


@pytest.mark.parametrize(
    ("mutation", "reason_code"),
    [
        ("base", "base_changed"),
        ("branch", "default_branch_changed"),
        ("missing_patch", "patch_missing"),
        ("patch_mismatch", "patch_mismatch"),
        ("missing_evidence", "evidence_missing"),
        ("evidence_mismatch", "evidence_mismatch"),
        ("lineage", "evidence_lineage_mismatch"),
        ("traversal", "path_traversal"),
        ("repair", "substantive_repair"),
    ],
)
def test_candidate_rejects_unverifiable_or_repaired_artifacts(
    mutation: str,
    reason_code: str,
) -> None:
    request, artifacts = _inputs()
    gateway = FakeCandidateGateway(
        default_branch=("trunk" if mutation == "branch" else "main"),
        default_commit=("9" * 40 if mutation == "base" else "b" * 40)
    )
    if mutation == "missing_patch":
        artifacts[request.candidate.patch.path] = FileNotFoundError()
    elif mutation == "patch_mismatch":
        artifacts[request.candidate.patch.path] = b"changed"
    elif mutation == "missing_evidence":
        artifacts[request.candidate.evidence_path] = FileNotFoundError()
    elif mutation == "evidence_mismatch":
        artifacts[request.candidate.evidence_path] = b"{}"
    elif mutation == "lineage":
        bad = json.dumps(
            {
                "campaign_id": "other-campaign",
                "candidates": [],
            }
        ).encode()
        artifacts[request.candidate.evidence_path] = bad
        request = CandidateApplicationRequest(
            **{
                **request.__dict__,
                "evidence_sha256": hashlib.sha256(bad).hexdigest(),
            }
        )
    applied: AppliedPatch | Exception | None = None
    if mutation == "traversal":
        applied = PatchTraversalError()
    elif mutation == "repair":
        applied = AppliedPatch(
            branch="branch",
            commit_sha="d" * 40,
            changed_paths=(Path("agent.py"),),
            exact=False,
            substantive_repair=True,
        )
    applier = FakePatchApplier(artifacts, applied)

    result = verify_and_apply_candidate(request, gateway, applier)

    assert result.status is CandidateApplicationStatus.REJECTED
    assert result.reason_code == reason_code
    assert gateway.created_prs == []
    assert gateway.closed_issues == []
    assert gateway.closed_prs == []


def test_candidate_application_is_idempotent_per_session_branch() -> None:
    request, artifacts = _inputs()
    existing = PullRequestReference(
        55,
        "https://github.com/octo-org/optimizer/pull/55",
        "foundry-opt/campaign-1/candidate-1/session-1",
        "d" * 40,
        False,
    )
    gateway = FakeCandidateGateway(existing_pr=existing)
    applier = FakePatchApplier(artifacts)

    result = verify_and_apply_candidate(request, gateway, applier)

    assert result.status is CandidateApplicationStatus.ALREADY_APPLIED
    assert result.pull_request == existing
    assert applier.apply_requests == []
    assert gateway.created_prs == []


def test_rejected_issue_and_pr_close_only_when_explicitly_requested() -> None:
    request, artifacts = _inputs()
    request = CandidateApplicationRequest(
        **{
            **request.__dict__,
            "close_rejected": True,
            "rejected_pull_request_number": 54,
        }
    )
    gateway = FakeCandidateGateway(default_commit="9" * 40)

    result = verify_and_apply_candidate(
        request,
        gateway,
        FakePatchApplier(artifacts),
    )

    assert result.status is CandidateApplicationStatus.REJECTED
    assert gateway.closed_issues == [101]
    assert gateway.closed_prs == [54]
