from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path

import pytest

from foundry_opt.evaluation import (
    AgentVersionRef,
    DatasetSplit,
    DatasetVersionRef,
    EvaluationResult,
    EvaluationRun,
    EvaluationStatus,
    EvaluatorDefinitionRef,
    MetricAggregate,
    NormalizedCase,
    NormalizedCaseMetric,
    Outcome,
    Usage,
)
from foundry_opt.orchestration import (
    AdvanceRequest,
    CampaignEvent,
    EventKind,
    OptimizationCampaign,
    OutboxRecord,
    StateObject,
    StateRefConflictError,
    StateRefPrivacyError,
    StateRefPushUnacknowledgedError,
    StateRefSnapshot,
)
from foundry_opt.orchestration.capability_bridge import (
    awaiting_candidate_capability_result,
    CandidateCapabilityBridge,
    CandidateCapabilityExecutionError,
    CandidateCapabilityExecution,
    CandidateCapabilityStatus,
    candidate_capability_issue_numbers,
    evaluation_result_from_state_object,
    evaluation_result_state_object,
    verify_active_optimizer_identity,
    _validate_planned_capability,
    _validate_execution,
)
from foundry_opt.preflight.interfaces import CommandResult


NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
SPEC_SHA256 = "a" * 64
BASE_COMMIT = "b" * 40


class Ledger:
    def __init__(self, snapshot: StateRefSnapshot) -> None:
        self.snapshot = snapshot
        self.commits = 0

    def load(self, repository_root: Path, issue_number: int):
        return self.snapshot

    def commit(self, repository_root: Path, **kwargs):
        assert kwargs["expected_revision"] == self.snapshot.revision
        self.commits += 1
        self.snapshot = StateRefSnapshot(
            f"{self.commits + 1:040x}",
            kwargs["state"],
            self.snapshot.inbox,
            (*self.snapshot.outbox, *kwargs.get("outbox", ())),
            (*self.snapshot.objects, *kwargs.get("objects", ())),
        )
        return self.snapshot


class Executor:
    def __init__(self, execution: CandidateCapabilityExecution) -> None:
        self.execution = execution
        self.reconciles: list[str] = []
        self.executes: list[str] = []

    def reconcile(self, repository_root, snapshot, planned):
        self.reconciles.append(planned.record_id)
        return None

    def execute(self, repository_root, snapshot, planned):
        self.executes.append(planned.record_id)
        return self.execution


class Assignments:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    def resume(self, issue_number: int, idempotency_key: str) -> None:
        self.calls.append((issue_number, idempotency_key))


def _snapshot() -> StateRefSnapshot:
    created = CampaignEvent(
        "created",
        EventKind.ISSUE_CREATED,
        1,
        NOW,
    )
    approved = CampaignEvent(
        "approved",
        EventKind.SPEC_POLICY_APPROVED,
        1,
        NOW,
        {"spec_sha256": SPEC_SHA256},
    )
    state = OptimizationCampaign().advance(
        AdvanceRequest(31, None, (created, approved))
    ).state
    effect_id = f"assets-31-1-{SPEC_SHA256[:16]}"
    intent = StateObject(
        f"objects/capabilities/{effect_id}.json",
        (
            json.dumps(
                {
                    "assets": [
                        {
                            "approval_gate": "policy",
                            "asset_id": "development",
                            "content_sha256": None,
                            "created_by": "foundry-deferred-provider",
                            "kind": "dataset",
                            "metrics": [],
                            "name": "development",
                            "path": None,
                            "remote_id": None,
                            "role": "development",
                            "source": "foundry",
                            "version": "1",
                        }
                    ],
                    "base_commit": BASE_COMMIT,
                    "effect_id": effect_id,
                    "environment": "acceptance",
                    "generation": 1,
                    "issue_number": 31,
                    "kind": "candidate_assets_registration",
                    "schema_version": 1,
                    "spec_sha256": SPEC_SHA256,
                    "target": "support",
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode(),
    )
    planned = OutboxRecord(
        effect_id,
        "candidate_assets_registration_planned",
        1,
        state.sequence,
        {
            "base_commit": BASE_COMMIT,
            "capability_path": intent.path,
            "capability_sha256": intent.sha256,
            "effect_id": effect_id,
            "effect_kind": "foundry_assets",
            "environment": "acceptance",
            "issue_number": 31,
            "max_attempts": 2,
            "spec_sha256": SPEC_SHA256,
            "target": "support",
        },
    )
    return StateRefSnapshot(
        "1" * 40,
        state,
        (created, approved),
        (planned,),
        (intent,),
    )


def test_capability_bridge_claims_executes_records_and_resumes(
    tmp_path: Path,
) -> None:
    effect_id = f"assets-31-1-{SPEC_SHA256[:16]}"
    result_object = StateObject(
        f"objects/capabilities/{effect_id}-result.json",
        (
            json.dumps(
                {
                    "assets": [
                        {
                            "asset_id": "development",
                            "content_sha256": "d" * 64,
                            "kind": "dataset",
                            "name": "dataset-development",
                            "remote_id": "dataset-id",
                            "role": "development",
                            "source": "repository",
                            "version": "d" * 16,
                        }
                    ],
                    "base_commit": BASE_COMMIT,
                    "effect_id": effect_id,
                    "generation": 1,
                    "issue_number": 31,
                    "kind": "candidate_assets_registration_result",
                    "schema_version": 1,
                    "spec_sha256": SPEC_SHA256,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode(),
    )
    execution = CandidateCapabilityExecution(
        record_kind="candidate_assets_registration_succeeded",
        payload={
            "base_commit": BASE_COMMIT,
            "capability_path": result_object.path,
            "capability_sha256": result_object.sha256,
            "effect_id": effect_id,
            "effect_kind": "foundry_assets",
            "issue_number": 31,
            "result_id": f"{effect_id}-result",
            "spec_sha256": SPEC_SHA256,
        },
        objects=(result_object,),
    )
    ledger = Ledger(_snapshot())
    executor = Executor(execution)
    assignments = Assignments()

    result = CandidateCapabilityBridge(
        ledger=ledger,
        executor=executor,
        assignments=assignments,
    ).advance(tmp_path, 31)

    assert result.status is CandidateCapabilityStatus.APPLIED
    assert result.effect_id == effect_id
    assert executor.reconciles == [effect_id]
    assert executor.executes == [effect_id]
    assert ledger.commits == 2
    assert ledger.snapshot.outbox[-2].kind == "candidate_capability_claimed"
    assert ledger.snapshot.outbox[-1].kind == (
        "candidate_assets_registration_succeeded"
    )
    assert ledger.snapshot.objects[-1] == result_object
    assert assignments.calls == [
        (31, f"capability-{effect_id}-succeeded")
    ]


def test_duplicate_capability_delivery_reconciles_persisted_success(
    tmp_path: Path,
) -> None:
    effect_id = f"assets-31-1-{SPEC_SHA256[:16]}"
    result_object = StateObject(
        f"objects/capabilities/{effect_id}-result.json",
        b'{"assets":[],"kind":"candidate_assets_registration_result"}\n',
    )
    execution = CandidateCapabilityExecution(
        record_kind="candidate_assets_registration_succeeded",
        payload={
            "base_commit": BASE_COMMIT,
            "capability_path": result_object.path,
            "capability_sha256": result_object.sha256,
            "effect_id": effect_id,
            "effect_kind": "foundry_assets",
            "issue_number": 31,
            "result_id": f"{effect_id}-result",
            "spec_sha256": SPEC_SHA256,
        },
        objects=(result_object,),
    )
    ledger = Ledger(_snapshot())
    executor = Executor(execution)
    assignments = Assignments()
    bridge = CandidateCapabilityBridge(
        ledger=ledger,
        executor=executor,
        assignments=assignments,
    )

    first = bridge.advance(tmp_path, 31)
    duplicate = bridge.advance(tmp_path, 31)

    assert first.status is CandidateCapabilityStatus.APPLIED
    assert duplicate.status is CandidateCapabilityStatus.ALREADY_APPLIED
    assert executor.executes == [effect_id]
    assert ledger.commits == 2
    assert assignments.calls == [
        (31, f"capability-{effect_id}-succeeded"),
        (31, f"capability-{effect_id}-succeeded"),
    ]


def test_capability_failure_retries_once_then_records_terminal_result(
    tmp_path: Path,
) -> None:
    effect_id = f"assets-31-1-{SPEC_SHA256[:16]}"

    class FailingExecutor:
        def __init__(self) -> None:
            self.calls = 0

        def reconcile(self, repository_root, snapshot, planned):
            return None

        def execute(self, repository_root, snapshot, planned):
            self.calls += 1
            raise CandidateCapabilityExecutionError(
                "foundry_transport_unavailable",
                retryable=True,
            )

    ledger = Ledger(_snapshot())
    executor = FailingExecutor()
    assignments = Assignments()
    bridge = CandidateCapabilityBridge(
        ledger=ledger,
        executor=executor,
        assignments=assignments,
    )

    first = bridge.advance(tmp_path, 31)
    second = bridge.advance(tmp_path, 31)
    duplicate = bridge.advance(tmp_path, 31)

    assert first.status is CandidateCapabilityStatus.RETRY
    assert second.status is CandidateCapabilityStatus.TERMINAL
    assert duplicate.status is CandidateCapabilityStatus.TERMINAL
    assert executor.calls == 2
    failures = [
        record
        for record in ledger.snapshot.outbox
        if record.kind == "candidate_capability_failed"
    ]
    assert [record.payload["attempt"] for record in failures] == [1, 2]
    assert [record.payload["status"] for record in failures] == [
        "retryable",
        "terminal",
    ]
    assert all(
        record.payload["reason"] == "foundry_transport_unavailable"
        for record in failures
    )
    assert assignments.calls == [
        (31, f"capability-{effect_id}-failed"),
        (31, f"capability-{effect_id}-failed"),
    ]


def test_steward_waits_only_for_unresolved_or_retryable_capabilities() -> None:
    pending = _snapshot()
    effect_id = pending.outbox[0].record_id
    retryable = StateRefSnapshot(
        pending.revision,
        pending.state,
        pending.inbox,
        (
            *pending.outbox,
            OutboxRecord(
                f"{effect_id}-failed-1",
                "candidate_capability_failed",
                1,
                pending.state.sequence,
                {
                    "attempt": 1,
                    "base_commit": BASE_COMMIT,
                    "effect_id": effect_id,
                    "effect_kind": "foundry_assets",
                    "issue_number": 31,
                    "max_attempts": 2,
                    "reason": "foundry_transport_unavailable",
                    "spec_sha256": SPEC_SHA256,
                    "status": "retryable",
                },
            ),
        ),
        pending.objects,
    )
    terminal = StateRefSnapshot(
        retryable.revision,
        retryable.state,
        retryable.inbox,
        (
            *retryable.outbox,
            OutboxRecord(
                f"{effect_id}-failed-2",
                "candidate_capability_failed",
                1,
                retryable.state.sequence,
                {
                    "attempt": 2,
                    "base_commit": BASE_COMMIT,
                    "effect_id": effect_id,
                    "effect_kind": "foundry_assets",
                    "issue_number": 31,
                    "max_attempts": 2,
                    "reason": "foundry_transport_unavailable",
                    "spec_sha256": SPEC_SHA256,
                    "status": "terminal",
                },
            ),
        ),
        retryable.objects,
    )

    assert awaiting_candidate_capability_result(pending)
    assert awaiting_candidate_capability_result(retryable)
    assert not awaiting_candidate_capability_result(terminal)


def test_evaluation_result_object_round_trips_only_privacy_safe_evidence() -> None:
    effect_id = "evaluation-31-1-baseline"
    run = EvaluationRun(
        "run-1",
        "evaluation-1",
        "baseline",
        DatasetSplit.DEVELOPMENT,
        AgentVersionRef("support", "draft-baseline", "draft"),
        DatasetVersionRef("dataset-id", "1"),
        EvaluatorDefinitionRef("evaluator-id", "2"),
        EvaluationStatus.COMPLETED,
        "https://ai.azure.com/projects/demo/evaluations/evaluation-1/runs/run-1",
        NOW,
        NOW,
        None,
    )
    original = EvaluationResult(
        run=run,
        cases=(
            NormalizedCase(
                "case-1",
                "c" * 64,
                ("response-1",),
                (
                    NormalizedCaseMetric(
                        "quality",
                        0.9,
                        0.9,
                        "private evaluator prompt must not persist",
                        Outcome.PASS,
                    ),
                ),
                Usage(10, 5, 1),
                None,
                "private provider response must not persist",
                12,
            ),
        ),
        metrics={
            "quality": MetricAggregate(
                "quality",
                0.9,
                0.9,
                0.9,
                0.0,
                Outcome.PASS,
                1,
            )
        },
        usage=Usage(10, 5, 1),
        duration_ms=12,
        errors=("private provider response must not persist",),
        complete=True,
        needs_repeat=False,
        attempts=1,
    )

    state_object = evaluation_result_state_object(
        effect_id=effect_id,
        issue_number=31,
        generation=1,
        spec_sha256=SPEC_SHA256,
        base_commit=BASE_COMMIT,
        idempotency_key="d" * 64,
        result=original,
    )
    restored = evaluation_result_from_state_object(
        state_object,
        effect_id=effect_id,
        issue_number=31,
        generation=1,
        spec_sha256=SPEC_SHA256,
        base_commit=BASE_COMMIT,
        idempotency_key="d" * 64,
    )

    assert b"private evaluator prompt" not in state_object.content
    assert b"private provider response" not in state_object.content
    assert b"access_token" not in state_object.content
    assert json.loads(state_object.content)["idempotency_key"] == "d" * 64
    assert restored.run.run_id == original.run.run_id
    assert restored.run.evaluation_id == original.run.evaluation_id
    assert restored.metrics == original.metrics
    assert [
        (case.case_id, case.case_hash) for case in restored.cases
    ] == [("case-1", "c" * 64)]
    assert restored.cases[0].scores[0].reason is None
    assert restored.cases[0].error == "case_error"
    legacy_document = json.loads(state_object.content)
    legacy_document.pop("idempotency_key")
    legacy_document["schema_version"] = 1
    legacy_object = StateObject(
        state_object.path,
        (
            json.dumps(
                legacy_document,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode(),
    )
    legacy = evaluation_result_from_state_object(
        legacy_object,
        effect_id=effect_id,
        issue_number=31,
        generation=1,
        spec_sha256=SPEC_SHA256,
        base_commit=BASE_COMMIT,
        idempotency_key="d" * 64,
    )
    assert legacy.run == restored.run
    with pytest.raises(ValueError, match="result object is invalid"):
        evaluation_result_from_state_object(
            state_object,
            effect_id=effect_id,
            issue_number=31,
            generation=1,
            spec_sha256=SPEC_SHA256,
            base_commit="c" * 40,
            idempotency_key="d" * 64,
        )


def test_bridge_processes_new_effect_after_an_earlier_success(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    assets_effect = snapshot.outbox[0].record_id
    assets_success = OutboxRecord(
        f"{assets_effect}-succeeded",
        "candidate_assets_registration_succeeded",
        1,
        snapshot.state.sequence,
        {
            "base_commit": BASE_COMMIT,
            "capability_path": (
                f"objects/capabilities/{assets_effect}-result.json"
            ),
            "capability_sha256": "d" * 64,
            "effect_id": assets_effect,
            "effect_kind": "foundry_assets",
            "issue_number": 31,
            "result_id": f"{assets_effect}-result",
            "spec_sha256": SPEC_SHA256,
        },
    )
    draft_effect = "draft-31-1-baseline"
    draft_plan = OutboxRecord(
        draft_effect,
        "candidate_effect_planned",
        1,
        snapshot.state.sequence,
        {
            "base_commit": BASE_COMMIT,
            "bundle_sha256": "e" * 64,
            "candidate_id": "baseline",
            "effect_id": draft_effect,
            "effect_kind": "foundry_draft",
            "idempotency_key": "f" * 64,
            "issue_number": 31,
            "max_attempts": 1,
            "slot": 0,
            "spec_sha256": SPEC_SHA256,
        },
    )
    ledger = Ledger(
        StateRefSnapshot(
            snapshot.revision,
            snapshot.state,
            snapshot.inbox,
            (*snapshot.outbox, assets_success, draft_plan),
            snapshot.objects,
        )
    )
    execution = CandidateCapabilityExecution(
        record_kind="candidate_effect_succeeded",
        payload={
            "base_commit": BASE_COMMIT,
            "bundle_sha256": "e" * 64,
            "candidate_id": "baseline",
            "draft_id": "draft-baseline",
            "effect_id": draft_effect,
            "effect_kind": "foundry_draft",
            "issue_number": 31,
            "spec_sha256": SPEC_SHA256,
        },
    )
    executor = Executor(execution)

    result = CandidateCapabilityBridge(
        ledger=ledger,
        executor=executor,
        assignments=Assignments(),
    ).advance(tmp_path, 31)

    assert result.status is CandidateCapabilityStatus.APPLIED
    assert result.effect_id == draft_effect
    assert executor.executes == [draft_effect]


def test_claim_cas_conflict_prevents_external_operation(
    tmp_path: Path,
) -> None:
    class ConflictingLedger(Ledger):
        def commit(self, repository_root: Path, **kwargs):
            raise StateRefConflictError("changed")

    effect_id = _snapshot().outbox[0].record_id
    result_object = StateObject(
        f"objects/capabilities/{effect_id}-result.json",
        b'{"assets":[],"kind":"candidate_assets_registration_result"}\n',
    )
    executor = Executor(
        CandidateCapabilityExecution(
            record_kind="candidate_assets_registration_succeeded",
            payload={
                "base_commit": BASE_COMMIT,
                "capability_path": result_object.path,
                "capability_sha256": result_object.sha256,
                "effect_id": effect_id,
                "effect_kind": "foundry_assets",
                "issue_number": 31,
                "result_id": f"{effect_id}-result",
                "spec_sha256": SPEC_SHA256,
            },
            objects=(result_object,),
        )
    )

    result = CandidateCapabilityBridge(
        ledger=ConflictingLedger(_snapshot()),
        executor=executor,
        assignments=Assignments(),
    ).advance(tmp_path, 31)

    assert result.status is CandidateCapabilityStatus.CONFLICT
    assert executor.reconciles == []
    assert executor.executes == []


def test_result_ack_loss_reloads_persisted_success_without_duplicate_call(
    tmp_path: Path,
) -> None:
    class AckLossLedger(Ledger):
        def commit(self, repository_root: Path, **kwargs):
            persisted = super().commit(repository_root, **kwargs)
            if self.commits == 2:
                raise StateRefPushUnacknowledgedError(
                    ref="refs/heads/foundry-opt/state/issue-31",
                    expected_revision=kwargs["expected_revision"],
                    proposed_revision=persisted.revision,
                    proposed_tree="f" * 40,
                )
            return persisted

    effect_id = _snapshot().outbox[0].record_id
    result_object = StateObject(
        f"objects/capabilities/{effect_id}-result.json",
        b'{"assets":[],"kind":"candidate_assets_registration_result"}\n',
    )
    executor = Executor(
        CandidateCapabilityExecution(
            record_kind="candidate_assets_registration_succeeded",
            payload={
                "base_commit": BASE_COMMIT,
                "capability_path": result_object.path,
                "capability_sha256": result_object.sha256,
                "effect_id": effect_id,
                "effect_kind": "foundry_assets",
                "issue_number": 31,
                "result_id": f"{effect_id}-result",
                "spec_sha256": SPEC_SHA256,
            },
            objects=(result_object,),
        )
    )
    ledger = AckLossLedger(_snapshot())
    assignments = Assignments()

    result = CandidateCapabilityBridge(
        ledger=ledger,
        executor=executor,
        assignments=assignments,
    ).advance(tmp_path, 31)

    assert result.status is CandidateCapabilityStatus.APPLIED
    assert executor.executes == [effect_id]
    assert ledger.snapshot.outbox[-1].record_id == f"{effect_id}-succeeded"
    assert assignments.calls == [
        (31, f"capability-{effect_id}-succeeded")
    ]


def test_capability_state_objects_reject_raw_rows_prompts_and_tokens() -> None:
    for field in ("dataset_row", "prompt", "token"):
        with pytest.raises(StateRefPrivacyError):
            StateObject(
                "objects/capabilities/private-result.json",
                (
                    json.dumps(
                        {field: "must-never-persist"},
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    + "\n"
                ).encode(),
            )


def test_capability_scope_and_optimizer_identity_are_fail_closed(
    tmp_path: Path,
) -> None:
    assert candidate_capability_issue_numbers(
        requested_issue="31",
        state_ref=None,
        tracked=(32, 31),
    ) == (31,)
    assert candidate_capability_issue_numbers(
        requested_issue=None,
        state_ref="foundry-opt/state/issue-32",
        tracked=(31, 32),
    ) == (32,)
    with pytest.raises(ValueError, match="not tracked"):
        candidate_capability_issue_numbers(
            requested_issue="33",
            state_ref=None,
            tracked=(31, 32),
        )

    class Commands:
        def run(self, arguments, **kwargs):
            return CommandResult(
                0,
                json.dumps(
                    {
                        "subscription": "subscription",
                        "tenant": "tenant",
                        "userName": "optimizer-client",
                        "userType": "servicePrincipal",
                    }
                ),
                "",
            )

    environment = {
        "AZURE_CLIENT_ID": "optimizer-client",
        "AZURE_SUBSCRIPTION_ID": "subscription",
        "AZURE_TENANT_ID": "tenant",
    }
    verify_active_optimizer_identity(Commands(), tmp_path, environment)
    with pytest.raises(ValueError, match="scope"):
        verify_active_optimizer_identity(
            Commands(),
            tmp_path,
            {
                **environment,
                "AZURE_DEPLOYMENT_CLIENT_ID": "optimizer-client",
            },
        )


def test_capability_plan_rejects_unknown_fields_before_execution(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    planned = snapshot.outbox[0]
    unknown = OutboxRecord(
        planned.record_id,
        planned.kind,
        planned.generation,
        planned.sequence,
        {**dict(planned.payload), "status": "unexpected"},
    )
    ledger = Ledger(
        StateRefSnapshot(
            snapshot.revision,
            snapshot.state,
            snapshot.inbox,
            (unknown,),
            snapshot.objects,
        )
    )
    effect_id = planned.record_id
    result_object = StateObject(
        f"objects/capabilities/{effect_id}-result.json",
        b'{"assets":[],"kind":"candidate_assets_registration_result"}\n',
    )
    executor = Executor(
        CandidateCapabilityExecution(
            record_kind="candidate_assets_registration_succeeded",
            payload={
                "base_commit": BASE_COMMIT,
                "capability_path": result_object.path,
                "capability_sha256": result_object.sha256,
                "effect_id": effect_id,
                "effect_kind": "foundry_assets",
                "issue_number": 31,
                "result_id": f"{effect_id}-result",
                "spec_sha256": SPEC_SHA256,
            },
            objects=(result_object,),
        )
    )

    result = CandidateCapabilityBridge(
        ledger=ledger,
        executor=executor,
        assignments=Assignments(),
    ).advance(tmp_path, 31)

    assert result.status is CandidateCapabilityStatus.TERMINAL
    assert result.code == "candidate_capability_intent_invalid"
    assert executor.reconciles == []
    assert executor.executes == []


def test_capability_result_rejects_unknown_fields(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    effect_id = snapshot.outbox[0].record_id
    result_object = StateObject(
        f"objects/capabilities/{effect_id}-result.json",
        b'{"assets":[],"kind":"candidate_assets_registration_result"}\n',
    )
    execution = CandidateCapabilityExecution(
        record_kind="candidate_assets_registration_succeeded",
        payload={
            "base_commit": BASE_COMMIT,
            "capability_path": result_object.path,
            "capability_sha256": result_object.sha256,
            "effect_id": effect_id,
            "effect_kind": "foundry_assets",
            "issue_number": 31,
            "result_id": f"{effect_id}-result",
            "spec_sha256": SPEC_SHA256,
            "status": "unexpected",
        },
        objects=(result_object,),
    )

    result = CandidateCapabilityBridge(
        ledger=Ledger(snapshot),
        executor=Executor(execution),
        assignments=Assignments(),
    ).advance(tmp_path, 31)

    assert result.status is CandidateCapabilityStatus.TERMINAL
    assert result.code == "candidate_capability_result_invalid"


def test_legacy_evaluation_plan_is_an_explicit_closed_schema() -> None:
    snapshot = _snapshot()
    effect_id = "evaluation-31-1-baseline"
    legacy = OutboxRecord(
        effect_id,
        "candidate_effect_planned",
        1,
        snapshot.state.sequence,
        {
            "base_commit": BASE_COMMIT,
            "candidate_id": "baseline",
            "effect_id": effect_id,
            "effect_kind": "foundry_evaluation",
            "issue_number": 31,
            "max_attempts": 2,
            "slot": 0,
            "spec_sha256": SPEC_SHA256,
        },
    )

    _validate_planned_capability(
        StateRefSnapshot(
            snapshot.revision,
            snapshot.state,
            snapshot.inbox,
            (legacy,),
            snapshot.objects,
        ),
        legacy,
        31,
    )


def test_evaluation_result_key_must_match_current_plan() -> None:
    effect_id = "evaluation-31-1-baseline"
    planned = OutboxRecord(
        effect_id,
        "candidate_effect_planned",
        1,
        2,
        {
            "base_commit": BASE_COMMIT,
            "candidate_id": "baseline",
            "effect_id": effect_id,
            "effect_kind": "foundry_evaluation",
            "idempotency_key": "a" * 64,
            "issue_number": 31,
            "max_attempts": 2,
            "slot": 0,
            "spec_sha256": SPEC_SHA256,
        },
    )
    result_object = StateObject(
        f"objects/capabilities/{effect_id}-result.json",
        b'{"kind":"candidate_evaluation_result"}\n',
    )
    execution = CandidateCapabilityExecution(
        "candidate_effect_succeeded",
        {
            "base_commit": BASE_COMMIT,
            "candidate_id": "baseline",
            "capability_path": result_object.path,
            "capability_sha256": result_object.sha256,
            "effect_id": effect_id,
            "effect_kind": "foundry_evaluation",
            "evaluation_id": "evaluation-1",
            "idempotency_key": "b" * 64,
            "issue_number": 31,
            "metrics": {"quality": 0.9},
            "run_id": "run-1",
            "spec_sha256": SPEC_SHA256,
        },
        (result_object,),
    )

    with pytest.raises(ValueError, match="idempotency binding"):
        _validate_execution(planned, execution)
