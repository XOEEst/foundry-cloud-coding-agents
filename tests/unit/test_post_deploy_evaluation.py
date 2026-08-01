"""Targeted tests for the OIDC-backed post-deployment evaluation adapter.

These drive :mod:`foundry_opt.adapters.post_deploy_evaluation`, the
``PostDeployEvaluator`` seam of the optimization RECONCILE lifecycle. The
adapter replays the pinned validation split against the *published* Foundry
version through the real ``OptimizationEvaluationBinder`` and compares it
against the persisted baseline and selected draft validation results — never
re-running either — to confirm a retained improvement, a regression, or that
the provider run is still pending.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from foundry_opt.adapters.evaluation import PollPolicy
from foundry_opt.adapters.optimization_evaluation import (
    OptimizationEvaluationError,
    build_evaluation_policy,
)
from foundry_opt.adapters.post_deploy_evaluation import (
    LivePostDeployEvaluator,
    build_live_post_deploy_evaluator,
)
from foundry_opt.campaign.models import CandidateArtifact, PatchArtifact
from foundry_opt.campaign.state import (
    CampaignState,
    CandidateState,
    FinalizedPublication,
    MemoryCampaignStateStore,
)
from foundry_opt.config.models import MetricPolicy, MutationClass
from foundry_opt.deployment import OptimizationDeploymentLineage
from foundry_opt.evaluation import (
    AgentVersionRef,
    DatasetSplit,
    DatasetVersionRef,
    EvaluationItem,
    EvaluationPolicy,
    EvaluationResult,
    EvaluationRun,
    EvaluationScore,
    EvaluationStatus,
    EvaluatorDefinitionRef,
    Usage,
    normalize_evaluation,
)
from foundry_opt.evidence import EvaluationAssetReference
from foundry_opt.optimization.lifecycle import (
    PostDeployOutcome,
    PostDeployRequest,
    PostDeployStatus,
)
from foundry_opt.optimization.models import (
    AssetKind,
    AssetProvenance,
    OptimizationSpec,
)
from foundry_opt.optimization.runner import CapabilityUnavailableError


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

REPOSITORY = "octo-org/agents"
AGENT = "support_agent"
ISSUE = 7
CAMPAIGN_ID = "issue-7"
CANDIDATE_ID = "candidate-1"
PROJECT_ENDPOINT = "https://account.services.ai.azure.com/api/projects/project"
DEPLOYMENT_VERSION = 13

BASE_COMMIT = "b" * 40
RESULT_COMMIT = "c" * 40
MERGE_COMMIT = "e" * 40
MERGE_TREE = "f" * 40

GOAL = (
    "Improve response quality for the support agent while preserving safety "
    "guardrails across every candidate."
)

VAL_DATASET = DatasetVersionRef("foundry-dataset-val", "1")
COMPOSITE_EVALUATOR = EvaluatorDefinitionRef("composite-eval", "1")
CASE_ID = "case-1"
CASE_HASH = "case-hash-1"

_EVIDENCE_PATH = Path(
    f".foundry-optimizer/campaigns/{CAMPAIGN_ID}/validation-evidence.json"
)


# ---------------------------------------------------------------------------
# Specification + asset fixtures
# ---------------------------------------------------------------------------


def _dataset(asset_id: str, role: str, remote_id: str) -> AssetProvenance:
    return AssetProvenance(
        asset_id=asset_id,
        kind=AssetKind.DATASET,
        source="foundry",
        role=role,
        name=f"support-{role}",
        version="1",
        created_by="foundry-opt",
        remote_id=remote_id,
    )


def _evaluator(
    asset_id: str,
    *,
    name: str,
    metric: str,
    remote_id: str,
) -> AssetProvenance:
    return AssetProvenance(
        asset_id=asset_id,
        kind=AssetKind.EVALUATOR,
        source="builtin",
        name=name,
        version="1",
        created_by="builtin-evaluator-provider",
        metrics=(metric,),
        remote_id=remote_id,
    )


def _metric_policy(
    *,
    threshold: float = 0.8,
    materiality: float = 0.05,
    hard_guardrail: bool = False,
) -> MetricPolicy:
    return MetricPolicy(
        direction="maximize",
        threshold=threshold,
        materiality=materiality,
        hard_guardrail=hard_guardrail,
        undefined_behavior="fail",
    )


def _spec(
    *,
    metrics: dict[str, MetricPolicy] | None = None,
    evaluators: tuple[AssetProvenance, ...] | None = None,
) -> OptimizationSpec:
    return OptimizationSpec(
        issue_number=ISSUE,
        repository=REPOSITORY,
        base_commit=BASE_COMMIT,
        target=AGENT,
        environment="acceptance",
        base_agent_version="12",
        goal=GOAL,
        datasets=(
            _dataset("dataset-dev", "development", "foundry-dataset-dev"),
            _dataset("dataset-val", "validation", "foundry-dataset-val"),
        ),
        evaluators=evaluators
        or (
            _evaluator(
                "evaluator-quality",
                name="quality",
                metric="quality",
                remote_id="builtin:quality:1",
            ),
        ),
        metrics=metrics or {"quality": _metric_policy()},
        allowed_mutations=frozenset({MutationClass.SYSTEM_INSTRUCTIONS}),
    )


def _guardrail_spec() -> OptimizationSpec:
    return _spec(
        metrics={
            "quality": _metric_policy(),
            "safety": _metric_policy(hard_guardrail=True),
        },
        evaluators=(
            _evaluator(
                "evaluator-quality",
                name="quality",
                metric="quality",
                remote_id="builtin:quality:1",
            ),
            _evaluator(
                "evaluator-safety",
                name="safety",
                metric="safety",
                remote_id="builtin:safety:1",
            ),
        ),
    )


def _asset(provenance: AssetProvenance) -> EvaluationAssetReference:
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


def _assets(spec: OptimizationSpec) -> tuple[EvaluationAssetReference, ...]:
    return tuple(
        _asset(provenance)
        for provenance in (*spec.datasets, *spec.evaluators)
    )


# ---------------------------------------------------------------------------
# Evaluation-result fixtures
# ---------------------------------------------------------------------------


def _draft_agent(draft_id: str) -> AgentVersionRef:
    return AgentVersionRef(AGENT, draft_id, draft_id)


def _published_agent(version: int) -> AgentVersionRef:
    return AgentVersionRef(AGENT, str(version), str(version))


def _result(
    subject_id: str,
    agent: AgentVersionRef,
    metric_values: Mapping[str, float],
    *,
    policy: EvaluationPolicy,
    status: EvaluationStatus = EvaluationStatus.COMPLETED,
    dataset: DatasetVersionRef = VAL_DATASET,
    evaluator: EvaluatorDefinitionRef = COMPOSITE_EVALUATOR,
    case_id: str = CASE_ID,
    case_hash: str = CASE_HASH,
    run_id: str | None = None,
) -> EvaluationResult:
    run = EvaluationRun(
        run_id=run_id or f"run-{subject_id}",
        evaluation_id="eval-validation",
        subject_id=subject_id,
        split=DatasetSplit.VALIDATION,
        agent=agent,
        dataset=dataset,
        evaluator=evaluator,
        status=status,
        portal_url=None,
        started_at=None,
        completed_at=None,
        error=None,
    )
    item = EvaluationItem(
        case_id=case_id,
        case_hash=case_hash,
        response_ids=("resp-1",),
        scores=tuple(
            EvaluationScore(name, value, value)
            for name, value in metric_values.items()
        ),
        usage=Usage(10, 4, 0),
        trajectory=None,
        error=None,
        duration_ms=5,
    )
    return normalize_evaluation(run, (item,), policy)


def _published(
    metric_values: Mapping[str, float],
    *,
    policy: EvaluationPolicy,
    version: int = DEPLOYMENT_VERSION,
    status: EvaluationStatus = EvaluationStatus.COMPLETED,
    dataset: DatasetVersionRef = VAL_DATASET,
    evaluator: EvaluatorDefinitionRef = COMPOSITE_EVALUATOR,
    case_id: str = CASE_ID,
    case_hash: str = CASE_HASH,
    needs_repeat: bool | None = None,
    run_id: str | None = None,
) -> EvaluationResult:
    result = _result(
        f"published-{CANDIDATE_ID}",
        _published_agent(version),
        metric_values,
        policy=policy,
        status=status,
        dataset=dataset,
        evaluator=evaluator,
        case_id=case_id,
        case_hash=case_hash,
        run_id=run_id,
    )
    if needs_repeat is not None:
        result = replace(result, needs_repeat=needs_repeat)
    return result


# ---------------------------------------------------------------------------
# Campaign-state fixtures
# ---------------------------------------------------------------------------


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _patch_sha(candidate_id: str) -> str:
    return _sha(f"patch::{candidate_id}")


def _artifact(candidate_id: str = CANDIDATE_ID) -> CandidateArtifact:
    return CandidateArtifact(
        candidate_id=candidate_id,
        patch=PatchArtifact(
            candidate_id=candidate_id,
            path=Path(
                f".foundry-optimizer/campaigns/{CAMPAIGN_ID}/patches/"
                f"{candidate_id}.diff"
            ),
            sha256=_patch_sha(candidate_id),
            base_commit=BASE_COMMIT,
            result_commit=RESULT_COMMIT,
        ),
        draft_id=f"draft-{candidate_id}",
        evidence_path=_EVIDENCE_PATH,
        eligible=True,
        metrics={"quality": 0.9},
    )


def _write_evidence(tmp_path: Path) -> str:
    path = tmp_path / _EVIDENCE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(
        {"campaign_id": CAMPAIGN_ID, "evidence": "redacted"},
        sort_keys=True,
    ).encode("utf-8")
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def _state(
    spec: OptimizationSpec,
    *,
    baseline_validation: EvaluationResult | None,
    candidate_validation: EvaluationResult | None,
    assets: tuple[EvaluationAssetReference, ...] = (),
    status: str = "completed",
) -> CampaignState:
    now = datetime(2026, 7, 28, tzinfo=UTC)
    candidate = CandidateState(
        candidate_id=CANDIDATE_ID,
        slot=0,
        status="evaluated",
        artifact=_artifact(),
        validation_result=candidate_validation,
    )
    return CampaignState(
        campaign_id=CAMPAIGN_ID,
        target=AGENT,
        base_commit=BASE_COMMIT,
        status=status,
        started_at=now,
        updated_at=now,
        goal_sha256=_sha(spec.goal),
        spec_sha256=spec.sha256,
        assets=assets,
        candidates=(candidate,),
        pareto_candidate_ids=(CANDIDATE_ID,),
        baseline_validation=baseline_validation,
        finalized=FinalizedPublication(
            campaign_pull_request_number=100,
            campaign_pull_request_url=(
                f"https://github.com/{REPOSITORY}/pull/100"
            ),
            candidate_issue_numbers={CANDIDATE_ID: 201},
        ),
    )


def _lineage(
    spec: OptimizationSpec,
    evidence_sha: str,
    **overrides: object,
) -> OptimizationDeploymentLineage:
    kwargs: dict[str, object] = {
        "parent_issue_number": ISSUE,
        "spec_sha256": spec.sha256,
        "campaign_id": CAMPAIGN_ID,
        "campaign_pull_request_number": 100,
        "candidate_issue_number": 201,
        "candidate_pull_request_number": 110,
        "candidate_id": CANDIDATE_ID,
        "selected_draft_id": f"draft-{CANDIDATE_ID}",
        "patch_sha256": _patch_sha(CANDIDATE_ID),
        "evidence_sha256": evidence_sha,
        "selected_tree_sha": MERGE_TREE,
        "selected_merge_commit": MERGE_COMMIT,
    }
    kwargs.update(overrides)
    return OptimizationDeploymentLineage(**kwargs)


# ---------------------------------------------------------------------------
# Fakes: evaluation binder / runner
# ---------------------------------------------------------------------------


@dataclass
class _FakeRunner:
    results: list[EvaluationResult] = field(default_factory=list)
    error: Exception | None = None
    calls: list[tuple[Any, DatasetSplit, int]] = field(default_factory=list)

    def __call__(
        self,
        subject: Any,
        split: DatasetSplit,
        attempt: int,
    ) -> EvaluationResult:
        self.calls.append((subject, split, attempt))
        if self.error is not None:
            raise self.error
        return self.results[attempt - 1]


@dataclass
class _FakeBinder:
    runner: _FakeRunner
    calls: list[tuple[OptimizationSpec, Sequence[Any]]] = field(
        default_factory=list
    )

    def __call__(
        self,
        spec: OptimizationSpec,
        assets: Sequence[Any],
    ) -> _FakeRunner:
        self.calls.append((spec, assets))
        return self.runner


@dataclass
class _FakeBinderFactory:
    runner: _FakeRunner
    endpoints: list[str] = field(default_factory=list)
    binder: _FakeBinder | None = None

    def __call__(self, project_endpoint: str) -> _FakeBinder:
        self.endpoints.append(project_endpoint)
        self.binder = _FakeBinder(self.runner)
        return self.binder


def _harness(
    tmp_path: Path,
    *,
    published_results: list[EvaluationResult] | None = None,
    runner_error: Exception | None = None,
    baseline_values: Mapping[str, float] | None = None,
    draft_values: Mapping[str, float] | None = None,
    spec: OptimizationSpec | None = None,
    deployment_version: int | None = DEPLOYMENT_VERSION,
    candidate_validation_present: bool = True,
    baseline_present: bool = True,
    assets: tuple[EvaluationAssetReference, ...] = (),
    save_state: bool = True,
    state_status: str = "completed",
    lineage_overrides: Mapping[str, object] | None = None,
) -> SimpleNamespace:
    spec = spec or _spec()
    policy = build_evaluation_policy(spec)
    evidence_sha = _write_evidence(tmp_path)
    baseline = (
        _result(
            "baseline",
            _draft_agent("draft-baseline"),
            baseline_values or {"quality": 0.85},
            policy=policy,
        )
        if baseline_present
        else None
    )
    draft = (
        _result(
            CANDIDATE_ID,
            _draft_agent(f"draft-{CANDIDATE_ID}"),
            draft_values or {"quality": 0.95},
            policy=policy,
        )
        if candidate_validation_present
        else None
    )
    state = _state(
        spec,
        baseline_validation=baseline,
        candidate_validation=draft,
        assets=assets,
        status=state_status,
    )
    store = MemoryCampaignStateStore()
    if save_state:
        store.save(tmp_path, state)
    runner = _FakeRunner(
        results=published_results or [], error=runner_error
    )
    factory = _FakeBinderFactory(runner)
    evaluator = LivePostDeployEvaluator(
        binder_factory=factory, state_store=store
    )
    request = PostDeployRequest(
        repository_root=tmp_path,
        lineage=_lineage(spec, evidence_sha, **(lineage_overrides or {})),
        selected_candidate_id=CANDIDATE_ID,
        deployment_version=deployment_version,
        project_endpoint=PROJECT_ENDPOINT,
        spec=spec,
    )
    return SimpleNamespace(
        evaluator=evaluator,
        request=request,
        factory=factory,
        runner=runner,
        policy=policy,
        state=state,
        spec=spec,
    )


# ---------------------------------------------------------------------------
# Retained improvement
# ---------------------------------------------------------------------------


def test_retained_improvement_when_published_matches_selection(
    tmp_path: Path,
) -> None:
    policy = build_evaluation_policy(_spec())
    harness = _harness(
        tmp_path,
        published_results=[_published({"quality": 0.95}, policy=policy)],
        baseline_values={"quality": 0.85},
        draft_values={"quality": 0.95},
    )

    outcome = harness.evaluator.evaluate(harness.request)

    assert outcome.status is PostDeployStatus.RETAINED_IMPROVEMENT
    assert outcome.reason_code is None
    # Aggregate metrics only — the median, never per-case rows.
    assert outcome.metrics == {"quality": 0.95}
    assert outcome.baseline_metrics == {"quality": 0.85}
    assert outcome.selected_draft_metrics == {"quality": 0.95}
    # The published subject drove exactly one validation attempt (no repeat)
    # against the per-project binder using the campaign's materialized assets.
    assert harness.runner.calls[0][1] is DatasetSplit.VALIDATION
    assert len(harness.runner.calls) == 1
    assert harness.factory.endpoints == [PROJECT_ENDPOINT]
    assert harness.factory.binder is not None
    spec_arg, assets_arg = harness.factory.binder.calls[0]
    assert spec_arg is harness.request.spec
    assert assets_arg == harness.state.assets


def test_published_subject_pins_exact_published_version_not_draft(
    tmp_path: Path,
) -> None:
    policy = build_evaluation_policy(_spec())
    harness = _harness(
        tmp_path,
        published_results=[_published({"quality": 0.95}, policy=policy)],
    )

    harness.evaluator.evaluate(harness.request)

    subject = harness.runner.calls[0][0]
    # Exact published agent name and numeric version — never a campaign draft.
    assert subject.agent.agent_id == AGENT
    assert subject.agent.version == str(DEPLOYMENT_VERSION)
    assert subject.agent.draft_id == str(DEPLOYMENT_VERSION)
    assert not subject.agent.draft_id.startswith("draft-")
    assert subject.subject_id == f"published-{CANDIDATE_ID}"


# ---------------------------------------------------------------------------
# Regressions
# ---------------------------------------------------------------------------


def test_regressed_when_published_loses_against_baseline(
    tmp_path: Path,
) -> None:
    policy = build_evaluation_policy(_spec())
    harness = _harness(
        tmp_path,
        published_results=[_published({"quality": 0.82}, policy=policy)],
        baseline_values={"quality": 0.90},
        draft_values={"quality": 0.95},
    )

    outcome = harness.evaluator.evaluate(harness.request)

    assert outcome.status is PostDeployStatus.REGRESSED
    assert outcome.reason_code == "baseline_regression"
    assert outcome.metrics == {"quality": 0.82}


def test_regressed_on_hard_guardrail_failure(tmp_path: Path) -> None:
    spec = _guardrail_spec()
    policy = build_evaluation_policy(spec)
    harness = _harness(
        tmp_path,
        spec=spec,
        published_results=[
            _published({"quality": 0.95, "safety": 0.50}, policy=policy)
        ],
        baseline_values={"quality": 0.85, "safety": 0.90},
        draft_values={"quality": 0.95, "safety": 0.90},
    )

    outcome = harness.evaluator.evaluate(harness.request)

    assert outcome.status is PostDeployStatus.REGRESSED
    assert outcome.reason_code == "guardrail_regression"


def test_regressed_when_worse_than_selected_draft_beyond_noise(
    tmp_path: Path,
) -> None:
    policy = build_evaluation_policy(_spec())
    # Materially improves the baseline, but drops 0.07 below the selected
    # draft — beyond the metric materiality (0.05) noise band.
    harness = _harness(
        tmp_path,
        published_results=[_published({"quality": 0.88}, policy=policy)],
        baseline_values={"quality": 0.82},
        draft_values={"quality": 0.95},
    )

    outcome = harness.evaluator.evaluate(harness.request)

    assert outcome.status is PostDeployStatus.REGRESSED
    assert outcome.reason_code == "selected_draft_regression"


def test_retained_within_selected_draft_noise_band(tmp_path: Path) -> None:
    policy = build_evaluation_policy(_spec())
    # Drops only 0.03 below the selected draft — within the noise band — while
    # still materially improving the baseline.
    harness = _harness(
        tmp_path,
        published_results=[_published({"quality": 0.92}, policy=policy)],
        baseline_values={"quality": 0.85},
        draft_values={"quality": 0.95},
    )

    outcome = harness.evaluator.evaluate(harness.request)

    assert outcome.status is PostDeployStatus.RETAINED_IMPROVEMENT
    assert outcome.metrics == {"quality": 0.92}


# ---------------------------------------------------------------------------
# Repeat policy
# ---------------------------------------------------------------------------


def test_bounded_repeat_policy_is_applied(tmp_path: Path) -> None:
    policy = build_evaluation_policy(_spec())
    first = _published(
        {"quality": 0.90}, policy=policy, needs_repeat=True, run_id="run-a"
    )
    second = _published({"quality": 0.94}, policy=policy, run_id="run-b")
    harness = _harness(
        tmp_path,
        published_results=[first, second],
        baseline_values={"quality": 0.85},
        draft_values={"quality": 0.92},
    )

    outcome = harness.evaluator.evaluate(harness.request)

    # Two attempts were driven and combined (median of 0.90 and 0.94 = 0.92).
    assert [attempt for _s, _split, attempt in harness.runner.calls] == [1, 2]
    assert outcome.status is PostDeployStatus.RETAINED_IMPROVEMENT
    assert outcome.metrics == {"quality": pytest.approx(0.92)}


# ---------------------------------------------------------------------------
# Pending
# ---------------------------------------------------------------------------


def test_pending_when_provider_run_incomplete(tmp_path: Path) -> None:
    policy = build_evaluation_policy(_spec())
    partial = _published(
        {"quality": 0.95},
        policy=policy,
        status=EvaluationStatus.PARTIAL,
        run_id="run-a",
    )
    partial_repeat = _published(
        {"quality": 0.95},
        policy=policy,
        status=EvaluationStatus.PARTIAL,
        run_id="run-b",
    )
    harness = _harness(
        tmp_path,
        published_results=[partial, partial_repeat],
    )

    outcome = harness.evaluator.evaluate(harness.request)

    assert outcome.status is PostDeployStatus.PENDING
    assert outcome.reason_code == "provider_run_incomplete"


# ---------------------------------------------------------------------------
# Lineage / cross-lineage
# ---------------------------------------------------------------------------


def test_regressed_on_cross_lineage_dataset_drift(tmp_path: Path) -> None:
    policy = build_evaluation_policy(_spec())
    # The published run scored a different pinned dataset than the baseline and
    # selected draft were evaluated against.
    published = _published(
        {"quality": 0.95},
        policy=policy,
        dataset=DatasetVersionRef("other-dataset", "1"),
    )
    harness = _harness(
        tmp_path,
        published_results=[published],
    )

    outcome = harness.evaluator.evaluate(harness.request)

    assert outcome.status is PostDeployStatus.REGRESSED
    assert outcome.reason_code == "lineage_mismatch"


def test_regressed_on_cross_lineage_case_drift(tmp_path: Path) -> None:
    policy = build_evaluation_policy(_spec())
    published = _published(
        {"quality": 0.95},
        policy=policy,
        case_hash="drifted-case-hash",
    )
    harness = _harness(
        tmp_path,
        published_results=[published],
    )

    outcome = harness.evaluator.evaluate(harness.request)

    assert outcome.status is PostDeployStatus.REGRESSED
    assert outcome.reason_code == "lineage_mismatch"


# ---------------------------------------------------------------------------
# Precondition / provenance failures -> CapabilityUnavailableError
# ---------------------------------------------------------------------------


def test_missing_deployment_version_blocks(tmp_path: Path) -> None:
    harness = _harness(tmp_path, deployment_version=None)

    with pytest.raises(CapabilityUnavailableError) as excinfo:
        harness.evaluator.evaluate(harness.request)

    assert excinfo.value.code == "post_deploy_deployment_version_invalid"
    # Fails closed before running any provider evaluation.
    assert harness.factory.endpoints == []


def test_non_positive_deployment_version_blocks(tmp_path: Path) -> None:
    harness = _harness(tmp_path, deployment_version=0)

    with pytest.raises(CapabilityUnavailableError) as excinfo:
        harness.evaluator.evaluate(harness.request)

    assert excinfo.value.code == "post_deploy_deployment_version_invalid"


def test_missing_finalized_state_blocks(tmp_path: Path) -> None:
    harness = _harness(tmp_path, save_state=False)

    with pytest.raises(CapabilityUnavailableError) as excinfo:
        harness.evaluator.evaluate(harness.request)

    assert excinfo.value.code == "post_deploy_state_missing"
    assert harness.factory.endpoints == []


def test_unfinalized_state_blocks(tmp_path: Path) -> None:
    harness = _harness(tmp_path, state_status="active")

    with pytest.raises(CapabilityUnavailableError) as excinfo:
        harness.evaluator.evaluate(harness.request)

    assert excinfo.value.code == "post_deploy_state_missing"


def test_patch_identity_mismatch_blocks(tmp_path: Path) -> None:
    harness = _harness(
        tmp_path,
        lineage_overrides={"patch_sha256": "9" * 64},
    )

    with pytest.raises(CapabilityUnavailableError) as excinfo:
        harness.evaluator.evaluate(harness.request)

    assert excinfo.value.code == "post_deploy_lineage_mismatch"
    assert harness.factory.endpoints == []


def test_evidence_identity_mismatch_blocks(tmp_path: Path) -> None:
    harness = _harness(
        tmp_path,
        lineage_overrides={"evidence_sha256": "a" * 64},
    )

    with pytest.raises(CapabilityUnavailableError) as excinfo:
        harness.evaluator.evaluate(harness.request)

    assert excinfo.value.code == "post_deploy_lineage_mismatch"


def test_missing_selected_validation_blocks(tmp_path: Path) -> None:
    harness = _harness(tmp_path, candidate_validation_present=False)

    with pytest.raises(CapabilityUnavailableError) as excinfo:
        harness.evaluator.evaluate(harness.request)

    assert excinfo.value.code == "post_deploy_selection_unavailable"


def test_missing_baseline_validation_blocks(tmp_path: Path) -> None:
    harness = _harness(tmp_path, baseline_present=False)

    with pytest.raises(CapabilityUnavailableError) as excinfo:
        harness.evaluator.evaluate(harness.request)

    assert excinfo.value.code == "post_deploy_baseline_unavailable"


def test_provider_failure_is_typed_capability_unavailable(
    tmp_path: Path,
) -> None:
    harness = _harness(
        tmp_path,
        runner_error=OptimizationEvaluationError(
            "the per-specification Foundry evaluation failed: boom"
        ),
    )

    with pytest.raises(CapabilityUnavailableError) as excinfo:
        harness.evaluator.evaluate(harness.request)

    assert excinfo.value.code == "post_deploy_unavailable"


# ---------------------------------------------------------------------------
# End-to-end resource close through the real binder + production factory
# ---------------------------------------------------------------------------


class _FakeCredential:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class _FakeCredentialProvider:
    def __init__(self) -> None:
        self.created: list[_FakeCredential] = []

    def create(self) -> _FakeCredential:
        credential = _FakeCredential()
        self.created.append(credential)
        return credential


class _FakeProjectClient:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


def _score_payload(metric: str, value: float) -> dict[str, object]:
    return {
        "metric": metric,
        "raw_score": value,
        "normalized_score": value,
        "reason": None,
    }


def _item_payload(
    scores: tuple[Mapping[str, object], ...],
) -> dict[str, object]:
    return {
        "kind": "batch",
        "case_id": CASE_ID,
        "case_hash": CASE_HASH,
        "response_id": "resp-1",
        "scores": [dict(score) for score in scores],
        "usage": {"input_tokens": 10, "output_tokens": 4, "cached_tokens": 0},
        "error": None,
        "duration_ms": 5,
    }


@dataclass
class _FakeTransport:
    definition_id: str = "composite-eval"
    definition_version: str = "1"
    definitions: dict[str, dict[str, object]] = field(default_factory=dict)
    _run_counter: int = 0

    def find_definition(
        self, fingerprint: str
    ) -> Mapping[str, object] | None:
        return self.definitions.get(fingerprint)

    def create_definition(
        self, payload: Mapping[str, object]
    ) -> Mapping[str, object]:
        definition = {
            "id": self.definition_id,
            "version": self.definition_version,
            "fingerprint": payload["fingerprint"],
            "portal_url": None,
        }
        self.definitions[str(payload["fingerprint"])] = definition
        return definition

    def create_run(
        self, payload: Mapping[str, object]
    ) -> Mapping[str, object]:
        self._run_counter += 1
        return {
            "run_id": f"run-{self._run_counter}",
            "evaluation_id": self.definition_id,
            "status": "queued",
        }

    def get_run(self, run_id: str) -> Mapping[str, object]:
        return {
            "run_id": run_id,
            "evaluation_id": self.definition_id,
            "status": "completed",
            "portal_url": f"https://portal.example/{run_id}",
            "started_at": None,
            "completed_at": None,
            "error": None,
        }

    def list_output_items(
        self,
        run_id: str,
        *,
        continuation_token: str | None,
        page_size: int,
    ) -> Mapping[str, object]:
        return {
            "items": [_item_payload((_score_payload("quality", 0.95),))],
            "continuation_token": None,
            "run_id": run_id,
            "evaluation_id": self.definition_id,
        }


def test_real_binder_run_closes_azure_resources(tmp_path: Path) -> None:
    spec = _spec()
    policy = build_evaluation_policy(spec)
    evidence_sha = _write_evidence(tmp_path)
    baseline = _result(
        "baseline", _draft_agent("draft-baseline"), {"quality": 0.85},
        policy=policy,
    )
    draft = _result(
        CANDIDATE_ID, _draft_agent(f"draft-{CANDIDATE_ID}"), {"quality": 0.95},
        policy=policy,
    )
    state = _state(
        spec,
        baseline_validation=baseline,
        candidate_validation=draft,
        assets=_assets(spec),
    )
    store = MemoryCampaignStateStore()
    store.save(tmp_path, state)

    provider = _FakeCredentialProvider()
    client = _FakeProjectClient()
    transport = _FakeTransport()
    evaluator = build_live_post_deploy_evaluator(
        provider,
        state_store=store,
        client_factory=lambda endpoint, credential: client,
        transport_factory=lambda project, endpoint: transport,
        poll_policy=PollPolicy(max_attempts=3, initial_delay_seconds=0.0),
        sleep=lambda _seconds: None,
    )
    request = PostDeployRequest(
        repository_root=tmp_path,
        lineage=_lineage(spec, evidence_sha),
        selected_candidate_id=CANDIDATE_ID,
        deployment_version=DEPLOYMENT_VERSION,
        project_endpoint=PROJECT_ENDPOINT,
        spec=spec,
    )

    outcome = evaluator.evaluate(request)

    assert outcome.status is PostDeployStatus.RETAINED_IMPROVEMENT
    assert outcome.metrics == {"quality": 0.95}
    # The real binder created and closed its Azure client and credential.
    assert client.closed == 1
    assert provider.created and provider.created[0].closed == 1


def test_isinstance_of_post_deploy_outcome(tmp_path: Path) -> None:
    policy = build_evaluation_policy(_spec())
    harness = _harness(
        tmp_path,
        published_results=[_published({"quality": 0.95}, policy=policy)],
    )

    outcome = harness.evaluator.evaluate(harness.request)

    assert isinstance(outcome, PostDeployOutcome)
