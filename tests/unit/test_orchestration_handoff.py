from __future__ import annotations

from pathlib import Path
import json

import pytest

from foundry_opt.preflight.interfaces import CommandResult
from foundry_opt.orchestration.handoff import (
    _ProductionHandoffEffects,
    GhHandoffPullRequestGateway,
    HandoffApplyResult,
    HandoffApplyStatus,
    HandoffEventError,
    HandoffFinalizer,
    TrustedHandoffContext,
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

    def fetch_head(self, revision: str) -> str:
        self.fetched.append(revision)
        return revision


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
    assert gateway.fetched == [HEAD]


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
            json.dumps([[{
                "filename": PATH,
                "sha": BLOB,
                "status": "added",
            }]]),
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
    assert gateway.fetch_head(HEAD) == HEAD
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
        "origin",
        "--delete",
        "copilot/steward-issue-31",
    ) in arguments


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
