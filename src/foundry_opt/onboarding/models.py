from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class OnboardingStatus(StrEnum):
    READY = "ready"
    BLOCKED = "blocked"
    CONFLICT = "conflict"


class ChangeStatus(StrEnum):
    CREATED = "created"
    PLANNED = "planned"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class PythonAgentCandidate:
    name: str
    source_path: Path
    entry_point: Path


@dataclass(frozen=True)
class FoundryAgentDiscovery:
    name: str
    versions: tuple[str, ...]


@dataclass(frozen=True)
class DeployedModelDiscovery:
    name: str


@dataclass(frozen=True)
class DatasetDiscovery:
    name: str
    versions: tuple[str, ...]


@dataclass(frozen=True)
class MetricDiscovery:
    name: str
    direction: str
    threshold: float
    materiality: float
    hard_guardrail: bool


@dataclass(frozen=True)
class EvaluatorDiscovery:
    name: str
    reference: str
    metrics: tuple[MetricDiscovery, ...] = ()


@dataclass(frozen=True)
class AppInsightsDiscovery:
    connected: bool
    workspace_resource_id: str | None = None


@dataclass(frozen=True)
class DeploymentWorkflowDiscovery:
    path: Path
    trigger: str


@dataclass(frozen=True)
class RepositoryDiscovery:
    repository: str
    repository_id: str
    default_branch: str
    current_branch: str
    authenticated_login: str
    viewer_permission: str
    clean: bool
    python_agents: tuple[PythonAgentCandidate, ...] = ()
    validation_commands: tuple[str, ...] = ()
    foundry_agents: tuple[FoundryAgentDiscovery, ...] = ()
    deployed_models: tuple[DeployedModelDiscovery, ...] = ()
    datasets: tuple[DatasetDiscovery, ...] = ()
    evaluators: tuple[EvaluatorDiscovery, ...] = ()
    app_insights: AppInsightsDiscovery = AppInsightsDiscovery(False)
    deployment_workflows: tuple[DeploymentWorkflowDiscovery, ...] = ()


@dataclass(frozen=True)
class OidcTrustResult:
    subject: str
    repository_id: str
    verified: bool
    detail: str | None = None


@dataclass(frozen=True)
class DraftProbeResult:
    agent_name: str
    version: str


@dataclass(frozen=True)
class OnboardingRequest:
    repository_root: Path
    environment_name: str
    target_name: str
    project_endpoint: str
    project_resource_id: str
    tenant_id: str
    client_id: str
    subscription_id: str
    product_install: str


@dataclass(frozen=True)
class OnboardingChange:
    path: Path
    content: str
    status: ChangeStatus
    detail: str | None = None


@dataclass(frozen=True)
class DraftPullRequest:
    title: str
    body: str


@dataclass(frozen=True)
class OnboardingResult:
    status: OnboardingStatus
    changes: tuple[OnboardingChange, ...]
    draft_pull_request: DraftPullRequest
    discovery: RepositoryDiscovery | None = None
    oidc: OidcTrustResult | None = None
    draft_probe: DraftProbeResult | None = None
    blockers: tuple[str, ...] = ()
    guidance: tuple[str, ...] = field(default_factory=tuple)

    @property
    def exit_code(self) -> int:
        return 0 if self.status is OnboardingStatus.READY else 1
