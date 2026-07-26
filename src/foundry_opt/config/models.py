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
    deadline_minutes: int = Field(ge=1, le=50)
    candidate_cutoff_minutes: int = Field(ge=1, le=40)
    max_changed_candidates: int = Field(ge=1, le=3)
    transient_retries: int = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_cutoff_precedes_deadline(self) -> CampaignTiming:
        if self.candidate_cutoff_minutes >= self.deadline_minutes:
            raise ValueError("candidate_cutoff_minutes must be less than deadline_minutes")
        return self


class CampaignOverrides(ConfigModel):
    deadline_minutes: int | None = Field(default=None, ge=1, le=50)
    candidate_cutoff_minutes: int | None = Field(default=None, ge=1, le=40)
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

    @model_validator(mode="before")
    @classmethod
    def reject_secrets(cls, value: Any) -> Any:
        secret_keys = {
            "access_key",
            "api_key",
            "client_certificate",
            "client_secret",
            "connection_string",
            "credential",
            "password",
            "private_key",
            "secret",
            "secret_value",
            "shared_key",
            "signing_key",
            "token",
        }
        plural_secret_keys = {
            "access_keys",
            "access_tokens",
            "api_keys",
            "client_certificates",
            "client_secrets",
            "connection_strings",
            "credentials",
            "passwords",
            "private_keys",
            "secrets",
            "shared_keys",
            "signing_keys",
        }
        secret_value_markers = (
            "accountkey=",
            "github_pat_",
            "ghp_",
            "-----begin private key-----",
        )

        def visit(node: Any, path: tuple[str | int, ...] = ()) -> None:
            if isinstance(node, dict):
                for key, child in node.items():
                    snake_key = re.sub(
                        r"([a-z0-9])([A-Z])",
                        r"\1_\2",
                        str(key).replace("-", "_"),
                    )
                    normalized = snake_key.casefold()
                    has_secret_name = (
                        normalized in secret_keys
                        or normalized in plural_secret_keys
                        or any(
                            normalized.endswith(f"_{suffix}")
                            for suffix in secret_keys | plural_secret_keys
                        )
                    )
                    if has_secret_name:
                        location = ".".join(map(str, (*path, key)))
                        raise ValueError(
                            f"configuration must not contain secrets ({location})"
                        )
                    visit(child, (*path, key))
            elif isinstance(node, list):
                for index, child in enumerate(node):
                    visit(child, (*path, index))
            elif isinstance(node, str):
                lowered = node.casefold()
                if any(marker in lowered for marker in secret_value_markers):
                    location = ".".join(map(str, path))
                    raise ValueError(
                        f"configuration must not contain secrets ({location})"
                    )

        visit(value)
        return value

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
