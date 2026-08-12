from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

import foundry_opt.cli as cli
from foundry_opt.cli import app
from foundry_opt.orchestration import (
    WorkspaceAdvanceRequest,
    WorkspaceIssueStatusProjectionIntent,
    WorkspacePhase,
    WorkspacePullRequest,
    WorkspaceResult,
    WorkspaceTrigger,
)


runner = CliRunner()


def _result() -> WorkspaceResult:
    return WorkspaceResult(
        phase=WorkspacePhase.SPECIFICATION,
        workspace_pull_request=WorkspacePullRequest(
            number=104,
            issue_number=31,
            branch="foundry-opt/workspace/issue-31",
            title="[Optimize] #31 workspace - draft, not yet selectable",
            draft=True,
            reuse_existing=True,
            base_commit="a" * 40,
        ),
        planned_effect_kinds=("workspace_pr_sync",),
        recorded=True,
        issue_status_projection_intent=(
            WorkspaceIssueStatusProjectionIntent(
                issue_number=31,
                phase=WorkspacePhase.SPECIFICATION,
                workspace_pull_request_number=104,
            )
        ),
    )


def test_workspace_advance_emits_stable_json(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    class Service:
        def advance(self, request: WorkspaceAdvanceRequest):
            assert request.repository_root == tmp_path
            assert request.issue_number == 31
            return _result()

    monkeypatch.setattr(
        cli,
        "build_workspace_service",
        lambda: Service(),
    )

    completed = runner.invoke(
        app,
        ["workspace", "advance", "--issue", "31", "--json"],
    )

    assert completed.exit_code == 0
    assert json.loads(completed.stdout) == _result().to_dict()
    assert json.loads(completed.stdout)["issue_status_projection_intent"] == {
        "issue_number": 31,
        "kind": "workspace_issue_status",
        "phase": "specification",
        "status": "workspace_ready",
        "workspace_pull_request_number": 104,
    }


def test_workspace_advance_rejects_direct_lifecycle_trigger(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    class Service:
        def advance(self, request: WorkspaceAdvanceRequest):
            raise AssertionError("unsafe lifecycle trigger reached service")

    monkeypatch.setattr(cli, "build_workspace_service", lambda: Service())

    completed = runner.invoke(
        app,
        [
            "workspace",
            "advance",
            "--issue",
            "31",
            "--trigger",
            "deployment_completed",
            "--json",
        ],
    )

    assert completed.exit_code == 2


def test_workspace_help_exposes_production_lifecycle_commands() -> None:
    completed = runner.invoke(app, ["workspace", "--help"])

    assert completed.exit_code == 0
    assert "advance" in completed.stdout
    assert "intake" in completed.stdout
    assert "experiments-complete" in completed.stdout
    assert "operation-complete" in completed.stdout


def test_workspace_experiments_complete_ingests_manifest(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    manifest = tmp_path / "candidates.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "issue_number": 31,
                "target": "support-agent",
            }
        ),
        encoding="utf-8",
    )

    class Service:
        def complete_experiments(self, payload, *, repository_root):
            assert payload["target"] == "support-agent"
            assert repository_root == tmp_path
            return _result()

    monkeypatch.setattr(cli, "build_workspace_service", lambda: Service())

    completed = runner.invoke(
        app,
        [
            "workspace",
            "experiments-complete",
            "--issue",
            "31",
            "--manifest",
            str(manifest),
            "--json",
        ],
    )

    assert completed.exit_code == 0
    assert json.loads(completed.stdout) == _result().to_dict()


def test_workspace_operation_complete_uses_trusted_context(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    operation = tmp_path / "operation.json"
    operation.write_text(
        json.dumps({"schema_version": 1, "operation_id": "deploy-1"}),
        encoding="utf-8",
    )

    class IntakeResult:
        def to_dict(self):
            return {"event": {"operation_id": "deploy-1"}}

    class Service:
        def ingest_operation(
            self,
            payload,
            context,
            *,
            repository_root,
        ):
            assert payload["operation_id"] == "deploy-1"
            assert context.repository == "octo-org/optimizer"
            assert context.repository_id == 123
            assert repository_root == tmp_path
            return IntakeResult()

    monkeypatch.setattr(cli, "build_workspace_service", lambda: Service())

    completed = runner.invoke(
        app,
        [
            "workspace",
            "operation-complete",
            "--result",
            str(operation),
            "--delivery-id",
            "delivery-123",
            "--repository",
            "octo-org/optimizer",
            "--repository-id",
            "123",
            "--json",
        ],
    )

    assert completed.exit_code == 0
    assert json.loads(completed.stdout)["event"]["operation_id"] == "deploy-1"


def test_workspace_intake_normalizes_trusted_event_through_service(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps(
            {
                "action": "opened",
                "issue": {
                    "number": 31,
                    "title": "[Optimize] Improve policy coverage",
                    "body": "Improve policy coverage.",
                },
                "repository": {
                    "full_name": "octo-org/optimizer",
                    "id": 123,
                },
            }
        ),
        encoding="utf-8",
    )

    class IntakeResult:
        exit_code = 0

        def to_dict(self):
            return {
                "event": {"delivery_id": "delivery-123"},
                "workspace": _result().to_dict(),
            }

    class Service:
        def ingest(
            self,
            payload,
            context,
            *,
            base_commit,
            repository_root,
        ):
            assert payload["issue"]["number"] == 31
            assert context.repository == "octo-org/optimizer"
            assert base_commit == "a" * 40
            assert repository_root == tmp_path
            return IntakeResult()

    monkeypatch.setattr(
        cli,
        "build_workspace_service",
        lambda: Service(),
    )

    completed = runner.invoke(
        app,
        [
            "workspace",
            "intake",
            "--event-path",
            str(event_path),
            "--event-name",
            "issues",
            "--delivery-id",
            "delivery-123",
            "--repository",
            "octo-org/optimizer",
            "--repository-id",
            "123",
            "--base-commit",
            "a" * 40,
            "--json",
        ],
    )

    assert completed.exit_code == 0
    assert json.loads(completed.stdout)["event"]["delivery_id"] == (
        "delivery-123"
    )
