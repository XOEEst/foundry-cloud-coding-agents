from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from pathlib import Path

import pytest

from foundry_opt.adapters.commands import CommandExitError
from foundry_opt.adapters.github_reconcile import GhCandidateReconcileGateway
from foundry_opt.github_workflow import (
    GitHubCapabilities,
    PullRequestReference,
)
from foundry_opt.preflight.interfaces import CommandResult


REPOSITORY = "octo-org/agents"
COMMIT = "c" * 40
TREE = "d" * 40
MERGE_COMMIT = "e" * 40
MARKER = "foundry-opt:candidate-pr:issue-7:candidate-1:"


class FakeRunner:
    def __init__(self) -> None:
        self.rules: list[tuple[object, object]] = []
        self.calls: list[tuple[str, ...]] = []
        # Default: resolve the repository from the origin remote.
        self.add(
            _has("git", "remote", "get-url", "origin"),
            f"https://github.com/{REPOSITORY}.git\n",
        )

    def add(self, predicate: object, value: object) -> "FakeRunner":
        self.rules.append((predicate, value))
        return self

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
        environment: Mapping[str, str] | None = None,
        input_text: str | None = None,
        input_bytes: bytes | None = None,
    ) -> CommandResult:
        args = tuple(arguments)
        self.calls.append(args)
        for predicate, value in self.rules:
            if predicate(args):  # type: ignore[operator]
                if isinstance(value, Exception):
                    raise value
                return CommandResult(0, str(value), "")
        raise CommandExitError(
            list(args),
            exit_code=1,
            stdout="",
            stderr="unmatched command",
        )


def _has(*fragments: str):
    def predicate(args: tuple[str, ...]) -> bool:
        return all(fragment in args for fragment in fragments)

    return predicate


def _api_path_contains(fragment: str):
    def predicate(args: tuple[str, ...]) -> bool:
        return (
            len(args) >= 3
            and args[0] == "gh"
            and args[1] == "api"
            and any(fragment in part for part in args)
        )

    return predicate


def _gateway(
    runner: FakeRunner,
    *,
    granted: GitHubCapabilities = (
        GitHubCapabilities.MERGE | GitHubCapabilities.DEPLOY_DISPATCH
    ),
    root: Path | None = None,
) -> GhCandidateReconcileGateway:
    return GhCandidateReconcileGateway(
        runner,
        root or Path("."),
        granted_capabilities=granted,
    )


def _pull_request(number: int = 401) -> PullRequestReference:
    return PullRequestReference(
        number=number,
        url=f"https://github.com/{REPOSITORY}/pull/{number}",
        head_branch="foundry-opt/issue-7/candidate-1/lifecycle",
        head_commit=COMMIT,
        draft=False,
        body="candidate",
        base_branch="main",
        state="OPEN",
    )


def test_verify_permissions_masks_to_granted() -> None:
    gateway = _gateway(FakeRunner(), granted=GitHubCapabilities.MERGE)

    report = gateway.verify_permissions(
        GitHubCapabilities.MERGE | GitHubCapabilities.DEPLOY_DISPATCH
    )

    assert report.granted is GitHubCapabilities.MERGE


def test_requires_explicit_capabilities() -> None:
    with pytest.raises(ValueError):
        GhCandidateReconcileGateway(
            FakeRunner(),
            Path("."),
            granted_capabilities=None,
        )


def test_locate_candidate_pull_request_matches_marker() -> None:
    runner = FakeRunner().add(
        _has("gh", "pr", "list"),
        json.dumps(
            [
                {
                    "number": 401,
                    "url": (
                        f"https://github.com/{REPOSITORY}/pull/401"
                    ),
                    "headRefName": (
                        "foundry-opt/issue-7/candidate-1/lifecycle"
                    ),
                    "headRefOid": COMMIT,
                    "isDraft": False,
                    "body": f"body <!-- {MARKER}sess --> tail",
                    "baseRefName": "main",
                    "state": "OPEN",
                }
            ]
        ),
    )

    pull_request = _gateway(runner).locate_candidate_pull_request(
        Path("."), "issue-7", "candidate-1"
    )

    assert pull_request is not None
    assert pull_request.number == 401
    assert pull_request.base_branch == "main"
    # All states are queried so a human-merged pull request is still located.
    list_call = next(c for c in runner.calls if "list" in c and "pr" in c)
    assert "--state" in list_call
    assert list_call[list_call.index("--state") + 1] == "all"


def _pr_json(number: int, state: str, candidate: str = "candidate-1") -> dict:
    marker = f"foundry-opt:candidate-pr:issue-7:{candidate}:s"
    return {
        "number": number,
        "url": f"https://github.com/{REPOSITORY}/pull/{number}",
        "headRefName": f"foundry-opt/issue-7/{candidate}/lifecycle",
        "headRefOid": COMMIT,
        "isDraft": False,
        "body": f"body <!-- {marker} -->",
        "baseRefName": "main",
        "state": state,
    }


def test_locate_candidate_pull_request_detects_merged_selection() -> None:
    runner = FakeRunner().add(
        _has("gh", "pr", "list"),
        json.dumps([_pr_json(401, "MERGED")]),
    )

    pull_request = _gateway(runner).locate_candidate_pull_request(
        Path("."), "issue-7", "candidate-1"
    )

    assert pull_request is not None
    assert pull_request.state == "MERGED"


def test_locate_prefers_merged_over_closed_duplicate() -> None:
    runner = FakeRunner().add(
        _has("gh", "pr", "list"),
        json.dumps([_pr_json(400, "CLOSED"), _pr_json(401, "MERGED")]),
    )

    pull_request = _gateway(runner).locate_candidate_pull_request(
        Path("."), "issue-7", "candidate-1"
    )

    assert pull_request is not None
    assert pull_request.number == 401
    assert pull_request.state == "MERGED"


def test_locate_rejects_ambiguous_open_duplicates() -> None:
    from foundry_opt.adapters.github_reconcile import (
        GitHubReconcileResponseError,
    )

    runner = FakeRunner().add(
        _has("gh", "pr", "list"),
        json.dumps([_pr_json(401, "OPEN"), _pr_json(402, "OPEN")]),
    )

    with pytest.raises(GitHubReconcileResponseError):
        _gateway(runner).locate_candidate_pull_request(
            Path("."), "issue-7", "candidate-1"
        )
    runner = FakeRunner().add(_has("gh", "pr", "list"), "[]")

    pull_request = _gateway(runner).locate_candidate_pull_request(
        Path("."), "issue-7", "candidate-1"
    )

    assert pull_request is None


def test_candidate_checks_normalizes_buckets() -> None:
    runner = FakeRunner().add(
        _has("gh", "pr", "checks"),
        json.dumps(
            [
                {"name": "spec", "bucket": "pass", "state": "SUCCESS"},
                {"name": "exact", "bucket": "fail", "state": "FAILURE"},
                {"name": "lint", "bucket": "skipping", "state": "SKIPPED"},
            ]
        ),
    )

    checks = _gateway(runner).candidate_checks(Path("."), _pull_request())

    assert checks == {
        "spec": "success",
        "exact": "failure",
        "lint": "skipped",
    }


def test_resolve_merge_commit_reads_oid() -> None:
    runner = FakeRunner().add(
        _has("gh", "pr", "view"),
        json.dumps(
            {
                "mergeCommit": {"oid": MERGE_COMMIT},
                "state": "MERGED",
                "mergedAt": "2026-07-28T00:00:00Z",
            }
        ),
    )

    assert (
        _gateway(runner).resolve_merge_commit(Path("."), 401)
        == MERGE_COMMIT
    )


def test_resolve_tree_returns_tree() -> None:
    runner = FakeRunner().add(_has("git", "rev-parse"), f"{TREE}\n")

    assert _gateway(runner).resolve_tree(Path("."), MERGE_COMMIT) == TREE


def test_resolve_tree_returns_none_when_unfetchable() -> None:
    runner = (
        FakeRunner()
        .add(_has("git", "rev-parse"), "not-a-tree\n")
        .add(
            _has("git", "fetch"),
            CommandExitError(
                ["git", "fetch"], exit_code=1, stdout="", stderr="no"
            ),
        )
    )

    assert _gateway(runner).resolve_tree(Path("."), MERGE_COMMIT) is None


def _protection(
    *,
    checks: bool = True,
    reviews: bool = True,
    enforce_admins: bool = True,
) -> str:
    document: dict[str, object] = {}
    if checks:
        document["required_status_checks"] = {
            "strict": True,
            "contexts": ["foundry-opt/exact-patch"],
        }
    if reviews:
        document["required_pull_request_reviews"] = {
            "required_approving_review_count": 1
        }
    document["enforce_admins"] = {"enabled": enforce_admins}
    return json.dumps(document)


def test_branch_protection_allows_protected_branch() -> None:
    runner = (
        FakeRunner()
        .add(_api_path_contains("/protection"), _protection())
        .add(_api_path_contains("/permission"), "push")
    )

    assert (
        _gateway(runner).branch_protection_allows(
            Path("."), _pull_request(), "foundry-opt-merge-app"
        )
        is True
    )


def test_branch_protection_rejects_admin_bypass() -> None:
    runner = (
        FakeRunner()
        .add(
            _api_path_contains("/protection"),
            _protection(enforce_admins=False),
        )
        .add(_api_path_contains("/permission"), "admin")
    )

    assert (
        _gateway(runner).branch_protection_allows(
            Path("."), _pull_request(), "foundry-opt-merge-app"
        )
        is False
    )


def test_branch_protection_rejects_unprotected_branch() -> None:
    runner = FakeRunner().add(
        _api_path_contains("/protection"),
        _protection(checks=False),
    )

    assert (
        _gateway(runner).branch_protection_allows(
            Path("."), _pull_request(), "foundry-opt-merge-app"
        )
        is False
    )


def test_branch_protection_rejects_insufficient_permission() -> None:
    runner = (
        FakeRunner()
        .add(_api_path_contains("/protection"), _protection())
        .add(_api_path_contains("/permission"), "read")
    )

    assert (
        _gateway(runner).branch_protection_allows(
            Path("."), _pull_request(), "foundry-opt-merge-app"
        )
        is False
    )


def test_merge_pull_request_never_uses_admin_bypass() -> None:
    runner = FakeRunner().add(_has("gh", "pr", "merge"), "")

    _gateway(runner).merge_pull_request(
        Path("."), 401, COMMIT, "foundry-opt-merge-app"
    )

    merge_calls = [c for c in runner.calls if "merge" in c and "pr" in c]
    assert len(merge_calls) == 1
    assert "--admin" not in merge_calls[0]
    assert "--match-head-commit" in merge_calls[0]
    assert COMMIT in merge_calls[0]


def test_dispatch_deployment_runs_manual_workflow(tmp_path: Path) -> None:
    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "deploy.yml").write_text(
        "name: Deploy Foundry agent\n"
        "on:\n  workflow_dispatch: {}\n"
        "jobs:\n"
        "  publish:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo publish foundry agent\n",
        encoding="utf-8",
    )
    runner = (
        FakeRunner()
        .add(_api_path_contains("repos/"), "main")
        .add(_has("gh", "workflow", "run"), "")
    )

    _gateway(runner, root=tmp_path).dispatch_deployment(
        tmp_path, 401, COMMIT
    )

    dispatch_calls = [
        c for c in runner.calls if "workflow" in c and "run" in c
    ]
    assert len(dispatch_calls) == 1
    assert ".github/workflows/deploy.yml" in dispatch_calls[0]
    assert "main" in dispatch_calls[0]


def test_dispatch_deployment_rejects_missing_workflow(tmp_path: Path) -> None:
    with pytest.raises(Exception):
        _gateway(FakeRunner(), root=tmp_path).dispatch_deployment(
            tmp_path, 401, COMMIT
        )
