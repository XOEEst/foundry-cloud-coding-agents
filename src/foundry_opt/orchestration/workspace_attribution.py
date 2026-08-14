from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import re
from urllib.parse import urlsplit


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_ASSIGNMENT_MARKER = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"
)
_NORMALIZED_COPILOT_LOGIN = "copilot-swe-agent[bot]"


class WorkspaceCandidateImportEvent(StrEnum):
    ISSUE_COMMENT = "issue_comment"
    SCHEDULE = "schedule"


@dataclass(frozen=True)
class WorkspaceCandidateProvenance:
    copilot_actor_id: int
    copilot_actor_login: str
    candidate_source_commit_sha: str
    candidate_source_commit_url: str
    acknowledgement_comment_id: int
    acknowledgement_comment_url: str
    assignment_marker_key: str
    workspace_pr_number: int
    importer_workflow_run_id: int
    importer_workflow_run_url: str
    trusted_event_name: WorkspaceCandidateImportEvent

    def __post_init__(self) -> None:
        for value, name in (
            (self.copilot_actor_id, "Copilot actor ID"),
            (
                self.acknowledgement_comment_id,
                "acknowledgement comment ID",
            ),
            (self.workspace_pr_number, "workspace PR number"),
            (self.importer_workflow_run_id, "importer workflow run ID"),
        ):
            _positive_integer(value, name)
        login = self.copilot_actor_login
        if login == "app/copilot-swe-agent":
            login = _NORMALIZED_COPILOT_LOGIN
        if login not in {"Copilot", _NORMALIZED_COPILOT_LOGIN}:
            raise ValueError("Copilot actor login is not trusted")
        object.__setattr__(self, "copilot_actor_login", login)
        if (
            not isinstance(self.candidate_source_commit_sha, str)
            or _COMMIT.fullmatch(self.candidate_source_commit_sha) is None
        ):
            raise ValueError(
                "candidate source commit SHA must be 40 lowercase hex characters"
            )
        if (
            not isinstance(self.assignment_marker_key, str)
            or _ASSIGNMENT_MARKER.fullmatch(self.assignment_marker_key)
            is None
        ):
            raise ValueError(
                "assignment marker key must be a bounded safe string"
            )
        try:
            event = WorkspaceCandidateImportEvent(self.trusted_event_name)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "trusted event is not supported for candidate import"
            ) from error
        object.__setattr__(self, "trusted_event_name", event)

        source_repository = _validate_source_commit_url(
            self.candidate_source_commit_url,
            self.candidate_source_commit_sha,
        )
        acknowledgement_repository = _validate_acknowledgement_url(
            self.acknowledgement_comment_url,
            self.workspace_pr_number,
            self.acknowledgement_comment_id,
        )
        run_repository = _validate_workflow_run_url(
            self.importer_workflow_run_url,
            self.importer_workflow_run_id,
        )
        if (
            acknowledgement_repository != source_repository
            or run_repository != source_repository
        ):
            raise ValueError(
                "candidate attribution URLs must use the same repository"
            )

    @property
    def repository(self) -> str:
        parts = urlsplit(self.candidate_source_commit_url).path.split("/")
        return f"{parts[1]}/{parts[2]}"

    @property
    def canonical_json(self) -> str:
        return json.dumps(
            {
                "acknowledgement_comment_id": (
                    self.acknowledgement_comment_id
                ),
                "acknowledgement_comment_url": (
                    self.acknowledgement_comment_url
                ),
                "assignment_marker_key": self.assignment_marker_key,
                "candidate_source_commit_sha": (
                    self.candidate_source_commit_sha
                ),
                "candidate_source_commit_url": (
                    self.candidate_source_commit_url
                ),
                "copilot_actor_id": self.copilot_actor_id,
                "copilot_actor_login": self.copilot_actor_login,
                "importer_workflow_run_id": self.importer_workflow_run_id,
                "importer_workflow_run_url": (
                    self.importer_workflow_run_url
                ),
                "schema_version": 1,
                "trusted_event_name": self.trusted_event_name.value,
                "workspace_pr_number": self.workspace_pr_number,
            },
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @property
    def identity_sha256(self) -> str:
        return hashlib.sha256(
            self.canonical_json.encode("utf-8")
        ).hexdigest()


def _positive_integer(value: int, name: str) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _canonical_github_path(
    value: str,
    name: str,
    *,
    allowed_fragment: str = "",
) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a canonical HTTPS GitHub URL")
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise ValueError(
            f"{name} must be a canonical HTTPS GitHub URL"
        ) from error
    parts = tuple(parsed.path.removeprefix("/").split("/"))
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.query
        or parsed.fragment != allowed_fragment
        or len(parts) < 3
        or _REPOSITORY_PART.fullmatch(parts[0]) is None
        or _REPOSITORY_PART.fullmatch(parts[1]) is None
        or value
        != (
            f"https://github.com/{'/'.join(parts)}"
            + (f"#{allowed_fragment}" if allowed_fragment else "")
        )
    ):
        raise ValueError(f"{name} must be a canonical HTTPS GitHub URL")
    return parts


def _validate_source_commit_url(value: str, sha: str) -> str:
    parts = _canonical_github_path(value, "source commit URL")
    if len(parts) != 4 or parts[2:] != ("commit", sha):
        raise ValueError(
            "source commit URL must match the candidate source commit SHA"
        )
    return f"{parts[0]}/{parts[1]}"


def _validate_acknowledgement_url(
    value: str,
    pull_request_number: int,
    comment_id: int,
) -> str:
    fragment = f"issuecomment-{comment_id}"
    parts = _canonical_github_path(
        value,
        "acknowledgement comment URL",
        allowed_fragment=fragment,
    )
    if (
        len(parts) != 4
        or parts[2:] != ("pull", str(pull_request_number))
    ):
        raise ValueError(
            "acknowledgement comment URL must match the PR and comment IDs"
        )
    return f"{parts[0]}/{parts[1]}"


def _validate_workflow_run_url(value: str, run_id: int) -> str:
    parts = _canonical_github_path(value, "workflow run URL")
    if len(parts) != 5 or parts[2:] != ("actions", "runs", str(run_id)):
        raise ValueError("workflow run URL must match the workflow run ID")
    return f"{parts[0]}/{parts[1]}"


__all__ = [
    "WorkspaceCandidateImportEvent",
    "WorkspaceCandidateProvenance",
]
