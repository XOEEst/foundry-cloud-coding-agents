from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from pathlib import Path
import subprocess
from typing import Any

from foundry_opt.orchestration import (
    GitWorkspaceStore,
    OptimizationWorkspace,
    WorkspaceAdvanceRequest,
    WorkspacePhase,
    WorkspaceTrigger,
    build_production_workspace,
)
from foundry_opt.orchestration.workspace_github import GhWorkspacePullRequests
from foundry_opt.orchestration.workspace_production import (
    ProductionWorkspaceService,
)
from foundry_opt.preflight.interfaces import CommandResult


class FakeCommands:
    def __init__(self, responses: dict[tuple[str, ...], str]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

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
        self.calls.append(command)
        if command not in self.responses:
            raise AssertionError(f"unexpected command: {command}")
        return CommandResult(0, self.responses[command], "")


def test_production_builder_uses_git_state_and_gh_pull_requests(
    tmp_path: Path,
) -> None:
    subprocess.run(
        ("git", "init", "-b", "main"),
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    workspace = build_production_workspace(
        tmp_path,
        repository="octo-org/optimizer",
        base_branch="main",
        commands=FakeCommands({}),
    )

    assert isinstance(workspace, OptimizationWorkspace)
    assert isinstance(workspace._store, GitWorkspaceStore)
    assert isinstance(workspace._pull_requests, GhWorkspacePullRequests)


def test_production_service_loads_issue_and_reuses_recorded_pr(
    tmp_path: Path,
) -> None:
    body = "\n".join(
        (
            "<!-- foundry-opt:workspace-pr:issue-31:v1 -->",
            f"<!-- foundry-opt:workspace-base:{'a' * 40} -->",
        )
    )
    commands = FakeCommands(
        {
            ("git", "remote", "get-url", "origin"): (
                "https://github.com/octo-org/optimizer.git\n"
            ),
            (
                "gh",
                "repo",
                "view",
                "octo-org/optimizer",
                "--json",
                "nameWithOwner,defaultBranchRef",
            ): json.dumps(
                {
                    "nameWithOwner": "octo-org/optimizer",
                    "defaultBranchRef": {"name": "main"},
                }
            ),
            (
                "gh",
                "issue",
                "view",
                "31",
                "--repo",
                "octo-org/optimizer",
                "--json",
                "number,title,body,state",
            ): json.dumps(
                {
                    "number": 31,
                    "title": "[Optimize] Improve policy coverage",
                    "body": "Improve policy coverage without weakening safety.",
                    "state": "OPEN",
                }
            ),
            (
                "gh",
                "pr",
                "list",
                "--repo",
                "octo-org/optimizer",
                "--state",
                "all",
                "--search",
                '"foundry-opt:workspace-pr:issue-31:v1" in:body',
                "--json",
                "number,body",
                "--limit",
                "2",
            ): json.dumps([{"number": 104, "body": body}]),
        }
    )

    class RecordingWorkspace:
        def __init__(self) -> None:
            self.request: Any = None

        def advance(self, request):
            self.request = request
            from foundry_opt.orchestration import WorkspaceResult

            return WorkspaceResult(
                phase=WorkspacePhase.SPECIFICATION,
                workspace_pull_request=request.workspace_pull_request,
                planned_effect_kinds=("workspace_pr_sync",),
                recorded=False,
            )

    recording = RecordingWorkspace()
    service = ProductionWorkspaceService(
        commands=commands,
        workspace_factory=lambda **_: recording,
    )

    service.advance(
        WorkspaceAdvanceRequest(
            repository_root=tmp_path,
            issue_number=31,
            trigger=WorkspaceTrigger.CONTINUE,
        )
    )

    assert recording.request.issue.base_commit == "a" * 40
    assert recording.request.issue.number == 31
