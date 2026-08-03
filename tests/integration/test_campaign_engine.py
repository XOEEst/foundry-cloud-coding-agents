from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from foundry_opt.adapters.campaign_git import CampaignGit
from foundry_opt.campaign.protocols import ActiveCampaignError, PinnedRepository


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
    _run(
        (
            "git",
            f"--git-dir={origin}",
            "symbolic-ref",
            "HEAD",
            "refs/heads/main",
        ),
        tmp_path,
    )
    commit = _run(("git", "rev-parse", "HEAD"), repository).decode().strip()
    return repository, origin, commit


def _advance_remote_main(tmp_path: Path, origin: Path) -> str:
    publisher = tmp_path / "publisher"
    _run(("git", "clone", str(origin), str(publisher)), tmp_path)
    _run(("git", "config", "user.name", "Campaign Publisher"), publisher)
    _run(
        ("git", "config", "user.email", "publisher@example.invalid"),
        publisher,
    )
    (publisher / "remote-advance.md").write_text(
        "advanced\n",
        encoding="utf-8",
    )
    _run(("git", "add", "remote-advance.md"), publisher)
    _run(("git", "commit", "-m", "advance main"), publisher)
    _run(("git", "push", "origin", "main"), publisher)
    return _run(("git", "rev-parse", "HEAD"), publisher).decode().strip()


def test_campaign_git_pins_remote_main_from_copilot_session_branch(
    tmp_path: Path,
) -> None:
    repository, _, base_commit = _repository(tmp_path)
    _run(
        ("git", "checkout", "-b", "copilot/fix-live-campaign"),
        repository,
    )
    (repository / "copilot-plan.md").write_text(
        "initial plan\n",
        encoding="utf-8",
    )
    _run(("git", "add", "copilot-plan.md"), repository)
    _run(("git", "commit", "-m", "Initial plan"), repository)
    session_commit = _run(
        ("git", "rev-parse", "HEAD"),
        repository,
    ).decode().strip()

    pinned = CampaignGit(
        default_branch=lambda _: "main"
    ).pin_default_branch(repository)

    assert pinned.default_branch == "main"
    assert pinned.commit == base_commit
    assert (
        _run(("git", "branch", "--show-current"), repository).decode().strip()
        == "copilot/fix-live-campaign"
    )
    assert (
        _run(("git", "rev-parse", "HEAD"), repository).decode().strip()
        == session_commit
    )
    assert _run(("git", "status", "--porcelain"), repository) == b""


def test_campaign_git_ignores_stale_local_origin_head(
    tmp_path: Path,
) -> None:
    repository, _, base_commit = _repository(tmp_path)
    _run(("git", "checkout", "-b", "copilot/stale-head"), repository)
    (repository / "copilot-plan.md").write_text(
        "initial plan\n",
        encoding="utf-8",
    )
    _run(("git", "add", "copilot-plan.md"), repository)
    _run(("git", "commit", "-m", "Initial plan"), repository)
    session_commit = _run(
        ("git", "rev-parse", "HEAD"),
        repository,
    ).decode().strip()
    _run(
        (
            "git",
            "update-ref",
            "refs/remotes/origin/copilot/stale-head",
            session_commit,
        ),
        repository,
    )
    _run(
        (
            "git",
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/copilot/stale-head",
        ),
        repository,
    )

    pinned = CampaignGit().pin_default_branch(repository)

    assert pinned == PinnedRepository("main", base_commit)


def test_campaign_git_rejects_remote_main_advancing_while_pinning(
    tmp_path: Path,
) -> None:
    repository, origin, _ = _repository(tmp_path)
    resolutions = 0

    def current_default_branch(_: Path) -> str:
        nonlocal resolutions
        resolutions += 1
        if resolutions == 2:
            _advance_remote_main(tmp_path, origin)
        return "main"

    with pytest.raises(ValueError, match="advanced"):
        CampaignGit(
            default_branch=current_default_branch
        ).pin_default_branch(repository)


def test_campaign_git_pins_advanced_remote_main_without_tracking_ref(
    tmp_path: Path,
) -> None:
    repository, origin, stale_tracking_commit = _repository(tmp_path)
    advanced_commit = _advance_remote_main(tmp_path, origin)

    pinned = CampaignGit().pin_default_branch(repository)

    assert pinned == PinnedRepository("main", advanced_commit)
    assert advanced_commit != stale_tracking_commit
    assert (
        _run(
            ("git", "rev-parse", "refs/remotes/origin/main^{commit}"),
            repository,
        )
        .decode()
        .strip()
        == stale_tracking_commit
    )


def test_campaign_git_rejects_missing_remote_head_despite_local_fallback(
    tmp_path: Path,
) -> None:
    repository, origin, _ = _repository(tmp_path)
    _run(
        (
            "git",
            f"--git-dir={origin}",
            "symbolic-ref",
            "HEAD",
            "refs/heads/missing",
        ),
        tmp_path,
    )
    _run(
        (
            "git",
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/main",
        ),
        repository,
    )

    with pytest.raises(ValueError, match="unavailable"):
        CampaignGit().pin_default_branch(repository)


def test_campaign_git_rejects_non_commit_remote_default_tip(
    tmp_path: Path,
) -> None:
    repository, origin, _ = _repository(tmp_path)
    blob = _run(
        (
            "git",
            f"--git-dir={origin}",
            "hash-object",
            "-w",
            "--stdin",
        ),
        tmp_path,
        input_bytes=b"malicious branch target",
    ).decode().strip()
    (origin / "refs" / "heads" / "main").write_text(
        f"{blob}\n",
        encoding="ascii",
    )

    with pytest.raises((RuntimeError, ValueError)):
        CampaignGit().pin_default_branch(repository)


def test_campaign_git_rejects_remote_head_changing_while_pinning(
    tmp_path: Path,
) -> None:
    repository, origin, _ = _repository(tmp_path)
    publisher = tmp_path / "publisher"
    _run(("git", "clone", str(origin), str(publisher)), tmp_path)
    _run(("git", "checkout", "-b", "trunk"), publisher)
    _run(("git", "push", "origin", "trunk"), publisher)
    resolutions = 0

    def current_default_branch(_: Path) -> str:
        nonlocal resolutions
        resolutions += 1
        if resolutions == 2:
            _run(
                (
                    "git",
                    f"--git-dir={origin}",
                    "symbolic-ref",
                    "HEAD",
                    "refs/heads/trunk",
                ),
                tmp_path,
            )
            return "trunk"
        return "main"

    with pytest.raises(ValueError, match="changed"):
        CampaignGit(
            default_branch=current_default_branch
        ).pin_default_branch(repository)


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


def test_campaign_git_requires_clean_repository(
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
