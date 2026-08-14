from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
import json
from math import isfinite
import re
from types import MappingProxyType
from typing import Mapping, Protocol

from foundry_opt.drafts import DraftRequest
from foundry_opt.evaluation import (
    DatasetSplit,
    EvaluationPolicy,
    EvaluationResult,
    EvaluationSubject,
)


class DirectExperimentUnavailable(RuntimeError):
    """Direct execution is ineligible before any Foundry side effect."""


class CandidateExperimentPending(RuntimeError):
    """A persisted Actions operation has not produced its result yet."""

    def __init__(self, idempotency_key: str) -> None:
        self.idempotency_key = idempotency_key
        super().__init__(
            "candidate experiment awaits trusted Actions reconciliation"
        )


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


def _identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} is invalid")


def _sha256(value: str, name: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{name} must be a SHA-256 digest")


def _safe_text(value: str, name: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{name} is invalid")


def _frozen_metrics(values: Mapping[str, float]) -> Mapping[str, float]:
    copied: dict[str, float] = {}
    for name, value in values.items():
        _identifier(name, "metric name")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
        ):
            raise ValueError("candidate experiment metrics must be finite")
        copied[name] = float(value)
    return MappingProxyType(copied)


def _frozen_guardrails(values: Mapping[str, str]) -> Mapping[str, str]:
    copied: dict[str, str] = {}
    for name, value in values.items():
        _identifier(name, "guardrail name")
        _safe_text(value, "guardrail result")
        copied[name] = value
    return MappingProxyType(copied)


@dataclass(frozen=True)
class CandidateExperimentRequest:
    issue_number: int
    candidate_id: str
    patch_sha256: str
    bundle_sha256: str
    evidence_sha256: str
    idempotency_key: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.issue_number, bool)
            or not isinstance(self.issue_number, int)
            or self.issue_number < 1
        ):
            raise ValueError("issue_number must be positive")
        _identifier(self.candidate_id, "candidate_id")
        _sha256(self.patch_sha256, "patch_sha256")
        _sha256(self.bundle_sha256, "bundle_sha256")
        _sha256(self.evidence_sha256, "evidence_sha256")
        _sha256(self.idempotency_key, "idempotency_key")


@dataclass(frozen=True)
class CandidateExperimentResult:
    candidate_id: str
    executor: str
    metrics: Mapping[str, float]
    guardrails: Mapping[str, str]
    draft_id: str
    evaluation_id: str
    run_id: str
    bundle_sha256: str
    evidence_sha256: str
    operation_sha256: str | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.candidate_id, "candidate_id")
        _identifier(self.executor, "executor")
        object.__setattr__(self, "metrics", _frozen_metrics(self.metrics))
        object.__setattr__(
            self,
            "guardrails",
            _frozen_guardrails(self.guardrails),
        )
        for value, name in (
            (self.draft_id, "draft_id"),
            (self.evaluation_id, "evaluation_id"),
            (self.run_id, "run_id"),
        ):
            _safe_text(value, name)
        _sha256(self.bundle_sha256, "result bundle_sha256")
        _sha256(self.evidence_sha256, "result evidence_sha256")
        if (self.operation_sha256 is None) != (
            self.idempotency_key is None
        ):
            raise ValueError(
                "candidate experiment result lineage is incomplete"
            )
        if self.operation_sha256 is not None:
            _sha256(self.operation_sha256, "operation_sha256")
            _sha256(self.idempotency_key, "result idempotency_key")


@dataclass(frozen=True)
class CandidateExperimentOperation:
    issue_number: int
    candidate_id: str
    patch_sha256: str
    bundle_sha256: str
    evidence_sha256: str
    idempotency_key: str
    schema_version: int = field(default=2, init=False)
    kind: str = field(default="candidate_experiment", init=False)

    def __post_init__(self) -> None:
        CandidateExperimentRequest(
            issue_number=self.issue_number,
            candidate_id=self.candidate_id,
            patch_sha256=self.patch_sha256,
            bundle_sha256=self.bundle_sha256,
            evidence_sha256=self.evidence_sha256,
            idempotency_key=self.idempotency_key,
        )

    @classmethod
    def from_request(
        cls,
        request: CandidateExperimentRequest,
    ) -> "CandidateExperimentOperation":
        return cls(
            issue_number=request.issue_number,
            candidate_id=request.candidate_id,
            patch_sha256=request.patch_sha256,
            bundle_sha256=request.bundle_sha256,
            evidence_sha256=request.evidence_sha256,
            idempotency_key=request.idempotency_key,
        )

    def to_dict(self) -> dict[str, int | str]:
        return {
            "candidate_id": self.candidate_id,
            "bundle_sha256": self.bundle_sha256,
            "evidence_sha256": self.evidence_sha256,
            "idempotency_key": self.idempotency_key,
            "issue_number": self.issue_number,
            "kind": self.kind,
            "patch_sha256": self.patch_sha256,
            "schema_version": self.schema_version,
        }

    @property
    def sha256(self) -> str:
        payload = json.dumps(
            self.to_dict(),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class PersistedCandidateExperimentOperation:
    operation: CandidateExperimentOperation
    reference: str
    sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.reference, str)
            or not _REFERENCE.fullmatch(self.reference)
            or "://" in self.reference
        ):
            raise ValueError("persisted experiment reference is invalid")
        _sha256(self.sha256, "persisted experiment sha256")
        if self.sha256 != self.operation.sha256:
            raise ValueError("persisted experiment operation changed")


EvaluationRunner = Callable[
    [EvaluationSubject, DatasetSplit, int],
    EvaluationResult,
]


@dataclass(frozen=True)
class CandidateExperimentPlan:
    patch_sha256: str
    evidence_sha256: str
    draft_request: DraftRequest
    split: DatasetSplit
    policy: EvaluationPolicy
    evaluate: EvaluationRunner

    def __post_init__(self) -> None:
        _sha256(self.patch_sha256, "candidate plan patch_sha256")
        _sha256(self.evidence_sha256, "candidate plan evidence_sha256")
        if self.split is not DatasetSplit.DEVELOPMENT:
            raise ValueError(
                "candidate experiments may only use development data"
            )


class CandidateExperimentAdapter(Protocol):
    """Evaluate once, or report unavailable before any side effect."""

    def evaluate(
        self,
        request: CandidateExperimentRequest,
    ) -> CandidateExperimentResult: ...


class CandidateExperimentActionsGateway(Protocol):
    """Durable transport for one consolidated candidate experiment.

    ``persist`` must return the canonical operation envelope. ``dispatch``
    must be idempotent for the envelope's SHA-256 and idempotency key, so
    retries cannot create a second Foundry operation. ``reconcile`` returns
    only a result bound to that same persisted lineage.
    """

    def persist(
        self,
        operation: CandidateExperimentOperation,
    ) -> PersistedCandidateExperimentOperation: ...

    def dispatch(
        self,
        operation: PersistedCandidateExperimentOperation,
    ) -> None: ...

    def reconcile(
        self,
        operation: PersistedCandidateExperimentOperation,
    ) -> CandidateExperimentResult | None: ...


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
