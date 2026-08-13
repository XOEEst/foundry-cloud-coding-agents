from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from math import isfinite
import re
from types import MappingProxyType
from typing import Mapping

from foundry_opt.orchestration.workspace import WorkspacePhase


@dataclass(frozen=True)
class CandidateSummary:
    candidate_id: str
    metrics: Mapping[str, float]
    eligible: bool
    selected: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "metrics",
            MappingProxyType(dict(self.metrics)),
        )


@dataclass(frozen=True)
class WorkspaceSpecificationRecord:
    status: str
    spec_sha256: str | None
    base_commit: str
    target: str
    environment: str
    asset_ids: tuple[str, ...]
    metric_names: tuple[str, ...]
    policy_reason: str

    def __post_init__(self) -> None:
        if self.status not in {"policy_approved", "human_review_required"}:
            raise ValueError("workspace specification status is invalid")
        if (
            self.status == "policy_approved"
            and (
                self.spec_sha256 is None
                or re.fullmatch(r"[0-9a-f]{64}", self.spec_sha256)
                is None
            )
        ) or (
            self.status == "human_review_required"
            and self.spec_sha256 is not None
            and re.fullmatch(r"[0-9a-f]{64}", self.spec_sha256) is None
        ):
            raise ValueError("workspace specification digest is invalid")
        if re.fullmatch(r"[0-9a-f]{40}", self.base_commit) is None:
            raise ValueError("workspace specification base commit is invalid")
        for value, name in (
            (self.target, "target"),
            (self.environment, "environment"),
        ):
            if re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value
            ) is None:
                raise ValueError(
                    f"workspace specification {name} is invalid"
                )
        for values, name in (
            (self.asset_ids, "asset identities"),
            (self.metric_names, "metrics"),
        ):
            if (
                type(values) is not tuple
                or not values
                or len(set(values)) != len(values)
                or any(
                    re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,255}",
                        value,
                    )
                    is None
                    for value in values
                )
            ):
                raise ValueError(
                    f"workspace specification {name} are invalid"
                )
        if (
            not isinstance(self.policy_reason, str)
            or not self.policy_reason
            or len(self.policy_reason) > 512
            or any(ord(character) < 32 for character in self.policy_reason)
        ):
            raise ValueError(
                "workspace specification policy reason is invalid"
            )


@dataclass(frozen=True)
class WorkspaceBaselineRecord:
    status: str
    operation_sha256: str
    idempotency_key: str
    bundle_sha256: str
    evidence_sha256: str
    dataset_ids: tuple[str, ...]
    evaluator_ids: tuple[str, ...]
    split: str
    sample_count: int
    executor: str | None = None
    draft_id: str | None = None
    evaluation_id: str | None = None
    run_id: str | None = None
    metrics: Mapping[str, float] = field(default_factory=dict)
    guardrails: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in {"pending", "completed"}:
            raise ValueError("workspace baseline status is invalid")
        for value, name in (
            (self.operation_sha256, "operation"),
            (self.idempotency_key, "idempotency"),
            (self.bundle_sha256, "bundle"),
            (self.evidence_sha256, "evidence"),
        ):
            if re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError(
                    f"workspace baseline {name} digest is invalid"
                )
        for values, name in (
            (self.dataset_ids, "dataset identities"),
            (self.evaluator_ids, "evaluator identities"),
        ):
            if (
                type(values) is not tuple
                or not values
                or len(set(values)) != len(values)
                or any(
                    re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,255}",
                        value,
                    )
                    is None
                    for value in values
                )
            ):
                raise ValueError(f"workspace baseline {name} are invalid")
        if self.split != "development":
            raise ValueError("workspace baseline split is invalid")
        if type(self.sample_count) is not int or self.sample_count < 1:
            raise ValueError("workspace baseline sample count is invalid")
        metrics = dict(self.metrics)
        guardrails = dict(self.guardrails)
        if any(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name)
            is None
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            for name, value in metrics.items()
        ):
            raise ValueError("workspace baseline metrics are invalid")
        if any(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name)
            is None
            or not isinstance(value, str)
            or not value
            or len(value) > 512
            for name, value in guardrails.items()
        ):
            raise ValueError("workspace baseline guardrails are invalid")
        completed_values = (
            self.executor,
            self.draft_id,
            self.evaluation_id,
            self.run_id,
        )
        if self.status == "pending":
            if (
                any(value is not None for value in completed_values)
                or metrics
                or guardrails
            ):
                raise ValueError("pending workspace baseline has result fields")
        elif (
            any(
                not isinstance(value, str) or not value
                for value in completed_values
            )
            or not metrics
        ):
            raise ValueError("completed workspace baseline is incomplete")
        object.__setattr__(self, "metrics", MappingProxyType(metrics))
        object.__setattr__(
            self, "guardrails", MappingProxyType(guardrails)
        )


@dataclass(frozen=True)
class WorkspaceLineage:
    spec_sha256: str
    base_commit: str
    patch_sha256: str
    evidence_sha256: str
    bundle_sha256: str
    expected_tree: str
    selected_candidate_id: str
    workspace_pull_request_number: int
    required_checks: Mapping[str, str]
    required_checks_provenance: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.spec_sha256, "spec"),
            (self.patch_sha256, "patch"),
            (self.evidence_sha256, "evidence"),
            (self.bundle_sha256, "bundle"),
        ):
            if re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError(
                    f"workspace lineage {name} digest is invalid"
                )
        for value, name in (
            (self.base_commit, "base commit"),
            (self.expected_tree, "expected tree"),
        ):
            if re.fullmatch(r"[0-9a-f]{40}", value) is None:
                raise ValueError(f"workspace lineage {name} is invalid")
        if re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
            self.selected_candidate_id,
        ) is None:
            raise ValueError(
                "workspace lineage selected candidate is invalid"
            )
        if (
            type(self.workspace_pull_request_number) is not int
            or self.workspace_pull_request_number < 1
        ):
            raise ValueError(
                "workspace lineage pull request is invalid"
            )
        checks = dict(self.required_checks)
        if (
            not checks
            or any(
                not isinstance(name, str)
                or not name
                or len(name) > 256
                or any(ord(character) < 32 for character in name)
                or status != "success"
                for name, status in checks.items()
            )
        ):
            raise ValueError(
                "workspace lineage required checks are invalid"
            )
        if re.fullmatch(
            r"trusted-selector:head:[0-9a-f]{40}",
            self.required_checks_provenance,
        ) is None:
            raise ValueError(
                "workspace lineage check provenance is invalid"
            )
        object.__setattr__(
            self,
            "required_checks",
            MappingProxyType(checks),
        )


@dataclass(frozen=True)
class WorkspaceExperimentRecord:
    candidate_id: str
    patch_sha256: str
    bundle_sha256: str
    evidence_sha256: str
    idempotency_key: str
    operation_sha256: str
    status: str
    executor: str | None = None
    draft_id: str | None = None
    evaluation_id: str | None = None
    run_id: str | None = None
    metrics: Mapping[str, float] = field(default_factory=dict)
    guardrails: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
            self.candidate_id,
        ) is None:
            raise ValueError("workspace experiment candidate is invalid")
        for value, name in (
            (self.patch_sha256, "patch"),
            (self.bundle_sha256, "bundle"),
            (self.evidence_sha256, "evidence"),
            (self.idempotency_key, "idempotency"),
            (self.operation_sha256, "operation"),
        ):
            if re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError(
                    f"workspace experiment {name} digest is invalid"
                )
        if self.status not in {"pending", "completed"}:
            raise ValueError("workspace experiment status is invalid")
        metrics = dict(self.metrics)
        guardrails = dict(self.guardrails)
        if any(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name)
            is None
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            for name, value in metrics.items()
        ):
            raise ValueError("workspace experiment metrics are invalid")
        if any(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name)
            is None
            or not isinstance(value, str)
            or not value
            or len(value) > 512
            for name, value in guardrails.items()
        ):
            raise ValueError("workspace experiment guardrails are invalid")
        completed_values = (
            self.executor,
            self.draft_id,
            self.evaluation_id,
            self.run_id,
        )
        if self.status == "pending":
            if (
                any(value is not None for value in completed_values)
                or metrics
                or guardrails
            ):
                raise ValueError(
                    "pending workspace experiment has result fields"
                )
        elif (
            any(
                not isinstance(value, str) or not value
                for value in completed_values
            )
            or not metrics
        ):
            raise ValueError(
                "completed workspace experiment result is incomplete"
            )
        object.__setattr__(self, "metrics", MappingProxyType(metrics))
        object.__setattr__(
            self,
            "guardrails",
            MappingProxyType(guardrails),
        )


@dataclass(frozen=True)
class WorkspaceUpdate:
    issue_number: int
    phase: WorkspacePhase
    workspace_pull_request_number: int | None
    semantic_event: str
    candidates: tuple[CandidateSummary, ...] = ()
    selected_patch: bytes | None = None
    external_operation_ids: tuple[str, ...] = ()
    experiments: tuple[WorkspaceExperimentRecord, ...] = ()
    lineage: WorkspaceLineage | None = None
    specification: WorkspaceSpecificationRecord | None = None
    baseline: WorkspaceBaselineRecord | None = None


@dataclass(frozen=True)
class WorkspaceSnapshot:
    issue_number: int
    revision: str
    phase: WorkspacePhase
    workspace_pull_request_number: int | None
    candidates: tuple[CandidateSummary, ...]
    selected_patch: bytes | None
    external_operation_ids: tuple[str, ...]
    experiments: tuple[WorkspaceExperimentRecord, ...]
    lineage: WorkspaceLineage | None
    specification: WorkspaceSpecificationRecord | None = None
    baseline: WorkspaceBaselineRecord | None = None


@dataclass(frozen=True)
class AuditBundle:
    issue_number: int
    final_snapshot: WorkspaceSnapshot
    journal: tuple[str, ...]
    candidates: tuple[CandidateSummary, ...]
    selected_patch: bytes | None
    external_operation_ids: tuple[str, ...]
    experiments: tuple[WorkspaceExperimentRecord, ...]
    lineage: WorkspaceLineage | None
    retained_paths: tuple[str, ...]
    specification: WorkspaceSpecificationRecord | None = None
    baseline: WorkspaceBaselineRecord | None = None


class InMemoryWorkspaceStore:
    def __init__(self) -> None:
        self._snapshots: dict[int, WorkspaceSnapshot] = {}
        self._journals: dict[int, tuple[str, ...]] = {}

    def load(self, issue_number: int) -> WorkspaceSnapshot | None:
        return self._snapshots.get(issue_number)

    def commit(
        self,
        *,
        expected_revision: str | None,
        update: WorkspaceUpdate,
    ) -> WorkspaceSnapshot:
        current = self.load(update.issue_number)
        current_revision = current.revision if current is not None else None
        if current_revision != expected_revision:
            raise ValueError("workspace revision changed")
        if (
            current is not None
            and current.lineage is not None
            and update.lineage != current.lineage
        ):
            raise ValueError("workspace lineage changed")
        if (
            current is not None
            and current.specification is not None
            and update.specification != current.specification
        ):
            raise ValueError("workspace specification changed")
        _validate_baseline_update(
            update.baseline,
            current.baseline if current is not None else None,
        )
        _validate_experiment_records(
            update.experiments,
            current.experiments if current is not None else (),
        )
        _validate_lineage_update(update)
        revision = str(int(current_revision or "0") + 1)
        snapshot = WorkspaceSnapshot(
            issue_number=update.issue_number,
            revision=revision,
            phase=update.phase,
            workspace_pull_request_number=(
                update.workspace_pull_request_number
            ),
            candidates=update.candidates,
            selected_patch=update.selected_patch,
            external_operation_ids=update.external_operation_ids,
            experiments=update.experiments,
            lineage=update.lineage,
            specification=update.specification,
            baseline=update.baseline,
        )
        self._snapshots[update.issue_number] = snapshot
        self._journals[update.issue_number] = (
            *self._journals.get(update.issue_number, ()),
            update.semantic_event,
        )
        return snapshot

    def finalize(self, issue_number: int) -> AuditBundle:
        snapshot = self._snapshots.pop(issue_number)
        journal = self._journals.pop(issue_number)
        retained_paths = ["snapshot.json", "journal.jsonl"]
        if snapshot.candidates:
            retained_paths.append("evidence/candidates.json")
        if snapshot.selected_patch is not None:
            retained_paths.append("patches/selected.patch")
        return AuditBundle(
            issue_number=issue_number,
            final_snapshot=snapshot,
            journal=journal,
            candidates=snapshot.candidates,
            selected_patch=snapshot.selected_patch,
            external_operation_ids=snapshot.external_operation_ids,
            experiments=snapshot.experiments,
            lineage=snapshot.lineage,
            specification=snapshot.specification,
            baseline=snapshot.baseline,
            retained_paths=tuple(retained_paths),
        )


def _validate_lineage_update(update: WorkspaceUpdate) -> None:
    lineage = update.lineage
    if lineage is None:
        return
    selected = tuple(
        item for item in update.candidates if item.selected
    )
    if (
        update.selected_patch is None
        or hashlib.sha256(update.selected_patch).hexdigest()
        != lineage.patch_sha256
        or update.workspace_pull_request_number
        != lineage.workspace_pull_request_number
        or len(selected) != 1
        or selected[0].candidate_id
        != lineage.selected_candidate_id
    ):
        raise ValueError("workspace lineage does not match state")


def _validate_experiment_records(
    records: tuple[WorkspaceExperimentRecord, ...],
    previous: tuple[WorkspaceExperimentRecord, ...],
) -> None:
    if (
        type(records) is not tuple
        or any(type(item) is not WorkspaceExperimentRecord for item in records)
        or len({item.candidate_id for item in records}) != len(records)
        or len({item.idempotency_key for item in records}) != len(records)
        or len({item.operation_sha256 for item in records}) != len(records)
    ):
        raise ValueError("workspace experiment records are invalid")
    current_by_id = {item.candidate_id: item for item in records}
    for prior in previous:
        current = current_by_id.get(prior.candidate_id)
        if current is None:
            raise ValueError("workspace experiment record was removed")
        if prior.status == "completed" and current != prior:
            raise ValueError("completed workspace experiment changed")
        if prior.status == "pending" and (
            current.patch_sha256 != prior.patch_sha256
            or current.bundle_sha256 != prior.bundle_sha256
            or current.evidence_sha256 != prior.evidence_sha256
            or current.idempotency_key != prior.idempotency_key
            or current.operation_sha256 != prior.operation_sha256
        ):
            raise ValueError("workspace experiment lineage changed")


def _validate_baseline_update(
    baseline: WorkspaceBaselineRecord | None,
    previous: WorkspaceBaselineRecord | None,
) -> None:
    if previous is None:
        return
    if baseline is None:
        raise ValueError("workspace baseline was removed")
    if previous.status == "completed" and baseline != previous:
        raise ValueError("completed workspace baseline changed")
    if previous.status == "pending" and (
        baseline.operation_sha256 != previous.operation_sha256
        or baseline.idempotency_key != previous.idempotency_key
        or baseline.bundle_sha256 != previous.bundle_sha256
        or baseline.evidence_sha256 != previous.evidence_sha256
        or baseline.dataset_ids != previous.dataset_ids
        or baseline.evaluator_ids != previous.evaluator_ids
        or baseline.split != previous.split
        or baseline.sample_count != previous.sample_count
    ):
        raise ValueError("workspace baseline lineage changed")
