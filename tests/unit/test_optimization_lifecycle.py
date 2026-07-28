from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from foundry_opt.campaign.models import (
    CandidateArtifact,
    PatchArtifact,
)
from foundry_opt.campaign.protocols import PinnedRepository
from foundry_opt.campaign.state import (
    CampaignState,
    CandidateState,
    FinalizedPublication,
    MemoryCampaignStateStore,
)
from foundry_opt.config.models import AutomationPolicy, OptimizerConfig
from foundry_opt.deployment import (
    DeploymentLineageMismatchError,
    DeploymentTrigger,
    DeploymentWorkflow,
)
from foundry_opt.github_workflow.models import (
    AppliedPatch,
    ArtifactInspection,
    GitHubCapabilities,
    GitHubPermissionReport,
    PullRequestReference,
    RepositoryState,
)
from foundry_opt.optimization.commands import (
    OptimizeCommandRequest,
    OptimizeCommandStatus,
    OptimizePhase,
)
from foundry_opt.optimization.models import (
    AssetKind,
    AssetProvenance,
    DecisionMode,
    DeploymentMode,
    OptimizationSpec,
)
from foundry_opt.optimization.runner import (
    CapabilityUnavailableError,
    IssueOptimizationDependencies,
    IssueOptimizationRunner,
)
from foundry_opt.optimization.specification import spec_file_path
from foundry_opt.config.models import MetricPolicy, MutationClass
from foundry_opt.optimization.lifecycle import (
    CandidateApplyService,
    CandidateReconcileService,
    DeploymentOutcome,
    DeploymentOutcomeStatus,
    FileLifecycleStateStore,
    LifecycleDependencies,
    LifecycleState,
    LifecycleStateError,
    MemoryLifecycleStateStore,
    PostDeployOutcome,
    PostDeployStatus,
)


BASE_COMMIT = "b" * 40
RESULT_COMMIT = "c" * 40
RESULT_TREE = "d" * 40
APPROVAL_COMMIT = "a" * 40
MERGE_COMMIT = "e" * 40
MERGE_TREE = "f" * 40
REPOSITORY = "octo-org/agents"
DEFAULT_BRANCH = "main"
CAMPAIGN_ID = "issue-7"
ISSUE = 7
GOAL = (
    "Improve the support agent's policy coverage while preserving the "
    "advisory safety boundary across every candidate."
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


_CONFIG_YAML = """
schema_version: "1"
default_environment: acceptance
environments:
  acceptance:
    project_endpoint: https://example.services.ai.azure.com/api/projects/demo
    project_resource_id: /subscriptions/sub/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/foundry/projects/demo
    allowed_models: [gpt-5.1]
    deployment_workflow:
      path: .github/workflows/deploy.yml
      trigger: manual
targets:
  support_agent:
    environment: acceptance
    source_paths: [agent]
    edit_paths: [agent]
    entry_point: agent/main.py
    base_agent_version: "12"
    package:
      include: ["agent/**"]
      exclude: []
    datasets:
      development:
        - {name: dev, version: v1, mode: batch}
      validation:
        - {name: held-out, version: v1, mode: batch}
    evaluators:
      - {name: quality, reference: quality-evaluator, metrics: [quality]}
    validation_commands: ["uv run pytest -q"]
    metrics:
      quality:
        direction: maximize
        threshold: 0.8
        materiality: 0.05
        hard_guardrail: false
        undefined_behavior: fail
    allowed_mutations: [system_instructions]
campaign:
  deadline_minutes: 50
  candidate_cutoff_minutes: 40
  max_changed_candidates: 2
  transient_retries: 1
  stale_after_hours: 2
  evidence_path: .foundry-optimizer/campaigns
  allowed_mutations: [system_instructions]
"""


def _autopilot_policy(*, deploy: bool = False) -> AutomationPolicy:
    return AutomationPolicy(
        allowed_dataset_sources={"foundry", "synthetic"},
        allowed_evaluator_sources={"foundry"},
        allow_candidate_auto_selection=True,
        allow_merge=True,
        allow_deployment=deploy,
        merge_actor="foundry-opt-merge-app",
        required_checks=(
            "foundry-opt/spec",
            "foundry-opt/exact-patch",
        ),
    )


def _config(policy: AutomationPolicy | None = None) -> OptimizerConfig:
    config = OptimizerConfig.model_validate(yaml.safe_load(_CONFIG_YAML))
    if policy is not None:
        config = config.model_copy(update={"automation_policy": policy})
    return config


# ---------------------------------------------------------------------------
# Spec + finalized campaign state
# ---------------------------------------------------------------------------


def _spec(
    *,
    decision_mode: DecisionMode = DecisionMode.HUMAN,
    deployment_mode: DeploymentMode = DeploymentMode.HUMAN,
) -> OptimizationSpec:
    return OptimizationSpec(
        issue_number=ISSUE,
        repository=REPOSITORY,
        base_commit=BASE_COMMIT,
        target="support_agent",
        environment="acceptance",
        base_agent_version="12",
        goal=GOAL,
        datasets=(
            AssetProvenance(
                asset_id="development",
                kind=AssetKind.DATASET,
                source="foundry",
                role="development",
                name="support-development",
                version="v1",
                created_by="foundry-opt",
            ),
            AssetProvenance(
                asset_id="validation",
                kind=AssetKind.DATASET,
                source="foundry",
                role="validation",
                name="support-validation",
                version="v1",
                created_by="foundry-opt",
            ),
        ),
        evaluators=(
            AssetProvenance(
                asset_id="quality",
                kind=AssetKind.EVALUATOR,
                source="foundry",
                name="quality-evaluator",
                version="v1",
                created_by="foundry-opt",
                metrics=("quality",),
            ),
        ),
        metrics={
            "quality": MetricPolicy(
                direction="maximize",
                threshold=0.8,
                materiality=0.05,
                hard_guardrail=False,
                undefined_behavior="fail",
            )
        },
        allowed_mutations=frozenset({MutationClass.SYSTEM_INSTRUCTIONS}),
        decision_mode=decision_mode,
        deployment_mode=deployment_mode,
    )


def _patch_sha(candidate_id: str) -> str:
    return _sha(f"patch::{candidate_id}")


def _patch_path(candidate_id: str) -> Path:
    return Path(
        f".foundry-optimizer/campaigns/{CAMPAIGN_ID}/patches/"
        f"{candidate_id}.diff"
    )


_EVIDENCE_PATH = Path(
    f".foundry-optimizer/campaigns/{CAMPAIGN_ID}/validation-evidence.json"
)


def _artifact(
    candidate_id: str,
    *,
    eligible: bool = True,
) -> CandidateArtifact:
    return CandidateArtifact(
        candidate_id=candidate_id,
        patch=PatchArtifact(
            candidate_id=candidate_id,
            path=_patch_path(candidate_id),
            sha256=_patch_sha(candidate_id),
            base_commit=BASE_COMMIT,
            result_commit=RESULT_COMMIT,
        ),
        draft_id=f"draft-{candidate_id}",
        evidence_path=_EVIDENCE_PATH,
        eligible=eligible,
        metrics={"quality": 0.9},
    )


def _finalized_state(
    spec: OptimizationSpec,
    *,
    candidate_ids: tuple[str, ...] = ("candidate-1", "candidate-2"),
    pareto: tuple[str, ...] | None = None,
    campaign_pull_request: int = 100,
) -> CampaignState:
    pareto = pareto if pareto is not None else candidate_ids
    now = datetime(2026, 7, 28, tzinfo=UTC)
    candidates = tuple(
        CandidateState(
            candidate_id=candidate_id,
            slot=index,
            status="evaluated",
            artifact=_artifact(
                candidate_id, eligible=candidate_id in pareto
            ),
        )
        for index, candidate_id in enumerate(candidate_ids)
    )
    issue_numbers = {
        candidate_id: 200 + index
        for index, candidate_id in enumerate(candidate_ids)
    }
    return CampaignState(
        campaign_id=CAMPAIGN_ID,
        target="support_agent",
        base_commit=BASE_COMMIT,
        status="completed",
        started_at=now,
        updated_at=now,
        goal_sha256=_sha(spec.goal),
        spec_sha256=spec.sha256,
        assets=(),
        baseline_draft_id="draft-baseline",
        candidates=candidates,
        launched_slots=len(candidate_ids),
        pareto_candidate_ids=pareto,
        finalized=FinalizedPublication(
            campaign_pull_request_number=campaign_pull_request,
            campaign_pull_request_url=(
                f"https://github.com/{REPOSITORY}/pull/"
                f"{campaign_pull_request}"
            ),
            candidate_issue_numbers=issue_numbers,
        ),
    )


def _write_repo(
    tmp_path: Path,
    spec: OptimizationSpec,
    state: CampaignState,
) -> None:
    spec_path = tmp_path / spec_file_path(ISSUE)
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(
        yaml.safe_dump(spec.model_dump(mode="json")),
        encoding="utf-8",
    )
    evidence = {
        "campaign_id": state.campaign_id,
        "candidates": [
            {
                "subject_id": candidate.candidate_id,
                "patch_hash": candidate.artifact.patch.sha256,
                "agent": {"draft_id": candidate.artifact.draft_id},
                "result_tree": RESULT_TREE,
            }
            for candidate in state.candidates
            if candidate.artifact is not None
        ],
        "pareto": {"eligible_ids": list(state.pareto_candidate_ids)},
    }
    evidence_path = tmp_path / _EVIDENCE_PATH
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeClock:
    def now(self) -> datetime:
        return datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


class FakeRepository:
    def pin_default_branch(self, repository_root: Path) -> PinnedRepository:
        return PinnedRepository(
            default_branch=DEFAULT_BRANCH,
            commit=BASE_COMMIT,
        )


@dataclass
class FakeApprovalReport:
    approved: bool
    default_branch: str | None
    approval_commit: str | None
    reason: str | None


class FakeSpecApproval:
    def __init__(self, *, approved: bool = True) -> None:
        self.approved = approved

    def verify_spec_approval(
        self,
        repository_root: Path,
        *,
        issue_number: int,
        spec: OptimizationSpec,
        spec_sha256: str,
        base_commit: str,
    ) -> FakeApprovalReport:
        if not self.approved:
            return FakeApprovalReport(
                approved=False,
                default_branch=None,
                approval_commit=None,
                reason="the specification is not merged",
            )
        return FakeApprovalReport(
            approved=True,
            default_branch=DEFAULT_BRANCH,
            approval_commit=APPROVAL_COMMIT,
            reason=None,
        )


class FakeGithubGateway:
    """Candidate + issue gateway. Echoes created PRs and records mutations."""

    def __init__(
        self,
        *,
        granted: GitHubCapabilities = (
            GitHubCapabilities.CANDIDATE_PUBLICATION
        ),
        fail_close_pr_times: int = 0,
    ) -> None:
        self.granted = granted
        self._prs: dict[str, PullRequestReference] = {}
        self._counter = 300
        self.comments: list[tuple[int, str]] = []
        self.updated_bodies: list[tuple[int, str]] = []
        self.closed_issues: list[tuple[int, str]] = []
        self.closed_prs: list[tuple[int, str]] = []
        self._fail_close_pr_times = fail_close_pr_times

    def verify_permissions(
        self,
        required: GitHubCapabilities,
    ) -> GitHubPermissionReport:
        return GitHubPermissionReport(self.granted & required)

    def repository_state(self, repository_root: Path) -> RepositoryState:
        return RepositoryState(REPOSITORY, DEFAULT_BRANCH, BASE_COMMIT)

    def find_candidate_pull_request(
        self,
        repository_root: Path,
        head_branch: str,
    ) -> PullRequestReference | None:
        return self._prs.get(head_branch)

    def create_candidate_pull_request(
        self,
        repository_root: Path,
        *,
        base_branch: str,
        head_branch: str,
        commit_sha: str,
        title: str,
        body: str,
    ) -> PullRequestReference:
        self._counter += 1
        pull_request = PullRequestReference(
            number=self._counter,
            url=f"https://github.com/{REPOSITORY}/pull/{self._counter}",
            head_branch=head_branch,
            head_commit=commit_sha,
            draft=False,
            body=body,
            base_branch=base_branch,
            state="OPEN",
        )
        self._prs[head_branch] = pull_request
        return pull_request

    def comment_issue(
        self,
        repository_root: Path,
        issue_number: int,
        body: str,
    ) -> None:
        self.comments.append((issue_number, body))

    def update_issue_body(
        self,
        repository_root: Path,
        issue_number: int,
        body: str,
    ) -> None:
        self.updated_bodies.append((issue_number, body))

    def close_issue(
        self,
        repository_root: Path,
        issue_number: int,
        comment: str,
    ) -> None:
        self.closed_issues.append((issue_number, comment))

    def close_pull_request(
        self,
        repository_root: Path,
        pull_request_number: int,
        comment: str,
    ) -> None:
        if self._fail_close_pr_times > 0:
            self._fail_close_pr_times -= 1
            raise RuntimeError("transient close failure")
        self.closed_prs.append((pull_request_number, comment))


class FakePatchApplier:
    def __init__(self, root: Path) -> None:
        self._root = root
        self.applied: list[Any] = []

    def inspect_artifact(
        self,
        repository_root: Path,
        path: Path,
    ) -> ArtifactInspection:
        posix = path.as_posix()
        for candidate_id in ("candidate-1", "candidate-2", "candidate-3"):
            if posix == _patch_path(candidate_id).as_posix():
                content = b"exact-patch-bytes"
                return ArtifactInspection(
                    path=path,
                    sha256=_patch_sha(candidate_id),
                    byte_count=len(content),
                    content=content,
                )
        data = (repository_root / path).read_bytes()
        return ArtifactInspection(
            path=path,
            sha256=hashlib.sha256(data).hexdigest(),
            byte_count=len(data),
            content=data,
        )

    def apply_exact(self, request: Any) -> AppliedPatch:
        self.applied.append(request)
        return AppliedPatch(
            branch=request.branch,
            commit_sha=RESULT_COMMIT,
            changed_paths=(Path("agent/main.py"),),
            exact=True,
            substantive_repair=False,
            tree_sha=RESULT_TREE,
        )

    def resolve_tree(
        self,
        repository_root: Path,
        commit: str,
    ) -> str | None:
        return RESULT_TREE

    def resolve_branch_commit(
        self,
        repository_root: Path,
        branch: str,
    ) -> str | None:
        return RESULT_COMMIT

    def restore_after_publication_failure(
        self,
        repository_root: Path,
        base_commit: str,
        base_branch: str,
    ) -> None:
        return None


class FakeReconcileGateway:
    def __init__(
        self,
        *,
        granted: GitHubCapabilities = (
            GitHubCapabilities.MERGE | GitHubCapabilities.DEPLOY_DISPATCH
        ),
        branch_allowed: bool = True,
        checks: dict[str, str] | None = None,
        located: tuple[str, ...] = ("candidate-1", "candidate-2"),
        states: dict[str, str] | None = None,
    ) -> None:
        self.granted = granted
        self.branch_allowed = branch_allowed
        self._checks = checks or {
            "foundry-opt/spec": "success",
            "foundry-opt/exact-patch": "success",
        }
        self._located = located
        self._states = states or {}
        self.merged: list[tuple[int, str, str]] = []
        self.dispatched: list[tuple[int, str]] = []
        self._pr_numbers = {
            "candidate-1": 401,
            "candidate-2": 402,
            "candidate-3": 403,
        }

    def verify_permissions(
        self,
        required: GitHubCapabilities,
    ) -> GitHubPermissionReport:
        return GitHubPermissionReport(self.granted & required)

    def branch_protection_allows(
        self,
        repository_root: Path,
        pull_request: PullRequestReference,
        actor: str,
    ) -> bool:
        return self.branch_allowed

    def merge_pull_request(
        self,
        repository_root: Path,
        pull_request_number: int,
        expected_head_commit: str,
        actor: str,
    ) -> None:
        self.merged.append(
            (pull_request_number, expected_head_commit, actor)
        )

    def dispatch_deployment(
        self,
        repository_root: Path,
        pull_request_number: int,
        expected_head_commit: str,
    ) -> None:
        self.dispatched.append((pull_request_number, expected_head_commit))

    def locate_candidate_pull_request(
        self,
        repository_root: Path,
        campaign_id: str,
        candidate_id: str,
    ) -> PullRequestReference | None:
        if candidate_id not in self._located:
            return None
        number = self._pr_numbers[candidate_id]
        return PullRequestReference(
            number=number,
            url=f"https://github.com/{REPOSITORY}/pull/{number}",
            head_branch=(
                f"foundry-opt/{CAMPAIGN_ID}/{candidate_id}/lifecycle"
            ),
            head_commit=RESULT_COMMIT,
            draft=False,
            body="candidate",
            base_branch=DEFAULT_BRANCH,
            state=self._states.get(candidate_id, "OPEN"),
        )

    def candidate_checks(
        self,
        repository_root: Path,
        pull_request: PullRequestReference,
    ) -> dict[str, str]:
        return dict(self._checks)

    def resolve_merge_commit(
        self,
        repository_root: Path,
        pull_request_number: int,
    ) -> str:
        return MERGE_COMMIT

    def resolve_tree(
        self,
        repository_root: Path,
        commit: str,
    ) -> str | None:
        return MERGE_TREE


class FakeDeploymentCoordinator:
    def __init__(
        self,
        *,
        outcome: DeploymentOutcome | None = None,
        error: Exception | None = None,
    ) -> None:
        self.outcome = outcome or DeploymentOutcome(
            status=DeploymentOutcomeStatus.VERIFIED,
            version=13,
            run_url=(
                f"https://github.com/{REPOSITORY}/actions/runs/9001"
            ),
            portal_url="https://ai.azure.com/projects/demo/agents/support",
        )
        self.error = error
        self.requests: list[Any] = []

    def deploy(self, request: Any) -> DeploymentOutcome:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.outcome


class FakePostDeployEvaluator:
    def __init__(
        self,
        *,
        outcome: PostDeployOutcome | None = None,
        error: Exception | None = None,
    ) -> None:
        self.outcome = outcome or PostDeployOutcome(
            status=PostDeployStatus.RETAINED_IMPROVEMENT,
            metrics={"quality": 0.9},
        )
        self.error = error
        self.requests: list[Any] = []

    def evaluate(self, request: Any) -> PostDeployOutcome:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.outcome


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@dataclass
class Harness:
    root: Path
    dependencies: LifecycleDependencies
    github: FakeGithubGateway
    reconcile: FakeReconcileGateway
    applier: FakePatchApplier
    deployment: FakeDeploymentCoordinator
    post_deploy: FakePostDeployEvaluator
    lifecycle_state: MemoryLifecycleStateStore

    def apply_service(self) -> CandidateApplyService:
        return CandidateApplyService(self.dependencies)

    def reconcile_service(self) -> CandidateReconcileService:
        return CandidateReconcileService(self.dependencies)


def _harness(
    tmp_path: Path,
    *,
    spec: OptimizationSpec | None = None,
    state: CampaignState | None = None,
    policy: AutomationPolicy | None = None,
    github: FakeGithubGateway | None = None,
    reconcile: FakeReconcileGateway | None = None,
    deployment: FakeDeploymentCoordinator | None = None,
    post_deploy: FakePostDeployEvaluator | None = None,
    workflow_trigger: DeploymentTrigger = DeploymentTrigger.MERGE,
    approved: bool = True,
    write: bool = True,
    lifecycle_store: Any | None = None,
) -> Harness:
    spec = spec or _spec()
    state = state or _finalized_state(spec)
    if write:
        _write_repo(tmp_path, spec, state)
    campaign_store = MemoryCampaignStateStore()
    campaign_store.save(tmp_path, state)
    lifecycle_state = lifecycle_store or MemoryLifecycleStateStore()
    github = github or FakeGithubGateway()
    reconcile = reconcile or FakeReconcileGateway()
    applier = FakePatchApplier(tmp_path)
    deployment = deployment or FakeDeploymentCoordinator()
    post_deploy = post_deploy or FakePostDeployEvaluator()
    workflow = DeploymentWorkflow(
        path=Path(".github/workflows/deploy.yml"),
        trigger=workflow_trigger,
        exists=True,
        name="Deploy",
    )
    dependencies = LifecycleDependencies(
        config=_config(policy),
        state=campaign_store,
        lifecycle_state=lifecycle_state,
        github_gateway_factory=lambda root: github,
        reconcile_gateway_factory=lambda root: reconcile,
        patch_applier=applier,
        repository=FakeRepository(),
        spec_approval=FakeSpecApproval(approved=approved),
        deployment=deployment,
        post_deploy=post_deploy,
        clock=FakeClock(),
        detect_workflow=lambda root: workflow,
    )
    return Harness(
        root=tmp_path,
        dependencies=dependencies,
        github=github,
        reconcile=reconcile,
        applier=applier,
        deployment=deployment,
        post_deploy=post_deploy,
        lifecycle_state=lifecycle_state,
    )


def _apply_request(
    tmp_path: Path,
    candidate_id: str = "candidate-1",
    *,
    verify_only: bool = False,
) -> OptimizeCommandRequest:
    return OptimizeCommandRequest(
        repository_root=tmp_path,
        issue_number=ISSUE,
        phase=OptimizePhase.APPLY,
        candidate_id=candidate_id,
        verify_only=verify_only,
    )


def _reconcile_request(tmp_path: Path) -> OptimizeCommandRequest:
    return OptimizeCommandRequest(
        repository_root=tmp_path,
        issue_number=ISSUE,
        phase=OptimizePhase.RECONCILE,
    )


# ---------------------------------------------------------------------------
# APPLY tests
# ---------------------------------------------------------------------------


def test_apply_publishes_exact_candidate(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    result = harness.apply_service().execute(_apply_request(tmp_path))

    assert result.status is OptimizeCommandStatus.COMPLETE
    assert result.details["code"] == "applied"
    assert result.details["candidate_id"] == "candidate-1"
    assert result.details["commit_sha"] == RESULT_COMMIT
    # The candidate (child) issue received the exact-patch comment, and the
    # parent optimization issue received exactly one apply note.
    child_comments = [c for c in harness.github.comments if c[0] == 200]
    parent_comments = [c for c in harness.github.comments if c[0] == ISSUE]
    assert len(child_comments) == 1
    assert len(parent_comments) == 1
    assert len(harness.applier.applied) == 1


def test_apply_is_idempotent(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    first = harness.apply_service().execute(_apply_request(tmp_path))
    second = harness.apply_service().execute(_apply_request(tmp_path))

    assert first.details["code"] == "applied"
    assert second.status is OptimizeCommandStatus.COMPLETE
    assert second.details["code"] == "already_applied"
    # Exactly one patch application and one parent comment across both runs.
    assert len(harness.applier.applied) == 1
    parent_comments = [c for c in harness.github.comments if c[0] == ISSUE]
    assert len(parent_comments) == 1


def test_verify_only_reports_not_applied_without_writes(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)

    result = harness.apply_service().execute(
        _apply_request(tmp_path, verify_only=True)
    )

    assert result.status is OptimizeCommandStatus.AWAITING_AGENT
    assert result.details["code"] == "candidate_not_applied"
    # No repository or GitHub writes were performed.
    assert harness.applier.applied == []
    assert harness.github.comments == []
    assert harness.github._prs == {}
    assert harness.github.closed_issues == []


def test_verify_only_verifies_applied_candidate_without_writes(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    harness.apply_service().execute(_apply_request(tmp_path))
    applied_count = len(harness.applier.applied)
    comment_count = len(harness.github.comments)

    result = harness.apply_service().execute(
        _apply_request(tmp_path, verify_only=True)
    )

    assert result.status is OptimizeCommandStatus.COMPLETE
    assert result.details["code"] == "verified"
    assert result.details["candidate_id"] == "candidate-1"
    # Verification performed no additional writes.
    assert len(harness.applier.applied) == applied_count
    assert len(harness.github.comments) == comment_count


def test_apply_blocks_when_campaign_not_finalized(tmp_path: Path) -> None:
    spec = _spec()
    state = _finalized_state(spec)
    _write_repo(tmp_path, spec, state)
    campaign_store = MemoryCampaignStateStore()  # empty: no saved campaign
    dependencies = LifecycleDependencies(
        config=_config(),
        state=campaign_store,
        lifecycle_state=MemoryLifecycleStateStore(),
        github_gateway_factory=lambda root: FakeGithubGateway(),
        reconcile_gateway_factory=lambda root: FakeReconcileGateway(),
        patch_applier=FakePatchApplier(tmp_path),
        repository=FakeRepository(),
        spec_approval=FakeSpecApproval(),
        deployment=FakeDeploymentCoordinator(),
        post_deploy=FakePostDeployEvaluator(),
        clock=FakeClock(),
    )

    result = CandidateApplyService(dependencies).execute(
        _apply_request(tmp_path)
    )

    assert result.status is OptimizeCommandStatus.BLOCKED
    assert result.details["code"] == "campaign_not_finalized"


def test_apply_blocks_ineligible_candidate(tmp_path: Path) -> None:
    spec = _spec()
    state = _finalized_state(spec, pareto=("candidate-1",))
    harness = _harness(tmp_path, spec=spec, state=state)

    result = harness.apply_service().execute(
        _apply_request(tmp_path, "candidate-2")
    )

    assert result.status is OptimizeCommandStatus.BLOCKED
    assert result.details["code"] == "candidate_not_eligible"


def test_apply_rejection_is_reported_and_not_applied(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    # A default-branch base drift makes the exact-patch verification reject.
    harness.github.repository_state = (  # type: ignore[assignment]
        lambda repository_root: RepositoryState(
            REPOSITORY, DEFAULT_BRANCH, "1" * 40
        )
    )

    result = harness.apply_service().execute(_apply_request(tmp_path))

    assert result.status is OptimizeCommandStatus.FAILED
    assert result.details["code"] == "base_changed"
    assert harness.applier.applied == []


# ---------------------------------------------------------------------------
# RECONCILE tests
# ---------------------------------------------------------------------------


def test_reconcile_human_reports_ranked_and_waits(tmp_path: Path) -> None:
    harness = _harness(tmp_path, spec=_spec(decision_mode=DecisionMode.HUMAN))

    result = harness.reconcile_service().execute(_reconcile_request(tmp_path))

    assert result.status is OptimizeCommandStatus.AWAITING_AGENT
    assert result.details["code"] == "waiting_for_human"
    ranked = result.details["ranked_candidates"]
    assert {item["candidate_id"] for item in ranked} == {
        "candidate-1",
        "candidate-2",
    }
    assert harness.reconcile.merged == []


def test_reconcile_blocks_without_applied_candidates(tmp_path: Path) -> None:
    harness = _harness(
        tmp_path,
        spec=_spec(decision_mode=DecisionMode.HUMAN),
        reconcile=FakeReconcileGateway(located=()),
    )

    result = harness.reconcile_service().execute(_reconcile_request(tmp_path))

    assert result.status is OptimizeCommandStatus.BLOCKED
    assert result.details["code"] == "no_applied_candidates"


def test_reconcile_autopilot_denied_when_policy_disabled(
    tmp_path: Path,
) -> None:
    harness = _harness(
        tmp_path,
        spec=_spec(decision_mode=DecisionMode.AUTOPILOT_IF_ALLOWED),
        policy=None,  # default automation policy disables merge
    )

    result = harness.reconcile_service().execute(_reconcile_request(tmp_path))

    assert result.status is OptimizeCommandStatus.BLOCKED
    assert result.details["code"] == "autopilot_policy_disabled"
    assert harness.reconcile.merged == []


def test_reconcile_autopilot_blocks_on_branch_protection(
    tmp_path: Path,
) -> None:
    harness = _harness(
        tmp_path,
        spec=_spec(decision_mode=DecisionMode.AUTOPILOT_IF_ALLOWED),
        policy=_autopilot_policy(),
        reconcile=FakeReconcileGateway(branch_allowed=False),
    )

    result = harness.reconcile_service().execute(_reconcile_request(tmp_path))

    assert result.status is OptimizeCommandStatus.BLOCKED
    assert result.details["code"] == "branch_protection_incompatible"
    assert harness.reconcile.merged == []


def test_reconcile_autopilot_blocks_on_failed_checks(tmp_path: Path) -> None:
    harness = _harness(
        tmp_path,
        spec=_spec(decision_mode=DecisionMode.AUTOPILOT_IF_ALLOWED),
        policy=_autopilot_policy(),
        reconcile=FakeReconcileGateway(
            checks={
                "foundry-opt/spec": "success",
                "foundry-opt/exact-patch": "failure",
            }
        ),
    )

    result = harness.reconcile_service().execute(_reconcile_request(tmp_path))

    assert result.status is OptimizeCommandStatus.BLOCKED
    assert result.details["code"] == "required_checks_failed"


def test_reconcile_autopilot_requires_separate_deploy_capability(
    tmp_path: Path,
) -> None:
    harness = _harness(
        tmp_path,
        spec=_spec(
            decision_mode=DecisionMode.AUTOPILOT_IF_ALLOWED,
            deployment_mode=DeploymentMode.AFTER_MERGE_IF_ALLOWED,
        ),
        policy=_autopilot_policy(deploy=True),
        reconcile=FakeReconcileGateway(granted=GitHubCapabilities.MERGE),
        workflow_trigger=DeploymentTrigger.MANUAL,
    )

    result = harness.reconcile_service().execute(_reconcile_request(tmp_path))

    # The merge uses MERGE; auto-dispatching the deployment requires the
    # separate DEPLOY_DISPATCH capability, which is verified after the merge.
    assert result.status is OptimizeCommandStatus.BLOCKED
    assert result.details["code"] == "permission_denied"
    assert harness.reconcile.merged == [
        (401, RESULT_COMMIT, "foundry-opt-merge-app")
    ]
    assert harness.deployment.requests == []


def test_reconcile_human_deployment_observes_then_verifies(
    tmp_path: Path,
) -> None:
    # decision autopilot, deployment human + manual workflow: the deploy is
    # not auto-dispatched; the coordinator observes until the maintainer
    # triggers it.
    pending = FakeDeploymentCoordinator(
        outcome=DeploymentOutcome(
            status=DeploymentOutcomeStatus.MANUAL_TRIGGER_REQUIRED,
        )
    )
    harness = _harness(
        tmp_path,
        spec=_spec(
            decision_mode=DecisionMode.AUTOPILOT_IF_ALLOWED,
            deployment_mode=DeploymentMode.HUMAN,
        ),
        policy=_autopilot_policy(),
        workflow_trigger=DeploymentTrigger.MANUAL,
        deployment=pending,
    )

    result = harness.reconcile_service().execute(_reconcile_request(tmp_path))

    assert result.status is OptimizeCommandStatus.AWAITING_AGENT
    assert result.details["code"] == "deployment_manual_trigger_required"
    # The coordinator observed without an optimizer dispatch.
    assert pending.requests[0].dispatch is False
    assert harness.reconcile.dispatched == []
    assert harness.github.closed_issues == []
    saved = harness.lifecycle_state.load(tmp_path, CAMPAIGN_ID)
    assert saved is not None
    assert saved.merge_commit == MERGE_COMMIT
    assert saved.deployment_dispatched is False
    assert saved.parent_closed is False

    # The maintainer triggers the workflow; a rerun observes VERIFIED and the
    # lifecycle completes.
    pending.outcome = DeploymentOutcome(
        status=DeploymentOutcomeStatus.VERIFIED, version=13
    )
    rerun = harness.reconcile_service().execute(_reconcile_request(tmp_path))
    assert rerun.status is OptimizeCommandStatus.COMPLETE
    assert rerun.details["code"] == "reconciled"
    assert pending.requests[1].dispatch is False
    assert ISSUE in [n for n, _ in harness.github.closed_issues]


def test_reconcile_autopilot_merge_trigger_success_closes_issue(
    tmp_path: Path,
) -> None:
    harness = _harness(
        tmp_path,
        spec=_spec(
            decision_mode=DecisionMode.AUTOPILOT_IF_ALLOWED,
            deployment_mode=DeploymentMode.AFTER_MERGE_IF_ALLOWED,
        ),
        policy=_autopilot_policy(deploy=True),
        reconcile=FakeReconcileGateway(granted=GitHubCapabilities.MERGE),
        workflow_trigger=DeploymentTrigger.MERGE,
    )

    result = harness.reconcile_service().execute(_reconcile_request(tmp_path))

    assert result.status is OptimizeCommandStatus.COMPLETE
    assert result.details["code"] == "reconciled"
    assert result.details["candidate_id"] == "candidate-1"
    assert result.details["deployment_version"] == 13
    # Merge trigger: the deploy workflow runs on merge, no explicit dispatch.
    assert harness.reconcile.dispatched == []
    coordinator_request = harness.deployment.requests[0]
    assert coordinator_request.workflow.trigger is DeploymentTrigger.MERGE
    assert coordinator_request.dispatch is False
    assert coordinator_request.merge_commit == MERGE_COMMIT
    # Parent updated, superseded surfaces closed, parent closed exactly once.
    assert harness.github.updated_bodies[0][0] == ISSUE
    assert 201 in [n for n, _ in harness.github.closed_issues]
    assert 100 in [n for n, _ in harness.github.closed_prs]
    assert ISSUE in [n for n, _ in harness.github.closed_issues]


def test_reconcile_flows_published_version_and_post_deploy_metrics(
    tmp_path: Path,
) -> None:
    # The published deployment version and the post-deployment metrics must
    # both cross into the closed issue: its updated body, close comment, and
    # the machine-readable reconcile details.
    harness = _harness(
        tmp_path,
        spec=_spec(
            decision_mode=DecisionMode.AUTOPILOT_IF_ALLOWED,
            deployment_mode=DeploymentMode.AFTER_MERGE_IF_ALLOWED,
        ),
        policy=_autopilot_policy(deploy=True),
        reconcile=FakeReconcileGateway(granted=GitHubCapabilities.MERGE),
        workflow_trigger=DeploymentTrigger.MERGE,
        deployment=FakeDeploymentCoordinator(
            outcome=DeploymentOutcome(
                status=DeploymentOutcomeStatus.VERIFIED, version=17
            )
        ),
        post_deploy=FakePostDeployEvaluator(
            outcome=PostDeployOutcome(
                status=PostDeployStatus.RETAINED_IMPROVEMENT,
                metrics={"quality": 0.94, "safety": 0.99},
            )
        ),
    )

    result = harness.reconcile_service().execute(_reconcile_request(tmp_path))

    assert result.status is OptimizeCommandStatus.COMPLETE
    assert result.details["deployment_version"] == 17
    assert result.details["post_deploy_metrics"] == {
        "quality": 0.94,
        "safety": 0.99,
    }
    # The updated parent body records the deployed version and the retained
    # post-deployment metrics.
    body = harness.github.updated_bodies[0][1]
    assert "17" in body
    assert "quality" in body and "0.94" in body


def test_reconcile_autopilot_manual_dispatches_via_coordinator(
    tmp_path: Path,
) -> None:
    harness = _harness(
        tmp_path,
        spec=_spec(
            decision_mode=DecisionMode.AUTOPILOT_IF_ALLOWED,
            deployment_mode=DeploymentMode.AFTER_MERGE_IF_ALLOWED,
        ),
        policy=_autopilot_policy(deploy=True),
        workflow_trigger=DeploymentTrigger.MANUAL,
    )

    result = harness.reconcile_service().execute(_reconcile_request(tmp_path))

    assert result.status is OptimizeCommandStatus.COMPLETE
    assert result.details["code"] == "reconciled"
    # reconcile_candidates never dispatches (with the PR head); the coordinator
    # dispatches against the exact merge commit.
    assert harness.reconcile.dispatched == []
    coordinator_request = harness.deployment.requests[0]
    assert coordinator_request.workflow.trigger is DeploymentTrigger.MANUAL
    assert coordinator_request.dispatch is True
    assert coordinator_request.merge_commit == MERGE_COMMIT
    saved = harness.lifecycle_state.load(tmp_path, CAMPAIGN_ID)
    assert saved is not None
    assert saved.deployment_dispatched is True


def test_reconcile_manual_dispatch_is_not_repeated_on_rerun(
    tmp_path: Path,
) -> None:
    coordinator = FakeDeploymentCoordinator(
        outcome=DeploymentOutcome(status=DeploymentOutcomeStatus.PENDING)
    )
    harness = _harness(
        tmp_path,
        spec=_spec(
            decision_mode=DecisionMode.AUTOPILOT_IF_ALLOWED,
            deployment_mode=DeploymentMode.AFTER_MERGE_IF_ALLOWED,
        ),
        policy=_autopilot_policy(deploy=True),
        workflow_trigger=DeploymentTrigger.MANUAL,
        deployment=coordinator,
    )

    first = harness.reconcile_service().execute(_reconcile_request(tmp_path))
    assert first.status is OptimizeCommandStatus.AWAITING_AGENT
    assert first.details["code"] == "deployment_pending"
    assert coordinator.requests[0].dispatch is True

    # The workflow completes; a rerun must observe, never dispatch again.
    coordinator.outcome = DeploymentOutcome(
        status=DeploymentOutcomeStatus.VERIFIED, version=13
    )
    second = harness.reconcile_service().execute(_reconcile_request(tmp_path))
    assert second.status is OptimizeCommandStatus.COMPLETE
    assert coordinator.requests[1].dispatch is False
    assert len(harness.reconcile.merged) == 1


def test_reconcile_blocks_on_deployment_lineage_mismatch(
    tmp_path: Path,
) -> None:
    harness = _harness(
        tmp_path,
        spec=_spec(
            decision_mode=DecisionMode.AUTOPILOT_IF_ALLOWED,
            deployment_mode=DeploymentMode.AFTER_MERGE_IF_ALLOWED,
        ),
        policy=_autopilot_policy(deploy=True),
        reconcile=FakeReconcileGateway(granted=GitHubCapabilities.MERGE),
        workflow_trigger=DeploymentTrigger.MERGE,
        deployment=FakeDeploymentCoordinator(
            error=DeploymentLineageMismatchError()
        ),
    )

    result = harness.reconcile_service().execute(_reconcile_request(tmp_path))

    assert result.status is OptimizeCommandStatus.BLOCKED
    assert result.details["code"] == "deployment_lineage_mismatch"
    assert harness.github.closed_issues == []


def test_reconcile_blocks_when_deployment_unavailable(tmp_path: Path) -> None:
    harness = _harness(
        tmp_path,
        spec=_spec(
            decision_mode=DecisionMode.AUTOPILOT_IF_ALLOWED,
            deployment_mode=DeploymentMode.AFTER_MERGE_IF_ALLOWED,
        ),
        policy=_autopilot_policy(deploy=True),
        reconcile=FakeReconcileGateway(granted=GitHubCapabilities.MERGE),
        workflow_trigger=DeploymentTrigger.MERGE,
        deployment=FakeDeploymentCoordinator(
            error=CapabilityUnavailableError(
                "deployment_unavailable",
                "the live deployment binding is not wired",
            )
        ),
    )

    result = harness.reconcile_service().execute(_reconcile_request(tmp_path))

    assert result.status is OptimizeCommandStatus.BLOCKED
    assert result.details["code"] == "deployment_unavailable"
    assert harness.post_deploy.requests == []
    assert harness.github.closed_issues == []


def test_reconcile_blocks_on_post_deploy_regression(tmp_path: Path) -> None:
    harness = _harness(
        tmp_path,
        spec=_spec(
            decision_mode=DecisionMode.AUTOPILOT_IF_ALLOWED,
            deployment_mode=DeploymentMode.AFTER_MERGE_IF_ALLOWED,
        ),
        policy=_autopilot_policy(deploy=True),
        reconcile=FakeReconcileGateway(granted=GitHubCapabilities.MERGE),
        workflow_trigger=DeploymentTrigger.MERGE,
        post_deploy=FakePostDeployEvaluator(
            outcome=PostDeployOutcome(
                status=PostDeployStatus.REGRESSED,
                reason_code="quality_below_baseline",
            )
        ),
    )

    result = harness.reconcile_service().execute(_reconcile_request(tmp_path))

    assert result.status is OptimizeCommandStatus.BLOCKED
    assert result.details["code"] == "post_deploy_regression"
    # The parent optimization issue is never closed on a regression.
    assert ISSUE not in [n for n, _ in harness.github.closed_issues]
    saved = harness.lifecycle_state.load(tmp_path, CAMPAIGN_ID)
    assert saved is not None
    assert saved.deployment_verified is True
    assert saved.post_deploy_retained is False
    assert saved.parent_closed is False


def test_reconcile_blocks_when_post_deploy_unavailable(tmp_path: Path) -> None:
    harness = _harness(
        tmp_path,
        spec=_spec(
            decision_mode=DecisionMode.AUTOPILOT_IF_ALLOWED,
            deployment_mode=DeploymentMode.AFTER_MERGE_IF_ALLOWED,
        ),
        policy=_autopilot_policy(deploy=True),
        reconcile=FakeReconcileGateway(granted=GitHubCapabilities.MERGE),
        workflow_trigger=DeploymentTrigger.MERGE,
        post_deploy=FakePostDeployEvaluator(
            error=CapabilityUnavailableError(
                "post_deploy_unavailable",
                "the live post-deploy evaluation binding is not wired",
            )
        ),
    )

    result = harness.reconcile_service().execute(_reconcile_request(tmp_path))

    assert result.status is OptimizeCommandStatus.BLOCKED
    assert result.details["code"] == "post_deploy_unavailable"
    assert ISSUE not in [n for n, _ in harness.github.closed_issues]


def test_reconcile_cleanup_is_idempotent_after_partial_failure(
    tmp_path: Path,
) -> None:
    # The campaign PR close fails once, then succeeds on the retry.
    github = FakeGithubGateway(fail_close_pr_times=1)
    spec = _spec(
        decision_mode=DecisionMode.AUTOPILOT_IF_ALLOWED,
        deployment_mode=DeploymentMode.AFTER_MERGE_IF_ALLOWED,
    )
    state = _finalized_state(spec)
    harness = _harness(
        tmp_path,
        spec=spec,
        state=state,
        policy=_autopilot_policy(deploy=True),
        github=github,
        reconcile=FakeReconcileGateway(granted=GitHubCapabilities.MERGE),
        workflow_trigger=DeploymentTrigger.MERGE,
    )

    first = harness.reconcile_service().execute(_reconcile_request(tmp_path))
    assert first.status is OptimizeCommandStatus.FAILED

    second = harness.reconcile_service().execute(_reconcile_request(tmp_path))
    assert second.status is OptimizeCommandStatus.COMPLETE
    assert second.details["code"] == "reconciled"

    # Each superseded surface is closed exactly once despite the retry, and
    # the deployment/merge were not repeated.
    closed_issue_numbers = [n for n, _ in github.closed_issues]
    assert closed_issue_numbers.count(201) == 1
    assert closed_issue_numbers.count(ISSUE) == 1
    assert [n for n, _ in github.closed_prs].count(100) == 1
    assert len(harness.reconcile.merged) == 1
    assert len(harness.deployment.requests) == 2  # re-observed on retry


def test_reconcile_closes_parent_only_after_retained_improvement(
    tmp_path: Path,
) -> None:
    harness = _harness(
        tmp_path,
        spec=_spec(
            decision_mode=DecisionMode.AUTOPILOT_IF_ALLOWED,
            deployment_mode=DeploymentMode.AFTER_MERGE_IF_ALLOWED,
        ),
        policy=_autopilot_policy(deploy=True),
        reconcile=FakeReconcileGateway(granted=GitHubCapabilities.MERGE),
        workflow_trigger=DeploymentTrigger.MERGE,
    )

    result = harness.reconcile_service().execute(_reconcile_request(tmp_path))

    assert result.status is OptimizeCommandStatus.COMPLETE
    saved = harness.lifecycle_state.load(tmp_path, CAMPAIGN_ID)
    assert saved is not None
    assert saved.post_deploy_retained is True
    assert saved.parent_closed is True
    assert ISSUE in [n for n, _ in harness.github.closed_issues]


def test_reconcile_blocks_when_spec_not_approved(tmp_path: Path) -> None:
    harness = _harness(
        tmp_path,
        spec=_spec(decision_mode=DecisionMode.AUTOPILOT_IF_ALLOWED),
        policy=_autopilot_policy(),
        approved=False,
    )

    result = harness.reconcile_service().execute(_reconcile_request(tmp_path))

    assert result.status is OptimizeCommandStatus.BLOCKED
    assert result.details["code"] == "spec_not_approved"


# ---------------------------------------------------------------------------
# Human selection resume
# ---------------------------------------------------------------------------


def test_reconcile_human_resume_selects_merged_candidate(
    tmp_path: Path,
) -> None:
    # A maintainer merged candidate-1 directly on GitHub; a rerun detects the
    # single merged eligible candidate, resolves its merge commit, and
    # continues the deployment/issue lifecycle.
    harness = _harness(
        tmp_path,
        spec=_spec(
            decision_mode=DecisionMode.HUMAN,
            deployment_mode=DeploymentMode.HUMAN,
        ),
        reconcile=FakeReconcileGateway(states={"candidate-1": "MERGED"}),
        workflow_trigger=DeploymentTrigger.MERGE,
    )

    result = harness.reconcile_service().execute(_reconcile_request(tmp_path))

    assert result.status is OptimizeCommandStatus.COMPLETE
    assert result.details["code"] == "reconciled"
    assert result.details["candidate_id"] == "candidate-1"
    # The human merged directly, so the optimizer never called the merge API.
    assert harness.reconcile.merged == []
    coordinator_request = harness.deployment.requests[0]
    assert coordinator_request.merge_commit == MERGE_COMMIT
    assert coordinator_request.dispatch is False
    assert ISSUE in [n for n, _ in harness.github.closed_issues]


def test_reconcile_human_waits_when_nothing_is_merged(tmp_path: Path) -> None:
    harness = _harness(
        tmp_path,
        spec=_spec(decision_mode=DecisionMode.HUMAN),
    )

    result = harness.reconcile_service().execute(_reconcile_request(tmp_path))

    assert result.status is OptimizeCommandStatus.AWAITING_AGENT
    assert result.details["code"] == "waiting_for_human"
    assert harness.deployment.requests == []


def test_reconcile_blocks_on_multiple_merged_candidates(
    tmp_path: Path,
) -> None:
    harness = _harness(
        tmp_path,
        spec=_spec(decision_mode=DecisionMode.HUMAN),
        reconcile=FakeReconcileGateway(
            states={"candidate-1": "MERGED", "candidate-2": "MERGED"}
        ),
    )

    result = harness.reconcile_service().execute(_reconcile_request(tmp_path))

    assert result.status is OptimizeCommandStatus.BLOCKED
    assert result.details["code"] == "ambiguous_merged_selection"
    assert harness.deployment.requests == []
    assert harness.github.closed_issues == []


# ---------------------------------------------------------------------------
# Fail-closed durable state store
# ---------------------------------------------------------------------------


def _state_file(tmp_path: Path) -> Path:
    return (
        tmp_path
        / ".foundry-optimizer"
        / "lifecycle"
        / f"{CAMPAIGN_ID}.json"
    )


def _sample_lifecycle_state() -> LifecycleState:
    return LifecycleState(
        campaign_id=CAMPAIGN_ID,
        issue_number=ISSUE,
        session_id="lifecycle-issue-7",
        updated_at="2026-07-28T12:00:00+00:00",
        selected_candidate_id="candidate-1",
        selected_pull_request_number=401,
        merge_commit=MERGE_COMMIT,
    )


def test_file_state_store_round_trips(tmp_path: Path) -> None:
    store = FileLifecycleStateStore()
    assert store.load(tmp_path, CAMPAIGN_ID) is None

    state = _sample_lifecycle_state()
    store.save(tmp_path, state)
    loaded = store.load(tmp_path, CAMPAIGN_ID)

    assert loaded == state


def test_file_state_store_fails_closed_on_invalid_json(tmp_path: Path) -> None:
    store = FileLifecycleStateStore()
    store.save(tmp_path, _sample_lifecycle_state())
    _state_file(tmp_path).write_text("{ not valid json", encoding="utf-8")

    with pytest.raises(LifecycleStateError):
        store.load(tmp_path, CAMPAIGN_ID)


def test_file_state_store_fails_closed_on_tampered_schema(
    tmp_path: Path,
) -> None:
    store = FileLifecycleStateStore()
    path = _state_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "campaign_id": CAMPAIGN_ID,
                "issue_number": -5,
                "session_id": "s",
                "updated_at": "t",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(LifecycleStateError):
        store.load(tmp_path, CAMPAIGN_ID)


def test_file_state_store_rejects_symlinked_file(tmp_path: Path) -> None:
    store = FileLifecycleStateStore()
    path = _state_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.json"
    outside.write_text(
        json.dumps(_lifecycle_state_json()), encoding="utf-8"
    )
    try:
        path.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted in this environment")

    with pytest.raises(LifecycleStateError):
        store.load(tmp_path, CAMPAIGN_ID)


def _lifecycle_state_json() -> dict[str, Any]:
    return {
        "campaign_id": CAMPAIGN_ID,
        "issue_number": ISSUE,
        "session_id": "lifecycle-issue-7",
        "updated_at": "2026-07-28T12:00:00+00:00",
    }


def test_reconcile_blocks_on_corrupt_lifecycle_state(tmp_path: Path) -> None:
    store = FileLifecycleStateStore()
    harness = _harness(
        tmp_path,
        spec=_spec(decision_mode=DecisionMode.HUMAN),
        lifecycle_store=store,
    )
    path = _state_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ tampered", encoding="utf-8")

    result = harness.reconcile_service().execute(_reconcile_request(tmp_path))

    assert result.status is OptimizeCommandStatus.BLOCKED
    assert result.details["code"] == "lifecycle_state_corrupt"


# ---------------------------------------------------------------------------
# Runner delegation
# ---------------------------------------------------------------------------


def _runner_with_lifecycle(harness: Harness) -> IssueOptimizationRunner:
    dependencies = IssueOptimizationDependencies(
        config=harness.dependencies.config,
        spec_service=None,
        spec_gateway=None,
        registration_gateway=None,
        repository=None,
        validate=None,
        build_bundle=None,
        create_draft=None,
        bind_evaluation=None,
        write_evidence=None,
        publish=None,
        state=None,
        clock=FakeClock(),
        apply_service=harness.apply_service(),
        reconcile_service=harness.reconcile_service(),
    )
    return IssueOptimizationRunner(dependencies)


def test_runner_delegates_apply_to_lifecycle_service(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    runner = _runner_with_lifecycle(harness)

    result = runner.execute(_apply_request(tmp_path))

    assert result.status is OptimizeCommandStatus.COMPLETE
    assert result.phase is OptimizePhase.APPLY
    assert result.details["code"] == "applied"


def test_runner_delegates_reconcile_to_lifecycle_service(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, spec=_spec(decision_mode=DecisionMode.HUMAN))
    runner = _runner_with_lifecycle(harness)

    result = runner.execute(_reconcile_request(tmp_path))

    assert result.status is OptimizeCommandStatus.AWAITING_AGENT
    assert result.phase is OptimizePhase.RECONCILE
    assert result.details["code"] == "waiting_for_human"


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def test_build_lifecycle_services_wires_live_deployment_and_post_deploy() -> (
    None
):
    from foundry_opt.adapters.optimization_deployment import (
        LiveDeploymentCoordinator,
    )
    from foundry_opt.adapters.post_deploy_evaluation import (
        LivePostDeployEvaluator,
    )
    from foundry_opt.optimization.lifecycle import build_lifecycle_services

    services = build_lifecycle_services(_config())
    assert isinstance(services.apply_service, CandidateApplyService)
    assert isinstance(services.reconcile_service, CandidateReconcileService)

    # The default production build wires the live Azure-OIDC deployment
    # coordinator and post-deployment evaluator; the unavailable placeholders
    # are never used on a production path.
    deps = services.reconcile_service._deps
    assert isinstance(deps.deployment, LiveDeploymentCoordinator)
    assert isinstance(deps.post_deploy, LivePostDeployEvaluator)
    assert "Unavailable" not in type(deps.deployment).__name__
    assert "Unavailable" not in type(deps.post_deploy).__name__
    # The post-deployment evaluator shares the lifecycle's campaign state store
    # (same FileCampaignStateStore instance).
    assert deps.post_deploy._state_store is deps.state
    # The deployment coordinator observes/dispatches with the exact-commit
    # ``selected_commit`` input the acceptance workflow declares.
    assert "selected_commit" in deps.deployment._dispatch_input_names


def test_build_lifecycle_services_accepts_injected_seams() -> None:
    from foundry_opt.optimization.lifecycle import build_lifecycle_services

    deployment = FakeDeploymentCoordinator()
    post_deploy = FakePostDeployEvaluator()
    services = build_lifecycle_services(
        _config(), deployment=deployment, post_deploy=post_deploy
    )

    assert services.reconcile_service._deps.deployment is deployment
    assert services.reconcile_service._deps.post_deploy is post_deploy


def test_unavailable_seams_remain_for_explicit_injection_only() -> None:
    from foundry_opt.optimization.lifecycle import (
        DeploymentLifecycleRequest,
        PostDeployRequest,
        _UnavailableDeploymentCoordinator,
        _UnavailablePostDeployEvaluator,
    )

    with pytest.raises(CapabilityUnavailableError):
        _UnavailableDeploymentCoordinator().deploy(
            DeploymentLifecycleRequest.__new__(DeploymentLifecycleRequest)
        )
    with pytest.raises(CapabilityUnavailableError):
        _UnavailablePostDeployEvaluator().evaluate(
            PostDeployRequest.__new__(PostDeployRequest)
        )
