from collections.abc import Callable
from contextlib import suppress
from typing import Any, Protocol
from urllib.parse import urlsplit

from azure.ai.projects import AIProjectClient
from azure.core.exceptions import (
    AzureError,
    ClientAuthenticationError,
    HttpResponseError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.identity import AzureCliCredential, ClientSecretCredential

from foundry_opt.preflight.interfaces import EnvironmentReader, GatewayResult


class FoundryAccessError(RuntimeError):
    """Base class for stable Foundry connectivity failures."""


class FoundryMissingCredentialsError(FoundryAccessError):
    def __init__(self, missing: tuple[str, ...]) -> None:
        self.missing = missing
        super().__init__("Required Azure service principal credentials are missing.")


class FoundryAuthenticationError(FoundryAccessError):
    def __init__(self) -> None:
        super().__init__("Azure rejected the supplied service principal credentials.")


class FoundryAuthorizationError(FoundryAccessError):
    def __init__(self) -> None:
        super().__init__("The service principal cannot read the Foundry project.")


class FoundryEndpointError(FoundryAccessError):
    def __init__(self) -> None:
        super().__init__("The Foundry project endpoint is invalid or was not found.")


class FoundryThrottledError(FoundryAccessError):
    def __init__(self) -> None:
        super().__init__("The Foundry service throttled the read-only access check.")


class FoundryTransportError(FoundryAccessError):
    def __init__(self) -> None:
        super().__init__("A network transport failure prevented Foundry access.")


class FoundryServiceError(FoundryAccessError):
    def __init__(self) -> None:
        super().__init__("The Foundry service could not complete the access check.")


class FoundryUnexpectedSdkError(FoundryAccessError):
    def __init__(self) -> None:
        super().__init__("The Foundry SDK failed unexpectedly.")


class AzureCredentialProvider(Protocol):
    def create(self) -> Any: ...


ClientSecretFactory = Callable[[str, str, str], Any]
AzureCliFactory = Callable[[str], Any]
ClientFactory = Callable[[str, Any], Any]


def _create_client_secret_credential(
    tenant_id: str,
    client_id: str,
    client_secret: str,
) -> ClientSecretCredential:
    return ClientSecretCredential(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
    )


def _create_azure_cli_credential(tenant_id: str) -> AzureCliCredential:
    return AzureCliCredential(tenant_id=tenant_id)


def _create_client(endpoint: str, credential: Any) -> AIProjectClient:
    return AIProjectClient(endpoint=endpoint, credential=credential)


class AzureCliCredentialProvider:
    def __init__(
        self,
        environment: EnvironmentReader,
        *,
        credential_factory: AzureCliFactory = _create_azure_cli_credential,
    ) -> None:
        self._environment = environment
        self._credential_factory = credential_factory

    def create(self) -> Any:
        tenant_id = (self._environment.get("AZURE_TENANT_ID") or "").strip()
        if not tenant_id:
            raise FoundryMissingCredentialsError(("AZURE_TENANT_ID",))
        try:
            return self._credential_factory(tenant_id)
        except Exception as error:
            raise _translate_credential_error(error) from None


class ClientSecretCredentialProvider:
    def __init__(
        self,
        environment: EnvironmentReader,
        *,
        tenant_id: str | None = None,
        client_id: str | None = None,
        credential_factory: ClientSecretFactory = _create_client_secret_credential,
    ) -> None:
        self._environment = environment
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._credential_factory = credential_factory

    def create(self) -> Any:
        values = {
            "AZURE_TENANT_ID": self._tenant_id
            or self._environment.get("AZURE_TENANT_ID"),
            "AZURE_CLIENT_ID": self._client_id
            or self._environment.get("AZURE_CLIENT_ID"),
            "AZURE_CLIENT_SECRET": self._environment.get("AZURE_CLIENT_SECRET"),
        }
        missing = tuple(
            name
            for name, value in values.items()
            if not value or not value.strip()
        )
        if missing:
            raise FoundryMissingCredentialsError(missing)
        try:
            return self._credential_factory(
                values["AZURE_TENANT_ID"],
                values["AZURE_CLIENT_ID"],
                values["AZURE_CLIENT_SECRET"],
            )
        except Exception as error:
            raise _translate_credential_error(error) from None


class FoundryGateway:
    def __init__(
        self,
        credential_provider: AzureCredentialProvider,
        *,
        client_factory: ClientFactory = _create_client,
    ) -> None:
        self._credential_provider = credential_provider
        self._client_factory = client_factory

    def verify_access(self, project_endpoint: str) -> GatewayResult:
        _validate_project_endpoint(project_endpoint)

        credential = None
        client = None
        try:
            credential = self._credential_provider.create()

            try:
                client = self._client_factory(project_endpoint, credential)
                agents = client.agents.list(limit=1)
                next(iter(agents), None)
            except Exception as error:
                raise _translate_sdk_error(error) from None
        finally:
            _close_quietly(client)
            _close_quietly(credential)

        return GatewayResult(
            summary="Foundry project access verified",
            detail="Read-only agent enumeration succeeded.",
        )


def _validate_project_endpoint(endpoint: str) -> None:
    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise FoundryEndpointError() from error

    path_parts = tuple(parsed.path.split("/"))
    hostname_parts = (
        tuple(hostname.casefold().split(".")) if hostname else ()
    )
    supported_hostname = hostname_parts[1:] == (
        "services",
        "ai",
        "azure",
        "com",
    )
    resource_name = hostname_parts[0] if supported_hostname else ""
    supported_hostname = (
        bool(resource_name)
        and len(resource_name) <= 63
        and resource_name.isascii()
        and resource_name[0].isalnum()
        and resource_name[-1].isalnum()
        and all(character.isalnum() or character == "-" for character in resource_name)
    )
    if (
        parsed.scheme.casefold() != "https"
        or not hostname
        or port is not None
        or not supported_hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or len(path_parts) != 4
        or path_parts[:3] != ("", "api", "projects")
        or not path_parts[3]
    ):
        raise FoundryEndpointError()


def _translate_credential_error(error: Exception) -> FoundryAccessError:
    if isinstance(error, (ClientAuthenticationError, ValueError)):
        return FoundryAuthenticationError()
    if isinstance(error, AzureError):
        return FoundryServiceError()
    return FoundryUnexpectedSdkError()


def _translate_sdk_error(error: Exception) -> FoundryAccessError:
    if isinstance(error, ClientAuthenticationError):
        return FoundryAuthenticationError()
    if isinstance(error, ServiceRequestError):
        return FoundryTransportError()
    if isinstance(error, ServiceResponseError):
        return FoundryServiceError()
    if isinstance(error, HttpResponseError):
        status_code = getattr(error, "status_code", None)
        if status_code is None and error.response is not None:
            status_code = getattr(error.response, "status_code", None)
        if status_code == 401:
            return FoundryAuthenticationError()
        if status_code == 403:
            return FoundryAuthorizationError()
        if status_code in {400, 404}:
            return FoundryEndpointError()
        if status_code == 429:
            return FoundryThrottledError()
        return FoundryServiceError()
    if isinstance(error, ValueError):
        return FoundryEndpointError()
    if isinstance(error, AzureError):
        return FoundryServiceError()
    return FoundryUnexpectedSdkError()


def _close_quietly(resource: Any | None) -> None:
    close = getattr(resource, "close", None)
    if callable(close):
        with suppress(Exception):
            close()


AzureFoundryGateway = FoundryGateway
FoundryGatewayError = FoundryAccessError
