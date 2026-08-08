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
from foundry_opt.orchestration.git_state import (
    CandidateDesignLoopbackError,
    candidate_design_loopback_handoff_session,
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
    (root / "outside.txt").write_text("outside baseline\n", encoding="utf-8")
    _git(root, "add", "agent/instructions.md", "outside.txt")
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


def test_candidate_capture_normalizes_committed_candidate_edits(
    tmp_path: Path,
) -> None:
    root, source, intent, result, request = _capture_fixture(tmp_path)
    _git(root, "commit", "--allow-empty", "-m", "Initial plan")
    source.write_text("candidate design\n", encoding="utf-8")
    _git(root, "add", "agent/instructions.md")
    _git(root, "commit", "-m", "Commit candidate design")

    with pytest.raises(
        CandidateDesignPushUnacknowledgedError
    ) as captured:
        _ProductionCandidateDesignRepository(
            SubprocessCommandRunner()
        ).capture(request, intent, result)

    artifact = captured.value.artifact
    assert artifact.changed_paths == (Path("agent/instructions.md"),)
    assert _git(root, "rev-parse", f"{artifact.head_commit}^") == (
        intent.base_commit
    )
    assert _git(
        root,
        "show",
        f"{artifact.head_commit}:agent/instructions.md",
    ) == "candidate design"
    assert _git(root, "rev-parse", "HEAD") != artifact.head_commit


def test_candidate_capture_combines_committed_and_uncommitted_edits(
    tmp_path: Path,
) -> None:
    root, source, intent, result, request = _capture_fixture(tmp_path)
    _git(root, "commit", "--allow-empty", "-m", "Initial plan")
    source.write_text("committed candidate design\n", encoding="utf-8")
    _git(root, "add", "agent/instructions.md")
    _git(root, "commit", "-m", "Commit candidate design")
    source.write_text("final candidate design\n", encoding="utf-8")
    extra = root / "agent" / "examples.md"
    extra.write_text("new example\n", encoding="utf-8")

    with pytest.raises(
        CandidateDesignPushUnacknowledgedError
    ) as captured:
        _ProductionCandidateDesignRepository(
            SubprocessCommandRunner()
        ).capture(request, intent, result)

    artifact = captured.value.artifact
    assert artifact.changed_paths == (
        Path("agent/examples.md"),
        Path("agent/instructions.md"),
    )
    assert _git(
        root,
        "show",
        f"{artifact.head_commit}:agent/instructions.md",
    ) == "final candidate design"
    assert _git(
        root,
        "show",
        f"{artifact.head_commit}:agent/examples.md",
    ) == "new example"


def test_candidate_capture_accepts_ten_first_parent_commits(
    tmp_path: Path,
) -> None:
    root, source, intent, result, request = _capture_fixture(tmp_path)
    _git(root, "commit", "--allow-empty", "-m", "Initial plan")
    for index in range(1, 10):
        source.write_text(f"candidate design {index}\n", encoding="utf-8")
        _git(root, "add", "agent/instructions.md")
        _git(root, "commit", "-m", f"Candidate edit {index}")

    with pytest.raises(
        CandidateDesignPushUnacknowledgedError
    ) as captured:
        _ProductionCandidateDesignRepository(
            SubprocessCommandRunner()
        ).capture(request, intent, result)

    assert captured.value.artifact.changed_paths == (
        Path("agent/instructions.md"),
    )


def test_candidate_capture_rejects_more_than_ten_commits(
    tmp_path: Path,
) -> None:
    root, source, intent, result, request = _capture_fixture(tmp_path)
    _git(root, "commit", "--allow-empty", "-m", "Initial plan")
    for index in range(1, 11):
        source.write_text(f"candidate design {index}\n", encoding="utf-8")
        _git(root, "add", "agent/instructions.md")
        _git(root, "commit", "-m", f"Candidate edit {index}")

    with pytest.raises(ValueError, match="checkout base changed"):
        _ProductionCandidateDesignRepository(
            SubprocessCommandRunner()
        ).capture(request, intent, result)


def test_candidate_capture_rejects_committed_forbidden_path(
    tmp_path: Path,
) -> None:
    root, source, intent, result, request = _capture_fixture(tmp_path)
    _git(root, "commit", "--allow-empty", "-m", "Initial plan")
    source.write_text("candidate design\n", encoding="utf-8")
    (root / "outside.txt").write_text("forbidden edit\n", encoding="utf-8")
    _git(root, "add", "agent/instructions.md", "outside.txt")
    _git(root, "commit", "-m", "Commit candidate and forbidden edits")

    with pytest.raises(ValueError, match="forbidden paths"):
        _ProductionCandidateDesignRepository(
            SubprocessCommandRunner()
        ).capture(request, intent, result)


@pytest.mark.parametrize("operation", ("delete", "rename"))
def test_candidate_capture_rejects_committed_forbidden_path_removal(
    tmp_path: Path,
    operation: str,
) -> None:
    root, source, intent, result, request = _capture_fixture(tmp_path)
    _git(root, "commit", "--allow-empty", "-m", "Initial plan")
    source.write_text("candidate design\n", encoding="utf-8")
    if operation == "delete":
        (root / "outside.txt").unlink()
    else:
        _git(root, "mv", "outside.txt", "agent/imported.txt")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", f"Candidate {operation}")

    with pytest.raises(ValueError, match="forbidden paths"):
        _ProductionCandidateDesignRepository(
            SubprocessCommandRunner()
        ).capture(request, intent, result)


def test_candidate_capture_rejects_committed_result_file(
    tmp_path: Path,
) -> None:
    root, source, intent, result, request = _capture_fixture(tmp_path)
    intent = replace(intent, edit_paths=(Path("."),))
    _git(root, "commit", "--allow-empty", "-m", "Initial plan")
    source.write_text("candidate design\n", encoding="utf-8")
    _git(
        root,
        "add",
        "agent/instructions.md",
        request.result_file.relative_to(root).as_posix(),
    )
    _git(root, "commit", "-m", "Commit candidate and result")

    with pytest.raises(ValueError, match="forbidden paths"):
        _ProductionCandidateDesignRepository(
            SubprocessCommandRunner()
        ).capture(request, intent, result)


def test_candidate_capture_rejects_dirty_reserved_handoff_file(
    tmp_path: Path,
) -> None:
    root, source, intent, result, request = _capture_fixture(tmp_path)
    intent = replace(intent, edit_paths=(Path("."),))
    _git(root, "commit", "--allow-empty", "-m", "Initial plan")
    source.write_text("candidate design\n", encoding="utf-8")
    handoff = root / ".foundry-optimizer" / "handoffs" / "candidate.json"
    handoff.parent.mkdir(parents=True)
    handoff.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="forbidden paths"):
        _ProductionCandidateDesignRepository(
            SubprocessCommandRunner()
        ).capture(request, intent, result)


@pytest.mark.parametrize("mode", ("120000", "160000"))
def test_candidate_capture_rejects_committed_non_file_entries(
    tmp_path: Path,
    mode: str,
) -> None:
    root, source, intent, result, request = _capture_fixture(tmp_path)
    _git(root, "commit", "--allow-empty", "-m", "Initial plan")
    source.write_text("candidate design\n", encoding="utf-8")
    _git(root, "add", "agent/instructions.md")
    if mode == "120000":
        entry = root / "entry.txt"
        entry.write_text("outside.txt", encoding="utf-8")
        object_id = _git(root, "hash-object", "-w", "entry.txt")
        entry.unlink()
    else:
        object_id = intent.base_commit
    _git(
        root,
        "update-index",
        "--add",
        "--cacheinfo",
        f"{mode},{object_id},agent/non-file",
    )
    _git(root, "commit", "-m", "Commit non-file entry")

    with pytest.raises(ValueError, match="forbidden paths"):
        _ProductionCandidateDesignRepository(
            SubprocessCommandRunner()
        ).capture(request, intent, result)


def test_candidate_capture_rejects_final_content_reverted_to_base(
    tmp_path: Path,
) -> None:
    root, source, intent, result, request = _capture_fixture(tmp_path)
    _git(root, "commit", "--allow-empty", "-m", "Initial plan")
    source.write_text("candidate design\n", encoding="utf-8")
    _git(root, "add", "agent/instructions.md")
    _git(root, "commit", "-m", "Commit candidate design")
    source.write_text("baseline\n", encoding="utf-8")

    with pytest.raises(ValueError, match="forbidden paths"):
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
    (root / "outside.txt").write_text("outside baseline\n", encoding="utf-8")
    _git(root, "add", "agent/instructions.md", "outside.txt")
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


def test_candidate_capture_forces_marker_independent_loopback_handoff(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root, source, intent, result, request = _capture_fixture(tmp_path)
    _git(root, "checkout", "-b", "copilot/foundry-opt-design-candidate-345")
    _git(root, "commit", "--allow-empty", "-m", "Initial plan")
    _git(
        root,
        "remote",
        "add",
        "origin",
        "http://127.0.0.1:49152/microsoft-foundry/luffy-test-agents-repo",
    )
    monkeypatch.setenv(
        "GITHUB_REPOSITORY",
        "microsoft-foundry/luffy-test-agents-repo",
    )
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv(
        "FOUNDRY_OPT_COPILOT_GIT_PROXY",
        raising=False,
    )
    source.write_text("candidate design\n", encoding="utf-8")

    with pytest.raises(CandidateDesignPushUnacknowledgedError):
        _ProductionCandidateDesignRepository(
            SubprocessCommandRunner()
        ).capture(request, intent, result)


@pytest.mark.parametrize(
    "remote_url",
    (
        "http://127.0.0.1:49152/octo-org/optimizer",
        "http://127.1:49152/octo-org/optimizer",
        "http://2130706433:49152/octo-org/optimizer",
        "http://0x7f000001:49152/octo-org/optimizer",
        "http://0177.0.0.1:49152/octo-org/optimizer",
        "https://localhost:49152/octo-org/optimizer",
        "http://local%68ost:49152/octo-org/optimizer",
        "http://127%2e0%2e0%2e1:49152/octo-org/optimizer",
        "http://localhost.:49152/octo-org/optimizer",
        "http://proxy.localhost:49152/octo-org/optimizer",
        "http://[::1]:49152/octo-org/optimizer",
        "http://[::ffff:127.0.0.1]:49152/octo-org/optimizer",
    ),
)
def test_candidate_loopback_handoff_detector_accepts_exact_native_context(
    tmp_path: Path,
    monkeypatch,
    remote_url: str,
) -> None:
    root, _, _, _, _ = _capture_fixture(tmp_path)
    _git(root, "checkout", "-b", "copilot/session")
    _git(root, "remote", "add", "origin", remote_url)
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo-org/optimizer")

    session = candidate_design_loopback_handoff_session(root)

    assert session is not None
    assert session.branch == "copilot/session"
    assert session.remote_url == remote_url


@pytest.mark.parametrize(
    ("remote_url", "branch"),
    (
        ("http://127.0.0.1/octo-org/optimizer", "copilot/session"),
        ("http://127.0.0.1:49152/octo-org/other", "copilot/session"),
        ("http://[::1", "copilot/session"),
        ("http://127.0.0.1:49152/octo-org/optimizer", "main"),
    ),
)
def test_candidate_capture_rejects_invalid_loopback_context(
    tmp_path: Path,
    monkeypatch,
    remote_url: str,
    branch: str,
) -> None:
    root, source, intent, result, request = _capture_fixture(tmp_path)
    if branch != "main":
        _git(root, "checkout", "-b", branch)
    _git(root, "commit", "--allow-empty", "-m", "Initial plan")
    _git(root, "remote", "add", "origin", remote_url)
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo-org/optimizer")
    source.write_text("candidate design\n", encoding="utf-8")

    with pytest.raises(CandidateDesignLoopbackError):
        _ProductionCandidateDesignRepository(
            SubprocessCommandRunner()
        ).capture(request, intent, result)


def test_candidate_capture_directly_pushes_to_normal_git_remote(
    tmp_path: Path,
) -> None:
    root, source, intent, result, request = _capture_fixture(tmp_path)
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare")
    _git(root, "commit", "--allow-empty", "-m", "Initial plan")
    _git(root, "remote", "add", "origin", str(remote))
    source.write_text("candidate design\n", encoding="utf-8")

    artifact = _ProductionCandidateDesignRepository(
        SubprocessCommandRunner()
    ).capture(request, intent, result)

    assert _git(
        root,
        "ls-remote",
        "--heads",
        "origin",
        artifact.ref,
    ).split()[0] == artifact.head_commit


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
