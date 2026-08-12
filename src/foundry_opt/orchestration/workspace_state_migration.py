from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import re
import subprocess

from foundry_opt.orchestration.git_transport import (
    fetch_revision,
    GitTransportError,
    remote_revision,
    resolve_safe_fetch_remote,
)


_COMMIT = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class WorkspaceStateMigrationPlan:
    issue_number: int
    source_ref: str
    source_revision: str
    source_schema_version: int
    target_schema_version: int
    legacy_paths: tuple[str, ...]
    read_only: bool


def workspace_state_v3_migration_plan(
    *,
    issue_number: int,
    source_revision: str,
    source_paths: tuple[str, ...],
) -> WorkspaceStateMigrationPlan:
    _issue_number(issue_number)
    if _COMMIT.fullmatch(source_revision) is None:
        raise ValueError("source_revision must be a commit SHA")
    paths = tuple(source_paths)
    if (
        any(type(path) is not str for path in paths)
        or len(paths) != len(set(paths))
        or "snapshot.json" not in paths
        or "journal.jsonl" not in paths
        or any(
            path not in {"snapshot.json", "journal.jsonl"}
            and not path.startswith(("inbox/", "outbox/", "objects/"))
            for path in paths
        )
    ):
        raise ValueError("legacy v3 paths are invalid")
    return WorkspaceStateMigrationPlan(
        issue_number=issue_number,
        source_ref=_state_ref(issue_number),
        source_revision=source_revision,
        source_schema_version=3,
        target_schema_version=4,
        legacy_paths=paths,
        read_only=True,
    )


def detect_workspace_state_v3(
    repository_root: Path,
    issue_number: int,
    *,
    remote: str = "origin",
) -> WorkspaceStateMigrationPlan | None:
    root = _repository_root(repository_root)
    _issue_number(issue_number)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", remote) is None:
        raise ValueError("remote is invalid")
    safe_remote = resolve_safe_fetch_remote(root, remote)
    if safe_remote is None:
        raise RuntimeError("workspace state fetch destination is not trusted")
    ref = _state_ref(issue_number)
    try:
        revision = remote_revision(root, safe_remote, ref)
        if revision is None:
            return None
        fetched = fetch_revision(root, safe_remote, ref)
    except GitTransportError as error:
        raise RuntimeError("workspace state v3 detection failed") from error
    if fetched != revision:
        raise RuntimeError("workspace state changed during v3 detection")
    paths = tuple(
        line
        for line in _git_text(
            root,
            "ls-tree",
            "-r",
            "--name-only",
            revision,
        ).splitlines()
        if line
    )
    try:
        snapshot = json.loads(
            _git_bytes(root, "show", f"{revision}:snapshot.json")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("workspace state snapshot is invalid") from error
    if (
        type(snapshot) is not dict
        or snapshot.get("schema_version") != 3
    ):
        return None
    return workspace_state_v3_migration_plan(
        issue_number=issue_number,
        source_revision=revision,
        source_paths=paths,
    )


def _repository_root(path: Path) -> Path:
    root = Path(os.path.abspath(path))
    completed = subprocess.run(
        ("git", "rev-parse", "--show-toplevel"),
        cwd=root,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("repository_root must be a Git worktree")
    discovered = Path(
        os.path.abspath(completed.stdout.decode("utf-8").strip())
    )
    if os.path.normcase(discovered) != os.path.normcase(root):
        raise ValueError("repository_root must be the Git worktree root")
    return root


def _issue_number(value: int) -> None:
    if type(value) is not int or value < 1:
        raise ValueError("issue_number must be a positive integer")


def _state_ref(issue_number: int) -> str:
    return f"refs/heads/foundry-opt/state/issue-{issue_number}"


def _git_text(cwd: Path, *arguments: str) -> str:
    return _git_bytes(cwd, *arguments).decode("utf-8").strip()


def _git_bytes(cwd: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("Git workspace migration inspection failed")
    return completed.stdout
