"""``gh``/``git`` adapter for the candidate reconciliation gateway.

Implements
:class:`~foundry_opt.optimization.lifecycle.LifecycleReconcileGateway`
using the authenticated GitHub CLI and local Git. It performs real merges (or
merge-queue enrollment) and deployment-workflow dispatch through the standard
protected paths — it never passes an administrative bypass flag — and reports
its granted capabilities honestly so the reconciliation logic enforces the
separate ``MERGE`` and ``DEPLOY_DISPATCH`` capabilities.
"""

from __future__ import annotations

from collections.abc import Sequence
import json
from pathlib import Path
import re
from typing import Any

from foundry_opt.adapters.commands import CommandError
from foundry_opt.adapters.github import github_repository_from_remote_url
from foundry_opt.deployment import (
    DeploymentTrigger,
    detect_deployment_workflow,
)
from foundry_opt.github_workflow import (
    GitHubCapabilities,
    GitHubPermissionReport,
    PullRequestReference,
)
from foundry_opt.preflight.interfaces import CommandRunner


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_TREE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_MERGE_PERMISSIONS = frozenset({"admin", "maintain", "write", "push"})
_CHECK_BUCKETS = {
    "pass": "success",
    "fail": "failure",
    "pending": "pending",
    "skipping": "skipped",
    "cancel": "cancelled",
}


class GitHubReconcileError(RuntimeError):
    code = "github_reconcile_failed"

    def __init__(self, operation: str) -> None:
        self.operation = operation
        super().__init__(f"GitHub reconcile operation failed: {operation}")


class GitHubReconcileResponseError(GitHubReconcileError):
    code = "github_reconcile_response_invalid"


class GhCandidateReconcileGateway:
    def __init__(
        self,
        command_runner: CommandRunner,
        repository_root: Path,
        *,
        granted_capabilities: GitHubCapabilities | None = None,
    ) -> None:
        if not isinstance(granted_capabilities, GitHubCapabilities):
            raise ValueError("granted_capabilities must be explicit")
        self._commands = command_runner
        self._repository_root = repository_root
        self._granted_capabilities = granted_capabilities
        self._repository: str | None = None
        self._default_branch: str | None = None

    # -- capability signalling ---------------------------------------------

    def verify_permissions(
        self,
        required: GitHubCapabilities,
    ) -> GitHubPermissionReport:
        return GitHubPermissionReport(self._granted_capabilities & required)

    # -- ranked slate lookups ----------------------------------------------

    def locate_candidate_pull_request(
        self,
        repository_root: Path,
        campaign_id: str,
        candidate_id: str,
    ) -> PullRequestReference | None:
        marker = f"foundry-opt:candidate-pr:{campaign_id}:{candidate_id}:"
        raw = self._run(
            "locate_candidate_pull_request",
            (
                "gh",
                "pr",
                "list",
                "--repo",
                self._repository_name(),
                "--state",
                "all",
                "--search",
                f'"{marker}" in:body',
                "--json",
                (
                    "number,url,headRefName,headRefOid,isDraft,body,"
                    "baseRefName,state"
                ),
                "--limit",
                "10",
            ),
            cwd=repository_root,
        )
        matches = [
            item
            for item in _json_list(raw, "locate_candidate_pull_request")
            if isinstance(item.get("body"), str) and marker in item["body"]
        ]
        if not matches:
            return None
        # A human maintainer merges the chosen candidate pull request, so
        # reconciliation must see MERGED state on rerun. Prefer the merged
        # (then open, then closed) pull request and reject a real ambiguity.
        priority = {"MERGED": 0, "OPEN": 1, "CLOSED": 2}
        matches.sort(
            key=lambda item: priority.get(
                str(item.get("state", "")).upper(), 9
            )
        )
        top_state = str(matches[0].get("state", "")).upper()
        if (
            sum(
                1
                for item in matches
                if str(item.get("state", "")).upper() == top_state
            )
            > 1
        ):
            raise GitHubReconcileResponseError(
                "locate_candidate_pull_request"
            )
        return _pull_request_from_json(matches[0])

    def candidate_checks(
        self,
        repository_root: Path,
        pull_request: PullRequestReference,
    ) -> dict[str, str]:
        try:
            raw = self._run(
                "candidate_checks",
                (
                    "gh",
                    "pr",
                    "checks",
                    str(pull_request.number),
                    "--repo",
                    self._repository_name(),
                    "--json",
                    "name,bucket,state",
                ),
                cwd=repository_root,
            )
        except GitHubReconcileError:
            # ``gh pr checks`` exits non-zero when a required check has failed
            # while still emitting the JSON payload; surface it as no checks
            # so the reconciliation blocks on the missing successes.
            return {}
        checks: dict[str, str] = {}
        for item in _json_list(raw, "candidate_checks"):
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            checks[name] = _normalize_check(item)
        return checks

    def resolve_merge_commit(
        self,
        repository_root: Path,
        pull_request_number: int,
    ) -> str:
        raw = self._run(
            "resolve_merge_commit",
            (
                "gh",
                "pr",
                "view",
                str(pull_request_number),
                "--repo",
                self._repository_name(),
                "--json",
                "mergeCommit,state,mergedAt",
            ),
            cwd=repository_root,
        )
        document = _json_object(raw, "resolve_merge_commit")
        merge_commit = document.get("mergeCommit")
        if not isinstance(merge_commit, dict):
            raise GitHubReconcileResponseError("resolve_merge_commit")
        oid = merge_commit.get("oid")
        if not isinstance(oid, str) or not _COMMIT.fullmatch(oid):
            raise GitHubReconcileResponseError("resolve_merge_commit")
        return oid

    def resolve_tree(
        self,
        repository_root: Path,
        commit: str,
    ) -> str | None:
        if not _COMMIT.fullmatch(commit):
            return None
        for attempt in range(2):
            try:
                tree = self._commands.run(
                    ("git", "rev-parse", f"{commit}^{{tree}}"),
                    cwd=repository_root,
                ).stdout.strip()
            except CommandError:
                tree = ""
            if _TREE.fullmatch(tree):
                return tree
            if attempt == 0:
                try:
                    self._commands.run(
                        ("git", "fetch", "origin", commit),
                        cwd=repository_root,
                    )
                except CommandError:
                    return None
        return None

    # -- branch protection --------------------------------------------------

    def branch_protection_allows(
        self,
        repository_root: Path,
        pull_request: PullRequestReference,
        actor: str,
    ) -> bool:
        base = pull_request.base_branch
        if not base:
            return False
        try:
            protection_raw = self._run(
                "branch_protection",
                (
                    "gh",
                    "api",
                    (
                        f"repos/{self._repository_name()}/branches/"
                        f"{base}/protection"
                    ),
                ),
                cwd=repository_root,
            )
        except GitHubReconcileError:
            return False
        try:
            protection = json.loads(protection_raw)
        except json.JSONDecodeError:
            return False
        if not isinstance(protection, dict):
            return False
        required_checks = protection.get("required_status_checks")
        required_reviews = protection.get("required_pull_request_reviews")
        enforce_admins = protection.get("enforce_admins")
        # Reject unprotected or admin-bypassable branches: autopilot must merge
        # through the same enforced checks/reviews as a human, never a broad
        # administrative override.
        if (
            not isinstance(required_checks, dict)
            or not isinstance(required_reviews, dict)
            or not _flag_enabled(enforce_admins)
        ):
            return False
        return self._actor_can_merge(repository_root, actor)

    def _actor_can_merge(self, repository_root: Path, actor: str) -> bool:
        try:
            permission = self._run(
                "actor_permission",
                (
                    "gh",
                    "api",
                    (
                        f"repos/{self._repository_name()}/collaborators/"
                        f"{actor}/permission"
                    ),
                    "--jq",
                    ".permission",
                ),
                cwd=repository_root,
            ).strip()
        except GitHubReconcileError:
            return False
        return permission in _MERGE_PERMISSIONS

    # -- mutations ----------------------------------------------------------

    def merge_pull_request(
        self,
        repository_root: Path,
        pull_request_number: int,
        expected_head_commit: str,
        actor: str,
    ) -> None:
        if not _COMMIT.fullmatch(expected_head_commit):
            raise GitHubReconcileResponseError("merge_pull_request")
        # No ``--admin`` flag: the merge (or merge-queue enrollment) goes
        # through the branch's protected path so required checks are enforced.
        self._run(
            "merge_pull_request",
            (
                "gh",
                "pr",
                "merge",
                str(pull_request_number),
                "--repo",
                self._repository_name(),
                "--merge",
                "--match-head-commit",
                expected_head_commit,
            ),
            cwd=repository_root,
        )

    def dispatch_deployment(
        self,
        repository_root: Path,
        pull_request_number: int,
        expected_head_commit: str,
    ) -> None:
        workflow = detect_deployment_workflow(repository_root)
        if not workflow.exists or workflow.trigger is not (
            DeploymentTrigger.MANUAL
        ):
            raise GitHubReconcileError("dispatch_deployment")
        self._run(
            "dispatch_deployment",
            (
                "gh",
                "workflow",
                "run",
                workflow.path.as_posix(),
                "--repo",
                self._repository_name(),
                "--ref",
                self._default_branch_name(repository_root),
            ),
            cwd=repository_root,
        )

    # -- internals ----------------------------------------------------------

    def _default_branch_name(self, repository_root: Path) -> str:
        if self._default_branch is not None:
            return self._default_branch
        branch = self._run(
            "default_branch",
            (
                "gh",
                "api",
                f"repos/{self._repository_name()}",
                "--jq",
                ".default_branch",
            ),
            cwd=repository_root,
        ).strip()
        if not branch:
            raise GitHubReconcileResponseError("default_branch")
        self._default_branch = branch
        return branch

    def _repository_name(self) -> str:
        if self._repository is not None:
            return self._repository
        remote = self._run(
            "origin",
            ("git", "remote", "get-url", "origin"),
            cwd=self._repository_root,
        ).strip()
        repository = github_repository_from_remote_url(remote)
        if repository is None:
            raise GitHubReconcileResponseError("origin")
        self._repository = repository
        return repository

    def _run(
        self,
        operation: str,
        arguments: Sequence[str],
        *,
        cwd: Path,
    ) -> str:
        try:
            return self._commands.run(arguments, cwd=cwd).stdout
        except CommandError as error:
            raise GitHubReconcileError(operation) from error


def _flag_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return isinstance(value, dict) and value.get("enabled") is True


def _normalize_check(item: dict[str, Any]) -> str:
    bucket = item.get("bucket")
    if isinstance(bucket, str) and bucket in _CHECK_BUCKETS:
        return _CHECK_BUCKETS[bucket]
    state = item.get("state")
    if isinstance(state, str):
        lowered = state.casefold()
        if lowered in {
            "success",
            "failure",
            "pending",
            "cancelled",
            "skipped",
        }:
            return lowered
    return "pending"


def _json_list(raw: str, operation: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise GitHubReconcileResponseError(operation) from error
    if not isinstance(value, list) or not all(
        isinstance(item, dict) for item in value
    ):
        raise GitHubReconcileResponseError(operation)
    return value


def _json_object(raw: str, operation: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise GitHubReconcileResponseError(operation) from error
    if not isinstance(value, dict):
        raise GitHubReconcileResponseError(operation)
    return value


def _pull_request_from_json(value: dict[str, Any]) -> PullRequestReference:
    try:
        return PullRequestReference(
            number=int(value["number"]),
            url=str(value["url"]),
            head_branch=str(value["headRefName"]),
            head_commit=str(value["headRefOid"]),
            draft=bool(value["isDraft"]),
            body=str(value.get("body") or ""),
            base_branch=str(value["baseRefName"]),
            state=str(value["state"]).upper(),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise GitHubReconcileResponseError(
            "locate_candidate_pull_request"
        ) from error
