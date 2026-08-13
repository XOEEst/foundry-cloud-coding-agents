from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping, TYPE_CHECKING

from foundry_opt.evaluation import EvaluationPolicy
from foundry_opt.orchestration.candidate_experiments import (
    CandidateExperimentRequest,
    CandidateExperimentResult,
)
from foundry_opt.orchestration.public_evidence import (
    FoundryOperation,
    OptimizationReport,
)

if TYPE_CHECKING:
    from foundry_opt.orchestration.candidate_search import (
        CandidateSearchSummary,
    )
    from foundry_opt.orchestration.workspace_coordinator import (
        WorkspaceCandidateCoordinator,
    )
    from foundry_opt.orchestration.workspace_runtime import (
        WorkspacePullRequestAdapter,
        WorkspaceStore,
    )
    from foundry_opt.orchestration.workspace_store import (
        WorkspaceSnapshot,
        WorkspaceUpdate,
    )


class WorkspacePhase(StrEnum):
    SPECIFICATION = "specification"
    EVALUATING = "evaluating"
    AWAITING_SELECTION = "awaiting_selection"
    DEPLOYMENT = "deployment"
    RETENTION = "retention"
    COMPLETED = "completed"


class WorkspaceTrigger(StrEnum):
    ISSUE_CREATED = "issue_created"
    CONTINUE = "continue"
    EXPERIMENTS_COMPLETED = "experiments_completed"
    PULL_REQUEST_MERGED = "pull_request_merged"
    DEPLOYMENT_COMPLETED = "deployment_completed"
    RETENTION_COMPLETED = "retention_completed"


class WorkspaceNextActionKind(StrEnum):
    REVIEW_SPECIFICATION = "review_specification"
    ESTABLISH_BASELINE = "establish_baseline"
    AWAIT_TRUSTED_ACTIONS_RESULT = "await_trusted_actions_result"
    DESIGN_CANDIDATES = "design_candidates"
    RUN_CANDIDATE_EXPERIMENTS = "run_candidate_experiments"
    MERGE_WORKSPACE_PULL_REQUEST = "merge_workspace_pull_request"
    DEPLOY_SELECTED_CANDIDATE = "deploy_selected_candidate"
    COMPLETE_RETENTION = "complete_retention"
    NONE = "none"


@dataclass(frozen=True)
class WorkspaceNextAction:
    kind: WorkspaceNextActionKind
    issue_number: int
    workspace_pull_request_number: int | None
    trigger: WorkspaceTrigger | None

    def to_dict(self) -> dict[str, int | str | None]:
        return {
            "issue_number": self.issue_number,
            "kind": self.kind.value,
            "trigger": self.trigger.value if self.trigger is not None else None,
            "workspace_pull_request_number": (
                self.workspace_pull_request_number
            ),
        }


@dataclass(frozen=True)
class WorkspaceIssue:
    number: int
    title: str
    body: str
    base_commit: str


@dataclass(frozen=True)
class WorkspaceCandidate:
    experiment: CandidateExperimentRequest
    experiment_result: CandidateExperimentResult
    exact_patch: bytes
    summary: str
    changed_paths: tuple[str, ...]
    validation: tuple[str, ...]
    expected_tree: str

    def __post_init__(self) -> None:
        if not isinstance(self.exact_patch, bytes) or not self.exact_patch:
            raise ValueError("workspace candidate patch is required")
        if (
            hashlib.sha256(self.exact_patch).hexdigest()
            != self.experiment.patch_sha256
        ):
            raise ValueError("workspace candidate patch binding changed")
        result = self.experiment_result
        if (
            result.candidate_id != self.experiment.candidate_id
            or result.bundle_sha256 != self.experiment.bundle_sha256
            or result.evidence_sha256 != self.experiment.evidence_sha256
            or (
                result.idempotency_key is not None
                and result.idempotency_key
                != self.experiment.idempotency_key
            )
        ):
            raise ValueError("workspace candidate result binding changed")
        if (
            not isinstance(self.summary, str)
            or not self.summary.strip()
            or len(self.summary) > 4096
        ):
            raise ValueError("workspace candidate summary is invalid")
        if re.fullmatch(r"[0-9a-f]{40}", self.expected_tree) is None:
            raise ValueError("workspace candidate expected tree is invalid")
        if any(
            not isinstance(path, str)
            or not path
            or path.startswith(("/", "\\"))
            or ".." in Path(path).parts
            for path in self.changed_paths
        ):
            raise ValueError("workspace candidate changed paths are invalid")
        if any(
            not isinstance(item, str) or not item.strip()
            for item in self.validation
        ):
            raise ValueError("workspace candidate validation is invalid")
        object.__setattr__(self, "changed_paths", tuple(self.changed_paths))
        object.__setattr__(self, "validation", tuple(self.validation))

    @property
    def foundry_operations(self) -> tuple[FoundryOperation, ...]:
        result = self.experiment_result
        return tuple(
            FoundryOperation(
                kind=kind,
                identifier=identifier,
                url="https://ai.azure.com",
                status="completed",
            )
            for kind, identifier in (
                ("draft", result.draft_id),
                ("evaluation", result.evaluation_id),
                ("run", result.run_id),
            )
        )


@dataclass(frozen=True)
class WorkspaceCandidateProposal:
    candidate_id: str
    exact_patch: bytes
    summary: str
    mutation_class: str

    def __post_init__(self) -> None:
        if re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
            self.candidate_id,
        ) is None:
            raise ValueError("workspace proposal candidate is invalid")
        if (
            not isinstance(self.exact_patch, bytes)
            or not self.exact_patch
            or re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
                self.mutation_class,
            )
            is None
        ):
            raise ValueError("workspace proposal mutation is invalid")
        if (
            not isinstance(self.summary, str)
            or not self.summary.strip()
            or len(self.summary) > 4096
        ):
            raise ValueError("workspace proposal metadata is invalid")

    @property
    def patch_sha256(self) -> str:
        return hashlib.sha256(self.exact_patch).hexdigest()


@dataclass(frozen=True)
class WorkspaceReportContext:
    baseline_metrics: Mapping[str, float]
    policy: EvaluationPolicy
    sample_count: int
    split: str
    spec_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "baseline_metrics",
            MappingProxyType(dict(self.baseline_metrics)),
        )
        if type(self.sample_count) is not int or self.sample_count < 0:
            raise ValueError("workspace report sample count is invalid")
        if not isinstance(self.split, str) or not self.split.strip():
            raise ValueError("workspace report split is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", self.spec_sha256) is None:
            raise ValueError("workspace report spec digest is invalid")


@dataclass(frozen=True)
class WorkspaceSelectionRequest:
    issue: WorkspaceIssue
    candidates: tuple[WorkspaceCandidate, ...]
    experiments: tuple[CandidateSearchSummary, ...]
    report_context: WorkspaceReportContext


@dataclass(frozen=True)
class WorkspaceSelectionDecision:
    selected_candidate_id: str
    eligible_candidate_ids: tuple[str, ...]
    recommendation: str
    rejection_reasons: Mapping[str, str]
    required_checks: Mapping[str, str]

    def __post_init__(self) -> None:
        eligible = tuple(self.eligible_candidate_ids)
        if (
            not eligible
            or len(eligible) != len(set(eligible))
            or self.selected_candidate_id not in eligible
        ):
            raise ValueError("workspace selection eligibility is invalid")
        if (
            not isinstance(self.recommendation, str)
            or not self.recommendation.strip()
        ):
            raise ValueError("workspace selection recommendation is invalid")
        object.__setattr__(self, "eligible_candidate_ids", eligible)
        object.__setattr__(
            self,
            "rejection_reasons",
            MappingProxyType(dict(self.rejection_reasons)),
        )
        object.__setattr__(
            self,
            "required_checks",
            MappingProxyType(dict(self.required_checks)),
        )


@dataclass(frozen=True)
class WorkspaceOperation:
    trigger: WorkspaceTrigger
    operation_id: str
    workspace_pull_request_number: int
    candidate_id: str
    patch_sha256: str
    bundle_sha256: str
    evidence_sha256: str
    predecessor_operation_id: str | None = None

    def __post_init__(self) -> None:
        if self.trigger not in {
            WorkspaceTrigger.DEPLOYMENT_COMPLETED,
            WorkspaceTrigger.RETENTION_COMPLETED,
        }:
            raise ValueError("workspace operation trigger is invalid")
        if (
            re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,255}",
                self.operation_id,
            )
            is None
        ):
            raise ValueError("workspace operation ID is invalid")
        if (
            type(self.workspace_pull_request_number) is not int
            or self.workspace_pull_request_number < 1
        ):
            raise ValueError("workspace operation pull request is invalid")
        if re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
            self.candidate_id,
        ) is None:
            raise ValueError("workspace operation candidate is invalid")
        for value, name in (
            (self.patch_sha256, "patch"),
            (self.bundle_sha256, "bundle"),
            (self.evidence_sha256, "evidence"),
        ):
            if re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError(
                    f"workspace operation {name} digest is invalid"
                )
        if (
            self.trigger is WorkspaceTrigger.RETENTION_COMPLETED
            and (
                self.predecessor_operation_id is None
                or re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,255}",
                    self.predecessor_operation_id,
                )
                is None
            )
        ):
            raise ValueError(
                "workspace retention predecessor operation is invalid"
            )


@dataclass(frozen=True)
class WorkspaceRequest:
    repository_root: Path
    issue: WorkspaceIssue
    trigger: WorkspaceTrigger
    workspace_pull_request: WorkspacePullRequest | None = None
    candidates: tuple[WorkspaceCandidate, ...] = ()
    report_context: WorkspaceReportContext | None = None
    operation: WorkspaceOperation | None = None


@dataclass(frozen=True)
class WorkspacePullRequest:
    number: int | None
    issue_number: int
    branch: str
    title: str
    draft: bool
    reuse_existing: bool
    base_commit: str


@dataclass(frozen=True)
class WorkspaceResult:
    phase: WorkspacePhase
    workspace_pull_request: WorkspacePullRequest | None
    planned_effect_kinds: tuple[str, ...]
    recorded: bool = False
    issue_status_projection_intent: (
        WorkspaceIssueStatusProjectionIntent | None
    ) = None
    next_action: WorkspaceNextAction | None = None
    report: OptimizationReport | None = None

    def to_dict(self) -> dict[str, Any]:
        pull_request = self.workspace_pull_request
        return {
            "issue_number": (
                pull_request.issue_number
                if pull_request is not None
                else None
            ),
            "issue_status_projection_intent": (
                self.issue_status_projection_intent.to_dict()
                if self.issue_status_projection_intent is not None
                else None
            ),
            "phase": self.phase.value,
            "next_action": (
                self.next_action.to_dict()
                if self.next_action is not None
                else None
            ),
            "planned_effect_kinds": list(self.planned_effect_kinds),
            "recorded": self.recorded,
            "report": (
                _report_to_dict(self.report)
                if self.report is not None
                else None
            ),
            "workspace_pull_request": (
                {
                    "base_commit": pull_request.base_commit,
                    "branch": pull_request.branch,
                    "draft": pull_request.draft,
                    "issue_number": pull_request.issue_number,
                    "number": pull_request.number,
                    "reuse_existing": pull_request.reuse_existing,
                    "title": pull_request.title,
                }
                if pull_request is not None
                else None
            ),
        }


@dataclass(frozen=True)
class WorkspaceIssueStatusProjectionIntent:
    issue_number: int
    phase: WorkspacePhase
    workspace_pull_request_number: int

    def __post_init__(self) -> None:
        if type(self.issue_number) is not int or self.issue_number < 1:
            raise ValueError("workspace projection issue is invalid")
        if (
            type(self.workspace_pull_request_number) is not int
            or self.workspace_pull_request_number < 1
        ):
            raise ValueError(
                "workspace projection pull request is invalid"
            )

    def to_dict(self) -> dict[str, int | str]:
        return {
            "issue_number": self.issue_number,
            "kind": "workspace_issue_status",
            "phase": self.phase.value,
            "status": "workspace_ready",
            "workspace_pull_request_number": (
                self.workspace_pull_request_number
            ),
        }


class OptimizationWorkspace:
    def __init__(
        self,
        *,
        store: WorkspaceStore | None = None,
        pull_requests: WorkspacePullRequestAdapter | None = None,
        candidate_coordinator: WorkspaceCandidateCoordinator | None = None,
    ) -> None:
        from foundry_opt.orchestration.workspace_runtime import (
            PlanningWorkspacePullRequests,
        )
        from foundry_opt.orchestration.workspace_store import (
            InMemoryWorkspaceStore,
        )

        self._store = (
            store if store is not None else InMemoryWorkspaceStore()
        )
        self._pull_requests = (
            pull_requests
            if pull_requests is not None
            else PlanningWorkspacePullRequests()
        )
        self._candidate_coordinator = candidate_coordinator

    def advance(self, request: WorkspaceRequest) -> WorkspaceResult:
        issue = request.issue
        snapshot = self._store.load(issue.number)
        if request.trigger is WorkspaceTrigger.EXPERIMENTS_COMPLETED:
            if snapshot is None or snapshot.phase not in {
                WorkspacePhase.SPECIFICATION,
                WorkspacePhase.EVALUATING,
                WorkspacePhase.AWAITING_SELECTION,
            }:
                raise ValueError("workspace transition is not allowed")
            if self._candidate_coordinator is None:
                raise ValueError(
                    "workspace candidate coordinator is not configured"
                )
            pull_request = self._resolved_pull_request(
                issue,
                request.workspace_pull_request,
                snapshot.workspace_pull_request_number,
                selected=False,
            )
            outcome = self._candidate_coordinator.complete(
                request=request,
                pull_request=pull_request,
            )
            return WorkspaceResult(
                phase=WorkspacePhase.AWAITING_SELECTION,
                workspace_pull_request=outcome.workspace_pull_request,
                planned_effect_kinds=(
                    "candidate_experiments",
                    "workspace_state_commit",
                    "workspace_pr_finalize",
                ),
                recorded=outcome.recorded,
                issue_status_projection_intent=(
                    self._projection(
                        issue.number,
                        WorkspacePhase.AWAITING_SELECTION,
                        outcome.workspace_pull_request.number,
                    )
                ),
                next_action=self._next_action(
                    issue.number,
                    WorkspacePhase.AWAITING_SELECTION,
                    outcome.workspace_pull_request.number,
                ),
                report=outcome.report,
            )
        if request.trigger in {
            WorkspaceTrigger.PULL_REQUEST_MERGED,
            WorkspaceTrigger.DEPLOYMENT_COMPLETED,
            WorkspaceTrigger.RETENTION_COMPLETED,
        }:
            return self._advance_lifecycle(request, snapshot)
        if request.trigger not in {
            WorkspaceTrigger.ISSUE_CREATED,
            WorkspaceTrigger.CONTINUE,
        }:
            raise ValueError("workspace transition is not allowed")
        if (
            request.trigger is WorkspaceTrigger.CONTINUE
            and snapshot is not None
            and snapshot.phase
            in {
                WorkspacePhase.AWAITING_SELECTION,
                WorkspacePhase.DEPLOYMENT,
                WorkspacePhase.RETENTION,
                WorkspacePhase.COMPLETED,
            }
        ):
            pull_request = self._resolved_pull_request(
                issue,
                None,
                snapshot.workspace_pull_request_number,
                selected=True,
            )
            return WorkspaceResult(
                phase=snapshot.phase,
                workspace_pull_request=pull_request,
                planned_effect_kinds=(),
                recorded=False,
                issue_status_projection_intent=self._projection(
                    issue.number,
                    snapshot.phase,
                    pull_request.number,
                ),
                next_action=self._next_action(
                    issue.number,
                    snapshot.phase,
                    pull_request.number,
                ),
            )
        pull_request = request.workspace_pull_request
        if (
            request.trigger is WorkspaceTrigger.CONTINUE
            and snapshot is None
            and pull_request is None
        ):
            raise ValueError(
                "workspace pull request is required to continue"
            )
        expected_number = (
            snapshot.workspace_pull_request_number
            if snapshot is not None
            and snapshot.workspace_pull_request_number is not None
            else pull_request.number
            if pull_request is not None
            else None
        )
        if pull_request is not None:
            self._validate_pull_request(
                issue=issue,
                pull_request=pull_request,
                expected_number=expected_number,
            )
        if pull_request is None:
            pull_request = WorkspacePullRequest(
                number=(
                    snapshot.workspace_pull_request_number
                    if snapshot is not None
                    else None
                ),
                issue_number=issue.number,
                branch=f"foundry-opt/workspace/issue-{issue.number}",
                title=(
                    f"[Optimize] #{issue.number} workspace - "
                    "draft, not yet selectable"
                ),
                draft=True,
                reuse_existing=True,
                base_commit=issue.base_commit,
            )
        pull_request = self._pull_requests.synchronize(
            request.repository_root,
            pull_request,
        )
        self._validate_pull_request(
            issue=issue,
            pull_request=pull_request,
            expected_number=expected_number,
        )
        phase = (
            snapshot.phase
            if snapshot is not None
            else WorkspacePhase.SPECIFICATION
        )
        recorded = (
            snapshot is None
            or snapshot.workspace_pull_request_number
            != pull_request.number
        )
        if recorded:
            self._store.commit(
                expected_revision=(
                    snapshot.revision if snapshot is not None else None
                ),
                update=self._workspace_update(
                    request=request,
                    pull_request=pull_request,
                    phase=phase,
                    snapshot=snapshot,
                ),
            )
        return WorkspaceResult(
            phase=phase,
            workspace_pull_request=pull_request,
            planned_effect_kinds=("workspace_pr_sync",),
            recorded=recorded,
            issue_status_projection_intent=(
                WorkspaceIssueStatusProjectionIntent(
                    issue_number=issue.number,
                    phase=phase,
                    workspace_pull_request_number=pull_request.number,
                )
                if pull_request.number is not None
                else None
            ),
            next_action=self._next_action(
                issue.number,
                phase,
                pull_request.number,
            ),
        )

    def _advance_lifecycle(
        self,
        request: WorkspaceRequest,
        snapshot: WorkspaceSnapshot | None,
    ) -> WorkspaceResult:
        if snapshot is None:
            raise ValueError("workspace transition is not allowed")
        operation_id = self._validated_operation(request, snapshot)
        transitions = {
            (
                WorkspacePhase.AWAITING_SELECTION,
                WorkspaceTrigger.PULL_REQUEST_MERGED,
            ): WorkspacePhase.DEPLOYMENT,
            (
                WorkspacePhase.DEPLOYMENT,
                WorkspaceTrigger.DEPLOYMENT_COMPLETED,
            ): WorkspacePhase.RETENTION,
            (
                WorkspacePhase.RETENTION,
                WorkspaceTrigger.RETENTION_COMPLETED,
            ): WorkspacePhase.COMPLETED,
        }
        phase = transitions.get((snapshot.phase, request.trigger))
        if phase is None:
            duplicate_phases = {
                WorkspaceTrigger.PULL_REQUEST_MERGED: {
                    WorkspacePhase.DEPLOYMENT,
                    WorkspacePhase.RETENTION,
                    WorkspacePhase.COMPLETED,
                },
                WorkspaceTrigger.DEPLOYMENT_COMPLETED: {
                    WorkspacePhase.RETENTION,
                    WorkspacePhase.COMPLETED,
                },
                WorkspaceTrigger.RETENTION_COMPLETED: {
                    WorkspacePhase.COMPLETED,
                },
            }
            if snapshot.phase in duplicate_phases.get(
                request.trigger,
                set(),
            ):
                if (
                    operation_id is not None
                    and operation_id
                    not in snapshot.external_operation_ids
                ):
                    raise ValueError(
                        "workspace duplicate operation is not recorded"
                    )
                pull_request = self._resolved_pull_request(
                    request.issue,
                    request.workspace_pull_request,
                    snapshot.workspace_pull_request_number,
                    selected=True,
                )
                return WorkspaceResult(
                    phase=snapshot.phase,
                    workspace_pull_request=pull_request,
                    planned_effect_kinds=(),
                    recorded=False,
                    issue_status_projection_intent=self._projection(
                        request.issue.number,
                        snapshot.phase,
                        pull_request.number,
                    ),
                    next_action=self._next_action(
                        request.issue.number,
                        snapshot.phase,
                        pull_request.number,
                    ),
                )
            raise ValueError("workspace transition is not allowed")
        pull_request = self._resolved_pull_request(
            request.issue,
            request.workspace_pull_request,
            snapshot.workspace_pull_request_number,
            selected=True,
        )
        self._store.commit(
            expected_revision=snapshot.revision,
            update=self._workspace_update(
                request=request,
                pull_request=pull_request,
                phase=phase,
                snapshot=snapshot,
                external_operation_ids=(
                    (
                        *snapshot.external_operation_ids,
                        operation_id,
                    )
                    if operation_id is not None
                    else snapshot.external_operation_ids
                ),
            ),
        )
        return WorkspaceResult(
            phase=phase,
            workspace_pull_request=pull_request,
            planned_effect_kinds=("workspace_state_commit",),
            recorded=True,
            issue_status_projection_intent=self._projection(
                request.issue.number,
                phase,
                pull_request.number,
            ),
            next_action=self._next_action(
                request.issue.number,
                phase,
                pull_request.number,
            ),
        )

    @staticmethod
    def _validated_operation(
        request: WorkspaceRequest,
        snapshot: WorkspaceSnapshot,
    ) -> str | None:
        if request.trigger is WorkspaceTrigger.PULL_REQUEST_MERGED:
            if request.operation is not None:
                raise ValueError(
                    "workspace merge transition cannot carry an operation"
                )
            return None
        operation = request.operation
        if operation is None or operation.trigger is not request.trigger:
            raise ValueError(
                "workspace transition requires a trusted operation result"
            )
        selected = tuple(
            item for item in snapshot.candidates if item.selected
        )
        patch = snapshot.selected_patch
        lineage = snapshot.lineage
        if (
            len(selected) != 1
            or patch is None
            or lineage is None
            or operation.workspace_pull_request_number
            != snapshot.workspace_pull_request_number
            or operation.candidate_id != selected[0].candidate_id
            or hashlib.sha256(patch).hexdigest()
            != operation.patch_sha256
        ):
            raise ValueError("workspace operation selection lineage changed")
        if (
            operation.workspace_pull_request_number
            != lineage.workspace_pull_request_number
            or operation.candidate_id
            != lineage.selected_candidate_id
            or operation.patch_sha256 != lineage.patch_sha256
            or operation.bundle_sha256 != lineage.bundle_sha256
            or operation.evidence_sha256 != lineage.evidence_sha256
        ):
            raise ValueError("workspace operation lineage changed")
        markers = {
            (
                f"{operation.candidate_id}:patch:"
                f"{operation.patch_sha256}"
            ),
            (
                f"{operation.candidate_id}:bundle:"
                f"{operation.bundle_sha256}"
            ),
            (
                f"{operation.candidate_id}:evidence:"
                f"{operation.evidence_sha256}"
            ),
        }
        if not markers <= set(snapshot.external_operation_ids):
            raise ValueError("workspace operation lineage is incomplete")
        if (
            request.trigger is WorkspaceTrigger.RETENTION_COMPLETED
            and operation.predecessor_operation_id
            not in snapshot.external_operation_ids
        ):
            raise ValueError(
                "workspace retention predecessor is not recorded"
            )
        return operation.operation_id

    @staticmethod
    def _resolved_pull_request(
        issue: WorkspaceIssue,
        supplied: WorkspacePullRequest | None,
        number: int | None,
        *,
        selected: bool,
    ) -> WorkspacePullRequest:
        expected = WorkspacePullRequest(
            number=number,
            issue_number=issue.number,
            branch=f"foundry-opt/workspace/issue-{issue.number}",
            title=(
                f"[Optimize] #{issue.number} selected candidate"
                if selected
                else (
                    f"[Optimize] #{issue.number} workspace - "
                    "draft, not yet selectable"
                )
            ),
            draft=not selected,
            reuse_existing=True,
            base_commit=issue.base_commit,
        )
        if supplied is None:
            return expected
        if (
            supplied.number != expected.number
            or supplied.issue_number != expected.issue_number
            or supplied.branch != expected.branch
            or supplied.title != expected.title
            or supplied.draft is not expected.draft
            or supplied.reuse_existing is not True
            or supplied.base_commit != expected.base_commit
        ):
            raise ValueError("workspace pull request does not match issue")
        return supplied

    @staticmethod
    def _projection(
        issue_number: int,
        phase: WorkspacePhase,
        pull_request_number: int | None,
    ) -> WorkspaceIssueStatusProjectionIntent | None:
        if pull_request_number is None:
            return None
        return WorkspaceIssueStatusProjectionIntent(
            issue_number=issue_number,
            phase=phase,
            workspace_pull_request_number=pull_request_number,
        )

    @staticmethod
    def _next_action(
        issue_number: int,
        phase: WorkspacePhase,
        pull_request_number: int | None,
    ) -> WorkspaceNextAction:
        kind, trigger = {
            WorkspacePhase.SPECIFICATION: (
                WorkspaceNextActionKind.DESIGN_CANDIDATES,
                WorkspaceTrigger.EXPERIMENTS_COMPLETED,
            ),
            WorkspacePhase.EVALUATING: (
                WorkspaceNextActionKind.RUN_CANDIDATE_EXPERIMENTS,
                WorkspaceTrigger.EXPERIMENTS_COMPLETED,
            ),
            WorkspacePhase.AWAITING_SELECTION: (
                WorkspaceNextActionKind.MERGE_WORKSPACE_PULL_REQUEST,
                WorkspaceTrigger.PULL_REQUEST_MERGED,
            ),
            WorkspacePhase.DEPLOYMENT: (
                WorkspaceNextActionKind.DEPLOY_SELECTED_CANDIDATE,
                WorkspaceTrigger.DEPLOYMENT_COMPLETED,
            ),
            WorkspacePhase.RETENTION: (
                WorkspaceNextActionKind.COMPLETE_RETENTION,
                WorkspaceTrigger.RETENTION_COMPLETED,
            ),
            WorkspacePhase.COMPLETED: (
                WorkspaceNextActionKind.NONE,
                None,
            ),
        }[phase]
        return WorkspaceNextAction(
            kind=kind,
            issue_number=issue_number,
            workspace_pull_request_number=pull_request_number,
            trigger=trigger,
        )

    @staticmethod
    def _workspace_update(
        *,
        request: WorkspaceRequest,
        pull_request: WorkspacePullRequest,
        phase: WorkspacePhase,
        snapshot: WorkspaceSnapshot | None,
        external_operation_ids: tuple[str, ...] | None = None,
    ) -> WorkspaceUpdate:
        from foundry_opt.orchestration.workspace_store import WorkspaceUpdate

        return WorkspaceUpdate(
            issue_number=request.issue.number,
            phase=phase,
            workspace_pull_request_number=pull_request.number,
            semantic_event=request.trigger.value,
            candidates=snapshot.candidates if snapshot is not None else (),
            selected_patch=(
                snapshot.selected_patch if snapshot is not None else None
            ),
            external_operation_ids=(
                external_operation_ids
                if external_operation_ids is not None
                else (
                    snapshot.external_operation_ids
                    if snapshot is not None
                    else ()
                )
            ),
            experiments=snapshot.experiments if snapshot is not None else (),
            lineage=snapshot.lineage if snapshot is not None else None,
            specification=(
                snapshot.specification if snapshot is not None else None
            ),
            baseline=snapshot.baseline if snapshot is not None else None,
        )

    @staticmethod
    def _validate_pull_request(
        *,
        issue: WorkspaceIssue,
        pull_request: WorkspacePullRequest,
        expected_number: int | None,
    ) -> None:
        if (
            pull_request.issue_number != issue.number
            or pull_request.branch
            != f"foundry-opt/workspace/issue-{issue.number}"
            or pull_request.title
            != (
                f"[Optimize] #{issue.number} workspace - "
                "draft, not yet selectable"
            )
            or pull_request.draft is not True
            or pull_request.reuse_existing is not True
            or pull_request.base_commit != issue.base_commit
            or (
                expected_number is not None
                and pull_request.number != expected_number
            )
        ):
            raise ValueError("workspace pull request does not match issue")


def _report_to_dict(report: OptimizationReport) -> dict[str, Any]:
    return {
        "alternatives": [
            (
                {
                    "candidate_id": item.candidate_id,
                    "outcome": item.outcome,
                    "rejection_reason": item.rejection_reason,
                }
                if not isinstance(item, str)
                else item
            )
            for item in report.alternatives
        ],
        "base_commit": report.base_commit,
        "baseline_metrics": dict(report.baseline_metrics),
        "bundle_sha256": report.bundle_sha256,
        "candidate_id": report.candidate_id,
        "candidate_metrics": dict(report.candidate_metrics),
        "changed_paths": list(report.changed_paths),
        "evidence_sha256": report.evidence_sha256,
        "expected_tree": report.expected_tree,
        "foundry_operations": [
            {
                "completed_at": item.completed_at,
                "identifier": item.identifier,
                "kind": item.kind,
                "started_at": item.started_at,
                "status": item.status,
                "url": item.url,
            }
            for item in report.foundry_operations
        ],
        "guardrails": dict(report.guardrails),
        "issue_number": report.issue_number,
        "materiality": dict(report.materiality),
        "merge_gate": report.merge_gate.value,
        "patch_sha256": report.patch_sha256,
        "recommendation": report.recommendation,
        "required_checks": dict(report.required_checks),
        "sample_count": report.sample_count,
        "spec_sha256": report.spec_sha256,
        "split": report.split,
        "thresholds": dict(report.thresholds),
        "validation": list(report.validation),
    }
