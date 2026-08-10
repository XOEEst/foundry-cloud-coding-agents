from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from foundry_opt.campaign.models import CampaignLimits, PatchArtifact
from foundry_opt.campaign.protocols import CampaignWorktree, PinnedRepository
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
from foundry_opt.evidence import (
    EvaluationAssetReference,
    EvidenceManifest,
)
from foundry_opt.orchestration import (
    AdvanceRequest,
    CampaignEvent,
    CampaignPhase,
    EventKind,
    OptimizationCampaign,
    OutboxRecord,
    StateObject,
    StateRefPrivacyError,
    StateRefSnapshot,
)
from foundry_opt.orchestration.candidate_workers import (
    CandidateAssetsRegistrationPending,
    CandidateAssetsRegistrationPlan,
    CandidateDesignPending,
    CandidateDesignArtifact,
    CandidateDesignIntent,
    CandidateDesignResult,
    CandidateDesignSubmissionRequest,
    CandidateDesignSubmissionService,
    CandidateDesignSubmissionStatus,
    CandidateDraftEffects,
    CandidateEffectPending,
    CandidateEvaluationEffects,
    CandidateWorkerDependencies,
    CandidateWorkerPlan,
    CandidateWorkerRequest,
    CandidateWorkerService,
    CandidateWorkerStatus,
    _candidate_design_submission_record,
    _evaluation_intent,
)
from foundry_opt.packaging import BundleArtifact, ValidationReport
from foundry_opt.packaging import ValidationResult


NOW = datetime(2026, 7, 31, 18, 0, tzinfo=UTC)
SPEC_SHA256 = "a" * 64
BASE_COMMIT = "b" * 40


def test_candidate_assets_are_durably_delegated_before_any_worker_adapter(
    tmp_path: Path,
) -> None:
    effect_id = f"assets-31-1-{SPEC_SHA256[:16]}"
    intent = StateObject(
        f"objects/capabilities/{effect_id}.json",
        (
            json.dumps(
                {
                    "assets": [
                        {
                            "asset_id": "development",
                            "content_sha256": "d" * 64,
                            "kind": "dataset",
                            "path": "evaluation/development.jsonl",
                            "role": "development",
                            "source": "repository",
                        }
                    ],
                    "base_commit": BASE_COMMIT,
                    "environment": "acceptance",
                    "generation": 1,
                    "issue_number": 31,
                    "kind": "candidate_assets_registration",
                    "schema_version": 1,
                    "spec_sha256": SPEC_SHA256,
                    "target": "support",
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode(),
    )
    registration = CandidateAssetsRegistrationPlan(
        effect_id=effect_id,
        issue_number=31,
        generation=1,
        spec_sha256=SPEC_SHA256,
        base_commit=BASE_COMMIT,
        target="support",
        environment="acceptance",
        max_attempts=2,
        intent=intent,
    )

    class PendingResolver:
        def resolve(self, request, state):
            raise CandidateAssetsRegistrationPending(registration)

    class NoWorkerRepository(Repository):
        def pin_default_branch(self, repository_root):
            raise AssertionError("candidate repository must not be touched")

    class NoDrafts:
        def reconcile(self, intent):
            raise AssertionError("Foundry draft adapter must not be called")

        create = reconcile

    class NoEvaluations:
        def reconcile(self, intent):
            raise AssertionError(
                "Foundry evaluation adapter must not be called"
            )

        run = reconcile

    ledger = Ledger(_seed_snapshot())
    service = CandidateWorkerService(
        ledger=ledger,
        resolver=PendingResolver(),
        dependencies=CandidateWorkerDependencies(
            repository=NoWorkerRepository(tmp_path),
            designer=DeferredDesigner(),
            validate=lambda path: (_ for _ in ()).throw(
                AssertionError("validation must not run")
            ),
            build_bundle=lambda root, output: (_ for _ in ()).throw(
                AssertionError("bundle creation must not run")
            ),
            drafts=NoDrafts(),
            evaluations=NoEvaluations(),
            write_evidence=lambda request: (_ for _ in ()).throw(
                AssertionError("evidence must not be written")
            ),
            clock=Clock(),
        ),
    )

    first = service.advance(CandidateWorkerRequest(tmp_path, 31))
    second = service.advance(CandidateWorkerRequest(tmp_path, 31))

    assert first.status is CandidateWorkerStatus.WAITING
    assert first.code == "candidate_assets_registration_pending"
    assert second.status is CandidateWorkerStatus.WAITING
    assert ledger.commits == 1
    assert first.snapshot.objects[-1] == intent
    planned = first.snapshot.outbox[-1]
    assert planned.kind == "candidate_assets_registration_planned"
    assert dict(planned.payload) == {
        "base_commit": BASE_COMMIT,
        "capability_path": intent.path,
        "capability_sha256": intent.sha256,
        "effect_id": effect_id,
        "effect_kind": "foundry_assets",
        "environment": "acceptance",
        "issue_number": 31,
        "max_attempts": 2,
        "spec_sha256": SPEC_SHA256,
        "target": "support",
    }


def test_evaluation_idempotency_binds_campaign_generation_and_draft() -> None:
    plan = _plan()
    draft = DraftRecord(
        plan.target,
        "draft-baseline",
        plan.base_agent_version,
        "e" * 64,
        "draft",
    )

    first = _evaluation_intent(plan, "baseline", draft)
    duplicate = _evaluation_intent(plan, "baseline", draft)
    new_generation = _evaluation_intent(
        replace(
            plan,
            generation=2,
            spec_sha256="c" * 64,
        ),
        "baseline",
        draft,
    )
    new_draft = _evaluation_intent(
        plan,
        "baseline",
        replace(draft, version_id="draft-baseline-2"),
    )
    new_base = _evaluation_intent(
        replace(plan, base_commit="d" * 40),
        "baseline",
        draft,
    )
    new_dataset = _evaluation_intent(
        replace(
            plan,
            assets=(
                replace(plan.assets[0], remote_id="foundry-dev-2"),
                *plan.assets[1:],
            ),
        ),
        "baseline",
        draft,
    )

    assert first.idempotency_key == duplicate.idempotency_key
    assert len(first.idempotency_key) == 64
    assert first.subject.idempotency_key == first.idempotency_key
    assert new_generation.idempotency_key != first.idempotency_key
    assert new_draft.idempotency_key != first.idempotency_key
    assert new_base.idempotency_key != first.idempotency_key
    assert new_dataset.idempotency_key != first.idempotency_key


def test_legacy_evaluation_plan_without_key_resumes_compatibly(
    tmp_path: Path,
) -> None:
    plan = _plan()
    intent = _evaluation_intent(
        plan,
        "baseline",
        DraftRecord(
            plan.target,
            "draft-baseline",
            plan.base_agent_version,
            "e" * 64,
            "draft",
        ),
    )
    snapshot = _seed_snapshot()
    legacy = OutboxRecord(
        intent.effect_id,
        "candidate_effect_planned",
        1,
        snapshot.state.sequence,
        {
            "base_commit": plan.base_commit,
            "candidate_id": "baseline",
            "effect_id": intent.effect_id,
            "effect_kind": "foundry_evaluation",
            "issue_number": 31,
            "max_attempts": 1,
            "slot": 0,
            "spec_sha256": plan.spec_sha256,
        },
    )
    ledger = Ledger(
        StateRefSnapshot(
            snapshot.revision,
            snapshot.state,
            snapshot.inbox,
            (legacy,),
        )
    )
    service = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(plan),
        dependencies=CandidateWorkerDependencies(
            repository=Repository(tmp_path),
            designer=DeferredDesigner(),
            validate=lambda path: ValidationReport((), False),
            build_bundle=lambda root, output: pytest.fail(
                "legacy plan check must not build"
            ),
            drafts=Drafts(),
            evaluations=Evaluations(),
            write_evidence=lambda request: pytest.fail(
                "legacy plan check must not write evidence"
            ),
            clock=Clock(),
        ),
    )

    result = service._plan_effect(
        CandidateWorkerRequest(tmp_path, 31),
        ledger.snapshot,
        intent.effect_id,
        "foundry_evaluation",
        "baseline",
        0,
        plan,
        idempotency_key=intent.idempotency_key,
    )

    assert result is ledger.snapshot
    assert ledger.commits == 0


def test_foundry_draft_is_deferred_after_its_durable_effect_plan(
    tmp_path: Path,
) -> None:
    class PendingDrafts:
        def reconcile(self, intent):
            return None

        def create(self, intent):
            raise CandidateEffectPending("foundry_draft")

    class NoEvaluations:
        def reconcile(self, intent):
            raise AssertionError("evaluation must wait for the draft result")

        run = reconcile

    def build_bundle(root: Path, output: Path) -> BundleArtifact:
        return BundleArtifact(
            output,
            "e" * 64,
            ("agent/instructions.md",),
            (),
            1,
            output.with_suffix(".manifest.json"),
        )

    ledger = Ledger(_seed_snapshot())
    result = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(_plan()),
        dependencies=CandidateWorkerDependencies(
            repository=Repository(tmp_path),
            designer=DeferredDesigner(),
            validate=lambda path: ValidationReport((), False),
            build_bundle=build_bundle,
            drafts=PendingDrafts(),
            evaluations=NoEvaluations(),
            write_evidence=lambda request: (_ for _ in ()).throw(
                AssertionError("evidence must wait for evaluation")
            ),
            clock=Clock(),
        ),
    ).advance(CandidateWorkerRequest(tmp_path, 31))

    assert result.status is CandidateWorkerStatus.WAITING
    assert result.code == "candidate_draft_pending"
    planned = [
        record
        for record in result.snapshot.outbox
        if record.kind == "candidate_effect_planned"
        and record.payload.get("effect_kind") == "foundry_draft"
    ]
    assert len(planned) == 1
    assert planned[0].payload["max_attempts"] == 1
    assert not any(
        record.kind == "candidate_effect_succeeded"
        for record in result.snapshot.outbox
    )


def test_foundry_evaluation_is_deferred_after_draft_success(
    tmp_path: Path,
) -> None:
    class PendingEvaluations:
        def reconcile(self, intent):
            return None

        def run(self, intent):
            raise CandidateEffectPending("foundry_evaluation")

    def build_bundle(root: Path, output: Path) -> BundleArtifact:
        return BundleArtifact(
            output,
            "e" * 64,
            ("agent/instructions.md",),
            (),
            1,
            output.with_suffix(".manifest.json"),
        )

    ledger = Ledger(_seed_snapshot())
    drafts = Drafts()
    result = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(_plan()),
        dependencies=CandidateWorkerDependencies(
            repository=Repository(tmp_path),
            designer=DeferredDesigner(),
            validate=lambda path: ValidationReport((), False),
            build_bundle=build_bundle,
            drafts=drafts,
            evaluations=PendingEvaluations(),
            write_evidence=lambda request: (_ for _ in ()).throw(
                AssertionError("evidence must wait for evaluation")
            ),
            clock=Clock(),
        ),
    ).advance(CandidateWorkerRequest(tmp_path, 31))

    assert result.status is CandidateWorkerStatus.WAITING
    assert result.code == "candidate_evaluation_pending"
    assert drafts.creates == 1
    assert any(
        record.kind == "candidate_effect_succeeded"
        and record.payload.get("effect_kind") == "foundry_draft"
        for record in result.snapshot.outbox
    )
    evaluation_plans = [
        record
        for record in result.snapshot.outbox
        if record.kind == "candidate_effect_planned"
        and record.payload.get("effect_kind") == "foundry_evaluation"
    ]
    assert len(evaluation_plans) == 1
    assert evaluation_plans[0].payload["max_attempts"] == 1
    assert evaluation_plans[0].payload["idempotency_key"] == (
        _evaluation_intent(
            _plan(),
            "baseline",
            DraftRecord(
                "support",
                "draft-baseline",
                12,
                "e" * 64,
                "draft",
            ),
        ).idempotency_key
    )


def test_candidate_patch_is_durable_before_candidate_draft_delegation(
    tmp_path: Path,
) -> None:
    class CandidatePendingDrafts(Drafts):
        def create(self, intent):
            if intent.subject_id != "baseline":
                raise CandidateEffectPending("foundry_draft")
            return super().create(intent)

    def build_bundle(root: Path, output: Path) -> BundleArtifact:
        return BundleArtifact(
            output,
            "e" * 64,
            ("agent/instructions.md",),
            (),
            1,
            output.with_suffix(".manifest.json"),
        )

    ledger = Ledger(_seed_snapshot())
    repository = Repository(tmp_path)
    result = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(_plan()),
        dependencies=CandidateWorkerDependencies(
            repository=repository,
            designer=Designer(repository),
            validate=lambda path: ValidationReport((), False),
            build_bundle=build_bundle,
            drafts=CandidatePendingDrafts(),
            evaluations=Evaluations(),
            write_evidence=lambda request: (_ for _ in ()).throw(
                AssertionError("candidate evidence must wait for evaluation")
            ),
            clock=Clock(),
        ),
    ).advance(CandidateWorkerRequest(tmp_path, 31))

    assert result.status is CandidateWorkerStatus.WAITING
    assert result.code == "candidate_draft_pending"
    artifact = next(
        record
        for record in result.snapshot.outbox
        if record.kind == "candidate_artifact_ready"
    )
    patch_hash = str(artifact.payload["patch_sha256"])
    patch = next(
        item
        for item in result.snapshot.objects
        if item.path == f"objects/patches/{patch_hash}.patch"
    )
    assert patch.sha256 == patch_hash
    assert b"data/" not in patch.content


@pytest.mark.parametrize("mismatch", ["draft", "dataset", "subject"])
def test_evaluation_result_rejects_reconciled_binding_mismatch(
    tmp_path: Path,
    mismatch: str,
) -> None:
    class MismatchedEvaluations(Evaluations):
        def run(self, intent):
            result = super().run(intent)
            run = result.run
            if mismatch == "draft":
                run = replace(
                    run,
                    agent=replace(
                        run.agent,
                        draft_id="draft-other",
                    ),
                )
            elif mismatch == "dataset":
                run = replace(
                    run,
                    dataset=replace(
                        run.dataset,
                        dataset_id="dataset-other",
                    ),
                )
            else:
                run = replace(run, subject_id="candidate-other")
            changed = replace(result, run=run)
            self.results[intent.effect_id] = changed
            return changed

    def build_bundle(root: Path, output: Path) -> BundleArtifact:
        return BundleArtifact(
            output,
            "e" * 64,
            ("agent/instructions.md",),
            (),
            1,
            output.with_suffix(".manifest.json"),
        )

    result = CandidateWorkerService(
        ledger=Ledger(_seed_snapshot()),
        resolver=PlanResolver(_plan()),
        dependencies=CandidateWorkerDependencies(
            repository=Repository(tmp_path),
            designer=DeferredDesigner(),
            validate=lambda path: ValidationReport((), False),
            build_bundle=build_bundle,
            drafts=Drafts(),
            evaluations=MismatchedEvaluations(),
            write_evidence=lambda request: (_ for _ in ()).throw(
                AssertionError("mismatched evaluation must not be evidenced")
            ),
            clock=Clock(),
        ),
    ).advance(CandidateWorkerRequest(tmp_path, 31))

    assert result.status is CandidateWorkerStatus.FAILED
    assert result.code == "evaluation_binding_mismatch"
    assert result.snapshot.state.phase is CampaignPhase.BASELINE


def test_candidate_designer_result_is_bound_to_the_exact_reservation() -> None:
    intent = CandidateDesignIntent(
        effect_id="design-31-2-1",
        issue_number=31,
        generation=2,
        spec_sha256="a" * 64,
        base_commit="b" * 40,
        target="support",
        candidate_id="candidate-1",
        slot=1,
        worktree=Path("Q:/repo/.foundry-optimizer/worktrees/candidate-1"),
        goal="Improve grounded support answers without weakening safety.",
        edit_paths=(Path("agent"),),
        allowed_mutations=frozenset({"system_instructions"}),
        restricted_opt_ins={"tool_contract_schema_changes": False},
        baseline_metrics={"quality": 0.5},
        feedback=(),
    )
    result = CandidateDesignResult(
        effect_id=intent.effect_id,
        result_id="design-result-1",
        issue_number=intent.issue_number,
        generation=intent.generation,
        spec_sha256=intent.spec_sha256,
        base_commit=intent.base_commit,
        candidate_id=intent.candidate_id,
        slot=intent.slot,
        idea_id="idea-1",
        mutation_class="system_instructions",
        parent_idea_ids=(),
        required_opt_ins=frozenset(),
        motivation="Make the answer policy explicit.",
        lessons=("The baseline omits the escalation rule.",),
        complexity="small",
    )

    result.require_matches(intent)

    with pytest.raises(ValueError, match="generation"):
        CandidateDesignResult(
            **{
                **result.__dict__,
                "generation": 1,
            }
        ).require_matches(intent)

    with pytest.raises(ValueError, match="repository-relative"):
        CandidateDesignIntent(
            **{
                **intent.__dict__,
                "edit_paths": (Path("../outside"),),
            }
        )


def test_candidate_design_intent_accepts_multiline_issue_goal() -> None:
    intent = CandidateDesignIntent(
        effect_id="design-31-2-1",
        issue_number=31,
        generation=2,
        spec_sha256="a" * 64,
        base_commit="b" * 40,
        target="support",
        candidate_id="candidate-1",
        slot=1,
        worktree=Path("Q:/repo/.foundry-optimizer/worktrees/candidate-1"),
        goal=(
            "Improve grounded support answers.\n"
            "Preserve every configured safety guardrail."
        ),
        edit_paths=(Path("agent"),),
        allowed_mutations=frozenset({"system_instructions"}),
        restricted_opt_ins={},
        baseline_metrics={"quality": 0.5},
    )

    assert "\n" in intent.goal


class Ledger:
    def __init__(self, snapshot: StateRefSnapshot) -> None:
        self.snapshot = snapshot
        self.commits = 0

    def load(self, repository_root: Path, issue_number: int):
        return self.snapshot

    def commit(self, repository_root: Path, **kwargs):
        assert kwargs["expected_revision"] == self.snapshot.revision
        inbox = kwargs.get("inbox", ())
        outbox = kwargs.get("outbox", ())
        existing_events = {event.event_id for event in self.snapshot.inbox}
        existing_records = {
            record.record_id for record in self.snapshot.outbox
        }
        existing_objects = {item.path for item in self.snapshot.objects}
        assert not existing_events.intersection(
            event.event_id for event in inbox
        )
        assert not existing_records.intersection(
            record.record_id for record in outbox
        )
        assert not existing_objects.intersection(
            item.path for item in kwargs.get("objects", ())
        )
        self.commits += 1
        self.snapshot = StateRefSnapshot(
            revision=f"{self.commits + 1:040x}",
            state=kwargs["state"],
            inbox=(*self.snapshot.inbox, *inbox),
            outbox=(*self.snapshot.outbox, *outbox),
            objects=(
                *self.snapshot.objects,
                *kwargs.get("objects", ()),
            ),
        )
        return self.snapshot


class Clock:
    def now(self) -> datetime:
        return NOW


class Repository:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.worktrees: dict[str, CampaignWorktree] = {}
        self.designed: set[str] = set()
        self.changed_path = Path("agent/instructions.md")
        self.cleanup_calls: list[str] = []

    def pin_default_branch(self, repository_root: Path) -> PinnedRepository:
        return PinnedRepository("main", BASE_COMMIT)

    def create_worktree(
        self,
        repository_root: Path,
        campaign_id: str,
        candidate_id: str,
        base_commit: str,
    ) -> CampaignWorktree:
        path = (
            self.root
            / ".foundry-optimizer"
            / "worktrees"
            / campaign_id
            / candidate_id
        )
        path.mkdir(parents=True)
        worktree = CampaignWorktree(
            candidate_id,
            path.resolve(),
            f"foundry-opt/{campaign_id}/{candidate_id}",
            base_commit,
        )
        self.worktrees[candidate_id] = worktree
        return worktree

    def open_worktree(
        self,
        repository_root: Path,
        campaign_id: str,
        candidate_id: str,
        base_commit: str,
    ) -> CampaignWorktree:
        return self.worktrees[candidate_id]

    def reconcile_worktree(
        self,
        repository_root: Path,
        campaign_id: str,
        candidate_id: str,
        base_commit: str,
    ) -> CampaignWorktree:
        return self.worktrees.get(candidate_id) or self.create_worktree(
            repository_root,
            campaign_id,
            candidate_id,
            base_commit,
        )

    def changed_paths(
        self,
        worktree: CampaignWorktree,
    ) -> tuple[Path, ...]:
        if worktree.candidate_id not in self.designed:
            return ()
        return (self.changed_path,)

    def commit_worktree(
        self,
        worktree: CampaignWorktree,
        message: str,
    ) -> str:
        return "c" * 40

    def export_patch(
        self,
        repository_root: Path,
        campaign_id: str,
        worktree: CampaignWorktree,
        result_commit: str,
    ) -> PatchArtifact:
        path = Path(
            f".foundry-optimizer/campaigns/{campaign_id}/"
            f"{worktree.candidate_id}.patch"
        )
        content = (
            "diff --git a/agent/instructions.md "
            "b/agent/instructions.md\n"
        ).encode()
        output = repository_root / path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)
        return PatchArtifact(
            worktree.candidate_id,
            path,
            hashlib.sha256(content).hexdigest(),
            worktree.base_commit,
            result_commit,
            "f" * 40,
        )

    def cleanup_worktree(
        self,
        repository_root: Path,
        worktree: CampaignWorktree,
    ) -> None:
        self.cleanup_calls.append(worktree.branch)
        self.worktrees.pop(worktree.candidate_id, None)


class CleanupCrashRepository(Repository):
    def __init__(
        self,
        root: Path,
        *,
        fail_candidate_id: str,
        remove_before_failure: bool = False,
    ) -> None:
        super().__init__(root)
        self.fail_candidate_id = fail_candidate_id
        self.remove_before_failure = remove_before_failure
        self.failed = False

    def cleanup_worktree(
        self,
        repository_root: Path,
        worktree: CampaignWorktree,
    ) -> None:
        self.cleanup_calls.append(worktree.branch)
        if (
            worktree.candidate_id == self.fail_candidate_id
            and not self.failed
        ):
            self.failed = True
            if self.remove_before_failure:
                self.worktrees.pop(worktree.candidate_id, None)
            raise RuntimeError("cleanup interrupted")
        self.worktrees.pop(worktree.candidate_id, None)


class Designer:
    def __init__(
        self,
        repository: Repository,
        *,
        mutation_class: str = "system_instructions",
    ) -> None:
        self.repository = repository
        self.mutation_class = mutation_class
        self.results: dict[str, CandidateDesignResult] = {}
        self.invocations = 0
        self.intents: list[CandidateDesignIntent] = []

    def reconcile(
        self,
        intent: CandidateDesignIntent,
    ) -> tuple[CandidateDesignResult, ...]:
        result = self.results.get(intent.effect_id)
        return () if result is None else (result,)

    def invoke(self, intent: CandidateDesignIntent) -> CandidateDesignResult:
        self.invocations += 1
        self.intents.append(intent)
        self.repository.designed.add(intent.candidate_id)
        result = CandidateDesignResult(
            effect_id=intent.effect_id,
            result_id=f"result-{intent.candidate_id}",
            issue_number=intent.issue_number,
            generation=intent.generation,
            spec_sha256=intent.spec_sha256,
            base_commit=intent.base_commit,
            candidate_id=intent.candidate_id,
            slot=intent.slot,
            idea_id=f"idea-{intent.slot}",
            mutation_class=self.mutation_class,
            parent_idea_ids=(
                (intent.feedback[-1].idea_id,)
                if intent.feedback
                else ()
            ),
            motivation="Clarify the escalation rule.",
            lessons=("The baseline omits a required escalation.",),
            complexity="small",
        )
        self.results[intent.effect_id] = result
        return result


class DeferredDesigner:
    def reconcile(
        self,
        intent: CandidateDesignIntent,
    ) -> tuple[CandidateDesignResult, ...]:
        return ()

    def invoke(self, intent: CandidateDesignIntent) -> CandidateDesignResult:
        raise CandidateDesignPending()


class Drafts(CandidateDraftEffects):
    def __init__(self) -> None:
        self.records: dict[str, DraftRecord] = {}
        self.creates = 0

    def reconcile(self, intent):
        return self.records.get(intent.effect_id)

    def create(self, intent):
        self.creates += 1
        record = DraftRecord(
            agent_name="support",
            version_id=f"draft-{intent.subject_id}",
            base_version=12,
            sha256=intent.bundle.sha256,
            status="draft",
        )
        self.records[intent.effect_id] = record
        return record


class Evaluations(CandidateEvaluationEffects):
    def __init__(self) -> None:
        self.results: dict[str, EvaluationResult] = {}
        self.runs = 0

    def reconcile(self, intent):
        return self.results.get(intent.effect_id)

    def run(self, intent):
        self.runs += 1
        value = 0.5 if intent.subject.subject_id == "baseline" else 0.9
        result = _evaluation(
            intent.subject,
            value,
            dataset=intent.dataset,
        )
        self.results[intent.effect_id] = result
        return result


@dataclass
class PlanResolver:
    plan: CandidateWorkerPlan

    def resolve(self, request, state):
        return self.plan


def _evaluation(
    subject,
    value: float,
    *,
    dataset: DatasetVersionRef = DatasetVersionRef("dev", "1"),
) -> EvaluationResult:
    outcome = Outcome.PASS if value >= 0.8 else Outcome.FAIL
    run = EvaluationRun(
        run_id=f"run-{subject.subject_id}",
        evaluation_id=f"eval-{subject.subject_id}",
        subject_id=subject.subject_id,
        split=DatasetSplit.DEVELOPMENT,
        agent=subject.agent,
        dataset=dataset,
        evaluator=EvaluatorDefinitionRef("quality", "1"),
        status=EvaluationStatus.COMPLETED,
        portal_url=None,
        started_at=None,
        completed_at=None,
        error=None,
    )
    return EvaluationResult(
        run=run,
        cases=(
            NormalizedCase(
                case_id="case-1",
                case_hash="case-hash",
                response_ids=(f"response-{subject.subject_id}",),
                scores=(
                    NormalizedCaseMetric(
                        "quality",
                        value,
                        value,
                        None,
                        outcome,
                    ),
                ),
                usage=Usage(),
                trajectory=None,
                error=None,
                duration_ms=1,
            ),
        ),
        metrics={
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
        usage=Usage(),
        duration_ms=1,
        errors=(),
        complete=True,
        needs_repeat=False,
        attempts=1,
    )


def _cleanup_records(
    snapshot: StateRefSnapshot,
    candidate_id: str,
) -> tuple[str, ...]:
    return tuple(
        record.kind
        for record in snapshot.outbox
        if (
            record.payload.get("candidate_id") == candidate_id
            and record.payload.get("effect_kind") == "worktree_cleanup"
        )
    )


def _seed_snapshot() -> StateRefSnapshot:
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
    assert state.phase is CampaignPhase.BASELINE
    return StateRefSnapshot("1" * 40, state, (created, approved), ())


def _plan(*, max_changed_candidates: int = 1) -> CandidateWorkerPlan:
    assets = (
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
    )
    return CandidateWorkerPlan(
        issue_number=31,
        generation=1,
        spec_sha256=SPEC_SHA256,
        base_commit=BASE_COMMIT,
        target="support",
        base_agent_version=12,
        goal="Improve grounded support answers without weakening safety.",
        limits=CampaignLimits(50, 40, max_changed_candidates, 0),
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
        assets=assets,
        evidence_root=Path(".foundry-optimizer/campaigns"),
    )


def test_steward_candidate_worker_completes_baseline_and_one_candidate(
    tmp_path: Path,
) -> None:
    ledger = Ledger(_seed_snapshot())
    repository = Repository(tmp_path)
    designer = Designer(repository)
    drafts = Drafts()
    evaluations = Evaluations()

    def build_bundle(root: Path, output: Path) -> BundleArtifact:
        return BundleArtifact(
            output,
            "e" * 64,
            ("agent/instructions.md",),
            (),
            1,
            output.with_suffix(".manifest.json"),
        )

    def write_evidence(request) -> EvidenceManifest:
        content = (
            json.dumps(
                {
                    "campaign_id": request.campaign_id,
                    "metrics": {"quality": 0.9},
                    "schema_version": 1,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode()
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        request.output_path.write_bytes(content)
        return EvidenceManifest(
            request.output_path,
            hashlib.sha256(content).hexdigest(),
            len(content),
            tuple(
                result.run.evaluation_id
                for result in (request.baseline, *request.candidates)
            ),
            tuple(
                result.run.run_id
                for result in (request.baseline, *request.candidates)
            ),
            "9" * 64,
            request.spec_sha256,
        )

    service = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(_plan()),
        dependencies=CandidateWorkerDependencies(
            repository=repository,
            designer=designer,
            validate=lambda path: ValidationReport((), False),
            build_bundle=build_bundle,
            drafts=drafts,
            evaluations=evaluations,
            write_evidence=write_evidence,
            clock=Clock(),
        ),
    )

    result = service.advance(CandidateWorkerRequest(tmp_path, 31))

    assert result.status is CandidateWorkerStatus.COMPLETE
    assert result.snapshot.state.phase is CampaignPhase.CANDIDATES
    assert result.snapshot.state.baseline_evaluation_id == "eval-baseline"
    assert result.snapshot.state.candidates[0].candidate_id == "candidate-1"
    assert result.snapshot.state.candidates[0].eligible is True
    assert designer.invocations == 1
    assert drafts.creates == 2
    assert evaluations.runs == 2
    attestation = next(
        record
        for record in result.snapshot.outbox
        if record.kind == "candidate_attestation"
    )
    assert attestation.payload["changed_paths"] == [
        "agent/instructions.md"
    ]
    assert attestation.payload["result"] == "eligible"
    assert attestation.payload["tree_sha"] == "f" * 40
    assert attestation.payload["allowed_paths"] == ["agent"]
    assert attestation.payload["patch_path"].endswith("candidate-1.patch")
    assert attestation.payload["motivation"] == (
        "Clarify the escalation rule."
    )
    assert {item.path for item in result.snapshot.objects} == {
        "objects/candidates/g1-candidate-1.json",
        "objects/evidence/" + attestation.payload["evidence_sha256"] + ".json",
        "objects/patches/" + attestation.payload["patch_sha256"] + ".patch",
    }

    duplicate = service.advance(CandidateWorkerRequest(tmp_path, 31))

    assert duplicate.snapshot == result.snapshot
    assert designer.invocations == 1
    assert drafts.creates == 2
    assert evaluations.runs == 2


def test_candidate_design_delegates_a_typed_specialist_intent(
    tmp_path: Path,
) -> None:
    ledger = Ledger(_seed_snapshot())
    repository = Repository(tmp_path)
    drafts = Drafts()
    evaluations = Evaluations()

    def build_bundle(root: Path, output: Path) -> BundleArtifact:
        return BundleArtifact(
            output,
            "e" * 64,
            ("agent/instructions.md",),
            (),
            1,
            output.with_suffix(".manifest.json"),
        )

    service = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(_plan()),
        dependencies=CandidateWorkerDependencies(
            repository=repository,
            designer=DeferredDesigner(),
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
            clock=Clock(),
        ),
    )

    result = service.advance(CandidateWorkerRequest(tmp_path, 31))

    assert result.status is CandidateWorkerStatus.WAITING
    assert result.code == "candidate_design_pending"
    assert result.snapshot.state.phase is CampaignPhase.CANDIDATES
    assert repository.designed == set()
    assert drafts.creates == 1
    assert evaluations.runs == 1
    design = next(
        record
        for record in result.snapshot.outbox
        if record.kind == "specialist_work_request"
        and record.payload.get("work_kind") == "design_candidate"
    )
    assert design.payload["specialist"] == "foundry-candidate-designer"
    assert design.payload["effect_id"] == "design-31-1-1"
    assert design.payload["candidate_id"] == "candidate-1"
    assert design.payload["branch"].endswith("/candidate-1")
    assert design.payload["goal"] == _plan().goal
    assert design.payload["allowed_paths"] == ["agent"]
    assert design.payload["allowed_mutations"] == ["system_instructions"]
    assert design.payload["baseline_metrics"] == {"quality": 0.5}
    assert design.payload["restricted_opt_ins"] == {}
    assert design.payload["candidate_feedback"] == []

    duplicate = service.advance(CandidateWorkerRequest(tmp_path, 31))

    assert duplicate.status is CandidateWorkerStatus.WAITING
    assert duplicate.snapshot == result.snapshot
    assert sum(
        record.kind == "specialist_work_request"
        for record in duplicate.snapshot.outbox
    ) == 1
    assert drafts.creates == 1
    assert evaluations.runs == 1


def test_candidate_designer_records_a_typed_remote_result(
    tmp_path: Path,
) -> None:
    planned = OutboxRecord(
        "design-31-1-1-worker",
        "specialist_work_request",
        1,
        2,
        {
            "allowed_mutations": ["system_instructions"],
            "allowed_paths": ["agent"],
            "base_commit": BASE_COMMIT,
            "baseline_metrics": {"quality": 0.5},
            "branch": "foundry-opt/issue-31-g1/candidate-1",
            "candidate_feedback": [],
            "candidate_id": "candidate-1",
            "effect_id": "design-31-1-1",
            "goal": _plan().goal,
            "issue_number": 31,
            "reason": "candidate_design_pending",
            "restricted_opt_ins": {},
            "slot": 1,
            "spec_sha256": SPEC_SHA256,
            "specialist": "foundry-candidate-designer",
            "target": "support",
            "work_kind": "design_candidate",
        },
    )
    initial = _seed_snapshot()
    ledger = Ledger(
        replace(initial, outbox=(planned,))
    )
    result_file = tmp_path / "design-result.json"
    result_file.write_text(
        json.dumps(
            {
                "effect_id": "design-31-1-1",
                "result_id": "designer-result-1",
                "issue_number": 31,
                "generation": 1,
                "spec_sha256": SPEC_SHA256,
                "base_commit": BASE_COMMIT,
                "candidate_id": "candidate-1",
                "slot": 1,
                "idea_id": "idea-1",
                "mutation_class": "system_instructions",
                "parent_idea_ids": [],
                "required_opt_ins": [],
                "motivation": "Clarify the escalation rule.",
                "lessons": ["The baseline omits a required escalation."],
                "complexity": "small",
            }
        ),
        encoding="utf-8",
    )

    class Capture:
        cleaned = False

        def capture(self, request, intent, result):
            assert request.result_file == result_file
            result.require_matches(intent)
            return CandidateDesignArtifact(
                ref="refs/heads/foundry-opt/design/issue-31/design-31-1-1",
                head_commit="c" * 40,
                tree_sha="d" * 40,
                changed_paths=(Path("agent/instructions.md"),),
            )

        def cleanup(self, request, intent):
            self.cleaned = True

    capture = Capture()
    service = CandidateDesignSubmissionService(
        ledger=ledger,
        repository=capture,
    )

    result = service.submit(
        CandidateDesignSubmissionRequest(
            repository_root=tmp_path,
            issue_number=31,
            effect_id="design-31-1-1",
            worker_issue_number=84,
            result_file=result_file,
        )
    )
    duplicate = service.submit(
        CandidateDesignSubmissionRequest(
            repository_root=tmp_path,
            issue_number=31,
            effect_id="design-31-1-1",
            worker_issue_number=84,
            result_file=result_file,
        )
    )

    assert result.status is CandidateDesignSubmissionStatus.RECORDED
    assert duplicate.status is CandidateDesignSubmissionStatus.ALREADY_RECORDED
    submitted = result.snapshot.outbox[-1]
    assert submitted.kind == "candidate_design_submitted"
    assert submitted.payload["head_commit"] == "c" * 40
    assert submitted.payload["tree_sha"] == "d" * 40
    assert submitted.payload["changed_paths"] == [
        "agent/instructions.md"
    ]
    assert submitted.payload["worker_issue_number"] == 84
    assert capture.cleaned is True


def test_baseline_cleanup_failure_preserves_evaluation_and_resumes(
    tmp_path: Path,
) -> None:
    ledger = Ledger(_seed_snapshot())
    repository = CleanupCrashRepository(
        tmp_path,
        fail_candidate_id="baseline",
    )
    designer = Designer(repository)
    drafts = Drafts()
    evaluations = Evaluations()
    service = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(_plan()),
        dependencies=CandidateWorkerDependencies(
            repository=repository,
            designer=designer,
            validate=lambda path: ValidationReport((), False),
            build_bundle=lambda root, output: BundleArtifact(
                output,
                "e" * 64,
                ("agent/instructions.md",),
                (),
                1,
                output.with_suffix(".manifest.json"),
            ),
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
            clock=Clock(),
        ),
    )

    failed = service.advance(CandidateWorkerRequest(tmp_path, 31))

    assert failed.status is CandidateWorkerStatus.FAILED
    assert failed.code == "worktree_cleanup_failed"
    assert failed.snapshot.state.phase is CampaignPhase.CANDIDATES
    assert failed.snapshot.state.baseline_evaluation_id == "eval-baseline"
    assert drafts.creates == 1
    assert evaluations.runs == 1
    assert _cleanup_records(failed.snapshot, "baseline") == (
        "candidate_worktree_cleanup_planned",
    )

    resumed = service.advance(CandidateWorkerRequest(tmp_path, 31))

    assert resumed.status is CandidateWorkerStatus.COMPLETE
    assert drafts.creates == 2
    assert evaluations.runs == 2
    assert designer.invocations == 1
    assert _cleanup_records(resumed.snapshot, "baseline") == (
        "candidate_worktree_cleanup_planned",
        "candidate_worktree_cleanup_succeeded",
    )
    assert repository.cleanup_calls.count(
        f"foundry-opt/{_plan().campaign_id}/baseline"
    ) == 2


def test_validation_failure_records_ineligible_candidate_and_blocks(
    tmp_path: Path,
) -> None:
    ledger = Ledger(_seed_snapshot())
    repository = Repository(tmp_path)
    designer = Designer(repository)
    drafts = Drafts()
    evaluations = Evaluations()

    def build_bundle(root: Path, output: Path) -> BundleArtifact:
        return BundleArtifact(
            output,
            "e" * 64,
            ("agent/instructions.md",),
            (),
            1,
            output.with_suffix(".manifest.json"),
        )

    failed = ValidationReport(
        (
            ValidationResult(
                ("pytest",),
                Path("."),
                False,
                1,
                "",
                "redacted failure",
            ),
        ),
        False,
    )
    service = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(_plan()),
        dependencies=CandidateWorkerDependencies(
            repository=repository,
            designer=designer,
            validate=lambda path: failed,
            build_bundle=build_bundle,
            drafts=drafts,
            evaluations=evaluations,
            write_evidence=lambda request: pytest.fail(
                "rejected candidates must not write evaluation evidence"
            ),
            clock=Clock(),
        ),
    )

    result = service.advance(CandidateWorkerRequest(tmp_path, 31))

    assert result.status is CandidateWorkerStatus.BLOCKED
    assert result.code == "no_eligible_candidates"
    assert result.snapshot.state.phase is CampaignPhase.BLOCKED
    assert result.snapshot.state.candidates[0].eligible is False
    attestation = next(
        record
        for record in result.snapshot.outbox
        if record.kind == "candidate_attestation"
    )
    assert attestation.payload["result"] == "validation_failed"
    assert drafts.creates == 1
    assert evaluations.runs == 1


@pytest.mark.parametrize(
    ("validation_passed", "expected_status", "expected_phase"),
    (
        (True, CandidateWorkerStatus.COMPLETE, CampaignPhase.CANDIDATES),
        (False, CandidateWorkerStatus.BLOCKED, CampaignPhase.BLOCKED),
    ),
)
def test_candidate_terminal_cleanup_retries_without_repeating_evaluation(
    tmp_path: Path,
    validation_passed: bool,
    expected_status: CandidateWorkerStatus,
    expected_phase: CampaignPhase,
) -> None:
    ledger = Ledger(_seed_snapshot())
    repository = CleanupCrashRepository(
        tmp_path,
        fail_candidate_id="candidate-1",
        remove_before_failure=True,
    )
    designer = Designer(repository)
    drafts = Drafts()
    evaluations = Evaluations()
    validation = (
        ValidationReport((), False)
        if validation_passed
        else ValidationReport(
            (
                ValidationResult(
                    ("pytest",),
                    Path("."),
                    False,
                    1,
                    "",
                    "redacted failure",
                ),
            ),
            False,
        )
    )
    service = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(_plan()),
        dependencies=CandidateWorkerDependencies(
            repository=repository,
            designer=designer,
            validate=lambda path: validation,
            build_bundle=lambda root, output: BundleArtifact(
                output,
                "e" * 64,
                ("agent/instructions.md",),
                (),
                1,
                output.with_suffix(".manifest.json"),
            ),
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
            clock=Clock(),
        ),
    )

    failed = service.advance(CandidateWorkerRequest(tmp_path, 31))

    assert failed.status is CandidateWorkerStatus.FAILED
    assert failed.code == "worktree_cleanup_failed"
    assert failed.snapshot.state.phase is CampaignPhase.CANDIDATES
    assert failed.snapshot.state.candidates[0].eligible is validation_passed
    assert designer.invocations == 1
    assert evaluations.runs == (2 if validation_passed else 1)
    assert _cleanup_records(failed.snapshot, "candidate-1") == (
        "candidate_worktree_cleanup_planned",
    )

    resumed = service.advance(CandidateWorkerRequest(tmp_path, 31))
    cleanup_calls = len(repository.cleanup_calls)
    duplicate = service.advance(CandidateWorkerRequest(tmp_path, 31))

    assert resumed.status is expected_status
    assert resumed.snapshot.state.phase is expected_phase
    assert duplicate.snapshot == resumed.snapshot
    assert len(repository.cleanup_calls) == cleanup_calls
    assert designer.invocations == 1
    assert drafts.creates == (2 if validation_passed else 1)
    assert evaluations.runs == (2 if validation_passed else 1)
    assert _cleanup_records(resumed.snapshot, "candidate-1") == (
        "candidate_worktree_cleanup_planned",
        "candidate_worktree_cleanup_succeeded",
    )


@pytest.mark.parametrize(
    ("changed_path", "mutation_class", "expected_result"),
    (
        (
            Path(".github/workflows/unsafe.yml"),
            "system_instructions",
            "forbidden_paths",
        ),
        (
            Path("agent/instructions.md"),
            "tool_contracts",
            "forbidden_mutation",
        ),
    ),
)
def test_candidate_guardrails_reject_forbidden_edits_before_foundry(
    tmp_path: Path,
    changed_path: Path,
    mutation_class: str,
    expected_result: str,
) -> None:
    ledger = Ledger(_seed_snapshot())
    repository = Repository(tmp_path)
    repository.changed_path = changed_path
    designer = Designer(repository, mutation_class=mutation_class)
    drafts = Drafts()
    evaluations = Evaluations()

    def build_bundle(root: Path, output: Path) -> BundleArtifact:
        return BundleArtifact(
            output,
            "e" * 64,
            ("agent/instructions.md",),
            (),
            1,
            output.with_suffix(".manifest.json"),
        )

    service = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(_plan()),
        dependencies=CandidateWorkerDependencies(
            repository=repository,
            designer=designer,
            validate=lambda path: ValidationReport((), False),
            build_bundle=build_bundle,
            drafts=drafts,
            evaluations=evaluations,
            write_evidence=lambda request: pytest.fail(
                "guardrail-rejected candidates must not write evidence"
            ),
            clock=Clock(),
        ),
    )

    result = service.advance(CandidateWorkerRequest(tmp_path, 31))

    assert result.status is CandidateWorkerStatus.BLOCKED
    attestation = next(
        record
        for record in result.snapshot.outbox
        if record.kind == "candidate_attestation"
    )
    assert attestation.payload["result"] == expected_result
    assert drafts.creates == 1
    assert evaluations.runs == 1


def test_validation_cannot_add_an_extra_forbidden_edit(
    tmp_path: Path,
) -> None:
    ledger = Ledger(_seed_snapshot())
    repository = Repository(tmp_path)
    designer = Designer(repository)
    drafts = Drafts()
    evaluations = Evaluations()

    def validate(path: Path) -> ValidationReport:
        repository.changed_path = Path(".github/workflows/extra.yml")
        return ValidationReport((), False)

    def build_bundle(root: Path, output: Path) -> BundleArtifact:
        return BundleArtifact(
            output,
            "e" * 64,
            ("agent/instructions.md",),
            (),
            1,
            output.with_suffix(".manifest.json"),
        )

    result = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(_plan()),
        dependencies=CandidateWorkerDependencies(
            repository=repository,
            designer=designer,
            validate=validate,
            build_bundle=build_bundle,
            drafts=drafts,
            evaluations=evaluations,
            write_evidence=lambda request: pytest.fail(
                "post-validation forbidden edits must stop before evidence"
            ),
            clock=Clock(),
        ),
    ).advance(CandidateWorkerRequest(tmp_path, 31))

    assert result.status is CandidateWorkerStatus.BLOCKED
    attestation = next(
        record
        for record in result.snapshot.outbox
        if record.kind == "candidate_attestation"
    )
    assert attestation.payload["result"] == "forbidden_paths"
    assert attestation.payload["changed_paths"] == [
        ".github/workflows/extra.yml"
    ]
    assert drafts.creates == 1
    assert evaluations.runs == 1


def test_unknown_parent_lineage_is_rejected_before_foundry(
    tmp_path: Path,
) -> None:
    class UnknownParentDesigner(Designer):
        def invoke(self, intent: CandidateDesignIntent):
            result = super().invoke(intent)
            return replace(
                result,
                parent_idea_ids=("idea-from-another-campaign",),
            )

    ledger = Ledger(_seed_snapshot())
    repository = Repository(tmp_path)
    designer = UnknownParentDesigner(repository)
    drafts = Drafts()
    evaluations = Evaluations()

    def build_bundle(root: Path, output: Path) -> BundleArtifact:
        return BundleArtifact(
            output,
            "e" * 64,
            ("agent/instructions.md",),
            (),
            1,
            output.with_suffix(".manifest.json"),
        )

    result = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(_plan()),
        dependencies=CandidateWorkerDependencies(
            repository=repository,
            designer=designer,
            validate=lambda path: ValidationReport((), False),
            build_bundle=build_bundle,
            drafts=drafts,
            evaluations=evaluations,
            write_evidence=lambda request: pytest.fail(
                "invalid lineage must stop before evidence"
            ),
            clock=Clock(),
        ),
    ).advance(CandidateWorkerRequest(tmp_path, 31))

    assert result.status is CandidateWorkerStatus.BLOCKED
    attestation = next(
        record
        for record in result.snapshot.outbox
        if record.kind == "candidate_attestation"
    )
    assert attestation.payload["result"] == "invalid_lineage"
    assert drafts.creates == 1
    assert evaluations.runs == 1


def test_unchanged_candidate_is_recorded_without_foundry_candidate_effects(
    tmp_path: Path,
) -> None:
    class UnchangedRepository(Repository):
        def changed_paths(self, worktree: CampaignWorktree):
            return ()

    ledger = Ledger(_seed_snapshot())
    repository = UnchangedRepository(tmp_path)
    designer = Designer(repository)
    drafts = Drafts()
    evaluations = Evaluations()

    def build_bundle(root: Path, output: Path) -> BundleArtifact:
        return BundleArtifact(
            output,
            "e" * 64,
            ("agent/instructions.md",),
            (),
            1,
            output.with_suffix(".manifest.json"),
        )

    result = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(_plan()),
        dependencies=CandidateWorkerDependencies(
            repository=repository,
            designer=designer,
            validate=lambda path: ValidationReport((), False),
            build_bundle=build_bundle,
            drafts=drafts,
            evaluations=evaluations,
            write_evidence=lambda request: pytest.fail(
                "unchanged candidates must not write evidence"
            ),
            clock=Clock(),
        ),
    ).advance(CandidateWorkerRequest(tmp_path, 31))

    assert result.status is CandidateWorkerStatus.BLOCKED
    attestation = next(
        record
        for record in result.snapshot.outbox
        if record.kind == "candidate_attestation"
    )
    assert attestation.payload["result"] == "unchanged"
    assert drafts.creates == 1
    assert evaluations.runs == 1


def test_campaign_deadline_rejects_inflight_candidate_before_more_effects(
    tmp_path: Path,
) -> None:
    class MutableClock:
        def __init__(self) -> None:
            self.current = NOW

        def now(self) -> datetime:
            return self.current

    class DeadlineDesigner(Designer):
        def __init__(
            self,
            repository: Repository,
            clock: MutableClock,
        ) -> None:
            super().__init__(repository)
            self.clock = clock

        def invoke(self, intent: CandidateDesignIntent):
            result = super().invoke(intent)
            self.clock.current = NOW + timedelta(minutes=51)
            return result

    ledger = Ledger(_seed_snapshot())
    repository = Repository(tmp_path)
    clock = MutableClock()
    designer = DeadlineDesigner(repository, clock)
    drafts = Drafts()
    evaluations = Evaluations()

    def build_bundle(root: Path, output: Path) -> BundleArtifact:
        return BundleArtifact(
            output,
            "e" * 64,
            ("agent/instructions.md",),
            (),
            1,
            output.with_suffix(".manifest.json"),
        )

    result = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(_plan()),
        dependencies=CandidateWorkerDependencies(
            repository=repository,
            designer=designer,
            validate=lambda path: pytest.fail(
                "deadline must stop before validation"
            ),
            build_bundle=build_bundle,
            drafts=drafts,
            evaluations=evaluations,
            write_evidence=lambda request: pytest.fail(
                "deadline-rejected candidates must not write evidence"
            ),
            clock=clock,
        ),
    ).advance(CandidateWorkerRequest(tmp_path, 31))

    assert result.status is CandidateWorkerStatus.BLOCKED
    attestation = next(
        record
        for record in result.snapshot.outbox
        if record.kind == "candidate_attestation"
    )
    assert attestation.payload["result"] == "deadline_exceeded"
    assert drafts.creates == 1
    assert evaluations.runs == 1


def test_resume_after_draft_effect_ack_loss_does_not_duplicate_foundry(
    tmp_path: Path,
) -> None:
    class SessionCrash(RuntimeError):
        pass

    class CrashAfterCreateDrafts(Drafts):
        def __init__(self) -> None:
            super().__init__()
            self.crashed = False

        def create(self, intent):
            record = super().create(intent)
            if intent.subject_id == "candidate-1" and not self.crashed:
                self.crashed = True
                raise SessionCrash("session ended after Foundry accepted draft")
            return record

    class LostDesignerReconciliation:
        def __init__(self) -> None:
            self.calls = 0

        def reconcile(self, intent):
            self.calls += 1
            return ()

        def invoke(self, intent):
            pytest.fail(
                "a persisted successful design must never be reinvoked"
            )

    ledger = Ledger(_seed_snapshot())
    repository = Repository(tmp_path)
    designer = Designer(repository)
    drafts = CrashAfterCreateDrafts()
    evaluations = Evaluations()

    def build_bundle(root: Path, output: Path) -> BundleArtifact:
        return BundleArtifact(
            output,
            "e" * 64,
            ("agent/instructions.md",),
            (),
            1,
            output.with_suffix(".manifest.json"),
        )

    def write_evidence(request) -> EvidenceManifest:
        return EvidenceManifest(
            request.output_path,
            "f" * 64,
            10,
            tuple(
                item.run.evaluation_id
                for item in (request.baseline, *request.candidates)
            ),
            tuple(
                item.run.run_id
                for item in (request.baseline, *request.candidates)
            ),
            "9" * 64,
            request.spec_sha256,
        )

    dependencies = CandidateWorkerDependencies(
        repository=repository,
        designer=designer,
        validate=lambda path: ValidationReport((), False),
        build_bundle=build_bundle,
        drafts=drafts,
        evaluations=evaluations,
        write_evidence=write_evidence,
        clock=Clock(),
    )
    service = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(_plan()),
        dependencies=dependencies,
    )

    with pytest.raises(SessionCrash):
        service.advance(CandidateWorkerRequest(tmp_path, 31))

    recovered_designer = LostDesignerReconciliation()
    resumed = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(_plan()),
        dependencies=replace(
            dependencies,
            designer=recovered_designer,
        ),
    ).advance(CandidateWorkerRequest(tmp_path, 31))

    assert resumed.status is CandidateWorkerStatus.COMPLETE
    assert designer.invocations == 1
    assert recovered_designer.calls == 1
    assert drafts.creates == 2
    assert evaluations.runs == 2


def test_resume_after_crash_before_effect_discards_uncheckpointed_bundle(
    tmp_path: Path,
) -> None:
    class SessionCrash(RuntimeError):
        pass

    ledger = Ledger(_seed_snapshot())
    repository = Repository(tmp_path)
    designer = Designer(repository)
    drafts = Drafts()
    evaluations = Evaluations()
    crashed = False

    def crashing_bundle(root: Path, output: Path) -> BundleArtifact:
        nonlocal crashed
        if not crashed:
            crashed = True
            output.write_bytes(b"uncheckpointed bundle")
            raise SessionCrash("session ended before effect was planned")
        assert not output.exists()
        output.write_bytes(b"rebuilt bundle")
        return BundleArtifact(
            output,
            "e" * 64,
            ("agent/instructions.md",),
            (),
            len(b"rebuilt bundle"),
            output.with_suffix(".manifest.json"),
        )

    dependencies = CandidateWorkerDependencies(
        repository=repository,
        designer=designer,
        validate=lambda path: ValidationReport((), False),
        build_bundle=crashing_bundle,
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
        clock=Clock(),
    )
    with pytest.raises(SessionCrash):
        CandidateWorkerService(
            ledger=ledger,
            resolver=PlanResolver(_plan()),
            dependencies=dependencies,
        ).advance(CandidateWorkerRequest(tmp_path, 31))

    resumed = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(_plan()),
        dependencies=dependencies,
    ).advance(CandidateWorkerRequest(tmp_path, 31))

    assert resumed.status is CandidateWorkerStatus.COMPLETE
    assert drafts.creates == 2
    assert evaluations.runs == 2


def test_session_timeout_persists_progress_and_replacement_resumes(
    tmp_path: Path,
) -> None:
    ledger = Ledger(_seed_snapshot())
    repository = Repository(tmp_path)
    designer = Designer(repository)
    drafts = Drafts()
    evaluations = Evaluations()

    def build_bundle(root: Path, output: Path) -> BundleArtifact:
        return BundleArtifact(
            output,
            "e" * 64,
            ("agent/instructions.md",),
            (),
            1,
            output.with_suffix(".manifest.json"),
        )

    def write_evidence(request) -> EvidenceManifest:
        return EvidenceManifest(
            request.output_path,
            "f" * 64,
            10,
            tuple(
                item.run.evaluation_id
                for item in (request.baseline, *request.candidates)
            ),
            tuple(
                item.run.run_id
                for item in (request.baseline, *request.candidates)
            ),
            "9" * 64,
            request.spec_sha256,
        )

    dependencies = CandidateWorkerDependencies(
        repository=repository,
        designer=designer,
        validate=lambda path: ValidationReport((), False),
        build_bundle=build_bundle,
        drafts=drafts,
        evaluations=evaluations,
        write_evidence=write_evidence,
        clock=Clock(),
    )
    service = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(_plan()),
        dependencies=dependencies,
    )

    timed_out = service.advance(
        CandidateWorkerRequest(
            tmp_path,
            31,
            session_deadline=NOW,
        )
    )

    assert timed_out.status is CandidateWorkerStatus.WAITING
    assert timed_out.code == "session_timeout"
    assert timed_out.snapshot.state.phase is CampaignPhase.BASELINE
    assert drafts.creates == 0
    assert evaluations.runs == 0
    assert designer.invocations == 0

    resumed = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(_plan()),
        dependencies=dependencies,
    ).advance(
        CandidateWorkerRequest(
            tmp_path,
            31,
            session_deadline=NOW + timedelta(minutes=30),
        )
    )

    assert resumed.status is CandidateWorkerStatus.COMPLETE
    assert drafts.creates == 2
    assert evaluations.runs == 2
    assert designer.invocations == 1


def test_second_candidate_receives_redacted_adaptive_feedback_and_max_stops(
    tmp_path: Path,
) -> None:
    ledger = Ledger(_seed_snapshot())
    repository = Repository(tmp_path)
    designer = Designer(repository)
    drafts = Drafts()
    evaluations = Evaluations()

    def build_bundle(root: Path, output: Path) -> BundleArtifact:
        return BundleArtifact(
            output,
            "e" * 64,
            ("agent/instructions.md",),
            (),
            1,
            output.with_suffix(".manifest.json"),
        )

    service = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(_plan(max_changed_candidates=2)),
        dependencies=CandidateWorkerDependencies(
            repository=repository,
            designer=designer,
            validate=lambda path: ValidationReport((), False),
            build_bundle=build_bundle,
            drafts=drafts,
            evaluations=evaluations,
            write_evidence=lambda request: EvidenceManifest(
                request.output_path,
                (
                    "f" * 64
                    if request.candidates[0].run.subject_id == "candidate-1"
                    else "8" * 64
                ),
                10,
                tuple(
                    item.run.evaluation_id
                    for item in (request.baseline, *request.candidates)
                ),
                tuple(
                    item.run.run_id
                    for item in (request.baseline, *request.candidates)
                ),
                "9" * 64,
                request.spec_sha256,
            ),
            clock=Clock(),
        ),
    )

    result = service.advance(CandidateWorkerRequest(tmp_path, 31))

    assert result.status is CandidateWorkerStatus.COMPLETE
    assert designer.invocations == 2
    assert len(result.snapshot.state.candidates) == 2
    second = designer.intents[1]
    assert len(second.feedback) == 1
    assert second.feedback[0].candidate_id == "candidate-1"
    assert second.feedback[0].result == "eligible"
    assert second.feedback[0].metrics == {"quality": 0.9}
    assert second.feedback[0].lessons == (
        "The baseline omits a required escalation.",
    )
    second_attestation = next(
        record
        for record in result.snapshot.outbox
        if (
            record.kind == "candidate_attestation"
            and record.payload["candidate_id"] == "candidate-2"
        )
    )
    assert second_attestation.payload["parent_idea_ids"] == ["idea-1"]
    assert drafts.creates == 3
    assert evaluations.runs == 3


def test_global_development_pareto_revises_dominated_earlier_candidate(
    tmp_path: Path,
) -> None:
    class RankedEvaluations(Evaluations):
        def run(self, intent):
            self.runs += 1
            values = {
                "baseline": 0.5,
                "candidate-1": 0.85,
                "candidate-2": 0.9,
            }
            result = _evaluation(
                intent.subject,
                values[intent.subject.subject_id],
                dataset=intent.dataset,
            )
            self.results[intent.effect_id] = result
            return result

    ledger = Ledger(_seed_snapshot())
    repository = Repository(tmp_path)
    designer = Designer(repository)
    drafts = Drafts()
    evaluations = RankedEvaluations()

    def build_bundle(root: Path, output: Path) -> BundleArtifact:
        return BundleArtifact(
            output,
            "e" * 64,
            ("agent/instructions.md",),
            (),
            1,
            output.with_suffix(".manifest.json"),
        )

    result = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(_plan(max_changed_candidates=2)),
        dependencies=CandidateWorkerDependencies(
            repository=repository,
            designer=designer,
            validate=lambda path: ValidationReport((), False),
            build_bundle=build_bundle,
            drafts=drafts,
            evaluations=evaluations,
            write_evidence=lambda request: EvidenceManifest(
                request.output_path,
                (
                    "f" * 64
                    if request.candidates[0].run.subject_id == "candidate-1"
                    else "8" * 64
                ),
                10,
                (),
                (),
                "9" * 64,
                request.spec_sha256,
            ),
            clock=Clock(),
        ),
    ).advance(CandidateWorkerRequest(tmp_path, 31))

    assert result.status is CandidateWorkerStatus.COMPLETE
    assert [
        candidate.eligible
        for candidate in result.snapshot.state.candidates
    ] == [False, True]
    revisions = [
        event
        for event in result.snapshot.inbox
        if event.kind is EventKind.CANDIDATE_ELIGIBILITY_REVISED
    ]
    assert [event.payload["candidate_id"] for event in revisions] == [
        "candidate-1"
    ]


def test_completed_generation_ignores_later_cutoff_and_deadline_reentry(
    tmp_path: Path,
) -> None:
    class MutableClock:
        def __init__(self) -> None:
            self.current = NOW

        def now(self) -> datetime:
            return self.current

    class RankedEvaluations(Evaluations):
        def __init__(self) -> None:
            super().__init__()
            self.reconciliations = 0

        def reconcile(self, intent):
            self.reconciliations += 1
            return super().reconcile(intent)

        def run(self, intent):
            self.runs += 1
            values = {
                "baseline": 0.5,
                "candidate-1": 0.85,
                "candidate-2": 0.9,
            }
            result = _evaluation(
                intent.subject,
                values[intent.subject.subject_id],
                dataset=intent.dataset,
            )
            self.results[intent.effect_id] = result
            return result

    ledger = Ledger(_seed_snapshot())
    repository = Repository(tmp_path)
    evaluations = RankedEvaluations()
    clock = MutableClock()
    service = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(_plan(max_changed_candidates=2)),
        dependencies=CandidateWorkerDependencies(
            repository=repository,
            designer=Designer(repository),
            validate=lambda path: ValidationReport((), False),
            build_bundle=lambda root, output: BundleArtifact(
                output,
                "e" * 64,
                ("agent/instructions.md",),
                (),
                1,
                output.with_suffix(".manifest.json"),
            ),
            drafts=Drafts(),
            evaluations=evaluations,
            write_evidence=lambda request: EvidenceManifest(
                request.output_path,
                (
                    "f" * 64
                    if request.candidates[-1].run.subject_id == "candidate-1"
                    else "8" * 64
                ),
                10,
                (),
                (),
                "9" * 64,
                request.spec_sha256,
            ),
            clock=clock,
        ),
    )

    completed = service.advance(CandidateWorkerRequest(tmp_path, 31))
    commits_after_completion = ledger.commits
    reconciliations_after_completion = evaluations.reconciliations

    clock.current = NOW + timedelta(minutes=41)
    after_cutoff = service.advance(CandidateWorkerRequest(tmp_path, 31))
    clock.current = NOW + timedelta(minutes=51)
    after_deadline = service.advance(CandidateWorkerRequest(tmp_path, 31))

    assert completed.status is CandidateWorkerStatus.COMPLETE
    assert after_cutoff.snapshot == completed.snapshot
    assert after_deadline.snapshot == completed.snapshot
    completions = [
        event
        for event in after_deadline.snapshot.inbox
        if event.kind is EventKind.CANDIDATE_WORKERS_COMPLETED
    ]
    assert len(completions) == 1
    assert completions[0].event_id == "candidate-workers-1-completed"
    assert completions[0].payload["stop_reason"] == "max_candidates"
    assert sum(
        record.kind == "candidate_eligibility_revised"
        for record in after_deadline.snapshot.outbox
    ) == 1
    assert ledger.commits == commits_after_completion
    assert evaluations.reconciliations == reconciliations_after_completion


def test_duplicate_and_reordered_designer_results_use_only_exact_binding(
    tmp_path: Path,
) -> None:
    class ReorderedDesigner:
        def __init__(self, repository: Repository) -> None:
            self.repository = repository
            self.invocations = 0

        def reconcile(self, intent: CandidateDesignIntent):
            self.repository.designed.add(intent.candidate_id)
            exact = CandidateDesignResult(
                effect_id=intent.effect_id,
                result_id="exact-result",
                issue_number=intent.issue_number,
                generation=intent.generation,
                spec_sha256=intent.spec_sha256,
                base_commit=intent.base_commit,
                candidate_id=intent.candidate_id,
                slot=intent.slot,
                idea_id="idea-exact",
                mutation_class="system_instructions",
                motivation="Use the approved escalation rule.",
                lessons=("The exact reservation is authoritative.",),
                complexity="small",
            )
            stale = CandidateDesignResult(
                **{
                    **exact.__dict__,
                    "result_id": "stale-result",
                    "generation": intent.generation + 1,
                }
            )
            future = CandidateDesignResult(
                **{
                    **exact.__dict__,
                    "result_id": "future-result",
                    "candidate_id": "candidate-2",
                    "slot": 2,
                }
            )
            return (future, exact, stale, exact)

        def invoke(self, intent: CandidateDesignIntent):
            self.invocations += 1
            pytest.fail("an exact reconciled result must not be invoked again")

    ledger = Ledger(_seed_snapshot())
    repository = Repository(tmp_path)
    designer = ReorderedDesigner(repository)
    drafts = Drafts()
    evaluations = Evaluations()

    def build_bundle(root: Path, output: Path) -> BundleArtifact:
        return BundleArtifact(
            output,
            "e" * 64,
            ("agent/instructions.md",),
            (),
            1,
            output.with_suffix(".manifest.json"),
        )

    service = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(_plan()),
        dependencies=CandidateWorkerDependencies(
            repository=repository,
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
            clock=Clock(),
        ),
    )

    result = service.advance(CandidateWorkerRequest(tmp_path, 31))

    assert result.status is CandidateWorkerStatus.COMPLETE
    design_record = next(
        record
        for record in result.snapshot.outbox
        if record.kind == "candidate_design_succeeded"
    )
    assert design_record.payload["result_id"] == "exact-result"
    assert designer.invocations == 0


@pytest.mark.parametrize("stale", (False, True), ids=("exact", "stale"))
def test_resume_rehydrates_submitted_design_before_planning_draft(
    tmp_path: Path,
    stale: bool,
) -> None:
    ledger = Ledger(_seed_snapshot())
    initial_repository = Repository(tmp_path)
    initial_drafts = Drafts()
    initial_evaluations = Evaluations()
    dependencies = CandidateWorkerDependencies(
        repository=initial_repository,
        designer=DeferredDesigner(),
        validate=lambda path: ValidationReport((), False),
        build_bundle=lambda root, output: BundleArtifact(
            output,
            "e" * 64,
            ("agent/instructions.md",),
            (),
            1,
            output.with_suffix(".manifest.json"),
        ),
        drafts=initial_drafts,
        evaluations=initial_evaluations,
        write_evidence=lambda request: pytest.fail(
            "candidate design must resume before evidence"
        ),
        clock=Clock(),
    )
    first = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(_plan()),
        dependencies=dependencies,
    ).advance(CandidateWorkerRequest(tmp_path, 31))

    assert first.status is CandidateWorkerStatus.WAITING
    assert first.code == "candidate_design_pending"
    result = CandidateDesignResult(
        effect_id="design-31-1-1",
        result_id="designer-result-1",
        issue_number=31,
        generation=1,
        spec_sha256=SPEC_SHA256,
        base_commit=BASE_COMMIT,
        candidate_id="candidate-1",
        slot=1,
        idea_id="idea-1",
        mutation_class="system_instructions",
        motivation="Clarify the escalation rule.",
        lessons=("The baseline omits a required escalation.",),
        complexity="small",
    )
    artifact = CandidateDesignArtifact(
        "refs/heads/foundry-opt/design/issue-31/design-31-1-1",
        "c" * 40,
        "f" * 40,
        (Path("agent/instructions.md"),),
    )
    submitted = _candidate_design_submission_record(
        ledger.snapshot,
        "design-31-1-1-submitted",
        result,
        artifact,
        84,
    )
    if stale:
        submitted = replace(
            submitted,
            payload={
                **submitted.payload,
                "spec_sha256": "d" * 64,
            },
        )
    assigned = OutboxRecord(
        "design-31-1-1-worker-succeeded",
        "specialist_work_succeeded",
        1,
        ledger.snapshot.state.sequence,
        {
            "assigned": True,
            "created": True,
            "effect_id": "design-31-1-1-worker",
            "issue_number": 31,
            "result_id": "designer-assignment-84",
            "specialist": "foundry-candidate-designer",
            "work_kind": "design_candidate",
            "worker_issue_number": 84,
        },
    )
    ledger.commit(
        tmp_path,
        issue_number=31,
        expected_revision=ledger.snapshot.revision,
        state=ledger.snapshot.state,
        outbox=(assigned, submitted),
    )
    shutil.rmtree(
        tmp_path / ".foundry-optimizer" / "worktrees",
        ignore_errors=True,
    )

    class RehydratingRepository(Repository):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.rehydrations: list[tuple[str, str, str]] = []

        def rehydrate_worktree(
            self,
            repository_root,
            campaign_id,
            candidate_id,
            base_commit,
            **artifact_binding,
        ):
            self.rehydrations.append(
                (
                    artifact_binding["source_ref"],
                    artifact_binding["result_commit"],
                    artifact_binding["result_tree"],
                )
            )
            self.designed.add(candidate_id)
            return self.reconcile_worktree(
                repository_root,
                campaign_id,
                candidate_id,
                base_commit,
            )

    class ReconciledDesigner:
        def reconcile(self, intent):
            return (result,)

        def invoke(self, intent):
            pytest.fail("a submitted design must not be invoked again")

    class PendingDrafts:
        def reconcile(self, intent):
            return initial_drafts.reconcile(intent)

        def create(self, intent):
            raise CandidateEffectPending("foundry_draft")

    resumed_repository = RehydratingRepository(tmp_path)
    resumed = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(_plan()),
        dependencies=CandidateWorkerDependencies(
            repository=resumed_repository,
            designer=ReconciledDesigner(),
            validate=lambda path: ValidationReport((), False),
            build_bundle=dependencies.build_bundle,
            drafts=PendingDrafts(),
            evaluations=initial_evaluations,
            write_evidence=lambda request: pytest.fail(
                "draft planning must stop before evidence"
            ),
            clock=Clock(),
        ),
    ).advance(CandidateWorkerRequest(tmp_path, 31))

    if stale:
        assert resumed.status is CandidateWorkerStatus.FAILED
        assert resumed.code == "candidate_worktree_failed"
        assert resumed.summary == (
            "Candidate worktree rehydration failed. "
            "(candidate_design_artifact_stale)"
        )
        assert resumed_repository.rehydrations == []
        return
    assert resumed.status is CandidateWorkerStatus.WAITING
    assert resumed.code == "candidate_draft_pending"
    assert resumed_repository.rehydrations == [
        (artifact.ref, artifact.head_commit, artifact.tree_sha)
    ]
    assert any(
        record.kind == "candidate_effect_planned"
        and record.payload.get("effect_kind") == "foundry_draft"
        for record in resumed.snapshot.outbox
    )


def test_resume_rejects_stale_base_bound_to_durable_reservation(
    tmp_path: Path,
) -> None:
    ledger = Ledger(_seed_snapshot())
    repository = Repository(tmp_path)
    designer = Designer(repository)
    drafts = Drafts()
    evaluations = Evaluations()
    initial = _plan()

    def build_bundle(root: Path, output: Path) -> BundleArtifact:
        return BundleArtifact(
            output,
            "e" * 64,
            ("agent/instructions.md",),
            (),
            1,
            output.with_suffix(".manifest.json"),
        )

    dependencies = CandidateWorkerDependencies(
        repository=repository,
        designer=designer,
        validate=lambda path: ValidationReport((), False),
        build_bundle=build_bundle,
        drafts=drafts,
        evaluations=evaluations,
        write_evidence=lambda request: pytest.fail(
            "a stale base must stop before evidence"
        ),
        clock=Clock(),
    )
    first = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(initial),
        dependencies=dependencies,
    ).advance(
        CandidateWorkerRequest(
            tmp_path,
            31,
            session_deadline=NOW,
        )
    )
    assert first.status is CandidateWorkerStatus.WAITING

    stale = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(
            replace(initial, base_commit="c" * 40)
        ),
        dependencies=dependencies,
    ).advance(CandidateWorkerRequest(tmp_path, 31))

    assert stale.status is CandidateWorkerStatus.BLOCKED
    assert stale.code == "candidate_base_stale"
    assert stale.snapshot.state.phase is CampaignPhase.BASELINE
    assert drafts.creates == 0
    assert evaluations.runs == 0
    assert designer.invocations == 0


@pytest.mark.parametrize(
    ("elapsed_minutes", "stop_reason"),
    ((41, "candidate_cutoff"), (51, "campaign_deadline")),
)
def test_cutoff_and_deadline_stop_before_launching_changed_candidate(
    tmp_path: Path,
    elapsed_minutes: int,
    stop_reason: str,
) -> None:
    class MutableClock:
        def __init__(self) -> None:
            self.current = NOW

        def now(self) -> datetime:
            return self.current

    class AdvancingEvaluations(Evaluations):
        def __init__(self, clock: MutableClock) -> None:
            super().__init__()
            self.clock = clock

        def run(self, intent):
            result = super().run(intent)
            if intent.subject.subject_id == "baseline":
                self.clock.current = NOW + timedelta(
                    minutes=elapsed_minutes
                )
            return result

    ledger = Ledger(_seed_snapshot())
    repository = Repository(tmp_path)
    designer = Designer(repository)
    drafts = Drafts()
    clock = MutableClock()
    evaluations = AdvancingEvaluations(clock)

    def build_bundle(root: Path, output: Path) -> BundleArtifact:
        return BundleArtifact(
            output,
            "e" * 64,
            ("agent/instructions.md",),
            (),
            1,
            output.with_suffix(".manifest.json"),
        )

    result = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(_plan()),
        dependencies=CandidateWorkerDependencies(
            repository=repository,
            designer=designer,
            validate=lambda path: ValidationReport((), False),
            build_bundle=build_bundle,
            drafts=drafts,
            evaluations=evaluations,
            write_evidence=lambda request: pytest.fail(
                "no changed candidate should reach evidence"
            ),
            clock=clock,
        ),
    ).advance(CandidateWorkerRequest(tmp_path, 31))

    assert result.status is CandidateWorkerStatus.BLOCKED
    assert result.code == "no_eligible_candidates"
    assert designer.invocations == 0
    assert len(result.snapshot.state.candidates) == 0
    completion = next(
        event
        for event in result.snapshot.inbox
        if event.kind is EventKind.CANDIDATE_WORKERS_COMPLETED
    )
    assert completion.payload["stop_reason"] == stop_reason


def test_privacy_contract_rejects_raw_token_bearing_specialist_metadata() -> None:
    fields = {
        "effect_id": "design-31-1-1",
        "result_id": "result-1",
        "issue_number": 31,
        "generation": 1,
        "spec_sha256": SPEC_SHA256,
        "base_commit": BASE_COMMIT,
        "candidate_id": "candidate-1",
        "slot": 1,
        "idea_id": "idea-1",
        "mutation_class": "system_instructions",
        "motivation": "Authorization: Bearer sensitive-token",
        "lessons": ("Keep evidence aggregate-only.",),
        "complexity": "small",
    }

    with pytest.raises(ValueError, match="sensitive"):
        CandidateDesignResult(**fields)

    with pytest.raises(StateRefPrivacyError, match="sensitive"):
        OutboxRecord(
            "candidate-private",
            "candidate_design_succeeded",
            1,
            2,
            {
                "issue_number": 31,
                "motivation": "Authorization: Bearer sensitive-token",
            },
        )

    with pytest.raises(StateRefPrivacyError, match="privacy allowlist"):
        OutboxRecord(
            "candidate-raw",
            "candidate_design_succeeded",
            1,
            2,
            {
                "issue_number": 31,
                "raw_response": "never persist this",
            },
        )


def test_timeout_after_effect_plan_resumes_without_duplicate_designer_call(
    tmp_path: Path,
) -> None:
    class MutableClock:
        def __init__(self) -> None:
            self.current = NOW

        def now(self) -> datetime:
            return self.current

    class TimeoutDesigner(Designer):
        def __init__(
            self,
            repository: Repository,
            clock: MutableClock,
        ) -> None:
            super().__init__(repository)
            self.clock = clock
            self.expired_once = False

        def reconcile(self, intent: CandidateDesignIntent):
            if (
                intent.candidate_id == "candidate-1"
                and not self.expired_once
            ):
                self.expired_once = True
                self.clock.current = NOW + timedelta(minutes=1)
            return super().reconcile(intent)

    ledger = Ledger(_seed_snapshot())
    repository = Repository(tmp_path)
    clock = MutableClock()
    designer = TimeoutDesigner(repository, clock)
    drafts = Drafts()
    evaluations = Evaluations()

    def build_bundle(root: Path, output: Path) -> BundleArtifact:
        return BundleArtifact(
            output,
            "e" * 64,
            ("agent/instructions.md",),
            (),
            1,
            output.with_suffix(".manifest.json"),
        )

    dependencies = CandidateWorkerDependencies(
        repository=repository,
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
    first = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(_plan()),
        dependencies=dependencies,
    ).advance(
        CandidateWorkerRequest(
            tmp_path,
            31,
            session_deadline=NOW + timedelta(minutes=1),
        )
    )

    assert first.status is CandidateWorkerStatus.WAITING
    assert first.code == "session_timeout"
    assert designer.invocations == 0
    assert sum(
        record.kind == "candidate_effect_planned"
        and record.payload.get("effect_kind") == "candidate_design"
        for record in first.snapshot.outbox
    ) == 1

    resumed = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(_plan()),
        dependencies=dependencies,
    ).advance(
        CandidateWorkerRequest(
            tmp_path,
            31,
            session_deadline=NOW + timedelta(minutes=30),
        )
    )

    assert resumed.status is CandidateWorkerStatus.COMPLETE
    assert designer.invocations == 1
    assert sum(
        record.kind == "candidate_effect_planned"
        and record.payload.get("effect_kind") == "candidate_design"
        for record in resumed.snapshot.outbox
    ) == 1


def test_pending_cleanup_uses_its_persisted_generation_binding_once(
    tmp_path: Path,
) -> None:
    initial = _seed_snapshot()
    plan = _plan()
    repository = Repository(tmp_path)
    repository.create_worktree(
        tmp_path,
        plan.campaign_id,
        "candidate-1",
        plan.base_commit,
    )
    edited = CampaignEvent(
        "edited",
        EventKind.ISSUE_EDITED,
        2,
        NOW,
    )
    state = OptimizationCampaign().advance(
        AdvanceRequest(31, initial.state, (edited,))
    ).state
    cleanup_id = "worktree-cleanup-1-1"
    cleanup = OutboxRecord(
        cleanup_id,
        "candidate_worktree_cleanup_planned",
        1,
        initial.state.sequence,
        {
            "base_commit": plan.base_commit,
            "branch": (
                f"foundry-opt/{plan.campaign_id}/candidate-1"
            ),
            "campaign_id": plan.campaign_id,
            "candidate_id": "candidate-1",
            "effect_id": cleanup_id,
            "effect_kind": "worktree_cleanup",
            "issue_number": 31,
            "slot": 1,
            "spec_sha256": plan.spec_sha256,
            "work_kind": "candidate",
        },
    )
    ledger = Ledger(
        StateRefSnapshot(
            initial.revision,
            state,
            (*initial.inbox, edited),
            (cleanup,),
        )
    )
    service = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(replace(plan, generation=2)),
        dependencies=CandidateWorkerDependencies(
            repository=repository,
            designer=Designer(repository),
            validate=lambda path: ValidationReport((), False),
            build_bundle=lambda root, output: pytest.fail(
                "terminal stale cleanup must not restart workers"
            ),
            drafts=Drafts(),
            evaluations=Evaluations(),
            write_evidence=lambda request: pytest.fail(
                "terminal stale cleanup must not write evidence"
            ),
            clock=Clock(),
        ),
    )

    first = service.advance(CandidateWorkerRequest(tmp_path, 31))
    duplicate = service.advance(CandidateWorkerRequest(tmp_path, 31))

    assert first.status is CandidateWorkerStatus.BLOCKED
    assert first.code == "candidate_workers_phase_invalid"
    assert duplicate.snapshot == first.snapshot
    assert repository.cleanup_calls == [
        f"foundry-opt/{plan.campaign_id}/candidate-1"
    ]
    assert _cleanup_records(first.snapshot, "candidate-1") == (
        "candidate_worktree_cleanup_planned",
        "candidate_worktree_cleanup_succeeded",
    )


def test_stale_cleanup_cannot_target_another_generation(
    tmp_path: Path,
) -> None:
    snapshot = _seed_snapshot()
    plan = _plan()
    cleanup_id = "worktree-cleanup-1-1"
    cleanup = OutboxRecord(
        cleanup_id,
        "candidate_worktree_cleanup_planned",
        1,
        snapshot.state.sequence,
        {
            "base_commit": plan.base_commit,
            "branch": (
                "foundry-opt/issue-31-g2-aaaaaaaa-bbbbbbbb/"
                "candidate-1"
            ),
            "campaign_id": "issue-31-g2-aaaaaaaa-bbbbbbbb",
            "candidate_id": "candidate-1",
            "effect_id": cleanup_id,
            "effect_kind": "worktree_cleanup",
            "issue_number": 31,
            "slot": 1,
            "spec_sha256": plan.spec_sha256,
            "work_kind": "candidate",
        },
    )
    ledger = Ledger(
        StateRefSnapshot(
            snapshot.revision,
            snapshot.state,
            snapshot.inbox,
            (cleanup,),
        )
    )
    repository = Repository(tmp_path)

    result = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(plan),
        dependencies=CandidateWorkerDependencies(
            repository=repository,
            designer=Designer(repository),
            validate=lambda path: ValidationReport((), False),
            build_bundle=lambda root, output: pytest.fail(
                "invalid cleanup binding must stop workers"
            ),
            drafts=Drafts(),
            evaluations=Evaluations(),
            write_evidence=lambda request: pytest.fail(
                "invalid cleanup binding must stop workers"
            ),
            clock=Clock(),
        ),
    ).advance(CandidateWorkerRequest(tmp_path, 31))

    assert result.status is CandidateWorkerStatus.FAILED
    assert result.code == "worktree_cleanup_binding_invalid"
    assert result.snapshot == ledger.snapshot
    assert repository.cleanup_calls == []


@pytest.mark.parametrize(
    ("plan", "code"),
    (
        (
            replace(_plan(), generation=2),
            "candidate_generation_stale",
        ),
        (
            replace(_plan(), spec_sha256="c" * 64),
            "candidate_spec_stale",
        ),
    ),
)
def test_stale_generation_and_spec_stop_before_any_worker_effect(
    tmp_path: Path,
    plan: CandidateWorkerPlan,
    code: str,
) -> None:
    ledger = Ledger(_seed_snapshot())
    repository = Repository(tmp_path)
    designer = Designer(repository)
    drafts = Drafts()
    evaluations = Evaluations()

    result = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(plan),
        dependencies=CandidateWorkerDependencies(
            repository=repository,
            designer=designer,
            validate=lambda path: ValidationReport((), False),
            build_bundle=lambda root, output: pytest.fail(
                "stale workers must not package"
            ),
            drafts=drafts,
            evaluations=evaluations,
            write_evidence=lambda request: pytest.fail(
                "stale workers must not write evidence"
            ),
            clock=Clock(),
        ),
    ).advance(CandidateWorkerRequest(tmp_path, 31))

    assert result.status is CandidateWorkerStatus.BLOCKED
    assert result.code == code
    assert ledger.commits == 0
    assert designer.invocations == 0
    assert drafts.creates == 0
    assert evaluations.runs == 0


def test_persisted_draft_success_never_recreates_when_reconciliation_is_lost(
    tmp_path: Path,
) -> None:
    class MutableClock:
        def __init__(self) -> None:
            self.current = NOW

        def now(self) -> datetime:
            return self.current

    class TimeoutEvaluations(Evaluations):
        def __init__(self, clock: MutableClock) -> None:
            super().__init__()
            self.clock = clock
            self.expired = False

        def reconcile(self, intent):
            if (
                intent.subject.subject_id == "candidate-1"
                and not self.expired
            ):
                self.expired = True
                self.clock.current = NOW + timedelta(minutes=1)
            return super().reconcile(intent)

    class LostDraftReconciliation:
        def reconcile(self, intent):
            return None

        def create(self, intent):
            pytest.fail("a persisted successful draft must never be recreated")

    ledger = Ledger(_seed_snapshot())
    repository = Repository(tmp_path)
    designer = Designer(repository)
    drafts = Drafts()
    clock = MutableClock()
    evaluations = TimeoutEvaluations(clock)

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
        resolver=PlanResolver(_plan()),
        dependencies=CandidateWorkerDependencies(
            repository=repository,
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
            tmp_path,
            31,
            session_deadline=NOW + timedelta(minutes=1),
        )
    )

    assert first.status is CandidateWorkerStatus.WAITING
    assert drafts.creates == 2
    assert evaluations.runs == 1

    resumed = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(_plan()),
        dependencies=CandidateWorkerDependencies(
            repository=repository,
            designer=designer,
            validate=lambda path: ValidationReport((), False),
            build_bundle=build_bundle,
            drafts=LostDraftReconciliation(),
            evaluations=evaluations,
            write_evidence=evidence,
            clock=clock,
        ),
    ).advance(
        CandidateWorkerRequest(
            tmp_path,
            31,
            session_deadline=NOW + timedelta(minutes=30),
        )
    )

    assert resumed.status is CandidateWorkerStatus.COMPLETE
    assert evaluations.runs == 2


def test_persisted_evaluation_success_fails_closed_without_repeating_run(
    tmp_path: Path,
) -> None:
    class SessionCrash(RuntimeError):
        pass

    class LostEvaluationReconciliation:
        def reconcile(self, intent):
            return None

        def run(self, intent):
            pytest.fail(
                "a persisted successful evaluation must never be rerun"
            )

    ledger = Ledger(_seed_snapshot())
    repository = Repository(tmp_path)
    designer = Designer(repository)
    drafts = Drafts()
    evaluations = Evaluations()
    crashed = False

    def build_bundle(root: Path, output: Path) -> BundleArtifact:
        return BundleArtifact(
            output,
            "e" * 64,
            ("agent/instructions.md",),
            (),
            1,
            output.with_suffix(".manifest.json"),
        )

    def crash_after_evaluation(request):
        nonlocal crashed
        if not crashed:
            crashed = True
            raise SessionCrash("evaluation completed before evidence write")
        pytest.fail("the replacement cannot reconstruct raw evaluation output")

    dependencies = CandidateWorkerDependencies(
        repository=repository,
        designer=designer,
        validate=lambda path: ValidationReport((), False),
        build_bundle=build_bundle,
        drafts=drafts,
        evaluations=evaluations,
        write_evidence=crash_after_evaluation,
        clock=Clock(),
    )
    with pytest.raises(SessionCrash):
        CandidateWorkerService(
            ledger=ledger,
            resolver=PlanResolver(_plan()),
            dependencies=dependencies,
        ).advance(CandidateWorkerRequest(tmp_path, 31))

    resumed = CandidateWorkerService(
        ledger=ledger,
        resolver=PlanResolver(_plan()),
        dependencies=replace(
            dependencies,
            evaluations=LostEvaluationReconciliation(),
        ),
    ).advance(CandidateWorkerRequest(tmp_path, 31))

    assert resumed.status is CandidateWorkerStatus.FAILED
    assert resumed.code == "effect_reconciliation_failed"
    assert evaluations.runs == 2
