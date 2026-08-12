from dataclasses import replace
from pathlib import Path

import pytest

from foundry_opt.orchestration import (
    InMemoryWorkspaceStore,
    OptimizationWorkspace,
    WorkspaceIssue,
    WorkspacePhase,
    WorkspacePullRequest,
    WorkspaceRequest,
    WorkspaceTrigger,
)


class AssigningWorkspacePullRequests:
    def __init__(self, number: int = 104) -> None:
        self.number = number
        self.synchronized: list[WorkspacePullRequest] = []

    def synchronize(
        self,
        repository_root: Path,
        pull_request: WorkspacePullRequest,
    ) -> WorkspacePullRequest:
        self.synchronized.append(pull_request)
        if pull_request.number is not None:
            return pull_request
        return replace(pull_request, number=self.number)


class MismatchingWorkspacePullRequests(AssigningWorkspacePullRequests):
    def synchronize(
        self,
        repository_root: Path,
        pull_request: WorkspacePullRequest,
    ) -> WorkspacePullRequest:
        synchronized = super().synchronize(repository_root, pull_request)
        return replace(synchronized, issue_number=32)


def test_issue_creation_plans_one_persistent_draft_workspace_pr(
    tmp_path: Path,
) -> None:
    workspace = OptimizationWorkspace()

    result = workspace.advance(
        WorkspaceRequest(
            repository_root=tmp_path,
            issue=WorkspaceIssue(
                number=31,
                title="[Optimize] Improve policy coverage",
                body="Improve policy coverage without weakening safety.",
                base_commit="a" * 40,
            ),
            trigger=WorkspaceTrigger.ISSUE_CREATED,
        )
    )

    assert result.phase is WorkspacePhase.SPECIFICATION
    assert result.workspace_pull_request is not None
    assert result.workspace_pull_request.issue_number == 31
    assert result.workspace_pull_request.branch == "foundry-opt/workspace/issue-31"
    assert result.workspace_pull_request.title == (
        "[Optimize] #31 workspace - draft, not yet selectable"
    )
    assert result.workspace_pull_request.draft is True
    assert result.workspace_pull_request.reuse_existing is True
    assert result.workspace_pull_request.base_commit == "a" * 40
    assert result.planned_effect_kinds == ("workspace_pr_sync",)


def test_issue_creation_commits_the_synchronized_workspace_pr_number(
    tmp_path: Path,
) -> None:
    store = InMemoryWorkspaceStore()
    pull_requests = AssigningWorkspacePullRequests()
    workspace = OptimizationWorkspace(
        store=store,
        pull_requests=pull_requests,
    )

    result = workspace.advance(
        WorkspaceRequest(
            repository_root=tmp_path,
            issue=WorkspaceIssue(
                number=31,
                title="[Optimize] Improve policy coverage",
                body="Improve policy coverage without weakening safety.",
                base_commit="a" * 40,
            ),
            trigger=WorkspaceTrigger.ISSUE_CREATED,
        )
    )

    snapshot = store.load(31)
    assert result.workspace_pull_request is not None
    assert result.workspace_pull_request.number == 104
    assert snapshot is not None
    assert snapshot.workspace_pull_request_number == 104
    assert pull_requests.synchronized == [
        replace(result.workspace_pull_request, number=None)
    ]


def test_continuation_reuses_the_existing_workspace_pr(
    tmp_path: Path,
) -> None:
    workspace = OptimizationWorkspace()
    existing = WorkspacePullRequest(
        number=104,
        issue_number=31,
        branch="foundry-opt/workspace/issue-31",
        title="[Optimize] #31 workspace - draft, not yet selectable",
        draft=True,
        reuse_existing=True,
        base_commit="a" * 40,
    )

    result = workspace.advance(
        WorkspaceRequest(
            repository_root=tmp_path,
            issue=WorkspaceIssue(
                number=31,
                title="[Optimize] Improve policy coverage",
                body="Improve policy coverage without weakening safety.",
                base_commit="a" * 40,
            ),
            trigger=WorkspaceTrigger.CONTINUE,
            workspace_pull_request=existing,
        )
    )

    assert result.workspace_pull_request is existing
    assert result.workspace_pull_request.number == 104
    assert result.planned_effect_kinds == ("workspace_pr_sync",)


def test_continuation_without_workspace_state_fails_closed(
    tmp_path: Path,
) -> None:
    workspace = OptimizationWorkspace(
        store=InMemoryWorkspaceStore(),
        pull_requests=AssigningWorkspacePullRequests(),
    )

    with pytest.raises(ValueError, match="workspace pull request"):
        workspace.advance(
            WorkspaceRequest(
                repository_root=tmp_path,
                issue=WorkspaceIssue(
                    number=31,
                    title="[Optimize] Improve policy coverage",
                    body="Improve policy coverage without weakening safety.",
                    base_commit="a" * 40,
                ),
                trigger=WorkspaceTrigger.CONTINUE,
            )
        )


def test_continuation_loads_the_committed_workspace_pr(
    tmp_path: Path,
) -> None:
    store = InMemoryWorkspaceStore()
    pull_requests = AssigningWorkspacePullRequests()
    workspace = OptimizationWorkspace(
        store=store,
        pull_requests=pull_requests,
    )
    issue = WorkspaceIssue(
        number=31,
        title="[Optimize] Improve policy coverage",
        body="Improve policy coverage without weakening safety.",
        base_commit="a" * 40,
    )
    workspace.advance(
        WorkspaceRequest(
            repository_root=tmp_path,
            issue=issue,
            trigger=WorkspaceTrigger.ISSUE_CREATED,
        )
    )

    result = workspace.advance(
        WorkspaceRequest(
            repository_root=tmp_path,
            issue=issue,
            trigger=WorkspaceTrigger.CONTINUE,
        )
    )

    assert result.workspace_pull_request is not None
    assert result.workspace_pull_request.number == 104
    assert pull_requests.synchronized[-1] == result.workspace_pull_request


def test_repeated_issue_and_continuation_events_are_idempotent(
    tmp_path: Path,
) -> None:
    store = InMemoryWorkspaceStore()
    workspace = OptimizationWorkspace(
        store=store,
        pull_requests=AssigningWorkspacePullRequests(),
    )
    issue = WorkspaceIssue(
        number=31,
        title="[Optimize] Improve policy coverage",
        body="Improve policy coverage without weakening safety.",
        base_commit="a" * 40,
    )

    results = tuple(
        workspace.advance(
            WorkspaceRequest(
                repository_root=tmp_path,
                issue=issue,
                trigger=trigger,
            )
        )
        for trigger in (
            WorkspaceTrigger.ISSUE_CREATED,
            WorkspaceTrigger.ISSUE_CREATED,
            WorkspaceTrigger.CONTINUE,
            WorkspaceTrigger.CONTINUE,
        )
    )

    snapshot = store.load(31)
    assert snapshot is not None
    assert snapshot.revision == "1"
    assert snapshot.workspace_pull_request_number == 104
    assert {
        effect
        for result in results
        for effect in result.planned_effect_kinds
    } == {"workspace_pr_sync"}
    assert {
        result.workspace_pull_request.number
        for result in results
        if result.workspace_pull_request is not None
    } == {104}
    assert [result.recorded for result in results] == [
        True,
        False,
        False,
        False,
    ]


def test_continuation_rejects_a_workspace_pr_for_another_issue(
    tmp_path: Path,
) -> None:
    workspace = OptimizationWorkspace()

    with pytest.raises(ValueError, match="workspace pull request"):
        workspace.advance(
            WorkspaceRequest(
                repository_root=tmp_path,
                issue=WorkspaceIssue(
                    number=31,
                    title="[Optimize] Improve policy coverage",
                    body="Improve policy coverage without weakening safety.",
                    base_commit="a" * 40,
                ),
                trigger=WorkspaceTrigger.CONTINUE,
                workspace_pull_request=WorkspacePullRequest(
                    number=104,
                    issue_number=32,
                    branch="foundry-opt/workspace/issue-32",
                    title="[Optimize] #32 workspace - draft",
                    draft=True,
                    reuse_existing=True,
                    base_commit="a" * 40,
                ),
            )
        )


def test_continuation_rejects_a_different_pr_than_the_committed_workspace(
    tmp_path: Path,
) -> None:
    store = InMemoryWorkspaceStore()
    pull_requests = AssigningWorkspacePullRequests()
    workspace = OptimizationWorkspace(
        store=store,
        pull_requests=pull_requests,
    )
    issue = WorkspaceIssue(
        number=31,
        title="[Optimize] Improve policy coverage",
        body="Improve policy coverage without weakening safety.",
        base_commit="a" * 40,
    )
    workspace.advance(
        WorkspaceRequest(
            repository_root=tmp_path,
            issue=issue,
            trigger=WorkspaceTrigger.ISSUE_CREATED,
        )
    )

    with pytest.raises(ValueError, match="workspace pull request"):
        workspace.advance(
            WorkspaceRequest(
                repository_root=tmp_path,
                issue=issue,
                trigger=WorkspaceTrigger.CONTINUE,
                workspace_pull_request=WorkspacePullRequest(
                    number=105,
                    issue_number=31,
                    branch="foundry-opt/workspace/issue-31",
                    title=(
                        "[Optimize] #31 workspace - "
                        "draft, not yet selectable"
                    ),
                    draft=True,
                    reuse_existing=True,
                    base_commit="a" * 40,
                ),
            )
        )

    snapshot = store.load(31)
    assert snapshot is not None
    assert snapshot.workspace_pull_request_number == 104


def test_issue_creation_rejects_a_mismatched_synchronized_pr(
    tmp_path: Path,
) -> None:
    store = InMemoryWorkspaceStore()
    workspace = OptimizationWorkspace(
        store=store,
        pull_requests=MismatchingWorkspacePullRequests(),
    )

    with pytest.raises(ValueError, match="workspace pull request"):
        workspace.advance(
            WorkspaceRequest(
                repository_root=tmp_path,
                issue=WorkspaceIssue(
                    number=31,
                    title="[Optimize] Improve policy coverage",
                    body="Improve policy coverage without weakening safety.",
                    base_commit="a" * 40,
                ),
                trigger=WorkspaceTrigger.ISSUE_CREATED,
            )
        )

    assert store.load(31) is None


def test_first_slice_rejects_unimplemented_triggers(
    tmp_path: Path,
) -> None:
    workspace = OptimizationWorkspace()

    with pytest.raises(ValueError, match="workspace trigger"):
        workspace.advance(
            WorkspaceRequest(
                repository_root=tmp_path,
                issue=WorkspaceIssue(
                    number=31,
                    title="[Optimize] Improve policy coverage",
                    body="Improve policy coverage without weakening safety.",
                    base_commit="a" * 40,
                ),
                trigger=WorkspaceTrigger.EXPERIMENTS_COMPLETED,
            )
        )
