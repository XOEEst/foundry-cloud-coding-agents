from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Protocol

from foundry_opt.adapters.commands import CommandError, SubprocessCommandRunner
from foundry_opt.adapters.github import github_repository_from_remote_url
from foundry_opt.evaluation import (
    MetricDirection,
    MetricPolicy,
    UndefinedBehavior,
)
from foundry_opt.optimization.issues import (
    IssueSpecificationError,
    parse_optimization_issue_request,
)
from foundry_opt.orchestration.workspace import WorkspacePhase
from foundry_opt.orchestration.workspace_git_store import GitWorkspaceStore
from foundry_opt.orchestration.workspace_store import WorkspaceSnapshot
from foundry_opt.security import reject_secret_content


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SUMMARY_TEXT = re.compile(r"^[^\x00-\x1f]{1,4096}$")


def _positive_integer(value: object, name: str) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} is invalid")


def _identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")


def _repository(value: str, name: str) -> None:
    if not isinstance(value, str) or _REPOSITORY.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")


def _sha256(value: str, name: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")


def _commit(value: str, name: str) -> None:
    if not isinstance(value, str) or _COMMIT.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")


def _summary_text(value: str, name: str) -> None:
    if not isinstance(value, str) or _SUMMARY_TEXT.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")


@dataclass(frozen=True)
class WorkspaceVerifyRequest:
    repository_root: Path
    issue_number: int
    candidate_id: str
    workspace_pull_request_number: int | None = None
    head_sha: str | None = None

    def __post_init__(self) -> None:
        _positive_integer(self.issue_number, "workspace issue number")
        _identifier(self.candidate_id, "workspace candidate ID")
        if self.workspace_pull_request_number is not None:
            _positive_integer(
                self.workspace_pull_request_number,
                "workspace pull request number",
            )
        if self.head_sha is not None:
            _commit(self.head_sha, "workspace head commit")


@dataclass(frozen=True)
class WorkspaceVerifiedIssue:
    repository: str
    target: str
    metrics: Mapping[str, MetricPolicy]

    def __post_init__(self) -> None:
        _repository(self.repository, "workspace repository")
        _identifier(self.target, "workspace target")
        policies = dict(self.metrics)
        if not policies:
            raise ValueError("workspace metric policies are invalid")
        for name, policy in policies.items():
            _identifier(name, "workspace metric name")
            if not isinstance(policy, MetricPolicy):
                raise ValueError("workspace metric policy is invalid")
        object.__setattr__(self, "metrics", MappingProxyType(policies))


@dataclass(frozen=True)
class WorkspaceEvidenceLink:
    path: str
    url: str
    state_revision: str
    sha256: str

    def __post_init__(self) -> None:
        if self.path != "evidence/candidates.json":
            raise ValueError("workspace evidence path is invalid")
        if (
            not isinstance(self.url, str)
            or not self.url.startswith("https://github.com/")
            or self.path not in self.url
        ):
            raise ValueError("workspace evidence URL is invalid")
        _commit(self.state_revision, "workspace state revision")
        _sha256(self.sha256, "workspace evidence digest")

    def to_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "state_revision": self.state_revision,
            "url": self.url,
        }


@dataclass(frozen=True)
class WorkspaceMetricVerification:
    name: str
    value: float | None
    threshold: float
    materiality: float
    hard_guardrail: bool
    guardrail_status: str | None

    def __post_init__(self) -> None:
        _identifier(self.name, "workspace metric name")
        if self.guardrail_status is not None and self.guardrail_status not in {
            "pass",
            "fail",
            "undefined",
        }:
            raise ValueError("workspace guardrail status is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "guardrail_status": self.guardrail_status,
            "hard_guardrail": self.hard_guardrail,
            "materiality": self.materiality,
            "name": self.name,
            "threshold": self.threshold,
            "value": self.value,
        }


class WorkspaceVerifyStatus(StrEnum):
    VERIFIED = "verified"


@dataclass(frozen=True)
class WorkspaceVerifyResult:
    issue_number: int
    candidate_id: str
    status: WorkspaceVerifyStatus
    repository: str
    target: str
    phase: WorkspacePhase
    workspace_pull_request_number: int
    head_sha: str | None
    head_tree: str
    expected_tree: str
    patch_sha256: str
    bundle_sha256: str
    evidence: WorkspaceEvidenceLink
    metric_table: tuple[WorkspaceMetricVerification, ...]
    guardrails: Mapping[str, str]
    summary_markdown: str

    def __post_init__(self) -> None:
        _positive_integer(self.issue_number, "workspace issue number")
        _identifier(self.candidate_id, "workspace candidate ID")
        if not isinstance(self.status, WorkspaceVerifyStatus):
            raise ValueError("workspace verify status is invalid")
        _repository(self.repository, "workspace repository")
        _identifier(self.target, "workspace target")
        if not isinstance(self.phase, WorkspacePhase):
            raise ValueError("workspace phase is invalid")
        _positive_integer(
            self.workspace_pull_request_number,
            "workspace pull request number",
        )
        if self.head_sha is not None:
            _commit(self.head_sha, "workspace head commit")
        _commit(self.head_tree, "workspace head tree")
        _commit(self.expected_tree, "workspace expected tree")
        _sha256(self.patch_sha256, "workspace patch digest")
        _sha256(self.bundle_sha256, "workspace bundle digest")
        if not isinstance(self.evidence, WorkspaceEvidenceLink):
            raise ValueError("workspace evidence link is invalid")
        rows = tuple(self.metric_table)
        if not rows or any(
            not isinstance(row, WorkspaceMetricVerification)
            for row in rows
        ):
            raise ValueError("workspace metric table is invalid")
        object.__setattr__(self, "metric_table", rows)
        guardrails = dict(self.guardrails)
        for name, status in guardrails.items():
            _identifier(name, "workspace guardrail name")
            if status not in {"pass", "fail", "undefined"}:
                raise ValueError("workspace guardrail status is invalid")
        object.__setattr__(self, "guardrails", MappingProxyType(guardrails))
        if not isinstance(self.summary_markdown, str) or not self.summary_markdown.strip():
            raise ValueError("workspace summary markdown is invalid")

    @property
    def exit_code(self) -> int:
        return 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_sha256": self.bundle_sha256,
            "candidate_id": self.candidate_id,
            "evidence": self.evidence.to_dict(),
            "expected_tree": self.expected_tree,
            "guardrails": dict(self.guardrails),
            "head_sha": self.head_sha,
            "head_tree": self.head_tree,
            "issue_number": self.issue_number,
            "metric_table": [
                row.to_dict() for row in self.metric_table
            ],
            "patch_sha256": self.patch_sha256,
            "phase": self.phase.value,
            "repository": self.repository,
            "status": self.status.value,
            "summary_markdown": self.summary_markdown,
            "target": self.target,
            "workspace_pull_request_number": (
                self.workspace_pull_request_number
            ),
        }


class WorkspaceSnapshotLoader(Protocol):
    def load(self, issue_number: int) -> WorkspaceSnapshot | None: ...


class WorkspaceVerifyIssueLoader(Protocol):
    def load(
        self,
        repository_root: Path,
        repository: str,
        issue_number: int,
    ) -> WorkspaceVerifiedIssue: ...


class WorkspaceRepositoryResolver(Protocol):
    def resolve(self, repository_root: Path) -> str: ...


class WorkspaceHeadTreeResolver(Protocol):
    def resolve(
        self,
        repository_root: Path,
        head_sha: str | None,
    ) -> str: ...


class ProductionWorkspaceRepositoryResolver:
    def __init__(self, commands: SubprocessCommandRunner) -> None:
        self._commands = commands

    def resolve(self, repository_root: Path) -> str:
        try:
            remote = self._commands.run(
                ("git", "remote", "get-url", "origin"),
                cwd=repository_root,
            ).stdout.strip()
        except CommandError as error:
            raise RuntimeError(
                "workspace repository origin is unavailable"
            ) from error
        repository = github_repository_from_remote_url(remote)
        if repository is None:
            raise ValueError("workspace repository origin is invalid")
        return repository


class ProductionWorkspaceVerifyIssueLoader:
    def __init__(self, commands: SubprocessCommandRunner) -> None:
        self._commands = commands

    def load(
        self,
        repository_root: Path,
        repository: str,
        issue_number: int,
    ) -> WorkspaceVerifiedIssue:
        try:
            payload = json.loads(
                self._commands.run(
                    (
                        "gh",
                        "issue",
                        "view",
                        str(issue_number),
                        "--repo",
                        repository,
                        "--json",
                        "number,title,body,state",
                    ),
                    cwd=repository_root,
                ).stdout
            )
        except (CommandError, json.JSONDecodeError) as error:
            raise RuntimeError("workspace issue is unavailable") from error
        title = payload.get("title")
        body = payload.get("body")
        if (
            payload.get("number") != issue_number
            or payload.get("state") != "OPEN"
            or not isinstance(title, str)
            or not title.startswith("[Optimize] ")
            or len(title) > 256
            or not isinstance(body, str)
            or len(body) > 262_144
        ):
            raise ValueError("workspace optimization issue is invalid")
        reject_secret_content(title)
        reject_secret_content(body)
        try:
            parsed = parse_optimization_issue_request(
                issue_number=issue_number,
                repository=repository,
                body=body,
            )
        except IssueSpecificationError as error:
            raise ValueError(
                "workspace optimization issue is invalid"
            ) from error
        return WorkspaceVerifiedIssue(
            repository=parsed.repository,
            target=parsed.target,
            metrics={
                name: MetricPolicy(
                    name=name,
                    direction=MetricDirection(policy.direction.value),
                    threshold=policy.threshold,
                    materiality=policy.materiality,
                    hard_guardrail=policy.hard_guardrail,
                    undefined_behavior=UndefinedBehavior(
                        policy.undefined_behavior.value
                    ),
                )
                for name, policy in parsed.metrics.items()
            },
        )


class ProductionWorkspaceHeadTreeResolver:
    def __init__(self, commands: SubprocessCommandRunner) -> None:
        self._commands = commands

    def resolve(
        self,
        repository_root: Path,
        head_sha: str | None,
    ) -> str:
        reference = head_sha or "HEAD"
        if head_sha is not None:
            _commit(head_sha, "workspace head commit")
        try:
            tree = self._commands.run(
                ("git", "rev-parse", "--verify", f"{reference}^{{tree}}"),
                cwd=repository_root,
            ).stdout.strip().lower()
        except CommandError as error:
            raise RuntimeError("workspace head tree is unavailable") from error
        _commit(tree, "workspace head tree")
        return tree


class WorkspaceVerificationService:
    def __init__(
        self,
        *,
        store: WorkspaceSnapshotLoader | None = None,
        issue_loader: WorkspaceVerifyIssueLoader | None = None,
        repository_resolver: WorkspaceRepositoryResolver | None = None,
        head_tree_resolver: WorkspaceHeadTreeResolver | None = None,
    ) -> None:
        commands = SubprocessCommandRunner()
        self._store = store
        self._issue_loader = (
            issue_loader
            if issue_loader is not None
            else ProductionWorkspaceVerifyIssueLoader(commands)
        )
        self._repository_resolver = (
            repository_resolver
            if repository_resolver is not None
            else ProductionWorkspaceRepositoryResolver(commands)
        )
        self._head_tree_resolver = (
            head_tree_resolver
            if head_tree_resolver is not None
            else ProductionWorkspaceHeadTreeResolver(commands)
        )

    def verify(
        self,
        request: WorkspaceVerifyRequest,
    ) -> WorkspaceVerifyResult:
        root = request.repository_root.expanduser().resolve()
        repository = self._repository_resolver.resolve(root)
        issue = self._issue_loader.load(
            root,
            repository,
            request.issue_number,
        )
        if issue.repository.casefold() != repository.casefold():
            raise ValueError("workspace issue repository changed")
        snapshot_store = self._store or GitWorkspaceStore(root)
        snapshot = snapshot_store.load(request.issue_number)
        if snapshot is None:
            raise ValueError("workspace state is unavailable")
        if snapshot.phase not in {
            WorkspacePhase.AWAITING_SELECTION,
            WorkspacePhase.DEPLOYMENT,
            WorkspacePhase.RETENTION,
            WorkspacePhase.COMPLETED,
        }:
            raise ValueError("workspace selected state is unavailable")
        if (
            request.workspace_pull_request_number is not None
            and snapshot.workspace_pull_request_number
            != request.workspace_pull_request_number
        ):
            raise ValueError("workspace pull request changed")
        if snapshot.workspace_pull_request_number is None:
            raise ValueError("workspace pull request is unavailable")
        selected = tuple(
            candidate
            for candidate in snapshot.candidates
            if candidate.selected
        )
        if len(selected) != 1:
            raise ValueError("workspace selected candidate is invalid")
        candidate = selected[0]
        if candidate.candidate_id != request.candidate_id:
            raise ValueError("workspace selected candidate changed")
        if set(candidate.metrics) - set(issue.metrics):
            raise ValueError("workspace selected metrics changed")
        patch_sha256 = _external_operation_digest(
            snapshot,
            request.candidate_id,
            "patch",
            _SHA256,
        )
        bundle_sha256 = _external_operation_digest(
            snapshot,
            request.candidate_id,
            "bundle",
            _SHA256,
        )
        evidence_sha256 = _external_operation_digest(
            snapshot,
            request.candidate_id,
            "evidence",
            _SHA256,
        )
        expected_tree = _external_operation_digest(
            snapshot,
            request.candidate_id,
            "tree",
            _COMMIT,
        )
        if snapshot.selected_patch is None:
            raise ValueError("workspace selected patch is unavailable")
        if hashlib.sha256(snapshot.selected_patch).hexdigest() != patch_sha256:
            raise ValueError("workspace selected patch changed")
        head_tree = self._head_tree_resolver.resolve(root, request.head_sha)
        if head_tree != expected_tree:
            raise ValueError("workspace exact head tree changed")
        metric_table = tuple(
            _metric_table_row(name, policy, candidate.metrics.get(name))
            for name, policy in sorted(issue.metrics.items())
        )
        guardrails = {
            row.name: row.guardrail_status
            for row in metric_table
            if row.hard_guardrail and row.guardrail_status is not None
        }
        evidence = WorkspaceEvidenceLink(
            path="evidence/candidates.json",
            url=(
                f"https://github.com/{repository}/blob/"
                f"{snapshot.revision}/evidence/candidates.json"
            ),
            state_revision=snapshot.revision,
            sha256=evidence_sha256,
        )
        summary_markdown = _summary_markdown(
            issue_number=request.issue_number,
            candidate_id=request.candidate_id,
            repository=repository,
            target=issue.target,
            workspace_pull_request_number=(
                snapshot.workspace_pull_request_number
            ),
            head_tree=head_tree,
            expected_tree=expected_tree,
            patch_sha256=patch_sha256,
            bundle_sha256=bundle_sha256,
            evidence=evidence,
            metric_table=metric_table,
            guardrails=guardrails,
        )
        return WorkspaceVerifyResult(
            issue_number=request.issue_number,
            candidate_id=request.candidate_id,
            status=WorkspaceVerifyStatus.VERIFIED,
            repository=repository,
            target=issue.target,
            phase=snapshot.phase,
            workspace_pull_request_number=(
                snapshot.workspace_pull_request_number
            ),
            head_sha=request.head_sha,
            head_tree=head_tree,
            expected_tree=expected_tree,
            patch_sha256=patch_sha256,
            bundle_sha256=bundle_sha256,
            evidence=evidence,
            metric_table=metric_table,
            guardrails=guardrails,
            summary_markdown=summary_markdown,
        )


def _external_operation_digest(
    snapshot: WorkspaceSnapshot,
    candidate_id: str,
    kind: str,
    pattern: re.Pattern[str],
) -> str:
    prefix = f"{candidate_id}:{kind}:"
    matches = [
        item[len(prefix):]
        for item in snapshot.external_operation_ids
        if item.startswith(prefix)
    ]
    if len(matches) != 1 or pattern.fullmatch(matches[0]) is None:
        raise ValueError(f"workspace {kind} lineage is invalid")
    return matches[0]


def _metric_table_row(
    name: str,
    policy: MetricPolicy,
    value: float | None,
) -> WorkspaceMetricVerification:
    guardrail_status = None
    if policy.hard_guardrail:
        if value is None:
            guardrail_status = "undefined"
        else:
            guardrail_status = "pass" if policy.passes(value) else "fail"
    return WorkspaceMetricVerification(
        name=name,
        value=value,
        threshold=policy.threshold,
        materiality=policy.materiality,
        hard_guardrail=policy.hard_guardrail,
        guardrail_status=guardrail_status,
    )


def _summary_markdown(
    *,
    issue_number: int,
    candidate_id: str,
    repository: str,
    target: str,
    workspace_pull_request_number: int,
    head_tree: str,
    expected_tree: str,
    patch_sha256: str,
    bundle_sha256: str,
    evidence: WorkspaceEvidenceLink,
    metric_table: tuple[WorkspaceMetricVerification, ...],
    guardrails: Mapping[str, str],
) -> str:
    lines = [
        "## Trusted workspace verification",
        "",
        f"- Repository: `{repository}`",
        f"- Issue: `#{issue_number}`",
        f"- Workspace pull request: `#{workspace_pull_request_number}`",
        f"- Target: `{target}`",
        f"- Candidate: `{candidate_id}`",
        f"- Exact head tree: `{head_tree}`",
        f"- Expected selected tree: `{expected_tree}`",
        f"- Patch SHA-256: `{patch_sha256}`",
        f"- Bundle SHA-256: `{bundle_sha256}`",
        (
            "Immutable evidence: "
            f"[`{evidence.path}`]({evidence.url})"
        ),
        f"- Evidence SHA-256: `{evidence.sha256}`",
        f"- State revision: `{evidence.state_revision}`",
        "",
        "## Metric table",
        "",
        _metric_table_markdown(metric_table),
        "",
        "## Guardrails",
        "",
        _guardrails_markdown(guardrails),
    ]
    summary = "\n".join(lines)
    if len(summary) > 65_536:
        raise ValueError("workspace summary markdown is too large")
    return summary


def _metric_table_markdown(
    metric_table: tuple[WorkspaceMetricVerification, ...],
) -> str:
    rows = [
        "Metric | Value | Threshold | Materiality | Guardrail",
        "--- | ---: | ---: | ---: | ---",
    ]
    for row in metric_table:
        rows.append(
            f"`{row.name}` | {_number(row.value)} | "
            f"{_number(row.threshold)} | {_number(row.materiality)} | "
            f"{row.guardrail_status or 'n/a'}"
        )
    return "\n".join(rows)


def _guardrails_markdown(guardrails: Mapping[str, str]) -> str:
    if not guardrails:
        return "No hard guardrails were configured."
    rows: list[str] = []
    for name, status in sorted(guardrails.items()):
        _summary_text(status, "workspace guardrail summary")
        rows.append(f"- Guardrail `{name}`: **{status}**")
    return "\n".join(rows)


def _number(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:g}"


def build_production_workspace_verification_service() -> (
    WorkspaceVerificationService
):
    return WorkspaceVerificationService()


__all__ = [
    "WorkspaceEvidenceLink",
    "WorkspaceMetricVerification",
    "WorkspaceVerificationService",
    "WorkspaceVerifiedIssue",
    "WorkspaceVerifyRequest",
    "WorkspaceVerifyResult",
    "WorkspaceVerifyStatus",
    "build_production_workspace_verification_service",
]
