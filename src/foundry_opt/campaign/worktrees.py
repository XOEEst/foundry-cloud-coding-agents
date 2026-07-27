from pathlib import Path


def contained_worktree_root(
    repository_root: Path,
    campaign_id: str,
) -> Path:
    root = repository_root.expanduser().resolve()
    candidate = (
        root / ".foundry-optimizer" / "worktrees" / campaign_id
    ).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("campaign worktree root escapes the repository")
    return candidate


def require_managed_worktree(
    repository_root: Path,
    worktree_path: Path,
) -> Path:
    managed = (
        repository_root.expanduser().resolve()
        / ".foundry-optimizer"
        / "worktrees"
    ).resolve()
    candidate = worktree_path.expanduser().resolve()
    if not candidate.is_relative_to(managed):
        raise ValueError("refusing to operate on an unmanaged worktree")
    return candidate
