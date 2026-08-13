import hashlib
from pathlib import Path

import pytest

from foundry_opt.orchestration import (
    CandidateExperimentOperation,
    CandidateExperimentPending,
    CandidateExperimentRequest,
    CandidateExperimentResult,
    InMemoryWorkspaceStore,
    WorkspaceCandidateProposal,
    WorkspaceExperimentExecutor,
    WorkspacePhase,
    WorkspaceUpdate,
    TrustedWorkspaceExperimentResultContext,
    normalize_workspace_experiment_result,
)


def _proposal() -> WorkspaceCandidateProposal:
    return WorkspaceCandidateProposal(
        candidate_id="candidate-1",
        exact_patch=b"trusted proposal patch",
        idempotency_key="a" * 64,
        experiment_reference="target:support-agent",
        summary="Improve quality.",
        changed_paths=("agent.py",),
        validation=("tests passed",),
        expected_tree="b" * 40,
    )


class Builder:
    def build(self, **kwargs) -> CandidateExperimentRequest:
        proposal = kwargs["proposal"]
        return CandidateExperimentRequest(
            issue_number=kwargs["issue_number"],
            candidate_id=proposal.candidate_id,
            patch_sha256=proposal.patch_sha256,
            bundle_sha256="c" * 64,
            evidence_sha256="d" * 64,
            idempotency_key=proposal.idempotency_key,
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
            candidate_id=request.candidate_id,
            executor="direct_oidc",
            metrics={"quality": 0.9},
            guardrails={"safety": "pass"},
            draft_id="draft-1",
            evaluation_id="evaluation-1",
            run_id="run-1",
            bundle_sha256=request.bundle_sha256,
            evidence_sha256=request.evidence_sha256,
            operation_sha256=(
                CandidateExperimentOperation.from_request(request).sha256
            ),
            idempotency_key=request.idempotency_key,
        )


def _store() -> InMemoryWorkspaceStore:
    store = InMemoryWorkspaceStore()
    store.commit(
        expected_revision=None,
        update=WorkspaceUpdate(
            issue_number=31,
            phase=WorkspacePhase.SPECIFICATION,
            workspace_pull_request_number=104,
            semantic_event="issue_created",
        ),
    )
    return store


def test_trusted_execution_persists_result_and_retry_does_not_rerun(
    tmp_path: Path,
) -> None:
    store = _store()
    runner = Runner()
    executor = WorkspaceExperimentExecutor(
        store=store,
        runner=runner,
        request_builder=Builder(),
    )

    first = executor.execute(
        repository_root=tmp_path,
        issue_number=31,
        target="support-agent",
        base_commit="e" * 40,
        proposal=_proposal(),
    )
    retry = executor.execute(
        repository_root=tmp_path,
        issue_number=31,
        target="support-agent",
        base_commit="e" * 40,
        proposal=_proposal(),
    )

    record = store.load(31).experiments[0]
    assert first.status == "completed"
    assert retry.recorded is False
    assert runner.calls == 1
    assert record.metrics == {"quality": 0.9}
    assert record.guardrails == {"safety": "pass"}
    assert record.bundle_sha256 == "c" * 64
    assert record.evidence_sha256 == "d" * 64
    assert record.patch_sha256 == hashlib.sha256(
        _proposal().exact_patch
    ).hexdigest()


def test_actions_pending_persists_operation_without_result_fields(
    tmp_path: Path,
) -> None:
    store = _store()
    runner = Runner(pending=True)
    result = WorkspaceExperimentExecutor(
        store=store,
        runner=runner,
        request_builder=Builder(),
    ).execute(
        repository_root=tmp_path,
        issue_number=31,
        target="support-agent",
        base_commit="e" * 40,
        proposal=_proposal(),
    )

    record = store.load(31).experiments[0]
    assert result.status == "pending"
    assert result.next_action == "await_trusted_actions_result"
    assert record.status == "pending"
    assert record.metrics == {}
    assert record.draft_id is None


def test_trusted_actions_result_completes_pending_and_reconciles_retry(
    tmp_path: Path,
) -> None:
    store = _store()
    WorkspaceExperimentExecutor(
        store=store,
        runner=Runner(pending=True),
        request_builder=Builder(),
    ).execute(
        repository_root=tmp_path,
        issue_number=31,
        target="support-agent",
        base_commit="e" * 40,
        proposal=_proposal(),
    )
    pending = store.load(31).experiments[0]
    result = CandidateExperimentResult(
        candidate_id=pending.candidate_id,
        executor="actions_oidc",
        metrics={"quality": 0.91},
        guardrails={"safety": "pass"},
        draft_id="draft-actions-1",
        evaluation_id="evaluation-actions-1",
        run_id="run-actions-1",
        bundle_sha256=pending.bundle_sha256,
        evidence_sha256=pending.evidence_sha256,
        operation_sha256=pending.operation_sha256,
        idempotency_key=pending.idempotency_key,
    )
    executor = WorkspaceExperimentExecutor(
        store=store,
        runner=None,
        request_builder=None,
    )

    first = executor.ingest_result(issue_number=31, result=result)
    retry = executor.ingest_result(issue_number=31, result=result)

    record = store.load(31).experiments[0]
    assert first.recorded is True
    assert retry.recorded is False
    assert record.status == "completed"
    assert record.metrics == {"quality": 0.91}
    assert record.evidence_sha256 == "d" * 64


def test_trusted_actions_result_rejects_changed_lineage(
    tmp_path: Path,
) -> None:
    store = _store()
    WorkspaceExperimentExecutor(
        store=store,
        runner=Runner(pending=True),
        request_builder=Builder(),
    ).execute(
        repository_root=tmp_path,
        issue_number=31,
        target="support-agent",
        base_commit="e" * 40,
        proposal=_proposal(),
    )
    pending = store.load(31).experiments[0]
    forged = CandidateExperimentResult(
        candidate_id=pending.candidate_id,
        executor="actions_oidc",
        metrics={"quality": 99.0},
        guardrails={"safety": "pass"},
        draft_id="draft-actions-1",
        evaluation_id="evaluation-actions-1",
        run_id="run-actions-1",
        bundle_sha256=pending.bundle_sha256,
        evidence_sha256="f" * 64,
        operation_sha256=pending.operation_sha256,
        idempotency_key=pending.idempotency_key,
    )

    with pytest.raises(ValueError, match="lineage changed"):
        WorkspaceExperimentExecutor(
            store=store,
            runner=None,
            request_builder=None,
        ).ingest_result(issue_number=31, result=forged)

    assert store.load(31).experiments[0] == pending


def test_trusted_actions_payload_rejects_repository_spoof() -> None:
    payload = {
        "schema_version": 1,
        "issue_number": 31,
        "candidate_id": "candidate-1",
        "executor": "actions_oidc",
        "metrics": {"quality": 0.9},
        "guardrails": {"safety": "pass"},
        "draft_id": "draft-1",
        "evaluation_id": "evaluation-1",
        "run_id": "run-1",
        "bundle_sha256": "c" * 64,
        "evidence_sha256": "d" * 64,
        "operation_sha256": "e" * 64,
        "idempotency_key": "a" * 64,
        "repository": {
            "full_name": "evil/example",
            "id": 123,
        },
    }

    with pytest.raises(ValueError, match="repository changed"):
        normalize_workspace_experiment_result(
            payload,
            TrustedWorkspaceExperimentResultContext(
                delivery_id="delivery-1",
                repository="octo-org/optimizer",
                repository_id=123,
            ),
        )
