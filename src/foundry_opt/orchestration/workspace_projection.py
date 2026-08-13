from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Protocol

from foundry_opt.orchestration.public_evidence import (
    OptimizationReport,
    PublicEvidenceRenderer,
)
from foundry_opt.orchestration.workspace import (
    WorkspaceIssueStatusProjectionIntent,
    WorkspacePhase,
)
from foundry_opt.preflight.interfaces import CommandRunner


_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
)
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_TRUSTED_AUTHORS = frozenset(
    {"github-actions[bot]", "copilot-swe-agent[bot]"}
)


@dataclass(frozen=True)
class WorkspaceIssueProjectionResult:
    issue_number: int
    status_changed: bool
    created_milestones: tuple[str, ...]


class WorkspaceIssueProjector(Protocol):
    def project(
        self,
        intent: WorkspaceIssueStatusProjectionIntent,
        *,
        base_commit: str,
        report: OptimizationReport | None = None,
    ) -> WorkspaceIssueProjectionResult: ...


class GhWorkspaceIssueProjector:
    """Publish deterministic status and append-only workspace milestones."""

    def __init__(
        self,
        commands: CommandRunner,
        *,
        repository_root: Path,
        repository: str,
        renderer: PublicEvidenceRenderer | None = None,
    ) -> None:
        if _REPOSITORY.fullmatch(repository) is None:
            raise ValueError("workspace projection repository is invalid")
        self._commands = commands
        self._root = repository_root
        self._repository = repository
        self._renderer = renderer or PublicEvidenceRenderer()
        self._authenticated_author: Mapping[str, Any] | None = None
        self._authenticated_author_loaded = False

    def project(
        self,
        intent: WorkspaceIssueStatusProjectionIntent,
        *,
        base_commit: str,
        report: OptimizationReport | None = None,
    ) -> WorkspaceIssueProjectionResult:
        if _COMMIT.fullmatch(base_commit) is None:
            raise ValueError("workspace projection base commit is invalid")
        if report is not None and report.issue_number != intent.issue_number:
            raise ValueError("workspace projection report issue changed")
        comments = self._comments(intent.issue_number)
        status_changed = self._upsert_status(
            comments,
            intent,
            _status_body(intent),
        )
        created: list[str] = []
        for name, marker, body in _milestones(
            intent,
            base_commit=base_commit,
            report=report,
            renderer=self._renderer,
        ):
            if self._append_milestone(
                comments,
                intent.issue_number,
                marker,
                body,
            ):
                created.append(name)
        return WorkspaceIssueProjectionResult(
            issue_number=intent.issue_number,
            status_changed=status_changed,
            created_milestones=tuple(created),
        )

    def _comments(self, issue_number: int) -> list[dict[str, Any]]:
        response = self._json(
            (
                "gh",
                "api",
                "--paginate",
                "--slurp",
                (
                    f"repos/{self._repository}/issues/"
                    f"{issue_number}/comments?per_page=100"
                ),
            )
        )
        if (
            not isinstance(response, list)
            or len(response) > 100
            or any(not isinstance(page, list) for page in response)
            or any(
                not isinstance(item, dict)
                for page in response
                if isinstance(page, list)
                for item in page
            )
        ):
            raise RuntimeError(
                "workspace issue comments response is invalid"
            )
        comments = [
            item
            for page in response
            for item in page
        ]
        if len(comments) > 10_000:
            raise RuntimeError("workspace issue comment history is too large")
        return comments

    def _upsert_status(
        self,
        comments: list[dict[str, Any]],
        intent: WorkspaceIssueStatusProjectionIntent,
        body: str,
    ) -> bool:
        marker = _status_marker(intent.issue_number)
        existing = _marked_comment(
            comments,
            marker,
            self._author_is_trusted,
        )
        if existing is None:
            self._write(
                "POST",
                (
                    f"repos/{self._repository}/issues/"
                    f"{intent.issue_number}/comments"
                ),
                body,
            )
            return True
        if existing["body"] == body:
            return False
        self._write(
            "PATCH",
            (
                f"repos/{self._repository}/issues/comments/"
                f"{existing['id']}"
            ),
            body,
        )
        return True

    def _append_milestone(
        self,
        comments: list[dict[str, Any]],
        issue_number: int,
        marker: str,
        body: str,
    ) -> bool:
        existing = _marked_comment(
            comments,
            marker,
            self._author_is_trusted,
        )
        if existing is not None:
            if existing["body"] != body:
                raise RuntimeError(
                    "workspace immutable milestone changed"
                )
            return False
        self._write(
            "POST",
            (
                f"repos/{self._repository}/issues/"
                f"{issue_number}/comments"
            ),
            body,
        )
        return True

    def _write(self, method: str, endpoint: str, body: str) -> None:
        response = self._json(
            (
                "gh",
                "api",
                "--method",
                method,
                endpoint,
                "--input",
                "-",
            ),
            input_document={"body": body},
        )
        if (
            not isinstance(response, Mapping)
            or response.get("body") != body
            or type(response.get("id")) is not int
            or response["id"] < 1
            or not self._author_is_trusted(response.get("user"))
        ):
            raise RuntimeError(
                "workspace issue projection was not confirmed"
            )

    def _author_is_trusted(self, value: Any) -> bool:
        if _trusted_comment_author(value):
            return True
        actor = self._current_authenticated_author()
        return (
            isinstance(value, Mapping)
            and value.get("login") == actor.get("login")
            and value.get("id") == actor.get("id")
            and value.get("type") == actor.get("type")
        )

    def _current_authenticated_author(self) -> Mapping[str, Any]:
        if not self._authenticated_author_loaded:
            response = self._json(("gh", "api", "user"))
            if (
                not isinstance(response, Mapping)
                or not isinstance(response.get("login"), str)
                or not response["login"]
                or type(response.get("id")) is not int
                or response["id"] < 1
                or response.get("type") not in {"Bot", "User"}
            ):
                raise RuntimeError(
                    "workspace projection authenticated author is invalid"
                )
            self._authenticated_author = response
            self._authenticated_author_loaded = True
        assert self._authenticated_author is not None
        return self._authenticated_author

    def _json(
        self,
        arguments: tuple[str, ...],
        *,
        input_document: Mapping[str, Any] | None = None,
    ) -> Any:
        result = self._commands.run(
            arguments,
            cwd=self._root,
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
                "workspace issue projection response is invalid"
            ) from error


def _marked_comment(
    comments: list[dict[str, Any]],
    marker: str,
    author_is_trusted: Callable[[Any], bool],
) -> dict[str, Any] | None:
    matches = [
        item
        for item in comments
        if isinstance(item.get("body"), str)
        and (
            item["body"] == marker
            or item["body"].startswith(f"{marker}\n")
        )
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise RuntimeError("workspace issue projection marker is ambiguous")
    comment = matches[0]
    user = comment.get("user")
    if (
        type(comment.get("id")) is not int
        or comment["id"] < 1
        or not author_is_trusted(user)
    ):
        raise RuntimeError("workspace issue projection marker is untrusted")
    return comment


def _trusted_comment_author(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    login = value.get("login")
    if login in _TRUSTED_AUTHORS:
        return True
    return (
        login == "Copilot"
        and value.get("id") == 198982749
        and value.get("type") == "Bot"
    )


def _status_marker(issue_number: int) -> str:
    return (
        "<!-- foundry-opt:workspace-status:"
        f"issue-{issue_number}:v1 -->"
    )


def _milestone_marker(issue_number: int, name: str) -> str:
    return (
        "<!-- foundry-opt:workspace-milestone:"
        f"issue-{issue_number}:{name}:v1 -->"
    )


def _status_body(intent: WorkspaceIssueStatusProjectionIntent) -> str:
    next_status = {
        WorkspacePhase.SPECIFICATION: (
            "Copilot candidate preparation is ready."
        ),
        WorkspacePhase.EVALUATING: (
            "Trusted candidate experiments are in progress."
        ),
        WorkspacePhase.AWAITING_SELECTION: (
            "Candidate evidence is ready for human merge review."
        ),
        WorkspacePhase.DEPLOYMENT: (
            "The merged candidate is awaiting trusted deployment."
        ),
        WorkspacePhase.RETENTION: (
            "Deployment completed; retained-improvement checks are pending."
        ),
        WorkspacePhase.COMPLETED: "Optimization is complete.",
    }[intent.phase]
    return "\n".join(
        (
            _status_marker(intent.issue_number),
            "## Current Foundry optimization status",
            "",
            f"- Phase: `{intent.phase.value}`",
            (
                "- Workspace pull request: "
                f"#{intent.workspace_pull_request_number}"
            ),
            f"- Status: {next_status}",
        )
    )


def _milestones(
    intent: WorkspaceIssueStatusProjectionIntent,
    *,
    base_commit: str,
    report: OptimizationReport | None,
    renderer: PublicEvidenceRenderer,
) -> tuple[tuple[str, str, str], ...]:
    rank = {
        WorkspacePhase.SPECIFICATION: 0,
        WorkspacePhase.EVALUATING: 1,
        WorkspacePhase.AWAITING_SELECTION: 2,
        WorkspacePhase.DEPLOYMENT: 3,
        WorkspacePhase.RETENTION: 4,
        WorkspacePhase.COMPLETED: 5,
    }[intent.phase]
    milestones: list[tuple[str, str, str]] = []
    specification = _milestone_marker(
        intent.issue_number,
        "specification",
    )
    milestones.append(
        (
            "specification",
            specification,
            "\n".join(
                (
                    specification,
                    "## Specification and baseline milestone",
                    "",
                    (
                        f"Workspace PR "
                        f"#{intent.workspace_pull_request_number} was "
                        f"established at approved base `{base_commit}`."
                    ),
                )
            ),
        )
    )
    if rank >= 1:
        experiments = _milestone_marker(
            intent.issue_number,
            "experiments",
        )
        milestones.append(
            (
                "experiments",
                experiments,
                "\n".join(
                    (
                        experiments,
                        "## Trusted experiments milestone",
                        "",
                        (
                            "Candidate experiments entered trusted "
                            "evaluation in the same workspace PR "
                            f"#{intent.workspace_pull_request_number}."
                        ),
                    )
                ),
            )
        )
    if report is not None:
        projection = renderer.render_issue(report)
        milestones.append(
            ("candidate_ready", projection.marker, projection.body)
        )
    return tuple(milestones)


__all__ = [
    "GhWorkspaceIssueProjector",
    "WorkspaceIssueProjectionResult",
    "WorkspaceIssueProjector",
]
