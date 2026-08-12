from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class WorkspacePhase(StrEnum):
    SPECIFICATION = "specification"
    EVALUATING = "evaluating"
    AWAITING_SELECTION = "awaiting_selection"
    DEPLOYMENT = "deployment"
    RETENTION = "retention"
    COMPLETED = "completed"


class WorkspaceTrigger(StrEnum):
    ISSUE_CREATED = "issue_created"
    CONTINUE = "continue"
    EXPERIMENTS_COMPLETED = "experiments_completed"
    PULL_REQUEST_MERGED = "pull_request_merged"
    DEPLOYMENT_COMPLETED = "deployment_completed"
    RETENTION_COMPLETED = "retention_completed"


@dataclass(frozen=True)
class WorkspaceIssue:
    number: int
    title: str
    body: str
    base_commit: str


@dataclass(frozen=True)
class WorkspaceRequest:
    repository_root: Path
    issue: WorkspaceIssue
    trigger: WorkspaceTrigger
    workspace_pull_request: WorkspacePullRequest | None = None


@dataclass(frozen=True)
class WorkspacePullRequest:
    number: int | None
    issue_number: int
    branch: str
    title: str
    draft: bool
    reuse_existing: bool
    base_commit: str


@dataclass(frozen=True)
class WorkspaceResult:
    phase: WorkspacePhase
    workspace_pull_request: WorkspacePullRequest | None
    planned_effect_kinds: tuple[str, ...]


class OptimizationWorkspace:
    def advance(self, request: WorkspaceRequest) -> WorkspaceResult:
        issue = request.issue
        pull_request = request.workspace_pull_request
        if pull_request is not None and (
            pull_request.issue_number != issue.number
            or pull_request.branch
            != f"foundry-opt/workspace/issue-{issue.number}"
            or pull_request.base_commit != issue.base_commit
        ):
            raise ValueError("workspace pull request does not match issue")
        if pull_request is None:
            pull_request = WorkspacePullRequest(
                number=None,
                issue_number=issue.number,
                branch=f"foundry-opt/workspace/issue-{issue.number}",
                title=(
                    f"[Optimize] #{issue.number} workspace - "
                    "draft, not yet selectable"
                ),
                draft=True,
                reuse_existing=True,
                base_commit=issue.base_commit,
            )
        return WorkspaceResult(
            phase=WorkspacePhase.SPECIFICATION,
            workspace_pull_request=pull_request,
            planned_effect_kinds=("workspace_pr_sync",),
        )
