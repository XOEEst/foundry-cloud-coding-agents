from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any

from foundry_opt.orchestration.workspace import (
    WorkspaceCandidateProposal,
)
from foundry_opt.orchestration.workspace_attribution import (
    WorkspaceCandidateProvenance,
    parse_workspace_candidate_provenance,
    workspace_candidate_provenance_document,
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
    provenance: WorkspaceCandidateProvenance | None = None


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
        payload, schema_version=4
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
    if len(ids) != len(set(ids)):
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
    schema_version = payload.get("schema_version")
    if schema_version == 3:
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
        provenance = None
    elif schema_version == 4:
        _exact_keys(
            payload,
            {
                "base_commit",
                "candidate",
                "issue_number",
                "provenance",
                "schema_version",
                "target",
            },
            "workspace candidate manifest",
        )
        provenance = parse_workspace_candidate_provenance(
            payload["provenance"]
        )
    else:
        raise ValueError("workspace manifest schema version is invalid")
    issue_number, target, base_commit = _header(
        payload, schema_version=schema_version
    )
    return WorkspaceCandidateManifest(
        issue_number=issue_number,
        target=target,
        base_commit=base_commit,
        candidate=_candidate(payload["candidate"]),
        provenance=provenance,
    )


def workspace_candidate_manifest_document(
    manifest: WorkspaceCandidateManifest,
    provenance: WorkspaceCandidateProvenance,
) -> dict[str, Any]:
    if type(manifest) is not WorkspaceCandidateManifest:
        raise ValueError("workspace candidate manifest is invalid")
    if (
        manifest.provenance is not None
        and manifest.provenance != provenance
    ):
        raise ValueError("workspace candidate provenance changed")
    return {
        "base_commit": manifest.base_commit,
        "candidate": {
            "candidate_id": manifest.candidate.candidate_id,
            "mutation_class": manifest.candidate.mutation_class,
            "patch_base64": base64.b64encode(
                manifest.candidate.exact_patch
            ).decode("ascii"),
            "summary": manifest.candidate.summary,
        },
        "issue_number": manifest.issue_number,
        "provenance": workspace_candidate_provenance_document(provenance),
        "schema_version": 4,
        "target": manifest.target,
    }


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
            "mutation_class",
            "patch_base64",
            "summary",
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
        summary=value["summary"],
        mutation_class=value["mutation_class"],
    )


def _exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    name: str,
) -> None:
    if set(value) != expected:
        raise ValueError(f"{name} fields are invalid")


__all__ = [
    "WorkspaceCandidateManifest",
    "WorkspaceExperimentManifest",
    "parse_workspace_candidate_manifest",
    "parse_workspace_experiment_manifest",
    "workspace_candidate_manifest_document",
]
