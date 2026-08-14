from pathlib import Path

from typer.testing import CliRunner

import foundry_opt.cli as cli
from foundry_opt.onboarding import (
    AppInsightsDiscovery,
    ChangeStatus,
    DatasetDiscovery,
    DeployedModelDiscovery,
    DeploymentWorkflowDiscovery,
    DraftProbeResult,
    DraftPullRequestPublication,
    EvaluatorDiscovery,
    FoundryAgentDiscovery,
    MetricDiscovery,
    OidcTrustResult,
    OnboardingDependencies,
    OnboardingChange,
    PythonAgentCandidate,
    RepositoryDiscovery,
)


class Discovery:
    def discover(self, request):
        return RepositoryDiscovery(
            repository="octo-org/agents",
            repository_id="123",
            default_branch="main",
            current_branch="main",
            authenticated_login="octocat",
            viewer_permission="ADMIN",
            clean=True,
            python_agents=(
                PythonAgentCandidate(
                    "support-agent",
                    Path("agent"),
                    Path("agent/main.py"),
                ),
            ),
            validation_commands=("uv run pytest",),
            foundry_agents=(FoundryAgentDiscovery("support-agent", ("2",)),),
            deployed_models=(DeployedModelDiscovery("gpt-5.1"),),
            datasets=(
                DatasetDiscovery("dev", ("1",)),
                DatasetDiscovery("validation", ("1",)),
            ),
            evaluators=(
                EvaluatorDiscovery(
                    "quality",
                    "quality:1",
                    (
                        MetricDiscovery(
                            "quality",
                            "maximize",
                            0.8,
                            0.05,
                            False,
                        ),
                    ),
                ),
            ),
            app_insights=AppInsightsDiscovery(False),
            deployment_workflows=(
                DeploymentWorkflowDiscovery(
                    Path(".github/workflows/deploy.yml"),
                    "manual",
                ),
            ),
        )


class Oidc:
    def verify(self, request, discovery):
        return OidcTrustResult(
            subject="repo:octo-org@1/agents@123",
            repository_id="123",
            verified=True,
        )


class Probe:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def probe(self, request, agent, source):
        if self.fail:
            raise RuntimeError("source-bundle draft API unavailable")
        return DraftProbeResult(agent.name, "draft-cli-probe")

    def delete_probe(self, agent_name, version):
        return None


class Publisher:
    def publish(self, request, discovery, changes, draft_pull_request):
        return DraftPullRequestPublication(
            url="https://github.com/octo-org/agents/pull/42",
            branch="foundry-opt/onboarding-support-agent",
            commit_sha="abc123",
        )


class Variables:
    def configure(self, request, discovery):
        from foundry_opt.onboarding import (
            GitHubVariableChange,
            GitHubVariableChangeStatus,
            GitHubVariableScope,
        )

        return (
            GitHubVariableChange(
                name="AZURE_TENANT_ID",
                scope=GitHubVariableScope.AGENTS,
                status=GitHubVariableChangeStatus.CREATED,
            ),
        )


class TestChangeWriter:
    def prevalidate(self, repository_root, contents):
        return tuple(
            OnboardingChange(path, content, ChangeStatus.PLANNED)
            for path, content in contents.items()
        )

    def write(self, repository_root, contents):
        changes = []
        for path, content in contents.items():
            destination = repository_root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
            changes.append(
                OnboardingChange(path, content, ChangeStatus.CREATED)
            )
        return tuple(changes)


def _arguments() -> list[str]:
    return [
        "init",
        "--environment",
        "acceptance",
        "--target",
        "support-agent",
        "--project-endpoint",
        "https://example.services.ai.azure.com/api/projects/demo",
        "--project-resource-id",
        "/subscriptions/sub/projects/demo",
        "--tenant-id",
        "tenant",
        "--client-id",
        "client",
        "--subscription-id",
        "subscription",
        "--product-install",
        "foundry-cloud-coding-agent==0.1.0",
    ]


def test_init_cli_returns_zero_and_describes_draft_pr(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        cli,
        "build_onboarding_dependencies",
        lambda: OnboardingDependencies(
            Discovery(),
            Oidc(),
            Probe(),
            publisher=Publisher(),
            change_writer=TestChangeWriter(),
        ),
    )

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.app, _arguments())

    assert result.exit_code == 0
    assert "Onboarding ready" in result.stdout
    assert "Draft PR: Configure Foundry optimizer onboarding" in result.stdout
    assert "https://github.com/octo-org/agents/pull/42" in result.stdout
    assert ".github/foundry-optimizer.yaml" in result.stdout
    assert ".github/workflows/foundry-optimization-operations.yml" in (
        result.stdout
    )
    assert ".github/workflows/foundry-optimization-workspace.yml" in (
        result.stdout
    )
    assert "COPILOT_ASSIGNMENT_TOKEN" in result.stdout
    assert "foundry-opt init cannot create Actions secrets" in result.stdout
    assert "installation token" in result.stdout
    assert "invocation and verified assignment-comment cleanup only" in (
        result.stdout
    )
    assert "github-actions[bot]" in result.stdout
    assert "optimizer AZURE_CLIENT_ID" in result.stdout
    assert "Foundry operations workflow" in result.stdout


def test_init_cli_can_create_agents_variables(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        cli,
        "build_onboarding_dependencies",
        lambda: OnboardingDependencies(
            Discovery(),
            Oidc(),
            Probe(),
            publisher=Publisher(),
            change_writer=TestChangeWriter(),
            variables=Variables(),
        ),
    )

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli.app,
        [*_arguments(), "--set-github-variables"],
    )

    assert result.exit_code == 0
    assert (
        "GitHub variable: [created] agents/AZURE_TENANT_ID"
        in result.stdout
    )
    assert "tenant" not in result.stdout


def test_init_cli_returns_one_when_draft_probe_is_unavailable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        cli,
        "build_onboarding_dependencies",
        lambda: OnboardingDependencies(
            Discovery(),
            Oidc(),
            Probe(fail=True),
            change_writer=TestChangeWriter(),
        ),
    )

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.app, _arguments())

    assert result.exit_code == 1
    assert "Onboarding blocked" in result.stdout
    assert "source-bundle draft API unavailable" in result.stdout
    assert "planned" in result.stdout


def test_init_cli_does_not_offer_secret_or_certificate_onboarding() -> None:
    result = CliRunner().invoke(cli.app, ["init", "--help"])

    assert result.exit_code == 0
    assert "--client-secret" not in result.stdout
    assert "--certificate" not in result.stdout


def test_init_cli_requires_set_flag_for_variable_updates() -> None:
    result = CliRunner().invoke(
        cli.app,
        [*_arguments(), "--update-github-variables"],
    )

    assert result.exit_code == 2
    assert "require set_github_variables" in result.stderr
