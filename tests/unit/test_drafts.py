from __future__ import annotations

from io import BytesIO
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import zipfile

import pytest

from foundry_opt.adapters.drafts import (
    DraftApiError,
    DraftAuthenticationError,
    DraftAuthorizationError,
    DraftGateway,
    DraftHashMismatchError,
    DraftResponseError,
)
from foundry_opt.drafts import DraftRequest
from foundry_opt.packaging import (
    BundleArtifact,
    BundleRequest,
    build_source_bundle,
)


PROJECT_ENDPOINT = "https://example.services.ai.azure.com/api/projects/demo"


class FakeCredentialProvider:
    def __init__(self) -> None:
        self.credential = SimpleNamespace(closed=False)

        def close() -> None:
            self.credential.closed = True

        self.credential.close = close

    def create(self) -> object:
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
        self.reason = "failure"

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeProjectClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.requests: list[Any] = []
        self.closed = False

    def send_request(self, request: Any, **_: Any) -> FakeResponse:
        self.requests.append(request)
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


class MutatingProjectClient(FakeProjectClient):
    def __init__(
        self,
        responses: list[FakeResponse],
        mutate_after_baseline: Any,
    ) -> None:
        super().__init__(responses)
        self.mutate_after_baseline = mutate_after_baseline

    def send_request(self, request: Any, **kwargs: Any) -> FakeResponse:
        response = super().send_request(request, **kwargs)
        if len(self.requests) == 1:
            self.mutate_after_baseline()
        return response


def _bundle(tmp_path: Path, binary: bytes = b"print('ok')\n"):
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "main.py").write_bytes(binary)
    return build_source_bundle(
        BundleRequest(repository, tmp_path / "agent-code.zip")
    )


def _request(
    tmp_path: Path,
    *,
    probe: bool = False,
    binary: bytes = b"print('ok')\n",
) -> DraftRequest:
    return DraftRequest(
        project_endpoint=PROJECT_ENDPOINT,
        agent_name="demo-agent",
        base_version=7,
        bundle=_bundle(tmp_path, binary),
        runtime="python_3_13",
        entry_point=("python", "main.py"),
        dependency_resolution="remote_build",
        cpu="1",
        memory="2Gi",
        protocol="responses",
        protocol_version="1.0.0",
        environment_variables={"AZURE_AI_MODEL_DEPLOYMENT_NAME": "model"},
        probe=probe,
    )


def _baseline() -> FakeResponse:
    return FakeResponse(
        200,
        {
            "version": "7",
            "draft": False,
            "status": "active",
            "description": "published baseline",
            "metadata": {
                "owner": "platform",
                "telemetry-mode": "full",
            },
            "blueprint_reference": {
                "type": "ManagedAgentIdentityBlueprint",
                "blueprint_id": "baseline-blueprint",
            },
            "definition": {
                "kind": "hosted",
                "cpu": "2",
                "memory": "4Gi",
                "protocol_versions": [
                    {"protocol": "invocations", "version": "1.0.0"}
                ],
                "environment_variables": {
                    "AZURE_AI_MODEL_DEPLOYMENT_NAME": "baseline-model",
                    "TELEMETRY_MODE": "full",
                },
                "code_configuration": {
                    "runtime": "python_3_14",
                    "entry_point": ["python", "old.py"],
                    "dependency_resolution": "bundled",
                    "content_hash": "old-content-hash",
                },
                "responsible_ai": {"policy_name": "baseline-rai"},
                "telemetry": {
                    "application_insights_connection": "baseline-telemetry"
                },
                "future_operational_field": {"preserve": True},
            },
        },
    )


def _draft(sha256: str, version: str = "draft-candidate") -> FakeResponse:
    return FakeResponse(
        200,
        {
            "version": version,
            "draft": True,
            "status": "creating",
            "definition": {
                "code_configuration": {"content_hash": sha256}
            },
        },
    )


def _gateway(
    client: FakeProjectClient,
) -> tuple[DraftGateway, FakeCredentialProvider]:
    credentials = FakeCredentialProvider()
    gateway = DraftGateway(
        credentials,
        client_factory=lambda endpoint, credential: client,
    )
    return gateway, credentials


def _multipart_part(request: Any, name: str) -> tuple[Any, ...]:
    return dict(request._files)[name]


def test_create_draft_uses_preview_rest_contract_and_preserves_binary_zip(
    tmp_path: Path,
) -> None:
    binary = bytes(range(256))
    request = _request(tmp_path, binary=binary)
    client = FakeProjectClient([_baseline(), _draft(request.bundle.sha256)])
    gateway, credentials = _gateway(client)

    record = gateway.create_draft(request)

    assert record.version_id == "draft-candidate"
    assert record.base_version == 7
    assert record.sha256 == request.bundle.sha256
    assert record.probe is False
    baseline_call, create_call = client.requests
    assert baseline_call.method == "GET"
    assert baseline_call.url == (
        f"{PROJECT_ENDPOINT}/agents/demo-agent/versions/7?api-version=v1"
    )
    assert create_call.method == "POST"
    assert create_call.url == (
        f"{PROJECT_ENDPOINT}/agents/demo-agent/versions?api-version=v1"
    )
    assert create_call.headers["Foundry-Features"] == "DraftAgents=V1Preview"
    assert create_call.headers["x-ms-code-zip-sha256"] == request.bundle.sha256
    metadata_part = _multipart_part(create_call, "metadata")
    metadata = json.loads(metadata_part[1])
    assert metadata["draft"] is True
    assert metadata["metadata"]["foundry-opt-base-version"] == "7"
    assert metadata["metadata"]["owner"] == "platform"
    assert metadata["metadata"]["telemetry-mode"] == "full"
    assert metadata["description"] == "published baseline"
    assert metadata["blueprint_reference"]["blueprint_id"] == "baseline-blueprint"
    definition = metadata["definition"]
    assert definition["cpu"] == "2"
    assert definition["memory"] == "4Gi"
    assert definition["protocol_versions"] == [
        {"protocol": "invocations", "version": "1.0.0"}
    ]
    assert definition["environment_variables"] == {
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": "baseline-model",
        "TELEMETRY_MODE": "full",
    }
    assert definition["responsible_ai"] == {"policy_name": "baseline-rai"}
    assert definition["telemetry"] == {
        "application_insights_connection": "baseline-telemetry"
    }
    assert definition["future_operational_field"] == {"preserve": True}
    assert definition["code_configuration"] == {
        "runtime": request.runtime,
        "entry_point": list(request.entry_point),
        "dependency_resolution": request.dependency_resolution,
    }
    assert "container_configuration" not in json.dumps(metadata)
    uploaded_zip = _multipart_part(create_call, "code")[1]
    assert uploaded_zip == request.bundle.path.read_bytes()
    with zipfile.ZipFile(BytesIO(uploaded_zip)) as archive:
        assert archive.read("main.py") == binary
    assert client.closed is True
    assert credentials.credential.closed is True


def test_create_draft_rejects_release_shaped_response(tmp_path: Path) -> None:
    request = _request(tmp_path)
    client = FakeProjectClient(
        [_baseline(), _draft(request.bundle.sha256, version="8")]
    )
    gateway, _ = _gateway(client)

    with pytest.raises(DraftResponseError):
        gateway.create_draft(request)


def test_create_draft_rejects_draft_baseline(tmp_path: Path) -> None:
    request = _request(tmp_path)
    client = FakeProjectClient(
        [FakeResponse(200, {"version": "draft-old", "draft": True})]
    )
    gateway, _ = _gateway(client)

    with pytest.raises(DraftResponseError):
        gateway.create_draft(request)


def test_create_draft_reserves_provenance_metadata_at_capacity(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    baseline = _baseline()
    baseline._payload["metadata"] = {
        f"inherited-{index:02d}": f"value-{index:02d}"
        for index in range(16)
    }
    client = FakeProjectClient([baseline, _draft(request.bundle.sha256)])
    gateway, _ = _gateway(client)

    gateway.create_draft(request)

    metadata = json.loads(_multipart_part(client.requests[1], "metadata")[1])
    inherited = metadata["metadata"]
    assert len(inherited) == 16
    assert inherited["foundry-opt-base-version"] == "7"
    assert inherited["foundry-opt-source-sha256"] == request.bundle.sha256
    assert tuple(inherited)[:2] == (
        "foundry-opt-base-version",
        "foundry-opt-source-sha256",
    )
    assert "inherited-00" in inherited
    assert "inherited-13" in inherited
    assert "inherited-14" not in inherited
    assert "inherited-15" not in inherited


def test_create_draft_fails_when_caller_metadata_exceeds_reserved_capacity(
    tmp_path: Path,
) -> None:
    values = _request(tmp_path).__dict__

    with pytest.raises(ValueError, match="reserved.*provenance"):
        DraftRequest(
            **{
                **values,
                "metadata": {
                    f"caller-{index:02d}": f"value-{index:02d}"
                    for index in range(15)
                },
            }
        )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, DraftAuthenticationError),
        (403, DraftAuthorizationError),
        (400, DraftApiError),
        (404, DraftApiError),
        (409, DraftApiError),
        (500, DraftApiError),
    ],
)
def test_create_draft_translates_api_failures_without_response_details(
    tmp_path: Path,
    status: int,
    expected: type[Exception],
) -> None:
    request = _request(tmp_path)
    client = FakeProjectClient(
        [
            _baseline(),
            FakeResponse(status, {"error": {"message": "token=secret-value"}}),
        ]
    )
    gateway, _ = _gateway(client)

    with pytest.raises(expected) as raised:
        gateway.create_draft(request)

    assert "secret-value" not in str(raised.value)
    if isinstance(raised.value, DraftApiError):
        assert raised.value.status_code == status


def test_create_draft_rejects_service_zip_hash_mismatch(tmp_path: Path) -> None:
    request = _request(tmp_path)
    client = FakeProjectClient([_baseline(), _draft("0" * 64)])
    gateway, _ = _gateway(client)

    with pytest.raises(DraftHashMismatchError):
        gateway.create_draft(request)


def test_create_draft_rejects_locally_tampered_zip_before_api_call(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    request.bundle.path.write_bytes(b"tampered")
    client = FakeProjectClient([])
    gateway, _ = _gateway(client)

    with pytest.raises(DraftHashMismatchError):
        gateway.create_draft(request)

    assert client.requests == []


def test_create_draft_rejects_empty_zip_before_api_call(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    empty_zip = BytesIO()
    with zipfile.ZipFile(empty_zip, "w"):
        pass
    content = empty_zip.getvalue()
    path = tmp_path / "empty.zip"
    path.write_bytes(content)
    empty_bundle = BundleArtifact(
        path=path,
        sha256=hashlib.sha256(content).hexdigest(),
        included_files=(),
        excluded_files=(),
        byte_size=len(content),
        manifest_path=tmp_path / "empty.zip.manifest.json",
    )
    client = FakeProjectClient([])
    gateway, _ = _gateway(client)

    with pytest.raises(DraftHashMismatchError):
        gateway.create_draft(replace(request, bundle=empty_bundle))

    assert client.requests == []


def test_create_draft_uploads_exact_pre_baseline_verified_snapshot(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    verified_bytes = request.bundle.path.read_bytes()
    replacement = BytesIO()
    with zipfile.ZipFile(replacement, "w") as archive:
        archive.writestr("main.py", b"raced replacement")

    client = MutatingProjectClient(
        [_baseline(), _draft(request.bundle.sha256)],
        lambda: request.bundle.path.write_bytes(replacement.getvalue()),
    )
    gateway, _ = _gateway(client)

    gateway.create_draft(request)

    create_call = client.requests[1]
    uploaded = _multipart_part(create_call, "code")[1]
    assert uploaded == verified_bytes
    assert uploaded != request.bundle.path.read_bytes()


def test_create_draft_requires_positive_published_base_version(
    tmp_path: Path,
) -> None:
    values = _request(tmp_path).__dict__

    with pytest.raises(ValueError):
        DraftRequest(**{**values, "base_version": 0})

    with pytest.raises(ValueError):
        DraftRequest(**{**values, "base_version": "7"})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CLIENT_SECRET", "value"),
        ("CONFIG", "Authorization: Bearer raw-token"),
        ("SETTINGS", "AccountKey=raw-key;Endpoint=https://storage/"),
    ],
)
def test_draft_request_rejects_raw_credentials(
    tmp_path: Path,
    name: str,
    value: str,
) -> None:
    values = _request(tmp_path).__dict__

    with pytest.raises(ValueError):
        DraftRequest(
            **{**values, "environment_variables": {name: value}}
        )


def test_draft_request_rejects_non_foundry_endpoint(tmp_path: Path) -> None:
    values = _request(tmp_path).__dict__

    with pytest.raises(ValueError):
        DraftRequest(
            **{
                **values,
                "project_endpoint": "https://evil.example/api/projects/demo",
            }
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user@example.services.ai.azure.com/api/projects/demo",
        "https://user:password@example.services.ai.azure.com/api/projects/demo",
        "https://example.services.ai.azure.com\\@evil.example/api/projects/demo",
        "https:\\\\example.services.ai.azure.com\\api\\projects\\demo",
        "https://example.services.ai.azure.com/api/projects/demo/",
        "https://example.services.ai.azure.com//api/projects/demo",
        "https://example.services.ai.azure.com/api/projects/demo%2fother",
        "https://example.services.ai.azure.com/api/projects/../demo",
        "HTTPS://example.services.ai.azure.com/api/projects/demo",
        "https://EXAMPLE.services.ai.azure.com/api/projects/demo",
    ],
)
def test_draft_request_rejects_noncanonical_ambiguous_endpoints(
    tmp_path: Path,
    endpoint: str,
) -> None:
    values = _request(tmp_path).__dict__

    with pytest.raises(ValueError):
        DraftRequest(**{**values, "project_endpoint": endpoint})


def test_delete_probe_deletes_only_the_probe_version(tmp_path: Path) -> None:
    request = _request(tmp_path, probe=True)
    client = FakeProjectClient(
        [_baseline(), _draft(request.bundle.sha256), FakeResponse(204)]
    )
    gateway, _ = _gateway(client)
    record = gateway.create_draft(request)

    gateway.delete_probe(record)

    delete_call = client.requests[-1]
    assert delete_call.method == "DELETE"
    assert delete_call.url.endswith(
        "/agents/demo-agent/versions/draft-candidate?api-version=v1"
    )


def test_delete_probe_refuses_candidate_drafts(tmp_path: Path) -> None:
    request = _request(tmp_path)
    client = FakeProjectClient([_baseline(), _draft(request.bundle.sha256)])
    gateway, _ = _gateway(client)
    record = gateway.create_draft(request)

    with pytest.raises(ValueError):
        gateway.delete_probe(record)


def test_delete_probe_rejects_caller_forged_probe_record(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    client = FakeProjectClient([_baseline(), _draft(request.bundle.sha256)])
    gateway, _ = _gateway(client)
    candidate = gateway.create_draft(request)
    forged = replace(candidate, probe=True)

    with pytest.raises(ValueError):
        gateway.delete_probe(forged)


def test_delete_probe_requires_creating_gateway_identity(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, probe=True)
    client = FakeProjectClient([_baseline(), _draft(request.bundle.sha256)])
    creating_gateway, _ = _gateway(client)
    record = creating_gateway.create_draft(request)
    other_gateway, _ = _gateway(FakeProjectClient([]))

    with pytest.raises(ValueError):
        other_gateway.delete_probe(record)


def test_delete_probe_rejects_replayed_record(tmp_path: Path) -> None:
    request = _request(tmp_path, probe=True)
    client = FakeProjectClient(
        [_baseline(), _draft(request.bundle.sha256), FakeResponse(204)]
    )
    gateway, _ = _gateway(client)
    record = gateway.create_draft(request)
    gateway.delete_probe(record)

    with pytest.raises(ValueError):
        gateway.delete_probe(record)
