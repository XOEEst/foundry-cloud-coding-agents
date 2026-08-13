from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from math import isfinite
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping

from foundry_opt.orchestration.campaign import OptimizationCampaign
from foundry_opt.orchestration.git_state import GitStateRef, StateRefSnapshot
from foundry_opt.orchestration.git_transport import (
    fetch_revision,
    GitTransportError,
    remote_revision,
    resolve_safe_fetch_remote,
)
from foundry_opt.orchestration.models import (
    AdvanceRequest,
    CampaignPhase,
    CampaignState,
)
from foundry_opt.orchestration.workspace import WorkspacePhase
from foundry_opt.orchestration.workspace_store import (
    CandidateSummary,
    WorkspaceLineage,
    WorkspaceUpdate,
)
from foundry_opt.security import reject_secret_content


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_CANDIDATE_OBJECT = re.compile(
    r"^objects/candidates/g([1-9][0-9]*)-"
    r"([A-Za-z0-9][A-Za-z0-9._-]{0,127})\.json$"
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_EXTERNAL_ID = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,255}$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXTERNAL_ID_FIELDS = (
    "draft_id",
    "evaluation_id",
    "result_id",
    "run_id",
    "workflow_id",
)


@dataclass(frozen=True)
class WorkspaceStateMigrationPlan:
    issue_number: int
    source_ref: str
    source_revision: str
    source_schema_version: int
    target_schema_version: int
    legacy_paths: tuple[str, ...]
    read_only: bool


class WorkspaceStateConversionError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkspaceStateConversionPayload:
    issue_number: int
    source_ref: str
    source_revision: str
    source_schema_version: int
    target_schema_version: int
    transitions: tuple[WorkspaceUpdate, ...]
    canonical_bytes: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()


def workspace_state_v3_migration_plan(
    *,
    issue_number: int,
    source_revision: str,
    source_paths: tuple[str, ...],
) -> WorkspaceStateMigrationPlan:
    _issue_number(issue_number)
    if _COMMIT.fullmatch(source_revision) is None:
        raise ValueError("source_revision must be a commit SHA")
    paths = tuple(source_paths)
    if (
        any(type(path) is not str for path in paths)
        or len(paths) != len(set(paths))
        or "snapshot.json" not in paths
        or "journal.jsonl" not in paths
        or any(
            path not in {"snapshot.json", "journal.jsonl"}
            and not path.startswith(("inbox/", "outbox/", "objects/"))
            for path in paths
        )
    ):
        raise ValueError("legacy v3 paths are invalid")
    return WorkspaceStateMigrationPlan(
        issue_number=issue_number,
        source_ref=_state_ref(issue_number),
        source_revision=source_revision,
        source_schema_version=3,
        target_schema_version=4,
        legacy_paths=paths,
        read_only=True,
    )


def convert_workspace_state_v3(
    repository_root: Path,
    issue_number: int,
    *,
    remote: str = "origin",
) -> WorkspaceStateConversionPayload:
    plan = detect_workspace_state_v3(
        repository_root,
        issue_number,
        remote=remote,
    )
    if plan is None:
        raise WorkspaceStateConversionError(
            "source ref is not a validated workspace state v3 ledger"
        )
    try:
        loaded = GitStateRef(remote=remote).load(
            repository_root,
            issue_number,
        )
    except Exception as error:
        raise WorkspaceStateConversionError(
            "workspace state v3 validation failed"
        ) from error
    if loaded is None or loaded.revision != plan.source_revision:
        raise WorkspaceStateConversionError(
            "workspace state v3 changed during conversion"
        )
    metadata = _candidate_metadata(loaded, issue_number)
    transitions = _conversion_transitions(loaded, metadata)
    document = _conversion_document(
        issue_number=issue_number,
        source_ref=plan.source_ref,
        source_revision=plan.source_revision,
        transitions=transitions,
    )
    payload = WorkspaceStateConversionPayload(
        issue_number=issue_number,
        source_ref=plan.source_ref,
        source_revision=plan.source_revision,
        source_schema_version=3,
        target_schema_version=4,
        transitions=transitions,
        canonical_bytes=_canonical_json(document),
    )
    validate_workspace_state_conversion_payload(payload)
    return payload


def validate_workspace_state_conversion_payload(
    payload: WorkspaceStateConversionPayload,
) -> None:
    if type(payload) is not WorkspaceStateConversionPayload:
        raise WorkspaceStateConversionError(
            "workspace conversion payload type is invalid"
        )
    _issue_number(payload.issue_number)
    if (
        payload.source_ref != _state_ref(payload.issue_number)
        or _COMMIT.fullmatch(payload.source_revision) is None
        or payload.source_schema_version != 3
        or payload.target_schema_version != 4
        or not payload.transitions
        or any(
            type(item) is not WorkspaceUpdate
            or item.issue_number != payload.issue_number
            for item in payload.transitions
        )
    ):
        raise WorkspaceStateConversionError(
            "workspace conversion payload lineage is invalid"
        )
    expected = _canonical_json(
        _conversion_document(
            issue_number=payload.issue_number,
            source_ref=payload.source_ref,
            source_revision=payload.source_revision,
            transitions=payload.transitions,
        )
    )
    if payload.canonical_bytes != expected:
        raise WorkspaceStateConversionError(
            "workspace conversion payload is not canonical"
        )


def detect_workspace_state_v3(
    repository_root: Path,
    issue_number: int,
    *,
    remote: str = "origin",
) -> WorkspaceStateMigrationPlan | None:
    root = _repository_root(repository_root)
    _issue_number(issue_number)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", remote) is None:
        raise ValueError("remote is invalid")
    safe_remote = resolve_safe_fetch_remote(root, remote)
    if safe_remote is None:
        raise RuntimeError("workspace state fetch destination is not trusted")
    ref = _state_ref(issue_number)
    try:
        revision = remote_revision(root, safe_remote, ref)
        if revision is None:
            return None
        fetched = fetch_revision(root, safe_remote, ref)
    except GitTransportError as error:
        raise RuntimeError("workspace state v3 detection failed") from error
    if fetched != revision:
        raise RuntimeError("workspace state changed during v3 detection")
    paths = tuple(
        line
        for line in _git_text(
            root,
            "ls-tree",
            "-r",
            "--name-only",
            revision,
        ).splitlines()
        if line
    )
    try:
        snapshot = json.loads(
            _git_bytes(root, "show", f"{revision}:snapshot.json")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("workspace state snapshot is invalid") from error
    if (
        type(snapshot) is not dict
        or snapshot.get("schema_version") != 3
    ):
        return None
    return workspace_state_v3_migration_plan(
        issue_number=issue_number,
        source_revision=revision,
        source_paths=paths,
    )


def _conversion_transitions(
    snapshot: StateRefSnapshot,
    metadata: Mapping[tuple[int, str], Mapping[str, Any]],
) -> tuple[WorkspaceUpdate, ...]:
    prior: CampaignState | None = None
    transitions: list[WorkspaceUpdate] = []
    for event in snapshot.inbox:
        try:
            state = OptimizationCampaign().advance(
                AdvanceRequest(
                    snapshot.state.issue_number,
                    prior,
                    (event,),
                )
            ).state
        except Exception as error:
            raise WorkspaceStateConversionError(
                "workspace state v3 journal replay failed"
            ) from error
        phase = _workspace_phase(state.phase)
        candidates = _candidate_summaries(state, metadata)
        selected_patch = _selected_patch(snapshot, state, metadata)
        lineage = _selected_lineage(
            state,
            metadata,
            selected_patch,
        )
        external_ids = _external_operation_ids(
            snapshot,
            state,
            metadata,
        )
        transitions.append(
            WorkspaceUpdate(
                issue_number=state.issue_number,
                phase=phase,
                workspace_pull_request_number=(
                    lineage.workspace_pull_request_number
                    if lineage is not None
                    else None
                ),
                semantic_event=event.kind.value,
                candidates=candidates,
                selected_patch=selected_patch,
                external_operation_ids=external_ids,
                lineage=lineage,
            )
        )
        prior = state
    if prior != snapshot.state:
        raise WorkspaceStateConversionError(
            "workspace state v3 snapshot does not match journal replay"
        )
    return tuple(transitions)


def _workspace_phase(phase: CampaignPhase) -> WorkspacePhase:
    mapping = {
        CampaignPhase.SPECIFICATION: WorkspacePhase.SPECIFICATION,
        CampaignPhase.AWAITING_SPEC_APPROVAL: WorkspacePhase.SPECIFICATION,
        CampaignPhase.BASELINE: WorkspacePhase.EVALUATING,
        CampaignPhase.CANDIDATES: WorkspacePhase.EVALUATING,
        CampaignPhase.AWAITING_SELECTION: WorkspacePhase.AWAITING_SELECTION,
        CampaignPhase.DEPLOYMENT: WorkspacePhase.DEPLOYMENT,
        CampaignPhase.RETENTION: WorkspacePhase.RETENTION,
        CampaignPhase.COMPLETED: WorkspacePhase.COMPLETED,
    }
    try:
        return mapping[phase]
    except KeyError as error:
        raise WorkspaceStateConversionError(
            f"workspace phase {phase.value} cannot be represented in v4"
        ) from error


def _candidate_metadata(
    snapshot: StateRefSnapshot,
    issue_number: int,
) -> dict[tuple[int, str], Mapping[str, Any]]:
    metadata: dict[tuple[int, str], Mapping[str, Any]] = {}
    for item in snapshot.objects:
        match = _CANDIDATE_OBJECT.fullmatch(item.path)
        if match is None:
            continue
        generation = int(match.group(1))
        candidate_id = match.group(2)
        try:
            document = json.loads(item.content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WorkspaceStateConversionError(
                "candidate lineage object is invalid"
            ) from error
        _merge_candidate_metadata(
            metadata,
            generation=generation,
            candidate_id=candidate_id,
            issue_number=issue_number,
            document=document,
        )
    for record in snapshot.outbox:
        if record.kind != "candidate_attestation":
            continue
        candidate_id = record.payload.get("candidate_id")
        if type(candidate_id) is not str:
            raise WorkspaceStateConversionError(
                "candidate attestation lineage is invalid"
            )
        _merge_candidate_metadata(
            metadata,
            generation=record.generation,
            candidate_id=candidate_id,
            issue_number=issue_number,
            document=dict(record.payload),
        )
    return metadata


def _merge_candidate_metadata(
    metadata: dict[tuple[int, str], Mapping[str, Any]],
    *,
    generation: int,
    candidate_id: str,
    issue_number: int,
    document: Any,
) -> None:
    if (
        type(document) is not dict
        or document.get("candidate_id") != candidate_id
        or (
            "issue_number" in document
            and document["issue_number"] != issue_number
        )
    ):
        raise WorkspaceStateConversionError(
            "candidate lineage binding is invalid"
        )
    metrics = document.get("metrics", {})
    if type(metrics) is not dict:
        raise WorkspaceStateConversionError(
            "candidate metrics cannot be represented in v4"
        )
    normalized_metrics: dict[str, float] = {}
    for name, value in metrics.items():
        if (
            type(name) is not str
            or _IDENTIFIER.fullmatch(name) is None
            or type(value) not in {int, float}
            or isinstance(value, bool)
            or not isfinite(value)
        ):
            raise WorkspaceStateConversionError(
                "candidate metrics cannot be represented in v4"
            )
        normalized_metrics[name] = float(value)
    normalized = {
        "base_commit": document.get("base_commit"),
        "bundle_sha256": document.get("bundle_sha256"),
        "candidate_id": candidate_id,
        "draft_id": document.get("draft_id"),
        "eligible": document.get("eligible"),
        "evaluation_id": document.get("evaluation_id"),
        "evidence_sha256": document.get("evidence_sha256"),
        "metrics": normalized_metrics,
        "patch_sha256": document.get("patch_sha256"),
        "expected_tree": document.get(
            "expected_tree",
            document.get("tree_sha"),
        ),
        "required_checks": document.get("required_checks"),
        "required_checks_provenance": document.get(
            "required_checks_provenance"
        ),
        "workspace_pull_request_number": document.get(
            "workspace_pull_request_number"
        ),
    }
    key = (generation, candidate_id)
    existing = metadata.get(key)
    if existing is not None and existing != normalized:
        raise WorkspaceStateConversionError(
            "candidate lineage is ambiguous"
        )
    metadata[key] = normalized


def _candidate_summaries(
    state: CampaignState,
    metadata: Mapping[tuple[int, str], Mapping[str, Any]],
) -> tuple[CandidateSummary, ...]:
    summaries: list[CandidateSummary] = []
    for candidate in state.candidates:
        details = metadata.get(
            (state.generation, candidate.candidate_id),
            {},
        )
        evidence_sha256 = details.get("evidence_sha256")
        if (
            evidence_sha256 is not None
            and evidence_sha256 != candidate.evidence_sha256
        ):
            raise WorkspaceStateConversionError(
                "candidate evidence lineage changed"
            )
        eligible = details.get("eligible")
        if eligible is not None and eligible is not candidate.eligible:
            raise WorkspaceStateConversionError(
                "candidate eligibility lineage changed"
            )
        summaries.append(
            CandidateSummary(
                candidate_id=candidate.candidate_id,
                metrics=dict(details.get("metrics", {})),
                eligible=candidate.eligible,
                selected=(
                    candidate.candidate_id
                    == state.selected_candidate_id
                ),
            )
        )
    return tuple(summaries)


def _selected_patch(
    snapshot: StateRefSnapshot,
    state: CampaignState,
    metadata: Mapping[tuple[int, str], Mapping[str, Any]],
) -> bytes | None:
    selected = state.selected_candidate_id
    if selected is None:
        if state.merge_commit is not None:
            raise WorkspaceStateConversionError(
                "required selected lineage cannot be represented"
            )
        return None
    if state.merge_commit is None:
        raise WorkspaceStateConversionError(
            "required selected lineage cannot be represented"
        )
    candidate = next(
        (
            item
            for item in state.candidates
            if item.candidate_id == selected
        ),
        None,
    )
    details = metadata.get((state.generation, selected))
    if candidate is None or not candidate.eligible or details is None:
        raise WorkspaceStateConversionError(
            "required selected lineage cannot be represented"
        )
    if details.get("evidence_sha256") != candidate.evidence_sha256:
        raise WorkspaceStateConversionError(
            "required selected lineage cannot be represented"
        )
    patch_sha256 = details.get("patch_sha256")
    if (
        type(patch_sha256) is not str
        or _SHA256.fullmatch(patch_sha256) is None
    ):
        raise WorkspaceStateConversionError(
            "selected patch lineage cannot be represented"
        )
    matches = tuple(
        item
        for item in snapshot.objects
        if item.path == f"objects/patches/{patch_sha256}.patch"
    )
    if len(matches) != 1:
        raise WorkspaceStateConversionError(
            "selected patch lineage cannot be represented"
        )
    patch = matches[0].content
    if hashlib.sha256(patch).hexdigest() != patch_sha256:
        raise WorkspaceStateConversionError(
            "selected patch lineage cannot be represented"
        )
    try:
        patch_text = patch.decode("utf-8")
        reject_secret_content(patch_text)
    except (UnicodeDecodeError, ValueError) as error:
        raise WorkspaceStateConversionError(
            "selected patch lineage violates v4 privacy"
        ) from error
    return patch


def _selected_lineage(
    state: CampaignState,
    metadata: Mapping[tuple[int, str], Mapping[str, Any]],
    selected_patch: bytes | None,
) -> WorkspaceLineage | None:
    selected = state.selected_candidate_id
    if selected is None or selected_patch is None:
        return None
    details = metadata.get((state.generation, selected))
    candidate = next(
        (
            item
            for item in state.candidates
            if item.candidate_id == selected
        ),
        None,
    )
    if details is None or candidate is None:
        return None
    values = (
        state.spec_sha256,
        details.get("base_commit"),
        details.get("patch_sha256"),
        details.get("evidence_sha256"),
        details.get("bundle_sha256"),
        details.get("expected_tree"),
        details.get("workspace_pull_request_number"),
        details.get("required_checks"),
        details.get("required_checks_provenance"),
    )
    if any(value is None for value in values):
        return None
    try:
        return WorkspaceLineage(
            spec_sha256=state.spec_sha256,
            base_commit=details["base_commit"],
            patch_sha256=details["patch_sha256"],
            evidence_sha256=details["evidence_sha256"],
            bundle_sha256=details["bundle_sha256"],
            expected_tree=details["expected_tree"],
            selected_candidate_id=selected,
            workspace_pull_request_number=(
                details["workspace_pull_request_number"]
            ),
            required_checks=details["required_checks"],
            required_checks_provenance=(
                details["required_checks_provenance"]
            ),
        )
    except (TypeError, ValueError) as error:
        raise WorkspaceStateConversionError(
            "selected workspace lineage cannot be represented"
        ) from error


def _external_operation_ids(
    snapshot: StateRefSnapshot,
    state: CampaignState,
    metadata: Mapping[tuple[int, str], Mapping[str, Any]],
) -> tuple[str, ...]:
    values: list[str] = []
    if state.baseline_evaluation_id is not None:
        values.append(
            _external_id(
                state.baseline_evaluation_id,
                "baseline_evaluation_id",
            )
        )
    for candidate in state.candidates:
        details = metadata.get(
            (state.generation, candidate.candidate_id),
            {},
        )
        for key in ("draft_id", "evaluation_id"):
            value = details.get(key)
            if value is not None:
                values.append(_external_id(value, key))
    if state.merge_commit is not None:
        values.append(f"merge_commit:{state.merge_commit}")
    for record in snapshot.outbox:
        if record.sequence > state.sequence:
            continue
        for key in _EXTERNAL_ID_FIELDS:
            value = record.payload.get(key)
            if value is not None:
                values.append(_external_id(value, key))
    for event in snapshot.inbox[: state.sequence]:
        for key in _EXTERNAL_ID_FIELDS:
            value = event.payload.get(key)
            if value is not None:
                values.append(_external_id(value, key))
    if state.deployment_version is not None:
        values.append(
            _external_id(
                state.deployment_version,
                "deployment_version",
            )
        )
    return tuple(dict.fromkeys(values))


def _external_id(value: Any, field_name: str) -> str:
    if type(value) is int and value > 0:
        normalized = f"{field_name}:{value}"
    elif type(value) is str:
        normalized = value
    else:
        raise WorkspaceStateConversionError(
            "external operation ID cannot be represented in v4"
        )
    if _SAFE_EXTERNAL_ID.fullmatch(normalized) is None:
        raise WorkspaceStateConversionError(
            "external operation ID cannot be represented in v4"
        )
    try:
        reject_secret_content(normalized)
    except ValueError as error:
        raise WorkspaceStateConversionError(
            "external operation ID violates v4 privacy"
        ) from error
    return normalized


def _conversion_document(
    *,
    issue_number: int,
    source_ref: str,
    source_revision: str,
    transitions: tuple[WorkspaceUpdate, ...],
) -> dict[str, Any]:
    document = {
        "issue_number": issue_number,
        "schema_version": 1,
        "source_ref": source_ref,
        "source_revision": source_revision,
        "source_schema_version": 3,
        "target_schema_version": 4,
        "transitions": [
            _transition_document(item) for item in transitions
        ],
    }
    try:
        reject_secret_content(document)
    except ValueError as error:
        raise WorkspaceStateConversionError(
            "workspace conversion payload violates v4 privacy"
        ) from error
    return document


def _transition_document(update: WorkspaceUpdate) -> dict[str, Any]:
    patch = update.selected_patch
    try:
        patch_text = patch.decode("utf-8") if patch is not None else None
    except UnicodeDecodeError as error:
        raise WorkspaceStateConversionError(
            "selected patch cannot be represented in canonical payload"
        ) from error
    return {
        "candidates": [
            {
                "candidate_id": item.candidate_id,
                "eligible": item.eligible,
                "metrics": dict(item.metrics),
                "selected": item.selected,
            }
            for item in update.candidates
        ],
        "external_operation_ids": list(update.external_operation_ids),
        "issue_number": update.issue_number,
        "lineage": (
            {
                "base_commit": update.lineage.base_commit,
                "bundle_sha256": update.lineage.bundle_sha256,
                "evidence_sha256": update.lineage.evidence_sha256,
                "expected_tree": update.lineage.expected_tree,
                "patch_sha256": update.lineage.patch_sha256,
                "required_checks": dict(
                    update.lineage.required_checks
                ),
                "required_checks_provenance": (
                    update.lineage.required_checks_provenance
                ),
                "selected_candidate_id": (
                    update.lineage.selected_candidate_id
                ),
                "spec_sha256": update.lineage.spec_sha256,
                "workspace_pull_request_number": (
                    update.lineage.workspace_pull_request_number
                ),
            }
            if update.lineage is not None
            else None
        ),
        "phase": update.phase.value,
        "selected_patch": patch_text,
        "semantic_event": update.semantic_event,
        "workspace_pull_request_number": (
            update.workspace_pull_request_number
        ),
    }


def _canonical_json(document: Any) -> bytes:
    return (
        json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _repository_root(path: Path) -> Path:
    root = Path(os.path.abspath(path))
    completed = subprocess.run(
        ("git", "rev-parse", "--show-toplevel"),
        cwd=root,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("repository_root must be a Git worktree")
    discovered = Path(
        os.path.abspath(completed.stdout.decode("utf-8").strip())
    )
    if os.path.normcase(discovered) != os.path.normcase(root):
        raise ValueError("repository_root must be the Git worktree root")
    return root


def _issue_number(value: int) -> None:
    if type(value) is not int or value < 1:
        raise ValueError("issue_number must be a positive integer")


def _state_ref(issue_number: int) -> str:
    return f"refs/heads/foundry-opt/state/issue-{issue_number}"


def _git_text(cwd: Path, *arguments: str) -> str:
    return _git_bytes(cwd, *arguments).decode("utf-8").strip()


def _git_bytes(cwd: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("Git workspace migration inspection failed")
    return completed.stdout
