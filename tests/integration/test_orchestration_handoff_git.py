from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

import pytest

import foundry_opt.orchestration.handoff as handoff_module
from foundry_opt.adapters.commands import SubprocessCommandRunner
from foundry_opt.orchestration import (
    AdvanceRequest,
    CampaignEvent,
    EventKind,
    GitStateRef,
    OptimizationCampaign,
    OutboxRecord,
    StateRefPushUnacknowledgedError,
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
    HandoffFinalizer,
    StewardStateHandoff,
    TrustedHandoffContext,
    TrustedHandoffRequest,
    discover_trusted_handoff_requests,
)
from foundry_opt.orchestration.issue_intake import GitIssueEventInbox
from foundry_opt.orchestration.git_state import (
    is_verified_copilot_git_proxy,
)
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
LIVE_COPILOT_ENVIRONMENT = {
    "FOUNDRY_OPT_COPILOT_GIT_PROXY": "1",
    "GITHUB_ACTIONS": "true",
    "GITHUB_REPOSITORY": "microsoft-foundry/luffy-test-agents-repo",
    "COPILOT_CLI": "1",
    "COPILOT_AGENT_SOURCE_ENVIRONMENT": "production",
    "COPILOT_AGENT_START_TIME_SEC": "1785872107",
    "COPILOT_AGENT_TIMEOUT_MIN": "59",
    "COPILOT_AGENT_SESSION_ID": (
        "11111111-2222-4333-8444-555555555555"
    ),
    "GITHUB_AGENT_BRANCH_NAME": "copilot/steward-issue-31",
    "GITHUB_AGENT_ACTOR": "copilot-swe-agent[bot]",
}


def _set_live_copilot_environment(monkeypatch) -> None:
    for name in (
        "COPILOT_CLI",
        "GITHUB_COPILOT_API_TOKEN",
        "GITHUB_COPILOT_ACTION_DOWNLOAD_URL",
        "GITHUB_COPILOT_LOG_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in LIVE_COPILOT_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)


def _set_normal_github_actions_environment(monkeypatch) -> None:
    for name in (
        "COPILOT_AGENT_SESSION_ID",
        "COPILOT_CLI",
        "GITHUB_COPILOT_ACTION_DOWNLOAD_URL",
        "GITHUB_COPILOT_LOG_ID",
        *LIVE_COPILOT_ENVIRONMENT,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo-org/optimizer")
    monkeypatch.setenv("FOUNDRY_OPT_COPILOT_GIT_PROXY", "1")


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
    _set_live_copilot_environment(monkeypatch)
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
    assert b"live-fixture-api-token" not in handoff.content
    assert LIVE_COPILOT_ENVIRONMENT[
        "COPILOT_AGENT_SESSION_ID"
    ].encode() not in handoff.content

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


def test_verified_cloud_proxy_routes_state_without_private_push(
    tmp_path: Path,
    monkeypatch,
    copilot_git_proxy,
) -> None:
    repository, origin, _ = _repository(tmp_path)
    proxy = copilot_git_proxy.install(
        repository,
        origin,
        acknowledgement="unrelated",
    )
    push_target = tmp_path / "push-target.git"
    _git(tmp_path, "init", "--bare", str(push_target))
    _git(
        repository,
        "config",
        "remote.origin.pushurl",
        str(push_target),
    )
    _set_live_copilot_environment(monkeypatch)
    event = CampaignEvent(
        "github-run-1",
        EventKind.ISSUE_CREATED,
        1,
        NOW,
    )
    state = OptimizationCampaign().advance(
        AdvanceRequest(31, None, (event,))
    ).state

    with pytest.raises(StateRefPushUnacknowledgedError) as raised:
        GitStateRef().commit(
            repository,
            issue_number=31,
            expected_revision=None,
            state=state,
            inbox=(event,),
        )

    assert raised.value.proposal is not None
    assert raised.value.proposal.event_ids == ("github-run-1",)
    assert proxy.real_revision(
        "refs/heads/foundry-opt/state/issue-31"
    ) is None
    assert (
        _git(
            repository,
            "ls-remote",
            "--heads",
            str(push_target),
            "refs/heads/foundry-opt/state/issue-31",
        )
        == ""
    )


def test_copilot_proxy_rejects_spoofed_marker_subsets(
    tmp_path: Path,
    monkeypatch,
    copilot_git_proxy,
) -> None:
    repository, origin, _ = _repository(tmp_path)
    copilot_git_proxy.install(
        repository,
        origin,
        acknowledgement="unrelated",
    )
    _set_live_copilot_environment(monkeypatch)
    assert is_verified_copilot_git_proxy(repository) is True

    for marker in (
        "FOUNDRY_OPT_COPILOT_GIT_PROXY",
        "GITHUB_ACTIONS",
        "GITHUB_REPOSITORY",
    ):
        monkeypatch.delenv(marker)
        assert is_verified_copilot_git_proxy(repository) is False, marker
        monkeypatch.setenv(marker, LIVE_COPILOT_ENVIRONMENT[marker])

    for value in ("", "0", "01", "true", " 1 "):
        monkeypatch.setenv("FOUNDRY_OPT_COPILOT_GIT_PROXY", value)
        assert is_verified_copilot_git_proxy(repository) is False, value


def test_local_copilot_cli_without_trusted_proxy_context_is_not_verified(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository, _, _ = _repository(tmp_path)
    for name in LIVE_COPILOT_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COPILOT_CLI", "1")

    assert is_verified_copilot_git_proxy(repository) is False


def test_copilot_proxy_accepts_live_child_without_api_token(
    tmp_path: Path,
    monkeypatch,
    copilot_git_proxy,
) -> None:
    repository, origin, _ = _repository(tmp_path)
    copilot_git_proxy.install(
        repository,
        origin,
        acknowledgement="unrelated",
    )
    _set_live_copilot_environment(monkeypatch)

    assert "GITHUB_COPILOT_API_TOKEN" not in os.environ
    assert is_verified_copilot_git_proxy(repository) is True


def test_copilot_proxy_rejects_malformed_live_markers(
    tmp_path: Path,
    monkeypatch,
    copilot_git_proxy,
) -> None:
    repository, origin, _ = _repository(tmp_path)
    copilot_git_proxy.install(
        repository,
        origin,
        acknowledgement="unrelated",
    )
    malformed = (
        ("FOUNDRY_OPT_COPILOT_GIT_PROXY", ""),
        ("FOUNDRY_OPT_COPILOT_GIT_PROXY", "0"),
        ("FOUNDRY_OPT_COPILOT_GIT_PROXY", "true"),
        ("GITHUB_ACTIONS", "TRUE"),
        ("GITHUB_ACTIONS", "false"),
        ("GITHUB_REPOSITORY", "octo-org/optimizer/extra"),
        ("GITHUB_REPOSITORY", "octo org/optimizer"),
        ("COPILOT_AGENT_SOURCE_ENVIRONMENT", "Production"),
        ("COPILOT_AGENT_SOURCE_ENVIRONMENT", "staging"),
        ("COPILOT_AGENT_START_TIME_SEC", "0"),
        ("COPILOT_AGENT_START_TIME_SEC", "1785872107.0"),
        ("COPILOT_AGENT_START_TIME_SEC", "4102444800"),
        ("COPILOT_AGENT_TIMEOUT_MIN", "0"),
        ("COPILOT_AGENT_TIMEOUT_MIN", "60.0"),
        ("COPILOT_AGENT_TIMEOUT_MIN", "1441"),
        ("COPILOT_AGENT_SESSION_ID", ""),
        ("COPILOT_AGENT_SESSION_ID", "   "),
        ("COPILOT_AGENT_SESSION_ID", "short"),
        ("COPILOT_AGENT_SESSION_ID", "session id"),
        ("COPILOT_AGENT_SESSION_ID", "session/id"),
        ("COPILOT_AGENT_SESSION_ID", "session-id\nspoof"),
        ("COPILOT_AGENT_SESSION_ID", "x" * 129),
    )

    for name, value in malformed:
        _set_live_copilot_environment(monkeypatch)
        monkeypatch.setenv(name, value)
        assert is_verified_copilot_git_proxy(repository) is False, (
            name,
            value,
        )


def test_copilot_proxy_does_not_require_absent_runtime_markers(
    tmp_path: Path,
    monkeypatch,
    copilot_git_proxy,
) -> None:
    repository, origin, _ = _repository(tmp_path)
    copilot_git_proxy.install(
        repository,
        origin,
        acknowledgement="unrelated",
    )
    _set_live_copilot_environment(monkeypatch)
    for name in (
        "COPILOT_AGENT_SOURCE_ENVIRONMENT",
        "COPILOT_AGENT_START_TIME_SEC",
        "COPILOT_AGENT_TIMEOUT_MIN",
        "COPILOT_AGENT_SESSION_ID",
        "GITHUB_COPILOT_API_TOKEN",
        "GITHUB_COPILOT_ACTION_DOWNLOAD_URL",
        "GITHUB_COPILOT_LOG_ID",
    ):
        monkeypatch.delenv(name, raising=False)

    assert is_verified_copilot_git_proxy(repository) is True


def test_copilot_proxy_requires_exact_loopback_repository_path(
    tmp_path: Path,
    monkeypatch,
    copilot_git_proxy,
) -> None:
    repository, origin, _ = _repository(tmp_path)
    copilot_git_proxy.install(
        repository,
        origin,
        acknowledgement="unrelated",
    )
    _set_live_copilot_environment(monkeypatch)
    remote_url = _git(
        repository,
        "config",
        "--get",
        "remote.origin.url",
    )
    parsed_remote = urlsplit(remote_url)
    assert parsed_remote.scheme == "http"
    assert parsed_remote.hostname == "127.0.0.1"
    assert parsed_remote.port is not None
    assert parsed_remote.path == (
        "/microsoft-foundry/luffy-test-agents-repo"
    )
    valid_urls = (
        remote_url,
        "https://127.0.0.1:26831/"
        "microsoft-foundry/luffy-test-agents-repo",
        "http://[::1]:26831/"
        "microsoft-foundry/luffy-test-agents-repo",
    )
    for valid_url in valid_urls:
        _git(repository, "remote", "set-url", "origin", valid_url)
        assert is_verified_copilot_git_proxy(repository) is True, valid_url

    monkeypatch.setenv("GITHUB_REPOSITORY", "microsoft-foundry/other")
    assert is_verified_copilot_git_proxy(repository) is False
    monkeypatch.setenv(
        "GITHUB_REPOSITORY",
        "microsoft-foundry/luffy-test-agents-repo",
    )

    invalid_urls = (
        f"{remote_url}/",
        re.sub(r":\d+/", "/", remote_url, count=1),
        re.sub(r":\d+/", ":0/", remote_url, count=1),
        remote_url.replace(
            "microsoft-foundry/luffy-test-agents-repo",
            "microsoft-foundry/other",
        ),
        f"{remote_url}?transport=proxy",
        f"{remote_url}#transport=proxy",
        remote_url.replace("127.0.0.1", "user@127.0.0.1"),
        remote_url.replace("127.0.0.1", "github.com"),
    )
    for invalid_url in invalid_urls:
        _git(repository, "remote", "set-url", "origin", invalid_url)
        assert is_verified_copilot_git_proxy(repository) is False, (
            invalid_url
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
    _set_live_copilot_environment(monkeypatch)
    assert os.environ["FOUNDRY_OPT_COPILOT_GIT_PROXY"] == "1"
    assert _git(repository, "symbolic-ref", "--short", "HEAD").startswith(
        "copilot/"
    )
    assert is_verified_copilot_git_proxy(repository) is True

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
    _set_normal_github_actions_environment(monkeypatch)
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


@pytest.mark.parametrize(
    "redirect",
    ("pushurl", "pushInsteadOf", "insteadOf"),
)
def test_handoff_transport_ignores_untrusted_push_redirection(
    tmp_path: Path,
    monkeypatch,
    copilot_git_proxy,
    redirect: str,
) -> None:
    repository, origin, _ = _repository(tmp_path)
    event = CampaignEvent(
        "github-run-1",
        EventKind.ISSUE_CREATED,
        1,
        NOW,
    )
    assert GitIssueEventInbox(repository).append(31, event) is True
    (origin / "hooks" / "post-receive").unlink()
    copilot_git_proxy.install(
        repository,
        origin,
        acknowledgement="unrelated",
    )
    remote_url = _git(
        repository,
        "config",
        "--get",
        "remote.origin.url",
    )
    push_target = tmp_path / f"{redirect}.git"
    _git(tmp_path, "init", "--bare", str(push_target))
    if redirect == "pushurl":
        _git(
            repository,
            "config",
            "remote.origin.pushurl",
            str(push_target),
        )
    elif redirect == "pushInsteadOf":
        _git(
            repository,
            "config",
            f"url.{push_target.resolve().as_uri()}.pushInsteadOf",
            remote_url,
        )
    else:
        _git(
            repository,
            "config",
            f"url.{push_target.resolve().as_uri()}.insteadOf",
            remote_url,
        )
    _set_live_copilot_environment(monkeypatch)

    result = StewardAdvanceService(
        inbox=GitCampaignInbox(),
        handoffs=CloudHandoffStore(),
    ).advance(StewardAdvanceRequest(repository, 31))

    assert result.code == "state_handoff_created"
    handoff_commit = _git(repository, "rev-parse", "HEAD")
    proposed_revision = _git(
        repository,
        "rev-parse",
        f"{handoff_commit}^2",
    )
    assert subprocess.run(
        (
            "git",
            f"--git-dir={push_target}",
            "cat-file",
            "-e",
            f"{proposed_revision}^{{commit}}",
        ),
        check=False,
        capture_output=True,
    ).returncode != 0
    assert (
        _git(
            repository,
            "ls-remote",
            "--heads",
            str(push_target),
            "refs/heads/copilot/steward-issue-31",
        )
        == ""
    )


def test_live_copilot_cli_runtime_enables_cloud_handoff(
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
    _set_live_copilot_environment(monkeypatch)
    event = CampaignEvent(
        "github-run-1",
        EventKind.ISSUE_CREATED,
        1,
        NOW,
    )
    assert GitIssueEventInbox(repository).append(31, event) is True
    assert os.environ["COPILOT_CLI"] == "1"
    assert is_verified_copilot_git_proxy(repository) is True

    result = StewardAdvanceService(
        inbox=GitCampaignInbox(),
        handoffs=CloudHandoffStore(),
    ).advance(StewardAdvanceRequest(repository, 31))

    assert result.status is StewardAdvanceStatus.WAITING
    assert result.disposition == "delegate"
    assert result.code == "state_handoff_created"
    assert _git(repository, "rev-parse", "HEAD") != base
    assert proxy.real_revision(
        "refs/heads/copilot/steward-issue-31"
    ) == _git(repository, "rev-parse", "HEAD")
    assert _git(
        repository,
        "ls-tree",
        "-r",
        "--name-only",
        "HEAD",
        "--",
        ".foundry-optimizer/handoffs",
    )


@pytest.mark.parametrize("marker", (None, "", "0", "true", "01", " 1 "))
def test_missing_or_wrong_setup_marker_keeps_conflict_semantics(
    tmp_path: Path,
    monkeypatch,
    copilot_git_proxy,
    marker: str | None,
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
    _set_live_copilot_environment(monkeypatch)
    if marker is None:
        monkeypatch.delenv(
            "FOUNDRY_OPT_COPILOT_GIT_PROXY",
            raising=False,
        )
    else:
        monkeypatch.setenv("FOUNDRY_OPT_COPILOT_GIT_PROXY", marker)

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


@pytest.mark.parametrize("redirect", ("pushurl", "pushInsteadOf"))
def test_wrong_marker_cannot_redirect_private_state_objects(
    tmp_path: Path,
    monkeypatch,
    copilot_git_proxy,
    redirect: str,
) -> None:
    repository, origin, base = _repository(tmp_path)
    event = CampaignEvent(
        "github-run-1",
        EventKind.ISSUE_CREATED,
        1,
        NOW,
    )
    assert GitIssueEventInbox(repository).append(31, event) is True
    copilot_git_proxy.install(
        repository,
        origin,
        acknowledgement="unrelated",
    )
    remote_url = _git(
        repository,
        "config",
        "--get",
        "remote.origin.url",
    )
    push_target = tmp_path / f"wrong-marker-{redirect}.git"
    _git(tmp_path, "init", "--bare", str(push_target))
    if redirect == "pushurl":
        _git(
            repository,
            "config",
            "remote.origin.pushurl",
            str(push_target),
        )
    else:
        _git(
            repository,
            "config",
            f"url.{push_target.resolve().as_uri()}.pushInsteadOf",
            remote_url,
        )
    _set_live_copilot_environment(monkeypatch)
    monkeypatch.setenv("FOUNDRY_OPT_COPILOT_GIT_PROXY", "0")

    result = StewardAdvanceService(
        inbox=GitCampaignInbox(),
        handoffs=CloudHandoffStore(),
    ).advance(StewardAdvanceRequest(repository, 31))

    assert result.status is StewardAdvanceStatus.CONFLICT
    assert result.code == "state_ref_conflict"
    assert _git(repository, "rev-parse", "HEAD") == base
    assert (
        _git(
            repository,
            "ls-remote",
            "--heads",
            str(push_target),
            "refs/heads/foundry-opt/state/issue-31",
        )
        == ""
    )


def test_wrong_marker_ignores_untrusted_pack_helpers(
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
    (origin / "hooks" / "post-receive").unlink()
    copilot_git_proxy.install(
        repository,
        origin,
        acknowledgement="unrelated",
        loopback_origin=False,
    )
    _set_live_copilot_environment(monkeypatch)
    monkeypatch.setenv("FOUNDRY_OPT_COPILOT_GIT_PROXY", "0")

    state = OptimizationCampaign().advance(
        AdvanceRequest(31, None, (event,))
    ).state
    snapshot = GitStateRef().commit(
        repository,
        issue_number=31,
        expected_revision=None,
        state=state,
        inbox=(event,),
    )

    assert snapshot.state == state
    assert _git(repository, "rev-parse", "HEAD") == base
    assert (
        _git(
            repository,
            "ls-remote",
            "--heads",
            str(origin),
            "refs/heads/foundry-opt/state/issue-31",
        )
        != ""
    )


def test_non_copilot_branch_with_trusted_setup_marker_keeps_conflict_semantics(
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
    _git(repository, "checkout", "main")
    proxy = copilot_git_proxy.install(
        repository,
        origin,
        acknowledgement="unrelated",
    )
    _set_live_copilot_environment(monkeypatch)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/copilot/spoofed")

    assert is_verified_copilot_git_proxy(repository) is False
    result = StewardAdvanceService(
        inbox=GitCampaignInbox(),
        handoffs=CloudHandoffStore(),
    ).advance(StewardAdvanceRequest(repository, 31))

    assert result.status is StewardAdvanceStatus.CONFLICT
    assert result.code == "state_ref_conflict"
    assert _git(repository, "rev-parse", "HEAD") == base
    assert proxy.real_revision(
        "refs/heads/foundry-opt/state/issue-31"
    ) is None
    assert proxy.real_revision("refs/heads/main") == base


def test_detached_head_rejects_invalid_wrapper_branch(
    tmp_path: Path,
    monkeypatch,
    copilot_git_proxy,
) -> None:
    repository, origin, _ = _repository(tmp_path)
    copilot_git_proxy.install(
        repository,
        origin,
        acknowledgement="unrelated",
    )
    _set_live_copilot_environment(monkeypatch)
    head = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "--detach", head)

    assert is_verified_copilot_git_proxy(repository) is True
    for branch in (
        "",
        "main",
        "refs/heads/copilot/session",
        "copilot/../main",
        "copilot/session.lock",
        "copilot/session//nested",
        "copilot/session@{1}",
        f"copilot/{'x' * 201}",
    ):
        monkeypatch.setenv("GITHUB_AGENT_BRANCH_NAME", branch)
        assert is_verified_copilot_git_proxy(repository) is False, branch


def test_detached_copilot_branch_requires_wrapper_identity_markers(
    tmp_path: Path,
    monkeypatch,
    copilot_git_proxy,
) -> None:
    repository, origin, _ = _repository(tmp_path)
    copilot_git_proxy.install(
        repository,
        origin,
        acknowledgement="unrelated",
    )
    _set_live_copilot_environment(monkeypatch)
    head = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "--detach", head)

    assert is_verified_copilot_git_proxy(repository) is True
    for marker in (
        "GITHUB_AGENT_BRANCH_NAME",
        "GITHUB_AGENT_ACTOR",
        "COPILOT_AGENT_SOURCE_ENVIRONMENT",
        "COPILOT_AGENT_START_TIME_SEC",
        "COPILOT_AGENT_TIMEOUT_MIN",
        "COPILOT_AGENT_SESSION_ID",
    ):
        _set_live_copilot_environment(monkeypatch)
        monkeypatch.delenv(marker)
        assert is_verified_copilot_git_proxy(repository) is False, marker
    for actor in (
        "",
        "copilot/agent",
        "copilot agent",
        "copilot-agent\nspoof",
        "x" * 106,
    ):
        _set_live_copilot_environment(monkeypatch)
        monkeypatch.setenv("GITHUB_AGENT_ACTOR", actor)
        assert is_verified_copilot_git_proxy(repository) is False, actor


def test_attached_copilot_branch_requires_matching_wrapper_branch(
    tmp_path: Path,
    monkeypatch,
    copilot_git_proxy,
) -> None:
    repository, origin, _ = _repository(tmp_path)
    copilot_git_proxy.install(
        repository,
        origin,
        acknowledgement="unrelated",
    )
    _set_live_copilot_environment(monkeypatch)

    assert is_verified_copilot_git_proxy(repository) is True

    monkeypatch.setenv(
        "GITHUB_AGENT_BRANCH_NAME",
        "copilot/other-session",
    )
    assert is_verified_copilot_git_proxy(repository) is False

    monkeypatch.setenv(
        "GITHUB_AGENT_BRANCH_NAME",
        "copilot/steward-issue-31",
    )
    _git(repository, "checkout", "main")
    assert is_verified_copilot_git_proxy(repository) is False


def test_detached_copilot_branch_must_match_proxy_tip(
    tmp_path: Path,
    monkeypatch,
    copilot_git_proxy,
) -> None:
    repository, origin, _ = _repository(tmp_path)
    copilot_git_proxy.install(
        repository,
        origin,
        acknowledgement="unrelated",
    )
    _set_live_copilot_environment(monkeypatch)
    (repository / "README.md").write_text(
        "unpublished detached head\n",
        encoding="utf-8",
    )
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "unpublished detached head")
    head = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "--detach", head)

    assert is_verified_copilot_git_proxy(repository) is False


def test_live_detached_copilot_branch_creates_state_handoff(
    tmp_path: Path,
    monkeypatch,
    copilot_git_proxy,
) -> None:
    repository, origin, _ = _repository(tmp_path)
    branch = (
        "copilot/optimize-agent-instructions-"
        "11111111-2222-4333-8444-555555555555"
    )
    base = _git(repository, "rev-parse", "HEAD")
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
    _set_live_copilot_environment(monkeypatch)
    monkeypatch.setenv("GITHUB_AGENT_BRANCH_NAME", branch)
    _git(repository, "checkout", "--detach", base)

    result = StewardAdvanceService(
        inbox=GitCampaignInbox(),
        handoffs=CloudHandoffStore(),
    ).advance(StewardAdvanceRequest(repository, 31))

    assert result.status is StewardAdvanceStatus.WAITING
    assert result.code == "state_handoff_created"
    head = _git(repository, "rev-parse", "HEAD")
    assert head != base
    assert proxy.real_revision(f"refs/heads/{branch}") == head
    path = _git(
        repository,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        f"{head}^1",
        head,
    )
    assert path.startswith(
        ".foundry-optimizer/handoffs/steward/issue-31/"
    )
    assert (repository / path).is_file()
    assert (
        subprocess.run(
            ("git", "symbolic-ref", "--quiet", "HEAD"),
            cwd=repository,
            check=False,
            capture_output=True,
        ).returncode
        != 0
    )


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
    _set_live_copilot_environment(monkeypatch)

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


def test_ordinary_actions_setup_marker_keeps_normal_non_handoff_semantics(
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
    _git(repository, "checkout", "main")
    proxy = copilot_git_proxy.install(
        repository,
        origin,
        acknowledgement="unrelated",
        loopback_origin=False,
    )
    github_origin = "https://github.com/octo-org/optimizer"
    _git(repository, "remote", "set-url", "origin", github_origin)
    _git(
        repository,
        "config",
        f"url.{origin.resolve().as_uri()}.insteadOf",
        github_origin,
    )
    _set_normal_github_actions_environment(monkeypatch)

    assert (
        _git(repository, "config", "--get", "remote.origin.url")
        == github_origin
    )
    assert _git(repository, "symbolic-ref", "--short", "HEAD") == "main"
    assert os.environ["FOUNDRY_OPT_COPILOT_GIT_PROXY"] == "1"
    assert is_verified_copilot_git_proxy(repository) is False
    result = StewardAdvanceService(
        inbox=GitCampaignInbox(),
        handoffs=CloudHandoffStore(),
    ).advance(StewardAdvanceRequest(repository, 31))

    assert result.status is StewardAdvanceStatus.FAILED
    assert result.code == "state_ref_unavailable"
    assert _git(repository, "rev-parse", "HEAD") == base
    assert proxy.real_revision(
        "refs/heads/foundry-opt/state/issue-31"
    ) is None
    assert proxy.real_revision("refs/heads/main") == base


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
    _set_live_copilot_environment(monkeypatch)
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
    _git(repository, "checkout", "main")
    (repository / "rollout.txt").write_text(
        "transport-only rollout\n",
        encoding="utf-8",
    )
    _git(repository, "add", "rollout.txt")
    _git(repository, "commit", "-m", "roll out fallback transport")
    rolled_out_base = _git(repository, "rev-parse", "HEAD")
    _git(repository, "push", "origin", "main")
    proxy.disable()
    (origin / "hooks" / "post-receive").unlink()
    _set_normal_github_actions_environment(monkeypatch)
    current_product_commit = "f" * 40
    monkeypatch.setattr(
        handoff_module,
        "_product_identity",
        lambda: (handoff.product_version, current_product_commit),
    )
    monkeypatch.setenv(
        "TRUSTED_HANDOFF_PRODUCT_COMMITS",
        handoff.product_commit,
    )

    class OpenPullRequestGateway:
        def __init__(self) -> None:
            self.closed: list[int] = []
            self.deleted: list[tuple[str, str]] = []

        def list_open_pull_requests(self):
            return [self.get_pull_request(90), self.get_pull_request(90)]

        def get_pull_request(self, number):
            assert number == 90
            return {
                "base": {
                    "ref": "main",
                    "repo": {"full_name": "octo-org/optimizer"},
                    "sha": rolled_out_base,
                },
                "created_at": "2026-08-05T20:54:42Z",
                "draft": True,
                "head": {
                    "ref": "copilot/steward-issue-31",
                    "repo": {"full_name": "octo-org/optimizer"},
                    "sha": head,
                },
                "merged": False,
                "number": 90,
                "state": "open",
                "statusCheckRollup": [{"conclusion": "action_required"}],
                "user": {
                    "html_url": (
                        "https://github.com/apps/copilot-swe-agent"
                    ),
                    "id": 198982749,
                    "login": "Copilot",
                    "type": "Bot",
                },
            }

        def get_pull_request_files(self, number):
            assert number == 90
            return [
                {
                    "filename": path,
                    "sha": blob,
                    "status": "added",
                }
            ]

        def fetch_revision(self, revision):
            assert revision in {rolled_out_base, head}
            return revision

        def head_has_copilot_session_attestation(
            self,
            number,
            branch,
            revision,
        ):
            assert number == 90
            assert branch == "copilot/steward-issue-31"
            assert revision == head
            return True

        def close_internal_pull_request(self, number, **kwargs):
            self.closed.append(number)

        def delete_branch_if_head(self, branch, revision):
            self.deleted.append((branch, revision))
            return True

    gateway = OpenPullRequestGateway()
    request, = discover_trusted_handoff_requests(
        TrustedHandoffContext(
            "schedule",
            "octo-org/optimizer",
            123,
            "main",
        ),
        repository,
        gateway,
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

    class Assignments:
        def release(self, issue_number):
            assert issue_number == 31

        def assign(self, issue_number, idempotency_key):
            raise AssertionError("terminal state must not be reassigned")

    class Effects:
        def reconcile(self, issue_number):
            assert issue_number == 31

    HandoffFinalizer(
        gateway=gateway,
        assignments=Assignments(),
        effects=Effects(),
        should_reassign=lambda issue_number: False,
    ).finalize(request, applied)

    assert gateway.deleted == [
        ("copilot/steward-issue-31", head)
    ]
    assert gateway.closed == [90]


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
    _set_live_copilot_environment(monkeypatch)
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
    _set_normal_github_actions_environment(monkeypatch)
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
    _set_live_copilot_environment(monkeypatch)
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
    _set_normal_github_actions_environment(monkeypatch)
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
