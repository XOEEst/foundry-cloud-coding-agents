from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from foundry_opt.campaign.protocols import (
    CampaignLock,
    CampaignWorktree,
    PinnedRepository,
)
from foundry_opt.campaign.state import FileCampaignStateStore
from foundry_opt.campaign.worktrees import contained_worktree_root
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
from foundry_opt.evidence import EvidenceManifest
from foundry_opt.optimization.assets import AssetIdentity
from foundry_opt.optimization.commands import (
    OptimizeCommandRequest,
    OptimizeCommandResult,
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
from foundry_opt.packaging import (
    BundleArtifact,
    ValidationReport,
    ValidationResult,
)


BASE_COMMIT = "b" * 40
APPROVAL_COMMIT = "a" * 40
GOAL = (
    "Improve response quality for the support agent while preserving safety "
    "guardrails across every candidate."
)

_METRIC_VALUES = {
    ("baseline", "development"): 0.80,
    ("baseline", "validation"): 0.80,
    ("candidate-1", "development"): 0.90,
    ("candidate-1", "validation"): 0.88,
    ("candidate-2", "development"): 0.84,
    ("candidate-2", "validation"): 0.84,
    ("candidate-3", "development"): 0.83,
    ("candidate-3", "validation"): 0.83,
}


# ---------------------------------------------------------------------------
# Config + spec fixtures
# ---------------------------------------------------------------------------


def _config() -> OptimizerConfig:
    document = {
        "schema_version": "1",
        "default_environment": "acceptance",
        "environments": {
            "acceptance": {
                "project_endpoint": (
                    "https://example.services.ai.azure.com/api/projects/demo"
                ),
                "project_resource_id": (
                    "/subscriptions/sub/resourceGroups/rg/providers/"
                    "Microsoft.CognitiveServices/accounts/foundry/projects/demo"
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
                        {"name": "held-out", "version": "v1", "mode": "batch"}
                    ],
                },
                "evaluators": [
                    {
                        "name": "quality",
                        "reference": "quality-evaluator",
                        "metrics": ["quality"],
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
                    }
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
    return OptimizerConfig.model_validate(document)


def _spec(issue_number: int = 7) -> OptimizationSpec:
    return OptimizationSpec(
        issue_number=issue_number,
        repository="octo-org/optimizer",
        base_commit=BASE_COMMIT,
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
                metrics=("quality",),
            ),
        ),
        metrics={
            "quality": {
                "direction": "maximize",
                "threshold": 0.8,
                "materiality": 0.05,
                "hard_guardrail": False,
                "undefined_behavior": "fail",
            }
        },
        allowed_mutations=frozenset({"system_instructions"}),
    )


def _write_spec_bundle(
    root: Path,
    spec: OptimizationSpec,
    asset_paths: dict[str, str | None] | None = None,
) -> None:
    spec_path = root / spec_file_path(spec.issue_number)
    provenance_path = root / provenance_file_path(spec.issue_number)
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    document = json.loads(spec.canonical_json)
    spec_path.write_text(
        yaml.safe_dump(document, sort_keys=True, default_flow_style=False),
        encoding="utf-8",
    )
    paths = asset_paths or {}

    def _entry(provenance: AssetProvenance) -> dict[str, object]:
        return {
            "asset_id": provenance.asset_id,
            "path": paths.get(provenance.asset_id),
            "source": provenance.source,
        }

    provenance_document = {
        "base_commit": spec.base_commit,
        "datasets": [_entry(item) for item in spec.datasets],
        "evaluators": [_entry(item) for item in spec.evaluators],
        "issue_number": spec.issue_number,
        "schema_version": 1,
        "spec_sha256": spec.sha256,
    }
    provenance_path.write_text(
        json.dumps(provenance_document, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Evaluation result helpers
# ---------------------------------------------------------------------------


def _result(
    subject_id: str,
    split: DatasetSplit,
    *,
    agent: AgentVersionRef | None = None,
) -> EvaluationResult:
    value = _METRIC_VALUES[(subject_id, split.value)]
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
    case = NormalizedCase(
        case_id="case-1",
        case_hash="case-hash",
        response_ids=(f"response-{subject_id}-{split.value}",),
        scores=(NormalizedCaseMetric("quality", value, value, None, Outcome.PASS),),
        usage=Usage(),
        trajectory=None,
        error=None,
        duration_ms=1,
    )
    return EvaluationResult(
        run=run,
        cases=(case,),
        metrics={
            "quality": MetricAggregate(
                "quality", value, value, value, 0.0, Outcome.PASS, 1
            )
        },
        usage=Usage(),
        duration_ms=1,
        errors=(),
        complete=True,
        needs_repeat=False,
        attempts=1,
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


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 26, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, *, minutes: int) -> None:
        self.current += timedelta(minutes=minutes)


class FakeRepository:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.changed: dict[str, tuple[Path, ...]] = {}
        self.cleaned: list[str] = []
        self.committed: list[str] = []
        self.locks: list[str] = []
        self.released: list[str] = []
        self.reconciled: list[str] = []
        self.worktree_base = BASE_COMMIT

    def pin_default_branch(self, repository_root: Path) -> PinnedRepository:
        return PinnedRepository("main", BASE_COMMIT)

    def acquire_lock(self, **kwargs: object) -> CampaignLock:
        self.locks.append(str(kwargs.get("campaign_id")))
        return CampaignLock(str(kwargs["campaign_id"]))

    def release_lock(self, **kwargs: object) -> None:
        self.released.append(str(kwargs.get("campaign_id")))

    def _worktree(self, campaign_id: str, candidate_id: str) -> CampaignWorktree:
        path = contained_worktree_root(self.root, campaign_id) / candidate_id
        return CampaignWorktree(
            candidate_id,
            path,
            f"foundry-opt/{campaign_id}/{candidate_id}",
            self.worktree_base,
        )

    def create_worktree(
        self,
        repository_root: Path,
        campaign_id: str,
        candidate_id: str,
        base_commit: str,
    ) -> CampaignWorktree:
        worktree = self._worktree(campaign_id, candidate_id)
        worktree.path.mkdir(parents=True, exist_ok=True)
        return worktree

    def reconcile_worktree(
        self,
        repository_root: Path,
        campaign_id: str,
        candidate_id: str,
        base_commit: str,
    ) -> CampaignWorktree:
        import shutil

        worktree = self._worktree(campaign_id, candidate_id)
        if worktree.path.is_dir():
            shutil.rmtree(worktree.path, ignore_errors=True)
        self.reconciled.append(candidate_id)
        worktree.path.mkdir(parents=True, exist_ok=True)
        return worktree

    def open_worktree(
        self,
        repository_root: Path,
        campaign_id: str,
        candidate_id: str,
        base_commit: str,
    ) -> CampaignWorktree:
        worktree = self._worktree(campaign_id, candidate_id)
        if not worktree.path.is_dir():
            raise ValueError("worktree does not exist")
        return worktree

    def changed_paths(self, worktree: CampaignWorktree) -> tuple[Path, ...]:
        return self.changed.get(
            worktree.candidate_id, (Path("agent/instructions.md"),)
        )

    def reset_worktree(self, worktree: CampaignWorktree) -> None:
        pass

    def commit_worktree(self, worktree: CampaignWorktree, message: str) -> str:
        self.committed.append(worktree.candidate_id)
        return "c" * 40

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
            sha256=hashlib.sha256(
                worktree.candidate_id.encode()
            ).hexdigest(),
            base_commit=BASE_COMMIT,
            result_commit=result_commit,
        )

    def cleanup_worktree(
        self,
        repository_root: Path,
        worktree: CampaignWorktree,
    ) -> None:
        self.cleaned.append(worktree.candidate_id)
        if worktree.path.is_dir():
            import shutil

            shutil.rmtree(worktree.path, ignore_errors=True)


class FakeSpecGateway:
    def __init__(self, approved: bool = True, reason: str | None = None) -> None:
        self.approved = approved
        self.reason = reason
        self.calls: list[str] = []

    def verify_spec_approval(
        self,
        repository_root: Path,
        *,
        issue_number: int,
        spec: OptimizationSpec,
        spec_sha256: str,
        base_commit: str,
    ) -> SpecApprovalResult:
        self.calls.append(spec_sha256)
        if self.approved:
            return SpecApprovalResult(
                approved=True,
                default_branch="main",
                approval_commit=APPROVAL_COMMIT,
            )
        return SpecApprovalResult(
            approved=False,
            reason=self.reason or "the specification pull request is not merged",
        )


class FakeRegistrationGateway:
    def __init__(self) -> None:
        self.registered: list[tuple[str, str, str]] = []

    def register(self, *, kind, name, version, content) -> AssetIdentity:
        self.registered.append((kind.value, name, version))
        return AssetIdentity(
            remote_id=f"registered:{name}:{version}",
            name=name,
            version=version,
            content_sha256=None,
        )


@dataclass
class FakeSpecServiceResult:
    status: SpecServiceStatus
    issue_number: int
    spec_sha256: str | None = None
    pull_request: object | None = None
    blockers: tuple[str, ...] = ()
    failures: tuple[object, ...] = ()


class FakeSpecService:
    def __init__(self, result: FakeSpecServiceResult | None = None) -> None:
        self.result = result
        self.calls: list[int] = []

    def prepare_specification(self, repository_root: Path, issue_number: int):
        self.calls.append(issue_number)
        return self.result or FakeSpecServiceResult(
            status=SpecServiceStatus.COMPLETE,
            issue_number=issue_number,
            spec_sha256="9" * 64,
        )


class FakePublisher:
    def __init__(self) -> None:
        self.inputs: list[CampaignPublicationInputs] = []

    def publish(self, inputs: CampaignPublicationInputs):
        from foundry_opt.campaign.state import FinalizedPublication

        self.inputs.append(inputs)
        issue_numbers = {
            candidate_id: 200 + index
            for index, candidate_id in enumerate(
                inputs.report.pareto_candidate_ids
            )
        }
        return FinalizedPublication(
            campaign_pull_request_number=42,
            campaign_pull_request_url=(
                "https://github.com/octo-org/optimizer/pull/42"
            ),
            candidate_issue_numbers=issue_numbers,
        )


class RecordingEvaluator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def __call__(self, subject, split, attempt) -> EvaluationResult:
        self.calls.append((subject.subject_id, split.value, attempt))
        return _result(
            subject.subject_id,
            split,
            agent=subject.agent,
        )


class RepeatingBaselineEvaluator(RecordingEvaluator):
    def __call__(self, subject, split, attempt) -> EvaluationResult:
        result = super().__call__(subject, split, attempt)
        if (
            subject.subject_id != "baseline"
            or split is not DatasetSplit.DEVELOPMENT
        ):
            return result
        value = 0.6 if attempt == 1 else 0.8
        aggregate = replace(
            result.metrics["quality"],
            median=value,
            minimum=value,
            maximum=value,
        )
        score = replace(
            result.cases[0].scores[0],
            raw_score=value,
            normalized_score=value,
        )
        return replace(
            result,
            cases=(replace(result.cases[0], scores=(score,)),),
            metrics={"quality": aggregate},
            needs_repeat=attempt == 1,
        )


def _evidence_writer():
    def write_evidence(request) -> EvidenceManifest:
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_text("redacted", encoding="utf-8")
        return EvidenceManifest(
            path=request.output_path,
            sha256="e" * 64,
            byte_count=8,
            evaluation_ids=(),
            run_ids=(),
            goal_sha256=hashlib.sha256(
                request.goal.encode("utf-8")
            ).hexdigest(),
            spec_sha256=request.spec_sha256,
        )

    return write_evidence


def _validate(passed: bool = True):
    def validate(path: Path) -> ValidationReport:
        return ValidationReport(
            (
                ValidationResult(
                    ("test",), path, passed, 0 if passed else 1, "", ""
                ),
            ),
            discovered=True,
        )

    return validate


@dataclass
class Harness:
    root: Path
    clock: FakeClock
    repository: FakeRepository
    spec_gateway: FakeSpecGateway
    registration_gateway: FakeRegistrationGateway
    spec_service: FakeSpecService
    publisher: FakePublisher
    evaluator: RecordingEvaluator
    validate_passed: bool = True

    def dependencies(self) -> IssueOptimizationDependencies:
        return IssueOptimizationDependencies(
            config=_config(),
            spec_service=self.spec_service,
            spec_gateway=self.spec_gateway,
            registration_gateway=self.registration_gateway,
            repository=self.repository,
            validate=_validate(self.validate_passed),
            build_bundle=lambda root, output: _bundle(output),
            create_draft=(
                lambda target, subject_id, key, bundle: DraftRecord(
                    target, f"draft-{subject_id}", 1, bundle.sha256, "ready"
                )
            ),
            bind_evaluation=lambda spec, assets: self.evaluator,
            write_evidence=_evidence_writer(),
            publish=self.publisher,
            state=FileCampaignStateStore(),
            clock=self.clock,
        )

    def runner(self) -> IssueOptimizationRunner:
        return IssueOptimizationRunner(self.dependencies())


def _harness(root: Path, **overrides) -> Harness:
    clock = overrides.get("clock") or FakeClock()
    return Harness(
        root=root,
        clock=clock,
        repository=overrides.get("repository") or FakeRepository(root),
        spec_gateway=overrides.get("spec_gateway") or FakeSpecGateway(),
        registration_gateway=(
            overrides.get("registration_gateway") or FakeRegistrationGateway()
        ),
        spec_service=overrides.get("spec_service") or FakeSpecService(),
        publisher=overrides.get("publisher") or FakePublisher(),
        evaluator=overrides.get("evaluator") or RecordingEvaluator(),
        validate_passed=overrides.get("validate_passed", True),
    )


def _request(root: Path, phase: OptimizePhase, **kwargs) -> OptimizeCommandRequest:
    return OptimizeCommandRequest(
        repository_root=root,
        issue_number=kwargs.get("issue_number", 7),
        phase=phase,
        candidate_id=kwargs.get("candidate_id"),
        idea_file=kwargs.get("idea_file"),
    )


def _idea(root: Path, idea_id: str, parents: tuple[str, ...] = ()) -> Path:
    path = root / f"{idea_id}.json"
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


# ---------------------------------------------------------------------------
# RUN initialization
# ---------------------------------------------------------------------------


def test_run_establishes_baseline_and_awaits_agent(tmp_path: Path) -> None:
    _write_spec_bundle(tmp_path, _spec())
    harness = _harness(tmp_path)

    result = harness.runner().execute(_request(tmp_path, OptimizePhase.RUN))

    assert result.status is OptimizeCommandStatus.AWAITING_AGENT
    assert result.exit_code == 0
    assert "candidate request" in (result.next_action or "")
    # The baseline draft and development evaluation are established.
    assert harness.evaluator.calls == [("baseline", "development", 1)]
    assert harness.spec_gateway.calls == [_spec().sha256]
    state = FileCampaignStateStore().load(tmp_path, "issue-7")
    assert state is not None
    assert state.baseline_development is not None
    assert state.baseline_metrics == {"quality": 0.80}
    assert harness.repository.released == ["issue-7"]


def test_run_combines_requested_baseline_repeat(tmp_path: Path) -> None:
    _write_spec_bundle(tmp_path, _spec())
    evaluator = RepeatingBaselineEvaluator()
    harness = _harness(tmp_path, evaluator=evaluator)

    result = harness.runner().execute(
        _request(tmp_path, OptimizePhase.RUN)
    )

    assert result.status is OptimizeCommandStatus.AWAITING_AGENT
    assert evaluator.calls[:2] == [
        ("baseline", "development", 1),
        ("baseline", "development", 2),
    ]
    state = FileCampaignStateStore().load(tmp_path, "issue-7")
    assert state is not None
    assert state.baseline_development is not None
    assert state.baseline_development.attempts == 2
    assert state.baseline_development.metrics["quality"].median == 0.7


def test_run_blocks_when_spec_not_approved(tmp_path: Path) -> None:
    _write_spec_bundle(tmp_path, _spec())
    harness = _harness(tmp_path, spec_gateway=FakeSpecGateway(approved=False))

    result = harness.runner().execute(_request(tmp_path, OptimizePhase.RUN))

    assert result.status is OptimizeCommandStatus.BLOCKED
    assert result.exit_code == 1
    assert FileCampaignStateStore().load(tmp_path, "issue-7") is None


def test_run_blocks_when_spec_not_prepared(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    result = harness.runner().execute(_request(tmp_path, OptimizePhase.RUN))

    assert result.status is OptimizeCommandStatus.BLOCKED
    assert result.details["code"] == "spec_not_prepared"


def test_run_is_idempotent_after_baseline(tmp_path: Path) -> None:
    _write_spec_bundle(tmp_path, _spec())
    harness = _harness(tmp_path)
    runner = harness.runner()

    first = runner.execute(_request(tmp_path, OptimizePhase.RUN))
    second = harness.runner().execute(_request(tmp_path, OptimizePhase.RUN))

    assert first.status is OptimizeCommandStatus.AWAITING_AGENT
    assert second.status is OptimizeCommandStatus.AWAITING_AGENT
    # The baseline evaluation is not repeated on the second RUN.
    assert harness.evaluator.calls == [("baseline", "development", 1)]


# ---------------------------------------------------------------------------
# Full campaign happy path (two adaptive candidates)
# ---------------------------------------------------------------------------


def _run_two_candidate_campaign(harness: Harness) -> OptimizeCommandResult:
    root = harness.root
    runner = harness.runner()
    runner.execute(_request(root, OptimizePhase.RUN))

    request_1 = runner.execute(
        _request(root, OptimizePhase.CANDIDATE_REQUEST)
    )
    assert request_1.status is OptimizeCommandStatus.AWAITING_AGENT
    assert request_1.details["candidate_id"] == "candidate-1"

    submit_1 = runner.execute(
        _request(
            root,
            OptimizePhase.CANDIDATE_SUBMIT,
            candidate_id="candidate-1",
            idea_file=_idea(root, "idea-1"),
        )
    )
    assert submit_1.status is OptimizeCommandStatus.AWAITING_AGENT

    runner.execute(_request(root, OptimizePhase.CANDIDATE_REQUEST))
    submit_2 = runner.execute(
        _request(
            root,
            OptimizePhase.CANDIDATE_SUBMIT,
            candidate_id="candidate-2",
            idea_file=_idea(root, "idea-2", parents=("idea-1",)),
        )
    )
    assert "finalize" in (submit_2.next_action or "")

    return runner.execute(_request(root, OptimizePhase.RUN))


def test_full_two_candidate_campaign_finalizes_and_publishes(
    tmp_path: Path,
) -> None:
    _write_spec_bundle(tmp_path, _spec())
    harness = _harness(tmp_path)

    final = _run_two_candidate_campaign(harness)

    assert final.status is OptimizeCommandStatus.COMPLETE
    assert final.exit_code == 0
    assert final.details["campaign_pull_request"] == 42
    assert final.details["eligible_candidates"] == ["candidate-1"]
    # Exactly one campaign was published with the expected report.
    assert len(harness.publisher.inputs) == 1
    published = harness.publisher.inputs[0]
    assert published.report.pareto_candidate_ids == ("candidate-1",)
    assert published.validation_evidence is not None
    eligible = {
        candidate.candidate_id
        for candidate in published.report.candidates
        if candidate.eligible
    }
    assert eligible == {"candidate-1"}
    # Held-out validation runs only for the eligible development candidate.
    assert ("candidate-1", "validation", 1) in harness.evaluator.calls
    assert ("candidate-2", "validation", 1) not in harness.evaluator.calls


def test_finalize_is_idempotent(tmp_path: Path) -> None:
    _write_spec_bundle(tmp_path, _spec())
    harness = _harness(tmp_path)

    _run_two_candidate_campaign(harness)
    repeated = harness.runner().execute(_request(tmp_path, OptimizePhase.RUN))

    assert repeated.status is OptimizeCommandStatus.COMPLETE
    # Publication is not repeated once the campaign is finalized.
    assert len(harness.publisher.inputs) == 1


def test_finalize_reuses_heldout_evaluations_after_publish_failure(
    tmp_path: Path,
) -> None:
    from foundry_opt.optimization.runner import CapabilityUnavailableError

    class FlakyPublisher(FakePublisher):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def publish(self, inputs: CampaignPublicationInputs):
            self.attempts += 1
            if self.attempts == 1:
                raise CapabilityUnavailableError(
                    "campaign_publication_unavailable",
                    "publication is temporarily unavailable",
                )
            return super().publish(inputs)

    _write_spec_bundle(tmp_path, _spec())
    publisher = FlakyPublisher()
    harness = _harness(tmp_path, publisher=publisher)
    runner = harness.runner()
    runner.execute(_request(tmp_path, OptimizePhase.RUN))
    runner.execute(_request(tmp_path, OptimizePhase.CANDIDATE_REQUEST))
    runner.execute(
        _request(
            tmp_path,
            OptimizePhase.CANDIDATE_SUBMIT,
            candidate_id="candidate-1",
            idea_file=_idea(tmp_path, "idea-1"),
        )
    )
    runner.execute(_request(tmp_path, OptimizePhase.CANDIDATE_REQUEST))
    runner.execute(
        _request(
            tmp_path,
            OptimizePhase.CANDIDATE_SUBMIT,
            candidate_id="candidate-2",
            idea_file=_idea(tmp_path, "idea-2", parents=("idea-1",)),
        )
    )

    blocked = runner.execute(_request(tmp_path, OptimizePhase.RUN))
    assert blocked.status is OptimizeCommandStatus.BLOCKED
    completed = harness.runner().execute(_request(tmp_path, OptimizePhase.RUN))
    assert completed.status is OptimizeCommandStatus.COMPLETE

    # Held-out validation for the eligible candidate runs exactly once even
    # though finalization was attempted twice.
    validation_calls = [
        call
        for call in harness.evaluator.calls
        if call == ("candidate-1", "validation", 1)
    ]
    assert len(validation_calls) == 1


def test_finalize_reuses_heldout_winner_after_deadline_publish_retry(
    tmp_path: Path,
) -> None:
    from foundry_opt.optimization.runner import CapabilityUnavailableError

    class FlakyPublisher(FakePublisher):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def publish(self, inputs: CampaignPublicationInputs):
            self.attempts += 1
            if self.attempts == 1:
                raise CapabilityUnavailableError(
                    "campaign_publication_unavailable",
                    "publication is temporarily unavailable",
                )
            return super().publish(inputs)

    _write_spec_bundle(tmp_path, _spec())
    clock = FakeClock()
    publisher = FlakyPublisher()
    harness = _harness(
        tmp_path,
        clock=clock,
        publisher=publisher,
    )
    runner = harness.runner()
    runner.execute(_request(tmp_path, OptimizePhase.RUN))
    runner.execute(_request(tmp_path, OptimizePhase.CANDIDATE_REQUEST))
    runner.execute(
        _request(
            tmp_path,
            OptimizePhase.CANDIDATE_SUBMIT,
            candidate_id="candidate-1",
            idea_file=_idea(tmp_path, "idea-1"),
        )
    )
    runner.execute(_request(tmp_path, OptimizePhase.CANDIDATE_REQUEST))
    runner.execute(
        _request(
            tmp_path,
            OptimizePhase.CANDIDATE_SUBMIT,
            candidate_id="candidate-2",
            idea_file=_idea(tmp_path, "idea-2", parents=("idea-1",)),
        )
    )

    blocked = runner.execute(_request(tmp_path, OptimizePhase.RUN))
    state_after_failure = FileCampaignStateStore().load(
        tmp_path,
        "issue-7",
    )
    assert blocked.status is OptimizeCommandStatus.BLOCKED
    assert state_after_failure is not None
    expected_ids = state_after_failure.pareto_candidate_ids
    assert expected_ids == ("candidate-1",)
    assert state_after_failure.baseline_validation is not None

    clock.advance(minutes=60)
    completed = runner.execute(_request(tmp_path, OptimizePhase.RUN))
    final_state = FileCampaignStateStore().load(tmp_path, "issue-7")

    assert completed.status is OptimizeCommandStatus.COMPLETE
    assert completed.details["eligible_candidates"] == list(expected_ids)
    assert final_state is not None
    assert final_state.pareto_candidate_ids == expected_ids
    assert publisher.inputs[-1].report.pareto_candidate_ids == expected_ids
    assert publisher.inputs[-1].validation_evidence is not None


# ---------------------------------------------------------------------------
# Process restart between every step
# ---------------------------------------------------------------------------


def test_process_restart_between_every_step(tmp_path: Path) -> None:
    _write_spec_bundle(tmp_path, _spec())
    clock = FakeClock()
    repository = FakeRepository(tmp_path)
    publisher = FakePublisher()
    evaluator = RecordingEvaluator()

    def fresh() -> IssueOptimizationRunner:
        return _harness(
            tmp_path,
            clock=clock,
            repository=repository,
            publisher=publisher,
            evaluator=evaluator,
        ).runner()

    fresh().execute(_request(tmp_path, OptimizePhase.RUN))
    fresh().execute(_request(tmp_path, OptimizePhase.CANDIDATE_REQUEST))
    fresh().execute(
        _request(
            tmp_path,
            OptimizePhase.CANDIDATE_SUBMIT,
            candidate_id="candidate-1",
            idea_file=_idea(tmp_path, "idea-1"),
        )
    )
    fresh().execute(_request(tmp_path, OptimizePhase.CANDIDATE_REQUEST))
    fresh().execute(
        _request(
            tmp_path,
            OptimizePhase.CANDIDATE_SUBMIT,
            candidate_id="candidate-2",
            idea_file=_idea(tmp_path, "idea-2", parents=("idea-1",)),
        )
    )
    final = fresh().execute(_request(tmp_path, OptimizePhase.RUN))

    assert final.status is OptimizeCommandStatus.COMPLETE
    assert final.details["eligible_candidates"] == ["candidate-1"]
    # The development evaluations are each run exactly once despite restarts.
    development_calls = [
        call for call in evaluator.calls if call[1] == "development"
    ]
    assert development_calls.count(("candidate-1", "development", 1)) == 1
    assert development_calls.count(("baseline", "development", 1)) == 1


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_duplicate_request_returns_same_worktree(tmp_path: Path) -> None:
    _write_spec_bundle(tmp_path, _spec())
    harness = _harness(tmp_path)
    runner = harness.runner()
    runner.execute(_request(tmp_path, OptimizePhase.RUN))

    first = runner.execute(_request(tmp_path, OptimizePhase.CANDIDATE_REQUEST))
    second = runner.execute(_request(tmp_path, OptimizePhase.CANDIDATE_REQUEST))

    assert first.details["candidate_id"] == "candidate-1"
    assert second.details["candidate_id"] == "candidate-1"
    assert first.details["worktree"] == second.details["worktree"]
    state = FileCampaignStateStore().load(tmp_path, "issue-7")
    assert state is not None
    assert state.launched_slots == 1


def test_duplicate_submit_is_idempotent(tmp_path: Path) -> None:
    _write_spec_bundle(tmp_path, _spec())
    harness = _harness(tmp_path)
    runner = harness.runner()
    runner.execute(_request(tmp_path, OptimizePhase.RUN))
    runner.execute(_request(tmp_path, OptimizePhase.CANDIDATE_REQUEST))
    idea = _idea(tmp_path, "idea-1")

    first = runner.execute(
        _request(
            tmp_path,
            OptimizePhase.CANDIDATE_SUBMIT,
            candidate_id="candidate-1",
            idea_file=idea,
        )
    )
    second = runner.execute(
        _request(
            tmp_path,
            OptimizePhase.CANDIDATE_SUBMIT,
            candidate_id="candidate-1",
            idea_file=idea,
        )
    )

    assert first.status is OptimizeCommandStatus.AWAITING_AGENT
    assert second.status is OptimizeCommandStatus.AWAITING_AGENT
    # The candidate development evaluation runs exactly once.
    assert harness.evaluator.calls.count(("candidate-1", "development", 1)) == 1


# ---------------------------------------------------------------------------
# Idea contract + security
# ---------------------------------------------------------------------------


def _prepare_awaiting_candidate(harness: Harness) -> IssueOptimizationRunner:
    runner = harness.runner()
    runner.execute(_request(harness.root, OptimizePhase.RUN))
    runner.execute(_request(harness.root, OptimizePhase.CANDIDATE_REQUEST))
    return runner


def test_malicious_idea_json_is_rejected(tmp_path: Path) -> None:
    _write_spec_bundle(tmp_path, _spec())
    harness = _harness(tmp_path)
    runner = _prepare_awaiting_candidate(harness)
    malicious = tmp_path / "idea.json"
    malicious.write_text(
        json.dumps(
            {
                "idea_id": "idea-1",
                "mutation_class": "system_instructions",
                "parent_idea_ids": [],
                "required_opt_ins": [],
                "status": "evaluated",
                "metrics": {"quality": 1.0},
            }
        ),
        encoding="utf-8",
    )

    result = runner.execute(
        _request(
            tmp_path,
            OptimizePhase.CANDIDATE_SUBMIT,
            candidate_id="candidate-1",
            idea_file=malicious,
        )
    )

    assert result.status is OptimizeCommandStatus.AWAITING_AGENT
    assert result.details["candidate_status"] == "guardrail_rejected"
    state = FileCampaignStateStore().load(tmp_path, "issue-7")
    assert state is not None
    candidate = state.candidates[0]
    assert candidate.status == "guardrail_rejected"
    # No agent-supplied metrics reach the persisted candidate state.
    assert dict(candidate.metrics) == {}


def test_idea_outside_repository_is_rejected(tmp_path: Path) -> None:
    _write_spec_bundle(tmp_path, _spec())
    harness = _harness(tmp_path)
    runner = _prepare_awaiting_candidate(harness)
    outside = tmp_path.parent / "outside-idea.json"
    outside.write_text("{}", encoding="utf-8")

    result = runner.execute(
        _request(
            tmp_path,
            OptimizePhase.CANDIDATE_SUBMIT,
            candidate_id="candidate-1",
            idea_file=outside,
        )
    )

    assert result.details["candidate_status"] == "guardrail_rejected"


def test_idea_symlink_component_is_rejected(tmp_path: Path) -> None:
    _write_spec_bundle(tmp_path, _spec())
    harness = _harness(tmp_path)
    runner = _prepare_awaiting_candidate(harness)
    real = tmp_path / "real"
    real.mkdir()
    (real / "idea.json").write_text("{}", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not supported in this environment")

    result = runner.execute(
        _request(
            tmp_path,
            OptimizePhase.CANDIDATE_SUBMIT,
            candidate_id="candidate-1",
            idea_file=link / "idea.json",
        )
    )

    assert result.details["candidate_status"] == "guardrail_rejected"


def test_disallowed_changed_path_rejects_candidate(tmp_path: Path) -> None:
    _write_spec_bundle(tmp_path, _spec())
    harness = _harness(tmp_path)
    harness.repository.changed["candidate-1"] = (Path("secrets/config.env"),)
    runner = _prepare_awaiting_candidate(harness)

    result = runner.execute(
        _request(
            tmp_path,
            OptimizePhase.CANDIDATE_SUBMIT,
            candidate_id="candidate-1",
            idea_file=_idea(tmp_path, "idea-1"),
        )
    )

    assert result.details["candidate_status"] == "guardrail_rejected"
    assert "candidate-1" in harness.repository.cleaned


def test_disallowed_mutation_rejects_candidate(tmp_path: Path) -> None:
    _write_spec_bundle(tmp_path, _spec())
    harness = _harness(tmp_path)
    runner = _prepare_awaiting_candidate(harness)
    idea = tmp_path / "idea.json"
    idea.write_text(
        json.dumps(
            {
                "idea_id": "idea-1",
                "mutation_class": "model",
                "parent_idea_ids": [],
                "required_opt_ins": [],
            }
        ),
        encoding="utf-8",
    )

    result = runner.execute(
        _request(
            tmp_path,
            OptimizePhase.CANDIDATE_SUBMIT,
            candidate_id="candidate-1",
            idea_file=idea,
        )
    )

    assert result.details["candidate_status"] == "guardrail_rejected"


def test_validation_failure_rejects_candidate(tmp_path: Path) -> None:
    _write_spec_bundle(tmp_path, _spec())
    harness = _harness(tmp_path, validate_passed=False)
    runner = _prepare_awaiting_candidate(harness)

    result = runner.execute(
        _request(
            tmp_path,
            OptimizePhase.CANDIDATE_SUBMIT,
            candidate_id="candidate-1",
            idea_file=_idea(tmp_path, "idea-1"),
        )
    )

    assert result.details["candidate_status"] == "validation_failed"


def test_base_mismatch_rejects_candidate(tmp_path: Path) -> None:
    _write_spec_bundle(tmp_path, _spec())
    harness = _harness(tmp_path)
    runner = _prepare_awaiting_candidate(harness)
    # A worktree whose base drifted from the campaign base must be rejected.
    harness.repository.worktree_base = "e" * 40

    result = runner.execute(
        _request(
            tmp_path,
            OptimizePhase.CANDIDATE_SUBMIT,
            candidate_id="candidate-1",
            idea_file=_idea(tmp_path, "idea-1"),
        )
    )

    assert result.details["candidate_status"] == "guardrail_rejected"


def test_deadline_rejects_submission(tmp_path: Path) -> None:
    _write_spec_bundle(tmp_path, _spec())
    harness = _harness(tmp_path)
    runner = _prepare_awaiting_candidate(harness)
    harness.clock.advance(minutes=60)

    result = runner.execute(
        _request(
            tmp_path,
            OptimizePhase.CANDIDATE_SUBMIT,
            candidate_id="candidate-1",
            idea_file=_idea(tmp_path, "idea-1"),
        )
    )

    assert result.details["candidate_status"] == "deadline_exceeded"


def test_cutoff_blocks_new_candidate(tmp_path: Path) -> None:
    _write_spec_bundle(tmp_path, _spec())
    harness = _harness(tmp_path)
    runner = harness.runner()
    runner.execute(_request(tmp_path, OptimizePhase.RUN))
    harness.clock.advance(minutes=45)

    result = runner.execute(_request(tmp_path, OptimizePhase.CANDIDATE_REQUEST))

    assert result.status is OptimizeCommandStatus.AWAITING_AGENT
    assert "finalize" in (result.next_action or "")
    state = FileCampaignStateStore().load(tmp_path, "issue-7")
    assert state is not None
    assert state.launched_slots == 0


# ---------------------------------------------------------------------------
# Privacy: no raw held-out data in the context
# ---------------------------------------------------------------------------


def test_context_contains_no_raw_evaluation_data(tmp_path: Path) -> None:
    _write_spec_bundle(tmp_path, _spec())
    harness = _harness(tmp_path)
    runner = harness.runner()
    runner.execute(_request(tmp_path, OptimizePhase.RUN))
    runner.execute(_request(tmp_path, OptimizePhase.CANDIDATE_REQUEST))

    context_path = (
        tmp_path
        / ".foundry-optimizer"
        / "campaigns"
        / "issue-7"
        / "candidates"
        / "candidate-1"
        / "context.json"
    )
    document = json.loads(context_path.read_text(encoding="utf-8"))
    text = context_path.read_text(encoding="utf-8")
    assert document["goal"] == GOAL
    assert document["baseline_metrics"] == {"quality": 0.80}
    assert set(document["allowed_mutations"]) == {"system_instructions"}
    # No raw response ids, dataset rows, prompts, or secrets leak through.
    assert "response-" not in text
    assert "case-hash" not in text
    assert "cutoff_at" in document and "deadline_at" in document


# ---------------------------------------------------------------------------
# State mismatch / orphan invariants
# ---------------------------------------------------------------------------


def test_state_mismatch_fails_campaign(tmp_path: Path) -> None:
    _write_spec_bundle(tmp_path, _spec())
    harness = _harness(tmp_path)
    harness.runner().execute(_request(tmp_path, OptimizePhase.RUN))

    # Rewrite the spec goal so the persisted campaign no longer matches.
    mutated = OptimizationSpec(
        **{
            **_spec().model_dump(),
            "goal": (
                "Completely different optimization goal that must not match "
                "the started campaign at all."
            ),
        }
    )
    _write_spec_bundle(tmp_path, mutated)

    result = harness.runner().execute(_request(tmp_path, OptimizePhase.RUN))

    assert result.status is OptimizeCommandStatus.BLOCKED
    assert result.details["code"] == "campaign_state_mismatch"
    state = FileCampaignStateStore().load(tmp_path, "issue-7")
    assert state is not None
    assert state.status == "failed"


# ---------------------------------------------------------------------------
# AUTO routing
# ---------------------------------------------------------------------------


def test_auto_routes_to_spec_when_unprepared(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    result = harness.runner().execute(_request(tmp_path, OptimizePhase.AUTO))

    assert harness.spec_service.calls == [7]
    assert result.phase is OptimizePhase.SPEC


def test_auto_routes_to_run_then_request_then_finalize(tmp_path: Path) -> None:
    _write_spec_bundle(tmp_path, _spec())
    harness = _harness(tmp_path)
    runner = harness.runner()

    first = runner.execute(_request(tmp_path, OptimizePhase.AUTO))
    assert first.status is OptimizeCommandStatus.AWAITING_AGENT
    assert "candidate request" in (first.next_action or "")

    second = runner.execute(_request(tmp_path, OptimizePhase.AUTO))
    assert "candidate request" in (second.next_action or "")


# ---------------------------------------------------------------------------
# SPEC delegation + delegated phases
# ---------------------------------------------------------------------------


def test_spec_delegates_to_specification_service(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    result = harness.runner().execute(_request(tmp_path, OptimizePhase.SPEC))

    assert harness.spec_service.calls == [7]
    assert result.status is OptimizeCommandStatus.COMPLETE
    assert result.phase is OptimizePhase.SPEC


def test_spec_blocked_result_is_surfaced(tmp_path: Path) -> None:
    harness = _harness(
        tmp_path,
        spec_service=FakeSpecService(
            FakeSpecServiceResult(
                status=SpecServiceStatus.BLOCKED,
                issue_number=7,
                blockers=("the issue is missing a goal",),
            )
        ),
    )

    result = harness.runner().execute(_request(tmp_path, OptimizePhase.SPEC))

    assert result.status is OptimizeCommandStatus.BLOCKED
    assert "missing a goal" in result.summary


def test_apply_and_reconcile_are_blocked_without_wiring(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    apply = harness.runner().execute(
        OptimizeCommandRequest(
            repository_root=tmp_path,
            issue_number=7,
            phase=OptimizePhase.APPLY,
            candidate_id="candidate-1",
        )
    )
    reconcile = harness.runner().execute(
        _request(tmp_path, OptimizePhase.RECONCILE)
    )

    assert apply.status is OptimizeCommandStatus.BLOCKED
    assert apply.details["code"] == "apply_not_wired"
    assert reconcile.status is OptimizeCommandStatus.BLOCKED
    assert reconcile.details["code"] == "reconcile_not_wired"


# ---------------------------------------------------------------------------
# Asset materialization + lossless persistence
# ---------------------------------------------------------------------------


def _spec_with_registration() -> tuple[OptimizationSpec, bytes, bytes]:
    dev_content = b'{"query":"q","expected_behavior":"b"}\n'
    evaluator_content = b"def evaluate(candidate):\n    return 1.0\n"
    spec = OptimizationSpec(
        issue_number=7,
        repository="octo-org/optimizer",
        base_commit=BASE_COMMIT,
        target="support_agent",
        environment="acceptance",
        base_agent_version="12",
        goal=GOAL,
        datasets=(
            AssetProvenance(
                asset_id="dataset-dev",
                kind=AssetKind.DATASET,
                source="synthetic",
                role="development",
                content_sha256=hashlib.sha256(dev_content).hexdigest(),
                created_by="synthetic-dataset-provider",
                approval_gate=ApprovalGate.POLICY,
            ),
            AssetProvenance(
                asset_id="dataset-val",
                kind=AssetKind.DATASET,
                source="foundry",
                role="validation",
                name="val-dataset",
                version="1",
                created_by="foundry-existing-asset-provider",
                remote_id="foundry-dataset-val",
            ),
        ),
        evaluators=(
            AssetProvenance(
                asset_id="evaluator-quality",
                kind=AssetKind.EVALUATOR,
                source="custom",
                name="quality",
                version="1",
                content_sha256=hashlib.sha256(evaluator_content).hexdigest(),
                created_by="custom-evaluator-provider",
                approval_gate=ApprovalGate.POLICY,
                metrics=("quality",),
            ),
        ),
        metrics={
            "quality": {
                "direction": "maximize",
                "threshold": 0.8,
                "materiality": 0.05,
                "hard_guardrail": False,
                "undefined_behavior": "fail",
            }
        },
        allowed_mutations=frozenset({"system_instructions"}),
    )
    return spec, dev_content, evaluator_content


def test_run_registers_synthetic_and_custom_assets(tmp_path: Path) -> None:
    spec, dev_content, evaluator_content = _spec_with_registration()
    dev_path = ".foundry-optimizer/specs/issue-7/assets/dataset-dev.jsonl"
    evaluator_path = "agent/evaluators/quality.py"
    (tmp_path / dev_path).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / dev_path).write_bytes(dev_content)
    (tmp_path / evaluator_path).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / evaluator_path).write_bytes(evaluator_content)
    _write_spec_bundle(
        tmp_path,
        spec,
        {
            "dataset-dev": dev_path,
            "dataset-val": None,
            "evaluator-quality": evaluator_path,
        },
    )
    harness = _harness(tmp_path)

    result = harness.runner().execute(_request(tmp_path, OptimizePhase.RUN))

    assert result.status is OptimizeCommandStatus.AWAITING_AGENT
    # The synthetic dataset and custom evaluator are registered; the existing
    # foundry dataset is a no-op.
    assert len(harness.registration_gateway.registered) == 2
    state = FileCampaignStateStore().load(tmp_path, "issue-7")
    assert state is not None
    remote_ids = {asset.asset_id: asset.remote_id for asset in state.assets}
    assert remote_ids["dataset-dev"].startswith("registered:")
    assert remote_ids["evaluator-quality"].startswith("registered:")
    assert remote_ids["dataset-val"] == "foundry-dataset-val"


def test_resume_does_not_re_register_assets(tmp_path: Path) -> None:
    spec, dev_content, evaluator_content = _spec_with_registration()
    dev_path = ".foundry-optimizer/specs/issue-7/assets/dataset-dev.jsonl"
    evaluator_path = "agent/evaluators/quality.py"
    (tmp_path / dev_path).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / dev_path).write_bytes(dev_content)
    (tmp_path / evaluator_path).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / evaluator_path).write_bytes(evaluator_content)
    _write_spec_bundle(
        tmp_path,
        spec,
        {
            "dataset-dev": dev_path,
            "dataset-val": None,
            "evaluator-quality": evaluator_path,
        },
    )
    harness = _harness(tmp_path)

    harness.runner().execute(_request(tmp_path, OptimizePhase.RUN))
    # A resume RUN must reuse the persisted assets, not re-register them.
    harness.runner().execute(_request(tmp_path, OptimizePhase.RUN))

    assert len(harness.registration_gateway.registered) == 2


def test_run_blocks_when_asset_content_tampered(tmp_path: Path) -> None:
    spec, _, evaluator_content = _spec_with_registration()
    dev_path = ".foundry-optimizer/specs/issue-7/assets/dataset-dev.jsonl"
    evaluator_path = "agent/evaluators/quality.py"
    (tmp_path / dev_path).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / dev_path).write_bytes(b"tampered dataset content\n")
    (tmp_path / evaluator_path).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / evaluator_path).write_bytes(evaluator_content)
    _write_spec_bundle(
        tmp_path,
        spec,
        {
            "dataset-dev": dev_path,
            "dataset-val": None,
            "evaluator-quality": evaluator_path,
        },
    )
    harness = _harness(tmp_path)

    result = harness.runner().execute(_request(tmp_path, OptimizePhase.RUN))

    assert result.status is OptimizeCommandStatus.BLOCKED
    assert result.details["code"] == "asset_content_tampered"


def test_persisted_evaluation_results_are_lossless(tmp_path: Path) -> None:
    _write_spec_bundle(tmp_path, _spec())
    harness = _harness(tmp_path)
    runner = harness.runner()
    runner.execute(_request(tmp_path, OptimizePhase.RUN))
    runner.execute(_request(tmp_path, OptimizePhase.CANDIDATE_REQUEST))
    runner.execute(
        _request(
            tmp_path,
            OptimizePhase.CANDIDATE_SUBMIT,
            candidate_id="candidate-1",
            idea_file=_idea(tmp_path, "idea-1"),
        )
    )

    state = FileCampaignStateStore().load(tmp_path, "issue-7")
    assert state is not None
    assert state.baseline_development == _result(
        "baseline",
        DatasetSplit.DEVELOPMENT,
        agent=AgentVersionRef(
            "support_agent",
            "draft-baseline",
            "draft-baseline",
        ),
    )
    candidate = state.candidates[0]
    assert candidate.development_result == _result(
        "candidate-1",
        DatasetSplit.DEVELOPMENT,
        agent=AgentVersionRef(
            "support_agent",
            "draft-candidate-1",
            "draft-candidate-1",
        ),
    )
    assert candidate.provisional_eligible is True


def test_orphaned_awaiting_idea_resumes_after_restart(tmp_path: Path) -> None:
    _write_spec_bundle(tmp_path, _spec())
    clock = FakeClock()
    repository = FakeRepository(tmp_path)
    _harness(tmp_path, clock=clock, repository=repository).runner().execute(
        _request(tmp_path, OptimizePhase.RUN)
    )
    _harness(tmp_path, clock=clock, repository=repository).runner().execute(
        _request(tmp_path, OptimizePhase.CANDIDATE_REQUEST)
    )

    # A fresh process re-requests: the reserved slot resumes, not a new one.
    resumed = _harness(
        tmp_path, clock=clock, repository=repository
    ).runner().execute(_request(tmp_path, OptimizePhase.CANDIDATE_REQUEST))

    assert resumed.details["candidate_id"] == "candidate-1"
    state = FileCampaignStateStore().load(tmp_path, "issue-7")
    assert state is not None
    assert state.launched_slots == 1
    assert state.awaiting_candidate_id == "candidate-1"
