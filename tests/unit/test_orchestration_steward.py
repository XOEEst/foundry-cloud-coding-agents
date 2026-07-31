from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from foundry_opt.orchestration import (
    CampaignEvent,
    EventKind,
    OptimizationCampaign,
    StateRefConflictError,
)
from foundry_opt.orchestration.steward import (
    GitCampaignInbox,
    StewardAdvanceRequest,
    StewardAdvanceService,
    StewardAdvanceStatus,
)


NOW = datetime(2026, 7, 31, tzinfo=UTC)


def _event(
    event_id: str,
    kind: EventKind,
    *,
    generation: int = 1,
) -> CampaignEvent:
    return CampaignEvent(event_id, kind, generation, NOW)


class Ledger:
    def __init__(self, snapshot=None, *, conflict: bool = False) -> None:
        self.snapshot = snapshot
        self.conflict = conflict
        self.commits: list[dict[str, object]] = []

    def load(self, repository_root: Path, issue_number: int):
        return self.snapshot

    def commit(self, repository_root: Path, **kwargs):
        if self.conflict:
            raise StateRefConflictError("state ref changed")
        self.commits.append(kwargs)
        return type(
            "Snapshot",
            (),
            {
                "revision": "b" * 40,
                "state": kwargs["state"],
                "inbox": kwargs["inbox"],
                "outbox": kwargs["outbox"],
            },
        )()


class Inbox:
    def __init__(self, events: tuple[CampaignEvent, ...]) -> None:
        self.events = events

    def consume(self, request: StewardAdvanceRequest, snapshot):
        return self.events


def test_steward_advances_events_and_persists_state_and_outbox(
    tmp_path: Path,
) -> None:
    ledger = Ledger()
    service = StewardAdvanceService(
        ledger=ledger,
        inbox=Inbox((_event("event-1", EventKind.ISSUE_CREATED),)),
    )

    result = service.advance(StewardAdvanceRequest(tmp_path, 31))

    assert result.status is StewardAdvanceStatus.ADVANCED
    assert result.phase == "specification"
    assert result.disposition == "advance"
    assert result.revision == "b" * 40
    assert len(ledger.commits) == 1
    commit = ledger.commits[0]
    assert commit["expected_revision"] is None
    assert commit["state"] == result.state
    assert commit["inbox"][0].event_id == "event-1"
    assert commit["outbox"][0].kind == "campaign_advanced"


def test_steward_duplicate_event_is_a_no_write_wait(
    tmp_path: Path,
) -> None:
    event = _event("event-1", EventKind.ISSUE_CREATED)
    state = OptimizationCampaign().advance(
        __import__(
            "foundry_opt.orchestration",
            fromlist=["AdvanceRequest"],
        ).AdvanceRequest(31, None, (event,))
    ).state
    snapshot = type(
        "Snapshot",
        (),
        {
            "revision": "a" * 40,
            "state": state,
            "inbox": (event,),
            "outbox": (),
        },
    )()
    ledger = Ledger(snapshot)
    service = StewardAdvanceService(
        ledger=ledger,
        inbox=Inbox((event, event)),
    )

    result = service.advance(StewardAdvanceRequest(tmp_path, 31))

    assert result.status is StewardAdvanceStatus.WAITING
    assert result.revision == "a" * 40
    assert ledger.commits == []


def test_steward_records_stale_event_as_consumed_wait(
    tmp_path: Path,
) -> None:
    from foundry_opt.orchestration import AdvanceRequest

    created = _event("event-1", EventKind.ISSUE_CREATED)
    edited = _event("event-2", EventKind.ISSUE_EDITED)
    state = OptimizationCampaign().advance(
        AdvanceRequest(31, None, (created, edited))
    ).state
    snapshot = type(
        "Snapshot",
        (),
        {
            "revision": "a" * 40,
            "state": state,
            "inbox": (created, edited),
            "outbox": (),
        },
    )()
    stale = _event(
        "event-stale",
        EventKind.ISSUE_CLOSED,
        generation=1,
    )
    ledger = Ledger(snapshot)

    result = StewardAdvanceService(
        ledger=ledger,
        inbox=Inbox((stale,)),
    ).advance(StewardAdvanceRequest(tmp_path, 31))

    assert result.status is StewardAdvanceStatus.WAITING
    assert len(ledger.commits) == 1
    assert ledger.commits[0]["inbox"] == (stale,)
    assert ledger.commits[0]["outbox"][0].kind == "campaign_waiting"


def test_steward_no_event_waits_without_writing(
    tmp_path: Path,
) -> None:
    event = _event("event-1", EventKind.ISSUE_CREATED)
    from foundry_opt.orchestration import AdvanceRequest

    state = OptimizationCampaign().advance(
        AdvanceRequest(31, None, (event,))
    ).state
    snapshot = type(
        "Snapshot",
        (),
        {
            "revision": "a" * 40,
            "state": state,
            "inbox": (event,),
            "outbox": (),
        },
    )()
    ledger = Ledger(snapshot)

    result = StewardAdvanceService(
        ledger=ledger,
        inbox=Inbox(()),
    ).advance(StewardAdvanceRequest(tmp_path, 31))

    assert result.status is StewardAdvanceStatus.WAITING
    assert result.exit_code == 0
    assert ledger.commits == []


def test_steward_cas_conflict_is_typed_strict_failure(
    tmp_path: Path,
) -> None:
    result = StewardAdvanceService(
        ledger=Ledger(conflict=True),
        inbox=Inbox((_event("event-1", EventKind.ISSUE_CREATED),)),
    ).advance(StewardAdvanceRequest(tmp_path, 31))

    assert result.status is StewardAdvanceStatus.CONFLICT
    assert result.exit_code == 1
    assert result.to_dict()["code"] == "state_ref_conflict"


def test_steward_without_state_or_events_is_blocked(
    tmp_path: Path,
) -> None:
    result = StewardAdvanceService(
        ledger=Ledger(),
        inbox=Inbox(()),
    ).advance(StewardAdvanceRequest(tmp_path, 31))

    assert result.status is StewardAdvanceStatus.BLOCKED
    assert result.exit_code == 1
    assert result.to_dict()["code"] == "campaign_not_initialized"


def test_git_campaign_inbox_reads_transport_events_for_requested_issue(
    tmp_path: Path,
) -> None:
    event = _event("event-1", EventKind.ISSUE_CREATED)

    class RecordedInbox:
        def events(self, issue_number: int):
            assert issue_number == 31
            return (event,)

    inbox = GitCampaignInbox(
        factory=lambda repository_root: RecordedInbox()
    )

    assert inbox.consume(
        StewardAdvanceRequest(tmp_path, 31),
        None,
    ) == (event,)
