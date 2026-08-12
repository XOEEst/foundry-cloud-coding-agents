from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Protocol
from urllib.parse import quote

from foundry_opt.orchestration.git_state import (
    GitStateRef,
    OutboxRecord,
)
from foundry_opt.preflight.interfaces import CommandRunner


_DASHBOARD = "dashboard_projection"
_CANDIDATE_SLATE_DASHBOARD = "candidate_slate_dashboard"
_CANDIDATE_SELECTION_DASHBOARD = "candidate_selection_dashboard"
_DEPLOYMENT_DASHBOARD = "deployment_dashboard"
_DEPLOYMENT_FINAL_DASHBOARD = "deployment_final_dashboard"
_DEPLOYMENT_READY_FOR_HUMAN = "deployment_ready_for_human"
_DASHBOARD_KINDS = frozenset(
    {
        _DASHBOARD,
        _CANDIDATE_SLATE_DASHBOARD,
        _CANDIDATE_SELECTION_DASHBOARD,
        _DEPLOYMENT_DASHBOARD,
        _DEPLOYMENT_FINAL_DASHBOARD,
        _DEPLOYMENT_READY_FOR_HUMAN,
        "campaign_advanced",
        "campaign_waiting",
    }
)
_LABEL_ADD = "label_add"
_LABEL_REMOVE = "label_remove"
_PROJECTED_KINDS = frozenset(
    {*_DASHBOARD_KINDS, _LABEL_ADD, _LABEL_REMOVE}
)
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
)


class ProjectionError(ValueError):
    pass


@dataclass(frozen=True)
class DashboardComment:
    comment_id: int
    body: str


class ProjectionOutbox(Protocol):
    def for_issue(
        self,
        issue_number: int,
    ) -> tuple[OutboxRecord, ...]: ...


class DashboardGateway(Protocol):
    def find_dashboard(
        self,
        issue_number: int,
    ) -> DashboardComment | None: ...

    def create_dashboard(self, issue_number: int, body: str) -> None: ...

    def update_dashboard(
        self,
        issue_number: int,
        comment_id: int,
        body: str,
    ) -> None: ...

    def issue_labels(self, issue_number: int) -> frozenset[str]: ...

    def add_label(self, issue_number: int, label: str) -> None: ...

    def remove_label(self, issue_number: int, label: str) -> None: ...


class DashboardProjection:
    """Project only effects explicitly emitted by the Copilot steward."""

    def __init__(
        self,
        outbox: ProjectionOutbox,
        gateway: DashboardGateway,
    ) -> None:
        self._outbox = outbox
        self._gateway = gateway

    def project(self, issue_number: int) -> None:
        records = tuple(
            sorted(
                (
                    record
                    for record in self._outbox.for_issue(issue_number)
                    if record.kind in _PROJECTED_KINDS
                ),
                key=lambda item: (
                    item.generation,
                    item.sequence,
                    item.record_id,
                ),
            )
        )
        dashboard = next(
            (
                record
                for record in reversed(records)
                if record.kind in _DASHBOARD_KINDS
            ),
            None,
        )
        if dashboard is not None:
            self._project_dashboard(
                issue_number,
                dashboard,
                tuple(
                    record
                    for record in records
                    if record.kind in _DASHBOARD_KINDS
                ),
            )
        self._project_labels(issue_number, records)

    def _project_dashboard(
        self,
        issue_number: int,
        record: OutboxRecord,
        records: tuple[OutboxRecord, ...],
    ) -> None:
        for candidate in records:
            _require_issue(candidate, issue_number)
            _validate_dashboard_payload(candidate)
        marker = _projection_marker(record.record_id)
        existing = self._gateway.find_dashboard(issue_number)
        if existing is not None and marker in existing.body:
            return
        body = _dashboard_body(issue_number, record, records)
        if existing is None:
            self._gateway.create_dashboard(issue_number, body)
        else:
            self._gateway.update_dashboard(
                issue_number,
                existing.comment_id,
                body,
            )

    def _project_labels(
        self,
        issue_number: int,
        records: tuple[OutboxRecord, ...],
    ) -> None:
        labels = set(self._gateway.issue_labels(issue_number))
        for record in records:
            if record.kind not in {_LABEL_ADD, _LABEL_REMOVE}:
                continue
            _require_issue(record, issue_number)
            if set(record.payload) != {"issue_number", "label"}:
                raise ProjectionError("label projection payload is invalid")
            label = record.payload["label"]
            if not isinstance(label, str):
                raise ProjectionError("label must be text")
            if record.kind == _LABEL_ADD and label not in labels:
                self._gateway.add_label(issue_number, label)
                labels.add(label)
            elif record.kind == _LABEL_REMOVE and label in labels:
                self._gateway.remove_label(issue_number, label)
                labels.remove(label)


class GitStateProjectionOutbox:
    def __init__(
        self,
        repository_root: Path,
        store: GitStateRef | None = None,
    ) -> None:
        self._root = repository_root
        self._store = store or GitStateRef()

    def for_issue(
        self,
        issue_number: int,
    ) -> tuple[OutboxRecord, ...]:
        snapshot = self._store.load(self._root, issue_number)
        return () if snapshot is None else snapshot.outbox


class GhDashboardGateway:
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

    def find_dashboard(
        self,
        issue_number: int,
    ) -> DashboardComment | None:
        marker = dashboard_marker(issue_number)
        response = self._run_json(
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
        if not isinstance(response, list):
            raise ProjectionError("issue comments response is invalid")
        for page in response:
            if not isinstance(page, list):
                raise ProjectionError("issue comments response is invalid")
            for item in page:
                if not isinstance(item, dict):
                    raise ProjectionError(
                        "issue comments response is invalid"
                    )
                body = item.get("body")
                comment_id = item.get("id")
                user = item.get("user")
                if (
                    not isinstance(user, dict)
                    or not isinstance(user.get("login"), str)
                ):
                    raise ProjectionError(
                        "issue comment author is invalid"
                    )
                if (
                    isinstance(body, str)
                    and marker in body
                    and type(comment_id) is int
                    and comment_id > 0
                    and user["login"] == "github-actions[bot]"
                ):
                    return DashboardComment(comment_id, body)
        return None

    def create_dashboard(self, issue_number: int, body: str) -> None:
        self._write_json(
            "POST",
            (
                f"repos/{self._repository}/issues/"
                f"{issue_number}/comments"
            ),
            {"body": body},
        )

    def update_dashboard(
        self,
        issue_number: int,
        comment_id: int,
        body: str,
    ) -> None:
        if type(comment_id) is not int or comment_id < 1:
            raise ProjectionError("comment ID must be positive")
        self._write_json(
            "PATCH",
            (
                f"repos/{self._repository}/issues/comments/"
                f"{comment_id}"
            ),
            {"body": body},
        )

    def issue_labels(self, issue_number: int) -> frozenset[str]:
        response = self._run_json(
            (
                "gh",
                "api",
                (
                    f"repos/{self._repository}/issues/"
                    f"{issue_number}"
                ),
            )
        )
        if not isinstance(response, dict):
            raise ProjectionError("issue response is invalid")
        labels = response.get("labels")
        if not isinstance(labels, list):
            raise ProjectionError("issue labels response is invalid")
        names: set[str] = set()
        for item in labels:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("name"), str)
            ):
                raise ProjectionError("issue labels response is invalid")
            names.add(item["name"])
        return frozenset(names)

    def add_label(self, issue_number: int, label: str) -> None:
        self._write_json(
            "POST",
            (
                f"repos/{self._repository}/issues/"
                f"{issue_number}/labels"
            ),
            {"labels": [label]},
        )

    def remove_label(self, issue_number: int, label: str) -> None:
        self._commands.run(
            (
                "gh",
                "api",
                "--method",
                "DELETE",
                (
                    f"repos/{self._repository}/issues/"
                    f"{issue_number}/labels/{quote(label, safe='')}"
                ),
            ),
            cwd=self._root,
        )

    def _run_json(self, arguments: tuple[str, ...]):
        result = self._commands.run(arguments, cwd=self._root)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ProjectionError(
                "GitHub response is not valid JSON"
            ) from error

    def _write_json(
        self,
        method: str,
        endpoint: str,
        document: dict[str, object],
    ) -> None:
        self._commands.run(
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
            input_text=json.dumps(
                document,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )


def dashboard_marker(issue_number: int) -> str:
    if type(issue_number) is not int or issue_number < 1:
        raise ProjectionError("issue number must be positive")
    return f"<!-- foundry-opt:dashboard:issue-{issue_number} -->"


def _projection_marker(record_id: str) -> str:
    return f"<!-- foundry-opt:projection:{record_id} -->"


def _require_issue(record: OutboxRecord, issue_number: int) -> None:
    if record.payload.get("issue_number") != issue_number:
        raise ProjectionError("projection issue does not match outbox")


def _validate_dashboard_payload(record: OutboxRecord) -> None:
    expected = {
        "disposition",
        "issue_number",
        "phase",
        "status",
    }
    optional = {
        "effect_id",
        "reason",
        "spec_classification",
        "spec_sha256",
    }
    if record.kind == _CANDIDATE_SLATE_DASHBOARD:
        expected.update(
            {
                "baseline_metrics",
                "candidate_slate",
                "next_action",
                "spec_sha256",
            }
        )
    if record.kind == _CANDIDATE_SELECTION_DASHBOARD:
        expected.update(
            {
                "merge_commit",
                "next_action",
                "selected_candidate_id",
                "spec_sha256",
            }
        )
    if record.kind == _DEPLOYMENT_DASHBOARD:
        expected.update(
            {
                "candidate_id",
                "merge_commit",
                "next_action",
            }
        )
    if record.kind == _DEPLOYMENT_READY_FOR_HUMAN:
        expected.add("next_action")
    if record.kind == _DEPLOYMENT_FINAL_DASHBOARD:
        expected.update(
            {
                "baseline_metrics",
                "bundle_sha256",
                "candidate_id",
                "deployed_metrics",
                "deployment_version",
                "draft_metrics",
                "evidence_sha256",
                "lineage_sha256",
                "merge_actor",
                "merge_commit",
                "metadata_sha256",
                "patch_sha256",
                "portal_url",
                "run_id",
                "run_url",
                "required_checks",
                "source_sha256",
                "spec_sha256",
                "tree_sha",
            }
        )
    if (
        not expected <= set(record.payload)
        or set(record.payload) - expected - optional
    ):
        raise ProjectionError("dashboard projection payload is invalid")


def _dashboard_body(
    issue_number: int,
    record: OutboxRecord,
    records: tuple[OutboxRecord, ...],
) -> str:
    slate = _latest_record(records, _CANDIDATE_SLATE_DASHBOARD)
    selection = _latest_record(
        records,
        _CANDIDATE_SELECTION_DASHBOARD,
    )
    deployment = _latest_record(
        records,
        _DEPLOYMENT_DASHBOARD,
        _DEPLOYMENT_READY_FOR_HUMAN,
        _DEPLOYMENT_FINAL_DASHBOARD,
    )
    milestones = tuple(
        candidate
        for candidate in (slate, selection, deployment, record)
        if candidate is not None
    )
    markers = "\n".join(
        _projection_marker(candidate.record_id)
        for index, candidate in enumerate(milestones)
        if candidate.record_id
        not in {
            earlier.record_id
            for earlier in milestones[:index]
        }
    )
    details = _specification_details(records)
    sections = ""
    if slate is not None:
        sections += _dashboard_section(
            slate,
            historical=selection is not None or deployment is not None,
        )
    for candidate in (selection, deployment):
        if candidate is not None:
            sections += _dashboard_section(candidate)
    return (
        f"{dashboard_marker(issue_number)}\n"
        f"{markers}\n"
        "## Foundry optimization dashboard\n\n"
        f"- Generation: `{record.generation}`\n"
        f"- Sequence: `{record.sequence}`\n"
        f"- Phase: `{record.payload['phase']}`\n"
        f"- Status: `{record.payload['status']}`\n"
        f"- Disposition: `{record.payload['disposition']}`\n"
        f"{details}"
        f"{sections}"
    )


def _latest_record(
    records: tuple[OutboxRecord, ...],
    *kinds: str,
) -> OutboxRecord | None:
    return next(
        (
            record
            for record in reversed(records)
            if record.kind in kinds
        ),
        None,
    )


def _specification_details(
    records: tuple[OutboxRecord, ...],
) -> str:
    lines: list[str] = []
    fields = (
        ("spec_classification", "Specification classification"),
        ("spec_sha256", "Specification digest"),
        ("reason", "Reason"),
    )
    for field, label in fields:
        source = next(
            (
                record
                for record in reversed(records)
                if field in record.payload
            ),
            None,
        )
        if source is not None:
            lines.append(f"- {label}: `{source.payload[field]}`")
    return "\n".join(lines) + ("\n" if lines else "")


def _dashboard_section(
    record: OutboxRecord,
    *,
    historical: bool = False,
) -> str:
    if record.kind == _CANDIDATE_SLATE_DASHBOARD:
        baseline = record.payload["baseline_metrics"]
        baseline_text = ", ".join(
            f"`{name}={_number(value)}`"
            for name, value in sorted(baseline.items())
        ) or "none"
        table = [
            "| Rank | Candidate | Aggregates | Deltas | Guardrails | Evidence |",
            "| ---: | --- | --- | --- | --- | --- |",
        ]
        for row in record.payload["candidate_slate"]:
            metrics = ", ".join(
                f"`{name}={_number(value)}`"
                for name, value in sorted(row["metrics"].items())
            ) or "—"
            deltas = ", ".join(
                f"`{name}={_signed_number(value)}`"
                for name, value in sorted(row["deltas"].items())
            ) or "—"
            guardrails = ", ".join(
                f"`{name}={outcome}`"
                for name, outcome in sorted(row["guardrails"].items())
            ) or "—"
            table.append(
                "| "
                f"{row['rank']} | `{row['candidate_id']}` | "
                f"{metrics} | {deltas} | {guardrails} | "
                f"[redacted evidence]({row['evidence_url']}) |"
            )
        action = (
            (
                "### Historical selection evidence",
                "",
                "This pre-merge comparison is retained for audit history. "
                "It is not a current merge instruction.",
            )
            if historical
            else (
                "### Next human action",
                "",
                "Merge exactly one eligible candidate PR. A comment, label, "
                "or CLI command does not select a candidate. Deployment is "
                "not dispatched in this phase.",
            )
        )
        return "\n".join(
            (
                "",
                "### Candidate comparison",
                "",
                f"- Baseline aggregates: {baseline_text}",
                "",
                *table,
                "",
                *action,
                "",
            )
        )
    if record.kind == _CANDIDATE_SELECTION_DASHBOARD:
        return "\n".join(
            (
                "",
                "### Candidate selection",
                "",
                "- Selected candidate: "
                f"`{record.payload['selected_candidate_id']}`",
                f"- Merge commit: `{record.payload['merge_commit']}`",
                "- Next action: Deployment-ready; deployment has not been "
                "dispatched. The next gated phase owns deployment.",
                "",
            )
        )
    if record.kind == _DEPLOYMENT_DASHBOARD:
        return "\n".join(
            (
                "",
                "### Deployment",
                "",
                f"- Candidate: `{record.payload['candidate_id']}`",
                f"- Merge commit: `{record.payload['merge_commit']}`",
                "- The exact deployment intent is durable and awaiting the "
                "separate deployment bridge.",
                "",
            )
        )
    if record.kind == _DEPLOYMENT_READY_FOR_HUMAN:
        return "\n".join(
            (
                "",
                "### Deployment needs human attention",
                "",
                f"- Remediation: `{record.payload['next_action']}`",
                "- The root issue remains open. No automatic rollback was "
                "claimed.",
                "",
            )
        )
    if record.kind == _DEPLOYMENT_FINAL_DASHBOARD:
        baseline = _aggregate_text(record.payload["baseline_metrics"])
        draft = _aggregate_text(record.payload["draft_metrics"])
        deployed = _aggregate_text(record.payload["deployed_metrics"])
        return "\n".join(
            (
                "",
                "### Verified deployment result",
                "",
                f"- Candidate: `{record.payload['candidate_id']}`",
                "- Published version: "
                f"`{record.payload['deployment_version']}`",
                "- Deployment lineage SHA-256: "
                f"`{record.payload['lineage_sha256']}`",
                f"- Merge actor: `{record.payload['merge_actor']}`",
                "- Required checks: "
                + ", ".join(
                    f"`{check}`"
                    for check in record.payload["required_checks"]
                ),
                f"- Merge commit: `{record.payload['merge_commit']}`",
                f"- Tree: `{record.payload['tree_sha']}`",
                f"- Patch SHA-256: `{record.payload['patch_sha256']}`",
                f"- Bundle SHA-256: `{record.payload['bundle_sha256']}`",
                f"- Evidence SHA-256: `{record.payload['evidence_sha256']}`",
                f"- Workflow run: {record.payload['run_url']}",
                f"- Foundry portal: {record.payload['portal_url']}",
                f"- Baseline aggregates: {baseline}",
                f"- Selected draft aggregates: {draft}",
                f"- Deployed aggregates: {deployed}",
                "",
            )
        )
    return ""


def _aggregate_text(metrics: object) -> str:
    if not isinstance(metrics, dict):
        raise ProjectionError("aggregate metrics are invalid")
    return ", ".join(
        f"`{name}={_number(value)}`"
        for name, value in sorted(metrics.items())
    ) or "none"


def _number(value: float) -> str:
    return format(value, "g")


def _signed_number(value: float) -> str:
    return f"{value:+g}"
