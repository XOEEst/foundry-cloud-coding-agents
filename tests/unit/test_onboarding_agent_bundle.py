from __future__ import annotations

from pathlib import Path

import yaml

from foundry_opt.onboarding.bundle import generate_repository_agent_bundle
from foundry_opt.onboarding.models import OnboardingRequest


def _request() -> OnboardingRequest:
    return OnboardingRequest(
        repository_root=Path("."),
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


def test_bundle_generates_issue_form_and_three_custom_agents() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
    )

    assert {
        Path(".foundry-optimizer/.gitignore"),
        Path(".github/ISSUE_TEMPLATE/foundry-optimization.yml"),
        Path(".github/agents/foundry-optimization-planner.agent.md"),
        Path(".github/agents/foundry-optimization-runner.agent.md"),
        Path(".github/agents/foundry-candidate-applier.agent.md"),
    } <= set(files)

    issue_form = yaml.safe_load(
        files[Path(".github/ISSUE_TEMPLATE/foundry-optimization.yml")]
    )
    assert issue_form["name"] == "Foundry optimization"
    assert issue_form["labels"] == ["needs-triage"]
    field_ids = {
        field["id"]
        for field in issue_form["body"]
        if isinstance(field, dict) and "id" in field
    }
    assert {
        "target",
        "goal",
        "datasets",
        "evaluators",
        "metrics",
        "mutations",
        "decision",
        "deployment",
    } <= field_ids

    planner = files[
        Path(".github/agents/foundry-optimization-planner.agent.md")
    ]
    runner = files[
        Path(".github/agents/foundry-optimization-runner.agent.md")
    ]
    applier = files[
        Path(".github/agents/foundry-candidate-applier.agent.md")
    ]
    assert "foundry-opt optimize spec" in planner
    assert "foundry-mcp/*" in planner
    assert "foundry-opt optimize run" in runner
    assert "optimize candidate request" in runner
    assert "optimize candidate submit" in runner
    assert "foundry-mcp/*" in runner
    assert "foundry-opt optimize apply" in applier
    assert 'tools: ["read", "execute"]' in applier
    assert "edit" not in applier.split("---", 2)[1]


def test_bundle_copies_tenzing_snapshot_license_and_context() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
    )

    skill_root = Path(".github/skills/foundry-agent-optimizer")
    assert skill_root / "SKILL.md" in files
    assert skill_root / "ADAPTER_MAPPING.md" in files
    assert skill_root / "references/tenzing/LICENSE" in files
    assert skill_root / "references/tenzing/climb.md" in files
    assert "MIT License" in files[
        skill_root / "references/tenzing/LICENSE"
    ]
    context = files[skill_root / "REPOSITORY_CONTEXT.md"]
    assert "support-agent" in context
    assert "acceptance" in context
    assert "repository_id:123" in context
    assert "tenant-id" not in context
    assert "client-id" not in context


def test_bundle_generates_control_and_verification_workflows() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
    )

    workflow_paths = {
        Path(".github/workflows/foundry-optimization-control.yml"),
        Path(".github/workflows/foundry-exact-candidate-check.yml"),
        Path(".github/workflows/foundry-post-deployment-check.yml"),
    }
    assert workflow_paths <= set(files)
    for path in workflow_paths:
        document = yaml.safe_load(files[path])
        assert document["jobs"]
        text = files[path]
        assert "foundry-opt" in text
        assert "@main" not in text
        assert "@master" not in text


def test_generated_bundle_contains_no_azure_secret_contract() -> None:
    generated = "\n".join(
        generate_repository_agent_bundle(
            _request(),
            oidc_subject="repository_id:123",
        ).values()
    )

    assert "AZURE_CLIENT_SECRET" not in generated
    assert "client-secret" not in generated.casefold()
    assert "repository_id:123" in generated


def test_bundle_ignores_runtime_state_but_not_approved_specs() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
    )

    ignore = files[Path(".foundry-optimizer/.gitignore")]
    assert ignore == "campaigns/\nworktrees/\n"
    assert "specs/" not in ignore


def test_candidate_check_never_sources_pr_controlled_shell_content() -> None:
    workflow = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
    )[Path(".github/workflows/foundry-exact-candidate-check.yml")]

    assert "source candidate.env" not in workflow
    assert "> candidate.env" not in workflow
    assert "GITHUB_OUTPUT" in workflow
    assert (
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
        in workflow
    )
    assert "steps.metadata.outputs.candidate" in workflow
    assert "contains(github.event.pull_request.body" in workflow


def test_dispatch_inputs_are_passed_through_environment_variables() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
    )
    control = files[
        Path(".github/workflows/foundry-optimization-control.yml")
    ]
    deployed = files[
        Path(".github/workflows/foundry-post-deployment-check.yml")
    ]

    assert 'foundry-opt optimize "${{ inputs.phase }}"' not in control
    assert '--issue "${{ inputs.issue }}"' not in control
    assert "OPTIMIZE_PHASE: ${{ inputs.phase }}" in control
    assert "OPTIMIZATION_ISSUE: ${{ inputs.issue }}" in control
    assert 'foundry-opt optimize "$OPTIMIZE_PHASE"' in control
    assert '--issue "$OPTIMIZATION_ISSUE"' in control
    assert '--issue "${{ inputs.issue }}"' not in deployed
    assert "OPTIMIZATION_ISSUE: ${{ inputs.issue }}" in deployed
