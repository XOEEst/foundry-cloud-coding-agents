from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from foundry_opt.config.models import AutomationPolicy
from foundry_opt.optimization.specification import PreparedSpecFile
from foundry_opt.orchestration import (
    AdvanceDisposition,
    CampaignEvent,
    CampaignPhase,
    CampaignState,
    EventKind,
    OptimizationCampaign,
    StateRefConflictError,
)
from foundry_opt.orchestration.steward import (
    GitCampaignInbox,
    StewardAdvanceRequest,
    StewardAdvanceService,
    StewardAdvanceStatus,
)
from foundry_opt.orchestration.spec_policy import (
    MergedSpecApproval,
    OptimizationSpecPolicy,
    ResolvedSpecification,
    SpecClassification,
    SpecPolicyDecision,
    SpecPolicyIntent,
)


NOW = datetime(2026, 7, 31, tzinfo=UTC)


def _event(
    event_id: str,
    kind: EventKind,
    *,
    generation: int = 1,
) -> CampaignEvent:
    return CampaignEvent(event_id, kind, generation, NOW)


class Ledger:
    def __init__(self, snapshot=None, *, conflict: bool = False) -> None:
        self.snapshot = snapshot
        self.conflict = conflict
        self.commits: list[dict[str, object]] = []

    def load(self, repository_root: Path, issue_number: int):
        return self.snapshot

    def commit(self, repository_root: Path, **kwargs):
        if self.conflict:
            raise StateRefConflictError("state ref changed")
        self.commits.append(kwargs)
        return type(
            "Snapshot",
            (),
            {
                "revision": "b" * 40,
                "state": kwargs["state"],
                "inbox": kwargs["inbox"],
                "outbox": kwargs["outbox"],
            },
        )()


class Inbox:
    def __init__(self, events: tuple[CampaignEvent, ...]) -> None:
        self.events = events

    def consume(self, request: StewardAdvanceRequest, snapshot):
        return self.events


class SpecPolicy:
    def __init__(self, decision: SpecPolicyDecision | None) -> None:
        self.decision = decision
        self.calls = []

    def evaluate(self, request):
        self.calls.append(request)
        return self.decision


def test_steward_advances_events_and_persists_state_and_outbox(
    tmp_path: Path,
) -> None:
    ledger = Ledger()
    service = StewardAdvanceService(
        ledger=ledger,
        inbox=Inbox((_event("event-1", EventKind.ISSUE_CREATED),)),
    )

    result = service.advance(StewardAdvanceRequest(tmp_path, 31))

    assert result.status is StewardAdvanceStatus.ADVANCED
    assert result.phase == "specification"
    assert result.disposition == "advance"
    assert result.revision == "b" * 40
    assert len(ledger.commits) == 1
    commit = ledger.commits[0]
    assert commit["expected_revision"] is None
    assert commit["state"] == result.state
    assert commit["inbox"][0].event_id == "event-1"
    assert commit["outbox"][0].kind == "campaign_advanced"


def test_steward_runs_spec_policy_and_persists_digest_classification(
    tmp_path: Path,
) -> None:
    digest = "d" * 64
    policy_event = CampaignEvent(
        "spec-policy-1-digest",
        EventKind.SPEC_POLICY_APPROVED,
        1,
        NOW,
        {"spec_sha256": digest},
    )
    policy = SpecPolicy(
        SpecPolicyDecision(
            SpecClassification.POLICY_APPROVED,
            "existing_immutable_assets",
            spec_sha256=digest,
            event=policy_event,
        )
    )
    ledger = Ledger()

    result = StewardAdvanceService(
        ledger=ledger,
        inbox=Inbox((_event("event-1", EventKind.ISSUE_CREATED),)),
        spec_policy=policy,
    ).advance(StewardAdvanceRequest(tmp_path, 31))

    assert result.status is StewardAdvanceStatus.ADVANCED
    assert result.phase == "baseline"
    assert [item.kind for item in ledger.commits[0]["inbox"]] == [
        EventKind.ISSUE_CREATED,
        EventKind.SPEC_POLICY_APPROVED,
    ]
    dashboard = ledger.commits[0]["outbox"][0]
    assert dashboard.payload["spec_sha256"] == digest
    assert dashboard.payload["spec_classification"] == "policy_approved"
    assert dashboard.payload["reason"] == "existing_immutable_assets"


def test_steward_persists_specialist_intent_without_duplicate_dispatch(
    tmp_path: Path,
) -> None:
    created = _event("event-1", EventKind.ISSUE_CREATED)
    state = OptimizationCampaign().advance(
        __import__(
            "foundry_opt.orchestration",
            fromlist=["AdvanceRequest"],
        ).AdvanceRequest(31, None, (created,))
    ).state
    existing = __import__(
        "foundry_opt.orchestration",
        fromlist=["OutboxRecord"],
    ).OutboxRecord(
        "spec-planner-1-trace_asset",
        "specialist_work_request",
        1,
        1,
        {
            "issue_number": 31,
            "reason": "trace_asset",
            "spec_classification": "human_review",
            "specialist": "foundry-optimization-planner",
            "work_kind": "prepare_specification_pr",
        },
    )
    snapshot = type(
        "Snapshot",
        (),
        {
            "revision": "a" * 40,
            "state": state,
            "inbox": (created,),
            "outbox": (existing,),
        },
    )()
    policy = SpecPolicy(
        SpecPolicyDecision(
            SpecClassification.HUMAN_REVIEW,
            "trace_asset",
            intents=(
                SpecPolicyIntent(
                    "spec-planner-1-trace_asset",
                    "specialist_work_request",
                    dict(existing.payload),
                ),
            ),
        )
    )
    ledger = Ledger(snapshot)

    result = StewardAdvanceService(
        ledger=ledger,
        inbox=Inbox(()),
        spec_policy=policy,
    ).advance(StewardAdvanceRequest(tmp_path, 31))

    assert result.status is StewardAdvanceStatus.WAITING
    assert ledger.commits == []


def test_spec_review_event_preserves_delegate_disposition_and_effects(
    tmp_path: Path,
) -> None:
    digest = "d" * 64
    review = CampaignEvent(
        "spec-review-1",
        EventKind.SPEC_REVIEW_REQUIRED,
        1,
        NOW,
        {
            "base_ref_name": "main",
            "files": [
                {
                    "path": (
                        ".foundry-optimizer/specs/issue-31/"
                        "optimization-spec.yaml"
                    ),
                    "sha256": "e" * 64,
                }
            ],
            "head_commit": "b" * 40,
            "spec_sha256": digest,
            "tree_sha": "c" * 40,
        },
    )
    intent = SpecPolicyIntent(
        "spec-planner-1-digest",
        "specialist_work_request",
        {"issue_number": 31},
    )
    policy = SpecPolicy(
        SpecPolicyDecision(
            SpecClassification.HUMAN_REVIEW,
            "repository_content_changed",
            spec_sha256=digest,
            event=review,
            intents=(intent,),
            disposition=AdvanceDisposition.DELEGATE,
        )
    )
    ledger = Ledger()

    result = StewardAdvanceService(
        ledger=ledger,
        inbox=Inbox((_event("event-1", EventKind.ISSUE_CREATED),)),
        spec_policy=policy,
    ).advance(StewardAdvanceRequest(tmp_path, 31))

    assert result.status is StewardAdvanceStatus.ADVANCED
    assert result.disposition == "delegate"
    commit = ledger.commits[0]
    assert [record.kind for record in commit["outbox"]] == [
        "campaign_advanced",
        "specialist_work_request",
    ]
    assert commit["outbox"][0].payload["disposition"] == "delegate"
    assert commit["outbox"][0].payload["status"] == "advanced"


def test_legacy_review_rebases_atomically_and_resumes_exact_approval(
    tmp_path: Path,
) -> None:
    old_digest = "d" * 64
    new_digest = "e" * 64
    head_commit = "b" * 40
    tree_sha = "c" * 40
    merge_commit = "f" * 40
    files = (
        PreparedSpecFile(
            Path(
                ".foundry-optimizer/specs/issue-31/"
                "optimization-spec.yaml"
            ),
            "1" * 64,
        ),
        PreparedSpecFile(
            Path(
                ".foundry-optimizer/specs/issue-31/"
                "provenance.json"
            ),
            "2" * 64,
        ),
    )
    legacy = CampaignState(
        31,
        4,
        11,
        CampaignPhase.AWAITING_SPEC_APPROVAL,
        schema_version=1,
        spec_sha256=old_digest,
    )
    initial = SimpleNamespace(
        revision="a" * 40,
        state=legacy,
        inbox=(),
        outbox=(),
    )

    class StatefulLedger(Ledger):
        def commit(self, repository_root: Path, **kwargs):
            committed = super().commit(repository_root, **kwargs)
            self.snapshot = SimpleNamespace(
                revision=committed.revision,
                state=kwargs["state"],
                inbox=(*self.snapshot.inbox, *kwargs["inbox"]),
                outbox=(*self.snapshot.outbox, *kwargs["outbox"]),
            )
            return self.snapshot

    class Resolver:
        def resolve(self, repository_root: Path, issue_number: int):
            return ResolvedSpecification(
                spec=SimpleNamespace(
                    sha256=new_digest,
                    datasets=(),
                    evaluators=(),
                ),
                asset_paths={},
                base_ref_name="main",
                head_commit=head_commit,
                tree_sha=tree_sha,
                prepared_files=files,
            )

    class Approvals:
        approval: MergedSpecApproval | None = None

        def merged_approval(
            self,
            repository_root: Path,
            issue_number: int,
            *,
            expected: CampaignState,
        ) -> MergedSpecApproval | None:
            return self.approval

    approvals = Approvals()
    policy = OptimizationSpecPolicy(
        AutomationPolicy(),
        resolver=Resolver(),
        pinned_assets=SimpleNamespace(),
        approvals=approvals,
        clock=lambda: NOW,
    )
    ledger = StatefulLedger(initial)
    service = StewardAdvanceService(
        ledger=ledger,
        inbox=Inbox(()),
        spec_policy=policy,
    )
    request = StewardAdvanceRequest(tmp_path, 31)

    first = service.advance(request)

    assert first.status is StewardAdvanceStatus.ADVANCED
    assert first.disposition == "delegate"
    assert first.state is not None
    assert first.state.spec_sha256 == new_digest
    assert first.state.spec_head_commit == head_commit
    assert first.state.spec_tree_sha == tree_sha
    assert first.state.schema_version == 2
    assert ledger.commits[0]["inbox"][0].kind is (
        EventKind.SPEC_REVIEW_REQUIRED
    )
    assert ledger.commits[0]["outbox"][1].record_id == (
        "spec-planner-4-legacy-" + new_digest[:16]
    )
    assert ledger.commits[0]["outbox"][1].payload["spec_sha256"] == (
        new_digest
    )
    assert ledger.commits[0]["outbox"][0].payload["reason"] == (
        "legacy_spec_rebased"
    )

    retry = service.advance(request)

    assert retry.status is StewardAdvanceStatus.WAITING
    assert retry.state == first.state
    assert len(ledger.commits) == 1

    approvals.approval = MergedSpecApproval(
        generation=4,
        pull_request_number=81,
        base_ref_name="main",
        head_commit=head_commit,
        head_tree_sha=tree_sha,
        head_files=files,
        head_spec_sha256=new_digest,
        merge_commit=merge_commit,
        merge_tree_sha="9" * 40,
        merged_files=files,
        merged_spec_sha256=new_digest,
        remote_default_tip="8" * 40,
        merge_reachable_from_default=True,
    )

    approved = service.advance(request)

    assert approved.status is StewardAdvanceStatus.ADVANCED
    assert approved.phase == "baseline"
    assert approved.state is not None
    assert approved.state.spec_sha256 == new_digest
    assert ledger.commits[-1]["inbox"][0].kind is (
        EventKind.SPEC_HUMAN_APPROVED
    )


def test_steward_ignores_unverified_external_spec_approval(
    tmp_path: Path,
) -> None:
    created = _event("event-1", EventKind.ISSUE_CREATED)
    review = CampaignEvent(
        "spec-review-1",
        EventKind.SPEC_REVIEW_REQUIRED,
        1,
        NOW,
        {
            "base_ref_name": "main",
            "files": [
                {
                    "path": (
                        ".foundry-optimizer/specs/issue-31/"
                        "optimization-spec.yaml"
                    ),
                    "sha256": "e" * 64,
                }
            ],
            "head_commit": "b" * 40,
            "spec_sha256": "d" * 64,
            "tree_sha": "c" * 40,
        },
    )
    state = OptimizationCampaign().advance(
        __import__(
            "foundry_opt.orchestration",
            fromlist=["AdvanceRequest"],
        ).AdvanceRequest(31, None, (created, review))
    ).state
    snapshot = type(
        "Snapshot",
        (),
        {
            "revision": "a" * 40,
            "state": state,
            "inbox": (created, review),
            "outbox": (),
        },
    )()
    external = CampaignEvent(
        "unverified-approval",
        EventKind.SPEC_HUMAN_APPROVED,
        1,
        NOW,
    )
    ledger = Ledger(snapshot)

    result = StewardAdvanceService(
        ledger=ledger,
        inbox=Inbox((external,)),
        spec_policy=SpecPolicy(None),
    ).advance(StewardAdvanceRequest(tmp_path, 31))

    assert result.status is StewardAdvanceStatus.WAITING
    assert result.phase == "awaiting_spec_approval"
    assert ledger.commits == []


def test_steward_duplicate_event_is_a_no_write_wait(
    tmp_path: Path,
) -> None:
    event = _event("event-1", EventKind.ISSUE_CREATED)
    state = OptimizationCampaign().advance(
        __import__(
            "foundry_opt.orchestration",
            fromlist=["AdvanceRequest"],
        ).AdvanceRequest(31, None, (event,))
    ).state
    snapshot = type(
        "Snapshot",
        (),
        {
            "revision": "a" * 40,
            "state": state,
            "inbox": (event,),
            "outbox": (),
        },
    )()
    ledger = Ledger(snapshot)
    service = StewardAdvanceService(
        ledger=ledger,
        inbox=Inbox((event, event)),
    )

    result = service.advance(StewardAdvanceRequest(tmp_path, 31))

    assert result.status is StewardAdvanceStatus.WAITING
    assert result.revision == "a" * 40
    assert ledger.commits == []


def test_steward_records_stale_event_as_consumed_wait(
    tmp_path: Path,
) -> None:
    from foundry_opt.orchestration import AdvanceRequest

    created = _event("event-1", EventKind.ISSUE_CREATED)
    edited = _event("event-2", EventKind.ISSUE_EDITED)
    state = OptimizationCampaign().advance(
        AdvanceRequest(31, None, (created, edited))
    ).state
    snapshot = type(
        "Snapshot",
        (),
        {
            "revision": "a" * 40,
            "state": state,
            "inbox": (created, edited),
            "outbox": (),
        },
    )()
    stale = _event(
        "event-stale",
        EventKind.ISSUE_CLOSED,
        generation=1,
    )
    ledger = Ledger(snapshot)

    result = StewardAdvanceService(
        ledger=ledger,
        inbox=Inbox((stale,)),
    ).advance(StewardAdvanceRequest(tmp_path, 31))

    assert result.status is StewardAdvanceStatus.WAITING
    assert len(ledger.commits) == 1
    assert ledger.commits[0]["inbox"] == (stale,)
    assert ledger.commits[0]["outbox"][0].kind == "campaign_waiting"


def test_steward_no_event_waits_without_writing(
    tmp_path: Path,
) -> None:
    event = _event("event-1", EventKind.ISSUE_CREATED)
    from foundry_opt.orchestration import AdvanceRequest

    state = OptimizationCampaign().advance(
        AdvanceRequest(31, None, (event,))
    ).state
    snapshot = type(
        "Snapshot",
        (),
        {
            "revision": "a" * 40,
            "state": state,
            "inbox": (event,),
            "outbox": (),
        },
    )()
    ledger = Ledger(snapshot)

    result = StewardAdvanceService(
        ledger=ledger,
        inbox=Inbox(()),
    ).advance(StewardAdvanceRequest(tmp_path, 31))

    assert result.status is StewardAdvanceStatus.WAITING
    assert result.exit_code == 0
    assert ledger.commits == []


def test_steward_cas_conflict_is_typed_strict_failure(
    tmp_path: Path,
) -> None:
    result = StewardAdvanceService(
        ledger=Ledger(conflict=True),
        inbox=Inbox((_event("event-1", EventKind.ISSUE_CREATED),)),
    ).advance(StewardAdvanceRequest(tmp_path, 31))

    assert result.status is StewardAdvanceStatus.CONFLICT
    assert result.exit_code == 1
    assert result.to_dict()["code"] == "state_ref_conflict"


def test_steward_without_state_or_events_is_blocked(
    tmp_path: Path,
) -> None:
    result = StewardAdvanceService(
        ledger=Ledger(),
        inbox=Inbox(()),
    ).advance(StewardAdvanceRequest(tmp_path, 31))

    assert result.status is StewardAdvanceStatus.BLOCKED
    assert result.exit_code == 1
    assert result.to_dict()["code"] == "campaign_not_initialized"


def test_git_campaign_inbox_reads_transport_events_for_requested_issue(
    tmp_path: Path,
) -> None:
    event = _event("event-1", EventKind.ISSUE_CREATED)

    class RecordedInbox:
        def events(self, issue_number: int):
            assert issue_number == 31
            return (event,)

    inbox = GitCampaignInbox(
        factory=lambda repository_root: RecordedInbox()
    )

    assert inbox.consume(
        StewardAdvanceRequest(tmp_path, 31),
        None,
    ) == (event,)
