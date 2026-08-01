from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import hashlib
import json
from math import isfinite
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from types import MappingProxyType
from typing import Callable, Mapping, Protocol
from urllib.parse import urlsplit

from foundry_opt.deployment import (
    DEPLOYMENT_OIDC_CLIENT_ID,
    DeploymentTrigger,
)
from foundry_opt.orchestration.candidate_slate import (
    CandidateBinding,
    CandidatePullRequestReader,
    CandidatePullRequestState,
    CandidatePullRequestVerificationStatus,
    CandidateSelectionRequest,
    candidate_worker_bindings,
    verify_candidate_pull_request,
)
from foundry_opt.orchestration.git_state import (
    OutboxRecord,
    StateRefConflictError,
    StateRefError,
    StateRefSnapshot,
)
from foundry_opt.orchestration.models import (
    CampaignEvent,
    CampaignPhase,
    CampaignState,
    EventKind,
)


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
)
_GITHUB_LOGIN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}|"
    r"[A-Za-z0-9-]{0,34}\[bot\])$"
)
_WORKFLOW_EVENT_FIELDS = frozenset(
    {
        "attempt",
        "binding_sha256",
        "bundle_sha256",
        "candidate_id",
        "candidate_issue_number",
        "candidate_pull_request_number",
        "deployment_client_id",
        "draft_id",
        "effect_id",
        "evidence_sha256",
        "issue_number",
        "merge_actor",
        "merge_commit",
        "patch_sha256",
        "repository",
        "repository_id",
        "required_checks",
        "result_id",
        "run_conclusion",
        "run_actor",
        "run_id",
        "run_status",
        "run_url",
        "spec_sha256",
        "tree_sha",
        "workflow_actor",
        "workflow_id",
        "workflow_path",
        "workflow_ref",
        "workflow_trigger",
    }
)


def _identifier(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field_name} must be an identifier")


def _sha256(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{field_name} must be a SHA-256 digest")


def _commit(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _COMMIT.fullmatch(value):
        raise ValueError(f"{field_name} must be a full Git commit")


def _positive(value: object, field_name: str) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{field_name} must be positive")


def _repository_path(value: Path, field_name: str) -> Path:
    raw = str(value)
    windows = PureWindowsPath(raw)
    posix = PurePosixPath(raw.replace("\\", "/"))
    if (
        not raw
        or windows.drive
        or raw.startswith(("/", "\\"))
        or ".." in posix.parts
    ):
        raise ValueError(f"{field_name} must be repository-relative")
    return Path(posix.as_posix())


@dataclass(frozen=True)
class DeploymentWorkflowIdentity:
    repository: str
    repository_id: int
    path: Path
    ref: str
    trigger: DeploymentTrigger
    workflow_id: int
    actor: str
    deployment_client_id: str = DEPLOYMENT_OIDC_CLIENT_ID

    def __post_init__(self) -> None:
        if not _REPOSITORY.fullmatch(self.repository):
            raise ValueError("repository is invalid")
        _positive(self.repository_id, "repository_id")
        object.__setattr__(
            self,
            "path",
            _repository_path(self.path, "workflow path"),
        )
        if self.path.suffix.casefold() not in {".yml", ".yaml"}:
            raise ValueError("workflow path must be YAML")
        if (
            not isinstance(self.ref, str)
            or re.fullmatch(
                r"refs/heads/(?!.*\.\.)"
                r"[A-Za-z0-9][A-Za-z0-9._/@+-]{0,199}",
                self.ref,
            )
            is None
        ):
            raise ValueError("workflow ref is invalid")
        if not isinstance(self.trigger, DeploymentTrigger):
            raise ValueError("workflow trigger is invalid")
        _positive(self.workflow_id, "workflow_id")
        if not _GITHUB_LOGIN.fullmatch(self.actor):
            raise ValueError("workflow actor is invalid")
        if self.deployment_client_id != DEPLOYMENT_OIDC_CLIENT_ID:
            raise ValueError(
                "deployment workflow must use the deployment OIDC identity"
            )


@dataclass(frozen=True)
class DeploymentPlan:
    issue_number: int
    generation: int
    repository: str
    repository_id: int
    workflow: DeploymentWorkflowIdentity
    allowed_merge_actors: tuple[str, ...]
    required_checks: tuple[str, ...]
    max_attempts: int = 1
    timeout_seconds: int = 1800
    held_out_evaluation_id: str = "held-out"
    evaluation_policy_sha256: str = "0" * 64
    campaign_pull_request_number: int | None = None
    optimization_pull_request_number: int | None = None

    def __post_init__(self) -> None:
        _positive(self.issue_number, "issue_number")
        _positive(self.generation, "generation")
        if not _REPOSITORY.fullmatch(self.repository):
            raise ValueError("repository is invalid")
        _positive(self.repository_id, "repository_id")
        if (
            self.workflow.repository != self.repository
            or self.workflow.repository_id != self.repository_id
        ):
            raise ValueError("workflow repository does not match plan")
        actors = tuple(self.allowed_merge_actors)
        if not actors or len(set(actors)) != len(actors):
            raise ValueError(
                "allowed_merge_actors must be non-empty and unique"
            )
        if any(not _GITHUB_LOGIN.fullmatch(actor) for actor in actors):
            raise ValueError("allowed merge actor is invalid")
        object.__setattr__(self, "allowed_merge_actors", actors)
        checks = tuple(self.required_checks)
        if not checks or len(set(checks)) != len(checks):
            raise ValueError("required_checks must be non-empty and unique")
        for check in checks:
            _identifier(check, "required check")
        object.__setattr__(self, "required_checks", checks)
        _positive(self.max_attempts, "max_attempts")
        _positive(self.timeout_seconds, "timeout_seconds")
        _identifier(
            self.held_out_evaluation_id,
            "held_out_evaluation_id",
        )
        _sha256(
            self.evaluation_policy_sha256,
            "evaluation_policy_sha256",
        )
        for value, field_name in (
            (
                self.campaign_pull_request_number,
                "campaign_pull_request_number",
            ),
            (
                self.optimization_pull_request_number,
                "optimization_pull_request_number",
            ),
        ):
            if value is not None:
                _positive(value, field_name)


@dataclass(frozen=True)
class DeploymentSelectionSnapshot:
    binding: CandidateBinding
    candidate_pull_request_number: int
    candidate_issue_number: int
    head_commit: str
    merge_commit: str
    merge_tree_sha: str
    merge_actor: str
    checks: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.binding, CandidateBinding):
            raise ValueError("binding must be a CandidateBinding")
        _positive(
            self.candidate_pull_request_number,
            "candidate_pull_request_number",
        )
        _positive(self.candidate_issue_number, "candidate_issue_number")
        _commit(self.head_commit, "head_commit")
        _commit(self.merge_commit, "merge_commit")
        _commit(self.merge_tree_sha, "merge_tree_sha")
        if not _GITHUB_LOGIN.fullmatch(self.merge_actor):
            raise ValueError("merge_actor is invalid")
        checks = dict(self.checks)
        allowed = {
            "success",
            "failure",
            "pending",
            "cancelled",
            "skipped",
        }
        if any(
            not isinstance(name, str)
            or not name
            or value not in allowed
            for name, value in checks.items()
        ):
            raise ValueError("checks are invalid")
        object.__setattr__(self, "checks", MappingProxyType(checks))


@dataclass(frozen=True)
class DeploymentBinding:
    issue_number: int
    generation: int
    spec_sha256: str
    candidate_pull_request_number: int
    candidate_issue_number: int
    candidate_id: str
    draft_id: str
    merge_actor: str
    required_checks: tuple[str, ...]
    merge_commit: str
    tree_sha: str
    patch_sha256: str
    bundle_sha256: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        _positive(self.issue_number, "issue_number")
        _positive(self.generation, "generation")
        _positive(
            self.candidate_pull_request_number,
            "candidate_pull_request_number",
        )
        _positive(self.candidate_issue_number, "candidate_issue_number")
        _identifier(self.candidate_id, "candidate_id")
        _identifier(self.draft_id, "draft_id")
        if not _GITHUB_LOGIN.fullmatch(self.merge_actor):
            raise ValueError("merge_actor is invalid")
        checks = tuple(self.required_checks)
        if not checks or len(set(checks)) != len(checks):
            raise ValueError("required_checks must be non-empty and unique")
        for check in checks:
            _identifier(check, "required check")
        object.__setattr__(self, "required_checks", checks)
        for value, field_name in (
            (self.spec_sha256, "spec_sha256"),
            (self.patch_sha256, "patch_sha256"),
            (self.bundle_sha256, "bundle_sha256"),
            (self.evidence_sha256, "evidence_sha256"),
        ):
            _sha256(value, field_name)
        _commit(self.merge_commit, "merge_commit")
        _commit(self.tree_sha, "tree_sha")

    @property
    def binding_sha256(self) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "bundle_sha256": self.bundle_sha256,
                    "candidate_id": self.candidate_id,
                    "candidate_issue_number": self.candidate_issue_number,
                    "candidate_pull_request_number": (
                        self.candidate_pull_request_number
                    ),
                    "draft_id": self.draft_id,
                    "evidence_sha256": self.evidence_sha256,
                    "generation": self.generation,
                    "issue_number": self.issue_number,
                    "merge_actor": self.merge_actor,
                    "merge_commit": self.merge_commit,
                    "patch_sha256": self.patch_sha256,
                    "spec_sha256": self.spec_sha256,
                    "tree_sha": self.tree_sha,
                    "required_checks": list(self.required_checks),
                },
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class DeploymentWorkflowIntent:
    effect_id: str
    attempt: int
    binding: DeploymentBinding
    workflow: DeploymentWorkflowIdentity
    planned_at: datetime
    timeout_seconds: int

    def __post_init__(self) -> None:
        _identifier(self.effect_id, "effect_id")
        _positive(self.attempt, "attempt")
        if not isinstance(self.binding, DeploymentBinding):
            raise ValueError("binding must be a DeploymentBinding")
        if not isinstance(self.workflow, DeploymentWorkflowIdentity):
            raise ValueError("workflow must be a DeploymentWorkflowIdentity")
        if self.planned_at.tzinfo is None:
            raise ValueError("planned_at must be timezone-aware")
        _positive(self.timeout_seconds, "timeout_seconds")

    @property
    def lineage_sha256(self) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "binding_sha256": self.binding.binding_sha256,
                    "deployment_client_id": (
                        self.workflow.deployment_client_id
                    ),
                    "repository": self.workflow.repository,
                    "repository_id": self.workflow.repository_id,
                    "workflow_actor": self.workflow.actor,
                    "workflow_id": self.workflow.workflow_id,
                    "workflow_path": self.workflow.path.as_posix(),
                    "workflow_ref": self.workflow.ref,
                    "workflow_trigger": self.workflow.trigger.value,
                },
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()


class DeploymentWorkflowRunState(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True)
class DeploymentWorkflowResult:
    effect_id: str
    result_id: str
    attempt: int
    binding: DeploymentBinding
    workflow: DeploymentWorkflowIdentity
    run_id: int
    run_url: str
    state: DeploymentWorkflowRunState
    conclusion: str | None
    run_actor: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.effect_id, "effect_id")
        _identifier(self.result_id, "result_id")
        _positive(self.attempt, "attempt")
        if not isinstance(self.binding, DeploymentBinding):
            raise ValueError("binding must be a DeploymentBinding")
        if not isinstance(self.workflow, DeploymentWorkflowIdentity):
            raise ValueError("workflow must be a DeploymentWorkflowIdentity")
        _positive(self.run_id, "run_id")
        try:
            parsed = urlsplit(self.run_url)
            parts = tuple(part for part in parsed.path.split("/") if part)
        except ValueError as error:
            raise ValueError("run_url is invalid") from error
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
            or len(parts) != 5
            or "/".join(parts[:2]) != self.workflow.repository
            or parts[2:4] != ("actions", "runs")
            or parts[4] != str(self.run_id)
        ):
            raise ValueError("run_url is invalid")
        if not isinstance(self.state, DeploymentWorkflowRunState):
            raise ValueError("state is invalid")
        run_actor = self.run_actor or self.workflow.actor
        if not _GITHUB_LOGIN.fullmatch(run_actor):
            raise ValueError("run_actor is invalid")
        object.__setattr__(self, "run_actor", run_actor)
        if self.conclusion is not None:
            _identifier(self.conclusion, "conclusion")
        terminal = self.state in {
            DeploymentWorkflowRunState.SUCCESS,
            DeploymentWorkflowRunState.FAILURE,
            DeploymentWorkflowRunState.CANCELLED,
            DeploymentWorkflowRunState.TIMED_OUT,
        }
        if terminal != (self.conclusion is not None):
            raise ValueError(
                "terminal workflow results require a conclusion"
            )

    def require_matches(self, intent: DeploymentWorkflowIntent) -> None:
        if (
            self.effect_id != intent.effect_id
            or self.attempt != intent.attempt
            or self.binding != intent.binding
            or self.workflow != intent.workflow
        ):
            raise ValueError("deployment result does not match intent")


@dataclass(frozen=True)
class TrustedDeploymentWorkflowContext:
    event_name: str
    action: str
    delivery_id: str
    repository: str
    repository_id: int
    workflow_id: int
    workflow_path: Path
    actor: str
    deployment_client_id: str

    def __post_init__(self) -> None:
        if self.event_name != "workflow_run":
            raise ValueError("event_name must be workflow_run")
        if self.action not in {"requested", "in_progress", "completed"}:
            raise ValueError("workflow action is invalid")
        _identifier(self.delivery_id, "delivery_id")
        if not _REPOSITORY.fullmatch(self.repository):
            raise ValueError("repository is invalid")
        _positive(self.repository_id, "repository_id")
        _positive(self.workflow_id, "workflow_id")
        object.__setattr__(
            self,
            "workflow_path",
            _repository_path(self.workflow_path, "workflow path"),
        )
        if not _GITHUB_LOGIN.fullmatch(self.actor):
            raise ValueError("workflow actor is invalid")
        if self.deployment_client_id != DEPLOYMENT_OIDC_CLIENT_ID:
            raise ValueError(
                "workflow event must use the deployment OIDC identity"
            )


@dataclass(frozen=True)
class DeploymentWorkflowEvent:
    context: TrustedDeploymentWorkflowContext
    result: DeploymentWorkflowResult
    occurred_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(
            self.context, TrustedDeploymentWorkflowContext
        ):
            raise ValueError("context must be trusted workflow context")
        if not isinstance(self.result, DeploymentWorkflowResult):
            raise ValueError("result must be a deployment workflow result")
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        workflow = self.result.workflow
        terminal = self.result.state in {
            DeploymentWorkflowRunState.SUCCESS,
            DeploymentWorkflowRunState.FAILURE,
            DeploymentWorkflowRunState.CANCELLED,
            DeploymentWorkflowRunState.TIMED_OUT,
        }
        if (
            self.context.repository != workflow.repository
            or self.context.repository_id != workflow.repository_id
            or self.context.workflow_id != workflow.workflow_id
            or self.context.workflow_path != workflow.path
            or self.context.actor != workflow.actor
            or self.context.deployment_client_id
            != workflow.deployment_client_id
            or (self.context.action == "completed") != terminal
        ):
            raise ValueError(
                "trusted workflow context does not match deployment result"
            )

    def to_campaign_event(self) -> CampaignEvent:
        binding = self.result.binding
        workflow = self.result.workflow
        return CampaignEvent(
            event_id=(
                "deployment-workflow-"
                + hashlib.sha256(
                    self.context.delivery_id.encode("utf-8")
                ).hexdigest()[:24]
            ),
            kind=EventKind.DEPLOYMENT_WORKFLOW_OBSERVED,
            generation=binding.generation,
            occurred_at=self.occurred_at,
            payload={
                "attempt": self.result.attempt,
                "binding_sha256": binding.binding_sha256,
                "bundle_sha256": binding.bundle_sha256,
                "candidate_id": binding.candidate_id,
                "candidate_issue_number": binding.candidate_issue_number,
                "candidate_pull_request_number": (
                    binding.candidate_pull_request_number
                ),
                "deployment_client_id": workflow.deployment_client_id,
                "draft_id": binding.draft_id,
                "effect_id": self.result.effect_id,
                "evidence_sha256": binding.evidence_sha256,
                "issue_number": binding.issue_number,
                "merge_actor": binding.merge_actor,
                "merge_commit": binding.merge_commit,
                "patch_sha256": binding.patch_sha256,
                "repository": workflow.repository,
                "repository_id": workflow.repository_id,
                "result_id": self.result.result_id,
                "required_checks": list(binding.required_checks),
                "run_actor": self.result.run_actor,
                "run_conclusion": (
                    self.result.conclusion or "pending"
                ),
                "run_id": self.result.run_id,
                "run_status": self.result.state.value,
                "run_url": self.result.run_url,
                "spec_sha256": binding.spec_sha256,
                "tree_sha": binding.tree_sha,
                "workflow_actor": workflow.actor,
                "workflow_id": workflow.workflow_id,
                "workflow_path": workflow.path.as_posix(),
                "workflow_ref": workflow.ref,
                "workflow_trigger": workflow.trigger.value,
            },
        )


def deployment_workflow_event_from_payload(
    context: TrustedDeploymentWorkflowContext,
    payload: Mapping[str, object],
    intent: DeploymentWorkflowIntent,
    occurred_at: datetime,
) -> DeploymentWorkflowEvent:
    if not isinstance(context, TrustedDeploymentWorkflowContext):
        raise ValueError("trusted workflow context is required")
    if not isinstance(intent, DeploymentWorkflowIntent):
        raise ValueError("deployment workflow intent is required")
    repository = payload.get("repository")
    run = payload.get("workflow_run")
    if not isinstance(repository, Mapping) or not isinstance(run, Mapping):
        raise ValueError("workflow payload is invalid")
    if (
        repository.get("full_name") != intent.workflow.repository
        or repository.get("id") != intent.workflow.repository_id
        or context.repository != intent.workflow.repository
        or context.repository_id != intent.workflow.repository_id
    ):
        raise ValueError("workflow repository identity is untrusted")
    path_value = run.get("path")
    if not isinstance(path_value, str):
        raise ValueError("workflow path is invalid")
    path_text, separator, ref_text = path_value.partition("@")
    if (
        path_text != intent.workflow.path.as_posix()
        or separator != "@"
        or ref_text != intent.workflow.ref
        or run.get("workflow_id") != intent.workflow.workflow_id
        or context.workflow_id != intent.workflow.workflow_id
        or context.workflow_path != intent.workflow.path
    ):
        raise ValueError("workflow identity is untrusted")
    actor = run.get("actor")
    actor_login = (
        actor.get("login") if isinstance(actor, Mapping) else None
    )
    manual_dispatch_actor = (
        intent.workflow.trigger is DeploymentTrigger.MANUAL
        and intent.workflow.actor == "workflow-dispatch"
    )
    if (
        (not manual_dispatch_actor and actor_login != intent.workflow.actor)
        or (
            manual_dispatch_actor
            and (
                not isinstance(actor_login, str)
                or not actor_login.endswith("[bot]")
            )
        )
        or context.actor != intent.workflow.actor
        or context.deployment_client_id
        != intent.workflow.deployment_client_id
    ):
        raise ValueError("workflow actor identity is untrusted")
    if (
        intent.workflow.trigger is DeploymentTrigger.MERGE
        and run.get("head_sha") != intent.binding.merge_commit
    ):
        raise ValueError("workflow merge commit is untrusted")
    if (
        intent.workflow.trigger is DeploymentTrigger.MANUAL
        and run.get("display_title") != intent.effect_id
    ):
        raise ValueError("workflow dispatch correlation is untrusted")
    run_id = run.get("id")
    run_url = run.get("html_url")
    if type(run_id) is not int or not isinstance(run_url, str):
        raise ValueError("workflow run identity is invalid")
    status = str(run.get("status", "")).casefold()
    conclusion = str(run.get("conclusion", "")).casefold()
    if status == "completed":
        if conclusion == "success":
            state = DeploymentWorkflowRunState.SUCCESS
        elif conclusion in {"cancelled", "skipped", "neutral"}:
            state = DeploymentWorkflowRunState.CANCELLED
        elif conclusion == "timed_out":
            state = DeploymentWorkflowRunState.TIMED_OUT
        else:
            state = DeploymentWorkflowRunState.FAILURE
        normalized_conclusion: str | None = conclusion or "failure"
    elif status == "in_progress":
        state = DeploymentWorkflowRunState.IN_PROGRESS
        normalized_conclusion = None
    elif status in {"queued", "requested", "waiting", "pending"}:
        state = DeploymentWorkflowRunState.QUEUED
        normalized_conclusion = None
    else:
        raise ValueError("workflow run status is invalid")
    result = DeploymentWorkflowResult(
        effect_id=intent.effect_id,
        result_id=f"deployment-run-{run_id}-{state.value}",
        attempt=intent.attempt,
        binding=intent.binding,
        workflow=intent.workflow,
        run_id=run_id,
        run_url=run_url,
        state=state,
        conclusion=normalized_conclusion,
        run_actor=str(actor_login),
    )
    return DeploymentWorkflowEvent(context, result, occurred_at)


class DeploymentWorkflowInbox(Protocol):
    def append(
        self,
        issue_number: int,
        event: CampaignEvent,
    ) -> bool: ...


class DeploymentWorkflowIntakeStatus(StrEnum):
    RECORDED = "recorded"
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class DeploymentWorkflowIntakeResult:
    status: DeploymentWorkflowIntakeStatus
    event: CampaignEvent


class DeploymentWorkflowEventIntake:
    def __init__(self, inbox: DeploymentWorkflowInbox) -> None:
        self._inbox = inbox

    def ingest(
        self,
        event: DeploymentWorkflowEvent,
    ) -> DeploymentWorkflowIntakeResult:
        campaign_event = event.to_campaign_event()
        recorded = self._inbox.append(
            event.result.binding.issue_number,
            campaign_event,
        )
        return DeploymentWorkflowIntakeResult(
            (
                DeploymentWorkflowIntakeStatus.RECORDED
                if recorded
                else DeploymentWorkflowIntakeStatus.DUPLICATE
            ),
            campaign_event,
        )


class DeploymentPublicationStatus(StrEnum):
    VERIFIED = "verified"
    PENDING = "pending"
    MISMATCH = "mismatch"
    FAILED = "failed"


@dataclass(frozen=True)
class DeploymentPublishedVerificationRequest:
    repository_root: Path
    plan: DeploymentPlan
    intent: DeploymentWorkflowIntent
    workflow_result: DeploymentWorkflowResult


@dataclass(frozen=True)
class DeploymentPublishedVerification:
    status: DeploymentPublicationStatus
    intent: DeploymentWorkflowIntent
    workflow_result: DeploymentWorkflowResult
    deployment_version: int | None = None
    source_sha256: str | None = None
    tree_sha: str | None = None
    bundle_sha256: str | None = None
    merge_commit: str | None = None
    lineage_sha256: str | None = None
    metadata_sha256: str | None = None
    portal_url: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, DeploymentPublicationStatus):
            raise ValueError("publication status is invalid")
        if not isinstance(self.intent, DeploymentWorkflowIntent):
            raise ValueError("intent is invalid")
        if not isinstance(
            self.workflow_result, DeploymentWorkflowResult
        ):
            raise ValueError("workflow_result is invalid")
        self.workflow_result.require_matches(self.intent)
        if self.status is DeploymentPublicationStatus.VERIFIED:
            _positive(self.deployment_version, "deployment_version")
            for value, field_name in (
                (self.source_sha256, "source_sha256"),
                (self.bundle_sha256, "bundle_sha256"),
                (self.lineage_sha256, "lineage_sha256"),
                (self.metadata_sha256, "metadata_sha256"),
            ):
                _sha256(value, field_name)
            _commit(str(self.tree_sha), "tree_sha")
            _commit(str(self.merge_commit), "merge_commit")
            if self.portal_url is None:
                raise ValueError("portal_url is required")
            try:
                parsed = urlsplit(self.portal_url)
            except ValueError as error:
                raise ValueError("portal_url is invalid") from error
            if (
                parsed.scheme != "https"
                or parsed.hostname not in {"ai.azure.com", "portal.azure.com"}
                or parsed.port is not None
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("portal_url is invalid")
        elif self.reason is not None:
            _identifier(self.reason, "reason")

    def require_exact_lineage(self) -> None:
        if self.lineage_mismatch_reason() is not None:
            raise ValueError("published deployment lineage changed")

    def lineage_mismatch_reason(self) -> str | None:
        binding = self.intent.binding
        if self.source_sha256 != binding.bundle_sha256:
            return "published_source_mismatch"
        if self.bundle_sha256 != binding.bundle_sha256:
            return "published_bundle_mismatch"
        if self.tree_sha != binding.tree_sha:
            return "published_tree_mismatch"
        if self.merge_commit != binding.merge_commit:
            return "published_merge_lineage_mismatch"
        if self.lineage_sha256 != self.intent.lineage_sha256:
            return "published_lineage_mismatch"
        return None


class DeploymentPublicationVerifier(Protocol):
    def verify(
        self,
        request: DeploymentPublishedVerificationRequest,
    ) -> DeploymentPublishedVerification: ...


class LedgerDeploymentPublicationVerifier:
    """Read a bridge-recorded publication result without Azure credentials."""

    def __init__(self, ledger: DeploymentLedger) -> None:
        self._ledger = ledger

    def verify(
        self,
        request: DeploymentPublishedVerificationRequest,
    ) -> DeploymentPublishedVerification:
        snapshot = self._ledger.load(
            request.repository_root,
            request.intent.binding.issue_number,
        )
        if snapshot is None:
            raise RuntimeError("deployment publication state is unavailable")
        records = tuple(
            record
            for record in snapshot.outbox
            if (
                record.kind == "deployment_publication_observed"
                and record.generation == request.intent.binding.generation
                and record.payload.get("effect_id")
                == request.intent.effect_id
                and record.payload.get("run_id")
                == request.workflow_result.run_id
            )
        )
        if not records:
            return DeploymentPublishedVerification(
                DeploymentPublicationStatus.PENDING,
                request.intent,
                request.workflow_result,
                reason="deployment_publication_result_pending",
            )
        results = tuple(
            deployment_published_verification_from_record(
                record,
                request.intent,
                request.workflow_result,
            )
            for record in records
        )
        terminal = tuple(
            result
            for result in results
            if result.status is not DeploymentPublicationStatus.PENDING
        )
        if len(terminal) > 1 and any(
            result != terminal[0] for result in terminal[1:]
        ):
            raise ValueError("deployment publication results conflict")
        return terminal[-1] if terminal else results[-1]


class DeploymentPublicationResultRecorder:
    """CAS-persist the deployment-identity bridge's publication result."""

    def __init__(self, ledger: DeploymentLedger) -> None:
        self._ledger = ledger

    def record(
        self,
        repository_root: Path,
        issue_number: int,
        result: DeploymentPublishedVerification,
    ) -> StateRefSnapshot:
        snapshot = self._ledger.load(repository_root, issue_number)
        if snapshot is None:
            raise ValueError("deployment publication requires campaign state")
        if result.intent.binding.issue_number != issue_number:
            raise ValueError("deployment publication issue does not match")
        planned = tuple(
            record
            for record in snapshot.outbox
            if record.record_id == result.intent.effect_id
        )
        if len(planned) != 1:
            raise ValueError("deployment publication intent is unavailable")
        if deployment_workflow_intent(planned[0]) != result.intent:
            raise ValueError("deployment publication intent changed")
        record = deployment_published_verification_record(
            result,
            sequence=snapshot.state.sequence,
        )
        existing = tuple(
            item
            for item in snapshot.outbox
            if item.record_id == record.record_id
        )
        if existing:
            if len(existing) != 1 or existing[0] != record:
                raise ValueError("deployment publication result conflicts")
            return snapshot
        return self._ledger.commit(
            repository_root,
            issue_number=issue_number,
            expected_revision=snapshot.revision,
            state=snapshot.state,
            outbox=(record,),
        )


class ExistingDeploymentPublicationVerifier:
    """Adapt the verified legacy deployment coordinator without dispatching."""

    def __init__(
        self,
        coordinator: object,
        *,
        request_factory: Callable[
            [DeploymentPublishedVerificationRequest], object
        ],
    ) -> None:
        self._coordinator = coordinator
        self._request_factory = request_factory

    def verify(
        self,
        request: DeploymentPublishedVerificationRequest,
    ) -> DeploymentPublishedVerification:
        from foundry_opt.optimization.lifecycle import (
            DeploymentOutcomeStatus,
        )

        legacy_request = self._request_factory(request)
        if getattr(legacy_request, "dispatch", False):
            raise ValueError(
                "publication verification must not dispatch deployment"
            )
        outcome = self._coordinator.deploy(legacy_request)
        if outcome.status in {
            DeploymentOutcomeStatus.PENDING,
            DeploymentOutcomeStatus.MANUAL_TRIGGER_REQUIRED,
        }:
            return DeploymentPublishedVerification(
                DeploymentPublicationStatus.PENDING,
                request.intent,
                request.workflow_result,
                reason=outcome.reason_code or "published_version_pending",
            )
        if outcome.status is not DeploymentOutcomeStatus.VERIFIED:
            return DeploymentPublishedVerification(
                DeploymentPublicationStatus.MISMATCH,
                request.intent,
                request.workflow_result,
                reason=outcome.reason_code or "published_version_mismatch",
            )
        if (
            outcome.version is None
            or outcome.run_url != request.workflow_result.run_url
            or outcome.portal_url is None
            or outcome.source_sha256 is None
            or outcome.tree_sha is None
            or outcome.bundle_sha256 is None
            or outcome.merge_commit is None
            or outcome.lineage_sha256 is None
            or outcome.metadata_sha256 is None
        ):
            return DeploymentPublishedVerification(
                DeploymentPublicationStatus.MISMATCH,
                request.intent,
                request.workflow_result,
                reason="published_version_mismatch",
            )
        return DeploymentPublishedVerification(
            DeploymentPublicationStatus.VERIFIED,
            request.intent,
            request.workflow_result,
            deployment_version=outcome.version,
            source_sha256=outcome.source_sha256,
            tree_sha=outcome.tree_sha,
            bundle_sha256=outcome.bundle_sha256,
            merge_commit=outcome.merge_commit,
            lineage_sha256=outcome.lineage_sha256,
            metadata_sha256=outcome.metadata_sha256,
            portal_url=outcome.portal_url,
        )


@dataclass(frozen=True)
class PostDeploymentEvaluationIntent:
    effect_id: str
    deployment_effect_id: str
    binding: DeploymentBinding
    workflow: DeploymentWorkflowIdentity
    deployment_version: int
    evaluation_id: str
    evaluation_policy_sha256: str
    idempotency_key: str

    def __post_init__(self) -> None:
        _identifier(self.effect_id, "effect_id")
        _identifier(self.deployment_effect_id, "deployment_effect_id")
        if not isinstance(self.binding, DeploymentBinding):
            raise ValueError("binding must be a DeploymentBinding")
        if not isinstance(self.workflow, DeploymentWorkflowIdentity):
            raise ValueError("workflow must be a DeploymentWorkflowIdentity")
        _positive(self.deployment_version, "deployment_version")
        _identifier(self.evaluation_id, "evaluation_id")
        _sha256(
            self.evaluation_policy_sha256,
            "evaluation_policy_sha256",
        )
        _sha256(self.idempotency_key, "idempotency_key")

    @property
    def binding_sha256(self) -> str:
        return self.binding.binding_sha256


class PostDeploymentEvaluationStatus(StrEnum):
    PENDING = "pending"
    RETAINED_IMPROVEMENT = "retained_improvement"
    REGRESSED = "regressed"


@dataclass(frozen=True)
class PostDeploymentEvaluationResult:
    result_id: str
    intent: PostDeploymentEvaluationIntent
    status: PostDeploymentEvaluationStatus
    reason: str | None = None
    baseline_metrics: Mapping[str, float] = field(default_factory=dict)
    selected_draft_metrics: Mapping[str, float] = field(default_factory=dict)
    deployed_metrics: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _identifier(self.result_id, "result_id")
        if not isinstance(self.intent, PostDeploymentEvaluationIntent):
            raise ValueError(
                "intent must be a PostDeploymentEvaluationIntent"
            )
        if not isinstance(self.status, PostDeploymentEvaluationStatus):
            raise ValueError("evaluation status is invalid")
        if (
            self.status is PostDeploymentEvaluationStatus.REGRESSED
        ) != (self.reason is not None):
            raise ValueError(
                "only regressed evaluation results require a reason"
            )
        if self.reason is not None:
            _identifier(self.reason, "reason")
        for field_name in (
            "baseline_metrics",
            "selected_draft_metrics",
            "deployed_metrics",
        ):
            values = dict(getattr(self, field_name))
            for name, value in values.items():
                _identifier(name, "metric")
                if (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not isfinite(value)
                ):
                    raise ValueError(
                        f"{field_name} must contain finite aggregates"
                    )
            object.__setattr__(
                self,
                field_name,
                MappingProxyType(values),
            )


class PostDeploymentEvaluationEffects(Protocol):
    def reconcile(
        self,
        intent: PostDeploymentEvaluationIntent,
    ) -> PostDeploymentEvaluationResult | None: ...

    def run(
        self,
        intent: PostDeploymentEvaluationIntent,
    ) -> PostDeploymentEvaluationResult: ...


class ExistingPostDeploymentEvaluationEffects:
    """Adapt the existing pinned held-out evaluator to canonical effects."""

    def __init__(
        self,
        evaluator: object,
        *,
        request_factory: Callable[
            [PostDeploymentEvaluationIntent], object
        ],
        result_reader: Callable[
            [PostDeploymentEvaluationIntent],
            PostDeploymentEvaluationResult | None,
        ]
        | None = None,
        result_writer: Callable[
            [PostDeploymentEvaluationResult], None
        ]
        | None = None,
    ) -> None:
        self._evaluator = evaluator
        self._request_factory = request_factory
        self._results: dict[str, PostDeploymentEvaluationResult] = {}
        self._result_reader = result_reader
        self._result_writer = result_writer

    def reconcile(
        self,
        intent: PostDeploymentEvaluationIntent,
    ) -> PostDeploymentEvaluationResult | None:
        if self._result_reader is not None:
            return self._result_reader(intent)
        return self._results.get(intent.idempotency_key)

    def run(
        self,
        intent: PostDeploymentEvaluationIntent,
    ) -> PostDeploymentEvaluationResult:
        from foundry_opt.optimization.lifecycle import PostDeployStatus

        existing = self.reconcile(intent)
        if existing is not None:
            return existing
        outcome = self._evaluator.evaluate(self._request_factory(intent))
        status = {
            PostDeployStatus.PENDING: PostDeploymentEvaluationStatus.PENDING,
            PostDeployStatus.REGRESSED: (
                PostDeploymentEvaluationStatus.REGRESSED
            ),
            PostDeployStatus.RETAINED_IMPROVEMENT: (
                PostDeploymentEvaluationStatus.RETAINED_IMPROVEMENT
            ),
        }[outcome.status]
        reason = (
            outcome.reason_code or "post_deploy_regression"
            if status is PostDeploymentEvaluationStatus.REGRESSED
            else None
        )
        result = PostDeploymentEvaluationResult(
            result_id=(
                f"{intent.effect_id}-{status.value}-result"
            ),
            intent=intent,
            status=status,
            reason=reason,
            baseline_metrics=getattr(
                outcome, "baseline_metrics", {}
            ),
            selected_draft_metrics=getattr(
                outcome, "selected_draft_metrics", {}
            ),
            deployed_metrics=outcome.metrics,
        )
        if status is not PostDeploymentEvaluationStatus.PENDING:
            if self._result_writer is not None:
                self._result_writer(result)
            else:
                self._results[intent.idempotency_key] = result
        return result


class DeploymentDispatchClaimStatus(StrEnum):
    CLAIMED = "claimed"
    ALREADY_CLAIMED = "already_claimed"


class DeploymentDispatchClaimer(Protocol):
    def claim(
        self,
        intent: DeploymentWorkflowIntent,
    ) -> DeploymentDispatchClaimStatus: ...


class DeploymentDispatchClaimRecorder:
    """CAS-persist an at-most-once bridge claim before workflow dispatch."""

    def __init__(
        self,
        ledger: DeploymentLedger,
        repository_root: Path,
        issue_number: int,
    ) -> None:
        _positive(issue_number, "issue_number")
        self._ledger = ledger
        self._root = repository_root
        self._issue_number = issue_number

    def claim(
        self,
        intent: DeploymentWorkflowIntent,
    ) -> DeploymentDispatchClaimStatus:
        snapshot = self._ledger.load(self._root, self._issue_number)
        if snapshot is None:
            raise RuntimeError("deployment dispatch claim has no campaign")
        planned = tuple(
            record
            for record in snapshot.outbox
            if record.record_id == intent.effect_id
        )
        if len(planned) != 1:
            raise RuntimeError("deployment intent is unavailable")
        if deployment_workflow_intent(planned[0]) != intent:
            raise RuntimeError("deployment intent changed before claim")
        claim = OutboxRecord(
            record_id=f"{intent.effect_id}-claimed",
            kind="deployment_dispatch_claimed",
            generation=snapshot.state.generation,
            sequence=snapshot.state.sequence,
            payload={
                "attempt": intent.attempt,
                "binding_sha256": intent.binding.binding_sha256,
                "candidate_id": intent.binding.candidate_id,
                "effect_id": intent.effect_id,
                "issue_number": intent.binding.issue_number,
                "result": "claimed",
            },
        )
        existing = tuple(
            record
            for record in snapshot.outbox
            if record.record_id == claim.record_id
        )
        if existing:
            if (
                len(existing) != 1
                or existing[0].kind != claim.kind
                or dict(existing[0].payload) != dict(claim.payload)
            ):
                raise RuntimeError("deployment dispatch claim conflicts")
            return DeploymentDispatchClaimStatus.ALREADY_CLAIMED
        try:
            self._ledger.commit(
                self._root,
                issue_number=self._issue_number,
                expected_revision=snapshot.revision,
                state=snapshot.state,
                outbox=(claim,),
            )
        except (StateRefConflictError, StateRefError, ValueError) as error:
            raise RuntimeError(
                "deployment dispatch claim could not persist"
            ) from error
        return DeploymentDispatchClaimStatus.CLAIMED


class DeploymentWorkflowGateway(Protocol):
    def find(
        self,
        intent: DeploymentWorkflowIntent,
    ) -> DeploymentWorkflowResult | None: ...

    def dispatch(self, intent: DeploymentWorkflowIntent) -> None: ...


class DeploymentWorkflowResultRecorder:
    """Persist the exact workflow run ID before webhook reconciliation."""

    def __init__(self, ledger: DeploymentLedger) -> None:
        self._ledger = ledger

    def record(
        self,
        repository_root: Path,
        issue_number: int,
        result: DeploymentWorkflowResult,
    ) -> StateRefSnapshot:
        snapshot = self._ledger.load(repository_root, issue_number)
        if snapshot is None:
            raise ValueError("workflow result requires campaign state")
        planned = tuple(
            record
            for record in snapshot.outbox
            if record.record_id == result.effect_id
        )
        if len(planned) != 1:
            raise ValueError("workflow result intent is unavailable")
        intent = deployment_workflow_intent(planned[0])
        result.require_matches(intent)
        prior_bindings = tuple(
            record
            for record in snapshot.outbox
            if (
                record.kind == "deployment_workflow_run_bound"
                and record.payload.get("effect_id") == result.effect_id
            )
        )
        if any(
            record.payload.get("run_id") != result.run_id
            for record in prior_bindings
        ):
            raise ValueError(
                "deployment attempt is already bound to another run"
            )
        record = OutboxRecord(
            record_id=f"{result.effect_id}-run-{result.run_id}",
            kind="deployment_workflow_run_bound",
            generation=snapshot.state.generation,
            sequence=snapshot.state.sequence,
            payload={
                "attempt": result.attempt,
                "binding_sha256": result.binding.binding_sha256,
                "effect_id": result.effect_id,
                "issue_number": result.binding.issue_number,
                "run_id": result.run_id,
                "run_actor": result.run_actor,
                "run_url": result.run_url,
            },
        )
        existing = tuple(
            item
            for item in snapshot.outbox
            if item.record_id == record.record_id
        )
        if existing:
            if (
                len(existing) != 1
                or existing[0].kind != record.kind
                or existing[0].generation != record.generation
                or dict(existing[0].payload) != dict(record.payload)
            ):
                raise ValueError("workflow run binding conflicts")
            return snapshot
        return self._ledger.commit(
            repository_root,
            issue_number=issue_number,
            expected_revision=snapshot.revision,
            state=snapshot.state,
            outbox=(record,),
        )


class ExistingDeploymentWorkflowGateway:
    """Adapt the existing gh workflow gateway for the transport bridge."""

    def __init__(
        self,
        repository_root: Path,
        gateway: object,
        *,
        dispatch_input_name: str = "selected_commit",
        correlation_input_name: str = "foundry_opt_effect_id",
    ) -> None:
        _identifier(dispatch_input_name, "dispatch_input_name")
        _identifier(correlation_input_name, "correlation_input_name")
        self._root = repository_root
        self._gateway = gateway
        self._dispatch_input_name = dispatch_input_name
        self._correlation_input_name = correlation_input_name

    def find(
        self,
        intent: DeploymentWorkflowIntent,
    ) -> DeploymentWorkflowResult | None:
        from foundry_opt.adapters.optimization_deployment import (
            WorkflowRunQuery,
        )
        from foundry_opt.deployment import WorkflowRunStatus

        events = (
            ("workflow_dispatch",)
            if intent.workflow.trigger is DeploymentTrigger.MANUAL
            else ("push", "workflow_run")
        )
        run = self._gateway.find_run(
            self._root,
            query=WorkflowRunQuery(
                intent.workflow.path,
                events,
                intent.binding.merge_commit,
                intent.workflow.trigger,
                intent.workflow.trigger is not DeploymentTrigger.MANUAL,
                (
                    intent.effect_id
                    if intent.workflow.trigger is DeploymentTrigger.MANUAL
                    else None
                ),
            ),
        )
        if run is None:
            return None
        if (
            run.path != intent.workflow.path
            or run.trigger is not intent.workflow.trigger
            or run.head_commit != intent.binding.merge_commit
        ):
            raise ValueError("legacy workflow result changed identity")
        state = {
            WorkflowRunStatus.QUEUED: DeploymentWorkflowRunState.QUEUED,
            WorkflowRunStatus.IN_PROGRESS: (
                DeploymentWorkflowRunState.IN_PROGRESS
            ),
            WorkflowRunStatus.SUCCESS: DeploymentWorkflowRunState.SUCCESS,
            WorkflowRunStatus.FAILURE: DeploymentWorkflowRunState.FAILURE,
            WorkflowRunStatus.CANCELLED: (
                DeploymentWorkflowRunState.CANCELLED
            ),
        }[run.status]
        run_id = _run_id_from_url(run.url)
        conclusion = (
            None
            if state
            in {
                DeploymentWorkflowRunState.QUEUED,
                DeploymentWorkflowRunState.IN_PROGRESS,
            }
            else state.value
        )
        return DeploymentWorkflowResult(
            effect_id=intent.effect_id,
            result_id=f"deployment-run-{run_id}-{state.value}",
            attempt=intent.attempt,
            binding=intent.binding,
            workflow=intent.workflow,
            run_id=run_id,
            run_url=run.url,
            state=state,
            conclusion=conclusion,
        )

    def dispatch(self, intent: DeploymentWorkflowIntent) -> None:
        self._gateway.dispatch(
            self._root,
            workflow_path=intent.workflow.path,
            input_name=self._dispatch_input_name,
            commit=intent.binding.merge_commit,
            correlation_input_name=self._correlation_input_name,
            correlation_id=intent.effect_id,
        )


class DeploymentBridgeStatus(StrEnum):
    DISPATCHED = "dispatched"
    OBSERVED = "observed"
    WAITING = "waiting"
    INVALID = "invalid"


@dataclass(frozen=True)
class DeploymentBridgeResult:
    status: DeploymentBridgeStatus
    result: DeploymentWorkflowResult | None = None
    reason: str | None = None


class DeploymentWorkflowBridge:
    """Execute only a persisted deployment intent, never steward credentials."""

    def __init__(
        self,
        *,
        gateway: DeploymentWorkflowGateway,
        claimer: DeploymentDispatchClaimer,
    ) -> None:
        self._gateway = gateway
        self._claimer = claimer

    def apply(self, record: OutboxRecord) -> DeploymentBridgeResult:
        try:
            intent = deployment_workflow_intent(record)
            if intent.workflow.trigger is not DeploymentTrigger.MANUAL:
                observed = self._gateway.find(intent)
                if observed is not None:
                    observed.require_matches(intent)
                    return DeploymentBridgeResult(
                        DeploymentBridgeStatus.OBSERVED,
                        observed,
                    )
                return DeploymentBridgeResult(
                    DeploymentBridgeStatus.WAITING,
                    reason="deployment_workflow_not_observed",
                )
            try:
                claim = self._claimer.claim(intent)
            except RuntimeError:
                return DeploymentBridgeResult(
                    DeploymentBridgeStatus.WAITING,
                    reason="deployment_dispatch_claim_unavailable",
                )
            if claim is DeploymentDispatchClaimStatus.ALREADY_CLAIMED:
                return DeploymentBridgeResult(
                    DeploymentBridgeStatus.WAITING,
                    reason="deployment_dispatch_ack_pending",
                )
            try:
                self._gateway.dispatch(intent)
            except RuntimeError:
                return DeploymentBridgeResult(
                    DeploymentBridgeStatus.WAITING,
                    reason="deployment_dispatch_ack_unknown",
                )
            return DeploymentBridgeResult(
                DeploymentBridgeStatus.DISPATCHED,
                reason="deployment_run_pending",
            )
        except (KeyError, TypeError, ValueError):
            return DeploymentBridgeResult(
                DeploymentBridgeStatus.INVALID,
                reason="deployment_workflow_intent_invalid",
            )


class DeploymentCleanupKind(StrEnum):
    CANDIDATE_ISSUE_CLOSE = "candidate_issue_close_planned"
    CANDIDATE_ISSUE_SUPERSEDE = "candidate_issue_supersede_planned"
    CANDIDATE_PR_SUPERSEDE = "candidate_pr_supersede_planned"
    CAMPAIGN_PR_CLOSE = "campaign_pr_close_planned"
    OPTIMIZATION_PR_CLOSE = "optimization_pr_close_planned"
    FINAL_DASHBOARD = "deployment_final_dashboard"
    ROOT_COMMENT_FINAL = "root_comment_final_planned"
    ROOT_ISSUE_CLOSE = "root_issue_close_planned"


@dataclass(frozen=True)
class DeploymentCleanupEffect:
    effect_id: str
    kind: DeploymentCleanupKind
    generation: int
    sequence: int
    issue_number: int
    target_number: int
    reason: str
    candidate_id: str | None = None
    dependencies: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _identifier(self.effect_id, "effect_id")
        if not isinstance(self.kind, DeploymentCleanupKind):
            raise ValueError("cleanup kind is invalid")
        _positive(self.generation, "generation")
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("sequence must not be negative")
        _positive(self.issue_number, "issue_number")
        _positive(self.target_number, "target_number")
        _identifier(self.reason, "reason")
        if self.candidate_id is not None:
            _identifier(self.candidate_id, "candidate_id")
        dependencies = tuple(self.dependencies)
        if len(set(dependencies)) != len(dependencies):
            raise ValueError("cleanup dependencies must be unique")
        for dependency in dependencies:
            _identifier(dependency, "cleanup dependency")
        object.__setattr__(self, "dependencies", dependencies)
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(dict(self.metadata)),
        )


class DeploymentCleanupGateway(Protocol):
    def effect_applied(self, effect_id: str) -> bool: ...

    def apply(self, effect: DeploymentCleanupEffect) -> None: ...


class DeploymentCleanupBridgeStatus(StrEnum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    WAITING = "waiting"
    RETRY = "retry"
    INVALID = "invalid"


@dataclass(frozen=True)
class DeploymentCleanupBridgeResult:
    status: DeploymentCleanupBridgeStatus
    effect: DeploymentCleanupEffect | None = None
    reason: str | None = None


class DeploymentCleanupBridge:
    def __init__(self, gateway: DeploymentCleanupGateway) -> None:
        self._gateway = gateway

    def apply(self, record: OutboxRecord) -> DeploymentCleanupBridgeResult:
        try:
            effect = deployment_cleanup_effect(record)
        except (KeyError, TypeError, ValueError):
            return DeploymentCleanupBridgeResult(
                DeploymentCleanupBridgeStatus.INVALID,
                reason="deployment_cleanup_intent_invalid",
            )
        try:
            if self._gateway.effect_applied(effect.effect_id):
                return DeploymentCleanupBridgeResult(
                    DeploymentCleanupBridgeStatus.ALREADY_APPLIED,
                    effect,
                )
            if any(
                not self._gateway.effect_applied(dependency)
                for dependency in effect.dependencies
            ):
                return DeploymentCleanupBridgeResult(
                    DeploymentCleanupBridgeStatus.WAITING,
                    effect,
                    "deployment_cleanup_dependency_pending",
                )
            self._gateway.apply(effect)
        except RuntimeError:
            return DeploymentCleanupBridgeResult(
                DeploymentCleanupBridgeStatus.RETRY,
                effect,
                "deployment_cleanup_ack_unknown",
            )
        return DeploymentCleanupBridgeResult(
            DeploymentCleanupBridgeStatus.APPLIED,
            effect,
        )


@dataclass(frozen=True)
class DeploymentOrchestrationRequest:
    repository_root: Path
    issue_number: int

    def __post_init__(self) -> None:
        _positive(self.issue_number, "issue_number")


class DeploymentOrchestrationStatus(StrEnum):
    PLANNED = "planned"
    WAITING = "waiting"
    RETRYING = "retrying"
    COMPLETE = "complete"
    READY_FOR_HUMAN = "ready_for_human"
    CONFLICT = "conflict"
    FAILED = "failed"


@dataclass(frozen=True)
class DeploymentOrchestrationResult:
    status: DeploymentOrchestrationStatus
    snapshot: StateRefSnapshot
    summary: str
    code: str | None = None


class DeploymentLedger(Protocol):
    def load(
        self,
        repository_root: Path,
        issue_number: int,
    ) -> StateRefSnapshot | None: ...

    def commit(
        self,
        repository_root: Path,
        *,
        issue_number: int,
        expected_revision: str | None,
        state: CampaignState,
        inbox: tuple[CampaignEvent, ...] = (),
        outbox: tuple[OutboxRecord, ...] = (),
    ) -> StateRefSnapshot: ...


class DeploymentPlanResolver(Protocol):
    def resolve(
        self,
        request: DeploymentOrchestrationRequest,
        state: CampaignState,
    ) -> DeploymentPlan: ...


class DeploymentSelectionReader(Protocol):
    def read(
        self,
        request: DeploymentOrchestrationRequest,
        binding: CandidateBinding,
        plan: DeploymentPlan,
    ) -> DeploymentSelectionSnapshot: ...


class CandidateDeploymentSelectionReader:
    """Re-read the native candidate PR before deployment intent creation."""

    def __init__(self, reader: CandidatePullRequestReader) -> None:
        self._reader = reader

    def read(
        self,
        request: DeploymentOrchestrationRequest,
        binding: CandidateBinding,
        plan: DeploymentPlan,
    ) -> DeploymentSelectionSnapshot:
        default_branch = plan.workflow.ref.removeprefix("refs/heads/")
        snapshots = self._reader.snapshots_for(
            CandidateSelectionRequest(
                request.repository_root,
                request.issue_number,
                default_branch,
                plan.required_checks,
            ),
            (binding,),
        )
        matching = tuple(
            snapshot
            for snapshot in snapshots
            if (
                snapshot.state is CandidatePullRequestState.MERGED
                and snapshot.binding_sha256 == binding.binding_sha256
            )
        )
        if len(matching) != 1:
            raise ValueError("selected candidate merge is unavailable")
        snapshot = matching[0]
        verification = verify_candidate_pull_request(
            binding,
            snapshot,
            expected_default_branch=default_branch,
            required_checks=plan.required_checks,
        )
        if (
            verification.status
            is not CandidatePullRequestVerificationStatus.VERIFIED
            or snapshot.merge_commit is None
            or snapshot.merge_tree_sha is None
            or snapshot.merge_actor is None
        ):
            raise ValueError("selected candidate merge is not verified")
        return DeploymentSelectionSnapshot(
            binding=binding,
            candidate_pull_request_number=snapshot.pull_request_number,
            candidate_issue_number=snapshot.worker_issue_number,
            head_commit=snapshot.head_commit,
            merge_commit=snapshot.merge_commit,
            merge_tree_sha=snapshot.merge_tree_sha,
            merge_actor=snapshot.merge_actor,
            checks=snapshot.checks,
        )


class DeploymentOrchestrationService:
    """Plan an exact deployment effect before any bridge may dispatch it."""

    def __init__(
        self,
        *,
        ledger: DeploymentLedger,
        resolver: DeploymentPlanResolver,
        selection_reader: DeploymentSelectionReader,
        publication_verifier: DeploymentPublicationVerifier | None = None,
        evaluation_effects: PostDeploymentEvaluationEffects | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._ledger = ledger
        self._resolver = resolver
        self._selection_reader = selection_reader
        self._publication_verifier = publication_verifier
        self._evaluation_effects = evaluation_effects
        self._clock = clock or (lambda: datetime.now(UTC))

    def advance(
        self,
        request: DeploymentOrchestrationRequest,
    ) -> DeploymentOrchestrationResult:
        snapshot = self._ledger.load(
            request.repository_root,
            request.issue_number,
        )
        if snapshot is None:
            raise ValueError("deployment requires campaign state")
        if snapshot.state.phase is CampaignPhase.COMPLETED:
            return DeploymentOrchestrationResult(
                DeploymentOrchestrationStatus.COMPLETE,
                snapshot,
                "Deployment retention is already complete.",
            )
        if snapshot.state.phase is CampaignPhase.BLOCKED:
            return DeploymentOrchestrationResult(
                DeploymentOrchestrationStatus.READY_FOR_HUMAN,
                snapshot,
                "Deployment requires human remediation.",
                snapshot.state.block_reason,
            )
        if snapshot.state.phase not in {
            CampaignPhase.DEPLOYMENT,
            CampaignPhase.RETENTION,
        }:
            return DeploymentOrchestrationResult(
                DeploymentOrchestrationStatus.FAILED,
                snapshot,
                "Deployment orchestration is invalid in this phase.",
                "deployment_phase_invalid",
            )
        try:
            plan = self._resolver.resolve(request, snapshot.state)
        except Exception:
            return self._block(
                request,
                snapshot,
                "deployment_policy_unavailable",
                "Deployment policy could not be resolved.",
            )
        mismatch = _plan_mismatch(request, snapshot.state, plan)
        if mismatch is not None:
            return self._block(
                request,
                snapshot,
                mismatch,
                "Deployment policy does not match the selected campaign.",
            )
        if snapshot.state.phase is CampaignPhase.RETENTION:
            return self._advance_retention(request, snapshot, plan)
        existing = tuple(
            record
            for record in snapshot.outbox
            if (
                record.kind == "deployment_workflow_planned"
                and record.generation == snapshot.state.generation
            )
        )
        if existing:
            return self._advance_existing_deployment(
                request,
                snapshot,
                plan,
                existing,
            )
        try:
            candidate = _selected_candidate_binding(snapshot)
            selection = self._selection_reader.read(
                request,
                candidate,
                plan,
            )
            binding = _verified_deployment_binding(
                snapshot,
                candidate,
                selection,
                plan,
            )
            intent = _workflow_intent(
                plan,
                binding,
                attempt=1,
                planned_at=self._clock(),
            )
            record = _intent_record(snapshot, intent)
            dashboard = OutboxRecord(
                record_id=(
                    f"deployment-dashboard-{binding.generation}-"
                    f"{binding.binding_sha256[:16]}"
                ),
                kind="deployment_dashboard",
                generation=snapshot.state.generation,
                sequence=snapshot.state.sequence,
                payload={
                    "candidate_id": binding.candidate_id,
                    "disposition": "delegate",
                    "issue_number": binding.issue_number,
                    "merge_commit": binding.merge_commit,
                    "next_action": "dispatch_exact_deployment_workflow",
                    "phase": CampaignPhase.DEPLOYMENT.value,
                    "status": "planned",
                },
            )
            persisted = self._ledger.commit(
                request.repository_root,
                issue_number=request.issue_number,
                expected_revision=snapshot.revision,
                state=snapshot.state,
                outbox=(record, dashboard),
            )
        except StateRefConflictError:
            return DeploymentOrchestrationResult(
                DeploymentOrchestrationStatus.CONFLICT,
                snapshot,
                "Deployment state changed concurrently.",
                "state_ref_conflict",
            )
        except (KeyError, TypeError, ValueError):
            return self._block(
                request,
                snapshot,
                "deployment_selection_invalid",
                "The selected deployment lineage could not be verified.",
            )
        except StateRefError:
            return DeploymentOrchestrationResult(
                DeploymentOrchestrationStatus.FAILED,
                snapshot,
                "The deployment intent could not be persisted.",
                "deployment_intent_persist_failed",
            )
        return DeploymentOrchestrationResult(
            DeploymentOrchestrationStatus.PLANNED,
            persisted,
            "Exact deployment intent persisted for the thin bridge.",
        )

    def _advance_existing_deployment(
        self,
        request: DeploymentOrchestrationRequest,
        snapshot: StateRefSnapshot,
        plan: DeploymentPlan,
        records: tuple[OutboxRecord, ...],
    ) -> DeploymentOrchestrationResult:
        try:
            intents = tuple(
                deployment_workflow_intent(record) for record in records
            )
            attempts = {intent.attempt for intent in intents}
            if (
                len(attempts) != len(intents)
                or max(attempts) > plan.max_attempts
            ):
                raise ValueError("deployment attempts are invalid")
            intent = max(intents, key=lambda item: item.attempt)
            results = _workflow_results(snapshot, intent)
            if not results:
                results = _bridge_recorded_workflow_results(
                    snapshot,
                    intent,
                )
            if not results:
                if self._clock() >= (
                    intent.planned_at
                    + timedelta(seconds=intent.timeout_seconds)
                ):
                    claimed = any(
                        record.record_id == f"{intent.effect_id}-claimed"
                        for record in snapshot.outbox
                    )
                    reason = (
                        "deployment_dispatch_unknown"
                        if claimed
                        else "deployment_workflow_unobserved"
                    )
                    return self._block(
                        request,
                        snapshot,
                        reason,
                        (
                            "The deployment dispatch outcome is unknown."
                            if claimed
                            else "The deployment workflow was not observed."
                        ),
                    )
                return DeploymentOrchestrationResult(
                    DeploymentOrchestrationStatus.WAITING,
                    snapshot,
                    "The exact deployment intent is awaiting reconciliation.",
                    "deployment_result_pending",
                )
            result = _effective_workflow_result(results)
            if result.state in {
                DeploymentWorkflowRunState.QUEUED,
                DeploymentWorkflowRunState.IN_PROGRESS,
            }:
                return DeploymentOrchestrationResult(
                    DeploymentOrchestrationStatus.WAITING,
                    snapshot,
                    "The trusted deployment workflow is still running.",
                    "deployment_workflow_pending",
                )
            if result.state is not DeploymentWorkflowRunState.SUCCESS:
                if intent.attempt < plan.max_attempts:
                    return self._retry(
                        request,
                        snapshot,
                        plan,
                        intent,
                        result.state,
                    )
                return self._block(
                    request,
                    snapshot,
                    f"deployment_workflow_{result.state.value}",
                    "The trusted deployment workflow did not succeed.",
                )
            if self._publication_verifier is None:
                return DeploymentOrchestrationResult(
                    DeploymentOrchestrationStatus.FAILED,
                    snapshot,
                    "Published deployment verification is unavailable.",
                    "deployment_verifier_unavailable",
                )
            published = self._publication_verifier.verify(
                DeploymentPublishedVerificationRequest(
                    request.repository_root,
                    plan,
                    intent,
                    result,
                )
            )
            if published.status is DeploymentPublicationStatus.PENDING:
                return DeploymentOrchestrationResult(
                    DeploymentOrchestrationStatus.WAITING,
                    snapshot,
                    "The published Foundry version is not visible yet.",
                    published.reason or "published_version_pending",
                )
            if published.status is not DeploymentPublicationStatus.VERIFIED:
                return self._block(
                    request,
                    snapshot,
                    published.reason or "published_version_mismatch",
                    "The published Foundry version did not verify.",
                )
            lineage_mismatch = published.lineage_mismatch_reason()
            if lineage_mismatch is not None:
                return self._block(
                    request,
                    snapshot,
                    lineage_mismatch,
                    "The published Foundry lineage did not match selection.",
                )
            assert published.deployment_version is not None
            event = CampaignEvent(
                event_id=(
                    f"deployment-complete-{intent.binding.generation}-"
                    f"{published.deployment_version}-"
                    f"{intent.lineage_sha256[:16]}"
                ),
                kind=EventKind.DEPLOYMENT_COMPLETED,
                generation=intent.binding.generation,
                occurred_at=self._clock(),
                payload={
                    "deployment_version": published.deployment_version
                },
            )
            state = _campaign_advance(
                request.issue_number,
                snapshot.state,
                event,
            )
            evaluation = _evaluation_intent_record(
                state,
                plan,
                intent,
                published,
            )
            publication = _publication_record(
                state,
                intent,
                result,
                published,
            )
            persisted = self._ledger.commit(
                request.repository_root,
                issue_number=request.issue_number,
                expected_revision=snapshot.revision,
                state=state,
                inbox=(event,),
                outbox=(publication, evaluation),
            )
        except StateRefConflictError:
            return DeploymentOrchestrationResult(
                DeploymentOrchestrationStatus.CONFLICT,
                snapshot,
                "Deployment state changed concurrently.",
                "state_ref_conflict",
            )
        except (KeyError, TypeError, ValueError):
            return self._block(
                request,
                snapshot,
                "deployment_result_mismatch",
                "The deployment result did not match its exact intent.",
            )
        except StateRefError:
            return DeploymentOrchestrationResult(
                DeploymentOrchestrationStatus.FAILED,
                snapshot,
                "Verified deployment state could not be persisted.",
                "deployment_state_persist_failed",
            )
        except RuntimeError:
            return self._block(
                request,
                snapshot,
                "deployment_verifier_unavailable",
                "Published deployment verification was unavailable.",
            )
        return DeploymentOrchestrationResult(
            DeploymentOrchestrationStatus.PLANNED,
            persisted,
            "Published deployment verified; held-out evaluation persisted.",
        )

    def _advance_retention(
        self,
        request: DeploymentOrchestrationRequest,
        snapshot: StateRefSnapshot,
        plan: DeploymentPlan,
    ) -> DeploymentOrchestrationResult:
        records = tuple(
            record
            for record in snapshot.outbox
            if (
                record.kind == "post_deployment_evaluation_planned"
                and record.generation == snapshot.state.generation
            )
        )
        if len(records) != 1:
            return self._block(
                request,
                snapshot,
                "post_deploy_intent_invalid",
                "The held-out evaluation intent is unavailable.",
            )
        if self._evaluation_effects is None:
            return DeploymentOrchestrationResult(
                DeploymentOrchestrationStatus.FAILED,
                snapshot,
                "Post-deployment evaluation is unavailable.",
                "post_deploy_evaluator_unavailable",
            )
        try:
            intent = post_deployment_evaluation_intent(records[0])
            if (
                intent.evaluation_id != plan.held_out_evaluation_id
                or intent.evaluation_policy_sha256
                != plan.evaluation_policy_sha256
                or intent.deployment_version
                != snapshot.state.deployment_version
            ):
                raise ValueError("post-deployment evaluation policy changed")
            result = self._evaluation_effects.reconcile(intent)
            if result is None:
                result = self._evaluation_effects.run(intent)
            if result.intent != intent:
                raise ValueError(
                    "post-deployment evaluation result changed intent"
                )
            if result.status is PostDeploymentEvaluationStatus.PENDING:
                return DeploymentOrchestrationResult(
                    DeploymentOrchestrationStatus.WAITING,
                    snapshot,
                    "The pinned held-out evaluation is still running.",
                    "post_deploy_pending",
                )
            if result.status is PostDeploymentEvaluationStatus.REGRESSED:
                assert result.reason is not None
                event = CampaignEvent(
                    event_id=(
                        f"deployment-failed-{snapshot.state.generation}-"
                        f"{result.reason}"
                    ),
                    kind=EventKind.DEPLOYMENT_FAILED,
                    generation=snapshot.state.generation,
                    occurred_at=self._clock(),
                    payload={"reason": result.reason},
                )
                state = _campaign_advance(
                    request.issue_number,
                    snapshot.state,
                    event,
                )
                result_record = _evaluation_result_record(state, result)
                ready = _ready_for_human_record(state, result.reason)
                label = _ready_for_human_label(state)
                persisted = self._ledger.commit(
                    request.repository_root,
                    issue_number=request.issue_number,
                    expected_revision=snapshot.revision,
                    state=state,
                    inbox=(event,),
                    outbox=(result_record, ready, label),
                )
                return DeploymentOrchestrationResult(
                    DeploymentOrchestrationStatus.READY_FOR_HUMAN,
                    persisted,
                    "The deployed version regressed on held-out evaluation.",
                    result.reason,
                )
            event = CampaignEvent(
                event_id=(
                    f"retention-complete-{snapshot.state.generation}-"
                    f"{intent.idempotency_key[:16]}"
                ),
                kind=EventKind.RETENTION_COMPLETED,
                generation=snapshot.state.generation,
                occurred_at=self._clock(),
                payload={"retained": True},
            )
            state = _campaign_advance(
                request.issue_number,
                snapshot.state,
                event,
            )
            result_record = _evaluation_result_record(state, result)
            cleanup = _success_cleanup_records(
                snapshot,
                state,
                plan,
                intent,
                result,
            )
            persisted = self._ledger.commit(
                request.repository_root,
                issue_number=request.issue_number,
                expected_revision=snapshot.revision,
                state=state,
                inbox=(event,),
                outbox=(result_record, *cleanup),
            )
        except StateRefConflictError:
            return DeploymentOrchestrationResult(
                DeploymentOrchestrationStatus.CONFLICT,
                snapshot,
                "Deployment state changed concurrently.",
                "state_ref_conflict",
            )
        except (KeyError, TypeError, ValueError):
            return self._block(
                request,
                snapshot,
                "post_deploy_result_mismatch",
                "The held-out evaluation result did not match its intent.",
            )
        except StateRefError:
            return DeploymentOrchestrationResult(
                DeploymentOrchestrationStatus.FAILED,
                snapshot,
                "Post-deployment evaluation state could not be persisted.",
                "post_deploy_state_persist_failed",
            )
        except RuntimeError:
            return self._block(
                request,
                snapshot,
                "post_deploy_evaluator_unavailable",
                "The pinned held-out evaluation was unavailable.",
            )
        return DeploymentOrchestrationResult(
            DeploymentOrchestrationStatus.COMPLETE,
            persisted,
            "Deployment retained the selected improvement.",
        )

    def _retry(
        self,
        request: DeploymentOrchestrationRequest,
        snapshot: StateRefSnapshot,
        plan: DeploymentPlan,
        prior: DeploymentWorkflowIntent,
        prior_state: DeploymentWorkflowRunState,
    ) -> DeploymentOrchestrationResult:
        retry = DeploymentWorkflowIntent(
            effect_id=_deployment_effect_id(
                prior.binding,
                prior.attempt + 1,
            ),
            attempt=prior.attempt + 1,
            binding=prior.binding,
            workflow=prior.workflow,
            planned_at=self._clock(),
            timeout_seconds=plan.timeout_seconds,
        )
        record = _intent_record(snapshot, retry)
        dashboard = OutboxRecord(
            record_id=(
                f"deployment-retry-{retry.binding.generation}-"
                f"{retry.attempt}-{retry.binding.binding_sha256[:16]}"
            ),
            kind="deployment_retry_planned",
            generation=snapshot.state.generation,
            sequence=snapshot.state.sequence,
            payload={
                "attempt": retry.attempt,
                "binding_sha256": retry.binding.binding_sha256,
                "candidate_id": retry.binding.candidate_id,
                "effect_id": retry.effect_id,
                "issue_number": retry.binding.issue_number,
                "reason": f"workflow_{prior_state.value}",
                "status": "retrying",
            },
        )
        try:
            persisted = self._ledger.commit(
                request.repository_root,
                issue_number=request.issue_number,
                expected_revision=snapshot.revision,
                state=snapshot.state,
                outbox=(record, dashboard),
            )
        except StateRefConflictError:
            return DeploymentOrchestrationResult(
                DeploymentOrchestrationStatus.CONFLICT,
                snapshot,
                "Deployment state changed concurrently.",
                "state_ref_conflict",
            )
        except (StateRefError, TypeError, ValueError):
            return DeploymentOrchestrationResult(
                DeploymentOrchestrationStatus.FAILED,
                snapshot,
                "Deployment retry intent could not be persisted.",
                "deployment_retry_persist_failed",
            )
        return DeploymentOrchestrationResult(
            DeploymentOrchestrationStatus.RETRYING,
            persisted,
            "A bounded deployment workflow retry was persisted.",
            f"deployment_workflow_{prior_state.value}",
        )

    def _block(
        self,
        request: DeploymentOrchestrationRequest,
        snapshot: StateRefSnapshot,
        reason: str,
        summary: str,
    ) -> DeploymentOrchestrationResult:
        _identifier(reason, "reason")
        event = CampaignEvent(
            event_id=(
                f"deployment-failed-{snapshot.state.generation}-{reason}"
            ),
            kind=EventKind.DEPLOYMENT_FAILED,
            generation=snapshot.state.generation,
            occurred_at=self._clock(),
            payload={"reason": reason},
        )
        try:
            state = _campaign_advance(
                request.issue_number,
                snapshot.state,
                event,
            )
            ready = OutboxRecord(
                record_id=event.event_id,
                kind="deployment_ready_for_human",
                generation=state.generation,
                sequence=state.sequence,
                payload={
                    "disposition": "wait",
                    "issue_number": request.issue_number,
                    "next_action": "inspect_and_retry_deployment",
                    "phase": CampaignPhase.BLOCKED.value,
                    "reason": reason,
                    "status": "ready_for_human",
                },
            )
            label = _ready_for_human_label(state)
            persisted = self._ledger.commit(
                request.repository_root,
                issue_number=request.issue_number,
                expected_revision=snapshot.revision,
                state=state,
                inbox=(event,),
                outbox=(ready, label),
            )
        except StateRefConflictError:
            return DeploymentOrchestrationResult(
                DeploymentOrchestrationStatus.CONFLICT,
                snapshot,
                "Deployment state changed concurrently.",
                "state_ref_conflict",
            )
        except (StateRefError, TypeError, ValueError):
            return DeploymentOrchestrationResult(
                DeploymentOrchestrationStatus.FAILED,
                snapshot,
                "Deployment failure state could not be persisted.",
                "deployment_failure_persist_failed",
            )
        return DeploymentOrchestrationResult(
            DeploymentOrchestrationStatus.READY_FOR_HUMAN,
            persisted,
            summary,
            reason,
        )


def _plan_mismatch(
    request: DeploymentOrchestrationRequest,
    state: CampaignState,
    plan: DeploymentPlan,
) -> str | None:
    if plan.issue_number != request.issue_number:
        return "deployment_issue_mismatch"
    if plan.generation != state.generation:
        return "deployment_generation_mismatch"
    return None


def _selected_candidate_binding(
    snapshot: StateRefSnapshot,
) -> CandidateBinding:
    selected_id = snapshot.state.selected_candidate_id
    bindings = tuple(
        binding
        for binding in candidate_worker_bindings(snapshot)
        if binding.candidate_id == selected_id
    )
    if len(bindings) != 1:
        raise ValueError("selected candidate binding is unavailable")
    return bindings[0]


def _verified_deployment_binding(
    snapshot: StateRefSnapshot,
    candidate: CandidateBinding,
    selection: DeploymentSelectionSnapshot,
    plan: DeploymentPlan,
) -> DeploymentBinding:
    state = snapshot.state
    selected_records = tuple(
        record
        for record in snapshot.outbox
        if (
            record.kind == "candidate_selection_recorded"
            and record.generation == state.generation
            and record.payload.get("candidate_id") == candidate.candidate_id
        )
    )
    if len(selected_records) != 1:
        raise ValueError("candidate selection record is unavailable")
    selected = selected_records[0].payload
    planned_records = tuple(
        record
        for record in snapshot.outbox
        if (
            record.kind == "applier_worker_issue_planned"
            and record.generation == state.generation
            and record.payload.get("binding_sha256")
            == candidate.binding_sha256
        )
    )
    if (
        len(planned_records) != 1
        or tuple(planned_records[0].payload.get("required_checks", ()))
        != plan.required_checks
    ):
        raise ValueError("candidate required-check policy changed")
    if (
        selection.binding != candidate
        or selection.candidate_pull_request_number
        != selected.get("pull_request_number")
        or selection.candidate_issue_number
        != selected.get("worker_issue_number")
        or selection.head_commit != selected.get("head_commit")
        or selection.merge_commit != selected.get("merge_commit")
        or selection.merge_commit != state.merge_commit
        or selection.merge_tree_sha != candidate.tree_sha
        or selected.get("tree_sha") != candidate.tree_sha
        or selected.get("binding_sha256") != candidate.binding_sha256
        or selection.merge_actor not in plan.allowed_merge_actors
    ):
        raise ValueError("selected deployment lineage changed")
    if any(
        selection.checks.get(check) != "success"
        for check in plan.required_checks
    ):
        raise ValueError("required deployment checks did not pass")
    return DeploymentBinding(
        issue_number=state.issue_number,
        generation=state.generation,
        spec_sha256=candidate.spec_sha256,
        candidate_pull_request_number=(
            selection.candidate_pull_request_number
        ),
        candidate_issue_number=selection.candidate_issue_number,
        candidate_id=candidate.candidate_id,
        draft_id=candidate.draft_id,
        merge_actor=selection.merge_actor,
        required_checks=plan.required_checks,
        merge_commit=selection.merge_commit,
        tree_sha=candidate.tree_sha,
        patch_sha256=candidate.patch_sha256,
        bundle_sha256=candidate.bundle_sha256,
        evidence_sha256=candidate.evidence_sha256,
    )


def _workflow_intent(
    plan: DeploymentPlan,
    binding: DeploymentBinding,
    *,
    attempt: int,
    planned_at: datetime,
) -> DeploymentWorkflowIntent:
    effect_id = _deployment_effect_id(binding, attempt)
    return DeploymentWorkflowIntent(
        effect_id=effect_id,
        attempt=attempt,
        binding=binding,
        workflow=plan.workflow,
        planned_at=planned_at,
        timeout_seconds=plan.timeout_seconds,
    )


def _intent_record(
    snapshot: StateRefSnapshot,
    intent: DeploymentWorkflowIntent,
) -> OutboxRecord:
    binding = intent.binding
    workflow = intent.workflow
    return OutboxRecord(
        record_id=intent.effect_id,
        kind="deployment_workflow_planned",
        generation=snapshot.state.generation,
        sequence=snapshot.state.sequence,
        payload={
            "attempt": intent.attempt,
            "binding_sha256": binding.binding_sha256,
            "bundle_sha256": binding.bundle_sha256,
            "candidate_id": binding.candidate_id,
            "candidate_issue_number": binding.candidate_issue_number,
            "candidate_pull_request_number": (
                binding.candidate_pull_request_number
            ),
            "deployment_client_id": workflow.deployment_client_id,
            "draft_id": binding.draft_id,
            "effect_id": intent.effect_id,
            "effect_kind": "deployment_workflow",
            "evidence_sha256": binding.evidence_sha256,
            "issue_number": binding.issue_number,
            "lineage_sha256": intent.lineage_sha256,
            "merge_actor": binding.merge_actor,
            "merge_commit": binding.merge_commit,
            "patch_sha256": binding.patch_sha256,
            "repository": workflow.repository,
            "repository_id": workflow.repository_id,
            "required_checks": list(binding.required_checks),
            "spec_sha256": binding.spec_sha256,
            "started_at": intent.planned_at.isoformat(),
            "timeout_seconds": intent.timeout_seconds,
            "tree_sha": binding.tree_sha,
            "workflow_actor": workflow.actor,
            "workflow_id": workflow.workflow_id,
            "workflow_path": workflow.path.as_posix(),
            "workflow_ref": workflow.ref,
            "workflow_trigger": workflow.trigger.value,
        },
    )


def deployment_workflow_intent(
    record: OutboxRecord,
) -> DeploymentWorkflowIntent:
    payload = record.payload
    if (
        record.kind != "deployment_workflow_planned"
        or payload.get("effect_id") != record.record_id
        or payload.get("effect_kind") != "deployment_workflow"
    ):
        raise ValueError("deployment workflow intent metadata is invalid")
    binding = DeploymentBinding(
        issue_number=int(payload["issue_number"]),
        generation=record.generation,
        spec_sha256=str(payload["spec_sha256"]),
        candidate_pull_request_number=int(
            payload["candidate_pull_request_number"]
        ),
        candidate_issue_number=int(payload["candidate_issue_number"]),
        candidate_id=str(payload["candidate_id"]),
        draft_id=str(payload["draft_id"]),
        merge_actor=str(payload["merge_actor"]),
        required_checks=tuple(
            str(item) for item in payload["required_checks"]
        ),
        merge_commit=str(payload["merge_commit"]),
        tree_sha=str(payload["tree_sha"]),
        patch_sha256=str(payload["patch_sha256"]),
        bundle_sha256=str(payload["bundle_sha256"]),
        evidence_sha256=str(payload["evidence_sha256"]),
    )
    if payload.get("binding_sha256") != binding.binding_sha256:
        raise ValueError("deployment binding digest is invalid")
    workflow = DeploymentWorkflowIdentity(
        repository=str(payload["repository"]),
        repository_id=int(payload["repository_id"]),
        path=Path(str(payload["workflow_path"])),
        ref=str(payload["workflow_ref"]),
        trigger=DeploymentTrigger(str(payload["workflow_trigger"])),
        workflow_id=int(payload["workflow_id"]),
        actor=str(payload["workflow_actor"]),
        deployment_client_id=str(payload["deployment_client_id"]),
    )
    intent = DeploymentWorkflowIntent(
        effect_id=record.record_id,
        attempt=int(payload["attempt"]),
        binding=binding,
        workflow=workflow,
        planned_at=_datetime(str(payload["started_at"])),
        timeout_seconds=int(payload["timeout_seconds"]),
    )
    if payload.get("lineage_sha256") != intent.lineage_sha256:
        raise ValueError("deployment lineage digest is invalid")
    return intent


def _workflow_results(
    snapshot: StateRefSnapshot,
    intent: DeploymentWorkflowIntent,
) -> tuple[DeploymentWorkflowResult, ...]:
    results: dict[tuple[object, ...], DeploymentWorkflowResult] = {}
    for event in snapshot.inbox:
        if (
            event.kind is not EventKind.DEPLOYMENT_WORKFLOW_OBSERVED
            or event.generation != intent.binding.generation
            or event.payload.get("effect_id") != intent.effect_id
        ):
            continue
        result = deployment_workflow_result_from_event(event)
        result.require_matches(intent)
        key = (
            result.run_id,
            result.state,
            result.conclusion,
            result.result_id,
        )
        results[key] = result
    return tuple(results.values())


def _bridge_recorded_workflow_results(
    snapshot: StateRefSnapshot,
    intent: DeploymentWorkflowIntent,
) -> tuple[DeploymentWorkflowResult, ...]:
    publications = tuple(
        record
        for record in snapshot.outbox
        if (
            record.kind == "deployment_publication_observed"
            and record.generation == intent.binding.generation
            and record.payload.get("effect_id") == intent.effect_id
            and record.payload.get("status")
            == DeploymentPublicationStatus.VERIFIED.value
        )
    )
    if not publications:
        return ()
    run_ids = {
        int(record.payload["run_id"]) for record in publications
    }
    if len(run_ids) != 1:
        raise ValueError("publication records reference competing runs")
    (run_id,) = tuple(run_ids)
    bindings = tuple(
        record
        for record in snapshot.outbox
        if (
            record.kind == "deployment_workflow_run_bound"
            and record.generation == intent.binding.generation
            and record.payload.get("effect_id") == intent.effect_id
            and record.payload.get("run_id") == run_id
        )
    )
    if len(bindings) != 1:
        raise ValueError("publication workflow run is not exactly bound")
    payload = bindings[0].payload
    if (
        payload.get("binding_sha256") != intent.binding.binding_sha256
        or payload.get("attempt") != intent.attempt
    ):
        raise ValueError("publication workflow binding changed")
    return (
        DeploymentWorkflowResult(
            effect_id=intent.effect_id,
            result_id=f"deployment-run-{run_id}-success",
            attempt=intent.attempt,
            binding=intent.binding,
            workflow=intent.workflow,
            run_id=run_id,
            run_url=str(payload["run_url"]),
            state=DeploymentWorkflowRunState.SUCCESS,
            conclusion="success",
            run_actor=str(payload["run_actor"]),
        ),
    )


def deployment_workflow_result_from_event(
    event: CampaignEvent,
) -> DeploymentWorkflowResult:
    if (
        event.kind is not EventKind.DEPLOYMENT_WORKFLOW_OBSERVED
        or set(event.payload) != _WORKFLOW_EVENT_FIELDS
    ):
        raise ValueError("campaign event is not a workflow observation")
    payload = event.payload
    binding = DeploymentBinding(
        issue_number=int(payload["issue_number"]),
        generation=event.generation,
        spec_sha256=str(payload["spec_sha256"]),
        candidate_pull_request_number=int(
            payload["candidate_pull_request_number"]
        ),
        candidate_issue_number=int(payload["candidate_issue_number"]),
        candidate_id=str(payload["candidate_id"]),
        draft_id=str(payload["draft_id"]),
        merge_actor=str(payload["merge_actor"]),
        required_checks=tuple(
            str(item) for item in payload["required_checks"]
        ),
        merge_commit=str(payload["merge_commit"]),
        tree_sha=str(payload["tree_sha"]),
        patch_sha256=str(payload["patch_sha256"]),
        bundle_sha256=str(payload["bundle_sha256"]),
        evidence_sha256=str(payload["evidence_sha256"]),
    )
    if (
        payload["binding_sha256"] != binding.binding_sha256
        or int(payload["issue_number"]) != binding.issue_number
    ):
        raise ValueError("workflow observation binding is invalid")
    workflow = DeploymentWorkflowIdentity(
        repository=str(payload["repository"]),
        repository_id=int(payload["repository_id"]),
        path=Path(str(payload["workflow_path"])),
        ref=str(payload["workflow_ref"]),
        trigger=DeploymentTrigger(str(payload["workflow_trigger"])),
        workflow_id=int(payload["workflow_id"]),
        actor=str(payload["workflow_actor"]),
        deployment_client_id=str(payload["deployment_client_id"]),
    )
    conclusion = str(payload["run_conclusion"])
    return DeploymentWorkflowResult(
        effect_id=str(payload["effect_id"]),
        result_id=str(payload["result_id"]),
        attempt=int(payload["attempt"]),
        binding=binding,
        workflow=workflow,
        run_id=int(payload["run_id"]),
        run_actor=str(payload["run_actor"]),
        run_url=str(payload["run_url"]),
        state=DeploymentWorkflowRunState(str(payload["run_status"])),
        conclusion=None if conclusion == "pending" else conclusion,
    )


def _effective_workflow_result(
    results: tuple[DeploymentWorkflowResult, ...],
) -> DeploymentWorkflowResult:
    run_ids = {result.run_id for result in results}
    if len(run_ids) != 1:
        raise ValueError("deployment attempt has competing workflow runs")
    rank = {
        DeploymentWorkflowRunState.QUEUED: 0,
        DeploymentWorkflowRunState.IN_PROGRESS: 1,
        DeploymentWorkflowRunState.SUCCESS: 2,
        DeploymentWorkflowRunState.FAILURE: 2,
        DeploymentWorkflowRunState.CANCELLED: 2,
        DeploymentWorkflowRunState.TIMED_OUT: 2,
    }
    terminal = {
        (result.state, result.conclusion)
        for result in results
        if rank[result.state] == 2
    }
    if len(terminal) > 1:
        raise ValueError("workflow terminal result changed")
    return max(results, key=lambda item: rank[item.state])


def _campaign_advance(
    issue_number: int,
    state: CampaignState,
    event: CampaignEvent,
) -> CampaignState:
    from foundry_opt.orchestration.campaign import OptimizationCampaign
    from foundry_opt.orchestration.models import AdvanceRequest

    return OptimizationCampaign().advance(
        AdvanceRequest(issue_number, state, (event,))
    ).state


def _publication_record(
    state: CampaignState,
    intent: DeploymentWorkflowIntent,
    result: DeploymentWorkflowResult,
    published: DeploymentPublishedVerification,
) -> OutboxRecord:
    assert published.deployment_version is not None
    assert published.source_sha256 is not None
    assert published.tree_sha is not None
    assert published.bundle_sha256 is not None
    assert published.merge_commit is not None
    assert published.lineage_sha256 is not None
    assert published.metadata_sha256 is not None
    assert published.portal_url is not None
    return OutboxRecord(
        record_id=(
            f"deployment-published-{state.generation}-"
            f"{published.deployment_version}-"
            f"{published.lineage_sha256[:16]}"
        ),
        kind="deployment_published_verified",
        generation=state.generation,
        sequence=state.sequence,
        payload={
            "binding_sha256": intent.binding.binding_sha256,
            "bundle_sha256": published.bundle_sha256,
            "candidate_id": intent.binding.candidate_id,
            "deployment_version": published.deployment_version,
            "effect_id": intent.effect_id,
            "issue_number": intent.binding.issue_number,
            "lineage_sha256": published.lineage_sha256,
            "merge_commit": published.merge_commit,
            "metadata_sha256": published.metadata_sha256,
            "portal_url": published.portal_url,
            "run_id": result.run_id,
            "run_url": result.run_url,
            "source_sha256": published.source_sha256,
            "tree_sha": published.tree_sha,
        },
    )


def _evaluation_intent_record(
    state: CampaignState,
    plan: DeploymentPlan,
    intent: DeploymentWorkflowIntent,
    published: DeploymentPublishedVerification,
) -> OutboxRecord:
    assert published.deployment_version is not None
    identity = hashlib.sha256(
        (
            f"{intent.lineage_sha256}:"
            f"{published.deployment_version}:"
            f"{plan.held_out_evaluation_id}:"
            f"{plan.evaluation_policy_sha256}"
        ).encode("ascii")
    ).hexdigest()
    effect_id = (
        f"post-deploy-eval-{state.generation}-{identity[:20]}"
    )
    return OutboxRecord(
        record_id=effect_id,
        kind="post_deployment_evaluation_planned",
        generation=state.generation,
        sequence=state.sequence,
        payload={
            "binding_sha256": intent.binding.binding_sha256,
            "bundle_sha256": intent.binding.bundle_sha256,
            "candidate_id": intent.binding.candidate_id,
            "candidate_issue_number": (
                intent.binding.candidate_issue_number
            ),
            "candidate_pull_request_number": (
                intent.binding.candidate_pull_request_number
            ),
            "deployment_client_id": (
                intent.workflow.deployment_client_id
            ),
            "deployment_effect_id": intent.effect_id,
            "deployment_version": published.deployment_version,
            "draft_id": intent.binding.draft_id,
            "effect_id": effect_id,
            "effect_kind": "post_deployment_evaluation",
            "evaluation_id": plan.held_out_evaluation_id,
            "evaluation_policy_sha256": plan.evaluation_policy_sha256,
            "evidence_sha256": intent.binding.evidence_sha256,
            "idempotency_key": identity,
            "issue_number": intent.binding.issue_number,
            "lineage_sha256": intent.lineage_sha256,
            "merge_actor": intent.binding.merge_actor,
            "merge_commit": intent.binding.merge_commit,
            "patch_sha256": intent.binding.patch_sha256,
            "repository": intent.workflow.repository,
            "repository_id": intent.workflow.repository_id,
            "required_checks": list(intent.binding.required_checks),
            "spec_sha256": intent.binding.spec_sha256,
            "tree_sha": intent.binding.tree_sha,
            "workflow_actor": intent.workflow.actor,
            "workflow_id": intent.workflow.workflow_id,
            "workflow_path": intent.workflow.path.as_posix(),
            "workflow_ref": intent.workflow.ref,
            "workflow_trigger": intent.workflow.trigger.value,
        },
    )


def _datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError("datetime is invalid") from error
    if parsed.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return parsed


def _run_id_from_url(value: str) -> int:
    try:
        parsed = urlsplit(value)
        parts = tuple(part for part in parsed.path.split("/") if part)
    except ValueError as error:
        raise ValueError("workflow run URL is invalid") from error
    if (
        parsed.hostname != "github.com"
        or len(parts) != 5
        or parts[2:4] != ("actions", "runs")
        or not parts[4].isdigit()
    ):
        raise ValueError("workflow run URL is invalid")
    return int(parts[4])


def post_deployment_evaluation_intent(
    record: OutboxRecord,
) -> PostDeploymentEvaluationIntent:
    payload = record.payload
    if (
        record.kind != "post_deployment_evaluation_planned"
        or payload.get("effect_id") != record.record_id
        or payload.get("effect_kind") != "post_deployment_evaluation"
    ):
        raise ValueError("post-deployment evaluation intent is invalid")
    binding = DeploymentBinding(
        issue_number=int(payload["issue_number"]),
        generation=record.generation,
        spec_sha256=str(payload["spec_sha256"]),
        candidate_pull_request_number=int(
            payload["candidate_pull_request_number"]
        ),
        candidate_issue_number=int(payload["candidate_issue_number"]),
        candidate_id=str(payload["candidate_id"]),
        draft_id=str(payload["draft_id"]),
        merge_actor=str(payload["merge_actor"]),
        required_checks=tuple(
            str(item) for item in payload["required_checks"]
        ),
        merge_commit=str(payload["merge_commit"]),
        tree_sha=str(payload["tree_sha"]),
        patch_sha256=str(payload["patch_sha256"]),
        bundle_sha256=str(payload["bundle_sha256"]),
        evidence_sha256=str(payload["evidence_sha256"]),
    )
    if payload.get("binding_sha256") != binding.binding_sha256:
        raise ValueError("post-deployment binding digest is invalid")
    workflow = DeploymentWorkflowIdentity(
        repository=str(payload["repository"]),
        repository_id=int(payload["repository_id"]),
        path=Path(str(payload["workflow_path"])),
        ref=str(payload["workflow_ref"]),
        trigger=DeploymentTrigger(str(payload["workflow_trigger"])),
        workflow_id=int(payload["workflow_id"]),
        actor=str(payload["workflow_actor"]),
        deployment_client_id=str(payload["deployment_client_id"]),
    )
    return PostDeploymentEvaluationIntent(
        effect_id=record.record_id,
        deployment_effect_id=str(payload["deployment_effect_id"]),
        binding=binding,
        workflow=workflow,
        deployment_version=int(payload["deployment_version"]),
        evaluation_id=str(payload["evaluation_id"]),
        evaluation_policy_sha256=str(
            payload["evaluation_policy_sha256"]
        ),
        idempotency_key=str(payload["idempotency_key"]),
    )


def deployment_cleanup_effect(
    record: OutboxRecord,
) -> DeploymentCleanupEffect:
    try:
        kind = DeploymentCleanupKind(record.kind)
    except ValueError as error:
        raise ValueError("record is not a deployment cleanup effect") from error
    payload = record.payload
    if payload.get("effect_id") != record.record_id:
        raise ValueError("cleanup effect identity is invalid")
    if kind in {
        DeploymentCleanupKind.CANDIDATE_ISSUE_CLOSE,
        DeploymentCleanupKind.CANDIDATE_ISSUE_SUPERSEDE,
    }:
        target = int(payload["worker_issue_number"])
    elif kind in {
        DeploymentCleanupKind.CANDIDATE_PR_SUPERSEDE,
        DeploymentCleanupKind.CAMPAIGN_PR_CLOSE,
        DeploymentCleanupKind.OPTIMIZATION_PR_CLOSE,
    }:
        target = int(payload["pull_request_number"])
    else:
        target = int(payload["issue_number"])
    dependencies_value = payload.get("depends_on_effect_ids", ())
    if not isinstance(dependencies_value, (list, tuple)):
        raise ValueError("cleanup dependencies are invalid")
    return DeploymentCleanupEffect(
        effect_id=record.record_id,
        kind=kind,
        generation=record.generation,
        sequence=record.sequence,
        issue_number=int(payload["issue_number"]),
        target_number=target,
        reason=str(payload["reason"]),
        candidate_id=(
            str(payload["candidate_id"])
            if isinstance(payload.get("candidate_id"), str)
            else None
        ),
        dependencies=tuple(str(item) for item in dependencies_value),
        metadata=payload,
    )


def deployment_published_verification_record(
    result: DeploymentPublishedVerification,
    *,
    sequence: int,
) -> OutboxRecord:
    payload: dict[str, object] = {
        "binding_sha256": result.intent.binding.binding_sha256,
        "effect_id": result.intent.effect_id,
        "issue_number": result.intent.binding.issue_number,
        "lineage_sha256": result.intent.lineage_sha256,
        "run_id": result.workflow_result.run_id,
        "run_url": result.workflow_result.run_url,
        "status": result.status.value,
    }
    if result.reason is not None:
        payload["reason"] = result.reason
    if result.status is DeploymentPublicationStatus.VERIFIED:
        assert result.deployment_version is not None
        assert result.source_sha256 is not None
        assert result.tree_sha is not None
        assert result.bundle_sha256 is not None
        assert result.merge_commit is not None
        assert result.lineage_sha256 is not None
        assert result.metadata_sha256 is not None
        assert result.portal_url is not None
        payload.update(
            {
                "bundle_sha256": result.bundle_sha256,
                "deployment_version": result.deployment_version,
                "merge_commit": result.merge_commit,
                "metadata_sha256": result.metadata_sha256,
                "portal_url": result.portal_url,
                "source_sha256": result.source_sha256,
                "tree_sha": result.tree_sha,
            }
        )
    version = result.deployment_version or 0
    return OutboxRecord(
        record_id=(
            f"publication-{result.intent.binding.generation}-"
            f"{result.intent.binding.binding_sha256[:16]}-"
            f"{result.status.value}-{version}"
        ),
        kind="deployment_publication_observed",
        generation=result.intent.binding.generation,
        sequence=sequence,
        payload=payload,
    )


def deployment_published_verification_from_record(
    record: OutboxRecord,
    intent: DeploymentWorkflowIntent,
    workflow_result: DeploymentWorkflowResult,
) -> DeploymentPublishedVerification:
    payload = record.payload
    if (
        record.kind != "deployment_publication_observed"
        or payload.get("effect_id") != intent.effect_id
        or payload.get("binding_sha256")
        != intent.binding.binding_sha256
        or payload.get("lineage_sha256") != intent.lineage_sha256
        or payload.get("run_id") != workflow_result.run_id
        or payload.get("run_url") != workflow_result.run_url
    ):
        raise ValueError("deployment publication record is invalid")
    status = DeploymentPublicationStatus(str(payload["status"]))
    if status is DeploymentPublicationStatus.VERIFIED:
        return DeploymentPublishedVerification(
            status,
            intent,
            workflow_result,
            deployment_version=int(payload["deployment_version"]),
            source_sha256=str(payload["source_sha256"]),
            tree_sha=str(payload["tree_sha"]),
            bundle_sha256=str(payload["bundle_sha256"]),
            merge_commit=str(payload["merge_commit"]),
            lineage_sha256=str(payload["lineage_sha256"]),
            metadata_sha256=str(payload["metadata_sha256"]),
            portal_url=str(payload["portal_url"]),
        )
    return DeploymentPublishedVerification(
        status,
        intent,
        workflow_result,
        reason=str(payload.get("reason") or "published_version_pending"),
    )


def _evaluation_result_record(
    state: CampaignState,
    result: PostDeploymentEvaluationResult,
) -> OutboxRecord:
    payload = _evaluation_result_payload(result)
    return OutboxRecord(
        record_id=f"{result.intent.effect_id}-result",
        kind="post_deployment_evaluation_result",
        generation=state.generation,
        sequence=state.sequence,
        payload=payload,
    )


def _evaluation_result_payload(
    result: PostDeploymentEvaluationResult,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "baseline_metrics": dict(result.baseline_metrics),
        "binding_sha256": result.intent.binding_sha256,
        "candidate_id": result.intent.binding.candidate_id,
        "deployed_metrics": dict(result.deployed_metrics),
        "deployment_version": result.intent.deployment_version,
        "draft_metrics": dict(result.selected_draft_metrics),
        "effect_id": result.intent.effect_id,
        "evaluation_id": result.intent.evaluation_id,
        "evaluation_policy_sha256": (
            result.intent.evaluation_policy_sha256
        ),
        "issue_number": result.intent.binding.issue_number,
        "result_id": result.result_id,
        "retained": (
            result.status
            is PostDeploymentEvaluationStatus.RETAINED_IMPROVEMENT
        ),
        "status": result.status.value,
    }
    if result.reason is not None:
        payload["reason"] = result.reason
    return payload


def post_deployment_evaluation_observation_record(
    result: PostDeploymentEvaluationResult,
    *,
    sequence: int,
) -> OutboxRecord:
    return OutboxRecord(
        record_id=f"{result.intent.effect_id}-observed",
        kind="post_deployment_evaluation_observed",
        generation=result.intent.binding.generation,
        sequence=sequence,
        payload=_evaluation_result_payload(result),
    )


def post_deployment_evaluation_result_from_record(
    record: OutboxRecord,
    intent: PostDeploymentEvaluationIntent,
) -> PostDeploymentEvaluationResult:
    payload = record.payload
    if (
        record.kind != "post_deployment_evaluation_observed"
        or payload.get("effect_id") != intent.effect_id
        or payload.get("binding_sha256") != intent.binding_sha256
        or payload.get("deployment_version") != intent.deployment_version
        or payload.get("evaluation_id") != intent.evaluation_id
        or payload.get("evaluation_policy_sha256")
        != intent.evaluation_policy_sha256
    ):
        raise ValueError("post-deployment evaluation observation is invalid")
    status = PostDeploymentEvaluationStatus(str(payload["status"]))
    return PostDeploymentEvaluationResult(
        result_id=str(payload["result_id"]),
        intent=intent,
        status=status,
        reason=(
            str(payload["reason"])
            if isinstance(payload.get("reason"), str)
            else None
        ),
        baseline_metrics=dict(payload["baseline_metrics"]),
        selected_draft_metrics=dict(payload["draft_metrics"]),
        deployed_metrics=dict(payload["deployed_metrics"]),
    )


def _ready_for_human_record(
    state: CampaignState,
    reason: str,
) -> OutboxRecord:
    return OutboxRecord(
        record_id=f"deployment-failed-{state.generation}-{reason}",
        kind="deployment_ready_for_human",
        generation=state.generation,
        sequence=state.sequence,
        payload={
            "disposition": "wait",
            "issue_number": state.issue_number,
            "next_action": "inspect_and_retry_deployment",
            "phase": CampaignPhase.BLOCKED.value,
            "reason": reason,
            "status": "ready_for_human",
        },
    )


def _ready_for_human_label(state: CampaignState) -> OutboxRecord:
    return OutboxRecord(
        record_id=f"deployment-ready-human-label-{state.generation}",
        kind="label_add",
        generation=state.generation,
        sequence=state.sequence,
        payload={
            "issue_number": state.issue_number,
            "label": "ready-for-human",
        },
    )


def _success_cleanup_records(
    snapshot: StateRefSnapshot,
    state: CampaignState,
    plan: DeploymentPlan,
    intent: PostDeploymentEvaluationIntent,
    result: PostDeploymentEvaluationResult,
) -> tuple[OutboxRecord, ...]:
    digest = intent.idempotency_key[:16]
    publications = tuple(
        record
        for record in snapshot.outbox
        if (
            record.kind == "deployment_published_verified"
            and record.generation == state.generation
            and record.payload.get("candidate_id")
            == intent.binding.candidate_id
        )
    )
    if len(publications) != 1:
        raise ValueError("verified deployment publication is unavailable")
    publication = publications[0].payload
    common = {
        "bundle_sha256": intent.binding.bundle_sha256,
        "candidate_id": intent.binding.candidate_id,
        "deployed_metrics": dict(result.deployed_metrics),
        "deployment_version": intent.deployment_version,
        "draft_metrics": dict(result.selected_draft_metrics),
        "evidence_sha256": intent.binding.evidence_sha256,
        "issue_number": intent.binding.issue_number,
        "lineage_sha256": publication["lineage_sha256"],
        "merge_actor": intent.binding.merge_actor,
        "merge_commit": intent.binding.merge_commit,
        "metadata_sha256": publication["metadata_sha256"],
        "patch_sha256": intent.binding.patch_sha256,
        "portal_url": publication["portal_url"],
        "run_id": publication["run_id"],
        "run_url": publication["run_url"],
        "source_sha256": publication["source_sha256"],
        "spec_sha256": intent.binding.spec_sha256,
        "status": "completed",
        "tree_sha": intent.binding.tree_sha,
        "required_checks": list(intent.binding.required_checks),
    }
    records: list[OutboxRecord] = []
    records.append(
        OutboxRecord(
            record_id=f"deployment-complete-label-{state.generation}",
            kind="label_remove",
            generation=state.generation,
            sequence=state.sequence,
            payload={
                "issue_number": state.issue_number,
                "label": "ready-for-human",
            },
        )
    )
    worker_issues = {
        str(record.payload["candidate_id"]): int(
            record.payload["worker_issue_number"]
        )
        for record in snapshot.outbox
        if (
            record.kind == "applier_worker_issue_succeeded"
            and record.generation == state.generation
            and isinstance(record.payload.get("candidate_id"), str)
            and type(record.payload.get("worker_issue_number")) is int
        )
    }
    markers = {
        str(record.payload["candidate_id"]): str(record.payload["marker"])
        for record in snapshot.outbox
        if (
            record.kind == "applier_worker_issue_planned"
            and record.generation == state.generation
            and isinstance(record.payload.get("candidate_id"), str)
            and isinstance(record.payload.get("marker"), str)
        )
    }
    pull_requests = {
        str(record.payload["candidate_id"]): int(
            record.payload["pull_request_number"]
        )
        for record in snapshot.outbox
        if (
            record.generation == state.generation
            and isinstance(record.payload.get("candidate_id"), str)
            and type(record.payload.get("pull_request_number")) is int
        )
    }
    existing_candidate_effects = {
        (
            record.kind,
            str(record.payload.get("candidate_id", "")),
        )
        for record in snapshot.outbox
        if record.generation == state.generation
        and record.payload.get("effect_id") == record.record_id
        and record.kind
        in {
            "candidate_issue_close_planned",
            "candidate_issue_supersede_planned",
            "candidate_pr_supersede_planned",
        }
    }
    for candidate_id, issue_number in sorted(worker_issues.items()):
        selected = candidate_id == intent.binding.candidate_id
        kind = (
            "candidate_issue_close_planned"
            if selected
            else "candidate_issue_supersede_planned"
        )
        if (kind, candidate_id) not in existing_candidate_effects:
            effect_id = (
                f"cleanup-issue-{state.generation}-"
                f"{_identity(candidate_id, str(issue_number))[:20]}"
            )
            payload: dict[str, object] = {
                "candidate_id": candidate_id,
                "effect_id": effect_id,
                "issue_number": state.issue_number,
                "reason": (
                    "selected_candidate_deployed"
                    if selected
                    else "candidate_selected_elsewhere"
                ),
                "worker_issue_number": issue_number,
            }
            marker = markers.get(candidate_id)
            if marker is not None:
                payload["marker"] = marker
            records.append(
                OutboxRecord(
                    record_id=effect_id,
                    kind=kind,
                    generation=state.generation,
                    sequence=state.sequence,
                    payload=payload,
                )
            )
        if (
            not selected
            and candidate_id in pull_requests
            and (
                "candidate_pr_supersede_planned",
                candidate_id,
            )
            not in existing_candidate_effects
        ):
            number = pull_requests[candidate_id]
            effect_id = (
                f"cleanup-pr-{state.generation}-"
                f"{_identity(candidate_id, str(number))[:20]}"
            )
            payload = {
                "candidate_id": candidate_id,
                "effect_id": effect_id,
                "issue_number": state.issue_number,
                "pull_request_number": number,
                "reason": "candidate_selected_elsewhere",
            }
            marker = markers.get(candidate_id)
            if marker is not None:
                payload["marker"] = marker
            records.append(
                OutboxRecord(
                    record_id=effect_id,
                    kind="candidate_pr_supersede_planned",
                    generation=state.generation,
                    sequence=state.sequence,
                    payload=payload,
                )
            )
    for kind, number in (
        ("campaign_pr_close_planned", plan.campaign_pull_request_number),
        (
            "optimization_pr_close_planned",
            plan.optimization_pull_request_number,
        ),
    ):
        if number is None:
            continue
        effect_id = f"{kind.removesuffix('_planned')}-{number}"
        records.append(
            OutboxRecord(
                record_id=effect_id,
                kind=kind,
                generation=state.generation,
                sequence=state.sequence,
                payload={
                    "candidate_id": intent.binding.candidate_id,
                    "effect_id": effect_id,
                    "issue_number": state.issue_number,
                    "pull_request_number": number,
                    "reason": "optimization_completed",
                },
            )
        )
    dashboard = OutboxRecord(
        record_id=f"final-dashboard-{state.generation}-{digest}",
        kind="deployment_final_dashboard",
        generation=state.generation,
        sequence=state.sequence,
        payload={
            **common,
            "baseline_metrics": dict(result.baseline_metrics),
            "disposition": "complete",
            "effect_id": f"final-dashboard-{state.generation}-{digest}",
            "phase": CampaignPhase.COMPLETED.value,
            "reason": "retained_improvement",
        },
    )
    comment = OutboxRecord(
        record_id=f"final-comment-{state.generation}-{digest}",
        kind="root_comment_final_planned",
        generation=state.generation,
        sequence=state.sequence,
        payload={
            **common,
            "baseline_metrics": dict(result.baseline_metrics),
            "effect_id": f"final-comment-{state.generation}-{digest}",
            "reason": "retained_improvement",
        },
    )
    records.extend((dashboard, comment))
    dependencies = tuple(
        dict.fromkeys(
            record.record_id
            for record in records
            if record.kind
            not in {
                "label_add",
                "label_remove",
            }
        )
    )
    records.append(
        OutboxRecord(
            record_id=f"root-close-{state.generation}-{digest}",
            kind="root_issue_close_planned",
            generation=state.generation,
            sequence=state.sequence,
            payload={
                **common,
                "depends_on_effect_ids": list(dependencies),
                "effect_id": f"root-close-{state.generation}-{digest}",
                "reason": "retained_improvement",
            },
        )
    )
    return tuple(records)


def _deployment_effect_id(
    binding: DeploymentBinding,
    attempt: int,
) -> str:
    return (
        f"deployment-{binding.generation}-"
        f"{binding.binding_sha256[:20]}-a{attempt}"
    )


def _identity(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
