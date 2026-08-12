from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re

import yaml

from foundry_opt.onboarding.bundle import (
    generate_repository_agent_bundle,
    legacy_repository_agent_bundle,
    legacy_repository_agent_hashes,
)
from foundry_opt.onboarding.models import OnboardingRequest
from foundry_opt.optimization.issues import (
    REQUIRED_HEADINGS,
    parse_optimization_issue_request,
)


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


def _workflows(files: dict[Path, str]) -> dict[Path, str]:
    return {
        path: text
        for path, text in files.items()
        if path.parts[:2] == (".github", "workflows")
    }


def test_bundle_generates_single_workspace_customer_surfaces() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
    )

    agents = {
        path
        for path in files
        if path.parts[:2] == (".github", "agents")
    }
    workflows = set(_workflows(files))

    assert agents == {
        Path(".github/agents/foundry-optimization-steward.agent.md"),
    }
    assert workflows == {
        Path(".github/workflows/foundry-optimization-workspace.yml"),
        Path(".github/workflows/foundry-optimization-operations.yml"),
        Path(".github/workflows/foundry-exact-candidate-check.yml"),
        Path(".github/workflows/deploy-foundry-agent.yml"),
    }
    assert not any(
        fragment in path.name
        for path in files
        for fragment in (
            "planner",
            "designer",
            "applier",
            "handoff",
            "capability",
            "reconcile",
            "deployment-bridge",
        )
    )

    steward = files[
        Path(".github/agents/foundry-optimization-steward.agent.md")
    ]
    assert "one persistent draft workspace pull request" in steward
    assert "compare bounded candidates internally" in steward
    assert "update the same pull request" in steward
    assert "Never create a worker issue" in steward
    assert "Never create an internal handoff pull request" in steward
    assert "planner, designer, or applier" in steward
    assert steward.count(
        "foundry-opt steward advance --issue <number> --json"
    ) == 1


def test_orchestration_onboarding_bundle_matches_golden_hashes() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
        deployment_workflow_name="Deploy support agent",
    )
    golden_path = (
        Path(__file__).resolve().parents[1]
        / "golden"
        / "orchestration-onboarding-bundle.json"
    )
    expected = json.loads(golden_path.read_text(encoding="utf-8"))
    actual = {
        path.as_posix(): hashlib.sha256(
            content.encode("utf-8")
        ).hexdigest()
        for path, content in files.items()
        if path.as_posix() in expected
    }

    assert actual == expected


def test_issue_form_matches_strict_parser_and_configured_target() -> None:
    request = replace(_request(), target_name="claims-agent")
    files = generate_repository_agent_bundle(
        request,
        oidc_subject="repository_id:123",
    )
    issue_form = yaml.safe_load(
        files[Path(".github/ISSUE_TEMPLATE/foundry-optimization.yml")]
    )
    fields = {
        field["attributes"]["label"]: field
        for field in issue_form["body"]
        if isinstance(field, dict) and "id" in field
    }
    assert tuple(fields) == REQUIRED_HEADINGS + ("Confirmation",)
    assert fields["Configured target"]["attributes"]["placeholder"] == (
        "claims-agent"
    )
    sections: list[str] = []
    for heading in REQUIRED_HEADINGS:
        field = fields[heading]
        attributes = field["attributes"]
        value = (
            attributes["options"][attributes.get("default", 0)]
            if field["type"] == "dropdown"
            else attributes["placeholder"]
        )
        sections.append(f"### {heading}\n\n{value}\n")

    parsed = parse_optimization_issue_request(
        issue_number=42,
        repository="octo-org/agents",
        body="\n".join(sections),
    )

    assert parsed.target == "claims-agent"
    assert issue_form["labels"] == ["needs-triage"]


def test_bundle_copies_skill_snapshot_and_workspace_context() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
    )
    skill_root = Path(".github/skills/foundry-agent-optimizer")

    assert skill_root / "SKILL.md" in files
    assert skill_root / "ADAPTER_MAPPING.md" in files
    assert skill_root / "references/tenzing/LICENSE" in files
    assert "MIT License" in files[
        skill_root / "references/tenzing/LICENSE"
    ]
    context = files[skill_root / "REPOSITORY_CONTEXT.md"]
    assert "support-agent" in context
    assert "acceptance" in context
    assert "repository_id:123" in context
    assert "tenant-id" not in context
    assert "client-id" not in context
    assert "one persistent draft workspace pull request" in context
    assert "same workspace pull request" in context
    assert "specialist pull requests" in context


def test_workspace_workflow_owns_intake_lifecycle_and_same_pr_resume() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
    )
    text = files[
        Path(".github/workflows/foundry-optimization-workspace.yml")
    ]
    workflow = yaml.safe_load(text)

    assert set(workflow[True]) == {
        "issues",
        "pull_request_target",
        "workflow_dispatch",
    }
    assert workflow["permissions"] == {
        "contents": "write",
        "issues": "write",
        "pull-requests": "write",
    }
    assert "COPILOT_ASSIGNMENT_TOKEN" in text
    assert "foundry-opt workspace advance --issue \"$ISSUE\" --json" in (
        " ".join(text.split())
    )
    assert "continue the same Copilot pull request" in text
    assert "foundry-opt:workspace-pr:issue-" in text
    assert "pull_request_target" in text
    assert "github.event.repository.default_branch" in text
    assert "ref: ${{ github.event.pull_request.head.sha }}" not in text
    assert "azure/login@" not in text
    assert "id-token: write" not in text
    assert "foundry-opt steward advance" not in text


def test_operations_workflow_uses_optimizer_oidc_and_owns_retention() -> None:
    request = replace(
        _request(),
        set_github_variables=True,
        mirror_actions_environment="production",
    )
    files = generate_repository_agent_bundle(
        request,
        oidc_subject="repository_id:123",
    )
    text = files[
        Path(".github/workflows/foundry-optimization-operations.yml")
    ]
    workflow = yaml.safe_load(text)

    assert workflow["permissions"] == {
        "contents": "write",
        "id-token": "write",
        "issues": "write",
        "pull-requests": "write",
    }
    assert workflow["jobs"]["operate"]["environment"] == "production"
    assert "AZURE_CLIENT_ID: ${{ vars.AZURE_CLIENT_ID }}" in text
    assert "AZURE_DEPLOYMENT_CLIENT_ID" not in text
    assert "python -m foundry_opt.orchestration.capability_bridge" in (
        " ".join(text.split())
    )
    assert "Evaluate retained post-deployment behavior" in text
    assert "foundry-opt optimize reconcile --issue \"$issue\" --json" in (
        " ".join(text.split())
    )
    assert "head -25" in text
    assert "ref: ${{ github.event.repository.default_branch }}" in text


def test_deployment_workflow_keeps_deployment_identity_isolated() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
        deployment_workflow_name="Deploy support agent",
    )
    text = files[Path(".github/workflows/deploy-foundry-agent.yml")]
    workflow = yaml.safe_load(text)

    assert workflow[True]["workflow_run"] == {
        "workflows": ["Deploy support agent"],
        "types": ["completed"],
    }
    assert workflow["permissions"] == {
        "actions": "write",
        "contents": "write",
        "id-token": "write",
    }
    assert "AZURE_CLIENT_ID: ${{ vars.AZURE_DEPLOYMENT_CLIENT_ID }}" in text
    assert "AZURE_CLIENT_ID: ${{ vars.AZURE_CLIENT_ID }}" not in text
    assert "foundry-opt steward deployment-bridge" in (
        " ".join(text.split())
    )
    assert "publication-result-auto" in text
    assert "foundry-opt optimize reconcile" not in text
    assert "foundry-optimization-reconcile.yml" not in text
    assert "COPILOT_ASSIGNMENT_TOKEN" not in text


def test_exact_check_never_executes_untrusted_pull_request_code() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
    )
    text = files[
        Path(".github/workflows/foundry-exact-candidate-check.yml")
    ]

    assert "shell: python" in text
    assert "PR_BODY: ${{ github.event.pull_request.body }}" in text
    assert 'run: foundry-opt optimize apply --issue "$ISSUE"' in text
    assert "source " not in text
    assert "eval " not in text
    assert "bash -c" not in text
    assert "${{ github.event.pull_request.body }}" not in "\n".join(
        line for line in text.splitlines() if line.lstrip().startswith("run:")
    )


def test_generated_workflows_are_yaml_safe_and_action_pinned() -> None:
    files = generate_repository_agent_bundle(
        replace(
            _request(),
            environment_name='prod"\\nrun: echo owned',
        ),
        oidc_subject="repository_id:123",
    )

    for path, text in _workflows(files).items():
        document = yaml.safe_load(text)
        assert document["jobs"], path
        for line in text.splitlines():
            if line.lstrip().startswith("uses:"):
                assert re.fullmatch(
                    r"\s*uses:\s*[^@\s]+@[0-9a-f]{40}(?:\s+#.*)?",
                    line,
                ), (path, line)
        assert "@main" not in text
        assert "@master" not in text
        assert "gh pr create" not in text
        assert "source " not in text

    operations = yaml.safe_load(
        files[
            Path(".github/workflows/foundry-optimization-operations.yml")
        ]
    )
    deployment = yaml.safe_load(
        files[Path(".github/workflows/deploy-foundry-agent.yml")]
    )
    assert operations["jobs"]["operate"]["environment"] == (
        'prod"\\nrun: echo owned'
    )
    assert deployment["jobs"]["deployment-bridge"]["environment"] == (
        'prod"\\nrun: echo owned'
    )


def test_privileged_workflows_use_pinned_isolated_installs() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
    )

    for path in (
        Path(".github/workflows/foundry-optimization-workspace.yml"),
        Path(".github/workflows/foundry-optimization-operations.yml"),
        Path(".github/workflows/deploy-foundry-agent.yml"),
    ):
        text = files[path]
        assert "fetch-depth: 0" in text
        assert "ref: ${{ github.event.repository.default_branch }}" in text
        assert "--no-project --no-config --no-env-file" in (
            " ".join(text.split())
        )


def test_bundle_contains_no_azure_secret_contract() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
    )
    text = "\n".join(files.values()).casefold()

    assert "azure_client_secret" not in text
    assert "client-secret" not in text
    assert "password:" not in text


def test_previous_generated_surfaces_remain_available_for_safe_cleanup() -> None:
    previous = legacy_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
        deployment_workflow_name="Deploy support agent",
    )

    for path in (
        Path(".github/agents/foundry-optimization-planner.agent.md"),
        Path(".github/agents/foundry-candidate-designer.agent.md"),
        Path(".github/agents/foundry-candidate-applier.agent.md"),
        Path(".github/workflows/foundry-optimization-issue-intake.yml"),
        Path(".github/workflows/foundry-optimization-reconcile.yml"),
        Path(".github/workflows/foundry-optimization-handoff.yml"),
        Path(".github/workflows/foundry-optimization-capability.yml"),
        Path(
            ".github/workflows/"
            "foundry-optimization-deployment-bridge.yml"
        ),
        Path(".github/workflows/foundry-optimization-control.yml"),
        Path(".github/workflows/foundry-post-deployment-check.yml"),
    ):
        assert path in previous

    bridge = previous[
        Path(
            ".github/workflows/"
            "foundry-optimization-deployment-bridge.yml"
        )
    ]
    assert "workflows: [\"Deploy support agent\"]" in bridge
    assert "gh workflow run foundry-optimization-reconcile.yml" in bridge
    hashes = legacy_repository_agent_hashes(
        _request(),
        oidc_subject="repository_id:123",
        deployment_workflow_name="Deploy support agent",
    )
    assert {
        Path(".github/workflows/campaign-drafts.yml"),
        Path(".github/workflows/campaign-evaluate.yml"),
    } <= set(hashes)
