from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess

from typer.testing import CliRunner

import foundry_opt.cli as cli
from foundry_opt.adapters.campaign_git import CampaignGit
from foundry_opt.config.models import AutomationPolicy
from foundry_opt.drafts import DraftRecord
from foundry_opt.evaluation import (
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
from foundry_opt.optimization.issues import parse_optimization_issue_request
from foundry_opt.optimization.models import (
    ApprovalGate,
    AssetKind,
    AssetProvenance,
    OptimizationSpec,
)
from foundry_opt.optimization.production import (
    build_production_steward_candidate_selection,
    build_production_steward_candidate_slate,
    build_production_steward_candidate_workers,
)
from foundry_opt.orchestration import (
    CampaignEvent,
    EventKind,
    GitStateRef,
)
from foundry_opt.orchestration.issue_intake import GitIssueEventInbox
from foundry_opt.orchestration.spec_policy import (
    OptimizationSpecPolicy,
    ResolvedSpecification,
)
from foundry_opt.orchestration.steward import (
    GitCampaignInbox,
    StewardAdvanceService,
)


ISSUE = 46


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _issue_body() -> str:
    return """### Configured target

support-agent

### Optimization goal

Improve complete policy coverage while preserving all configured safety guardrails.

### Dataset requests

- asset_id: development
  source: foundry
  role: development
  name: support-development
  version: v1
- asset_id: validation
  source: foundry
  role: validation
  name: support-validation
  version: v1

### Evaluator requests

- asset_id: task-quality
  source: builtin
  name: task-quality
  version: v1
  metrics: [quality]

### Metric policies

quality:
  direction: maximize
  threshold: 0.8
  materiality: 0.05
  hard_guardrail: false
  undefined_behavior: fail

### Allowed mutations

- system_instructions

### Candidate decision

human

### Deployment decision

human

### Confirmation

- [x] I understand this starts one bounded campaign.
"""


def _write_config(root: Path) -> None:
    path = root / ".github" / "foundry-optimizer.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        """schema_version: "1"
default_environment: acceptance
environments:
  acceptance:
    project_endpoint: https://example.services.ai.azure.com/api/projects/demo
    project_resource_id: /subscriptions/sub/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/demo/projects/demo
    allowed_models: [gpt-5.1]
    deployment_workflow:
      path: .github/workflows/deploy.yml
      trigger: manual
targets:
  support-agent:
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
        - name: support-development
          version: v1
          mode: batch
      validation:
        - name: support-validation
          version: v1
          mode: batch
    evaluators:
      - name: task-quality
        reference: task-quality
        metrics: [quality]
    validation_commands:
      - python -c "print('ok')"
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
  max_changed_candidates: 1
  transient_retries: 0
  stale_after_hours: 2
  evidence_path: .foundry-optimizer/campaigns
  allowed_issue_overrides: []
  allowed_mutations: [system_instructions]
automation_policy:
  allowed_dataset_sources: [foundry]
  allowed_evaluator_sources: [builtin]
  allow_spec_auto_approval: true
""",
        encoding="utf-8",
    )


class Resolver:
    def __init__(self, base_commit: str) -> None:
        request = parse_optimization_issue_request(
            issue_number=ISSUE,
            repository="octo-org/optimizer",
            body=_issue_body(),
        )
        self.spec = OptimizationSpec(
            issue_number=ISSUE,
            repository=request.repository,
            base_commit=base_commit,
            target=request.target,
            environment="acceptance",
            base_agent_version="12",
            goal=request.goal,
            datasets=tuple(
                AssetProvenance(
                    asset_id=asset.asset_id,
                    kind=AssetKind.DATASET,
                    source=asset.source,
                    role=asset.role,
                    name=asset.name,
                    version=asset.version,
                    created_by="foundry-existing-asset-provider",
                    approval_gate=ApprovalGate.POLICY,
                    remote_id=f"remote:{asset.asset_id}",
                )
                for asset in request.datasets
            ),
            evaluators=tuple(
                AssetProvenance(
                    asset_id=asset.asset_id,
                    kind=AssetKind.EVALUATOR,
                    source=asset.source,
                    name=asset.name,
                    version=asset.version,
                    created_by="builtin-evaluator-provider",
                    approval_gate=ApprovalGate.POLICY,
                    remote_id=f"builtin:{asset.asset_id}:v1",
                    metrics=asset.metrics,
                )
                for asset in request.evaluators
            ),
            metrics=request.metrics,
            allowed_mutations=request.allowed_mutations,
            decision_mode=request.decision_mode,
            deployment_mode=request.deployment_mode,
        )

    def resolve(self, repository_root: Path, issue_number: int):
        return ResolvedSpecification(
            spec=self.spec,
            asset_paths={
                asset.asset_id: None
                for asset in (*self.spec.datasets, *self.spec.evaluators)
            },
        )


class DraftGateway:
    def create_draft(self, request):
        return DraftRecord(
            request.agent_name,
            f"draft-{request.subject}",
            request.base_version,
            request.bundle.sha256,
            "draft",
        )


def _evaluation(subject, split, attempt):
    run = EvaluationRun(
        run_id=f"run-{subject.subject_id}",
        evaluation_id=f"eval-{subject.subject_id}",
        subject_id=subject.subject_id,
        split=DatasetSplit.DEVELOPMENT,
        agent=subject.agent,
        dataset=DatasetVersionRef("development", "v1"),
        evaluator=EvaluatorDefinitionRef("task-quality", "v1"),
        status=EvaluationStatus.COMPLETED,
        portal_url=None,
        started_at=None,
        completed_at=None,
        error=None,
    )
    return EvaluationResult(
        run=run,
        cases=(
            NormalizedCase(
                case_id="case-1",
                case_hash="case-hash",
                response_ids=("response-1",),
                scores=(
                    NormalizedCaseMetric(
                        "quality",
                        0.5,
                        0.5,
                        None,
                        Outcome.FAIL,
                    ),
                ),
                usage=Usage(1, 1),
                trajectory=None,
                error=None,
                duration_ms=1,
            ),
        ),
        metrics={
            "quality": MetricAggregate(
                "quality",
                0.5,
                0.5,
                0.5,
                0.0,
                Outcome.FAIL,
                1,
            )
        },
        usage=Usage(),
        duration_ms=1,
        errors=(),
        complete=True,
        needs_repeat=False,
        attempts=1,
    )


class Binder:
    def __call__(self, spec, assets):
        return _evaluation


class NoApprovals:
    def merged_approval(self, *args, **kwargs):
        return None


def test_actual_cli_progresses_policy_issue_into_candidate_delegation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare")
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    (root / ".gitignore").write_text(
        ".foundry-optimizer/\n",
        encoding="utf-8",
    )
    _write_config(root)
    (root / "agent").mkdir()
    source = root / "agent" / "main.py"
    source.write_text("VALUE = 'baseline'\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline repository")
    base_commit = _git(root, "rev-parse", "HEAD")
    _git(root, "remote", "add", "origin", str(remote))
    _git(root, "push", "-u", "origin", "main")
    GitIssueEventInbox(root).append(
        ISSUE,
        CampaignEvent(
            "github-created-46",
            EventKind.ISSUE_CREATED,
            1,
            datetime.now(UTC),
        ),
    )
    policy = OptimizationSpecPolicy(
        AutomationPolicy(
            allowed_dataset_sources={"foundry"},
            allowed_evaluator_sources={"builtin"},
            allow_spec_auto_approval=True,
        ),
        resolver=Resolver(base_commit),
        pinned_assets=object(),
        approvals=NoApprovals(),
    )
    workers = build_production_steward_candidate_workers(
        repository=CampaignGit(default_branch=lambda root: "main"),
        draft_gateway=DraftGateway(),
        binder_factory=lambda endpoint: Binder(),
    )
    service = StewardAdvanceService(
        inbox=GitCampaignInbox(),
        spec_policy=policy,
        candidate_workers=workers,
        candidate_slate=build_production_steward_candidate_slate(),
        candidate_selection=build_production_steward_candidate_selection(),
    )
    monkeypatch.setattr(
        cli,
        "build_steward_advance_service",
        lambda: service,
    )
    monkeypatch.chdir(root)

    result = CliRunner().invoke(
        cli.app,
        ["steward", "advance", "--issue", str(ISSUE), "--json"],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "waiting"
    assert payload["phase"] == "candidates"
    assert payload["code"] == "candidate_design_pending"
    snapshot = GitStateRef().load(root, ISSUE)
    assert snapshot is not None
    assert snapshot.state.baseline_evaluation_id == "eval-baseline"
    assert any(
        item.path == "objects/specifications/g1.json"
        for item in snapshot.objects
    )
    assert any(
        record.kind == "specialist_work_request"
        and record.payload.get("specialist")
        == "foundry-candidate-designer"
        for record in snapshot.outbox
    )
    assert _git(root, "status", "--porcelain") == ""
    assert source.read_text(encoding="utf-8") == "VALUE = 'baseline'\n"
    assert (
        _git(
            root,
            "ls-remote",
            "origin",
            f"refs/heads/foundry-opt/state/issue-{ISSUE}",
        )
    )
