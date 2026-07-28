from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from foundry_opt.campaign.lineage import IdeaLineage
from foundry_opt.campaign.models import CandidateArtifact, PatchArtifact
from foundry_opt.campaign.state import (
    CampaignState,
    CandidateState,
    DraftCreationIntent,
    DraftMetadata,
    FileCampaignStateStore,
    FinalizedPublication,
)
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
from foundry_opt.evidence import EvaluationAssetReference


GOAL_SHA256 = "1" * 64
SPEC_SHA256 = "2" * 64
ASSETS = (
    EvaluationAssetReference(
        asset_id="dataset-dev",
        kind="dataset",
        source="repository",
        role="development",
    ),
)


def test_file_campaign_state_round_trips_safe_resumable_metadata(
    tmp_path: Path,
) -> None:
    patch = PatchArtifact(
        "candidate-1",
        Path(".foundry-optimizer/campaigns/campaign-1/candidate-1.patch"),
        "a" * 64,
        "b" * 40,
        "c" * 40,
    )
    artifact = CandidateArtifact(
        "candidate-1",
        patch,
        "draft-candidate-1",
        Path(
            ".foundry-optimizer/campaigns/campaign-1/"
            "validation-evidence.json"
        ),
        True,
        {"quality": 0.9},
    )
    now = datetime(2026, 7, 26, 12, tzinfo=UTC)
    draft = DraftMetadata(
        "agent",
        "draft-candidate-1",
        7,
        "d" * 64,
        "ready",
        False,
        "https://resource.services.ai.azure.com/api/projects/project",
    )
    intent = DraftCreationIntent("candidate-1", "e" * 64, "reconciled")
    state = CampaignState(
        "campaign-1",
        "agent",
        "b" * 40,
        "completed",
        now,
        now,
        GOAL_SHA256,
        SPEC_SHA256,
        ASSETS,
        "draft-baseline",
        {"quality": 0.5},
        (
            CandidateState(
                candidate_id="candidate-1",
                slot=1,
                status="evaluated",
                attempts=1,
                lineage=IdeaLineage(
                    "idea-1",
                    (),
                    "system_instructions",
                    (Path("agent/instructions.md"),),
                ),
                artifact=artifact,
                metrics={"quality": 0.9},
                timings={
                    "generation_seconds": 12.0,
                    "total_seconds": 30.0,
                },
                draft=draft,
                draft_intent=intent,
            ),
        ),
        1,
        0,
        ("candidate-1",),
        baseline_draft=replace(draft, version_id="draft-baseline"),
        baseline_draft_intent=replace(intent, subject_id="baseline"),
    )
    store = FileCampaignStateStore()

    store.save(tmp_path, state)
    loaded = store.load(tmp_path, "campaign-1")

    assert loaded == state
    state_path = (
        tmp_path
        / ".foundry-optimizer"
        / "campaigns"
        / "campaign-1"
        / "state.json"
    )
    text = state_path.read_text(encoding="utf-8")
    assert "instructions.md" in text
    assert "generation_seconds" in text
    assert "draft-baseline" in text
    assert '"idempotency_key": "eeee' in text
    assert "response" not in text
    assert "secret" not in text
    assert GOAL_SHA256 in text
    assert SPEC_SHA256 in text
    assert "dataset-dev" in text


def _evaluation_result(subject_id: str) -> EvaluationResult:
    run = EvaluationRun(
        run_id=f"run-{subject_id}",
        evaluation_id=f"eval-{subject_id}",
        subject_id=subject_id,
        split=DatasetSplit.DEVELOPMENT,
        agent=AgentVersionRef("agent", f"draft-{subject_id}", "7"),
        dataset=DatasetVersionRef("dataset", "1"),
        evaluator=EvaluatorDefinitionRef("quality", "1"),
        status=EvaluationStatus.COMPLETED,
        portal_url=None,
        started_at=None,
        completed_at=None,
        error=None,
    )
    case = NormalizedCase(
        "case-1",
        "case-hash",
        (f"response-id-{subject_id}",),
        (NormalizedCaseMetric("quality", 0.8, 0.8, None, Outcome.PASS),),
        Usage(),
        None,
        None,
        1,
    )
    return EvaluationResult(
        run=run,
        cases=(case,),
        metrics={
            "quality": MetricAggregate(
                "quality", 0.8, 0.8, 0.8, 0.0, Outcome.PASS, 1
            )
        },
        usage=Usage(),
        duration_ms=1,
        errors=(),
        complete=True,
        needs_repeat=False,
        attempts=1,
    )


def test_file_campaign_state_round_trips_handoff_metadata(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 26, 12, tzinfo=UTC)
    state = CampaignState(
        campaign_id="campaign-1",
        target="agent",
        base_commit="b" * 40,
        status="active",
        started_at=now,
        updated_at=now,
        goal_sha256=GOAL_SHA256,
        spec_sha256=SPEC_SHA256,
        assets=ASSETS,
        baseline_draft_id="draft-baseline",
        baseline_metrics={"quality": 0.5},
        candidates=(
            CandidateState(
                candidate_id="candidate-1",
                slot=1,
                status="awaiting_idea",
                attempts=1,
                context_path=(
                    ".foundry-optimizer/campaigns/campaign-1/candidates/"
                    "candidate-1/context.json"
                ),
                context_sha256="a" * 64,
            ),
            CandidateState(
                candidate_id="candidate-2",
                slot=2,
                status="evaluated",
                attempts=1,
                idea_path=(
                    ".foundry-optimizer/campaigns/campaign-1/candidates/"
                    "candidate-2/idea.json"
                ),
                idea_sha256="c" * 64,
                metrics={"quality": 0.9},
                provisional_eligible=True,
                development_result=_evaluation_result("candidate-2"),
            ),
        ),
        launched_slots=2,
        baseline_development=_evaluation_result("baseline"),
        awaiting_candidate_id="candidate-1",
        finalized=FinalizedPublication(
            campaign_pull_request_number=42,
            campaign_pull_request_url=(
                "https://github.com/octo-org/optimizer/pull/42"
            ),
            candidate_issue_numbers={"candidate-2": 101},
        ),
    )
    store = FileCampaignStateStore()

    store.save(tmp_path, state)
    loaded = store.load(tmp_path, "campaign-1")

    assert loaded == state
    assert loaded is not None
    assert loaded.baseline_development == _evaluation_result("baseline")
    assert (
        loaded.candidates[1].development_result
        == _evaluation_result("candidate-2")
    )
    assert loaded.awaiting_candidate_id == "candidate-1"
    assert loaded.finalized is not None
    assert loaded.finalized.candidate_issue_numbers["candidate-2"] == 101


def test_file_campaign_state_marks_abandoned_campaign_stale(
    tmp_path: Path,
) -> None:
    started = datetime(2026, 7, 26, 8, tzinfo=UTC)
    store = FileCampaignStateStore()
    store.save(
        tmp_path,
        CampaignState(
            "campaign-1",
            "agent",
            "b" * 40,
            "active",
            started,
            started,
            GOAL_SHA256,
            SPEC_SHA256,
            ASSETS,
        ),
    )

    recovered = datetime(2026, 7, 26, 10, tzinfo=UTC)
    store.mark_stale(tmp_path, "campaign-1", recovered)

    state = store.load(tmp_path, "campaign-1")
    assert state is not None
    assert state.status == "stale"
    assert state.updated_at == recovered
    assert state.error_code == "stale_lock_recovered"
