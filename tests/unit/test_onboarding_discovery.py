from collections.abc import Sequence
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from foundry_opt.adapters.discovery import (
    AzureSdkFoundryInventory,
    FoundryInventory,
    LocalOnboardingDiscovery,
    _uses_deployment_identity,
)
from foundry_opt.adapters.foundry import FoundryEndpointError
from foundry_opt.onboarding import (
    AppInsightsDiscovery,
    DatasetDiscovery,
    DeployedModelDiscovery,
    EvaluatorDiscovery,
    FoundryAgentDiscovery,
    MetricDiscovery,
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
    (workflows / "deploy-foundry-agent.yml").write_text(
        "name: Deploy Foundry agent\non:\n  push:\n    branches: [main]\n"
        "permissions:\n  contents: read\n  id-token: write\n"
        "jobs:\n  deploy:\n    environment: acceptance\n    steps:\n"
        "      - uses: "
        "azure/login@532459ea530d8321f2fb9bb10d1e0bcf23869a43\n"
        "        with:\n"
        "          client-id: ${{ vars.AZURE_DEPLOYMENT_CLIENT_ID }}\n"
        "      - run: azd deploy\n"
        "      - uses: actions/upload-artifact@"
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        "        with:\n"
        "          name: foundry-optimization-deployment-result\n"
        "          path: .foundry-optimizer/deployment-result.json\n",
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
        ".github/workflows/deploy-foundry-agent.yml"
    )
    assert result.deployment_workflows[0].trigger == "merge"
    assert (
        result.deployment_workflows[0].deployment_identity_verified
        is True
    )


def test_discovery_ignores_all_generated_optimizer_workflows(
    tmp_path: Path,
) -> None:
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent/main.py").write_text(
        "SYSTEM_INSTRUCTIONS = 'Help the user.'\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
        encoding="utf-8",
    )
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    deployment = (
        "name: Deploy customer agent\n"
        "on:\n  workflow_dispatch:\n"
        "permissions:\n  contents: read\n  id-token: write\n"
        "jobs:\n  deploy:\n    environment: acceptance\n    steps:\n"
        "      - uses: "
        "azure/login@532459ea530d8321f2fb9bb10d1e0bcf23869a43\n"
        "        with:\n"
        "          client-id: ${{ vars.AZURE_DEPLOYMENT_CLIENT_ID }}\n"
        "      - run: azd deploy\n"
        "      - uses: actions/upload-artifact@"
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        "        with:\n"
        "          name: foundry-optimization-deployment-result\n"
        "          path: deployment-result.json\n"
    )
    (workflows / "customer-deploy.yml").write_text(
        deployment,
        encoding="utf-8",
    )
    generated_files: dict[str, str] = {}
    for name in (
        "copilot-setup-steps.yml",
        "deploy-foundry-agent.yml",
        "foundry-exact-candidate-check.yml",
        "foundry-optimization-capability.yml",
        "foundry-optimization-deployment-bridge.yml",
        "foundry-optimization-handoff.yml",
        "foundry-optimization-issue-intake.yml",
        "foundry-optimization-operations.yml",
        "foundry-optimization-reconcile.yml",
        "foundry-optimization-workspace.yml",
        "foundry-post-deployment-check.yml",
    ):
        path = workflows / name
        path.write_text(deployment, encoding="utf-8")
        generated_files[
            path.relative_to(tmp_path).as_posix()
        ] = hashlib.sha256(
            path.read_text(encoding="utf-8").encode("utf-8")
        ).hexdigest()
    (tmp_path / ".github/foundry-optimizer.generated.json").write_text(
        json.dumps({"files": generated_files}),
        encoding="utf-8",
    )

    result = LocalOnboardingDiscovery(
        FakeCommands(),
        FakeFoundryInventory(),
    ).discover(_request(tmp_path))

    assert tuple(
        workflow.path for workflow in result.deployment_workflows
    ) == (Path(".github/workflows/customer-deploy.yml"),)


def test_deployment_identity_must_be_on_the_deploying_job() -> None:
    document = {
        "permissions": {"id-token": "write"},
        "jobs": {
            "identity-decoy": {
                "environment": "acceptance",
                "steps": [
                    {
                        "uses": (
                            "azure/login@"
                            "532459ea530d8321f2fb9bb10d1e0bcf23869a43"
                        ),
                        "with": {
                            "client-id": (
                                "${{ vars.AZURE_DEPLOYMENT_CLIENT_ID }}"
                            ),
                        },
                    },
                ],
            },
            "deploy": {
                "environment": "acceptance",
                "steps": [{"run": "azd deploy"}],
            },
        },
    }

    assert _uses_deployment_identity(document, "acceptance") is False


def test_deployment_identity_login_must_precede_deployment() -> None:
    document = {
        "permissions": {"id-token": "write"},
        "jobs": {
            "deploy": {
                "environment": "acceptance",
                "steps": [
                    {"run": "azd deploy"},
                    {
                        "uses": (
                            "azure/login@"
                            "532459ea530d8321f2fb9bb10d1e0bcf23869a43"
                        ),
                        "with": {
                            "client-id": (
                                "${{ vars.AZURE_DEPLOYMENT_CLIENT_ID }}"
                            ),
                        },
                    },
                    {
                        "uses": (
                            "actions/upload-artifact@"
                            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                        ),
                        "with": {
                            "name": (
                                "foundry-optimization-deployment-result"
                            ),
                            "path": "deployment-result.json",
                        },
                    },
                ],
            },
        },
    }

    assert _uses_deployment_identity(document, "acceptance") is False


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
        DatasetDiscovery(
            "development",
            ("1", "2"),
            role="development",
        ),
    )
    assert inventory.evaluators == (
        EvaluatorDiscovery(
            "quality",
            "quality:3",
            needs_input=(
                "Foundry did not expose optimizer metric policy semantics."
            ),
        ),
    )
    assert inventory.app_insights == AppInsightsDiscovery(connected=True)
    assert client.closed is True
    assert credential.closed is True


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://evil.example/api/projects/demo",
        "https://example.services.ai.azure.com.evil/api/projects/demo",
        "http://example.services.ai.azure.com/api/projects/demo",
        "https://example.services.ai.azure.com/api/projects/demo?token=leak",
    ],
)
def test_sdk_inventory_rejects_unofficial_endpoint_before_credentials(
    tmp_path: Path,
    endpoint: str,
) -> None:
    credential_created = False
    client_created = False

    def credential_factory(tenant: str):
        nonlocal credential_created
        credential_created = True
        raise AssertionError("credential creation must not be reached")

    def client_factory(project_endpoint: str, credential):
        nonlocal client_created
        client_created = True
        raise AssertionError("client creation must not be reached")

    request = replace(_request(tmp_path), project_endpoint=endpoint)

    with pytest.raises(FoundryEndpointError):
        AzureSdkFoundryInventory(
            credential_factory=credential_factory,
            client_factory=client_factory,
        ).discover(request)

    assert credential_created is False
    assert client_created is False


def test_sdk_inventory_discovers_explicit_evaluator_metric_semantics(
    tmp_path: Path,
) -> None:
    class Credential:
        def close(self) -> None:
            return None

    class Client:
        agents = type("Agents", (), {
            "list": lambda self: [],
        })()
        deployments = type("Deployments", (), {
            "list": lambda self: [],
        })()
        datasets = type("Datasets", (), {"list": lambda self: []})()
        beta = type("Beta", (), {
            "evaluators": type("Evaluators", (), {
                "list": lambda self, **kwargs: [{
                    "name": "quality",
                    "version": "3",
                    "tags": {
                        "foundry-opt.metrics": (
                            '[{"name":"quality","direction":"maximize",'
                            '"threshold":0.8,"materiality":0.05,'
                            '"hard_guardrail":false}]'
                        )
                    },
                }],
            })(),
        })()
        connections = type("Connections", (), {
            "list": lambda self: [],
        })()

        def close(self) -> None:
            return None

    inventory = AzureSdkFoundryInventory(
        credential_factory=lambda tenant: Credential(),
        client_factory=lambda endpoint, credential: Client(),
    ).discover(_request(tmp_path))

    assert inventory.evaluators == (
        EvaluatorDiscovery(
            "quality",
            "quality:3",
            metrics=(
                MetricDiscovery(
                    name="quality",
                    direction="maximize",
                    threshold=0.8,
                    materiality=0.05,
                    hard_guardrail=False,
                ),
            ),
        ),
    )


def test_sdk_inventory_does_not_coerce_invalid_evaluator_semantics(
    tmp_path: Path,
) -> None:
    class Credential:
        def close(self) -> None:
            return None

    class Client:
        agents = type("Agents", (), {"list": lambda self: []})()
        deployments = type("Deployments", (), {"list": lambda self: []})()
        datasets = type("Datasets", (), {"list": lambda self: []})()
        beta = type("Beta", (), {
            "evaluators": type("Evaluators", (), {
                "list": lambda self, **kwargs: [{
                    "name": "quality",
                    "version": "3",
                    "tags": {
                        "foundry-opt.metrics": (
                            '[{"name":"quality","direction":"maximize",'
                            '"threshold":0.8,"materiality":0.05,'
                            '"hard_guardrail":"false"}]'
                        )
                    },
                }],
            })(),
        })()
        connections = type("Connections", (), {"list": lambda self: []})()

        def close(self) -> None:
            return None

    inventory = AzureSdkFoundryInventory(
        credential_factory=lambda tenant: Credential(),
        client_factory=lambda endpoint, credential: Client(),
    ).discover(_request(tmp_path))

    assert inventory.evaluators[0].metrics == ()
    assert "invalid" in (inventory.evaluators[0].needs_input or "")
