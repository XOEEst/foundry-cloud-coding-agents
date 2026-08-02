from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from foundry_opt.adapters.commands import (
    CommandExitError,
    SubprocessCommandRunner,
)
from foundry_opt.onboarding import (
    ChangeStatus,
    DraftPullRequest,
    OnboardingChange,
    OnboardingRequest,
    RepositoryDiscovery,
)
from foundry_opt.onboarding.production import (
    build_production_onboarding_dependencies,
)
from foundry_opt.onboarding.repository import (
    ChangeSetConflictError,
    ChangeSetWriteError,
    GhOnboardingPublisher,
    GitChangeSetWriter,
    OnboardingPublishError,
    UnsafeChangePathError,
    normalize_legacy_generated_content,
)
from foundry_opt.preflight.interfaces import CommandResult


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Onboarding Test")
    _git(repository, "config", "user.email", "onboarding@example.invalid")
    (repository / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "baseline")
    return repository


def _managed_contents(path: Path, content: str) -> dict[Path, str]:
    manifest = {
        "files": {
            path.as_posix(): hashlib.sha256(
                content.encode("utf-8")
            ).hexdigest(),
        },
        "generator": "foundry-opt init",
        "schema_version": 1,
    }
    return {
        path: content,
        Path(".github/foundry-optimizer.generated.json"): (
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        ),
    }


def test_git_change_set_builds_commit_without_materializing_generated_paths(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    base = _git(repository, "rev-parse", "HEAD")
    contents = {
        Path(".github/foundry-optimizer.yaml"): "schema_version: '1'\n",
        Path(".github/workflows/copilot-setup-steps.yml"): "name: Setup\n",
    }
    writer = GitChangeSetWriter(SubprocessCommandRunner())

    planned = writer.prevalidate(repository, contents)
    changes = writer.write(repository, contents)

    assert all(change.status is ChangeStatus.PLANNED for change in planned)
    assert all(change.status is ChangeStatus.CREATED for change in changes)
    assert {change.base_commit for change in changes} == {base}
    commit_sha = changes[0].commit_sha
    assert commit_sha
    assert {change.commit_sha for change in changes} == {commit_sha}
    assert _git(repository, "rev-parse", "HEAD") == base
    assert _git(repository, "status", "--porcelain=v1", "--untracked-files=all") == ""
    assert not (repository / ".github").exists()
    assert (
        _git(
            repository,
            "show",
            f"{commit_sha}:.github/foundry-optimizer.yaml",
        )
        == "schema_version: '1'"
    )
    assert (
        _git(
            repository,
            "show",
            f"{commit_sha}:.github/workflows/copilot-setup-steps.yml",
        )
        == "name: Setup"
    )


def test_git_change_set_updates_only_manifest_owned_generated_files(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    path = Path(".github/workflows/foundry-optimization-control.yml")
    old = _managed_contents(path, "name: Old generated control\n")
    for relative, content in old.items():
        destination = repository / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    _git(repository, "add", ".github")
    _git(repository, "commit", "-m", "generated onboarding v1")
    base = _git(repository, "rev-parse", "HEAD")
    desired = _managed_contents(
        path,
        "name: Trusted transport retry\n",
    )
    writer = GitChangeSetWriter(SubprocessCommandRunner())

    planned = writer.prevalidate(repository, desired)
    changes = writer.write(repository, desired)

    assert {change.status for change in planned} == {
        ChangeStatus.UPDATED
    }
    assert {change.status for change in changes} == {
        ChangeStatus.UPDATED
    }
    commit = changes[0].commit_sha
    assert commit and commit != base
    assert (
        _git(repository, "show", f"{commit}:{path.as_posix()}")
        == "name: Trusted transport retry"
    )
    assert _git(repository, "rev-parse", "HEAD") == base


def test_git_change_set_is_idempotent_when_generated_files_match(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    path = Path(".github/workflows/foundry-optimization-reconcile.yml")
    contents = _managed_contents(path, "name: Trusted transport retry\n")
    for relative, content in contents.items():
        destination = repository / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    _git(repository, "add", ".github")
    _git(repository, "commit", "-m", "generated onboarding")
    base = _git(repository, "rev-parse", "HEAD")
    writer = GitChangeSetWriter(SubprocessCommandRunner())

    planned = writer.prevalidate(repository, contents)
    changes = writer.write(repository, contents)

    assert {change.status for change in planned} == {
        ChangeStatus.UNCHANGED
    }
    assert {change.status for change in changes} == {
        ChangeStatus.UNCHANGED
    }
    assert {change.commit_sha for change in changes} == {base}
    assert _git(repository, "rev-parse", "HEAD") == base


def test_git_change_set_migrates_exact_legacy_generated_content(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    path = Path(".github/workflows/foundry-optimization-control.yml")
    old_content = "name: Foundry optimization control\n"
    destination = repository / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(old_content, encoding="utf-8")
    _git(repository, "add", path.as_posix())
    _git(repository, "commit", "-m", "legacy generated onboarding")
    desired = _managed_contents(
        path,
        "name: Trusted transport retry\n",
    )
    manifest_path = Path(".github/foundry-optimizer.generated.json")
    manifest = json.loads(desired[manifest_path])
    manifest["accepted_previous_sha256"] = {
        path.as_posix(): [
            hashlib.sha256(old_content.encode("utf-8")).hexdigest()
        ]
    }
    desired[manifest_path] = (
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    writer = GitChangeSetWriter(SubprocessCommandRunner())

    changes = writer.write(repository, desired)

    updated = next(change for change in changes if change.path == path)
    assert updated.status is ChangeStatus.UPDATED
    assert updated.commit_sha
    assert (
        _git(
            repository,
            "show",
            f"{updated.commit_sha}:{path.as_posix()}",
        )
        == "name: Trusted transport retry"
    )


def test_git_change_set_migrates_legacy_workflow_across_package_pins(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    path = Path(
        ".github/workflows/foundry-optimization-issue-intake.yml"
    )
    old_content = (
        "name: Foundry optimization issue intake\n"
        "jobs:\n"
        "  bridge:\n"
        "    env:\n"
        '      OPTIMIZER_PACKAGE: "foundry-cloud-coding-agent==0.1.0"\n'
    )
    destination = repository / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(old_content, encoding="utf-8")
    _git(repository, "add", path.as_posix())
    _git(repository, "commit", "-m", "legacy package pin")
    new_content = old_content.replace("==0.1.0", "==0.2.0")
    desired = _managed_contents(path, new_content)
    manifest_path = Path(".github/foundry-optimizer.generated.json")
    manifest = json.loads(desired[manifest_path])
    normalized = old_content.replace(
        'OPTIMIZER_PACKAGE: "foundry-cloud-coding-agent==0.1.0"',
        "OPTIMIZER_PACKAGE: <PINNED_PRODUCT_INSTALL>",
    )
    manifest["accepted_previous_normalized_sha256"] = {
        path.as_posix(): [
            hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        ]
    }
    desired[manifest_path] = (
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )

    changes = GitChangeSetWriter(SubprocessCommandRunner()).write(
        repository,
        desired,
    )

    assert next(change for change in changes if change.path == path).status is (
        ChangeStatus.UPDATED
    )


def test_legacy_pin_normalization_rejects_custom_shell_suffix() -> None:
    path = Path(".github/workflows/foundry-optimization-control.yml")

    assert normalize_legacy_generated_content(
        path,
        "      run: uv tool install 'foundry-cloud-coding-agent==0.1.0' "
        "&& echo custom\n",
    ) is None
    assert normalize_legacy_generated_content(
        path,
        "      run: uv tool install 'private-custom-runner==9.9.9'\n",
    ) is None


def test_git_change_set_removes_only_exact_obsolete_generated_files(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    obsolete = Path(
        ".github/workflows/foundry-post-deployment-check.yml"
    )
    old_content = "name: Foundry post-deployment check\n"
    destination = repository / obsolete
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(old_content, encoding="utf-8")
    _git(repository, "add", obsolete.as_posix())
    _git(repository, "commit", "-m", "legacy generated workflow")
    current = Path(
        ".github/workflows/foundry-optimization-reconcile.yml"
    )
    desired = _managed_contents(current, "name: Trusted transport retry\n")
    manifest_path = Path(".github/foundry-optimizer.generated.json")
    manifest = json.loads(desired[manifest_path])
    manifest["obsolete"] = {
        obsolete.as_posix(): [
            hashlib.sha256(old_content.encode("utf-8")).hexdigest()
        ]
    }
    desired[manifest_path] = (
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    writer = GitChangeSetWriter(SubprocessCommandRunner())

    changes = writer.write(repository, desired)

    removed = next(change for change in changes if change.path == obsolete)
    assert removed.status is ChangeStatus.REMOVED
    assert removed.commit_sha
    assert (
        _git(
            repository,
            "ls-tree",
            "-r",
            "--name-only",
            removed.commit_sha,
            "--",
            obsolete.as_posix(),
        )
        == ""
    )


def test_git_change_set_preserves_modified_obsolete_user_file(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    obsolete = Path(
        ".github/workflows/foundry-optimization-control.yml"
    )
    generated = "name: Foundry optimization control\n"
    modified = generated + "# user policy\n"
    destination = repository / obsolete
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(modified, encoding="utf-8")
    _git(repository, "add", obsolete.as_posix())
    _git(repository, "commit", "-m", "user modified generated workflow")
    current = Path(
        ".github/workflows/foundry-optimization-reconcile.yml"
    )
    desired = _managed_contents(current, "name: Trusted transport retry\n")
    manifest_path = Path(".github/foundry-optimizer.generated.json")
    manifest = json.loads(desired[manifest_path])
    manifest["obsolete"] = {
        obsolete.as_posix(): [
            hashlib.sha256(generated.encode("utf-8")).hexdigest()
        ]
    }
    desired[manifest_path] = (
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )

    with pytest.raises(ChangeSetConflictError) as raised:
        GitChangeSetWriter(SubprocessCommandRunner()).prevalidate(
            repository,
            desired,
        )

    assert raised.value.paths == (obsolete,)
    assert destination.read_text(encoding="utf-8") == modified


def test_git_change_set_rechecks_head_conflicts_after_prevalidation(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    path = Path(".github/foundry-optimizer.yaml")
    contents = {path: "schema_version: '1'\n"}
    writer = GitChangeSetWriter(SubprocessCommandRunner())
    writer.prevalidate(repository, contents)
    destination = repository / path
    destination.parent.mkdir(parents=True)
    destination.write_text("other process\n", encoding="utf-8")
    _git(repository, "add", path.as_posix())
    _git(repository, "commit", "-m", "other process wins")

    with pytest.raises(ChangeSetConflictError) as raised:
        writer.write(repository, contents)

    assert raised.value.paths == (path,)
    assert destination.read_text(encoding="utf-8") == "other process\n"


def test_git_change_set_rejects_repository_relative_escape(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(UnsafeChangePathError, match="repository-relative"):
        GitChangeSetWriter(SubprocessCommandRunner()).prevalidate(
            repository,
            {Path("../outside.yaml"): "unsafe\n"},
        )


def test_git_change_set_surfaces_temporary_index_cleanup_after_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = _repository(tmp_path)

    class FailingCommands:
        def __init__(self) -> None:
            self.delegate = SubprocessCommandRunner()

        def run(self, arguments, **kwargs):
            if tuple(arguments)[0:2] == ("git", "write-tree"):
                raise CommandExitError(
                    arguments,
                    exit_code=1,
                    stdout="",
                    stderr="write-tree failed",
                )
            return self.delegate.run(arguments, **kwargs)

    original_unlink = Path.unlink

    def fail_index_cleanup(path: Path, *args, **kwargs):
        if path.name.startswith("foundry-opt-index-") and not path.name.endswith(
            ".lock"
        ):
            raise OSError("index cleanup denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_index_cleanup)

    with pytest.raises(ChangeSetWriteError) as raised:
        GitChangeSetWriter(FailingCommands()).write(
            repository,
            {Path(".github/foundry-optimizer.yaml"): "schema_version: '1'\n"},
        )

    assert raised.value.residual_paths
    assert "index cleanup denied" in raised.value.cleanup_errors[0]


class FakeCommands:
    def __init__(
        self,
        *,
        fail_pr: bool = False,
        pr_exists: bool = False,
        current_head: str = "base123",
    ) -> None:
        self.fail_pr = fail_pr
        self.pr_exists = pr_exists
        self.current_head = current_head
        self.invocations: list[tuple[str, ...]] = []
        self.local_ref: str | None = None
        self.remote_ref: str | None = None

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
        environment: Mapping[str, str] | None = None,
        input_text: str | None = None,
        input_bytes: bytes | None = None,
    ) -> CommandResult:
        command = tuple(arguments)
        self.invocations.append(command)
        branch_ref = (
            "refs/heads/foundry-opt/"
            "onboarding-support-agent-commit123"
        )
        if command == ("git", "rev-parse", "--verify", "HEAD"):
            return CommandResult(0, f"{self.current_head}\n", "")
        if command == (
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ):
            return CommandResult(0, "", "")
        if command[0:2] == ("git", "for-each-ref"):
            return CommandResult(0, f"{self.local_ref or ''}\n", "")
        if command[0:3] == ("git", "ls-remote", "--heads"):
            output = (
                f"{self.remote_ref}\t{branch_ref}\n"
                if self.remote_ref
                else ""
            )
            return CommandResult(0, output, "")
        if command[0:2] == ("git", "update-ref"):
            if command[2] == "-d":
                expected = command[4]
                if self.local_ref != expected:
                    raise CommandExitError(
                        command,
                        exit_code=1,
                        stdout="",
                        stderr="ref changed",
                    )
                self.local_ref = None
            else:
                self.local_ref = command[3]
            return CommandResult(0, "", "")
        if command[0:2] == ("git", "push"):
            if command[-1].startswith(":"):
                self.remote_ref = None
            else:
                self.remote_ref = "commit123"
            return CommandResult(0, "", "")
        if command[0:3] == ("gh", "pr", "create"):
            if self.fail_pr:
                raise CommandExitError(
                    command,
                    exit_code=1,
                    stdout="",
                    stderr="API failure",
                )
            return CommandResult(
                0,
                "https://github.com/octo-org/agents/pull/42\n",
                "",
            )
        if command[0:3] == ("gh", "pr", "list"):
            return CommandResult(
                0,
                (
                    '[{"url":"https://github.com/octo-org/agents/pull/42"}]'
                    if self.pr_exists
                    else "[]"
                ),
                "",
            )
        raise AssertionError(f"Unexpected command: {command}")


def test_github_publisher_pushes_prepared_commit_without_checkout(
    tmp_path: Path,
) -> None:
    commands = FakeCommands()
    request, discovery, changes, draft_pr = _publication_inputs(tmp_path)

    result = GhOnboardingPublisher(commands).publish(
        request,
        discovery,
        changes,
        draft_pr,
    )

    branch = "foundry-opt/onboarding-support-agent-commit123"
    assert result.commit_sha == "commit123"
    assert result.branch == branch
    assert (
        "git",
        "update-ref",
        f"refs/heads/{branch}",
        "commit123",
        "0000000000000000000000000000000000000000",
    ) in commands.invocations
    assert (
        "git",
        "push",
        f"--force-with-lease=refs/heads/{branch}:",
        "origin",
        f"commit123:refs/heads/{branch}",
    ) in commands.invocations
    assert not any(
        command[0:2] in {("git", "switch"), ("git", "checkout")}
        for command in commands.invocations
    )


def test_github_publisher_deletes_only_invocation_refs_after_pr_failure(
    tmp_path: Path,
) -> None:
    commands = FakeCommands(fail_pr=True)
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
    assert commands.local_ref is None
    assert commands.remote_ref is None
    branch_ref = (
        "refs/heads/foundry-opt/onboarding-support-agent-commit123"
    )
    assert (
        "git",
        "push",
        f"--force-with-lease={branch_ref}:commit123",
        "origin",
        f":{branch_ref}",
    ) in commands.invocations
    assert (
        "git",
        "update-ref",
        "-d",
        branch_ref,
        "commit123",
    ) in commands.invocations


def test_github_publisher_rejects_commit_when_clean_head_changed(
    tmp_path: Path,
) -> None:
    commands = FakeCommands(current_head="new-head")
    request, discovery, changes, draft_pr = _publication_inputs(tmp_path)

    with pytest.raises(OnboardingPublishError, match="base HEAD changed") as raised:
        GhOnboardingPublisher(commands).publish(
            request,
            discovery,
            changes,
            draft_pr,
        )

    assert raised.value.phase == "inspect"
    assert commands.local_ref is None
    assert commands.remote_ref is None


def test_github_publisher_never_deletes_preexisting_remote_branch(
    tmp_path: Path,
) -> None:
    commands = FakeCommands()
    commands.remote_ref = "commit123"
    request, discovery, changes, draft_pr = _publication_inputs(tmp_path)

    with pytest.raises(OnboardingPublishError, match="already exists"):
        GhOnboardingPublisher(commands).publish(
            request,
            discovery,
            changes,
            draft_pr,
        )

    assert commands.remote_ref == "commit123"
    assert not any(
        command[0:2] == ("git", "push")
        and command[-1].startswith(":")
        for command in commands.invocations
    )


def test_github_publisher_preserves_branch_when_pr_creation_is_ambiguous(
    tmp_path: Path,
) -> None:
    commands = FakeCommands(fail_pr=True, pr_exists=True)
    request, discovery, changes, draft_pr = _publication_inputs(tmp_path)

    with pytest.raises(OnboardingPublishError) as raised:
        GhOnboardingPublisher(commands).publish(
            request,
            discovery,
            changes,
            draft_pr,
        )

    assert commands.remote_ref == "commit123"
    assert any("draft pr may already exist" in item for item in (
        value.casefold() for value in raised.value.residual_state
    ))


def test_production_onboarding_uses_git_plumbing_repository() -> None:
    dependencies = build_production_onboarding_dependencies()

    assert isinstance(dependencies.change_writer, GitChangeSetWriter)
    assert isinstance(dependencies.publisher, GhOnboardingPublisher)


def _publication_inputs(tmp_path: Path):
    changes = (
        OnboardingChange(
            path=Path(".github/foundry-optimizer.yaml"),
            content="schema_version: '1'\n",
            status=ChangeStatus.CREATED,
            base_commit="base123",
            commit_sha="commit123",
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
