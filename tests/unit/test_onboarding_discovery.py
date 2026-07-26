from collections.abc import Sequence
from pathlib import Path

from foundry_opt.adapters.discovery import (
    AzureSdkFoundryInventory,
    FoundryInventory,
    LocalOnboardingDiscovery,
)
from foundry_opt.onboarding import (
    AppInsightsDiscovery,
    DatasetDiscovery,
    DeployedModelDiscovery,
    EvaluatorDiscovery,
    FoundryAgentDiscovery,
    OnboardingRequest,
)
from foundry_opt.preflight.interfaces import CommandResult


class FakeCommands:
    def __init__(self) -> None:
        self.responses = {
            ("git", "branch", "--show-current"): "main\n",
            ("git", "status", "--porcelain", "--untracked-files=all"): "",
            ("git", "remote", "get-url", "origin"): (
                "https://github.com/octo-org/agents.git\n"
            ),
            ("gh", "api", "user", "--jq", ".login"): "octocat\n",
            ("gh", "api", "repos/octo-org/agents"): (
                '{"id":123456,"full_name":"octo-org/agents",'
                '"default_branch":"main","permissions":{"admin":true}}'
            ),
        }

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
    ) -> CommandResult:
        return CommandResult(
            exit_code=0,
            stdout=self.responses[tuple(arguments)],
            stderr="",
        )


class FakeFoundryInventory:
    def discover(self, request: OnboardingRequest) -> FoundryInventory:
        return FoundryInventory(
            agents=(FoundryAgentDiscovery("support-agent", ("4", "5")),),
            deployed_models=(DeployedModelDiscovery("gpt-5.1"),),
            datasets=(DatasetDiscovery("dev", ("1",)),),
            evaluators=(EvaluatorDiscovery("quality", "quality:1"),),
            app_insights=AppInsightsDiscovery(connected=True),
        )


def _request(repository_root: Path) -> OnboardingRequest:
    return OnboardingRequest(
        repository_root=repository_root,
        environment_name="acceptance",
        target_name="support-agent",
        project_endpoint=(
            "https://example.services.ai.azure.com/api/projects/demo"
        ),
        project_resource_id="/subscriptions/sub/projects/demo",
        tenant_id="tenant",
        client_id="client",
        subscription_id="subscription",
        product_install="foundry-cloud-coding-agent==0.1.0",
    )


def test_discovery_finds_agents_checks_foundry_resources_and_workflows(
    tmp_path: Path,
) -> None:
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent/main.py").write_text(
        "from azure.ai.projects import AIProjectClient\n"
        "SYSTEM_INSTRUCTIONS = 'Help the user.'\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\n"
        "[tool.ruff]\nline-length = 88\n",
        encoding="utf-8",
    )
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "name: CI\non: [pull_request]\njobs:\n  test:\n"
        "    steps:\n      - run: uv run pytest -q\n",
        encoding="utf-8",
    )
    (workflows / "deploy.yml").write_text(
        "name: Deploy Foundry agent\non:\n  push:\n    branches: [main]\n"
        "jobs:\n  deploy:\n    steps:\n      - run: azd deploy\n",
        encoding="utf-8",
    )

    result = LocalOnboardingDiscovery(
        FakeCommands(),
        FakeFoundryInventory(),
    ).discover(_request(tmp_path))

    assert result.repository == "octo-org/agents"
    assert result.repository_id == "123456"
    assert result.authenticated_login == "octocat"
    assert result.viewer_permission == "ADMIN"
    assert result.clean is True
    assert result.python_agents[0].entry_point == Path("agent/main.py")
    assert result.validation_commands == (
        "uv run pytest",
        "uv run ruff check .",
        "uv run pytest -q",
    )
    assert result.foundry_agents[0].versions == ("4", "5")
    assert result.app_insights.connected is True
    assert result.deployment_workflows[0].path == Path(
        ".github/workflows/deploy.yml"
    )
    assert result.deployment_workflows[0].trigger == "merge"


def test_sdk_inventory_uses_supported_read_only_collections(
    tmp_path: Path,
) -> None:
    class Closable:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    credential = Closable()

    class Agents:
        def list(self):
            return [{"name": "support-agent"}]

        def list_versions(self, name, *, include_drafts):
            assert name == "support-agent"
            assert include_drafts is True
            return [{"version": "2"}, {"version": "draft-probe"}]

    class Deployments:
        def list(self):
            return [{"name": "gpt-5.1"}]

    class Datasets:
        def list(self):
            return [
                {"name": "development", "version": "2"},
                {"name": "development", "version": "1"},
            ]

    class Evaluators:
        def list(self, *, type):
            assert type == "all"
            return [{"name": "quality", "version": "3"}]

    class Connections:
        def list(self):
            return [{
                "connection_type": "ApplicationInsights",
                "resource_id": "/subscriptions/sub/workspaces/logs",
            }]

    class Client(Closable):
        agents = Agents()
        deployments = Deployments()
        datasets = Datasets()
        beta = type("Beta", (), {"evaluators": Evaluators()})()
        connections = Connections()

    client = Client()
    inventory = AzureSdkFoundryInventory(
        credential_factory=lambda tenant: credential,
        client_factory=lambda endpoint, supplied: client,
    ).discover(_request(tmp_path))

    assert inventory.agents == (
        FoundryAgentDiscovery(
            "support-agent",
            ("2", "draft-probe"),
        ),
    )
    assert inventory.deployed_models == (
        DeployedModelDiscovery("gpt-5.1"),
    )
    assert inventory.datasets == (
        DatasetDiscovery("development", ("1", "2")),
    )
    assert inventory.evaluators == (
        EvaluatorDiscovery("quality", "quality:3"),
    )
    assert inventory.app_insights == AppInsightsDiscovery(connected=True)
    assert client.closed is True
    assert credential.closed is True
