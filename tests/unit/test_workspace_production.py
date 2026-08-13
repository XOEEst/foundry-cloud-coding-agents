from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

import foundry_opt.orchestration.workspace_production as workspace_production
from foundry_opt.orchestration import (
    CandidateExperimentRequest,
    GitWorkspaceStore,
    InMemoryWorkspaceStore,
    OptimizationWorkspace,
    TrustedWorkspaceEventContext,
    WorkspaceAdvanceRequest,
    WorkspaceBaselineRecord,
    WorkspaceBaselinePlan,
    WorkspaceExperimentRecord,
    WorkspaceIssueStatusProjectionIntent,
    WorkspacePhase,
    WorkspacePullRequest,
    WorkspaceResult,
    WorkspaceSnapshot,
    WorkspaceSpecificationRecord,
    WorkspaceTrigger,
    WorkspaceUpdate,
    build_production_workspace,
)
from foundry_opt.orchestration.workspace_github import GhWorkspacePullRequests
from foundry_opt.orchestration.workspace_runtime import (
    PlanningWorkspacePullRequests,
)
from foundry_opt.orchestration.workspace_execution_production import (
    LiveWorkspaceExperimentAdapter,
)
from foundry_opt.orchestration.workspace import (
    WorkspaceNextAction,
    WorkspaceNextActionKind,
)
from foundry_opt.orchestration.workspace_production import (
    ProductionWorkspaceError,
    ProductionWorkspaceService,
    build_production_workspace_service,
)
from foundry_opt.adapters.commands import CommandExitError
from foundry_opt.preflight.interfaces import CommandResult


class FakeCommands:
    def __init__(self, responses: dict[tuple[str, ...], str]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
        environment: Mapping[str, str] | None = None,
        input_text: str | None = None,
        input_bytes: bytes | None = None,
    ) -> CommandResult:
        command = tuple(arguments)
        self.calls.append(command)
        if command not in self.responses:
            raise AssertionError(f"unexpected command: {command}")
        return CommandResult(0, self.responses[command], "")


class FailingCommands:
    def run(self, arguments, **kwargs):
        del kwargs
        raise CommandExitError(
            arguments,
            exit_code=1,
            stdout="",
            stderr="blocked",
        )


def test_production_builder_uses_git_state_and_gh_pull_requests(
    tmp_path: Path,
) -> None:
    subprocess.run(
        ("git", "init", "-b", "main"),
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    workspace = build_production_workspace(
        tmp_path,
        repository="octo-org/optimizer",
        base_branch="main",
        commands=FakeCommands({}),
    )

    assert isinstance(workspace, OptimizationWorkspace)
    assert isinstance(workspace._store, GitWorkspaceStore)
    assert isinstance(workspace._pull_requests, GhWorkspacePullRequests)


def test_build_production_workspace_service_uses_real_baseline_and_experiment_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    monkeypatch.chdir(repository_root)

    service = build_production_workspace_service()

    assert isinstance(service, ProductionWorkspaceService)
    assert service._experiment_runner is not None
    assert service._baseline_request_builder is not None


def test_repository_context_uses_verified_copilot_proxy_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    values = {
        "COPILOT_AGENT_SOURCE_ENVIRONMENT": "production",
        "COPILOT_AGENT_START_TIME_SEC": "1786600000",
        "COPILOT_AGENT_TIMEOUT_MIN": "59",
        "COPILOT_AGENT_SESSION_ID": "session-123456789",
        "FOUNDRY_OPT_COPILOT_GIT_PROXY": "1",
        "FOUNDRY_OPT_REPOSITORY": "octo-org/optimizer",
        "FOUNDRY_OPT_REPOSITORY_ID": "12345",
        "FOUNDRY_OPT_DEFAULT_BRANCH": "main",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    commands = FakeCommands({})
    service = ProductionWorkspaceService(commands=commands)

    context = service._repository_context(tmp_path)
    repository_id = service._repository_id(
        tmp_path,
        context.repository,
    )

    assert context.repository == "octo-org/optimizer"
    assert context.default_branch == "main"
    assert repository_id == 12345
    assert commands.calls == []


def test_copilot_proxy_continuation_uses_persisted_workspace_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    values = {
        "COPILOT_AGENT_SOURCE_ENVIRONMENT": "production",
        "COPILOT_AGENT_START_TIME_SEC": "1786600000",
        "COPILOT_AGENT_TIMEOUT_MIN": "59",
        "COPILOT_AGENT_SESSION_ID": "session-123456789",
        "FOUNDRY_OPT_COPILOT_GIT_PROXY": "1",
        "FOUNDRY_OPT_REPOSITORY": "octo-org/optimizer",
        "FOUNDRY_OPT_REPOSITORY_ID": "12345",
        "FOUNDRY_OPT_DEFAULT_BRANCH": "main",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    store = InMemoryWorkspaceStore()
    store.commit(
        expected_revision=None,
        update=WorkspaceUpdate(
            issue_number=31,
            phase=WorkspacePhase.EVALUATING,
            workspace_pull_request_number=104,
            semantic_event="baseline_completed",
            specification=_trusted_specification(),
            baseline=_trusted_baseline(),
        ),
    )
    monkeypatch.setattr(
        workspace_production,
        "GitWorkspaceStore",
        lambda root: store,
    )
    service = ProductionWorkspaceService(commands=FailingCommands())

    issue = service._issue(tmp_path, "octo-org/optimizer", 31)
    existing = service._existing_workspace_pull_request(
        tmp_path,
        "octo-org/optimizer",
        31,
    )
    workspace = build_production_workspace(
        tmp_path,
        repository="octo-org/optimizer",
        base_branch="main",
        commands=FailingCommands(),
    )

    assert issue["title"] == "[Optimize] Persisted workspace issue #31"
    assert existing == (104, "b" * 40)
    assert isinstance(
        workspace._pull_requests,
        PlanningWorkspacePullRequests,
    )


def test_actions_workspace_service_executes_live_operations_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    monkeypatch.chdir(repository_root)

    service = build_production_workspace_service(
        actions_execution=True,
    )

    assert isinstance(
        service._experiment_runner,
        LiveWorkspaceExperimentAdapter,
    )


def test_issue_created_plans_baseline_before_candidate_assignment(
    monkeypatch,
    tmp_path: Path,
) -> None:
    store = InMemoryWorkspaceStore()
    pull_request = WorkspacePullRequest(
        number=104,
        issue_number=31,
        branch="foundry-opt/workspace/issue-31",
        title=(
            "[Optimize] #31 workspace - draft, not yet selectable"
        ),
        draft=True,
        reuse_existing=True,
        base_commit="b" * 40,
    )

    class Workspace:
        def advance(self, request):
            store.commit(
                expected_revision=None,
                update=WorkspaceUpdate(
                    issue_number=31,
                    phase=WorkspacePhase.SPECIFICATION,
                    workspace_pull_request_number=104,
                    semantic_event="issue_created",
                ),
            )
            return WorkspaceResult(
                phase=WorkspacePhase.SPECIFICATION,
                workspace_pull_request=pull_request,
                planned_effect_kinds=("workspace_pr_created",),
            )

    class Resolver:
        def resolve(self, **kwargs):
            return _trusted_specification()

    class BaselineBuilder:
        def build(self, **kwargs):
            specification = kwargs["specification"]
            return WorkspaceBaselinePlan(
                request=CandidateExperimentRequest(
                    issue_number=31,
                    candidate_id="baseline",
                    patch_sha256=specification.spec_sha256,
                    bundle_sha256="c" * 64,
                    evidence_sha256="d" * 64,
                    idempotency_key="e" * 64,
                ),
                dataset_ids=("development", "validation"),
                evaluator_ids=("quality",),
                sample_count=24,
            )

    monkeypatch.setattr(
        workspace_production,
        "GitWorkspaceStore",
        lambda root: store,
    )
    monkeypatch.setattr(
        workspace_production,
        "_load_workspace_snapshot",
        lambda root, issue: store.load(issue),
    )
    monkeypatch.setattr(
        workspace_production,
        "load_config",
        lambda path: object(),
    )
    service = ProductionWorkspaceService(
        commands=FakeCommands({}),
        workspace_factory=lambda **kwargs: Workspace(),
        baseline_request_builder=BaselineBuilder(),
        specification_resolver=Resolver(),
    )
    monkeypatch.setattr(
        service,
        "_repository_context",
        lambda root: type(
            "Context",
            (),
            {
                "repository": "octo-org/optimizer",
                "default_branch": "main",
            },
        )(),
    )
    monkeypatch.setattr(
        service,
        "_issue",
        lambda root, repository, issue: {
            "title": "[Optimize] Improve quality",
            "body": "Trusted issue body.",
        },
    )
    monkeypatch.setattr(
        service,
        "_existing_workspace_pull_request",
        lambda root, repository, issue: None,
    )
    monkeypatch.setattr(
        service,
        "_default_commit",
        lambda root, branch: "b" * 40,
    )
    monkeypatch.setattr(
        service,
        "_project_workspace_result",
        lambda *args, **kwargs: None,
    )

    result = service.advance(
        WorkspaceAdvanceRequest(
            repository_root=tmp_path,
            issue_number=31,
            trigger=WorkspaceTrigger.ISSUE_CREATED,
        )
    )
    assignment = service.assign_copilot(
        repository_root=tmp_path,
        issue_number=31,
        assignment_token=None,
    )

    snapshot = store.load(31)
    assert snapshot.workspace_pull_request_number == 104
    assert snapshot.specification == _trusted_specification()
    assert snapshot.baseline.status == "pending"
    assert result.next_action.kind.value == (
        "await_trusted_actions_result"
    )
    assert assignment.assigned is False
    assert assignment.next_action == "await_trusted_actions_result"


def _trusted_specification() -> WorkspaceSpecificationRecord:
    return WorkspaceSpecificationRecord(
        status="policy_approved",
        spec_sha256="f" * 64,
        base_commit="b" * 40,
        target="support-agent",
        environment="development",
        asset_ids=("development", "validation", "quality"),
        metric_names=("quality",),
        policy_reason="repository policy approved immutable assets",
    )


def test_production_enriches_candidate_action_with_trusted_work_contract(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = SimpleNamespace(
        campaign=SimpleNamespace(max_changed_candidates=2),
        targets={
            "support-agent": SimpleNamespace(
                allowed_mutations=("system_instructions",),
                campaign_overrides=None,
            )
        },
    )
    monkeypatch.setattr(
        workspace_production,
        "load_config",
        lambda path: config,
    )
    snapshot = WorkspaceSnapshot(
        issue_number=31,
        revision="a" * 40,
        phase=WorkspacePhase.EVALUATING,
        workspace_pull_request_number=104,
        candidates=(),
        selected_patch=None,
        external_operation_ids=(),
        experiments=(),
        lineage=None,
        specification=_trusted_specification(),
        baseline=_trusted_baseline(),
    )
    result = WorkspaceResult(
        phase=WorkspacePhase.EVALUATING,
        workspace_pull_request=WorkspacePullRequest(
            number=104,
            issue_number=31,
            branch="foundry-opt/workspace/issue-31",
            title="[Optimize] #31 workspace",
            draft=True,
            reuse_existing=True,
            base_commit="b" * 40,
        ),
        planned_effect_kinds=(),
        next_action=WorkspaceNextAction(
            kind=WorkspaceNextActionKind.RUN_CANDIDATE_EXPERIMENTS,
            issue_number=31,
            workspace_pull_request_number=104,
            trigger=WorkspaceTrigger.CONTINUE,
        ),
    )

    enriched = ProductionWorkspaceService(
        commands=FakeCommands({}),
    )._with_candidate_work(tmp_path, result, snapshot)

    contract = enriched.to_dict()["next_action"]["candidate_work"]
    assert contract["target"] == "support-agent"
    assert contract["base_commit"] == "b" * 40
    assert contract["candidate_id"] == "candidate-1"
    assert contract["candidate_number"] == 1
    assert contract["candidate_limit"] == 2
    assert contract["allowed_mutations"] == ["system_instructions"]


def test_copilot_experiment_writes_revision_bound_proxy_envelope(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = SimpleNamespace(
        campaign=SimpleNamespace(max_changed_candidates=2),
        targets={
            "support-agent": SimpleNamespace(
                allowed_mutations=("system_instructions",),
                campaign_overrides=None,
            )
        },
    )
    snapshot = WorkspaceSnapshot(
        issue_number=31,
        revision="a" * 40,
        phase=WorkspacePhase.EVALUATING,
        workspace_pull_request_number=104,
        candidates=(),
        selected_patch=None,
        external_operation_ids=(),
        experiments=(),
        lineage=None,
        specification=_trusted_specification(),
        baseline=_trusted_baseline(),
    )
    monkeypatch.setattr(
        workspace_production,
        "load_config",
        lambda path: config,
    )
    monkeypatch.setattr(
        workspace_production,
        "GitWorkspaceStore",
        lambda root: SimpleNamespace(load=lambda issue: snapshot),
    )
    monkeypatch.setattr(
        workspace_production,
        "_trusted_copilot_repository_context",
        lambda: (
            SimpleNamespace(
                repository="octo-org/optimizer",
                default_branch="main",
            ),
            123,
        ),
    )
    service = ProductionWorkspaceService(
        commands=FakeCommands({}),
        experiment_runner=object(),
    )
    monkeypatch.setattr(
        service,
        "_repository_context",
        lambda root: SimpleNamespace(
            repository="octo-org/optimizer",
            default_branch="main",
        ),
    )
    monkeypatch.setattr(
        service,
        "_existing_workspace_pull_request",
        lambda root, repository, issue: (104, "b" * 40),
    )
    payload = {
        "schema_version": 3,
        "issue_number": 31,
        "target": "support-agent",
        "base_commit": "b" * 40,
        "candidate": {
            "candidate_id": "candidate-1",
            "mutation_class": "system_instructions",
            "summary": "Improve policy coverage.",
            "patch_base64": base64.b64encode(
                b"diff --git a/agent.py b/agent.py\n"
            ).decode("ascii"),
        },
    }

    result = service.execute_experiment(
        payload,
        repository_root=tmp_path,
    )

    envelope = json.loads(
        (
            tmp_path
            / ".foundry-optimizer"
            / "workspace-candidate.json"
        ).read_text(encoding="utf-8")
    )
    assert result.status == "proxy_import_required"
    assert result.recorded is False
    assert result.next_action.endswith(
        ".foundry-optimizer/workspace-candidate.json"
    )
    assert envelope == {
        "expected_revision": "a" * 40,
        "kind": "workspace_candidate_proposal",
        "manifest": payload,
        "schema_version": 1,
    }


def _trusted_baseline() -> WorkspaceBaselineRecord:
    return WorkspaceBaselineRecord(
        status="completed",
        operation_sha256="1" * 64,
        idempotency_key="2" * 64,
        bundle_sha256="3" * 64,
        evidence_sha256="4" * 64,
        dataset_ids=("development", "validation"),
        evaluator_ids=("quality",),
        split="development",
        sample_count=12,
        executor="direct_oidc",
        draft_id="baseline-draft",
        evaluation_id="baseline-evaluation",
        run_id="baseline-run",
        metrics={"quality": 0.8},
        guardrails={"safety": "pass"},
    )


def test_production_builder_wires_candidate_coordinator(
    tmp_path: Path,
) -> None:
    subprocess.run(
        ("git", "init", "-b", "main"),
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    class Selector:
        def select(self, request):
            raise AssertionError("not selected while building")

    workspace = build_production_workspace(
        tmp_path,
        repository="octo-org/optimizer",
        base_branch="main",
        commands=FakeCommands({}),
        candidate_count=2,
        selector=Selector(),
    )

    assert workspace._candidate_coordinator is not None


def test_production_advance_publishes_state_derived_issue_projection(
    monkeypatch,
    tmp_path: Path,
) -> None:
    projected: dict[str, Any] = {}
    pull_request = WorkspacePullRequest(
        number=104,
        issue_number=31,
        branch="foundry-opt/workspace/issue-31",
        title="[Optimize] #31 workspace - draft, not yet selectable",
        draft=True,
        reuse_existing=True,
        base_commit="a" * 40,
    )
    workspace_result = WorkspaceResult(
        phase=WorkspacePhase.SPECIFICATION,
        workspace_pull_request=pull_request,
        planned_effect_kinds=("workspace_pr_sync",),
        issue_status_projection_intent=(
            WorkspaceIssueStatusProjectionIntent(
                issue_number=31,
                phase=WorkspacePhase.SPECIFICATION,
                workspace_pull_request_number=104,
            )
        ),
    )

    class Workspace:
        def advance(self, request):
            return workspace_result

    class Projector:
        def project(self, intent, *, base_commit, report=None):
            projected["intent"] = intent
            projected["base_commit"] = base_commit
            projected["report"] = report

    service = ProductionWorkspaceService(
        commands=FakeCommands({}),
        workspace_factory=lambda **_: Workspace(),
        issue_projector_factory=lambda **kwargs: (
            projected.update(kwargs) or Projector()
        ),
    )
    monkeypatch.setattr(
        service,
        "_repository_context",
        lambda root: type(
            "Context",
            (),
            {
                "repository": "octo-org/optimizer",
                "default_branch": "main",
            },
        )(),
    )
    monkeypatch.setattr(
        service,
        "_issue",
        lambda root, repository, issue: {
            "title": "[Optimize] Improve policy coverage",
            "body": "Improve policy coverage.",
        },
    )
    monkeypatch.setattr(
        service,
        "_existing_workspace_pull_request",
        lambda root, repository, issue: (104, "a" * 40),
    )

    result = service.advance(
        WorkspaceAdvanceRequest(
            repository_root=tmp_path,
            issue_number=31,
        )
    )

    assert result is workspace_result
    assert projected["repository"] == "octo-org/optimizer"
    assert projected["intent"] == (
        workspace_result.issue_status_projection_intent
    )
    assert projected["base_commit"] == "a" * 40
    assert projected["report"] is None


def test_production_assignment_uses_state_workspace_pr_for_candidate_work(
    monkeypatch,
    tmp_path: Path,
) -> None:
    snapshot = WorkspaceSnapshot(
        issue_number=31,
        revision="a" * 40,
        phase=WorkspacePhase.SPECIFICATION,
        workspace_pull_request_number=104,
        candidates=(),
        selected_patch=None,
        external_operation_ids=(),
        experiments=(),
        lineage=None,
        specification=_trusted_specification(),
        baseline=_trusted_baseline(),
    )
    monkeypatch.setattr(
        workspace_production,
        "GitWorkspaceStore",
        lambda root: type(
            "Store",
            (),
            {"load": lambda self, issue: snapshot},
        )(),
    )
    assigned: dict[str, Any] = {}

    class Assigner:
        def assign(
            self,
            *,
            issue_number,
            pull_request_number,
            assignment_key,
        ):
            assigned["issue"] = issue_number
            assigned["pr"] = pull_request_number
            assigned["key"] = assignment_key
            return True

    service = ProductionWorkspaceService(
        commands=FakeCommands({}),
        copilot_assigner_factory=lambda **kwargs: (
            assigned.update(kwargs) or Assigner()
        ),
    )
    monkeypatch.setattr(
        service,
        "_repository_context",
        lambda root: type(
            "Context",
            (),
            {
                "repository": "octo-org/optimizer",
                "default_branch": "main",
            },
        )(),
    )
    monkeypatch.setattr(
        service,
        "_existing_workspace_pull_request",
        lambda root, repository, issue: (104, "b" * 40),
    )

    result = service.assign_copilot(
        repository_root=tmp_path,
        issue_number=31,
        assignment_token="assignment-token",
    )

    assert result.assigned is True
    assert result.workspace_pull_request_number == 104
    assert result.next_action == "design_candidates"
    assert assigned["issue"] == 31
    assert assigned["pr"] == 104
    assert assigned["key"] == "a" * 40
    assert assigned["assignment_token"] == "assignment-token"


def test_production_assignment_skips_human_merge_wait_without_token(
    monkeypatch,
    tmp_path: Path,
) -> None:
    snapshot = WorkspaceSnapshot(
        issue_number=31,
        revision="a" * 40,
        phase=WorkspacePhase.AWAITING_SELECTION,
        workspace_pull_request_number=104,
        candidates=(),
        selected_patch=None,
        external_operation_ids=(),
        experiments=(),
        lineage=None,
    )
    monkeypatch.setattr(
        workspace_production,
        "GitWorkspaceStore",
        lambda root: type(
            "Store",
            (),
            {"load": lambda self, issue: snapshot},
        )(),
    )
    service = ProductionWorkspaceService(
        commands=FakeCommands({}),
        copilot_assigner_factory=lambda **_: pytest.fail(
            "human merge wait must not assign Copilot"
        ),
    )

    result = service.assign_copilot(
        repository_root=tmp_path,
        issue_number=31,
        assignment_token=None,
    )

    assert result.assigned is False
    assert result.status == "not_required"
    assert result.next_action == "merge_workspace_pull_request"


def test_production_assignment_skips_spec_review_and_baseline_waits() -> None:
    review = WorkspaceSnapshot(
        issue_number=31,
        revision="a" * 40,
        phase=WorkspacePhase.SPECIFICATION,
        workspace_pull_request_number=104,
        candidates=(),
        selected_patch=None,
        external_operation_ids=(),
        experiments=(),
        lineage=None,
        specification=WorkspaceSpecificationRecord(
            status="human_review_required",
            spec_sha256=None,
            base_commit="b" * 40,
            target="support-agent",
            environment="development",
            asset_ids=("development", "validation", "quality"),
            metric_names=("quality",),
            policy_reason="custom assets require review",
        ),
    )
    baseline = WorkspaceSnapshot(
        **{
            **review.__dict__,
            "specification": _trusted_specification(),
        }
    )

    assert workspace_production._copilot_assignment_action(review) == (
        "review_specification",
        False,
    )
    assert workspace_production._copilot_assignment_action(baseline) == (
        "establish_baseline",
        False,
    )


def test_production_assignment_skips_pending_actions_experiment(
    monkeypatch,
    tmp_path: Path,
) -> None:
    snapshot = WorkspaceSnapshot(
        issue_number=31,
        revision="a" * 40,
        phase=WorkspacePhase.EVALUATING,
        workspace_pull_request_number=104,
        candidates=(),
        selected_patch=None,
        external_operation_ids=(),
        experiments=(
            WorkspaceExperimentRecord(
                candidate_id="candidate-1",
                mutation_class="system_instructions",
                patch_sha256="1" * 64,
                bundle_sha256="2" * 64,
                evidence_sha256="3" * 64,
                idempotency_key="4" * 64,
                operation_sha256="5" * 64,
                status="pending",
                changed_paths=("agent.py",),
                validation=("pytest: passed",),
                expected_tree="6" * 40,
            ),
        ),
        lineage=None,
    )
    monkeypatch.setattr(
        workspace_production,
        "GitWorkspaceStore",
        lambda root: type(
            "Store",
            (),
            {"load": lambda self, issue: snapshot},
        )(),
    )
    service = ProductionWorkspaceService(
        commands=FakeCommands({}),
        copilot_assigner_factory=lambda **_: pytest.fail(
            "pending Actions experiment must not assign Copilot"
        ),
    )

    result = service.assign_copilot(
        repository_root=tmp_path,
        issue_number=31,
        assignment_token=None,
    )

    assert result.assigned is False
    assert result.next_action == "await_trusted_actions_result"


def test_production_service_wires_trusted_workspace_verifier(
    monkeypatch,
    tmp_path: Path,
) -> None:
    commands = FakeCommands(
        {
            ("git", "remote", "get-url", "origin"): (
                "https://github.com/octo-org/optimizer.git\n"
            ),
            (
                "gh",
                "repo",
                "view",
                "octo-org/optimizer",
                "--json",
                "nameWithOwner,defaultBranchRef",
            ): json.dumps(
                {
                    "nameWithOwner": "octo-org/optimizer",
                    "defaultBranchRef": {"name": "main"},
                }
            ),
        }
    )
    recorded = {}

    class Verifier:
        def __init__(self, **kwargs):
            recorded.update(kwargs)

        def verify(
            self,
            root,
            *,
            issue_number,
            pull_request_number,
        ):
            recorded["root"] = root
            recorded["issue"] = issue_number
            recorded["pr"] = pull_request_number
            return "verified"

    monkeypatch.setattr(workspace_production, "WorkspaceVerifier", Verifier)
    monkeypatch.setattr(
        workspace_production,
        "GitWorkspaceStore",
        lambda root: f"store:{root}",
    )

    result = ProductionWorkspaceService(commands=commands).verify(
        repository_root=tmp_path,
        issue_number=31,
        pull_request_number=104,
    )

    assert result == "verified"
    assert recorded["repository"] == "octo-org/optimizer"
    assert recorded["base_branch"] == "main"
    assert recorded["issue"] == 31
    assert recorded["pr"] == 104


def test_production_service_loads_issue_and_reuses_recorded_pr(
    tmp_path: Path,
) -> None:
    body = "\n".join(
        (
            "<!-- foundry-opt:workspace-pr:issue-31:v1 -->",
            f"<!-- foundry-opt:workspace-base:{'a' * 40} -->",
        )
    )
    commands = FakeCommands(
        {
            ("git", "remote", "get-url", "origin"): (
                "https://github.com/octo-org/optimizer.git\n"
            ),
            (
                "gh",
                "repo",
                "view",
                "octo-org/optimizer",
                "--json",
                "nameWithOwner,defaultBranchRef",
            ): json.dumps(
                {
                    "nameWithOwner": "octo-org/optimizer",
                    "defaultBranchRef": {"name": "main"},
                }
            ),
            (
                "gh",
                "issue",
                "view",
                "31",
                "--repo",
                "octo-org/optimizer",
                "--json",
                "number,title,body,state",
            ): json.dumps(
                {
                    "number": 31,
                    "title": "[Optimize] Improve policy coverage",
                    "body": "Improve policy coverage without weakening safety.",
                    "state": "OPEN",
                }
            ),
            (
                "gh",
                "pr",
                "list",
                "--repo",
                "octo-org/optimizer",
                "--state",
                "all",
                "--head",
                "foundry-opt/workspace/issue-31",
                "--json",
                "number,body",
                "--limit",
                "2",
            ): json.dumps([{"number": 104, "body": body}]),
            (
                "gh",
                "pr",
                "list",
                "--repo",
                "octo-org/optimizer",
                "--state",
                "all",
                "--search",
                '"foundry-opt:workspace-pr:issue-31:v1" in:body',
                "--json",
                "number,body",
                "--limit",
                "2",
            ): json.dumps([{"number": 104, "body": body}]),
        }
    )

    class RecordingWorkspace:
        def __init__(self) -> None:
            self.request: Any = None

        def advance(self, request):
            self.request = request
            from foundry_opt.orchestration import WorkspaceResult

            return WorkspaceResult(
                phase=WorkspacePhase.SPECIFICATION,
                workspace_pull_request=request.workspace_pull_request,
                planned_effect_kinds=("workspace_pr_sync",),
                recorded=False,
            )

    recording = RecordingWorkspace()
    service = ProductionWorkspaceService(
        commands=commands,
        workspace_factory=lambda **_: recording,
    )

    service.advance(
        WorkspaceAdvanceRequest(
            repository_root=tmp_path,
            issue_number=31,
            trigger=WorkspaceTrigger.CONTINUE,
        )
    )

    assert recording.request.issue.base_commit == "a" * 40
    assert recording.request.issue.number == 31


@pytest.mark.parametrize(
    "trigger",
    (
        WorkspaceTrigger.PULL_REQUEST_MERGED,
        WorkspaceTrigger.DEPLOYMENT_COMPLETED,
        WorkspaceTrigger.RETENTION_COMPLETED,
    ),
)
def test_production_service_rejects_direct_trusted_lifecycle_triggers(
    tmp_path: Path,
    trigger: WorkspaceTrigger,
) -> None:
    service = ProductionWorkspaceService(
        commands=FakeCommands({}),
        workspace_factory=lambda **_: pytest.fail(
            "unsafe lifecycle trigger reached workspace"
        ),
    )

    with pytest.raises(ProductionWorkspaceError, match="trusted"):
        service.advance(
            WorkspaceAdvanceRequest(
                repository_root=tmp_path,
                issue_number=31,
                trigger=trigger,
            )
        )


def test_production_service_finds_workspace_pr_during_search_lag(
    tmp_path: Path,
) -> None:
    body = "\n".join(
        (
            "<!-- foundry-opt:workspace-pr:issue-31:v1 -->",
            f"<!-- foundry-opt:workspace-base:{'a' * 40} -->",
        )
    )
    repository_responses = {
        ("git", "remote", "get-url", "origin"): (
            "https://github.com/octo-org/optimizer.git\n"
        ),
        (
            "gh",
            "repo",
            "view",
            "octo-org/optimizer",
            "--json",
            "nameWithOwner,defaultBranchRef",
        ): json.dumps(
            {
                "nameWithOwner": "octo-org/optimizer",
                "defaultBranchRef": {"name": "main"},
            }
        ),
        (
            "gh",
            "issue",
            "view",
            "31",
            "--repo",
            "octo-org/optimizer",
            "--json",
            "number,title,body,state",
        ): json.dumps(
            {
                "number": 31,
                "title": "[Optimize] Improve policy coverage",
                "body": "Improve policy coverage.",
                "state": "OPEN",
            }
        ),
        (
            "gh",
            "pr",
            "list",
            "--repo",
            "octo-org/optimizer",
            "--state",
            "all",
            "--head",
            "foundry-opt/workspace/issue-31",
            "--json",
            "number,body",
            "--limit",
            "2",
        ): json.dumps([{"number": 104, "body": body}]),
        (
            "gh",
            "pr",
            "list",
            "--repo",
            "octo-org/optimizer",
            "--state",
            "all",
            "--search",
            '"foundry-opt:workspace-pr:issue-31:v1" in:body',
            "--json",
            "number,body",
            "--limit",
            "2",
        ): "[]",
    }
    recording: dict[str, Any] = {}

    class Workspace:
        def advance(self, request):
            recording["request"] = request
            from foundry_opt.orchestration import WorkspaceResult

            return WorkspaceResult(
                phase=WorkspacePhase.SPECIFICATION,
                workspace_pull_request=request.workspace_pull_request,
                planned_effect_kinds=("workspace_pr_sync",),
            )

    ProductionWorkspaceService(
        commands=FakeCommands(repository_responses),
        workspace_factory=lambda **_: Workspace(),
    ).advance(
        WorkspaceAdvanceRequest(
            repository_root=tmp_path,
            issue_number=31,
        )
    )

    assert recording["request"].workspace_pull_request.number == 104
    assert recording["request"].issue.base_commit == "a" * 40


def test_workspace_intake_rejects_trusted_repository_id_mismatch(
    tmp_path: Path,
) -> None:
    commands = FakeCommands(
        {
            ("git", "remote", "get-url", "origin"): (
                "https://github.com/octo-org/optimizer.git\n"
            ),
            (
                "gh",
                "repo",
                "view",
                "octo-org/optimizer",
                "--json",
                "nameWithOwner,defaultBranchRef",
            ): json.dumps(
                {
                    "nameWithOwner": "octo-org/optimizer",
                    "defaultBranchRef": {"name": "main"},
                }
            ),
            (
                "gh",
                "api",
                "repos/octo-org/optimizer",
                "--jq",
                ".id",
            ): "999\n",
        }
    )
    service = ProductionWorkspaceService(
        commands=commands,
        workspace_factory=lambda **_: pytest.fail(
            "workspace must not be built after repository ID mismatch"
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="repository ID does not match",
    ):
        service.ingest(
            {
                "action": "opened",
                "issue": {
                    "number": 31,
                    "title": "[Optimize] Improve policy coverage",
                    "body": "Improve policy coverage.",
                },
                "repository": {
                    "full_name": "octo-org/optimizer",
                    "id": 123,
                },
            },
            TrustedWorkspaceEventContext(
                event_name="issues",
                delivery_id="delivery-123",
                repository="octo-org/optimizer",
                repository_id=123,
            ),
            base_commit="a" * 40,
            repository_root=tmp_path,
        )


def test_existing_workspace_pr_discovery_rejects_inconsistent_results(
    tmp_path: Path,
) -> None:
    marker = "<!-- foundry-opt:workspace-pr:issue-31:v1 -->"
    commands = FakeCommands(
        {
            (
                "gh",
                "pr",
                "list",
                "--repo",
                "octo-org/optimizer",
                "--state",
                "all",
                "--head",
                "foundry-opt/workspace/issue-31",
                "--json",
                "number,body",
                "--limit",
                "2",
            ): json.dumps(
                [
                    {
                        "number": 104,
                        "body": (
                            f"{marker}\n"
                            f"<!-- foundry-opt:workspace-base:{'a' * 40} -->"
                        ),
                    }
                ]
            ),
            (
                "gh",
                "pr",
                "list",
                "--repo",
                "octo-org/optimizer",
                "--state",
                "all",
                "--search",
                '"foundry-opt:workspace-pr:issue-31:v1" in:body',
                "--json",
                "number,body",
                "--limit",
                "2",
            ): json.dumps(
                [
                    {
                        "number": 104,
                        "body": (
                            f"{marker}\n"
                            f"<!-- foundry-opt:workspace-base:{'b' * 40} -->"
                        ),
                    }
                ]
            ),
        }
    )

    with pytest.raises(RuntimeError, match="inconsistent"):
        ProductionWorkspaceService(
            commands=commands
        )._existing_workspace_pull_request(
            tmp_path,
            "octo-org/optimizer",
            31,
        )
