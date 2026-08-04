from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import re
import subprocess

import pytest

from foundry_opt.adapters.commands import SubprocessCommandRunner
from foundry_opt.orchestration import (
    AdvanceRequest,
    CampaignEvent,
    EventKind,
    GitStateRef,
    OptimizationCampaign,
    OutboxRecord,
)
from foundry_opt.orchestration.candidate_workers import (
    CandidateDesignSubmissionRequest,
    CandidateDesignSubmissionService,
    CandidateDesignSubmissionStatus,
)
from foundry_opt.orchestration.handoff import (
    CandidateDesignHandoff,
    CloudHandoffStore,
    HandoffApplyService,
    HandoffApplyStatus,
    StewardStateHandoff,
    TrustedHandoffRequest,
)
from foundry_opt.orchestration.issue_intake import GitIssueEventInbox
from foundry_opt.orchestration.steward import (
    GitCampaignInbox,
    StewardAdvanceRequest,
    StewardAdvanceService,
    StewardAdvanceStatus,
)
from foundry_opt.optimization.production import (
    _ProductionCandidateDesignRepository,
)


NOW = datetime(2026, 8, 3, tzinfo=UTC)


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, Path, str]:
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(origin))
    repository = tmp_path / "repository"
    _git(tmp_path, "init", "-b", "main", str(repository))
    _git(repository, "config", "user.name", "Copilot")
    _git(repository, "config", "user.email", "copilot@example.invalid")
    _git(repository, "config", "core.longpaths", "true")
    (repository / "README.md").write_text("handoff\n", encoding="utf-8")
    (repository / "agent").mkdir()
    (repository / "agent" / "instructions.md").write_text(
        "baseline\n",
        encoding="utf-8",
    )
    _git(repository, "add", "README.md", "agent/instructions.md")
    _git(repository, "commit", "-m", "baseline")
    base = _git(repository, "rev-parse", "HEAD")
    _git(repository, "remote", "add", "origin", str(origin))
    _git(repository, "push", "-u", "origin", "main")
    _git(repository, "checkout", "-b", "copilot/steward-issue-31")
    _git(
        repository,
        "push",
        "-u",
        "origin",
        "copilot/steward-issue-31",
    )
    _install_proxy_hook(origin)
    return repository, origin, base


def _install_proxy_hook(origin: Path) -> None:
    hook = origin / "hooks" / "post-receive"
    hook.write_text(
        "#!/bin/sh\n"
        'git_dir="${GIT_DIR:-.}"\n'
        "while read old new ref; do\n"
        '  case "$ref" in\n'
        "    refs/heads/foundry-opt/state/*|"
        "refs/heads/foundry-opt/design/*)\n"
        '      git --git-dir="$git_dir" update-ref -d "$ref" "$new"\n'
        "      ;;\n"
        "  esac\n"
        "done\n",
        encoding="utf-8",
        newline="\n",
    )
    hook.chmod(0o755)


def test_cloud_steward_persists_one_content_addressed_handoff(
    tmp_path: Path,
    monkeypatch,
    copilot_git_proxy,
) -> None:
    repository, origin, base = _repository(tmp_path)
    copilot_git_proxy.install(
        repository,
        origin,
        acknowledgement="absent",
    )
    monkeypatch.setenv(
        "COPILOT_AGENT_SESSION_ID",
        "11111111-2222-4333-8444-555555555555",
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo-org/optimizer")
    monkeypatch.delenv("COPILOT_CLI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    event = CampaignEvent(
        "github-run-1",
        EventKind.ISSUE_CREATED,
        1,
        NOW,
    )
    inbox = GitIssueEventInbox(repository)
    assert inbox.append(31, event) is True
    source_inbox_revision = _git(
        repository,
        "ls-remote",
        "--heads",
        "origin",
        "refs/heads/foundry-opt/inbox/issue-31",
    ).split()[0]

    result = StewardAdvanceService(
        inbox=GitCampaignInbox(),
        handoffs=CloudHandoffStore(),
    ).advance(StewardAdvanceRequest(repository, 31))

    assert result.status is StewardAdvanceStatus.WAITING
    assert result.disposition == "delegate"
    assert result.code == "state_handoff_created"
    assert (
        _git(
            repository,
            "ls-remote",
            "--heads",
            "origin",
            "refs/heads/foundry-opt/state/*",
        )
        == ""
    )
    head = _git(
        repository,
        "ls-remote",
        "--heads",
        "origin",
        "refs/heads/copilot/steward-issue-31",
    ).split()[0]
    changed = _git(
        repository,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        f"{head}^1",
        head,
    ).splitlines()
    assert len(changed) == 1
    path = changed[0]
    assert re.fullmatch(
        r"\.foundry-optimizer/handoffs/steward/"
        r"issue-31/g1/[0-9a-f]{64}\.json",
        path,
    )
    handoff = StewardStateHandoff.from_bytes(
        subprocess.run(
            ("git", "show", f"{head}:{path}"),
            cwd=repository,
            check=True,
            capture_output=True,
        ).stdout
    )
    parents = _git(repository, "rev-list", "--parents", "-n", "1", head).split()

    assert parents == [head, base, handoff.proposed_revision]
    assert handoff.issue_number == 31
    assert handoff.generation == 1
    assert handoff.expected_prior_revision is None
    assert handoff.source_inbox_revision == source_inbox_revision
    assert handoff.event_ids == ("github-run-1",)
    assert handoff.path == path
    assert handoff.handoff_id == Path(path).stem
    assert handoff.proposed_tree == _git(
        repository,
        "rev-parse",
        f"{handoff.proposed_revision}^{{tree}}",
    )
    assert handoff.payload_hashes.snapshot_sha256
    assert handoff.payload_hashes.journal_sha256
    assert handoff.payload_hashes.inbox[0].record_id == "github-run-1"
    assert "secret" not in handoff.content.decode("utf-8").casefold()
    assert "trace" not in handoff.content.decode("utf-8").casefold()
    assert "dataset_row" not in handoff.content.decode("utf-8").casefold()

    retry = StewardAdvanceService(
        inbox=GitCampaignInbox(),
        handoffs=CloudHandoffStore(),
    ).advance(StewardAdvanceRequest(repository, 31))

    assert retry.code == "state_handoff_created"
    assert _git(repository, "rev-parse", "HEAD") == head
    assert (
        _git(
            repository,
            "ls-remote",
            "--heads",
            "origin",
            "refs/heads/copilot/steward-issue-31",
        ).split()[0]
        == head
    )


@pytest.mark.parametrize(
    "acknowledgement",
    ("absent", "expected", "proposed", "unrelated"),
)
def test_verified_copilot_proxy_always_creates_state_handoff(
    tmp_path: Path,
    monkeypatch,
    copilot_git_proxy,
    acknowledgement: str,
) -> None:
    repository, origin, base = _repository(tmp_path)
    (origin / "hooks" / "post-receive").unlink()
    created = CampaignEvent(
        "github-run-1",
        EventKind.ISSUE_CREATED,
        1,
        NOW,
    )
    inbox = GitIssueEventInbox(repository)
    assert inbox.append(31, created) is True
    state = OptimizationCampaign().advance(
        AdvanceRequest(31, None, (created,))
    ).state
    current = GitStateRef().commit(
        repository,
        issue_number=31,
        expected_revision=None,
        state=state,
        inbox=(created,),
    )
    edited = CampaignEvent(
        "github-run-2",
        EventKind.ISSUE_EDITED,
        2,
        NOW + timedelta(minutes=1),
    )
    assert inbox.append(31, edited) is True
    proxy = copilot_git_proxy.install(
        repository,
        origin,
        acknowledgement=acknowledgement,
    )
    monkeypatch.setenv(
        "COPILOT_AGENT_SESSION_ID",
        "11111111-2222-4333-8444-555555555555",
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo-org/optimizer")
    monkeypatch.delenv("COPILOT_CLI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    result = StewardAdvanceService(
        inbox=GitCampaignInbox(),
        handoffs=CloudHandoffStore(),
    ).advance(StewardAdvanceRequest(repository, 31))

    assert result.status is StewardAdvanceStatus.WAITING
    assert result.disposition == "delegate"
    assert result.code == "state_handoff_created"
    assert proxy.real_revision(
        "refs/heads/foundry-opt/state/issue-31"
    ) == current.revision
    head = proxy.real_revision(
        "refs/heads/copilot/steward-issue-31"
    )
    assert head is not None
    path = _git(
        repository,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        f"{head}^1",
        head,
    )
    blob = _git(
        repository,
        "ls-tree",
        head,
        "--",
        path,
    ).split()[2]
    handoff = StewardStateHandoff.from_bytes(
        subprocess.run(
            ("git", "show", f"{head}:{path}"),
            cwd=repository,
            check=True,
            capture_output=True,
        ).stdout
    )
    assert handoff.expected_prior_revision == current.revision
    assert handoff.event_ids == ("github-run-2",)

    proxy.disable()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    applied = HandoffApplyService().apply(
        TrustedHandoffRequest(
            repository_root=repository,
            repository="octo-org/optimizer",
            repository_id=123,
            pull_request_number=90,
            author_login="copilot-swe-agent[bot]",
            base_repository="octo-org/optimizer",
            base_ref="main",
            base_revision=base,
            head_repository="octo-org/optimizer",
            head_ref="copilot/steward-issue-31",
            head_revision=head,
            handoff_path=path,
            handoff_blob=blob,
        )
    )

    assert applied.status is HandoffApplyStatus.APPLIED
    assert applied.snapshot is not None
    assert applied.snapshot.revision == handoff.proposed_revision


def test_local_copilot_cli_marker_does_not_enable_cloud_handoff(
    tmp_path: Path,
    monkeypatch,
    copilot_git_proxy,
) -> None:
    repository, origin, base = _repository(tmp_path)
    copilot_git_proxy.install(
        repository,
        origin,
        acknowledgement="absent",
    )
    monkeypatch.setenv(
        "COPILOT_AGENT_SESSION_ID",
        "11111111-2222-4333-8444-555555555555",
    )
    monkeypatch.setenv("COPILOT_CLI", "1")
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo-org/optimizer")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    event = CampaignEvent(
        "github-run-1",
        EventKind.ISSUE_CREATED,
        1,
        NOW,
    )
    assert GitIssueEventInbox(repository).append(31, event) is True

    result = StewardAdvanceService(
        inbox=GitCampaignInbox(),
        handoffs=CloudHandoffStore(),
    ).advance(StewardAdvanceRequest(repository, 31))

    assert result.status is StewardAdvanceStatus.FAILED
    assert result.code == "state_handoff_failed"
    assert _git(repository, "rev-parse", "HEAD") == base
    assert (
        _git(
            repository,
            "ls-tree",
            "-r",
            "--name-only",
            "HEAD",
            "--",
            ".foundry-optimizer/handoffs",
        )
        == ""
    )


def test_loopback_origin_without_copilot_markers_keeps_conflict_semantics(
    tmp_path: Path,
    monkeypatch,
    copilot_git_proxy,
) -> None:
    repository, origin, base = _repository(tmp_path)
    event = CampaignEvent(
        "github-run-1",
        EventKind.ISSUE_CREATED,
        1,
        NOW,
    )
    assert GitIssueEventInbox(repository).append(31, event) is True
    proxy = copilot_git_proxy.install(
        repository,
        origin,
        acknowledgement="unrelated",
    )
    for marker in (
        "COPILOT_AGENT_SESSION_ID",
        "COPILOT_CLI",
        "GITHUB_ACTIONS",
        "GITHUB_REPOSITORY",
    ):
        monkeypatch.delenv(marker, raising=False)

    result = StewardAdvanceService(
        inbox=GitCampaignInbox(),
        handoffs=CloudHandoffStore(),
    ).advance(StewardAdvanceRequest(repository, 31))

    assert result.status is StewardAdvanceStatus.CONFLICT
    assert result.code == "state_ref_conflict"
    assert proxy.real_revision(
        "refs/heads/foundry-opt/state/issue-31"
    ) is None
    assert proxy.real_revision(
        "refs/heads/copilot/steward-issue-31"
    ) == base


def test_copilot_markers_without_loopback_keep_conflict_semantics(
    tmp_path: Path,
    monkeypatch,
    copilot_git_proxy,
) -> None:
    repository, origin, base = _repository(tmp_path)
    event = CampaignEvent(
        "github-run-1",
        EventKind.ISSUE_CREATED,
        1,
        NOW,
    )
    assert GitIssueEventInbox(repository).append(31, event) is True
    proxy = copilot_git_proxy.install(
        repository,
        origin,
        acknowledgement="unrelated",
        loopback_origin=False,
    )
    monkeypatch.setenv(
        "COPILOT_AGENT_SESSION_ID",
        "11111111-2222-4333-8444-555555555555",
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo-org/optimizer")
    monkeypatch.delenv("COPILOT_CLI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    result = StewardAdvanceService(
        inbox=GitCampaignInbox(),
        handoffs=CloudHandoffStore(),
    ).advance(StewardAdvanceRequest(repository, 31))

    assert result.status is StewardAdvanceStatus.CONFLICT
    assert result.code == "state_ref_conflict"
    assert proxy.real_revision(
        "refs/heads/foundry-opt/state/issue-31"
    ) is None
    assert proxy.real_revision(
        "refs/heads/copilot/steward-issue-31"
    ) == base


def test_trusted_transport_cas_applies_handoff_once(
    tmp_path: Path,
    monkeypatch,
    copilot_git_proxy,
) -> None:
    repository, origin, base = _repository(tmp_path)
    proxy = copilot_git_proxy.install(
        repository,
        origin,
        acknowledgement="absent",
    )
    monkeypatch.setenv(
        "COPILOT_AGENT_SESSION_ID",
        "11111111-2222-4333-8444-555555555555",
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo-org/optimizer")
    monkeypatch.delenv("COPILOT_CLI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    event = CampaignEvent(
        "github-run-1",
        EventKind.ISSUE_CREATED,
        1,
        NOW,
    )
    assert GitIssueEventInbox(repository).append(31, event) is True
    result = StewardAdvanceService(
        inbox=GitCampaignInbox(),
        handoffs=CloudHandoffStore(),
    ).advance(StewardAdvanceRequest(repository, 31))
    assert result.code == "state_handoff_created"
    head = _git(repository, "rev-parse", "HEAD")
    path = _git(
        repository,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        f"{head}^1",
        head,
    )
    blob = _git(
        repository,
        "ls-tree",
        head,
        "--",
        path,
    ).split()[2]
    handoff = StewardStateHandoff.from_bytes(
        subprocess.run(
            ("git", "show", f"{head}:{path}"),
            cwd=repository,
            check=True,
            capture_output=True,
        ).stdout
    )
    edited = CampaignEvent(
        "github-run-2",
        EventKind.ISSUE_EDITED,
        2,
        NOW + timedelta(minutes=1),
    )
    assert GitIssueEventInbox(repository).append(31, edited) is True
    proxy.disable()
    (origin / "hooks" / "post-receive").unlink()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    request = TrustedHandoffRequest(
        repository_root=repository,
        repository="octo-org/optimizer",
        repository_id=123,
        pull_request_number=90,
        author_login="copilot-swe-agent[bot]",
        base_repository="octo-org/optimizer",
        base_ref="main",
        base_revision=base,
        head_repository="octo-org/optimizer",
        head_ref="copilot/steward-issue-31",
        head_revision=head,
        handoff_path=path,
        handoff_blob=blob,
    )
    service = HandoffApplyService()

    applied = service.apply(request)
    duplicate = service.apply(request)

    assert applied.status is HandoffApplyStatus.APPLIED
    assert duplicate.status is HandoffApplyStatus.ALREADY_APPLIED
    assert applied.handoff_id == handoff.handoff_id
    assert duplicate.handoff_id == handoff.handoff_id
    assert applied.snapshot is not None
    assert applied.snapshot.revision == handoff.proposed_revision
    assert [event.event_id for event in applied.snapshot.inbox] == [
        "github-run-1"
    ]
    assert [
        event.event_id
        for event in GitIssueEventInbox(repository).events(31)
    ] == ["github-run-1", "github-run-2"]
    assert (
        _git(
            repository,
            "ls-remote",
            "--heads",
            "origin",
            "refs/heads/foundry-opt/state/issue-31",
        ).split()[0]
        == handoff.proposed_revision
    )


def test_competing_valid_state_handoff_fails_closed(
    tmp_path: Path,
    monkeypatch,
    copilot_git_proxy,
) -> None:
    repository, origin, base = _repository(tmp_path)
    proxy = copilot_git_proxy.install(
        repository,
        origin,
        acknowledgement="unrelated",
    )
    monkeypatch.setenv(
        "COPILOT_AGENT_SESSION_ID",
        "11111111-2222-4333-8444-555555555555",
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo-org/optimizer")
    monkeypatch.delenv("COPILOT_CLI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    created = CampaignEvent(
        "github-run-1",
        EventKind.ISSUE_CREATED,
        1,
        NOW,
    )
    assert GitIssueEventInbox(repository).append(31, created) is True
    handoff_result = StewardAdvanceService(
        inbox=GitCampaignInbox(),
        handoffs=CloudHandoffStore(),
    ).advance(StewardAdvanceRequest(repository, 31))
    assert handoff_result.code == "state_handoff_created"
    head = _git(repository, "rev-parse", "HEAD")
    path = _git(
        repository,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        f"{head}^1",
        head,
    )
    blob = _git(
        repository,
        "ls-tree",
        head,
        "--",
        path,
    ).split()[2]
    handoff = StewardStateHandoff.from_bytes(
        subprocess.run(
            ("git", "show", f"{head}:{path}"),
            cwd=repository,
            check=True,
            capture_output=True,
        ).stdout
    )
    edited = CampaignEvent(
        "github-run-2",
        EventKind.ISSUE_EDITED,
        2,
        NOW + timedelta(minutes=1),
    )
    assert GitIssueEventInbox(repository).append(31, edited) is True
    proxy.disable()
    (origin / "hooks" / "post-receive").unlink()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    competing_state = OptimizationCampaign().advance(
        AdvanceRequest(31, None, (created, edited))
    ).state
    competing = GitStateRef().commit(
        repository,
        issue_number=31,
        expected_revision=None,
        state=competing_state,
        inbox=(created, edited),
    )

    result = HandoffApplyService().apply(
        TrustedHandoffRequest(
            repository_root=repository,
            repository="octo-org/optimizer",
            repository_id=123,
            pull_request_number=90,
            author_login="copilot-swe-agent[bot]",
            base_repository="octo-org/optimizer",
            base_ref="main",
            base_revision=base,
            head_repository="octo-org/optimizer",
            head_ref="copilot/steward-issue-31",
            head_revision=head,
            handoff_path=path,
            handoff_blob=blob,
        )
    )

    assert result.status is HandoffApplyStatus.CONFLICT
    assert result.code == "state_ref_conflict"
    assert GitStateRef().load(repository, 31) == competing
    assert competing.revision != handoff.proposed_revision


@pytest.mark.parametrize(
    "acknowledgement",
    ("absent", "expected", "proposed", "unrelated"),
)
def test_cloud_candidate_designer_persists_result_handoff(
    tmp_path: Path,
    monkeypatch,
    copilot_git_proxy,
    acknowledgement: str,
) -> None:
    repository, origin, base = _repository(tmp_path)
    (origin / "hooks" / "post-receive").unlink()
    created = CampaignEvent(
        "github-run-1",
        EventKind.ISSUE_CREATED,
        1,
        NOW,
    )
    state = OptimizationCampaign().advance(
        AdvanceRequest(31, None, (created,))
    ).state
    planned = OutboxRecord(
        "design-31-1-1-worker",
        "specialist_work_request",
        1,
        state.sequence,
        {
            "allowed_mutations": ["system_instructions"],
            "allowed_paths": ["agent"],
            "base_commit": base,
            "baseline_metrics": {"quality": 0.5},
            "branch": "foundry-opt/issue-31-g1/candidate-1",
            "candidate_feedback": [],
            "candidate_id": "candidate-1",
            "effect_id": "design-31-1-1",
            "goal": (
                "Improve grounded support answers without weakening safety."
            ),
            "issue_number": 31,
            "reason": "candidate_design_pending",
            "restricted_opt_ins": {},
            "slot": 1,
            "spec_sha256": "a" * 64,
            "specialist": "foundry-candidate-designer",
            "target": "support",
            "work_kind": "design_candidate",
        },
    )
    assigned = OutboxRecord(
        "design-31-1-1-worker-succeeded",
        "specialist_work_succeeded",
        1,
        state.sequence,
        {
            "assigned": True,
            "created": True,
            "effect_id": planned.record_id,
            "issue_number": 31,
            "result_id": "designer-assignment-84",
            "specialist": "foundry-candidate-designer",
            "work_kind": "design_candidate",
            "worker_issue_number": 84,
        },
    )
    current = GitStateRef().commit(
        repository,
        issue_number=31,
        expected_revision=None,
        state=state,
        inbox=(created,),
        outbox=(planned, assigned),
    )
    assert GitIssueEventInbox(repository).append(31, created) is True
    proxy = copilot_git_proxy.install(
        repository,
        origin,
        acknowledgement=acknowledgement,
    )
    monkeypatch.setenv(
        "COPILOT_AGENT_SESSION_ID",
        "11111111-2222-4333-8444-555555555555",
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo-org/optimizer")
    monkeypatch.delenv("COPILOT_CLI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    (repository / "agent" / "instructions.md").write_text(
        "candidate\n",
        encoding="utf-8",
    )
    result_file = (
        repository
        / ".foundry-optimizer"
        / "design-results"
        / "design-31-1-1.json"
    )
    result_file.parent.mkdir(parents=True)
    result_file.write_text(
        (
            '{"base_commit":"'
            + base
            + '","candidate_id":"candidate-1","complexity":"small",'
            '"effect_id":"design-31-1-1","generation":1,'
            '"idea_id":"idea-1","issue_number":31,'
            '"lessons":["The baseline omits the escalation rule."],'
            '"motivation":"Clarify the escalation rule.",'
            '"mutation_class":"system_instructions",'
            '"parent_idea_ids":[],"required_opt_ins":[],'
            '"result_id":"designer-result-1","slot":1,'
            '"spec_sha256":"'
            + "a" * 64
            + '"}'
        ),
        encoding="utf-8",
    )

    submitted = CandidateDesignSubmissionService(
        ledger=GitStateRef(),
        repository=_ProductionCandidateDesignRepository(
            SubprocessCommandRunner()
        ),
        handoffs=CloudHandoffStore(),
    ).submit(
        CandidateDesignSubmissionRequest(
            repository,
            31,
            "design-31-1-1",
            84,
            result_file.resolve(),
        )
    )

    assert submitted.status is CandidateDesignSubmissionStatus.WAITING
    assert submitted.code == "candidate_design_handoff_created"
    assert submitted.snapshot.revision == current.revision
    assert _git(repository, "status", "--porcelain") == ""
    assert (
        repository / "agent" / "instructions.md"
    ).read_text(encoding="utf-8") == "baseline\n"
    assert result_file.exists() is False
    assert proxy.real_revision(
        "refs/heads/foundry-opt/design/issue-31/design-31-1-1"
    ) is None
    head = _git(repository, "rev-parse", "HEAD")
    path = _git(
        repository,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        f"{head}^1",
        head,
    )
    handoff = CandidateDesignHandoff.from_bytes(
        subprocess.run(
            ("git", "show", f"{head}:{path}"),
            cwd=repository,
            check=True,
            capture_output=True,
        ).stdout
    )

    assert handoff.issue_number == 31
    assert handoff.generation == 1
    assert handoff.expected_prior_revision == current.revision
    assert handoff.effect_id == "design-31-1-1"
    assert handoff.worker_issue_number == 84
    assert handoff.proposed_ref == (
        "refs/heads/foundry-opt/design/issue-31/design-31-1-1"
    )
    assert handoff.changed_paths == ("agent/instructions.md",)
    assert handoff.result.result_id == "designer-result-1"
    with pytest.raises(ValueError, match="privacy"):
        replace(
            handoff,
            result=replace(
                handoff.result,
                motivation="raw trace row copied verbatim",
            ),
        )
    assert _git(
        repository,
        "rev-parse",
        f"{head}^2",
    ) == handoff.proposed_revision
    blob = _git(
        repository,
        "ls-tree",
        head,
        "--",
        path,
    ).split()[2]
    proxy.disable()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    _git(
        repository,
        "push",
        "origin",
        f"{handoff.proposed_revision}:{handoff.proposed_ref}",
    )
    request = TrustedHandoffRequest(
        repository_root=repository,
        repository="octo-org/optimizer",
        repository_id=123,
        pull_request_number=91,
        author_login="copilot-swe-agent[bot]",
        base_repository="octo-org/optimizer",
        base_ref="main",
        base_revision=base,
        head_repository="octo-org/optimizer",
        head_ref="copilot/steward-issue-31",
        head_revision=head,
        handoff_path=path,
        handoff_blob=blob,
    )

    applied = HandoffApplyService().apply(request)
    duplicate = HandoffApplyService().apply(request)

    assert applied.status is HandoffApplyStatus.APPLIED
    assert duplicate.status is HandoffApplyStatus.ALREADY_APPLIED
    assert (
        _git(
            repository,
            "ls-remote",
            "--heads",
            "origin",
            handoff.proposed_ref,
        ).split()[0]
        == handoff.proposed_revision
    )
    updated = GitStateRef().load(repository, 31)
    assert updated is not None
    submitted_record = next(
        record
        for record in updated.outbox
        if record.record_id == "design-31-1-1-submitted"
    )
    assert submitted_record.payload["result_id"] == "designer-result-1"
    assert submitted_record.payload["head_commit"] == (
        handoff.proposed_revision
    )
