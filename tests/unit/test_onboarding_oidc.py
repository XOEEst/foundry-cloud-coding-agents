from collections.abc import Sequence
from dataclasses import replace
import json
from pathlib import Path

import pytest

from foundry_opt.adapters.oidc import (
    CommandOidcVerifier,
    OidcVerificationError,
)
from foundry_opt.deployment import DEPLOYMENT_OIDC_CLIENT_ID
from foundry_opt.onboarding import OnboardingRequest, RepositoryDiscovery
from foundry_opt.preflight.interfaces import CommandResult


class FakeCommands:
    def __init__(
        self,
        *,
        federated_subject: str,
        use_default: bool = False,
        user_type: str = "user",
        user_name: str = "operator@example.com",
        accessible_subscription: str = "subscription",
        application_id: str = "client",
        deployment_federated_subject: str | None = None,
    ) -> None:
        self.invocations: list[tuple[str, ...]] = []
        self.responses = {
            (
                "gh",
                "api",
                "repos/octo-org/agents/actions/oidc/customization/sub",
            ): (
                f'{{"use_default":{str(use_default).lower()},'
                '"use_immutable_subject":true,'
                f'"include_claim_keys":'
                f'{json.dumps([] if use_default else ["repository_id"])},'
                '"sub_claim_prefix":"repo:octo-org@42/agents@123456"}'
            ),
            (
                "az",
                "account",
                "show",
                "--query",
                "{tenant:tenantId,subscription:id,userName:user.name,"
                "userType:user.type}",
                "-o",
                "json",
            ): (
                '{"tenant":"tenant","subscription":"subscription",'
                f'"userName":"{user_name}","userType":"{user_type}"}}'
            ),
            (
                "az",
                "rest",
                "--method",
                "get",
                "--url",
                "https://management.azure.com/subscriptions/subscription"
                "?api-version=2022-12-01",
                "--query",
                "subscriptionId",
                "-o",
                "tsv",
            ): accessible_subscription,
            (
                "az",
                "ad",
                "app",
                "show",
                "--id",
                "client",
                "--query",
                "{appId:appId,id:id}",
                "-o",
                "json",
            ): (
                f'{{"appId":"{application_id}",'
                '"id":"00000000-0000-0000-0000-000000000001"}'
            ),
            (
                "az",
                "ad",
                "app",
                "federated-credential",
                "list",
                "--id",
                "00000000-0000-0000-0000-000000000001",
                "-o",
                "json",
            ): (
                '[{"issuer":"https://token.actions.githubusercontent.com",'
                f'"subject":"{federated_subject}",'
                '"audiences":["api://AzureADTokenExchange"]}]'
            ),
            (
                "az",
                "ad",
                "app",
                "show",
                "--id",
                DEPLOYMENT_OIDC_CLIENT_ID,
                "--query",
                "{appId:appId,id:id}",
                "-o",
                "json",
            ): (
                f'{{"appId":"{DEPLOYMENT_OIDC_CLIENT_ID}",'
                '"id":"00000000-0000-0000-0000-000000000002"}'
            ),
            (
                "az",
                "ad",
                "app",
                "federated-credential",
                "list",
                "--id",
                "00000000-0000-0000-0000-000000000002",
                "-o",
                "json",
            ): (
                '[{"issuer":"https://token.actions.githubusercontent.com",'
                '"subject":"'
                f'{deployment_federated_subject or federated_subject}",'
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
        federated_subject=(
            "repo:octo-org@42/agents@123456:repository_id:123456"
        ),
    )

    result = CommandOidcVerifier(commands).verify(
        _request(tmp_path),
        _discovery(),
    )

    assert result.verified is True
    assert result.repository_id == "123456"
    assert result.subject == (
        "repo:octo-org@42/agents@123456:repository_id:123456"
    )
    assert all(
        "secret" not in argument.casefold()
        for invocation in commands.invocations
        for argument in invocation
    )
    assert any(
        invocation[:6]
        == (
            "az",
            "ad",
            "app",
            "show",
            "--id",
            DEPLOYMENT_OIDC_CLIENT_ID,
        )
        for invocation in commands.invocations
    )


def test_oidc_verifier_accepts_default_immutable_subject_for_human_operator(
    tmp_path: Path,
) -> None:
    commands = FakeCommands(
        federated_subject=(
            "repo:octo-org@42/agents@123456:environment:acceptance"
        ),
        use_default=True,
        user_type="user",
        user_name="operator@example.com",
    )

    result = CommandOidcVerifier(commands).verify(
        _request(tmp_path),
        _discovery(),
    )

    assert result.verified is True
    assert result.subject == (
        "repo:octo-org@42/agents@123456:environment:acceptance"
    )
    assert any(command[0:2] == ("az", "rest") for command in commands.invocations)
    assert (
        "az",
        "ad",
        "app",
        "show",
        "--id",
        "client",
        "--query",
        "{appId:appId,id:id}",
        "-o",
        "json",
    ) in commands.invocations
    assert (
        "az",
        "ad",
        "app",
        "federated-credential",
        "list",
        "--id",
        "00000000-0000-0000-0000-000000000001",
        "-o",
        "json",
    ) in commands.invocations


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


def test_oidc_verifier_rejects_operator_without_subscription_access(
    tmp_path: Path,
) -> None:
    verifier = CommandOidcVerifier(
        FakeCommands(
            federated_subject="repo:octo-org@42/agents@123456",
            accessible_subscription="",
        )
    )

    with pytest.raises(
        OidcVerificationError,
        match="cannot access the requested subscription",
    ):
        verifier.verify(_request(tmp_path), _discovery())


def test_oidc_verifier_rejects_a_different_target_application(
    tmp_path: Path,
) -> None:
    verifier = CommandOidcVerifier(
        FakeCommands(
            federated_subject="repo:octo-org@42/agents@123456",
            application_id="different-client",
        )
    )

    with pytest.raises(
        OidcVerificationError,
        match="target Entra application",
    ):
        verifier.verify(_request(tmp_path), _discovery())


def test_oidc_verifier_uses_selected_deployment_environment_subject(
    tmp_path: Path,
) -> None:
    optimizer_subject = (
        "repo:octo-org@42/agents@123456:environment:acceptance"
    )
    deployment_subject = (
        "repo:octo-org@42/agents@123456:environment:production"
    )
    request = replace(
        _request(tmp_path),
        set_github_variables=True,
        mirror_actions_environment="production",
    )
    verifier = CommandOidcVerifier(
        FakeCommands(
            federated_subject=optimizer_subject,
            deployment_federated_subject=deployment_subject,
            use_default=True,
        )
    )

    result = verifier.verify(request, _discovery())

    assert result.verified is True


def test_oidc_verifier_requires_distinct_optimizer_and_deployment_apps(
    tmp_path: Path,
) -> None:
    subject = (
        "repo:octo-org@42/agents@123456:repository_id:123456"
    )
    request = replace(
        _request(tmp_path),
        deployment_client_id="client",
    )

    with pytest.raises(OidcVerificationError, match="must be distinct"):
        CommandOidcVerifier(
            FakeCommands(federated_subject=subject)
        ).verify(request, _discovery())
