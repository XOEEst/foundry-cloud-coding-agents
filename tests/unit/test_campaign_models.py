from pathlib import Path

import pytest

from foundry_opt.campaign.models import (
    CampaignLimits,
    CampaignReport,
    CandidateArtifact,
    PatchArtifact,
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
    EvaluationAssetReference(
        asset_id="dataset-val",
        kind="dataset",
        source="repository",
        role="validation",
    ),
    EvaluationAssetReference(
        asset_id="evaluator-quality",
        kind="evaluator",
        source="builtin",
        name="quality",
        version="1",
        metrics=("quality",),
    ),
)


def _patch(candidate_id: str = "candidate-1") -> PatchArtifact:
    return PatchArtifact(
        candidate_id=candidate_id,
        path=Path(f".foundry-optimizer/campaigns/c1/{candidate_id}.patch"),
        sha256="a" * 64,
        base_commit="b" * 40,
        result_commit="c" * 40,
    )


def _candidate(candidate_id: str = "candidate-1") -> CandidateArtifact:
    return CandidateArtifact(
        candidate_id=candidate_id,
        patch=_patch(candidate_id),
        draft_id="draft-123",
        evidence_path=Path(
            f".foundry-optimizer/campaigns/c1/{candidate_id}.json"
        ),
        eligible=True,
        metrics={"quality": 0.9},
    )


def test_campaign_limits_preserve_the_cloud_agent_operating_envelope() -> None:
    limits = CampaignLimits(
        deadline_minutes=50,
        candidate_cutoff_minutes=40,
        max_changed_candidates=3,
        transient_retries=1,
    )

    assert limits.max_changed_candidates == 3

    with pytest.raises(ValueError):
        CampaignLimits(51, 40, 3, 1)
    with pytest.raises(ValueError):
        CampaignLimits(40, 40, 3, 1)


def test_patch_artifact_requires_exact_hashes_and_repository_paths() -> None:
    artifact = _patch()

    assert artifact.path.as_posix().endswith("candidate-1.patch")

    with pytest.raises(ValueError):
        PatchArtifact(
            candidate_id="candidate-1",
            path=Path("../outside.patch"),
            sha256="not-a-hash",
            base_commit="main",
            result_commit="result",
        )


def test_campaign_report_binds_pareto_ids_to_unique_candidates() -> None:
    candidate = _candidate()
    report = CampaignReport(
        campaign_id="campaign-1",
        target="acceptance-agent",
        base_commit="b" * 40,
        baseline_draft_id="draft-baseline",
        candidates=(candidate,),
        pareto_candidate_ids=("candidate-1",),
        goal_sha256=GOAL_SHA256,
        spec_sha256=SPEC_SHA256,
        assets=ASSETS,
    )

    assert report.pareto_candidate_ids == ("candidate-1",)
    assert report.goal_sha256 == GOAL_SHA256
    assert report.spec_sha256 == SPEC_SHA256
    assert report.assets == ASSETS

    with pytest.raises(ValueError):
        CampaignReport(
            campaign_id="campaign-1",
            target="acceptance-agent",
            base_commit="b" * 40,
            baseline_draft_id="draft-baseline",
            candidates=(candidate,),
            pareto_candidate_ids=("missing",),
            goal_sha256=GOAL_SHA256,
            spec_sha256=SPEC_SHA256,
            assets=ASSETS,
        )

    with pytest.raises(ValueError):
        CampaignReport(
            campaign_id="campaign-1",
            target="acceptance-agent",
            base_commit="b" * 40,
            baseline_draft_id="draft-baseline",
            candidates=(candidate,),
            pareto_candidate_ids=("candidate-1",),
            goal_sha256="not-a-hash",
            spec_sha256=SPEC_SHA256,
            assets=ASSETS,
        )

    with pytest.raises(ValueError):
        CampaignReport(
            campaign_id="campaign-1",
            target="acceptance-agent",
            base_commit="b" * 40,
            baseline_draft_id="draft-baseline",
            candidates=(candidate,),
            pareto_candidate_ids=("candidate-1",),
            goal_sha256=GOAL_SHA256,
            spec_sha256=SPEC_SHA256,
            assets=(ASSETS[0], ASSETS[0]),
        )
