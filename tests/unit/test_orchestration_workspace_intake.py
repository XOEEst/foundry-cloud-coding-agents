from __future__ import annotations

from foundry_opt.orchestration import (
    TrustedWorkspaceEventContext,
    WorkspaceEventKind,
    WorkspaceTrigger,
    normalize_workspace_event,
)
from foundry_opt.orchestration.workspace_github import (
    workspace_pull_request_base_marker,
    workspace_pull_request_marker,
)


def _context(event_name: str) -> TrustedWorkspaceEventContext:
    return TrustedWorkspaceEventContext(
        event_name=event_name,
        delivery_id="delivery-123",
        repository="octo-org/optimizer",
        repository_id=123,
    )


def test_trusted_issue_opened_normalizes_to_workspace_creation() -> None:
    event = normalize_workspace_event(
        {
            "action": "opened",
            "issue": {
                "number": 31,
                "title": "[Optimize] Improve policy coverage",
                "body": "Improve policy coverage without weakening safety.",
            },
            "repository": {
                "full_name": "octo-org/optimizer",
                "id": 123,
            },
        },
        _context("issues"),
        base_commit="a" * 40,
    )

    assert event.kind is WorkspaceEventKind.ISSUE
    assert event.trigger is WorkspaceTrigger.ISSUE_CREATED
    assert event.issue_number == 31
    assert event.base_commit == "a" * 40
    assert event.workspace_pull_request is None


def test_trusted_workspace_pr_event_normalizes_same_pr_continuation() -> None:
    body = "\n".join(
        (
            workspace_pull_request_marker(31),
            workspace_pull_request_base_marker("a" * 40),
        )
    )

    event = normalize_workspace_event(
        {
            "action": "synchronize",
            "pull_request": {
                "number": 104,
                "title": (
                    "[Optimize] #31 workspace - draft, not yet selectable"
                ),
                "body": body,
                "draft": True,
                "state": "open",
                "head": {
                    "ref": "foundry-opt/workspace/issue-31",
                    "repo": {"full_name": "octo-org/optimizer"},
                },
            },
            "repository": {
                "full_name": "octo-org/optimizer",
                "id": 123,
            },
        },
        _context("pull_request"),
    )

    assert event.kind is WorkspaceEventKind.PULL_REQUEST
    assert event.trigger is WorkspaceTrigger.CONTINUE
    assert event.workspace_pull_request is not None
    assert event.workspace_pull_request.number == 104
    assert event.workspace_pull_request.base_commit == "a" * 40


def test_trusted_merged_workspace_pr_normalizes_deployment_transition() -> None:
    body = "\n".join(
        (
            workspace_pull_request_marker(31),
            workspace_pull_request_base_marker("a" * 40),
        )
    )

    event = normalize_workspace_event(
        {
            "action": "closed",
            "pull_request": {
                "number": 104,
                "title": "[Optimize] #31 selected candidate",
                "body": body,
                "draft": False,
                "state": "closed",
                "merged": True,
                "head": {
                    "ref": "foundry-opt/workspace/issue-31",
                    "repo": {"full_name": "octo-org/optimizer"},
                },
            },
            "repository": {
                "full_name": "octo-org/optimizer",
                "id": 123,
            },
        },
        _context("pull_request"),
    )

    assert event.trigger is WorkspaceTrigger.PULL_REQUEST_MERGED
    assert event.workspace_pull_request is not None
    assert event.workspace_pull_request.number == 104
    assert event.workspace_pull_request.draft is False


def test_closed_unmerged_workspace_pr_fails_closed() -> None:
    import pytest

    body = "\n".join(
        (
            workspace_pull_request_marker(31),
            workspace_pull_request_base_marker("a" * 40),
        )
    )

    with pytest.raises(ValueError, match="action"):
        normalize_workspace_event(
            {
                "action": "closed",
                "pull_request": {
                    "number": 104,
                    "title": "[Optimize] #31 selected candidate",
                    "body": body,
                    "draft": False,
                    "state": "closed",
                    "merged": False,
                    "head": {
                        "ref": "foundry-opt/workspace/issue-31",
                        "repo": {"full_name": "octo-org/optimizer"},
                    },
                },
                "repository": {
                    "full_name": "octo-org/optimizer",
                    "id": 123,
                },
            },
            _context("pull_request"),
        )


def test_workspace_intake_rejects_a_repository_mismatch() -> None:
    import pytest

    with pytest.raises(ValueError, match="repository"):
        normalize_workspace_event(
            {
                "action": "opened",
                "issue": {
                    "number": 31,
                    "title": "[Optimize] Improve policy coverage",
                    "body": "Improve policy coverage.",
                },
                "repository": {
                    "full_name": "other-org/optimizer",
                    "id": 123,
                },
            },
            _context("issues"),
            base_commit="a" * 40,
        )
