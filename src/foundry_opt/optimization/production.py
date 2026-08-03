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
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
from typing import Any
from urllib.parse import quote

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
    OptimizePhase,
)
from foundry_opt.optimization.compatibility import (
    CompatibilityOptimizationCommandService,
    LegacyCampaignEventProjector,
    LegacyGenerationFence,
    LegacyRuntimeNamespace,
)
from foundry_opt.orchestration.steward import (
    GitCampaignInbox,
    StewardAdvanceService,
)
from foundry_opt.orchestration.git_state import GitStateRef, StateRefError
from foundry_opt.orchestration.spec_policy import (
    GhMergedSpecApprovalReader,
    GitPinnedAssetReader,
    OptimizationSpecPolicy,
    OptimizationSpecServiceResolver,
    RepositorySpecPolicy,
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
    provenance_file_path,
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


def _validation_request(
    config: OptimizerConfig,
    repository_root: Path,
) -> ValidationRequest:
    matching_targets = tuple(
        target
        for target in config.targets.values()
        if (repository_root / Path(str(target.entry_point))).is_file()
    )
    if not matching_targets:
        return ValidationRequest(repository_root=repository_root)
    command_sets = {
        tuple(target.validation_commands)
        for target in matching_targets
    }
    if len(command_sets) != 1:
        raise ValueError(
            "multiple matching targets define different validation commands"
        )
    (configured_commands,) = command_sets
    commands = tuple(
        tuple(shlex.split(command, posix=True))
        for command in configured_commands
    )
    return ValidationRequest(
        repository_root=repository_root,
        commands=commands,
    )


def _bundle_request(
    config: OptimizerConfig,
    repository_root: Path,
    output_path: Path,
) -> BundleRequest:
    matching_targets = tuple(
        target
        for target in config.targets.values()
        if (repository_root / Path(str(target.entry_point))).is_file()
    )
    contracts = {
        (
            tuple(str(pattern) for pattern in target.package.include),
            tuple(str(pattern) for pattern in target.package.exclude),
            target.runtime.dependency_resolution or "remote_build",
        )
        for target in matching_targets
    }
    if len(contracts) != 1:
        raise ValueError(
            "the candidate worktree does not resolve to one package contract"
        )
    include, exclude, dependency_resolution = next(iter(contracts))
    return BundleRequest(
        repository_root=repository_root,
        output_path=output_path,
        include=include,
        exclude=exclude,
        dependency_resolution=dependency_resolution,
        evidence_paths=(Path(str(config.campaign.evidence_path)),),
    )


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
    orchestrated: bool = True,
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

    def spec_generation(
        repository_root: Path,
        issue_number: int,
    ) -> int:
        snapshot = GitStateRef().load(repository_root, issue_number)
        if snapshot is None:
            raise StateRefError(
                "campaign state is required for orchestrated specification "
                "publication"
            )
        return snapshot.state.generation

    spec_service = OptimizationSpecService(
        config,
        registry=registry,
        gateway=GhOptimizationGateway(
            commands, granted_capabilities=_SPEC_CAPABILITIES
        ),
        publisher=GitSpecPublisher(commands),
        generation_provider=spec_generation if orchestrated else None,
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
            _validation_request(config, path), commands
        ),
        build_bundle=lambda root_path, output: build_source_bundle(
            _bundle_request(config, root_path, output)
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


class _LegacyOptimizationCommandService:
    """Migration-only adapter around the former phase-owned runner."""

    def __init__(self, *, config_path: Path = _DEFAULT_CONFIG_PATH) -> None:
        self._config_path = config_path

    def precheck(
        self,
        request: OptimizeCommandRequest,
    ) -> OptimizeCommandResult | None:
        config_path = request.repository_root / self._config_path
        try:
            load_config(config_path)
        except (ConfigLoadError, FileNotFoundError, OSError, ValueError) as error:
            return _configuration_error(request, error)
        if (
            request.phase is OptimizePhase.RUN
            and not (
                (
                    request.repository_root
                    / spec_file_path(request.issue_number)
                ).is_file()
                and (
                    request.repository_root
                    / provenance_file_path(request.issue_number)
                ).is_file()
            )
        ):
            return OptimizeCommandResult(
                status=OptimizeCommandStatus.BLOCKED,
                phase=request.phase,
                summary=(
                    "no merged optimization specification was found; run "
                    "`optimize spec` and merge the approved specification "
                    "first"
                ),
                issue_number=request.issue_number,
                details={"code": "spec_not_prepared"},
            )
        return None

    def execute(
        self,
        request: OptimizeCommandRequest,
    ) -> OptimizeCommandResult:
        config_path = request.repository_root / self._config_path
        try:
            config = load_config(config_path)
        except (ConfigLoadError, FileNotFoundError, OSError, ValueError) as error:
            return _configuration_error(request, error)
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


def _configuration_error(
    request: OptimizeCommandRequest,
    error: Exception,
) -> OptimizeCommandResult:
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


def build_optimization_command_service() -> OptimizationCommandService:
    """Return the production issue-driven optimization command service."""
    return ProductionOptimizationCommandService()


def build_production_steward_spec_policy(
    *,
    config_path: Path = _DEFAULT_CONFIG_PATH,
) -> RepositorySpecPolicy:
    """Build the repository-aware specification policy for the steward."""

    commands = SubprocessCommandRunner()
    environment = OsEnvironmentReader()
    credential = _default_credential_provider(environment)
    resolution_factory = _default_resolution_gateway_factory(credential)
    pinned_assets = GitPinnedAssetReader(commands)
    approvals = GhMergedSpecApprovalReader(commands)
    state = GitStateRef()

    def generation(repository_root: Path, issue_number: int) -> int | None:
        snapshot = state.load(repository_root, issue_number)
        return snapshot.state.generation if snapshot is not None else None

    def factory(repository_root: Path) -> OptimizationSpecPolicy:
        config = load_config(repository_root / config_path)
        service = OptimizationSpecService(
            config,
            registry=build_specification_asset_registry(
                resolution_gateway_factory=resolution_factory
            ),
            gateway=GhOptimizationGateway(
                commands,
                granted_capabilities=_SPEC_CAPABILITIES,
            ),
            publisher=GitSpecPublisher(commands),
            generation_provider=generation,
        )
        return OptimizationSpecPolicy(
            config.automation_policy,
            resolver=OptimizationSpecServiceResolver(service),
            pinned_assets=pinned_assets,
            approvals=approvals,
        )

    return RepositorySpecPolicy(factory)


def _production_approved_spec(
    repository_root: Path,
    issue_number: int,
    generation: int,
    expected_spec_sha256: str,
) -> tuple[OptimizationSpec, Mapping[str, Path | None]]:
    snapshot = GitStateRef().load(repository_root, issue_number)
    if (
        snapshot is None
        or snapshot.state.generation != generation
        or snapshot.state.spec_sha256 != expected_spec_sha256
    ):
        raise ValueError("approved candidate state changed")
    path = f"objects/specifications/g{generation}.json"
    objects = tuple(
        item for item in snapshot.objects if item.path == path
    )
    if not objects:
        return _production_merged_spec(
            repository_root,
            issue_number,
            expected_spec_sha256,
        )
    if len(objects) != 1:
        raise ValueError("approved candidate specification is ambiguous")
    try:
        document = json.loads(objects[0].content)
        if (
            not isinstance(document, dict)
            or set(document) != {"asset_paths", "spec"}
            or not isinstance(document["asset_paths"], dict)
        ):
            raise ValueError
        spec = OptimizationSpec.model_validate(document["spec"])
        asset_paths = {
            str(asset_id): (
                Path(raw_path) if raw_path is not None else None
            )
            for asset_id, raw_path in document["asset_paths"].items()
        }
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(
            "approved candidate specification is invalid"
        ) from error
    expected_assets = {
        asset.asset_id for asset in (*spec.datasets, *spec.evaluators)
    }
    if (
        spec.issue_number != issue_number
        or spec.sha256 != expected_spec_sha256
        or set(asset_paths) != expected_assets
        or any(
            path is not None
            and (
                path.is_absolute()
                or ".." in path.parts
                or not str(path)
            )
            for path in asset_paths.values()
        )
    ):
        raise ValueError("approved candidate specification changed")
    return spec, asset_paths


def _production_merged_spec(
    repository_root: Path,
    issue_number: int,
    expected_spec_sha256: str,
) -> tuple[OptimizationSpec, Mapping[str, Path | None]]:
    try:
        spec = OptimizationSpec.model_validate(
            yaml.safe_load(
                (
                    repository_root / spec_file_path(issue_number)
                ).read_text(encoding="utf-8")
            )
        )
        provenance = json.loads(
            (
                repository_root / provenance_file_path(issue_number)
            ).read_text(encoding="utf-8")
        )
        if (
            not isinstance(provenance, dict)
            or provenance.get("issue_number") != issue_number
            or provenance.get("base_commit") != spec.base_commit
            or provenance.get("spec_sha256") != spec.sha256
        ):
            raise ValueError
        paths = {
            str(entry["asset_id"]): (
                Path(entry["path"])
                if entry.get("path") is not None
                else None
            )
            for entry in (
                *provenance.get("datasets", ()),
                *provenance.get("evaluators", ()),
            )
            if isinstance(entry, dict)
        }
    except (
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        yaml.YAMLError,
    ) as error:
        raise ValueError(
            "approved candidate specification is unavailable"
        ) from error
    expected_assets = {
        asset.asset_id for asset in (*spec.datasets, *spec.evaluators)
    }
    if (
        spec.issue_number != issue_number
        or spec.sha256 != expected_spec_sha256
        or set(paths) != expected_assets
    ):
        raise ValueError("approved candidate specification changed")
    return spec, paths


def _production_git_bytes(
    repository_root: Path,
    commit: str,
    path: Path,
) -> bytes:
    completed = subprocess.run(
        ("git", "show", f"{commit}:{path.as_posix()}"),
        cwd=repository_root,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise ValueError("approved candidate asset is unavailable")
    return completed.stdout


class _ProductionCandidatePlanSource:
    """Resolve exact approved campaign inputs for the canonical workers."""

    def __init__(
        self,
        *,
        config_path: Path,
        registration_gateway_factory: RegistrationGatewayFactory,
    ) -> None:
        self._config_path = config_path
        self._registration_gateway_factory = registration_gateway_factory
        self._resolved: dict[
            tuple[int, int, str],
            tuple[Path, OptimizerConfig, OptimizationSpec, tuple[Any, ...]],
        ] = {}

    def resolve(
        self,
        repository_root: Path,
        issue_number: int,
        generation: int,
        expected_spec_sha256: str,
    ) -> tuple[OptimizerConfig, OptimizationSpec, tuple[Any, ...]]:
        root = repository_root.expanduser().resolve()
        config = load_config(root / self._config_path)
        spec, asset_paths = _production_approved_spec(
            root,
            issue_number,
            generation,
            expected_spec_sha256,
        )
        if (
            spec.issue_number != issue_number
            or spec.sha256 != expected_spec_sha256
        ):
            raise ValueError("approved candidate specification changed")
        target = config.targets.get(spec.target)
        if (
            target is None
            or target.environment != spec.environment
            or target.base_agent_version != spec.base_agent_version
        ):
            raise ValueError("approved candidate target changed")
        assets = self._materialize_assets(
            root,
            config,
            spec,
            asset_paths,
        )
        self._resolved[(issue_number, generation, spec.sha256)] = (
            root,
            config,
            spec,
            assets,
        )
        return config, spec, assets

    def resolved_for(
        self,
        issue_number: int,
        generation: int,
        spec_sha256: str,
    ) -> tuple[Path, OptimizerConfig, OptimizationSpec, tuple[Any, ...]]:
        try:
            return self._resolved[(issue_number, generation, spec_sha256)]
        except KeyError as error:
            raise ValueError(
                "candidate production inputs have not been resolved"
            ) from error

    def _materialize_assets(
        self,
        root: Path,
        config: OptimizerConfig,
        spec: OptimizationSpec,
        paths: Mapping[str, Path | None],
    ) -> tuple[Any, ...]:
        from foundry_opt.optimization.assets import (
            canonicalize_repository_asset_content,
            materialize_prepared_asset,
        )
        from foundry_opt.optimization.models import PreparedEvaluationAsset
        from foundry_opt.optimization.runner import _asset_reference

        registration = _RegistrationGateway(
            config,
            spec.environment,
            self._registration_gateway_factory,
        )
        references: list[Any] = []
        for asset in (*spec.datasets, *spec.evaluators):
            path = paths.get(asset.asset_id)
            materialized = asset
            if path is not None:
                absolute = (root / path).resolve()
                if not absolute.is_relative_to(root):
                    raise ValueError(
                        "approved candidate asset path is invalid"
                    )
                content = canonicalize_repository_asset_content(
                    _production_git_bytes(
                        root,
                        spec.base_commit,
                        path,
                    )
                )
                if (
                    asset.content_sha256 is not None
                    and hashlib.sha256(content).hexdigest()
                    != asset.content_sha256
                ):
                    raise ValueError(
                        "approved candidate asset content changed"
                    )
                materialized = materialize_prepared_asset(
                    PreparedEvaluationAsset(
                        provenance=asset,
                        files={path: content},
                    ),
                    registration,
                )
            elif asset.remote_id is None:
                raise ValueError(
                    "approved candidate asset has no remote identity"
                )
            references.append(_asset_reference(materialized))
        return tuple(references)


class _ProductionCandidateWorkerPlanResolver:
    def __init__(self, source: _ProductionCandidatePlanSource) -> None:
        self._source = source

    def resolve(self, request: Any, state: Any) -> Any:
        from foundry_opt.optimization.runner import (
            _campaign_limits,
            _evaluation_policy,
            _restricted_opt_ins,
        )
        from foundry_opt.orchestration.candidate_workers import (
            CandidateWorkerPlan,
        )

        if state.spec_sha256 is None:
            raise ValueError("candidate workers require an approved spec")
        config, spec, assets = self._source.resolve(
            request.repository_root,
            request.issue_number,
            state.generation,
            state.spec_sha256,
        )
        target = config.targets[spec.target]
        return CandidateWorkerPlan(
            issue_number=request.issue_number,
            generation=state.generation,
            spec_sha256=spec.sha256,
            base_commit=spec.base_commit,
            target=spec.target,
            base_agent_version=int(spec.base_agent_version),
            goal=spec.goal,
            limits=_campaign_limits(config, target),
            edit_paths=tuple(Path(str(path)) for path in target.edit_paths),
            allowed_mutations=frozenset(
                mutation.value for mutation in spec.allowed_mutations
            ),
            restricted_opt_ins=_restricted_opt_ins(spec),
            evaluation_policy=_evaluation_policy(spec),
            assets=assets,
            evidence_root=Path(str(config.campaign.evidence_path)),
        )


class _ProductionCandidateDraftEffects:
    def __init__(
        self,
        source: _ProductionCandidatePlanSource,
        credential: AzureCredentialProvider,
        draft_gateway: Any | None = None,
    ) -> None:
        self._source = source
        self._gateway = draft_gateway or DraftGateway(credential)

    def reconcile(self, intent: Any) -> DraftRecord | None:
        return self.create(intent)

    def create(self, intent: Any) -> DraftRecord:
        _, config, _, _ = self._source.resolved_for(
            intent.issue_number,
            intent.generation,
            intent.spec_sha256,
        )
        return _DraftCreator(config, self._gateway)(
            intent.target,
            intent.subject_id,
            intent.idempotency_key,
            intent.bundle,
        )


class _ProductionCandidateEvaluationEffects:
    def __init__(
        self,
        source: _ProductionCandidatePlanSource,
        credential: AzureCredentialProvider,
        binder_factory: BinderFactory | None,
    ) -> None:
        self._source = source
        self._credential = credential
        self._binder_factory = binder_factory

    def reconcile(self, intent: Any) -> Any | None:
        return self.run(intent)

    def run(self, intent: Any) -> Any:
        from foundry_opt.evaluation import evaluate_with_repeat

        _, config, spec, assets = self._source.resolved_for(
            intent.issue_number,
            intent.generation,
            intent.spec_sha256,
        )
        factory = self._binder_factory or _default_binder_factory(
            self._credential,
            config,
        )
        evaluate = _EvaluationBinder(config, factory)(
            spec,
            assets,
        )
        return evaluate_with_repeat(
            intent.subject,
            intent.split,
            intent.policy,
            evaluate,
        )


class _ProductionCandidateDesignRepository:
    """Capture specialist edits on a deterministic remote result ref."""

    def __init__(self, commands: CommandRunner) -> None:
        self._commands = commands

    def capture(self, request: Any, intent: Any, result: Any) -> Any:
        from foundry_opt.orchestration.candidate_workers import (
            CandidateDesignArtifact,
        )

        root = request.repository_root.expanduser().resolve()
        expected_result = (
            root
            / ".foundry-optimizer"
            / "design-results"
            / f"{intent.effect_id}.json"
        ).resolve()
        if request.result_file.resolve() != expected_result:
            raise ValueError("candidate design result path is invalid")
        result.require_matches(intent)
        head = self._commands.run(
            ("git", "rev-parse", "HEAD^{commit}"),
            cwd=root,
        ).stdout.strip()
        if head != intent.base_commit:
            raise ValueError("candidate designer checkout base changed")
        result_path = expected_result.relative_to(root)
        changed = _production_changed_paths(self._commands, root, intent)
        candidate_paths = tuple(
            path for path in changed if path != result_path
        )
        if (
            not candidate_paths
            or any(
                not _path_is_allowed(path, intent.edit_paths)
                for path in candidate_paths
            )
            or any(
                path != result_path
                and not _path_is_allowed(path, intent.edit_paths)
                for path in changed
            )
        ):
            raise ValueError("candidate design changed forbidden paths")
        index = (
            root
            / ".foundry-optimizer"
            / "design-indexes"
            / f"{intent.effect_id}.index"
        )
        index.parent.mkdir(parents=True, exist_ok=True)
        index.unlink(missing_ok=True)
        environment = {
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
            "GIT_AUTHOR_EMAIL": "foundry-opt@example.invalid",
            "GIT_AUTHOR_NAME": "Foundry Candidate Designer",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
            "GIT_COMMITTER_EMAIL": "foundry-opt@example.invalid",
            "GIT_COMMITTER_NAME": "Foundry Candidate Designer",
            "GIT_INDEX_FILE": str(index),
        }
        try:
            self._commands.run(
                ("git", "read-tree", intent.base_commit),
                cwd=root,
                environment=environment,
            )
            self._commands.run(
                (
                    "git",
                    "add",
                    "-A",
                    "--",
                    *(path.as_posix() for path in intent.edit_paths),
                ),
                cwd=root,
                environment=environment,
            )
            tree = self._commands.run(
                ("git", "write-tree"),
                cwd=root,
                environment=environment,
            ).stdout.strip()
            commit = self._commands.run(
                (
                    "git",
                    "commit-tree",
                    tree,
                    "-p",
                    intent.base_commit,
                ),
                cwd=root,
                environment=environment,
                input_text=(
                    "Capture candidate design "
                    f"{intent.effect_id}\n"
                ),
            ).stdout.strip()
            ref = (
                "refs/heads/foundry-opt/design/"
                f"issue-{intent.issue_number}/{intent.effect_id}"
            )
            existing = self._commands.run(
                ("git", "ls-remote", "--heads", "origin", ref),
                cwd=root,
            ).stdout.strip()
            if existing:
                remote_commit = existing.split()[0]
                if remote_commit != commit:
                    raise ValueError("candidate design ref changed")
            else:
                self._commands.run(
                    ("git", "push", "--quiet", "origin", f"{commit}:{ref}"),
                    cwd=root,
                )
        finally:
            index.unlink(missing_ok=True)
            try:
                index.parent.rmdir()
            except OSError:
                pass
        return CandidateDesignArtifact(
            ref=ref,
            head_commit=commit,
            tree_sha=tree,
            changed_paths=candidate_paths,
        )

    def cleanup(self, request: Any, intent: Any) -> None:
        root = request.repository_root.expanduser().resolve()
        request.result_file.unlink(missing_ok=True)
        paths = tuple(path.as_posix() for path in intent.edit_paths)
        self._commands.run(
            (
                "git",
                "restore",
                "--source=HEAD",
                "--staged",
                "--worktree",
                "--",
                *paths,
            ),
            cwd=root,
        )
        self._commands.run(
            ("git", "clean", "-fd", "--", *paths),
            cwd=root,
        )


class _ProductionCandidateDesigner:
    """Reconcile only typed results captured by the designer specialist."""

    def __init__(
        self,
        *,
        ledger: Any | None = None,
        commands: CommandRunner | None = None,
    ) -> None:
        self._ledger = ledger or GitStateRef()
        self._commands = commands or SubprocessCommandRunner()

    def reconcile(self, intent: Any) -> tuple[Any, ...]:
        root = _repository_root_from_worktree(
            intent.worktree,
            self._commands,
        )
        snapshot = self._ledger.load(root, intent.issue_number)
        if snapshot is None:
            raise ValueError("candidate design state is unavailable")
        records = tuple(
            record
            for record in snapshot.outbox
            if record.record_id == f"{intent.effect_id}-submitted"
        )
        if not records:
            return ()
        if len(records) != 1:
            raise ValueError("candidate design result is ambiguous")
        record = records[0]
        result = _production_candidate_design_result(intent, record)
        ref = record.payload.get("ref")
        head_commit = record.payload.get("head_commit")
        tree_sha = record.payload.get("tree_sha")
        if not all(isinstance(value, str) for value in (ref, head_commit, tree_sha)):
            raise ValueError("candidate design Git binding is invalid")
        self._commands.run(
            ("git", "fetch", "--quiet", "origin", ref),
            cwd=root,
        )
        fetched = self._commands.run(
            ("git", "rev-parse", "FETCH_HEAD^{commit}"),
            cwd=root,
        ).stdout.strip()
        parent = self._commands.run(
            ("git", "rev-parse", f"{fetched}^"),
            cwd=root,
        ).stdout.strip()
        fetched_tree = self._commands.run(
            ("git", "rev-parse", f"{fetched}^{{tree}}"),
            cwd=root,
        ).stdout.strip()
        if (
            fetched != head_commit
            or parent != intent.base_commit
            or fetched_tree != tree_sha
        ):
            raise ValueError("candidate design Git binding changed")
        raw_changed = record.payload.get("changed_paths")
        if not isinstance(raw_changed, list):
            raise ValueError("candidate design changed paths are invalid")
        expected_changed = tuple(
            Path(path) for path in raw_changed
        )
        observed_changed = tuple(
            sorted(
                Path(value)
                for value in self._commands.run(
                    (
                        "git",
                        "diff",
                        "--name-only",
                        "-z",
                        intent.base_commit,
                        fetched,
                        "--",
                    ),
                    cwd=root,
                ).stdout.split("\0")
                if value
            )
        )
        if (
            observed_changed != expected_changed
            or any(
                not _path_is_allowed(path, intent.edit_paths)
                for path in observed_changed
            )
        ):
            raise ValueError("candidate design changed paths are invalid")
        remote_patch = self._commands.run(
            (
                "git",
                "diff",
                "--binary",
                "--full-index",
                intent.base_commit,
                fetched,
                "--",
            ),
            cwd=root,
        ).stdout
        current_patch = self._commands.run(
            (
                "git",
                "diff",
                "--binary",
                "--full-index",
                intent.base_commit,
                "--",
            ),
            cwd=intent.worktree,
        ).stdout
        if current_patch == remote_patch:
            return (result,)
        if current_patch:
            raise ValueError("candidate design worktree changed")
        self._commands.run(
            (
                "git",
                "apply",
                "--index",
                "--binary",
                "--whitespace=nowarn",
                "-",
            ),
            cwd=intent.worktree,
            input_bytes=remote_patch.encode("utf-8"),
        )
        applied = self._commands.run(
            (
                "git",
                "diff",
                "--binary",
                "--full-index",
                intent.base_commit,
                "--",
            ),
            cwd=intent.worktree,
        ).stdout
        if applied != remote_patch:
            raise ValueError("candidate design patch changed during apply")
        return (result,)

    def invoke(self, intent: Any) -> Any:
        from foundry_opt.orchestration.candidate_workers import (
            CandidateDesignPending,
        )

        raise CandidateDesignPending()


class _ProductionCandidateSlatePlanResolver:
    def __init__(
        self,
        commands: CommandRunner,
        config_path: Path,
    ) -> None:
        self._commands = commands
        self._config_path = config_path

    def resolve(self, request: Any, state: Any) -> Any:
        from foundry_opt.optimization.runner import _evaluation_policy
        from foundry_opt.orchestration.candidate_slate import (
            CandidateSlatePlan,
        )

        root = request.repository_root
        config = load_config(root / self._config_path)
        if state.spec_sha256 is None:
            raise ValueError("candidate slate requires an approved spec")
        spec, _ = _production_approved_spec(
            root,
            request.issue_number,
            state.generation,
            state.spec_sha256,
        )
        target = config.targets.get(spec.target)
        if (
            target is None
            or target.environment != spec.environment
            or target.base_agent_version != spec.base_agent_version
        ):
            raise ValueError("approved candidate slate target changed")
        if (
            state.spec_sha256 is None
            or spec.issue_number != request.issue_number
            or spec.sha256 != state.spec_sha256
        ):
            raise ValueError("approved candidate slate specification changed")
        repository_document = _production_json(
            self._commands,
            (
                "gh",
                "repo",
                "view",
                "--json",
                "nameWithOwner,defaultBranchRef",
            ),
            root,
        )
        repository = repository_document.get("nameWithOwner")
        default_ref = repository_document.get("defaultBranchRef")
        default_branch = (
            default_ref.get("name")
            if isinstance(default_ref, Mapping)
            else None
        )
        if (
            not isinstance(repository, str)
            or not isinstance(default_branch, str)
            or repository != spec.repository
        ):
            raise ValueError("candidate repository identity is unavailable")
        pinned = CampaignGit(
            default_branch=lambda repository_root: default_branch
        ).pin_default_branch(root)
        if pinned.commit != spec.base_commit:
            raise ValueError("candidate slate base commit changed")
        required_checks = tuple(
            dict.fromkeys(
                (
                    "Foundry exact candidate check",
                    *config.automation_policy.required_checks,
                )
            )
        )
        return CandidateSlatePlan(
            issue_number=request.issue_number,
            generation=state.generation,
            repository=repository,
            default_branch=default_branch,
            spec_sha256=spec.sha256,
            base_commit=spec.base_commit,
            evaluation_policy=_evaluation_policy(spec),
            required_checks=required_checks,
        )


class _ProductionCandidatePullRequestReader:
    def __init__(self, commands: CommandRunner) -> None:
        self._commands = commands

    def snapshots_for(self, request: Any, bindings: tuple[Any, ...]) -> Any:
        from foundry_opt.orchestration.candidate_bridge import (
            GhCandidatePullRequestReader,
        )

        repository = _production_json(
            self._commands,
            (
                "gh",
                "repo",
                "view",
                "--json",
                "nameWithOwner",
            ),
            request.repository_root,
        ).get("nameWithOwner")
        if not isinstance(repository, str):
            raise ValueError("candidate repository identity is unavailable")
        return GhCandidatePullRequestReader(
            self._commands,
            request.repository_root,
            repository,
        ).snapshots_for(request, bindings)


def _production_changed_paths(
    commands: CommandRunner,
    root: Path,
    intent: Any,
) -> tuple[Path, ...]:
    tracked = commands.run(
        (
            "git",
            "diff",
            "--name-only",
            "-z",
            intent.base_commit,
            "--",
        ),
        cwd=root,
    ).stdout
    untracked = commands.run(
        (
            "git",
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ),
        cwd=root,
    ).stdout
    return tuple(
        Path(value)
        for value in sorted(
            {
                value
                for document in (tracked, untracked)
                for value in document.split("\0")
                if value
            }
        )
    )


def _path_is_allowed(path: Path, allowed: tuple[Path, ...]) -> bool:
    return any(path == root or root in path.parents for root in allowed)


def _production_candidate_design_result(
    intent: Any,
    record: Any,
) -> Any:
    from foundry_opt.orchestration.candidate_workers import (
        CandidateDesignResult,
    )

    if (
        record.kind != "candidate_design_submitted"
        or record.generation != intent.generation
        or record.payload.get("effect_id") != intent.effect_id
    ):
        raise ValueError("candidate design result binding is invalid")
    try:
        result = CandidateDesignResult(
            effect_id=str(record.payload["effect_id"]),
            result_id=str(record.payload["result_id"]),
            issue_number=int(record.payload["issue_number"]),
            generation=record.generation,
            spec_sha256=str(record.payload["spec_sha256"]),
            base_commit=str(record.payload["base_commit"]),
            candidate_id=str(record.payload["candidate_id"]),
            slot=int(record.payload["slot"]),
            idea_id=str(record.payload["idea_id"]),
            mutation_class=str(record.payload["mutation_class"]),
            parent_idea_ids=tuple(record.payload["parent_idea_ids"]),
            required_opt_ins=frozenset(
                record.payload["required_opt_ins"]
            ),
            motivation=str(record.payload["motivation"]),
            lessons=tuple(record.payload["lessons"]),
            complexity=str(record.payload["complexity"]),
        )
        result.require_matches(intent)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("candidate design result is invalid") from error
    return result


def build_production_steward_candidate_workers(
    *,
    config_path: Path = _DEFAULT_CONFIG_PATH,
    command_runner: CommandRunner | None = None,
    environment: EnvironmentReader | None = None,
    credential_provider: AzureCredentialProvider | None = None,
    registration_gateway_factory: RegistrationGatewayFactory | None = None,
    binder_factory: BinderFactory | None = None,
    repository: Any | None = None,
    draft_gateway: Any | None = None,
) -> Any:
    from foundry_opt.orchestration.candidate_workers import (
        CandidateWorkerDependencies,
        CandidateWorkerService,
    )

    commands = command_runner or SubprocessCommandRunner()
    reader = environment or OsEnvironmentReader()
    credential = credential_provider or _default_credential_provider(reader)
    registration = (
        registration_gateway_factory
        or _default_registration_gateway_factory(credential)
    )
    source = _ProductionCandidatePlanSource(
        config_path=config_path,
        registration_gateway_factory=registration,
    )
    campaign_repository = repository or CampaignGit()
    return CandidateWorkerService(
        ledger=GitStateRef(),
        resolver=_ProductionCandidateWorkerPlanResolver(source),
        dependencies=CandidateWorkerDependencies(
            repository=campaign_repository,
            designer=_ProductionCandidateDesigner(
                ledger=GitStateRef(),
                commands=commands,
            ),
            validate=lambda path: run_validation(
                _validation_request(
                    load_config(
                        _repository_root_from_worktree(path) / config_path
                    ),
                    path,
                ),
                commands,
            ),
            build_bundle=lambda root_path, output: build_source_bundle(
                _bundle_request(
                    load_config(
                        _repository_root_from_worktree(root_path)
                        / config_path
                    ),
                    root_path,
                    output,
                )
            ),
            drafts=_ProductionCandidateDraftEffects(
                source,
                credential,
                draft_gateway,
            ),
            evaluations=_ProductionCandidateEvaluationEffects(
                source,
                credential,
                binder_factory,
            ),
            write_evidence=write_redacted_evidence,
            clock=UtcClock(),
        ),
    )


def build_production_candidate_design_submission_service(
    *,
    command_runner: CommandRunner | None = None,
) -> Any:
    from foundry_opt.orchestration.candidate_workers import (
        CandidateDesignSubmissionService,
    )

    commands = command_runner or SubprocessCommandRunner()
    return CandidateDesignSubmissionService(
        ledger=GitStateRef(),
        repository=_ProductionCandidateDesignRepository(commands),
    )


def build_production_steward_candidate_slate(
    *,
    config_path: Path = _DEFAULT_CONFIG_PATH,
    command_runner: CommandRunner | None = None,
) -> Any:
    from foundry_opt.orchestration.candidate_slate import (
        CandidateSlateService,
    )

    commands = command_runner or SubprocessCommandRunner()
    return CandidateSlateService(
        ledger=GitStateRef(),
        resolver=_ProductionCandidateSlatePlanResolver(
            commands,
            config_path,
        ),
    )


def build_production_steward_candidate_selection(
    *,
    config_path: Path = _DEFAULT_CONFIG_PATH,
    command_runner: CommandRunner | None = None,
) -> Any:
    from foundry_opt.orchestration.candidate_slate import (
        CandidateSelectionService,
    )

    commands = command_runner or SubprocessCommandRunner()
    return CandidateSelectionService(
        ledger=GitStateRef(),
        reader=_ProductionCandidatePullRequestReader(commands),
        resolver=_ProductionCandidateSlatePlanResolver(
            commands,
            config_path,
        ),
    )


def _repository_root_from_worktree(
    path: Path,
    commands: CommandRunner | None = None,
) -> Path:
    common = (commands or SubprocessCommandRunner()).run(
        ("git", "rev-parse", "--path-format=absolute", "--git-common-dir"),
        cwd=path,
    ).stdout.strip()
    if not common:
        raise ValueError("candidate worktree repository is unavailable")
    return Path(common).resolve().parent


class _ProductionDeploymentPlanResolver:
    def __init__(
        self,
        commands: CommandRunner,
        config_path: Path,
    ) -> None:
        self._commands = commands
        self._config_path = config_path

    def resolve(self, request: Any, state: Any) -> Any:
        from foundry_opt.campaign.state import FileCampaignStateStore
        from foundry_opt.deployment import (
            DeploymentTrigger as DomainDeploymentTrigger,
        )
        from foundry_opt.orchestration.candidate_slate import (
            candidate_worker_bindings,
        )
        from foundry_opt.orchestration.deployment import (
            DeploymentPlan,
            DeploymentWorkflowIdentity,
        )

        root = request.repository_root
        config = load_config(root / self._config_path)
        if state.spec_sha256 is None:
            raise ValueError("deployment requires an approved spec")
        spec, _ = _production_approved_spec(
            root,
            request.issue_number,
            state.generation,
            state.spec_sha256,
        )
        target = config.targets.get(spec.target)
        if (
            target is None
            or target.environment != spec.environment
            or target.base_agent_version != spec.base_agent_version
        ):
            raise ValueError("approved deployment target changed")
        if spec.sha256 != state.spec_sha256:
            raise ValueError("approved deployment specification changed")
        environment = config.environments[spec.environment]
        repository_document = _production_json(
            self._commands,
            (
                "gh",
                "repo",
                "view",
                "--json",
                "nameWithOwner,databaseId,defaultBranchRef",
            ),
            root,
        )
        repository = repository_document.get("nameWithOwner")
        repository_id = repository_document.get("databaseId")
        default_ref = repository_document.get("defaultBranchRef")
        default_branch = (
            default_ref.get("name")
            if isinstance(default_ref, Mapping)
            else None
        )
        if (
            not isinstance(repository, str)
            or type(repository_id) is not int
            or not isinstance(default_branch, str)
        ):
            raise ValueError("GitHub repository identity is unavailable")
        if spec.repository != repository:
            raise ValueError("deployment repository changed")
        workflow_path = Path(str(environment.deployment_workflow.path))
        workflow_document = _production_json(
            self._commands,
            (
                "gh",
                "api",
                (
                    f"repos/{repository}/actions/workflows/"
                    f"{quote(workflow_path.as_posix(), safe='')}"
                ),
            ),
            root,
        )
        workflow_id = workflow_document.get("id")
        if type(workflow_id) is not int or workflow_id < 1:
            raise ValueError("deployment workflow identity is unavailable")
        snapshot = GitStateRef().load(root, request.issue_number)
        if snapshot is None:
            raise ValueError("deployment campaign state is unavailable")
        selections = tuple(
            record
            for record in snapshot.outbox
            if (
                record.kind == "candidate_selection_recorded"
                and record.generation == state.generation
                and record.payload.get("candidate_id")
                == state.selected_candidate_id
            )
        )
        if len(selections) != 1:
            raise ValueError("selected candidate pull request is unavailable")
        pull_request_number = selections[0].payload.get(
            "pull_request_number"
        )
        if type(pull_request_number) is not int:
            raise ValueError("selected candidate pull request is invalid")
        pull_request = _production_json(
            self._commands,
            (
                "gh",
                "pr",
                "view",
                str(pull_request_number),
                "--repo",
                repository,
                "--json",
                "mergedBy",
            ),
            root,
        )
        merged_by = pull_request.get("mergedBy")
        merge_actor = (
            merged_by.get("login")
            if isinstance(merged_by, Mapping)
            else None
        )
        if not isinstance(merge_actor, str):
            raise ValueError("candidate merge actor is unavailable")
        configured_actor = config.automation_policy.merge_actor
        if configured_actor is not None:
            allowed_actors = (configured_actor,)
        else:
            permission = _production_json(
                self._commands,
                (
                    "gh",
                    "api",
                    (
                        f"repos/{repository}/collaborators/"
                        f"{merge_actor}/permission"
                    ),
                ),
                root,
            ).get("permission")
            if permission not in {"admin", "maintain", "write"}:
                raise ValueError("candidate merge actor is not authorized")
            allowed_actors = (merge_actor,)
        bindings = tuple(
            binding
            for binding in candidate_worker_bindings(snapshot)
            if binding.candidate_id == state.selected_candidate_id
        )
        if len(bindings) != 1:
            raise ValueError("selected candidate binding is unavailable")
        binding = bindings[0]
        plans = tuple(
            record
            for record in snapshot.outbox
            if (
                record.kind == "applier_worker_issue_planned"
                and record.generation == state.generation
                and record.payload.get("binding_sha256")
                == binding.binding_sha256
            )
        )
        if len(plans) != 1:
            raise ValueError("candidate required checks are unavailable")
        required_checks = tuple(plans[0].payload["required_checks"])
        campaign_id = (
            f"issue-{request.issue_number}-g{state.generation}-"
            f"{state.spec_sha256[:8]}-{binding.base_commit[:8]}"
        )
        legacy_state = FileCampaignStateStore().load(root, campaign_id)
        campaign_pr = (
            legacy_state.finalized.campaign_pull_request_number
            if legacy_state is not None
            and legacy_state.finalized is not None
            else None
        )
        policy_document = {
            name: policy.model_dump(mode="json")
            for name, policy in sorted(spec.metrics.items())
        }
        policy_sha256 = hashlib.sha256(
            json.dumps(
                policy_document,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        trigger = DomainDeploymentTrigger(
            environment.deployment_workflow.trigger.value
        )
        if trigger is DomainDeploymentTrigger.MANUAL:
            workflow_source = yaml.safe_load(
                (root / workflow_path).read_text(encoding="utf-8")
            )
            if not isinstance(workflow_source, Mapping):
                raise ValueError("manual deployment workflow is invalid")
            triggers = workflow_source.get(
                "on",
                workflow_source.get(True),
            )
            dispatch = (
                triggers.get("workflow_dispatch")
                if isinstance(triggers, Mapping)
                else None
            )
            inputs = (
                dispatch.get("inputs")
                if isinstance(dispatch, Mapping)
                else None
            )
            run_name = workflow_source.get("run-name")
            if (
                not isinstance(inputs, Mapping)
                or not {
                    "selected_commit",
                    "foundry_opt_effect_id",
                }
                <= set(inputs)
                or not isinstance(run_name, str)
                or run_name.strip()
                != "${{ inputs.foundry_opt_effect_id }}"
            ):
                raise ValueError(
                    "manual deployment workflow lacks exact correlation"
                )
        workflow_actor = (
            merge_actor
            if trigger is DomainDeploymentTrigger.MERGE
            else "workflow-dispatch"
        )
        return DeploymentPlan(
            issue_number=request.issue_number,
            generation=state.generation,
            repository=repository,
            repository_id=repository_id,
            workflow=DeploymentWorkflowIdentity(
                repository=repository,
                repository_id=repository_id,
                path=workflow_path,
                ref=f"refs/heads/{default_branch}",
                trigger=trigger,
                workflow_id=workflow_id,
                actor=workflow_actor,
            ),
            allowed_merge_actors=allowed_actors,
            required_checks=required_checks,
            max_attempts=(
                config.campaign.transient_retries + 1
                if trigger is DomainDeploymentTrigger.MANUAL
                else 1
            ),
            timeout_seconds=1800,
            held_out_evaluation_id=(
                f"held-out-{spec.sha256[:16]}"
            ),
            evaluation_policy_sha256=policy_sha256,
            campaign_pull_request_number=campaign_pr,
            optimization_pull_request_number=None,
        )


class _ProductionDeploymentSelectionReader:
    def __init__(self, commands: CommandRunner) -> None:
        self._commands = commands

    def read(self, request: Any, binding: Any, plan: Any) -> Any:
        from foundry_opt.orchestration.candidate_bridge import (
            GhCandidatePullRequestReader,
        )
        from foundry_opt.orchestration.deployment import (
            CandidateDeploymentSelectionReader,
        )

        return CandidateDeploymentSelectionReader(
            GhCandidatePullRequestReader(
                self._commands,
                request.repository_root,
                plan.repository,
            )
        ).read(request, binding, plan)


class _ProductionPostDeploymentEvaluationEffects:
    def __init__(
        self,
        repository_root: Path,
        config_path: Path,
        credential_provider: AzureCredentialProvider,
    ) -> None:
        from foundry_opt.adapters.post_deploy_evaluation import (
            build_live_post_deploy_evaluator,
        )
        from foundry_opt.orchestration.deployment import (
            ExistingPostDeploymentEvaluationEffects,
        )

        self._root = repository_root
        self._config_path = config_path
        self._state = FileCampaignStateStore()
        self._adapter = ExistingPostDeploymentEvaluationEffects(
            build_live_post_deploy_evaluator(
                credential_provider,
                state_store=self._state,
            ),
            request_factory=self._request,
        )

    def reconcile(self, intent: Any) -> Any:
        from foundry_opt.orchestration.deployment import (
            post_deployment_evaluation_result_from_record,
        )

        snapshot = GitStateRef().load(
            self._root,
            intent.binding.issue_number,
        )
        if snapshot is not None:
            observed = tuple(
                record
                for record in snapshot.outbox
                if (
                    record.kind
                    == "post_deployment_evaluation_observed"
                    and record.payload.get("effect_id") == intent.effect_id
                )
            )
            if len(observed) > 1:
                raise RuntimeError("evaluation observations conflict")
            if observed:
                return post_deployment_evaluation_result_from_record(
                    observed[0],
                    intent,
                )
        return self._adapter.reconcile(intent)

    def run(self, intent: Any) -> Any:
        from foundry_opt.orchestration import OutboxRecord
        from foundry_opt.orchestration.deployment import (
            PostDeploymentEvaluationResult,
            PostDeploymentEvaluationStatus,
            post_deployment_evaluation_observation_record,
            post_deployment_evaluation_result_from_record,
        )

        ledger = GitStateRef()
        snapshot = ledger.load(
            self._root,
            intent.binding.issue_number,
        )
        if snapshot is None:
            raise RuntimeError("evaluation claim state is unavailable")
        claim_id = f"{intent.effect_id}-claimed"
        existing = tuple(
            record
            for record in snapshot.outbox
            if record.record_id == claim_id
        )
        if existing:
            if (
                len(existing) != 1
                or existing[0].kind
                != "post_deployment_evaluation_claimed"
                or existing[0].payload.get("effect_id")
                != intent.effect_id
                or existing[0].payload.get("binding_sha256")
                != intent.binding_sha256
            ):
                raise RuntimeError("evaluation claim conflicts")
            result = self._adapter.run(intent)
            if result.status is PostDeploymentEvaluationStatus.PENDING:
                return result
            latest = ledger.load(self._root, intent.binding.issue_number)
            if latest is None:
                raise RuntimeError("evaluation result state is unavailable")
            observation = post_deployment_evaluation_observation_record(
                result,
                sequence=latest.state.sequence,
            )
            existing_observation = tuple(
                record
                for record in latest.outbox
                if record.record_id == observation.record_id
            )
            if existing_observation:
                return post_deployment_evaluation_result_from_record(
                    existing_observation[0],
                    intent,
                )
            try:
                ledger.commit(
                    self._root,
                    issue_number=intent.binding.issue_number,
                    expected_revision=latest.revision,
                    state=latest.state,
                    outbox=(observation,),
                )
            except StateRefError:
                recovered = self.reconcile(intent)
                if recovered is not None:
                    return recovered
                raise
            return result
        claim = OutboxRecord(
            record_id=claim_id,
            kind="post_deployment_evaluation_claimed",
            generation=snapshot.state.generation,
            sequence=snapshot.state.sequence,
            payload={
                "binding_sha256": intent.binding_sha256,
                "candidate_id": intent.binding.candidate_id,
                "effect_id": intent.effect_id,
                "evaluation_id": intent.evaluation_id,
                "idempotency_key": intent.idempotency_key,
                "issue_number": intent.binding.issue_number,
                "result": "claimed",
            },
        )
        try:
            ledger.commit(
                self._root,
                issue_number=intent.binding.issue_number,
                expected_revision=snapshot.revision,
                state=snapshot.state,
                outbox=(claim,),
            )
        except StateRefError:
            return PostDeploymentEvaluationResult(
                result_id=f"{intent.effect_id}-pending",
                intent=intent,
                status=PostDeploymentEvaluationStatus.PENDING,
            )
        return self._adapter.run(intent)

    def _request(self, intent: Any) -> Any:
        from foundry_opt.deployment import OptimizationDeploymentLineage
        from foundry_opt.optimization.lifecycle import PostDeployRequest

        config = load_config(self._root / self._config_path)
        snapshot = GitStateRef().load(
            self._root,
            intent.binding.issue_number,
        )
        if snapshot is None:
            raise ValueError("campaign state is unavailable")
        spec, _ = _production_approved_spec(
            self._root,
            intent.binding.issue_number,
            intent.binding.generation,
            intent.binding.spec_sha256,
        )
        plans = tuple(
            record
            for record in snapshot.outbox
            if (
                record.kind == "applier_worker_issue_planned"
                and record.payload.get("binding_sha256")
                == intent.binding.binding_sha256
            )
        )
        if len(plans) != 1:
            raise ValueError("candidate base commit is unavailable")
        base_commit = str(plans[0].payload["base_commit"])
        campaign_id = (
            f"issue-{intent.binding.issue_number}-"
            f"g{intent.binding.generation}-"
            f"{intent.binding.spec_sha256[:8]}-{base_commit[:8]}"
        )
        campaign = self._state.load(self._root, campaign_id)
        if campaign is None or campaign.finalized is None:
            raise ValueError("finalized campaign state is unavailable")
        lineage = OptimizationDeploymentLineage(
            parent_issue_number=intent.binding.issue_number,
            spec_sha256=intent.binding.spec_sha256,
            campaign_id=campaign_id,
            campaign_pull_request_number=(
                campaign.finalized.campaign_pull_request_number
            ),
            candidate_issue_number=(
                intent.binding.candidate_issue_number
            ),
            candidate_pull_request_number=(
                intent.binding.candidate_pull_request_number
            ),
            candidate_id=intent.binding.candidate_id,
            selected_draft_id=intent.binding.draft_id,
            patch_sha256=intent.binding.patch_sha256,
            evidence_sha256=intent.binding.evidence_sha256,
            selected_tree_sha=intent.binding.tree_sha,
            selected_merge_commit=intent.binding.merge_commit,
        )
        endpoint = str(
            config.environments[spec.environment].project_endpoint
        )
        return PostDeployRequest(
            repository_root=self._root,
            lineage=lineage,
            selected_candidate_id=intent.binding.candidate_id,
            deployment_version=intent.deployment_version,
            project_endpoint=endpoint,
            spec=spec,
        )


class ProductionDeploymentOrchestration:
    """Lazily assemble canonical deployment without deployment credentials."""

    def __init__(
        self,
        *,
        config_path: Path = _DEFAULT_CONFIG_PATH,
        commands: CommandRunner | None = None,
        environment: EnvironmentReader | None = None,
        credential_provider: AzureCredentialProvider | None = None,
    ) -> None:
        self._config_path = config_path
        self._commands = commands or SubprocessCommandRunner()
        self._environment = environment or OsEnvironmentReader()
        self._credential = credential_provider or _default_credential_provider(
            self._environment
        )
        self._evaluations: dict[
            tuple[Path, int], _ProductionPostDeploymentEvaluationEffects
        ] = {}

    def advance(self, request: Any) -> Any:
        from foundry_opt.orchestration.deployment import (
            DeploymentOrchestrationService,
            LedgerDeploymentPublicationVerifier,
        )

        key = (request.repository_root.resolve(), request.issue_number)
        effects = self._evaluations.get(key)
        if effects is None:
            effects = _ProductionPostDeploymentEvaluationEffects(
                key[0],
                self._config_path,
                self._credential,
            )
            self._evaluations[key] = effects
        ledger = GitStateRef()
        return DeploymentOrchestrationService(
            ledger=ledger,
            resolver=_ProductionDeploymentPlanResolver(
                self._commands,
                self._config_path,
            ),
            selection_reader=_ProductionDeploymentSelectionReader(
                self._commands
            ),
            publication_verifier=LedgerDeploymentPublicationVerifier(
                ledger
            ),
            evaluation_effects=effects,
        ).advance(request)


def build_production_steward_deployment(
    *,
    config_path: Path = _DEFAULT_CONFIG_PATH,
) -> ProductionDeploymentOrchestration:
    return ProductionDeploymentOrchestration(config_path=config_path)


def build_production_steward_advance_service(
    *,
    config_path: Path = _DEFAULT_CONFIG_PATH,
) -> StewardAdvanceService:
    """Assemble every canonical phase behind the production steward."""
    return StewardAdvanceService(
        inbox=GitCampaignInbox(),
        spec_policy=build_production_steward_spec_policy(
            config_path=config_path
        ),
        candidate_workers=build_production_steward_candidate_workers(
            config_path=config_path
        ),
        candidate_slate=build_production_steward_candidate_slate(
            config_path=config_path
        ),
        candidate_selection=build_production_steward_candidate_selection(
            config_path=config_path
        ),
        deployment=build_production_steward_deployment(
            config_path=config_path
        ),
    )


def _production_json(
    commands: CommandRunner,
    arguments: tuple[str, ...],
    cwd: Path,
) -> dict[str, Any]:
    try:
        value = json.loads(commands.run(arguments, cwd=cwd).stdout)
    except (json.JSONDecodeError, OSError) as error:
        raise ValueError("production GitHub response is invalid") from error
    if not isinstance(value, dict):
        raise ValueError("production GitHub response is invalid")
    return value


class ProductionOptimizationCommandService(
    CompatibilityOptimizationCommandService
):
    """Compatibility facade backed by the durable steward ledger."""

    def __init__(self, *, config_path: Path = _DEFAULT_CONFIG_PATH) -> None:
        fence = LegacyGenerationFence()
        legacy = _LegacyOptimizationCommandService(
            config_path=config_path
        )
        super().__init__(
            legacy=legacy,
            steward=build_production_steward_advance_service(
                config_path=config_path
            ),
            projector=LegacyCampaignEventProjector(
                artifact_generation=fence.generation,
            ),
            fence=fence,
            precheck=legacy.precheck,
            runtime_namespace=LegacyRuntimeNamespace(),
        )
