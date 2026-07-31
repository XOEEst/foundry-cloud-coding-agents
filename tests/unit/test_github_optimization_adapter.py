from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import pytest

from foundry_opt.adapters.commands import CommandExitError
from foundry_opt.adapters.github_optimization import (
    GhOptimizationGateway,
    GhOptimizationGatewayError,
    GhOptimizationResponseError,
    GitSpecPublisher,
    GitSpecPublisherError,
)
from foundry_opt.github_workflow.models import (
    GitHubCapabilities,
    IssueReference,
    PullRequestReference,
    RepositoryState,
)
from foundry_opt.optimization.specification import (
    SpecBranchConflictError,
    spec_issue_marker,
)
from foundry_opt.preflight.interfaces import CommandResult


_REPOSITORY = "octo-org/optimizer"
_ISSUE_NUMBER = 7


class FakeCommands:
    """Dict-keyed command double for static, argument-array-shaped calls."""

    def __init__(
        self,
        responses: dict[tuple[str, ...], str | Exception],
    ) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
        environment: Mapping[str, str] | None = None,
        input_text: str | None = None,
        input_bytes: bytes | None = None,
    ) -> CommandResult:
        command = tuple(arguments)
        self.calls.append(
            {
                "arguments": command,
                "cwd": cwd,
                "environment": dict(environment) if environment else None,
                "input_text": input_text,
                "input_bytes": input_bytes,
            }
        )
        response = self.responses.get(command, "")
        if isinstance(response, Exception):
            raise response
        return CommandResult(0, response, "")

    @property
    def invocations(self) -> list[tuple[str, ...]]:
        return [call["arguments"] for call in self.calls]


class ScriptedCommands:
    """Handler-based command double for calls with content-dependent or
    otherwise unpredictable arguments (blob/tree/commit SHAs, random
    temporary index paths).
    """

    def __init__(
        self,
        handler: Callable[[dict[str, Any]], CommandResult],
    ) -> None:
        self.handler = handler
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
        environment: Mapping[str, str] | None = None,
        input_text: str | None = None,
        input_bytes: bytes | None = None,
    ) -> CommandResult:
        command = tuple(arguments)
        record = {
            "arguments": command,
            "cwd": cwd,
            "environment": dict(environment) if environment else None,
            "input_text": input_text,
            "input_bytes": input_bytes,
        }
        self.calls.append(record)
        return self.handler(record)


def _issue_payload(
    *,
    number: int = _ISSUE_NUMBER,
    repository: str = _REPOSITORY,
    labels: tuple[str, ...] = ("needs-triage",),
    state: str = "open",
    body: str = "issue body",
    is_pull_request: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "number": number,
        "html_url": f"https://github.com/{repository}/issues/{number}",
        "title": "Optimize the support agent",
        "body": body,
        "state": state,
        "labels": [{"name": label} for label in labels],
    }
    if is_pull_request:
        payload["pull_request"] = {"url": "https://api.github.com/..."}
    return payload


def _pull_request_json(
    *,
    number: int = 55,
    repository: str = _REPOSITORY,
    head_branch: str = "foundry-opt/spec/issue-7/abc123",
    head_commit: str = "d" * 40,
    body: str = "",
    base_branch: str = "main",
    draft: bool = True,
    state: str = "OPEN",
) -> dict[str, Any]:
    return {
        "number": number,
        "url": f"https://github.com/{repository}/pull/{number}",
        "headRefName": head_branch,
        "headRefOid": head_commit,
        "isDraft": draft,
        "body": body,
        "baseRefName": base_branch,
        "state": state,
    }


def _gateway(
    commands: FakeCommands,
    *,
    granted: GitHubCapabilities = GitHubCapabilities.METADATA_READ
    | GitHubCapabilities.ISSUES_WRITE
    | GitHubCapabilities.CONTENTS_WRITE
    | GitHubCapabilities.PULL_REQUESTS_WRITE,
) -> GhOptimizationGateway:
    return GhOptimizationGateway(commands, granted_capabilities=granted)


def _with_origin(
    responses: dict[tuple[str, ...], str | Exception],
    *,
    repository: str = _REPOSITORY,
) -> dict[tuple[str, ...], str | Exception]:
    merged = {
        ("git", "remote", "get-url", "origin"): (
            f"https://github.com/{repository}.git\n"
        )
    }
    merged.update(responses)
    return merged


# ---------------------------------------------------------------------------
# GhOptimizationGateway
# ---------------------------------------------------------------------------


def test_verify_permissions_returns_the_intersection_of_required_and_granted() -> None:
    commands = FakeCommands({})
    gateway = _gateway(
        commands, granted=GitHubCapabilities.METADATA_READ
    )

    report = gateway.verify_permissions(
        GitHubCapabilities.METADATA_READ | GitHubCapabilities.ISSUES_WRITE
    )

    assert report.granted == GitHubCapabilities.METADATA_READ
    assert commands.calls == []


def test_repository_state_uses_argument_arrays_and_returns_pinned_commit() -> None:
    responses = _with_origin(
        {
            (
                "gh",
                "api",
                f"repos/{_REPOSITORY}",
                "--jq",
                ".default_branch",
            ): "main\n",
            (
                "gh",
                "api",
                f"repos/{_REPOSITORY}/commits/main",
                "--jq",
                ".sha",
            ): f"{'a' * 40}\n",
        }
    )
    commands = FakeCommands(responses)
    gateway = _gateway(commands)

    state = gateway.repository_state(Path("repository"))

    assert state == RepositoryState(_REPOSITORY, "main", "a" * 40)
    for call in commands.calls:
        assert isinstance(call["arguments"], tuple)
        assert all(isinstance(part, str) for part in call["arguments"])


def test_get_issue_parses_a_well_formed_open_issue() -> None:
    payload = _issue_payload(body="hello world")
    commands = FakeCommands(
        _with_origin(
            {
                (
                    "gh",
                    "api",
                    f"repos/{_REPOSITORY}/issues/{_ISSUE_NUMBER}",
                ): json.dumps(payload)
            }
        )
    )
    gateway = _gateway(commands)

    issue = gateway.get_issue(Path("repository"), _ISSUE_NUMBER)

    assert issue == IssueReference(
        _ISSUE_NUMBER,
        f"https://github.com/{_REPOSITORY}/issues/{_ISSUE_NUMBER}",
        "Optimize the support agent",
        "hello world",
        "OPEN",
        ("needs-triage",),
    )


def test_get_issue_never_treats_a_pull_request_as_an_issue() -> None:
    payload = _issue_payload(is_pull_request=True)
    commands = FakeCommands(
        _with_origin(
            {
                (
                    "gh",
                    "api",
                    f"repos/{_REPOSITORY}/issues/{_ISSUE_NUMBER}",
                ): json.dumps(payload)
            }
        )
    )
    gateway = _gateway(commands)

    issue = gateway.get_issue(Path("repository"), _ISSUE_NUMBER)

    assert issue is None


def test_get_issue_raises_on_malformed_json() -> None:
    commands = FakeCommands(
        _with_origin(
            {
                (
                    "gh",
                    "api",
                    f"repos/{_REPOSITORY}/issues/{_ISSUE_NUMBER}",
                ): "not json"
            }
        )
    )
    gateway = _gateway(commands)

    with pytest.raises(GhOptimizationResponseError):
        gateway.get_issue(Path("repository"), _ISSUE_NUMBER)


def test_get_issue_raises_when_the_response_shape_is_invalid() -> None:
    payload = _issue_payload(number=_ISSUE_NUMBER + 1)  # mismatched number
    commands = FakeCommands(
        _with_origin(
            {
                (
                    "gh",
                    "api",
                    f"repos/{_REPOSITORY}/issues/{_ISSUE_NUMBER}",
                ): json.dumps(payload)
            }
        )
    )
    gateway = _gateway(commands)

    with pytest.raises(GhOptimizationResponseError):
        gateway.get_issue(Path("repository"), _ISSUE_NUMBER)


def test_find_spec_pull_request_matches_only_the_marked_pull_request() -> None:
    marker = spec_issue_marker(_ISSUE_NUMBER)
    matching = _pull_request_json(number=55, body=f"{marker}\nSpec SHA-256: `x`")
    other = _pull_request_json(number=56, body="unrelated pull request")
    list_command = (
        "gh",
        "pr",
        "list",
        "--repo",
        _REPOSITORY,
        "--state",
        "open",
        "--search",
        f'"{marker}" in:body',
        "--json",
        "number,url,headRefName,headRefOid,isDraft,body,baseRefName,state",
        "--limit",
        "5",
    )
    commands = FakeCommands(
        _with_origin({list_command: json.dumps([matching, other])})
    )
    gateway = _gateway(commands)

    result = gateway.find_spec_pull_request(Path("repository"), _ISSUE_NUMBER)

    assert result is not None
    assert result.number == 55


def test_find_spec_pull_request_rejects_multiple_matches() -> None:
    marker = spec_issue_marker(_ISSUE_NUMBER)
    first = _pull_request_json(number=55, body=f"{marker}\nSpec SHA-256: `x`")
    second = _pull_request_json(number=56, body=f"{marker}\nSpec SHA-256: `y`")
    list_command = (
        "gh",
        "pr",
        "list",
        "--repo",
        _REPOSITORY,
        "--state",
        "open",
        "--search",
        f'"{marker}" in:body',
        "--json",
        "number,url,headRefName,headRefOid,isDraft,body,baseRefName,state",
        "--limit",
        "5",
    )
    commands = FakeCommands(
        _with_origin({list_command: json.dumps([first, second])})
    )
    gateway = _gateway(commands)

    with pytest.raises(GhOptimizationResponseError):
        gateway.find_spec_pull_request(Path("repository"), _ISSUE_NUMBER)


def test_comment_issue_passes_untrusted_body_through_stdin_only() -> None:
    adversarial_body = "safe text `; rm -rf / #` and $(whoami)"
    comment_command = (
        "gh",
        "issue",
        "comment",
        str(_ISSUE_NUMBER),
        "--repo",
        _REPOSITORY,
        "--body-file",
        "-",
    )
    commands = FakeCommands(_with_origin({comment_command: ""}))
    gateway = _gateway(commands)

    gateway.comment_issue(Path("repository"), _ISSUE_NUMBER, adversarial_body)

    matching_calls = [
        call for call in commands.calls if call["arguments"] == comment_command
    ]
    assert len(matching_calls) == 1
    assert matching_calls[0]["input_text"] == adversarial_body
    # The untrusted content must never appear inside the argument array.
    assert not any(
        "rm -rf" in part for call in commands.calls for part in call["arguments"]
    )


def test_has_issue_comment_detects_the_marker_substring() -> None:
    marker = "<!-- foundry-opt:spec-comment:issue-7 -->"
    comments_command = (
        "gh",
        "api",
        f"repos/{_REPOSITORY}/issues/{_ISSUE_NUMBER}/comments",
        "--paginate",
        "--jq",
        ".[].body",
    )
    commands = FakeCommands(_with_origin({comments_command: f"other\n{marker}\n"}))
    gateway = _gateway(commands)

    assert gateway.has_issue_comment(Path("repository"), _ISSUE_NUMBER, marker)
    assert not gateway.has_issue_comment(
        Path("repository"), _ISSUE_NUMBER, "<!-- not-present -->"
    )


def test_add_and_remove_labels_build_argument_arrays_without_shell_strings() -> None:
    commands = FakeCommands(_with_origin({}))
    gateway = _gateway(commands)

    gateway.add_labels(
        Path("repository"), _ISSUE_NUMBER, ("ready-for-agent",)
    )
    gateway.remove_labels(
        Path("repository"), _ISSUE_NUMBER, ("needs-triage",)
    )

    assert (
        "gh",
        "issue",
        "edit",
        str(_ISSUE_NUMBER),
        "--repo",
        _REPOSITORY,
        "--add-label",
        "ready-for-agent",
    ) in commands.invocations
    assert (
        "gh",
        "issue",
        "edit",
        str(_ISSUE_NUMBER),
        "--repo",
        _REPOSITORY,
        "--remove-label",
        "needs-triage",
    ) in commands.invocations


def test_gateway_errors_are_redacted_and_expose_only_the_operation() -> None:
    secret = "ghp_leaked0123456789leaked0123456789"
    command = (
        "gh",
        "api",
        f"repos/{_REPOSITORY}",
        "--jq",
        ".default_branch",
    )
    commands = FakeCommands(
        _with_origin(
            {
                command: CommandExitError(
                    command,
                    exit_code=403,
                    stdout="",
                    stderr=f"denied token={secret}",
                )
            }
        )
    )
    gateway = _gateway(commands)

    with pytest.raises(GhOptimizationGatewayError) as raised:
        gateway.repository_state(Path("repository"))

    assert raised.value.operation == "repository_metadata"
    assert secret not in str(raised.value)


# ---------------------------------------------------------------------------
# GitSpecPublisher.prepare_commit
# ---------------------------------------------------------------------------


def _plumbing_handler(
    *,
    base_commit: str,
    tree_sha: str = "e" * 40,
    commit_sha: str = "f" * 40,
    cat_file_failures: int = 0,
) -> tuple[Callable[[dict[str, Any]], CommandResult], dict[str, int]]:
    counters = {"cat_file": 0, "fetch": 0}

    def handler(record: dict[str, Any]) -> CommandResult:
        args: tuple[str, ...] = record["arguments"]
        if args[:3] == ("git", "cat-file", "-e"):
            counters["cat_file"] += 1
            if counters["cat_file"] <= cat_file_failures:
                raise CommandExitError(
                    args, exit_code=128, stdout="", stderr="fatal: bad object"
                )
            return CommandResult(0, "", "")
        if args[:2] == ("git", "fetch"):
            counters["fetch"] += 1
            assert args[-1] == base_commit
            return CommandResult(0, "", "")
        if args[:3] == ("git", "rev-parse", "--git-path"):
            return CommandResult(0, ".git/foundry-opt-spec-index-fake\n", "")
        if args[:2] == ("git", "read-tree"):
            assert record["environment"] is not None
            assert "GIT_INDEX_FILE" in record["environment"]
            return CommandResult(0, "", "")
        if args == ("git", "hash-object", "-w", "--stdin"):
            assert record["input_bytes"] is not None
            digest = hashlib.sha1(record["input_bytes"]).hexdigest()
            return CommandResult(0, f"{digest}\n", "")
        if args[:3] == ("git", "update-index", "--add"):
            return CommandResult(0, "", "")
        if args == ("git", "write-tree"):
            return CommandResult(0, f"{tree_sha}\n", "")
        if args[:2] == ("git", "commit-tree"):
            return CommandResult(0, f"{commit_sha}\n", "")
        raise AssertionError(f"unexpected git-spec-publisher command: {args}")

    return handler, counters


def test_prepare_commit_uses_plumbing_and_passes_content_through_stdin(
    tmp_path: Path,
) -> None:
    base_commit = "a" * 40
    handler, _ = _plumbing_handler(base_commit=base_commit)
    commands = ScriptedCommands(handler)
    publisher = GitSpecPublisher(commands)
    files = {
        Path("a.txt"): b"hello world\n",
        Path("dir/b.txt"): b"other content\n",
    }

    prepared = publisher.prepare_commit(
        tmp_path,
        base_commit=base_commit,
        files=files,
        message="foundry-opt: prepare optimization spec for issue #7\n",
    )

    assert prepared.head_commit == "f" * 40
    assert prepared.tree_sha == "e" * 40
    for call in commands.calls:
        assert isinstance(call["arguments"], tuple)
        assert all(isinstance(part, str) for part in call["arguments"])
    # File content is passed only through stdin, never as a literal argument.
    joined_arguments = " ".join(
        part for call in commands.calls for part in call["arguments"]
    )
    assert "hello world" not in joined_arguments
    assert "other content" not in joined_arguments
    stdin_payloads = [
        call["input_bytes"] for call in commands.calls if call["input_bytes"]
    ]
    assert b"hello world\n" in stdin_payloads
    assert b"other content\n" in stdin_payloads


def test_prepare_commit_fetches_the_base_commit_when_locally_unavailable(
    tmp_path: Path,
) -> None:
    base_commit = "a" * 40
    handler, counters = _plumbing_handler(
        base_commit=base_commit, cat_file_failures=1
    )
    commands = ScriptedCommands(handler)
    publisher = GitSpecPublisher(commands)

    publisher.prepare_commit(
        tmp_path,
        base_commit=base_commit,
        files={Path("a.txt"): b"content\n"},
        message="msg",
    )

    assert counters["fetch"] == 1
    assert counters["cat_file"] == 2


def test_prepare_commit_rejects_a_path_collision_that_is_not_portable(
    tmp_path: Path,
) -> None:
    handler, _ = _plumbing_handler(base_commit="a" * 40)
    commands = ScriptedCommands(handler)
    publisher = GitSpecPublisher(commands)

    with pytest.raises(ValueError):
        publisher.prepare_commit(
            tmp_path,
            base_commit="a" * 40,
            files={
                PurePosixPath("A.txt"): b"one\n",
                PurePosixPath("a.txt"): b"two\n",
            },
            message="msg",
        )


def test_prepare_commit_wraps_command_failures_without_leaking_output(
    tmp_path: Path,
) -> None:
    secret = "ghp_leaked0123456789leaked0123456789"

    def handler(record: dict[str, Any]) -> CommandResult:
        args = record["arguments"]
        if args[:3] == ("git", "cat-file", "-e"):
            return CommandResult(0, "", "")
        raise CommandExitError(
            args, exit_code=1, stdout="", stderr=f"denied token={secret}"
        )

    commands = ScriptedCommands(handler)
    publisher = GitSpecPublisher(commands)

    with pytest.raises(GitSpecPublisherError) as raised:
        publisher.prepare_commit(
            tmp_path,
            base_commit="a" * 40,
            files={Path("a.txt"): b"content\n"},
            message="msg",
        )

    assert secret not in str(raised.value)


def test_prepare_commit_pins_a_fixed_author_and_committer_identity(
    tmp_path: Path,
) -> None:
    # commit-tree must never depend on ambient git config or wall-clock time:
    # only a fully pinned author/committer identity makes the resulting
    # commit SHA reproducible across separate invocations for identical
    # inputs, which is required for exact pull-request idempotency.
    base_commit = "a" * 40
    handler, _ = _plumbing_handler(base_commit=base_commit)
    commands = ScriptedCommands(handler)
    publisher = GitSpecPublisher(commands)

    publisher.prepare_commit(
        tmp_path,
        base_commit=base_commit,
        files={Path("a.txt"): b"content\n"},
        message="msg",
    )

    commit_tree_calls = [
        call for call in commands.calls if call["arguments"][:2] == ("git", "commit-tree")
    ]
    assert len(commit_tree_calls) == 1
    environment = commit_tree_calls[0]["environment"]
    assert environment is not None
    assert environment["GIT_AUTHOR_NAME"] == "foundry-opt-bot"
    assert environment["GIT_AUTHOR_EMAIL"]
    assert environment["GIT_AUTHOR_DATE"]
    assert environment["GIT_COMMITTER_NAME"] == "foundry-opt-bot"
    assert environment["GIT_COMMITTER_EMAIL"]
    assert environment["GIT_COMMITTER_DATE"]


def test_prepare_commit_uses_the_identical_identity_across_separate_calls(
    tmp_path: Path,
) -> None:
    base_commit = "a" * 40
    handler, _ = _plumbing_handler(base_commit=base_commit)
    commands = ScriptedCommands(handler)
    publisher = GitSpecPublisher(commands)
    files = {Path("a.txt"): b"content\n"}

    publisher.prepare_commit(
        tmp_path, base_commit=base_commit, files=files, message="msg"
    )
    first_environment = next(
        call["environment"]
        for call in commands.calls
        if call["arguments"][:2] == ("git", "commit-tree")
    )

    commands.calls.clear()
    publisher.prepare_commit(
        tmp_path, base_commit=base_commit, files=files, message="msg"
    )
    second_environment = next(
        call["environment"]
        for call in commands.calls
        if call["arguments"][:2] == ("git", "commit-tree")
    )

    assert first_environment == second_environment


# ---------------------------------------------------------------------------
# GitSpecPublisher.publish
# ---------------------------------------------------------------------------


def _publish_handler(
    *,
    repository: str = _REPOSITORY,
    remote_line: str = "",
    pr_number: int = 42,
) -> Callable[[dict[str, Any]], CommandResult]:
    def handler(record: dict[str, Any]) -> CommandResult:
        args: tuple[str, ...] = record["arguments"]
        if args == ("git", "remote", "get-url", "origin"):
            return CommandResult(0, f"https://github.com/{repository}.git\n", "")
        if args[:4] == ("git", "ls-remote", "--heads", "origin"):
            return CommandResult(0, remote_line, "")
        if args[:2] == ("git", "push"):
            return CommandResult(0, "", "")
        if args[:3] == ("gh", "pr", "create"):
            assert record["input_text"] is not None
            return CommandResult(
                0, f"https://github.com/{repository}/pull/{pr_number}\n", ""
            )
        raise AssertionError(f"unexpected git-spec-publisher command: {args}")

    return handler


def test_publish_pushes_and_opens_a_draft_pull_request_with_body_via_stdin(
    tmp_path: Path,
) -> None:
    adversarial_body = "spec details `; rm -rf / #` and $(whoami)"
    commands = ScriptedCommands(_publish_handler())
    publisher = GitSpecPublisher(commands)

    reference = publisher.publish(
        tmp_path,
        base_branch="main",
        branch="foundry-opt/spec/issue-7/abc123456789",
        commit_sha="d" * 40,
        title="[foundry-opt] Optimization spec for issue #7",
        body=adversarial_body,
    )

    assert reference == PullRequestReference(
        42,
        f"https://github.com/{_REPOSITORY}/pull/42",
        "foundry-opt/spec/issue-7/abc123456789",
        "d" * 40,
        True,
        adversarial_body,
        "main",
        "OPEN",
    )
    push_calls = [
        call for call in commands.calls if call["arguments"][:2] == ("git", "push")
    ]
    assert len(push_calls) == 1
    assert push_calls[0]["arguments"][-1] == (
        f"{'d' * 40}:refs/heads/foundry-opt/spec/issue-7/abc123456789"
    )
    create_calls = [
        call
        for call in commands.calls
        if call["arguments"][:3] == ("gh", "pr", "create")
    ]
    assert len(create_calls) == 1
    assert create_calls[0]["input_text"] == adversarial_body
    assert "--body-file" in create_calls[0]["arguments"]
    assert not any(
        "rm -rf" in part for part in create_calls[0]["arguments"]
    )


def test_publish_skips_the_push_when_the_remote_branch_already_matches(
    tmp_path: Path,
) -> None:
    branch = "foundry-opt/spec/issue-7/abc123456789"
    commit_sha = "d" * 40
    remote_line = f"{commit_sha}\trefs/heads/{branch}\n"
    commands = ScriptedCommands(_publish_handler(remote_line=remote_line))
    publisher = GitSpecPublisher(commands)

    publisher.publish(
        tmp_path,
        base_branch="main",
        branch=branch,
        commit_sha=commit_sha,
        title="title",
        body="body",
    )

    assert not any(
        call["arguments"][:2] == ("git", "push") for call in commands.calls
    )


def test_publish_raises_branch_conflict_when_remote_branch_points_elsewhere(
    tmp_path: Path,
) -> None:
    branch = "foundry-opt/spec/issue-7/abc123456789"
    remote_line = f"{'c' * 40}\trefs/heads/{branch}\n"
    commands = ScriptedCommands(_publish_handler(remote_line=remote_line))
    publisher = GitSpecPublisher(commands)

    with pytest.raises(SpecBranchConflictError) as raised:
        publisher.publish(
            tmp_path,
            base_branch="main",
            branch=branch,
            commit_sha="d" * 40,
            title="title",
            body="body",
        )

    assert raised.value.remote_commit == "c" * 40
    assert not any(
        call["arguments"][:3] == ("gh", "pr", "create") for call in commands.calls
    )
