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
)
from foundry_opt.orchestration.workspace_operations_executor import (
    WorkspaceOperationsResult,
    WorkspaceOperationsStatus,
    WorkspaceResumeRequest,
    WorkspaceVerificationRequest,
)
from foundry_opt.orchestration.workspace_verification import (
    WorkspaceEvidenceLink,
    WorkspaceMetricVerification,
    WorkspaceVerifyResult,
    WorkspaceVerifyStatus,
)
from foundry_opt.orchestration.workspace_operations_executor import (
    WorkspaceOperationsResult,
    WorkspaceOperationsStatus,
    WorkspaceResumeRequest,
    WorkspaceVerificationRequest,
)
from foundry_opt.orchestration.workspace_verification import (
    WorkspaceEvidenceLink,
    WorkspaceMetricVerification,
    WorkspaceVerifyResult,
    WorkspaceVerifyStatus,
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
    assert "assign" in completed.stdout
    assert "intake" in completed.stdout
    assert "experiment" in completed.stdout
    assert "experiment-result" in completed.stdout
    assert "experiments-complete" in completed.stdout
    assert "baseline" in completed.stdout
    assert "baseline-result" in completed.stdout
    assert "operation-complete" in completed.stdout
    assert "verify" in completed.stdout
    assert "operations" in completed.stdout

    operations = runner.invoke(app, ["workspace", "operations", "--help"])

    assert operations.exit_code == 0
    assert "execute" in operations.stdout
    assert "reconcile" in operations.stdout


def test_workspace_assign_uses_secret_without_emitting_it(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "COPILOT_ASSIGNMENT_TOKEN",
        "assignment-token",
    )
    monkeypatch.setenv("GH_TOKEN", "actions-token")

    class Result:
        def to_dict(self):
            return {
                "assigned": True,
                "issue_number": 31,
                "next_action": "run_candidate_experiments",
                "status": "assigned",
                "workspace_pull_request_number": 104,
            }

    class Service:
        def assign_copilot(
            self,
            *,
            repository_root,
            issue_number,
            assignment_token,
        ):
            assert repository_root == tmp_path
            assert issue_number == 31
            assert assignment_token == "assignment-token"
            assert "COPILOT_ASSIGNMENT_TOKEN" not in __import__("os").environ
            assert __import__("os").environ["GH_TOKEN"] == "actions-token"
            return Result()

    monkeypatch.setattr(cli, "build_workspace_service", lambda: Service())

    completed = runner.invoke(
        app,
        ["workspace", "assign", "--issue", "31", "--json"],
    )

    assert completed.exit_code == 0
    assert json.loads(completed.stdout)["workspace_pull_request_number"] == 104
    assert "assignment-token" not in completed.stdout


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


def test_workspace_experiment_executes_untrusted_candidate_manifest(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    manifest = tmp_path / "candidate.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "issue_number": 31,
                "target": "support-agent",
                "base_commit": "a" * 40,
                "candidate": {
                    "candidate_id": "candidate-1",
                    "mutation_class": "system_instructions",
                    "patch_base64": "cGF0Y2g=",
                    "summary": "Improve quality.",
                },
            }
        ),
        encoding="utf-8",
    )

    class Result:
        candidate_id = "candidate-1"
        status = "completed"
        next_action = "experiments_complete"

        def to_dict(self):
            return {
                "candidate_id": self.candidate_id,
                "status": self.status,
                "next_action": self.next_action,
            }

    class Service:
        def execute_experiment(self, payload, *, repository_root):
            assert payload["candidate"]["candidate_id"] == "candidate-1"
            assert (
                payload["candidate"]["mutation_class"]
                == "system_instructions"
            )
            assert repository_root == tmp_path
            return Result()

    monkeypatch.setattr(cli, "build_workspace_service", lambda: Service())

    completed = runner.invoke(
        app,
        [
            "workspace",
            "experiment",
            "--issue",
            "31",
            "--candidate-manifest",
            str(manifest),
            "--json",
        ],
    )

    assert completed.exit_code == 0
    assert json.loads(completed.stdout)["status"] == "completed"


def test_workspace_experiment_result_uses_trusted_context(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    result_path = tmp_path / "experiment-result.json"
    result_path.write_text(
        json.dumps({"schema_version": 1, "issue_number": 31}),
        encoding="utf-8",
    )

    class Result:
        candidate_id = "candidate-1"
        status = "completed"

        def to_dict(self):
            return {
                "candidate_id": self.candidate_id,
                "status": self.status,
            }

    class Service:
        def ingest_experiment_result(
            self,
            payload,
            context,
            *,
            repository_root,
        ):
            assert payload["issue_number"] == 31
            assert context.delivery_id == "delivery-123"
            assert context.repository == "octo-org/optimizer"
            assert context.repository_id == 123
            assert repository_root == tmp_path
            return Result()

    monkeypatch.setattr(cli, "build_workspace_service", lambda: Service())

    completed = runner.invoke(
        app,
        [
            "workspace",
            "experiment-result",
            "--result",
            str(result_path),
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
    assert json.loads(completed.stdout)["candidate_id"] == "candidate-1"


def test_workspace_baseline_executes_from_trusted_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    class Result:
        status = "completed"
        next_action = "design_candidates"

        def to_dict(self):
            return {
                "status": self.status,
                "next_action": self.next_action,
            }

    class Service:
        def execute_baseline(self, *, repository_root, issue_number):
            assert repository_root == tmp_path
            assert issue_number == 31
            return Result()

    monkeypatch.setattr(cli, "build_workspace_service", lambda: Service())

    completed = runner.invoke(
        app,
        ["workspace", "baseline", "--issue", "31", "--json"],
    )

    assert completed.exit_code == 0
    assert json.loads(completed.stdout)["status"] == "completed"


def test_workspace_baseline_result_uses_trusted_context(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    result_path = tmp_path / "baseline-result.json"
    result_path.write_text(
        json.dumps({"schema_version": 1, "issue_number": 31}),
        encoding="utf-8",
    )

    class Result:
        status = "completed"

        def to_dict(self):
            return {"status": self.status}

    class Service:
        def ingest_baseline_result(
            self,
            payload,
            context,
            *,
            repository_root,
        ):
            assert payload["issue_number"] == 31
            assert context.delivery_id == "delivery-123"
            assert context.repository == "octo-org/optimizer"
            assert context.repository_id == 123
            assert repository_root == tmp_path
            return Result()

    monkeypatch.setattr(cli, "build_workspace_service", lambda: Service())

    completed = runner.invoke(
        app,
        [
            "workspace",
            "baseline-result",
            "--result",
            str(result_path),
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
    assert json.loads(completed.stdout)["status"] == "completed"


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


def test_workspace_verify_emits_stable_json(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    class Service:
        def verify(self, request):
            assert request.repository_root == tmp_path
            assert request.issue_number == 31
            assert request.candidate_id == "candidate-2"
            assert request.workspace_pull_request_number == 104
            assert request.head_sha == "a" * 40
            return WorkspaceVerifyResult(
                issue_number=31,
                candidate_id="candidate-2",
                status=WorkspaceVerifyStatus.VERIFIED,
                repository="octo-org/optimizer",
                target="support-agent",
                phase=WorkspacePhase.AWAITING_SELECTION,
                workspace_pull_request_number=104,
                head_sha="a" * 40,
                head_tree="b" * 40,
                expected_tree="b" * 40,
                patch_sha256="1" * 64,
                bundle_sha256="2" * 64,
                evidence=WorkspaceEvidenceLink(
                    path="evidence/candidates.json",
                    url=(
                        "https://github.com/octo-org/optimizer/blob/"
                        + "c" * 40
                        + "/evidence/candidates.json"
                    ),
                    state_revision="c" * 40,
                    sha256="3" * 64,
                ),
                metric_table=(
                    WorkspaceMetricVerification(
                        name="quality",
                        value=0.9,
                        threshold=0.7,
                        materiality=0.1,
                        hard_guardrail=False,
                        guardrail_status=None,
                    ),
                    WorkspaceMetricVerification(
                        name="safety",
                        value=1.0,
                        threshold=1.0,
                        materiality=0.0,
                        hard_guardrail=True,
                        guardrail_status="pass",
                    ),
                ),
                guardrails={"safety": "pass"},
                summary_markdown="## Trusted workspace verification",
            )

    monkeypatch.setattr(
        cli,
        "build_workspace_verification_service",
        lambda: Service(),
    )

    completed = runner.invoke(
        app,
        [
            "workspace",
            "verify",
            "--issue",
            "31",
            "--candidate",
            "candidate-2",
            "--pull-request",
            "104",
            "--head-sha",
            "a" * 40,
            "--json",
        ],
    )

    assert completed.exit_code == 0
    assert json.loads(completed.stdout) == {
        "bundle_sha256": "2" * 64,
        "candidate_id": "candidate-2",
        "evidence": {
            "path": "evidence/candidates.json",
            "sha256": "3" * 64,
            "state_revision": "c" * 40,
            "url": (
                "https://github.com/octo-org/optimizer/blob/"
                + "c" * 40
                + "/evidence/candidates.json"
            ),
        },
        "expected_tree": "b" * 40,
        "guardrails": {"safety": "pass"},
        "head_sha": "a" * 40,
        "head_tree": "b" * 40,
        "issue_number": 31,
        "metric_table": [
            {
                "guardrail_status": None,
                "hard_guardrail": False,
                "materiality": 0.1,
                "name": "quality",
                "threshold": 0.7,
                "value": 0.9,
            },
            {
                "guardrail_status": "pass",
                "hard_guardrail": True,
                "materiality": 0.0,
                "name": "safety",
                "threshold": 1.0,
                "value": 1.0,
            },
        ],
        "patch_sha256": "1" * 64,
        "phase": "awaiting_selection",
        "repository": "octo-org/optimizer",
        "status": "verified",
        "summary_markdown": "## Trusted workspace verification",
        "target": "support-agent",
        "workspace_pull_request_number": 104,
    }


def test_workspace_operations_execute_emits_stable_json(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    class Service:
        def execute(self, request):
            assert request.repository_root == tmp_path
            assert request.issue_number == 31
            assert request.context.event_name == "workflow_dispatch"
            assert request.context.repository == "octo-org/optimizer"
            assert request.context.repository_id == 123
            return WorkspaceOperationsResult(
                issue_number=31,
                status=WorkspaceOperationsStatus.CANDIDATE_RECORDED,
                recorded=True,
                phase=WorkspacePhase.EVALUATING,
                operation_id="2" * 64,
            )

    monkeypatch.setattr(
        cli,
        "build_workspace_operations_service",
        lambda: Service(),
    )

    completed = runner.invoke(
        app,
        [
            "workspace",
            "operations",
            "execute",
            "--issue",
            "31",
            "--event-name",
            "workflow_dispatch",
            "--repository",
            "octo-org/optimizer",
            "--repository-id",
            "123",
            "--json",
        ],
    )

    assert completed.exit_code == 0
    assert json.loads(completed.stdout) == {
        "deployment_run_id": None,
        "deployment_run_url": None,
        "finalization": None,
        "issue_number": 31,
        "operation_id": "2" * 64,
        "phase": "evaluating",
        "recorded": True,
        "resume": None,
        "status": "candidate_recorded",
        "verification": None,
        "workspace_pull_request_number": None,
    }


def test_workspace_operations_execute_emits_baseline_resume_payload(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    class Service:
        def execute(self, request):
            return WorkspaceOperationsResult(
                issue_number=31,
                status=WorkspaceOperationsStatus.BASELINE_RECORDED,
                recorded=True,
                phase=WorkspacePhase.EVALUATING,
                operation_id="2" * 64,
                workspace_pull_request_number=104,
                resume=WorkspaceResumeRequest(
                    workspace_pull_request_number=104,
                    comment_marker="<!-- foundry-opt:workspace-resume -->",
                    comment_body=(
                        "<!-- foundry-opt:workspace-resume -->\n"
                        "@copilot continue this same workspace pull request."
                    ),
                ),
            )

    monkeypatch.setattr(
        cli,
        "build_workspace_operations_service",
        lambda: Service(),
    )

    completed = runner.invoke(
        app,
        [
            "workspace",
            "operations",
            "execute",
            "--issue",
            "31",
            "--event-name",
            "workflow_dispatch",
            "--repository",
            "octo-org/optimizer",
            "--repository-id",
            "123",
            "--json",
        ],
    )

    assert completed.exit_code == 0
    payload = json.loads(completed.stdout)
    assert payload["status"] == "baseline_recorded"
    assert payload["workspace_pull_request_number"] == 104
    assert payload["resume"] == {
        "comment_body": (
            "<!-- foundry-opt:workspace-resume -->\n"
            "@copilot continue this same workspace pull request."
        ),
        "comment_marker": "<!-- foundry-opt:workspace-resume -->",
        "workspace_pull_request_number": 104,
    }


def test_workspace_operations_reconcile_uses_trusted_artifact_context(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    operation = tmp_path / "deployment-result.json"
    operation.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "kind": "deployment_result",
                "status": "completed",
                "issue_number": 31,
                "workspace_pull_request_number": 104,
                "operation_id": "deployment-123",
                "candidate_id": "candidate-2",
                "patch_sha256": "1" * 64,
                "bundle_sha256": "2" * 64,
                "evidence_sha256": "3" * 64,
                "spec_sha256": "4" * 64,
                "merge_commit": "a" * 40,
                "tree_sha": "b" * 40,
                "artifact_name": "foundry-optimization-deployment-result",
                "run_id": 991,
                "run_url": "https://github.com/octo-org/optimizer/actions/runs/991",
                "deployment_version": 13,
                "portal_url": "https://ai.azure.com/projects/demo/agents/demo/versions/13",
                "lineage_sha256": "5" * 64,
                "repository": {
                    "full_name": "octo-org/optimizer",
                    "id": 123,
                },
            }
        ),
        encoding="utf-8",
    )

    class Service:
        def reconcile(self, request):
            assert request.repository_root == tmp_path
            assert request.issue_number == 31
            assert request.payload["operation_id"] == "deployment-123"
            assert request.context.repository == "octo-org/optimizer"
            assert request.context.repository_id == 123
            assert request.context.run_id == 991
            assert (
                request.context.artifact_name
                == "foundry-optimization-deployment-result"
            )
            return WorkspaceOperationsResult(
                issue_number=31,
                status=WorkspaceOperationsStatus.COMPLETED,
                recorded=True,
                phase=WorkspacePhase.COMPLETED,
                operation_id="retention-123",
                workspace_pull_request_number=104,
            )

    monkeypatch.setattr(
        cli,
        "build_workspace_operations_service",
        lambda: Service(),
    )

    completed = runner.invoke(
        app,
        [
            "workspace",
            "operations",
            "reconcile",
            "--issue",
            "31",
            "--result",
            str(operation),
            "--repository",
            "octo-org/optimizer",
            "--repository-id",
            "123",
            "--run-id",
            "991",
            "--json",
        ],
    )

    assert completed.exit_code == 0
    assert json.loads(completed.stdout)["status"] == "completed"


def test_workspace_operations_execute_emits_verification_payload(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    class Service:
        def execute(self, request):
            return WorkspaceOperationsResult(
                issue_number=31,
                status=WorkspaceOperationsStatus.CANDIDATE_RECORDED,
                recorded=False,
                phase=WorkspacePhase.AWAITING_SELECTION,
                operation_id="2" * 64,
                workspace_pull_request_number=104,
                verification=WorkspaceVerificationRequest(
                    issue_number=31,
                    candidate_id="candidate-2",
                    workspace_pull_request_number=104,
                ),
            )

    monkeypatch.setattr(
        cli,
        "build_workspace_operations_service",
        lambda: Service(),
    )

    completed = runner.invoke(
        app,
        [
            "workspace",
            "operations",
            "execute",
            "--issue",
            "31",
            "--event-name",
            "workflow_dispatch",
            "--repository",
            "octo-org/optimizer",
            "--repository-id",
            "123",
            "--json",
        ],
    )

    assert completed.exit_code == 0
    assert json.loads(completed.stdout)["verification"] == {
        "candidate_id": "candidate-2",
        "check_name": "exact-candidate",
        "issue_number": 31,
        "workspace_pull_request_number": 104,
    }


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
