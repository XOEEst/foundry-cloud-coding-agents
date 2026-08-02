from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

from foundry_opt.orchestration import (
    CampaignPhase,
    CampaignState,
    OutboxRecord,
    StateRefSnapshot,
)
from foundry_opt.orchestration.transport import (
    GhSpecialistWorkerGateway,
    SpecialistEffectResultRecorder,
    TransportEffectReconciler,
    SpecialistWorkBridge,
    SpecialistWorkBridgeStatus,
)
from foundry_opt.preflight.interfaces import CommandResult


ISSUE = 31
GENERATION = 2


def _planned() -> OutboxRecord:
    return OutboxRecord(
        record_id="spec-planner-2-immutable-asset",
        kind="specialist_work_request",
        generation=GENERATION,
        sequence=4,
        payload={
            "issue_number": ISSUE,
            "reason": "immutable_asset_unpinned",
            "spec_classification": "human_review",
            "specialist": "foundry-optimization-planner",
            "work_kind": "prepare_specification_pr",
        },
    )


class Gateway:
    def __init__(self) -> None:
        self.issue_number: int | None = None
        self.assigned: list[tuple[int, str]] = []
        self.markers: list[tuple[int, str]] = []

    def find_issue(self, marker: str) -> int | None:
        return self.issue_number

    def create_issue(self, *, title: str, body: str, marker: str) -> int:
        assert marker in body
        assert "prepare_specification_pr" in body
        assert "refs/heads/foundry-opt/state/issue-31" in body
        assert "immutable_asset_unpinned" in body
        self.issue_number = 84
        return 84

    def has_assignment_marker(self, issue_number: int, marker: str) -> bool:
        return (issue_number, marker) in self.markers

    def assign_specialist(
        self,
        issue_number: int,
        *,
        specialist: str,
    ) -> None:
        self.assigned.append((issue_number, specialist))

    def record_assignment_marker(
        self,
        issue_number: int,
        marker: str,
    ) -> None:
        self.markers.append((issue_number, marker))


class Ledger:
    def __init__(self, planned: OutboxRecord) -> None:
        self.snapshot = StateRefSnapshot(
            revision="a" * 40,
            state=CampaignState(
                issue_number=ISSUE,
                generation=GENERATION,
                sequence=4,
                phase=CampaignPhase.SPECIFICATION,
            ),
            inbox=(),
            outbox=(planned,),
        )

    def load(self, repository_root: Path, issue_number: int):
        return self.snapshot

    def commit(self, repository_root: Path, **kwargs):
        self.snapshot = replace(
            self.snapshot,
            revision="b" * 40,
            outbox=(*self.snapshot.outbox, *kwargs.get("outbox", ())),
        )
        return self.snapshot


class Commands:
    def __init__(self) -> None:
        self.responses = [
            "[[]]",
            '{"number":84}',
            '{"assignees":[]}',
            "[[]]",
            "",
            "",
            "",
        ]
        self.calls: list[dict[str, object]] = []

    def run(
        self,
        arguments,
        *,
        cwd=None,
        environment=None,
        input_text=None,
        input_bytes=None,
    ) -> CommandResult:
        self.calls.append(
            {
                "arguments": tuple(arguments),
                "input_text": input_text,
            }
        )
        return CommandResult(0, self.responses.pop(0), "")


def test_specialist_transport_creates_assigns_and_records_once() -> None:
    planned = _planned()
    gateway = Gateway()
    bridge = SpecialistWorkBridge(gateway)
    ledger = Ledger(planned)

    applied = bridge.apply(planned)
    recorded = SpecialistEffectResultRecorder(ledger).record(
        Path("."),
        ISSUE,
        applied.result,
    )
    duplicate = bridge.apply(planned)

    assert applied.status is SpecialistWorkBridgeStatus.APPLIED
    assert recorded.snapshot.outbox[-1].kind == "specialist_work_succeeded"
    assert recorded.snapshot.outbox[-1].payload["worker_issue_number"] == 84
    assert gateway.assigned == [(84, "foundry-optimization-planner")]
    assert duplicate.status is SpecialistWorkBridgeStatus.ALREADY_APPLIED


def test_github_specialist_gateway_uses_remove_reassign_without_pr_api() -> None:
    commands = Commands()
    gateway = GhSpecialistWorkerGateway(
        commands,
        Path("repository"),
        "octo-org/optimizer",
    )
    bridge = SpecialistWorkBridge(gateway)

    result = bridge.apply(_planned())

    assert result.status is SpecialistWorkBridgeStatus.APPLIED
    payloads = [
        json.loads(str(call["input_text"]))
        for call in commands.calls
        if call["input_text"] is not None
    ]
    assert {
        "assignees": ["copilot-swe-agent[bot]"],
    } in payloads
    assert {
        "assignees": ["copilot-swe-agent[bot]"],
        "agent_assignment": {
            "custom_agent": "foundry-optimization-planner",
            "custom_instructions": (
                "Fulfil only the persisted prepare_specification_pr intent."
            ),
            "target_repo": "octo-org/optimizer",
        },
    } in payloads
    assert all(
        "/pulls" not in " ".join(call["arguments"])
        for call in commands.calls
    )


def test_transport_reconciler_applies_only_persisted_specialist_intent() -> None:
    planned = _planned()
    ledger = Ledger(planned)
    gateway = Gateway()
    reconciler = TransportEffectReconciler(
        ledger=ledger,
        specialist=SpecialistWorkBridge(gateway),
    )

    first = reconciler.reconcile(Path("."), ISSUE)
    second = reconciler.reconcile(Path("."), ISSUE)

    assert first.specialist_statuses == (
        SpecialistWorkBridgeStatus.APPLIED,
    )
    assert second.specialist_statuses == ()
    assert gateway.assigned == [(84, "foundry-optimization-planner")]


def test_specialist_acknowledgement_is_idempotent_after_state_advances() -> None:
    planned = _planned()
    gateway = Gateway()
    applied = SpecialistWorkBridge(gateway).apply(planned)
    ledger = Ledger(planned)
    first = SpecialistEffectResultRecorder(ledger).record(
        Path("."),
        ISSUE,
        applied.result,
    )
    ledger.snapshot = replace(
        first.snapshot,
        state=replace(first.snapshot.state, sequence=5),
    )

    duplicate = SpecialistEffectResultRecorder(ledger).record(
        Path("."),
        ISSUE,
        applied.result,
    )

    assert duplicate.status.value == "already_recorded"


def test_specialist_worker_marker_is_bound_to_root_issue() -> None:
    class MarkerGateway(Gateway):
        def __init__(self) -> None:
            super().__init__()
            self.by_marker: dict[str, int] = {}

        def find_issue(self, marker: str) -> int | None:
            return self.by_marker.get(marker)

        def create_issue(self, *, title: str, body: str, marker: str) -> int:
            number = 80 + len(self.by_marker)
            self.by_marker[marker] = number
            return number

    first = _planned()
    second = OutboxRecord(
        record_id=first.record_id,
        kind=first.kind,
        generation=first.generation,
        sequence=first.sequence,
        payload={**dict(first.payload), "issue_number": 32},
    )
    gateway = MarkerGateway()
    bridge = SpecialistWorkBridge(gateway)

    assert bridge.apply(first).status is SpecialistWorkBridgeStatus.APPLIED
    assert bridge.apply(second).status is SpecialistWorkBridgeStatus.APPLIED
    assert len(gateway.by_marker) == 2


def test_claimed_specialist_effect_is_acknowledged_after_supersession() -> None:
    planned = _planned()
    ledger = Ledger(planned)
    delegate = SpecialistWorkBridge(Gateway())

    class SupersedingBridge:
        def apply(self, record):
            ledger.snapshot = replace(
                ledger.snapshot,
                revision="c" * 40,
                state=CampaignState(
                    issue_number=ISSUE,
                    generation=GENERATION + 1,
                    sequence=5,
                    phase=CampaignPhase.SPECIFICATION,
                ),
            )
            return delegate.apply(record)

    result = TransportEffectReconciler(
        ledger=ledger,
        specialist=SupersedingBridge(),
    ).reconcile(Path("."), ISSUE)

    assert result.specialist_statuses == (
        SpecialistWorkBridgeStatus.APPLIED,
    )
    assert any(
        record.kind == "specialist_work_succeeded"
        for record in ledger.snapshot.outbox
    )
