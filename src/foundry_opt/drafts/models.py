from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit
import re

from foundry_opt.packaging import BundleArtifact


_AGENT_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
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
    runtime: str
    entry_point: tuple[str, ...]
    dependency_resolution: str
    cpu: str
    memory: str
    protocol: str
    protocol_version: str
    environment_variables: Mapping[str, str] = field(default_factory=dict)
    description: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)
    probe: bool = False

    def __post_init__(self) -> None:
        parsed = urlsplit(self.project_endpoint)
        hostname = parsed.hostname.casefold() if parsed.hostname else ""
        path_parts = parsed.path.split("/")
        if (
            parsed.scheme.casefold() != "https"
            or not hostname.endswith(".services.ai.azure.com")
            or hostname == ".services.ai.azure.com"
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
            or len(path_parts) != 4
            or path_parts[:3] != ["", "api", "projects"]
            or not path_parts[3]
        ):
            raise ValueError("project_endpoint must be a Foundry project HTTPS URL")
        if not _AGENT_NAME.fullmatch(self.agent_name):
            raise ValueError("agent_name is invalid")
        if (
            not isinstance(self.base_version, int)
            or isinstance(self.base_version, bool)
            or self.base_version <= 0
        ):
            raise ValueError("base_version must be a positive published version")
        if not self.entry_point or any(not part for part in self.entry_point):
            raise ValueError("entry_point must not be empty")
        if self.dependency_resolution not in {"remote_build", "bundled"}:
            raise ValueError("dependency_resolution is invalid")
        if not self.runtime or not self.cpu or not self.memory:
            raise ValueError("runtime, cpu, and memory are required")
        if not self.protocol or not self.protocol_version:
            raise ValueError("protocol and protocol_version are required")

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
            raise ValueError("metadata permits at most 14 caller entries")
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
