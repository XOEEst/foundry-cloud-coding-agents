from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import re
from typing import Any

from foundry_opt.preflight.interfaces import CommandRunner


_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
)
_COPILOT = "copilot-swe-agent[bot]"
_CUSTOM_AGENT = "foundry-optimization-steward"


class GhWorkspaceCopilotAssigner:
    """Assign Copilot only to an existing single-workspace pull request."""

    def __init__(
        self,
        commands: CommandRunner,
        *,
        repository_root: Path,
        repository: str,
        assignment_token: str,
    ) -> None:
        if _REPOSITORY.fullmatch(repository) is None:
            raise ValueError("workspace assignment repository is invalid")
        if not assignment_token:
            raise ValueError("Copilot assignment token is required")
        self._commands = commands
        self._root = repository_root
        self._repository = repository
        self._assignment_environment = {"GH_TOKEN": assignment_token}

    def assign(
        self,
        *,
        issue_number: int,
        pull_request_number: int,
    ) -> bool:
        _positive(issue_number, "workspace issue")
        _positive(pull_request_number, "workspace pull request")
        endpoint = (
            f"repos/{self._repository}/issues/{pull_request_number}"
        )
        pull_request = self._api(("gh", "api", endpoint))
        if (
            not isinstance(pull_request, Mapping)
            or pull_request.get("number") != pull_request_number
            or pull_request.get("state") != "open"
            or not isinstance(pull_request.get("pull_request"), Mapping)
        ):
            raise RuntimeError(
                "workspace assignment target is not an open pull request"
            )
        assignees = pull_request.get("assignees")
        if not isinstance(assignees, list):
            raise RuntimeError(
                "workspace pull request assignees are invalid"
            )
        if any(
            isinstance(item, Mapping)
            and item.get("login") == _COPILOT
            for item in assignees
        ):
            return False
        body = {
            "agent_assignment": {
                "custom_agent": _CUSTOM_AGENT,
                "custom_instructions": (
                    f"Continue optimization issue #{issue_number} in "
                    f"this existing workspace pull request "
                    f"#{pull_request_number} only. Run `foundry-opt "
                    f"workspace advance --issue {issue_number} --json`, "
                    "follow only returned candidate-work next actions, "
                    "and do not create another issue or pull request."
                ),
                "target_repo": self._repository,
            },
            "assignees": [_COPILOT],
        }
        response = self._api(
            (
                "gh",
                "api",
                "--method",
                "POST",
                f"{endpoint}/assignees",
                "--input",
                "-",
            ),
            input_document=body,
        )
        assigned = (
            response.get("assignees")
            if isinstance(response, Mapping)
            else None
        )
        if (
            not isinstance(assigned, list)
            or not any(
                isinstance(item, Mapping)
                and item.get("login") == _COPILOT
                for item in assigned
            )
        ):
            raise RuntimeError(
                "workspace Copilot assignment was not confirmed"
            )
        return True

    def _api(
        self,
        arguments: tuple[str, ...],
        *,
        input_document: Mapping[str, Any] | None = None,
    ) -> Any:
        result = self._commands.run(
            arguments,
            cwd=self._root,
            environment=self._assignment_environment,
            input_text=(
                json.dumps(
                    input_document,
                    ensure_ascii=True,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                if input_document is not None
                else None
            ),
        )
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                "workspace assignment response is invalid"
            ) from error


def _positive(value: int, name: str) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} is invalid")


__all__ = ["GhWorkspaceCopilotAssigner"]
