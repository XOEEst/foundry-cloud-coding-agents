from pathlib import Path

import pytest

from foundry_opt.orchestration import (
    OptimizationWorkspace,
    WorkspaceIssue,
    WorkspacePhase,
    WorkspacePullRequest,
    WorkspaceRequest,
    WorkspaceTrigger,
)


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
