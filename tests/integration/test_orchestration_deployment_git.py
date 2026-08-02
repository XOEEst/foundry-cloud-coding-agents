from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess

from foundry_opt.deployment import DEPLOYMENT_OIDC_CLIENT_ID, DeploymentTrigger
from foundry_opt.orchestration import (
    AdvanceRequest,
    CampaignEvent,
    CampaignPhase,
    CandidateBinding,
    EventKind,
    GitStateRef,
    OptimizationCampaign,
    OutboxRecord,
)
from foundry_opt.orchestration.deployment import (
    DeploymentOrchestrationRequest,
    DeploymentOrchestrationService,
    DeploymentOrchestrationStatus,
    DeploymentPlan,
    DeploymentDispatchClaimRecorder,
    DeploymentDispatchClaimStatus,
    DeploymentSelectionSnapshot,
    DeploymentWorkflowIdentity,
    DeploymentWorkflowResult,
    DeploymentWorkflowResultRecorder,
    DeploymentWorkflowRunState,
    PostDeploymentEvaluationResult,
    PostDeploymentEvaluationStatus,
    LedgerDeploymentPublicationVerifier,
    deployment_workflow_intent,
    deployment_workflow_result_from_event,
)
from foundry_opt.orchestration.issue_intake import (
    DeploymentWorkflowEventRouter,
    GitIssueEventInbox,
    TrustedEventContext,
)
from foundry_opt.orchestration.deployment_bridge import (
    record_deployment_publication_file,
)


NOW = datetime(2026, 7, 31, 22, 0, tzinfo=UTC)
ISSUE = 44
SPEC = "a" * 64
BASE = "b" * 40
HEAD = "c" * 40
MERGE = "d" * 40
TREE = "e" * 40
PATCH = "1" * 64
BUNDLE = "2" * 64
EVIDENCE = "3" * 64


def _run(arguments: tuple[str, ...], cwd: Path) -> str:
    return subprocess.run(
        arguments,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    repository = tmp_path / "repository"
    _run(("git", "init", "--bare", str(origin)), tmp_path)
    _run(("git", "init", "-b", "main", str(repository)), tmp_path)
    _run(("git", "config", "user.name", "Deployment Test"), repository)
    _run(
        ("git", "config", "user.email", "deployment@example.invalid"),
        repository,
    )
    (repository / "README.md").write_text("deployment\n", encoding="utf-8")
    _run(("git", "add", "."), repository)
    _run(("git", "commit", "-m", "baseline"), repository)
    _run(("git", "remote", "add", "origin", str(origin)), repository)
    _run(("git", "push", "-u", "origin", "main"), repository)
    return repository


def _binding() -> CandidateBinding:
    return CandidateBinding(
        issue_number=ISSUE,
        generation=1,
        spec_sha256=SPEC,
        base_commit=BASE,
        candidate_id="candidate-1",
        draft_id="draft-candidate-1",
        evidence_sha256=EVIDENCE,
        patch_sha256=PATCH,
        bundle_sha256=BUNDLE,
        tree_sha=TREE,
        allowed_paths=(Path("agent"),),
        changed_paths=(Path("agent/instructions.md"),),
    )


def _plan() -> DeploymentPlan:
    return DeploymentPlan(
        issue_number=ISSUE,
        generation=1,
        repository="octo-org/agents",
        repository_id=1234,
        workflow=DeploymentWorkflowIdentity(
            repository="octo-org/agents",
            repository_id=1234,
            path=Path(".github/workflows/deploy.yml"),
            ref="refs/heads/main",
            trigger=DeploymentTrigger.MANUAL,
            workflow_id=77,
            actor="github-actions[bot]",
            deployment_client_id=DEPLOYMENT_OIDC_CLIENT_ID,
        ),
        allowed_merge_actors=("maintainer",),
        required_checks=("exact-candidate", "tests"),
        max_attempts=2,
        timeout_seconds=1800,
        campaign_pull_request_number=100,
    )


class Resolver:
    def resolve(self, request, state):
        return _plan()


class SelectionReader:
    def read(self, request, binding, plan):
        return DeploymentSelectionSnapshot(
            binding=binding,
            candidate_pull_request_number=91,
            candidate_issue_number=84,
            head_commit=HEAD,
            merge_commit=MERGE,
            merge_tree_sha=TREE,
            merge_actor="maintainer",
            checks={
                "exact-candidate": "success",
                "tests": "success",
            },
        )


class Evaluation:
    def __init__(self) -> None:
        self.runs = 0
        self.result = None

    def reconcile(self, intent):
        return self.result

    def run(self, intent):
        self.runs += 1
        self.result = PostDeploymentEvaluationResult(
            result_id=f"{intent.effect_id}-result",
            intent=intent,
            status=PostDeploymentEvaluationStatus.RETAINED_IMPROVEMENT,
            baseline_metrics={"quality": 0.7},
            selected_draft_metrics={"quality": 0.9},
            deployed_metrics={"quality": 0.9},
        )
        return self.result


class Assignments:
    def __init__(self) -> None:
        self.values = []

    def assign(self, issue_number, idempotency_key):
        self.values.append((issue_number, idempotency_key))
        return True

    def has_live_lease(self, issue_number):
        return False


class Projection:
    def __init__(self) -> None:
        self.values = []

    def project(self, issue_number):
        self.values.append(issue_number)


def _seed(repository: Path) -> None:
    binding = _binding()
    events = (
        CampaignEvent("created", EventKind.ISSUE_CREATED, 1, NOW),
        CampaignEvent(
            "approved",
            EventKind.SPEC_POLICY_APPROVED,
            1,
            NOW,
            {"spec_sha256": SPEC},
        ),
        CampaignEvent(
            "baseline",
            EventKind.BASELINE_COMPLETED,
            1,
            NOW,
            {"evaluation_id": "eval-baseline"},
        ),
        CampaignEvent(
            "candidate",
            EventKind.CANDIDATE_EVALUATED,
            1,
            NOW,
            {
                "candidate_id": "candidate-1",
                "eligible": True,
                "evidence_sha256": EVIDENCE,
            },
        ),
        CampaignEvent(
            "workers-complete",
            EventKind.CANDIDATE_WORKERS_COMPLETED,
            1,
            NOW,
            {
                "attempted_count": 1,
                "eligible_count": 1,
                "stop_reason": "budget_complete",
            },
        ),
        CampaignEvent(
            "slate",
            EventKind.SLATE_PUBLISHED,
            1,
            NOW,
        ),
        CampaignEvent(
            "merged",
            EventKind.CANDIDATE_MERGED,
            1,
            NOW,
            {"candidate_id": "candidate-1", "merge_commit": MERGE},
        ),
    )
    state = OptimizationCampaign().advance(
        AdvanceRequest(ISSUE, None, events)
    ).state
    marker = (
        "<!-- foundry-opt:candidate-pr:issue-44:g1:candidate-1:"
        f"{binding.binding_sha256[:20]} -->"
    )
    planned = OutboxRecord(
        "applier-1-candidate-1-binding",
        "applier_worker_issue_planned",
        1,
        state.sequence,
        {
            "allowed_paths": ["agent"],
            "attestation_path": "objects/candidates/g1-candidate-1.json",
            "base_commit": BASE,
            "binding_sha256": binding.binding_sha256,
            "bundle_sha256": BUNDLE,
            "candidate_id": "candidate-1",
            "changed_paths": ["agent/instructions.md"],
            "draft_id": "draft-candidate-1",
            "effect_id": "applier-1-candidate-1-binding",
            "effect_kind": "applier_worker_issue",
            "evidence_path": f"objects/evidence/{EVIDENCE}.json",
            "evidence_sha256": EVIDENCE,
            "issue_number": ISSUE,
            "marker": marker,
            "patch_path": f"objects/patches/{PATCH}.patch",
            "patch_sha256": PATCH,
            "required_checks": ["exact-candidate", "tests"],
            "spec_sha256": SPEC,
            "specialist": "foundry-candidate-applier",
            "tree_sha": TREE,
            "work_kind": "apply_exact_candidate",
        },
    )
    succeeded = OutboxRecord(
        f"{planned.record_id}-succeeded",
        "applier_worker_issue_succeeded",
        1,
        state.sequence,
        {
            "assigned": True,
            "binding_sha256": binding.binding_sha256,
            "candidate_id": "candidate-1",
            "created": True,
            "effect_id": planned.record_id,
            "issue_number": ISSUE,
            "result_id": "applier-result-candidate-1",
            "worker_issue_number": 84,
        },
    )
    selection = OutboxRecord(
        "selection-1-candidate-1-91",
        "candidate_selection_recorded",
        1,
        state.sequence,
        {
            "binding_sha256": binding.binding_sha256,
            "candidate_id": "candidate-1",
            "head_commit": HEAD,
            "issue_number": ISSUE,
            "merge_commit": MERGE,
            "pull_request_number": 91,
            "tree_sha": TREE,
            "worker_issue_number": 84,
        },
    )
    GitStateRef().commit(
        repository,
        issue_number=ISSUE,
        expected_revision=None,
        state=state,
        inbox=events,
        outbox=(planned, succeeded, selection),
    )
    GitIssueEventInbox(repository).append(ISSUE, events[0])


def test_real_git_resume_keeps_deployment_and_evaluation_exactly_once(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _seed(repository)
    first = DeploymentOrchestrationService(
        ledger=GitStateRef(),
        resolver=Resolver(),
        selection_reader=SelectionReader(),
        clock=lambda: NOW,
    ).advance(DeploymentOrchestrationRequest(repository, ISSUE))
    assert first.status is DeploymentOrchestrationStatus.PLANNED
    first_intent = deployment_workflow_intent(
        next(
            record
            for record in first.snapshot.outbox
            if record.kind == "deployment_workflow_planned"
        )
    )
    assert (
        DeploymentDispatchClaimRecorder(
            GitStateRef(), repository, ISSUE
        ).claim(first_intent)
        is DeploymentDispatchClaimStatus.CLAIMED
    )
    assert (
        DeploymentDispatchClaimRecorder(
            GitStateRef(), repository, ISSUE
        ).claim(first_intent)
        is DeploymentDispatchClaimStatus.ALREADY_CLAIMED
    )

    resumed = DeploymentOrchestrationService(
        ledger=GitStateRef(),
        resolver=Resolver(),
        selection_reader=SelectionReader(),
        clock=lambda: NOW,
    ).advance(DeploymentOrchestrationRequest(repository, ISSUE))
    assert resumed.status is DeploymentOrchestrationStatus.WAITING
    assert (
        sum(
            record.kind == "deployment_workflow_planned"
            for record in resumed.snapshot.outbox
        )
        == 1
    )

    planned = next(
        record
        for record in resumed.snapshot.outbox
        if record.kind == "deployment_workflow_planned"
    )
    intent = deployment_workflow_intent(planned)
    assignments = Assignments()
    projection = Projection()
    recorded = DeploymentWorkflowEventRouter(
        repository,
        GitIssueEventInbox(repository),
        assignments,
        projection,
    ).ingest(
        {
            "action": "completed",
            "repository": {
                "full_name": "octo-org/agents",
                "id": 1234,
            },
            "workflow_run": {
                "id": 991,
                "display_title": intent.effect_id,
                "workflow_id": 77,
                "path": (
                    ".github/workflows/deploy.yml@refs/heads/main"
                ),
                "head_sha": MERGE,
                "status": "completed",
                "conclusion": "success",
                "html_url": (
                    "https://github.com/octo-org/agents/actions/runs/991"
                ),
                "actor": {"login": "github-actions[bot]"},
                "updated_at": "2026-07-31T22:00:00Z",
            },
        },
        TrustedEventContext(
            "workflow_run",
            "delivery-991",
            "octo-org/agents",
            1234,
        ),
    )
    assert recorded is not None
    assert assignments.values
    assert projection.values == [ISSUE]
    workflow_result = GitIssueEventInbox(repository).events(ISSUE)[-1]
    routed_snapshot = GitStateRef().load(repository, ISSUE)
    assert routed_snapshot is not None
    state = OptimizationCampaign().advance(
        AdvanceRequest(
            ISSUE,
            routed_snapshot.state,
            (workflow_result,),
        )
    ).state
    observed = GitStateRef().commit(
        repository,
        issue_number=ISSUE,
        expected_revision=routed_snapshot.revision,
        state=state,
        inbox=(workflow_result,),
    )
    assert observed.state.phase is CampaignPhase.DEPLOYMENT
    workflow_run = deployment_workflow_result_from_event(workflow_result)
    result_path = repository / "deployment-result.json"
    result_path.write_text(
        json.dumps(
            {
                "bundle_sha256": BUNDLE,
                "deployment_version": 13,
                "effect_id": intent.effect_id,
                "lineage_sha256": intent.lineage_sha256,
                "merge_commit": MERGE,
                "metadata_sha256": "4" * 64,
                "portal_url": (
                    "https://ai.azure.com/projects/demo/agents/"
                    "support/versions/13"
                ),
                "run_actor": "github-actions[bot]",
                "run_id": workflow_run.run_id,
                "run_url": workflow_run.run_url,
                "source_sha256": BUNDLE,
                "tree_sha": TREE,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    record_deployment_publication_file(
        repository,
        ISSUE,
        Path("deployment-result.json"),
    )

    evaluation = Evaluation()
    retention = DeploymentOrchestrationService(
        ledger=GitStateRef(),
        resolver=Resolver(),
        selection_reader=SelectionReader(),
        publication_verifier=LedgerDeploymentPublicationVerifier(
            GitStateRef()
        ),
        evaluation_effects=evaluation,
        clock=lambda: NOW,
    ).advance(DeploymentOrchestrationRequest(repository, ISSUE))
    assert retention.snapshot.state.phase is CampaignPhase.RETENTION

    completed = DeploymentOrchestrationService(
        ledger=GitStateRef(),
        resolver=Resolver(),
        selection_reader=SelectionReader(),
        publication_verifier=LedgerDeploymentPublicationVerifier(
            GitStateRef()
        ),
        evaluation_effects=evaluation,
        clock=lambda: NOW,
    ).advance(DeploymentOrchestrationRequest(repository, ISSUE))
    assert completed.status is DeploymentOrchestrationStatus.COMPLETE
    assert completed.snapshot.state.phase is CampaignPhase.COMPLETED
    assert evaluation.runs == 1
    serialized = json.dumps(
        [
            dict(record.payload)
            for record in completed.snapshot.outbox
        ],
        sort_keys=True,
    )
    assert "raw_response" not in serialized
    assert "dataset_row" not in serialized


def test_workflow_router_selects_only_latest_retry_attempt(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _seed(repository)
    planned = DeploymentOrchestrationService(
        ledger=GitStateRef(),
        resolver=Resolver(),
        selection_reader=SelectionReader(),
        clock=lambda: NOW,
    ).advance(DeploymentOrchestrationRequest(repository, ISSUE))
    inbox = GitIssueEventInbox(repository)
    router = DeploymentWorkflowEventRouter(
        repository,
        inbox,
        Assignments(),
        Projection(),
    )
    failed = router.ingest(
        {
            "action": "completed",
            "repository": {
                "full_name": "octo-org/agents",
                "id": 1234,
            },
            "workflow_run": {
                "id": 991,
                "display_title": deployment_workflow_intent(
                    next(
                        record
                        for record in planned.snapshot.outbox
                        if record.kind == "deployment_workflow_planned"
                    )
                ).effect_id,
                "workflow_id": 77,
                "path": (
                    ".github/workflows/deploy.yml@refs/heads/main"
                ),
                "head_sha": MERGE,
                "status": "completed",
                "conclusion": "failure",
                "html_url": (
                    "https://github.com/octo-org/agents/actions/runs/991"
                ),
                "actor": {"login": "github-actions[bot]"},
                "updated_at": "2026-07-31T22:00:00Z",
            },
        },
        TrustedEventContext(
            "workflow_run",
            "delivery-failed-991",
            "octo-org/agents",
            1234,
        ),
    )
    assert failed is not None
    failure_event = inbox.events(ISSUE)[-1]
    routed_snapshot = GitStateRef().load(repository, ISSUE)
    assert routed_snapshot is not None
    failure_state = OptimizationCampaign().advance(
        AdvanceRequest(
            ISSUE,
            routed_snapshot.state,
            (failure_event,),
        )
    ).state
    failed_snapshot = GitStateRef().commit(
        repository,
        issue_number=ISSUE,
        expected_revision=routed_snapshot.revision,
        state=failure_state,
        inbox=(failure_event,),
    )
    retry = DeploymentOrchestrationService(
        ledger=GitStateRef(),
        resolver=Resolver(),
        selection_reader=SelectionReader(),
        clock=lambda: NOW,
    ).advance(DeploymentOrchestrationRequest(repository, ISSUE))
    assert retry.status is DeploymentOrchestrationStatus.RETRYING
    assert retry.snapshot.revision != failed_snapshot.revision
    retry_record = max(
        (
            record
            for record in retry.snapshot.outbox
            if record.kind == "deployment_workflow_planned"
        ),
        key=lambda record: int(record.payload["attempt"]),
    )
    retry_intent = deployment_workflow_intent(retry_record)
    DeploymentWorkflowResultRecorder(GitStateRef()).record(
        repository,
        ISSUE,
        DeploymentWorkflowResult(
            effect_id=retry_intent.effect_id,
            result_id="deployment-run-992",
            attempt=retry_intent.attempt,
            binding=retry_intent.binding,
            workflow=retry_intent.workflow,
            run_id=992,
            run_url=(
                "https://github.com/octo-org/agents/actions/runs/992"
            ),
            state=DeploymentWorkflowRunState.SUCCESS,
            conclusion="success",
        ),
    )

    succeeded = router.ingest(
        {
            "action": "completed",
            "repository": {
                "full_name": "octo-org/agents",
                "id": 1234,
            },
            "workflow_run": {
                "id": 992,
                "display_title": retry_intent.effect_id,
                "workflow_id": 77,
                "path": (
                    ".github/workflows/deploy.yml@refs/heads/main"
                ),
                "head_sha": MERGE,
                "status": "completed",
                "conclusion": "success",
                "html_url": (
                    "https://github.com/octo-org/agents/actions/runs/992"
                ),
                "actor": {"login": "github-actions[bot]"},
                "updated_at": "2026-07-31T22:05:00Z",
            },
        },
        TrustedEventContext(
            "workflow_run",
            "delivery-success-992",
            "octo-org/agents",
            1234,
        ),
    )

    assert succeeded is not None
    assert succeeded.event.payload["attempt"] == 2
