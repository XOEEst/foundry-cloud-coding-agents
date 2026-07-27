from pathlib import Path

import pytest

from foundry_opt.onboarding import (
    AppInsightsDiscovery,
    ChangeStatus,
    DatasetDiscovery,
    DeployedModelDiscovery,
    DeploymentWorkflowDiscovery,
    DraftProbeResult,
    DraftPullRequestPublication,
    EvaluatorDiscovery,
    FoundryAgentDiscovery,
    MetricDiscovery,
    OidcTrustResult,
    OnboardingChange,
    OnboardingDependencies,
    OnboardingRequest,
    OnboardingStatus,
    PythonAgentCandidate,
    RepositoryDiscovery,
    run_onboarding,
)
from foundry_opt.config import load_config
from foundry_opt.onboarding.repository import (
    ChangeSetConflictError,
    ChangeSetWriteError,
    OnboardingPublishError,
)


class FakeDiscovery:
    def discover(self, request: OnboardingRequest) -> RepositoryDiscovery:
        return RepositoryDiscovery(
            repository="octo-org/agents",
            repository_id="123456",
            default_branch="main",
            current_branch="main",
            authenticated_login="octocat",
            viewer_permission="ADMIN",
            clean=True,
            python_agents=(
                PythonAgentCandidate(
                    name="support-agent",
                    source_path=Path("agent"),
                    entry_point=Path("agent/main.py"),
                ),
            ),
            validation_commands=("uv run pytest", "uv run ruff check ."),
            foundry_agents=(
                FoundryAgentDiscovery(
                    name="support-agent",
                    versions=("7", "draft-probe-old"),
                ),
            ),
            deployed_models=(DeployedModelDiscovery(name="gpt-5.1"),),
            datasets=(
                DatasetDiscovery(name="development", versions=("v2",)),
                DatasetDiscovery(name="validation", versions=("v1",)),
            ),
            evaluators=(
                EvaluatorDiscovery(
                    name="quality",
                    reference="quality:3",
                    metrics=(
                        MetricDiscovery(
                            name="quality",
                            direction="maximize",
                            threshold=0.8,
                            materiality=0.05,
                            hard_guardrail=False,
                        ),
                    ),
                ),
            ),
            app_insights=AppInsightsDiscovery(connected=True),
            deployment_workflows=(
                DeploymentWorkflowDiscovery(
                    path=Path(".github/workflows/deploy.yml"),
                    trigger="merge",
                ),
            ),
        )


class FakeOidc:
    def verify(
        self,
        request: OnboardingRequest,
        discovery: RepositoryDiscovery,
    ) -> OidcTrustResult:
        return OidcTrustResult(
            subject="repo:octo-org@42/agents@123456",
            repository_id="123456",
            verified=True,
        )


class FakeDraftProbe:
    def __init__(self) -> None:
        self.probed = 0
        self.deleted: list[tuple[str, str]] = []

    def probe(
        self,
        request: OnboardingRequest,
        agent: FoundryAgentDiscovery,
    ) -> DraftProbeResult:
        self.probed += 1
        return DraftProbeResult(
            agent_name=agent.name,
            version="draft-onboarding-probe",
        )

    def delete_probe(self, agent_name: str, version: str) -> None:
        self.deleted.append((agent_name, version))


class PublishedVersionProbe(FakeDraftProbe):
    def probe(
        self,
        request: OnboardingRequest,
        agent: FoundryAgentDiscovery,
    ) -> DraftProbeResult:
        return DraftProbeResult(agent_name=agent.name, version="8")


class UnavailableProbe(FakeDraftProbe):
    def probe(
        self,
        request: OnboardingRequest,
        agent: FoundryAgentDiscovery,
    ) -> DraftProbeResult:
        raise RuntimeError("source-bundle draft API is not implemented")


class FakePublisher:
    def publish(self, request, discovery, changes, draft_pull_request):
        return DraftPullRequestPublication(
            url="https://github.com/octo-org/agents/pull/42",
            branch="foundry-opt/onboarding-support-agent",
            commit_sha="abc123",
        )


class FailingPublisher:
    def publish(self, request, discovery, changes, draft_pull_request):
        raise RuntimeError("push rejected")


class TestChangeWriter:
    def prevalidate(self, repository_root, contents):
        return tuple(
            OnboardingChange(path, content, ChangeStatus.PLANNED)
            for path, content in contents.items()
        )

    def write(self, repository_root, contents):
        changes = []
        for path, content in contents.items():
            destination = repository_root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
            changes.append(
                OnboardingChange(path, content, ChangeStatus.CREATED)
            )
        return tuple(changes)


def _request(repository_root: Path) -> OnboardingRequest:
    return OnboardingRequest(
        repository_root=repository_root,
        environment_name="acceptance",
        target_name="support-agent",
        project_endpoint=(
            "https://example.services.ai.azure.com/api/projects/demo"
        ),
        project_resource_id="/subscriptions/sub/projects/demo",
        tenant_id="tenant-id",
        client_id="client-id",
        subscription_id="subscription-id",
        product_install=(
            "foundry-cloud-coding-agent @ "
            "git+https://github.com/octo-org/product.git@"
            "0123456789abcdef0123456789abcdef01234567"
        ),
    )


def test_run_onboarding_generates_secretless_draft_change_set(
    tmp_path: Path,
) -> None:
    probe = FakeDraftProbe()

    result = run_onboarding(
        _request(tmp_path),
        OnboardingDependencies(
            discovery=FakeDiscovery(),
            oidc=FakeOidc(),
            draft_probe=probe,
            publisher=FakePublisher(),
            change_writer=TestChangeWriter(),
        ),
    )

    assert result.status is OnboardingStatus.READY
    assert result.exit_code == 0
    assert result.published_pull_request == DraftPullRequestPublication(
        url="https://github.com/octo-org/agents/pull/42",
        branch="foundry-opt/onboarding-support-agent",
        commit_sha="abc123",
    )
    assert result.draft_pull_request.title == "Configure Foundry optimizer onboarding"
    assert {change.path.as_posix() for change in result.changes} == {
        ".github/foundry-optimizer.yaml",
        ".github/workflows/copilot-setup-steps.yml",
        ".github/skills/foundry-agent-optimizer/SKILL.md",
    }
    assert probe.deleted == [
        ("support-agent", "draft-onboarding-probe")
    ]

    generated = "\n".join(
        change.content for change in result.changes
    )
    assert "authentication: oidc" in generated
    assert "id-token: write" in generated
    assert "python-version: '3.12'" in generated
    assert "AZURE_TENANT_ID=${{ vars.AZURE_TENANT_ID }}" in generated
    assert "AZURE_CLIENT_ID=${{ vars.AZURE_CLIENT_ID }}" in generated
    assert (
        "AZURE_SUBSCRIPTION_ID=${{ vars.AZURE_SUBSCRIPTION_ID }}"
        in generated
    )
    assert generated.count('>> "$GITHUB_ENV"') == 3
    assert "client-id: ${{ env.AZURE_CLIENT_ID }}" in generated
    assert "tenant-id: ${{ env.AZURE_TENANT_ID }}" in generated
    assert (
        "subscription-id: ${{ env.AZURE_SUBSCRIPTION_ID }}" in generated
    )
    assert "repository-ID" in generated
    assert (
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
        in generated
    )
    assert (
        "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97"
        in generated
    )
    assert (
        "astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9"
        in generated
    )
    assert (
        "azure/login@532459ea530d8321f2fb9bb10d1e0bcf23869a43"
        in generated
    )
    assert "AZURE_CLIENT_SECRET" not in generated
    assert "gh secret" not in generated
    assert "client-secret" not in generated.casefold()
    assert all(
        (tmp_path / change.path).read_text(encoding="utf-8")
        == change.content
        for change in result.changes
    )
    loaded = load_config(tmp_path / ".github/foundry-optimizer.yaml")
    assert loaded.default_environment == "acceptance"


def test_run_onboarding_preserves_existing_files_and_reports_conflicts(
    tmp_path: Path,
) -> None:
    existing = tmp_path / ".github/foundry-optimizer.yaml"
    existing.parent.mkdir(parents=True)
    existing.write_text("existing: true\n", encoding="utf-8")

    probe = FakeDraftProbe()
    result = run_onboarding(
        _request(tmp_path),
        OnboardingDependencies(
            discovery=FakeDiscovery(),
            oidc=FakeOidc(),
            draft_probe=probe,
        ),
    )

    assert result.status is OnboardingStatus.CONFLICT
    assert result.exit_code == 1
    assert existing.read_text(encoding="utf-8") == "existing: true\n"
    assert result.blockers == (
        "Existing path was not overwritten: .github/foundry-optimizer.yaml",
    )
    assert probe.probed == 0
    assert not (
        tmp_path / ".github/workflows/copilot-setup-steps.yml"
    ).exists()


def test_run_onboarding_rejects_non_draft_probe_without_deleting_it(
    tmp_path: Path,
) -> None:
    probe = PublishedVersionProbe()

    result = run_onboarding(
        _request(tmp_path),
        OnboardingDependencies(
            discovery=FakeDiscovery(),
            oidc=FakeOidc(),
            draft_probe=probe,
            change_writer=TestChangeWriter(),
        ),
    )

    assert result.status is OnboardingStatus.BLOCKED
    assert result.exit_code == 1
    assert probe.deleted == []
    assert "did not return a draft-* version" in result.blockers[0]
    assert {change.status.value for change in result.changes} == {"planned"}
    assert not (tmp_path / ".github/foundry-optimizer.yaml").exists()


def test_run_onboarding_exposes_placeholder_probe_as_a_real_blocker(
    tmp_path: Path,
) -> None:
    result = run_onboarding(
        _request(tmp_path),
        OnboardingDependencies(
            discovery=FakeDiscovery(),
            oidc=FakeOidc(),
            draft_probe=UnavailableProbe(),
            change_writer=TestChangeWriter(),
        ),
    )

    assert result.status is OnboardingStatus.BLOCKED
    assert "source-bundle draft API is not implemented" in result.blockers[0]
    assert {change.status.value for change in result.changes} == {"planned"}


def test_run_onboarding_requires_clean_default_branch_and_local_admin(
    tmp_path: Path,
) -> None:
    class UnsafeDiscovery(FakeDiscovery):
        def discover(self, request: OnboardingRequest) -> RepositoryDiscovery:
            safe = super().discover(request)
            return RepositoryDiscovery(
                **{
                    **safe.__dict__,
                    "current_branch": "feature",
                    "authenticated_login": "",
                    "viewer_permission": "WRITE",
                    "clean": False,
                }
            )

    result = run_onboarding(
        _request(tmp_path),
        OnboardingDependencies(
            discovery=UnsafeDiscovery(),
            oidc=FakeOidc(),
            draft_probe=FakeDraftProbe(),
        ),
    )

    assert result.status is OnboardingStatus.BLOCKED
    assert result.blockers == (
        "Onboarding must run on the default branch.",
        "Onboarding requires a clean worktree.",
        "A locally authenticated GitHub user is required.",
        "The authenticated GitHub user must have ADMIN permission.",
    )


def test_run_onboarding_rejects_unpinned_multiline_install_before_discovery(
    tmp_path: Path,
) -> None:
    class UnexpectedDiscovery:
        def discover(self, request: OnboardingRequest) -> RepositoryDiscovery:
            raise AssertionError("unsafe request reached discovery")

    unsafe = OnboardingRequest(
        **{
            **_request(tmp_path).__dict__,
            "product_install": (
                "foundry-cloud-coding-agent==0.1.0\n"
                "curl https://example.invalid"
            ),
        }
    )

    result = run_onboarding(
        unsafe,
        OnboardingDependencies(
            discovery=UnexpectedDiscovery(),
            oidc=FakeOidc(),
            draft_probe=FakeDraftProbe(),
        ),
    )

    assert result.status is OnboardingStatus.BLOCKED
    assert result.blockers == (
        "The product install must be pinned to a version or commit.",
    )


def test_run_onboarding_redacts_boundary_failure_details(
    tmp_path: Path,
) -> None:
    class SecretFailureDiscovery:
        def discover(self, request: OnboardingRequest) -> RepositoryDiscovery:
            raise RuntimeError("client_secret=hunter2")

    result = run_onboarding(
        _request(tmp_path),
        OnboardingDependencies(
            discovery=SecretFailureDiscovery(),
            oidc=FakeOidc(),
            draft_probe=FakeDraftProbe(),
        ),
    )

    assert "hunter2" not in result.blockers[0]
    assert "client_secret=[REDACTED]" in result.blockers[0]


def test_run_onboarding_returns_typed_evaluator_needs_input(
    tmp_path: Path,
) -> None:
    class NeedsInputDiscovery(FakeDiscovery):
        def discover(self, request: OnboardingRequest) -> RepositoryDiscovery:
            discovered = super().discover(request)
            return RepositoryDiscovery(
                **{
                    **discovered.__dict__,
                    "evaluators": (
                        EvaluatorDiscovery(
                            name="quality",
                            reference="quality:3",
                            needs_input=(
                                "Foundry did not expose optimizer metric "
                                "policy semantics."
                            ),
                        ),
                    ),
                }
            )

    result = run_onboarding(
        _request(tmp_path),
        OnboardingDependencies(
            discovery=NeedsInputDiscovery(),
            oidc=FakeOidc(),
            draft_probe=FakeDraftProbe(),
        ),
    )

    assert result.status is OnboardingStatus.BLOCKED
    assert result.blockers == (
        "Evaluator quality:3 needs input: Foundry did not expose optimizer "
        "metric policy semantics.",
    )


def test_run_onboarding_blocks_unknown_target_without_fallback(
    tmp_path: Path,
) -> None:
    probe = FakeDraftProbe()
    request = OnboardingRequest(
        **{**_request(tmp_path).__dict__, "target_name": "missing-agent"}
    )

    result = run_onboarding(
        request,
        OnboardingDependencies(
            discovery=FakeDiscovery(),
            oidc=FakeOidc(),
            draft_probe=probe,
        ),
    )

    assert result.status is OnboardingStatus.BLOCKED
    assert result.blockers == (
        "Target 'missing-agent' must exactly match one local Python agent; "
        "found 0.",
        "Target 'missing-agent' must exactly match one Foundry agent; found 0.",
    )
    assert probe.probed == 0


def test_run_onboarding_blocks_ambiguous_target_without_fallback(
    tmp_path: Path,
) -> None:
    class AmbiguousDiscovery(FakeDiscovery):
        def discover(self, request: OnboardingRequest) -> RepositoryDiscovery:
            discovered = super().discover(request)
            duplicate = discovered.python_agents[0]
            return RepositoryDiscovery(
                **{
                    **discovered.__dict__,
                    "python_agents": (duplicate, duplicate),
                }
            )

    probe = FakeDraftProbe()
    result = run_onboarding(
        _request(tmp_path),
        OnboardingDependencies(
            discovery=AmbiguousDiscovery(),
            oidc=FakeOidc(),
            draft_probe=probe,
        ),
    )

    assert result.status is OnboardingStatus.BLOCKED
    assert result.blockers == (
        "Target 'support-agent' must exactly match one local Python agent; "
        "found 2.",
    )
    assert probe.probed == 0


def test_run_onboarding_blocks_when_draft_pr_publication_fails(
    tmp_path: Path,
) -> None:
    result = run_onboarding(
        _request(tmp_path),
        OnboardingDependencies(
            discovery=FakeDiscovery(),
            oidc=FakeOidc(),
            draft_probe=FakeDraftProbe(),
            publisher=FailingPublisher(),
            change_writer=TestChangeWriter(),
        ),
    )

    assert result.status is OnboardingStatus.BLOCKED
    assert result.published_pull_request is None
    assert result.blockers == (
        "Draft pull request publication failed: push rejected",
    )


def test_run_onboarding_prevalidates_symlinked_parents_before_probe(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (tmp_path / ".github").symlink_to(
            outside,
            target_is_directory=True,
        )
    except OSError as error:
        pytest.skip(f"directory symlinks unavailable: {error}")
    probe = FakeDraftProbe()

    result = run_onboarding(
        _request(tmp_path),
        OnboardingDependencies(
            discovery=FakeDiscovery(),
            oidc=FakeOidc(),
            draft_probe=probe,
            publisher=FakePublisher(),
        ),
    )

    assert result.status is OnboardingStatus.BLOCKED
    assert "symlinked parent" in result.blockers[0]
    assert probe.probed == 0
    assert not (outside / "foundry-optimizer.yaml").exists()


def test_run_onboarding_reports_destination_race_as_conflict(
    tmp_path: Path,
) -> None:
    raced_path = Path(".github/foundry-optimizer.yaml")

    class RacingWriter:
        def prevalidate(self, repository_root, contents):
            return tuple(
                OnboardingChange(path, content, ChangeStatus.PLANNED)
                for path, content in contents.items()
            )

        def write(self, repository_root, contents):
            raise ChangeSetConflictError((raced_path,))

    result = run_onboarding(
        _request(tmp_path),
        OnboardingDependencies(
            discovery=FakeDiscovery(),
            oidc=FakeOidc(),
            draft_probe=FakeDraftProbe(),
            publisher=FakePublisher(),
            change_writer=RacingWriter(),
        ),
    )

    assert result.status is OnboardingStatus.CONFLICT
    assert result.blockers == (
        "Existing path was not overwritten: .github/foundry-optimizer.yaml",
    )
    assert next(
        change for change in result.changes if change.path == raced_path
    ).status is ChangeStatus.CONFLICT


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        (
            "datasets",
            (
                DatasetDiscovery("first", ("1",)),
                DatasetDiscovery("second", ("1",)),
            ),
            "Dataset roles require exactly one development and one validation "
            "dataset.",
        ),
        (
            "evaluators",
            (
                EvaluatorDiscovery(
                    "quality-a",
                    "quality-a:1",
                    metrics=(
                        MetricDiscovery(
                            "quality",
                            "maximize",
                            0.8,
                            0.05,
                            False,
                        ),
                    ),
                ),
                EvaluatorDiscovery(
                    "quality-b",
                    "quality-b:1",
                    metrics=(
                        MetricDiscovery(
                            "quality",
                            "maximize",
                            0.8,
                            0.05,
                            False,
                        ),
                    ),
                ),
            ),
            "Evaluator role is ambiguous; select exactly one optimization "
            "evaluator.",
        ),
        (
            "deployment_workflows",
            (
                DeploymentWorkflowDiscovery(
                    Path(".github/workflows/deploy-a.yml"),
                    "manual",
                ),
                DeploymentWorkflowDiscovery(
                    Path(".github/workflows/deploy-b.yml"),
                    "manual",
                ),
            ),
            "Deployment workflow role is ambiguous; select exactly one "
            "deployment workflow.",
        ),
    ],
)
def test_run_onboarding_blocks_ambiguous_discovered_roles(
    tmp_path: Path,
    field: str,
    value: tuple,
    expected: str,
) -> None:
    class AmbiguousRoleDiscovery(FakeDiscovery):
        def discover(self, request: OnboardingRequest) -> RepositoryDiscovery:
            discovered = super().discover(request)
            return RepositoryDiscovery(
                **{**discovered.__dict__, field: value}
            )

    result = run_onboarding(
        _request(tmp_path),
        OnboardingDependencies(
            discovery=AmbiguousRoleDiscovery(),
            oidc=FakeOidc(),
            draft_probe=FakeDraftProbe(),
            publisher=FakePublisher(),
        ),
    )

    assert result.status is OnboardingStatus.BLOCKED
    assert expected in result.blockers


def test_run_onboarding_surfaces_residual_change_set_state(
    tmp_path: Path,
) -> None:
    class ResidualWriter(TestChangeWriter):
        def write(self, repository_root, contents):
            raise ChangeSetWriteError(
                "cleanup failed",
                residual_paths=(
                    Path(".github/foundry-optimizer.yaml"),
                ),
                cleanup_errors=("permission denied",),
            )

    result = run_onboarding(
        _request(tmp_path),
        OnboardingDependencies(
            discovery=FakeDiscovery(),
            oidc=FakeOidc(),
            draft_probe=FakeDraftProbe(),
            publisher=FakePublisher(),
            change_writer=ResidualWriter(),
        ),
    )

    assert result.status is OnboardingStatus.PARTIAL
    assert result.residual_state == (
        ".github/foundry-optimizer.yaml",
        "permission denied",
    )


def test_run_onboarding_surfaces_publication_compensation_residuals(
    tmp_path: Path,
) -> None:
    class ResidualPublisher:
        def publish(self, request, discovery, changes, draft_pull_request):
            raise OnboardingPublishError(
                "draft PR failed",
                phase="draft_pr",
                residual_state=(
                    "remote branch foundry-opt/onboarding-support-agent "
                    "may remain at abc123",
                ),
            )

    result = run_onboarding(
        _request(tmp_path),
        OnboardingDependencies(
            discovery=FakeDiscovery(),
            oidc=FakeOidc(),
            draft_probe=FakeDraftProbe(),
            publisher=ResidualPublisher(),
            change_writer=TestChangeWriter(),
        ),
    )

    assert result.status is OnboardingStatus.PARTIAL
    assert result.residual_state == (
        "remote branch foundry-opt/onboarding-support-agent may remain at "
        "abc123",
    )


def test_run_onboarding_accepts_explicit_discovery_roles(
    tmp_path: Path,
) -> None:
    class ExplicitRoleDiscovery(FakeDiscovery):
        def discover(self, request: OnboardingRequest) -> RepositoryDiscovery:
            discovered = super().discover(request)
            metric = discovered.evaluators[0].metrics
            return RepositoryDiscovery(
                **{
                    **discovered.__dict__,
                    "datasets": (
                        DatasetDiscovery(
                            "first",
                            ("1",),
                            role="validation",
                        ),
                        DatasetDiscovery(
                            "second",
                            ("1",),
                            role="development",
                        ),
                    ),
                    "evaluators": (
                        EvaluatorDiscovery(
                            "unused",
                            "unused:1",
                            metrics=(),
                            needs_input="metric policy is not configured",
                        ),
                        EvaluatorDiscovery(
                            "quality",
                            "quality:1",
                            metrics=metric,
                            role="optimization",
                        ),
                    ),
                    "deployment_workflows": (
                        DeploymentWorkflowDiscovery(
                            Path(".github/workflows/other.yml"),
                            "manual",
                        ),
                        DeploymentWorkflowDiscovery(
                            Path(".github/workflows/deploy.yml"),
                            "manual",
                            role="deployment",
                        ),
                    ),
                }
            )

    result = run_onboarding(
        _request(tmp_path),
        OnboardingDependencies(
            discovery=ExplicitRoleDiscovery(),
            oidc=FakeOidc(),
            draft_probe=FakeDraftProbe(),
            publisher=FakePublisher(),
            change_writer=TestChangeWriter(),
        ),
    )

    assert result.status is OnboardingStatus.READY
    config = load_config(tmp_path / ".github/foundry-optimizer.yaml")
    target = config.targets["support-agent"]
    assert target.datasets.development[0].name == "second"
    assert target.datasets.validation[0].name == "first"
    assert target.evaluators[0].name == "quality"
    assert (
        config.environments["acceptance"].deployment_workflow.path.as_posix()
        == ".github/workflows/deploy.yml"
    )
