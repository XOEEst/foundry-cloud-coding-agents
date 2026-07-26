from collections.abc import Sequence
from pathlib import Path

import pytest

from foundry_opt.adapters.oidc import (
    CommandOidcVerifier,
    OidcVerificationError,
)
from foundry_opt.onboarding import OnboardingRequest, RepositoryDiscovery
from foundry_opt.preflight.interfaces import CommandResult


class FakeCommands:
    def __init__(self, *, federated_subject: str) -> None:
        self.invocations: list[tuple[str, ...]] = []
        self.responses = {
            (
                "gh",
                "api",
                "repos/octo-org/agents/actions/oidc/customization/sub",
            ): (
                '{"use_default":false,"use_immutable_subject":true,'
                '"include_claim_keys":["repository_id"],'
                '"sub_claim_prefix":"repo:octo-org@42/agents@123456"}'
            ),
            (
                "az",
                "account",
                "show",
                "--query",
                "{tenant:tenantId,subscription:id,client:user.name,"
                "userType:user.type}",
                "-o",
                "json",
            ): (
                '{"tenant":"tenant","subscription":"subscription",'
                '"client":"client","userType":"servicePrincipal"}'
            ),
            (
                "az",
                "ad",
                "app",
                "federated-credential",
                "list",
                "--id",
                "client",
                "-o",
                "json",
            ): (
                '[{"issuer":"https://token.actions.githubusercontent.com",'
                f'"subject":"{federated_subject}",'
                '"audiences":["api://AzureADTokenExchange"]}]'
            ),
        }

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
    ) -> CommandResult:
        command = tuple(arguments)
        self.invocations.append(command)
        return CommandResult(0, self.responses[command], "")


def _request(tmp_path: Path) -> OnboardingRequest:
    return OnboardingRequest(
        repository_root=tmp_path,
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


def _discovery() -> RepositoryDiscovery:
    return RepositoryDiscovery(
        repository="octo-org/agents",
        repository_id="123456",
        default_branch="main",
        current_branch="main",
        authenticated_login="octocat",
        viewer_permission="ADMIN",
        clean=True,
    )


def test_oidc_verifier_requires_exact_immutable_repository_id_trust(
    tmp_path: Path,
) -> None:
    commands = FakeCommands(
        federated_subject="repo:octo-org@42/agents@123456",
    )

    result = CommandOidcVerifier(commands).verify(
        _request(tmp_path),
        _discovery(),
    )

    assert result.verified is True
    assert result.repository_id == "123456"
    assert result.subject == "repo:octo-org@42/agents@123456"
    assert all(
        "secret" not in argument.casefold()
        for invocation in commands.invocations
        for argument in invocation
    )


def test_oidc_verifier_rejects_a_different_federated_subject(
    tmp_path: Path,
) -> None:
    verifier = CommandOidcVerifier(
        FakeCommands(federated_subject="repo:other@999/repository@888")
    )

    with pytest.raises(
        OidcVerificationError,
        match="exact repository-ID subject",
    ):
        verifier.verify(_request(tmp_path), _discovery())
