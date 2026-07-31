from __future__ import annotations

from dataclasses import replace
from typing import Any

from foundry_opt.orchestration.models import (
    AdvanceDisposition,
    AdvanceRequest,
    AdvanceResult,
    CampaignEvent,
    CampaignPhase,
    CampaignState,
    CandidateRecord,
    EventKind,
    SpecFileHash,
)


class InvalidCampaignTransition(ValueError):
    pass


class OptimizationCampaign:
    """Pure, replayable state machine executed by the Copilot steward."""

    def advance(self, request: AdvanceRequest) -> AdvanceResult:
        state = request.state
        changed = False
        for event in request.events:
            if state is not None and event.event_id in (
                state.processed_event_ids
            ):
                continue
            if state is not None and event.generation < state.generation:
                continue
            state = self._apply(request.issue_number, state, event)
            changed = True
        if state is None:
            raise InvalidCampaignTransition(
                "the first event must create a campaign"
            )
        return AdvanceResult(
            state,
            (
                _disposition(state.phase)
                if changed
                else AdvanceDisposition.WAIT
            ),
        )

    def _apply(
        self,
        issue_number: int,
        state: CampaignState | None,
        event: CampaignEvent,
    ) -> CampaignState:
        if state is None:
            if (
                event.kind is not EventKind.ISSUE_CREATED
                or event.generation != 1
            ):
                raise InvalidCampaignTransition(
                    "the first event must be issue_created generation 1"
                )
            return CampaignState(
                issue_number=issue_number,
                generation=1,
                sequence=1,
                phase=CampaignPhase.SPECIFICATION,
                processed_event_ids=(event.event_id,),
            )

        if (
            event.generation > state.generation
            and event.kind not in {
                EventKind.ISSUE_EDITED,
                EventKind.ISSUE_REOPENED,
            }
        ):
            raise InvalidCampaignTransition(
                "future-generation events require issue supersession"
            )
        if event.kind is EventKind.ISSUE_CREATED:
            if state.generation == event.generation == 1:
                return self._next(state, event)
            raise InvalidCampaignTransition(
                "issue_created is only valid for generation 1"
            )
        if event.kind is EventKind.ISSUE_REOPENED:
            if (
                state.phase
                not in {
                    CampaignPhase.CANCELLED,
                    CampaignPhase.COMPLETED,
                    CampaignPhase.BLOCKED,
                }
                or event.generation != state.generation + 1
            ):
                raise InvalidCampaignTransition(
                    "issue_reopened requires a closed terminal generation"
                )
            return CampaignState(
                issue_number=state.issue_number,
                generation=event.generation,
                sequence=state.sequence + 1,
                phase=CampaignPhase.SPECIFICATION,
                processed_event_ids=(
                    *state.processed_event_ids,
                    event.event_id,
                ),
            )
        if event.kind is EventKind.ISSUE_EDITED:
            if event.generation > state.generation + 1:
                raise InvalidCampaignTransition(
                    "issue_edited cannot skip a generation"
                )
            if state.phase in {
                CampaignPhase.COMPLETED,
                CampaignPhase.BLOCKED,
                CampaignPhase.CANCELLED,
            }:
                raise InvalidCampaignTransition(
                    "terminal campaigns require an explicit new issue"
                )
            return CampaignState(
                issue_number=state.issue_number,
                generation=max(
                    state.generation + 1,
                    event.generation,
                ),
                sequence=state.sequence + 1,
                phase=CampaignPhase.SPECIFICATION,
                processed_event_ids=(
                    *state.processed_event_ids,
                    event.event_id,
                ),
            )
        if event.kind is EventKind.ISSUE_DECLASSIFIED:
            if event.generation != state.generation:
                raise InvalidCampaignTransition(
                    "issue_declassified must cancel the current generation"
                )
            if state.phase in {
                CampaignPhase.COMPLETED,
                CampaignPhase.BLOCKED,
                CampaignPhase.CANCELLED,
            }:
                return self._next(state, event)
            return self._next(
                state,
                event,
                phase=CampaignPhase.CANCELLED,
            )
        if event.kind is EventKind.ISSUE_CLOSED:
            if state.phase in {
                CampaignPhase.COMPLETED,
                CampaignPhase.BLOCKED,
                CampaignPhase.CANCELLED,
            }:
                return self._next(state, event)
            return self._next(
                state,
                event,
                phase=CampaignPhase.CANCELLED,
            )

        if event.kind is EventKind.SPEC_POLICY_APPROVED:
            self._require_phase(state, CampaignPhase.SPECIFICATION, event)
            return self._next(
                state,
                event,
                phase=CampaignPhase.BASELINE,
                spec_sha256=_sha(event.payload, "spec_sha256"),
            )
        if event.kind is EventKind.SPEC_REVIEW_REQUIRED:
            recovering_legacy_review = (
                state.phase is CampaignPhase.AWAITING_SPEC_APPROVAL
                and (
                    state.spec_base_ref_name is None
                    or state.spec_head_commit is None
                    or state.spec_tree_sha is None
                    or not state.spec_files
                )
            )
            if not recovering_legacy_review:
                self._require_phase(
                    state,
                    CampaignPhase.SPECIFICATION,
                    event,
                )
            materialization: dict[str, Any] = {}
            if set(event.payload) != {"spec_sha256"}:
                materialization = {
                    "spec_base_ref_name": _identifier(
                        event.payload, "base_ref_name"
                    ),
                    "spec_head_commit": _commit(
                        event.payload, "head_commit"
                    ),
                    "spec_tree_sha": _commit(
                        event.payload, "tree_sha"
                    ),
                    "spec_files": _spec_files(event.payload),
                }
            if recovering_legacy_review:
                if not materialization:
                    raise InvalidCampaignTransition(
                        "legacy spec recovery requires exact materialization"
                    )
            return self._next(
                state,
                event,
                phase=(
                    state.phase
                    if recovering_legacy_review
                    else CampaignPhase.AWAITING_SPEC_APPROVAL
                ),
                schema_version=(
                    2 if recovering_legacy_review else state.schema_version
                ),
                spec_sha256=_sha(event.payload, "spec_sha256"),
                **materialization,
            )
        if event.kind is EventKind.SPEC_HUMAN_APPROVED:
            self._require_phase(
                state,
                CampaignPhase.AWAITING_SPEC_APPROVAL,
                event,
            )
            if event.payload and (
                _sha(event.payload, "spec_sha256")
                != state.spec_sha256
            ):
                raise InvalidCampaignTransition(
                    "human approval does not match the pinned specification"
                )
            return self._next(
                state,
                event,
                phase=CampaignPhase.BASELINE,
            )
        if event.kind is EventKind.BASELINE_COMPLETED:
            self._require_phase(state, CampaignPhase.BASELINE, event)
            return self._next(
                state,
                event,
                phase=CampaignPhase.CANDIDATES,
                baseline_evaluation_id=_identifier(
                    event.payload, "evaluation_id"
                ),
            )
        if event.kind is EventKind.CANDIDATE_EVALUATED:
            self._require_phase(state, CampaignPhase.CANDIDATES, event)
            candidate = CandidateRecord(
                candidate_id=_identifier(
                    event.payload, "candidate_id"
                ),
                eligible=_boolean(event.payload, "eligible"),
                evidence_sha256=_sha(
                    event.payload, "evidence_sha256"
                ),
            )
            if candidate.candidate_id in {
                item.candidate_id for item in state.candidates
            }:
                raise InvalidCampaignTransition(
                    "candidate has already been evaluated"
                )
            return self._next(
                state,
                event,
                candidates=(*state.candidates, candidate),
            )
        if event.kind is EventKind.SLATE_PUBLISHED:
            self._require_phase(state, CampaignPhase.CANDIDATES, event)
            if not state.candidates:
                raise InvalidCampaignTransition(
                    "a candidate slate requires evaluated candidates"
                )
            if not any(item.eligible for item in state.candidates):
                return self._next(
                    state,
                    event,
                    phase=CampaignPhase.BLOCKED,
                    block_reason="no_eligible_candidates",
                )
            return self._next(
                state,
                event,
                phase=CampaignPhase.AWAITING_SELECTION,
            )
        if event.kind is EventKind.CANDIDATE_MERGED:
            self._require_phase(
                state,
                CampaignPhase.AWAITING_SELECTION,
                event,
            )
            candidate_id = _identifier(
                event.payload, "candidate_id"
            )
            selected = next(
                (
                    item
                    for item in state.candidates
                    if item.candidate_id == candidate_id
                ),
                None,
            )
            if selected is None or not selected.eligible:
                return self._next(
                    state,
                    event,
                    phase=CampaignPhase.BLOCKED,
                    block_reason="ineligible_candidate_merged",
                )
            return self._next(
                state,
                event,
                phase=CampaignPhase.DEPLOYMENT,
                selected_candidate_id=candidate_id,
                merge_commit=_commit(event.payload, "merge_commit"),
            )
        if event.kind is EventKind.DEPLOYMENT_COMPLETED:
            self._require_phase(state, CampaignPhase.DEPLOYMENT, event)
            version = event.payload.get("deployment_version")
            if (
                not isinstance(version, int)
                or isinstance(version, bool)
                or version < 1
            ):
                raise InvalidCampaignTransition(
                    "deployment_version must be positive"
                )
            return self._next(
                state,
                event,
                phase=CampaignPhase.RETENTION,
                deployment_version=version,
            )
        if event.kind is EventKind.RETENTION_COMPLETED:
            self._require_phase(state, CampaignPhase.RETENTION, event)
            retained = _boolean(event.payload, "retained")
            return self._next(
                state,
                event,
                phase=(
                    CampaignPhase.COMPLETED
                    if retained
                    else CampaignPhase.BLOCKED
                ),
                block_reason=(
                    None
                    if retained
                    else "retained_improvement_failed"
                ),
            )
        raise InvalidCampaignTransition(
            f"{event.kind.value} is not valid in {state.phase.value}"
        )

    def _next(
        self,
        state: CampaignState,
        event: CampaignEvent,
        **changes: Any,
    ) -> CampaignState:
        return replace(
            state,
            sequence=state.sequence + 1,
            processed_event_ids=(
                *state.processed_event_ids,
                event.event_id,
            ),
            **changes,
        )

    def _require_phase(
        self,
        state: CampaignState,
        phase: CampaignPhase,
        event: CampaignEvent,
    ) -> None:
        if state.phase is not phase:
            raise InvalidCampaignTransition(
                f"{event.kind.value} requires {phase.value}"
            )


def _identifier(payload: Any, field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise InvalidCampaignTransition(f"{field} is required")
    return value


def _sha(payload: Any, field: str) -> str:
    value = _identifier(payload, field)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise InvalidCampaignTransition(f"{field} must be a SHA-256 digest")
    return value


def _spec_files(payload: Any) -> tuple[SpecFileHash, ...]:
    value = payload.get("files")
    if not isinstance(value, list) or not value:
        raise InvalidCampaignTransition("files must be a non-empty list")
    try:
        return tuple(SpecFileHash(**item) for item in value)
    except (TypeError, ValueError) as error:
        raise InvalidCampaignTransition(
            "files must contain pinned paths and hashes"
        ) from error


def _commit(payload: Any, field: str) -> str:
    value = _identifier(payload, field)
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise InvalidCampaignTransition(f"{field} must be a full Git commit")
    return value


def _boolean(payload: Any, field: str) -> bool:
    value = payload.get(field)
    if not isinstance(value, bool):
        raise InvalidCampaignTransition(f"{field} must be boolean")
    return value


def _disposition(phase: CampaignPhase) -> AdvanceDisposition:
    if phase is CampaignPhase.COMPLETED:
        return AdvanceDisposition.COMPLETE
    if phase is CampaignPhase.BLOCKED:
        return AdvanceDisposition.BLOCKED
    if phase in {
        CampaignPhase.AWAITING_SPEC_APPROVAL,
        CampaignPhase.AWAITING_SELECTION,
        CampaignPhase.CANCELLED,
    }:
        return AdvanceDisposition.WAIT
    return AdvanceDisposition.ADVANCE
