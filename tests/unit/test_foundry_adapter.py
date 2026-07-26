from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ServiceRequestError,
    ServiceResponseError,
)

from foundry_opt.adapters.foundry import (
    AzureCliCredentialProvider,
    ClientSecretCredentialProvider,
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


PROJECT_ENDPOINT = "https://example.services.ai.azure.com/api/projects/demo"


class FakeEnvironmentReader:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.requested: list[str] = []

    def get(self, name: str) -> str | None:
        self.requested.append(name)
        return self.values.get(name)


class FakeAgents:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.limits: list[int | None] = []

    def list(self, *, limit: int | None = None) -> Iterator[object]:
        self.limits.append(limit)
        if self.failure is not None:
            raise self.failure
        return iter((object(),))


class FakeProjectClient:
    def __init__(self, failure: Exception | None = None) -> None:
        self.agents = FakeAgents(failure)
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeCredentialProvider:
    def __init__(
        self,
        credential: object | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.credential = credential or object()
        self.failure = failure
        self.calls = 0

    def create(self) -> object:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return self.credential


def _environment() -> FakeEnvironmentReader:
    return FakeEnvironmentReader(
        {
            "AZURE_TENANT_ID": "tenant-value",
            "AZURE_CLIENT_ID": "client-value",
            "AZURE_CLIENT_SECRET": "secret-value",
        }
    )


def test_oidc_provider_builds_azure_cli_credential_without_a_secret() -> None:
    environment = FakeEnvironmentReader(
        {
            "AZURE_TENANT_ID": "tenant-value",
            "AZURE_CLIENT_ID": "client-value",
            "AZURE_SUBSCRIPTION_ID": "subscription-value",
        }
    )
    calls: list[str] = []
    credential = object()

    result = AzureCliCredentialProvider(
        environment,
        credential_factory=lambda tenant_id: calls.append(tenant_id) or credential,
    ).create()

    assert result is credential
    assert calls == ["tenant-value"]
    assert "AZURE_CLIENT_SECRET" not in environment.requested


def test_client_secret_provider_requires_only_the_secret_from_environment() -> None:
    environment = FakeEnvironmentReader({})

    with pytest.raises(FoundryMissingCredentialsError) as raised:
        ClientSecretCredentialProvider(
            environment,
            tenant_id="tenant-value",
            client_id="client-value",
        ).create()

    assert raised.value.missing == ("AZURE_CLIENT_SECRET",)


def test_client_secret_provider_builds_explicit_credential() -> None:
    environment = FakeEnvironmentReader(
        {"AZURE_CLIENT_SECRET": "secret-value"}
    )
    calls: list[tuple[str, str, str]] = []
    credential = object()

    result = ClientSecretCredentialProvider(
        environment,
        tenant_id="tenant-value",
        client_id="client-value",
        credential_factory=lambda tenant, client, secret: (
            calls.append((tenant, client, secret)) or credential
        ),
    ).create()

    assert result is credential
    assert calls == [("tenant-value", "client-value", "secret-value")]


def _http_error(status_code: int) -> HttpResponseError:
    response = SimpleNamespace(
        status_code=status_code,
        reason="failure",
        headers={},
        request=SimpleNamespace(url=PROJECT_ENDPOINT),
    )
    return HttpResponseError(message="SDK detail containing secret-value", response=response)


def test_verify_access_builds_explicit_credential_and_enumerates_one_agent() -> None:
    credential = SimpleNamespace(close=lambda: None)
    credential_provider = FakeCredentialProvider(credential)
    client_arguments: list[tuple[str, object]] = []
    client = FakeProjectClient()

    def client_factory(endpoint: str, supplied_credential: object) -> object:
        client_arguments.append((endpoint, supplied_credential))
        return client

    result = FoundryGateway(
        credential_provider,
        client_factory=client_factory,
    ).verify_access(PROJECT_ENDPOINT)

    assert credential_provider.calls == 1
    assert client_arguments == [(PROJECT_ENDPOINT, credential)]
    assert client.agents.limits == [1]
    assert client.closed is True
    assert result.summary == "Foundry project access verified"
    assert result.detail == "Read-only agent enumeration succeeded."
    assert "secret-value" not in f"{result.summary} {result.detail}"


def test_verify_access_reports_all_missing_credentials_without_values() -> None:
    environment = FakeEnvironmentReader(
        {
            "AZURE_TENANT_ID": "",
            "AZURE_CLIENT_ID": "client-value",
        }
    )

    with pytest.raises(FoundryMissingCredentialsError) as raised:
        ClientSecretCredentialProvider(environment).create()

    assert raised.value.missing == (
        "AZURE_TENANT_ID",
        "AZURE_CLIENT_SECRET",
    )
    assert "client-value" not in str(raised.value)


def test_verify_access_accepts_official_foundry_project_host() -> None:
    endpoint = "https://valid-resource.services.ai.azure.com/api/projects/demo"
    client = FakeProjectClient()
    client_arguments: list[tuple[str, object]] = []

    FoundryGateway(
        FakeCredentialProvider(),
        client_factory=lambda supplied_endpoint, credential: (
            client_arguments.append((supplied_endpoint, credential)) or client
        ),
    ).verify_access(endpoint)

    assert len(client_arguments) == 1
    assert client_arguments[0][0] == endpoint
    assert client.agents.limits == [1]


@pytest.mark.parametrize(
    ("sdk_error", "expected_type"),
    [
        (
            ClientAuthenticationError("SDK detail containing secret-value"),
            FoundryAuthenticationError,
        ),
        (_http_error(401), FoundryAuthenticationError),
        (_http_error(403), FoundryAuthorizationError),
        (_http_error(404), FoundryEndpointError),
        (_http_error(429), FoundryThrottledError),
        (_http_error(500), FoundryServiceError),
        (ServiceRequestError("secret-value"), FoundryTransportError),
        (ServiceResponseError("secret-value"), FoundryServiceError),
        (RuntimeError("secret-value"), FoundryUnexpectedSdkError),
    ],
)
def test_verify_access_translates_sdk_failures_without_leaking_details(
    sdk_error: Exception,
    expected_type: type[Exception],
) -> None:
    client = FakeProjectClient(sdk_error)

    with pytest.raises(expected_type) as raised:
        FoundryGateway(
            FakeCredentialProvider(),
            client_factory=lambda *_: client,
        ).verify_access(PROJECT_ENDPOINT)

    assert "secret-value" not in str(raised.value)
    assert client.closed is True


@pytest.mark.parametrize(
    "endpoint",
    [
        "not-a-url",
        "http://example.services.ai.azure.com/api/projects/demo",
        "https://example.services.ai.azure.com/wrong/demo",
        "https://example.services.ai.azure.com/api//projects/demo",
        "https://example.services.ai.azure.com/api/projects/demo/",
        "https://example.services.ai.azure.com/api/projects/demo/extra",
        "https://example.services.ai.azure.com/api/projects/demo?token=secret-value",
        "https://example.services.ai.azure.com:443/api/projects/demo",
        "https://user@example.services.ai.azure.com/api/projects/demo",
        "https://example.services.ai.azure.com/api/projects/demo#secret-value",
    ],
)
def test_verify_access_rejects_malformed_project_endpoints(endpoint: str) -> None:
    factory_called = False
    credential_provider = FakeCredentialProvider()

    def client_factory(*_: Any) -> object:
        nonlocal factory_called
        factory_called = True
        return FakeProjectClient()

    with pytest.raises(FoundryEndpointError) as raised:
        FoundryGateway(
            credential_provider,
            client_factory=client_factory,
        ).verify_access(endpoint)

    assert factory_called is False
    assert credential_provider.calls == 0
    assert "secret-value" not in str(raised.value)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://attacker.example/api/projects/demo",
        "https://example.foundry.azure.com/api/projects/demo",
        "https://example.services.ai.azure.com.attacker.example/api/projects/demo",
        "https://example.foundry.azure.com.attacker.example/api/projects/demo",
        "https://attacker.example.services.ai.azure.com/api/projects/demo",
        "https://attacker.example.foundry.azure.com/api/projects/demo",
        "https://example.services.ai.azure.us/api/projects/demo",
        "https://.services.ai.azure.com/api/projects/demo",
        "https://-example.services.ai.azure.com/api/projects/demo",
        "https://example-.foundry.azure.com/api/projects/demo",
    ],
)
def test_verify_access_rejects_unsupported_hosts_before_constructing_factories(
    endpoint: str,
) -> None:
    credential_provider = FakeCredentialProvider()
    client_calls: list[tuple[Any, ...]] = []

    with pytest.raises(FoundryEndpointError):
        FoundryGateway(
            credential_provider,
            client_factory=lambda *args: client_calls.append(args),
        ).verify_access(endpoint)

    assert credential_provider.calls == 0
    assert client_calls == []
