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
_ISSUE_EVENT_KINDS = frozenset(
    {*_ACTIONS.values(), EventKind.ISSUE_DECLASSIFIED}
)
_PR_EVENT_KINDS = frozenset(
    {
        EventKind.CANDIDATE_PR_OPENED,
        EventKind.CANDIDATE_PR_SYNCHRONIZED,
        EventKind.CANDIDATE_PR_EDITED,
        EventKind.CANDIDATE_PR_CLOSED,
        EventKind.CANDIDATE_PR_MERGED,
    }
)
_WORKFLOW_EVENT_KINDS = frozenset(
    {EventKind.DEPLOYMENT_WORKFLOW_OBSERVED}
)


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


class DeploymentWorkflowEventRouter:
    """Route one trusted workflow_run to its exact persisted deployment."""

    def __init__(
        self,
        repository_root: Path,
        inbox: IssueEventInbox,
        assignments: StewardAssignments,
        projection: IssueProjection,
    ) -> None:
        self._root = repository_root
        self._inbox = inbox
        self._assignments = assignments
        self._projection = projection

    def ingest(
        self,
        payload: Mapping[str, Any],
        context: TrustedEventContext,
    ) -> IntakeResult | None:
        if context.event_name != "workflow_run":
            raise TrustedIssueEventError(
                "workflow router requires workflow_run"
            )
        repository = payload.get("repository")
        run = payload.get("workflow_run")
        action = payload.get("action")
        if (
            not isinstance(repository, Mapping)
            or not isinstance(run, Mapping)
            or action not in {"requested", "in_progress", "completed"}
            or repository.get("full_name") != context.repository
            or repository.get("id") != context.repository_id
        ):
            raise TrustedIssueEventError(
                "workflow payload identity is invalid"
            )
        from foundry_opt.orchestration.deployment import (
            DeploymentWorkflowEventIntake,
            DeploymentWorkflowResultRecorder,
            TrustedDeploymentWorkflowContext,
            deployment_workflow_event_from_payload,
            deployment_workflow_intent,
        )
        from foundry_opt.orchestration.git_state import GitStateRef

        matches: list[tuple[int, Any]] = []
        for issue_number in self._inbox.issue_numbers():
            snapshot = GitStateRef().load(self._root, issue_number)
            if snapshot is None:
                continue
            intents = []
            for record in snapshot.outbox:
                if record.kind != "deployment_workflow_planned" or (
                    record.generation != snapshot.state.generation
                ):
                    continue
                try:
                    intents.append(deployment_workflow_intent(record))
                except (KeyError, TypeError, ValueError):
                    continue
            if not intents:
                continue
            run_id = run.get("id")
            bound_effects = {
                str(record.payload["effect_id"])
                for record in snapshot.outbox
                if (
                    record.kind == "deployment_workflow_run_bound"
                    and record.generation == snapshot.state.generation
                    and record.payload.get("run_id") == run_id
                    and isinstance(record.payload.get("effect_id"), str)
                )
            }
            bound = tuple(
                intent
                for intent in intents
                if intent.effect_id in bound_effects
            )
            if len(bound) == 1:
                intent = bound[0]
            elif len(bound) > 1:
                raise TrustedIssueEventError(
                    "workflow run is not bound to one deployment attempt"
                )
            elif any(
                intent.workflow.trigger.value == "manual"
                for intent in intents
            ):
                intent = max(intents, key=lambda item: item.attempt)
            elif len(intents) > 1:
                intent = max(intents, key=lambda item: item.attempt)
            else:
                intent = intents[0]
            path = run.get("path")
            if (
                intent.workflow.repository == context.repository
                and intent.workflow.repository_id == context.repository_id
                and run.get("workflow_id") == intent.workflow.workflow_id
                and path
                == (
                    f"{intent.workflow.path.as_posix()}@"
                    f"{intent.workflow.ref}"
                )
                and (
                    (
                        intent.workflow.trigger.value == "manual"
                        and run.get("display_title") == intent.effect_id
                    )
                    or (
                        intent.workflow.trigger.value != "manual"
                        and run.get("head_sha")
                        == intent.binding.merge_commit
                    )
                )
            ):
                matches.append((issue_number, intent))
        if not matches:
            return None
        if len(matches) != 1:
            raise TrustedIssueEventError(
                "workflow payload matches multiple deployment intents"
            )
        issue_number, intent = matches[0]
        occurred_at = _workflow_time(run)
        actor = run.get("actor")
        actor_login = (
            actor.get("login") if isinstance(actor, Mapping) else None
        )
        if not isinstance(actor_login, str):
            raise TrustedIssueEventError("workflow actor is invalid")
        event = deployment_workflow_event_from_payload(
            TrustedDeploymentWorkflowContext(
                event_name="workflow_run",
                action=str(action),
                delivery_id=context.delivery_id,
                repository=context.repository,
                repository_id=context.repository_id,
                workflow_id=int(intent.workflow.workflow_id),
                workflow_path=intent.workflow.path,
                actor=(
                    intent.workflow.actor
                    if intent.workflow.trigger.value == "manual"
                    and intent.workflow.actor == "workflow-dispatch"
                    else actor_login
                ),
                deployment_client_id=(
                    intent.workflow.deployment_client_id
                ),
            ),
            payload,
            intent,
            occurred_at,
        )
        DeploymentWorkflowResultRecorder(GitStateRef()).record(
            self._root,
            issue_number,
            event.result,
        )
        result = DeploymentWorkflowEventIntake(self._inbox).ingest(event)
        self._assignments.assign(
            issue_number,
            result.event.event_id,
        )
        self._projection.project(issue_number)
        return IntakeResult(
            result.event,
            result.status.value == "recorded",
        )


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
    existing = tuple(
        event for event in existing if event.kind in _ISSUE_EVENT_KINDS
    )
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


def _workflow_time(run: Mapping[str, Any]) -> datetime:
    value = run.get("updated_at") or run.get("run_started_at")
    if not isinstance(value, str):
        raise TrustedIssueEventError("workflow timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise TrustedIssueEventError(
            "workflow timestamp is invalid"
        ) from error
    if parsed.tzinfo is None:
        raise TrustedIssueEventError("workflow timestamp is invalid")
    return parsed


def _inbox_ref(issue_number: int) -> str:
    if type(issue_number) is not int or issue_number < 1:
        raise ValueError("issue number must be positive")
    return f"refs/heads/foundry-opt/inbox/issue-{issue_number}"


def _require_transport_event(event: CampaignEvent) -> None:
    if event.kind in _ISSUE_EVENT_KINDS:
        if event.payload:
            raise IssueInboxError(
                "Git issue transport event payload must be empty"
            )
        return
    if event.kind in _WORKFLOW_EVENT_KINDS:
        try:
            from foundry_opt.orchestration.deployment import (
                deployment_workflow_result_from_event,
            )

            deployment_workflow_result_from_event(event)
        except ValueError as error:
            raise IssueInboxError(
                "deployment workflow transport payload is invalid"
            ) from error
        return
    if event.kind not in _PR_EVENT_KINDS:
        raise IssueInboxError(
            "Git issue inbox accepts only trusted transport events"
        )
    expected = {
        "binding_sha256",
        "candidate_id",
        "head_commit",
        "pull_request_number",
    }
    if event.kind is EventKind.CANDIDATE_PR_MERGED:
        expected.add("merge_commit")
    payload = event.payload
    if set(payload) != expected:
        raise IssueInboxError(
            "candidate PR transport payload is invalid"
        )
    if (
        not isinstance(payload["binding_sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", payload["binding_sha256"]) is None
        or not isinstance(payload["candidate_id"], str)
        or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
            payload["candidate_id"],
        )
        is None
        or not isinstance(payload["head_commit"], str)
        or re.fullmatch(r"[0-9a-f]{40}", payload["head_commit"]) is None
        or type(payload["pull_request_number"]) is not int
        or payload["pull_request_number"] < 1
        or (
            "merge_commit" in payload
            and (
                not isinstance(payload["merge_commit"], str)
                or re.fullmatch(
                    r"[0-9a-f]{40}",
                    payload["merge_commit"],
                )
                is None
            )
        )
    ):
        raise IssueInboxError(
            "candidate PR transport binding is invalid"
        )


def _validate_transport_sequence(
    events: tuple[CampaignEvent, ...],
) -> None:
    prior: tuple[CampaignEvent, ...] = ()
    for event in events:
        _require_transport_event(event)
        if event.kind in {*_PR_EVENT_KINDS, *_WORKFLOW_EVENT_KINDS}:
            issue_events = tuple(
                item
                for item in prior
                if item.kind in _ISSUE_EVENT_KINDS
            )
            if (
                not issue_events
                or event.generation < 1
                or event.generation > issue_events[-1].generation
            ):
                raise IssueInboxError(
                    "candidate PR event generation is invalid"
                )
            prior = (*prior, event)
            continue
        try:
            expected = _generation(event.kind, prior)
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
        "payload": dict(event.payload),
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
        if (
            document["schema_version"] != 1
            or type(document["payload"]) is not dict
        ):
            raise ValueError
        event = CampaignEvent(
            event_id=document["event_id"],
            generation=document["generation"],
            kind=EventKind(document["kind"]),
            occurred_at=datetime.fromisoformat(
                document["occurred_at"].replace("Z", "+00:00")
            ),
            payload=document["payload"],
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
    if event_name in {"schedule", "workflow_dispatch"}:
        from foundry_opt.orchestration.deployment_bridge import (
            reconcile_deployment_workflow_effects,
        )

        for issue_number in inbox.issue_numbers():
            reconcile_deployment_workflow_effects(
                root,
                issue_number,
                commands,
            )
        intake.recover(f"schedule-{run_id}")
        return
    if event_name not in {"issues", "pull_request", "workflow_run"}:
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
    if event_name == "workflow_run":
        DeploymentWorkflowEventRouter(
            root,
            inbox,
            assignments,
            projection,
        ).ingest(payload, context)
        return
    if event_name == "pull_request":
        pull_request = payload.get("pull_request")
        body = (
            pull_request.get("body")
            if isinstance(pull_request, dict)
            else None
        )
        changes = payload.get("changes")
        changed_body = (
            changes.get("body")
            if isinstance(changes, dict)
            else None
        )
        previous_body = (
            changed_body.get("from")
            if isinstance(changed_body, dict)
            else None
        )
        marker = None
        for candidate_body in (body, previous_body):
            if not isinstance(candidate_body, str):
                continue
            marker = re.search(
                r"<!-- foundry-opt:candidate-pr:"
                r"issue-([1-9][0-9]*):g[1-9][0-9]*:"
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}:"
                r"[0-9a-f]{20} -->",
                candidate_body,
            )
            if marker is not None:
                break
        if marker is None:
            return
        issue_number = int(marker.group(1))
        from foundry_opt.orchestration.candidate_slate import (
            CandidatePullRequestEventIntake,
            TrustedCandidatePullRequestContext,
            candidate_pull_request_event_from_payload,
            candidate_pr_marker,
            candidate_worker_bindings,
        )
        from foundry_opt.orchestration.git_state import GitStateRef

        snapshot = GitStateRef().load(root, issue_number)
        if snapshot is None:
            return
        bindings = candidate_worker_bindings(snapshot)
        if not any(
            candidate_pr_marker(binding) == marker.group(0)
            for binding in bindings
        ):
            return
        event = candidate_pull_request_event_from_payload(
            payload,
            TrustedCandidatePullRequestContext(
                event_name=event_name,
                delivery_id=run_id,
                repository=repository,
                repository_id=repository_id,
            ),
            bindings,
        )
        recorded = CandidatePullRequestEventIntake(inbox).ingest(event)
        assignments.assign(issue_number, recorded.event.event_id)
        projection.project(issue_number)
        return
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
