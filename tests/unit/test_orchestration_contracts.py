from __future__ import annotations

from datetime import UTC, datetime

import pytest

from foundry_opt.orchestration import (
    AdvanceDisposition,
    AdvanceRequest,
    CampaignEvent,
    CampaignPhase,
    CampaignState,
    EventKind,
    InvalidCampaignTransition,
    OptimizationCampaign,
)


NOW = datetime(2026, 7, 31, tzinfo=UTC)


def _event(
    event_id: str,
    kind: EventKind,
    *,
    generation: int = 1,
    **payload: object,
) -> CampaignEvent:
    return CampaignEvent(
        event_id=event_id,
        kind=kind,
        generation=generation,
        occurred_at=NOW,
        payload=payload,
    )


def test_issue_creation_starts_steward_specification() -> None:
    campaign = OptimizationCampaign()

    result = campaign.advance(
        AdvanceRequest(
            issue_number=31,
            state=None,
            events=(_event("event-1", EventKind.ISSUE_CREATED),),
        )
    )

    assert result.state.phase is CampaignPhase.SPECIFICATION
    assert result.state.generation == 1
    assert result.state.sequence == 1
    assert result.disposition is AdvanceDisposition.ADVANCE


def test_terminal_edit_waits_for_explicit_reopen_generation() -> None:
    campaign = OptimizationCampaign()
    state = CampaignState(
        issue_number=31,
        generation=1,
        sequence=2,
        phase=CampaignPhase.CANCELLED,
        processed_event_ids=("event-1", "event-2"),
    )

    result = campaign.advance(
        AdvanceRequest(
            issue_number=31,
            state=state,
            events=(
                _event(
                    "event-3",
                    EventKind.ISSUE_EDITED,
                    generation=2,
                ),
                _event(
                    "event-4",
                    EventKind.ISSUE_CLOSED,
                    generation=2,
                ),
                _event(
                    "event-5",
                    EventKind.ISSUE_REOPENED,
                    generation=3,
                ),
            ),
        )
    )

    assert result.state.generation == 3
    assert result.state.phase is CampaignPhase.SPECIFICATION
    assert result.state.processed_event_ids[-3:] == (
        "event-3",
        "event-4",
        "event-5",
    )


def test_cancelled_workflow_observation_does_not_block_reopen() -> None:
    state = CampaignState(
        issue_number=31,
        generation=1,
        sequence=7,
        phase=CampaignPhase.DEPLOYMENT,
        processed_event_ids=("event-1",),
        spec_sha256="a" * 64,
        baseline_evaluation_id="eval-baseline",
        candidates=(
            {
                "candidate_id": "candidate-1",
                "eligible": True,
                "evidence_sha256": "b" * 64,
            },
        ),
        selected_candidate_id="candidate-1",
        merge_commit="c" * 40,
    )

    result = OptimizationCampaign().advance(
        AdvanceRequest(
            issue_number=31,
            state=state,
            events=(
                _event("event-2", EventKind.ISSUE_CLOSED),
                _event(
                    "event-3",
                    EventKind.DEPLOYMENT_WORKFLOW_OBSERVED,
                ),
                _event(
                    "event-4",
                    EventKind.ISSUE_REOPENED,
                    generation=2,
                ),
            ),
        )
    )

    assert result.state.generation == 2
    assert result.state.phase is CampaignPhase.SPECIFICATION


def test_policy_spec_baseline_candidates_and_slate_progress() -> None:
    campaign = OptimizationCampaign()
    state = campaign.advance(
        AdvanceRequest(
            issue_number=31,
            state=None,
            events=(_event("event-1", EventKind.ISSUE_CREATED),),
        )
    ).state

    result = campaign.advance(
        AdvanceRequest(
            issue_number=31,
            state=state,
            events=(
                _event(
                    "event-2",
                    EventKind.SPEC_POLICY_APPROVED,
                    spec_sha256="a" * 64,
                ),
                _event(
                    "event-3",
                    EventKind.BASELINE_COMPLETED,
                    evaluation_id="eval-baseline",
                ),
                _event(
                    "event-4",
                    EventKind.CANDIDATE_EVALUATED,
                    candidate_id="candidate-1",
                    eligible=True,
                    evidence_sha256="b" * 64,
                ),
                _event(
                    "event-5",
                    EventKind.SLATE_PUBLISHED,
                ),
            ),
        )
    )

    assert result.state.phase is CampaignPhase.AWAITING_SELECTION
    assert result.state.spec_sha256 == "a" * 64
    assert result.state.baseline_evaluation_id == "eval-baseline"
    assert result.state.candidates[0].candidate_id == "candidate-1"
    assert result.state.candidates[0].eligible is True
    assert result.disposition is AdvanceDisposition.WAIT


def test_merge_deployment_and_retention_complete_campaign() -> None:
    state = CampaignState(
        issue_number=31,
        generation=1,
        sequence=4,
        phase=CampaignPhase.AWAITING_SELECTION,
        processed_event_ids=("event-1", "event-2", "event-3", "event-4"),
        spec_sha256="a" * 64,
        baseline_evaluation_id="eval-baseline",
        candidates=(
            {
                "candidate_id": "candidate-1",
                "eligible": True,
                "evidence_sha256": "b" * 64,
            },
        ),
    )
    campaign = OptimizationCampaign()

    result = campaign.advance(
        AdvanceRequest(
            issue_number=31,
            state=state,
            events=(
                _event(
                    "event-5",
                    EventKind.CANDIDATE_MERGED,
                    candidate_id="candidate-1",
                    merge_commit="c" * 40,
                ),
                _event(
                    "event-6",
                    EventKind.DEPLOYMENT_COMPLETED,
                    deployment_version=5,
                ),
                _event(
                    "event-7",
                    EventKind.RETENTION_COMPLETED,
                    retained=True,
                ),
            ),
        )
    )

    assert result.state.phase is CampaignPhase.COMPLETED
    assert result.state.selected_candidate_id == "candidate-1"
    assert result.state.merge_commit == "c" * 40
    assert result.state.deployment_version == 5
    assert result.disposition is AdvanceDisposition.COMPLETE


def test_issue_edit_supersedes_generation_and_old_events() -> None:
    campaign = OptimizationCampaign()
    state = CampaignState(
        issue_number=31,
        generation=1,
        sequence=2,
        phase=CampaignPhase.CANDIDATES,
        processed_event_ids=("event-1", "event-2"),
        spec_sha256="a" * 64,
        baseline_evaluation_id="eval-baseline",
    )

    edited = campaign.advance(
        AdvanceRequest(
            issue_number=31,
            state=state,
            events=(_event("event-3", EventKind.ISSUE_EDITED),),
        )
    ).state
    stale = campaign.advance(
        AdvanceRequest(
            issue_number=31,
            state=edited,
            events=(
                _event(
                    "event-4",
                    EventKind.CANDIDATE_EVALUATED,
                    generation=1,
                    candidate_id="candidate-old",
                    eligible=True,
                    evidence_sha256="b" * 64,
                ),
            ),
        )
    ).state

    assert edited.generation == 2
    assert edited.phase is CampaignPhase.SPECIFICATION
    assert edited.spec_sha256 is None
    assert stale == edited


def test_intake_generation_edit_supersedes_current_generation() -> None:
    campaign = OptimizationCampaign()
    state = CampaignState(
        issue_number=31,
        generation=1,
        sequence=2,
        phase=CampaignPhase.CANDIDATES,
        processed_event_ids=("event-1", "event-2"),
        spec_sha256="a" * 64,
        baseline_evaluation_id="eval-baseline",
    )

    result = campaign.advance(
        AdvanceRequest(
            issue_number=31,
            state=state,
            events=(
                _event(
                    "event-3",
                    EventKind.ISSUE_EDITED,
                    generation=2,
                ),
            ),
        )
    )

    assert result.state.generation == 2
    assert result.state.phase is CampaignPhase.SPECIFICATION


def test_duplicate_event_is_idempotent() -> None:
    campaign = OptimizationCampaign()
    state = campaign.advance(
        AdvanceRequest(
            issue_number=31,
            state=None,
            events=(_event("event-1", EventKind.ISSUE_CREATED),),
        )
    ).state

    result = campaign.advance(
        AdvanceRequest(
            issue_number=31,
            state=state,
            events=(_event("event-1", EventKind.ISSUE_CREATED),),
        )
    )

    assert result.state == state
    assert result.disposition is AdvanceDisposition.WAIT


def test_invalid_selection_and_regression_fail_closed() -> None:
    campaign = OptimizationCampaign()
    state = CampaignState(
        issue_number=31,
        generation=1,
        sequence=1,
        phase=CampaignPhase.AWAITING_SELECTION,
        processed_event_ids=("event-1",),
        spec_sha256="a" * 64,
        baseline_evaluation_id="eval-baseline",
        candidates=(
            {
                "candidate_id": "candidate-1",
                "eligible": False,
                "evidence_sha256": "b" * 64,
            },
            {
                "candidate_id": "candidate-2",
                "eligible": True,
                "evidence_sha256": "d" * 64,
            },
        ),
    )

    invalid_merge = campaign.advance(
        AdvanceRequest(
            issue_number=31,
            state=state,
            events=(
                _event(
                    "event-2",
                    EventKind.CANDIDATE_MERGED,
                    candidate_id="candidate-1",
                    merge_commit="c" * 40,
                ),
            ),
        )
    )

    retained_state = CampaignState(
        issue_number=31,
        generation=1,
        sequence=1,
        phase=CampaignPhase.RETENTION,
        processed_event_ids=("event-1",),
        spec_sha256="a" * 64,
        baseline_evaluation_id="eval-baseline",
        candidates=(
            {
                "candidate_id": "candidate-1",
                "eligible": True,
                "evidence_sha256": "b" * 64,
            },
        ),
        selected_candidate_id="candidate-1",
        merge_commit="c" * 40,
        deployment_version=5,
    )
    result = campaign.advance(
        AdvanceRequest(
            issue_number=31,
            state=retained_state,
            events=(
                _event(
                    "event-2",
                    EventKind.RETENTION_COMPLETED,
                    retained=False,
                ),
            ),
        )
    )

    assert invalid_merge.state.phase is CampaignPhase.BLOCKED
    assert invalid_merge.state.block_reason == "ineligible_candidate_merged"
    assert result.state.phase is CampaignPhase.BLOCKED
    assert result.disposition is AdvanceDisposition.BLOCKED


def test_all_ineligible_slate_blocks_without_waiting_forever() -> None:
    campaign = OptimizationCampaign()
    state = CampaignState(
        issue_number=31,
        generation=1,
        sequence=3,
        phase=CampaignPhase.CANDIDATES,
        processed_event_ids=("event-1", "event-2", "event-3"),
        spec_sha256="a" * 64,
        baseline_evaluation_id="eval-baseline",
        candidates=(
            {
                "candidate_id": "candidate-1",
                "eligible": False,
                "evidence_sha256": "b" * 64,
            },
        ),
    )

    result = campaign.advance(
        AdvanceRequest(
            issue_number=31,
            state=state,
            events=(_event("event-4", EventKind.SLATE_PUBLISHED),),
        )
    )

    assert result.state.phase is CampaignPhase.BLOCKED
    assert result.state.block_reason == "no_eligible_candidates"


def test_candidate_worker_completion_blocks_without_publishing_slate() -> None:
    state = CampaignState(
        issue_number=31,
        generation=1,
        sequence=3,
        phase=CampaignPhase.CANDIDATES,
        processed_event_ids=("event-1", "event-2", "event-3"),
        spec_sha256="a" * 64,
        baseline_evaluation_id="eval-baseline",
        candidates=(
            {
                "candidate_id": "candidate-1",
                "eligible": False,
                "evidence_sha256": "b" * 64,
            },
        ),
    )

    result = OptimizationCampaign().advance(
        AdvanceRequest(
            31,
            state,
            (
                _event(
                    "candidate-workers-1-max_candidates",
                    EventKind.CANDIDATE_WORKERS_COMPLETED,
                    attempted_count=1,
                    eligible_count=0,
                    stop_reason="max_candidates",
                ),
            ),
        )
    )

    assert result.state.phase is CampaignPhase.BLOCKED
    assert result.state.block_reason == "no_eligible_candidates"

    with pytest.raises(
        InvalidCampaignTransition,
        match="counters",
    ):
        OptimizationCampaign().advance(
            AdvanceRequest(
                31,
                state,
                (
                    _event(
                        "candidate-workers-mismatch",
                        EventKind.CANDIDATE_WORKERS_COMPLETED,
                        attempted_count=2,
                        eligible_count=0,
                        stop_reason="max_candidates",
                    ),
                ),
            )
        )


def test_state_contract_rejects_truthy_non_boolean_and_impossible_phase() -> None:
    with pytest.raises(ValueError, match="eligible must be boolean"):
        CampaignState(
            issue_number=31,
            generation=1,
            sequence=1,
            phase=CampaignPhase.CANDIDATES,
            processed_event_ids=("event-1",),
            spec_sha256="a" * 64,
            baseline_evaluation_id="eval-baseline",
            candidates=(
                {
                    "candidate_id": "candidate-1",
                    "eligible": "false",
                    "evidence_sha256": "b" * 64,
                },
            ),
        )

    with pytest.raises(ValueError, match="deployment lineage"):
        CampaignState(
            issue_number=31,
            generation=1,
            sequence=1,
            phase=CampaignPhase.RETENTION,
            processed_event_ids=("event-1",),
            spec_sha256="a" * 64,
            baseline_evaluation_id="eval-baseline",
        )


def test_terminal_campaign_cannot_be_resurrected_by_edit() -> None:
    campaign = OptimizationCampaign()
    completed = CampaignState(
        issue_number=31,
        generation=1,
        sequence=1,
        phase=CampaignPhase.COMPLETED,
        processed_event_ids=("event-1",),
        spec_sha256="a" * 64,
        baseline_evaluation_id="eval-baseline",
        candidates=(
            {
                "candidate_id": "candidate-1",
                "eligible": True,
                "evidence_sha256": "b" * 64,
            },
        ),
        selected_candidate_id="candidate-1",
        merge_commit="c" * 40,
        deployment_version=5,
    )

    result = campaign.advance(
        AdvanceRequest(
            issue_number=31,
            state=completed,
            events=(
                _event(
                    "event-2",
                    EventKind.ISSUE_EDITED,
                    generation=2,
                ),
            ),
        )
    )

    assert result.state.generation == 2
    assert result.state.phase is CampaignPhase.COMPLETED
    assert result.state.spec_sha256 == completed.spec_sha256
    assert result.state.selected_candidate_id == completed.selected_candidate_id


def test_issue_closure_preserves_completed_outcome() -> None:
    campaign = OptimizationCampaign()
    completed = CampaignState(
        issue_number=31,
        generation=1,
        sequence=1,
        phase=CampaignPhase.COMPLETED,
        processed_event_ids=("event-1",),
        spec_sha256="a" * 64,
        baseline_evaluation_id="eval-baseline",
        candidates=(
            {
                "candidate_id": "candidate-1",
                "eligible": True,
                "evidence_sha256": "b" * 64,
            },
        ),
        selected_candidate_id="candidate-1",
        merge_commit="c" * 40,
        deployment_version=5,
    )

    result = campaign.advance(
        AdvanceRequest(
            issue_number=31,
            state=completed,
            events=(_event("event-2", EventKind.ISSUE_CLOSED),),
        )
    )

    assert result.state.phase is CampaignPhase.COMPLETED
    assert result.state.sequence == 2
    assert result.disposition is AdvanceDisposition.COMPLETE


def test_reopened_cancelled_issue_supersedes_generation() -> None:
    campaign = OptimizationCampaign()
    cancelled = CampaignState(
        issue_number=31,
        generation=2,
        sequence=4,
        phase=CampaignPhase.CANCELLED,
        processed_event_ids=("event-1", "event-2", "event-3", "event-4"),
    )

    result = campaign.advance(
        AdvanceRequest(
            issue_number=31,
            state=cancelled,
            events=(
                _event(
                    "event-5",
                    EventKind.ISSUE_REOPENED,
                    generation=3,
                ),
            ),
        )
    )

    assert result.state.generation == 3
    assert result.state.sequence == 5
    assert result.state.phase is CampaignPhase.SPECIFICATION
    assert result.disposition is AdvanceDisposition.ADVANCE


@pytest.mark.parametrize(
    "terminal",
    (
        CampaignState(
            issue_number=31,
            generation=2,
            sequence=4,
            phase=CampaignPhase.BLOCKED,
            processed_event_ids=(
                "event-1",
                "event-2",
                "event-3",
                "event-4",
            ),
            block_reason="no_eligible_candidates",
        ),
        CampaignState(
            issue_number=31,
            generation=2,
            sequence=4,
            phase=CampaignPhase.COMPLETED,
            processed_event_ids=(
                "event-1",
                "event-2",
                "event-3",
                "event-4",
            ),
            spec_sha256="a" * 64,
            baseline_evaluation_id="baseline-1",
            candidates=(
                {
                    "candidate_id": "candidate-1",
                    "eligible": True,
                    "evidence_sha256": "b" * 64,
                },
            ),
            selected_candidate_id="candidate-1",
            merge_commit="c" * 40,
            deployment_version=3,
        ),
    ),
)
def test_closed_terminal_campaign_can_reopen_as_new_generation(
    terminal: CampaignState,
) -> None:
    result = OptimizationCampaign().advance(
        AdvanceRequest(
            issue_number=31,
            state=terminal,
            events=(
                _event(
                    "event-5",
                    EventKind.ISSUE_CLOSED,
                    generation=2,
                ),
                _event(
                    "event-6",
                    EventKind.ISSUE_REOPENED,
                    generation=3,
                ),
            ),
        )
    )

    assert result.state.generation == 3
    assert result.state.sequence == 6
    assert result.state.phase is CampaignPhase.SPECIFICATION
    assert result.state.block_reason is None


def test_issue_declassification_cancels_active_generation() -> None:
    state = CampaignState(
        issue_number=31,
        generation=2,
        sequence=3,
        phase=CampaignPhase.CANDIDATES,
        processed_event_ids=("event-1", "event-2", "event-3"),
        spec_sha256="a" * 64,
        baseline_evaluation_id="baseline-1",
    )

    result = OptimizationCampaign().advance(
        AdvanceRequest(
            issue_number=31,
            state=state,
            events=(
                _event(
                    "event-4",
                    EventKind.ISSUE_DECLASSIFIED,
                    generation=2,
                ),
            ),
        )
    )

    assert result.state.phase is CampaignPhase.CANCELLED
    assert result.state.generation == 2
    assert result.disposition is AdvanceDisposition.WAIT
