from __future__ import annotations

import json
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
        customization = self._json(
            (
                "gh",
                "api",
                f"repos/{discovery.repository}/actions/oidc/customization/sub",
            ),
            request,
        )
        if (
            not isinstance(customization, dict)
            or customization.get("use_default") is not False
            or customization.get("use_immutable_subject") is not True
            or "repository_id"
            not in customization.get("include_claim_keys", ())
        ):
            raise OidcVerificationError(
                "GitHub is not configured for an immutable repository-ID subject."
            )
        subject = customization.get("sub_claim_prefix")
        if (
            not isinstance(subject, str)
            or not subject
            or not subject.endswith(f"@{discovery.repository_id}")
        ):
            raise OidcVerificationError(
                "GitHub returned an invalid repository-ID subject."
            )

        account = self._json(
            (
                "az",
                "account",
                "show",
                "--query",
                "{tenant:tenantId,subscription:id,client:user.name,"
                "userType:user.type}",
                "-o",
                "json",
            ),
            request,
        )
        if not isinstance(account, dict) or any(
            account.get(key) != expected
            for key, expected in (
                ("tenant", request.tenant_id),
                ("subscription", request.subscription_id),
                ("client", request.client_id),
                ("userType", "servicePrincipal"),
            )
        ):
            raise OidcVerificationError(
                "The Azure CLI session does not match the requested OIDC identity."
            )

        credentials = self._json(
            (
                "az",
                "ad",
                "app",
                "federated-credential",
                "list",
                "--id",
                request.client_id,
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
                "The Entra application lacks the exact repository-ID subject trust."
            )

        return OidcTrustResult(
            subject=subject,
            repository_id=discovery.repository_id,
            verified=True,
            detail=(
                "GitHub immutable subject, Azure CLI identity, and exact Entra "
                "federated credential are verified."
            ),
        )

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
