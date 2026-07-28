"""End-to-end optimization lifecycle integration test with fake live adapters.

This drives the *real* :class:`~foundry_opt.optimization.runner.IssueOptimizationRunner`
and the *real* APPLY/RECONCILE lifecycle services
(:class:`~foundry_opt.optimization.lifecycle.CandidateApplyService`,
:class:`~foundry_opt.optimization.lifecycle.CandidateReconcileService`) through
a full optimization: ``RUN`` establishes the baseline and adaptive candidates,
publication finalizes the campaign, ``APPLY`` publishes the selected candidate
pull request, a maintainer merges it (human decision mode), and ``RECONCILE``
resumes to observe the manual deployment, confirm a retained post-deployment
improvement, and close the optimization issue.

Every network seam is a fake: the Foundry draft/evaluation/registration seams,
the GitHub candidate/reconcile gateways, and the *live* deployment coordinator
and post-deployment evaluator (faithful stand-ins for the Azure-OIDC adapters
that :func:`~foundry_opt.optimization.lifecycle.build_lifecycle_services` wires
in production). No real Azure, Foundry, ``gh``, or ``git`` access occurs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from foundry_opt.campaign.models import PatchArtifact
from foundry_opt.campaign.protocols import (
    CampaignLock,
    CampaignWorktree,
    PinnedRepository,
)
from foundry_opt.campaign.state import FileCampaignStateStore
from foundry_opt.campaign.worktrees import contained_worktree_root
from foundry_opt.config.models import (
    AutomationPolicy,
    MetricPolicy,
    MutationClass,
    OptimizerConfig,
)
from foundry_opt.deployment import DeploymentTrigger, DeploymentWorkflow
from foundry_opt.drafts import DraftRecord
from foundry_opt.evaluation import (
    AgentVersionRef,
    DatasetSplit,
    DatasetVersionRef,
    EvaluationResult,
    EvaluationRun,
    EvaluationStatus,
    EvaluatorDefinitionRef,
    MetricAggregate,
    NormalizedCase,
    NormalizedCaseMetric,
    Outcome,
    Usage,
)
from foundry_opt.evidence import EvidenceManifest
from foundry_opt.github_workflow.models import (
    AppliedPatch,
    ArtifactInspection,
    GitHubCapabilities,
    GitHubPermissionReport,
    PullRequestReference,
    RepositoryState,
)
from foundry_opt.optimization.assets import AssetIdentity
from foundry_opt.optimization.commands import (
    OptimizeCommandRequest,
    OptimizeCommandStatus,
    OptimizePhase,
)
from foundry_opt.optimization.lifecycle import (
    CandidateApplyService,
    CandidateReconcileService,
    DeploymentOutcome,
    DeploymentOutcomeStatus,
    LifecycleDependencies,
    MemoryLifecycleStateStore,
    PostDeployOutcome,
    PostDeployStatus,
)
from foundry_opt.optimization.models import (
    ApprovalGate,
    AssetKind,
    AssetProvenance,
    DecisionMode,
    DeploymentMode,
    OptimizationSpec,
)
from foundry_opt.optimization.runner import (
    IssueOptimizationDependencies,
    IssueOptimizationRunner,
    SpecApprovalResult,
)
from foundry_opt.optimization.specification import (
    SpecServiceStatus,
    provenance_file_path,
    spec_file_path,
)
from foundry_opt.packaging import (
    BundleArtifact,
    ValidationReport,
    ValidationResult,
)


ISSUE = 7
CAMPAIGN_ID = "issue-7"
REPOSITORY = "octo-org/optimizer"
BASE_COMMIT = "b" * 40
APPROVAL_COMMIT = "a" * 40
RESULT_TREE = "d" * 40
MERGE_COMMIT = "e" * 40
MERGE_TREE = "f" * 40
DEFAULT_BRANCH = "main"
GOAL = (
    "Improve the support agent's answer coverage while preserving the "
    "advisory safety boundary on every candidate."
)

_METRIC_VALUES = {
    ("baseline", "development"): 0.80,
    ("baseline", "validation"): 0.80,
    ("candidate-1", "development"): 0.90,
    ("candidate-1", "validation"): 0.88,
    ("candidate-2", "development"): 0.84,
    ("candidate-2", "validation"): 0.84,
}


# ---------------------------------------------------------------------------
# Config + spec
# ---------------------------------------------------------------------------


def _config(policy: AutomationPolicy | None = None) -> OptimizerConfig:
    document = {
        "schema_version": "1",
        "default_environment": "acceptance",
        "environments": {
            "acceptance": {
                "project_endpoint": (
                    "https://example.services.ai.azure.com/api/projects/demo"
                ),
                "project_resource_id": (
                    "/subscriptions/sub/resourceGroups/rg/providers/"
                    "Microsoft.CognitiveServices/accounts/foundry/projects/demo"
                ),
                "allowed_models": ["gpt-5.1"],
                "deployment_workflow": {
                    "path": ".github/workflows/deploy.yml",
                    "trigger": "manual",
                },
            }
        },
        "targets": {
            "support_agent": {
                "environment": "acceptance",
                "source_paths": ["agent"],
                "edit_paths": ["agent"],
                "entry_point": "agent/main.py",
                "base_agent_version": "12",
                "package": {"include": ["agent/**"], "exclude": []},
                "datasets": {
                    "development": [
                        {"name": "dev", "version": "v1", "mode": "batch"}
                    ],
                    "validation": [
                        {"name": "held-out", "version": "v1", "mode": "batch"}
                    ],
                },
                "evaluators": [
                    {
                        "name": "quality",
                        "reference": "quality-evaluator",
                        "metrics": ["quality"],
                    }
                ],
                "validation_commands": ["uv run pytest -q"],
                "metrics": {
                    "quality": {
                        "direction": "maximize",
                        "threshold": 0.8,
                        "materiality": 0.05,
                        "hard_guardrail": False,
                        "undefined_behavior": "fail",
                    }
                },
                "allowed_mutations": ["system_instructions"],
            }
        },
        "campaign": {
            "deadline_minutes": 50,
            "candidate_cutoff_minutes": 40,
            "max_changed_candidates": 2,
            "transient_retries": 1,
            "stale_after_hours": 2,
            "evidence_path": ".foundry-optimizer/campaigns",
            "allowed_issue_overrides": [],
            "allowed_mutations": ["system_instructions"],
        },
    }
    config = OptimizerConfig.model_validate(document)
    if policy is not None:
        config = config.model_copy(update={"automation_policy": policy})
    return config


def _spec() -> OptimizationSpec:
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
                asset_id="dataset-dev",
                kind=AssetKind.DATASET,
                source="foundry",
                role="development",
                name="dev-dataset",
                version="1",
                created_by="foundry-existing-asset-provider",
                approval_gate=ApprovalGate.POLICY,
                remote_id="foundry-dataset-dev",
            ),
            AssetProvenance(
                asset_id="dataset-val",
                kind=AssetKind.DATASET,
                source="foundry",
                role="validation",
                name="val-dataset",
                version="1",
                created_by="foundry-existing-asset-provider",
                approval_gate=ApprovalGate.POLICY,
                remote_id="foundry-dataset-val",
            ),
        ),
        evaluators=(
            AssetProvenance(
                asset_id="evaluator-quality",
                kind=AssetKind.EVALUATOR,
                source="builtin",
                name="quality",
                version="1",
                created_by="builtin-evaluator-provider",
                approval_gate=ApprovalGate.POLICY,
                remote_id="builtin:quality:1",
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
        decision_mode=DecisionMode.HUMAN,
        deployment_mode=DeploymentMode.HUMAN,
    )


def _write_spec_bundle(root: Path, spec: OptimizationSpec) -> None:
    spec_path = root / spec_file_path(spec.issue_number)
    provenance_path = root / provenance_file_path(spec.issue_number)
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    document = json.loads(spec.canonical_json)
    spec_path.write_text(
        yaml.safe_dump(document, sort_keys=True, default_flow_style=False),
        encoding="utf-8",
    )

    def _entry(provenance: AssetProvenance) -> dict[str, object]:
        return {
            "asset_id": provenance.asset_id,
            "path": None,
            "source": provenance.source,
        }

    provenance_document = {
        "base_commit": spec.base_commit,
        "datasets": [_entry(item) for item in spec.datasets],
        "evaluators": [_entry(item) for item in spec.evaluators],
        "issue_number": spec.issue_number,
        "schema_version": 1,
        "spec_sha256": spec.sha256,
    }
    provenance_path.write_text(
        json.dumps(provenance_document, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Evaluation result + evidence helpers
# ---------------------------------------------------------------------------


def _result(
    subject_id: str,
    split: DatasetSplit,
    *,
    agent: AgentVersionRef | None = None,
) -> EvaluationResult:
    value = _METRIC_VALUES[(subject_id, split.value)]
    run = EvaluationRun(
        run_id=f"run-{subject_id}-{split.value}",
        evaluation_id=f"eval-{subject_id}-{split.value}",
        subject_id=subject_id,
        split=split,
        agent=agent
        or AgentVersionRef("support_agent", f"draft-{subject_id}", "1"),
        dataset=DatasetVersionRef(f"dataset-{split.value}", "1"),
        evaluator=EvaluatorDefinitionRef("quality", "1"),
        status=EvaluationStatus.COMPLETED,
        portal_url=None,
        started_at=None,
        completed_at=None,
        error=None,
    )
    case = NormalizedCase(
        case_id="case-1",
        case_hash="case-hash",
        response_ids=(f"response-{subject_id}-{split.value}",),
        scores=(
            NormalizedCaseMetric("quality", value, value, None, Outcome.PASS),
        ),
        usage=Usage(),
        trajectory=None,
        error=None,
        duration_ms=1,
    )
    return EvaluationResult(
        run=run,
        cases=(case,),
        metrics={
            "quality": MetricAggregate(
                "quality", value, value, value, 0.0, Outcome.PASS, 1
            )
        },
        usage=Usage(),
        duration_ms=1,
        errors=(),
        complete=True,
        needs_repeat=False,
        attempts=1,
    )


class RecordingEvaluator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def __call__(self, subject, split, attempt) -> EvaluationResult:
        self.calls.append((subject.subject_id, split.value, attempt))
        return _result(subject.subject_id, split, agent=subject.agent)


def _bundle(path: Path) -> BundleArtifact:
    return BundleArtifact(
        path=path,
        sha256="c" * 64,
        included_files=("agent/instructions.md",),
        excluded_files=(),
        byte_size=1,
        manifest_path=path.with_suffix(".manifest.json"),
    )


def _evidence_writer():
    """Fake evidence writer producing the APPLY/RECONCILE-compatible schema.

    The exact-patch applier and the reconcile lineage both read this file, so
    it records the per-candidate ``patch_hash``/``draft_id``/``result_tree``
    lineage and the eligible pareto set that those steps verify.
    """

    def write_evidence(request) -> EvidenceManifest:
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        patch_hashes = request.patch_hashes or {}
        candidates = [
            {
                "subject_id": candidate.run.subject_id,
                "patch_hash": patch_hashes.get(candidate.run.subject_id),
                "agent": {"draft_id": f"draft-{candidate.run.subject_id}"},
                "result_tree": RESULT_TREE,
            }
            for candidate in request.candidates
            if candidate.run.subject_id in patch_hashes
        ]
        document = {
            "campaign_id": request.campaign_id,
            "candidates": candidates,
            "pareto": {
                "eligible_ids": [
                    candidate["subject_id"] for candidate in candidates
                ]
            },
        }
        serialized = json.dumps(document, sort_keys=True).encode("utf-8")
        request.output_path.write_bytes(serialized)
        return EvidenceManifest(
            path=request.output_path,
            sha256=hashlib.sha256(serialized).hexdigest(),
            byte_count=len(serialized),
            evaluation_ids=(),
            run_ids=(),
            goal_sha256=hashlib.sha256(
                request.goal.encode("utf-8")
            ).hexdigest(),
            spec_sha256=request.spec_sha256,
        )

    return write_evidence


def _validate(passed: bool = True):
    def validate(path: Path) -> ValidationReport:
        return ValidationReport(
            (
                ValidationResult(
                    ("test",), path, passed, 0 if passed else 1, "", ""
                ),
            ),
            discovered=True,
        )

    return validate


# ---------------------------------------------------------------------------
# Runner Foundry-seam fakes
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 26, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, *, minutes: int) -> None:
        self.current += timedelta(minutes=minutes)


class FakeRunnerRepository:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.worktree_base = BASE_COMMIT

    def pin_default_branch(self, repository_root: Path) -> PinnedRepository:
        return PinnedRepository(DEFAULT_BRANCH, BASE_COMMIT)

    def acquire_lock(self, **kwargs: object) -> CampaignLock:
        return CampaignLock(str(kwargs["campaign_id"]))

    def release_lock(self, **kwargs: object) -> None:
        return None

    def _worktree(self, campaign_id: str, candidate_id: str) -> CampaignWorktree:
        path = contained_worktree_root(self.root, campaign_id) / candidate_id
        return CampaignWorktree(
            candidate_id,
            path,
            f"foundry-opt/{campaign_id}/{candidate_id}",
            self.worktree_base,
        )

    def create_worktree(
        self, repository_root, campaign_id, candidate_id, base_commit
    ) -> CampaignWorktree:
        worktree = self._worktree(campaign_id, candidate_id)
        worktree.path.mkdir(parents=True, exist_ok=True)
        return worktree

    def reconcile_worktree(
        self, repository_root, campaign_id, candidate_id, base_commit
    ) -> CampaignWorktree:
        import shutil

        worktree = self._worktree(campaign_id, candidate_id)
        if worktree.path.is_dir():
            shutil.rmtree(worktree.path, ignore_errors=True)
        worktree.path.mkdir(parents=True, exist_ok=True)
        return worktree

    def open_worktree(
        self, repository_root, campaign_id, candidate_id, base_commit
    ) -> CampaignWorktree:
        worktree = self._worktree(campaign_id, candidate_id)
        if not worktree.path.is_dir():
            raise ValueError("worktree does not exist")
        return worktree

    def changed_paths(self, worktree: CampaignWorktree) -> tuple[Path, ...]:
        return (Path("agent/instructions.md"),)

    def reset_worktree(self, worktree: CampaignWorktree) -> None:
        return None

    def commit_worktree(self, worktree: CampaignWorktree, message: str) -> str:
        return "c" * 40

    def export_patch(
        self, repository_root, campaign_id, worktree, result_commit
    ) -> PatchArtifact:
        return PatchArtifact(
            candidate_id=worktree.candidate_id,
            path=Path(
                f".foundry-optimizer/campaigns/{campaign_id}/"
                f"{worktree.candidate_id}.patch"
            ),
            sha256=hashlib.sha256(
                worktree.candidate_id.encode()
            ).hexdigest(),
            base_commit=BASE_COMMIT,
            result_commit=result_commit,
        )

    def cleanup_worktree(self, repository_root, worktree) -> None:
        if worktree.path.is_dir():
            import shutil

            shutil.rmtree(worktree.path, ignore_errors=True)


class FakeSpecGateway:
    def verify_spec_approval(
        self, repository_root, *, issue_number, spec, spec_sha256, base_commit
    ) -> SpecApprovalResult:
        return SpecApprovalResult(
            approved=True,
            default_branch=DEFAULT_BRANCH,
            approval_commit=APPROVAL_COMMIT,
        )


class FakeRegistrationGateway:
    def register(self, *, kind, name, version, content) -> AssetIdentity:
        return AssetIdentity(
            remote_id=f"registered:{name}:{version}",
            name=name,
            version=version,
            content_sha256=None,
        )


@dataclass
class FakeSpecServiceResult:
    status: SpecServiceStatus
    issue_number: int
    spec_sha256: str | None = None
    pull_request: object | None = None
    blockers: tuple[str, ...] = ()
    failures: tuple[object, ...] = ()


class FakeSpecService:
    def prepare_specification(self, repository_root: Path, issue_number: int):
        return FakeSpecServiceResult(
            status=SpecServiceStatus.COMPLETE,
            issue_number=issue_number,
            spec_sha256=_spec().sha256,
        )


class FakePublisher:
    def __init__(self) -> None:
        self.inputs: list[Any] = []

    def publish(self, inputs):
        from foundry_opt.campaign.state import FinalizedPublication

        self.inputs.append(inputs)
        issue_numbers = {
            candidate_id: 200 + index
            for index, candidate_id in enumerate(
                inputs.report.pareto_candidate_ids
            )
        }
        return FinalizedPublication(
            campaign_pull_request_number=42,
            campaign_pull_request_url=(
                f"https://github.com/{REPOSITORY}/pull/42"
            ),
            candidate_issue_numbers=issue_numbers,
        )


def _runner_dependencies(
    root: Path,
    clock: FakeClock,
    publisher: FakePublisher,
    evaluator: RecordingEvaluator,
    state_store: FileCampaignStateStore,
) -> IssueOptimizationDependencies:
    return IssueOptimizationDependencies(
        config=_config(),
        spec_service=FakeSpecService(),
        spec_gateway=FakeSpecGateway(),
        registration_gateway=FakeRegistrationGateway(),
        repository=FakeRunnerRepository(root),
        validate=_validate(True),
        build_bundle=lambda root, output: _bundle(output),
        create_draft=(
            lambda target, subject_id, key, bundle: DraftRecord(
                target, f"draft-{subject_id}", 1, bundle.sha256, "ready"
            )
        ),
        bind_evaluation=lambda spec, assets: evaluator,
        write_evidence=_evidence_writer(),
        publish=publisher,
        state=state_store,
        clock=clock,
    )


def _idea(root: Path, idea_id: str, parents: tuple[str, ...] = ()) -> Path:
    path = root / f"{idea_id}.json"
    path.write_text(
        json.dumps(
            {
                "idea_id": idea_id,
                "mutation_class": "system_instructions",
                "parent_idea_ids": list(parents),
                "required_opt_ins": [],
            }
        ),
        encoding="utf-8",
    )
    return path


def _request(root: Path, phase: OptimizePhase, **kwargs) -> OptimizeCommandRequest:
    return OptimizeCommandRequest(
        repository_root=root,
        issue_number=ISSUE,
        phase=phase,
        candidate_id=kwargs.get("candidate_id"),
        idea_file=kwargs.get("idea_file"),
    )


# ---------------------------------------------------------------------------
# Lifecycle GitHub-seam fakes
# ---------------------------------------------------------------------------


class FakeIssueGateway:
    def __init__(self) -> None:
        self._prs: dict[str, PullRequestReference] = {}
        self._counter = 300
        self.comments: list[tuple[int, str]] = []
        self.updated_bodies: list[tuple[int, str]] = []
        self.closed_issues: list[tuple[int, str]] = []
        self.closed_prs: list[tuple[int, str]] = []

    def verify_permissions(self, required) -> GitHubPermissionReport:
        return GitHubPermissionReport(
            GitHubCapabilities.CANDIDATE_PUBLICATION & required
        )

    def repository_state(self, repository_root: Path) -> RepositoryState:
        return RepositoryState(REPOSITORY, DEFAULT_BRANCH, BASE_COMMIT)

    def find_candidate_pull_request(
        self, repository_root: Path, head_branch: str
    ) -> PullRequestReference | None:
        return self._prs.get(head_branch)

    def create_candidate_pull_request(
        self,
        repository_root,
        *,
        base_branch,
        head_branch,
        commit_sha,
        title,
        body,
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

    def comment_issue(self, repository_root, issue_number, body) -> None:
        self.comments.append((issue_number, body))

    def update_issue_body(self, repository_root, issue_number, body) -> None:
        self.updated_bodies.append((issue_number, body))

    def close_issue(self, repository_root, issue_number, comment) -> None:
        self.closed_issues.append((issue_number, comment))

    def close_pull_request(
        self, repository_root, pull_request_number, comment
    ) -> None:
        self.closed_prs.append((pull_request_number, comment))


class FakeLifecyclePatchApplier:
    """Exact-patch applier aligned to the runner-produced campaign state."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self.applied: list[Any] = []
        self.patch_shas: dict[str, str] = {}

    def inspect_artifact(
        self, repository_root: Path, path: Path
    ) -> ArtifactInspection:
        posix = path.as_posix()
        for candidate_id, sha in self.patch_shas.items():
            if posix.endswith(f"{candidate_id}.patch"):
                content = f"patch::{candidate_id}".encode("utf-8")
                return ArtifactInspection(
                    path=path,
                    sha256=sha,
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
            commit_sha="c" * 40,
            changed_paths=(Path("agent/main.py"),),
            exact=True,
            substantive_repair=False,
            tree_sha=RESULT_TREE,
        )

    def resolve_tree(self, repository_root: Path, commit: str) -> str | None:
        return RESULT_TREE

    def resolve_branch_commit(
        self, repository_root: Path, branch: str
    ) -> str | None:
        return "c" * 40

    def restore_after_publication_failure(
        self, repository_root, base_commit, base_branch
    ) -> None:
        return None


class FakeReconcileGateway:
    def __init__(self, patch_shas: dict[str, str]) -> None:
        self._patch_shas = patch_shas
        self._states: dict[str, str] = {}
        self.merged: list[tuple[int, str, str]] = []
        self.dispatched: list[tuple[int, str]] = []
        self._pr_numbers = {"candidate-1": 401, "candidate-2": 402}

    def mark_merged(self, candidate_id: str) -> None:
        self._states[candidate_id] = "MERGED"

    def verify_permissions(self, required) -> GitHubPermissionReport:
        granted = (
            GitHubCapabilities.MERGE | GitHubCapabilities.DEPLOY_DISPATCH
        )
        return GitHubPermissionReport(granted & required)

    def branch_protection_allows(
        self, repository_root, pull_request, actor
    ) -> bool:
        return True

    def merge_pull_request(
        self, repository_root, pull_request_number, expected_head_commit, actor
    ) -> None:
        self.merged.append((pull_request_number, expected_head_commit, actor))

    def dispatch_deployment(
        self, repository_root, pull_request_number, expected_head_commit
    ) -> None:
        self.dispatched.append((pull_request_number, expected_head_commit))

    def locate_candidate_pull_request(
        self, repository_root, campaign_id, candidate_id
    ) -> PullRequestReference | None:
        if candidate_id not in self._pr_numbers:
            return None
        number = self._pr_numbers[candidate_id]
        return PullRequestReference(
            number=number,
            url=f"https://github.com/{REPOSITORY}/pull/{number}",
            head_branch=f"foundry-opt/{CAMPAIGN_ID}/{candidate_id}/lifecycle",
            head_commit="c" * 40,
            draft=False,
            body="candidate",
            base_branch=DEFAULT_BRANCH,
            state=self._states.get(candidate_id, "OPEN"),
        )

    def candidate_checks(
        self, repository_root, pull_request
    ) -> dict[str, str]:
        return {
            "foundry-opt/spec": "success",
            "foundry-opt/exact-patch": "success",
        }

    def resolve_merge_commit(
        self, repository_root, pull_request_number
    ) -> str:
        return MERGE_COMMIT

    def resolve_tree(self, repository_root, commit) -> str | None:
        return MERGE_TREE


class FakeLifecycleRepository:
    def pin_default_branch(self, repository_root: Path) -> PinnedRepository:
        return PinnedRepository(DEFAULT_BRANCH, BASE_COMMIT)


class FakeSpecApproval:
    @dataclass
    class _Report:
        approved: bool
        default_branch: str | None
        approval_commit: str | None
        reason: str | None

    def verify_spec_approval(
        self, repository_root, *, issue_number, spec, spec_sha256, base_commit
    ):
        return self._Report(
            approved=True,
            default_branch=DEFAULT_BRANCH,
            approval_commit=APPROVAL_COMMIT,
            reason=None,
        )


class FakeLifecycleClock:
    def now(self) -> datetime:
        return datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fake live adapters (Azure-OIDC stand-ins, no network)
# ---------------------------------------------------------------------------


class FakeDeploymentCoordinator:
    """Faithful stand-in for the live deployment coordinator.

    Observes the manual deployment (never dispatches in human deployment mode),
    records each request so the test can assert exact-commit routing and
    dispatch-idempotency, and returns a verified published version.
    """

    def __init__(self, version: int = 13) -> None:
        self._version = version
        self.requests: list[Any] = []

    def deploy(self, request: Any) -> DeploymentOutcome:
        self.requests.append(request)
        return DeploymentOutcome(
            status=DeploymentOutcomeStatus.VERIFIED,
            version=self._version,
            run_url=f"https://github.com/{REPOSITORY}/actions/runs/900",
            portal_url=(
                "https://ai.azure.com/projects/demo/agents/support_agent"
                f"/versions/{self._version}"
            ),
        )


class FakePostDeployEvaluator:
    """Faithful stand-in for the live post-deployment evaluator."""

    def __init__(self, metrics: dict[str, float] | None = None) -> None:
        self._metrics = metrics or {"quality": 0.88, "safety": 0.99}
        self.requests: list[Any] = []

    def evaluate(self, request: Any) -> PostDeployOutcome:
        self.requests.append(request)
        return PostDeployOutcome(
            status=PostDeployStatus.RETAINED_IMPROVEMENT,
            metrics=self._metrics,
        )


# ---------------------------------------------------------------------------
# The end-to-end test
# ---------------------------------------------------------------------------


def _run_campaign(root: Path, deps) -> Any:
    """Drive RUN -> candidate request/submit x2 -> finalize/publish."""
    runner = IssueOptimizationRunner(deps)
    runner.execute(_request(root, OptimizePhase.RUN))

    runner.execute(_request(root, OptimizePhase.CANDIDATE_REQUEST))
    runner.execute(
        _request(
            root,
            OptimizePhase.CANDIDATE_SUBMIT,
            candidate_id="candidate-1",
            idea_file=_idea(root, "idea-1"),
        )
    )

    runner.execute(_request(root, OptimizePhase.CANDIDATE_REQUEST))
    runner.execute(
        _request(
            root,
            OptimizePhase.CANDIDATE_SUBMIT,
            candidate_id="candidate-2",
            idea_file=_idea(root, "idea-2", parents=("idea-1",)),
        )
    )

    return runner.execute(_request(root, OptimizePhase.RUN))


def test_full_lifecycle_run_to_close_with_fake_live_adapters(
    tmp_path: Path,
) -> None:
    root = tmp_path
    spec = _spec()
    _write_spec_bundle(root, spec)
    state_store = FileCampaignStateStore()
    clock = FakeClock()
    publisher = FakePublisher()
    evaluator = RecordingEvaluator()
    runner_deps = _runner_dependencies(
        root, clock, publisher, evaluator, state_store
    )

    # --- RUN -> candidate(s) -> publication --------------------------------
    final = _run_campaign(root, runner_deps)
    assert final.status is OptimizeCommandStatus.COMPLETE
    assert len(publisher.inputs) == 1
    finalized_state = state_store.load(root, CAMPAIGN_ID)
    assert finalized_state is not None
    assert finalized_state.finalized is not None
    assert "candidate-1" in finalized_state.pareto_candidate_ids

    # Align the lifecycle patch applier + reconcile gateway to the exact
    # candidate patches the campaign persisted.
    patch_shas = {
        candidate.candidate_id: candidate.artifact.patch.sha256
        for candidate in finalized_state.candidates
        if candidate.artifact is not None
    }

    github = FakeIssueGateway()
    reconcile = FakeReconcileGateway(patch_shas)
    applier = FakeLifecyclePatchApplier(root)
    applier.patch_shas = patch_shas
    deployment = FakeDeploymentCoordinator(version=17)
    post_deploy = FakePostDeployEvaluator(
        metrics={"quality": 0.9, "safety": 0.99}
    )
    lifecycle_state = MemoryLifecycleStateStore()
    workflow = DeploymentWorkflow(
        path=Path(".github/workflows/deploy.yml"),
        trigger=DeploymentTrigger.MANUAL,
        exists=True,
        name="Deploy",
    )
    deps = LifecycleDependencies(
        config=_config(),
        state=state_store,
        lifecycle_state=lifecycle_state,
        github_gateway_factory=lambda root: github,
        reconcile_gateway_factory=lambda root: reconcile,
        patch_applier=applier,
        repository=FakeLifecycleRepository(),
        spec_approval=FakeSpecApproval(),
        deployment=deployment,
        post_deploy=post_deploy,
        clock=FakeLifecycleClock(),
        detect_workflow=lambda root: workflow,
    )

    # --- APPLY the selected candidate --------------------------------------
    apply_service = CandidateApplyService(deps)
    apply_result = apply_service.execute(
        OptimizeCommandRequest(
            repository_root=root,
            issue_number=ISSUE,
            phase=OptimizePhase.APPLY,
            candidate_id="candidate-1",
        )
    )
    assert apply_result.status is OptimizeCommandStatus.COMPLETE
    assert apply_result.details["code"] == "applied"
    assert len(applier.applied) == 1

    # --- RECONCILE (human decision): first run awaits the maintainer merge --
    reconcile_service = CandidateReconcileService(deps)
    reconcile_request = OptimizeCommandRequest(
        repository_root=root,
        issue_number=ISSUE,
        phase=OptimizePhase.RECONCILE,
    )
    waiting = reconcile_service.execute(reconcile_request)
    assert waiting.status is OptimizeCommandStatus.AWAITING_AGENT
    assert waiting.details["code"] == "waiting_for_human"
    assert deployment.requests == []
    assert github.closed_issues == []

    # --- Maintainer merges the chosen candidate PR -------------------------
    reconcile.mark_merged("candidate-1")

    # --- RECONCILE (resume): observe deployment, retain, close -------------
    closed = reconcile_service.execute(reconcile_request)
    assert closed.status is OptimizeCommandStatus.COMPLETE
    assert closed.details["code"] == "reconciled"
    assert closed.details["candidate_id"] == "candidate-1"

    # The manual deployment was observed against the exact merge commit and
    # never optimizer-dispatched (human deployment mode).
    assert len(deployment.requests) == 1
    coordinator_request = deployment.requests[0]
    assert coordinator_request.dispatch is False
    assert coordinator_request.merge_commit == MERGE_COMMIT
    assert reconcile.dispatched == []

    # The published version and the retained post-deployment metrics both flow
    # into the closed issue's details and updated body.
    assert closed.details["deployment_version"] == 17
    assert closed.details["post_deploy_metrics"] == {
        "quality": 0.9,
        "safety": 0.99,
    }
    body = github.updated_bodies[0][1]
    assert "17" in body and "quality" in body

    # The post-deployment evaluator saw the concrete published version.
    assert post_deploy.requests[0].deployment_version == 17

    # The parent optimization issue is closed exactly once.
    closed_issue_numbers = [n for n, _ in github.closed_issues]
    assert ISSUE in closed_issue_numbers
