import json
from pathlib import Path

from typer.testing import CliRunner

import foundry_opt.cli as cli
from foundry_opt.auth.probe import (
    AuthProbeResult,
    AzurePrincipalProbe,
    EnvironmentKind,
    FoundryConnectivityProbe,
    OidcRequestVariablesProbe,
    RefreshReacquisitionProbe,
    TokenAcquisitionProbe,
)
from foundry_opt.cli import app


runner = CliRunner()


def test_help_identifies_the_optimizer() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Optimize Microsoft Foundry coding agents" in result.stdout


def test_version_reports_the_installed_cli_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "foundry-opt 0.1.0"


def test_auth_probe_emits_stable_json(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    expected = AuthProbeResult(
        scope="copilot-optimizer",
        environment_kind=EnvironmentKind.COPILOT_AGENT_POST_SETUP,
        oidc_request_variables=OidcRequestVariablesProbe(
            request_url_present=True,
            request_token_present=True,
        ),
        azure_principal=AzurePrincipalProbe(
            available=True,
            principal_type="service_principal",
            client_match=True,
            tenant_match=True,
            subscription_match=True,
        ),
        token_acquisition=(
            TokenAcquisitionProbe(resource="ai.azure.com", success=True),
            TokenAcquisitionProbe(
                resource="cognitiveservices.azure.com",
                success=True,
            ),
        ),
        foundry_connectivity=FoundryConnectivityProbe(
            configured=True,
            firewall_reachable=True,
            read_only_access_success=True,
        ),
        refresh_reacquisition=RefreshReacquisitionProbe(),
        direct_operations_eligible=True,
        errors=(),
    )

    class StubProbe:
        def run(self, request):
            assert request.repository_root == tmp_path
            assert request.scope == "copilot-optimizer"
            return expected

    monkeypatch.setattr(cli, "build_auth_probe", lambda: StubProbe())

    result = runner.invoke(
        app,
        ["auth", "probe", "--scope", "copilot-optimizer", "--json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == expected.to_dict()


def test_auth_probe_does_not_echo_an_invalid_scope() -> None:
    secret_scope = "eyJscopevalue.header.signature"

    result = runner.invoke(
        app,
        ["auth", "probe", "--scope", secret_scope, "--json"],
    )

    assert result.exit_code == 2
    assert "unsupported scope" in result.stderr
    assert secret_scope not in result.output
