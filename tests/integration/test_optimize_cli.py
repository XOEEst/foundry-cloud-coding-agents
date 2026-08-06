from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import foundry_opt.cli as cli
from foundry_opt.optimization import (
    OptimizeCommandRequest,
    OptimizeCommandResult,
    OptimizeCommandStatus,
    OptimizePhase,
)
from foundry_opt.orchestration.steward import (
    StewardAdvanceResult,
    StewardAdvanceStatus,
)
from foundry_opt.orchestration import (
    CampaignPhase,
    CampaignState,
    StateRefSnapshot,
)
from foundry_opt.orchestration.candidate_workers import (
    CandidateDesignSubmissionResult,
    CandidateDesignSubmissionStatus,
)


class Service:
    def __init__(
        self,
        result: OptimizeCommandResult | None = None,
    ) -> None:
        self.requests: list[OptimizeCommandRequest] = []
        self.result = result

    def execute(
        self,
        request: OptimizeCommandRequest,
    ) -> OptimizeCommandResult:
        self.requests.append(request)
        return self.result or OptimizeCommandResult(
            status=OptimizeCommandStatus.COMPLETE,
            phase=request.phase,
            summary=f"Completed {request.phase.value}",
            issue_number=request.issue_number,
            details={"candidate_id": request.candidate_id},
        )


def test_optimize_defaults_to_state_aware_phase(
    monkeypatch,
    tmp_path: Path,
) -> None:
    service = Service()
    monkeypatch.setattr(
        cli,
        "build_optimization_command_service",
        lambda: service,
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli.app,
        ["optimize", "--issue", "42"],
    )

    assert result.exit_code == 0
    assert "Completed auto" in result.stdout
    assert service.requests == [
        OptimizeCommandRequest(
            repository_root=tmp_path,
            issue_number=42,
            phase=OptimizePhase.AUTO,
        )
    ]


def test_optimize_explicit_phases_route_through_one_contract(
    monkeypatch,
    tmp_path: Path,
) -> None:
    service = Service()
    monkeypatch.setattr(
        cli,
        "build_optimization_command_service",
        lambda: service,
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    for command, phase in (
        ("spec", OptimizePhase.SPEC),
        ("run", OptimizePhase.RUN),
        ("reconcile", OptimizePhase.RECONCILE),
    ):
        result = runner.invoke(
            cli.app,
            ["optimize", command, "--issue", "42"],
        )
        assert result.exit_code == 0
        assert service.requests[-1].phase is phase

    result = runner.invoke(
        cli.app,
        [
            "optimize",
            "apply",
            "--issue",
            "42",
            "--candidate",
            "candidate-a",
            "--verify-only",
        ],
    )

    assert result.exit_code == 0
    assert service.requests[-1].phase is OptimizePhase.APPLY
    assert service.requests[-1].candidate_id == "candidate-a"
    assert service.requests[-1].verify_only is True


def test_optimize_apply_requires_candidate() -> None:
    result = CliRunner().invoke(
        cli.app,
        ["optimize", "apply", "--issue", "42"],
    )

    assert result.exit_code == 2
    assert "--candidate" in result.stderr


def test_optimize_json_output_is_stable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    service = Service()
    monkeypatch.setattr(
        cli,
        "build_optimization_command_service",
        lambda: service,
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli.app,
        ["optimize", "--issue", "42", "--json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "details": {"candidate_id": None},
        "issue_number": 42,
        "next_action": None,
        "phase": "auto",
        "status": "complete",
        "summary": "Completed auto",
    }


def test_optimize_blocked_result_returns_one(
    monkeypatch,
    tmp_path: Path,
) -> None:
    service = Service(
        OptimizeCommandResult(
            status=OptimizeCommandStatus.BLOCKED,
            phase=OptimizePhase.SPEC,
            summary="Specification approval is required.",
            issue_number=42,
            details={"pull_request": 17},
        )
    )
    monkeypatch.setattr(
        cli,
        "build_optimization_command_service",
        lambda: service,
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli.app,
        ["optimize", "spec", "--issue", "42"],
    )

    assert result.exit_code == 1
    assert "Specification approval is required." in result.stdout


def test_optimize_candidate_request_routes_to_request_phase(
    monkeypatch,
    tmp_path: Path,
) -> None:
    service = Service(
        OptimizeCommandResult(
            status=OptimizeCommandStatus.AWAITING_AGENT,
            phase=OptimizePhase.CANDIDATE_REQUEST,
            summary="Candidate worktree is ready.",
            issue_number=42,
            details={"worktree": "/tmp/wt", "candidate_id": "candidate-1"},
            next_action="Edit the worktree and submit an idea file.",
        )
    )
    monkeypatch.setattr(
        cli,
        "build_optimization_command_service",
        lambda: service,
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli.app,
        ["optimize", "candidate", "request", "--issue", "42"],
    )

    assert result.exit_code == 0
    assert service.requests[-1] == OptimizeCommandRequest(
        repository_root=tmp_path,
        issue_number=42,
        phase=OptimizePhase.CANDIDATE_REQUEST,
    )
    assert "Edit the worktree and submit an idea file." in result.stdout


def test_optimize_candidate_submit_routes_with_candidate_and_idea(
    monkeypatch,
    tmp_path: Path,
) -> None:
    service = Service()
    monkeypatch.setattr(
        cli,
        "build_optimization_command_service",
        lambda: service,
    )
    monkeypatch.chdir(tmp_path)
    idea = tmp_path / "idea.json"
    idea.write_text("{}", encoding="utf-8")

    result = CliRunner().invoke(
        cli.app,
        [
            "optimize",
            "candidate",
            "submit",
            "--issue",
            "42",
            "--candidate",
            "candidate-1",
            "--idea-file",
            str(idea),
        ],
    )

    assert result.exit_code == 0
    assert service.requests[-1] == OptimizeCommandRequest(
        repository_root=tmp_path,
        issue_number=42,
        phase=OptimizePhase.CANDIDATE_SUBMIT,
        candidate_id="candidate-1",
        idea_file=idea,
    )


def test_optimize_candidate_submit_requires_candidate_and_idea() -> None:
    runner = CliRunner()

    missing_idea = runner.invoke(
        cli.app,
        [
            "optimize",
            "candidate",
            "submit",
            "--issue",
            "42",
            "--candidate",
            "candidate-1",
        ],
    )
    assert missing_idea.exit_code == 2
    assert "--idea-file" in missing_idea.stderr

    missing_candidate = runner.invoke(
        cli.app,
        [
            "optimize",
            "candidate",
            "submit",
            "--issue",
            "42",
            "--idea-file",
            "idea.json",
        ],
    )
    assert missing_candidate.exit_code == 2
    assert "--candidate" in missing_candidate.stderr


def test_optimize_candidate_request_json_is_stable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    service = Service(
        OptimizeCommandResult(
            status=OptimizeCommandStatus.AWAITING_AGENT,
            phase=OptimizePhase.CANDIDATE_REQUEST,
            summary="Candidate worktree is ready.",
            issue_number=42,
            details={"candidate_id": "candidate-1"},
            next_action="Submit an idea file for candidate-1.",
        )
    )
    monkeypatch.setattr(
        cli,
        "build_optimization_command_service",
        lambda: service,
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli.app,
        ["optimize", "candidate", "request", "--issue", "42", "--json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "details": {"candidate_id": "candidate-1"},
        "issue_number": 42,
        "next_action": "Submit an idea file for candidate-1.",
        "phase": "candidate-request",
        "status": "awaiting_agent",
        "summary": "Candidate worktree is ready.",
    }


class StewardService:
    def __init__(self, result: StewardAdvanceResult) -> None:
        self.result = result
        self.requests = []

    def advance(self, request):
        self.requests.append(request)
        return self.result


def test_steward_advance_routes_issue_and_renders_json(
    monkeypatch,
    tmp_path: Path,
) -> None:
    service = StewardService(
        StewardAdvanceResult(
            status=StewardAdvanceStatus.WAITING,
            issue_number=42,
            summary="No new campaign events.",
            phase="candidates",
            disposition="wait",
            revision="a" * 40,
        )
    )
    monkeypatch.setattr(cli, "build_steward_advance_service", lambda: service)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli.app,
        ["steward", "advance", "--issue", "42", "--json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "code": None,
        "disposition": "wait",
        "issue_number": 42,
        "phase": "candidates",
        "revision": "a" * 40,
        "status": "waiting",
        "summary": "No new campaign events.",
    }
    assert service.requests[0].repository_root == tmp_path
    assert service.requests[0].issue_number == 42


def test_steward_advance_strict_failure_returns_one(
    monkeypatch,
    tmp_path: Path,
) -> None:
    service = StewardService(
        StewardAdvanceResult(
            status=StewardAdvanceStatus.CONFLICT,
            issue_number=42,
            summary="The durable campaign state changed concurrently.",
            code="state_ref_conflict",
        )
    )
    monkeypatch.setattr(cli, "build_steward_advance_service", lambda: service)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli.app,
        ["steward", "advance", "--issue", "42"],
    )

    assert result.exit_code == 1
    assert "state_ref_conflict" in result.stdout


@pytest.mark.parametrize("json_output", (False, True))
def test_steward_advance_renders_stable_candidate_failure_detail(
    monkeypatch,
    tmp_path: Path,
    json_output: bool,
) -> None:
    summary = (
        "Candidate draft could not be created. "
        "(DraftAuthenticationError: Azure authentication failed.)"
    )
    service = StewardService(
        StewardAdvanceResult(
            status=StewardAdvanceStatus.FAILED,
            issue_number=42,
            summary=summary,
            phase="baseline",
            revision="a" * 40,
            code="candidate_draft_unavailable",
        )
    )
    monkeypatch.setattr(cli, "build_steward_advance_service", lambda: service)
    monkeypatch.chdir(tmp_path)
    arguments = ["steward", "advance", "--issue", "42"]
    if json_output:
        arguments.append("--json")

    result = CliRunner().invoke(cli.app, arguments)

    assert result.exit_code == 1
    assert "candidate_draft_unavailable" in result.stdout
    assert "DraftAuthenticationError" in result.stdout
    if json_output:
        payload = json.loads(result.stdout)
        assert payload["summary"] == summary
        assert payload["code"] == "candidate_draft_unavailable"


def test_candidate_designer_submits_typed_result_through_cli(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class CandidateDesignService:
        requests = []

        def submit(self, request):
            self.requests.append(request)
            return CandidateDesignSubmissionResult(
                CandidateDesignSubmissionStatus.RECORDED,
                StateRefSnapshot(
                    "a" * 40,
                    CampaignState(
                        42,
                        1,
                        1,
                        CampaignPhase.SPECIFICATION,
                    ),
                    (),
                    (),
                ),
            )

    service = CandidateDesignService()
    monkeypatch.setattr(
        cli,
        "build_candidate_design_submission_service",
        lambda: service,
    )
    monkeypatch.chdir(tmp_path)
    result_file = tmp_path / "design-result.json"
    result_file.write_text("{}", encoding="utf-8")

    result = CliRunner().invoke(
        cli.app,
        [
            "steward",
            "candidate-design-result",
            "--issue",
            "42",
            "--effect",
            "design-42-1-1",
            "--worker-issue",
            "84",
            "--result-file",
            str(result_file),
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "code": None,
        "issue_number": 42,
        "revision": "a" * 40,
        "status": "recorded",
    }
    assert service.requests[0].repository_root == tmp_path
    assert service.requests[0].effect_id == "design-42-1-1"
    assert service.requests[0].worker_issue_number == 84
    assert service.requests[0].result_file == result_file
