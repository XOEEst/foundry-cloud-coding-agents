from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
import subprocess

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
    GitWorkspaceExactBranchPublisher,
    InMemoryWorkspaceStore,
    OptimizationWorkspace,
    WorkspaceCandidate,
    WorkspaceCandidateCoordinator,
    WorkspaceExactPatchResult,
    WorkspaceIssue,
    WorkspaceNextActionKind,
    WorkspacePhase,
    WorkspacePullRequest,
    WorkspaceReportContext,
    WorkspaceRequest,
    WorkspaceSelectionDecision,
    WorkspaceTrigger,
)
from foundry_opt.adapters.commands import SubprocessCommandRunner
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
    experiment = CandidateExperimentRequest(
        issue_number=31,
        candidate_id=candidate_id,
        patch_sha256=hashlib.sha256(patch).hexdigest(),
        bundle_sha256=f"{index + 3:x}" * 64,
        evidence_sha256=f"{index + 5:x}" * 64,
        idempotency_key=f"{index + 8:x}" * 64,
    )
    return WorkspaceCandidate(
        experiment=experiment,
        experiment_result=CandidateExperimentResult(
            candidate_id=candidate_id,
            executor="direct_oidc",
            metrics={"quality": float(index)},
            guardrails={"safety": "pass"},
            draft_id=f"draft-{index}",
            evaluation_id=f"evaluation-{index}",
            run_id=f"run-{index}",
            bundle_sha256=experiment.bundle_sha256,
            evidence_sha256=experiment.evidence_sha256,
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
        index = int(request.candidate_id.rsplit("-", 1)[1])
        return CandidateExperimentResult(
            candidate_id=request.candidate_id,
            executor="direct_oidc",
            metrics={"quality": float(index)},
            guardrails={"safety": "pass"},
            draft_id=f"draft-{index}",
            evaluation_id=f"evaluation-{index}",
            run_id=f"run-{index}",
            bundle_sha256=request.bundle_sha256,
            evidence_sha256=request.evidence_sha256,
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


class RecordingExactPublisher:
    def __init__(self, *, tree: str = "2" * 40) -> None:
        self.tree = tree
        self.calls = []

    def publish(self, repository_root, pull_request, candidate):
        self.calls.append((repository_root, pull_request, candidate))
        return WorkspaceExactPatchResult(
            commit_sha="c" * 40,
            tree_sha=self.tree,
            changed_paths=candidate.changed_paths,
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
    publisher = RecordingExactPublisher()
    coordinator = WorkspaceCandidateCoordinator(
        store=store,
        runner=runner,
        selector=SelectSecondCandidate(),
        exact_publisher=publisher,
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
    assert snapshot.external_operation_ids[:10] == (
        "draft-1",
        "evaluation-1",
        "run-1",
        f"candidate-1:bundle:{'4' * 64}",
        f"candidate-1:evidence:{'6' * 64}",
        "draft-2",
        "evaluation-2",
        "run-2",
        f"candidate-2:bundle:{'5' * 64}",
        f"candidate-2:evidence:{'7' * 64}",
    )
    assert f"candidate-2:patch:{_candidate(2).experiment.patch_sha256}" in (
        snapshot.external_operation_ids
    )
    assert f"candidate-2:tree:{'2' * 40}" in (
        snapshot.external_operation_ids
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
    assert result.report.bundle_sha256 == "5" * 64
    assert result.report.evidence_sha256 == "7" * 64
    assert snapshot.lineage is not None
    assert snapshot.lineage.patch_sha256 == result.report.patch_sha256
    assert snapshot.lineage.evidence_sha256 == (
        result.report.evidence_sha256
    )
    assert snapshot.lineage.bundle_sha256 == result.report.bundle_sha256
    assert snapshot.lineage.spec_sha256 == result.report.spec_sha256
    assert snapshot.lineage.expected_tree == result.report.expected_tree
    assert snapshot.lineage.required_checks == {
        "Foundry exact candidate check": "success",
        "tests": "success",
    }
    assert snapshot.lineage.required_checks_provenance == (
        f"trusted-selector:head:{'c' * 40}"
    )
    document = result.to_dict()
    assert document["next_action"]["kind"] == (
        "merge_workspace_pull_request"
    )
    assert document["report"]["candidate_id"] == "candidate-2"
    assert document["workspace_pull_request"]["number"] == 104
    assert publisher.calls[0][2].experiment.candidate_id == "candidate-2"
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
        exact_publisher=RecordingExactPublisher(),
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
        exact_publisher=RecordingExactPublisher(),
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


def test_candidate_completion_never_commits_or_readies_unverified_tree(
    tmp_path: Path,
) -> None:
    store = InMemoryWorkspaceStore()
    finalizer = RecordingFinalizer()
    coordinator = WorkspaceCandidateCoordinator(
        store=store,
        runner=RecordingRunner(),
        selector=SelectSecondCandidate(),
        exact_publisher=RecordingExactPublisher(tree="f" * 40),
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

    with pytest.raises(ValueError, match="exact candidate tree"):
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

    assert store.load(31).phase is WorkspacePhase.EVALUATING
    assert finalizer.calls == []


def test_candidate_completion_resumes_partial_and_duplicates_without_rerun(
    tmp_path: Path,
) -> None:
    class FailsSecondOnce(RecordingRunner):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        def evaluate(self, request):
            if request.candidate_id == "candidate-2" and not self.failed:
                self.failed = True
                self.calls.append(request.candidate_id)
                raise RuntimeError("candidate pending")
            return super().evaluate(request)

    store = InMemoryWorkspaceStore()
    runner = FailsSecondOnce()
    coordinator = WorkspaceCandidateCoordinator(
        store=store,
        runner=runner,
        selector=SelectSecondCandidate(),
        exact_publisher=RecordingExactPublisher(),
        finalizer=RecordingFinalizer(),
        candidate_count=2,
    )
    workspace = OptimizationWorkspace(
        store=store,
        candidate_coordinator=coordinator,
    )
    request = WorkspaceRequest(
        repository_root=tmp_path,
        issue=_issue(),
        trigger=WorkspaceTrigger.EXPERIMENTS_COMPLETED,
        workspace_pull_request=_pull_request(),
        candidates=(_candidate(1), _candidate(2)),
        report_context=_report_context(),
    )
    workspace.advance(
        WorkspaceRequest(
            repository_root=tmp_path,
            issue=_issue(),
            trigger=WorkspaceTrigger.ISSUE_CREATED,
            workspace_pull_request=_pull_request(),
        )
    )

    with pytest.raises(RuntimeError, match="pending"):
        workspace.advance(request)

    partial = store.load(31)
    assert partial.phase is WorkspacePhase.EVALUATING
    assert [item.candidate_id for item in partial.candidates] == [
        "candidate-1"
    ]

    completed = workspace.advance(request)
    completed_snapshot = store.load(31)
    completed_revision = completed_snapshot.revision
    completed_lineage = completed_snapshot.lineage
    duplicate = workspace.advance(request)

    assert runner.calls == ["candidate-1", "candidate-2", "candidate-2"]
    assert completed.recorded is True
    assert duplicate.recorded is False
    assert store.load(31).revision == completed_revision
    assert store.load(31).lineage == completed_lineage
    assert duplicate.report.patch_sha256 == completed_lineage.patch_sha256


def test_github_finalizer_preserves_workspace_identity_without_marking_pr_ready(
    tmp_path: Path,
) -> None:
    class Commands:
        def __init__(self) -> None:
            self.calls = []

        def run(self, arguments, **kwargs):
            self.calls.append((tuple(arguments), kwargs))
            if tuple(arguments[:3]) == ("gh", "pr", "view"):
                return CommandResult(
                    0,
                    json.dumps(
                        {
                            "body": "old",
                            "headRefName": (
                                "foundry-opt/workspace/issue-31"
                            ),
                            "isDraft": True,
                            "number": 104,
                            "state": "OPEN",
                            "title": "old",
                        }
                    ),
                    "",
                )
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
    assert finalized.draft is True
    assert commands.calls[0][0][:3] == ("gh", "pr", "view")
    assert commands.calls[1][0][:3] == ("gh", "pr", "edit")
    assert "<!-- foundry-opt:workspace-pr:issue-31:v1 -->" in (
        commands.calls[1][1]["input_text"]
    )
    assert f"<!-- foundry-opt:workspace-base:{'b' * 40} -->" in (
        commands.calls[1][1]["input_text"]
    )
    assert len(commands.calls) == 2


def test_github_finalizer_reconciles_already_ready_pr_without_edits(
    tmp_path: Path,
) -> None:
    projection = PullRequestProjection(
        title="[Optimize] #31 selected candidate",
        body="Trusted public evidence.",
        draft=False,
    )
    expected_body = "\n\n".join(
        (
            "<!-- foundry-opt:workspace-pr:issue-31:v1 -->",
            f"<!-- foundry-opt:workspace-base:{'b' * 40} -->",
            projection.body,
        )
    )

    class Commands:
        def __init__(self) -> None:
            self.calls = []

        def run(self, arguments, **kwargs):
            self.calls.append(tuple(arguments))
            return CommandResult(
                0,
                json.dumps(
                    {
                        "body": expected_body,
                        "headRefName": (
                            "foundry-opt/workspace/issue-31"
                        ),
                        "isDraft": False,
                        "number": 104,
                        "state": "OPEN",
                        "title": projection.title,
                    }
                ),
                "",
            )

    commands = Commands()
    finalized = GhWorkspacePullRequestFinalizer(
        commands,
        repository="octo-org/optimizer",
    ).finalize(tmp_path, _pull_request(), projection)

    assert finalized.draft is False
    assert len(commands.calls) == 1
    assert commands.calls[0][:3] == ("gh", "pr", "view")


def test_exact_publisher_normalizes_existing_workspace_branch(
    tmp_path: Path,
) -> None:
    origin = tmp_path / "origin.git"
    repository = tmp_path / "repository"
    subprocess.run(
        ("git", "init", "--bare", str(origin)),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "clone", str(origin), str(repository)),
        check=True,
        capture_output=True,
    )
    for name, value in (
        ("user.name", "Workspace Test"),
        ("user.email", "workspace@example.invalid"),
    ):
        subprocess.run(
            ("git", "config", name, value),
            cwd=repository,
            check=True,
            capture_output=True,
        )
    (repository / "agent.py").write_text("baseline\n", encoding="utf-8")
    subprocess.run(
        ("git", "add", "agent.py"),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "commit", "-m", "base"),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    base = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ("git", "branch", "-M", "main"),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "push", "-u", "origin", "main"),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    branch = "foundry-opt/workspace/issue-31"
    subprocess.run(
        ("git", "switch", "-c", branch),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "commit", "--allow-empty", "-m", "workspace"),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "push", "origin", branch),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "switch", "main"),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    (repository / "agent.py").write_text("candidate-2\n", encoding="utf-8")
    patch = subprocess.run(
        ("git", "diff", "--binary"),
        cwd=repository,
        check=True,
        capture_output=True,
    ).stdout
    subprocess.run(
        ("git", "add", "agent.py"),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    expected_tree = subprocess.run(
        ("git", "write-tree"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ("git", "reset", "--hard", base),
        cwd=repository,
        check=True,
        capture_output=True,
    )
    candidate = WorkspaceCandidate(
        experiment=(experiment := CandidateExperimentRequest(
            issue_number=31,
            candidate_id="candidate-2",
            patch_sha256=hashlib.sha256(patch).hexdigest(),
            bundle_sha256="5" * 64,
            evidence_sha256="7" * 64,
            idempotency_key="a" * 64,
        )),
        experiment_result=CandidateExperimentResult(
            candidate_id="candidate-2",
            executor="direct_oidc",
            metrics={"quality": 2.0},
            guardrails={"safety": "pass"},
            draft_id="draft-2",
            evaluation_id="evaluation-2",
            run_id="run-2",
            bundle_sha256=experiment.bundle_sha256,
            evidence_sha256=experiment.evidence_sha256,
        ),
        exact_patch=patch,
        summary="Selected exact candidate.",
        changed_paths=("agent.py",),
        validation=("pytest: passed",),
        expected_tree=expected_tree,
    )

    result = GitWorkspaceExactBranchPublisher(
        SubprocessCommandRunner()
    ).publish(
        repository,
        replace(_pull_request(), base_commit=base),
        candidate,
    )

    assert result.tree_sha == expected_tree
    assert result.changed_paths == ("agent.py",)
    assert subprocess.run(
        ("git", "ls-remote", "origin", f"refs/heads/{branch}"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()[0] == result.commit_sha
    assert subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == base
    assert subprocess.run(
        ("git", "status", "--porcelain=v1", "--untracked-files=all"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""
