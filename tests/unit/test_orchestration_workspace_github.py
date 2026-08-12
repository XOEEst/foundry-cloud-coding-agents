from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from pathlib import Path
from typing import Any

import pytest

from foundry_opt.orchestration import (
    InMemoryWorkspaceStore,
    OptimizationWorkspace,
    WorkspaceIssue,
    WorkspacePhase,
    WorkspaceRequest,
    WorkspaceTrigger,
    WorkspaceUpdate,
)
from foundry_opt.orchestration.workspace_github import (
    GhWorkspacePullRequests,
    GitHubWorkspacePullRequestError,
    workspace_pull_request_base_marker,
    workspace_pull_request_marker,
)
from foundry_opt.preflight.interfaces import CommandResult


_REPOSITORY = "octo-org/optimizer"
_BASE_BRANCH = "main"
_ISSUE_NUMBER = 31
_BASE_COMMIT = "a" * 40
_BRANCH = "foundry-opt/workspace/issue-31"


class FakeCommands:
    def __init__(
        self,
        responses: dict[
            tuple[str, ...],
            str | Exception | list[str | Exception],
        ],
    ) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
        environment: Mapping[str, str] | None = None,
        input_text: str | None = None,
        input_bytes: bytes | None = None,
    ) -> CommandResult:
        command = tuple(arguments)
        self.calls.append(
            {
                "arguments": command,
                "cwd": cwd,
                "environment": environment,
                "input_text": input_text,
                "input_bytes": input_bytes,
            }
        )
        response = self.responses.get(command)
        if response is None:
            raise AssertionError(f"unexpected command: {command}")
        if isinstance(response, list):
            if not response:
                raise AssertionError(
                    f"exhausted command responses: {command}"
                )
            response = response.pop(0)
        if isinstance(response, Exception):
            raise response
        return CommandResult(0, response, "")

    @property
    def invocations(self) -> list[tuple[str, ...]]:
        return [call["arguments"] for call in self.calls]


def test_issue_creation_creates_one_draft_workspace_pull_request(
    tmp_path: Path,
) -> None:
    marker = workspace_pull_request_marker(_ISSUE_NUMBER)
    list_by_branch = (
        "gh",
        "pr",
        "list",
        "--repo",
        _REPOSITORY,
        "--state",
        "all",
        "--head",
        _BRANCH,
        "--json",
        "number,headRefName,baseRefName,title,body,isDraft,state",
        "--limit",
        "2",
    )
    list_by_marker = (
        "gh",
        "pr",
        "list",
        "--repo",
        _REPOSITORY,
        "--state",
        "all",
        "--search",
        '"foundry-opt:workspace-pr:issue-31:v1" in:body',
        "--json",
        "number,headRefName,baseRefName,title,body,isDraft,state",
        "--limit",
        "2",
    )
    branch_ref = f"refs/heads/{_BRANCH}"
    commands = FakeCommands(
        {
            list_by_branch: "[]",
            list_by_marker: "[]",
            (
                "git",
                "config",
                "--get-all",
                "remote.origin.url",
            ): f"https://github.com/{_REPOSITORY}.git\n",
            (
                "git",
                "remote",
                "get-url",
                "--push",
                "--all",
                "origin",
            ): f"https://github.com/{_REPOSITORY}.git\n",
            (
                "git",
                "ls-remote",
                "--heads",
                "origin",
                branch_ref,
            ): "",
            (
                "git",
                "rev-parse",
                f"{_BASE_COMMIT}^{{tree}}",
            ): f"{'c' * 40}\n",
            (
                "git",
                "commit-tree",
                "c" * 40,
                "-p",
                _BASE_COMMIT,
                "-m",
                "Create persistent optimization workspace for issue-31",
            ): f"{'b' * 40}\n",
            (
                "git",
                "push",
                f"--force-with-lease={branch_ref}:",
                "origin",
                f"{'b' * 40}:{branch_ref}",
            ): "",
            (
                "gh",
                "pr",
                "create",
                "--repo",
                _REPOSITORY,
                "--draft",
                "--base",
                _BASE_BRANCH,
                "--head",
                _BRANCH,
                "--title",
                "[Optimize] #31 workspace - draft, not yet selectable",
                "--body-file",
                "-",
            ): f"https://github.com/{_REPOSITORY}/pull/104\n",
        }
    )
    store = InMemoryWorkspaceStore()
    workspace = OptimizationWorkspace(
        store=store,
        pull_requests=GhWorkspacePullRequests(
            commands,
            repository=_REPOSITORY,
            base_branch=_BASE_BRANCH,
        ),
    )

    result = workspace.advance(
        WorkspaceRequest(
            repository_root=tmp_path,
            issue=WorkspaceIssue(
                number=_ISSUE_NUMBER,
                title="[Optimize] Improve policy coverage",
                body="Improve policy coverage without weakening safety.",
                base_commit=_BASE_COMMIT,
            ),
            trigger=WorkspaceTrigger.ISSUE_CREATED,
        )
    )

    snapshot = store.load(_ISSUE_NUMBER)
    assert result.workspace_pull_request is not None
    assert result.workspace_pull_request.number == 104
    assert snapshot is not None
    assert snapshot.workspace_pull_request_number == 104
    assert commands.calls[-1]["input_text"] == (
        f"{marker}\n\n"
        f"{workspace_pull_request_base_marker(_BASE_COMMIT)}\n\n"
        "Persistent optimization workspace for issue #31.\n"
    )
    assert sum(
        invocation[:3] == ("gh", "pr", "create")
        for invocation in commands.invocations
    ) == 1
    assert not any(
        invocation[:2] == ("gh", "issue")
        for invocation in commands.invocations
    )


def test_continuation_updates_and_reuses_the_same_workspace_pr(
    tmp_path: Path,
) -> None:
    marker = workspace_pull_request_marker(_ISSUE_NUMBER)
    existing = json.dumps(
        [
            {
                "number": 104,
                "headRefName": _BRANCH,
                "baseRefName": _BASE_BRANCH,
                "title": "Outdated workspace title",
                "body": f"{marker}\n\nExisting public evidence.\n",
                "isDraft": True,
                "state": "OPEN",
            }
        ]
    )
    list_by_branch = (
        "gh",
        "pr",
        "list",
        "--repo",
        _REPOSITORY,
        "--state",
        "all",
        "--head",
        _BRANCH,
        "--json",
        "number,headRefName,baseRefName,title,body,isDraft,state",
        "--limit",
        "2",
    )
    list_by_marker = (
        "gh",
        "pr",
        "list",
        "--repo",
        _REPOSITORY,
        "--state",
        "all",
        "--search",
        '"foundry-opt:workspace-pr:issue-31:v1" in:body',
        "--json",
        "number,headRefName,baseRefName,title,body,isDraft,state",
        "--limit",
        "2",
    )
    update_title = (
        "gh",
        "pr",
        "edit",
        "104",
        "--repo",
        _REPOSITORY,
        "--title",
        "[Optimize] #31 workspace - draft, not yet selectable",
    )
    commands = FakeCommands(
        {
            list_by_branch: existing,
            list_by_marker: existing,
            update_title: "",
        }
    )
    store = InMemoryWorkspaceStore()
    store.commit(
        expected_revision=None,
        update=WorkspaceUpdate(
            issue_number=_ISSUE_NUMBER,
            phase=WorkspacePhase.SPECIFICATION,
            workspace_pull_request_number=104,
            semantic_event="issue_created",
        ),
    )
    workspace = OptimizationWorkspace(
        store=store,
        pull_requests=GhWorkspacePullRequests(
            commands,
            repository=_REPOSITORY,
            base_branch=_BASE_BRANCH,
        ),
    )

    result = workspace.advance(
        WorkspaceRequest(
            repository_root=tmp_path,
            issue=WorkspaceIssue(
                number=_ISSUE_NUMBER,
                title="[Optimize] Improve policy coverage",
                body="Improve policy coverage without weakening safety.",
                base_commit=_BASE_COMMIT,
            ),
            trigger=WorkspaceTrigger.CONTINUE,
        )
    )

    assert result.workspace_pull_request is not None
    assert result.workspace_pull_request.number == 104
    assert commands.invocations[-1] == update_title
    assert not any(
        invocation[:3] == ("gh", "pr", "create")
        or invocation[:2] == ("gh", "issue")
        or invocation[:2] == ("git", "push")
        for invocation in commands.invocations
    )


def test_multiple_workspace_pull_requests_fail_closed(
    tmp_path: Path,
) -> None:
    marker = workspace_pull_request_marker(_ISSUE_NUMBER)
    matches = json.dumps(
        [
            {
                "number": number,
                "headRefName": _BRANCH,
                "baseRefName": _BASE_BRANCH,
                "title": (
                    "[Optimize] #31 workspace - "
                    "draft, not yet selectable"
                ),
                "body": marker,
                "isDraft": True,
                "state": "OPEN",
            }
            for number in (104, 105)
        ]
    )
    commands = FakeCommands(
        {
            (
                "gh",
                "pr",
                "list",
                "--repo",
                _REPOSITORY,
                "--state",
                "all",
                "--head",
                _BRANCH,
                "--json",
                "number,headRefName,baseRefName,title,body,isDraft,state",
                "--limit",
                "2",
            ): matches,
            (
                "gh",
                "pr",
                "list",
                "--repo",
                _REPOSITORY,
                "--state",
                "all",
                "--search",
                '"foundry-opt:workspace-pr:issue-31:v1" in:body',
                "--json",
                "number,headRefName,baseRefName,title,body,isDraft,state",
                "--limit",
                "2",
            ): "[]",
        }
    )
    workspace = OptimizationWorkspace(
        store=InMemoryWorkspaceStore(),
        pull_requests=GhWorkspacePullRequests(
            commands,
            repository=_REPOSITORY,
            base_branch=_BASE_BRANCH,
        ),
    )

    with pytest.raises(
        GitHubWorkspacePullRequestError,
        match="multiple workspace pull requests",
    ):
        workspace.advance(
            WorkspaceRequest(
                repository_root=tmp_path,
                issue=WorkspaceIssue(
                    number=_ISSUE_NUMBER,
                    title="[Optimize] Improve policy coverage",
                    body=(
                        "Improve policy coverage without weakening safety."
                    ),
                    base_commit=_BASE_COMMIT,
                ),
                trigger=WorkspaceTrigger.ISSUE_CREATED,
            )
        )

    assert len(commands.invocations) == 2


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("number", 105),
        ("headRefName", "foundry-opt/workspace/issue-32"),
        ("baseRefName", "release"),
        ("body", "<!-- foundry-opt:workspace-pr:issue-32:v1 -->"),
        ("isDraft", False),
        ("state", "CLOSED"),
        ("state", "MERGED"),
    ),
)
def test_mismatched_workspace_pull_requests_fail_closed(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    pull_request = {
        "number": 104,
        "headRefName": _BRANCH,
        "baseRefName": _BASE_BRANCH,
        "title": (
            "[Optimize] #31 workspace - draft, not yet selectable"
        ),
        "body": workspace_pull_request_marker(_ISSUE_NUMBER),
        "isDraft": True,
        "state": "OPEN",
    }
    pull_request[field] = value
    response = json.dumps([pull_request])
    commands = FakeCommands(
        {
            (
                "gh",
                "pr",
                "list",
                "--repo",
                _REPOSITORY,
                "--state",
                "all",
                "--head",
                _BRANCH,
                "--json",
                "number,headRefName,baseRefName,title,body,isDraft,state",
                "--limit",
                "2",
            ): response,
            (
                "gh",
                "pr",
                "list",
                "--repo",
                _REPOSITORY,
                "--state",
                "all",
                "--search",
                '"foundry-opt:workspace-pr:issue-31:v1" in:body',
                "--json",
                "number,headRefName,baseRefName,title,body,isDraft,state",
                "--limit",
                "2",
            ): response,
        }
    )
    store = InMemoryWorkspaceStore()
    store.commit(
        expected_revision=None,
        update=WorkspaceUpdate(
            issue_number=_ISSUE_NUMBER,
            phase=WorkspacePhase.SPECIFICATION,
            workspace_pull_request_number=104,
            semantic_event="issue_created",
        ),
    )
    workspace = OptimizationWorkspace(
        store=store,
        pull_requests=GhWorkspacePullRequests(
            commands,
            repository=_REPOSITORY,
            base_branch=_BASE_BRANCH,
        ),
    )

    with pytest.raises(
        GitHubWorkspacePullRequestError,
        match="does not match",
    ):
        workspace.advance(
            WorkspaceRequest(
                repository_root=tmp_path,
                issue=WorkspaceIssue(
                    number=_ISSUE_NUMBER,
                    title="[Optimize] Improve policy coverage",
                    body=(
                        "Improve policy coverage without weakening safety."
                    ),
                    base_commit=_BASE_COMMIT,
                ),
                trigger=WorkspaceTrigger.CONTINUE,
            )
        )

    assert len(commands.invocations) == 2


def test_repeated_events_reuse_the_discovered_workspace_pr(
    tmp_path: Path,
) -> None:
    existing = json.dumps(
        [
            {
                "number": 104,
                "headRefName": _BRANCH,
                "baseRefName": _BASE_BRANCH,
                "title": (
                    "[Optimize] #31 workspace - "
                    "draft, not yet selectable"
                ),
                "body": workspace_pull_request_marker(_ISSUE_NUMBER),
                "isDraft": True,
                "state": "OPEN",
            }
        ]
    )
    commands = FakeCommands(
        {
            (
                "gh",
                "pr",
                "list",
                "--repo",
                _REPOSITORY,
                "--state",
                "all",
                "--head",
                _BRANCH,
                "--json",
                "number,headRefName,baseRefName,title,body,isDraft,state",
                "--limit",
                "2",
            ): existing,
            (
                "gh",
                "pr",
                "list",
                "--repo",
                _REPOSITORY,
                "--state",
                "all",
                "--search",
                '"foundry-opt:workspace-pr:issue-31:v1" in:body',
                "--json",
                "number,headRefName,baseRefName,title,body,isDraft,state",
                "--limit",
                "2",
            ): existing,
        }
    )
    store = InMemoryWorkspaceStore()
    workspace = OptimizationWorkspace(
        store=store,
        pull_requests=GhWorkspacePullRequests(
            commands,
            repository=_REPOSITORY,
            base_branch=_BASE_BRANCH,
        ),
    )
    issue = WorkspaceIssue(
        number=_ISSUE_NUMBER,
        title="[Optimize] Improve policy coverage",
        body="Improve policy coverage without weakening safety.",
        base_commit=_BASE_COMMIT,
    )

    first = workspace.advance(
        WorkspaceRequest(
            repository_root=tmp_path,
            issue=issue,
            trigger=WorkspaceTrigger.ISSUE_CREATED,
        )
    )
    repeated = workspace.advance(
        WorkspaceRequest(
            repository_root=tmp_path,
            issue=issue,
            trigger=WorkspaceTrigger.CONTINUE,
        )
    )

    snapshot = store.load(_ISSUE_NUMBER)
    assert first.workspace_pull_request is not None
    assert repeated.workspace_pull_request is not None
    assert first.workspace_pull_request.number == 104
    assert repeated.workspace_pull_request.number == 104
    assert snapshot is not None
    assert snapshot.revision == "1"
    assert len(commands.invocations) == 4


def test_continuation_does_not_replace_a_missing_committed_pr(
    tmp_path: Path,
) -> None:
    commands = FakeCommands(
        {
            (
                "gh",
                "pr",
                "list",
                "--repo",
                _REPOSITORY,
                "--state",
                "all",
                "--head",
                _BRANCH,
                "--json",
                "number,headRefName,baseRefName,title,body,isDraft,state",
                "--limit",
                "2",
            ): "[]",
            (
                "gh",
                "pr",
                "list",
                "--repo",
                _REPOSITORY,
                "--state",
                "all",
                "--search",
                '"foundry-opt:workspace-pr:issue-31:v1" in:body',
                "--json",
                "number,headRefName,baseRefName,title,body,isDraft,state",
                "--limit",
                "2",
            ): "[]",
        }
    )
    store = InMemoryWorkspaceStore()
    store.commit(
        expected_revision=None,
        update=WorkspaceUpdate(
            issue_number=_ISSUE_NUMBER,
            phase=WorkspacePhase.SPECIFICATION,
            workspace_pull_request_number=104,
            semantic_event="issue_created",
        ),
    )
    workspace = OptimizationWorkspace(
        store=store,
        pull_requests=GhWorkspacePullRequests(
            commands,
            repository=_REPOSITORY,
            base_branch=_BASE_BRANCH,
        ),
    )

    with pytest.raises(
        GitHubWorkspacePullRequestError,
        match="was not found",
    ):
        workspace.advance(
            WorkspaceRequest(
                repository_root=tmp_path,
                issue=WorkspaceIssue(
                    number=_ISSUE_NUMBER,
                    title="[Optimize] Improve policy coverage",
                    body=(
                        "Improve policy coverage without weakening safety."
                    ),
                    base_commit=_BASE_COMMIT,
                ),
                trigger=WorkspaceTrigger.CONTINUE,
            )
        )

    assert len(commands.invocations) == 2


def test_issue_creation_rejects_a_mismatched_push_remote(
    tmp_path: Path,
) -> None:
    commands = FakeCommands(
        {
            (
                "gh",
                "pr",
                "list",
                "--repo",
                _REPOSITORY,
                "--state",
                "all",
                "--head",
                _BRANCH,
                "--json",
                "number,headRefName,baseRefName,title,body,isDraft,state",
                "--limit",
                "2",
            ): "[]",
            (
                "gh",
                "pr",
                "list",
                "--repo",
                _REPOSITORY,
                "--state",
                "all",
                "--search",
                '"foundry-opt:workspace-pr:issue-31:v1" in:body',
                "--json",
                "number,headRefName,baseRefName,title,body,isDraft,state",
                "--limit",
                "2",
            ): "[]",
            (
                "git",
                "config",
                "--get-all",
                "remote.origin.url",
            ): f"https://github.com/{_REPOSITORY}.git\n",
            (
                "git",
                "remote",
                "get-url",
                "--push",
                "--all",
                "origin",
            ): "https://github.com/other-org/other-repo.git\n",
        }
    )
    workspace = OptimizationWorkspace(
        store=InMemoryWorkspaceStore(),
        pull_requests=GhWorkspacePullRequests(
            commands,
            repository=_REPOSITORY,
            base_branch=_BASE_BRANCH,
        ),
    )

    with pytest.raises(
        GitHubWorkspacePullRequestError,
        match="push remote",
    ):
        workspace.advance(
            WorkspaceRequest(
                repository_root=tmp_path,
                issue=WorkspaceIssue(
                    number=_ISSUE_NUMBER,
                    title="[Optimize] Improve policy coverage",
                    body=(
                        "Improve policy coverage without weakening safety."
                    ),
                    base_commit=_BASE_COMMIT,
                ),
                trigger=WorkspaceTrigger.ISSUE_CREATED,
            )
        )

    assert commands.invocations[-1][:4] == (
        "git",
        "remote",
        "get-url",
        "--push",
    )


def test_issue_creation_reuses_a_concurrently_created_workspace_pr(
    tmp_path: Path,
) -> None:
    existing = json.dumps(
        [
            {
                "number": 104,
                "headRefName": _BRANCH,
                "baseRefName": _BASE_BRANCH,
                "title": (
                    "[Optimize] #31 workspace - "
                    "draft, not yet selectable"
                ),
                "body": workspace_pull_request_marker(_ISSUE_NUMBER),
                "isDraft": True,
                "state": "OPEN",
            }
        ]
    )
    list_by_branch = (
        "gh",
        "pr",
        "list",
        "--repo",
        _REPOSITORY,
        "--state",
        "all",
        "--head",
        _BRANCH,
        "--json",
        "number,headRefName,baseRefName,title,body,isDraft,state",
        "--limit",
        "2",
    )
    list_by_marker = (
        "gh",
        "pr",
        "list",
        "--repo",
        _REPOSITORY,
        "--state",
        "all",
        "--search",
        '"foundry-opt:workspace-pr:issue-31:v1" in:body',
        "--json",
        "number,headRefName,baseRefName,title,body,isDraft,state",
        "--limit",
        "2",
    )
    remote_url = f"https://github.com/{_REPOSITORY}.git\n"
    commands = FakeCommands(
        {
            list_by_branch: ["[]", existing],
            list_by_marker: ["[]", existing],
            (
                "git",
                "config",
                "--get-all",
                "remote.origin.url",
            ): remote_url,
            (
                "git",
                "remote",
                "get-url",
                "--push",
                "--all",
                "origin",
            ): remote_url,
            (
                "git",
                "ls-remote",
                "--heads",
                "origin",
                f"refs/heads/{_BRANCH}",
            ): (
                f"{_BASE_COMMIT}\t"
                f"refs/heads/{_BRANCH}\n"
            ),
        }
    )
    workspace = OptimizationWorkspace(
        store=InMemoryWorkspaceStore(),
        pull_requests=GhWorkspacePullRequests(
            commands,
            repository=_REPOSITORY,
            base_branch=_BASE_BRANCH,
        ),
    )

    result = workspace.advance(
        WorkspaceRequest(
            repository_root=tmp_path,
            issue=WorkspaceIssue(
                number=_ISSUE_NUMBER,
                title="[Optimize] Improve policy coverage",
                body="Improve policy coverage without weakening safety.",
                base_commit=_BASE_COMMIT,
            ),
            trigger=WorkspaceTrigger.ISSUE_CREATED,
        )
    )

    assert result.workspace_pull_request is not None
    assert result.workspace_pull_request.number == 104
    assert commands.invocations.count(list_by_branch) == 2
    assert commands.invocations.count(list_by_marker) == 2
    assert not any(
        invocation[:3] == ("gh", "pr", "create")
        for invocation in commands.invocations
    )


def test_issue_creation_reuses_a_matching_remote_branch_commit(
    tmp_path: Path,
) -> None:
    list_by_branch = (
        "gh",
        "pr",
        "list",
        "--repo",
        _REPOSITORY,
        "--state",
        "all",
        "--head",
        _BRANCH,
        "--json",
        "number,headRefName,baseRefName,title,body,isDraft,state",
        "--limit",
        "2",
    )
    list_by_marker = (
        "gh",
        "pr",
        "list",
        "--repo",
        _REPOSITORY,
        "--state",
        "all",
        "--search",
        '"foundry-opt:workspace-pr:issue-31:v1" in:body',
        "--json",
        "number,headRefName,baseRefName,title,body,isDraft,state",
        "--limit",
        "2",
    )
    branch_ref = f"refs/heads/{_BRANCH}"
    remote_url = f"https://github.com/{_REPOSITORY}.git\n"
    commands = FakeCommands(
        {
            list_by_branch: "[]",
            list_by_marker: "[]",
            (
                "git",
                "config",
                "--get-all",
                "remote.origin.url",
            ): remote_url,
            (
                "git",
                "remote",
                "get-url",
                "--push",
                "--all",
                "origin",
            ): remote_url,
            (
                "git",
                "ls-remote",
                "--heads",
                "origin",
                branch_ref,
            ): f"{_BASE_COMMIT}\t{branch_ref}\n",
            (
                "gh",
                "pr",
                "create",
                "--repo",
                _REPOSITORY,
                "--draft",
                "--base",
                _BASE_BRANCH,
                "--head",
                _BRANCH,
                "--title",
                "[Optimize] #31 workspace - draft, not yet selectable",
                "--body-file",
                "-",
            ): f"https://github.com/{_REPOSITORY}/pull/104\n",
        }
    )
    workspace = OptimizationWorkspace(
        store=InMemoryWorkspaceStore(),
        pull_requests=GhWorkspacePullRequests(
            commands,
            repository=_REPOSITORY,
            base_branch=_BASE_BRANCH,
        ),
    )

    result = workspace.advance(
        WorkspaceRequest(
            repository_root=tmp_path,
            issue=WorkspaceIssue(
                number=_ISSUE_NUMBER,
                title="[Optimize] Improve policy coverage",
                body="Improve policy coverage without weakening safety.",
                base_commit=_BASE_COMMIT,
            ),
            trigger=WorkspaceTrigger.ISSUE_CREATED,
        )
    )

    assert result.workspace_pull_request is not None
    assert result.workspace_pull_request.number == 104
    assert commands.invocations.count(list_by_branch) == 2
    assert not any(
        invocation[:2] == ("git", "push")
        for invocation in commands.invocations
    )


def test_issue_creation_rejects_a_mismatched_remote_branch_commit(
    tmp_path: Path,
) -> None:
    branch_ref = f"refs/heads/{_BRANCH}"
    remote_url = f"https://github.com/{_REPOSITORY}.git\n"
    commands = FakeCommands(
        {
            (
                "gh",
                "pr",
                "list",
                "--repo",
                _REPOSITORY,
                "--state",
                "all",
                "--head",
                _BRANCH,
                "--json",
                "number,headRefName,baseRefName,title,body,isDraft,state",
                "--limit",
                "2",
            ): "[]",
            (
                "gh",
                "pr",
                "list",
                "--repo",
                _REPOSITORY,
                "--state",
                "all",
                "--search",
                '"foundry-opt:workspace-pr:issue-31:v1" in:body',
                "--json",
                "number,headRefName,baseRefName,title,body,isDraft,state",
                "--limit",
                "2",
            ): "[]",
            (
                "git",
                "config",
                "--get-all",
                "remote.origin.url",
            ): remote_url,
            (
                "git",
                "remote",
                "get-url",
                "--push",
                "--all",
                "origin",
            ): remote_url,
            (
                "git",
                "ls-remote",
                "--heads",
                "origin",
                branch_ref,
            ): f"{'b' * 40}\t{branch_ref}\n",
            (
                "git",
                "rev-parse",
                f"{_BASE_COMMIT}^{{tree}}",
            ): f"{'c' * 40}\n",
            (
                "git",
                "commit-tree",
                "c" * 40,
                "-p",
                _BASE_COMMIT,
                "-m",
                "Create persistent optimization workspace for issue-31",
            ): f"{'d' * 40}\n",
        }
    )
    workspace = OptimizationWorkspace(
        store=InMemoryWorkspaceStore(),
        pull_requests=GhWorkspacePullRequests(
            commands,
            repository=_REPOSITORY,
            base_branch=_BASE_BRANCH,
        ),
    )

    with pytest.raises(
        GitHubWorkspacePullRequestError,
        match="branch does not match base commit",
    ):
        workspace.advance(
            WorkspaceRequest(
                repository_root=tmp_path,
                issue=WorkspaceIssue(
                    number=_ISSUE_NUMBER,
                    title="[Optimize] Improve policy coverage",
                    body=(
                        "Improve policy coverage without weakening safety."
                    ),
                    base_commit=_BASE_COMMIT,
                ),
                trigger=WorkspaceTrigger.ISSUE_CREATED,
            )
        )

    assert not any(
        invocation[:2] == ("git", "push")
        or invocation[:3] == ("gh", "pr", "create")
        for invocation in commands.invocations
    )
