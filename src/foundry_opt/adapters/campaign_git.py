from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
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
        self._default_branch = default_branch or remote_default_branch
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
        commit = self._remote_branch_tip(root, branch)
        self._git(
            root,
            "fetch",
            "--quiet",
            "--no-tags",
            "origin",
            commit,
        )
        fetched = self._git_text(root, "rev-parse", "FETCH_HEAD^{commit}")
        if fetched != commit:
            raise ValueError("fetched default-branch commit changed")
        self._git(root, "cat-file", "-e", f"{commit}^{{commit}}")
        self._git(root, "cat-file", "-e", f"{commit}^{{tree}}")
        current_branch = self._default_branch(root).strip()
        _validate_branch(root, current_branch)
        if current_branch != branch:
            raise ValueError("remote default branch changed while pinning")
        if self._remote_branch_tip(root, branch) != commit:
            raise ValueError("remote default branch advanced while pinning")
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

    def open_worktree(
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
        resolved = path.resolve()
        existing = self._worktrees.get(resolved)
        if existing is not None:
            return existing
        if path.is_symlink() or not path.is_dir():
            raise ValueError("campaign worktree does not exist")
        branch = f"foundry-opt/{campaign_id}/{candidate_id}"
        registered_paths: set[Path] = set()
        for line in self._git(root, "worktree", "list", "--porcelain").split(
            b"\n"
        ):
            if line.startswith(b"worktree "):
                raw = line[len(b"worktree "):].decode("utf-8", "replace")
                try:
                    registered_paths.add(Path(raw).expanduser().resolve())
                except OSError:
                    continue
        if resolved not in registered_paths:
            raise ValueError("campaign worktree is not registered with Git")
        worktree = CampaignWorktree(candidate_id, path, branch, base_commit)
        self._worktrees[resolved] = worktree
        return worktree

    def reconcile_worktree(
        self,
        repository_root: Path,
        campaign_id: str,
        candidate_id: str,
        base_commit: str,
    ) -> CampaignWorktree:
        """Discard any orphan worktree/branch, then recreate a clean worktree.

        Used to recover from a crash between ``create_worktree`` and the
        durable ``awaiting_idea`` reservation: the partially created worktree
        (registered branch + directory) is force-removed before a fresh
        worktree is created at the exact base commit.
        """
        root = _repository_root(repository_root)
        _identifier(campaign_id, "campaign_id")
        _identifier(candidate_id, "candidate_id")
        _commit(base_commit)
        worktree_root = contained_worktree_root(root, campaign_id)
        path = worktree_root / candidate_id
        branch = f"foundry-opt/{campaign_id}/{candidate_id}"
        if os.path.lexists(path):
            # Only ever operate inside the managed worktree area.
            require_managed_worktree(root, path)
            subprocess.run(
                ("git", "worktree", "remove", "--force", str(path)),
                cwd=root,
                check=False,
                capture_output=True,
            )
            if os.path.lexists(path):
                _remove_managed_tree(path)
        subprocess.run(
            ("git", "branch", "-D", branch),
            cwd=root,
            check=False,
            capture_output=True,
        )
        self._git(root, "worktree", "prune")
        self._worktrees.pop(path.resolve(), None)
        return self.create_worktree(root, campaign_id, candidate_id, base_commit)

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
        if not self._git(path, "status", "--porcelain=v1", "--"):
            head = self._git_text(path, "rev-parse", "HEAD^{commit}")
            if head == worktree.base_commit:
                raise ValueError("candidate worktree has no changes")
            return head
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
            result_tree=self._git_text(
                worktree.path,
                "rev-parse",
                f"{result_commit}^{{tree}}",
            ),
        )

    def cleanup_worktree(
        self,
        repository_root: Path,
        worktree: CampaignWorktree,
    ) -> None:
        root = _repository_root(repository_root)
        path = require_managed_worktree(root, worktree.path)
        branch_parts = worktree.branch.split("/")
        if (
            len(branch_parts) != 3
            or branch_parts[0] != "foundry-opt"
            or branch_parts[2] != worktree.candidate_id
        ):
            raise ValueError("worktree cleanup binding is invalid")
        campaign_id = branch_parts[1]
        _identifier(campaign_id, "campaign_id")
        expected_path = (
            contained_worktree_root(root, campaign_id)
            / worktree.candidate_id
        ).resolve()
        if path != expected_path:
            raise ValueError("worktree cleanup path does not match branch")
        owned = self._worktrees.get(path)
        if owned is not None and owned != worktree:
            raise ValueError("worktree cleanup ownership changed")
        registered = _registered_worktrees(root)
        if path in registered:
            registered_branch = registered[path]
            if registered_branch != f"refs/heads/{worktree.branch}":
                raise ValueError("registered worktree branch changed")
            result = subprocess.run(
                ("git", "worktree", "remove", "--force", str(path)),
                cwd=root,
                check=False,
                capture_output=True,
            )
            if result.returncode != 0 and path.exists():
                raise RuntimeError(
                    result.stderr.decode(
                        "utf-8",
                        errors="replace",
                    ).strip()
                )
        if os.path.lexists(path):
            _remove_managed_tree(path)
        deleted = subprocess.run(
            ("git", "branch", "-D", worktree.branch),
            cwd=root,
            check=False,
            capture_output=True,
        )
        remaining = subprocess.run(
            (
                "git",
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{worktree.branch}",
            ),
            cwd=root,
            check=False,
            capture_output=True,
        )
        if remaining.returncode == 0:
            raise RuntimeError(
                deleted.stderr.decode(
                    "utf-8",
                    errors="replace",
                ).strip()
                or "optimizer worktree branch still exists"
            )
        if remaining.returncode != 1:
            raise RuntimeError(
                remaining.stderr.decode(
                    "utf-8",
                    errors="replace",
                ).strip()
                or "optimizer worktree branch state is unavailable"
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

    def _remote_branch_tip(self, root: Path, branch: str) -> str:
        output = self._git(
            root,
            "ls-remote",
            "--heads",
            "origin",
            f"refs/heads/{branch}",
        )
        expected_ref = f"refs/heads/{branch}".encode("utf-8")
        matches: list[str] = []
        for line in output.splitlines():
            fields = line.split(b"\t")
            if len(fields) != 2 or fields[1] != expected_ref:
                continue
            try:
                commit = fields[0].decode("ascii")
            except UnicodeDecodeError as error:
                raise ValueError(
                    "remote default-branch tip is invalid"
                ) from error
            _commit(commit)
            matches.append(commit)
        if len(matches) != 1:
            raise ValueError("remote default-branch tip is unavailable")
        return matches[0]


def remote_default_branch(repository_root: Path) -> str:
    result = subprocess.run(
        (
            "git",
            "ls-remote",
            "--symref",
            "origin",
            "HEAD",
        ),
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip() or "could not resolve remote default branch"
        )
    branches = []
    for line in result.stdout.splitlines():
        match = re.fullmatch(r"ref: refs/heads/(.+)\s+HEAD", line)
        if match is not None:
            branches.append(match.group(1))
    if len(branches) != 1:
        raise ValueError("remote default branch is unavailable")
    return branches[0]


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


def _registered_worktrees(repository_root: Path) -> dict[Path, str | None]:
    result = subprocess.run(
        ("git", "worktree", "list", "--porcelain"),
        cwd=repository_root,
        check=True,
        capture_output=True,
    )
    registered: dict[Path, str | None] = {}
    path: Path | None = None
    for line in result.stdout.splitlines():
        if line.startswith(b"worktree "):
            path = Path(
                line.removeprefix(b"worktree ").decode(
                    "utf-8",
                    errors="replace",
                )
            ).expanduser().resolve()
            registered[path] = None
        elif line.startswith(b"branch ") and path is not None:
            registered[path] = line.removeprefix(b"branch ").decode(
                "utf-8",
                errors="replace",
            )
        elif not line:
            path = None
    return registered


def _remove_managed_tree(path: Path) -> None:
    """Force-remove a managed worktree directory, rejecting symlinks."""
    if path.is_symlink():
        raise ValueError("refusing to remove a symlinked worktree path")
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


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
