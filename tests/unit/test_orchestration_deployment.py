from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from foundry_opt.deployment import DEPLOYMENT_OIDC_CLIENT_ID, DeploymentTrigger
from foundry_opt.orchestration import (
    AdvanceRequest,
    CampaignPhase,
    CampaignState,
    CandidateBinding,
    CandidatePullRequestSnapshot,
    CandidatePullRequestState,
    CandidateRecord,
    OutboxRecord,
    OptimizationCampaign,
    StateRefSnapshot,
    StewardAdvanceRequest,
    StewardAdvanceService,
    StewardAdvanceStatus,
)
from foundry_opt.orchestration.deployment import (
    DeploymentBridgeStatus,
    DeploymentDispatchClaimRecorder,
    DeploymentDispatchClaimStatus,
    DeploymentCleanupBridge,
    DeploymentCleanupBridgeStatus,
    CandidateDeploymentSelectionReader,
    ExistingDeploymentPublicationVerifier,
    ExistingDeploymentWorkflowGateway,
    ExistingPostDeploymentEvaluationEffects,
    LedgerDeploymentPublicationVerifier,
    DeploymentOrchestrationRequest,
    DeploymentOrchestrationService,
    DeploymentOrchestrationStatus,
    DeploymentPlan,
    PostDeploymentEvaluationResult,
    PostDeploymentEvaluationStatus,
    DeploymentPublicationStatus,
    DeploymentPublicationResultRecorder,
    DeploymentPublishedVerification,
    DeploymentSelectionSnapshot,
    DeploymentWorkflowEvent,
    DeploymentWorkflowEventIntake,
    DeploymentWorkflowIntakeStatus,
    DeploymentWorkflowBridge,
    DeploymentWorkflowIdentity,
    DeploymentWorkflowResult,
    DeploymentWorkflowRunState,
    TrustedDeploymentWorkflowContext,
    deployment_workflow_event_from_payload,
)


NOW = datetime(2026, 7, 31, 20, 0, tzinfo=UTC)
ISSUE = 31
GENERATION = 2
SPEC = "a" * 64
BASE = "b" * 40
HEAD = "c" * 40
MERGE = "d" * 40
TREE = "e" * 40
PATCH = "1" * 64
BUNDLE = "2" * 64
EVIDENCE = "3" * 64


def _binding(candidate_id: str = "candidate-1") -> CandidateBinding:
    return CandidateBinding(
        issue_number=ISSUE,
        generation=GENERATION,
        spec_sha256=SPEC,
        base_commit=BASE,
        candidate_id=candidate_id,
        draft_id=f"draft-{candidate_id}",
        evidence_sha256=EVIDENCE,
        patch_sha256=PATCH,
        bundle_sha256=BUNDLE,
        tree_sha=TREE,
        allowed_paths=(Path("agent"),),
        changed_paths=(Path("agent/instructions.md"),),
    )


def _snapshot() -> StateRefSnapshot:
    binding = _binding()
    state = CampaignState(
        issue_number=ISSUE,
        generation=GENERATION,
        sequence=12,
        phase=CampaignPhase.DEPLOYMENT,
        spec_sha256=SPEC,
        baseline_evaluation_id="eval-baseline",
        candidates=(CandidateRecord("candidate-1", True, EVIDENCE),),
        selected_candidate_id="candidate-1",
        merge_commit=MERGE,
    )
    planned = OutboxRecord(
        record_id="applier-2-candidate-1-binding",
        kind="applier_worker_issue_planned",
        generation=GENERATION,
        sequence=10,
        payload={
            "allowed_paths": ["agent"],
            "attestation_path": "objects/candidates/g2-candidate-1.json",
            "base_commit": BASE,
            "binding_sha256": binding.binding_sha256,
            "bundle_sha256": BUNDLE,
            "candidate_id": "candidate-1",
            "changed_paths": ["agent/instructions.md"],
            "draft_id": "draft-candidate-1",
            "effect_id": "applier-2-candidate-1-binding",
            "effect_kind": "applier_worker_issue",
            "evidence_path": f"objects/evidence/{EVIDENCE}.json",
            "evidence_sha256": EVIDENCE,
            "issue_number": ISSUE,
            "marker": (
                "<!-- foundry-opt:candidate-pr:issue-31:g2:candidate-1:"
                f"{binding.binding_sha256[:20]} -->"
            ),
            "patch_path": f"objects/patches/{PATCH}.patch",
            "patch_sha256": PATCH,
            "required_checks": ["exact-candidate", "tests"],
            "spec_sha256": SPEC,
            "specialist": "foundry-candidate-applier",
            "tree_sha": TREE,
            "work_kind": "apply_exact_candidate",
        },
    )
    succeeded = OutboxRecord(
        record_id="applier-2-candidate-1-binding-succeeded",
        kind="applier_worker_issue_succeeded",
        generation=GENERATION,
        sequence=10,
        payload={
            "assigned": True,
            "binding_sha256": binding.binding_sha256,
            "candidate_id": "candidate-1",
            "created": True,
            "effect_id": planned.record_id,
            "issue_number": ISSUE,
            "result_id": "applier-result-candidate-1",
            "worker_issue_number": 84,
        },
    )
    selected = OutboxRecord(
        record_id="selection-2-candidate-1-91",
        kind="candidate_selection_recorded",
        generation=GENERATION,
        sequence=12,
        payload={
            "binding_sha256": binding.binding_sha256,
            "candidate_id": "candidate-1",
            "head_commit": HEAD,
            "issue_number": ISSUE,
            "merge_commit": MERGE,
            "pull_request_number": 91,
            "tree_sha": TREE,
            "worker_issue_number": 84,
        },
    )
    return StateRefSnapshot(
        revision="f" * 40,
        state=state,
        inbox=(),
        outbox=(planned, succeeded, selected),
    )


def _plan() -> DeploymentPlan:
    return DeploymentPlan(
        issue_number=ISSUE,
        generation=GENERATION,
        repository="octo-org/agents",
        repository_id=1234,
        workflow=DeploymentWorkflowIdentity(
            repository="octo-org/agents",
            repository_id=1234,
            path=Path(".github/workflows/deploy.yml"),
            ref="refs/heads/main",
            trigger=DeploymentTrigger.MANUAL,
            workflow_id=77,
            actor="github-actions[bot]",
            deployment_client_id=DEPLOYMENT_OIDC_CLIENT_ID,
        ),
        allowed_merge_actors=("maintainer",),
        required_checks=("exact-candidate", "tests"),
        max_attempts=2,
        timeout_seconds=1800,
        campaign_pull_request_number=100,
        optimization_pull_request_number=101,
    )


class Ledger:
    def __init__(self, snapshot: StateRefSnapshot) -> None:
        self.snapshot = snapshot
        self.commits: list[dict[str, object]] = []

    def load(self, repository_root: Path, issue_number: int):
        return self.snapshot

    def commit(self, repository_root: Path, **kwargs):
        self.commits.append(kwargs)
        self.snapshot = replace(
            self.snapshot,
            revision="9" * 40,
            state=kwargs["state"],
            inbox=(*self.snapshot.inbox, *kwargs.get("inbox", ())),
            outbox=(*self.snapshot.outbox, *kwargs.get("outbox", ())),
        )
        return self.snapshot


class Resolver:
    def resolve(self, request, state):
        return _plan()


class SelectionReader:
    def read(self, request, binding, plan):
        return DeploymentSelectionSnapshot(
            binding=binding,
            candidate_pull_request_number=91,
            candidate_issue_number=84,
            head_commit=HEAD,
            merge_commit=MERGE,
            merge_tree_sha=TREE,
            merge_actor="maintainer",
            checks={
                "exact-candidate": "success",
                "tests": "success",
            },
        )


class ChangedSelectionReader(SelectionReader):
    def __init__(self, change: dict[str, object]) -> None:
        self.change = change

    def read(self, request, binding, plan):
        return replace(
            super().read(request, binding, plan),
            **self.change,
        )


def test_deployment_intent_is_persisted_before_dispatch() -> None:
    ledger = Ledger(_snapshot())
    service = DeploymentOrchestrationService(
        ledger=ledger,
        resolver=Resolver(),
        selection_reader=SelectionReader(),
        clock=lambda: NOW,
    )

    result = service.advance(
        DeploymentOrchestrationRequest(Path.cwd(), ISSUE)
    )

    assert result.status is DeploymentOrchestrationStatus.PLANNED
    assert len(ledger.commits) == 1
    records = ledger.commits[0]["outbox"]
    intent = next(
        record
        for record in records
        if record.kind == "deployment_workflow_planned"
    )
    assert intent.payload == {
        "attempt": 1,
        "binding_sha256": intent.payload["binding_sha256"],
        "bundle_sha256": BUNDLE,
        "candidate_id": "candidate-1",
        "candidate_issue_number": 84,
        "candidate_pull_request_number": 91,
        "deployment_client_id": DEPLOYMENT_OIDC_CLIENT_ID,
        "draft_id": "draft-candidate-1",
        "effect_id": intent.record_id,
        "effect_kind": "deployment_workflow",
        "evidence_sha256": EVIDENCE,
        "issue_number": ISSUE,
        "lineage_sha256": intent.payload["lineage_sha256"],
        "merge_actor": "maintainer",
        "merge_commit": MERGE,
        "patch_sha256": PATCH,
        "repository": "octo-org/agents",
        "repository_id": 1234,
        "required_checks": ["exact-candidate", "tests"],
        "spec_sha256": SPEC,
        "started_at": NOW.isoformat(),
        "timeout_seconds": 1800,
        "tree_sha": TREE,
        "workflow_actor": "github-actions[bot]",
        "workflow_id": 77,
        "workflow_path": ".github/workflows/deploy.yml",
        "workflow_ref": "refs/heads/main",
        "workflow_trigger": "manual",
    }


@pytest.mark.parametrize(
    "change",
    (
        {"merge_actor": "untrusted-actor"},
        {
            "checks": {
                "exact-candidate": "success",
                "tests": "failure",
            }
        },
        {"merge_tree_sha": "9" * 40},
    ),
)
def test_selection_policy_mismatch_fails_closed_before_intent(
    change: dict[str, object],
) -> None:
    ledger = Ledger(_snapshot())
    service = DeploymentOrchestrationService(
        ledger=ledger,
        resolver=Resolver(),
        selection_reader=ChangedSelectionReader(change),
        clock=lambda: NOW,
    )

    result = service.advance(
        DeploymentOrchestrationRequest(Path.cwd(), ISSUE)
    )

    assert result.status is DeploymentOrchestrationStatus.READY_FOR_HUMAN
    assert result.snapshot.state.phase is CampaignPhase.BLOCKED
    assert result.snapshot.state.block_reason == "deployment_selection_invalid"
    assert not any(
        record.kind == "deployment_workflow_planned"
        for record in result.snapshot.outbox
    )


class Claimer:
    def __init__(self) -> None:
        self.claimed: set[str] = set()

    def claim(self, intent):
        if intent.effect_id in self.claimed:
            return DeploymentDispatchClaimStatus.ALREADY_CLAIMED
        self.claimed.add(intent.effect_id)
        return DeploymentDispatchClaimStatus.CLAIMED


class AckLostGateway:
    def __init__(self) -> None:
        self.dispatches = 0
        self.result: DeploymentWorkflowResult | None = None

    def find(self, intent):
        return self.result

    def dispatch(self, intent):
        self.dispatches += 1
        self.result = DeploymentWorkflowResult(
            effect_id=intent.effect_id,
            result_id="deployment-run-991",
            attempt=intent.attempt,
            binding=intent.binding,
            workflow=intent.workflow,
            run_id=991,
            run_url=(
                "https://github.com/octo-org/agents/actions/runs/991"
            ),
            state=DeploymentWorkflowRunState.QUEUED,
            conclusion=None,
        )
        raise RuntimeError("dispatch acknowledgement lost")


def test_dispatch_ack_loss_reconciles_without_dispatching_twice() -> None:
    ledger = Ledger(_snapshot())
    service = DeploymentOrchestrationService(
        ledger=ledger,
        resolver=Resolver(),
        selection_reader=SelectionReader(),
        clock=lambda: NOW,
    )
    planned = service.advance(
        DeploymentOrchestrationRequest(Path.cwd(), ISSUE)
    )
    intent = next(
        record
        for record in planned.snapshot.outbox
        if record.kind == "deployment_workflow_planned"
    )
    claimer = Claimer()
    gateway = AckLostGateway()
    bridge = DeploymentWorkflowBridge(gateway=gateway, claimer=claimer)

    first = bridge.apply(intent)
    second = bridge.apply(intent)

    assert first.status is DeploymentBridgeStatus.WAITING
    assert first.reason == "deployment_dispatch_ack_unknown"
    assert second.status is DeploymentBridgeStatus.WAITING
    assert second.reason == "deployment_dispatch_ack_pending"
    assert gateway.dispatches == 1


def test_workflow_run_binding_is_stable_across_status_progression() -> None:
    ledger = Ledger(_snapshot())
    planned = DeploymentOrchestrationService(
        ledger=ledger,
        resolver=Resolver(),
        selection_reader=SelectionReader(),
        clock=lambda: NOW,
    ).advance(DeploymentOrchestrationRequest(Path.cwd(), ISSUE))
    record = next(
        item
        for item in planned.snapshot.outbox
        if item.kind == "deployment_workflow_planned"
    )
    from foundry_opt.orchestration.deployment import (
        DeploymentWorkflowResultRecorder,
    )

    recorder = DeploymentWorkflowResultRecorder(ledger)
    queued = _workflow_result(
        record,
        state=DeploymentWorkflowRunState.QUEUED,
        conclusion=None,
    )
    first = recorder.record(Path.cwd(), ISSUE, queued)
    ledger.snapshot = replace(
        first,
        state=replace(first.state, sequence=first.state.sequence + 1),
    )
    succeeded = replace(
        queued,
        result_id="deployment-run-991-success",
        state=DeploymentWorkflowRunState.SUCCESS,
        conclusion="success",
    )

    second = recorder.record(Path.cwd(), ISSUE, succeeded)

    assert (
        sum(
            item.kind == "deployment_workflow_run_bound"
            for item in second.outbox
        )
        == 1
    )
    with pytest.raises(ValueError, match="another run"):
        recorder.record(
            Path.cwd(),
            ISSUE,
            replace(
                succeeded,
                run_id=992,
                run_url=(
                    "https://github.com/octo-org/agents/actions/runs/992"
                ),
            ),
        )


class FailingClaimLedger(Ledger):
    def commit(self, repository_root: Path, **kwargs):
        raise RuntimeError("claim persistence failed")


class CountingGateway:
    def __init__(self) -> None:
        self.dispatches = 0

    def find(self, intent):
        return None

    def dispatch(self, intent):
        self.dispatches += 1


def test_bridge_never_dispatches_when_durable_claim_cannot_persist() -> None:
    ledger = Ledger(_snapshot())
    planned = DeploymentOrchestrationService(
        ledger=ledger,
        resolver=Resolver(),
        selection_reader=SelectionReader(),
        clock=lambda: NOW,
    ).advance(DeploymentOrchestrationRequest(Path.cwd(), ISSUE))
    intent = next(
        record
        for record in planned.snapshot.outbox
        if record.kind == "deployment_workflow_planned"
    )
    failed_ledger = FailingClaimLedger(planned.snapshot)
    gateway = CountingGateway()
    bridge = DeploymentWorkflowBridge(
        gateway=gateway,
        claimer=DeploymentDispatchClaimRecorder(
            failed_ledger,
            Path.cwd(),
            ISSUE,
        ),
    )

    result = bridge.apply(intent)

    assert result.status is DeploymentBridgeStatus.WAITING
    assert result.reason == "deployment_dispatch_claim_unavailable"
    assert gateway.dispatches == 0


class WorkflowInbox:
    def __init__(self) -> None:
        self.events = []

    def append(self, issue_number, event):
        if any(item.event_id == event.event_id for item in self.events):
            return False
        self.events.append(event)
        return True


def _workflow_result(
    intent_record: OutboxRecord,
    *,
    state: DeploymentWorkflowRunState = DeploymentWorkflowRunState.SUCCESS,
    conclusion: str | None = "success",
    run_id: int = 991,
) -> DeploymentWorkflowResult:
    from foundry_opt.orchestration.deployment import (
        deployment_workflow_intent,
    )

    intent = deployment_workflow_intent(intent_record)
    return DeploymentWorkflowResult(
        effect_id=intent.effect_id,
        result_id="deployment-run-991",
        attempt=intent.attempt,
        binding=intent.binding,
        workflow=intent.workflow,
        run_id=run_id,
        run_url=(
            "https://github.com/octo-org/agents/actions/runs/"
            f"{run_id}"
        ),
        state=state,
        conclusion=conclusion,
    )


def test_trusted_workflow_result_is_a_duplicate_safe_inbox_event() -> None:
    ledger = Ledger(_snapshot())
    planned = DeploymentOrchestrationService(
        ledger=ledger,
        resolver=Resolver(),
        selection_reader=SelectionReader(),
        clock=lambda: NOW,
    ).advance(DeploymentOrchestrationRequest(Path.cwd(), ISSUE))
    intent = next(
        record
        for record in planned.snapshot.outbox
        if record.kind == "deployment_workflow_planned"
    )
    result = _workflow_result(intent)
    context = TrustedDeploymentWorkflowContext(
        event_name="workflow_run",
        action="completed",
        delivery_id="delivery-991",
        repository="octo-org/agents",
        repository_id=1234,
        workflow_id=77,
        workflow_path=Path(".github/workflows/deploy.yml"),
        actor="github-actions[bot]",
        deployment_client_id=DEPLOYMENT_OIDC_CLIENT_ID,
    )
    inbox = WorkflowInbox()
    intake = DeploymentWorkflowEventIntake(inbox)
    event = DeploymentWorkflowEvent(context, result, NOW)

    first = intake.ingest(event)
    second = intake.ingest(event)

    assert first.status is DeploymentWorkflowIntakeStatus.RECORDED
    assert second.status is DeploymentWorkflowIntakeStatus.DUPLICATE
    assert first.event.payload["run_id"] == 991
    assert first.event.payload["run_status"] == "success"
    assert first.event.payload["run_conclusion"] == "success"
    assert first.event.payload["binding_sha256"] == (
        result.binding.binding_sha256
    )
    with pytest.raises(ValueError, match="trusted workflow"):
        DeploymentWorkflowEvent(
            replace(context, actor="untrusted-actor"),
            result,
            NOW,
        )


class PublicationVerifier:
    def __init__(self) -> None:
        self.calls = 0

    def verify(self, request):
        self.calls += 1
        return DeploymentPublishedVerification(
            status=DeploymentPublicationStatus.VERIFIED,
            intent=request.intent,
            workflow_result=request.workflow_result,
            deployment_version=13,
            source_sha256=BUNDLE,
            tree_sha=TREE,
            bundle_sha256=BUNDLE,
            merge_commit=MERGE,
            lineage_sha256=request.intent.lineage_sha256,
            metadata_sha256="4" * 64,
            portal_url=(
                "https://ai.azure.com/projects/demo/agents/"
                "support/versions/13"
            ),
        )


class ChangedPublicationVerifier(PublicationVerifier):
    def __init__(self, change: dict[str, object]) -> None:
        super().__init__()
        self.change = change

    def verify(self, request):
        return replace(super().verify(request), **self.change)


def _with_workflow_result(
    ledger: Ledger,
    snapshot: StateRefSnapshot,
    intent: OutboxRecord,
    *,
    delivery_id: str,
    result: DeploymentWorkflowResult | None = None,
) -> None:
    event = DeploymentWorkflowEvent(
        TrustedDeploymentWorkflowContext(
            "workflow_run",
            "completed",
            delivery_id,
            "octo-org/agents",
            1234,
            77,
            Path(".github/workflows/deploy.yml"),
            "github-actions[bot]",
            DEPLOYMENT_OIDC_CLIENT_ID,
        ),
        result or _workflow_result(intent),
        NOW,
    ).to_campaign_event()
    ledger.snapshot = replace(
        snapshot,
        state=OptimizationCampaign().advance(
            AdvanceRequest(ISSUE, snapshot.state, (event,))
        ).state,
        inbox=(*snapshot.inbox, event),
    )


def test_successful_workflow_persists_version_before_evaluation() -> None:
    ledger = Ledger(_snapshot())
    verifier = PublicationVerifier()
    service = DeploymentOrchestrationService(
        ledger=ledger,
        resolver=Resolver(),
        selection_reader=SelectionReader(),
        publication_verifier=verifier,
        clock=lambda: NOW,
    )
    planned = service.advance(
        DeploymentOrchestrationRequest(Path.cwd(), ISSUE)
    )
    intent = next(
        record
        for record in planned.snapshot.outbox
        if record.kind == "deployment_workflow_planned"
    )
    workflow_event = DeploymentWorkflowEvent(
        TrustedDeploymentWorkflowContext(
            event_name="workflow_run",
            action="completed",
            delivery_id="delivery-success-991",
            repository="octo-org/agents",
            repository_id=1234,
            workflow_id=77,
            workflow_path=Path(".github/workflows/deploy.yml"),
            actor="github-actions[bot]",
            deployment_client_id=DEPLOYMENT_OIDC_CLIENT_ID,
        ),
        _workflow_result(intent),
        NOW,
    ).to_campaign_event()
    state = OptimizationCampaign().advance(
        AdvanceRequest(
            ISSUE,
            planned.snapshot.state,
            (workflow_event,),
        )
    ).state
    ledger.snapshot = replace(
        planned.snapshot,
        state=state,
        inbox=(*planned.snapshot.inbox, workflow_event),
    )

    result = service.advance(
        DeploymentOrchestrationRequest(Path.cwd(), ISSUE)
    )

    assert result.status is DeploymentOrchestrationStatus.PLANNED
    assert result.snapshot.state.phase is CampaignPhase.RETENTION
    assert result.snapshot.state.deployment_version == 13
    assert verifier.calls == 1
    evaluation = next(
        record
        for record in result.snapshot.outbox
        if record.kind == "post_deployment_evaluation_planned"
    )
    assert evaluation.payload["deployment_version"] == 13
    assert evaluation.payload["binding_sha256"] == (
        _workflow_result(intent).binding.binding_sha256
    )


def test_bridge_publication_result_is_durable_and_duplicate_safe() -> None:
    from foundry_opt.orchestration.deployment import (
        DeploymentPublishedVerificationRequest,
        deployment_workflow_intent,
    )

    ledger = Ledger(_snapshot())
    planned = DeploymentOrchestrationService(
        ledger=ledger,
        resolver=Resolver(),
        selection_reader=SelectionReader(),
        clock=lambda: NOW,
    ).advance(DeploymentOrchestrationRequest(Path.cwd(), ISSUE))
    record = next(
        item
        for item in planned.snapshot.outbox
        if item.kind == "deployment_workflow_planned"
    )
    intent = deployment_workflow_intent(record)
    workflow_result = _workflow_result(record)
    publication = DeploymentPublishedVerification(
        DeploymentPublicationStatus.VERIFIED,
        intent,
        workflow_result,
        deployment_version=13,
        source_sha256=BUNDLE,
        tree_sha=TREE,
        bundle_sha256=BUNDLE,
        merge_commit=MERGE,
        lineage_sha256=intent.lineage_sha256,
        metadata_sha256="4" * 64,
        portal_url=(
            "https://ai.azure.com/projects/demo/agents/"
            "support/versions/13"
        ),
    )
    recorder = DeploymentPublicationResultRecorder(ledger)
    from foundry_opt.orchestration.deployment import (
        DeploymentWorkflowResultRecorder,
    )

    DeploymentWorkflowResultRecorder(ledger).record(
        Path.cwd(),
        ISSUE,
        workflow_result,
    )
    first = recorder.record(Path.cwd(), ISSUE, publication)
    second = recorder.record(Path.cwd(), ISSUE, publication)
    observed = LedgerDeploymentPublicationVerifier(ledger).verify(
        DeploymentPublishedVerificationRequest(
            Path.cwd(),
            _plan(),
            intent,
            workflow_result,
        )
    )

    assert first.revision == second.revision
    assert observed == publication
    assert (
        sum(
            item.kind == "deployment_publication_observed"
            for item in second.outbox
        )
        == 1
    )
    advanced = DeploymentOrchestrationService(
        ledger=ledger,
        resolver=Resolver(),
        selection_reader=SelectionReader(),
        publication_verifier=LedgerDeploymentPublicationVerifier(ledger),
        clock=lambda: NOW,
    ).advance(DeploymentOrchestrationRequest(Path.cwd(), ISSUE))
    assert advanced.snapshot.state.phase is CampaignPhase.RETENTION


@pytest.mark.parametrize(
    ("change", "reason"),
    (
        ({"source_sha256": "5" * 64}, "published_source_mismatch"),
        ({"bundle_sha256": "5" * 64}, "published_bundle_mismatch"),
        ({"tree_sha": "5" * 40}, "published_tree_mismatch"),
        (
            {"merge_commit": "5" * 40},
            "published_merge_lineage_mismatch",
        ),
        ({"lineage_sha256": "5" * 64}, "published_lineage_mismatch"),
    ),
)
def test_published_lineage_mismatch_requires_human(
    change: dict[str, object],
    reason: str,
) -> None:
    ledger = Ledger(_snapshot())
    service = DeploymentOrchestrationService(
        ledger=ledger,
        resolver=Resolver(),
        selection_reader=SelectionReader(),
        publication_verifier=ChangedPublicationVerifier(change),
        clock=lambda: NOW,
    )
    planned = service.advance(
        DeploymentOrchestrationRequest(Path.cwd(), ISSUE)
    )
    intent = next(
        record
        for record in planned.snapshot.outbox
        if record.kind == "deployment_workflow_planned"
    )
    _with_workflow_result(
        ledger,
        planned.snapshot,
        intent,
        delivery_id=f"delivery-{reason}",
    )

    result = service.advance(
        DeploymentOrchestrationRequest(Path.cwd(), ISSUE)
    )

    assert result.status is DeploymentOrchestrationStatus.READY_FOR_HUMAN
    assert result.snapshot.state.block_reason == reason


class MissingPublishedVersionVerifier:
    def verify(self, request):
        return DeploymentPublishedVerification(
            status=DeploymentPublicationStatus.MISMATCH,
            intent=request.intent,
            workflow_result=request.workflow_result,
            reason="published_version_mismatch",
        )


def test_published_version_mismatch_requires_human() -> None:
    ledger = Ledger(_snapshot())
    service = DeploymentOrchestrationService(
        ledger=ledger,
        resolver=Resolver(),
        selection_reader=SelectionReader(),
        publication_verifier=MissingPublishedVersionVerifier(),
        clock=lambda: NOW,
    )
    planned = service.advance(
        DeploymentOrchestrationRequest(Path.cwd(), ISSUE)
    )
    intent = next(
        record
        for record in planned.snapshot.outbox
        if record.kind == "deployment_workflow_planned"
    )
    _with_workflow_result(
        ledger,
        planned.snapshot,
        intent,
        delivery_id="delivery-version-mismatch",
    )

    result = service.advance(
        DeploymentOrchestrationRequest(Path.cwd(), ISSUE)
    )

    assert result.snapshot.state.block_reason == "published_version_mismatch"


def test_mismatched_workflow_result_fails_closed_durably() -> None:
    ledger = Ledger(_snapshot())
    service = DeploymentOrchestrationService(
        ledger=ledger,
        resolver=Resolver(),
        selection_reader=SelectionReader(),
        publication_verifier=PublicationVerifier(),
        clock=lambda: NOW,
    )
    planned = service.advance(
        DeploymentOrchestrationRequest(Path.cwd(), ISSUE)
    )
    intent = next(
        record
        for record in planned.snapshot.outbox
        if record.kind == "deployment_workflow_planned"
    )
    trusted = DeploymentWorkflowEvent(
        TrustedDeploymentWorkflowContext(
            event_name="workflow_run",
            action="completed",
            delivery_id="delivery-mismatch-991",
            repository="octo-org/agents",
            repository_id=1234,
            workflow_id=77,
            workflow_path=Path(".github/workflows/deploy.yml"),
            actor="github-actions[bot]",
            deployment_client_id=DEPLOYMENT_OIDC_CLIENT_ID,
        ),
        _workflow_result(intent),
        NOW,
    ).to_campaign_event()
    mismatched = replace(
        trusted,
        payload={**trusted.payload, "workflow_id": 88},
    )
    state = OptimizationCampaign().advance(
        AdvanceRequest(
            ISSUE,
            planned.snapshot.state,
            (mismatched,),
        )
    ).state
    ledger.snapshot = replace(
        planned.snapshot,
        state=state,
        inbox=(*planned.snapshot.inbox, mismatched),
    )

    result = service.advance(
        DeploymentOrchestrationRequest(Path.cwd(), ISSUE)
    )

    assert result.status is DeploymentOrchestrationStatus.READY_FOR_HUMAN
    assert result.snapshot.state.phase is CampaignPhase.BLOCKED
    assert result.snapshot.state.block_reason == "deployment_result_mismatch"
    ready = next(
        record
        for record in result.snapshot.outbox
        if record.kind == "deployment_ready_for_human"
    )
    assert ready.payload["reason"] == "deployment_result_mismatch"
    assert any(
        record.kind == "label_add"
        and record.payload["label"] == "ready-for-human"
        for record in result.snapshot.outbox
    )
    assert not any(
        record.kind == "root_issue_close_planned"
        for record in result.snapshot.outbox
    )


def test_duplicate_and_reordered_workflow_updates_reconcile_once() -> None:
    ledger = Ledger(_snapshot())
    verifier = PublicationVerifier()
    service = DeploymentOrchestrationService(
        ledger=ledger,
        resolver=Resolver(),
        selection_reader=SelectionReader(),
        publication_verifier=verifier,
        clock=lambda: NOW,
    )
    planned = service.advance(
        DeploymentOrchestrationRequest(Path.cwd(), ISSUE)
    )
    record = next(
        item
        for item in planned.snapshot.outbox
        if item.kind == "deployment_workflow_planned"
    )
    result = _workflow_result(record)
    success = DeploymentWorkflowEvent(
        TrustedDeploymentWorkflowContext(
            "workflow_run",
            "completed",
            "delivery-success-first",
            "octo-org/agents",
            1234,
            77,
            Path(".github/workflows/deploy.yml"),
            "github-actions[bot]",
            DEPLOYMENT_OIDC_CLIENT_ID,
        ),
        result,
        NOW,
    ).to_campaign_event()
    queued = DeploymentWorkflowEvent(
        TrustedDeploymentWorkflowContext(
            "workflow_run",
            "requested",
            "delivery-queued-late",
            "octo-org/agents",
            1234,
            77,
            Path(".github/workflows/deploy.yml"),
            "github-actions[bot]",
            DEPLOYMENT_OIDC_CLIENT_ID,
        ),
        replace(
            result,
            result_id="deployment-run-991-queued",
            state=DeploymentWorkflowRunState.QUEUED,
            conclusion=None,
        ),
        NOW,
    ).to_campaign_event()
    duplicate = replace(
        success,
        event_id="deployment-workflow-delivery-success-duplicate",
    )
    state = OptimizationCampaign().advance(
        AdvanceRequest(
            ISSUE,
            planned.snapshot.state,
            (success, queued, duplicate),
        )
    ).state
    ledger.snapshot = replace(
        planned.snapshot,
        state=state,
        inbox=(*planned.snapshot.inbox, success, queued, duplicate),
    )

    reconciled = service.advance(
        DeploymentOrchestrationRequest(Path.cwd(), ISSUE)
    )

    assert reconciled.snapshot.state.phase is CampaignPhase.RETENTION
    assert verifier.calls == 1


@pytest.mark.parametrize(
    ("run_state", "conclusion"),
    (
        (DeploymentWorkflowRunState.FAILURE, "failure"),
        (DeploymentWorkflowRunState.CANCELLED, "cancelled"),
        (DeploymentWorkflowRunState.TIMED_OUT, "timed_out"),
    ),
)
def test_terminal_workflow_retries_once_then_requires_human(
    run_state: DeploymentWorkflowRunState,
    conclusion: str,
) -> None:
    ledger = Ledger(_snapshot())
    service = DeploymentOrchestrationService(
        ledger=ledger,
        resolver=Resolver(),
        selection_reader=SelectionReader(),
        publication_verifier=PublicationVerifier(),
        clock=lambda: NOW,
    )
    planned = service.advance(
        DeploymentOrchestrationRequest(Path.cwd(), ISSUE)
    )
    first_intent = next(
        record
        for record in planned.snapshot.outbox
        if record.kind == "deployment_workflow_planned"
    )
    first_event = DeploymentWorkflowEvent(
        TrustedDeploymentWorkflowContext(
            "workflow_run",
            "completed",
            f"delivery-{conclusion}-991",
            "octo-org/agents",
            1234,
            77,
            Path(".github/workflows/deploy.yml"),
            "github-actions[bot]",
            DEPLOYMENT_OIDC_CLIENT_ID,
        ),
        _workflow_result(
            first_intent,
            state=run_state,
            conclusion=conclusion,
        ),
        NOW,
    ).to_campaign_event()
    ledger.snapshot = replace(
        planned.snapshot,
        state=OptimizationCampaign().advance(
            AdvanceRequest(
                ISSUE,
                planned.snapshot.state,
                (first_event,),
            )
        ).state,
        inbox=(*planned.snapshot.inbox, first_event),
    )

    retry = service.advance(
        DeploymentOrchestrationRequest(Path.cwd(), ISSUE)
    )

    assert retry.status is DeploymentOrchestrationStatus.RETRYING
    intents = tuple(
        record
        for record in retry.snapshot.outbox
        if record.kind == "deployment_workflow_planned"
    )
    assert [record.payload["attempt"] for record in intents] == [1, 2]
    waiting = service.advance(
        DeploymentOrchestrationRequest(Path.cwd(), ISSUE)
    )
    assert waiting.status is DeploymentOrchestrationStatus.WAITING
    assert len(
        [
            record
            for record in waiting.snapshot.outbox
            if record.kind == "deployment_workflow_planned"
        ]
    ) == 2

    second_intent = intents[-1]
    second_event = DeploymentWorkflowEvent(
        TrustedDeploymentWorkflowContext(
            "workflow_run",
            "completed",
            f"delivery-{conclusion}-992",
            "octo-org/agents",
            1234,
            77,
            Path(".github/workflows/deploy.yml"),
            "github-actions[bot]",
            DEPLOYMENT_OIDC_CLIENT_ID,
        ),
        _workflow_result(
            second_intent,
            state=run_state,
            conclusion=conclusion,
            run_id=992,
        ),
        NOW,
    ).to_campaign_event()
    ledger.snapshot = replace(
        waiting.snapshot,
        state=OptimizationCampaign().advance(
            AdvanceRequest(
                ISSUE,
                waiting.snapshot.state,
                (second_event,),
            )
        ).state,
        inbox=(*waiting.snapshot.inbox, second_event),
    )

    blocked = service.advance(
        DeploymentOrchestrationRequest(Path.cwd(), ISSUE)
    )

    assert blocked.status is DeploymentOrchestrationStatus.READY_FOR_HUMAN
    assert blocked.snapshot.state.block_reason == (
        f"deployment_workflow_{run_state.value}"
    )


def test_unobserved_dispatch_timeout_never_risks_a_duplicate() -> None:
    ledger = Ledger(_snapshot())
    DeploymentOrchestrationService(
        ledger=ledger,
        resolver=Resolver(),
        selection_reader=SelectionReader(),
        clock=lambda: NOW,
    ).advance(DeploymentOrchestrationRequest(Path.cwd(), ISSUE))

    result = DeploymentOrchestrationService(
        ledger=ledger,
        resolver=Resolver(),
        selection_reader=SelectionReader(),
        clock=lambda: NOW + timedelta(seconds=1801),
    ).advance(DeploymentOrchestrationRequest(Path.cwd(), ISSUE))

    assert result.status is DeploymentOrchestrationStatus.READY_FOR_HUMAN
    assert result.code == "deployment_workflow_unobserved"
    assert (
        sum(
            record.kind == "deployment_workflow_planned"
            for record in result.snapshot.outbox
        )
        == 1
    )


def test_claimed_but_unobserved_dispatch_fails_closed_as_unknown() -> None:
    ledger = Ledger(_snapshot())
    planned = DeploymentOrchestrationService(
        ledger=ledger,
        resolver=Resolver(),
        selection_reader=SelectionReader(),
        clock=lambda: NOW,
    ).advance(DeploymentOrchestrationRequest(Path.cwd(), ISSUE))
    record = next(
        item
        for item in planned.snapshot.outbox
        if item.kind == "deployment_workflow_planned"
    )
    from foundry_opt.orchestration.deployment import (
        deployment_workflow_intent,
    )

    DeploymentDispatchClaimRecorder(
        ledger,
        Path.cwd(),
        ISSUE,
    ).claim(deployment_workflow_intent(record))

    result = DeploymentOrchestrationService(
        ledger=ledger,
        resolver=Resolver(),
        selection_reader=SelectionReader(),
        clock=lambda: NOW + timedelta(seconds=1801),
    ).advance(DeploymentOrchestrationRequest(Path.cwd(), ISSUE))

    assert result.status is DeploymentOrchestrationStatus.READY_FOR_HUMAN
    assert result.code == "deployment_dispatch_unknown"


def test_deployment_claim_rejects_superseded_generation() -> None:
    ledger = Ledger(_snapshot())
    planned = DeploymentOrchestrationService(
        ledger=ledger,
        resolver=Resolver(),
        selection_reader=SelectionReader(),
        clock=lambda: NOW,
    ).advance(DeploymentOrchestrationRequest(Path.cwd(), ISSUE))
    record = next(
        item
        for item in planned.snapshot.outbox
        if item.kind == "deployment_workflow_planned"
    )
    from foundry_opt.orchestration.deployment import (
        deployment_workflow_intent,
    )

    ledger.snapshot = replace(
        planned.snapshot,
        state=CampaignState(
            issue_number=ISSUE,
            generation=GENERATION + 1,
            sequence=planned.snapshot.state.sequence + 1,
            phase=CampaignPhase.SPECIFICATION,
        ),
    )

    with pytest.raises(RuntimeError, match="unavailable"):
        DeploymentDispatchClaimRecorder(
            ledger,
            Path.cwd(),
            ISSUE,
        ).claim(deployment_workflow_intent(record))


def _advance_to_retention(
    ledger: Ledger,
    *,
    evaluation_effects=None,
) -> DeploymentOrchestrationService:
    service = DeploymentOrchestrationService(
        ledger=ledger,
        resolver=Resolver(),
        selection_reader=SelectionReader(),
        publication_verifier=PublicationVerifier(),
        evaluation_effects=evaluation_effects,
        clock=lambda: NOW,
    )
    planned = service.advance(
        DeploymentOrchestrationRequest(Path.cwd(), ISSUE)
    )
    intent = next(
        record
        for record in planned.snapshot.outbox
        if record.kind == "deployment_workflow_planned"
    )
    _with_workflow_result(
        ledger,
        planned.snapshot,
        intent,
        delivery_id="delivery-retention-success",
    )
    advanced = service.advance(
        DeploymentOrchestrationRequest(Path.cwd(), ISSUE)
    )
    assert advanced.snapshot.state.phase is CampaignPhase.RETENTION
    return service


class EvaluationEffects:
    def __init__(
        self,
        status: PostDeploymentEvaluationStatus,
        reason: str | None,
    ) -> None:
        self.status = status
        self.reason = reason
        self.runs = 0

    def reconcile(self, intent):
        return None

    def run(self, intent):
        self.runs += 1
        deployed = (
            {"quality": 0.89, "safety": 1.0}
            if self.status
            is PostDeploymentEvaluationStatus.RETAINED_IMPROVEMENT
            else {"quality": 0.60, "safety": 0.8}
        )
        return PostDeploymentEvaluationResult(
            result_id=f"{intent.effect_id}-result",
            intent=intent,
            status=self.status,
            reason=self.reason,
            baseline_metrics={"quality": 0.70, "safety": 1.0},
            selected_draft_metrics={"quality": 0.90, "safety": 1.0},
            deployed_metrics=deployed,
        )


@pytest.mark.parametrize(
    "reason",
    (
        "guardrail_regression",
        "baseline_regression",
        "selected_draft_regression",
    ),
)
def test_held_out_regression_leaves_root_open_for_human(
    reason: str,
) -> None:
    ledger = Ledger(_snapshot())
    effects = EvaluationEffects(
        PostDeploymentEvaluationStatus.REGRESSED,
        reason,
    )
    service = _advance_to_retention(
        ledger,
        evaluation_effects=effects,
    )

    result = service.advance(
        DeploymentOrchestrationRequest(Path.cwd(), ISSUE)
    )

    assert effects.runs == 1
    assert result.status is DeploymentOrchestrationStatus.READY_FOR_HUMAN
    assert result.snapshot.state.phase is CampaignPhase.BLOCKED
    assert result.snapshot.state.block_reason == reason
    assert not any(
        record.kind == "root_issue_close_planned"
        for record in result.snapshot.outbox
    )


def _snapshot_with_alternative() -> StateRefSnapshot:
    snapshot = _snapshot()
    binding = _binding("candidate-2")
    planned = OutboxRecord(
        record_id="applier-2-candidate-2-binding",
        kind="applier_worker_issue_planned",
        generation=GENERATION,
        sequence=10,
        payload={
            "allowed_paths": ["agent"],
            "attestation_path": "objects/candidates/g2-candidate-2.json",
            "base_commit": BASE,
            "binding_sha256": binding.binding_sha256,
            "bundle_sha256": BUNDLE,
            "candidate_id": "candidate-2",
            "changed_paths": ["agent/instructions.md"],
            "draft_id": "draft-candidate-2",
            "effect_id": "applier-2-candidate-2-binding",
            "effect_kind": "applier_worker_issue",
            "evidence_path": f"objects/evidence/{EVIDENCE}.json",
            "evidence_sha256": EVIDENCE,
            "issue_number": ISSUE,
            "marker": (
                "<!-- foundry-opt:candidate-pr:issue-31:g2:candidate-2:"
                f"{binding.binding_sha256[:20]} -->"
            ),
            "patch_path": f"objects/patches/{PATCH}.patch",
            "patch_sha256": PATCH,
            "required_checks": ["exact-candidate", "tests"],
            "spec_sha256": SPEC,
            "specialist": "foundry-candidate-applier",
            "tree_sha": TREE,
            "work_kind": "apply_exact_candidate",
        },
    )
    succeeded = OutboxRecord(
        record_id="applier-2-candidate-2-binding-succeeded",
        kind="applier_worker_issue_succeeded",
        generation=GENERATION,
        sequence=10,
        payload={
            "assigned": True,
            "binding_sha256": binding.binding_sha256,
            "candidate_id": "candidate-2",
            "created": True,
            "effect_id": planned.record_id,
            "issue_number": ISSUE,
            "result_id": "applier-result-candidate-2",
            "worker_issue_number": 85,
        },
    )
    observed = OutboxRecord(
        record_id="pr-observation-2-candidate-2",
        kind="candidate_pr_verified",
        generation=GENERATION,
        sequence=11,
        payload={
            "binding_sha256": binding.binding_sha256,
            "candidate_id": "candidate-2",
            "head_commit": HEAD,
            "issue_number": ISSUE,
            "pull_request_number": 92,
            "reason": "exact_candidate_verified",
            "tree_sha": TREE,
            "worker_issue_number": 85,
        },
    )
    return replace(
        snapshot,
        state=replace(
            snapshot.state,
            candidates=(
                *snapshot.state.candidates,
                CandidateRecord("candidate-2", True, EVIDENCE),
            ),
        ),
        outbox=(*snapshot.outbox, planned, succeeded, observed),
    )


def test_retained_improvement_completes_and_plans_all_cleanup() -> None:
    ledger = Ledger(_snapshot_with_alternative())
    effects = EvaluationEffects(
        PostDeploymentEvaluationStatus.RETAINED_IMPROVEMENT,
        None,
    )
    service = _advance_to_retention(
        ledger,
        evaluation_effects=effects,
    )

    result = service.advance(
        DeploymentOrchestrationRequest(Path.cwd(), ISSUE)
    )

    assert result.status is DeploymentOrchestrationStatus.COMPLETE
    assert result.snapshot.state.phase is CampaignPhase.COMPLETED
    kinds = {record.kind for record in result.snapshot.outbox}
    assert {
        "candidate_issue_close_planned",
        "candidate_issue_supersede_planned",
        "candidate_pr_supersede_planned",
        "campaign_pr_close_planned",
        "optimization_pr_close_planned",
        "deployment_final_dashboard",
        "root_comment_final_planned",
        "root_issue_close_planned",
    } <= kinds
    evaluation = next(
        record
        for record in result.snapshot.outbox
        if record.kind == "post_deployment_evaluation_result"
    )
    assert evaluation.payload["baseline_metrics"] == {
        "quality": 0.70,
        "safety": 1.0,
    }
    assert evaluation.payload["draft_metrics"] == {
        "quality": 0.90,
        "safety": 1.0,
    }
    assert evaluation.payload["deployed_metrics"] == {
        "quality": 0.89,
        "safety": 1.0,
    }
    comment = next(
        record
        for record in result.snapshot.outbox
        if record.kind == "root_comment_final_planned"
    )
    assert comment.payload["spec_sha256"] == SPEC
    assert comment.payload["merge_commit"] == MERGE
    assert comment.payload["tree_sha"] == TREE
    assert comment.payload["patch_sha256"] == PATCH
    assert comment.payload["bundle_sha256"] == BUNDLE
    assert comment.payload["evidence_sha256"] == EVIDENCE
    assert comment.payload["run_id"] == 991
    assert comment.payload["deployment_version"] == 13


class AckLostCleanupGateway:
    def __init__(self) -> None:
        self.applied: set[str] = set()
        self.calls = 0

    def effect_applied(self, effect_id):
        return effect_id in self.applied

    def apply(self, effect):
        self.calls += 1
        self.applied.add(effect.effect_id)
        raise RuntimeError("cleanup acknowledgement lost")


def test_cleanup_ack_loss_reconciles_without_duplicate_mutation() -> None:
    ledger = Ledger(_snapshot_with_alternative())
    service = _advance_to_retention(
        ledger,
        evaluation_effects=EvaluationEffects(
            PostDeploymentEvaluationStatus.RETAINED_IMPROVEMENT,
            None,
        ),
    )
    completed = service.advance(
        DeploymentOrchestrationRequest(Path.cwd(), ISSUE)
    )
    record = next(
        item
        for item in completed.snapshot.outbox
        if item.kind == "candidate_issue_close_planned"
    )
    gateway = AckLostCleanupGateway()
    bridge = DeploymentCleanupBridge(gateway)

    first = bridge.apply(record)
    second = bridge.apply(record)

    assert first.status is DeploymentCleanupBridgeStatus.RETRY
    assert second.status is DeploymentCleanupBridgeStatus.ALREADY_APPLIED
    assert gateway.calls == 1


def test_root_closure_waits_for_all_prior_cleanup_effects() -> None:
    ledger = Ledger(_snapshot_with_alternative())
    service = _advance_to_retention(
        ledger,
        evaluation_effects=EvaluationEffects(
            PostDeploymentEvaluationStatus.RETAINED_IMPROVEMENT,
            None,
        ),
    )
    completed = service.advance(
        DeploymentOrchestrationRequest(Path.cwd(), ISSUE)
    )
    root_close = next(
        item
        for item in completed.snapshot.outbox
        if item.kind == "root_issue_close_planned"
    )
    gateway = AckLostCleanupGateway()
    bridge = DeploymentCleanupBridge(gateway)

    waiting = bridge.apply(root_close)
    gateway.applied.update(root_close.payload["depends_on_effect_ids"])
    retried = bridge.apply(root_close)

    assert waiting.status is DeploymentCleanupBridgeStatus.WAITING
    assert retried.status is DeploymentCleanupBridgeStatus.RETRY


def test_root_closure_ignores_cleanup_from_prior_generations() -> None:
    snapshot = _snapshot_with_alternative()
    old = OutboxRecord(
        record_id="old-generation-close",
        kind="candidate_issue_close_planned",
        generation=1,
        sequence=4,
        payload={
            "candidate_id": "candidate-1",
            "effect_id": "old-generation-close",
            "issue_number": ISSUE,
            "marker": (
                "<!-- foundry-opt:candidate-pr:"
                "issue-31:g1:candidate-1:"
                "11111111111111111111 -->"
            ),
            "reason": "selected_candidate_deployed",
            "worker_issue_number": 70,
        },
    )
    ledger = Ledger(replace(snapshot, outbox=(*snapshot.outbox, old)))
    service = _advance_to_retention(
        ledger,
        evaluation_effects=EvaluationEffects(
            PostDeploymentEvaluationStatus.RETAINED_IMPROVEMENT,
            None,
        ),
    )

    completed = service.advance(
        DeploymentOrchestrationRequest(Path.cwd(), ISSUE)
    )
    root_close = next(
        record
        for record in completed.snapshot.outbox
        if record.kind == "root_issue_close_planned"
    )

    assert "old-generation-close" not in (
        root_close.payload["depends_on_effect_ids"]
    )
    assert any(
        record.kind == "candidate_issue_close_planned"
        and record.generation == GENERATION
        and record.payload["candidate_id"] == "candidate-1"
        for record in completed.snapshot.outbox
    )


@pytest.mark.parametrize(
    ("lifecycle_checks", "applied_count"),
    (((True, True), 1), ((True, False), 0)),
)
def test_completed_terminal_edit_preserves_prior_cleanup_generation(
    monkeypatch,
    lifecycle_checks: tuple[bool, ...],
    applied_count: int,
) -> None:
    import foundry_opt.orchestration.deployment_bridge as deployment_bridge
    import foundry_opt.orchestration.issue_intake as issue_intake

    cleanup = OutboxRecord(
        record_id="root-close-1",
        kind="root_issue_close_planned",
        generation=1,
        sequence=9,
        payload={"effect_id": "root-close-1"},
    )
    snapshot = StateRefSnapshot(
        revision="a" * 40,
        state=CampaignState(
            issue_number=ISSUE,
            generation=2,
            sequence=10,
            phase=CampaignPhase.COMPLETED,
            spec_sha256=SPEC,
            baseline_evaluation_id="eval-baseline",
            candidates=(CandidateRecord("candidate-1", True, EVIDENCE),),
            selected_candidate_id="candidate-1",
            merge_commit=MERGE,
            deployment_version=2,
        ),
        inbox=(),
        outbox=(cleanup,),
    )
    applied: list[OutboxRecord] = []

    class Ledger:
        def load(self, repository_root: Path, issue_number: int):
            return snapshot

    class Bridge:
        def __init__(self, gateway) -> None:
            pass

        def apply(self, record: OutboxRecord):
            applied.append(record)
            return record.record_id

    class Recovery:
        def __init__(self, *args) -> None:
            self.checks = iter(lifecycle_checks)

        def can_reconcile_cleanup(self, issue_number: int) -> bool:
            return next(self.checks)

    monkeypatch.setattr(deployment_bridge, "GitStateRef", lambda: Ledger())
    monkeypatch.setattr(
        deployment_bridge,
        "_repository_name",
        lambda commands, repository_root: "octo-org/optimizer",
    )
    monkeypatch.setattr(
        deployment_bridge,
        "GhDeploymentCleanupGateway",
        lambda *args: object(),
    )
    monkeypatch.setattr(
        deployment_bridge,
        "DeploymentCleanupBridge",
        Bridge,
    )
    monkeypatch.setattr(
        issue_intake,
        "GitIssueEventInbox",
        lambda root: object(),
    )
    monkeypatch.setattr(
        issue_intake,
        "GitStateCampaignRecovery",
        Recovery,
    )

    deployment_bridge.reconcile_deployment_cleanup_effects(
        Path("."),
        ISSUE,
        object(),
    )

    assert applied == [cleanup] * applied_count


@pytest.mark.parametrize(("active", "expected"), ((False, []), (True, [1])))
def test_deployment_dispatch_revalidates_trusted_lifecycle(
    monkeypatch,
    active: bool,
    expected: list[int],
) -> None:
    import foundry_opt.orchestration.deployment_bridge as deployment_bridge
    import foundry_opt.orchestration.issue_intake as issue_intake

    planned = OutboxRecord(
        "deployment-1",
        "deployment_workflow_planned",
        1,
        4,
    )
    snapshot = StateRefSnapshot(
        revision="a" * 40,
        state=CampaignState(
            issue_number=ISSUE,
            generation=1,
            sequence=4,
            phase=CampaignPhase.DEPLOYMENT,
            spec_sha256=SPEC,
            baseline_evaluation_id="eval-baseline",
            candidates=(CandidateRecord("candidate-1", True, EVIDENCE),),
            selected_candidate_id="candidate-1",
            merge_commit=MERGE,
        ),
        inbox=(),
        outbox=(planned,),
    )
    applied: list[int] = []

    class Ledger:
        def load(self, repository_root: Path, issue_number: int):
            return snapshot

    class Recovery:
        def __init__(self, *args) -> None:
            pass

        def can_dispatch_deployment(self, issue_number: int) -> bool:
            return active

    class Bridge:
        def __init__(self, **kwargs) -> None:
            pass

        def apply(self, record: OutboxRecord):
            applied.append(1)
            return SimpleNamespace(result=None)

    monkeypatch.setattr(deployment_bridge, "GitStateRef", lambda: Ledger())
    monkeypatch.setattr(
        deployment_bridge,
        "ExistingDeploymentWorkflowGateway",
        lambda *args: object(),
    )
    monkeypatch.setattr(
        deployment_bridge,
        "GhWorkflowRunGateway",
        lambda commands: object(),
    )
    monkeypatch.setattr(
        deployment_bridge,
        "DeploymentDispatchClaimRecorder",
        lambda *args: object(),
    )
    monkeypatch.setattr(deployment_bridge, "DeploymentWorkflowBridge", Bridge)
    monkeypatch.setattr(
        deployment_bridge,
        "deployment_workflow_intent",
        lambda record: SimpleNamespace(attempt=1),
    )
    monkeypatch.setattr(
        issue_intake,
        "GitIssueEventInbox",
        lambda root: object(),
    )
    monkeypatch.setattr(
        issue_intake,
        "GitStateCampaignRecovery",
        Recovery,
    )

    deployment_bridge.reconcile_deployment_workflow_effects(
        Path("."),
        ISSUE,
        object(),
    )

    assert applied == expected


class StewardDeploymentDelegate:
    def __init__(self, snapshot: StateRefSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def advance(self, request):
        self.calls += 1
        from foundry_opt.orchestration.deployment import (
            DeploymentOrchestrationResult,
        )

        return DeploymentOrchestrationResult(
            DeploymentOrchestrationStatus.PLANNED,
            self.snapshot,
            "Deployment intent persisted.",
        )


def test_steward_delegates_canonical_deployment_phase() -> None:
    snapshot = _snapshot()
    ledger = Ledger(snapshot)
    deployment = StewardDeploymentDelegate(snapshot)
    steward = StewardAdvanceService(
        ledger=ledger,
        deployment=deployment,
    )

    result = steward.advance(
        StewardAdvanceRequest(Path.cwd(), ISSUE)
    )

    assert deployment.calls == 1
    assert result.status is StewardAdvanceStatus.ADVANCED
    assert result.phase == CampaignPhase.DEPLOYMENT.value


class FailNextCommitLedger(Ledger):
    def __init__(self, snapshot: StateRefSnapshot) -> None:
        super().__init__(snapshot)
        self.fail_next = False

    def commit(self, repository_root: Path, **kwargs):
        if self.fail_next:
            self.fail_next = False
            from foundry_opt.orchestration import StateRefConflictError

            raise StateRefConflictError("simulated CAS loss")
        return super().commit(repository_root, **kwargs)


class RecoveringEvaluationEffects(EvaluationEffects):
    def __init__(self) -> None:
        super().__init__(
            PostDeploymentEvaluationStatus.RETAINED_IMPROVEMENT,
            None,
        )
        self.saved = None

    def reconcile(self, intent):
        return self.saved

    def run(self, intent):
        self.saved = super().run(intent)
        return self.saved


def test_resume_reconciles_evaluation_without_running_it_twice() -> None:
    ledger = FailNextCommitLedger(_snapshot())
    effects = RecoveringEvaluationEffects()
    service = _advance_to_retention(
        ledger,
        evaluation_effects=effects,
    )
    ledger.fail_next = True

    conflicted = service.advance(
        DeploymentOrchestrationRequest(Path.cwd(), ISSUE)
    )
    resumed = service.advance(
        DeploymentOrchestrationRequest(Path.cwd(), ISSUE)
    )

    assert conflicted.status is DeploymentOrchestrationStatus.CONFLICT
    assert resumed.status is DeploymentOrchestrationStatus.COMPLETE
    assert effects.runs == 1


class CandidateReader:
    def snapshots_for(self, request, bindings):
        binding = bindings[0]
        return (
            CandidatePullRequestSnapshot(
                pull_request_number=91,
                worker_issue_number=84,
                state=CandidatePullRequestState.MERGED,
                author="copilot-swe-agent[bot]",
                draft=False,
                base_ref_name="main",
                current_default_branch="main",
                current_default_commit=MERGE,
                base_commit=BASE,
                head_commit=HEAD,
                head_parent_commit=BASE,
                head_tree_sha=TREE,
                patch_sha256=PATCH,
                changed_paths=(Path("agent/instructions.md"),),
                body=(
                    from_candidate_body(binding)
                ),
                checks={
                    "exact-candidate": "success",
                    "tests": "success",
                },
                binding_sha256=binding.binding_sha256,
                spec_sha256=SPEC,
                bundle_sha256=BUNDLE,
                evidence_sha256=EVIDENCE,
                marker=(
                    "<!-- foundry-opt:candidate-pr:"
                    f"issue-31:g2:candidate-1:"
                    f"{binding.binding_sha256[:20]} -->"
                ),
                merge_commit=MERGE,
                merge_parent_commit=BASE,
                merge_tree_sha=TREE,
                merge_reachable_from_default=True,
                merge_actor="maintainer",
            ),
        )


def from_candidate_body(binding: CandidateBinding) -> str:
    from foundry_opt.orchestration import candidate_pr_body

    return candidate_pr_body(
        binding,
        worker_issue_number=84,
        required_checks=("exact-candidate", "tests"),
    )


def test_candidate_deployment_reader_reverifies_merge_actor_and_checks() -> None:
    binding = _binding()
    selection = CandidateDeploymentSelectionReader(CandidateReader()).read(
        DeploymentOrchestrationRequest(Path.cwd(), ISSUE),
        binding,
        _plan(),
    )

    assert selection.merge_actor == "maintainer"
    assert selection.merge_commit == MERGE
    assert selection.checks["tests"] == "success"


@pytest.mark.parametrize(
    "change",
    (
        {"repository": {"full_name": "evil/repository", "id": 1234}},
        {
            "workflow_run": {
                "id": 991,
                "workflow_id": 88,
                "path": ".github/workflows/deploy.yml@refs/heads/main",
                "head_sha": MERGE,
                "status": "completed",
                "conclusion": "success",
                "html_url": (
                    "https://github.com/octo-org/agents/actions/runs/991"
                ),
                "actor": {"login": "github-actions[bot]"},
            }
        },
        {
            "workflow_run": {
                "id": 991,
                "workflow_id": 77,
                "path": ".github/workflows/deploy.yml@refs/heads/main",
                "head_sha": MERGE,
                "status": "completed",
                "conclusion": "success",
                "html_url": (
                    "https://github.com/octo-org/agents/actions/runs/991"
                ),
                "actor": {"login": "untrusted-actor"},
            }
        },
    ),
)
def test_raw_workflow_payload_fails_closed_on_untrusted_identity(
    change: dict[str, object],
) -> None:
    ledger = Ledger(_snapshot())
    planned = DeploymentOrchestrationService(
        ledger=ledger,
        resolver=Resolver(),
        selection_reader=SelectionReader(),
        clock=lambda: NOW,
    ).advance(DeploymentOrchestrationRequest(Path.cwd(), ISSUE))
    record = next(
        item
        for item in planned.snapshot.outbox
        if item.kind == "deployment_workflow_planned"
    )
    from foundry_opt.orchestration.deployment import (
        deployment_workflow_intent,
    )

    payload = {
        "repository": {"full_name": "octo-org/agents", "id": 1234},
        "workflow_run": {
            "id": 991,
            "display_title": deployment_workflow_intent(
                record
            ).effect_id,
            "workflow_id": 77,
            "path": ".github/workflows/deploy.yml@refs/heads/main",
            "head_sha": MERGE,
            "status": "completed",
            "conclusion": "success",
            "html_url": (
                "https://github.com/octo-org/agents/actions/runs/991"
            ),
            "actor": {"login": "github-actions[bot]"},
        },
    }
    payload.update(change)

    with pytest.raises(ValueError, match="workflow"):
        deployment_workflow_event_from_payload(
            TrustedDeploymentWorkflowContext(
                "workflow_run",
                "completed",
                "delivery-raw-991",
                "octo-org/agents",
                1234,
                77,
                Path(".github/workflows/deploy.yml"),
                "github-actions[bot]",
                DEPLOYMENT_OIDC_CLIENT_ID,
            ),
            payload,
            deployment_workflow_intent(record),
            NOW,
        )


def test_run_bound_manual_payload_accepts_dispatch_actor_and_ref_head() -> None:
    from foundry_opt.orchestration.deployment import (
        deployment_workflow_intent,
    )

    plan = replace(
        _plan(),
        workflow=replace(
            _plan().workflow,
            actor="workflow-dispatch",
        ),
    )

    class ManualResolver:
        def resolve(self, request, state):
            return plan

    ledger = Ledger(_snapshot())
    planned = DeploymentOrchestrationService(
        ledger=ledger,
        resolver=ManualResolver(),
        selection_reader=SelectionReader(),
        clock=lambda: NOW,
    ).advance(DeploymentOrchestrationRequest(Path.cwd(), ISSUE))
    intent = deployment_workflow_intent(
        next(
            record
            for record in planned.snapshot.outbox
            if record.kind == "deployment_workflow_planned"
        )
    )

    event = deployment_workflow_event_from_payload(
        TrustedDeploymentWorkflowContext(
            "workflow_run",
            "completed",
            "delivery-manual-991",
            "octo-org/agents",
            1234,
            77,
            Path(".github/workflows/deploy.yml"),
            "workflow-dispatch",
            DEPLOYMENT_OIDC_CLIENT_ID,
        ),
        {
            "repository": {
                "full_name": "octo-org/agents",
                "id": 1234,
            },
            "workflow_run": {
                "id": 991,
                "display_title": intent.effect_id,
                "workflow_id": 77,
                "path": (
                    ".github/workflows/deploy.yml@refs/heads/main"
                ),
                "head_sha": "9" * 40,
                "status": "completed",
                "conclusion": "success",
                "html_url": (
                    "https://github.com/octo-org/agents/actions/runs/991"
                ),
                "actor": {"login": "deployment-app[bot]"},
            },
        },
        intent,
        NOW,
    )

    assert event.result.binding.merge_commit == MERGE
    assert event.result.run_id == 991


def test_existing_deployment_and_evaluation_interfaces_are_adapted() -> None:
    from foundry_opt.optimization.lifecycle import (
        DeploymentOutcome,
        DeploymentOutcomeStatus,
        PostDeployOutcome,
        PostDeployStatus,
    )
    from foundry_opt.orchestration.deployment import (
        DeploymentPublishedVerificationRequest,
        deployment_workflow_intent,
        post_deployment_evaluation_intent,
    )

    ledger = Ledger(_snapshot())
    planned = DeploymentOrchestrationService(
        ledger=ledger,
        resolver=Resolver(),
        selection_reader=SelectionReader(),
        clock=lambda: NOW,
    ).advance(DeploymentOrchestrationRequest(Path.cwd(), ISSUE))
    record = next(
        item
        for item in planned.snapshot.outbox
        if item.kind == "deployment_workflow_planned"
    )
    intent = deployment_workflow_intent(record)
    workflow_result = _workflow_result(record)

    class Coordinator:
        def deploy(self, request):
            return DeploymentOutcome(
                DeploymentOutcomeStatus.VERIFIED,
                version=13,
                run_url=workflow_result.run_url,
                portal_url=(
                    "https://ai.azure.com/projects/demo/agents/"
                    "support/versions/13"
                ),
                reason_code="verified",
                source_sha256=BUNDLE,
                tree_sha=TREE,
                bundle_sha256=BUNDLE,
                merge_commit=MERGE,
                lineage_sha256=request.intent.lineage_sha256,
                metadata_sha256="4" * 64,
            )

    publication = ExistingDeploymentPublicationVerifier(
        Coordinator(),
        request_factory=lambda request: request,
    ).verify(
        DeploymentPublishedVerificationRequest(
            Path.cwd(),
            _plan(),
            intent,
            workflow_result,
        )
    )
    assert publication.status is DeploymentPublicationStatus.VERIFIED
    assert publication.source_sha256 == BUNDLE

    _with_workflow_result(
        ledger,
        planned.snapshot,
        record,
        delivery_id="delivery-adapter-991",
    )
    retention = DeploymentOrchestrationService(
        ledger=ledger,
        resolver=Resolver(),
        selection_reader=SelectionReader(),
        publication_verifier=PublicationVerifier(),
        clock=lambda: NOW,
    ).advance(DeploymentOrchestrationRequest(Path.cwd(), ISSUE))
    evaluation_record = next(
        item
        for item in retention.snapshot.outbox
        if item.kind == "post_deployment_evaluation_planned"
    )

    class Evaluator:
        def evaluate(self, request):
            return PostDeployOutcome(
                PostDeployStatus.RETAINED_IMPROVEMENT,
                metrics={"quality": 0.9},
                baseline_metrics={"quality": 0.7},
                selected_draft_metrics={"quality": 0.9},
            )

    evaluation = ExistingPostDeploymentEvaluationEffects(
        Evaluator(),
        request_factory=lambda evaluation_intent: evaluation_intent,
    ).run(post_deployment_evaluation_intent(evaluation_record))
    assert (
        evaluation.status
        is PostDeploymentEvaluationStatus.RETAINED_IMPROVEMENT
    )
    assert evaluation.baseline_metrics["quality"] == 0.7
    assert evaluation.selected_draft_metrics["quality"] == 0.9


def test_existing_workflow_gateway_is_a_thin_exact_commit_bridge() -> None:
    from foundry_opt.deployment import (
        DeploymentWorkflowRun,
        WorkflowRunStatus,
    )
    from foundry_opt.orchestration.deployment import (
        deployment_workflow_intent,
    )

    ledger = Ledger(_snapshot())
    planned = DeploymentOrchestrationService(
        ledger=ledger,
        resolver=Resolver(),
        selection_reader=SelectionReader(),
        clock=lambda: NOW,
    ).advance(DeploymentOrchestrationRequest(Path.cwd(), ISSUE))
    record = next(
        item
        for item in planned.snapshot.outbox
        if item.kind == "deployment_workflow_planned"
    )
    intent = deployment_workflow_intent(record)

    class Gateway:
        def __init__(self) -> None:
            self.dispatches = []

        def find_run(self, repository_root, *, query):
            assert query.head_sha == MERGE
            assert query.match_head_sha is False
            return DeploymentWorkflowRun(
                path=Path(".github/workflows/deploy.yml"),
                trigger=DeploymentTrigger.MANUAL,
                status=WorkflowRunStatus.SUCCESS,
                head_commit=MERGE,
                url=(
                    "https://github.com/octo-org/agents/actions/runs/991"
                ),
            )

        def dispatch(self, repository_root, **kwargs):
            self.dispatches.append(kwargs)

    legacy = Gateway()
    gateway = ExistingDeploymentWorkflowGateway(Path.cwd(), legacy)

    observed = gateway.find(intent)
    gateway.dispatch(intent)

    assert observed is not None
    assert observed.run_id == 991
    assert observed.state is DeploymentWorkflowRunState.SUCCESS
    assert legacy.dispatches == [
        {
            "workflow_path": Path(".github/workflows/deploy.yml"),
            "input_name": "selected_commit",
            "commit": MERGE,
            "correlation_input_name": "foundry_opt_effect_id",
            "correlation_id": intent.effect_id,
        }
    ]
