from pathlib import Path
from typing import Mapping, Protocol

from foundry_opt.onboarding.models import (
    DraftPullRequest,
    DraftPullRequestPublication,
    DraftProbeResult,
    FoundryAgentDiscovery,
    OnboardingChange,
    OidcTrustResult,
    OnboardingRequest,
    RepositoryDiscovery,
)


class DiscoveryGateway(Protocol):
    def discover(self, request: OnboardingRequest) -> RepositoryDiscovery: ...


class OidcVerifier(Protocol):
    def verify(
        self,
        request: OnboardingRequest,
        discovery: RepositoryDiscovery,
    ) -> OidcTrustResult: ...


class DraftProbe(Protocol):
    def probe(
        self,
        request: OnboardingRequest,
        agent: FoundryAgentDiscovery,
    ) -> DraftProbeResult: ...

    def delete_probe(self, agent_name: str, version: str) -> None: ...


class ChangeSetWriter(Protocol):
    def prevalidate(
        self,
        repository_root: Path,
        contents: Mapping[Path, str],
    ) -> tuple[OnboardingChange, ...]: ...

    def write(
        self,
        repository_root: Path,
        contents: Mapping[Path, str],
    ) -> tuple[OnboardingChange, ...]: ...


class OnboardingPublisher(Protocol):
    def publish(
        self,
        request: OnboardingRequest,
        discovery: RepositoryDiscovery,
        changes: tuple[OnboardingChange, ...],
        draft_pull_request: DraftPullRequest,
    ) -> DraftPullRequestPublication: ...
