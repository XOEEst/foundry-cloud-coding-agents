from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

import pytest

from foundry_opt.campaign import (
    CampaignReport,
    CandidateArtifact,
    PatchArtifact,
)
from foundry_opt.evidence import EvaluationAssetReference
from foundry_opt.github_workflow import (
    ArtifactReference,
    CampaignPublicationError,
    CampaignPublicationRequest,
    CommitBlob,
    CommitInspection,
    GitHubCapabilities,
    GitHubPermissionReport,
    IssueReference,
    PullRequestReference,
    RedactionProvenance,
    RepositoryState,
    GitHubPermissionDeniedError,
    publish_campaign,
)

_PATCH_PATH = Path(
    ".foundry-optimizer/campaigns/c1/candidate-1.patch"
)
_EVIDENCE_PATH = Path(
    ".foundry-optimizer/campaigns/c1/candidate-1.json"
)
_MANIFEST_PATH = Path(
    ".foundry-optimizer/campaigns/c1/manifest.json"
)
_PATCH_BYTES = b"diff --git a/agent.py b/agent.py\n"
_GOAL_SHA256 = "1" * 64
_SPEC_SHA256 = "2" * 64
_ASSETS = (
    EvaluationAssetReference(
        asset_id="dataset-dev",
        kind="dataset",
        source="repository",
        role="development",
    ),
)
_PROVENANCE = RedactionProvenance(
    generator="foundry-opt.evidence",
    schema_version=1,
    source_sha256="1" * 64,
)
_MANIFEST_BYTES = json.dumps(
    {
        "schema_version": 1,
        "redaction_provenance": {
            "generator": _PROVENANCE.generator,
            "schema_version": _PROVENANCE.schema_version,
            "source_sha256": _PROVENANCE.source_sha256,
        },
    },
    sort_keys=True,
).encode()
_BASELINE_RESULT = {
    "subject_id": "baseline",
    "agent": {
        "agent_id": "agent-1",
        "draft_id": "draft-baseline",
        "version": "version-1",
    },
    "dataset": {"dataset_id": "dataset-1", "version": "version-1"},
    "evaluator": {
        "definition_id": "evaluator-1",
        "version": "version-1",
    },
    "evaluation_id": "evaluation-baseline",
    "run_id": "run-baseline",
    "attempts": [],
    "split": "validation",
    "portal_url": None,
    "complete": True,
    "repeat_count": 0,
    "duration_ms": 10.0,
    "usage": {
        "input_tokens": 1,
        "output_tokens": 1,
        "cached_tokens": 0,
    },
    "error_count": 0,
    "metrics": {},
    "cases": [],
}
_CANDIDATE_RESULT = {
    **_BASELINE_RESULT,
    "subject_id": "candidate-1",
    "agent": {
        "agent_id": "agent-1",
        "draft_id": "draft-candidate-1",
        "version": "version-2",
    },
    "evaluation_id": "evaluation-candidate-1",
    "run_id": "run-candidate-1",
    "patch_hash": hashlib.sha256(_PATCH_BYTES).hexdigest(),
}
_EVIDENCE_BYTES = json.dumps(
    {
        "schema_version": 1,
        "campaign_id": "campaign-1",
        "source_hash": "source-1",
        "goal_sha256": _GOAL_SHA256,
        "spec_sha256": _SPEC_SHA256,
        "assets": [
            {
                "asset_id": "dataset-dev",
                "kind": "dataset",
                "source": "repository",
                "role": "development",
                "name": None,
                "version": None,
                "remote_id": None,
                "content_sha256": None,
                "approval_gate": "policy",
                "metrics": [],
            }
        ],
        "baseline": _BASELINE_RESULT,
        "candidates": [_CANDIDATE_RESULT],
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
).encode()


@dataclass
class FakeGateway:
    campaign_pr: PullRequestReference | None = None
    permission_report: GitHubPermissionReport | None = None
    fail_labels: bool = False
    fail_sub_issue: bool = False
    fail_dependency: bool = False
    fail_issue: bool = False
    artifact_contents: dict[Path, bytes] | None = None
    base_is_ancestor: bool = True
    extra_changed_paths: tuple[Path, ...] = ()
    candidate_prs: dict[str, PullRequestReference] | None = None

    def __post_init__(self) -> None:
        if self.artifact_contents is None:
            self.artifact_contents = {
                _PATCH_PATH: _PATCH_BYTES,
                _EVIDENCE_PATH: _EVIDENCE_BYTES,
                _MANIFEST_PATH: _MANIFEST_BYTES,
            }
        self.created_campaigns: list[tuple[str, str, str, str]] = []
        self.created_issues: list[tuple[str, str]] = []
        self.labels: list[tuple[int, tuple[str, ...]]] = []
        self.links: list[tuple[int, int]] = []
        self.updated_issues: list[tuple[int, str]] = []
        self.updated_prs: list[tuple[int, str]] = []
        self.dependencies: list[tuple[int, int]] = []
        self.removed_labels: list[tuple[int, tuple[str, ...]]] = []
        self.reopened_issues: list[int] = []
        self.closed_prs: list[int] = []
        self.events: list[str] = []
        self.existing_issues: dict[str, IssueReference] = {}
        self.native_children: set[tuple[int, int]] = set()
        if self.candidate_prs is None:
            self.candidate_prs = {}

    def inspect_commit(
        self,
        repository_root: Path,
        *,
        base_commit: str,
        head_commit: str,
        artifact_paths: tuple[Path, ...],
    ) -> CommitInspection:
        assert self.artifact_contents is not None
        blobs = tuple(
            CommitBlob(path, self.artifact_contents[path])
            for path in artifact_paths
            if path in self.artifact_contents
        )
        return CommitInspection(
            base_commit=base_commit,
            head_commit=head_commit,
            base_is_ancestor=self.base_is_ancestor,
            changed_paths=(
                *artifact_paths,
                *self.extra_changed_paths,
            ),
            blobs=blobs,
        )

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

    def find_candidate_pull_request(
        self,
        repository_root: Path,
        head_branch: str,
    ) -> PullRequestReference | None:
        assert self.candidate_prs is not None
        return self.candidate_prs.get(head_branch)

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
            base_branch=base_branch,
            state="OPEN",
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
            state="OPEN",
            labels=(),
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

    def remove_labels(
        self,
        repository_root: Path,
        issue_number: int,
        labels: tuple[str, ...],
    ) -> None:
        self.removed_labels.append((issue_number, labels))

    def reopen_issue(
        self,
        repository_root: Path,
        issue_number: int,
    ) -> None:
        self.reopened_issues.append(issue_number)

    def is_sub_issue(
        self,
        repository_root: Path,
        parent_number: int,
        child_number: int,
    ) -> bool:
        return (parent_number, child_number) in self.native_children

    def link_sub_issue(
        self,
        repository_root: Path,
        parent_number: int,
        child_number: int,
    ) -> None:
        if self.fail_sub_issue:
            raise RuntimeError("sub-issues unavailable")
        self.links.append((parent_number, child_number))
        self.native_children.add((parent_number, child_number))

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
        path=_PATCH_PATH,
        sha256=hashlib.sha256(_PATCH_BYTES).hexdigest(),
        base_commit="b" * 40,
        result_commit="c" * 40,
    )
    candidate = CandidateArtifact(
        candidate_id="candidate-1",
        patch=patch,
        draft_id="draft-candidate-1",
        evidence_path=_EVIDENCE_PATH,
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
        goal_sha256=_GOAL_SHA256,
        spec_sha256=_SPEC_SHA256,
        assets=_ASSETS,
    )


def _request() -> CampaignPublicationRequest:
    return CampaignPublicationRequest(
        repository_root=Path("repository"),
        report=_report(),
        head_branch="foundry-opt/campaign-1",
        head_commit="d" * 40,
        manifests=(
            ArtifactReference(
                path=_MANIFEST_PATH,
                sha256=hashlib.sha256(_MANIFEST_BYTES).hexdigest(),
                provenance=_PROVENANCE,
            ),
        ),
        evidence_sha256={
            "candidate-1": hashlib.sha256(_EVIDENCE_BYTES).hexdigest()
        },
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
    assert (
        "Patch SHA-256: `" + _report().candidates[0].patch.sha256 + "`"
        in body
    )
    assert (
        "Evidence SHA-256: `"
        + hashlib.sha256(_EVIDENCE_BYTES).hexdigest()
        + "`"
        in body
    )
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
        body=(
            "<!-- foundry-opt:campaign:campaign-1 -->\n"
            "- Exact base commit: `" + "b" * 40 + "`\n"
            "Temporary review surface; automation must never merge this PR."
        ),
        base_branch="main",
        state="OPEN",
    )
    marker = "<!-- foundry-opt:candidate:campaign-1:candidate-1 -->"
    existing_issue = IssueReference(
        number=101,
        url="https://github.com/octo-org/optimizer/issues/101",
        title="candidate",
        body=marker,
        state="OPEN",
        labels=("ready-for-agent",),
    )
    gateway = FakeGateway(campaign_pr=existing_pr)
    gateway.existing_issues[marker] = existing_issue
    gateway.native_children.add((42, 101))

    publication = publish_campaign(request, gateway)

    assert publication.campaign_pull_request == existing_pr
    assert publication.candidate_issues[0].issue.number == existing_issue.number
    assert "Patch SHA-256" in publication.candidate_issues[0].issue.body
    assert gateway.created_campaigns == []
    assert gateway.created_issues == []
    assert gateway.updated_issues
    assert gateway.labels == []
    assert gateway.links == []


def test_existing_candidate_issue_is_reconciled_on_retry() -> None:
    request = _request()
    campaign_body = (
        "<!-- foundry-opt:campaign:campaign-1 -->\n"
        "- Exact base commit: `" + "b" * 40 + "`\n"
        "Temporary review surface; automation must never merge this PR."
    )
    campaign_pr = PullRequestReference(
        42,
        "https://github.com/octo-org/optimizer/pull/42",
        request.head_branch,
        request.head_commit,
        True,
        campaign_body,
        "main",
        "OPEN",
    )
    marker = "<!-- foundry-opt:candidate:campaign-1:candidate-1 -->"
    stale = IssueReference(
        101,
        "https://github.com/octo-org/optimizer/issues/101",
        "stale",
        marker + "\nstale body",
        state="CLOSED",
        labels=("needs-triage",),
    )
    gateway = FakeGateway(campaign_pr=campaign_pr)
    gateway.existing_issues[marker] = stale

    publication = publish_campaign(request, gateway)

    reconciled = publication.candidate_issues[0].issue
    assert reconciled.state == "OPEN"
    assert reconciled.labels == ("ready-for-agent",)
    assert "Patch SHA-256" in reconciled.body
    assert gateway.reopened_issues == [101]
    assert gateway.removed_labels == [(101, ("needs-triage",))]
    assert gateway.labels == [(101, ("ready-for-agent",))]
    assert gateway.links == [(42, 101)]


@pytest.mark.parametrize(
    ("changes",),
    [
        ({"state": "CLOSED"},),
        ({"base_branch": "release"},),
        ({"head_commit": "9" * 40},),
        ({"draft": False},),
        ({"body": "<!-- unrelated -->"},),
    ],
)
def test_campaign_pr_reuse_rejects_stale_or_unrelated_prs(
    changes: dict[str, object],
) -> None:
    request = _request()
    values: dict[str, object] = {
        "number": 42,
        "url": "https://github.com/octo-org/optimizer/pull/42",
        "head_branch": request.head_branch,
        "head_commit": request.head_commit,
        "draft": True,
        "body": (
            "<!-- foundry-opt:campaign:campaign-1 -->\n"
            "- Exact base commit: `" + "b" * 40 + "`\n"
            "Temporary review surface; automation must never merge this PR."
        ),
        "base_branch": "main",
        "state": "OPEN",
    }
    values.update(changes)
    gateway = FakeGateway(
        campaign_pr=PullRequestReference(**values)
    )

    with pytest.raises(CampaignPublicationError):
        publish_campaign(request, gateway)

    assert gateway.created_campaigns == []


def test_sub_issue_failure_uses_task_list_fallback_and_is_explicit() -> None:
    gateway = FakeGateway(fail_sub_issue=True)

    publication = publish_campaign(_request(), gateway)

    assert gateway.updated_prs
    assert "- [ ] #101" in gateway.updated_prs[0][1]
    assert "Part of #42" in gateway.created_issues[0][1]
    assert publication.failures == ()


def test_labels_and_dependencies_are_best_effort_and_explicit() -> None:
    report = _report()
    request = CampaignPublicationRequest(
        repository_root=Path("repository"),
        report=report,
        head_branch="foundry-opt/campaign-1",
        head_commit="d" * 40,
        manifests=(),
        evidence_sha256={
            "candidate-1": hashlib.sha256(_EVIDENCE_BYTES).hexdigest()
        },
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


def test_publication_rejects_ineligible_pareto_candidate() -> None:
    eligible = _report().candidates[0]
    ineligible = CandidateArtifact(
        candidate_id=eligible.candidate_id,
        patch=eligible.patch,
        draft_id=eligible.draft_id,
        evidence_path=eligible.evidence_path,
        eligible=False,
        metrics=eligible.metrics,
    )
    report = CampaignReport(
        campaign_id="campaign-1",
        target="support-agent",
        base_commit="b" * 40,
        baseline_draft_id="draft-baseline",
        candidates=(ineligible,),
        pareto_candidate_ids=("candidate-1",),
        goal_sha256=_GOAL_SHA256,
        spec_sha256=_SPEC_SHA256,
        assets=_ASSETS,
    )
    request = _request()

    with pytest.raises(ValueError, match="eligible"):
        CampaignPublicationRequest(
            **{
                **request.__dict__,
                "report": report,
            }
        )


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
        (
            "<!-- foundry-opt:candidate-pr:"
            "campaign-1:candidate-1:session-1 -->\n"
            "Base commit: `" + _report().base_commit + "`\n"
            "Patch SHA-256: `"
            + _report().candidates[0].patch.sha256
            + "`\n"
            "Evidence SHA-256: `"
            + _request().evidence_sha256["candidate-1"]
            + "`"
        ),
        "main",
        "OPEN",
    )
    ready = CampaignPublicationRequest(
        **{
            **request.__dict__,
            "cleanup_requested": True,
            "candidate_pull_requests": {"candidate-1": candidate_pr},
        }
    )
    gateway = FakeGateway(
        candidate_prs={candidate_pr.head_branch: candidate_pr}
    )

    publication = publish_campaign(ready, gateway)

    assert publication.campaign_closed is True
    assert gateway.events[-1] == "campaign-close"
    assert gateway.events.index("issue-101") < gateway.events.index(
        "campaign-close"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "CLOSED"},
        {"draft": True},
        {"base_branch": "release"},
        {"head_commit": "9" * 40},
        {"body": "<!-- unrelated -->"},
    ],
)
def test_cleanup_rejects_unverified_candidate_pr(
    changes: dict[str, object],
) -> None:
    request = _request()
    values: dict[str, object] = {
        "number": 55,
        "url": "https://github.com/octo-org/optimizer/pull/55",
        "head_branch": "foundry-opt/campaign-1/candidate-1/session-1",
        "head_commit": "1" * 40,
        "draft": False,
        "body": (
            "<!-- foundry-opt:candidate-pr:"
            "campaign-1:candidate-1:session-1 -->\n"
            "Base commit: `" + request.report.base_commit + "`\n"
            "Patch SHA-256: `"
            + request.report.candidates[0].patch.sha256
            + "`\n"
            "Evidence SHA-256: `"
            + request.evidence_sha256["candidate-1"]
            + "`"
        ),
        "base_branch": "main",
        "state": "OPEN",
    }
    values.update(changes)
    expected = PullRequestReference(
        55,
        "https://github.com/octo-org/optimizer/pull/55",
        "foundry-opt/campaign-1/candidate-1/session-1",
        "1" * 40,
        False,
    )
    remote = PullRequestReference(**values)
    cleanup = CampaignPublicationRequest(
        **{
            **request.__dict__,
            "cleanup_requested": True,
            "candidate_pull_requests": {"candidate-1": expected},
        }
    )
    gateway = FakeGateway(
        candidate_prs={expected.head_branch: remote}
    )

    publication = publish_campaign(cleanup, gateway)

    assert publication.campaign_closed is False
    assert publication.failures[-1].code == "cleanup_not_ready"
    assert gateway.closed_prs == []


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


@pytest.mark.parametrize(
    ("gateway", "message"),
    [
        (
            FakeGateway(base_is_ancestor=False),
            "descend",
        ),
        (
            FakeGateway(extra_changed_paths=(Path("secrets.txt"),)),
            "allowed",
        ),
        (
            FakeGateway(
                artifact_contents={
                    _PATCH_PATH: b"changed patch",
                    _EVIDENCE_PATH: _EVIDENCE_BYTES,
                    _MANIFEST_PATH: _MANIFEST_BYTES,
                }
            ),
            "hash",
        ),
        (
            FakeGateway(
                artifact_contents={
                    _PATCH_PATH: _PATCH_BYTES,
                    _EVIDENCE_PATH: b'{"schema_version":1}',
                    _MANIFEST_PATH: _MANIFEST_BYTES,
                }
            ),
            "hash",
        ),
    ],
)
def test_publication_rejects_unverified_head_artifacts(
    gateway: FakeGateway,
    message: str,
) -> None:
    with pytest.raises(CampaignPublicationError, match=message):
        publish_campaign(_request(), gateway)

    assert gateway.created_campaigns == []
    assert gateway.created_issues == []


def test_publication_requires_manifest_redaction_provenance() -> None:
    bad_manifest = json.dumps(
        {"schema_version": 1},
        sort_keys=True,
    ).encode()
    request = _request()
    request = CampaignPublicationRequest(
        **{
            **request.__dict__,
            "manifests": (
                ArtifactReference(
                    path=_MANIFEST_PATH,
                    sha256=hashlib.sha256(bad_manifest).hexdigest(),
                    provenance=_PROVENANCE,
                ),
            ),
        }
    )
    gateway = FakeGateway(
        artifact_contents={
            _PATCH_PATH: _PATCH_BYTES,
            _EVIDENCE_PATH: _EVIDENCE_BYTES,
            _MANIFEST_PATH: bad_manifest,
        }
    )

    with pytest.raises(CampaignPublicationError, match="provenance"):
        publish_campaign(request, gateway)

    assert gateway.created_campaigns == []


def test_publication_rejects_manifest_payload_fields_and_sensitive_values() -> None:
    secret = "sentinel-sensitive-value"
    manifest = json.loads(_MANIFEST_BYTES)
    manifest["content"] = {"messages": ["raw payload"]}
    manifest["redaction_provenance"]["generator"] = secret
    content = json.dumps(manifest, sort_keys=True).encode()
    request = _request()
    request = CampaignPublicationRequest(
        **{
            **request.__dict__,
            "manifests": (
                ArtifactReference(
                    path=_MANIFEST_PATH,
                    sha256=hashlib.sha256(content).hexdigest(),
                    provenance=RedactionProvenance(
                        generator=secret,
                        schema_version=1,
                        source_sha256="1" * 64,
                    ),
                ),
            ),
            "sensitive_values": (secret,),
        }
    )
    gateway = FakeGateway(
        artifact_contents={
            _PATCH_PATH: _PATCH_BYTES,
            _EVIDENCE_PATH: _EVIDENCE_BYTES,
            _MANIFEST_PATH: content,
        }
    )

    with pytest.raises(CampaignPublicationError, match="schema|sensitive"):
        publish_campaign(request, gateway)


def test_publication_rejects_evidence_not_bound_to_exact_patch() -> None:
    bad_evidence = json.dumps(
        {
            **json.loads(_EVIDENCE_BYTES),
            "candidates": [
                {
                    **_CANDIDATE_RESULT,
                    "patch_hash": "9" * 64,
                }
            ],
        },
        sort_keys=True,
    ).encode()
    request = _request()
    request = CampaignPublicationRequest(
        **{
            **request.__dict__,
            "evidence_sha256": {
                "candidate-1": hashlib.sha256(bad_evidence).hexdigest()
            },
        }
    )
    gateway = FakeGateway(
        artifact_contents={
            _PATCH_PATH: _PATCH_BYTES,
            _EVIDENCE_PATH: bad_evidence,
            _MANIFEST_PATH: _MANIFEST_BYTES,
        }
    )

    with pytest.raises(CampaignPublicationError, match="provenance"):
        publish_campaign(request, gateway)


def test_publication_rejects_evidence_with_wrong_spec_identity() -> None:
    document = json.loads(_EVIDENCE_BYTES)
    document["spec_sha256"] = "9" * 64
    bad_evidence = json.dumps(document, sort_keys=True).encode()
    request = CampaignPublicationRequest(
        **{
            **_request().__dict__,
            "evidence_sha256": {
                "candidate-1": hashlib.sha256(bad_evidence).hexdigest()
            },
        }
    )
    gateway = FakeGateway(
        artifact_contents={
            _PATCH_PATH: _PATCH_BYTES,
            _EVIDENCE_PATH: bad_evidence,
            _MANIFEST_PATH: _MANIFEST_BYTES,
        }
    )

    with pytest.raises(CampaignPublicationError, match="provenance"):
        publish_campaign(request, gateway)


@pytest.mark.parametrize(
    ("path", "field"),
    [
        (("baseline",), "messages"),
        (("candidates", 0, "agent"), "content"),
        (("candidates", 0, "cases"), "messages"),
    ],
)
def test_publication_rejects_nested_payload_bearing_evidence_fields(
    path: tuple[object, ...],
    field: str,
) -> None:
    document = json.loads(_EVIDENCE_BYTES)
    current: object = document
    for part in path:
        current = current[part]  # type: ignore[index]
    if isinstance(current, list):
        current.append({field: ["raw payload"]})
    else:
        current[field] = "raw payload"  # type: ignore[index]
    content = json.dumps(document, sort_keys=True).encode()
    request = _request()
    request = CampaignPublicationRequest(
        **{
            **request.__dict__,
            "evidence_sha256": {
                "candidate-1": hashlib.sha256(content).hexdigest()
            },
        }
    )
    gateway = FakeGateway(
        artifact_contents={
            _PATCH_PATH: _PATCH_BYTES,
            _EVIDENCE_PATH: content,
            _MANIFEST_PATH: _MANIFEST_BYTES,
        }
    )

    with pytest.raises(CampaignPublicationError, match="schema"):
        publish_campaign(request, gateway)


def test_publication_rejects_sensitive_values_inside_allowed_evidence_fields() -> None:
    secret = "sentinel-sensitive-value"
    document = json.loads(_EVIDENCE_BYTES)
    document["candidates"][0]["agent"]["version"] = secret
    content = json.dumps(document, sort_keys=True).encode()
    request = _request()
    request = CampaignPublicationRequest(
        **{
            **request.__dict__,
            "evidence_sha256": {
                "candidate-1": hashlib.sha256(content).hexdigest()
            },
            "sensitive_values": (secret,),
        }
    )
    gateway = FakeGateway(
        artifact_contents={
            _PATCH_PATH: _PATCH_BYTES,
            _EVIDENCE_PATH: content,
            _MANIFEST_PATH: _MANIFEST_BYTES,
        }
    )

    with pytest.raises(CampaignPublicationError, match="sensitive"):
        publish_campaign(request, gateway)
