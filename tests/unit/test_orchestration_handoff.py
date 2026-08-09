from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import json

import pytest

from foundry_opt.adapters.commands import CommandExitError
from foundry_opt.preflight.interfaces import CommandResult
from foundry_opt.orchestration.handoff import (
    _ProductionHandoffEffects,
    CandidateDesignHandoff,
    GhHandoffPullRequestGateway,
    HandoffApplyResult,
    HandoffApplyStatus,
    HandoffError,
    HandoffEventError,
    HandoffFinalizer,
    StewardStateHandoff,
    TrustedHandoffContext,
    discover_trusted_handoff_requests,
    trusted_handoff_request_from_payload,
)


BASE = "a" * 40
HEAD = "b" * 40
BLOB = "c" * 40
PATH = (
    ".foundry-optimizer/handoffs/steward/issue-31/g1/"
    + "d" * 64
    + ".json"
)


def _pull_request() -> dict[str, object]:
    return {
        "base": {
            "ref": "trunk",
            "repo": {"full_name": "octo-org/optimizer"},
            "sha": BASE,
        },
        "body": None,
        "head": {
            "ref": "copilot/steward-issue-31",
            "repo": {"full_name": "octo-org/optimizer"},
            "sha": HEAD,
        },
        "merged": False,
        "number": 90,
        "state": "open",
        "title": "Advance optimization issue 31",
        "user": {"login": "copilot-swe-agent[bot]"},
        "created_at": "2026-08-05T20:54:42Z",
    }


def _payload() -> dict[str, object]:
    return {
        "action": "opened",
        "pull_request": _pull_request(),
        "repository": {
            "default_branch": "trunk",
            "full_name": "octo-org/optimizer",
            "id": 123,
        },
        "sender": {
            "html_url": "https://github.com/apps/copilot-swe-agent",
            "id": 198982749,
            "login": "Copilot",
            "type": "Bot",
        },
    }


class Gateway:
    def __init__(self) -> None:
        self.pull_request = _pull_request()
        self.files = [
            {
                "filename": PATH,
                "sha": BLOB,
                "status": "added",
            }
        ]
        self.fetched: list[str] = []

    def get_pull_request(self, number: int):
        assert number == 90
        return self.pull_request

    def get_pull_request_files(self, number: int):
        assert number == 90
        return self.files

    def fetch_revision(self, revision: str) -> str:
        self.fetched.append(revision)
        return revision

    def head_has_copilot_session_attestation(
        self,
        number: int,
        branch: str,
        revision: str,
    ) -> bool:
        return True


def _live_copilot_pull_request(
    number: int,
    *,
    created_at: str,
) -> dict[str, object]:
    pull_request = deepcopy(_pull_request())
    pull_request["number"] = number
    pull_request["created_at"] = created_at
    pull_request["draft"] = True
    pull_request["head"]["ref"] = f"copilot/steward-issue-{number}"
    pull_request["head"]["sha"] = f"{number % 16:x}" * 40
    pull_request["user"] = {
        "html_url": "https://github.com/apps/copilot-swe-agent",
        "id": 198982749,
        "login": "Copilot",
        "type": "Bot",
    }
    return pull_request


class DiscoveryGateway:
    def __init__(
        self,
        pull_requests: list[dict[str, object]],
    ) -> None:
        self.pull_requests = pull_requests
        self.live = {
            int(pull_request["number"]): pull_request
            for pull_request in pull_requests
            if isinstance(pull_request.get("number"), int)
        }
        self.files = {
            number: [
                {
                    "filename": PATH,
                    "sha": BLOB,
                    "status": "added",
                }
            ]
            for number in self.live
        }
        self.fetched: list[str] = []
        self.copilot_pushes = {
            str(pull_request["head"]["sha"]): True
            for pull_request in self.live.values()
        }

    def list_open_pull_requests(self):
        return self.pull_requests

    def get_pull_request(self, number: int):
        return self.live[number]

    def get_pull_request_files(self, number: int):
        return self.files[number]

    def fetch_revision(self, revision: str) -> str:
        self.fetched.append(revision)
        return revision

    def head_has_copilot_session_attestation(
        self,
        number: int,
        branch: str,
        revision: str,
    ) -> bool:
        assert number in self.live
        return self.copilot_pushes.get(revision, False)


def test_scheduled_discovery_filters_orders_and_deduplicates_candidates() -> None:
    newest = _live_copilot_pull_request(
        92,
        created_at="2026-08-05T20:56:42Z",
    )
    newest["user"].pop("html_url")
    newest["statusCheckRollup"] = [{"conclusion": "action_required"}]
    oldest = _live_copilot_pull_request(
        91,
        created_at="2026-08-05T20:54:42Z",
    )
    wrong_author = _live_copilot_pull_request(
        93,
        created_at="2026-08-05T20:53:42Z",
    )
    wrong_author["user"] = {
        "id": 1,
        "login": "attacker",
        "type": "User",
    }
    fork = _live_copilot_pull_request(
        94,
        created_at="2026-08-05T20:52:42Z",
    )
    fork["head"]["repo"]["full_name"] = "attacker/fork"
    wrong_branch = _live_copilot_pull_request(
        95,
        created_at="2026-08-05T20:51:42Z",
    )
    wrong_branch["head"]["ref"] = "feature/not-a-handoff"
    wrong_base = _live_copilot_pull_request(
        96,
        created_at="2026-08-05T20:50:42Z",
    )
    wrong_base["base"]["ref"] = "release"
    closed = _live_copilot_pull_request(
        97,
        created_at="2026-08-05T20:49:42Z",
    )
    closed["state"] = "closed"
    renamed = _live_copilot_pull_request(
        98,
        created_at="2026-08-05T20:48:42Z",
    )
    wrong_pusher = _live_copilot_pull_request(
        99,
        created_at="2026-08-05T20:47:42Z",
    )
    gateway = DiscoveryGateway(
        [
            newest,
            wrong_author,
            deepcopy(oldest),
            fork,
            oldest,
            wrong_branch,
            wrong_base,
            closed,
            renamed,
            wrong_pusher,
        ]
    )
    newest_summary = deepcopy(newest)
    newest_summary.pop("merged")
    newest_summary["merged_at"] = None
    gateway.pull_requests[0] = newest_summary
    gateway.files[98][0]["previous_filename"] = "README.md"
    gateway.copilot_pushes[str(wrong_pusher["head"]["sha"])] = False

    requests = discover_trusted_handoff_requests(
        TrustedHandoffContext(
            "schedule",
            "octo-org/optimizer",
            123,
            "trunk",
        ),
        Path("repository"),
        gateway,
        limit=10,
    )

    assert [request.pull_request_number for request in requests] == [91, 92]
    assert [request.author_login for request in requests] == [
        "Copilot",
        "Copilot",
    ]
    assert gateway.fetched == [
        BASE,
        oldest["head"]["sha"],
        BASE,
        newest["head"]["sha"],
    ]

    limited_gateway = DiscoveryGateway([newest, oldest])
    limited = discover_trusted_handoff_requests(
        TrustedHandoffContext(
            "schedule",
            "octo-org/optimizer",
            123,
            "trunk",
        ),
        Path("repository"),
        limited_gateway,
        limit=1,
    )
    assert [request.pull_request_number for request in limited] == [91]


def test_dispatch_retry_still_requires_exact_single_handoff_file() -> None:
    pull_request = _live_copilot_pull_request(
        90,
        created_at="2026-08-05T20:54:42Z",
    )
    gateway = DiscoveryGateway([pull_request])
    gateway.files[90].append(
        {
            "filename": "agent/instructions.md",
            "sha": "e" * 40,
            "status": "modified",
        }
    )

    with pytest.raises(HandoffEventError, match="exactly one"):
        discover_trusted_handoff_requests(
            TrustedHandoffContext(
                "workflow_dispatch",
                "octo-org/optimizer",
                123,
                "trunk",
            ),
            Path("repository"),
            gateway,
            requested_pull_request=90,
        )


def test_dispatch_retry_rejects_designer_handoff_with_payload_files(
) -> None:
    pull_request = _live_copilot_pull_request(
        90,
        created_at="2026-08-05T20:54:42Z",
    )
    gateway = DiscoveryGateway([pull_request])
    designer_path = (
        ".foundry-optimizer/handoffs/designer/issue-31/g1/"
        + "d" * 64
        + ".json"
    )
    gateway.files[90] = [
        {
            "filename": designer_path,
            "sha": BLOB,
            "status": "added",
        },
        {
            "filename": "agent/main.py",
            "sha": "e" * 40,
            "status": "modified",
        },
        {
            "filename": "tests/test_agent_unit.py",
            "sha": "f" * 40,
            "status": "modified",
        },
    ]

    with pytest.raises(HandoffEventError, match="exactly one"):
        discover_trusted_handoff_requests(
            TrustedHandoffContext(
                "workflow_dispatch",
                "octo-org/optimizer",
                123,
                "trunk",
            ),
            Path("repository"),
            gateway,
            requested_pull_request=90,
        )


def test_trusted_event_accepts_only_current_exact_copilot_handoff() -> None:
    gateway = Gateway()

    request = trusted_handoff_request_from_payload(
        _payload(),
        TrustedHandoffContext(
            event_name="pull_request_target",
            repository="octo-org/optimizer",
            repository_id=123,
            default_branch="trunk",
        ),
        Path("repository"),
        gateway,
    )

    assert request.pull_request_number == 90
    assert request.base_ref == "trunk"
    assert request.base_revision == BASE
    assert request.head_revision == HEAD
    assert request.handoff_path == PATH
    assert request.handoff_blob == BLOB
    assert gateway.fetched == [BASE, HEAD]


def test_trusted_event_rejects_non_copilot_sender() -> None:
    payload = _payload()
    payload["sender"] = {
        "id": 1,
        "login": "collaborator",
        "type": "User",
    }

    with pytest.raises(HandoffEventError, match="sender"):
        trusted_handoff_request_from_payload(
            payload,
            TrustedHandoffContext(
                "pull_request_target",
                "octo-org/optimizer",
                123,
                "trunk",
            ),
            Path("repository"),
            Gateway(),
        )


def test_trusted_event_retry_accepts_already_closed_internal_pr() -> None:
    gateway = Gateway()
    gateway.pull_request["state"] = "closed"

    request = trusted_handoff_request_from_payload(
        _payload(),
        TrustedHandoffContext(
            "pull_request_target",
            "octo-org/optimizer",
            123,
            "trunk",
        ),
        Path("repository"),
        gateway,
    )

    assert request.pull_request_number == 90
    assert request.head_revision == HEAD


def test_trusted_event_rejects_extra_file_fork_and_stale_head() -> None:
    gateway = Gateway()
    gateway.files.append(
        {
            "filename": "agent/instructions.md",
            "sha": "e" * 40,
            "status": "modified",
        }
    )
    with pytest.raises(HandoffEventError, match="exactly one"):
        trusted_handoff_request_from_payload(
            _payload(),
            TrustedHandoffContext(
                "pull_request_target",
                "octo-org/optimizer",
                123,
                "trunk",
            ),
            Path("repository"),
            gateway,
        )


class Commands:
    def __init__(self) -> None:
        self.responses = [
            json.dumps(_pull_request()),
            json.dumps([{
                "filename": PATH,
                "sha": BLOB,
                "status": "added",
            }]),
            "",
            HEAD,
            "",
            f"{HEAD}\trefs/heads/copilot/steward-issue-31",
            "",
            "",
            "",
        ]
        self.calls: list[dict[str, object]] = []

    def run(
        self,
        arguments,
        *,
        cwd=None,
        environment=None,
        input_text=None,
        input_bytes=None,
    ) -> CommandResult:
        self.calls.append(
            {
                "arguments": tuple(arguments),
                "cwd": cwd,
                "environment": environment,
                "input_text": input_text,
            }
        )
        return CommandResult(0, self.responses.pop(0), "")


def test_github_handoff_gateway_never_checks_out_or_executes_pr_content() -> None:
    commands = Commands()
    gateway = GhHandoffPullRequestGateway(
        commands,
        Path("repository"),
        "octo-org/optimizer",
    )

    assert gateway.get_pull_request(90)["number"] == 90
    assert gateway.get_pull_request_files(90)[0]["filename"] == PATH
    assert gateway.fetch_revision(HEAD) == HEAD
    gateway.close_internal_pull_request(
        90,
        handoff_id="d" * 64,
        kind="steward_state",
    )
    assert gateway.delete_branch_if_head(
        "copilot/steward-issue-31",
        HEAD,
    ) is True
    assert gateway.delete_branch_if_head(
        "copilot/steward-issue-31",
        HEAD,
    ) is True

    arguments = [call["arguments"] for call in commands.calls]
    assert (
        "git",
        "fetch",
        "--quiet",
        "--no-tags",
        "origin",
        HEAD,
    ) in arguments
    assert (
        "gh",
        "api",
        "repos/octo-org/optimizer/pulls/90/files?per_page=100",
    ) in arguments
    assert all("--paginate" not in call for call in arguments)
    assert all("checkout" not in call for call in arguments)
    assert all(call["environment"] is None for call in commands.calls)
    close_call = commands.calls[4]
    close_payload = json.loads(str(close_call["input_text"]))
    assert close_payload["state"] == "closed"
    assert close_payload["title"].startswith("[internal]")
    assert "<!-- foundry-opt:internal-handoff:" in close_payload["body"]
    assert close_call["arguments"] == (
        "gh",
        "api",
        "--method",
        "PATCH",
        "repos/octo-org/optimizer/issues/90",
        "--input",
        "-",
    )
    assert (
        "git",
        "push",
        f"--force-with-lease=refs/heads/copilot/steward-issue-31:{HEAD}",
        "origin",
        ":refs/heads/copilot/steward-issue-31",
    ) in arguments


class DiscoveryCommands:
    def __init__(self, responses: list[object]) -> None:
        self.responses = [
            json.dumps(response)
            for response in responses
        ]
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        arguments,
        *,
        cwd=None,
        environment=None,
        input_text=None,
        input_bytes=None,
    ) -> CommandResult:
        self.calls.append(tuple(arguments))
        return CommandResult(0, self.responses.pop(0), "")


def test_github_discovery_paginates_explicitly_and_fails_on_bound() -> None:
    item = _live_copilot_pull_request(
        90,
        created_at="2026-08-05T20:54:42Z",
    )
    commands = DiscoveryCommands([[item] * 100, [item]])
    gateway = GhHandoffPullRequestGateway(
        commands,
        Path("repository"),
        "octo-org/optimizer",
    )

    assert len(gateway.list_open_pull_requests()) == 101
    assert all("--paginate" not in call for call in commands.calls)
    assert commands.calls == [
        (
            "gh",
            "api",
            "repos/octo-org/optimizer/pulls"
            "?state=open&sort=created&direction=asc&per_page=100&page=1",
        ),
        (
            "gh",
            "api",
            "repos/octo-org/optimizer/pulls"
            "?state=open&sort=created&direction=asc&per_page=100&page=2",
        ),
    ]

    bounded = GhHandoffPullRequestGateway(
        DiscoveryCommands([[item] * 100, [item] * 100]),
        Path("repository"),
        "octo-org/optimizer",
    )
    with pytest.raises(HandoffEventError, match="bounded"):
        bounded.list_open_pull_requests()


def _pr_277_timeline() -> list[dict[str, object]]:
    app = {"id": 1143301, "slug": "copilot-swe-agent"}
    copilot = {"id": 198982749, "login": "Copilot", "type": "Bot"}
    owner = {"id": 18523445, "login": "XOEEst", "type": "User"}
    events: list[dict[str, object]] = [
        {
            "actor": copilot,
            "created_at": f"2026-08-06T20:{minute:02d}:00Z",
            "event": "committed",
            "sha": f"{minute:040x}",
        }
        for minute in range(8)
    ]
    events.extend([
        {
            "actor": copilot,
            "assignee": copilot,
            "created_at": "2026-08-06T20:08:00Z",
            "event": "assigned",
        },
        {
            "actor": copilot,
            "assignee": owner,
            "created_at": "2026-08-06T20:08:01Z",
            "event": "assigned",
        },
        {
            "actor": copilot,
            "created_at": "2026-08-06T20:08:02Z",
            "event": "mentioned",
        },
        {
            "actor": copilot,
            "created_at": "2026-08-06T20:08:03Z",
            "event": "connected",
        },
        {
            "actor": owner,
            "created_at": "2026-08-06T20:08:04Z",
            "event": "copilot_work_started",
            "performed_via_github_app": app,
        },
        {
            "actor": copilot,
            "created_at": "2026-08-06T20:08:05Z",
            "event": "committed",
            "sha": HEAD,
        },
        {
            "actor": copilot,
            "created_at": "2026-08-06T20:08:06Z",
            "event": "renamed",
        },
        {
            "actor": owner,
            "created_at": "2026-08-06T20:08:07Z",
            "event": "copilot_work_finished",
            "performed_via_github_app": app,
        },
        {
            "actor": owner,
            "created_at": "2026-08-06T20:08:08Z",
            "event": "review_requested",
            "requested_reviewer": copilot,
        },
    ])
    return events


def _pr_291_timeline() -> list[dict[str, object]]:
    app = {"id": 1143301, "slug": "copilot-swe-agent"}
    copilot = {"id": 198982749, "login": "Copilot", "type": "Bot"}
    owner = {"id": 18523445, "login": "XOEEst", "type": "User"}
    prior_revisions = (
        "de08391f5437a4b174eebe62dfee06bc57992f89",
        "3327902b22533d9843120e6871d901a7a0e14e6b",
        "08f3974c24924215574861deb619dddbc4e474cd",
        "1bd77b4cc41da1a79214f219dc7dc0047461bd75",
        "f1c7048606b1d402f50b954ff58be717bdd6b17b",
        "e78e07720dfad957f6d50f6db3d214d324191364",
        "18f0b8548bab2d2316b72259904f116444c3bbe0",
        "f8da5b1c2c2c94bb50a3e829ff96ea80f11dbb55",
        "1a9fe00f133dabb5ab6d9c2d96b4a4329afd2363",
        "db0c89345bbc084ada13221d163a2fc540be01c9",
        "c5753990f6147be7ac731b3f44a2eb4eeab66afa",
        "ee5d008df73ed513fad40ed8c945722402734d8f",
        "402afccccadf0e0ef29309cbce8b2c652a95e677",
        "282159a967d9046709baf9ce9a93c0cf82a0ee96",
        "646338728040634607e1cb2337812ef3ac4d52eb",
        "fd955225fcacc5131d67eb6ab35c03d23c8581c6",
    )
    events: list[dict[str, object]] = [
        {"event": "committed", "sha": revision}
        for revision in prior_revisions
    ]
    events.extend([
        {
            "actor": deepcopy(copilot),
            "assignee": deepcopy(copilot),
            "created_at": "2026-08-07T06:54:44Z",
            "event": "assigned",
        },
        {
            "actor": deepcopy(copilot),
            "assignee": deepcopy(owner),
            "created_at": "2026-08-07T06:54:44Z",
            "event": "assigned",
        },
        {
            "actor": deepcopy(copilot),
            "created_at": "2026-08-07T06:54:45Z",
            "event": "mentioned",
        },
        {
            "actor": deepcopy(owner),
            "created_at": "2026-08-07T06:55:21Z",
            "event": "copilot_work_started",
            "performed_via_github_app": deepcopy(app),
        },
        {
            "event": "committed",
            "sha": "7e23fb90056a4994c7dfb18d5b95601c36ac2cee",
        },
        {
            "actor": deepcopy(copilot),
            "created_at": "2026-08-07T06:57:15Z",
            "event": "renamed",
            "rename": {
                "from": (
                    "[WIP] Optimize travel policy coverage with final "
                    "capability loop"
                ),
                "to": (
                    "Persist steward handoff envelope for optimization "
                    "campaign issue #280"
                ),
            },
        },
        {
            "actor": deepcopy(owner),
            "created_at": "2026-08-07T06:57:31Z",
            "event": "copilot_work_finished",
            "performed_via_github_app": deepcopy(app),
        },
        {
            "actor": deepcopy(copilot),
            "created_at": "2026-08-07T06:57:31Z",
            "event": "review_requested",
            "requested_reviewer": deepcopy(owner),
        },
    ])
    return events


def _pr_449_timeline() -> list[dict[str, object]]:
    app = {"id": 1143301, "slug": "copilot-swe-agent"}
    copilot = {"id": 198982749, "login": "Copilot", "type": "Bot"}
    owner = {"id": 18523445, "login": "XOEEst", "type": "User"}
    return [
        {
            "event": "committed",
            "sha": "51b6d7870fc9c6469aa26378dd16edfc34b31c8a",
        },
        {
            "event": "committed",
            "sha": "570bfbfa8476089676b1ccc152354668dc7fb360",
        },
        {
            "actor": deepcopy(copilot),
            "assignee": deepcopy(copilot),
            "created_at": "2026-08-08T21:42:18Z",
            "event": "assigned",
        },
        {
            "actor": deepcopy(copilot),
            "assignee": deepcopy(owner),
            "created_at": "2026-08-08T21:42:18Z",
            "event": "assigned",
        },
        {
            "actor": deepcopy(copilot),
            "created_at": "2026-08-08T21:42:20Z",
            "event": "mentioned",
        },
        {
            "actor": deepcopy(owner),
            "created_at": "2026-08-08T21:42:31Z",
            "event": "copilot_work_started",
            "performed_via_github_app": deepcopy(app),
        },
        {
            "actor": deepcopy(copilot),
            "created_at": "2026-08-08T21:42:31Z",
            "event": "connected",
        },
        {
            "event": "committed",
            "sha": "29ec71a98e7e3121105a25d02c2ecdd1d41a77e7",
        },
        {
            "event": "committed",
            "sha": "82fa79186b0ccde5f1942ac858c6859ffe3d3b13",
        },
        {
            "actor": deepcopy(copilot),
            "created_at": "2026-08-08T21:50:15Z",
            "event": "renamed",
        },
        {
            "actor": deepcopy(copilot),
            "created_at": "2026-08-08T21:50:25Z",
            "event": "review_requested",
            "requested_reviewer": deepcopy(owner),
        },
        {
            "actor": deepcopy(owner),
            "created_at": "2026-08-08T21:50:25Z",
            "event": "copilot_work_finished",
            "performed_via_github_app": deepcopy(app),
        },
    ]


def test_github_discovery_accepts_pr_449_commits_ending_at_head() -> None:
    gateway = GhHandoffPullRequestGateway(
        DiscoveryCommands([_pr_449_timeline()]),
        Path("repository"),
        "octo-org/optimizer",
    )

    assert gateway.head_has_copilot_session_attestation(
        449,
        "copilot/foundry-optdesign-428-1-1-worker",
        "82fa79186b0ccde5f1942ac858c6859ffe3d3b13",
    ) is True


@pytest.mark.parametrize(
    "case",
    (
        "head-not-last",
        "commit-after-finish",
        "force-push-in-window",
        "force-push-after-finish",
        "too-many-commits",
        "duplicate-commit",
        "uppercase-commit",
        "short-commit",
        "non-hex-commit",
        "non-string-commit",
    ),
)
def test_github_discovery_rejects_invalid_pr_449_commit_sequence(
    case: str,
) -> None:
    events = deepcopy(_pr_449_timeline())
    if case == "head-not-last":
        events.insert(9, {"event": "committed", "sha": "c" * 40})
    elif case == "commit-after-finish":
        events.append({"event": "committed", "sha": "c" * 40})
    elif case == "force-push-in-window":
        events.insert(9, {"event": "head_ref_force_pushed"})
    elif case == "force-push-after-finish":
        events.append({"event": "head_ref_force_pushed"})
    elif case == "too-many-commits":
        events[7:9] = [
            {"event": "committed", "sha": f"{index:040x}"}
            for index in range(1, 21)
        ] + [
            {
                "event": "committed",
                "sha": "82fa79186b0ccde5f1942ac858c6859ffe3d3b13",
            }
        ]
    elif case == "duplicate-commit":
        events.insert(8, deepcopy(events[7]))
    elif case == "uppercase-commit":
        events[7]["sha"] = "A" * 40
    elif case == "short-commit":
        events[7]["sha"] = "a" * 39
    elif case == "non-hex-commit":
        events[7]["sha"] = "g" * 40
    elif case == "non-string-commit":
        events[7]["sha"] = 29

    gateway = GhHandoffPullRequestGateway(
        DiscoveryCommands([events]),
        Path("repository"),
        "octo-org/optimizer",
    )

    assert gateway.head_has_copilot_session_attestation(
        449,
        "copilot/foundry-optdesign-428-1-1-worker",
        "82fa79186b0ccde5f1942ac858c6859ffe3d3b13",
    ) is False


def test_github_discovery_accepts_pr_277_copilot_lead_in() -> None:
    events = _pr_277_timeline()
    commands = DiscoveryCommands([events])
    gateway = GhHandoffPullRequestGateway(
        commands,
        Path("repository"),
        "octo-org/optimizer",
    )

    assert gateway.head_has_copilot_session_attestation(
        90,
        "copilot/steward-issue-31",
        HEAD,
    ) is True
    assert commands.calls == [
        (
            "gh",
            "api",
            "-H",
            "Accept: application/vnd.github+json",
            "repos/octo-org/optimizer/issues/90/timeline"
            "?per_page=100&page=1",
        )
    ]


def test_github_discovery_accepts_pr_291_strong_lead_in_without_connection(
) -> None:
    events = _pr_291_timeline()
    gateway = GhHandoffPullRequestGateway(
        DiscoveryCommands([events]),
        Path("repository"),
        "octo-org/optimizer",
    )

    assert gateway.head_has_copilot_session_attestation(
        291,
        "copilot/steward-issue-280",
        "7e23fb90056a4994c7dfb18d5b95601c36ac2cee",
    ) is True


@pytest.mark.parametrize("repeated", ("self-assignment", "mention"))
def test_github_discovery_accepts_unambiguous_strong_lead_in_repetition(
    repeated: str,
) -> None:
    events = deepcopy(_pr_291_timeline())
    source_index = 16 if repeated == "self-assignment" else 18
    duplicate = deepcopy(events[source_index])
    events.insert(source_index + 1, duplicate)
    gateway = GhHandoffPullRequestGateway(
        DiscoveryCommands([events]),
        Path("repository"),
        "octo-org/optimizer",
    )

    assert gateway.head_has_copilot_session_attestation(
        291,
        "copilot/steward-issue-280",
        "7e23fb90056a4994c7dfb18d5b95601c36ac2cee",
    ) is True


@pytest.mark.parametrize(
    "case",
    (
        "missing-self-assignment",
        "missing-owner-assignment",
        "missing-mention",
        "duplicate-owner-assignment",
        "owner-before-self-assignment",
        "mention-before-owner-assignment",
        "wrong-self-assignment-actor",
        "wrong-self-assignee",
        "wrong-owner-assignment-actor",
        "wrong-owner-assignee",
        "wrong-mention-actor",
        "unrelated-lead-in-event",
        "stale-lead-in",
        "out-of-order-lead-in",
        "unbounded-lead-in",
        "nested-work-start",
        "interleaved-unrelated-finish",
        "connected-outside-window",
        "extra-window-commit",
        "force-push-in-window",
        "commit-after-finish",
        "force-push-after-finish",
    ),
)
def test_github_discovery_rejects_inexact_zero_connection_lead_in(
    case: str,
) -> None:
    events = deepcopy(_pr_291_timeline())
    if case == "missing-self-assignment":
        events.pop(16)
    elif case == "missing-owner-assignment":
        events.pop(17)
    elif case == "missing-mention":
        events.pop(18)
    elif case == "duplicate-owner-assignment":
        events.insert(18, deepcopy(events[17]))
    elif case == "owner-before-self-assignment":
        events[16], events[17] = events[17], events[16]
    elif case == "mention-before-owner-assignment":
        events[17], events[18] = events[18], events[17]
    elif case == "wrong-self-assignment-actor":
        events[16]["actor"]["id"] = 1
    elif case == "wrong-self-assignee":
        events[16]["assignee"]["login"] = "copilot-swe-agent[bot]"
    elif case == "wrong-owner-assignment-actor":
        events[17]["actor"]["login"] = "copilot-swe-agent[bot]"
    elif case == "wrong-owner-assignee":
        events[17]["assignee"]["id"] = 1
    elif case == "wrong-mention-actor":
        events[18]["actor"]["type"] = "User"
    elif case == "unrelated-lead-in-event":
        events.insert(
            19,
            {
                "actor": deepcopy(events[18]["actor"]),
                "created_at": "2026-08-07T06:54:46Z",
                "event": "labeled",
            },
        )
    elif case == "stale-lead-in":
        events[16]["created_at"] = "2026-08-07T06:49:00Z"
    elif case == "out-of-order-lead-in":
        events[18]["created_at"] = "2026-08-07T06:56:00Z"
    elif case == "unbounded-lead-in":
        events.insert(17, deepcopy(events[16]))
        events.insert(18, deepcopy(events[16]))
    elif case == "nested-work-start":
        events.insert(20, deepcopy(events[19]))
    elif case == "interleaved-unrelated-finish":
        prior_start = deepcopy(events[19])
        prior_start["created_at"] = "2026-08-07T06:53:00Z"
        unrelated_finish = deepcopy(events[22])
        unrelated_finish["actor"] = {
            "id": 999,
            "login": "other-user",
            "type": "User",
        }
        unrelated_finish["created_at"] = "2026-08-07T06:53:30Z"
        events[16:16] = [prior_start, unrelated_finish]
    elif case == "connected-outside-window":
        events.insert(
            8,
            {
                "actor": deepcopy(events[16]["actor"]),
                "created_at": "2026-08-07T06:30:00Z",
                "event": "connected",
            },
        )
    elif case == "extra-window-commit":
        events.insert(21, {"event": "committed", "sha": "f" * 40})
    elif case == "force-push-in-window":
        events.insert(21, {"event": "head_ref_force_pushed"})
    elif case == "commit-after-finish":
        events.insert(23, {"event": "committed", "sha": "f" * 40})
    elif case == "force-push-after-finish":
        events.insert(23, {"event": "head_ref_force_pushed"})

    gateway = GhHandoffPullRequestGateway(
        DiscoveryCommands([events]),
        Path("repository"),
        "octo-org/optimizer",
    )

    assert gateway.head_has_copilot_session_attestation(
        291,
        "copilot/steward-issue-280",
        "7e23fb90056a4994c7dfb18d5b95601c36ac2cee",
    ) is False


def test_github_discovery_binds_head_to_exact_copilot_session() -> None:
    events = _pr_277_timeline()
    forged = GhHandoffPullRequestGateway(
        DiscoveryCommands(
            [[
                *events[:13],
                {"event": "committed", "sha": "f" * 40},
                *events[14:],
            ]]
        ),
        Path("repository"),
        "octo-org/optimizer",
    )
    assert forged.head_has_copilot_session_attestation(
        90,
        "copilot/steward-issue-31",
        HEAD,
    ) is False

    bounded = GhHandoffPullRequestGateway(
        DiscoveryCommands([[{}] * 100, [{}] * 100, [{}] * 100]),
        Path("repository"),
        "octo-org/optimizer",
    )
    with pytest.raises(HandoffEventError, match="timeline discovery"):
        bounded.head_has_copilot_session_attestation(
            90,
            "copilot/steward-issue-31",
            HEAD,
        )


def test_github_discovery_accepts_connection_inside_work_window() -> None:
    app = {"id": 1143301, "slug": "copilot-swe-agent"}
    owner = {"id": 18523445, "login": "XOEEst", "type": "User"}
    events = [
        {
            "actor": owner,
            "event": "copilot_work_started",
            "performed_via_github_app": app,
        },
        {
            "actor": {
                "id": 198982749,
                "login": "Copilot",
                "type": "Bot",
            },
            "event": "connected",
        },
        {"event": "committed", "sha": HEAD},
        {
            "actor": owner,
            "event": "copilot_work_finished",
            "performed_via_github_app": app,
        },
    ]
    gateway = GhHandoffPullRequestGateway(
        DiscoveryCommands([events]),
        Path("repository"),
        "octo-org/optimizer",
    )

    assert gateway.head_has_copilot_session_attestation(
        90,
        "copilot/steward-issue-31",
        HEAD,
    ) is True


@pytest.mark.parametrize(
    "case",
    (
        "arbitrary-old",
        "after-finish",
        "unrelated-before-start",
        "too-many-lead-in-events",
        "stale-lead-in",
        "out-of-order-lead-in",
        "wrong-bot-id",
        "wrong-bot-login",
        "multiple-connections",
    ),
)
def test_github_discovery_rejects_unbound_connections(case: str) -> None:
    events = deepcopy(_pr_277_timeline())
    connection = events[11]
    if case == "arbitrary-old":
        events.pop(11)
        events.insert(4, connection)
    elif case == "after-finish":
        events.pop(11)
        events.insert(15, connection)
    elif case == "unrelated-before-start":
        events.insert(
            12,
            {
                "actor": events[12]["actor"],
                "created_at": "2026-08-06T20:08:03Z",
                "event": "labeled",
            },
        )
    elif case == "too-many-lead-in-events":
        events.insert(8, deepcopy(events[8]))
    elif case == "stale-lead-in":
        for index in range(8, 12):
            events[index]["created_at"] = (
                f"2026-08-06T20:00:0{index - 8}Z"
            )
    elif case == "out-of-order-lead-in":
        connection["created_at"] = "2026-08-06T20:09:00Z"
    elif case == "wrong-bot-id":
        connection["actor"]["id"] = 1
    elif case == "wrong-bot-login":
        connection["actor"]["login"] = "copilot-swe-agent"
    elif case == "multiple-connections":
        duplicate = deepcopy(connection)
        duplicate["created_at"] = "2026-08-06T20:08:05Z"
        events.insert(14, duplicate)

    gateway = GhHandoffPullRequestGateway(
        DiscoveryCommands([events]),
        Path("repository"),
        "octo-org/optimizer",
    )

    assert gateway.head_has_copilot_session_attestation(
        90,
        "copilot/steward-issue-31",
        HEAD,
    ) is False


@pytest.mark.parametrize(
    "case",
    (
        "other-user-assignee",
        "start-actor-assignee-id-mismatch",
        "start-actor-assignee-login-mismatch",
        "assignment-actor-id-mismatch",
        "assignment-actor-login-mismatch",
        "multiple-unrelated-assignments",
        "author-performed-assignment",
    ),
)
def test_github_discovery_rejects_inexact_lead_in_assignments(
    case: str,
) -> None:
    events = deepcopy(_pr_277_timeline())
    assignment = events[9]
    if case == "other-user-assignee":
        assignment["assignee"] = {
            "id": 999,
            "login": "other-user",
            "type": "User",
        }
    elif case == "start-actor-assignee-id-mismatch":
        assignment["assignee"] = {
            "id": 999,
            "login": "XOEEst",
            "type": "User",
        }
    elif case == "start-actor-assignee-login-mismatch":
        assignment["assignee"] = {
            "id": 18523445,
            "login": "other-user",
            "type": "User",
        }
    elif case == "assignment-actor-id-mismatch":
        assignment["actor"] = {
            "id": 1,
            "login": "Copilot",
            "type": "Bot",
        }
    elif case == "assignment-actor-login-mismatch":
        assignment["actor"] = {
            "id": 198982749,
            "login": "copilot-swe-agent",
            "type": "Bot",
        }
    elif case == "multiple-unrelated-assignments":
        events.insert(
            10,
            {
                "actor": deepcopy(assignment["actor"]),
                "assignee": {
                    "id": 999,
                    "login": "other-user",
                    "type": "User",
                },
                "created_at": "2026-08-06T20:08:01Z",
                "event": "assigned",
            },
        )
    elif case == "author-performed-assignment":
        assignment["actor"] = deepcopy(assignment["assignee"])

    gateway = GhHandoffPullRequestGateway(
        DiscoveryCommands([events]),
        Path("repository"),
        "octo-org/optimizer",
    )

    assert gateway.head_has_copilot_session_attestation(
        90,
        "copilot/steward-issue-31",
        HEAD,
    ) is False


@pytest.mark.parametrize(
    "case",
    (
        "different-finish-id",
        "different-finish-login",
        "different-finish-app",
        "different-start-app",
        "missing-actor-login",
        "connected-after-finish",
        "force-push-in-window",
        "force-push-after-finish",
        "commit-after-finish",
    ),
)
def test_github_discovery_rejects_inexact_work_windows(case: str) -> None:
    events = deepcopy(_pr_277_timeline())
    if case == "different-finish-id":
        events[15]["actor"] = {
            "id": 1,
            "login": "XOEEst",
            "type": "User",
        }
    elif case == "different-finish-login":
        events[15]["actor"] = {
            "id": 18523445,
            "login": "other-user",
            "type": "User",
        }
    elif case == "different-finish-app":
        events[15]["performed_via_github_app"]["id"] = 1
    elif case == "different-start-app":
        events[12]["performed_via_github_app"]["slug"] = "other-app"
    elif case == "missing-actor-login":
        actor = {"id": 18523445, "type": "User"}
        events[12]["actor"] = actor
        events[15]["actor"] = deepcopy(actor)
    elif case == "connected-after-finish":
        events.insert(16, deepcopy(events[11]))
    elif case == "force-push-in-window":
        events.insert(14, {"event": "head_ref_force_pushed"})
    elif case == "force-push-after-finish":
        events.insert(16, {"event": "head_ref_force_pushed"})
    elif case == "commit-after-finish":
        events.insert(16, {"event": "committed", "sha": HEAD})

    gateway = GhHandoffPullRequestGateway(
        DiscoveryCommands([events]),
        Path("repository"),
        "octo-org/optimizer",
    )

    assert gateway.head_has_copilot_session_attestation(
        90,
        "copilot/steward-issue-31",
        HEAD,
    ) is False


def test_github_discovery_rejects_multiple_matching_work_windows() -> None:
    app = {"id": 1143301, "slug": "copilot-swe-agent"}
    owner = {"id": 18523445, "login": "XOEEst", "type": "User"}
    start = {
        "actor": owner,
        "event": "copilot_work_started",
        "performed_via_github_app": app,
    }
    events = [
        start,
        deepcopy(start),
        {
            "actor": {
                "id": 198982749,
                "login": "Copilot",
                "type": "Bot",
            },
            "event": "connected",
        },
        {"event": "committed", "sha": HEAD},
        {
            "actor": owner,
            "event": "copilot_work_finished",
            "performed_via_github_app": app,
        },
    ]
    gateway = GhHandoffPullRequestGateway(
        DiscoveryCommands([events]),
        Path("repository"),
        "octo-org/optimizer",
    )

    assert gateway.head_has_copilot_session_attestation(
        90,
        "copilot/steward-issue-31",
        HEAD,
    ) is False


@pytest.mark.parametrize(
    (
        "transport_candidates",
        "effect_candidates",
        "current_transport",
        "current_effects",
        "expected_transport",
        "expected_effects",
    ),
    (
        ((), (), False, False, [], []),
        ((31,), (31,), False, False, [], []),
        ((), (31,), False, True, [], [31]),
        ((31,), (31,), True, True, [31], [31]),
    ),
)
def test_production_handoff_effects_honor_lifecycle_gate(
    monkeypatch,
    transport_candidates,
    effect_candidates,
    current_transport,
    current_effects,
    expected_transport,
    expected_effects,
) -> None:
    import foundry_opt.orchestration.deployment_bridge as deployment_bridge
    import foundry_opt.orchestration.git_state as git_state
    import foundry_opt.orchestration.issue_intake as issue_intake
    import foundry_opt.orchestration.projection as projection
    import foundry_opt.orchestration.transport as transport

    reconciled: list[int] = []
    cleaned: list[int] = []
    projected: list[int] = []

    class Recovery:
        def __init__(self, *args) -> None:
            pass

        def effect_candidates(self, issue_numbers):
            return type(
                "Candidates",
                (),
                {
                    "transport": transport_candidates,
                    "persisted": effect_candidates,
                },
            )()

        def can_reconcile_transport(self, issue_number):
            return current_transport

        def can_reconcile_persisted_effects(self, issue_number):
            return current_effects

    monkeypatch.setattr(
        issue_intake,
        "GitIssueEventInbox",
        lambda root: object(),
    )
    monkeypatch.setattr(
        issue_intake,
        "GitStateCampaignRecovery",
        Recovery,
    )
    monkeypatch.setattr(git_state, "GitStateRef", lambda: object())
    monkeypatch.setattr(
        transport,
        "reconcile_github_transport_effects",
        lambda root, issue_number, *args, **kwargs: reconciled.append(
            issue_number
        ),
    )
    monkeypatch.setattr(
        deployment_bridge,
        "reconcile_deployment_cleanup_effects",
        lambda root, issue_number, *args: cleaned.append(issue_number),
    )
    monkeypatch.setattr(
        projection,
        "GitStateProjectionOutbox",
        lambda root: object(),
    )
    monkeypatch.setattr(
        projection,
        "GhDashboardGateway",
        lambda *args: object(),
    )
    monkeypatch.setattr(
        projection,
        "DashboardProjection",
        lambda *args: type(
            "Projection",
            (),
            {"project": lambda self, issue: projected.append(issue)},
        )(),
    )

    _ProductionHandoffEffects(
        Path("."),
        object(),
        "octo-org/optimizer",
        "assignment-token",
    ).reconcile(31)

    assert reconciled == expected_transport
    assert cleaned == expected_effects
    assert projected == expected_effects


def test_handoff_finalizer_applies_effects_closes_and_reassigns() -> None:
    class FinalizeGateway:
        def __init__(self) -> None:
            self.closed = []
            self.deleted = []

        def close_internal_pull_request(self, number, **kwargs):
            self.closed.append((number, kwargs))

        def delete_branch_if_head(self, branch, revision):
            self.deleted.append((branch, revision))
            return True

    class Assignments:
        def __init__(self) -> None:
            self.released = []
            self.assigned = []

        def release(self, issue_number):
            self.released.append(issue_number)

        def assign(self, issue_number, idempotency_key):
            self.assigned.append((issue_number, idempotency_key))
            return True

    class Effects:
        def __init__(self) -> None:
            self.issues = []

        def reconcile(self, issue_number):
            self.issues.append(issue_number)

    gateway = FinalizeGateway()
    assignments = Assignments()
    effects = Effects()
    finalizer = HandoffFinalizer(
        gateway=gateway,
        assignments=assignments,
        effects=effects,
        should_reassign=lambda issue: issue == 31,
    )
    request = type(
        "Request",
        (),
        {
            "pull_request_number": 90,
            "head_ref": "copilot/steward-issue-31",
            "head_revision": HEAD,
        },
    )()

    finalizer.finalize(
        request,
        HandoffApplyResult(
            HandoffApplyStatus.APPLIED,
            handoff_id="d" * 64,
            issue_number=31,
            kind="steward_state",
        ),
    )

    assert effects.issues == [31]
    assert assignments.released == [31]
    assert assignments.assigned == [
        (31, "handoff-" + "d" * 64)
    ]
    assert gateway.closed[0][0] == 90
    assert gateway.deleted == [
        ("copilot/steward-issue-31", HEAD)
    ]


def test_closing_an_already_closed_internal_handoff_is_idempotent() -> None:
    class Commands:
        def __init__(self) -> None:
            self.calls = []

        def run(self, arguments, **kwargs):
            command = tuple(arguments)
            self.calls.append(command)
            if "--method" in command:
                raise CommandExitError(
                    command,
                    exit_code=1,
                    stdout="",
                    stderr="pull request is already closed",
                )
            assert command[-1] == (
                "repos/octo-org/optimizer/pulls/90"
            )
            return CommandResult(
                0,
                json.dumps({"number": 90, "state": "closed"}),
                "",
            )

    commands = Commands()
    gateway = GhHandoffPullRequestGateway(
        commands,
        Path("."),
        "octo-org/optimizer",
    )

    gateway.close_internal_pull_request(
        90,
        handoff_id="d" * 64,
        kind="steward_state",
    )

    assert len(commands.calls) == 2


@pytest.mark.parametrize(
    "envelope_type",
    [StewardStateHandoff, CandidateDesignHandoff],
)
def test_handoff_envelopes_reject_unknown_top_level_fields(
    envelope_type,
) -> None:
    content = json.dumps(
        {
            "handoff_id": "d" * 64,
            "payload": {},
            "marker": "secondary-only",
        }
    ).encode()

    with pytest.raises(ValueError, match="document is invalid"):
        envelope_type.from_bytes(content)


@pytest.mark.parametrize(
    "envelope_type",
    [StewardStateHandoff, CandidateDesignHandoff],
)
def test_handoff_envelopes_reject_unknown_payload_fields(
    envelope_type,
) -> None:
    content = json.dumps(
        {
            "handoff_id": "d" * 64,
            "payload": {"unknown": True},
        }
    ).encode()

    with pytest.raises(ValueError, match="fields are invalid"):
        envelope_type.from_bytes(content)


def test_handoff_finalizer_does_not_close_an_advanced_branch() -> None:
    class FinalizeGateway:
        def __init__(self) -> None:
            self.closed: list[int] = []

        def close_internal_pull_request(self, number, **kwargs):
            self.closed.append(number)

        def delete_branch_if_head(self, branch, revision):
            return False

    class Assignments:
        def release(self, issue_number):
            pass

        def assign(self, issue_number, idempotency_key):
            return True

    gateway = FinalizeGateway()
    finalizer = HandoffFinalizer(
        gateway=gateway,
        assignments=Assignments(),
        effects=type(
            "Effects",
            (),
            {"reconcile": lambda self, issue_number: None},
        )(),
        should_reassign=lambda issue_number: False,
    )
    request = type(
        "Request",
        (),
        {
            "pull_request_number": 90,
            "head_ref": "copilot/steward-issue-31",
            "head_revision": HEAD,
        },
    )()

    with pytest.raises(HandoffError, match="advanced"):
        finalizer.finalize(
            request,
            HandoffApplyResult(
                HandoffApplyStatus.APPLIED,
                handoff_id="d" * 64,
                issue_number=31,
                kind="steward_state",
            ),
        )

    assert gateway.closed == []


def test_trusted_event_rejects_fork_and_stale_head() -> None:
    gateway = Gateway()
    gateway.pull_request["head"]["repo"]["full_name"] = "attacker/fork"
    with pytest.raises(HandoffEventError, match="identity"):
        trusted_handoff_request_from_payload(
            _payload(),
            TrustedHandoffContext(
                "pull_request_target",
                "octo-org/optimizer",
                123,
                "trunk",
            ),
            Path("repository"),
            gateway,
        )

    gateway = Gateway()
    gateway.pull_request["head"]["sha"] = "f" * 40
    with pytest.raises(HandoffEventError, match="current"):
        trusted_handoff_request_from_payload(
            _payload(),
            TrustedHandoffContext(
                "pull_request_target",
                "octo-org/optimizer",
                123,
                "trunk",
            ),
            Path("repository"),
            gateway,
        )
