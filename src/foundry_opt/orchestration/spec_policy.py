from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
import hashlib
import json
from pathlib import Path
import re
import subprocess
from types import MappingProxyType
from typing import Callable, Mapping, Protocol

import yaml

from foundry_opt.config.models import AutomationPolicy
from foundry_opt.optimization.models import (
    ApprovalGate,
    AssetKind,
    OptimizationSpec,
)
from foundry_opt.optimization.specification import PreparedSpecFile
from foundry_opt.orchestration.models import (
    AdvanceDisposition,
    CampaignEvent,
    CampaignPhase,
    CampaignState,
    EventKind,
)
from foundry_opt.preflight.interfaces import CommandRunner


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BRANCH = re.compile(
    r"^(?!-)(?!.*(?:\.\.|//|@\{|\\))[A-Za-z0-9._/@+-]+$"
)
_GENERATION_MARKER = re.compile(r"Generation: `([1-9][0-9]*)`")
_DIGEST_MARKER = re.compile(r"Spec SHA-256: `([0-9a-f]{64})`")


class SpecClassification(StrEnum):
    POLICY_APPROVED = "policy_approved"
    HUMAN_REVIEW = "human_review"


@dataclass(frozen=True)
class ResolvedSpecification:
    spec: OptimizationSpec
    asset_paths: Mapping[str, Path | None]
    new_asset_paths: tuple[Path, ...] = ()
    base_ref_name: str | None = None
    head_commit: str | None = None
    tree_sha: str | None = None
    prepared_files: tuple[PreparedSpecFile, ...] = ()

    def __post_init__(self) -> None:
        paths = dict(self.asset_paths)
        expected = {
            asset.asset_id
            for asset in (*self.spec.datasets, *self.spec.evaluators)
        }
        if set(paths) != expected:
            raise ValueError("asset paths must match specification assets")
        materialization = (
            self.base_ref_name,
            self.head_commit,
            self.tree_sha,
            self.prepared_files,
        )
        if any(materialization) and not all(materialization):
            raise ValueError("spec materialization metadata must be complete")
        object.__setattr__(self, "asset_paths", MappingProxyType(paths))


@dataclass(frozen=True)
class UnresolvedSpecification:
    reason: str


class SpecificationResolver(Protocol):
    def resolve(
        self,
        repository_root: Path,
        issue_number: int,
    ) -> ResolvedSpecification | UnresolvedSpecification: ...


class PinnedAssetReader(Protocol):
    def read(
        self,
        repository_root: Path,
        *,
        commit: str,
        path: Path,
    ) -> bytes | None: ...


@dataclass(frozen=True)
class MergedSpecApproval:
    generation: int
    pull_request_number: int
    base_ref_name: str
    head_commit: str
    head_tree_sha: str
    head_files: tuple[PreparedSpecFile, ...]
    head_spec_sha256: str
    merge_commit: str
    merge_tree_sha: str
    merged_files: tuple[PreparedSpecFile, ...]
    merged_spec_sha256: str
    remote_default_tip: str
    merge_reachable_from_default: bool

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValueError("generation must be positive")
        if self.pull_request_number < 1:
            raise ValueError("pull request number must be positive")
        if not self.base_ref_name:
            raise ValueError("base ref name is required")
        for value in (
            self.head_commit,
            self.head_tree_sha,
            self.merge_commit,
            self.merge_tree_sha,
            self.remote_default_tip,
        ):
            if not _COMMIT.fullmatch(value):
                raise ValueError("approval commits must be full Git commits")
        for value in (
            self.head_spec_sha256,
            self.merged_spec_sha256,
        ):
            if not _SHA256.fullmatch(value):
                raise ValueError("approval digests must be SHA-256 values")
        if not self.head_files or not self.merged_files:
            raise ValueError("approval files must not be empty")
        if type(self.merge_reachable_from_default) is not bool:
            raise ValueError("merge reachability must be boolean")


class SpecApprovalReader(Protocol):
    def merged_approval(
        self,
        repository_root: Path,
        issue_number: int,
        *,
        expected: CampaignState,
    ) -> MergedSpecApproval | None: ...


@dataclass(frozen=True)
class SpecPolicyRequest:
    repository_root: Path
    issue_number: int
    state: CampaignState

    def __post_init__(self) -> None:
        if self.issue_number != self.state.issue_number:
            raise ValueError("state issue does not match policy request")


@dataclass(frozen=True)
class SpecPolicyIntent:
    intent_id: str
    kind: str
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "payload",
            MappingProxyType(dict(self.payload)),
        )


@dataclass(frozen=True)
class SpecPolicyDecision:
    classification: SpecClassification
    reason: str
    spec_sha256: str | None = None
    event: CampaignEvent | None = None
    intents: tuple[SpecPolicyIntent, ...] = ()
    disposition: AdvanceDisposition = AdvanceDisposition.WAIT

    @property
    def dashboard_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "spec_classification": self.classification.value,
            "reason": self.reason,
        }
        if self.spec_sha256 is not None:
            payload["spec_sha256"] = self.spec_sha256
        return payload


class OptimizationSpecPolicy:
    """Classify immutable specifications at the steward boundary."""

    def __init__(
        self,
        policy: AutomationPolicy,
        *,
        resolver: SpecificationResolver,
        pinned_assets: PinnedAssetReader,
        approvals: SpecApprovalReader,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._policy = policy
        self._resolver = resolver
        self._pinned_assets = pinned_assets
        self._approvals = approvals
        self._clock = clock or (lambda: datetime.now(UTC))

    def evaluate(self, request: SpecPolicyRequest) -> SpecPolicyDecision | None:
        if request.state.phase is CampaignPhase.SPECIFICATION:
            return self._classify(request)
        if request.state.phase is CampaignPhase.AWAITING_SPEC_APPROVAL:
            return self._approve(request)
        return None

    def _classify(self, request: SpecPolicyRequest) -> SpecPolicyDecision:
        resolved = self._resolver.resolve(
            request.repository_root,
            request.issue_number,
        )
        if isinstance(resolved, UnresolvedSpecification):
            return self._human_decision(
                request,
                reason=resolved.reason,
                digest=None,
                event=None,
            )

        digest = resolved.spec.sha256
        reason = self._human_reason(request.repository_root, resolved)
        if reason is None:
            event = CampaignEvent(
                event_id=(
                    f"spec-policy-{request.state.generation}-"
                    f"{digest[:16]}"
                ),
                kind=EventKind.SPEC_POLICY_APPROVED,
                generation=request.state.generation,
                occurred_at=self._clock(),
                payload={"spec_sha256": digest},
            )
            return SpecPolicyDecision(
                classification=SpecClassification.POLICY_APPROVED,
                reason="existing_immutable_assets",
                spec_sha256=digest,
                event=event,
                disposition=AdvanceDisposition.ADVANCE,
            )
        if (
            resolved.base_ref_name is None
            or resolved.head_commit is None
            or resolved.tree_sha is None
            or not resolved.prepared_files
        ):
            raise ValueError("human review requires pinned materialization")
        event = CampaignEvent(
            event_id=(
                f"spec-review-{request.state.generation}-"
                f"{digest[:16]}"
            ),
            kind=EventKind.SPEC_REVIEW_REQUIRED,
            generation=request.state.generation,
            occurred_at=self._clock(),
            payload={
                "base_ref_name": resolved.base_ref_name,
                "files": [
                    {
                        "path": item.path.as_posix(),
                        "sha256": item.sha256,
                    }
                    for item in resolved.prepared_files
                ],
                "head_commit": resolved.head_commit,
                "spec_sha256": digest,
                "tree_sha": resolved.tree_sha,
            },
        )
        return self._human_decision(
            request,
            reason=reason,
            digest=digest,
            event=event,
        )

    def _human_reason(
        self,
        repository_root: Path,
        resolved: ResolvedSpecification,
    ) -> str | None:
        if resolved.new_asset_paths:
            return "new_asset_bytes"
        if not self._policy.allow_spec_auto_approval:
            return "policy_auto_approval_disabled"
        for asset in (*resolved.spec.datasets, *resolved.spec.evaluators):
            allowed = (
                self._policy.allowed_dataset_sources
                if asset.kind is AssetKind.DATASET
                else self._policy.allowed_evaluator_sources
            )
            if asset.source not in allowed:
                return "source_not_automated"
            if asset.approval_gate is not ApprovalGate.POLICY:
                return "asset_requires_human"
            if asset.source == "repository":
                path = resolved.asset_paths[asset.asset_id]
                if path is None or asset.content_sha256 is None:
                    return "repository_asset_unpinned"
                content = self._pinned_assets.read(
                    repository_root,
                    commit=resolved.spec.base_commit,
                    path=path,
                )
                if content is None:
                    return "repository_asset_missing"
                if (
                    hashlib.sha256(content).hexdigest()
                    != asset.content_sha256
                ):
                    return "repository_content_changed"
                continue
            if asset.source in {"foundry", "builtin"}:
                if (
                    not asset.name
                    or not asset.version
                    or not asset.remote_id
                    or resolved.asset_paths[asset.asset_id] is not None
                    or asset.content_sha256 is not None
                ):
                    return "immutable_asset_unpinned"
                continue
            if asset.source == "custom":
                return "custom_asset"
            if asset.source == "synthetic":
                return "synthetic_asset"
            if asset.source == "trace":
                return "trace_asset"
            return "unsupported_asset_source"
        return None

    def _human_decision(
        self,
        request: SpecPolicyRequest,
        *,
        reason: str,
        digest: str | None,
        event: CampaignEvent | None,
        intent_identity: str | None = None,
    ) -> SpecPolicyDecision:
        identity = intent_identity or (
            digest[:16] if digest is not None else reason
        )
        payload: dict[str, object] = {
            "issue_number": request.issue_number,
            "reason": reason,
            "spec_classification": SpecClassification.HUMAN_REVIEW.value,
            "specialist": "foundry-optimization-planner",
            "work_kind": "prepare_specification_pr",
        }
        if digest is not None:
            payload["spec_sha256"] = digest
        if event is not None:
            payload.update(
                {
                    key: value
                    for key, value in event.payload.items()
                    if key != "spec_sha256"
                }
            )
        return SpecPolicyDecision(
            classification=SpecClassification.HUMAN_REVIEW,
            reason=reason,
            spec_sha256=digest,
            event=event,
            intents=(
                SpecPolicyIntent(
                    intent_id=(
                        f"spec-planner-{request.state.generation}-{identity}"
                    ),
                    kind="specialist_work_request",
                    payload=payload,
                ),
            ),
            disposition=AdvanceDisposition.DELEGATE,
        )

    def _approve(self, request: SpecPolicyRequest) -> SpecPolicyDecision:
        expected = request.state.spec_sha256
        if expected is None:
            raise ValueError("awaiting approval requires a spec digest")
        if (
            request.state.spec_base_ref_name is None
            or request.state.spec_head_commit is None
            or request.state.spec_tree_sha is None
            or not request.state.spec_files
        ):
            return self._rematerialize_legacy_review(request, expected)
        approval = self._approvals.merged_approval(
            request.repository_root,
            request.issue_number,
            expected=request.state,
        )
        if approval is None:
            return SpecPolicyDecision(
                SpecClassification.HUMAN_REVIEW,
                "awaiting_spec_pull_request_merge",
                spec_sha256=expected,
                disposition=AdvanceDisposition.WAIT,
            )
        reason: str | None = None
        expected_files = tuple(
            PreparedSpecFile(Path(item.path), item.sha256)
            for item in request.state.spec_files
        )
        if (
            request.state.spec_base_ref_name is None
            or request.state.spec_head_commit is None
            or request.state.spec_tree_sha is None
            or not expected_files
        ):
            reason = "approval_materialization_missing"
        elif approval.generation != request.state.generation:
            reason = "approval_generation_mismatch"
        elif approval.head_commit != request.state.spec_head_commit:
            reason = "approval_head_mismatch"
        elif approval.head_tree_sha != request.state.spec_tree_sha:
            reason = "approval_tree_mismatch"
        elif (
            approval.head_files != expected_files
            or approval.merged_files != expected_files
        ):
            reason = "approval_files_mismatch"
        elif (
            approval.head_spec_sha256 != expected
            or approval.merged_spec_sha256 != expected
        ):
            reason = "approval_digest_mismatch"
        elif not approval.merge_reachable_from_default:
            reason = "approval_merge_not_on_default"
        if reason is not None:
            identity = hashlib.sha256(
                (
                    f"{approval.generation}:{approval.pull_request_number}:"
                    f"{approval.head_commit}:{approval.merge_commit}"
                ).encode("ascii")
            ).hexdigest()[:16]
            return SpecPolicyDecision(
                SpecClassification.HUMAN_REVIEW,
                reason,
                spec_sha256=expected,
                intents=(
                    SpecPolicyIntent(
                        intent_id=(
                            f"spec-rejected-{request.state.generation}-"
                            f"{identity}"
                        ),
                        kind="spec_approval_rejected",
                        payload={
                            "issue_number": request.issue_number,
                            "pull_request_number": (
                                approval.pull_request_number
                            ),
                            "reason": reason,
                            "spec_classification": (
                                SpecClassification.HUMAN_REVIEW.value
                            ),
                            "spec_sha256": expected,
                        },
                    ),
                ),
            )
        event = CampaignEvent(
            event_id=(
                f"spec-human-{request.state.generation}-"
                f"{approval.merge_commit[:16]}"
            ),
            kind=EventKind.SPEC_HUMAN_APPROVED,
            generation=request.state.generation,
            occurred_at=self._clock(),
            payload={
                "head_commit": approval.head_commit,
                "merge_commit": approval.merge_commit,
                "pull_request_number": approval.pull_request_number,
                "spec_sha256": expected,
            },
        )
        return SpecPolicyDecision(
            SpecClassification.HUMAN_REVIEW,
            "verified_spec_pull_request",
            spec_sha256=expected,
            event=event,
            disposition=AdvanceDisposition.ADVANCE,
        )

    def _rematerialize_legacy_review(
        self,
        request: SpecPolicyRequest,
        expected: str,
    ) -> SpecPolicyDecision:
        resolved = self._resolver.resolve(
            request.repository_root,
            request.issue_number,
        )
        if isinstance(resolved, UnresolvedSpecification):
            return self._human_decision(
                request,
                reason="legacy_spec_rematerialization_unavailable",
                digest=expected,
                event=None,
                intent_identity=f"legacy-unresolved-{expected[:16]}",
            )
        if (
            resolved.base_ref_name is None
            or resolved.head_commit is None
            or resolved.tree_sha is None
            or not resolved.prepared_files
        ):
            return self._human_decision(
                request,
                reason="legacy_spec_materialization_incomplete",
                digest=expected,
                event=None,
                intent_identity=f"legacy-incomplete-{expected[:16]}",
            )
        digest = resolved.spec.sha256
        reason = (
            "legacy_spec_rematerialized"
            if digest == expected
            else "legacy_spec_rebased"
        )
        event = CampaignEvent(
            event_id=(
                f"spec-rematerialized-{request.state.generation}-"
                f"{digest[:16]}"
            ),
            kind=EventKind.SPEC_REVIEW_REQUIRED,
            generation=request.state.generation,
            occurred_at=self._clock(),
            payload={
                "base_ref_name": resolved.base_ref_name,
                "files": [
                    {
                        "path": item.path.as_posix(),
                        "sha256": item.sha256,
                    }
                    for item in resolved.prepared_files
                ],
                "head_commit": resolved.head_commit,
                "spec_sha256": digest,
                "tree_sha": resolved.tree_sha,
            },
        )
        return self._human_decision(
            request,
            reason=reason,
            digest=digest,
            event=event,
            intent_identity=f"legacy-{digest[:16]}",
        )


class OptimizationSpecServiceResolver:
    """Adapt the existing spec service's no-publish resolution path."""

    def __init__(self, service: object) -> None:
        self._service = service

    def resolve(
        self,
        repository_root: Path,
        issue_number: int,
    ) -> ResolvedSpecification | UnresolvedSpecification:
        result = self._service.prepare_specification(
            repository_root,
            issue_number,
            publish=False,
        )
        if result.spec is None:
            reason = (
                "trace_asset"
                if any("trace" in item.casefold() for item in result.blockers)
                else "specification_unresolved"
            )
            return UnresolvedSpecification(reason)
        return ResolvedSpecification(
            spec=result.spec,
            asset_paths=result.asset_paths,
            new_asset_paths=result.new_asset_paths,
            base_ref_name=result.base_ref_name,
            head_commit=result.head_commit,
            tree_sha=result.tree_sha,
            prepared_files=result.prepared_files,
        )


class GitPinnedAssetReader:
    def __init__(self, commands: CommandRunner) -> None:
        self._commands = commands

    def read(
        self,
        repository_root: Path,
        *,
        commit: str,
        path: Path,
    ) -> bytes | None:
        result = subprocess.run(
            ("git", "show", f"{commit}:{path.as_posix()}"),
            cwd=repository_root,
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            return None
        return result.stdout


class GhMergedSpecApprovalReader:
    """Verify the exact merged specification PR head and merge content."""

    def __init__(self, commands: CommandRunner) -> None:
        self._commands = commands

    def merged_approval(
        self,
        repository_root: Path,
        issue_number: int,
        *,
        expected: CampaignState,
    ) -> MergedSpecApproval | None:
        from foundry_opt.optimization.specification import (
            spec_branch_name,
            spec_file_path,
            spec_issue_marker,
        )

        try:
            expected_branch = spec_branch_name(
                issue_number,
                expected.spec_sha256 or "",
                generation=expected.generation,
            )
            repository = json.loads(
                self._commands.run(
                    (
                        "gh",
                        "repo",
                        "view",
                        "--json",
                        "defaultBranchRef",
                    ),
                    cwd=repository_root,
                ).stdout
            )
            default_ref = repository.get("defaultBranchRef")
            default_branch = (
                default_ref.get("name")
                if isinstance(default_ref, dict)
                else None
            )
            if (
                not isinstance(default_branch, str)
                or not _BRANCH.fullmatch(default_branch)
            ):
                return None
            raw = self._commands.run(
                (
                    "gh",
                    "pr",
                    "list",
                    "--state",
                    "merged",
                    "--head",
                    expected_branch,
                    "--limit",
                    "100",
                    "--json",
                    (
                        "number,body,headRefName,headRefOid,"
                        "baseRefName,mergeCommit"
                    ),
                ),
                cwd=repository_root,
            ).stdout
            pull_requests = json.loads(raw)
        except Exception:
            return None
        if not isinstance(pull_requests, list):
            return None

        marker = spec_issue_marker(issue_number)
        for item in pull_requests:
            if not isinstance(item, dict):
                continue
            body = item.get("body")
            generation_match = (
                _GENERATION_MARKER.search(body)
                if isinstance(body, str)
                else None
            )
            digest_match = (
                _DIGEST_MARKER.search(body)
                if isinstance(body, str)
                else None
            )
            if (
                not isinstance(body, str)
                or marker not in body
                or generation_match is None
                or digest_match is None
            ):
                continue
            generation = int(generation_match.group(1))
            digest = digest_match.group(1)
            number = item.get("number")
            head_commit = item.get("headRefOid")
            merge = item.get("mergeCommit")
            merge_commit = (
                merge.get("oid") if isinstance(merge, dict) else None
            )
            if (
                type(number) is not int
                or number < 1
                or generation != expected.generation
                or digest != expected.spec_sha256
                or not isinstance(head_commit, str)
                or not isinstance(merge_commit, str)
                or not _COMMIT.fullmatch(head_commit)
                or not _COMMIT.fullmatch(merge_commit)
                or item.get("baseRefName") != default_branch
                or head_commit != expected.spec_head_commit
                or item.get("headRefName") != expected_branch
            ):
                continue
            try:
                self._commands.run(
                    (
                        "git",
                        "fetch",
                        "--quiet",
                        "origin",
                        f"pull/{number}/head",
                    ),
                    cwd=repository_root,
                )
                fetched = self._commands.run(
                    ("git", "rev-parse", "FETCH_HEAD^{commit}"),
                    cwd=repository_root,
                ).stdout.strip()
                if fetched != head_commit:
                    continue
                self._commands.run(
                    (
                        "git",
                        "fetch",
                        "--quiet",
                        "origin",
                        f"refs/heads/{default_branch}",
                    ),
                    cwd=repository_root,
                )
                remote_default_tip = self._commands.run(
                    ("git", "rev-parse", "FETCH_HEAD^{commit}"),
                    cwd=repository_root,
                ).stdout.strip()
                head_tree = self._commands.run(
                    ("git", "rev-parse", f"{head_commit}^{{tree}}"),
                    cwd=repository_root,
                ).stdout.strip()
                merge_tree = self._commands.run(
                    ("git", "rev-parse", f"{merge_commit}^{{tree}}"),
                    cwd=repository_root,
                ).stdout.strip()
                expected_files = tuple(
                    PreparedSpecFile(Path(value.path), value.sha256)
                    for value in expected.spec_files
                )
                head_files = self._read_files(
                    repository_root,
                    head_commit,
                    expected_files,
                )
                merged_files = self._read_files(
                    repository_root,
                    merge_commit,
                    expected_files,
                )
                path = spec_file_path(issue_number).as_posix()
                head_spec = self._read_spec(
                    repository_root,
                    f"{head_commit}:{path}",
                )
                merged_spec = self._read_spec(
                    repository_root,
                    f"{merge_commit}:{path}",
                )
            except Exception:
                continue
            if (
                not _COMMIT.fullmatch(remote_default_tip)
                or not _COMMIT.fullmatch(head_tree)
                or not _COMMIT.fullmatch(merge_tree)
                or not head_files
                or not merged_files
                or head_spec is None
                or merged_spec is None
                or head_spec.sha256 != digest
                or merged_spec.sha256 != digest
            ):
                continue
            try:
                self._commands.run(
                    (
                        "git",
                        "merge-base",
                        "--is-ancestor",
                        merge_commit,
                        remote_default_tip,
                    ),
                    cwd=repository_root,
                )
                reachable = True
            except Exception:
                reachable = False
            return MergedSpecApproval(
                generation=generation,
                pull_request_number=number,
                base_ref_name=default_branch,
                head_commit=head_commit,
                head_tree_sha=head_tree,
                head_files=head_files,
                head_spec_sha256=head_spec.sha256,
                merge_commit=merge_commit,
                merge_tree_sha=merge_tree,
                merged_files=merged_files,
                merged_spec_sha256=merged_spec.sha256,
                remote_default_tip=remote_default_tip,
                merge_reachable_from_default=reachable,
            )
        return None

    def _read_files(
        self,
        repository_root: Path,
        commit: str,
        expected: tuple[PreparedSpecFile, ...],
    ) -> tuple[PreparedSpecFile, ...]:
        files: list[PreparedSpecFile] = []
        for item in expected:
            result = subprocess.run(
                (
                    "git",
                    "cat-file",
                    "blob",
                    f"{commit}:{item.path.as_posix()}",
                ),
                cwd=repository_root,
                check=False,
                capture_output=True,
            )
            if result.returncode != 0:
                return ()
            files.append(
                PreparedSpecFile(
                    item.path,
                    hashlib.sha256(result.stdout).hexdigest(),
                )
            )
        return tuple(files)

    def _read_spec(
        self,
        repository_root: Path,
        object_ref: str,
    ) -> OptimizationSpec | None:
        try:
            content = self._commands.run(
                ("git", "show", object_ref),
                cwd=repository_root,
            ).stdout
            return OptimizationSpec.model_validate(yaml.safe_load(content))
        except Exception:
            return None


class RepositorySpecPolicy:
    """Load the repository-specific policy only when the steward needs it."""

    def __init__(
        self,
        factory: Callable[[Path], OptimizationSpecPolicy],
    ) -> None:
        self._factory = factory
        self._policies: dict[Path, OptimizationSpecPolicy] = {}

    def evaluate(self, request: SpecPolicyRequest) -> SpecPolicyDecision | None:
        root = request.repository_root.resolve()
        policy = self._policies.get(root)
        if policy is None:
            policy = self._factory(root)
            self._policies[root] = policy
        return policy.evaluate(request)
