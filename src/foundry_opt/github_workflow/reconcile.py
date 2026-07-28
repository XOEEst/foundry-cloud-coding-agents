from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping, Protocol

from foundry_opt.config.models import AutomationPolicy
from foundry_opt.github_workflow.models import (
    GitHubCapabilities,
    GitHubPermissionReport,
    PullRequestReference,
)
from foundry_opt.optimization import (
    ApprovalGate,
    DecisionMode,
    OptimizationSpecApproval,
)


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class CandidateReconcileStatus(StrEnum):
    WAITING_FOR_HUMAN = "waiting_for_human"
    BLOCKED = "blocked"
    MERGED = "merged"
    DEPLOYMENT_DISPATCHED = "deployment_dispatched"


@dataclass(frozen=True)
class CandidateReconcileEntry:
    candidate_id: str
    pull_request: PullRequestReference
    eligible: bool
    checks: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.candidate_id):
            raise ValueError("candidate_id is invalid")
        if not isinstance(self.pull_request, PullRequestReference):
            raise ValueError("pull_request is invalid")
        normalized = dict(self.checks)
        if any(
            not name.strip()
            or conclusion not in {
                "success",
                "failure",
                "pending",
                "cancelled",
                "skipped",
            }
            for name, conclusion in normalized.items()
        ):
            raise ValueError("candidate checks are invalid")
        object.__setattr__(
            self,
            "checks",
            MappingProxyType(normalized),
        )


@dataclass(frozen=True)
class CandidateReconcileRequest:
    repository_root: Path
    approval: OptimizationSpecApproval
    automation_policy: AutomationPolicy
    ranked_candidates: tuple[CandidateReconcileEntry, ...]

    def __post_init__(self) -> None:
        if not self.ranked_candidates:
            raise ValueError("ranked_candidates must not be empty")
        candidate_ids = tuple(
            candidate.candidate_id
            for candidate in self.ranked_candidates
        )
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("ranked candidate IDs must be unique")


@dataclass(frozen=True)
class CandidateReconcileResult:
    status: CandidateReconcileStatus
    selected_candidate_id: str | None = None
    pull_request_number: int | None = None
    reason_code: str | None = None


class CandidateReconcileGateway(Protocol):
    def verify_permissions(
        self,
        required: GitHubCapabilities,
    ) -> GitHubPermissionReport: ...

    def branch_protection_allows(
        self,
        repository_root: Path,
        pull_request: PullRequestReference,
        actor: str,
    ) -> bool: ...

    def merge_pull_request(
        self,
        repository_root: Path,
        pull_request_number: int,
        expected_head_commit: str,
        actor: str,
    ) -> None: ...

    def dispatch_deployment(
        self,
        repository_root: Path,
        pull_request_number: int,
        expected_head_commit: str,
    ) -> None: ...


def reconcile_candidates(
    request: CandidateReconcileRequest,
    gateway: CandidateReconcileGateway,
) -> CandidateReconcileResult:
    spec = request.approval.spec
    policy = request.automation_policy
    if spec.decision_mode is DecisionMode.HUMAN:
        return CandidateReconcileResult(
            status=CandidateReconcileStatus.WAITING_FOR_HUMAN
        )
    if (
        not policy.allow_candidate_auto_selection
        or not policy.allow_merge
        or policy.merge_actor is None
    ):
        return _blocked("autopilot_policy_disabled")
    if any(
        asset.approval_gate is ApprovalGate.HUMAN
        for asset in (*spec.datasets, *spec.evaluators)
    ) and request.approval.approval_gate is not ApprovalGate.HUMAN:
        return _blocked("human_asset_approval_required")

    selected = next(
        (
            candidate
            for candidate in request.ranked_candidates
            if candidate.eligible
        ),
        None,
    )
    if selected is None:
        return _blocked("no_eligible_candidate")
    if any(
        selected.checks.get(check) != "success"
        for check in policy.required_checks
    ):
        return _blocked("required_checks_failed")

    required = GitHubCapabilities.MERGE
    if policy.allow_deployment:
        required |= GitHubCapabilities.DEPLOY_DISPATCH
    permissions = gateway.verify_permissions(required)
    if required & ~permissions.granted:
        return _blocked("permission_denied")
    if not gateway.branch_protection_allows(
        request.repository_root,
        selected.pull_request,
        policy.merge_actor,
    ):
        return _blocked("branch_protection_incompatible")

    gateway.merge_pull_request(
        request.repository_root,
        selected.pull_request.number,
        selected.pull_request.head_commit,
        policy.merge_actor,
    )
    if policy.allow_deployment:
        gateway.dispatch_deployment(
            request.repository_root,
            selected.pull_request.number,
            selected.pull_request.head_commit,
        )
        status = CandidateReconcileStatus.DEPLOYMENT_DISPATCHED
    else:
        status = CandidateReconcileStatus.MERGED
    return CandidateReconcileResult(
        status=status,
        selected_candidate_id=selected.candidate_id,
        pull_request_number=selected.pull_request.number,
    )


def _blocked(reason_code: str) -> CandidateReconcileResult:
    return CandidateReconcileResult(
        status=CandidateReconcileStatus.BLOCKED,
        reason_code=reason_code,
    )
