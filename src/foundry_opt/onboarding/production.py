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
)
from foundry_opt.onboarding.runner import OnboardingDependencies
from foundry_opt.onboarding.repository import GhOnboardingPublisher


class DraftProbeUnavailable(RuntimeError):
    pass


class UnavailableSourceBundleDraftProbe:
    def probe(
        self,
        request: OnboardingRequest,
        agent: FoundryAgentDiscovery,
    ) -> DraftProbeResult:
        raise DraftProbeUnavailable(
            "The source-bundle draft API belongs to Milestone 3 and is not "
            "available in this build; onboarding cannot claim draft safety."
        )

    def delete_probe(self, agent_name: str, version: str) -> None:
        raise DraftProbeUnavailable("No draft probe was created.")


def build_production_onboarding_dependencies() -> OnboardingDependencies:
    commands = SubprocessCommandRunner()
    return OnboardingDependencies(
        discovery=LocalOnboardingDiscovery(
            commands,
            AzureSdkFoundryInventory(),
        ),
        oidc=CommandOidcVerifier(commands),
        draft_probe=UnavailableSourceBundleDraftProbe(),
        publisher=GhOnboardingPublisher(commands),
    )
