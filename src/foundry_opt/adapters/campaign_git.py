from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from foundry_opt.campaign.models import PatchArtifact
from foundry_opt.campaign.protocols import (
    ActiveCampaignError,
    CampaignLock,
    CampaignWorktree,
    PinnedRepository,
)
from foundry_opt.campaign.worktrees import (
    contained_worktree_root,
    require_managed_worktree,
)


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


class CampaignGit:
    def __init__(
        self,
        *,
        default_branch: Callable[[Path], str] | None = None,
    ) -> None:
        self._default_branch = default_branch or _github_default_branch
        self._worktrees: dict[Path, CampaignWorktree] = {}

    def pin_default_branch(self, repository_root: Path) -> PinnedRepository:
        root = _repository_root(repository_root)
        status = self._git(
            root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            ".",
            ":(exclude).foundry-optimizer",
        )
        if status:
            raise ValueError("campaign repository must be clean")
        branch = self._default_branch(root).strip()
        _validate_branch(root, branch)
        self._git(
            root,
            "fetch",
            "--quiet",
            "origin",
            f"{branch}:refs/remotes/origin/{branch}",
        )
        commit = self._git_text(
            root,
            "rev-parse",
            f"refs/remotes/origin/{branch}^{{commit}}",
        )
        head = self._git_text(root, "rev-parse", "HEAD^{commit}")
        if head != commit:
            raise ValueError(
                "campaign must start from the exact GitHub default-branch commit"
            )
        return PinnedRepository(branch, commit)

    def acquire_lock(
        self,
        *,
        repository_root: Path,
        target: str,
        campaign_id: str,
        base_commit: str,
        now: datetime,
        stale_after: timedelta,
    ) -> CampaignLock:
        root = _repository_root(repository_root)
        _identifier(target, "target")
        _identifier(campaign_id, "campaign_id")
        _commit(base_commit)
        if stale_after < timedelta(hours=2):
            raise ValueError("stale_after must be at least two hours")
        if now.tzinfo is None:
            raise ValueError("lock timestamps must be timezone-aware")
        path = _lock_path(root, target)
        path.parent.mkdir(parents=True, exist_ok=True)
        recovered: str | None = None
        try:
            existing = _read_lock(path)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            created = datetime.fromisoformat(existing["created_at"])
            if now.astimezone(UTC) - created.astimezone(UTC) < stale_after:
                raise ActiveCampaignError(
                    f"target {target!r} already has an active campaign"
                )
            recovered = existing.get("campaign_id")
            path.unlink()
        payload = {
            "base_commit": base_commit,
            "campaign_id": campaign_id,
            "created_at": now.astimezone(UTC).isoformat(),
            "target": target,
        }
        try:
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
        except FileExistsError as error:
            raise ActiveCampaignError(
                f"target {target!r} already has an active campaign"
            ) from error
        return CampaignLock(campaign_id, recovered)

    def release_lock(
        self,
        *,
        repository_root: Path,
        target: str,
        campaign_id: str,
    ) -> None:
        root = _repository_root(repository_root)
        path = _lock_path(root, target)
        try:
            existing = _read_lock(path)
        except FileNotFoundError:
            return
        if existing.get("campaign_id") == campaign_id:
            path.unlink(missing_ok=True)

    def create_worktree(
        self,
        repository_root: Path,
        campaign_id: str,
        candidate_id: str,
        base_commit: str,
    ) -> CampaignWorktree:
        root = _repository_root(repository_root)
        _identifier(campaign_id, "campaign_id")
        _identifier(candidate_id, "candidate_id")
        _commit(base_commit)
        worktree_root = contained_worktree_root(root, campaign_id)
        path = worktree_root / candidate_id
        if os.path.lexists(path):
            raise ValueError("campaign worktree path already exists")
        worktree_root.mkdir(parents=True, exist_ok=True)
        branch = f"foundry-opt/{campaign_id}/{candidate_id}"
        self._git(
            root,
            "worktree",
            "add",
            "--quiet",
            "-b",
            branch,
            str(path),
            base_commit,
        )
        worktree = CampaignWorktree(candidate_id, path, branch, base_commit)
        self._worktrees[path.resolve()] = worktree
        return worktree

    def changed_paths(
        self,
        worktree: CampaignWorktree,
    ) -> tuple[Path, ...]:
        root = self._owned(worktree)
        tracked = self._git(
            root,
            "diff",
            "--name-only",
            "-z",
            worktree.base_commit,
            "--",
        )
        untracked = self._git(
            root,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        )
        names = {
            item.decode("utf-8")
            for item in (*tracked.split(b"\0"), *untracked.split(b"\0"))
            if item
        }
        return tuple(Path(name) for name in sorted(names))

    def reset_worktree(self, worktree: CampaignWorktree) -> None:
        path = self._owned(worktree)
        self._git(path, "reset", "--hard", "--quiet", worktree.base_commit)
        self._git(path, "clean", "-fdx", "--quiet")

    def commit_worktree(
        self,
        worktree: CampaignWorktree,
        message: str,
    ) -> str:
        path = self._owned(worktree)
        self._git(path, "add", "-A")
        self._git(
            path,
            "-c",
            "user.name=foundry-opt",
            "-c",
            "user.email=foundry-opt@users.noreply.github.com",
            "commit",
            "--quiet",
            "-m",
            message,
        )
        return self._git_text(path, "rev-parse", "HEAD^{commit}")

    def export_patch(
        self,
        repository_root: Path,
        campaign_id: str,
        worktree: CampaignWorktree,
        result_commit: str,
    ) -> PatchArtifact:
        root = _repository_root(repository_root)
        _commit(result_commit)
        self._owned(worktree)
        patch_bytes = self._git(
            worktree.path,
            "diff",
            "--binary",
            "--full-index",
            worktree.base_commit,
            result_commit,
            "--",
        )
        relative = (
            Path(".foundry-optimizer")
            / "campaigns"
            / campaign_id
            / f"{worktree.candidate_id}.patch"
        )
        output = root / relative
        if not output.parent.resolve().is_relative_to(root):
            raise ValueError("patch artifact path escapes repository")
        output.parent.mkdir(parents=True, exist_ok=True)
        if not output.parent.resolve().is_relative_to(root):
            raise ValueError("patch artifact path escapes repository")
        digest = hashlib.sha256(patch_bytes).hexdigest()
        try:
            with output.open("xb") as stream:
                stream.write(patch_bytes)
        except FileExistsError:
            if output.is_symlink() or output.read_bytes() != patch_bytes:
                raise ValueError(
                    "existing patch artifact does not match candidate"
                )
        return PatchArtifact(
            candidate_id=worktree.candidate_id,
            path=relative,
            sha256=digest,
            base_commit=worktree.base_commit,
            result_commit=result_commit,
        )

    def cleanup_worktree(
        self,
        repository_root: Path,
        worktree: CampaignWorktree,
    ) -> None:
        root = _repository_root(repository_root)
        path = require_managed_worktree(root, worktree.path)
        self._owned(worktree)
        result = subprocess.run(
            ("git", "worktree", "remove", "--force", str(path)),
            cwd=root,
            check=False,
            capture_output=True,
        )
        if result.returncode != 0 and path.exists():
            raise RuntimeError(
                result.stderr.decode("utf-8", errors="replace").strip()
            )
        subprocess.run(
            ("git", "branch", "-D", worktree.branch),
            cwd=root,
            check=False,
            capture_output=True,
        )
        self._git(root, "worktree", "prune")
        self._worktrees.pop(path, None)

    def _owned(self, worktree: CampaignWorktree) -> Path:
        path = worktree.path.expanduser().resolve()
        if self._worktrees.get(path) != worktree:
            raise ValueError("worktree is not owned by this campaign adapter")
        return path

    def _git(self, cwd: Path, *arguments: str) -> bytes:
        result = subprocess.run(
            ("git", *arguments),
            cwd=cwd,
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.decode("utf-8", errors="replace").strip()
                or "git command failed"
            )
        return result.stdout

    def _git_text(self, cwd: Path, *arguments: str) -> str:
        return self._git(cwd, *arguments).decode("ascii").strip()


def _github_default_branch(repository_root: Path) -> str:
    result = subprocess.run(
        (
            "gh",
            "repo",
            "view",
            "--json",
            "defaultBranchRef",
            "--jq",
            ".defaultBranchRef.name",
        ),
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(
            result.stderr.strip() or "could not resolve GitHub default branch"
        )
    return result.stdout.strip()


def _repository_root(path: Path) -> Path:
    root = path.expanduser().resolve()
    result = subprocess.run(
        ("git", "rev-parse", "--show-toplevel"),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or Path(result.stdout.strip()).resolve() != root:
        raise ValueError("repository_root must be the Git worktree root")
    return root


def _identifier(value: str, field: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} is invalid")


def _commit(value: str) -> None:
    if not _COMMIT.fullmatch(value):
        raise ValueError("commit is invalid")


def _validate_branch(repository_root: Path, branch: str) -> None:
    result = subprocess.run(
        ("git", "check-ref-format", "--branch", branch),
        cwd=repository_root,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise ValueError("default branch is invalid")


def _lock_path(root: Path, target: str) -> Path:
    path = root / ".foundry-optimizer" / "campaigns" / ".locks" / f"{target}.json"
    if not path.resolve().is_relative_to(root):
        raise ValueError("lock path escapes repository")
    return path


def _read_lock(path: Path) -> dict[str, str]:
    if path.is_symlink():
        raise ValueError("campaign lock cannot be a symlink")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("campaign lock metadata is invalid")
    required = {"base_commit", "campaign_id", "created_at", "target"}
    if set(value) != required or not all(
        isinstance(item, str) for item in value.values()
    ):
        raise ValueError("campaign lock metadata is invalid")
    return value
