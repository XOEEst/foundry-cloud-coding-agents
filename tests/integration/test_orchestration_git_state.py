from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from threading import Barrier

import pytest

from foundry_opt.orchestration import (
    AdvanceRequest,
    CampaignEvent,
    CandidateRecord,
    EventKind,
    GitStateRef,
    OptimizationCampaign,
    OutboxRecord,
    StateRefConflictError,
    StateRefCorruptionError,
    StateRefPrivacyError,
    StateObject,
)
from foundry_opt.orchestration.issue_intake import GitIssueEventInbox


NOW = datetime(2026, 7, 31, tzinfo=UTC)
V1_STATE_FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "orchestration"
    / "state-v1"
)
V1_AWAITING_SPEC_FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "orchestration"
    / "state-v1-awaiting-spec-approval"
)


def _run(arguments: tuple[str, ...], cwd: Path) -> str:
    return subprocess.run(
        arguments,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _repository(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    repository = tmp_path / "repository"
    _run(("git", "init", "--bare", str(origin)), tmp_path)
    _run(("git", "init", "-b", "main", str(repository)), tmp_path)
    _run(("git", "config", "user.name", "State Test"), repository)
    _run(
        ("git", "config", "user.email", "state@example.invalid"),
        repository,
    )
    (repository / "README.md").write_text("state tests\n", encoding="utf-8")
    _run(("git", "add", "."), repository)
    _run(("git", "commit", "-m", "baseline"), repository)
    _run(("git", "remote", "add", "origin", str(origin)), repository)
    _run(("git", "push", "-u", "origin", "main"), repository)
    return repository, origin


def _event(
    event_id: str,
    kind: EventKind,
    *,
    generation: int = 1,
    **payload: object,
) -> CampaignEvent:
    return CampaignEvent(
        event_id=event_id,
        kind=kind,
        generation=generation,
        occurred_at=NOW,
        payload=payload,
    )


def _install_state_fixture(
    repository: Path,
    fixture: Path = V1_STATE_FIXTURE,
) -> None:
    _run(("git", "checkout", "--orphan", "state-v1-fixture"), repository)
    _run(("git", "rm", "-rf", "."), repository)
    shutil.copytree(fixture, repository, dirs_exist_ok=True)
    _run(("git", "add", "."), repository)
    _run(("git", "commit", "-m", "install v1 state fixture"), repository)
    _run(
        (
            "git",
            "push",
            "origin",
            "HEAD:refs/heads/foundry-opt/state/issue-31",
        ),
        repository,
    )
    _run(("git", "checkout", "main"), repository)


def test_state_ref_replays_exact_historical_v1_fixture(
    tmp_path: Path,
) -> None:
    repository, _ = _repository(tmp_path)
    _install_state_fixture(repository)

    loaded = GitStateRef().load(repository, 31)

    assert loaded is not None
    assert loaded.state.schema_version == 1
    assert loaded.state.phase.value == "specification"
    assert loaded.state.processed_event_ids == ("event-1",)
    assert loaded.state.spec_base_ref_name is None
    assert loaded.state.spec_files == ()


def test_state_ref_replays_historical_v1_awaiting_spec_fixture(
    tmp_path: Path,
) -> None:
    repository, _ = _repository(tmp_path)
    _install_state_fixture(repository, V1_AWAITING_SPEC_FIXTURE)

    loaded = GitStateRef().load(repository, 31)

    assert loaded is not None
    assert loaded.state.schema_version == 1
    assert loaded.state.phase.value == "awaiting_spec_approval"
    assert loaded.state.spec_sha256 == "d" * 64
    assert loaded.state.spec_head_commit is None
    assert [event.kind for event in loaded.inbox] == [
        EventKind.ISSUE_CREATED,
        EventKind.SPEC_REVIEW_REQUIRED,
    ]

    recovery = _event(
        "spec-rematerialized-1-dddddddddddddddd",
        EventKind.SPEC_REVIEW_REQUIRED,
        base_ref_name="main",
        files=[
            {
                "path": (
                    ".foundry-optimizer/specs/issue-31/"
                    "optimization-spec.yaml"
                ),
                "sha256": "e" * 64,
            }
        ],
        head_commit="a" * 40,
        spec_sha256="d" * 64,
        tree_sha="b" * 40,
    )
    recovered = OptimizationCampaign().advance(
        AdvanceRequest(31, loaded.state, (recovery,))
    ).state
    intent = OutboxRecord(
        "spec-planner-1-legacy-dddddddddddddddd",
        "specialist_work_request",
        generation=1,
        sequence=recovered.sequence,
        payload={"issue_number": 31},
    )

    migrated = GitStateRef().commit(
        repository,
        issue_number=31,
        expected_revision=loaded.revision,
        state=recovered,
        inbox=(recovery,),
        outbox=(intent,),
    )

    assert migrated.state.schema_version == 2
    assert migrated.state.spec_head_commit == "a" * 40
    assert migrated.state.spec_tree_sha == "b" * 40
    assert migrated.state.spec_files[0].sha256 == "e" * 64
    assert migrated.outbox[-1] == intent
    assert GitStateRef().load(repository, 31) == migrated


def test_state_ref_migrates_v1_fixture_on_next_write_without_rehashing_history(
    tmp_path: Path,
) -> None:
    repository, _ = _repository(tmp_path)
    _install_state_fixture(repository)
    store = GitStateRef()
    loaded = store.load(repository, 31)
    assert loaded is not None
    outbox = OutboxRecord(
        "dispatch-migration",
        "continue_campaign",
        generation=1,
        sequence=1,
        payload={"issue_number": 31},
    )

    migrated = store.commit(
        repository,
        issue_number=31,
        expected_revision=loaded.revision,
        state=loaded.state,
        outbox=(outbox,),
    )

    journal = _run(
        ("git", "show", f"{migrated.revision}:journal.jsonl"),
        repository,
    ).splitlines()
    snapshot = json.loads(
        _run(
            ("git", "show", f"{migrated.revision}:snapshot.json"),
            repository,
        )
    )
    historical_event = json.loads(
        _run(
            (
                "git",
                "show",
                f"{migrated.revision}:inbox/event-1.json",
            ),
            repository,
        )
    )

    assert journal[0] == (
        V1_STATE_FIXTURE / "journal.jsonl"
    ).read_text(encoding="utf-8").strip()
    assert json.loads(journal[1])["schema_version"] == 3
    assert json.loads(journal[1])["state_schema_version"] == 2
    assert snapshot["schema_version"] == 3
    assert snapshot["state"]["schema_version"] == 2
    assert snapshot["state"]["spec_base_ref_name"] is None
    assert snapshot["state"]["spec_files"] == []
    assert historical_event["schema_version"] == 1
    assert migrated.state.schema_version == 2
    assert store.load(repository, 31) == migrated


def test_state_ref_creates_and_loads_atomic_snapshot_inbox_and_outbox(
    tmp_path: Path,
) -> None:
    repository, origin = _repository(tmp_path)
    event = _event("event-1", EventKind.ISSUE_CREATED)
    state = OptimizationCampaign().advance(
        AdvanceRequest(31, None, (event,))
    ).state
    outbox = OutboxRecord(
        record_id="dispatch-1",
        kind="continue_campaign",
        generation=1,
        sequence=1,
        payload={"issue_number": 31},
    )

    created = GitStateRef().commit(
        repository,
        issue_number=31,
        expected_revision=None,
        state=state,
        inbox=(event,),
        outbox=(outbox,),
    )
    loaded = GitStateRef().load(repository, 31)

    assert loaded == created
    assert loaded is not None
    assert loaded.state == state
    assert loaded.inbox == (event,)
    assert loaded.outbox == (outbox,)
    assert (
        _run(
            (
                "git",
                f"--git-dir={origin}",
                "for-each-ref",
                "--format=%(refname)",
                "refs/heads/foundry-opt/state",
            ),
            tmp_path,
        )
        == "refs/heads/foundry-opt/state/issue-31\n"
    )


def test_state_ref_persists_exact_candidate_objects(
    tmp_path: Path,
) -> None:
    repository, _ = _repository(tmp_path)
    event = _event("event-1", EventKind.ISSUE_CREATED)
    state = OptimizationCampaign().advance(
        AdvanceRequest(31, None, (event,))
    ).state
    attestation = b'{"candidate_id":"candidate-1","schema_version":1}\n'
    evidence = b'{"metrics":{"quality":0.9},"schema_version":1}\n'
    patch = b"diff --git a/agent.md b/agent.md\n"
    objects = (
        StateObject(
            "objects/patches/" + hashlib.sha256(patch).hexdigest() + ".patch",
            patch,
        ),
        StateObject(
            "objects/candidates/g1-candidate-1.json",
            attestation,
        ),
        StateObject(
            "objects/evidence/" + hashlib.sha256(evidence).hexdigest() + ".json",
            evidence,
        ),
    )

    created = GitStateRef().commit(
        repository,
        issue_number=31,
        expected_revision=None,
        state=state,
        inbox=(event,),
        objects=objects,
    )

    assert created.objects == tuple(
        sorted(objects, key=lambda item: item.path)
    )
    assert GitStateRef().load(repository, 31) == created
    assert (
        subprocess.run(
            (
                "git",
                "show",
                (
                    f"{created.revision}:objects/patches/"
                    f"{hashlib.sha256(patch).hexdigest()}.patch"
                ),
            ),
            cwd=repository,
            check=True,
            capture_output=True,
        ).stdout
        == patch
    )


def test_state_ref_rejects_stale_compare_and_swap_from_another_clone(
    tmp_path: Path,
) -> None:
    repository, origin = _repository(tmp_path)
    other = tmp_path / "other"
    _run(("git", "clone", str(origin), str(other)), tmp_path)
    store = GitStateRef()
    created_event = _event("event-1", EventKind.ISSUE_CREATED)
    created_state = OptimizationCampaign().advance(
        AdvanceRequest(31, None, (created_event,))
    ).state
    created = store.commit(
        repository,
        issue_number=31,
        expected_revision=None,
        state=created_state,
        inbox=(created_event,),
    )
    stale = store.load(other, 31)
    assert stale == created

    edited_event = _event("event-2", EventKind.ISSUE_EDITED)
    edited_state = OptimizationCampaign().advance(
        AdvanceRequest(31, created_state, (edited_event,))
    ).state
    store.commit(
        repository,
        issue_number=31,
        expected_revision=created.revision,
        state=edited_state,
        inbox=(edited_event,),
    )

    with pytest.raises(StateRefConflictError, match="changed"):
        store.commit(
            other,
            issue_number=31,
            expected_revision=stale.revision,
            state=stale.state,
            outbox=(
                OutboxRecord(
                    "dispatch-stale",
                    "continue_campaign",
                    generation=1,
                    sequence=1,
                    payload={"issue_number": 31},
                ),
            ),
        )


def test_state_ref_replays_append_only_journal_and_preserves_counters(
    tmp_path: Path,
) -> None:
    repository, _ = _repository(tmp_path)
    store = GitStateRef()
    event_1 = _event("event-1", EventKind.ISSUE_CREATED)
    state_1 = OptimizationCampaign().advance(
        AdvanceRequest(31, None, (event_1,))
    ).state
    revision_1 = store.commit(
        repository,
        issue_number=31,
        expected_revision=None,
        state=state_1,
        inbox=(event_1,),
    )
    events = (
        _event(
            "event-2",
            EventKind.SPEC_POLICY_APPROVED,
            spec_sha256="a" * 64,
        ),
        _event(
            "event-3",
            EventKind.BASELINE_COMPLETED,
            evaluation_id="baseline-1",
        ),
        _event(
            "event-4",
            EventKind.CANDIDATE_EVALUATED,
            candidate_id="candidate-1",
            eligible=True,
            evidence_sha256="b" * 64,
        ),
    )
    state_2 = OptimizationCampaign().advance(
        AdvanceRequest(31, state_1, events)
    ).state
    revision_2 = store.commit(
        repository,
        issue_number=31,
        expected_revision=revision_1.revision,
        state=state_2,
        inbox=events,
    )
    journal_2 = _run(
        ("git", "show", f"{revision_2.revision}:journal.jsonl"),
        repository,
    )
    edited = _event("event-5", EventKind.ISSUE_EDITED)
    state_3 = OptimizationCampaign().advance(
    AdvanceRequest(31, state_2, (edited,))
    ).state
    revision_3 = store.commit(
    repository,
    issue_number=31,
    expected_revision=revision_2.revision,
    state=state_3,
    inbox=(edited,),
    )

    loaded = store.load(repository, 31)
    journal_3 = _run(
    ("git", "show", f"{revision_3.revision}:journal.jsonl"),
    repository,
    )

    assert loaded is not None
    assert loaded.state.generation == 2
    assert loaded.state.sequence == 5
    assert revision_2.state.candidates == (
    CandidateRecord("candidate-1", True, "b" * 64),
    )
    assert type(revision_2.state.candidates[0]) is CandidateRecord
    assert journal_3.startswith(journal_2)


def test_state_ref_fails_closed_when_journal_hash_does_not_match(
    tmp_path: Path,
) -> None:
    repository, origin = _repository(tmp_path)
    event = _event("event-1", EventKind.ISSUE_CREATED)
    state = OptimizationCampaign().advance(
        AdvanceRequest(31, None, (event,))
    ).state
    GitStateRef().commit(
        repository,
        issue_number=31,
        expected_revision=None,
        state=state,
        inbox=(event,),
    )
    corrupt = tmp_path / "corrupt"
    _run(("git", "clone", str(origin), str(corrupt)), tmp_path)
    _run(
        (
            "git",
            "fetch",
            "origin",
            "refs/heads/foundry-opt/state/issue-31",
        ),
        corrupt,
    )
    _run(("git", "checkout", "-b", "corrupt", "FETCH_HEAD"), corrupt)
    _run(("git", "config", "user.name", "Corruption Test"), corrupt)
    _run(
        ("git", "config", "user.email", "corrupt@example.invalid"),
        corrupt,
    )
    journal = corrupt / "journal.jsonl"
    journal_entry = json.loads(journal.read_text(encoding="utf-8"))
    journal_entry["state_sha256"] = "0" * 64
    journal.write_text(
        json.dumps(
            journal_entry,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _run(("git", "add", "journal.jsonl"), corrupt)
    _run(("git", "commit", "-m", "corrupt journal"), corrupt)
    _run(
        (
            "git",
            "push",
            "--force",
            "origin",
            "HEAD:refs/heads/foundry-opt/state/issue-31",
        ),
        corrupt,
    )

    with pytest.raises(StateRefCorruptionError, match="hash"):
        GitStateRef().load(repository, 31)


def test_state_ref_privacy_allowlist_rejects_raw_and_token_like_fields(
    tmp_path: Path,
) -> None:
    repository, _ = _repository(tmp_path)
    store = GitStateRef()
    created_event = _event("event-1", EventKind.ISSUE_CREATED)
    created_state = OptimizationCampaign().advance(
        AdvanceRequest(31, None, (created_event,))
    ).state
    created = store.commit(
        repository,
        issue_number=31,
        expected_revision=None,
        state=created_state,
        inbox=(created_event,),
    )
    private_event = _event(
        "event-2",
        EventKind.ISSUE_EDITED,
        raw_prompt="do not persist this",
    )
    private_state = OptimizationCampaign().advance(
        AdvanceRequest(31, created_state, (private_event,))
    ).state

    with pytest.raises(StateRefPrivacyError, match="privacy allowlist"):
        store.commit(
            repository,
            issue_number=31,
            expected_revision=created.revision,
            state=private_state,
            inbox=(private_event,),
        )
    for field in (
        "secret",
        "raw_prompt",
        "response",
        "trace",
        "dataset_row",
        "tool_payload",
        "api_token",
    ):
        with pytest.raises(
            StateRefPrivacyError,
            match="privacy allowlist",
        ):
            OutboxRecord(
                "dispatch-private",
                "continue_campaign",
                generation=1,
                sequence=1,
                payload={field: "private"},
            )

    assert store.load(repository, 31) == created


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("state-version", "unsupported campaign state schema_version"),
        ("candidate-version", "unsupported candidate schema_version"),
        ("candidate-field", "candidate fields are invalid"),
    ),
)
def test_state_ref_deserialization_is_strict_and_versioned(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    repository, origin = _repository(tmp_path)
    event = _event("event-1", EventKind.ISSUE_CREATED)
    state = OptimizationCampaign().advance(
        AdvanceRequest(31, None, (event,))
    ).state
    GitStateRef().commit(
        repository,
        issue_number=31,
        expected_revision=None,
        state=state,
        inbox=(event,),
    )
    corrupt = tmp_path / "strict"
    _run(("git", "clone", str(origin), str(corrupt)), tmp_path)
    _run(
        (
            "git",
            "fetch",
            "origin",
            "refs/heads/foundry-opt/state/issue-31",
        ),
        corrupt,
    )
    _run(("git", "checkout", "-b", "strict", "FETCH_HEAD"), corrupt)
    _run(("git", "config", "user.name", "Strict Test"), corrupt)
    _run(
        ("git", "config", "user.email", "strict@example.invalid"),
        corrupt,
    )
    snapshot_path = corrupt / "snapshot.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if mutation == "state-version":
        snapshot["state"]["schema_version"] = 3
    elif mutation == "candidate-version":
        snapshot["state"]["candidates"] = [
            {
                "candidate_id": "candidate-1",
                "eligible": True,
                "evidence_sha256": "b" * 64,
                "schema_version": 2,
            }
        ]
    else:
        snapshot["state"]["candidates"] = [
            {
                "candidate_id": "candidate-1",
                "eligible": True,
                "evidence_sha256": "b" * 64,
                "raw_response": "private",
                "schema_version": 1,
            }
        ]
    snapshot_path.write_text(
        json.dumps(snapshot, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _run(("git", "add", "snapshot.json"), corrupt)
    _run(("git", "commit", "-m", "mutate snapshot"), corrupt)
    _run(
        (
            "git",
            "push",
            "--force",
            "origin",
            "HEAD:refs/heads/foundry-opt/state/issue-31",
        ),
        corrupt,
    )

    with pytest.raises(StateRefCorruptionError, match=message):
        GitStateRef().load(repository, 31)


def test_issue_event_inbox_appends_idempotent_transport_events(
    tmp_path: Path,
) -> None:
    repository, _ = _repository(tmp_path)
    inbox = GitIssueEventInbox(repository)
    opened = _event("github-delivery-1", EventKind.ISSUE_CREATED)
    edited = _event(
        "github-delivery-2",
        EventKind.ISSUE_EDITED,
        generation=2,
    )

    assert inbox.append(31, opened) is True
    assert inbox.append(31, opened) is False
    assert inbox.append(31, edited) is True

    assert inbox.events(31) == (opened, edited)
    assert inbox.issue_numbers() == (31,)


def test_issue_event_inbox_durably_accepts_reordered_candidate_pr_events(
    tmp_path: Path,
) -> None:
    repository, _ = _repository(tmp_path)
    inbox = GitIssueEventInbox(repository)
    opened = _event("github-delivery-1", EventKind.ISSUE_CREATED)
    synchronized = _event(
        "github-pr-sync",
        EventKind.CANDIDATE_PR_SYNCHRONIZED,
        binding_sha256="a" * 64,
        candidate_id="candidate-1",
        head_commit="b" * 40,
        pull_request_number=91,
    )
    pr_opened = _event(
        "github-pr-open",
        EventKind.CANDIDATE_PR_OPENED,
        binding_sha256="a" * 64,
        candidate_id="candidate-1",
        head_commit="c" * 40,
        pull_request_number=91,
    )

    assert inbox.append(31, opened) is True
    assert inbox.append(31, synchronized) is True
    assert inbox.append(31, pr_opened) is True
    assert inbox.append(31, synchronized) is False

    assert inbox.events(31) == (opened, synchronized, pr_opened)


def test_issue_event_inbox_durably_records_specification_pr_wakeup(
    tmp_path: Path,
) -> None:
    repository, _ = _repository(tmp_path)
    inbox = GitIssueEventInbox(repository)
    opened = _event("github-delivery-1", EventKind.ISSUE_CREATED)
    spec_pr = _event(
        "github-spec-pr",
        EventKind.SPEC_PR_MERGED,
        head_commit="b" * 40,
        merge_commit="c" * 40,
        pull_request_number=91,
        spec_sha256="a" * 64,
    )

    assert inbox.append(31, opened) is True
    assert inbox.append(31, spec_pr) is True
    assert inbox.events(31) == (opened, spec_pr)


def test_issue_event_inbox_retries_concurrent_trusted_deliveries(
    tmp_path: Path,
) -> None:
    repository, origin = _repository(tmp_path)
    GitIssueEventInbox(repository).append(
        31,
        _event("github-delivery-1", EventKind.ISSUE_CREATED),
    )
    clones = []
    for name in ("first", "second"):
        clone = tmp_path / name
        _run(("git", "clone", str(origin), str(clone)), tmp_path)
        _run(("git", "config", "user.name", "State Test"), clone)
        _run(
            ("git", "config", "user.email", "state@example.invalid"),
            clone,
        )
        clones.append(clone)
    inboxes = [GitIssueEventInbox(clone) for clone in clones]
    barrier = Barrier(2)

    def synchronized(original):
        calls = 0

        def load(issue_number):
            nonlocal calls
            loaded = original(issue_number)
            calls += 1
            if calls == 1:
                barrier.wait(timeout=10)
            return loaded

        return load

    for inbox in inboxes:
        inbox._load = synchronized(inbox._load)  # type: ignore[method-assign]
    events = (
        _event(
            "github-spec-1",
            EventKind.SPEC_PR_OPENED,
            head_commit="b" * 40,
            pull_request_number=91,
            spec_sha256="a" * 64,
        ),
        _event(
            "github-spec-2",
            EventKind.SPEC_PR_EDITED,
            head_commit="c" * 40,
            pull_request_number=91,
            spec_sha256="a" * 64,
        ),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(
                lambda pair: pair[0].append(31, pair[1]),
                zip(inboxes, events, strict=True),
            )
        )

    assert results == (True, True)
    persisted = GitIssueEventInbox(repository).events(31)
    assert {event.event_id for event in persisted} == {
        "github-delivery-1",
        "github-spec-1",
        "github-spec-2",
    }
