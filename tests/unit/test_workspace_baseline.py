from pathlib import Path

import pytest

from foundry_opt.orchestration import (
    CandidateExperimentOperation,
    CandidateExperimentPending,
    CandidateExperimentRequest,
    CandidateExperimentResult,
    InMemoryWorkspaceStore,
    WorkspaceBaselineExecutor,
    WorkspaceBaselinePlan,
    WorkspacePhase,
    WorkspaceSpecificationRecord,
    WorkspaceUpdate,
)


def _store() -> InMemoryWorkspaceStore:
    store = InMemoryWorkspaceStore()
    store.commit(
        expected_revision=None,
        update=WorkspaceUpdate(
            issue_number=31,
            phase=WorkspacePhase.SPECIFICATION,
            workspace_pull_request_number=104,
            semantic_event="trusted_specification_resolved",
            specification=WorkspaceSpecificationRecord(
                status="policy_approved",
                spec_sha256="a" * 64,
                base_commit="b" * 40,
                target="support-agent",
                environment="development",
                asset_ids=("development", "validation", "quality"),
                metric_names=("quality",),
                policy_reason=(
                    "repository policy approved immutable assets"
                ),
            ),
        ),
    )
    return store


class Builder:
    def build(self, **kwargs) -> WorkspaceBaselinePlan:
        specification = kwargs["specification"]
        return WorkspaceBaselinePlan(
            request=CandidateExperimentRequest(
                issue_number=kwargs["issue_number"],
                candidate_id="baseline",
                patch_sha256=specification.spec_sha256,
                bundle_sha256="c" * 64,
                evidence_sha256="d" * 64,
                idempotency_key="e" * 64,
            ),
            dataset_ids=("development", "validation"),
            evaluator_ids=("quality",),
            sample_count=24,
        )


class Runner:
    def __init__(self, *, pending: bool = False) -> None:
        self.pending = pending
        self.calls = 0

    def evaluate(self, request):
        self.calls += 1
        if self.pending:
            raise CandidateExperimentPending(request.idempotency_key)
        return CandidateExperimentResult(
            candidate_id="baseline",
            executor="direct_oidc",
            metrics={"quality": 0.72},
            guardrails={"safety": "pass"},
            draft_id="baseline-draft",
            evaluation_id="baseline-evaluation",
            run_id="baseline-run",
            bundle_sha256=request.bundle_sha256,
            evidence_sha256=request.evidence_sha256,
            operation_sha256=(
                CandidateExperimentOperation.from_request(request).sha256
            ),
            idempotency_key=request.idempotency_key,
        )


def test_trusted_baseline_persists_and_retry_does_not_rerun(
    tmp_path: Path,
) -> None:
    store = _store()
    runner = Runner()
    executor = WorkspaceBaselineExecutor(
        store=store,
        runner=runner,
        request_builder=Builder(),
    )

    first = executor.execute(
        repository_root=tmp_path,
        issue_number=31,
        target="support-agent",
        base_commit="b" * 40,
    )
    retry = executor.execute(
        repository_root=tmp_path,
        issue_number=31,
        target="support-agent",
        base_commit="b" * 40,
    )

    baseline = store.load(31).baseline
    assert first.status == "completed"
    assert retry.recorded is False
    assert runner.calls == 1
    assert baseline.metrics == {"quality": 0.72}
    assert baseline.sample_count == 24
    assert baseline.dataset_ids == ("development", "validation")


def test_pending_baseline_persists_without_accepting_result_fields(
    tmp_path: Path,
) -> None:
    store = _store()
    result = WorkspaceBaselineExecutor(
        store=store,
        runner=Runner(pending=True),
        request_builder=Builder(),
    ).execute(
        repository_root=tmp_path,
        issue_number=31,
        target="support-agent",
        base_commit="b" * 40,
    )

    baseline = store.load(31).baseline
    assert result.status == "pending"
    assert result.next_action == "await_trusted_actions_result"
    assert baseline.metrics == {}
    assert baseline.executor is None


def test_baseline_rejects_metrics_outside_trusted_specification(
    tmp_path: Path,
) -> None:
    class ForgedRunner(Runner):
        def evaluate(self, request):
            result = super().evaluate(request)
            return CandidateExperimentResult(
                candidate_id=result.candidate_id,
                executor=result.executor,
                metrics={"forged": 99.0},
                guardrails=result.guardrails,
                draft_id=result.draft_id,
                evaluation_id=result.evaluation_id,
                run_id=result.run_id,
                bundle_sha256=result.bundle_sha256,
                evidence_sha256=result.evidence_sha256,
                operation_sha256=result.operation_sha256,
                idempotency_key=result.idempotency_key,
            )

    with pytest.raises(ValueError, match="baseline lineage changed"):
        WorkspaceBaselineExecutor(
            store=_store(),
            runner=ForgedRunner(),
            request_builder=Builder(),
        ).execute(
            repository_root=tmp_path,
            issue_number=31,
            target="support-agent",
            base_commit="b" * 40,
        )
