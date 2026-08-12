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
    _run(("git", "init", "--bare", str(origin)), tmp_path)
    _run(("git", "init", "-b", "main", str(repository)), tmp_path)
    _run(("git", "config", "user.name", "Migration Test"), repository)
    _run(
        ("git", "config", "user.email", "migration@example.invalid"),
        repository,
    )
    (repository / "README.md").write_text("migration tests\n")
    _run(("git", "add", "."), repository)
    _run(("git", "commit", "-m", "baseline"), repository)
    _run(("git", "remote", "add", "origin", str(origin)), repository)
    _run(("git", "push", "-u", "origin", "main"), repository)
    return repository


@pytest.fixture
def repository() -> Path:
    root = Path.cwd() / ".migration-test" / uuid4().hex[:8]
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


def test_inventory_reports_all_closed_legacy_refs_without_private_state(
    repository: Path,
) -> None:
    state_revision = _v3_state(repository, 31)
    inbox_revision = _install_ref(
        repository,
        "refs/heads/foundry-opt/inbox/issue-31",
        "inbox",
    )
    service = WorkspaceMigrationService(
        repository,
        _Lifecycle({31: IssueLifecycle.CLOSED}),
    )

    payload = service.inventory().to_dict()

    assert payload == {
        "all_closed": True,
        "counts": {"closed": 1, "open": 0, "unknown": 0},
        "items": [
            {
                "inbox_ref": "refs/heads/foundry-opt/inbox/issue-31",
                "inbox_revision": inbox_revision,
                "issue_lifecycle": "closed",
                "issue_number": 31,
                "state_ref": "refs/heads/foundry-opt/state/issue-31",
                "state_revision": state_revision,
                "state_schema_version": 3,
            }
        ],
        "status": "ready",
    }
    assert "snapshot" not in str(payload)
    assert "journal" not in str(payload)


@pytest.mark.parametrize(
    "lifecycle",
    [IssueLifecycle.OPEN, IssueLifecycle.UNKNOWN],
)
def test_open_or_unknown_issue_refuses_conversion_and_archival(
    repository: Path,
    lifecycle: IssueLifecycle,
) -> None:
    source_revision = _v3_state(repository, 31)
    service = WorkspaceMigrationService(
        repository,
        _Lifecycle({31: lifecycle}),
    )

    with pytest.raises(WorkspaceMigrationError, match="not closed"):
        service.convert(
            31,
            expected_source_revision=source_revision,
        )
    plan = service.plan_archive(31).to_dict()

    assert plan["status"] == "refused"
    assert plan["issue_lifecycle"] == lifecycle.value
    assert _revision(
        repository,
        "refs/heads/foundry-opt/state/issue-31",
    ) == source_revision
    assert _revision(
        repository,
        "refs/heads/foundry-opt/audit/issue-31",
    ) is None


def test_conversion_is_idempotent_and_preserves_v3_source(
    repository: Path,
) -> None:
    source_revision = _v3_state(repository, 31)
    service = WorkspaceMigrationService(
        repository,
        _Lifecycle({31: IssueLifecycle.CLOSED}),
    )

    first = service.convert(
        31,
        expected_source_revision=source_revision,
    ).to_dict()
    second = service.convert(
        31,
        expected_source_revision=source_revision,
    ).to_dict()

    assert first["status"] == "converted"
    assert second["status"] == "already_converted"
    assert first["audit_revision"] == second["audit_revision"]
    assert _revision(
        repository,
        "refs/heads/foundry-opt/state/issue-31",
    ) == source_revision


def test_archive_dry_run_does_not_mutate_refs(
    repository: Path,
) -> None:
    state_revision = _v3_state(repository, 31)
    inbox_revision = _install_ref(
        repository,
        "refs/heads/foundry-opt/inbox/issue-31",
        "inbox",
    )
    service = WorkspaceMigrationService(
        repository,
        _Lifecycle({31: IssueLifecycle.CLOSED}),
    )
    conversion = service.convert(
        31,
        expected_source_revision=state_revision,
    )

    plan = service.plan_archive(31).to_dict()

    assert plan["status"] == "planned"
    assert plan["apply"] is False
    assert plan["expected_revisions"] == {
        "audit": conversion.audit_revision,
        "inbox": inbox_revision,
        "state": state_revision,
    }
    assert _revision(
        repository,
        "refs/heads/foundry-opt/state/issue-31",
    ) == state_revision
    assert _revision(
        repository,
        "refs/heads/foundry-opt/inbox/issue-31",
    ) == inbox_revision


def test_archive_refuses_refs_changed_after_planning(
    repository: Path,
) -> None:
    state_revision = _v3_state(repository, 31)
    _install_ref(
        repository,
        "refs/heads/foundry-opt/inbox/issue-31",
        "inbox",
    )
    service = WorkspaceMigrationService(
        repository,
        _Lifecycle({31: IssueLifecycle.CLOSED}),
    )
    service.convert(31, expected_source_revision=state_revision)
    plan = service.plan_archive(31)
    changed_inbox = _install_ref(
        repository,
        "refs/heads/foundry-opt/inbox/issue-31",
        "changed inbox",
    )

    with pytest.raises(WorkspaceMigrationError, match="changed"):
        service.apply_archive(
            31,
            expected_revisions=plan.expected_revisions,
        )

    assert _revision(
        repository,
        "refs/heads/foundry-opt/state/issue-31",
    ) == state_revision
    assert _revision(
        repository,
        "refs/heads/foundry-opt/inbox/issue-31",
    ) == changed_inbox


def test_archive_deletes_only_explicit_issue_refs_and_retries_safely(
    repository: Path,
) -> None:
    state_revision = _v3_state(repository, 31)
    inbox_revision = _install_ref(
        repository,
        "refs/heads/foundry-opt/inbox/issue-31",
        "inbox",
    )
    other_state = _v3_state(repository, 99)
    other_ref = _install_ref(
        repository,
        "refs/heads/foundry-opt/keep/me",
        "keep",
    )
    service = WorkspaceMigrationService(
        repository,
        _Lifecycle(
            {
                31: IssueLifecycle.CLOSED,
                99: IssueLifecycle.CLOSED,
            }
        ),
    )
    conversion = service.convert(
        31,
        expected_source_revision=state_revision,
    )
    expectations = {
        "audit": conversion.audit_revision,
        "inbox": inbox_revision,
        "state": state_revision,
    }

    first = service.apply_archive(
        31,
        expected_revisions=expectations,
    ).to_dict()
    second = service.apply_archive(
        31,
        expected_revisions=expectations,
    ).to_dict()

    assert first["status"] == "completed"
    assert first["deleted_refs"] == [
        "refs/heads/foundry-opt/inbox/issue-31",
        "refs/heads/foundry-opt/state/issue-31",
    ]
    assert second["status"] == "already_completed"
    assert second["deleted_refs"] == []
    assert _revision(
        repository,
        "refs/heads/foundry-opt/state/issue-99",
    ) == other_state
    assert _revision(
        repository,
        "refs/heads/foundry-opt/keep/me",
    ) == other_ref
    assert _revision(
        repository,
        "refs/heads/foundry-opt/audit/issue-31",
    ) == conversion.audit_revision
