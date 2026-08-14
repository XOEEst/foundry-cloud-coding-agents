from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


class GitHubCredentialProvider(Protocol):
    def command_environment(self) -> Mapping[str, str] | None: ...


@dataclass(frozen=True)
class ActionsGitHubCredentialProvider:
    """Use the workflow-scoped github.token supplied as GH_TOKEN."""

    token: str | None = None

    def __post_init__(self) -> None:
        if self.token == "":
            raise ValueError("Actions GitHub token must not be empty")

    def command_environment(self) -> Mapping[str, str] | None:
        return {"GH_TOKEN": self.token} if self.token is not None else None


@dataclass(frozen=True)
class CopilotAssignmentCredentialProvider:
    """Scope the user credential to Copilot invocation and marker cleanup."""

    token: str

    def __post_init__(self) -> None:
        if not self.token:
            raise ValueError("Copilot assignment token is required")

    def command_environment(self) -> Mapping[str, str]:
        return {"GH_TOKEN": self.token}


__all__ = [
    "ActionsGitHubCredentialProvider",
    "CopilotAssignmentCredentialProvider",
    "GitHubCredentialProvider",
]
