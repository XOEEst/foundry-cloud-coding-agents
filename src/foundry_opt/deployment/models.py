from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit

from foundry_opt.drafts.models import project_endpoint_components
from foundry_opt.evaluation import AgentVersionRef, EvaluationPolicy
from foundry_opt.packaging import BundleArtifact


DEPLOYMENT_OIDC_CLIENT_ID = "f9a80789-0e85-44df-9345-e6123d3a7dfa"
DEPLOYMENT_REQUIRED_ROLE = "Azure AI Project Manager"
DEPLOYMENT_ROLE_SCOPE = "project"
_AGENT_NAME = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TREE_HASH = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SENSITIVE_MARKERS = (
    "SECRET",
    "PASSWORD",
    "TOKEN",
    "API_KEY",
    "PRIVATE_KEY",
    "CONNECTION_STRING",
    "CREDENTIAL",
)


def repository_path(value: Path, field_name: str) -> Path:
    raw = str(value)
    windows = PureWindowsPath(raw)
    posix = PurePosixPath(raw.replace("\\", "/"))
    if (
        not raw
        or windows.drive
        or raw.startswith(("/", "\\"))
        or posix == PurePosixPath(".")
        or ".." in posix.parts
    ):
        raise ValueError(f"{field_name} must be repository-relative")
    return Path(posix.as_posix())


def _sha256(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256")


def _tree_hash(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _TREE_HASH.fullmatch(value):
        raise ValueError(f"{field_name} is invalid")


def _identifier(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field_name} is invalid")


def _safe_url(value: str, hosts: set[str], field_name: str) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{field_name} is invalid") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname.casefold() not in hosts
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{field_name} is invalid")


@dataclass(frozen=True)
class DeploymentRequest:
    project_endpoint: str
    agent_name: str
    base_version: int
    expected_baseline_source_sha256: str
    bundle: BundleArtifact
    runtime: str
    entry_point: tuple[str, ...]
    dependency_resolution: str
    patch_sha256: str
    tree_hash: str
    evidence_sha256: str
    description: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        project_endpoint_components(self.project_endpoint)
        if not _AGENT_NAME.fullmatch(self.agent_name):
            raise ValueError("agent_name is invalid")
        if (
            not isinstance(self.base_version, int)
            or isinstance(self.base_version, bool)
            or self.base_version < 1
        ):
            raise ValueError("base_version must be a positive published version")
        _sha256(
            self.expected_baseline_source_sha256,
            "expected_baseline_source_sha256",
        )
        if not isinstance(self.bundle, BundleArtifact):
            raise ValueError("bundle must be a BundleArtifact")
        if not self.runtime:
            raise ValueError("runtime is required")
        if not self.entry_point or any(not part for part in self.entry_point):
            raise ValueError("entry_point must not be empty")
        if self.dependency_resolution not in {"remote_build", "bundled"}:
            raise ValueError("dependency_resolution is invalid")
        _sha256(self.patch_sha256, "patch_sha256")
        _tree_hash(self.tree_hash, "tree_hash")
        _sha256(self.evidence_sha256, "evidence_sha256")
        metadata = dict(self.metadata)
        if len(metadata) > 10:
            raise ValueError(
                "metadata permits at most 10 caller entries; six entries "
                "are reserved for deployment provenance"
            )
        for key, value in metadata.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("metadata keys and values must be strings")
            normalized = key.upper().replace("-", "_")
            if any(marker in normalized for marker in _SENSITIVE_MARKERS):
                raise ValueError("metadata must not contain credentials")
            if _looks_sensitive(value):
                raise ValueError("metadata must not contain credentials")
        if {
            "foundry-opt-base-version",
            "foundry-opt-baseline-source-sha256",
            "foundry-opt-source-sha256",
            "foundry-opt-patch-sha256",
            "foundry-opt-tree-hash",
            "foundry-opt-evidence-sha256",
        } & metadata.keys():
            raise ValueError("foundry-opt deployment provenance is reserved")
        if self.description is not None and _looks_sensitive(self.description):
            raise ValueError("description must not contain credentials")
        object.__setattr__(self, "metadata", MappingProxyType(metadata))


@dataclass(frozen=True)
class DeploymentRecord:
    project_endpoint: str
    agent_name: str
    version: int
    base_version: int
    baseline_source_sha256: str
    sha256: str
    patch_sha256: str
    tree_hash: str
    evidence_sha256: str
    status: str | None = None
    portal_url: str | None = None
    runtime: str = ""
    entry_point: tuple[str, ...] = ()
    dependency_resolution: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        project_endpoint_components(self.project_endpoint)
        if not _AGENT_NAME.fullmatch(self.agent_name):
            raise ValueError("agent_name is invalid")
        if (
            not isinstance(self.version, int)
            or isinstance(self.version, bool)
            or self.version < 1
            or self.base_version < 1
            or self.version <= self.base_version
        ):
            raise ValueError("deployment versions must be positive integers")
        _sha256(
            self.baseline_source_sha256,
            "baseline_source_sha256",
        )
        _sha256(self.sha256, "sha256")
        _sha256(self.patch_sha256, "patch_sha256")
        _tree_hash(self.tree_hash, "tree_hash")
        _sha256(self.evidence_sha256, "evidence_sha256")
        if not self.runtime:
            raise ValueError("deployment runtime is required")
        if not self.entry_point or any(not part for part in self.entry_point):
            raise ValueError("deployment entry_point is required")
        if self.dependency_resolution not in {"remote_build", "bundled"}:
            raise ValueError("deployment dependency_resolution is invalid")
        metadata = dict(self.metadata)
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in metadata.items()
        ):
            raise ValueError("deployment metadata must contain strings")
        object.__setattr__(self, "metadata", MappingProxyType(metadata))
        if self.portal_url is not None:
            _safe_url(
                self.portal_url,
                {"ai.azure.com", "portal.azure.com"},
                "portal_url",
            )


class DeploymentTrigger(StrEnum):
    MERGE = "merge"
    MANUAL = "manual"


@dataclass(frozen=True)
class DeploymentWorkflowModel:
    trigger: DeploymentTrigger
    permissions: tuple[str, ...]
    actions: tuple[str, ...]


@dataclass(frozen=True)
class DeploymentWorkflowScaffold:
    description: str
    model: DeploymentWorkflowModel


@dataclass(frozen=True)
class DeploymentWorkflow:
    path: Path
    trigger: DeploymentTrigger
    exists: bool = True
    name: str = "Deployment workflow"
    scaffold: DeploymentWorkflowScaffold | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "path",
            repository_path(self.path, "workflow path"),
        )
        if self.path.suffix.casefold() not in {".yml", ".yaml"}:
            raise ValueError("workflow path must be YAML")
        if not self.name:
            raise ValueError("workflow name is required")
        if self.exists and self.scaffold is not None:
            raise ValueError("existing workflows cannot have a scaffold")
        if not self.exists and self.scaffold is None:
            raise ValueError("missing workflows require a scaffold model")


class WorkflowRunStatus(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class DeploymentWorkflowRun:
    path: Path
    trigger: DeploymentTrigger
    status: WorkflowRunStatus
    head_commit: str
    url: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "path",
            repository_path(self.path, "workflow run path"),
        )
        if not _COMMIT.fullmatch(self.head_commit):
            raise ValueError("head_commit is invalid")
        _safe_url(self.url, {"github.com"}, "workflow run URL")
        parts = tuple(part for part in urlsplit(self.url).path.split("/") if part)
        if (
            len(parts) != 5
            or parts[2:4] != ("actions", "runs")
            or not parts[4].isdigit()
        ):
            raise ValueError("workflow run URL is invalid")


@dataclass(frozen=True)
class DeployedRuntime:
    agent_name: str
    deployed_version: int
    latest_version: int
    source_sha256: str
    portal_url: str

    def __post_init__(self) -> None:
        if not _AGENT_NAME.fullmatch(self.agent_name):
            raise ValueError("agent_name is invalid")
        for value in (self.deployed_version, self.latest_version):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 1
            ):
                raise ValueError("runtime versions must be positive integers")
        _sha256(self.source_sha256, "source_sha256")
        _safe_url(
            self.portal_url,
            {"ai.azure.com", "portal.azure.com"},
            "portal_url",
        )


@dataclass(frozen=True)
class DeploymentVerificationRequest:
    repository_root: Path
    candidate_id: str
    patch_path: Path
    expected_patch_sha256: str
    expected_base_commit: str
    expected_baseline_source_sha256: str
    expected_tree_hash: str
    deployed_tree_hash: str
    evidence_path: Path
    expected_evidence_sha256: str
    expected_campaign_id: str
    expected_baseline_subject_id: str
    baseline_bundle: BundleArtifact
    expected_baseline_bundle_sha256: str
    bundle: BundleArtifact
    expected_bundle_sha256: str
    expected_project_endpoint: str
    expected_agent_name: str
    expected_base_version: int
    expected_version: int
    expected_runtime: str
    expected_entry_point: tuple[str, ...]
    expected_dependency_resolution: str
    expected_baseline_agent: AgentVersionRef
    expected_candidate_agents: Mapping[str, AgentVersionRef]
    expected_metric_policy: EvaluationPolicy
    expected_commit: str
    expected_run_url: str
    expected_portal_url: str
    record: DeploymentRecord
    workflow: DeploymentWorkflow
    workflow_run: DeploymentWorkflowRun | None
    runtime: DeployedRuntime | None
    bundle_include: tuple[str, ...] = ("**",)
    bundle_exclude: tuple[str, ...] = ()
    bundle_dependency_resolution: str = "remote_build"
    bundle_evidence_paths: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.candidate_id, "candidate_id")
        object.__setattr__(
            self,
            "patch_path",
            repository_path(self.patch_path, "patch_path"),
        )
        object.__setattr__(
            self,
            "evidence_path",
            repository_path(self.evidence_path, "evidence_path"),
        )
        _sha256(self.expected_patch_sha256, "expected_patch_sha256")
        if not _COMMIT.fullmatch(self.expected_base_commit):
            raise ValueError("expected_base_commit is invalid")
        _sha256(
            self.expected_baseline_source_sha256,
            "expected_baseline_source_sha256",
        )
        _tree_hash(self.expected_tree_hash, "expected_tree_hash")
        _tree_hash(self.deployed_tree_hash, "deployed_tree_hash")
        _sha256(
            self.expected_evidence_sha256,
            "expected_evidence_sha256",
        )
        _identifier(self.expected_campaign_id, "expected_campaign_id")
        _identifier(
            self.expected_baseline_subject_id,
            "expected_baseline_subject_id",
        )
        if not isinstance(self.baseline_bundle, BundleArtifact):
            raise ValueError("baseline_bundle must be a BundleArtifact")
        _sha256(
            self.expected_baseline_bundle_sha256,
            "expected_baseline_bundle_sha256",
        )
        _sha256(self.expected_bundle_sha256, "expected_bundle_sha256")
        project_endpoint_components(self.expected_project_endpoint)
        if not _AGENT_NAME.fullmatch(self.expected_agent_name):
            raise ValueError("expected_agent_name is invalid")
        if (
            not isinstance(self.expected_base_version, int)
            or isinstance(self.expected_base_version, bool)
            or self.expected_base_version < 1
        ):
            raise ValueError("expected_base_version is invalid")
        if (
            not isinstance(self.expected_version, int)
            or isinstance(self.expected_version, bool)
            or self.expected_version <= self.expected_base_version
        ):
            raise ValueError("expected_version is invalid")
        if not self.expected_runtime:
            raise ValueError("expected_runtime is required")
        if not self.expected_entry_point or any(
            not part for part in self.expected_entry_point
        ):
            raise ValueError("expected_entry_point is required")
        if self.expected_dependency_resolution not in {
            "remote_build",
            "bundled",
        }:
            raise ValueError("expected_dependency_resolution is invalid")
        if (
            not isinstance(self.expected_baseline_agent, AgentVersionRef)
            or self.expected_baseline_agent.agent_id
            != self.expected_agent_name
        ):
            raise ValueError("expected_baseline_agent is invalid")
        candidate_agents = dict(self.expected_candidate_agents)
        if (
            self.candidate_id not in candidate_agents
            or self.expected_baseline_agent in candidate_agents.values()
            or len(set(candidate_agents.values())) != len(candidate_agents)
            or any(
                not isinstance(subject_id, str)
                or not _IDENTIFIER.fullmatch(subject_id)
                or not isinstance(agent, AgentVersionRef)
                or agent.agent_id != self.expected_agent_name
                for subject_id, agent in candidate_agents.items()
            )
        ):
            raise ValueError("expected_candidate_agents is invalid")
        object.__setattr__(
            self,
            "expected_candidate_agents",
            MappingProxyType(candidate_agents),
        )
        if not isinstance(self.expected_metric_policy, EvaluationPolicy):
            raise ValueError(
                "expected_metric_policy must be an EvaluationPolicy"
            )
        if not _COMMIT.fullmatch(self.expected_commit):
            raise ValueError("expected_commit is invalid")
        _safe_url(
            self.expected_run_url,
            {"github.com"},
            "expected_run_url",
        )
        _safe_url(
            self.expected_portal_url,
            {"ai.azure.com", "portal.azure.com"},
            "expected_portal_url",
        )
        if not self.bundle_include or any(
            not pattern.strip()
            for pattern in self.bundle_include + self.bundle_exclude
        ):
            raise ValueError("bundle patterns must not be empty")
        if self.bundle_dependency_resolution not in {
            "remote_build",
            "bundled",
        }:
            raise ValueError("bundle_dependency_resolution is invalid")
        object.__setattr__(
            self,
            "bundle_evidence_paths",
            tuple(
                repository_path(path, "bundle evidence path")
                for path in self.bundle_evidence_paths
            ),
        )


class DeploymentVerificationStatus(StrEnum):
    VERIFIED = "verified"
    MANUAL_TRIGGER_REQUIRED = "manual_trigger_required"
    MERGE_DEPLOYMENT_PENDING = "merge_deployment_pending"
    WORKFLOW_PENDING = "workflow_pending"
    WORKFLOW_FAILED = "workflow_failed"
    MISMATCH = "mismatch"


@dataclass(frozen=True)
class DeploymentCheck:
    name: str
    passed: bool


@dataclass(frozen=True)
class DeploymentVerification:
    verified: bool
    status: DeploymentVerificationStatus
    version: int | None
    sha256: str | None
    run_url: str | None
    portal_url: str | None
    checks: tuple[DeploymentCheck, ...] = ()

    @property
    def failed_checks(self) -> tuple[str, ...]:
        return tuple(check.name for check in self.checks if not check.passed)


def _looks_sensitive(value: str) -> bool:
    lowered = value.casefold()
    return any(
        marker in lowered
        for marker in (
            "authorization: bearer ",
            "authorization=bearer ",
            "accountkey=",
            "sharedaccesskey=",
            "clientsecret=",
            "client_secret=",
            "private key-----",
            "api_key=",
            "api-key=",
            "access_token=",
            "access-token=",
            "?sig=",
            "&sig=",
        )
    )
