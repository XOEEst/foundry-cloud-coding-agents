from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from foundry_opt.deployment import DEPLOYMENT_OIDC_CLIENT_ID


class OnboardingStatus(StrEnum):
    READY = "ready"
    BLOCKED = "blocked"
    CONFLICT = "conflict"
    PARTIAL = "partial"


class ChangeStatus(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    REMOVED = "removed"
    PLANNED = "planned"
    CONFLICT = "conflict"


class GitHubVariableScope(StrEnum):
    AGENTS = "agents"
    ACTIONS_ENVIRONMENT = "actions_environment"


class GitHubVariableChangeStatus(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


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
    role: str | None = None


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
    needs_input: str | None = None
    role: str | None = None


@dataclass(frozen=True)
class AppInsightsDiscovery:
    connected: bool
    workspace_resource_id: str | None = None


@dataclass(frozen=True)
class DeploymentWorkflowDiscovery:
    path: Path
    trigger: str
    role: str | None = None
    name: str | None = None
    deployment_identity_verified: bool | None = None
    trigger_contract_verified: bool | None = None


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
    deployment_client_id: str = DEPLOYMENT_OIDC_CLIENT_ID
    set_github_variables: bool = False
    mirror_actions_environment: str | None = None
    update_github_variables: bool = False

    def __post_init__(self) -> None:
        if (
            self.mirror_actions_environment is not None
            or self.update_github_variables
        ) and not self.set_github_variables:
            raise ValueError(
                "GitHub variable mirror/update options require "
                "set_github_variables"
            )
        if self.mirror_actions_environment is not None:
            environment = self.mirror_actions_environment
            if (
                not environment.strip()
                or any(ord(character) < 32 for character in environment)
                or "/" in environment
                or "\\" in environment
            ):
                raise ValueError(
                    "mirror_actions_environment is not a safe environment name"
                )


@dataclass(frozen=True)
class OnboardingChange:
    path: Path
    content: str
    status: ChangeStatus
    detail: str | None = None
    base_commit: str | None = None
    commit_sha: str | None = None


@dataclass(frozen=True)
class DraftPullRequest:
    title: str
    body: str


@dataclass(frozen=True)
class DraftPullRequestPublication:
    url: str
    branch: str
    commit_sha: str


@dataclass(frozen=True)
class GitHubVariableChange:
    name: str
    scope: GitHubVariableScope
    status: GitHubVariableChangeStatus
    environment: str | None = None
    value: None = None


@dataclass(frozen=True)
class OnboardingResult:
    status: OnboardingStatus
    changes: tuple[OnboardingChange, ...]
    draft_pull_request: DraftPullRequest
    discovery: RepositoryDiscovery | None = None
    oidc: OidcTrustResult | None = None
    draft_probe: DraftProbeResult | None = None
    published_pull_request: DraftPullRequestPublication | None = None
    variable_changes: tuple[GitHubVariableChange, ...] = ()
    blockers: tuple[str, ...] = ()
    residual_state: tuple[str, ...] = ()
    guidance: tuple[str, ...] = field(default_factory=tuple)

    @property
    def exit_code(self) -> int:
        return 0 if self.status is OnboardingStatus.READY else 1
