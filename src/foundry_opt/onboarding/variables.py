from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from typing import Protocol
from urllib.parse import quote

from foundry_opt.adapters.commands import (
    CommandError,
    CommandExitError,
    SubprocessCommandRunner,
)
from foundry_opt.onboarding.models import (
    GitHubVariableChange,
    GitHubVariableChangeStatus,
    GitHubVariableScope,
    OnboardingRequest,
    RepositoryDiscovery,
)
from foundry_opt.preflight.interfaces import CommandRunner


_VARIABLE_NAMES = (
    "AZURE_TENANT_ID",
    "AZURE_CLIENT_ID",
    "AZURE_SUBSCRIPTION_ID",
)


class GitHubVariableConflictError(RuntimeError):
    def __init__(self, names: tuple[str, ...]) -> None:
        self.names = names
        super().__init__(
            "GitHub variables already exist with different values: "
            + ", ".join(names)
        )


class GitHubVariableGateway(Protocol):
    def list_agents_variables(self, repository: str) -> dict[str, str]: ...

    def create_agents_variable(
        self,
        repository: str,
        name: str,
        value: str,
    ) -> None: ...

    def update_agents_variable(
        self,
        repository: str,
        name: str,
        value: str,
    ) -> None: ...

    def list_environment_variables(
        self,
        repository: str,
        environment: str,
    ) -> dict[str, str]: ...

    def create_environment_variable(
        self,
        repository: str,
        environment: str,
        name: str,
        value: str,
    ) -> None: ...

    def update_environment_variable(
        self,
        repository: str,
        environment: str,
        name: str,
        value: str,
    ) -> None: ...


class UnavailableGitHubVariableConfigurer:
    def configure(
        self,
        request: OnboardingRequest,
        discovery: RepositoryDiscovery,
    ) -> tuple[GitHubVariableChange, ...]:
        raise RuntimeError("GitHub variable configuration is unavailable")


class GitHubVariableConfigurator:
    def __init__(self, gateway: GitHubVariableGateway) -> None:
        self._gateway = gateway

    def configure(
        self,
        request: OnboardingRequest,
        discovery: RepositoryDiscovery,
    ) -> tuple[GitHubVariableChange, ...]:
        desired = {
            "AZURE_TENANT_ID": request.tenant_id,
            "AZURE_CLIENT_ID": request.client_id,
            "AZURE_SUBSCRIPTION_ID": request.subscription_id,
        }
        agents = self._gateway.list_agents_variables(discovery.repository)
        environment_name = request.mirror_actions_environment
        environment = (
            self._gateway.list_environment_variables(
                discovery.repository,
                environment_name,
            )
            if environment_name is not None
            else None
        )
        conflicts = tuple(
            name
            for name, value in desired.items()
            if (
                name in agents and agents[name] != value
            )
            or (
                environment is not None
                and name in environment
                and environment[name] != value
            )
        )
        if conflicts and not request.update_github_variables:
            raise GitHubVariableConflictError(conflicts)

        changes: list[GitHubVariableChange] = []
        changes.extend(
            self._apply_scope(
                desired,
                agents,
                scope=GitHubVariableScope.AGENTS,
                create=lambda name, value: (
                    self._gateway.create_agents_variable(
                        discovery.repository,
                        name,
                        value,
                    )
                ),
                update=lambda name, value: (
                    self._gateway.update_agents_variable(
                        discovery.repository,
                        name,
                        value,
                    )
                ),
                allow_update=request.update_github_variables,
            )
        )
        if environment_name is not None and environment is not None:
            changes.extend(
                self._apply_scope(
                    desired,
                    environment,
                    scope=GitHubVariableScope.ACTIONS_ENVIRONMENT,
                    create=lambda name, value: (
                        self._gateway.create_environment_variable(
                            discovery.repository,
                            environment_name,
                            name,
                            value,
                        )
                    ),
                    update=lambda name, value: (
                        self._gateway.update_environment_variable(
                            discovery.repository,
                            environment_name,
                            name,
                            value,
                        )
                    ),
                    allow_update=request.update_github_variables,
                    environment=environment_name,
                )
            )
        return tuple(changes)

    def _apply_scope(
        self,
        desired: Mapping[str, str],
        existing: Mapping[str, str],
        *,
        scope: GitHubVariableScope,
        create,
        update,
        allow_update: bool,
        environment: str | None = None,
    ) -> tuple[GitHubVariableChange, ...]:
        changes: list[GitHubVariableChange] = []
        for name in _VARIABLE_NAMES:
            value = desired[name]
            if name not in existing:
                create(name, value)
                status = GitHubVariableChangeStatus.CREATED
            elif existing[name] == value:
                status = GitHubVariableChangeStatus.UNCHANGED
            elif allow_update:
                update(name, value)
                status = GitHubVariableChangeStatus.UPDATED
            else:
                raise AssertionError("variable conflicts must be prevalidated")
            changes.append(
                GitHubVariableChange(
                    name=name,
                    scope=scope,
                    status=status,
                    environment=environment,
                )
            )
        return tuple(changes)


class GitHubApiVariableGateway:
    def __init__(self, command_runner: CommandRunner | None = None) -> None:
        self._commands = command_runner or SubprocessCommandRunner()

    def list_agents_variables(self, repository: str) -> dict[str, str]:
        endpoint = f"repos/{repository}/agents/variables"
        return self._list(endpoint, missing_environment=False)

    def create_agents_variable(
        self,
        repository: str,
        name: str,
        value: str,
    ) -> None:
        self._write(
            "POST",
            f"repos/{repository}/agents/variables",
            {"name": name, "value": value},
        )

    def update_agents_variable(
        self,
        repository: str,
        name: str,
        value: str,
    ) -> None:
        self._write(
            "PATCH",
            f"repos/{repository}/agents/variables/{quote(name, safe='')}",
            {"name": name, "value": value},
        )

    def list_environment_variables(
        self,
        repository: str,
        environment: str,
    ) -> dict[str, str]:
        endpoint = (
            f"repos/{repository}/environments/"
            f"{quote(environment, safe='')}/variables"
        )
        return self._list(endpoint, missing_environment=True)

    def create_environment_variable(
        self,
        repository: str,
        environment: str,
        name: str,
        value: str,
    ) -> None:
        self._write(
            "POST",
            (
                f"repos/{repository}/environments/"
                f"{quote(environment, safe='')}/variables"
            ),
            {"name": name, "value": value},
        )

    def update_environment_variable(
        self,
        repository: str,
        environment: str,
        name: str,
        value: str,
    ) -> None:
        self._write(
            "PATCH",
            (
                f"repos/{repository}/environments/"
                f"{quote(environment, safe='')}/variables/"
                f"{quote(name, safe='')}"
            ),
            {"name": name, "value": value},
        )

    def _list(
        self,
        endpoint: str,
        *,
        missing_environment: bool,
    ) -> dict[str, str]:
        try:
            output = self._run(
                (
                    "gh",
                    "api",
                    "--method",
                    "GET",
                    "-H",
                    "Accept: application/vnd.github+json",
                    "-H",
                    "X-GitHub-Api-Version: 2026-03-10",
                    endpoint,
                )
            )
        except CommandExitError as error:
            if missing_environment and error.exit_code in {1, 22, 404}:
                raise RuntimeError(
                    "The requested Actions deployment environment does not "
                    "exist or is not accessible."
                ) from error
            raise RuntimeError("GitHub variable lookup failed") from error
        try:
            document = json.loads(output)
            variables = document["variables"]
            if not isinstance(variables, list):
                raise ValueError
            result = {
                item["name"]: item["value"]
                for item in variables
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("name"), str)
                    and isinstance(item.get("value"), str)
                )
            }
            if len(result) != len(variables):
                raise ValueError
            return result
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "GitHub returned invalid variable metadata"
            ) from error

    def _write(
        self,
        method: str,
        endpoint: str,
        body: Mapping[str, str],
    ) -> None:
        try:
            self._run(
                (
                    "gh",
                    "api",
                    "--method",
                    method,
                    "-H",
                    "Accept: application/vnd.github+json",
                    "-H",
                    "X-GitHub-Api-Version: 2026-03-10",
                    "--input",
                    "-",
                    endpoint,
                ),
                input_text=json.dumps(body, separators=(",", ":")),
            )
        except CommandError as error:
            raise RuntimeError("GitHub variable update failed") from error

    def _run(
        self,
        arguments: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> str:
        return self._commands.run(
            arguments,
            input_text=input_text,
        ).stdout
