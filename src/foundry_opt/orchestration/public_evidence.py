from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class FoundryOperation:
    kind: str
    identifier: str
    url: str
    status: str


@dataclass(frozen=True)
class OptimizationReport:
    issue_number: int
    candidate_id: str
    recommendation: str
    alternatives: tuple[str, ...]
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


@dataclass(frozen=True)
class PullRequestProjection:
    title: str
    body: str
    draft: bool


class PublicEvidenceRenderer:
    def render_pr(self, report: OptimizationReport) -> PullRequestProjection:
        metric_rows = []
        for name in sorted(
            set(report.baseline_metrics) | set(report.candidate_metrics)
        ):
            baseline = report.baseline_metrics.get(name)
            candidate = report.candidate_metrics.get(name)
            delta = (
                _signed_number(candidate - baseline)
                if baseline is not None and candidate is not None
                else "n/a"
            )
            metric_rows.append(
                f"{name} | {_optional_number(baseline)} | "
                f"{_optional_number(candidate)} | {delta}"
            )
        operations = "\n".join(
            f"- {operation.kind}: "
            f"[`{operation.identifier}`]({operation.url}) - "
            f"{operation.status}"
            for operation in report.foundry_operations
        )
        guardrails = "\n".join(
            f"- Guardrail `{name}`: **{status}**"
            for name, status in sorted(report.guardrails.items())
        )
        alternatives = "\n".join(
            f"- {alternative}" for alternative in report.alternatives
        )
        changed = "\n".join(
            f"- `{path}`" for path in report.changed_paths
        )
        validation = "\n".join(
            f"- {result}" for result in report.validation
        )
        body = "\n".join(
            (
                "## Copilot recommendation",
                "",
                report.recommendation,
                "",
                "## Alternatives tested",
                "",
                alternatives,
                "",
                "## Evaluation improvement",
                "",
                "Metric | Baseline | Candidate | Delta",
                "--- | ---: | ---: | ---:",
                *metric_rows,
                "",
                guardrails,
                "",
                f"{report.split} split, {report.sample_count} samples",
                "",
                "## Foundry operations",
                "",
                operations,
                "",
                "## Code changes",
                "",
                changed,
                "",
                "## Validation",
                "",
                validation,
                "",
                "## Exact lineage",
                "",
                f"- Spec SHA-256: `{report.spec_sha256}`",
                f"- Base commit: `{report.base_commit}`",
                f"- Patch SHA-256: `{report.patch_sha256}`",
                f"- Evidence SHA-256: `{report.evidence_sha256}`",
                f"- Bundle SHA-256: `{report.bundle_sha256}`",
                f"- Expected tree: `{report.expected_tree}`",
                "",
                "## Your action",
                "",
                f"Merge this PR to select and deploy "
                f"`{report.candidate_id}`.",
            )
        )
        return PullRequestProjection(
            title=(
                f"[Optimize] #{report.issue_number} selected candidate"
            ),
            body=body,
            draft=False,
        )


def _number(value: float) -> str:
    return f"{value:g}"


def _optional_number(value: float | None) -> str:
    return "n/a" if value is None else _number(value)


def _signed_number(value: float) -> str:
    return f"{value:+g}" if value else "0"
