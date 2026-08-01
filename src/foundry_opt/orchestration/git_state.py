from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import hashlib
import json
from math import isfinite
import os
from pathlib import Path
import re
import subprocess
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit
from uuid import uuid4

from foundry_opt.orchestration.campaign import OptimizationCampaign
from foundry_opt.orchestration.models import (
    AdvanceRequest,
    CampaignEvent,
    CampaignPhase,
    CampaignState,
    CandidateRecord,
    EventKind,
    SpecFileHash,
)
from foundry_opt.security import reject_secret_content


_LEDGER_SCHEMA_VERSION = 3
_STATE_SCHEMA_VERSION = 2
_RECORD_SCHEMA_VERSION = 1
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_TEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,255}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_EVENT_PAYLOAD_FIELDS = {
    EventKind.ISSUE_CREATED: frozenset(),
    EventKind.ISSUE_EDITED: frozenset(),
    EventKind.ISSUE_DECLASSIFIED: frozenset(),
    EventKind.ISSUE_REOPENED: frozenset(),
    EventKind.ISSUE_CLOSED: frozenset(),
    EventKind.SPEC_POLICY_APPROVED: frozenset({"spec_sha256"}),
    EventKind.SPEC_REVIEW_REQUIRED: frozenset(
        {
            "base_ref_name",
            "files",
            "head_commit",
            "spec_sha256",
            "tree_sha",
        }
    ),
    EventKind.SPEC_HUMAN_APPROVED: frozenset(
        {
            "head_commit",
            "merge_commit",
            "pull_request_number",
            "spec_sha256",
        }
    ),
    EventKind.BASELINE_COMPLETED: frozenset({"evaluation_id"}),
    EventKind.CANDIDATE_EVALUATED: frozenset(
        {"candidate_id", "eligible", "evidence_sha256"}
    ),
    EventKind.CANDIDATE_ELIGIBILITY_REVISED: frozenset(
        {"candidate_id", "eligible"}
    ),
    EventKind.CANDIDATE_WORKERS_COMPLETED: frozenset(
        {"attempted_count", "eligible_count", "stop_reason"}
    ),
    EventKind.SLATE_PUBLISHED: frozenset(),
    EventKind.CANDIDATE_PR_OPENED: frozenset(
        {
            "binding_sha256",
            "candidate_id",
            "head_commit",
            "pull_request_number",
        }
    ),
    EventKind.CANDIDATE_PR_SYNCHRONIZED: frozenset(
        {
            "binding_sha256",
            "candidate_id",
            "head_commit",
            "pull_request_number",
        }
    ),
    EventKind.CANDIDATE_PR_EDITED: frozenset(
        {
            "binding_sha256",
            "candidate_id",
            "head_commit",
            "pull_request_number",
        }
    ),
    EventKind.CANDIDATE_PR_CLOSED: frozenset(
        {
            "binding_sha256",
            "candidate_id",
            "head_commit",
            "pull_request_number",
        }
    ),
    EventKind.CANDIDATE_PR_MERGED: frozenset(
        {
            "binding_sha256",
            "candidate_id",
            "head_commit",
            "merge_commit",
            "pull_request_number",
        }
    ),
    EventKind.CANDIDATE_SELECTION_FAILED: frozenset({"reason"}),
    EventKind.CANDIDATE_MERGED: frozenset(
        {"candidate_id", "merge_commit"}
    ),
    EventKind.DEPLOYMENT_WORKFLOW_OBSERVED: frozenset(
        {
            "attempt",
            "binding_sha256",
            "bundle_sha256",
            "candidate_id",
            "candidate_issue_number",
            "candidate_pull_request_number",
            "deployment_client_id",
            "draft_id",
            "effect_id",
            "evidence_sha256",
            "issue_number",
            "merge_actor",
            "merge_commit",
            "patch_sha256",
            "repository",
            "repository_id",
            "required_checks",
            "result_id",
            "run_actor",
            "run_conclusion",
            "run_id",
            "run_status",
            "run_url",
            "spec_sha256",
            "tree_sha",
            "workflow_actor",
            "workflow_id",
            "workflow_path",
            "workflow_ref",
            "workflow_trigger",
        }
    ),
    EventKind.DEPLOYMENT_FAILED: frozenset({"reason"}),
    EventKind.DEPLOYMENT_COMPLETED: frozenset({"deployment_version"}),
    EventKind.RETENTION_COMPLETED: frozenset({"retained"}),
}
_OUTBOX_PAYLOAD_FIELDS = frozenset(
    {
        "assignee",
        "assigned",
        "allowed_paths",
        "attestation_sha256",
        "attestation_path",
        "base_commit",
        "baseline_metrics",
        "branch",
        "base_ref_name",
        "bundle_sha256",
        "binding_sha256",
        "campaign_id",
        "candidate_id",
        "candidate_issue_number",
        "candidate_pull_request_number",
        "changed_paths",
        "candidate_slate",
        "commit_sha",
        "complexity",
        "created",
        "cutoff_at",
        "deadline_at",
        "files",
        "head_commit",
        "deployment_version",
        "deployment_client_id",
        "deployment_effect_id",
        "depends_on_effect_ids",
        "disposition",
        "deployed_metrics",
        "draft_id",
        "draft_metrics",
        "effect_id",
        "effect_kind",
        "eligible",
        "evaluation_id",
        "evaluation_policy_sha256",
        "evidence_path",
        "evidence_sha256",
        "evidence_url",
        "goal_sha256",
        "idempotency_key",
        "idea_id",
        "issue_number",
        "label",
        "lessons",
        "lineage_sha256",
        "max_changed_candidates",
        "marker",
        "merge_actor",
        "merge_commit",
        "metadata_sha256",
        "metrics",
        "motivation",
        "mutation_class",
        "parent_idea_ids",
        "patch_sha256",
        "patch_path",
        "phase",
        "portal_url",
        "pull_request_number",
        "repository",
        "repository_id",
        "ref",
        "required_opt_ins",
        "required_checks",
        "result",
        "result_commit",
        "result_id",
        "retained",
        "run_id",
        "run_actor",
        "run_conclusion",
        "run_status",
        "run_url",
        "source_sha256",
        "slot",
        "spec_sha256",
        "spec_classification",
        "selected_candidate_id",
        "specialist",
        "started_at",
        "state_sha256",
        "status",
        "target",
        "tree_sha",
        "attempt",
        "timeout_seconds",
        "workflow_actor",
        "workflow_id",
        "workflow_path",
        "workflow_ref",
        "workflow_trigger",
        "next_action",
        "reason",
        "work_kind",
        "worker_issue_number",
    }
)
_HASH_FIELDS = frozenset(
    {
        "evidence_sha256",
        "attestation_sha256",
        "bundle_sha256",
        "goal_sha256",
        "idempotency_key",
        "lineage_sha256",
        "patch_sha256",
        "spec_sha256",
        "state_sha256",
        "binding_sha256",
        "evaluation_policy_sha256",
        "metadata_sha256",
        "source_sha256",
    }
)
_NUMBER_FIELDS = frozenset(
    {
        "deployment_version",
        "candidate_issue_number",
        "candidate_pull_request_number",
        "issue_number",
        "max_changed_candidates",
        "pull_request_number",
        "repository_id",
        "attempt",
        "timeout_seconds",
        "workflow_id",
        "worker_issue_number",
    }
)
_NONNEGATIVE_NUMBER_FIELDS = frozenset(
    {"attempted_count", "eligible_count", "slot"}
)
_BOOLEAN_FIELDS = frozenset({"assigned", "created", "eligible", "retained"})
_COMMIT_FIELDS = frozenset(
    {
        "base_commit",
        "commit_sha",
        "head_commit",
        "merge_commit",
        "result_commit",
        "tree_sha",
    }
)
_IDENTIFIER_LIST_FIELDS = frozenset(
    {
        "depends_on_effect_ids",
        "parent_idea_ids",
        "required_checks",
        "required_opt_ins",
    }
)
_REDACTED_TEXT_FIELDS = frozenset(
    {"complexity", "motivation"}
)
_SENSITIVE_TEXT_MARKERS = (
    "authorization: bearer ",
    "authorization=bearer ",
    "access_token=",
    "access-token=",
    "api_key=",
    "api-key=",
    "client_secret=",
    "clientsecret=",
    "sharedaccesskey=",
    "sharedaccesssignature=",
    "?sig=",
    "&sig=",
)
_COMMIT_ENVIRONMENT = {
    "GIT_AUTHOR_NAME": "Foundry Optimizer Steward",
    "GIT_AUTHOR_EMAIL": "foundry-opt@example.invalid",
    "GIT_COMMITTER_NAME": "Foundry Optimizer Steward",
    "GIT_COMMITTER_EMAIL": "foundry-opt@example.invalid",
}


class StateRefError(RuntimeError):
    pass


class StateRefConflictError(StateRefError):
    pass


class StateRefCorruptionError(StateRefError):
    pass


class StateRefPrivacyError(StateRefCorruptionError):
    pass


@dataclass(frozen=True)
class OutboxRecord:
    record_id: str
    kind: str
    generation: int
    sequence: int
    payload: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = _RECORD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_version(self.schema_version, "outbox")
        _identifier(self.record_id, "record_id")
        _identifier(self.kind, "kind")
        _positive_integer(self.generation, "generation")
        _nonnegative_integer(self.sequence, "sequence")
        normalized = dict(self.payload)
        _validate_outbox_payload(normalized)
        object.__setattr__(self, "payload", MappingProxyType(normalized))


@dataclass(frozen=True)
class StateObject:
    path: str
    content: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or re.fullmatch(
            r"objects/(?:"
            r"candidates/g[1-9][0-9]*-[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
            r"\.json|"
            r"evidence/[0-9a-f]{64}\.json|"
            r"patches/[0-9a-f]{64}\.patch"
            r")",
            self.path,
        ) is None:
            raise ValueError("state object path is invalid")
        if not isinstance(self.content, bytes) or not self.content:
            raise ValueError("state object content must be non-empty bytes")
        if len(self.content) > 10_000_000:
            raise ValueError("state object content is too large")
        digest = hashlib.sha256(self.content).hexdigest()
        filename = self.path.rsplit("/", 1)[-1].split(".", 1)[0]
        if self.path.startswith(("objects/evidence/", "objects/patches/")):
            if filename != digest:
                raise ValueError("content-addressed state object hash changed")
        if self.path.endswith(".json"):
            try:
                document = json.loads(self.content)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError("state JSON object is invalid") from error
            if (
                self.path.startswith("objects/candidates/")
                and self.content != _canonical_json(document)
            ):
                raise ValueError("state JSON object must be canonical")
            _validate_state_object_privacy(document)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True)
class StateRefSnapshot:
    revision: str
    state: CampaignState
    inbox: tuple[CampaignEvent, ...]
    outbox: tuple[OutboxRecord, ...]
    objects: tuple[StateObject, ...] = ()


@dataclass(frozen=True)
class _LoadedRef:
    snapshot: StateRefSnapshot
    journal: tuple[dict[str, Any], ...]


class GitStateRef:
    """Durable, compare-and-swap campaign ledger on a steward-owned Git ref."""

    def __init__(self, *, remote: str = "origin") -> None:
        _identifier(remote, "remote")
        self._remote = remote

    def load(
        self,
        repository_root: Path,
        issue_number: int,
    ) -> StateRefSnapshot | None:
        root = _repository_root(repository_root)
        ref = _state_ref(issue_number)
        revision = self._remote_revision(root, ref)
        if revision is None:
            return None
        self._fetch(root, ref, revision)
        return self._load_revision(root, issue_number, revision).snapshot

    def commit(
        self,
        repository_root: Path,
        *,
        issue_number: int,
        expected_revision: str | None,
        state: CampaignState,
        inbox: tuple[CampaignEvent, ...] = (),
        outbox: tuple[OutboxRecord, ...] = (),
        objects: tuple[StateObject, ...] = (),
    ) -> StateRefSnapshot:
        root = _repository_root(repository_root)
        ref = _state_ref(issue_number)
        if expected_revision is not None:
            _commit(expected_revision, "expected_revision")
        current_revision = self._remote_revision(root, ref)
        if current_revision != expected_revision:
            raise StateRefConflictError(
                "state ref changed since it was loaded"
            )
        loaded: _LoadedRef | None = None
        if current_revision is not None:
            self._fetch(root, ref, current_revision)
            loaded = self._load_revision(
                root,
                issue_number,
                current_revision,
            )
        if state.issue_number != issue_number:
            raise ValueError("state issue does not match state ref")
        if not inbox and not outbox and not objects:
            raise ValueError("a state transaction cannot be empty")

        existing_inbox = loaded.snapshot.inbox if loaded else ()
        existing_outbox = loaded.snapshot.outbox if loaded else ()
        existing_objects = loaded.snapshot.objects if loaded else ()
        _require_new_ids(
            (event.event_id for event in existing_inbox),
            (event.event_id for event in inbox),
            "inbox event",
        )
        _require_new_ids(
            (record.record_id for record in existing_outbox),
            (record.record_id for record in outbox),
            "outbox record",
        )
        _require_new_ids(
            (item.path for item in existing_objects),
            (item.path for item in objects),
            "state object",
        )
        for record in outbox:
            if (
                record.generation != state.generation
                or record.sequence != state.sequence
            ):
                raise ValueError(
                    "outbox generation and sequence must match state"
                )

        all_inbox = (*existing_inbox, *inbox)
        all_outbox = (*existing_outbox, *outbox)
        all_objects = tuple(
            sorted(
                (*existing_objects, *objects),
                key=lambda item: item.path,
            )
        )
        inbox_documents = {
            event.event_id: _event_to_document(event)
            for event in all_inbox
        }
        outbox_documents = {
            record.record_id: _outbox_to_document(record)
            for record in all_outbox
        }
        state = replace(state, schema_version=_STATE_SCHEMA_VERSION)
        state_document = _state_to_document(state)
        replayed = _replay(issue_number, all_inbox)
        if replayed != state:
            raise ValueError("snapshot does not match inbox replay")

        state_sha256 = _document_sha256(state_document)
        journal = list(loaded.journal if loaded else ())
        previous_hash = (
            journal[-1]["entry_sha256"] if journal else None
        )
        entry_without_hash = {
            "generation": state.generation,
            "inbox": [
                {
                    "event_id": event.event_id,
                    "sha256": _document_sha256(
                        inbox_documents[event.event_id]
                    ),
                }
                for event in inbox
            ],
            "index": len(journal) + 1,
            "outbox": [
                {
                    "record_id": record.record_id,
                    "sha256": _document_sha256(
                        outbox_documents[record.record_id]
                    ),
                }
                for record in outbox
            ],
            "objects": [
                {
                    "path": item.path,
                    "sha256": item.sha256,
                }
                for item in sorted(objects, key=lambda value: value.path)
            ],
            "previous_sha256": previous_hash,
            "schema_version": _LEDGER_SCHEMA_VERSION,
            "sequence": state.sequence,
            "state_schema_version": state.schema_version,
            "state_sha256": state_sha256,
        }
        entry = {
            **entry_without_hash,
            "entry_sha256": _document_sha256(entry_without_hash),
        }
        journal.append(entry)
        snapshot_document = {
            "journal_head": entry["entry_sha256"],
            "schema_version": _LEDGER_SCHEMA_VERSION,
            "state": state_document,
        }
        files = {
            "journal.jsonl": b"".join(
                _canonical_json(item) for item in journal
            ),
            "snapshot.json": _canonical_json(snapshot_document),
            **{
                f"inbox/{event_id}.json": _canonical_json(document)
                for event_id, document in inbox_documents.items()
            },
            **{
                f"outbox/{record_id}.json": _canonical_json(document)
                for record_id, document in outbox_documents.items()
            },
            **{item.path: item.content for item in all_objects},
        }
        revision = self._write_commit(
            root,
            ref,
            current_revision,
            files,
            issue_number,
        )
        return StateRefSnapshot(
            revision=revision,
            state=state,
            inbox=all_inbox,
            outbox=all_outbox,
            objects=all_objects,
        )

    def _remote_revision(self, root: Path, ref: str) -> str | None:
        result = _run(
            root,
            "git",
            "ls-remote",
            "--heads",
            self._remote,
            ref,
        )
        output = result.stdout.decode("utf-8").strip()
        if not output:
            return None
        fields = output.split()
        if len(fields) != 2 or fields[1] != ref:
            raise StateRefCorruptionError("state ref metadata is invalid")
        _commit(fields[0], "state revision")
        return fields[0]

    def _fetch(self, root: Path, ref: str, revision: str) -> None:
        _run(root, "git", "fetch", "--quiet", self._remote, ref)
        fetched = _git_text(root, "rev-parse", "FETCH_HEAD^{commit}")
        if fetched != revision:
            raise StateRefConflictError(
                "state ref changed while it was being loaded"
            )

    def _load_revision(
        self,
        root: Path,
        issue_number: int,
        revision: str,
    ) -> _LoadedRef:
        try:
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
            if not paths or any(
                path not in {"journal.jsonl", "snapshot.json"}
                and not path.startswith(("inbox/", "outbox/", "objects/"))
                for path in paths
            ):
                raise StateRefCorruptionError(
                    "state ref contains unexpected paths"
                )
            snapshot_document = _read_document(
                _git_bytes(root, "show", f"{revision}:snapshot.json"),
                "snapshot",
            )
            journal_bytes = _git_bytes(
                root,
                "show",
                f"{revision}:journal.jsonl",
            )
            journal = tuple(
                _read_document(line, "journal entry")
                for line in journal_bytes.splitlines(keepends=True)
            )
            inbox_documents = {
                _record_id(path, "inbox"): _read_document(
                    _git_bytes(root, "show", f"{revision}:{path}"),
                    "inbox event",
                )
                for path in paths
                if path.startswith("inbox/")
            }
            outbox_documents = {
                _record_id(path, "outbox"): _read_document(
                    _git_bytes(root, "show", f"{revision}:{path}"),
                    "outbox record",
                )
                for path in paths
                if path.startswith("outbox/")
            }
            objects = tuple(
                StateObject(
                    path,
                    _git_bytes(root, "show", f"{revision}:{path}"),
                )
                for path in paths
                if path.startswith("objects/")
            )
            state, inbox, outbox, objects = _validate_ledger(
                issue_number,
                snapshot_document,
                journal,
                inbox_documents,
                outbox_documents,
                objects,
            )
        except StateRefError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise StateRefCorruptionError(
                "state ref is corrupt"
            ) from error
        return _LoadedRef(
            StateRefSnapshot(revision, state, inbox, outbox, objects),
            journal,
        )

    def _write_commit(
        self,
        root: Path,
        ref: str,
        parent: str | None,
        files: Mapping[str, bytes],
        issue_number: int,
    ) -> str:
        git_dir_text = _git_text(root, "rev-parse", "--git-dir")
        git_dir = Path(git_dir_text)
        if not git_dir.is_absolute():
            git_dir = root / git_dir
        index_path = git_dir / f"foundry-state-{os.getpid()}-{uuid4().hex}"
        environment = {"GIT_INDEX_FILE": str(index_path)}
        try:
            _run(
                root,
                "git",
                "read-tree",
                "--empty" if parent is None else f"{parent}^{{tree}}",
                environment=environment,
            )
            for path, content in sorted(files.items()):
                blob = _run(
                    root,
                    "git",
                    "hash-object",
                    "-w",
                    "--stdin",
                    input_bytes=content,
                ).stdout.decode("ascii").strip()
                _run(
                    root,
                    "git",
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    "100644",
                    blob,
                    path,
                    environment=environment,
                )
            tree = _git_text(
                root,
                "write-tree",
                environment=environment,
            )
            arguments = ["git", "commit-tree", tree]
            if parent is not None:
                arguments.extend(("-p", parent))
            arguments.extend(("-m", f"Update state for issue-{issue_number}"))
            commit_sha = _run(
                root,
                *arguments,
                environment={**environment, **_COMMIT_ENVIRONMENT},
            ).stdout.decode("ascii").strip()
        finally:
            index_path.unlink(missing_ok=True)
            Path(f"{index_path}.lock").unlink(missing_ok=True)

        lease = f"--force-with-lease={ref}:{parent or ''}"
        result = _run(
            root,
            "git",
            "push",
            lease,
            self._remote,
            f"{commit_sha}:{ref}",
            check=False,
        )
        if result.returncode != 0:
            raise StateRefConflictError(
                "state ref compare-and-swap failed"
            )
        return commit_sha


def _validate_ledger(
    issue_number: int,
    snapshot_document: Any,
    journal: tuple[dict[str, Any], ...],
    inbox_documents: Mapping[str, Any],
    outbox_documents: Mapping[str, Any],
    objects: tuple[StateObject, ...],
) -> tuple[
    CampaignState,
    tuple[CampaignEvent, ...],
    tuple[OutboxRecord, ...],
    tuple[StateObject, ...],
]:
    _exact_keys(
        snapshot_document,
        {"journal_head", "schema_version", "state"},
        "snapshot",
    )
    _require_ledger_version(
        snapshot_document["schema_version"],
        "snapshot",
    )
    _sha256_text(snapshot_document["journal_head"], "journal head")
    if not journal:
        raise StateRefCorruptionError("state journal is empty")
    state = _state_from_document(snapshot_document["state"])
    if state.issue_number != issue_number:
        raise StateRefCorruptionError("snapshot issue does not match ref")

    previous_hash: str | None = None
    inbox: list[CampaignEvent] = []
    outbox: list[OutboxRecord] = []
    seen_inbox: set[str] = set()
    seen_outbox: set[str] = set()
    seen_objects: set[str] = set()
    object_documents = {item.path: item for item in objects}
    if len(object_documents) != len(objects):
        raise StateRefCorruptionError("state object paths are not unique")
    replayed: CampaignState | None = None
    for expected_index, entry in enumerate(journal, 1):
        version = entry.get("schema_version")
        _require_ledger_version(version, "journal entry")
        keys = {
            "entry_sha256",
            "generation",
            "inbox",
            "index",
            "outbox",
            "previous_sha256",
            "schema_version",
            "sequence",
            "state_sha256",
        }
        if version == 3:
            keys.update({"objects", "state_schema_version"})
        _exact_keys(entry, keys, "journal entry")
        _exact_integer(entry["index"], "journal index")
        _positive_integer(entry["generation"], "journal generation")
        _nonnegative_integer(entry["sequence"], "journal sequence")
        _sha256_text(entry["entry_sha256"], "journal entry hash")
        _sha256_text(entry["state_sha256"], "journal state hash")
        if entry["previous_sha256"] is not None:
            _sha256_text(
                entry["previous_sha256"],
                "journal previous hash",
            )
        if entry["index"] != expected_index:
            raise StateRefCorruptionError("journal index is invalid")
        if entry["previous_sha256"] != previous_hash:
            raise StateRefCorruptionError("journal hash chain is invalid")
        entry_without_hash = {
            key: value
            for key, value in entry.items()
            if key != "entry_sha256"
        }
        if entry["entry_sha256"] != _document_sha256(entry_without_hash):
            raise StateRefCorruptionError("journal entry hash is invalid")
        for reference in _reference_list(
            entry["inbox"], "event_id", "inbox"
        ):
            event_id = reference["event_id"]
            if event_id in seen_inbox or event_id not in inbox_documents:
                raise StateRefCorruptionError(
                    "journal inbox reference is invalid"
                )
            document = inbox_documents[event_id]
            if reference["sha256"] != _document_sha256(document):
                raise StateRefCorruptionError("inbox hash is invalid")
            event = _event_from_document(document)
            if event.event_id != event_id:
                raise StateRefCorruptionError(
                    "inbox identity is invalid"
                )
            inbox.append(event)
            seen_inbox.add(event_id)
        for reference in _reference_list(
            entry["outbox"], "record_id", "outbox"
        ):
            record_id = reference["record_id"]
            if record_id in seen_outbox or record_id not in outbox_documents:
                raise StateRefCorruptionError(
                    "journal outbox reference is invalid"
                )
            document = outbox_documents[record_id]
            if reference["sha256"] != _document_sha256(document):
                raise StateRefCorruptionError("outbox hash is invalid")
            record = _outbox_from_document(document)
            if record.record_id != record_id:
                raise StateRefCorruptionError(
                    "outbox identity is invalid"
                )
            if (
                record.generation != entry["generation"]
                or record.sequence != entry["sequence"]
            ):
                raise StateRefCorruptionError(
                    "outbox checkpoint is invalid"
                )
            outbox.append(record)
            seen_outbox.add(record_id)
        if version == 3:
            for reference in _object_reference_list(entry["objects"]):
                path = reference["path"]
                if path in seen_objects or path not in object_documents:
                    raise StateRefCorruptionError(
                        "journal state object reference is invalid"
                    )
                item = object_documents[path]
                if reference["sha256"] != item.sha256:
                    raise StateRefCorruptionError(
                        "state object hash is invalid"
                    )
                seen_objects.add(path)
        replayed = _advance(issue_number, replayed, tuple(inbox), entry)
        previous_hash = entry["entry_sha256"]

    if set(inbox_documents) != seen_inbox:
        raise StateRefCorruptionError("unreferenced inbox record")
    if set(outbox_documents) != seen_outbox:
        raise StateRefCorruptionError("unreferenced outbox record")
    if set(object_documents) != seen_objects:
        raise StateRefCorruptionError("unreferenced state object")
    if snapshot_document["journal_head"] != previous_hash:
        raise StateRefCorruptionError("snapshot journal head is invalid")
    if replayed != state:
        raise StateRefCorruptionError("snapshot does not match replay")
    return state, tuple(inbox), tuple(outbox), objects


def _advance(
    issue_number: int,
    prior: CampaignState | None,
    all_inbox: tuple[CampaignEvent, ...],
    entry: Mapping[str, Any],
) -> CampaignState:
    event_count = len(entry["inbox"])
    events = all_inbox[-event_count:] if event_count else ()
    if events:
        replayed = OptimizationCampaign().advance(
            AdvanceRequest(issue_number, prior, events)
        ).state
    elif prior is not None:
        replayed = prior
    else:
        raise StateRefCorruptionError(
            "the first journal entry requires an inbox event"
        )
    state_schema_version = (
        entry["state_schema_version"]
        if entry["schema_version"] == 3
        else entry["schema_version"]
    )
    _require_state_version(state_schema_version, "journal state")
    replayed = replace(replayed, schema_version=state_schema_version)
    if (
        entry["generation"] != replayed.generation
        or entry["sequence"] != replayed.sequence
        or entry["state_sha256"]
        != _document_sha256(_state_to_document(replayed))
    ):
        raise StateRefCorruptionError(
            "journal state checkpoint is invalid"
        )
    return replayed


def _replay(
    issue_number: int,
    events: tuple[CampaignEvent, ...],
) -> CampaignState:
    if not events:
        raise ValueError("initial state requires an inbox event")
    return OptimizationCampaign().advance(
        AdvanceRequest(issue_number, None, events)
    ).state


def _state_to_document(state: CampaignState) -> dict[str, Any]:
    if state.schema_version == 1:
        return _state_to_document_v1(state)
    if state.schema_version == 2:
        return _state_to_document_v2(state)
    raise ValueError("unsupported campaign state schema_version")


def _state_to_document_v1(state: CampaignState) -> dict[str, Any]:
    if any(
        (
            state.spec_base_ref_name,
            state.spec_head_commit,
            state.spec_tree_sha,
            state.spec_files,
        )
    ):
        raise ValueError("v1 campaign state cannot contain spec policy fields")
    return _state_to_document_common(state, include_spec_policy=False)


def _state_to_document_v2(state: CampaignState) -> dict[str, Any]:
    return _state_to_document_common(state, include_spec_policy=True)


def _state_to_document_common(
    state: CampaignState,
    *,
    include_spec_policy: bool,
) -> dict[str, Any]:
    if type(state.schema_version) is not int:
        raise ValueError("unsupported campaign state schema_version")
    _positive_integer(state.issue_number, "issue_number")
    _positive_integer(state.generation, "generation")
    _nonnegative_integer(state.sequence, "sequence")
    if type(state.phase) is not CampaignPhase:
        raise ValueError("phase must be a CampaignPhase")
    for event_id in state.processed_event_ids:
        _identifier(event_id, "processed event ID")
    if any(
        type(candidate) is not CandidateRecord
        for candidate in state.candidates
    ):
        raise ValueError("candidates must be CandidateRecord values")
    document = {
        "baseline_evaluation_id": state.baseline_evaluation_id,
        "block_reason": state.block_reason,
        "candidates": [
            _candidate_to_document(candidate)
            for candidate in state.candidates
        ],
        "deployment_version": state.deployment_version,
        "generation": state.generation,
        "issue_number": state.issue_number,
        "merge_commit": state.merge_commit,
        "phase": state.phase.value,
        "processed_event_ids": list(state.processed_event_ids),
        "schema_version": state.schema_version,
        "selected_candidate_id": state.selected_candidate_id,
        "sequence": state.sequence,
        "spec_sha256": state.spec_sha256,
    }
    if include_spec_policy:
        document.update(
            {
                "spec_base_ref_name": state.spec_base_ref_name,
                "spec_files": [
                    {"path": item.path, "sha256": item.sha256}
                    for item in state.spec_files
                ],
                "spec_head_commit": state.spec_head_commit,
                "spec_tree_sha": state.spec_tree_sha,
            }
        )
    return document


def _state_from_document(document: Any) -> CampaignState:
    if type(document) is not dict:
        raise StateRefCorruptionError("campaign state fields are invalid")
    version = document.get("schema_version")
    _require_state_version(version, "campaign state")
    if version == 1:
        return _state_from_document_v1(document)
    return _state_from_document_v2(document)


def _state_from_document_v1(document: dict[str, Any]) -> CampaignState:
    keys = {
        "baseline_evaluation_id",
        "block_reason",
        "candidates",
        "deployment_version",
        "generation",
        "issue_number",
        "merge_commit",
        "phase",
        "processed_event_ids",
        "schema_version",
        "selected_candidate_id",
        "sequence",
        "spec_sha256",
    }
    _exact_keys(document, keys, "campaign state")
    return _state_from_document_common(
        document,
        spec_base_ref_name=None,
        spec_head_commit=None,
        spec_tree_sha=None,
        spec_files=(),
    )


def _state_from_document_v2(document: dict[str, Any]) -> CampaignState:
    keys = {
        "baseline_evaluation_id",
        "block_reason",
        "candidates",
        "deployment_version",
        "generation",
        "issue_number",
        "merge_commit",
        "phase",
        "processed_event_ids",
        "schema_version",
        "selected_candidate_id",
        "sequence",
        "spec_base_ref_name",
        "spec_files",
        "spec_head_commit",
        "spec_sha256",
        "spec_tree_sha",
    }
    _exact_keys(document, keys, "campaign state")
    _nullable_string(
        document["spec_base_ref_name"], "spec_base_ref_name"
    )
    _nullable_string(document["spec_head_commit"], "spec_head_commit")
    _nullable_string(document["spec_tree_sha"], "spec_tree_sha")
    spec_files_document = document["spec_files"]
    if type(spec_files_document) is not list:
        raise StateRefCorruptionError("spec_files must be a list")
    try:
        spec_files = tuple(
            SpecFileHash(**item) for item in spec_files_document
        )
    except (TypeError, ValueError) as error:
        raise StateRefCorruptionError("spec_files are invalid") from error
    return _state_from_document_common(
        document,
        spec_base_ref_name=document["spec_base_ref_name"],
        spec_head_commit=document["spec_head_commit"],
        spec_tree_sha=document["spec_tree_sha"],
        spec_files=spec_files,
    )


def _state_from_document_common(
    document: dict[str, Any],
    *,
    spec_base_ref_name: str | None,
    spec_head_commit: str | None,
    spec_tree_sha: str | None,
    spec_files: tuple[SpecFileHash, ...],
) -> CampaignState:
    _positive_integer(document["issue_number"], "issue_number")
    _positive_integer(document["generation"], "generation")
    _nonnegative_integer(document["sequence"], "sequence")
    phase_text = _string(document["phase"], "phase")
    try:
        phase = CampaignPhase(phase_text)
    except ValueError as error:
        raise StateRefCorruptionError("campaign phase is invalid") from error
    processed = _string_list(
        document["processed_event_ids"], "processed_event_ids"
    )
    for event_id in processed:
        _identifier(event_id, "processed event ID")
    candidates_document = document["candidates"]
    if type(candidates_document) is not list:
        raise StateRefCorruptionError("candidates must be a list")
    candidates = tuple(
        _candidate_from_document(item) for item in candidates_document
    )
    _nullable_string(document["spec_sha256"], "spec_sha256")
    _nullable_string(
        document["baseline_evaluation_id"],
        "baseline_evaluation_id",
    )
    _nullable_string(
        document["selected_candidate_id"],
        "selected_candidate_id",
    )
    _nullable_string(document["merge_commit"], "merge_commit")
    _nullable_positive_integer(
        document["deployment_version"],
        "deployment_version",
    )
    _nullable_string(document["block_reason"], "block_reason")
    return CampaignState(
        issue_number=document["issue_number"],
        generation=document["generation"],
        sequence=document["sequence"],
        phase=phase,
        schema_version=document["schema_version"],
        processed_event_ids=processed,
        spec_sha256=document["spec_sha256"],
        spec_base_ref_name=spec_base_ref_name,
        spec_head_commit=spec_head_commit,
        spec_tree_sha=spec_tree_sha,
        spec_files=spec_files,
        baseline_evaluation_id=document["baseline_evaluation_id"],
        candidates=candidates,
        selected_candidate_id=document["selected_candidate_id"],
        merge_commit=document["merge_commit"],
        deployment_version=document["deployment_version"],
        block_reason=document["block_reason"],
    )


def _candidate_to_document(candidate: CandidateRecord) -> dict[str, Any]:
    return {
        "candidate_id": candidate.candidate_id,
        "eligible": candidate.eligible,
        "evidence_sha256": candidate.evidence_sha256,
        "schema_version": _RECORD_SCHEMA_VERSION,
    }


def _candidate_from_document(document: Any) -> CandidateRecord:
    _exact_keys(
        document,
        {
            "candidate_id",
            "eligible",
            "evidence_sha256",
            "schema_version",
        },
        "candidate",
    )
    _require_version(document["schema_version"], "candidate")
    _string(document["candidate_id"], "candidate_id")
    _boolean(document["eligible"], "eligible")
    _string(document["evidence_sha256"], "evidence_sha256")
    return CandidateRecord(
        candidate_id=document["candidate_id"],
        eligible=document["eligible"],
        evidence_sha256=document["evidence_sha256"],
    )


def _event_to_document(event: CampaignEvent) -> dict[str, Any]:
    _identifier(event.event_id, "event_id")
    _positive_integer(event.generation, "generation")
    if type(event.kind) is not EventKind:
        raise ValueError("kind must be an EventKind")
    payload = dict(event.payload)
    _validate_event_payload(event.kind, payload)
    return {
        "event_id": event.event_id,
        "generation": event.generation,
        "kind": event.kind.value,
        "occurred_at": _datetime_text(event.occurred_at),
        "payload": payload,
        "schema_version": _RECORD_SCHEMA_VERSION,
    }


def _event_from_document(document: Any) -> CampaignEvent:
    _exact_keys(
        document,
        {
            "event_id",
            "generation",
            "kind",
            "occurred_at",
            "payload",
            "schema_version",
        },
        "campaign event",
    )
    _require_version(document["schema_version"], "campaign event")
    _identifier(document["event_id"], "event_id")
    _positive_integer(document["generation"], "generation")
    kind_text = _string(document["kind"], "kind")
    try:
        kind = EventKind(kind_text)
    except ValueError as error:
        raise StateRefCorruptionError("event kind is invalid") from error
    occurred_at = _parse_datetime(document["occurred_at"])
    if type(document["payload"]) is not dict:
        raise StateRefCorruptionError("event payload must be an object")
    _validate_event_payload(kind, document["payload"])
    return CampaignEvent(
        event_id=document["event_id"],
        kind=kind,
        generation=document["generation"],
        occurred_at=occurred_at,
        payload=document["payload"],
    )


def _outbox_to_document(record: OutboxRecord) -> dict[str, Any]:
    return {
        "generation": record.generation,
        "kind": record.kind,
        "payload": dict(record.payload),
        "record_id": record.record_id,
        "schema_version": record.schema_version,
        "sequence": record.sequence,
    }


def _outbox_from_document(document: Any) -> OutboxRecord:
    _exact_keys(
        document,
        {
            "generation",
            "kind",
            "payload",
            "record_id",
            "schema_version",
            "sequence",
        },
        "outbox record",
    )
    _require_version(document["schema_version"], "outbox record")
    _identifier(document["record_id"], "record_id")
    _identifier(document["kind"], "kind")
    _positive_integer(document["generation"], "generation")
    _nonnegative_integer(document["sequence"], "sequence")
    if type(document["payload"]) is not dict:
        raise StateRefCorruptionError("outbox payload must be an object")
    return OutboxRecord(**document)


def _validate_event_payload(
    kind: EventKind,
    payload: Mapping[str, Any],
) -> None:
    expected = _EVENT_PAYLOAD_FIELDS[kind]
    if (
        kind is EventKind.SPEC_REVIEW_REQUIRED
        and set(payload) == {"spec_sha256"}
    ):
        _validate_payload_value("spec_sha256", payload["spec_sha256"])
        return
    if (
        kind is EventKind.SPEC_HUMAN_APPROVED
        and not payload
    ):
        return
    if set(payload) != expected:
        raise StateRefPrivacyError(
            f"{kind.value} payload violates the privacy allowlist"
        )
    for key, value in payload.items():
        _validate_payload_value(key, value)


def _validate_outbox_payload(payload: Mapping[str, Any]) -> None:
    disallowed = set(payload) - _OUTBOX_PAYLOAD_FIELDS
    if disallowed:
        raise StateRefPrivacyError(
            "outbox payload violates the privacy allowlist"
        )
    for key, value in payload.items():
        _validate_payload_value(key, value)


def _validate_candidate_slate(value: Any) -> None:
    if not isinstance(value, list) or not value:
        raise StateRefPrivacyError(
            "candidate_slate must be a non-empty list"
        )
    candidate_ids: list[str] = []
    for row in value:
        if type(row) is not dict or set(row) != {
            "candidate_id",
            "deltas",
            "draft_id",
            "evidence_sha256",
            "evidence_url",
            "guardrails",
            "metrics",
            "rank",
        }:
            raise StateRefPrivacyError("candidate slate row is invalid")
        try:
            _identifier(row["candidate_id"], "candidate_id")
            _identifier(row["draft_id"], "draft_id")
            _sha256_text(row["evidence_sha256"], "evidence_sha256")
            _positive_integer(row["rank"], "rank")
        except (TypeError, ValueError) as error:
            raise StateRefPrivacyError(
                "candidate slate identity is invalid"
            ) from error
        if not str(row["draft_id"]).startswith("draft-"):
            raise StateRefPrivacyError(
                "candidate slate draft is invalid"
            )
        for field_name in ("deltas", "metrics"):
            values = row[field_name]
            if not isinstance(values, dict):
                raise StateRefPrivacyError(
                    "candidate slate aggregates are invalid"
                )
            for name, metric in values.items():
                try:
                    _identifier(name, "metric")
                except ValueError as error:
                    raise StateRefPrivacyError(
                        "candidate slate metric is invalid"
                    ) from error
                if (
                    not isinstance(metric, (int, float))
                    or isinstance(metric, bool)
                    or not isfinite(metric)
                ):
                    raise StateRefPrivacyError(
                        "candidate slate aggregate is invalid"
                    )
        guardrails = row["guardrails"]
        if not isinstance(guardrails, dict):
            raise StateRefPrivacyError(
                "candidate slate guardrails are invalid"
            )
        for name, outcome in guardrails.items():
            try:
                _identifier(name, "guardrail")
            except ValueError as error:
                raise StateRefPrivacyError(
                    "candidate slate guardrail is invalid"
                ) from error
            if outcome not in {"pass", "fail"}:
                raise StateRefPrivacyError(
                    "candidate slate guardrail outcome is invalid"
                )
        url = row["evidence_url"]
        if (
            not isinstance(url, str)
            or len(url) > 2048
            or re.fullmatch(
                r"https://github\.com/"
                r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/blob/"
                r"foundry-opt/state/issue-[1-9][0-9]*/"
                r"objects/evidence/[0-9a-f]{64}\.json",
                url,
            )
            is None
        ):
            raise StateRefPrivacyError(
                "candidate slate evidence URL is invalid"
            )
        candidate_ids.append(row["candidate_id"])
    if len(candidate_ids) != len(set(candidate_ids)):
        raise StateRefPrivacyError(
            "candidate slate candidates must be unique"
        )


def _validate_payload_value(key: str, value: Any) -> None:
    if key in {
        "attestation_path",
        "evidence_path",
        "patch_path",
        "workflow_path",
    }:
        try:
            SpecFileHash(path=value, sha256="0" * 64)
        except (TypeError, ValueError) as error:
            raise StateRefPrivacyError(
                f"{key} must be a privacy-safe repository path"
            ) from error
        return
    if key in {"merge_actor", "run_actor", "workflow_actor"}:
        if (
            not isinstance(value, str)
            or re.fullmatch(
                r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}|"
                r"[A-Za-z0-9-]{0,34}\[bot\])",
                value,
            )
            is None
        ):
            raise StateRefPrivacyError(
                f"{key} must be a GitHub login"
            )
        return
    if key == "run_url":
        try:
            parsed = urlsplit(value)
            parts = tuple(part for part in parsed.path.split("/") if part)
        except (TypeError, ValueError) as error:
            raise StateRefPrivacyError("run_url is invalid") from error
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or len(parts) != 5
            or parts[2:4] != ("actions", "runs")
            or not parts[4].isdigit()
        ):
            raise StateRefPrivacyError("run_url is invalid")
        return
    if key == "portal_url":
        try:
            parsed = urlsplit(value)
        except (TypeError, ValueError) as error:
            raise StateRefPrivacyError("portal_url is invalid") from error
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"ai.azure.com", "portal.azure.com"}
            or parsed.port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise StateRefPrivacyError("portal_url is invalid")
        return
    if key == "run_id":
        if type(value) is int and value >= 1:
            return
        try:
            _identifier(value, "run_id")
        except (TypeError, ValueError) as error:
            raise StateRefPrivacyError(
                "run_id must be a positive number or identifier"
            ) from error
        return
    if key == "candidate_slate":
        _validate_candidate_slate(value)
        return
    if key == "marker":
        if (
            not isinstance(value, str)
            or re.fullmatch(
                r"<!-- foundry-opt:candidate-pr:"
                r"issue-[1-9][0-9]*:g[1-9][0-9]*:"
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}:"
                r"[0-9a-f]{20} -->",
                value,
            )
            is None
        ):
            raise StateRefPrivacyError("marker is invalid")
        return
    if key == "files":
        if not isinstance(value, list) or not value:
            raise StateRefPrivacyError(
                "files must be a non-empty list of pinned hashes"
            )
        try:
            files = tuple(SpecFileHash(**item) for item in value)
        except (TypeError, ValueError) as error:
            raise StateRefPrivacyError(
                "files must contain privacy-safe paths and hashes"
            ) from error
        if len({item.path for item in files}) != len(files):
            raise StateRefPrivacyError("files must contain unique paths")
        return
    if key in {"allowed_paths", "changed_paths"}:
        if not isinstance(value, list):
            raise StateRefPrivacyError(
                f"{key} must be a path list"
            )
        try:
            paths = tuple(
                SpecFileHash(path=item, sha256="0" * 64).path
                for item in value
            )
        except (TypeError, ValueError) as error:
            raise StateRefPrivacyError(
                f"{key} must contain privacy-safe paths"
            ) from error
        if len(set(paths)) != len(paths):
            raise StateRefPrivacyError(
                f"{key} must contain unique paths"
            )
        return
    if key in _IDENTIFIER_LIST_FIELDS:
        if not isinstance(value, list):
            raise StateRefPrivacyError(f"{key} must be an identifier list")
        try:
            for item in value:
                _identifier(item, key)
        except (TypeError, ValueError) as error:
            raise StateRefPrivacyError(
                f"{key} must be an identifier list"
            ) from error
        if len(set(value)) != len(value):
            raise StateRefPrivacyError(f"{key} must contain unique values")
        return
    if key == "lessons":
        if not isinstance(value, list) or not value:
            raise StateRefPrivacyError(
                "lessons must be non-empty redacted text"
            )
        for item in value:
            _validate_redacted_text(item, "lesson")
        return
    if key in {
        "baseline_metrics",
        "deployed_metrics",
        "draft_metrics",
        "metrics",
    }:
        if not isinstance(value, dict):
            raise StateRefPrivacyError("metrics must be an aggregate mapping")
        for name, metric in value.items():
            try:
                _identifier(name, "metric")
            except ValueError as error:
                raise StateRefPrivacyError(
                    "metrics must use identifier keys"
                ) from error
            if (
                not isinstance(metric, (int, float))
                or isinstance(metric, bool)
                or not isfinite(metric)
            ):
                raise StateRefPrivacyError(
                    "metrics must contain finite aggregates"
                )
        return
    if key in _HASH_FIELDS:
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise StateRefPrivacyError(f"{key} must be a SHA-256 digest")
        return
    if key in _NUMBER_FIELDS:
        try:
            _positive_integer(value, key)
        except (TypeError, ValueError) as error:
            raise StateRefPrivacyError(
                f"{key} must be a positive integer"
            ) from error
        return
    if key in _NONNEGATIVE_NUMBER_FIELDS:
        try:
            _nonnegative_integer(value, key)
        except (TypeError, ValueError) as error:
            raise StateRefPrivacyError(
                f"{key} must be a non-negative integer"
            ) from error
        return
    if key in _BOOLEAN_FIELDS:
        if type(value) is not bool:
            raise StateRefPrivacyError(f"{key} must be boolean")
        return
    if key in _COMMIT_FIELDS:
        if not isinstance(value, str) or not _COMMIT.fullmatch(value):
            raise StateRefPrivacyError(f"{key} must be a full Git commit")
        return
    if key in _REDACTED_TEXT_FIELDS:
        _validate_redacted_text(value, key)
        return
    if not isinstance(value, str) or not _SAFE_TEXT.fullmatch(value):
        raise StateRefPrivacyError(
            f"{key} must be allowlisted metadata"
        )


def _validate_redacted_text(value: Any, key: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise StateRefPrivacyError(
            f"{key} must be bounded redacted text"
        )
    if any(
        marker in value.casefold()
        for marker in _SENSITIVE_TEXT_MARKERS
    ):
        raise StateRefPrivacyError(
            f"{key} contains sensitive content"
        )
    try:
        reject_secret_content(value)
    except ValueError as error:
        raise StateRefPrivacyError(
            f"{key} contains sensitive content"
        ) from error


def _validate_state_object_privacy(value: Any, key: str = "object") -> None:
    forbidden = {
        "credential",
        "dataset_row",
        "prompt",
        "raw_prompt",
        "raw_response",
        "response",
        "secret",
        "token",
        "tool_payload",
        "trace",
    }
    if isinstance(value, dict):
        for child_key, child in value.items():
            if not isinstance(child_key, str):
                raise StateRefPrivacyError(
                    "state object keys must be text"
                )
            normalized = child_key.casefold().replace("-", "_")
            if normalized in forbidden:
                raise StateRefPrivacyError(
                    "state object contains prohibited raw content"
                )
            _validate_state_object_privacy(child, child_key)
        return
    if isinstance(value, list):
        for child in value:
            _validate_state_object_privacy(child, key)
        return
    if isinstance(value, str):
        if any(
            marker in value.casefold()
            for marker in _SENSITIVE_TEXT_MARKERS
        ):
            raise StateRefPrivacyError(
                "state object contains sensitive content"
            )
        try:
            reject_secret_content(value)
        except ValueError as error:
            raise StateRefPrivacyError(
                "state object contains sensitive content"
            ) from error
        return
    if value is None or type(value) in {bool, int, float}:
        if isinstance(value, float) and not isfinite(value):
            raise StateRefPrivacyError(
                "state object numbers must be finite"
            )
        return
    raise StateRefPrivacyError("state object value is invalid")


def _read_document(content: bytes, description: str) -> Any:
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StateRefCorruptionError(
            f"{description} is not valid JSON"
        ) from error
    if content != _canonical_json(document):
        raise StateRefCorruptionError(
            f"{description} is not canonical JSON"
        )
    return document


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


def _document_sha256(document: Any) -> str:
    return hashlib.sha256(_canonical_json(document)).hexdigest()


def _reference_list(
    value: Any,
    identity_key: str,
    description: str,
) -> tuple[dict[str, str], ...]:
    if type(value) is not list:
        raise StateRefCorruptionError(
            f"journal {description} references must be a list"
        )
    references: list[dict[str, str]] = []
    for item in value:
        _exact_keys(
            item,
            {identity_key, "sha256"},
            f"{description} reference",
        )
        _identifier(item[identity_key], identity_key)
        if (
            not isinstance(item["sha256"], str)
            or not _SHA256.fullmatch(item["sha256"])
        ):
            raise StateRefCorruptionError(
                f"{description} reference hash is invalid"
            )
        references.append(item)
    return tuple(references)


def _object_reference_list(value: Any) -> tuple[dict[str, str], ...]:
    if type(value) is not list:
        raise StateRefCorruptionError(
            "journal state object references must be a list"
        )
    references: list[dict[str, str]] = []
    for item in value:
        _exact_keys(item, {"path", "sha256"}, "state object reference")
        if not isinstance(item["path"], str):
            raise StateRefCorruptionError(
                "state object reference path is invalid"
            )
        _sha256_text(item["sha256"], "state object reference hash")
        references.append(item)
    return tuple(references)


def _record_id(path: str, directory: str) -> str:
    prefix = f"{directory}/"
    if (
        not path.startswith(prefix)
        or not path.endswith(".json")
        or "/" in path[len(prefix) :]
    ):
        raise StateRefCorruptionError(
            f"{directory} record path is invalid"
        )
    record_id = path[len(prefix) : -5]
    _identifier(record_id, f"{directory} record ID")
    return record_id


def _state_ref(issue_number: int) -> str:
    _positive_integer(issue_number, "issue_number")
    return f"refs/heads/foundry-opt/state/issue-{issue_number}"


def _repository_root(path: Path) -> Path:
    root = path.resolve()
    try:
        discovered = Path(
            _git_text(root, "rev-parse", "--show-toplevel")
        ).resolve()
    except StateRefError as error:
        raise ValueError("repository_root must be a Git worktree") from error
    if discovered != root:
        raise ValueError("repository_root must be the Git worktree root")
    return root


def _run(
    cwd: Path,
    *arguments: str,
    input_bytes: bytes | None = None,
    environment: Mapping[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    process_environment = os.environ.copy()
    if environment:
        process_environment.update(environment)
    result = subprocess.run(
        arguments,
        cwd=cwd,
        env=process_environment,
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise StateRefError("Git state ref operation failed")
    return result


def _git_text(
    cwd: Path,
    *arguments: str,
    environment: Mapping[str, str] | None = None,
) -> str:
    return _run(
        cwd,
        "git",
        *arguments,
        environment=environment,
    ).stdout.decode("utf-8").strip()


def _git_bytes(cwd: Path, *arguments: str) -> bytes:
    return _run(cwd, "git", *arguments).stdout


def _exact_keys(
    value: Any,
    expected: set[str],
    description: str,
) -> None:
    if type(value) is not dict or set(value) != expected:
        raise StateRefCorruptionError(
            f"{description} fields are invalid"
        )


def _require_version(value: Any, description: str) -> None:
    if type(value) is not int or value != _RECORD_SCHEMA_VERSION:
        raise StateRefCorruptionError(
            f"unsupported {description} schema_version"
        )


def _require_state_version(value: Any, description: str) -> None:
    if type(value) is not int or value not in {1, 2}:
        raise StateRefCorruptionError(
            f"unsupported {description} schema_version"
        )


def _require_ledger_version(value: Any, description: str) -> None:
    if type(value) is not int or value not in {1, 2, 3}:
        raise StateRefCorruptionError(
            f"unsupported {description} schema_version"
        )


def _identifier(value: Any, description: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{description} must be an identifier")
    return value


def _commit(value: Any, description: str) -> str:
    if not isinstance(value, str) or not _COMMIT.fullmatch(value):
        raise ValueError(f"{description} must be a full Git commit")
    return value


def _sha256_text(value: Any, description: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{description} must be a SHA-256 digest")
    return value


def _exact_integer(value: Any, description: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{description} must be an integer")
    return value


def _positive_integer(value: Any, description: str) -> int:
    integer = _exact_integer(value, description)
    if integer < 1:
        raise ValueError(f"{description} must be positive")
    return integer


def _nonnegative_integer(value: Any, description: str) -> int:
    integer = _exact_integer(value, description)
    if integer < 0:
        raise ValueError(f"{description} must not be negative")
    return integer


def _nullable_positive_integer(value: Any, description: str) -> None:
    if value is not None:
        _positive_integer(value, description)


def _string(value: Any, description: str) -> str:
    if type(value) is not str:
        raise StateRefCorruptionError(f"{description} must be a string")
    return value


def _nullable_string(value: Any, description: str) -> None:
    if value is not None:
        _string(value, description)


def _boolean(value: Any, description: str) -> bool:
    if type(value) is not bool:
        raise StateRefCorruptionError(f"{description} must be boolean")
    return value


def _string_list(value: Any, description: str) -> tuple[str, ...]:
    if type(value) is not list or any(type(item) is not str for item in value):
        raise StateRefCorruptionError(
            f"{description} must be a string list"
        )
    return tuple(value)


def _datetime_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("event timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: Any) -> datetime:
    text = _string(value, "occurred_at")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise StateRefCorruptionError(
            "occurred_at is invalid"
        ) from error
    if parsed.tzinfo is None or _datetime_text(parsed) != text:
        raise StateRefCorruptionError(
            "occurred_at is not canonical"
        )
    return parsed


def _require_new_ids(
    existing: Any,
    added: Any,
    description: str,
) -> None:
    seen = set(existing)
    additions = tuple(added)
    if len(set(additions)) != len(additions) or seen.intersection(additions):
        raise ValueError(f"{description} IDs must be append-only and unique")
