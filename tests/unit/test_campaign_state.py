from datetime import UTC, datetime
from pathlib import Path

from foundry_opt.campaign.lineage import IdeaLineage
from foundry_opt.campaign.models import CandidateArtifact, PatchArtifact
from foundry_opt.campaign.state import (
    CampaignState,
    CandidateState,
    FileCampaignStateStore,
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
    state = CampaignState(
        "campaign-1",
        "agent",
        "b" * 40,
        "completed",
        now,
        now,
        "draft-baseline",
        {"quality": 0.5},
        (
            CandidateState(
                "candidate-1",
                1,
                "evaluated",
                1,
                IdeaLineage(
                    "idea-1",
                    (),
                    "system_instructions",
                    (Path("agent/instructions.md"),),
                ),
                artifact,
                {"quality": 0.9},
                None,
                {"generation_seconds": 12.0, "total_seconds": 30.0},
            ),
        ),
        1,
        0,
        ("candidate-1",),
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
    assert "response" not in text
    assert "secret" not in text


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
        ),
    )

    recovered = datetime(2026, 7, 26, 10, tzinfo=UTC)
    store.mark_stale(tmp_path, "campaign-1", recovered)

    state = store.load(tmp_path, "campaign-1")
    assert state is not None
    assert state.status == "stale"
    assert state.updated_at == recovered
    assert state.error_code == "stale_lock_recovered"
