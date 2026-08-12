from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import yaml

from foundry_opt.auth.probe import (
    AuthProbeRequest,
    EnvironmentKind,
    OidcProbe,
)
from foundry_opt.preflight.interfaces import CommandResult, GatewayResult


class FakeEnvironment:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, name: str) -> str | None:
        return self.values.get(name)


class FakeCommands:
    def __init__(self, account: dict[str, str] | Exception) -> None:
        self.account = account
        self.invocations: list[tuple[str, ...]] = []

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
        **kwargs,
    ) -> CommandResult:
        del cwd, kwargs
        self.invocations.append(tuple(arguments))
        if isinstance(self.account, Exception):
            raise self.account
        return CommandResult(0, json.dumps(self.account), "")


class FakeCredential:
    def __init__(self, failures: set[str] | None = None) -> None:
        self.failures = failures or set()
        self.scopes: list[str] = []
        self.closed = False

    def get_token(self, scope: str):
        self.scopes.append(scope)
        if scope in self.failures:
            raise RuntimeError("token=eyJsecret.header.signature")
        return object()

    def close(self) -> None:
        self.closed = True


class FakeCredentialProvider:
    def __init__(self, credential: FakeCredential | Exception) -> None:
        self.credential = credential

    def create(self):
        if isinstance(self.credential, Exception):
            raise self.credential
        return self.credential


class FakeFoundryGateway:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.endpoints: list[str] = []

    def verify_access(self, project_endpoint: str) -> GatewayResult:
        self.endpoints.append(project_endpoint)
        if self.error is not None:
            raise self.error
        return GatewayResult(
            summary="Foundry project access verified",
            detail="Read-only agent enumeration succeeded.",
        )


def _write_config(root: Path) -> None:
    path = root / ".github" / "foundry-optimizer.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1",
                "default_environment": "acceptance",
                "environments": {
                    "acceptance": {
                        "authentication": "oidc",
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
                    "agent": {
                        "environment": "acceptance",
                        "source_paths": ["agent"],
                        "edit_paths": ["agent"],
                        "entry_point": "agent/main.py",
                        "base_agent_version": "1",
                        "package": {"include": ["agent/**"]},
                        "datasets": {
                            "development": [
                                {"name": "dev", "version": "1", "mode": "batch"}
                            ],
                            "validation": [
                                {"name": "val", "version": "1", "mode": "batch"}
                            ],
                        },
                        "evaluators": [
                            {
                                "name": "quality",
                                "reference": "quality",
                                "metrics": ["quality"],
                            }
                        ],
                        "validation_commands": ["uv run pytest"],
                        "metrics": {
                            "quality": {
                                "direction": "maximize",
                                "threshold": 0.8,
                                "materiality": 0.01,
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
                    "max_changed_candidates": 1,
                    "transient_retries": 0,
                    "allowed_mutations": ["system_instructions"],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _post_setup_environment() -> FakeEnvironment:
    return FakeEnvironment(
        {
            "GITHUB_ACTIONS": "true",
            "COPILOT_AGENT_SOURCE_ENVIRONMENT": "production",
            "COPILOT_AGENT_START_TIME_SEC": "1785872107",
            "COPILOT_AGENT_TIMEOUT_MIN": "59",
            "COPILOT_AGENT_SESSION_ID": "session-1234567890",
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://example.invalid/oidc",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-secret",
            "AZURE_TENANT_ID": "tenant",
            "AZURE_CLIENT_ID": "client",
            "AZURE_SUBSCRIPTION_ID": "subscription",
        }
    )


def test_probe_reports_post_setup_direct_operation_eligibility(
    tmp_path: Path,
) -> None:
    _write_config(tmp_path)
    credential = FakeCredential()
    foundry = FakeFoundryGateway()
    probe = OidcProbe(
        environment=_post_setup_environment(),
        command_runner=FakeCommands(
            {
                "tenant": "tenant",
                "subscription": "subscription",
                "client": "client",
                "userType": "servicePrincipal",
            }
        ),
        credential_provider=FakeCredentialProvider(credential),
        foundry_gateway=foundry,
    )

    result = probe.run(
        AuthProbeRequest(
            repository_root=tmp_path,
            scope="copilot-optimizer",
        )
    )

    assert result.environment_kind is EnvironmentKind.COPILOT_AGENT_POST_SETUP
    assert result.oidc_request_variables.present is True
    assert result.azure_principal.principal_type == "service_principal"
    assert result.azure_principal.client_match is True
    assert result.azure_principal.tenant_match is True
    assert result.azure_principal.subscription_match is True
    assert [item.success for item in result.token_acquisition] == [True, True]
    assert credential.scopes == [
        "https://ai.azure.com/.default",
        "https://cognitiveservices.azure.com/.default",
    ]
    assert credential.closed is True
    assert result.foundry_connectivity.configured is True
    assert result.foundry_connectivity.firewall_reachable is True
    assert result.foundry_connectivity.read_only_access_success is True
    assert foundry.endpoints == [
        "https://example.services.ai.azure.com/api/projects/demo"
    ]
    assert result.refresh_reacquisition.status == "unknown"
    assert (
        result.refresh_reacquisition.reason
        == "requires_delayed_live_acceptance_probe"
    )
    assert result.direct_operations_eligible is True
    assert result.exit_code == 0


def test_probe_distinguishes_setup_time_actions_and_fails_closed(
    tmp_path: Path,
) -> None:
    _write_config(tmp_path)
    environment = _post_setup_environment()
    for name in (
        "COPILOT_AGENT_SOURCE_ENVIRONMENT",
        "COPILOT_AGENT_START_TIME_SEC",
        "COPILOT_AGENT_TIMEOUT_MIN",
        "COPILOT_AGENT_SESSION_ID",
    ):
        environment.values.pop(name)
    result = OidcProbe(
        environment=environment,
        command_runner=FakeCommands(
            {
                "tenant": "tenant",
                "subscription": "subscription",
                "client": "client",
                "userType": "servicePrincipal",
            }
        ),
        credential_provider=FakeCredentialProvider(FakeCredential()),
        foundry_gateway=FakeFoundryGateway(),
    ).run(
        AuthProbeRequest(
            repository_root=tmp_path,
            scope="copilot-optimizer",
        )
    )

    assert result.environment_kind is EnvironmentKind.ACTIONS_SETUP
    assert result.direct_operations_eligible is False
    assert result.exit_code == 1


def test_probe_rejects_spoofed_post_setup_markers(tmp_path: Path) -> None:
    _write_config(tmp_path)
    environment = _post_setup_environment()
    environment.values["COPILOT_AGENT_SOURCE_ENVIRONMENT"] = "staging"

    result = OidcProbe(
        environment=environment,
        command_runner=FakeCommands(
            {
                "tenant": "tenant",
                "subscription": "subscription",
                "client": "client",
                "userType": "servicePrincipal",
            }
        ),
        credential_provider=FakeCredentialProvider(FakeCredential()),
        foundry_gateway=FakeFoundryGateway(),
    ).run(
        AuthProbeRequest(
            repository_root=tmp_path,
            scope="copilot-optimizer",
        )
    )

    assert result.environment_kind is EnvironmentKind.UNKNOWN
    assert result.direct_operations_eligible is False


def test_probe_never_exposes_request_tokens_or_credential_errors(
    tmp_path: Path,
) -> None:
    environment = _post_setup_environment()
    environment.values["ACTIONS_ID_TOKEN_REQUEST_TOKEN"] = (
        "eyJrequesttoken.header.signature"
    )
    result = OidcProbe(
        environment=environment,
        command_runner=FakeCommands(RuntimeError("password=cache-secret")),
        credential_provider=FakeCredentialProvider(
            RuntimeError("Authorization: Bearer credential-cache-secret")
        ),
        foundry_gateway=FakeFoundryGateway(),
    ).run(
        AuthProbeRequest(
            repository_root=tmp_path,
            scope="copilot-optimizer",
        )
    )

    document = json.dumps(result.to_dict(), sort_keys=True)
    assert "eyJrequesttoken" not in document
    assert "cache-secret" not in document
    assert "credential-cache-secret" not in document
    assert result.oidc_request_variables.present is True
    assert result.azure_principal.available is False
    assert result.direct_operations_eligible is False
    assert all(error.message for error in result.errors)


def test_probe_requires_configured_read_only_foundry_connectivity(
    tmp_path: Path,
) -> None:
    result = OidcProbe(
        environment=_post_setup_environment(),
        command_runner=FakeCommands(
            {
                "tenant": "tenant",
                "subscription": "subscription",
                "client": "client",
                "userType": "servicePrincipal",
            }
        ),
        credential_provider=FakeCredentialProvider(FakeCredential()),
        foundry_gateway=FakeFoundryGateway(),
    ).run(
        AuthProbeRequest(
            repository_root=tmp_path,
            scope="copilot-optimizer",
        )
    )

    assert result.foundry_connectivity.configured is False
    assert result.foundry_connectivity.read_only_access_success is None
    assert result.direct_operations_eligible is False
