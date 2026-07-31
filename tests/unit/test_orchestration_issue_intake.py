from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

import pytest

from foundry_opt.orchestration import EventKind
from foundry_opt.orchestration.issue_intake import (
    GhStewardAssignments,
    IssueEventIntake,
    TrustedEventContext,
    TrustedIssueEventError,
)
from foundry_opt.preflight.interfaces import CommandResult


def _payload(action: str, *, state: str = "open") -> dict[str, object]:
    return {
        "action": action,
        "repository": {
            "id": 123,
            "full_name": "octo-org/optimizer",
        },
        "issue": {
            "number": 31,
            "state": state,
            "updated_at": "2026-07-31T10:00:00Z",
            "title": "[Optimize] Improve support quality",
        },
    }


def _context(delivery: str, *, event_name: str = "issues"):
    return TrustedEventContext(
        event_name=event_name,
        delivery_id=delivery,
        repository="octo-org/optimizer",
        repository_id=123,
    )


@dataclass
class FakeInbox:
    recorded: dict[int, list] = field(default_factory=dict)

    def events(self, issue_number: int):
        return tuple(self.recorded.get(issue_number, ()))

    def append(self, issue_number: int, event):
        events = self.recorded.setdefault(issue_number, [])
        if event.event_id in {item.event_id for item in events}:
            return False
        events.append(event)
        return True

    def issue_numbers(self):
        return tuple(sorted(self.recorded))


@dataclass
class FakeAssignments:
    assigned: list[tuple[int, str]] = field(default_factory=list)

    def assign(self, issue_number: int, idempotency_key: str) -> bool:
        if (issue_number, idempotency_key) in self.assigned:
            return False
        self.assigned.append((issue_number, idempotency_key))
        return True


@dataclass
class FakeProjection:
    projected: list[int] = field(default_factory=list)

    def project(self, issue_number: int) -> None:
        self.projected.append(issue_number)


class FakeCommands:
    def __init__(
        self,
        responses: dict[tuple[str, ...], str] | None = None,
    ) -> None:
        self.responses = responses or {}
        self.calls: list[dict[str, object]] = []

    def run(
        self,
        arguments,
        *,
        cwd: Path | None = None,
        environment=None,
        input_text: str | None = None,
        input_bytes: bytes | None = None,
    ) -> CommandResult:
        self.calls.append(
            {
                "arguments": tuple(arguments),
                "cwd": cwd,
                "input_text": input_text,
            }
        )
        return CommandResult(
            0,
            self.responses.get(tuple(arguments), ""),
            "",
        )


def test_trusted_issue_events_are_recorded_without_domain_decisions() -> None:
    inbox = FakeInbox()
    assignments = FakeAssignments()
    projection = FakeProjection()
    intake = IssueEventIntake(inbox, assignments, projection)

    opened = intake.ingest(
        _payload("opened"),
        _context("11111111-1111-4111-8111-111111111111"),
    )
    edited = intake.ingest(
        _payload("edited"),
        _context("22222222-2222-4222-8222-222222222222"),
    )
    closed = intake.ingest(
        _payload("closed", state="closed"),
        _context("33333333-3333-4333-8333-333333333333"),
    )
    reopened = intake.ingest(
        _payload("reopened"),
        _context("44444444-4444-4444-8444-444444444444"),
    )

    assert [item.kind for item in inbox.events(31)] == [
        EventKind.ISSUE_CREATED,
        EventKind.ISSUE_EDITED,
        EventKind.ISSUE_CLOSED,
        EventKind.ISSUE_REOPENED,
    ]
    assert [item.generation for item in inbox.events(31)] == [1, 2, 2, 3]
    assert opened.recorded is True
    assert edited.recorded is True
    assert closed.recorded is True
    assert reopened.recorded is True
    assert assignments.assigned == [
        (31, "github-11111111-1111-4111-8111-111111111111"),
        (31, "github-22222222-2222-4222-8222-222222222222"),
        (31, "github-33333333-3333-4333-8333-333333333333"),
        (31, "github-44444444-4444-4444-8444-444444444444"),
    ]
    assert projection.projected == [31, 31, 31, 31]


def test_duplicate_delivery_is_idempotent() -> None:
    inbox = FakeInbox()
    assignments = FakeAssignments()
    projection = FakeProjection()
    intake = IssueEventIntake(inbox, assignments, projection)
    context = _context("11111111-1111-4111-8111-111111111111")

    first = intake.ingest(_payload("opened"), context)
    duplicate = intake.ingest(_payload("opened"), context)

    assert first.recorded is True
    assert duplicate.recorded is False
    assert len(inbox.events(31)) == 1
    assert assignments.assigned == [
        (31, "github-11111111-1111-4111-8111-111111111111")
    ]
    assert projection.projected == [31, 31]


def test_distinct_run_ids_preserve_same_timestamp_edits() -> None:
    inbox = FakeInbox()
    intake = IssueEventIntake(
        inbox,
        FakeAssignments(),
        FakeProjection(),
    )
    intake.ingest(
        _payload("opened"),
        _context("1001"),
    )

    first = intake.ingest(_payload("edited"), _context("1002"))
    second = intake.ingest(_payload("edited"), _context("1003"))

    assert first.event.event_id == "github-1002"
    assert second.event.event_id == "github-1003"
    assert [event.generation for event in inbox.events(31)] == [1, 2, 3]


def test_prefix_removal_declassifies_existing_optimization() -> None:
    inbox = FakeInbox()
    intake = IssueEventIntake(
        inbox,
        FakeAssignments(),
        FakeProjection(),
    )
    intake.ingest(_payload("opened"), _context("1001"))
    declassified = intake.ingest(
        {
            **_payload("edited"),
            "issue": {
                **_payload("edited")["issue"],
                "title": "No longer an optimization",
            },
        },
        _context("1002"),
    )

    assert declassified.event.kind is EventKind.ISSUE_DECLASSIFIED
    assert declassified.event.generation == 1


def test_close_and_reopen_still_route_after_prefix_removal() -> None:
    inbox = FakeInbox()
    intake = IssueEventIntake(
        inbox,
        FakeAssignments(),
        FakeProjection(),
    )
    intake.ingest(_payload("opened"), _context("1001"))
    without_prefix = {
        **_payload("edited"),
        "issue": {
            **_payload("edited")["issue"],
            "title": "No longer an optimization",
        },
    }
    intake.ingest(without_prefix, _context("1002"))
    closed = intake.ingest(
        {
            **_payload("closed", state="closed"),
            "issue": {
                **_payload("closed", state="closed")["issue"],
                "title": "No longer an optimization",
            },
        },
        _context("1003"),
    )
    reopened = intake.ingest(
        {
            **_payload("reopened"),
            "issue": {
                **_payload("reopened")["issue"],
                "title": "No longer an optimization",
            },
        },
        _context("1004"),
    )

    assert closed.event.kind is EventKind.ISSUE_CLOSED
    assert reopened.event.kind is EventKind.ISSUE_REOPENED
    assert reopened.event.generation == 2


@pytest.mark.parametrize(
    ("payload", "context", "message"),
    (
        (
            _payload("labeled"),
            _context("11111111-1111-4111-8111-111111111111"),
            "action",
        ),
        (
            _payload("opened"),
            _context(
                "11111111-1111-4111-8111-111111111111",
                event_name="pull_request",
            ),
            "event name",
        ),
        (
            {
                **_payload("opened"),
                "repository": {
                    "id": 999,
                    "full_name": "octo-org/optimizer",
                },
            },
            _context("11111111-1111-4111-8111-111111111111"),
            "repository identity",
        ),
        (
            {
                **_payload("opened"),
                "issue": {
                    **_payload("opened")["issue"],
                    "title": "Ordinary issue",
                },
            },
            _context("11111111-1111-4111-8111-111111111111"),
            "optimization title",
        ),
    ),
)
def test_untrusted_issue_events_fail_closed(payload, context, message) -> None:
    intake = IssueEventIntake(
        FakeInbox(),
        FakeAssignments(),
        FakeProjection(),
    )

    with pytest.raises(TrustedIssueEventError, match=message):
        intake.ingest(payload, context)


def test_scheduled_recovery_reassigns_open_and_unconsumed_cancellation() -> None:
    inbox = FakeInbox()
    assignments = FakeAssignments()
    projection = FakeProjection()
    intake = IssueEventIntake(inbox, assignments, projection)
    intake.ingest(
        _payload("opened"),
        _context("11111111-1111-4111-8111-111111111111"),
    )
    intake.ingest(
        {**_payload("opened"), "issue": {**_payload("opened")["issue"], "number": 32}},
        _context("22222222-2222-4222-8222-222222222222"),
    )
    intake.ingest(
        {
            **_payload("closed", state="closed"),
            "issue": {
                **_payload("closed", state="closed")["issue"],
                "number": 32,
            },
        },
        _context("33333333-3333-4333-8333-333333333333"),
    )
    assignments.assigned.clear()
    projection.projected.clear()

    intake.recover("schedule-9001")

    assert assignments.assigned == [
        (31, "schedule-9001-issue-31"),
        (32, "schedule-9001-issue-32"),
    ]
    assert projection.projected == [31, 32]


def test_steward_assignment_uses_fixed_custom_agent_request() -> None:
    comments = (
        "gh",
        "api",
        "--paginate",
        "--slurp",
        "repos/octo-org/optimizer/issues/31/comments",
    )
    commands = FakeCommands(
        {
            comments: "[]",
        }
    )
    assignments = GhStewardAssignments(
        commands,
        Path("repository"),
        "octo-org/optimizer",
    )

    assert assignments.assign(31, "github-run-42") is True

    call = next(
        item
        for item in commands.calls
        if item["arguments"][2:4] == ("--method", "POST")
        and item["arguments"][-3].endswith("/assignees")
    )
    assert call["arguments"] == (
        "gh",
        "api",
        "--method",
        "POST",
        "repos/octo-org/optimizer/issues/31/assignees",
        "--input",
        "-",
    )
    assert json.loads(str(call["input_text"])) == {
        "assignees": ["copilot-swe-agent[bot]"],
        "agent_assignment": {
            "target_repo": "octo-org/optimizer",
            "custom_agent": "foundry-optimization-steward",
            "custom_instructions": (
                "Advance this campaign only from its trusted Git-state inbox."
            ),
        },
    }
    remove = next(
        item
        for item in commands.calls
        if item["arguments"][2:4] == ("--method", "DELETE")
    )
    assert json.loads(str(remove["input_text"])) == {
        "assignees": ["copilot-swe-agent[bot]"]
    }
    marker = "<!-- foundry-opt:steward-trigger:github-run-42 -->"
    assert any(
        json.loads(str(item["input_text"])) == {"body": marker}
        for item in commands.calls
        if item["input_text"] is not None
    )


def test_steward_retrigger_marker_is_idempotent() -> None:
    comments = (
        "gh",
        "api",
        "--paginate",
        "--slurp",
        "repos/octo-org/optimizer/issues/31/comments",
    )
    marker = "<!-- foundry-opt:steward-trigger:github-run-42 -->"
    commands = FakeCommands(
        {
            comments: json.dumps(
                [[
                    {
                        "body": marker,
                        "user": {"login": "github-actions[bot]"},
                    }
                ]]
            )
        }
    )
    assignments = GhStewardAssignments(
        commands,
        Path("repository"),
        "octo-org/optimizer",
    )

    assert assignments.assign(31, "github-run-42") is False
    assert [call["arguments"] for call in commands.calls] == [comments]
