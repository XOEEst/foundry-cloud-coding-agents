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
