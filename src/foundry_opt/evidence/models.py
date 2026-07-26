from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from foundry_opt.evaluation import EvaluationResult, ParetoResult


@dataclass(frozen=True)
class TelemetryEvidence:
    response_id: str
    request_count: int
    dependency_count: int
    exception_count: int
    duration_ms: float
    success_rate: float | None


@dataclass(frozen=True)
class EvidenceRequest:
    output_path: Path
    campaign_id: str
    baseline: EvaluationResult
    candidates: tuple[EvaluationResult, ...]
    pareto: ParetoResult
    source_hash: str
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
