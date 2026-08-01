from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import subprocess

from foundry_opt.optimization import (
    OptimizeCommandRequest,
    OptimizeCommandResult,
    OptimizeCommandStatus,
    OptimizePhase,
)
from foundry_opt.optimization.compatibility import (
    CompatibilityOptimizationCommandService,
    LegacyCampaignEventProjector,
    VerifiedCandidateMerge,
    VerifiedSpecApproval,
)
from foundry_opt.orchestration import (
    AdvanceRequest,
    CampaignEvent,
    CampaignPhase,
    CampaignState,
    EventKind,
    GitStateRef,
    OptimizationCampaign,
    StateRefSnapshot,
)
from foundry_opt.orchestration.steward import StewardAdvanceService
from foundry_opt.orchestration.candidate_workers import (
    CandidateWorkerResult,
    CandidateWorkerStatus,
)


NOW = datetime(2026, 7, 31, tzinfo=UTC)


class Legacy:
    def __init__(self, phase: OptimizePhase) -> None:
        self.phase = phase
        self.calls = 0

    def execute(self, request: OptimizeCommandRequest) -> OptimizeCommandResult:
        self.calls += 1
        return OptimizeCommandResult(
            OptimizeCommandStatus.COMPLETE,
            self.phase,
            "legacy completed",
            request.issue_number,
        )


def _run(arguments: tuple[str, ...], cwd: Path) -> str:
    return subprocess.run(
        arguments,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _repository(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    repository = tmp_path / "repository"
    _run(("git", "init", "--bare", str(origin)), tmp_path)
    _run(("git", "init", "-b", "main", str(repository)), tmp_path)
    _run(("git", "config", "user.name", "Compatibility Test"), repository)
    _run(
        ("git", "config", "user.email", "compat@example.invalid"),
        repository,
    )
    (repository / "README.md").write_text("test\n", encoding="utf-8")
    _run(("git", "add", "."), repository)
    _run(("git", "commit", "-m", "baseline"), repository)
    _run(("git", "remote", "add", "origin", str(origin)), repository)
    _run(("git", "push", "-u", "origin", "main"), repository)
    return repository


def _event(
    event_id: str,
    kind: EventKind,
    **payload: object,
) -> CampaignEvent:
    return CampaignEvent(event_id, kind, 1, NOW, payload)


def _seed(
    repository: Path,
    events: tuple[CampaignEvent, ...],
) -> GitStateRef:
    store = GitStateRef()
    state = OptimizationCampaign().advance(
        AdvanceRequest(7, None, events)
    ).state
    store.commit(
        repository,
        issue_number=7,
        expected_revision=None,
        state=state,
        inbox=events,
    )
    return store


def test_verified_spec_approval_is_cas_persisted_before_run(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    store = _seed(
        repository,
        (
            _event("created", EventKind.ISSUE_CREATED),
            _event(
                "spec-review",
                EventKind.SPEC_REVIEW_REQUIRED,
                spec_sha256="a" * 64,
            ),
        ),
    )
    legacy = Legacy(OptimizePhase.RUN)
    service = CompatibilityOptimizationCommandService(
        legacy=legacy,
        steward=StewardAdvanceService(ledger=store),
        projector=LegacyCampaignEventProjector(
            campaign_state=lambda root, campaign_id: None,
            lifecycle_state=lambda root, campaign_id: None,
            verified_spec_approval=lambda root, issue, digest: (
                VerifiedSpecApproval(digest, "b" * 40)
            ),
        ),
    )

    result = service.execute(
        OptimizeCommandRequest(repository, 7, OptimizePhase.RUN)
    )

    assert result.status is OptimizeCommandStatus.COMPLETE
    assert legacy.calls == 1
    snapshot = store.load(repository, 7)
    assert snapshot is not None
    assert snapshot.state.phase is CampaignPhase.BASELINE
    assert any(
        event.kind is EventKind.SPEC_HUMAN_APPROVED
        for event in snapshot.inbox
    )


def test_verified_candidate_merge_is_cas_persisted_before_reconcile(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    store = _seed(
        repository,
        (
            _event("created", EventKind.ISSUE_CREATED),
            _event(
                "spec",
                EventKind.SPEC_POLICY_APPROVED,
                spec_sha256="a" * 64,
            ),
            _event(
                "baseline",
                EventKind.BASELINE_COMPLETED,
                evaluation_id="baseline-1",
            ),
            _event(
                "candidate",
                EventKind.CANDIDATE_EVALUATED,
                candidate_id="candidate-1",
                eligible=True,
                evidence_sha256="c" * 64,
            ),
            _event("slate", EventKind.SLATE_PUBLISHED),
        ),
    )
    legacy = Legacy(OptimizePhase.RECONCILE)
    service = CompatibilityOptimizationCommandService(
        legacy=legacy,
        steward=StewardAdvanceService(ledger=store),
        projector=LegacyCampaignEventProjector(
            campaign_state=lambda root, campaign_id: None,
            lifecycle_state=lambda root, campaign_id: None,
            verified_candidate_merge=lambda root, issue, candidates: (
                VerifiedCandidateMerge("candidate-1", "d" * 40, 19)
            ),
        ),
    )

    result = service.execute(
        OptimizeCommandRequest(repository, 7, OptimizePhase.RECONCILE)
    )

    assert result.status is OptimizeCommandStatus.COMPLETE
    assert legacy.calls == 1
    snapshot = store.load(repository, 7)
    assert snapshot is not None
    assert snapshot.state.phase is CampaignPhase.DEPLOYMENT
    assert any(
        event.kind is EventKind.CANDIDATE_MERGED
        for event in snapshot.inbox
    )


def test_legacy_bootstrap_accepts_later_trusted_creation_and_continues(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    store = _seed(
        repository,
        (
            _event(
                "compat-issue-created-7",
                EventKind.ISSUE_CREATED,
            ),
        ),
    )
    trusted_created = _event(
        "github-delivery-created",
        EventKind.ISSUE_CREATED,
    )
    approved = _event(
        "policy-approved",
        EventKind.SPEC_POLICY_APPROVED,
        spec_sha256="a" * 64,
    )

    result = StewardAdvanceService(ledger=store).advance(
        __import__(
            "foundry_opt.orchestration.steward",
            fromlist=["StewardAdvanceRequest"],
        ).StewardAdvanceRequest(repository, 7),
        events=(trusted_created, approved),
    )

    assert result.exit_code == 0
    snapshot = store.load(repository, 7)
    assert snapshot is not None
    assert snapshot.state.phase is CampaignPhase.BASELINE
    assert trusted_created.event_id in snapshot.state.processed_event_ids
    assert tuple(event.event_id for event in snapshot.inbox) == (
        "compat-issue-created-7",
        "github-delivery-created",
        "policy-approved",
    )


def test_run_phase_adapts_completed_canonical_candidate_workers(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    store = _seed(
        repository,
        (
            _event("created", EventKind.ISSUE_CREATED),
            _event(
                "spec",
                EventKind.SPEC_POLICY_APPROVED,
                spec_sha256="a" * 64,
            ),
        ),
    )
    baseline = store.load(repository, 7)
    assert baseline is not None
    completed_event = _event(
        "candidate-workers-1-max_candidates",
        EventKind.CANDIDATE_WORKERS_COMPLETED,
        attempted_count=1,
        eligible_count=1,
        stop_reason="max_candidates",
    )
    completed_state = CampaignState(
        7,
        1,
        5,
        CampaignPhase.CANDIDATES,
        processed_event_ids=(
            "created",
            "spec",
            "baseline",
            "candidate",
            completed_event.event_id,
        ),
        spec_sha256="a" * 64,
        baseline_evaluation_id="eval-baseline",
        candidates=(
            {
                "candidate_id": "candidate-1",
                "eligible": True,
                "evidence_sha256": "b" * 64,
            },
        ),
    )

    class CandidateWorkers:
        def advance(self, request):
            return CandidateWorkerResult(
                CandidateWorkerStatus.COMPLETE,
                StateRefSnapshot(
                    "f" * 40,
                    completed_state,
                    (*baseline.inbox, completed_event),
                    baseline.outbox,
                ),
                "candidate workers complete",
            )

    legacy = Legacy(OptimizePhase.RUN)
    service = CompatibilityOptimizationCommandService(
        legacy=legacy,
        steward=StewardAdvanceService(
            ledger=store,
            candidate_workers=CandidateWorkers(),
        ),
        precheck=lambda request: OptimizeCommandResult(
            OptimizeCommandStatus.BLOCKED,
            request.phase,
            "legacy precheck must not mask canonical workers",
            request.issue_number,
        ),
    )

    result = service.execute(
        OptimizeCommandRequest(repository, 7, OptimizePhase.RUN)
    )

    assert result.status is OptimizeCommandStatus.COMPLETE
    assert result.details["source"] == "canonical_steward"
    assert legacy.calls == 0
