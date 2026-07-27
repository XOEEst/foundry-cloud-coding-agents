from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from copy import deepcopy
import hashlib
import hmac
from io import BytesIO
import json
from pathlib import Path
import re
import secrets
from typing import Any, Protocol
from urllib.parse import quote
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

from foundry_opt.drafts import DraftRecord, DraftRequest
from foundry_opt.drafts.models import project_endpoint_components


_API_VERSION = "v1"
_PREVIEW_HEADER = "DraftAgents=V1Preview"
_DRAFT_VERSION = re.compile(r"^draft-[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_ZIP_BYTES = 250 * 1024 * 1024


class DraftError(RuntimeError):
    """Base class for stable draft deployment failures."""


class DraftAuthenticationError(DraftError):
    def __init__(self) -> None:
        super().__init__("Azure authentication failed during draft deployment.")


class DraftAuthorizationError(DraftError):
    def __init__(self) -> None:
        super().__init__(
            "The Azure identity cannot create Foundry draft versions."
        )


class DraftApiError(DraftError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(
            f"The Foundry draft API request failed with status {status_code}."
        )


class DraftResponseError(DraftError):
    def __init__(self) -> None:
        super().__init__(
            "Foundry did not return a confirmed draft version."
        )


class DraftHashMismatchError(DraftError):
    def __init__(self) -> None:
        super().__init__("Foundry returned a different source ZIP SHA-256.")


class CredentialProvider(Protocol):
    def create(self) -> Any: ...


ClientFactory = Callable[[str, Any], Any]


def _create_client(endpoint: str, credential: Any) -> AIProjectClient:
    return AIProjectClient(endpoint=endpoint, credential=credential)


class DraftGateway:
    def __init__(
        self,
        credential_provider: CredentialProvider,
        *,
        client_factory: ClientFactory = _create_client,
    ) -> None:
        self._credential_provider = credential_provider
        self._client_factory = client_factory
        self._probe_records: dict[int, tuple[DraftRecord, str, bytes]] = {}

    def create_draft(self, request: DraftRequest) -> DraftRecord:
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
            _verify_published_baseline(baseline, request.base_version)

            metadata = _draft_metadata(request, baseline)
            create_request = HttpRequest(
                "POST",
                _versions_url(request.project_endpoint, request.agent_name),
                headers={
                    "Accept": "application/json",
                    "Foundry-Features": _PREVIEW_HEADER,
                    "x-ms-code-zip-sha256": request.bundle.sha256,
                },
                files=(
                    (
                        "metadata",
                        (
                            "metadata.json",
                            json.dumps(
                                metadata,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8"),
                            "application/json",
                        ),
                    ),
                    (
                        "code",
                        (
                            request.bundle.path.name,
                            bundle_bytes,
                            "application/zip",
                        ),
                    ),
                ),
            )
            response, response_headers = _send_json_with_headers(
                client,
                create_request,
            )
            record = _parse_draft_response(
                response,
                response_headers,
                request,
            )
            if request.probe:
                token = secrets.token_urlsafe(32)
                self._probe_records[id(record)] = (
                    record,
                    token,
                    _probe_signature(record, token),
                )
            return record
        finally:
            _close_quietly(client)
            _close_quietly(credential)

    def delete_probe(self, record: DraftRecord) -> None:
        registered = self._probe_records.get(id(record))
        if registered is None:
            raise ValueError("only a confirmed onboarding probe may be deleted")
        registered_record, token, expected_signature = registered
        if (
            registered_record is not record
            or not hmac.compare_digest(
                _probe_signature(record, token),
                expected_signature,
            )
            or not record.project_endpoint
            or not _DRAFT_VERSION.fullmatch(record.version_id)
        ):
            raise ValueError("only a confirmed onboarding probe may be deleted")
        credential = None
        client = None
        try:
            credential = self._credential_provider.create()
            client = self._client_factory(record.project_endpoint, credential)
            response = _send(
                client,
                HttpRequest(
                    "DELETE",
                    _version_url(
                        record.project_endpoint,
                        record.agent_name,
                        record.version_id,
                    ),
                    headers={"Foundry-Features": _PREVIEW_HEADER},
                ),
            )
            _require_success(response)
            self._probe_records.pop(id(record), None)
        finally:
            _close_quietly(client)
            _close_quietly(credential)


def _draft_metadata(
    request: DraftRequest,
    baseline: dict[str, Any],
) -> dict[str, Any]:
    baseline_definition = baseline.get("definition")
    if (
        not isinstance(baseline_definition, dict)
        or baseline_definition.get("kind") != "hosted"
    ):
        raise DraftResponseError()
    definition = deepcopy(baseline_definition)
    definition.pop("container_configuration", None)
    configuration = definition.get("code_configuration")
    if configuration is None:
        configuration = {}
    if not isinstance(configuration, dict):
        raise DraftResponseError()
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
        raise DraftResponseError()
    provenance = {
        "foundry-opt-base-version": str(request.base_version),
        "foundry-opt-source-sha256": request.bundle.sha256,
    }
    caller_metadata = dict(request.metadata)
    if any(key in caller_metadata for key in provenance):
        raise DraftResponseError()
    if len(caller_metadata) > 14:
        raise DraftResponseError()
    inherited_metadata = {
        key: value
        for key, value in sorted(deepcopy(baseline_metadata).items())
        if key not in provenance and key not in caller_metadata
    }
    inherited_slots = 16 - len(provenance) - len(caller_metadata)
    metadata = {
        **provenance,
        **caller_metadata,
        **dict(list(inherited_metadata.items())[:inherited_slots]),
    }

    payload: dict[str, Any] = {
        "definition": definition,
        "description": (
            request.description
            if request.description is not None
            else deepcopy(baseline.get("description"))
        ),
        "metadata": metadata,
        "draft": True,
    }
    blueprint_reference = baseline.get("blueprint_reference")
    if blueprint_reference is not None:
        payload["blueprint_reference"] = deepcopy(blueprint_reference)
    return payload


def _verify_published_baseline(
    payload: dict[str, Any],
    expected_version: int,
) -> None:
    if (
        str(payload.get("version", "")) != str(expected_version)
        or payload.get("draft") is not False
    ):
        raise DraftResponseError()


def _parse_draft_response(
    payload: dict[str, Any],
    headers: Any,
    request: DraftRequest,
) -> DraftRecord:
    version = payload.get("version")
    if (
        not isinstance(version, str)
        or not _DRAFT_VERSION.fullmatch(version)
        or payload.get("draft") is not True
    ):
        raise DraftResponseError()

    returned_hash = _nested_hash(payload) or _header(
        headers,
        "x-ms-code-zip-sha256",
    )
    if not isinstance(returned_hash, str) or (
        returned_hash.casefold() != request.bundle.sha256.casefold()
    ):
        raise DraftHashMismatchError()
    status = payload.get("status")
    return DraftRecord(
        agent_name=request.agent_name,
        version_id=version,
        base_version=request.base_version,
        sha256=request.bundle.sha256,
        status=status if isinstance(status, str) else None,
        probe=request.probe,
        project_endpoint=request.project_endpoint,
    )


def _nested_hash(payload: dict[str, Any]) -> str | None:
    definition = payload.get("definition")
    if not isinstance(definition, dict):
        return None
    configuration = definition.get("code_configuration")
    if not isinstance(configuration, dict):
        return None
    value = configuration.get("content_hash")
    return value if isinstance(value, str) else None


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
        raise DraftResponseError() from error
    if not isinstance(payload, dict):
        raise DraftResponseError()
    return payload, getattr(response, "headers", {})


def _send(client: Any, request: HttpRequest) -> Any:
    try:
        return client.send_request(request)
    except Exception as error:
        raise _translate_exception(error) from None


def _require_success(response: Any) -> None:
    status = int(getattr(response, "status_code", 0))
    if 200 <= status < 300:
        return
    if status == 401:
        raise DraftAuthenticationError()
    if status == 403:
        raise DraftAuthorizationError()
    raise DraftApiError(status)


def _translate_exception(error: Exception) -> DraftError:
    if isinstance(error, ClientAuthenticationError):
        return DraftAuthenticationError()
    if isinstance(error, HttpResponseError):
        status = getattr(error, "status_code", None)
        if status == 401:
            return DraftAuthenticationError()
        if status == 403:
            return DraftAuthorizationError()
        return DraftApiError()
    if isinstance(
        error,
        (ServiceRequestError, ServiceResponseError, AzureError),
    ):
        return DraftApiError()
    return DraftApiError()


def _verify_local_bundle(
    path: Path,
    expected_sha256: str,
    expected_size: int,
) -> bytes:
    try:
        content = path.read_bytes()
    except OSError as error:
        raise DraftHashMismatchError() from error
    if len(content) != expected_size or len(content) > _MAX_ZIP_BYTES:
        raise DraftHashMismatchError()
    if not zipfile.is_zipfile(BytesIO(content)):
        raise DraftHashMismatchError()
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            if not any(not item.is_dir() for item in archive.infolist()):
                raise DraftHashMismatchError()
    except zipfile.BadZipFile as error:
        raise DraftHashMismatchError() from error
    digest = hashlib.sha256(content).hexdigest()
    if digest.casefold() != expected_sha256.casefold():
        raise DraftHashMismatchError()
    return content


def _probe_signature(record: DraftRecord, token: str) -> bytes:
    identity = "\0".join(
        (
            record.project_endpoint,
            record.agent_name,
            record.version_id,
            str(record.base_version),
            record.sha256,
        )
    ).encode("utf-8")
    return hmac.digest(token.encode("utf-8"), identity, "sha256")


def _project_url(project_endpoint: str) -> str:
    host, project = project_endpoint_components(project_endpoint)
    return (
        f"https://{host}/api/projects/"
        f"{quote(project, safe='')}"
    )


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


def _close_quietly(resource: Any | None) -> None:
    close = getattr(resource, "close", None)
    if callable(close):
        with suppress(Exception):
            close()


AzureDraftGateway = DraftGateway
