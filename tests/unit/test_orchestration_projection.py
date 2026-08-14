from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

import pytest

from foundry_opt.orchestration import OutboxRecord
from foundry_opt.orchestration.projection import (
    DashboardComment,
    DashboardProjection,
    GhDashboardGateway,
    ProjectionError,
)
from foundry_opt.preflight.interfaces import CommandResult


@dataclass
class FakeOutbox:
    records: tuple[OutboxRecord, ...]

    def for_issue(self, issue_number: int) -> tuple[OutboxRecord, ...]:
        return self.records


@dataclass
class FakeDashboardGateway:
    comment: DashboardComment | None = None
    labels: set[str] = field(default_factory=set)
    created: list[str] = field(default_factory=list)
    updated: list[tuple[int, str]] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def find_dashboard(self, issue_number: int):
        return self.comment

    def create_dashboard(self, issue_number: int, body: str) -> None:
        self.created.append(body)
        self.comment = DashboardComment(81, body)

    def update_dashboard(
        self,
        issue_number: int,
        comment_id: int,
        body: str,
    ) -> None:
        self.updated.append((comment_id, body))
        self.comment = DashboardComment(comment_id, body)

    def issue_labels(self, issue_number: int) -> frozenset[str]:
        return frozenset(self.labels)

    def add_label(self, issue_number: int, label: str) -> None:
        self.added.append(label)
        self.labels.add(label)

    def remove_label(self, issue_number: int, label: str) -> None:
        self.removed.append(label)
        self.labels.discard(label)


def _record(
    record_id: str,
    kind: str,
    sequence: int,
    **payload: object,
) -> OutboxRecord:
    return OutboxRecord(
        record_id=record_id,
        kind=kind,
        generation=2,
        sequence=sequence,
        payload={"issue_number": 31, **payload},
    )


def test_dashboard_projection_uses_stable_and_idempotent_markers() -> None:
    gateway = FakeDashboardGateway()
    source = FakeOutbox(
        (
            _record(
                "dashboard-4",
                "dashboard_projection",
                4,
                phase="baseline",
                status="running",
                disposition="advance",
            ),
        )
    )
    projection = DashboardProjection(source, gateway)

    projection.project(31)
    projection.project(31)

    assert len(gateway.created) == 1
    assert gateway.updated == []
    assert "<!-- foundry-opt:dashboard:issue-31 -->" in gateway.created[0]
    assert (
        "<!-- foundry-opt:projection:dashboard-4 -->"
        in gateway.created[0]
    )
    assert "Phase: `baseline`" in gateway.created[0]


def test_projection_applies_only_dashboard_and_label_outbox_effects() -> None:
    gateway = FakeDashboardGateway(labels={"needs-triage", "keep"})
    source = FakeOutbox(
        (
            _record(
                "dispatch-4",
                "continue_campaign",
                4,
                status="running",
            ),
            _record(
                "label-add-4",
                "label_add",
                4,
                label="optimization-running",
            ),
            _record(
                "label-remove-4",
                "label_remove",
                4,
                label="needs-triage",
            ),
        )
    )

    projection = DashboardProjection(source, gateway)
    projection.project(31)
    projection.project(31)

    assert gateway.created == []
    assert gateway.updated == []
    assert gateway.added == ["optimization-running"]
    assert gateway.removed == ["needs-triage"]
    assert gateway.labels == {"optimization-running", "keep"}


def test_new_dashboard_record_updates_the_single_dashboard_comment() -> None:
    gateway = FakeDashboardGateway()
    first = _record(
        "dashboard-4",
        "dashboard_projection",
        4,
        phase="baseline",
        status="running",
        disposition="advance",
    )
    source = FakeOutbox((first,))
    projection = DashboardProjection(source, gateway)
    projection.project(31)
    source.records = (
        first,
        _record(
            "dashboard-5",
            "dashboard_projection",
            5,
            phase="awaiting_selection",
            status="waiting",
            disposition="wait",
        ),
    )

    projection.project(31)

    assert len(gateway.created) == 1
    assert len(gateway.updated) == 1
    assert "projection:dashboard-5" in gateway.updated[0][1]
    assert "awaiting_selection" in gateway.updated[0][1]


def test_final_deployment_dashboard_renders_only_aggregate_comparison() -> None:
    gateway = FakeDashboardGateway()
    source = FakeOutbox(
        (
            _record(
                "final-dashboard-2",
                "deployment_final_dashboard",
                14,
                phase="completed",
                status="completed",
                disposition="complete",
                candidate_id="candidate-1",
                deployment_version=13,
                lineage_sha256="a" * 64,
                merge_actor="maintainer",
                required_checks=["exact-candidate", "tests"],
                spec_sha256="b" * 64,
                merge_commit="c" * 40,
                tree_sha="d" * 40,
                patch_sha256="e" * 64,
                bundle_sha256="f" * 64,
                evidence_sha256="1" * 64,
                metadata_sha256="2" * 64,
                source_sha256="f" * 64,
                run_id=991,
                run_url=(
                    "https://github.com/octo-org/agents/actions/runs/991"
                ),
                portal_url=(
                    "https://ai.azure.com/projects/demo/agents/"
                    "support/versions/13"
                ),
                baseline_metrics={"quality": 0.7},
                draft_metrics={"quality": 0.9},
                deployed_metrics={"quality": 0.88},
            ),
        )
    )

    DashboardProjection(source, gateway).project(31)

    assert len(gateway.created) == 1
    body = gateway.created[0]
    assert "Candidate: `candidate-1`" in body
    assert "Published version: `13`" in body
    assert "Baseline aggregates: `quality=0.7`" in body
    assert "Selected draft aggregates: `quality=0.9`" in body
    assert "Deployed aggregates: `quality=0.88`" in body
    assert "raw_response" not in body


def test_deployment_dashboard_preserves_pre_merge_candidate_evidence() -> None:
    gateway = FakeDashboardGateway()
    slate = _record(
        "slate-dashboard-2",
        "candidate_slate_dashboard",
        9,
        phase="awaiting_selection",
        status="waiting",
        disposition="wait",
        spec_sha256="d" * 64,
        baseline_metrics={"quality": 0.5},
        candidate_slate=[
            {
                "candidate_id": "candidate-1",
                "rank": 1,
                "draft_id": "draft-candidate-1",
                "metrics": {"quality": 0.9},
                "deltas": {"quality": 0.4},
                "guardrails": {"safety": "pass"},
                "evidence_sha256": "e" * 64,
                "evidence_url": (
                    "https://github.com/octo-org/optimizer/blob/"
                    "foundry-opt/state/issue-31/objects/evidence/"
                    + "e" * 64
                    + ".json"
                ),
            }
        ],
        next_action="merge_exactly_one_candidate_pr",
        source_sha256="9" * 64,
    )
    final = _record(
        "final-dashboard-2",
        "deployment_final_dashboard",
        14,
        phase="completed",
        status="completed",
        disposition="complete",
        candidate_id="candidate-1",
        deployment_version=13,
        lineage_sha256="a" * 64,
        merge_actor="maintainer",
        required_checks=["exact-candidate", "tests"],
        spec_sha256="b" * 64,
        merge_commit="c" * 40,
        tree_sha="d" * 40,
        patch_sha256="e" * 64,
        bundle_sha256="f" * 64,
        evidence_sha256="1" * 64,
        metadata_sha256="2" * 64,
        source_sha256="f" * 64,
        run_id=991,
        run_url=(
            "https://github.com/octo-org/agents/actions/runs/991"
        ),
        portal_url="https://ai.azure.com/example",
        baseline_metrics={"quality": 0.5},
        draft_metrics={"quality": 0.9},
        deployed_metrics={"quality": 0.88},
    )
    source = FakeOutbox((slate, final))

    DashboardProjection(source, gateway).project(31)

    body = gateway.created[0]
    assert "### Candidate comparison" in body
    assert "[redacted evidence](https://github.com/" in body
    assert "### Verified deployment result" in body
    assert "### Historical selection evidence" in body
    assert "Merge exactly one eligible candidate PR" not in body
    assert "<!-- foundry-opt:projection:slate-dashboard-2 -->" in body
    assert "<!-- foundry-opt:projection:final-dashboard-2 -->" in body


def test_latest_dashboard_record_still_requires_current_schema() -> None:
    gateway = FakeDashboardGateway()
    source = FakeOutbox(
        (
            _record(
                "slate-dashboard-2",
                "candidate_slate_dashboard",
                9,
                phase="awaiting_selection",
                status="waiting",
                disposition="wait",
                spec_sha256="d" * 64,
                baseline_metrics={"quality": 0.5},
                candidate_slate=[
                    {
                        "candidate_id": "candidate-1",
                        "rank": 1,
                        "draft_id": "draft-candidate-1",
                        "metrics": {"quality": 0.9},
                        "deltas": {"quality": 0.4},
                        "guardrails": {"safety": "pass"},
                        "evidence_sha256": "e" * 64,
                        "evidence_url": (
                            "https://github.com/octo-org/optimizer/blob/"
                            "foundry-opt/state/issue-31/objects/evidence/"
                            + "e" * 64
                            + ".json"
                        ),
                    }
                ],
                next_action="merge_exactly_one_candidate_pr",
                source_sha256="9" * 64,
            ),
        )
    )

    with pytest.raises(
        ProjectionError,
        match="dashboard projection payload is invalid",
    ):
        DashboardProjection(source, gateway).project(31)


def test_dashboard_explains_specification_digest_and_classification() -> None:
    gateway = FakeDashboardGateway()
    source = FakeOutbox(
        (
            _record(
                "dashboard-spec",
                "dashboard_projection",
                2,
                phase="baseline",
                status="advanced",
                disposition="advance",
                spec_sha256="d" * 64,
                spec_classification="policy_approved",
                reason="existing_immutable_assets",
            ),
        )
    )

    DashboardProjection(source, gateway).project(31)

    assert "Specification digest: `" + ("d" * 64) + "`" in gateway.created[0]
    assert "Specification classification: `policy_approved`" in (
        gateway.created[0]
    )
    assert "existing_immutable_assets" in gateway.created[0]


def test_dashboard_renders_redacted_ranked_candidate_slate() -> None:
    gateway = FakeDashboardGateway()
    source = FakeOutbox(
        (
            _record(
                "slate-dashboard-2",
                "candidate_slate_dashboard",
                9,
                phase="awaiting_selection",
                status="waiting",
                disposition="wait",
                spec_sha256="d" * 64,
                baseline_metrics={"quality": 0.5, "safety": 1.0},
                candidate_slate=[
                    {
                        "candidate_id": "candidate-1",
                        "rank": 1,
                        "draft_id": "draft-candidate-1",
                        "metrics": {"quality": 0.9, "safety": 1.0},
                        "deltas": {"quality": 0.4, "safety": 0.0},
                        "guardrails": {"safety": "pass"},
                        "evidence_sha256": "e" * 64,
                        "evidence_url": (
                            "https://github.com/octo-org/optimizer/blob/"
                            "foundry-opt/state/issue-31/objects/evidence/"
                            + "e" * 64
                            + ".json"
                        ),
                    }
                ],
                next_action="merge_exactly_one_candidate_pr",
            ),
        )
    )

    DashboardProjection(source, gateway).project(31)

    body = gateway.created[0]
    assert "| Rank | Candidate | Aggregates | Deltas | Guardrails | Evidence |" in body
    assert "| 1 | `candidate-1` | `quality=0.9`, `safety=1` |" in body
    assert "`quality=+0.4`, `safety=+0`" in body
    assert "`safety=pass`" in body
    assert "[redacted evidence]" in body
    assert "Merge exactly one eligible candidate PR" in body
    assert all(
        forbidden not in body.casefold()
        for forbidden in (
            "raw prompt",
            "raw response",
            "dataset row",
            "tool payload",
        )
    )


def test_dashboard_reports_merge_selection_as_deployment_ready() -> None:
    gateway = FakeDashboardGateway()
    source = FakeOutbox(
        (
            _record(
                "selection-dashboard-2",
                "candidate_selection_dashboard",
                10,
                phase="deployment",
                status="ready",
                disposition="wait",
                spec_sha256="d" * 64,
                selected_candidate_id="candidate-1",
                merge_commit="e" * 40,
                next_action="deployment_ready_for_next_phase",
            ),
        )
    )

    DashboardProjection(source, gateway).project(31)

    body = gateway.created[0]
    assert "Selected candidate: `candidate-1`" in body
    assert "Merge commit: `" + "e" * 40 + "`" in body
    assert "Deployment-ready; deployment has not been dispatched" in body


class FakeCommands:
    def __init__(self, responses: dict[tuple[str, ...], str]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def run(
        self,
        arguments,
        *,
        cwd: Path | None = None,
        environment=None,
        input_text: str | None = None,
        input_bytes: bytes | None = None,
    ) -> CommandResult:
        command = tuple(arguments)
        self.calls.append(
            {
                "arguments": command,
                "input_text": input_text,
            }
        )
        return CommandResult(
            0,
            self.responses.get(command, ""),
            "",
        )


def test_github_dashboard_gateway_uses_structured_api_requests() -> None:
    comments = (
        "gh",
        "api",
        "--paginate",
        "--slurp",
        "repos/octo-org/optimizer/issues/31/comments",
    )
    issue = (
        "gh",
        "api",
        "repos/octo-org/optimizer/issues/31",
    )
    commands = FakeCommands(
        {
            comments: json.dumps(
                [[
                    {
                        "id": 80,
                        "body": (
                            "<!-- foundry-opt:dashboard:issue-31 -->\n"
                            "spoofed"
                        ),
                        "user": {"login": "untrusted-user"},
                    },
                    {
                        "id": 81,
                        "body": (
                            "<!-- foundry-opt:dashboard:issue-31 -->\nold"
                        ),
                        "user": {"login": "github-actions[bot]"},
                    }
                ]]
            ),
            issue: json.dumps(
                {"labels": [{"name": "needs-triage"}]}
            ),
        }
    )
    gateway = GhDashboardGateway(
        commands,
        Path("repository"),
        "octo-org/optimizer",
    )

    assert gateway.find_dashboard(31) == DashboardComment(
        81,
        "<!-- foundry-opt:dashboard:issue-31 -->\nold",
    )
    assert gateway.issue_labels(31) == frozenset({"needs-triage"})
    gateway.update_dashboard(31, 81, "safe dashboard")
    gateway.add_label(31, "optimization-running")
    gateway.remove_label(31, "needs-triage")

    bodies = [
        json.loads(str(call["input_text"]))
        for call in commands.calls
        if call["input_text"] is not None
    ]
    assert {"body": "safe dashboard"} in bodies
    assert {"labels": ["optimization-running"]} in bodies
    assert any(
        call["arguments"][-1]
        == "repos/octo-org/optimizer/issues/31/labels/needs-triage"
        for call in commands.calls
    )
