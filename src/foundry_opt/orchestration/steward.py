from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
from pathlib import Path
from typing import Any, Callable, Protocol

from foundry_opt.orchestration.campaign import (
    InvalidCampaignTransition,
    OptimizationCampaign,
)
from foundry_opt.orchestration.git_state import (
    GitStateRef,
    OutboxRecord,
    StateRefConflictError,
    StateRefError,
    StateRefSnapshot,
)
from foundry_opt.orchestration.models import (
    AdvanceDisposition,
    AdvanceRequest,
    CampaignEvent,
    CampaignState,
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

    def __post_init__(self) -> None:
        if self.issue_number < 1:
            raise ValueError("issue_number must be positive")


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
    ) -> StateRefSnapshot: ...


class StewardAdvanceService:
    """Consume campaign events and atomically advance the steward ledger."""

    def __init__(
        self,
        *,
        ledger: CampaignLedger | None = None,
        inbox: CampaignInbox | None = None,
        campaign: OptimizationCampaign | None = None,
    ) -> None:
        self._ledger = ledger or GitStateRef()
        self._inbox = inbox or EmptyCampaignInbox()
        self._campaign = campaign or OptimizationCampaign()

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
            if event.event_id in seen:
                continue
            seen.add(event.event_id)
            pending.append(event)

        if not pending:
            if snapshot is None:
                return self._failure(
                    request,
                    StewardAdvanceStatus.BLOCKED,
                    "The campaign has not received an issue-created event.",
                    "campaign_not_initialized",
                )
            return self._result(
                request,
                StewardAdvanceStatus.WAITING,
                "No new campaign events.",
                snapshot.state,
                AdvanceDisposition.WAIT,
                snapshot.revision,
            )

        try:
            advanced = self._campaign.advance(
                AdvanceRequest(
                    issue_number=request.issue_number,
                    state=snapshot.state if snapshot is not None else None,
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

        status = _status(advanced.disposition)
        outbox = (
            OutboxRecord(
                record_id=_outbox_id(advanced.state, tuple(pending)),
                kind=(
                    "campaign_waiting"
                    if advanced.disposition is AdvanceDisposition.WAIT
                    else "campaign_advanced"
                ),
                generation=advanced.state.generation,
                sequence=advanced.state.sequence,
                payload={
                    "disposition": advanced.disposition.value,
                    "issue_number": request.issue_number,
                    "phase": advanced.state.phase.value,
                    "status": status.value,
                },
            ),
        )
        try:
            persisted = self._ledger.commit(
                request.repository_root,
                issue_number=request.issue_number,
                expected_revision=(
                    snapshot.revision if snapshot is not None else None
                ),
                state=advanced.state,
                inbox=tuple(pending),
                outbox=outbox,
            )
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
        return self._result(
            request,
            status,
            _summary(status, advanced.state),
            advanced.state,
            advanced.disposition,
            persisted.revision,
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
) -> str:
    identity = "\n".join(event.event_id for event in events)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return (
        f"advance-{state.generation}-{state.sequence}-{digest}"
    )


def _git_issue_event_inbox(repository_root: Path) -> Any:
    from foundry_opt.orchestration.issue_intake import GitIssueEventInbox

    return GitIssueEventInbox(repository_root)
