from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from pydantic import ValidationError
import pytest

from foundry_opt.config.models import (
    AutomationPolicy,
    MetricPolicy,
    MutationClass,
    RestrictedOptIns,
)
from foundry_opt.optimization import (
    ApprovalGate,
    AssetKind,
    AssetProvenance,
    DecisionMode,
    EvaluationAssetContext,
    EvaluationAssetProvider,
    EvaluationAssetRequest,
    OptimizationIssueRequest,
    OptimizationSpec,
    OptimizationSpecApproval,
    PreparedEvaluationAsset,
    approve_optimization_spec,
    spec_is_autopilot_eligible,
)


def _metric() -> MetricPolicy:
    return MetricPolicy(
        direction="maximize",
        threshold=0.8,
        materiality=0.05,
        hard_guardrail=False,
        undefined_behavior="fail",
    )


def _dataset(
    asset_id: str = "development",
    *,
    source: str = "foundry",
    approval_gate: ApprovalGate = ApprovalGate.POLICY,
) -> EvaluationAssetRequest:
    return EvaluationAssetRequest(
        asset_id=asset_id,
        kind=AssetKind.DATASET,
        source=source,
        role="development",
        name="support-development",
        version="v1",
        approval_gate=approval_gate,
    )


def _evaluator() -> EvaluationAssetRequest:
    return EvaluationAssetRequest(
        asset_id="quality",
        kind=AssetKind.EVALUATOR,
        source="foundry",
        name="quality-evaluator",
        version="v2",
        metrics=("quality",),
    )


def _issue() -> OptimizationIssueRequest:
    return OptimizationIssueRequest(
        issue_number=42,
        repository="octo-org/agents",
        target="support-agent",
        goal=(
            "Improve complete policy coverage while preserving the advisory "
            "safety boundary."
        ),
        datasets=(
            _dataset(),
            EvaluationAssetRequest(
                asset_id="validation",
                kind=AssetKind.DATASET,
                source="synthetic",
                role="validation",
                name="support-validation",
                version="issue-42-v1",
                parameters={
                    "row_count": 20,
                    "categories": ["happy_path", "safety"],
                },
            ),
        ),
        evaluators=(_evaluator(),),
        metrics={"quality": _metric()},
        allowed_mutations=frozenset(
            {
                MutationClass.SYSTEM_INSTRUCTIONS,
                MutationClass.PYTHON_LOGIC,
            }
        ),
        decision_mode=DecisionMode.HUMAN,
    )


def _provenance(
    request: EvaluationAssetRequest,
    *,
    content_sha256: str | None = None,
) -> AssetProvenance:
    return AssetProvenance(
        asset_id=request.asset_id,
        kind=request.kind,
        source=request.source,
        role=request.role,
        name=request.name,
        version=request.version,
        content_sha256=content_sha256,
        created_by="foundry-opt",
        approval_gate=request.approval_gate,
        metrics=request.metrics,
    )


def _spec() -> OptimizationSpec:
    issue = _issue()
    return OptimizationSpec(
        issue_number=issue.issue_number,
        repository=issue.repository,
        base_commit="a" * 40,
        target=issue.target,
        environment="acceptance",
        base_agent_version="2",
        goal=issue.goal,
        datasets=(
            _provenance(issue.datasets[0]),
            _provenance(issue.datasets[1], content_sha256="b" * 64),
        ),
        evaluators=(_provenance(issue.evaluators[0]),),
        metrics=issue.metrics,
        allowed_mutations=issue.allowed_mutations,
        restricted_opt_ins=RestrictedOptIns(),
        decision_mode=issue.decision_mode,
    )


def test_issue_request_accepts_job_specific_assets_and_goal() -> None:
    issue = _issue()

    assert issue.goal.startswith("Improve complete policy coverage")
    assert issue.datasets[1].source == "synthetic"
    assert issue.datasets[1].parameters["row_count"] == 20
    assert issue.evaluators[0].metrics == ("quality",)


def test_issue_request_rejects_duplicate_asset_ids() -> None:
    issue = _issue()

    with pytest.raises(ValidationError, match="asset IDs must be unique"):
        OptimizationIssueRequest(
            **{
                **issue.model_dump(),
                "datasets": (issue.datasets[0], issue.datasets[0]),
            }
        )


@pytest.mark.parametrize(
    ("repository", "goal"),
    [
        ("not-a-repository", "A sufficiently descriptive optimization goal."),
        ("octo/repo", "short"),
        (
            "octo/repo",
            "Improve quality without regressions. ghp_exampleCredentialValue",
        ),
    ],
)
def test_issue_request_rejects_untrusted_identity_or_secret_content(
    repository: str,
    goal: str,
) -> None:
    values = _issue().model_dump()
    values.update(repository=repository, goal=goal)

    with pytest.raises(ValidationError):
        OptimizationIssueRequest(**values)


def test_trace_assets_always_require_human_review() -> None:
    with pytest.raises(
        ValidationError,
        match="trace-derived assets require human approval",
    ):
        EvaluationAssetRequest(
            asset_id="production-failures",
            kind=AssetKind.DATASET,
            source="trace",
            role="validation",
            name="support-traces",
            version="v1",
            approval_gate=ApprovalGate.POLICY,
            parameters={"lookback_hours": 24},
        )


def test_spec_hash_is_deterministic_and_goal_sensitive() -> None:
    first = _spec()
    same = OptimizationSpec.model_validate(first.model_dump())
    changed = OptimizationSpec.model_validate(
        {
            **first.model_dump(),
            "goal": "Improve a different measurable behavior without regressions.",
        }
    )

    assert first.sha256 == same.sha256
    assert first.canonical_json == same.canonical_json
    assert first.sha256 != changed.sha256


def test_spec_hash_sorts_unordered_contract_fields() -> None:
    values = _spec().model_dump()
    values["metrics"]["quality"]["repeat"] = {
        "max_repeats": 1,
        "conditions": {"noisy", "borderline", "partial"},
    }
    spec = OptimizationSpec.model_validate(values)

    document = json.loads(spec.canonical_json)

    assert document["allowed_mutations"] == [
        "python_logic",
        "system_instructions",
    ]
    assert document["metrics"]["quality"]["repeat"]["conditions"] == [
        "borderline",
        "noisy",
        "partial",
    ]


def test_spec_hash_is_stable_across_python_hash_seeds() -> None:
    values = _spec().model_dump(mode="json")
    values["metrics"]["quality"]["repeat"] = {
        "max_repeats": 1,
        "conditions": ["noisy", "borderline", "partial"],
    }
    script = (
        "import json, os;"
        "from foundry_opt.optimization import OptimizationSpec;"
        "print(OptimizationSpec.model_validate("
        "json.loads(os.environ['SPEC_DOCUMENT'])).sha256)"
    )
    hashes = set()
    for seed in ("1", "2", "3", "4"):
        environment = {
            **os.environ,
            "PYTHONHASHSEED": seed,
            "SPEC_DOCUMENT": json.dumps(values),
        }
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        hashes.add(completed.stdout.strip())

    assert len(hashes) == 1


def test_spec_approval_binds_hash_and_merge_commit() -> None:
    spec = _spec()

    approval = approve_optimization_spec(spec, approval_commit="c" * 40)

    assert approval.spec_sha256 == spec.sha256
    assert approval.approval_commit == "c" * 40
    assert approval.approval_gate is ApprovalGate.HUMAN
    assert approval.spec == spec

    with pytest.raises(ValidationError, match="spec hash does not match"):
        OptimizationSpecApproval(
            spec=spec,
            spec_sha256="d" * 64,
            approval_commit="c" * 40,
        )


def test_autopilot_eligibility_fails_closed_for_mixed_asset_gates() -> None:
    spec = _spec()
    policy = AutomationPolicy(
        allowed_dataset_sources={"foundry", "synthetic", "trace"},
        allowed_evaluator_sources={"foundry"},
        allow_spec_auto_approval=True,
    )
    assert spec_is_autopilot_eligible(spec, policy) is True

    trace = AssetProvenance(
        asset_id="trace-validation",
        kind=AssetKind.DATASET,
        source="trace",
        role="validation",
        name="support-traces",
        version="v1",
        created_by="foundry-opt",
        approval_gate=ApprovalGate.HUMAN,
    )
    mixed = OptimizationSpec.model_validate(
        {
            **spec.model_dump(),
            "datasets": (*spec.datasets, trace),
        }
    )

    assert spec_is_autopilot_eligible(mixed, policy) is False


def test_automation_policy_requires_explicit_merge_actor_and_ordering() -> None:
    with pytest.raises(ValidationError, match="merge_actor"):
        AutomationPolicy(
            allow_candidate_auto_selection=True,
            allow_merge=True,
        )

    with pytest.raises(ValidationError, match="required_checks"):
        AutomationPolicy(
            allow_candidate_auto_selection=True,
            allow_merge=True,
            merge_actor="foundry-opt-merge-app",
        )

    with pytest.raises(ValidationError, match="deployment requires merge"):
        AutomationPolicy(allow_deployment=True)

    policy = AutomationPolicy(
        allow_candidate_auto_selection=True,
        allow_merge=True,
        allow_deployment=True,
        merge_actor="foundry-opt-merge-app",
        required_checks=("foundry-opt/exact-patch",),
    )

    assert policy.merge_actor == "foundry-opt-merge-app"
    assert policy.trace_requires_human_review is True


def test_asset_provider_contract_supports_future_source_types() -> None:
    class FutureProvider:
        source_type = "future-generator"

        def prepare(
            self,
            request: EvaluationAssetRequest,
            context: EvaluationAssetContext,
        ) -> PreparedEvaluationAsset:
            return PreparedEvaluationAsset(
                provenance=AssetProvenance(
                    asset_id=request.asset_id,
                    kind=request.kind,
                    source=request.source,
                    role=request.role,
                    name=request.name,
                    version=request.version,
                    created_by="future-provider",
                    approval_gate=request.approval_gate,
                ),
                files={Path("generated.jsonl"): b'{"query":"hello"}\n'},
            )

    provider: EvaluationAssetProvider = FutureProvider()
    request = EvaluationAssetRequest(
        asset_id="future-data",
        kind=AssetKind.DATASET,
        source="future-generator",
        role="development",
        name="future-data",
        version="v1",
    )

    prepared = provider.prepare(
        request,
        EvaluationAssetContext(
            repository_root=Path("."),
            project_endpoint=(
                "https://example.services.ai.azure.com/api/projects/demo"
            ),
            target="support-agent",
            issue_number=42,
        ),
    )

    assert prepared.provenance.source == "future-generator"
    assert tuple(prepared.files) == (Path("generated.jsonl"),)
