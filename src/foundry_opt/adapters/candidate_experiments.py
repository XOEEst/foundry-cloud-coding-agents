from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

from foundry_opt.auth import (
    AUTH_PROBE_SCOPE,
    AuthProbeRequest,
    AuthProbeResult,
)
from foundry_opt.drafts import DraftRecord, DraftRequest
from foundry_opt.evaluation import (
    AgentVersionRef,
    EvaluationResult,
    EvaluationSubject,
    evaluate_with_repeat,
)
from foundry_opt.orchestration.candidate_experiments import (
    CandidateExperimentActionsGateway,
    CandidateExperimentAdapter,
    CandidateExperimentOperation,
    CandidateExperimentPending,
    CandidateExperimentPlan,
    CandidateExperimentRequest,
    CandidateExperimentResult,
    CandidateExperimentRunner,
    DirectExperimentUnavailable,
    PersistedCandidateExperimentOperation,
)
from foundry_opt.preflight.redaction import redact


class AuthProbe(Protocol):
    def run(self, request: AuthProbeRequest) -> AuthProbeResult: ...


class DraftCreator(Protocol):
    def create_draft(self, request: DraftRequest) -> DraftRecord: ...


PlanResolver = Callable[
    [CandidateExperimentRequest],
    CandidateExperimentPlan,
]


class DirectCandidateExperimentAdapter:
    """Run the safe eligibility probe immediately before direct execution."""

    def __init__(
        self,
        *,
        repository_root: Path,
        auth_probe: AuthProbe,
        operation: CandidateExperimentAdapter,
    ) -> None:
        self._repository_root = repository_root
        self._auth_probe = auth_probe
        self._operation = operation

    def evaluate(
        self,
        request: CandidateExperimentRequest,
    ) -> CandidateExperimentResult:
        probe = self._auth_probe.run(
            AuthProbeRequest(
                repository_root=self._repository_root,
                scope=AUTH_PROBE_SCOPE,
            )
        )
        if not probe.direct_operations_eligible:
            raise DirectExperimentUnavailable(
                "direct candidate experiment is not OIDC eligible"
            )
        try:
            result = self._operation.evaluate(request)
        except DirectExperimentUnavailable as error:
            raise RuntimeError(
                "direct candidate experiment reported unavailable after "
                "execution began"
            ) from error
        return _bound_result(request, result, executor="direct_oidc")


class ActionsCandidateExperimentAdapter:
    """Dispatch one persisted operation and reconcile its redacted summary.

    The gateway's dispatch is idempotent for the persisted operation SHA and
    idempotency key. This adapter therefore safely retries dispatch only after
    reconciliation confirms that the durable result is still absent.
    """

    def __init__(self, gateway: CandidateExperimentActionsGateway) -> None:
        self._gateway = gateway

    def evaluate(
        self,
        request: CandidateExperimentRequest,
    ) -> CandidateExperimentResult:
        operation = CandidateExperimentOperation.from_request(request)
        persisted = self._gateway.persist(operation)
        _validate_persisted(operation, persisted.operation)
        result = self._gateway.reconcile(persisted)
        if result is None:
            self._gateway.dispatch(persisted)
            result = self._gateway.reconcile(persisted)
        if result is None:
            raise CandidateExperimentPending(request.idempotency_key)
        _validate_result_lineage(persisted, result)
        return _bound_result(request, result, executor="actions_oidc")


class FoundryCandidateExperimentOperation:
    """Create one idempotent draft and evaluate only aggregate development data."""

    def __init__(
        self,
        *,
        draft_gateway: DraftCreator,
        resolve_plan: PlanResolver,
        executor: str,
    ) -> None:
        self._draft_gateway = draft_gateway
        self._resolve_plan = resolve_plan
        self._executor = executor

    def evaluate(
        self,
        request: CandidateExperimentRequest,
    ) -> CandidateExperimentResult:
        plan = self._resolve_plan(request)
        _validate_plan(request, plan)
        draft = self._draft_gateway.create_draft(plan.draft_request)
        _validate_draft(plan, draft)
        agent = AgentVersionRef(
            plan.draft_request.agent_name,
            draft.version_id,
            draft.version_id,
        )
        subject = EvaluationSubject(
            request.candidate_id,
            agent,
            request.idempotency_key,
        )
        result = evaluate_with_repeat(
            subject,
            plan.split,
            plan.policy,
            plan.evaluate,
        )
        _validate_evaluation(plan, subject, result)
        metrics = {
            name: aggregate.median
            for name, aggregate in result.metrics.items()
            if aggregate.median is not None
        }
        guardrails = {
            metric.name: (
                result.metrics[metric.name].outcome.value
                if metric.name in result.metrics
                else "undefined"
            )
            for metric in plan.policy.metrics
            if metric.hard_guardrail
        }
        return CandidateExperimentResult(
            candidate_id=request.candidate_id,
            executor=self._executor,
            metrics=metrics,
            guardrails=guardrails,
            draft_id=draft.version_id,
            evaluation_id=result.run.evaluation_id,
            run_id=result.run.run_id,
            bundle_sha256=request.bundle_sha256,
            evidence_sha256=request.evidence_sha256,
            operation_sha256=(
                CandidateExperimentOperation.from_request(request).sha256
            ),
            idempotency_key=request.idempotency_key,
        )


def build_production_direct_candidate_experiment_adapter(
    *,
    repository_root: Path,
    operation: CandidateExperimentAdapter,
    auth_probe: AuthProbe | None = None,
) -> DirectCandidateExperimentAdapter:
    if auth_probe is None:
        from foundry_opt.auth import build_production_auth_probe

        auth_probe = build_production_auth_probe()
    return DirectCandidateExperimentAdapter(
        repository_root=repository_root,
        auth_probe=auth_probe,
        operation=operation,
    )


def build_production_candidate_experiment_runner(
    *,
    repository_root: Path,
    direct_operation: CandidateExperimentAdapter,
    actions_gateway: CandidateExperimentActionsGateway,
    auth_probe: AuthProbe | None = None,
) -> CandidateExperimentRunner:
    return CandidateExperimentRunner(
        direct=build_production_direct_candidate_experiment_adapter(
            repository_root=repository_root,
            operation=direct_operation,
            auth_probe=auth_probe,
        ),
        fallback=ActionsCandidateExperimentAdapter(actions_gateway),
    )


def _validate_persisted(
    expected: CandidateExperimentOperation,
    actual: CandidateExperimentOperation,
) -> None:
    if actual != expected:
        raise ValueError("persisted candidate experiment operation changed")
    if actual.idempotency_key != expected.idempotency_key:
        raise ValueError("persisted candidate experiment idempotency changed")


def _validate_result_lineage(
    persisted: PersistedCandidateExperimentOperation,
    result: CandidateExperimentResult,
) -> None:
    if (
        result.operation_sha256 != persisted.sha256
        or result.idempotency_key
        != persisted.operation.idempotency_key
        or result.bundle_sha256
        != persisted.operation.bundle_sha256
        or result.evidence_sha256
        != persisted.operation.evidence_sha256
    ):
        raise ValueError(
            "candidate experiment result lineage does not match the "
            "persisted operation"
        )


def _validate_plan(
    request: CandidateExperimentRequest,
    plan: CandidateExperimentPlan,
) -> None:
    draft = plan.draft_request
    if plan.patch_sha256 != request.patch_sha256:
        raise ValueError("candidate experiment patch binding changed")
    if plan.draft_request.bundle.sha256 != request.bundle_sha256:
        raise ValueError("candidate experiment bundle binding changed")
    if plan.evidence_sha256 != request.evidence_sha256:
        raise ValueError("candidate experiment evidence binding changed")
    if draft.idempotency_key != request.idempotency_key:
        raise ValueError("candidate experiment draft idempotency changed")
    if draft.subject != request.candidate_id:
        raise ValueError("candidate experiment draft subject changed")
    if draft.probe:
        raise ValueError("candidate experiment cannot use a probe draft")


def _validate_draft(
    plan: CandidateExperimentPlan,
    draft: DraftRecord,
) -> None:
    request = plan.draft_request
    if (
        draft.agent_name != request.agent_name
        or draft.base_version != request.base_version
        or draft.sha256 != request.bundle.sha256
        or not draft.version_id
    ):
        raise ValueError("candidate experiment draft binding changed")


def _validate_evaluation(
    plan: CandidateExperimentPlan,
    subject: EvaluationSubject,
    result: EvaluationResult,
) -> None:
    if not result.complete:
        raise ValueError("candidate experiment evaluation is incomplete")
    expected_metrics = {metric.name for metric in plan.policy.metrics}
    if not set(result.metrics).issubset(expected_metrics):
        raise ValueError("candidate experiment returned an unknown metric")
    for run in result.all_runs:
        if (
            run.subject_id != subject.subject_id
            or run.split is not plan.split
            or run.agent != subject.agent
            or not run.evaluation_id
            or not run.run_id
        ):
            raise ValueError("candidate experiment evaluation binding changed")


def _bound_result(
    request: CandidateExperimentRequest,
    result: CandidateExperimentResult,
    *,
    executor: str,
) -> CandidateExperimentResult:
    if result.candidate_id != request.candidate_id:
        raise ValueError("candidate experiment result changed candidate")
    return CandidateExperimentResult(
        candidate_id=request.candidate_id,
        executor=executor,
        metrics=result.metrics,
        guardrails=_redacted_guardrails(result.guardrails),
        draft_id=_redacted(result.draft_id),
        evaluation_id=_redacted(result.evaluation_id),
        run_id=_redacted(result.run_id),
        bundle_sha256=result.bundle_sha256,
        evidence_sha256=result.evidence_sha256,
        operation_sha256=result.operation_sha256,
        idempotency_key=result.idempotency_key,
    )


def _redacted_guardrails(
    guardrails: Mapping[str, str],
) -> dict[str, str]:
    redacted_guardrails: dict[str, str] = {}
    for name, value in guardrails.items():
        safe_name = _redacted(name)
        if safe_name in redacted_guardrails:
            raise ValueError("redacted guardrail names are ambiguous")
        redacted_guardrails[safe_name] = _redacted(value)
    return redacted_guardrails


def _redacted(value: str) -> str:
    return redact(value) or "[REDACTED]"


__all__ = [
    "ActionsCandidateExperimentAdapter",
    "DirectCandidateExperimentAdapter",
    "FoundryCandidateExperimentOperation",
    "build_production_candidate_experiment_runner",
    "build_production_direct_candidate_experiment_adapter",
]
