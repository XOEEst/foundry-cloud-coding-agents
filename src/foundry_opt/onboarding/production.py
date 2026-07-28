from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from foundry_opt.adapters.commands import SubprocessCommandRunner
from foundry_opt.adapters.discovery import (
    AzureSdkFoundryInventory,
    LocalOnboardingDiscovery,
)
from foundry_opt.adapters.oidc import CommandOidcVerifier
from foundry_opt.onboarding.models import (
    DraftProbeResult,
    FoundryAgentDiscovery,
    OnboardingRequest,
    PythonAgentCandidate,
)
from foundry_opt.onboarding.repository import (
    GhOnboardingPublisher,
    GitChangeSetWriter,
)
from foundry_opt.onboarding.variables import (
    GitHubApiVariableGateway,
    GitHubVariableConfigurator,
)
from foundry_opt.onboarding.runner import OnboardingDependencies


class DraftProbeUnavailable(RuntimeError):
    pass


class UnavailableSourceBundleDraftProbe:
    def probe(
        self,
        request: OnboardingRequest,
        agent: FoundryAgentDiscovery,
        source: PythonAgentCandidate,
    ) -> DraftProbeResult:
        raise DraftProbeUnavailable(
            "The Milestone 3 source-bundle DraftGateway is not available in "
            "this build; onboarding cannot claim draft safety."
        )

    def delete_probe(self, agent_name: str, version: str) -> None:
        raise DraftProbeUnavailable("No draft probe was created.")


@dataclass(frozen=True)
class DraftIntegrationComponents:
    gateway: Any | None
    build_bundle: Callable[[Any], Any]
    bundle_request_type: Callable[..., Any]
    draft_request_type: Callable[..., Any]
    scratch_path_factory: Callable[[OnboardingRequest], Path] | None = None
    gateway_factory: Callable[[OnboardingRequest], Any] | None = None


class SourceBundleDraftProbe:
    def __init__(self, components: DraftIntegrationComponents) -> None:
        self._components = components
        self._records: dict[tuple[str, str], tuple[Any, Any]] = {}

    def probe(
        self,
        request: OnboardingRequest,
        agent: FoundryAgentDiscovery,
        source: PythonAgentCandidate,
    ) -> DraftProbeResult:
        published = tuple(
            int(version)
            for version in agent.versions
            if version.isdecimal()
        )
        if not published:
            raise DraftProbeUnavailable(
                "The target has no published numeric base version."
            )
        output_path = self._scratch_path(request)
        artifact = None
        record = None
        gateway = (
            self._components.gateway_factory(request)
            if self._components.gateway_factory is not None
            else self._components.gateway
        )
        if gateway is None:
            raise DraftProbeUnavailable(
                "The Milestone 3 DraftGateway factory returned no gateway."
            )
        try:
            bundle_request = self._components.bundle_request_type(
                repository_root=request.repository_root,
                output_path=output_path,
                include=(f"{source.source_path.as_posix()}/**",),
            )
            artifact = self._components.build_bundle(bundle_request)
            draft_request = self._components.draft_request_type(
                project_endpoint=request.project_endpoint,
                agent_name=agent.name,
                base_version=max(published),
                bundle=artifact,
                entry_point=("python", source.entry_point.as_posix()),
                probe=True,
            )
            record = gateway.create_draft(draft_request)
            version = getattr(record, "version_id", "")
            record_agent = getattr(record, "agent_name", "")
            if record_agent != agent.name or not str(version).startswith("draft-"):
                raise DraftProbeUnavailable(
                    "Milestone 3 did not return a confirmed draft-* probe."
                )
        except Exception:
            if record is not None:
                gateway.delete_probe(record)
            _cleanup_bundle_artifact(artifact, output_path)
            raise
        try:
            _cleanup_bundle_artifact(artifact, output_path)
        except Exception:
            gateway.delete_probe(record)
            raise
        result = DraftProbeResult(
            agent_name=record_agent,
            version=str(version),
        )
        self._records[(result.agent_name, result.version)] = (gateway, record)
        return result

    def delete_probe(self, agent_name: str, version: str) -> None:
        key = (agent_name, version)
        registered = self._records.get(key)
        if registered is None:
            raise DraftProbeUnavailable(
                "Only a probe created by this onboarding invocation may be "
                "deleted."
            )
        gateway, record = registered
        gateway.delete_probe(record)
        self._records.pop(key, None)

    def _scratch_path(self, request: OnboardingRequest) -> Path:
        factory = self._components.scratch_path_factory
        if factory is not None:
            return factory(request)
        commands = SubprocessCommandRunner()
        value = commands.run(
            (
                "git",
                "rev-parse",
                "--git-path",
                f"foundry-opt-probe-{uuid4().hex}.zip",
            ),
            cwd=request.repository_root,
        ).stdout.strip()
        path = Path(value)
        return path if path.is_absolute() else request.repository_root / path


def build_source_bundle_draft_probe(
    components: DraftIntegrationComponents | None = None,
):
    if components is not None:
        return SourceBundleDraftProbe(components)
    try:
        from foundry_opt.adapters.drafts import (
            DraftGateway as AzureDraftGateway,
        )
        from foundry_opt.adapters.foundry import AzureCliCredentialProvider
        from foundry_opt.drafts import DraftRequest
        from foundry_opt.packaging import BundleRequest, build_source_bundle
    except ImportError:
        return UnavailableSourceBundleDraftProbe()
    return SourceBundleDraftProbe(
        DraftIntegrationComponents(
            gateway=None,
            build_bundle=build_source_bundle,
            bundle_request_type=BundleRequest,
            draft_request_type=DraftRequest,
            gateway_factory=lambda request: AzureDraftGateway(
                AzureCliCredentialProvider(
                    _OnboardingEnvironmentReader(request)
                )
            ),
        )
    )


def build_production_onboarding_dependencies(
    *,
    draft_probe_factory: Callable[[], Any] = build_source_bundle_draft_probe,
) -> OnboardingDependencies:
    commands = SubprocessCommandRunner()
    return OnboardingDependencies(
        discovery=LocalOnboardingDiscovery(
            commands,
            AzureSdkFoundryInventory(),
        ),
        oidc=CommandOidcVerifier(commands),
        draft_probe=draft_probe_factory(),
        publisher=GhOnboardingPublisher(commands),
        change_writer=GitChangeSetWriter(commands),
        variables=GitHubVariableConfigurator(
            GitHubApiVariableGateway(commands)
        ),
    )


def _cleanup_bundle_artifact(artifact: Any | None, output_path: Path) -> None:
    paths = [output_path]
    manifest = getattr(artifact, "manifest_path", None)
    if isinstance(manifest, Path):
        paths.append(manifest)
    artifact_path = getattr(artifact, "path", None)
    if isinstance(artifact_path, Path):
        paths.append(artifact_path)
    errors: list[str] = []
    for path in dict.fromkeys(paths):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as error:
            errors.append(f"{path}: {error}")
    if errors:
        raise DraftProbeUnavailable(
            "Draft probe bundle cleanup failed: " + "; ".join(errors)
        )


class _OnboardingEnvironmentReader:
    def __init__(self, request: OnboardingRequest) -> None:
        self._request = request

    def get(self, name: str) -> str | None:
        if name == "AZURE_TENANT_ID":
            return self._request.tenant_id
        return None
