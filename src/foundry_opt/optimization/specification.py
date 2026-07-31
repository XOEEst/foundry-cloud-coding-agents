"""Prepares and publishes an immutable optimization specification.

This module turns a triaged, labeled GitHub issue into an immutable
:class:`~foundry_opt.optimization.models.OptimizationSpec`, prepares its
evaluation assets without registering them with Foundry, and opens (or
idempotently reuses) a draft specification pull request. It never merges,
approves, deploys, or reads raw trace rows. Trace requests materialize only
privacy-safe provenance metadata into the immutable human-review spec.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol

import yaml

from foundry_opt.config.models import OptimizerConfig
from foundry_opt.github_workflow.errors import GitHubPermissionDeniedError
from foundry_opt.github_workflow.models import (
    GitHubCapabilities,
    GitHubPermissionReport,
    IssueReference,
    PullRequestReference,
    RepositoryState,
)
from foundry_opt.optimization.assets import (
    EvaluationAssetError,
    EvaluationAssetProviderRegistry,
    HumanReviewRequired,
)
from foundry_opt.optimization.issues import (
    IssueSpecificationError,
    parse_optimization_issue_request,
)
from foundry_opt.optimization.models import (
    AssetProvenance,
    EvaluationAssetContext,
    OptimizationIssueRequest,
    OptimizationSpec,
    PreparedEvaluationAsset,
)


_SPEC_SHA_PREFIX_LENGTH = 12
_SPEC_DIRECTORY = ".foundry-optimizer/specs"
_ISSUE_URL = re.compile(
    r"^https://github\.com/(?P<repository>[^/]+/[^/]+)/issues/\d+$"
)
_CANONICAL_LABELS = frozenset({"ready-for-agent", "needs-triage"})
# Creating (or reusing) a draft spec pull request only means the spec is
# ready for *human* review; it moves the issue to `ready-for-human`, never
# directly to `ready-for-agent`. Only a future spec-merge event (out of
# scope here) should mark the parent issue ready for runner/agent
# assignment.
_READY_FOR_HUMAN_LABEL = "ready-for-human"
_TRIAGE_LABEL = "needs-triage"


class OptimizationSpecServiceError(RuntimeError):
    """Raised for defects that are never a normal, reportable business state."""


class SpecBranchConflictError(RuntimeError):
    """Raised by a :class:`SpecPublisher` when a branch is owned by another commit."""

    def __init__(self, branch: str, remote_commit: str) -> None:
        self.branch = branch
        self.remote_commit = remote_commit
        super().__init__(f"branch {branch!r} is already at {remote_commit!r}")


class SpecServiceStatus(StrEnum):
    COMPLETE = "complete"
    BLOCKED = "blocked"
    CONFLICT = "conflict"
    PARTIAL = "partial"


@dataclass(frozen=True)
class SpecServiceFailure:
    operation: str
    code: str
    message: str


@dataclass(frozen=True)
class PreparedSpecFile:
    path: Path
    sha256: str


class PreparedSpecCommit(str):
    tree_sha: str

    def __new__(
        cls,
        head_commit: str,
        tree_sha: str,
    ) -> PreparedSpecCommit:
        value = str.__new__(cls, head_commit)
        value.tree_sha = tree_sha
        return value

    @property
    def head_commit(self) -> str:
        return str(self)


@dataclass(frozen=True)
class SpecServiceResult:
    status: SpecServiceStatus
    issue_number: int
    spec: OptimizationSpec | None = None
    spec_sha256: str | None = None
    branch: str | None = None
    pull_request: PullRequestReference | None = None
    prepared_files: tuple[PreparedSpecFile, ...] = ()
    blockers: tuple[str, ...] = ()
    issue_updated: bool = False
    failures: tuple[SpecServiceFailure, ...] = ()
    asset_paths: Mapping[str, Path | None] = field(default_factory=dict)
    new_asset_paths: tuple[Path, ...] = ()
    base_ref_name: str | None = None
    head_commit: str | None = None
    tree_sha: str | None = None

    def __post_init__(self) -> None:
        if self.issue_number < 1:
            raise ValueError("issue_number must be positive")
        object.__setattr__(
            self,
            "asset_paths",
            MappingProxyType(dict(self.asset_paths)),
        )


class OptimizationSpecGateway(Protocol):
    def verify_permissions(
        self,
        required: GitHubCapabilities,
    ) -> GitHubPermissionReport: ...

    def repository_state(self, repository_root: Path) -> RepositoryState: ...

    def get_issue(
        self,
        repository_root: Path,
        issue_number: int,
    ) -> IssueReference | None: ...

    def find_spec_pull_request(
        self,
        repository_root: Path,
        issue_number: int,
    ) -> PullRequestReference | None: ...

    def comment_issue(
        self,
        repository_root: Path,
        issue_number: int,
        body: str,
    ) -> None: ...

    def has_issue_comment(
        self,
        repository_root: Path,
        issue_number: int,
        marker: str,
    ) -> bool: ...

    def add_labels(
        self,
        repository_root: Path,
        issue_number: int,
        labels: tuple[str, ...],
    ) -> None: ...

    def remove_labels(
        self,
        repository_root: Path,
        issue_number: int,
        labels: tuple[str, ...],
    ) -> None: ...


class SpecPublisher(Protocol):
    """The safe change-set/commit publisher seam.

    Implementations must build the commit through Git plumbing without
    touching the local checkout (no working tree writes, no index/HEAD
    mutation) and must never claim the resulting pull request is approved.
    """

    def prepare_commit(
        self,
        repository_root: Path,
        *,
        base_commit: str,
        files: Mapping[Path, bytes],
        message: str,
    ) -> PreparedSpecCommit: ...

    def publish(
        self,
        repository_root: Path,
        *,
        base_branch: str,
        branch: str,
        commit_sha: str,
        title: str,
        body: str,
    ) -> PullRequestReference: ...


def spec_issue_marker(issue_number: int) -> str:
    return f"<!-- foundry-opt:spec:issue-{issue_number} -->"


def spec_branch_name(
    issue_number: int,
    spec_sha256: str,
    *,
    generation: int | None = None,
) -> str:
    branch = (
        f"foundry-opt/spec/issue-{issue_number}/"
        f"{spec_sha256[:_SPEC_SHA_PREFIX_LENGTH]}"
    )
    if generation is not None:
        if generation < 1:
            raise ValueError("generation must be positive")
        branch += f"/generation-{generation}"
    return branch


def spec_directory(issue_number: int) -> Path:
    return Path(_SPEC_DIRECTORY) / f"issue-{issue_number}"


def spec_file_path(issue_number: int) -> Path:
    return spec_directory(issue_number) / "optimization-spec.yaml"


def provenance_file_path(issue_number: int) -> Path:
    return spec_directory(issue_number) / "provenance.json"


def _synthetic_asset_commit_path(issue_number: int, original_path: Path) -> Path:
    """Namespace a synthetic provider's output under the issue's own spec
    directory so it can never overwrite a tracked customer file (or another
    issue's synthetic output) at the provider's un-namespaced path."""
    return spec_directory(issue_number) / "assets" / original_path.name


def _review_asset_commit_path(
    issue_number: int,
    asset_id: str,
    original_path: Path,
) -> Path:
    return (
        spec_directory(issue_number)
        / "assets"
        / asset_id
        / original_path.name
    )


class OptimizationSpecService:
    def __init__(
        self,
        config: OptimizerConfig,
        *,
        registry: EvaluationAssetProviderRegistry,
        gateway: OptimizationSpecGateway,
        publisher: SpecPublisher,
        generation_provider: Callable[[Path, int], int | None] | None = None,
    ) -> None:
        self._config = config
        self._registry = registry
        self._gateway = gateway
        self._publisher = publisher
        self._generation_provider = generation_provider

    def prepare_specification(
        self,
        repository_root: Path,
        issue_number: int,
        *,
        publish: bool = True,
    ) -> SpecServiceResult:
        if issue_number < 1:
            raise ValueError("issue_number must be positive")

        required = GitHubCapabilities.METADATA_READ
        if publish:
            required |= (
                GitHubCapabilities.ISSUES_WRITE
                | GitHubCapabilities.CONTENTS_WRITE
                | GitHubCapabilities.PULL_REQUESTS_WRITE
            )
        permissions = self._gateway.verify_permissions(required)
        missing = required & ~permissions.granted
        if missing:
            raise GitHubPermissionDeniedError(missing)

        state = self._gateway.repository_state(repository_root)

        issue = self._gateway.get_issue(repository_root, issue_number)
        if issue is None:
            return self._blocked(
                repository_root,
                issue_number,
                "issue_not_found",
                "no open GitHub issue (and not a pull request) was found "
                f"for #{issue_number}",
            )

        issue_repository = _repository_from_issue_url(issue.url)
        if issue_repository is None or (
            issue_repository.casefold() != state.repository.casefold()
        ):
            return self._blocked(
                repository_root,
                issue_number,
                "repository_mismatch",
                "the issue does not belong to the verified repository",
            )
        if issue.state != "OPEN":
            return self._blocked(
                repository_root,
                issue_number,
                "issue_closed",
                "the issue must be open to prepare a specification",
            )
        if not (_CANONICAL_LABELS & set(issue.labels)):
            return self._blocked(
                repository_root,
                issue_number,
                "label_not_ready",
                "the issue must carry the 'ready-for-agent' or "
                "'needs-triage' label",
            )

        try:
            request: OptimizationIssueRequest = parse_optimization_issue_request(
                issue_number=issue_number,
                repository=state.repository,
                body=issue.body,
            )
        except IssueSpecificationError as error:
            return self._blocked(
                repository_root,
                issue_number,
                "invalid_specification",
                f"the issue specification is invalid: {error}",
            )

        target = self._config.targets.get(request.target)
        if target is None:
            return self._blocked(
                repository_root,
                issue_number,
                "unknown_target",
                f"target {request.target!r} is not configured",
            )

        if not request.allowed_mutations <= target.allowed_mutations:
            return self._blocked(
                repository_root,
                issue_number,
                "mutation_not_allowed",
                "requested mutations are not allowed by target "
                f"{request.target!r}",
            )
        if not request.allowed_mutations <= self._config.campaign.allowed_mutations:
            return self._blocked(
                repository_root,
                issue_number,
                "mutation_not_allowed",
                "requested mutations are not allowed by the campaign policy",
            )

        policy = self._config.automation_policy
        for dataset in request.datasets:
            if dataset.source not in policy.allowed_dataset_sources:
                return self._blocked(
                    repository_root,
                    issue_number,
                    "source_not_allowed",
                    f"dataset source {dataset.source!r} is not allowed",
                )
            if dataset.source == "synthetic":
                row_count = dataset.parameters.get("row_count")
                if (
                    not isinstance(row_count, int)
                    or row_count > policy.synthetic_max_rows
                ):
                    return self._blocked(
                        repository_root,
                        issue_number,
                        "synthetic_row_count_exceeded",
                        "synthetic dataset row_count exceeds the configured "
                        f"limit of {policy.synthetic_max_rows}",
                    )
        for evaluator in request.evaluators:
            if evaluator.source not in policy.allowed_evaluator_sources:
                return self._blocked(
                    repository_root,
                    issue_number,
                    "source_not_allowed",
                    f"evaluator source {evaluator.source!r} is not allowed",
                )

        environment = self._config.environments.get(target.environment)
        if environment is None:
            raise OptimizationSpecServiceError(
                f"target {request.target!r} references an unknown "
                "environment"
            )
        context = EvaluationAssetContext(
            repository_root=repository_root,
            project_endpoint=str(environment.project_endpoint),
            target=request.target,
            issue_number=issue_number,
        )

        prepared_datasets: list[PreparedEvaluationAsset] = []
        prepared_evaluators: list[PreparedEvaluationAsset] = []
        try:
            for dataset_request in request.datasets:
                prepared_datasets.append(
                    self._registry.prepare(dataset_request, context)
                )
            for evaluator_request in request.evaluators:
                prepared_evaluators.append(
                    self._registry.prepare(evaluator_request, context)
                )
        except HumanReviewRequired as error:
            return self._blocked(
                repository_root,
                issue_number,
                "human_review_required",
                str(error),
            )
        except (EvaluationAssetError, ValueError) as error:
            return self._blocked(
                repository_root,
                issue_number,
                "asset_preparation_failed",
                str(error),
            )

        spec = OptimizationSpec(
            issue_number=issue_number,
            repository=state.repository,
            base_commit=state.default_commit,
            target=request.target,
            environment=target.environment,
            base_agent_version=target.base_agent_version,
            goal=request.goal,
            datasets=tuple(
                prepared.provenance for prepared in prepared_datasets
            ),
            evaluators=tuple(
                prepared.provenance for prepared in prepared_evaluators
            ),
            metrics=request.metrics,
            allowed_mutations=request.allowed_mutations,
            restricted_opt_ins=request.restricted_opt_ins,
            decision_mode=request.decision_mode,
            deployment_mode=request.deployment_mode,
        )
        spec_sha256 = spec.sha256
        generation = (
            self._generation_provider(repository_root, issue_number)
            if publish and self._generation_provider is not None
            else None
        )
        branch = spec_branch_name(
            issue_number,
            spec_sha256,
            generation=generation,
        )

        prepared_asset_paths: dict[str, Path | None] = {}
        source_asset_paths: dict[str, Path | None] = {}
        committed_asset_files: dict[Path, bytes] = {}
        new_asset_paths: list[Path] = []
        for prepared in (*prepared_datasets, *prepared_evaluators):
            asset_id = prepared.provenance.asset_id
            if not prepared.files:
                prepared_asset_paths[asset_id] = None
                source_asset_paths[asset_id] = None
                continue
            if len(prepared.files) != 1:
                raise OptimizationSpecServiceError(
                    f"asset {asset_id!r} prepared more than one file; "
                    "expected exactly one"
                )
            ((original_path, content),) = prepared.files.items()
            source_asset_paths[asset_id] = original_path
            if prepared.provenance.source == "synthetic":
                # Newly generated content is namespaced under the issue's
                # own spec directory and committed there, so it can never
                # collide with a tracked customer file or another issue's
                # synthetic output at the provider's un-namespaced path.
                committed_path = _synthetic_asset_commit_path(
                    issue_number, original_path
                )
                if committed_path in committed_asset_files and (
                    committed_asset_files[committed_path] != content
                ):
                    raise OptimizationSpecServiceError(
                        "prepared asset path collision: "
                        f"{committed_path.as_posix()}"
                    )
                committed_asset_files[committed_path] = content
                prepared_asset_paths[asset_id] = committed_path
                new_asset_paths.append(committed_path)
            elif prepared.provenance.source in {"repository", "custom"}:
                committed_path = _review_asset_commit_path(
                    issue_number,
                    asset_id,
                    original_path,
                )
                if committed_path in committed_asset_files and (
                    committed_asset_files[committed_path] != content
                ):
                    raise OptimizationSpecServiceError(
                        "prepared asset path collision: "
                        f"{committed_path.as_posix()}"
                    )
                committed_asset_files[committed_path] = content
                prepared_asset_paths[asset_id] = committed_path
            else:
                prepared_asset_paths[asset_id] = original_path

        spec_yaml = _render_spec_yaml(spec)
        provenance_json = _render_provenance_json(
            spec, spec_sha256, asset_paths=prepared_asset_paths
        )
        spec_path = spec_file_path(issue_number)
        provenance_path = provenance_file_path(issue_number)
        for reserved_path in (spec_path, provenance_path):
            if reserved_path in committed_asset_files:
                raise OptimizationSpecServiceError(
                    "prepared asset path collides with a generated spec "
                    f"file: {reserved_path.as_posix()}"
                )
        commit_files: dict[Path, bytes] = dict(committed_asset_files)
        commit_files[spec_path] = spec_yaml
        commit_files[provenance_path] = provenance_json
        prepared_files = tuple(
            sorted(
                (
                    PreparedSpecFile(
                        path=path,
                        sha256=hashlib.sha256(content).hexdigest(),
                    )
                    for path, content in commit_files.items()
                ),
                key=lambda item: item.path.as_posix(),
            )
        )

        # The spec commit is always built (deterministically, via git
        # plumbing) before any pull-request matching so that idempotency can
        # be verified against the exact expected head commit, not only
        # branch/body markers.
        message = (
            f"foundry-opt: prepare optimization spec for issue "
            f"#{issue_number}\n\nSpec SHA-256: {spec_sha256}\n"
        )
        prepared_commit = self._publisher.prepare_commit(
            repository_root,
            base_commit=state.default_commit,
            files=commit_files,
            message=message,
        )
        commit_sha = prepared_commit.head_commit

        if not publish:
            return SpecServiceResult(
                status=SpecServiceStatus.COMPLETE,
                issue_number=issue_number,
                spec=spec,
                spec_sha256=spec_sha256,
                branch=branch,
                prepared_files=prepared_files,
                asset_paths=source_asset_paths,
                new_asset_paths=tuple(
                    sorted(new_asset_paths, key=lambda path: path.as_posix())
                ),
                base_ref_name=state.default_branch,
                head_commit=commit_sha,
                tree_sha=prepared_commit.tree_sha,
            )

        existing = self._gateway.find_spec_pull_request(
            repository_root, issue_number
        )
        if existing is not None:
            if _spec_pull_request_matches(
                existing,
                issue_number=issue_number,
                branch=branch,
                base_branch=state.default_branch,
                spec_sha256=spec_sha256,
                base_commit=state.default_commit,
                head_commit=commit_sha,
                tree_sha=prepared_commit.tree_sha,
                prepared_files=prepared_files,
            ):
                pull_request = existing
            else:
                return SpecServiceResult(
                    status=SpecServiceStatus.CONFLICT,
                    issue_number=issue_number,
                    spec=spec,
                    spec_sha256=spec_sha256,
                    branch=branch,
                    pull_request=existing,
                    prepared_files=prepared_files,
                    blockers=(
                        "an existing spec pull request does not match the "
                        "current issue specification and base commit; "
                        "resolve or close it before retrying",
                    ),
                )
        else:
            try:
                pull_request = self._publisher.publish(
                    repository_root,
                    base_branch=state.default_branch,
                    branch=branch,
                    commit_sha=commit_sha,
                    title=(
                        f"[foundry-opt] Optimization spec for issue "
                        f"#{issue_number} ({request.target})"
                    ),
                    body=_spec_pull_request_body(
                        spec,
                        issue_number=issue_number,
                        spec_sha256=spec_sha256,
                        generation=generation,
                        prepared_datasets=prepared_datasets,
                        prepared_evaluators=prepared_evaluators,
                        prepared_files=prepared_files,
                        head_commit=commit_sha,
                        tree_sha=prepared_commit.tree_sha,
                    ),
                )
            except SpecBranchConflictError as error:
                return SpecServiceResult(
                    status=SpecServiceStatus.CONFLICT,
                    issue_number=issue_number,
                    spec=spec,
                    spec_sha256=spec_sha256,
                    branch=branch,
                    prepared_files=prepared_files,
                    blockers=(
                        f"branch {error.branch!r} is already at "
                        f"{error.remote_commit!r}",
                    ),
                )

        failures: list[SpecServiceFailure] = []
        issue_updated = self._update_issue(
            repository_root,
            issue,
            issue_number=issue_number,
            pull_request=pull_request,
            failures=failures,
        )

        status = (
            SpecServiceStatus.COMPLETE
            if not failures
            else SpecServiceStatus.PARTIAL
        )
        return SpecServiceResult(
            status=status,
            issue_number=issue_number,
            spec=spec,
            spec_sha256=spec_sha256,
            branch=branch,
            pull_request=pull_request,
            prepared_files=prepared_files,
            base_ref_name=state.default_branch,
            head_commit=commit_sha,
            tree_sha=prepared_commit.tree_sha,
            issue_updated=issue_updated,
            failures=tuple(failures),
            asset_paths=source_asset_paths,
            new_asset_paths=tuple(
                sorted(new_asset_paths, key=lambda path: path.as_posix())
            ),
        )

    def _update_issue(
        self,
        repository_root: Path,
        issue: IssueReference,
        *,
        issue_number: int,
        pull_request: PullRequestReference,
        failures: list[SpecServiceFailure],
    ) -> bool:
        updated = False
        comment_marker = _spec_comment_marker(issue_number)
        try:
            already_commented = self._gateway.has_issue_comment(
                repository_root, issue_number, comment_marker
            )
            if not already_commented:
                self._gateway.comment_issue(
                    repository_root,
                    issue_number,
                    _spec_ready_comment_body(issue_number, pull_request),
                )
                updated = True
        except RuntimeError as error:
            failures.append(
                SpecServiceFailure(
                    "comment_issue",
                    "comment_failed",
                    f"posting the specification comment failed: {error}",
                )
            )

        labels = set(issue.labels)
        if _TRIAGE_LABEL in labels and _READY_FOR_HUMAN_LABEL not in labels:
            try:
                self._gateway.remove_labels(
                    repository_root, issue_number, (_TRIAGE_LABEL,)
                )
                self._gateway.add_labels(
                    repository_root, issue_number, (_READY_FOR_HUMAN_LABEL,)
                )
                updated = True
            except RuntimeError as error:
                failures.append(
                    SpecServiceFailure(
                        "update_labels",
                        "label_update_failed",
                        "moving the issue to "
                        f"'{_READY_FOR_HUMAN_LABEL}' failed: {error}",
                    )
                )
        return updated

    def _blocked(
        self,
        repository_root: Path,
        issue_number: int,
        code: str,
        message: str,
    ) -> SpecServiceResult:
        failures: list[SpecServiceFailure] = []
        try:
            marker = _spec_block_comment_marker(issue_number, code)
            if not self._gateway.has_issue_comment(
                repository_root, issue_number, marker
            ):
                self._gateway.comment_issue(
                    repository_root,
                    issue_number,
                    _blocked_comment_body(marker, code, message),
                )
        except RuntimeError as error:
            failures.append(
                SpecServiceFailure(
                    "comment_issue",
                    "comment_failed",
                    f"posting the blocked-state comment failed: {error}",
                )
            )
        return SpecServiceResult(
            status=SpecServiceStatus.BLOCKED,
            issue_number=issue_number,
            blockers=(message,),
            failures=tuple(failures),
        )


def _repository_from_issue_url(url: str) -> str | None:
    match = _ISSUE_URL.match(url)
    return match.group("repository") if match else None


def _spec_comment_marker(issue_number: int) -> str:
    return f"<!-- foundry-opt:spec-comment:issue-{issue_number} -->"


def _spec_block_comment_marker(issue_number: int, code: str) -> str:
    return f"<!-- foundry-opt:spec-blocked:issue-{issue_number}:{code} -->"


def _spec_pull_request_matches(
    pull_request: PullRequestReference,
    *,
    issue_number: int,
    branch: str,
    base_branch: str,
    spec_sha256: str,
    base_commit: str,
    head_commit: str,
    tree_sha: str,
    prepared_files: tuple[PreparedSpecFile, ...],
) -> bool:
    marker = spec_issue_marker(issue_number)
    return (
        pull_request.state == "OPEN"
        and pull_request.draft
        and pull_request.head_branch == branch
        and pull_request.head_commit == head_commit
        and pull_request.base_branch == base_branch
        and marker in pull_request.body
        and f"Spec SHA-256: `{spec_sha256}`" in pull_request.body
        and f"Base commit: `{base_commit}`" in pull_request.body
        and f"Expected head: `{head_commit}`" in pull_request.body
        and f"Expected tree: `{tree_sha}`" in pull_request.body
        and all(
            f"`{item.path.as_posix()}`: `{item.sha256}`"
            in pull_request.body
            for item in prepared_files
        )
    )


def _spec_pull_request_body(
    spec: OptimizationSpec,
    *,
    issue_number: int,
    spec_sha256: str,
    generation: int | None,
    prepared_datasets: list[PreparedEvaluationAsset],
    prepared_evaluators: list[PreparedEvaluationAsset],
    prepared_files: tuple[PreparedSpecFile, ...],
    head_commit: str,
    tree_sha: str,
) -> str:
    def _asset_lines(prepared: list[PreparedEvaluationAsset]) -> str:
        return "\n".join(
            f"- `{item.provenance.asset_id}` (source: `{item.provenance.source}`, "
            f"approval gate: `{item.provenance.approval_gate.value}`)"
            for item in prepared
        )

    lines = [
            spec_issue_marker(issue_number),
            f"Issue: #{issue_number}",
            f"Target: `{spec.target}`",
            f"Base commit: `{spec.base_commit}`",
            f"Spec SHA-256: `{spec_sha256}`",
            f"Expected head: `{head_commit}`",
            f"Expected tree: `{tree_sha}`",
    ]
    if generation is not None:
        lines.append(f"Generation: `{generation}`")
    lines.extend(
        (
            "",
            "## Datasets",
            _asset_lines(prepared_datasets),
            "",
            "## Evaluators",
            _asset_lines(prepared_evaluators),
            "",
            "## Immutable files",
            *(
                f"- `{item.path.as_posix()}`: `{item.sha256}`"
                for item in prepared_files
            ),
            "",
            "This pull request is not approved. Approval is recorded only "
            "when a maintainer merges it; policy-gated assets remain "
            "provisional and human-gated assets always require explicit "
            "human review before merge.",
        )
    )
    return "\n".join(lines) + "\n"


def _spec_ready_comment_body(
    issue_number: int,
    pull_request: PullRequestReference,
) -> str:
    return "\n".join(
        (
            _spec_comment_marker(issue_number),
            "A draft optimization specification pull request is ready for "
            f"review: #{pull_request.number}.",
            "This pull request is not approved; merging it records human "
            "approval of the pinned specification.",
        )
    ) + "\n"


def _blocked_comment_body(marker: str, code: str, message: str) -> str:
    return "\n".join(
        (
            marker,
            f"Automated specification preparation is blocked ({code}).",
            message,
        )
    ) + "\n"


def _render_spec_yaml(spec: OptimizationSpec) -> bytes:
    document = json.loads(spec.canonical_json)
    text = yaml.safe_dump(
        document,
        sort_keys=True,
        default_flow_style=False,
        allow_unicode=False,
    )
    return text.encode("utf-8")


def _render_provenance_json(
    spec: OptimizationSpec,
    spec_sha256: str,
    *,
    asset_paths: Mapping[str, Path | None],
) -> bytes:
    def _asset_entry(provenance: AssetProvenance) -> dict[str, Any]:
        path = asset_paths.get(provenance.asset_id)
        return {
            "approval_gate": provenance.approval_gate.value,
            "asset_id": provenance.asset_id,
            "content_sha256": provenance.content_sha256,
            "created_by": provenance.created_by,
            "kind": provenance.kind.value,
            "metrics": list(provenance.metrics),
            "name": provenance.name,
            # The materialized local path needed after merge: a synthetic
            # asset's namespaced committed path, a repository/custom
            # asset's existing tracked path, or None when the source (e.g.
            # foundry/builtin) has no associated local file.
            "path": path.as_posix() if path is not None else None,
            "remote_id": provenance.remote_id,
            "role": provenance.role,
            "source": provenance.source,
            "version": provenance.version,
        }

    document = {
        "base_commit": spec.base_commit,
        "datasets": [_asset_entry(item) for item in spec.datasets],
        "evaluators": [_asset_entry(item) for item in spec.evaluators],
        "generated_by": "foundry-opt-optimization-spec-service",
        "issue_number": spec.issue_number,
        "repository": spec.repository,
        "schema_version": 1,
        "spec_sha256": spec_sha256,
        "target": spec.target,
    }
    return (
        json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("utf-8")
