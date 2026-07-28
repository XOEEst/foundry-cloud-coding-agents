from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, StrEnum
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    model_validator,
)

from foundry_opt.config.models import (
    AutomationPolicy,
    MetricPolicy,
    MutationClass,
    RestrictedOptIns,
)
from foundry_opt.security import reject_secret_content


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/"
    r"[A-Za-z0-9._-][A-Za-z0-9._-]{0,99}$"
)
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AssetKind(StrEnum):
    DATASET = "dataset"
    EVALUATOR = "evaluator"


class ApprovalGate(StrEnum):
    POLICY = "policy"
    HUMAN = "human"


class DecisionMode(StrEnum):
    HUMAN = "human"
    AUTOPILOT_IF_ALLOWED = "autopilot_if_allowed"


class DeploymentMode(StrEnum):
    HUMAN = "human"
    AFTER_MERGE_IF_ALLOWED = "after_merge_if_allowed"


def _identifier(value: str, field: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} must be an identifier")


def _repository_path(value: Path | None) -> Path | None:
    if value is None:
        return None
    raw = str(value)
    windows = PureWindowsPath(raw)
    posix = PurePosixPath(raw.replace("\\", "/"))
    if (
        not raw
        or windows.drive
        or raw.startswith(("/", "\\"))
        or ".." in posix.parts
    ):
        raise ValueError("path must be repository-relative")
    return Path(posix.as_posix())


class EvaluationAssetRequest(StrictContract):
    asset_id: str
    kind: AssetKind
    source: str
    role: str | None = None
    name: str | None = None
    version: str | None = None
    path: Path | None = None
    metrics: tuple[str, ...] = ()
    parameters: dict[str, Any] = Field(default_factory=dict)
    approval_gate: ApprovalGate = ApprovalGate.POLICY

    @model_validator(mode="before")
    @classmethod
    def reject_secrets(cls, value: Any) -> Any:
        return reject_secret_content(value)

    @model_validator(mode="after")
    def validate_asset(self) -> EvaluationAssetRequest:
        _identifier(self.asset_id, "asset_id")
        _identifier(self.source, "source")
        object.__setattr__(self, "path", _repository_path(self.path))
        if self.kind is AssetKind.DATASET:
            if self.role not in {"development", "validation"}:
                raise ValueError(
                    "dataset role must be development or validation"
                )
            if self.metrics:
                raise ValueError("dataset requests must not define metrics")
        else:
            if self.role is not None:
                raise ValueError("evaluator requests must not define a role")
            if not self.metrics:
                raise ValueError("evaluator requests require metrics")
            if len(set(self.metrics)) != len(self.metrics):
                raise ValueError("evaluator metrics must be unique")
            for metric in self.metrics:
                _identifier(metric, "metric")
        if self.source in {"foundry", "builtin", "custom"} and (
            not self.name or not self.version
        ):
            raise ValueError(
                f"{self.source} assets require name and version"
            )
        if self.source == "repository" and self.path is None:
            raise ValueError("repository assets require a path")
        if self.source == "synthetic":
            if self.kind is not AssetKind.DATASET:
                raise ValueError("synthetic source is only valid for datasets")
            row_count = self.parameters.get("row_count")
            if (
                not isinstance(row_count, int)
                or isinstance(row_count, bool)
                or row_count < 1
            ):
                raise ValueError(
                    "synthetic datasets require a positive row_count"
                )
        if self.source == "trace":
            if self.kind is not AssetKind.DATASET:
                raise ValueError("trace source is only valid for datasets")
            if self.approval_gate is not ApprovalGate.HUMAN:
                raise ValueError(
                    "trace-derived assets require human approval"
                )
        return self


class OptimizationIssueRequest(StrictContract):
    issue_number: int = Field(ge=1)
    repository: str
    target: str
    goal: str = Field(min_length=20, max_length=4000)
    datasets: tuple[EvaluationAssetRequest, ...] = Field(min_length=2)
    evaluators: tuple[EvaluationAssetRequest, ...] = Field(min_length=1)
    metrics: dict[str, MetricPolicy] = Field(min_length=1)
    allowed_mutations: frozenset[MutationClass] = Field(min_length=1)
    restricted_opt_ins: RestrictedOptIns = Field(
        default_factory=RestrictedOptIns
    )
    decision_mode: DecisionMode = DecisionMode.HUMAN
    deployment_mode: DeploymentMode = DeploymentMode.HUMAN

    @model_validator(mode="before")
    @classmethod
    def reject_secrets(cls, value: Any) -> Any:
        return reject_secret_content(value)

    @model_validator(mode="after")
    def validate_request(self) -> OptimizationIssueRequest:
        if not _REPOSITORY.fullmatch(self.repository):
            raise ValueError("repository must use OWNER/REPOSITORY format")
        _identifier(self.target, "target")
        assets = (*self.datasets, *self.evaluators)
        asset_ids = tuple(asset.asset_id for asset in assets)
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("asset IDs must be unique")
        if any(
            asset.kind is not AssetKind.DATASET
            for asset in self.datasets
        ):
            raise ValueError("datasets must contain only dataset assets")
        if any(
            asset.kind is not AssetKind.EVALUATOR
            for asset in self.evaluators
        ):
            raise ValueError("evaluators must contain only evaluator assets")
        roles = {asset.role for asset in self.datasets}
        if not {"development", "validation"}.issubset(roles):
            raise ValueError(
                "datasets require development and validation roles"
            )
        referenced_metrics = {
            metric
            for evaluator in self.evaluators
            for metric in evaluator.metrics
        }
        if referenced_metrics - self.metrics.keys():
            raise ValueError("evaluator metrics are not configured")
        return self


class AssetProvenance(StrictContract):
    asset_id: str
    kind: AssetKind
    source: str
    role: str | None = None
    name: str | None = None
    version: str | None = None
    content_sha256: str | None = None
    created_by: str
    approval_gate: ApprovalGate = ApprovalGate.POLICY
    remote_id: str | None = None
    metrics: tuple[str, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def reject_secrets(cls, value: Any) -> Any:
        return reject_secret_content(value)

    @model_validator(mode="after")
    def validate_provenance(self) -> AssetProvenance:
        _identifier(self.asset_id, "asset_id")
        _identifier(self.source, "source")
        _identifier(self.created_by, "created_by")
        if self.kind is AssetKind.DATASET and self.role not in {
            "development",
            "validation",
        }:
            raise ValueError("dataset provenance requires a dataset role")
        if self.kind is AssetKind.EVALUATOR and self.role is not None:
            raise ValueError("evaluator provenance must not define a role")
        if self.kind is AssetKind.DATASET:
            if self.metrics:
                raise ValueError("dataset provenance must not define metrics")
        elif not self.metrics:
            raise ValueError(
                "evaluator provenance requires at least one metric"
            )
        elif len(set(self.metrics)) != len(self.metrics):
            raise ValueError("evaluator provenance metrics must be unique")
        else:
            for metric in self.metrics:
                _identifier(metric, "metric")
        if self.content_sha256 is not None and not _SHA256.fullmatch(
            self.content_sha256
        ):
            raise ValueError("content_sha256 must be a SHA-256 digest")
        if self.source == "trace" and self.approval_gate is not ApprovalGate.HUMAN:
            raise ValueError("trace-derived assets require human approval")
        return self


class OptimizationSpec(StrictContract):
    schema_version: str = "1"
    issue_number: int = Field(ge=1)
    repository: str
    base_commit: str
    target: str
    environment: str
    base_agent_version: str
    goal: str = Field(min_length=20, max_length=4000)
    datasets: tuple[AssetProvenance, ...] = Field(min_length=2)
    evaluators: tuple[AssetProvenance, ...] = Field(min_length=1)
    metrics: dict[str, MetricPolicy] = Field(min_length=1)
    allowed_mutations: frozenset[MutationClass] = Field(min_length=1)
    restricted_opt_ins: RestrictedOptIns = Field(
        default_factory=RestrictedOptIns
    )
    decision_mode: DecisionMode = DecisionMode.HUMAN
    deployment_mode: DeploymentMode = DeploymentMode.HUMAN

    @model_validator(mode="before")
    @classmethod
    def reject_secrets(cls, value: Any) -> Any:
        return reject_secret_content(value)

    @model_validator(mode="after")
    def validate_spec(self) -> OptimizationSpec:
        if self.schema_version != "1":
            raise ValueError("schema_version must be '1'")
        if not _REPOSITORY.fullmatch(self.repository):
            raise ValueError("repository must use OWNER/REPOSITORY format")
        if not _COMMIT.fullmatch(self.base_commit):
            raise ValueError("base_commit must be a full Git commit")
        _identifier(self.target, "target")
        _identifier(self.environment, "environment")
        if (
            not self.base_agent_version.isdecimal()
            or int(self.base_agent_version) < 1
        ):
            raise ValueError(
                "base_agent_version must be a positive published version"
            )
        assets = (*self.datasets, *self.evaluators)
        asset_ids = tuple(asset.asset_id for asset in assets)
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("asset IDs must be unique")
        if any(
            asset.kind is not AssetKind.DATASET for asset in self.datasets
        ):
            raise ValueError("datasets must contain only dataset provenance")
        if any(
            asset.kind is not AssetKind.EVALUATOR
            for asset in self.evaluators
        ):
            raise ValueError(
                "evaluators must contain only evaluator provenance"
            )
        return self

    @property
    def canonical_json(self) -> str:
        return json.dumps(
            _canonical_value(self.model_dump(mode="python")),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (set, frozenset)):
        normalized = (_canonical_value(item) for item in value)
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ),
        )
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if isinstance(value, Path):
        return value.as_posix()
    return value


class OptimizationSpecApproval(StrictContract):
    spec: OptimizationSpec
    spec_sha256: str
    approval_commit: str
    approval_gate: ApprovalGate = ApprovalGate.HUMAN

    @model_validator(mode="after")
    def validate_approval(self) -> OptimizationSpecApproval:
        if self.spec_sha256 != self.spec.sha256:
            raise ValueError("spec hash does not match")
        if not _COMMIT.fullmatch(self.approval_commit):
            raise ValueError("approval_commit must be a full Git commit")
        return self


def approve_optimization_spec(
    spec: OptimizationSpec,
    *,
    approval_commit: str,
    approval_gate: ApprovalGate = ApprovalGate.HUMAN,
) -> OptimizationSpecApproval:
    return OptimizationSpecApproval(
        spec=spec,
        spec_sha256=spec.sha256,
        approval_commit=approval_commit,
        approval_gate=approval_gate,
    )


def spec_is_autopilot_eligible(
    spec: OptimizationSpec,
    policy: AutomationPolicy,
) -> bool:
    if not policy.allow_spec_auto_approval:
        return False
    for asset in (*spec.datasets, *spec.evaluators):
        allowed_sources = (
            policy.allowed_dataset_sources
            if asset.kind is AssetKind.DATASET
            else policy.allowed_evaluator_sources
        )
        if (
            asset.source not in allowed_sources
            or asset.approval_gate is not ApprovalGate.POLICY
            or asset.source == "trace"
        ):
            return False
    return True


@dataclass(frozen=True)
class EvaluationAssetContext:
    repository_root: Path
    project_endpoint: str
    target: str
    issue_number: int

    def __post_init__(self) -> None:
        if not self.repository_root:
            raise ValueError("repository_root is required")
        HttpUrl(self.project_endpoint)
        _identifier(self.target, "target")
        if self.issue_number < 1:
            raise ValueError("issue_number must be positive")


@dataclass(frozen=True)
class PreparedEvaluationAsset:
    provenance: AssetProvenance
    files: Mapping[Path, bytes]

    def __post_init__(self) -> None:
        normalized: dict[Path, bytes] = {}
        for path, content in self.files.items():
            safe = _repository_path(path)
            if safe is None:
                raise ValueError("asset file path is required")
            if not isinstance(content, bytes):
                raise ValueError("asset file content must be bytes")
            normalized[safe] = content
        object.__setattr__(self, "files", MappingProxyType(normalized))


class EvaluationAssetProvider(Protocol):
    source_type: str

    def prepare(
        self,
        request: EvaluationAssetRequest,
        context: EvaluationAssetContext,
    ) -> PreparedEvaluationAsset: ...
