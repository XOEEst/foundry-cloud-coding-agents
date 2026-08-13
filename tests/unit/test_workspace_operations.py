import pytest
from pathlib import Path
from types import SimpleNamespace

from foundry_opt.orchestration import (
    TrustedWorkspaceOperationContext,
    WorkspaceTrigger,
    normalize_workspace_operation,
)
from foundry_opt.orchestration.git_transport import SafePushRemote
import foundry_opt.orchestration.workspace_operation_store as operation_store


def _payload() -> dict:
    return {
        "schema_version": 1,
        "kind": "deployment_result",
        "status": "completed",
        "issue_number": 31,
        "workspace_pull_request_number": 104,
        "operation_id": "deployment-123",
        "candidate_id": "candidate-2",
        "patch_sha256": "a" * 64,
        "bundle_sha256": "b" * 64,
        "evidence_sha256": "c" * 64,
        "predecessor_operation_id": None,
        "repository": {
            "full_name": "octo-org/optimizer",
            "id": 123,
        },
    }


def _context() -> TrustedWorkspaceOperationContext:
    return TrustedWorkspaceOperationContext(
        delivery_id="delivery-123",
        repository="octo-org/optimizer",
        repository_id=123,
    )


def test_operation_store_deletes_ref_through_validated_remote_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = object.__new__(operation_store.GitWorkspaceOperationStore)
    store._root = tmp_path
    store._remote = "origin"
    store._git = SimpleNamespace(
        _remote_revision=lambda ref: "a" * 40,
    )
    commands: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        operation_store,
        "resolve_safe_push_remote",
        lambda root, remote: SafePushRemote(
            name=remote,
            url="https://github.com/octo-org/optimizer.git",
            isolated=False,
        ),
    )
    monkeypatch.setattr(
        operation_store,
        "_run",
        lambda *arguments: commands.append(arguments),
    )

    assert store.delete_ref(31) is True
    assert commands == [
        (
            tmp_path,
            "git",
            "push",
            "https://github.com/octo-org/optimizer.git",
            ":refs/heads/foundry-opt/operations/issue-31",
        )
    ]


def test_trusted_workspace_operation_normalizes_completed_lineage() -> None:
    event = normalize_workspace_operation(_payload(), _context())

    assert event.issue_number == 31
    assert event.operation.trigger is WorkspaceTrigger.DEPLOYMENT_COMPLETED
    assert event.operation.operation_id == "deployment-123"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("status", "running", "not completed"),
        ("workspace_pull_request_number", 105, "lineage"),
        ("bundle_sha256", "d" * 64, "lineage"),
    ),
)
def test_lifecycle_rejects_forged_trusted_operation_lineage(
    field: str,
    value,
    message: str,
    tmp_path: Path,
) -> None:
    from dataclasses import replace
    import hashlib

    from foundry_opt.orchestration import (
        CandidateSummary,
        InMemoryWorkspaceStore,
        OptimizationWorkspace,
        WorkspaceIssue,
        WorkspaceLineage,
        WorkspacePhase,
        WorkspacePullRequest,
        WorkspaceRequest,
        WorkspaceUpdate,
    )

    payload = _payload()
    payload[field] = value
    if field == "status":
        with pytest.raises(ValueError, match=message):
            normalize_workspace_operation(payload, _context())
        return
    operation = normalize_workspace_operation(payload, _context()).operation
    patch = b"selected"
    patch_sha = hashlib.sha256(patch).hexdigest()
    store = InMemoryWorkspaceStore()
    issue = WorkspaceIssue(31, "[Optimize] X", "body", "f" * 40)
    pr = WorkspacePullRequest(
        104,
        31,
        "foundry-opt/workspace/issue-31",
        "[Optimize] #31 selected candidate",
        False,
        True,
        "f" * 40,
    )
    store.commit(
        expected_revision=None,
        update=WorkspaceUpdate(
            issue_number=31,
            phase=WorkspacePhase.DEPLOYMENT,
            workspace_pull_request_number=104,
            semantic_event="merged",
            candidates=(
                CandidateSummary(
                    "candidate-2",
                    {"quality": 1.0},
                    True,
                    True,
                ),
            ),
            selected_patch=patch,
            external_operation_ids=(
                f"candidate-2:patch:{patch_sha}",
                f"candidate-2:bundle:{'b' * 64}",
                f"candidate-2:evidence:{'c' * 64}",
            ),
            lineage=WorkspaceLineage(
                spec_sha256="a" * 64,
                base_commit="f" * 40,
                patch_sha256=patch_sha,
                evidence_sha256="c" * 64,
                bundle_sha256="b" * 64,
                expected_tree="e" * 40,
                selected_candidate_id="candidate-2",
                workspace_pull_request_number=104,
                required_checks={"tests": "success"},
                required_checks_provenance=(
                    f"trusted-selector:head:{'d' * 40}"
                ),
            ),
        ),
    )
    operation = replace(operation, patch_sha256=patch_sha)

    with pytest.raises(ValueError, match=message):
        OptimizationWorkspace(store=store).advance(
            WorkspaceRequest(
                repository_root=tmp_path,
                issue=issue,
                trigger=WorkspaceTrigger.DEPLOYMENT_COMPLETED,
                workspace_pull_request=pr,
                operation=operation,
            )
        )
