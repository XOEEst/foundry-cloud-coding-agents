from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from foundry_opt.campaign import (
    CampaignReport,
    CandidateArtifact,
    PatchArtifact,
)
from foundry_opt.github_workflow import (
    ArtifactReference,
    CampaignPublicationRequest,
    GitHubCapabilities,
    GitHubPermissionReport,
    IssueReference,
    PullRequestReference,
    RepositoryState,
    GitHubPermissionDeniedError,
    publish_campaign,
)


@dataclass
class FakeGateway:
    campaign_pr: PullRequestReference | None = None
    permission_report: GitHubPermissionReport | None = None
    fail_labels: bool = False
    fail_sub_issue: bool = False
    fail_dependency: bool = False
    fail_issue: bool = False

    def __post_init__(self) -> None:
        self.created_campaigns: list[tuple[str, str, str, str]] = []
        self.created_issues: list[tuple[str, str]] = []
        self.labels: list[tuple[int, tuple[str, ...]]] = []
        self.links: list[tuple[int, int]] = []
        self.updated_issues: list[tuple[int, str]] = []
        self.updated_prs: list[tuple[int, str]] = []
        self.dependencies: list[tuple[int, int]] = []
        self.closed_prs: list[int] = []
        self.events: list[str] = []
        self.existing_issues: dict[str, IssueReference] = {}

    def verify_permissions(
        self,
        required: GitHubCapabilities,
    ) -> GitHubPermissionReport:
        return self.permission_report or GitHubPermissionReport(
            granted=required
        )

    def repository_state(self, repository_root: Path) -> RepositoryState:
        return RepositoryState(
            repository="octo-org/optimizer",
            default_branch="main",
            default_commit="b" * 40,
        )

    def artifact_url(
        self,
        repository: str,
        commit: str,
        path: Path,
    ) -> str:
        return (
            f"https://github.com/{repository}/blob/{commit}/"
            f"{path.as_posix()}"
        )

    def find_campaign_pull_request(
        self,
        repository_root: Path,
        head_branch: str,
    ) -> PullRequestReference | None:
        return self.campaign_pr

    def create_campaign_pull_request(
        self,
        repository_root: Path,
        *,
        base_branch: str,
        head_branch: str,
        head_commit: str,
        title: str,
        body: str,
    ) -> PullRequestReference:
        self.created_campaigns.append(
            (base_branch, head_branch, title, body)
        )
        self.events.append("campaign-pr")
        self.campaign_pr = PullRequestReference(
            number=42,
            url="https://github.com/octo-org/optimizer/pull/42",
            head_branch=head_branch,
            head_commit=head_commit,
            draft=True,
            body=body,
        )
        return self.campaign_pr

    def find_candidate_issue(
        self,
        repository_root: Path,
        marker: str,
    ) -> IssueReference | None:
        return self.existing_issues.get(marker)

    def create_issue(
        self,
        repository_root: Path,
        *,
        title: str,
        body: str,
    ) -> IssueReference:
        if self.fail_issue:
            raise RuntimeError("body contained token=secret")
        self.created_issues.append((title, body))
        issue = IssueReference(
            number=100 + len(self.created_issues),
            url=(
                "https://github.com/octo-org/optimizer/issues/"
                f"{100 + len(self.created_issues)}"
            ),
            title=title,
            body=body,
        )
        self.events.append(f"issue-{issue.number}")
        return issue

    def add_labels(
        self,
        repository_root: Path,
        issue_number: int,
        labels: tuple[str, ...],
    ) -> None:
        if self.fail_labels:
            raise RuntimeError("token=secret-label-error")
        self.labels.append((issue_number, labels))

    def link_sub_issue(
        self,
        repository_root: Path,
        parent_number: int,
        child_number: int,
    ) -> None:
        if self.fail_sub_issue:
            raise RuntimeError("sub-issues unavailable")
        self.links.append((parent_number, child_number))

    def add_dependency(
        self,
        repository_root: Path,
        issue_number: int,
        blocker_number: int,
    ) -> None:
        if self.fail_dependency:
            raise RuntimeError("dependencies unavailable")
        self.dependencies.append((issue_number, blocker_number))

    def update_issue_body(
        self,
        repository_root: Path,
        issue_number: int,
        body: str,
    ) -> None:
        self.updated_issues.append((issue_number, body))

    def update_pull_request_body(
        self,
        repository_root: Path,
        pull_request_number: int,
        body: str,
    ) -> None:
        self.updated_prs.append((pull_request_number, body))

    def close_pull_request(
        self,
        repository_root: Path,
        pull_request_number: int,
        comment: str,
    ) -> None:
        self.closed_prs.append(pull_request_number)
        self.events.append("campaign-close")


def _report() -> CampaignReport:
    patch = PatchArtifact(
        candidate_id="candidate-1",
        path=Path(".foundry-optimizer/campaigns/c1/candidate-1.patch"),
        sha256="a" * 64,
        base_commit="b" * 40,
        result_commit="c" * 40,
    )
    candidate = CandidateArtifact(
        candidate_id="candidate-1",
        patch=patch,
        draft_id="draft-candidate-1",
        evidence_path=Path(
            ".foundry-optimizer/campaigns/c1/candidate-1.json"
        ),
        eligible=True,
        metrics={"quality": 0.9},
    )
    return CampaignReport(
        campaign_id="campaign-1",
        target="support-agent",
        base_commit="b" * 40,
        baseline_draft_id="draft-baseline",
        candidates=(candidate,),
        pareto_candidate_ids=("candidate-1",),
    )


def _request() -> CampaignPublicationRequest:
    return CampaignPublicationRequest(
        repository_root=Path("repository"),
        report=_report(),
        head_branch="foundry-opt/campaign-1",
        head_commit="d" * 40,
        manifests=(
            ArtifactReference(
                path=Path(
                    ".foundry-optimizer/campaigns/c1/manifest.json"
                ),
                sha256="e" * 64,
            ),
        ),
        evidence_sha256={"candidate-1": "f" * 64},
        reproduction_instructions=(
            "Run the configured validation commands.",
        ),
    )


def test_publish_campaign_creates_draft_pr_and_one_pareto_child() -> None:
    gateway = FakeGateway()

    publication = publish_campaign(_request(), gateway)

    assert publication.campaign_pull_request.number == 42
    assert len(publication.candidate_issues) == 1
    assert gateway.links == [(42, 101)]
    assert gateway.labels == [(101, ("ready-for-agent",))]
    title, body = gateway.created_issues[0]
    assert title == "[foundry-opt] support-agent candidate candidate-1"
    assert "Base commit: `" + "b" * 40 + "`" in body
    assert "Patch SHA-256: `" + "a" * 64 + "`" in body
    assert "Evidence SHA-256: `" + "f" * 64 + "`" in body
    assert "raw prompt" not in body.casefold()
    _, _, _, campaign_body = gateway.created_campaigns[0]
    assert "Temporary review surface; automation must never merge this PR." in (
        campaign_body
    )


def test_publication_is_idempotent_and_does_not_duplicate_children() -> None:
    request = _request()
    existing_pr = PullRequestReference(
        number=42,
        url="https://github.com/octo-org/optimizer/pull/42",
        head_branch=request.head_branch,
        head_commit=request.head_commit,
        draft=True,
        body="existing",
    )
    marker = "<!-- foundry-opt:candidate:campaign-1:candidate-1 -->"
    existing_issue = IssueReference(
        number=101,
        url="https://github.com/octo-org/optimizer/issues/101",
        title="candidate",
        body=marker,
    )
    gateway = FakeGateway(campaign_pr=existing_pr)
    gateway.existing_issues[marker] = existing_issue

    publication = publish_campaign(request, gateway)

    assert publication.campaign_pull_request == existing_pr
    assert publication.candidate_issues[0].issue == existing_issue
    assert gateway.created_campaigns == []
    assert gateway.created_issues == []
    assert gateway.labels == []
    assert gateway.links == []


def test_sub_issue_failure_uses_task_list_fallback_and_is_explicit() -> None:
    gateway = FakeGateway(fail_sub_issue=True)

    publication = publish_campaign(_request(), gateway)

    assert gateway.updated_prs
    assert "- [ ] #101" in gateway.updated_prs[0][1]
    assert "Part of #42" in gateway.created_issues[0][1]
    assert [failure.code for failure in publication.failures] == [
        "sub_issue_fallback"
    ]
    assert "secret" not in publication.failures[0].message


def test_labels_and_dependencies_are_best_effort_and_explicit() -> None:
    report = _report()
    request = CampaignPublicationRequest(
        repository_root=Path("repository"),
        report=report,
        head_branch="foundry-opt/campaign-1",
        head_commit="d" * 40,
        manifests=(),
        evidence_sha256={"candidate-1": "f" * 64},
        reproduction_instructions=("Run validation.",),
        dependencies={"candidate-1": ("candidate-0",)},
    )
    gateway = FakeGateway(fail_labels=True, fail_dependency=True)
    blocker_marker = (
        "<!-- foundry-opt:candidate:campaign-1:candidate-0 -->"
    )
    gateway.existing_issues[blocker_marker] = IssueReference(
        99,
        "https://github.com/octo-org/optimizer/issues/99",
        "blocker",
        blocker_marker,
    )

    publication = publish_campaign(request, gateway)

    assert {failure.code for failure in publication.failures} == {
        "label_failed",
        "dependency_fallback",
    }
    assert gateway.updated_issues
    assert "Blocked by: #99" in gateway.updated_issues[-1][1]


def test_permission_denial_happens_before_any_write() -> None:
    gateway = FakeGateway(
        permission_report=GitHubPermissionReport(
            granted=GitHubCapabilities.ISSUES
        )
    )

    with pytest.raises(GitHubPermissionDeniedError) as raised:
        publish_campaign(_request(), gateway)

    assert raised.value.missing & GitHubCapabilities.PULL_REQUESTS
    assert gateway.created_campaigns == []
    assert gateway.created_issues == []


def test_cleanup_waits_until_every_candidate_pr_is_available() -> None:
    request = _request()
    waiting = CampaignPublicationRequest(
        **{
            **request.__dict__,
            "cleanup_requested": True,
            "candidate_pull_requests": {},
        }
    )
    gateway = FakeGateway()

    publication = publish_campaign(waiting, gateway)

    assert publication.campaign_closed is False
    assert gateway.closed_prs == []
    assert publication.failures[-1].code == "cleanup_not_ready"

    candidate_pr = PullRequestReference(
        55,
        "https://github.com/octo-org/optimizer/pull/55",
        "foundry-opt/campaign-1/candidate-1",
        "1" * 40,
        False,
    )
    ready = CampaignPublicationRequest(
        **{
            **request.__dict__,
            "cleanup_requested": True,
            "candidate_pull_requests": {"candidate-1": candidate_pr},
        }
    )
    gateway = FakeGateway()

    publication = publish_campaign(ready, gateway)

    assert publication.campaign_closed is True
    assert gateway.events[-1] == "campaign-close"
    assert gateway.events.index("issue-101") < gateway.events.index(
        "campaign-close"
    )


def test_partial_issue_failure_is_reported_without_secret_details() -> None:
    gateway = FakeGateway(fail_issue=True)

    publication = publish_campaign(_request(), gateway)

    assert publication.candidate_issues == ()
    assert publication.failures[0].code == "issue_create_failed"
    assert "secret" not in publication.failures[0].message


def test_reproduction_instructions_are_redacted_and_raw_content_is_rejected() -> None:
    request = _request()
    secret = "sentinel-secret"
    redacted = CampaignPublicationRequest(
        **{
            **request.__dict__,
            "reproduction_instructions": (
                f"Run validation with token={secret}.",
            ),
            "sensitive_values": (secret,),
        }
    )
    gateway = FakeGateway()

    publish_campaign(redacted, gateway)

    assert secret not in gateway.created_issues[0][1]
    assert "[REDACTED]" in gateway.created_issues[0][1]

    with pytest.raises(ValueError, match="redacted summaries"):
        CampaignPublicationRequest(
            **{
                **request.__dict__,
                "reproduction_instructions": (
                    "Raw prompt: confidential customer request",
                ),
            }
        )
