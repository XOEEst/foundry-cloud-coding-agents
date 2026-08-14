from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import subprocess

import pytest

from foundry_opt.adapters.commands import SubprocessCommandRunner
from foundry_opt.orchestration import (
    CandidateExperimentRequest,
    CandidateExperimentResult,
    GitWorkspaceExactBranchPublisher,
    WorkspaceCandidate,
    WorkspaceCandidateProvenance,
    WorkspacePullRequest,
)


_BRANCH = "foundry-opt/workspace/issue-31"
_COMMIT_ENVIRONMENT = {
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
    "GIT_AUTHOR_EMAIL": "foundry-opt@example.invalid",
    "GIT_AUTHOR_NAME": "Foundry Optimizer Workspace",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
    "GIT_COMMITTER_EMAIL": "foundry-opt@example.invalid",
    "GIT_COMMITTER_NAME": "Foundry Optimizer Workspace",
}
_COAUTHOR = (
    "Co-authored-by: GitHub Copilot "
    "<198982749+Copilot@users.noreply.github.com>"
)


@dataclass(frozen=True)
class ExactRepository:
    root: Path
    base: str
    candidate: WorkspaceCandidate
    pull_request: WorkspacePullRequest


def _git(
    root: Path,
    *arguments: str,
    input_bytes: bytes | None = None,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ("git", *arguments),
        cwd=root,
        check=True,
        capture_output=True,
        input=input_bytes,
        env=(
            {**os.environ, **environment}
            if environment is not None
            else None
        ),
    )


def _repository(tmp_path: Path) -> ExactRepository:
    origin = tmp_path / "origin.git"
    root = tmp_path / "repository"
    subprocess.run(
        ("git", "init", "--bare", str(origin)),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "clone", str(origin), str(root)),
        check=True,
        capture_output=True,
    )
    _git(root, "config", "user.name", "Workspace Test")
    _git(
        root,
        "config",
        "user.email",
        "workspace@example.invalid",
    )
    (root / "agent.py").write_text("baseline\n", encoding="utf-8")
    _git(root, "add", "agent.py")
    _git(root, "commit", "-m", "base")
    base = _git(root, "rev-parse", "HEAD").stdout.decode().strip()
    _git(root, "branch", "-M", "main")
    _git(root, "push", "-u", "origin", "main")
    _git(root, "switch", "-c", _BRANCH)
    _git(root, "commit", "--allow-empty", "-m", "workspace")
    _git(root, "push", "origin", _BRANCH)
    _git(root, "switch", "main")

    (root / "agent.py").write_text("candidate-2\n", encoding="utf-8")
    patch = _git(root, "diff", "--binary").stdout
    _git(root, "add", "agent.py")
    expected_tree = _git(root, "write-tree").stdout.decode().strip()
    _git(root, "reset", "--hard", base)
    experiment = CandidateExperimentRequest(
        issue_number=31,
        candidate_id="candidate-2",
        patch_sha256=hashlib.sha256(patch).hexdigest(),
        bundle_sha256="5" * 64,
        evidence_sha256="7" * 64,
        idempotency_key="a" * 64,
    )
    candidate = WorkspaceCandidate(
        experiment=experiment,
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
    return ExactRepository(
        root=root,
        base=base,
        candidate=candidate,
        pull_request=WorkspacePullRequest(
            number=104,
            issue_number=31,
            branch=_BRANCH,
            title="[Optimize] #31 workspace",
            draft=True,
            reuse_existing=True,
            base_commit=base,
        ),
    )


def _provenance(
    *,
    source_sha: str = "9" * 40,
    comment_id: int = 501,
) -> WorkspaceCandidateProvenance:
    return WorkspaceCandidateProvenance(
        copilot_actor_id=198982749,
        copilot_actor_login="Copilot",
        candidate_source_commit_sha=source_sha,
        candidate_source_commit_url=(
            "https://github.com/octo-org/optimizer/commit/" + source_sha
        ),
        acknowledgement_comment_id=comment_id,
        acknowledgement_comment_url=(
            "https://github.com/octo-org/optimizer/pull/"
            f"104#issuecomment-{comment_id}"
        ),
        assignment_marker_key="issue-31:assignment-a1:v1",
        workspace_pr_number=104,
        importer_workflow_run_id=9001,
        importer_workflow_run_url=(
            "https://github.com/octo-org/optimizer/actions/runs/9001"
        ),
        trusted_event_name="issue_comment",
    )


def _message(root: Path, commit: str) -> str:
    raw = _git(root, "cat-file", "commit", commit).stdout.decode()
    return raw.split("\n\n", 1)[1]


def _force_branch(
    repository: ExactRepository,
    message: str,
) -> str:
    commit = _git(
        repository.root,
        "commit-tree",
        repository.candidate.expected_tree,
        "-p",
        repository.base,
        "-F",
        "-",
        input_bytes=message.encode(),
        environment=_COMMIT_ENVIRONMENT,
    ).stdout.decode().strip()
    _git(
        repository.root,
        "push",
        "--force",
        "origin",
        f"{commit}:refs/heads/{_BRANCH}",
    )
    return commit


def test_replay_reads_existing_legacy_exact_commit(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    legacy = _git(
        repository.root,
        "commit-tree",
        repository.candidate.expected_tree,
        "-p",
        repository.base,
        "-m",
        "Apply selected optimization candidate for issue-31",
        environment=_COMMIT_ENVIRONMENT,
    ).stdout.decode().strip()
    _git(
        repository.root,
        "push",
        "--force",
        "origin",
        f"{legacy}:refs/heads/{_BRANCH}",
    )

    result = GitWorkspaceExactBranchPublisher(
        SubprocessCommandRunner()
    ).publish(
        repository.root,
        repository.pull_request,
        repository.candidate,
    )

    assert result.commit_sha == legacy
    assert result.tree_sha == repository.candidate.expected_tree


def test_exact_commit_preserves_tree_and_binds_provenance(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    publisher = GitWorkspaceExactBranchPublisher(
        SubprocessCommandRunner()
    )

    legacy = publisher.publish(
        repository.root,
        repository.pull_request,
        repository.candidate,
    )
    provenance = _provenance()
    attributed = publisher.publish(
        repository.root,
        repository.pull_request,
        repository.candidate,
        provenance,
    )
    replay = publisher.publish(
        repository.root,
        repository.pull_request,
        repository.candidate,
        provenance,
    )
    changed = publisher.publish(
        repository.root,
        repository.pull_request,
        repository.candidate,
        _provenance(source_sha="8" * 40),
    )

    assert {
        legacy.tree_sha,
        attributed.tree_sha,
        replay.tree_sha,
        changed.tree_sha,
    } == {repository.candidate.expected_tree}
    assert legacy.commit_sha != attributed.commit_sha
    assert replay.commit_sha == attributed.commit_sha
    assert changed.commit_sha != attributed.commit_sha
    assert _message(repository.root, legacy.commit_sha) == (
        "Apply selected optimization candidate for issue-31\n"
    )
    assert _COAUTHOR not in _message(
        repository.root,
        legacy.commit_sha,
    )


def test_attributed_commit_message_is_exact(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    provenance = _provenance()
    result = GitWorkspaceExactBranchPublisher(
        SubprocessCommandRunner()
    ).publish(
        repository.root,
        repository.pull_request,
        repository.candidate,
        provenance,
    )

    assert _message(repository.root, result.commit_sha) == (
        "Apply selected optimization candidate for issue-31\n"
        "\n"
        "Selected candidate ID: candidate-2\n"
        f"Copilot source commit SHA: {'9' * 40}\n"
        "Copilot source commit URL: "
        f"https://github.com/octo-org/optimizer/commit/{'9' * 40}\n"
        "Copilot acknowledgement URL: "
        "https://github.com/octo-org/optimizer/pull/"
        "104#issuecomment-501\n"
        f"Provenance SHA-256: {provenance.identity_sha256}\n"
        "\n"
        f"{_COAUTHOR}\n"
    )


def test_replay_rejects_tree_only_and_mismatched_message(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    provenance = _provenance()
    publisher = GitWorkspaceExactBranchPublisher(
        SubprocessCommandRunner()
    )
    expected = publisher.publish(
        repository.root,
        repository.pull_request,
        repository.candidate,
        provenance,
    )

    tree_only = _force_branch(
        repository,
        "Apply selected optimization candidate for issue-31",
    )
    normalized_tree_only = publisher.publish(
        repository.root,
        repository.pull_request,
        repository.candidate,
        provenance,
    )
    mismatched = _force_branch(
        repository,
        _message(repository.root, expected.commit_sha).replace(
            "Provenance SHA-256:",
            "Altered provenance SHA-256:",
        ),
    )
    normalized_mismatch = publisher.publish(
        repository.root,
        repository.pull_request,
        repository.candidate,
        provenance,
    )

    assert tree_only != expected.commit_sha
    assert mismatched != expected.commit_sha
    assert normalized_tree_only.commit_sha == expected.commit_sha
    assert normalized_mismatch.commit_sha == expected.commit_sha
    assert normalized_tree_only.tree_sha == repository.candidate.expected_tree
    assert normalized_mismatch.tree_sha == repository.candidate.expected_tree


def test_spoofed_provenance_cannot_add_trailer(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(
        ValueError,
        match="workspace candidate provenance is invalid",
    ):
        GitWorkspaceExactBranchPublisher(
            SubprocessCommandRunner()
        ).publish(
            repository.root,
            repository.pull_request,
            repository.candidate,
            object(),
        )

    with pytest.raises(
        ValueError,
        match="workspace candidate provenance is invalid",
    ):
        GitWorkspaceExactBranchPublisher(
            SubprocessCommandRunner()
        ).publish(
            repository.root,
            repository.pull_request,
            repository.candidate,
            replace(
                _provenance(),
                workspace_pr_number=105,
                acknowledgement_comment_url=(
                    "https://github.com/octo-org/optimizer/pull/"
                    "105#issuecomment-501"
                ),
            ),
        )
