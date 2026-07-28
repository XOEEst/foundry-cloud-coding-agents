from __future__ import annotations

import re
from typing import Any


_SECRET_KEYS = {
    "access_key",
    "api_key",
    "client_certificate",
    "client_secret",
    "connection_string",
    "credential",
    "password",
    "private_key",
    "secret",
    "secret_value",
    "shared_key",
    "signing_key",
    "token",
}
_PLURAL_SECRET_KEYS = {
    "access_keys",
    "access_tokens",
    "api_keys",
    "client_certificates",
    "client_secrets",
    "connection_strings",
    "credentials",
    "passwords",
    "private_keys",
    "secrets",
    "shared_keys",
    "signing_keys",
}
_SECRET_VALUE_MARKERS = (
    "accountkey=",
    "github_pat_",
    "ghp_",
    "-----begin private key-----",
)


def reject_secret_content(value: Any) -> Any:
    """Reject secret-shaped keys and values in persisted public contracts."""

    def visit(node: Any, path: tuple[str | int, ...] = ()) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                snake_key = re.sub(
                    r"([a-z0-9])([A-Z])",
                    r"\1_\2",
                    str(key).replace("-", "_"),
                )
                normalized = snake_key.casefold()
                has_secret_name = (
                    normalized in _SECRET_KEYS
                    or normalized in _PLURAL_SECRET_KEYS
                    or any(
                        normalized.endswith(f"_{suffix}")
                        for suffix in _SECRET_KEYS | _PLURAL_SECRET_KEYS
                    )
                )
                if has_secret_name:
                    location = ".".join(map(str, (*path, key)))
                    raise ValueError(
                        f"configuration must not contain secrets ({location})"
                    )
                visit(child, (*path, key))
        elif isinstance(node, (list, tuple, set, frozenset)):
            for index, child in enumerate(node):
                visit(child, (*path, index))
        elif isinstance(node, str):
            lowered = node.casefold()
            if any(marker in lowered for marker in _SECRET_VALUE_MARKERS):
                location = ".".join(map(str, path))
                raise ValueError(
                    f"configuration must not contain secrets ({location})"
                )

    visit(value)
    return value
