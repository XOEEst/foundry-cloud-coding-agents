from __future__ import annotations

from pathlib import Path
from typing import Protocol

from foundry_opt.orchestration.workspace import WorkspacePullRequest
from foundry_opt.orchestration.workspace_store import (
    WorkspaceSnapshot,
    WorkspaceUpdate,
)


class WorkspaceStore(Protocol):
    def load(self, issue_number: int) -> WorkspaceSnapshot | None: ...

    def commit(
        self,
        *,
        expected_revision: str | None,
        update: WorkspaceUpdate,
    ) -> WorkspaceSnapshot: ...


class WorkspacePullRequestAdapter(Protocol):
    def synchronize(
        self,
        repository_root: Path,
        pull_request: WorkspacePullRequest,
    ) -> WorkspacePullRequest: ...


class PlanningWorkspacePullRequests:
    def synchronize(
        self,
        repository_root: Path,
        pull_request: WorkspacePullRequest,
    ) -> WorkspacePullRequest:
        return pull_request
