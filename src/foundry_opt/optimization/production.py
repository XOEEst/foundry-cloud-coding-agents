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
import re
import shlex
import subprocess
from typing import Any
from urllib.parse import quote

import yaml

from foundry_opt.adapters.campaign_git import (
    CampaignGit,
    remote_default_branch,
)
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
from foundry_opt.github_workflow.models import (
    GitHubCapabilities,
    GitHubPermissionReport,
    IssueReference,
    RepositoryState,
)
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
from foundry_opt.orchestration.git_state import (
    candidate_design_loopback_handoff_session,
    GitStateRef,
    is_verified_copilot_git_proxy,
    StateRefError,
    StateRefPushUnacknowledgedError,
)
from foundry_opt.orchestration.git_transport import (
    compare_and_swap_push,
    GitTransportError,
    remote_revision,
    resolve_safe_push_remote,
)
from foundry_opt.orchestration.issue_intake import GitIssueEventInbox
from foundry_opt.orchestration.models import EventKind
from foundry_opt.orchestration.spec_policy import (
    GitTransportMergedSpecApprovalReader,
    GitPinnedAssetReader,
    OptimizationSpecPolicy,
    OptimizationSpecServiceResolver,
    RepositorySpecPolicy,
    UnresolvedSpecification,
)
from foundry_opt.optimization.models import (
    ApprovalGate,
    AssetProvenance,
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
_NO_GIT_REPLACEMENTS = {"GIT_NO_REPLACE_OBJECTS": "1"}
_CANDIDATE_DESIGN_MAX_SESSION_COMMITS = 10
_CANDIDATE_DESIGN_FILE_MODES = frozenset({"100644", "100755"})
_CANDIDATE_DESIGN_RESERVED_ROOT = ".foundry-optimizer"


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


class _DeferredFoundryResolutionProvider:
    """Pin an exact name/version without contacting Foundry in Copilot."""

    source_type = "foundry"

    def prepare(
        self,
        request: EvaluationAssetRequest,
        context: EvaluationAssetContext,
    ) -> PreparedEvaluationAsset:
        if (
            request.source != "foundry"
            or not request.name
            or not request.version
        ):
            raise ValueError(
                "Foundry assets require an exact name and version"
            )
        return PreparedEvaluationAsset(
            provenance=AssetProvenance(
                asset_id=request.asset_id,
                kind=request.kind,
                source=request.source,
                role=request.role,
                name=request.name,
                version=request.version,
                created_by="foundry-deferred-provider",
                approval_gate=request.approval_gate,
                metrics=request.metrics,
            ),
            files={},
        )


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


def _build_deferred_specification_asset_registry(
    *,
    synthetic_max_rows: int = 200,
) -> EvaluationAssetProviderRegistry:
    registry = EvaluationAssetProviderRegistry()
    registry.register(_DeferredFoundryResolutionProvider())
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


def _git_remote_default_branch(repository_root: Path) -> str:
    return remote_default_branch(repository_root)


class _TrustedIssueOptimizationGateway:
    def __init__(
        self,
        issue: Any,
        pinned: Any,
    ) -> None:
        self._issue = issue
        self._pinned = pinned

    def verify_permissions(
        self,
        required: GitHubCapabilities,
    ) -> GitHubPermissionReport:
        return GitHubPermissionReport(
            required & GitHubCapabilities.METADATA_READ
        )

    def repository_state(self, repository_root: Path) -> RepositoryState:
        return RepositoryState(
            self._issue.repository,
            self._pinned.default_branch,
            self._pinned.commit,
        )

    def get_issue(
        self,
        repository_root: Path,
        issue_number: int,
    ) -> IssueReference | None:
        if issue_number != self._issue.issue_number:
            return None
        return IssueReference(
            issue_number,
            (
                f"https://github.com/{self._issue.repository}/issues/"
                f"{issue_number}"
            ),
            self._issue.title,
            self._issue.body,
        )

    def find_spec_pull_request(
        self,
        repository_root: Path,
        issue_number: int,
    ) -> None:
        return None

    def comment_issue(
        self,
        repository_root: Path,
        issue_number: int,
        body: str,
    ) -> None:
        return None

    def has_issue_comment(
        self,
        repository_root: Path,
        issue_number: int,
        marker: str,
    ) -> bool:
        return True

    def add_labels(
        self,
        repository_root: Path,
        issue_number: int,
        labels: tuple[str, ...],
    ) -> None:
        return None

    def remove_labels(
        self,
        repository_root: Path,
        issue_number: int,
        labels: tuple[str, ...],
    ) -> None:
        return None


class _TrustedInboxSpecResolver:
    def __init__(
        self,
        config: OptimizerConfig,
        *,
        registry: EvaluationAssetProviderRegistry,
        publisher: GitSpecPublisher,
    ) -> None:
        self._config = config
        self._registry = registry
        self._publisher = publisher

    def resolve(
        self,
        repository_root: Path,
        issue_number: int,
    ) -> Any:
        inbox = GitIssueEventInbox(repository_root)
        current = None
        reason = "issue_content_unavailable"
        for event in inbox.events(issue_number):
            if event.kind in {
                EventKind.ISSUE_CREATED,
                EventKind.ISSUE_EDITED,
                EventKind.ISSUE_REOPENED,
            }:
                current = event
                reason = str(
                    event.payload.get(
                        "issue_error",
                        "issue_content_unavailable",
                    )
                )
            elif event.kind is EventKind.ISSUE_DECLASSIFIED:
                current = None
                reason = "issue_declassified"
            elif event.kind is EventKind.ISSUE_CLOSED:
                current = None
                reason = "issue_closed"
        if current is None:
            return UnresolvedSpecification(reason)
        sha256 = current.payload.get("issue_sha256")
        if not isinstance(sha256, str):
            return UnresolvedSpecification(reason)
        issue = inbox.issue_content(issue_number, sha256)
        pinned = CampaignGit(
            default_branch=_git_remote_default_branch
        ).pin_default_branch(repository_root)
        service = OptimizationSpecService(
            self._config,
            registry=self._registry,
            gateway=_TrustedIssueOptimizationGateway(issue, pinned),
            publisher=self._publisher,
            require_issue_label=False,
        )
        return OptimizationSpecServiceResolver(service).resolve(
            repository_root,
            issue_number,
        )


def build_production_steward_spec_policy(
    *,
    config_path: Path = _DEFAULT_CONFIG_PATH,
) -> RepositorySpecPolicy:
    """Build the repository-aware specification policy for the steward."""

    commands = SubprocessCommandRunner()
    pinned_assets = GitPinnedAssetReader(commands)
    approvals = GitTransportMergedSpecApprovalReader(
        commands,
        default_branch=_git_remote_default_branch,
    )

    def factory(repository_root: Path) -> OptimizationSpecPolicy:
        config = load_config(repository_root / config_path)
        resolver = _TrustedInboxSpecResolver(
            config,
            registry=_build_deferred_specification_asset_registry(),
            publisher=GitSpecPublisher(commands),
        )
        return OptimizationSpecPolicy(
            config.automation_policy,
            resolver=resolver,
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
            generation,
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
        generation: int,
    ) -> tuple[Any, ...]:
        from foundry_opt.orchestration.candidate_workers import (
            CandidateAssetsRegistrationPending,
        )
        from foundry_opt.optimization.runner import _asset_reference

        plan = _candidate_assets_registration_plan(
            config,
            spec,
            paths,
            generation,
        )
        snapshot = GitStateRef().load(root, spec.issue_number)
        if snapshot is None:
            raise ValueError("candidate asset state is unavailable")
        success = next(
            (
                record
                for record in snapshot.outbox
                if (
                    record.record_id == f"{plan.effect_id}-succeeded"
                    and record.kind
                    == "candidate_assets_registration_succeeded"
                    and record.generation == plan.generation
                )
            ),
            None,
        )
        if success is None:
            terminal = next(
                (
                    record
                    for record in reversed(snapshot.outbox)
                    if (
                        record.kind == "candidate_capability_failed"
                        and record.payload.get("effect_id")
                        == plan.effect_id
                        and record.payload.get("status") == "terminal"
                    )
                ),
                None,
            )
            if terminal is not None:
                raise CapabilityUnavailableError(
                    "foundry_assets_capability_failed",
                    "trusted Foundry asset capability execution failed",
                )
            raise CandidateAssetsRegistrationPending(plan)
        result = _candidate_assets_registration_result(
            snapshot,
            plan,
            spec,
        )
        return tuple(_asset_reference(asset) for asset in result)


def _candidate_assets_registration_plan(
    config: OptimizerConfig,
    spec: OptimizationSpec,
    paths: Mapping[str, Path | None],
    generation: int,
) -> Any:
    from foundry_opt.orchestration.candidate_workers import (
        CandidateAssetsRegistrationPlan,
    )
    from foundry_opt.orchestration.git_state import StateObject

    assets = [
        {
            **asset.model_dump(mode="json"),
            "path": (
                paths[asset.asset_id].as_posix()
                if paths[asset.asset_id] is not None
                else None
            ),
        }
        for asset in (*spec.datasets, *spec.evaluators)
    ]
    binding = {
        "assets": assets,
        "base_commit": spec.base_commit,
        "environment": spec.environment,
        "generation": generation,
        "issue_number": spec.issue_number,
        "kind": "candidate_assets_registration",
        "schema_version": 1,
        "spec_sha256": spec.sha256,
        "target": spec.target,
    }
    digest = hashlib.sha256(
        json.dumps(
            binding,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    effect_id = f"assets-{spec.issue_number}-{generation}-{digest[:16]}"
    document = {**binding, "effect_id": effect_id}
    intent = StateObject(
        f"objects/capabilities/{effect_id}.json",
        (
            json.dumps(
                document,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
    )
    return CandidateAssetsRegistrationPlan(
        effect_id=effect_id,
        issue_number=spec.issue_number,
        generation=generation,
        spec_sha256=spec.sha256,
        base_commit=spec.base_commit,
        target=spec.target,
        environment=spec.environment,
        max_attempts=config.campaign.transient_retries + 1,
        intent=intent,
    )


def _candidate_assets_registration_result(
    snapshot: Any,
    plan: Any,
    spec: OptimizationSpec,
) -> tuple[AssetProvenance, ...]:
    from foundry_opt.optimization.assets import (
        deterministic_asset_name,
        deterministic_asset_version,
    )

    success = next(
        record
        for record in snapshot.outbox
        if record.record_id == f"{plan.effect_id}-succeeded"
    )
    if (
        success.payload.get("base_commit") != plan.base_commit
        or success.payload.get("effect_id") != plan.effect_id
        or success.payload.get("effect_kind") != "foundry_assets"
        or success.payload.get("issue_number") != plan.issue_number
        or success.payload.get("spec_sha256") != plan.spec_sha256
    ):
        raise ValueError("candidate asset result binding changed")
    path = success.payload.get("capability_path")
    digest = success.payload.get("capability_sha256")
    objects = tuple(
        item for item in snapshot.objects if item.path == path
    )
    if len(objects) != 1 or objects[0].sha256 != digest:
        raise ValueError("candidate asset result object is unavailable")
    try:
        document = json.loads(objects[0].content)
        if (
            not isinstance(document, dict)
            or set(document)
            != {
                "assets",
                "base_commit",
                "effect_id",
                "environment",
                "generation",
                "issue_number",
                "kind",
                "schema_version",
                "spec_sha256",
                "target",
            }
            or document["schema_version"] != 1
            or document["kind"]
            != "candidate_assets_registration_result"
            or document["effect_id"] != plan.effect_id
            or document["issue_number"] != plan.issue_number
            or document["generation"] != plan.generation
            or document["spec_sha256"] != plan.spec_sha256
            or document["base_commit"] != plan.base_commit
            or document["target"] != plan.target
            or document["environment"] != plan.environment
            or not isinstance(document["assets"], list)
        ):
            raise ValueError
        materialized = tuple(
            AssetProvenance.model_validate(item)
            for item in document["assets"]
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(
            "candidate asset result object is invalid"
        ) from error
    expected = (*spec.datasets, *spec.evaluators)
    if len(materialized) != len(expected):
        raise ValueError("candidate asset result count changed")
    by_id = {asset.asset_id: asset for asset in materialized}
    if len(by_id) != len(materialized):
        raise ValueError("candidate asset result identities repeat")
    for approved in expected:
        observed = by_id.get(approved.asset_id)
        if (
            observed is None
            or observed.kind is not approved.kind
            or observed.source != approved.source
            or observed.role != approved.role
            or observed.content_sha256 != approved.content_sha256
            or observed.created_by != approved.created_by
            or observed.approval_gate is not approved.approval_gate
            or observed.metrics != approved.metrics
            or not observed.name
            or not observed.version
            or not observed.remote_id
            or (
                approved.remote_id is not None
                and observed.remote_id != approved.remote_id
            )
            or (
                approved.source in {"foundry", "builtin"}
                and (
                    observed.name != approved.name
                    or observed.version != approved.version
                )
            )
            or (
                approved.source not in {"foundry", "builtin"}
                and observed.name != deterministic_asset_name(approved)
            )
            or (
                approved.kind is AssetKind.DATASET
                and approved.source not in {"foundry", "builtin"}
                and observed.version
                != deterministic_asset_version(approved)
            )
        ):
            raise ValueError("candidate asset result changed approved inputs")
    return tuple(by_id[asset.asset_id] for asset in expected)


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


class _ActionsCandidateDraftEffects:
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


class _ActionsCandidateEvaluationEffects:
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


class _ProductionCandidateDraftEffects:
    """Read Actions-recorded draft results; never call Foundry."""

    def __init__(self, source: _ProductionCandidatePlanSource) -> None:
        self._source = source

    def reconcile(self, intent: Any) -> DraftRecord | None:
        root, _, _, _ = self._source.resolved_for(
            intent.issue_number,
            intent.generation,
            intent.spec_sha256,
        )
        snapshot = GitStateRef().load(root, intent.issue_number)
        if snapshot is None:
            raise ValueError("candidate draft state is unavailable")
        success = _candidate_capability_success(
            snapshot,
            intent.effect_id,
            "foundry_draft",
        )
        if success is None:
            _raise_terminal_candidate_capability(
                snapshot,
                intent.effect_id,
                "foundry_drafts_capability_failed",
            )
            return None
        if (
            success.payload.get("bundle_sha256")
            != intent.bundle.sha256
            or success.payload.get("candidate_id") != intent.subject_id
        ):
            raise ValueError("candidate draft result binding changed")
        return DraftRecord(
            agent_name=intent.target,
            version_id=str(success.payload["draft_id"]),
            base_version=intent.base_agent_version,
            sha256=intent.bundle.sha256,
            status="draft",
        )

    def create(self, intent: Any) -> DraftRecord:
        from foundry_opt.orchestration.candidate_workers import (
            CandidateEffectPending,
        )

        raise CandidateEffectPending("foundry_draft")


class _ProductionCandidateEvaluationEffects:
    """Read Actions-recorded normalized results; never call Foundry."""

    def __init__(self, source: _ProductionCandidatePlanSource) -> None:
        self._source = source

    def reconcile(self, intent: Any) -> Any | None:
        from foundry_opt.orchestration.capability_bridge import (
            evaluation_result_from_state_object,
        )
        from foundry_opt.orchestration.candidate_workers import (
            _validate_evaluation_result,
        )

        root, _, _, _ = self._source.resolved_for(
            intent.issue_number,
            intent.generation,
            intent.spec_sha256,
        )
        snapshot = GitStateRef().load(root, intent.issue_number)
        if snapshot is None:
            raise ValueError("candidate evaluation state is unavailable")
        success = _candidate_capability_success(
            snapshot,
            intent.effect_id,
            "foundry_evaluation",
        )
        if success is None:
            _raise_terminal_candidate_capability(
                snapshot,
                intent.effect_id,
                "foundry_evaluation_capability_failed",
            )
            return None
        path = success.payload.get("capability_path")
        digest = success.payload.get("capability_sha256")
        objects = tuple(
            item for item in snapshot.objects if item.path == path
        )
        if len(objects) != 1 or objects[0].sha256 != digest:
            raise ValueError(
                "candidate evaluation result object is unavailable"
            )
        result = evaluation_result_from_state_object(
            objects[0],
            effect_id=intent.effect_id,
            issue_number=intent.issue_number,
            generation=intent.generation,
            spec_sha256=intent.spec_sha256,
            base_commit=intent.base_commit,
            idempotency_key=intent.idempotency_key,
        )
        if (
            success.payload.get("evaluation_id")
            != result.run.evaluation_id
            or success.payload.get("run_id") != result.run.run_id
            or (
                success.payload.get("idempotency_key") is not None
                and success.payload.get("idempotency_key")
                != intent.idempotency_key
            )
        ):
            raise ValueError(
                "candidate evaluation result binding changed"
            )
        _validate_evaluation_result(intent, result)
        return result

    def run(self, intent: Any) -> Any:
        from foundry_opt.orchestration.candidate_workers import (
            CandidateEffectPending,
        )

        raise CandidateEffectPending("foundry_evaluation")


def _candidate_capability_success(
    snapshot: Any,
    effect_id: str,
    effect_kind: str,
) -> Any | None:
    matches = tuple(
        record
        for record in snapshot.outbox
        if record.record_id == f"{effect_id}-succeeded"
    )
    if not matches:
        return None
    if (
        len(matches) != 1
        or matches[0].kind != "candidate_effect_succeeded"
        or matches[0].payload.get("effect_id") != effect_id
        or matches[0].payload.get("effect_kind") != effect_kind
        or matches[0].generation != snapshot.state.generation
    ):
        raise ValueError("candidate capability result is invalid")
    return matches[0]


def _raise_terminal_candidate_capability(
    snapshot: Any,
    effect_id: str,
    code: str,
) -> None:
    if any(
        record.kind == "candidate_capability_failed"
        and record.payload.get("effect_id") == effect_id
        and record.payload.get("status") == "terminal"
        for record in snapshot.outbox
    ):
        raise CapabilityUnavailableError(
            code,
            "trusted Foundry capability execution failed",
        )


class _ProductionCandidateCapabilityExecutor:
    """Execute persisted Foundry intents in trusted Actions only."""

    def __init__(
        self,
        *,
        config_path: Path,
        commands: CommandRunner,
        credential: AzureCredentialProvider,
        resolution_gateway_factory: ResolutionGatewayFactory,
        registration_gateway_factory: RegistrationGatewayFactory,
        binder_factory: BinderFactory | None,
        draft_gateway: Any | None,
    ) -> None:
        self._config_path = config_path
        self._commands = commands
        self._credential = credential
        self._resolution_factory = resolution_gateway_factory
        self._registration_factory = registration_gateway_factory
        self._binder_factory = binder_factory
        self._draft_gateway = draft_gateway
        self._source = _ProductionCandidatePlanSource(
            config_path=config_path,
            registration_gateway_factory=registration_gateway_factory,
        )

    def reconcile(
        self,
        repository_root: Path,
        snapshot: Any,
        planned: Any,
    ) -> Any:
        return self._execute(repository_root, snapshot, planned)

    def execute(
        self,
        repository_root: Path,
        snapshot: Any,
        planned: Any,
    ) -> Any:
        return self._execute(repository_root, snapshot, planned)

    def _execute(
        self,
        repository_root: Path,
        snapshot: Any,
        planned: Any,
    ) -> Any:
        from foundry_opt.orchestration.capability_bridge import (
            CandidateCapabilityExecutionError,
        )

        try:
            effect_kind = planned.payload.get("effect_kind")
            if effect_kind == "foundry_assets":
                return self._assets(repository_root, snapshot, planned)
            if effect_kind == "foundry_draft":
                return self._draft(repository_root, snapshot, planned)
            if effect_kind == "foundry_evaluation":
                return self._evaluation(
                    repository_root,
                    snapshot,
                    planned,
                )
            raise ValueError("candidate capability effect kind is invalid")
        except CandidateCapabilityExecutionError:
            raise
        except CapabilityUnavailableError as error:
            raise CandidateCapabilityExecutionError(
                error.code,
                retryable=True,
            ) from error
        except Exception as error:
            raise CandidateCapabilityExecutionError(
                "candidate_capability_execution_failed",
                retryable=False,
            ) from error

    def _assets(
        self,
        root: Path,
        snapshot: Any,
        planned: Any,
    ) -> Any:
        from foundry_opt.optimization.assets import (
            canonicalize_repository_asset_content,
            materialize_prepared_asset,
        )
        from foundry_opt.orchestration.capability_bridge import (
            CandidateCapabilityExecution,
        )

        config = load_config(root / self._config_path)
        spec, paths = _production_approved_spec(
            root,
            snapshot.state.issue_number,
            snapshot.state.generation,
            str(snapshot.state.spec_sha256),
        )
        expected = _candidate_assets_registration_plan(
            config,
            spec,
            paths,
            snapshot.state.generation,
        )
        if (
            planned.record_id != expected.effect_id
            or planned.kind != "candidate_assets_registration_planned"
            or dict(planned.payload) != dict(expected.payload)
            or not any(
                item == expected.intent for item in snapshot.objects
            )
        ):
            raise ValueError("candidate asset intent changed")
        registration = _RegistrationGateway(
            config,
            spec.environment,
            self._registration_factory,
        )
        endpoint = _environment_endpoint(config, spec.environment)
        materialized: list[AssetProvenance] = []
        for asset in (*spec.datasets, *spec.evaluators):
            path = paths[asset.asset_id]
            if path is not None:
                content = canonicalize_repository_asset_content(
                    _production_git_bytes(root, spec.base_commit, path)
                )
                if (
                    asset.content_sha256 is None
                    or hashlib.sha256(content).hexdigest()
                    != asset.content_sha256
                ):
                    raise ValueError(
                        "candidate asset content changed"
                    )
                materialized.append(
                    materialize_prepared_asset(
                        PreparedEvaluationAsset(
                            provenance=asset,
                            files={path: content},
                        ),
                        registration,
                    )
                )
                continue
            if asset.source == "builtin":
                if asset.remote_id is None:
                    raise ValueError(
                        "builtin candidate asset is not pinned"
                    )
                materialized.append(asset)
                continue
            if asset.source == "foundry":
                if not asset.name or not asset.version:
                    raise ValueError(
                        "Foundry candidate asset is not pinned"
                    )
                remote_id = self._resolution_factory(endpoint).resolve(
                    kind=asset.kind,
                    name=asset.name,
                    version=asset.version,
                )
                if not isinstance(remote_id, str) or not remote_id:
                    raise ValueError(
                        "Foundry candidate asset identity is invalid"
                    )
                if asset.remote_id is not None and remote_id != asset.remote_id:
                    raise ValueError(
                        "Foundry candidate asset identity changed"
                    )
                materialized.append(
                    asset.model_copy(update={"remote_id": remote_id})
                )
                continue
            if asset.remote_id is None:
                raise ValueError(
                    "candidate asset has no materialization path"
                )
            materialized.append(asset)
        result_object = _candidate_assets_result_object(
            expected,
            tuple(materialized),
        )
        return CandidateCapabilityExecution(
            record_kind="candidate_assets_registration_succeeded",
            payload={
                "base_commit": spec.base_commit,
                "capability_path": result_object.path,
                "capability_sha256": result_object.sha256,
                "effect_id": expected.effect_id,
                "effect_kind": "foundry_assets",
                "issue_number": spec.issue_number,
                "result_id": f"{expected.effect_id}-result",
                "spec_sha256": spec.sha256,
            },
            objects=(result_object,),
        )

    def _draft(
        self,
        root: Path,
        snapshot: Any,
        planned: Any,
    ) -> Any:
        from foundry_opt.orchestration.capability_bridge import (
            CandidateCapabilityExecution,
        )
        from foundry_opt.orchestration.candidate_workers import (
            CandidateWorkerRequest,
            _draft_intent,
        )

        plan = _ProductionCandidateWorkerPlanResolver(self._source).resolve(
            CandidateWorkerRequest(root, snapshot.state.issue_number),
            snapshot.state,
        )
        candidate_id = str(planned.payload["candidate_id"])
        try:
            bundle = self._build_exact_bundle(
                root,
                plan,
                planned.record_id,
                snapshot,
                candidate_id,
                str(planned.payload["bundle_sha256"]),
            )
            intent = _draft_intent(
                plan,
                candidate_id,
                bundle,
            )
            if (
                intent.effect_id != planned.record_id
                or intent.idempotency_key
                != planned.payload.get("idempotency_key")
                or dict(planned.payload)
                != {
                    "base_commit": plan.base_commit,
                    "bundle_sha256": bundle.sha256,
                    "candidate_id": candidate_id,
                    "effect_id": intent.effect_id,
                    "effect_kind": "foundry_draft",
                    "idempotency_key": intent.idempotency_key,
                    "issue_number": intent.issue_number,
                    "max_attempts": plan.limits.transient_retries + 1,
                    "slot": _candidate_slot(candidate_id),
                    "spec_sha256": intent.spec_sha256,
                }
            ):
                raise ValueError("candidate draft intent changed")
            draft = _ActionsCandidateDraftEffects(
                self._source,
                self._credential,
                self._draft_gateway,
            ).reconcile(intent)
            if draft is None:
                raise CapabilityUnavailableError(
                    "foundry_drafts_unavailable",
                    "candidate draft could not be reconciled",
                )
            return CandidateCapabilityExecution(
                record_kind="candidate_effect_succeeded",
                payload={
                    "base_commit": plan.base_commit,
                    "bundle_sha256": bundle.sha256,
                    "candidate_id": intent.subject_id,
                    "draft_id": draft.version_id,
                    "effect_id": intent.effect_id,
                    "effect_kind": "foundry_draft",
                    "issue_number": intent.issue_number,
                    "spec_sha256": intent.spec_sha256,
                },
            )
        finally:
            self._remove_capability_worktree(root, planned.record_id)

    def _evaluation(
        self,
        root: Path,
        snapshot: Any,
        planned: Any,
    ) -> Any:
        from foundry_opt.orchestration.capability_bridge import (
            CandidateCapabilityExecution,
            evaluation_result_state_object,
        )
        from foundry_opt.orchestration.candidate_workers import (
            CandidateWorkerRequest,
            _aggregate_metrics,
            _evaluation_intent,
            _validate_evaluation_result,
        )

        plan = _ProductionCandidateWorkerPlanResolver(self._source).resolve(
            CandidateWorkerRequest(root, snapshot.state.issue_number),
            snapshot.state,
        )
        candidate_id = str(planned.payload["candidate_id"])
        draft_record = _candidate_capability_success(
            snapshot,
            _draft_effect_id(plan, candidate_id),
            "foundry_draft",
        )
        if draft_record is None:
            raise ValueError("candidate evaluation draft is unavailable")
        draft = DraftRecord(
            plan.target,
            str(draft_record.payload["draft_id"]),
            plan.base_agent_version,
            str(draft_record.payload["bundle_sha256"]),
            "draft",
        )
        intent = _evaluation_intent(plan, candidate_id, draft)
        expected_payload = {
            "base_commit": plan.base_commit,
            "candidate_id": candidate_id,
            "effect_id": intent.effect_id,
            "effect_kind": "foundry_evaluation",
            "idempotency_key": intent.idempotency_key,
            "issue_number": intent.issue_number,
            "max_attempts": plan.limits.transient_retries + 1,
            "slot": _candidate_slot(candidate_id),
            "spec_sha256": intent.spec_sha256,
        }
        legacy_payload = {
            key: value
            for key, value in expected_payload.items()
            if key != "idempotency_key"
        }
        if (
            intent.effect_id != planned.record_id
            or dict(planned.payload)
            not in (expected_payload, legacy_payload)
        ):
            raise ValueError("candidate evaluation intent changed")
        result = _ActionsCandidateEvaluationEffects(
            self._source,
            self._credential,
            self._binder_factory,
        ).reconcile(intent)
        if result is None:
            raise CapabilityUnavailableError(
                "foundry_evaluation_unavailable",
                "candidate evaluation could not be reconciled",
            )
        _validate_evaluation_result(intent, result)
        result_object = evaluation_result_state_object(
            effect_id=intent.effect_id,
            issue_number=intent.issue_number,
            generation=intent.generation,
            spec_sha256=intent.spec_sha256,
            base_commit=intent.base_commit,
            idempotency_key=intent.idempotency_key,
            result=result,
        )
        return CandidateCapabilityExecution(
            record_kind="candidate_effect_succeeded",
            payload={
                "base_commit": plan.base_commit,
                "candidate_id": candidate_id,
                "capability_path": result_object.path,
                "capability_sha256": result_object.sha256,
                "effect_id": intent.effect_id,
                "effect_kind": "foundry_evaluation",
                "evaluation_id": result.run.evaluation_id,
                "idempotency_key": intent.idempotency_key,
                "issue_number": intent.issue_number,
                "metrics": _aggregate_metrics(result),
                "run_id": result.run.run_id,
                "spec_sha256": intent.spec_sha256,
            },
            objects=(result_object,),
        )

    def _build_exact_bundle(
        self,
        root: Path,
        plan: Any,
        effect_id: str,
        snapshot: Any,
        candidate_id: str,
        expected_sha256: str,
    ) -> BundleArtifact:
        worktree = _capability_worktree_path(root, effect_id)
        self._remove_capability_worktree(root, effect_id)
        worktree.parent.mkdir(parents=True, exist_ok=True)
        self._commands.run(
            (
                "git",
                "worktree",
                "add",
                "--detach",
                "--force",
                str(worktree),
                plan.base_commit,
            ),
            cwd=root,
        )
        if candidate_id != "baseline":
            artifact, patch = _candidate_patch_object(
                snapshot,
                plan,
                candidate_id,
            )
            self._commands.run(
                (
                    "git",
                    "apply",
                    "--index",
                    "--binary",
                    "--whitespace=nowarn",
                    "-",
                ),
                cwd=worktree,
                input_bytes=patch.content,
            )
            tree = self._commands.run(
                ("git", "write-tree"),
                cwd=worktree,
            ).stdout.strip()
            if tree != artifact.payload.get("tree_sha"):
                raise ValueError(
                    "candidate capability patch tree changed"
                )
        config = load_config(worktree / self._config_path)
        bundle = build_source_bundle(
            _bundle_request(
                config,
                worktree,
                worktree / ".foundry-opt-capability.zip",
            )
        )
        if bundle.sha256 != expected_sha256:
            raise ValueError("candidate capability bundle changed")
        return bundle

    def _remove_capability_worktree(
        self,
        root: Path,
        effect_id: str,
    ) -> None:
        import shutil

        worktree = _capability_worktree_path(root, effect_id)
        try:
            self._commands.run(
                (
                    "git",
                    "worktree",
                    "remove",
                    "--force",
                    str(worktree),
                ),
                cwd=root,
            )
        except Exception:
            pass
        if worktree.exists():
            shutil.rmtree(worktree)
        try:
            self._commands.run(
                ("git", "worktree", "prune"),
                cwd=root,
            )
        except Exception:
            pass


def _capability_worktree_path(root: Path, effect_id: str) -> Path:
    if re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
        effect_id,
    ) is None:
        raise ValueError("candidate capability effect ID is invalid")
    repository_root = root.expanduser().resolve()
    current = repository_root
    for part in (".foundry-optimizer", "capability-worktrees"):
        current = current / part
        if current.is_symlink():
            raise ValueError(
                "candidate capability worktree parent is unsafe"
            )
    worktree = current / effect_id
    if worktree.is_symlink():
        raise ValueError("candidate capability worktree is unsafe")
    parent = worktree.parent.resolve()
    if not parent.is_relative_to(repository_root):
        raise ValueError("candidate capability worktree escapes repository")
    return parent / effect_id


def _candidate_assets_result_object(
    plan: Any,
    assets: tuple[AssetProvenance, ...],
) -> Any:
    from foundry_opt.orchestration.git_state import StateObject

    document = {
        "assets": [
            asset.model_dump(mode="json") for asset in assets
        ],
        "base_commit": plan.base_commit,
        "effect_id": plan.effect_id,
        "environment": plan.environment,
        "generation": plan.generation,
        "issue_number": plan.issue_number,
        "kind": "candidate_assets_registration_result",
        "schema_version": 1,
        "spec_sha256": plan.spec_sha256,
        "target": plan.target,
    }
    return StateObject(
        f"objects/capabilities/{plan.effect_id}-result.json",
        (
            json.dumps(
                document,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
    )


def _candidate_patch_object(
    snapshot: Any,
    plan: Any,
    candidate_id: str,
) -> tuple[Any, Any]:
    records = tuple(
        record
        for record in snapshot.outbox
        if (
            record.kind == "candidate_artifact_ready"
            and record.generation == plan.generation
            and record.payload.get("candidate_id") == candidate_id
        )
    )
    if len(records) != 1:
        raise ValueError("candidate artifact is unavailable")
    patch_sha256 = records[0].payload.get("patch_sha256")
    objects = tuple(
        item
        for item in snapshot.objects
        if item.path == f"objects/patches/{patch_sha256}.patch"
    )
    if (
        len(objects) != 1
        or objects[0].sha256 != patch_sha256
        or records[0].payload.get("base_commit") != plan.base_commit
        or records[0].payload.get("spec_sha256") != plan.spec_sha256
    ):
        raise ValueError("candidate patch object is unavailable")
    return records[0], objects[0]


def _draft_effect_id(plan: Any, candidate_id: str) -> str:
    return f"draft-{plan.issue_number}-{plan.generation}-{candidate_id}"


def _candidate_slot(candidate_id: str) -> int:
    if candidate_id == "baseline":
        return 0
    match = re.fullmatch(r"candidate-([1-9][0-9]*)", candidate_id)
    if match is None:
        raise ValueError("candidate capability subject is invalid")
    return int(match.group(1))


class _ProductionCandidateDesignRepository:
    """Capture specialist edits on a deterministic remote result ref."""

    def __init__(self, commands: CommandRunner) -> None:
        self._commands = commands

    def capture(self, request: Any, intent: Any, result: Any) -> Any:
        from foundry_opt.orchestration.candidate_workers import (
            CandidateDesignArtifact,
            CandidateDesignPushUnacknowledgedError,
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
        session_commits = _candidate_designer_checkout_commits(
            self._commands,
            root,
            intent.base_commit,
        )
        if session_commits is None:
            raise ValueError("candidate designer checkout base changed")
        result_path = expected_result.relative_to(root)
        head_commit = (
            session_commits[-1]
            if session_commits
            else intent.base_commit
        )
        parent = intent.base_commit
        for commit in session_commits:
            entries = _production_diff_entries(
                self._commands,
                root,
                parent,
                commit,
            )
            if not _candidate_design_tree_entries_are_allowed(
                self._commands,
                root,
                commit,
                entries,
                intent.edit_paths,
            ):
                raise ValueError("candidate design changed forbidden paths")
            parent = commit
        committed_entries = _production_diff_entries(
            self._commands,
            root,
            intent.base_commit,
            head_commit,
        )
        if not _candidate_design_tree_entries_are_allowed(
            self._commands,
            root,
            head_commit,
            committed_entries,
            intent.edit_paths,
        ):
            raise ValueError("candidate design changed forbidden paths")
        dirty_paths = _production_changed_paths(self._commands, root)
        if any(
            path != result_path
            and (
                _candidate_design_path_is_reserved(path)
                or not _path_is_allowed(path, intent.edit_paths)
            )
            for path in dirty_paths
        ):
            raise ValueError("candidate design changed forbidden paths")
        index = Path(
            self._commands.run(
                (
                    "git",
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-path",
                    f"foundry-design-indexes/{intent.effect_id}.index",
                ),
                cwd=root,
                environment=_NO_GIT_REPLACEMENTS,
            ).stdout.strip()
        )
        index.parent.mkdir(parents=True, exist_ok=True)
        index.unlink(missing_ok=True)
        environment = {
            **_NO_GIT_REPLACEMENTS,
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
            self._commands.run(
                (
                    "git",
                    "reset",
                    "--quiet",
                    intent.base_commit,
                    "--",
                    _CANDIDATE_DESIGN_RESERVED_ROOT,
                ),
                cwd=root,
                environment=environment,
            )
            tree = self._commands.run(
                ("git", "write-tree"),
                cwd=root,
                environment=environment,
            ).stdout.strip()
            staged_paths = _production_tree_changed_paths(
                self._commands,
                root,
                intent.base_commit,
                tree,
            )
            staged_entries = _production_diff_entries(
                self._commands,
                root,
                intent.base_commit,
                tree,
            )
            staged_entry_paths = tuple(
                sorted(path for path, _, _, _ in staged_entries)
            )
            if (
                not staged_paths
                or staged_paths != staged_entry_paths
                or not _candidate_design_tree_entries_are_allowed(
                    self._commands,
                    root,
                    tree,
                    staged_entries,
                    intent.edit_paths,
                )
            ):
                raise ValueError(
                    "candidate design changed forbidden paths"
                )
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
                input_bytes=(
                    "Capture candidate design "
                    f"{intent.effect_id}\n"
                ).encode("utf-8"),
            ).stdout.strip()
            ref = (
                "refs/heads/foundry-opt/design/"
                f"issue-{intent.issue_number}/{intent.effect_id}"
            )
            artifact = CandidateDesignArtifact(
                ref=ref,
                head_commit=commit,
                tree_sha=tree,
                changed_paths=staged_paths,
            )
            if candidate_design_loopback_handoff_session(root) is not None:
                raise CandidateDesignPushUnacknowledgedError(
                    artifact
                )
            safe_remote = resolve_safe_push_remote(root, "origin")
            if safe_remote is None:
                raise CandidateDesignPushUnacknowledgedError(
                    artifact
                )
            try:
                existing = remote_revision(root, safe_remote, ref)
            except GitTransportError as error:
                raise ValueError(
                    "candidate design remote query failed"
                ) from error
            if existing is not None:
                if existing != commit:
                    raise ValueError("candidate design ref changed")
            else:
                try:
                    pushed = compare_and_swap_push(
                        root,
                        safe_remote,
                        source_revision=commit,
                        destination_ref=ref,
                        expected_revision=None,
                    )
                except GitTransportError as error:
                    raise ValueError(
                        "candidate design transport failed"
                    ) from error
                if pushed.before is not None or pushed.returncode != 0:
                    raise CandidateDesignPushUnacknowledgedError(
                        artifact
                    )
                if pushed.after != commit:
                    raise ValueError(
                        "candidate design ref acknowledgement changed"
                    )
        finally:
            index.unlink(missing_ok=True)
            try:
                index.parent.rmdir()
            except OSError:
                pass
        return artifact

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
        if ref != (
            "refs/heads/foundry-opt/design/"
            f"issue-{intent.issue_number}/{intent.effect_id}"
        ):
            raise ValueError("candidate design Git binding changed")
        if _git_replacements_present(self._commands, root):
            raise ValueError("candidate design Git binding changed")
        self._commands.run(
            ("git", "cat-file", "-e", f"{head_commit}^{{commit}}"),
            cwd=root,
            environment=_NO_GIT_REPLACEMENTS,
        )
        parent = self._commands.run(
            ("git", "rev-parse", f"{head_commit}^"),
            cwd=root,
            environment=_NO_GIT_REPLACEMENTS,
        ).stdout.strip()
        fetched_tree = self._commands.run(
            ("git", "rev-parse", f"{head_commit}^{{tree}}"),
            cwd=root,
            environment=_NO_GIT_REPLACEMENTS,
        ).stdout.strip()
        if (
            parent != intent.base_commit
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
                        head_commit,
                        "--",
                    ),
                    cwd=root,
                    environment=_NO_GIT_REPLACEMENTS,
                ).stdout.split("\0")
                if value
            )
        )
        observed_entries = _production_diff_entries(
            self._commands,
            root,
            intent.base_commit,
            head_commit,
        )
        if (
            observed_changed != expected_changed
            or observed_changed
            != tuple(
                sorted(path for path, _, _, _ in observed_entries)
            )
            or any(
                not _path_is_allowed(path, intent.edit_paths)
                for path in observed_changed
            )
            or not _candidate_design_tree_entries_are_allowed(
                self._commands,
                root,
                head_commit,
                observed_entries,
                intent.edit_paths,
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
                head_commit,
                "--",
            ),
            cwd=root,
            environment=_NO_GIT_REPLACEMENTS,
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
            environment=_NO_GIT_REPLACEMENTS,
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
            environment=_NO_GIT_REPLACEMENTS,
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
            environment=_NO_GIT_REPLACEMENTS,
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
        from foundry_opt.orchestration.models import CampaignPhase

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
        repository = spec.repository
        pinned = CampaignGit(
            default_branch=_git_remote_default_branch
        ).pin_default_branch(root)
        if (
            pinned.commit != spec.base_commit
            and state.phase
            not in {
                CampaignPhase.AWAITING_SELECTION,
                CampaignPhase.DEPLOYMENT,
            }
        ):
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
            default_branch=pinned.default_branch,
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


def _candidate_designer_checkout_commits(
    commands: CommandRunner,
    root: Path,
    base_commit: str,
) -> tuple[str, ...] | None:
    if _git_replacements_present(commands, root):
        return None
    try:
        base = commands.run(
            ("git", "rev-parse", f"{base_commit}^{{commit}}"),
            cwd=root,
            environment=_NO_GIT_REPLACEMENTS,
        ).stdout.strip()
        head = commands.run(
            ("git", "rev-parse", "HEAD^{commit}"),
            cwd=root,
            environment=_NO_GIT_REPLACEMENTS,
        ).stdout.strip()
        if base != base_commit:
            return None
        commits: list[str] = []
        current = head
        while current != base:
            if len(commits) >= _CANDIDATE_DESIGN_MAX_SESSION_COMMITS:
                return None
            raw_commit = commands.run(
                ("git", "cat-file", "commit", current),
                cwd=root,
                environment=_NO_GIT_REPLACEMENTS,
            ).stdout
            parents: list[str] = []
            for line in raw_commit.splitlines():
                if not line:
                    break
                if line.startswith("parent "):
                    parents.append(line.removeprefix("parent "))
            if len(parents) != 1:
                return None
            commits.append(current)
            current = parents[0]
        return tuple(reversed(commits))
    except Exception:
        return None


def _git_replacements_present(
    commands: CommandRunner,
    root: Path,
) -> bool:
    try:
        return bool(
            commands.run(
                ("git", "replace", "--list"),
                cwd=root,
                environment=_NO_GIT_REPLACEMENTS,
            ).stdout.strip()
        )
    except Exception:
        return True


def _production_changed_paths(
    commands: CommandRunner,
    root: Path,
) -> tuple[Path, ...]:
    tracked = tuple(
        commands.run(
            arguments,
            cwd=root,
            environment=_NO_GIT_REPLACEMENTS,
        ).stdout
        for arguments in (
            (
                "git",
                "diff",
                "--no-ext-diff",
                "--no-renames",
                "--name-only",
                "-z",
                "--cached",
                "HEAD",
                "--",
            ),
            (
                "git",
                "diff",
                "--no-ext-diff",
                "--no-renames",
                "--name-only",
                "-z",
                "--",
            ),
        )
    )
    untracked = commands.run(
        (
            "git",
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ),
        cwd=root,
        environment=_NO_GIT_REPLACEMENTS,
    ).stdout
    return tuple(
        Path(value)
        for value in sorted(
            {
                value
                for document in (*tracked, untracked)
                for value in document.split("\0")
                if value
            }
        )
    )


def _production_tree_changed_paths(
    commands: CommandRunner,
    root: Path,
    base_commit: str,
    tree: str,
) -> tuple[Path, ...]:
    return tuple(
        sorted(
            Path(value)
            for value in commands.run(
                (
                    "git",
                    "diff",
                    "--no-ext-diff",
                    "--no-renames",
                    "--name-only",
                    "-z",
                    base_commit,
                    tree,
                    "--",
                ),
                cwd=root,
                environment=_NO_GIT_REPLACEMENTS,
            ).stdout.split("\0")
            if value
        )
    )


def _production_diff_entries(
    commands: CommandRunner,
    root: Path,
    before: str,
    after: str,
) -> tuple[tuple[Path, str, str, str], ...]:
    raw = commands.run(
        (
            "git",
            "diff",
            "--raw",
            "-z",
            "--no-abbrev",
            "--no-ext-diff",
            "--no-renames",
            before,
            after,
            "--",
        ),
        cwd=root,
        environment=_NO_GIT_REPLACEMENTS,
    ).stdout
    values = raw.split("\0")
    entries: list[tuple[Path, str, str, str]] = []
    index = 0
    while index < len(values) and values[index]:
        if index + 1 >= len(values):
            raise ValueError("candidate design Git diff is invalid")
        fields = values[index].split()
        path = values[index + 1]
        if (
            len(fields) != 5
            or not fields[0].startswith(":")
            or not path
        ):
            raise ValueError("candidate design Git diff is invalid")
        entries.append(
            (
                Path(path),
                fields[0].removeprefix(":"),
                fields[1],
                fields[4],
            )
        )
        index += 2
    if any(values[index:]):
        raise ValueError("candidate design Git diff is invalid")
    return tuple(entries)


def _candidate_design_path_is_reserved(path: Path) -> bool:
    return bool(
        path.parts
        and path.parts[0].casefold()
        == _CANDIDATE_DESIGN_RESERVED_ROOT.casefold()
    )


def _candidate_design_tree_entries_are_allowed(
    commands: CommandRunner,
    root: Path,
    revision: str,
    entries: tuple[tuple[Path, str, str, str], ...],
    allowed: tuple[Path, ...],
) -> bool:
    for path, _, new_mode, _ in entries:
        if (
            _candidate_design_path_is_reserved(path)
            or not _path_is_allowed(path, allowed)
            or new_mode not in _CANDIDATE_DESIGN_FILE_MODES
        ):
            return False
        raw = commands.run(
            (
                "git",
                "ls-tree",
                "-z",
                revision,
                "--",
                f":(literal){path.as_posix()}",
            ),
            cwd=root,
            environment=_NO_GIT_REPLACEMENTS,
        ).stdout
        records = tuple(value for value in raw.split("\0") if value)
        if len(records) != 1 or "\t" not in records[0]:
            return False
        metadata, observed_path = records[0].split("\t", 1)
        fields = metadata.split()
        if (
            len(fields) != 3
            or fields[0] != new_mode
            or fields[1] != "blob"
            or observed_path != path.as_posix()
        ):
            return False
    return True


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
    campaign_repository = repository or CampaignGit(
        default_branch=_git_remote_default_branch
    )
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
            drafts=_ProductionCandidateDraftEffects(source),
            evaluations=_ProductionCandidateEvaluationEffects(source),
            write_evidence=write_redacted_evidence,
            clock=UtcClock(),
        ),
    )


def build_production_candidate_capability_bridge(
    *,
    assignments: Any,
    config_path: Path = _DEFAULT_CONFIG_PATH,
    command_runner: CommandRunner | None = None,
    environment: EnvironmentReader | None = None,
    credential_provider: AzureCredentialProvider | None = None,
    resolution_gateway_factory: ResolutionGatewayFactory | None = None,
    registration_gateway_factory: RegistrationGatewayFactory | None = None,
    binder_factory: BinderFactory | None = None,
    draft_gateway: Any | None = None,
    ledger: Any | None = None,
) -> Any:
    from foundry_opt.orchestration.capability_bridge import (
        CandidateCapabilityBridge,
    )

    commands = command_runner or SubprocessCommandRunner()
    reader = environment or OsEnvironmentReader()
    credential = credential_provider or _default_credential_provider(reader)
    resolution = (
        resolution_gateway_factory
        or _default_resolution_gateway_factory(credential)
    )
    registration = (
        registration_gateway_factory
        or _default_registration_gateway_factory(credential)
    )
    return CandidateCapabilityBridge(
        ledger=ledger or GitStateRef(),
        executor=_ProductionCandidateCapabilityExecutor(
            config_path=config_path,
            commands=commands,
            credential=credential,
            resolution_gateway_factory=resolution,
            registration_gateway_factory=registration,
            binder_factory=binder_factory,
            draft_gateway=draft_gateway,
        ),
        assignments=assignments,
    )


def build_production_candidate_design_submission_service(
    *,
    command_runner: CommandRunner | None = None,
) -> Any:
    from foundry_opt.orchestration.candidate_workers import (
        CandidateDesignSubmissionService,
    )
    from foundry_opt.orchestration.handoff import CloudHandoffStore

    commands = command_runner or SubprocessCommandRunner()
    return CandidateDesignSubmissionService(
        ledger=GitStateRef(),
        repository=_ProductionCandidateDesignRepository(commands),
        handoffs=CloudHandoffStore(),
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
        ):
            raise ValueError("GitHub repository identity is unavailable")
        if spec.repository != repository:
            raise ValueError("deployment repository changed")
        repository_id = _production_json(
            self._commands,
            (
                "gh",
                "api",
                f"repos/{repository}",
            ),
            root,
        ).get("id")
        if type(repository_id) is not int:
            raise ValueError("GitHub repository ID is unavailable")
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
        if is_verified_copilot_git_proxy(self._root):
            return None
        return self._adapter.reconcile(intent)

    def run(self, intent: Any) -> Any:
        from foundry_opt.orchestration import OutboxRecord
        from foundry_opt.orchestration.deployment import (
            PostDeploymentEvaluationResult,
            PostDeploymentEvaluationStatus,
            post_deployment_evaluation_observation_record,
            post_deployment_evaluation_result_from_record,
        )

        if is_verified_copilot_git_proxy(self._root):
            return PostDeploymentEvaluationResult(
                result_id=f"{intent.effect_id}-pending",
                intent=intent,
                status=PostDeploymentEvaluationStatus.PENDING,
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
            except StateRefPushUnacknowledgedError:
                raise
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
        except StateRefPushUnacknowledgedError:
            raise
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
    from foundry_opt.orchestration.handoff import CloudHandoffStore

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
        handoffs=CloudHandoffStore(),
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
