from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
import json
from pathlib import Path
import re
from typing import Any, Protocol

from foundry_opt.adapters.commands import SubprocessCommandRunner
from foundry_opt.adapters.environment import OsEnvironmentReader
from foundry_opt.adapters.foundry import (
    AzureCliCredentialProvider,
    FoundryAccessError,
    FoundryAuthenticationError,
    FoundryAuthorizationError,
    FoundryEndpointError,
    FoundryGateway,
    FoundryMissingCredentialsError,
    FoundryServiceError,
    FoundryThrottledError,
    FoundryTransportError,
    FoundryUnexpectedSdkError,
)
from foundry_opt.config import ConfigLoadError, load_config
from foundry_opt.config.models import AuthenticationMode
from foundry_opt.preflight.interfaces import (
    CommandRunner,
    EnvironmentReader,
    FoundryGateway as FoundryGatewayProtocol,
)
from foundry_opt.preflight.redaction import redact


AUTH_PROBE_SCOPE = "copilot-optimizer"
_CONFIG_PATH = Path(".github/foundry-optimizer.yaml")
_OIDC_REQUEST_NAMES = (
    "ACTIONS_ID_TOKEN_REQUEST_URL",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
)
_AZURE_IDENTIFIER_NAMES = (
    "AZURE_TENANT_ID",
    "AZURE_CLIENT_ID",
    "AZURE_SUBSCRIPTION_ID",
)
_COPILOT_AGENT_MARKERS = (
    "COPILOT_AGENT_SOURCE_ENVIRONMENT",
    "COPILOT_AGENT_START_TIME_SEC",
    "COPILOT_AGENT_TIMEOUT_MIN",
    "COPILOT_AGENT_SESSION_ID",
)
_COPILOT_AGENT_SESSION_ID = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$"
)
_TOKEN_SCOPES = (
    ("ai.azure.com", "https://ai.azure.com/.default"),
    (
        "cognitiveservices.azure.com",
        "https://cognitiveservices.azure.com/.default",
    ),
)


class TokenCredential(Protocol):
    def get_token(self, *scopes: str, **kwargs: Any) -> Any: ...


class CredentialProvider(Protocol):
    def create(self) -> TokenCredential: ...


class EnvironmentKind(StrEnum):
    ACTIONS_SETUP = "actions_setup"
    COPILOT_AGENT_POST_SETUP = "copilot_agent_post_setup"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AuthProbeRequest:
    repository_root: Path
    scope: str

    def __post_init__(self) -> None:
        if self.scope != AUTH_PROBE_SCOPE:
            raise ValueError(f"unsupported auth probe scope: {self.scope}")


@dataclass(frozen=True)
class ProbeError:
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": redact(self.message) or "Authentication probe failed.",
        }


@dataclass(frozen=True)
class OidcRequestVariablesProbe:
    request_url_present: bool
    request_token_present: bool

    @property
    def present(self) -> bool:
        return self.request_url_present and self.request_token_present

    def to_dict(self) -> dict[str, bool]:
        return {
            "present": self.present,
            "request_token_present": self.request_token_present,
            "request_url_present": self.request_url_present,
        }


@dataclass(frozen=True)
class AzurePrincipalProbe:
    available: bool
    principal_type: str
    client_match: bool
    tenant_match: bool = False
    subscription_match: bool = False

    def to_dict(self) -> dict[str, bool | str]:
        return {
            "available": self.available,
            "client_match": self.client_match,
            "principal_type": self.principal_type,
            "subscription_match": self.subscription_match,
            "tenant_match": self.tenant_match,
        }


@dataclass(frozen=True)
class TokenAcquisitionProbe:
    resource: str
    success: bool
    attempted: bool = True

    def to_dict(self) -> dict[str, bool | str]:
        return {
            "attempted": self.attempted,
            "resource": self.resource,
            "success": self.success,
        }


@dataclass(frozen=True)
class FoundryConnectivityProbe:
    configured: bool
    firewall_reachable: bool | None
    read_only_access_success: bool | None
    attempted: bool = True
    configuration_valid: bool = True
    authentication_mode: str | None = "oidc"

    def to_dict(self) -> dict[str, bool | str | None]:
        return {
            "attempted": self.attempted,
            "authentication_mode": self.authentication_mode,
            "configuration_valid": self.configuration_valid,
            "configured": self.configured,
            "firewall_reachable": self.firewall_reachable,
            "read_only_access_success": self.read_only_access_success,
        }


@dataclass(frozen=True)
class RefreshReacquisitionProbe:
    status: str = "unknown"
    reason: str = "requires_delayed_live_acceptance_probe"

    def to_dict(self) -> dict[str, str]:
        return {"reason": self.reason, "status": self.status}


@dataclass(frozen=True)
class AuthProbeResult:
    scope: str
    environment_kind: EnvironmentKind
    oidc_request_variables: OidcRequestVariablesProbe
    azure_principal: AzurePrincipalProbe
    token_acquisition: tuple[TokenAcquisitionProbe, ...]
    foundry_connectivity: FoundryConnectivityProbe
    refresh_reacquisition: RefreshReacquisitionProbe
    direct_operations_eligible: bool
    errors: tuple[ProbeError, ...]

    @property
    def exit_code(self) -> int:
        return 0 if self.direct_operations_eligible else 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "azure_principal": self.azure_principal.to_dict(),
            "direct_operations_eligible": self.direct_operations_eligible,
            "environment_kind": self.environment_kind.value,
            "errors": [error.to_dict() for error in self.errors],
            "foundry_connectivity": self.foundry_connectivity.to_dict(),
            "oidc_request_variables": self.oidc_request_variables.to_dict(),
            "refresh_reacquisition": self.refresh_reacquisition.to_dict(),
            "scope": self.scope,
            "token_acquisition": [
                acquisition.to_dict()
                for acquisition in self.token_acquisition
            ],
        }


class OidcProbe:
    def __init__(
        self,
        *,
        environment: EnvironmentReader,
        command_runner: CommandRunner,
        credential_provider: CredentialProvider,
        foundry_gateway: FoundryGatewayProtocol,
    ) -> None:
        self._environment = environment
        self._commands = command_runner
        self._credential_provider = credential_provider
        self._foundry = foundry_gateway

    def run(self, request: AuthProbeRequest) -> AuthProbeResult:
        errors: list[ProbeError] = []
        environment_kind = self._environment_kind()
        oidc_variables = OidcRequestVariablesProbe(
            request_url_present=self._present(_OIDC_REQUEST_NAMES[0]),
            request_token_present=self._present(_OIDC_REQUEST_NAMES[1]),
        )
        principal = self._principal(request, errors)
        token_acquisition = self._acquire_tokens(principal, errors)
        foundry = self._check_foundry(
            request,
            principal=principal,
            token_acquisition=token_acquisition,
            errors=errors,
        )
        refresh_reacquisition = RefreshReacquisitionProbe()
        direct_operations_eligible = (
            environment_kind is EnvironmentKind.COPILOT_AGENT_POST_SETUP
            and self._environment.get("GITHUB_ACTIONS") == "true"
            and oidc_variables.present
            and principal.available
            and principal.principal_type == "service_principal"
            and principal.client_match
            and principal.tenant_match
            and principal.subscription_match
            and all(item.success for item in token_acquisition)
            and foundry.configured
            and foundry.configuration_valid
            and foundry.authentication_mode == AuthenticationMode.OIDC.value
            and foundry.firewall_reachable is True
            and foundry.read_only_access_success is True
            and refresh_reacquisition.status == "passed"
        )
        return AuthProbeResult(
            scope=request.scope,
            environment_kind=environment_kind,
            oidc_request_variables=oidc_variables,
            azure_principal=principal,
            token_acquisition=token_acquisition,
            foundry_connectivity=foundry,
            refresh_reacquisition=refresh_reacquisition,
            direct_operations_eligible=direct_operations_eligible,
            errors=tuple(errors),
        )

    def _environment_kind(self) -> EnvironmentKind:
        marker_presence = tuple(
            self._present(name) for name in _COPILOT_AGENT_MARKERS
        )
        if all(marker_presence) and self._sane_copilot_agent_markers():
            return EnvironmentKind.COPILOT_AGENT_POST_SETUP
        if (
            self._environment.get("GITHUB_ACTIONS") == "true"
            and not any(marker_presence)
        ):
            return EnvironmentKind.ACTIONS_SETUP
        return EnvironmentKind.UNKNOWN

    def _principal(
        self,
        request: AuthProbeRequest,
        errors: list[ProbeError],
    ) -> AzurePrincipalProbe:
        expected = {
            name: (self._environment.get(name) or "").strip()
            for name in _AZURE_IDENTIFIER_NAMES
        }
        if any(not value for value in expected.values()):
            errors.append(
                ProbeError(
                    code="azure_identifiers_incomplete",
                    message=(
                        "The non-secret Azure OIDC identifiers are incomplete."
                    ),
                )
            )
            return AzurePrincipalProbe(
                available=False,
                principal_type="unknown",
                client_match=False,
                tenant_match=False,
                subscription_match=False,
            )
        try:
            output = self._commands.run(
                (
                    "az",
                    "account",
                    "show",
                    "--query",
                    "{tenant:tenantId,subscription:id,client:user.name,"
                    "userType:user.type}",
                    "--output",
                    "json",
                    "--only-show-errors",
                ),
                cwd=request.repository_root,
            ).stdout
            account = json.loads(output)
            if not isinstance(account, dict):
                raise TypeError
        except Exception:
            errors.append(
                ProbeError(
                    code="azure_principal_unavailable",
                    message="The active Azure principal could not be inspected.",
                )
            )
            return AzurePrincipalProbe(
                available=False,
                principal_type="unknown",
                client_match=False,
                tenant_match=False,
                subscription_match=False,
            )

        principal_type = _principal_type(account.get("userType"))
        client_match = (
            principal_type == "service_principal"
            and _matches(account.get("client"), expected["AZURE_CLIENT_ID"])
        )
        tenant_match = _matches(
            account.get("tenant"),
            expected["AZURE_TENANT_ID"],
        )
        subscription_match = _matches(
            account.get("subscription"),
            expected["AZURE_SUBSCRIPTION_ID"],
        )
        if not (client_match and tenant_match and subscription_match):
            errors.append(
                ProbeError(
                    code="azure_principal_mismatch",
                    message=(
                        "The active Azure principal does not match the "
                        "configured optimizer identity."
                    ),
                )
            )
        return AzurePrincipalProbe(
            available=True,
            principal_type=principal_type,
            client_match=client_match,
            tenant_match=tenant_match,
            subscription_match=subscription_match,
        )

    def _acquire_tokens(
        self,
        principal: AzurePrincipalProbe,
        errors: list[ProbeError],
    ) -> tuple[TokenAcquisitionProbe, ...]:
        if (
            not principal.available
            or principal.principal_type != "service_principal"
            or not principal.client_match
            or not principal.tenant_match
            or not principal.subscription_match
        ):
            return tuple(
                TokenAcquisitionProbe(
                    resource=resource,
                    success=False,
                    attempted=False,
                )
                for resource, _ in _TOKEN_SCOPES
            )

        try:
            credential = self._credential_provider.create()
        except Exception:
            errors.append(
                ProbeError(
                    code="azure_credential_unavailable",
                    message="The Azure CLI credential could not be created.",
                )
            )
            return tuple(
                TokenAcquisitionProbe(
                    resource=resource,
                    success=False,
                    attempted=True,
                )
                for resource, _ in _TOKEN_SCOPES
            )

        results: list[TokenAcquisitionProbe] = []
        try:
            for resource, scope in _TOKEN_SCOPES:
                try:
                    credential.get_token(scope)
                except Exception:
                    errors.append(
                        ProbeError(
                            code=f"token_acquisition_failed_{resource}",
                            message=(
                                f"Azure token acquisition failed for {resource}."
                            ),
                        )
                    )
                    success = False
                else:
                    success = True
                results.append(
                    TokenAcquisitionProbe(
                        resource=resource,
                        success=success,
                    )
                )
        finally:
            close = getattr(credential, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()
        return tuple(results)

    def _check_foundry(
        self,
        request: AuthProbeRequest,
        *,
        principal: AzurePrincipalProbe,
        token_acquisition: tuple[TokenAcquisitionProbe, ...],
        errors: list[ProbeError],
    ) -> FoundryConnectivityProbe:
        config_path = request.repository_root / _CONFIG_PATH
        if not config_path.is_file():
            errors.append(
                ProbeError(
                    code="foundry_configuration_missing",
                    message="The Foundry optimizer configuration is not present.",
                )
            )
            return FoundryConnectivityProbe(
                configured=False,
                configuration_valid=False,
                authentication_mode=None,
                attempted=False,
                firewall_reachable=None,
                read_only_access_success=None,
            )
        try:
            config = load_config(config_path)
            environment = config.environments[config.default_environment]
        except (ConfigLoadError, KeyError):
            errors.append(
                ProbeError(
                    code="foundry_configuration_invalid",
                    message="The Foundry optimizer configuration is invalid.",
                )
            )
            return FoundryConnectivityProbe(
                configured=True,
                configuration_valid=False,
                authentication_mode=None,
                attempted=False,
                firewall_reachable=None,
                read_only_access_success=False,
            )

        authentication_mode = environment.authentication.value
        prerequisites_met = (
            principal.available
            and principal.client_match
            and principal.tenant_match
            and principal.subscription_match
            and all(item.success for item in token_acquisition)
        )
        if authentication_mode != AuthenticationMode.OIDC.value:
            errors.append(
                ProbeError(
                    code="foundry_authentication_not_oidc",
                    message=(
                        "The configured Foundry environment does not use OIDC."
                    ),
                )
            )
            prerequisites_met = False
        if not prerequisites_met:
            return FoundryConnectivityProbe(
                configured=True,
                configuration_valid=True,
                authentication_mode=authentication_mode,
                attempted=False,
                firewall_reachable=None,
                read_only_access_success=False,
            )

        try:
            self._foundry.verify_access(str(environment.project_endpoint))
        except Exception as error:
            code, message = _foundry_failure(error)
            errors.append(ProbeError(code=code, message=message))
            return FoundryConnectivityProbe(
                configured=True,
                configuration_valid=True,
                authentication_mode=authentication_mode,
                attempted=True,
                firewall_reachable=False,
                read_only_access_success=False,
            )
        return FoundryConnectivityProbe(
            configured=True,
            configuration_valid=True,
            authentication_mode=authentication_mode,
            attempted=True,
            firewall_reachable=True,
            read_only_access_success=True,
        )

    def _present(self, name: str) -> bool:
        return bool((self._environment.get(name) or "").strip())

    def _sane_copilot_agent_markers(self) -> bool:
        source = self._environment.get(
            "COPILOT_AGENT_SOURCE_ENVIRONMENT"
        )
        start = self._environment.get("COPILOT_AGENT_START_TIME_SEC") or ""
        timeout = self._environment.get("COPILOT_AGENT_TIMEOUT_MIN") or ""
        session_id = self._environment.get("COPILOT_AGENT_SESSION_ID") or ""
        return (
            source == "production"
            and start.isascii()
            and len(start) == 10
            and start.isdecimal()
            and 1_577_836_800 <= int(start) <= 4_102_444_799
            and timeout.isascii()
            and timeout.isdecimal()
            and 1 <= int(timeout) <= 24 * 60
            and _COPILOT_AGENT_SESSION_ID.fullmatch(session_id) is not None
        )


def build_production_auth_probe() -> OidcProbe:
    environment = OsEnvironmentReader()
    credential_provider = AzureCliCredentialProvider(environment)
    return OidcProbe(
        environment=environment,
        command_runner=SubprocessCommandRunner(),
        credential_provider=credential_provider,
        foundry_gateway=FoundryGateway(credential_provider),
    )


def _matches(actual: Any, expected: str) -> bool:
    return (
        isinstance(actual, str)
        and actual.casefold() == expected.casefold()
    )


def _principal_type(value: Any) -> str:
    if not isinstance(value, str):
        return "unknown"
    normalized = value.casefold()
    if normalized == "serviceprincipal":
        return "service_principal"
    if normalized == "user":
        return "user"
    return "unknown"


def _foundry_failure(error: Exception) -> tuple[str, str]:
    if isinstance(error, FoundryMissingCredentialsError):
        return (
            "foundry_credentials_incomplete",
            "Configured Azure authentication is incomplete.",
        )
    if isinstance(error, FoundryAuthenticationError):
        return (
            "foundry_authentication_failed",
            "Foundry authentication failed.",
        )
    if isinstance(error, FoundryAuthorizationError):
        return (
            "foundry_authorization_failed",
            "The optimizer identity cannot read the Foundry project.",
        )
    if isinstance(error, FoundryEndpointError):
        return (
            "foundry_endpoint_failed",
            "The Foundry project endpoint is invalid or unavailable.",
        )
    if isinstance(error, FoundryThrottledError):
        return (
            "foundry_throttled",
            "The Foundry read-only access check was throttled.",
        )
    if isinstance(error, FoundryTransportError):
        return (
            "foundry_transport_failed",
            "Firewall or network connectivity to Foundry failed.",
        )
    if isinstance(error, FoundryServiceError):
        return (
            "foundry_service_failed",
            "The Foundry service could not complete the read-only check.",
        )
    if isinstance(error, FoundryUnexpectedSdkError):
        return (
            "foundry_sdk_failed",
            "The Foundry SDK could not complete the read-only check.",
        )
    if isinstance(error, FoundryAccessError):
        return (
            "foundry_access_failed",
            "The Foundry read-only access check failed.",
        )
    return (
        "foundry_access_unexpected",
        "The Foundry read-only access check failed unexpectedly.",
    )
