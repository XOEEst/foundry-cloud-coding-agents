from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Any

from foundry_opt.adapters.commands import CommandError
from foundry_opt.orchestration.workspace_runtime import WorkspaceStore
from foundry_opt.orchestration.workspace_store import WorkspaceLineage
from foundry_opt.preflight.interfaces import CommandRunner


@dataclass(frozen=True)
class WorkspaceVerificationCheck:
    name: str
    status: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "message": self.message,
            "name": self.name,
            "status": self.status,
        }


@dataclass(frozen=True)
class WorkspaceVerificationResult:
    issue_number: int
    pull_request_number: int
    repository: str
    verified: bool
    checks: tuple[WorkspaceVerificationCheck, ...]
    head_commit: str | None = None
    head_tree: str | None = None
    changed_paths: tuple[str, ...] = ()
    lineage: WorkspaceLineage | None = None

    @property
    def exit_code(self) -> int:
        return 0 if self.verified else 1

    def to_dict(self) -> dict[str, Any]:
        lineage = self.lineage
        return {
            "changed_paths": list(self.changed_paths),
            "checks": [item.to_dict() for item in self.checks],
            "head_commit": self.head_commit,
            "head_tree": self.head_tree,
            "issue_number": self.issue_number,
            "lineage": (
                {
                    "base_commit": lineage.base_commit,
                    "bundle_sha256": lineage.bundle_sha256,
                    "evidence_sha256": lineage.evidence_sha256,
                    "expected_tree": lineage.expected_tree,
                    "patch_sha256": lineage.patch_sha256,
                    "required_checks": dict(lineage.required_checks),
                    "required_checks_provenance": (
                        lineage.required_checks_provenance
                    ),
                    "selected_candidate_id": (
                        lineage.selected_candidate_id
                    ),
                    "spec_sha256": lineage.spec_sha256,
                    "workspace_pull_request_number": (
                        lineage.workspace_pull_request_number
                    ),
                }
                if lineage is not None
                else None
            ),
            "pull_request_number": self.pull_request_number,
            "repository": self.repository,
            "verified": self.verified,
        }


class WorkspaceVerifier:
    def __init__(
        self,
        *,
        store: WorkspaceStore,
        commands: CommandRunner,
        repository: str,
        base_branch: str,
        remote: str = "origin",
    ) -> None:
        self._store = store
        self._commands = commands
        self._repository = repository
        self._base_branch = base_branch
        self._remote = remote

    def verify(
        self,
        repository_root: Path,
        *,
        issue_number: int,
        pull_request_number: int,
    ) -> WorkspaceVerificationResult:
        checks: list[WorkspaceVerificationCheck] = []
        try:
            snapshot = self._store.load(issue_number)
        except Exception:
            return self._result(
                issue_number,
                pull_request_number,
                checks=(
                    _failed(
                        "workspace_state",
                        "Trusted v4 workspace state could not be loaded.",
                    ),
                ),
            )
        lineage = snapshot.lineage if snapshot is not None else None
        if snapshot is None or lineage is None:
            return self._result(
                issue_number,
                pull_request_number,
                lineage=lineage,
                checks=(
                    _failed(
                        "workspace_lineage",
                        "Exact selected workspace lineage is missing.",
                    ),
                ),
            )
        state_identity = (
            snapshot.workspace_pull_request_number
            == pull_request_number
            == lineage.workspace_pull_request_number
        )
        checks.append(
            _check(
                "workspace_lineage",
                state_identity,
                "Workspace state binds the requested pull request."
                if state_identity
                else "Workspace pull request identity does not match state.",
            )
        )
        try:
            pull = self._json_object(
                (
                    "gh",
                    "api",
                    (
                        f"repos/{self._repository}/pulls/"
                        f"{pull_request_number}"
                    ),
                ),
                repository_root,
            )
            head = _pull_ref(pull, "head")
            base = _pull_ref(pull, "base")
            head_sha = head["sha"]
            identity = (
                pull.get("number") == pull_request_number
                and head["repo"].casefold()
                == self._repository.casefold()
                and base["repo"].casefold()
                == self._repository.casefold()
                and head["ref"]
                == f"foundry-opt/workspace/issue-{issue_number}"
                and base["ref"] == self._base_branch
            )
            checks.append(
                _check(
                    "pull_request_identity",
                    identity,
                    "GitHub repository and workspace branch match."
                    if identity
                    else "GitHub repository or branch identity changed.",
                )
            )
            ready = (
                pull.get("state") == "open"
                and pull.get("draft") is False
            )
            checks.append(
                _check(
                    "pull_request_ready",
                    ready,
                    "Pull request is open and ready."
                    if ready
                    else "Pull request is not open and ready.",
                )
            )
            base_matches = base["sha"] == lineage.base_commit
            checks.append(
                _check(
                    "base_commit",
                    base_matches,
                    "Pull request base matches approved base."
                    if base_matches
                    else "Pull request base commit changed.",
                )
            )
            head_tree, head_parent = self._head_details(
                repository_root,
                head_sha,
            )
            tree_matches = (
                head_tree == lineage.expected_tree
                and head_parent == lineage.base_commit
            )
            checks.append(
                _check(
                    "head_tree",
                    tree_matches,
                    "Head tree and parent match exact selected lineage."
                    if tree_matches
                    else "Head tree or parent does not match lineage.",
                )
            )
            patch_tree, patch_paths = self._verify_patch(
                repository_root,
                issue_number=issue_number,
                head_sha=head_sha,
                lineage=lineage,
                selected_patch=snapshot.selected_patch,
            )
            patch_matches = (
                snapshot.selected_patch is not None
                and hashlib.sha256(snapshot.selected_patch).hexdigest()
                == lineage.patch_sha256
                and patch_tree == lineage.expected_tree
            )
            checks.append(
                _check(
                    "selected_patch",
                    patch_matches,
                    "Selected patch reproduces the expected tree."
                    if patch_matches
                    else "Selected patch does not reproduce lineage.",
                )
            )
            head_paths = self._head_paths(
                repository_root,
                head_sha,
            )
            paths_match = (
                patch_paths == head_paths and bool(head_paths)
            )
            checks.append(
                _check(
                    "changed_paths",
                    paths_match,
                    "Selected patch paths match the exact head commit."
                    if paths_match
                    else "Selected patch paths do not match the head.",
                )
            )
            markers = {
                (
                    f"{lineage.selected_candidate_id}:patch:"
                    f"{lineage.patch_sha256}"
                ),
                (
                    f"{lineage.selected_candidate_id}:evidence:"
                    f"{lineage.evidence_sha256}"
                ),
                (
                    f"{lineage.selected_candidate_id}:bundle:"
                    f"{lineage.bundle_sha256}"
                ),
                (
                    f"{lineage.selected_candidate_id}:tree:"
                    f"{lineage.expected_tree}"
                ),
                f"workspace_commit:{head_sha}",
            }
            compact_lineage = markers <= set(
                snapshot.external_operation_ids
            )
            selected_experiment = next(
                (
                    item
                    for item in snapshot.experiments
                    if item.candidate_id
                    == lineage.selected_candidate_id
                ),
                None,
            )
            compact_lineage = compact_lineage and (
                snapshot.specification is not None
                and snapshot.specification.status == "policy_approved"
                and snapshot.specification.spec_sha256
                == lineage.spec_sha256
                and snapshot.specification.base_commit
                == lineage.base_commit
                and snapshot.baseline is not None
                and snapshot.baseline.status == "completed"
                and selected_experiment is not None
                and selected_experiment.status == "completed"
                and selected_experiment.patch_sha256
                == lineage.patch_sha256
                and selected_experiment.evidence_sha256
                == lineage.evidence_sha256
                and selected_experiment.bundle_sha256
                == lineage.bundle_sha256
                and selected_experiment.expected_tree
                == lineage.expected_tree
                and selected_experiment.changed_paths == head_paths
            )
            checks.append(
                _check(
                    "state_lineage",
                    compact_lineage,
                    "State-derived spec, evidence, and bundle lineage is bound."
                    if compact_lineage
                    else "State-derived lineage markers are incomplete.",
                )
            )
            check_context = self._required_checks(
                repository_root,
                head_sha=head_sha,
                lineage=lineage,
            )
            checks.append(
                _check(
                    "required_checks",
                    check_context,
                    "Required GitHub checks succeeded for the exact head."
                    if check_context
                    else "Required GitHub check context does not match.",
                )
            )
            return self._result(
                issue_number,
                pull_request_number,
                checks=tuple(checks),
                head_commit=head_sha,
                head_tree=head_tree,
                changed_paths=head_paths,
                lineage=lineage,
            )
        except (CommandError, RuntimeError, ValueError, json.JSONDecodeError):
            checks.append(
                _failed(
                    "trusted_inputs",
                    "Trusted GitHub or Git verification failed closed.",
                )
            )
            return self._result(
                issue_number,
                pull_request_number,
                checks=tuple(checks),
                lineage=lineage,
            )

    def _head_details(
        self,
        root: Path,
        head_sha: str,
    ) -> tuple[str, str]:
        self._commands.run(
            ("git", "fetch", "--no-tags", self._remote, head_sha),
            cwd=root,
        )
        tree = self._commands.run(
            ("git", "rev-parse", "--verify", f"{head_sha}^{{tree}}"),
            cwd=root,
        ).stdout.strip()
        parent = self._commands.run(
            ("git", "rev-parse", "--verify", f"{head_sha}^"),
            cwd=root,
        ).stdout.strip()
        if (
            re.fullmatch(r"[0-9a-f]{40}", tree) is None
            or re.fullmatch(r"[0-9a-f]{40}", parent) is None
        ):
            raise RuntimeError("workspace head lineage is invalid")
        return tree, parent

    def _head_paths(
        self,
        root: Path,
        head_sha: str,
    ) -> tuple[str, ...]:
        return tuple(
            item
            for item in self._commands.run(
                (
                    "git",
                    "diff-tree",
                    "--no-commit-id",
                    "--name-only",
                    "-r",
                    "-z",
                    head_sha,
                ),
                cwd=root,
            ).stdout.split("\0")
            if item
        )

    def _verify_patch(
        self,
        root: Path,
        *,
        issue_number: int,
        head_sha: str,
        lineage: WorkspaceLineage,
        selected_patch: bytes | None,
    ) -> tuple[str, tuple[str, ...]]:
        if selected_patch is None:
            raise RuntimeError("workspace selected patch is missing")
        worktree = root / ".fv" / f"v{issue_number}-{head_sha[:8]}"
        self._remove_worktree(root, worktree)
        worktree.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._commands.run(
                (
                    "git",
                    "worktree",
                    "add",
                    "--detach",
                    str(worktree),
                    lineage.base_commit,
                ),
                cwd=root,
            )
            self._commands.run(
                ("git", "apply", "--check", "--binary", "--index", "-"),
                cwd=worktree,
                input_bytes=selected_patch,
            )
            self._commands.run(
                (
                    "git",
                    "apply",
                    "--binary",
                    "--index",
                    "--whitespace=nowarn",
                    "-",
                ),
                cwd=worktree,
                input_bytes=selected_patch,
            )
            paths = tuple(
                item
                for item in self._commands.run(
                    (
                        "git",
                        "diff",
                        "--cached",
                        "--name-only",
                        "-z",
                    ),
                    cwd=worktree,
                ).stdout.split("\0")
                if item
            )
            tree = self._commands.run(
                ("git", "write-tree"),
                cwd=worktree,
            ).stdout.strip()
            return tree, paths
        finally:
            self._remove_worktree(root, worktree)

    def _required_checks(
        self,
        root: Path,
        *,
        head_sha: str,
        lineage: WorkspaceLineage,
    ) -> bool:
        if lineage.required_checks_provenance != (
            f"trusted-selector:head:{head_sha}"
        ):
            return False
        document = self._json_object(
            (
                "gh",
                "api",
                f"repos/{self._repository}/commits/{head_sha}/check-runs",
            ),
            root,
        )
        values = document.get("check_runs")
        if not isinstance(values, list):
            return False
        by_name: dict[str, tuple[str, str, str]] = {}
        for item in values:
            if not isinstance(item, dict):
                return False
            name = item.get("name")
            if name not in lineage.required_checks:
                continue
            value = (
                item.get("status"),
                item.get("conclusion"),
                item.get("head_sha"),
            )
            if name in by_name:
                return False
            by_name[name] = value
        return all(
            lineage.required_checks[name] == "success"
            and by_name.get(name)
            == ("completed", "success", head_sha)
            for name in lineage.required_checks
        )

    def _json_object(
        self,
        arguments: tuple[str, ...],
        root: Path,
    ) -> dict[str, Any]:
        value = json.loads(
            self._commands.run(arguments, cwd=root).stdout
        )
        if not isinstance(value, dict):
            raise RuntimeError("trusted GitHub response is invalid")
        return value

    def _remove_worktree(self, root: Path, worktree: Path) -> None:
        try:
            self._commands.run(
                (
                    "git",
                    "worktree",
                    "remove",
                    "--force",
                    str(worktree),
                ),
                cwd=root,
            )
        except CommandError:
            pass
        if worktree.exists():
            shutil.rmtree(worktree)
        try:
            self._commands.run(
                ("git", "worktree", "prune"),
                cwd=root,
            )
        except CommandError:
            pass
        if worktree.parent.exists() and not any(
            worktree.parent.iterdir()
        ):
            worktree.parent.rmdir()

    def _result(
        self,
        issue_number: int,
        pull_request_number: int,
        *,
        checks: tuple[WorkspaceVerificationCheck, ...],
        lineage: WorkspaceLineage | None = None,
        head_commit: str | None = None,
        head_tree: str | None = None,
        changed_paths: tuple[str, ...] = (),
    ) -> WorkspaceVerificationResult:
        return WorkspaceVerificationResult(
            issue_number=issue_number,
            pull_request_number=pull_request_number,
            repository=self._repository,
            verified=bool(checks)
            and all(item.status == "pass" for item in checks),
            checks=checks,
            head_commit=head_commit,
            head_tree=head_tree,
            changed_paths=changed_paths,
            lineage=lineage,
        )


def _pull_ref(document: dict[str, Any], name: str) -> dict[str, str]:
    value = document.get(name)
    repository = value.get("repo") if isinstance(value, dict) else None
    full_name = (
        repository.get("full_name")
        if isinstance(repository, dict)
        else None
    )
    ref = value.get("ref") if isinstance(value, dict) else None
    sha = value.get("sha") if isinstance(value, dict) else None
    if (
        not isinstance(full_name, str)
        or not isinstance(ref, str)
        or not isinstance(sha, str)
        or re.fullmatch(r"[0-9a-f]{40}", sha) is None
    ):
        raise RuntimeError("trusted pull request reference is invalid")
    return {"ref": ref, "repo": full_name, "sha": sha}


def _check(
    name: str,
    passed: bool,
    message: str,
) -> WorkspaceVerificationCheck:
    return WorkspaceVerificationCheck(
        name=name,
        status="pass" if passed else "fail",
        message=message,
    )


def _failed(name: str, message: str) -> WorkspaceVerificationCheck:
    return _check(name, False, message)


__all__ = [
    "WorkspaceVerificationCheck",
    "WorkspaceVerificationResult",
    "WorkspaceVerifier",
]
