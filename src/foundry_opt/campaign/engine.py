from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Protocol

from foundry_opt.campaign.lineage import IdeaLineage
from foundry_opt.campaign.models import CampaignReport, CandidateArtifact
from foundry_opt.campaign.protocols import (
    BundleBuilder,
    CampaignRepository,
    CampaignRequest,
    CampaignStateError,
    CandidateContext,
    CandidateFeedback,
    CandidateGenerator,
    Clock,
    DraftCreator,
    EvaluationRunner,
    EvidenceWriter,
    TransientCandidateError,
    ValidationRunner,
)
from foundry_opt.campaign.state import (
    CampaignState,
    CampaignStateStore,
    CandidateState,
)
from foundry_opt.evaluation import (
    AgentVersionRef,
    DatasetSplit,
    EvaluationResult,
    EvaluationSubject,
    select_eligible_candidates,
)
from foundry_opt.evidence import EvidenceRequest


@dataclass(frozen=True)
class CampaignDependencies:
    repository: CampaignRepository
    generator: CandidateGenerator
    validate: ValidationRunner
    build_bundle: BundleBuilder
    create_draft: DraftCreator
    evaluate: EvaluationRunner
    write_evidence: EvidenceWriter
    state: CampaignStateStore
    clock: Clock


def run_campaign(
    request: CampaignRequest,
    dependencies: CampaignDependencies,
) -> CampaignReport:
    tracked = _TrackedRepository(dependencies.repository)
    guarded = replace(dependencies, repository=tracked)
    try:
        return _run_campaign(request, guarded)
    except CampaignStateError:
        if tracked.acquired:
            tracked.release()
        raise
    except Exception as error:
        if tracked.acquired:
            root = request.repository_root.expanduser().resolve()
            state = dependencies.state.load(root, request.campaign_id)
            if state is not None and state.status == "active":
                dependencies.state.save(
                    root,
                    replace(
                        state,
                        status="failed",
                        updated_at=dependencies.clock.now(),
                        error_code=type(error).__name__,
                    ),
                )
            tracked.release()
        raise


def _run_campaign(
    request: CampaignRequest,
    dependencies: CampaignDependencies,
) -> CampaignReport:
    root = request.repository_root.expanduser().resolve()
    pinned = dependencies.repository.pin_default_branch(root)
    lock = dependencies.repository.acquire_lock(
        repository_root=root,
        target=request.target,
        campaign_id=request.campaign_id,
        base_commit=pinned.commit,
        now=dependencies.clock.now(),
        stale_after=request.stale_after,
    )
    if lock.recovered_campaign_id is not None:
        dependencies.state.mark_stale(
            root,
            lock.recovered_campaign_id,
            dependencies.clock.now(),
        )

    state = dependencies.state.load(root, request.campaign_id)
    if state is not None and (
        state.target != request.target or state.base_commit != pinned.commit
    ):
        state = replace(
            state,
            status="failed",
            updated_at=dependencies.clock.now(),
            error_code="campaign_state_mismatch",
        )
        dependencies.state.save(root, state)
        dependencies.repository.release_lock(
            repository_root=root,
            target=request.target,
            campaign_id=request.campaign_id,
        )
        raise CampaignStateError(state)
    if state is not None and state.status == "completed":
        dependencies.repository.release_lock(
            repository_root=root,
            target=request.target,
            campaign_id=request.campaign_id,
        )
        return _report_from_state(state)
    if state is not None and state.status in {"failed", "stale"}:
        dependencies.repository.release_lock(
            repository_root=root,
            target=request.target,
            campaign_id=request.campaign_id,
        )
        raise CampaignStateError(state)
    if state is not None and state.status == "active":
        state = replace(
            state,
            status="stale",
            updated_at=dependencies.clock.now(),
            error_code="orphaned_active_state",
        )
        dependencies.state.save(root, state)
        dependencies.repository.release_lock(
            repository_root=root,
            target=request.target,
            campaign_id=request.campaign_id,
        )
        raise CampaignStateError(state)

    started = dependencies.clock.now()
    state = state or CampaignState(
        campaign_id=request.campaign_id,
        target=request.target,
        base_commit=pinned.commit,
        status="active",
        started_at=started,
        updated_at=started,
    )
    dependencies.state.save(root, state)
    baseline_worktree = None
    try:
        baseline_worktree = dependencies.repository.create_worktree(
            root,
            request.campaign_id,
            "baseline",
            pinned.commit,
        )
        baseline_bundle = dependencies.build_bundle(
            baseline_worktree.path,
            baseline_worktree.path / ".foundry-opt-baseline.zip",
        )
        baseline_draft = dependencies.create_draft(
            request.target,
            "baseline",
            baseline_bundle,
        )
        baseline_subject = _subject(
            request.target,
            "baseline",
            baseline_draft.version_id,
        )
        baseline_development = _evaluate(
            dependencies,
            baseline_subject,
            DatasetSplit.DEVELOPMENT,
        )
        state = replace(
            state,
            baseline_draft_id=baseline_draft.version_id,
            baseline_metrics=_metrics(baseline_development),
            updated_at=dependencies.clock.now(),
        )
        dependencies.state.save(root, state)
    finally:
        if baseline_worktree is not None:
            dependencies.repository.cleanup_worktree(root, baseline_worktree)

    development_results: list[EvaluationResult] = []
    feedback: list[CandidateFeedback] = []
    for slot in range(state.launched_slots + 1, request.limits.max_changed_candidates + 1):
        elapsed = dependencies.clock.now() - state.started_at
        if elapsed.total_seconds() >= request.limits.candidate_cutoff_minutes * 60:
            break
        candidate_id = f"candidate-{slot}"
        candidate_started = dependencies.clock.now()
        timings: dict[str, float] = {}
        state = replace(
            state,
            launched_slots=slot,
            candidates=(
                *state.candidates,
                CandidateState(candidate_id, slot, "launched", attempts=1),
            ),
            updated_at=dependencies.clock.now(),
        )
        dependencies.state.save(root, state)
        worktree = dependencies.repository.create_worktree(
            root,
            request.campaign_id,
            candidate_id,
            pinned.commit,
        )
        idea = None
        try:
            context = CandidateContext(
                campaign_id=request.campaign_id,
                target=request.target,
                candidate_id=candidate_id,
                slot=slot,
                worktree=worktree.path,
                base_commit=pinned.commit,
                edit_paths=request.edit_paths,
                allowed_mutations=request.allowed_mutations,
                restricted_opt_ins=request.restricted_opt_ins,
                baseline_metrics=state.baseline_metrics,
                history=tuple(feedback),
            )
            generation_started = dependencies.clock.now()
            try:
                idea = dependencies.generator.generate(context)
            except TransientCandidateError:
                if (
                    state.transient_retries_used
                    >= request.limits.transient_retries
                ):
                    state = _replace_candidate(
                        state,
                        candidate_id,
                        status="transient_failed",
                        error_code="transient_retry_exhausted",
                        timings={
                            "generation_seconds": _seconds(
                                dependencies.clock.now() - generation_started
                            ),
                            "total_seconds": _seconds(
                                dependencies.clock.now() - candidate_started
                            ),
                        },
                    )
                    dependencies.state.save(root, state)
                    continue
                dependencies.repository.reset_worktree(worktree)
                state = replace(
                    _replace_candidate(
                        state,
                        candidate_id,
                        attempts=2,
                    ),
                    transient_retries_used=state.transient_retries_used + 1,
                    updated_at=dependencies.clock.now(),
                )
                dependencies.state.save(root, state)
                try:
                    idea = dependencies.generator.generate(context)
                except TransientCandidateError:
                    state = _replace_candidate(
                        state,
                        candidate_id,
                        status="transient_failed",
                        error_code="transient_retry_exhausted",
                        timings={
                            "generation_seconds": _seconds(
                                dependencies.clock.now() - generation_started
                            ),
                            "total_seconds": _seconds(
                                dependencies.clock.now() - candidate_started
                            ),
                        },
                    )
                    dependencies.state.save(root, state)
                    continue
            timings["generation_seconds"] = _seconds(
                dependencies.clock.now() - generation_started
            )
            changed_paths = dependencies.repository.changed_paths(worktree)
            lineage = IdeaLineage(
                idea.idea_id,
                idea.parent_idea_ids,
                idea.mutation_class,
                changed_paths,
            )
            if _deadline_reached(request, state, dependencies):
                state = _replace_candidate(
                    state,
                    candidate_id,
                    status="deadline_exceeded",
                    lineage=lineage,
                    error_code="campaign_deadline_exceeded",
                    timings={
                        **timings,
                        "total_seconds": _seconds(
                            dependencies.clock.now() - candidate_started
                        ),
                    },
                )
                feedback.append(
                    CandidateFeedback(
                        candidate_id,
                        idea.idea_id,
                        "deadline_exceeded",
                    )
                )
                dependencies.state.save(root, state)
                continue
            try:
                if not set(idea.parent_idea_ids).issubset(
                    item.idea_id for item in feedback
                ):
                    raise ValueError("idea lineage references an unknown parent")
                _enforce_mutation(
                    request,
                    idea.mutation_class,
                    idea.required_opt_ins,
                )
                _enforce_paths(request.edit_paths, changed_paths)
            except ValueError:
                state = _replace_candidate(
                    state,
                    candidate_id,
                    status="guardrail_rejected",
                    lineage=lineage,
                    error_code="mutation_guardrail_rejected",
                    timings={
                        **timings,
                        "total_seconds": _seconds(
                            dependencies.clock.now() - candidate_started
                        ),
                    },
                )
                feedback.append(
                    CandidateFeedback(
                        candidate_id,
                        idea.idea_id,
                        "guardrail_rejected",
                    )
                )
                dependencies.state.save(root, state)
                continue
            if not changed_paths:
                state = _replace_candidate(
                    state,
                    candidate_id,
                    status="unchanged",
                    lineage=lineage,
                    timings={
                        **timings,
                        "total_seconds": _seconds(
                            dependencies.clock.now() - candidate_started
                        ),
                    },
                )
                feedback.append(
                    CandidateFeedback(
                        candidate_id,
                        idea.idea_id,
                        "unchanged",
                    )
                )
                dependencies.state.save(root, state)
                continue
            validation_started = dependencies.clock.now()
            validation = dependencies.validate(worktree.path)
            timings["validation_seconds"] = _seconds(
                dependencies.clock.now() - validation_started
            )
            if not validation.passed:
                state = _replace_candidate(
                    state,
                    candidate_id,
                    status="validation_failed",
                    lineage=lineage,
                    error_code="validation_failed",
                    timings={
                        **timings,
                        "total_seconds": _seconds(
                            dependencies.clock.now() - candidate_started
                        ),
                    },
                )
                feedback.append(
                    CandidateFeedback(
                        candidate_id,
                        idea.idea_id,
                        "validation_failed",
                    )
                )
                dependencies.state.save(root, state)
                continue
            if _deadline_reached(request, state, dependencies):
                state = _replace_candidate(
                    state,
                    candidate_id,
                    status="deadline_exceeded",
                    lineage=lineage,
                    error_code="campaign_deadline_exceeded",
                    timings={
                        **timings,
                        "total_seconds": _seconds(
                            dependencies.clock.now() - candidate_started
                        ),
                    },
                )
                dependencies.state.save(root, state)
                continue
            patch_started = dependencies.clock.now()
            result_commit = dependencies.repository.commit_worktree(
                worktree,
                f"foundry-opt candidate {candidate_id}",
            )
            patch = dependencies.repository.export_patch(
                root,
                request.campaign_id,
                worktree,
                result_commit,
            )
            timings["patch_seconds"] = _seconds(
                dependencies.clock.now() - patch_started
            )
            bundle_started = dependencies.clock.now()
            bundle = dependencies.build_bundle(
                worktree.path,
                worktree.path / f".foundry-opt-{candidate_id}.zip",
            )
            timings["bundle_seconds"] = _seconds(
                dependencies.clock.now() - bundle_started
            )
            if _deadline_reached(request, state, dependencies):
                state = _replace_candidate(
                    state,
                    candidate_id,
                    status="deadline_exceeded",
                    lineage=lineage,
                    error_code="campaign_deadline_exceeded",
                    timings={
                        **timings,
                        "total_seconds": _seconds(
                            dependencies.clock.now() - candidate_started
                        ),
                    },
                )
                dependencies.state.save(root, state)
                continue
            draft_started = dependencies.clock.now()
            draft = dependencies.create_draft(
                request.target,
                candidate_id,
                bundle,
            )
            timings["draft_seconds"] = _seconds(
                dependencies.clock.now() - draft_started
            )
            if _deadline_reached(request, state, dependencies):
                state = _replace_candidate(
                    state,
                    candidate_id,
                    status="deadline_exceeded",
                    lineage=lineage,
                    error_code="campaign_deadline_exceeded",
                    timings={
                        **timings,
                        "total_seconds": _seconds(
                            dependencies.clock.now() - candidate_started
                        ),
                    },
                )
                dependencies.state.save(root, state)
                continue
            evaluation_started = dependencies.clock.now()
            result = _evaluate(
                dependencies,
                _subject(request.target, candidate_id, draft.version_id),
                DatasetSplit.DEVELOPMENT,
            )
            timings["development_evaluation_seconds"] = _seconds(
                dependencies.clock.now() - evaluation_started
            )
            development_results.append(result)
            metrics = _metrics(result)
            artifact = CandidateArtifact(
                candidate_id=candidate_id,
                patch=patch,
                draft_id=draft.version_id,
                evidence_path=(
                    request.evidence_root
                    / request.campaign_id
                    / "development-evidence.json"
                ),
                eligible=False,
                metrics=metrics,
            )
            state = _replace_candidate(
                state,
                candidate_id,
                status="evaluated",
                lineage=lineage,
                artifact=artifact,
                metrics=metrics,
                timings={
                    **timings,
                    "total_seconds": _seconds(
                        dependencies.clock.now() - candidate_started
                    ),
                },
            )
            provisional = select_eligible_candidates(
                baseline_development,
                tuple(development_results),
                request.evaluation_policy,
            )
            feedback.append(
                CandidateFeedback(
                    candidate_id,
                    idea.idea_id,
                    "evaluated",
                    metrics,
                    candidate_id in provisional.eligible_ids,
                )
            )
            dependencies.state.save(root, state)
        except Exception as error:
            state = _replace_candidate(
                state,
                candidate_id,
                status="failed",
                error_code=type(error).__name__,
                timings={
                    **timings,
                    "total_seconds": _seconds(
                        dependencies.clock.now() - candidate_started
                    ),
                },
            )
            feedback.append(
                CandidateFeedback(
                    candidate_id,
                    idea.idea_id if idea is not None else candidate_id,
                    "failed",
                )
            )
            dependencies.state.save(root, state)
        finally:
            dependencies.repository.cleanup_worktree(root, worktree)

    development_pareto = select_eligible_candidates(
        baseline_development,
        tuple(development_results),
        request.evaluation_policy,
    )
    development_output = _repository_output(
        root,
        request.evidence_root
        / request.campaign_id
        / "development-evidence.json",
        "development evidence",
    )
    development_evidence = dependencies.write_evidence(
        EvidenceRequest(
            output_path=development_output,
            campaign_id=request.campaign_id,
            baseline=baseline_development,
            candidates=tuple(development_results),
            pareto=development_pareto,
            metric_policies=request.evaluation_policy,
            source_hash=baseline_bundle.sha256,
            patch_hashes={
                candidate.artifact.candidate_id: candidate.artifact.patch.sha256
                for candidate in state.candidates
                if candidate.artifact is not None
            },
        )
    )
    development_path = _repository_relative(
        root,
        development_evidence.path,
        "development evidence",
    )
    state = replace(
        state,
        candidates=tuple(
            replace(
                candidate,
                artifact=replace(
                    candidate.artifact,
                    evidence_path=development_path,
                ),
            )
            if candidate.artifact is not None
            else candidate
            for candidate in state.candidates
        ),
    )
    final_ids: tuple[str, ...] = ()
    validation_results: list[EvaluationResult] = []
    validation_timings: dict[str, float] = {}
    if (
        development_pareto.eligible_ids
        and (
            dependencies.clock.now() - state.started_at
        ).total_seconds()
        < request.limits.deadline_minutes * 60
    ):
        baseline_validation = _evaluate(
            dependencies,
            baseline_subject,
            DatasetSplit.VALIDATION,
        )
        by_id = {result.run.subject_id: result for result in development_results}
        for candidate_id in development_pareto.eligible_ids:
            development = by_id[candidate_id]
            validation_started = dependencies.clock.now()
            validation_results.append(
                _evaluate(
                    dependencies,
                    EvaluationSubject(candidate_id, development.run.agent),
                    DatasetSplit.VALIDATION,
                )
            )
            validation_timings[candidate_id] = _seconds(
                dependencies.clock.now() - validation_started
            )
        validation_pareto = select_eligible_candidates(
            baseline_validation,
            tuple(validation_results),
            request.evaluation_policy,
        )
        final_ids = validation_pareto.eligible_ids
        validation_output = _repository_output(
            root,
            request.evidence_root
            / request.campaign_id
            / "validation-evidence.json",
            "validation evidence",
        )
        validation_evidence = dependencies.write_evidence(
            EvidenceRequest(
                output_path=validation_output,
                campaign_id=request.campaign_id,
                baseline=baseline_validation,
                candidates=tuple(validation_results),
                pareto=validation_pareto,
                metric_policies=request.evaluation_policy,
                source_hash=baseline_bundle.sha256,
                patch_hashes={
                    candidate.artifact.candidate_id:
                    candidate.artifact.patch.sha256
                    for candidate in state.candidates
                    if (
                        candidate.artifact is not None
                        and candidate.candidate_id
                        in development_pareto.eligible_ids
                    )
                },
            )
        )
        validation_path = _repository_relative(
            root,
            validation_evidence.path,
            "validation evidence",
        )
        state = replace(
            state,
            candidates=tuple(
                replace(
                    candidate,
                    artifact=replace(
                        candidate.artifact,
                        evidence_path=(
                            validation_path
                            if candidate.candidate_id
                            in development_pareto.eligible_ids
                            else development_path
                        ),
                        eligible=candidate.candidate_id in final_ids,
                        metrics=(
                            _metrics(next(
                                result
                                for result in validation_results
                                if result.run.subject_id
                                == candidate.candidate_id
                            ))
                            if candidate.candidate_id
                            in development_pareto.eligible_ids
                            else candidate.artifact.metrics
                        ),
                    ),
                    timings={
                        **candidate.timings,
                        **(
                            {
                                "held_out_evaluation_seconds":
                                validation_timings[candidate.candidate_id]
                            }
                            if candidate.candidate_id in validation_timings
                            else {}
                        ),
                    },
                )
                if candidate.artifact is not None
                else candidate
                for candidate in state.candidates
            ),
        )
    state = replace(
        state,
        status="completed",
        pareto_candidate_ids=final_ids,
        updated_at=dependencies.clock.now(),
    )
    dependencies.state.save(root, state)
    dependencies.repository.release_lock(
        repository_root=root,
        target=request.target,
        campaign_id=request.campaign_id,
    )
    return _report_from_state(state)


class _TrackedRepository:
    def __init__(self, repository: CampaignRepository) -> None:
        self._repository = repository
        self.acquired = False
        self._release_arguments: dict[str, object] | None = None

    def acquire_lock(self, **arguments: object):
        lock = self._repository.acquire_lock(**arguments)
        self.acquired = True
        self._release_arguments = {
            "repository_root": arguments["repository_root"],
            "target": arguments["target"],
            "campaign_id": arguments["campaign_id"],
        }
        return lock

    def release_lock(self, **arguments: object) -> None:
        self._repository.release_lock(**arguments)
        self.acquired = False
        self._release_arguments = None

    def release(self) -> None:
        if self.acquired and self._release_arguments is not None:
            self.release_lock(**self._release_arguments)

    def __getattr__(self, name: str):
        return getattr(self._repository, name)


def _subject(target: str, subject_id: str, draft_id: str) -> EvaluationSubject:
    return EvaluationSubject(
        subject_id,
        AgentVersionRef(target, draft_id, draft_id),
    )


def _evaluate(
    dependencies: CampaignDependencies,
    subject: EvaluationSubject,
    split: DatasetSplit,
) -> EvaluationResult:
    result = dependencies.evaluate(subject, split, 1)
    if result.needs_repeat:
        result = dependencies.evaluate(subject, split, 2)
    return result


def _metrics(result: EvaluationResult) -> dict[str, float]:
    return {
        name: aggregate.median
        for name, aggregate in result.metrics.items()
        if aggregate.median is not None
    }


def _seconds(delta) -> float:
    return max(0.0, delta.total_seconds())


def _deadline_reached(
    request: CampaignRequest,
    state: CampaignState,
    dependencies: CampaignDependencies,
) -> bool:
    return (
        dependencies.clock.now() - state.started_at
    ).total_seconds() >= request.limits.deadline_minutes * 60


def _repository_relative(
    repository_root: Path,
    path: Path,
    label: str,
) -> Path:
    candidate = path if path.is_absolute() else repository_root / path
    resolved = candidate.expanduser().resolve()
    if not resolved.is_relative_to(repository_root):
        raise ValueError(f"{label} path escapes repository")
    return resolved.relative_to(repository_root)


def _repository_output(
    repository_root: Path,
    relative_path: Path,
    label: str,
) -> Path:
    output = repository_root / relative_path
    resolved_parent = output.parent.resolve()
    if not resolved_parent.is_relative_to(repository_root):
        raise ValueError(f"{label} path escapes repository")
    output.parent.mkdir(parents=True, exist_ok=True)
    if not output.parent.resolve().is_relative_to(repository_root):
        raise ValueError(f"{label} path escapes repository")
    return output


def _enforce_mutation(
    request: CampaignRequest,
    mutation_class: str,
    required_opt_ins: frozenset[str],
) -> None:
    if mutation_class not in request.allowed_mutations:
        raise ValueError(f"mutation class is not allowed: {mutation_class}")
    missing = tuple(
        opt_in
        for opt_in in required_opt_ins
        if not request.restricted_opt_ins.get(opt_in, False)
    )
    if missing:
        raise ValueError(
            "candidate requires disabled opt-ins: " + ", ".join(sorted(missing))
        )


def _enforce_paths(
    allowed_roots: tuple[Path, ...],
    changed_paths: tuple[Path, ...],
) -> None:
    for changed in changed_paths:
        normalized = Path(str(changed).replace("\\", "/"))
        if not any(
            normalized == root or normalized.is_relative_to(root)
            for root in allowed_roots
        ):
            raise ValueError(f"candidate changed disallowed path: {changed}")


def _replace_candidate(
    state: CampaignState,
    candidate_id: str,
    **changes: object,
) -> CampaignState:
    return replace(
        state,
        candidates=tuple(
            replace(candidate, **changes)
            if candidate.candidate_id == candidate_id
            else candidate
            for candidate in state.candidates
        ),
    )


def _report_from_state(state: CampaignState) -> CampaignReport:
    if state.baseline_draft_id is None:
        raise RuntimeError("campaign has no baseline draft")
    return CampaignReport(
        campaign_id=state.campaign_id,
        target=state.target,
        base_commit=state.base_commit,
        baseline_draft_id=state.baseline_draft_id,
        candidates=tuple(
            candidate.artifact
            for candidate in state.candidates
            if candidate.artifact is not None
        ),
        pareto_candidate_ids=state.pareto_candidate_ids,
    )
