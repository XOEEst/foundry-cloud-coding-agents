"""Targeted tests for the per-spec Foundry evaluation binding.

These drive :mod:`foundry_opt.adapters.optimization_evaluation`, the
OIDC-backed callable that satisfies the runner ``EvaluationBinder`` contract.
The binder reuses the existing :class:`FoundryEvaluationTransport`,
:class:`EvaluationGateway`, and normalization/funnel policies to run one
composite Foundry evaluation definition per approved specification split.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

from foundry_opt.adapters.evaluation import EvaluationSchemaError, PollPolicy
from foundry_opt.adapters.foundry_evaluation import EvaluationServiceError
from foundry_opt.adapters.optimization_evaluation import (
    OptimizationEvaluationBinder,
    OptimizationEvaluationError,
)
from foundry_opt.evaluation import (
    AgentVersionRef,
    DatasetSplit,
    EvaluationResult,
    EvaluationSubject,
    Outcome,
    evaluate_with_repeat,
)
from foundry_opt.evidence import EvaluationAssetReference
from foundry_opt.optimization.models import (
    ApprovalGate,
    AssetKind,
    AssetProvenance,
    OptimizationSpec,
)

BASE_COMMIT = "b" * 40
GOAL = (
    "Improve response quality for the support agent while preserving safety "
    "guardrails across every candidate."
)
PROJECT_ENDPOINT = (
    "https://account.services.ai.azure.com/api/projects/project"
)


# ---------------------------------------------------------------------------
# Specification + asset fixtures
# ---------------------------------------------------------------------------


def _dataset_provenance(
    asset_id: str,
    role: str,
    *,
    name: str,
    remote_id: str | None,
) -> AssetProvenance:
    return AssetProvenance(
        asset_id=asset_id,
        kind=AssetKind.DATASET,
        source="foundry",
        role=role,
        name=name,
        version="1",
        created_by="foundry-existing-asset-provider",
        approval_gate=ApprovalGate.POLICY,
        remote_id=remote_id,
    )


def _evaluator_provenance(
    asset_id: str,
    *,
    name: str,
    remote_id: str | None,
    metrics: tuple[str, ...] = ("quality",),
) -> AssetProvenance:
    return AssetProvenance(
        asset_id=asset_id,
        kind=AssetKind.EVALUATOR,
        source="builtin",
        name=name,
        version="1",
        created_by="builtin-evaluator-provider",
        approval_gate=ApprovalGate.POLICY,
        remote_id=remote_id,
        metrics=metrics,
    )


def _metric(direction: str = "maximize", threshold: float = 0.8) -> dict:
    return {
        "direction": direction,
        "threshold": threshold,
        "materiality": 0.05,
        "hard_guardrail": False,
        "undefined_behavior": "fail",
    }


def _spec(
    *,
    metrics: dict[str, dict] | None = None,
    evaluators: tuple[AssetProvenance, ...] | None = None,
    datasets: tuple[AssetProvenance, ...] | None = None,
) -> OptimizationSpec:
    return OptimizationSpec(
        issue_number=7,
        repository="octo-org/optimizer",
        base_commit=BASE_COMMIT,
        target="support_agent",
        environment="acceptance",
        base_agent_version="12",
        goal=GOAL,
        datasets=datasets
        or (
            _dataset_provenance(
                "dataset-dev",
                "development",
                name="dev-dataset",
                remote_id="foundry-dataset-dev",
            ),
            _dataset_provenance(
                "dataset-val",
                "validation",
                name="val-dataset",
                remote_id="foundry-dataset-val",
            ),
        ),
        evaluators=evaluators
        or (
            _evaluator_provenance(
                "evaluator-quality",
                name="quality",
                remote_id="builtin:quality:1",
            ),
        ),
        metrics=metrics or {"quality": _metric()},
        allowed_mutations=frozenset({"system_instructions"}),
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


def _subject(subject_id: str = "candidate-1") -> EvaluationSubject:
    return EvaluationSubject(
        subject_id,
        AgentVersionRef("support_agent", f"draft-{subject_id}", "3"),
    )


# ---------------------------------------------------------------------------
# Fake credential / client (resource-close seam)
# ---------------------------------------------------------------------------


class FakeCredential:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class FakeCredentialProvider:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.created: list[FakeCredential] = []

    def create(self) -> FakeCredential:
        if self.error is not None:
            raise self.error
        credential = FakeCredential()
        self.created.append(credential)
        return credential


class FakeProjectClient:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


# ---------------------------------------------------------------------------
# Fake evaluation transport (reuses EvaluationGateway + normalization)
# ---------------------------------------------------------------------------


def _item(
    *,
    case_id: str = "case-1",
    case_hash: str = "case-hash-1",
    response_id: str = "resp-1",
    scores: tuple[Mapping[str, object], ...] = (),
    error: str | None = None,
) -> dict[str, object]:
    return {
        "kind": "batch",
        "case_id": case_id,
        "case_hash": case_hash,
        "response_id": response_id,
        "scores": [dict(score) for score in scores],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 4,
            "cached_tokens": 0,
        },
        "error": error,
        "duration_ms": 5,
    }


def _score(metric: str, value: float) -> dict[str, object]:
    return {
        "metric": metric,
        "raw_score": value,
        "normalized_score": value,
        "reason": None,
    }


@dataclass
class FakeTransport:
    definition_id: str = "composite-eval"
    definition_version: str = "1"
    definitions: dict[str, dict[str, object]] = field(default_factory=dict)
    created_definitions: list[Mapping[str, object]] = field(
        default_factory=list
    )
    created_runs: list[Mapping[str, object]] = field(default_factory=list)
    statuses: list[str] = field(default_factory=lambda: ["completed"])
    pages: list[list[dict[str, object]]] | None = None
    run_overrides: dict[str, object] = field(default_factory=dict)
    create_definition_error: Exception | None = None
    create_run_error: Exception | None = None
    get_run_error: Exception | None = None
    _run_counter: int = 0

    def default_pages(self) -> list[list[dict[str, object]]]:
        return [[_item(scores=(_score("quality", 0.9),))]]

    def find_definition(
        self, fingerprint: str
    ) -> Mapping[str, object] | None:
        return self.definitions.get(fingerprint)

    def create_definition(
        self, payload: Mapping[str, object]
    ) -> Mapping[str, object]:
        if self.create_definition_error is not None:
            raise self.create_definition_error
        self.created_definitions.append(payload)
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
        if self.create_run_error is not None:
            raise self.create_run_error
        self.created_runs.append(payload)
        self._run_counter += 1
        return {
            "run_id": f"run-{self._run_counter}",
            "evaluation_id": self.definition_id,
            "status": "queued",
            **self.run_overrides,
        }

    def get_run(self, run_id: str) -> Mapping[str, object]:
        if self.get_run_error is not None:
            raise self.get_run_error
        status = (
            self.statuses.pop(0)
            if len(self.statuses) > 1
            else self.statuses[0]
        )
        return {
            "run_id": run_id,
            "evaluation_id": self.definition_id,
            "status": status,
            "portal_url": f"https://portal.example/{run_id}",
            "started_at": None,
            "completed_at": None,
            "error": "one case failed" if status == "partial" else None,
        }

    def list_output_items(
        self,
        run_id: str,
        *,
        continuation_token: str | None,
        page_size: int,
    ) -> Mapping[str, object]:
        pages = self.pages if self.pages is not None else self.default_pages()
        items = pages.pop(0) if len(pages) > 1 else pages[0]
        return {
            "items": [dict(item) for item in items],
            "continuation_token": None,
            "run_id": run_id,
            "evaluation_id": self.definition_id,
        }


def _binder(
    transport: FakeTransport,
    *,
    credential_provider: FakeCredentialProvider | None = None,
    client: FakeProjectClient | None = None,
    client_factory: Any | None = None,
) -> tuple[OptimizationEvaluationBinder, FakeProjectClient, FakeCredentialProvider]:
    project_client = client or FakeProjectClient()
    provider = credential_provider or FakeCredentialProvider()
    factory = client_factory or (lambda endpoint, credential: project_client)
    binder = OptimizationEvaluationBinder(
        PROJECT_ENDPOINT,
        credential_provider=provider,
        client_factory=factory,
        transport_factory=lambda project, endpoint: transport,
        poll_policy=PollPolicy(max_attempts=3, initial_delay_seconds=0.0),
        sleep=lambda _seconds: None,
    )
    return binder, project_client, provider


# ---------------------------------------------------------------------------
# Development / validation split selection
# ---------------------------------------------------------------------------


def test_development_split_runs_against_dev_dataset_and_draft() -> None:
    spec = _spec()
    transport = FakeTransport()
    binder, client, provider = _binder(transport)

    evaluate = binder(spec, _assets(spec))
    result = evaluate(_subject(), DatasetSplit.DEVELOPMENT, 1)

    assert isinstance(result, EvaluationResult)
    assert result.run.split is DatasetSplit.DEVELOPMENT
    assert result.metrics["quality"].outcome is Outcome.PASS
    assert result.complete is True
    run = transport.created_runs[0]
    assert run["kind"] == "batch"
    assert run["dataset"] == {"dataset_id": "foundry-dataset-dev", "version": "1"}
    assert run["agent"]["draft_id"] == "draft-candidate-1"
    assert run["agent"]["version"] == "3"
    assert run["evaluator"] == {
        "definition_id": "composite-eval",
        "version": "1",
    }
    # Case hashes, run IDs, portal URLs, and usage are preserved verbatim.
    assert result.run.run_id == "run-1"
    assert result.run.portal_url == "https://portal.example/run-1"
    assert result.cases[0].case_hash == "case-hash-1"
    assert result.usage.input_tokens == 10
    assert client.closed == 1
    assert provider.created[0].closed == 1


def test_validation_split_selects_validation_dataset() -> None:
    spec = _spec()
    transport = FakeTransport()
    binder, _client, _provider = _binder(transport)

    evaluate = binder(spec, _assets(spec))
    result = evaluate(_subject("baseline"), DatasetSplit.VALIDATION, 1)

    assert result.run.split is DatasetSplit.VALIDATION
    run = transport.created_runs[0]
    assert run["dataset"] == {"dataset_id": "foundry-dataset-val", "version": "1"}
    assert run["split"] == "validation"


# ---------------------------------------------------------------------------
# Multiple evaluators / metrics compose one definition
# ---------------------------------------------------------------------------


def test_multiple_evaluators_compose_single_definition() -> None:
    spec = _spec(
        metrics={
            "quality": _metric(),
            "safety": _metric(threshold=0.5),
        },
        evaluators=(
            _evaluator_provenance(
                "evaluator-quality",
                name="quality",
                remote_id="builtin:quality:1",
                metrics=("quality",),
            ),
            _evaluator_provenance(
                "evaluator-safety",
                name="safety",
                remote_id="builtin:safety:2",
                metrics=("safety",),
            ),
        ),
    )
    transport = FakeTransport(
        pages=[
            [
                _item(
                    scores=(
                        _score("quality", 0.9),
                        _score("safety", 0.7),
                    )
                )
            ]
        ]
    )
    binder, _client, _provider = _binder(transport)

    evaluate = binder(spec, _assets(spec))
    result = evaluate(_subject(), DatasetSplit.DEVELOPMENT, 1)

    assert set(result.metrics) == {"quality", "safety"}
    assert result.metrics["quality"].outcome is Outcome.PASS
    assert result.metrics["safety"].outcome is Outcome.PASS

    payload = transport.created_definitions[0]
    criteria = payload["configuration"]["testing_criteria"]
    # One criterion per metric: criterion name == metric name, while
    # evaluator_name is the evaluator catalog name. Exact remote identity
    # remains in evaluator_reference for lineage.
    mapping = {
        criterion["name"]: criterion["evaluator_name"]
        for criterion in criteria
    }
    assert mapping == {
        "quality": "quality",
        "safety": "safety",
    }
    normalization = payload["configuration"]["normalization"]
    assert set(normalization) == {"quality", "safety"}


# ---------------------------------------------------------------------------
# Bounded repeat combines two attempts
# ---------------------------------------------------------------------------


def test_repeat_combines_incomplete_then_complete_attempt() -> None:
    spec = _spec()
    transport = FakeTransport(
        statuses=["partial", "completed"],
        pages=[
            [_item(scores=(_score("quality", 0.9),), error="flaky")],
            [_item(scores=(_score("quality", 0.9),))],
        ],
    )
    binder, _client, _provider = _binder(transport)
    evaluate = binder(spec, _assets(spec))

    policy = _policy_from_binder(spec)
    combined = evaluate_with_repeat(
        _subject(), DatasetSplit.DEVELOPMENT, policy, evaluate
    )

    assert combined.attempts == 2
    assert combined.needs_repeat is False
    assert combined.complete is True
    # Two distinct runs were created and one definition was reused.
    assert len(transport.created_runs) == 2
    assert len(transport.created_definitions) == 1
    # Usage is summed across both attempts.
    assert combined.usage.input_tokens == 20


def test_single_complete_attempt_does_not_repeat() -> None:
    spec = _spec()
    transport = FakeTransport()
    binder, _client, _provider = _binder(transport)
    evaluate = binder(spec, _assets(spec))

    result = evaluate(_subject(), DatasetSplit.DEVELOPMENT, 1)

    assert result.needs_repeat is False
    assert result.attempts == 1


# ---------------------------------------------------------------------------
# Definition identity / fingerprint
# ---------------------------------------------------------------------------


def test_definition_reused_within_split_and_split_specific() -> None:
    spec = _spec()
    transport = FakeTransport()
    binder, _client, _provider = _binder(transport)
    evaluate = binder(spec, _assets(spec))

    evaluate(_subject("baseline"), DatasetSplit.DEVELOPMENT, 1)
    evaluate(_subject("candidate-1"), DatasetSplit.DEVELOPMENT, 1)

    # Same split reuses exactly one composite definition.
    assert len(transport.created_definitions) == 1
    dev_fingerprint = transport.created_definitions[0]["fingerprint"]

    evaluate(_subject("baseline"), DatasetSplit.VALIDATION, 1)
    assert len(transport.created_definitions) == 2
    val_fingerprint = transport.created_definitions[1]["fingerprint"]
    assert dev_fingerprint != val_fingerprint


def test_fingerprint_binds_spec_dataset_and_evaluator_identities() -> None:
    base = _spec()
    fingerprint = _dev_fingerprint(base)

    # Changing an evaluator identity changes the fingerprint.
    other_evaluator = _spec(
        evaluators=(
            _evaluator_provenance(
                "evaluator-quality",
                name="quality",
                remote_id="builtin:quality:2",
            ),
        )
    )
    assert _dev_fingerprint(other_evaluator) != fingerprint

    # Changing the metric policy changes the fingerprint.
    other_metric = _spec(metrics={"quality": _metric(threshold=0.6)})
    assert _dev_fingerprint(other_metric) != fingerprint

    # Changing the split dataset remote id changes the fingerprint.
    other_dataset = _spec(
        datasets=(
            _dataset_provenance(
                "dataset-dev",
                "development",
                name="dev-dataset",
                remote_id="foundry-dataset-dev-2",
            ),
            _dataset_provenance(
                "dataset-val",
                "validation",
                name="val-dataset",
                remote_id="foundry-dataset-val",
            ),
        )
    )
    assert _dev_fingerprint(other_dataset) != fingerprint


# ---------------------------------------------------------------------------
# Fail-closed behaviors
# ---------------------------------------------------------------------------


def test_missing_dataset_role_fails_closed() -> None:
    spec = _spec(
        datasets=(
            _dataset_provenance(
                "dataset-dev",
                "development",
                name="dev-dataset",
                remote_id="foundry-dataset-dev",
            ),
            _dataset_provenance(
                "dataset-dev-2",
                "development",
                name="dev-dataset-2",
                remote_id="foundry-dataset-dev-2",
            ),
        )
    )
    transport = FakeTransport()
    binder, _client, _provider = _binder(transport)
    evaluate = binder(spec, _assets(spec))

    with pytest.raises(OptimizationEvaluationError):
        # Ambiguous development role.
        evaluate(_subject(), DatasetSplit.DEVELOPMENT, 1)

    with pytest.raises(OptimizationEvaluationError):
        # No validation dataset at all.
        evaluate(_subject(), DatasetSplit.VALIDATION, 1)


def test_missing_dataset_remote_id_fails_closed() -> None:
    spec = _spec(
        datasets=(
            _dataset_provenance(
                "dataset-dev",
                "development",
                name="dev-dataset",
                remote_id=None,
            ),
            _dataset_provenance(
                "dataset-val",
                "validation",
                name="val-dataset",
                remote_id="foundry-dataset-val",
            ),
        )
    )
    transport = FakeTransport()
    binder, _client, _provider = _binder(transport)
    evaluate = binder(spec, _assets(spec))

    with pytest.raises(OptimizationEvaluationError):
        evaluate(_subject(), DatasetSplit.DEVELOPMENT, 1)
    assert transport.created_runs == []


def test_missing_evaluator_remote_id_fails_closed() -> None:
    spec = _spec(
        evaluators=(
            _evaluator_provenance(
                "evaluator-quality",
                name="quality",
                remote_id=None,
            ),
        )
    )
    transport = FakeTransport()
    binder, _client, _provider = _binder(transport)

    with pytest.raises(OptimizationEvaluationError):
        binder(spec, _assets(spec))(_subject(), DatasetSplit.DEVELOPMENT, 1)


def test_unknown_metric_in_results_fails_closed() -> None:
    spec = _spec()
    transport = FakeTransport(
        pages=[[_item(scores=(_score("hallucinated", 0.9),))]]
    )
    binder, _client, _provider = _binder(transport)
    evaluate = binder(spec, _assets(spec))

    with pytest.raises(OptimizationEvaluationError):
        evaluate(_subject(), DatasetSplit.DEVELOPMENT, 1)


# ---------------------------------------------------------------------------
# Evaluator <-> metric provenance binding (fail closed)
# ---------------------------------------------------------------------------


def test_evaluator_declaring_multiple_metrics_fails_closed() -> None:
    # Foundry Evals cannot select one metric from a multi-metric evaluator.
    spec = _spec(
        metrics={"quality": _metric(), "safety": _metric(threshold=0.5)},
        evaluators=(
            _evaluator_provenance(
                "evaluator-combined",
                name="combined",
                remote_id="builtin:combined:1",
                metrics=("quality", "safety"),
            ),
        ),
    )
    transport = FakeTransport()
    binder, _client, _provider = _binder(transport)

    with pytest.raises(OptimizationEvaluationError, match="one metric"):
        binder(spec, _assets(spec))


def test_metric_without_any_evaluator_fails_closed() -> None:
    spec = _spec(
        metrics={"quality": _metric(), "safety": _metric(threshold=0.5)},
        evaluators=(
            _evaluator_provenance(
                "evaluator-quality",
                name="quality",
                remote_id="builtin:quality:1",
                metrics=("quality",),
            ),
        ),
    )
    transport = FakeTransport()
    binder, _client, _provider = _binder(transport)

    with pytest.raises(OptimizationEvaluationError, match="safety"):
        binder(spec, _assets(spec))


def test_evaluator_referencing_unknown_metric_fails_closed() -> None:
    spec = _spec(
        metrics={"quality": _metric()},
        evaluators=(
            _evaluator_provenance(
                "evaluator-quality",
                name="quality",
                remote_id="builtin:quality:1",
                metrics=("hallucinated",),
            ),
        ),
    )
    transport = FakeTransport()
    binder, _client, _provider = _binder(transport)

    with pytest.raises(OptimizationEvaluationError, match="not in the approved"):
        binder(spec, _assets(spec))


def test_ambiguous_metric_producers_fail_closed() -> None:
    spec = _spec(
        metrics={"quality": _metric()},
        evaluators=(
            _evaluator_provenance(
                "evaluator-quality-a",
                name="quality-a",
                remote_id="builtin:quality-a:1",
                metrics=("quality",),
            ),
            _evaluator_provenance(
                "evaluator-quality-b",
                name="quality-b",
                remote_id="builtin:quality-b:1",
                metrics=("quality",),
            ),
        ),
    )
    transport = FakeTransport()
    binder, _client, _provider = _binder(transport)

    with pytest.raises(
        OptimizationEvaluationError, match="more than one evaluator"
    ):
        binder(spec, _assets(spec))


def test_asset_spec_mismatch_fails_closed() -> None:
    spec = _spec()
    foreign = _spec(
        evaluators=(
            _evaluator_provenance(
                "evaluator-other",
                name="other",
                remote_id="builtin:other:1",
            ),
        )
    )
    transport = FakeTransport()
    binder, _client, _provider = _binder(transport)

    with pytest.raises(OptimizationEvaluationError):
        binder(spec, _assets(foreign))


# ---------------------------------------------------------------------------
# Cross-lineage + provider failures
# ---------------------------------------------------------------------------


def test_cross_lineage_run_rejected() -> None:
    spec = _spec()
    transport = FakeTransport(
        run_overrides={
            "agent": {
                "agent_id": "support_agent",
                "draft_id": "draft-candidate-1",
                "version": "999",
            }
        }
    )
    binder, client, provider = _binder(transport)
    evaluate = binder(spec, _assets(spec))

    with pytest.raises(OptimizationEvaluationError):
        evaluate(_subject(), DatasetSplit.DEVELOPMENT, 1)
    # Resources are still closed on failure.
    assert client.closed == 1
    assert provider.created[0].closed == 1


def test_provider_error_fails_closed_and_closes_resources() -> None:
    spec = _spec()
    transport = FakeTransport(
        create_run_error=EvaluationServiceError("Foundry unavailable")
    )
    binder, client, provider = _binder(transport)
    evaluate = binder(spec, _assets(spec))

    with pytest.raises(OptimizationEvaluationError):
        evaluate(_subject(), DatasetSplit.DEVELOPMENT, 1)
    assert client.closed == 1
    assert provider.created[0].closed == 1


def test_failed_run_status_fails_closed() -> None:
    spec = _spec()
    transport = FakeTransport(statuses=["failed"])
    binder, _client, _provider = _binder(transport)
    evaluate = binder(spec, _assets(spec))

    with pytest.raises(OptimizationEvaluationError):
        evaluate(_subject(), DatasetSplit.DEVELOPMENT, 1)


def test_unsupported_simulation_fails_closed() -> None:
    # The reused transport rejects conversation simulation the composite batch
    # binding cannot express; the binder surfaces it as a closed failure.
    spec = _spec()
    transport = FakeTransport(
        create_run_error=EvaluationSchemaError(
            "The current Foundry conversation simulation API cannot bind the "
            "requested personas into supported scenario inputs."
        )
    )
    binder, client, provider = _binder(transport)
    evaluate = binder(spec, _assets(spec))

    with pytest.raises(OptimizationEvaluationError):
        evaluate(_subject(), DatasetSplit.DEVELOPMENT, 1)
    assert client.closed == 1
    assert provider.created[0].closed == 1


def test_credential_failure_fails_closed_without_client() -> None:
    spec = _spec()
    transport = FakeTransport()
    provider = FakeCredentialProvider(error=RuntimeError("no credential"))
    binder, client, _provider = _binder(
        transport, credential_provider=provider
    )
    evaluate = binder(spec, _assets(spec))

    with pytest.raises(OptimizationEvaluationError):
        evaluate(_subject(), DatasetSplit.DEVELOPMENT, 1)
    assert client.closed == 0


# ---------------------------------------------------------------------------
# Full round trip through the real FoundryEvaluationTransport (fake client)
# ---------------------------------------------------------------------------


def _provider_run(
    run_id: str, eval_id: str, status: str, metadata: Mapping[str, str]
) -> dict[str, object]:
    return {
        "id": run_id,
        "eval_id": eval_id,
        "status": status,
        "metadata": dict(metadata),
        "report_url": f"https://ai.azure.com/evaluations/{run_id}",
        "created_at": 1785132000,
        "result_counts": {
            "total": 1,
            "passed": 1,
            "failed": 0,
            "errored": 0,
        },
        "error": None,
    }


class FakeOutputItems:
    def __init__(self) -> None:
        self.evals: FakeEvals | None = None
        self.calls: list[dict[str, object]] = []

    def list(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        # Echo one provider result per configured testing criterion, using the
        # criterion name verbatim. This proves the criterion name (derived from
        # the metric the evaluator produces) is what surfaces as the scored
        # metric — i.e. the evaluator-quality asset yields a "quality" score.
        assert self.evals is not None
        results = [
            {
                "name": name,
                "score": 0.9,
                "normalized_score": 0.9,
                "passed": True,
            }
            for name in self.evals.definition_criteria
        ]
        return {
            "data": [
                {
                    "id": "output-1",
                    "run_id": "evalrun-1",
                    "eval_id": "eval-def-1",
                    "datasource_item": {
                        "case_id": "case-1",
                        "case_hash": "case-hash-1",
                        "response_id": "resp-1",
                    },
                    "results": results,
                    "sample": {
                        "usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 4,
                            "cached_tokens": 0,
                        },
                        "error": None,
                    },
                }
            ],
            "has_more": False,
        }


class FakeRuns:
    def __init__(self) -> None:
        self.output_items = FakeOutputItems()
        self.create_calls: list[dict[str, object]] = []
        self.retrieve_calls: list[dict[str, object]] = []
        self.status = "completed"
        self._metadata: dict[str, Mapping[str, str]] = {}
        self._counter = 0

    def create(self, **kwargs: object) -> dict[str, object]:
        self.create_calls.append(kwargs)
        self._counter += 1
        run_id = f"evalrun-{self._counter}"
        metadata = kwargs["metadata"]
        assert isinstance(metadata, Mapping)
        self._metadata[run_id] = metadata
        return _provider_run(run_id, str(kwargs["eval_id"]), "queued", metadata)

    def retrieve(self, *, eval_id: str, run_id: str) -> dict[str, object]:
        self.retrieve_calls.append({"eval_id": eval_id, "run_id": run_id})
        return _provider_run(
            run_id, eval_id, self.status, self._metadata[run_id]
        )


class FakeEvals:
    def __init__(self) -> None:
        self.runs = FakeRuns()
        self.runs.output_items.evals = self
        self.list_page: dict[str, object] = {"data": [], "has_more": False}
        self.create_calls: list[dict[str, object]] = []
        self.definition_criteria: list[str] = []

    def list(self, **kwargs: object) -> dict[str, object]:
        return self.list_page

    def create(self, **kwargs: object) -> dict[str, object]:
        self.create_calls.append(kwargs)
        criteria = kwargs["testing_criteria"]
        assert isinstance(criteria, list)
        self.definition_criteria = [
            str(criterion["name"]) for criterion in criteria
        ]
        return {"id": "eval-def-1", "metadata": kwargs["metadata"]}


class FakeOpenAIClient:
    def __init__(self) -> None:
        self.evals = FakeEvals()
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class FakeProjectClientWithOpenAI:
    def __init__(self) -> None:
        self.openai = FakeOpenAIClient()
        self.closed = 0

    def get_openai_client(self) -> FakeOpenAIClient:
        return self.openai

    def close(self) -> None:
        self.closed += 1


def test_real_transport_round_trip_with_fake_openai_client() -> None:
    spec = _spec()
    project_client = FakeProjectClientWithOpenAI()
    provider = FakeCredentialProvider()
    binder = OptimizationEvaluationBinder(
        PROJECT_ENDPOINT,
        credential_provider=provider,
        client_factory=lambda endpoint, credential: project_client,
        poll_policy=PollPolicy(max_attempts=2, initial_delay_seconds=0.0),
        sleep=lambda _seconds: None,
    )

    evaluate = binder(spec, _assets(spec))
    result = evaluate(_subject(), DatasetSplit.DEVELOPMENT, 1)

    assert result.complete is True
    assert result.run.run_id == "evalrun-1"
    # The evaluator-quality asset (metrics=("quality",)) drives a criterion
    # named "quality", whose echoed provider result becomes the quality score.
    assert result.metrics["quality"].outcome is Outcome.PASS
    assert result.cases[0].case_hash == "case-hash-1"

    evals = project_client.openai.evals
    # The composite configuration is accepted by the real transport, and the
    # single criterion is named for the metric, referencing the remote
    # evaluator identity.
    assert len(evals.create_calls) == 1
    criteria = evals.create_calls[0]["testing_criteria"]
    assert [c["name"] for c in criteria] == ["quality"]
    assert criteria[0]["evaluator_name"] == "quality"
    # The batch run pins the exact split dataset remote id and draft agent.
    data_source = evals.runs.create_calls[0]["data_source"]
    assert "foundry-dataset-dev/versions/1" in data_source["source"]["id"]
    assert data_source["target"]["version"] == "3"
    assert project_client.closed == 1
    assert provider.created[0].closed == 1


# ---------------------------------------------------------------------------
# Helpers that peek at the binder-internal policy / fingerprint
# ---------------------------------------------------------------------------


def _policy_from_binder(spec: OptimizationSpec):
    from foundry_opt.adapters.optimization_evaluation import (
        build_evaluation_policy,
    )

    return build_evaluation_policy(spec)


def _dev_fingerprint(spec: OptimizationSpec) -> str:
    transport = FakeTransport()
    binder, _client, _provider = _binder(transport)
    evaluate = binder(spec, _assets(spec))
    evaluate(_subject(), DatasetSplit.DEVELOPMENT, 1)
    return str(transport.created_definitions[0]["fingerprint"])
