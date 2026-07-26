from typing import Protocol

from foundry_opt.onboarding.models import (
    DraftProbeResult,
    FoundryAgentDiscovery,
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
