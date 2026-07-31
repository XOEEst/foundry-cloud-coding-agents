from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping, Protocol
from uuid import uuid4

from foundry_opt.adapters.commands import SubprocessCommandRunner
from foundry_opt.orchestration.models import CampaignEvent, EventKind
from foundry_opt.preflight.interfaces import CommandRunner


_DELIVERY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,126}$")
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
)
_ACTIONS = {
    "opened": EventKind.ISSUE_CREATED,
    "edited": EventKind.ISSUE_EDITED,
    "reopened": EventKind.ISSUE_REOPENED,
    "closed": EventKind.ISSUE_CLOSED,
}


class TrustedIssueEventError(ValueError):
    pass


class IssueInboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class TrustedEventContext:
    event_name: str
    delivery_id: str
    repository: str
    repository_id: int

    def __post_init__(self) -> None:
        if not _DELIVERY_ID.fullmatch(self.delivery_id):
            raise TrustedIssueEventError("delivery ID is invalid")
        if not _REPOSITORY.fullmatch(self.repository):
            raise TrustedIssueEventError("repository is invalid")
        if (
            type(self.repository_id) is not int
            or self.repository_id < 1
        ):
            raise TrustedIssueEventError("repository ID is invalid")


@dataclass(frozen=True)
class IntakeResult:
    event: CampaignEvent
    recorded: bool


class IssueEventInbox(Protocol):
    def events(self, issue_number: int) -> tuple[CampaignEvent, ...]: ...

    def append(
        self,
        issue_number: int,
        event: CampaignEvent,
    ) -> bool: ...

    def issue_numbers(self) -> tuple[int, ...]: ...


class StewardAssignments(Protocol):
    def assign(
        self,
        issue_number: int,
        idempotency_key: str,
    ) -> bool: ...


class IssueProjection(Protocol):
    def project(self, issue_number: int) -> None: ...


class IssueEventIntake:
    """Transport issue events without making campaign decisions."""

    def __init__(
        self,
        inbox: IssueEventInbox,
        assignments: StewardAssignments,
        projection: IssueProjection,
    ) -> None:
        self._inbox = inbox
        self._assignments = assignments
        self._projection = projection

    def ingest(
        self,
        payload: Mapping[str, Any],
        context: TrustedEventContext,
    ) -> IntakeResult:
        issue_number, action, title, occurred_at = _validate_payload(
            payload,
            context,
        )
        event_id = f"github-{context.delivery_id}"
        existing = self._inbox.events(issue_number)
        kind = _event_kind(action, title, existing)
        duplicate = next(
            (item for item in existing if item.event_id == event_id),
            None,
        )
        if duplicate is not None:
            if duplicate.kind is not kind:
                raise TrustedIssueEventError(
                    "delivery ID was reused for another action"
                )
            self._assignments.assign(
                issue_number,
                duplicate.event_id,
            )
            self._projection.project(issue_number)
            return IntakeResult(duplicate, False)

        generation = _generation(kind, existing)
        event = CampaignEvent(
            event_id=event_id,
            kind=kind,
            generation=generation,
            occurred_at=occurred_at,
        )
        recorded = self._inbox.append(issue_number, event)
        if not recorded:
            return IntakeResult(event, False)
        self._assignments.assign(issue_number, event.event_id)
        self._projection.project(issue_number)
        return IntakeResult(event, True)

    def recover(self, trigger_id: str) -> None:
        _identifier(trigger_id, "recovery trigger ID")
        for issue_number in self._inbox.issue_numbers():
            events = self._inbox.events(issue_number)
            if not events:
                continue
            self._assignments.assign(
                issue_number,
                f"{trigger_id}-issue-{issue_number}",
            )
            self._projection.project(issue_number)


class GitIssueEventInbox:
    """Append-only trusted issue events on steward-owned Git refs."""

    def __init__(
        self,
        repository_root: Path,
        *,
        remote: str = "origin",
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", remote):
            raise ValueError("remote is invalid")
        self._root = repository_root.resolve()
        self._remote = remote
        _git(self._root, "rev-parse", "--show-toplevel")

    def events(self, issue_number: int) -> tuple[CampaignEvent, ...]:
        _, events = self._load(issue_number)
        return events

    def append(
        self,
        issue_number: int,
        event: CampaignEvent,
    ) -> bool:
        ref = _inbox_ref(issue_number)
        revision, events = self._load(issue_number)
        duplicate = next(
            (item for item in events if item.event_id == event.event_id),
            None,
        )
        if duplicate is not None:
            if duplicate != event:
                raise IssueInboxError(
                    "event ID already contains different content"
                )
            return False
        _require_transport_event(event)
        _validate_transport_sequence((*events, event))
        path = (
            f"events/{len(events) + 1:08d}-{event.event_id}.json"
        )
        content = _event_bytes(event)
        git_dir_text = _git_text(
            self._root,
            "rev-parse",
            "--git-dir",
        )
        git_dir = Path(git_dir_text)
        if not git_dir.is_absolute():
            git_dir = self._root / git_dir
        index = git_dir / f"foundry-inbox-{os.getpid()}-{uuid4().hex}"
        environment = {"GIT_INDEX_FILE": str(index)}
        try:
            _git(
                self._root,
                "read-tree",
                "--empty" if revision is None else f"{revision}^{{tree}}",
                environment=environment,
            )
            blob = _git(
                self._root,
                "hash-object",
                "-w",
                "--stdin",
                input_bytes=content,
            ).decode("ascii").strip()
            _git(
                self._root,
                "update-index",
                "--add",
                "--cacheinfo",
                "100644",
                blob,
                path,
                environment=environment,
            )
            tree = _git_text(
                self._root,
                "write-tree",
                environment=environment,
            )
            arguments = ["commit-tree", tree]
            if revision is not None:
                arguments.extend(("-p", revision))
            arguments.extend(
                ("-m", f"Record issue-{issue_number} event")
            )
            commit = _git_text(
                self._root,
                *arguments,
                environment={
                    **environment,
                    "GIT_AUTHOR_NAME": "Foundry Optimizer Intake",
                    "GIT_AUTHOR_EMAIL": "foundry-opt@example.invalid",
                    "GIT_COMMITTER_NAME": "Foundry Optimizer Intake",
                    "GIT_COMMITTER_EMAIL": "foundry-opt@example.invalid",
                },
            )
        finally:
            index.unlink(missing_ok=True)
            Path(f"{index}.lock").unlink(missing_ok=True)
        lease = f"--force-with-lease={ref}:{revision or ''}"
        _git(
            self._root,
            "push",
            lease,
            self._remote,
            f"{commit}:{ref}",
        )
        return True

    def issue_numbers(self) -> tuple[int, ...]:
        prefix = "refs/heads/foundry-opt/inbox/issue-"
        output = _git_text(
            self._root,
            "ls-remote",
            "--heads",
            self._remote,
            f"{prefix}*",
        )
        numbers: list[int] = []
        for line in output.splitlines():
            fields = line.split()
            if len(fields) != 2 or not fields[1].startswith(prefix):
                raise IssueInboxError("inbox ref metadata is invalid")
            suffix = fields[1][len(prefix):]
            if not suffix.isdecimal() or int(suffix) < 1:
                raise IssueInboxError("inbox ref issue is invalid")
            numbers.append(int(suffix))
        return tuple(sorted(numbers))

    def _load(
        self,
        issue_number: int,
    ) -> tuple[str | None, tuple[CampaignEvent, ...]]:
        ref = _inbox_ref(issue_number)
        output = _git_text(
            self._root,
            "ls-remote",
            "--heads",
            self._remote,
            ref,
        )
        if not output:
            return None, ()
        fields = output.split()
        if (
            len(fields) != 2
            or fields[1] != ref
            or not re.fullmatch(r"[0-9a-f]{40}", fields[0])
        ):
            raise IssueInboxError("inbox ref metadata is invalid")
        revision = fields[0]
        _git(
            self._root,
            "fetch",
            "--quiet",
            self._remote,
            ref,
        )
        fetched = _git_text(
            self._root,
            "rev-parse",
            "FETCH_HEAD^{commit}",
        )
        if fetched != revision:
            raise IssueInboxError("inbox ref changed while loading")
        paths = tuple(
            line
            for line in _git_text(
                self._root,
                "ls-tree",
                "-r",
                "--name-only",
                revision,
            ).splitlines()
            if line
        )
        if any(
            re.fullmatch(
                r"events/[0-9]{8}-[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.json",
                path,
            )
            is None
            for path in paths
        ):
            raise IssueInboxError("inbox contains an unexpected path")
        events = tuple(
            _event_from_bytes(
                _git(
                    self._root,
                    "show",
                    f"{revision}:{path}",
                )
            )
            for path in sorted(paths)
        )
        for index, path in enumerate(sorted(paths), 1):
            if not path.startswith(f"events/{index:08d}-"):
                raise IssueInboxError("inbox event sequence is invalid")
        if len({event.event_id for event in events}) != len(events):
            raise IssueInboxError("inbox event IDs are not unique")
        _validate_transport_sequence(events)
        return revision, events


class GhStewardAssignments:
    def __init__(
        self,
        commands: CommandRunner,
        repository_root: Path,
        repository: str,
    ) -> None:
        if not _REPOSITORY.fullmatch(repository):
            raise ValueError("repository is invalid")
        self._commands = commands
        self._root = repository_root
        self._repository = repository

    def assign(
        self,
        issue_number: int,
        idempotency_key: str,
    ) -> bool:
        if type(issue_number) is not int or issue_number < 1:
            raise ValueError("issue number must be positive")
        _identifier(idempotency_key, "idempotency key")
        marker = (
            "<!-- foundry-opt:steward-trigger:"
            f"{idempotency_key} -->"
        )
        if self._has_marker(issue_number, marker):
            return False
        endpoint = (
            f"repos/{self._repository}/issues/"
            f"{issue_number}/assignees"
        )
        assignees = {"assignees": ["copilot-swe-agent[bot]"]}
        self._commands.run(
            (
                "gh",
                "api",
                "--method",
                "DELETE",
                endpoint,
                "--input",
                "-",
            ),
            cwd=self._root,
            input_text=json.dumps(
                assignees,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        body = {
            **assignees,
            "agent_assignment": {
                "target_repo": self._repository,
                "custom_agent": "foundry-optimization-steward",
                "custom_instructions": (
                    "Advance this campaign only from its trusted "
                    "Git-state inbox."
                ),
            },
        }
        self._commands.run(
            (
                "gh",
                "api",
                "--method",
                "POST",
                endpoint,
                "--input",
                "-",
            ),
            cwd=self._root,
            input_text=json.dumps(
                body,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        self._commands.run(
            (
                "gh",
                "api",
                "--method",
                "POST",
                (
                    f"repos/{self._repository}/issues/"
                    f"{issue_number}/comments"
                ),
                "--input",
                "-",
            ),
            cwd=self._root,
            input_text=json.dumps(
                {"body": marker},
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        return True

    def _has_marker(self, issue_number: int, marker: str) -> bool:
        result = self._commands.run(
            (
                "gh",
                "api",
                "--paginate",
                "--slurp",
                (
                    f"repos/{self._repository}/issues/"
                    f"{issue_number}/comments"
                ),
            ),
            cwd=self._root,
        )
        try:
            pages = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise TrustedIssueEventError(
                "issue comments response is invalid"
            ) from error
        if not isinstance(pages, list):
            raise TrustedIssueEventError(
                "issue comments response is invalid"
            )
        for page in pages:
            if not isinstance(page, list):
                raise TrustedIssueEventError(
                    "issue comments response is invalid"
                )
            for item in page:
                if not isinstance(item, Mapping):
                    raise TrustedIssueEventError(
                        "issue comments response is invalid"
                    )
                user = item.get("user")
                if (
                    isinstance(user, Mapping)
                    and user.get("login") == "github-actions[bot]"
                    and marker in str(item.get("body", ""))
                ):
                    return True
        return False


def _validate_payload(
    payload: Mapping[str, Any],
    context: TrustedEventContext,
) -> tuple[int, str, str, datetime]:
    if context.event_name != "issues":
        raise TrustedIssueEventError("event name must be issues")
    if not isinstance(payload, Mapping):
        raise TrustedIssueEventError("event payload must be an object")
    action = payload.get("action")
    if not isinstance(action, str) or action not in _ACTIONS:
        raise TrustedIssueEventError("issue action is not trusted")
    repository = payload.get("repository")
    if not isinstance(repository, Mapping):
        raise TrustedIssueEventError("repository identity is missing")
    if (
        repository.get("id") != context.repository_id
        or repository.get("full_name") != context.repository
    ):
        raise TrustedIssueEventError("repository identity does not match")
    issue = payload.get("issue")
    if not isinstance(issue, Mapping) or "pull_request" in issue:
        raise TrustedIssueEventError("issue identity is invalid")
    issue_number = issue.get("number")
    if type(issue_number) is not int or issue_number < 1:
        raise TrustedIssueEventError("issue number is invalid")
    title = issue.get("title")
    if not isinstance(title, str):
        raise TrustedIssueEventError("issue title is invalid")
    state = issue.get("state")
    expected_state = "closed" if action == "closed" else "open"
    if state != expected_state:
        raise TrustedIssueEventError("issue state does not match action")
    updated_at = issue.get("updated_at")
    if not isinstance(updated_at, str):
        raise TrustedIssueEventError("issue timestamp is invalid")
    try:
        occurred_at = datetime.fromisoformat(
            updated_at.replace("Z", "+00:00")
        )
    except ValueError as error:
        raise TrustedIssueEventError(
            "issue timestamp is invalid"
        ) from error
    if occurred_at.tzinfo is None:
        raise TrustedIssueEventError("issue timestamp is invalid")
    return issue_number, action, title, occurred_at


def _generation(
    kind: EventKind,
    existing: tuple[CampaignEvent, ...],
) -> int:
    if kind is EventKind.ISSUE_CREATED:
        if existing:
            raise TrustedIssueEventError(
                "opened is only valid as the first issue event"
            )
        return 1
    if not existing:
        raise TrustedIssueEventError(
            f"{kind.value} requires an existing issue event"
        )
    latest = existing[-1]
    if kind is EventKind.ISSUE_REOPENED:
        if latest.kind is not EventKind.ISSUE_CLOSED:
            raise TrustedIssueEventError(
                "reopened requires a closed issue"
            )
        return latest.generation + 1
    if kind in {
        EventKind.ISSUE_CLOSED,
        EventKind.ISSUE_DECLASSIFIED,
    }:
        if latest.kind is EventKind.ISSUE_CLOSED:
            raise TrustedIssueEventError("issue is already closed")
        return latest.generation
    if latest.kind is EventKind.ISSUE_CLOSED:
        raise TrustedIssueEventError("edited is invalid for a closed issue")
    return latest.generation + 1


def _event_kind(
    action: str,
    title: str,
    existing: tuple[CampaignEvent, ...],
) -> EventKind:
    if action == "opened":
        if not title.startswith("[Optimize] "):
            raise TrustedIssueEventError(
                "initial issue is missing the optimization title"
            )
        return EventKind.ISSUE_CREATED
    if not existing:
        raise TrustedIssueEventError(
            "issue has no trusted optimization inbox identity"
        )
    if action == "edited" and not title.startswith("[Optimize] "):
        return EventKind.ISSUE_DECLASSIFIED
    return _ACTIONS[action]


def _inbox_ref(issue_number: int) -> str:
    if type(issue_number) is not int or issue_number < 1:
        raise ValueError("issue number must be positive")
    return f"refs/heads/foundry-opt/inbox/issue-{issue_number}"


def _require_transport_event(event: CampaignEvent) -> None:
    if event.kind not in {
        EventKind.ISSUE_CREATED,
        EventKind.ISSUE_EDITED,
        EventKind.ISSUE_DECLASSIFIED,
        EventKind.ISSUE_REOPENED,
        EventKind.ISSUE_CLOSED,
    } or event.payload:
        raise IssueInboxError(
            "Git issue inbox accepts only transport issue events"
        )


def _validate_transport_sequence(
    events: tuple[CampaignEvent, ...],
) -> None:
    actions = {
        EventKind.ISSUE_CREATED: EventKind.ISSUE_CREATED,
        EventKind.ISSUE_EDITED: EventKind.ISSUE_EDITED,
        EventKind.ISSUE_DECLASSIFIED: EventKind.ISSUE_DECLASSIFIED,
        EventKind.ISSUE_REOPENED: EventKind.ISSUE_REOPENED,
        EventKind.ISSUE_CLOSED: EventKind.ISSUE_CLOSED,
    }
    prior: tuple[CampaignEvent, ...] = ()
    for event in events:
        try:
            expected = _generation(actions[event.kind], prior)
        except TrustedIssueEventError as error:
            raise IssueInboxError(
                "inbox event sequence is invalid"
            ) from error
        if event.generation != expected:
            raise IssueInboxError("inbox event generation is invalid")
        prior = (*prior, event)


def _event_bytes(event: CampaignEvent) -> bytes:
    _require_transport_event(event)
    document = {
        "event_id": event.event_id,
        "generation": event.generation,
        "kind": event.kind.value,
        "occurred_at": event.occurred_at.isoformat().replace(
            "+00:00",
            "Z",
        ),
        "payload": {},
        "schema_version": 1,
    }
    return (
        json.dumps(document, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _event_from_bytes(content: bytes) -> CampaignEvent:
    try:
        document = json.loads(content)
        if type(document) is not dict or set(document) != {
            "event_id",
            "generation",
            "kind",
            "occurred_at",
            "payload",
            "schema_version",
        }:
            raise ValueError
        if document["schema_version"] != 1 or document["payload"] != {}:
            raise ValueError
        event = CampaignEvent(
            event_id=document["event_id"],
            generation=document["generation"],
            kind=EventKind(document["kind"]),
            occurred_at=datetime.fromisoformat(
                document["occurred_at"].replace("Z", "+00:00")
            ),
        )
        _require_transport_event(event)
        return event
    except (KeyError, TypeError, ValueError) as error:
        raise IssueInboxError("inbox event is invalid") from error


def _git_text(
    root: Path,
    *arguments: str,
    environment: Mapping[str, str] | None = None,
) -> str:
    return _git(
        root,
        *arguments,
        environment=environment,
    ).decode("utf-8").strip()


def _git(
    root: Path,
    *arguments: str,
    environment: Mapping[str, str] | None = None,
    input_bytes: bytes | None = None,
) -> bytes:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=root,
        env=(
            None
            if environment is None
            else {**os.environ, **environment}
        ),
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise IssueInboxError(
            f"Git issue inbox command failed: {arguments[0]}"
        )
    return completed.stdout


def main() -> None:
    event_name = _required_environment("TRUSTED_EVENT_NAME")
    repository = _required_environment("TRUSTED_REPOSITORY")
    repository_id_text = _required_environment(
        "TRUSTED_REPOSITORY_ID"
    )
    if not repository_id_text.isdecimal():
        raise TrustedIssueEventError("repository ID is invalid")
    repository_id = int(repository_id_text)
    root = Path.cwd()
    commands = SubprocessCommandRunner()
    inbox = GitIssueEventInbox(root)
    assignments = GhStewardAssignments(
        commands,
        root,
        repository,
    )
    from foundry_opt.orchestration.projection import (
        DashboardProjection,
        GhDashboardGateway,
        GitStateProjectionOutbox,
    )

    projection = DashboardProjection(
        GitStateProjectionOutbox(root),
        GhDashboardGateway(commands, root, repository),
    )
    intake = IssueEventIntake(inbox, assignments, projection)
    run_id = _required_environment("TRUSTED_RUN_ID")
    _identifier(run_id, "trusted run ID")
    if event_name == "schedule":
        intake.recover(f"schedule-{run_id}")
        return
    if event_name != "issues":
        raise TrustedIssueEventError("event name is not trusted")
    event_path = Path(_required_environment("TRUSTED_EVENT_PATH"))
    try:
        if event_path.stat().st_size > 2_000_000:
            raise TrustedIssueEventError("event payload is too large")
        payload = json.loads(event_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TrustedIssueEventError(
            "event payload cannot be read"
        ) from error
    if not isinstance(payload, dict):
        raise TrustedIssueEventError("event payload must be an object")
    context = TrustedEventContext(
        event_name=event_name,
        delivery_id=run_id,
        repository=repository,
        repository_id=repository_id,
    )
    issue_number, action, _, _ = _validate_payload(payload, context)
    if action != "opened" and not inbox.events(issue_number):
        return
    intake.ingest(payload, context)


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise TrustedIssueEventError(
            f"required trusted environment is missing: {name}"
        )
    return value


def _identifier(value: str, description: str) -> None:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is None:
        raise TrustedIssueEventError(f"{description} is invalid")


if __name__ == "__main__":
    main()
