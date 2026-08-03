from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess

from foundry_opt.adapters.commands import SubprocessCommandRunner
from foundry_opt.orchestration import (
    CampaignPhase,
    CampaignState,
    OutboxRecord,
    StateRefSnapshot,
)
from foundry_opt.orchestration.candidate_workers import (
    CandidateDesignIntent,
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
