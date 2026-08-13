from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

import pytest

from foundry_opt.orchestration import (
    AdvanceRequest,
    CampaignEvent,
    EventKind,
    GitStateRef,
    OptimizationCampaign,
)
from foundry_opt.orchestration.workspace_migration import (
    IssueLifecycle,
    WorkspaceLegacyCleanupPlan,
    WorkspaceMigrationError,
    WorkspaceMigrationService,
)


NOW = datetime(2026, 8, 12, tzinfo=UTC)


def _run(arguments: tuple[str, ...], cwd: Path) -> str:
    return subprocess.run(
        arguments,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    repository = tmp_path / "repository"
    _run(("git", "init", "--bare", "--initial-branch=main", str(origin)), tmp_path)
    _run(("git", "init", "-b", "main", str(repository)), tmp_path)
    _run(("git", "config", "user.name", "Cleanup Test"), repository)
    _run(("git", "config", "user.email", "cleanup@example.invalid"), repository)
    (repository / "README.md").write_text("cleanup tests\n", encoding="utf-8")
    _run(("git", "add", "."), repository)
    _run(("git", "commit", "-m", "baseline"), repository)
    _run(("git", "remote", "add", "origin", str(origin)), repository)
    _run(("git", "push", "-u", "origin", "main"), repository)
    return repository


@pytest.fixture
def repository() -> Path:
    root = Path.cwd() / ".cleanup-test" / uuid4().hex[:8]
    root.mkdir(parents=True)
    try:
        yield _repository(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _v3_state(repository: Path, issue_number: int) -> str:
    event = CampaignEvent(
        event_id=f"issue-{issue_number}-created",
        kind=EventKind.ISSUE_CREATED,
        generation=1,
        occurred_at=NOW,
        payload={},
    )
    state = OptimizationCampaign().advance(
        AdvanceRequest(issue_number, None, (event,))
    ).state
    return GitStateRef().commit(
        repository,
        issue_number=issue_number,
        expected_revision=None,
        state=state,
        inbox=(event,),
    ).revision


def _install_ref(repository: Path, ref: str, message: str) -> str:
    tree = _run(("git", "rev-parse", "HEAD^{tree}"), repository)
    revision = _run(
        ("git", "commit-tree", tree, "-m", message),
        repository,
    )
    _run(
        ("git", "push", "--force", "origin", f"{revision}:{ref}"),
        repository,
    )
    return revision


def _revision(repository: Path, ref: str) -> str | None:
    output = _run(("git", "ls-remote", "--heads", "origin", ref), repository)
    return output.split()[0] if output else None


class _Lifecycle:
    def __init__(self, states: dict[int, IssueLifecycle]) -> None:
        self._states = states

    def classify(self, issue_number: int) -> IssueLifecycle:
        return self._states.get(issue_number, IssueLifecycle.UNKNOWN)


def test_cleanup_plan_reports_mixed_classes_and_state_audit(
    repository: Path,
) -> None:
    state_revision = _v3_state(repository, 31)
    inbox_revision = _install_ref(
        repository,
        "refs/heads/foundry-opt/inbox/issue-31",
        "inbox",
    )
    design_revision_one = _install_ref(
        repository,
        "refs/heads/foundry-opt/design/issue-31/design-31-1-1",
        "design one",
    )
    design_revision_two = _install_ref(
        repository,
        "refs/heads/foundry-opt/design/issue-31/design-31-1-2",
        "design two",
    )
    spec_revision = _install_ref(
        repository,
        "refs/heads/foundry-opt/spec/issue-8/a2c766f075de",
        "spec",
    )
    orphan_inbox_revision = _install_ref(
        repository,
        "refs/heads/foundry-opt/inbox/issue-178",
        "orphan inbox",
    )
    migration_revision = _install_ref(
        repository,
        "refs/heads/foundry-opt/migration/issue-902/temporary-1",
        "migration",
    )
    service = WorkspaceMigrationService(
        repository,
        _Lifecycle(
            {
                8: IssueLifecycle.CLOSED,
                31: IssueLifecycle.CLOSED,
                178: IssueLifecycle.CLOSED,
                902: IssueLifecycle.CLOSED,
            }
        ),
    )

    plan = service.plan_cleanup_legacy()
    items = {item.ref: item for item in plan.items}

    assert plan.status == "planned"
    assert plan.reason is None
    assert plan.content_hash and len(plan.content_hash) == 64
    assert items["refs/heads/foundry-opt/state/issue-31"].action == "retain"
    assert (
        items["refs/heads/foundry-opt/state/issue-31"].reason
        == "state_requires_convert_audit"
    )
    assert items["refs/heads/foundry-opt/inbox/issue-31"].action == "retain"
    assert (
        items["refs/heads/foundry-opt/inbox/issue-31"].reason
        == "state_requires_current_archive"
    )
    assert (
        items["refs/heads/foundry-opt/design/issue-31/design-31-1-1"].action
        == "delete"
    )
    assert (
        items["refs/heads/foundry-opt/design/issue-31/design-31-1-2"].action
        == "delete"
    )
    assert items["refs/heads/foundry-opt/spec/issue-8/a2c766f075de"].action == "delete"
    assert items["refs/heads/foundry-opt/inbox/issue-178"].action == "delete"
    assert (
        items["refs/heads/foundry-opt/migration/issue-902/temporary-1"].action
        == "delete"
    )
    assert {
        item.ref for item in plan.deletions
    } == {
        "refs/heads/foundry-opt/design/issue-31/design-31-1-1",
        "refs/heads/foundry-opt/design/issue-31/design-31-1-2",
        "refs/heads/foundry-opt/spec/issue-8/a2c766f075de",
        "refs/heads/foundry-opt/inbox/issue-178",
        "refs/heads/foundry-opt/migration/issue-902/temporary-1",
    }
    assert _revision(
        repository,
        "refs/heads/foundry-opt/state/issue-31",
    ) == state_revision
    assert _revision(
        repository,
        "refs/heads/foundry-opt/inbox/issue-31",
    ) == inbox_revision
    assert _revision(
        repository,
        "refs/heads/foundry-opt/design/issue-31/design-31-1-1",
    ) == design_revision_one
    assert _revision(
        repository,
        "refs/heads/foundry-opt/design/issue-31/design-31-1-2",
    ) == design_revision_two
    assert _revision(
        repository,
        "refs/heads/foundry-opt/spec/issue-8/a2c766f075de",
    ) == spec_revision
    assert _revision(
        repository,
        "refs/heads/foundry-opt/inbox/issue-178",
    ) == orphan_inbox_revision
    assert _revision(
        repository,
        "refs/heads/foundry-opt/migration/issue-902/temporary-1",
    ) == migration_revision


def test_cleanup_plan_refuses_open_issue_orphan(
    repository: Path,
) -> None:
    _install_ref(
        repository,
        "refs/heads/foundry-opt/inbox/issue-42",
        "orphan inbox",
    )
    service = WorkspaceMigrationService(
        repository,
        _Lifecycle({42: IssueLifecycle.OPEN}),
    )

    plan = service.plan_cleanup_legacy()

    assert plan.status == "refused"
    assert plan.reason == "issue_not_closed"
    assert plan.deletions == ()
    assert plan.items[0].action == "retain"
    assert plan.items[0].reason == "issue_not_closed"


def test_cleanup_plan_refuses_malformed_paths(
    repository: Path,
) -> None:
    _install_ref(
        repository,
        "refs/heads/foundry-opt/design/issue-31/invalid!/name",
        "malformed",
    )
    service = WorkspaceMigrationService(
        repository,
        _Lifecycle({31: IssueLifecycle.CLOSED}),
    )

    with pytest.raises(WorkspaceMigrationError, match="metadata is invalid"):
        service.plan_cleanup_legacy()


def test_cleanup_apply_succeeds_and_preserves_non_targets(
    repository: Path,
) -> None:
    state_revision = _v3_state(repository, 31)
    retained_state = _v3_state(repository, 99)
    _install_ref(
        repository,
        "refs/heads/foundry-opt/design/issue-31/design-31-1-1",
        "design one",
    )
    _install_ref(
        repository,
        "refs/heads/foundry-opt/spec/issue-8/a2c766f075de",
        "spec",
    )
    _install_ref(
        repository,
        "refs/heads/foundry-opt/inbox/issue-178",
        "orphan inbox",
    )
    service = WorkspaceMigrationService(
        repository,
        _Lifecycle(
            {
                8: IssueLifecycle.CLOSED,
                31: IssueLifecycle.CLOSED,
                99: IssueLifecycle.OPEN,
                178: IssueLifecycle.CLOSED,
            }
        ),
    )

    plan = service.plan_cleanup_legacy()
    result = service.apply_cleanup_legacy(plan)

    assert result.status == "completed"
    assert set(result.deleted_refs) == {
        "refs/heads/foundry-opt/design/issue-31/design-31-1-1",
        "refs/heads/foundry-opt/inbox/issue-178",
        "refs/heads/foundry-opt/spec/issue-8/a2c766f075de",
    }
    assert {
        item.ref for item in result.audit_manifest
    } == set(result.deleted_refs)
    assert result.content_hash == plan.content_hash
    assert _revision(
        repository,
        "refs/heads/foundry-opt/state/issue-31",
    ) == state_revision
    assert _revision(
        repository,
        "refs/heads/foundry-opt/state/issue-99",
    ) == retained_state
    assert _revision(
        repository,
        "refs/heads/foundry-opt/design/issue-31/design-31-1-1",
    ) is None
    assert _revision(
        repository,
        "refs/heads/foundry-opt/spec/issue-8/a2c766f075de",
    ) is None
    assert _revision(
        repository,
        "refs/heads/foundry-opt/inbox/issue-178",
    ) is None


def test_cleanup_apply_rejects_changed_lease(
    repository: Path,
) -> None:
    original = _install_ref(
        repository,
        "refs/heads/foundry-opt/spec/issue-8/a2c766f075de",
        "spec",
    )
    service = WorkspaceMigrationService(
        repository,
        _Lifecycle({8: IssueLifecycle.CLOSED}),
    )
    plan = service.plan_cleanup_legacy()
    changed = _install_ref(
        repository,
        "refs/heads/foundry-opt/spec/issue-8/a2c766f075de",
        "changed spec",
    )

    with pytest.raises(WorkspaceMigrationError, match="changed after planning"):
        service.apply_cleanup_legacy(plan)

    assert _revision(
        repository,
        "refs/heads/foundry-opt/spec/issue-8/a2c766f075de",
    ) == changed
    assert changed != original


def test_cleanup_apply_rejects_partial_atomic_failure(
    repository: Path,
    monkeypatch,
) -> None:
    _install_ref(
        repository,
        "refs/heads/foundry-opt/design/issue-31/design-31-1-1",
        "design one",
    )
    _install_ref(
        repository,
        "refs/heads/foundry-opt/design/issue-31/design-31-1-2",
        "design two",
    )
    service = WorkspaceMigrationService(
        repository,
        _Lifecycle({31: IssueLifecycle.CLOSED}),
    )
    plan = service.plan_cleanup_legacy()

    def fake_delete_refs(root, remote, *, refs, guard_ref=None, guard_revision=None):
        target = sorted(refs)[0]
        subprocess.run(
            ("git", "push", remote.url, f":{target}"),
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        return 1

    monkeypatch.setattr(
        "foundry_opt.orchestration.workspace_migration.atomic_compare_and_swap_delete",
        fake_delete_refs,
    )

    with pytest.raises(WorkspaceMigrationError, match="deletion was not verified"):
        service.apply_cleanup_legacy(plan)

    assert _revision(
        repository,
        "refs/heads/foundry-opt/design/issue-31/design-31-1-1",
    ) is None
    assert _revision(
        repository,
        "refs/heads/foundry-opt/design/issue-31/design-31-1-2",
    ) is not None


def test_cleanup_plan_rejects_tampered_content_hash(
    repository: Path,
) -> None:
    _install_ref(
        repository,
        "refs/heads/foundry-opt/spec/issue-8/a2c766f075de",
        "spec",
    )
    service = WorkspaceMigrationService(
        repository,
        _Lifecycle({8: IssueLifecycle.CLOSED}),
    )
    plan = service.plan_cleanup_legacy()
    payload = plan.to_dict()
    payload["content_hash"] = "0" * 64

    with pytest.raises(WorkspaceMigrationError, match="content hash"):
        WorkspaceLegacyCleanupPlan.from_dict(payload)
