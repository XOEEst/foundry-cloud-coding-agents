from collections.abc import Mapping, Sequence
import json
from pathlib import Path

import pytest

from foundry_opt.orchestration import (
    EvidenceMergeGate,
    OptimizationReport,
    PublicEvidenceRenderer,
    WorkspaceIssueStatusProjectionIntent,
    WorkspacePhase,
)
from foundry_opt.orchestration.workspace_projection import (
    GhWorkspaceIssueProjector,
)
from foundry_opt.orchestration.workspace_operations_production import (
    _issue_comments,
)
from foundry_opt.preflight.interfaces import CommandResult


class GitHubComments:
    def __init__(
        self,
        *,
        author: dict | None = None,
        authenticated_author: dict | None = None,
    ) -> None:
        self.comments: list[dict] = []
        self.writes: list[tuple[str, str]] = []
        self.next_id = 1
        self.author = author or {"login": "github-actions[bot]"}
        self.authenticated_author = authenticated_author or self.author

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
        environment: Mapping[str, str] | None = None,
        input_text: str | None = None,
        input_bytes: bytes | None = None,
    ) -> CommandResult:
        del cwd, environment, input_bytes
        command = tuple(arguments)
        if command == ("gh", "api", "user"):
            return CommandResult(
                0,
                json.dumps(self.authenticated_author),
                "",
            )
        if "--paginate" in command:
            return CommandResult(0, json.dumps([self.comments]), "")
        payload = json.loads(input_text or "{}")
        if "--method" not in command:
            raise AssertionError(f"unexpected command: {command}")
        method = command[command.index("--method") + 1]
        endpoint = command[command.index("--input") - 1]
        if method == "POST":
            comment = {
                "id": self.next_id,
                "body": payload["body"],
                "user": self.author,
            }
            self.next_id += 1
            self.comments.append(comment)
            self.writes.append(("POST", payload["body"]))
            return CommandResult(0, json.dumps(comment), "")
        if method == "PATCH":
            comment_id = int(endpoint.rsplit("/", 1)[1])
            comment = next(
                item for item in self.comments if item["id"] == comment_id
            )
            comment["body"] = payload["body"]
            self.writes.append(("PATCH", payload["body"]))
            return CommandResult(0, json.dumps(comment), "")
        raise AssertionError(f"unexpected method: {method}")


def test_workspace_completion_reads_paginated_issue_comments(
    tmp_path: Path,
) -> None:
    commands = GitHubComments()
    commands.comments.append(
        {"id": 7, "body": "<!-- final -->\nComplete."}
    )

    assert _issue_comments(
        commands,
        tmp_path,
        "octo-org/optimizer",
        31,
    ) == commands.comments


def _intent(phase: WorkspacePhase) -> WorkspaceIssueStatusProjectionIntent:
    return WorkspaceIssueStatusProjectionIntent(
        issue_number=31,
        phase=phase,
        workspace_pull_request_number=104,
    )


def _report() -> OptimizationReport:
    return OptimizationReport(
        issue_number=31,
        candidate_id="candidate-2",
        recommendation="Use the trusted selected candidate.",
        alternatives=("candidate-1: rejected",),
        baseline_metrics={"quality": 0.5},
        candidate_metrics={"quality": 0.9},
        guardrails={"safety": "pass"},
        thresholds={"quality": 0.8},
        sample_count=12,
        split="development",
        foundry_operations=(),
        changed_paths=("agent.py",),
        validation=("pytest: passed",),
        spec_sha256="1" * 64,
        base_commit="2" * 40,
        patch_sha256="3" * 64,
        evidence_sha256="4" * 64,
        bundle_sha256="5" * 64,
        expected_tree="6" * 40,
        required_checks={"tests": "success"},
        merge_gate=EvidenceMergeGate.ELIGIBLE,
    )


def test_issue_projection_updates_status_and_appends_milestones_once(
    tmp_path: Path,
) -> None:
    commands = GitHubComments()
    projector = GhWorkspaceIssueProjector(
        commands,
        repository_root=tmp_path,
        repository="octo-org/optimizer",
    )

    first = projector.project(
        _intent(WorkspacePhase.SPECIFICATION),
        base_commit="2" * 40,
    )
    second = projector.project(
        _intent(WorkspacePhase.EVALUATING),
        base_commit="2" * 40,
    )
    retry = projector.project(
        _intent(WorkspacePhase.EVALUATING),
        base_commit="2" * 40,
    )

    bodies = [item["body"] for item in commands.comments]
    assert first.created_milestones == ("specification",)
    assert second.created_milestones == ("experiments",)
    assert retry.status_changed is False
    assert retry.created_milestones == ()
    assert len(commands.comments) == 3
    assert sum("workspace-status:issue-31:v1" in body for body in bodies) == 1
    assert sum(
        "workspace-milestone:issue-31:specification:v1" in body
        for body in bodies
    ) == 1
    assert sum(
        "workspace-milestone:issue-31:experiments:v1" in body
        for body in bodies
    ) == 1
    assert "Phase: `evaluating`" in bodies[0]
    assert [method for method, _ in commands.writes].count("PATCH") == 1

    with pytest.raises(RuntimeError, match="immutable milestone changed"):
        projector.project(
            _intent(WorkspacePhase.EVALUATING),
            base_commit="7" * 40,
        )
    assert sum(
        "approved base `" + "2" * 40 + "`" in item["body"]
        for item in commands.comments
    ) == 1


def test_issue_projection_accepts_exact_authenticated_user_token(
    tmp_path: Path,
) -> None:
    actor = {"id": 123, "login": "octocat", "type": "User"}
    commands = GitHubComments(
        author=actor,
        authenticated_author=actor,
    )
    projector = GhWorkspaceIssueProjector(
        commands,
        repository_root=tmp_path,
        repository="octo-org/optimizer",
    )

    result = projector.project(
        _intent(WorkspacePhase.SPECIFICATION),
        base_commit="a" * 40,
    )

    assert result.status_changed is True


def test_issue_projection_rejects_different_user_token_author(
    tmp_path: Path,
) -> None:
    commands = GitHubComments(
        author={"id": 456, "login": "attacker", "type": "User"},
        authenticated_author={
            "id": 123,
            "login": "octocat",
            "type": "User",
        },
    )
    projector = GhWorkspaceIssueProjector(
        commands,
        repository_root=tmp_path,
        repository="octo-org/optimizer",
    )

    with pytest.raises(
        RuntimeError,
        match="projection was not confirmed",
    ):
        projector.project(
            _intent(WorkspacePhase.SPECIFICATION),
            base_commit="a" * 40,
        )


def test_candidate_ready_evidence_is_identical_on_issue_and_pr(
    tmp_path: Path,
) -> None:
    commands = GitHubComments()
    renderer = PublicEvidenceRenderer()
    projector = GhWorkspaceIssueProjector(
        commands,
        repository_root=tmp_path,
        repository="octo-org/optimizer",
        renderer=renderer,
    )
    report = _report()

    result = projector.project(
        _intent(WorkspacePhase.AWAITING_SELECTION),
        base_commit=report.base_commit,
        report=report,
    )
    retry = projector.project(
        _intent(WorkspacePhase.AWAITING_SELECTION),
        base_commit=report.base_commit,
        report=report,
    )

    issue = renderer.render_issue(report)
    pull_request = renderer.render_pr(report)
    candidate_comments = [
        item["body"]
        for item in commands.comments
        if issue.marker in item["body"]
    ]
    assert result.created_milestones == (
        "specification",
        "experiments",
        "candidate_ready",
    )
    assert retry.created_milestones == ()
    assert candidate_comments == [issue.body]
    assert issue.marker in pull_request.body
    assert "quality | 0.5 | 0.9 | +0.4" in issue.body
    assert "quality | 0.5 | 0.9 | +0.4" in pull_request.body
