from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from foundry_opt.adapters.commands import CommandExitError
from foundry_opt.adapters.deployment import (
    DEPLOYMENT_OIDC_CLIENT_ID,
    DeploymentIdentityError,
    DeploymentResponseError,
)
from foundry_opt.adapters.optimization_deployment import (
    FoundryPublishedDeploymentReader,
    GhWorkflowRunGateway,
    LiveDeploymentCoordinator,
    OptimizationDeploymentError,
    PublishedDeployment,
    WorkflowRunQuery,
    build_live_deployment_coordinator,
)
from foundry_opt.config.models import OptimizerConfig
from foundry_opt.deployment import (
    DeployedRuntime,
    DeploymentLineageMismatchError,
    DeploymentRecord,
    DeploymentTrigger,
    DeploymentWorkflow,
    DeploymentWorkflowRun,
    OptimizationDeploymentLineage,
    WorkflowRunStatus,
    optimization_deployment_lineage_sha256,
)
from foundry_opt.github_workflow.models import PullRequestReference
from foundry_opt.optimization.lifecycle import (
    DeploymentLifecycleRequest,
    DeploymentOutcome,
    DeploymentOutcomeStatus,
)
from foundry_opt.optimization.models import (
    AssetKind,
    AssetProvenance,
    OptimizationSpec,
)
from foundry_opt.optimization.runner import CapabilityUnavailableError
from foundry_opt.config.models import MetricPolicy, MutationClass
from foundry_opt.preflight.interfaces import CommandResult


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

REPOSITORY = "octo-org/agents"
AGENT = "support-agent"
PROJECT_ENDPOINT = (
    "https://example.services.ai.azure.com/api/projects/demo"
)
WORKFLOW_PATH = Path(".github/workflows/deploy.yml")

BASE_COMMIT = "b" * 40
PR_HEAD_COMMIT = "c" * 40
MERGE_COMMIT = "e" * 40
OTHER_COMMIT = "f" * 40
TREE = "a" * 40
PATCH_SHA = "1" * 64
EVIDENCE_SHA = "2" * 64
BASELINE_SHA = "3" * 64
BUNDLE_SHA = "4" * 64
PORTAL_URL = (
    "https://ai.azure.com/projects/demo/agents/support-agent/versions/13"
)
RUN_URL = "https://github.com/octo-org/agents/actions/runs/12"

BASE_VERSION = 12
PUBLISHED_VERSION = 13

GOAL = (
    "Improve the support agent's answer coverage while preserving the "
    "advisory safety boundary on every candidate."
)


_CONFIG_YAML = f"""
schema_version: "1"
default_environment: acceptance
environments:
  acceptance:
    project_endpoint: {PROJECT_ENDPOINT}
    project_resource_id: /subscriptions/s/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/f/projects/demo
    allowed_models: [gpt-5.1]
    deployment_workflow:
      path: .github/workflows/deploy.yml
      trigger: manual
targets:
  {AGENT}:
    environment: acceptance
    source_paths: [agent]
    edit_paths: [agent]
    entry_point: agent/main.py
    base_agent_version: "{BASE_VERSION}"
    package:
      include: ["agent/**"]
      exclude: []
    datasets:
      development:
        - {{name: dev, version: v1, mode: batch}}
      validation:
        - {{name: held-out, version: v1, mode: batch}}
    evaluators:
      - {{name: quality, reference: quality-evaluator, metrics: [quality]}}
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


def _config() -> OptimizerConfig:
    return OptimizerConfig.model_validate(yaml.safe_load(_CONFIG_YAML))


def _spec() -> OptimizationSpec:
    return OptimizationSpec(
        issue_number=7,
        repository=REPOSITORY,
        base_commit=BASE_COMMIT,
        target=AGENT,
        environment="acceptance",
        base_agent_version=str(BASE_VERSION),
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
    )


def _lineage() -> OptimizationDeploymentLineage:
    return OptimizationDeploymentLineage(
        parent_issue_number=7,
        spec_sha256=_spec().sha256,
        campaign_id="issue-7",
        campaign_pull_request_number=100,
        candidate_issue_number=201,
        candidate_pull_request_number=110,
        candidate_id="candidate-1",
        selected_draft_id="draft-candidate-1",
        patch_sha256=PATCH_SHA,
        evidence_sha256=EVIDENCE_SHA,
        selected_tree_sha=TREE,
        selected_merge_commit=MERGE_COMMIT,
    )


def _pull_request() -> PullRequestReference:
    return PullRequestReference(
        number=110,
        url=f"https://github.com/{REPOSITORY}/pull/110",
        head_branch="foundry-opt/issue-7/candidate-1/session",
        head_commit=PR_HEAD_COMMIT,
        draft=False,
        body="",
        base_branch="main",
        state="MERGED",
    )


def _workflow(
    trigger: DeploymentTrigger = DeploymentTrigger.MANUAL,
    *,
    exists: bool = True,
) -> DeploymentWorkflow:
    if exists:
        return DeploymentWorkflow(
            path=WORKFLOW_PATH,
            trigger=trigger,
            exists=True,
            name="Deploy Foundry",
        )
    from foundry_opt.deployment import (
        DeploymentWorkflowModel,
        DeploymentWorkflowScaffold,
    )

    return DeploymentWorkflow(
        path=WORKFLOW_PATH,
        trigger=trigger,
        exists=False,
        name="Deploy Foundry",
        scaffold=DeploymentWorkflowScaffold(
            description="generated",
            model=DeploymentWorkflowModel(
                trigger=trigger,
                permissions=("id-token: write",),
                actions=("publish",),
            ),
        ),
    )


def _request(
    tmp_path: Path,
    *,
    trigger: DeploymentTrigger = DeploymentTrigger.MANUAL,
    dispatch: bool = False,
    exists: bool = True,
) -> DeploymentLifecycleRequest:
    return DeploymentLifecycleRequest(
        repository_root=tmp_path,
        workflow=_workflow(trigger, exists=exists),
        lineage=_lineage(),
        selected_candidate_id="candidate-1",
        selected_pull_request=_pull_request(),
        merge_commit=MERGE_COMMIT,
        project_endpoint=PROJECT_ENDPOINT,
        dispatch=dispatch,
        spec=_spec(),
    )


def _run(
    status: WorkflowRunStatus,
    *,
    trigger: DeploymentTrigger = DeploymentTrigger.MANUAL,
    head_commit: str = MERGE_COMMIT,
    url: str = RUN_URL,
) -> DeploymentWorkflowRun:
    return DeploymentWorkflowRun(
        path=WORKFLOW_PATH,
        trigger=trigger,
        status=status,
        head_commit=head_commit,
        url=url,
    )


def _published(
    *,
    lineage: OptimizationDeploymentLineage | None = None,
    version: int = PUBLISHED_VERSION,
    latest_version: int | None = None,
    source_sha256: str = BUNDLE_SHA,
    record_sha256: str = BUNDLE_SHA,
    portal_url: str = PORTAL_URL,
    runtime_portal_url: str | None = None,
    status: str = "active",
    include_lineage_digest: bool = True,
) -> PublishedDeployment:
    lineage = lineage or _lineage()
    digest = optimization_deployment_lineage_sha256(lineage)
    metadata = {
        "foundry-opt-base-version": str(BASE_VERSION),
        "foundry-opt-baseline-source-sha256": BASELINE_SHA,
        "foundry-opt-source-sha256": record_sha256,
        "foundry-opt-patch-sha256": PATCH_SHA,
        "foundry-opt-tree-hash": TREE,
        "foundry-opt-evidence-sha256": EVIDENCE_SHA,
    }
    if include_lineage_digest:
        metadata["foundry-opt-lineage-sha256"] = digest
    record = DeploymentRecord(
        project_endpoint=PROJECT_ENDPOINT,
        agent_name=AGENT,
        version=version,
        base_version=BASE_VERSION,
        baseline_source_sha256=BASELINE_SHA,
        sha256=record_sha256,
        patch_sha256=lineage.patch_sha256,
        tree_hash=lineage.selected_tree_sha,
        evidence_sha256=lineage.evidence_sha256,
        lineage=lineage,
        status=status,
        portal_url=portal_url,
        runtime="python_3_13",
        entry_point=("python", "agent/main.py"),
        dependency_resolution="remote_build",
        metadata=metadata,
    )
    runtime = DeployedRuntime(
        agent_name=AGENT,
        deployed_version=version,
        latest_version=latest_version if latest_version is not None else version,
        source_sha256=source_sha256,
        portal_url=runtime_portal_url or portal_url,
    )
    return PublishedDeployment(record=record, runtime=runtime)


def _write_dispatch_workflow(
    tmp_path: Path,
    *,
    input_name: str = "selected_commit",
) -> None:
    path = tmp_path / WORKFLOW_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "name: Deploy Foundry\n"
        "on:\n"
        "  workflow_dispatch:\n"
        "    inputs:\n"
        f"      {input_name}:\n"
        "        description: exact selected commit\n"
        "        required: true\n"
        "jobs:\n"
        "  publish:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo deploy\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeWorkflowRunGateway:
    def __init__(
        self,
        find_results: Sequence[DeploymentWorkflowRun | None],
        *,
        dispatch_error: Exception | None = None,
    ) -> None:
        self._results = list(find_results)
        self._last = self._results[-1] if self._results else None
        self._dispatch_error = dispatch_error
        self.find_calls: list[WorkflowRunQuery] = []
        self.dispatch_calls: list[tuple[Path, str, str]] = []

    def find_run(
        self,
        repository_root: Path,
        *,
        query: WorkflowRunQuery,
    ) -> DeploymentWorkflowRun | None:
        self.find_calls.append(query)
        if self._results:
            return self._results.pop(0)
        return self._last

    def dispatch(
        self,
        repository_root: Path,
        *,
        workflow_path: Path,
        input_name: str,
        commit: str,
    ) -> None:
        if self._dispatch_error is not None:
            raise self._dispatch_error
        self.dispatch_calls.append((workflow_path, input_name, commit))


class FakeReader:
    def __init__(
        self,
        result: PublishedDeployment | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self._result = result
        self._error = error
        self.calls: list[tuple[str, str, OptimizationDeploymentLineage]] = []

    def read_latest(
        self,
        *,
        project_endpoint: str,
        agent_name: str,
        expected_lineage: OptimizationDeploymentLineage,
    ) -> PublishedDeployment | None:
        self.calls.append((project_endpoint, agent_name, expected_lineage))
        if self._error is not None:
            raise self._error
        return self._result


class FakePublisher:
    def __init__(self, outcome: DeploymentOutcome) -> None:
        self._outcome = outcome
        self.calls: list[DeploymentLifecycleRequest] = []

    def publish(
        self,
        request: DeploymentLifecycleRequest,
    ) -> DeploymentOutcome:
        self.calls.append(request)
        return self._outcome


def _coordinator(
    gateway: FakeWorkflowRunGateway,
    reader: FakeReader,
    *,
    publisher: FakePublisher | None = None,
    poll_attempts: int = 3,
) -> LiveDeploymentCoordinator:
    return LiveDeploymentCoordinator(
        _config(),
        gateway,
        reader,
        publisher=publisher,
        poll_attempts=poll_attempts,
        poll_interval_seconds=0.0,
        sleep=lambda _seconds: None,
    )


# ---------------------------------------------------------------------------
# Coordinator: manual observe / dispatch
# ---------------------------------------------------------------------------


def test_manual_observe_only_returns_manual_trigger_required(
    tmp_path: Path,
) -> None:
    gateway = FakeWorkflowRunGateway([None])
    reader = FakeReader()
    coordinator = _coordinator(gateway, reader)

    outcome = coordinator.deploy(
        _request(tmp_path, trigger=DeploymentTrigger.MANUAL, dispatch=False)
    )

    assert outcome.status is DeploymentOutcomeStatus.MANUAL_TRIGGER_REQUIRED
    assert gateway.dispatch_calls == []
    assert reader.calls == []
    # Only the exact merge commit is ever queried, never a latest run.
    assert gateway.find_calls[0].head_sha == MERGE_COMMIT
    assert gateway.find_calls[0].events == ("workflow_dispatch",)


def test_manual_dispatch_forwards_exact_merge_commit(tmp_path: Path) -> None:
    _write_dispatch_workflow(tmp_path)
    success = _run(WorkflowRunStatus.SUCCESS)
    gateway = FakeWorkflowRunGateway([None, success])
    reader = FakeReader(_published())
    coordinator = _coordinator(gateway, reader)

    outcome = coordinator.deploy(
        _request(tmp_path, trigger=DeploymentTrigger.MANUAL, dispatch=True)
    )

    assert outcome.status is DeploymentOutcomeStatus.VERIFIED
    assert outcome.version == PUBLISHED_VERSION
    assert outcome.run_url == RUN_URL
    assert outcome.portal_url == PORTAL_URL
    # Dispatched exactly once with the exact merge commit (not the PR head).
    assert len(gateway.dispatch_calls) == 1
    workflow_path, input_name, commit = gateway.dispatch_calls[0]
    assert workflow_path == WORKFLOW_PATH
    assert input_name == "selected_commit"
    assert commit == MERGE_COMMIT
    assert commit != PR_HEAD_COMMIT


def test_manual_dispatch_requires_declared_commit_input(
    tmp_path: Path,
) -> None:
    # The workflow declares a different input, so the coordinator refuses to
    # dispatch rather than deploy without forwarding the exact commit.
    _write_dispatch_workflow(tmp_path, input_name="environment")
    gateway = FakeWorkflowRunGateway([None])
    coordinator = _coordinator(gateway, FakeReader())

    with pytest.raises(OptimizationDeploymentError):
        coordinator.deploy(
            _request(tmp_path, trigger=DeploymentTrigger.MANUAL, dispatch=True)
        )
    assert gateway.dispatch_calls == []


def test_manual_dispatch_uses_config_mapped_input_name(tmp_path: Path) -> None:
    _write_dispatch_workflow(tmp_path, input_name="deploy_commit")
    success = _run(WorkflowRunStatus.SUCCESS)
    gateway = FakeWorkflowRunGateway([None, success])
    coordinator = LiveDeploymentCoordinator(
        _config(),
        gateway,
        FakeReader(_published()),
        dispatch_input_names=("selected_commit", "deploy_commit"),
        poll_attempts=3,
        poll_interval_seconds=0.0,
        sleep=lambda _s: None,
    )

    outcome = coordinator.deploy(
        _request(tmp_path, trigger=DeploymentTrigger.MANUAL, dispatch=True)
    )

    assert outcome.status is DeploymentOutcomeStatus.VERIFIED
    assert gateway.dispatch_calls[0][1] == "deploy_commit"


def test_manual_dispatch_is_idempotent_when_run_exists(
    tmp_path: Path,
) -> None:
    # Partial retry: a run already exists for this workflow + commit, so the
    # coordinator observes it instead of dispatching a duplicate deployment.
    success = _run(WorkflowRunStatus.SUCCESS)
    gateway = FakeWorkflowRunGateway([success])
    reader = FakeReader(_published())
    coordinator = _coordinator(gateway, reader)

    outcome = coordinator.deploy(
        _request(tmp_path, trigger=DeploymentTrigger.MANUAL, dispatch=True)
    )

    assert outcome.status is DeploymentOutcomeStatus.VERIFIED
    assert gateway.dispatch_calls == []


# ---------------------------------------------------------------------------
# Coordinator: merge observe
# ---------------------------------------------------------------------------


def test_merge_observe_success_verifies(tmp_path: Path) -> None:
    success = _run(WorkflowRunStatus.SUCCESS, trigger=DeploymentTrigger.MERGE)
    gateway = FakeWorkflowRunGateway([success])
    reader = FakeReader(_published())
    coordinator = _coordinator(gateway, reader)

    outcome = coordinator.deploy(
        _request(tmp_path, trigger=DeploymentTrigger.MERGE, dispatch=False)
    )

    assert outcome.status is DeploymentOutcomeStatus.VERIFIED
    assert outcome.version == PUBLISHED_VERSION
    assert gateway.dispatch_calls == []
    assert gateway.find_calls[0].events == ("push", "workflow_run")
    assert gateway.find_calls[0].head_sha == MERGE_COMMIT


def test_merge_observe_no_run_is_pending(tmp_path: Path) -> None:
    gateway = FakeWorkflowRunGateway([None])
    coordinator = _coordinator(gateway, FakeReader())

    outcome = coordinator.deploy(
        _request(tmp_path, trigger=DeploymentTrigger.MERGE, dispatch=False)
    )

    assert outcome.status is DeploymentOutcomeStatus.PENDING
    assert outcome.reason_code == "merge_deployment_pending"


# ---------------------------------------------------------------------------
# Coordinator: run failure / pending
# ---------------------------------------------------------------------------


def test_run_failure_returns_failed(tmp_path: Path) -> None:
    failed = _run(WorkflowRunStatus.FAILURE)
    gateway = FakeWorkflowRunGateway([failed])
    reader = FakeReader(_published())
    coordinator = _coordinator(gateway, reader)

    outcome = coordinator.deploy(
        _request(tmp_path, trigger=DeploymentTrigger.MANUAL, dispatch=False)
    )

    assert outcome.status is DeploymentOutcomeStatus.FAILED
    assert outcome.run_url == RUN_URL
    # No published version is read once the workflow has failed.
    assert reader.calls == []


def test_run_pending_polls_then_returns_pending(tmp_path: Path) -> None:
    pending = _run(WorkflowRunStatus.IN_PROGRESS)
    gateway = FakeWorkflowRunGateway([pending])
    reader = FakeReader(_published())
    coordinator = _coordinator(gateway, reader, poll_attempts=3)

    outcome = coordinator.deploy(
        _request(tmp_path, trigger=DeploymentTrigger.MANUAL, dispatch=False)
    )

    assert outcome.status is DeploymentOutcomeStatus.PENDING
    assert outcome.run_url == RUN_URL
    assert reader.calls == []
    # Bounded polling: one observation plus (poll_attempts - 1) re-checks.
    assert len(gateway.find_calls) == 3


def test_run_transitions_to_success_after_polling(tmp_path: Path) -> None:
    in_progress = _run(WorkflowRunStatus.IN_PROGRESS)
    success = _run(WorkflowRunStatus.SUCCESS)
    gateway = FakeWorkflowRunGateway([in_progress, success])
    reader = FakeReader(_published())
    coordinator = _coordinator(gateway, reader, poll_attempts=5)

    outcome = coordinator.deploy(
        _request(tmp_path, trigger=DeploymentTrigger.MANUAL, dispatch=False)
    )

    assert outcome.status is DeploymentOutcomeStatus.VERIFIED


# ---------------------------------------------------------------------------
# Coordinator: verification of the published version
# ---------------------------------------------------------------------------


def test_lineage_mismatch_propagates(tmp_path: Path) -> None:
    success = _run(WorkflowRunStatus.SUCCESS)
    gateway = FakeWorkflowRunGateway([success])
    reader = FakeReader(error=DeploymentLineageMismatchError())
    coordinator = _coordinator(gateway, reader)

    with pytest.raises(DeploymentLineageMismatchError):
        coordinator.deploy(
            _request(tmp_path, trigger=DeploymentTrigger.MANUAL, dispatch=False)
        )


def test_bundle_hash_mismatch_returns_mismatch(tmp_path: Path) -> None:
    # The deployed runtime source hash disagrees with the published record's
    # bundle hash: fail closed instead of reporting VERIFIED.
    success = _run(WorkflowRunStatus.SUCCESS)
    published = _published(source_sha256="9" * 64, record_sha256=BUNDLE_SHA)
    gateway = FakeWorkflowRunGateway([success])
    coordinator = _coordinator(gateway, FakeReader(published))

    outcome = coordinator.deploy(
        _request(tmp_path, trigger=DeploymentTrigger.MANUAL, dispatch=False)
    )

    assert outcome.status is DeploymentOutcomeStatus.MISMATCH
    assert outcome.reason_code == "provenance_mismatch"


def test_non_latest_published_version_returns_mismatch(
    tmp_path: Path,
) -> None:
    # A newer numeric version exists than the one deployed: refuse to verify.
    success = _run(WorkflowRunStatus.SUCCESS)
    published = _published(version=PUBLISHED_VERSION, latest_version=14)
    gateway = FakeWorkflowRunGateway([success])
    coordinator = _coordinator(gateway, FakeReader(published))

    outcome = coordinator.deploy(
        _request(tmp_path, trigger=DeploymentTrigger.MANUAL, dispatch=False)
    )

    assert outcome.status is DeploymentOutcomeStatus.MISMATCH


def test_missing_published_version_returns_mismatch(tmp_path: Path) -> None:
    success = _run(WorkflowRunStatus.SUCCESS)
    gateway = FakeWorkflowRunGateway([success])
    coordinator = _coordinator(gateway, FakeReader(None))

    outcome = coordinator.deploy(
        _request(tmp_path, trigger=DeploymentTrigger.MANUAL, dispatch=False)
    )

    assert outcome.status is DeploymentOutcomeStatus.MISMATCH
    assert outcome.reason_code == "published_version_missing"


def test_wrong_run_commit_is_never_verified(tmp_path: Path) -> None:
    # The gateway only ever hands back a run bound to the exact merge commit;
    # if it cannot (no matching run), the coordinator does not read a version.
    gateway = FakeWorkflowRunGateway([None])
    reader = FakeReader(_published())
    coordinator = _coordinator(gateway, reader)

    outcome = coordinator.deploy(
        _request(tmp_path, trigger=DeploymentTrigger.MANUAL, dispatch=False)
    )

    assert outcome.status is DeploymentOutcomeStatus.MANUAL_TRIGGER_REQUIRED
    assert reader.calls == []


# ---------------------------------------------------------------------------
# Coordinator: OIDC / capability errors
# ---------------------------------------------------------------------------


def test_oidc_identity_error_becomes_capability_unavailable(
    tmp_path: Path,
) -> None:
    success = _run(WorkflowRunStatus.SUCCESS)
    gateway = FakeWorkflowRunGateway([success])
    reader = FakeReader(error=DeploymentIdentityError())
    coordinator = _coordinator(gateway, reader)

    with pytest.raises(CapabilityUnavailableError) as error:
        coordinator.deploy(
            _request(tmp_path, trigger=DeploymentTrigger.MANUAL, dispatch=False)
        )
    assert error.value.code == "deployment_identity"


def test_response_error_becomes_capability_unavailable(tmp_path: Path) -> None:
    success = _run(WorkflowRunStatus.SUCCESS)
    gateway = FakeWorkflowRunGateway([success])
    reader = FakeReader(error=DeploymentResponseError())
    coordinator = _coordinator(gateway, reader)

    with pytest.raises(CapabilityUnavailableError) as error:
        coordinator.deploy(
            _request(tmp_path, trigger=DeploymentTrigger.MANUAL, dispatch=False)
        )
    assert error.value.code == "deployment_response"


def test_invalid_merge_commit_raises(tmp_path: Path) -> None:
    gateway = FakeWorkflowRunGateway([None])
    coordinator = _coordinator(gateway, FakeReader())
    request = _request(tmp_path)
    object.__setattr__(request, "merge_commit", "not-a-commit")

    with pytest.raises(OptimizationDeploymentError):
        coordinator.deploy(request)


# ---------------------------------------------------------------------------
# Coordinator: generated (product-published) manual workflow
# ---------------------------------------------------------------------------


def test_generated_workflow_without_publisher_is_unavailable(
    tmp_path: Path,
) -> None:
    gateway = FakeWorkflowRunGateway([None])
    coordinator = _coordinator(gateway, FakeReader())

    with pytest.raises(CapabilityUnavailableError) as error:
        coordinator.deploy(
            _request(
                tmp_path,
                trigger=DeploymentTrigger.MANUAL,
                dispatch=True,
                exists=False,
            )
        )
    assert error.value.code == "deployment_workflow_missing"
    assert gateway.find_calls == []


def test_generated_workflow_delegates_to_publisher(tmp_path: Path) -> None:
    gateway = FakeWorkflowRunGateway([None])
    published_outcome = DeploymentOutcome(
        status=DeploymentOutcomeStatus.VERIFIED,
        version=PUBLISHED_VERSION,
        portal_url=PORTAL_URL,
    )
    publisher = FakePublisher(published_outcome)
    coordinator = _coordinator(gateway, FakeReader(), publisher=publisher)

    request = _request(
        tmp_path,
        trigger=DeploymentTrigger.MANUAL,
        dispatch=True,
        exists=False,
    )
    outcome = coordinator.deploy(request)

    assert outcome is published_outcome
    assert publisher.calls == [request]
    assert gateway.find_calls == []


# ---------------------------------------------------------------------------
# GhWorkflowRunGateway
# ---------------------------------------------------------------------------


def _has(*fragments: str):
    def predicate(args: tuple[str, ...]) -> bool:
        return all(fragment in args for fragment in fragments)

    return predicate


class FakeCommandRunner:
    def __init__(self) -> None:
        self.rules: list[tuple[Any, Any]] = []
        self.calls: list[tuple[str, ...]] = []
        self.add(
            _has("git", "remote", "get-url", "origin"),
            f"https://github.com/{REPOSITORY}.git\n",
        )
        self.add(_has("gh", "api"), "main\n")
        self.add(_has("gh", "workflow", "run"), "")

    def add(self, predicate: Any, value: Any) -> "FakeCommandRunner":
        self.rules.append((predicate, value))
        return self

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
        environment: Mapping[str, str] | None = None,
        input_text: str | None = None,
        input_bytes: bytes | None = None,
    ) -> CommandResult:
        args = tuple(arguments)
        self.calls.append(args)
        for predicate, value in self.rules:
            if predicate(args):
                if isinstance(value, Exception):
                    raise value
                return CommandResult(0, str(value), "")
        raise CommandExitError(
            list(args), exit_code=1, stdout="", stderr="unmatched"
        )


def _run_list_json() -> str:
    return json.dumps(
        [
            {
                "databaseId": 5,
                "headSha": OTHER_COMMIT,
                "status": "completed",
                "conclusion": "success",
                "event": "push",
                "path": ".github/workflows/deploy.yml",
                "url": (
                    "https://github.com/octo-org/agents/actions/runs/5"
                ),
            },
            {
                "databaseId": 9,
                "headSha": MERGE_COMMIT,
                "status": "completed",
                "conclusion": "success",
                "event": "push",
                "path": ".github/workflows/other.yml",
                "url": (
                    "https://github.com/octo-org/agents/actions/runs/9"
                ),
            },
            {
                "databaseId": 7,
                "headSha": MERGE_COMMIT,
                "status": "completed",
                "conclusion": "failure",
                "event": "push",
                "path": ".github/workflows/deploy.yml",
                "url": (
                    "https://github.com/octo-org/agents/actions/runs/7"
                ),
            },
            {
                "databaseId": 12,
                "headSha": MERGE_COMMIT,
                "status": "completed",
                "conclusion": "success",
                "event": "push",
                "path": ".github/workflows/deploy.yml",
                "url": (
                    "https://github.com/octo-org/agents/actions/runs/12"
                ),
            },
        ]
    )


def _merge_query() -> WorkflowRunQuery:
    return WorkflowRunQuery(
        workflow_path=WORKFLOW_PATH,
        events=("push", "workflow_run"),
        head_sha=MERGE_COMMIT,
        trigger=DeploymentTrigger.MERGE,
    )


def test_gh_gateway_selects_exact_commit_and_workflow(tmp_path: Path) -> None:
    runner = FakeCommandRunner().add(
        _has("gh", "run", "list"), _run_list_json()
    )
    gateway = GhWorkflowRunGateway(runner)

    run = gateway.find_run(tmp_path, query=_merge_query())

    assert run is not None
    # The most recent run bound to the exact commit + workflow is chosen; the
    # wrong-commit and wrong-workflow runs are ignored.
    assert run.url.endswith("/runs/12")
    assert run.head_commit == MERGE_COMMIT
    assert run.status is WorkflowRunStatus.SUCCESS
    assert run.path == WORKFLOW_PATH


def test_gh_gateway_ignores_runs_for_other_commits(tmp_path: Path) -> None:
    only_wrong = json.dumps(
        [
            {
                "databaseId": 5,
                "headSha": OTHER_COMMIT,
                "status": "completed",
                "conclusion": "success",
                "event": "push",
                "path": ".github/workflows/deploy.yml",
                "url": (
                    "https://github.com/octo-org/agents/actions/runs/5"
                ),
            }
        ]
    )
    runner = FakeCommandRunner().add(_has("gh", "run", "list"), only_wrong)
    gateway = GhWorkflowRunGateway(runner)

    assert gateway.find_run(tmp_path, query=_merge_query()) is None


def test_gh_gateway_ignores_disallowed_events(tmp_path: Path) -> None:
    manual_only = json.dumps(
        [
            {
                "databaseId": 5,
                "headSha": MERGE_COMMIT,
                "status": "completed",
                "conclusion": "success",
                "event": "workflow_dispatch",
                "path": ".github/workflows/deploy.yml",
                "url": (
                    "https://github.com/octo-org/agents/actions/runs/5"
                ),
            }
        ]
    )
    runner = FakeCommandRunner().add(_has("gh", "run", "list"), manual_only)
    gateway = GhWorkflowRunGateway(runner)

    # A merge query does not accept a workflow_dispatch run.
    assert gateway.find_run(tmp_path, query=_merge_query()) is None


def test_gh_gateway_maps_pending_status(tmp_path: Path) -> None:
    pending = json.dumps(
        [
            {
                "databaseId": 5,
                "headSha": MERGE_COMMIT,
                "status": "in_progress",
                "conclusion": None,
                "event": "push",
                "path": ".github/workflows/deploy.yml",
                "url": (
                    "https://github.com/octo-org/agents/actions/runs/5"
                ),
            }
        ]
    )
    runner = FakeCommandRunner().add(_has("gh", "run", "list"), pending)
    gateway = GhWorkflowRunGateway(runner)

    run = gateway.find_run(tmp_path, query=_merge_query())
    assert run is not None
    assert run.status is WorkflowRunStatus.IN_PROGRESS


def test_gh_gateway_dispatch_forwards_commit_field(tmp_path: Path) -> None:
    runner = FakeCommandRunner()
    gateway = GhWorkflowRunGateway(runner)

    gateway.dispatch(
        tmp_path,
        workflow_path=WORKFLOW_PATH,
        input_name="selected_commit",
        commit=MERGE_COMMIT,
    )

    dispatched = [
        call
        for call in runner.calls
        if "workflow" in call and "run" in call and "gh" in call
    ]
    assert len(dispatched) == 1
    call = dispatched[0]
    assert ".github/workflows/deploy.yml" in call
    assert "--ref" in call
    assert "main" in call
    assert f"selected_commit={MERGE_COMMIT}" in call


def test_gh_gateway_dispatch_rejects_bad_commit(tmp_path: Path) -> None:
    gateway = GhWorkflowRunGateway(FakeCommandRunner())
    with pytest.raises(OptimizationDeploymentError):
        gateway.dispatch(
            tmp_path,
            workflow_path=WORKFLOW_PATH,
            input_name="selected_commit",
            commit="deadbeef",
        )


def test_gh_gateway_wraps_command_failure(tmp_path: Path) -> None:
    runner = FakeCommandRunner().add(
        _has("gh", "run", "list"),
        CommandExitError(["gh"], exit_code=1, stdout="", stderr="boom"),
    )
    gateway = GhWorkflowRunGateway(runner)
    with pytest.raises(OptimizationDeploymentError):
        gateway.find_run(tmp_path, query=_merge_query())


# ---------------------------------------------------------------------------
# FoundryPublishedDeploymentReader
# ---------------------------------------------------------------------------


class FakeCredentialProvider:
    def __init__(self, client_id: str = DEPLOYMENT_OIDC_CLIENT_ID) -> None:
        self.client_id = client_id
        self.create_count = 0
        self.credential = SimpleNamespace(closed=False)
        self.credential.close = lambda: setattr(
            self.credential, "closed", True
        )

    def active_client_id(self) -> str:
        return self.client_id

    def create(self) -> object:
        self.create_count += 1
        return self.credential


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.requests: list[Any] = []
        self.closed = False

    def send_request(self, request: Any) -> FakeResponse:
        self.requests.append(request)
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


def _versions_payload() -> dict[str, Any]:
    return {
        "value": [
            {"version": "12", "draft": False},
            {"version": "13", "draft": False},
            {"version": "14", "draft": True},
        ]
    }


def _readback_payload(
    *,
    lineage: OptimizationDeploymentLineage | None = None,
    lineage_digest: str | None = None,
) -> dict[str, Any]:
    lineage = lineage or _lineage()
    digest = lineage_digest or optimization_deployment_lineage_sha256(lineage)
    return {
        "version": "13",
        "draft": False,
        "status": "active",
        "portal_url": PORTAL_URL,
        "metadata": {
            "foundry-opt-base-version": str(BASE_VERSION),
            "foundry-opt-baseline-source-sha256": BASELINE_SHA,
            "foundry-opt-source-sha256": BUNDLE_SHA,
            "foundry-opt-patch-sha256": PATCH_SHA,
            "foundry-opt-tree-hash": TREE,
            "foundry-opt-evidence-sha256": EVIDENCE_SHA,
            "foundry-opt-lineage-sha256": digest,
        },
        "definition": {
            "kind": "hosted",
            "code_configuration": {
                "runtime": "python_3_13",
                "entry_point": ["python", "agent/main.py"],
                "dependency_resolution": "remote_build",
                "content_hash": BUNDLE_SHA,
            },
        },
    }


def _reader(
    client: FakeClient,
    *,
    credentials: FakeCredentialProvider | None = None,
) -> tuple[FoundryPublishedDeploymentReader, FakeCredentialProvider]:
    credentials = credentials or FakeCredentialProvider()
    reader = FoundryPublishedDeploymentReader(
        credentials,
        client_factory=lambda endpoint, credential: client,
    )
    return reader, credentials


def test_reader_reads_latest_numeric_version(tmp_path: Path) -> None:
    client = FakeClient(
        [
            FakeResponse(200, _versions_payload()),
            FakeResponse(200, _readback_payload()),
        ]
    )
    reader, credentials = _reader(client)

    published = reader.read_latest(
        project_endpoint=PROJECT_ENDPOINT,
        agent_name=AGENT,
        expected_lineage=_lineage(),
    )

    assert published is not None
    assert published.record.version == 13
    assert published.runtime.deployed_version == 13
    assert published.runtime.latest_version == 13
    assert published.runtime.source_sha256 == BUNDLE_SHA
    assert published.runtime.portal_url == PORTAL_URL
    assert published.record.lineage == _lineage()
    assert credentials.create_count == 1
    assert client.closed is True
    assert credentials.credential.closed is True


def test_reader_returns_none_without_published_version() -> None:
    client = FakeClient([FakeResponse(200, {"value": []})])
    reader, _ = _reader(client)

    published = reader.read_latest(
        project_endpoint=PROJECT_ENDPOINT,
        agent_name=AGENT,
        expected_lineage=_lineage(),
    )
    assert published is None


def test_reader_fails_closed_on_lineage_digest_mismatch() -> None:
    client = FakeClient(
        [
            FakeResponse(200, _versions_payload()),
            FakeResponse(
                200,
                _readback_payload(lineage_digest="0" * 64),
            ),
        ]
    )
    reader, _ = _reader(client)

    with pytest.raises(DeploymentLineageMismatchError):
        reader.read_latest(
            project_endpoint=PROJECT_ENDPOINT,
            agent_name=AGENT,
            expected_lineage=_lineage(),
        )


def test_reader_rejects_non_deployment_principal() -> None:
    client = FakeClient([])
    reader, _ = _reader(
        client, credentials=FakeCredentialProvider(client_id="intruder")
    )

    with pytest.raises(DeploymentIdentityError):
        reader.read_latest(
            project_endpoint=PROJECT_ENDPOINT,
            agent_name=AGENT,
            expected_lineage=_lineage(),
        )


def test_reader_rejects_missing_portal_url() -> None:
    payload = _readback_payload()
    del payload["portal_url"]
    client = FakeClient(
        [
            FakeResponse(200, _versions_payload()),
            FakeResponse(200, payload),
        ]
    )
    reader, _ = _reader(client)

    with pytest.raises(DeploymentResponseError):
        reader.read_latest(
            project_endpoint=PROJECT_ENDPOINT,
            agent_name=AGENT,
            expected_lineage=_lineage(),
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_build_live_deployment_coordinator_assembles() -> None:
    coordinator = build_live_deployment_coordinator(
        _config(),
        command_runner=FakeCommandRunner(),
        credential_provider=FakeCredentialProvider(),
    )
    assert isinstance(coordinator, LiveDeploymentCoordinator)
