from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any

from foundry_opt.orchestration.workspace import (
    WorkspaceOperation,
    WorkspaceTrigger,
)
from foundry_opt.security import reject_secret_content


_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
)
_DELIVERY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


@dataclass(frozen=True)
class TrustedWorkspaceOperationContext:
    delivery_id: str
    repository: str
    repository_id: int

    def __post_init__(self) -> None:
        if _DELIVERY.fullmatch(self.delivery_id) is None:
            raise ValueError(
                "trusted workspace operation delivery is invalid"
            )
        if _REPOSITORY.fullmatch(self.repository) is None:
            raise ValueError(
                "trusted workspace operation repository is invalid"
            )
        if type(self.repository_id) is not int or self.repository_id < 1:
            raise ValueError(
                "trusted workspace operation repository ID is invalid"
            )


@dataclass(frozen=True)
class NormalizedWorkspaceOperation:
    delivery_id: str
    repository: str
    repository_id: int
    issue_number: int
    operation: WorkspaceOperation


def normalize_workspace_operation(
    payload: Mapping[str, Any],
    context: TrustedWorkspaceOperationContext,
) -> NormalizedWorkspaceOperation:
    reject_secret_content(payload)
    if set(payload) != {
        "bundle_sha256",
        "candidate_id",
        "evidence_sha256",
        "issue_number",
        "kind",
        "operation_id",
        "patch_sha256",
        "predecessor_operation_id",
        "repository",
        "schema_version",
        "status",
        "workspace_pull_request_number",
    }:
        raise ValueError("workspace operation fields are invalid")
    repository = payload["repository"]
    if (
        not isinstance(repository, Mapping)
        or set(repository) != {"full_name", "id"}
        or repository["full_name"] != context.repository
        or repository["id"] != context.repository_id
    ):
        raise ValueError("workspace operation repository changed")
    if payload["schema_version"] != 1 or payload["status"] != "completed":
        raise ValueError("workspace operation is not completed")
    kind = payload["kind"]
    trigger = {
        "deployment_result": WorkspaceTrigger.DEPLOYMENT_COMPLETED,
        "retention_result": WorkspaceTrigger.RETENTION_COMPLETED,
    }.get(kind)
    if trigger is None:
        raise ValueError("workspace operation kind is invalid")
    issue_number = payload["issue_number"]
    if type(issue_number) is not int or issue_number < 1:
        raise ValueError("workspace operation issue is invalid")
    operation = WorkspaceOperation(
        trigger=trigger,
        operation_id=payload["operation_id"],
        workspace_pull_request_number=(
            payload["workspace_pull_request_number"]
        ),
        candidate_id=payload["candidate_id"],
        patch_sha256=payload["patch_sha256"],
        bundle_sha256=payload["bundle_sha256"],
        evidence_sha256=payload["evidence_sha256"],
        predecessor_operation_id=payload["predecessor_operation_id"],
    )
    if (
        trigger is WorkspaceTrigger.DEPLOYMENT_COMPLETED
        and operation.predecessor_operation_id is not None
    ):
        raise ValueError(
            "workspace deployment cannot have a predecessor"
        )
    return NormalizedWorkspaceOperation(
        delivery_id=context.delivery_id,
        repository=context.repository,
        repository_id=context.repository_id,
        issue_number=issue_number,
        operation=operation,
    )


__all__ = [
    "NormalizedWorkspaceOperation",
    "TrustedWorkspaceOperationContext",
    "normalize_workspace_operation",
]
