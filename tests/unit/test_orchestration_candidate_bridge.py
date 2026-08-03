from __future__ import annotations

import json
from pathlib import Path

from foundry_opt.orchestration.candidate_bridge import (
    GhApplierWorkerGateway,
    GhCandidateSupersessionGateway,
)
from foundry_opt.preflight.interfaces import CommandResult


class Commands:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.responses: list[str] = []

    def run(
        self,
        arguments,
        *,
        cwd: Path | None = None,
        environment=None,
        input_text: str | None = None,
        input_bytes: bytes | None = None,
    ) -> CommandResult:
        self.calls.append(
            {
                "arguments": tuple(arguments),
                "environment": environment,
                "input_text": input_text,
            }
        )
        return CommandResult(
            0,
            self.responses.pop(0) if self.responses else "",
            "",
        )


def test_github_bridge_creates_and_assigns_exact_patch_worker_issue() -> None:
    commands = Commands()
    commands.responses = [
        "[[]]",
        '{"number":84}',
        '{"assignees":[]}',
        "[[]]",
        "",
        "",
        "",
    ]
    gateway = GhApplierWorkerGateway(
        commands,
        Path("repository"),
        "octo-org/optimizer",
        assignment_token="assignment-token",
    )
    marker = (
        "<!-- foundry-opt:candidate-pr:"
        "issue-31:g2:candidate-1:0123456789abcdef0123 -->"
    )

    assert gateway.find_issue(marker) is None
    assert gateway.create_issue(
        title="[foundry-opt] Apply exact candidate-1",
        body=f"{marker}\nExact patch only.\n",
        marker=marker,
    ) == 84
    assert gateway.has_assignment_marker(84, marker) is False
    gateway.assign_exact_patch_specialist(84, marker=marker)
    gateway.record_assignment_marker(84, marker)

    payloads = [
        json.loads(str(call["input_text"]))
        for call in commands.calls
        if call["input_text"] is not None
    ]
    assert {
        "assignees": ["copilot-swe-agent[bot]"],
        "agent_assignment": {
            "custom_agent": "foundry-candidate-applier",
            "custom_instructions": (
                "Apply the exact steward-attested patch and open the "
                "native candidate pull request. Do not make extra edits."
            ),
            "target_repo": "octo-org/optimizer",
        },
    } in payloads
    assert all(
        "pulls" not in str(call["arguments"])
        for call in commands.calls
    )
    assignment_calls = [
        call
        for call in commands.calls
        if call["arguments"][2:4]
        in {("--method", "DELETE"), ("--method", "POST")}
        and call["arguments"][-3].endswith("/assignees")
    ]
    assert assignment_calls
    assert all(
        call["environment"] == {"GH_TOKEN": "assignment-token"}
        for call in assignment_calls
    )
    assert all(
        call["environment"] is None
        for call in commands.calls
        if call not in assignment_calls
    )
    assert all(
        "assignment-token" not in " ".join(call["arguments"])
        and "assignment-token" not in str(call["input_text"])
        for call in commands.calls
    )


def test_github_bridge_closes_superseded_issue_and_pull_request() -> None:
    commands = Commands()
    commands.responses = [
        '{"state":"open"}',
        "[[]]",
        "",
        "",
        '{"state":"open"}',
        "[[]]",
        "",
        "",
    ]
    gateway = GhCandidateSupersessionGateway(
        commands,
        Path("repository"),
        "octo-org/optimizer",
    )
    marker = (
        "<!-- foundry-opt:candidate-pr:"
        "issue-31:g2:candidate-2:0123456789abcdef0123 -->"
    )

    assert gateway.issue_is_superseded(84, marker) is False
    gateway.supersede_issue(84, "superseded", marker)
    assert gateway.pull_request_is_superseded(92, marker) is False
    gateway.supersede_pull_request(92, "superseded", marker)

    endpoints = [
        call["arguments"][4]
        for call in commands.calls
        if call["input_text"] is not None
    ]
    assert "repos/octo-org/optimizer/issues/84" in endpoints
    assert "repos/octo-org/optimizer/pulls/92" in endpoints
