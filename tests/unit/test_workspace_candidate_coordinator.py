from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from foundry_opt.evaluation import (
    EvaluationPolicy,
    MetricDirection,
    MetricPolicy,
)
from foundry_opt.orchestration import (
    CandidateExperimentRequest,
    CandidateExperimentResult,
    GhWorkspacePullRequestFinalizer,
    InMemoryWorkspaceStore,
    OptimizationWorkspace,
    WorkspaceCandidate,
    WorkspaceCandidateCoordinator,
    WorkspaceIssue,
    WorkspaceNextActionKind,
    WorkspacePhase,
    WorkspacePullRequest,
    WorkspaceReportContext,
    WorkspaceRequest,
    WorkspaceSelectionDecision,
    WorkspaceTrigger,
)
from foundry_opt.orchestration.public_evidence import PullRequestProjection
from foundry_opt.preflight.interfaces import CommandResult


def _patch(candidate_id: str) -> bytes:
    return (
        f"diff --git a/agent.py b/agent.py\n"
        f"--- a/agent.py\n"
        f"+++ b/agent.py\n"
        f"@@ -1 +1 @@\n"
        f"-baseline\n"
        f"+{candidate_id}\n"
    ).encode()


def _candidate(index: int) -> WorkspaceCandidate:
    candidate_id = f"candidate-{index}"
    patch = _patch(candidate_id)
    return WorkspaceCandidate(
        experiment=CandidateExperimentRequest(
            issue_number=31,
            candidate_id=candidate_id,
            patch_sha256=hashlib.sha256(patch).hexdigest(),
            idempotency_key=f"{index + 8:x}" * 64,
        ),
        exact_patch=patch,
        summary=f"Exact summary for {candidate_id}.",
        changed_paths=(
            ("agent.py", "scratch/exploration.txt")
            if index == 1
            else ("agent.py",)
        ),
        validation=("uv run pytest -q: passed",),
        expected_tree=f"{index:x}" * 40,
    )


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def evaluate(
        self,
        request: CandidateExperimentRequest,
    ) -> CandidateExperimentResult:
        self.calls.append(request.candidate_id)
        index = len(self.calls)
        return CandidateExperimentResult(
            candidate_id=request.candidate_id,
            executor="direct_oidc",
            metrics={"quality": float(index)},
            guardrails={"safety": "pass"},
            draft_id=f"draft-{index}",
            evaluation_id=f"evaluation-{index}",
            run_id=f"run-{index}",
        )


class SelectSecondCandidate:
    def select(self, request):
        assert tuple(
            summary.candidate_id for summary in request.experiments
        ) == ("candidate-1", "candidate-2")
        return WorkspaceSelectionDecision(
            selected_candidate_id="candidate-2",
            eligible_candidate_ids=("candidate-2",),
            recommendation="Select the stronger exact candidate.",
            rejection_reasons={
                "candidate-1": "Lower trusted quality score.",
            },
            required_checks={
                "Foundry exact candidate check": "success",
                "tests": "success",
            },
        )


class RecordingFinalizer:
    def __init__(self) -> None:
        self.calls = []

    def finalize(self, repository_root, pull_request, projection):
        self.calls.append((repository_root, pull_request, projection))
        return WorkspacePullRequest(
            number=pull_request.number,
            issue_number=pull_request.issue_number,
            branch=pull_request.branch,
            title=projection.title,
            draft=projection.draft,
            reuse_existing=True,
            base_commit=pull_request.base_commit,
        )


def _report_context() -> WorkspaceReportContext:
    return WorkspaceReportContext(
        baseline_metrics={"quality": 0.0},
        policy=EvaluationPolicy(
            metrics=(
                MetricPolicy(
                    name="quality",
                    direction=MetricDirection.MAXIMIZE,
                    threshold=1.0,
                    materiality=0.5,
                ),
            )
        ),
        sample_count=6,
        split="development",
        spec_sha256="a" * 64,
    )


def _issue() -> WorkspaceIssue:
    return WorkspaceIssue(
        number=31,
        title="[Optimize] Improve policy coverage",
        body="Improve policy coverage without weakening safety.",
        base_commit="b" * 40,
    )


def _pull_request() -> WorkspacePullRequest:
    return WorkspacePullRequest(
        number=104,
        issue_number=31,
        branch="foundry-opt/workspace/issue-31",
        title="[Optimize] #31 workspace - draft, not yet selectable",
        draft=True,
        reuse_existing=True,
        base_commit="b" * 40,
    )


def test_candidate_completion_evaluates_exact_count_and_finalizes_same_pr(
    tmp_path: Path,
) -> None:
    store = InMemoryWorkspaceStore()
    runner = RecordingRunner()
    finalizer = RecordingFinalizer()
    coordinator = WorkspaceCandidateCoordinator(
        store=store,
        runner=runner,
        selector=SelectSecondCandidate(),
        finalizer=finalizer,
        candidate_count=2,
    )
    workspace = OptimizationWorkspace(
        store=store,
        candidate_coordinator=coordinator,
    )
    workspace.advance(
        WorkspaceRequest(
            repository_root=tmp_path,
            issue=_issue(),
            trigger=WorkspaceTrigger.ISSUE_CREATED,
            workspace_pull_request=_pull_request(),
        )
    )

    result = workspace.advance(
        WorkspaceRequest(
            repository_root=tmp_path,
            issue=_issue(),
            trigger=WorkspaceTrigger.EXPERIMENTS_COMPLETED,
            workspace_pull_request=_pull_request(),
            candidates=(_candidate(1), _candidate(2)),
            report_context=_report_context(),
        )
    )

    snapshot = store.load(31)
    assert runner.calls == ["candidate-1", "candidate-2"]
    assert snapshot is not None
    assert snapshot.phase is WorkspacePhase.AWAITING_SELECTION
    assert [item.candidate_id for item in snapshot.candidates] == [
        "candidate-1",
        "candidate-2",
    ]
    assert [item.selected for item in snapshot.candidates] == [False, True]
    assert snapshot.selected_patch == _candidate(2).exact_patch
    assert snapshot.external_operation_ids == (
        "draft-1",
        "evaluation-1",
        "run-1",
        "draft-2",
        "evaluation-2",
        "run-2",
    )
    assert result.workspace_pull_request is not None
    assert result.workspace_pull_request.number == 104
    assert result.workspace_pull_request.draft is False
    assert result.workspace_pull_request.title == (
        "[Optimize] #31 selected candidate"
    )
    assert result.next_action is not None
    assert result.next_action.kind is (
        WorkspaceNextActionKind.MERGE_WORKSPACE_PULL_REQUEST
    )
    assert result.report is not None
    assert result.report.candidate_id == "candidate-2"
    assert result.report.candidate_metrics == {"quality": 2.0}
    assert result.report.changed_paths == ("agent.py",)
    assert "scratch/exploration.txt" not in result.report.changed_paths
    assert result.report.patch_sha256 == (
        _candidate(2).experiment.patch_sha256
    )
    document = result.to_dict()
    assert document["next_action"]["kind"] == (
        "merge_workspace_pull_request"
    )
    assert document["report"]["candidate_id"] == "candidate-2"
    assert document["workspace_pull_request"]["number"] == 104
    assert len(finalizer.calls) == 1


def test_candidate_completion_requires_configured_count_before_evaluation(
    tmp_path: Path,
) -> None:
    store = InMemoryWorkspaceStore()
    runner = RecordingRunner()
    coordinator = WorkspaceCandidateCoordinator(
        store=store,
        runner=runner,
        selector=SelectSecondCandidate(),
        finalizer=RecordingFinalizer(),
        candidate_count=2,
    )
    workspace = OptimizationWorkspace(
        store=store,
        candidate_coordinator=coordinator,
    )
    workspace.advance(
        WorkspaceRequest(
            repository_root=tmp_path,
            issue=_issue(),
            trigger=WorkspaceTrigger.ISSUE_CREATED,
            workspace_pull_request=_pull_request(),
        )
    )
    before = store.load(31)

    with pytest.raises(ValueError, match="configured candidate count"):
        workspace.advance(
            WorkspaceRequest(
                repository_root=tmp_path,
                issue=_issue(),
                trigger=WorkspaceTrigger.EXPERIMENTS_COMPLETED,
                workspace_pull_request=_pull_request(),
                candidates=(_candidate(1),),
                report_context=_report_context(),
            )
        )

    assert runner.calls == []
    assert store.load(31) == before


def test_candidate_completion_fails_closed_before_selection_without_checks(
    tmp_path: Path,
) -> None:
    class UnsafeSelector:
        def select(self, request):
            return WorkspaceSelectionDecision(
                selected_candidate_id="candidate-2",
                eligible_candidate_ids=("candidate-2",),
                recommendation="Select it.",
                rejection_reasons={"candidate-1": "Lower score."},
                required_checks={"tests": "pending"},
            )

    store = InMemoryWorkspaceStore()
    runner = RecordingRunner()
    finalizer = RecordingFinalizer()
    coordinator = WorkspaceCandidateCoordinator(
        store=store,
        runner=runner,
        selector=UnsafeSelector(),
        finalizer=finalizer,
        candidate_count=2,
    )
    workspace = OptimizationWorkspace(
        store=store,
        candidate_coordinator=coordinator,
    )
    workspace.advance(
        WorkspaceRequest(
            repository_root=tmp_path,
            issue=_issue(),
            trigger=WorkspaceTrigger.ISSUE_CREATED,
            workspace_pull_request=_pull_request(),
        )
    )

    with pytest.raises(ValueError, match="successful trusted checks"):
        workspace.advance(
            WorkspaceRequest(
                repository_root=tmp_path,
                issue=_issue(),
                trigger=WorkspaceTrigger.EXPERIMENTS_COMPLETED,
                workspace_pull_request=_pull_request(),
                candidates=(_candidate(1), _candidate(2)),
                report_context=_report_context(),
            )
        )

    assert runner.calls == ["candidate-1", "candidate-2"]
    assert store.load(31).phase is WorkspacePhase.EVALUATING
    assert finalizer.calls == []


def test_github_finalizer_preserves_workspace_identity_and_marks_pr_ready(
    tmp_path: Path,
) -> None:
    class Commands:
        def __init__(self) -> None:
            self.calls = []

        def run(self, arguments, **kwargs):
            self.calls.append((tuple(arguments), kwargs))
            return CommandResult(0, "", "")

    commands = Commands()
    finalized = GhWorkspacePullRequestFinalizer(
        commands,
        repository="octo-org/optimizer",
    ).finalize(
        tmp_path,
        _pull_request(),
        PullRequestProjection(
            title="[Optimize] #31 selected candidate",
            body="Trusted public evidence.",
            draft=False,
        ),
    )

    assert finalized.number == 104
    assert finalized.draft is False
    assert commands.calls[0][0][:3] == ("gh", "pr", "edit")
    assert "<!-- foundry-opt:workspace-pr:issue-31:v1 -->" in (
        commands.calls[0][1]["input_text"]
    )
    assert f"<!-- foundry-opt:workspace-base:{'b' * 40} -->" in (
        commands.calls[0][1]["input_text"]
    )
    assert commands.calls[1][0][:3] == ("gh", "pr", "ready")
