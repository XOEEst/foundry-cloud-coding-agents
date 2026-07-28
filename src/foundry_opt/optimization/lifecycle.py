"""Issue-driven optimization APPLY and RECONCILE lifecycle services.

This module owns the two terminal optimization phases that the
:class:`~foundry_opt.optimization.runner.IssueOptimizationRunner` delegates to
its ``apply_service`` and ``reconcile_service`` seams:

* ``APPLY`` loads the finalized campaign's parent issue, campaign publication,
  and the target candidate issue's *exact* artifact references, constructs a
  :class:`~foundry_opt.github_workflow.CandidateApplicationRequest` carrying a
  human or autopilot pull-request policy derived from the approved spec, and
  invokes :func:`~foundry_opt.github_workflow.verify_and_apply_candidate`
  against the exact-patch applier and campaign gateway. ``--verify-only``
  re-verifies an already-published candidate pull request without performing
  any writes. Parent and child issue comments are idempotent.

* ``RECONCILE`` loads the approved spec, the finalized campaign report, and
  each eligible candidate pull request's checks/rank, then delegates the merge
  decision to :func:`~foundry_opt.github_workflow.reconcile_candidates`. In
  human decision mode it reports the ranked eligible pull requests and waits.
  Autopilot requires an explicit policy, separate merge and deployment
  capabilities/actor, branch/ruleset compatibility, all required checks, and
  human spec approval for trace-derived assets. After the selected pull
  request merges it builds an
  :class:`~foundry_opt.deployment.OptimizationDeploymentLineage`, detects the
  configured deployment workflow, observes/verifies the deployment through an
  injected coordinator seam, runs an injected post-deployment evaluation seam,
  updates the parent issue with the aggregate identifiers/hashes/links, closes
  the superseded candidate issues and the temporary campaign pull request, and
  only closes the parent issue once a retained improvement is confirmed.

Every seam is a live production adapter. The *live* deployment and
post-deployment evaluation bindings are the Azure-OIDC
:class:`~foundry_opt.adapters.optimization_deployment.LiveDeploymentCoordinator`
and :class:`~foundry_opt.adapters.post_deploy_evaluation.LivePostDeployEvaluator`
that :func:`build_lifecycle_services` wires by default; a test may still inject
a fake (or the ``_Unavailable*`` placeholders) directly. When a live binding's
precondition is missing at call time the adapter raises the typed
:class:`~foundry_opt.optimization.runner.CapabilityUnavailableError` so the
service surfaces an honest ``blocked`` result rather than fabricating success.
Every partial mutation is recorded in typed, atomically persisted
:class:`LifecycleState` so a retried invocation resumes instead of repeating
writes.

The :func:`build_lifecycle_services` factory assembles the production adapters,
and :func:`foundry_opt.optimization.production.build_issue_optimization_dependencies`
threads the resulting services (sharing the single Azure OIDC credential
provider, command runner, and campaign state store) onto the runner
dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol

import yaml

from foundry_opt.campaign.models import CandidateArtifact
from foundry_opt.campaign.protocols import CampaignRepository, Clock
from foundry_opt.campaign.state import (
    CampaignState,
    CampaignStateStore,
    FinalizedPublication,
)
from foundry_opt.config.models import AutomationPolicy, OptimizerConfig
from foundry_opt.deployment import (
    DeploymentLineageMismatchError,
    DeploymentTrigger,
    DeploymentWorkflow,
    OptimizationDeploymentLineage,
    detect_deployment_workflow,
    optimization_deployment_lineage_sha256,
)
from foundry_opt.github_workflow import (
    CandidateApplicationRequest,
    CandidateApplicationStatus,
    CandidateMergeMode,
    CandidatePullRequestPolicy,
    CandidateReconcileEntry,
    CandidateReconcileRequest,
    CandidateReconcileStatus,
    GitHubCapabilities,
    GitHubPermissionReport,
    PullRequestReference,
    reconcile_candidates,
    verify_and_apply_candidate,
)
from foundry_opt.github_workflow.candidate import (
    CandidateGateway,
    PatchApplier,
)
from foundry_opt.github_workflow.errors import GitHubPermissionDeniedError
from foundry_opt.optimization.commands import (
    OptimizeCommandRequest,
    OptimizeCommandResult,
    OptimizeCommandStatus,
    OptimizePhase,
)
from foundry_opt.optimization.models import (
    ApprovalGate,
    DecisionMode,
    DeploymentMode,
    OptimizationSpec,
    OptimizationSpecApproval,
    approve_optimization_spec,
    spec_is_autopilot_eligible,
)
from foundry_opt.optimization.runner import CapabilityUnavailableError
from foundry_opt.optimization.specification import spec_file_path
from foundry_opt.preflight.interfaces import CommandRunner
from foundry_opt.preflight.redaction import redact


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_TREE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class LifecycleStateError(RuntimeError):
    """The durable lifecycle state is unreadable, tampered, or unsafe.

    Raised (never swallowed into a fresh state) so a corrupt or maliciously
    replaced state file fails the invocation closed rather than silently
    repeating already-completed merges, deployments, or issue closures.
    """


def _campaign_id(issue_number: int) -> str:
    # Mirrors foundry_opt.optimization.runner._campaign_id so the lifecycle
    # loads the same durable campaign state the runner persisted.
    return f"issue-{issue_number}"


# ---------------------------------------------------------------------------
# Issue / pull-request gateway
# ---------------------------------------------------------------------------


class LifecycleIssueGateway(Protocol):
    """Idempotent parent/child issue and pull-request mutations.

    The production adapter is
    :class:`~foundry_opt.adapters.github_campaign.GhCampaignGateway`; every
    method is only invoked once per logical mutation because the lifecycle
    records completed mutations in :class:`LifecycleState`.
    """

    def comment_issue(
        self,
        repository_root: Path,
        issue_number: int,
        body: str,
    ) -> None: ...

    def update_issue_body(
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


# ---------------------------------------------------------------------------
# Reconcile gateway
# ---------------------------------------------------------------------------


class LifecycleReconcileGateway(Protocol):
    """Merge-decision and deployment-dispatch gateway for reconciliation.

    A structural superset of
    :class:`~foundry_opt.github_workflow.reconcile.CandidateReconcileGateway`
    so it can be passed directly to
    :func:`~foundry_opt.github_workflow.reconcile_candidates`, extended with
    the lookups the lifecycle needs to build the ranked candidate slate and the
    post-merge deployment lineage.
    """

    def verify_permissions(
        self,
        required: GitHubCapabilities,
    ) -> GitHubPermissionReport: ...

    def branch_protection_allows(
        self,
        repository_root: Path,
        pull_request: PullRequestReference,
        actor: str,
    ) -> bool: ...

    def merge_pull_request(
        self,
        repository_root: Path,
        pull_request_number: int,
        expected_head_commit: str,
        actor: str,
    ) -> None: ...

    def dispatch_deployment(
        self,
        repository_root: Path,
        pull_request_number: int,
        expected_head_commit: str,
    ) -> None: ...

    def locate_candidate_pull_request(
        self,
        repository_root: Path,
        campaign_id: str,
        candidate_id: str,
    ) -> PullRequestReference | None: ...

    def candidate_checks(
        self,
        repository_root: Path,
        pull_request: PullRequestReference,
    ) -> Mapping[str, str]: ...

    def resolve_merge_commit(
        self,
        repository_root: Path,
        pull_request_number: int,
    ) -> str: ...

    def resolve_tree(
        self,
        repository_root: Path,
        commit: str,
    ) -> str | None: ...


# ---------------------------------------------------------------------------
# Spec approval gateway (re-used from the runner's protocol shape)
# ---------------------------------------------------------------------------


class SpecApprovalReport(Protocol):
    approved: bool
    default_branch: str | None
    approval_commit: str | None
    reason: str | None


class SpecApprovalGateway(Protocol):
    def verify_spec_approval(
        self,
        repository_root: Path,
        *,
        issue_number: int,
        spec: OptimizationSpec,
        spec_sha256: str,
        base_commit: str,
    ) -> SpecApprovalReport: ...


# ---------------------------------------------------------------------------
# Deployment coordinator seam
# ---------------------------------------------------------------------------


class DeploymentOutcomeStatus(StrEnum):
    VERIFIED = "verified"
    MANUAL_TRIGGER_REQUIRED = "manual_trigger_required"
    PENDING = "pending"
    FAILED = "failed"
    MISMATCH = "mismatch"


@dataclass(frozen=True)
class DeploymentOutcome:
    status: DeploymentOutcomeStatus
    version: int | None = None
    run_url: str | None = None
    portal_url: str | None = None
    reason_code: str | None = None


@dataclass(frozen=True)
class DeploymentLifecycleRequest:
    repository_root: Path
    workflow: DeploymentWorkflow
    lineage: OptimizationDeploymentLineage
    selected_candidate_id: str
    selected_pull_request: PullRequestReference
    merge_commit: str
    project_endpoint: str
    dispatch: bool
    spec: OptimizationSpec


class DeploymentCoordinator(Protocol):
    """Observes and verifies the configured deployment for a merged candidate.

    When ``request.dispatch`` is true (an autopilot manual-trigger workflow),
    the coordinator triggers the deployment workflow run against
    ``request.merge_commit`` — never the candidate pull-request head — and must
    be idempotent so a retry does not launch a duplicate run. When it is false
    (a merge-trigger workflow, or a human-triggered manual workflow) the
    coordinator only observes. It then binds the published Foundry deployment
    record and runtime into a
    :class:`~foundry_opt.deployment.DeploymentVerificationRequest` and calls
    :func:`~foundry_opt.deployment.verify_deployed_selection`, raising
    :class:`~foundry_opt.deployment.DeploymentLineageMismatchError` when the
    recorded lineage diverges. When the binding is not wired it raises
    :class:`~foundry_opt.optimization.runner.CapabilityUnavailableError`.
    """

    def deploy(
        self,
        request: DeploymentLifecycleRequest,
    ) -> DeploymentOutcome: ...


# ---------------------------------------------------------------------------
# Post-deployment evaluation seam
# ---------------------------------------------------------------------------


class PostDeployStatus(StrEnum):
    RETAINED_IMPROVEMENT = "retained_improvement"
    REGRESSED = "regressed"
    PENDING = "pending"


@dataclass(frozen=True)
class PostDeployOutcome:
    status: PostDeployStatus
    reason_code: str | None = None
    metrics: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "metrics",
            MappingProxyType(dict(self.metrics)),
        )


@dataclass(frozen=True)
class PostDeployRequest:
    repository_root: Path
    lineage: OptimizationDeploymentLineage
    selected_candidate_id: str
    deployment_version: int | None
    project_endpoint: str
    spec: OptimizationSpec


class PostDeployEvaluator(Protocol):
    """Re-evaluates the deployed selection to confirm a retained improvement.

    A live implementation replays the pinned validation split against the
    deployed Foundry version. When the binding is not wired it raises
    :class:`~foundry_opt.optimization.runner.CapabilityUnavailableError` so the
    lifecycle blocks rather than fabricating a retained improvement.
    """

    def evaluate(self, request: PostDeployRequest) -> PostDeployOutcome: ...


# ---------------------------------------------------------------------------
# Typed idempotent lifecycle state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LifecycleState:
    """Durable record of the partial mutations of a lifecycle invocation."""

    campaign_id: str
    issue_number: int
    session_id: str
    updated_at: str
    applied_candidate_ids: tuple[str, ...] = ()
    selected_candidate_id: str | None = None
    selected_pull_request_number: int | None = None
    merge_commit: str | None = None
    lineage_sha256: str | None = None
    deployment_dispatched: bool = False
    deployment_version: int | None = None
    deployment_verified: bool = False
    post_deploy_retained: bool = False
    parent_updated: bool = False
    closed_issue_numbers: tuple[int, ...] = ()
    closed_pull_request_numbers: tuple[int, ...] = ()
    parent_closed: bool = False

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.campaign_id):
            raise ValueError("campaign_id is invalid")
        if self.issue_number < 1:
            raise ValueError("issue_number must be positive")
        if not _IDENTIFIER.fullmatch(self.session_id):
            raise ValueError("session_id is invalid")
        if self.merge_commit is not None and not _COMMIT.fullmatch(
            self.merge_commit
        ):
            raise ValueError("merge_commit is invalid")
        if self.lineage_sha256 is not None and not _SHA256.fullmatch(
            self.lineage_sha256
        ):
            raise ValueError("lineage_sha256 is invalid")
        object.__setattr__(
            self,
            "applied_candidate_ids",
            tuple(dict.fromkeys(self.applied_candidate_ids)),
        )
        object.__setattr__(
            self,
            "closed_issue_numbers",
            tuple(dict.fromkeys(self.closed_issue_numbers)),
        )
        object.__setattr__(
            self,
            "closed_pull_request_numbers",
            tuple(dict.fromkeys(self.closed_pull_request_numbers)),
        )


class LifecycleStateStore(Protocol):
    def load(
        self,
        repository_root: Path,
        campaign_id: str,
    ) -> LifecycleState | None: ...

    def save(
        self,
        repository_root: Path,
        state: LifecycleState,
    ) -> None: ...


class MemoryLifecycleStateStore:
    def __init__(self) -> None:
        self._states: dict[tuple[Path, str], LifecycleState] = {}

    def load(
        self,
        repository_root: Path,
        campaign_id: str,
    ) -> LifecycleState | None:
        return self._states.get((repository_root.resolve(), campaign_id))

    def save(
        self,
        repository_root: Path,
        state: LifecycleState,
    ) -> None:
        key = (repository_root.resolve(), state.campaign_id)
        self._states[key] = state


_LIFECYCLE_STATE_ROOT = Path(".foundry-optimizer") / "lifecycle"


class FileLifecycleStateStore:
    """Atomic, fail-closed JSON store for the durable lifecycle state.

    ``load`` returns ``None`` only when no state file exists yet; an existing
    file that is a symlink, escapes the repository, or is malformed/tampered
    raises :class:`LifecycleStateError` rather than masquerading as a fresh
    state (which would repeat merges and issue closures). ``save`` writes
    through a private temporary file with ``fsync`` before an atomic
    ``os.replace``.
    """

    def load(
        self,
        repository_root: Path,
        campaign_id: str,
    ) -> LifecycleState | None:
        path = _lifecycle_state_path(repository_root, campaign_id)
        _reject_symlink_components(path, repository_root)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise LifecycleStateError(
                "the lifecycle state file could not be read"
            ) from error
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise LifecycleStateError(
                "the lifecycle state file is not valid JSON; it may be "
                "corrupt or tampered"
            ) from error
        return _lifecycle_state_from_document(document)

    def save(
        self,
        repository_root: Path,
        state: LifecycleState,
    ) -> None:
        path = _lifecycle_state_path(repository_root, state.campaign_id)
        _reject_symlink_components(path, repository_root)
        _ensure_safe_directory(path.parent, repository_root)
        payload = (
            json.dumps(
                _lifecycle_state_document(state),
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        temporary = path.with_name(
            f".lifecycle-{os.getpid()}-{os.urandom(4).hex()}.tmp"
        )
        try:
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _lifecycle_state_path(repository_root: Path, campaign_id: str) -> Path:
    if not _IDENTIFIER.fullmatch(campaign_id):
        raise LifecycleStateError("campaign_id is invalid")
    root = repository_root.expanduser().resolve()
    path = root / _LIFECYCLE_STATE_ROOT / f"{campaign_id}.json"
    if not path.resolve().is_relative_to(root):
        raise LifecycleStateError(
            "the lifecycle state path escapes the repository"
        )
    return path


def _reject_symlink_components(path: Path, repository_root: Path) -> None:
    root = repository_root.expanduser().resolve()
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise LifecycleStateError(
            "the lifecycle state path escapes the repository"
        ) from error
    current = root
    for part in (*relative.parts[:-1], relative.parts[-1]):
        current = current / part
        if os.path.lexists(current) and current.is_symlink():
            raise LifecycleStateError(
                "the lifecycle state path must not contain symlinks"
            )


def _ensure_safe_directory(path: Path, repository_root: Path) -> None:
    root = repository_root.expanduser().resolve()
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise LifecycleStateError(
            "the lifecycle state directory escapes the repository"
        ) from error
    current = root
    for part in relative.parts:
        current = current / part
        if os.path.lexists(current) and current.is_symlink():
            raise LifecycleStateError(
                "the lifecycle state directory must not contain symlinks"
            )
        current.mkdir(exist_ok=True)


def _lifecycle_state_document(state: LifecycleState) -> dict[str, Any]:
    return {
        "campaign_id": state.campaign_id,
        "issue_number": state.issue_number,
        "session_id": state.session_id,
        "updated_at": state.updated_at,
        "applied_candidate_ids": list(state.applied_candidate_ids),
        "selected_candidate_id": state.selected_candidate_id,
        "selected_pull_request_number": (
            state.selected_pull_request_number
        ),
        "merge_commit": state.merge_commit,
        "lineage_sha256": state.lineage_sha256,
        "deployment_dispatched": state.deployment_dispatched,
        "deployment_version": state.deployment_version,
        "deployment_verified": state.deployment_verified,
        "post_deploy_retained": state.post_deploy_retained,
        "parent_updated": state.parent_updated,
        "closed_issue_numbers": list(state.closed_issue_numbers),
        "closed_pull_request_numbers": list(
            state.closed_pull_request_numbers
        ),
        "parent_closed": state.parent_closed,
    }


def _lifecycle_state_from_document(document: Any) -> LifecycleState:
    if not isinstance(document, dict):
        raise LifecycleStateError(
            "the lifecycle state document is not an object"
        )
    try:
        return LifecycleState(
            campaign_id=str(document["campaign_id"]),
            issue_number=int(document["issue_number"]),
            session_id=str(document["session_id"]),
            updated_at=str(document["updated_at"]),
            applied_candidate_ids=tuple(
                str(value)
                for value in document.get("applied_candidate_ids", ())
            ),
            selected_candidate_id=_optional_str(
                document.get("selected_candidate_id")
            ),
            selected_pull_request_number=_optional_int(
                document.get("selected_pull_request_number")
            ),
            merge_commit=_optional_str(document.get("merge_commit")),
            lineage_sha256=_optional_str(document.get("lineage_sha256")),
            deployment_dispatched=bool(
                document.get("deployment_dispatched", False)
            ),
            deployment_version=_optional_int(
                document.get("deployment_version")
            ),
            deployment_verified=bool(
                document.get("deployment_verified", False)
            ),
            post_deploy_retained=bool(
                document.get("post_deploy_retained", False)
            ),
            parent_updated=bool(document.get("parent_updated", False)),
            closed_issue_numbers=tuple(
                int(value)
                for value in document.get("closed_issue_numbers", ())
                if isinstance(value, int) and not isinstance(value, bool)
            ),
            closed_pull_request_numbers=tuple(
                int(value)
                for value in document.get(
                    "closed_pull_request_numbers", ()
                )
                if isinstance(value, int) and not isinstance(value, bool)
            ),
            parent_closed=bool(document.get("parent_closed", False)),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise LifecycleStateError(
            "the lifecycle state document is malformed; it may be corrupt "
            "or tampered"
        ) from error


def _optional_str(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def _optional_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(
        value, bool
    ) else None


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LifecycleDependencies:
    config: OptimizerConfig
    state: CampaignStateStore
    lifecycle_state: LifecycleStateStore
    github_gateway_factory: Callable[[Path], Any]
    reconcile_gateway_factory: Callable[[Path], LifecycleReconcileGateway]
    patch_applier: PatchApplier
    repository: CampaignRepository
    spec_approval: SpecApprovalGateway
    deployment: DeploymentCoordinator
    post_deploy: PostDeployEvaluator
    clock: Clock
    detect_workflow: Callable[[Path], DeploymentWorkflow] = (
        detect_deployment_workflow
    )


# ---------------------------------------------------------------------------
# Shared loading helpers
# ---------------------------------------------------------------------------


class _VerifyOnlyWriteError(RuntimeError):
    """A verify-only invocation attempted a repository or GitHub write."""


@dataclass(frozen=True)
class _FinalizedCampaign:
    state: CampaignState
    finalized: FinalizedPublication
    spec: OptimizationSpec


def _load_finalized_campaign(
    deps: LifecycleDependencies,
    root: Path,
    issue_number: int,
) -> _FinalizedCampaign | str:
    campaign_id = _campaign_id(issue_number)
    state = deps.state.load(root, campaign_id)
    if state is None:
        return (
            "no campaign state exists for this issue; run the campaign first"
        )
    if state.finalized is None or state.status != "completed":
        return (
            "the campaign is not finalized yet; finalize it with "
            "`foundry-opt optimize run` before applying or reconciling"
        )
    spec = _load_spec(root, issue_number, state.spec_sha256)
    if spec is None:
        return (
            "the merged optimization specification does not match the "
            "finalized campaign; inspect the campaign before continuing"
        )
    return _FinalizedCampaign(
        state=state,
        finalized=state.finalized,
        spec=spec,
    )


def _load_spec(
    root: Path,
    issue_number: int,
    spec_sha256: str,
) -> OptimizationSpec | None:
    spec_path = root / spec_file_path(issue_number)
    try:
        document = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        spec = OptimizationSpec.model_validate(document)
    except (OSError, UnicodeError, yaml.YAMLError, ValueError):
        return None
    if spec.sha256 != spec_sha256:
        return None
    return spec


def _artifact(
    state: CampaignState,
    candidate_id: str,
) -> CandidateArtifact | None:
    for candidate in state.candidates:
        if (
            candidate.candidate_id == candidate_id
            and candidate.artifact is not None
        ):
            return candidate.artifact
    return None


def _evidence_sha256(root: Path, artifact: CandidateArtifact) -> str | None:
    evidence_path = (root / artifact.evidence_path).resolve()
    if not evidence_path.is_relative_to(root.resolve()):
        return None
    try:
        return hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    except OSError:
        return None


def _candidate_policy(
    spec: OptimizationSpec,
    policy: AutomationPolicy,
) -> CandidatePullRequestPolicy:
    autopilot = (
        spec.decision_mode is DecisionMode.AUTOPILOT_IF_ALLOWED
        and policy.allow_candidate_auto_selection
        and policy.allow_merge
        and policy.merge_actor is not None
        and bool(policy.required_checks)
    )
    if not autopilot:
        return CandidatePullRequestPolicy()
    return CandidatePullRequestPolicy(
        mode=CandidateMergeMode.AUTOPILOT,
        spec_sha256=spec.sha256,
        merge_actor=policy.merge_actor,
        required_checks=policy.required_checks,
        deployment_allowed=(
            policy.allow_deployment
            and spec.deployment_mode
            is DeploymentMode.AFTER_MERGE_IF_ALLOWED
        ),
    )


def _session_id(campaign_id: str) -> str:
    # Deterministic, branch-safe session so retried applies reuse the same
    # candidate branch/pull request rather than opening a duplicate.
    return f"lifecycle-{campaign_id}"


# ---------------------------------------------------------------------------
# Result builders
# ---------------------------------------------------------------------------


def _blocked(
    phase: OptimizePhase,
    issue_number: int,
    code: str,
    message: str,
) -> OptimizeCommandResult:
    return OptimizeCommandResult(
        status=OptimizeCommandStatus.BLOCKED,
        phase=phase,
        summary=redact(message),
        issue_number=issue_number,
        details={"code": code},
    )


def _failed(
    phase: OptimizePhase,
    issue_number: int,
    code: str,
    message: str,
) -> OptimizeCommandResult:
    return OptimizeCommandResult(
        status=OptimizeCommandStatus.FAILED,
        phase=phase,
        summary=redact(message),
        issue_number=issue_number,
        details={"code": code},
    )


# ---------------------------------------------------------------------------
# APPLY service
# ---------------------------------------------------------------------------


class CandidateApplyService:
    """Applies one exact evaluated candidate patch (``optimize apply``)."""

    def __init__(self, dependencies: LifecycleDependencies) -> None:
        self._deps = dependencies

    def execute(
        self,
        request: OptimizeCommandRequest,
    ) -> OptimizeCommandResult:
        try:
            return self._execute(request)
        except LifecycleStateError as error:
            return _blocked(
                OptimizePhase.APPLY,
                request.issue_number,
                "lifecycle_state_corrupt",
                "the durable lifecycle state is unreadable or tampered; "
                f"inspect it before retrying: {error}",
            )
        except CapabilityUnavailableError as error:
            return _blocked(
                OptimizePhase.APPLY,
                request.issue_number,
                error.code,
                str(error),
            )
        except Exception as error:  # noqa: BLE001 - surface as typed result
            return _failed(
                OptimizePhase.APPLY,
                request.issue_number,
                "lifecycle_error",
                f"the apply lifecycle failed unexpectedly: {error}",
            )

    def _execute(
        self,
        request: OptimizeCommandRequest,
    ) -> OptimizeCommandResult:
        root = request.repository_root.expanduser().resolve()
        candidate_id = request.candidate_id
        if candidate_id is None:
            return _blocked(
                OptimizePhase.APPLY,
                request.issue_number,
                "candidate_required",
                "a candidate identifier is required to apply a patch",
            )

        loaded = _load_finalized_campaign(
            self._deps, root, request.issue_number
        )
        if isinstance(loaded, str):
            return _blocked(
                OptimizePhase.APPLY,
                request.issue_number,
                "campaign_not_finalized",
                loaded,
            )
        state, finalized, spec = loaded.state, loaded.finalized, loaded.spec

        artifact = _artifact(state, candidate_id)
        if artifact is None or candidate_id not in state.pareto_candidate_ids:
            return _blocked(
                OptimizePhase.APPLY,
                request.issue_number,
                "candidate_not_eligible",
                f"candidate {candidate_id!r} is not an eligible finalized "
                "candidate",
            )
        if not artifact.eligible:
            return _blocked(
                OptimizePhase.APPLY,
                request.issue_number,
                "candidate_not_eligible",
                f"candidate {candidate_id!r} was not retained after "
                "held-out evaluation",
            )
        if candidate_id not in finalized.candidate_issue_numbers:
            return _blocked(
                OptimizePhase.APPLY,
                request.issue_number,
                "candidate_issue_missing",
                f"candidate {candidate_id!r} has no published candidate issue",
            )

        evidence_sha256 = _evidence_sha256(root, artifact)
        if evidence_sha256 is None:
            return _blocked(
                OptimizePhase.APPLY,
                request.issue_number,
                "evidence_missing",
                "the finalized candidate evidence is missing or unreadable",
            )

        try:
            pinned = self._deps.repository.pin_default_branch(root)
        except Exception:
            return _blocked(
                OptimizePhase.APPLY,
                request.issue_number,
                "default_branch_unavailable",
                "the repository default branch could not be pinned",
            )

        session_id = _session_id(state.campaign_id)
        application = CandidateApplicationRequest(
            repository_root=root,
            campaign_id=state.campaign_id,
            target=spec.target,
            expected_default_branch=pinned.default_branch,
            session_id=session_id,
            campaign_pull_request_number=(
                finalized.campaign_pull_request_number
            ),
            candidate_issue_number=finalized.candidate_issue_numbers[
                candidate_id
            ],
            candidate=artifact,
            evidence_sha256=evidence_sha256,
            close_rejected=False,
            decision_policy=_candidate_policy(
                spec, self._deps.config.automation_policy
            ),
        )

        gateway = self._deps.github_gateway_factory(root)
        if request.verify_only:
            return self._verify_only(request, application, gateway)
        return self._apply(request, application, gateway, state, session_id)

    # -- verify-only --------------------------------------------------------

    def _verify_only(
        self,
        request: OptimizeCommandRequest,
        application: CandidateApplicationRequest,
        gateway: CandidateGateway,
    ) -> OptimizeCommandResult:
        read_only_gateway = _ReadOnlyGateway(gateway)
        read_only_applier = _ReadOnlyPatchApplier(self._deps.patch_applier)
        try:
            result = verify_and_apply_candidate(
                application,
                read_only_gateway,
                read_only_applier,
            )
        except _VerifyOnlyWriteError:
            return OptimizeCommandResult(
                status=OptimizeCommandStatus.AWAITING_AGENT,
                phase=OptimizePhase.APPLY,
                summary=(
                    f"Candidate {application.candidate.candidate_id} has not "
                    "been applied yet."
                ),
                issue_number=request.issue_number,
                details={
                    "code": "candidate_not_applied",
                    "candidate_id": application.candidate.candidate_id,
                },
                next_action=(
                    "Run `foundry-opt optimize apply --issue "
                    f"{request.issue_number} --candidate "
                    f"{application.candidate.candidate_id}` to publish the "
                    "exact patch."
                ),
            )
        except GitHubPermissionDeniedError as error:
            return _blocked(
                OptimizePhase.APPLY,
                request.issue_number,
                "permission_denied",
                "the GitHub token lacks the capabilities required to verify "
                f"the candidate ({error.missing!r})",
            )
        if result.status is CandidateApplicationStatus.ALREADY_APPLIED:
            return OptimizeCommandResult(
                status=OptimizeCommandStatus.COMPLETE,
                phase=OptimizePhase.APPLY,
                summary=(
                    f"Candidate {result.candidate_id} is already published as "
                    f"#{result.pull_request.number} and matches the exact "
                    "verified patch."
                ),
                issue_number=request.issue_number,
                details={
                    "code": "verified",
                    "candidate_id": result.candidate_id,
                    "pull_request": result.pull_request.number,
                    "commit_sha": result.commit_sha,
                },
            )
        return _blocked(
            OptimizePhase.APPLY,
            request.issue_number,
            result.reason_code or "verification_failed",
            "the published candidate pull request does not match the exact "
            "verified patch",
        )

    # -- apply --------------------------------------------------------------

    def _apply(
        self,
        request: OptimizeCommandRequest,
        application: CandidateApplicationRequest,
        gateway: CandidateGateway,
        state: CampaignState,
        session_id: str,
    ) -> OptimizeCommandResult:
        try:
            result = verify_and_apply_candidate(
                application,
                gateway,
                self._deps.patch_applier,
            )
        except GitHubPermissionDeniedError as error:
            return _blocked(
                OptimizePhase.APPLY,
                request.issue_number,
                "permission_denied",
                "the GitHub token lacks the capabilities required to apply "
                f"the candidate ({error.missing!r})",
            )
        candidate_id = application.candidate.candidate_id
        if result.status is CandidateApplicationStatus.REJECTED:
            return _failed(
                OptimizePhase.APPLY,
                request.issue_number,
                result.reason_code or "rejected",
                "the candidate failed exact-patch verification and was not "
                f"applied ({result.reason_code})",
            )

        pull_request = result.pull_request
        assert pull_request is not None
        self._comment_parent_once(
            request,
            state,
            session_id,
            candidate_id,
            pull_request.number,
            application.candidate_issue_number,
        )
        already = result.status is CandidateApplicationStatus.ALREADY_APPLIED
        return OptimizeCommandResult(
            status=OptimizeCommandStatus.COMPLETE,
            phase=OptimizePhase.APPLY,
            summary=(
                f"Candidate {candidate_id} "
                + ("was already applied as" if already else "applied as")
                + f" pull request #{pull_request.number}."
            ),
            issue_number=request.issue_number,
            details={
                "code": (
                    "already_applied" if already else "applied"
                ),
                "candidate_id": candidate_id,
                "pull_request": pull_request.number,
                "commit_sha": result.commit_sha,
                "candidate_issue": application.candidate_issue_number,
            },
            next_action=(
                "Run `foundry-opt optimize reconcile --issue "
                f"{request.issue_number}` once every eligible candidate is "
                "applied."
            ),
        )

    def _comment_parent_once(
        self,
        request: OptimizeCommandRequest,
        state: CampaignState,
        session_id: str,
        candidate_id: str,
        pull_request_number: int,
        candidate_issue_number: int,
    ) -> None:
        lifecycle_state = self._deps.lifecycle_state.load(
            request.repository_root.expanduser().resolve(),
            state.campaign_id,
        ) or LifecycleState(
            campaign_id=state.campaign_id,
            issue_number=request.issue_number,
            session_id=session_id,
            updated_at=self._deps.clock.now().isoformat(),
        )
        if candidate_id in lifecycle_state.applied_candidate_ids:
            return
        root = request.repository_root.expanduser().resolve()
        gateway = self._deps.github_gateway_factory(root)
        try:
            gateway.comment_issue(
                root,
                request.issue_number,
                _apply_parent_comment(
                    state.campaign_id,
                    candidate_id,
                    pull_request_number,
                    candidate_issue_number,
                ),
            )
        except Exception:
            # The child issue comment already recorded the exact publication;
            # a failed parent note is retried on the next invocation.
            return
        self._deps.lifecycle_state.save(
            root,
            replace(
                lifecycle_state,
                applied_candidate_ids=(
                    *lifecycle_state.applied_candidate_ids,
                    candidate_id,
                ),
                updated_at=self._deps.clock.now().isoformat(),
            ),
        )


def _apply_parent_comment(
    campaign_id: str,
    candidate_id: str,
    pull_request_number: int,
    candidate_issue_number: int,
) -> str:
    return "\n".join(
        (
            f"<!-- foundry-opt:apply:{campaign_id}:{candidate_id} -->",
            f"Applied exact candidate `{candidate_id}` as pull request "
            f"#{pull_request_number}.",
            f"Candidate issue: #{candidate_issue_number}",
        )
    )


# ---------------------------------------------------------------------------
# RECONCILE service
# ---------------------------------------------------------------------------


class CandidateReconcileService:
    """Reconciles decisions, deployment, and issue state (``reconcile``)."""

    def __init__(self, dependencies: LifecycleDependencies) -> None:
        self._deps = dependencies

    def execute(
        self,
        request: OptimizeCommandRequest,
    ) -> OptimizeCommandResult:
        try:
            return self._execute(request)
        except LifecycleStateError as error:
            return _blocked(
                OptimizePhase.RECONCILE,
                request.issue_number,
                "lifecycle_state_corrupt",
                "the durable lifecycle state is unreadable or tampered; "
                f"inspect it before retrying: {error}",
            )
        except CapabilityUnavailableError as error:
            return _blocked(
                OptimizePhase.RECONCILE,
                request.issue_number,
                error.code,
                str(error),
            )
        except Exception as error:  # noqa: BLE001 - surface as typed result
            return _failed(
                OptimizePhase.RECONCILE,
                request.issue_number,
                "lifecycle_error",
                f"the reconcile lifecycle failed unexpectedly: {error}",
            )

    def _execute(
        self,
        request: OptimizeCommandRequest,
    ) -> OptimizeCommandResult:
        root = request.repository_root.expanduser().resolve()
        loaded = _load_finalized_campaign(
            self._deps, root, request.issue_number
        )
        if isinstance(loaded, str):
            return _blocked(
                OptimizePhase.RECONCILE,
                request.issue_number,
                "campaign_not_finalized",
                loaded,
            )
        state, finalized, spec = loaded.state, loaded.finalized, loaded.spec
        policy = self._deps.config.automation_policy
        workflow = self._deps.detect_workflow(root)
        deploy_automated = (
            spec.deployment_mode is DeploymentMode.AFTER_MERGE_IF_ALLOWED
            and policy.allow_deployment
        )

        resumed = self._deps.lifecycle_state.load(root, state.campaign_id)
        if resumed is not None and resumed.selected_candidate_id is not None:
            # A prior invocation already selected and merged a candidate;
            # re-running the merge decision would double-merge, so resume the
            # deployment and issue lifecycle from the recorded selection.
            selected = self._reconstruct_selected(spec, state, resumed)
            if selected is None:
                return _blocked(
                    OptimizePhase.RECONCILE,
                    request.issue_number,
                    "selection_unavailable",
                    "the recorded selected candidate is no longer present",
                )
            gateway = self._deps.reconcile_gateway_factory(root)
            merge_commit = resumed.merge_commit
            if merge_commit is None:
                resolved = self._resolve_merge_commit(
                    request, root, gateway, selected
                )
                if isinstance(resolved, OptimizeCommandResult):
                    return resolved
                merge_commit = resolved
                resumed = self._persist(
                    request, replace(resumed, merge_commit=merge_commit)
                )
            entries = self._ranked_entries(root, state, finalized, gateway)
            return self._continue_after_merge(
                request,
                root,
                state,
                finalized,
                spec,
                selected,
                entries,
                workflow,
                deploy_automated,
                merge_commit,
                resumed,
            )

        approval = self._spec_approval(request, root, state, spec)
        if isinstance(approval, OptimizeCommandResult):
            return approval

        gateway = self._deps.reconcile_gateway_factory(root)
        entries = self._ranked_entries(root, state, finalized, gateway)
        if not entries:
            return _blocked(
                OptimizePhase.RECONCILE,
                request.issue_number,
                "no_applied_candidates",
                "no eligible candidate pull requests have been applied; run "
                "`foundry-opt optimize apply` for each candidate first",
            )

        # Adopt an already-merged eligible selection before attempting a new
        # merge: in human decision mode a maintainer merges the chosen pull
        # request directly on GitHub, and in autopilot mode a prior merge may
        # not have persisted its selection. Either way we must never merge a
        # second candidate.
        merged = tuple(
            entry
            for entry in entries
            if entry.eligible and entry.pull_request.state == "MERGED"
        )
        if len(merged) > 1:
            return _blocked(
                OptimizePhase.RECONCILE,
                request.issue_number,
                "ambiguous_merged_selection",
                "more than one eligible candidate pull request is already "
                "merged; a human must resolve which selection to deploy",
            )
        if len(merged) == 1:
            return self._record_and_continue(
                request,
                root,
                state,
                finalized,
                spec,
                merged[0],
                entries,
                workflow,
                deploy_automated,
                gateway,
            )

        if spec.decision_mode is DecisionMode.HUMAN:
            # Nothing merged yet; report the ranked eligible pull requests and
            # wait for a maintainer to merge one.
            return self._waiting(request, entries)

        open_entries = tuple(
            entry
            for entry in entries
            if entry.pull_request.state == "OPEN"
        )
        if not open_entries:
            return _blocked(
                OptimizePhase.RECONCILE,
                request.issue_number,
                "no_open_candidates",
                "no open eligible candidate pull requests remain to merge",
            )
        # The merge decision never dispatches a deployment (allow_deployment is
        # forced off); deployment is dispatched independently, against the
        # exact merge commit, after it is resolved.
        reconcile_policy = policy.model_copy(
            update={"allow_deployment": False}
        )
        try:
            decision = reconcile_candidates(
                CandidateReconcileRequest(
                    repository_root=root,
                    approval=approval,
                    automation_policy=reconcile_policy,
                    ranked_candidates=open_entries,
                ),
                gateway,
            )
        except GitHubPermissionDeniedError as error:
            return _blocked(
                OptimizePhase.RECONCILE,
                request.issue_number,
                "permission_denied",
                "the reconcile actor lacks the required capabilities "
                f"({error.missing!r})",
            )

        if decision.status is CandidateReconcileStatus.WAITING_FOR_HUMAN:
            return self._waiting(request, entries)
        if decision.status is CandidateReconcileStatus.BLOCKED:
            return _blocked(
                OptimizePhase.RECONCILE,
                request.issue_number,
                decision.reason_code or "reconcile_blocked",
                "autopilot reconciliation is blocked: "
                f"{decision.reason_code}",
            )

        selected = _entry_for(open_entries, decision.selected_candidate_id)
        assert selected is not None
        return self._record_and_continue(
            request,
            root,
            state,
            finalized,
            spec,
            selected,
            entries,
            workflow,
            deploy_automated,
            gateway,
        )

    def _record_and_continue(
        self,
        request: OptimizeCommandRequest,
        root: Path,
        state: CampaignState,
        finalized: FinalizedPublication,
        spec: OptimizationSpec,
        selected: CandidateReconcileEntry,
        entries: tuple[CandidateReconcileEntry, ...],
        workflow: DeploymentWorkflow,
        deploy_automated: bool,
        gateway: LifecycleReconcileGateway,
    ) -> OptimizeCommandResult:
        # Record the selection and resolve the exact merge commit before any
        # further mutation so a retry resumes rather than re-merging.
        lifecycle_state = self._load_or_init(request, state, selected)
        merge_commit = self._resolve_merge_commit(
            request, root, gateway, selected
        )
        if isinstance(merge_commit, OptimizeCommandResult):
            return merge_commit
        lifecycle_state = self._persist(
            request, replace(lifecycle_state, merge_commit=merge_commit)
        )
        return self._continue_after_merge(
            request,
            root,
            state,
            finalized,
            spec,
            selected,
            entries,
            workflow,
            deploy_automated,
            merge_commit,
            lifecycle_state,
        )

    # -- spec approval ------------------------------------------------------

    def _spec_approval(
        self,
        request: OptimizeCommandRequest,
        root: Path,
        state: CampaignState,
        spec: OptimizationSpec,
    ) -> OptimizationSpecApproval | OptimizeCommandResult:
        try:
            pinned = self._deps.repository.pin_default_branch(root)
            report = self._deps.spec_approval.verify_spec_approval(
                root,
                issue_number=request.issue_number,
                spec=spec,
                spec_sha256=state.spec_sha256,
                base_commit=pinned.commit,
            )
        except Exception:
            return _blocked(
                OptimizePhase.RECONCILE,
                request.issue_number,
                "spec_approval_unavailable",
                "the merged specification approval could not be verified",
            )
        if not report.approved or report.approval_commit is None:
            return _blocked(
                OptimizePhase.RECONCILE,
                request.issue_number,
                "spec_not_approved",
                report.reason or "the specification is not approved",
            )
        gate = (
            ApprovalGate.POLICY
            if spec_is_autopilot_eligible(
                spec, self._deps.config.automation_policy
            )
            else ApprovalGate.HUMAN
        )
        return approve_optimization_spec(
            spec,
            approval_commit=report.approval_commit,
            approval_gate=gate,
        )

    # -- ranked slate -------------------------------------------------------

    def _ranked_entries(
        self,
        root: Path,
        state: CampaignState,
        finalized: FinalizedPublication,
        gateway: LifecycleReconcileGateway,
    ) -> tuple[CandidateReconcileEntry, ...]:
        entries: list[CandidateReconcileEntry] = []
        for candidate_id in state.pareto_candidate_ids:
            artifact = _artifact(state, candidate_id)
            if (
                artifact is None
                or not artifact.eligible
                or candidate_id not in finalized.candidate_issue_numbers
            ):
                continue
            pull_request = gateway.locate_candidate_pull_request(
                root,
                state.campaign_id,
                candidate_id,
            )
            if pull_request is None:
                continue
            checks = gateway.candidate_checks(root, pull_request)
            entries.append(
                CandidateReconcileEntry(
                    candidate_id=candidate_id,
                    pull_request=pull_request,
                    eligible=artifact.eligible,
                    checks=checks,
                )
            )
        return tuple(entries)

    def _waiting(
        self,
        request: OptimizeCommandRequest,
        entries: tuple[CandidateReconcileEntry, ...],
    ) -> OptimizeCommandResult:
        ranked = tuple(
            {
                "candidate_id": entry.candidate_id,
                "pull_request": entry.pull_request.number,
            }
            for entry in entries
            if entry.eligible
        )
        return OptimizeCommandResult(
            status=OptimizeCommandStatus.AWAITING_AGENT,
            phase=OptimizePhase.RECONCILE,
            summary=(
                "Human decision required: review and merge one of the ranked "
                "eligible candidate pull requests."
            ),
            issue_number=request.issue_number,
            details={
                "code": "waiting_for_human",
                "ranked_candidates": ranked,
            },
            next_action=(
                "A maintainer merges the chosen candidate pull request, then "
                "re-run `foundry-opt optimize reconcile --issue "
                f"{request.issue_number}`."
            ),
        )

    # -- post-merge deployment + issue lifecycle ----------------------------

    def _resolve_merge_commit(
        self,
        request: OptimizeCommandRequest,
        root: Path,
        gateway: LifecycleReconcileGateway,
        selected: CandidateReconcileEntry,
    ) -> str | OptimizeCommandResult:
        try:
            merge_commit = gateway.resolve_merge_commit(
                root, selected.pull_request.number
            )
        except Exception:
            return _blocked(
                OptimizePhase.RECONCILE,
                request.issue_number,
                "merge_commit_unavailable",
                "the merge commit for the selected pull request could not be "
                "resolved",
            )
        if not _COMMIT.fullmatch(merge_commit):
            return _blocked(
                OptimizePhase.RECONCILE,
                request.issue_number,
                "merge_commit_unavailable",
                "the resolved merge commit is not a full Git commit",
            )
        return merge_commit

    def _reconstruct_selected(
        self,
        spec: OptimizationSpec,
        state: CampaignState,
        lifecycle_state: LifecycleState,
    ) -> CandidateReconcileEntry | None:
        candidate_id = lifecycle_state.selected_candidate_id
        number = lifecycle_state.selected_pull_request_number
        artifact = (
            _artifact(state, candidate_id)
            if candidate_id is not None
            else None
        )
        if candidate_id is None or number is None or artifact is None:
            return None
        pull_request = PullRequestReference(
            number=number,
            url=f"https://github.com/{spec.repository}/pull/{number}",
            head_branch=(
                f"foundry-opt/{state.campaign_id}/{candidate_id}/"
                f"{lifecycle_state.session_id}"
            ),
            head_commit=artifact.patch.result_commit,
            draft=False,
            body="",
            base_branch="",
            state="MERGED",
        )
        return CandidateReconcileEntry(
            candidate_id=candidate_id,
            pull_request=pull_request,
            eligible=artifact.eligible,
        )

    def _continue_after_merge(
        self,
        request: OptimizeCommandRequest,
        root: Path,
        state: CampaignState,
        finalized: FinalizedPublication,
        spec: OptimizationSpec,
        selected: CandidateReconcileEntry,
        entries: tuple[CandidateReconcileEntry, ...],
        workflow: DeploymentWorkflow,
        deploy_automated: bool,
        merge_commit: str,
        lifecycle_state: LifecycleState,
    ) -> OptimizeCommandResult:
        gateway = self._deps.reconcile_gateway_factory(root)
        lineage = self._build_lineage(
            request,
            root,
            state,
            finalized,
            selected,
            merge_commit,
            gateway,
        )
        if isinstance(lineage, OptimizeCommandResult):
            return lineage
        lineage_sha256 = optimization_deployment_lineage_sha256(lineage)
        lifecycle_state = self._persist(
            request,
            replace(
                lifecycle_state,
                merge_commit=merge_commit,
                lineage_sha256=lineage_sha256,
            ),
        )

        project_endpoint = self._project_endpoint(spec)
        if project_endpoint is None:
            return _blocked(
                OptimizePhase.RECONCILE,
                request.issue_number,
                "environment_unavailable",
                f"environment {spec.environment!r} is not configured",
            )

        return self._deploy_and_close(
            request,
            root,
            state,
            finalized,
            spec,
            selected,
            entries,
            workflow,
            deploy_automated,
            lineage,
            lineage_sha256,
            merge_commit,
            project_endpoint,
            lifecycle_state,
        )

    def _deploy_and_close(
        self,
        request: OptimizeCommandRequest,
        root: Path,
        state: CampaignState,
        finalized: FinalizedPublication,
        spec: OptimizationSpec,
        selected: CandidateReconcileEntry,
        entries: tuple[CandidateReconcileEntry, ...],
        workflow: DeploymentWorkflow,
        deploy_automated: bool,
        lineage: OptimizationDeploymentLineage,
        lineage_sha256: str,
        merge_commit: str,
        project_endpoint: str,
        lifecycle_state: LifecycleState,
    ) -> OptimizeCommandResult:
        # Only an autopilot manual-trigger workflow is dispatched by the
        # optimizer, and only once. Merge-trigger workflows run on merge, and a
        # human-mode manual workflow is triggered by the maintainer; in both
        # cases the coordinator merely observes.
        dispatch = (
            deploy_automated
            and workflow.trigger is DeploymentTrigger.MANUAL
            and not lifecycle_state.deployment_dispatched
        )
        if dispatch:
            gateway = self._deps.reconcile_gateway_factory(root)
            permissions = gateway.verify_permissions(
                GitHubCapabilities.DEPLOY_DISPATCH
            )
            if GitHubCapabilities.DEPLOY_DISPATCH & ~permissions.granted:
                return _blocked(
                    OptimizePhase.RECONCILE,
                    request.issue_number,
                    "permission_denied",
                    "auto-dispatching the deployment workflow requires the "
                    "separate DEPLOY_DISPATCH capability, which the reconcile "
                    "actor was not granted",
                )
        try:
            outcome = self._deps.deployment.deploy(
                DeploymentLifecycleRequest(
                    repository_root=root,
                    workflow=workflow,
                    lineage=lineage,
                    selected_candidate_id=selected.candidate_id,
                    selected_pull_request=selected.pull_request,
                    merge_commit=merge_commit,
                    project_endpoint=project_endpoint,
                    dispatch=dispatch,
                    spec=spec,
                )
            )
        except DeploymentLineageMismatchError:
            return _blocked(
                OptimizePhase.RECONCILE,
                request.issue_number,
                "deployment_lineage_mismatch",
                "the deployed selection's optimization lineage does not "
                "match the expected issue, spec, campaign, candidate, or "
                "commit provenance",
            )
        except CapabilityUnavailableError as error:
            return _blocked(
                OptimizePhase.RECONCILE,
                request.issue_number,
                error.code,
                str(error),
            )

        if dispatch:
            # The coordinator returned, so the workflow was dispatched exactly
            # once; a resumed invocation must observe rather than re-dispatch.
            lifecycle_state = self._persist(
                request,
                replace(lifecycle_state, deployment_dispatched=True),
            )

        if outcome.status is not DeploymentOutcomeStatus.VERIFIED:
            return self._deployment_incomplete(
                request, selected, workflow, outcome
            )
        lifecycle_state = self._persist(
            request,
            replace(
                lifecycle_state,
                deployment_version=outcome.version,
                deployment_verified=True,
            ),
        )

        try:
            post_deploy = self._deps.post_deploy.evaluate(
                PostDeployRequest(
                    repository_root=root,
                    lineage=lineage,
                    selected_candidate_id=selected.candidate_id,
                    deployment_version=outcome.version,
                    project_endpoint=project_endpoint,
                    spec=spec,
                )
            )
        except CapabilityUnavailableError as error:
            return _blocked(
                OptimizePhase.RECONCILE,
                request.issue_number,
                error.code,
                str(error),
            )

        if post_deploy.status is PostDeployStatus.REGRESSED:
            return _blocked(
                OptimizePhase.RECONCILE,
                request.issue_number,
                "post_deploy_regression",
                "the deployed selection regressed on the held-out evaluation; "
                "the parent issue stays open for inspection",
            )
        if post_deploy.status is not PostDeployStatus.RETAINED_IMPROVEMENT:
            return OptimizeCommandResult(
                status=OptimizeCommandStatus.AWAITING_AGENT,
                phase=OptimizePhase.RECONCILE,
                summary=(
                    "The post-deployment evaluation has not confirmed a "
                    "retained improvement yet."
                ),
                issue_number=request.issue_number,
                details={
                    "code": "post_deploy_pending",
                    "candidate_id": selected.candidate_id,
                },
                next_action=(
                    "Re-run `foundry-opt optimize reconcile --issue "
                    f"{request.issue_number}` once the post-deployment "
                    "evaluation completes."
                ),
            )

        lifecycle_state = self._persist(
            request,
            replace(lifecycle_state, post_deploy_retained=True),
        )
        return self._finalize_issue_state(
            request,
            root,
            state,
            finalized,
            selected,
            entries,
            lineage,
            lineage_sha256,
            merge_commit,
            outcome,
            post_deploy,
            lifecycle_state,
        )

    def _deployment_incomplete(
        self,
        request: OptimizeCommandRequest,
        selected: CandidateReconcileEntry,
        workflow: DeploymentWorkflow,
        outcome: DeploymentOutcome,
    ) -> OptimizeCommandResult:
        details = {
            "code": f"deployment_{outcome.status.value}",
            "candidate_id": selected.candidate_id,
            "deployment_trigger": workflow.trigger.value,
            "run_url": outcome.run_url,
        }
        if outcome.status in (
            DeploymentOutcomeStatus.FAILED,
            DeploymentOutcomeStatus.MISMATCH,
        ):
            return OptimizeCommandResult(
                status=OptimizeCommandStatus.BLOCKED,
                phase=OptimizePhase.RECONCILE,
                summary=(
                    f"The deployment for candidate {selected.candidate_id} "
                    f"did not verify ({outcome.status.value})."
                ),
                issue_number=request.issue_number,
                details=details,
            )
        return OptimizeCommandResult(
            status=OptimizeCommandStatus.AWAITING_AGENT,
            phase=OptimizePhase.RECONCILE,
            summary=(
                f"The deployment for candidate {selected.candidate_id} is "
                f"not complete ({outcome.status.value})."
            ),
            issue_number=request.issue_number,
            details=details,
            next_action=(
                "Complete the deployment workflow, then re-run "
                "`foundry-opt optimize reconcile --issue "
                f"{request.issue_number}`."
            ),
        )

    def _finalize_issue_state(
        self,
        request: OptimizeCommandRequest,
        root: Path,
        state: CampaignState,
        finalized: FinalizedPublication,
        selected: CandidateReconcileEntry,
        entries: tuple[CandidateReconcileEntry, ...],
        lineage: OptimizationDeploymentLineage,
        lineage_sha256: str,
        merge_commit: str,
        outcome: DeploymentOutcome,
        post_deploy: PostDeployOutcome,
        lifecycle_state: LifecycleState,
    ) -> OptimizeCommandResult:
        gateway = self._deps.github_gateway_factory(root)

        if not lifecycle_state.parent_updated:
            gateway.update_issue_body(
                root,
                request.issue_number,
                _parent_summary_body(
                    state,
                    finalized,
                    selected,
                    lineage,
                    lineage_sha256,
                    merge_commit,
                    outcome,
                    post_deploy,
                ),
            )
            lifecycle_state = self._persist(
                request, replace(lifecycle_state, parent_updated=True)
            )

        lifecycle_state = self._close_superseded(
            request,
            root,
            finalized,
            selected,
            entries,
            gateway,
            lifecycle_state,
        )

        if not lifecycle_state.parent_closed:
            gateway.close_issue(
                root,
                request.issue_number,
                _parent_close_comment(selected.candidate_id, outcome),
            )
            lifecycle_state = self._persist(
                request, replace(lifecycle_state, parent_closed=True)
            )

        return OptimizeCommandResult(
            status=OptimizeCommandStatus.COMPLETE,
            phase=OptimizePhase.RECONCILE,
            summary=(
                f"Candidate {selected.candidate_id} was merged, deployed, and "
                "confirmed as a retained improvement; the optimization issue "
                "is closed."
            ),
            issue_number=request.issue_number,
            details={
                "code": "reconciled",
                "candidate_id": selected.candidate_id,
                "pull_request": selected.pull_request.number,
                "merge_commit": merge_commit,
                "lineage_sha256": lineage_sha256,
                "deployment_version": outcome.version,
                "run_url": outcome.run_url,
                "portal_url": outcome.portal_url,
                "post_deploy_metrics": dict(post_deploy.metrics),
                "campaign_pull_request": (
                    finalized.campaign_pull_request_number
                ),
            },
        )

    def _close_superseded(
        self,
        request: OptimizeCommandRequest,
        root: Path,
        finalized: FinalizedPublication,
        selected: CandidateReconcileEntry,
        entries: tuple[CandidateReconcileEntry, ...],
        gateway: LifecycleIssueGateway,
        lifecycle_state: LifecycleState,
    ) -> LifecycleState:
        for candidate_id, issue_number in (
            finalized.candidate_issue_numbers.items()
        ):
            if candidate_id == selected.candidate_id:
                continue
            if issue_number not in lifecycle_state.closed_issue_numbers:
                gateway.close_issue(
                    root,
                    issue_number,
                    _superseded_comment(selected.candidate_id),
                )
                lifecycle_state = self._persist(
                    request,
                    replace(
                        lifecycle_state,
                        closed_issue_numbers=(
                            *lifecycle_state.closed_issue_numbers,
                            issue_number,
                        ),
                    ),
                )
            entry = _entry_for(entries, candidate_id)
            if (
                entry is not None
                and entry.pull_request.state == "OPEN"
                and entry.pull_request.number
                not in lifecycle_state.closed_pull_request_numbers
            ):
                gateway.close_pull_request(
                    root,
                    entry.pull_request.number,
                    _superseded_comment(selected.candidate_id),
                )
                lifecycle_state = self._persist(
                    request,
                    replace(
                        lifecycle_state,
                        closed_pull_request_numbers=(
                            *lifecycle_state.closed_pull_request_numbers,
                            entry.pull_request.number,
                        ),
                    ),
                )

        campaign_number = finalized.campaign_pull_request_number
        if campaign_number not in lifecycle_state.closed_pull_request_numbers:
            gateway.close_pull_request(
                root,
                campaign_number,
                _campaign_close_comment(selected.candidate_id),
            )
            lifecycle_state = self._persist(
                request,
                replace(
                    lifecycle_state,
                    closed_pull_request_numbers=(
                        *lifecycle_state.closed_pull_request_numbers,
                        campaign_number,
                    ),
                ),
            )
        return lifecycle_state

    # -- lineage ------------------------------------------------------------

    def _build_lineage(
        self,
        request: OptimizeCommandRequest,
        root: Path,
        state: CampaignState,
        finalized: FinalizedPublication,
        selected: CandidateReconcileEntry,
        merge_commit: str,
        gateway: LifecycleReconcileGateway,
    ) -> OptimizationDeploymentLineage | OptimizeCommandResult:
        artifact = _artifact(state, selected.candidate_id)
        assert artifact is not None
        evidence_sha256 = _evidence_sha256(root, artifact)
        if evidence_sha256 is None:
            return _blocked(
                OptimizePhase.RECONCILE,
                request.issue_number,
                "evidence_missing",
                "the finalized candidate evidence is missing or unreadable",
            )
        try:
            tree_sha = gateway.resolve_tree(root, merge_commit)
        except Exception:
            tree_sha = None
        if tree_sha is None or not _TREE.fullmatch(tree_sha):
            return _blocked(
                OptimizePhase.RECONCILE,
                request.issue_number,
                "selected_tree_unavailable",
                "the merged selection's result tree could not be resolved",
            )
        try:
            return OptimizationDeploymentLineage(
                parent_issue_number=request.issue_number,
                spec_sha256=state.spec_sha256,
                campaign_id=state.campaign_id,
                campaign_pull_request_number=(
                    finalized.campaign_pull_request_number
                ),
                candidate_issue_number=finalized.candidate_issue_numbers[
                    selected.candidate_id
                ],
                candidate_pull_request_number=selected.pull_request.number,
                candidate_id=selected.candidate_id,
                selected_draft_id=artifact.draft_id,
                patch_sha256=artifact.patch.sha256,
                evidence_sha256=evidence_sha256,
                selected_tree_sha=tree_sha,
                selected_merge_commit=merge_commit,
            )
        except ValueError:
            return _blocked(
                OptimizePhase.RECONCILE,
                request.issue_number,
                "lineage_invalid",
                "the deployment lineage provenance could not be constructed",
            )

    # -- lifecycle state helpers -------------------------------------------

    def _load_or_init(
        self,
        request: OptimizeCommandRequest,
        state: CampaignState,
        selected: CandidateReconcileEntry,
    ) -> LifecycleState:
        root = request.repository_root.expanduser().resolve()
        existing = self._deps.lifecycle_state.load(root, state.campaign_id)
        session_id = _session_id(state.campaign_id)
        if existing is None:
            existing = LifecycleState(
                campaign_id=state.campaign_id,
                issue_number=request.issue_number,
                session_id=session_id,
                updated_at=self._deps.clock.now().isoformat(),
            )
        return self._persist(
            request,
            replace(
                existing,
                selected_candidate_id=selected.candidate_id,
                selected_pull_request_number=selected.pull_request.number,
            ),
        )

    def _persist(
        self,
        request: OptimizeCommandRequest,
        state: LifecycleState,
    ) -> LifecycleState:
        root = request.repository_root.expanduser().resolve()
        updated = replace(
            state, updated_at=self._deps.clock.now().isoformat()
        )
        self._deps.lifecycle_state.save(root, updated)
        return updated

    # -- config helpers -----------------------------------------------------

    def _project_endpoint(self, spec: OptimizationSpec) -> str | None:
        profile = self._deps.config.environments.get(spec.environment)
        if profile is None:
            return None
        return str(profile.project_endpoint)


def _entry_for(
    entries: tuple[CandidateReconcileEntry, ...],
    candidate_id: str | None,
) -> CandidateReconcileEntry | None:
    if candidate_id is None:
        return None
    for entry in entries:
        if entry.candidate_id == candidate_id:
            return entry
    return None


def _parent_summary_body(
    state: CampaignState,
    finalized: FinalizedPublication,
    selected: CandidateReconcileEntry,
    lineage: OptimizationDeploymentLineage,
    lineage_sha256: str,
    merge_commit: str,
    outcome: DeploymentOutcome,
    post_deploy: PostDeployOutcome,
) -> str:
    lines = [
        f"<!-- foundry-opt:reconciled:{state.campaign_id} -->",
        "## Optimization result",
        f"- Campaign: `{state.campaign_id}`",
        f"- Spec SHA-256: `{state.spec_sha256}`",
        f"- Selected candidate: `{selected.candidate_id}`",
        f"- Candidate pull request: #{selected.pull_request.number}",
        f"- Merge commit: `{merge_commit}`",
        f"- Selected tree: `{lineage.selected_tree_sha}`",
        f"- Patch SHA-256: `{lineage.patch_sha256}`",
        f"- Evidence SHA-256: `{lineage.evidence_sha256}`",
        f"- Deployment lineage SHA-256: `{lineage_sha256}`",
        f"- Campaign pull request: "
        f"#{finalized.campaign_pull_request_number}",
    ]
    if outcome.version is not None:
        lines.append(f"- Deployed version: `{outcome.version}`")
    if outcome.run_url is not None:
        lines.append(f"- Deployment run: {outcome.run_url}")
    if outcome.portal_url is not None:
        lines.append(f"- Foundry portal: {outcome.portal_url}")
    metrics_line = _post_deploy_metrics_line(post_deploy)
    if metrics_line is not None:
        lines.append(metrics_line)
    return "\n".join(lines) + "\n"


def _post_deploy_metrics_line(post_deploy: PostDeployOutcome) -> str | None:
    if not post_deploy.metrics:
        return None
    rendered = ", ".join(
        f"{name}={value}"
        for name, value in sorted(post_deploy.metrics.items())
    )
    return f"- Post-deployment metrics: {rendered}"


def _parent_close_comment(
    candidate_id: str,
    outcome: DeploymentOutcome,
) -> str:
    version = (
        f" as version {outcome.version}"
        if outcome.version is not None
        else ""
    )
    return (
        f"Candidate `{candidate_id}` was deployed{version} and confirmed as a "
        "retained improvement. Closing the optimization issue."
    )


def _superseded_comment(selected_candidate_id: str) -> str:
    return (
        f"Superseded by the selected candidate `{selected_candidate_id}`, "
        "which was merged and deployed. Closing this optimization surface."
    )


def _campaign_close_comment(selected_candidate_id: str) -> str:
    return (
        "The optimization campaign concluded; candidate "
        f"`{selected_candidate_id}` was selected, merged, and deployed. "
        "Closing the temporary campaign pull request."
    )


# ---------------------------------------------------------------------------
# Read-only wrappers for verify-only application
# ---------------------------------------------------------------------------


class _ReadOnlyGateway:
    """Wraps a candidate gateway and forbids every mutating operation."""

    def __init__(self, inner: CandidateGateway) -> None:
        self._inner = inner

    def verify_permissions(
        self,
        required: GitHubCapabilities,
    ) -> GitHubPermissionReport:
        return self._inner.verify_permissions(required)

    def repository_state(self, repository_root: Path):
        return self._inner.repository_state(repository_root)

    def find_candidate_pull_request(
        self,
        repository_root: Path,
        head_branch: str,
    ) -> PullRequestReference | None:
        return self._inner.find_candidate_pull_request(
            repository_root, head_branch
        )

    def create_candidate_pull_request(self, *args: Any, **kwargs: Any):
        raise _VerifyOnlyWriteError("create_candidate_pull_request")

    def comment_issue(self, *args: Any, **kwargs: Any) -> None:
        raise _VerifyOnlyWriteError("comment_issue")

    def close_issue(self, *args: Any, **kwargs: Any) -> None:
        raise _VerifyOnlyWriteError("close_issue")

    def close_pull_request(self, *args: Any, **kwargs: Any) -> None:
        raise _VerifyOnlyWriteError("close_pull_request")


class _ReadOnlyPatchApplier:
    """Wraps a patch applier and forbids every mutating operation."""

    def __init__(self, inner: PatchApplier) -> None:
        self._inner = inner

    def inspect_artifact(self, repository_root: Path, path: Path):
        return self._inner.inspect_artifact(repository_root, path)

    def resolve_tree(self, repository_root: Path, commit: str) -> str | None:
        return self._inner.resolve_tree(repository_root, commit)

    def resolve_branch_commit(
        self,
        repository_root: Path,
        branch: str,
    ) -> str | None:
        return self._inner.resolve_branch_commit(repository_root, branch)

    def apply_exact(self, request: Any):
        raise _VerifyOnlyWriteError("apply_exact")

    def restore_after_publication_failure(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        # A verify-only run performs no writes, so there is nothing to
        # restore; this is a defensive no-op.
        return None


# ---------------------------------------------------------------------------
# Services container + production builder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LifecycleServices:
    apply_service: CandidateApplyService
    reconcile_service: CandidateReconcileService


def build_lifecycle_services(
    config: OptimizerConfig,
    *,
    command_runner: CommandRunner | None = None,
    credential_provider: Any | None = None,
    environment: Any | None = None,
    deployment: DeploymentCoordinator | None = None,
    post_deploy: PostDeployEvaluator | None = None,
    lifecycle_state: LifecycleStateStore | None = None,
    clock: Clock | None = None,
) -> LifecycleServices:
    """Assemble the production APPLY and RECONCILE lifecycle services.

    Every seam is a live production adapter. The GitHub adapters are real: the
    exact-patch applier and campaign gateway publish and verify candidate pull
    requests, and the reconcile gateway performs real merges (or merge-queue
    enrollment) and workflow dispatch without a broad administrative bypass.
    The deployment and post-deployment evaluation bindings are the live
    Azure-OIDC adapters as well:

    * ``deployment`` is the
      :class:`~foundry_opt.adapters.optimization_deployment.LiveDeploymentCoordinator`
      (real ``gh`` workflow-run gateway + Foundry published-version reader).
      The reader authenticates as the dedicated deployment OIDC identity; the
      single shared Azure OIDC credential provider is threaded through a
      :class:`~foundry_opt.optimization.production.DeploymentIdentityCredentialProvider`
      so the reader fails closed unless the reconcile actor is that identity.
      No generated-workflow publisher is wired: a repository whose deployment
      workflow cannot be observed remains an honest ``deployment_workflow_missing``
      blocker rather than a fabricated publication.
    * ``post_deploy`` is the
      :class:`~foundry_opt.adapters.post_deploy_evaluation.LivePostDeployEvaluator`
      (real per-project evaluation binder), reading the *same*
      :class:`~foundry_opt.campaign.state.FileCampaignStateStore` instance the
      lifecycle uses so it replays exactly the persisted campaign selection.

    ``deployment``/``post_deploy`` may still be injected directly (fakes or the
    ``_Unavailable*`` placeholders) for tests; when supplied they replace the
    live adapters. When a live binding's precondition is missing at call time
    (no OIDC identity, unreachable Foundry, missing tool) the adapter raises the
    typed :class:`~foundry_opt.optimization.runner.CapabilityUnavailableError`
    so the reconcile service surfaces an honest ``blocked`` result.
    """

    from foundry_opt.adapters.campaign_git import CampaignGit
    from foundry_opt.adapters.commands import SubprocessCommandRunner
    from foundry_opt.adapters.environment import OsEnvironmentReader
    from foundry_opt.adapters.foundry import AzureCliCredentialProvider
    from foundry_opt.adapters.github_campaign import (
        GhCampaignGateway,
        GitExactPatchApplier,
    )
    from foundry_opt.adapters.github_reconcile import (
        GhCandidateReconcileGateway,
    )
    from foundry_opt.adapters.optimization_deployment import (
        build_live_deployment_coordinator,
    )
    from foundry_opt.adapters.post_deploy_evaluation import (
        build_live_post_deploy_evaluator,
    )
    from foundry_opt.optimization.production import (
        DeploymentIdentityCredentialProvider,
        GitSpecApprovalGateway,
        UtcClock,
    )

    commands = command_runner or SubprocessCommandRunner()
    reader = environment or OsEnvironmentReader()
    credential = credential_provider or AzureCliCredentialProvider(reader)
    campaign_state = _file_campaign_state_store()

    # The deployment coordinator reads published versions as the dedicated
    # deployment OIDC identity; the post-deployment evaluator replays the
    # pinned validation split through the shared Foundry OIDC credential and
    # the same campaign state store the lifecycle persists to.
    deployment_coordinator = deployment or build_live_deployment_coordinator(
        config,
        command_runner=commands,
        credential_provider=DeploymentIdentityCredentialProvider(
            credential, reader
        ),
    )
    post_deploy_evaluator = post_deploy or build_live_post_deploy_evaluator(
        credential,
        state_store=campaign_state,
    )

    dependencies = LifecycleDependencies(
        config=config,
        state=campaign_state,
        lifecycle_state=lifecycle_state or FileLifecycleStateStore(),
        github_gateway_factory=lambda root: GhCampaignGateway(
            commands,
            root,
            granted_capabilities=GitHubCapabilities.CANDIDATE_PUBLICATION,
        ),
        reconcile_gateway_factory=lambda root: GhCandidateReconcileGateway(
            commands,
            root,
            granted_capabilities=(
                GitHubCapabilities.MERGE
                | GitHubCapabilities.DEPLOY_DISPATCH
            ),
        ),
        patch_applier=GitExactPatchApplier(commands),
        repository=CampaignGit(),
        spec_approval=GitSpecApprovalGateway(commands),
        deployment=deployment_coordinator,
        post_deploy=post_deploy_evaluator,
        clock=clock or UtcClock(),
    )
    return LifecycleServices(
        apply_service=CandidateApplyService(dependencies),
        reconcile_service=CandidateReconcileService(dependencies),
    )


def _file_campaign_state_store() -> CampaignStateStore:
    from foundry_opt.campaign.state import FileCampaignStateStore

    return FileCampaignStateStore()


class _UnavailableDeploymentCoordinator:
    """Deployment coordinator placeholder for explicit test injection.

    The production :func:`build_lifecycle_services` factory always wires the
    live :class:`~foundry_opt.adapters.optimization_deployment.LiveDeploymentCoordinator`;
    this placeholder is never used on a production path and exists only so a
    test can inject a deployment seam that reports the live binding as
    unavailable.
    """

    def deploy(
        self,
        request: DeploymentLifecycleRequest,
    ) -> DeploymentOutcome:
        raise CapabilityUnavailableError(
            "deployment_unavailable",
            "observing and verifying the deployment requires the live "
            "Foundry deployment binding (Azure OIDC), which is not wired in "
            "this build",
        )


class _UnavailablePostDeployEvaluator:
    """Post-deploy evaluator placeholder for explicit test injection.

    The production :func:`build_lifecycle_services` factory always wires the
    live :class:`~foundry_opt.adapters.post_deploy_evaluation.LivePostDeployEvaluator`;
    this placeholder is never used on a production path and exists only for
    tests that need an unavailable post-deployment seam.
    """

    def evaluate(self, request: PostDeployRequest) -> PostDeployOutcome:
        raise CapabilityUnavailableError(
            "post_deploy_unavailable",
            "the post-deployment evaluation requires the live Foundry "
            "evaluation binding (Azure OIDC), which is not wired in this "
            "build",
        )
