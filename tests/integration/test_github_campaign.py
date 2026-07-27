from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from foundry_opt.adapters.commands import SubprocessCommandRunner
from foundry_opt.adapters.github_campaign import GitExactPatchApplier
from foundry_opt.github_workflow import (
    ExactPatchRequest,
    PatchTraversalError,
)


def _run(
    runner: SubprocessCommandRunner,
    repository: Path,
    *arguments: str,
) -> str:
    return runner.run(("git", *arguments), cwd=repository).stdout


def _repository(tmp_path: Path) -> tuple[
    Path,
    str,
    Path,
    bytes,
    str,
]:
    repository = tmp_path / "repository"
    repository.mkdir()
    runner = SubprocessCommandRunner()
    _run(runner, repository, "init", "--quiet")
    _run(runner, repository, "config", "user.name", "Foundry Test")
    _run(
        runner,
        repository,
        "config",
        "user.email",
        "foundry-test@example.invalid",
    )
    (repository / ".gitignore").write_text(
        ".foundry-optimizer/\n",
        encoding="utf-8",
    )
    (repository / "agent.py").write_text("VALUE = 1\n", encoding="utf-8")
    original_binary = bytes(range(64))
    (repository / "asset.bin").write_bytes(original_binary)
    _run(runner, repository, "add", ".")
    _run(runner, repository, "commit", "--quiet", "-m", "base")
    base = _run(runner, repository, "rev-parse", "HEAD").strip()

    (repository / "agent.py").write_text("VALUE = 2\n", encoding="utf-8")
    expected_binary = b"\x00\xffcandidate\x00binary\xfe"
    (repository / "asset.bin").write_bytes(expected_binary)
    _run(runner, repository, "add", "agent.py", "asset.bin")
    expected_tree = _run(runner, repository, "write-tree").strip()
    patch = _run(
        runner,
        repository,
        "diff",
        "--binary",
        "--full-index",
        base,
    ).encode("utf-8")
    _run(runner, repository, "reset", "--hard", "--quiet", base)
    patch_path = Path(
        ".foundry-optimizer/campaigns/c1/candidate-1.patch"
    )
    destination = repository / patch_path
    destination.parent.mkdir(parents=True)
    destination.write_bytes(patch)
    return repository, base, patch_path, expected_binary, expected_tree


def test_git_patch_applier_applies_text_and_binary_patch_exactly(
    tmp_path: Path,
) -> None:
    repository, base, patch_path, expected_binary, expected_tree = _repository(
        tmp_path
    )
    patch_sha = hashlib.sha256((repository / patch_path).read_bytes()).hexdigest()
    applier = GitExactPatchApplier(SubprocessCommandRunner())

    applied = applier.apply_exact(
        ExactPatchRequest(
            repository_root=repository,
            base_commit=base,
            patch_path=patch_path,
            expected_patch_sha256=patch_sha,
            expected_tree_sha=expected_tree,
            branch="foundry-opt/campaign-1/candidate-1/session-1",
            commit_message="Apply exact candidate",
        )
    )

    assert applied.exact is True
    assert applied.substantive_repair is False
    assert (repository / "agent.py").read_text(encoding="utf-8") == (
        "VALUE = 2\n"
    )
    assert (repository / "asset.bin").read_bytes() == expected_binary
    assert {
        path.as_posix() for path in applied.changed_paths
    } == {"agent.py", "asset.bin"}


def test_git_patch_applier_rejects_patch_path_traversal_before_branching(
    tmp_path: Path,
) -> None:
    repository, base, patch_path, _, expected_tree = _repository(tmp_path)
    malicious = (
        "diff --git a/../outside.txt b/../outside.txt\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/../outside.txt\n"
        "@@ -0,0 +1 @@\n"
        "+escaped\n"
    ).encode()
    (repository / patch_path).write_bytes(malicious)
    outside = repository.parent / "outside.txt"
    applier = GitExactPatchApplier(SubprocessCommandRunner())

    with pytest.raises(PatchTraversalError):
        applier.apply_exact(
            ExactPatchRequest(
                repository_root=repository,
                base_commit=base,
                patch_path=patch_path,
                expected_patch_sha256=hashlib.sha256(malicious).hexdigest(),
                expected_tree_sha=expected_tree,
                branch="foundry-opt/campaign-1/malicious/session-1",
                commit_message="Reject traversal",
            )
        )

    assert outside.exists() is False
    current_branch = _run(
        SubprocessCommandRunner(),
        repository,
        "branch",
        "--show-current",
    ).strip()
    assert current_branch not in {
        "foundry-opt/campaign-1/malicious/session-1"
    }


def test_git_patch_applier_reuses_exact_local_branch_after_restore(
    tmp_path: Path,
) -> None:
    repository, base, patch_path, _, expected_tree = _repository(tmp_path)
    request = ExactPatchRequest(
        repository_root=repository,
        base_commit=base,
        patch_path=patch_path,
        expected_patch_sha256=hashlib.sha256(
            (repository / patch_path).read_bytes()
        ).hexdigest(),
        expected_tree_sha=expected_tree,
        branch="foundry-opt/campaign-1/candidate-1/session-1",
        commit_message="Apply exact candidate",
    )
    applier = GitExactPatchApplier(SubprocessCommandRunner())
    base_branch = _run(
        SubprocessCommandRunner(),
        repository,
        "branch",
        "--show-current",
    ).strip()
    first = applier.apply_exact(request)
    applier.restore_after_publication_failure(
        repository,
        base,
        base_branch,
    )

    second = applier.apply_exact(request)

    assert second.commit_sha == first.commit_sha
    assert second.tree_sha == expected_tree
    assert (
        _run(
            SubprocessCommandRunner(),
            repository,
            "branch",
            "--show-current",
        ).strip()
        == base_branch
    )
