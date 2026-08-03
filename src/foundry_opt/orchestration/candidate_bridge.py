from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any

from foundry_opt.adapters.commands import (
    CommandError,
    CommandExitError,
    SubprocessCommandRunner,
)
from foundry_opt.adapters.github_campaign import GitExactPatchApplier
from foundry_opt.orchestration.candidate_slate import (
    CandidateBinding,
    CandidatePullRequestSnapshot,
    CandidatePullRequestState,
    CandidateSelectionRequest,
    candidate_pr_marker,
)
from foundry_opt.preflight.interfaces import CommandRunner


_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
)
_MARKER = re.compile(
    r"^<!-- foundry-opt:candidate-pr:"
    r"issue-[1-9][0-9]*:g[1-9][0-9]*:"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}:"
    r"[0-9a-f]{20} -->$"
)


class CandidateBridgeError(RuntimeError):
    pass


class GhApplierWorkerGateway:
    """GitHub transport adapter that creates issues, never pull requests."""

    def __init__(
        self,
        commands: CommandRunner,
        repository_root: Path,
        repository: str,
        *,
        assignment_token: str | None = None,
    ) -> None:
        if not _REPOSITORY.fullmatch(repository):
            raise ValueError("repository is invalid")
        if assignment_token == "":
            raise ValueError("Copilot assignment token is required")
        self._commands = commands
        self._root = repository_root
        self._repository = repository
        self._assignment_environment = (
            {"GH_TOKEN": assignment_token}
            if assignment_token is not None
            else None
        )

    def find_issue(self, marker: str) -> int | None:
        _marker(marker)
        pages = self._json(
            (
                "gh",
                "api",
                "--paginate",
                "--slurp",
                (
                    f"repos/{self._repository}/issues"
                    "?state=all&per_page=100"
                ),
            )
        )
        if not isinstance(pages, list):
            raise CandidateBridgeError("issue search response is invalid")
        matches: list[int] = []
        for page in pages:
            if not isinstance(page, list):
                raise CandidateBridgeError(
                    "issue search response is invalid"
                )
            for item in page:
                if (
                    not isinstance(item, dict)
                    or "pull_request" in item
                    or marker not in str(item.get("body", ""))
                ):
                    continue
                number = item.get("number")
                user = item.get("user")
                if (
                    type(number) is not int
                    or number < 1
                    or not isinstance(user, dict)
                    or user.get("login") != "github-actions[bot]"
                ):
                    raise CandidateBridgeError(
                        "worker issue identity is invalid"
                    )
                matches.append(number)
        if len(matches) > 1:
            raise CandidateBridgeError("worker issue marker is ambiguous")
        return matches[0] if matches else None

    def create_issue(
        self,
        *,
        title: str,
        body: str,
        marker: str,
    ) -> int:
        _marker(marker)
        if marker not in body or not title.strip():
            raise ValueError("worker issue content is invalid")
        response = self._write(
            "POST",
            f"repos/{self._repository}/issues",
            {"body": body, "title": title},
        )
        if not isinstance(response, dict):
            raise CandidateBridgeError(
                "worker issue response is invalid"
            )
        number = response.get("number")
        if type(number) is not int or number < 1:
            raise CandidateBridgeError(
                "worker issue number is invalid"
            )
        return number

    def assign_exact_patch_specialist(
        self,
        issue_number: int,
        *,
        marker: str,
    ) -> None:
        _positive(issue_number, "issue_number")
        _marker(marker)
        endpoint = (
            f"repos/{self._repository}/issues/"
            f"{issue_number}/assignees"
        )
        if self._assignment_environment is None:
            raise CandidateBridgeError(
                "Copilot assignment token is required"
            )
        assignees = {"assignees": ["copilot-swe-agent[bot]"]}
        self._write(
            "DELETE",
            endpoint,
            assignees,
            assignment=True,
        )
        self._write(
            "POST",
            endpoint,
            {
                **assignees,
                "agent_assignment": {
                    "custom_agent": "foundry-candidate-applier",
                    "custom_instructions": (
                        "Apply the exact steward-attested patch and open "
                        "the native candidate pull request. Do not make "
                        "extra edits."
                    ),
                    "target_repo": self._repository,
                },
            },
            assignment=True,
        )

    def has_assignment_marker(
        self,
        issue_number: int,
        marker: str,
    ) -> bool:
        _positive(issue_number, "issue_number")
        _marker(marker)
        response = self._json(
            (
                "gh",
                "api",
                f"repos/{self._repository}/issues/{issue_number}",
            )
        )
        if not isinstance(response, dict):
            raise CandidateBridgeError("worker issue response is invalid")
        assignees = response.get("assignees")
        if not isinstance(assignees, list):
            raise CandidateBridgeError(
                "worker issue assignees are invalid"
            )
        assigned = any(
            isinstance(item, dict)
            and item.get("login") == "copilot-swe-agent[bot]"
            for item in assignees
        )
        if assigned:
            return True
        assignment_marker = _assignment_marker(marker)
        pages = self._json(
            (
                "gh",
                "api",
                "--paginate",
                "--slurp",
                (
                    f"repos/{self._repository}/issues/"
                    f"{issue_number}/comments"
                ),
            )
        )
        if not isinstance(pages, list):
            raise CandidateBridgeError(
                "worker issue comments are invalid"
            )
        return any(
            isinstance(page, list)
            and any(
                isinstance(item, dict)
                and isinstance(item.get("user"), dict)
                and item["user"].get("login") == "github-actions[bot]"
                and assignment_marker in str(item.get("body", ""))
                for item in page
            )
            for page in pages
        )

    def record_assignment_marker(
        self,
        issue_number: int,
        marker: str,
    ) -> None:
        _positive(issue_number, "issue_number")
        _marker(marker)
        assignment_marker = _assignment_marker(marker)
        self._write(
            "POST",
            (
                f"repos/{self._repository}/issues/"
                f"{issue_number}/comments"
            ),
            {"body": assignment_marker},
        )

    def _json(self, arguments: tuple[str, ...]) -> Any:
        result = self._commands.run(arguments, cwd=self._root)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise CandidateBridgeError(
                "GitHub response is not valid JSON"
            ) from error

    def _write(
        self,
        method: str,
        endpoint: str,
        document: dict[str, object],
        *,
        assignment: bool = False,
    ) -> Any:
        result = self._commands.run(
            (
                "gh",
                "api",
                "--method",
                method,
                endpoint,
                "--input",
                "-",
            ),
            cwd=self._root,
            environment=(
                self._assignment_environment
                if assignment
                else None
            ),
            input_text=json.dumps(
                document,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        if not result.stdout:
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise CandidateBridgeError(
                "GitHub write response is not valid JSON"
            ) from error


class GhCandidateSupersessionGateway(GhApplierWorkerGateway):
    def issue_is_superseded(self, number: int, marker: str) -> bool:
        _positive(number, "issue_number")
        _marker(marker)
        response = self._json(
            (
                "gh",
                "api",
                f"repos/{self._repository}/issues/{number}",
            )
        )
        if not isinstance(response, dict):
            raise CandidateBridgeError("issue response is invalid")
        return response.get("state") == "closed"

    def supersede_issue(
        self,
        number: int,
        body: str,
        marker: str,
    ) -> None:
        _positive(number, "issue_number")
        _marker(marker)
        if not self._has_bot_comment(number, marker):
            self._write(
                "POST",
                f"repos/{self._repository}/issues/{number}/comments",
                {"body": f"{marker}\n{body}"},
            )
        self._write(
            "PATCH",
            f"repos/{self._repository}/issues/{number}",
            {"state": "closed"},
        )

    def pull_request_is_superseded(
        self,
        number: int,
        marker: str,
    ) -> bool:
        _positive(number, "pull_request_number")
        _marker(marker)
        response = self._json(
            (
                "gh",
                "api",
                f"repos/{self._repository}/pulls/{number}",
            )
        )
        if not isinstance(response, dict):
            raise CandidateBridgeError(
                "pull request response is invalid"
            )
        return response.get("state") == "closed"

    def supersede_pull_request(
        self,
        number: int,
        body: str,
        marker: str,
    ) -> None:
        _positive(number, "pull_request_number")
        _marker(marker)
        if not self._has_bot_comment(number, marker):
            self._write(
                "POST",
                f"repos/{self._repository}/issues/{number}/comments",
                {"body": f"{marker}\n{body}"},
            )
        self._write(
            "PATCH",
            f"repos/{self._repository}/pulls/{number}",
            {"state": "closed"},
        )

    def _has_bot_comment(self, number: int, marker: str) -> bool:
        pages = self._json(
            (
                "gh",
                "api",
                "--paginate",
                "--slurp",
                (
                    f"repos/{self._repository}/issues/"
                    f"{number}/comments"
                ),
            )
        )
        if not isinstance(pages, list):
            raise CandidateBridgeError(
                "candidate comments response is invalid"
            )
        return any(
            isinstance(page, list)
            and any(
                isinstance(item, dict)
                and isinstance(item.get("user"), dict)
                and item["user"].get("login") == "github-actions[bot]"
                and marker in str(item.get("body", ""))
                for item in page
            )
            for page in pages
        )


class GhCandidatePullRequestReader:
    """Read and independently reproduce native Copilot candidate PRs."""

    def __init__(
        self,
        commands: CommandRunner,
        repository_root: Path,
        repository: str,
    ) -> None:
        if not _REPOSITORY.fullmatch(repository):
            raise ValueError("repository is invalid")
        self._commands = commands
        self._root = repository_root
        self._repository = repository
        self._patch = GitExactPatchApplier(SubprocessCommandRunner())

    def snapshots_for(
        self,
        request: CandidateSelectionRequest,
        bindings: tuple[CandidateBinding, ...],
    ) -> tuple[CandidatePullRequestSnapshot, ...]:
        repository = self._json(
            (
                "gh",
                "repo",
                "view",
                "--repo",
                self._repository,
                "--json",
                "defaultBranchRef",
            )
        )
        default_ref = (
            repository.get("defaultBranchRef")
            if isinstance(repository, dict)
            else None
        )
        default_branch = (
            default_ref.get("name")
            if isinstance(default_ref, dict)
            else None
        )
        if (
            not isinstance(default_branch, str)
            or not default_branch
        ):
            raise CandidateBridgeError(
                "GitHub default branch is invalid"
            )
        _git(
            self._root,
            "fetch",
            "--quiet",
            "origin",
            f"refs/heads/{default_branch}",
        )
        current_default_commit = _git_text(
            self._root,
            "rev-parse",
            "FETCH_HEAD^{commit}",
        )
        candidates: list[
            tuple[CandidateBinding, dict[str, Any], int | None]
        ] = []
        seen_numbers: set[int] = set()
        for binding in bindings:
            marker = candidate_pr_marker(binding)
            response = self._json(
                (
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    self._repository,
                    "--state",
                    "all",
                    "--search",
                    f'"{marker}" in:body',
                    "--json",
                    (
                        "number,body,author,isDraft,baseRefName,"
                        "headRefOid,state,mergeCommit,mergedBy"
                    ),
                    "--limit",
                    "10",
                )
            )
            if not isinstance(response, list):
                raise CandidateBridgeError(
                    "candidate pull request response is invalid"
                )
            matches = tuple(
                item
                for item in response
                if isinstance(item, dict)
                and marker in str(item.get("body", ""))
            )
            if not matches:
                continue
            for item in matches:
                number = item.get("number")
                if type(number) is int:
                    seen_numbers.add(number)
                candidates.append((binding, item, None))
        by_binding = {
            binding.binding_sha256: binding for binding in bindings
        }
        for observation in request.observed_pull_requests:
            if observation.pull_request_number in seen_numbers:
                continue
            binding = by_binding.get(observation.binding_sha256)
            if binding is None:
                raise CandidateBridgeError(
                    "observed candidate binding is stale"
                )
            item = self._json(
                (
                    "gh",
                    "pr",
                    "view",
                    str(observation.pull_request_number),
                    "--repo",
                    self._repository,
                    "--json",
                    (
                        "number,body,author,isDraft,baseRefName,"
                        "headRefOid,state,mergeCommit,mergedBy"
                    ),
                )
            )
            if not isinstance(item, dict):
                raise CandidateBridgeError(
                    "observed candidate pull request is invalid"
                )
            candidates.append(
                (binding, item, observation.worker_issue_number)
            )
        return tuple(
            self._snapshot(
                binding,
                item,
                worker_issue_number,
                default_branch,
                current_default_commit,
            )
            for binding, item, worker_issue_number in candidates
        )

    def _snapshot(
        self,
        binding: CandidateBinding,
        item: dict[str, Any],
        expected_worker_issue_number: int | None,
        current_default_branch: str,
        current_default_commit: str,
    ) -> CandidatePullRequestSnapshot:
        number = item.get("number")
        body = item.get("body")
        head_oid = item.get("headRefOid")
        author = item.get("author")
        if (
            type(number) is not int
            or number < 1
            or not isinstance(body, str)
            or not isinstance(head_oid, str)
            or re.fullmatch(r"[0-9a-f]{40}", head_oid) is None
            or not isinstance(author, dict)
            or not isinstance(author.get("login"), str)
        ):
            raise CandidateBridgeError(
                "candidate pull request identity is invalid"
            )
        _git(
            self._root,
            "fetch",
            "--quiet",
            "origin",
            f"pull/{number}/head",
        )
        fetched = _git_text(
            self._root,
            "rev-parse",
            "FETCH_HEAD^{commit}",
        )
        if fetched != head_oid:
            raise CandidateBridgeError(
                "candidate pull request head changed while reading"
            )
        parent = _git_text(
            self._root,
            "rev-parse",
            f"{fetched}^",
        )
        tree = self._patch.resolve_tree(self._root, fetched)
        if tree is None:
            raise CandidateBridgeError(
                "candidate pull request tree is unavailable"
            )
        patch = _git(
            self._root,
            "diff",
            "--binary",
            "--full-index",
            binding.base_commit,
            fetched,
            "--",
        )
        changed_paths = tuple(
            Path(value.decode("utf-8"))
            for value in _git(
                self._root,
                "diff",
                "--name-only",
                "-z",
                binding.base_commit,
                fetched,
                "--",
            ).split(b"\0")
            if value
        )
        checks_document = self._checks_json(
            (
                "gh",
                "pr",
                "checks",
                str(number),
                "--repo",
                self._repository,
                "--json",
                "name,bucket,state",
            )
        )
        if not isinstance(checks_document, list):
            raise CandidateBridgeError(
                "candidate checks response is invalid"
            )
        checks: dict[str, str] = {}
        for check in checks_document:
            if not isinstance(check, dict):
                raise CandidateBridgeError(
                    "candidate check is invalid"
                )
            name = check.get("name")
            bucket = str(check.get("bucket", "")).casefold()
            if not isinstance(name, str) or not name:
                raise CandidateBridgeError(
                    "candidate check identity is invalid"
                )
            checks[name] = {
                "pass": "success",
                "fail": "failure",
                "pending": "pending",
                "skipping": "skipped",
                "cancel": "cancelled",
            }.get(bucket, "pending")
        state_text = str(item.get("state", "")).upper()
        merge = item.get("mergeCommit")
        merge_commit = merge.get("oid") if isinstance(merge, dict) else None
        if state_text == "MERGED" or merge_commit is not None:
            state = CandidatePullRequestState.MERGED
        elif state_text == "CLOSED":
            state = CandidatePullRequestState.CLOSED
            merge_commit = None
        elif state_text == "OPEN":
            state = CandidatePullRequestState.OPEN
            merge_commit = None
        else:
            raise CandidateBridgeError(
                "candidate pull request state is invalid"
            )
        merge_parent_commit: str | None = None
        merge_tree_sha: str | None = None
        merge_reachable = False
        merge_actor: str | None = None
        if state is CandidatePullRequestState.MERGED:
            if (
                not isinstance(merge_commit, str)
                or re.fullmatch(r"[0-9a-f]{40}", merge_commit) is None
            ):
                raise CandidateBridgeError(
                    "candidate merge commit is invalid"
                )
            try:
                merge_parent_commit = _git_text(
                    self._root,
                    "rev-parse",
                    f"{merge_commit}^1",
                )
            except CandidateBridgeError:
                _git(
                    self._root,
                    "fetch",
                    "--quiet",
                    "origin",
                    merge_commit,
                )
                merge_parent_commit = _git_text(
                    self._root,
                    "rev-parse",
                    f"{merge_commit}^1",
                )
            merge_tree_sha = self._patch.resolve_tree(
                self._root,
                merge_commit,
            )
            if merge_tree_sha is None:
                raise CandidateBridgeError(
                    "candidate merge tree is unavailable"
                )
            merge_reachable = _git_is_ancestor(
                self._root,
                merge_commit,
                current_default_commit,
            )
            merged_by = item.get("mergedBy")
            if (
                isinstance(merged_by, dict)
                and isinstance(merged_by.get("login"), str)
            ):
                merge_actor = str(merged_by["login"])
        worker_issue = (
            expected_worker_issue_number
            if expected_worker_issue_number is not None
            else _body_integer(
                body,
                r"Candidate worker issue: #([1-9][0-9]*)",
            )
        )
        return CandidatePullRequestSnapshot(
            pull_request_number=number,
            worker_issue_number=worker_issue,
            state=state,
            author=str(author["login"]),
            draft=item.get("isDraft"),
            base_ref_name=str(item.get("baseRefName", "")),
            current_default_branch=current_default_branch,
            current_default_commit=current_default_commit,
            base_commit=parent,
            head_commit=fetched,
            head_parent_commit=parent,
            head_tree_sha=tree,
            patch_sha256=hashlib.sha256(patch).hexdigest(),
            changed_paths=changed_paths,
            body=body,
            checks=checks,
            binding_sha256=binding.binding_sha256,
            spec_sha256=_body_hash(body, "Spec SHA-256"),
            bundle_sha256=_body_hash(body, "Bundle SHA-256"),
            evidence_sha256=_body_hash(body, "Evidence SHA-256"),
            marker=(
                candidate_pr_marker(binding)
                if candidate_pr_marker(binding) in body
                else None
            ),
            merge_commit=merge_commit,
            merge_parent_commit=merge_parent_commit,
            merge_tree_sha=merge_tree_sha,
            merge_reachable_from_default=merge_reachable,
            merge_actor=merge_actor,
        )

    def _checks_json(self, arguments: tuple[str, ...]) -> Any:
        try:
            result = self._commands.run(arguments, cwd=self._root)
            raw = result.stdout
        except CommandExitError as error:
            raw = error.stdout
        except CommandError as error:
            raise CandidateBridgeError(
                "candidate checks are unavailable"
            ) from error
        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise CandidateBridgeError(
                "candidate checks response is not valid JSON"
            ) from error

    def _json(self, arguments: tuple[str, ...]) -> Any:
        result = self._commands.run(arguments, cwd=self._root)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise CandidateBridgeError(
                "GitHub response is not valid JSON"
            ) from error


def _marker(value: str) -> None:
    if not isinstance(value, str) or not _MARKER.fullmatch(value):
        raise ValueError("candidate marker is invalid")


def _positive(value: int, field_name: str) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{field_name} must be positive")


def _assignment_marker(marker: str) -> str:
    digest = hashlib.sha256(marker.encode("utf-8")).hexdigest()[:20]
    return f"<!-- foundry-opt:applier-assigned:{digest} -->"


def _body_integer(body: str, pattern: str) -> int:
    match = re.search(pattern, body)
    if match is None:
        raise CandidateBridgeError(
            "candidate pull request worker issue is missing"
        )
    return int(match.group(1))


def _body_hash(body: str, label: str) -> str | None:
    match = re.search(
        rf"{re.escape(label)}: `([0-9a-f]{{64}})`",
        body,
    )
    return match.group(1) if match is not None else None


def _git_text(root: Path, *arguments: str) -> str:
    return _git(root, *arguments).decode("ascii").strip()


def _git(root: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=root,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise CandidateBridgeError(
            completed.stderr.decode("utf-8", errors="replace").strip()
            or "Git candidate verification failed"
        )
    return completed.stdout


def _git_is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    completed = subprocess.run(
        ("git", "merge-base", "--is-ancestor", ancestor, descendant),
        cwd=root,
        check=False,
        capture_output=True,
    )
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    raise CandidateBridgeError(
        completed.stderr.decode("utf-8", errors="replace").strip()
        or "Git merge reachability check failed"
    )
