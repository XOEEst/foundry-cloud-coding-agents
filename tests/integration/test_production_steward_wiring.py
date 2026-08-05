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
from foundry_opt.orchestration.handoff import (
    CloudHandoffStore,
    HandoffApplyService,
    HandoffApplyStatus,
    TrustedHandoffRequest,
)
from foundry_opt.orchestration.issue_intake import (
    GitIssueEventInbox,
    IssueEventIntake,
    TrustedEventContext,
)
from foundry_opt.orchestration.spec_policy import (
    OptimizationSpecPolicy,
    ResolvedSpecification,
)
from foundry_opt.orchestration.steward import (
    GitCampaignInbox,
    StewardAdvanceService,
)


ISSUE = 46
LIVE_COPILOT_ENVIRONMENT = {
    "FOUNDRY_OPT_COPILOT_GIT_PROXY": "1",
    "GITHUB_ACTIONS": "true",
    "GITHUB_REPOSITORY": "microsoft-foundry/luffy-test-agents-repo",
    "COPILOT_AGENT_SOURCE_ENVIRONMENT": "production",
    "COPILOT_AGENT_START_TIME_SEC": "1785872107",
    "COPILOT_AGENT_TIMEOUT_MIN": "59",
    "COPILOT_AGENT_SESSION_ID": (
        "11111111-2222-4333-8444-555555555555"
    ),
    "GITHUB_AGENT_BRANCH_NAME": "copilot/steward-issue-46",
    "GITHUB_AGENT_ACTOR": "copilot-swe-agent[bot]",
}


def _set_live_copilot_environment(monkeypatch) -> None:
    for name in (
        "COPILOT_CLI",
        "GITHUB_COPILOT_API_TOKEN",
        "GITHUB_COPILOT_ACTION_DOWNLOAD_URL",
        "GITHUB_COPILOT_LOG_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in LIVE_COPILOT_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)


def _set_normal_github_actions_environment(monkeypatch) -> None:
    for name in (
        "COPILOT_AGENT_SESSION_ID",
        "COPILOT_CLI",
        "GITHUB_COPILOT_ACTION_DOWNLOAD_URL",
        "GITHUB_COPILOT_LOG_ID",
        *LIVE_COPILOT_ENVIRONMENT,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo-org/optimizer")
    monkeypatch.setenv("FOUNDRY_OPT_COPILOT_GIT_PROXY", "1")


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


class NoAssignments:
    def has_live_lease(self, issue_number: int) -> bool:
        return False

    def assign(self, issue_number: int, idempotency_key: str) -> bool:
        return True


class NoProjection:
    def project(self, issue_number: int) -> None:
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


def test_production_issue_created_advances_through_state_handoff(
    monkeypatch,
    tmp_path: Path,
    copilot_git_proxy,
) -> None:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare")
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "core.longpaths", "true")
    _write_config(root)
    (root / "agent").mkdir()
    (root / "agent" / "main.py").write_text(
        "VALUE = 'baseline'\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline repository")
    base_commit = _git(root, "rev-parse", "HEAD")
    _git(root, "remote", "add", "origin", str(remote))
    _git(root, "push", "-u", "origin", "main")
    _git(root, "checkout", "-b", "copilot/steward-issue-46")
    _git(root, "push", "-u", "origin", "copilot/steward-issue-46")
    GitIssueEventInbox(root).append(
        ISSUE,
        CampaignEvent(
            "github-created-46",
            EventKind.ISSUE_CREATED,
            1,
            datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
        ),
    )
    proxy = copilot_git_proxy.install(
        root,
        remote,
        acknowledgement="unrelated",
    )
    _set_live_copilot_environment(monkeypatch)
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
    service = StewardAdvanceService(
        inbox=GitCampaignInbox(),
        spec_policy=policy,
        handoffs=CloudHandoffStore(),
    )

    delegated = service.advance(
        __import__(
            "foundry_opt.orchestration.steward",
            fromlist=["StewardAdvanceRequest"],
        ).StewardAdvanceRequest(root, ISSUE)
    )

    assert delegated.status.value == "waiting"
    assert delegated.disposition == "delegate"
    assert delegated.code == "state_handoff_created"
    assert delegated.phase == "baseline"
    head = _git(root, "rev-parse", "HEAD")
    assert proxy.real_revision(
        "refs/heads/copilot/steward-issue-46"
    ) == head
    path = _git(
        root,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        f"{head}^1",
        head,
    )
    blob = _git(root, "ls-tree", head, "--", path).split()[2]
    assert path.startswith(
        ".foundry-optimizer/handoffs/steward/issue-46/"
    )
    handoff_content = subprocess.run(
        ("git", "show", f"{head}:{path}"),
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    assert LIVE_COPILOT_ENVIRONMENT[
        "COPILOT_AGENT_SESSION_ID"
    ].encode() not in handoff_content
    proxy.disable()
    _set_normal_github_actions_environment(monkeypatch)
    applied = HandoffApplyService().apply(
        TrustedHandoffRequest(
            repository_root=root,
            repository="octo-org/optimizer",
            repository_id=123,
            pull_request_number=90,
            author_login="copilot-swe-agent[bot]",
            base_repository="octo-org/optimizer",
            base_ref="main",
            base_revision=base_commit,
            head_repository="octo-org/optimizer",
            head_ref="copilot/steward-issue-46",
            head_revision=head,
            handoff_path=path,
            handoff_blob=blob,
        )
    )

    assert applied.status is HandoffApplyStatus.APPLIED
    assert applied.snapshot is not None
    assert applied.snapshot.state.phase.value == "baseline"
    assert any(
        item.path == "objects/specifications/g1.json"
        for item in applied.snapshot.objects
    )
    resumed = StewardAdvanceService(
        inbox=GitCampaignInbox(),
        spec_policy=policy,
    ).advance(
        __import__(
            "foundry_opt.orchestration.steward",
            fromlist=["StewardAdvanceRequest"],
        ).StewardAdvanceRequest(root, ISSUE)
    )
    assert resumed.status.value == "waiting"
    assert resumed.phase == "baseline"


def test_production_cli_resolves_trusted_issue_without_github_api(
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
    config = root / ".github" / "foundry-optimizer.yaml"
    config.write_text(
        config.read_text(encoding="utf-8")
        .replace(
            "allowed_dataset_sources: [foundry]",
            "allowed_dataset_sources: [repository]",
        ),
        encoding="utf-8",
    )
    (root / "agent").mkdir()
    (root / "agent" / "main.py").write_text(
        "VALUE = 'baseline'\n",
        encoding="utf-8",
    )
    data = root / "data"
    data.mkdir()
    (data / "development.jsonl").write_text(
        '{"query":"hello","expected":"safe"}\n',
        encoding="utf-8",
    )
    (data / "validation.jsonl").write_text(
        '{"query":"refund","expected":"cite policy"}\n',
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline repository")
    remote_main_commit = _git(root, "rev-parse", "HEAD")
    _git(root, "remote", "add", "origin", str(remote))
    _git(root, "push", "-u", "origin", "main")
    _git(
        root,
        f"--git-dir={remote}",
        "symbolic-ref",
        "HEAD",
        "refs/heads/main",
    )
    _git(root, "checkout", "-b", "copilot/issue-46")
    (root / "copilot-plan.md").write_text(
        "initial Copilot plan\n",
        encoding="utf-8",
    )
    _git(root, "add", "copilot-plan.md")
    _git(root, "commit", "-m", "Initial plan")
    session_commit = _git(root, "rev-parse", "HEAD")
    body = _issue_body().replace(
        """- asset_id: development
  source: foundry
  role: development
  name: support-development
  version: v1
- asset_id: validation
  source: foundry
  role: validation
  name: support-validation
  version: v1
""",
        """- asset_id: development
  source: repository
  role: development
  path: data/development.jsonl
  approval_gate: human
- asset_id: validation
  source: repository
  role: validation
  path: data/validation.jsonl
""",
    )
    IssueEventIntake(
        GitIssueEventInbox(root),
        NoAssignments(),
        NoProjection(),
    ).ingest(
        {
            "action": "opened",
            "repository": {
                "id": 123,
                "full_name": "octo-org/optimizer",
            },
            "issue": {
                "number": ISSUE,
                "state": "open",
                "updated_at": "2026-08-03T12:00:00Z",
                "title": "[Optimize] Improve support quality",
                "body": (
                    "customer: never-persist-this-private-preamble\n\n"
                    + body
                ),
            },
        },
        TrustedEventContext(
            event_name="issues",
            delivery_id="production-created-46",
            repository="octo-org/optimizer",
            repository_id=123,
        ),
    )

    original_run = subprocess.run
    commands: list[tuple[str, ...]] = []

    def reject_github_api(arguments, *args, **kwargs):
        command = tuple(arguments)
        commands.append(command)
        if command and command[0] == "gh":
            raise AssertionError("production steward called GitHub API")
        return original_run(arguments, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", reject_github_api)
    monkeypatch.chdir(root)

    result = CliRunner().invoke(
        cli.app,
        ["steward", "advance", "--issue", str(ISSUE), "--json"],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "advanced"
    assert payload["phase"] == "awaiting_spec_approval"
    assert payload["revision"]
    assert not any(command[0] == "gh" for command in commands)
    snapshot = GitStateRef().load(root, ISSUE)
    assert snapshot is not None
    assert snapshot.state.spec_sha256
    specification = next(
        item
        for item in snapshot.objects
        if item.path == "objects/specifications/g1.json"
    )
    assert json.loads(specification.content)["spec"]["base_commit"] == (
        remote_main_commit
    )
    assert _git(root, "branch", "--show-current") == "copilot/issue-46"
    assert _git(root, "rev-parse", "HEAD") == session_commit
    created_event = next(
        event
        for event in snapshot.inbox
        if event.kind is EventKind.ISSUE_CREATED
    )
    created_issue = GitIssueEventInbox(root).issue_content(
        ISSUE,
        str(created_event.payload["issue_sha256"]),
    )
    assert "never-persist-this-private-preamble" not in created_issue.body
    assert any(
        item.path == "objects/specifications/g1.json"
        for item in snapshot.objects
    )
    assert any(
        record.kind == "specialist_work_request"
        and record.payload.get("specialist")
        == "foundry-optimization-planner"
        for record in snapshot.outbox
    )

    edited_body = body.replace(
        (
            "Improve complete policy coverage while preserving all "
            "configured safety guardrails."
        ),
        (
            "Improve edited policy coverage while preserving every "
            "configured safety guardrail."
        ),
    )
    IssueEventIntake(
        GitIssueEventInbox(root),
        NoAssignments(),
        NoProjection(),
    ).ingest(
        {
            "action": "edited",
            "repository": {
                "id": 123,
                "full_name": "octo-org/optimizer",
            },
            "issue": {
                "number": ISSUE,
                "state": "open",
                "updated_at": "2026-08-03T12:05:00Z",
                "title": "[Optimize] Improve edited support quality",
                "body": edited_body,
            },
        },
        TrustedEventContext(
            event_name="issues",
            delivery_id="production-edited-46",
            repository="octo-org/optimizer",
            repository_id=123,
        ),
    )

    edited_result = CliRunner().invoke(
        cli.app,
        ["steward", "advance", "--issue", str(ISSUE), "--json"],
    )

    assert edited_result.exit_code == 0, edited_result.stdout
    edited_payload = json.loads(edited_result.stdout)
    assert edited_payload["phase"] == "awaiting_spec_approval"
    edited_snapshot = GitStateRef().load(root, ISSUE)
    assert edited_snapshot is not None
    assert edited_snapshot.state.generation == 2
    specification = next(
        item
        for item in edited_snapshot.objects
        if item.path == "objects/specifications/g2.json"
    )
    specification_document = json.loads(specification.content)
    assert specification_document["spec"]["goal"] == (
        "Improve edited policy coverage while preserving every "
        "configured safety guardrail."
    )
    assert not any(command[0] == "gh" for command in commands)

    assert edited_snapshot.state.spec_head_commit is not None
    _git(
        root,
        "push",
        "origin",
        f"{edited_snapshot.state.spec_head_commit}:refs/heads/main",
    )
    GitIssueEventInbox(root).append(
        ISSUE,
        CampaignEvent(
            "trusted-spec-merged-46",
            EventKind.SPEC_PR_MERGED,
            2,
            datetime(2026, 8, 3, 12, 7, tzinfo=UTC),
            payload={
                "head_commit": edited_snapshot.state.spec_head_commit,
                "merge_commit": edited_snapshot.state.spec_head_commit,
                "pull_request_number": 146,
                "spec_sha256": edited_snapshot.state.spec_sha256,
            },
        ),
    )

    approval_result = CliRunner().invoke(
        cli.app,
        ["steward", "advance", "--issue", str(ISSUE), "--json"],
    )

    assert approval_result.exit_code == 1, approval_result.stdout
    approval_payload = json.loads(approval_result.stdout)
    assert approval_payload["code"] == "candidate_workers_unavailable"
    approved_snapshot = GitStateRef().load(root, ISSUE)
    assert approved_snapshot is not None
    assert approved_snapshot.state.phase.value == "baseline"
    assert any(
        event.kind is EventKind.SPEC_HUMAN_APPROVED
        and event.generation == 2
        for event in approved_snapshot.inbox
    )
    assert not any(command[0] == "gh" for command in commands)

    invalid_body = edited_body.replace(
        "### Confirmation",
        """### Private rows

customer: confidential-example

### Confirmation""",
    )
    IssueEventIntake(
        GitIssueEventInbox(root),
        NoAssignments(),
        NoProjection(),
    ).ingest(
        {
            "action": "edited",
            "repository": {
                "id": 123,
                "full_name": "octo-org/optimizer",
            },
            "issue": {
                "number": ISSUE,
                "state": "open",
                "updated_at": "2026-08-03T12:10:00Z",
                "title": "[Optimize] Invalid private input",
                "body": invalid_body,
            },
        },
        TrustedEventContext(
            event_name="issues",
            delivery_id="production-invalid-46",
            repository="octo-org/optimizer",
            repository_id=123,
        ),
    )

    invalid_result = CliRunner().invoke(
        cli.app,
        ["steward", "advance", "--issue", str(ISSUE), "--json"],
    )

    assert invalid_result.exit_code == 1, invalid_result.stdout
    invalid_payload = json.loads(invalid_result.stdout)
    assert invalid_payload["status"] == "blocked"
    assert invalid_payload["phase"] == "blocked"
    invalid_snapshot = GitStateRef().load(root, ISSUE)
    assert invalid_snapshot is not None
    assert invalid_snapshot.state.generation == 3
    assert invalid_snapshot.state.block_reason == "invalid_specification"
    assert not any(
        item.path == "objects/specifications/g3.json"
        for item in invalid_snapshot.objects
    )
    assert not any(
        record.kind == "specialist_work_request"
        and record.generation == 3
        for record in invalid_snapshot.outbox
    )
    inbox_revision = _git(
        root,
        "ls-remote",
        "origin",
        f"refs/heads/foundry-opt/inbox/issue-{ISSUE}",
    ).split()[0]
    inbox_paths = _git(
        root,
        "ls-tree",
        "-r",
        "--name-only",
        inbox_revision,
    ).splitlines()
    assert len(
        [path for path in inbox_paths if path.startswith("issues/")]
    ) == 2
    assert not any(command[0] == "gh" for command in commands)

    for action, delivery, title, state, occurred_at in (
        (
            "edited",
            "production-declassified-46",
            "No longer an optimization",
            "open",
            "2026-08-03T12:15:00Z",
        ),
        (
            "closed",
            "production-closed-46",
            "No longer an optimization",
            "closed",
            "2026-08-03T12:20:00Z",
        ),
    ):
        IssueEventIntake(
            GitIssueEventInbox(root),
            NoAssignments(),
            NoProjection(),
        ).ingest(
            {
                "action": action,
                "repository": {
                    "id": 123,
                    "full_name": "octo-org/optimizer",
                },
                "issue": {
                    "number": ISSUE,
                    "state": state,
                    "updated_at": occurred_at,
                    "title": title,
                    "body": "customer: never-persist-this-private-row",
                },
            },
            TrustedEventContext(
                event_name="issues",
                delivery_id=delivery,
                repository="octo-org/optimizer",
                repository_id=123,
            ),
        )
        terminal_result = CliRunner().invoke(
            cli.app,
            ["steward", "advance", "--issue", str(ISSUE), "--json"],
        )
        assert terminal_result.exit_code == 1, terminal_result.stdout
        assert json.loads(terminal_result.stdout)["status"] == "blocked"

    _git(root, "fetch", "origin", "main")
    _git(root, "reset", "--hard", "FETCH_HEAD")
    reopened_body = edited_body.replace(
        (
            "Improve edited policy coverage while preserving every "
            "configured safety guardrail."
        ),
        (
            "Improve reopened policy coverage while preserving every "
            "configured safety guardrail."
        ),
    )
    IssueEventIntake(
        GitIssueEventInbox(root),
        NoAssignments(),
        NoProjection(),
    ).ingest(
        {
            "action": "reopened",
            "repository": {
                "id": 123,
                "full_name": "octo-org/optimizer",
            },
            "issue": {
                "number": ISSUE,
                "state": "open",
                "updated_at": "2026-08-03T12:25:00Z",
                "title": "[Optimize] Reopened support quality",
                "body": reopened_body,
            },
        },
        TrustedEventContext(
            event_name="issues",
            delivery_id="production-reopened-46",
            repository="octo-org/optimizer",
            repository_id=123,
        ),
    )

    reopened_result = CliRunner().invoke(
        cli.app,
        ["steward", "advance", "--issue", str(ISSUE), "--json"],
    )

    assert reopened_result.exit_code == 0, reopened_result.stdout
    assert json.loads(reopened_result.stdout)["phase"] == (
        "awaiting_spec_approval"
    )
    reopened_snapshot = GitStateRef().load(root, ISSUE)
    assert reopened_snapshot is not None
    assert reopened_snapshot.state.generation == 4
    reopened_specification = next(
        item
        for item in reopened_snapshot.objects
        if item.path == "objects/specifications/g4.json"
    )
    assert json.loads(reopened_specification.content)["spec"]["goal"] == (
        "Improve reopened policy coverage while preserving every "
        "configured safety guardrail."
    )
    assert not any(command[0] == "gh" for command in commands)
