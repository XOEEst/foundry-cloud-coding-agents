from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess

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
)


NOW = datetime(2026, 7, 31, tzinfo=UTC)


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
        snapshot["state"]["schema_version"] = 2
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
