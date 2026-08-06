from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import json

import pytest

from foundry_opt.preflight.interfaces import CommandResult
from foundry_opt.orchestration.handoff import (
    _ProductionHandoffEffects,
    GhHandoffPullRequestGateway,
    HandoffApplyResult,
    HandoffApplyStatus,
    HandoffError,
    HandoffEventError,
    HandoffFinalizer,
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
        "repos/octo-org/optimizer/pulls/90/files?per_page=2",
    ) in arguments
    assert all("--paginate" not in call for call in arguments)
    assert all("checkout" not in call for call in arguments)
    assert all(call["environment"] is None for call in commands.calls)
    close_call = commands.calls[4]
    close_payload = json.loads(str(close_call["input_text"]))
    assert close_payload["state"] == "closed"
    assert close_payload["title"].startswith("[internal]")
    assert "<!-- foundry-opt:internal-handoff:" in close_payload["body"]
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


def test_github_discovery_binds_head_to_exact_copilot_session() -> None:
    app = {"id": 1143301, "slug": "copilot-swe-agent"}
    events = [
        {
            "actor": {"id": 7, "login": "maintainer"},
            "event": "copilot_work_started",
            "performed_via_github_app": app,
        },
        {
            "actor": {"id": 198982749, "login": "Copilot"},
            "event": "connected",
        },
        {"event": "committed", "sha": HEAD},
        {
            "actor": {"id": 7, "login": "maintainer"},
            "event": "copilot_work_finished",
            "performed_via_github_app": app,
        },
    ]
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

    forged = GhHandoffPullRequestGateway(
        DiscoveryCommands(
            [[
                *events[:2],
                {"event": "committed", "sha": "f" * 40},
                *events[2:],
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
