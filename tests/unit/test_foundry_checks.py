from pathlib import Path
from textwrap import dedent

import pytest

from foundry_opt.adapters.foundry import (
    FoundryAuthenticationError,
    FoundryAuthorizationError,
    FoundryEndpointError,
    FoundryMissingCredentialsError,
    FoundryServiceError,
    FoundryThrottledError,
    FoundryTransportError,
    FoundryUnexpectedSdkError,
)
from foundry_opt.preflight.foundry_checks import FoundryAccessCheck
from foundry_opt.preflight.interfaces import GatewayResult
from foundry_opt.preflight.models import CheckStatus, PreflightRequest


class FakeFoundryGateway:
    def __init__(
        self,
        *,
        result: GatewayResult | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.result = result
        self.failure = failure
        self.endpoints: list[str] = []

    def verify_access(self, project_endpoint: str) -> GatewayResult:
        self.endpoints.append(project_endpoint)
        if self.failure is not None:
            raise self.failure
        assert self.result is not None
        return self.result


def _write_config(root: Path, *, include_secret_marker: bool = False) -> Path:
    config_path = root / ".github" / "foundry-optimizer.yaml"
    config_path.parent.mkdir(parents=True)
    suffix = "\nsecret: secret-value" if include_secret_marker else ""
    config_path.write_text(
        dedent(
            f"""
            schema_version: "1"
            default_environment: development
            environments:
              development:
                project_endpoint: https://dev.services.ai.azure.com/api/projects/dev
                project_resource_id: /subscriptions/sub/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/dev/projects/dev
                allowed_models: [gpt-5.1]
                deployment_workflow:
                  path: .github/workflows/deploy.yml
                  trigger: manual
              acceptance:
                project_endpoint: https://acceptance.services.ai.azure.com/api/projects/acceptance
                project_resource_id: /subscriptions/sub/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acceptance/projects/acceptance
                allowed_models: [gpt-5.1]
                deployment_workflow:
                  path: .github/workflows/deploy.yml
                  trigger: manual
            targets:
              support_agent:
                environment: acceptance
                source_paths: [agent]
                edit_paths: [agent]
                entry_point: agent/main.py
                base_agent_version: "12"
                package:
                  include: ["agent/**"]
                datasets:
                  development:
                    - name: dev
                      version: v1
                      mode: batch
                  validation:
                    - name: held-out
                      version: v1
                      mode: batch
                evaluators:
                  - name: quality
                    reference: quality-evaluator
                    metrics: [quality]
                validation_commands: ["uv run pytest -q"]
                metrics:
                  quality:
                    direction: maximize
                    threshold: 0.8
                    materiality: 0.05
                    hard_guardrail: false
                    undefined_behavior: fail
                allowed_mutations: [system_instructions]
            campaign:
              deadline_minutes: 50
              candidate_cutoff_minutes: 40
              max_changed_candidates: 3
              transient_retries: 1
              stale_after_hours: 2
              evidence_path: .foundry-optimizer/campaigns
              allowed_mutations: [system_instructions]
            {suffix}
            """
        ),
        encoding="utf-8",
    )
    return config_path


def _request(root: Path, environment: str = "acceptance") -> PreflightRequest:
    return PreflightRequest(
        repository_root=root,
        config_path=Path(".github/foundry-optimizer.yaml"),
        environment=environment,
        target="support_agent",
    )


def test_check_loads_requested_environment_and_returns_gateway_success(
    tmp_path: Path,
) -> None:
    _write_config(tmp_path)
    gateway = FakeFoundryGateway(
        result=GatewayResult(
            summary="Foundry project access verified",
            detail="Read-only agent enumeration succeeded.",
        )
    )

    result = FoundryAccessCheck(gateway).run(_request(tmp_path))

    assert gateway.endpoints == [
        "https://acceptance.services.ai.azure.com/api/projects/acceptance"
    ]
    assert result.check_id == "foundry.access"
    assert result.status is CheckStatus.PASS
    assert result.summary == "Foundry project access verified"
    assert result.detail == "Read-only agent enumeration succeeded."
    assert result.remediation is None


@pytest.mark.parametrize(
    ("failure", "summary", "remediation_fragment"),
    [
        (
            FoundryMissingCredentialsError(("AZURE_CLIENT_SECRET",)),
            "Configured Azure authentication is incomplete",
            "authentication setup",
        ),
        (
            FoundryAuthenticationError(),
            "Foundry authentication failed",
            "Azure identity",
        ),
        (
            FoundryAuthorizationError(),
            "Foundry authorization failed",
            "Foundry User",
        ),
        (
            FoundryEndpointError(),
            "Foundry project endpoint is invalid or unreachable",
            "project_endpoint",
        ),
        (
            FoundryThrottledError(),
            "Foundry access check was throttled",
            "retry",
        ),
        (
            FoundryTransportError(),
            "Could not connect to the Foundry service",
            "network",
        ),
        (
            FoundryServiceError(),
            "Foundry service could not complete the access check",
            "service health",
        ),
        (
            FoundryUnexpectedSdkError(),
            "Unexpected Foundry SDK failure",
            "SDK",
        ),
    ],
)
def test_check_maps_typed_gateway_failures_to_actionable_results(
    tmp_path: Path,
    failure: Exception,
    summary: str,
    remediation_fragment: str,
) -> None:
    _write_config(tmp_path)
    gateway = FakeFoundryGateway(failure=failure)

    result = FoundryAccessCheck(gateway).run(_request(tmp_path))

    assert result.status is CheckStatus.FAIL
    assert result.summary == summary
    assert result.detail is None
    assert result.remediation is not None
    assert remediation_fragment.casefold() in result.remediation.casefold()


def test_check_reports_unknown_requested_environment_without_calling_gateway(
    tmp_path: Path,
) -> None:
    _write_config(tmp_path)
    gateway = FakeFoundryGateway(
        result=GatewayResult(summary="must not be returned")
    )

    result = FoundryAccessCheck(gateway).run(_request(tmp_path, "production"))

    assert gateway.endpoints == []
    assert result.status is CheckStatus.FAIL
    assert result.summary == "Configured Foundry environment was not found"
    assert result.remediation == (
        "Choose an environment defined in the optimizer configuration."
    )


def test_check_does_not_expose_configuration_failure_details(tmp_path: Path) -> None:
    _write_config(tmp_path, include_secret_marker=True)
    gateway = FakeFoundryGateway(
        result=GatewayResult(summary="must not be returned")
    )

    result = FoundryAccessCheck(gateway).run(_request(tmp_path))

    combined = " ".join(
        value
        for value in (result.summary, result.detail, result.remediation)
        if value is not None
    )
    assert gateway.endpoints == []
    assert result.status is CheckStatus.FAIL
    assert result.summary == "Foundry configuration could not be loaded"
    assert "secret-value" not in combined
