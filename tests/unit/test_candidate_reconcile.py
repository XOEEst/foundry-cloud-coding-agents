from __future__ import annotations

from pathlib import Path

from foundry_opt.config.models import (
    AutomationPolicy,
    MetricPolicy,
    MutationClass,
)
from foundry_opt.github_workflow import (
    CandidateReconcileEntry,
    CandidateReconcileRequest,
    CandidateReconcileStatus,
    GitHubCapabilities,
    GitHubPermissionReport,
    PullRequestReference,
    reconcile_candidates,
)
from foundry_opt.optimization import (
    ApprovalGate,
    AssetKind,
    AssetProvenance,
    DecisionMode,
    OptimizationSpec,
    approve_optimization_spec,
)


def _spec() -> OptimizationSpec:
    return OptimizationSpec(
        issue_number=42,
        repository="octo-org/agents",
        base_commit="a" * 40,
        target="support-agent",
        environment="acceptance",
        base_agent_version="2",
        goal=(
            "Improve complete policy coverage while preserving the advisory "
            "safety boundary."
        ),
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
                source="synthetic",
                role="validation",
                name="support-validation",
                version="v1",
                content_sha256="b" * 64,
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
        allowed_mutations=frozenset(
            {MutationClass.SYSTEM_INSTRUCTIONS}
        ),
    )


def _pull_request(number: int = 55) -> PullRequestReference:
    return PullRequestReference(
        number=number,
        url=f"https://github.com/octo-org/agents/pull/{number}",
        head_branch=f"foundry-opt/candidate-{number}",
        head_commit="d" * 40,
        draft=False,
        body="candidate",
        base_branch="main",
        state="OPEN",
    )


class Gateway:
    def __init__(
        self,
        *,
        granted: GitHubCapabilities = (
            GitHubCapabilities.MERGE
            | GitHubCapabilities.DEPLOY_DISPATCH
        ),
        branch_allowed: bool = True,
    ) -> None:
        self.granted = granted
        self.branch_allowed = branch_allowed
        self.merged: list[tuple[int, str, str]] = []
        self.dispatched: list[tuple[int, str]] = []

    def verify_permissions(self, required):
        return GitHubPermissionReport(self.granted & required)

    def branch_protection_allows(self, repository_root, pull_request, actor):
        return self.branch_allowed

    def merge_pull_request(
        self,
        repository_root,
        pull_request_number,
        expected_head_commit,
        actor,
    ):
        self.merged.append(
            (pull_request_number, expected_head_commit, actor)
        )

    def dispatch_deployment(
        self,
        repository_root,
        pull_request_number,
        expected_head_commit,
    ):
        self.dispatched.append(
            (pull_request_number, expected_head_commit)
        )


def _autopilot_spec() -> OptimizationSpec:
    spec = _spec()
    return OptimizationSpec.model_validate(
        {
            **spec.model_dump(),
            "decision_mode": DecisionMode.AUTOPILOT_IF_ALLOWED,
        }
    )


def _policy(*, deploy: bool = False) -> AutomationPolicy:
    return AutomationPolicy(
        allowed_dataset_sources={"foundry", "synthetic", "trace"},
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


def _entry(
    *,
    candidate_id: str = "candidate-a",
    number: int = 55,
    checks: dict[str, str] | None = None,
    eligible: bool = True,
) -> CandidateReconcileEntry:
    return CandidateReconcileEntry(
        candidate_id=candidate_id,
        pull_request=_pull_request(number),
        eligible=eligible,
        checks=checks
        or {
            "foundry-opt/spec": "success",
            "foundry-opt/exact-patch": "success",
        },
    )


def test_human_decision_mode_never_requests_merge_permission() -> None:
    approval = approve_optimization_spec(_spec(), approval_commit="c" * 40)
    gateway = Gateway(granted=GitHubCapabilities.NONE)

    result = reconcile_candidates(
        CandidateReconcileRequest(
            repository_root=Path("."),
            approval=approval,
            automation_policy=_policy(),
            ranked_candidates=(_entry(),),
        ),
        gateway,
    )

    assert result.status is CandidateReconcileStatus.WAITING_FOR_HUMAN
    assert gateway.merged == []


def test_autopilot_merges_highest_ranked_eligible_checked_candidate() -> None:
    approval = approve_optimization_spec(
        _autopilot_spec(),
        approval_commit="c" * 40,
    )
    gateway = Gateway()

    result = reconcile_candidates(
        CandidateReconcileRequest(
            repository_root=Path("."),
            approval=approval,
            automation_policy=_policy(),
            ranked_candidates=(
                _entry(
                    candidate_id="candidate-b",
                    number=56,
                    eligible=False,
                ),
                _entry(),
            ),
        ),
        gateway,
    )

    assert result.status is CandidateReconcileStatus.MERGED
    assert result.selected_candidate_id == "candidate-a"
    assert gateway.merged == [
        (55, "d" * 40, "foundry-opt-merge-app")
    ]


def test_autopilot_requires_all_named_checks() -> None:
    approval = approve_optimization_spec(
        _autopilot_spec(),
        approval_commit="c" * 40,
    )
    gateway = Gateway()

    result = reconcile_candidates(
        CandidateReconcileRequest(
            repository_root=Path("."),
            approval=approval,
            automation_policy=_policy(),
            ranked_candidates=(
                _entry(
                    checks={
                        "foundry-opt/spec": "success",
                        "foundry-opt/exact-patch": "failure",
                    }
                ),
            ),
        ),
        gateway,
    )

    assert result.status is CandidateReconcileStatus.BLOCKED
    assert result.reason_code == "required_checks_failed"
    assert gateway.merged == []


def test_autopilot_fails_when_branch_protection_rejects_merge_actor() -> None:
    approval = approve_optimization_spec(
        _autopilot_spec(),
        approval_commit="c" * 40,
    )
    gateway = Gateway(branch_allowed=False)

    result = reconcile_candidates(
        CandidateReconcileRequest(
            repository_root=Path("."),
            approval=approval,
            automation_policy=_policy(),
            ranked_candidates=(_entry(),),
        ),
        gateway,
    )

    assert result.status is CandidateReconcileStatus.BLOCKED
    assert result.reason_code == "branch_protection_incompatible"
    assert gateway.merged == []


def test_trace_spec_requires_human_spec_approval_before_autopilot_merge() -> None:
    spec = _autopilot_spec()
    trace = spec.datasets[0].model_copy(
        update={
            "source": "trace",
            "approval_gate": ApprovalGate.HUMAN,
        }
    )
    spec = OptimizationSpec.model_validate(
        {
            **spec.model_dump(),
            "datasets": (trace, *spec.datasets[1:]),
        }
    )
    approval = approve_optimization_spec(
        spec,
        approval_commit="c" * 40,
        approval_gate=ApprovalGate.POLICY,
    )
    gateway = Gateway()

    result = reconcile_candidates(
        CandidateReconcileRequest(
            repository_root=Path("."),
            approval=approval,
            automation_policy=_policy(),
            ranked_candidates=(_entry(),),
        ),
        gateway,
    )

    assert result.status is CandidateReconcileStatus.BLOCKED
    assert result.reason_code == "human_asset_approval_required"


def test_autopilot_dispatches_deployment_only_with_separate_capability() -> None:
    approval = approve_optimization_spec(
        _autopilot_spec(),
        approval_commit="c" * 40,
    )
    gateway = Gateway()

    result = reconcile_candidates(
        CandidateReconcileRequest(
            repository_root=Path("."),
            approval=approval,
            automation_policy=_policy(deploy=True),
            ranked_candidates=(_entry(),),
        ),
        gateway,
    )

    assert result.status is CandidateReconcileStatus.DEPLOYMENT_DISPATCHED
    assert gateway.dispatched == [(55, "d" * 40)]
