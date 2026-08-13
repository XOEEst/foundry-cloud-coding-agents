from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

import foundry_opt.orchestration.workspace_production as workspace_production
from foundry_opt.orchestration import (
    GitWorkspaceStore,
    OptimizationWorkspace,
    TrustedWorkspaceEventContext,
    WorkspaceAdvanceRequest,
    WorkspacePhase,
    WorkspaceTrigger,
    build_production_workspace,
)
from foundry_opt.orchestration.workspace_github import GhWorkspacePullRequests
from foundry_opt.orchestration.workspace_production import (
    ProductionWorkspaceError,
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


def test_production_builder_wires_candidate_coordinator(
    tmp_path: Path,
) -> None:
    subprocess.run(
        ("git", "init", "-b", "main"),
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    class Runner:
        def evaluate(self, request):
            raise AssertionError("not evaluated while building")

    class Selector:
        def select(self, request):
            raise AssertionError("not selected while building")

    workspace = build_production_workspace(
        tmp_path,
        repository="octo-org/optimizer",
        base_branch="main",
        commands=FakeCommands({}),
        candidate_count=2,
        experiment_runner=Runner(),
        selector=Selector(),
    )

    assert workspace._candidate_coordinator is not None


def test_production_service_wires_trusted_workspace_verifier(
    monkeypatch,
    tmp_path: Path,
) -> None:
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
        }
    )
    recorded = {}

    class Verifier:
        def __init__(self, **kwargs):
            recorded.update(kwargs)

        def verify(
            self,
            root,
            *,
            issue_number,
            pull_request_number,
        ):
            recorded["root"] = root
            recorded["issue"] = issue_number
            recorded["pr"] = pull_request_number
            return "verified"

    monkeypatch.setattr(workspace_production, "WorkspaceVerifier", Verifier)
    monkeypatch.setattr(
        workspace_production,
        "GitWorkspaceStore",
        lambda root: f"store:{root}",
    )

    result = ProductionWorkspaceService(commands=commands).verify(
        repository_root=tmp_path,
        issue_number=31,
        pull_request_number=104,
    )

    assert result == "verified"
    assert recorded["repository"] == "octo-org/optimizer"
    assert recorded["base_branch"] == "main"
    assert recorded["issue"] == 31
    assert recorded["pr"] == 104


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
                "--head",
                "foundry-opt/workspace/issue-31",
                "--json",
                "number,body",
                "--limit",
                "2",
            ): json.dumps([{"number": 104, "body": body}]),
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


@pytest.mark.parametrize(
    "trigger",
    (
        WorkspaceTrigger.PULL_REQUEST_MERGED,
        WorkspaceTrigger.DEPLOYMENT_COMPLETED,
        WorkspaceTrigger.RETENTION_COMPLETED,
    ),
)
def test_production_service_rejects_direct_trusted_lifecycle_triggers(
    tmp_path: Path,
    trigger: WorkspaceTrigger,
) -> None:
    service = ProductionWorkspaceService(
        commands=FakeCommands({}),
        workspace_factory=lambda **_: pytest.fail(
            "unsafe lifecycle trigger reached workspace"
        ),
    )

    with pytest.raises(ProductionWorkspaceError, match="trusted"):
        service.advance(
            WorkspaceAdvanceRequest(
                repository_root=tmp_path,
                issue_number=31,
                trigger=trigger,
            )
        )


def test_production_service_finds_workspace_pr_during_search_lag(
    tmp_path: Path,
) -> None:
    body = "\n".join(
        (
            "<!-- foundry-opt:workspace-pr:issue-31:v1 -->",
            f"<!-- foundry-opt:workspace-base:{'a' * 40} -->",
        )
    )
    repository_responses = {
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
                "body": "Improve policy coverage.",
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
            "--head",
            "foundry-opt/workspace/issue-31",
            "--json",
            "number,body",
            "--limit",
            "2",
        ): json.dumps([{"number": 104, "body": body}]),
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
        ): "[]",
    }
    recording: dict[str, Any] = {}

    class Workspace:
        def advance(self, request):
            recording["request"] = request
            from foundry_opt.orchestration import WorkspaceResult

            return WorkspaceResult(
                phase=WorkspacePhase.SPECIFICATION,
                workspace_pull_request=request.workspace_pull_request,
                planned_effect_kinds=("workspace_pr_sync",),
            )

    ProductionWorkspaceService(
        commands=FakeCommands(repository_responses),
        workspace_factory=lambda **_: Workspace(),
    ).advance(
        WorkspaceAdvanceRequest(
            repository_root=tmp_path,
            issue_number=31,
        )
    )

    assert recording["request"].workspace_pull_request.number == 104
    assert recording["request"].issue.base_commit == "a" * 40


def test_workspace_intake_rejects_trusted_repository_id_mismatch(
    tmp_path: Path,
) -> None:
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
                "api",
                "repos/octo-org/optimizer",
                "--jq",
                ".id",
            ): "999\n",
        }
    )
    service = ProductionWorkspaceService(
        commands=commands,
        workspace_factory=lambda **_: pytest.fail(
            "workspace must not be built after repository ID mismatch"
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="repository ID does not match",
    ):
        service.ingest(
            {
                "action": "opened",
                "issue": {
                    "number": 31,
                    "title": "[Optimize] Improve policy coverage",
                    "body": "Improve policy coverage.",
                },
                "repository": {
                    "full_name": "octo-org/optimizer",
                    "id": 123,
                },
            },
            TrustedWorkspaceEventContext(
                event_name="issues",
                delivery_id="delivery-123",
                repository="octo-org/optimizer",
                repository_id=123,
            ),
            base_commit="a" * 40,
            repository_root=tmp_path,
        )


def test_existing_workspace_pr_discovery_rejects_inconsistent_results(
    tmp_path: Path,
) -> None:
    marker = "<!-- foundry-opt:workspace-pr:issue-31:v1 -->"
    commands = FakeCommands(
        {
            (
                "gh",
                "pr",
                "list",
                "--repo",
                "octo-org/optimizer",
                "--state",
                "all",
                "--head",
                "foundry-opt/workspace/issue-31",
                "--json",
                "number,body",
                "--limit",
                "2",
            ): json.dumps(
                [
                    {
                        "number": 104,
                        "body": (
                            f"{marker}\n"
                            f"<!-- foundry-opt:workspace-base:{'a' * 40} -->"
                        ),
                    }
                ]
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
            ): json.dumps(
                [
                    {
                        "number": 104,
                        "body": (
                            f"{marker}\n"
                            f"<!-- foundry-opt:workspace-base:{'b' * 40} -->"
                        ),
                    }
                ]
            ),
        }
    )

    with pytest.raises(RuntimeError, match="inconsistent"):
        ProductionWorkspaceService(
            commands=commands
        )._existing_workspace_pull_request(
            tmp_path,
            "octo-org/optimizer",
            31,
        )
