from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from copy import deepcopy
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import secrets
import time
from typing import Any, Protocol
from urllib.parse import quote, urlsplit
import zipfile

from azure.ai.projects import AIProjectClient
from azure.core.exceptions import (
    AzureError,
    ClientAuthenticationError,
    HttpResponseError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.core.rest import HttpRequest

from foundry_opt.deployment.errors import (
    DeploymentApiError,
    DeploymentAuthenticationError,
    DeploymentAuthorizationError,
    DeploymentConflictError,
    DeploymentError,
    DeploymentHashMismatchError,
    DeploymentIdentityError,
    DeploymentResponseError,
    DeploymentStatusError,
)
from foundry_opt.deployment.models import (
    DEPLOYMENT_OIDC_CLIENT_ID,
    DeploymentRecord,
    DeploymentRequest,
)
from foundry_opt.drafts.models import project_endpoint_components


_API_VERSION = "v1"
_MAX_ZIP_BYTES = 250 * 1024 * 1024
_PROVENANCE_KEYS = (
    "foundry-opt-base-version",
    "foundry-opt-baseline-source-sha256",
    "foundry-opt-source-sha256",
    "foundry-opt-patch-sha256",
    "foundry-opt-tree-hash",
    "foundry-opt-evidence-sha256",
)
_SUCCESS_STATUSES = {"active", "completed", "ready", "succeeded"}
_PENDING_STATUSES = {
    "creating",
    "in_progress",
    "pending",
    "provisioning",
    "queued",
    "updating",
}
_FAILURE_STATUSES = {"cancelled", "error", "failed"}


class CredentialProvider(Protocol):
    def active_client_id(self) -> str: ...

    def create(self) -> Any: ...


ClientFactory = Callable[[str, Any], Any]


def _create_client(endpoint: str, credential: Any) -> AIProjectClient:
    return AIProjectClient(endpoint=endpoint, credential=credential)


class DeploymentGateway:
    def __init__(
        self,
        credential_provider: CredentialProvider,
        *,
        client_factory: ClientFactory = _create_client,
        deployment_client_id: str = DEPLOYMENT_OIDC_CLIENT_ID,
        conflict_retries: int = 1,
        poll_attempts: int = 30,
        poll_interval_seconds: float = 2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if deployment_client_id != DEPLOYMENT_OIDC_CLIENT_ID:
            raise ValueError("deployment_client_id must use the deployment OIDC app")
        if conflict_retries not in {0, 1}:
            raise ValueError("conflict_retries must be zero or one")
        if not 1 <= poll_attempts <= 120:
            raise ValueError("poll_attempts must be between 1 and 120")
        if not 0 <= poll_interval_seconds <= 60:
            raise ValueError(
                "poll_interval_seconds must be between zero and 60"
            )
        self._credential_provider = credential_provider
        self._client_factory = client_factory
        self._conflict_retries = conflict_retries
        self._poll_attempts = poll_attempts
        self._poll_interval_seconds = poll_interval_seconds
        self._sleep = sleep
        self.deployment_client_id = deployment_client_id

    def publish(self, request: DeploymentRequest) -> DeploymentRecord:
        _verify_active_principal(
            self._credential_provider,
            self.deployment_client_id,
        )
        bundle_bytes = _verify_local_bundle(
            request.bundle.path,
            request.bundle.sha256,
            request.bundle.byte_size,
        )
        credential = None
        client = None
        try:
            credential = self._credential_provider.create()
            client = self._client_factory(request.project_endpoint, credential)
            baseline = _send_json(
                client,
                HttpRequest(
                    "GET",
                    _version_url(
                        request.project_endpoint,
                        request.agent_name,
                        str(request.base_version),
                    ),
                    headers={"Accept": "application/json"},
                ),
            )
            _verify_published_baseline(baseline, request)
            metadata = _deployment_metadata(request, baseline)
            metadata_json = json.dumps(
                metadata,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            multipart_body, content_type = _multipart_body(
                metadata_json,
                bundle_bytes,
                request.bundle.path.name,
            )
            headers = {
                "Accept": "application/json",
                "Content-Type": content_type,
                "Idempotency-Key": _idempotency_key(
                    request,
                    metadata_json,
                    bundle_bytes,
                ),
                "x-ms-code-zip-sha256": request.bundle.sha256,
            }
            response: dict[str, Any] | None = None
            response_headers: Any = {}
            for attempt in range(self._conflict_retries + 1):
                try:
                    response, response_headers = _send_json_with_headers(
                        client,
                        HttpRequest(
                            "POST",
                            _versions_url(
                                request.project_endpoint,
                                request.agent_name,
                            ),
                            headers=headers,
                            content=multipart_body,
                        ),
                    )
                    break
                except DeploymentConflictError:
                    if attempt >= self._conflict_retries:
                        raise
            if response is None:
                raise DeploymentResponseError()
            version = _parse_created_version(
                response,
                response_headers,
                request,
            )
            return self._poll_published_version(
                client,
                request,
                version,
                metadata,
            )
        finally:
            _close_quietly(client)
            _close_quietly(credential)

    def _poll_published_version(
        self,
        client: Any,
        request: DeploymentRequest,
        version: int,
        expected_payload: dict[str, Any],
    ) -> DeploymentRecord:
        for attempt in range(self._poll_attempts):
            payload = _send_json(
                client,
                HttpRequest(
                    "GET",
                    _version_url(
                        request.project_endpoint,
                        request.agent_name,
                        str(version),
                    ),
                    headers={"Accept": "application/json"},
                ),
            )
            record = _parse_published_readback(
                payload,
                request.project_endpoint,
                request.agent_name,
                version,
            )
            status = (record.status or "").casefold()
            if status in _SUCCESS_STATUSES:
                _verify_effective_payload(
                    payload,
                    expected_payload,
                    request.bundle.sha256,
                )
                _verify_readback_matches_request(record, request)
                return record
            if status in _FAILURE_STATUSES:
                raise DeploymentStatusError(status)
            if status not in _PENDING_STATUSES:
                raise DeploymentStatusError()
            if attempt + 1 < self._poll_attempts:
                self._sleep(self._poll_interval_seconds)
        raise DeploymentStatusError("pending")


def _deployment_metadata(
    request: DeploymentRequest,
    baseline: dict[str, Any],
) -> dict[str, Any]:
    baseline_definition = baseline.get("definition")
    if (
        not isinstance(baseline_definition, dict)
        or baseline_definition.get("kind") != "hosted"
    ):
        raise DeploymentResponseError()
    definition = deepcopy(baseline_definition)
    definition.pop("container_configuration", None)
    configuration = definition.get("code_configuration")
    if configuration is None:
        configuration = {}
    if not isinstance(configuration, dict):
        raise DeploymentResponseError()
    configuration = deepcopy(configuration)
    configuration.pop("content_hash", None)
    configuration.update(
        {
            "runtime": request.runtime,
            "entry_point": list(request.entry_point),
            "dependency_resolution": request.dependency_resolution,
        }
    )
    definition["code_configuration"] = configuration

    baseline_metadata = baseline.get("metadata")
    if baseline_metadata is None:
        baseline_metadata = {}
    if not isinstance(baseline_metadata, dict):
        raise DeploymentResponseError()
    provenance = _provenance_metadata(request)
    caller_metadata = dict(request.metadata)
    if set(_PROVENANCE_KEYS) & caller_metadata.keys():
        raise DeploymentResponseError()
    inherited = {
        key: value
        for key, value in sorted(deepcopy(baseline_metadata).items())
        if key not in provenance and key not in caller_metadata
    }
    inherited_slots = 16 - len(provenance) - len(caller_metadata)
    metadata = {
        **provenance,
        **caller_metadata,
        **dict(list(inherited.items())[:inherited_slots]),
    }
    payload: dict[str, Any] = {
        "definition": definition,
        "description": (
            request.description
            if request.description is not None
            else deepcopy(baseline.get("description"))
        ),
        "metadata": metadata,
        "draft": False,
    }
    blueprint_reference = baseline.get("blueprint_reference")
    if blueprint_reference is not None:
        payload["blueprint_reference"] = deepcopy(blueprint_reference)
    return payload


def _provenance_metadata(request: DeploymentRequest) -> dict[str, str]:
    return {
        "foundry-opt-base-version": str(request.base_version),
        "foundry-opt-baseline-source-sha256": (
            request.expected_baseline_source_sha256
        ),
        "foundry-opt-source-sha256": request.bundle.sha256,
        "foundry-opt-patch-sha256": request.patch_sha256,
        "foundry-opt-tree-hash": request.tree_hash,
        "foundry-opt-evidence-sha256": request.evidence_sha256,
    }


def _verify_published_baseline(
    payload: dict[str, Any],
    request: DeploymentRequest,
) -> None:
    if (
        str(payload.get("version", "")) != str(request.base_version)
        or payload.get("draft") is not False
        or str(payload.get("status", "")).casefold() != "active"
        or _nested_hash(payload)
        != request.expected_baseline_source_sha256
    ):
        raise DeploymentResponseError()


def _parse_created_version(
    payload: dict[str, Any],
    headers: Any,
    request: DeploymentRequest,
) -> int:
    raw_version = payload.get("version")
    if isinstance(raw_version, bool):
        raise DeploymentResponseError()
    if isinstance(raw_version, int):
        version = raw_version
    elif isinstance(raw_version, str) and raw_version.isdigit():
        version = int(raw_version)
    else:
        raise DeploymentResponseError()
    if (
        version <= request.base_version
        or payload.get("draft") is not False
    ):
        raise DeploymentResponseError()
    returned_hash = _nested_hash(payload) or _header(
        headers,
        "x-ms-code-zip-sha256",
    )
    if (
        not isinstance(returned_hash, str)
        or returned_hash.casefold() != request.bundle.sha256.casefold()
    ):
        raise DeploymentHashMismatchError()
    return version


def _parse_published_readback(
    payload: dict[str, Any],
    project_endpoint: str,
    agent_name: str,
    expected_version: int,
) -> DeploymentRecord:
    raw_version = payload.get("version")
    if isinstance(raw_version, bool):
        raise DeploymentResponseError()
    if isinstance(raw_version, int):
        version = raw_version
    elif isinstance(raw_version, str) and raw_version.isdigit():
        version = int(raw_version)
    else:
        raise DeploymentResponseError()
    if version != expected_version or payload.get("draft") is not False:
        raise DeploymentResponseError()
    status = payload.get("status")
    if not isinstance(status, str) or not status:
        raise DeploymentStatusError()
    definition = payload.get("definition")
    if (
        not isinstance(definition, dict)
        or definition.get("kind") != "hosted"
    ):
        raise DeploymentResponseError()
    configuration = definition.get("code_configuration")
    metadata = payload.get("metadata")
    if not isinstance(configuration, dict) or not isinstance(metadata, dict):
        raise DeploymentResponseError()
    runtime = configuration.get("runtime")
    entry_point = configuration.get("entry_point")
    dependency_resolution = configuration.get("dependency_resolution")
    source_sha256 = configuration.get("content_hash")
    if (
        not isinstance(runtime, str)
        or not runtime
        or not isinstance(entry_point, list)
        or not entry_point
        or any(not isinstance(part, str) or not part for part in entry_point)
        or not isinstance(dependency_resolution, str)
        or not isinstance(source_sha256, str)
        or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in metadata.items()
        )
    ):
        raise DeploymentResponseError()
    try:
        return DeploymentRecord(
            project_endpoint=project_endpoint,
            agent_name=agent_name,
            version=version,
            base_version=int(metadata["foundry-opt-base-version"]),
            baseline_source_sha256=metadata[
                "foundry-opt-baseline-source-sha256"
            ],
            sha256=source_sha256,
            patch_sha256=metadata["foundry-opt-patch-sha256"],
            tree_hash=metadata["foundry-opt-tree-hash"],
            evidence_sha256=metadata["foundry-opt-evidence-sha256"],
            status=status.casefold(),
            portal_url=_safe_portal_url(
                payload.get("portal_url")
                or payload.get("portalUrl")
                or _nested_portal_url(payload)
            ),
            runtime=runtime,
            entry_point=tuple(entry_point),
            dependency_resolution=dependency_resolution,
            metadata=metadata,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise DeploymentResponseError() from error


def _verify_readback_matches_request(
    record: DeploymentRecord,
    request: DeploymentRequest,
) -> None:
    if (
        record.base_version != request.base_version
        or record.baseline_source_sha256
        != request.expected_baseline_source_sha256
        or record.sha256 != request.bundle.sha256
        or record.patch_sha256 != request.patch_sha256
        or record.tree_hash != request.tree_hash
        or record.evidence_sha256 != request.evidence_sha256
        or record.runtime != request.runtime
        or record.entry_point != request.entry_point
        or record.dependency_resolution != request.dependency_resolution
        or any(
            record.metadata.get(key) != value
            for key, value in _provenance_metadata(request).items()
        )
    ):
        raise DeploymentResponseError()


def _verify_effective_payload(
    payload: dict[str, Any],
    expected_payload: dict[str, Any],
    bundle_sha256: str,
) -> None:
    expected = deepcopy(expected_payload)
    definition = expected.get("definition")
    if not isinstance(definition, dict):
        raise DeploymentResponseError()
    configuration = definition.get("code_configuration")
    if not isinstance(configuration, dict):
        raise DeploymentResponseError()
    configuration["content_hash"] = bundle_sha256
    for key in ("definition", "description", "metadata", "draft"):
        if key not in payload or payload[key] != expected.get(key):
            raise DeploymentResponseError()
    if (
        ("blueprint_reference" in payload)
        != ("blueprint_reference" in expected)
        or payload.get("blueprint_reference")
        != expected.get("blueprint_reference")
    ):
        raise DeploymentResponseError()


def _nested_hash(payload: dict[str, Any]) -> str | None:
    definition = payload.get("definition")
    if not isinstance(definition, dict):
        return None
    configuration = definition.get("code_configuration")
    if not isinstance(configuration, dict):
        return None
    value = configuration.get("content_hash")
    return value if isinstance(value, str) else None


def _nested_portal_url(payload: dict[str, Any]) -> object:
    links = payload.get("links")
    if not isinstance(links, dict):
        return None
    return links.get("portal") or links.get("portal_url")


def _multipart_body(
    metadata_json: bytes,
    bundle_bytes: bytes,
    filename: str,
) -> tuple[bytes, str]:
    boundary = f"----FoundryOptBoundary{secrets.token_hex(16)}"
    safe_filename = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
    parts = (
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="metadata"\r\n',
        b"Content-Type: application/json\r\n\r\n",
        metadata_json,
        b"\r\n",
        f"--{boundary}\r\n".encode(),
        (
            'Content-Disposition: form-data; name="code"; '
            f'filename="{safe_filename}"\r\n'
        ).encode(),
        b"Content-Type: application/zip\r\n\r\n",
        bundle_bytes,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    )
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _idempotency_key(
    request: DeploymentRequest,
    metadata_json: bytes,
    bundle_bytes: bytes,
) -> str:
    bundle_digest = hashlib.sha256(bundle_bytes).hexdigest().encode("ascii")
    canonical_payload = (
        b"foundry-opt-published-version-v1\0"
        + request.project_endpoint.encode("utf-8")
        + b"\0"
        + request.agent_name.encode("utf-8")
        + b"\0"
        + metadata_json
        + b"\0"
        + bundle_digest
    )
    return hashlib.sha256(canonical_payload).hexdigest()


def _verify_active_principal(
    provider: CredentialProvider,
    expected_client_id: str,
) -> None:
    active_client_id = getattr(provider, "active_client_id", None)
    if not callable(active_client_id):
        raise DeploymentIdentityError()
    try:
        actual = active_client_id()
    except Exception:
        raise DeploymentIdentityError() from None
    if (
        not isinstance(actual, str)
        or actual.casefold() != expected_client_id.casefold()
    ):
        raise DeploymentIdentityError()


def _header(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    for key, value in headers.items():
        if str(key).casefold() == name.casefold():
            return str(value)
    return None


def _send_json(client: Any, request: HttpRequest) -> dict[str, Any]:
    payload, _ = _send_json_with_headers(client, request)
    return payload


def _send_json_with_headers(
    client: Any,
    request: HttpRequest,
) -> tuple[dict[str, Any], Any]:
    response = _send(client, request)
    _require_success(response)
    try:
        payload = response.json()
    except Exception as error:
        raise DeploymentResponseError() from error
    if not isinstance(payload, dict):
        raise DeploymentResponseError()
    return payload, getattr(response, "headers", {})


def _send(client: Any, request: HttpRequest) -> Any:
    try:
        return client.send_request(request)
    except DeploymentError:
        raise
    except Exception as error:
        raise _translate_exception(error) from None


def _require_success(response: Any) -> None:
    status = int(getattr(response, "status_code", 0))
    if 200 <= status < 300:
        return
    if status == 401:
        raise DeploymentAuthenticationError()
    if status == 403:
        raise DeploymentAuthorizationError()
    if status == 409:
        raise DeploymentConflictError()
    raise DeploymentApiError(status)


def _translate_exception(error: Exception) -> DeploymentError:
    if isinstance(error, ClientAuthenticationError):
        return DeploymentAuthenticationError()
    if isinstance(error, HttpResponseError):
        status = getattr(error, "status_code", None)
        if status == 401:
            return DeploymentAuthenticationError()
        if status == 403:
            return DeploymentAuthorizationError()
        if status == 409:
            return DeploymentConflictError()
        return DeploymentApiError(status if isinstance(status, int) else None)
    if isinstance(
        error,
        (ServiceRequestError, ServiceResponseError, AzureError),
    ):
        return DeploymentApiError()
    return DeploymentApiError()


def _verify_local_bundle(
    path: Path,
    expected_sha256: str,
    expected_size: int,
) -> bytes:
    try:
        content = path.read_bytes()
    except OSError as error:
        raise DeploymentHashMismatchError() from error
    if len(content) != expected_size or len(content) > _MAX_ZIP_BYTES:
        raise DeploymentHashMismatchError()
    if not zipfile.is_zipfile(BytesIO(content)):
        raise DeploymentHashMismatchError()
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            if not any(not item.is_dir() for item in archive.infolist()):
                raise DeploymentHashMismatchError()
    except zipfile.BadZipFile as error:
        raise DeploymentHashMismatchError() from error
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise DeploymentHashMismatchError()
    return content


def _project_url(project_endpoint: str) -> str:
    host, project = project_endpoint_components(project_endpoint)
    return f"https://{host}/api/projects/{quote(project, safe='')}"


def _versions_url(project_endpoint: str, agent_name: str) -> str:
    return (
        f"{_project_url(project_endpoint)}/agents/"
        f"{quote(agent_name, safe='')}/versions?api-version={_API_VERSION}"
    )


def _version_url(
    project_endpoint: str,
    agent_name: str,
    version: str,
) -> str:
    return (
        f"{_project_url(project_endpoint)}/agents/"
        f"{quote(agent_name, safe='')}/versions/{quote(version, safe='')}"
        f"?api-version={_API_VERSION}"
    )


def _safe_portal_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname.casefold()
        not in {"ai.azure.com", "portal.azure.com"}
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    return value


def _close_quietly(resource: Any | None) -> None:
    close = getattr(resource, "close", None)
    if callable(close):
        with suppress(Exception):
            close()


AzureDeploymentGateway = DeploymentGateway
