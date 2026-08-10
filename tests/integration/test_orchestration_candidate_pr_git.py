from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

from foundry_opt.orchestration.candidate_bridge import (
    GhCandidatePullRequestReader,
)
from foundry_opt.orchestration.candidate_slate import (
    CandidateBinding,
    CandidatePullRequestState,
    CandidatePullRequestReference,
    CandidateSelectionRequest,
    CandidatePullRequestVerificationStatus,
    candidate_pr_body,
    candidate_pr_marker,
    verify_candidate_pull_request,
)
from foundry_opt.preflight.interfaces import CommandResult


def _run(arguments: tuple[str, ...], cwd: Path) -> bytes:
    return subprocess.run(
        arguments,
        cwd=cwd,
        check=True,
        capture_output=True,
    ).stdout


def _repository(tmp_path: Path) -> tuple[Path, str, str, str, bytes]:
    origin = tmp_path / "origin.git"
    repository = tmp_path / "repository"
    _run(("git", "init", "--bare", str(origin)), tmp_path)
    _run(("git", "init", "-b", "main", str(repository)), tmp_path)
    _run(("git", "config", "user.name", "PR Test"), repository)
    _run(
        ("git", "config", "user.email", "pr@example.invalid"),
        repository,
    )
    source = repository / "agent" / "instructions.md"
    source.parent.mkdir()
    source.write_text("baseline\n", encoding="utf-8")
    _run(("git", "add", "."), repository)
    _run(("git", "commit", "-m", "baseline"), repository)
    base = _run(("git", "rev-parse", "HEAD"), repository).decode().strip()
    _run(("git", "remote", "add", "origin", str(origin)), repository)
    _run(("git", "push", "-u", "origin", "main"), repository)
    _run(("git", "switch", "-c", "candidate"), repository)
    source.write_text("candidate\n", encoding="utf-8")
    _run(("git", "add", "."), repository)
    _run(("git", "commit", "-m", "candidate"), repository)
    head = _run(("git", "rev-parse", "HEAD"), repository).decode().strip()
    tree = _run(
        ("git", "rev-parse", "HEAD^{tree}"),
        repository,
    ).decode().strip()
    patch = _run(
        ("git", "diff", "--binary", "--full-index", base, head, "--"),
        repository,
    )
    _run(
        (
            "git",
            "push",
            "origin",
            f"{head}:refs/pull/91/head",
        ),
        repository,
    )
    _run(("git", "switch", "main"), repository)
    return repository, base, head, tree, patch


class GithubCommands:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        arguments,
        *,
        cwd: Path | None = None,
        environment=None,
        input_text: str | None = None,
        input_bytes: bytes | None = None,
    ) -> CommandResult:
        self.calls.append(tuple(arguments))
        return CommandResult(0, self.responses.pop(0), "")


def test_native_candidate_pr_reader_uses_exact_git_patch_and_tree(
    tmp_path: Path,
) -> None:
    repository, base, head, tree, patch = _repository(tmp_path)
    binding = CandidateBinding(
        issue_number=31,
        generation=2,
        spec_sha256="a" * 64,
        base_commit=base,
        candidate_id="candidate-1",
        draft_id="draft-candidate-1",
        evidence_sha256="c" * 64,
        patch_sha256=hashlib.sha256(patch).hexdigest(),
        bundle_sha256="d" * 64,
        tree_sha=tree,
        allowed_paths=(Path("agent"),),
        changed_paths=(Path("agent/instructions.md"),),
    )
    body = candidate_pr_body(
        binding,
        worker_issue_number=84,
        required_checks=("exact-candidate", "tests"),
    )
    commands = GithubCommands(
        [
            json.dumps({"defaultBranchRef": {"name": "main"}}),
            json.dumps(
                [
                    {
                        "author": {"login": "copilot-swe-agent[bot]"},
                        "baseRefName": "main",
                        "body": body,
                        "headRefOid": head,
                        "isDraft": False,
                        "mergeCommit": None,
                        "number": 91,
                        "state": "OPEN",
                    }
                ]
            ),
            json.dumps(
                [
                    {"bucket": "pass", "name": "exact-candidate"},
                    {"bucket": "pass", "name": "tests"},
                ]
            ),
        ]
    )

    snapshots = GhCandidatePullRequestReader(
        commands,
        repository,
        "octo-org/optimizer",
    ).snapshots_for(
        CandidateSelectionRequest(
            repository,
            31,
            expected_default_branch="main",
            required_checks=("exact-candidate", "tests"),
        ),
        (binding,),
    )

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.state is CandidatePullRequestState.OPEN
    assert snapshot.head_parent_commit == base
    assert snapshot.head_tree_sha == tree
    assert snapshot.patch_sha256 == binding.patch_sha256
    assert snapshot.changed_paths == binding.changed_paths
    assert snapshot.marker == candidate_pr_marker(binding)
    assert commands.calls[0] == (
        "gh",
        "repo",
        "view",
        "octo-org/optimizer",
        "--json",
        "defaultBranchRef",
    )


def test_merged_candidate_reader_accepts_exact_merge_on_default_branch(
    tmp_path: Path,
) -> None:
    repository, base, head, tree, patch = _repository(tmp_path)
    _run(("git", "merge", "--no-ff", "candidate", "-m", "merge"), repository)
    merge = _run(("git", "rev-parse", "HEAD"), repository).decode().strip()
    _run(("git", "push", "origin", "main"), repository)
    binding = CandidateBinding(
        issue_number=31,
        generation=2,
        spec_sha256="a" * 64,
        base_commit=base,
        candidate_id="candidate-1",
        draft_id="draft-candidate-1",
        evidence_sha256="c" * 64,
        patch_sha256=hashlib.sha256(patch).hexdigest(),
        bundle_sha256="d" * 64,
        tree_sha=tree,
        allowed_paths=(Path("agent"),),
        changed_paths=(Path("agent/instructions.md"),),
    )
    body = candidate_pr_body(
        binding,
        worker_issue_number=84,
        required_checks=("exact-candidate", "tests"),
    )
    commands = GithubCommands(
        [
            json.dumps({"defaultBranchRef": {"name": "main"}}),
            json.dumps(
                [
                    {
                        "author": {"login": "copilot-swe-agent[bot]"},
                        "baseRefName": "main",
                        "body": body,
                        "headRefOid": head,
                        "isDraft": False,
                        "mergeCommit": {"oid": merge},
                        "number": 91,
                        "state": "MERGED",
                    }
                ]
            ),
            json.dumps(
                [
                    {"bucket": "pass", "name": "exact-candidate"},
                    {"bucket": "pass", "name": "tests"},
                ]
            ),
        ]
    )
    request = CandidateSelectionRequest(
        repository,
        31,
        expected_default_branch="main",
        required_checks=("exact-candidate", "tests"),
    )

    snapshot = GhCandidatePullRequestReader(
        commands,
        repository,
        "octo-org/optimizer",
    ).snapshots_for(request, (binding,))[0]
    verification = verify_candidate_pull_request(
        binding,
        snapshot,
        expected_default_branch="main",
        required_checks=request.required_checks,
    )

    assert snapshot.current_default_commit == merge
    assert snapshot.merge_parent_commit == base
    assert snapshot.merge_tree_sha == tree
    assert snapshot.merge_reachable_from_default is True
    assert verification.status is CandidatePullRequestVerificationStatus.VERIFIED


def test_observed_edited_pr_is_read_even_after_marker_removal(
    tmp_path: Path,
) -> None:
    repository, base, head, tree, patch = _repository(tmp_path)
    binding = CandidateBinding(
        issue_number=31,
        generation=2,
        spec_sha256="a" * 64,
        base_commit=base,
        candidate_id="candidate-1",
        draft_id="draft-candidate-1",
        evidence_sha256="c" * 64,
        patch_sha256=hashlib.sha256(patch).hexdigest(),
        bundle_sha256="d" * 64,
        tree_sha=tree,
        allowed_paths=(Path("agent"),),
        changed_paths=(Path("agent/instructions.md"),),
    )
    commands = GithubCommands(
        [
            json.dumps({"defaultBranchRef": {"name": "main"}}),
            "[]",
            json.dumps(
                {
                    "author": {"login": "copilot-swe-agent[bot]"},
                    "baseRefName": "main",
                    "body": "marker removed",
                    "headRefOid": head,
                    "isDraft": False,
                    "mergeCommit": None,
                    "number": 91,
                    "state": "OPEN",
                }
            ),
            json.dumps(
                [
                    {"bucket": "pass", "name": "exact-candidate"},
                    {"bucket": "pass", "name": "tests"},
                ]
            ),
        ]
    )
    request = CandidateSelectionRequest(
        repository,
        31,
        expected_default_branch="main",
        required_checks=("exact-candidate", "tests"),
        observed_pull_requests=(
            CandidatePullRequestReference(
                91,
                binding.binding_sha256,
                84,
            ),
        ),
    )

    snapshot = GhCandidatePullRequestReader(
        commands,
        repository,
        "octo-org/optimizer",
    ).snapshots_for(request, (binding,))[0]
    verification = verify_candidate_pull_request(
        binding,
        snapshot,
        expected_default_branch="main",
        required_checks=request.required_checks,
    )

    assert snapshot.pull_request_number == 91
    assert verification.reason == "candidate_body_mismatch"
