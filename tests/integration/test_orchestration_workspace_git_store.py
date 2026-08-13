from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
from threading import Barrier

import pytest

from foundry_opt.orchestration import (
    CandidateSummary,
    detect_workspace_state_v3,
    GitWorkspaceStore,
    WorkspaceCompletedError,
    WorkspaceBaselineRecord,
    WorkspaceConflictError,
    WorkspaceCorruptionError,
    WorkspaceExperimentRecord,
    WorkspaceMigrationRequiredError,
    WorkspaceLineage,
    WorkspacePhase,
    WorkspacePrivacyError,
    WorkspaceSpecificationRecord,
    WorkspaceUpdate,
)


def _run(arguments: tuple[str, ...], cwd: Path) -> str:
    return subprocess.run(
        arguments,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _repository(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    repository = tmp_path / "repository"
    _run(("git", "init", "--bare", str(origin)), tmp_path)
    _run(("git", "init", "-b", "main", str(repository)), tmp_path)
    _run(("git", "config", "user.name", "Workspace Test"), repository)
    _run(
        ("git", "config", "user.email", "workspace@example.invalid"),
        repository,
    )
    (repository / "README.md").write_text("workspace tests\n")
    _run(("git", "add", "."), repository)
    _run(("git", "commit", "-m", "baseline"), repository)
    _run(("git", "remote", "add", "origin", str(origin)), repository)
    _run(("git", "push", "-u", "origin", "main"), repository)
    return repository, origin


def _tree(repository: Path, revision: str) -> tuple[str, ...]:
    return tuple(
        _run(
            ("git", "ls-tree", "-r", "--name-only", revision),
            repository,
        ).splitlines()
    )


def test_git_workspace_store_commits_and_loads_compact_v4_state(
    tmp_path: Path,
) -> None:
    repository, _ = _repository(tmp_path)
    store = GitWorkspaceStore(repository)
    patch = b"diff --git a/agent.py b/agent.py\n"
    lineage = WorkspaceLineage(
        spec_sha256="a" * 64,
        base_commit="b" * 40,
        patch_sha256=hashlib.sha256(patch).hexdigest(),
        evidence_sha256="c" * 64,
        bundle_sha256="d" * 64,
        expected_tree="e" * 40,
        selected_candidate_id="candidate-1",
        workspace_pull_request_number=104,
        required_checks={"tests": "success"},
        required_checks_provenance=(
            f"trusted-selector:head:{'f' * 40}"
        ),
    )
    specification = WorkspaceSpecificationRecord(
        status="policy_approved",
        spec_sha256="a" * 64,
        base_commit="b" * 40,
        target="support-agent",
        environment="development",
        asset_ids=("development", "validation", "quality"),
        metric_names=("policy_coverage",),
        policy_reason="repository policy approved immutable assets",
    )
    baseline = WorkspaceBaselineRecord(
        status="completed",
        operation_sha256="3" * 64,
        idempotency_key="4" * 64,
        bundle_sha256="5" * 64,
        evidence_sha256="6" * 64,
        dataset_ids=("development", "validation"),
        evaluator_ids=("quality",),
        split="development",
        sample_count=24,
        executor="direct_oidc",
        draft_id="baseline-draft",
        evaluation_id="baseline-evaluation",
        run_id="baseline-run",
        metrics={"policy_coverage": 0.2},
        guardrails={"safety": "pass"},
    )

    first = store.commit(
        expected_revision=None,
        update=WorkspaceUpdate(
            issue_number=31,
            phase=WorkspacePhase.SPECIFICATION,
            workspace_pull_request_number=104,
            semantic_event="issue_created",
            specification=specification,
            baseline=baseline,
        ),
    )
    second = store.commit(
        expected_revision=first.revision,
        update=WorkspaceUpdate(
            issue_number=31,
            phase=WorkspacePhase.AWAITING_SELECTION,
            workspace_pull_request_number=104,
            semantic_event="candidate_slate_published",
            candidates=(
                CandidateSummary(
                    candidate_id="candidate-1",
                    metrics={"policy_coverage": 0.5},
                    eligible=True,
                    selected=True,
                ),
            ),
            selected_patch=patch,
            external_operation_ids=("evalrun-123",),
            experiments=(
                WorkspaceExperimentRecord(
                    candidate_id="candidate-1",
                    patch_sha256=hashlib.sha256(patch).hexdigest(),
                    bundle_sha256="d" * 64,
                    evidence_sha256="c" * 64,
                    idempotency_key="1" * 64,
                    operation_sha256="2" * 64,
                    status="completed",
                    executor="direct_oidc",
                    draft_id="draft-1",
                    evaluation_id="evaluation-1",
                    run_id="run-1",
                    metrics={"policy_coverage": 0.5},
                    guardrails={"safety": "pass"},
                ),
            ),
            lineage=lineage,
            specification=specification,
            baseline=baseline,
        ),
    )

    assert store.load(31) == second
    assert second.lineage == lineage
    assert _tree(repository, second.revision) == (
        "evidence/candidates.json",
        "journal.jsonl",
        "patches/selected.patch",
        "snapshot.json",
    )
    snapshot_bytes = subprocess.run(
        ("git", "show", f"{second.revision}:snapshot.json"),
        cwd=repository,
        check=True,
        capture_output=True,
    ).stdout
    snapshot = json.loads(snapshot_bytes)
    assert snapshot_bytes == (
        json.dumps(
            snapshot,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
    assert snapshot["state"]["lineage"]["spec_sha256"] == "a" * 64
    assert snapshot["state"]["specification"]["spec_sha256"] == "a" * 64
    assert snapshot["state"]["baseline"]["sample_count"] == 24
    assert snapshot["state"]["experiments"][0]["status"] == "completed"
    assert snapshot["state"]["experiments"][0]["metrics"] == {
        "policy_coverage": 0.5
    }
    journal = _run(
        ("git", "show", f"{second.revision}:journal.jsonl"),
        repository,
    ).splitlines()
    first_entry, second_entry = map(json.loads, journal)
    assert first_entry["semantic_event"] == "issue_created"
    assert second_entry["semantic_event"] == "candidate_slate_published"
    assert second_entry["previous_sha256"] == first_entry["entry_sha256"]
    assert second_entry["entry_sha256"] == hashlib.sha256(
        (
            json.dumps(
                {
                    key: value
                    for key, value in second_entry.items()
                    if key != "entry_sha256"
                },
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode()
    ).hexdigest()


def test_git_workspace_store_omits_empty_optional_artifacts(
    tmp_path: Path,
) -> None:
    repository, _ = _repository(tmp_path)
    snapshot = GitWorkspaceStore(repository).commit(
        expected_revision=None,
        update=WorkspaceUpdate(
            issue_number=31,
            phase=WorkspacePhase.SPECIFICATION,
            workspace_pull_request_number=None,
            semantic_event="issue_created",
        ),
    )

    assert _tree(repository, snapshot.revision) == (
        "journal.jsonl",
        "snapshot.json",
    )


def test_git_workspace_store_uses_compare_and_swap(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    store = GitWorkspaceStore(repository)
    created = store.commit(
        expected_revision=None,
        update=WorkspaceUpdate(
            issue_number=31,
            phase=WorkspacePhase.SPECIFICATION,
            workspace_pull_request_number=None,
            semantic_event="issue_created",
        ),
    )
    barrier = Barrier(2)

    def commit(event: str) -> str:
        barrier.wait()
        return store.commit(
            expected_revision=created.revision,
            update=WorkspaceUpdate(
                issue_number=31,
                phase=WorkspacePhase.EVALUATING,
                workspace_pull_request_number=None,
                semantic_event=event,
            ),
        ).revision

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [
            executor.submit(commit, "evaluation_started"),
            executor.submit(commit, "evaluation_retried"),
        ]
    revisions: list[str] = []
    conflicts = 0
    for outcome in outcomes:
        try:
            revisions.append(outcome.result())
        except WorkspaceConflictError:
            conflicts += 1

    assert len(revisions) == 1
    assert conflicts == 1
    assert store.load(31).revision == revisions[0]  # type: ignore[union-attr]


def test_git_workspace_store_fails_closed_on_privacy_and_corruption(
    tmp_path: Path,
) -> None:
    repository, origin = _repository(tmp_path)
    store = GitWorkspaceStore(repository)

    with pytest.raises(WorkspacePrivacyError):
        store.commit(
            expected_revision=None,
            update=WorkspaceUpdate(
                issue_number=31,
                phase=WorkspacePhase.SPECIFICATION,
                workspace_pull_request_number=None,
                semantic_event="github_pat_not-a-real-token",
            ),
        )

    created = store.commit(
        expected_revision=None,
        update=WorkspaceUpdate(
            issue_number=31,
            phase=WorkspacePhase.SPECIFICATION,
            workspace_pull_request_number=None,
            semantic_event="issue_created",
        ),
    )
    snapshot = json.loads(
        _run(("git", "show", f"{created.revision}:snapshot.json"), repository)
    )
    snapshot["journal_head"] = "0" * 64
    bad_snapshot = json.dumps(
        snapshot,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"
    _run(("git", "checkout", "--orphan", "corrupt-workspace"), repository)
    _run(("git", "rm", "-rf", "."), repository)
    (repository / "snapshot.json").write_text(bad_snapshot)
    (repository / "journal.jsonl").write_text(
        _run(
            ("git", "show", f"{created.revision}:journal.jsonl"),
            repository,
        )
    )
    _run(("git", "add", "."), repository)
    _run(("git", "commit", "-m", "corrupt workspace"), repository)
    _run(
        (
            "git",
            "push",
            "--force",
            str(origin),
            "HEAD:refs/heads/foundry-opt/state/issue-31",
        ),
        repository,
    )

    with pytest.raises(WorkspaceCorruptionError):
        store.load(31)


def test_git_workspace_store_detects_v3_without_mutating_legacy_ref(
    tmp_path: Path,
) -> None:
    repository, _ = _repository(tmp_path)
    snapshot = json.dumps(
        {
            "journal_head": "b" * 64,
            "schema_version": 3,
            "state": {"schema_version": 2},
        },
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"
    journal = json.dumps(
        {
            "entry_sha256": "b" * 64,
            "schema_version": 3,
        },
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"
    _run(("git", "checkout", "--orphan", "legacy-v3"), repository)
    _run(("git", "rm", "-rf", "."), repository)
    (repository / "snapshot.json").write_text(snapshot)
    (repository / "journal.jsonl").write_text(journal)
    _run(("git", "add", "."), repository)
    _run(("git", "commit", "-m", "legacy v3 workspace"), repository)
    legacy_revision = _run(("git", "rev-parse", "HEAD"), repository).strip()
    _run(
        (
            "git",
            "push",
            "origin",
            "HEAD:refs/heads/foundry-opt/state/issue-31",
        ),
        repository,
    )
    store = GitWorkspaceStore(repository)

    plan = detect_workspace_state_v3(repository, 31)
    with pytest.raises(WorkspaceMigrationRequiredError) as caught:
        store.load(31)

    assert plan is not None
    assert plan.source_revision == legacy_revision
    assert caught.value.plan.source_revision == legacy_revision
    assert caught.value.plan.read_only is True
    assert (
        _run(
            (
                "git",
                "ls-remote",
                "origin",
                "refs/heads/foundry-opt/state/issue-31",
            ),
            repository,
        ).split()[0]
        == legacy_revision
    )


def test_finalize_creates_minimal_audit_and_completion_tombstone(
    tmp_path: Path,
) -> None:
    repository, _ = _repository(tmp_path)
    store = GitWorkspaceStore(repository)
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
            external_operation_ids=("evalrun-123", "deployment-run-456"),
        ),
    )

    audit = store.finalize(31)

    assert audit.final_snapshot == snapshot
    assert audit.journal == ("retained_improvement",)
    assert audit.external_operation_ids == (
        "evalrun-123",
        "deployment-run-456",
    )
    assert set(_tree(repository, snapshot.revision)) == set(
        audit.retained_paths
    )
    audit_revision = _run(
        (
            "git",
            "ls-remote",
            "origin",
            "refs/heads/foundry-opt/audit/issue-31",
        ),
        repository,
    ).split()[0]
    assert audit_revision == snapshot.revision
    active_revision = _run(
        ("git", "ls-remote", "origin", "refs/heads/foundry-opt/state/issue-31"),
        repository,
    ).split()[0]
    _run(
        (
            "git",
            "fetch",
            "origin",
            "refs/heads/foundry-opt/state/issue-31",
        ),
        repository,
    )
    assert _tree(repository, active_revision) == ("completion.json",)
    assert store.load(31) is None
    assert store.finalize(31) == audit
    with pytest.raises(WorkspaceCompletedError):
        store.commit(
            expected_revision=None,
            update=WorkspaceUpdate(
                issue_number=31,
                phase=WorkspacePhase.EVALUATING,
                workspace_pull_request_number=104,
                semantic_event="late_update",
            ),
        )
