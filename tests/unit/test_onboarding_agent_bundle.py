from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re

from typer.testing import CliRunner
import yaml

from foundry_opt.cli import app
from foundry_opt.onboarding.bundle import (
    generate_repository_agent_bundle,
    legacy_repository_agent_bundle,
    legacy_repository_agent_hashes,
)
from foundry_opt.onboarding.generation import _render_setup_workflow
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
    normalized_steward = " ".join(steward.split())
    assert "one persistent draft workspace pull request" in normalized_steward
    assert "compare bounded candidates internally" in normalized_steward
    assert "Update the same pull request" in normalized_steward
    assert "Never create another issue" in normalized_steward
    assert "a handoff artifact" in normalized_steward
    assert "or a second optimization pull request" in normalized_steward
    assert (
        "returned durable workspace state and `next_action`"
        in normalized_steward
    )
    assert "`next_action.candidate_work`" in normalized_steward
    assert "schema-v3 JSON manifest" in normalized_steward
    assert "--candidate-manifest <manifest.json> --json" in normalized_steward
    assert "revision-bound continuation" in normalized_steward
    assert "proxy_import_required" in normalized_steward
    assert ".foundry-optimizer/workspace-candidate.json" in steward
    assert "Do not stop merely because" in normalized_steward
    assert "waiting for an external operation" in normalized_steward
    assert "waiting for the human merge" in normalized_steward
    assert steward.count(
        "foundry-opt workspace advance --issue <number> --json"
    ) == 1
    assert "foundry-opt steward advance" not in steward
    assert Path(".github/workflows/deploy-foundry-agent.yml") not in files


def test_generated_customer_instructions_have_no_legacy_transport() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
    )
    instruction_paths = (
        Path(".github/agents/foundry-optimization-steward.agent.md"),
        Path(
            ".github/skills/foundry-agent-optimizer/"
            "ADAPTER_MAPPING.md"
        ),
        Path(".github/skills/foundry-agent-optimizer/SKILL.md"),
        Path(
            ".github/skills/foundry-agent-optimizer/"
            "REPOSITORY_CONTEXT.md"
        ),
    )
    text = "\n".join(files[path] for path in instruction_paths).casefold()

    for forbidden in (
        "worker issue",
        "specialist agent",
        "internal handoff",
        "candidate pull request",
        "candidate pr",
        "foundry-optimization-capability.yml",
        "foundry-optimization-deployment-bridge.yml",
        "foundry-optimization-handoff.yml",
        "foundry-optimization-issue-intake.yml",
        "foundry-optimization-reconcile.yml",
        "foundry-opt steward advance",
    ):
        assert forbidden not in text
    assert "foundry-opt workspace advance" in text
    assert "next_actions" in text


def test_every_emitted_foundry_opt_command_resolves_against_cli() -> None:
    request = _request()
    files = generate_repository_agent_bundle(
        request,
        oidc_subject="repository_id:123",
        deployment_workflow_name="Deploy support agent",
    )
    files[Path(".github/workflows/copilot-setup-steps.yml")] = (
        _render_setup_workflow(
            request,
            warm_token_cache=True,
            export_proxy_marker=True,
        )
    )
    emitted = " ".join("\n".join(files.values()).split())
    occurrences = re.findall(
        r"\bfoundry-opt\s+(--version|[a-z][a-z0-9-]*)"
        r"(?:\s+([a-z][a-z0-9-]*))?"
        r"(?:\s+([a-z][a-z0-9-]*))?",
        emitted,
    )
    assert occurrences

    runner = CliRunner()
    resolved: set[str] = set()
    for first, second, third in occurrences:
        if first == "--version":
            result = runner.invoke(app, ["--version"])
            command = "--version"
        else:
            candidates = (
                ([first, second, third, "--help"], f"{first} {second} {third}")
                if second and third
                else None,
                ([first, second, "--help"], f"{first} {second}")
                if second
                else None,
                ([first, "--help"], first),
            )
            result = None
            command = ""
            for candidate in candidates:
                if candidate is None:
                    continue
                arguments, name = candidate
                completed = runner.invoke(app, arguments)
                if completed.exit_code == 0:
                    result = completed
                    command = name
                    break
            assert result is not None, (first, second, third)
        assert result.exit_code == 0, (command, result.stdout)
        resolved.add(command)

    assert {
        "init",
        "workspace advance",
        "workspace operations execute",
        "workspace operations reconcile",
    } <= resolved


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
    assert "secondary optimization" in context
    assert "specialist pull requests" not in context


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
        "issue_comment",
        "pull_request_target",
        "schedule",
        "workflow_run",
        "workflow_dispatch",
    }
    assert workflow["permissions"] == {
        "actions": "write",
        "contents": "write",
        "issues": "write",
        "pull-requests": "write",
    }
    assert "COPILOT_ASSIGNMENT_TOKEN" in text
    assert (
        "GH_TOKEN: ${{ secrets.COPILOT_ASSIGNMENT_TOKEN }}"
        in text
    )
    assert '"${command[@]}" advance --issue "$ISSUE" --json' in text
    assert "intake" in text
    assert '--event-path "$TRUSTED_EVENT_PATH"' in text
    assert '--event-name "$TRUSTED_EVENT_NAME"' in text
    assert '--delivery-id "$TRUSTED_RUN_ID"' in text
    assert 'args+=(--base-commit "$(git rev-parse HEAD)")' in text
    assert "Dispatch trusted workspace operations" in text
    assert "gh workflow run foundry-optimization-operations.yml" in text
    assert '--ref "$DEFAULT_BRANCH"' in text
    assert '-f "issue=$ISSUE"' in text
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
        "actions": "write",
        "checks": "write",
        "contents": "write",
        "id-token": "write",
        "issues": "write",
        "pull-requests": "write",
    }
    assert workflow["jobs"]["operate"]["environment"] == "production"
    assert workflow[True]["workflow_run"] == {
        "workflows": ["Foundry deployment"],
        "types": ["completed"],
    }
    assert "AZURE_CLIENT_ID: ${{ vars.AZURE_CLIENT_ID }}" in text
    assert "AZURE_DEPLOYMENT_CLIENT_ID" not in text
    assert "foundry-opt workspace operations execute" in (
        " ".join(text.split())
    )
    assert "foundry-opt workspace operations reconcile" in (
        " ".join(text.split())
    )
    assert "gh run download" in text
    assert "workspace-resume.ndjson" in text
    assert "Publish trusted exact verification check and ready finalized workspace pull request" in text
    assert 'repos/{repository}/commits/{head_sha}/check-runs' in text
    assert 'repos/{repository}/check-runs/{check_run_id}' in text
    assert '"foundry-opt",' in text
    assert '"workspace",' in text
    assert '"verify",' in text
    assert '"ready",' in text
    assert '"assign",' in text
    assert "gh pr comment" not in text
    assert "@copilot" not in text
    assert "head -25" in text
    assert text.count(
        "GH_TOKEN: ${{ secrets.COPILOT_ASSIGNMENT_TOKEN }}"
    ) == 1
    assert (
        'if [ "$TRUSTED_EVENT_NAME" = "push" ] &&'
        in text
    )
    assert "ref: ${{ github.event.repository.default_branch }}" in text
    assert "push:" not in text
    for forbidden in (
        "optimize reconcile",
        "capability_bridge",
        "deployment_bridge",
        "worker issue",
        "internal handoff",
        "campaign-drafts",
        "campaign-evaluate",
    ):
        assert forbidden not in text


def test_operations_workflow_resumes_same_workspace_pull_request_without_creating_new_work() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
    )
    text = files[
        Path(".github/workflows/foundry-optimization-operations.yml")
    ]

    assert "Resume same workspace pull request when trusted state needs Copilot" in text
    assert text.index(
        "Publish trusted exact verification check and ready finalized workspace pull request"
    ) < text.index(
        "Resume same workspace pull request when trusted state needs Copilot"
    )
    assert "workspace resume payload is invalid" in text
    assert text.index("entries.add(issue)") < text.index(
        'resume = document.get("resume")'
    )
    assert "foundry-opt" in text
    assert "workspace" in text
    assert "assign" in text
    assert "gh pr comment" not in text
    assert "COPILOT_ASSIGNMENT_TOKEN" in text
    assert 'GH_TOKEN: ""' in text
    assert "FOUNDRY_OPT_DEPLOYMENT_GH_TOKEN: ${{ github.token }}" in text
    assert "deployment_run_id:" in text
    assert "inputs.deployment_run_id != ''" in text
    assert (
        "github.event.workflow_run.id || inputs.deployment_run_id"
        in text
    )
    assert (
        'environment["GH_TOKEN"] = '
        'environment["COPILOT_ASSIGNMENT_TOKEN"]'
    ) in text
    assert 'print(result.stderr, end="", file=sys.stderr)' in text
    assert "raise SystemExit(result.returncode)" in text
    assert "gh pr create" not in text
    assert "gh issue edit" not in text
    assert "--add-assignee" not in text
    assert "worker issue" not in text


def test_workspace_workflow_imports_candidate_envelope_from_exact_pr_head() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
    )
    text = files[
        Path(".github/workflows/foundry-optimization-workspace.yml")
    ]

    assert "workspace-candidate.json" in text
    assert 'git fetch --no-tags origin "$head_sha"' in text
    assert 'git show "$head_sha:$envelope_path"' in text
    assert 'envelope["kind"] != "workspace_candidate_proposal"' in text
    assert "Workspace candidate envelope is stale" in text
    assert '--candidate-manifest "$manifest_file"' in text
    assert 'branch="foundry-opt/workspace/issue-$ISSUE"' in text


def test_workspace_workflow_scans_candidate_envelopes_from_trusted_schedule() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
    )
    text = files[
        Path(".github/workflows/foundry-optimization-workspace.yml")
    ]

    assert 'cron: "*/5 * * * *"' in text
    assert "scan-candidate-envelopes:" in text
    assert "github.event_name == 'schedule'" in text
    assert ".foundry-optimizer/workspace-candidate.json" in text
    assert "foundry-opt/state/issue-" in text
    assert '"foundry-optimization-workspace.yml"' in text
    assert 'f"issue={issue}"' in text
    assert 'workflows: ["CodeQL"]' in text
    assert "github.event.workflow_run.conclusion == 'success'" in text


def test_workspace_workflow_imports_after_copilot_completion_comment() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
    )
    text = files[
        Path(".github/workflows/foundry-optimization-workspace.yml")
    ]

    assert "issue_comment:" in text
    assert "github.event.comment.user.login == 'Copilot'" in text
    assert 'event_name == "issue_comment"' in text
    assert '[ "$TRUSTED_EVENT_NAME" = "issue_comment" ]' in text


def test_bundle_preserves_customer_deployment_workflow() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
        deployment_workflow_name="Deploy support agent",
    )
    operations = yaml.safe_load(
        files[
            Path(".github/workflows/foundry-optimization-operations.yml")
        ]
    )

    assert Path(".github/workflows/deploy-foundry-agent.yml") not in files
    assert operations[True]["workflow_run"] == {
        "workflows": ["Deploy support agent"],
        "types": ["completed"],
    }


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
    assert '"foundry-opt",' in text
    assert '"workspace",' in text
    assert '"verify",' in text
    assert 'GITHUB_STEP_SUMMARY' in text
    assert 'summary_markdown' in text
    assert "source " not in text
    assert "eval " not in text
    assert "bash -c" not in text
    assert "${{ github.event.pull_request.body }}" not in "\n".join(
        line for line in text.splitlines() if line.lstrip().startswith("run:")
    )
    assert 'optimize apply --issue "$ISSUE"' not in text


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
    assert operations["jobs"]["operate"]["environment"] == (
        'prod"\\nrun: echo owned'
    )
    assert Path(".github/workflows/deploy-foundry-agent.yml") not in files


def test_privileged_workflows_use_pinned_isolated_installs() -> None:
    files = generate_repository_agent_bundle(
        _request(),
        oidc_subject="repository_id:123",
    )

    for path in (
        Path(".github/workflows/foundry-optimization-workspace.yml"),
        Path(".github/workflows/foundry-optimization-operations.yml"),
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
