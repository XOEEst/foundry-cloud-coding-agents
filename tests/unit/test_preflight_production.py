from collections.abc import Sequence
from pathlib import Path

from foundry_opt.adapters.github import GitHubRepositoryMetadata
from foundry_opt.config.models import OptimizerConfig
from foundry_opt.preflight.interfaces import (
    CommandResult,
    GatewayResult,
)
from foundry_opt.preflight.models import CheckStatus, PreflightRequest
from foundry_opt.preflight.production import (
    AzureCredentialsCheck,
    build_production_preflight_runner,
)


class FakeEnvironment:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, name: str) -> str | None:
        return self.values.get(name)


class TrackingEnvironment(FakeEnvironment):
    def __init__(self, values: dict[str, str]) -> None:
        super().__init__(values)
        self.requested: list[str] = []

    def get(self, name: str) -> str | None:
        self.requested.append(name)
        return super().get(name)


class FakeCommandRunner:
    def __init__(
        self,
        responses: dict[tuple[str, ...], str] | None = None,
    ) -> None:
        self.responses = {
            ("git", "rev-parse", "--is-inside-work-tree"): "true\n",
            ("git", "remote", "get-url", "origin"): (
                "https://github.com/octo-org/optimizer.git\n"
            ),
            ("git", "branch", "--show-current"): "main\n",
            ("git", "status", "--porcelain", "--untracked-files=all"): "",
            **(responses or {}),
        }

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
    ) -> CommandResult:
        command = tuple(arguments)
        return CommandResult(
            exit_code=0,
            stdout=self.responses[command],
            stderr="",
        )


class FakeGitHubGateway:
    def verify_access(self, repository_root: Path) -> GatewayResult:
        return GatewayResult(summary="GitHub access verified")

    def repository_metadata(
        self,
        repository_root: Path,
    ) -> GitHubRepositoryMetadata:
        return GitHubRepositoryMetadata(
            repository="octo-org/optimizer",
            default_branch="main",
            viewer_permission="ADMIN",
        )


class FakeFoundryGateway:
    def __init__(self) -> None:
        self.project_endpoints: list[str] = []

    def verify_access(self, project_endpoint: str) -> GatewayResult:
        self.project_endpoints.append(project_endpoint)
        return GatewayResult(summary="Foundry project access verified")


def _config(authentication: str = "client_secret") -> OptimizerConfig:
    return OptimizerConfig.model_validate(
        {
            "schema_version": "1",
            "default_environment": "acceptance",
            "environments": {
                "acceptance": {
                    "authentication": authentication,
                    "project_endpoint": (
                        "https://example.services.ai.azure.com/api/projects/demo"
                    ),
                    "project_resource_id": "/subscriptions/sub/projects/demo",
                    "allowed_models": ["gpt-5.1"],
                    "deployment_workflow": {
                        "path": ".github/workflows/deploy.yml",
                        "trigger": "manual",
                    },
                }
            },
            "targets": {
                "support_agent": {
                    "environment": "acceptance",
                    "source_paths": ["agent"],
                    "edit_paths": ["agent"],
                    "entry_point": "agent/main.py",
                    "base_agent_version": "12",
                    "package": {"include": ["agent/**"]},
                    "datasets": {
                        "development": [
                            {"name": "dev", "version": "v1", "mode": "batch"}
                        ],
                        "validation": [
                            {"name": "held-out", "version": "v1", "mode": "batch"}
                        ],
                    },
                    "evaluators": [
                        {
                            "name": "quality",
                            "reference": "quality-evaluator",
                            "metrics": ["quality"],
                        }
                    ],
                    "validation_commands": ["uv run pytest -q"],
                    "metrics": {
                        "quality": {
                            "direction": "maximize",
                            "threshold": 0.8,
                            "materiality": 0.05,
                            "hard_guardrail": False,
                            "undefined_behavior": "fail",
                        }
                    },
                    "allowed_mutations": ["system_instructions"],
                }
            },
            "campaign": {
                "deadline_minutes": 50,
                "candidate_cutoff_minutes": 40,
                "max_changed_candidates": 3,
                "transient_retries": 1,
                "allowed_mutations": ["system_instructions"],
            },
        }
    )


def _request(
    tmp_path: Path,
    *,
    environment: str = "acceptance",
    target: str = "support_agent",
) -> PreflightRequest:
    return PreflightRequest(
        repository_root=tmp_path,
        config_path=Path(".github/foundry-optimizer.yaml"),
        environment=environment,
        target=target,
    )


def test_credentials_check_reports_names_without_values(tmp_path: Path) -> None:
    environment = FakeEnvironment(
        {
            "AZURE_TENANT_ID": "tenant-secret",
            "AZURE_CLIENT_ID": "",
            "AZURE_CLIENT_SECRET": "client-secret",
        }
    )

    result = AzureCredentialsCheck(
        environment,
        authentication_mode="client_secret",
        command_runner=FakeCommandRunner(),
    ).run(_request(tmp_path))

    assert result.status is CheckStatus.FAIL
    assert result.detail == "Missing: AZURE_CLIENT_ID"
    assert "tenant-secret" not in str(result)
    assert "client-secret" not in str(result)


def test_oidc_credentials_check_verifies_expected_azure_cli_identity(
    tmp_path: Path,
) -> None:
    environment = FakeEnvironment(
        {
            "AZURE_TENANT_ID": "tenant-value",
            "AZURE_CLIENT_ID": "client-value",
            "AZURE_SUBSCRIPTION_ID": "subscription-value",
        }
    )
    command_runner = FakeCommandRunner({
        (
            "az",
            "account",
            "show",
            "--query",
            "{tenant:tenantId,subscription:id,client:user.name,userType:user.type}",
            "-o",
            "json",
        ): (
            '{"tenant":"tenant-value","subscription":"subscription-value",'
            '"client":"client-value","userType":"servicePrincipal"}'
        )
    })

    result = AzureCredentialsCheck(
        environment,
        authentication_mode="oidc",
        command_runner=command_runner,
    ).run(_request(tmp_path))

    assert result.status is CheckStatus.PASS
    assert result.summary == "Azure OIDC session matches the configured identity"


def test_oidc_credentials_check_reports_missing_non_secret_identifiers(
    tmp_path: Path,
) -> None:
    result = AzureCredentialsCheck(
        FakeEnvironment({"AZURE_TENANT_ID": "tenant-value"}),
        authentication_mode="oidc",
        command_runner=FakeCommandRunner(),
    ).run(_request(tmp_path))

    assert result.status is CheckStatus.FAIL
    assert result.detail == (
        "Missing: AZURE_CLIENT_ID, AZURE_SUBSCRIPTION_ID"
    )
    assert "AZURE_CLIENT_SECRET" not in str(result)


def test_oidc_credentials_check_rejects_the_wrong_cli_identity(
    tmp_path: Path,
) -> None:
    command = (
        "az",
        "account",
        "show",
        "--query",
        "{tenant:tenantId,subscription:id,client:user.name,userType:user.type}",
        "-o",
        "json",
    )
    runner = FakeCommandRunner(
        {
            command: (
                '{"tenant":"other-tenant","subscription":"subscription-value",'
                '"client":"other-client","userType":"user"}'
            )
        }
    )
    result = AzureCredentialsCheck(
        FakeEnvironment(
            {
                "AZURE_TENANT_ID": "tenant-value",
                "AZURE_CLIENT_ID": "client-value",
                "AZURE_SUBSCRIPTION_ID": "subscription-value",
            }
        ),
        authentication_mode="oidc",
        command_runner=runner,
    ).run(_request(tmp_path))

    assert result.status is CheckStatus.FAIL
    assert result.detail == "Mismatched: AZURE_CLIENT_ID, AZURE_TENANT_ID"
    assert "tenant-value" not in str(result)
    assert "client-value" not in str(result)


def test_production_runner_rejects_a_target_missing_from_validated_config(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, target="missing_agent")

    report = build_production_preflight_runner(
        _config(),
        request,
        command_runner=FakeCommandRunner(),
        environment=FakeEnvironment({}),
        github_gateway=FakeGitHubGateway(),
        foundry_gateway=FakeFoundryGateway(),
        executable_finder=lambda executable: f"C:/{executable}.exe",
    ).run(request)

    assert report.passed is False
    assert report.results[0].check_id == "selection.target"
    assert report.results[0].status is CheckStatus.FAIL
    assert report.results[0].summary == "Selected optimization target was not found"
    assert report.results[0].detail == "Target: missing_agent"


def test_production_runner_rejects_target_environment_mismatch(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, environment="production")

    report = build_production_preflight_runner(
        _config(),
        request,
        command_runner=FakeCommandRunner(),
        environment=FakeEnvironment({}),
        github_gateway=FakeGitHubGateway(),
        foundry_gateway=FakeFoundryGateway(),
        executable_finder=lambda executable: f"C:/{executable}.exe",
    ).run(request)

    assert report.passed is False
    assert report.results[0].check_id == "selection.environment"
    assert report.results[0].status is CheckStatus.FAIL
    assert report.results[0].summary == (
        "Selected target does not use the requested environment"
    )
    assert report.results[0].detail == (
        "Target support_agent uses acceptance; requested production"
    )


def test_production_runner_assembles_checks_in_stable_order(
    tmp_path: Path,
) -> None:
    environment = FakeEnvironment(
        {
            "AZURE_TENANT_ID": "tenant-value",
            "AZURE_CLIENT_ID": "client-value",
            "AZURE_CLIENT_SECRET": "secret-value",
        }
    )
    foundry_gateway = FakeFoundryGateway()
    runner = build_production_preflight_runner(
        _config(),
        _request(tmp_path),
        command_runner=FakeCommandRunner(),
        environment=environment,
        github_gateway=FakeGitHubGateway(),
        foundry_gateway=foundry_gateway,
        executable_finder=lambda executable: f"C:/{executable}.exe",
    )

    report = runner.run(_request(tmp_path))

    assert [result.check_id for result in report.results] == [
        "runtime.python",
        "runtime.uv",
        "runtime.git",
        "runtime.gh",
        "runtime.az",
        "runtime.azd",
        "repository.git",
        "repository.github_remote",
        "repository.default_branch",
        "repository.worktree",
        "credentials.copilot_assignment_scope",
        "credentials.azure",
        "github.permission",
        "foundry.access",
    ]
    assert report.passed is True
    assert foundry_gateway.project_endpoints == [
        "https://example.services.ai.azure.com/api/projects/demo"
    ]


def test_oidc_production_runner_never_requests_client_secret(
    tmp_path: Path,
) -> None:
    command = (
        "az",
        "account",
        "show",
        "--query",
        "{tenant:tenantId,subscription:id,client:user.name,userType:user.type}",
        "-o",
        "json",
    )
    environment = TrackingEnvironment(
        {
            "AZURE_TENANT_ID": "tenant-value",
            "AZURE_CLIENT_ID": "client-value",
            "AZURE_SUBSCRIPTION_ID": "subscription-value",
        }
    )
    request = _request(tmp_path)
    report = build_production_preflight_runner(
        _config("oidc"),
        request,
        command_runner=FakeCommandRunner(
            {
                command: (
                    '{"tenant":"tenant-value",'
                    '"subscription":"subscription-value",'
                    '"client":"client-value",'
                    '"userType":"servicePrincipal"}'
                )
            }
        ),
        environment=environment,
        github_gateway=FakeGitHubGateway(),
        foundry_gateway=FakeFoundryGateway(),
        executable_finder=lambda executable: f"C:/{executable}.exe",
    ).run(request)

    assert report.passed is True
    assert "AZURE_CLIENT_SECRET" not in environment.requested
