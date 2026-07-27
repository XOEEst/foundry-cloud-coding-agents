from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from pathlib import Path

import pytest

from foundry_opt.adapters.commands import CommandExitError
from foundry_opt.adapters.github_campaign import (
    GhCampaignGateway,
    GitHubCampaignGatewayError,
)
from foundry_opt.github_workflow import (
    GitHubCapabilities,
    PullRequestReference,
)
from foundry_opt.preflight.interfaces import CommandResult


class FakeCommands:
    def __init__(
        self,
        responses: dict[tuple[str, ...], str | Exception],
    ) -> None:
        self.responses = responses
        self.invocations: list[tuple[str, ...]] = []

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
        environment: Mapping[str, str] | None = None,
        input_text: str | None = None,
    ) -> CommandResult:
        command = tuple(arguments)
        self.invocations.append(command)
        response = self.responses.get(command, "")
        if isinstance(response, Exception):
            raise response
        return CommandResult(0, response, "")


def _metadata() -> str:
    return json.dumps(
        {
            "full_name": "octo-org/optimizer",
            "default_branch": "main",
            "permissions": {
                "admin": False,
                "maintain": False,
                "push": True,
                "triage": True,
                "pull": True,
            },
        }
    )


def _gateway(
    extra: dict[tuple[str, ...], str | Exception] | None = None,
) -> tuple[GhCampaignGateway, FakeCommands]:
    responses: dict[tuple[str, ...], str | Exception] = {
        ("git", "remote", "get-url", "origin"): (
            "https://github.com/octo-org/optimizer.git\n"
        ),
        ("gh", "api", "repos/octo-org/optimizer"): _metadata(),
    }
    responses.update(extra or {})
    commands = FakeCommands(responses)
    return GhCampaignGateway(commands, Path("repository")), commands


def test_runtime_permission_probe_accepts_write_installation_token_without_admin() -> None:
    gateway, commands = _gateway()

    report = gateway.verify_permissions(
        GitHubCapabilities.CAMPAIGN_PUBLICATION
    )

    assert report.granted == GitHubCapabilities.CAMPAIGN_PUBLICATION
    assert ("gh", "api", "user") not in commands.invocations
    assert not any("viewerPermission" in part for call in commands.invocations for part in call)


def test_runtime_can_use_declared_installation_token_capabilities() -> None:
    responses = {
        ("git", "remote", "get-url", "origin"): (
            "https://github.com/octo-org/optimizer.git\n"
        ),
        (
            "gh",
            "api",
            "repos/octo-org/optimizer",
        ): json.dumps(
            {
                "full_name": "octo-org/optimizer",
                "default_branch": "main",
            }
        ),
    }
    commands = FakeCommands(responses)
    gateway = GhCampaignGateway(
        commands,
        Path("repository"),
        granted_capabilities=GitHubCapabilities.CAMPAIGN_PUBLICATION,
    )

    report = gateway.verify_permissions(
        GitHubCapabilities.CAMPAIGN_PUBLICATION
    )

    assert report.granted == GitHubCapabilities.CAMPAIGN_PUBLICATION


def test_all_gh_operations_are_argument_arrays_with_explicit_repo() -> None:
    create = (
        "gh",
        "pr",
        "create",
        "--repo",
        "octo-org/optimizer",
        "--draft",
        "--base",
        "main",
        "--head",
        "foundry-opt/campaign-1",
        "--title",
        "Campaign",
        "--body",
        "Safe body",
    )
    gateway, commands = _gateway(
        {
            create: "https://github.com/octo-org/optimizer/pull/42\n",
        }
    )

    result = gateway.create_campaign_pull_request(
        Path("repository"),
        base_branch="main",
        head_branch="foundry-opt/campaign-1",
        head_commit="d" * 40,
        title="Campaign",
        body="Safe body",
    )

    assert result == PullRequestReference(
        42,
        "https://github.com/octo-org/optimizer/pull/42",
        "foundry-opt/campaign-1",
        "d" * 40,
        True,
        "Safe body",
    )
    assert create in commands.invocations
    assert (
        "git",
        "push",
        "--force-with-lease=refs/heads/foundry-opt/campaign-1:",
        "origin",
        "d" * 40 + ":refs/heads/foundry-opt/campaign-1",
    ) in commands.invocations


def test_gateway_errors_are_stable_and_do_not_expose_command_output() -> None:
    command = ("gh", "api", "repos/octo-org/optimizer")
    secret = "github_pat_secret"
    gateway, _ = _gateway(
        {
            command: CommandExitError(
                command,
                exit_code=403,
                stdout="",
                stderr=f"denied token={secret}",
            )
        }
    )

    with pytest.raises(GitHubCampaignGatewayError) as raised:
        gateway.verify_permissions(
            GitHubCapabilities.CAMPAIGN_PUBLICATION
        )

    assert raised.value.operation == "repository_metadata"
    assert secret not in str(raised.value)
