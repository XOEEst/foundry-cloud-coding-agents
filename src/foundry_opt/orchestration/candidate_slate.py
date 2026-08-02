from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from types import MappingProxyType
from typing import Callable, Mapping, Protocol

from foundry_opt.evaluation import EvaluationPolicy
from foundry_opt.orchestration.campaign import OptimizationCampaign
from foundry_opt.orchestration.git_state import (
    OutboxRecord,
    StateObject,
    StateRefConflictError,
    StateRefError,
    StateRefSnapshot,
)
from foundry_opt.orchestration.models import CampaignEvent, EventKind
from foundry_opt.orchestration.models import (
    AdvanceRequest,
    CampaignPhase,
    CampaignState,
)


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def _identifier(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field_name} must be an identifier")


def _repository_path(value: Path, field_name: str) -> Path:
    raw = str(value)
    windows = PureWindowsPath(raw)
    posix = PurePosixPath(raw.replace("\\", "/"))
    if (
        not raw
        or windows.drive
        or raw.startswith(("/", "\\"))
        or ".." in posix.parts
    ):
        raise ValueError(f"{field_name} must be repository-relative")
    return Path(posix.as_posix())


@dataclass(frozen=True)
class CandidateBinding:
    issue_number: int
    generation: int
    spec_sha256: str
    base_commit: str
    candidate_id: str
    draft_id: str
    evidence_sha256: str
    patch_sha256: str
    bundle_sha256: str
    tree_sha: str
    allowed_paths: tuple[Path, ...]
    changed_paths: tuple[Path, ...]

    def __post_init__(self) -> None:
        if type(self.issue_number) is not int or self.issue_number < 1:
            raise ValueError("issue_number must be positive")
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("generation must be positive")
        for value, field_name in (
            (self.spec_sha256, "spec_sha256"),
            (self.evidence_sha256, "evidence_sha256"),
            (self.patch_sha256, "patch_sha256"),
            (self.bundle_sha256, "bundle_sha256"),
        ):
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise ValueError(f"{field_name} must be a SHA-256 digest")
        for value, field_name in (
            (self.base_commit, "base_commit"),
            (self.tree_sha, "tree_sha"),
        ):
            if not isinstance(value, str) or not _COMMIT.fullmatch(value):
                raise ValueError(f"{field_name} must be a full Git object")
        _identifier(self.candidate_id, "candidate_id")
        _identifier(self.draft_id, "draft_id")
        if not self.draft_id.startswith("draft-"):
            raise ValueError("draft_id must identify a draft")
        paths = tuple(
            _repository_path(path, "allowed_path")
            for path in self.allowed_paths
        )
        if not paths or len(set(paths)) != len(paths):
            raise ValueError("allowed_paths must be non-empty and unique")
        object.__setattr__(self, "allowed_paths", paths)
        changed = tuple(
            _repository_path(path, "changed_path")
            for path in self.changed_paths
        )
        if not changed or len(set(changed)) != len(changed):
            raise ValueError("changed_paths must be non-empty and unique")
        if any(
            not any(
                path == root or path.is_relative_to(root)
                for root in paths
            )
            for path in changed
        ):
            raise ValueError("changed_paths must be within allowed_paths")
        object.__setattr__(self, "changed_paths", changed)

    @property
    def binding_sha256(self) -> str:
        document = {
            "allowed_paths": [
                path.as_posix() for path in self.allowed_paths
            ],
            "base_commit": self.base_commit,
            "bundle_sha256": self.bundle_sha256,
            "candidate_id": self.candidate_id,
            "changed_paths": [
                path.as_posix() for path in self.changed_paths
            ],
            "draft_id": self.draft_id,
            "evidence_sha256": self.evidence_sha256,
            "generation": self.generation,
            "issue_number": self.issue_number,
            "patch_sha256": self.patch_sha256,
            "spec_sha256": self.spec_sha256,
            "tree_sha": self.tree_sha,
        }
        return hashlib.sha256(
            json.dumps(
                document,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class ApplierWorkerIntent:
    effect_id: str
    binding: CandidateBinding

    def __post_init__(self) -> None:
        _identifier(self.effect_id, "effect_id")
        if not isinstance(self.binding, CandidateBinding):
            raise ValueError("binding must be a CandidateBinding")


@dataclass(frozen=True)
class ApplierWorkerResult:
    effect_id: str
    result_id: str
    binding: CandidateBinding
    worker_issue_number: int
    created: bool
    assigned: bool

    def __post_init__(self) -> None:
        _identifier(self.effect_id, "effect_id")
        _identifier(self.result_id, "result_id")
        if not isinstance(self.binding, CandidateBinding):
            raise ValueError("binding must be a CandidateBinding")
        if (
            type(self.worker_issue_number) is not int
            or self.worker_issue_number < 1
        ):
            raise ValueError("worker_issue_number must be positive")
        if type(self.created) is not bool or type(self.assigned) is not bool:
            raise ValueError("worker result flags must be boolean")
        if self.assigned and not self.created:
            raise ValueError("an assigned worker issue must exist")

    def require_matches(self, intent: ApplierWorkerIntent) -> None:
        if self.effect_id != intent.effect_id:
            raise ValueError("applier result effect does not match intent")
        if self.binding != intent.binding:
            raise ValueError("applier result binding does not match intent")


@dataclass(frozen=True)
class CandidateSlatePlan:
    issue_number: int
    generation: int
    repository: str
    default_branch: str
    spec_sha256: str
    base_commit: str
    evaluation_policy: EvaluationPolicy
    required_checks: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.issue_number) is not int or self.issue_number < 1:
            raise ValueError("issue_number must be positive")
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("generation must be positive")
        if re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/"
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}",
            self.repository,
        ) is None:
            raise ValueError("repository is invalid")
        if (
            not isinstance(self.default_branch, str)
            or not self.default_branch
            or any(character.isspace() for character in self.default_branch)
        ):
            raise ValueError("default_branch is invalid")
        if not _SHA256.fullmatch(self.spec_sha256):
            raise ValueError("spec_sha256 must be a SHA-256 digest")
        if not _COMMIT.fullmatch(self.base_commit):
            raise ValueError("base_commit must be a full Git commit")
        if not isinstance(self.evaluation_policy, EvaluationPolicy):
            raise ValueError("evaluation_policy is invalid")
        if (
            not self.required_checks
            or len(set(self.required_checks)) != len(self.required_checks)
        ):
            raise ValueError("required_checks must be non-empty and unique")
        for check in self.required_checks:
            _identifier(check, "required check")

    @property
    def campaign_id(self) -> str:
        return (
            f"issue-{self.issue_number}-g{self.generation}-"
            f"{self.spec_sha256[:8]}-{self.base_commit[:8]}"
        )


@dataclass(frozen=True)
class CandidateSlateRequest:
    repository_root: Path
    issue_number: int

    def __post_init__(self) -> None:
        if type(self.issue_number) is not int or self.issue_number < 1:
            raise ValueError("issue_number must be positive")


class CandidateSlateStatus(StrEnum):
    PUBLISHED = "published"
    WAITING = "waiting"
    BLOCKED = "blocked"
    FAILED = "failed"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class CandidateSlateResult:
    status: CandidateSlateStatus
    snapshot: StateRefSnapshot
    summary: str
    code: str | None = None


class CandidateSlateLedger(Protocol):
    def load(
        self,
        repository_root: Path,
        issue_number: int,
    ) -> StateRefSnapshot | None: ...

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
    ) -> StateRefSnapshot: ...


class CandidateSlatePlanResolver(Protocol):
    def resolve(
        self,
        request: CandidateSlateRequest,
        state: CampaignState,
    ) -> CandidateSlatePlan: ...


class ApplierWorkerGateway(Protocol):
    def find_issue(self, marker: str) -> int | None: ...

    def create_issue(
        self,
        *,
        title: str,
        body: str,
        marker: str,
    ) -> int: ...

    def assign_exact_patch_specialist(
        self,
        issue_number: int,
        *,
        marker: str,
    ) -> None: ...

    def has_assignment_marker(
        self,
        issue_number: int,
        marker: str,
    ) -> bool: ...

    def record_assignment_marker(
        self,
        issue_number: int,
        marker: str,
    ) -> None: ...


class ApplierWorkerBridgeStatus(StrEnum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    RETRY = "retry"
    INVALID = "invalid"


@dataclass(frozen=True)
class ApplierWorkerBridgeResult:
    status: ApplierWorkerBridgeStatus
    worker_issue_number: int | None = None
    reason: str | None = None

    def worker_result(
        self,
        intent: ApplierWorkerIntent,
    ) -> ApplierWorkerResult:
        if (
            self.status
            not in {
                ApplierWorkerBridgeStatus.APPLIED,
                ApplierWorkerBridgeStatus.ALREADY_APPLIED,
            }
            or self.worker_issue_number is None
        ):
            raise ValueError("worker effect has not completed")
        return ApplierWorkerResult(
            effect_id=intent.effect_id,
            result_id=(
                f"applier-result-{intent.binding.binding_sha256[:16]}-"
                f"{self.worker_issue_number}"
            ),
            binding=intent.binding,
            worker_issue_number=self.worker_issue_number,
            created=True,
            assigned=True,
        )


class ApplierWorkerBridge:
    """Apply steward-decided worker issue effects without creating PRs."""

    def __init__(self, gateway: ApplierWorkerGateway) -> None:
        self._gateway = gateway

    def apply(self, record: OutboxRecord) -> ApplierWorkerBridgeResult:
        try:
            intent = _worker_intent(record)
        except (TypeError, ValueError):
            return ApplierWorkerBridgeResult(
                ApplierWorkerBridgeStatus.INVALID,
                reason="applier_worker_intent_invalid",
            )
        marker = candidate_pr_marker(intent.binding)
        try:
            issue_number = self._gateway.find_issue(marker)
            if issue_number is None:
                issue_number = self._gateway.create_issue(
                    title=(
                        "[foundry-opt] Apply exact "
                        f"{intent.binding.candidate_id}"
                    ),
                    body=_worker_issue_body(record, intent),
                    marker=marker,
                )
            if self._gateway.has_assignment_marker(issue_number, marker):
                return ApplierWorkerBridgeResult(
                    ApplierWorkerBridgeStatus.ALREADY_APPLIED,
                    issue_number,
                )
            self._gateway.assign_exact_patch_specialist(
                issue_number,
                marker=marker,
            )
            self._gateway.record_assignment_marker(
                issue_number,
                marker,
            )
        except RuntimeError:
            return ApplierWorkerBridgeResult(
                ApplierWorkerBridgeStatus.RETRY,
                issue_number if "issue_number" in locals() else None,
                "applier_worker_effect_unacknowledged",
            )
        return ApplierWorkerBridgeResult(
            ApplierWorkerBridgeStatus.APPLIED,
            issue_number,
        )


def applier_worker_result_record(
    planned: OutboxRecord,
    result: ApplierWorkerResult,
    *,
    sequence: int | None = None,
    generation: int | None = None,
) -> OutboxRecord:
    intent = _worker_intent(planned)
    result.require_matches(intent)
    return OutboxRecord(
        record_id=f"{planned.record_id}-succeeded",
        kind="applier_worker_issue_succeeded",
        generation=(
            planned.generation if generation is None else generation
        ),
        sequence=planned.sequence if sequence is None else sequence,
        payload={
            "assigned": result.assigned,
            "binding_sha256": result.binding.binding_sha256,
            "candidate_id": result.binding.candidate_id,
            "created": result.created,
            "effect_id": result.effect_id,
            "issue_number": result.binding.issue_number,
            "result_id": result.result_id,
            "worker_issue_number": result.worker_issue_number,
        },
    )


class CandidateEffectRecordStatus(StrEnum):
    RECORDED = "recorded"
    ALREADY_RECORDED = "already_recorded"
    CONFLICT = "conflict"
    FAILED = "failed"


@dataclass(frozen=True)
class CandidateEffectRecordResult:
    status: CandidateEffectRecordStatus
    snapshot: StateRefSnapshot
    code: str | None = None


class CandidateEffectResultRecorder:
    """CAS-persist bridge acknowledgements before later PR intake."""

    def __init__(self, ledger: CandidateSlateLedger) -> None:
        self._ledger = ledger

    def record(
        self,
        repository_root: Path,
        issue_number: int,
        result: ApplierWorkerResult,
    ) -> CandidateEffectRecordResult:
        snapshot = self._ledger.load(repository_root, issue_number)
        if snapshot is None:
            raise ValueError("candidate effect requires campaign state")
        planned = tuple(
            record
            for record in snapshot.outbox
            if record.record_id == result.effect_id
        )
        if len(planned) != 1:
            return CandidateEffectRecordResult(
                CandidateEffectRecordStatus.FAILED,
                snapshot,
                "candidate_effect_plan_unavailable",
            )
        success = applier_worker_result_record(
            planned[0],
            result,
            sequence=snapshot.state.sequence,
            generation=snapshot.state.generation,
        )
        existing = tuple(
            record
            for record in snapshot.outbox
            if record.record_id == success.record_id
        )
        if existing:
            if (
                len(existing) != 1
                or existing[0].kind != success.kind
                or existing[0].generation != success.generation
                or dict(existing[0].payload) != dict(success.payload)
            ):
                return CandidateEffectRecordResult(
                    CandidateEffectRecordStatus.FAILED,
                    snapshot,
                    "candidate_effect_result_conflict",
                )
            return CandidateEffectRecordResult(
                CandidateEffectRecordStatus.ALREADY_RECORDED,
                snapshot,
            )
        try:
            persisted = self._ledger.commit(
                repository_root,
                issue_number=issue_number,
                expected_revision=snapshot.revision,
                state=snapshot.state,
                outbox=(success,),
            )
        except StateRefConflictError:
            return CandidateEffectRecordResult(
                CandidateEffectRecordStatus.CONFLICT,
                snapshot,
                "state_ref_conflict",
            )
        except (StateRefError, TypeError, ValueError):
            return CandidateEffectRecordResult(
                CandidateEffectRecordStatus.FAILED,
                snapshot,
                "candidate_effect_persist_failed",
            )
        return CandidateEffectRecordResult(
            CandidateEffectRecordStatus.RECORDED,
            persisted,
        )


class CandidateSupersessionGateway(Protocol):
    def issue_is_superseded(self, number: int, marker: str) -> bool: ...

    def supersede_issue(
        self,
        number: int,
        body: str,
        marker: str,
    ) -> None: ...

    def pull_request_is_superseded(
        self,
        number: int,
        marker: str,
    ) -> bool: ...

    def supersede_pull_request(
        self,
        number: int,
        body: str,
        marker: str,
    ) -> None: ...


class CandidateSupersessionBridgeStatus(StrEnum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    RETRY = "retry"
    INVALID = "invalid"


@dataclass(frozen=True)
class CandidateSupersessionBridgeResult:
    status: CandidateSupersessionBridgeStatus
    reason: str | None = None


class CandidateSupersessionBridge:
    def __init__(self, gateway: CandidateSupersessionGateway) -> None:
        self._gateway = gateway

    def apply(
        self,
        record: OutboxRecord,
    ) -> CandidateSupersessionBridgeResult:
        try:
            marker, number, is_issue, reason = _supersession_binding(record)
        except (TypeError, ValueError):
            return CandidateSupersessionBridgeResult(
                CandidateSupersessionBridgeStatus.INVALID,
                "candidate_supersession_intent_invalid",
            )
        body = (
            marker
            + (
                "\nSuperseded because another exact eligible candidate "
                "pull request was selected by merge.\n"
                if reason == "candidate_selected_elsewhere"
                else (
                    "\nCandidate pull request rejected by exact "
                    f"verification: `{reason}`.\n"
                )
            )
        )
        try:
            already = (
                self._gateway.issue_is_superseded(number, marker)
                if is_issue
                else self._gateway.pull_request_is_superseded(
                    number,
                    marker,
                )
            )
            if already:
                return CandidateSupersessionBridgeResult(
                    CandidateSupersessionBridgeStatus.ALREADY_APPLIED
                )
            if is_issue:
                self._gateway.supersede_issue(number, body, marker)
            else:
                self._gateway.supersede_pull_request(
                    number,
                    body,
                    marker,
                )
        except RuntimeError:
            return CandidateSupersessionBridgeResult(
                CandidateSupersessionBridgeStatus.RETRY,
                "candidate_supersession_unacknowledged",
            )
        return CandidateSupersessionBridgeResult(
            CandidateSupersessionBridgeStatus.APPLIED
        )


class CandidateSlateService:
    """Publish durable candidate objects and issue intents after workers."""

    def __init__(
        self,
        *,
        ledger: CandidateSlateLedger,
        resolver: CandidateSlatePlanResolver,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._ledger = ledger
        self._resolver = resolver
        self._clock = clock or (lambda: datetime.now(UTC))

    def advance(self, request: CandidateSlateRequest) -> CandidateSlateResult:
        snapshot = self._ledger.load(
            request.repository_root,
            request.issue_number,
        )
        if snapshot is None:
            raise ValueError("candidate slate requires campaign state")
        if snapshot.state.phase is CampaignPhase.AWAITING_SELECTION:
            return CandidateSlateResult(
                CandidateSlateStatus.WAITING,
                snapshot,
                "The candidate slate is already published.",
            )
        if snapshot.state.phase is CampaignPhase.BLOCKED:
            return CandidateSlateResult(
                CandidateSlateStatus.BLOCKED,
                snapshot,
                "The campaign has no publishable candidate slate.",
                snapshot.state.block_reason,
            )
        if snapshot.state.phase is not CampaignPhase.CANDIDATES:
            return CandidateSlateResult(
                CandidateSlateStatus.BLOCKED,
                snapshot,
                "Candidate slate publication is invalid in this phase.",
                "candidate_slate_phase_invalid",
            )
        if not any(
            event.kind is EventKind.CANDIDATE_WORKERS_COMPLETED
            and event.generation == snapshot.state.generation
            for event in snapshot.inbox
        ):
            return CandidateSlateResult(
                CandidateSlateStatus.WAITING,
                snapshot,
                "Candidate workers have not completed.",
                "candidate_workers_incomplete",
            )
        plan = self._resolver.resolve(request, snapshot.state)
        mismatch = _slate_plan_mismatch(request, snapshot.state, plan)
        if mismatch is not None:
            return CandidateSlateResult(
                CandidateSlateStatus.BLOCKED,
                snapshot,
                "Candidate slate inputs are stale.",
                mismatch,
            )
        eligible = tuple(
            candidate
            for candidate in snapshot.state.candidates
            if candidate.eligible
        )
        if not eligible:
            return CandidateSlateResult(
                CandidateSlateStatus.BLOCKED,
                snapshot,
                "No eligible candidate can be published.",
                "no_eligible_candidates",
            )
        try:
            baseline = _single_record(
                snapshot,
                "candidate_baseline_attestation",
                plan.generation,
            )
            baseline_metrics = _numeric_mapping(
                baseline.payload.get("metrics"),
                "baseline metrics",
            )
            candidates = tuple(
                _load_candidate(
                    request.repository_root,
                    snapshot,
                    plan,
                    candidate.candidate_id,
                    candidate.evidence_sha256,
                    baseline_metrics,
                )
                for candidate in eligible
            )
            ranked = _rank_candidates(
                candidates,
                baseline_metrics,
                plan.evaluation_policy,
            )
            event = CampaignEvent(
                event_id=f"candidate-slate-{plan.generation}-published",
                kind=EventKind.SLATE_PUBLISHED,
                generation=plan.generation,
                occurred_at=self._clock(),
            )
            state = OptimizationCampaign().advance(
                AdvanceRequest(
                    request.issue_number,
                    snapshot.state,
                    (event,),
                )
            ).state
            checkpoint = StateRefSnapshot(
                snapshot.revision,
                state,
                snapshot.inbox,
                snapshot.outbox,
                snapshot.objects,
            )
            candidate_objects = tuple(
                item
                for candidate in ranked
                for item in candidate["objects"]
            )
            objects = _new_slate_objects(snapshot, candidate_objects)
            intents = tuple(
                _applier_outbox(checkpoint, plan, candidate)
                for candidate in ranked
            )
            dashboard = _dashboard_outbox(
                checkpoint,
                plan,
                ranked,
                baseline_metrics,
            )
            persisted = self._ledger.commit(
                request.repository_root,
                issue_number=request.issue_number,
                expected_revision=snapshot.revision,
                state=state,
                inbox=(event,),
                outbox=(dashboard, *intents),
                objects=objects,
            )
        except StateRefConflictError:
            return CandidateSlateResult(
                CandidateSlateStatus.CONFLICT,
                snapshot,
                "Candidate slate state changed concurrently.",
                "state_ref_conflict",
            )
        except (KeyError, OSError, StateRefError, TypeError, ValueError):
            return CandidateSlateResult(
                CandidateSlateStatus.FAILED,
                snapshot,
                "Candidate slate evidence could not be verified.",
                "candidate_slate_invalid",
            )
        return CandidateSlateResult(
            CandidateSlateStatus.PUBLISHED,
            persisted,
            "Candidate slate published for human merge selection.",
        )


class CandidatePullRequestState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"


class CandidatePullRequestAction(StrEnum):
    OPENED = "opened"
    SYNCHRONIZE = "synchronize"
    EDITED = "edited"
    CLOSED = "closed"
    MERGED = "merged"


@dataclass(frozen=True)
class TrustedCandidatePullRequestContext:
    event_name: str
    delivery_id: str
    repository: str
    repository_id: int

    def __post_init__(self) -> None:
        if self.event_name != "pull_request":
            raise ValueError("event_name must be pull_request")
        _identifier(self.delivery_id, "delivery_id")
        if re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/"
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}",
            self.repository,
        ) is None:
            raise ValueError("repository is invalid")
        if type(self.repository_id) is not int or self.repository_id < 1:
            raise ValueError("repository_id must be positive")


def candidate_pull_request_event_from_payload(
    payload: Mapping[str, object],
    context: TrustedCandidatePullRequestContext,
    bindings: tuple[CandidateBinding, ...],
) -> CandidatePullRequestEvent:
    if not isinstance(payload, Mapping):
        raise ValueError("pull request payload must be an object")
    repository = payload.get("repository")
    if (
        not isinstance(repository, Mapping)
        or repository.get("id") != context.repository_id
        or repository.get("full_name") != context.repository
    ):
        raise ValueError("repository identity does not match")
    action = payload.get("action")
    if action not in {"opened", "synchronize", "edited", "closed", "merged"}:
        raise ValueError("pull request action is not trusted")
    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, Mapping):
        raise ValueError("pull request identity is missing")
    number = pull_request.get("number")
    body = pull_request.get("body")
    user = pull_request.get("user")
    head = pull_request.get("head")
    if (
        type(number) is not int
        or number < 1
        or not isinstance(body, str)
        or not isinstance(user, Mapping)
        or user.get("login") != "copilot-swe-agent[bot]"
        or not isinstance(head, Mapping)
        or not isinstance(head.get("sha"), str)
        or not _COMMIT.fullmatch(str(head["sha"]))
    ):
        raise ValueError("pull request worker identity is invalid")
    lineage_bodies = [body]
    changes = payload.get("changes")
    changed_body = (
        changes.get("body")
        if isinstance(changes, Mapping)
        else None
    )
    previous_body = (
        changed_body.get("from")
        if isinstance(changed_body, Mapping)
        else None
    )
    if isinstance(previous_body, str):
        lineage_bodies.append(previous_body)
    matching = tuple(
        binding
        for binding in bindings
        if any(
            candidate_pr_marker(binding) in candidate_body
            for candidate_body in lineage_bodies
        )
    )
    if len(matching) != 1:
        raise ValueError("pull request worker lineage is invalid")
    occurred_at_text = pull_request.get("updated_at")
    if not isinstance(occurred_at_text, str):
        raise ValueError("pull request timestamp is invalid")
    try:
        occurred_at = datetime.fromisoformat(
            occurred_at_text.replace("Z", "+00:00")
        )
    except ValueError as error:
        raise ValueError("pull request timestamp is invalid") from error
    if occurred_at.tzinfo is None:
        raise ValueError("pull request timestamp is invalid")
    merged = action == "merged" or (
        action == "closed" and pull_request.get("merged") is True
    )
    normalized = (
        CandidatePullRequestAction.MERGED
        if merged
        else CandidatePullRequestAction(str(action))
    )
    merge_commit = pull_request.get("merge_commit_sha") if merged else None
    if merged and (
        not isinstance(merge_commit, str)
        or not _COMMIT.fullmatch(merge_commit)
    ):
        raise ValueError("merged pull request commit is invalid")
    return CandidatePullRequestEvent(
        event_id=context.delivery_id,
        action=normalized,
        occurred_at=occurred_at,
        binding=matching[0],
        pull_request_number=number,
        head_commit=str(head["sha"]),
        merge_commit=merge_commit,
    )


@dataclass(frozen=True)
class CandidatePullRequestEvent:
    event_id: str
    action: CandidatePullRequestAction
    occurred_at: datetime
    binding: CandidateBinding
    pull_request_number: int
    head_commit: str
    merge_commit: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.event_id, "event_id")
        if not isinstance(self.action, CandidatePullRequestAction):
            raise ValueError("action must be a CandidatePullRequestAction")
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        if not isinstance(self.binding, CandidateBinding):
            raise ValueError("binding must be a CandidateBinding")
        if (
            type(self.pull_request_number) is not int
            or self.pull_request_number < 1
        ):
            raise ValueError("pull_request_number must be positive")
        if not _COMMIT.fullmatch(self.head_commit):
            raise ValueError("head_commit must be a full Git commit")
        if self.merge_commit is not None and not _COMMIT.fullmatch(
            self.merge_commit
        ):
            raise ValueError("merge_commit must be a full Git commit")
        if (
            self.action is CandidatePullRequestAction.MERGED
            and self.merge_commit is None
        ):
            raise ValueError("merged events require merge_commit")
        if (
            self.action is not CandidatePullRequestAction.MERGED
            and self.merge_commit is not None
        ):
            raise ValueError("merge_commit is only valid for merged events")

    def to_campaign_event(self) -> CampaignEvent:
        kinds = {
            CandidatePullRequestAction.OPENED: EventKind.CANDIDATE_PR_OPENED,
            CandidatePullRequestAction.SYNCHRONIZE: (
                EventKind.CANDIDATE_PR_SYNCHRONIZED
            ),
            CandidatePullRequestAction.EDITED: EventKind.CANDIDATE_PR_EDITED,
            CandidatePullRequestAction.CLOSED: EventKind.CANDIDATE_PR_CLOSED,
            CandidatePullRequestAction.MERGED: EventKind.CANDIDATE_PR_MERGED,
        }
        payload: dict[str, object] = {
            "binding_sha256": self.binding.binding_sha256,
            "candidate_id": self.binding.candidate_id,
            "head_commit": self.head_commit,
            "pull_request_number": self.pull_request_number,
        }
        if self.merge_commit is not None:
            payload["merge_commit"] = self.merge_commit
        return CampaignEvent(
            event_id=self.event_id,
            kind=kinds[self.action],
            generation=self.binding.generation,
            occurred_at=self.occurred_at,
            payload=payload,
        )


class CandidatePullRequestInbox(Protocol):
    def append(
        self,
        issue_number: int,
        event: CampaignEvent,
    ) -> bool: ...


class CandidatePullRequestIntakeStatus(StrEnum):
    RECORDED = "recorded"
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class CandidatePullRequestIntakeResult:
    status: CandidatePullRequestIntakeStatus
    event: CampaignEvent


class CandidatePullRequestEventIntake:
    """Append trusted PR events; steward replay decides their meaning."""

    def __init__(self, inbox: CandidatePullRequestInbox) -> None:
        self._inbox = inbox

    def ingest(
        self,
        event: CandidatePullRequestEvent,
    ) -> CandidatePullRequestIntakeResult:
        campaign_event = event.to_campaign_event()
        recorded = self._inbox.append(
            event.binding.issue_number,
            campaign_event,
        )
        return CandidatePullRequestIntakeResult(
            (
                CandidatePullRequestIntakeStatus.RECORDED
                if recorded
                else CandidatePullRequestIntakeStatus.DUPLICATE
            ),
            campaign_event,
        )


@dataclass(frozen=True)
class CandidatePullRequestReference:
    pull_request_number: int
    binding_sha256: str
    worker_issue_number: int | None = None

    def __post_init__(self) -> None:
        if (
            type(self.pull_request_number) is not int
            or self.pull_request_number < 1
        ):
            raise ValueError("pull_request_number must be positive")
        if not _SHA256.fullmatch(self.binding_sha256):
            raise ValueError("binding_sha256 must be a SHA-256 digest")
        if (
            self.worker_issue_number is not None
            and (
                type(self.worker_issue_number) is not int
                or self.worker_issue_number < 1
            )
        ):
            raise ValueError("worker_issue_number must be positive")


@dataclass(frozen=True)
class CandidateSelectionRequest:
    repository_root: Path
    issue_number: int
    expected_default_branch: str
    required_checks: tuple[str, ...]
    observed_pull_requests: tuple[CandidatePullRequestReference, ...] = ()

    def __post_init__(self) -> None:
        if type(self.issue_number) is not int or self.issue_number < 1:
            raise ValueError("issue_number must be positive")
        if (
            not isinstance(self.expected_default_branch, str)
            or not self.expected_default_branch
        ):
            raise ValueError("expected_default_branch is required")
        if (
            not self.required_checks
            or len(set(self.required_checks)) != len(self.required_checks)
        ):
            raise ValueError("required_checks must be non-empty and unique")
        observations = tuple(self.observed_pull_requests)
        if any(
            not isinstance(item, CandidatePullRequestReference)
            for item in observations
        ):
            raise ValueError(
                "observed_pull_requests must contain typed references"
            )
        if len(
            {item.pull_request_number for item in observations}
        ) != len(observations):
            raise ValueError(
                "observed pull request numbers must be unique"
            )
        object.__setattr__(self, "observed_pull_requests", observations)


class CandidateSelectionStatus(StrEnum):
    SELECTED = "selected"
    WAITING = "waiting"
    BLOCKED = "blocked"
    FAILED = "failed"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class CandidateSelectionResult:
    status: CandidateSelectionStatus
    snapshot: StateRefSnapshot
    summary: str
    code: str | None = None


class CandidatePullRequestReader(Protocol):
    def snapshots_for(
        self,
        request: CandidateSelectionRequest,
        bindings: tuple[CandidateBinding, ...],
    ) -> tuple[CandidatePullRequestSnapshot, ...]: ...


class CandidateSelectionService:
    """Treat exactly one valid candidate PR merge as the selection event."""

    def __init__(
        self,
        *,
        ledger: CandidateSlateLedger,
        reader: CandidatePullRequestReader,
        resolver: CandidateSlatePlanResolver | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._ledger = ledger
        self._reader = reader
        self._resolver = resolver
        self._clock = clock or (lambda: datetime.now(UTC))

    def advance(
        self,
        request: CandidateSelectionRequest | CandidateSlateRequest,
    ) -> CandidateSelectionResult:
        snapshot = self._ledger.load(
            request.repository_root,
            request.issue_number,
        )
        if snapshot is None:
            raise ValueError("candidate selection requires campaign state")
        if snapshot.state.phase in {
            CampaignPhase.RETENTION,
            CampaignPhase.COMPLETED,
        }:
            return CandidateSelectionResult(
                CandidateSelectionStatus.WAITING,
                snapshot,
                "A candidate selection has already been recorded.",
            )
        if snapshot.state.phase is CampaignPhase.BLOCKED:
            return CandidateSelectionResult(
                CandidateSelectionStatus.BLOCKED,
                snapshot,
                "Candidate selection is blocked.",
                snapshot.state.block_reason,
            )
        if snapshot.state.phase not in {
            CampaignPhase.AWAITING_SELECTION,
            CampaignPhase.DEPLOYMENT,
        }:
            return CandidateSelectionResult(
                CandidateSelectionStatus.BLOCKED,
                snapshot,
                "Candidate selection is not valid in this phase.",
                "candidate_selection_phase_invalid",
            )
        selection_request = request
        if isinstance(request, CandidateSlateRequest):
            if self._resolver is None:
                return CandidateSelectionResult(
                    CandidateSelectionStatus.FAILED,
                    snapshot,
                    "Candidate selection policy is unavailable.",
                    "candidate_selection_policy_unavailable",
                )
            plan = self._resolver.resolve(request, snapshot.state)
            mismatch = _slate_plan_mismatch(
                selection_request,
                snapshot.state,
                plan,
            )
            if mismatch is not None:
                return CandidateSelectionResult(
                    CandidateSelectionStatus.BLOCKED,
                    snapshot,
                    "Candidate selection policy is stale.",
                    mismatch,
                )
            selection_request = CandidateSelectionRequest(
                request.repository_root,
                request.issue_number,
                plan.default_branch,
                plan.required_checks,
            )
        assert isinstance(selection_request, CandidateSelectionRequest)
        selection_request = replace(
            selection_request,
            observed_pull_requests=_observed_pull_requests(
                snapshot,
                selection_request.observed_pull_requests,
            ),
        )
        try:
            worker_bindings = _completed_worker_bindings(snapshot)
            bindings = tuple(
                binding for binding, _ in worker_bindings
            )
            expected_worker_issues = {
                binding.candidate_id: worker_issue_number
                for binding, worker_issue_number in worker_bindings
            }
            pull_requests = self._reader.snapshots_for(
                selection_request,
                bindings,
            )
            by_candidate: dict[str, CandidatePullRequestSnapshot] = {}
            for pull_request in pull_requests:
                matching = tuple(
                    binding
                    for binding in bindings
                    if (
                        pull_request.binding_sha256
                        == binding.binding_sha256
                        or (
                            pull_request.binding_sha256 is None
                            and candidate_pr_marker(binding)
                            in pull_request.body
                        )
                    )
                )
                if len(matching) != 1:
                    if snapshot.state.phase is CampaignPhase.DEPLOYMENT:
                        continue
                    raise ValueError("candidate PR lineage is ambiguous")
                candidate_id = matching[0].candidate_id
                if (
                    pull_request.worker_issue_number
                    != expected_worker_issues[candidate_id]
                ):
                    raise ValueError(
                        "candidate PR worker issue lineage changed"
                    )
                if candidate_id in by_candidate:
                    if (
                        pull_request.state
                        is CandidatePullRequestState.MERGED
                        or by_candidate[candidate_id].state
                        is CandidatePullRequestState.MERGED
                    ):
                        return self._block(
                            selection_request,
                            snapshot,
                            "multiple_candidate_merges",
                        )
                    raise ValueError("candidate has competing pull requests")
                by_candidate[candidate_id] = pull_request
            verified_merges: list[
                tuple[CandidateBinding, CandidatePullRequestSnapshot]
            ] = []
            invalid_merges: list[str] = []
            observations: list[OutboxRecord] = []
            for binding in bindings:
                pull_request = by_candidate.get(binding.candidate_id)
                if pull_request is None:
                    continue
                verification = verify_candidate_pull_request(
                    binding,
                    pull_request,
                    expected_default_branch=(
                        selection_request.expected_default_branch
                    ),
                    required_checks=selection_request.required_checks,
                )
                if (
                    pull_request.state
                    is CandidatePullRequestState.MERGED
                    and snapshot.state.phase is CampaignPhase.DEPLOYMENT
                    and binding.candidate_id
                    == snapshot.state.selected_candidate_id
                    and pull_request.merge_commit
                    == snapshot.state.merge_commit
                ):
                    verified_merges.append((binding, pull_request))
                    continue
                if pull_request.state is not CandidatePullRequestState.MERGED:
                    observations.append(
                        _pr_observation(
                            snapshot,
                            binding,
                            pull_request,
                            verification,
                        )
                    )
                if pull_request.state is CandidatePullRequestState.MERGED:
                    if (
                        verification.status
                        is CandidatePullRequestVerificationStatus.VERIFIED
                    ):
                        verified_merges.append((binding, pull_request))
                    else:
                        invalid_merges.append(
                            verification.reason or "candidate_merge_invalid"
                        )
            if invalid_merges:
                return self._block(
                    request,
                    snapshot,
                    "invalid_candidate_merge",
                )
            if snapshot.state.phase is CampaignPhase.DEPLOYMENT:
                competing = tuple(
                    (binding, pull_request)
                    for binding, pull_request in verified_merges
                    if (
                        binding.candidate_id
                        != snapshot.state.selected_candidate_id
                        or pull_request.merge_commit
                        != snapshot.state.merge_commit
                    )
                )
                if competing or len(verified_merges) > 1:
                    return self._block(
                        selection_request,
                        snapshot,
                        "multiple_candidate_merges",
                    )
                return CandidateSelectionResult(
                    CandidateSelectionStatus.WAITING,
                    snapshot,
                    "The recorded candidate selection remains unique.",
                    "candidate_selection_recorded",
                )
            if len(verified_merges) > 1:
                return self._block(
                    selection_request,
                    snapshot,
                    "multiple_candidate_merges",
                )
            if not verified_merges:
                existing_ids = {
                    record.record_id for record in snapshot.outbox
                }
                new_observations = tuple(
                    record
                    for record in observations
                    if record.record_id not in existing_ids
                )
                persisted = (
                    self._ledger.commit(
                        request.repository_root,
                        issue_number=request.issue_number,
                        expected_revision=snapshot.revision,
                        state=snapshot.state,
                        outbox=new_observations,
                    )
                    if new_observations
                    else snapshot
                )
                return CandidateSelectionResult(
                    CandidateSelectionStatus.WAITING,
                    persisted,
                    "Merge exactly one eligible candidate pull request.",
                    "waiting_for_candidate_merge",
                )
            selected, pull_request = verified_merges[0]
            assert pull_request.merge_commit is not None
            event = CampaignEvent(
                event_id=(
                    f"candidate-merge-{snapshot.state.generation}-"
                    f"{pull_request.pull_request_number}-"
                    f"{pull_request.merge_commit[:16]}"
                ),
                kind=EventKind.CANDIDATE_MERGED,
                generation=snapshot.state.generation,
                occurred_at=self._clock(),
                payload={
                    "candidate_id": selected.candidate_id,
                    "merge_commit": pull_request.merge_commit,
                },
            )
            state = OptimizationCampaign().advance(
                AdvanceRequest(
                    request.issue_number,
                    snapshot.state,
                    (event,),
                )
            ).state
            checkpoint = StateRefSnapshot(
                snapshot.revision,
                state,
                snapshot.inbox,
                snapshot.outbox,
                snapshot.objects,
            )
            effects = tuple(
                effect
                for binding in bindings
                if binding.candidate_id != selected.candidate_id
                for effect in _supersession_effects(
                    checkpoint,
                    binding,
                    by_candidate.get(binding.candidate_id),
                )
            )
            dashboard = OutboxRecord(
                record_id=(
                    f"selection-dashboard-{state.generation}-"
                    f"{selected.candidate_id}-"
                    f"{pull_request.merge_commit[:16]}"
                ),
                kind="candidate_selection_dashboard",
                generation=state.generation,
                sequence=state.sequence,
                payload={
                    "disposition": "wait",
                    "issue_number": request.issue_number,
                    "merge_commit": pull_request.merge_commit,
                    "next_action": "deployment_ready_for_next_phase",
                    "phase": CampaignPhase.DEPLOYMENT.value,
                    "selected_candidate_id": selected.candidate_id,
                    "spec_sha256": selected.spec_sha256,
                    "status": "ready",
                },
            )
            selection_record = OutboxRecord(
                record_id=(
                    f"selection-{state.generation}-"
                    f"{selected.candidate_id}-"
                    f"{pull_request.pull_request_number}"
                ),
                kind="candidate_selection_recorded",
                generation=state.generation,
                sequence=state.sequence,
                payload={
                    "binding_sha256": selected.binding_sha256,
                    "candidate_id": selected.candidate_id,
                    "head_commit": pull_request.head_commit,
                    "issue_number": request.issue_number,
                    "merge_commit": pull_request.merge_commit,
                    "pull_request_number": (
                        pull_request.pull_request_number
                    ),
                    "tree_sha": selected.tree_sha,
                    "worker_issue_number": (
                        pull_request.worker_issue_number
                    ),
                },
            )
            persisted = self._ledger.commit(
                request.repository_root,
                issue_number=request.issue_number,
                expected_revision=snapshot.revision,
                state=state,
                inbox=(event,),
                outbox=(
                    dashboard,
                    selection_record,
                    *(
                        record
                        for record in observations
                        if record.record_id
                        not in {
                            item.record_id for item in snapshot.outbox
                        }
                    ),
                    *effects,
                ),
            )
        except StateRefConflictError:
            return CandidateSelectionResult(
                CandidateSelectionStatus.CONFLICT,
                snapshot,
                "Candidate selection state changed concurrently.",
                "state_ref_conflict",
            )
        except _WorkerAcknowledgementPending:
            return CandidateSelectionResult(
                CandidateSelectionStatus.WAITING,
                snapshot,
                "Candidate worker acknowledgement is still pending.",
                "candidate_worker_ack_pending",
            )
        except (KeyError, StateRefError, TypeError, ValueError):
            return CandidateSelectionResult(
                CandidateSelectionStatus.FAILED,
                snapshot,
                "Candidate pull request lineage could not be verified.",
                "candidate_selection_invalid",
            )
        except RuntimeError:
            return CandidateSelectionResult(
                CandidateSelectionStatus.FAILED,
                snapshot,
                "Candidate pull requests could not be inspected.",
                "candidate_pr_intake_unavailable",
            )
        return CandidateSelectionResult(
            CandidateSelectionStatus.SELECTED,
            persisted,
            "Candidate selected by exact pull request merge.",
        )

    def _block(
        self,
        request: CandidateSelectionRequest,
        snapshot: StateRefSnapshot,
        reason: str,
    ) -> CandidateSelectionResult:
        event_id = (
            f"candidate-selection-{snapshot.state.generation}-{reason}"
        )
        event = CampaignEvent(
            event_id=event_id,
            kind=EventKind.CANDIDATE_SELECTION_FAILED,
            generation=snapshot.state.generation,
            occurred_at=self._clock(),
            payload={"reason": reason},
        )
        state = OptimizationCampaign().advance(
            AdvanceRequest(
                request.issue_number,
                snapshot.state,
                (event,),
            )
        ).state
        record = OutboxRecord(
            record_id=event_id,
            kind="candidate_selection_blocked",
            generation=state.generation,
            sequence=state.sequence,
            payload={
                "issue_number": request.issue_number,
                "reason": reason,
            },
        )
        try:
            persisted = self._ledger.commit(
                request.repository_root,
                issue_number=request.issue_number,
                expected_revision=snapshot.revision,
                state=state,
                inbox=(event,),
                outbox=(record,),
            )
        except StateRefConflictError:
            return CandidateSelectionResult(
                CandidateSelectionStatus.CONFLICT,
                snapshot,
                "Candidate selection state changed concurrently.",
                "state_ref_conflict",
            )
        return CandidateSelectionResult(
            CandidateSelectionStatus.BLOCKED,
            persisted,
            "Candidate selection failed closed.",
            reason,
        )


@dataclass(frozen=True)
class CandidatePullRequestSnapshot:
    pull_request_number: int
    worker_issue_number: int
    state: CandidatePullRequestState
    author: str
    draft: bool
    base_ref_name: str
    current_default_branch: str
    current_default_commit: str
    base_commit: str
    head_commit: str
    head_parent_commit: str
    head_tree_sha: str
    patch_sha256: str
    changed_paths: tuple[Path, ...]
    body: str
    checks: Mapping[str, str]
    binding_sha256: str | None = None
    spec_sha256: str | None = None
    bundle_sha256: str | None = None
    evidence_sha256: str | None = None
    marker: str | None = None
    merge_commit: str | None = None
    merge_parent_commit: str | None = None
    merge_tree_sha: str | None = None
    merge_reachable_from_default: bool = False
    merge_actor: str | None = None

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.pull_request_number, "pull_request_number"),
            (self.worker_issue_number, "worker_issue_number"),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{field_name} must be positive")
        if not isinstance(self.state, CandidatePullRequestState):
            raise ValueError("state must be a CandidatePullRequestState")
        if (
            not isinstance(self.author, str)
            or re.fullmatch(
                r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}|"
                r"[A-Za-z0-9-]{0,34}\[bot\])",
                self.author,
            )
            is None
        ):
            raise ValueError("author must be a GitHub login")
        if type(self.draft) is not bool:
            raise ValueError("draft must be boolean")
        for value, field_name in (
            (self.base_ref_name, "base_ref_name"),
            (self.current_default_branch, "current_default_branch"),
        ):
            if (
                not isinstance(value, str)
                or not value
                or any(character.isspace() for character in value)
            ):
                raise ValueError(f"{field_name} must be a safe Git ref")
        for value, field_name in (
            (self.current_default_commit, "current_default_commit"),
            (self.base_commit, "base_commit"),
            (self.head_commit, "head_commit"),
            (self.head_parent_commit, "head_parent_commit"),
            (self.head_tree_sha, "head_tree_sha"),
        ):
            if not isinstance(value, str) or not _COMMIT.fullmatch(value):
                raise ValueError(f"{field_name} must be a full Git object")
        if (
            not isinstance(self.patch_sha256, str)
            or not _SHA256.fullmatch(self.patch_sha256)
        ):
            raise ValueError("patch_sha256 must be a SHA-256 digest")
        if (
            self.binding_sha256 is not None
            and not _SHA256.fullmatch(self.binding_sha256)
        ):
            raise ValueError("binding_sha256 must be a SHA-256 digest")
        for value, field_name in (
            (self.spec_sha256, "spec_sha256"),
            (self.bundle_sha256, "bundle_sha256"),
            (self.evidence_sha256, "evidence_sha256"),
        ):
            if value is not None and not _SHA256.fullmatch(value):
                raise ValueError(f"{field_name} must be a SHA-256 digest")
        if self.marker is not None and not isinstance(self.marker, str):
            raise ValueError("marker must be text")
        paths = tuple(
            _repository_path(path, "changed_path")
            for path in self.changed_paths
        )
        if not paths or len(set(paths)) != len(paths):
            raise ValueError("changed_paths must be non-empty and unique")
        object.__setattr__(self, "changed_paths", paths)
        if not isinstance(self.body, str):
            raise ValueError("body must be text")
        checks = dict(self.checks)
        allowed = {
            "success",
            "failure",
            "pending",
            "cancelled",
            "skipped",
        }
        if any(
            not isinstance(name, str)
            or not name
            or conclusion not in allowed
            for name, conclusion in checks.items()
        ):
            raise ValueError("checks are invalid")
        object.__setattr__(self, "checks", MappingProxyType(checks))
        if self.merge_commit is not None and not _COMMIT.fullmatch(
            self.merge_commit
        ):
            raise ValueError("merge_commit must be a full Git commit")
        for value, field_name in (
            (self.merge_parent_commit, "merge_parent_commit"),
            (self.merge_tree_sha, "merge_tree_sha"),
        ):
            if value is not None and not _COMMIT.fullmatch(value):
                raise ValueError(f"{field_name} must be a full Git object")
        if type(self.merge_reachable_from_default) is not bool:
            raise ValueError(
                "merge_reachable_from_default must be boolean"
            )
        if (
            self.merge_actor is not None
            and re.fullmatch(
                r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}|"
                r"[A-Za-z0-9-]{0,34}\[bot\])",
                self.merge_actor,
            )
            is None
        ):
            raise ValueError("merge_actor must be a GitHub login")
        if (
            self.state is CandidatePullRequestState.MERGED
            and (
                self.merge_commit is None
                or self.merge_parent_commit is None
                or self.merge_tree_sha is None
            )
        ):
            raise ValueError(
                "merged pull requests require exact merge lineage"
            )
        if (
            self.state is not CandidatePullRequestState.MERGED
            and any(
                (
                    self.merge_commit,
                    self.merge_parent_commit,
                    self.merge_tree_sha,
                    self.merge_reachable_from_default,
                    self.merge_actor,
                )
            )
        ):
            raise ValueError("merge lineage is only valid for merged PRs")


class CandidatePullRequestVerificationStatus(StrEnum):
    VERIFIED = "verified"
    PENDING = "pending"
    CLOSED = "closed"
    INVALID = "invalid"


@dataclass(frozen=True)
class CandidatePullRequestVerification:
    status: CandidatePullRequestVerificationStatus
    reason: str | None = None


def candidate_pr_marker(binding: CandidateBinding) -> str:
    return (
        "<!-- foundry-opt:candidate-pr:"
        f"issue-{binding.issue_number}:g{binding.generation}:"
        f"{binding.candidate_id}:{binding.binding_sha256[:20]} -->"
    )


def candidate_pr_body(
    binding: CandidateBinding,
    *,
    worker_issue_number: int,
    required_checks: tuple[str, ...],
) -> str:
    if type(worker_issue_number) is not int or worker_issue_number < 1:
        raise ValueError("worker_issue_number must be positive")
    if len(set(required_checks)) != len(required_checks):
        raise ValueError("required_checks must be unique")
    for check in required_checks:
        _identifier(check, "required check")
    changed = "\n".join(
        f"- `{path.as_posix()}`" for path in binding.changed_paths
    )
    checks = "\n".join(f"- `{check}`" for check in required_checks)
    return "\n".join(
        (
            candidate_pr_marker(binding),
            f"Candidate worker issue: #{worker_issue_number}",
            f"Generation: `{binding.generation}`",
            f"Spec SHA-256: `{binding.spec_sha256}`",
            f"Base commit: `{binding.base_commit}`",
            f"Candidate: `{binding.candidate_id}`",
            f"Draft: `{binding.draft_id}`",
            f"Evidence SHA-256: `{binding.evidence_sha256}`",
            f"Patch SHA-256: `{binding.patch_sha256}`",
            f"Bundle SHA-256: `{binding.bundle_sha256}`",
            f"Expected tree: `{binding.tree_sha}`",
            "",
            "Human selection: merging this pull request is the only "
            "supported candidate-selection action.",
            "Automation must not merge this pull request or dispatch "
            "deployment.",
            "A comment, label, or CLI command does not select this candidate.",
            "",
            "## Required checks",
            checks,
            "",
            "## Exact changed paths",
            changed,
        )
    ) + "\n"


def verify_candidate_pull_request(
    binding: CandidateBinding,
    snapshot: CandidatePullRequestSnapshot,
    *,
    expected_default_branch: str,
    required_checks: tuple[str, ...],
) -> CandidatePullRequestVerification:
    expected_body = candidate_pr_body(
        binding,
        worker_issue_number=snapshot.worker_issue_number,
        required_checks=required_checks,
    )
    required_lines = tuple(
        line for line in expected_body.splitlines() if line
    )
    if snapshot.author != "copilot-swe-agent[bot]":
        return _invalid("untrusted_pr_author")
    if snapshot.draft:
        return _invalid("candidate_pr_is_draft")
    if (
        snapshot.base_ref_name != expected_default_branch
        or snapshot.current_default_branch != expected_default_branch
    ):
        return _invalid("default_branch_changed")
    if (
        snapshot.state is not CandidatePullRequestState.MERGED
        and snapshot.current_default_commit != binding.base_commit
    ):
        return _invalid("default_branch_advanced")
    if snapshot.base_commit != binding.base_commit:
        return _invalid("base_changed")
    if snapshot.head_parent_commit != binding.base_commit:
        return _invalid("head_parent_changed")
    if snapshot.head_tree_sha != binding.tree_sha:
        return _invalid("result_tree_mismatch")
    if snapshot.patch_sha256 != binding.patch_sha256:
        return _invalid("patch_mismatch")
    if (
        snapshot.binding_sha256 is not None
        and snapshot.binding_sha256 != binding.binding_sha256
    ):
        return _invalid("candidate_binding_mismatch")
    if (
        snapshot.spec_sha256 is not None
        and snapshot.spec_sha256 != binding.spec_sha256
    ):
        return _invalid("spec_mismatch")
    if (
        snapshot.bundle_sha256 is not None
        and snapshot.bundle_sha256 != binding.bundle_sha256
    ):
        return _invalid("bundle_mismatch")
    if (
        snapshot.evidence_sha256 is not None
        and snapshot.evidence_sha256 != binding.evidence_sha256
    ):
        return _invalid("evidence_mismatch")
    if (
        snapshot.marker is not None
        and snapshot.marker != candidate_pr_marker(binding)
    ):
        return _invalid("candidate_marker_mismatch")
    if snapshot.changed_paths != binding.changed_paths:
        return _invalid("changed_paths_mismatch")
    if snapshot.state is CandidatePullRequestState.MERGED:
        if snapshot.merge_parent_commit != binding.base_commit:
            return _invalid("merge_base_changed")
        if snapshot.merge_tree_sha != binding.tree_sha:
            return _invalid("merge_tree_mismatch")
        if not snapshot.merge_reachable_from_default:
            return _invalid("merge_not_on_default")
    if any(line not in snapshot.body for line in required_lines):
        return _invalid("candidate_body_mismatch")
    if snapshot.state is CandidatePullRequestState.CLOSED:
        return CandidatePullRequestVerification(
            CandidatePullRequestVerificationStatus.CLOSED,
            "closed_unmerged",
        )
    conclusions = tuple(
        snapshot.checks.get(check) for check in required_checks
    )
    if any(value in {None, "pending"} for value in conclusions):
        return CandidatePullRequestVerification(
            CandidatePullRequestVerificationStatus.PENDING,
            "required_checks_pending",
        )
    if any(value != "success" for value in conclusions):
        return _invalid("required_checks_failed")
    return CandidatePullRequestVerification(
        CandidatePullRequestVerificationStatus.VERIFIED
    )


def _invalid(reason: str) -> CandidatePullRequestVerification:
    return CandidatePullRequestVerification(
        CandidatePullRequestVerificationStatus.INVALID,
        reason,
    )


def _slate_plan_mismatch(
    request: CandidateSlateRequest,
    state: CampaignState,
    plan: CandidateSlatePlan,
) -> str | None:
    if plan.issue_number != request.issue_number:
        return "candidate_issue_stale"
    if plan.generation != state.generation:
        return "candidate_generation_stale"
    if plan.spec_sha256 != state.spec_sha256:
        return "candidate_spec_stale"
    return None


def _worker_intent(record: OutboxRecord) -> ApplierWorkerIntent:
    payload = record.payload
    if (
        record.kind != "applier_worker_issue_planned"
        or payload.get("effect_id") != record.record_id
        or payload.get("effect_kind") != "applier_worker_issue"
        or payload.get("specialist") != "foundry-candidate-applier"
        or payload.get("work_kind") != "apply_exact_candidate"
    ):
        raise ValueError("worker intent metadata is invalid")
    binding = CandidateBinding(
        issue_number=int(payload["issue_number"]),
        generation=record.generation,
        spec_sha256=str(payload["spec_sha256"]),
        base_commit=str(payload["base_commit"]),
        candidate_id=str(payload["candidate_id"]),
        draft_id=str(payload["draft_id"]),
        evidence_sha256=str(payload["evidence_sha256"]),
        patch_sha256=str(payload["patch_sha256"]),
        bundle_sha256=str(payload["bundle_sha256"]),
        tree_sha=str(payload["tree_sha"]),
        allowed_paths=tuple(
            Path(str(path)) for path in payload["allowed_paths"]
        ),
        changed_paths=tuple(
            Path(str(path)) for path in payload["changed_paths"]
        ),
    )
    if (
        payload.get("binding_sha256") != binding.binding_sha256
        or payload.get("marker") != candidate_pr_marker(binding)
    ):
        raise ValueError("worker intent binding is invalid")
    return ApplierWorkerIntent(record.record_id, binding)


def applier_worker_intent(record: OutboxRecord) -> ApplierWorkerIntent:
    """Parse one persisted applier intent for a transport bridge."""

    return _worker_intent(record)


def _completed_worker_bindings(
    snapshot: StateRefSnapshot,
) -> tuple[tuple[CandidateBinding, int], ...]:
    planned = tuple(
        record
        for record in snapshot.outbox
        if (
            record.kind == "applier_worker_issue_planned"
            and record.generation == snapshot.state.generation
        )
    )
    if not planned:
        raise ValueError("candidate worker bindings are unavailable")
    completed: list[tuple[CandidateBinding, int]] = []
    for record in planned:
        intent = _worker_intent(record)
        success_id = f"{record.record_id}-succeeded"
        successes = tuple(
            item
            for item in snapshot.outbox
            if item.record_id == success_id
        )
        if not successes:
            raise _WorkerAcknowledgementPending
        if len(successes) != 1:
            raise ValueError("candidate worker issue is not acknowledged")
        success = successes[0]
        payload = success.payload
        if (
            success.kind != "applier_worker_issue_succeeded"
            or payload.get("effect_id") != record.record_id
            or payload.get("candidate_id")
            != intent.binding.candidate_id
            or payload.get("binding_sha256")
            != intent.binding.binding_sha256
            or payload.get("created") is not True
            or payload.get("assigned") is not True
            or type(payload.get("worker_issue_number")) is not int
            or int(payload["worker_issue_number"]) < 1
        ):
            raise ValueError("candidate worker acknowledgement is invalid")
        completed.append(
            (
                intent.binding,
                int(payload["worker_issue_number"]),
            )
        )
    return tuple(completed)


class _WorkerAcknowledgementPending(RuntimeError):
    pass


def _observed_pull_requests(
    snapshot: StateRefSnapshot,
    provided: tuple[CandidatePullRequestReference, ...],
) -> tuple[CandidatePullRequestReference, ...]:
    observed = {
        item.pull_request_number: item
        for item in provided
    }
    worker_issues = {
        str(record.payload["binding_sha256"]): int(
            record.payload["worker_issue_number"]
        )
        for record in snapshot.outbox
        if (
            record.kind == "applier_worker_issue_succeeded"
            and record.generation == snapshot.state.generation
            and isinstance(record.payload.get("binding_sha256"), str)
            and type(record.payload.get("worker_issue_number")) is int
        )
    }
    for event in snapshot.inbox:
        if (
            event.generation != snapshot.state.generation
            or event.kind
            not in {
                EventKind.CANDIDATE_PR_OPENED,
                EventKind.CANDIDATE_PR_SYNCHRONIZED,
                EventKind.CANDIDATE_PR_EDITED,
                EventKind.CANDIDATE_PR_CLOSED,
                EventKind.CANDIDATE_PR_MERGED,
            }
        ):
            continue
        observed[int(event.payload["pull_request_number"])] = (
            CandidatePullRequestReference(
                int(event.payload["pull_request_number"]),
                str(event.payload["binding_sha256"]),
                worker_issues.get(str(event.payload["binding_sha256"])),
            )
        )
    return tuple(
        observed[number] for number in sorted(observed)
    )


def candidate_worker_bindings(
    snapshot: StateRefSnapshot,
) -> tuple[CandidateBinding, ...]:
    return tuple(
        _worker_intent(record).binding
        for record in snapshot.outbox
        if (
            record.kind == "applier_worker_issue_planned"
            and record.generation == snapshot.state.generation
        )
    )


def _worker_issue_body(
    record: OutboxRecord,
    intent: ApplierWorkerIntent,
) -> str:
    payload = record.payload
    checks = "\n".join(
        f"- `{check}`" for check in payload["required_checks"]
    )
    return "\n".join(
        (
            candidate_pr_marker(intent.binding),
            "Apply only the exact steward-attested candidate patch.",
            f"Root issue: #{intent.binding.issue_number}",
            "State ref: "
            f"`foundry-opt/state/issue-{intent.binding.issue_number}`",
            f"Generation: `{intent.binding.generation}`",
            f"Candidate: `{intent.binding.candidate_id}`",
            f"Spec SHA-256: `{intent.binding.spec_sha256}`",
            f"Base commit: `{intent.binding.base_commit}`",
            f"Patch object: `{payload['patch_path']}`",
            f"Evidence object: `{payload['evidence_path']}`",
            f"Attestation object: `{payload['attestation_path']}`",
            f"Expected tree: `{intent.binding.tree_sha}`",
            "",
            "Open a native Copilot pull request containing no additional "
            "edits. GitHub Actions must not create the pull request.",
            "The PR body must identify this worker issue, repeat every exact "
            "binding above, list the exact changed paths, and include:",
            "",
            "Human selection: merging this pull request is the only "
            "supported candidate-selection action.",
            "Automation must not merge this pull request or dispatch "
            "deployment.",
            "A comment, label, or CLI command does not select this candidate.",
            "",
            "Required checks:",
            checks,
        )
    ) + "\n"


def _pr_observation(
    snapshot: StateRefSnapshot,
    binding: CandidateBinding,
    pull_request: CandidatePullRequestSnapshot,
    verification: CandidatePullRequestVerification,
) -> OutboxRecord:
    reason = verification.reason
    if (
        verification.status
        is CandidatePullRequestVerificationStatus.VERIFIED
    ):
        kind = "candidate_pr_verified"
        reason = "exact_candidate_verified"
    elif (
        verification.status
        is CandidatePullRequestVerificationStatus.PENDING
    ):
        kind = "candidate_pr_verification_pending"
    elif (
        verification.status
        is CandidatePullRequestVerificationStatus.CLOSED
    ):
        kind = "candidate_pr_closed_observed"
    else:
        kind = "candidate_pr_reject_planned"
    if reason is None:
        raise ValueError("candidate PR observation requires a reason")
    digest = hashlib.sha256(
        (
            f"{kind}:{pull_request.pull_request_number}:"
            f"{pull_request.head_commit}:{reason}:"
            f"{binding.binding_sha256}"
        ).encode("ascii")
    ).hexdigest()[:16]
    payload: dict[str, object] = {
        "binding_sha256": binding.binding_sha256,
        "candidate_id": binding.candidate_id,
        "head_commit": pull_request.head_commit,
        "issue_number": binding.issue_number,
        "pull_request_number": pull_request.pull_request_number,
        "reason": reason,
        "tree_sha": pull_request.head_tree_sha,
        "worker_issue_number": pull_request.worker_issue_number,
    }
    if kind == "candidate_pr_reject_planned":
        payload["marker"] = candidate_pr_marker(binding)
    return OutboxRecord(
        record_id=(
            f"pr-observation-{snapshot.state.generation}-{digest}"
        ),
        kind=kind,
        generation=snapshot.state.generation,
        sequence=snapshot.state.sequence,
        payload=payload,
    )


def _supersession_effects(
    snapshot: StateRefSnapshot,
    binding: CandidateBinding,
    pull_request: CandidatePullRequestSnapshot | None,
) -> tuple[OutboxRecord, ...]:
    common = {
        "candidate_id": binding.candidate_id,
        "issue_number": binding.issue_number,
        "marker": candidate_pr_marker(binding),
        "reason": "candidate_selected_elsewhere",
    }
    issue_number = (
        pull_request.worker_issue_number
        if pull_request is not None
        else None
    )
    records: list[OutboxRecord] = []
    if issue_number is not None:
        records.append(
            OutboxRecord(
                record_id=(
                    f"supersede-issue-{snapshot.state.generation}-"
                    f"{binding.candidate_id}-{issue_number}"
                ),
                kind="candidate_issue_supersede_planned",
                generation=snapshot.state.generation,
                sequence=snapshot.state.sequence,
                payload={
                    **common,
                    "worker_issue_number": issue_number,
                },
            )
        )
    if pull_request is not None:
        records.append(
            OutboxRecord(
                record_id=(
                    f"supersede-pr-{snapshot.state.generation}-"
                    f"{binding.candidate_id}-"
                    f"{pull_request.pull_request_number}"
                ),
                kind="candidate_pr_supersede_planned",
                generation=snapshot.state.generation,
                sequence=snapshot.state.sequence,
                payload={
                    **common,
                    "pull_request_number": (
                        pull_request.pull_request_number
                    ),
                },
            )
        )
    return tuple(records)


def _supersession_binding(
    record: OutboxRecord,
) -> tuple[str, int, bool, str]:
    payload = record.payload
    if (
        not isinstance(payload.get("reason"), str)
        or payload.get("issue_number") is None
        or not isinstance(payload.get("marker"), str)
    ):
        raise ValueError("supersession metadata is invalid")
    marker = str(payload["marker"])
    if record.kind == "candidate_issue_supersede_planned":
        number = payload.get("worker_issue_number")
        is_issue = True
    elif record.kind == "candidate_pr_supersede_planned":
        number = payload.get("pull_request_number")
        is_issue = False
    elif record.kind == "candidate_pr_reject_planned":
        number = payload.get("pull_request_number")
        is_issue = False
    else:
        raise ValueError("supersession kind is invalid")
    if type(number) is not int or number < 1:
        raise ValueError("supersession number is invalid")
    return marker, number, is_issue, str(payload["reason"])


def _single_record(
    snapshot: StateRefSnapshot,
    kind: str,
    generation: int,
    *,
    candidate_id: str | None = None,
) -> OutboxRecord:
    records = tuple(
        record
        for record in snapshot.outbox
        if (
            record.kind == kind
            and record.generation == generation
            and (
                candidate_id is None
                or record.payload.get("candidate_id") == candidate_id
            )
        )
    )
    if len(records) != 1:
        raise ValueError(f"{kind} checkpoint is missing or ambiguous")
    return records[0]


def _numeric_mapping(value: object, field_name: str) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an aggregate mapping")
    metrics: dict[str, float] = {}
    for name, score in value.items():
        _identifier(str(name), f"{field_name} name")
        if (
            not isinstance(score, (int, float))
            or isinstance(score, bool)
        ):
            raise ValueError(f"{field_name} must contain numbers")
        metrics[str(name)] = float(score)
    return metrics


def _safe_artifact(root: Path, relative: str) -> Path:
    path = _repository_path(Path(relative), "artifact path")
    candidate = root.resolve() / path
    current = root.resolve()
    for part in path.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("artifact path contains a symbolic link")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise ValueError("artifact path escapes the repository")
    return resolved


def _load_candidate(
    root: Path,
    snapshot: StateRefSnapshot,
    plan: CandidateSlatePlan,
    candidate_id: str,
    expected_evidence_sha256: str,
    baseline_metrics: Mapping[str, float],
) -> dict[str, object]:
    record = _single_record(
        snapshot,
        "candidate_attestation",
        plan.generation,
        candidate_id=candidate_id,
    )
    payload = dict(record.payload)
    if (
        payload.get("issue_number") != plan.issue_number
        or payload.get("candidate_id") != candidate_id
        or payload.get("spec_sha256") != plan.spec_sha256
        or payload.get("base_commit") != plan.base_commit
        or payload.get("eligible") is not True
        or payload.get("evidence_sha256") != expected_evidence_sha256
    ):
        raise ValueError("candidate attestation binding is invalid")
    attestation_sha256 = payload.get("attestation_sha256")
    if (
        not isinstance(attestation_sha256, str)
        or not _SHA256.fullmatch(attestation_sha256)
        or attestation_sha256
        != _document_sha256(
            {
                key: value
                for key, value in payload.items()
                if key != "attestation_sha256"
            }
        )
    ):
        raise ValueError("candidate attestation hash is invalid")
    binding = CandidateBinding(
        issue_number=plan.issue_number,
        generation=plan.generation,
        spec_sha256=plan.spec_sha256,
        base_commit=plan.base_commit,
        candidate_id=candidate_id,
        draft_id=str(payload["draft_id"]),
        evidence_sha256=str(payload["evidence_sha256"]),
        patch_sha256=str(payload["patch_sha256"]),
        bundle_sha256=str(payload["bundle_sha256"]),
        tree_sha=str(payload["tree_sha"]),
        allowed_paths=tuple(
            Path(str(path)) for path in payload["allowed_paths"]
        ),
        changed_paths=tuple(
            Path(str(path)) for path in payload["changed_paths"]
        ),
    )
    candidate_object = _state_object(
        snapshot,
        f"objects/candidates/g{plan.generation}-{candidate_id}.json",
    )
    evidence_object = _state_object(
        snapshot,
        f"objects/evidence/{binding.evidence_sha256}.json",
    )
    patch_object = _state_object(
        snapshot,
        f"objects/patches/{binding.patch_sha256}.patch",
    )
    if candidate_object is not None:
        try:
            durable_attestation = json.loads(candidate_object.content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("durable candidate attestation is invalid") from error
        if durable_attestation != payload:
            raise ValueError("durable candidate attestation changed")
    patch = (
        patch_object.content
        if patch_object is not None
        else _safe_artifact(root, str(payload["patch_path"])).read_bytes()
    )
    evidence = (
        evidence_object.content
        if evidence_object is not None
        else _safe_artifact(
            root,
            str(payload["evidence_path"]),
        ).read_bytes()
    )
    if hashlib.sha256(patch).hexdigest() != binding.patch_sha256:
        raise ValueError("candidate patch hash changed")
    if hashlib.sha256(evidence).hexdigest() != binding.evidence_sha256:
        raise ValueError("candidate evidence hash changed")
    try:
        evidence_document = json.loads(evidence)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("candidate evidence is invalid") from error
    if (
        not isinstance(evidence_document, dict)
        or evidence_document.get("campaign_id") != plan.campaign_id
    ):
        raise ValueError("candidate evidence lineage changed")
    metrics = _numeric_mapping(payload.get("metrics"), "candidate metrics")
    if set(metrics) != set(baseline_metrics):
        raise ValueError("candidate metrics do not match the baseline")
    candidate_object = candidate_object or StateObject(
        f"objects/candidates/g{plan.generation}-{candidate_id}.json",
        _canonical_json(payload),
    )
    evidence_object = evidence_object or StateObject(
        f"objects/evidence/{binding.evidence_sha256}.json",
        evidence,
    )
    patch_object = patch_object or StateObject(
        f"objects/patches/{binding.patch_sha256}.patch",
        patch,
    )
    return {
        "attestation_path": candidate_object.path,
        "binding": binding,
        "evidence_path": evidence_object.path,
        "metrics": metrics,
        "objects": (
            candidate_object,
            evidence_object,
            patch_object,
        ),
        "patch_path": patch_object.path,
    }


def _state_object(
    snapshot: StateRefSnapshot,
    path: str,
) -> StateObject | None:
    matches = tuple(item for item in snapshot.objects if item.path == path)
    if len(matches) > 1:
        raise ValueError("durable candidate object is ambiguous")
    return matches[0] if matches else None


def _new_slate_objects(
    snapshot: StateRefSnapshot,
    candidates: tuple[StateObject, ...],
) -> tuple[StateObject, ...]:
    known = {item.path: item for item in snapshot.objects}
    additions: dict[str, StateObject] = {}
    for item in candidates:
        existing = known.get(item.path) or additions.get(item.path)
        if existing is not None:
            if existing.content != item.content:
                raise ValueError("candidate state object content changed")
            continue
        additions[item.path] = item
    return tuple(
        additions[path] for path in sorted(additions)
    )


def _rank_candidates(
    candidates: tuple[dict[str, object], ...],
    baseline: Mapping[str, float],
    policy: EvaluationPolicy,
) -> tuple[dict[str, object], ...]:
    objective_names = tuple(
        metric.name
        for metric in policy.metrics
        if not metric.hard_guardrail
    )
    enriched: list[dict[str, object]] = []
    for candidate in candidates:
        metrics = candidate["metrics"]
        assert isinstance(metrics, dict)
        deltas = {
            metric.name: metric.improvement(
                baseline[metric.name],
                metrics[metric.name],
            )
            for metric in policy.metrics
        }
        guardrails = {
            metric.name: (
                "pass"
                if metric.passes(metrics[metric.name])
                else "fail"
            )
            for metric in policy.metrics
            if metric.hard_guardrail
        }
        score = tuple(deltas[name] for name in objective_names)
        enriched.append(
            {
                **candidate,
                "deltas": deltas,
                "guardrails": guardrails,
                "score": score,
            }
        )
    enriched.sort(
        key=lambda item: (
            tuple(-value for value in item["score"]),
            item["binding"].candidate_id,
        )
    )
    rank = 0
    prior: tuple[float, ...] | None = None
    for index, candidate in enumerate(enriched, 1):
        score = candidate["score"]
        if score != prior:
            rank = index
            prior = score
        candidate["rank"] = rank
    return tuple(enriched)


def _applier_outbox(
    snapshot: StateRefSnapshot,
    plan: CandidateSlatePlan,
    candidate: Mapping[str, object],
) -> OutboxRecord:
    binding = candidate["binding"]
    assert isinstance(binding, CandidateBinding)
    intent = ApplierWorkerIntent(
        effect_id=(
            f"applier-{plan.generation}-{binding.candidate_id}-"
            f"{binding.binding_sha256[:16]}"
        ),
        binding=binding,
    )
    return OutboxRecord(
        record_id=intent.effect_id,
        kind="applier_worker_issue_planned",
        generation=snapshot.state.generation,
        sequence=snapshot.state.sequence,
        payload={
            "allowed_paths": [
                path.as_posix() for path in binding.allowed_paths
            ],
            "attestation_path": candidate["attestation_path"],
            "base_commit": binding.base_commit,
            "binding_sha256": binding.binding_sha256,
            "bundle_sha256": binding.bundle_sha256,
            "candidate_id": binding.candidate_id,
            "changed_paths": [
                path.as_posix() for path in binding.changed_paths
            ],
            "draft_id": binding.draft_id,
            "effect_id": intent.effect_id,
            "effect_kind": "applier_worker_issue",
            "evidence_path": candidate["evidence_path"],
            "evidence_sha256": binding.evidence_sha256,
            "issue_number": binding.issue_number,
            "marker": candidate_pr_marker(binding),
            "patch_path": candidate["patch_path"],
            "patch_sha256": binding.patch_sha256,
            "required_checks": list(plan.required_checks),
            "spec_sha256": binding.spec_sha256,
            "specialist": "foundry-candidate-applier",
            "tree_sha": binding.tree_sha,
            "work_kind": "apply_exact_candidate",
        },
    )


def _dashboard_outbox(
    snapshot: StateRefSnapshot,
    plan: CandidateSlatePlan,
    candidates: tuple[dict[str, object], ...],
    baseline: Mapping[str, float],
) -> OutboxRecord:
    rows = []
    for candidate in candidates:
        binding = candidate["binding"]
        assert isinstance(binding, CandidateBinding)
        rows.append(
            {
                "candidate_id": binding.candidate_id,
                "deltas": candidate["deltas"],
                "draft_id": binding.draft_id,
                "evidence_sha256": binding.evidence_sha256,
                "evidence_url": (
                    f"https://github.com/{plan.repository}/blob/"
                    f"foundry-opt/state/issue-{plan.issue_number}/"
                    f"{candidate['evidence_path']}"
                ),
                "guardrails": candidate["guardrails"],
                "metrics": candidate["metrics"],
                "rank": candidate["rank"],
            }
        )
    digest = _document_sha256(
        {
            "baseline": dict(baseline),
            "candidates": rows,
            "generation": plan.generation,
            "spec_sha256": plan.spec_sha256,
        }
    )
    return OutboxRecord(
        record_id=(
            f"slate-dashboard-{plan.generation}-{digest[:16]}"
        ),
        kind="candidate_slate_dashboard",
        generation=snapshot.state.generation,
        sequence=snapshot.state.sequence,
        payload={
            "baseline_metrics": dict(baseline),
            "candidate_slate": rows,
            "disposition": "wait",
            "issue_number": plan.issue_number,
            "next_action": "merge_exactly_one_candidate_pr",
            "phase": CampaignPhase.AWAITING_SELECTION.value,
            "spec_sha256": plan.spec_sha256,
            "status": "waiting",
        },
    )


def _canonical_json(document: object) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _document_sha256(document: object) -> str:
    return hashlib.sha256(
        json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
