from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import zipfile

import pytest
import yaml

from foundry_opt.adapters.commands import CommandExitError
from foundry_opt.adapters.drafts import DraftAuthenticationError, DraftGateway
from foundry_opt.adapters.foundry import AzureCliCredentialProvider
from foundry_opt.adapters.foundry_assets import (
    EvaluationAssetRegistrationGateway,
    FoundryAssetAuthenticationError,
    FoundryAssetResolutionGateway as LiveFoundryAssetResolutionGateway,
)
from foundry_opt.adapters.optimization_evaluation import (
    OptimizationEvaluationBinder,
    OptimizationEvaluationError,
)
from foundry_opt.adapters.optimization_publication import CampaignPublisher
from foundry_opt.config.models import OptimizerConfig
from foundry_opt.drafts import DraftRecord, DraftRequest
from foundry_opt.optimization.assets import AssetIdentity
from foundry_opt.optimization.commands import (
    OptimizeCommandRequest,
    OptimizeCommandStatus,
    OptimizePhase,
)
from foundry_opt.optimization.lifecycle import (
    CandidateApplyService,
    CandidateReconcileService,
)
from foundry_opt.optimization.models import (
    ApprovalGate,
    AssetKind,
    AssetProvenance,
    EvaluationAssetContext,
    EvaluationAssetRequest,
    OptimizationSpec,
)
from foundry_opt.optimization.production import (
    GitSpecApprovalGateway,
    ProductionOptimizationCommandService,
    _DraftCreator,
    _EvaluationBinder,
    _RegistrationGateway,
    build_issue_optimization_dependencies,
    build_optimization_command_service,
    build_specification_asset_registry,
)
from foundry_opt.orchestration import StateRefError
from foundry_opt.optimization.runner import (
    CapabilityUnavailableError,
    IssueOptimizationRunner,
)
from foundry_opt.optimization.specification import (
    OptimizationSpecService,
    spec_file_path,
)
from foundry_opt.packaging import BundleArtifact

BASE_COMMIT = "b" * 40
DEFAULT_COMMIT = "a" * 40
APPROVAL_COMMIT = "c" * 40
ACCEPTANCE_ENDPOINT = "https://acc.services.ai.azure.com/api/projects/acc"
PROD_ENDPOINT = "https://prod.services.ai.azure.com/api/projects/prod"


# ---------------------------------------------------------------------------
# Config fixtures
# ---------------------------------------------------------------------------


def _target_document(
    environment: str,
    *,
    base_agent_version: str = "12",
    runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "environment": environment,
        "source_paths": ["agent"],
        "edit_paths": ["agent"],
        "entry_point": "agent/main.py",
        "base_agent_version": base_agent_version,
        "package": {"include": ["agent/**"], "exclude": []},
        "datasets": {
            "development": [{"name": "dev", "version": "v1", "mode": "batch"}],
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
    if runtime is not None:
        document["runtime"] = runtime
    return document


def _environment_document(endpoint: str, workflow: str) -> dict[str, Any]:
    return {
        "project_endpoint": endpoint,
        "project_resource_id": (
            "/subscriptions/sub/resourceGroups/rg/providers/"
            "Microsoft.CognitiveServices/accounts/foundry/projects/demo"
        ),
        "allowed_models": ["gpt-5.1"],
        "deployment_workflow": {"path": workflow, "trigger": "manual"},
    }


def _config_dict() -> dict[str, Any]:
    return {
        "schema_version": "1",
        "default_environment": "acceptance",
        "environments": {
            "acceptance": _environment_document(
                ACCEPTANCE_ENDPOINT, ".github/workflows/deploy.yml"
            ),
            "prod": _environment_document(
                PROD_ENDPOINT, ".github/workflows/deploy-prod.yml"
            ),
        },
        "targets": {
            "support-agent": _target_document(
                "acceptance",
                runtime={
                    "runtime": "python_3_11",
                    "dependency_resolution": "bundled",
                    "cpu": "2",
                    "memory": "4Gi",
                    "protocol": "responses",
                    "protocol_version": "2.0.0",
                },
            ),
            "billing-agent": _target_document("prod", base_agent_version="9"),
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


def _config() -> OptimizerConfig:
    return OptimizerConfig.model_validate(_config_dict())


def _write_config(root: Path) -> Path:
    config_path = root / ".github" / "foundry-optimizer.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(_config_dict()), encoding="utf-8")
    return config_path


# ---------------------------------------------------------------------------
# Spec + bundle fixtures
# ---------------------------------------------------------------------------


def _spec(
    *,
    target: str = "support-agent",
    environment: str = "acceptance",
    issue_number: int = 7,
) -> OptimizationSpec:
    return OptimizationSpec(
        issue_number=issue_number,
        repository="octo-org/optimizer",
        base_commit=BASE_COMMIT,
        target=target,
        environment=environment,
        base_agent_version="12",
        goal=(
            "Improve response quality for the support agent while preserving "
            "safety guardrails across every candidate."
        ),
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
            "quality": {
                "direction": "maximize",
                "threshold": 0.8,
                "materiality": 0.05,
                "hard_guardrail": False,
                "undefined_behavior": "fail",
            }
        },
        allowed_mutations=frozenset({"system_instructions"}),
    )


def _bundle() -> BundleArtifact:
    return BundleArtifact(
        path=Path("agent/.foundry-opt-baseline.zip"),
        sha256="d" * 64,
        included_files=("agent/main.py",),
        excluded_files=(),
        byte_size=10,
        manifest_path=Path("agent/.foundry-opt-baseline.manifest.json"),
    )


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _RecordingDraftGateway:
    def __init__(self) -> None:
        self.requests: list[DraftRequest] = []

    def create_draft(self, request: DraftRequest) -> DraftRecord:
        self.requests.append(request)
        return DraftRecord(
            agent_name=request.agent_name,
            version_id=f"draft-{request.agent_name}",
            base_version=request.base_version,
            sha256=request.bundle.sha256,
            status="ready",
            project_endpoint=request.project_endpoint,
        )


class _FailingDraftGateway:
    def create_draft(self, request: DraftRequest) -> DraftRecord:
        raise DraftAuthenticationError()


class _RecordingRegistrationGateway:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self.calls: list[tuple[str, str, str]] = []

    def register(
        self, *, kind: AssetKind, name: str, version: str, content: Any
    ) -> AssetIdentity:
        self.calls.append((kind.value, name, version))
        return AssetIdentity(
            remote_id=f"registered:{name}:{version}",
            name=name,
            version=version,
            content_sha256=None,
        )


class _FailingRegistrationGateway:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint

    def register(
        self, *, kind: AssetKind, name: str, version: str, content: Any
    ) -> AssetIdentity:
        raise FoundryAssetAuthenticationError()


class _RecordingResolutionGateway:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self.calls: list[tuple[str, str, str]] = []

    def resolve(self, *, kind: AssetKind, name: str, version: str) -> str:
        self.calls.append((kind.value, name, version))
        return f"resolved:{self.endpoint}:{name}:{version}"


class _RecordingBinder:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self.calls: list[tuple[OptimizationSpec, Any]] = []

    def __call__(self, spec: OptimizationSpec, assets: Any) -> Any:
        self.calls.append((spec, assets))

        def evaluate(subject: Any, split: Any, attempt: int) -> Any:
            return ("evaluated", subject, split, attempt)

        return evaluate


class _FailingBinder:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint

    def __call__(self, spec: OptimizationSpec, assets: Any) -> Any:
        def evaluate(subject: Any, split: Any, attempt: int) -> Any:
            raise OptimizationEvaluationError("Azure credential missing")

        return evaluate


class _FactorySpy:
    def __init__(self, gateway_cls: Any) -> None:
        self.endpoints: list[str] = []
        self.gateways: list[Any] = []
        self._gateway_cls = gateway_cls

    def __call__(self, endpoint: str) -> Any:
        self.endpoints.append(endpoint)
        gateway = self._gateway_cls(endpoint)
        self.gateways.append(gateway)
        return gateway


class _NoTenantEnvironment:
    def get(self, name: str) -> str | None:
        return None


class _FakeCommandResult:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.exit_code = 0


class _FakeCommands:
    def __init__(
        self,
        responses: dict[tuple[str, ...], str],
        *,
        failing: set[tuple[str, ...]] | None = None,
    ) -> None:
        self._responses = responses
        self._failing = failing or set()
        self.calls: list[tuple[str, ...]] = []

    def run(self, arguments: Any, *, cwd: Any = None, **kwargs: Any) -> Any:
        key = tuple(arguments)
        self.calls.append(key)
        if key in self._failing:
            raise CommandExitError(
                list(key), exit_code=1, stdout="", stderr="failed"
            )
        if key in self._responses:
            return _FakeCommandResult(self._responses[key])
        raise AssertionError(f"unexpected command: {key}")


def _fake_credential_provider() -> Any:
    class _Provider:
        def create(self) -> Any:  # pragma: no cover - never called by fakes
            raise AssertionError("fake seams must not create a credential")

    return _Provider()


# ---------------------------------------------------------------------------
# Service assembly
# ---------------------------------------------------------------------------


def test_build_optimization_command_service_is_available() -> None:
    service = build_optimization_command_service()
    assert isinstance(service, ProductionOptimizationCommandService)
    assert service._steward._deployment is not None


def test_cli_steward_builder_wires_canonical_deployment() -> None:
    from foundry_opt.cli import build_steward_advance_service
    from foundry_opt.optimization.production import (
        DeploymentIdentityCredentialProvider,
    )

    service = build_steward_advance_service()

    assert service._deployment is not None
    assert not isinstance(
        service._deployment._credential,
        DeploymentIdentityCredentialProvider,
    )


def test_missing_configuration_is_blocked(tmp_path: Path) -> None:
    service = build_optimization_command_service()

    result = service.execute(
        OptimizeCommandRequest(
            repository_root=tmp_path,
            issue_number=7,
            phase=OptimizePhase.RUN,
        )
    )

    assert result.status is OptimizeCommandStatus.BLOCKED
    assert result.details["code"] == "configuration_unavailable"


def test_missing_configuration_json_contract(tmp_path: Path) -> None:
    service = build_optimization_command_service()

    result = service.execute(
        OptimizeCommandRequest(
            repository_root=tmp_path,
            issue_number=7,
            phase=OptimizePhase.AUTO,
        )
    )

    payload = result.to_dict()
    assert payload["status"] == "blocked"
    assert payload["phase"] == "auto"
    assert payload["issue_number"] == 7
    assert payload["details"]["code"] == "configuration_unavailable"
    # The JSON contract round-trips through the CLI serializer.
    assert json.loads(json.dumps(payload)) == payload


def test_run_blocks_when_spec_not_prepared(tmp_path: Path) -> None:
    _write_config(tmp_path)

    service = build_optimization_command_service()
    result = service.execute(
        OptimizeCommandRequest(
            repository_root=tmp_path,
            issue_number=7,
            phase=OptimizePhase.RUN,
        )
    )
    assert result.status is OptimizeCommandStatus.BLOCKED
    assert result.details["code"] == "spec_not_prepared"


def test_spec_phase_blocks_when_git_tools_unavailable(tmp_path: Path) -> None:
    # ``tmp_path`` is not a Git repository, so the SPEC phase's GitHub/git
    # tool calls fail; the production service must surface that as a typed
    # ``blocked`` result rather than crashing the CLI with a traceback.
    _write_config(tmp_path)

    service = build_optimization_command_service()
    result = service.execute(
        OptimizeCommandRequest(
            repository_root=tmp_path,
            issue_number=7,
            phase=OptimizePhase.SPEC,
        )
    )
    assert result.status is OptimizeCommandStatus.BLOCKED
    # The block is typed (either surfaced by the spec service or the
    # production boundary safety net) and never an untyped crash.
    assert isinstance(result.details.get("code"), str)
    assert result.details["code"]


# ---------------------------------------------------------------------------
# Live adapter assembly (no unavailable seams)
# ---------------------------------------------------------------------------


def test_dependencies_use_live_foundry_and_publication_adapters() -> None:
    from foundry_opt.adapters.optimization_deployment import (
        LiveDeploymentCoordinator,
    )
    from foundry_opt.adapters.post_deploy_evaluation import (
        LivePostDeployEvaluator,
    )

    dependencies = build_issue_optimization_dependencies(_config())

    assert isinstance(dependencies.spec_service, OptimizationSpecService)
    assert isinstance(dependencies.spec_gateway, GitSpecApprovalGateway)
    assert isinstance(dependencies.registration_gateway, _RegistrationGateway)
    assert isinstance(dependencies.create_draft, _DraftCreator)
    assert isinstance(dependencies.bind_evaluation, _EvaluationBinder)
    assert isinstance(dependencies.publish, CampaignPublisher)
    # The APPLY/RECONCILE lifecycle services are wired (issue-deployment).
    assert isinstance(dependencies.apply_service, CandidateApplyService)
    assert isinstance(
        dependencies.reconcile_service, CandidateReconcileService
    )
    # The lifecycle deployment/post-deployment seams are the live Azure-OIDC
    # adapters, never the unavailable placeholders.
    reconcile_deps = dependencies.reconcile_service._deps
    assert isinstance(reconcile_deps.deployment, LiveDeploymentCoordinator)
    assert isinstance(reconcile_deps.post_deploy, LivePostDeployEvaluator)
    # None of the seams is an "unavailable" placeholder.
    for seam in (
        dependencies.registration_gateway,
        dependencies.create_draft,
        dependencies.bind_evaluation,
        dependencies.publish,
        reconcile_deps.deployment,
        reconcile_deps.post_deploy,
    ):
        assert "Unavailable" not in type(seam).__name__
    # The runner assembles from the production dependencies.
    assert isinstance(
        IssueOptimizationRunner(dependencies), IssueOptimizationRunner
    )


def test_orchestrated_spec_generation_propagates_state_load_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def fail_load(*args, **kwargs):
        raise StateRefError("state unavailable")

    monkeypatch.setattr(
        "foundry_opt.optimization.production.GitStateRef.load",
        fail_load,
    )
    dependencies = build_issue_optimization_dependencies(_config())

    with pytest.raises(StateRefError, match="state unavailable"):
        dependencies.spec_service._generation_provider(tmp_path, 31)


def test_orchestrated_spec_generation_rejects_missing_campaign_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "foundry_opt.optimization.production.GitStateRef.load",
        lambda *args, **kwargs: None,
    )
    dependencies = build_issue_optimization_dependencies(_config())

    with pytest.raises(StateRefError, match="campaign state"):
        dependencies.spec_service._generation_provider(tmp_path, 31)


def test_explicit_non_orchestrated_dependencies_allow_generationless_specs(
) -> None:
    dependencies = build_issue_optimization_dependencies(
        _config(),
        orchestrated=False,
    )

    assert dependencies.spec_service._generation_provider is None


def test_dependencies_share_one_campaign_state_store() -> None:
    # The lifecycle campaign state store and the post-deployment evaluator's
    # state store must be the same instance so a reconcile reads exactly the
    # state a run persisted.
    dependencies = build_issue_optimization_dependencies(_config())
    reconcile_deps = dependencies.reconcile_service._deps
    assert reconcile_deps.post_deploy._state_store is reconcile_deps.state


def test_dependencies_deployment_uses_dedicated_identity_and_gh_gateway() -> (
    None
):
    from foundry_opt.adapters.deployment import DEPLOYMENT_OIDC_CLIENT_ID
    from foundry_opt.adapters.optimization_deployment import (
        FoundryPublishedDeploymentReader,
        GhWorkflowRunGateway,
    )

    dependencies = build_issue_optimization_dependencies(_config())
    coordinator = dependencies.reconcile_service._deps.deployment
    # Real gh workflow-run gateway and Foundry published-version reader.
    assert isinstance(coordinator._workflow_gateway, GhWorkflowRunGateway)
    assert isinstance(coordinator._reader, FoundryPublishedDeploymentReader)
    # The reader enforces the dedicated deployment OIDC identity.
    assert (
        coordinator._reader._deployment_client_id == DEPLOYMENT_OIDC_CLIENT_ID
    )
    # No generated-workflow publisher is wired, so a missing committed
    # deployment workflow remains an honest blocker.
    assert coordinator._publisher is None


def test_deployment_identity_credential_provider_contract() -> None:
    from foundry_opt.optimization.production import (
        DeploymentIdentityCredentialProvider,
    )

    credential = object()

    class _Shared:
        def create(self) -> object:
            return credential

    class _Reader:
        def __init__(self, mapping: dict[str, str]) -> None:
            self._mapping = mapping

        def get(self, name: str) -> str | None:
            return self._mapping.get(name)

    provider = DeploymentIdentityCredentialProvider(
        _Shared(), _Reader({"AZURE_CLIENT_ID": "  the-client-id  "})
    )
    # ``create`` delegates verbatim to the shared Azure OIDC provider.
    assert provider.create() is credential
    # ``active_client_id`` reports the trimmed OIDC-login principal.
    assert provider.active_client_id() == "the-client-id"
    # Missing AZURE_CLIENT_ID reports empty so the reader fails closed.
    missing = DeploymentIdentityCredentialProvider(_Shared(), _Reader({}))
    assert missing.active_client_id() == ""


def test_deployment_identity_provider_satisfies_reader_identity_check() -> None:
    from foundry_opt.adapters.deployment import (
        DEPLOYMENT_OIDC_CLIENT_ID,
        DeploymentIdentityError,
        _verify_active_principal,
    )
    from foundry_opt.optimization.production import (
        DeploymentIdentityCredentialProvider,
    )

    class _Shared:
        def create(self) -> object:  # pragma: no cover - never called here
            raise AssertionError("identity check must not create a credential")

    class _Reader:
        def __init__(self, client_id: str | None) -> None:
            self._client_id = client_id

        def get(self, name: str) -> str | None:
            return self._client_id if name == "AZURE_CLIENT_ID" else None

    # Authenticated as the dedicated deployment identity: the reader's active
    # principal check passes.
    deployment_provider = DeploymentIdentityCredentialProvider(
        _Shared(), _Reader(DEPLOYMENT_OIDC_CLIENT_ID)
    )
    _verify_active_principal(deployment_provider, DEPLOYMENT_OIDC_CLIENT_ID)

    # Any other (or missing) principal fails closed.
    other_provider = DeploymentIdentityCredentialProvider(
        _Shared(), _Reader("00000000-0000-0000-0000-000000000000")
    )
    with pytest.raises(DeploymentIdentityError):
        _verify_active_principal(other_provider, DEPLOYMENT_OIDC_CLIENT_ID)


def test_default_factories_build_real_adapter_types() -> None:
    reader = _NoTenantEnvironment()
    credential = AzureCliCredentialProvider(reader)

    dependencies = build_issue_optimization_dependencies(
        _config(), environment=reader, credential_provider=credential
    )

    # The draft gateway is the live source-bundle gateway.
    assert isinstance(dependencies.create_draft._draft_gateway, DraftGateway)
    # The resolution/registration/binder factories build the live adapters.
    resolution = dependencies.spec_service._registry.get("foundry")
    live_gateway = resolution._gateway_factory(ACCEPTANCE_ENDPOINT)
    assert isinstance(live_gateway, LiveFoundryAssetResolutionGateway)
    registration = dependencies.registration_gateway._factory(
        ACCEPTANCE_ENDPOINT
    )
    assert isinstance(registration, EvaluationAssetRegistrationGateway)
    binder = dependencies.bind_evaluation._binder_factory(ACCEPTANCE_ENDPOINT)
    assert isinstance(binder, OptimizationEvaluationBinder)
    assert binder._evaluator_model_deployment == "gpt-5.1"


def test_default_validator_runs_target_configured_commands(
    tmp_path: Path,
) -> None:
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "main.py").write_text("", encoding="utf-8")
    expected = (("uv", "run", "pytest", "-q"),)
    commands = _FakeCommands({command: "" for command in expected})
    dependencies = build_issue_optimization_dependencies(
        _config(),
        command_runner=commands,
        credential_provider=_fake_credential_provider(),
    )

    report = dependencies.validate(tmp_path)

    assert report.passed is True
    assert commands.calls == list(expected)


def test_default_bundle_builder_uses_target_package_rules(
    tmp_path: Path,
) -> None:
    config_document = _config_dict()
    del config_document["targets"]["billing-agent"]
    config = OptimizerConfig.model_validate(config_document)
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "main.py").write_text(
        "print('agent')\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_agent.py").write_text(
        "raise AssertionError\n",
        encoding="utf-8",
    )
    dependencies = build_issue_optimization_dependencies(
        config,
        command_runner=_FakeCommands({}),
        credential_provider=_fake_credential_provider(),
    )
    output = tmp_path / "candidate.zip"

    dependencies.build_bundle(tmp_path, output)

    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == ["agent/main.py"]


# ---------------------------------------------------------------------------
# Draft creator: exact request from target config + runtime contract
# ---------------------------------------------------------------------------


def test_draft_creator_builds_exact_request_from_target_config() -> None:
    gateway = _RecordingDraftGateway()
    dependencies = build_issue_optimization_dependencies(
        _config(),
        credential_provider=_fake_credential_provider(),
        draft_gateway=gateway,
    )

    record = dependencies.create_draft(
        "support-agent", "baseline", "idem-key", _bundle()
    )

    assert isinstance(record, DraftRecord)
    (request,) = gateway.requests
    assert request.project_endpoint == ACCEPTANCE_ENDPOINT
    assert request.agent_name == "support-agent"
    assert request.base_version == 12
    assert request.bundle.sha256 == "d" * 64
    # Runtime contract comes from the target config, not a hardcoded value.
    assert request.runtime == "python_3_11"
    assert request.entry_point == ("python", "agent/main.py")
    assert request.dependency_resolution == "bundled"
    assert request.cpu == "2"
    assert request.memory == "4Gi"
    assert request.protocol_version == "2.0.0"


def test_draft_creator_routes_to_target_specific_endpoint() -> None:
    gateway = _RecordingDraftGateway()
    dependencies = build_issue_optimization_dependencies(
        _config(),
        credential_provider=_fake_credential_provider(),
        draft_gateway=gateway,
    )

    dependencies.create_draft("billing-agent", "baseline", "k", _bundle())

    (request,) = gateway.requests
    # The billing target lives in the prod environment, not the default one.
    assert request.project_endpoint == PROD_ENDPOINT
    assert request.agent_name == "billing-agent"
    assert request.base_version == 9
    # No runtime block configured -> inherit the published baseline (never a
    # guessed value that would overwrite it).
    assert request.runtime is None
    assert request.dependency_resolution is None
    assert request.cpu is None
    assert request.memory is None
    assert request.protocol is None
    assert request.protocol_version is None
    # entry_point always reflects the target's entry point.
    assert request.entry_point == ("python", "agent/main.py")


def test_draft_creator_blocks_unknown_target() -> None:
    dependencies = build_issue_optimization_dependencies(
        _config(),
        credential_provider=_fake_credential_provider(),
        draft_gateway=_RecordingDraftGateway(),
    )

    with pytest.raises(CapabilityUnavailableError) as excinfo:
        dependencies.create_draft("ghost-agent", "baseline", "k", _bundle())
    assert excinfo.value.code == "unknown_target"


# ---------------------------------------------------------------------------
# Evaluation binder: per-spec endpoint routing
# ---------------------------------------------------------------------------


def test_bind_evaluation_routes_to_spec_environment() -> None:
    spy = _FactorySpy(_RecordingBinder)
    dependencies = build_issue_optimization_dependencies(
        _config(),
        credential_provider=_fake_credential_provider(),
        binder_factory=spy,
    )

    evaluate = dependencies.bind_evaluation(
        _spec(target="billing-agent", environment="prod"), ()
    )

    assert spy.endpoints == [PROD_ENDPOINT]
    assert evaluate("subject", "development", 1)[0] == "evaluated"


# ---------------------------------------------------------------------------
# Registration gateway: environment routing
# ---------------------------------------------------------------------------


def test_registration_gateway_routes_to_target_environment() -> None:
    spy = _FactorySpy(_RecordingRegistrationGateway)
    dependencies = build_issue_optimization_dependencies(
        _config(),
        credential_provider=_fake_credential_provider(),
        registration_gateway_factory=spy,
        target_environment="prod",
    )

    identity = dependencies.registration_gateway.register(
        kind=AssetKind.DATASET,
        name="dataset-x",
        version="abc123",
        content={},
    )

    assert spy.endpoints == [PROD_ENDPOINT]
    assert identity.remote_id == "registered:dataset-x:abc123"


# ---------------------------------------------------------------------------
# Spec-phase asset resolution uses the live gateway per context endpoint
# ---------------------------------------------------------------------------


def test_specification_registry_resolves_foundry_assets_per_endpoint() -> None:
    spy = _FactorySpy(_RecordingResolutionGateway)
    registry = build_specification_asset_registry(
        resolution_gateway_factory=spy
    )

    request = EvaluationAssetRequest(
        asset_id="dataset-dev",
        kind=AssetKind.DATASET,
        source="foundry",
        role="development",
        name="dev-dataset",
        version="3",
    )
    context = EvaluationAssetContext(
        repository_root=Path("."),
        project_endpoint=PROD_ENDPOINT,
        target="billing-agent",
        issue_number=7,
    )

    prepared = registry.prepare(request, context)

    assert spy.endpoints == [PROD_ENDPOINT]
    assert prepared.provenance.remote_id == (
        f"resolved:{PROD_ENDPOINT}:dev-dataset:3"
    )


def test_spec_service_registry_uses_live_resolution_provider() -> None:
    dependencies = build_issue_optimization_dependencies(_config())
    provider = dependencies.spec_service._registry.get("foundry")
    assert type(provider).__name__ == "_PerEndpointFoundryResolutionProvider"


# ---------------------------------------------------------------------------
# Missing OIDC / live failures surface typed BLOCKED (never crash)
# ---------------------------------------------------------------------------


def test_draft_creator_blocks_when_oidc_missing() -> None:
    dependencies = build_issue_optimization_dependencies(
        _config(),
        credential_provider=_fake_credential_provider(),
        draft_gateway=_FailingDraftGateway(),
    )

    with pytest.raises(CapabilityUnavailableError) as excinfo:
        dependencies.create_draft("support-agent", "baseline", "k", _bundle())
    assert excinfo.value.code == "foundry_drafts_unavailable"


def test_registration_gateway_blocks_when_oidc_missing() -> None:
    spy = _FactorySpy(_FailingRegistrationGateway)
    dependencies = build_issue_optimization_dependencies(
        _config(),
        credential_provider=_fake_credential_provider(),
        registration_gateway_factory=spy,
    )

    with pytest.raises(CapabilityUnavailableError) as excinfo:
        dependencies.registration_gateway.register(
            kind=AssetKind.DATASET, name="d", version="1", content={}
        )
    assert excinfo.value.code == "foundry_registration_unavailable"


def test_evaluation_runner_blocks_when_oidc_missing() -> None:
    spy = _FactorySpy(_FailingBinder)
    dependencies = build_issue_optimization_dependencies(
        _config(),
        credential_provider=_fake_credential_provider(),
        binder_factory=spy,
    )

    evaluate = dependencies.bind_evaluation(_spec(), ())
    with pytest.raises(CapabilityUnavailableError) as excinfo:
        evaluate("subject", "development", 1)
    assert excinfo.value.code == "foundry_evaluation_unavailable"


def test_live_registration_blocks_without_azure_tenant() -> None:
    reader = _NoTenantEnvironment()
    dependencies = build_issue_optimization_dependencies(
        _config(),
        environment=reader,
        credential_provider=AzureCliCredentialProvider(reader),
        target_environment="acceptance",
    )

    with pytest.raises(CapabilityUnavailableError) as excinfo:
        dependencies.registration_gateway.register(
            kind=AssetKind.DATASET,
            name="d",
            version="1",
            content={Path("data.jsonl"): b'{"input": "x"}\n'},
        )
    assert excinfo.value.code == "foundry_registration_unavailable"


# ---------------------------------------------------------------------------
# GitSpecApprovalGateway: remote default branch + merge-commit correctness
# ---------------------------------------------------------------------------


def _approval_commands(
    *,
    spec: OptimizationSpec,
    default_branch: str,
    default_commit: str,
    committed_spec: OptimizationSpec | None = None,
    ancestor_ok: bool = True,
) -> _FakeCommands:
    spec_path = spec_file_path(spec.issue_number).as_posix()
    remote_ref = f"refs/remotes/origin/{default_branch}"
    committed = committed_spec or spec
    committed_yaml = yaml.safe_dump(json.loads(committed.canonical_json))
    responses = {
        (
            "git",
            "fetch",
            "--quiet",
            "origin",
            f"{default_branch}:{remote_ref}",
        ): "",
        ("git", "rev-parse", f"{remote_ref}^{{commit}}"): default_commit,
        ("git", "cat-file", "-e", f"{default_commit}:{spec_path}"): "",
        ("git", "show", f"{default_commit}:{spec_path}"): committed_yaml,
        (
            "git",
            "rev-list",
            "-1",
            default_commit,
            "--",
            spec_path,
        ): APPROVAL_COMMIT,
    }
    ancestor_key = (
        "git",
        "merge-base",
        "--is-ancestor",
        spec.base_commit,
        default_commit,
    )
    failing: set[tuple[str, ...]] = set()
    if ancestor_ok:
        responses[ancestor_key] = ""
    else:
        failing.add(ancestor_key)
    return _FakeCommands(responses, failing=failing)


def test_spec_approval_uses_remote_default_branch_not_local_head() -> None:
    spec = _spec()
    commands = _approval_commands(
        spec=spec, default_branch="trunk", default_commit=DEFAULT_COMMIT
    )
    gateway = GitSpecApprovalGateway(
        commands, default_branch=lambda root: "trunk"
    )

    result = gateway.verify_spec_approval(
        Path("."),
        issue_number=spec.issue_number,
        spec=spec,
        spec_sha256=spec.sha256,
        base_commit=DEFAULT_COMMIT,
    )

    assert result.approved is True
    # The default branch is the real remote branch, never a hardcoded "main".
    assert result.default_branch == "trunk"
    # The approval commit is the actual merge commit, not the base commit.
    assert result.approval_commit == APPROVAL_COMMIT


def test_spec_approval_rejects_base_commit_off_default_branch() -> None:
    spec = _spec()
    commands = _approval_commands(
        spec=spec, default_branch="main", default_commit=DEFAULT_COMMIT
    )
    gateway = GitSpecApprovalGateway(
        commands, default_branch=lambda root: "main"
    )

    result = gateway.verify_spec_approval(
        Path("."),
        issue_number=spec.issue_number,
        spec=spec,
        spec_sha256=spec.sha256,
        base_commit="f" * 40,
    )

    assert result.approved is False
    assert result.approval_commit is None


def test_spec_approval_requires_base_commit_ancestor() -> None:
    spec = _spec()
    commands = _approval_commands(
        spec=spec,
        default_branch="main",
        default_commit=DEFAULT_COMMIT,
        ancestor_ok=False,
    )
    gateway = GitSpecApprovalGateway(
        commands, default_branch=lambda root: "main"
    )

    result = gateway.verify_spec_approval(
        Path("."),
        issue_number=spec.issue_number,
        spec=spec,
        spec_sha256=spec.sha256,
        base_commit=DEFAULT_COMMIT,
    )

    assert result.approved is False
    assert "ancestor" in (result.reason or "")


def test_spec_approval_rejects_hash_mismatch() -> None:
    spec = _spec()
    commands = _approval_commands(
        spec=spec,
        default_branch="main",
        default_commit=DEFAULT_COMMIT,
        committed_spec=_spec(target="billing-agent", environment="prod"),
    )
    gateway = GitSpecApprovalGateway(
        commands, default_branch=lambda root: "main"
    )

    result = gateway.verify_spec_approval(
        Path("."),
        issue_number=spec.issue_number,
        spec=spec,
        spec_sha256=spec.sha256,
        base_commit=DEFAULT_COMMIT,
    )

    assert result.approved is False
    assert "hash" in (result.reason or "")


def test_spec_approval_blocks_when_default_branch_unresolved() -> None:
    spec = _spec()

    def _fail(root: Path) -> str:
        raise RuntimeError("gh not authenticated")

    gateway = GitSpecApprovalGateway(_FakeCommands({}), default_branch=_fail)

    result = gateway.verify_spec_approval(
        Path("."),
        issue_number=spec.issue_number,
        spec=spec,
        spec_sha256=spec.sha256,
        base_commit=DEFAULT_COMMIT,
    )

    assert result.approved is False
    assert "default branch" in (result.reason or "")
