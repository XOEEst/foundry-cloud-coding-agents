from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from foundry_opt.orchestration.candidate_experiments import (
    CandidateExperimentAdapter,
    CandidateExperimentRequest,
)


@dataclass(frozen=True)
class CandidateSearchSummary:
    candidate_id: str
    patch_sha256: str
    bundle_sha256: str
    evidence_sha256: str
    idempotency_key: str
    executor: str
    metrics: Mapping[str, float]
    guardrails: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "metrics",
            MappingProxyType(dict(self.metrics)),
        )
        object.__setattr__(
            self,
            "guardrails",
            MappingProxyType(dict(self.guardrails)),
        )


class BoundedCandidateSearch:
    """Sequential internal search that returns only selection-safe summaries."""

    def __init__(
        self,
        *,
        runner: CandidateExperimentAdapter,
        max_candidates: int,
    ) -> None:
        if (
            isinstance(max_candidates, bool)
            or not isinstance(max_candidates, int)
            or not 1 <= max_candidates <= 32
        ):
            raise ValueError("max_candidates must be between 1 and 32")
        self._runner = runner
        self._max_candidates = max_candidates

    def evaluate(
        self,
        requests: Sequence[CandidateExperimentRequest],
    ) -> tuple[CandidateSearchSummary, ...]:
        configured = tuple(requests)
        if not configured:
            raise ValueError(
                "candidate search requires at least one configured candidate"
            )
        if len(configured) > self._max_candidates:
            raise ValueError("candidate search exceeds its configured bound")
        candidate_ids = tuple(item.candidate_id for item in configured)
        idempotency_keys = tuple(
            item.idempotency_key for item in configured
        )
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate search IDs must be unique")
        if len(idempotency_keys) != len(set(idempotency_keys)):
            raise ValueError(
                "candidate search idempotency keys must be unique"
            )
        summaries: list[CandidateSearchSummary] = []
        for request in configured:
            result = self._runner.evaluate(request)
            if result.candidate_id != request.candidate_id:
                raise ValueError(
                    "candidate search result changed candidate binding"
                )
            summaries.append(
                CandidateSearchSummary(
                    candidate_id=request.candidate_id,
                    patch_sha256=request.patch_sha256,
                    bundle_sha256=result.bundle_sha256,
                    evidence_sha256=result.evidence_sha256,
                    idempotency_key=request.idempotency_key,
                    executor=result.executor,
                    metrics=result.metrics,
                    guardrails=result.guardrails,
                )
            )
        return tuple(summaries)


__all__ = ["BoundedCandidateSearch", "CandidateSearchSummary"]
