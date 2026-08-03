from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import json
from pathlib import Path

import pytest

from foundry_opt.orchestration import (
    AdvanceRequest,
    CampaignEvent,
    CampaignPhase,
    CampaignState,
    EventKind,
    OptimizationCampaign,
    OutboxRecord,
    StateRefSnapshot,
)
from foundry_opt.orchestration.issue_intake import (
    GhStewardAssignments,
    GitStateCampaignRecovery,
    IssueInboxConcurrencyError,
    IssueEventIntake,
    recovery_issue_numbers,
    TrustedEventContext,
    TrustedIssueEventError,
    specification_pull_request_event_from_payload,
    specification_pull_request_issue_from_payload,
    main as issue_intake_main,
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


def _optimization_body(
    goal: str = "Improve support response quality safely.",
) -> str:
    return f"""### Configured target

support-agent

### Optimization goal

{goal}

### Dataset requests

- asset_id: development
  source: repository
  role: development
  path: data/development.jsonl
- asset_id: validation
  source: repository
  role: validation
  path: data/validation.jsonl

### Evaluator requests

- asset_id: task-quality
  source: builtin
  name: task-quality
  version: v1
  metrics: [quality]

### Metric policies

quality:
  direction: maximize
  threshold: 0.8
  materiality: 0.05
  hard_guardrail: false
  undefined_behavior: fail

### Allowed mutations

- system_instructions

### Candidate decision

human

### Deployment decision

human
"""


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

    def append(self, issue_number: int, event, *, issue=None):
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
    live_leases: set[int] = field(default_factory=set)

    def assign(self, issue_number: int, idempotency_key: str) -> bool:
        if (issue_number, idempotency_key) in self.assigned:
            return False
        self.assigned.append((issue_number, idempotency_key))
        return True

    def has_live_lease(self, issue_number: int) -> bool:
        return issue_number in self.live_leases


@dataclass
class FakeProjection:
    projected: list[int] = field(default_factory=list)

    def project(self, issue_number: int) -> None:
        self.projected.append(issue_number)


@dataclass
class FakeRecovery:
    active: set[int]

    def should_recover(self, issue_number: int) -> bool:
        return issue_number in self.active


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
                "environment": environment,
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


def test_issue_intake_fails_early_without_assignment_secret(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRUSTED_EVENT_NAME", "schedule")
    monkeypatch.setenv("TRUSTED_REPOSITORY", "octo-org/optimizer")
    monkeypatch.setenv("TRUSTED_REPOSITORY_ID", "123")
    monkeypatch.setenv("TRUSTED_RUN_ID", "42")
    monkeypatch.delenv("COPILOT_ASSIGNMENT_TOKEN", raising=False)

    with pytest.raises(
        TrustedIssueEventError,
        match=(
            "required Actions secret is missing: "
            "COPILOT_ASSIGNMENT_TOKEN"
        ),
    ):
        issue_intake_main()


def test_duplicate_delivery_is_idempotent() -> None:
    inbox = FakeInbox()
    assignments = FakeAssignments()
    projection = FakeProjection()
    intake = IssueEventIntake(inbox, assignments, projection)
    context = _context("11111111-1111-4111-8111-111111111111")
    payload = {
        **_payload("opened"),
        "issue": {
            **_payload("opened")["issue"],
            "body": _optimization_body(),
        },
    }

    first = intake.ingest(payload, context)
    duplicate = intake.ingest(payload, context)

    assert first.recorded is True
    assert duplicate.recorded is False
    assert len(inbox.events(31)) == 1
    assert assignments.assigned == [
        (31, "github-11111111-1111-4111-8111-111111111111")
    ]
    assert projection.projected == [31, 31]

    changed = {
        **payload,
        "issue": {
            **payload["issue"],
            "body": _optimization_body(
                "Improve edited support response quality safely."
            ),
        },
    }
    with pytest.raises(
        TrustedIssueEventError,
        match="reused for different issue content",
    ):
        intake.ingest(changed, context)


def test_new_event_preserves_active_steward_lease() -> None:
    inbox = FakeInbox()
    assignments = FakeAssignments()
    intake = IssueEventIntake(
        inbox,
        assignments,
        FakeProjection(),
    )
    intake.ingest(_payload("opened"), _context("1001"))
    assignments.live_leases.add(31)

    intake.ingest(_payload("edited"), _context("1002"))

    assert assignments.assigned == [(31, "github-1001")]
    assert [event.event_id for event in inbox.events(31)] == [
        "github-1001",
        "github-1002",
    ]


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


def test_concurrent_issue_edit_recomputes_generation_after_cas_loss() -> None:
    class RacingInbox(FakeInbox):
        raced = False

        def append(self, issue_number: int, event):
            if event.kind is EventKind.ISSUE_EDITED and not self.raced:
                self.raced = True
                self.recorded[issue_number].append(
                    replace(
                        event,
                        event_id="github-competing-edit",
                    )
                )
                raise IssueInboxConcurrencyError(
                    "concurrent issue event"
                )
            return super().append(issue_number, event)

    inbox = RacingInbox()
    intake = IssueEventIntake(
        inbox,
        FakeAssignments(),
        FakeProjection(),
    )
    intake.ingest(_payload("opened"), _context("1001"))

    result = intake.ingest(_payload("edited"), _context("1002"))

    assert result.event.generation == 3
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


def test_scheduled_recovery_skips_terminal_campaigns_and_live_leases() -> None:
    inbox = FakeInbox(recorded={31: [object()], 32: [object()], 33: [object()]})
    assignments = FakeAssignments(live_leases={32})
    projection = FakeProjection()
    intake = IssueEventIntake(
        inbox,
        assignments,
        projection,
        recovery=FakeRecovery({31, 32}),
    )

    intake.recover("schedule-9002")

    assert assignments.assigned == [(31, "schedule-9002-issue-31")]
    assert projection.projected == [31]


def test_durable_recovery_uses_state_and_unprocessed_inbox_not_labels() -> None:
    closed = CampaignEvent(
        event_id="github-closed",
        kind=EventKind.ISSUE_CLOSED,
        generation=1,
        occurred_at=datetime(2026, 7, 31, tzinfo=UTC),
    )
    reopened = CampaignEvent(
        event_id="github-reopened",
        kind=EventKind.ISSUE_REOPENED,
        generation=2,
        occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    inbox = FakeInbox(recorded={31: [closed]})

    class Ledger:
        def __init__(self) -> None:
            self.snapshot = StateRefSnapshot(
                revision="a" * 40,
                state=CampaignState(
                    issue_number=31,
                    generation=1,
                    sequence=2,
                    phase=CampaignPhase.CANCELLED,
                    processed_event_ids=(closed.event_id,),
                ),
                inbox=(closed,),
                outbox=(),
            )

        def load(self, repository_root: Path, issue_number: int):
            return self.snapshot

    ledger = Ledger()
    recovery = GitStateCampaignRecovery(Path("."), inbox, ledger)

    assert recovery.should_recover(31) is False

    stale = CampaignEvent(
        event_id="github-stale",
        kind=EventKind.SPEC_PR_CLOSED,
        generation=1,
        occurred_at=datetime(2026, 7, 31, tzinfo=UTC),
        payload={
            "head_commit": "b" * 40,
            "pull_request_number": 91,
            "spec_sha256": "a" * 64,
        },
    )
    inbox.recorded[31].append(stale)
    ledger.snapshot = replace(
        ledger.snapshot,
        inbox=(closed, stale),
        state=CampaignState(
            issue_number=31,
            generation=1,
            sequence=3,
            phase=CampaignPhase.BLOCKED,
            processed_event_ids=(closed.event_id,),
            block_reason="no_eligible_candidates",
        ),
    )

    assert recovery.should_recover(31) is False

    inbox.recorded[31].append(reopened)

    assert recovery.should_recover(31) is True


def test_recovery_waits_for_candidate_designer_then_resumes_submission() -> None:
    created = CampaignEvent(
        event_id="github-opened",
        kind=EventKind.ISSUE_CREATED,
        generation=1,
        occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    inbox = FakeInbox(recorded={31: [created]})
    planned = OutboxRecord(
        "design-31-1-1-worker",
        "specialist_work_request",
        1,
        2,
        {
            "allowed_mutations": ["system_instructions"],
            "allowed_paths": ["agent"],
            "base_commit": "b" * 40,
            "baseline_metrics": {"quality": 0.5},
            "branch": "foundry-opt/issue-31-g1/candidate-1",
            "candidate_feedback": [],
            "candidate_id": "candidate-1",
            "effect_id": "design-31-1-1",
            "goal": (
                "Improve grounded support answers without weakening safety."
            ),
            "issue_number": 31,
            "reason": "candidate_design_pending",
            "restricted_opt_ins": {},
            "slot": 1,
            "spec_sha256": "a" * 64,
            "specialist": "foundry-candidate-designer",
            "target": "support",
            "work_kind": "design_candidate",
        },
    )

    class Ledger:
        snapshot = StateRefSnapshot(
            "a" * 40,
            CampaignState(
                31,
                1,
                2,
                CampaignPhase.CANDIDATES,
                processed_event_ids=(created.event_id,),
                spec_sha256="a" * 64,
                baseline_evaluation_id="eval-baseline",
            ),
            (created,),
            (planned,),
        )

        def load(self, repository_root: Path, issue_number: int):
            return self.snapshot

    ledger = Ledger()
    recovery = GitStateCampaignRecovery(Path("."), inbox, ledger)

    assert recovery.should_recover(31) is False

    submitted = OutboxRecord(
        "design-31-1-1-submitted",
        "candidate_design_submitted",
        1,
        2,
        {
            "base_commit": "b" * 40,
            "candidate_id": "candidate-1",
            "changed_paths": ["agent/instructions.md"],
            "complexity": "small",
            "effect_id": "design-31-1-1",
            "head_commit": "c" * 40,
            "idea_id": "idea-1",
            "issue_number": 31,
            "lessons": ["The baseline omits an escalation rule."],
            "motivation": "Clarify the escalation rule.",
            "mutation_class": "system_instructions",
            "parent_idea_ids": [],
            "ref": (
                "refs/heads/foundry-opt/design/"
                "issue-31/design-31-1-1"
            ),
            "required_opt_ins": [],
            "result_id": "designer-result-1",
            "slot": 1,
            "spec_sha256": "a" * 64,
            "tree_sha": "d" * 40,
            "worker_issue_number": 84,
        },
    )
    ledger.snapshot = replace(
        ledger.snapshot,
        outbox=(*ledger.snapshot.outbox, submitted),
    )

    assert recovery.should_recover(31) is True


def test_recovery_retry_scope_validates_issue_and_state_ref() -> None:
    assert recovery_issue_numbers(
        requested_issue="31",
        state_ref="main",
        tracked=(31, 42),
    ) == (31,)
    assert recovery_issue_numbers(
        requested_issue=None,
        state_ref="foundry-opt/state/issue-42",
        tracked=(31, 42),
    ) == (42,)

    with pytest.raises(TrustedIssueEventError, match="issue number"):
        recovery_issue_numbers(
            requested_issue="31;echo owned",
            state_ref="main",
            tracked=(31,),
        )
    with pytest.raises(TrustedIssueEventError, match="not tracked"):
        recovery_issue_numbers(
            requested_issue="99",
            state_ref="main",
            tracked=(31,),
        )


def test_specification_pull_request_event_routes_only_trusted_marker() -> None:
    payload = {
        "action": "closed",
        "repository": {
            "id": 123,
            "full_name": "octo-org/optimizer",
        },
        "pull_request": {
            "body": (
                "<!-- foundry-opt:spec:issue-31 -->\n"
                "Generation: `2`\n"
                f"Spec SHA-256: `{'a' * 64}`"
            ),
        },
    }
    context = _context("9003", event_name="pull_request")

    assert specification_pull_request_issue_from_payload(
        payload,
        context,
    ) == 31

    spoofed = {
        **payload,
        "repository": {
            "id": 999,
            "full_name": "octo-org/optimizer",
        },
    }
    with pytest.raises(TrustedIssueEventError, match="repository identity"):
        specification_pull_request_issue_from_payload(spoofed, context)


def test_specification_pull_request_is_normalized_as_transport_event() -> None:
    payload = {
        "action": "closed",
        "repository": {
            "id": 123,
            "full_name": "octo-org/optimizer",
        },
        "pull_request": {
            "number": 91,
            "body": (
                "<!-- foundry-opt:spec:issue-31 -->\n"
                "Generation: `2`\n"
                f"Spec SHA-256: `{'a' * 64}`"
            ),
            "head": {"sha": "b" * 40},
            "merged": True,
            "merge_commit_sha": "c" * 40,
            "state": "closed",
            "updated_at": "2026-08-01T10:00:00Z",
        },
    }
    context = _context("9004", event_name="pull_request")

    issue_number, event = specification_pull_request_event_from_payload(
        payload,
        context,
    )

    assert issue_number == 31
    assert event.kind is EventKind.SPEC_PR_MERGED
    assert event.generation == 2
    assert event.payload == {
        "head_commit": "b" * 40,
        "merge_commit": "c" * 40,
        "pull_request_number": 91,
        "spec_sha256": "a" * 64,
    }


def test_specification_pull_request_rejects_ambiguous_marker_metadata() -> None:
    payload = {
        "action": "opened",
        "repository": {
            "id": 123,
            "full_name": "octo-org/optimizer",
        },
        "pull_request": {
            "number": 91,
            "body": (
                "<!-- foundry-opt:spec:issue-31 -->\n"
                "Generation: `1`\n"
                "Generation: `2`\n"
                f"Spec SHA-256: `{'a' * 64}`"
            ),
            "head": {"sha": "b" * 40},
            "merged": False,
            "state": "open",
            "updated_at": "2026-08-01T10:00:00Z",
        },
    }

    with pytest.raises(TrustedIssueEventError, match="metadata is ambiguous"):
        specification_pull_request_event_from_payload(
            payload,
            _context("9005", event_name="pull_request"),
        )


def test_specification_pull_request_edit_uses_previous_trusted_marker() -> None:
    previous = (
        "<!-- foundry-opt:spec:issue-31 -->\n"
        "Generation: `2`\n"
        f"Spec SHA-256: `{'a' * 64}`"
    )
    payload = {
        "action": "edited",
        "changes": {"body": {"from": previous}},
        "repository": {
            "id": 123,
            "full_name": "octo-org/optimizer",
        },
        "pull_request": {
            "number": 91,
            "body": "marker removed",
            "head": {"sha": "b" * 40},
            "merged": False,
            "state": "open",
            "updated_at": "2026-08-01T10:00:00Z",
        },
    }

    issue_number, event = specification_pull_request_event_from_payload(
        payload,
        _context("9006", event_name="pull_request"),
    )

    assert issue_number == 31
    assert event.kind is EventKind.SPEC_PR_EDITED
    assert event.generation == 2


def test_specification_pull_request_event_is_a_domain_neutral_wakeup() -> None:
    campaign = OptimizationCampaign()
    created = CampaignEvent(
        event_id="github-opened",
        kind=EventKind.ISSUE_CREATED,
        generation=1,
        occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    state = campaign.advance(
        AdvanceRequest(ISSUE := 31, None, (created,))
    ).state
    observed = CampaignEvent(
        event_id="github-spec-pr",
        kind=EventKind.SPEC_PR_OPENED,
        generation=1,
        occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
        payload={
            "head_commit": "b" * 40,
            "pull_request_number": 91,
            "spec_sha256": "a" * 64,
        },
    )

    result = campaign.advance(
        AdvanceRequest(ISSUE, state, (observed,))
    )

    assert result.state.phase is CampaignPhase.SPECIFICATION
    assert result.state.processed_event_ids[-1] == observed.event_id


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
        assignment_token="assignment-token",
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
                "Run `foundry-opt steward advance --issue 31 --json` "
                "exactly once, report only its persisted result, then stop. "
                "Do not inspect or edit source, tests, configuration, or "
                "the session pull request."
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
    assignment_calls = [
        item
        for item in commands.calls
        if item["arguments"][2:4]
        in {("--method", "DELETE"), ("--method", "POST")}
        and item["arguments"][-3].endswith("/assignees")
    ]
    assert assignment_calls
    assert all(
        item["environment"] == {"GH_TOKEN": "assignment-token"}
        for item in assignment_calls
    )
    marker = "<!-- foundry-opt:steward-trigger:github-run-42 -->"
    marker_call = next(
        item
        for item in commands.calls
        if item["input_text"] is not None
        and json.loads(str(item["input_text"])) == {"body": marker}
    )
    assert marker_call["environment"] is None
    assert all(
        "assignment-token" not in " ".join(item["arguments"])
        and "assignment-token" not in str(item["input_text"])
        for item in commands.calls
    )


def test_steward_assignment_can_release_a_delegated_session() -> None:
    commands = FakeCommands()
    assignments = GhStewardAssignments(
        commands,
        Path("repository"),
        "octo-org/optimizer",
        assignment_token="assignment-token",
    )

    assignments.release(31)

    assert commands.calls == [
        {
            "arguments": (
                "gh",
                "api",
                "--method",
                "DELETE",
                "repos/octo-org/optimizer/issues/31/assignees",
                "--input",
                "-",
            ),
            "cwd": Path("repository"),
            "environment": {"GH_TOKEN": "assignment-token"},
            "input_text": '{"assignees":["copilot-swe-agent[bot]"]}',
        }
    ]


def test_steward_live_lease_uses_assignee_not_mutable_labels() -> None:
    issue = (
        "gh",
        "api",
        "repos/octo-org/optimizer/issues/31",
    )
    comments = (
        "gh",
        "api",
        "--paginate",
        "--slurp",
        "repos/octo-org/optimizer/issues/31/comments",
    )
    commands = FakeCommands(
        {
            issue: json.dumps(
                {
                    "assignees": [
                        {"login": "copilot-swe-agent[bot]"},
                    ],
                    "labels": [],
                }
            ),
            comments: json.dumps(
                [[
                    {
                        "body": (
                            "<!-- foundry-opt:steward-trigger:run-1 -->"
                        ),
                        "created_at": "2026-08-01T09:30:00Z",
                        "user": {"login": "github-actions[bot]"},
                    }
                ]]
            ),
        }
    )
    assignments = GhStewardAssignments(
        commands,
        Path("repository"),
        "octo-org/optimizer",
        assignment_token="assignment-token",
        clock=lambda: datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
    )

    assert assignments.has_live_lease(31) is True
    assert [call["arguments"] for call in commands.calls] == [
        issue,
        comments,
    ]


def test_steward_lease_expires_even_when_bot_remains_assigned() -> None:
    issue = (
        "gh",
        "api",
        "repos/octo-org/optimizer/issues/31",
    )
    comments = (
        "gh",
        "api",
        "--paginate",
        "--slurp",
        "repos/octo-org/optimizer/issues/31/comments",
    )
    commands = FakeCommands(
        {
            issue: json.dumps(
                {
                    "assignees": [
                        {"login": "copilot-swe-agent[bot]"},
                    ],
                }
            ),
            comments: json.dumps(
                [[
                    {
                        "body": (
                            "<!-- foundry-opt:steward-trigger:run-1 -->"
                        ),
                        "created_at": "2026-08-01T08:00:00Z",
                        "user": {"login": "github-actions[bot]"},
                    }
                ]]
            ),
        }
    )
    assignments = GhStewardAssignments(
        commands,
        Path("repository"),
        "octo-org/optimizer",
        assignment_token="assignment-token",
        clock=lambda: datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
    )

    assert assignments.has_live_lease(31) is False


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
        assignment_token="assignment-token",
    )

    assert assignments.assign(31, "github-run-42") is False
    assert [call["arguments"] for call in commands.calls] == [comments]
