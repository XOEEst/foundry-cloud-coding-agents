from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import json
import math
from pathlib import PurePosixPath
import re
from types import MappingProxyType
from typing import Mapping

from foundry_opt.orchestration.workspace_attribution import (
    WorkspaceCandidateProvenance,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OBJECT = re.compile(r"^[0-9a-f]{40}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_CHECK_STATES = frozenset(
    {"success", "failure", "pending", "cancelled", "skipped"}
)


class EvidenceMergeGate(StrEnum):
    PENDING = "pending"
    ELIGIBLE = "eligible"
    BLOCKED = "blocked"
    SELECTED = "selected"
    DEPLOYED = "deployed"


@dataclass(frozen=True)
class AlternativeResult:
    candidate_id: str
    outcome: str
    rejection_reason: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.candidate_id, "alternative candidate ID")
        if self.outcome not in {"selected", "rejected", "failed"}:
            raise ValueError("alternative outcome is invalid")
        if self.outcome in {"rejected", "failed"} and not (
            isinstance(self.rejection_reason, str)
            and self.rejection_reason.strip()
        ):
            raise ValueError(
                "rejected and failed alternatives require a reason"
            )
        if self.rejection_reason is not None:
            object.__setattr__(
                self,
                "rejection_reason",
                self.rejection_reason.strip(),
            )


@dataclass(frozen=True)
class FoundryOperation:
    kind: str
    identifier: str
    url: str
    status: str
    started_at: str | None = None
    completed_at: str | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.kind, "Foundry operation kind"),
            (self.identifier, "Foundry operation ID"),
            (self.status, "Foundry operation status"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        if (
            not isinstance(self.url, str)
            or not self.url.startswith(("https://", "http://"))
        ):
            raise ValueError("Foundry operation URL must be HTTP(S)")
        for value, name in (
            (self.started_at, "Foundry operation start time"),
            (self.completed_at, "Foundry operation completion time"),
        ):
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise ValueError(f"{name} must be non-empty when present")


@dataclass(frozen=True)
class OptimizationReport:
    issue_number: int
    candidate_id: str
    recommendation: str
    alternatives: tuple[AlternativeResult | str, ...]
    baseline_metrics: Mapping[str, float]
    candidate_metrics: Mapping[str, float]
    guardrails: Mapping[str, str]
    thresholds: Mapping[str, float]
    sample_count: int
    split: str
    foundry_operations: tuple[FoundryOperation, ...]
    changed_paths: tuple[str, ...]
    validation: tuple[str, ...]
    spec_sha256: str
    base_commit: str
    patch_sha256: str
    evidence_sha256: str
    bundle_sha256: str
    expected_tree: str
    materiality: Mapping[str, float] = field(default_factory=dict)
    required_checks: Mapping[str, str] = field(default_factory=dict)
    merge_gate: EvidenceMergeGate = EvidenceMergeGate.PENDING
    candidate_provenance: WorkspaceCandidateProvenance | None = None

    def __post_init__(self) -> None:
        if type(self.issue_number) is not int or self.issue_number < 1:
            raise ValueError("issue_number must be positive")
        _require_identifier(self.candidate_id, "candidate_id")
        if not isinstance(self.recommendation, str):
            raise ValueError("recommendation must be text")
        alternatives = tuple(self.alternatives)
        if any(
            not isinstance(item, (AlternativeResult, str))
            for item in alternatives
        ):
            raise ValueError("alternatives are invalid")
        object.__setattr__(self, "alternatives", alternatives)
        for field_name in (
            "baseline_metrics",
            "candidate_metrics",
            "thresholds",
            "materiality",
        ):
            object.__setattr__(
                self,
                field_name,
                _numeric_mapping(getattr(self, field_name), field_name),
            )
        guardrails = _text_mapping(self.guardrails, "guardrails")
        checks = _text_mapping(self.required_checks, "required_checks")
        if any(status not in _CHECK_STATES for status in checks.values()):
            raise ValueError("required check status is invalid")
        object.__setattr__(self, "guardrails", guardrails)
        object.__setattr__(self, "required_checks", checks)
        if type(self.sample_count) is not int or self.sample_count < 0:
            raise ValueError("sample_count must be non-negative")
        if not isinstance(self.split, str) or not self.split.strip():
            raise ValueError("split must be non-empty")
        operations = tuple(self.foundry_operations)
        if any(not isinstance(item, FoundryOperation) for item in operations):
            raise ValueError("foundry_operations are invalid")
        object.__setattr__(self, "foundry_operations", operations)
        paths = tuple(_repository_path(path) for path in self.changed_paths)
        if len(set(paths)) != len(paths):
            raise ValueError("changed_paths must be unique")
        object.__setattr__(self, "changed_paths", paths)
        validation = tuple(self.validation)
        if any(not isinstance(item, str) for item in validation):
            raise ValueError("validation results must be text")
        object.__setattr__(self, "validation", validation)
        for value, name in (
            (self.spec_sha256, "spec_sha256"),
            (self.patch_sha256, "patch_sha256"),
            (self.evidence_sha256, "evidence_sha256"),
            (self.bundle_sha256, "bundle_sha256"),
        ):
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise ValueError(f"{name} must be a SHA-256 digest")
        for value, name in (
            (self.base_commit, "base_commit"),
            (self.expected_tree, "expected_tree"),
        ):
            if not isinstance(value, str) or not _GIT_OBJECT.fullmatch(value):
                raise ValueError(f"{name} must be a full Git object ID")
        if not isinstance(self.merge_gate, EvidenceMergeGate):
            raise ValueError("merge_gate must be an EvidenceMergeGate")
        if (
            self.candidate_provenance is not None
            and type(self.candidate_provenance)
            is not WorkspaceCandidateProvenance
        ):
            raise ValueError("candidate_provenance is invalid")
        if self.merge_gate is EvidenceMergeGate.ELIGIBLE and (
            not checks or any(status != "success" for status in checks.values())
        ):
            raise ValueError(
                "eligible merge gate requires successful required checks"
            )


@dataclass(frozen=True)
class PullRequestProjection:
    title: str
    body: str
    draft: bool


@dataclass(frozen=True)
class IssueProjection:
    marker: str
    body: str


@dataclass(frozen=True)
class CheckProjection:
    name: str
    title: str
    summary: str
    status: str
    conclusion: str | None


class PublicEvidenceRenderer:
    def render_pr(self, report: OptimizationReport) -> PullRequestProjection:
        body = self._body(report)
        return PullRequestProjection(
            title=f"[Optimize] #{report.issue_number} selected candidate",
            body=body,
            draft=report.merge_gate in {
                EvidenceMergeGate.PENDING,
                EvidenceMergeGate.BLOCKED,
            },
        )

    def render_issue(self, report: OptimizationReport) -> IssueProjection:
        marker = public_evidence_marker(report)
        milestone = {
            EvidenceMergeGate.PENDING: (
                "Candidate evidence is still pending trusted checks."
            ),
            EvidenceMergeGate.ELIGIBLE: (
                "Candidate evidence is complete and the candidate is "
                "eligible for human selection."
            ),
            EvidenceMergeGate.BLOCKED: (
                "Candidate evidence is blocked and is not eligible for "
                "selection."
            ),
            EvidenceMergeGate.SELECTED: (
                "The candidate was selected by its trusted merge event; "
                "deployment is not implied."
            ),
            EvidenceMergeGate.DEPLOYED: (
                "The candidate was already selected and deployed. The "
                "pre-merge evidence above remains the selection record."
            ),
        }[report.merge_gate]
        heading = (
            "Deployment milestone"
            if report.merge_gate is EvidenceMergeGate.DEPLOYED
            else (
                "Selection milestone"
                if report.merge_gate is EvidenceMergeGate.SELECTED
                else "Candidate milestone"
            )
        )
        return IssueProjection(
            marker=marker,
            body="\n".join(
                (
                    self._body(report),
                    "",
                    f"## {heading}",
                    "",
                    milestone,
                )
            ),
        )

    def render_check(self, report: OptimizationReport) -> CheckProjection:
        if report.merge_gate is EvidenceMergeGate.PENDING:
            status = "in_progress"
            conclusion = None
            title = f"{report.candidate_id} evidence is pending"
        elif report.merge_gate is EvidenceMergeGate.BLOCKED:
            status = "completed"
            conclusion = "failure"
            title = f"{report.candidate_id} is blocked from merge"
        else:
            status = "completed"
            conclusion = "success"
            title = f"{report.candidate_id} evidence is verified"
        return CheckProjection(
            name="Foundry exact candidate check",
            title=title,
            summary=self._body(report),
            status=status,
            conclusion=conclusion,
        )

    def _body(self, report: OptimizationReport) -> str:
        return "\n".join(
            (
                public_evidence_marker(report),
                "## Who did what",
                "",
                public_actor_ledger(report.candidate_provenance),
                "",
                "## Copilot investigation",
                "",
                _candidate_attribution(report),
                "",
                "## Optimizer recommendation",
                "",
                _safe_prose(report.recommendation)
                or "No recommendation was recorded.",
                "",
                "## Evaluation improvement",
                "",
                _metric_table(report),
                "",
                "## Evaluation policy",
                "",
                _policy_table(report),
                "",
                f"{_safe_inline(report.split)} split, "
                f"{report.sample_count} samples",
                "",
                "## Guardrails",
                "",
                _guardrails(report.guardrails),
                "",
                "## Foundry operations",
                "",
                _operations(report.foundry_operations),
                "",
                "## Code changes — exact changed paths",
                "",
                _changed_paths(report.changed_paths),
                "",
                "## Validation",
                "",
                _validation(report.validation),
                "",
                "## Exact lineage",
                "",
                f"- Spec SHA-256: `{report.spec_sha256}`",
                f"- Base commit: `{report.base_commit}`",
                f"- Patch SHA-256: `{report.patch_sha256}`",
                f"- Evidence SHA-256: `{report.evidence_sha256}`",
                f"- Bundle SHA-256: `{report.bundle_sha256}`",
                f"- Expected tree: `{report.expected_tree}`",
                (
                    "- Copilot provenance SHA-256: "
                    f"`{report.candidate_provenance.identity_sha256}`"
                    if report.candidate_provenance is not None
                    else "- Copilot provenance unavailable"
                ),
                "",
                "## Merge gate",
                "",
                _merge_gate(report),
                "",
                "## Your action",
                "",
                _merge_action(report),
            )
        )


def public_evidence_marker(report: OptimizationReport) -> str:
    document = {
        "base_commit": report.base_commit,
        "bundle_sha256": report.bundle_sha256,
        "candidate_id": report.candidate_id,
        "evidence_sha256": report.evidence_sha256,
        "expected_tree": report.expected_tree,
        "issue_number": report.issue_number,
        "merge_gate": report.merge_gate.value,
        "patch_sha256": report.patch_sha256,
        "provenance_identity_sha256": (
            report.candidate_provenance.identity_sha256
            if report.candidate_provenance is not None
            else None
        ),
        "required_checks": dict(report.required_checks),
        "spec_sha256": report.spec_sha256,
    }
    digest = hashlib.sha256(
        json.dumps(
            document,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return (
        "<!-- foundry-opt:public-evidence:v1:"
        f"issue-{report.issue_number}:{digest[:24]} -->"
    )


def public_actor_ledger(
    provenance: WorkspaceCandidateProvenance | None,
    *,
    deployment_run_url: str | None = None,
    final: bool = False,
) -> str:
    if provenance is None:
        copilot = "Copilot provenance unavailable."
        importer = "trusted import provenance unavailable"
    else:
        copilot = (
            "candidate investigation; "
            f"[source commit]({provenance.candidate_source_commit_url}) and "
            "[acknowledgement comment]"
            f"({provenance.acknowledgement_comment_url}) verified"
        )
        importer = (
            f"[trusted import]({provenance.importer_workflow_run_url})"
        )
    human_action = (
        "created the request; merge was the only requested human action. "
        "No login identity is inferred."
        if final
        else (
            "created the request; merge remains the only requested human "
            "action once trusted evidence is eligible. No login identity "
            "is inferred."
        )
    )
    optimizer = (
        f"{importer}; evaluation, selection, exact verification, deployment "
        "reconciliation, and public evidence/closure handling"
    )
    if deployment_run_url is not None:
        optimizer += f"; [deployment run]({deployment_run_url})"
    else:
        optimizer += (
            "; existing Foundry run links are retained in candidate evidence"
        )
    return "\n".join(
        (
            f"- **Human** — {human_action}",
            f"- **GitHub Copilot** — {copilot}",
            (
                "- **Foundry Optimizer (`github-actions[bot]` interim)** — "
                f"{optimizer}."
            ),
        )
    )


def _candidate_attribution(report: OptimizationReport) -> str:
    provenance = report.candidate_provenance
    commit = (
        f"[`{provenance.candidate_source_commit_sha[:12]}`]"
        f"({provenance.candidate_source_commit_url})"
        if provenance is not None
        else "Copilot provenance unavailable"
    )
    acknowledgement = (
        f"[comment]({provenance.acknowledgement_comment_url})"
        if provenance is not None
        else "Copilot provenance unavailable"
    )
    rows = [
        (
            "Candidate | Copilot commit | Copilot acknowledgement | "
            "Evaluation outcome / rejection reason"
        ),
        "--- | --- | --- | ---",
    ]
    alternatives = report.alternatives or (
        AlternativeResult(
            candidate_id=report.candidate_id,
            outcome="selected",
        ),
    )
    for item in alternatives:
        if isinstance(item, AlternativeResult):
            candidate_id = item.candidate_id
            outcome = item.outcome + (
                f": {item.rejection_reason}"
                if item.rejection_reason
                else ""
            )
        else:
            parsed_id, separator, detail = item.partition(":")
            candidate_id = (
                parsed_id.strip()
                if separator
                and _IDENTIFIER.fullmatch(parsed_id.strip()) is not None
                else report.candidate_id
            )
            outcome = detail.strip() if separator and detail.strip() else item
        selected_provenance = candidate_id == report.candidate_id
        rows.append(
            f"{_safe_inline(candidate_id)} | "
            f"{commit if selected_provenance else '—'} | "
            f"{acknowledgement if selected_provenance else '—'} | "
            f"{_safe_inline(outcome)}"
        )
    return "\n".join(rows)


def _metric_table(report: OptimizationReport) -> str:
    names = sorted(
        set(report.baseline_metrics) | set(report.candidate_metrics)
    )
    if not names:
        return "No aggregate metrics were reported."
    rows = [
        "Metric | Baseline | Candidate | Delta",
        "--- | ---: | ---: | ---:",
    ]
    for name in names:
        baseline = report.baseline_metrics.get(name)
        candidate = report.candidate_metrics.get(name)
        delta = (
            _signed_number(candidate - baseline)
            if baseline is not None and candidate is not None
            else "n/a"
        )
        rows.append(
            f"{_safe_inline(name)} | {_optional_number(baseline)} | "
            f"{_optional_number(candidate)} | {delta}"
        )
    return "\n".join(rows)


def _policy_table(report: OptimizationReport) -> str:
    names = sorted(set(report.thresholds) | set(report.materiality))
    if not names:
        return "No thresholds or materiality values were recorded."
    rows = [
        "Metric | Threshold | Materiality",
        "--- | ---: | ---:",
    ]
    for name in names:
        rows.append(
            f"{_safe_inline(name)} | "
            f"{_optional_number(report.thresholds.get(name))} | "
            f"{_optional_number(report.materiality.get(name))}"
        )
    return "\n".join(rows)


def _guardrails(guardrails: Mapping[str, str]) -> str:
    if not guardrails:
        return "No guardrail results were recorded."
    return "\n".join(
        f"- Guardrail `{_safe_inline(name)}`: "
        f"**{_safe_inline(status)}**"
        for name, status in sorted(guardrails.items())
    )


def _operations(operations: tuple[FoundryOperation, ...]) -> str:
    if not operations:
        return "No Foundry operations were recorded."
    rows = [
        "Operation | ID | Status | Started | Completed",
        "--- | --- | --- | --- | ---",
    ]
    for operation in operations:
        rows.append(
            f"{_safe_inline(operation.kind)} | "
            f"[`{_safe_inline(operation.identifier)}`]({operation.url}) | "
            f"{_safe_inline(operation.status)} | "
            + (
                _safe_inline(operation.started_at)
                if operation.started_at
                else "n/a"
            )
            + " | "
            + (
                _safe_inline(operation.completed_at)
                if operation.completed_at
                else "n/a"
            )
        )
    return "\n".join(rows)


def _changed_paths(paths: tuple[str, ...]) -> str:
    if not paths:
        return "No changed paths were recorded."
    return "\n".join(f"- `{path}`" for path in paths)


def _validation(results: tuple[str, ...]) -> str:
    if not results:
        return "No validation results were recorded."
    return "\n".join(f"- {_safe_prose(result)}" for result in results)


def _merge_gate(report: OptimizationReport) -> str:
    checks = (
        "\n".join(
            f"- `{_safe_inline(name)}`: **{_safe_inline(status)}**"
            for name, status in sorted(report.required_checks.items())
        )
        if report.required_checks
        else "- No required check results were recorded."
    )
    return "\n".join(
        (
            f"- Trusted state: **{report.merge_gate.value}**",
            checks,
        )
    )


def _merge_action(report: OptimizationReport) -> str:
    if report.merge_gate is EvidenceMergeGate.ELIGIBLE:
        return (
            "Merge this PR to select and deploy "
            f"`{report.candidate_id}`."
        )
    if report.merge_gate is EvidenceMergeGate.PENDING:
        return (
            "Do not merge this PR until the trusted merge gate becomes "
            "eligible."
        )
    if report.merge_gate is EvidenceMergeGate.BLOCKED:
        return (
            "Do not merge this PR. Trusted evidence blocks this candidate."
        )
    if report.merge_gate is EvidenceMergeGate.SELECTED:
        return (
            f"`{report.candidate_id}` was selected by merge. No additional "
            "merge action is required."
        )
    return (
        f"`{report.candidate_id}` is already selected and deployed. "
        "No merge action is required."
    )


def _numeric_mapping(
    values: Mapping[str, float],
    field_name: str,
) -> Mapping[str, float]:
    if not isinstance(values, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    normalized: dict[str, float] = {}
    for name, value in values.items():
        if (
            not isinstance(name, str)
            or not name
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError(f"{field_name} is invalid")
        normalized[name] = float(value)
    return MappingProxyType(normalized)


def _text_mapping(
    values: Mapping[str, str],
    field_name: str,
) -> Mapping[str, str]:
    if not isinstance(values, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    normalized = dict(values)
    if any(
        not isinstance(name, str)
        or not name
        or not isinstance(value, str)
        or not value
        for name, value in normalized.items()
    ):
        raise ValueError(f"{field_name} is invalid")
    return MappingProxyType(normalized)


def _repository_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("changed path must be text")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or "`" in normalized
        or any(ord(character) < 32 for character in normalized)
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("changed repository path is invalid")
    return path.as_posix()


def _require_identifier(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field_name} is invalid")


def _safe_prose(value: str) -> str:
    escaped = value.replace("<!--", "&lt;!--").replace("-->", "--&gt;")
    return "\n".join(
        line.replace("#", r"\#", 1)
        if line.lstrip().startswith("#")
        else line
        for line in escaped.strip().splitlines()
    )


def _safe_inline(value: str | None) -> str:
    if value is None:
        return ""
    return (
        _safe_prose(value)
        .replace("|", "&#124;")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _number(value: float) -> str:
    return f"{value:g}"


def _optional_number(value: float | None) -> str:
    return "n/a" if value is None else _number(value)


def _signed_number(value: float) -> str:
    return f"{value:+g}" if value else "0"
