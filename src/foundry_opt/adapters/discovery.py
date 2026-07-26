from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
import json
from pathlib import Path
import re
import tomllib
from typing import Any, Protocol

from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential
import yaml

from foundry_opt.adapters.foundry import _validate_project_endpoint
from foundry_opt.adapters.github import github_repository_from_remote_url
from foundry_opt.onboarding.models import (
    AppInsightsDiscovery,
    DatasetDiscovery,
    DeployedModelDiscovery,
    DeploymentWorkflowDiscovery,
    EvaluatorDiscovery,
    FoundryAgentDiscovery,
    MetricDiscovery,
    OnboardingRequest,
    PythonAgentCandidate,
    RepositoryDiscovery,
)
from foundry_opt.preflight.interfaces import CommandRunner


class DiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class FoundryInventory:
    agents: tuple[FoundryAgentDiscovery, ...] = ()
    deployed_models: tuple[DeployedModelDiscovery, ...] = ()
    datasets: tuple[DatasetDiscovery, ...] = ()
    evaluators: tuple[EvaluatorDiscovery, ...] = ()
    app_insights: AppInsightsDiscovery = AppInsightsDiscovery(False)


class FoundryInventoryGateway(Protocol):
    def discover(self, request: OnboardingRequest) -> FoundryInventory: ...


class LocalOnboardingDiscovery:
    def __init__(
        self,
        command_runner: CommandRunner,
        foundry_inventory: FoundryInventoryGateway,
    ) -> None:
        self._commands = command_runner
        self._foundry_inventory = foundry_inventory

    def discover(self, request: OnboardingRequest) -> RepositoryDiscovery:
        root = request.repository_root
        current_branch = self._run(
            ("git", "branch", "--show-current"),
            root,
        )
        status = self._run(
            ("git", "status", "--porcelain", "--untracked-files=all"),
            root,
        )
        remote = self._run(("git", "remote", "get-url", "origin"), root)
        repository = github_repository_from_remote_url(remote)
        if repository is None:
            raise DiscoveryError("origin is not a supported GitHub repository")

        login = self._run(("gh", "api", "user", "--jq", ".login"), root)
        try:
            metadata = json.loads(
                self._run(("gh", "api", f"repos/{repository}"), root)
            )
            resolved_repository = metadata["full_name"]
            repository_id = str(metadata["id"])
            default_branch = metadata["default_branch"]
            admin = metadata["permissions"]["admin"]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise DiscoveryError(
                "GitHub returned invalid repository metadata"
            ) from error
        if (
            not isinstance(resolved_repository, str)
            or resolved_repository.casefold() != repository.casefold()
            or not isinstance(default_branch, str)
            or not default_branch
            or not isinstance(admin, bool)
        ):
            raise DiscoveryError("GitHub returned invalid repository metadata")

        inventory = self._foundry_inventory.discover(request)
        return RepositoryDiscovery(
            repository=resolved_repository,
            repository_id=repository_id,
            default_branch=default_branch,
            current_branch=current_branch,
            authenticated_login=login,
            viewer_permission="ADMIN" if admin else "WRITE",
            clean=not status,
            python_agents=_discover_python_agents(root),
            validation_commands=_discover_validation_commands(root),
            foundry_agents=inventory.agents,
            deployed_models=inventory.deployed_models,
            datasets=inventory.datasets,
            evaluators=inventory.evaluators,
            app_insights=inventory.app_insights,
            deployment_workflows=_discover_deployment_workflows(root),
        )

    def _run(self, arguments: tuple[str, ...], cwd: Path) -> str:
        try:
            return self._commands.run(arguments, cwd=cwd).stdout.strip()
        except Exception as error:
            raise DiscoveryError(f"{arguments[0]} discovery command failed") from error


CredentialFactory = Callable[[str], Any]
ClientFactory = Callable[[str, Any], Any]


def _credential_factory(tenant_id: str) -> AzureCliCredential:
    return AzureCliCredential(tenant_id=tenant_id)


def _client_factory(endpoint: str, credential: Any) -> AIProjectClient:
    return AIProjectClient(
        endpoint=endpoint,
        credential=credential,
        allow_preview=True,
    )


class AzureSdkFoundryInventory:
    def __init__(
        self,
        *,
        credential_factory: CredentialFactory = _credential_factory,
        client_factory: ClientFactory = _client_factory,
    ) -> None:
        self._credential_factory = credential_factory
        self._client_factory = client_factory

    def discover(self, request: OnboardingRequest) -> FoundryInventory:
        _validate_project_endpoint(request.project_endpoint)
        credential = self._credential_factory(request.tenant_id)
        client = None
        try:
            client = self._client_factory(request.project_endpoint, credential)
            agents = tuple(
                FoundryAgentDiscovery(
                    name=name,
                    versions=tuple(
                        sorted(
                            {
                                str(_attribute(version, "version", "id"))
                                for version in client.agents.list_versions(
                                    name,
                                    include_drafts=True,
                                )
                                if _attribute(version, "version", "id") is not None
                            },
                            key=_version_sort_key,
                        )
                    ),
                )
                for name in sorted(
                    {
                        str(_attribute(agent, "name"))
                        for agent in client.agents.list()
                        if _attribute(agent, "name") is not None
                    }
                )
            )
            deployments = tuple(
                DeployedModelDiscovery(name=name)
                for name in sorted(
                    {
                        str(
                            _attribute(
                                deployment,
                                "name",
                                "deployment_name",
                                "model_name",
                            )
                        )
                        for deployment in client.deployments.list()
                        if _attribute(
                            deployment,
                            "name",
                            "deployment_name",
                            "model_name",
                        )
                        is not None
                    }
                )
            )
            datasets = _group_versions(
                client.datasets.list(),
                DatasetDiscovery,
            )
            evaluators = _discover_evaluators(
                client.beta.evaluators.list(type="all")
            )
            app_insights = _discover_app_insights(client.connections.list())
            return FoundryInventory(
                agents=agents,
                deployed_models=deployments,
                datasets=datasets,
                evaluators=evaluators,
                app_insights=app_insights,
            )
        finally:
            _close(client)
            _close(credential)


def _discover_python_agents(root: Path) -> tuple[PythonAgentCandidate, ...]:
    candidates: list[PythonAgentCandidate] = []
    ignored = {
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        "tests",
        "test",
    }
    markers = (
        "AIProjectClient",
        "agents.create",
        "create_version",
        "SYSTEM_INSTRUCTIONS",
        "system_instructions",
    )
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if any(part in ignored or part.startswith(".") for part in relative.parts):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if path.name not in {"agent.py", "app.py", "main.py"} and not any(
            marker in content for marker in markers
        ):
            continue
        source = relative.parent if relative.parent != Path(".") else relative
        name = source.name if source != relative else relative.stem
        candidates.append(
            PythonAgentCandidate(
                name=name.replace("_", "-"),
                source_path=source,
                entry_point=relative,
            )
        )
    return tuple(candidates)


def _discover_validation_commands(root: Path) -> tuple[str, ...]:
    commands: list[str] = []
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            document = {}
        tool = document.get("tool", {})
        if "pytest" in tool:
            commands.append("uv run pytest")
        if "ruff" in tool:
            commands.append("uv run ruff check .")
        if "mypy" in tool:
            commands.append("uv run mypy .")
        if "pyright" in tool:
            commands.append("uv run pyright")

    workflow_root = root / ".github/workflows"
    if workflow_root.is_dir():
        paths = (*workflow_root.glob("*.yml"), *workflow_root.glob("*.yaml"))
        for path in sorted(paths):
            for command in _workflow_run_commands(path):
                if _is_validation_command(command):
                    commands.append(command)
    return tuple(dict.fromkeys(commands))


def _workflow_run_commands(path: Path) -> tuple[str, ...]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError):
        return ()
    commands: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "run" and isinstance(child, str):
                    commands.extend(
                        line.strip()
                        for line in child.splitlines()
                        if line.strip()
                    )
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(document)
    return tuple(commands)


def _is_validation_command(command: str) -> bool:
    lowered = command.casefold()
    return any(
        marker in lowered
        for marker in (
            "pytest",
            "ruff",
            "mypy",
            "pyright",
            "tox",
            "nox",
            "python -m compileall",
        )
    )


def _discover_deployment_workflows(
    root: Path,
) -> tuple[DeploymentWorkflowDiscovery, ...]:
    workflow_root = root / ".github/workflows"
    if not workflow_root.is_dir():
        return ()
    discoveries: list[DeploymentWorkflowDiscovery] = []
    paths = sorted((*workflow_root.glob("*.yml"), *workflow_root.glob("*.yaml")))
    for path in paths:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        lowered = f"{path.name}\n{content}".casefold()
        if "deploy" not in lowered or not any(
            marker in lowered for marker in ("foundry", "azure", "azd")
        ):
            continue
        trigger = (
            "merge"
            if re.search(r"(?m)^\s*push\s*:", content)
            else "manual"
        )
        discoveries.append(
            DeploymentWorkflowDiscovery(
                path=path.relative_to(root),
                trigger=trigger,
            )
        )
    return tuple(discoveries)


def _group_versions(
    items: Iterable[Any],
    factory: type[DatasetDiscovery],
) -> tuple[DatasetDiscovery, ...]:
    grouped: dict[str, set[str]] = {}
    for item in items:
        name = _attribute(item, "name")
        version = _attribute(item, "version", "id")
        if name is None or version is None:
            continue
        grouped.setdefault(str(name), set()).add(str(version))
    return tuple(
        factory(
            name=name,
            versions=tuple(sorted(versions, key=_version_sort_key)),
        )
        for name, versions in sorted(grouped.items())
    )


def _discover_app_insights(
    connections: Iterable[Any],
) -> AppInsightsDiscovery:
    for connection in connections:
        connection_type = str(
            _attribute(connection, "type", "connection_type") or ""
        ).casefold()
        if "app" not in connection_type or "insight" not in connection_type:
            continue
        resource_id = _attribute(connection, "workspace_resource_id")
        if isinstance(resource_id, str) and resource_id.startswith("/"):
            return AppInsightsDiscovery(
                connected=True,
                workspace_resource_id=resource_id,
            )
        return AppInsightsDiscovery(connected=True)
    return AppInsightsDiscovery(connected=False)


def _discover_evaluators(
    items: Iterable[Any],
) -> tuple[EvaluatorDiscovery, ...]:
    discovered: dict[tuple[str, str], EvaluatorDiscovery] = {}
    for item in items:
        name = _attribute(item, "name")
        version = _attribute(item, "version", "id")
        if name is None or version is None:
            continue
        name = str(name)
        version = str(version)
        metrics, needs_input = _metric_semantics(item)
        discovered[(name, version)] = EvaluatorDiscovery(
            name=name,
            reference=f"{name}:{version}",
            metrics=metrics,
            needs_input=needs_input,
        )
    return tuple(discovered[key] for key in sorted(discovered))


def _metric_semantics(
    evaluator: Any,
) -> tuple[tuple[MetricDiscovery, ...], str | None]:
    tags = _attribute(evaluator, "tags")
    raw = (
        tags.get("foundry-opt.metrics")
        if isinstance(tags, dict)
        else None
    )
    if raw is None:
        return (
            (),
            "Foundry did not expose optimizer metric policy semantics.",
        )
    try:
        values = json.loads(raw)
        if not isinstance(values, list) or not values:
            raise ValueError
        if any(
            not isinstance(value, dict)
            or not isinstance(value.get("name"), str)
            or not value["name"]
            or value.get("direction") not in {"maximize", "minimize"}
            or not isinstance(value.get("threshold"), (int, float))
            or isinstance(value.get("threshold"), bool)
            or not isinstance(value.get("materiality"), (int, float))
            or isinstance(value.get("materiality"), bool)
            or value["materiality"] <= 0
            or type(value.get("hard_guardrail")) is not bool
            for value in values
        ):
            raise ValueError
        metrics = tuple(
            MetricDiscovery(
                name=value["name"],
                direction=value["direction"],
                threshold=float(value["threshold"]),
                materiality=float(value["materiality"]),
                hard_guardrail=value["hard_guardrail"],
            )
            for value in values
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return (
            (),
            "The foundry-opt.metrics evaluator tag is invalid; provide "
            "explicit direction, threshold, materiality, and guardrail input.",
        )
    return metrics, None


def _attribute(value: Any, *names: str) -> Any | None:
    for name in names:
        result = getattr(value, name, None)
        if result is not None:
            return getattr(result, "value", result)
        if isinstance(value, dict) and value.get(name) is not None:
            return value[name]
    return None


def _version_sort_key(value: str) -> tuple[int, int | str]:
    return (0, int(value)) if value.isdecimal() else (1, value)


def _close(resource: Any | None) -> None:
    close = getattr(resource, "close", None)
    if callable(close):
        with suppress(Exception):
            close()
