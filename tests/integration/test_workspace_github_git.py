from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from foundry_opt.adapters.commands import SubprocessCommandRunner
from foundry_opt.orchestration import (
    InMemoryWorkspaceStore,
    OptimizationWorkspace,
    WorkspaceIssue,
    WorkspaceRequest,
    WorkspaceTrigger,
)
from foundry_opt.orchestration.workspace_github import (
    GhWorkspacePullRequests,
    workspace_pull_request_marker,
)
from foundry_opt.preflight.interfaces import CommandResult


class GitWithFakeGitHub:
    def __init__(self) -> None:
        self._commands = SubprocessCommandRunner()
        self.github_calls: list[tuple[tuple[str, ...], str | None]] = []

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
        if command[0] == "git":
            return self._commands.run(
                command,
                cwd=cwd,
                environment=environment,
                input_text=input_text,
                input_bytes=input_bytes,
            )
        self.github_calls.append((command, input_text))
        if command[:3] == ("gh", "pr", "list"):
            return CommandResult(0, "[]", "")
        if command[:3] == ("gh", "pr", "create"):
            return CommandResult(
                0,
                "https://github.com/octo-org/optimizer/pull/104\n",
                "",
            )
        raise AssertionError(f"unexpected command: {command}")


def test_workspace_creation_publishes_only_the_reserved_branch(
    tmp_path: Path,
) -> None:
    commands = SubprocessCommandRunner()
    origin = tmp_path / "origin.git"
    repository = tmp_path / "repository"
    commands.run(("git", "init", "--bare", str(origin)))
    commands.run(("git", "init", "-b", "main", str(repository)))
    commands.run(
        ("git", "config", "user.name", "Workspace Test"),
        cwd=repository,
    )
    commands.run(
        ("git", "config", "user.email", "workspace@example.invalid"),
        cwd=repository,
    )
    (repository / "README.md").write_text(
        "workspace integration\n",
        encoding="utf-8",
    )
    commands.run(("git", "add", "README.md"), cwd=repository)
    commands.run(("git", "commit", "-m", "base"), cwd=repository)
    base_commit = commands.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
    ).stdout.strip()
    commands.run(
        ("git", "remote", "add", "origin", str(origin)),
        cwd=repository,
    )
    transport = GitWithFakeGitHub()
    workspace = OptimizationWorkspace(
        store=InMemoryWorkspaceStore(),
        pull_requests=GhWorkspacePullRequests(
            transport,
            repository="octo-org/optimizer",
            base_branch="main",
        ),
    )

    result = workspace.advance(
        WorkspaceRequest(
            repository_root=repository,
            issue=WorkspaceIssue(
                number=31,
                title="[Optimize] Improve policy coverage",
                body="Improve policy coverage without weakening safety.",
                base_commit=base_commit,
            ),
            trigger=WorkspaceTrigger.ISSUE_CREATED,
        )
    )

    remote_commit = commands.run(
        (
            "git",
            "--git-dir",
            str(origin),
            "rev-parse",
            "refs/heads/foundry-opt/workspace/issue-31",
        )
    ).stdout.strip()
    remote_refs = commands.run(
        (
            "git",
            "--git-dir",
            str(origin),
            "for-each-ref",
            "--format=%(refname)",
            "refs/heads",
        )
    ).stdout.splitlines()
    assert result.workspace_pull_request is not None
    assert result.workspace_pull_request.number == 104
    assert remote_commit == base_commit
    assert remote_refs == [
        "refs/heads/foundry-opt/workspace/issue-31"
    ]
    create_call = transport.github_calls[-1]
    assert create_call[0][:3] == ("gh", "pr", "create")
    assert workspace_pull_request_marker(31) in (create_call[1] or "")
