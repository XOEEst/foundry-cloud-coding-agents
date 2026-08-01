from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Callable

import yaml

from foundry_opt.campaign.state import FileCampaignStateStore
from foundry_opt.optimization.lifecycle import FileLifecycleStateStore
from foundry_opt.optimization.models import OptimizationSpec
from foundry_opt.optimization.specification import spec_file_path

from foundry_opt.optimization.commands import (
    OptimizationCommandService,
    OptimizeCommandRequest,
    OptimizeCommandResult,
    OptimizeCommandStatus,
    OptimizePhase,
)
from foundry_opt.orchestration.models import (
    AdvanceRequest,
    CampaignEvent,
    CampaignPhase,
    CampaignState,
    EventKind,
)
from foundry_opt.orchestration.campaign import OptimizationCampaign
from foundry_opt.orchestration.steward import (
    StewardAdvanceRequest,
    StewardAdvanceService,
    StewardAdvanceStatus,
)


class CompatibilityOptimizationCommandService:
    """Preserve phase commands while making the steward ledger authoritative."""

    def __init__(
        self,
        *,
        legacy: OptimizationCommandService,
        steward: StewardAdvanceService,
        clock: Callable[[], datetime] | None = None,
        projector: Callable[
            [OptimizeCommandRequest, CampaignState],
            tuple[CampaignEvent, ...],
        ]
        | None = None,
        fence: LegacyGenerationFence | None = None,
        precheck: Callable[
            [OptimizeCommandRequest], OptimizeCommandResult | None
        ]
        | None = None,
        runtime_namespace: LegacyRuntimeNamespace | None = None,
    ) -> None:
        self._legacy = legacy
        self._steward = steward
        self._clock = clock or (lambda: datetime.now(UTC))
        self._projector = projector or (
            lambda request, state: ()
        )
        self._fence = fence
        self._precheck = precheck
        self._runtime_namespace = runtime_namespace

    def execute(
        self,
        request: OptimizeCommandRequest,
    ) -> OptimizeCommandResult:
        precheck = (
            self._precheck(request)
            if self._precheck is not None
            else None
        )
        steward_result = self._steward.advance(
            StewardAdvanceRequest(
                request.repository_root,
                request.issue_number,
            )
        )
        if (
            steward_result.status is StewardAdvanceStatus.BLOCKED
            and steward_result.code == "campaign_not_initialized"
        ):
            if precheck is not None:
                return precheck
            return _gate_failure(request, steward_result.code)
        if steward_result.status in {
            StewardAdvanceStatus.CONFLICT,
            StewardAdvanceStatus.FAILED,
        }:
            if precheck is not None:
                return precheck
            return _gate_failure(request, steward_result.code)
        if steward_result.state is None:
            if precheck is not None:
                return precheck
            return _gate_failure(request, "campaign_state_unavailable")
        projected = self._project_trusted(request, steward_result.state)
        if isinstance(projected, OptimizeCommandResult):
            return projected
        if projected:
            steward_result = self._steward.advance(
                StewardAdvanceRequest(
                    request.repository_root,
                    request.issue_number,
                ),
                events=projected,
            )
            if _failed(steward_result.status):
                return _sync_failure(request, steward_result.code)
            steward_result = self._steward.advance(
                StewardAdvanceRequest(
                    request.repository_root,
                    request.issue_number,
                )
            )
            if steward_result.status in {
                StewardAdvanceStatus.CONFLICT,
                StewardAdvanceStatus.FAILED,
            }:
                return _gate_failure(request, steward_result.code)
            if steward_result.state is None:
                return _gate_failure(
                    request,
                    "campaign_state_unavailable",
                )
        gate = _gate(request, steward_result.state)
        if gate is not None:
            return gate
        canonical = _canonical_candidate_result(request, steward_result)
        if canonical is not None:
            return canonical
        canonical_selection = _canonical_selection_result(
            request,
            steward_result,
        )
        if canonical_selection is not None:
            return canonical_selection
        if precheck is not None:
            return precheck
        if self._runtime_namespace is not None:
            try:
                self._runtime_namespace.prepare(
                    request,
                    steward_result.state.generation,
                )
            except Exception:
                return _gate_failure(
                    request,
                    "compatibility_runtime_namespace_failed",
                )

        try:
            before = (
                self._fence.capture(request)
                if self._fence is not None
                else None
            )
        except Exception:
            return _gate_failure(
                request,
                "compatibility_generation_fence_unavailable",
            )
        result = self._legacy.execute(request)
        if result.status in {
            OptimizeCommandStatus.BLOCKED,
            OptimizeCommandStatus.FAILED,
        }:
            return result
        if self._fence is not None and before is not None:
            try:
                self._fence.record(
                    request,
                    steward_result.state.generation,
                    before,
                )
            except Exception:
                return _sync_failure(
                    request,
                    "compatibility_generation_fence_failed",
                )
        if steward_result.state is not None:
            projected = self._project(request, steward_result.state)
            if isinstance(projected, OptimizeCommandResult):
                return projected
            if projected:
                steward_result = self._steward.advance(
                    StewardAdvanceRequest(
                        request.repository_root,
                        request.issue_number,
                    ),
                    events=projected,
                )
        if _failed(steward_result.status):
            return _sync_failure(request, steward_result.code)
        return result

    def _project(
        self,
        request: OptimizeCommandRequest,
        state: CampaignState,
    ) -> tuple[CampaignEvent, ...] | OptimizeCommandResult:
        try:
            return self._projector(request, state)
        except Exception:
            return _sync_failure(
                request,
                "compatibility_projection_failed",
            )

    def _project_trusted(
        self,
        request: OptimizeCommandRequest,
        state: CampaignState,
    ) -> tuple[CampaignEvent, ...] | OptimizeCommandResult:
        trusted = getattr(self._projector, "trusted_events", None)
        if trusted is None:
            return ()
        try:
            return trusted(request, state)
        except Exception:
            return _sync_failure(
                request,
                "compatibility_trusted_projection_failed",
            )


@dataclass(frozen=True)
class VerifiedSpecApproval:
    spec_sha256: str
    approval_commit: str

    def __post_init__(self) -> None:
        if not _is_sha256(self.spec_sha256):
            raise ValueError("spec_sha256 must be a SHA-256 digest")
        if not _is_commit(self.approval_commit):
            raise ValueError("approval_commit must be a full Git commit")


class LegacyRuntimeNamespace:
    """Archive issue-keyed runner state whenever generation changes."""

    def prepare(
        self,
        request: OptimizeCommandRequest,
        generation: int,
    ) -> None:
        marker_path = _runtime_marker_path(request)
        marker = self._load_marker(marker_path, request.issue_number)
        prior = marker.get("generation")
        if prior == generation:
            return
        if prior is not None or generation > 1:
            label = (
                f"generation-{prior}"
                if isinstance(prior, int)
                else "generation-unknown"
            )
            archive = (
                marker_path.parent.parent
                / "archive"
                / f"issue-{request.issue_number}"
                / label
            )
            self._archive_runtime(request, archive)
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json(
            marker_path,
            {
                "generation": generation,
                "issue_number": request.issue_number,
                "schema_version": 1,
            },
        )

    def _load_marker(
        self,
        path: Path,
        issue_number: int,
    ) -> dict[str, Any]:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if (
            type(document) is not dict
            or document.get("schema_version") != 1
            or document.get("issue_number") != issue_number
            or type(document.get("generation")) is not int
        ):
            raise ValueError("legacy runtime namespace is invalid")
        return document

    def _archive_runtime(
        self,
        request: OptimizeCommandRequest,
        archive: Path,
    ) -> None:
        root = request.repository_root.expanduser().resolve()
        campaign = (
            root
            / ".foundry-optimizer"
            / "campaigns"
            / f"issue-{request.issue_number}"
        )
        lifecycle = (
            root
            / ".foundry-optimizer"
            / "lifecycle"
            / f"issue-{request.issue_number}.json"
        )
        if not campaign.exists() and not lifecycle.exists():
            return
        archive.mkdir(parents=True, exist_ok=False)
        if campaign.exists():
            shutil.move(str(campaign), str(archive / "campaign"))
        if lifecycle.exists():
            shutil.move(str(lifecycle), str(archive / "lifecycle.json"))


class LegacyGenerationFence:
    """Persist generation ownership only for artifacts changed after gating."""

    def __init__(
        self,
        *,
        artifacts: Callable[
            [OptimizeCommandRequest], dict[str, str]
        ]
        | None = None,
    ) -> None:
        self._artifacts = artifacts or _legacy_artifacts

    def capture(
        self,
        request: OptimizeCommandRequest,
    ) -> dict[str, str]:
        return dict(self._artifacts(request))

    def record(
        self,
        request: OptimizeCommandRequest,
        generation: int,
        before: dict[str, str],
    ) -> None:
        after = self._artifacts(request)
        document = self._load(request)
        records = document.setdefault("artifacts", {})
        for key, digest in after.items():
            if before.get(key) == digest:
                continue
            records[key] = {
                "generation": generation,
                "sha256": digest,
            }
        self._save(request, document)

    def generation(
        self,
        request: OptimizeCommandRequest,
        key: str,
        digest: str,
    ) -> int | None:
        record = self._load(request).get(
            "artifacts", {}
        ).get(key)
        if (
            not isinstance(record, dict)
            or record.get("sha256") != digest
            or type(record.get("generation")) is not int
        ):
            return None
        return record["generation"]

    def _load(
        self,
        request: OptimizeCommandRequest,
    ) -> dict[str, Any]:
        path = _fence_path(request)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {
                "artifacts": {},
                "issue_number": request.issue_number,
                "schema_version": 1,
            }
        if (
            type(document) is not dict
            or document.get("schema_version") != 1
            or document.get("issue_number") != request.issue_number
            or type(document.get("artifacts")) is not dict
        ):
            raise ValueError("legacy generation fence is invalid")
        return document

    def _save(
        self,
        request: OptimizeCommandRequest,
        document: dict[str, Any],
    ) -> None:
        path = _fence_path(request)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(
            f".{path.name}-{os.getpid()}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(
                    document,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


class LegacyCampaignEventProjector:
    """Project migration-only runner state into canonical campaign events."""

    def __init__(
        self,
        *,
        spec_sha256: Callable[[Path, int], str | None] | None = None,
        campaign_state: Callable[[Path, str], Any | None] | None = None,
        lifecycle_state: Callable[[Path, str], Any | None] | None = None,
        verified_spec_approval: Callable[
            [Path, int, str], VerifiedSpecApproval | None
        ]
        | None = None,
        artifact_generation: Callable[
            [OptimizeCommandRequest, str, str], int | None
        ]
        | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        campaign_store = FileCampaignStateStore()
        lifecycle_store = FileLifecycleStateStore()
        self._spec_sha256 = spec_sha256 or _load_spec_sha256
        self._campaign_state = (
            campaign_state or campaign_store.load
        )
        self._lifecycle_state = (
            lifecycle_state or lifecycle_store.load
        )
        self._verified_spec_approval = (
            verified_spec_approval or _load_verified_spec_approval
        )
        self._artifact_generation = (
            artifact_generation
            or (lambda request, key, digest: None)
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._campaign = OptimizationCampaign()

    def __call__(
        self,
        request: OptimizeCommandRequest,
        state: CampaignState,
    ) -> tuple[CampaignEvent, ...]:
        campaign_id = f"issue-{request.issue_number}"
        legacy = self._campaign_state(
            request.repository_root,
            campaign_id,
        )
        lifecycle = self._lifecycle_state(
            request.repository_root,
            campaign_id,
        )
        spec_sha256 = (
            getattr(legacy, "spec_sha256", None)
            or self._spec_sha256(
                request.repository_root,
                request.issue_number,
            )
        )
        working = state
        events: list[CampaignEvent] = []

        def append(
            kind: EventKind,
            identity: str,
            **payload: object,
        ) -> None:
            nonlocal working
            event = CampaignEvent(
                event_id=_event_id(
                    kind,
                    working.generation,
                    identity,
                ),
                kind=kind,
                generation=working.generation,
                occurred_at=self._clock(),
                payload=payload,
            )
            working = self._campaign.advance(
                AdvanceRequest(
                    request.issue_number,
                    working,
                    (event,),
                )
            ).state
            events.append(event)

        if (
            working.phase is CampaignPhase.SPECIFICATION
            and spec_sha256 is not None
            and self._current_generation(
                working,
                request,
                "spec",
                spec_sha256,
            )
        ):
            append(
                EventKind.SPEC_REVIEW_REQUIRED,
                spec_sha256,
                spec_sha256=spec_sha256,
            )
        if (
            working.phase is CampaignPhase.AWAITING_SPEC_APPROVAL
            and working.spec_sha256 is not None
        ):
            approval = self._verified_spec_approval(
                request.repository_root,
                request.issue_number,
                working.spec_sha256,
            )
            if (
                approval is not None
                and approval.spec_sha256 == working.spec_sha256
            ):
                append(
                    EventKind.SPEC_HUMAN_APPROVED,
                    approval.approval_commit,
                )
        baseline = (
            getattr(legacy, "baseline_development", None)
            if legacy is not None
            else None
        )
        if (
            working.phase is CampaignPhase.BASELINE
            and baseline is not None
        ):
            evaluation_id = baseline.run.evaluation_id
            digest = _semantic_digest(
                "baseline",
                legacy.spec_sha256,
                evaluation_id,
            )
            if self._current_generation(
                working,
                request,
                "baseline",
                digest,
            ):
                append(
                    EventKind.BASELINE_COMPLETED,
                    evaluation_id,
                    evaluation_id=evaluation_id,
                )
        if working.phase is CampaignPhase.CANDIDATES and legacy is not None:
            known = {
                candidate.candidate_id
                for candidate in working.candidates
            }
            for candidate in legacy.candidates:
                artifact = getattr(candidate, "artifact", None)
                if (
                    artifact is None
                    or candidate.candidate_id in known
                ):
                    continue
                evidence_path = (
                    request.repository_root / artifact.evidence_path
                )
                evidence_sha256 = hashlib.sha256(
                    evidence_path.read_bytes()
                ).hexdigest()
                digest = _semantic_digest(
                    "candidate",
                    legacy.spec_sha256,
                    candidate.candidate_id,
                    str(artifact.eligible),
                    evidence_sha256,
                )
                if not self._current_generation(
                    working,
                    request,
                    f"candidate:{candidate.candidate_id}",
                    digest,
                ):
                    continue
                append(
                    EventKind.CANDIDATE_EVALUATED,
                    f"{candidate.candidate_id}:{evidence_sha256}",
                    candidate_id=candidate.candidate_id,
                    eligible=artifact.eligible,
                    evidence_sha256=evidence_sha256,
                )
                known.add(candidate.candidate_id)
            if (
                working.phase is CampaignPhase.CANDIDATES
                and getattr(legacy, "finalized", None) is not None
                and working.candidates
                and self._current_generation(
                    working,
                    request,
                    "slate",
                    _slate_digest(legacy),
                )
            ):
                append(
                    EventKind.SLATE_PUBLISHED,
                    ",".join(
                        candidate.candidate_id
                        for candidate in working.candidates
                    ),
                )
        if lifecycle is not None:
            deployment_version = getattr(
                lifecycle,
                "deployment_version",
                None,
            )
            if (
                working.phase is CampaignPhase.DEPLOYMENT
                and deployment_version is not None
                and self._current_generation(
                    working,
                    request,
                    "lifecycle:deployment",
                    _semantic_digest(
                        "deployment",
                        str(deployment_version),
                    ),
                )
            ):
                append(
                    EventKind.DEPLOYMENT_COMPLETED,
                    str(deployment_version),
                    deployment_version=deployment_version,
                )
            if (
                working.phase is CampaignPhase.RETENTION
                and getattr(
                    lifecycle,
                    "post_deploy_retained",
                    False,
                )
                and self._current_generation(
                    working,
                    request,
                    "lifecycle:retention",
                    _semantic_digest("retention", "true"),
                )
            ):
                append(
                    EventKind.RETENTION_COMPLETED,
                    "retained",
                    retained=True,
                )
        return tuple(events)

    def trusted_events(
        self,
        request: OptimizeCommandRequest,
        state: CampaignState,
    ) -> tuple[CampaignEvent, ...]:
        if (
            state.phase is CampaignPhase.AWAITING_SPEC_APPROVAL
            and state.spec_sha256 is not None
        ):
            approval = self._verified_spec_approval(
                request.repository_root,
                request.issue_number,
                state.spec_sha256,
            )
            if approval is not None:
                return (
                    CampaignEvent(
                        event_id=_event_id(
                            EventKind.SPEC_HUMAN_APPROVED,
                            state.generation,
                            approval.approval_commit,
                        ),
                        kind=EventKind.SPEC_HUMAN_APPROVED,
                        generation=state.generation,
                        occurred_at=self._clock(),
                    ),
                )
        return ()

    def _current_generation(
        self,
        state: CampaignState,
        request: OptimizeCommandRequest,
        key: str,
        digest: str,
    ) -> bool:
        return (
            self._artifact_generation(request, key, digest)
            == state.generation
        )


def _failed(status: StewardAdvanceStatus) -> bool:
    return status in {
            StewardAdvanceStatus.BLOCKED,
            StewardAdvanceStatus.CONFLICT,
            StewardAdvanceStatus.FAILED,
        }


_ALLOWED_PHASES = {
    OptimizePhase.AUTO: frozenset(
        {
            CampaignPhase.SPECIFICATION,
            CampaignPhase.BASELINE,
            CampaignPhase.CANDIDATES,
            CampaignPhase.DEPLOYMENT,
            CampaignPhase.RETENTION,
        }
    ),
    OptimizePhase.SPEC: frozenset({CampaignPhase.SPECIFICATION}),
    OptimizePhase.RUN: frozenset(
        {CampaignPhase.BASELINE, CampaignPhase.CANDIDATES}
    ),
    OptimizePhase.CANDIDATE_REQUEST: frozenset(
        {CampaignPhase.CANDIDATES}
    ),
    OptimizePhase.CANDIDATE_SUBMIT: frozenset(
        {CampaignPhase.CANDIDATES}
    ),
    OptimizePhase.APPLY: frozenset(
        {CampaignPhase.AWAITING_SELECTION}
    ),
    OptimizePhase.RECONCILE: frozenset(
        {
            CampaignPhase.DEPLOYMENT,
            CampaignPhase.RETENTION,
            CampaignPhase.COMPLETED,
            CampaignPhase.BLOCKED,
        }
    ),
}
_WAITING_PHASES = frozenset(
    {
        CampaignPhase.AWAITING_SPEC_APPROVAL,
        CampaignPhase.AWAITING_SELECTION,
        CampaignPhase.CANCELLED,
    }
)


def _gate(
    request: OptimizeCommandRequest,
    state: CampaignState,
) -> OptimizeCommandResult | None:
    if state.phase in _ALLOWED_PHASES[request.phase]:
        return None
    if state.phase in _WAITING_PHASES:
        code = "campaign_phase_waiting"
    elif state.phase is CampaignPhase.BLOCKED:
        code = "campaign_blocked"
    elif state.phase is CampaignPhase.COMPLETED:
        code = "campaign_complete"
    else:
        code = "campaign_phase_incompatible"
    return OptimizeCommandResult(
        status=OptimizeCommandStatus.BLOCKED,
        phase=request.phase,
        summary=(
            f"The canonical campaign is in {state.phase.value}; "
            f"{request.phase.value} cannot run."
        ),
        issue_number=request.issue_number,
        details={
            "code": code,
            "generation": state.generation,
            "phase": state.phase.value,
        },
    )


def _canonical_candidate_result(
    request: OptimizeCommandRequest,
    steward_result: Any,
) -> OptimizeCommandResult | None:
    if request.phase not in {
        OptimizePhase.RUN,
        OptimizePhase.CANDIDATE_REQUEST,
        OptimizePhase.CANDIDATE_SUBMIT,
    }:
        return None
    state = steward_result.state
    if state is None:
        return None
    if steward_result.code == "session_timeout":
        return OptimizeCommandResult(
            OptimizeCommandStatus.AWAITING_AGENT,
            request.phase,
            steward_result.summary,
            request.issue_number,
            details={
                "generation": state.generation,
                "phase": state.phase.value,
                "source": "canonical_steward",
            },
            next_action="A replacement steward session will resume the ledger.",
        )
    completed = any(
        event_id.startswith(
            f"candidate-workers-{state.generation}-"
        )
        for event_id in state.processed_event_ids
    )
    if not completed:
        return None
    return OptimizeCommandResult(
        OptimizeCommandStatus.COMPLETE,
        request.phase,
        steward_result.summary,
        request.issue_number,
        details={
            "candidate_count": len(state.candidates),
            "generation": state.generation,
            "phase": state.phase.value,
            "source": "canonical_steward",
        },
    )


def _canonical_selection_result(
        request: OptimizeCommandRequest,
        steward_result: Any,
) -> OptimizeCommandResult | None:
    state = steward_result.state
    if (
        request.phase is not OptimizePhase.RECONCILE
        or state is None
    ):
        return None
    if state.phase is CampaignPhase.COMPLETED:
        return OptimizeCommandResult(
            OptimizeCommandStatus.COMPLETE,
            request.phase,
            (
                "The canonical steward verified deployment and retained "
                "improvement; cleanup and root closure were durably planned."
            ),
            request.issue_number,
            details={
                "candidate_id": state.selected_candidate_id,
                "deployment_version": state.deployment_version,
                "generation": state.generation,
                "merge_commit": state.merge_commit,
                "phase": state.phase.value,
                "source": "canonical_steward",
            },
        )
    if state.phase is CampaignPhase.BLOCKED:
        return OptimizeCommandResult(
            OptimizeCommandStatus.BLOCKED,
            request.phase,
            (
                "The canonical steward left the root issue open for human "
                "remediation."
            ),
            request.issue_number,
            details={
                "code": state.block_reason or "deployment_blocked",
                "generation": state.generation,
                "phase": state.phase.value,
                "source": "canonical_steward",
            },
            next_action=(
                "Inspect the redacted deployment dashboard and retry only "
                "after resolving the recorded reason."
            ),
        )
    if state.phase not in {
        CampaignPhase.DEPLOYMENT,
        CampaignPhase.RETENTION,
    }:
        return None
    return OptimizeCommandResult(
        OptimizeCommandStatus.AWAITING_AGENT,
        request.phase,
        (
            "The canonical steward is reconciling the exact deployment and "
            "pinned held-out retained-improvement evaluation."
        ),
        request.issue_number,
        details={
            "generation": state.generation,
            "merge_commit": state.merge_commit,
            "phase": state.phase.value,
            "selected_candidate_id": state.selected_candidate_id,
            "source": "canonical_steward",
        },
        next_action=(
            "Wait for the steward and thin deployment bridge; this "
            "compatibility command does not own deployment transitions."
        ),
    )


def _gate_failure(
    request: OptimizeCommandRequest,
    code: str | None,
) -> OptimizeCommandResult:
    unavailable = code in {
        "campaign_not_initialized",
        "inbox_unavailable",
        "state_ref_unavailable",
    }
    return OptimizeCommandResult(
        status=(
            OptimizeCommandStatus.BLOCKED
            if unavailable
            else OptimizeCommandStatus.FAILED
        ),
        phase=request.phase,
        summary=(
            "The compatibility command was not started because its "
            "canonical campaign state was unavailable."
        ),
        issue_number=request.issue_number,
        details={"code": code or "orchestration_gate_failed"},
    )


def _sync_failure(
    request: OptimizeCommandRequest,
    code: str | None,
) -> OptimizeCommandResult:
    return OptimizeCommandResult(
        status=OptimizeCommandStatus.FAILED,
        phase=request.phase,
        summary=(
            "The compatibility command completed, but its durable "
            "campaign state was not persisted."
        ),
        issue_number=request.issue_number,
        details={"code": code or "orchestration_sync_failed"},
    )


def _load_spec_sha256(
    repository_root: Path,
    issue_number: int,
) -> str | None:
    path = repository_root / spec_file_path(issue_number)
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    spec = OptimizationSpec.model_validate(document)
    return spec.sha256


def _load_verified_spec_approval(
    repository_root: Path,
    issue_number: int,
    expected_sha256: str,
) -> VerifiedSpecApproval | None:
    try:
        default_branch = _command(
            repository_root,
            "gh",
            "repo",
            "view",
            "--json",
            "defaultBranchRef",
            "--jq",
            ".defaultBranchRef.name",
        )
        remote_ref = f"refs/remotes/origin/{default_branch}"
        _command(
            repository_root,
            "git",
            "fetch",
            "--quiet",
            "origin",
            f"{default_branch}:{remote_ref}",
        )
        default_commit = _command(
            repository_root,
            "git",
            "rev-parse",
            f"{remote_ref}^{{commit}}",
        )
        path = spec_file_path(issue_number).as_posix()
        content = _command(
            repository_root,
            "git",
            "show",
            f"{default_commit}:{path}",
        )
        spec = OptimizationSpec.model_validate(yaml.safe_load(content))
        if spec.sha256 != expected_sha256:
            return None
        approval_commit = _command(
            repository_root,
            "git",
            "rev-list",
            "-1",
            default_commit,
            "--",
            path,
        )
        return VerifiedSpecApproval(expected_sha256, approval_commit)
    except Exception:
        return None


def _legacy_artifacts(
    request: OptimizeCommandRequest,
) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    spec_sha256 = _load_spec_sha256(
        request.repository_root,
        request.issue_number,
    )
    if spec_sha256 is not None:
        artifacts["spec"] = spec_sha256
    campaign_id = f"issue-{request.issue_number}"
    campaign = FileCampaignStateStore().load(
        request.repository_root,
        campaign_id,
    )
    if campaign is not None:
        baseline = campaign.baseline_development
        if baseline is not None:
            artifacts["baseline"] = _semantic_digest(
                "baseline",
                campaign.spec_sha256,
                baseline.run.evaluation_id,
            )
        for candidate in campaign.candidates:
            artifact = candidate.artifact
            if artifact is None:
                continue
            evidence = (
                request.repository_root / artifact.evidence_path
            )
            if not evidence.is_file():
                continue
            evidence_sha256 = hashlib.sha256(
                evidence.read_bytes()
            ).hexdigest()
            artifacts[f"candidate:{candidate.candidate_id}"] = (
                _semantic_digest(
                    "candidate",
                    campaign.spec_sha256,
                    candidate.candidate_id,
                    str(artifact.eligible),
                    evidence_sha256,
                )
            )
        if campaign.finalized is not None:
            artifacts["slate"] = _slate_digest(campaign)
    lifecycle = FileLifecycleStateStore().load(
        request.repository_root,
        campaign_id,
    )
    if lifecycle is not None:
        if (
            lifecycle.selected_candidate_id is not None
            and lifecycle.merge_commit is not None
        ):
            artifacts["lifecycle:merge"] = _semantic_digest(
                "merge",
                lifecycle.selected_candidate_id,
                lifecycle.merge_commit,
            )
        if lifecycle.deployment_version is not None:
            artifacts["lifecycle:deployment"] = _semantic_digest(
                "deployment",
                str(lifecycle.deployment_version),
            )
        if lifecycle.post_deploy_retained:
            artifacts["lifecycle:retention"] = _semantic_digest(
                "retention",
                "true",
            )
    return artifacts


def _slate_digest(campaign: Any) -> str:
    return _semantic_digest(
        "slate",
        campaign.spec_sha256,
        *(
            candidate.candidate_id
            for candidate in campaign.candidates
            if candidate.artifact is not None
        ),
    )


def _semantic_digest(*values: str) -> str:
    return hashlib.sha256(
        "\0".join(values).encode("utf-8")
    ).hexdigest()


def _fence_path(request: OptimizeCommandRequest) -> Path:
    root = request.repository_root.expanduser().resolve()
    path = (
        root
        / ".foundry-optimizer"
        / "compatibility"
        / f"issue-{request.issue_number}-generations.json"
    )
    if not path.resolve().is_relative_to(root):
        raise ValueError("legacy generation fence escapes repository")
    return path


def _runtime_marker_path(request: OptimizeCommandRequest) -> Path:
    root = request.repository_root.expanduser().resolve()
    path = (
        root
        / ".foundry-optimizer"
        / "compatibility"
        / "runtime"
        / f"issue-{request.issue_number}.json"
    )
    if not path.resolve().is_relative_to(root):
        raise ValueError("legacy runtime namespace escapes repository")
    return path


def _atomic_json(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}-{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(
                document,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _command(repository_root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        arguments,
        cwd=repository_root,
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError("approval verification command failed")
    return completed.stdout.strip()


def _is_sha256(value: str) -> bool:
    return (
        len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_commit(value: str) -> bool:
    return (
        len(value) == 40
        and all(character in "0123456789abcdef" for character in value)
    )


def _event_id(
    kind: EventKind,
    generation: int,
    identity: str,
) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return f"compat-{generation}-{kind.value}-{digest}"
