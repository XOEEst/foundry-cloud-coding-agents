from collections.abc import Sequence
import os
from pathlib import Path

import pytest

import foundry_opt.onboarding.repository as repository_module
from foundry_opt.adapters.commands import CommandExitError
from foundry_opt.onboarding import (
    ChangeStatus,
    DraftPullRequest,
    OnboardingChange,
    OnboardingRequest,
    RepositoryDiscovery,
)
from foundry_opt.onboarding.repository import (
    AtomicWriteUnsupportedError,
    ChangeSetConflictError,
    ChangeSetWriteError,
    GhOnboardingPublisher,
    OnboardingPublishError,
    SafeChangeSetWriter,
    UnsafeChangePathError,
)
from foundry_opt.onboarding.production import (
    build_production_onboarding_dependencies,
)
from foundry_opt.preflight.interfaces import CommandResult


LINUX_ATOMIC_WRITES = (
    os.name == "posix" and repository_module._secure_posix_writes_available()
)


def test_safe_writer_rejects_symlinked_parent_before_writing(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    github = tmp_path / ".github"
    try:
        github.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks unavailable: {error}")

    with pytest.raises(UnsafeChangePathError, match="symlinked parent"):
        SafeChangeSetWriter().prevalidate(
            tmp_path,
            {Path(".github/foundry-optimizer.yaml"): "schema_version: '1'\n"},
        )

    assert not (outside / "foundry-optimizer.yaml").exists()


def test_safe_writer_rejects_paths_outside_resolved_repository(
    tmp_path: Path,
) -> None:
    with pytest.raises(UnsafeChangePathError, match="repository-relative"):
        SafeChangeSetWriter().prevalidate(
            tmp_path,
            {Path("../outside.yaml"): "unsafe\n"},
        )


@pytest.mark.skipif(
    not LINUX_ATOMIC_WRITES,
    reason="secure atomic writer requires Linux dirfd and renameat2",
)
def test_safe_writer_rolls_back_files_when_any_write_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    writes = 0
    original_write = os.write

    def partial_write(fd: int, content: bytes) -> int:
        nonlocal writes
        writes += 1
        if writes == 2:
            assert not (
                tmp_path / ".github/foundry-optimizer.yaml"
            ).exists()
            original_write(fd, content[:2])
            raise OSError("disk full")
        return original_write(fd, content)

    monkeypatch.setattr(repository_module.os, "write", partial_write)
    writer = SafeChangeSetWriter()
    contents = {
        Path(".github/foundry-optimizer.yaml"): "schema_version: '1'\n",
        Path(".github/workflows/copilot-setup-steps.yml"): "name: Setup\n",
    }

    with pytest.raises(ChangeSetWriteError, match="disk full"):
        writer.write(tmp_path, contents)

    assert not (tmp_path / ".github/foundry-optimizer.yaml").exists()
    assert not (tmp_path / ".github/workflows/copilot-setup-steps.yml").exists()
    assert list(tmp_path.rglob(".foundry-opt-*.tmp")) == []


@pytest.mark.skipif(
    not LINUX_ATOMIC_WRITES,
    reason="secure atomic writer requires Linux dirfd and renameat2",
)
def test_safe_writer_preserves_destination_won_by_another_process(
    tmp_path: Path,
    monkeypatch,
) -> None:
    original_link = os.link
    link_attempts = 0
    winner = tmp_path / ".github/workflows/copilot-setup-steps.yml"

    def racing_link(source, destination, *args, **kwargs):
        nonlocal link_attempts
        link_attempts += 1
        if link_attempts == 2:
            winner.write_text("other process\n", encoding="utf-8")
            raise FileExistsError(str(winner))
        return original_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(repository_module.os, "link", racing_link)
    contents = {
        Path(".github/foundry-optimizer.yaml"): "schema_version: '1'\n",
        Path(".github/workflows/copilot-setup-steps.yml"): "name: Setup\n",
    }

    with pytest.raises(ChangeSetConflictError):
        SafeChangeSetWriter().write(tmp_path, contents)

    assert not (tmp_path / ".github/foundry-optimizer.yaml").exists()
    assert winner.read_text(encoding="utf-8") == "other process\n"
    assert list(tmp_path.rglob(".foundry-opt-*.tmp")) == []


@pytest.mark.skipif(
    not LINUX_ATOMIC_WRITES,
    reason="secure atomic writer requires Linux dirfd and renameat2",
)
def test_safe_writer_installs_only_fully_written_temporary_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    original_link = os.link
    observed: list[bytes] = []

    def observing_link(source, destination, *args, **kwargs):
        source_dir = kwargs.get("src_dir_fd")
        if source_dir is None:
            observed.append(Path(source).read_bytes())
        else:
            descriptor = os.open(source, os.O_RDONLY, dir_fd=source_dir)
            try:
                observed.append(os.read(descriptor, 4096))
            finally:
                os.close(descriptor)
        return original_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(repository_module.os, "link", observing_link)
    content = "schema_version: '1'\n"

    SafeChangeSetWriter().write(
        tmp_path,
        {Path(".github/foundry-optimizer.yaml"): content},
    )

    assert observed == [content.encode("utf-8")]
    assert (
        tmp_path / ".github/foundry-optimizer.yaml"
    ).read_text(encoding="utf-8") == content
    assert list(tmp_path.rglob(".foundry-opt-*.tmp")) == []


@pytest.mark.skipif(
    LINUX_ATOMIC_WRITES,
    reason="platform provides secure Linux atomic primitives",
)
def test_safe_writer_fails_closed_before_writing_on_unsupported_platform(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        AtomicWriteUnsupportedError,
        match="refusing to write",
    ):
        SafeChangeSetWriter().prevalidate(
            tmp_path,
            {Path(".github/foundry-optimizer.yaml"): "schema_version: '1'\n"},
        )

    assert not (tmp_path / ".github").exists()


class FakeCommands:
    def __init__(self) -> None:
        self.invocations: list[tuple[str, ...]] = []

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
    ) -> CommandResult:
        command = tuple(arguments)
        self.invocations.append(command)
        if command == ("git", "branch", "--show-current"):
            return CommandResult(0, "main\n", "")
        if command[0:3] == ("git", "branch", "--list"):
            return CommandResult(0, "", "")
        if command[0:3] == ("git", "ls-remote", "--heads"):
            return CommandResult(0, "", "")
        if command == ("git", "rev-parse", "HEAD"):
            return CommandResult(0, "abc123\n", "")
        if command[0:3] == ("gh", "pr", "create"):
            return CommandResult(
                0,
                "https://github.com/octo-org/agents/pull/42\n",
                "",
            )
        return CommandResult(0, "", "")


def test_github_publisher_owns_branch_commit_push_and_draft_pr(
    tmp_path: Path,
) -> None:
    commands = FakeCommands()
    changes = (
        OnboardingChange(
            path=Path(".github/foundry-optimizer.yaml"),
            content="schema_version: '1'\n",
            status=ChangeStatus.CREATED,
        ),
    )
    request = OnboardingRequest(
        repository_root=tmp_path,
        environment_name="acceptance",
        target_name="support-agent",
        project_endpoint=(
            "https://example.services.ai.azure.com/api/projects/demo"
        ),
        project_resource_id="/subscriptions/sub/projects/demo",
        tenant_id="tenant",
        client_id="client",
        subscription_id="subscription",
        product_install="foundry-cloud-coding-agent==0.1.0",
    )
    discovery = RepositoryDiscovery(
        repository="octo-org/agents",
        repository_id="123",
        default_branch="main",
        current_branch="main",
        authenticated_login="octocat",
        viewer_permission="ADMIN",
        clean=True,
    )

    result = GhOnboardingPublisher(commands).publish(
        request,
        discovery,
        changes,
        DraftPullRequest("Configure onboarding", "Review this change."),
    )

    assert result.url == "https://github.com/octo-org/agents/pull/42"
    assert result.branch == "foundry-opt/onboarding-support-agent"
    assert result.commit_sha == "abc123"
    assert ("git", "switch", "-c", result.branch) in commands.invocations
    assert (
        "git",
        "push",
        "--set-upstream",
        "--force-with-lease=refs/heads/"
        "foundry-opt/onboarding-support-agent:",
        "origin",
        result.branch,
    ) in commands.invocations
    pr_command = next(
        command
        for command in commands.invocations
        if command[0:3] == ("gh", "pr", "create")
    )
    assert "--draft" in pr_command
    assert ("--base", "main") == (
        pr_command[pr_command.index("--base")],
        pr_command[pr_command.index("--base") + 1],
    )


def test_production_onboarding_uses_github_publisher() -> None:
    dependencies = build_production_onboarding_dependencies()

    assert isinstance(dependencies.publisher, GhOnboardingPublisher)


def test_github_publisher_compensates_after_draft_pr_failure(
    tmp_path: Path,
) -> None:
    class PrFailureCommands(FakeCommands):
        def run(self, arguments, *, cwd=None):
            command = tuple(arguments)
            if command[0:3] == ("gh", "pr", "create"):
                self.invocations.append(command)
                raise CommandExitError(
                    command,
                    exit_code=1,
                    stdout="",
                    stderr="API failure",
                )
            return super().run(arguments, cwd=cwd)

    commands = PrFailureCommands()
    request, discovery, changes, draft_pr = _publication_inputs(tmp_path)

    with pytest.raises(OnboardingPublishError) as raised:
        GhOnboardingPublisher(commands).publish(
            request,
            discovery,
            changes,
            draft_pr,
        )

    assert raised.value.phase == "draft_pr"
    assert raised.value.residual_state == ()
    assert (
        "git",
        "push",
        "--force-with-lease=refs/heads/"
        "foundry-opt/onboarding-support-agent:abc123",
        "origin",
        ":refs/heads/foundry-opt/onboarding-support-agent",
    ) in commands.invocations
    assert ("git", "switch", "main") in commands.invocations
    assert (
        "git",
        "branch",
        "-D",
        "foundry-opt/onboarding-support-agent",
    ) in commands.invocations


def test_github_publisher_surfaces_failed_compensation(
    tmp_path: Path,
) -> None:
    class CompensationFailureCommands(FakeCommands):
        def run(self, arguments, *, cwd=None):
            command = tuple(arguments)
            if command[0:3] == ("gh", "pr", "create"):
                self.invocations.append(command)
                raise CommandExitError(
                    command,
                    exit_code=1,
                    stdout="",
                    stderr="API failure",
                )
            if (
                command[0:2] == ("git", "push")
                and command[2].startswith("--force-with-lease=")
            ):
                self.invocations.append(command)
                raise CommandExitError(
                    command,
                    exit_code=1,
                    stdout="",
                    stderr="lease mismatch",
                )
            return super().run(arguments, cwd=cwd)

    request, discovery, changes, draft_pr = _publication_inputs(tmp_path)

    with pytest.raises(OnboardingPublishError) as raised:
        GhOnboardingPublisher(CompensationFailureCommands()).publish(
            request,
            discovery,
            changes,
            draft_pr,
        )

    assert raised.value.phase == "draft_pr"
    assert raised.value.residual_state == (
        "remote branch foundry-opt/onboarding-support-agent may remain at abc123",
    )


def test_github_publisher_cleans_staged_files_after_commit_failure(
    tmp_path: Path,
) -> None:
    class CommitFailureCommands(FakeCommands):
        def run(self, arguments, *, cwd=None):
            command = tuple(arguments)
            if command[0:2] == ("git", "commit"):
                self.invocations.append(command)
                raise CommandExitError(
                    command,
                    exit_code=1,
                    stdout="",
                    stderr="hook rejected commit",
                )
            return super().run(arguments, cwd=cwd)

    commands = CommitFailureCommands()
    request, discovery, changes, draft_pr = _publication_inputs(tmp_path)

    with pytest.raises(OnboardingPublishError) as raised:
        GhOnboardingPublisher(commands).publish(
            request,
            discovery,
            changes,
            draft_pr,
        )

    assert raised.value.phase == "commit"
    assert raised.value.residual_state == ()
    paths = tuple(change.path.as_posix() for change in changes)
    assert ("git", "reset", "--", *paths) in commands.invocations
    assert ("git", "clean", "-f", "--", *paths) in commands.invocations
    assert ("git", "switch", "main") in commands.invocations
    assert (
        "git",
        "branch",
        "-D",
        "foundry-opt/onboarding-support-agent",
    ) in commands.invocations


def test_github_publisher_cleans_partially_staged_files_after_add_failure(
    tmp_path: Path,
) -> None:
    class AddFailureCommands(FakeCommands):
        def run(self, arguments, *, cwd=None):
            command = tuple(arguments)
            if command[0:2] == ("git", "add"):
                self.invocations.append(command)
                raise CommandExitError(
                    command,
                    exit_code=1,
                    stdout="",
                    stderr="index write failed after partial update",
                )
            return super().run(arguments, cwd=cwd)

    commands = AddFailureCommands()
    request, discovery, changes, draft_pr = _publication_inputs(tmp_path)

    with pytest.raises(OnboardingPublishError) as raised:
        GhOnboardingPublisher(commands).publish(
            request,
            discovery,
            changes,
            draft_pr,
        )

    assert raised.value.phase == "stage"
    assert raised.value.residual_state == ()
    paths = tuple(change.path.as_posix() for change in changes)
    assert ("git", "reset", "--", *paths) in commands.invocations
    assert ("git", "clean", "-f", "--", *paths) in commands.invocations


def _publication_inputs(tmp_path: Path):
    changes = (
        OnboardingChange(
            path=Path(".github/foundry-optimizer.yaml"),
            content="schema_version: '1'\n",
            status=ChangeStatus.CREATED,
        ),
    )
    request = OnboardingRequest(
        repository_root=tmp_path,
        environment_name="acceptance",
        target_name="support-agent",
        project_endpoint=(
            "https://example.services.ai.azure.com/api/projects/demo"
        ),
        project_resource_id="/subscriptions/sub/projects/demo",
        tenant_id="tenant",
        client_id="client",
        subscription_id="subscription",
        product_install="foundry-cloud-coding-agent==0.1.0",
    )
    discovery = RepositoryDiscovery(
        repository="octo-org/agents",
        repository_id="123",
        default_branch="main",
        current_branch="main",
        authenticated_login="octocat",
        viewer_permission="ADMIN",
        clean=True,
    )
    return (
        request,
        discovery,
        changes,
        DraftPullRequest("Configure onboarding", "Review this change."),
    )
