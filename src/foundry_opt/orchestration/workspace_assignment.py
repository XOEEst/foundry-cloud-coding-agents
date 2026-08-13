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
        actor = self._api(("gh", "api", "user"))
        if not _trusted_user(actor):
            raise RuntimeError(
                "workspace assignment author is invalid"
            )
        marker = (
            "<!-- foundry-opt:workspace-copilot-assignment:"
            f"issue-{issue_number}:v1 -->"
        )
        pages = self._api(
            (
                "gh",
                "api",
                "--paginate",
                "--slurp",
                (
                    f"{endpoint}/comments?per_page=100"
                ),
            )
        )
        comments = _comments(pages)
        matches = [
            item
            for item in comments
            if isinstance(item.get("body"), str)
            and marker in item["body"]
        ]
        if len(matches) > 1:
            raise RuntimeError(
                "workspace assignment marker is ambiguous"
            )
        if matches:
            if not _same_user(matches[0].get("user"), actor):
                raise RuntimeError(
                    "workspace assignment marker is untrusted"
                )
            return False
        body = (
            f"{marker}\n"
            "@copilot Continue this existing workspace pull request "
            f"#{pull_request_number} for optimization issue "
            f"#{issue_number}. Read and follow "
            f"`.github/agents/{_CUSTOM_AGENT}.agent.md`. Run "
            f"`foundry-opt workspace advance --issue {issue_number} "
            "--json`, perform only returned candidate-work next actions, "
            "and do not create another issue or pull request."
        )
        response = self._api(
            (
                "gh",
                "api",
                "--method",
                "POST",
                f"{endpoint}/comments",
                "--input",
                "-",
            ),
            input_document={"body": body},
        )
        if (
            not isinstance(response, Mapping)
            or response.get("body") != body
            or not _same_user(response.get("user"), actor)
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


def _trusted_user(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and isinstance(value.get("login"), str)
        and bool(value["login"])
        and type(value.get("id")) is int
        and value["id"] > 0
        and value.get("type") == "User"
    )


def _same_user(value: Any, expected: Mapping[str, Any]) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("login") == expected.get("login")
        and value.get("id") == expected.get("id")
        and value.get("type") == expected.get("type")
    )


def _comments(value: Any) -> list[Mapping[str, Any]]:
    if (
        not isinstance(value, list)
        or any(not isinstance(page, list) for page in value)
        or any(
            not isinstance(item, Mapping)
            for page in value
            if isinstance(page, list)
            for item in page
        )
    ):
        raise RuntimeError(
            "workspace assignment comments are invalid"
        )
    comments = [
        item
        for page in value
        for item in page
    ]
    if len(comments) > 10_000:
        raise RuntimeError(
            "workspace assignment comment history is too large"
        )
    return comments


__all__ = ["GhWorkspaceCopilotAssigner"]
