from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Any, Mapping

from foundry_opt.orchestration.workspace import (
    WorkspacePullRequest,
    WorkspaceTrigger,
)
from foundry_opt.orchestration.workspace_github import (
    workspace_pull_request_base_commit,
    workspace_pull_request_marker,
)
from foundry_opt.security import reject_secret_content


_COMMIT = re.compile(r"^[0-9a-fA-F]{40}$")
_DELIVERY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
)
_ISSUE_ACTIONS = {
    "opened": WorkspaceTrigger.ISSUE_CREATED,
    "edited": WorkspaceTrigger.CONTINUE,
    "reopened": WorkspaceTrigger.CONTINUE,
}
_PULL_REQUEST_ACTIONS = frozenset(
    {"opened", "edited", "reopened", "synchronize"}
)


class WorkspaceEventKind(StrEnum):
    ISSUE = "issue"
    PULL_REQUEST = "pull_request"


@dataclass(frozen=True)
class TrustedWorkspaceEventContext:
    event_name: str
    delivery_id: str
    repository: str
    repository_id: int

    def __post_init__(self) -> None:
        if self.event_name not in {
            "issues",
            "pull_request",
            "pull_request_target",
        }:
            raise ValueError("workspace event name is not trusted")
        if _DELIVERY_ID.fullmatch(self.delivery_id) is None:
            raise ValueError("workspace delivery ID is invalid")
        if _REPOSITORY.fullmatch(self.repository) is None:
            raise ValueError("workspace repository is invalid")
        if type(self.repository_id) is not int or self.repository_id < 1:
            raise ValueError("workspace repository ID is invalid")


@dataclass(frozen=True)
class NormalizedWorkspaceEvent:
    kind: WorkspaceEventKind
    delivery_id: str
    repository: str
    repository_id: int
    issue_number: int
    trigger: WorkspaceTrigger
    base_commit: str
    workspace_pull_request: WorkspacePullRequest | None = None


def normalize_workspace_event(
    payload: Mapping[str, Any],
    context: TrustedWorkspaceEventContext,
    *,
    base_commit: str | None = None,
) -> NormalizedWorkspaceEvent:
    if not isinstance(payload, Mapping):
        raise ValueError("workspace event payload must be an object")
    _validate_repository(payload, context)
    if context.event_name == "issues":
        return _normalize_issue_event(
            payload,
            context,
            base_commit=base_commit,
        )
    return _normalize_pull_request_event(payload, context)


def _normalize_issue_event(
    payload: Mapping[str, Any],
    context: TrustedWorkspaceEventContext,
    *,
    base_commit: str | None,
) -> NormalizedWorkspaceEvent:
    action = payload.get("action")
    trigger = _ISSUE_ACTIONS.get(action)
    if trigger is None:
        raise ValueError("workspace issue action is not supported")
    issue = payload.get("issue")
    if not isinstance(issue, Mapping):
        raise ValueError("workspace issue payload is invalid")
    issue_number = _positive_integer(issue.get("number"), "issue number")
    title = issue.get("title")
    body = issue.get("body")
    if (
        not isinstance(title, str)
        or not title.startswith("[Optimize] ")
        or len(title) > 256
        or not isinstance(body, str)
        or len(body) > 262_144
    ):
        raise ValueError("workspace optimization issue is invalid")
    reject_secret_content(title)
    reject_secret_content(body)
    commit = _base_commit(base_commit)
    return NormalizedWorkspaceEvent(
        kind=WorkspaceEventKind.ISSUE,
        delivery_id=context.delivery_id,
        repository=context.repository,
        repository_id=context.repository_id,
        issue_number=issue_number,
        trigger=trigger,
        base_commit=commit,
    )


def _normalize_pull_request_event(
    payload: Mapping[str, Any],
    context: TrustedWorkspaceEventContext,
) -> NormalizedWorkspaceEvent:
    action = payload.get("action")
    raw_pull_request = payload.get("pull_request")
    merged = (
        action == "closed"
        and isinstance(raw_pull_request, Mapping)
        and raw_pull_request.get("merged") is True
    )
    if action not in _PULL_REQUEST_ACTIONS and not merged:
        raise ValueError("workspace pull request action is not supported")
    pull_request = raw_pull_request
    if not isinstance(pull_request, Mapping):
        raise ValueError("workspace pull request payload is invalid")
    number = _positive_integer(
        pull_request.get("number"),
        "pull request number",
    )
    body = pull_request.get("body")
    title = pull_request.get("title")
    head = pull_request.get("head")
    head_repository = (
        head.get("repo") if isinstance(head, Mapping) else None
    )
    expected_draft = not merged
    expected_state = "closed" if merged else "open"
    if (
        not isinstance(body, str)
        or not isinstance(title, str)
        or pull_request.get("draft") is not expected_draft
        or pull_request.get("state") != expected_state
        or (merged and pull_request.get("merged") is not True)
        or not isinstance(head, Mapping)
        or not isinstance(head_repository, Mapping)
        or head_repository.get("full_name") != context.repository
    ):
        raise ValueError("workspace pull request is invalid")
    marker = re.findall(
        r"<!-- foundry-opt:workspace-pr:issue-([1-9][0-9]*):v1 -->",
        body,
    )
    if len(marker) != 1:
        raise ValueError("workspace pull request marker is invalid")
    issue_number = int(marker[0])
    branch = f"foundry-opt/workspace/issue-{issue_number}"
    expected_title = (
        f"[Optimize] #{issue_number} selected candidate"
        if merged
        else (
            f"[Optimize] #{issue_number} workspace - "
            "draft, not yet selectable"
        )
    )
    if (
        workspace_pull_request_marker(issue_number) not in body
        or head.get("ref") != branch
        or title != expected_title
    ):
        raise ValueError("workspace pull request does not match issue")
    commit = workspace_pull_request_base_commit(body)
    return NormalizedWorkspaceEvent(
        kind=WorkspaceEventKind.PULL_REQUEST,
        delivery_id=context.delivery_id,
        repository=context.repository,
        repository_id=context.repository_id,
        issue_number=issue_number,
        trigger=(
            WorkspaceTrigger.PULL_REQUEST_MERGED
            if merged
            else WorkspaceTrigger.CONTINUE
        ),
        base_commit=commit,
        workspace_pull_request=WorkspacePullRequest(
            number=number,
            issue_number=issue_number,
            branch=branch,
            title=expected_title,
            draft=not merged,
            reuse_existing=True,
            base_commit=commit,
        ),
    )


def _validate_repository(
    payload: Mapping[str, Any],
    context: TrustedWorkspaceEventContext,
) -> None:
    repository = payload.get("repository")
    if (
        not isinstance(repository, Mapping)
        or repository.get("full_name") != context.repository
        or repository.get("id") != context.repository_id
    ):
        raise ValueError("workspace event repository does not match")


def _base_commit(value: str | None) -> str:
    if not isinstance(value, str) or _COMMIT.fullmatch(value) is None:
        raise ValueError("workspace base commit is invalid")
    return value.lower()


def _positive_integer(value: Any, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"workspace {name} is invalid")
    return value
