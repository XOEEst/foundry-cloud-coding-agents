from foundry_opt.orchestration import (
    CandidateSummary,
    InMemoryWorkspaceStore,
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
