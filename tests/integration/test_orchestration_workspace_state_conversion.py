from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from foundry_opt.orchestration import (
    AdvanceRequest,
    CampaignEvent,
    EventKind,
    GitStateRef,
    GitWorkspaceStore,
    OptimizationCampaign,
    StateObject,
    WorkspaceConflictError,
    WorkspacePhase,
    WorkspaceStateConversionError,
    convert_workspace_state_v3,
)


NOW = datetime(2026, 8, 12, tzinfo=UTC)


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
    _run(("git", "config", "user.name", "Conversion Test"), repository)
    _run(
        ("git", "config", "user.email", "conversion@example.invalid"),
        repository,
    )
    (repository / "README.md").write_text("conversion tests\n")
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
    return CampaignEvent(
        event_id=event_id,
        kind=kind,
        generation=1,
        occurred_at=NOW,
        payload=payload,
    )


def _install_selected_v3(
    repository: Path,
    *,
    include_patch: bool,
) -> str:
    evidence_sha256 = "b" * 64
    patch = b"diff --git a/agent.py b/agent.py\n"
    patch_sha256 = hashlib.sha256(patch).hexdigest()
    events = (
        _event("event-1", EventKind.ISSUE_CREATED),
        _event(
            "event-2",
            EventKind.SPEC_POLICY_APPROVED,
            spec_sha256="a" * 64,
        ),
        _event(
            "event-3",
            EventKind.BASELINE_COMPLETED,
            evaluation_id="baseline-eval-1",
        ),
        _event(
            "event-4",
            EventKind.CANDIDATE_EVALUATED,
            candidate_id="candidate-1",
            eligible=True,
            evidence_sha256=evidence_sha256,
        ),
        _event(
            "event-5",
            EventKind.CANDIDATE_WORKERS_COMPLETED,
            attempted_count=1,
            eligible_count=1,
            stop_reason="budget_complete",
        ),
        _event("event-6", EventKind.SLATE_PUBLISHED),
        _event(
            "event-7",
            EventKind.CANDIDATE_MERGED,
            candidate_id="candidate-1",
            merge_commit="c" * 40,
        ),
    )
    state = OptimizationCampaign().advance(
        AdvanceRequest(31, None, events)
    ).state
    candidate = {
        "base_commit": "d" * 40,
        "bundle_sha256": "e" * 64,
        "candidate_id": "candidate-1",
        "draft_id": "draft-1",
        "eligible": True,
        "evaluation_id": "candidate-eval-1",
        "evidence_sha256": evidence_sha256,
        "issue_number": 31,
        "metrics": {"policy_coverage": 0.75},
        "patch_sha256": patch_sha256,
        "expected_tree": "f" * 40,
        "required_checks": {"tests": "success"},
        "required_checks_provenance": (
            f"trusted-selector:head:{'c' * 40}"
        ),
        "workspace_pull_request_number": 104,
    }
    candidate_content = (
        json.dumps(
            candidate,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
    objects = [
        StateObject(
            "objects/candidates/g1-candidate-1.json",
            candidate_content,
        )
    ]
    if include_patch:
        objects.append(
            StateObject(
                f"objects/patches/{patch_sha256}.patch",
                patch,
            )
        )
    snapshot = GitStateRef().commit(
        repository,
        issue_number=31,
        expected_revision=None,
        state=state,
        inbox=events,
        objects=tuple(objects),
    )
    return snapshot.revision


def _remote_revision(repository: Path, ref: str) -> str:
    return _run(("git", "ls-remote", "origin", ref), repository).split()[0]


def test_v3_conversion_before_selection_omits_optional_artifacts(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    event = _event("event-1", EventKind.ISSUE_CREATED)
    state = OptimizationCampaign().advance(
        AdvanceRequest(31, None, (event,))
    ).state
    source = GitStateRef().commit(
        repository,
        issue_number=31,
        expected_revision=None,
        state=state,
        inbox=(event,),
    )

    payload = convert_workspace_state_v3(repository, 31)
    converted = GitWorkspaceStore(repository).write_conversion(
        payload,
        target_ref="refs/heads/foundry-opt/converted/specification-31",
        expected_revision=None,
    )

    assert converted.phase is WorkspacePhase.SPECIFICATION
    assert converted.candidates == ()
    assert converted.selected_patch is None
    assert _run(
        ("git", "ls-tree", "-r", "--name-only", converted.revision),
        repository,
    ).splitlines() == ["journal.jsonl", "snapshot.json"]
    assert _remote_revision(
        repository,
        "refs/heads/foundry-opt/state/issue-31",
    ) == source.revision


def test_v3_conversion_is_canonical_one_way_and_idempotent(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    source_revision = _install_selected_v3(
        repository,
        include_patch=True,
    )
    source_ref = "refs/heads/foundry-opt/state/issue-31"
    target_ref = "refs/heads/foundry-opt/converted/issue-31"

    first_payload = convert_workspace_state_v3(repository, 31)
    second_payload = convert_workspace_state_v3(repository, 31)

    assert first_payload == second_payload
    assert first_payload.canonical_bytes.endswith(b"\n")
    assert first_payload.source_revision == source_revision
    assert first_payload.transitions[-1].phase is WorkspacePhase.DEPLOYMENT
    assert first_payload.transitions[-1].candidates[0].metrics == {
        "policy_coverage": 0.75
    }
    assert first_payload.transitions[-1].candidates[0].selected is True
    assert first_payload.transitions[-1].selected_patch == (
        b"diff --git a/agent.py b/agent.py\n"
    )
    assert first_payload.transitions[-1].external_operation_ids == (
        "baseline-eval-1",
        "draft-1",
        "candidate-eval-1",
        "merge_commit:" + "c" * 40,
    )
    assert first_payload.transitions[-1].lineage is not None
    assert first_payload.transitions[-1].lineage.spec_sha256 == "a" * 64
    assert first_payload.transitions[-1].lineage.bundle_sha256 == "e" * 64
    assert (
        first_payload.transitions[-1].workspace_pull_request_number
        == 104
    )

    store = GitWorkspaceStore(repository)
    first = store.write_conversion(
        first_payload,
        target_ref=target_ref,
        expected_revision=None,
    )
    second = store.write_conversion(
        second_payload,
        target_ref=target_ref,
        expected_revision=None,
    )

    assert first == second
    assert first.phase is WorkspacePhase.DEPLOYMENT
    assert _remote_revision(repository, source_ref) == source_revision
    assert _remote_revision(repository, target_ref) == first.revision
    assert _run(
        ("git", "ls-tree", "-r", "--name-only", first.revision),
        repository,
    ).splitlines() == [
        "evidence/candidates.json",
        "journal.jsonl",
        "patches/selected.patch",
        "snapshot.json",
    ]
    snapshot = json.loads(
        _run(
            ("git", "show", f"{first.revision}:snapshot.json"),
            repository,
        )
    )
    assert snapshot["schema_version"] == 4
    assert snapshot["state"]["phase"] == "deployment"
    assert _remote_revision(repository, source_ref) == source_revision

    tree = _run(
        ("git", "show", "-s", "--format=%T", first.revision),
        repository,
    ).strip()
    wrong_parent = _run(("git", "rev-parse", "HEAD"), repository).strip()
    rewritten = subprocess.run(
        ("git", "commit-tree", tree, "-p", wrong_parent),
        cwd=repository,
        check=True,
        capture_output=True,
        input="wrong conversion lineage\n",
        text=True,
    ).stdout.strip()
    _run(
        ("git", "push", "--force", "origin", f"{rewritten}:{target_ref}"),
        repository,
    )
    with pytest.raises(WorkspaceConflictError, match="source lineage"):
        store.write_conversion(
            first_payload,
            target_ref=target_ref,
            expected_revision=None,
        )

    occupied_ref = "refs/heads/foundry-opt/converted/occupied-31"
    _run(
        ("git", "push", "origin", f"HEAD:{occupied_ref}"),
        repository,
    )
    with pytest.raises(WorkspaceConflictError):
        store.write_conversion(
            first_payload,
            target_ref=occupied_ref,
            expected_revision=None,
        )
    assert _remote_revision(repository, source_ref) == source_revision


def test_v3_conversion_fails_when_selected_patch_lineage_is_missing(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    source_revision = _install_selected_v3(
        repository,
        include_patch=False,
    )

    with pytest.raises(
        WorkspaceStateConversionError,
        match="selected patch lineage",
    ):
        convert_workspace_state_v3(repository, 31)

    assert _remote_revision(
        repository,
        "refs/heads/foundry-opt/state/issue-31",
    ) == source_revision
    assert (
        _run(
            (
                "git",
                "ls-remote",
                "origin",
                "refs/heads/foundry-opt/converted/issue-31",
            ),
            repository,
        )
        == ""
    )
