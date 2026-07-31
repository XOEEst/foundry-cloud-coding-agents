from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from types import MappingProxyType
from typing import Any, Mapping
from uuid import uuid4

from foundry_opt.orchestration.campaign import OptimizationCampaign
from foundry_opt.orchestration.models import (
    AdvanceRequest,
    CampaignEvent,
    CampaignPhase,
    CampaignState,
    CandidateRecord,
    EventKind,
)


_SCHEMA_VERSION = 1
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
    EventKind.SPEC_REVIEW_REQUIRED: frozenset({"spec_sha256"}),
    EventKind.SPEC_HUMAN_APPROVED: frozenset(),
    EventKind.BASELINE_COMPLETED: frozenset({"evaluation_id"}),
    EventKind.CANDIDATE_EVALUATED: frozenset(
        {"candidate_id", "eligible", "evidence_sha256"}
    ),
    EventKind.SLATE_PUBLISHED: frozenset(),
    EventKind.CANDIDATE_MERGED: frozenset(
        {"candidate_id", "merge_commit"}
    ),
    EventKind.DEPLOYMENT_COMPLETED: frozenset({"deployment_version"}),
    EventKind.RETENTION_COMPLETED: frozenset({"retained"}),
}
_OUTBOX_PAYLOAD_FIELDS = frozenset(
    {
        "assignee",
        "branch",
        "candidate_id",
        "commit_sha",
        "deployment_version",
        "disposition",
        "eligible",
        "evaluation_id",
        "evidence_sha256",
        "idempotency_key",
        "issue_number",
        "label",
        "merge_commit",
        "phase",
        "pull_request_number",
        "ref",
        "retained",
        "spec_sha256",
        "state_sha256",
        "status",
    }
)
_HASH_FIELDS = frozenset(
    {
        "evidence_sha256",
        "idempotency_key",
        "spec_sha256",
        "state_sha256",
    }
)
_NUMBER_FIELDS = frozenset(
    {"deployment_version", "issue_number", "pull_request_number"}
)
_BOOLEAN_FIELDS = frozenset({"eligible", "retained"})
_COMMIT_FIELDS = frozenset({"commit_sha", "merge_commit"})
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
    schema_version: int = _SCHEMA_VERSION

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
class StateRefSnapshot:
    revision: str
    state: CampaignState
    inbox: tuple[CampaignEvent, ...]
    outbox: tuple[OutboxRecord, ...]


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
        if not inbox and not outbox:
            raise ValueError("a state transaction cannot be empty")

        existing_inbox = loaded.snapshot.inbox if loaded else ()
        existing_outbox = loaded.snapshot.outbox if loaded else ()
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
        inbox_documents = {
            event.event_id: _event_to_document(event)
            for event in all_inbox
        }
        outbox_documents = {
            record.record_id: _outbox_to_document(record)
            for record in all_outbox
        }
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
            "previous_sha256": previous_hash,
            "schema_version": _SCHEMA_VERSION,
            "sequence": state.sequence,
            "state_sha256": state_sha256,
        }
        entry = {
            **entry_without_hash,
            "entry_sha256": _document_sha256(entry_without_hash),
        }
        journal.append(entry)
        snapshot_document = {
            "journal_head": entry["entry_sha256"],
            "schema_version": _SCHEMA_VERSION,
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
                and not path.startswith(("inbox/", "outbox/"))
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
            state, inbox, outbox = _validate_ledger(
                issue_number,
                snapshot_document,
                journal,
                inbox_documents,
                outbox_documents,
            )
        except StateRefError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise StateRefCorruptionError(
                "state ref is corrupt"
            ) from error
        return _LoadedRef(
            StateRefSnapshot(revision, state, inbox, outbox),
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
) -> tuple[
    CampaignState,
    tuple[CampaignEvent, ...],
    tuple[OutboxRecord, ...],
]:
    _exact_keys(
        snapshot_document,
        {"journal_head", "schema_version", "state"},
        "snapshot",
    )
    _require_version(snapshot_document["schema_version"], "snapshot")
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
    replayed: CampaignState | None = None
    for expected_index, entry in enumerate(journal, 1):
        _exact_keys(
            entry,
            {
                "entry_sha256",
                "generation",
                "inbox",
                "index",
                "outbox",
                "previous_sha256",
                "schema_version",
                "sequence",
                "state_sha256",
            },
            "journal entry",
        )
        _require_version(entry["schema_version"], "journal entry")
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
        replayed = _advance(issue_number, replayed, tuple(inbox), entry)
        previous_hash = entry["entry_sha256"]

    if set(inbox_documents) != seen_inbox:
        raise StateRefCorruptionError("unreferenced inbox record")
    if set(outbox_documents) != seen_outbox:
        raise StateRefCorruptionError("unreferenced outbox record")
    if snapshot_document["journal_head"] != previous_hash:
        raise StateRefCorruptionError("snapshot journal head is invalid")
    if replayed != state:
        raise StateRefCorruptionError("snapshot does not match replay")
    return state, tuple(inbox), tuple(outbox)


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
    if type(state.schema_version) is not int or (
        state.schema_version != _SCHEMA_VERSION
    ):
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
    return {
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


def _state_from_document(document: Any) -> CampaignState:
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
    _require_version(document["schema_version"], "campaign state")
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
        "schema_version": _SCHEMA_VERSION,
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
        "schema_version": _SCHEMA_VERSION,
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


def _validate_payload_value(key: str, value: Any) -> None:
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
    if key in _BOOLEAN_FIELDS:
        if type(value) is not bool:
            raise StateRefPrivacyError(f"{key} must be boolean")
        return
    if key in _COMMIT_FIELDS:
        if not isinstance(value, str) or not _COMMIT.fullmatch(value):
            raise StateRefPrivacyError(f"{key} must be a full Git commit")
        return
    if not isinstance(value, str) or not _SAFE_TEXT.fullmatch(value):
        raise StateRefPrivacyError(
            f"{key} must be allowlisted metadata"
        )


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
    if type(value) is not int or value != _SCHEMA_VERSION:
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
