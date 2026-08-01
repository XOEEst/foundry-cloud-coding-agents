from __future__ import annotations

import hashlib
from pathlib import Path

from foundry_opt.optimization import (
    OptimizeCommandRequest,
    OptimizeCommandResult,
    OptimizeCommandStatus,
    OptimizePhase,
)
from foundry_opt.optimization.compatibility import (
    CompatibilityOptimizationCommandService,
    LegacyGenerationFence,
    LegacyCampaignEventProjector,
    LegacyRuntimeNamespace,
    VerifiedSpecApproval,
)
from foundry_opt.orchestration.steward import (
    StewardAdvanceResult,
    StewardAdvanceStatus,
)
from foundry_opt.orchestration import (
    AdvanceRequest,
    CampaignEvent,
    CampaignPhase,
    CampaignState,
    EventKind,
    OptimizationCampaign,
)
from datetime import UTC, datetime


class Legacy:
    def __init__(self, result: OptimizeCommandResult) -> None:
        self.result = result
        self.requests: list[OptimizeCommandRequest] = []

    def execute(self, request: OptimizeCommandRequest) -> OptimizeCommandResult:
        self.requests.append(request)
        return self.result


class Steward:
    def __init__(self, *results: StewardAdvanceResult) -> None:
        self.results = list(results)
        self.calls = []

    def advance(self, request, *, events=()):
        self.calls.append((request, events))
        return self.results.pop(0)


class AdvancingSteward:
    def __init__(self, state: CampaignState) -> None:
        self.state = state
        self.calls = []
        self.campaign = OptimizationCampaign()

    def advance(self, request, *, events=()):
        self.calls.append((request, events))
        if events:
            advanced = self.campaign.advance(
                AdvanceRequest(request.issue_number, self.state, events)
            )
            self.state = advanced.state
            status = (
                StewardAdvanceStatus.WAITING
                if advanced.disposition.value == "wait"
                else StewardAdvanceStatus.ADVANCED
            )
        else:
            status = StewardAdvanceStatus.WAITING
        return StewardAdvanceResult(
            status,
            request.issue_number,
            "state",
            phase=self.state.phase.value,
            state=self.state,
        )


def _steward_result(
    status: StewardAdvanceStatus,
    *,
    code: str | None = None,
) -> StewardAdvanceResult:
    return StewardAdvanceResult(
        status=status,
        issue_number=7,
        summary="steward result",
        code=code,
    )


def _request(tmp_path: Path) -> OptimizeCommandRequest:
    return OptimizeCommandRequest(
        repository_root=tmp_path,
        issue_number=7,
        phase=OptimizePhase.SPEC,
    )


def test_compatibility_adapter_does_not_bootstrap_without_trusted_intake(
    tmp_path: Path,
) -> None:
    expected = OptimizeCommandResult(
        OptimizeCommandStatus.COMPLETE,
        OptimizePhase.SPEC,
        "A draft specification is ready.",
        7,
        {"pull_request": 12},
    )
    legacy = Legacy(expected)
    steward = Steward(
        _steward_result(
            StewardAdvanceStatus.BLOCKED,
            code="campaign_not_initialized",
        ),
    )

    result = CompatibilityOptimizationCommandService(
        legacy=legacy,
        steward=steward,
    ).execute(_request(tmp_path))

    assert result.status is OptimizeCommandStatus.BLOCKED
    assert result.details["code"] == "campaign_not_initialized"
    assert legacy.requests == []
    assert len(steward.calls) == 1
    assert steward.calls[0][1] == ()


def test_compatibility_adapter_fails_closed_on_ledger_conflict(
    tmp_path: Path,
) -> None:
    legacy = Legacy(
        OptimizeCommandResult(
            OptimizeCommandStatus.COMPLETE,
            OptimizePhase.SPEC,
            "legacy success",
            7,
        )
    )
    steward = Steward(
        _steward_result(
            StewardAdvanceStatus.CONFLICT,
            code="state_ref_conflict",
        )
    )

    result = CompatibilityOptimizationCommandService(
        legacy=legacy,
        steward=steward,
    ).execute(_request(tmp_path))

    assert result.status is OptimizeCommandStatus.FAILED
    assert result.details["code"] == "state_ref_conflict"
    assert legacy.requests == []


def test_compatibility_adapter_does_not_mask_legacy_failure(
    tmp_path: Path,
) -> None:
    expected = OptimizeCommandResult(
        OptimizeCommandStatus.BLOCKED,
        OptimizePhase.SPEC,
        "configuration unavailable",
        7,
        {"code": "configuration_unavailable"},
    )
    legacy = Legacy(expected)
    steward = Steward(
        StewardAdvanceResult(
            StewardAdvanceStatus.ADVANCED,
            7,
            "ready",
            state=CampaignState(7, 1, 1, CampaignPhase.SPECIFICATION),
        )
    )

    result = CompatibilityOptimizationCommandService(
        legacy=legacy,
        steward=steward,
    ).execute(_request(tmp_path))

    assert result is expected
    assert len(steward.calls) == 1


def test_compatibility_adapter_gates_cancelled_campaign_before_legacy(
    tmp_path: Path,
) -> None:
    legacy = Legacy(
        OptimizeCommandResult(
            OptimizeCommandStatus.COMPLETE,
            OptimizePhase.RUN,
            "must not run",
            7,
        )
    )
    steward = Steward(
        StewardAdvanceResult(
            StewardAdvanceStatus.WAITING,
            7,
            "cancelled",
            phase="cancelled",
            state=CampaignState(
                7,
                1,
                2,
                CampaignPhase.CANCELLED,
                processed_event_ids=("created", "closed"),
            ),
        )
    )

    result = CompatibilityOptimizationCommandService(
        legacy=legacy,
        steward=steward,
    ).execute(
        OptimizeCommandRequest(
            tmp_path,
            7,
            OptimizePhase.RUN,
        )
    )

    assert result.status is OptimizeCommandStatus.BLOCKED
    assert result.details["code"] == "campaign_phase_waiting"
    assert legacy.requests == []


def test_compatibility_adapter_gates_wrong_phase_before_legacy(
    tmp_path: Path,
) -> None:
    legacy = Legacy(
        OptimizeCommandResult(
            OptimizeCommandStatus.COMPLETE,
            OptimizePhase.RUN,
            "must not run",
            7,
        )
    )
    steward = Steward(
        StewardAdvanceResult(
            StewardAdvanceStatus.ADVANCED,
            7,
            "specification",
            phase="specification",
            state=CampaignState(
                7,
                1,
                1,
                CampaignPhase.SPECIFICATION,
            ),
        )
    )

    result = CompatibilityOptimizationCommandService(
        legacy=legacy,
        steward=steward,
    ).execute(
        OptimizeCommandRequest(
            tmp_path,
            7,
            OptimizePhase.RUN,
        )
    )

    assert result.status is OptimizeCommandStatus.BLOCKED
    assert result.details["code"] == "campaign_phase_incompatible"
    assert legacy.requests == []


def test_compatibility_adapter_reports_canonical_block_before_legacy(
    tmp_path: Path,
) -> None:
    legacy = Legacy(
        OptimizeCommandResult(
            OptimizeCommandStatus.COMPLETE,
            OptimizePhase.RUN,
            "must not run",
            7,
        )
    )
    steward = Steward(
        StewardAdvanceResult(
            StewardAdvanceStatus.BLOCKED,
            7,
            "blocked",
            state=CampaignState(
                7,
                1,
                5,
                CampaignPhase.BLOCKED,
                block_reason="no_eligible_candidates",
            ),
        )
    )

    result = CompatibilityOptimizationCommandService(
        legacy=legacy,
        steward=steward,
    ).execute(
        OptimizeCommandRequest(tmp_path, 7, OptimizePhase.RUN)
    )

    assert result.status is OptimizeCommandStatus.BLOCKED
    assert result.details["code"] == "campaign_blocked"
    assert legacy.requests == []


def test_verified_spec_approval_is_persisted_then_reloaded_before_run(
    tmp_path: Path,
) -> None:
    state = CampaignState(
        7,
        1,
        2,
        CampaignPhase.AWAITING_SPEC_APPROVAL,
        spec_sha256="a" * 64,
    )
    steward = AdvancingSteward(state)
    legacy = Legacy(
        OptimizeCommandResult(
            OptimizeCommandStatus.COMPLETE,
            OptimizePhase.RUN,
            "baseline started",
            7,
        )
    )
    projector = LegacyCampaignEventProjector(
        campaign_state=lambda root, campaign_id: None,
        lifecycle_state=lambda root, campaign_id: None,
        verified_spec_approval=lambda root, issue, digest: (
            VerifiedSpecApproval(digest, "b" * 40)
        ),
    )

    result = CompatibilityOptimizationCommandService(
        legacy=legacy,
        steward=steward,
        projector=projector,
    ).execute(
        OptimizeCommandRequest(tmp_path, 7, OptimizePhase.RUN)
    )

    assert result.status is OptimizeCommandStatus.COMPLETE
    assert legacy.requests
    assert len(steward.calls) == 3
    assert steward.calls[1][1][0].kind is EventKind.SPEC_HUMAN_APPROVED
    assert steward.calls[2][1] == ()
    assert steward.state.phase is CampaignPhase.BASELINE


def test_reconcile_adapts_deployment_ready_canonical_selection(
    tmp_path: Path,
) -> None:
    from foundry_opt.orchestration import CandidateRecord

    state = CampaignState(
        7,
        1,
        6,
        CampaignPhase.DEPLOYMENT,
        spec_sha256="a" * 64,
        baseline_evaluation_id="baseline-1",
        candidates=(CandidateRecord("candidate-1", True, "c" * 64),),
        selected_candidate_id="candidate-1",
        merge_commit="d" * 40,
    )
    steward = AdvancingSteward(state)
    legacy = Legacy(
        OptimizeCommandResult(
            OptimizeCommandStatus.COMPLETE,
            OptimizePhase.RECONCILE,
            "deployment reconciled",
            7,
        )
    )

    result = CompatibilityOptimizationCommandService(
        legacy=legacy,
        steward=steward,
    ).execute(
        OptimizeCommandRequest(tmp_path, 7, OptimizePhase.RECONCILE)
    )

    assert result.status is OptimizeCommandStatus.AWAITING_AGENT
    assert result.details["source"] == "canonical_steward"
    assert result.details["selected_candidate_id"] == "candidate-1"
    assert legacy.requests == []
    assert len(steward.calls) == 1
    assert steward.state.phase is CampaignPhase.DEPLOYMENT


def test_compatibility_adapter_projects_legacy_progress_through_advance(
    tmp_path: Path,
) -> None:
    created = CampaignEvent(
        "compat-issue-created-7",
        EventKind.ISSUE_CREATED,
        1,
        datetime(2026, 7, 31, tzinfo=UTC),
    )
    state = OptimizationCampaign().advance(
        AdvanceRequest(7, None, (created,))
    ).state
    bootstrap = StewardAdvanceResult(
        StewardAdvanceStatus.ADVANCED,
        7,
        "bootstrapped",
        state=state,
    )
    projected = CampaignEvent(
        "compat-spec",
        EventKind.SPEC_REVIEW_REQUIRED,
        1,
        datetime(2026, 7, 31, tzinfo=UTC),
        {"spec_sha256": "a" * 64},
    )
    synchronized = StewardAdvanceResult(
        StewardAdvanceStatus.WAITING,
        7,
        "waiting",
        state=state,
    )
    steward = Steward(bootstrap, synchronized)
    legacy = Legacy(
        OptimizeCommandResult(
            OptimizeCommandStatus.COMPLETE,
            OptimizePhase.SPEC,
            "spec ready",
            7,
        )
    )

    result = CompatibilityOptimizationCommandService(
        legacy=legacy,
        steward=steward,
        projector=lambda request, current: (projected,),
    ).execute(_request(tmp_path))

    assert result.status is OptimizeCommandStatus.COMPLETE
    assert steward.calls[1][1] == (projected,)


def test_compatibility_adapter_fails_when_projected_state_conflicts(
    tmp_path: Path,
) -> None:
    created = CampaignEvent(
        "compat-issue-created-7",
        EventKind.ISSUE_CREATED,
        1,
        datetime(2026, 7, 31, tzinfo=UTC),
    )
    state = OptimizationCampaign().advance(
        AdvanceRequest(7, None, (created,))
    ).state
    steward = Steward(
        StewardAdvanceResult(
            StewardAdvanceStatus.ADVANCED,
            7,
            "bootstrapped",
            state=state,
        ),
        StewardAdvanceResult(
            StewardAdvanceStatus.CONFLICT,
            7,
            "conflict",
            code="state_ref_conflict",
            state=state,
        ),
    )
    legacy = Legacy(
        OptimizeCommandResult(
            OptimizeCommandStatus.COMPLETE,
            OptimizePhase.SPEC,
            "spec ready",
            7,
        )
    )

    result = CompatibilityOptimizationCommandService(
        legacy=legacy,
        steward=steward,
        projector=lambda request, current: (
            CampaignEvent(
                "compat-spec",
                EventKind.SPEC_REVIEW_REQUIRED,
                1,
                datetime(2026, 7, 31, tzinfo=UTC),
                {"spec_sha256": "a" * 64},
            ),
        ),
    ).execute(_request(tmp_path))

    assert result.status is OptimizeCommandStatus.FAILED
    assert result.details["code"] == "state_ref_conflict"


def test_legacy_projector_maps_spec_progress_to_campaign_event(
    tmp_path: Path,
) -> None:
    created = CampaignEvent(
        "event-1",
        EventKind.ISSUE_CREATED,
        1,
        datetime(2026, 7, 31, tzinfo=UTC),
    )
    state = OptimizationCampaign().advance(
        AdvanceRequest(7, None, (created,))
    ).state
    projector = LegacyCampaignEventProjector(
        spec_sha256=lambda root, issue: "a" * 64,
        campaign_state=lambda root, campaign_id: None,
        lifecycle_state=lambda root, campaign_id: None,
        artifact_generation=lambda request, key, digest: 1,
        clock=lambda: datetime(2026, 7, 31, tzinfo=UTC),
    )

    events = projector(_request(tmp_path), state)

    assert len(events) == 1
    assert events[0].kind is EventKind.SPEC_REVIEW_REQUIRED
    assert events[0].payload == {"spec_sha256": "a" * 64}


def test_legacy_projector_maps_completed_runner_and_lifecycle_state(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    evidence = Path("evidence.json")
    (tmp_path / evidence).write_text("redacted", encoding="utf-8")
    old_campaign = SimpleNamespace(
        spec_sha256="a" * 64,
        baseline_development=SimpleNamespace(
            run=SimpleNamespace(evaluation_id="baseline-1")
        ),
        candidates=(
            SimpleNamespace(
                candidate_id="candidate-1",
                artifact=SimpleNamespace(
                    eligible=True,
                    evidence_path=evidence,
                ),
            ),
        ),
        finalized=SimpleNamespace(),
    )
    lifecycle = SimpleNamespace(
        selected_candidate_id="candidate-1",
        merge_commit="b" * 40,
        deployment_version=3,
        post_deploy_retained=True,
    )
    created = CampaignEvent(
        "event-1",
        EventKind.ISSUE_CREATED,
        1,
        datetime(2026, 7, 31, tzinfo=UTC),
    )
    state = OptimizationCampaign().advance(
        AdvanceRequest(7, None, (created,))
    ).state
    projector = LegacyCampaignEventProjector(
        spec_sha256=lambda root, issue: "a" * 64,
        campaign_state=lambda root, campaign_id: old_campaign,
        lifecycle_state=lambda root, campaign_id: lifecycle,
        verified_spec_approval=lambda root, issue, digest: (
            VerifiedSpecApproval(digest, "c" * 40)
        ),
        artifact_generation=lambda request, key, digest: 1,
        clock=lambda: datetime(2026, 7, 31, tzinfo=UTC),
    )

    events = projector(
        OptimizeCommandRequest(
            tmp_path,
            7,
            OptimizePhase.RECONCILE,
        ),
        state,
    )

    assert [event.kind for event in events] == [
        EventKind.SPEC_REVIEW_REQUIRED,
        EventKind.SPEC_HUMAN_APPROVED,
        EventKind.BASELINE_COMPLETED,
        EventKind.CANDIDATE_EVALUATED,
        EventKind.SLATE_PUBLISHED,
    ]


def test_legacy_projector_does_not_infer_human_approval_from_state(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    old_campaign = SimpleNamespace(
        spec_sha256="a" * 64,
        baseline_development=SimpleNamespace(
            run=SimpleNamespace(evaluation_id="baseline-1")
        ),
        candidates=(),
        finalized=None,
    )
    state = CampaignState(
        7,
        1,
        2,
        CampaignPhase.AWAITING_SPEC_APPROVAL,
        spec_sha256="a" * 64,
    )
    projector = LegacyCampaignEventProjector(
        spec_sha256=lambda root, issue: "a" * 64,
        campaign_state=lambda root, campaign_id: old_campaign,
        lifecycle_state=lambda root, campaign_id: None,
        verified_spec_approval=lambda root, issue, digest: None,
        artifact_generation=lambda request, key, digest: 1,
    )

    assert projector(_request(tmp_path), state) == ()


def test_legacy_projector_rejects_artifacts_from_prior_generation(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    old_campaign = SimpleNamespace(
        spec_sha256="a" * 64,
        baseline_development=SimpleNamespace(
            run=SimpleNamespace(evaluation_id="baseline-1")
        ),
        candidates=(),
        finalized=None,
    )
    state = CampaignState(
        7,
        2,
        4,
        CampaignPhase.BASELINE,
        spec_sha256="a" * 64,
    )
    projector = LegacyCampaignEventProjector(
        spec_sha256=lambda root, issue: "a" * 64,
        campaign_state=lambda root, campaign_id: old_campaign,
        lifecycle_state=lambda root, campaign_id: None,
        artifact_generation=lambda request, key, digest: 1,
    )

    assert projector(
        OptimizeCommandRequest(tmp_path, 7, OptimizePhase.RUN),
        state,
    ) == ()


def test_generation_fence_only_tags_artifacts_changed_after_gate(
    tmp_path: Path,
) -> None:
    spec = tmp_path / "spec.yaml"
    spec.write_text("first", encoding="utf-8")
    fence = LegacyGenerationFence(
        artifacts=lambda request: {
            "spec": hashlib.sha256(spec.read_bytes()).hexdigest()
        }
    )
    request = _request(tmp_path)
    before = fence.capture(request)

    fence.record(request, 2, before)
    first_digest = hashlib.sha256(spec.read_bytes()).hexdigest()
    assert fence.generation(request, "spec", first_digest) is None

    spec.write_text("second", encoding="utf-8")
    fence.record(request, 2, before)
    second_digest = hashlib.sha256(spec.read_bytes()).hexdigest()
    assert fence.generation(request, "spec", second_digest) == 2


def test_runtime_namespace_archives_prior_generation_state_before_execution(
    tmp_path: Path,
) -> None:
    campaign = (
        tmp_path
        / ".foundry-optimizer"
        / "campaigns"
        / "issue-7"
    )
    campaign.mkdir(parents=True)
    (campaign / "state.json").write_text(
        '{"status":"finalized"}',
        encoding="utf-8",
    )
    lifecycle = (
        tmp_path
        / ".foundry-optimizer"
        / "lifecycle"
        / "issue-7.json"
    )
    lifecycle.parent.mkdir(parents=True)
    lifecycle.write_text('{"parent_closed":true}', encoding="utf-8")
    namespace = LegacyRuntimeNamespace()
    request = OptimizeCommandRequest(tmp_path, 7, OptimizePhase.RUN)

    namespace.prepare(request, 2)

    assert not campaign.exists()
    assert not lifecycle.exists()
    archive = (
        tmp_path
        / ".foundry-optimizer"
        / "compatibility"
        / "archive"
        / "issue-7"
        / "generation-unknown"
    )
    assert (archive / "campaign" / "state.json").is_file()
    assert (archive / "lifecycle.json").is_file()


def test_compatibility_prepares_generation_namespace_before_legacy(
    tmp_path: Path,
) -> None:
    order: list[str] = []

    class Namespace:
        def prepare(self, request, generation):
            order.append(f"namespace-{generation}")

    class OrderedLegacy(Legacy):
        def execute(self, request):
            order.append("legacy")
            return super().execute(request)

    legacy = OrderedLegacy(
        OptimizeCommandResult(
            OptimizeCommandStatus.COMPLETE,
            OptimizePhase.RUN,
            "run",
            7,
        )
    )
    steward = Steward(
        StewardAdvanceResult(
            StewardAdvanceStatus.ADVANCED,
            7,
            "baseline",
            state=CampaignState(
                7,
                2,
                4,
                CampaignPhase.BASELINE,
                spec_sha256="a" * 64,
            ),
        )
    )

    CompatibilityOptimizationCommandService(
        legacy=legacy,
        steward=steward,
        runtime_namespace=Namespace(),
    ).execute(
        OptimizeCommandRequest(tmp_path, 7, OptimizePhase.RUN)
    )

    assert order == ["namespace-2", "legacy"]
