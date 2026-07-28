from __future__ import annotations

import json
from pathlib import Path

import pytest

from foundry_opt.adapters.commands import CommandExitError
from foundry_opt.onboarding.models import (
    GitHubVariableChangeStatus,
    GitHubVariableScope,
    OnboardingRequest,
    RepositoryDiscovery,
)
from foundry_opt.onboarding.variables import (
    GitHubApiVariableGateway,
    GitHubVariableConflictError,
    GitHubVariableConfigurator,
)
from foundry_opt.preflight.interfaces import CommandResult


def _request(
    *,
    update: bool = False,
    mirror: str | None = None,
) -> OnboardingRequest:
    return OnboardingRequest(
        repository_root=Path("."),
        environment_name="acceptance",
        target_name="support-agent",
        project_endpoint=(
            "https://example.services.ai.azure.com/api/projects/demo"
        ),
        project_resource_id="/subscriptions/sub/projects/demo",
        tenant_id="tenant-id",
        client_id="client-id",
        subscription_id="subscription-id",
        product_install="foundry-cloud-coding-agent==0.1.0",
        set_github_variables=True,
        mirror_actions_environment=mirror,
        update_github_variables=update,
    )


def _discovery() -> RepositoryDiscovery:
    return RepositoryDiscovery(
        repository="octo-org/agents",
        repository_id="123",
        default_branch="main",
        current_branch="main",
        authenticated_login="octocat",
        viewer_permission="ADMIN",
        clean=True,
    )


class MemoryGateway:
    def __init__(
        self,
        *,
        agents: dict[str, str] | None = None,
        environments: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self.agents = dict(agents or {})
        self.environments = {
            name: dict(values)
            for name, values in (environments or {}).items()
        }

    def list_agents_variables(self, repository: str) -> dict[str, str]:
        return dict(self.agents)

    def create_agents_variable(
        self,
        repository: str,
        name: str,
        value: str,
    ) -> None:
        self.agents[name] = value

    def update_agents_variable(
        self,
        repository: str,
        name: str,
        value: str,
    ) -> None:
        self.agents[name] = value

    def list_environment_variables(
        self,
        repository: str,
        environment: str,
    ) -> dict[str, str]:
        if environment not in self.environments:
            raise RuntimeError("deployment environment does not exist")
        return dict(self.environments[environment])

    def create_environment_variable(
        self,
        repository: str,
        environment: str,
        name: str,
        value: str,
    ) -> None:
        self.environments[environment][name] = value

    def update_environment_variable(
        self,
        repository: str,
        environment: str,
        name: str,
        value: str,
    ) -> None:
        self.environments[environment][name] = value


def test_configurator_creates_agents_variables_and_optional_mirror() -> None:
    gateway = MemoryGateway(environments={"production": {}})

    changes = GitHubVariableConfigurator(gateway).configure(
        _request(mirror="production"),
        _discovery(),
    )

    assert gateway.agents == {
        "AZURE_TENANT_ID": "tenant-id",
        "AZURE_CLIENT_ID": "client-id",
        "AZURE_SUBSCRIPTION_ID": "subscription-id",
    }
    assert gateway.environments["production"] == gateway.agents
    assert {
        (change.scope, change.status)
        for change in changes
    } == {
        (
            GitHubVariableScope.AGENTS,
            GitHubVariableChangeStatus.CREATED,
        ),
        (
            GitHubVariableScope.ACTIONS_ENVIRONMENT,
            GitHubVariableChangeStatus.CREATED,
        ),
    }
    assert all(change.value is None for change in changes)


def test_configurator_is_idempotent_when_values_match() -> None:
    expected = {
        "AZURE_TENANT_ID": "tenant-id",
        "AZURE_CLIENT_ID": "client-id",
        "AZURE_SUBSCRIPTION_ID": "subscription-id",
    }
    gateway = MemoryGateway(agents=expected)

    changes = GitHubVariableConfigurator(gateway).configure(
        _request(),
        _discovery(),
    )

    assert all(
        change.status is GitHubVariableChangeStatus.UNCHANGED
        for change in changes
    )


def test_configurator_fails_without_mutation_on_different_value() -> None:
    gateway = MemoryGateway(
        agents={
            "AZURE_TENANT_ID": "different",
            "AZURE_CLIENT_ID": "client-id",
            "AZURE_SUBSCRIPTION_ID": "subscription-id",
        }
    )

    with pytest.raises(GitHubVariableConflictError) as raised:
        GitHubVariableConfigurator(gateway).configure(
            _request(),
            _discovery(),
        )

    assert raised.value.names == ("AZURE_TENANT_ID",)
    assert gateway.agents["AZURE_TENANT_ID"] == "different"


def test_configurator_updates_only_with_explicit_update_flag() -> None:
    gateway = MemoryGateway(
        agents={
            "AZURE_TENANT_ID": "different",
            "AZURE_CLIENT_ID": "client-id",
            "AZURE_SUBSCRIPTION_ID": "subscription-id",
        }
    )

    changes = GitHubVariableConfigurator(gateway).configure(
        _request(update=True),
        _discovery(),
    )

    assert gateway.agents["AZURE_TENANT_ID"] == "tenant-id"
    updated = next(
        change
        for change in changes
        if change.name == "AZURE_TENANT_ID"
    )
    assert updated.status is GitHubVariableChangeStatus.UPDATED


class FakeCommands:
    def __init__(self) -> None:
        self.invocations: list[tuple[tuple[str, ...], str | None]] = []

    def run(
        self,
        arguments,
        *,
        cwd=None,
        environment=None,
        input_text=None,
    ) -> CommandResult:
        command = tuple(arguments)
        self.invocations.append((command, input_text))
        if command[-1] == "repos/octo-org/agents/agents/variables":
            if "--method" in command and command[command.index("--method") + 1] == "GET":
                return CommandResult(
                    0,
                    json.dumps({"variables": [], "total_count": 0}),
                    "",
                )
            return CommandResult(0, "", "")
        if command[-1].endswith("/environments/production/variables"):
            return CommandResult(
                0,
                json.dumps({"variables": [], "total_count": 0}),
                "",
            )
        return CommandResult(0, "", "")


def test_api_gateway_uses_agents_endpoint_and_stdin_for_values() -> None:
    commands = FakeCommands()
    gateway = GitHubApiVariableGateway(commands)

    gateway.create_agents_variable(
        "octo-org/agents",
        "AZURE_CLIENT_ID",
        "client-id",
    )

    command, input_text = commands.invocations[-1]
    assert command[-1] == "repos/octo-org/agents/agents/variables"
    assert "client-id" not in command
    assert json.loads(input_text) == {
        "name": "AZURE_CLIENT_ID",
        "value": "client-id",
    }


def test_api_gateway_reports_missing_actions_environment() -> None:
    class MissingEnvironment(FakeCommands):
        def run(self, arguments, **kwargs):
            command = tuple(arguments)
            if "/environments/missing/variables" in command[-1]:
                raise CommandExitError(
                    command,
                    exit_code=404,
                    stdout="",
                    stderr="Not Found",
                )
            return super().run(arguments, **kwargs)

    gateway = GitHubApiVariableGateway(MissingEnvironment())

    with pytest.raises(RuntimeError, match="deployment environment"):
        gateway.list_environment_variables(
            "octo-org/agents",
            "missing",
        )
