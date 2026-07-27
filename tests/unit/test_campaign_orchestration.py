from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from foundry_opt.campaign.engine import CampaignDependencies, run_campaign
from foundry_opt.campaign.protocols import (
    CampaignLock,
    CampaignRequest,
    CampaignWorktree,
    CandidateIdea,
    PinnedRepository,
)
from foundry_opt.campaign.state import MemoryCampaignStateStore
from foundry_opt.campaign.state import CampaignState
from foundry_opt.campaign.models import CampaignLimits
from foundry_opt.drafts import DraftRecord
from foundry_opt.evaluation import (
    AgentVersionRef,
    DatasetSplit,
    DatasetVersionRef,
    EvaluationPolicy,
    EvaluationResult,
    EvaluationRun,
    EvaluationStatus,
    EvaluationSubject,
    EvaluatorDefinitionRef,
    MetricDirection,
    MetricPolicy,
    NormalizedCase,
    NormalizedCaseMetric,
    Outcome,
    Usage,
)
from foundry_opt.evidence import EvidenceManifest
from foundry_opt.packaging import (
    BundleArtifact,
    ValidationReport,
    ValidationResult,
)


BASE_COMMIT = "a" * 40


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 26, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, *, minutes: int) -> None:
        self.current += timedelta(minutes=minutes)


class FakeRepository:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.created: list[str] = []
        self.cleaned: list[str] = []
        self.reset: list[str] = []
        self.released = 0
        self.changed: dict[str, tuple[Path, ...]] = {}

    def pin_default_branch(self, repository_root: Path) -> PinnedRepository:
        assert repository_root == self.root
        return PinnedRepository("main", BASE_COMMIT)

    def acquire_lock(self, **kwargs: object) -> CampaignLock:
        return CampaignLock("campaign-1")

    def release_lock(self, **kwargs: object) -> None:
        self.released += 1

    def create_worktree(
        self,
        repository_root: Path,
        campaign_id: str,
        candidate_id: str,
        base_commit: str,
    ) -> CampaignWorktree:
        self.created.append(candidate_id)
        path = self.root / ".foundry-optimizer" / "worktrees" / candidate_id
        path.mkdir(parents=True)
        return CampaignWorktree(
            candidate_id,
            path,
            f"foundry-opt/{campaign_id}/{candidate_id}",
            base_commit,
        )

    def changed_paths(
        self,
        worktree: CampaignWorktree,
    ) -> tuple[Path, ...]:
        return self.changed.get(
            worktree.candidate_id,
            (Path("agent/instructions.md"),),
        )

    def reset_worktree(self, worktree: CampaignWorktree) -> None:
        self.reset.append(worktree.candidate_id)

    def commit_worktree(
        self,
        worktree: CampaignWorktree,
        message: str,
    ) -> str:
        return (
            "b" * 40
            if worktree.candidate_id == "candidate-1"
            else "c" * 40
        )

    def export_patch(
        self,
        repository_root: Path,
        campaign_id: str,
        worktree: CampaignWorktree,
        result_commit: str,
    ):
        from foundry_opt.campaign.models import PatchArtifact

        return PatchArtifact(
            candidate_id=worktree.candidate_id,
            path=Path(
                f".foundry-optimizer/campaigns/{campaign_id}/"
                f"{worktree.candidate_id}.patch"
            ),
            sha256=worktree.candidate_id.encode().hex().ljust(64, "0")[:64],
            base_commit=BASE_COMMIT,
            result_commit=result_commit,
        )

    def cleanup_worktree(
        self,
        repository_root: Path,
        worktree: CampaignWorktree,
    ) -> None:
        self.cleaned.append(worktree.candidate_id)


class FakeGenerator:
    def __init__(self) -> None:
        self.history_lengths: list[int] = []
        self.observed_history = []

    def generate(self, context):
        self.history_lengths.append(len(context.history))
        self.observed_history.append(context.history)
        return CandidateIdea(
            idea_id=f"idea-{context.slot}",
            mutation_class="system_instructions",
            parent_idea_ids=(
                ()
                if not context.history
                else (context.history[-1].idea_id,)
            ),
        )


def _bundle(path: Path) -> BundleArtifact:
    return BundleArtifact(
        path=path,
        sha256="d" * 64,
        included_files=("agent/instructions.md",),
        excluded_files=(),
        byte_size=1,
        manifest_path=path.with_suffix(".manifest.json"),
    )


def _result(
    subject: EvaluationSubject,
    split: DatasetSplit,
    value: float,
) -> EvaluationResult:
    run = EvaluationRun(
        run_id=f"run-{subject.subject_id}-{split.value}",
        evaluation_id=f"eval-{subject.subject_id}-{split.value}",
        subject_id=subject.subject_id,
        split=split,
        agent=subject.agent,
        dataset=DatasetVersionRef(f"dataset-{split.value}", "1"),
        evaluator=EvaluatorDefinitionRef("quality", "1"),
        status=EvaluationStatus.COMPLETED,
        portal_url=None,
        started_at=None,
        completed_at=None,
        error=None,
    )
    score = NormalizedCaseMetric(
        "quality",
        value,
        value,
        None,
        Outcome.PASS,
    )
    case = NormalizedCase(
        "case-1",
        "case-hash",
        (f"response-{subject.subject_id}-{split.value}",),
        (score,),
        Usage(),
        None,
        None,
        1,
    )
    from foundry_opt.evaluation import MetricAggregate

    return EvaluationResult(
        run=run,
        cases=(case,),
        metrics={
            "quality": MetricAggregate(
                "quality",
                value,
                value,
                value,
                0.0,
                Outcome.PASS,
                1,
            )
        },
        usage=Usage(),
        duration_ms=1,
        errors=(),
        complete=True,
        needs_repeat=False,
        attempts=1,
    )


def _request(tmp_path: Path, *, candidates: int = 2) -> CampaignRequest:
    return CampaignRequest(
        campaign_id="campaign-1",
        target="agent",
        repository_root=tmp_path,
        limits=CampaignLimits(50, 40, candidates, 1),
        edit_paths=(Path("agent"),),
        allowed_mutations=frozenset({"system_instructions"}),
        evaluation_policy=EvaluationPolicy(
            (
                MetricPolicy(
                    "quality",
                    MetricDirection.MAXIMIZE,
                    0.0,
                    0.1,
                ),
            )
        ),
    )


def _dependencies(
    tmp_path: Path,
    *,
    repository: FakeRepository,
    generator,
    clock: FakeClock,
    validations: list[bool] | None = None,
) -> CampaignDependencies:
    validation_outcomes = iter(validations or [True] * 3)

    def validate(path: Path) -> ValidationReport:
        passed = next(validation_outcomes)
        return ValidationReport(
            (
                ValidationResult(
                    ("test",),
                    path,
                    passed,
                    0 if passed else 1,
                    "",
                    "",
                ),
            ),
            discovered=True,
        )

    def evaluate(
        subject: EvaluationSubject,
        split: DatasetSplit,
        attempt: int,
    ) -> EvaluationResult:
        values = {
            "baseline": 0.5,
            "candidate-1": 0.8,
            "candidate-2": 0.7,
            "candidate-3": 0.6,
        }
        return _result(subject, split, values[subject.subject_id])

    def write_evidence(request) -> EvidenceManifest:
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_text("redacted", encoding="utf-8")
        return EvidenceManifest(
            request.output_path,
            "e" * 64,
            8,
            (),
            (),
        )

    return CampaignDependencies(
        repository=repository,
        generator=generator,
        validate=validate,
        build_bundle=lambda root, output: _bundle(output),
        create_draft=lambda target, subject_id, bundle: DraftRecord(
            target,
            f"draft-{subject_id}",
            1,
            bundle.sha256,
            "ready",
        ),
        evaluate=evaluate,
        write_evidence=write_evidence,
        state=MemoryCampaignStateStore(),
        clock=clock,
    )


def test_campaign_runs_exact_base_and_adapts_candidates_sequentially(
    tmp_path: Path,
) -> None:
    repository = FakeRepository(tmp_path)
    generator = FakeGenerator()
    values = {
        ("baseline", DatasetSplit.DEVELOPMENT): 0.5,
        ("candidate-1", DatasetSplit.DEVELOPMENT): 0.8,
        ("candidate-2", DatasetSplit.DEVELOPMENT): 0.4,
        ("baseline", DatasetSplit.VALIDATION): 0.5,
        ("candidate-1", DatasetSplit.VALIDATION): 0.75,
    }
    evaluation_calls: list[tuple[str, DatasetSplit]] = []

    def evaluate(
        subject: EvaluationSubject,
        split: DatasetSplit,
        attempt: int,
    ) -> EvaluationResult:
        assert attempt == 1
        evaluation_calls.append((subject.subject_id, split))
        return _result(subject, split, values[(subject.subject_id, split)])

    def validate(path: Path) -> ValidationReport:
        return ValidationReport(
            (
                ValidationResult(
                    ("test",),
                    path,
                    True,
                    0,
                    "",
                    "",
                ),
            ),
            discovered=True,
        )

    def create_draft(
        target: str,
        subject_id: str,
        bundle: BundleArtifact,
    ) -> DraftRecord:
        return DraftRecord(
            target,
            f"draft-{subject_id}",
            1,
            bundle.sha256,
            "ready",
        )

    def write_evidence(request) -> EvidenceManifest:
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_text("redacted", encoding="utf-8")
        return EvidenceManifest(
            request.output_path,
            "e" * 64,
            8,
            tuple(
                result.run.evaluation_id
                for result in (request.baseline, *request.candidates)
            ),
            tuple(
                result.run.run_id
                for result in (request.baseline, *request.candidates)
            ),
        )

    request = _request(tmp_path)
    report = run_campaign(
        request,
        CampaignDependencies(
            repository=repository,
            generator=generator,
            validate=validate,
            build_bundle=lambda root, output: _bundle(output),
            create_draft=create_draft,
            evaluate=evaluate,
            write_evidence=write_evidence,
            state=MemoryCampaignStateStore(),
            clock=FakeClock(),
        ),
    )

    assert report.base_commit == BASE_COMMIT
    assert report.baseline_draft_id == "draft-baseline"
    assert tuple(candidate.candidate_id for candidate in report.candidates) == (
        "candidate-1",
        "candidate-2",
    )
    assert report.pareto_candidate_ids == ("candidate-1",)
    assert generator.history_lengths == [0, 1]
    assert generator.observed_history[1][0].metrics == {"quality": 0.8}
    assert generator.observed_history[1][0].eligible
    assert evaluation_calls == [
        ("baseline", DatasetSplit.DEVELOPMENT),
        ("candidate-1", DatasetSplit.DEVELOPMENT),
        ("candidate-2", DatasetSplit.DEVELOPMENT),
        ("baseline", DatasetSplit.VALIDATION),
        ("candidate-1", DatasetSplit.VALIDATION),
    ]
    assert repository.created == ["baseline", "candidate-1", "candidate-2"]
    assert repository.cleaned == ["baseline", "candidate-1", "candidate-2"]


def test_transient_retry_is_free_and_reuses_the_same_candidate_slot(
    tmp_path: Path,
) -> None:
    from foundry_opt.campaign.protocols import TransientCandidateError

    class RetryGenerator(FakeGenerator):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[str] = []

        def generate(self, context):
            self.calls.append(context.candidate_id)
            if len(self.calls) == 1:
                raise TransientCandidateError("temporary agent outage")
            return super().generate(context)

    repository = FakeRepository(tmp_path)
    generator = RetryGenerator()
    report = run_campaign(
        _request(tmp_path),
        _dependencies(
            tmp_path,
            repository=repository,
            generator=generator,
            clock=FakeClock(),
        ),
    )

    assert generator.calls == [
        "candidate-1",
        "candidate-1",
        "candidate-2",
    ]
    assert repository.reset == ["candidate-1"]
    assert len(report.candidates) == 2


def test_validation_failure_consumes_a_slot_and_informs_next_idea(
    tmp_path: Path,
) -> None:
    repository = FakeRepository(tmp_path)
    generator = FakeGenerator()
    report = run_campaign(
        _request(tmp_path),
        _dependencies(
            tmp_path,
            repository=repository,
            generator=generator,
            clock=FakeClock(),
            validations=[False, True],
        ),
    )

    assert tuple(candidate.candidate_id for candidate in report.candidates) == (
        "candidate-2",
    )
    assert generator.history_lengths == [0, 1]
    assert repository.created == ["baseline", "candidate-1", "candidate-2"]


def test_non_transient_generation_failure_consumes_slot_without_retry(
    tmp_path: Path,
) -> None:
    class FailingGenerator(FakeGenerator):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def generate(self, context):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("agent failed")
            return super().generate(context)

    repository = FakeRepository(tmp_path)
    generator = FailingGenerator()
    report = run_campaign(
        _request(tmp_path),
        _dependencies(
            tmp_path,
            repository=repository,
            generator=generator,
            clock=FakeClock(),
        ),
    )

    assert generator.calls == 2
    assert repository.reset == []
    assert tuple(candidate.candidate_id for candidate in report.candidates) == (
        "candidate-2",
    )


def test_unchanged_candidate_consumes_a_slot_without_creating_a_draft(
    tmp_path: Path,
) -> None:
    repository = FakeRepository(tmp_path)
    repository.changed["candidate-1"] = ()
    generator = FakeGenerator()
    report = run_campaign(
        _request(tmp_path),
        _dependencies(
            tmp_path,
            repository=repository,
            generator=generator,
            clock=FakeClock(),
        ),
    )

    assert tuple(candidate.candidate_id for candidate in report.candidates) == (
        "candidate-2",
    )
    assert generator.history_lengths == [0, 1]


def test_candidate_cutoff_stops_new_launches_and_reserves_reporting_time(
    tmp_path: Path,
) -> None:
    clock = FakeClock()

    class SlowGenerator(FakeGenerator):
        def generate(self, context):
            idea = super().generate(context)
            clock.advance(minutes=40)
            return idea

    repository = FakeRepository(tmp_path)
    report = run_campaign(
        _request(tmp_path, candidates=3),
        _dependencies(
            tmp_path,
            repository=repository,
            generator=SlowGenerator(),
            clock=clock,
        ),
    )

    assert tuple(candidate.candidate_id for candidate in report.candidates) == (
        "candidate-1",
    )
    assert repository.created == ["baseline", "candidate-1"]


def test_hard_deadline_skips_held_out_work_but_still_returns_report(
    tmp_path: Path,
) -> None:
    clock = FakeClock()

    class DeadlineGenerator(FakeGenerator):
        def generate(self, context):
            idea = super().generate(context)
            clock.advance(minutes=50)
            return idea

    repository = FakeRepository(tmp_path)
    dependencies = _dependencies(
        tmp_path,
        repository=repository,
        generator=DeadlineGenerator(),
        clock=clock,
    )
    splits: list[DatasetSplit] = []
    evaluate = dependencies.evaluate

    def record_evaluation(subject, split, attempt):
        splits.append(split)
        return evaluate(subject, split, attempt)

    dependencies = CampaignDependencies(
        **{
            **dependencies.__dict__,
            "evaluate": record_evaluation,
        }
    )
    report = run_campaign(_request(tmp_path), dependencies)

    assert splits == [
        DatasetSplit.DEVELOPMENT,
    ]
    assert report.pareto_candidate_ids == ()
    assert report.candidates == ()


def test_hard_guardrail_blocks_provisional_pareto_and_held_out_funnel(
    tmp_path: Path,
) -> None:
    from foundry_opt.evaluation import MetricAggregate

    repository = FakeRepository(tmp_path)
    dependencies = _dependencies(
        tmp_path,
        repository=repository,
        generator=FakeGenerator(),
        clock=FakeClock(),
    )
    calls: list[tuple[str, DatasetSplit]] = []

    def evaluate(subject, split, attempt):
        calls.append((subject.subject_id, split))
        quality = 0.5 if subject.subject_id == "baseline" else 0.8
        safety = 1.0 if subject.subject_id == "baseline" else 0.0
        result = _result(subject, split, quality)
        safety_outcome = Outcome.PASS if safety >= 0.5 else Outcome.FAIL
        case = replace(
            result.cases[0],
            scores=(
                *result.cases[0].scores,
                NormalizedCaseMetric(
                    "safety",
                    safety,
                    safety,
                    None,
                    safety_outcome,
                ),
            ),
        )
        return replace(
            result,
            cases=(case,),
            metrics={
                **result.metrics,
                "safety": MetricAggregate(
                    "safety",
                    safety,
                    safety,
                    safety,
                    0.0,
                    safety_outcome,
                    1,
                ),
            },
        )

    dependencies = CampaignDependencies(
        **{
            **dependencies.__dict__,
            "evaluate": evaluate,
        }
    )
    request = CampaignRequest(
        campaign_id="campaign-1",
        target="agent",
        repository_root=tmp_path,
        limits=CampaignLimits(50, 40, 1, 1),
        edit_paths=(Path("agent"),),
        allowed_mutations=frozenset({"system_instructions"}),
        evaluation_policy=EvaluationPolicy(
            (
                MetricPolicy(
                    "quality",
                    MetricDirection.MAXIMIZE,
                    0.0,
                    0.1,
                ),
                MetricPolicy(
                    "safety",
                    MetricDirection.MAXIMIZE,
                    0.5,
                    0.0,
                    hard_guardrail=True,
                ),
            )
        ),
    )

    report = run_campaign(request, dependencies)

    assert report.pareto_candidate_ids == ()
    assert not report.candidates[0].eligible
    assert calls == [
        ("baseline", DatasetSplit.DEVELOPMENT),
        ("candidate-1", DatasetSplit.DEVELOPMENT),
    ]


def test_disallowed_mutation_is_rejected_and_consumes_its_slot(
    tmp_path: Path,
) -> None:
    repository = FakeRepository(tmp_path)
    repository.changed["candidate-1"] = (Path("production/deploy.yml"),)
    generator = FakeGenerator()

    report = run_campaign(
        _request(tmp_path),
        _dependencies(
            tmp_path,
            repository=repository,
            generator=generator,
            clock=FakeClock(),
        ),
    )

    assert tuple(candidate.candidate_id for candidate in report.candidates) == (
        "candidate-2",
    )
    assert generator.history_lengths == [0, 1]
    assert repository.cleaned == ["baseline", "candidate-1", "candidate-2"]
    assert repository.released == 1


def test_restricted_mutation_requires_explicit_opt_in(tmp_path: Path) -> None:
    class RestrictedGenerator(FakeGenerator):
        def generate(self, context):
            self.history_lengths.append(len(context.history))
            self.observed_history.append(context.history)
            return CandidateIdea(
                f"idea-{context.slot}",
                "system_instructions",
                required_opt_ins=frozenset({"external_services"}),
            )

    repository = FakeRepository(tmp_path)
    report = run_campaign(
        _request(tmp_path, candidates=1),
        _dependencies(
            tmp_path,
            repository=repository,
            generator=RestrictedGenerator(),
            clock=FakeClock(),
        ),
    )

    assert report.candidates == ()
    assert repository.cleaned == ["baseline", "candidate-1"]


def test_stale_active_state_is_reported_without_restarting_slots(
    tmp_path: Path,
) -> None:
    from foundry_opt.campaign.protocols import CampaignStateError

    class RecoveredRepository(FakeRepository):
        def acquire_lock(self, **kwargs: object) -> CampaignLock:
            return CampaignLock("campaign-1", "campaign-1")

    clock = FakeClock()
    repository = RecoveredRepository(tmp_path)
    dependencies = _dependencies(
        tmp_path,
        repository=repository,
        generator=FakeGenerator(),
        clock=clock,
    )
    dependencies.state.save(
        tmp_path,
        CampaignState(
            "campaign-1",
            "agent",
            BASE_COMMIT,
            "active",
            clock.now() - timedelta(hours=2),
            clock.now() - timedelta(hours=2),
            launched_slots=1,
        ),
    )

    with pytest.raises(CampaignStateError) as caught:
        run_campaign(_request(tmp_path), dependencies)

    assert caught.value.state.status == "stale"
    assert repository.created == []
    assert repository.released == 1


def test_orphaned_active_state_is_not_silently_restarted(
    tmp_path: Path,
) -> None:
    from foundry_opt.campaign.protocols import CampaignStateError

    clock = FakeClock()
    repository = FakeRepository(tmp_path)
    dependencies = _dependencies(
        tmp_path,
        repository=repository,
        generator=FakeGenerator(),
        clock=clock,
    )
    dependencies.state.save(
        tmp_path,
        CampaignState(
            "campaign-1",
            "agent",
            BASE_COMMIT,
            "active",
            clock.now() - timedelta(minutes=30),
            clock.now() - timedelta(minutes=30),
            launched_slots=1,
        ),
    )

    with pytest.raises(CampaignStateError) as caught:
        run_campaign(_request(tmp_path), dependencies)

    assert caught.value.state.error_code == "orphaned_active_state"
    assert repository.created == []
    assert repository.released == 1


def test_fatal_campaign_failure_is_persisted_and_releases_the_lock(
    tmp_path: Path,
) -> None:
    repository = FakeRepository(tmp_path)
    clock = FakeClock()
    dependencies = _dependencies(
        tmp_path,
        repository=repository,
        generator=FakeGenerator(),
        clock=clock,
    )

    def fail_bundle(root: Path, output: Path) -> BundleArtifact:
        raise RuntimeError("customer content must not enter state")

    dependencies = CampaignDependencies(
        **{
            **dependencies.__dict__,
            "build_bundle": fail_bundle,
        }
    )

    with pytest.raises(RuntimeError):
        run_campaign(_request(tmp_path), dependencies)

    state = dependencies.state.load(tmp_path, "campaign-1")
    assert state is not None
    assert state.status == "failed"
    assert state.error_code == "RuntimeError"
    assert "customer content" not in repr(state)
    assert repository.cleaned == ["baseline"]
    assert repository.released == 1
