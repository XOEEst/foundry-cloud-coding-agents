import hashlib

from foundry_opt.orchestration import (
    CandidateSummary,
    InMemoryWorkspaceStore,
    WorkspaceExperimentRecord,
    WorkspaceLineage,
    WorkspacePhase,
    WorkspaceUpdate,
)


def test_finalize_retains_only_the_minimal_audit_bundle() -> None:
    store = InMemoryWorkspaceStore()

    snapshot = store.commit(
        expected_revision=None,
        update=WorkspaceUpdate(
            issue_number=31,
            phase=WorkspacePhase.COMPLETED,
            workspace_pull_request_number=104,
            semantic_event="retained_improvement",
            candidates=(
                CandidateSummary(
                    candidate_id="candidate-1",
                    metrics={"policy_coverage": 0.5},
                    eligible=True,
                    selected=True,
                ),
            ),
            selected_patch=b"diff --git a/agent.py b/agent.py\n",
            external_operation_ids=(
                "evalrun-123",
                "deployment-run-456",
            ),
            experiments=(
                WorkspaceExperimentRecord(
                    candidate_id="candidate-1",
                    mutation_class="system_instructions",
                    patch_sha256=hashlib.sha256(
                        b"diff --git a/agent.py b/agent.py\n"
                    ).hexdigest(),
                    bundle_sha256="e" * 64,
                    evidence_sha256="d" * 64,
                    idempotency_key="1" * 64,
                    operation_sha256="2" * 64,
                    status="completed",
                    changed_paths=("agent.py",),
                    validation=("pytest: passed",),
                    expected_tree="b" * 40,
                    executor="direct_oidc",
                    draft_id="draft-1",
                    evaluation_id="evaluation-1",
                    run_id="run-1",
                    metrics={"policy_coverage": 0.5},
                    guardrails={"safety": "pass"},
                ),
            ),
            lineage=WorkspaceLineage(
                spec_sha256="a" * 64,
                base_commit="b" * 40,
                patch_sha256=hashlib.sha256(
                    b"diff --git a/agent.py b/agent.py\n"
                ).hexdigest(),
                evidence_sha256="d" * 64,
                bundle_sha256="e" * 64,
                expected_tree="f" * 40,
                selected_candidate_id="candidate-1",
                workspace_pull_request_number=104,
                required_checks={"tests": "success"},
                required_checks_provenance=(
                    f"trusted-selector:head:{'1' * 40}"
                ),
            ),
        ),
    )

    audit = store.finalize(31)

    assert snapshot.revision == "1"
    assert audit.issue_number == 31
    assert audit.final_snapshot == snapshot
    assert audit.journal == ("retained_improvement",)
    assert audit.candidates == snapshot.candidates
    assert audit.selected_patch == b"diff --git a/agent.py b/agent.py\n"
    assert audit.external_operation_ids == (
        "evalrun-123",
        "deployment-run-456",
    )
    assert audit.experiments == snapshot.experiments
    assert audit.lineage == snapshot.lineage
    assert audit.retained_paths == (
        "snapshot.json",
        "journal.jsonl",
        "evidence/candidates.json",
        "patches/selected.patch",
    )
    assert store.load(31) is None


def test_finalize_omits_empty_optional_audit_artifacts() -> None:
    store = InMemoryWorkspaceStore()
    store.commit(
        expected_revision=None,
        update=WorkspaceUpdate(
            issue_number=31,
            phase=WorkspacePhase.SPECIFICATION,
            workspace_pull_request_number=None,
            semantic_event="issue_created",
        ),
    )

    audit = store.finalize(31)

    assert audit.retained_paths == ("snapshot.json", "journal.jsonl")
