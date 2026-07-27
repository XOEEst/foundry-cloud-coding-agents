from __future__ import annotations

from pathlib import Path
from typing import Protocol

from foundry_opt.campaign import CandidateArtifact
from foundry_opt.github_workflow.errors import (
    CampaignPublicationError,
    GitHubPermissionDeniedError,
)
from foundry_opt.github_workflow.models import (
    CampaignPublication,
    CampaignPublicationRequest,
    CandidateIssuePublication,
    GitHubCapabilities,
    GitHubPermissionReport,
    IssueReference,
    PullRequestReference,
    RepositoryState,
    WorkflowFailure,
)
from foundry_opt.preflight.redaction import redact


class CampaignGateway(Protocol):
    def verify_permissions(
        self,
        required: GitHubCapabilities,
    ) -> GitHubPermissionReport: ...

    def repository_state(self, repository_root: Path) -> RepositoryState: ...

    def artifact_url(
        self,
        repository: str,
        commit: str,
        path: Path,
    ) -> str: ...

    def find_campaign_pull_request(
        self,
        repository_root: Path,
        head_branch: str,
    ) -> PullRequestReference | None: ...

    def create_campaign_pull_request(
        self,
        repository_root: Path,
        *,
        base_branch: str,
        head_branch: str,
        head_commit: str,
        title: str,
        body: str,
    ) -> PullRequestReference: ...

    def find_candidate_issue(
        self,
        repository_root: Path,
        marker: str,
    ) -> IssueReference | None: ...

    def create_issue(
        self,
        repository_root: Path,
        *,
        title: str,
        body: str,
    ) -> IssueReference: ...

    def add_labels(
        self,
        repository_root: Path,
        issue_number: int,
        labels: tuple[str, ...],
    ) -> None: ...

    def link_sub_issue(
        self,
        repository_root: Path,
        parent_number: int,
        child_number: int,
    ) -> None: ...

    def add_dependency(
        self,
        repository_root: Path,
        issue_number: int,
        blocker_number: int,
    ) -> None: ...

    def update_issue_body(
        self,
        repository_root: Path,
        issue_number: int,
        body: str,
    ) -> None: ...

    def update_pull_request_body(
        self,
        repository_root: Path,
        pull_request_number: int,
        body: str,
    ) -> None: ...

    def close_pull_request(
        self,
        repository_root: Path,
        pull_request_number: int,
        comment: str,
    ) -> None: ...


def publish_campaign(
    request: CampaignPublicationRequest,
    gateway: CampaignGateway,
) -> CampaignPublication:
    required = GitHubCapabilities.CAMPAIGN_PUBLICATION
    permissions = gateway.verify_permissions(required)
    missing = required & ~permissions.granted
    if missing:
        raise GitHubPermissionDeniedError(missing)

    state = gateway.repository_state(request.repository_root)
    if state.default_commit != request.report.base_commit:
        raise CampaignPublicationError(
            "The default branch changed after the campaign started"
        )
    campaign_body = _campaign_body(request, gateway, state)
    campaign_pr = gateway.find_campaign_pull_request(
        request.repository_root,
        request.head_branch,
    )
    if campaign_pr is not None and (
        not campaign_pr.draft
        or campaign_pr.head_commit != request.head_commit
    ):
        raise CampaignPublicationError(
            "Existing campaign PR does not match the temporary draft"
        )
    if campaign_pr is None:
        campaign_pr = gateway.create_campaign_pull_request(
            request.repository_root,
            base_branch=state.default_branch,
            head_branch=request.head_branch,
            head_commit=request.head_commit,
            title=(
                f"[foundry-opt] campaign {request.report.campaign_id} "
                f"for {request.report.target}"
            ),
            body=campaign_body,
        )

    issues: list[CandidateIssuePublication] = []
    issue_by_candidate: dict[str, IssueReference] = {}
    failures: list[WorkflowFailure] = []
    candidates = {
        candidate.candidate_id: candidate
        for candidate in request.report.candidates
        if candidate.eligible
    }
    required_candidate_ids: set[str] = set()
    for candidate_id in request.report.pareto_candidate_ids:
        candidate = candidates.get(candidate_id)
        if candidate is None:
            continue
        required_candidate_ids.add(candidate_id)
        marker = _candidate_marker(request.report.campaign_id, candidate_id)
        issue = gateway.find_candidate_issue(
            request.repository_root,
            marker,
        )
        if issue is None:
            try:
                issue = gateway.create_issue(
                    request.repository_root,
                    title=(
                        f"[foundry-opt] {request.report.target} candidate "
                        f"{candidate_id}"
                    ),
                    body=_candidate_body(
                        request,
                        candidate,
                        campaign_pr,
                        marker,
                        gateway,
                        state,
                    ),
                )
            except RuntimeError:
                failures.append(
                    _failure(
                        "create_issue",
                        candidate_id,
                        "issue_create_failed",
                        "Candidate issue creation failed.",
                    )
                )
                continue
            try:
                gateway.add_labels(
                    request.repository_root,
                    issue.number,
                    ("ready-for-agent",),
                )
            except RuntimeError:
                failures.append(
                    _failure(
                        "add_labels",
                        candidate_id,
                        "label_failed",
                        "Canonical candidate label could not be applied.",
                    )
                )
            try:
                gateway.link_sub_issue(
                    request.repository_root,
                    campaign_pr.number,
                    issue.number,
                )
            except RuntimeError:
                task_body = _append_line(
                    campaign_pr.body or campaign_body,
                    f"- [ ] #{issue.number}",
                )
                try:
                    gateway.update_pull_request_body(
                        request.repository_root,
                        campaign_pr.number,
                        task_body,
                    )
                    campaign_pr = PullRequestReference(
                        number=campaign_pr.number,
                        url=campaign_pr.url,
                        head_branch=campaign_pr.head_branch,
                        head_commit=campaign_pr.head_commit,
                        draft=campaign_pr.draft,
                        body=task_body,
                    )
                except RuntimeError:
                    failures.append(
                        _failure(
                            "fallback_task_list",
                            candidate_id,
                            "fallback_link_failed",
                            "Native and task-list child linking failed.",
                        )
                    )
                else:
                    failures.append(
                        _failure(
                            "link_sub_issue",
                            candidate_id,
                            "sub_issue_fallback",
                            "Native sub-issues unavailable; task-list "
                            "fallback was used.",
                        )
                    )
        issues.append(CandidateIssuePublication(candidate_id, issue))
        issue_by_candidate[candidate_id] = issue

    for candidate_id, blocker_ids in (request.dependencies or {}).items():
        issue = issue_by_candidate.get(candidate_id)
        if issue is None:
            continue
        for blocker_id in blocker_ids:
            blocker = issue_by_candidate.get(blocker_id)
            if blocker is None:
                blocker = gateway.find_candidate_issue(
                    request.repository_root,
                    _candidate_marker(
                        request.report.campaign_id,
                        blocker_id,
                    ),
                )
            if blocker is None:
                failures.append(
                    _failure(
                        "add_dependency",
                        candidate_id,
                        "dependency_missing",
                        "A declared blocker issue was not found.",
                    )
                )
                continue
            try:
                gateway.add_dependency(
                    request.repository_root,
                    issue.number,
                    blocker.number,
                )
            except RuntimeError:
                fallback_body = _prepend_line(
                    issue.body,
                    f"Blocked by: #{blocker.number}",
                )
                try:
                    gateway.update_issue_body(
                        request.repository_root,
                        issue.number,
                        fallback_body,
                    )
                except RuntimeError:
                    failures.append(
                        _failure(
                            "fallback_dependency",
                            candidate_id,
                            "dependency_failed",
                            "Native and body dependency linking failed.",
                        )
                    )
                else:
                    issue = IssueReference(
                        issue.number,
                        issue.url,
                        issue.title,
                        fallback_body,
                    )
                    issue_by_candidate[candidate_id] = issue
                    failures.append(
                        _failure(
                            "add_dependency",
                            candidate_id,
                            "dependency_fallback",
                            "Native dependencies unavailable; body fallback "
                            "was used.",
                        )
                    )

    campaign_closed = False
    if request.cleanup_requested:
        available_candidates = set(request.candidate_pull_requests or {})
        if required_candidate_ids <= available_candidates:
            try:
                gateway.close_pull_request(
                    request.repository_root,
                    campaign_pr.number,
                    "Candidate pull requests are available; closing the "
                    "temporary campaign review surface.",
                )
            except RuntimeError:
                failures.append(
                    _failure(
                        "cleanup",
                        request.report.campaign_id,
                        "cleanup_failed",
                        "Temporary campaign PR cleanup failed.",
                    )
                )
            else:
                campaign_closed = True
        else:
            failures.append(
                _failure(
                    "cleanup",
                    request.report.campaign_id,
                    "cleanup_not_ready",
                    "Campaign PR remains open until all candidate PRs are "
                    "available.",
                )
            )

    published_issues = tuple(
        CandidateIssuePublication(
            publication.candidate_id,
            issue_by_candidate.get(
                publication.candidate_id,
                publication.issue,
            ),
        )
        for publication in issues
    )
    return CampaignPublication(
        campaign_pull_request=campaign_pr,
        candidate_issues=published_issues,
        failures=tuple(failures),
        campaign_closed=campaign_closed,
    )


def _campaign_body(
    request: CampaignPublicationRequest,
    gateway: CampaignGateway,
    state: RepositoryState,
) -> str:
    lines = [
        "<!-- foundry-opt:campaign:"
        f"{request.report.campaign_id} -->",
        "# Foundry optimization campaign",
        "",
        "**Temporary review surface; automation must never merge this PR.**",
        "",
        f"- Target: `{request.report.target}`",
        f"- Exact base commit: `{request.report.base_commit}`",
        f"- Baseline draft: `{request.report.baseline_draft_id}`",
        "",
        "## Redacted manifests",
    ]
    for manifest in request.manifests:
        url = gateway.artifact_url(
            state.repository,
            request.head_commit,
            manifest.path,
        )
        lines.append(
            f"- [`{manifest.path.as_posix()}`]({url}) — SHA-256 "
            f"`{manifest.sha256}`"
        )
    lines.extend(("", "## Pareto candidates"))
    candidates = {
        candidate.candidate_id: candidate
        for candidate in request.report.candidates
    }
    for candidate_id in request.report.pareto_candidate_ids:
        candidate = candidates[candidate_id]
        metrics = ", ".join(
            f"{name}={value:g}"
            for name, value in sorted(candidate.metrics.items())
        )
        patch_url = gateway.artifact_url(
            state.repository,
            request.head_commit,
            candidate.patch.path,
        )
        evidence_url = gateway.artifact_url(
            state.repository,
            request.head_commit,
            candidate.evidence_path,
        )
        lines.extend(
            (
                f"- **{candidate_id}** ({metrics})",
                f"  - Exact patch: [{candidate.patch.path.as_posix()}]"
                f"({patch_url}) — SHA-256 `{candidate.patch.sha256}`",
                f"  - Verified evidence: "
                f"[{candidate.evidence_path.as_posix()}]({evidence_url})"
                f" — SHA-256 "
                f"`{request.evidence_sha256[candidate_id]}`",
            )
        )
    return "\n".join(lines) + "\n"


def _candidate_body(
    request: CampaignPublicationRequest,
    candidate: CandidateArtifact,
    campaign_pr: PullRequestReference,
    marker: str,
    gateway: CampaignGateway,
    state: RepositoryState,
) -> str:
    patch_url = gateway.artifact_url(
        state.repository,
        request.head_commit,
        candidate.patch.path,
    )
    evidence_url = gateway.artifact_url(
        state.repository,
        request.head_commit,
        candidate.evidence_path,
    )
    instructions = "\n".join(
        f"{index}. {redact(instruction, request.sensitive_values)}"
        for index, instruction in enumerate(
            request.reproduction_instructions,
            start=1,
        )
    )
    return "\n".join(
        (
            marker,
            f"Part of #{campaign_pr.number}",
            "",
            f"Target: `{request.report.target}`",
            f"Base commit: `{candidate.patch.base_commit}`",
            f"Evaluated result commit: `{candidate.patch.result_commit}`",
            f"Draft: `{candidate.draft_id}`",
            f"Patch: [{candidate.patch.path.as_posix()}]({patch_url})",
            f"Patch SHA-256: `{candidate.patch.sha256}`",
            (
                "Evidence: "
                f"[{candidate.evidence_path.as_posix()}]({evidence_url})"
            ),
            (
                "Evidence SHA-256: "
                f"`{request.evidence_sha256[candidate.candidate_id]}`"
            ),
            "",
            "## Reproduction",
            instructions,
            "",
            "Apply only the exact verified patch. If the base or artifact "
            "differs, stop and start a new campaign; do not repair the patch.",
        )
    ) + "\n"


def _candidate_marker(campaign_id: str, candidate_id: str) -> str:
    return (
        f"<!-- foundry-opt:candidate:{campaign_id}:{candidate_id} -->"
    )


def _failure(
    operation: str,
    subject: str,
    code: str,
    message: str,
) -> WorkflowFailure:
    return WorkflowFailure(operation, subject, code, message)


def _append_line(body: str, line: str) -> str:
    normalized = body.rstrip()
    if line in normalized.splitlines():
        return normalized + "\n"
    return f"{normalized}\n\n{line}\n"


def _prepend_line(body: str, line: str) -> str:
    if line in body.splitlines():
        return body
    return f"{line}\n{body}"
