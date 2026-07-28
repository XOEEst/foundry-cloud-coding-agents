"""OIDC-backed post-deployment evaluation of a published selection.

This module owns the production :class:`LivePostDeployEvaluator`, the
adapter that satisfies the reconcile lifecycle's ``PostDeployEvaluator`` seam
(:mod:`foundry_opt.optimization.lifecycle`). After the deployment coordinator
has *verified* the published Foundry deployment, the reconcile service asks
this adapter to confirm the deployed selection is still a *retained
improvement* before it closes the parent optimization issue.

Design
------
The adapter never re-runs the baseline or the selected draft: those validation
results were produced during the campaign, persisted losslessly in the
campaign state, selected on the validation Pareto frontier, and pinned as
deployment evidence. Re-running them would spend budget and could drift from
the exact dataset/evaluator/case lineage the selection was made against.
Instead the adapter:

* Loads the finalized campaign state and candidate evidence addressed by the
  deployment lineage (campaign, candidate, spec), and fails closed unless the
  ``deployment_version`` is a concrete published version and the selected
  candidate's patch/evidence/draft identity matches the lineage exactly.
* Builds a published :class:`~foundry_opt.evaluation.EvaluationSubject` for the
  *exact* published agent name and numeric version — never the campaign draft —
  and replays the *pinned* validation split through the real
  :class:`~foundry_opt.adapters.optimization_evaluation.OptimizationEvaluationBinder`
  and the campaign's approved, materialized asset references, honoring the
  bounded-repeat policy via
  :func:`~foundry_opt.evaluation.evaluate_with_repeat`.
* Reuses the persisted ``baseline_validation`` and the selected candidate's
  ``validation_result`` and compares the published result against both using
  the exact dataset/evaluator/case lineage, hard guardrails, materiality, and
  the configured metric policy
  (:func:`~foundry_opt.evaluation.select_eligible_candidates`).

Outcomes
--------
* ``RETAINED_IMPROVEMENT`` — the published version is still eligible against
  the baseline (passes guardrails and materially improves it) and is not worse
  than the selected draft beyond the repeat/noise policy.
* ``REGRESSED`` — the published version lost against the baseline, failed a
  hard guardrail, drifted from the pinned dataset/evaluator/case lineage, or is
  materially worse than the selected draft.
* ``PENDING`` — the provider run has not completed; the reconcile service
  re-runs later.

Only identity, provenance, and aggregate metrics ever leave this module; raw
prompts, responses, or dataset rows never do. Any missing/mismatched
provenance or Azure OIDC/provider failure fails closed with the typed
:class:`~foundry_opt.optimization.runner.CapabilityUnavailableError` so the
lifecycle blocks honestly rather than fabricating a retained improvement.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from pathlib import Path

from foundry_opt.adapters.optimization_evaluation import (
    AzureCredentialProvider,
    EvaluationRunner,
    OptimizationEvaluationBinder,
    OptimizationEvaluationError,
    build_evaluation_policy,
)
from foundry_opt.campaign.models import CandidateArtifact
from foundry_opt.campaign.state import (
    CampaignState,
    CampaignStateStore,
    CandidateState,
)
from foundry_opt.evaluation import (
    AgentVersionRef,
    DatasetSplit,
    EvaluationPolicy,
    EvaluationResult,
    EvaluationStatus,
    EvaluationSubject,
    MetricDirection,
    Outcome,
    UndefinedBehavior,
    evaluate_with_repeat,
    select_eligible_candidates,
)
from foundry_opt.evidence import EvaluationAssetReference
from foundry_opt.optimization.lifecycle import (
    PostDeployOutcome,
    PostDeployRequest,
    PostDeployStatus,
)
from foundry_opt.optimization.models import OptimizationSpec
from foundry_opt.optimization.runner import CapabilityUnavailableError
from foundry_opt.preflight.redaction import redact


__all__ = [
    "EvaluationBinder",
    "EvaluationBinderFactory",
    "LivePostDeployEvaluator",
    "build_live_post_deploy_evaluator",
]


# The binder returned by the factory is itself the runner ``EvaluationBinder``:
# calling it with the approved spec and its materialized assets returns an
# ``EvaluationRunner`` (``(subject, split, attempt) -> EvaluationResult``).
EvaluationBinder = Callable[
    [OptimizationSpec, Sequence[EvaluationAssetReference]],
    EvaluationRunner,
]
EvaluationBinderFactory = Callable[[str], EvaluationBinder]


class LivePostDeployEvaluator:
    """Re-evaluate the published selection and confirm a retained improvement.

    ``binder_factory`` builds a per-project evaluation binder (the production
    default wraps :class:`OptimizationEvaluationBinder`); ``state_store`` reads
    the finalized campaign state (the production default is the on-disk
    :class:`~foundry_opt.campaign.state.FileCampaignStateStore`).
    """

    def __init__(
        self,
        *,
        binder_factory: EvaluationBinderFactory,
        state_store: CampaignStateStore | None = None,
    ) -> None:
        self._binder_factory = binder_factory
        self._state_store = state_store or _file_campaign_state_store()

    # -- PostDeployEvaluator seam ------------------------------------------

    def evaluate(self, request: PostDeployRequest) -> PostDeployOutcome:
        lineage = request.lineage
        spec = request.spec
        root = request.repository_root.expanduser().resolve()

        version = request.deployment_version
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version < 1
        ):
            raise CapabilityUnavailableError(
                "post_deploy_deployment_version_invalid",
                "the post-deployment evaluation requires a concrete published "
                "deployment version",
            )

        state = self._state_store.load(root, lineage.campaign_id)
        if (
            state is None
            or state.finalized is None
            or state.status != "completed"
        ):
            raise CapabilityUnavailableError(
                "post_deploy_state_missing",
                "no finalized campaign state exists for the deployed "
                "selection's lineage",
            )

        candidate = self._verify_identity(request, state, root)
        baseline = _require_evaluable(
            state.baseline_validation,
            "post_deploy_baseline_unavailable",
            "the persisted baseline validation result is missing or "
            "incomplete",
        )
        selected = _require_evaluable(
            candidate.validation_result,
            "post_deploy_selection_unavailable",
            "the persisted selected candidate validation result is missing or "
            "incomplete",
        )
        if baseline.run.agent.agent_id != spec.target:
            raise CapabilityUnavailableError(
                "post_deploy_lineage_mismatch",
                "the persisted baseline agent does not match the approved "
                "specification target",
            )

        policy = build_evaluation_policy(spec)
        published = self._run_published_validation(
            request, state, version, policy
        )
        return _decide(published, baseline, selected, policy)

    # -- identity / provenance ---------------------------------------------

    def _verify_identity(
        self,
        request: PostDeployRequest,
        state: CampaignState,
        root: Path,
    ) -> CandidateState:
        lineage = request.lineage
        spec = request.spec
        finalized = state.finalized
        assert finalized is not None  # narrowed by evaluate()

        if (
            spec.sha256 != lineage.spec_sha256
            or state.spec_sha256 != lineage.spec_sha256
        ):
            raise _lineage_error("the approved specification identity")
        if request.selected_candidate_id != lineage.candidate_id:
            raise _lineage_error("the selected candidate identity")
        if state.campaign_id != lineage.campaign_id:
            raise _lineage_error("the campaign identity")

        candidate = _find_candidate(state, lineage.candidate_id)
        if candidate is None or candidate.artifact is None:
            raise _lineage_error("the finalized selected candidate artifact")
        artifact = candidate.artifact

        if artifact.patch.sha256 != lineage.patch_sha256:
            raise _lineage_error("the selected candidate patch")
        if artifact.draft_id != lineage.selected_draft_id:
            raise _lineage_error("the selected draft")

        evidence_sha256 = _evidence_sha256(root, artifact)
        if evidence_sha256 is None:
            raise _lineage_error(
                "the finalized candidate evidence (missing or unreadable)"
            )
        if evidence_sha256 != lineage.evidence_sha256:
            raise _lineage_error("the selected candidate evidence")

        if (
            finalized.campaign_pull_request_number
            != lineage.campaign_pull_request_number
            or finalized.candidate_issue_numbers.get(lineage.candidate_id)
            != lineage.candidate_issue_number
        ):
            raise _lineage_error("the finalized campaign publication")
        return candidate

    # -- published replay --------------------------------------------------

    def _run_published_validation(
        self,
        request: PostDeployRequest,
        state: CampaignState,
        version: int,
        policy: EvaluationPolicy,
    ) -> EvaluationResult:
        spec = request.spec
        published_version = str(version)
        # Pin the exact published agent name and numeric version; the deployed
        # version is not a campaign draft, so the reference intentionally
        # carries the published version rather than a draft id.
        subject = EvaluationSubject(
            f"published-{request.selected_candidate_id}",
            AgentVersionRef(spec.target, published_version, published_version),
        )
        try:
            binder = self._binder_factory(request.project_endpoint)
            runner = binder(spec, state.assets)
            return evaluate_with_repeat(
                subject, DatasetSplit.VALIDATION, policy, runner
            )
        except CapabilityUnavailableError:
            raise
        except OptimizationEvaluationError as error:
            raise CapabilityUnavailableError(
                "post_deploy_unavailable",
                "the post-deployment validation replay failed: "
                + redact(str(error)),
            ) from error
        except Exception as error:  # fail closed on any provider failure
            raise CapabilityUnavailableError(
                "post_deploy_unavailable",
                "the post-deployment validation replay failed: "
                + redact(str(error)),
            ) from error


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


def _decide(
    published: EvaluationResult,
    baseline: EvaluationResult,
    selected: EvaluationResult,
    policy: EvaluationPolicy,
) -> PostDeployOutcome:
    metrics = _aggregate_metrics(published)

    if not _is_evaluable(published):
        # The provider run has not completed; the lifecycle re-runs later.
        return PostDeployOutcome(
            status=PostDeployStatus.PENDING,
            reason_code="provider_run_incomplete",
            metrics=metrics,
        )

    if not _same_lineage(published, baseline) or not _same_lineage(
        published, selected
    ):
        return PostDeployOutcome(
            status=PostDeployStatus.REGRESSED,
            reason_code="lineage_mismatch",
            metrics=metrics,
        )

    eligibility = select_eligible_candidates(baseline, (published,), policy)
    if published.run.subject_id not in eligibility.eligible_ids:
        reason_code = (
            "guardrail_regression"
            if _failed_guardrail(published, policy)
            else "baseline_regression"
        )
        return PostDeployOutcome(
            status=PostDeployStatus.REGRESSED,
            reason_code=reason_code,
            metrics=metrics,
        )

    if _regresses_beyond_noise(selected, published, policy):
        return PostDeployOutcome(
            status=PostDeployStatus.REGRESSED,
            reason_code="selected_draft_regression",
            metrics=metrics,
        )

    return PostDeployOutcome(
        status=PostDeployStatus.RETAINED_IMPROVEMENT,
        reason_code=None,
        metrics=metrics,
    )


def _same_lineage(
    left: EvaluationResult,
    right: EvaluationResult,
) -> bool:
    return (
        left.run.split is right.run.split
        and left.run.dataset == right.run.dataset
        and left.run.evaluator == right.run.evaluator
        and _case_lineage(left) == _case_lineage(right)
    )


def _case_lineage(result: EvaluationResult) -> frozenset[tuple[str, str]]:
    return frozenset(
        (case.case_id, case.case_hash) for case in result.cases
    )


def _failed_guardrail(
    result: EvaluationResult,
    policy: EvaluationPolicy,
) -> bool:
    for metric in policy.metrics:
        if not metric.hard_guardrail:
            continue
        aggregate = result.metrics.get(metric.name)
        if aggregate is not None and aggregate.outcome is Outcome.FAIL:
            return True
        if metric.undefined_behavior is UndefinedBehavior.FAIL and (
            aggregate is None or aggregate.outcome is Outcome.UNDEFINED
        ):
            return True
    return False


def _regresses_beyond_noise(
    reference: EvaluationResult,
    candidate: EvaluationResult,
    policy: EvaluationPolicy,
) -> bool:
    """True when ``candidate`` is materially worse than ``reference``.

    The published version is a re-deployment of the selected candidate's exact
    changes, so its validation metrics should track the selected draft's. A
    drop within the repeat/noise band (``noisy_spread``) or below the metric's
    materiality is treated as noise; a larger drop on any metric is a material
    regression against the selection.
    """

    for metric in policy.metrics:
        reference_aggregate = reference.metrics.get(metric.name)
        candidate_aggregate = candidate.metrics.get(metric.name)
        if (
            reference_aggregate is None
            or candidate_aggregate is None
            or reference_aggregate.median is None
            or candidate_aggregate.median is None
        ):
            continue
        reference_median = reference_aggregate.median
        candidate_median = candidate_aggregate.median
        if metric.direction is MetricDirection.MAXIMIZE:
            regression = reference_median - candidate_median
        else:
            regression = candidate_median - reference_median
        tolerance = max(policy.noisy_spread, metric.materiality)
        if regression > tolerance:
            return True
    return False


def _aggregate_metrics(result: EvaluationResult) -> dict[str, float]:
    return {
        name: float(aggregate.median)
        for name, aggregate in result.metrics.items()
        if aggregate.median is not None
    }


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------


def _find_candidate(
    state: CampaignState,
    candidate_id: str,
) -> CandidateState | None:
    for candidate in state.candidates:
        if candidate.candidate_id == candidate_id:
            return candidate
    return None


def _evidence_sha256(
    root: Path,
    artifact: CandidateArtifact,
) -> str | None:
    evidence_path = (root / artifact.evidence_path).resolve()
    if not evidence_path.is_relative_to(root):
        return None
    try:
        return hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    except OSError:
        return None


def _require_evaluable(
    result: EvaluationResult | None,
    code: str,
    message: str,
) -> EvaluationResult:
    if result is None or not _is_evaluable(result):
        raise CapabilityUnavailableError(code, message)
    return result


def _is_evaluable(result: EvaluationResult) -> bool:
    return (
        result.complete
        and result.run.status is EvaluationStatus.COMPLETED
    )


def _lineage_error(subject: str) -> CapabilityUnavailableError:
    return CapabilityUnavailableError(
        "post_deploy_lineage_mismatch",
        f"{subject} does not match the finalized campaign selection",
    )


def _file_campaign_state_store() -> CampaignStateStore:
    from foundry_opt.campaign.state import FileCampaignStateStore

    return FileCampaignStateStore()


# ---------------------------------------------------------------------------
# Production factory
# ---------------------------------------------------------------------------


def build_live_post_deploy_evaluator(
    credential_provider: AzureCredentialProvider,
    *,
    state_store: CampaignStateStore | None = None,
    **binder_kwargs: object,
) -> LivePostDeployEvaluator:
    """Wire the live OIDC-backed post-deployment evaluator.

    ``binder_kwargs`` are forwarded verbatim to
    :class:`OptimizationEvaluationBinder` (e.g. ``poll_policy`` or the
    ``client_factory``/``transport_factory`` seams) so the same single shared
    Azure CLI OIDC credential provider drives every per-attempt validation run.
    """

    def binder_factory(project_endpoint: str) -> EvaluationBinder:
        return OptimizationEvaluationBinder(
            project_endpoint,
            credential_provider=credential_provider,
            **binder_kwargs,
        )

    return LivePostDeployEvaluator(
        binder_factory=binder_factory,
        state_store=state_store,
    )
