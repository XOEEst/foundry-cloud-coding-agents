"""Production assembly for the issue-driven optimization command service.

This wires :class:`~foundry_opt.optimization.runner.IssueOptimizationRunner`
with the completed *live* adapters: the OIDC-backed Foundry evaluation asset
resolution/registration gateways
(:mod:`foundry_opt.adapters.foundry_assets`), the source-bundle draft gateway
(:mod:`foundry_opt.adapters.drafts`), the per-specification Foundry evaluation
binder (:mod:`foundry_opt.adapters.optimization_evaluation`), and the campaign
publisher (:mod:`foundry_opt.adapters.optimization_publication`). The APPLY and
RECONCILE lifecycle services are wired through
:func:`foundry_opt.optimization.lifecycle.build_lifecycle_services`, including
their live Azure-OIDC deployment coordinator
(:class:`~foundry_opt.adapters.optimization_deployment.LiveDeploymentCoordinator`)
and post-deployment evaluator
(:class:`~foundry_opt.adapters.post_deploy_evaluation.LivePostDeployEvaluator`);
no lifecycle seam is left as an unavailable placeholder on a production path.

Every Foundry seam is bound to the project endpoint of the *target's* approved
environment rather than a single default endpoint: because a project endpoint
is target-specific, the draft creator, the evaluation binder, and the spec
service's resolution provider each resolve their endpoint lazily from the
approved specification (or the requested target) at call time, and the
registration gateway is bound to the spec's environment for the execution.

Azure authentication is kept explicit and OIDC-only: a single shared
:class:`~foundry_opt.adapters.foundry.AzureCliCredentialProvider` (reading
``AZURE_TENANT_ID`` from the process/Agents environment) is threaded into
every Foundry seam, and each adapter opens and closes its own client and
credential per operation. When the live precondition is missing (no OIDC
tenant, unreachable Foundry, missing tool) the seam raises the typed
:class:`~foundry_opt.optimization.runner.CapabilityUnavailableError` so the
runner surfaces an honest ``blocked`` result instead of crashing or
fabricating success.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from foundry_opt.adapters.campaign_git import CampaignGit
from foundry_opt.adapters.commands import SubprocessCommandRunner
from foundry_opt.adapters.drafts import DraftError, DraftGateway
from foundry_opt.adapters.environment import OsEnvironmentReader
from foundry_opt.adapters.foundry import (
    AzureCliCredentialProvider,
    AzureCredentialProvider,
    FoundryAccessError,
)
from foundry_opt.adapters.foundry_assets import (
    EvaluationAssetRegistrationGateway,
    FoundryAssetGatewayError,
    FoundryAssetResolutionGateway,
)
from foundry_opt.adapters.github_optimization import (
    GhOptimizationGateway,
    GitSpecPublisher,
)
from foundry_opt.adapters.optimization_evaluation import (
    OptimizationEvaluationBinder,
    OptimizationEvaluationError,
)
from foundry_opt.adapters.optimization_publication import CampaignPublisher
from foundry_opt.campaign.state import FileCampaignStateStore
from foundry_opt.config import load_config
from foundry_opt.config.loader import ConfigLoadError
from foundry_opt.config.models import AgentTarget, OptimizerConfig
from foundry_opt.drafts import DraftRecord, DraftRequest
from foundry_opt.evidence.writer import write_redacted_evidence
from foundry_opt.github_workflow.models import GitHubCapabilities
from foundry_opt.optimization.assets import (
    BuiltinEvaluatorProvider,
    CustomEvaluatorAssetProvider,
    EvaluationAssetProviderRegistry,
    ExistingFoundryAssetProvider,
    RepositoryAssetProvider,
    SyntheticDatasetProvider,
    TraceEvaluationAssetProvider,
)
from foundry_opt.optimization.commands import (
    OptimizationCommandService,
    OptimizeCommandRequest,
    OptimizeCommandResult,
    OptimizeCommandStatus,
)
from foundry_opt.optimization.models import (
    AssetKind,
    EvaluationAssetContext,
    EvaluationAssetRequest,
    OptimizationSpec,
    PreparedEvaluationAsset,
)
from foundry_opt.optimization.runner import (
    CapabilityUnavailableError,
    IssueOptimizationDependencies,
    IssueOptimizationRunner,
    SpecApprovalResult,
)
from foundry_opt.optimization.specification import (
    OptimizationSpecService,
    spec_file_path,
)
from foundry_opt.packaging import (
    BundleArtifact,
    BundleRequest,
    ValidationRequest,
    build_source_bundle,
    run_validation,
)
from foundry_opt.preflight.interfaces import CommandRunner, EnvironmentReader
from foundry_opt.preflight.redaction import redact
from foundry_opt.evidence import EvaluationAssetReference


_SPEC_CAPABILITIES = (
    GitHubCapabilities.METADATA_READ
    | GitHubCapabilities.ISSUES_WRITE
    | GitHubCapabilities.CONTENTS_WRITE
    | GitHubCapabilities.PULL_REQUESTS_WRITE
)
_DEFAULT_CONFIG_PATH = Path(".github/foundry-optimizer.yaml")
_COMMIT_LENGTH = 40
_HEX = "0123456789abcdef"


class UtcClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


# Factory type aliases (endpoint-parameterised because a Foundry project
# endpoint is target-specific).
ResolutionGatewayFactory = Callable[[str], Any]
RegistrationGatewayFactory = Callable[[str], Any]
BinderFactory = Callable[[str], Any]


# ---------------------------------------------------------------------------
# Git-based spec approval gateway
# ---------------------------------------------------------------------------


class GitSpecApprovalGateway:
    """Confirms a specification was merged onto the *remote* default branch.

    Approval is recorded only by a maintainer merging the specification pull
    request, so this gateway trusts committed content on the repository's
    default branch — never the local working tree, the current local ``HEAD``,
    or a hardcoded branch name. It resolves the GitHub default branch and its
    exact remote-tracking commit, confirms the pinned campaign base commit is
    that commit, verifies the spec file is committed there (and hashes to the
    pinned specification hash), requires the spec's own ``base_commit`` to be
    an ancestor of the current default commit, and returns the actual commit
    that last modified the spec on the default branch as the approval merge
    commit.
    """

    def __init__(
        self,
        command_runner: CommandRunner,
        *,
        default_branch: Callable[[Path], str] | None = None,
    ) -> None:
        self._commands = command_runner
        self._default_branch = default_branch or self._github_default_branch

    def verify_spec_approval(
        self,
        repository_root: Path,
        *,
        issue_number: int,
        spec: OptimizationSpec,
        spec_sha256: str,
        base_commit: str,
    ) -> SpecApprovalResult:
        spec_path = spec_file_path(issue_number).as_posix()

        try:
            default_branch = self._default_branch(repository_root).strip()
        except Exception:
            return SpecApprovalResult(
                approved=False,
                reason=(
                    "the GitHub default branch could not be resolved; ensure "
                    "the GitHub CLI is authenticated"
                ),
            )
        if not default_branch:
            return SpecApprovalResult(
                approved=False,
                reason="the GitHub default branch could not be resolved",
            )

        default_commit = self._resolve_remote_default_commit(
            repository_root, default_branch
        )
        if default_commit is None:
            return SpecApprovalResult(
                approved=False,
                reason=(
                    "the remote default-branch commit could not be resolved; "
                    "ensure the repository default branch is reachable"
                ),
            )
        if default_commit != base_commit:
            return SpecApprovalResult(
                approved=False,
                reason=(
                    "the campaign base commit is not the current "
                    f"{default_branch!r} default-branch commit; re-run after "
                    "syncing the default branch"
                ),
            )

        object_ref = f"{default_commit}:{spec_path}"
        try:
            self._commands.run(
                ("git", "cat-file", "-e", object_ref),
                cwd=repository_root,
            )
        except Exception:
            return SpecApprovalResult(
                approved=False,
                reason=(
                    "the approved specification is not present on the "
                    "default branch; merge the specification pull request "
                    "first"
                ),
            )
        try:
            committed = self._commands.run(
                ("git", "show", object_ref),
                cwd=repository_root,
            ).stdout
            committed_spec = OptimizationSpec.model_validate(
                yaml.safe_load(committed)
            )
        except Exception:
            return SpecApprovalResult(
                approved=False,
                reason="the committed specification could not be read",
            )
        if committed_spec.sha256 != spec_sha256:
            return SpecApprovalResult(
                approved=False,
                reason=(
                    "the committed specification does not match the pinned "
                    "hash"
                ),
            )

        if not self._is_ancestor(
            repository_root, spec.base_commit, default_commit
        ):
            return SpecApprovalResult(
                approved=False,
                reason=(
                    "the specification base commit is not an ancestor of the "
                    "current default branch; rebase and re-approve the "
                    "specification"
                ),
            )

        try:
            approval_commit = self._commands.run(
                ("git", "rev-list", "-1", default_commit, "--", spec_path),
                cwd=repository_root,
            ).stdout.strip()
        except Exception:
            return SpecApprovalResult(
                approved=False,
                reason="the specification approval commit could not be read",
            )
        if not _is_commit(approval_commit):
            return SpecApprovalResult(
                approved=False,
                reason="the specification approval commit could not be read",
            )
        return SpecApprovalResult(
            approved=True,
            default_branch=default_branch,
            approval_commit=approval_commit,
        )

    def _resolve_remote_default_commit(
        self,
        repository_root: Path,
        default_branch: str,
    ) -> str | None:
        remote_ref = f"refs/remotes/origin/{default_branch}"
        try:
            self._commands.run(
                (
                    "git",
                    "fetch",
                    "--quiet",
                    "origin",
                    f"{default_branch}:{remote_ref}",
                ),
                cwd=repository_root,
            )
        except Exception:
            return None
        try:
            commit = self._commands.run(
                ("git", "rev-parse", f"{remote_ref}^{{commit}}"),
                cwd=repository_root,
            ).stdout.strip()
        except Exception:
            return None
        return commit if _is_commit(commit) else None

    def _is_ancestor(
        self,
        repository_root: Path,
        ancestor: str,
        descendant: str,
    ) -> bool:
        try:
            self._commands.run(
                (
                    "git",
                    "merge-base",
                    "--is-ancestor",
                    ancestor,
                    descendant,
                ),
                cwd=repository_root,
            )
        except Exception:
            return False
        return True

    def _github_default_branch(self, repository_root: Path) -> str:
        return self._commands.run(
            (
                "gh",
                "repo",
                "view",
                "--json",
                "defaultBranchRef",
                "--jq",
                ".defaultBranchRef.name",
            ),
            cwd=repository_root,
        ).stdout.strip()


def _is_commit(value: str) -> bool:
    return (
        len(value) == _COMMIT_LENGTH
        and all(character in _HEX for character in value)
    )


# ---------------------------------------------------------------------------
# Foundry evaluation asset resolution provider (spec service)
# ---------------------------------------------------------------------------


class _PerEndpointFoundryResolutionProvider:
    """A ``foundry`` asset provider that resolves per the context endpoint.

    The spec service's :class:`EvaluationAssetContext` already carries the
    approved target's project endpoint; this provider builds the real
    :class:`FoundryAssetResolutionGateway` for exactly that endpoint (never a
    default one) and delegates the existing pin-by-identity behaviour to the
    stock :class:`ExistingFoundryAssetProvider`.
    """

    source_type = "foundry"

    def __init__(self, gateway_factory: ResolutionGatewayFactory) -> None:
        self._gateway_factory = gateway_factory

    def prepare(
        self,
        request: EvaluationAssetRequest,
        context: EvaluationAssetContext,
    ) -> PreparedEvaluationAsset:
        gateway = self._gateway_factory(context.project_endpoint)
        delegate = ExistingFoundryAssetProvider(gateway=gateway)
        try:
            return delegate.prepare(request, context)
        except (FoundryAccessError, FoundryAssetGatewayError) as error:
            raise CapabilityUnavailableError(
                "foundry_resolution_unavailable",
                "resolving an existing Foundry asset requires live Microsoft "
                "Foundry access (Azure OIDC): " + redact(str(error)),
            ) from error


def build_specification_asset_registry(
    *,
    resolution_gateway_factory: ResolutionGatewayFactory,
    synthetic_max_rows: int = 200,
) -> EvaluationAssetProviderRegistry:
    """Build the spec-service asset registry wired to the live resolution
    gateway.

    This mirrors :func:`foundry_opt.optimization.assets.build_default_registry`
    but replaces the fixed-endpoint Foundry provider with one that resolves
    against the approved target's project endpoint at preparation time.
    """

    registry = EvaluationAssetProviderRegistry()
    registry.register(
        _PerEndpointFoundryResolutionProvider(resolution_gateway_factory)
    )
    registry.register(RepositoryAssetProvider())
    registry.register(SyntheticDatasetProvider(max_rows=synthetic_max_rows))
    registry.register(TraceEvaluationAssetProvider())
    registry.register(CustomEvaluatorAssetProvider())
    registry.register(BuiltinEvaluatorProvider())
    return registry


# ---------------------------------------------------------------------------
# Endpoint + draft-request helpers
# ---------------------------------------------------------------------------


def _environment_endpoint(config: OptimizerConfig, environment_name: str) -> str:
    environment = config.environments.get(environment_name)
    if environment is None:
        raise ValueError(f"environment {environment_name!r} is not configured")
    return str(environment.project_endpoint)


def _draft_request(
    target_name: str,
    target: AgentTarget,
    endpoint: str,
    bundle: BundleArtifact,
    *,
    idempotency_key: str | None = None,
    subject: str | None = None,
) -> DraftRequest:
    """Construct the exact ``DraftRequest`` for a campaign candidate.

    The Foundry agent name is the target name (the onboarding convention binds
    each target to the identically-named hosted agent), the base published
    version and entry point come from the target, and the source bundle is the
    candidate worktree bundle. The runtime contract comes from the target's
    :class:`~foundry_opt.config.models.AgentRuntime`, whose fields default to
    ``None`` (*inherit the published baseline*) and are forwarded as-is: the
    draft gateway keeps the baseline's runtime/dependency for every field left
    unset and overrides only the fields a target explicitly configures, while
    ``entry_point`` always reflects the new bundle and the hosted
    CPU/memory/protocol stay inherited.
    """

    runtime = target.runtime
    return DraftRequest(
        project_endpoint=endpoint,
        agent_name=target_name,
        base_version=int(target.base_agent_version),
        bundle=bundle,
        runtime=runtime.runtime,
        entry_point=("python", target.entry_point.as_posix()),
        dependency_resolution=runtime.dependency_resolution,
        cpu=runtime.cpu,
        memory=runtime.memory,
        protocol=runtime.protocol,
        protocol_version=runtime.protocol_version,
        idempotency_key=idempotency_key,
        subject=subject,
    )


# ---------------------------------------------------------------------------
# Per-spec Foundry seams (draft / evaluation / registration)
# ---------------------------------------------------------------------------


class _DraftCreator:
    """Builds the exact per-target ``DraftRequest`` and calls the live gateway."""

    def __init__(
        self,
        config: OptimizerConfig,
        draft_gateway: Any,
    ) -> None:
        self._config = config
        self._draft_gateway = draft_gateway

    def __call__(
        self,
        target_name: str,
        subject_id: str,
        idempotency_key: str,
        bundle: BundleArtifact,
    ) -> DraftRecord:
        target = self._config.targets.get(target_name)
        if target is None:
            raise CapabilityUnavailableError(
                "unknown_target",
                f"target {target_name!r} is not configured",
            )
        try:
            endpoint = _environment_endpoint(self._config, target.environment)
            request = _draft_request(
                target_name,
                target,
                endpoint,
                bundle,
                idempotency_key=idempotency_key,
                subject=subject_id,
            )
        except (KeyError, ValueError) as error:
            raise CapabilityUnavailableError(
                "draft_request_invalid",
                f"the draft request for target {target_name!r} could not be "
                "constructed: " + redact(str(error)),
            ) from error
        try:
            return self._draft_gateway.create_draft(request)
        except (FoundryAccessError, DraftError) as error:
            raise CapabilityUnavailableError(
                "foundry_drafts_unavailable",
                "creating an agent draft requires live Microsoft Foundry "
                "access (Azure OIDC): " + redact(str(error)),
            ) from error


class _EvaluationBinder:
    """Binds an approved spec to the live evaluation binder for its endpoint."""

    def __init__(
        self,
        config: OptimizerConfig,
        binder_factory: BinderFactory,
    ) -> None:
        self._config = config
        self._binder_factory = binder_factory

    def __call__(
        self,
        spec: OptimizationSpec,
        assets: Sequence[EvaluationAssetReference],
    ) -> Callable[..., Any]:
        try:
            endpoint = _environment_endpoint(self._config, spec.environment)
            binder = self._binder_factory(endpoint)
            runner = binder(spec, assets)
        except (
            ValueError,
            FoundryAccessError,
            OptimizationEvaluationError,
        ) as error:
            raise CapabilityUnavailableError(
                "foundry_evaluation_unavailable",
                "the per-job Foundry evaluation binding requires live "
                "Microsoft Foundry access (Azure OIDC): " + redact(str(error)),
            ) from error

        def evaluate(subject: Any, split: Any, attempt: int) -> Any:
            try:
                return runner(subject, split, attempt)
            except (FoundryAccessError, OptimizationEvaluationError) as error:
                raise CapabilityUnavailableError(
                    "foundry_evaluation_unavailable",
                    "the per-job Foundry evaluation requires live Microsoft "
                    "Foundry access (Azure OIDC): " + redact(str(error)),
                ) from error

        return evaluate


class _RegistrationGateway:
    """Registers approved assets against the spec environment's endpoint."""

    def __init__(
        self,
        config: OptimizerConfig,
        environment_name: str,
        registration_gateway_factory: RegistrationGatewayFactory,
    ) -> None:
        self._config = config
        self._environment = environment_name
        self._factory = registration_gateway_factory

    def register(
        self,
        *,
        kind: AssetKind,
        name: str,
        version: str,
        content: Mapping[Path, bytes],
    ) -> Any:
        try:
            endpoint = _environment_endpoint(self._config, self._environment)
            gateway = self._factory(endpoint)
        except ValueError as error:
            raise CapabilityUnavailableError(
                "foundry_registration_unavailable",
                "the approved environment endpoint could not be resolved: "
                + redact(str(error)),
            ) from error
        try:
            return gateway.register(
                kind=kind,
                name=name,
                version=version,
                content=content,
            )
        except (FoundryAccessError, FoundryAssetGatewayError) as error:
            raise CapabilityUnavailableError(
                "foundry_registration_unavailable",
                "registering an evaluation asset requires live Microsoft "
                "Foundry access (Azure OIDC): " + redact(str(error)),
            ) from error


# ---------------------------------------------------------------------------
# Default adapter factories (single shared OIDC credential provider)
# ---------------------------------------------------------------------------


def _default_credential_provider(
    environment: EnvironmentReader,
) -> AzureCredentialProvider:
    return AzureCliCredentialProvider(environment)


class DeploymentIdentityCredentialProvider:
    """Adapts the shared Azure OIDC provider to the deployment identity seam.

    The live published-deployment reader
    (:class:`~foundry_opt.adapters.optimization_deployment.FoundryPublishedDeploymentReader`)
    fails closed unless the *active* Azure principal is the dedicated
    deployment OIDC identity. This adapter threads the same explicit Azure
    OIDC credential provider used by every other Foundry seam — ``create``
    delegates verbatim so credentials are minted through the identical OIDC
    mechanism — while ``active_client_id`` reports the principal the OIDC login
    established (the ``AZURE_CLIENT_ID`` the ``azure/login`` step exports). When
    the reconcile actor is not authenticated as the deployment identity the
    reader raises
    :class:`~foundry_opt.adapters.deployment.DeploymentIdentityError`, which the
    coordinator surfaces as an honest ``blocked`` capability rather than a
    forged verification.
    """

    def __init__(
        self,
        credential_provider: AzureCredentialProvider,
        environment: EnvironmentReader,
    ) -> None:
        self._credential_provider = credential_provider
        self._environment = environment

    def create(self) -> Any:
        return self._credential_provider.create()

    def active_client_id(self) -> str:
        return (self._environment.get("AZURE_CLIENT_ID") or "").strip()


def _default_resolution_gateway_factory(
    credential_provider: AzureCredentialProvider,
) -> ResolutionGatewayFactory:
    return lambda endpoint: FoundryAssetResolutionGateway(
        endpoint, credential_provider
    )


def _default_registration_gateway_factory(
    credential_provider: AzureCredentialProvider,
) -> RegistrationGatewayFactory:
    return lambda endpoint: EvaluationAssetRegistrationGateway(
        endpoint, credential_provider
    )


def _default_binder_factory(
    credential_provider: AzureCredentialProvider,
    config: OptimizerConfig,
) -> BinderFactory:
    def build(endpoint: str) -> OptimizationEvaluationBinder:
        model_deployment = next(
            (
                environment.allowed_models[0]
                for environment in config.environments.values()
                if str(environment.project_endpoint) == endpoint
                and environment.allowed_models
            ),
            None,
        )
        if model_deployment is None:
            raise ValueError(
                "the evaluation environment requires at least one allowed "
                "model deployment"
            )
        return OptimizationEvaluationBinder(
            endpoint,
            credential_provider=credential_provider,
            evaluator_model_deployment=model_deployment,
        )

    return build


def _spec_environment(
    config: OptimizerConfig,
    repository_root: Path,
    issue_number: int,
) -> str:
    """Best-effort resolve the approved spec's environment for this issue.

    The registration gateway must target the *approved* environment's project
    endpoint, but ``register`` receives no spec/target context, so the
    environment is resolved once from the merged spec on disk (falling back to
    the configured default when no merged spec exists yet, e.g. during SPEC).
    """

    spec_path = repository_root / spec_file_path(issue_number)
    try:
        document = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return config.default_environment
    if isinstance(document, Mapping):
        environment = document.get("environment")
        if isinstance(environment, str) and environment in config.environments:
            return environment
    return config.default_environment


# ---------------------------------------------------------------------------
# Dependency assembly
# ---------------------------------------------------------------------------


def build_issue_optimization_dependencies(
    config: OptimizerConfig,
    *,
    command_runner: CommandRunner | None = None,
    environment: EnvironmentReader | None = None,
    credential_provider: AzureCredentialProvider | None = None,
    resolution_gateway_factory: ResolutionGatewayFactory | None = None,
    registration_gateway_factory: RegistrationGatewayFactory | None = None,
    binder_factory: BinderFactory | None = None,
    draft_gateway: Any | None = None,
    publisher: Any | None = None,
    target_environment: str | None = None,
    lifecycle_services: Any | None = None,
) -> IssueOptimizationDependencies:
    """Assemble the live issue-driven optimization dependencies.

    All Foundry seams share one explicit OIDC credential provider and resolve
    their (target-specific) project endpoint lazily. The optional factory
    parameters exist so tests can inject fakes that observe environment/target
    routing without any live Azure access.
    """

    commands = command_runner or SubprocessCommandRunner()
    reader = environment or OsEnvironmentReader()
    credential = credential_provider or _default_credential_provider(reader)

    resolution_factory = (
        resolution_gateway_factory
        or _default_resolution_gateway_factory(credential)
    )
    registration_factory = (
        registration_gateway_factory
        or _default_registration_gateway_factory(credential)
    )
    binder = binder_factory or _default_binder_factory(credential, config)
    gateway = draft_gateway or DraftGateway(credential)
    campaign_publisher = publisher or CampaignPublisher(commands)

    registry = build_specification_asset_registry(
        resolution_gateway_factory=resolution_factory
    )
    spec_service = OptimizationSpecService(
        config,
        registry=registry,
        gateway=GhOptimizationGateway(
            commands, granted_capabilities=_SPEC_CAPABILITIES
        ),
        publisher=GitSpecPublisher(commands),
    )

    registration_environment = target_environment or config.default_environment

    services = lifecycle_services
    if services is None:
        from foundry_opt.optimization.lifecycle import build_lifecycle_services

        services = build_lifecycle_services(
            config,
            command_runner=commands,
            credential_provider=credential,
            environment=reader,
        )

    return IssueOptimizationDependencies(
        config=config,
        spec_service=spec_service,
        spec_gateway=GitSpecApprovalGateway(commands),
        registration_gateway=_RegistrationGateway(
            config, registration_environment, registration_factory
        ),
        repository=CampaignGit(),
        validate=lambda path: run_validation(
            ValidationRequest(repository_root=path), commands
        ),
        build_bundle=lambda root_path, output: build_source_bundle(
            BundleRequest(repository_root=root_path, output_path=output)
        ),
        create_draft=_DraftCreator(config, gateway),
        bind_evaluation=_EvaluationBinder(config, binder),
        write_evidence=write_redacted_evidence,
        publish=campaign_publisher,
        state=FileCampaignStateStore(),
        clock=UtcClock(),
        apply_service=getattr(services, "apply_service", None),
        reconcile_service=getattr(services, "reconcile_service", None),
    )


class ProductionOptimizationCommandService:
    """Loads configuration lazily and delegates to the runner."""

    def __init__(self, *, config_path: Path = _DEFAULT_CONFIG_PATH) -> None:
        self._config_path = config_path

    def execute(
        self,
        request: OptimizeCommandRequest,
    ) -> OptimizeCommandResult:
        config_path = request.repository_root / self._config_path
        try:
            config = load_config(config_path)
        except (ConfigLoadError, FileNotFoundError, OSError, ValueError) as error:
            return OptimizeCommandResult(
                status=OptimizeCommandStatus.BLOCKED,
                phase=request.phase,
                summary=(
                    "the optimizer configuration could not be loaded: "
                    f"{redact(str(error))}"
                ),
                issue_number=request.issue_number,
                details={"code": "configuration_unavailable"},
            )
        target_environment = _spec_environment(
            config, request.repository_root, request.issue_number
        )
        dependencies = build_issue_optimization_dependencies(
            config, target_environment=target_environment
        )
        try:
            return IssueOptimizationRunner(dependencies).execute(request)
        except Exception as error:  # noqa: BLE001 - CLI boundary must not crash
            # The runner already converts every missing live capability into a
            # typed ``blocked`` result; anything that still escapes (a failed
            # GitHub/git tool call, an unreachable remote, an unexpected SDK
            # fault) is surfaced as a redacted ``blocked`` result rather than a
            # traceback so the production CLI never leaks internals or crashes.
            return OptimizeCommandResult(
                status=OptimizeCommandStatus.BLOCKED,
                phase=request.phase,
                summary=(
                    "the optimization command could not be completed: "
                    f"{redact(str(error))}"
                ),
                issue_number=request.issue_number,
                details={"code": "optimization_unavailable"},
            )


def build_optimization_command_service() -> OptimizationCommandService:
    """Return the production issue-driven optimization command service."""
    return ProductionOptimizationCommandService()
