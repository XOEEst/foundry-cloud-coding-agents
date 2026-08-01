from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path

import pytest

from foundry_opt.orchestration.candidate_slate import (
    ApplierWorkerIntent,
    ApplierWorkerResult,
    CandidateBinding,
    CandidatePullRequestSnapshot,
    CandidatePullRequestAction,
    CandidatePullRequestEvent,
    CandidatePullRequestEventIntake,
    CandidatePullRequestIntakeStatus,
    CandidateSelectionRequest,
    CandidateSelectionService,
    CandidateSelectionStatus,
    CandidateSupersessionBridge,
    CandidateSupersessionBridgeStatus,
    CandidateEffectResultRecorder,
    CandidateEffectRecordStatus,
    TrustedCandidatePullRequestContext,
    candidate_pull_request_event_from_payload,
    applier_worker_result_record,
    CandidatePullRequestState,
    CandidatePullRequestVerificationStatus,
    CandidateSlatePlan,
    CandidateSlateRequest,
    CandidateSlateService,
    CandidateSlateStatus,
    ApplierWorkerBridge,
    ApplierWorkerBridgeStatus,
    candidate_pr_body,
    candidate_pr_marker,
    verify_candidate_pull_request,
)
from foundry_opt.evaluation import (
    EvaluationPolicy,
    MetricDirection,
    MetricPolicy,
)
from foundry_opt.orchestration import (
    AdvanceRequest,
    CampaignEvent,
    CampaignPhase,
    CampaignState,
    EventKind,
    OptimizationCampaign,
    OutboxRecord,
    StateRefSnapshot,
    StateObject,
)


def _binding() -> CandidateBinding:
    return CandidateBinding(
        issue_number=31,
        generation=2,
        spec_sha256="a" * 64,
        base_commit="b" * 40,
        candidate_id="candidate-1",
        draft_id="draft-candidate-1",
        evidence_sha256="c" * 64,
        patch_sha256="d" * 64,
        bundle_sha256="e" * 64,
        tree_sha="f" * 40,
        allowed_paths=(Path("agent"), Path("skills/support")),
        changed_paths=(Path("agent/instructions.md"),),
    )


def test_applier_worker_result_is_bound_to_the_exact_candidate() -> None:
    binding = _binding()
    intent = ApplierWorkerIntent(
        effect_id="applier-31-2-candidate-1",
        binding=binding,
    )
    result = ApplierWorkerResult(
        effect_id=intent.effect_id,
        result_id="applier-result-31-2-candidate-1",
        binding=binding,
        worker_issue_number=84,
        created=True,
        assigned=True,
    )

    result.require_matches(intent)

    assert candidate_pr_marker(binding) == (
        "<!-- foundry-opt:candidate-pr:issue-31:g2:candidate-1:"
        f"{binding.binding_sha256[:20]} -->"
    )
    with pytest.raises(ValueError, match="binding"):
        replace(
            result,
            binding=replace(binding, tree_sha="1" * 40),
        ).require_matches(intent)


def test_native_copilot_pull_request_verifies_exact_candidate() -> None:
    binding = _binding()
    snapshot = CandidatePullRequestSnapshot(
        pull_request_number=91,
        worker_issue_number=84,
        state=CandidatePullRequestState.OPEN,
        author="copilot-swe-agent[bot]",
        draft=False,
        base_ref_name="main",
        current_default_branch="main",
        current_default_commit=binding.base_commit,
        base_commit=binding.base_commit,
        head_commit="1" * 40,
        head_parent_commit=binding.base_commit,
        head_tree_sha=binding.tree_sha,
        patch_sha256=binding.patch_sha256,
        changed_paths=binding.changed_paths,
        body=candidate_pr_body(
            binding,
            worker_issue_number=84,
            required_checks=("exact-candidate", "tests"),
        ),
        checks={"exact-candidate": "success", "tests": "success"},
        binding_sha256=binding.binding_sha256,
        spec_sha256=binding.spec_sha256,
        bundle_sha256=binding.bundle_sha256,
        evidence_sha256=binding.evidence_sha256,
        marker=candidate_pr_marker(binding),
    )

    result = verify_candidate_pull_request(
        binding,
        snapshot,
        expected_default_branch="main",
        required_checks=("exact-candidate", "tests"),
    )

    assert result.status is CandidatePullRequestVerificationStatus.VERIFIED
    assert result.reason is None


def test_merged_candidate_allows_default_tip_to_be_exact_merge() -> None:
    binding = _binding()
    snapshot = CandidatePullRequestSnapshot(
        pull_request_number=91,
        worker_issue_number=84,
        state=CandidatePullRequestState.MERGED,
        author="copilot-swe-agent[bot]",
        draft=False,
        base_ref_name="main",
        current_default_branch="main",
        current_default_commit="8" * 40,
        base_commit=binding.base_commit,
        head_commit="1" * 40,
        head_parent_commit=binding.base_commit,
        head_tree_sha=binding.tree_sha,
        patch_sha256=binding.patch_sha256,
        changed_paths=binding.changed_paths,
        body=candidate_pr_body(
            binding,
            worker_issue_number=84,
            required_checks=("exact-candidate", "tests"),
        ),
        checks={"exact-candidate": "success", "tests": "success"},
        binding_sha256=binding.binding_sha256,
        spec_sha256=binding.spec_sha256,
        bundle_sha256=binding.bundle_sha256,
        evidence_sha256=binding.evidence_sha256,
        marker=candidate_pr_marker(binding),
        merge_commit="8" * 40,
        merge_parent_commit=binding.base_commit,
        merge_tree_sha=binding.tree_sha,
        merge_reachable_from_default=True,
    )

    result = verify_candidate_pull_request(
        binding,
        snapshot,
        expected_default_branch="main",
        required_checks=("exact-candidate", "tests"),
    )

    assert result.status is CandidatePullRequestVerificationStatus.VERIFIED


@pytest.mark.parametrize(
    ("change", "reason"),
    (
        ({"head_tree_sha": "2" * 40}, "result_tree_mismatch"),
        ({"base_commit": "2" * 40}, "base_changed"),
        ({"current_default_commit": "2" * 40}, "default_branch_advanced"),
        ({"patch_sha256": "2" * 64}, "patch_mismatch"),
        ({"spec_sha256": "2" * 64}, "spec_mismatch"),
        ({"bundle_sha256": "2" * 64}, "bundle_mismatch"),
        ({"evidence_sha256": "2" * 64}, "evidence_mismatch"),
        ({"marker": "<!-- wrong -->"}, "candidate_marker_mismatch"),
        (
            {"changed_paths": (Path("agent/instructions.md"), Path("extra.py"))},
            "changed_paths_mismatch",
        ),
        (
            {"checks": {"exact-candidate": "failure", "tests": "success"}},
            "required_checks_failed",
        ),
    ),
)
def test_native_pull_request_rejects_exactness_failures(
    change: dict[str, object],
    reason: str,
) -> None:
    binding = _binding()
    snapshot = CandidatePullRequestSnapshot(
        pull_request_number=91,
        worker_issue_number=84,
        state=CandidatePullRequestState.OPEN,
        author="copilot-swe-agent[bot]",
        draft=False,
        base_ref_name="main",
        current_default_branch="main",
        current_default_commit=binding.base_commit,
        base_commit=binding.base_commit,
        head_commit="1" * 40,
        head_parent_commit=binding.base_commit,
        head_tree_sha=binding.tree_sha,
        patch_sha256=binding.patch_sha256,
        changed_paths=binding.changed_paths,
        body=candidate_pr_body(
            binding,
            worker_issue_number=84,
            required_checks=("exact-candidate", "tests"),
        ),
        checks={"exact-candidate": "success", "tests": "success"},
        binding_sha256=binding.binding_sha256,
        spec_sha256=binding.spec_sha256,
        bundle_sha256=binding.bundle_sha256,
        evidence_sha256=binding.evidence_sha256,
        marker=candidate_pr_marker(binding),
    )

    result = verify_candidate_pull_request(
        binding,
        replace(snapshot, **change),
        expected_default_branch="main",
        required_checks=("exact-candidate", "tests"),
    )

    assert result.status is CandidatePullRequestVerificationStatus.INVALID
    assert result.reason == reason


def test_synchronize_requires_reverification_and_closed_unmerged_is_terminal() -> None:
    binding = _binding()
    pending = CandidatePullRequestSnapshot(
        pull_request_number=91,
        worker_issue_number=84,
        state=CandidatePullRequestState.OPEN,
        author="copilot-swe-agent[bot]",
        draft=False,
        base_ref_name="main",
        current_default_branch="main",
        current_default_commit=binding.base_commit,
        base_commit=binding.base_commit,
        head_commit="2" * 40,
        head_parent_commit=binding.base_commit,
        head_tree_sha=binding.tree_sha,
        patch_sha256=binding.patch_sha256,
        changed_paths=binding.changed_paths,
        body=candidate_pr_body(
            binding,
            worker_issue_number=84,
            required_checks=("exact-candidate", "tests"),
        ),
        checks={"exact-candidate": "pending", "tests": "pending"},
        binding_sha256=binding.binding_sha256,
    )

    synchronized = verify_candidate_pull_request(
        binding,
        pending,
        expected_default_branch="main",
        required_checks=("exact-candidate", "tests"),
    )
    closed = verify_candidate_pull_request(
        binding,
        replace(
            pending,
            state=CandidatePullRequestState.CLOSED,
        ),
        expected_default_branch="main",
        required_checks=("exact-candidate", "tests"),
    )

    assert synchronized.status is CandidatePullRequestVerificationStatus.PENDING
    assert synchronized.reason == "required_checks_pending"
    assert closed.status is CandidatePullRequestVerificationStatus.CLOSED
    assert closed.reason == "closed_unmerged"


@pytest.mark.parametrize(
    ("action", "kind"),
    (
        (CandidatePullRequestAction.OPENED, EventKind.CANDIDATE_PR_OPENED),
        (
            CandidatePullRequestAction.SYNCHRONIZE,
            EventKind.CANDIDATE_PR_SYNCHRONIZED,
        ),
        (CandidatePullRequestAction.EDITED, EventKind.CANDIDATE_PR_EDITED),
        (CandidatePullRequestAction.CLOSED, EventKind.CANDIDATE_PR_CLOSED),
        (CandidatePullRequestAction.MERGED, EventKind.CANDIDATE_PR_MERGED),
    ),
)
def test_native_pull_request_events_preserve_trusted_binding(
    action: CandidatePullRequestAction,
    kind: EventKind,
) -> None:
    binding = _binding()
    event = CandidatePullRequestEvent(
        event_id=f"delivery-{action.value}",
        action=action,
        occurred_at=datetime(2026, 7, 31, tzinfo=UTC),
        binding=binding,
        pull_request_number=91,
        head_commit="1" * 40,
        merge_commit=("2" * 40 if action is CandidatePullRequestAction.MERGED else None),
    ).to_campaign_event()

    assert event.kind is kind
    assert event.generation == binding.generation
    assert event.payload["binding_sha256"] == binding.binding_sha256
    assert event.payload["candidate_id"] == binding.candidate_id


class PullRequestInbox:
    def __init__(self) -> None:
        self.events: dict[str, CampaignEvent] = {}

    def append(self, issue_number: int, event: CampaignEvent) -> bool:
        existing = self.events.get(event.event_id)
        if existing is not None:
            if existing != event:
                raise ValueError("delivery was reused")
            return False
        self.events[event.event_id] = event
        return True


def test_pull_request_intake_deduplicates_reordered_webhooks() -> None:
    binding = _binding()
    inbox = PullRequestInbox()
    intake = CandidatePullRequestEventIntake(inbox)
    synchronized = CandidatePullRequestEvent(
        event_id="delivery-sync",
        action=CandidatePullRequestAction.SYNCHRONIZE,
        occurred_at=datetime(2026, 7, 31, 18, 2, tzinfo=UTC),
        binding=binding,
        pull_request_number=91,
        head_commit="2" * 40,
    )
    opened = CandidatePullRequestEvent(
        event_id="delivery-open",
        action=CandidatePullRequestAction.OPENED,
        occurred_at=datetime(2026, 7, 31, 18, 1, tzinfo=UTC),
        binding=binding,
        pull_request_number=91,
        head_commit="1" * 40,
    )

    first = intake.ingest(synchronized)
    second = intake.ingest(opened)
    duplicate = intake.ingest(synchronized)

    assert first.status is CandidatePullRequestIntakeStatus.RECORDED
    assert second.status is CandidatePullRequestIntakeStatus.RECORDED
    assert duplicate.status is CandidatePullRequestIntakeStatus.DUPLICATE
    assert set(inbox.events) == {"delivery-open", "delivery-sync"}


def test_trusted_github_payload_normalizes_native_candidate_merge() -> None:
    binding = _binding()
    event = candidate_pull_request_event_from_payload(
        {
            "action": "closed",
            "repository": {
                "id": 123,
                "full_name": "octo-org/optimizer",
            },
            "pull_request": {
                "number": 91,
                "body": candidate_pr_body(
                    binding,
                    worker_issue_number=84,
                    required_checks=("exact-candidate", "tests"),
                ),
                "merged": True,
                "merge_commit_sha": "8" * 40,
                "updated_at": "2026-07-31T18:03:00Z",
                "user": {"login": "copilot-swe-agent[bot]"},
                "head": {"sha": "1" * 40},
            },
        },
        TrustedCandidatePullRequestContext(
            event_name="pull_request",
            delivery_id="delivery-merge",
            repository="octo-org/optimizer",
            repository_id=123,
        ),
        (binding,),
    )

    assert event.action is CandidatePullRequestAction.MERGED
    assert event.binding == binding
    assert event.merge_commit == "8" * 40

    with pytest.raises(ValueError, match="lineage"):
        candidate_pull_request_event_from_payload(
            {
                "action": "opened",
                "repository": {
                    "id": 123,
                    "full_name": "octo-org/optimizer",
                },
                "pull_request": {
                    "number": 92,
                    "body": "<!-- unrelated -->",
                    "merged": False,
                    "merge_commit_sha": None,
                    "updated_at": "2026-07-31T18:04:00Z",
                    "user": {"login": "copilot-swe-agent[bot]"},
                    "head": {"sha": "2" * 40},
                },
            },
            TrustedCandidatePullRequestContext(
                event_name="pull_request",
                delivery_id="delivery-untrusted",
                repository="octo-org/optimizer",
                repository_id=123,
            ),
            (binding,),
        )


def test_edited_event_uses_previous_marker_to_invalidate_body_change() -> None:
    binding = _binding()
    event = candidate_pull_request_event_from_payload(
        {
            "action": "edited",
            "changes": {
                "body": {
                    "from": candidate_pr_body(
                        binding,
                        worker_issue_number=84,
                        required_checks=("exact-candidate", "tests"),
                    )
                }
            },
            "repository": {
                "id": 123,
                "full_name": "octo-org/optimizer",
            },
            "pull_request": {
                "number": 91,
                "body": "marker removed",
                "merged": False,
                "merge_commit_sha": None,
                "updated_at": "2026-07-31T18:04:00Z",
                "user": {"login": "copilot-swe-agent[bot]"},
                "head": {"sha": "2" * 40},
            },
        },
        TrustedCandidatePullRequestContext(
            event_name="pull_request",
            delivery_id="delivery-edited",
            repository="octo-org/optimizer",
            repository_id=123,
        ),
        (binding,),
    )

    assert event.action is CandidatePullRequestAction.EDITED
    assert event.binding == binding


def test_pull_request_events_are_observations_not_selection_commands(
    tmp_path: Path,
) -> None:
    snapshot, binding = _awaiting_selection_snapshot(tmp_path, count=1)
    opened = CandidatePullRequestEvent(
        event_id="delivery-open",
        action=CandidatePullRequestAction.OPENED,
        occurred_at=datetime(2026, 7, 31, 18, 1, tzinfo=UTC),
        binding=binding[0],
        pull_request_number=91,
        head_commit="1" * 40,
    ).to_campaign_event()
    synchronized = CandidatePullRequestEvent(
        event_id="delivery-sync",
        action=CandidatePullRequestAction.SYNCHRONIZE,
        occurred_at=datetime(2026, 7, 31, 18, 2, tzinfo=UTC),
        binding=binding[0],
        pull_request_number=91,
        head_commit="2" * 40,
    ).to_campaign_event()

    result = OptimizationCampaign().advance(
        AdvanceRequest(
            31,
            snapshot.state,
            (synchronized, opened, synchronized),
        )
    )

    assert result.state.phase is CampaignPhase.AWAITING_SELECTION
    assert result.state.selected_candidate_id is None
    assert result.state.processed_event_ids[-2:] == (
        "delivery-sync",
        "delivery-open",
    )


class SlateLedger:
    def __init__(self, snapshot: StateRefSnapshot) -> None:
        self.snapshot = snapshot
        self.commits = 0

    def load(self, repository_root: Path, issue_number: int):
        return self.snapshot

    def commit(self, repository_root: Path, **kwargs):
        assert kwargs["expected_revision"] == self.snapshot.revision
        self.commits += 1
        self.snapshot = StateRefSnapshot(
            revision=f"{self.commits + 10:040x}",
            state=kwargs["state"],
            inbox=(*self.snapshot.inbox, *kwargs.get("inbox", ())),
            outbox=(*self.snapshot.outbox, *kwargs.get("outbox", ())),
            objects=(*self.snapshot.objects, *kwargs.get("objects", ())),
        )
        return self.snapshot


class SlateResolver:
    def __init__(self, plan: CandidateSlatePlan) -> None:
        self.plan = plan

    def resolve(self, request, state):
        return self.plan


def _canonical(document: object) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _slate_harness(
    tmp_path: Path,
    *,
    candidate_metrics: tuple[dict[str, float], ...] = (
        {"quality": 0.9, "safety": 1.0},
    ),
) -> tuple[CandidateSlateService, SlateLedger]:
    binding = _binding()
    campaign_id = "issue-31-g2-" + "a" * 8 + "-" + "b" * 8
    bindings: list[CandidateBinding] = []
    attestations: list[OutboxRecord] = []
    candidates: list[dict[str, object]] = []
    for slot, metrics in enumerate(candidate_metrics, 1):
        candidate_id = f"candidate-{slot}"
        patch_path = (
            tmp_path
            / ".foundry-optimizer"
            / "campaigns"
            / campaign_id
            / f"{candidate_id}.patch"
        )
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        patch = (
            "diff --git a/agent/instructions.md "
            "b/agent/instructions.md\n"
            f"# {candidate_id}\n"
        ).encode()
        patch_path.write_bytes(patch)
        evidence_path = (
            tmp_path
            / ".foundry-optimizer"
            / "campaigns"
            / campaign_id
            / candidate_id
            / "development-evidence.json"
        )
        evidence_path.parent.mkdir(parents=True)
        evidence = _canonical(
            {
                "campaign_id": campaign_id,
                "metrics": metrics,
                "schema_version": 1,
            }
        )
        evidence_path.write_bytes(evidence)
        item_binding = replace(
            binding,
            candidate_id=candidate_id,
            draft_id=f"draft-{candidate_id}",
            patch_sha256=hashlib.sha256(patch).hexdigest(),
            evidence_sha256=hashlib.sha256(evidence).hexdigest(),
            tree_sha=f"{slot:x}" * 40,
        )
        bindings.append(item_binding)
        attestation: dict[str, object] = {
            "allowed_paths": ["agent", "skills/support"],
            "attestation_sha256": "",
            "base_commit": item_binding.base_commit,
            "bundle_sha256": item_binding.bundle_sha256,
            "candidate_id": item_binding.candidate_id,
            "changed_paths": ["agent/instructions.md"],
            "complexity": "small",
            "draft_id": item_binding.draft_id,
            "eligible": True,
            "evaluation_id": f"eval-{candidate_id}",
            "evidence_path": evidence_path.relative_to(tmp_path).as_posix(),
            "evidence_sha256": item_binding.evidence_sha256,
            "idea_id": f"idea-{slot}",
            "issue_number": 31,
            "lessons": ["The candidate improves quality."],
            "lineage_sha256": f"{slot:x}" * 64,
            "metrics": metrics,
            "motivation": "Improve answer quality.",
            "mutation_class": "system_instructions",
            "parent_idea_ids": [],
            "patch_path": patch_path.relative_to(tmp_path).as_posix(),
            "patch_sha256": item_binding.patch_sha256,
            "result": "eligible",
            "result_commit": f"{slot + 3:x}" * 40,
            "run_id": f"run-{candidate_id}",
            "slot": slot,
            "spec_sha256": item_binding.spec_sha256,
            "tree_sha": item_binding.tree_sha,
        }
        attestation["attestation_sha256"] = hashlib.sha256(
            json.dumps(
                {
                    key: value
                    for key, value in attestation.items()
                    if key != "attestation_sha256"
                },
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        attestations.append(
            OutboxRecord(
                f"candidate-attestation-2-{slot}",
                "candidate_attestation",
                2,
                8,
                attestation,
            )
        )
        candidates.append(
            {
                "candidate_id": candidate_id,
                "eligible": True,
                "evidence_sha256": item_binding.evidence_sha256,
            }
        )
    binding = bindings[0]
    completed = CampaignEvent(
        "candidate-workers-2-completed",
        EventKind.CANDIDATE_WORKERS_COMPLETED,
        2,
        datetime(2026, 7, 31, tzinfo=UTC),
        {
            "attempted_count": len(candidate_metrics),
            "eligible_count": len(candidate_metrics),
            "stop_reason": "max_candidates",
        },
    )
    state = CampaignState(
        issue_number=31,
        generation=2,
        sequence=8,
        phase=CampaignPhase.CANDIDATES,
        processed_event_ids=(
            "created",
            "approved",
            "baseline",
            "candidate",
            completed.event_id,
        ),
        spec_sha256=binding.spec_sha256,
        baseline_evaluation_id="eval-baseline",
        candidates=tuple(candidates),
    )
    outbox = (
        OutboxRecord(
            "baseline-attestation-2",
            "candidate_baseline_attestation",
            2,
            8,
            {
                "base_commit": binding.base_commit,
                "bundle_sha256": "9" * 64,
                "draft_id": "draft-baseline",
                "evaluation_id": "eval-baseline",
                "issue_number": 31,
                "metrics": {"quality": 0.5, "safety": 1.0},
                "spec_sha256": binding.spec_sha256,
            },
        ),
        *attestations,
    )
    ledger = SlateLedger(
        StateRefSnapshot(
            "9" * 40,
            state,
            (completed,),
            outbox,
        )
    )
    plan = CandidateSlatePlan(
        issue_number=31,
        generation=2,
        repository="octo-org/optimizer",
        default_branch="main",
        spec_sha256=binding.spec_sha256,
        base_commit=binding.base_commit,
        evaluation_policy=EvaluationPolicy(
            (
                MetricPolicy(
                    "quality",
                    MetricDirection.MAXIMIZE,
                    0.8,
                    0.05,
                ),
                MetricPolicy(
                    "safety",
                    MetricDirection.MAXIMIZE,
                    1.0,
                    0.0,
                    hard_guardrail=True,
                ),
            )
        ),
        required_checks=("exact-candidate", "tests"),
    )
    return (
        CandidateSlateService(
            ledger=ledger,
            resolver=SlateResolver(plan),
        ),
        ledger,
    )


def test_steward_publishes_one_eligible_candidate_slate(
    tmp_path: Path,
) -> None:
    service, ledger = _slate_harness(tmp_path)

    result = service.advance(CandidateSlateRequest(tmp_path, 31))

    assert result.status is CandidateSlateStatus.PUBLISHED
    assert result.snapshot.state.phase is CampaignPhase.AWAITING_SELECTION
    assert [item.path for item in result.snapshot.objects] == [
        "objects/candidates/g2-candidate-1.json",
        (
            "objects/evidence/"
            + result.snapshot.state.candidates[0].evidence_sha256
            + ".json"
        ),
        next(
            item.path
            for item in result.snapshot.objects
            if item.path.startswith("objects/patches/")
        ),
    ]
    intents = [
        record
        for record in result.snapshot.outbox
        if record.kind == "applier_worker_issue_planned"
    ]
    assert len(intents) == 1
    assert intents[0].payload["candidate_id"] == "candidate-1"
    dashboard = [
        record
        for record in result.snapshot.outbox
        if record.kind == "candidate_slate_dashboard"
    ][-1]
    assert dashboard.payload["candidate_slate"][0]["rank"] == 1
    assert dashboard.payload["candidate_slate"][0]["deltas"] == {
        "quality": 0.4,
        "safety": 0.0,
    }

    duplicate = service.advance(CandidateSlateRequest(tmp_path, 31))

    assert duplicate.snapshot == result.snapshot
    assert ledger.commits == 1


@pytest.mark.parametrize("count", (1, 2, 3))
def test_slate_emits_one_deterministic_applier_intent_per_candidate(
    tmp_path: Path,
    count: int,
) -> None:
    service, _ = _slate_harness(
        tmp_path,
        candidate_metrics=tuple(
            {"quality": 0.9 - slot * 0.01, "safety": 1.0}
            for slot in range(count)
        ),
    )

    result = service.advance(CandidateSlateRequest(tmp_path, 31))

    intents = [
        record
        for record in result.snapshot.outbox
        if record.kind == "applier_worker_issue_planned"
    ]
    assert [record.payload["candidate_id"] for record in intents] == [
        f"candidate-{slot}" for slot in range(1, count + 1)
    ]
    assert len({record.record_id for record in intents}) == count


def test_slate_ranking_is_deterministic_and_ties_share_rank(
    tmp_path: Path,
) -> None:
    service, _ = _slate_harness(
        tmp_path,
        candidate_metrics=(
            {"quality": 0.85, "safety": 1.0},
            {"quality": 0.95, "safety": 1.0},
            {"quality": 0.95, "safety": 1.0},
        ),
    )

    result = service.advance(CandidateSlateRequest(tmp_path, 31))

    dashboard = [
        record
        for record in result.snapshot.outbox
        if record.kind == "candidate_slate_dashboard"
    ][-1]
    rows = dashboard.payload["candidate_slate"]
    assert [(row["candidate_id"], row["rank"]) for row in rows] == [
        ("candidate-2", 1),
        ("candidate-3", 1),
        ("candidate-1", 3),
    ]


def test_slate_fails_closed_when_evidence_contains_raw_content(
    tmp_path: Path,
) -> None:
    service, ledger = _slate_harness(tmp_path)
    attestation = next(
        record
        for record in ledger.snapshot.outbox
        if record.kind == "candidate_attestation"
    )
    evidence_path = (
        tmp_path / str(attestation.payload["evidence_path"])
    )
    evidence_path.write_bytes(
        _canonical(
            {
                "campaign_id": "issue-31-g2-aaaaaaaa-bbbbbbbb",
                "prompt": "raw customer prompt",
                "schema_version": 1,
            }
        )
    )

    result = service.advance(CandidateSlateRequest(tmp_path, 31))

    assert result.status is CandidateSlateStatus.FAILED
    assert result.code == "candidate_slate_invalid"
    assert ledger.commits == 0


def test_replacement_steward_publishes_from_durable_candidate_objects(
    tmp_path: Path,
) -> None:
    service, ledger = _slate_harness(tmp_path)
    attestation = next(
        record
        for record in ledger.snapshot.outbox
        if record.kind == "candidate_attestation"
    )
    payload = dict(attestation.payload)
    evidence_path = tmp_path / str(payload["evidence_path"])
    patch_path = tmp_path / str(payload["patch_path"])
    objects = (
        StateObject(
            "objects/candidates/g2-candidate-1.json",
            _canonical(payload),
        ),
        StateObject(
            "objects/evidence/"
            + str(payload["evidence_sha256"])
            + ".json",
            evidence_path.read_bytes(),
        ),
        StateObject(
            "objects/patches/"
            + str(payload["patch_sha256"])
            + ".patch",
            patch_path.read_bytes(),
        ),
    )
    evidence_path.unlink()
    patch_path.unlink()
    ledger.snapshot = replace(ledger.snapshot, objects=objects)

    result = service.advance(CandidateSlateRequest(tmp_path, 31))

    assert result.status is CandidateSlateStatus.PUBLISHED
    assert result.snapshot.objects == objects


class WorkerGateway:
    def __init__(self) -> None:
        self.issue_number: int | None = None
        self.marker: str | None = None
        self.created = 0
        self.assigned = 0
        self.comments = 0
        self.fail_assignment_once = False
        self.fail_ack_once = False

    def find_issue(self, marker: str) -> int | None:
        return self.issue_number if marker == self.marker else None

    def create_issue(self, *, title: str, body: str, marker: str) -> int:
        self.created += 1
        self.issue_number = 84
        self.marker = marker
        return 84

    def assign_exact_patch_specialist(
        self,
        issue_number: int,
        *,
        marker: str,
    ) -> None:
        self.assigned += 1
        if self.fail_assignment_once:
            self.fail_assignment_once = False
            raise RuntimeError("assignment failed")

    def has_assignment_marker(
        self,
        issue_number: int,
        marker: str,
    ) -> bool:
        return self.comments > 0 and marker == self.marker

    def record_assignment_marker(
        self,
        issue_number: int,
        marker: str,
    ) -> None:
        self.comments += 1
        self.marker = marker
        if self.fail_ack_once:
            self.fail_ack_once = False
            raise RuntimeError("ack lost")


def _worker_record() -> OutboxRecord:
    binding = _binding()
    return OutboxRecord(
        record_id="applier-2-candidate-1-" + binding.binding_sha256[:16],
        kind="applier_worker_issue_planned",
        generation=2,
        sequence=9,
        payload={
            "allowed_paths": [
                path.as_posix() for path in binding.allowed_paths
            ],
            "attestation_path": "objects/candidates/g2-candidate-1.json",
            "base_commit": binding.base_commit,
            "binding_sha256": binding.binding_sha256,
            "bundle_sha256": binding.bundle_sha256,
            "candidate_id": binding.candidate_id,
            "changed_paths": [
                path.as_posix() for path in binding.changed_paths
            ],
            "draft_id": binding.draft_id,
            "effect_id": "applier-2-candidate-1-"
            + binding.binding_sha256[:16],
            "effect_kind": "applier_worker_issue",
            "evidence_path": "objects/evidence/" + "c" * 64 + ".json",
            "evidence_sha256": binding.evidence_sha256,
            "issue_number": binding.issue_number,
            "marker": candidate_pr_marker(binding),
            "patch_path": "objects/patches/" + "d" * 64 + ".patch",
            "patch_sha256": binding.patch_sha256,
            "required_checks": ["exact-candidate", "tests"],
            "spec_sha256": binding.spec_sha256,
            "specialist": "foundry-candidate-applier",
            "tree_sha": binding.tree_sha,
            "work_kind": "apply_exact_candidate",
        },
    )


def test_applier_worker_bridge_retries_assignment_without_duplicate_issue() -> None:
    gateway = WorkerGateway()
    gateway.fail_assignment_once = True
    bridge = ApplierWorkerBridge(gateway)
    record = _worker_record()

    first = bridge.apply(record)
    second = bridge.apply(record)

    assert first.status is ApplierWorkerBridgeStatus.RETRY
    assert second.status is ApplierWorkerBridgeStatus.APPLIED
    assert gateway.created == 1
    assert gateway.assigned == 2
    assert gateway.comments == 1
    assert second.worker_issue_number == 84
    intent = ApplierWorkerIntent(
        effect_id=record.record_id,
        binding=_binding_from_worker_record(record),
    )
    worker_result = second.worker_result(intent)
    checkpoint = applier_worker_result_record(record, worker_result)
    assert checkpoint.kind == "applier_worker_issue_succeeded"
    assert checkpoint.payload["worker_issue_number"] == 84


def test_applier_worker_result_is_cas_recorded_once(
    tmp_path: Path,
) -> None:
    slate, _ = _slate_harness(tmp_path)
    published = slate.advance(CandidateSlateRequest(tmp_path, 31))
    planned = next(
        record
        for record in published.snapshot.outbox
        if record.kind == "applier_worker_issue_planned"
    )
    intent = ApplierWorkerIntent(
        planned.record_id,
        _binding_from_worker_record(planned),
    )
    result = ApplierWorkerResult(
        effect_id=intent.effect_id,
        result_id="worker-result-1",
        binding=intent.binding,
        worker_issue_number=84,
        created=True,
        assigned=True,
    )
    ledger = SlateLedger(published.snapshot)
    recorder = CandidateEffectResultRecorder(ledger)

    first = recorder.record(tmp_path, 31, result)
    duplicate = recorder.record(tmp_path, 31, result)

    assert first.status is CandidateEffectRecordStatus.RECORDED
    assert duplicate.status is CandidateEffectRecordStatus.ALREADY_RECORDED
    assert ledger.commits == 1


def test_applier_worker_bridge_recovers_after_assignment_ack_loss() -> None:
    gateway = WorkerGateway()
    gateway.fail_ack_once = True
    bridge = ApplierWorkerBridge(gateway)
    record = _worker_record()

    first = bridge.apply(record)
    second = bridge.apply(record)

    assert first.status is ApplierWorkerBridgeStatus.RETRY
    assert second.status is ApplierWorkerBridgeStatus.ALREADY_APPLIED
    assert gateway.created == 1
    assert gateway.assigned == 1
    assert gateway.comments == 1


class SelectionReader:
    def __init__(
        self,
        snapshots: tuple[CandidatePullRequestSnapshot, ...],
    ) -> None:
        self.snapshots = snapshots

    def snapshots_for(self, request, bindings):
        return self.snapshots


def test_open_candidate_pr_verification_is_checkpointed_idempotently(
    tmp_path: Path,
) -> None:
    snapshot, bindings = _awaiting_selection_snapshot(tmp_path, count=1)
    ledger = SlateLedger(snapshot)
    reader = SelectionReader(
        (
            _pr_snapshot(
                bindings[0],
                number=91,
                state=CandidatePullRequestState.OPEN,
            ),
        )
    )
    service = CandidateSelectionService(ledger=ledger, reader=reader)
    request = CandidateSelectionRequest(
        tmp_path,
        31,
        expected_default_branch="main",
        required_checks=("exact-candidate", "tests"),
    )

    first = service.advance(request)
    duplicate = service.advance(request)

    assert first.status is CandidateSelectionStatus.WAITING
    verification = next(
        record
        for record in first.snapshot.outbox
        if record.kind == "candidate_pr_verified"
    )
    assert verification.payload["head_commit"] == reader.snapshots[0].head_commit
    assert verification.payload["pull_request_number"] == 91
    assert duplicate.snapshot == first.snapshot
    assert ledger.commits == 1


def test_pull_request_event_waits_for_reordered_worker_ack(
    tmp_path: Path,
) -> None:
    slate, _ = _slate_harness(tmp_path)
    published = slate.advance(CandidateSlateRequest(tmp_path, 31))
    planned = next(
        record
        for record in published.snapshot.outbox
        if record.kind == "applier_worker_issue_planned"
    )
    binding = _binding_from_worker_record(planned)
    ledger = SlateLedger(published.snapshot)

    result = CandidateSelectionService(
        ledger=ledger,
        reader=SelectionReader(
            (
                _pr_snapshot(
                    binding,
                    number=91,
                    state=CandidatePullRequestState.OPEN,
                ),
            )
        ),
    ).advance(
        CandidateSelectionRequest(
            tmp_path,
            31,
            expected_default_branch="main",
            required_checks=("exact-candidate", "tests"),
        )
    )

    assert result.status is CandidateSelectionStatus.WAITING
    assert result.code == "candidate_worker_ack_pending"
    assert ledger.commits == 0


def test_synchronize_extra_edit_invalidates_verification_and_plans_close(
    tmp_path: Path,
) -> None:
    snapshot, bindings = _awaiting_selection_snapshot(tmp_path, count=1)
    ledger = SlateLedger(snapshot)
    reader = SelectionReader(
        (
            _pr_snapshot(
                bindings[0],
                number=91,
                state=CandidatePullRequestState.OPEN,
            ),
        )
    )
    service = CandidateSelectionService(ledger=ledger, reader=reader)
    request = CandidateSelectionRequest(
        tmp_path,
        31,
        expected_default_branch="main",
        required_checks=("exact-candidate", "tests"),
    )
    service.advance(request)
    reader.snapshots = (
        replace(
            reader.snapshots[0],
            head_commit="6" * 40,
            changed_paths=(
                Path("agent/instructions.md"),
                Path("extra.py"),
            ),
        ),
    )

    result = service.advance(request)

    rejected = next(
        record
        for record in result.snapshot.outbox
        if record.kind == "candidate_pr_reject_planned"
    )
    assert rejected.payload["reason"] == "changed_paths_mismatch"
    assert rejected.payload["pull_request_number"] == 91


def test_edited_pr_without_marker_is_rejected_from_durable_event_binding(
    tmp_path: Path,
) -> None:
    snapshot, bindings = _awaiting_selection_snapshot(tmp_path, count=1)
    edited_event = CandidatePullRequestEvent(
        event_id="delivery-edited",
        action=CandidatePullRequestAction.EDITED,
        occurred_at=datetime(2026, 7, 31, 18, 4, tzinfo=UTC),
        binding=bindings[0],
        pull_request_number=91,
        head_commit="6" * 40,
    ).to_campaign_event()
    snapshot = replace(
        snapshot,
        inbox=(*snapshot.inbox, edited_event),
    )
    edited = replace(
        _pr_snapshot(
            bindings[0],
            number=91,
            state=CandidatePullRequestState.OPEN,
        ),
        body="marker removed",
        head_commit="6" * 40,
        binding_sha256=bindings[0].binding_sha256,
    )

    result = CandidateSelectionService(
        ledger=SlateLedger(snapshot),
        reader=SelectionReader((edited,)),
    ).advance(
        CandidateSelectionRequest(
            tmp_path,
            31,
            expected_default_branch="main",
            required_checks=("exact-candidate", "tests"),
        )
    )

    rejected = next(
        record
        for record in result.snapshot.outbox
        if record.kind == "candidate_pr_reject_planned"
    )
    assert rejected.payload["reason"] == "candidate_body_mismatch"


def _awaiting_selection_snapshot(
    tmp_path: Path,
    *,
    count: int = 2,
) -> tuple[StateRefSnapshot, tuple[CandidateBinding, ...]]:
    service, _ = _slate_harness(
        tmp_path,
        candidate_metrics=tuple(
            {"quality": 0.9 - index * 0.01, "safety": 1.0}
            for index in range(count)
        ),
    )
    result = service.advance(CandidateSlateRequest(tmp_path, 31))
    planned = tuple(
        record
        for record in result.snapshot.outbox
        if record.kind == "applier_worker_issue_planned"
    )
    successes = tuple(
        applier_worker_result_record(
            record,
            ApplierWorkerResult(
                effect_id=record.record_id,
                result_id=f"worker-result-{index}",
                binding=_binding_from_worker_record(record),
                worker_issue_number=80 + index,
                created=True,
                assigned=True,
            ),
        )
        for index, record in enumerate(planned, 1)
    )
    result = replace(
        result,
        snapshot=replace(
            result.snapshot,
            outbox=(*result.snapshot.outbox, *successes),
        ),
    )
    bindings = tuple(
        _binding_from_worker_record(record)
        for record in result.snapshot.outbox
        if record.kind == "applier_worker_issue_planned"
    )
    return result.snapshot, bindings


def _binding_from_worker_record(record: OutboxRecord) -> CandidateBinding:
    payload = record.payload
    return CandidateBinding(
        issue_number=int(payload["issue_number"]),
        generation=record.generation,
        spec_sha256=str(payload["spec_sha256"]),
        base_commit=str(payload["base_commit"]),
        candidate_id=str(payload["candidate_id"]),
        draft_id=str(payload["draft_id"]),
        evidence_sha256=str(payload["evidence_sha256"]),
        patch_sha256=str(payload["patch_sha256"]),
        bundle_sha256=str(payload["bundle_sha256"]),
        tree_sha=str(payload["tree_sha"]),
        allowed_paths=tuple(Path(path) for path in payload["allowed_paths"]),
        changed_paths=tuple(Path(path) for path in payload["changed_paths"]),
    )


def _pr_snapshot(
    binding: CandidateBinding,
    *,
    number: int,
    state: CandidatePullRequestState,
    merge_commit: str | None = None,
) -> CandidatePullRequestSnapshot:
    merged = state is CandidatePullRequestState.MERGED
    return CandidatePullRequestSnapshot(
        pull_request_number=number,
        worker_issue_number=number - 10,
        state=state,
        author="copilot-swe-agent[bot]",
        draft=False,
        base_ref_name="main",
        current_default_branch="main",
        current_default_commit=(
            merge_commit if merged and merge_commit is not None else binding.base_commit
        ),
        base_commit=binding.base_commit,
        head_commit=f"{number % 10:x}" * 40,
        head_parent_commit=binding.base_commit,
        head_tree_sha=binding.tree_sha,
        patch_sha256=binding.patch_sha256,
        changed_paths=binding.changed_paths,
        body=candidate_pr_body(
            binding,
            worker_issue_number=number - 10,
            required_checks=("exact-candidate", "tests"),
        ),
        checks={"exact-candidate": "success", "tests": "success"},
        binding_sha256=binding.binding_sha256,
        spec_sha256=binding.spec_sha256,
        bundle_sha256=binding.bundle_sha256,
        evidence_sha256=binding.evidence_sha256,
        marker=candidate_pr_marker(binding),
        merge_commit=merge_commit,
        merge_parent_commit=(binding.base_commit if merged else None),
        merge_tree_sha=(binding.tree_sha if merged else None),
        merge_reachable_from_default=merged,
    )


def test_first_valid_merge_records_selection_and_supersedes_alternatives(
    tmp_path: Path,
) -> None:
    snapshot, bindings = _awaiting_selection_snapshot(tmp_path)
    ledger = SlateLedger(snapshot)
    reader = SelectionReader(
        (
            _pr_snapshot(
                bindings[0],
                number=91,
                state=CandidatePullRequestState.MERGED,
                merge_commit="8" * 40,
            ),
            _pr_snapshot(
                bindings[1],
                number=92,
                state=CandidatePullRequestState.OPEN,
            ),
        )
    )

    result = CandidateSelectionService(
        ledger=ledger,
        reader=reader,
    ).advance(
        CandidateSelectionRequest(
            tmp_path,
            31,
            expected_default_branch="main",
            required_checks=("exact-candidate", "tests"),
        )
    )

    assert result.status is CandidateSelectionStatus.SELECTED
    assert result.snapshot.state.phase is CampaignPhase.DEPLOYMENT
    assert result.snapshot.state.selected_candidate_id == "candidate-1"
    effects = [
        record
        for record in result.snapshot.outbox
        if record.kind in {
            "candidate_issue_supersede_planned",
            "candidate_pr_supersede_planned",
        }
    ]
    assert {record.kind for record in effects} == {
        "candidate_issue_supersede_planned",
        "candidate_pr_supersede_planned",
    }
    assert {record.payload["candidate_id"] for record in effects} == {
        "candidate-2"
    }
    assert all(
        record.payload["reason"] == "candidate_selected_elsewhere"
        for record in effects
    )
    selection_dashboard = next(
        record
        for record in result.snapshot.outbox
        if record.kind == "candidate_selection_dashboard"
    )
    assert selection_dashboard.payload["selected_candidate_id"] == (
        "candidate-1"
    )
    assert selection_dashboard.payload["next_action"] == (
        "deployment_ready_for_next_phase"
    )
    assert "deployment_version" not in selection_dashboard.payload
    selection_record = next(
        record
        for record in result.snapshot.outbox
        if record.kind == "candidate_selection_recorded"
    )
    assert selection_record.payload == {
        "binding_sha256": bindings[0].binding_sha256,
        "candidate_id": "candidate-1",
        "head_commit": reader.snapshots[0].head_commit,
        "issue_number": 31,
        "merge_commit": "8" * 40,
        "pull_request_number": 91,
        "tree_sha": bindings[0].tree_sha,
        "worker_issue_number": 81,
    }

    duplicate = CandidateSelectionService(
        ledger=ledger,
        reader=reader,
    ).advance(
        CandidateSelectionRequest(
            tmp_path,
            31,
            expected_default_branch="main",
            required_checks=("exact-candidate", "tests"),
        )
    )
    assert duplicate.status is CandidateSelectionStatus.WAITING
    assert ledger.commits == 1


def test_two_valid_candidate_merges_fail_closed(
    tmp_path: Path,
) -> None:
    snapshot, bindings = _awaiting_selection_snapshot(tmp_path)
    ledger = SlateLedger(snapshot)
    reader = SelectionReader(
        tuple(
            _pr_snapshot(
                binding,
                number=91 + index,
                state=CandidatePullRequestState.MERGED,
                merge_commit=f"{8 + index:x}" * 40,
            )
            for index, binding in enumerate(bindings)
        )
    )

    result = CandidateSelectionService(
        ledger=ledger,
        reader=reader,
    ).advance(
        CandidateSelectionRequest(
            tmp_path,
            31,
            expected_default_branch="main",
            required_checks=("exact-candidate", "tests"),
        )
    )

    assert result.status is CandidateSelectionStatus.BLOCKED
    assert result.snapshot.state.phase is CampaignPhase.BLOCKED
    assert result.snapshot.state.block_reason == "multiple_candidate_merges"


def test_two_merges_for_same_candidate_fail_closed(
    tmp_path: Path,
) -> None:
    snapshot, bindings = _awaiting_selection_snapshot(tmp_path, count=1)
    first = _pr_snapshot(
        bindings[0],
        number=91,
        state=CandidatePullRequestState.MERGED,
        merge_commit="8" * 40,
    )
    second = replace(
        _pr_snapshot(
            bindings[0],
            number=92,
            state=CandidatePullRequestState.MERGED,
            merge_commit="9" * 40,
        ),
        worker_issue_number=first.worker_issue_number,
        body=first.body,
    )

    result = CandidateSelectionService(
        ledger=SlateLedger(snapshot),
        reader=SelectionReader((first, second)),
    ).advance(
        CandidateSelectionRequest(
            tmp_path,
            31,
            expected_default_branch="main",
            required_checks=("exact-candidate", "tests"),
        )
    )

    assert result.status is CandidateSelectionStatus.BLOCKED
    assert result.snapshot.state.block_reason == "multiple_candidate_merges"


def test_stale_merged_candidate_fails_closed(
    tmp_path: Path,
) -> None:
    snapshot, bindings = _awaiting_selection_snapshot(tmp_path, count=1)
    stale = replace(
        _pr_snapshot(
            bindings[0],
            number=91,
            state=CandidatePullRequestState.MERGED,
            merge_commit="8" * 40,
        ),
        merge_parent_commit="7" * 40,
    )
    ledger = SlateLedger(snapshot)

    result = CandidateSelectionService(
        ledger=ledger,
        reader=SelectionReader((stale,)),
    ).advance(
        CandidateSelectionRequest(
            tmp_path,
            31,
            expected_default_branch="main",
            required_checks=("exact-candidate", "tests"),
        )
    )

    assert result.status is CandidateSelectionStatus.BLOCKED
    assert result.snapshot.state.block_reason == "invalid_candidate_merge"


def test_pull_request_from_wrong_worker_issue_is_rejected(
    tmp_path: Path,
) -> None:
    snapshot, bindings = _awaiting_selection_snapshot(tmp_path, count=1)
    wrong_worker = replace(
        _pr_snapshot(
            bindings[0],
            number=91,
            state=CandidatePullRequestState.OPEN,
        ),
        worker_issue_number=999,
        body=candidate_pr_body(
            bindings[0],
            worker_issue_number=999,
            required_checks=("exact-candidate", "tests"),
        ),
    )

    result = CandidateSelectionService(
        ledger=SlateLedger(snapshot),
        reader=SelectionReader((wrong_worker,)),
    ).advance(
        CandidateSelectionRequest(
            tmp_path,
            31,
            expected_default_branch="main",
            required_checks=("exact-candidate", "tests"),
        )
    )

    assert result.status is CandidateSelectionStatus.FAILED
    assert result.code == "candidate_selection_invalid"


def test_selection_failure_event_replays_fail_closed_state(
    tmp_path: Path,
) -> None:
    snapshot, _ = _awaiting_selection_snapshot(tmp_path, count=1)
    event = CampaignEvent(
        "candidate-selection-2-invalid_candidate_merge",
        EventKind.CANDIDATE_SELECTION_FAILED,
        2,
        datetime(2026, 7, 31, 18, 5, tzinfo=UTC),
        {"reason": "invalid_candidate_merge"},
    )

    result = OptimizationCampaign().advance(
        AdvanceRequest(31, snapshot.state, (event,))
    )

    assert result.state.phase is CampaignPhase.BLOCKED
    assert result.state.block_reason == "invalid_candidate_merge"


def test_replayed_competing_domain_merges_fail_closed() -> None:
    state = CampaignState(
        issue_number=31,
        generation=2,
        sequence=5,
        phase=CampaignPhase.AWAITING_SELECTION,
        processed_event_ids=("a", "b", "c", "d", "e"),
        spec_sha256="a" * 64,
        baseline_evaluation_id="baseline",
        candidates=(
            {
                "candidate_id": "candidate-1",
                "eligible": True,
                "evidence_sha256": "b" * 64,
            },
            {
                "candidate_id": "candidate-2",
                "eligible": True,
                "evidence_sha256": "c" * 64,
            },
        ),
    )

    result = OptimizationCampaign().advance(
        AdvanceRequest(
            31,
            state,
            (
                CampaignEvent(
                    "merge-one",
                    EventKind.CANDIDATE_MERGED,
                    2,
                    datetime(2026, 7, 31, 18, 5, tzinfo=UTC),
                    {
                        "candidate_id": "candidate-1",
                        "merge_commit": "d" * 40,
                    },
                ),
                CampaignEvent(
                    "merge-two",
                    EventKind.CANDIDATE_MERGED,
                    2,
                    datetime(2026, 7, 31, 18, 6, tzinfo=UTC),
                    {
                        "candidate_id": "candidate-2",
                        "merge_commit": "e" * 40,
                    },
                ),
            ),
        )
    )

    assert result.state.phase is CampaignPhase.BLOCKED
    assert result.state.block_reason == "multiple_candidate_merges"


def test_competing_merge_after_selection_fails_closed(
    tmp_path: Path,
) -> None:
    snapshot, bindings = _awaiting_selection_snapshot(tmp_path)
    selected_state = OptimizationCampaign().advance(
        AdvanceRequest(
            31,
            snapshot.state,
            (
                CampaignEvent(
                    "selected-first",
                    EventKind.CANDIDATE_MERGED,
                    2,
                    datetime(2026, 7, 31, 18, 6, tzinfo=UTC),
                    {
                        "candidate_id": "candidate-1",
                        "merge_commit": "8" * 40,
                    },
                ),
            ),
        )
    ).state
    ledger = SlateLedger(replace(snapshot, state=selected_state))
    reader = SelectionReader(
        (
            _pr_snapshot(
                bindings[0],
                number=91,
                state=CandidatePullRequestState.MERGED,
                merge_commit="8" * 40,
            ),
            _pr_snapshot(
                bindings[1],
                number=92,
                state=CandidatePullRequestState.MERGED,
                merge_commit="9" * 40,
            ),
        )
    )

    result = CandidateSelectionService(
        ledger=ledger,
        reader=reader,
    ).advance(
        CandidateSelectionRequest(
            tmp_path,
            31,
            expected_default_branch="main",
            required_checks=("exact-candidate", "tests"),
        )
    )

    assert result.status is CandidateSelectionStatus.BLOCKED
    assert result.snapshot.state.phase is CampaignPhase.BLOCKED
    assert result.snapshot.state.block_reason == "multiple_candidate_merges"


def test_recorded_selection_does_not_depend_on_later_pr_body_edits(
    tmp_path: Path,
) -> None:
    snapshot, bindings = _awaiting_selection_snapshot(tmp_path, count=1)
    selected_state = OptimizationCampaign().advance(
        AdvanceRequest(
            31,
            snapshot.state,
            (
                CampaignEvent(
                    "selected-first",
                    EventKind.CANDIDATE_MERGED,
                    2,
                    datetime(2026, 7, 31, 18, 6, tzinfo=UTC),
                    {
                        "candidate_id": "candidate-1",
                        "merge_commit": "8" * 40,
                    },
                ),
            ),
        )
    ).state
    edited = replace(
        _pr_snapshot(
            bindings[0],
            number=91,
            state=CandidatePullRequestState.MERGED,
            merge_commit="8" * 40,
        ),
        body="edited after merge",
    )

    result = CandidateSelectionService(
        ledger=SlateLedger(replace(snapshot, state=selected_state)),
        reader=SelectionReader((edited,)),
    ).advance(
        CandidateSelectionRequest(
            tmp_path,
            31,
            expected_default_branch="main",
            required_checks=("exact-candidate", "tests"),
        )
    )

    assert result.status is CandidateSelectionStatus.WAITING
    assert result.snapshot.state.phase is CampaignPhase.DEPLOYMENT


class SupersessionGateway:
    def __init__(self) -> None:
        self.closed_issues: set[int] = set()
        self.closed_prs: set[int] = set()
        self.issue_calls = 0
        self.pr_calls = 0
        self.lose_issue_ack = False
        self.lose_pr_ack = False

    def issue_is_superseded(self, number: int, marker: str) -> bool:
        return number in self.closed_issues

    def supersede_issue(self, number: int, body: str, marker: str) -> None:
        self.issue_calls += 1
        self.closed_issues.add(number)
        if self.lose_issue_ack:
            self.lose_issue_ack = False
            raise RuntimeError("ack lost")

    def pull_request_is_superseded(
        self,
        number: int,
        marker: str,
    ) -> bool:
        return number in self.closed_prs

    def supersede_pull_request(
        self,
        number: int,
        body: str,
        marker: str,
    ) -> None:
        self.pr_calls += 1
        self.closed_prs.add(number)
        if self.lose_pr_ack:
            self.lose_pr_ack = False
            raise RuntimeError("ack lost")


def test_supersession_effects_reconcile_ack_loss_without_duplicate_closure(
    tmp_path: Path,
) -> None:
    snapshot, bindings = _awaiting_selection_snapshot(tmp_path)
    selected = CandidateSelectionService(
        ledger=SlateLedger(snapshot),
        reader=SelectionReader(
            (
                _pr_snapshot(
                    bindings[0],
                    number=91,
                    state=CandidatePullRequestState.MERGED,
                    merge_commit="8" * 40,
                ),
                _pr_snapshot(
                    bindings[1],
                    number=92,
                    state=CandidatePullRequestState.OPEN,
                ),
            )
        ),
    ).advance(
        CandidateSelectionRequest(
            tmp_path,
            31,
            expected_default_branch="main",
            required_checks=("exact-candidate", "tests"),
        )
    )
    issue = next(
        record
        for record in selected.snapshot.outbox
        if record.kind == "candidate_issue_supersede_planned"
    )
    pull_request = next(
        record
        for record in selected.snapshot.outbox
        if record.kind == "candidate_pr_supersede_planned"
    )
    gateway = SupersessionGateway()
    gateway.lose_issue_ack = True
    gateway.lose_pr_ack = True
    bridge = CandidateSupersessionBridge(gateway)

    assert bridge.apply(issue).status is CandidateSupersessionBridgeStatus.RETRY
    assert bridge.apply(issue).status is (
        CandidateSupersessionBridgeStatus.ALREADY_APPLIED
    )
    assert bridge.apply(pull_request).status is (
        CandidateSupersessionBridgeStatus.RETRY
    )
    assert bridge.apply(pull_request).status is (
        CandidateSupersessionBridgeStatus.ALREADY_APPLIED
    )
    assert gateway.issue_calls == 1
    assert gateway.pr_calls == 1
