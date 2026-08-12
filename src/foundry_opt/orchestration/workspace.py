from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from foundry_opt.orchestration.workspace_runtime import (
        WorkspacePullRequestAdapter,
        WorkspaceStore,
    )
    from foundry_opt.orchestration.workspace_store import (
        WorkspaceSnapshot,
        WorkspaceUpdate,
    )


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
    recorded: bool = False
    issue_status_projection_intent: (
        WorkspaceIssueStatusProjectionIntent | None
    ) = None

    def to_dict(self) -> dict[str, Any]:
        pull_request = self.workspace_pull_request
        return {
            "issue_number": (
                pull_request.issue_number
                if pull_request is not None
                else None
            ),
            "issue_status_projection_intent": (
                self.issue_status_projection_intent.to_dict()
                if self.issue_status_projection_intent is not None
                else None
            ),
            "phase": self.phase.value,
            "planned_effect_kinds": list(self.planned_effect_kinds),
            "recorded": self.recorded,
            "workspace_pull_request": (
                {
                    "base_commit": pull_request.base_commit,
                    "branch": pull_request.branch,
                    "draft": pull_request.draft,
                    "issue_number": pull_request.issue_number,
                    "number": pull_request.number,
                    "reuse_existing": pull_request.reuse_existing,
                    "title": pull_request.title,
                }
                if pull_request is not None
                else None
            ),
        }


@dataclass(frozen=True)
class WorkspaceIssueStatusProjectionIntent:
    issue_number: int
    phase: WorkspacePhase
    workspace_pull_request_number: int

    def __post_init__(self) -> None:
        if type(self.issue_number) is not int or self.issue_number < 1:
            raise ValueError("workspace projection issue is invalid")
        if (
            type(self.workspace_pull_request_number) is not int
            or self.workspace_pull_request_number < 1
        ):
            raise ValueError(
                "workspace projection pull request is invalid"
            )

    def to_dict(self) -> dict[str, int | str]:
        return {
            "issue_number": self.issue_number,
            "kind": "workspace_issue_status",
            "phase": self.phase.value,
            "status": "workspace_ready",
            "workspace_pull_request_number": (
                self.workspace_pull_request_number
            ),
        }


class OptimizationWorkspace:
    def __init__(
        self,
        *,
        store: WorkspaceStore | None = None,
        pull_requests: WorkspacePullRequestAdapter | None = None,
    ) -> None:
        from foundry_opt.orchestration.workspace_runtime import (
            PlanningWorkspacePullRequests,
        )
        from foundry_opt.orchestration.workspace_store import (
            InMemoryWorkspaceStore,
        )

        self._store = (
            store if store is not None else InMemoryWorkspaceStore()
        )
        self._pull_requests = (
            pull_requests
            if pull_requests is not None
            else PlanningWorkspacePullRequests()
        )

    def advance(self, request: WorkspaceRequest) -> WorkspaceResult:
        if request.trigger not in {
            WorkspaceTrigger.ISSUE_CREATED,
            WorkspaceTrigger.CONTINUE,
        }:
            raise ValueError(
                f"workspace trigger is not implemented: {request.trigger}"
            )
        issue = request.issue
        snapshot = self._store.load(issue.number)
        pull_request = request.workspace_pull_request
        if (
            request.trigger is WorkspaceTrigger.CONTINUE
            and snapshot is None
            and pull_request is None
        ):
            raise ValueError(
                "workspace pull request is required to continue"
            )
        expected_number = (
            snapshot.workspace_pull_request_number
            if snapshot is not None
            and snapshot.workspace_pull_request_number is not None
            else pull_request.number
            if pull_request is not None
            else None
        )
        if pull_request is not None:
            self._validate_pull_request(
                issue=issue,
                pull_request=pull_request,
                expected_number=expected_number,
            )
        if pull_request is None:
            pull_request = WorkspacePullRequest(
                number=(
                    snapshot.workspace_pull_request_number
                    if snapshot is not None
                    else None
                ),
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
        pull_request = self._pull_requests.synchronize(
            request.repository_root,
            pull_request,
        )
        self._validate_pull_request(
            issue=issue,
            pull_request=pull_request,
            expected_number=expected_number,
        )
        phase = (
            snapshot.phase
            if snapshot is not None
            else WorkspacePhase.SPECIFICATION
        )
        recorded = (
            snapshot is None
            or snapshot.workspace_pull_request_number
            != pull_request.number
        )
        if recorded:
            self._store.commit(
                expected_revision=(
                    snapshot.revision if snapshot is not None else None
                ),
                update=self._workspace_update(
                    request=request,
                    pull_request=pull_request,
                    phase=phase,
                    snapshot=snapshot,
                ),
            )
        return WorkspaceResult(
            phase=phase,
            workspace_pull_request=pull_request,
            planned_effect_kinds=("workspace_pr_sync",),
            recorded=recorded,
            issue_status_projection_intent=(
                WorkspaceIssueStatusProjectionIntent(
                    issue_number=issue.number,
                    phase=phase,
                    workspace_pull_request_number=pull_request.number,
                )
                if pull_request.number is not None
                else None
            ),
        )

    @staticmethod
    def _workspace_update(
        *,
        request: WorkspaceRequest,
        pull_request: WorkspacePullRequest,
        phase: WorkspacePhase,
        snapshot: WorkspaceSnapshot | None,
    ) -> WorkspaceUpdate:
        from foundry_opt.orchestration.workspace_store import WorkspaceUpdate

        return WorkspaceUpdate(
            issue_number=request.issue.number,
            phase=phase,
            workspace_pull_request_number=pull_request.number,
            semantic_event=request.trigger.value,
            candidates=snapshot.candidates if snapshot is not None else (),
            selected_patch=(
                snapshot.selected_patch if snapshot is not None else None
            ),
            external_operation_ids=(
                snapshot.external_operation_ids
                if snapshot is not None
                else ()
            ),
        )

    @staticmethod
    def _validate_pull_request(
        *,
        issue: WorkspaceIssue,
        pull_request: WorkspacePullRequest,
        expected_number: int | None,
    ) -> None:
        if (
            pull_request.issue_number != issue.number
            or pull_request.branch
            != f"foundry-opt/workspace/issue-{issue.number}"
            or pull_request.title
            != (
                f"[Optimize] #{issue.number} workspace - "
                "draft, not yet selectable"
            )
            or pull_request.draft is not True
            or pull_request.reuse_existing is not True
            or pull_request.base_commit != issue.base_commit
            or (
                expected_number is not None
                and pull_request.number != expected_number
            )
        ):
            raise ValueError("workspace pull request does not match issue")
