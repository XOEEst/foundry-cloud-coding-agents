from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from foundry_opt.adapters.campaign_git import CampaignGit
from foundry_opt.campaign.protocols import ActiveCampaignError


def _run(
    arguments: tuple[str, ...],
    cwd: Path,
    *,
    input_bytes: bytes | None = None,
) -> bytes:
    return subprocess.run(
        arguments,
        cwd=cwd,
        input=input_bytes,
        check=True,
        capture_output=True,
    ).stdout


def _repository(tmp_path: Path) -> tuple[Path, Path, str]:
    origin = tmp_path / "origin.git"
    repository = tmp_path / "repository"
    _run(("git", "init", "--bare", str(origin)), tmp_path)
    _run(("git", "init", "-b", "main", str(repository)), tmp_path)
    _run(("git", "config", "user.name", "Campaign Test"), repository)
    _run(("git", "config", "user.email", "campaign@example.invalid"), repository)
    (repository / "agent").mkdir()
    (repository / "agent" / "instructions.md").write_text(
        "baseline\n",
        encoding="utf-8",
    )
    (repository / "agent" / "payload.bin").write_bytes(b"\x00baseline\xff")
    _run(("git", "add", "."), repository)
    _run(("git", "commit", "-m", "baseline"), repository)
    _run(("git", "remote", "add", "origin", str(origin)), repository)
    _run(("git", "push", "-u", "origin", "main"), repository)
    commit = _run(("git", "rev-parse", "HEAD"), repository).decode().strip()
    return repository, origin, commit


def test_campaign_git_exports_replayable_binary_patch_from_exact_base(
    tmp_path: Path,
) -> None:
    repository, origin, base_commit = _repository(tmp_path)
    git = CampaignGit(default_branch=lambda _: "main")

    pinned = git.pin_default_branch(repository)
    candidate = git.create_worktree(
        repository,
        "campaign-1",
        "candidate-1",
        pinned.commit,
    )
    (candidate.path / "agent" / "payload.bin").write_bytes(
        b"\x00changed\x10\x11\xff"
    )
    (candidate.path / "agent" / "new.txt").write_text("new\n", encoding="utf-8")

    assert pinned.commit == base_commit
    assert set(git.changed_paths(candidate)) == {
        Path("agent/new.txt"),
        Path("agent/payload.bin"),
    }

    result_commit = git.commit_worktree(candidate, "candidate")
    patch = git.export_patch(
        repository,
        "campaign-1",
        candidate,
        result_commit,
    )
    patch_bytes = (repository / patch.path).read_bytes()

    assert b"GIT binary patch" in patch_bytes
    assert patch.sha256 == hashlib.sha256(patch_bytes).hexdigest()
    assert patch.base_commit == base_commit
    assert patch.result_commit == result_commit

    replay = git.create_worktree(
        repository,
        "campaign-1",
        "replay",
        base_commit,
    )
    _run(
        ("git", "apply", "--binary", str(repository / patch.path)),
        replay.path,
    )
    _run(("git", "add", "-A"), replay.path)
    replay_tree = _run(("git", "write-tree"), replay.path).decode().strip()
    result_tree = _run(
        ("git", "rev-parse", f"{result_commit}^{{tree}}"),
        repository,
    ).decode().strip()
    assert replay_tree == result_tree
    assert (
        git.export_patch(
            repository,
            "campaign-1",
            candidate,
            result_commit,
        )
        == patch
    )
    (repository / patch.path).write_bytes(b"tampered")
    with pytest.raises(ValueError):
        git.export_patch(
            repository,
            "campaign-1",
            candidate,
            result_commit,
        )

    git.cleanup_worktree(repository, candidate)
    git.cleanup_worktree(repository, replay)
    worktrees = _run(("git", "worktree", "list", "--porcelain"), repository)
    assert str(candidate.path).encode() not in worktrees
    assert str(replay.path).encode() not in worktrees
    assert (
        _run(
            (
                "git",
                f"--git-dir={origin}",
                "for-each-ref",
                "--format=%(refname)",
                "refs/heads",
            ),
            tmp_path,
        )
        == b"refs/heads/main\n"
    )


def test_campaign_git_recovers_only_stale_target_lock(tmp_path: Path) -> None:
    repository, _, base_commit = _repository(tmp_path)
    git = CampaignGit(default_branch=lambda _: "main")
    now = datetime(2026, 7, 26, 12, tzinfo=UTC)
    git.acquire_lock(
        repository_root=repository,
        target="agent",
        campaign_id="campaign-1",
        base_commit=base_commit,
        now=now,
        stale_after=timedelta(hours=2),
    )

    with pytest.raises(ActiveCampaignError):
        git.acquire_lock(
            repository_root=repository,
            target="agent",
            campaign_id="campaign-2",
            base_commit=base_commit,
            now=now + timedelta(minutes=119),
            stale_after=timedelta(hours=2),
        )

    lock_path = (
        repository
        / ".foundry-optimizer"
        / "campaigns"
        / ".locks"
        / "agent.json"
    )
    metadata = json.loads(lock_path.read_text(encoding="utf-8"))
    assert set(metadata) == {
        "base_commit",
        "campaign_id",
        "created_at",
        "target",
    }

    recovered = git.acquire_lock(
        repository_root=repository,
        target="agent",
        campaign_id="campaign-2",
        base_commit=base_commit,
        now=now + timedelta(hours=2),
        stale_after=timedelta(hours=2),
    )
    assert recovered.recovered_campaign_id == "campaign-1"


def test_campaign_git_requires_clean_exact_default_branch_head(
    tmp_path: Path,
) -> None:
    repository, _, _ = _repository(tmp_path)
    git = CampaignGit(default_branch=lambda _: "main")
    (repository / "agent" / "instructions.md").write_text(
        "dirty\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="clean"):
        git.pin_default_branch(repository)

    _run(("git", "checkout", "--", "."), repository)
    (repository / "agent" / "local.md").write_text("local\n", encoding="utf-8")
    _run(("git", "add", "."), repository)
    _run(("git", "commit", "-m", "local-only"), repository)
    with pytest.raises(ValueError, match="exact GitHub default-branch"):
        git.pin_default_branch(repository)
