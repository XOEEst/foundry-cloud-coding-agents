from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess

import pytest

from foundry_opt.adapters.commands import SubprocessCommandRunner
from foundry_opt.orchestration import (
    CampaignPhase,
    CampaignState,
    OutboxRecord,
    StateRefSnapshot,
)
from foundry_opt.orchestration.candidate_workers import (
    CandidateDesignIntent,
    CandidateDesignPushUnacknowledgedError,
    CandidateDesignResult,
    CandidateDesignSubmissionRequest,
    CandidateDesignSubmissionService,
    CandidateDesignSubmissionStatus,
)
from foundry_opt.optimization.production import (
    _ProductionCandidateDesigner,
    _ProductionCandidateDesignRepository,
)


SPEC_SHA256 = "a" * 64


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _capture_fixture(tmp_path: Path):
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    (root / "agent").mkdir()
    source = root / "agent" / "instructions.md"
    source.write_text("baseline\n", encoding="utf-8")
    _git(root, "add", "agent/instructions.md")
    _git(root, "commit", "-m", "baseline")
    base_commit = _git(root, "rev-parse", "HEAD")
    result_file = (
        root
        / ".foundry-optimizer"
        / "design-results"
        / "design-31-1-1.json"
    )
    result_file.parent.mkdir(parents=True)
    result_file.write_text("{}", encoding="utf-8")
    intent = CandidateDesignIntent(
        effect_id="design-31-1-1",
        issue_number=31,
        generation=1,
        spec_sha256=SPEC_SHA256,
        base_commit=base_commit,
        target="support",
        candidate_id="candidate-1",
        slot=1,
        worktree=root.resolve(),
        goal="Improve support answers.",
        edit_paths=(Path("agent"),),
        allowed_mutations=frozenset({"system_instructions"}),
        restricted_opt_ins={},
        baseline_metrics={"quality": 0.5},
    )
    result = CandidateDesignResult(
        effect_id=intent.effect_id,
        result_id="designer-result-1",
        issue_number=intent.issue_number,
        generation=intent.generation,
        spec_sha256=intent.spec_sha256,
        base_commit=intent.base_commit,
        candidate_id=intent.candidate_id,
        slot=intent.slot,
        idea_id="idea-1",
        mutation_class="system_instructions",
        motivation="Clarify the escalation rule.",
        lessons=("The baseline omits an escalation rule.",),
        complexity="small",
    )
    request = CandidateDesignSubmissionRequest(
        repository_root=root.resolve(),
        issue_number=31,
        effect_id=intent.effect_id,
        worker_issue_number=84,
        result_file=result_file.resolve(),
    )
    return root, source, intent, result, request


def test_candidate_capture_rejects_initial_plan_with_committed_change(
    tmp_path: Path,
) -> None:
    root, source, intent, result, request = _capture_fixture(tmp_path)
    source.write_text("initial plan changed the file\n", encoding="utf-8")
    _git(root, "add", "agent/instructions.md")
    _git(root, "commit", "-m", "Initial plan")
    source.write_text("candidate design\n", encoding="utf-8")

    with pytest.raises(ValueError, match="checkout base changed"):
        _ProductionCandidateDesignRepository(
            SubprocessCommandRunner()
        ).capture(request, intent, result)


def test_candidate_capture_rejects_disallowed_uncommitted_path(
    tmp_path: Path,
) -> None:
    root, source, intent, result, request = _capture_fixture(tmp_path)
    _git(root, "commit", "--allow-empty", "-m", "Initial plan")
    source.write_text("candidate design\n", encoding="utf-8")
    (root / "outside.txt").write_text("forbidden\n", encoding="utf-8")

    with pytest.raises(ValueError, match="forbidden paths"):
        _ProductionCandidateDesignRepository(
            SubprocessCommandRunner()
        ).capture(request, intent, result)


def test_candidate_capture_rejects_merge_metadata_commit(
    tmp_path: Path,
) -> None:
    root, source, intent, result, request = _capture_fixture(tmp_path)
    _git(root, "checkout", "-b", "copilot-plan")
    _git(root, "commit", "--allow-empty", "-m", "Initial plan")
    _git(root, "checkout", "-b", "other-plan", intent.base_commit)
    _git(root, "commit", "--allow-empty", "-m", "Other plan")
    _git(root, "merge", "--no-ff", "copilot-plan", "-m", "Merge plans")
    source.write_text("candidate design\n", encoding="utf-8")

    with pytest.raises(ValueError, match="checkout base changed"):
        _ProductionCandidateDesignRepository(
            SubprocessCommandRunner()
        ).capture(request, intent, result)


def test_candidate_capture_rejects_unrelated_same_tree_commit(
    tmp_path: Path,
) -> None:
    root, source, intent, result, request = _capture_fixture(tmp_path)
    _git(root, "checkout", "--orphan", "unrelated-plan")
    _git(root, "rm", "-rf", ".")
    (root / "agent").mkdir()
    source.write_text("baseline\n", encoding="utf-8")
    _git(root, "add", "agent/instructions.md")
    _git(root, "commit", "-m", "Initial plan")
    source.write_text("candidate design\n", encoding="utf-8")

    with pytest.raises(ValueError, match="checkout base changed"):
        _ProductionCandidateDesignRepository(
            SubprocessCommandRunner()
        ).capture(request, intent, result)


def test_candidate_capture_rejects_git_replace_rewrites(
    tmp_path: Path,
) -> None:
    root, source, intent, result, request = _capture_fixture(tmp_path)
    _git(root, "commit", "--allow-empty", "-m", "Initial plan")
    replacement = root / "replacement.txt"
    replacement.write_text("rewritten baseline\n", encoding="utf-8")
    original_blob = _git(
        root,
        "rev-parse",
        f"{intent.base_commit}:agent/instructions.md",
    )
    replacement_blob = _git(root, "hash-object", "-w", "replacement.txt")
    replacement.unlink()
    _git(root, "replace", original_blob, replacement_blob)
    source.write_text("candidate design\n", encoding="utf-8")

    with pytest.raises(ValueError, match="checkout base changed"):
        _ProductionCandidateDesignRepository(
            SubprocessCommandRunner()
        ).capture(request, intent, result)


def test_candidate_capture_rejects_git_graft_rewrites(
    tmp_path: Path,
) -> None:
    root, source, intent, result, request = _capture_fixture(tmp_path)
    _git(root, "checkout", "--orphan", "grafted-plan")
    _git(root, "rm", "-rf", ".")
    (root / "agent").mkdir()
    source.write_text("baseline\n", encoding="utf-8")
    _git(root, "add", "agent/instructions.md")
    _git(root, "commit", "-m", "Initial plan")
    head = _git(root, "rev-parse", "HEAD")
    grafts = root / ".git" / "info" / "grafts"
    grafts.write_text(f"{head} {intent.base_commit}\n", encoding="ascii")
    source.write_text("candidate design\n", encoding="utf-8")

    with pytest.raises(ValueError, match="checkout base changed"):
        _ProductionCandidateDesignRepository(
            SubprocessCommandRunner()
        ).capture(request, intent, result)


def test_candidate_capture_excludes_reserved_files_from_broad_allowed_path(
    tmp_path: Path,
) -> None:
    root, source, intent, result, request = _capture_fixture(tmp_path)
    intent = replace(intent, edit_paths=(Path("."),))
    _git(root, "commit", "--allow-empty", "-m", "Initial plan")
    source.write_text("candidate design\n", encoding="utf-8")

    with pytest.raises(
        CandidateDesignPushUnacknowledgedError
    ) as captured:
        _ProductionCandidateDesignRepository(
            SubprocessCommandRunner()
        ).capture(request, intent, result)

    artifact = captured.value.artifact
    assert artifact.changed_paths == (Path("agent/instructions.md"),)
    assert _git(
        root,
        "diff",
        "--name-only",
        intent.base_commit,
        artifact.head_commit,
    ) == "agent/instructions.md"
    assert ".foundry-optimizer" not in _git(
        root,
        "ls-tree",
        "-r",
        "--name-only",
        artifact.head_commit,
    )


class Ledger:
    def __init__(self, snapshot: StateRefSnapshot) -> None:
        self.snapshot = snapshot

    def load(self, repository_root: Path, issue_number: int):
        return self.snapshot

    def commit(self, repository_root: Path, **kwargs):
        self.snapshot = replace(
            self.snapshot,
            revision="d" * 40,
            outbox=(
                *self.snapshot.outbox,
                *kwargs.get("outbox", ()),
            ),
        )
        return self.snapshot


def test_production_designer_result_resumes_without_editing_steward_checkout(
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare")
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    (root / "agent").mkdir()
    source = root / "agent" / "instructions.md"
    source.write_text("baseline\n", encoding="utf-8")
    _git(root, "add", "agent/instructions.md")
    _git(root, "commit", "-m", "baseline")
    base_commit = _git(root, "rev-parse", "HEAD")
    _git(root, "remote", "add", "origin", str(remote))
    _git(root, "push", "-u", "origin", "main")

    planned = OutboxRecord(
        "design-31-1-1-worker",
        "specialist_work_request",
        1,
        2,
        {
            "allowed_mutations": ["system_instructions"],
            "allowed_paths": ["agent"],
            "base_commit": base_commit,
            "baseline_metrics": {"quality": 0.5},
            "branch": "foundry-opt/issue-31-g1/candidate-1",
            "candidate_feedback": [],
            "candidate_id": "candidate-1",
            "effect_id": "design-31-1-1",
            "goal": (
                "Improve grounded support answers without weakening safety."
            ),
            "issue_number": 31,
            "reason": "candidate_design_pending",
            "restricted_opt_ins": {},
            "slot": 1,
            "spec_sha256": SPEC_SHA256,
            "specialist": "foundry-candidate-designer",
            "target": "support",
            "work_kind": "design_candidate",
        },
    )
    assigned = OutboxRecord(
        "design-31-1-1-worker-succeeded",
        "specialist_work_succeeded",
        1,
        2,
        {
            "assigned": True,
            "created": True,
            "effect_id": planned.record_id,
            "issue_number": 31,
            "result_id": "design-worker-result-84",
            "specialist": "foundry-candidate-designer",
            "work_kind": "design_candidate",
            "worker_issue_number": 84,
        },
    )
    ledger = Ledger(
        StateRefSnapshot(
            "c" * 40,
            CampaignState(
                31,
                1,
                2,
                CampaignPhase.BASELINE,
                spec_sha256=SPEC_SHA256,
            ),
            (),
            (planned, assigned),
        )
    )
    source.write_text("candidate design\n", encoding="utf-8")
    result_file = (
        root
        / ".foundry-optimizer"
        / "design-results"
        / "design-31-1-1.json"
    )
    result_file.parent.mkdir(parents=True)
    result_file.write_text(
        json.dumps(
            {
                "effect_id": "design-31-1-1",
                "result_id": "designer-result-1",
                "issue_number": 31,
                "generation": 1,
                "spec_sha256": SPEC_SHA256,
                "base_commit": base_commit,
                "candidate_id": "candidate-1",
                "slot": 1,
                "idea_id": "idea-1",
                "mutation_class": "system_instructions",
                "parent_idea_ids": [],
                "required_opt_ins": [],
                "motivation": "Clarify the escalation rule.",
                "lessons": ["The baseline omits a required escalation."],
                "complexity": "small",
            }
        ),
        encoding="utf-8",
    )
    commands = SubprocessCommandRunner()
    submitted = CandidateDesignSubmissionService(
        ledger=ledger,
        repository=_ProductionCandidateDesignRepository(commands),
    ).submit(
        CandidateDesignSubmissionRequest(
            repository_root=root,
            issue_number=31,
            effect_id="design-31-1-1",
            worker_issue_number=84,
            result_file=result_file,
        )
    )

    assert submitted.status is CandidateDesignSubmissionStatus.RECORDED
    assert _git(root, "rev-parse", "HEAD") == base_commit
    assert _git(root, "status", "--porcelain") == ""
    assert source.read_text(encoding="utf-8") == "baseline\n"
    assert result_file.exists() is False

    worktree = (
        root
        / ".foundry-optimizer"
        / "worktrees"
        / "issue-31-g1"
        / "candidate-1"
    )
    worktree.parent.mkdir(parents=True)
    _git(
        root,
        "worktree",
        "add",
        "-b",
        "foundry-opt/issue-31-g1/candidate-1",
        str(worktree),
        base_commit,
    )
    intent = CandidateDesignIntent(
        effect_id="design-31-1-1",
        issue_number=31,
        generation=1,
        spec_sha256=SPEC_SHA256,
        base_commit=base_commit,
        target="support",
        candidate_id="candidate-1",
        slot=1,
        worktree=worktree.resolve(),
        goal=(
            "Improve grounded support answers without weakening safety."
        ),
        edit_paths=(Path("agent"),),
        allowed_mutations=frozenset({"system_instructions"}),
        restricted_opt_ins={},
        baseline_metrics={"quality": 0.5},
    )

    results = _ProductionCandidateDesigner(
        ledger=ledger,
        commands=commands,
    ).reconcile(intent)

    assert len(results) == 1
    assert results[0].result_id == "designer-result-1"
    assert (
        worktree / "agent" / "instructions.md"
    ).read_text(encoding="utf-8") == "candidate design\n"
    assert _git(root, "rev-parse", "HEAD") == base_commit
