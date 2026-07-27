"""GitHub campaign publication and exact-candidate workflow."""

from foundry_opt.github_workflow.errors import (
    CampaignPublicationError,
    CandidatePublicationError,
    GitHubPermissionDeniedError,
    GitHubWorkflowError,
    PatchApplicationError,
    PatchTraversalError,
)
from foundry_opt.github_workflow.models import (
    AppliedPatch,
    ArtifactReference,
    ArtifactInspection,
    CampaignPublication,
    CampaignPublicationRequest,
    CandidateApplicationResult,
    CandidateApplicationStatus,
    CandidateApplicationRequest,
    CandidateIssuePublication,
    GitHubCapabilities,
    GitHubPermissionReport,
    ExactPatchRequest,
    IssueReference,
    PullRequestReference,
    RepositoryState,
    WorkflowFailure,
)
from foundry_opt.github_workflow.candidate import verify_and_apply_candidate
from foundry_opt.github_workflow.publication import publish_campaign

__all__ = [
    "AppliedPatch",
    "ArtifactReference",
    "ArtifactInspection",
    "CampaignPublication",
    "CampaignPublicationError",
    "CampaignPublicationRequest",
    "CandidateApplicationRequest",
    "CandidateApplicationResult",
    "CandidateApplicationStatus",
    "CandidateIssuePublication",
    "CandidatePublicationError",
    "ExactPatchRequest",
    "GitHubCapabilities",
    "GitHubPermissionDeniedError",
    "GitHubPermissionReport",
    "GitHubWorkflowError",
    "IssueReference",
    "PatchApplicationError",
    "PatchTraversalError",
    "PullRequestReference",
    "RepositoryState",
    "WorkflowFailure",
    "publish_campaign",
    "verify_and_apply_candidate",
]
