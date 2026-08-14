from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit

from foundry_opt.preflight.interfaces import CommandRunner


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
)
_REPOSITORY_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_ASSIGNMENT_MARKER = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"
)
_NORMALIZED_COPILOT_LOGIN = "copilot-swe-agent[bot]"
_COPILOT_ACTOR_ID = 198982749
_GITHUB_WEB_FLOW_ID = 19864447


class WorkspaceCandidateImportEvent(StrEnum):
    ISSUE_COMMENT = "issue_comment"
    SCHEDULE = "schedule"


@dataclass(frozen=True)
class TrustedWorkspaceCandidateImportContext:
    repository: str
    workspace_pr_number: int
    candidate_source_commit_sha: str
    expected_state_revision: str
    importer_workflow_run_id: int
    trusted_event_name: WorkspaceCandidateImportEvent
    acknowledgement_comment_id: int | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.repository, str)
            or _REPOSITORY.fullmatch(self.repository) is None
        ):
            raise ValueError("trusted import repository is invalid")
        _positive_integer(
            self.workspace_pr_number,
            "workspace PR number",
        )
        _positive_integer(
            self.importer_workflow_run_id,
            "importer workflow run ID",
        )
        for value, name in (
            (
                self.candidate_source_commit_sha,
                "candidate source commit SHA",
            ),
            (self.expected_state_revision, "expected state revision"),
        ):
            if not isinstance(value, str) or _COMMIT.fullmatch(value) is None:
                raise ValueError(
                    f"{name} must be 40 lowercase hex characters"
                )
        try:
            event = WorkspaceCandidateImportEvent(self.trusted_event_name)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "trusted event is not supported for candidate import"
            ) from error
        if self.acknowledgement_comment_id is not None:
            _positive_integer(
                self.acknowledgement_comment_id,
                "acknowledgement comment ID",
            )
        if (
            event is WorkspaceCandidateImportEvent.ISSUE_COMMENT
            and self.acknowledgement_comment_id is None
        ):
            raise ValueError(
                "direct candidate import requires its comment ID"
            )
        object.__setattr__(self, "trusted_event_name", event)


@dataclass(frozen=True)
class WorkspaceCandidateProvenance:
    copilot_actor_id: int
    copilot_actor_login: str
    candidate_source_commit_sha: str
    candidate_source_commit_url: str
    acknowledgement_comment_id: int | None
    acknowledgement_comment_url: str | None
    assignment_marker_key: str
    workspace_pr_number: int
    importer_workflow_run_id: int
    importer_workflow_run_url: str
    trusted_event_name: WorkspaceCandidateImportEvent

    def __post_init__(self) -> None:
        for value, name in (
            (self.copilot_actor_id, "Copilot actor ID"),
            (self.workspace_pr_number, "workspace PR number"),
            (self.importer_workflow_run_id, "importer workflow run ID"),
        ):
            _positive_integer(value, name)
        if self.copilot_actor_id != _COPILOT_ACTOR_ID:
            raise ValueError("Copilot actor ID is not trusted")
        acknowledgement_present = (
            self.acknowledgement_comment_id is not None
        )
        if acknowledgement_present != (
            self.acknowledgement_comment_url is not None
        ):
            raise ValueError(
                "acknowledgement comment ID and URL must be present together"
            )
        if self.acknowledgement_comment_id is not None:
            _positive_integer(
                self.acknowledgement_comment_id,
                "acknowledgement comment ID",
            )
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
        if (
            event is WorkspaceCandidateImportEvent.ISSUE_COMMENT
            and not acknowledgement_present
        ):
            raise ValueError(
                "direct candidate import requires acknowledgement provenance"
            )

        source_repository = _validate_source_commit_url(
            self.candidate_source_commit_url,
            self.candidate_source_commit_sha,
        )
        acknowledgement_repository = (
            _validate_acknowledgement_url(
                self.acknowledgement_comment_url,
                self.workspace_pr_number,
                self.acknowledgement_comment_id,
            )
            if self.acknowledgement_comment_url is not None
            and self.acknowledgement_comment_id is not None
            else None
        )
        run_repository = _validate_workflow_run_url(
            self.importer_workflow_run_url,
            self.importer_workflow_run_id,
        )
        if (
            (
                acknowledgement_repository is not None
                and acknowledgement_repository != source_repository
            )
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
            workspace_candidate_provenance_document(self),
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


class GhWorkspaceCandidateProvenanceResolver:
    """Resolve candidate attribution only from live trusted GitHub metadata."""

    def __init__(
        self,
        commands: CommandRunner,
        *,
        repository_root: Path,
        max_comments: int = 100,
    ) -> None:
        if type(max_comments) is not int or not 1 <= max_comments <= 100:
            raise ValueError("candidate comment bound is invalid")
        self._commands = commands
        self._root = repository_root
        self._max_comments = max_comments

    def resolve(
        self,
        *,
        issue_number: int,
        candidate_id: str,
        context: TrustedWorkspaceCandidateImportContext,
    ) -> WorkspaceCandidateProvenance:
        _positive_integer(issue_number, "workspace issue number")
        if (
            not isinstance(candidate_id, str)
            or _IDENTIFIER.fullmatch(candidate_id) is None
        ):
            raise ValueError("workspace candidate ID is invalid")
        repository = context.repository
        pull_number = context.workspace_pr_number
        pull = self._object(
            ("gh", "api", f"repos/{repository}/pulls/{pull_number}")
        )
        head = pull.get("head")
        head_repository = (
            head.get("repo") if isinstance(head, Mapping) else None
        )
        if (
            pull.get("number") != pull_number
            or pull.get("state") != "open"
            or not isinstance(head, Mapping)
            or head.get("sha") != context.candidate_source_commit_sha
            or not isinstance(head_repository, Mapping)
            or head_repository.get("full_name") != repository
        ):
            raise ValueError(
                "trusted candidate source does not match workspace PR head"
            )

        commit = self._object(
            (
                "gh",
                "api",
                (
                    f"repos/{repository}/commits/"
                    f"{context.candidate_source_commit_sha}"
                ),
            )
        )
        actor = commit.get("author")
        if (
            commit.get("sha") != context.candidate_source_commit_sha
            or not isinstance(commit.get("html_url"), str)
            or not _trusted_copilot_actor(actor)
        ):
            raise ValueError("trusted Copilot commit actor is invalid")
        _validate_github_signed_copilot_commit(commit)
        assert isinstance(actor, Mapping)

        assignment_marker_key = workspace_assignment_marker_key(
            issue_number,
            context.expected_state_revision,
        )
        acknowledgement_marker = (
            "<!-- foundry-opt:workspace-candidate-ack:"
            f"{assignment_marker_key}:{candidate_id}:"
            f"{context.candidate_source_commit_sha} -->"
        )
        if (
            context.trusted_event_name
            is WorkspaceCandidateImportEvent.ISSUE_COMMENT
        ):
            assert context.acknowledgement_comment_id is not None
            acknowledgement = self._object(
                (
                    "gh",
                    "api",
                    (
                        f"repos/{repository}/issues/comments/"
                        f"{context.acknowledgement_comment_id}"
                    ),
                )
            )
            if (
                acknowledgement.get("id")
                != context.acknowledgement_comment_id
                or acknowledgement.get("body") != acknowledgement_marker
                or not _same_actor(acknowledgement.get("user"), actor)
            ):
                raise ValueError(
                    "trusted Copilot acknowledgement is invalid"
                )
        else:
            comments = self._comments(
                (
                    "gh",
                    "api",
                    (
                        f"repos/{repository}/issues/{pull_number}/comments?"
                        "per_page=100&sort=created&direction=desc"
                    ),
                )
            )
            matches = [
                item
                for item in comments
                if item.get("body") == acknowledgement_marker
            ]
            if len(matches) > 1:
                raise ValueError(
                    "trusted Copilot acknowledgement is ambiguous"
                )
            acknowledgement = matches[0] if matches else None
            if (
                acknowledgement is not None
                and not _same_actor(acknowledgement.get("user"), actor)
            ):
                raise ValueError(
                    "trusted Copilot acknowledgement actor is invalid"
                )
        comment_id = (
            acknowledgement.get("id")
            if acknowledgement is not None
            else None
        )
        comment_url = (
            acknowledgement.get("html_url")
            if acknowledgement is not None
            else None
        )
        if acknowledgement is not None and (
            type(comment_id) is not int
            or comment_id < 1
            or not isinstance(comment_url, str)
        ):
            raise ValueError("trusted Copilot acknowledgement is invalid")

        return WorkspaceCandidateProvenance(
            copilot_actor_id=actor["id"],
            copilot_actor_login=actor["login"],
            candidate_source_commit_sha=(
                context.candidate_source_commit_sha
            ),
            candidate_source_commit_url=commit["html_url"],
            acknowledgement_comment_id=comment_id,
            acknowledgement_comment_url=comment_url,
            assignment_marker_key=assignment_marker_key,
            workspace_pr_number=pull_number,
            importer_workflow_run_id=context.importer_workflow_run_id,
            importer_workflow_run_url=(
                f"https://github.com/{repository}/actions/runs/"
                f"{context.importer_workflow_run_id}"
            ),
            trusted_event_name=context.trusted_event_name,
        )

    def _object(self, arguments: tuple[str, ...]) -> Mapping[str, Any]:
        value = self._json(arguments)
        if not isinstance(value, Mapping):
            raise ValueError("trusted GitHub response is invalid")
        return value

    def _comments(
        self,
        arguments: tuple[str, ...],
    ) -> tuple[Mapping[str, Any], ...]:
        value = self._json(arguments)
        if (
            not isinstance(value, list)
            or any(not isinstance(item, Mapping) for item in value)
        ):
            raise ValueError(
                "trusted candidate comment history is invalid"
            )
        comments = tuple(value)
        if len(comments) > self._max_comments:
            raise ValueError(
                "trusted candidate comment history exceeds its bound"
            )
        return comments

    def _json(self, arguments: tuple[str, ...]) -> Any:
        result = self._commands.run(arguments, cwd=self._root)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ValueError("trusted GitHub response is invalid") from error


def workspace_assignment_marker_key(
    issue_number: int,
    assignment_revision: str,
) -> str:
    _positive_integer(issue_number, "workspace issue number")
    if (
        not isinstance(assignment_revision, str)
        or not assignment_revision
        or len(assignment_revision) > 256
        or any(ord(character) < 32 for character in assignment_revision)
    ):
        raise ValueError("workspace assignment revision is invalid")
    return (
        f"issue-{issue_number}:"
        f"{hashlib.sha256(assignment_revision.encode('utf-8')).hexdigest()[:16]}"
        ":v1"
    )


def workspace_candidate_provenance_document(
    provenance: WorkspaceCandidateProvenance,
) -> dict[str, Any]:
    if type(provenance) is not WorkspaceCandidateProvenance:
        raise ValueError("workspace candidate provenance is invalid")
    return {
        "acknowledgement_comment_id": (
            provenance.acknowledgement_comment_id
        ),
        "acknowledgement_comment_url": (
            provenance.acknowledgement_comment_url
        ),
        "assignment_marker_key": provenance.assignment_marker_key,
        "candidate_source_commit_sha": (
            provenance.candidate_source_commit_sha
        ),
        "candidate_source_commit_url": (
            provenance.candidate_source_commit_url
        ),
        "copilot_actor_id": provenance.copilot_actor_id,
        "copilot_actor_login": provenance.copilot_actor_login,
        "importer_workflow_run_id": provenance.importer_workflow_run_id,
        "importer_workflow_run_url": (
            provenance.importer_workflow_run_url
        ),
        "schema_version": 1,
        "trusted_event_name": provenance.trusted_event_name.value,
        "workspace_pr_number": provenance.workspace_pr_number,
    }


def parse_workspace_candidate_provenance(
    value: Any,
) -> WorkspaceCandidateProvenance:
    if not isinstance(value, Mapping) or set(value) != {
        "acknowledgement_comment_id",
        "acknowledgement_comment_url",
        "assignment_marker_key",
        "candidate_source_commit_sha",
        "candidate_source_commit_url",
        "copilot_actor_id",
        "copilot_actor_login",
        "importer_workflow_run_id",
        "importer_workflow_run_url",
        "schema_version",
        "trusted_event_name",
        "workspace_pr_number",
    }:
        raise ValueError("workspace candidate provenance fields are invalid")
    if value["schema_version"] != 1:
        raise ValueError(
            "workspace candidate provenance schema version is invalid"
        )
    try:
        return WorkspaceCandidateProvenance(
            copilot_actor_id=value["copilot_actor_id"],
            copilot_actor_login=value["copilot_actor_login"],
            candidate_source_commit_sha=value[
                "candidate_source_commit_sha"
            ],
            candidate_source_commit_url=value[
                "candidate_source_commit_url"
            ],
            acknowledgement_comment_id=value[
                "acknowledgement_comment_id"
            ],
            acknowledgement_comment_url=value[
                "acknowledgement_comment_url"
            ],
            assignment_marker_key=value["assignment_marker_key"],
            workspace_pr_number=value["workspace_pr_number"],
            importer_workflow_run_id=value["importer_workflow_run_id"],
            importer_workflow_run_url=value["importer_workflow_run_url"],
            trusted_event_name=value["trusted_event_name"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("workspace candidate provenance is invalid") from error


def trusted_workspace_candidate_import_context_from_environment(
    environ: Mapping[str, str] | None = None,
) -> TrustedWorkspaceCandidateImportContext | None:
    values = os.environ if environ is None else environ
    origin = values.get("TRUSTED_CANDIDATE_IMPORT_ORIGIN", "")
    if not origin:
        return None
    if values.get("GITHUB_ACTIONS") != "true":
        raise ValueError(
            "trusted candidate import requires GitHub Actions context"
        )
    try:
        pull_request_number = int(
            values.get("TRUSTED_PULL_REQUEST_NUMBER", "")
        )
        run_id = int(values.get("TRUSTED_RUN_ID", ""))
        comment_value = values.get("TRUSTED_ACK_COMMENT_ID", "")
        comment_id = int(comment_value) if comment_value else None
    except ValueError as error:
        raise ValueError(
            "trusted candidate import numeric context is invalid"
        ) from error
    return TrustedWorkspaceCandidateImportContext(
        repository=values.get("TRUSTED_REPOSITORY", ""),
        workspace_pr_number=pull_request_number,
        candidate_source_commit_sha=values.get(
            "TRUSTED_HEAD_SHA",
            "",
        ),
        expected_state_revision=values.get(
            "TRUSTED_EXPECTED_REVISION",
            "",
        ),
        importer_workflow_run_id=run_id,
        trusted_event_name=origin,
        acknowledgement_comment_id=comment_id,
    )


def _trusted_copilot_actor(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("login") in {"Copilot", _NORMALIZED_COPILOT_LOGIN}
        and value.get("id") == _COPILOT_ACTOR_ID
        and value.get("type") == "Bot"
    )


def _validate_github_signed_copilot_commit(
    value: Mapping[str, Any],
) -> None:
    committer = value.get("committer")
    if (
        not isinstance(committer, Mapping)
        or committer.get("login") != "web-flow"
        or committer.get("id") != _GITHUB_WEB_FLOW_ID
        or committer.get("type") != "User"
    ):
        raise ValueError(
            "trusted Copilot commit committer is not GitHub web-flow"
        )
    commit = value.get("commit")
    verification = (
        commit.get("verification")
        if isinstance(commit, Mapping)
        else None
    )
    if (
        not isinstance(verification, Mapping)
        or verification.get("verified") is not True
        or verification.get("reason") != "valid"
    ):
        raise ValueError(
            "trusted Copilot commit verification is not valid"
        )
    for field in ("signature", "payload"):
        content = verification.get(field)
        if not isinstance(content, str) or not content.strip():
            raise ValueError(
                f"trusted Copilot commit verification {field} is missing"
            )


def _same_actor(value: Any, expected: Mapping[str, Any]) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("id") == expected.get("id")
        and value.get("login") == expected.get("login")
        and value.get("type") == "Bot"
    )


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
    "GhWorkspaceCandidateProvenanceResolver",
    "TrustedWorkspaceCandidateImportContext",
    "WorkspaceCandidateImportEvent",
    "WorkspaceCandidateProvenance",
    "parse_workspace_candidate_provenance",
    "trusted_workspace_candidate_import_context_from_environment",
    "workspace_assignment_marker_key",
    "workspace_candidate_provenance_document",
]
