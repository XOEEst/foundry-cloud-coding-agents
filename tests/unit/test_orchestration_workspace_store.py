import hashlib

import foundry_opt.orchestration.workspace_git_store as workspace_git_store
from foundry_opt.orchestration import (
    CandidateSummary,
    InMemoryWorkspaceStore,
    WorkspaceExperimentRecord,
    WorkspaceCandidateProvenance,
    WorkspaceLineage,
    WorkspacePhase,
    WorkspaceUpdate,
)


def test_finalize_retains_only_the_minimal_audit_bundle() -> None:
    store = InMemoryWorkspaceStore()
    provenance = WorkspaceCandidateProvenance(
        copilot_actor_id=198982749,
        copilot_actor_login="Copilot",
        candidate_source_commit_sha="9" * 40,
        candidate_source_commit_url=(
            "https://github.com/octo-org/optimizer/commit/" + "9" * 40
        ),
        acknowledgement_comment_id=501,
        acknowledgement_comment_url=(
            "https://github.com/octo-org/optimizer/pull/"
            "104#issuecomment-501"
        ),
        assignment_marker_key="issue-31:assignment-a1:v1",
        workspace_pr_number=104,
        importer_workflow_run_id=9001,
        importer_workflow_run_url=(
            "https://github.com/octo-org/optimizer/actions/runs/9001"
        ),
        trusted_event_name="issue_comment",
    )

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
                    provenance=provenance,
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
                candidate_provenance=provenance,
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
    assert audit.experiments[0].provenance == provenance
    assert audit.lineage == snapshot.lineage
    assert audit.lineage.candidate_provenance == provenance
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


def test_compact_v5_reconstructs_provenance_and_reads_v4_records() -> None:
    provenance = WorkspaceCandidateProvenance(
        copilot_actor_id=198982749,
        copilot_actor_login="Copilot",
        candidate_source_commit_sha="9" * 40,
        candidate_source_commit_url=(
            "https://github.com/octo-org/optimizer/commit/" + "9" * 40
        ),
        acknowledgement_comment_id=501,
        acknowledgement_comment_url=(
            "https://github.com/octo-org/optimizer/pull/"
            "104#issuecomment-501"
        ),
        assignment_marker_key="issue-31:assignment-a1:v1",
        workspace_pr_number=104,
        importer_workflow_run_id=9001,
        importer_workflow_run_url=(
            "https://github.com/octo-org/optimizer/actions/runs/9001"
        ),
        trusted_event_name="schedule",
    )
    record = WorkspaceExperimentRecord(
        candidate_id="candidate-1",
        mutation_class="system_instructions",
        patch_sha256="a" * 64,
        bundle_sha256="b" * 64,
        evidence_sha256="c" * 64,
        idempotency_key="d" * 64,
        operation_sha256="e" * 64,
        status="pending",
        changed_paths=("agent.py",),
        validation=("pytest: passed",),
        expected_tree="f" * 40,
        provenance=provenance,
    )

    document = workspace_git_store._experiment_to_document(record)

    assert workspace_git_store._SCHEMA_VERSION == 5
    assert workspace_git_store._experiments_from_document(
        [document]
    ) == (record,)
    workspace_git_store._validate_update(
        WorkspaceUpdate(
            issue_number=31,
            phase=WorkspacePhase.EVALUATING,
            workspace_pull_request_number=104,
            semantic_event="candidate_experiment_started_candidate-1",
            experiments=(record,),
        )
    )
    legacy = dict(document)
    legacy.pop("provenance")
    assert workspace_git_store._experiments_from_document(
        [legacy]
    )[0].provenance is None
