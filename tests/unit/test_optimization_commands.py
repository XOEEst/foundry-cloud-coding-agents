from __future__ import annotations

from pathlib import Path

import pytest

from foundry_opt.optimization import (
    OptimizeCommandRequest,
    OptimizeCommandResult,
    OptimizeCommandStatus,
    OptimizePhase,
)


def test_apply_request_requires_candidate_id() -> None:
    with pytest.raises(ValueError, match="candidate_id"):
        OptimizeCommandRequest(
            repository_root=Path("."),
            issue_number=1,
            phase=OptimizePhase.APPLY,
        )


def test_candidate_submit_requires_candidate_and_idea_file() -> None:
    with pytest.raises(ValueError, match="candidate_id"):
        OptimizeCommandRequest(
            repository_root=Path("."),
            issue_number=1,
            phase=OptimizePhase.CANDIDATE_SUBMIT,
            idea_file=Path("idea.json"),
        )
    with pytest.raises(ValueError, match="idea_file"):
        OptimizeCommandRequest(
            repository_root=Path("."),
            issue_number=1,
            phase=OptimizePhase.CANDIDATE_SUBMIT,
            candidate_id="candidate-1",
        )


def test_candidate_submit_accepts_candidate_and_idea_file() -> None:
    request = OptimizeCommandRequest(
        repository_root=Path("."),
        issue_number=1,
        phase=OptimizePhase.CANDIDATE_SUBMIT,
        candidate_id="candidate-1",
        idea_file=Path("idea.json"),
    )
    assert request.candidate_id == "candidate-1"
    assert request.idea_file == Path("idea.json")


def test_candidate_request_rejects_candidate_and_idea_file() -> None:
    with pytest.raises(ValueError, match="only valid"):
        OptimizeCommandRequest(
            repository_root=Path("."),
            issue_number=1,
            phase=OptimizePhase.CANDIDATE_REQUEST,
            candidate_id="candidate-1",
        )
    with pytest.raises(ValueError, match="idea_file is only valid"):
        OptimizeCommandRequest(
            repository_root=Path("."),
            issue_number=1,
            phase=OptimizePhase.CANDIDATE_REQUEST,
            idea_file=Path("idea.json"),
        )


def test_candidate_id_is_rejected_for_other_phases() -> None:
    with pytest.raises(ValueError, match="only valid"):
        OptimizeCommandRequest(
            repository_root=Path("."),
            issue_number=1,
            phase=OptimizePhase.RUN,
            candidate_id="candidate-a",
        )


def test_idea_file_is_rejected_for_other_phases() -> None:
    with pytest.raises(ValueError, match="idea_file is only valid"):
        OptimizeCommandRequest(
            repository_root=Path("."),
            issue_number=1,
            phase=OptimizePhase.RUN,
            idea_file=Path("idea.json"),
        )


def test_verify_only_is_rejected_for_other_phases() -> None:
    with pytest.raises(ValueError, match="verify_only"):
        OptimizeCommandRequest(
            repository_root=Path("."),
            issue_number=1,
            phase=OptimizePhase.RUN,
            verify_only=True,
        )


def test_command_result_exit_codes_are_stable() -> None:
    complete = OptimizeCommandResult(
        status=OptimizeCommandStatus.COMPLETE,
        phase=OptimizePhase.RUN,
        summary="done",
        issue_number=1,
    )
    awaiting = OptimizeCommandResult(
        status=OptimizeCommandStatus.AWAITING_AGENT,
        phase=OptimizePhase.RUN,
        summary="awaiting the coding agent",
        issue_number=1,
        next_action="Request the first candidate.",
    )
    blocked = OptimizeCommandResult(
        status=OptimizeCommandStatus.BLOCKED,
        phase=OptimizePhase.RUN,
        summary="blocked",
        issue_number=1,
    )
    failed = OptimizeCommandResult(
        status=OptimizeCommandStatus.FAILED,
        phase=OptimizePhase.RUN,
        summary="failed",
        issue_number=1,
    )

    assert complete.exit_code == 0
    assert awaiting.exit_code == 0
    assert blocked.exit_code == 1
    assert failed.exit_code == 1


def test_awaiting_agent_result_serializes_next_action() -> None:
    result = OptimizeCommandResult(
        status=OptimizeCommandStatus.AWAITING_AGENT,
        phase=OptimizePhase.CANDIDATE_REQUEST,
        summary="Candidate worktree is ready.",
        issue_number=7,
        details={"candidate_id": "candidate-1"},
        next_action="Submit an idea file for candidate-1.",
    )

    assert result.to_dict() == {
        "details": {"candidate_id": "candidate-1"},
        "issue_number": 7,
        "next_action": "Submit an idea file for candidate-1.",
        "phase": "candidate-request",
        "status": "awaiting_agent",
        "summary": "Candidate worktree is ready.",
    }

