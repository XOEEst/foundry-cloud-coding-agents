from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlsplit

from foundry_opt.evaluation import (
    EvaluationPolicy,
    EvaluationResult,
    ParetoResult,
)
from foundry_opt.security import reject_secret_content


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ASSET_KINDS = frozenset({"dataset", "evaluator"})
_ASSET_ROLES = frozenset({"development", "validation"})
_APPROVAL_GATES = frozenset({"policy", "human"})
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_MAX_REMOTE_ID_LENGTH = 2048
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "sig",
        "signature",
        "token",
        "key",
        "sas",
        "secret",
        "password",
        "credential",
        "access_key",
        "accountkey",
        "client_secret",
        "api_key",
    }
)


def _require_identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} must be a safe bounded identifier")


def _require_opaque_reference(value: str, field: str) -> None:
    """Validate an opaque remote reference (resource ID, URI, or provider
    identity string such as ``builtin:name:version``) without constraining
    it to the strict identifier charset. Rejects empty/oversized values,
    control characters, secret-shaped content, and credential-bearing query
    parameters, so provider-native references are safe to persist while
    remaining free of embedded secrets.
    """
    if not isinstance(value, str) or not value or len(value) > _MAX_REMOTE_ID_LENGTH:
        raise ValueError(
            f"{field} must be a non-empty string of at most "
            f"{_MAX_REMOTE_ID_LENGTH} characters"
        )
    if _CONTROL_CHARACTERS.search(value):
        raise ValueError(f"{field} must not contain control characters")
    reject_secret_content(value)
    query = urlsplit(value).query
    if query:
        query_keys = {
            key.casefold() for key, _ in parse_qsl(query, keep_blank_values=True)
        }
        if query_keys & _SENSITIVE_QUERY_KEYS:
            raise ValueError(
                f"{field} must not embed credential-like query parameters"
            )


def _require_asset_metrics(kind: str, metrics: tuple[str, ...]) -> None:
    """Validate the metric names a dataset/evaluator asset produces.

    Datasets never produce metrics, so their tuple must be empty. Evaluators
    must declare at least one unique, safe metric name so downstream binding
    can map each approved metric to exactly one evaluator.
    """
    if kind == "dataset":
        if metrics:
            raise ValueError("dataset asset references must not define metrics")
        return
    if not metrics:
        raise ValueError(
            "evaluator asset references require at least one metric"
        )
    if len(set(metrics)) != len(metrics):
        raise ValueError("evaluator asset reference metrics must be unique")
    for metric in metrics:
        _require_identifier(metric, "metric")


@dataclass(frozen=True)
class EvaluationAssetReference:
    """Immutable identity/provenance reference for a dataset or evaluator
    asset used by a campaign.

    This carries only identity and provenance metadata (never raw asset
    rows, dataset contents, or evaluator prompts) so it is safe to persist
    in campaign state and evidence output.
    """

    asset_id: str
    kind: str
    source: str
    role: str | None = None
    name: str | None = None
    version: str | None = None
    remote_id: str | None = None
    content_sha256: str | None = None
    approval_gate: str = "policy"
    metrics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", tuple(self.metrics))
        _require_identifier(self.asset_id, "asset_id")
        _require_identifier(self.source, "source")
        if self.kind not in _ASSET_KINDS:
            raise ValueError("kind must be dataset or evaluator")
        if self.kind == "dataset":
            if self.role not in _ASSET_ROLES:
                raise ValueError(
                    "dataset asset reference requires a dataset role"
                )
        elif self.role is not None:
            raise ValueError(
                "evaluator asset reference must not define a role"
            )
        for value, field in (
            (self.name, "name"),
            (self.version, "version"),
        ):
            if value is not None:
                _require_identifier(value, field)
        if self.remote_id is not None:
            _require_opaque_reference(self.remote_id, "remote_id")
        if self.content_sha256 is not None and not _SHA256.fullmatch(
            self.content_sha256
        ):
            raise ValueError("content_sha256 must be a SHA-256 digest")
        if self.approval_gate not in _APPROVAL_GATES:
            raise ValueError("approval_gate must be policy or human")
        _require_asset_metrics(self.kind, self.metrics)


@dataclass(frozen=True)
class TelemetryEvidence:
    response_id: str
    request_count: int
    dependency_count: int
    exception_count: int
    duration_ms: float
    success_rate: float | None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not isfinite(self.duration_ms):
            raise ValueError("Telemetry evidence duration must be finite.")
        if self.success_rate is not None and not isfinite(self.success_rate):
            raise ValueError("Telemetry evidence success rate must be finite.")


@dataclass(frozen=True)
class EvidenceRequest:
    output_path: Path
    campaign_id: str
    baseline: EvaluationResult
    candidates: tuple[EvaluationResult, ...]
    pareto: ParetoResult
    metric_policies: EvaluationPolicy
    source_hash: str
    goal: str
    spec_sha256: str
    assets: tuple[EvaluationAssetReference, ...]
    patch_hashes: dict[str, str] | None = None
    telemetry: tuple[TelemetryEvidence, ...] = ()
    sensitive_values: tuple[str, ...] = ()
    generated_at: datetime | None = None


@dataclass(frozen=True)
class EvidenceManifest:
    path: Path
    sha256: str
    byte_count: int
    evaluation_ids: tuple[str, ...]
    run_ids: tuple[str, ...]
    goal_sha256: str
    spec_sha256: str
