from __future__ import annotations

from foundry_opt.github_workflow.models import GitHubCapabilities


class GitHubWorkflowError(RuntimeError):
    code = "github_workflow_error"


class GitHubPermissionDeniedError(GitHubWorkflowError):
    code = "permission_denied"

    def __init__(self, missing: GitHubCapabilities) -> None:
        self.missing = missing
        super().__init__("GitHub token lacks required workflow permissions")


class CampaignPublicationError(GitHubWorkflowError):
    code = "campaign_publication_failed"


class CandidatePublicationError(GitHubWorkflowError):
    code = "candidate_publication_failed"


class PatchApplicationError(GitHubWorkflowError):
    code = "patch_application_failed"


class PatchTraversalError(PatchApplicationError):
    code = "path_traversal"
