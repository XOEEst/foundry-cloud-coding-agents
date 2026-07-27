"""Campaign orchestration contracts."""

from foundry_opt.campaign.engine import CampaignDependencies, run_campaign
from foundry_opt.campaign.models import (
    CampaignLimits,
    CampaignReport,
    CandidateArtifact,
    PatchArtifact,
)
from foundry_opt.campaign.protocols import CampaignRequest

__all__ = [
    "CampaignDependencies",
    "CampaignLimits",
    "CampaignReport",
    "CampaignRequest",
    "CandidateArtifact",
    "PatchArtifact",
    "run_campaign",
]
