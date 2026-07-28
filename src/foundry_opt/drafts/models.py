from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping
import re

from foundry_opt.packaging import BundleArtifact


_AGENT_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_PROJECT_ENDPOINT = re.compile(
    r"https://"
    r"(?P<resource>[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)"
    r"\.services\.ai\.azure\.com/api/projects/"
    r"(?P<project>[A-Za-z0-9](?:[A-Za-z0-9._~-]{0,127})?)"
)
_SENSITIVE_ENV_MARKERS = (
    "SECRET",
    "PASSWORD",
    "TOKEN",
    "API_KEY",
    "PRIVATE_KEY",
    "CONNECTION_STRING",
    "CREDENTIAL",
)


@dataclass(frozen=True)
class DraftRequest:
    project_endpoint: str
    agent_name: str
    base_version: int
    bundle: BundleArtifact
    entry_point: tuple[str, ...]
    runtime: str | None = None
    dependency_resolution: str | None = None
    cpu: str | None = None
    memory: str | None = None
    protocol: str | None = None
    protocol_version: str | None = None
    environment_variables: Mapping[str, str] = field(default_factory=dict)
    description: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)
    idempotency_key: str | None = None
    subject: str | None = None
    probe: bool = False

    def __post_init__(self) -> None:
        project_endpoint_components(self.project_endpoint)
        if not _AGENT_NAME.fullmatch(self.agent_name):
            raise ValueError("agent_name is invalid")
        if self.idempotency_key is not None and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", self.idempotency_key
        ):
            raise ValueError("idempotency_key must be a bounded token")
        if self.subject is not None and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", self.subject
        ):
            raise ValueError("subject must be a bounded identifier")
        if (
            not isinstance(self.base_version, int)
            or isinstance(self.base_version, bool)
            or self.base_version <= 0
        ):
            raise ValueError("base_version must be a positive published version")
        if not self.entry_point or any(not part for part in self.entry_point):
            raise ValueError("entry_point must not be empty")
        # ``runtime``/``dependency_resolution`` (and the hosted
        # ``cpu``/``memory``/``protocol``/``protocol_version``) are optional:
        # ``None`` means *inherit the published baseline* rather than a guessed
        # value that would silently overwrite it. When a field is explicitly
        # configured it must be a valid, non-empty value.
        if (
            self.dependency_resolution is not None
            and self.dependency_resolution not in {"remote_build", "bundled"}
        ):
            raise ValueError("dependency_resolution is invalid")
        for value, name in (
            (self.runtime, "runtime"),
            (self.cpu, "cpu"),
            (self.memory, "memory"),
            (self.protocol, "protocol"),
            (self.protocol_version, "protocol_version"),
        ):
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise ValueError(
                    f"{name} must be a non-empty string when configured"
                )

        environment = dict(self.environment_variables)
        for name, value in environment.items():
            normalized = name.upper().replace("-", "_")
            if any(marker in normalized for marker in _SENSITIVE_ENV_MARKERS):
                raise ValueError(
                    f"environment variable {name!r} may contain credentials"
                )
            if not isinstance(value, str):
                raise ValueError("environment variable values must be strings")
            if _looks_sensitive(value):
                raise ValueError(
                    f"environment variable {name!r} contains credential material"
                )
        metadata = dict(self.metadata)
        if len(metadata) > 14:
            raise ValueError(
                "metadata permits at most 14 caller entries; two entries "
                "are reserved for foundry-opt provenance"
            )
        if {
            "foundry-opt-base-version",
            "foundry-opt-source-sha256",
            "foundry-opt-idempotency-key",
            "foundry-opt-subject",
        } & metadata.keys():
            raise ValueError("foundry-opt provenance metadata is reserved")
        if any(not isinstance(key, str) or not isinstance(value, str)
               for key, value in metadata.items()):
            raise ValueError("metadata keys and values must be strings")
        for key, value in metadata.items():
            normalized = key.upper().replace("-", "_")
            if (
                any(marker in normalized for marker in _SENSITIVE_ENV_MARKERS)
                or _looks_sensitive(value)
            ):
                raise ValueError("metadata must not contain credentials")
        if self.description is not None and _looks_sensitive(self.description):
            raise ValueError("description must not contain credentials")
        object.__setattr__(
            self,
            "environment_variables",
            MappingProxyType(environment),
        )
        object.__setattr__(self, "metadata", MappingProxyType(metadata))


def project_endpoint_components(value: str) -> tuple[str, str]:
    match = _PROJECT_ENDPOINT.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError(
            "project_endpoint must be a canonical Foundry project HTTPS URL"
        )
    host = f"{match.group('resource')}.services.ai.azure.com"
    return host, match.group("project")


@dataclass(frozen=True)
class DraftRecord:
    agent_name: str
    version_id: str
    base_version: int
    sha256: str
    status: str | None
    probe: bool = False
    project_endpoint: str = ""


def _looks_sensitive(value: str) -> bool:
    lowered = value.casefold()
    return any(
        marker in lowered
        for marker in (
            "authorization: bearer ",
            "authorization=bearer ",
            "accountkey=",
            "sharedaccesskey=",
            "sharedaccesssignature=",
            "clientsecret=",
            "client_secret=",
            "private key-----",
            "api_key=",
            "api-key=",
            "access_token=",
            "access-token=",
            "?sig=",
            "&sig=",
        )
    )
