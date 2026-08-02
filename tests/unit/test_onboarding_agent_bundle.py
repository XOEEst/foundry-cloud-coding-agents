from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re

import yaml

from foundry_opt.onboarding.bundle import generate_repository_agent_bundle
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


def test_bundle_generates_issue_form_and_four_custom_agents() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
    )

    assert {
        Path(".foundry-optimizer/.gitignore"),
        Path(".github/ISSUE_TEMPLATE/foundry-optimization.yml"),
        Path(".github/agents/foundry-optimization-planner.agent.md"),
        Path(".github/agents/foundry-candidate-designer.agent.md"),
        Path(".github/agents/foundry-candidate-applier.agent.md"),
        Path(".github/agents/foundry-optimization-steward.agent.md"),
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
    designer = files[
        Path(".github/agents/foundry-candidate-designer.agent.md")
    ]
    applier = files[
        Path(".github/agents/foundry-candidate-applier.agent.md")
    ]
    assert "prepare_specification_pr" in planner
    assert "github/*" not in planner
    assert "CandidateDesignIntent" in designer
    assert "CandidateDesignResult" in designer
    assert "github/*" not in designer
    assert "foundry-opt optimize apply" in applier
    assert 'tools: ["read", "execute"]' in applier
    assert "edit" not in applier.split("---", 2)[1]


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


def test_bundle_uses_issue_only_entry_and_canonical_specialists() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
    )

    assert Path(
        ".github/agents/foundry-candidate-designer.agent.md"
    ) in files
    assert Path(
        ".github/agents/foundry-optimization-runner.agent.md"
    ) not in files

    steward = files[
        Path(".github/agents/foundry-optimization-steward.agent.md")
    ]
    planner = files[
        Path(".github/agents/foundry-optimization-planner.agent.md")
    ]
    designer = files[
        Path(".github/agents/foundry-candidate-designer.agent.md")
    ]
    applier = files[
        Path(".github/agents/foundry-candidate-applier.agent.md")
    ]

    assert "foundry-opt steward advance --issue <number>" in steward
    assert "refs/heads/foundry-opt/state/issue-<number>" in steward
    assert "canonical steward interfaces" in steward
    assert "raw evidence" in steward
    assert "specialist_work_request" in steward
    assert "candidate_design" in steward
    assert "applier_worker_issue_planned" in steward

    assert "prepare_specification_pr" in planner
    assert "pull request is opened by the native Copilot session" in planner
    assert 'tools: ["read", "search", "edit", "execute"]' in planner
    assert "github/*" not in planner.split("---", 2)[1]

    assert "CandidateDesignIntent" in designer
    assert "CandidateDesignResult" in designer
    assert "only the reserved worktree" in designer
    assert "raw evidence" in designer
    assert 'tools: ["read", "search", "edit", "execute"]' in designer
    assert "disable-model-invocation: false" in designer
    assert "github/*" not in designer.split("---", 2)[1]

    assert "applier_worker_issue_planned" in applier
    assert "native Copilot session" in applier
    assert 'tools: ["read", "execute"]' in applier


def test_issue_form_matches_strict_parser_and_configured_target() -> None:
    request = replace(_request(), target_name="claims-agent")
    text = generate_repository_agent_bundle(
        request,
        oidc_subject="repository_id:123",
    )[Path(".github/ISSUE_TEMPLATE/foundry-optimization.yml")]
    issue_form = yaml.safe_load(text)

    assert issue_form["title"] == "[Optimize] "
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
        if field["type"] == "dropdown":
            value = attributes["options"][attributes.get("default", 0)]
        else:
            value = attributes["placeholder"]
        sections.append(f"### {heading}\n\n{value}\n")
    parsed = parse_optimization_issue_request(
        issue_number=42,
        repository="octo-org/agents",
        body="\n".join(sections),
    )

    assert parsed.target == "claims-agent"
    assert {asset.role for asset in parsed.datasets} == {
        "development",
        "validation",
    }


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


def test_generated_skill_documents_issue_only_orchestration() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
    )
    skill = files[
        Path(".github/skills/foundry-agent-optimizer/SKILL.md")
    ]
    normalized = " ".join(skill.split())

    assert "Create one `[Optimize]` issue" in skill
    assert "foundry-opt steward advance --issue <number>" in skill
    assert "native Copilot session" in skill
    assert "Merge exactly one eligible candidate pull request" in normalized
    assert "Do not start a campaign with `workflow_dispatch`" in skill
    assert "foundry-opt optimize run --issue" not in skill


def test_bundle_generates_transport_and_verification_workflows() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
    )

    workflow_paths = {
        Path(".github/workflows/foundry-optimization-issue-intake.yml"),
        Path(".github/workflows/foundry-optimization-reconcile.yml"),
        Path(
            ".github/workflows/"
            "foundry-optimization-deployment-bridge.yml"
        ),
        Path(".github/workflows/foundry-exact-candidate-check.yml"),
    }
    assert workflow_paths <= set(files)
    for path in workflow_paths:
        document = yaml.safe_load(files[path])
        assert document["jobs"]
        text = files[path]
        assert "foundry-opt" in text or "foundry_opt" in text
        assert "GH_TOKEN: ${{ github.token }}" in text
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
    assert "marker.group(3)" in workflow
    assert "len(markers) != 1" in workflow
    assert "campaign_match" not in workflow
    assert "Candidate issue: #" not in workflow


def test_dispatch_inputs_are_validated_through_environment_variables() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
    )
    recovery = files[
        Path(".github/workflows/foundry-optimization-reconcile.yml")
    ]
    deployment = files[
        Path(
            ".github/workflows/"
            "foundry-optimization-deployment-bridge.yml"
        )
    ]

    assert '--issue "${{ inputs.issue }}"' not in recovery
    assert "TRUSTED_ISSUE_NUMBER: ${{ inputs.issue }}" in recovery
    assert '--issue "${{ inputs.issue }}"' not in deployment
    assert "REQUESTED_ISSUE: ${{ inputs.issue }}" in deployment
    assert 'foundry-opt steward deployment-bridge --issue "$REQUESTED_ISSUE"' in (
        deployment
    )
    assert "31;echo" not in deployment


def test_state_ref_workflows_fetch_full_history() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
    )

    for path in (
        Path(".github/workflows/foundry-optimization-issue-intake.yml"),
        Path(".github/workflows/foundry-optimization-reconcile.yml"),
        Path(
            ".github/workflows/"
            "foundry-optimization-deployment-bridge.yml"
        ),
    ):
        assert "fetch-depth: 0" in files[path]
        assert "ref: ${{ github.event.repository.default_branch }}" in (
            files[path]
        )


def test_deployment_workflow_uses_only_deployment_oidc_identity() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
    )
    deployment = files[
        Path(
            ".github/workflows/"
            "foundry-optimization-deployment-bridge.yml"
        )
    ]
    transport = files[
        Path(".github/workflows/foundry-optimization-reconcile.yml")
    ]

    assert "AZURE_DEPLOYMENT_CLIENT_ID" in deployment
    assert "AZURE_CLIENT_ID: ${{ vars.AZURE_CLIENT_ID }}" not in deployment
    assert "azure/login@" not in transport
    assert "AZURE_" not in transport


def test_deployment_bridge_uses_selected_actions_environment() -> None:
    request = replace(
        _request(),
        set_github_variables=True,
        mirror_actions_environment="production",
    )
    deployment = yaml.safe_load(
        generate_repository_agent_bundle(
            request,
            oidc_subject="repository_id:123",
        )[
            Path(
                ".github/workflows/"
                "foundry-optimization-deployment-bridge.yml"
            )
        ]
    )

    assert deployment["jobs"]["deployment-bridge"]["environment"] == (
        "production"
    )


def test_bundle_generates_transport_only_issue_intake_workflow() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
    )
    path = Path(
        ".github/workflows/foundry-optimization-issue-intake.yml"
    )

    assert path in files
    workflow = yaml.safe_load(files[path])
    assert workflow[True]["issues"]["types"] == [
        "opened",
        "edited",
        "reopened",
        "closed",
    ]
    assert workflow[True]["pull_request"]["types"]
    assert "workflow_run" not in workflow[True]
    assert workflow["permissions"] == {
        "contents": "write",
        "issues": "write",
        "pull-requests": "write",
    }
    assert (
        workflow["jobs"]["bridge"]["if"]
        == (
            "github.event_name != 'issues' || "
            "github.event.action != 'opened' || "
            "startsWith(github.event.issue.title, '[Optimize] ')"
        )
    )
    text = files[path]
    assert "github.event_path" in text
    assert "github.event_name" in text
    assert "github.repository_id" in text
    assert "github.run_id" in text
    assert "foundry_opt.orchestration.issue_intake" in text
    assert "fetch-depth: 0" in text
    assert "id-token: write" not in text
    assert "pull-requests: write" in text
    assert "actions: write" not in text
    assert "AZURE_" not in text
    assert text.count("github.event.issue.title") == 1
    assert "github.event.issue.body" not in text
    assert "optimize spec" not in text
    assert "optimize run" not in text
    assert "optimize apply" not in text
    assert "optimize reconcile" not in text


def test_bundle_generates_transport_recovery_and_deployment_workflows() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
    )
    event_path = Path(
        ".github/workflows/foundry-optimization-issue-intake.yml"
    )
    recovery_path = Path(
        ".github/workflows/foundry-optimization-reconcile.yml"
    )
    deployment_path = Path(
        ".github/workflows/foundry-optimization-deployment-bridge.yml"
    )

    assert {event_path, recovery_path, deployment_path} <= set(files)
    assert Path(
        ".github/workflows/foundry-optimization-control.yml"
    ) not in files
    assert Path(
        ".github/workflows/foundry-post-deployment-check.yml"
    ) not in files

    events = yaml.safe_load(files[event_path])
    assert events[True]["issues"]["types"] == [
        "opened",
        "edited",
        "reopened",
        "closed",
    ]
    assert events[True]["pull_request"]["types"] == [
        "opened",
        "synchronize",
        "reopened",
        "edited",
        "closed",
    ]
    assert "workflow_run" not in events[True]
    assert events["permissions"] == {
        "contents": "write",
        "issues": "write",
        "pull-requests": "write",
    }
    assert "concurrency" not in events

    recovery = yaml.safe_load(files[recovery_path])
    assert recovery[True]["push"]["branches"] == [
        "foundry-opt/state/issue-*"
    ]
    assert recovery[True]["schedule"]
    assert recovery[True]["workflow_dispatch"]["inputs"]["issue"]["type"] == (
        "number"
    )
    assert recovery["permissions"] == {
        "contents": "write",
        "issues": "write",
        "pull-requests": "write",
    }
    assert "id-token" not in recovery["permissions"]
    assert "TRUSTED_STATE_REF: ${{ github.ref_name }}" in files[recovery_path]

    deployment = yaml.safe_load(files[deployment_path])
    assert deployment[True]["push"]["branches"] == [
        "foundry-opt/state/issue-*"
    ]
    assert deployment[True]["schedule"]
    assert deployment[True]["workflow_run"] == {
        "workflows": ["Foundry deployment"],
        "types": ["completed"],
    }
    assert deployment[True]["workflow_dispatch"]["inputs"]["issue"]["type"] == (
        "number"
    )
    assert deployment["permissions"] == {
        "actions": "write",
        "contents": "write",
        "id-token": "write",
    }
    assert recovery["concurrency"] == {
        "group": "foundry-optimization-effects",
        "cancel-in-progress": False,
    }
    assert "github.event.workflow_run.id" in deployment["concurrency"][
        "group"
    ]
    assert deployment["concurrency"]["cancel-in-progress"] is False
    bridge = files[deployment_path]
    assert "AZURE_DEPLOYMENT_CLIENT_ID" in bridge
    assert "AZURE_CLIENT_ID: ${{ vars.AZURE_CLIENT_ID }}" not in bridge
    assert (
        'uv run --no-project --with "$OPTIMIZER_PACKAGE" '
        'foundry-opt steward deployment-bridge'
    ) in bridge
    assert "uv tool install" not in bridge
    assert "foundry-optimization-deployment-result" in bridge
    assert "publication-result-auto" in bridge
    assert "gh workflow run foundry-optimization-reconcile.yml" in bridge
    assert 'environment: "acceptance"' in bridge

    transport_text = files[event_path] + files[recovery_path]
    assert "azure/login@" not in transport_text
    assert "AZURE_" not in transport_text
    assert "id-token: write" not in transport_text
    for forbidden in (
        "optimize spec",
        "optimize run",
        "optimize candidate",
        "optimize apply",
        "select candidate",
        "evaluate candidate",
        "gh pr create",
    ):
        assert forbidden not in transport_text.casefold()


def test_workflow_completion_trigger_is_limited_to_discovered_deployment() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
        deployment_workflow_name="Deploy support agent",
    )
    deployment = yaml.safe_load(
        files[
            Path(
                ".github/workflows/"
                "foundry-optimization-deployment-bridge.yml"
            )
        ]
    )

    assert deployment[True]["workflow_run"] == {
        "workflows": ["Deploy support agent"],
        "types": ["completed"],
    }


def test_generated_workflows_are_yaml_safe_and_action_pinned() -> None:
    files = generate_repository_agent_bundle(
        replace(
            _request(),
            environment_name='prod"\\nrun: echo owned',
        ),
        oidc_subject="repository_id:123",
    )
    workflows = {
        path: text
        for path, text in files.items()
        if path.parts[:2] == (".github", "workflows")
    }

    for path, text in workflows.items():
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

    deployment = yaml.safe_load(
        workflows[
            Path(
                ".github/workflows/"
                "foundry-optimization-deployment-bridge.yml"
            )
        ]
    )
    assert deployment["jobs"]["deployment-bridge"]["environment"] == (
        'prod"\\nrun: echo owned'
    )


def test_bundle_generates_copilot_steward_domain_instructions() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
    )
    steward = files[
        Path(".github/agents/foundry-optimization-steward.agent.md")
    ]

    assert "Git-state inbox" in steward
    assert "canonical steward interfaces" in steward
    assert "foundry-opt steward advance" in steward
    assert "raw evidence" in steward
