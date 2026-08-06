from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import subprocess

import pytest

from foundry_opt.adapters.campaign_git import CampaignGit
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
    EvaluatorDefinitionRef,
    MetricAggregate,
    MetricDirection,
    MetricPolicy,
    NormalizedCase,
    NormalizedCaseMetric,
    Outcome,
    Usage,
)
from foundry_opt.evidence import EvaluationAssetReference, EvidenceManifest
from foundry_opt.orchestration import (
    AdvanceRequest,
    CampaignEvent,
    CampaignPhase,
    EventKind,
    GitStateRef,
    OptimizationCampaign,
)
from foundry_opt.orchestration.candidate_workers import (
    CandidateDesignIntent,
    CandidateDesignResult,
    CandidateWorkerDependencies,
    CandidateWorkerPlan,
    CandidateWorkerRequest,
    CandidateWorkerService,
    CandidateWorkerStatus,
)
from foundry_opt.packaging import BundleArtifact, ValidationReport


NOW = datetime(2026, 7, 31, 18, 0, tzinfo=UTC)
SPEC_SHA256 = "a" * 64


def _run(arguments: tuple[str, ...], cwd: Path) -> str:
    return subprocess.run(
        arguments,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    origin = tmp_path / "origin.git"
    repository = tmp_path / "repository"
    _run(("git", "init", "--bare", str(origin)), tmp_path)
    _run(("git", "init", "-b", "main", str(repository)), tmp_path)
    _run(("git", "config", "user.name", "Candidate Test"), repository)
    _run(
        ("git", "config", "user.email", "candidate@example.invalid"),
        repository,
    )
    source = repository / "agent" / "instructions.md"
    source.parent.mkdir(parents=True)
    source.write_text("baseline\n", encoding="utf-8")
    _run(("git", "add", "."), repository)
    _run(("git", "commit", "-m", "baseline"), repository)
    _run(("git", "remote", "add", "origin", str(origin)), repository)
    _run(("git", "push", "-u", "origin", "main"), repository)
    return repository, _run(("git", "rev-parse", "HEAD"), repository)


class Clock:
    def __init__(self) -> None:
        self.current = NOW

    def now(self) -> datetime:
        return self.current


class Designer:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.results: dict[str, CandidateDesignResult] = {}
        self.expired = False
        self.invocations = 0

    def reconcile(self, intent: CandidateDesignIntent):
        if intent.candidate_id == "candidate-1" and not self.expired:
            self.expired = True
            self.clock.current = NOW + timedelta(minutes=1)
        result = self.results.get(intent.effect_id)
        return () if result is None else (result,)

    def invoke(self, intent: CandidateDesignIntent):
        self.invocations += 1
        (intent.worktree / "agent" / "instructions.md").write_text(
            "candidate with explicit escalation\n",
            encoding="utf-8",
        )
        result = CandidateDesignResult(
            effect_id=intent.effect_id,
            result_id="result-candidate-1",
            issue_number=intent.issue_number,
            generation=intent.generation,
            spec_sha256=intent.spec_sha256,
            base_commit=intent.base_commit,
            candidate_id=intent.candidate_id,
            slot=intent.slot,
            idea_id="idea-1",
            mutation_class="system_instructions",
            motivation="Make escalation explicit.",
            lessons=("The baseline omits escalation.",),
            complexity="small",
        )
        self.results[intent.effect_id] = result
        return result


class Drafts:
    def __init__(self) -> None:
        self.records: dict[str, DraftRecord] = {}
        self.creates = 0

    def reconcile(self, intent):
        return self.records.get(intent.effect_id)

    def create(self, intent):
        self.creates += 1
        record = DraftRecord(
            "support",
            f"draft-{intent.subject_id}",
            12,
            intent.bundle.sha256,
            "draft",
        )
        self.records[intent.effect_id] = record
        return record


class Evaluations:
    def __init__(self) -> None:
        self.results: dict[str, EvaluationResult] = {}
        self.runs = 0

    def reconcile(self, intent):
        return self.results.get(intent.effect_id)

    def run(self, intent):
        self.runs += 1
        value = 0.5 if intent.subject.subject_id == "baseline" else 0.9
        outcome = Outcome.PASS if value >= 0.8 else Outcome.FAIL
        run = EvaluationRun(
            f"run-{intent.subject.subject_id}",
            f"eval-{intent.subject.subject_id}",
            intent.subject.subject_id,
            DatasetSplit.DEVELOPMENT,
            intent.subject.agent,
            intent.dataset,
            EvaluatorDefinitionRef("quality", "1"),
            EvaluationStatus.COMPLETED,
            None,
            None,
            None,
            None,
        )
        result = EvaluationResult(
            run,
            (
                NormalizedCase(
                    "case-1",
                    "case-hash",
                    (f"response-{intent.subject.subject_id}",),
                    (
                        NormalizedCaseMetric(
                            "quality",
                            value,
                            value,
                            None,
                            outcome,
                        ),
                    ),
                    Usage(),
                    None,
                    None,
                    1,
                ),
            ),
            {
                "quality": MetricAggregate(
                    "quality",
                    value,
                    value,
                    value,
                    0.0,
                    outcome,
                    1,
                )
            },
            Usage(),
            1,
            (),
            True,
            False,
            1,
        )
        self.results[intent.effect_id] = result
        return result


class Resolver:
    def __init__(self, plan: CandidateWorkerPlan) -> None:
        self.plan = plan

    def resolve(self, request, state):
        return self.plan


def _plan(base_commit: str) -> CandidateWorkerPlan:
    return CandidateWorkerPlan(
        issue_number=31,
        generation=1,
        spec_sha256=SPEC_SHA256,
        base_commit=base_commit,
        target="support",
        base_agent_version=12,
        goal="Improve grounded support answers without weakening safety.",
        limits=CampaignLimits(50, 40, 1, 0),
        edit_paths=(Path("agent"),),
        allowed_mutations=frozenset({"system_instructions"}),
        restricted_opt_ins={},
        evaluation_policy=EvaluationPolicy(
            (
                MetricPolicy(
                    "quality",
                    MetricDirection.MAXIMIZE,
                    0.8,
                    0.05,
                ),
            )
        ),
        assets=(
            EvaluationAssetReference(
                "dev",
                "dataset",
                "foundry",
                role="development",
                name="dev",
                version="1",
                remote_id="foundry-dev",
            ),
            EvaluationAssetReference(
                "quality",
                "evaluator",
                "builtin",
                name="quality",
                version="1",
                remote_id="builtin:quality:1",
                metrics=("quality",),
            ),
        ),
        evidence_root=Path(".foundry-optimizer/campaigns"),
    )


def test_real_git_ledger_and_worktree_resume_after_session_replacement(
    tmp_path: Path,
) -> None:
    repository, base_commit = _repository(tmp_path)
    created = CampaignEvent(
        "created",
        EventKind.ISSUE_CREATED,
        1,
        NOW,
    )
    approved = CampaignEvent(
        "approved",
        EventKind.SPEC_POLICY_APPROVED,
        1,
        NOW,
        {"spec_sha256": SPEC_SHA256},
    )
    state = OptimizationCampaign().advance(
        AdvanceRequest(31, None, (created, approved))
    ).state
    ledger = GitStateRef()
    ledger.commit(
        repository,
        issue_number=31,
        expected_revision=None,
        state=state,
        inbox=(created, approved),
    )
    clock = Clock()
    designer = Designer(clock)
    drafts = Drafts()
    evaluations = Evaluations()
    plan = _plan(base_commit)

    def build_bundle(root: Path, output: Path) -> BundleArtifact:
        return BundleArtifact(
            output,
            "e" * 64,
            ("agent/instructions.md",),
            (),
            1,
            output.with_suffix(".manifest.json"),
        )

    def evidence(request) -> EvidenceManifest:
        return EvidenceManifest(
            request.output_path,
            "f" * 64,
            10,
            (),
            (),
            "9" * 64,
            request.spec_sha256,
        )

    first = CandidateWorkerService(
        ledger=ledger,
        resolver=Resolver(plan),
        dependencies=CandidateWorkerDependencies(
            repository=CampaignGit(default_branch=lambda root: "main"),
            designer=designer,
            validate=lambda path: ValidationReport((), False),
            build_bundle=build_bundle,
            drafts=drafts,
            evaluations=evaluations,
            write_evidence=evidence,
            clock=clock,
        ),
    ).advance(
        CandidateWorkerRequest(
            repository,
            31,
            session_deadline=NOW + timedelta(minutes=1),
        )
    )

    assert first.status is CandidateWorkerStatus.WAITING
    assert first.snapshot.state.phase is CampaignPhase.CANDIDATES
    assert designer.invocations == 0

    resumed = CandidateWorkerService(
        ledger=ledger,
        resolver=Resolver(plan),
        dependencies=CandidateWorkerDependencies(
            repository=CampaignGit(default_branch=lambda root: "main"),
            designer=designer,
            validate=lambda path: ValidationReport((), False),
            build_bundle=build_bundle,
            drafts=drafts,
            evaluations=evaluations,
            write_evidence=evidence,
            clock=clock,
        ),
    ).advance(
        CandidateWorkerRequest(
            repository,
            31,
            session_deadline=NOW + timedelta(minutes=30),
        )
    )

    loaded = ledger.load(repository, 31)
    assert loaded == resumed.snapshot
    assert resumed.status is CandidateWorkerStatus.COMPLETE
    assert resumed.snapshot.state.candidates[0].eligible is True
    assert designer.invocations == 1
    assert drafts.creates == 2
    assert evaluations.runs == 2
    assert sum(
        record.kind == "candidate_effect_planned"
        and record.payload.get("effect_kind") == "candidate_design"
        for record in resumed.snapshot.outbox
    ) == 1
    worktrees = _run(("git", "worktree", "list", "--porcelain"), repository)
    assert ".foundry-optimizer/worktrees" not in worktrees


def test_real_worktree_resume_after_candidate_draft_ack_loss(
    tmp_path: Path,
) -> None:
    class SessionCrash(RuntimeError):
        pass

    class CrashDrafts(Drafts):
        def __init__(self) -> None:
            super().__init__()
            self.crashed = False

        def create(self, intent):
            record = super().create(intent)
            if intent.subject_id == "candidate-1" and not self.crashed:
                self.crashed = True
                raise SessionCrash("draft created before acknowledgement")
            return record

    repository, base_commit = _repository(tmp_path)
    created = CampaignEvent("created", EventKind.ISSUE_CREATED, 1, NOW)
    approved = CampaignEvent(
        "approved",
        EventKind.SPEC_POLICY_APPROVED,
        1,
        NOW,
        {"spec_sha256": SPEC_SHA256},
    )
    state = OptimizationCampaign().advance(
        AdvanceRequest(31, None, (created, approved))
    ).state
    ledger = GitStateRef()
    ledger.commit(
        repository,
        issue_number=31,
        expected_revision=None,
        state=state,
        inbox=(created, approved),
    )
    clock = Clock()
    designer = Designer(clock)
    drafts = CrashDrafts()
    evaluations = Evaluations()
    plan = _plan(base_commit)

    def build_bundle(root: Path, output: Path) -> BundleArtifact:
        return BundleArtifact(
            output,
            "e" * 64,
            ("agent/instructions.md",),
            (),
            1,
            output.with_suffix(".manifest.json"),
        )

    dependencies = lambda repository_adapter: CandidateWorkerDependencies(
        repository=repository_adapter,
        designer=designer,
        validate=lambda path: ValidationReport((), False),
        build_bundle=build_bundle,
        drafts=drafts,
        evaluations=evaluations,
        write_evidence=lambda request: EvidenceManifest(
            request.output_path,
            "f" * 64,
            10,
            (),
            (),
            "9" * 64,
            request.spec_sha256,
        ),
        clock=clock,
    )

    with pytest.raises(SessionCrash):
        CandidateWorkerService(
            ledger=ledger,
            resolver=Resolver(plan),
            dependencies=dependencies(
                CampaignGit(default_branch=lambda root: "main")
            ),
        ).advance(CandidateWorkerRequest(repository, 31))

    resumed = CandidateWorkerService(
        ledger=ledger,
        resolver=Resolver(plan),
        dependencies=dependencies(
            CampaignGit(default_branch=lambda root: "main")
        ),
    ).advance(CandidateWorkerRequest(repository, 31))

    assert resumed.status is CandidateWorkerStatus.COMPLETE
    assert drafts.creates == 2
    assert evaluations.runs == 2
    assert designer.invocations == 1


def test_real_worktree_cleanup_resumes_after_process_exit_before_ack(
    tmp_path: Path,
) -> None:
    class ProcessExit(BaseException):
        pass

    class ExitAfterCleanupGit(CampaignGit):
        def __init__(self) -> None:
            super().__init__(default_branch=lambda root: "main")
            self.exited = False

        def cleanup_worktree(self, repository_root, worktree) -> None:
            super().cleanup_worktree(repository_root, worktree)
            if worktree.candidate_id == "candidate-1" and not self.exited:
                self.exited = True
                raise ProcessExit("process exited before cleanup acknowledgement")

    repository, base_commit = _repository(tmp_path)
    created = CampaignEvent("created", EventKind.ISSUE_CREATED, 1, NOW)
    approved = CampaignEvent(
        "approved",
        EventKind.SPEC_POLICY_APPROVED,
        1,
        NOW,
        {"spec_sha256": SPEC_SHA256},
    )
    state = OptimizationCampaign().advance(
        AdvanceRequest(31, None, (created, approved))
    ).state
    ledger = GitStateRef()
    ledger.commit(
        repository,
        issue_number=31,
        expected_revision=None,
        state=state,
        inbox=(created, approved),
    )
    clock = Clock()
    designer = Designer(clock)
    drafts = Drafts()
    evaluations = Evaluations()
    plan = _plan(base_commit)

    def build_bundle(root: Path, output: Path) -> BundleArtifact:
        return BundleArtifact(
            output,
            "e" * 64,
            ("agent/instructions.md",),
            (),
            1,
            output.with_suffix(".manifest.json"),
        )

    def dependencies(repository_adapter) -> CandidateWorkerDependencies:
        return CandidateWorkerDependencies(
            repository=repository_adapter,
            designer=designer,
            validate=lambda path: ValidationReport((), False),
            build_bundle=build_bundle,
            drafts=drafts,
            evaluations=evaluations,
            write_evidence=lambda request: EvidenceManifest(
                request.output_path,
                "f" * 64,
                10,
                (),
                (),
                "9" * 64,
                request.spec_sha256,
            ),
            clock=clock,
        )

    with pytest.raises(ProcessExit):
        CandidateWorkerService(
            ledger=ledger,
            resolver=Resolver(plan),
            dependencies=dependencies(ExitAfterCleanupGit()),
        ).advance(CandidateWorkerRequest(repository, 31))

    after_exit = ledger.load(repository, 31)
    assert after_exit is not None
    assert after_exit.state.candidates[0].candidate_id == "candidate-1"
    assert any(
        record.kind == "candidate_worktree_cleanup_planned"
        and record.payload.get("candidate_id") == "candidate-1"
        for record in after_exit.outbox
    )
    assert not any(
        record.kind == "candidate_worktree_cleanup_succeeded"
        and record.payload.get("candidate_id") == "candidate-1"
        for record in after_exit.outbox
    )

    resumed = CandidateWorkerService(
        ledger=ledger,
        resolver=Resolver(plan),
        dependencies=dependencies(
            CampaignGit(default_branch=lambda root: "main")
        ),
    ).advance(CandidateWorkerRequest(repository, 31))

    assert resumed.status is CandidateWorkerStatus.COMPLETE
    assert designer.invocations == 1
    assert drafts.creates == 2
    assert evaluations.runs == 2
    assert any(
        record.kind == "candidate_worktree_cleanup_succeeded"
        and record.payload.get("candidate_id") == "candidate-1"
        for record in resumed.snapshot.outbox
    )
    worktrees = _run(("git", "worktree", "list", "--porcelain"), repository)
    assert ".foundry-optimizer/worktrees" not in worktrees
