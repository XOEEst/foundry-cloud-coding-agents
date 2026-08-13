from collections.abc import Mapping, Sequence
import json
from pathlib import Path

from foundry_opt.orchestration.workspace_assignment import (
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
                {
                    "number": 104,
                    "assignees": [
                        {"login": "copilot-swe-agent[bot]"}
                    ],
                }
            ),
        ]
    )

    assigned = GhWorkspaceCopilotAssigner(
        commands,
        repository_root=tmp_path,
        repository="octo-org/optimizer",
        assignment_token="assignment-token",
    ).assign(issue_number=31, pull_request_number=104)

    assert assigned is True
    assert len(commands.calls) == 2
    assert commands.calls[0][0] == (
        "gh",
        "api",
        "repos/octo-org/optimizer/issues/104",
    )
    assert commands.calls[1][0] == (
        "gh",
        "api",
        "--method",
        "POST",
        "repos/octo-org/optimizer/issues/104/assignees",
        "--input",
        "-",
    )
    assert commands.calls[0][1] == {"GH_TOKEN": "assignment-token"}
    assert commands.calls[1][1] == {"GH_TOKEN": "assignment-token"}
    body = json.loads(commands.calls[1][2])
    assert body["assignees"] == ["copilot-swe-agent[bot]"]
    assert body["agent_assignment"]["custom_agent"] == (
        "foundry-optimization-steward"
    )
    assert "pull request #104" in body["agent_assignment"][
        "custom_instructions"
    ]
    assert "issue #31" in body["agent_assignment"]["custom_instructions"]
    assert "assignment-token" not in commands.calls[1][2]


def test_workspace_assignment_is_noop_while_already_assigned(
    tmp_path: Path,
) -> None:
    commands = Commands(
        [
            json.dumps(
                {
                    "number": 104,
                    "state": "open",
                    "pull_request": {"url": "https://example.invalid/pr/104"},
                    "assignees": [
                        {"login": "copilot-swe-agent[bot]"}
                    ],
                }
            )
        ]
    )

    assigned = GhWorkspaceCopilotAssigner(
        commands,
        repository_root=tmp_path,
        repository="octo-org/optimizer",
        assignment_token="assignment-token",
    ).assign(issue_number=31, pull_request_number=104)

    assert assigned is False
    assert len(commands.calls) == 1
