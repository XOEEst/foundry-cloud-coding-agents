from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path

import pytest

from foundry_opt.orchestration.workspace_assignment import (
    GhWorkspaceAssignmentCleaner,
    GhWorkspaceCopilotAssigner,
)
from foundry_opt.preflight.interfaces import CommandResult


class Commands:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[
            tuple[
                tuple[str, ...],
                Mapping[str, str] | None,
                str | None,
            ]
        ] = []

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
        environment: Mapping[str, str] | None = None,
        input_text: str | None = None,
        input_bytes: bytes | None = None,
    ) -> CommandResult:
        del cwd, input_bytes
        self.calls.append((tuple(arguments), environment, input_text))
        return CommandResult(0, self.responses.pop(0), "")


def test_workspace_assignment_targets_existing_pull_request_only(
    tmp_path: Path,
) -> None:
    assignment_key = "revision-1"
    marker_hash = hashlib.sha256(
        assignment_key.encode("utf-8")
    ).hexdigest()[:16]
    commands = Commands(
        [
            json.dumps(
                {
                    "number": 104,
                    "state": "open",
                    "pull_request": {"url": "https://example.invalid/pr/104"},
                    "assignees": [],
                }
            ),
            json.dumps(
                {"id": 123, "login": "octocat", "type": "User"}
            ),
            json.dumps(
                [[
                    {
                        "body": (
                            "<!-- foundry-opt:"
                            "workspace-copilot-assignment:"
                            "issue-31:old-revision:v1 -->"
                        ),
                        "user": {
                            "id": 123,
                            "login": "octocat",
                            "type": "User",
                        },
                    },
                    {
                        "body": (
                            "> <!-- foundry-opt:"
                            "workspace-copilot-assignment:"
                            f"issue-31:{marker_hash}:v1 -->\n"
                            "Copilot quoted the assignment while reporting."
                        ),
                        "user": {
                            "id": 999,
                            "login": "copilot",
                            "type": "Bot",
                        },
                    },
                ]]
            ),
            json.dumps(
                {
                    "body": (
                        "<!-- foundry-opt:workspace-copilot-assignment:"
                        f"issue-31:{marker_hash}:v1 -->\n"
                        "@copilot Continue this existing workspace pull "
                        "request #104 for optimization issue #31. Read and "
                        "follow `.github/agents/"
                        "foundry-optimization-steward.agent.md`. Run "
                        "`foundry-opt workspace advance --issue 31 "
                        "--json`, perform only returned candidate-work "
                        "next actions, and do not create another issue or "
                        "pull request."
                    ),
                    "user": {
                        "id": 123,
                        "login": "octocat",
                        "type": "User",
                    },
                }
            ),
        ]
    )

    assigned = GhWorkspaceCopilotAssigner(
        commands,
        repository_root=tmp_path,
        repository="octo-org/optimizer",
        assignment_token="assignment-token",
    ).assign(
        issue_number=31,
        pull_request_number=104,
        assignment_key=assignment_key,
    )

    assert assigned is True
    assert len(commands.calls) == 4
    assert commands.calls[0][0] == (
        "gh",
        "api",
        "repos/octo-org/optimizer/issues/104",
    )
    assert commands.calls[1][0] == (
        "gh",
        "api",
        "user",
    )
    assert commands.calls[2][0] == (
        "gh",
        "api",
        "--paginate",
        "--slurp",
        "repos/octo-org/optimizer/issues/104/comments?per_page=100",
    )
    assert commands.calls[3][0] == (
        "gh",
        "api",
        "--method",
        "POST",
        "repos/octo-org/optimizer/issues/104/comments",
        "--input",
        "-",
    )
    assert all(
        environment == {"GH_TOKEN": "assignment-token"}
        for _, environment, _ in commands.calls
    )
    body = json.loads(commands.calls[3][2])["body"]
    assert "@copilot" in body
    assert "pull request #104" in body
    assert "issue #31" in body
    assert "foundry-optimization-steward.agent.md" in body
    assert "assignment-token" not in body


def test_workspace_assignment_is_noop_while_already_assigned(
    tmp_path: Path,
) -> None:
    assignment_key = "revision-1"
    marker_hash = hashlib.sha256(
        assignment_key.encode("utf-8")
    ).hexdigest()[:16]
    commands = Commands(
        [
            json.dumps(
                {
                    "number": 104,
                    "state": "open",
                    "pull_request": {"url": "https://example.invalid/pr/104"},
                    "assignees": [],
                }
            ),
            json.dumps(
                {"id": 123, "login": "octocat", "type": "User"}
            ),
            json.dumps(
                [[
                    {
                        "body": (
                            "<!-- foundry-opt:"
                            "workspace-copilot-assignment:"
                            f"issue-31:{marker_hash}:v1 -->"
                        ),
                        "user": {
                            "id": 123,
                            "login": "octocat",
                            "type": "User",
                        },
                    }
                ]]
            ),
        ]
    )

    assigned = GhWorkspaceCopilotAssigner(
        commands,
        repository_root=tmp_path,
        repository="octo-org/optimizer",
        assignment_token="assignment-token",
    ).assign(
        issue_number=31,
        pull_request_number=104,
        assignment_key=assignment_key,
    )

    assert assigned is False
    assert len(commands.calls) == 3


def _assignment_comment(
    *,
    comment_id: int = 44,
    user_id: int = 123,
    login: str = "octocat",
) -> dict[str, object]:
    return {
        "id": comment_id,
        "body": (
            "<!-- foundry-opt:workspace-copilot-assignment:"
            "issue-31:assignment-a1:v1 -->\n"
            "@copilot Continue this workspace."
        ),
        "user": {
            "id": user_id,
            "login": login,
            "type": "User",
        },
    }


def _cleaner(commands: Commands, tmp_path: Path) -> GhWorkspaceAssignmentCleaner:
    return GhWorkspaceAssignmentCleaner(
        commands,
        repository_root=tmp_path,
        repository="octo-org/optimizer",
        assignment_token="assignment-token",
    )


def test_workspace_assignment_cleanup_deletes_exact_authenticated_marker(
    tmp_path: Path,
) -> None:
    commands = Commands(
        [
            json.dumps(
                {"id": 123, "login": "octocat", "type": "User"}
            ),
            json.dumps([[_assignment_comment()]]),
            "",
        ]
    )

    deleted = _cleaner(commands, tmp_path).cleanup(
        issue_number=31,
        pull_request_number=104,
        assignment_marker_key="issue-31:assignment-a1:v1",
    )

    assert deleted is True
    assert commands.calls[-1][0] == (
        "gh",
        "api",
        "--method",
        "DELETE",
        "repos/octo-org/optimizer/issues/comments/44",
    )
    assert all(
        environment == {"GH_TOKEN": "assignment-token"}
        for _, environment, _ in commands.calls
    )


def test_workspace_assignment_cleanup_replay_already_absent(
    tmp_path: Path,
) -> None:
    commands = Commands(
        [
            json.dumps(
                {"id": 123, "login": "octocat", "type": "User"}
            ),
            json.dumps([[]]),
        ]
    )

    deleted = _cleaner(commands, tmp_path).cleanup(
        issue_number=31,
        pull_request_number=104,
        assignment_marker_key="issue-31:assignment-a1:v1",
    )

    assert deleted is False
    assert len(commands.calls) == 2


def test_workspace_assignment_cleanup_rejects_ambiguous_marker(
    tmp_path: Path,
) -> None:
    commands = Commands(
        [
            json.dumps(
                {"id": 123, "login": "octocat", "type": "User"}
            ),
            json.dumps(
                [[
                    _assignment_comment(comment_id=44),
                    _assignment_comment(comment_id=45),
                ]]
            ),
        ]
    )

    with pytest.raises(RuntimeError, match="ambiguous"):
        _cleaner(commands, tmp_path).cleanup(
            issue_number=31,
            pull_request_number=104,
            assignment_marker_key="issue-31:assignment-a1:v1",
        )


def test_workspace_assignment_cleanup_rejects_foreign_author(
    tmp_path: Path,
) -> None:
    commands = Commands(
        [
            json.dumps(
                {"id": 123, "login": "octocat", "type": "User"}
            ),
            json.dumps(
                [[
                    _assignment_comment(
                        user_id=456,
                        login="someone-else",
                    )
                ]]
            ),
        ]
    )

    with pytest.raises(RuntimeError, match="foreign author"):
        _cleaner(commands, tmp_path).cleanup(
            issue_number=31,
            pull_request_number=104,
            assignment_marker_key="issue-31:assignment-a1:v1",
        )
