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
    DeploymentStatusError,
)
from foundry_opt.deployment import DeploymentRequest
from foundry_opt.packaging import BundleRequest, build_source_bundle


PROJECT_ENDPOINT = "https://example.services.ai.azure.com/api/projects/demo"
SHA = "a" * 64
TREE = "b" * 40
EVIDENCE = "c" * 64
BASELINE_SHA = "d" * 64


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
        expected_baseline_source_sha256=BASELINE_SHA,
        bundle=_bundle(tmp_path),
        runtime="python_3_13",
        entry_point=("python", "main.py"),
        dependency_resolution="remote_build",
        patch_sha256=SHA,
        tree_hash=TREE,
        evidence_sha256=EVIDENCE,
    )


def _baseline(
    version: int = 7,
    *,
    status: str = "active",
    source_sha256: str = BASELINE_SHA,
) -> FakeResponse:
    return FakeResponse(
        200,
        {
            "version": str(version),
            "draft": False,
            "status": status,
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
                    "content_hash": source_sha256,
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


def _readback(
    request: DeploymentRequest,
    *,
    status: str = "active",
    runtime: str | None = None,
    entry_point: list[str] | None = None,
    dependency_resolution: str | None = None,
    metadata: dict[str, str] | None = None,
    mutate: Any = None,
) -> FakeResponse:
    provenance = {
        "foundry-opt-base-version": str(request.base_version),
        "foundry-opt-baseline-source-sha256": (
            request.expected_baseline_source_sha256
        ),
        "foundry-opt-source-sha256": request.bundle.sha256,
        "foundry-opt-patch-sha256": request.patch_sha256,
        "foundry-opt-tree-hash": request.tree_hash,
        "foundry-opt-evidence-sha256": request.evidence_sha256,
    }
    persisted_metadata = {
        **provenance,
        **dict(request.metadata),
    }
    persisted_metadata.setdefault("owner", "platform")
    if metadata is not None:
        persisted_metadata = metadata
    payload = {
        "version": "8",
        "draft": False,
        "status": status,
        "portal_url": (
            "https://ai.azure.com/projects/demo/agents/demo-agent/"
            "versions/8"
        ),
        "description": (
            request.description
            if request.description is not None
            else "published baseline"
        ),
        "metadata": persisted_metadata,
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
                "runtime": runtime or request.runtime,
                "entry_point": entry_point or list(request.entry_point),
                "dependency_resolution": (
                    dependency_resolution or request.dependency_resolution
                ),
                "content_hash": request.bundle.sha256,
            },
            "responsible_ai": {"policy_name": "baseline"},
            "future_operational_field": {"preserve": True},
        },
    }
    if mutate is not None:
        mutate(payload)
    return FakeResponse(200, payload)


def _gateway(
    client: FakeClient,
) -> tuple[DeploymentGateway, FakeCredentialProvider]:
    credentials = FakeCredentialProvider()
    return (
        DeploymentGateway(
            credentials,
            client_factory=lambda endpoint, credential: client,
            poll_interval_seconds=0,
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
    client = FakeClient(
        [
            _baseline(),
            _published(request.bundle.sha256),
            _readback(request, status="creating"),
            _readback(request),
        ]
    )
    gateway, credentials = _gateway(client)

    record = gateway.publish(request)

    assert gateway.deployment_client_id == DEPLOYMENT_OIDC_CLIENT_ID
    assert record.version == 8
    assert record.baseline_source_sha256 == BASELINE_SHA
    assert record.sha256 == request.bundle.sha256
    assert record.patch_sha256 == SHA
    assert record.tree_hash == TREE
    assert record.evidence_sha256 == EVIDENCE
    assert record.status == "active"
    assert record.runtime == request.runtime
    assert record.entry_point == request.entry_point
    assert record.dependency_resolution == request.dependency_resolution
    assert record.metadata["foundry-opt-source-sha256"] == (
        request.bundle.sha256
    )
    baseline_call, publish_call, first_read, final_read = client.requests
    assert baseline_call.method == "GET"
    assert baseline_call.url == (
        f"{PROJECT_ENDPOINT}/agents/demo-agent/versions/7?api-version=v1"
    )
    assert publish_call.method == "POST"
    assert publish_call.url == (
        f"{PROJECT_ENDPOINT}/agents/demo-agent/versions?api-version=v1"
    )
    assert first_read.method == "GET"
    assert first_read.url == (
        f"{PROJECT_ENDPOINT}/agents/demo-agent/versions/8?api-version=v1"
    )
    assert final_read.url == first_read.url
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
    assert metadata["metadata"][
        "foundry-opt-baseline-source-sha256"
    ] == BASELINE_SHA
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


@pytest.mark.parametrize(
    "baseline",
    [
        lambda: _baseline(status="creating"),
        lambda: _baseline(status="failed"),
        lambda: _baseline(status="ACTIVE"),
        lambda: _baseline(source_sha256="0" * 64),
    ],
)
def test_publish_rejects_inactive_or_wrong_source_pinned_baseline(
    tmp_path: Path,
    baseline: Any,
) -> None:
    request = _request(tmp_path)
    client = FakeClient([baseline()])
    gateway, _ = _gateway(client)

    with pytest.raises(DeploymentResponseError):
        gateway.publish(request)

    assert len(client.requests) == 1


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
            _readback(request),
        ]
    )
    gateway, _ = _gateway(client)

    record = gateway.publish(request)

    assert record.version == 8
    first, second = client.requests[1:3]
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
        lambda request: replace(
            request,
            expected_baseline_source_sha256="e" * 64,
        ),
    ],
)
def test_idempotency_key_covers_complete_canonical_publication(
    tmp_path: Path,
    changed_request: Any,
) -> None:
    original = _request(tmp_path)
    changed = changed_request(original)
    original_client = FakeClient(
        [
            _baseline(),
            _published(original.bundle.sha256),
            _readback(original),
        ]
    )
    changed_client = FakeClient(
        [
            _baseline(
                changed.base_version,
                source_sha256=(
                    changed.expected_baseline_source_sha256
                ),
            ),
            _published(changed.bundle.sha256),
            _readback(changed),
        ]
    )

    _gateway(original_client)[0].publish(original)
    _gateway(changed_client)[0].publish(changed)

    assert (
        original_client.requests[1].headers["Idempotency-Key"]
        != changed_client.requests[1].headers["Idempotency-Key"]
    )


def test_publish_requires_terminal_successful_readback(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    client = FakeClient(
        [
            _baseline(),
            _published(request.bundle.sha256),
            _readback(request, status="creating"),
            _readback(request, status="pending"),
        ]
    )
    gateway = DeploymentGateway(
        FakeCredentialProvider(),
        client_factory=lambda endpoint, credential: client,
        poll_attempts=2,
        poll_interval_seconds=0,
    )

    with pytest.raises(DeploymentStatusError):
        gateway.publish(request)


@pytest.mark.parametrize("status", ["failed", "cancelled", "error"])
def test_publish_rejects_terminal_failure_readback(
    tmp_path: Path,
    status: str,
) -> None:
    request = _request(tmp_path)
    client = FakeClient(
        [
            _baseline(),
            _published(request.bundle.sha256),
            _readback(request, status=status),
        ]
    )
    gateway, _ = _gateway(client)

    with pytest.raises(DeploymentStatusError):
        gateway.publish(request)


@pytest.mark.parametrize(
    "status",
    ["completed", "ready", "succeeded", "ACTIVE"],
)
def test_publish_rejects_undocumented_success_status(
    tmp_path: Path,
    status: str,
) -> None:
    request = _request(tmp_path)
    client = FakeClient(
        [
            _baseline(),
            _published(request.bundle.sha256),
            _readback(request, status=status),
        ]
    )
    gateway, _ = _gateway(client)

    with pytest.raises(DeploymentStatusError):
        gateway.publish(request)


@pytest.mark.parametrize(
    "readback",
    [
        lambda request: _readback(request, runtime="python_3_12"),
        lambda request: _readback(
            request,
            entry_point=["python", "other.py"],
        ),
        lambda request: _readback(
            request,
            dependency_resolution="bundled",
        ),
        lambda request: _readback(
            request,
            metadata={
                "foundry-opt-base-version": "7",
                "foundry-opt-baseline-source-sha256": BASELINE_SHA,
                "foundry-opt-source-sha256": "0" * 64,
                "foundry-opt-patch-sha256": SHA,
                "foundry-opt-tree-hash": TREE,
                "foundry-opt-evidence-sha256": EVIDENCE,
            },
        ),
        lambda request: _readback(
            request,
            metadata={
                "foundry-opt-base-version": "07",
                "foundry-opt-baseline-source-sha256": BASELINE_SHA,
                "foundry-opt-source-sha256": request.bundle.sha256,
                "foundry-opt-patch-sha256": request.patch_sha256,
                "foundry-opt-tree-hash": request.tree_hash,
                "foundry-opt-evidence-sha256": request.evidence_sha256,
            },
        ),
    ],
)
def test_publish_rejects_effective_readback_mismatch(
    tmp_path: Path,
    readback: Any,
) -> None:
    request = _request(tmp_path)
    client = FakeClient(
        [
            _baseline(),
            _published(request.bundle.sha256),
            readback(request),
        ]
    )
    gateway, _ = _gateway(client)

    with pytest.raises(DeploymentResponseError):
        gateway.publish(request)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload["definition"].update({"cpu": "4"}),
        lambda payload: payload["definition"].update({"memory": "8Gi"}),
        lambda payload: payload["definition"]["environment_variables"].update(
            {"MODEL": "different"}
        ),
        lambda payload: payload["definition"].update(
            {
                "protocol_versions": [
                    {"protocol": "responses", "version": "1.0.0"}
                ]
            }
        ),
        lambda payload: payload["definition"]["responsible_ai"].update(
            {"policy_name": "different"}
        ),
        lambda payload: payload["definition"].update(
            {"future_operational_field": {"preserve": False}}
        ),
        lambda payload: payload["definition"]["code_configuration"].update(
            {"content_hash": "0" * 64}
        ),
        lambda payload: payload.update({"description": "different"}),
        lambda payload: payload["blueprint_reference"].update(
            {"blueprint_id": "different"}
        ),
        lambda payload: payload["metadata"].pop("owner"),
        lambda payload: payload["metadata"].update({"unexpected": "value"}),
    ],
)
def test_publish_rejects_any_effective_operational_payload_mismatch(
    tmp_path: Path,
    mutation: Any,
) -> None:
    request = _request(tmp_path)
    client = FakeClient(
        [
            _baseline(),
            _published(request.bundle.sha256),
            _readback(request, mutate=mutation),
        ]
    )
    gateway, _ = _gateway(client)

    with pytest.raises(DeploymentResponseError):
        gateway.publish(request)


def test_publish_verifies_all_caller_and_inherited_metadata(
    tmp_path: Path,
) -> None:
    request = replace(
        _request(tmp_path),
        description="release description",
        metadata={"release": "candidate", "owner": "delivery"},
    )
    client = FakeClient(
        [
            _baseline(),
            _published(request.bundle.sha256),
            _readback(request),
        ]
    )
    gateway, _ = _gateway(client)

    record = gateway.publish(request)

    assert record.metadata == {
        "foundry-opt-base-version": "7",
        "foundry-opt-baseline-source-sha256": BASELINE_SHA,
        "foundry-opt-source-sha256": request.bundle.sha256,
        "foundry-opt-patch-sha256": request.patch_sha256,
        "foundry-opt-tree-hash": request.tree_hash,
        "foundry-opt-evidence-sha256": request.evidence_sha256,
        "release": "candidate",
        "owner": "delivery",
    }


def test_deployment_request_rejects_noncanonical_endpoint_before_auth(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)

    with pytest.raises(ValueError, match="canonical Foundry"):
        DeploymentRequest(
            project_endpoint="https://evil.example/api/projects/demo",
            agent_name="demo-agent",
            base_version=7,
            expected_baseline_source_sha256=BASELINE_SHA,
            bundle=bundle,
            runtime="python_3_13",
            entry_point=("python", "main.py"),
            dependency_resolution="remote_build",
            patch_sha256=SHA,
            tree_hash=TREE,
            evidence_sha256=EVIDENCE,
        )
