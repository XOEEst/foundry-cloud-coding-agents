from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping, Protocol
from uuid import uuid4

from foundry_opt import __version__
from foundry_opt.adapters.commands import CommandExitError
from foundry_opt.orchestration.git_state import (
    GitStateRef,
    StateRefConflictError,
    StateRefError,
    StateRefProposal,
    StateRefPushUnacknowledgedError,
    StateRefSnapshot,
    verified_copilot_git_proxy_session,
)
from foundry_opt.orchestration.git_transport import (
    GitTransportError,
    isolated_compare_and_swap_push,
    isolated_remote_revision,
)
from foundry_opt.orchestration.candidate_workers import (
    CandidateDesignArtifact,
    CandidateDesignIntent,
    CandidateDesignResult,
    CandidateDesignSubmissionRequest,
)
from foundry_opt.orchestration.issue_intake import GitIssueEventInbox
from foundry_opt.orchestration.models import EventKind
from foundry_opt.security import reject_secret_content
from foundry_opt.preflight.interfaces import CommandRunner


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_ZERO_COMMIT = "0" * 40
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BRANCH = re.compile(
    r"^copilot/[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$"
)
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
)
_COPILOT_APP_USER_ID = 198982749
_DISCOVERY_PAGE_SIZE = 100
_DISCOVERY_MAX_PAGES = 2
_DISCOVERY_LIMIT = 10
_PUSH_EVENT_MAX_PAGES = 3
_HANDOFF_COMMIT_ENVIRONMENT = {
    "GIT_AUTHOR_NAME": "Foundry Optimizer Handoff",
    "GIT_AUTHOR_EMAIL": "foundry-opt@example.invalid",
    "GIT_COMMITTER_NAME": "Foundry Optimizer Handoff",
    "GIT_COMMITTER_EMAIL": "foundry-opt@example.invalid",
}
_TRANSPORT_EVENT_KINDS = frozenset(
    {
        EventKind.ISSUE_CREATED,
        EventKind.ISSUE_EDITED,
        EventKind.ISSUE_DECLASSIFIED,
        EventKind.ISSUE_REOPENED,
        EventKind.ISSUE_CLOSED,
        EventKind.SPEC_PR_OPENED,
        EventKind.SPEC_PR_SYNCHRONIZED,
        EventKind.SPEC_PR_EDITED,
        EventKind.SPEC_PR_CLOSED,
        EventKind.SPEC_PR_MERGED,
        EventKind.CANDIDATE_PR_OPENED,
        EventKind.CANDIDATE_PR_SYNCHRONIZED,
        EventKind.CANDIDATE_PR_EDITED,
        EventKind.CANDIDATE_PR_CLOSED,
        EventKind.CANDIDATE_PR_MERGED,
        EventKind.DEPLOYMENT_WORKFLOW_OBSERVED,
    }
)


class HandoffError(RuntimeError):
    pass


class HandoffEventError(ValueError):
    pass


class HandoffApplyStatus(StrEnum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    SUPERSEDED = "superseded"
    CONFLICT = "conflict"
    INVALID = "invalid"


@dataclass(frozen=True)
class PayloadHash:
    record_id: str
    sha256: str

    def __post_init__(self) -> None:
        if re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}",
            self.record_id,
        ) is None:
            raise ValueError("handoff payload identity is invalid")
        if _SHA256.fullmatch(self.sha256) is None:
            raise ValueError("handoff payload hash is invalid")


@dataclass(frozen=True)
class StatePayloadHashes:
    snapshot_sha256: str
    journal_sha256: str
    inbox: tuple[PayloadHash, ...]
    outbox: tuple[PayloadHash, ...]
    objects: tuple[PayloadHash, ...]

    def __post_init__(self) -> None:
        if (
            _SHA256.fullmatch(self.snapshot_sha256) is None
            or _SHA256.fullmatch(self.journal_sha256) is None
        ):
            raise ValueError("state handoff payload hash is invalid")
        for values in (self.inbox, self.outbox, self.objects):
            if len({item.record_id for item in values}) != len(values):
                raise ValueError("state handoff payload identities repeat")

    def to_document(self) -> dict[str, object]:
        return {
            "inbox": [
                {"record_id": item.record_id, "sha256": item.sha256}
                for item in self.inbox
            ],
            "journal_sha256": self.journal_sha256,
            "objects": [
                {"record_id": item.record_id, "sha256": item.sha256}
                for item in self.objects
            ],
            "outbox": [
                {"record_id": item.record_id, "sha256": item.sha256}
                for item in self.outbox
            ],
            "snapshot_sha256": self.snapshot_sha256,
        }

    @classmethod
    def from_document(cls, value: Any) -> StatePayloadHashes:
        if not isinstance(value, dict) or set(value) != {
            "inbox",
            "journal_sha256",
            "objects",
            "outbox",
            "snapshot_sha256",
        }:
            raise ValueError("state handoff payload hashes are invalid")
        return cls(
            snapshot_sha256=_string(
                value["snapshot_sha256"],
                "snapshot hash",
            ),
            journal_sha256=_string(
                value["journal_sha256"],
                "journal hash",
            ),
            inbox=_payload_hashes(value["inbox"]),
            outbox=_payload_hashes(value["outbox"]),
            objects=_payload_hashes(value["objects"]),
        )


@dataclass(frozen=True)
class StewardStateHandoff:
    handoff_id: str
    issue_number: int
    generation: int
    expected_prior_revision: str | None
    proposed_revision: str
    proposed_tree: str
    source_inbox_revision: str
    event_ids: tuple[str, ...]
    product_version: str
    product_commit: str
    session_branch: str
    session_base_revision: str
    payload_hashes: StatePayloadHashes
    schema_version: int = 1
    kind: str = "steward_state"

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.kind != "steward_state":
            raise ValueError("steward handoff schema is invalid")
        if _SHA256.fullmatch(self.handoff_id) is None:
            raise ValueError("steward handoff ID is invalid")
        if (
            type(self.issue_number) is not int
            or self.issue_number < 1
            or type(self.generation) is not int
            or self.generation < 1
        ):
            raise ValueError("steward handoff issue or generation is invalid")
        if self.expected_prior_revision is not None:
            _require_commit(
                self.expected_prior_revision,
                "expected prior revision",
            )
        for value, description in (
            (self.proposed_revision, "proposed revision"),
            (self.proposed_tree, "proposed tree"),
            (self.source_inbox_revision, "source inbox revision"),
            (self.product_commit, "product commit"),
            (self.session_base_revision, "session base revision"),
        ):
            _require_commit(value, description)
        if _BRANCH.fullmatch(self.session_branch) is None:
            raise ValueError("steward handoff session branch is invalid")
        if (
            not isinstance(self.product_version, str)
            or not self.product_version
            or len(self.product_version) > 64
        ):
            raise ValueError("steward handoff product version is invalid")
        if len(set(self.event_ids)) != len(self.event_ids):
            raise ValueError("steward handoff event IDs repeat")
        for event_id in self.event_ids:
            if re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
                event_id,
            ) is None:
                raise ValueError("steward handoff event ID is invalid")
        if self.handoff_id != _document_sha256(self.payload_document):
            raise ValueError("steward handoff content address is invalid")

    @property
    def path(self) -> str:
        return (
            ".foundry-optimizer/handoffs/steward/"
            f"issue-{self.issue_number}/g{self.generation}/"
            f"{self.handoff_id}.json"
        )

    @property
    def payload_document(self) -> dict[str, object]:
        return {
            "event_ids": list(self.event_ids),
            "expected_prior_revision": self.expected_prior_revision,
            "generation": self.generation,
            "issue_number": self.issue_number,
            "kind": self.kind,
            "payload_hashes": self.payload_hashes.to_document(),
            "product_commit": self.product_commit,
            "product_version": self.product_version,
            "proposed_revision": self.proposed_revision,
            "proposed_tree": self.proposed_tree,
            "schema_version": self.schema_version,
            "session_base_revision": self.session_base_revision,
            "session_branch": self.session_branch,
            "source_inbox_revision": self.source_inbox_revision,
        }

    @property
    def content(self) -> bytes:
        return _canonical_json(
            {
                "handoff_id": self.handoff_id,
                "payload": self.payload_document,
            }
        )

    @classmethod
    def create(
        cls,
        *,
        proposal: StateRefProposal,
        source_inbox_revision: str,
        product_version: str,
        product_commit: str,
        session_branch: str,
        session_base_revision: str,
        payload_hashes: StatePayloadHashes,
    ) -> StewardStateHandoff:
        payload = {
            "event_ids": list(proposal.event_ids),
            "expected_prior_revision": proposal.expected_revision,
            "generation": proposal.snapshot.state.generation,
            "issue_number": proposal.issue_number,
            "kind": "steward_state",
            "payload_hashes": payload_hashes.to_document(),
            "product_commit": product_commit,
            "product_version": product_version,
            "proposed_revision": proposal.proposed_revision,
            "proposed_tree": proposal.proposed_tree,
            "schema_version": 1,
            "session_base_revision": session_base_revision,
            "session_branch": session_branch,
            "source_inbox_revision": source_inbox_revision,
        }
        return cls(
            handoff_id=_document_sha256(payload),
            issue_number=proposal.issue_number,
            generation=proposal.snapshot.state.generation,
            expected_prior_revision=proposal.expected_revision,
            proposed_revision=proposal.proposed_revision,
            proposed_tree=proposal.proposed_tree,
            source_inbox_revision=source_inbox_revision,
            event_ids=proposal.event_ids,
            product_version=product_version,
            product_commit=product_commit,
            session_branch=session_branch,
            session_base_revision=session_base_revision,
            payload_hashes=payload_hashes,
        )

    @classmethod
    def from_bytes(cls, content: bytes) -> StewardStateHandoff:
        if not isinstance(content, bytes) or len(content) > 1_000_000:
            raise ValueError("steward handoff content is invalid")
        try:
            document = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("steward handoff JSON is invalid") from error
        if (
            not isinstance(document, dict)
            or set(document) != {"handoff_id", "payload"}
            or not isinstance(document["payload"], dict)
        ):
            raise ValueError("steward handoff document is invalid")
        payload = document["payload"]
        expected = {
            "event_ids",
            "expected_prior_revision",
            "generation",
            "issue_number",
            "kind",
            "payload_hashes",
            "product_commit",
            "product_version",
            "proposed_revision",
            "proposed_tree",
            "schema_version",
            "session_base_revision",
            "session_branch",
            "source_inbox_revision",
        }
        if set(payload) != expected:
            raise ValueError("steward handoff fields are invalid")
        handoff = cls(
            handoff_id=_string(document["handoff_id"], "handoff ID"),
            issue_number=payload["issue_number"],
            generation=payload["generation"],
            expected_prior_revision=payload["expected_prior_revision"],
            proposed_revision=_string(
                payload["proposed_revision"],
                "proposed revision",
            ),
            proposed_tree=_string(
                payload["proposed_tree"],
                "proposed tree",
            ),
            source_inbox_revision=_string(
                payload["source_inbox_revision"],
                "source inbox revision",
            ),
            event_ids=_string_tuple(payload["event_ids"], "event IDs"),
            product_version=_string(
                payload["product_version"],
                "product version",
            ),
            product_commit=_string(
                payload["product_commit"],
                "product commit",
            ),
            session_branch=_string(
                payload["session_branch"],
                "session branch",
            ),
            session_base_revision=_string(
                payload["session_base_revision"],
                "session base revision",
            ),
            payload_hashes=StatePayloadHashes.from_document(
                payload["payload_hashes"]
            ),
            schema_version=payload["schema_version"],
            kind=payload["kind"],
        )
        if content != handoff.content:
            raise ValueError("steward handoff JSON must be canonical")
        reject_secret_content(content.decode("utf-8"))
        return handoff


@dataclass(frozen=True)
class CandidateDesignHandoff:
    handoff_id: str
    issue_number: int
    generation: int
    expected_prior_revision: str
    source_inbox_revision: str
    event_ids: tuple[str, ...]
    effect_id: str
    worker_issue_number: int
    proposed_ref: str
    proposed_revision: str
    proposed_tree: str
    changed_paths: tuple[str, ...]
    changed_payload_hashes: tuple[PayloadHash, ...]
    result: CandidateDesignResult
    result_sha256: str
    product_version: str
    product_commit: str
    session_branch: str
    session_base_revision: str
    schema_version: int = 1
    kind: str = "candidate_design"

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.kind != "candidate_design":
            raise ValueError("candidate design handoff schema is invalid")
        if _SHA256.fullmatch(self.handoff_id) is None:
            raise ValueError("candidate design handoff ID is invalid")
        if (
            type(self.issue_number) is not int
            or self.issue_number < 1
            or type(self.generation) is not int
            or self.generation < 1
            or type(self.worker_issue_number) is not int
            or self.worker_issue_number < 1
        ):
            raise ValueError("candidate design handoff identity is invalid")
        for value, description in (
            (self.expected_prior_revision, "expected prior revision"),
            (self.source_inbox_revision, "source inbox revision"),
            (self.proposed_revision, "proposed revision"),
            (self.proposed_tree, "proposed tree"),
            (self.product_commit, "product commit"),
            (self.session_base_revision, "session base revision"),
        ):
            _require_commit(value, description)
        if re.fullmatch(
            r"refs/heads/foundry-opt/design/"
            r"issue-[1-9][0-9]*/"
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
            self.proposed_ref,
        ) is None:
            raise ValueError("candidate design handoff ref is invalid")
        if _BRANCH.fullmatch(self.session_branch) is None:
            raise ValueError("candidate design session branch is invalid")
        if self.result.issue_number != self.issue_number or (
            self.result.generation != self.generation
            or self.result.effect_id != self.effect_id
        ):
            raise ValueError("candidate design result binding is invalid")
        _validate_candidate_handoff_privacy(self.result)
        if self.result_sha256 != _document_sha256(
            _candidate_result_document(self.result)
        ):
            raise ValueError("candidate design result hash is invalid")
        if (
            not self.changed_paths
            or tuple(sorted(set(self.changed_paths)))
            != tuple(sorted(self.changed_paths))
            or tuple(item.record_id for item in self.changed_payload_hashes)
            != self.changed_paths
        ):
            raise ValueError("candidate design changed paths are invalid")
        for path in self.changed_paths:
            if re.fullmatch(
                r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*",
                path,
            ) is None:
                raise ValueError("candidate design changed path is invalid")
        if self.handoff_id != _document_sha256(self.payload_document):
            raise ValueError("candidate design content address is invalid")

    @property
    def path(self) -> str:
        return (
            ".foundry-optimizer/handoffs/designer/"
            f"issue-{self.issue_number}/g{self.generation}/"
            f"{self.handoff_id}.json"
        )

    @property
    def payload_document(self) -> dict[str, object]:
        return {
            "changed_paths": list(self.changed_paths),
            "changed_payload_hashes": [
                {"record_id": item.record_id, "sha256": item.sha256}
                for item in self.changed_payload_hashes
            ],
            "effect_id": self.effect_id,
            "event_ids": list(self.event_ids),
            "expected_prior_revision": self.expected_prior_revision,
            "generation": self.generation,
            "issue_number": self.issue_number,
            "kind": self.kind,
            "product_commit": self.product_commit,
            "product_version": self.product_version,
            "proposed_ref": self.proposed_ref,
            "proposed_revision": self.proposed_revision,
            "proposed_tree": self.proposed_tree,
            "result": _candidate_result_document(self.result),
            "result_sha256": self.result_sha256,
            "schema_version": self.schema_version,
            "session_base_revision": self.session_base_revision,
            "session_branch": self.session_branch,
            "source_inbox_revision": self.source_inbox_revision,
            "worker_issue_number": self.worker_issue_number,
        }

    @property
    def content(self) -> bytes:
        return _canonical_json(
            {
                "handoff_id": self.handoff_id,
                "payload": self.payload_document,
            }
        )

    @classmethod
    def create(
        cls,
        *,
        snapshot: StateRefSnapshot,
        source_inbox_revision: str,
        request: CandidateDesignSubmissionRequest,
        result: CandidateDesignResult,
        artifact: CandidateDesignArtifact,
        product_version: str,
        product_commit: str,
        session_branch: str,
        session_base_revision: str,
        changed_payload_hashes: tuple[PayloadHash, ...],
    ) -> CandidateDesignHandoff:
        result_document = _candidate_result_document(result)
        payload = {
            "changed_paths": [
                path.as_posix() for path in artifact.changed_paths
            ],
            "changed_payload_hashes": [
                {"record_id": item.record_id, "sha256": item.sha256}
                for item in changed_payload_hashes
            ],
            "effect_id": request.effect_id,
            "event_ids": [event.event_id for event in snapshot.inbox],
            "expected_prior_revision": snapshot.revision,
            "generation": snapshot.state.generation,
            "issue_number": request.issue_number,
            "kind": "candidate_design",
            "product_commit": product_commit,
            "product_version": product_version,
            "proposed_ref": artifact.ref,
            "proposed_revision": artifact.head_commit,
            "proposed_tree": artifact.tree_sha,
            "result": result_document,
            "result_sha256": _document_sha256(result_document),
            "schema_version": 1,
            "session_base_revision": session_base_revision,
            "session_branch": session_branch,
            "source_inbox_revision": source_inbox_revision,
            "worker_issue_number": request.worker_issue_number,
        }
        return cls(
            handoff_id=_document_sha256(payload),
            issue_number=request.issue_number,
            generation=snapshot.state.generation,
            expected_prior_revision=snapshot.revision,
            source_inbox_revision=source_inbox_revision,
            event_ids=tuple(event.event_id for event in snapshot.inbox),
            effect_id=request.effect_id,
            worker_issue_number=request.worker_issue_number,
            proposed_ref=artifact.ref,
            proposed_revision=artifact.head_commit,
            proposed_tree=artifact.tree_sha,
            changed_paths=tuple(
                path.as_posix() for path in artifact.changed_paths
            ),
            changed_payload_hashes=changed_payload_hashes,
            result=result,
            result_sha256=_document_sha256(result_document),
            product_version=product_version,
            product_commit=product_commit,
            session_branch=session_branch,
            session_base_revision=session_base_revision,
        )

    @classmethod
    def from_bytes(cls, content: bytes) -> CandidateDesignHandoff:
        if not isinstance(content, bytes) or len(content) > 1_000_000:
            raise ValueError("candidate design handoff content is invalid")
        try:
            document = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(
                "candidate design handoff JSON is invalid"
            ) from error
        if (
            not isinstance(document, dict)
            or set(document) != {"handoff_id", "payload"}
            or not isinstance(document["payload"], dict)
        ):
            raise ValueError("candidate design handoff document is invalid")
        payload = document["payload"]
        expected = {
            "changed_paths",
            "changed_payload_hashes",
            "effect_id",
            "event_ids",
            "expected_prior_revision",
            "generation",
            "issue_number",
            "kind",
            "product_commit",
            "product_version",
            "proposed_ref",
            "proposed_revision",
            "proposed_tree",
            "result",
            "result_sha256",
            "schema_version",
            "session_base_revision",
            "session_branch",
            "source_inbox_revision",
            "worker_issue_number",
        }
        if set(payload) != expected:
            raise ValueError("candidate design handoff fields are invalid")
        result = _candidate_result_from_document(payload["result"])
        handoff = cls(
            handoff_id=_string(document["handoff_id"], "handoff ID"),
            issue_number=payload["issue_number"],
            generation=payload["generation"],
            expected_prior_revision=_string(
                payload["expected_prior_revision"],
                "expected prior revision",
            ),
            source_inbox_revision=_string(
                payload["source_inbox_revision"],
                "source inbox revision",
            ),
            event_ids=_string_tuple(payload["event_ids"], "event IDs"),
            effect_id=_string(payload["effect_id"], "effect ID"),
            worker_issue_number=payload["worker_issue_number"],
            proposed_ref=_string(payload["proposed_ref"], "proposed ref"),
            proposed_revision=_string(
                payload["proposed_revision"],
                "proposed revision",
            ),
            proposed_tree=_string(
                payload["proposed_tree"],
                "proposed tree",
            ),
            changed_paths=_string_tuple(
                payload["changed_paths"],
                "changed paths",
            ),
            changed_payload_hashes=_payload_hashes(
                payload["changed_payload_hashes"]
            ),
            result=result,
            result_sha256=_string(
                payload["result_sha256"],
                "result hash",
            ),
            product_version=_string(
                payload["product_version"],
                "product version",
            ),
            product_commit=_string(
                payload["product_commit"],
                "product commit",
            ),
            session_branch=_string(
                payload["session_branch"],
                "session branch",
            ),
            session_base_revision=_string(
                payload["session_base_revision"],
                "session base revision",
            ),
            schema_version=payload["schema_version"],
            kind=payload["kind"],
        )
        if content != handoff.content:
            raise ValueError(
                "candidate design handoff JSON must be canonical"
            )
        reject_secret_content(content.decode("utf-8"))
        return handoff


@dataclass(frozen=True)
class HandoffReceipt:
    handoff_id: str
    path: str
    head_commit: str


@dataclass(frozen=True)
class TrustedHandoffRequest:
    repository_root: Path
    repository: str
    repository_id: int
    pull_request_number: int
    author_login: str
    base_repository: str
    base_ref: str
    base_revision: str
    head_repository: str
    head_ref: str
    head_revision: str
    handoff_path: str
    handoff_blob: str

    def __post_init__(self) -> None:
        for value, description in (
            (self.repository, "repository"),
            (self.base_repository, "base repository"),
            (self.head_repository, "head repository"),
        ):
            if _REPOSITORY.fullmatch(value) is None:
                raise ValueError(f"{description} is invalid")
        if (
            self.repository != self.base_repository
            or self.repository != self.head_repository
        ):
            raise ValueError("handoff pull request must use the same repository")
        if (
            type(self.repository_id) is not int
            or self.repository_id < 1
            or type(self.pull_request_number) is not int
            or self.pull_request_number < 1
        ):
            raise ValueError("handoff pull request identity is invalid")
        if self.author_login not in {"Copilot", "copilot-swe-agent[bot]"}:
            raise ValueError("handoff pull request author is invalid")
        if not _safe_ref_name(self.base_ref):
            raise ValueError("handoff base branch is invalid")
        if _BRANCH.fullmatch(self.head_ref) is None:
            raise ValueError("handoff head branch is invalid")
        _require_commit(self.base_revision, "handoff base revision")
        _require_commit(self.head_revision, "handoff head revision")
        if re.fullmatch(r"[0-9a-f]{40}", self.handoff_blob) is None:
            raise ValueError("handoff blob is invalid")
        if re.fullmatch(
            r"\.foundry-optimizer/handoffs/(?:steward|designer)/"
            r"issue-[1-9][0-9]*/g[1-9][0-9]*/[0-9a-f]{64}\.json",
            self.handoff_path,
        ) is None:
            raise ValueError("handoff path is invalid")


@dataclass(frozen=True)
class TrustedHandoffContext:
    event_name: str
    repository: str
    repository_id: int
    default_branch: str

    def __post_init__(self) -> None:
        if self.event_name not in {
            "pull_request_target",
            "schedule",
            "workflow_dispatch",
        }:
            raise HandoffEventError("handoff event name is invalid")
        if _REPOSITORY.fullmatch(self.repository) is None:
            raise HandoffEventError("handoff repository is invalid")
        if type(self.repository_id) is not int or self.repository_id < 1:
            raise HandoffEventError("handoff repository ID is invalid")
        if not _safe_ref_name(self.default_branch):
            raise HandoffEventError("handoff default branch is invalid")


class HandoffPullRequestGateway(Protocol):
    def list_open_pull_requests(self) -> list[Mapping[str, Any]]: ...

    def get_pull_request(self, number: int) -> Mapping[str, Any]: ...

    def get_pull_request_files(
        self,
        number: int,
    ) -> list[Mapping[str, Any]]: ...

    def fetch_revision(self, revision: str) -> str: ...

    def head_was_pushed_by_copilot(
        self,
        branch: str,
        revision: str,
        repository_id: int,
    ) -> bool: ...


class GhHandoffPullRequestGateway:
    def __init__(
        self,
        commands: CommandRunner,
        repository_root: Path,
        repository: str,
    ) -> None:
        if _REPOSITORY.fullmatch(repository) is None:
            raise ValueError("handoff repository is invalid")
        self._commands = commands
        self._root = repository_root
        self._repository = repository

    def list_open_pull_requests(self) -> list[Mapping[str, Any]]:
        result: list[Mapping[str, Any]] = []
        for page in range(1, _DISCOVERY_MAX_PAGES + 1):
            value = self._json(
                (
                    "gh",
                    "api",
                    (
                        f"repos/{self._repository}/pulls"
                        "?state=open&sort=created&direction=asc"
                        f"&per_page={_DISCOVERY_PAGE_SIZE}&page={page}"
                    ),
                )
            )
            if (
                not isinstance(value, list)
                or len(value) > _DISCOVERY_PAGE_SIZE
                or any(not isinstance(item, Mapping) for item in value)
            ):
                raise HandoffEventError(
                    "GitHub pull request discovery response is invalid"
                )
            result.extend(value)
            if len(value) < _DISCOVERY_PAGE_SIZE:
                return result
        raise HandoffEventError(
            "GitHub pull request discovery exceeded its bounded pagination"
        )

    def get_pull_request(self, number: int) -> Mapping[str, Any]:
        _positive_number(number, "pull request number")
        value = self._json(
            (
                "gh",
                "api",
                f"repos/{self._repository}/pulls/{number}",
            )
        )
        if not isinstance(value, Mapping):
            raise HandoffEventError("GitHub pull request response is invalid")
        return value

    def get_pull_request_files(
        self,
        number: int,
    ) -> list[Mapping[str, Any]]:
        _positive_number(number, "pull request number")
        value = self._json(
            (
                "gh",
                "api",
                (
                    f"repos/{self._repository}/pulls/{number}/files"
                    "?per_page=2"
                ),
            )
        )
        if not isinstance(value, list):
            raise HandoffEventError("GitHub pull request files are invalid")
        result: list[Mapping[str, Any]] = []
        for item in value:
            if not isinstance(item, Mapping):
                raise HandoffEventError(
                    "GitHub pull request files are invalid"
                )
            result.append(item)
        return result

    def fetch_revision(self, revision: str) -> str:
        _require_commit(revision, "handoff revision")
        self._commands.run(
            (
                "git",
                "fetch",
                "--quiet",
                "--no-tags",
                "origin",
                revision,
            ),
            cwd=self._root,
        )
        fetched = self._commands.run(
            ("git", "rev-parse", "FETCH_HEAD^{commit}"),
            cwd=self._root,
        ).stdout.strip()
        _require_commit(fetched, "fetched handoff revision")
        return fetched

    def head_was_pushed_by_copilot(
        self,
        branch: str,
        revision: str,
        repository_id: int,
    ) -> bool:
        if _BRANCH.fullmatch(branch) is None:
            raise ValueError("handoff branch is invalid")
        _require_commit(revision, "handoff head revision")
        _positive_number(repository_id, "handoff repository ID")
        ref = f"refs/heads/{branch}"
        for page in range(1, _PUSH_EVENT_MAX_PAGES + 1):
            value = self._json(
                (
                    "gh",
                    "api",
                    (
                        f"repos/{self._repository}/events"
                        f"?per_page={_DISCOVERY_PAGE_SIZE}&page={page}"
                    ),
                )
            )
            if (
                not isinstance(value, list)
                or len(value) > _DISCOVERY_PAGE_SIZE
                or any(not isinstance(item, Mapping) for item in value)
            ):
                raise HandoffEventError(
                    "GitHub repository events response is invalid"
                )
            for item in value:
                payload = item.get("payload")
                if (
                    item.get("type") != "PushEvent"
                    or not isinstance(payload, Mapping)
                    or payload.get("head") != revision
                    or payload.get("ref") != ref
                    or payload.get("repository_id") != repository_id
                ):
                    continue
                actor = item.get("actor")
                return (
                    isinstance(actor, Mapping)
                    and actor.get("id") == _COPILOT_APP_USER_ID
                    and actor.get("login") == "Copilot"
                )
            if len(value) < _DISCOVERY_PAGE_SIZE:
                return False
        raise HandoffEventError(
            "GitHub repository event discovery exceeded its bound"
        )

    def close_internal_pull_request(
        self,
        number: int,
        *,
        handoff_id: str,
        kind: str,
    ) -> None:
        _positive_number(number, "pull request number")
        if _SHA256.fullmatch(handoff_id) is None or kind not in {
            "steward_state",
            "candidate_design",
        }:
            raise ValueError("handoff closure identity is invalid")
        label = (
            "steward state"
            if kind == "steward_state"
            else "candidate designer result"
        )
        body = (
            f"<!-- foundry-opt:internal-handoff:{handoff_id} -->\n\n"
            "This is an internal Foundry optimizer transport pull request, "
            "not a candidate or specification pull request. Its exact "
            f"{label} envelope was handled by the trusted base workflow."
        )
        self._write(
            "PATCH",
            f"repos/{self._repository}/pulls/{number}",
            {
                "body": body,
                "state": "closed",
                "title": f"[internal] Foundry {label} handoff",
            },
        )

    def delete_branch_if_head(
        self,
        branch: str,
        revision: str,
    ) -> bool:
        if _BRANCH.fullmatch(branch) is None:
            raise ValueError("handoff branch is invalid")
        _require_commit(revision, "handoff branch revision")
        ref = f"refs/heads/{branch}"
        output = self._commands.run(
            ("git", "ls-remote", "--heads", "origin", ref),
            cwd=self._root,
        ).stdout.strip()
        if not output:
            return True
        fields = output.split()
        if len(fields) != 2 or fields[1] != ref:
            raise HandoffEventError(
                "handoff branch metadata is invalid"
            )
        if fields[0] != revision:
            return False
        try:
            self._commands.run(
                (
                    "git",
                    "push",
                    f"--force-with-lease={ref}:{revision}",
                    "origin",
                    f":{ref}",
                ),
                cwd=self._root,
            )
        except CommandExitError:
            return not self._commands.run(
                ("git", "ls-remote", "--heads", "origin", ref),
                cwd=self._root,
            ).stdout.strip()
        return not self._commands.run(
            ("git", "ls-remote", "--heads", "origin", ref),
            cwd=self._root,
        ).stdout.strip()

    def _json(self, arguments: tuple[str, ...]) -> Any:
        result = self._commands.run(arguments, cwd=self._root)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise HandoffEventError(
                "GitHub handoff response is invalid"
            ) from error

    def _write(
        self,
        method: str,
        endpoint: str,
        body: Mapping[str, object] | None,
    ) -> None:
        arguments = [
            "gh",
            "api",
            "--method",
            method,
            endpoint,
        ]
        input_text = None
        if body is not None:
            arguments.extend(("--input", "-"))
            input_text = json.dumps(
                dict(body),
                separators=(",", ":"),
                sort_keys=True,
            )
        self._commands.run(
            tuple(arguments),
            cwd=self._root,
            input_text=input_text,
        )


def trusted_handoff_request_from_payload(
    payload: Mapping[str, Any],
    context: TrustedHandoffContext,
    repository_root: Path,
    gateway: HandoffPullRequestGateway,
) -> TrustedHandoffRequest:
    if context.event_name != "pull_request_target":
        raise HandoffEventError("handoff event mode is invalid")
    if payload.get("action") not in {"opened", "synchronize", "reopened"}:
        raise HandoffEventError("handoff event action is invalid")
    sender = payload.get("sender")
    if (
        not isinstance(sender, Mapping)
        or _copilot_author_login(sender) is None
    ):
        raise HandoffEventError("handoff event sender is invalid")
    repository = payload.get("repository")
    event_pull_request = payload.get("pull_request")
    if (
        not isinstance(repository, Mapping)
        or repository.get("full_name") != context.repository
        or repository.get("id") != context.repository_id
        or repository.get("default_branch") != context.default_branch
        or not isinstance(event_pull_request, Mapping)
    ):
        raise HandoffEventError("handoff event repository identity is invalid")
    event_identity = _pull_request_identity(
        event_pull_request,
        context,
    )
    live = gateway.get_pull_request(event_identity["number"])
    live_identity = _pull_request_identity(live, context)
    if any(
        live_identity[key] != event_identity[key]
        for key in (
            "number",
            "author_login",
            "base_repository",
            "base_ref",
            "base_revision",
            "head_repository",
            "head_ref",
        )
    ):
        raise HandoffEventError(
            "handoff pull request identity is not current"
        )
    if live_identity["head_revision"] != event_identity["head_revision"]:
        raise HandoffEventError(
            "handoff pull request head is not current"
        )
    return _trusted_handoff_request_from_live(
        live,
        context,
        repository_root,
        gateway,
        require_open=False,
    )


def discover_trusted_handoff_requests(
    context: TrustedHandoffContext,
    repository_root: Path,
    gateway: HandoffPullRequestGateway,
    *,
    requested_pull_request: int | None = None,
    limit: int = _DISCOVERY_LIMIT,
) -> tuple[TrustedHandoffRequest, ...]:
    if context.event_name not in {"schedule", "workflow_dispatch"}:
        raise HandoffEventError("handoff discovery event mode is invalid")
    if type(limit) is not int or limit < 1 or limit > _DISCOVERY_LIMIT:
        raise HandoffEventError("handoff discovery limit is invalid")
    if requested_pull_request is not None:
        if context.event_name != "workflow_dispatch":
            raise HandoffEventError(
                "handoff retry is only valid for workflow dispatch"
            )
        _positive_number(
            requested_pull_request,
            "requested pull request number",
        )
        return (
            _trusted_handoff_request_from_live(
                gateway.get_pull_request(requested_pull_request),
                context,
                repository_root,
                gateway,
                require_open=True,
            ),
        )

    candidates: dict[int, tuple[datetime, Mapping[str, Any]]] = {}
    for pull_request in gateway.list_open_pull_requests():
        try:
            identity = _pull_request_identity(
                pull_request,
                context,
                allow_open_summary=True,
            )
            created_at = _pull_request_created_at(pull_request)
        except HandoffEventError:
            continue
        if pull_request.get("state") != "open":
            continue
        number = identity["number"]
        current = candidates.get(number)
        if current is None or created_at < current[0]:
            candidates[number] = (created_at, pull_request)

    requests: list[TrustedHandoffRequest] = []
    for _, pull_request in sorted(
        candidates.values(),
        key=lambda item: (item[0], item[1]["number"]),
    ):
        number = pull_request["number"]
        try:
            request = _trusted_handoff_request_from_live(
                gateway.get_pull_request(number),
                context,
                repository_root,
                gateway,
                require_open=True,
            )
        except HandoffEventError:
            continue
        requests.append(request)
        if len(requests) == limit:
            break
    return tuple(requests)


def _trusted_handoff_request_from_live(
    live: Mapping[str, Any],
    context: TrustedHandoffContext,
    repository_root: Path,
    gateway: HandoffPullRequestGateway,
    *,
    require_open: bool,
) -> TrustedHandoffRequest:
    live_identity = _pull_request_identity(live, context)
    if require_open and live.get("state") != "open":
        raise HandoffEventError("handoff pull request is not open")
    if (
        context.event_name != "pull_request_target"
        and not gateway.head_was_pushed_by_copilot(
            live_identity["head_ref"],
            live_identity["head_revision"],
            context.repository_id,
        )
    ):
        raise HandoffEventError(
            "handoff head was not pushed by the Copilot App"
        )
    files = gateway.get_pull_request_files(live_identity["number"])
    if len(files) != 1:
        raise HandoffEventError(
            "handoff pull request must change exactly one file"
        )
    file = files[0]
    if not isinstance(file, Mapping):
        raise HandoffEventError("handoff pull request file is invalid")
    path = file.get("filename")
    blob = file.get("sha")
    if (
        not isinstance(path, str)
        or re.fullmatch(
            r"\.foundry-optimizer/handoffs/(?:steward|designer)/"
            r"issue-[1-9][0-9]*/g[1-9][0-9]*/"
            r"[0-9a-f]{64}\.json",
            path,
        )
        is None
        or not isinstance(blob, str)
        or re.fullmatch(r"[0-9a-f]{40}", blob) is None
        or file.get("status") not in {"added", "modified"}
        or file.get("previous_filename") is not None
    ):
        raise HandoffEventError("handoff pull request file is invalid")
    fetched_base = gateway.fetch_revision(live_identity["base_revision"])
    if fetched_base != live_identity["base_revision"]:
        raise HandoffEventError("handoff pull request fetch is not exact")
    fetched_head = gateway.fetch_revision(live_identity["head_revision"])
    if fetched_head != live_identity["head_revision"]:
        raise HandoffEventError("handoff pull request fetch is not exact")
    return TrustedHandoffRequest(
        repository_root=repository_root,
        repository=context.repository,
        repository_id=context.repository_id,
        pull_request_number=live_identity["number"],
        author_login=live_identity["author_login"],
        base_repository=live_identity["base_repository"],
        base_ref=live_identity["base_ref"],
        base_revision=live_identity["base_revision"],
        head_repository=live_identity["head_repository"],
        head_ref=live_identity["head_ref"],
        head_revision=live_identity["head_revision"],
        handoff_path=path,
        handoff_blob=blob,
    )


@dataclass(frozen=True)
class HandoffApplyResult:
    status: HandoffApplyStatus
    handoff_id: str | None = None
    snapshot: StateRefSnapshot | None = None
    code: str | None = None
    issue_number: int | None = None
    kind: str | None = None
    worker_issue_number: int | None = None


class HandoffFinalizer:
    def __init__(
        self,
        *,
        gateway: Any,
        assignments: Any,
        effects: Any,
        should_reassign: Any,
    ) -> None:
        self._gateway = gateway
        self._assignments = assignments
        self._effects = effects
        self._should_reassign = should_reassign

    def finalize(
        self,
        request: TrustedHandoffRequest,
        result: HandoffApplyResult,
    ) -> None:
        if (
            result.handoff_id is None
            or result.issue_number is None
            or result.kind not in {"steward_state", "candidate_design"}
        ):
            raise HandoffError("handoff finalization identity is unavailable")
        if result.status in {
            HandoffApplyStatus.APPLIED,
            HandoffApplyStatus.ALREADY_APPLIED,
        }:
            self._effects.reconcile(result.issue_number)
        source_issue = (
            result.worker_issue_number
            if result.kind == "candidate_design"
            and result.worker_issue_number is not None
            else result.issue_number
        )
        self._assignments.release(source_issue)
        if self._should_reassign(result.issue_number):
            if source_issue != result.issue_number:
                self._assignments.release(result.issue_number)
            self._assignments.assign(
                result.issue_number,
                f"handoff-{result.handoff_id}",
            )
        if not self._gateway.delete_branch_if_head(
            request.head_ref,
            request.head_revision,
        ):
            raise HandoffError(
                "handoff branch advanced before finalization"
            )
        self._gateway.close_internal_pull_request(
            request.pull_request_number,
            handoff_id=result.handoff_id,
            kind=result.kind,
        )


class HandoffApplyService:
    def __init__(
        self,
        *,
        ledger: GitStateRef | None = None,
    ) -> None:
        self._ledger = ledger or GitStateRef()

    def apply(
        self,
        request: TrustedHandoffRequest,
    ) -> HandoffApplyResult:
        try:
            handoff = self._validate(request)
        except (HandoffError, StateRefError, ValueError):
            return HandoffApplyResult(
                HandoffApplyStatus.INVALID,
                code="handoff_validation_failed",
            )
        if isinstance(handoff, CandidateDesignHandoff):
            result = self._apply_candidate(request, handoff)
            return replace(
                result,
                issue_number=handoff.issue_number,
                kind=handoff.kind,
                worker_issue_number=handoff.worker_issue_number,
            )
        result = self._apply_state(request, handoff)
        return replace(
            result,
            issue_number=handoff.issue_number,
            kind=handoff.kind,
        )

    def _apply_state(
        self,
        request: TrustedHandoffRequest,
        handoff: StewardStateHandoff,
    ) -> HandoffApplyResult:
        current = self._ledger.load(
            request.repository_root,
            handoff.issue_number,
        )
        if (
            current is not None
            and current.revision == handoff.proposed_revision
        ):
            return HandoffApplyResult(
                HandoffApplyStatus.ALREADY_APPLIED,
                handoff.handoff_id,
                current,
            )
        if (
            (current.revision if current is not None else None)
            != handoff.expected_prior_revision
        ):
            return HandoffApplyResult(
                HandoffApplyStatus.CONFLICT,
                handoff.handoff_id,
                current,
                "state_ref_conflict",
            )
        try:
            snapshot = self._ledger.publish_revision(
                request.repository_root,
                issue_number=handoff.issue_number,
                expected_revision=handoff.expected_prior_revision,
                proposed_revision=handoff.proposed_revision,
            )
        except StateRefConflictError:
            return HandoffApplyResult(
                HandoffApplyStatus.CONFLICT,
                handoff.handoff_id,
                current,
                "state_ref_conflict",
            )
        except StateRefError:
            return HandoffApplyResult(
                HandoffApplyStatus.INVALID,
                handoff.handoff_id,
                current,
                "state_ref_publish_failed",
            )
        return HandoffApplyResult(
            HandoffApplyStatus.APPLIED,
            handoff.handoff_id,
            snapshot,
        )

    def _apply_candidate(
        self,
        request: TrustedHandoffRequest,
        handoff: CandidateDesignHandoff,
    ) -> HandoffApplyResult:
        from foundry_opt.orchestration.candidate_workers import (
            CandidateDesignArtifact,
            _candidate_design_submission_record,
            _submitted_design_matches,
        )

        current = self._ledger.load(
            request.repository_root,
            handoff.issue_number,
        )
        if current is None:
            return HandoffApplyResult(
                HandoffApplyStatus.CONFLICT,
                handoff.handoff_id,
                code="state_ref_conflict",
            )
        record_id = f"{handoff.effect_id}-submitted"
        existing = tuple(
            record
            for record in current.outbox
            if record.record_id == record_id
        )
        artifact = CandidateDesignArtifact(
            ref=handoff.proposed_ref,
            head_commit=handoff.proposed_revision,
            tree_sha=handoff.proposed_tree,
            changed_paths=tuple(Path(path) for path in handoff.changed_paths),
        )
        if existing:
            if (
                len(existing) == 1
                and _submitted_design_matches(
                    existing[0],
                    handoff.result,
                    handoff.worker_issue_number,
                )
                and existing[0].payload.get("ref") == artifact.ref
                and existing[0].payload.get("head_commit")
                == artifact.head_commit
                and existing[0].payload.get("tree_sha")
                == artifact.tree_sha
                and tuple(existing[0].payload.get("changed_paths", ()))
                == handoff.changed_paths
            ):
                return HandoffApplyResult(
                    HandoffApplyStatus.ALREADY_APPLIED,
                    handoff.handoff_id,
                    current,
                )
            return HandoffApplyResult(
                HandoffApplyStatus.CONFLICT,
                handoff.handoff_id,
                current,
                "candidate_design_result_conflict",
            )
        if current.revision != handoff.expected_prior_revision:
            return HandoffApplyResult(
                HandoffApplyStatus.CONFLICT,
                handoff.handoff_id,
                current,
                "state_ref_conflict",
            )
        remote_design = _remote_revision(
            request.repository_root,
            "origin",
            handoff.proposed_ref,
        )
        if remote_design not in {None, handoff.proposed_revision}:
            return HandoffApplyResult(
                HandoffApplyStatus.CONFLICT,
                handoff.handoff_id,
                current,
                "candidate_design_ref_conflict",
            )
        if remote_design is None:
            lease = f"--force-with-lease={handoff.proposed_ref}:"
            pushed = _run(
                request.repository_root,
                "git",
                "push",
                lease,
                "origin",
                (
                    f"{handoff.proposed_revision}:"
                    f"{handoff.proposed_ref}"
                ),
                check=False,
            )
            if pushed.returncode != 0 or _remote_revision(
                request.repository_root,
                "origin",
                handoff.proposed_ref,
            ) != handoff.proposed_revision:
                return HandoffApplyResult(
                    HandoffApplyStatus.CONFLICT,
                    handoff.handoff_id,
                    current,
                    "candidate_design_ref_publish_failed",
                )
        submitted = _candidate_design_submission_record(
            current,
            record_id,
            handoff.result,
            artifact,
            handoff.worker_issue_number,
        )
        try:
            persisted = self._ledger.commit(
                request.repository_root,
                issue_number=handoff.issue_number,
                expected_revision=current.revision,
                state=current.state,
                outbox=(submitted,),
            )
        except StateRefConflictError:
            return HandoffApplyResult(
                HandoffApplyStatus.CONFLICT,
                handoff.handoff_id,
                current,
                "state_ref_conflict",
            )
        except StateRefError:
            return HandoffApplyResult(
                HandoffApplyStatus.INVALID,
                handoff.handoff_id,
                current,
                "candidate_design_state_publish_failed",
            )
        return HandoffApplyResult(
            HandoffApplyStatus.APPLIED,
            handoff.handoff_id,
            persisted,
        )

    def _validate(
        self,
        request: TrustedHandoffRequest,
    ) -> StewardStateHandoff | CandidateDesignHandoff:
        root = request.repository_root.expanduser().resolve()
        if _run(
            root,
            "git",
            "cat-file",
            "-e",
            f"{request.head_revision}^{{commit}}",
            check=False,
        ).returncode != 0:
            raise HandoffError("handoff head commit is unavailable")
        parents = _git_text(
            root,
            "rev-list",
            "--parents",
            "-n",
            "1",
            request.head_revision,
        ).split()
        if len(parents) != 3:
            raise HandoffError("handoff commit parents are invalid")
        session_parent, proposed_revision = parents[1:]
        base_revisions = _git_text(
            root,
            "merge-base",
            "--all",
            request.base_revision,
            session_parent,
        ).splitlines()
        if len(base_revisions) != 1:
            raise HandoffError("handoff session parent is not based on base")
        effective_base_revision = base_revisions[0]
        _require_commit(
            effective_base_revision,
            "handoff effective base revision",
        )
        first_parent_paths = tuple(
            line
            for line in _git_text(
                root,
                "diff",
                "--name-only",
                session_parent,
                request.head_revision,
            ).splitlines()
            if line
        )
        base_paths = tuple(
            line
            for line in _git_text(
                root,
                "diff",
                "--name-only",
                effective_base_revision,
                request.head_revision,
            ).splitlines()
            if line
        )
        if (
            first_parent_paths != (request.handoff_path,)
            or base_paths != (request.handoff_path,)
        ):
            raise HandoffError("handoff pull request changed other paths")
        tree_line = _git_text(
            root,
            "ls-tree",
            request.head_revision,
            "--",
            request.handoff_path,
        ).split()
        if (
            len(tree_line) != 4
            or tree_line[1] != "blob"
            or tree_line[2] != request.handoff_blob
            or tree_line[3] != request.handoff_path
        ):
            raise HandoffError("handoff blob binding is invalid")
        content = _run(
            root,
            "git",
            "show",
            f"{request.head_revision}:{request.handoff_path}",
        ).stdout
        if request.handoff_path.startswith(
            ".foundry-optimizer/handoffs/steward/"
        ):
            handoff: StewardStateHandoff | CandidateDesignHandoff = (
                StewardStateHandoff.from_bytes(content)
            )
            label = "steward"
        else:
            handoff = CandidateDesignHandoff.from_bytes(content)
            label = "designer"
        if (
            handoff.path != request.handoff_path
            or handoff.session_branch != request.head_ref
            or handoff.session_base_revision != session_parent
            or handoff.proposed_revision != proposed_revision
        ):
            raise HandoffError("handoff session binding is invalid")
        author = _git_text(
            root,
            "show",
            "-s",
            "--format=%an%x00%ae%x00%cn%x00%ce",
            request.head_revision,
        ).split("\x00")
        if author != [
            "Foundry Optimizer Handoff",
            "foundry-opt@example.invalid",
            "Foundry Optimizer Handoff",
            "foundry-opt@example.invalid",
        ]:
            raise HandoffError("handoff commit identity is invalid")
        expected_message = (
            f"Foundry internal {label} handoff issue-{handoff.issue_number}\n\n"
            f"Foundry-Handoff-ID: {handoff.handoff_id}\n"
            f"Foundry-Handoff-Path: {handoff.path}\n"
            f"Foundry-Handoff-Blob: {request.handoff_blob}"
        )
        if (
            _git_text(
                root,
                "show",
                "-s",
                "--format=%B",
                request.head_revision,
            )
            != expected_message
        ):
            raise HandoffError("handoff marker is invalid")
        version, product_commit = _product_identity()
        if (
            handoff.product_version != version
            or handoff.product_commit
            not in _trusted_handoff_product_commits(product_commit)
        ):
            raise HandoffError("handoff product identity is invalid")
        if isinstance(handoff, CandidateDesignHandoff):
            self._validate_candidate(root, handoff, proposed_revision)
            return handoff
        self._validate_state(root, handoff)
        return handoff

    def _validate_state(
        self,
        root: Path,
        handoff: StewardStateHandoff,
    ) -> None:
        expected_parents = (
            []
            if handoff.expected_prior_revision is None
            else [handoff.expected_prior_revision]
        )
        if _git_text(
            root,
            "rev-list",
            "--parents",
            "-n",
            "1",
            handoff.proposed_revision,
        ).split() != [handoff.proposed_revision, *expected_parents]:
            raise HandoffError("handoff state parent is invalid")
        proposal = self._ledger.inspect_revision(
            root,
            handoff.issue_number,
            handoff.proposed_revision,
        )
        if (
            _git_text(
                root,
                "rev-parse",
                f"{handoff.proposed_revision}^{{tree}}",
            )
            != handoff.proposed_tree
            or _state_payload_hashes(
                root,
                handoff.proposed_revision,
            )
            != handoff.payload_hashes
        ):
            raise HandoffError("handoff state payload binding is invalid")
        trusted_events = _trusted_inbox_events(
            root,
            handoff.issue_number,
            handoff.source_inbox_revision,
        )
        proposed_events = {
            event.event_id: event for event in proposal.inbox
        }
        prior_count = 0
        if handoff.expected_prior_revision is not None:
            prior = self._ledger.inspect_revision(
                root,
                handoff.issue_number,
                handoff.expected_prior_revision,
            )
            if proposal.inbox[: len(prior.inbox)] != prior.inbox:
                raise HandoffError("handoff inbox history changed")
            prior_count = len(prior.inbox)
        if tuple(
            event.event_id for event in proposal.inbox[prior_count:]
        ) != handoff.event_ids or not _events_match_trust(
            proposed_events,
            handoff.event_ids,
            trusted_events,
        ):
            raise HandoffError("handoff events are not trusted")

    def _validate_candidate(
        self,
        root: Path,
        handoff: CandidateDesignHandoff,
        proposed_revision: str,
    ) -> None:
        from foundry_opt.orchestration.candidate_workers import (
            _candidate_design_intent,
        )

        if proposed_revision != handoff.proposed_revision:
            raise HandoffError("candidate design proposal binding is invalid")
        parent_line = _git_text(
            root,
            "rev-list",
            "--parents",
            "-n",
            "1",
            handoff.proposed_revision,
        ).split()
        if parent_line != [
            handoff.proposed_revision,
            handoff.result.base_commit,
        ]:
            raise HandoffError("candidate design parent is invalid")
        if _git_text(
            root,
            "rev-parse",
            f"{handoff.proposed_revision}^{{tree}}",
        ) != handoff.proposed_tree:
            raise HandoffError("candidate design tree binding is invalid")
        changed = tuple(
            line
            for line in _git_text(
                root,
                "diff",
                "--name-only",
                handoff.result.base_commit,
                handoff.proposed_revision,
            ).splitlines()
            if line
        )
        if changed != handoff.changed_paths:
            raise HandoffError("candidate design changed paths differ")
        hashes = tuple(
            PayloadHash(
                path,
                hashlib.sha256(
                    _run(
                        root,
                        "git",
                        "show",
                        f"{handoff.proposed_revision}:{path}",
                    ).stdout
                ).hexdigest(),
            )
            for path in changed
        )
        if hashes != handoff.changed_payload_hashes:
            raise HandoffError("candidate design payload hashes differ")
        snapshot = self._ledger.load(root, handoff.issue_number)
        if snapshot is None:
            raise HandoffError("candidate design state is unavailable")
        planned = tuple(
            record
            for record in snapshot.outbox
            if (
                record.kind == "specialist_work_request"
                and record.generation == snapshot.state.generation
                and record.payload.get("effect_id") == handoff.effect_id
                and record.payload.get("issue_number")
                == handoff.issue_number
                and record.payload.get("specialist")
                == "foundry-candidate-designer"
                and record.payload.get("work_kind") == "design_candidate"
            )
        )
        if len(planned) != 1:
            raise HandoffError("candidate design intent is unavailable")
        intent = _candidate_design_intent(root, planned[0])
        handoff.result.require_matches(intent)
        if any(
            not _path_is_allowed(Path(path), intent.edit_paths)
            for path in handoff.changed_paths
        ):
            raise HandoffError("candidate design path is not allowed")
        assignments = tuple(
            record
            for record in snapshot.outbox
            if (
                record.kind == "specialist_work_succeeded"
                and record.generation == snapshot.state.generation
                and record.payload.get("effect_id")
                == planned[0].record_id
                and record.payload.get("specialist")
                == "foundry-candidate-designer"
                and record.payload.get("work_kind") == "design_candidate"
            )
        )
        if (
            len(assignments) != 1
            or assignments[0].payload.get("worker_issue_number")
            != handoff.worker_issue_number
        ):
            raise HandoffError("candidate design assignment is invalid")
        trusted_events = _trusted_inbox_events(
            root,
            handoff.issue_number,
            handoff.source_inbox_revision,
        )
        snapshot_events = {
            event.event_id: event for event in snapshot.inbox
        }
        if tuple(snapshot_events) != handoff.event_ids or not (
            _events_match_trust(
                snapshot_events,
                handoff.event_ids,
                trusted_events,
            )
        ):
            raise HandoffError("candidate design events are not trusted")


@dataclass(frozen=True)
class _CloudSession:
    branch: str
    base_revision: str
    remote_url: str


class CloudHandoffStore:
    def __init__(self, *, remote: str = "origin") -> None:
        if re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
            remote,
        ) is None:
            raise ValueError("handoff remote is invalid")
        self._remote = remote

    def persist_state(
        self,
        repository_root: Path,
        error: StateRefPushUnacknowledgedError,
    ) -> HandoffReceipt:
        proposal = error.proposal
        if proposal is None:
            raise HandoffError("state proposal metadata is unavailable")
        root = repository_root.expanduser().resolve()
        if (
            verified_copilot_git_proxy_session(root, self._remote)
            is None
        ):
            raise StateRefConflictError(
                "verified Copilot git proxy is unavailable"
            )
        session = _cloud_session(root, self._remote)
        existing = _existing_handoff_content(root, session.base_revision)
        if existing is not None:
            path, content = existing
            handoff = StewardStateHandoff.from_bytes(content)
            if (
                handoff.path != path
                or handoff.proposed_revision
                != proposal.proposed_revision
                or handoff.expected_prior_revision
                != proposal.expected_revision
                or handoff.event_ids != proposal.event_ids
                or handoff.session_branch != session.branch
                or _cloud_remote_revision(
                    root,
                    session,
                    f"refs/heads/{session.branch}",
                )
                != session.base_revision
            ):
                raise HandoffError(
                    "existing steward handoff does not match proposal"
                )
            return HandoffReceipt(
                handoff.handoff_id,
                handoff.path,
                session.base_revision,
            )
        source_inbox_revision = _cloud_remote_revision(
            root,
            session,
            (
                "refs/heads/foundry-opt/inbox/"
                f"issue-{proposal.issue_number}"
            ),
        )
        if source_inbox_revision is None:
            raise HandoffError("trusted source inbox is unavailable")
        version, product_commit = _product_identity()
        handoff = StewardStateHandoff.create(
            proposal=proposal,
            source_inbox_revision=source_inbox_revision,
            product_version=version,
            product_commit=product_commit,
            session_branch=session.branch,
            session_base_revision=session.base_revision,
            payload_hashes=_state_payload_hashes(
                root,
                proposal.proposed_revision,
            ),
        )
        return self._commit_handoff(root, session, handoff)

    def persist_candidate_design(
        self,
        repository_root: Path,
        *,
        snapshot: StateRefSnapshot,
        request: CandidateDesignSubmissionRequest,
        intent: CandidateDesignIntent,
        result: CandidateDesignResult,
        artifact: CandidateDesignArtifact,
    ) -> HandoffReceipt:
        result.require_matches(intent)
        root = repository_root.expanduser().resolve()
        session = _cloud_session(root, self._remote)
        existing = _existing_handoff_content(root, session.base_revision)
        if existing is not None:
            path, content = existing
            handoff = CandidateDesignHandoff.from_bytes(content)
            if (
                handoff.path != path
                or handoff.expected_prior_revision != snapshot.revision
                or handoff.proposed_revision != artifact.head_commit
                or handoff.effect_id != request.effect_id
                or handoff.worker_issue_number
                != request.worker_issue_number
                or handoff.result != result
                or handoff.session_branch != session.branch
                or _cloud_remote_revision(
                    root,
                    session,
                    f"refs/heads/{session.branch}",
                )
                != session.base_revision
            ):
                raise HandoffError(
                    "existing designer handoff does not match proposal"
                )
            return HandoffReceipt(
                handoff.handoff_id,
                handoff.path,
                session.base_revision,
            )
        source_inbox_revision = _cloud_remote_revision(
            root,
            session,
            (
                "refs/heads/foundry-opt/inbox/"
                f"issue-{request.issue_number}"
            ),
        )
        if source_inbox_revision is None:
            raise HandoffError("trusted source inbox is unavailable")
        version, product_commit = _product_identity()
        changed_hashes = tuple(
            PayloadHash(
                path.as_posix(),
                hashlib.sha256(
                    _run(
                        root,
                        "git",
                        "show",
                        f"{artifact.head_commit}:{path.as_posix()}",
                    ).stdout
                ).hexdigest(),
            )
            for path in artifact.changed_paths
        )
        handoff = CandidateDesignHandoff.create(
            snapshot=snapshot,
            source_inbox_revision=source_inbox_revision,
            request=request,
            result=result,
            artifact=artifact,
            product_version=version,
            product_commit=product_commit,
            session_branch=session.branch,
            session_base_revision=session.base_revision,
            changed_payload_hashes=changed_hashes,
        )
        return self._commit_envelope(
            root,
            session,
            handoff_id=handoff.handoff_id,
            path=handoff.path,
            content=handoff.content,
            proposed_revision=handoff.proposed_revision,
            issue_number=handoff.issue_number,
            label="designer",
        )

    def _commit_handoff(
        self,
        root: Path,
        session: _CloudSession,
        handoff: StewardStateHandoff,
    ) -> HandoffReceipt:
        return self._commit_envelope(
            root,
            session,
            handoff_id=handoff.handoff_id,
            path=handoff.path,
            content=handoff.content,
            proposed_revision=handoff.proposed_revision,
            issue_number=handoff.issue_number,
            label="steward",
        )

    def _commit_envelope(
        self,
        root: Path,
        session: _CloudSession,
        *,
        handoff_id: str,
        path: str,
        content: bytes,
        proposed_revision: str,
        issue_number: int,
        label: str,
    ) -> HandoffReceipt:
        existing_paths = tuple(
            line
            for line in _git_text(
                root,
                "ls-tree",
                "-r",
                "--name-only",
                session.base_revision,
                "--",
                ".foundry-optimizer/handoffs",
            ).splitlines()
            if line
        )
        if existing_paths:
            if existing_paths != (path,) or _run(
                root,
                "git",
                "show",
                f"{session.base_revision}:{path}",
            ).stdout != content:
                raise HandoffError(
                    "cloud session already contains another handoff"
                )
            parents = _git_text(
                root,
                "rev-list",
                "--parents",
                "-n",
                "1",
                session.base_revision,
            ).split()
            if (
                len(parents) != 3
                or parents[2] != proposed_revision
            ):
                raise HandoffError(
                    "existing cloud handoff ancestry is invalid"
                )
            branch_ref = f"refs/heads/{session.branch}"
            remote = _cloud_remote_revision(root, session, branch_ref)
            if remote != session.base_revision:
                raise HandoffError(
                    "existing cloud handoff is not published"
                )
            return HandoffReceipt(
                handoff_id,
                path,
                session.base_revision,
            )
        status = _git_text(root, "status", "--porcelain=v1", "-uall")
        if status:
            raise HandoffError(
                "cloud handoff requires a clean session checkout"
            )
        artifact_path = root / Path(path)
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_bytes(content)
        git_dir = Path(_git_text(root, "rev-parse", "--git-dir"))
        if not git_dir.is_absolute():
            git_dir = root / git_dir
        index = git_dir / f"foundry-handoff-{os.getpid()}-{uuid4().hex}"
        environment = {"GIT_INDEX_FILE": str(index)}
        try:
            _run(
                root,
                "git",
                "read-tree",
                session.base_revision,
                environment=environment,
            )
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
            message = (
                f"Foundry internal {label} handoff issue-{issue_number}\n\n"
                f"Foundry-Handoff-ID: {handoff_id}\n"
                f"Foundry-Handoff-Path: {path}\n"
                f"Foundry-Handoff-Blob: {blob}\n"
            )
            commit = _run(
                root,
                "git",
                "commit-tree",
                tree,
                "-p",
                session.base_revision,
                "-p",
                proposed_revision,
                environment={
                    **environment,
                    **_HANDOFF_COMMIT_ENVIRONMENT,
                },
                input_bytes=message.encode("utf-8"),
            ).stdout.decode("ascii").strip()
        finally:
            index.unlink(missing_ok=True)
            Path(f"{index}.lock").unlink(missing_ok=True)
        branch_ref = f"refs/heads/{session.branch}"
        try:
            remote_before = isolated_remote_revision(
                root,
                session.remote_url,
                branch_ref,
            )
        except GitTransportError as error:
            raise HandoffError(
                "cloud session branch query failed"
            ) from error
        if remote_before not in {None, session.base_revision}:
            artifact_path.unlink(missing_ok=True)
            raise HandoffError(
                "cloud session branch changed concurrently"
            )
        try:
            local_before = _local_revision(root, branch_ref)
        except HandoffError:
            artifact_path.unlink(missing_ok=True)
            raise
        if local_before not in {None, session.base_revision}:
            artifact_path.unlink(missing_ok=True)
            raise HandoffError(
                "local cloud session branch changed concurrently"
            )
        try:
            _run(
                root,
                "git",
                "update-ref",
                branch_ref,
                commit,
                local_before or _ZERO_COMMIT,
            )
        except HandoffError:
            artifact_path.unlink(missing_ok=True)
            raise
        _run(root, "git", "reset", "--mixed", "--quiet", commit)
        try:
            pushed = isolated_compare_and_swap_push(
                root,
                session.remote_url,
                source_revision=commit,
                destination_ref=branch_ref,
                expected_revision=remote_before,
            )
        except GitTransportError as error:
            raise HandoffError(
                "cloud session handoff transport failed"
            ) from error
        if (
            pushed.before != remote_before
            or pushed.returncode != 0
        ):
            raise HandoffError("cloud session handoff push failed")
        if pushed.after != commit:
            raise HandoffError(
                "cloud session handoff push was not acknowledged"
            )
        if _git_text(root, "status", "--porcelain=v1", "-uall"):
            raise HandoffError("cloud handoff left session changes behind")
        return HandoffReceipt(handoff_id, path, commit)


def _cloud_session(root: Path, remote: str) -> _CloudSession:
    verified = verified_copilot_git_proxy_session(root, remote)
    if verified is None:
        raise HandoffError("verified Copilot git proxy is unavailable")
    if _BRANCH.fullmatch(verified.branch) is None:
        raise HandoffError("native Copilot session branch is invalid")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if _REPOSITORY.fullmatch(repository) is None:
        raise HandoffError("Copilot cloud repository marker is unavailable")
    return _CloudSession(
        branch=verified.branch,
        base_revision=verified.head_revision,
        remote_url=verified.remote_url,
    )


def _cloud_remote_revision(
    root: Path,
    session: _CloudSession,
    ref: str,
) -> str | None:
    try:
        return isolated_remote_revision(root, session.remote_url, ref)
    except GitTransportError as error:
        raise HandoffError(
            "isolated cloud handoff remote query failed"
        ) from error


def _local_revision(root: Path, ref: str) -> str | None:
    resolved = _run(
        root,
        "git",
        "rev-parse",
        "--verify",
        "--quiet",
        f"{ref}^{{commit}}",
        check=False,
    )
    if resolved.returncode == 1:
        return None
    if resolved.returncode != 0:
        raise HandoffError("local cloud session branch query failed")
    revision = resolved.stdout.decode("ascii").strip()
    if _COMMIT.fullmatch(revision) is None:
        raise HandoffError("local cloud session branch is invalid")
    return revision


def _existing_handoff_content(
    root: Path,
    revision: str,
) -> tuple[str, bytes] | None:
    paths = tuple(
        line
        for line in _git_text(
            root,
            "ls-tree",
            "-r",
            "--name-only",
            revision,
            "--",
            ".foundry-optimizer/handoffs",
        ).splitlines()
        if line
    )
    if not paths:
        return None
    if len(paths) != 1:
        raise HandoffError("cloud session contains multiple handoffs")
    path = paths[0]
    return (
        path,
        _run(root, "git", "show", f"{revision}:{path}").stdout,
    )


def _state_payload_hashes(
    root: Path,
    revision: str,
) -> StatePayloadHashes:
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
    required = {"journal.jsonl", "snapshot.json"}
    if not required <= set(paths):
        raise HandoffError("proposed state tree is incomplete")

    def digest(path: str) -> str:
        return hashlib.sha256(
            _run(root, "git", "show", f"{revision}:{path}").stdout
        ).hexdigest()

    return StatePayloadHashes(
        snapshot_sha256=digest("snapshot.json"),
        journal_sha256=digest("journal.jsonl"),
        inbox=tuple(
            PayloadHash(
                path.removeprefix("inbox/").removesuffix(".json"),
                digest(path),
            )
            for path in paths
            if path.startswith("inbox/")
        ),
        outbox=tuple(
            PayloadHash(
                path.removeprefix("outbox/").removesuffix(".json"),
                digest(path),
            )
            for path in paths
            if path.startswith("outbox/")
        ),
        objects=tuple(
            PayloadHash(path, digest(path))
            for path in paths
            if path.startswith("objects/")
        ),
    )


def _product_identity() -> tuple[str, str]:
    commit: str | None = None
    try:
        direct_url = metadata.distribution(
            "foundry-cloud-coding-agent"
        ).read_text("direct_url.json")
        document = json.loads(direct_url) if direct_url else {}
        vcs = document.get("vcs_info")
        if isinstance(vcs, dict):
            candidate = vcs.get("commit_id")
            if isinstance(candidate, str) and _COMMIT.fullmatch(candidate):
                commit = candidate
    except (json.JSONDecodeError, metadata.PackageNotFoundError):
        pass
    if commit is None:
        source = Path(__file__).resolve()
        result = subprocess.run(
            ("git", "rev-parse", "HEAD^{commit}"),
            cwd=source.parent,
            check=False,
            capture_output=True,
            text=True,
        )
        candidate = result.stdout.strip()
        if result.returncode == 0 and _COMMIT.fullmatch(candidate):
            commit = candidate
    if commit is None:
        raise HandoffError("installed product commit is unavailable")
    return __version__, commit


def _trusted_handoff_product_commits(
    installed_commit: str,
) -> frozenset[str]:
    _require_commit(installed_commit, "installed product commit")
    raw = os.environ.get("TRUSTED_HANDOFF_PRODUCT_COMMITS", "")
    if not raw:
        return frozenset((installed_commit,))
    values = raw.split(",")
    if (
        len(values) > 8
        or any(_COMMIT.fullmatch(value) is None for value in values)
    ):
        raise HandoffError(
            "trusted handoff product commit allowlist is invalid"
        )
    return frozenset((installed_commit, *values))


def _remote_revision(
    root: Path,
    remote: str,
    ref: str,
) -> str | None:
    output = _git_text(root, "ls-remote", "--heads", remote, ref)
    if not output:
        return None
    fields = output.split()
    if len(fields) != 2 or fields[1] != ref:
        raise HandoffError("remote handoff ref metadata is invalid")
    _require_commit(fields[0], "remote handoff revision")
    return fields[0]


def _trusted_inbox_events(
    root: Path,
    issue_number: int,
    source_revision: str,
) -> dict[str, Any]:
    _require_commit(source_revision, "source inbox revision")
    inbox = GitIssueEventInbox(root)
    events = inbox.events(issue_number)
    current = _remote_revision(
        root,
        "origin",
        (
            "refs/heads/foundry-opt/inbox/"
            f"issue-{issue_number}"
        ),
    )
    if current is None or _run(
        root,
        "git",
        "merge-base",
        "--is-ancestor",
        source_revision,
        current,
        check=False,
    ).returncode != 0:
        raise HandoffError(
            "handoff source inbox is not trusted history"
        )
    return {event.event_id: event for event in events}


def _events_match_trust(
    proposed: Mapping[str, Any],
    event_ids: tuple[str, ...],
    trusted: Mapping[str, Any],
) -> bool:
    for event_id in event_ids:
        event = proposed.get(event_id)
        if event is None:
            return False
        if event.kind in _TRANSPORT_EVENT_KINDS and (
            trusted.get(event_id) != event
        ):
            return False
    return True


def _payload_hashes(value: Any) -> tuple[PayloadHash, ...]:
    if not isinstance(value, list):
        raise ValueError("handoff payload hashes must be a list")
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "record_id",
            "sha256",
        }:
            raise ValueError("handoff payload hash fields are invalid")
        result.append(
            PayloadHash(
                _string(item["record_id"], "payload identity"),
                _string(item["sha256"], "payload hash"),
            )
        )
    return tuple(result)


def _candidate_result_document(
    result: CandidateDesignResult,
) -> dict[str, object]:
    return {
        "base_commit": result.base_commit,
        "candidate_id": result.candidate_id,
        "complexity": result.complexity,
        "effect_id": result.effect_id,
        "generation": result.generation,
        "idea_id": result.idea_id,
        "issue_number": result.issue_number,
        "lessons": list(result.lessons),
        "motivation": result.motivation,
        "mutation_class": result.mutation_class,
        "parent_idea_ids": list(result.parent_idea_ids),
        "required_opt_ins": sorted(result.required_opt_ins),
        "result_id": result.result_id,
        "slot": result.slot,
        "spec_sha256": result.spec_sha256,
    }


def _candidate_result_from_document(value: Any) -> CandidateDesignResult:
    expected = {
        "base_commit",
        "candidate_id",
        "complexity",
        "effect_id",
        "generation",
        "idea_id",
        "issue_number",
        "lessons",
        "motivation",
        "mutation_class",
        "parent_idea_ids",
        "required_opt_ins",
        "result_id",
        "slot",
        "spec_sha256",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("candidate design result fields are invalid")
    return CandidateDesignResult(
        effect_id=_string(value["effect_id"], "effect ID"),
        result_id=_string(value["result_id"], "result ID"),
        issue_number=value["issue_number"],
        generation=value["generation"],
        spec_sha256=_string(value["spec_sha256"], "spec hash"),
        base_commit=_string(value["base_commit"], "base commit"),
        candidate_id=_string(value["candidate_id"], "candidate ID"),
        slot=value["slot"],
        idea_id=_string(value["idea_id"], "idea ID"),
        mutation_class=_string(
            value["mutation_class"],
            "mutation class",
        ),
        parent_idea_ids=_string_tuple(
            value["parent_idea_ids"],
            "parent idea IDs",
        ),
        required_opt_ins=frozenset(
            _string_tuple(
                value["required_opt_ins"],
                "required opt-ins",
            )
        ),
        motivation=_string(value["motivation"], "motivation"),
        lessons=_string_tuple(value["lessons"], "lessons"),
        complexity=_string(value["complexity"], "complexity"),
    )


def _pull_request_identity(
    pull_request: Mapping[str, Any],
    context: TrustedHandoffContext,
    *,
    allow_open_summary: bool = False,
) -> dict[str, Any]:
    user = pull_request.get("user")
    base = pull_request.get("base")
    head = pull_request.get("head")
    state = pull_request.get("state")
    merged = pull_request.get("merged")
    summary_is_unmerged = (
        allow_open_summary
        and state == "open"
        and merged is None
        and pull_request.get("merged_at") is None
    )
    if (
        state not in {"open", "closed"}
        or (merged is not False and not summary_is_unmerged)
        or not isinstance(user, Mapping)
        or not isinstance(base, Mapping)
        or not isinstance(head, Mapping)
        or not isinstance(base.get("repo"), Mapping)
        or not isinstance(head.get("repo"), Mapping)
    ):
        raise HandoffEventError("handoff pull request identity is invalid")
    number = pull_request.get("number")
    author = _copilot_author_login(user)
    base_repository = base["repo"].get("full_name")
    head_repository = head["repo"].get("full_name")
    base_ref = base.get("ref")
    head_ref = head.get("ref")
    base_revision = base.get("sha")
    head_revision = head.get("sha")
    if (
        type(number) is not int
        or number < 1
        or author is None
        or base_repository != context.repository
        or head_repository != context.repository
        or base_ref != context.default_branch
        or not isinstance(head_ref, str)
        or _BRANCH.fullmatch(head_ref) is None
        or not isinstance(base_revision, str)
        or _COMMIT.fullmatch(base_revision) is None
        or not isinstance(head_revision, str)
        or _COMMIT.fullmatch(head_revision) is None
    ):
        raise HandoffEventError("handoff pull request identity is invalid")
    return {
        "author_login": author,
        "base_ref": base_ref,
        "base_repository": base_repository,
        "base_revision": base_revision,
        "head_ref": head_ref,
        "head_repository": head_repository,
        "head_revision": head_revision,
        "number": number,
    }


def _copilot_author_login(user: Mapping[str, Any]) -> str | None:
    login = user.get("login")
    if login == "copilot-swe-agent[bot]":
        if user.get("type") not in {None, "Bot"}:
            return None
        return login
    if (
        login == "Copilot"
        and user.get("id") == _COPILOT_APP_USER_ID
        and user.get("type") == "Bot"
        and user.get("html_url")
        == "https://github.com/apps/copilot-swe-agent"
    ):
        return login
    return None


def _pull_request_created_at(
    pull_request: Mapping[str, Any],
) -> datetime:
    value = pull_request.get("created_at")
    if not isinstance(value, str) or not value.endswith("Z"):
        raise HandoffEventError(
            "handoff pull request creation time is invalid"
        )
    try:
        created_at = datetime.fromisoformat(
            value.removesuffix("Z") + "+00:00"
        )
    except ValueError as error:
        raise HandoffEventError(
            "handoff pull request creation time is invalid"
        ) from error
    if created_at.utcoffset() is None:
        raise HandoffEventError(
            "handoff pull request creation time is invalid"
        )
    return created_at


def _validate_candidate_handoff_privacy(
    result: CandidateDesignResult,
) -> None:
    forbidden = (
        "raw trace",
        "trace row",
        "dataset row",
        "private dataset",
        "raw prompt",
        "raw response",
        "tool payload",
        "authorization: bearer",
    )
    for value in (
        result.motivation,
        result.complexity,
        *result.lessons,
    ):
        if any(marker in value.casefold() for marker in forbidden):
            raise ValueError(
                "candidate design handoff privacy validation failed"
            )


def _path_is_allowed(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _safe_ref_name(value: str) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}",
            value,
        )
        is not None
        and ".." not in value
        and "//" not in value
        and not value.endswith(("/", ".", ".lock"))
    )


def _positive_number(value: int, description: str) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{description} is invalid")


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _document_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _string(value: Any, description: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{description} must be a string")
    return value


def _string_tuple(value: Any, description: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise ValueError(f"{description} must be a string list")
    return tuple(value)


def _require_commit(value: str, description: str) -> None:
    if not isinstance(value, str) or _COMMIT.fullmatch(value) is None:
        raise ValueError(f"{description} is invalid")


def _git_text(root: Path, *arguments: str, environment=None) -> str:
    return _run(
        root,
        "git",
        *arguments,
        environment=environment,
    ).stdout.decode("utf-8").strip()


def _run(
    root: Path,
    *arguments: str,
    environment: dict[str, str] | None = None,
    input_bytes: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        arguments,
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
    if check and completed.returncode != 0:
        raise HandoffError(
            f"handoff command failed: {arguments[1]}"
        )
    return completed


class _ProductionHandoffEffects:
    def __init__(
        self,
        root: Path,
        commands: CommandRunner,
        repository: str,
        assignment_token: str,
    ) -> None:
        self._root = root
        self._commands = commands
        self._repository = repository
        self._assignment_token = assignment_token

    def reconcile(self, issue_number: int) -> None:
        from foundry_opt.orchestration.git_state import GitStateRef
        from foundry_opt.orchestration.issue_intake import (
            GitIssueEventInbox,
            GitStateCampaignRecovery,
        )
        from foundry_opt.orchestration.transport import (
            reconcile_github_transport_effects,
        )

        recovery = GitStateCampaignRecovery(
            self._root,
            GitIssueEventInbox(self._root),
            GitStateRef(),
        )
        candidates = recovery.effect_candidates((issue_number,))
        if (
            issue_number in candidates.transport
            and recovery.can_reconcile_transport(issue_number)
        ):
            reconcile_github_transport_effects(
                self._root,
                issue_number,
                self._commands,
                self._repository,
                assignment_token=self._assignment_token,
            )
        if (
            issue_number not in candidates.persisted
            or not recovery.can_reconcile_persisted_effects(
                issue_number
            )
        ):
            return
        from foundry_opt.orchestration.deployment_bridge import (
            reconcile_deployment_cleanup_effects,
        )

        reconcile_deployment_cleanup_effects(
            self._root,
            issue_number,
            self._commands,
        )
        from foundry_opt.orchestration.projection import (
            DashboardProjection,
            GhDashboardGateway,
            GitStateProjectionOutbox,
        )

        DashboardProjection(
            GitStateProjectionOutbox(self._root),
            GhDashboardGateway(
                self._commands,
                self._root,
                self._repository,
            ),
        ).project(issue_number)


def main() -> None:
    from foundry_opt.adapters.commands import SubprocessCommandRunner
    from foundry_opt.orchestration.issue_intake import (
        GhStewardAssignments,
        GitStateCampaignRecovery,
    )

    assignment_token = os.environ.pop(
        "COPILOT_ASSIGNMENT_TOKEN",
        None,
    )
    if not assignment_token:
        raise HandoffEventError(
            "required Copilot assignment token is unavailable"
        )
    event_name = _required_environment("TRUSTED_EVENT_NAME")
    repository = _required_environment("TRUSTED_REPOSITORY")
    repository_id_text = _required_environment(
        "TRUSTED_REPOSITORY_ID"
    )
    default_branch = _required_environment("TRUSTED_DEFAULT_BRANCH")
    if not repository_id_text.isdecimal():
        raise HandoffEventError("trusted repository ID is invalid")
    context = TrustedHandoffContext(
        event_name,
        repository,
        int(repository_id_text),
        default_branch,
    )
    event_path = Path(_required_environment("TRUSTED_EVENT_PATH"))
    try:
        if event_path.stat().st_size > 2_000_000:
            raise HandoffEventError("handoff event payload is too large")
        payload = json.loads(event_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HandoffEventError(
            "handoff event payload cannot be read"
        ) from error
    if not isinstance(payload, Mapping):
        raise HandoffEventError("handoff event payload must be an object")
    root = Path.cwd()
    commands = SubprocessCommandRunner()
    gateway = GhHandoffPullRequestGateway(
        commands,
        root,
        repository,
    )
    if event_name == "pull_request_target":
        requests = (
            trusted_handoff_request_from_payload(
                payload,
                context,
                root,
                gateway,
            ),
        )
    else:
        _validate_trusted_event_repository(payload, context)
        requests = discover_trusted_handoff_requests(
            context,
            root,
            gateway,
            requested_pull_request=_optional_positive_environment(
                "TRUSTED_PULL_REQUEST_NUMBER"
            ),
        )
    inbox = GitIssueEventInbox(root)
    ledger = GitStateRef()
    assignments = GhStewardAssignments(
        commands,
        root,
        repository,
        assignment_token=assignment_token,
    )
    recovery = GitStateCampaignRecovery(root, inbox, ledger)
    finalizer = HandoffFinalizer(
        gateway=gateway,
        assignments=assignments,
        effects=_ProductionHandoffEffects(
            root,
            commands,
            repository,
            assignment_token,
        ),
        should_reassign=recovery.should_recover,
    )
    if not requests:
        print('{"processed":0}')
        return
    service = HandoffApplyService(ledger=ledger)
    for request in requests:
        result = service.apply(request)
        if result.handoff_id is None:
            result = replace(
                result,
                **_handoff_identity_from_path(request.handoff_path),
            )
        finalizer.finalize(request, result)
        print(
            json.dumps(
                {
                    "code": result.code,
                    "handoff_id": result.handoff_id,
                    "issue_number": result.issue_number,
                    "kind": result.kind,
                    "pull_request_number": request.pull_request_number,
                    "status": result.status.value,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )


def _handoff_identity_from_path(path: str) -> dict[str, object]:
    match = re.fullmatch(
        r"\.foundry-optimizer/handoffs/(steward|designer)/"
        r"issue-([1-9][0-9]*)/g[1-9][0-9]*/([0-9a-f]{64})\.json",
        path,
    )
    if match is None:
        raise HandoffEventError("handoff path identity is invalid")
    return {
        "handoff_id": match.group(3),
        "issue_number": int(match.group(2)),
        "kind": (
            "steward_state"
            if match.group(1) == "steward"
            else "candidate_design"
        ),
    }


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise HandoffEventError(
            f"required trusted environment is unavailable: {name}"
        )
    return value


def _optional_positive_environment(name: str) -> int | None:
    value = os.environ.get(name)
    if value in {None, ""}:
        return None
    if not value.isdecimal() or int(value) < 1:
        raise HandoffEventError(
            f"trusted positive integer is invalid: {name}"
        )
    return int(value)


def _validate_trusted_event_repository(
    payload: Mapping[str, Any],
    context: TrustedHandoffContext,
) -> None:
    repository = payload.get("repository")
    if (
        not isinstance(repository, Mapping)
        or repository.get("full_name") != context.repository
        or repository.get("id") != context.repository_id
        or repository.get("default_branch") != context.default_branch
    ):
        raise HandoffEventError(
            "handoff event repository identity is invalid"
        )


if __name__ == "__main__":
    main()
