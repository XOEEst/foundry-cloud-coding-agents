"""State-aware, filesystem-handoff issue-driven optimization runner.

This module implements the production :class:`OptimizationCommandService`.
Unlike the synchronous :func:`foundry_opt.campaign.engine.run_campaign`
engine, it never calls a synchronous LLM ``CandidateGenerator``. Instead it
drives a bounded campaign incrementally across independent CLI invocations
(and therefore across process boundaries) using durable, atomically written
campaign state:

* ``RUN`` loads and verifies a merged, approved optimization spec, registers
  only approved assets, establishes a pinned baseline draft and development
  evaluation, and then pauses at ``awaiting_agent``.
* ``candidate request`` atomically reserves one slot, prepares an isolated
  optimizer worktree, and writes an agent-readable ``context.json`` that
  contains only redacted, aggregate information (never raw validation rows,
  prompts, responses, or secrets).
* ``candidate submit`` validates the agent-authored idea file, enforces the
  guardrails, runs validation, exports an exact patch, packages, drafts, and
  evaluates the candidate, then persists the full evaluation result.
* Finalization (reached through ``AUTO``/``RUN`` once slots are exhausted or
  the candidate cutoff passes) reconciles held-out evaluations, selects the
  Pareto frontier, writes redacted evidence, and publishes the temporary
  optimization pull request and candidate issues.

The runner computes every status and metric itself; agent input can never
set an outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Protocol

import yaml

from foundry_opt.campaign.lineage import IdeaLineage
from foundry_opt.campaign.models import (
    CampaignLimits,
    CampaignReport,
    CandidateArtifact,
)
from foundry_opt.campaign.protocols import (
    ActiveCampaignError,
    BundleBuilder,
    CampaignRepository,
    CampaignRequest,
    CampaignWorktree,
    CandidateIdea,
    Clock,
    DraftCreator,
    EvaluationRunner,
    EvidenceWriter,
    UnsafeMutationError,
    ValidationRunner,
)
from foundry_opt.campaign.state import (
    CampaignState,
    CampaignStateStore,
    CandidateState,
    DraftCreationIntent,
    DraftMetadata,
    FinalizedPublication,
)
from foundry_opt.campaign.worktrees import contained_worktree_root
from foundry_opt.config.models import (
    MetricDirection as ConfigMetricDirection,
    OptimizerConfig,
    UndefinedBehavior as ConfigUndefinedBehavior,
)
from foundry_opt.evaluation import (
    AgentVersionRef,
    DatasetSplit,
    EvaluationPolicy,
    EvaluationResult,
    EvaluationSubject,
    MetricDirection,
    MetricPolicy,
    ParetoResult,
    UndefinedBehavior,
    evaluate_with_repeat,
    select_eligible_candidates,
)
from foundry_opt.drafts import DraftRecord
from foundry_opt.evidence import (
    EvaluationAssetReference,
    EvidenceManifest,
    EvidenceRequest,
)
from foundry_opt.optimization.assets import (
    EvaluationAssetError,
    EvaluationAssetRegistrationGateway,
    TraceAssetRegistrationBlockedError,
    materialize_prepared_asset,
)
from foundry_opt.optimization.commands import (
    OptimizeCommandRequest,
    OptimizeCommandResult,
    OptimizeCommandStatus,
    OptimizePhase,
)
from foundry_opt.optimization.models import (
    AssetProvenance,
    OptimizationSpec,
    PreparedEvaluationAsset,
)
from foundry_opt.optimization.specification import (
    provenance_file_path,
    spec_file_path,
)
from foundry_opt.security import reject_secret_content


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")

_TERMINAL_CANDIDATE_STATUSES = frozenset(
    {
        "evaluated",
        "guardrail_rejected",
        "validation_failed",
        "unchanged",
        "deadline_exceeded",
        "cutoff_exceeded",
        "failed",
    }
)

# Non-terminal states a single reserved candidate slot moves through. Each is
# an irreversible checkpoint persisted before its side effect so a crashed
# submit resumes without repeating a commit, draft, or duplicate worktree.
_IN_FLIGHT_CANDIDATE_STATUSES = frozenset(
    {
        "preparing_worktree",
        "awaiting_idea",
        "committed",
        "drafted",
    }
)


# ---------------------------------------------------------------------------
# Honest capability signalling
# ---------------------------------------------------------------------------


class CapabilityUnavailableError(RuntimeError):
    """Raised when a required live Foundry/GitHub capability is missing.

    Production adapters raise this (never a fabricated success) so the runner
    can surface a typed ``blocked`` result instead of pretending the campaign
    made progress.
    """

    def __init__(self, code: str, message: str) -> None:
        if not _IDENTIFIER.fullmatch(code):
            raise ValueError("capability code must be an identifier")
        self.code = code
        super().__init__(message)


class IdeaContractError(ValueError):
    """The agent-authored idea file is malformed or violates the contract."""


class CampaignRecoveryError(RuntimeError):
    """A persisted campaign checkpoint is internally inconsistent.

    Raised when crash recovery finds a durable checkpoint that cannot be
    resumed (for example a ``committed`` candidate missing its persisted
    patch). The runner surfaces it as a typed ``failed`` result and marks the
    campaign for inspection instead of raising an unhandled traceback.
    """


# ---------------------------------------------------------------------------
# Spec approval gateway
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpecApprovalResult:
    """The typed outcome of verifying a merged, approved specification."""

    approved: bool
    default_branch: str | None = None
    approval_commit: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.approved:
            if self.approval_commit is None or not _COMMIT.fullmatch(
                self.approval_commit
            ):
                raise ValueError(
                    "an approved spec requires an approval merge commit"
                )
            if not self.default_branch:
                raise ValueError("an approved spec requires a default branch")
        elif not self.reason:
            raise ValueError("an unapproved spec requires a reason")


class SpecApprovalGateway(Protocol):
    """Verifies that a pinned spec was approved by a maintainer merge.

    Implementations confirm the spec file is present on the default branch at
    (or as an ancestor of) ``base_commit`` and that its approval merge commit
    and the default/base relation are consistent, without trusting local,
    unmerged content.
    """

    def verify_spec_approval(
        self,
        repository_root: Path,
        *,
        issue_number: int,
        spec: OptimizationSpec,
        spec_sha256: str,
        base_commit: str,
    ) -> SpecApprovalResult: ...


# ---------------------------------------------------------------------------
# Campaign publication seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CampaignPublicationInputs:
    repository_root: Path
    report: CampaignReport
    development_evidence: EvidenceManifest
    validation_evidence: EvidenceManifest | None
    reproduction_instructions: tuple[str, ...]


class CampaignPublisher(Protocol):
    """Publishes the temporary optimization PR and candidate issues.

    Production implementations assemble the artifact commit and delegate to
    :func:`foundry_opt.github_workflow.publish_campaign`; when the required
    GitHub capability is missing they raise :class:`CapabilityUnavailableError`
    rather than fabricating a publication.
    """

    def publish(
        self,
        inputs: CampaignPublicationInputs,
    ) -> FinalizedPublication: ...


EvaluationBinder = Callable[
    [OptimizationSpec, tuple[EvaluationAssetReference, ...]],
    EvaluationRunner,
]


class SpecificationService(Protocol):
    def prepare_specification(
        self,
        repository_root: Path,
        issue_number: int,
    ) -> Any: ...


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IssueOptimizationDependencies:
    config: OptimizerConfig
    spec_service: SpecificationService
    spec_gateway: SpecApprovalGateway
    registration_gateway: EvaluationAssetRegistrationGateway
    repository: CampaignRepository
    validate: ValidationRunner
    build_bundle: BundleBuilder
    create_draft: DraftCreator
    bind_evaluation: EvaluationBinder
    write_evidence: EvidenceWriter
    publish: CampaignPublisher
    state: CampaignStateStore
    clock: Clock
    apply_service: Any | None = None
    reconcile_service: Any | None = None


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class IssueOptimizationRunner:
    """Production ``OptimizationCommandService`` implementation."""

    def __init__(self, dependencies: IssueOptimizationDependencies) -> None:
        self._deps = dependencies

    # -- entry point --------------------------------------------------------

    def execute(
        self,
        request: OptimizeCommandRequest,
    ) -> OptimizeCommandResult:
        try:
            if request.phase is OptimizePhase.SPEC:
                return self._spec(request)
            if request.phase is OptimizePhase.RUN:
                return self._run(request)
            if request.phase is OptimizePhase.CANDIDATE_REQUEST:
                return self._candidate_request(request)
            if request.phase is OptimizePhase.CANDIDATE_SUBMIT:
                return self._candidate_submit(request)
            if request.phase is OptimizePhase.APPLY:
                return self._delegate(request, self._deps.apply_service, "apply")
            if request.phase is OptimizePhase.RECONCILE:
                return self._delegate(
                    request, self._deps.reconcile_service, "reconcile"
                )
            return self._auto(request)
        except CapabilityUnavailableError as error:
            return self._blocked(
                request.phase,
                request.issue_number,
                error.code,
                str(error),
            )
        except ActiveCampaignError:
            return self._blocked(
                request.phase,
                request.issue_number,
                "campaign_lock_active",
                "another optimizer process holds the campaign lock; retry "
                "once it releases",
            )
        except (CampaignRecoveryError, RuntimeError, ValueError) as error:
            return self._recover_failure(request, error)

    def _recover_failure(
        self,
        request: OptimizeCommandRequest,
        error: Exception,
    ) -> OptimizeCommandResult:
        """Mark the campaign failed and return a typed result (no traceback)."""
        code = (
            "campaign_recovery_failed"
            if isinstance(error, CampaignRecoveryError)
            else "campaign_execution_failed"
        )
        if request.phase in (
            OptimizePhase.RUN,
            OptimizePhase.CANDIDATE_REQUEST,
            OptimizePhase.CANDIDATE_SUBMIT,
            OptimizePhase.AUTO,
        ):
            try:
                root = _resolve_root(request.repository_root)
                campaign_id = _campaign_id(request.issue_number)
                state = self._deps.state.load(root, campaign_id)
                if (
                    state is not None
                    and state.finalized is None
                    and state.status not in {"failed"}
                ):
                    self._deps.state.save(
                        root,
                        replace(
                            state,
                            status="failed",
                            error_code=code,
                            updated_at=self._deps.clock.now(),
                        ),
                    )
            except Exception:
                pass
        return OptimizeCommandResult(
            status=OptimizeCommandStatus.FAILED,
            phase=request.phase,
            summary=(
                "the optimization command could not be completed and the "
                f"campaign was marked for inspection ({type(error).__name__})"
            ),
            issue_number=request.issue_number,
            details={"code": code},
        )

    # -- SPEC ---------------------------------------------------------------

    def _spec(self, request: OptimizeCommandRequest) -> OptimizeCommandResult:
        result = self._deps.spec_service.prepare_specification(
            request.repository_root,
            request.issue_number,
        )
        return _spec_result_to_command(request.issue_number, result)

    # -- RUN / finalize -----------------------------------------------------

    def _run(self, request: OptimizeCommandRequest) -> OptimizeCommandResult:
        root = _resolve_root(request.repository_root)
        campaign_id = _campaign_id(request.issue_number)
        state = self._deps.state.load(root, campaign_id)

        if state is not None and state.finalized is not None:
            return self._finalized_result(
                OptimizePhase.RUN, request.issue_number, state
            )
        if state is not None and state.status in {"failed", "stale"}:
            return self._blocked(
                OptimizePhase.RUN,
                request.issue_number,
                "campaign_requires_inspection",
                f"campaign {campaign_id} is {state.status} "
                f"({state.error_code or 'unknown'}) and needs inspection",
            )

        if state is None and not self._spec_bundle_exists(
            root, request.issue_number
        ):
            return self._blocked(
                OptimizePhase.RUN,
                request.issue_number,
                "spec_not_prepared",
                "no merged optimization specification was found; run "
                "`optimize spec` and merge the approved specification first",
            )

        spec, spec_sha256, asset_paths = self._load_spec_bundle(
            root, request.issue_number
        )
        pinned = self._deps.repository.pin_default_branch(root)
        approval = self._deps.spec_gateway.verify_spec_approval(
            root,
            issue_number=request.issue_number,
            spec=spec,
            spec_sha256=spec_sha256,
            base_commit=pinned.commit,
        )
        if not approval.approved:
            return self._blocked(
                OptimizePhase.RUN,
                request.issue_number,
                "spec_not_approved",
                approval.reason or "the specification has not been approved",
            )

        if state is not None and state.baseline_development is not None:
            # Resume: reuse the already-materialized assets rather than
            # re-registering them with Foundry on every invocation.
            campaign_request = self._campaign_request(
                campaign_id, spec, spec_sha256, state.assets, root
            )
            mismatch = _state_mismatch(state, campaign_request, pinned.commit)
            if mismatch is not None:
                self._deps.state.save(
                    root,
                    replace(
                        state,
                        status="failed",
                        error_code="campaign_state_mismatch",
                        updated_at=self._deps.clock.now(),
                    ),
                )
                return self._blocked(
                    OptimizePhase.RUN,
                    request.issue_number,
                    "campaign_state_mismatch",
                    mismatch,
                )
            return self._drive(
                OptimizePhase.RUN,
                request,
                root,
                state,
                spec,
                campaign_request,
            )

        assets = self._materialize_assets(root, spec, asset_paths)
        campaign_request = self._campaign_request(
            campaign_id, spec, spec_sha256, assets, root
        )

        if state is not None:
            mismatch = _state_mismatch(state, campaign_request, pinned.commit)
            if mismatch is not None:
                failed = replace(
                    state,
                    status="failed",
                    error_code="campaign_state_mismatch",
                    updated_at=self._deps.clock.now(),
                )
                self._deps.state.save(root, failed)
                return self._blocked(
                    OptimizePhase.RUN,
                    request.issue_number,
                    "campaign_state_mismatch",
                    mismatch,
                )

        state = self._initialize_campaign(
            root, campaign_request, spec, pinned.commit
        )
        return self._drive(
            OptimizePhase.RUN, request, root, state, spec, campaign_request
        )

    def _initialize_campaign(
        self,
        root: Path,
        request: CampaignRequest,
        spec: OptimizationSpec,
        base_commit: str,
    ) -> CampaignState:
        now = self._deps.clock.now()
        goal_sha256 = hashlib.sha256(request.goal.encode("utf-8")).hexdigest()
        lock = self._deps.repository.acquire_lock(
            repository_root=root,
            target=request.target,
            campaign_id=request.campaign_id,
            base_commit=base_commit,
            now=now,
            stale_after=request.stale_after,
        )
        if lock.recovered_campaign_id is not None:
            self._deps.state.mark_stale(root, lock.recovered_campaign_id, now)
        try:
            state = CampaignState(
                campaign_id=request.campaign_id,
                target=request.target,
                base_commit=base_commit,
                status="active",
                started_at=now,
                updated_at=now,
                goal_sha256=goal_sha256,
                spec_sha256=request.spec_sha256,
                assets=request.assets,
            )
            self._deps.state.save(root, state)

            baseline_worktree = self._deps.repository.create_worktree(
                root, request.campaign_id, "baseline", base_commit
            )
            try:
                bundle = self._deps.build_bundle(
                    baseline_worktree.path,
                    baseline_worktree.path / ".foundry-opt-baseline.zip",
                )
                intent = _draft_intent(
                    request, "baseline", base_commit, bundle.sha256
                )
                state = replace(
                    state,
                    baseline_draft_intent=intent,
                    updated_at=self._deps.clock.now(),
                )
                self._deps.state.save(root, state)
                draft = self._deps.create_draft(
                    request.target,
                    "baseline",
                    intent.idempotency_key,
                    bundle,
                )
                state = replace(
                    state,
                    baseline_draft_id=draft.version_id,
                    baseline_draft=DraftMetadata.from_record(draft),
                    baseline_draft_intent=replace(intent, status="reconciled"),
                    updated_at=self._deps.clock.now(),
                )
                self._deps.state.save(root, state)
            finally:
                self._deps.repository.cleanup_worktree(root, baseline_worktree)

            evaluate = self._deps.bind_evaluation(spec, request.assets)
            baseline_subject = _subject(
                request.target, "baseline", draft.version_id
            )
            development = _evaluate(
                evaluate,
                baseline_subject,
                DatasetSplit.DEVELOPMENT,
                request.evaluation_policy,
            )
            state = replace(
                state,
                baseline_development=development,
                baseline_metrics=_metrics(development),
                updated_at=self._deps.clock.now(),
            )
            self._deps.state.save(root, state)
            return state
        except (CapabilityUnavailableError, IdeaContractError):
            raise
        except ActiveCampaignError:
            raise
        except Exception as error:
            self._fail_campaign(root, request.campaign_id, error)
            raise
        finally:
            self._deps.repository.release_lock(
                repository_root=root,
                target=request.target,
                campaign_id=request.campaign_id,
            )

    # -- candidate request --------------------------------------------------

    def _candidate_request(
        self,
        request: OptimizeCommandRequest,
    ) -> OptimizeCommandResult:
        root = _resolve_root(request.repository_root)
        campaign_id = _campaign_id(request.issue_number)
        state = self._deps.state.load(root, campaign_id)
        guard = self._require_active(
            OptimizePhase.CANDIDATE_REQUEST, request, state
        )
        if guard is not None:
            return guard
        assert state is not None

        spec, spec_sha256, _ = self._load_spec_bundle(
            root, request.issue_number
        )
        campaign_request = self._rebuild_request(root, state, spec, spec_sha256)

        # Resume an in-flight reservation before considering a new slot.
        if state.awaiting_candidate_id is not None:
            candidate = _candidate(state, state.awaiting_candidate_id)
            if candidate is not None and (
                candidate.status in _IN_FLIGHT_CANDIDATE_STATUSES
            ):
                if candidate.status == "preparing_worktree":
                    state, candidate = self._finish_reservation(
                        root, campaign_id, state, candidate, campaign_request
                    )
                return self._awaiting_idea_result(
                    OptimizePhase.CANDIDATE_REQUEST, request, root, candidate
                )

        if self._cutoff_reached(state, campaign_request):
            return self._finalize_ready(
                OptimizePhase.CANDIDATE_REQUEST,
                request,
                "the candidate cutoff has passed; finalize the campaign",
            )
        if state.launched_slots >= campaign_request.limits.max_changed_candidates:
            return self._finalize_ready(
                OptimizePhase.CANDIDATE_REQUEST,
                request,
                "every candidate slot has been used; finalize the campaign",
            )

        # Persist the reservation (status ``preparing_worktree``) BEFORE the
        # irreversible worktree creation, so a crash leaves durable evidence
        # that the slot was reserved and its worktree must be reconciled.
        slot = state.launched_slots + 1
        candidate_id = f"candidate-{slot}"
        candidate = CandidateState(
            candidate_id=candidate_id,
            slot=slot,
            status="preparing_worktree",
            attempts=1,
        )
        state = replace(
            state,
            launched_slots=slot,
            candidates=(*state.candidates, candidate),
            awaiting_candidate_id=candidate_id,
            updated_at=self._deps.clock.now(),
        )
        self._deps.state.save(root, state)

        state, candidate = self._finish_reservation(
            root, campaign_id, state, candidate, campaign_request
        )
        return self._awaiting_idea_result(
            OptimizePhase.CANDIDATE_REQUEST, request, root, candidate
        )

    def _finish_reservation(
        self,
        root: Path,
        campaign_id: str,
        state: CampaignState,
        candidate: CandidateState,
        campaign_request: CampaignRequest,
    ) -> tuple[CampaignState, CandidateState]:
        """Reconcile any orphan worktree, create a clean one, then persist
        the durable ``awaiting_idea`` checkpoint with the context hash."""
        worktree = self._deps.repository.reconcile_worktree(
            root, campaign_id, candidate.candidate_id, state.base_commit
        )
        context_relative, context_sha256 = self._write_context(
            root,
            campaign_id,
            candidate.candidate_id,
            candidate.slot,
            state,
            worktree,
            campaign_request,
        )
        candidate = replace(
            candidate,
            status="awaiting_idea",
            context_path=context_relative.as_posix(),
            context_sha256=context_sha256,
        )
        state = replace(
            _replace_candidate(state, candidate),
            awaiting_candidate_id=candidate.candidate_id,
            updated_at=self._deps.clock.now(),
        )
        self._deps.state.save(root, state)
        return state, candidate

    # -- candidate submit ---------------------------------------------------

    def _candidate_submit(
        self,
        request: OptimizeCommandRequest,
    ) -> OptimizeCommandResult:
        root = _resolve_root(request.repository_root)
        campaign_id = _campaign_id(request.issue_number)
        state = self._deps.state.load(root, campaign_id)
        guard = self._require_active(
            OptimizePhase.CANDIDATE_SUBMIT, request, state
        )
        if guard is not None:
            return guard
        assert state is not None

        candidate = _candidate(state, request.candidate_id or "")
        if candidate is None:
            return self._blocked(
                OptimizePhase.CANDIDATE_SUBMIT,
                request.issue_number,
                "unknown_candidate",
                f"candidate {request.candidate_id!r} is not part of this "
                "campaign",
            )

        spec, spec_sha256, _ = self._load_spec_bundle(
            root, request.issue_number
        )
        campaign_request = self._rebuild_request(root, state, spec, spec_sha256)

        if candidate.status in _TERMINAL_CANDIDATE_STATUSES:
            # Idempotent resubmission after the slot is already resolved. Any
            # worktree left orphaned by a crash between the eval save and its
            # cleanup is discarded so no duplicate worktree lingers.
            self._discard_candidate_worktree(
                root, campaign_id, candidate, state.base_commit
            )
            return self._post_submit_result(
                request, state, campaign_request
            )
        if candidate.status not in _IN_FLIGHT_CANDIDATE_STATUSES:
            return self._blocked(
                OptimizePhase.CANDIDATE_SUBMIT,
                request.issue_number,
                "candidate_not_awaiting_idea",
                f"candidate {candidate.candidate_id} is {candidate.status}, "
                "not awaiting an idea",
            )
        if state.awaiting_candidate_id != candidate.candidate_id:
            return self._blocked(
                OptimizePhase.CANDIDATE_SUBMIT,
                request.issue_number,
                "candidate_ownership_mismatch",
                "another candidate currently owns the reserved slot",
            )

        # A submit that lands on an unfinished reservation completes it and
        # asks the agent to edit the freshly reconciled worktree, since any
        # pre-crash edits cannot be trusted.
        if candidate.status == "preparing_worktree":
            _, candidate = self._finish_reservation(
                root, campaign_id, state, candidate, campaign_request
            )
            return self._awaiting_idea_result(
                OptimizePhase.CANDIDATE_SUBMIT, request, root, candidate
            )

        worktree = self._require_worktree(
            root, campaign_id, candidate, state.base_commit
        )

        # Resume irreversible checkpoints without repeating their side effects.
        if candidate.status == "committed":
            return self._resume_from_committed(
                root, request, state, candidate, worktree, spec,
                campaign_request,
            )
        if candidate.status == "drafted":
            return self._resume_from_drafted(
                root, request, state, candidate, worktree, spec,
                campaign_request,
            )

        try:
            idea, idea_relative, idea_sha256 = self._read_idea(
                root, request.idea_file, worktree
            )
        except IdeaContractError as error:
            return self._resolve_candidate(
                root,
                request,
                state,
                candidate,
                worktree,
                status="guardrail_rejected",
                error_code="malicious_or_invalid_idea",
                message=str(error),
                campaign_request=campaign_request,
            )

        return self._process_idea(
            root,
            request,
            state,
            candidate,
            worktree,
            spec,
            campaign_request,
            idea,
            idea_relative,
            idea_sha256,
        )

    def _process_idea(
        self,
        root: Path,
        request: OptimizeCommandRequest,
        state: CampaignState,
        candidate: CandidateState,
        worktree: CampaignWorktree,
        spec: OptimizationSpec,
        campaign_request: CampaignRequest,
        idea: CandidateIdea,
        idea_relative: Path,
        idea_sha256: str,
    ) -> OptimizeCommandResult:
        candidate = replace(
            candidate,
            idea_path=idea_relative.as_posix(),
            idea_sha256=idea_sha256,
        )
        if worktree.base_commit != state.base_commit:
            return self._resolve_candidate(
                root, request, state, candidate, worktree,
                status="guardrail_rejected",
                error_code="candidate_base_mismatch",
                message="the candidate worktree base does not match the "
                "campaign base",
                campaign_request=campaign_request,
            )
        if self._deadline_reached(state, campaign_request):
            return self._resolve_candidate(
                root, request, state, candidate, worktree,
                status="deadline_exceeded",
                error_code="campaign_deadline_exceeded",
                message="the campaign deadline passed before the candidate "
                "could be evaluated",
                campaign_request=campaign_request,
            )

        changed_paths = self._deps.repository.changed_paths(worktree)
        lineage = IdeaLineage(
            idea.idea_id,
            idea.parent_idea_ids,
            idea.mutation_class,
            changed_paths,
        )
        candidate = replace(candidate, lineage=lineage)
        try:
            _enforce_lineage(idea, state)
            _enforce_mutation(campaign_request, idea)
            _enforce_paths(campaign_request.edit_paths, changed_paths)
        except (ValueError, UnsafeMutationError) as error:
            return self._resolve_candidate(
                root, request, state, candidate, worktree,
                status="guardrail_rejected",
                error_code="mutation_guardrail_rejected",
                message=str(error),
                campaign_request=campaign_request,
            )
        if not changed_paths:
            return self._resolve_candidate(
                root, request, state, candidate, worktree,
                status="unchanged",
                error_code=None,
                message="the candidate did not change any files",
                campaign_request=campaign_request,
            )

        validation = self._deps.validate(worktree.path)
        if not validation.passed:
            return self._resolve_candidate(
                root, request, state, candidate, worktree,
                status="validation_failed",
                error_code="validation_failed",
                message="the candidate failed validation",
                campaign_request=campaign_request,
            )

        post_paths = self._deps.repository.changed_paths(worktree)
        try:
            _enforce_paths(campaign_request.edit_paths, post_paths)
        except ValueError as error:
            candidate = replace(
                candidate,
                lineage=IdeaLineage(
                    idea.idea_id,
                    idea.parent_idea_ids,
                    idea.mutation_class,
                    post_paths,
                ),
            )
            return self._resolve_candidate(
                root, request, state, candidate, worktree,
                status="guardrail_rejected",
                error_code="post_validation_guardrail_rejected",
                message=str(error),
                campaign_request=campaign_request,
            )
        if not post_paths:
            return self._resolve_candidate(
                root, request, state, candidate, worktree,
                status="unchanged",
                error_code=None,
                message="validation reverted every candidate change",
                campaign_request=campaign_request,
            )
        candidate = replace(
            candidate,
            lineage=IdeaLineage(
                idea.idea_id,
                idea.parent_idea_ids,
                idea.mutation_class,
                post_paths,
            ),
        )

        # Irreversible checkpoint 1: commit + export the exact patch, then
        # persist ``committed`` (result_commit + PatchArtifact) BEFORE any
        # bundle build or draft network call. A crash here resumes without a
        # second commit.
        result_commit = self._deps.repository.commit_worktree(
            worktree, f"foundry-opt candidate {candidate.candidate_id}"
        )
        patch = self._deps.repository.export_patch(
            root, state.campaign_id, worktree, result_commit
        )
        candidate = replace(
            candidate,
            status="committed",
            result_commit=result_commit,
            patch=patch,
        )
        state = _replace_candidate(state, candidate)
        self._deps.state.save(root, state)
        return self._resume_from_committed(
            root, request, state, candidate, worktree, spec, campaign_request
        )

    def _resume_from_committed(
        self,
        root: Path,
        request: OptimizeCommandRequest,
        state: CampaignState,
        candidate: CandidateState,
        worktree: CampaignWorktree,
        spec: OptimizationSpec,
        campaign_request: CampaignRequest,
    ) -> OptimizeCommandResult:
        if candidate.patch is None or candidate.result_commit is None:
            raise CampaignRecoveryError(
                "committed candidate is missing its persisted patch"
            )
        # The committed worktree tree is unchanged, so the deterministic bundle
        # is rebuilt (identical bytes/SHA-256) rather than persisted. Any
        # leftover bundle output from a prior attempt is removed first.
        self._clean_bundle_output(worktree, candidate.candidate_id)
        bundle = self._deps.build_bundle(
            worktree.path,
            worktree.path / f".foundry-opt-{candidate.candidate_id}.zip",
        )
        intent = _draft_intent(
            campaign_request,
            candidate.candidate_id,
            state.base_commit,
            bundle.sha256,
        )
        candidate = replace(candidate, draft_intent=intent)
        state = _replace_candidate(state, candidate)
        self._deps.state.save(root, state)
        # The draft gateway is idempotent on this idempotency key + bundle
        # hash, so a duplicate is never created even if a prior POST succeeded
        # before its record was persisted.
        draft = self._deps.create_draft(
            state.target,
            candidate.candidate_id,
            intent.idempotency_key,
            bundle,
        )
        return self._after_draft(
            root, request, state, candidate, worktree, spec, campaign_request,
            intent, draft,
        )

    def _after_draft(
        self,
        root: Path,
        request: OptimizeCommandRequest,
        state: CampaignState,
        candidate: CandidateState,
        worktree: CampaignWorktree,
        spec: OptimizationSpec,
        campaign_request: CampaignRequest,
        intent: DraftCreationIntent,
        draft: DraftRecord,
    ) -> OptimizeCommandResult:
        # Irreversible checkpoint 2: persist ``drafted`` (DraftMetadata) BEFORE
        # the development evaluation, so a crash resumes without recreating the
        # draft.
        candidate = replace(
            candidate,
            status="drafted",
            draft=DraftMetadata.from_record(draft),
            draft_intent=replace(intent, status="reconciled"),
        )
        state = _replace_candidate(state, candidate)
        self._deps.state.save(root, state)
        evaluate = self._deps.bind_evaluation(spec, state.assets)
        result = _evaluate(
            evaluate,
            _subject(state.target, candidate.candidate_id, draft.version_id),
            DatasetSplit.DEVELOPMENT,
            campaign_request.evaluation_policy,
        )
        return self._after_development_evaluation(
            root, request, state, candidate, worktree, campaign_request, result
        )

    def _resume_from_drafted(
        self,
        root: Path,
        request: OptimizeCommandRequest,
        state: CampaignState,
        candidate: CandidateState,
        worktree: CampaignWorktree,
        spec: OptimizationSpec,
        campaign_request: CampaignRequest,
    ) -> OptimizeCommandResult:
        if candidate.draft is None or candidate.patch is None:
            raise CampaignRecoveryError(
                "drafted candidate is missing its persisted draft"
            )
        # The draft already exists and is persisted; skip commit/bundle/draft
        # and re-run only the development evaluation.
        evaluate = self._deps.bind_evaluation(spec, state.assets)
        result = _evaluate(
            evaluate,
            _subject(
                state.target,
                candidate.candidate_id,
                candidate.draft.version_id,
            ),
            DatasetSplit.DEVELOPMENT,
            campaign_request.evaluation_policy,
        )
        return self._after_development_evaluation(
            root, request, state, candidate, worktree, campaign_request, result
        )

    def _after_development_evaluation(
        self,
        root: Path,
        request: OptimizeCommandRequest,
        state: CampaignState,
        candidate: CandidateState,
        worktree: CampaignWorktree,
        campaign_request: CampaignRequest,
        result: EvaluationResult,
    ) -> OptimizeCommandResult:
        if candidate.patch is None or candidate.draft is None:
            raise CampaignRecoveryError(
                "evaluated candidate is missing its patch or draft"
            )
        metrics = _metrics(result)
        artifact = CandidateArtifact(
            candidate_id=candidate.candidate_id,
            patch=candidate.patch,
            draft_id=candidate.draft.version_id,
            evidence_path=(
                campaign_request.evidence_root
                / state.campaign_id
                / "development-evidence.json"
            ),
            eligible=False,
            metrics=metrics,
        )
        candidate = replace(
            candidate,
            status="evaluated",
            artifact=artifact,
            metrics=metrics,
            development_result=result,
        )
        state = _replace_candidate(state, candidate)
        provisional = self._provisional_pareto(
            state, campaign_request.evaluation_policy
        )
        candidate = replace(
            candidate,
            provisional_eligible=(
                candidate.candidate_id in provisional.eligible_ids
            ),
        )
        state = replace(
            _replace_candidate(state, candidate),
            awaiting_candidate_id=None,
            updated_at=self._deps.clock.now(),
        )
        self._deps.state.save(root, state)
        self._deps.repository.cleanup_worktree(root, worktree)
        return self._post_submit_result(request, state, campaign_request)

    def _clean_bundle_output(
        self,
        worktree: CampaignWorktree,
        candidate_id: str,
    ) -> None:
        output = worktree.path / f".foundry-opt-{candidate_id}.zip"
        for path in (
            output,
            output.with_name(f"{output.name}.manifest.json"),
            output.with_name(f"{output.name}.partial"),
        ):
            if os.path.lexists(path) and not path.is_symlink():
                path.unlink(missing_ok=True)

    def _discard_candidate_worktree(
        self,
        root: Path,
        campaign_id: str,
        candidate: CandidateState,
        base_commit: str,
    ) -> None:
        worktree_path = (
            contained_worktree_root(root, campaign_id) / candidate.candidate_id
        )
        if not os.path.lexists(worktree_path):
            return
        try:
            worktree = self._deps.repository.open_worktree(
                root, campaign_id, candidate.candidate_id, base_commit
            )
        except (RuntimeError, ValueError):
            return
        try:
            self._deps.repository.cleanup_worktree(root, worktree)
        except (RuntimeError, ValueError):
            pass

    def _resolve_candidate(
        self,
        root: Path,
        request: OptimizeCommandRequest,
        state: CampaignState,
        candidate: CandidateState,
        worktree: CampaignWorktree,
        *,
        status: str,
        error_code: str | None,
        message: str,
        campaign_request: CampaignRequest,
    ) -> OptimizeCommandResult:
        candidate = replace(candidate, status=status, error_code=error_code)
        state = replace(
            _replace_candidate(state, candidate),
            awaiting_candidate_id=None,
            updated_at=self._deps.clock.now(),
        )
        self._deps.state.save(root, state)
        self._deps.repository.cleanup_worktree(root, worktree)
        result = self._post_submit_result(request, state, campaign_request)
        return replace(
            result,
            summary=(
                f"Candidate {candidate.candidate_id} was rejected: {message}."
            ),
            details={**dict(result.details), "candidate_status": status},
        )

    # -- finalize -----------------------------------------------------------

    def _finalize(
        self,
        phase: OptimizePhase,
        request: OptimizeCommandRequest,
        root: Path,
        state: CampaignState,
        spec: OptimizationSpec,
        campaign_request: CampaignRequest,
    ) -> OptimizeCommandResult:
        if state.finalized is not None:
            return self._finalized_result(phase, request.issue_number, state)

        baseline = state.baseline_development
        assert baseline is not None
        development_results = tuple(
            candidate.development_result
            for candidate in state.candidates
            if candidate.development_result is not None
        )
        policy = campaign_request.evaluation_policy
        development_pareto = select_eligible_candidates(
            baseline, development_results, policy
        )
        development_evidence = self._write_evidence(
            root,
            state,
            campaign_request,
            baseline,
            development_results,
            development_pareto,
            "development-evidence.json",
        )
        development_path = _relative_evidence(root, development_evidence.path)

        validation_results: list[EvaluationResult] = []
        baseline_validation: EvaluationResult | None = None
        validation_evidence: EvidenceManifest | None = None
        final_ids: tuple[str, ...] = ()
        results_by_id = {
            result.run.subject_id: result for result in development_results
        }
        if development_pareto.eligible_ids:
            already_started = state.baseline_validation is not None
            # The deadline may only block *starting wholly new* held-out
            # evaluation. Once any held-out work is persisted, reconciliation
            # always continues (and reuses) the remaining results, even past
            # the deadline, so a publication retry never discards progress.
            if already_started or not self._deadline_reached(
                state, campaign_request
            ):
                state = self._reconcile_held_out(
                    root,
                    state,
                    campaign_request,
                    spec,
                    development_pareto,
                    results_by_id,
                    policy,
                )
                baseline_validation = state.baseline_validation
                validation_results = [
                    candidate.validation_result
                    for candidate_id in development_pareto.eligible_ids
                    if (candidate := _candidate(state, candidate_id))
                    is not None
                    and candidate.validation_result is not None
                ]
        if baseline_validation is not None and len(validation_results) == len(
            development_pareto.eligible_ids
        ):
            validation_pareto = select_eligible_candidates(
                baseline_validation, tuple(validation_results), policy
            )
            final_ids = validation_pareto.eligible_ids
            validation_evidence = self._write_evidence(
                root,
                state,
                campaign_request,
                baseline_validation,
                tuple(validation_results),
                validation_pareto,
                "validation-evidence.json",
                eligible_only=development_pareto.eligible_ids,
            )
            validation_path = _relative_evidence(
                root, validation_evidence.path
            )
            validation_by_id = {
                result.run.subject_id: result
                for result in validation_results
            }
            state = replace(
                state,
                baseline_validation=baseline_validation,
                candidates=tuple(
                    self._finalize_candidate(
                        candidate,
                        final_ids,
                        development_path,
                        validation_path,
                        development_pareto.eligible_ids,
                        validation_by_id,
                    )
                    for candidate in state.candidates
                ),
            )
        else:
            state = replace(
                state,
                candidates=tuple(
                    self._finalize_candidate(
                        candidate,
                        final_ids,
                        development_path,
                        development_path,
                        (),
                        {},
                    )
                    for candidate in state.candidates
                ),
            )

        state = replace(
            state,
            status="completed",
            pareto_candidate_ids=final_ids,
            updated_at=self._deps.clock.now(),
        )
        self._deps.state.save(root, state)

        report = _report_from_state(state)
        publication = self._deps.publish.publish(
            CampaignPublicationInputs(
                repository_root=root,
                report=report,
                development_evidence=development_evidence,
                validation_evidence=validation_evidence,
                reproduction_instructions=_reproduction_instructions(
                    self._deps.config, state.target
                ),
            )
        )
        state = replace(
            state,
            finalized=publication,
            updated_at=self._deps.clock.now(),
        )
        self._deps.state.save(root, state)
        return self._finalized_result(phase, request.issue_number, state)

    def _finalize_candidate(
        self,
        candidate: CandidateState,
        final_ids: tuple[str, ...],
        development_path: Path,
        validation_path: Path,
        development_eligible: tuple[str, ...],
        validation_by_id: Mapping[str, EvaluationResult],
    ) -> CandidateState:
        if candidate.artifact is None:
            return candidate
        is_eligible = candidate.candidate_id in final_ids
        in_validation = candidate.candidate_id in development_eligible
        validation_result = validation_by_id.get(candidate.candidate_id)
        metrics = (
            _metrics(validation_result)
            if validation_result is not None
            else candidate.artifact.metrics
        )
        artifact = replace(
            candidate.artifact,
            evidence_path=(
                validation_path if in_validation else development_path
            ),
            eligible=is_eligible,
            metrics=metrics,
        )
        return replace(
            candidate,
            artifact=artifact,
            validation_result=(
                validation_result
                if validation_result is not None
                else candidate.validation_result
            ),
            provisional_eligible=is_eligible,
        )

    def _reconcile_held_out(
        self,
        root: Path,
        state: CampaignState,
        campaign_request: CampaignRequest,
        spec: OptimizationSpec,
        development_pareto: ParetoResult,
        results_by_id: Mapping[str, EvaluationResult],
        policy: EvaluationPolicy,
    ) -> CampaignState:
        """Run and persist held-out evaluations incrementally.

        The baseline held-out result is persisted the moment it completes, and
        every eligible candidate's held-out result is persisted immediately
        after its own evaluation and before the next network call. On a retry
        each already-persisted result is reused and only the missing held-out
        evaluations are run, so a crash mid-reconciliation never re-runs a
        completed evaluation.
        """
        evaluate = self._deps.bind_evaluation(spec, state.assets)
        if state.baseline_validation is None:
            baseline_validation = _evaluate(
                evaluate,
                _subject(
                    state.target,
                    "baseline",
                    _require_draft(state.baseline_draft),
                ),
                DatasetSplit.VALIDATION,
                policy,
            )
            state = replace(
                state,
                baseline_validation=baseline_validation,
                updated_at=self._deps.clock.now(),
            )
            self._deps.state.save(root, state)
        for candidate_id in development_pareto.eligible_ids:
            candidate = _candidate(state, candidate_id)
            if candidate is None or candidate.draft is None:
                raise CampaignRecoveryError(
                    "an eligible candidate is missing its persisted draft"
                )
            if candidate.validation_result is not None:
                continue
            result = _evaluate(
                evaluate,
                EvaluationSubject(
                    candidate_id,
                    results_by_id[candidate_id].run.agent,
                ),
                DatasetSplit.VALIDATION,
                policy,
            )
            candidate = replace(candidate, validation_result=result)
            state = replace(
                _replace_candidate(state, candidate),
                updated_at=self._deps.clock.now(),
            )
            self._deps.state.save(root, state)
        return state

    # -- AUTO ---------------------------------------------------------------

    def _auto(self, request: OptimizeCommandRequest) -> OptimizeCommandResult:
        root = _resolve_root(request.repository_root)
        campaign_id = _campaign_id(request.issue_number)
        state = self._deps.state.load(root, campaign_id)
        if state is None or state.baseline_development is None:
            if not self._spec_bundle_exists(root, request.issue_number):
                return self._spec(request)
            # Approved spec present but campaign not yet initialized -> RUN.
            return self._run(request)
        if state.finalized is not None:
            return self._finalized_result(
                OptimizePhase.AUTO, request.issue_number, state
            )
        if state.status in {"failed", "stale"}:
            return self._blocked(
                OptimizePhase.AUTO,
                request.issue_number,
                "campaign_requires_inspection",
                f"campaign {campaign_id} is {state.status} and needs "
                "inspection",
            )
        spec, spec_sha256, _ = self._load_spec_bundle(
            root, request.issue_number
        )
        campaign_request = self._rebuild_request(root, state, spec, spec_sha256)
        return self._drive(
            OptimizePhase.AUTO, request, root, state, spec, campaign_request
        )

    def _drive(
        self,
        phase: OptimizePhase,
        request: OptimizeCommandRequest,
        root: Path,
        state: CampaignState,
        spec: OptimizationSpec,
        campaign_request: CampaignRequest,
    ) -> OptimizeCommandResult:
        if state.awaiting_candidate_id is not None:
            candidate = _candidate(state, state.awaiting_candidate_id)
            if candidate is not None and (
                candidate.status in _IN_FLIGHT_CANDIDATE_STATUSES
            ):
                return self._awaiting_idea_result(phase, request, root, candidate)
        slots_used = state.launched_slots
        cutoff = self._cutoff_reached(state, campaign_request)
        if (
            not cutoff
            and slots_used < campaign_request.limits.max_changed_candidates
        ):
            return OptimizeCommandResult(
                status=OptimizeCommandStatus.AWAITING_AGENT,
                phase=phase,
                summary=(
                    "The campaign baseline is ready; request the next "
                    "candidate."
                ),
                issue_number=request.issue_number,
                details={
                    "campaign_id": state.campaign_id,
                    "launched_slots": slots_used,
                    "max_candidates": (
                        campaign_request.limits.max_changed_candidates
                    ),
                },
                next_action=(
                    "Run `foundry-opt optimize candidate request --issue "
                    f"{request.issue_number}`."
                ),
            )
        return self._finalize(
            phase, request, root, state, spec, campaign_request
        )

    # -- delegated phases ---------------------------------------------------

    def _delegate(
        self,
        request: OptimizeCommandRequest,
        service: Any | None,
        label: str,
    ) -> OptimizeCommandResult:
        if service is None:
            return self._blocked(
                request.phase,
                request.issue_number,
                f"{label}_not_wired",
                f"the {label} lifecycle is not wired in this build yet",
            )
        return service.execute(request)

    # -- result builders ----------------------------------------------------

    def _post_submit_result(
        self,
        request: OptimizeCommandRequest,
        state: CampaignState,
        campaign_request: CampaignRequest,
    ) -> OptimizeCommandResult:
        cutoff = self._cutoff_reached(state, campaign_request)
        remaining = (
            campaign_request.limits.max_changed_candidates
            - state.launched_slots
        )
        if not cutoff and remaining > 0:
            next_action = (
                "Run `foundry-opt optimize candidate request --issue "
                f"{request.issue_number}` for the next candidate."
            )
        else:
            next_action = (
                "Run `foundry-opt optimize run --issue "
                f"{request.issue_number}` to finalize the campaign."
            )
        return OptimizeCommandResult(
            status=OptimizeCommandStatus.AWAITING_AGENT,
            phase=OptimizePhase.CANDIDATE_SUBMIT,
            summary=(
                f"Candidate {request.candidate_id} was evaluated and "
                "recorded."
            ),
            issue_number=request.issue_number,
            details={
                "campaign_id": state.campaign_id,
                "candidate_id": request.candidate_id,
                "launched_slots": state.launched_slots,
                "remaining_slots": max(0, remaining),
            },
            next_action=next_action,
        )

    def _awaiting_idea_result(
        self,
        phase: OptimizePhase,
        request: OptimizeCommandRequest,
        root: Path,
        candidate: CandidateState,
    ) -> OptimizeCommandResult:
        worktree_root = contained_worktree_root(root, _campaign_id(
            request.issue_number
        ))
        worktree_path = worktree_root / candidate.candidate_id
        return OptimizeCommandResult(
            status=OptimizeCommandStatus.AWAITING_AGENT,
            phase=phase,
            summary=(
                f"Candidate {candidate.candidate_id} is reserved; edit the "
                "worktree and submit an idea file."
            ),
            issue_number=request.issue_number,
            details={
                "candidate_id": candidate.candidate_id,
                "context_path": candidate.context_path or "",
                "worktree": worktree_path.as_posix(),
            },
            next_action=(
                "Edit only the worktree, then run `foundry-opt optimize "
                f"candidate submit --issue {request.issue_number} --candidate "
                f"{candidate.candidate_id} --idea-file <path>`."
            ),
        )

    def _finalize_ready(
        self,
        phase: OptimizePhase,
        request: OptimizeCommandRequest,
        reason: str,
    ) -> OptimizeCommandResult:
        return OptimizeCommandResult(
            status=OptimizeCommandStatus.AWAITING_AGENT,
            phase=phase,
            summary=f"No further candidates: {reason}.",
            issue_number=request.issue_number,
            details={"campaign_id": _campaign_id(request.issue_number)},
            next_action=(
                "Run `foundry-opt optimize run --issue "
                f"{request.issue_number}` to finalize the campaign."
            ),
        )

    def _finalized_result(
        self,
        phase: OptimizePhase,
        issue_number: int,
        state: CampaignState,
    ) -> OptimizeCommandResult:
        finalized = state.finalized
        assert finalized is not None
        return OptimizeCommandResult(
            status=OptimizeCommandStatus.COMPLETE,
            phase=phase,
            summary=(
                f"Campaign {state.campaign_id} is finalized and published."
            ),
            issue_number=issue_number,
            details={
                "campaign_id": state.campaign_id,
                "campaign_pull_request": (
                    finalized.campaign_pull_request_number
                ),
                "eligible_candidates": list(state.pareto_candidate_ids),
                "candidate_issues": dict(finalized.candidate_issue_numbers),
            },
        )

    def _blocked(
        self,
        phase: OptimizePhase,
        issue_number: int,
        code: str,
        message: str,
    ) -> OptimizeCommandResult:
        return OptimizeCommandResult(
            status=OptimizeCommandStatus.BLOCKED,
            phase=phase,
            summary=message,
            issue_number=issue_number,
            details={"code": code},
        )

    # -- guards -------------------------------------------------------------

    def _require_active(
        self,
        phase: OptimizePhase,
        request: OptimizeCommandRequest,
        state: CampaignState | None,
    ) -> OptimizeCommandResult | None:
        if state is None:
            return self._blocked(
                phase,
                request.issue_number,
                "campaign_not_started",
                "no active campaign exists; run `optimize run` first",
            )
        if state.finalized is not None or state.status == "completed":
            return self._blocked(
                phase,
                request.issue_number,
                "campaign_finalized",
                "the campaign is already finalized",
            )
        if state.status in {"failed", "stale"}:
            return self._blocked(
                phase,
                request.issue_number,
                "campaign_requires_inspection",
                f"campaign {state.campaign_id} is {state.status} and needs "
                "inspection",
            )
        if state.baseline_development is None:
            return self._blocked(
                phase,
                request.issue_number,
                "baseline_not_established",
                "the campaign baseline has not been established; run "
                "`optimize run` first",
            )
        return None

    # -- spec + assets ------------------------------------------------------

    def _load_spec_bundle(
        self,
        root: Path,
        issue_number: int,
    ) -> tuple[OptimizationSpec, str, Mapping[str, Path | None]]:
        spec_path = root / spec_file_path(issue_number)
        provenance_path = root / provenance_file_path(issue_number)
        spec_text = _read_repo_file(root, spec_path)
        provenance_text = _read_repo_file(root, provenance_path)
        try:
            spec_document = yaml.safe_load(spec_text)
            spec = OptimizationSpec.model_validate(spec_document)
        except Exception as error:
            raise CapabilityUnavailableError(
                "spec_document_invalid",
                "the merged optimization spec could not be parsed",
            ) from error
        spec_sha256 = spec.sha256
        try:
            provenance = json.loads(provenance_text)
        except json.JSONDecodeError as error:
            raise CapabilityUnavailableError(
                "provenance_document_invalid",
                "the spec provenance document could not be parsed",
            ) from error
        if (
            not isinstance(provenance, dict)
            or provenance.get("spec_sha256") != spec_sha256
            or int(provenance.get("issue_number", -1)) != issue_number
            or provenance.get("base_commit") != spec.base_commit
        ):
            raise CapabilityUnavailableError(
                "spec_provenance_mismatch",
                "the spec and its provenance record disagree",
            )
        asset_paths: dict[str, Path | None] = {}
        for entry in (
            *provenance.get("datasets", ()),
            *provenance.get("evaluators", ()),
        ):
            if not isinstance(entry, dict):
                raise CapabilityUnavailableError(
                    "spec_provenance_mismatch",
                    "the spec provenance record is malformed",
                )
            raw_path = entry.get("path")
            asset_paths[str(entry["asset_id"])] = (
                Path(str(raw_path)) if raw_path is not None else None
            )
        return spec, spec_sha256, asset_paths

    def _spec_bundle_exists(self, root: Path, issue_number: int) -> bool:
        spec_path = root / spec_file_path(issue_number)
        provenance_path = root / provenance_file_path(issue_number)
        return spec_path.is_file() and provenance_path.is_file()

    def _materialize_assets(
        self,
        root: Path,
        spec: OptimizationSpec,
        asset_paths: Mapping[str, Path | None],
    ) -> tuple[EvaluationAssetReference, ...]:
        references: list[EvaluationAssetReference] = []
        for provenance in (*spec.datasets, *spec.evaluators):
            path = asset_paths.get(provenance.asset_id)
            materialized = self._materialize_single(root, provenance, path)
            references.append(_asset_reference(materialized))
        return tuple(references)

    def _materialize_single(
        self,
        root: Path,
        provenance: AssetProvenance,
        path: Path | None,
    ) -> AssetProvenance:
        if path is None:
            if provenance.remote_id is None:
                raise CapabilityUnavailableError(
                    "asset_not_registrable",
                    f"asset {provenance.asset_id} has neither a file nor a "
                    "remote identity",
                )
            return provenance
        absolute = root / path
        content = _read_repo_file(root, absolute)
        if (
            provenance.content_sha256 is not None
            and hashlib.sha256(content).hexdigest() != provenance.content_sha256
        ):
            raise CapabilityUnavailableError(
                "asset_content_tampered",
                f"asset {provenance.asset_id} content does not match its "
                "pinned hash",
            )
        prepared = PreparedEvaluationAsset(
            provenance=provenance, files={path: content}
        )
        try:
            return materialize_prepared_asset(
                prepared, self._deps.registration_gateway
            )
        except TraceAssetRegistrationBlockedError as error:
            raise CapabilityUnavailableError(
                "trace_requires_human_review", str(error)
            ) from error
        except EvaluationAssetError as error:
            raise CapabilityUnavailableError(
                "asset_registration_failed", str(error)
            ) from error

    # -- context + idea IO --------------------------------------------------

    def _write_context(
        self,
        root: Path,
        campaign_id: str,
        candidate_id: str,
        slot: int,
        state: CampaignState,
        worktree: CampaignWorktree,
        request: CampaignRequest,
    ) -> tuple[Path, str]:
        started = state.started_at
        cutoff_at = started + timedelta(
            minutes=request.limits.candidate_cutoff_minutes
        )
        deadline_at = started + timedelta(
            minutes=request.limits.deadline_minutes
        )
        document = {
            "allowed_edit_paths": [
                path.as_posix() for path in request.edit_paths
            ],
            "allowed_mutations": sorted(request.allowed_mutations),
            "base_commit": state.base_commit,
            "baseline_metrics": dict(state.baseline_metrics),
            "campaign_id": campaign_id,
            "candidate_id": candidate_id,
            "cutoff_at": cutoff_at.isoformat(),
            "deadline_at": deadline_at.isoformat(),
            "goal": request.goal,
            "prior_candidates": [
                _prior_candidate_document(candidate)
                for candidate in state.candidates
                if candidate.candidate_id != candidate_id
                and candidate.lineage is not None
            ],
            "restricted_opt_ins": dict(request.restricted_opt_ins),
            "schema_version": 1,
            "slot": slot,
            "target": state.target,
            "worktree": worktree.path.as_posix(),
        }
        content = (
            json.dumps(
                document,
                indent=2,
                sort_keys=True,
                separators=(",", ": "),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        # Defence in depth: the context must never carry secret-shaped text.
        reject_secret_content(content.decode("utf-8"))
        relative = (
            Path(".foundry-optimizer")
            / "campaigns"
            / campaign_id
            / "candidates"
            / candidate_id
            / "context.json"
        )
        target = root / relative
        _atomic_write(target, content, root)
        return relative, hashlib.sha256(content).hexdigest()

    def _read_idea(
        self,
        root: Path,
        idea_file: Path | None,
        worktree: CampaignWorktree,
    ) -> tuple[CandidateIdea, Path, str]:
        if idea_file is None:
            raise IdeaContractError("an idea file is required")
        candidate_path = idea_file
        if not candidate_path.is_absolute():
            candidate_path = root / candidate_path
        # Reject symlinked path components before resolving.
        current = root
        try:
            relative = candidate_path.resolve().relative_to(root)
        except (OSError, ValueError) as error:
            raise IdeaContractError(
                "the idea file must be inside the repository"
            ) from error
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise IdeaContractError(
                    "the idea file path must not contain symlinks"
                )
        worktree_resolved = worktree.path.expanduser().resolve()
        resolved = (root / relative).resolve()
        if resolved.is_relative_to(worktree_resolved):
            raise IdeaContractError(
                "the idea file must live outside the candidate worktree"
            )
        if not resolved.is_file():
            raise IdeaContractError("the idea file does not exist")
        content = resolved.read_bytes()
        idea = _parse_idea(content)
        return idea, relative, hashlib.sha256(content).hexdigest()

    # -- helpers ------------------------------------------------------------

    def _rebuild_request(
        self,
        root: Path,
        state: CampaignState,
        spec: OptimizationSpec,
        spec_sha256: str,
    ) -> CampaignRequest:
        return self._campaign_request(
            state.campaign_id,
            spec,
            spec_sha256,
            state.assets,
            root,
        )

    def _campaign_request(
        self,
        campaign_id: str,
        spec: OptimizationSpec,
        spec_sha256: str,
        assets: tuple[EvaluationAssetReference, ...],
        root: Path,
    ) -> CampaignRequest:
        target = self._deps.config.targets.get(spec.target)
        if target is None:
            raise CapabilityUnavailableError(
                "unknown_target",
                f"target {spec.target!r} is not configured",
            )
        limits = _campaign_limits(self._deps.config, target)
        stale_after = timedelta(
            hours=max(2, self._deps.config.campaign.stale_after_hours)
        )
        request = CampaignRequest(
            campaign_id=campaign_id,
            target=spec.target,
            repository_root=root,
            limits=limits,
            edit_paths=tuple(Path(str(path)) for path in target.edit_paths),
            allowed_mutations=frozenset(
                mutation.value for mutation in spec.allowed_mutations
            ),
            evaluation_policy=_evaluation_policy(spec),
            goal=spec.goal,
            spec_sha256=spec_sha256,
            assets=assets,
            restricted_opt_ins=_restricted_opt_ins(spec),
            evidence_root=Path(str(self._deps.config.campaign.evidence_path)),
            stale_after=stale_after,
        )
        return request

    def _require_worktree(
        self,
        root: Path,
        campaign_id: str,
        candidate: CandidateState,
        base_commit: str,
    ) -> CampaignWorktree:
        return self._deps.repository.open_worktree(
            root, campaign_id, candidate.candidate_id, base_commit
        )

    def _provisional_pareto(
        self,
        state: CampaignState,
        policy: EvaluationPolicy,
    ) -> ParetoResult:
        baseline = state.baseline_development
        assert baseline is not None
        development_results = tuple(
            candidate.development_result
            for candidate in state.candidates
            if candidate.development_result is not None
        )
        return select_eligible_candidates(
            baseline,
            development_results,
            policy,
        )

    def _write_evidence(
        self,
        root: Path,
        state: CampaignState,
        request: CampaignRequest,
        baseline: EvaluationResult,
        candidates: tuple[EvaluationResult, ...],
        pareto: ParetoResult,
        filename: str,
        *,
        eligible_only: tuple[str, ...] | None = None,
    ) -> EvidenceManifest:
        output = _evidence_output(root, request.evidence_root, state.campaign_id, filename)
        patch_hashes = {
            candidate.artifact.candidate_id: candidate.artifact.patch.sha256
            for candidate in state.candidates
            if candidate.artifact is not None
            and (
                eligible_only is None
                or candidate.candidate_id in eligible_only
            )
        }
        return self._deps.write_evidence(
            EvidenceRequest(
                output_path=output,
                campaign_id=state.campaign_id,
                baseline=baseline,
                candidates=candidates,
                pareto=pareto,
                metric_policies=request.evaluation_policy,
                source_hash=_require_draft_sha(state.baseline_draft),
                goal=request.goal,
                spec_sha256=state.spec_sha256,
                assets=state.assets,
                patch_hashes=patch_hashes,
            )
        )

    def _cutoff_reached(
        self,
        state: CampaignState,
        request: CampaignRequest,
    ) -> bool:
        elapsed = self._deps.clock.now() - state.started_at
        return (
            elapsed.total_seconds()
            >= request.limits.candidate_cutoff_minutes * 60
        )

    def _deadline_reached(
        self,
        state: CampaignState,
        request: CampaignRequest,
    ) -> bool:
        elapsed = self._deps.clock.now() - state.started_at
        return elapsed.total_seconds() >= request.limits.deadline_minutes * 60

    def _fail_campaign(
        self,
        root: Path,
        campaign_id: str,
        error: Exception,
    ) -> None:
        try:
            state = self._deps.state.load(root, campaign_id)
            if state is not None and state.status == "active":
                self._deps.state.save(
                    root,
                    replace(
                        state,
                        status="failed",
                        error_code=type(error).__name__,
                        updated_at=self._deps.clock.now(),
                    ),
                )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _resolve_root(path: Path) -> Path:
    return path.expanduser().resolve()


def _campaign_id(issue_number: int) -> str:
    return f"issue-{issue_number}"


def _campaign_limits(config: OptimizerConfig, target: Any) -> CampaignLimits:
    defaults = config.campaign
    overrides = getattr(target, "campaign_overrides", None)

    def pick(attribute: str) -> int:
        if overrides is not None:
            value = getattr(overrides, attribute)
            if value is not None:
                return int(value)
        return int(getattr(defaults, attribute))

    return CampaignLimits(
        deadline_minutes=pick("deadline_minutes"),
        candidate_cutoff_minutes=pick("candidate_cutoff_minutes"),
        max_changed_candidates=pick("max_changed_candidates"),
        transient_retries=pick("transient_retries"),
    )


def _evaluation_policy(spec: OptimizationSpec) -> EvaluationPolicy:
    metrics = tuple(
        MetricPolicy(
            name=name,
            direction=(
                MetricDirection.MAXIMIZE
                if policy.direction is ConfigMetricDirection.MAXIMIZE
                else MetricDirection.MINIMIZE
            ),
            threshold=float(policy.threshold),
            materiality=float(policy.materiality),
            hard_guardrail=bool(policy.hard_guardrail),
            undefined_behavior=(
                UndefinedBehavior.FAIL
                if policy.undefined_behavior is ConfigUndefinedBehavior.FAIL
                else UndefinedBehavior.IGNORE
            ),
        )
        for name, policy in spec.metrics.items()
    )
    return EvaluationPolicy(metrics)


def _restricted_opt_ins(spec: OptimizationSpec) -> Mapping[str, bool]:
    return {
        str(name): bool(value)
        for name, value in spec.restricted_opt_ins.model_dump().items()
    }


def _asset_reference(provenance: AssetProvenance) -> EvaluationAssetReference:
    return EvaluationAssetReference(
        asset_id=provenance.asset_id,
        kind=provenance.kind.value,
        source=provenance.source,
        role=provenance.role,
        name=provenance.name,
        version=provenance.version,
        remote_id=provenance.remote_id,
        content_sha256=provenance.content_sha256,
        approval_gate=provenance.approval_gate.value,
        metrics=provenance.metrics,
    )


def _asset_key(
    assets: tuple[EvaluationAssetReference, ...],
) -> tuple[tuple[str, ...], ...]:
    return tuple(
        sorted(
            (
                asset.asset_id,
                asset.kind,
                asset.source,
                asset.role or "",
                asset.name or "",
                asset.version or "",
                asset.remote_id or "",
                asset.content_sha256 or "",
                asset.approval_gate,
            )
            for asset in assets
        )
    )


def _state_mismatch(
    state: CampaignState,
    request: CampaignRequest,
    base_commit: str,
) -> str | None:
    goal_sha256 = hashlib.sha256(request.goal.encode("utf-8")).hexdigest()
    if state.target != request.target:
        return "the campaign target changed since it started"
    if state.base_commit != base_commit:
        return "the default-branch base commit changed since the campaign "\
            "started"
    if state.goal_sha256 != goal_sha256:
        return "the optimization goal changed since the campaign started"
    if state.spec_sha256 != request.spec_sha256:
        return "the specification changed since the campaign started"
    if _asset_key(state.assets) != _asset_key(request.assets):
        return "the evaluation assets changed since the campaign started"
    return None


def _draft_intent(
    request: CampaignRequest,
    subject_id: str,
    base_commit: str,
    bundle_sha256: str,
) -> DraftCreationIntent:
    payload = "\0".join(
        (
            request.campaign_id,
            request.target,
            subject_id,
            base_commit,
            bundle_sha256,
        )
    ).encode("utf-8")
    return DraftCreationIntent(
        subject_id=subject_id,
        idempotency_key=hashlib.sha256(payload).hexdigest(),
    )


def _subject(target: str, subject_id: str, draft_id: str) -> EvaluationSubject:
    return EvaluationSubject(
        subject_id,
        AgentVersionRef(target, draft_id, draft_id),
    )


def _evaluate(
    evaluate: EvaluationRunner,
    subject: EvaluationSubject,
    split: DatasetSplit,
    policy: EvaluationPolicy,
) -> EvaluationResult:
    return evaluate_with_repeat(subject, split, policy, evaluate)


def _metrics(result: EvaluationResult) -> dict[str, float]:
    return {
        name: aggregate.median
        for name, aggregate in result.metrics.items()
        if aggregate.median is not None
    }


def _enforce_lineage(idea: CandidateIdea, state: CampaignState) -> None:
    known = {
        candidate.lineage.idea_id
        for candidate in state.candidates
        if candidate.lineage is not None
    }
    if not set(idea.parent_idea_ids).issubset(known):
        raise ValueError("the idea references an unknown parent idea")


def _enforce_mutation(request: CampaignRequest, idea: CandidateIdea) -> None:
    if idea.mutation_class not in request.allowed_mutations:
        raise ValueError(
            f"mutation class is not allowed: {idea.mutation_class}"
        )
    missing = tuple(
        opt_in
        for opt_in in idea.required_opt_ins
        if not request.restricted_opt_ins.get(opt_in, False)
    )
    if missing:
        raise ValueError(
            "the idea requires disabled opt-ins: " + ", ".join(sorted(missing))
        )


def _enforce_paths(
    allowed_roots: tuple[Path, ...],
    changed_paths: tuple[Path, ...],
) -> None:
    for changed in changed_paths:
        normalized = Path(str(changed).replace("\\", "/"))
        if not any(
            normalized == root or normalized.is_relative_to(root)
            for root in allowed_roots
        ):
            raise ValueError(f"the candidate changed a disallowed path: {changed}")


def _replace_candidate(
    state: CampaignState,
    candidate: CandidateState,
) -> CampaignState:
    return replace(
        state,
        candidates=tuple(
            candidate
            if existing.candidate_id == candidate.candidate_id
            else existing
            for existing in state.candidates
        ),
    )


def _candidate(
    state: CampaignState,
    candidate_id: str,
) -> CandidateState | None:
    for candidate in state.candidates:
        if candidate.candidate_id == candidate_id:
            return candidate
    return None


def _report_from_state(state: CampaignState) -> CampaignReport:
    if state.baseline_draft_id is None:
        raise CapabilityUnavailableError(
            "baseline_draft_missing",
            "the campaign has no baseline draft to report",
        )
    return CampaignReport(
        campaign_id=state.campaign_id,
        target=state.target,
        base_commit=state.base_commit,
        baseline_draft_id=state.baseline_draft_id,
        candidates=tuple(
            candidate.artifact
            for candidate in state.candidates
            if candidate.artifact is not None
        ),
        pareto_candidate_ids=state.pareto_candidate_ids,
        goal_sha256=state.goal_sha256,
        spec_sha256=state.spec_sha256,
        assets=state.assets,
    )


def _reproduction_instructions(
    config: OptimizerConfig,
    target: str,
) -> tuple[str, ...]:
    return (
        "Check out the exact campaign base commit and apply the candidate's "
        "exact patch.",
        "Re-run the target's configured validation commands and re-evaluate "
        "on the pinned datasets to reproduce the recorded metrics.",
    )


def _relative_evidence(root: Path, path: Path) -> Path:
    resolved = path if path.is_absolute() else root / path
    resolved = resolved.expanduser().resolve()
    resolved_root = root.expanduser().resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ValueError("evidence path escapes the repository")
    return resolved.relative_to(resolved_root)


def _evidence_output(
    root: Path,
    evidence_root: Path,
    campaign_id: str,
    filename: str,
) -> Path:
    output = root / evidence_root / campaign_id / filename
    resolved_parent = output.parent.expanduser().resolve()
    if not resolved_parent.is_relative_to(root.expanduser().resolve()):
        raise ValueError("evidence path escapes the repository")
    output.parent.mkdir(parents=True, exist_ok=True)
    # The evidence writer is idempotent: a byte-identical deterministic
    # serialization reuses the existing artifact, so finalization retries do
    # not clobber or race on their own prior output.
    return output


def _require_draft(draft: DraftMetadata | None) -> str:
    if draft is None:
        raise CapabilityUnavailableError(
            "baseline_draft_missing",
            "the campaign baseline draft is missing",
        )
    return draft.version_id


def _require_draft_sha(draft: DraftMetadata | None) -> str:
    if draft is None:
        raise CapabilityUnavailableError(
            "baseline_draft_missing",
            "the campaign baseline draft is missing",
        )
    return draft.sha256


def _prior_candidate_document(candidate: CandidateState) -> dict[str, Any]:
    lineage = candidate.lineage
    return {
        "candidate_id": candidate.candidate_id,
        "changed_paths": (
            [path.as_posix() for path in lineage.changed_paths]
            if lineage is not None
            else []
        ),
        "idea_id": lineage.idea_id if lineage is not None else None,
        "metrics": dict(candidate.metrics),
        "mutation_class": (
            lineage.mutation_class if lineage is not None else None
        ),
        "parent_idea_ids": (
            list(lineage.parent_idea_ids) if lineage is not None else []
        ),
        "provisional_eligible": candidate.provisional_eligible,
        "status": candidate.status,
    }


def _read_repo_file(root: Path, path: Path) -> bytes:
    resolved_root = root.expanduser().resolve()
    try:
        relative = path.expanduser().resolve().relative_to(resolved_root)
    except (OSError, ValueError) as error:
        raise CapabilityUnavailableError(
            "path_escapes_repository",
            "a required file is outside the repository",
        ) from error
    current = resolved_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise CapabilityUnavailableError(
                "unsafe_repository_path",
                "a required file path contains a symlink",
            )
    if not current.is_file():
        raise CapabilityUnavailableError(
            "missing_repository_file",
            f"required file {relative.as_posix()} does not exist",
        )
    return current.read_bytes()


def _atomic_write(target: Path, content: bytes, root: Path) -> None:
    _ensure_safe_directory(target.parent, root)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.writing")
    try:
        with temporary.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _ensure_safe_directory(path: Path, root: Path) -> None:
    resolved_root = root.expanduser().resolve()
    relative = path.expanduser().resolve().relative_to(resolved_root)
    current = resolved_root
    for part in relative.parts:
        current = current / part
        if os.path.lexists(current) and current.is_symlink():
            raise ValueError("context directory cannot contain symlinks")
        current.mkdir(exist_ok=True)


def _parse_idea(content: bytes) -> CandidateIdea:
    try:
        document = json.loads(content)
    except json.JSONDecodeError as error:
        raise IdeaContractError("the idea file is not valid JSON") from error
    if not isinstance(document, dict):
        raise IdeaContractError("the idea file must be a JSON object")
    allowed = {
        "idea_id",
        "mutation_class",
        "parent_idea_ids",
        "required_opt_ins",
        "hypothesis",
        "motivation",
    }
    unexpected = set(document) - allowed
    if unexpected:
        raise IdeaContractError(
            "the idea file contains unsupported fields: "
            + ", ".join(sorted(str(field) for field in unexpected))
        )
    if "idea_id" not in document or "mutation_class" not in document:
        raise IdeaContractError(
            "the idea file requires idea_id and mutation_class"
        )
    parents = document.get("parent_idea_ids", ())
    opt_ins = document.get("required_opt_ins", ())
    if not isinstance(parents, list) or not all(
        isinstance(value, str) for value in parents
    ):
        raise IdeaContractError("parent_idea_ids must be a list of strings")
    if not isinstance(opt_ins, list) or not all(
        isinstance(value, str) for value in opt_ins
    ):
        raise IdeaContractError("required_opt_ins must be a list of strings")
    for optional in ("hypothesis", "motivation"):
        value = document.get(optional)
        if value is not None:
            if not isinstance(value, str) or len(value) > 2000:
                raise IdeaContractError(
                    f"{optional} must be a short string when provided"
                )
            reject_secret_content(value)
    try:
        return CandidateIdea(
            idea_id=str(document["idea_id"]),
            mutation_class=str(document["mutation_class"]),
            parent_idea_ids=tuple(parents),
            required_opt_ins=frozenset(opt_ins),
        )
    except ValueError as error:
        raise IdeaContractError(str(error)) from error


def _spec_result_to_command(
    issue_number: int,
    result: Any,
) -> OptimizeCommandResult:
    from foundry_opt.optimization.specification import SpecServiceStatus

    details: dict[str, Any] = {}
    pull_request = getattr(result, "pull_request", None)
    if pull_request is not None:
        details["pull_request"] = pull_request.number
    if getattr(result, "spec_sha256", None) is not None:
        details["spec_sha256"] = result.spec_sha256
    status = result.status
    if status is SpecServiceStatus.COMPLETE:
        return OptimizeCommandResult(
            status=OptimizeCommandStatus.COMPLETE,
            phase=OptimizePhase.SPEC,
            summary=(
                "A draft optimization specification pull request is ready "
                "for review."
            ),
            issue_number=issue_number,
            details=details,
            next_action=(
                "A maintainer must merge the specification pull request to "
                "record approval, then run `foundry-opt optimize run "
                f"--issue {issue_number}`."
            ),
        )
    if status is SpecServiceStatus.PARTIAL:
        details["failures"] = [
            failure.code for failure in getattr(result, "failures", ())
        ]
        return OptimizeCommandResult(
            status=OptimizeCommandStatus.COMPLETE,
            phase=OptimizePhase.SPEC,
            summary=(
                "The specification pull request is ready; some issue updates "
                "still need attention."
            ),
            issue_number=issue_number,
            details=details,
            next_action=(
                "Review the reported issue-update failures, then merge the "
                "specification pull request to record approval."
            ),
        )
    blockers = getattr(result, "blockers", ())
    reason = blockers[0] if blockers else "specification preparation is blocked"
    if status is SpecServiceStatus.CONFLICT:
        details["code"] = "spec_conflict"
    else:
        details["code"] = "spec_blocked"
    return OptimizeCommandResult(
        status=OptimizeCommandStatus.BLOCKED,
        phase=OptimizePhase.SPEC,
        summary=reason,
        issue_number=issue_number,
        details=details,
    )
