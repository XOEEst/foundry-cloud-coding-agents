from __future__ import annotations

from pathlib import Path
from typing import Mapping

from foundry_opt.config.models import OptimizerConfig
from foundry_opt.github_workflow.models import (
    GitHubCapabilities,
    GitHubPermissionReport,
    IssueReference,
    RepositoryState,
)
from foundry_opt.optimization.issues import parse_optimization_issue_request
from foundry_opt.optimization.models import (
    ApprovalGate,
    spec_is_autopilot_eligible,
)
from foundry_opt.optimization.production import (
    _build_deferred_specification_asset_registry,
)
from foundry_opt.optimization.specification import (
    OptimizationSpecService,
    PreparedSpecCommit,
    SpecServiceStatus,
)
from foundry_opt.orchestration.workspace import WorkspaceIssue
from foundry_opt.orchestration.workspace_store import (
    WorkspaceSpecificationRecord,
)


class TrustedWorkspaceSpecificationResolver:
    def resolve(
        self,
        *,
        repository_root: Path,
        repository: str,
        base_branch: str,
        issue: WorkspaceIssue,
        config: OptimizerConfig,
    ) -> WorkspaceSpecificationRecord:
        request = parse_optimization_issue_request(
            issue_number=issue.number,
            repository=repository,
            body=issue.body,
        )
        if request.target not in config.targets:
            raise ValueError("workspace issue target is not configured")
        target = config.targets[request.target]
        assets = (*request.datasets, *request.evaluators)
        asset_ids = tuple(item.asset_id for item in assets)
        metric_names = tuple(sorted(request.metrics))
        if set(metric_names) != set(target.metrics):
            return WorkspaceSpecificationRecord(
                status="human_review_required",
                spec_sha256=None,
                base_commit=issue.base_commit,
                target=request.target,
                environment=target.environment,
                asset_ids=asset_ids,
                metric_names=metric_names,
                policy_reason="issue metrics differ from repository policy",
            )
        if any(
            item.approval_gate is not ApprovalGate.POLICY
            or item.source not in {"foundry", "builtin"}
            for item in assets
        ):
            return WorkspaceSpecificationRecord(
                status="human_review_required",
                spec_sha256=None,
                base_commit=issue.base_commit,
                target=request.target,
                environment=target.environment,
                asset_ids=asset_ids,
                metric_names=metric_names,
                policy_reason=(
                    "specification contains human-gated or mutable assets"
                ),
            )
        gateway = _WorkspaceSpecificationGateway(
            repository=repository,
            base_branch=base_branch,
            issue=issue,
        )
        result = OptimizationSpecService(
            config,
            registry=_build_deferred_specification_asset_registry(),
            gateway=gateway,
            publisher=_ReadOnlySpecificationPublisher(issue.base_commit),
            require_issue_label=False,
        ).prepare_specification(
            repository_root,
            issue.number,
            publish=False,
        )
        if (
            result.status is not SpecServiceStatus.COMPLETE
            or result.spec is None
            or result.spec_sha256 is None
        ):
            reason = (
                result.blockers[0]
                if result.blockers
                else "trusted specification could not be resolved"
            )
            return WorkspaceSpecificationRecord(
                status="human_review_required",
                spec_sha256=None,
                base_commit=issue.base_commit,
                target=request.target,
                environment=target.environment,
                asset_ids=asset_ids,
                metric_names=metric_names,
                policy_reason=reason,
            )
        status = (
            "policy_approved"
            if spec_is_autopilot_eligible(
                result.spec, config.automation_policy
            )
            and not result.new_asset_paths
            else "human_review_required"
        )
        return WorkspaceSpecificationRecord(
            status=status,
            spec_sha256=result.spec_sha256,
            base_commit=issue.base_commit,
            target=result.spec.target,
            environment=result.spec.environment,
            asset_ids=tuple(
                item.asset_id
                for item in (*result.spec.datasets, *result.spec.evaluators)
            ),
            metric_names=tuple(sorted(result.spec.metrics)),
            policy_reason=(
                "repository policy approved immutable existing assets"
                if status == "policy_approved"
                else "repository automation policy requires human review"
            ),
        )


class _WorkspaceSpecificationGateway:
    def __init__(
        self,
        *,
        repository: str,
        base_branch: str,
        issue: WorkspaceIssue,
    ) -> None:
        self._repository = repository
        self._base_branch = base_branch
        self._issue = issue

    def verify_permissions(
        self, required: GitHubCapabilities
    ) -> GitHubPermissionReport:
        return GitHubPermissionReport(granted=required)

    def repository_state(self, repository_root: Path) -> RepositoryState:
        return RepositoryState(
            repository=self._repository,
            default_branch=self._base_branch,
            default_commit=self._issue.base_commit,
        )

    def get_issue(
        self, repository_root: Path, issue_number: int
    ) -> IssueReference | None:
        if issue_number != self._issue.number:
            return None
        return IssueReference(
            number=self._issue.number,
            url=(
                f"https://github.com/{self._repository}/issues/"
                f"{self._issue.number}"
            ),
            title=self._issue.title,
            body=self._issue.body,
            labels=(),
        )

    def find_spec_pull_request(self, *args, **kwargs):
        return None

    def comment_issue(self, *args, **kwargs) -> None:
        return None

    def has_issue_comment(self, *args, **kwargs) -> bool:
        return False

    def add_labels(self, *args, **kwargs) -> None:
        return None

    def remove_labels(self, *args, **kwargs) -> None:
        return None


class _ReadOnlySpecificationPublisher:
    def __init__(self, base_commit: str) -> None:
        self._base_commit = base_commit

    def prepare_commit(
        self,
        repository_root: Path,
        *,
        base_commit: str,
        files: Mapping[Path, bytes],
        message: str,
    ) -> PreparedSpecCommit:
        if base_commit != self._base_commit:
            raise ValueError("workspace specification base commit changed")
        return PreparedSpecCommit(base_commit, base_commit)

    def publish(self, *args, **kwargs):
        raise RuntimeError("read-only workspace specification cannot publish")


__all__ = ["TrustedWorkspaceSpecificationResolver"]
