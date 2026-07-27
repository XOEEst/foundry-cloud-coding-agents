from __future__ import annotations

from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import zipfile

import pytest

from foundry_opt.adapters.deployment import (
    DEPLOYMENT_OIDC_CLIENT_ID,
    DeploymentApiError,
    DeploymentAuthenticationError,
    DeploymentAuthorizationError,
    DeploymentConflictError,
    DeploymentGateway,
    DeploymentHashMismatchError,
    DeploymentIdentityError,
    DeploymentResponseError,
)
from foundry_opt.deployment import DeploymentRequest
from foundry_opt.packaging import BundleRequest, build_source_bundle


PROJECT_ENDPOINT = "https://example.services.ai.azure.com/api/projects/demo"
SHA = "a" * 64
TREE = "b" * 40
EVIDENCE = "c" * 64


class FakeCredentialProvider:
    def __init__(
        self,
        client_id: str = DEPLOYMENT_OIDC_CLIENT_ID,
    ) -> None:
        self.client_id = client_id
        self.create_count = 0
        self.credential = SimpleNamespace(closed=False)
        self.credential.close = lambda: setattr(
            self.credential, "closed", True
        )

    def active_client_id(self) -> str:
        return self.client_id

    def create(self) -> object:
        self.create_count += 1
        return self.credential


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.requests: list[Any] = []
        self.closed = False

    def send_request(self, request: Any) -> FakeResponse:
        self.requests.append(request)
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


def _bundle(tmp_path: Path, content: bytes = bytes(range(256))):
    repository = tmp_path / "agent"
    repository.mkdir()
    (repository / "main.py").write_bytes(content)
    return build_source_bundle(
        BundleRequest(repository, tmp_path / "agent.zip")
    )


def _request(tmp_path: Path) -> DeploymentRequest:
    return DeploymentRequest(
        project_endpoint=PROJECT_ENDPOINT,
        agent_name="demo-agent",
        base_version=7,
        bundle=_bundle(tmp_path),
        runtime="python_3_13",
        entry_point=("python", "main.py"),
        dependency_resolution="remote_build",
        patch_sha256=SHA,
        tree_hash=TREE,
        evidence_sha256=EVIDENCE,
    )


def _baseline(version: int = 7) -> FakeResponse:
    return FakeResponse(
        200,
        {
            "version": str(version),
            "draft": False,
            "description": "published baseline",
            "metadata": {"owner": "platform"},
            "blueprint_reference": {"blueprint_id": "pinned-blueprint"},
            "definition": {
                "kind": "hosted",
                "cpu": "2",
                "memory": "4Gi",
                "protocol_versions": [
                    {"protocol": "invocations", "version": "1.0.0"}
                ],
                "environment_variables": {"MODEL": "baseline-model"},
                "code_configuration": {
                    "runtime": "python_3_12",
                    "entry_point": ["python", "old.py"],
                    "dependency_resolution": "bundled",
                    "content_hash": "old",
                },
                "container_configuration": {
                    "image": "registry.azurecr.io/forbidden:latest"
                },
                "responsible_ai": {"policy_name": "baseline"},
                "future_operational_field": {"preserve": True},
            },
        },
    )


def _published(
    sha256: str,
    *,
    version: object = "8",
    draft: object = False,
) -> FakeResponse:
    return FakeResponse(
        201,
        {
            "version": version,
            "draft": draft,
            "status": "creating",
            "portal_url": (
                "https://ai.azure.com/projects/demo/agents/demo-agent/"
                "versions/8"
            ),
            "definition": {
                "code_configuration": {"content_hash": sha256}
            },
        },
    )


def _gateway(
    client: FakeClient,
) -> tuple[DeploymentGateway, FakeCredentialProvider]:
    credentials = FakeCredentialProvider()
    return (
        DeploymentGateway(
            credentials,
            client_factory=lambda endpoint, credential: client,
        ),
        credentials,
    )


def _multipart_part(request: Any, name: str) -> bytes:
    boundary = request.headers["Content-Type"].split(
        "boundary=", 1
    )[1].encode()
    for part in request.content.split(b"--" + boundary):
        if f'name="{name}"'.encode() not in part:
            continue
        return part.split(b"\r\n\r\n", 1)[1].removesuffix(b"\r\n")
    raise AssertionError(f"missing multipart part {name}")


def test_publish_uses_source_zip_contract_and_inherits_pinned_base(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    client = FakeClient([_baseline(), _published(request.bundle.sha256)])
    gateway, credentials = _gateway(client)

    record = gateway.publish(request)

    assert gateway.deployment_client_id == DEPLOYMENT_OIDC_CLIENT_ID
    assert record.version == 8
    assert record.sha256 == request.bundle.sha256
    assert record.patch_sha256 == SHA
    assert record.tree_hash == TREE
    assert record.evidence_sha256 == EVIDENCE
    baseline_call, publish_call = client.requests
    assert baseline_call.method == "GET"
    assert baseline_call.url == (
        f"{PROJECT_ENDPOINT}/agents/demo-agent/versions/7?api-version=v1"
    )
    assert publish_call.method == "POST"
    assert publish_call.url == (
        f"{PROJECT_ENDPOINT}/agents/demo-agent/versions?api-version=v1"
    )
    assert all(
        marker not in publish_call.url.casefold()
        for marker in ("routing", "endpoint", "acr")
    )
    assert "Foundry-Features" not in publish_call.headers
    assert (
        publish_call.headers["x-ms-code-zip-sha256"]
        == request.bundle.sha256
    )
    metadata = json.loads(_multipart_part(publish_call, "metadata"))
    assert metadata["draft"] is False
    assert metadata["description"] == "published baseline"
    assert metadata["blueprint_reference"] == {
        "blueprint_id": "pinned-blueprint"
    }
    assert metadata["metadata"]["owner"] == "platform"
    assert metadata["metadata"]["foundry-opt-base-version"] == "7"
    assert metadata["metadata"]["foundry-opt-patch-sha256"] == SHA
    definition = metadata["definition"]
    assert definition["cpu"] == "2"
    assert definition["memory"] == "4Gi"
    assert definition["environment_variables"] == {
        "MODEL": "baseline-model"
    }
    assert definition["responsible_ai"] == {"policy_name": "baseline"}
    assert definition["future_operational_field"] == {"preserve": True}
    assert definition["code_configuration"] == {
        "runtime": "python_3_13",
        "entry_point": ["python", "main.py"],
        "dependency_resolution": "remote_build",
    }
    assert "container_configuration" not in definition
    uploaded = _multipart_part(publish_call, "code")
    assert uploaded == request.bundle.path.read_bytes()
    with zipfile.ZipFile(BytesIO(uploaded)) as archive:
        assert archive.read("main.py") == bytes(range(256))
    assert client.closed is True
    assert credentials.credential.closed is True


@pytest.mark.parametrize(
    ("version", "draft"),
    [
        ("draft-candidate", True),
        ("8", True),
        (None, False),
        ("not-numeric", False),
        (0, False),
    ],
)
def test_publish_rejects_non_published_response(
    tmp_path: Path,
    version: object,
    draft: object,
) -> None:
    request = _request(tmp_path)
    client = FakeClient(
        [
            _baseline(),
            _published(
                request.bundle.sha256,
                version=version,
                draft=draft,
            ),
        ]
    )
    gateway, _ = _gateway(client)

    with pytest.raises(DeploymentResponseError):
        gateway.publish(request)


def test_publish_rejects_service_hash_mismatch(tmp_path: Path) -> None:
    request = _request(tmp_path)
    client = FakeClient([_baseline(), _published("0" * 64)])
    gateway, _ = _gateway(client)

    with pytest.raises(DeploymentHashMismatchError):
        gateway.publish(request)


def test_publish_rejects_tampered_bundle_before_authentication(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    request.bundle.path.write_bytes(b"tampered")
    client = FakeClient([])
    gateway, credentials = _gateway(client)

    with pytest.raises(DeploymentHashMismatchError):
        gateway.publish(request)

    assert client.requests == []
    assert credentials.credential.closed is False


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, DeploymentAuthenticationError),
        (403, DeploymentAuthorizationError),
        (400, DeploymentApiError),
        (404, DeploymentApiError),
        (500, DeploymentApiError),
    ],
)
def test_publish_translates_redacted_api_errors(
    tmp_path: Path,
    status: int,
    expected: type[Exception],
) -> None:
    request = _request(tmp_path)
    client = FakeClient(
        [
            _baseline(),
            FakeResponse(
                status,
                {"error": {"message": "Authorization: Bearer secret"}},
            ),
        ]
    )
    gateway, _ = _gateway(client)

    with pytest.raises(expected) as raised:
        gateway.publish(request)

    assert "secret" not in str(raised.value).casefold()


def test_publish_retries_one_conflict_with_identical_multipart_bytes(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    client = FakeClient(
        [
            _baseline(),
            FakeResponse(409, {"error": {"message": "conflict"}}),
            _published(request.bundle.sha256),
        ]
    )
    gateway, _ = _gateway(client)

    record = gateway.publish(request)

    assert record.version == 8
    first, second = client.requests[1:]
    assert first.content == second.content
    assert (
        first.headers["Idempotency-Key"]
        == second.headers["Idempotency-Key"]
    )


def test_publish_rejects_repeated_conflict_without_success_fallback(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    client = FakeClient(
        [_baseline(), FakeResponse(409), FakeResponse(409)]
    )
    gateway, _ = _gateway(client)

    with pytest.raises(DeploymentConflictError):
        gateway.publish(request)


def test_deployment_gateway_rejects_any_other_oidc_client() -> None:
    with pytest.raises(ValueError, match="deployment OIDC app"):
        DeploymentGateway(
            FakeCredentialProvider(),
            deployment_client_id="8179845f-cf82-46d6-bbb8-9adf13d082f9",
        )


def test_publish_rejects_wrong_active_principal_before_credentials_or_http(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    credentials = FakeCredentialProvider(
        "8179845f-cf82-46d6-bbb8-9adf13d082f9"
    )
    client = FakeClient([])
    gateway = DeploymentGateway(
        credentials,
        client_factory=lambda endpoint, credential: client,
    )

    with pytest.raises(DeploymentIdentityError):
        gateway.publish(request)

    assert credentials.create_count == 0
    assert client.requests == []


@pytest.mark.parametrize(
    "changed_request",
    [
        lambda request: replace(request, agent_name="other-agent"),
        lambda request: replace(
            request,
            project_endpoint=(
                "https://other.services.ai.azure.com/api/projects/demo"
            ),
        ),
        lambda request: replace(request, runtime="python_3_14"),
        lambda request: replace(
            request,
            entry_point=("python", "-m", "agent"),
        ),
        lambda request: replace(request, dependency_resolution="bundled"),
        lambda request: replace(request, description="new description"),
        lambda request: replace(request, metadata={"release": "candidate"}),
        lambda request: replace(request, base_version=6),
    ],
)
def test_idempotency_key_covers_complete_canonical_publication(
    tmp_path: Path,
    changed_request: Any,
) -> None:
    original = _request(tmp_path)
    changed = changed_request(original)
    original_client = FakeClient(
        [_baseline(), _published(original.bundle.sha256)]
    )
    changed_client = FakeClient(
        [
            _baseline(changed.base_version),
            _published(changed.bundle.sha256),
        ]
    )

    _gateway(original_client)[0].publish(original)
    _gateway(changed_client)[0].publish(changed)

    assert (
        original_client.requests[1].headers["Idempotency-Key"]
        != changed_client.requests[1].headers["Idempotency-Key"]
    )


def test_deployment_request_rejects_noncanonical_endpoint_before_auth(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)

    with pytest.raises(ValueError, match="canonical Foundry"):
        DeploymentRequest(
            project_endpoint="https://evil.example/api/projects/demo",
            agent_name="demo-agent",
            base_version=7,
            bundle=bundle,
            runtime="python_3_13",
            entry_point=("python", "main.py"),
            dependency_resolution="remote_build",
            patch_sha256=SHA,
            tree_hash=TREE,
            evidence_sha256=EVIDENCE,
        )
