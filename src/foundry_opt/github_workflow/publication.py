from __future__ import annotations

from pathlib import Path
import json
from math import isfinite
import re
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
    ArtifactReference,
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

    def inspect_commit(
        self,
        repository_root: Path,
        *,
        base_commit: str,
        head_commit: str,
        artifact_paths: tuple[Path, ...],
    ) -> CommitInspection: ...

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

    def find_candidate_pull_request(
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

    def remove_labels(
        self,
        repository_root: Path,
        issue_number: int,
        labels: tuple[str, ...],
    ) -> None: ...

    def reopen_issue(
        self,
        repository_root: Path,
        issue_number: int,
    ) -> None: ...

    def is_sub_issue(
        self,
        repository_root: Path,
        parent_number: int,
        child_number: int,
    ) -> bool: ...

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
    _verify_campaign_commit(request, gateway)
    campaign_body = _campaign_body(request, gateway, state)
    campaign_pr = gateway.find_campaign_pull_request(
        request.repository_root,
        request.head_branch,
    )
    if campaign_pr is not None and not _campaign_pr_matches(
        campaign_pr,
        request,
        state,
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
        if not _campaign_pr_matches(campaign_pr, request, state):
            raise CampaignPublicationError(
                "Created campaign PR does not match the temporary draft"
            )

    issues: list[CandidateIssuePublication] = []
    issue_by_candidate: dict[str, IssueReference] = {}
    failures: list[WorkflowFailure] = []
    candidates = {
        candidate.candidate_id: candidate
        for candidate in request.report.candidates
    }
    required_candidate_ids = set(request.report.pareto_candidate_ids)
    for candidate_id in request.report.pareto_candidate_ids:
        candidate = candidates[candidate_id]
        marker = _candidate_marker(request.report.campaign_id, candidate_id)
        issue = gateway.find_candidate_issue(
            request.repository_root,
            marker,
        )
        expected_body = _candidate_body(
            request,
            candidate,
            campaign_pr,
            marker,
            gateway,
            state,
        )
        if issue is None:
            try:
                issue = gateway.create_issue(
                    request.repository_root,
                    title=(
                        f"[foundry-opt] {request.report.target} candidate "
                        f"{candidate_id}"
                    ),
                    body=expected_body,
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
        issue, campaign_pr, reconciliation_failures = _reconcile_issue(
            request,
            gateway,
            candidate_id=candidate_id,
            issue=issue,
            expected_body=expected_body,
            campaign_pr=campaign_pr,
            campaign_body=campaign_body,
        )
        failures.extend(reconciliation_failures)
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
                        state=issue.state,
                        labels=issue.labels,
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
        if _candidate_prs_ready(
            request,
            gateway,
            state,
            required_candidate_ids,
        ):
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


def _candidate_prs_ready(
    request: CampaignPublicationRequest,
    gateway: CampaignGateway,
    repository_state: RepositoryState,
    required_candidate_ids: set[str],
) -> bool:
    expected_pull_requests = request.candidate_pull_requests or {}
    if set(expected_pull_requests) != required_candidate_ids:
        return False
    candidates = {
        candidate.candidate_id: candidate
        for candidate in request.report.candidates
    }
    for candidate_id in sorted(required_candidate_ids):
        expected = expected_pull_requests[candidate_id]
        try:
            actual = gateway.find_candidate_pull_request(
                request.repository_root,
                expected.head_branch,
            )
        except RuntimeError:
            return False
        candidate = candidates[candidate_id]
        marker = re.compile(
            r"<!-- foundry-opt:candidate-pr:"
            + re.escape(request.report.campaign_id)
            + ":"
            + re.escape(candidate_id)
            + r":[A-Za-z0-9._-]+ -->"
        )
        if (
            actual is None
            or actual.number != expected.number
            or actual.state != "OPEN"
            or actual.draft
            or actual.base_branch != repository_state.default_branch
            or actual.head_branch != expected.head_branch
            or actual.head_commit != expected.head_commit
            or marker.search(actual.body) is None
            or f"Base commit: `{candidate.patch.base_commit}`"
            not in actual.body
            or f"Patch SHA-256: `{candidate.patch.sha256}`"
            not in actual.body
            or (
                "Evidence SHA-256: "
                f"`{request.evidence_sha256[candidate_id]}`"
            )
            not in actual.body
        ):
            return False
    return True


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


def _verify_campaign_commit(
    request: CampaignPublicationRequest,
    gateway: CampaignGateway,
) -> None:
    candidate_by_id = {
        candidate.candidate_id: candidate
        for candidate in request.report.candidates
    }
    artifact_paths = tuple(
        dict.fromkeys(
            (
                *(manifest.path for manifest in request.manifests),
                *(
                    path
                    for candidate_id in request.report.pareto_candidate_ids
                    for path in (
                        candidate_by_id[candidate_id].patch.path,
                        candidate_by_id[candidate_id].evidence_path,
                    )
                ),
            )
        )
    )
    inspection = gateway.inspect_commit(
        request.repository_root,
        base_commit=request.report.base_commit,
        head_commit=request.head_commit,
        artifact_paths=artifact_paths,
    )
    if (
        inspection.base_commit != request.report.base_commit
        or inspection.head_commit != request.head_commit
        or not inspection.base_is_ancestor
    ):
        raise CampaignPublicationError(
            "Campaign head must descend from the exact campaign base"
        )
    allowed_paths = set(artifact_paths)
    if set(inspection.changed_paths) != allowed_paths:
        raise CampaignPublicationError(
            "Campaign head must change exactly the allowed artifacts"
        )
    blobs = {blob.path: blob for blob in inspection.blobs}
    if set(blobs) != allowed_paths:
        raise CampaignPublicationError(
            "Campaign head is missing a required artifact"
        )
    for manifest in request.manifests:
        blob = blobs[manifest.path]
        if blob.sha256 != manifest.sha256:
            raise CampaignPublicationError(
                "Campaign manifest hash does not match the exact head blob"
            )
        _verify_manifest_provenance(
            blob.content,
            manifest,
            request.sensitive_values,
        )
    for candidate_id in request.report.pareto_candidate_ids:
        candidate = candidate_by_id[candidate_id]
        if blobs[candidate.patch.path].sha256 != candidate.patch.sha256:
            raise CampaignPublicationError(
                "Campaign patch hash does not match the exact head blob"
            )
        evidence = blobs[candidate.evidence_path]
        if evidence.sha256 != request.evidence_sha256[candidate_id]:
            raise CampaignPublicationError(
                "Campaign evidence hash does not match the exact head blob"
            )
        _verify_redacted_evidence(
            evidence.content,
            request.report.campaign_id,
            candidate_id,
            candidate.patch.sha256,
            request.sensitive_values,
            expected_goal_sha256=request.report.goal_sha256,
            expected_spec_sha256=request.report.spec_sha256,
            expected_assets=request.report.assets,
        )


def _verify_manifest_provenance(
    content: bytes,
    manifest: ArtifactReference,
    sensitive_values: tuple[str, ...],
) -> None:
    try:
        document = json.loads(content)
        _strict_object(
            document,
            required={"schema_version", "redaction_provenance"},
        )
        if document["schema_version"] != 1:
            raise ValueError
        provenance = document["redaction_provenance"]
        _strict_object(
            provenance,
            required={
                "generator",
                "schema_version",
                "source_sha256",
            },
        )
        expected = manifest.provenance
        if (
            not isinstance(provenance, dict)
            or provenance.get("generator") != expected.generator
            or provenance.get("schema_version")
            != expected.schema_version
            or provenance.get("source_sha256")
            != expected.source_sha256
        ):
            raise ValueError
        _reject_sensitive_values(document, sensitive_values)
    except _SensitiveArtifactError as error:
        raise CampaignPublicationError(
            "Campaign manifest contains a sensitive value"
        ) from error
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        raise CampaignPublicationError(
            "Campaign manifest schema or redaction provenance is invalid"
        ) from error


def _verify_redacted_evidence(
    content: bytes,
    campaign_id: str,
    candidate_id: str,
    patch_sha256: str,
    sensitive_values: tuple[str, ...],
    *,
    expected_goal_sha256: str | None = None,
    expected_spec_sha256: str | None = None,
    expected_assets: tuple[object, ...] = (),
) -> None:
    try:
        document = json.loads(content)
        _validate_evidence_document(document)
        candidates = document["candidates"]
        pareto = document["pareto"]
        if (
            expected_goal_sha256 is not None
            and document.get("goal_sha256") != expected_goal_sha256
        ) or (
            expected_spec_sha256 is not None
            and document.get("spec_sha256") != expected_spec_sha256
        ):
            raise ValueError
        if expected_assets:
            actual_assets = document.get("assets")
            if not isinstance(actual_assets, list):
                raise ValueError
            expected_documents = sorted(
                (_asset_document(asset) for asset in expected_assets),
                key=lambda item: str(item["asset_id"]),
            )
            if sorted(
                actual_assets,
                key=lambda item: (
                    str(item.get("asset_id"))
                    if isinstance(item, dict)
                    else ""
                ),
            ) != expected_documents:
                raise ValueError
        if (
            document.get("schema_version") != 1
            or document.get("campaign_id") != campaign_id
            or not isinstance(candidates, list)
            or not isinstance(pareto, dict)
            or candidate_id not in pareto.get("eligible_ids", ())
            or not any(
                isinstance(candidate, dict)
                and candidate.get("subject_id") == candidate_id
                and candidate.get("patch_hash") == patch_sha256
                for candidate in candidates
            )
        ):
            raise ValueError
        _reject_sensitive_values(document, sensitive_values)
    except _SensitiveArtifactError as error:
        raise CampaignPublicationError(
            "Campaign evidence contains a sensitive value"
        ) from error
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        raise CampaignPublicationError(
            "Campaign evidence schema or redaction provenance is invalid"
        ) from error


class _SensitiveArtifactError(ValueError):
    pass


def _strict_object(
    value: object,
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError
    keys = set(value)
    allowed = required | (optional or set())
    if not required <= keys or not keys <= allowed:
        raise ValueError
    return value


def _validate_evidence_document(value: object) -> None:
    document = _strict_object(
        value,
        required={
            "schema_version",
            "campaign_id",
            "source_hash",
            "baseline",
            "candidates",
            "pareto",
        },
        optional={
            "goal_sha256",
            "spec_sha256",
            "assets",
            "generated_at",
            "telemetry",
        },
    )
    if document["schema_version"] != 1:
        raise ValueError
    _metadata_string(document["campaign_id"])
    _metadata_string(document["source_hash"])
    if "goal_sha256" in document:
        _sha256(document["goal_sha256"])
    if "spec_sha256" in document:
        _sha256(document["spec_sha256"])
    if "assets" in document:
        assets = document["assets"]
        if not isinstance(assets, list):
            raise ValueError
        asset_ids = tuple(_validate_asset(asset) for asset in assets)
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError
    if "generated_at" in document:
        _nullable_metadata_string(document["generated_at"])
    _validate_result(document["baseline"], candidate=False)
    candidates = document["candidates"]
    if not isinstance(candidates, list):
        raise ValueError
    for candidate in candidates:
        _validate_result(candidate, candidate=True)
    _validate_pareto(document["pareto"])
    if "telemetry" in document:
        telemetry = document["telemetry"]
        if not isinstance(telemetry, list):
            raise ValueError
        for item in telemetry:
            _validate_telemetry(item)


def _asset_document(asset: object) -> dict[str, object]:
    return {
        "asset_id": getattr(asset, "asset_id"),
        "kind": getattr(asset, "kind"),
        "source": getattr(asset, "source"),
        "role": getattr(asset, "role"),
        "name": getattr(asset, "name"),
        "version": getattr(asset, "version"),
        "remote_id": getattr(asset, "remote_id"),
        "content_sha256": getattr(asset, "content_sha256"),
        "approval_gate": getattr(asset, "approval_gate"),
        "metrics": list(getattr(asset, "metrics", ())),
    }


def _validate_asset(value: object) -> str:
    asset = _strict_object(
        value,
        required={
            "asset_id",
            "kind",
            "source",
            "role",
            "name",
            "version",
            "remote_id",
            "content_sha256",
            "approval_gate",
            "metrics",
        },
    )
    asset_id = _metadata_string(asset["asset_id"])
    if asset["kind"] not in {"dataset", "evaluator"}:
        raise ValueError
    _metadata_string(asset["source"])
    _nullable_metadata_string(asset["role"])
    _nullable_metadata_string(asset["name"])
    _nullable_metadata_string(asset["version"])
    _nullable_metadata_string(asset["remote_id"])
    if asset["content_sha256"] is not None:
        _sha256(asset["content_sha256"])
    if asset["approval_gate"] not in {"policy", "human"}:
        raise ValueError
    metrics = asset["metrics"]
    if not isinstance(metrics, list):
        raise ValueError
    for metric in metrics:
        _metadata_string(metric)
    if asset["kind"] == "dataset" and metrics:
        raise ValueError
    if asset["kind"] == "evaluator" and not metrics:
        raise ValueError
    return asset_id


def _validate_result(value: object, *, candidate: bool) -> None:
    required = {
        "subject_id",
        "agent",
        "dataset",
        "evaluator",
        "evaluation_id",
        "run_id",
        "attempts",
        "split",
        "portal_url",
        "complete",
        "repeat_count",
        "duration_ms",
        "usage",
        "error_count",
        "metrics",
        "cases",
    }
    if candidate:
        required.add("patch_hash")
    result = _strict_object(value, required=required)
    for key in ("subject_id", "evaluation_id", "run_id", "split"):
        _metadata_string(result[key])
    _nullable_metadata_string(result["portal_url"])
    _boolean(result["complete"])
    _nonnegative_integer(result["repeat_count"])
    _nonnegative_number(result["duration_ms"])
    _nonnegative_integer(result["error_count"])
    if candidate:
        _sha256(result["patch_hash"])
    _validate_identity(
        result["agent"],
        {"agent_id", "draft_id", "version"},
    )
    _validate_identity(
        result["dataset"],
        {"dataset_id", "version"},
    )
    _validate_identity(
        result["evaluator"],
        {"definition_id", "version"},
    )
    attempts = result["attempts"]
    if not isinstance(attempts, list):
        raise ValueError
    for attempt in attempts:
        _validate_attempt(attempt)
    _validate_usage(result["usage"])
    metrics = result["metrics"]
    if not isinstance(metrics, dict):
        raise ValueError
    for name, metric in metrics.items():
        _metadata_string(name)
        _validate_metric(metric)
    cases = result["cases"]
    if not isinstance(cases, list):
        raise ValueError
    for case in cases:
        _validate_case(case)


def _validate_identity(value: object, keys: set[str]) -> None:
    identity = _strict_object(value, required=keys)
    for item in identity.values():
        _metadata_string(item)


def _validate_attempt(value: object) -> None:
    attempt = _strict_object(
        value,
        required={
            "evaluation_id",
            "run_id",
            "status",
            "started_at",
            "completed_at",
            "error_code",
        },
    )
    for key in ("evaluation_id", "run_id", "status"):
        _metadata_string(attempt[key])
    for key in ("started_at", "completed_at", "error_code"):
        _nullable_metadata_string(attempt[key])


def _validate_usage(value: object) -> None:
    usage = _strict_object(
        value,
        required={"input_tokens", "output_tokens", "cached_tokens"},
    )
    for item in usage.values():
        _nonnegative_integer(item)


def _validate_metric(value: object) -> None:
    metric = _strict_object(
        value,
        required={
            "median",
            "minimum",
            "maximum",
            "spread",
            "outcome",
            "sample_count",
        },
    )
    for key in ("median", "minimum", "maximum", "spread"):
        _nullable_number(metric[key])
    _metadata_string(metric["outcome"])
    _nonnegative_integer(metric["sample_count"])


def _validate_case(value: object) -> None:
    case = _strict_object(
        value,
        required={
            "case_id",
            "case_hash",
            "response_ids",
            "duration_ms",
            "reason_code",
            "error_code",
            "scores",
            "usage",
            "trajectory",
        },
    )
    for key in ("case_id", "case_hash", "reason_code"):
        _metadata_string(case[key])
    _nullable_metadata_string(case["error_code"])
    _nonnegative_number(case["duration_ms"])
    response_ids = case["response_ids"]
    if not isinstance(response_ids, list):
        raise ValueError
    for response_id in response_ids:
        _metadata_string(response_id)
    scores = case["scores"]
    if not isinstance(scores, list):
        raise ValueError
    for score in scores:
        _validate_score(score)
    _validate_usage(case["usage"])
    trajectory = case["trajectory"]
    if trajectory is not None:
        _validate_trajectory(trajectory)


def _validate_score(value: object) -> None:
    score = _strict_object(
        value,
        required={
            "metric",
            "raw_score",
            "raw_score_code",
            "normalized_score",
            "outcome",
            "reason_code",
        },
    )
    for key in ("metric", "outcome", "reason_code"):
        _metadata_string(score[key])
    _nullable_metadata_string(score["raw_score_code"])
    raw_score = score["raw_score"]
    if raw_score is not None and not isinstance(
        raw_score,
        (str, int, float, bool),
    ):
        raise ValueError
    if isinstance(raw_score, str):
        _metadata_string(raw_score)
    _nullable_number(score["normalized_score"])


def _validate_trajectory(value: object) -> None:
    trajectory = _strict_object(
        value,
        required={"trajectory_id", "turn_count", "tool_calls"},
    )
    _metadata_string(trajectory["trajectory_id"])
    _nonnegative_integer(trajectory["turn_count"])
    tool_calls = trajectory["tool_calls"]
    if not isinstance(tool_calls, list):
        raise ValueError
    for value in tool_calls:
        tool_call = _strict_object(
            value,
            required={"call_id", "status_code", "duration_ms"},
        )
        _metadata_string(tool_call["call_id"])
        _metadata_string(tool_call["status_code"])
        _nonnegative_number(tool_call["duration_ms"])


def _validate_pareto(value: object) -> None:
    pareto = _strict_object(
        value,
        required={"frontier_ids", "eligible_ids", "decisions"},
    )
    for key in ("frontier_ids", "eligible_ids"):
        identifiers = pareto[key]
        if not isinstance(identifiers, list):
            raise ValueError
        for identifier in identifiers:
            _metadata_string(identifier)
    decisions = pareto["decisions"]
    if not isinstance(decisions, list):
        raise ValueError
    for value in decisions:
        decision = _strict_object(
            value,
            required={"subject_id", "eligible", "reason_code"},
        )
        _metadata_string(decision["subject_id"])
        _boolean(decision["eligible"])
        _metadata_string(decision["reason_code"])


def _validate_telemetry(value: object) -> None:
    telemetry = _strict_object(
        value,
        required={
            "response_id",
            "request_count",
            "dependency_count",
            "exception_count",
            "duration_ms",
            "success_rate",
        },
    )
    _metadata_string(telemetry["response_id"])
    for key in ("request_count", "dependency_count", "exception_count"):
        _nonnegative_integer(telemetry[key])
    _nonnegative_number(telemetry["duration_ms"])
    _nullable_number(telemetry["success_rate"])


def _metadata_string(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 2048
        or any(character.isspace() for character in value)
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError
    return value


def _nullable_metadata_string(value: object) -> None:
    if value is not None:
        _metadata_string(value)


def _nonnegative_integer(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError


def _nonnegative_number(value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or value < 0
    ):
        raise ValueError


def _nullable_number(value: object) -> None:
    if value is not None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
        ):
            raise ValueError


def _boolean(value: object) -> None:
    if not isinstance(value, bool):
        raise ValueError


def _sha256(value: object) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError


def _reject_sensitive_values(
    value: object,
    sensitive_values: tuple[str, ...],
) -> None:
    secrets = tuple(secret for secret in sensitive_values if secret)
    if isinstance(value, str):
        if any(secret in value for secret in secrets):
            raise _SensitiveArtifactError
    elif isinstance(value, dict):
        for key, item in value.items():
            _reject_sensitive_values(key, secrets)
            _reject_sensitive_values(item, secrets)
    elif isinstance(value, list):
        for item in value:
            _reject_sensitive_values(item, secrets)


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


_CANONICAL_LABELS = frozenset(
    {
        "needs-triage",
        "needs-info",
        "ready-for-agent",
        "ready-for-human",
        "wontfix",
    }
)


def _reconcile_issue(
    request: CampaignPublicationRequest,
    gateway: CampaignGateway,
    *,
    candidate_id: str,
    issue: IssueReference,
    expected_body: str,
    campaign_pr: PullRequestReference,
    campaign_body: str,
) -> tuple[
    IssueReference,
    PullRequestReference,
    tuple[WorkflowFailure, ...],
]:
    failures: list[WorkflowFailure] = []
    state = issue.state
    body = issue.body
    labels = set(issue.labels)
    if state != "OPEN":
        try:
            gateway.reopen_issue(
                request.repository_root,
                issue.number,
            )
            state = "OPEN"
        except RuntimeError:
            failures.append(
                _failure(
                    "reopen_issue",
                    candidate_id,
                    "reopen_failed",
                    "Existing candidate issue could not be reopened.",
                )
            )
    if body != expected_body:
        try:
            gateway.update_issue_body(
                request.repository_root,
                issue.number,
                expected_body,
            )
            body = expected_body
        except RuntimeError:
            failures.append(
                _failure(
                    "update_issue_body",
                    candidate_id,
                    "issue_body_failed",
                    "Existing candidate issue body could not be reconciled.",
                )
            )
    conflicting = tuple(
        sorted((labels & _CANONICAL_LABELS) - {"ready-for-agent"})
    )
    if conflicting:
        try:
            gateway.remove_labels(
                request.repository_root,
                issue.number,
                conflicting,
            )
            labels.difference_update(conflicting)
        except RuntimeError:
            failures.append(
                _failure(
                    "remove_labels",
                    candidate_id,
                    "label_remove_failed",
                    "Conflicting canonical labels could not be removed.",
                )
            )
    if "ready-for-agent" not in labels:
        try:
            gateway.add_labels(
                request.repository_root,
                issue.number,
                ("ready-for-agent",),
            )
            labels.add("ready-for-agent")
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
        linked = gateway.is_sub_issue(
            request.repository_root,
            campaign_pr.number,
            issue.number,
        )
        if not linked:
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
                base_branch=campaign_pr.base_branch,
                state=campaign_pr.state,
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
                    "Native sub-issues unavailable; task-list fallback "
                    "was used.",
                )
            )
    return (
        IssueReference(
            issue.number,
            issue.url,
            issue.title,
            body,
            state=state,
            labels=tuple(sorted(labels)),
        ),
        campaign_pr,
        tuple(failures),
    )


def _campaign_pr_matches(
    pull_request: PullRequestReference,
    request: CampaignPublicationRequest,
    state: RepositoryState,
) -> bool:
    marker = (
        f"<!-- foundry-opt:campaign:{request.report.campaign_id} -->"
    )
    return (
        pull_request.state == "OPEN"
        and pull_request.draft
        and pull_request.base_branch == state.default_branch
        and pull_request.head_branch == request.head_branch
        and pull_request.head_commit == request.head_commit
        and marker in pull_request.body
        and f"Exact base commit: `{request.report.base_commit}`"
        in pull_request.body
        and "automation must never merge this PR"
        in pull_request.body
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
    CommitInspection,
