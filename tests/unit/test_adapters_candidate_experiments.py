from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from foundry_opt.adapters.candidate_experiments import (
    ActionsCandidateExperimentAdapter,
    DirectCandidateExperimentAdapter,
    FoundryCandidateExperimentOperation,
)
from foundry_opt.auth import AUTH_PROBE_SCOPE, AuthProbeRequest
from foundry_opt.drafts import DraftRecord, DraftRequest
from foundry_opt.evaluation import (
    AgentVersionRef,
    DatasetSplit,
    DatasetVersionRef,
    EvaluationPolicy,
    EvaluationResult,
    EvaluationRun,
    EvaluationStatus,
    EvaluatorDefinitionRef,
    MetricAggregate,
    MetricDirection,
    MetricPolicy,
    NormalizedCase,
    NormalizedCaseMetric,
    Outcome,
    Usage,
)
from foundry_opt.orchestration.candidate_experiments import (
    CandidateExperimentOperation,
    CandidateExperimentPending,
    CandidateExperimentPlan,
    CandidateExperimentRequest,
    CandidateExperimentResult,
    DirectExperimentUnavailable,
    PersistedCandidateExperimentOperation,
)
from foundry_opt.packaging import BundleArtifact


def _request(candidate_id: str = "candidate-1") -> CandidateExperimentRequest:
    return CandidateExperimentRequest(
        issue_number=31,
        candidate_id=candidate_id,
        patch_sha256="1" * 64,
        idempotency_key="2" * 64,
    )


def _result(
    executor: str = "direct_oidc",
) -> CandidateExperimentResult:
    return CandidateExperimentResult(
        candidate_id="candidate-1",
        executor=executor,
        metrics={"quality": 0.75},
        guardrails={"safety": "pass"},
        draft_id="draft-123",
        evaluation_id="eval-123",
        run_id="run-123",
    )


@dataclass(frozen=True)
class ProbeResult:
    direct_operations_eligible: bool


class RecordingProbe:
    def __init__(self, eligible: bool, events: list[str]) -> None:
        self._eligible = eligible
        self._events = events
        self.requests: list[AuthProbeRequest] = []

    def run(self, request: AuthProbeRequest) -> ProbeResult:
        self._events.append("probe")
        self.requests.append(request)
        return ProbeResult(self._eligible)


class RecordingOperation:
    def __init__(
        self,
        events: list[str],
        result: CandidateExperimentResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self._events = events
        self._result = result or _result()
        self._error = error

    def evaluate(
        self,
        request: CandidateExperimentRequest,
    ) -> CandidateExperimentResult:
        self._events.append("operation")
        if self._error is not None:
            raise self._error
        return self._result


def test_direct_adapter_probes_immediately_before_foundry_operation(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    probe = RecordingProbe(True, events)
    adapter = DirectCandidateExperimentAdapter(
        repository_root=tmp_path,
        auth_probe=probe,
        operation=RecordingOperation(events),
    )

    result = adapter.evaluate(_request())

    assert result.executor == "direct_oidc"
    assert events == ["probe", "operation"]
    assert probe.requests == [
        AuthProbeRequest(
            repository_root=tmp_path,
            scope=AUTH_PROBE_SCOPE,
        )
    ]


def test_direct_adapter_is_unavailable_before_any_foundry_side_effect(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    adapter = DirectCandidateExperimentAdapter(
        repository_root=tmp_path,
        auth_probe=RecordingProbe(False, events),
        operation=RecordingOperation(events),
    )

    with pytest.raises(DirectExperimentUnavailable):
        adapter.evaluate(_request())

    assert events == ["probe"]


def test_direct_adapter_propagates_real_foundry_failure(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    adapter = DirectCandidateExperimentAdapter(
        repository_root=tmp_path,
        auth_probe=RecordingProbe(True, events),
        operation=RecordingOperation(
            events,
            error=RuntimeError("Foundry failed after draft creation"),
        ),
    )

    with pytest.raises(RuntimeError, match="after draft creation"):
        adapter.evaluate(_request())

    assert events == ["probe", "operation"]


def test_direct_operation_cannot_report_unavailable_after_execution_begins(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    adapter = DirectCandidateExperimentAdapter(
        repository_root=tmp_path,
        auth_probe=RecordingProbe(True, events),
        operation=RecordingOperation(
            events,
            error=DirectExperimentUnavailable("too late"),
        ),
    )

    with pytest.raises(RuntimeError, match="after execution began"):
        adapter.evaluate(_request())

    assert events == ["probe", "operation"]


class RecordingActionsGateway:
    def __init__(
        self,
        results: list[CandidateExperimentResult | None],
    ) -> None:
        self._results = results
        self.persisted: list[CandidateExperimentOperation] = []
        self.dispatched: list[PersistedCandidateExperimentOperation] = []
        self.reconciled: list[PersistedCandidateExperimentOperation] = []

    def persist(
        self,
        operation: CandidateExperimentOperation,
    ) -> PersistedCandidateExperimentOperation:
        self.persisted.append(operation)
        return PersistedCandidateExperimentOperation(
            operation=operation,
            reference=f"candidate-experiments/{operation.idempotency_key}.json",
            sha256=operation.sha256,
        )

    def dispatch(
        self,
        operation: PersistedCandidateExperimentOperation,
    ) -> None:
        self.dispatched.append(operation)

    def reconcile(
        self,
        operation: PersistedCandidateExperimentOperation,
    ) -> CandidateExperimentResult | None:
        self.reconciled.append(operation)
        return self._results.pop(0)


def test_actions_adapter_dispatches_one_consolidated_idempotent_operation() -> None:
    gateway = RecordingActionsGateway(
        [
            None,
            CandidateExperimentResult(
                candidate_id="candidate-1",
                executor="untrusted",
                metrics={"quality": 0.75},
                guardrails={
                    "safety": "pass Authorization: Bearer secret-token-value"
                },
                draft_id="draft-123",
                evaluation_id="eval-123",
                run_id="run-123",
            ),
        ]
    )
    adapter = ActionsCandidateExperimentAdapter(gateway)

    result = adapter.evaluate(_request())

    operation = gateway.persisted[0]
    assert operation.idempotency_key == _request().idempotency_key
    assert operation.to_dict() == {
        "candidate_id": "candidate-1",
        "idempotency_key": "2" * 64,
        "issue_number": 31,
        "kind": "candidate_experiment",
        "patch_sha256": "1" * 64,
        "schema_version": 1,
    }
    assert gateway.dispatched == [gateway.reconciled[0]]
    assert gateway.reconciled[0] == gateway.reconciled[1]
    assert result.executor == "actions_oidc"
    assert result.guardrails == {
        "safety": "pass Authorization: Bearer [REDACTED]"
    }


def test_actions_adapter_reconciles_existing_result_without_redispatch() -> None:
    gateway = RecordingActionsGateway([_result("actions_oidc")])

    result = ActionsCandidateExperimentAdapter(gateway).evaluate(_request())

    assert result.executor == "actions_oidc"
    assert gateway.dispatched == []


def test_actions_adapter_reports_pending_without_fabricating_result() -> None:
    gateway = RecordingActionsGateway([None, None])

    with pytest.raises(CandidateExperimentPending) as raised:
        ActionsCandidateExperimentAdapter(gateway).evaluate(_request())

    assert raised.value.idempotency_key == _request().idempotency_key
    assert len(gateway.dispatched) == 1


class RecordingDraftGateway:
    def __init__(self, record: DraftRecord) -> None:
        self.record = record
        self.requests: list[DraftRequest] = []

    def create_draft(self, request: DraftRequest) -> DraftRecord:
        self.requests.append(request)
        return self.record


def _evaluation_result(
    subject: AgentVersionRef,
) -> EvaluationResult:
    run = EvaluationRun(
        run_id="run-123",
        evaluation_id="eval-123",
        subject_id="candidate-1",
        split=DatasetSplit.DEVELOPMENT,
        agent=subject,
        dataset=DatasetVersionRef("dataset-1", "1"),
        evaluator=EvaluatorDefinitionRef("evaluator-1", "1"),
        status=EvaluationStatus.COMPLETED,
        portal_url=None,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        error=None,
    )
    case = NormalizedCase(
        case_id="private-case-id",
        case_hash="3" * 64,
        response_ids=("private-response-id",),
        scores=(
            NormalizedCaseMetric(
                metric="quality",
                raw_score=0.75,
                normalized_score=0.75,
                reason="private evaluator reason",
                outcome=Outcome.PASS,
            ),
        ),
        usage=Usage(input_tokens=999, output_tokens=111),
        trajectory=None,
        error=None,
        duration_ms=10,
    )
    return EvaluationResult(
        run=run,
        cases=(case,),
        metrics={
            "quality": MetricAggregate(
                metric="quality",
                median=0.75,
                minimum=0.75,
                maximum=0.75,
                spread=0.0,
                outcome=Outcome.PASS,
                sample_count=1,
            ),
            "safety": MetricAggregate(
                metric="safety",
                median=1.0,
                minimum=1.0,
                maximum=1.0,
                spread=0.0,
                outcome=Outcome.PASS,
                sample_count=1,
            ),
        },
        usage=Usage(input_tokens=999, output_tokens=111),
        duration_ms=10,
        errors=(),
        complete=True,
        needs_repeat=False,
        attempts=1,
        attempt_runs=(run,),
    )


def test_foundry_operation_reuses_draft_and_evaluation_adapters_without_raw_rows(
    tmp_path: Path,
) -> None:
    bundle_path = tmp_path / "candidate.zip"
    bundle_path.write_bytes(b"bundle")
    bundle = BundleArtifact(
        path=bundle_path,
        sha256="4" * 64,
        included_files=("main.py",),
        excluded_files=(),
        byte_size=6,
        manifest_path=tmp_path / "manifest.json",
    )
    draft_request = DraftRequest(
        project_endpoint=(
            "https://resource.services.ai.azure.com/api/projects/project"
        ),
        agent_name="agent",
        base_version=1,
        bundle=bundle,
        entry_point=("python", "main.py"),
        idempotency_key="2" * 64,
        subject="candidate-1",
    )
    draft_gateway = RecordingDraftGateway(
        DraftRecord(
            agent_name="agent",
            version_id="draft-123",
            base_version=1,
            sha256=bundle.sha256,
            status="draft",
        )
    )
    observed: list[tuple[object, DatasetSplit, int]] = []

    def evaluate(subject: object, split: DatasetSplit, attempt: int):
        observed.append((subject, split, attempt))
        return _evaluation_result(subject.agent)

    policy = EvaluationPolicy(
        (
            MetricPolicy(
                "quality",
                MetricDirection.MAXIMIZE,
                threshold=0.5,
                materiality=0.01,
            ),
            MetricPolicy(
                "safety",
                MetricDirection.MAXIMIZE,
                threshold=1.0,
                materiality=0.0,
                hard_guardrail=True,
            ),
        )
    )
    operation = FoundryCandidateExperimentOperation(
        draft_gateway=draft_gateway,
        resolve_plan=lambda request: CandidateExperimentPlan(
            patch_sha256=request.patch_sha256,
            draft_request=draft_request,
            split=DatasetSplit.DEVELOPMENT,
            policy=policy,
            evaluate=evaluate,
        ),
        executor="direct_oidc",
    )

    result = operation.evaluate(_request())

    assert draft_gateway.requests == [draft_request]
    subject, split, attempt = observed[0]
    assert subject.subject_id == "candidate-1"
    assert subject.idempotency_key == "2" * 64
    assert split is DatasetSplit.DEVELOPMENT
    assert attempt == 1
    assert result.metrics == {"quality": 0.75, "safety": 1.0}
    assert result.guardrails == {"safety": "pass"}
    assert not hasattr(result, "cases")
    assert not hasattr(result, "usage")
