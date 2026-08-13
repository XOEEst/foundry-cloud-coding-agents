from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any

from foundry_opt.orchestration.workspace import (
    WorkspaceCandidateProposal,
)
from foundry_opt.security import reject_secret_content


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_MAX_PATCH_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class WorkspaceExperimentManifest:
    issue_number: int
    target: str
    base_commit: str
    candidates: tuple[WorkspaceCandidateProposal, ...]


@dataclass(frozen=True)
class WorkspaceCandidateManifest:
    issue_number: int
    target: str
    base_commit: str
    candidate: WorkspaceCandidateProposal


def parse_workspace_experiment_manifest(
    payload: Mapping[str, Any],
    *,
    policy: object | None = None,
) -> WorkspaceExperimentManifest:
    reject_secret_content(payload)
    _exact_keys(
        payload,
        {
            "base_commit",
            "candidates",
            "issue_number",
            "schema_version",
            "target",
        },
        "workspace manifest",
    )
    issue_number, target, base_commit = _header(
        payload, schema_version=3
    )
    raw_candidates = payload["candidates"]
    if (
        not isinstance(raw_candidates, list)
        or not raw_candidates
        or len(raw_candidates) > 32
    ):
        raise ValueError("workspace manifest candidates are invalid")
    candidates = tuple(_candidate(item) for item in raw_candidates)
    ids = tuple(item.candidate_id for item in candidates)
    keys = tuple(item.idempotency_key for item in candidates)
    if (
        len(ids) != len(set(ids))
        or len(keys) != len(set(keys))
    ):
        raise ValueError("workspace proposal identities must be unique")
    return WorkspaceExperimentManifest(
        issue_number=issue_number,
        target=target,
        base_commit=base_commit,
        candidates=candidates,
    )


def parse_workspace_candidate_manifest(
    payload: Mapping[str, Any],
) -> WorkspaceCandidateManifest:
    reject_secret_content(payload)
    _exact_keys(
        payload,
        {
            "base_commit",
            "candidate",
            "issue_number",
            "schema_version",
            "target",
        },
        "workspace candidate manifest",
    )
    issue_number, target, base_commit = _header(
        payload, schema_version=2
    )
    return WorkspaceCandidateManifest(
        issue_number=issue_number,
        target=target,
        base_commit=base_commit,
        candidate=_candidate(payload["candidate"]),
    )


def _header(
    payload: Mapping[str, Any],
    *,
    schema_version: int,
) -> tuple[int, str, str]:
    if payload["schema_version"] != schema_version:
        raise ValueError("workspace manifest schema version is invalid")
    issue_number = payload["issue_number"]
    target = payload["target"]
    base_commit = payload["base_commit"]
    if type(issue_number) is not int or issue_number < 1:
        raise ValueError("workspace manifest issue number is invalid")
    if not isinstance(target, str) or _IDENTIFIER.fullmatch(target) is None:
        raise ValueError("workspace manifest target is invalid")
    if (
        not isinstance(base_commit, str)
        or _COMMIT.fullmatch(base_commit) is None
    ):
        raise ValueError("workspace manifest base commit is invalid")
    return issue_number, target, base_commit


def _candidate(value: Any) -> WorkspaceCandidateProposal:
    if not isinstance(value, Mapping):
        raise ValueError("workspace manifest candidate is invalid")
    _exact_keys(
        value,
        {
            "candidate_id",
            "changed_paths",
            "expected_tree",
            "experiment_reference",
            "idempotency_key",
            "patch_base64",
            "summary",
            "validation",
        },
        "workspace manifest candidate",
    )
    patch_value = value["patch_base64"]
    if not isinstance(patch_value, str):
        raise ValueError("workspace manifest patch is invalid")
    try:
        patch = base64.b64decode(patch_value, validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError("workspace manifest patch is invalid") from error
    if not patch or len(patch) > _MAX_PATCH_BYTES:
        raise ValueError("workspace manifest patch is invalid")
    try:
        reject_secret_content(patch.decode("utf-8"))
    except UnicodeDecodeError:
        pass
    return WorkspaceCandidateProposal(
        candidate_id=value["candidate_id"],
        exact_patch=patch,
        idempotency_key=value["idempotency_key"],
        experiment_reference=value["experiment_reference"],
        summary=value["summary"],
        changed_paths=_strings(value["changed_paths"], "changed paths"),
        validation=_strings(value["validation"], "validation"),
        expected_tree=value["expected_tree"],
    )


def _exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    name: str,
) -> None:
    if set(value) != expected:
        raise ValueError(f"{name} fields are invalid")


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) for item in value)
    ):
        raise ValueError(f"workspace {name} are invalid")
    return tuple(value)


__all__ = [
    "WorkspaceCandidateManifest",
    "WorkspaceExperimentManifest",
    "parse_workspace_candidate_manifest",
    "parse_workspace_experiment_manifest",
]
