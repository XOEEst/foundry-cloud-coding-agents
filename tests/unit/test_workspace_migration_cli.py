from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

import foundry_opt.cli as cli
from foundry_opt.adapters.commands import CommandExitError
from foundry_opt.cli import app
from foundry_opt.orchestration.workspace_migration import (
    GhIssueLifecycleReader,
    IssueLifecycle,
)
from foundry_opt.preflight.interfaces import CommandResult


runner = CliRunner()


class _Result:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def to_dict(self) -> dict[str, object]:
        return self._payload


class _CleanupPlan:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def to_dict(self) -> dict[str, object]:
        return self._payload

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "_CleanupPlan":
        return cls(payload)


def test_workspace_migration_commands_emit_stable_json(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    calls: list[tuple[object, ...]] = []

    class Service:
        def inventory(self):
            calls.append(("inventory",))
            return _Result({"items": [], "status": "ready"})

        def convert(self, issue_number, *, expected_source_revision):
            calls.append(
                ("convert", issue_number, expected_source_revision)
            )
            return _Result(
                {
                    "audit_revision": "b" * 40,
                    "issue_number": issue_number,
                    "status": "converted",
                }
            )

        def plan_archive(self, issue_number):
            calls.append(("plan", issue_number))
            return _Result(
                {
                    "apply": False,
                    "expected_revisions": {
                        "audit": "b" * 40,
                        "inbox": None,
                        "migration": "b" * 40,
                        "state": "a" * 40,
                    },
                    "issue_number": issue_number,
                    "status": "planned",
                }
            )

        def apply_archive(self, issue_number, *, expected_revisions):
            calls.append(("apply", issue_number, expected_revisions))
            return _Result(
                {
                    "apply": True,
                    "deleted_refs": [
                        "refs/heads/foundry-opt/state/issue-31"
                    ],
                    "issue_number": issue_number,
                    "status": "completed",
                }
            )

    monkeypatch.setattr(
        cli,
        "build_workspace_migration_service",
        lambda: Service(),
    )

    inventory = runner.invoke(app, ["workspace", "migration", "inventory"])
    conversion = runner.invoke(
        app,
        [
            "workspace",
            "migration",
            "convert",
            "--issue",
            "31",
            "--expected-source-revision",
            "a" * 40,
        ],
    )
    plan = runner.invoke(
        app,
        ["workspace", "migration", "archive", "--issue", "31"],
    )
    apply = runner.invoke(
        app,
        [
            "workspace",
            "migration",
            "archive",
            "--issue",
            "31",
            "--apply",
            "--expected-state-revision",
            "a" * 40,
            "--expected-inbox-revision",
            "absent",
            "--expected-audit-revision",
            "b" * 40,
            "--expected-migration-revision",
            "b" * 40,
        ],
    )

    assert inventory.exit_code == 0
    assert json.loads(inventory.stdout) == {
        "items": [],
        "status": "ready",
    }
    assert conversion.exit_code == 0
    assert json.loads(conversion.stdout)["status"] == "converted"
    assert plan.exit_code == 0
    assert json.loads(plan.stdout)["apply"] is False
    assert apply.exit_code == 0
    assert json.loads(apply.stdout)["status"] == "completed"
    assert calls == [
        ("inventory",),
        ("convert", 31, "a" * 40),
        ("plan", 31),
        (
            "apply",
            31,
            {
                "audit": "b" * 40,
                "inbox": None,
                "migration": "b" * 40,
                "state": "a" * 40,
            },
        ),
    ]


def test_workspace_migration_apply_requires_exact_expectations() -> None:
    completed = runner.invoke(
        app,
        [
            "workspace",
            "migration",
            "archive",
            "--issue",
            "31",
            "--apply",
        ],
    )

    assert completed.exit_code != 0
    assert "--expected-state-revision" in completed.output


def test_workspace_cleanup_legacy_commands_emit_stable_json(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    plan_file = tmp_path / "cleanup-plan.json"
    calls: list[tuple[object, ...]] = []

    class Service:
        def plan_cleanup_legacy(self):
            calls.append(("plan-cleanup",))
            return _CleanupPlan(
                {
                    "apply": False,
                    "content_hash": "c" * 64,
                    "deletions": [
                        {
                            "action": "delete",
                            "issue_lifecycle": "closed",
                            "issue_number": 31,
                            "reason": "closed_orphan_legacy_ref",
                            "ref": "refs/heads/foundry-opt/spec/issue-31/abc",
                            "ref_kind": "spec",
                            "revision": "a" * 40,
                        }
                    ],
                    "items": [
                        {
                            "action": "delete",
                            "issue_lifecycle": "closed",
                            "issue_number": 31,
                            "reason": "closed_orphan_legacy_ref",
                            "ref": "refs/heads/foundry-opt/spec/issue-31/abc",
                            "ref_kind": "spec",
                            "revision": "a" * 40,
                        }
                    ],
                    "remote": "origin",
                    "status": "planned",
                }
            )

        def apply_cleanup_legacy(self, plan):
            calls.append(("apply-cleanup", plan.to_dict()))
            return _Result(
                {
                    "apply": True,
                    "audit_manifest": [
                        {
                            "action": "delete",
                            "issue_lifecycle": "closed",
                            "issue_number": 31,
                            "reason": "closed_orphan_legacy_ref",
                            "ref": "refs/heads/foundry-opt/spec/issue-31/abc",
                            "ref_kind": "spec",
                            "revision": "a" * 40,
                        }
                    ],
                    "content_hash": "c" * 64,
                    "deleted_refs": [
                        "refs/heads/foundry-opt/spec/issue-31/abc"
                    ],
                    "remote": "origin",
                    "status": "completed",
                }
            )

    monkeypatch.setattr(
        cli,
        "build_workspace_migration_service",
        lambda: Service(),
    )
    monkeypatch.setattr(cli, "WorkspaceLegacyCleanupPlan", _CleanupPlan)

    dry_run = runner.invoke(
        app,
        [
            "workspace",
            "migration",
            "cleanup-legacy",
            "--plan-file",
            str(plan_file),
        ],
    )
    apply = runner.invoke(
        app,
        [
            "workspace",
            "migration",
            "cleanup-legacy",
            "--apply",
            "--plan-file",
            str(plan_file),
        ],
    )

    assert dry_run.exit_code == 0
    assert json.loads(dry_run.stdout)["status"] == "planned"
    assert json.loads(plan_file.read_text(encoding="utf-8")) == {
        "apply": False,
        "content_hash": "c" * 64,
        "deletions": [
            {
                "action": "delete",
                "issue_lifecycle": "closed",
                "issue_number": 31,
                "reason": "closed_orphan_legacy_ref",
                "ref": "refs/heads/foundry-opt/spec/issue-31/abc",
                "ref_kind": "spec",
                "revision": "a" * 40,
            }
        ],
        "items": [
            {
                "action": "delete",
                "issue_lifecycle": "closed",
                "issue_number": 31,
                "reason": "closed_orphan_legacy_ref",
                "ref": "refs/heads/foundry-opt/spec/issue-31/abc",
                "ref_kind": "spec",
                "revision": "a" * 40,
            }
        ],
        "remote": "origin",
        "status": "planned",
    }
    assert apply.exit_code == 0
    assert json.loads(apply.stdout)["status"] == "completed"
    assert calls == [
        ("plan-cleanup",),
        ("apply-cleanup", json.loads(plan_file.read_text(encoding="utf-8"))),
    ]


def test_workspace_cleanup_legacy_apply_requires_plan_file() -> None:
    completed = runner.invoke(
        app,
        [
            "workspace",
            "migration",
            "cleanup-legacy",
            "--apply",
        ],
    )

    assert completed.exit_code != 0
    assert "--plan-file" in completed.output


def test_github_lifecycle_reader_requests_only_public_issue_state(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, ...]] = []

    class Commands:
        def run(self, arguments, **kwargs):
            calls.append(tuple(arguments))
            return CommandResult(
                exit_code=0,
                stdout='{"number":31,"state":"CLOSED"}',
                stderr="",
            )

    lifecycle = GhIssueLifecycleReader(
        Commands(),
        repository_root=tmp_path,
        repository="octo-org/optimizer",
    ).classify(31)

    assert lifecycle is IssueLifecycle.CLOSED
    assert calls == [
        (
            "gh",
            "issue",
            "view",
            "31",
            "--repo",
            "octo-org/optimizer",
            "--json",
            "number,state",
        )
    ]


def test_github_lifecycle_reader_classifies_failures_as_unknown(
    tmp_path: Path,
) -> None:
    class Commands:
        def run(self, arguments, **kwargs):
            raise CommandExitError(
                arguments,
                exit_code=1,
                stdout="",
                stderr="not found",
            )

    lifecycle = GhIssueLifecycleReader(
        Commands(),
        repository_root=tmp_path,
        repository="octo-org/optimizer",
    ).classify(31)

    assert lifecycle is IssueLifecycle.UNKNOWN
