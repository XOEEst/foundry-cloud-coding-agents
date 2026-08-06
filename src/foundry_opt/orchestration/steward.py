from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
from pathlib import Path
import re
from typing import Any, Callable, Protocol

from foundry_opt.preflight.redaction import redact
from foundry_opt.security import reject_secret_content
from foundry_opt.orchestration.campaign import (
    InvalidCampaignTransition,
    OptimizationCampaign,
)
from foundry_opt.orchestration.candidate_workers import (
    CandidateWorkerRequest,
    CandidateWorkerResult,
    CandidateWorkerStatus,
)
from foundry_opt.orchestration.candidate_slate import (
    CandidateSelectionResult,
    CandidateSelectionStatus,
    CandidateSlateRequest,
    CandidateSlateResult,
    CandidateSlateStatus,
)
from foundry_opt.orchestration.git_state import (
    GitStateRef,
    OutboxRecord,
    StateRefConflictError,
    StateRefError,
    StateRefPushUnacknowledgedError,
    StateRefSnapshot,
    StateObject,
)
from foundry_opt.orchestration.deployment import (
    DeploymentOrchestrationRequest,
    DeploymentOrchestrationResult,
    DeploymentOrchestrationStatus,
)
from foundry_opt.orchestration.models import (
    AdvanceDisposition,
    AdvanceRequest,
    CampaignEvent,
    CampaignPhase,
    CampaignState,
    EventKind,
)
from foundry_opt.orchestration.spec_policy import (
    SpecPolicyDecision,
    SpecPolicyRequest,
)


_MAX_EXCEPTION_DETAIL = 240
_URL = re.compile(r"(?i)\b(?:https?|ssh|git)://\S+")
_PRIVATE_CONTENT = re.compile(
    r"(?i)\b(?:prompt|raw[_ -]?(?:row|response)s?|held[- ]out|trace)\b"
)
_ABSOLUTE_PATH = re.compile(r"(?:\b[A-Za-z]:\\|(?<!\w)/(?:home|users|var)/)")
_OPAQUE_TOKEN = re.compile(r"\b[A-Za-z0-9_-]{32,}\b")
_WORKTREE_FAILURE = re.compile(
    r"(?i)(?:"
    r"\bfatal:|"
    r"\b(?:campaign|candidate|managed|registered|optimizer|git)\s+worktree\b|"
    r"\bworktree\s+(?:path|repository|branch|cleanup)\b|"
    r"\brepository_root\b|"
    r"\bstate ref\b|"
    r"\bcampaign state\b|"
    r"\bgit (?:command|transport|operation)\b|"
    r"\bremote default branch\b"
    r")"
)


def _exception_class(error: Exception) -> str:
    name = type(error).__name__
    return name if name.isidentifier() else "Exception"


def _safe_exception_detail(
    error: Exception,
    *,
    include_message: bool,
) -> str:
    error_class = _exception_class(error)
    if not include_message:
        return error_class
    message = " ".join(str(error).split())
    sanitized = redact(message) or ""
    sanitized = _URL.sub("[REDACTED]", sanitized)
    if (
        not sanitized
        or _PRIVATE_CONTENT.search(sanitized)
        or _ABSOLUTE_PATH.search(sanitized)
        or _OPAQUE_TOKEN.search(sanitized)
    ):
        return error_class
    try:
        reject_secret_content(sanitized)
    except ValueError:
        return error_class
    if len(sanitized) > _MAX_EXCEPTION_DETAIL:
        sanitized = sanitized[: _MAX_EXCEPTION_DETAIL - 1].rstrip() + "…"
    return f"{error_class}: {sanitized}"


def _failure_summary(
    summary: str,
    error: Exception,
    *,
    include_message: bool = False,
) -> str:
    return (
        f"{summary} "
        f"({_safe_exception_detail(error, include_message=include_message)})"
    )


def _looks_like_worktree_failure(error: Exception) -> bool:
    if not isinstance(error, (RuntimeError, ValueError)):
        return False
    return _WORKTREE_FAILURE.search(str(error)) is not None


def _candidate_worker_failure(error: Exception) -> tuple[str, str]:
    from pydantic import ValidationError

    from foundry_opt.adapters.drafts import (
        DraftApiError,
        DraftAuthenticationError,
        DraftAuthorizationError,
        DraftConflictError,
        DraftError,
        DraftHashMismatchError,
        DraftResponseError,
    )
    from foundry_opt.adapters.evaluation import EvaluationAdapterError
    from foundry_opt.adapters.foundry_assets import (
        FoundryAssetAuthenticationError,
        FoundryAssetAuthorizationError,
        FoundryAssetGatewayError,
        FoundryAssetServiceError,
        FoundryAssetThrottledError,
        FoundryAssetTransportError,
        FoundryAssetUnexpectedSdkError,
    )
    from foundry_opt.adapters.optimization_evaluation import (
        OptimizationEvaluationError,
    )
    from foundry_opt.campaign.protocols import (
        ActiveCampaignError,
        CampaignStateError,
        UnsafeMutationError,
    )
    from foundry_opt.evidence.writer import SensitiveEvidenceError
    from foundry_opt.optimization.assets import EvaluationAssetError
    from foundry_opt.optimization.runner import (
        CampaignRecoveryError,
        CapabilityUnavailableError,
        IdeaContractError,
    )
    from foundry_opt.orchestration.git_transport import GitTransportError
    from foundry_opt.packaging.models import BundleError

    code = "candidate_workers_unavailable"
    summary = "Candidate workers could not be advanced."
    if isinstance(error, CapabilityUnavailableError):
        capability = error.code.casefold()
        if any(
            marker in capability
            for marker in ("asset", "registration", "resolution")
        ):
            code = "candidate_assets_unavailable"
            summary = "Candidate assets could not be prepared."
        elif "draft" in capability:
            code = "candidate_draft_unavailable"
            summary = "Candidate draft could not be created."
        elif "evaluation" in capability:
            code = "candidate_evaluation_unavailable"
            summary = "Candidate evaluation could not be completed."
        elif "evidence" in capability:
            code = "candidate_evidence_unavailable"
            summary = "Candidate evidence could not be produced."
        elif "validation" in capability:
            code = "candidate_validation_failed"
            summary = "Candidate validation failed."
        elif any(
            marker in capability
            for marker in ("git", "worktree", "state", "repository", "campaign")
        ):
            code = "candidate_worktree_failed"
            summary = "Candidate worktree or state operation failed."
    elif isinstance(
        error,
        (FoundryAssetGatewayError, EvaluationAssetError),
    ):
        code = "candidate_assets_unavailable"
        summary = "Candidate assets could not be prepared."
    elif isinstance(error, DraftError):
        code = "candidate_draft_unavailable"
        summary = "Candidate draft could not be created."
    elif isinstance(
        error,
        (EvaluationAdapterError, OptimizationEvaluationError),
    ):
        code = "candidate_evaluation_unavailable"
        summary = "Candidate evaluation could not be completed."
    elif isinstance(error, SensitiveEvidenceError):
        code = "candidate_evidence_unavailable"
        summary = "Candidate evidence could not be produced."
    elif isinstance(
        error,
        (
            StateRefError,
            GitTransportError,
            ActiveCampaignError,
            CampaignStateError,
            CampaignRecoveryError,
        ),
    ):
        code = "candidate_worktree_failed"
        summary = "Candidate worktree or state operation failed."
    elif isinstance(
        error,
        (
            ValidationError,
            IdeaContractError,
            UnsafeMutationError,
            BundleError,
        ),
    ):
        code = "candidate_validation_failed"
        summary = "Candidate validation failed."
    elif _looks_like_worktree_failure(error):
        code = "candidate_worktree_failed"
        summary = "Candidate worktree or state operation failed."
    elif isinstance(error, ValueError):
        code = "candidate_validation_failed"
        summary = "Candidate validation failed."
    include_message = type(error) in (
        DraftApiError,
        DraftAuthenticationError,
        DraftAuthorizationError,
        DraftConflictError,
        DraftHashMismatchError,
        DraftResponseError,
        FoundryAssetAuthenticationError,
        FoundryAssetAuthorizationError,
        FoundryAssetServiceError,
        FoundryAssetThrottledError,
        FoundryAssetTransportError,
        FoundryAssetUnexpectedSdkError,
    )
    return code, _failure_summary(
        summary,
        error,
        include_message=include_message,
    )


class StewardAdvanceStatus(StrEnum):
    ADVANCED = "advanced"
    WAITING = "waiting"
    COMPLETE = "complete"
    BLOCKED = "blocked"
    CONFLICT = "conflict"
    FAILED = "failed"


@dataclass(frozen=True)
class StewardAdvanceRequest:
    repository_root: Path
    issue_number: int
    session_deadline: datetime | None = None

    def __post_init__(self) -> None:
        if self.issue_number < 1:
            raise ValueError("issue_number must be positive")
        if (
            self.session_deadline is not None
            and self.session_deadline.tzinfo is None
        ):
            raise ValueError("session_deadline must be timezone-aware")


@dataclass(frozen=True)
class StewardAdvanceResult:
    status: StewardAdvanceStatus
    issue_number: int
    summary: str
    phase: str | None = None
    disposition: str | None = None
    revision: str | None = None
    code: str | None = None
    state: CampaignState | None = None

    @property
    def exit_code(self) -> int:
        return (
            0
            if self.status
            in {
                StewardAdvanceStatus.ADVANCED,
                StewardAdvanceStatus.WAITING,
                StewardAdvanceStatus.COMPLETE,
            }
            else 1
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "disposition": self.disposition,
            "issue_number": self.issue_number,
            "phase": self.phase,
            "revision": self.revision,
            "status": self.status.value,
            "summary": self.summary,
        }


class CampaignInbox(Protocol):
    def consume(
        self,
        request: StewardAdvanceRequest,
        snapshot: StateRefSnapshot | None,
    ) -> tuple[CampaignEvent, ...]: ...


class EmptyCampaignInbox:
    def consume(
        self,
        request: StewardAdvanceRequest,
        snapshot: StateRefSnapshot | None,
    ) -> tuple[CampaignEvent, ...]:
        return ()


class GitCampaignInbox:
    """Read trusted transport events from the issue-intake Git ref."""

    def __init__(
        self,
        *,
        factory: Callable[[Path], Any] | None = None,
    ) -> None:
        self._factory = factory or _git_issue_event_inbox

    def consume(
        self,
        request: StewardAdvanceRequest,
        snapshot: StateRefSnapshot | None,
    ) -> tuple[CampaignEvent, ...]:
        return tuple(
            self._factory(request.repository_root).events(
                request.issue_number
            )
        )


class CampaignLedger(Protocol):
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
        objects: tuple[StateObject, ...] = (),
    ) -> StateRefSnapshot: ...


class StewardStateHandoffs(Protocol):
    def persist_state(
        self,
        repository_root: Path,
        error: StateRefPushUnacknowledgedError,
    ) -> Any: ...


class StewardSpecPolicy(Protocol):
    def evaluate(
        self,
        request: SpecPolicyRequest,
    ) -> SpecPolicyDecision | None: ...


class StewardCandidateWorkers(Protocol):
    def advance(
        self,
        request: CandidateWorkerRequest,
    ) -> CandidateWorkerResult: ...


class StewardCandidateSlate(Protocol):
    def advance(
        self,
        request: CandidateSlateRequest,
    ) -> CandidateSlateResult: ...


class StewardCandidateSelection(Protocol):
    def advance(
        self,
        request: CandidateSlateRequest,
    ) -> CandidateSelectionResult: ...


class StewardDeployment(Protocol):
    def advance(
        self,
        request: DeploymentOrchestrationRequest,
    ) -> DeploymentOrchestrationResult: ...


class StewardAdvanceService:
    """Consume campaign events and atomically advance the steward ledger."""

    def __init__(
        self,
        *,
        ledger: CampaignLedger | None = None,
        inbox: CampaignInbox | None = None,
        campaign: OptimizationCampaign | None = None,
        spec_policy: StewardSpecPolicy | None = None,
        candidate_workers: StewardCandidateWorkers | None = None,
        candidate_slate: StewardCandidateSlate | None = None,
        candidate_selection: StewardCandidateSelection | None = None,
        deployment: StewardDeployment | None = None,
        handoffs: StewardStateHandoffs | None = None,
    ) -> None:
        self._ledger = ledger or GitStateRef()
        self._inbox = inbox or EmptyCampaignInbox()
        self._campaign = campaign or OptimizationCampaign()
        self._spec_policy = spec_policy
        self._candidate_workers = candidate_workers
        self._candidate_slate = candidate_slate
        self._candidate_selection = candidate_selection
        self._deployment = deployment
        self._handoffs = handoffs

    def advance(
        self,
        request: StewardAdvanceRequest,
        *,
        events: tuple[CampaignEvent, ...] = (),
    ) -> StewardAdvanceResult:
        try:
            snapshot = self._ledger.load(
                request.repository_root,
                request.issue_number,
            )
            consumed = (
                *self._inbox.consume(request, snapshot),
                *events,
            )
        except StateRefError:
            return self._failure(
                request,
                StewardAdvanceStatus.FAILED,
                "The durable campaign state could not be loaded.",
                "state_ref_unavailable",
            )
        except Exception:
            return self._failure(
                request,
                StewardAdvanceStatus.FAILED,
                "Campaign inbox events could not be consumed.",
                "inbox_unavailable",
            )

        existing_ids = (
            {event.event_id for event in snapshot.inbox}
            if snapshot is not None
            else set()
        )
        pending: list[CampaignEvent] = []
        seen = set(existing_ids)
        for event in consumed:
            if (
                self._spec_policy is not None
                and event.kind
                in {
                    EventKind.SPEC_POLICY_APPROVED,
                    EventKind.SPEC_POLICY_BLOCKED,
                    EventKind.SPEC_REVIEW_REQUIRED,
                    EventKind.SPEC_HUMAN_APPROVED,
                }
            ):
                continue
            if event.event_id in seen:
                continue
            seen.add(event.event_id)
            pending.append(event)

        if snapshot is None and not pending:
            return self._failure(
                request,
                StewardAdvanceStatus.BLOCKED,
                "The campaign has not received an issue-created event.",
                "campaign_not_initialized",
            )

        state = snapshot.state if snapshot is not None else None
        disposition = AdvanceDisposition.WAIT
        if pending:
            try:
                advanced = self._campaign.advance(
                    AdvanceRequest(
                        issue_number=request.issue_number,
                        state=state,
                        events=tuple(pending),
                    )
                )
            except (InvalidCampaignTransition, ValueError):
                return self._failure(
                    request,
                    StewardAdvanceStatus.FAILED,
                    "Campaign events violate the orchestration contract.",
                    "invalid_campaign_transition",
                    snapshot,
                )
            state = advanced.state
            disposition = advanced.disposition
        assert state is not None

        policy_decision: SpecPolicyDecision | None = None
        if self._spec_policy is not None:
            try:
                policy_decision = self._spec_policy.evaluate(
                    SpecPolicyRequest(
                        request.repository_root,
                        request.issue_number,
                        state,
                    )
                )
                if (
                    policy_decision is not None
                    and policy_decision.event is not None
                    and policy_decision.event.event_id not in seen
                ):
                    policy_advanced = self._campaign.advance(
                        AdvanceRequest(
                            issue_number=request.issue_number,
                            state=state,
                            events=(policy_decision.event,),
                        )
                    )
                    pending.append(policy_decision.event)
                    seen.add(policy_decision.event.event_id)
                    state = policy_advanced.state
                    disposition = policy_advanced.disposition
                    if (
                        policy_decision.intents
                        and policy_decision.disposition
                        is AdvanceDisposition.DELEGATE
                    ):
                        disposition = AdvanceDisposition.DELEGATE
                elif (
                    policy_decision is not None
                    and policy_decision.intents
                ):
                    disposition = policy_decision.disposition
            except (InvalidCampaignTransition, ValueError):
                return self._failure(
                    request,
                    StewardAdvanceStatus.FAILED,
                    "Specification policy violated the orchestration contract.",
                    "spec_policy_invalid",
                    snapshot,
                )
            except Exception:
                return self._failure(
                    request,
                    StewardAdvanceStatus.FAILED,
                    "Specification policy could not be evaluated.",
                    "spec_policy_unavailable",
                    snapshot,
                )

        existing_outbox_ids = (
            {record.record_id for record in snapshot.outbox}
            if snapshot is not None
            else set()
        )
        intent_outbox = tuple(
            OutboxRecord(
                record_id=intent.intent_id,
                kind=intent.kind,
                generation=state.generation,
                sequence=state.sequence,
                payload=intent.payload,
            )
            for intent in (
                policy_decision.intents if policy_decision is not None else ()
            )
            if intent.intent_id not in existing_outbox_ids
        )
        if not pending and not intent_outbox:
            if (
                snapshot is not None
                and self._candidate_selection is not None
                and state.phase
                in {
                    CampaignPhase.AWAITING_SELECTION,
                    CampaignPhase.DEPLOYMENT,
                }
            ):
                return self._advance_candidate_selection(
                    request,
                    snapshot,
                )
            if (
                snapshot is not None
                and self._candidate_workers is not None
                and state.phase
                in {CampaignPhase.BASELINE, CampaignPhase.CANDIDATES}
            ):
                return self._advance_candidate_workers(
                    request,
                    snapshot,
                )
            if (
                snapshot is not None
                and self._deployment is not None
                and state.phase
                in {
                    CampaignPhase.DEPLOYMENT,
                    CampaignPhase.RETENTION,
                    CampaignPhase.COMPLETED,
                }
            ):
                return self._advance_deployment(request, snapshot)
            if (
                snapshot is not None
                and self._candidate_slate is not None
                and state.phase
                in {
                    CampaignPhase.CANDIDATES,
                    CampaignPhase.AWAITING_SELECTION,
                    CampaignPhase.DEPLOYMENT,
                }
            ):
                return self._advance_candidate_slate(request, snapshot)
            return self._result(
                request,
                StewardAdvanceStatus.WAITING,
                "No new campaign events.",
                state,
                AdvanceDisposition.WAIT,
                snapshot.revision,
            )

        status = _status(disposition)
        preserve_terminal_projection = (
            snapshot is not None
            and bool(pending)
            and not intent_outbox
            and state.phase is snapshot.state.phase
            and state.phase
            in {
                CampaignPhase.BLOCKED,
                CampaignPhase.CANCELLED,
                CampaignPhase.COMPLETED,
            }
        )
        if preserve_terminal_projection:
            outbox = ()
        else:
            dashboard_payload: dict[str, object] = {
                "disposition": disposition.value,
                "issue_number": request.issue_number,
                "phase": state.phase.value,
                "status": status.value,
            }
            if policy_decision is not None:
                dashboard_payload.update(policy_decision.dashboard_payload)
            dashboard = OutboxRecord(
                record_id=_outbox_id(
                    state,
                    tuple(pending),
                    extra=tuple(
                        record.record_id for record in intent_outbox
                    ),
                ),
                kind=(
                    "campaign_waiting"
                    if disposition is AdvanceDisposition.WAIT
                    else "campaign_advanced"
                ),
                generation=state.generation,
                sequence=state.sequence,
                payload=dashboard_payload,
            )
            outbox = (dashboard, *intent_outbox)
        try:
            persisted = self._ledger.commit(
                request.repository_root,
                issue_number=request.issue_number,
                expected_revision=(
                    snapshot.revision if snapshot is not None else None
                ),
                state=state,
                inbox=tuple(pending),
                outbox=outbox,
                objects=(
                    policy_decision.objects
                    if policy_decision is not None
                    else ()
                ),
            )
        except StateRefPushUnacknowledgedError as error:
            return self._state_handoff(request, error, snapshot)
        except StateRefConflictError:
            return self._failure(
                request,
                StewardAdvanceStatus.CONFLICT,
                "The durable campaign state changed concurrently.",
                "state_ref_conflict",
                snapshot,
            )
        except (StateRefError, ValueError):
            return self._failure(
                request,
                StewardAdvanceStatus.FAILED,
                "The advanced campaign state could not be persisted.",
                "state_ref_persist_failed",
                snapshot,
            )
        result = self._result(
            request,
            status,
            _summary(status, state),
            state,
            disposition,
            persisted.revision,
        )
        if (
            self._candidate_selection is not None
            and persisted.state.phase
            in {
                CampaignPhase.AWAITING_SELECTION,
                CampaignPhase.DEPLOYMENT,
            }
        ):
            return self._advance_candidate_selection(request, persisted)
        if (
            self._candidate_workers is not None
            and persisted.state.phase
            in {CampaignPhase.BASELINE, CampaignPhase.CANDIDATES}
        ):
            return self._advance_candidate_workers(request, persisted)
        if (
            self._deployment is not None
            and persisted.state.phase
            in {
                CampaignPhase.DEPLOYMENT,
                CampaignPhase.RETENTION,
                CampaignPhase.COMPLETED,
            }
        ):
            return self._advance_deployment(request, persisted)
        if (
            self._candidate_slate is not None
            and persisted.state.phase
            in {
                CampaignPhase.CANDIDATES,
                CampaignPhase.AWAITING_SELECTION,
                CampaignPhase.DEPLOYMENT,
            }
        ):
            return self._advance_candidate_slate(request, persisted)
        return result

    def _state_handoff(
        self,
        request: StewardAdvanceRequest,
        error: StateRefPushUnacknowledgedError,
        snapshot: StateRefSnapshot | None,
    ) -> StewardAdvanceResult:
        if self._handoffs is None or error.proposal is None:
            return self._failure(
                request,
                StewardAdvanceStatus.FAILED,
                "The advanced campaign state could not be persisted.",
                "state_ref_push_unacknowledged",
                snapshot,
            )
        try:
            self._handoffs.persist_state(request.repository_root, error)
        except StateRefConflictError:
            return self._failure(
                request,
                StewardAdvanceStatus.CONFLICT,
                "The durable campaign state changed concurrently.",
                "state_ref_conflict",
                snapshot,
            )
        except Exception:
            return self._failure(
                request,
                StewardAdvanceStatus.FAILED,
                "The campaign state handoff could not be persisted.",
                "state_handoff_failed",
                snapshot,
            )
        proposed = error.proposal.snapshot
        return StewardAdvanceResult(
            status=StewardAdvanceStatus.WAITING,
            issue_number=request.issue_number,
            summary=(
                "Campaign state is awaiting trusted handoff transport."
            ),
            phase=proposed.state.phase.value,
            disposition=AdvanceDisposition.DELEGATE.value,
            revision=error.expected_revision,
            code="state_handoff_created",
            state=proposed.state,
        )

    def _advance_candidate_workers(
        self,
        request: StewardAdvanceRequest,
        snapshot: StateRefSnapshot,
    ) -> StewardAdvanceResult:
        assert self._candidate_workers is not None
        try:
            result = self._candidate_workers.advance(
                CandidateWorkerRequest(
                    request.repository_root,
                    request.issue_number,
                    request.session_deadline,
                )
            )
        except StateRefPushUnacknowledgedError as error:
            return self._state_handoff(request, error, snapshot)
        except Exception as error:
            code, summary = _candidate_worker_failure(error)
            return self._failure(
                request,
                StewardAdvanceStatus.FAILED,
                summary,
                code,
                snapshot,
            )
        status_by_worker = {
            CandidateWorkerStatus.COMPLETE: StewardAdvanceStatus.ADVANCED,
            CandidateWorkerStatus.WAITING: StewardAdvanceStatus.WAITING,
            CandidateWorkerStatus.BLOCKED: StewardAdvanceStatus.BLOCKED,
            CandidateWorkerStatus.FAILED: StewardAdvanceStatus.FAILED,
            CandidateWorkerStatus.CONFLICT: StewardAdvanceStatus.CONFLICT,
        }
        status = status_by_worker[result.status]
        if (
            result.status is CandidateWorkerStatus.COMPLETE
            and result.snapshot.revision == snapshot.revision
        ):
            status = StewardAdvanceStatus.WAITING
        if (
            result.status is CandidateWorkerStatus.COMPLETE
            and self._candidate_slate is not None
        ):
            return self._advance_candidate_slate(
                request,
                result.snapshot,
            )
        return StewardAdvanceResult(
            status=status,
            issue_number=request.issue_number,
            summary=result.summary,
            phase=result.snapshot.state.phase.value,
            disposition=(
                AdvanceDisposition.WAIT.value
                if status is StewardAdvanceStatus.WAITING
                else (
                    AdvanceDisposition.BLOCKED.value
                    if status is StewardAdvanceStatus.BLOCKED
                    else (
                        AdvanceDisposition.ADVANCE.value
                        if status
                        in {
                            StewardAdvanceStatus.ADVANCED,
                            StewardAdvanceStatus.COMPLETE,
                        }
                        else None
                    )
                )
            ),
            revision=result.snapshot.revision,
            code=result.code,
            state=result.snapshot.state,
        )

    def _advance_candidate_selection(
        self,
        request: StewardAdvanceRequest,
        snapshot: StateRefSnapshot,
    ) -> StewardAdvanceResult:
        assert self._candidate_selection is not None
        try:
            result = self._candidate_selection.advance(
                CandidateSlateRequest(
                    request.repository_root,
                    request.issue_number,
                )
            )
        except StateRefPushUnacknowledgedError as error:
            return self._state_handoff(request, error, snapshot)
        except Exception as error:
            return self._failure(
                request,
                StewardAdvanceStatus.FAILED,
                _failure_summary(
                    "Candidate selection could not be advanced.",
                    error,
                ),
                "candidate_selection_unavailable",
                snapshot,
            )
        status_by_selection = {
            CandidateSelectionStatus.SELECTED: StewardAdvanceStatus.ADVANCED,
            CandidateSelectionStatus.WAITING: StewardAdvanceStatus.WAITING,
            CandidateSelectionStatus.BLOCKED: StewardAdvanceStatus.BLOCKED,
            CandidateSelectionStatus.FAILED: StewardAdvanceStatus.FAILED,
            CandidateSelectionStatus.CONFLICT: StewardAdvanceStatus.CONFLICT,
        }
        status = status_by_selection[result.status]
        if (
            self._deployment is not None
            and result.snapshot.state.phase is CampaignPhase.DEPLOYMENT
            and status
            in {
                StewardAdvanceStatus.ADVANCED,
                StewardAdvanceStatus.WAITING,
            }
        ):
            return self._advance_deployment(
                request,
                result.snapshot,
            )
        return StewardAdvanceResult(
            status=status,
            issue_number=request.issue_number,
            summary=result.summary,
            phase=result.snapshot.state.phase.value,
            disposition=(
                AdvanceDisposition.ADVANCE.value
                if status is StewardAdvanceStatus.ADVANCED
                else (
                    AdvanceDisposition.WAIT.value
                    if status is StewardAdvanceStatus.WAITING
                    else (
                        AdvanceDisposition.BLOCKED.value
                        if status is StewardAdvanceStatus.BLOCKED
                        else None
                    )
                )
            ),
            revision=result.snapshot.revision,
            code=result.code,
            state=result.snapshot.state,
        )

    def _advance_deployment(
        self,
        request: StewardAdvanceRequest,
        snapshot: StateRefSnapshot,
    ) -> StewardAdvanceResult:
        assert self._deployment is not None
        try:
            result = self._deployment.advance(
                DeploymentOrchestrationRequest(
                    request.repository_root,
                    request.issue_number,
                )
            )
        except StateRefPushUnacknowledgedError as error:
            return self._state_handoff(request, error, snapshot)
        except Exception as error:
            return self._failure(
                request,
                StewardAdvanceStatus.FAILED,
                _failure_summary(
                    "Deployment orchestration could not be advanced.",
                    error,
                ),
                "deployment_orchestration_unavailable",
                snapshot,
            )
        status_by_deployment = {
            DeploymentOrchestrationStatus.PLANNED: (
                StewardAdvanceStatus.ADVANCED
            ),
            DeploymentOrchestrationStatus.RETRYING: (
                StewardAdvanceStatus.ADVANCED
            ),
            DeploymentOrchestrationStatus.WAITING: (
                StewardAdvanceStatus.WAITING
            ),
            DeploymentOrchestrationStatus.COMPLETE: (
                StewardAdvanceStatus.COMPLETE
            ),
            DeploymentOrchestrationStatus.READY_FOR_HUMAN: (
                StewardAdvanceStatus.BLOCKED
            ),
            DeploymentOrchestrationStatus.CONFLICT: (
                StewardAdvanceStatus.CONFLICT
            ),
            DeploymentOrchestrationStatus.FAILED: (
                StewardAdvanceStatus.FAILED
            ),
        }
        status = status_by_deployment[result.status]
        return StewardAdvanceResult(
            status=status,
            issue_number=request.issue_number,
            summary=result.summary,
            phase=result.snapshot.state.phase.value,
            disposition=(
                AdvanceDisposition.COMPLETE.value
                if status is StewardAdvanceStatus.COMPLETE
                else (
                    AdvanceDisposition.WAIT.value
                    if status is StewardAdvanceStatus.WAITING
                    else (
                        AdvanceDisposition.BLOCKED.value
                        if status is StewardAdvanceStatus.BLOCKED
                        else (
                            AdvanceDisposition.ADVANCE.value
                            if status is StewardAdvanceStatus.ADVANCED
                            else None
                        )
                    )
                )
            ),
            revision=result.snapshot.revision,
            code=result.code,
            state=result.snapshot.state,
        )

    def _advance_candidate_slate(
        self,
        request: StewardAdvanceRequest,
        snapshot: StateRefSnapshot,
    ) -> StewardAdvanceResult:
        assert self._candidate_slate is not None
        try:
            result = self._candidate_slate.advance(
                CandidateSlateRequest(
                    request.repository_root,
                    request.issue_number,
                )
            )
        except StateRefPushUnacknowledgedError as error:
            return self._state_handoff(request, error, snapshot)
        except Exception as error:
            return self._failure(
                request,
                StewardAdvanceStatus.FAILED,
                _failure_summary(
                    "Candidate slate could not be advanced.",
                    error,
                ),
                "candidate_slate_unavailable",
                snapshot,
            )
        status_by_slate = {
            CandidateSlateStatus.PUBLISHED: StewardAdvanceStatus.ADVANCED,
            CandidateSlateStatus.WAITING: StewardAdvanceStatus.WAITING,
            CandidateSlateStatus.BLOCKED: StewardAdvanceStatus.BLOCKED,
            CandidateSlateStatus.FAILED: StewardAdvanceStatus.FAILED,
            CandidateSlateStatus.CONFLICT: StewardAdvanceStatus.CONFLICT,
        }
        status = status_by_slate[result.status]
        return StewardAdvanceResult(
            status=status,
            issue_number=request.issue_number,
            summary=result.summary,
            phase=result.snapshot.state.phase.value,
            disposition=(
                AdvanceDisposition.ADVANCE.value
                if status is StewardAdvanceStatus.ADVANCED
                else (
                    AdvanceDisposition.WAIT.value
                    if status is StewardAdvanceStatus.WAITING
                    else (
                        AdvanceDisposition.BLOCKED.value
                        if status is StewardAdvanceStatus.BLOCKED
                        else None
                    )
                )
            ),
            revision=result.snapshot.revision,
            code=result.code,
            state=result.snapshot.state,
        )

    def _failure(
        self,
        request: StewardAdvanceRequest,
        status: StewardAdvanceStatus,
        summary: str,
        code: str,
        snapshot: StateRefSnapshot | None = None,
    ) -> StewardAdvanceResult:
        return StewardAdvanceResult(
            status=status,
            issue_number=request.issue_number,
            summary=summary,
            phase=(
                snapshot.state.phase.value
                if snapshot is not None
                else None
            ),
            revision=(
                snapshot.revision if snapshot is not None else None
            ),
            code=code,
            state=snapshot.state if snapshot is not None else None,
        )

    def _result(
        self,
        request: StewardAdvanceRequest,
        status: StewardAdvanceStatus,
        summary: str,
        state: CampaignState,
        disposition: AdvanceDisposition,
        revision: str,
    ) -> StewardAdvanceResult:
        return StewardAdvanceResult(
            status=status,
            issue_number=request.issue_number,
            summary=summary,
            phase=state.phase.value,
            disposition=disposition.value,
            revision=revision,
            state=state,
        )


def _status(disposition: AdvanceDisposition) -> StewardAdvanceStatus:
    if disposition is AdvanceDisposition.WAIT:
        return StewardAdvanceStatus.WAITING
    if disposition is AdvanceDisposition.COMPLETE:
        return StewardAdvanceStatus.COMPLETE
    if disposition is AdvanceDisposition.BLOCKED:
        return StewardAdvanceStatus.BLOCKED
    return StewardAdvanceStatus.ADVANCED


def _summary(
    status: StewardAdvanceStatus,
    state: CampaignState,
) -> str:
    if status is StewardAdvanceStatus.WAITING:
        return f"Campaign is waiting in {state.phase.value}."
    if status is StewardAdvanceStatus.COMPLETE:
        return "Campaign completed."
    if status is StewardAdvanceStatus.BLOCKED:
        return "Campaign is blocked."
    return f"Campaign advanced to {state.phase.value}."


def _outbox_id(
    state: CampaignState,
    events: tuple[CampaignEvent, ...],
    *,
    extra: tuple[str, ...] = (),
) -> str:
    identity = "\n".join(
        (*[event.event_id for event in events], *extra)
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return (
        f"advance-{state.generation}-{state.sequence}-{digest}"
    )


def _git_issue_event_inbox(repository_root: Path) -> Any:
    from foundry_opt.orchestration.issue_intake import GitIssueEventInbox

    return GitIssueEventInbox(repository_root)
