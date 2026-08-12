from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol


class DirectExperimentUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class CandidateExperimentRequest:
    issue_number: int
    candidate_id: str
    patch_sha256: str
    idempotency_key: str


@dataclass(frozen=True)
class CandidateExperimentResult:
    candidate_id: str
    executor: str
    metrics: Mapping[str, float]
    guardrails: Mapping[str, str]
    draft_id: str
    evaluation_id: str
    run_id: str


class CandidateExperimentAdapter(Protocol):
    """Evaluate once, or report unavailable before any side effect."""

    def evaluate(
        self,
        request: CandidateExperimentRequest,
    ) -> CandidateExperimentResult: ...


class CandidateExperimentRunner:
    def __init__(
        self,
        *,
        direct: CandidateExperimentAdapter,
        fallback: CandidateExperimentAdapter,
    ) -> None:
        self._direct = direct
        self._fallback = fallback

    def evaluate(
        self,
        request: CandidateExperimentRequest,
    ) -> CandidateExperimentResult:
        try:
            return self._direct.evaluate(request)
        except DirectExperimentUnavailable:
            return self._fallback.evaluate(request)
