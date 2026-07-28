"""Crash-recovery integration tests for the issue-driven runner.

These exercise the durable checkpoint machinery against a *real*
:class:`~foundry_opt.adapters.campaign_git.CampaignGit` (real Git worktrees,
commits, and patches) and the real evidence writer, while the live Foundry
seams (registration, drafts, evaluation, publication) are deterministic fakes.

Each test drives the campaign to a crash point, simulates a hard process kill
with a ``_SimulatedCrash`` that escapes ``execute`` uncaught, then resumes in a
fresh runner built from fresh dependencies (durable state is re-read from
disk). The resume must reach an identical patch/draft/evaluation/Pareto with no
duplicate worktrees or drafts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import subprocess

import pytest
import yaml

from foundry_opt.adapters.campaign_git import CampaignGit
from foundry_opt.campaign.state import FileCampaignStateStore, FinalizedPublication
from foundry_opt.config.models import OptimizerConfig
from foundry_opt.drafts import DraftRecord
from foundry_opt.evaluation import (
    AgentVersionRef,
    DatasetSplit,
    DatasetVersionRef,
    EvaluationResult,
    EvaluationRun,
    EvaluationStatus,
    EvaluatorDefinitionRef,
    MetricAggregate,
    NormalizedCase,
    NormalizedCaseMetric,
    Outcome,
    Usage,
)
from foundry_opt.evidence.writer import write_redacted_evidence
from foundry_opt.optimization.commands import (
    OptimizeCommandRequest,
    OptimizeCommandStatus,
    OptimizePhase,
)
from foundry_opt.optimization.models import (
    ApprovalGate,
    AssetKind,
    AssetProvenance,
    OptimizationSpec,
)
from foundry_opt.optimization.runner import (
    CampaignPublicationInputs,
    IssueOptimizationDependencies,
    IssueOptimizationRunner,
    SpecApprovalResult,
)
from foundry_opt.optimization.specification import (
    SpecServiceStatus,
    provenance_file_path,
    spec_file_path,
)
from foundry_opt.packaging import BundleRequest, build_source_bundle


ISSUE = 7
CAMPAIGN_ID = "issue-7"
GOAL = (
    "Improve response quality and latency for the support agent while "
    "preserving safety guardrails across every candidate."
)

# Two metrics so both candidates can sit on a 2-point Pareto frontier.
_METRICS = {
    ("baseline", "development"): {"quality": 0.80, "latency": 0.50},
    ("baseline", "validation"): {"quality": 0.80, "latency": 0.50},
    ("candidate-1", "development"): {"quality": 0.90, "latency": 0.50},
    ("candidate-1", "validation"): {"quality": 0.90, "latency": 0.50},
    ("candidate-2", "development"): {"quality": 0.85, "latency": 0.40},
    ("candidate-2", "validation"): {"quality": 0.85, "latency": 0.40},
}


class _SimulatedCrash(BaseException):
    """A hard process kill: escapes ``execute`` without being caught."""


# ---------------------------------------------------------------------------
# Real Git repository
# ---------------------------------------------------------------------------


def _run(arguments: tuple[str, ...], cwd: Path) -> str:
    return subprocess.run(
        arguments, cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _repository(tmp_path: Path) -> tuple[Path, str]:
    origin = tmp_path / "origin.git"
    repository = tmp_path / "repository"
    _run(("git", "init", "--bare", str(origin)), tmp_path)
    _run(("git", "init", "-b", "main", str(repository)), tmp_path)
    _run(("git", "config", "user.name", "Crash Test"), repository)
    _run(("git", "config", "user.email", "crash@example.invalid"), repository)
    (repository / ".gitignore").write_text(
        ".foundry-optimizer/\n", encoding="utf-8"
    )
    (repository / "agent").mkdir()
    (repository / "agent" / "instructions.md").write_text(
        "baseline instructions\n", encoding="utf-8"
    )
    _run(("git", "add", "."), repository)
    _run(("git", "commit", "-m", "baseline"), repository)
    _run(("git", "remote", "add", "origin", str(origin)), repository)
    _run(("git", "push", "-u", "origin", "main"), repository)
    commit = _run(("git", "rev-parse", "HEAD"), repository).strip()
    return repository, commit


# ---------------------------------------------------------------------------
# Config + spec fixtures
# ---------------------------------------------------------------------------


def _config() -> OptimizerConfig:
    return OptimizerConfig.model_validate(
        {
            "schema_version": "1",
            "default_environment": "acceptance",
            "environments": {
                "acceptance": {
                    "project_endpoint": (
                        "https://example.services.ai.azure.com/api/projects/demo"
                    ),
                    "project_resource_id": (
                        "/subscriptions/sub/resourceGroups/rg/providers/"
                        "Microsoft.CognitiveServices/accounts/foundry/projects/"
                        "demo"
                    ),
                    "allowed_models": ["gpt-5.1"],
                    "deployment_workflow": {
                        "path": ".github/workflows/deploy.yml",
                        "trigger": "manual",
                    },
                }
            },
            "targets": {
                "support_agent": {
                    "environment": "acceptance",
                    "source_paths": ["agent"],
                    "edit_paths": ["agent"],
                    "entry_point": "agent/main.py",
                    "base_agent_version": "12",
                    "package": {"include": ["agent/**"], "exclude": []},
                    "datasets": {
                        "development": [
                            {"name": "dev", "version": "v1", "mode": "batch"}
                        ],
                        "validation": [
                            {
                                "name": "held-out",
                                "version": "v1",
                                "mode": "batch",
                            }
                        ],
                    },
                    "evaluators": [
                        {
                            "name": "quality",
                            "reference": "quality-evaluator",
                            "metrics": ["quality", "latency"],
                        }
                    ],
                    "validation_commands": ["uv run pytest -q"],
                    "metrics": {
                        "quality": {
                            "direction": "maximize",
                            "threshold": 0.8,
                            "materiality": 0.05,
                            "hard_guardrail": False,
                            "undefined_behavior": "fail",
                        },
                        "latency": {
                            "direction": "minimize",
                            "threshold": 0.6,
                            "materiality": 0.05,
                            "hard_guardrail": False,
                            "undefined_behavior": "fail",
                        },
                    },
                    "allowed_mutations": ["system_instructions"],
                }
            },
            "campaign": {
                "deadline_minutes": 50,
                "candidate_cutoff_minutes": 40,
                "max_changed_candidates": 2,
                "transient_retries": 1,
                "stale_after_hours": 2,
                "evidence_path": ".foundry-optimizer/campaigns",
                "allowed_issue_overrides": [],
                "allowed_mutations": ["system_instructions"],
            },
        }
    )


def _spec(base_commit: str) -> OptimizationSpec:
    return OptimizationSpec(
        issue_number=ISSUE,
        repository="octo-org/optimizer",
        base_commit=base_commit,
        target="support_agent",
        environment="acceptance",
        base_agent_version="12",
        goal=GOAL,
        datasets=(
            AssetProvenance(
                asset_id="dataset-dev",
                kind=AssetKind.DATASET,
                source="foundry",
                role="development",
                name="dev-dataset",
                version="1",
                created_by="foundry-existing-asset-provider",
                approval_gate=ApprovalGate.POLICY,
                remote_id="foundry-dataset-dev",
            ),
            AssetProvenance(
                asset_id="dataset-val",
                kind=AssetKind.DATASET,
                source="foundry",
                role="validation",
                name="val-dataset",
                version="1",
                created_by="foundry-existing-asset-provider",
                approval_gate=ApprovalGate.POLICY,
                remote_id="foundry-dataset-val",
            ),
        ),
        evaluators=(
            AssetProvenance(
                asset_id="evaluator-quality",
                kind=AssetKind.EVALUATOR,
                source="builtin",
                name="quality",
                version="1",
                created_by="builtin-evaluator-provider",
                approval_gate=ApprovalGate.POLICY,
                remote_id="builtin:quality:1",
                metrics=("quality", "latency"),
            ),
        ),
        metrics={
            "quality": {
                "direction": "maximize",
                "threshold": 0.8,
                "materiality": 0.05,
                "hard_guardrail": False,
                "undefined_behavior": "fail",
            },
            "latency": {
                "direction": "minimize",
                "threshold": 0.6,
                "materiality": 0.05,
                "hard_guardrail": False,
                "undefined_behavior": "fail",
            },
        },
        allowed_mutations=frozenset({"system_instructions"}),
    )


def _write_spec_bundle(repository: Path, spec: OptimizationSpec) -> None:
    spec_path = repository / spec_file_path(ISSUE)
    provenance_path = repository / provenance_file_path(ISSUE)
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(
        yaml.safe_dump(
            json.loads(spec.canonical_json),
            sort_keys=True,
            default_flow_style=False,
        ),
        encoding="utf-8",
    )
    provenance_path.write_text(
        json.dumps(
            {
                "base_commit": spec.base_commit,
                "datasets": [
                    {"asset_id": item.asset_id, "path": None, "source": item.source}
                    for item in spec.datasets
                ],
                "evaluators": [
                    {"asset_id": item.asset_id, "path": None, "source": item.source}
                    for item in spec.evaluators
                ],
                "issue_number": ISSUE,
                "schema_version": 1,
                "spec_sha256": spec.sha256,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _result(
    subject_id: str,
    split: DatasetSplit,
    *,
    agent: AgentVersionRef | None = None,
) -> EvaluationResult:
    values = _METRICS[(subject_id, split.value)]
    run = EvaluationRun(
        run_id=f"run-{subject_id}-{split.value}",
        evaluation_id=f"eval-{subject_id}-{split.value}",
        subject_id=subject_id,
        split=split,
        agent=agent
        or AgentVersionRef("support_agent", f"draft-{subject_id}", "1"),
        dataset=DatasetVersionRef(f"dataset-{split.value}", "1"),
        evaluator=EvaluatorDefinitionRef("quality", "1"),
        status=EvaluationStatus.COMPLETED,
        portal_url=None,
        started_at=None,
        completed_at=None,
        error=None,
    )
    scores = tuple(
        NormalizedCaseMetric(name, value, value, None, Outcome.PASS)
        for name, value in sorted(values.items())
    )
    case = NormalizedCase(
        case_id="case-1",
        case_hash="case-hash",
        response_ids=(f"response-{subject_id}-{split.value}",),
        scores=scores,
        usage=Usage(),
        trajectory=None,
        error=None,
        duration_ms=1,
    )
    return EvaluationResult(
        run=run,
        cases=(case,),
        metrics={
            name: MetricAggregate(name, value, value, value, 0.0, Outcome.PASS, 1)
            for name, value in values.items()
        },
        usage=Usage(),
        duration_ms=1,
        errors=(),
        complete=True,
        needs_repeat=False,
        attempts=1,
    )


# ---------------------------------------------------------------------------
# Deterministic Foundry-seam fakes with injectable crashes
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 26, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, *, minutes: int) -> None:
        self.current += timedelta(minutes=minutes)


class FakeSpecGateway:
    def verify_spec_approval(self, repository_root, *, issue_number, spec, spec_sha256, base_commit):
        return SpecApprovalResult(
            approved=True, default_branch="main", approval_commit="a" * 40
        )


class FakeRegistrationGateway:
    def register(self, *, kind, name, version, content):
        raise AssertionError("no asset registration expected for foundry assets")


@dataclass
class FakeSpecServiceResult:
    status: SpecServiceStatus
    issue_number: int
    spec_sha256: str | None = None
    pull_request: object | None = None
    blockers: tuple[str, ...] = ()
    failures: tuple[object, ...] = ()


class FakeSpecService:
    def prepare_specification(self, repository_root, issue_number):
        return FakeSpecServiceResult(
            status=SpecServiceStatus.COMPLETE, issue_number=issue_number
        )


class DraftCreator:
    """Idempotent, production-like fake keyed on the idempotency key."""

    def __init__(self, crash_on_call: int | None = None) -> None:
        self.calls = 0
        self.by_key: dict[str, DraftRecord] = {}
        self.crash_on_call = crash_on_call

    def __call__(self, target, subject_id, idempotency_key, bundle) -> DraftRecord:
        self.calls += 1
        if self.crash_on_call == self.calls:
            self.crash_on_call = None
            raise _SimulatedCrash()
        if idempotency_key not in self.by_key:
            self.by_key[idempotency_key] = DraftRecord(
                target, f"draft-{subject_id}", 1, bundle.sha256, "ready"
            )
        return self.by_key[idempotency_key]

    @property
    def distinct_drafts(self) -> int:
        return len(self.by_key)


class Evaluator:
    def __init__(self, crash_on: tuple[str, str] | None = None, crash_after: int = 1) -> None:
        self.calls: list[tuple[str, str]] = []
        self.crash_on = crash_on
        self._matches = 0
        self._crash_after = crash_after

    def __call__(self, subject, split, attempt) -> EvaluationResult:
        key = (subject.subject_id, split.value)
        if self.crash_on == key:
            self._matches += 1
            if self._matches == self._crash_after:
                self.crash_on = None
                raise _SimulatedCrash()
        self.calls.append(key)
        return _result(subject.subject_id, split, agent=subject.agent)


class Publisher:
    def __init__(self, crash_once: bool = False) -> None:
        self.inputs: list[CampaignPublicationInputs] = []
        self.crash_once = crash_once

    def publish(self, inputs: CampaignPublicationInputs) -> FinalizedPublication:
        if self.crash_once:
            self.crash_once = False
            raise _SimulatedCrash()
        self.inputs.append(inputs)
        return FinalizedPublication(
            campaign_pull_request_number=42,
            campaign_pull_request_url=(
                "https://github.com/octo-org/optimizer/pull/42"
            ),
            candidate_issue_numbers={
                candidate_id: 200 + index
                for index, candidate_id in enumerate(
                    inputs.report.pareto_candidate_ids
                )
            },
        )


class CrashOnSave:
    """Wraps a state store; raises before the save whose state matches."""

    def __init__(self, inner, predicate) -> None:
        self._inner = inner
        self._predicate = predicate
        self.armed = True

    def load(self, root, campaign_id):
        return self._inner.load(root, campaign_id)

    def save(self, root, state) -> None:
        if self.armed and self._predicate(state):
            self.armed = False
            raise _SimulatedCrash()
        self._inner.save(root, state)

    def mark_stale(self, root, campaign_id, now) -> None:
        self._inner.mark_stale(root, campaign_id, now)


class CrashingRepository:
    """Wraps CampaignGit; crashes after a real reconcile/create side effect."""

    def __init__(self, inner: CampaignGit, crash_reconcile_after: int | None = None) -> None:
        self._inner = inner
        self._reconciles = 0
        self.crash_reconcile_after = crash_reconcile_after

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def reconcile_worktree(self, repository_root, campaign_id, candidate_id, base_commit):
        worktree = self._inner.reconcile_worktree(
            repository_root, campaign_id, candidate_id, base_commit
        )
        self._reconciles += 1
        if self.crash_reconcile_after == self._reconciles:
            self.crash_reconcile_after = None
            raise _SimulatedCrash()
        return worktree


def _write_evidence(request):
    return write_redacted_evidence(request)


@dataclass
class Deps:
    root: Path
    clock: FakeClock
    repository: object
    draft_creator: DraftCreator
    evaluator: Evaluator
    publisher: Publisher
    state: object

    def build(self) -> IssueOptimizationDependencies:
        return IssueOptimizationDependencies(
            config=_config(),
            spec_service=FakeSpecService(),
            spec_gateway=FakeSpecGateway(),
            registration_gateway=FakeRegistrationGateway(),
            repository=self.repository,
            validate=lambda path: _passing_validation(path),
            build_bundle=lambda root_path, output: build_source_bundle(
                BundleRequest(repository_root=root_path, output_path=output)
            ),
            create_draft=self.draft_creator,
            bind_evaluation=lambda spec, assets: self.evaluator,
            write_evidence=_write_evidence,
            publish=self.publisher,
            state=self.state,
            clock=self.clock,
        )

    def runner(self) -> IssueOptimizationRunner:
        return IssueOptimizationRunner(self.build())


def _passing_validation(path: Path):
    from foundry_opt.packaging import ValidationReport, ValidationResult

    return ValidationReport(
        (ValidationResult(("test",), path, True, 0, "", ""),), discovered=True
    )


def _request(phase: OptimizePhase, **kwargs) -> OptimizeCommandRequest:
    return OptimizeCommandRequest(
        repository_root=kwargs["root"],
        issue_number=ISSUE,
        phase=phase,
        candidate_id=kwargs.get("candidate_id"),
        idea_file=kwargs.get("idea_file"),
    )


def _idea(repository: Path, idea_id: str, parents: tuple[str, ...] = ()) -> Path:
    # Idea files live under the gitignored optimizer directory so they never
    # dirty the working tree that a later RUN pins against.
    path = repository / ".foundry-optimizer" / "ideas" / f"{idea_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "idea_id": idea_id,
                "mutation_class": "system_instructions",
                "parent_idea_ids": list(parents),
                "required_opt_ins": [],
            }
        ),
        encoding="utf-8",
    )
    return path


def _edit_worktree(repository: Path, candidate_id: str, text: str) -> None:
    worktree = (
        repository
        / ".foundry-optimizer"
        / "worktrees"
        / CAMPAIGN_ID
        / candidate_id
        / "agent"
        / "instructions.md"
    )
    worktree.write_text(text, encoding="utf-8")


def _worktree_count(repository: Path) -> int:
    listing = _run(("git", "worktree", "list", "--porcelain"), repository)
    return sum(
        1
        for line in listing.splitlines()
        if line.startswith("worktree ") and CAMPAIGN_ID in line
    )


def _deps(
    repository: Path,
    *,
    clock: FakeClock,
    repository_impl: object | None = None,
    draft_creator: DraftCreator | None = None,
    evaluator: Evaluator | None = None,
    publisher: Publisher | None = None,
    state: object | None = None,
) -> Deps:
    return Deps(
        root=repository,
        clock=clock,
        repository=repository_impl or CampaignGit(default_branch=lambda _: "main"),
        draft_creator=draft_creator or DraftCreator(),
        evaluator=evaluator or Evaluator(),
        publisher=publisher or Publisher(),
        state=state or FileCampaignStateStore(),
    )


def _establish_baseline(repository: Path, clock: FakeClock) -> None:
    result = _deps(repository, clock=clock).runner().execute(
        _request(OptimizePhase.RUN, root=repository)
    )
    assert result.status is OptimizeCommandStatus.AWAITING_AGENT


def _submit_candidate(
    repository: Path,
    clock: FakeClock,
    candidate_id: str,
    idea_id: str,
    *,
    parents: tuple[str, ...] = (),
    draft_creator: DraftCreator | None = None,
    evaluator: Evaluator | None = None,
) -> OptimizeCommandResultLike:
    deps = _deps(
        repository,
        clock=clock,
        draft_creator=draft_creator,
        evaluator=evaluator,
    )
    runner = deps.runner()
    runner.execute(_request(OptimizePhase.CANDIDATE_REQUEST, root=repository))
    _edit_worktree(repository, candidate_id, f"{candidate_id} instructions\n")
    idea = _idea(repository, idea_id, parents)
    return runner.execute(
        _request(
            OptimizePhase.CANDIDATE_SUBMIT,
            root=repository,
            candidate_id=candidate_id,
            idea_file=idea,
        )
    )


OptimizeCommandResultLike = object


# ---------------------------------------------------------------------------
# Happy path (sanity) with real Git
# ---------------------------------------------------------------------------


def test_full_campaign_with_real_git_finalizes_and_publishes(
    tmp_path: Path,
) -> None:
    repository, base_commit = _repository(tmp_path)
    _write_spec_bundle(repository, _spec(base_commit))
    clock = FakeClock()

    _establish_baseline(repository, clock)
    _submit_candidate(repository, clock, "candidate-1", "idea-1")
    _submit_candidate(
        repository, clock, "candidate-2", "idea-2", parents=("idea-1",)
    )
    final = _deps(repository, clock=clock).runner().execute(
        _request(OptimizePhase.RUN, root=repository)
    )

    assert final.status is OptimizeCommandStatus.COMPLETE
    assert set(final.details["eligible_candidates"]) == {
        "candidate-1",
        "candidate-2",
    }
    # No campaign worktrees remain and the default branch is untouched.
    assert _worktree_count(repository) == 0
    state = FileCampaignStateStore().load(repository, CAMPAIGN_ID)
    assert state is not None and state.finalized is not None


# ---------------------------------------------------------------------------
# Crash 1: after create_worktree, before the awaiting_idea state/context save
# ---------------------------------------------------------------------------


def test_crash_after_create_worktree_resumes_without_duplicate(
    tmp_path: Path,
) -> None:
    repository, base_commit = _repository(tmp_path)
    _write_spec_bundle(repository, _spec(base_commit))
    clock = FakeClock()
    _establish_baseline(repository, clock)

    crashing_repo = CrashingRepository(
        CampaignGit(default_branch=lambda _: "main"),
        crash_reconcile_after=1,
    )
    deps = _deps(repository, clock=clock, repository_impl=crashing_repo)
    with pytest.raises(_SimulatedCrash):
        deps.runner().execute(
            _request(OptimizePhase.CANDIDATE_REQUEST, root=repository)
        )

    # The reservation is durable at ``preparing_worktree`` with an orphan
    # worktree on disk.
    state = FileCampaignStateStore().load(repository, CAMPAIGN_ID)
    assert state is not None
    assert state.candidates[0].status == "preparing_worktree"
    assert _worktree_count(repository) == 1

    resumed = _deps(repository, clock=clock).runner().execute(
        _request(OptimizePhase.CANDIDATE_REQUEST, root=repository)
    )
    assert resumed.status is OptimizeCommandStatus.AWAITING_AGENT
    assert resumed.details["candidate_id"] == "candidate-1"
    # Exactly one reconciled worktree — no duplicate.
    assert _worktree_count(repository) == 1
    state = FileCampaignStateStore().load(repository, CAMPAIGN_ID)
    assert state is not None
    assert state.candidates[0].status == "awaiting_idea"
    assert state.launched_slots == 1


# ---------------------------------------------------------------------------
# Crash 2: after commit + export patch, before the draft POST
# ---------------------------------------------------------------------------


def test_crash_after_commit_resumes_without_recommitting(
    tmp_path: Path,
) -> None:
    repository, base_commit = _repository(tmp_path)
    _write_spec_bundle(repository, _spec(base_commit))
    clock = FakeClock()
    _establish_baseline(repository, clock)

    crashing_drafts = DraftCreator(crash_on_call=1)
    deps = _deps(repository, clock=clock, draft_creator=crashing_drafts)
    runner = deps.runner()
    runner.execute(_request(OptimizePhase.CANDIDATE_REQUEST, root=repository))
    _edit_worktree(repository, "candidate-1", "candidate-1 instructions\n")
    idea = _idea(repository, "idea-1")
    with pytest.raises(_SimulatedCrash):
        runner.execute(
            _request(
                OptimizePhase.CANDIDATE_SUBMIT,
                root=repository,
                candidate_id="candidate-1",
                idea_file=idea,
            )
        )

    state = FileCampaignStateStore().load(repository, CAMPAIGN_ID)
    assert state is not None
    committed = state.candidates[0]
    assert committed.status == "committed"
    assert committed.result_commit is not None
    assert committed.patch is not None
    committed_result = committed.result_commit
    committed_patch_sha = committed.patch.sha256

    resumed_drafts = DraftCreator()
    resumed = runner_submit_resume(
        repository, clock, "candidate-1", idea, draft_creator=resumed_drafts
    )
    assert resumed.status is OptimizeCommandStatus.AWAITING_AGENT

    state = FileCampaignStateStore().load(repository, CAMPAIGN_ID)
    assert state is not None
    evaluated = state.candidates[0]
    assert evaluated.status == "evaluated"
    # The commit was not repeated: identical result commit and patch.
    assert evaluated.result_commit == committed_result
    assert evaluated.artifact is not None
    assert evaluated.artifact.patch.sha256 == committed_patch_sha
    # Exactly one draft was created on resume.
    assert resumed_drafts.distinct_drafts == 1


def runner_submit_resume(
    repository: Path,
    clock: FakeClock,
    candidate_id: str,
    idea: Path,
    *,
    draft_creator: DraftCreator | None = None,
    evaluator: Evaluator | None = None,
    state: object | None = None,
):
    deps = _deps(
        repository,
        clock=clock,
        draft_creator=draft_creator,
        evaluator=evaluator,
        state=state,
    )
    return deps.runner().execute(
        _request(
            OptimizePhase.CANDIDATE_SUBMIT,
            root=repository,
            candidate_id=candidate_id,
            idea_file=idea,
        )
    )


# ---------------------------------------------------------------------------
# Crash 3: after the draft is persisted, before the development evaluation
# ---------------------------------------------------------------------------


def test_crash_after_draft_resumes_without_duplicate_draft(
    tmp_path: Path,
) -> None:
    repository, base_commit = _repository(tmp_path)
    _write_spec_bundle(repository, _spec(base_commit))
    clock = FakeClock()
    _establish_baseline(repository, clock)

    draft_creator = DraftCreator()
    crashing_eval = Evaluator(crash_on=("candidate-1", "development"))
    deps = _deps(
        repository,
        clock=clock,
        draft_creator=draft_creator,
        evaluator=crashing_eval,
    )
    runner = deps.runner()
    runner.execute(_request(OptimizePhase.CANDIDATE_REQUEST, root=repository))
    _edit_worktree(repository, "candidate-1", "candidate-1 instructions\n")
    idea = _idea(repository, "idea-1")
    with pytest.raises(_SimulatedCrash):
        runner.execute(
            _request(
                OptimizePhase.CANDIDATE_SUBMIT,
                root=repository,
                candidate_id="candidate-1",
                idea_file=idea,
            )
        )

    state = FileCampaignStateStore().load(repository, CAMPAIGN_ID)
    assert state is not None
    assert state.candidates[0].status == "drafted"
    assert draft_creator.distinct_drafts == 1

    resumed = runner_submit_resume(
        repository, clock, "candidate-1", idea, draft_creator=draft_creator
    )
    assert resumed.status is OptimizeCommandStatus.AWAITING_AGENT
    state = FileCampaignStateStore().load(repository, CAMPAIGN_ID)
    assert state is not None
    assert state.candidates[0].status == "evaluated"
    # The draft was not recreated on resume.
    assert draft_creator.distinct_drafts == 1
    assert draft_creator.calls == 1


# ---------------------------------------------------------------------------
# Crash 4: draft POST succeeded but its record was not persisted
# ---------------------------------------------------------------------------


def test_crash_between_draft_post_and_persist_reuses_draft(
    tmp_path: Path,
) -> None:
    repository, base_commit = _repository(tmp_path)
    _write_spec_bundle(repository, _spec(base_commit))
    clock = FakeClock()
    _establish_baseline(repository, clock)

    draft_creator = DraftCreator()
    crashing_state = CrashOnSave(
        FileCampaignStateStore(),
        lambda state: any(c.status == "drafted" for c in state.candidates),
    )
    deps = _deps(
        repository,
        clock=clock,
        draft_creator=draft_creator,
        state=crashing_state,
    )
    runner = deps.runner()
    runner.execute(_request(OptimizePhase.CANDIDATE_REQUEST, root=repository))
    _edit_worktree(repository, "candidate-1", "candidate-1 instructions\n")
    idea = _idea(repository, "idea-1")
    with pytest.raises(_SimulatedCrash):
        runner.execute(
            _request(
                OptimizePhase.CANDIDATE_SUBMIT,
                root=repository,
                candidate_id="candidate-1",
                idea_file=idea,
            )
        )

    # The draft POST succeeded but the ``drafted`` checkpoint was lost, so the
    # persisted state is still ``committed``.
    state = FileCampaignStateStore().load(repository, CAMPAIGN_ID)
    assert state is not None
    assert state.candidates[0].status == "committed"
    assert draft_creator.distinct_drafts == 1

    resumed = runner_submit_resume(
        repository, clock, "candidate-1", idea, draft_creator=draft_creator
    )
    assert resumed.status is OptimizeCommandStatus.AWAITING_AGENT
    # The idempotent draft creator reused the same draft — no duplicate.
    assert draft_creator.distinct_drafts == 1


# ---------------------------------------------------------------------------
# Crash 5: mid held-out evaluation, after baseline + one candidate persisted
# ---------------------------------------------------------------------------


def test_crash_mid_heldout_resumes_reusing_persisted_results(
    tmp_path: Path,
) -> None:
    repository, base_commit = _repository(tmp_path)
    _write_spec_bundle(repository, _spec(base_commit))
    clock = FakeClock()
    _establish_baseline(repository, clock)
    _submit_candidate(repository, clock, "candidate-1", "idea-1")
    _submit_candidate(
        repository, clock, "candidate-2", "idea-2", parents=("idea-1",)
    )

    # Crash on the second eligible candidate's held-out evaluation, after the
    # baseline and first candidate held-out results are persisted.
    crashing_eval = Evaluator(crash_on=("candidate-2", "validation"))
    deps = _deps(repository, clock=clock, evaluator=crashing_eval)
    with pytest.raises(_SimulatedCrash):
        deps.runner().execute(_request(OptimizePhase.RUN, root=repository))

    state = FileCampaignStateStore().load(repository, CAMPAIGN_ID)
    assert state is not None
    assert state.baseline_validation is not None
    persisted = {
        c.candidate_id: c.validation_result is not None for c in state.candidates
    }
    assert persisted["candidate-1"] is True
    assert persisted["candidate-2"] is False

    # Resume: baseline + candidate-1 held-out are reused; only candidate-2 runs.
    resume_eval = Evaluator()
    final = _deps(repository, clock=clock, evaluator=resume_eval).runner().execute(
        _request(OptimizePhase.RUN, root=repository)
    )
    assert final.status is OptimizeCommandStatus.COMPLETE
    assert ("baseline", "validation") not in resume_eval.calls
    assert ("candidate-1", "validation") not in resume_eval.calls
    assert ("candidate-2", "validation") in resume_eval.calls


# ---------------------------------------------------------------------------
# Crash 6: after evidence is written, before publication
# ---------------------------------------------------------------------------


def test_crash_after_evidence_before_publication_resumes(
    tmp_path: Path,
) -> None:
    repository, base_commit = _repository(tmp_path)
    _write_spec_bundle(repository, _spec(base_commit))
    clock = FakeClock()
    _establish_baseline(repository, clock)
    _submit_candidate(repository, clock, "candidate-1", "idea-1")
    _submit_candidate(
        repository, clock, "candidate-2", "idea-2", parents=("idea-1",)
    )

    crashing_publisher = Publisher(crash_once=True)
    deps = _deps(repository, clock=clock, publisher=crashing_publisher)
    with pytest.raises(_SimulatedCrash):
        deps.runner().execute(_request(OptimizePhase.RUN, root=repository))

    # Evidence was written and completion persisted, but publication did not
    # finish.
    evidence = (
        repository
        / ".foundry-optimizer"
        / "campaigns"
        / CAMPAIGN_ID
        / "development-evidence.json"
    )
    assert evidence.is_file()
    state = FileCampaignStateStore().load(repository, CAMPAIGN_ID)
    assert state is not None
    assert state.finalized is None

    resume_eval = Evaluator()
    resume_publisher = Publisher()
    final = _deps(
        repository,
        clock=clock,
        evaluator=resume_eval,
        publisher=resume_publisher,
    ).runner().execute(_request(OptimizePhase.RUN, root=repository))

    assert final.status is OptimizeCommandStatus.COMPLETE
    # Held-out evaluations were not re-run; evidence was reused (no FileExists).
    assert resume_eval.calls == []
    assert len(resume_publisher.inputs) == 1
    state = FileCampaignStateStore().load(repository, CAMPAIGN_ID)
    assert state is not None and state.finalized is not None

