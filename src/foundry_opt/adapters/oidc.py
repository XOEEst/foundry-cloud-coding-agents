from __future__ import annotations

import json
import re
from typing import Any

from foundry_opt.onboarding.models import (
    OidcTrustResult,
    OnboardingRequest,
    RepositoryDiscovery,
)
from foundry_opt.preflight.interfaces import CommandRunner


class OidcVerificationError(RuntimeError):
    pass


class CommandOidcVerifier:
    def __init__(self, command_runner: CommandRunner) -> None:
        self._commands = command_runner

    def verify(
        self,
        request: OnboardingRequest,
        discovery: RepositoryDiscovery,
    ) -> OidcTrustResult:
        if request.client_id == request.deployment_client_id:
            raise OidcVerificationError(
                "Optimizer and deployment Entra applications must be distinct."
            )
        customization = self._json(
            (
                "gh",
                "api",
                f"repos/{discovery.repository}/actions/oidc/customization/sub",
            ),
            request,
        )
        if not isinstance(customization, dict):
            raise OidcVerificationError(
                "GitHub returned an invalid OIDC subject configuration."
            )
        use_default = customization.get("use_default")
        claim_keys = customization.get("include_claim_keys", ())
        custom_repository_id = (
            use_default is False
            and claim_keys == ["repository_id"]
        )
        if (
            use_default not in {True, False}
            or customization.get("use_immutable_subject") is not True
            or (use_default is False and not custom_repository_id)
        ):
            raise OidcVerificationError(
                "GitHub is not configured for an immutable repository-ID subject."
            )
        subject_prefix = customization.get("sub_claim_prefix")
        owner, repository = discovery.repository.split("/", 1)
        expected_subject = re.compile(
            rf"^repo:{re.escape(owner)}@\d+/"
            rf"{re.escape(repository)}@{re.escape(discovery.repository_id)}$",
            re.IGNORECASE,
        )
        if (
            not isinstance(subject_prefix, str)
            or not expected_subject.fullmatch(subject_prefix)
        ):
            raise OidcVerificationError(
                "GitHub returned an invalid repository-ID subject."
            )
        subject = (
            f"{subject_prefix}:environment:"
            f"{request.environment_name.replace(':', '%3A')}"
            if use_default
            else f"{subject_prefix}:repository_id:{discovery.repository_id}"
        )
        deployment_environment = (
            request.mirror_actions_environment
            or request.environment_name
        )
        deployment_subject = (
            f"{subject_prefix}:environment:"
            f"{deployment_environment.replace(':', '%3A')}"
            if use_default
            else subject
        )

        account = self._json(
            (
                "az",
                "account",
                "show",
                "--query",
                "{tenant:tenantId,subscription:id,userName:user.name,"
                "userType:user.type}",
                "-o",
                "json",
            ),
            request,
        )
        if (
            not isinstance(account, dict)
            or any(
                account.get(key) != expected
                for key, expected in (
                    ("tenant", request.tenant_id),
                    ("subscription", request.subscription_id),
                )
            )
            or not isinstance(account.get("userName"), str)
            or not account["userName"]
            or not isinstance(account.get("userType"), str)
            or not account["userType"]
        ):
            raise OidcVerificationError(
                "The Azure CLI operator is not in the requested tenant and "
                "subscription."
            )
        accessible_subscription = self._text(
            (
                "az",
                "rest",
                "--method",
                "get",
                "--url",
                "https://management.azure.com/subscriptions/"
                f"{request.subscription_id}?api-version=2022-12-01",
                "--query",
                "subscriptionId",
                "-o",
                "tsv",
            ),
            request,
        )
        if accessible_subscription.strip() != request.subscription_id:
            raise OidcVerificationError(
                "The Azure CLI operator cannot access the requested subscription."
            )

        self._verify_application(
            request,
            request.client_id,
            subject,
            description="target",
        )
        self._verify_application(
            request,
            request.deployment_client_id,
            deployment_subject,
            description="deployment",
        )

        return OidcTrustResult(
            subject=subject,
            repository_id=discovery.repository_id,
            verified=True,
            detail=(
                "GitHub immutable subject, Azure operator access, optimizer "
                "and deployment Entra applications, and exact federated "
                "credentials are verified."
            ),
        )

    def _verify_application(
        self,
        request: OnboardingRequest,
        client_id: str,
        subject: str,
        *,
        description: str,
    ) -> None:
        application = self._json(
            (
                "az",
                "ad",
                "app",
                "show",
                "--id",
                client_id,
                "--query",
                "{appId:appId,id:id}",
                "-o",
                "json",
            ),
            request,
        )
        if (
            not isinstance(application, dict)
            or application.get("appId") != client_id
            or not application.get("id")
        ):
            raise OidcVerificationError(
                f"The {description} Entra application could not be verified."
            )
        application_object_id = application["id"]
        if not isinstance(application_object_id, str):
            raise OidcVerificationError(
                f"The {description} Entra application object ID is invalid."
            )

        credentials = self._json(
            (
                "az",
                "ad",
                "app",
                "federated-credential",
                "list",
                "--id",
                application_object_id,
                "-o",
                "json",
            ),
            request,
        )
        if not isinstance(credentials, list) or not any(
            _is_exact_trust(credential, subject)
            for credential in credentials
        ):
            raise OidcVerificationError(
                f"The {description} Entra application lacks the exact "
                "repository-ID subject trust."
            )

    def _text(
        self,
        arguments: tuple[str, ...],
        request: OnboardingRequest,
    ) -> str:
        try:
            return self._commands.run(
                arguments,
                cwd=request.repository_root,
            ).stdout
        except Exception as error:
            raise OidcVerificationError(
                f"{arguments[0]} OIDC verification failed."
            ) from error

    def _json(
        self,
        arguments: tuple[str, ...],
        request: OnboardingRequest,
    ) -> Any:
        try:
            output = self._commands.run(
                arguments,
                cwd=request.repository_root,
            ).stdout
            return json.loads(output)
        except Exception as error:
            raise OidcVerificationError(
                f"{arguments[0]} OIDC verification failed."
            ) from error


def _is_exact_trust(value: Any, subject: str) -> bool:
    return (
        isinstance(value, dict)
        and value.get("issuer")
        == "https://token.actions.githubusercontent.com"
        and value.get("subject") == subject
        and "api://AzureADTokenExchange" in value.get("audiences", ())
    )
