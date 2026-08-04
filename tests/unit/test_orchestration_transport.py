from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

from foundry_opt.orchestration import (
    CampaignPhase,
    CampaignState,
    CandidateRecord,
    OutboxRecord,
    SpecFileHash,
    StateRefSnapshot,
)
from foundry_opt.orchestration.transport import (
    GhSpecialistWorkerGateway,
    awaiting_specialist_result,
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


def _designer_planned() -> OutboxRecord:
    return OutboxRecord(
        record_id="design-31-2-1-worker",
        kind="specialist_work_request",
        generation=GENERATION,
        sequence=4,
        payload={
            "allowed_mutations": ["system_instructions"],
            "allowed_paths": ["agent"],
            "base_commit": "b" * 40,
            "baseline_metrics": {"quality": 0.5},
            "branch": "foundry-opt/issue-31-g2/candidate-1",
            "candidate_feedback": [],
            "candidate_id": "candidate-1",
            "effect_id": "design-31-2-1",
            "goal": (
                "Improve grounded support answers without weakening safety."
            ),
            "issue_number": ISSUE,
            "reason": "candidate_design_pending",
            "restricted_opt_ins": {},
            "slot": 1,
            "spec_sha256": "a" * 64,
            "specialist": "foundry-candidate-designer",
            "target": "support",
            "work_kind": "design_candidate",
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
        custom_instructions: str,
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
                "environment": environment,
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


def test_candidate_designer_transport_projects_the_typed_intent() -> None:
    class DesignerGateway(Gateway):
        def create_issue(self, *, title: str, body: str, marker: str) -> int:
            assert marker in body
            assert "foundry-candidate-designer" in body
            assert "design_candidate" in body
            assert "design-31-2-1" in body
            assert "Improve grounded support answers" in body
            assert '"allowed_paths":["agent"]' in body
            assert "CandidateDesignResult" in body
            self.issue_number = 85
            return 85

    gateway = DesignerGateway()

    result = SpecialistWorkBridge(gateway).apply(_designer_planned())

    assert result.status is SpecialistWorkBridgeStatus.APPLIED
    assert result.result is not None
    assert result.result.worker_issue_number == 85
    assert gateway.assigned == [(85, "foundry-candidate-designer")]


def test_github_specialist_gateway_uses_remove_reassign_without_pr_api() -> None:
    commands = Commands()
    gateway = GhSpecialistWorkerGateway(
        commands,
        Path("repository"),
        "octo-org/optimizer",
        assignment_token="assignment-token",
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
    assignment_calls = [
        call
        for call in commands.calls
        if call["arguments"][2:4]
        in {("--method", "DELETE"), ("--method", "POST")}
        and call["arguments"][-3].endswith("/assignees")
    ]
    assert assignment_calls
    assert all(
        call["environment"] == {"GH_TOKEN": "assignment-token"}
        for call in assignment_calls
    )
    assert all(
        call["environment"] is None
        for call in commands.calls
        if call not in assignment_calls
    )
    assert "assignment-token" not in repr(result)
    assert all(
        "assignment-token" not in " ".join(call["arguments"])
        and "assignment-token" not in str(call["input_text"])
        for call in commands.calls
    )


def test_github_specialist_gateway_assigns_candidate_designer() -> None:
    commands = Commands()
    gateway = GhSpecialistWorkerGateway(
        commands,
        Path("repository"),
        "octo-org/optimizer",
        assignment_token="assignment-token",
    )

    result = SpecialistWorkBridge(gateway).apply(_designer_planned())

    assert result.status is SpecialistWorkBridgeStatus.APPLIED
    payloads = [
        json.loads(str(call["input_text"]))
        for call in commands.calls
        if call["input_text"] is not None
    ]
    assignment = next(
        payload
        for payload in payloads
        if "agent_assignment" in payload
    )
    assert assignment["agent_assignment"]["custom_agent"] == (
        "foundry-candidate-designer"
    )
    assert "design-31-2-1" in assignment["agent_assignment"][
        "custom_instructions"
    ]
    assert "candidate-design-result" in assignment["agent_assignment"][
        "custom_instructions"
    ]
    assert "--worker-issue 84" in assignment["agent_assignment"][
        "custom_instructions"
    ]
    assert "then stop" in assignment["agent_assignment"][
        "custom_instructions"
    ]
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
    assert first.release_steward is True
    assert second.specialist_statuses == ()
    assert gateway.assigned == [(84, "foundry-optimization-planner")]


def test_transport_waits_for_planner_and_applier_specialists() -> None:
    planner = StateRefSnapshot(
        "a" * 40,
        CampaignState(
            ISSUE,
            GENERATION,
            4,
            CampaignPhase.AWAITING_SPEC_APPROVAL,
            spec_sha256="a" * 64,
            spec_base_ref_name="main",
            spec_head_commit="b" * 40,
            spec_tree_sha="c" * 40,
            spec_files=(
                SpecFileHash(
                    ".foundry-optimizer/specs/issue-31/"
                    "optimization-spec.yaml",
                    "d" * 64,
                ),
            ),
        ),
        (),
        (_planned(),),
    )
    applier = StateRefSnapshot(
        "b" * 40,
        CampaignState(
            ISSUE,
            GENERATION,
            8,
            CampaignPhase.AWAITING_SELECTION,
            spec_sha256="a" * 64,
            baseline_evaluation_id="eval-baseline",
            candidates=(
                CandidateRecord(
                    "candidate-1",
                    True,
                    "e" * 64,
                ),
            ),
        ),
        (),
        (
            OutboxRecord(
                "applier-2-candidate-1",
                "applier_worker_issue_planned",
                GENERATION,
                8,
            ),
        ),
    )

    assert awaiting_specialist_result(planner) is True
    assert awaiting_specialist_result(applier) is True


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


def test_claimed_prior_generation_work_is_not_replayed_after_reopen() -> None:
    planned = _planned()
    claimed = OutboxRecord(
        "spec-planner-2-immutable-asset-claimed",
        "specialist_work_claimed",
        GENERATION,
        4,
        {"effect_id": planned.record_id},
    )
    ledger = Ledger(planned)
    ledger.snapshot = replace(
        ledger.snapshot,
        state=CampaignState(
            issue_number=ISSUE,
            generation=GENERATION + 1,
            sequence=5,
            phase=CampaignPhase.SPECIFICATION,
        ),
        outbox=(planned, claimed),
    )
    gateway = Gateway()

    result = TransportEffectReconciler(
        ledger=ledger,
        specialist=SpecialistWorkBridge(gateway),
    ).reconcile(Path("."), ISSUE)

    assert result.specialist_statuses == ()
    assert gateway.assigned == []


def test_transport_revalidates_phase_before_specialist_side_effect() -> None:
    planned = _planned()

    class CancellingLedger(Ledger):
        def __init__(self, record: OutboxRecord) -> None:
            super().__init__(record)
            self.loads = 0

        def load(self, repository_root: Path, issue_number: int):
            self.loads += 1
            if self.loads >= 2:
                self.snapshot = replace(
                    self.snapshot,
                    state=replace(
                        self.snapshot.state,
                        phase=CampaignPhase.CANCELLED,
                    ),
                )
            return self.snapshot

    ledger = CancellingLedger(planned)
    gateway = Gateway()

    result = TransportEffectReconciler(
        ledger=ledger,
        specialist=SpecialistWorkBridge(gateway),
    ).reconcile(Path("."), ISSUE)

    assert result.specialist_statuses == ()
    assert gateway.assigned == []
