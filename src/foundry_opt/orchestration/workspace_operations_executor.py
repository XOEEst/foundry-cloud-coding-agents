from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import json
from math import isfinite
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import urlsplit

from foundry_opt.deployment import DeploymentTrigger
from foundry_opt.evaluation import EvaluationPolicy
from foundry_opt.evidence import EvaluationAssetReference
from foundry_opt.orchestration.candidate_experiments import (
    CandidateExperimentOperation,
    CandidateExperimentRequest,
    CandidateExperimentResult,
    PersistedCandidateExperimentOperation,
)
from foundry_opt.orchestration.workspace import (
    WorkspaceNextActionKind,
    WorkspaceOperation,
    WorkspacePhase,
    WorkspaceResult,
    WorkspaceTrigger,
)
from foundry_opt.orchestration.workspace_production import (
    WorkspaceAdvanceRequest,
)
from foundry_opt.security import reject_secret_content


_ARTIFACT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_EVENT_NAME = frozenset(
    {"push", "schedule", "workflow_dispatch", "workflow_run"}
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,255}$")
_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
)
_SAFE_TEXT = re.compile(r"^[^\x00-\x1f]{1,512}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STATE_REF = re.compile(
    r"^foundry-opt/state/issue-[1-9][0-9]*$"
)


def _positive_integer(value: object, name: str) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} is invalid")


def _identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")


def _artifact_name(value: str, name: str) -> None:
    if not isinstance(value, str) or _ARTIFACT.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")


def _sha256(value: str, name: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a SHA-256 digest")


def _commit(value: str, name: str) -> None:
    if not isinstance(value, str) or _COMMIT.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")


def _repository(value: str, name: str) -> None:
    if not isinstance(value, str) or _REPOSITORY.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")


def _relative_paths(
    values: tuple[str, ...],
    name: str,
) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if (
            not isinstance(value, str)
            or not value
            or value.startswith(("/", "\\"))
            or ".." in Path(value).parts
        ):
            raise ValueError(f"{name} is invalid")
        if value in seen:
            raise ValueError(f"{name} must be unique")
        normalized.append(value)
        seen.add(value)
    return tuple(normalized)


def _command_arguments(
    values: tuple[tuple[str, ...], ...],
    name: str,
) -> tuple[tuple[str, ...], ...]:
    normalized: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for command in values:
        if type(command) is not tuple or not command:
            raise ValueError(f"{name} is invalid")
        parsed: list[str] = []
        for argument in command:
            if (
                not isinstance(argument, str)
                or not argument
                or len(argument) > 256
                or any(ord(character) < 32 for character in argument)
            ):
                raise ValueError(f"{name} is invalid")
            parsed.append(argument)
        normalized_command = tuple(parsed)
        if normalized_command in seen:
            raise ValueError(f"{name} must be unique")
        normalized.append(normalized_command)
        seen.add(normalized_command)
    return tuple(normalized)


def _validated_glob(value: str, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith(("/", "\\"))
        or ".." in re.split(r"[\\/]", value)
    ):
        raise ValueError(f"{name} is invalid")
    return value


def _frozen_metrics(values: Mapping[str, float]) -> Mapping[str, float]:
    copied: dict[str, float] = {}
    for metric, value in values.items():
        if not isinstance(metric, str) or not metric.strip():
            raise ValueError("metric name is invalid")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
        ):
            raise ValueError("metric value is invalid")
        copied[metric] = float(value)
    return MappingProxyType(copied)


def _url(
    value: str,
    name: str,
    *,
    allowed_hosts: tuple[str, ...] | None = None,
) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is invalid")
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise ValueError(f"{name} is invalid") from error
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError(f"{name} is invalid")
    if (
        allowed_hosts is not None
        and parsed.hostname not in allowed_hosts
    ):
        raise ValueError(f"{name} is invalid")


def _safe_text(value: str, name: str) -> None:
    if not isinstance(value, str) or _SAFE_TEXT.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")


def _workspace_pull_request_number(
    workspace: WorkspaceResult | None,
) -> int | None:
    if workspace is None:
        return None
    next_action = workspace.next_action
    if next_action is not None and (
        type(next_action.workspace_pull_request_number) is int
        and next_action.workspace_pull_request_number > 0
    ):
        return next_action.workspace_pull_request_number
    if (
        workspace.workspace_pull_request is not None
        and type(workspace.workspace_pull_request.number) is int
        and workspace.workspace_pull_request.number > 0
    ):
        return workspace.workspace_pull_request.number
    projection = workspace.issue_status_projection_intent
    if projection is not None and (
        type(projection.workspace_pull_request_number) is int
        and projection.workspace_pull_request_number > 0
    ):
        return projection.workspace_pull_request_number
    return None


def _workspace_resume_request(
    *,
    issue_number: int,
    operation_key: str,
    workspace: WorkspaceResult | None,
) -> WorkspaceResumeRequest | None:
    if workspace is None or workspace.next_action is None:
        return None
    if (
        workspace.next_action.kind
        is not WorkspaceNextActionKind.RUN_CANDIDATE_EXPERIMENTS
    ):
        return None
    pull_request_number = _workspace_pull_request_number(workspace)
    if pull_request_number is None:
        return None
    marker_digest = hashlib.sha256(
        json.dumps(
            {
                "issue_number": issue_number,
                "next_action": workspace.next_action.kind.value,
                "operation_key": operation_key,
                "pull_request_number": pull_request_number,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:24]
    marker = (
        "<!-- foundry-opt:workspace-resume:v1:"
        f"issue-{issue_number}:pr-{pull_request_number}:{marker_digest} -->"
    )
    return WorkspaceResumeRequest(
        workspace_pull_request_number=pull_request_number,
        comment_marker=marker,
        comment_body="\n".join(
            (
                marker,
                "@copilot continue this same workspace pull request from "
                f"the trusted workspace state for issue #{issue_number}.",
                "Run `foundry-opt workspace advance --issue "
                f"{issue_number} --json` and continue only if the returned "
                "next action still requires Copilot work.",
            )
        ),
    )


def _workspace_verification_request(
    *,
    issue_number: int,
    workspace: WorkspaceResult | None,
) -> WorkspaceVerificationRequest | None:
    if (
        workspace is None
        or workspace.report is None
        or workspace.next_action is None
        or workspace.next_action.kind
        is not WorkspaceNextActionKind.MERGE_WORKSPACE_PULL_REQUEST
    ):
        return None
    pull_request_number = _workspace_pull_request_number(workspace)
    if pull_request_number is None:
        return None
    return WorkspaceVerificationRequest(
        issue_number=issue_number,
        candidate_id=workspace.report.candidate_id,
        workspace_pull_request_number=pull_request_number,
    )


class WorkspaceOperationsStatus(StrEnum):
    NOOP = "noop"
    BASELINE_PENDING = "baseline_pending"
    BASELINE_RECORDED = "baseline_recorded"
    CANDIDATE_PENDING = "candidate_pending"
    CANDIDATE_RECORDED = "candidate_recorded"
    DEPLOYMENT_DISPATCHED = "deployment_dispatched"
    DEPLOYMENT_WAITING = "deployment_waiting"
    RETENTION_PENDING = "retention_pending"
    READY_FOR_HUMAN = "ready_for_human"
    COMPLETED = "completed"


class WorkspaceRetentionStatus(StrEnum):
    PENDING = "pending"
    REGRESSED = "regressed"
    RETAINED_IMPROVEMENT = "retained_improvement"


@dataclass(frozen=True)
class TrustedWorkspaceExecutionContext:
    event_name: str
    repository: str
    repository_id: int
    state_ref: str | None = None
    workflow_run_id: int | None = None

    def __post_init__(self) -> None:
        if self.event_name not in _EVENT_NAME:
            raise ValueError("workspace event name is invalid")
        _repository(self.repository, "workspace repository")
        _positive_integer(self.repository_id, "workspace repository ID")
        if self.state_ref is not None and (
            not isinstance(self.state_ref, str)
            or _STATE_REF.fullmatch(self.state_ref) is None
        ):
            raise ValueError("workspace state ref is invalid")
        if self.workflow_run_id is not None:
            _positive_integer(
                self.workflow_run_id,
                "workspace workflow run ID",
            )


@dataclass(frozen=True)
class TrustedWorkspaceArtifactContext:
    repository: str
    repository_id: int
    run_id: int
    artifact_name: str = "foundry-optimization-deployment-result"

    def __post_init__(self) -> None:
        _repository(self.repository, "workspace repository")
        _positive_integer(self.repository_id, "workspace repository ID")
        _positive_integer(self.run_id, "workspace workflow run ID")
        _artifact_name(self.artifact_name, "workspace artifact name")


@dataclass(frozen=True)
class WorkspaceOperationsExecuteRequest:
    repository_root: Path
    issue_number: int
    context: TrustedWorkspaceExecutionContext

    def __post_init__(self) -> None:
        _positive_integer(self.issue_number, "workspace issue number")


@dataclass(frozen=True)
class WorkspaceOperationsReconcileRequest:
    repository_root: Path
    issue_number: int
    payload: Mapping[str, Any]
    context: TrustedWorkspaceArtifactContext

    def __post_init__(self) -> None:
        _positive_integer(self.issue_number, "workspace issue number")
        if not isinstance(self.payload, Mapping):
            raise ValueError("workspace result payload must be an object")


@dataclass(frozen=True)
class WorkspaceDeploymentTarget:
    issue_number: int
    phase: WorkspacePhase
    repository: str
    repository_id: int
    workspace_pull_request_number: int
    candidate_id: str
    patch_sha256: str
    bundle_sha256: str
    evidence_sha256: str
    spec_sha256: str
    merge_commit: str
    tree_sha: str
    workflow_name: str
    workflow_path: Path
    workflow_ref: str
    workflow_trigger: DeploymentTrigger
    cleanup_refs: tuple[str, ...] = ()
    cleanup_drafts: tuple[str, ...] = ()
    cleanup_artifacts: tuple[str, ...] = ()
    artifact_name: str = "foundry-optimization-deployment-result"

    def __post_init__(self) -> None:
        _positive_integer(self.issue_number, "workspace issue number")
        if self.phase not in {
            WorkspacePhase.DEPLOYMENT,
            WorkspacePhase.RETENTION,
            WorkspacePhase.COMPLETED,
        }:
            raise ValueError("workspace deployment phase is invalid")
        _repository(self.repository, "workspace repository")
        _positive_integer(self.repository_id, "workspace repository ID")
        _positive_integer(
            self.workspace_pull_request_number,
            "workspace pull request number",
        )
        _identifier(self.candidate_id, "workspace candidate")
        for value, name in (
            (self.patch_sha256, "workspace patch digest"),
            (self.bundle_sha256, "workspace bundle digest"),
            (self.evidence_sha256, "workspace evidence digest"),
            (self.spec_sha256, "workspace spec digest"),
        ):
            _sha256(value, name)
        for value, name in (
            (self.merge_commit, "workspace merge commit"),
            (self.tree_sha, "workspace tree"),
        ):
            _commit(value, name)
        _safe_text(self.workflow_name, "deployment workflow name")
        if (
            self.workflow_path.is_absolute()
            or self.workflow_path.suffix.casefold() not in {".yml", ".yaml"}
            or not self.workflow_path.parts
        ):
            raise ValueError("deployment workflow path is invalid")
        if (
            not isinstance(self.workflow_ref, str)
            or re.fullmatch(
                r"refs/heads/(?!.*\.\.)"
                r"[A-Za-z0-9][A-Za-z0-9._/@+-]{0,199}",
                self.workflow_ref,
            )
            is None
        ):
            raise ValueError("deployment workflow ref is invalid")
        if not isinstance(self.workflow_trigger, DeploymentTrigger):
            raise ValueError("deployment workflow trigger is invalid")
        object.__setattr__(
            self,
            "cleanup_refs",
            _relative_paths(self.cleanup_refs, "cleanup refs"),
        )
        object.__setattr__(
            self,
            "cleanup_drafts",
            tuple(_validated_cleanup_ids(self.cleanup_drafts)),
        )
        object.__setattr__(
            self,
            "cleanup_artifacts",
            _relative_paths(
                self.cleanup_artifacts,
                "cleanup artifacts",
            ),
        )
        _artifact_name(self.artifact_name, "deployment artifact name")

    @property
    def lineage_sha256(self) -> str:
        payload = json.dumps(
            {
                "bundle_sha256": self.bundle_sha256,
                "candidate_id": self.candidate_id,
                "evidence_sha256": self.evidence_sha256,
                "issue_number": self.issue_number,
                "merge_commit": self.merge_commit,
                "patch_sha256": self.patch_sha256,
                "repository": self.repository,
                "repository_id": self.repository_id,
                "spec_sha256": self.spec_sha256,
                "tree_sha": self.tree_sha,
                "workspace_pull_request_number": (
                    self.workspace_pull_request_number
                ),
            },
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def _validated_cleanup_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        _identifier(value, "cleanup draft")
        if value in seen:
            raise ValueError("cleanup drafts must be unique")
        normalized.append(value)
        seen.add(value)
    return tuple(normalized)


class WorkspaceDeploymentDispatchStatus(StrEnum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    OBSERVED = "observed"


@dataclass(frozen=True)
class WorkspaceDeploymentDispatchResult:
    status: WorkspaceDeploymentDispatchStatus
    run_id: int | None = None
    run_url: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, WorkspaceDeploymentDispatchStatus):
            raise ValueError("workspace deployment dispatch status is invalid")
        if self.run_id is not None:
            _positive_integer(self.run_id, "workspace deployment run ID")
        if self.run_url is not None:
            _url(
                self.run_url,
                "workspace deployment run URL",
                allowed_hosts=("github.com",),
            )


@dataclass(frozen=True)
class WorkspaceDeploymentArtifact:
    issue_number: int
    operation: WorkspaceOperation
    repository: str
    repository_id: int
    spec_sha256: str
    merge_commit: str
    tree_sha: str
    run_id: int
    run_url: str
    artifact_name: str
    deployment_version: int
    portal_url: str
    lineage_sha256: str

    def __post_init__(self) -> None:
        _positive_integer(self.issue_number, "workspace issue number")
        if self.operation.trigger is not WorkspaceTrigger.DEPLOYMENT_COMPLETED:
            raise ValueError("workspace deployment artifact trigger is invalid")
        _repository(self.repository, "workspace repository")
        _positive_integer(self.repository_id, "workspace repository ID")
        _sha256(self.spec_sha256, "workspace spec digest")
        _commit(self.merge_commit, "workspace merge commit")
        _commit(self.tree_sha, "workspace tree")
        _positive_integer(self.run_id, "workspace workflow run ID")
        _url(
            self.run_url,
            "workspace deployment run URL",
            allowed_hosts=("github.com",),
        )
        _artifact_name(self.artifact_name, "workspace artifact name")
        _positive_integer(
            self.deployment_version,
            "workspace deployment version",
        )
        _url(
            self.portal_url,
            "workspace deployment portal URL",
            allowed_hosts=("ai.azure.com", "portal.azure.com"),
        )
        _sha256(self.lineage_sha256, "workspace deployment lineage")


@dataclass(frozen=True)
class WorkspaceRetentionOutcome:
    status: WorkspaceRetentionStatus
    operation_id: str | None = None
    baseline_metrics: Mapping[str, float] = MappingProxyType({})
    selected_metrics: Mapping[str, float] = MappingProxyType({})
    deployed_metrics: Mapping[str, float] = MappingProxyType({})
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, WorkspaceRetentionStatus):
            raise ValueError("workspace retention status is invalid")
        if self.status is WorkspaceRetentionStatus.PENDING:
            if self.operation_id is not None or self.reason is not None:
                raise ValueError("pending retention cannot carry a result")
        else:
            if self.operation_id is None:
                raise ValueError("retention operation ID is required")
            _identifier(self.operation_id, "retention operation ID")
        if self.status is WorkspaceRetentionStatus.REGRESSED:
            if self.reason is None:
                raise ValueError("retention regression reason is required")
            _identifier(self.reason, "retention regression reason")
        elif self.reason is not None:
            raise ValueError("retention reason is invalid")
        object.__setattr__(
            self,
            "baseline_metrics",
            _frozen_metrics(self.baseline_metrics),
        )
        object.__setattr__(
            self,
            "selected_metrics",
            _frozen_metrics(self.selected_metrics),
        )
        object.__setattr__(
            self,
            "deployed_metrics",
            _frozen_metrics(self.deployed_metrics),
        )


@dataclass(frozen=True)
class WorkspaceFinalIssueProjection:
    marker: str
    body: str

    def __post_init__(self) -> None:
        _safe_text(self.marker, "workspace final marker")
        if not isinstance(self.body, str) or not self.body.strip():
            raise ValueError("workspace final comment body is invalid")

    def to_dict(self) -> dict[str, str]:
        return {"body": self.body, "marker": self.marker}


@dataclass(frozen=True)
class WorkspaceFinalizationEffect:
    finalized: bool
    closed_issue: bool
    projection: WorkspaceFinalIssueProjection
    cleaned_refs: tuple[str, ...] = ()
    cleaned_drafts: tuple[str, ...] = ()
    cleaned_artifacts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.projection, WorkspaceFinalIssueProjection):
            raise ValueError("workspace final projection is invalid")
        if self.closed_issue and not self.finalized:
            raise ValueError("closed issues must be finalized")
        object.__setattr__(
            self,
            "cleaned_refs",
            _relative_paths(self.cleaned_refs, "cleaned refs"),
        )
        object.__setattr__(
            self,
            "cleaned_artifacts",
            _relative_paths(
                self.cleaned_artifacts,
                "cleaned artifacts",
            ),
        )
        object.__setattr__(
            self,
            "cleaned_drafts",
            tuple(_validated_cleanup_ids(self.cleaned_drafts)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "cleaned_artifacts": list(self.cleaned_artifacts),
            "cleaned_drafts": list(self.cleaned_drafts),
            "cleaned_refs": list(self.cleaned_refs),
            "closed_issue": self.closed_issue,
            "finalized": self.finalized,
            "projection": self.projection.to_dict(),
        }


@dataclass(frozen=True)
class WorkspaceResumeRequest:
    workspace_pull_request_number: int
    comment_marker: str
    comment_body: str

    def __post_init__(self) -> None:
        _positive_integer(
            self.workspace_pull_request_number,
            "workspace pull request number",
        )
        _safe_text(self.comment_marker, "workspace resume marker")
        if (
            not isinstance(self.comment_body, str)
            or not self.comment_body.strip()
            or self.comment_marker not in self.comment_body
            or "@copilot" not in self.comment_body
        ):
            raise ValueError("workspace resume comment is invalid")

    def to_dict(self) -> dict[str, str | int]:
        return {
            "comment_body": self.comment_body,
            "comment_marker": self.comment_marker,
            "workspace_pull_request_number": (
                self.workspace_pull_request_number
            ),
        }


@dataclass(frozen=True)
class WorkspaceVerificationRequest:
    issue_number: int
    candidate_id: str
    workspace_pull_request_number: int
    check_name: str = "exact-candidate"

    def __post_init__(self) -> None:
        _positive_integer(self.issue_number, "workspace issue number")
        _identifier(self.candidate_id, "workspace candidate")
        _positive_integer(
            self.workspace_pull_request_number,
            "workspace pull request number",
        )
        _safe_text(self.check_name, "workspace verification check name")

    def to_dict(self) -> dict[str, int | str]:
        return {
            "candidate_id": self.candidate_id,
            "check_name": self.check_name,
            "issue_number": self.issue_number,
            "workspace_pull_request_number": (
                self.workspace_pull_request_number
            ),
        }


@dataclass(frozen=True)
class WorkspaceCompletionRequest:
    repository_root: Path
    target: WorkspaceDeploymentTarget
    deployment_result: WorkspaceDeploymentArtifact
    retention: WorkspaceRetentionOutcome

    def __post_init__(self) -> None:
        if (
            self.retention.status
            is not WorkspaceRetentionStatus.RETAINED_IMPROVEMENT
        ):
            raise ValueError(
                "workspace completion requires a retained improvement"
            )
        _validate_completion_binding(
            self.target,
            self.deployment_result,
        )


@dataclass(frozen=True)
class WorkspaceReadyForHumanRequest:
    repository_root: Path
    target: WorkspaceDeploymentTarget
    deployment_result: WorkspaceDeploymentArtifact
    retention: WorkspaceRetentionOutcome

    def __post_init__(self) -> None:
        if self.retention.status is not WorkspaceRetentionStatus.REGRESSED:
            raise ValueError(
                "workspace regression requires a regression result"
            )
        _validate_completion_binding(
            self.target,
            self.deployment_result,
        )


def _validate_completion_binding(
    target: WorkspaceDeploymentTarget,
    result: WorkspaceDeploymentArtifact,
) -> None:
    if (
        target.issue_number != result.issue_number
        or target.repository != result.repository
        or target.repository_id != result.repository_id
        or target.spec_sha256 != result.spec_sha256
        or target.merge_commit != result.merge_commit
        or target.tree_sha != result.tree_sha
        or target.lineage_sha256 != result.lineage_sha256
        or target.candidate_id != result.operation.candidate_id
        or target.patch_sha256 != result.operation.patch_sha256
        or target.bundle_sha256 != result.operation.bundle_sha256
        or target.evidence_sha256 != result.operation.evidence_sha256
        or target.workspace_pull_request_number
        != result.operation.workspace_pull_request_number
    ):
        raise ValueError("workspace completion lineage changed")


@dataclass(frozen=True)
class WorkspaceOperationsResult:
    issue_number: int
    status: WorkspaceOperationsStatus
    recorded: bool
    phase: WorkspacePhase | None = None
    operation_id: str | None = None
    workspace_pull_request_number: int | None = None
    deployment_run_id: int | None = None
    deployment_run_url: str | None = None
    finalization: WorkspaceFinalizationEffect | None = None
    resume: WorkspaceResumeRequest | None = None
    verification: WorkspaceVerificationRequest | None = None

    def __post_init__(self) -> None:
        _positive_integer(self.issue_number, "workspace issue number")
        if not isinstance(self.status, WorkspaceOperationsStatus):
            raise ValueError("workspace operations status is invalid")
        if self.operation_id is not None:
            _identifier(self.operation_id, "workspace operation ID")
        if self.workspace_pull_request_number is not None:
            _positive_integer(
                self.workspace_pull_request_number,
                "workspace pull request number",
            )
        if self.deployment_run_id is not None:
            _positive_integer(
                self.deployment_run_id,
                "workspace deployment run ID",
            )
        if self.deployment_run_url is not None:
            _url(
                self.deployment_run_url,
                "workspace deployment run URL",
                allowed_hosts=("github.com",),
            )
        if self.finalization is not None and not isinstance(
            self.finalization,
            WorkspaceFinalizationEffect,
        ):
            raise ValueError("workspace finalization effect is invalid")
        if self.resume is not None and not isinstance(
            self.resume,
            WorkspaceResumeRequest,
        ):
            raise ValueError("workspace resume request is invalid")
        if self.verification is not None and not isinstance(
            self.verification,
            WorkspaceVerificationRequest,
        ):
            raise ValueError("workspace verification request is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "deployment_run_id": self.deployment_run_id,
            "deployment_run_url": self.deployment_run_url,
            "finalization": (
                self.finalization.to_dict()
                if self.finalization is not None
                else None
            ),
            "issue_number": self.issue_number,
            "operation_id": self.operation_id,
            "phase": self.phase.value if self.phase is not None else None,
            "recorded": self.recorded,
            "resume": (
                self.resume.to_dict()
                if self.resume is not None
                else None
            ),
            "status": self.status.value,
            "verification": (
                self.verification.to_dict()
                if self.verification is not None
                else None
            ),
            "workspace_pull_request_number": (
                self.workspace_pull_request_number
            ),
        }


def workspace_final_issue_marker(
    *,
    issue_number: int,
    candidate_id: str,
    disposition: str,
) -> str:
    _positive_integer(issue_number, "workspace issue number")
    _identifier(candidate_id, "workspace candidate")
    _identifier(disposition, "workspace disposition")
    digest = hashlib.sha256(
        json.dumps(
            {
                "candidate_id": candidate_id,
                "disposition": disposition,
                "issue_number": issue_number,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:24]
    return (
        "<!-- foundry-opt:workspace-final:v1:"
        f"issue-{issue_number}:{disposition}:{digest} -->"
    )


def render_workspace_completion_projection(
    request: WorkspaceCompletionRequest,
) -> WorkspaceFinalIssueProjection:
    target = request.target
    artifact = request.deployment_result
    retention = request.retention
    marker = workspace_final_issue_marker(
        issue_number=target.issue_number,
        candidate_id=target.candidate_id,
        disposition="retained_improvement",
    )
    body = "\n".join(
        (
            marker,
            "## Workspace finalization",
            "",
            "Retained improvement confirmed after trusted deployment and "
            "held-out evaluation.",
            "",
            f"- Workspace pull request: #{target.workspace_pull_request_number}",
            f"- Candidate: `{target.candidate_id}`",
            f"- Deployment version: `{artifact.deployment_version}`",
            f"- Deployment portal: {artifact.portal_url}",
            f"- Trusted workflow run: {artifact.run_url}",
            "",
            "## Exact lineage",
            "",
            f"- Spec SHA-256: `{target.spec_sha256}`",
            f"- Patch SHA-256: `{target.patch_sha256}`",
            f"- Bundle SHA-256: `{target.bundle_sha256}`",
            f"- Evidence SHA-256: `{target.evidence_sha256}`",
            f"- Merge commit: `{target.merge_commit}`",
            f"- Tree SHA: `{target.tree_sha}`",
            "",
            "## Held-out evaluation",
            "",
            _metric_lines("Baseline", retention.baseline_metrics),
            "",
            _metric_lines(
                "Selected draft",
                retention.selected_metrics,
            ),
            "",
            _metric_lines("Deployed", retention.deployed_metrics),
            "",
            "The workspace is complete and the issue can be closed.",
        )
    )
    return WorkspaceFinalIssueProjection(marker=marker, body=body)


def render_workspace_ready_for_human_projection(
    request: WorkspaceReadyForHumanRequest,
) -> WorkspaceFinalIssueProjection:
    target = request.target
    artifact = request.deployment_result
    retention = request.retention
    assert retention.reason is not None
    marker = workspace_final_issue_marker(
        issue_number=target.issue_number,
        candidate_id=target.candidate_id,
        disposition="ready_for_human",
    )
    body = "\n".join(
        (
            marker,
            "## Workspace requires human review",
            "",
            "Trusted post-deployment evaluation detected a regression. "
            "The issue remains open for human remediation.",
            "",
            f"- Workspace pull request: #{target.workspace_pull_request_number}",
            f"- Candidate: `{target.candidate_id}`",
            f"- Deployment version: `{artifact.deployment_version}`",
            f"- Regression reason: `{retention.reason}`",
            f"- Trusted workflow run: {artifact.run_url}",
            "",
            "## Exact lineage",
            "",
            f"- Spec SHA-256: `{target.spec_sha256}`",
            f"- Patch SHA-256: `{target.patch_sha256}`",
            f"- Bundle SHA-256: `{target.bundle_sha256}`",
            f"- Evidence SHA-256: `{target.evidence_sha256}`",
            f"- Merge commit: `{target.merge_commit}`",
            f"- Tree SHA: `{target.tree_sha}`",
            "",
            "## Held-out evaluation",
            "",
            _metric_lines("Baseline", retention.baseline_metrics),
            "",
            _metric_lines(
                "Selected draft",
                retention.selected_metrics,
            ),
            "",
            _metric_lines("Deployed", retention.deployed_metrics),
        )
    )
    return WorkspaceFinalIssueProjection(marker=marker, body=body)


def _metric_lines(title: str, values: Mapping[str, float]) -> str:
    if not values:
        return f"### {title}\n\nNo metrics recorded."
    rows = [f"### {title}", "", "Metric | Value", "--- | ---"]
    for metric, value in sorted(values.items()):
        rows.append(f"{metric} | {value:.6g}")
    return "\n".join(rows)


def normalize_workspace_deployment_artifact(
    payload: Mapping[str, Any],
    *,
    context: TrustedWorkspaceArtifactContext,
    target: WorkspaceDeploymentTarget,
) -> WorkspaceDeploymentArtifact:
    reject_secret_content(payload)
    expected = {
        "artifact_name",
        "bundle_sha256",
        "candidate_id",
        "deployment_version",
        "evidence_sha256",
        "issue_number",
        "kind",
        "lineage_sha256",
        "merge_commit",
        "operation_id",
        "patch_sha256",
        "portal_url",
        "repository",
        "run_id",
        "run_url",
        "schema_version",
        "spec_sha256",
        "status",
        "tree_sha",
        "workspace_pull_request_number",
    }
    if set(payload) != expected:
        raise ValueError("workspace deployment result fields are invalid")
    repository = payload["repository"]
    if (
        not isinstance(repository, Mapping)
        or set(repository) != {"full_name", "id"}
        or repository["full_name"] != context.repository
        or repository["id"] != context.repository_id
        or repository["full_name"] != target.repository
        or repository["id"] != target.repository_id
    ):
        raise ValueError("workspace deployment repository changed")
    if (
        payload["schema_version"] != 2
        or payload["kind"] != "deployment_result"
        or payload["status"] != "completed"
    ):
        raise ValueError("workspace deployment result is invalid")
    issue_number = payload["issue_number"]
    if type(issue_number) is not int or issue_number < 1:
        raise ValueError("workspace deployment issue changed")
    if issue_number != target.issue_number:
        raise ValueError("workspace deployment issue changed")
    workspace_pull_request_number = payload["workspace_pull_request_number"]
    if (
        type(workspace_pull_request_number) is not int
        or workspace_pull_request_number
        != target.workspace_pull_request_number
    ):
        raise ValueError(
            "workspace deployment pull request lineage changed"
        )
    run_id = payload["run_id"]
    if type(run_id) is not int or run_id < 1:
        raise ValueError("workspace deployment run changed")
    deployment_version = payload["deployment_version"]
    if type(deployment_version) is not int or deployment_version < 1:
        raise ValueError("workspace deployment version changed")
    for field_name, expected_value in (
        ("candidate_id", target.candidate_id),
        ("patch_sha256", target.patch_sha256),
        ("bundle_sha256", target.bundle_sha256),
        ("evidence_sha256", target.evidence_sha256),
        ("spec_sha256", target.spec_sha256),
        ("merge_commit", target.merge_commit),
        ("tree_sha", target.tree_sha),
        ("artifact_name", target.artifact_name),
    ):
        if payload[field_name] != expected_value:
            raise ValueError(
                "workspace deployment lineage changed"
            )
    if payload["artifact_name"] != context.artifact_name:
        raise ValueError("workspace deployment artifact changed")
    if payload["run_id"] != context.run_id:
        raise ValueError("workspace deployment run changed")
    if payload["lineage_sha256"] != target.lineage_sha256:
        raise ValueError("workspace deployment lineage changed")
    operation = WorkspaceOperation(
        trigger=WorkspaceTrigger.DEPLOYMENT_COMPLETED,
        operation_id=str(payload["operation_id"]),
        workspace_pull_request_number=workspace_pull_request_number,
        candidate_id=str(payload["candidate_id"]),
        patch_sha256=str(payload["patch_sha256"]),
        bundle_sha256=str(payload["bundle_sha256"]),
        evidence_sha256=str(payload["evidence_sha256"]),
    )
    return WorkspaceDeploymentArtifact(
        issue_number=int(issue_number),
        operation=operation,
        repository=context.repository,
        repository_id=context.repository_id,
        spec_sha256=str(payload["spec_sha256"]),
        merge_commit=str(payload["merge_commit"]),
        tree_sha=str(payload["tree_sha"]),
        run_id=run_id,
        run_url=str(payload["run_url"]),
        artifact_name=str(payload["artifact_name"]),
        deployment_version=deployment_version,
        portal_url=str(payload["portal_url"]),
        lineage_sha256=str(payload["lineage_sha256"]),
    )


@dataclass(frozen=True)
class WorkspaceBaselineRequest:
    issue_number: int
    target_name: str
    published_base_version: str
    development_suite: str
    idempotency_key: str

    def __post_init__(self) -> None:
        _positive_integer(self.issue_number, "workspace issue number")
        _identifier(self.target_name, "workspace baseline target")
        _identifier(
            self.published_base_version,
            "workspace baseline published base version",
        )
        _identifier(
            self.development_suite,
            "workspace baseline development suite",
        )
        _sha256(
            self.idempotency_key,
            "workspace baseline idempotency key",
        )


@dataclass(frozen=True)
class WorkspaceBaselineOperation:
    issue_number: int
    target_name: str
    published_base_version: str
    development_suite: str
    idempotency_key: str
    schema_version: int = field(default=1, init=False)
    kind: str = field(default="workspace_baseline", init=False)

    def __post_init__(self) -> None:
        WorkspaceBaselineRequest(
            issue_number=self.issue_number,
            target_name=self.target_name,
            published_base_version=self.published_base_version,
            development_suite=self.development_suite,
            idempotency_key=self.idempotency_key,
        )

    @classmethod
    def from_request(
        cls,
        request: WorkspaceBaselineRequest,
    ) -> WorkspaceBaselineOperation:
        if not isinstance(request, WorkspaceBaselineRequest):
            raise ValueError("workspace baseline request is invalid")
        return cls(
            issue_number=request.issue_number,
            target_name=request.target_name,
            published_base_version=request.published_base_version,
            development_suite=request.development_suite,
            idempotency_key=request.idempotency_key,
        )

    @property
    def sha256(self) -> str:
        payload = json.dumps(
            {
                "development_suite": self.development_suite,
                "idempotency_key": self.idempotency_key,
                "issue_number": self.issue_number,
                "kind": self.kind,
                "published_base_version": self.published_base_version,
                "schema_version": self.schema_version,
                "target_name": self.target_name,
            },
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class PersistedWorkspaceBaselineOperation:
    operation: WorkspaceBaselineOperation
    reference: str
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.operation, WorkspaceBaselineOperation):
            raise ValueError("persisted workspace baseline is invalid")
        if (
            not isinstance(self.reference, str)
            or _REFERENCE.fullmatch(self.reference) is None
            or "://" in self.reference
        ):
            raise ValueError("persisted workspace baseline reference is invalid")
        _sha256(self.sha256, "persisted workspace baseline sha256")
        if self.sha256 != self.operation.sha256:
            raise ValueError("persisted workspace baseline changed")


@dataclass(frozen=True)
class WorkspaceBaselineResult:
    target_name: str
    executor: str
    metrics: Mapping[str, float]
    evaluation_id: str
    run_id: str
    base_commit: str
    published_base_version: str
    development_suite: str
    operation_sha256: str | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.target_name, "workspace baseline target")
        _identifier(self.executor, "workspace baseline executor")
        object.__setattr__(self, "metrics", _frozen_metrics(self.metrics))
        _safe_text(self.evaluation_id, "workspace baseline evaluation ID")
        _safe_text(self.run_id, "workspace baseline run ID")
        _commit(self.base_commit, "workspace baseline base commit")
        _identifier(
            self.published_base_version,
            "workspace baseline published base version",
        )
        _identifier(
            self.development_suite,
            "workspace baseline development suite",
        )
        if (self.operation_sha256 is None) != (
            self.idempotency_key is None
        ):
            raise ValueError("workspace baseline lineage is incomplete")
        if self.operation_sha256 is not None:
            _sha256(
                self.operation_sha256,
                "workspace baseline operation sha256",
            )
            _sha256(
                self.idempotency_key,
                "workspace baseline idempotency key",
            )


@dataclass(frozen=True)
class StoredWorkspaceBaselineResult:
    result: WorkspaceBaselineResult
    workspace: WorkspaceResult | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.result, WorkspaceBaselineResult):
            raise ValueError("workspace baseline result is invalid")
        if self.workspace is not None and not isinstance(
            self.workspace,
            WorkspaceResult,
        ):
            raise ValueError("workspace baseline workspace result is invalid")


@dataclass(frozen=True)
class PendingWorkspaceBaselineExecution:
    operation: PersistedWorkspaceBaselineOperation
    request_payload: Mapping[str, Any] = MappingProxyType({})

    def __post_init__(self) -> None:
        if not isinstance(
            self.operation,
            PersistedWorkspaceBaselineOperation,
        ):
            raise ValueError("pending workspace baseline is invalid")
        if not isinstance(self.request_payload, Mapping):
            raise ValueError("pending workspace baseline payload is invalid")
        object.__setattr__(
            self,
            "request_payload",
            MappingProxyType(dict(self.request_payload)),
        )


@dataclass(frozen=True)
class TrustedWorkspaceBaselinePlan:
    operation: PersistedWorkspaceBaselineOperation
    request: WorkspaceBaselineRequest
    base_commit: str
    target_name: str
    base_agent_version: int
    published_base_version: str
    development_suite: str
    assets: tuple[EvaluationAssetReference, ...]
    evaluation_policy: EvaluationPolicy

    def __post_init__(self) -> None:
        if not isinstance(
            self.operation,
            PersistedWorkspaceBaselineOperation,
        ):
            raise ValueError("workspace baseline operation is invalid")
        if not isinstance(self.request, WorkspaceBaselineRequest):
            raise ValueError("workspace baseline request is invalid")
        expected = WorkspaceBaselineOperation.from_request(self.request)
        if self.operation.operation != expected or self.operation.sha256 != (
            expected.sha256
        ):
            raise ValueError("workspace baseline lineage is invalid")
        _commit(self.base_commit, "workspace baseline base commit")
        _identifier(self.target_name, "workspace baseline target")
        _positive_integer(
            self.base_agent_version,
            "workspace baseline base agent version",
        )
        _identifier(
            self.published_base_version,
            "workspace baseline published base version",
        )
        _identifier(
            self.development_suite,
            "workspace baseline development suite",
        )
        if (
            self.request.target_name != self.target_name
            or self.request.published_base_version
            != self.published_base_version
            or self.request.development_suite != self.development_suite
        ):
            raise ValueError("workspace baseline lineage is invalid")
        assets = tuple(self.assets)
        if not assets or any(
            not isinstance(asset, EvaluationAssetReference)
            for asset in assets
        ):
            raise ValueError("workspace baseline assets are invalid")
        object.__setattr__(self, "assets", assets)
        if not isinstance(self.evaluation_policy, EvaluationPolicy):
            raise ValueError("workspace baseline evaluation policy is invalid")


@dataclass(frozen=True)
class StoredCandidateExperimentResult:
    result: CandidateExperimentResult
    workspace: WorkspaceResult | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.result, CandidateExperimentResult):
            raise ValueError("candidate experiment result is invalid")
        if self.workspace is not None and not isinstance(
            self.workspace,
            WorkspaceResult,
        ):
            raise ValueError("candidate workspace result is invalid")


@dataclass(frozen=True)
class PendingCandidateExperimentExecution:
    operation: PersistedCandidateExperimentOperation
    request_payload: Mapping[str, Any] = MappingProxyType({})

    def __post_init__(self) -> None:
        if not isinstance(
            self.operation,
            PersistedCandidateExperimentOperation,
        ):
            raise ValueError("pending candidate experiment is invalid")
        if not isinstance(self.request_payload, Mapping):
            raise ValueError(
                "pending candidate experiment payload is invalid"
            )
        object.__setattr__(
            self,
            "request_payload",
            MappingProxyType(dict(self.request_payload)),
        )


@dataclass(frozen=True)
class TrustedCandidatePackagingContract:
    include: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    dependency_resolution: str = "remote_build"
    evidence_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.include:
            raise ValueError("candidate packaging include is invalid")
        object.__setattr__(
            self,
            "include",
            tuple(
                _validated_glob(item, "candidate packaging include")
                for item in self.include
            ),
        )
        object.__setattr__(
            self,
            "exclude",
            tuple(
                _validated_glob(item, "candidate packaging exclude")
                for item in self.exclude
            ),
        )
        object.__setattr__(
            self,
            "evidence_paths",
            _relative_paths(
                self.evidence_paths,
                "candidate packaging evidence paths",
            ),
        )
        if self.dependency_resolution not in {"remote_build", "bundled"}:
            raise ValueError(
                "candidate packaging dependency resolution is invalid"
            )


@dataclass(frozen=True)
class TrustedCandidateExecutionPlan:
    operation: PersistedCandidateExperimentOperation
    request: CandidateExperimentRequest
    base_commit: str
    target_name: str
    base_agent_version: int
    allowed_paths: tuple[str, ...]
    allowed_mutations: frozenset[str]
    validation_commands: tuple[tuple[str, ...], ...]
    packaging: TrustedCandidatePackagingContract
    assets: tuple[EvaluationAssetReference, ...]
    evaluation_policy: EvaluationPolicy
    candidate_limit: int

    def __post_init__(self) -> None:
        if not isinstance(
            self.operation,
            PersistedCandidateExperimentOperation,
        ):
            raise ValueError("candidate execution operation is invalid")
        if not isinstance(self.request, CandidateExperimentRequest):
            raise ValueError("candidate execution request is invalid")
        expected = CandidateExperimentOperation.from_request(self.request)
        if self.operation.operation != expected or self.operation.sha256 != (
            expected.sha256
        ):
            raise ValueError("candidate execution lineage is invalid")
        _commit(self.base_commit, "candidate execution base commit")
        _identifier(self.target_name, "candidate execution target")
        _positive_integer(
            self.base_agent_version,
            "candidate execution base agent version",
        )
        if not self.allowed_paths:
            raise ValueError("candidate execution allowed paths are invalid")
        object.__setattr__(
            self,
            "allowed_paths",
            _relative_paths(
                self.allowed_paths,
                "candidate execution allowed paths",
            ),
        )
        if not self.allowed_mutations:
            raise ValueError(
                "candidate execution allowed mutations are invalid"
            )
        normalized_mutations: set[str] = set()
        for mutation in self.allowed_mutations:
            _identifier(mutation, "candidate execution allowed mutation")
            normalized_mutations.add(mutation)
        object.__setattr__(
            self,
            "allowed_mutations",
            frozenset(sorted(normalized_mutations)),
        )
        if not self.validation_commands:
            raise ValueError(
                "candidate execution validation commands are invalid"
            )
        object.__setattr__(
            self,
            "validation_commands",
            _command_arguments(
                self.validation_commands,
                "candidate execution validation commands",
            ),
        )
        if not isinstance(self.packaging, TrustedCandidatePackagingContract):
            raise ValueError("candidate execution packaging is invalid")
        assets = tuple(self.assets)
        if not assets or any(
            not isinstance(asset, EvaluationAssetReference)
            for asset in assets
        ):
            raise ValueError("candidate execution assets are invalid")
        object.__setattr__(self, "assets", assets)
        if not isinstance(self.evaluation_policy, EvaluationPolicy):
            raise ValueError(
                "candidate execution evaluation policy is invalid"
            )
        if (
            type(self.candidate_limit) is not int
            or not 1 <= self.candidate_limit <= 32
        ):
            raise ValueError("candidate execution candidate limit is invalid")


class PendingWorkspaceBaselineStore(Protocol):
    def load_pending(
        self,
        issue_number: int,
    ) -> PendingWorkspaceBaselineExecution | None: ...

    def load_result(
        self,
        operation: PersistedWorkspaceBaselineOperation,
    ) -> StoredWorkspaceBaselineResult | None: ...

    def persist_result(
        self,
        operation: PersistedWorkspaceBaselineOperation,
        result: WorkspaceBaselineResult,
    ) -> StoredWorkspaceBaselineResult: ...


class WorkspaceBaselineExecutionPlanner(Protocol):
    def resolve(
        self,
        repository_root: Path,
        pending: PendingWorkspaceBaselineExecution,
    ) -> TrustedWorkspaceBaselinePlan: ...


class WorkspaceBaselineOperationExecutor(Protocol):
    def reconcile(
        self,
        plan: TrustedWorkspaceBaselinePlan,
    ) -> WorkspaceBaselineResult | None: ...

    def execute(
        self,
        plan: TrustedWorkspaceBaselinePlan,
    ) -> WorkspaceBaselineResult | None: ...


@dataclass(frozen=True)
class WorkspaceBaselineCompletionRequest:
    repository_root: Path
    issue_number: int
    plan: TrustedWorkspaceBaselinePlan
    result: WorkspaceBaselineResult

    def __post_init__(self) -> None:
        _positive_integer(self.issue_number, "workspace issue number")
        if not isinstance(self.plan, TrustedWorkspaceBaselinePlan):
            raise ValueError("workspace baseline plan is invalid")
        if not isinstance(self.result, WorkspaceBaselineResult):
            raise ValueError("workspace baseline result is invalid")
        if self.plan.request.issue_number != self.issue_number:
            raise ValueError("workspace baseline issue changed")
        _validate_baseline_result(
            self.plan.operation,
            self.plan,
            self.result,
        )


class WorkspaceBaselineCompletionService(Protocol):
    def complete(
        self,
        request: WorkspaceBaselineCompletionRequest,
    ) -> WorkspaceResult | None: ...


class PendingCandidateExperimentStore(Protocol):
    def load_pending(
        self,
        issue_number: int,
    ) -> PendingCandidateExperimentExecution | None: ...

    def load_result(
        self,
        operation: PersistedCandidateExperimentOperation,
    ) -> StoredCandidateExperimentResult | None: ...

    def persist_result(
        self,
        operation: PersistedCandidateExperimentOperation,
        result: CandidateExperimentResult,
    ) -> StoredCandidateExperimentResult: ...


class CandidateExperimentExecutionPlanner(Protocol):
    def resolve(
        self,
        repository_root: Path,
        pending: PendingCandidateExperimentExecution,
    ) -> TrustedCandidateExecutionPlan: ...


class CandidateExperimentOperationExecutor(Protocol):
    def reconcile(
        self,
        plan: TrustedCandidateExecutionPlan,
    ) -> CandidateExperimentResult | None: ...

    def execute(
        self,
        plan: TrustedCandidateExecutionPlan,
    ) -> CandidateExperimentResult | None: ...


@dataclass(frozen=True)
class WorkspaceCandidateSelectionRequest:
    repository_root: Path
    issue_number: int
    plan: TrustedCandidateExecutionPlan
    result: CandidateExperimentResult

    def __post_init__(self) -> None:
        _positive_integer(self.issue_number, "workspace issue number")
        if not isinstance(self.plan, TrustedCandidateExecutionPlan):
            raise ValueError(
                "workspace candidate selection plan is invalid"
            )
        if not isinstance(self.result, CandidateExperimentResult):
            raise ValueError(
                "workspace candidate selection result is invalid"
            )
        if self.plan.request.issue_number != self.issue_number:
            raise ValueError(
                "workspace candidate selection issue changed"
            )
        _validate_candidate_result(self.plan.operation, self.result)


class WorkspaceCandidateSelectionService(Protocol):
    def complete(
        self,
        request: WorkspaceCandidateSelectionRequest,
    ) -> WorkspaceResult | None: ...


class WorkspaceDeploymentStateLoader(Protocol):
    def load(
        self,
        issue_number: int,
    ) -> WorkspaceDeploymentTarget | None: ...


class WorkspaceDeploymentWorkflowExecutor(Protocol):
    def execute(
        self,
        repository_root: Path,
        target: WorkspaceDeploymentTarget,
        context: TrustedWorkspaceExecutionContext,
    ) -> WorkspaceDeploymentDispatchResult: ...


class WorkspaceDeploymentRunVerifier(Protocol):
    def verify(
        self,
        repository_root: Path,
        target: WorkspaceDeploymentTarget,
        artifact: WorkspaceDeploymentArtifact,
    ) -> None: ...


class WorkspaceLifecycleService(Protocol):
    def advance(
        self,
        request: WorkspaceAdvanceRequest,
    ) -> WorkspaceResult: ...


class WorkspaceRetentionEvaluator(Protocol):
    def evaluate(
        self,
        repository_root: Path,
        target: WorkspaceDeploymentTarget,
        artifact: WorkspaceDeploymentArtifact,
    ) -> WorkspaceRetentionOutcome: ...


class WorkspaceCompletionFinalizer(Protocol):
    def complete(
        self,
        request: WorkspaceCompletionRequest,
    ) -> WorkspaceFinalizationEffect: ...

    def ready_for_human(
        self,
        request: WorkspaceReadyForHumanRequest,
    ) -> WorkspaceFinalizationEffect: ...


class EmptyPendingWorkspaceBaselineStore:
    def load_pending(
        self,
        issue_number: int,
    ) -> PendingWorkspaceBaselineExecution | None:
        return None

    def load_result(
        self,
        operation: PersistedWorkspaceBaselineOperation,
    ) -> StoredWorkspaceBaselineResult | None:
        return None

    def persist_result(
        self,
        operation: PersistedWorkspaceBaselineOperation,
        result: WorkspaceBaselineResult,
    ) -> StoredWorkspaceBaselineResult:
        return StoredWorkspaceBaselineResult(result=result)


class UnavailableWorkspaceBaselineExecutionPlanner:
    def resolve(
        self,
        repository_root: Path,
        pending: PendingWorkspaceBaselineExecution,
    ) -> TrustedWorkspaceBaselinePlan:
        raise RuntimeError("trusted workspace baseline planner is not configured")


class NoopWorkspaceBaselineOperationExecutor:
    def reconcile(
        self,
        plan: TrustedWorkspaceBaselinePlan,
    ) -> WorkspaceBaselineResult | None:
        return None

    def execute(
        self,
        plan: TrustedWorkspaceBaselinePlan,
    ) -> WorkspaceBaselineResult | None:
        return None


class NoopWorkspaceBaselineCompletionService:
    def complete(
        self,
        request: WorkspaceBaselineCompletionRequest,
    ) -> WorkspaceResult | None:
        return None


class EmptyPendingCandidateExperimentStore:
    def load_pending(
        self,
        issue_number: int,
    ) -> PendingCandidateExperimentExecution | None:
        return None

    def load_result(
        self,
        operation: PersistedCandidateExperimentOperation,
    ) -> StoredCandidateExperimentResult | None:
        return None

    def persist_result(
        self,
        operation: PersistedCandidateExperimentOperation,
        result: CandidateExperimentResult,
    ) -> StoredCandidateExperimentResult:
        return StoredCandidateExperimentResult(result=result)


class UnavailableCandidateExperimentExecutionPlanner:
    def resolve(
        self,
        repository_root: Path,
        pending: PendingCandidateExperimentExecution,
    ) -> TrustedCandidateExecutionPlan:
        raise RuntimeError(
            "trusted candidate execution planner is not configured"
        )


class NoopCandidateExperimentOperationExecutor:
    def reconcile(
        self,
        plan: TrustedCandidateExecutionPlan,
    ) -> CandidateExperimentResult | None:
        return None

    def execute(
        self,
        plan: TrustedCandidateExecutionPlan,
    ) -> CandidateExperimentResult | None:
        return None


class NoopWorkspaceCandidateSelectionService:
    def complete(
        self,
        request: WorkspaceCandidateSelectionRequest,
    ) -> WorkspaceResult | None:
        return None


class EmptyWorkspaceDeploymentStateLoader:
    def load(
        self,
        issue_number: int,
    ) -> WorkspaceDeploymentTarget | None:
        return None


class PlanningWorkspaceDeploymentWorkflowExecutor:
    def execute(
        self,
        repository_root: Path,
        target: WorkspaceDeploymentTarget,
        context: TrustedWorkspaceExecutionContext,
    ) -> WorkspaceDeploymentDispatchResult:
        return WorkspaceDeploymentDispatchResult(
            status=WorkspaceDeploymentDispatchStatus.PENDING
        )


class NoopWorkspaceDeploymentRunVerifier:
    def verify(
        self,
        repository_root: Path,
        target: WorkspaceDeploymentTarget,
        artifact: WorkspaceDeploymentArtifact,
    ) -> None:
        return None


class UnavailableWorkspaceRetentionEvaluator:
    def evaluate(
        self,
        repository_root: Path,
        target: WorkspaceDeploymentTarget,
        artifact: WorkspaceDeploymentArtifact,
    ) -> WorkspaceRetentionOutcome:
        raise RuntimeError(
            "workspace retention evaluator is not configured"
        )


class PlanningWorkspaceCompletionFinalizer:
    def complete(
        self,
        request: WorkspaceCompletionRequest,
    ) -> WorkspaceFinalizationEffect:
        return WorkspaceFinalizationEffect(
            finalized=False,
            closed_issue=False,
            projection=render_workspace_completion_projection(request),
        )

    def ready_for_human(
        self,
        request: WorkspaceReadyForHumanRequest,
    ) -> WorkspaceFinalizationEffect:
        return WorkspaceFinalizationEffect(
            finalized=False,
            closed_issue=False,
            projection=render_workspace_ready_for_human_projection(
                request
            ),
        )


class WorkspaceOperationsService:
    def __init__(
        self,
        *,
        baseline_store: PendingWorkspaceBaselineStore | None = None,
        baseline_planner: WorkspaceBaselineExecutionPlanner | None = None,
        baseline_executor: (
            WorkspaceBaselineOperationExecutor | None
        ) = None,
        baseline_completion: (
            WorkspaceBaselineCompletionService | None
        ) = None,
        candidate_store: PendingCandidateExperimentStore | None = None,
        candidate_planner: (
            CandidateExperimentExecutionPlanner | None
        ) = None,
        candidate_executor: CandidateExperimentOperationExecutor | None = None,
        candidate_selection: (
            WorkspaceCandidateSelectionService | None
        ) = None,
        deployment_loader: WorkspaceDeploymentStateLoader | None = None,
        deployment_executor: (
            WorkspaceDeploymentWorkflowExecutor | None
        ) = None,
        deployment_verifier: WorkspaceDeploymentRunVerifier | None = None,
        workspace_service: WorkspaceLifecycleService | None = None,
        retention_evaluator: WorkspaceRetentionEvaluator | None = None,
        finalizer: WorkspaceCompletionFinalizer | None = None,
    ) -> None:
        self._baseline_store = (
            baseline_store
            if baseline_store is not None
            else EmptyPendingWorkspaceBaselineStore()
        )
        self._baseline_planner = (
            baseline_planner
            if baseline_planner is not None
            else UnavailableWorkspaceBaselineExecutionPlanner()
        )
        self._baseline_executor = (
            baseline_executor
            if baseline_executor is not None
            else NoopWorkspaceBaselineOperationExecutor()
        )
        self._baseline_completion = (
            baseline_completion
            if baseline_completion is not None
            else NoopWorkspaceBaselineCompletionService()
        )
        self._candidate_store = (
            candidate_store
            if candidate_store is not None
            else EmptyPendingCandidateExperimentStore()
        )
        self._candidate_planner = (
            candidate_planner
            if candidate_planner is not None
            else UnavailableCandidateExperimentExecutionPlanner()
        )
        self._candidate_executor = (
            candidate_executor
            if candidate_executor is not None
            else NoopCandidateExperimentOperationExecutor()
        )
        self._candidate_selection = (
            candidate_selection
            if candidate_selection is not None
            else NoopWorkspaceCandidateSelectionService()
        )
        self._deployment_loader = (
            deployment_loader
            if deployment_loader is not None
            else EmptyWorkspaceDeploymentStateLoader()
        )
        self._deployment_executor = (
            deployment_executor
            if deployment_executor is not None
            else PlanningWorkspaceDeploymentWorkflowExecutor()
        )
        self._deployment_verifier = (
            deployment_verifier
            if deployment_verifier is not None
            else NoopWorkspaceDeploymentRunVerifier()
        )
        self._workspace_service = workspace_service
        self._retention_evaluator = (
            retention_evaluator
            if retention_evaluator is not None
            else UnavailableWorkspaceRetentionEvaluator()
        )
        self._finalizer = (
            finalizer
            if finalizer is not None
            else PlanningWorkspaceCompletionFinalizer()
        )

    def execute(
        self,
        request: WorkspaceOperationsExecuteRequest,
    ) -> WorkspaceOperationsResult:
        pending_baseline = self._baseline_store.load_pending(
            request.issue_number
        )
        if pending_baseline is not None:
            plan = self._baseline_planner.resolve(
                request.repository_root,
                pending_baseline,
            )
            return self._execute_baseline(request, plan)
        pending = self._candidate_store.load_pending(request.issue_number)
        if pending is not None:
            plan = self._candidate_planner.resolve(
                request.repository_root,
                pending,
            )
            return self._execute_candidate(request, plan)
        target = self._deployment_loader.load(request.issue_number)
        if target is None or target.phase is not WorkspacePhase.DEPLOYMENT:
            return WorkspaceOperationsResult(
                issue_number=request.issue_number,
                status=WorkspaceOperationsStatus.NOOP,
                recorded=False,
                phase=target.phase if target is not None else None,
                workspace_pull_request_number=(
                    target.workspace_pull_request_number
                    if target is not None
                    else None
                ),
            )
        if target.repository != request.context.repository:
            raise ValueError("workspace deployment repository changed")
        if target.repository_id != request.context.repository_id:
            raise ValueError("workspace deployment repository changed")
        dispatch = self._deployment_executor.execute(
            request.repository_root,
            target,
            request.context,
        )
        return WorkspaceOperationsResult(
            issue_number=request.issue_number,
            status=(
                WorkspaceOperationsStatus.DEPLOYMENT_DISPATCHED
                if dispatch.status
                is WorkspaceDeploymentDispatchStatus.DISPATCHED
                else WorkspaceOperationsStatus.DEPLOYMENT_WAITING
            ),
            recorded=dispatch.status
            is WorkspaceDeploymentDispatchStatus.DISPATCHED,
            phase=target.phase,
            workspace_pull_request_number=(
                target.workspace_pull_request_number
            ),
            deployment_run_id=dispatch.run_id,
            deployment_run_url=dispatch.run_url,
        )

    def reconcile(
        self,
        request: WorkspaceOperationsReconcileRequest,
    ) -> WorkspaceOperationsResult:
        target = self._deployment_loader.load(request.issue_number)
        if target is None:
            raise ValueError("workspace deployment state is unavailable")
        if self._workspace_service is None:
            raise RuntimeError("workspace lifecycle service is not configured")
        artifact = normalize_workspace_deployment_artifact(
            request.payload,
            context=request.context,
            target=target,
        )
        self._deployment_verifier.verify(
            request.repository_root,
            target,
            artifact,
        )
        completed_before_reconcile = (
            target.phase is WorkspacePhase.COMPLETED
        )
        if completed_before_reconcile:
            deployment_phase = WorkspacePhase.COMPLETED
            deployment_recorded = False
        else:
            deployment = self._workspace_service.advance(
                WorkspaceAdvanceRequest(
                    repository_root=request.repository_root,
                    issue_number=request.issue_number,
                    trigger=WorkspaceTrigger.DEPLOYMENT_COMPLETED,
                    expected_repository=request.context.repository,
                    trusted_repository_id=request.context.repository_id,
                    operation=artifact.operation,
                )
            )
            deployment_phase = deployment.phase
            deployment_recorded = deployment.recorded
        if deployment_phase not in {
            WorkspacePhase.RETENTION,
            WorkspacePhase.COMPLETED,
        }:
            raise RuntimeError(
                "workspace deployment transition did not reach retention"
            )
        retention = self._retention_evaluator.evaluate(
            request.repository_root,
            target,
            artifact,
        )
        if retention.status is WorkspaceRetentionStatus.PENDING:
            return WorkspaceOperationsResult(
                issue_number=request.issue_number,
                status=WorkspaceOperationsStatus.RETENTION_PENDING,
                recorded=deployment_recorded,
                phase=deployment_phase,
                operation_id=artifact.operation.operation_id,
                workspace_pull_request_number=(
                    target.workspace_pull_request_number
                ),
            )
        if retention.status is WorkspaceRetentionStatus.REGRESSED:
            effect = self._finalizer.ready_for_human(
                WorkspaceReadyForHumanRequest(
                    repository_root=request.repository_root,
                    target=target,
                    deployment_result=artifact,
                    retention=retention,
                )
            )
            return WorkspaceOperationsResult(
                issue_number=request.issue_number,
                status=WorkspaceOperationsStatus.READY_FOR_HUMAN,
                recorded=True,
                phase=deployment_phase,
                operation_id=retention.operation_id,
                workspace_pull_request_number=(
                    target.workspace_pull_request_number
                ),
                finalization=effect,
            )
        assert retention.operation_id is not None
        completed_recorded = False
        if not completed_before_reconcile:
            completed = self._workspace_service.advance(
                WorkspaceAdvanceRequest(
                    repository_root=request.repository_root,
                    issue_number=request.issue_number,
                    trigger=WorkspaceTrigger.RETENTION_COMPLETED,
                    expected_repository=request.context.repository,
                    trusted_repository_id=request.context.repository_id,
                    operation=WorkspaceOperation(
                        trigger=WorkspaceTrigger.RETENTION_COMPLETED,
                        operation_id=retention.operation_id,
                        workspace_pull_request_number=(
                            target.workspace_pull_request_number
                        ),
                        candidate_id=target.candidate_id,
                        patch_sha256=target.patch_sha256,
                        bundle_sha256=target.bundle_sha256,
                        evidence_sha256=target.evidence_sha256,
                        predecessor_operation_id=(
                            artifact.operation.operation_id
                        ),
                    ),
                )
            )
            completed_recorded = completed.recorded
        effect = self._finalizer.complete(
            WorkspaceCompletionRequest(
                repository_root=request.repository_root,
                target=target,
                deployment_result=artifact,
                retention=retention,
            )
        )
        return WorkspaceOperationsResult(
            issue_number=request.issue_number,
            status=WorkspaceOperationsStatus.COMPLETED,
            recorded=completed_recorded,
            phase=WorkspacePhase.COMPLETED,
            operation_id=retention.operation_id,
            workspace_pull_request_number=(
                target.workspace_pull_request_number
            ),
            finalization=effect,
        )

    def _execute_candidate(
        self,
        request: WorkspaceOperationsExecuteRequest,
        plan: TrustedCandidateExecutionPlan,
    ) -> WorkspaceOperationsResult:
        operation = plan.operation
        if plan.request.issue_number != request.issue_number:
            raise ValueError("candidate experiment issue changed")
        stored = self._candidate_store.load_result(operation)
        if stored is None:
            result = self._candidate_executor.reconcile(plan)
        else:
            result = stored.result
        if result is None:
            result = self._candidate_executor.execute(plan)
            if result is None:
                return WorkspaceOperationsResult(
                    issue_number=request.issue_number,
                    status=WorkspaceOperationsStatus.CANDIDATE_PENDING,
                    recorded=False,
                    operation_id=operation.sha256,
                )
        _validate_candidate_result(operation, result)
        existing = self._candidate_store.load_result(operation)
        if existing is not None:
            _validate_candidate_result(operation, existing.result)
            workspace, workspace_recorded = (
                self._complete_candidate_selection(
                    request=request,
                    plan=plan,
                    stored=existing,
                )
            )
            return WorkspaceOperationsResult(
                issue_number=request.issue_number,
                status=WorkspaceOperationsStatus.CANDIDATE_RECORDED,
                recorded=workspace_recorded,
                operation_id=operation.sha256,
                phase=workspace.phase if workspace is not None else None,
                workspace_pull_request_number=(
                    _workspace_pull_request_number(workspace)
                ),
                resume=_workspace_resume_request(
                    issue_number=request.issue_number,
                    operation_key=operation.sha256,
                    workspace=workspace,
                ),
                verification=_workspace_verification_request(
                    issue_number=request.issue_number,
                    workspace=workspace,
                ),
            )
        persisted = self._candidate_store.persist_result(operation, result)
        workspace, _workspace_recorded = self._complete_candidate_selection(
            request=request,
            plan=plan,
            stored=persisted,
        )
        return WorkspaceOperationsResult(
            issue_number=request.issue_number,
            status=WorkspaceOperationsStatus.CANDIDATE_RECORDED,
            recorded=True,
            operation_id=operation.sha256,
            phase=workspace.phase if workspace is not None else None,
            workspace_pull_request_number=(
                _workspace_pull_request_number(workspace)
            ),
            resume=_workspace_resume_request(
                issue_number=request.issue_number,
                operation_key=operation.sha256,
                workspace=workspace,
            ),
            verification=_workspace_verification_request(
                issue_number=request.issue_number,
                workspace=workspace,
            ),
        )

    def _execute_baseline(
        self,
        request: WorkspaceOperationsExecuteRequest,
        plan: TrustedWorkspaceBaselinePlan,
    ) -> WorkspaceOperationsResult:
        operation = plan.operation
        if plan.request.issue_number != request.issue_number:
            raise ValueError("workspace baseline issue changed")
        stored = self._baseline_store.load_result(operation)
        if stored is None:
            result = self._baseline_executor.reconcile(plan)
        else:
            result = stored.result
        if result is None:
            result = self._baseline_executor.execute(plan)
            if result is None:
                return WorkspaceOperationsResult(
                    issue_number=request.issue_number,
                    status=WorkspaceOperationsStatus.BASELINE_PENDING,
                    recorded=False,
                    operation_id=operation.sha256,
                )
        _validate_baseline_result(operation, plan, result)
        existing = self._baseline_store.load_result(operation)
        if existing is not None:
            _validate_baseline_result(operation, plan, existing.result)
            workspace, workspace_recorded = self._complete_baseline(
                request=request,
                plan=plan,
                stored=existing,
            )
            return WorkspaceOperationsResult(
                issue_number=request.issue_number,
                status=WorkspaceOperationsStatus.BASELINE_RECORDED,
                recorded=workspace_recorded,
                operation_id=operation.sha256,
                phase=workspace.phase if workspace is not None else None,
                workspace_pull_request_number=(
                    _workspace_pull_request_number(workspace)
                ),
                resume=_workspace_resume_request(
                    issue_number=request.issue_number,
                    operation_key=operation.sha256,
                    workspace=workspace,
                ),
            )
        persisted = self._baseline_store.persist_result(operation, result)
        workspace, _workspace_recorded = self._complete_baseline(
            request=request,
            plan=plan,
            stored=persisted,
        )
        return WorkspaceOperationsResult(
            issue_number=request.issue_number,
            status=WorkspaceOperationsStatus.BASELINE_RECORDED,
            recorded=True,
            operation_id=operation.sha256,
            phase=workspace.phase if workspace is not None else None,
            workspace_pull_request_number=(
                _workspace_pull_request_number(workspace)
            ),
            resume=_workspace_resume_request(
                issue_number=request.issue_number,
                operation_key=operation.sha256,
                workspace=workspace,
            ),
        )

    def _complete_candidate_selection(
        self,
        *,
        request: WorkspaceOperationsExecuteRequest,
        plan: TrustedCandidateExecutionPlan,
        stored: StoredCandidateExperimentResult,
    ) -> tuple[WorkspaceResult | None, bool]:
        workspace = stored.workspace
        completed = self._candidate_selection.complete(
            WorkspaceCandidateSelectionRequest(
                repository_root=request.repository_root,
                issue_number=request.issue_number,
                plan=plan,
                result=stored.result,
            )
        )
        if completed is None:
            return workspace, False
        return completed, completed.recorded

    def _complete_baseline(
        self,
        *,
        request: WorkspaceOperationsExecuteRequest,
        plan: TrustedWorkspaceBaselinePlan,
        stored: StoredWorkspaceBaselineResult,
    ) -> tuple[WorkspaceResult | None, bool]:
        workspace = stored.workspace
        completed = self._baseline_completion.complete(
            WorkspaceBaselineCompletionRequest(
                repository_root=request.repository_root,
                issue_number=request.issue_number,
                plan=plan,
                result=stored.result,
            )
        )
        if completed is None:
            return workspace, False
        return completed, completed.recorded


def _validate_candidate_result(
    operation: PersistedCandidateExperimentOperation,
    result: CandidateExperimentResult,
) -> None:
    if (
        result.candidate_id != operation.operation.candidate_id
        or result.bundle_sha256 != operation.operation.bundle_sha256
        or result.evidence_sha256 != operation.operation.evidence_sha256
        or result.operation_sha256 != operation.sha256
        or result.idempotency_key != operation.operation.idempotency_key
    ):
        raise ValueError("candidate experiment result lineage changed")


def _validate_baseline_result(
    operation: PersistedWorkspaceBaselineOperation,
    plan: TrustedWorkspaceBaselinePlan,
    result: WorkspaceBaselineResult,
) -> None:
    if (
        result.target_name != operation.operation.target_name
        or result.target_name != plan.target_name
        or result.base_commit != plan.base_commit
        or result.published_base_version
        != operation.operation.published_base_version
        or result.published_base_version != plan.published_base_version
        or result.development_suite != operation.operation.development_suite
        or result.development_suite != plan.development_suite
        or result.operation_sha256 != operation.sha256
        or result.idempotency_key != operation.operation.idempotency_key
    ):
        raise ValueError("workspace baseline result lineage changed")


def build_production_workspace_operations_service() -> (
    WorkspaceOperationsService
):
    from foundry_opt.orchestration.workspace_production import (
        build_production_workspace_service,
    )
    from foundry_opt.orchestration.workspace_operations_production import (
        build_production_workspace_operations_bindings,
    )

    workspace_service = build_production_workspace_service(
        actions_execution=True,
    )
    return WorkspaceOperationsService(
        **build_production_workspace_operations_bindings(
            Path.cwd(),
            workspace_service=workspace_service,
        )
    )


__all__ = [
    "CandidateExperimentExecutionPlanner",
    "CandidateExperimentOperationExecutor",
    "EmptyPendingWorkspaceBaselineStore",
    "EmptyPendingCandidateExperimentStore",
    "EmptyWorkspaceDeploymentStateLoader",
    "NoopWorkspaceBaselineOperationExecutor",
    "NoopWorkspaceDeploymentRunVerifier",
    "PendingWorkspaceBaselineExecution",
    "PendingCandidateExperimentExecution",
    "PlanningWorkspaceCompletionFinalizer",
    "PlanningWorkspaceDeploymentWorkflowExecutor",
    "PendingWorkspaceBaselineStore",
    "PendingCandidateExperimentStore",
    "PersistedWorkspaceBaselineOperation",
    "StoredCandidateExperimentResult",
    "StoredWorkspaceBaselineResult",
    "TrustedWorkspaceBaselinePlan",
    "TrustedCandidateExecutionPlan",
    "TrustedCandidatePackagingContract",
    "WorkspaceBaselineCompletionRequest",
    "WorkspaceBaselineCompletionService",
    "WorkspaceBaselineExecutionPlanner",
    "WorkspaceBaselineOperation",
    "WorkspaceBaselineOperationExecutor",
    "WorkspaceBaselineRequest",
    "WorkspaceBaselineResult",
    "WorkspaceCandidateSelectionRequest",
    "WorkspaceCandidateSelectionService",
    "TrustedWorkspaceArtifactContext",
    "TrustedWorkspaceExecutionContext",
    "UnavailableWorkspaceBaselineExecutionPlanner",
    "UnavailableCandidateExperimentExecutionPlanner",
    "UnavailableWorkspaceRetentionEvaluator",
    "WorkspaceCompletionFinalizer",
    "WorkspaceCompletionRequest",
    "WorkspaceDeploymentArtifact",
    "WorkspaceDeploymentDispatchResult",
    "WorkspaceDeploymentDispatchStatus",
    "WorkspaceDeploymentRunVerifier",
    "WorkspaceDeploymentStateLoader",
    "WorkspaceDeploymentTarget",
    "WorkspaceDeploymentWorkflowExecutor",
    "WorkspaceFinalIssueProjection",
    "WorkspaceFinalizationEffect",
    "WorkspaceLifecycleService",
    "WorkspaceOperationsExecuteRequest",
    "WorkspaceOperationsReconcileRequest",
    "WorkspaceOperationsResult",
    "WorkspaceOperationsService",
    "WorkspaceOperationsStatus",
    "WorkspaceReadyForHumanRequest",
    "WorkspaceResumeRequest",
    "WorkspaceVerificationRequest",
    "WorkspaceRetentionEvaluator",
    "WorkspaceRetentionOutcome",
    "WorkspaceRetentionStatus",
    "build_production_workspace_operations_service",
    "normalize_workspace_deployment_artifact",
    "render_workspace_completion_projection",
    "render_workspace_ready_for_human_projection",
    "workspace_final_issue_marker",
]
