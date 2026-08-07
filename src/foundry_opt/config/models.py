from __future__ import annotations

import re
from datetime import date
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    HttpUrl,
    model_validator,
)

from foundry_opt.security import reject_secret_content


def _repository_path(value: Any) -> PurePosixPath:
    if not isinstance(value, (str, PurePosixPath)):
        raise ValueError("must be a repository-relative path")

    raw = str(value)
    windows_path = PureWindowsPath(raw)
    if windows_path.drive or raw.startswith(("\\\\", "//")):
        raise ValueError("must be a repository-relative path")

    raw = raw.replace("\\", "/")
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or ".." in path.parts:
        raise ValueError("must be a repository-relative path without '..'")
    return path


RepositoryPath = Annotated[PurePosixPath, BeforeValidator(_repository_path)]


def _repository_glob(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("must be a repository-relative glob")

    windows_path = PureWindowsPath(value)
    if (
        not value
        or windows_path.drive
        or value.startswith(("/", "\\"))
        or ".." in re.split(r"[\\/]", value)
    ):
        raise ValueError(
            "must be a repository-relative glob without a '..' path segment"
        )
    return value


RepositoryGlob = Annotated[str, BeforeValidator(_repository_glob)]


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class DeploymentTrigger(StrEnum):
    MANUAL = "manual"
    MERGE = "merge"


class AuthenticationMode(StrEnum):
    OIDC = "oidc"
    CLIENT_SECRET = "client_secret"


class DatasetMode(StrEnum):
    BATCH = "batch"
    SIMULATION = "simulation"


class MetricDirection(StrEnum):
    MAXIMIZE = "maximize"
    MINIMIZE = "minimize"


class UndefinedBehavior(StrEnum):
    FAIL = "fail"
    EXCLUDE = "exclude"


class RepeatCondition(StrEnum):
    BORDERLINE = "borderline"
    NOISY = "noisy"
    PARTIAL = "partial"


class MutationClass(StrEnum):
    SYSTEM_INSTRUCTIONS = "system_instructions"
    PYTHON_LOGIC = "python_logic"
    RETRIEVAL_ORCHESTRATION = "retrieval_orchestration"
    TESTS = "tests"
    PACKAGING = "packaging"
    MODEL = "model"
    SKILLS = "skills"
    TOOL_DESCRIPTIONS = "tool_descriptions"
    TOOL_CONTRACTS = "tool_contracts"


class IssueOverride(StrEnum):
    DEADLINE_MINUTES = "deadline_minutes"
    CANDIDATE_CUTOFF_MINUTES = "candidate_cutoff_minutes"
    MAX_CHANGED_CANDIDATES = "max_changed_candidates"
    TRANSIENT_RETRIES = "transient_retries"


class AutomationPolicy(ConfigModel):
    allowed_dataset_sources: set[str] = Field(
        default_factory=lambda: {"foundry", "repository"},
        min_length=1,
    )
    allowed_evaluator_sources: set[str] = Field(
        default_factory=lambda: {"foundry", "repository", "builtin"},
        min_length=1,
    )
    synthetic_max_rows: int = Field(default=100, ge=1, le=1000)
    trace_requires_human_review: Literal[True] = True
    allow_spec_auto_approval: bool = False
    allow_candidate_auto_selection: bool = False
    allow_merge: bool = False
    allow_deployment: bool = False
    merge_actor: str | None = None
    required_checks: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_automation_order(self) -> AutomationPolicy:
        for source in (
            *self.allowed_dataset_sources,
            *self.allowed_evaluator_sources,
        ):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", source):
                raise ValueError("automation asset sources must be identifiers")
        if self.allow_merge and not self.allow_candidate_auto_selection:
            raise ValueError("merge requires candidate auto selection")
        if self.allow_merge and not self.merge_actor:
            raise ValueError("merge_actor is required when merge is enabled")
        if self.allow_merge and not self.required_checks:
            raise ValueError(
                "required_checks are required when merge is enabled"
            )
        if self.merge_actor is not None and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
            self.merge_actor,
        ):
            raise ValueError("merge_actor must be an identifier")
        if len(self.required_checks) != len(set(self.required_checks)):
            raise ValueError("required_checks must be unique")
        if any(not check.strip() for check in self.required_checks):
            raise ValueError("required_checks must not contain empty values")
        return self


class DeploymentWorkflow(ConfigModel):
    path: RepositoryPath
    trigger: DeploymentTrigger


class PricingFallback(ConfigModel):
    model: str = Field(min_length=1)
    input_usd_per_million_tokens: float = Field(ge=0)
    output_usd_per_million_tokens: float = Field(ge=0)
    effective_date: date
    source: str = Field(min_length=1)


class EnvironmentProfile(ConfigModel):
    authentication: AuthenticationMode = AuthenticationMode.OIDC
    project_endpoint: HttpUrl
    project_resource_id: str = Field(min_length=1)
    application_insights_workspace_resource_id: str | None = None
    allowed_models: list[str] = Field(min_length=1)
    deployment_workflow: DeploymentWorkflow
    pricing_fallbacks: list[PricingFallback] = Field(default_factory=list)


class PackageRules(ConfigModel):
    include: list[RepositoryGlob] = Field(min_length=1)
    exclude: list[RepositoryGlob] = Field(default_factory=list)


class DatasetReference(ConfigModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    mode: DatasetMode


class DatasetSelection(ConfigModel):
    development: list[DatasetReference] = Field(min_length=1)
    validation: list[DatasetReference] = Field(min_length=1)


class EvaluatorDefinition(ConfigModel):
    name: str = Field(min_length=1)
    reference: str = Field(min_length=1)
    metrics: list[str] = Field(min_length=1)


class RepeatPolicy(ConfigModel):
    max_repeats: int = Field(default=1, ge=0, le=1)
    conditions: set[RepeatCondition] = Field(default_factory=set)


class MetricPolicy(ConfigModel):
    direction: MetricDirection
    threshold: float
    materiality: float = Field(gt=0)
    hard_guardrail: bool
    undefined_behavior: UndefinedBehavior
    repeat: RepeatPolicy | None = None


class RestrictedOptIns(ConfigModel):
    tool_contract_schema_changes: bool = False
    external_services: bool = False
    infrastructure: bool = False
    permission_expansion: bool = False
    paid_dependencies: bool = False
    model_deployment: bool = False


class CampaignTiming(ConfigModel):
    deadline_minutes: int = Field(ge=1, le=240)
    candidate_cutoff_minutes: int = Field(ge=1, le=180)
    max_changed_candidates: int = Field(ge=1, le=3)
    transient_retries: int = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_cutoff_precedes_deadline(self) -> CampaignTiming:
        if self.candidate_cutoff_minutes >= self.deadline_minutes:
            raise ValueError("candidate_cutoff_minutes must be less than deadline_minutes")
        return self


class CampaignOverrides(ConfigModel):
    deadline_minutes: int | None = Field(default=None, ge=1, le=240)
    candidate_cutoff_minutes: int | None = Field(default=None, ge=1, le=180)
    max_changed_candidates: int | None = Field(default=None, ge=1, le=3)
    transient_retries: int | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_optional_cutoff(self) -> CampaignOverrides:
        if (
            self.deadline_minutes is not None
            and self.candidate_cutoff_minutes is not None
            and self.candidate_cutoff_minutes >= self.deadline_minutes
        ):
            raise ValueError("candidate_cutoff_minutes must be less than deadline_minutes")
        return self


class AgentRuntime(ConfigModel):
    """The hosted-agent runtime contract for a campaign draft.

    A campaign candidate is a source-only mutation of the *same* hosted agent,
    so by default every field is ``None`` — meaning *inherit the published
    baseline version's contract* rather than a guessed value that would
    silently overwrite it (e.g. forcing ``python_3_12`` onto a ``python_3_13``
    baseline). A target overrides an individual field only by configuring it
    explicitly; the production draft creator forwards these inherit-or-override
    values into the ``DraftRequest``, and the draft gateway leaves the
    baseline's runtime/dependency and hosted CPU/memory/protocol untouched for
    every field left as ``None``.
    """

    runtime: str | None = Field(default=None, min_length=1)
    dependency_resolution: Literal["remote_build", "bundled"] | None = None
    cpu: str | None = Field(default=None, min_length=1)
    memory: str | None = Field(default=None, min_length=1)
    protocol: str | None = Field(default=None, min_length=1)
    protocol_version: str | None = Field(default=None, min_length=1)


class AgentTarget(ConfigModel):
    environment: str = Field(min_length=1)
    source_paths: list[RepositoryPath] = Field(min_length=1)
    edit_paths: list[RepositoryPath] = Field(min_length=1)
    entry_point: RepositoryPath
    base_agent_version: str = Field(min_length=1)
    package: PackageRules
    datasets: DatasetSelection
    evaluators: list[EvaluatorDefinition] = Field(min_length=1)
    validation_commands: list[str] = Field(min_length=1)
    metrics: dict[str, MetricPolicy] = Field(min_length=1)
    allowed_mutations: set[MutationClass] = Field(min_length=1)
    restricted_opt_ins: RestrictedOptIns = Field(default_factory=RestrictedOptIns)
    runtime: AgentRuntime = Field(default_factory=AgentRuntime)
    campaign_overrides: CampaignOverrides | None = None

    @model_validator(mode="after")
    def validate_target(self) -> AgentTarget:
        if (
            not self.base_agent_version.isdecimal()
            or int(self.base_agent_version) < 1
        ):
            raise ValueError(
                "base_agent_version must be a positive published version"
            )

        if (
            MutationClass.TOOL_CONTRACTS in self.allowed_mutations
            and not self.restricted_opt_ins.tool_contract_schema_changes
        ):
            raise ValueError(
                "tool_contracts mutations require "
                "restricted_opt_ins.tool_contract_schema_changes=true"
            )

        missing_metrics = {
            metric
            for evaluator in self.evaluators
            for metric in evaluator.metrics
            if metric not in self.metrics
        }
        if missing_metrics:
            missing = ", ".join(sorted(missing_metrics))
            raise ValueError(f"evaluator metrics are not configured: {missing}")
        return self


class CampaignDefaults(CampaignTiming):
    stale_after_hours: int = Field(default=2, ge=2)
    evidence_path: RepositoryPath = PurePosixPath(".foundry-optimizer/campaigns")
    allowed_issue_overrides: set[IssueOverride] = Field(default_factory=set)
    allowed_mutations: set[MutationClass] = Field(min_length=1)


class OptimizerConfig(ConfigModel):
    schema_version: Literal["1"]
    default_environment: str = Field(min_length=1)
    environments: dict[str, EnvironmentProfile] = Field(min_length=1)
    targets: dict[str, AgentTarget] = Field(min_length=1)
    campaign: CampaignDefaults
    automation_policy: AutomationPolicy = Field(
        default_factory=AutomationPolicy
    )

    @model_validator(mode="before")
    @classmethod
    def reject_secrets(cls, value: Any) -> Any:
        return reject_secret_content(value)

    @model_validator(mode="after")
    def validate_references(self) -> OptimizerConfig:
        if self.default_environment not in self.environments:
            raise ValueError("default_environment must name a configured environment")

        unknown_environments = {
            target.environment
            for target in self.targets.values()
            if target.environment not in self.environments
        }
        if unknown_environments:
            names = ", ".join(sorted(unknown_environments))
            raise ValueError(f"targets reference unknown environments: {names}")

        for name, target in self.targets.items():
            if target.campaign_overrides is not None:
                deadline = (
                    target.campaign_overrides.deadline_minutes
                    if target.campaign_overrides.deadline_minutes is not None
                    else self.campaign.deadline_minutes
                )
                cutoff = (
                    target.campaign_overrides.candidate_cutoff_minutes
                    if target.campaign_overrides.candidate_cutoff_minutes is not None
                    else self.campaign.candidate_cutoff_minutes
                )
                if cutoff >= deadline:
                    raise ValueError(
                        f"target {name!r} effective campaign timing: "
                        "candidate_cutoff_minutes must be less than deadline_minutes"
                    )

            unsupported = target.allowed_mutations - self.campaign.allowed_mutations
            if unsupported:
                values = ", ".join(sorted(value.value for value in unsupported))
                raise ValueError(
                    f"target {name!r} allows mutations disabled by the campaign: {values}"
                )
        return self
