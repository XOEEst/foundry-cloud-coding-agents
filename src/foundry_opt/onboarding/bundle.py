from __future__ import annotations

import json
from pathlib import Path

import yaml

from foundry_opt.onboarding.models import OnboardingRequest


_CHECKOUT_ACTION = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
_SETUP_PYTHON_ACTION = (
    "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97"
)
_SETUP_UV_ACTION = (
    "astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9"
)
_AZURE_LOGIN_ACTION = (
    "azure/login@532459ea530d8321f2fb9bb10d1e0bcf23869a43"
)


def generate_repository_agent_bundle(
    request: OnboardingRequest,
    *,
    oidc_subject: str,
) -> dict[Path, str]:
    contents = _copy_skill_template()
    skill_root = Path(".github/skills/foundry-agent-optimizer")
    contents[skill_root / "REPOSITORY_CONTEXT.md"] = (
        "# Repository optimization context\n\n"
        f"- Configured target: `{request.target_name}`\n"
        f"- Configured environment: `{request.environment_name}`\n"
        f"- Verified immutable OIDC subject: `{oidc_subject}`\n"
        "- Configuration: `.github/foundry-optimizer.yaml`\n"
        "- Azure authentication is OIDC-only; never request credentials.\n"
        "- GitHub Copilot receives non-secret Azure identifiers through "
        "repository-level Agents variables.\n"
    )
    contents.update(
        {
            Path(".foundry-optimizer/.gitignore"): (
                "campaigns/\nworktrees/\n"
            ),
            Path(".github/ISSUE_TEMPLATE/foundry-optimization.yml"): (
                _issue_form()
            ),
            Path(
                ".github/agents/foundry-optimization-planner.agent.md"
            ): _planner_agent(),
            Path(
                ".github/agents/foundry-optimization-runner.agent.md"
            ): _runner_agent(),
            Path(
                ".github/agents/foundry-candidate-applier.agent.md"
            ): _applier_agent(),
            Path(
                ".github/workflows/foundry-optimization-control.yml"
            ): _control_workflow(request),
            Path(
                ".github/workflows/foundry-exact-candidate-check.yml"
            ): _candidate_check_workflow(request),
            Path(
                ".github/workflows/foundry-post-deployment-check.yml"
            ): _post_deployment_workflow(request),
        }
    )
    return contents


def _copy_skill_template() -> dict[Path, str]:
    source_root = (
        Path(__file__).resolve().parents[1]
        / "templates"
        / "skills"
        / "foundry-agent-optimizer"
    )
    if not source_root.is_dir():
        raise RuntimeError("Foundry optimizer skill template is missing")
    destination_root = Path(".github/skills/foundry-agent-optimizer")
    return {
        destination_root / path.relative_to(source_root): path.read_text(
            encoding="utf-8"
        )
        for path in sorted(source_root.rglob("*"))
        if path.is_file()
    }


def _issue_form() -> str:
    document = {
        "name": "Foundry optimization",
        "description": (
            "Define a measurable optimization goal for a configured "
            "Microsoft Foundry agent."
        ),
        "title": "[Optimize] ",
        "labels": ["needs-triage"],
        "body": [
            {
                "type": "markdown",
                "attributes": {
                    "value": (
                        "Describe the desired behavior and evaluation contract. "
                        "Do not include credentials, raw traces, or private "
                        "dataset rows."
                    )
                },
            },
            _textarea(
                "target",
                "Configured target",
                "Target name from `.github/foundry-optimizer.yaml`.",
                "support-agent",
            ),
            _textarea(
                "goal",
                "Optimization goal",
                "State the measurable behavior to improve and what must remain unchanged.",
                (
                    "Improve complete policy coverage while preserving all "
                    "configured safety guardrails."
                ),
            ),
            _textarea(
                "datasets",
                "Dataset requests",
                (
                    "YAML list. Sources may be `foundry`, `repository`, "
                    "`synthetic`, or `trace`. Include development and "
                    "validation roles."
                ),
                (
                    "- asset_id: development\n"
                    "  source: foundry\n"
                    "  role: development\n"
                    "  name: support-development\n"
                    "  version: v1\n"
                    "- asset_id: validation\n"
                    "  source: synthetic\n"
                    "  role: validation\n"
                    "  name: support-validation\n"
                    "  version: issue-v1\n"
                    "  parameters:\n"
                    "    row_count: 20"
                ),
            ),
            _textarea(
                "evaluators",
                "Evaluator requests",
                "YAML list of existing, built-in, repository, or custom evaluators.",
                (
                    "- asset_id: task-quality\n"
                    "  source: foundry\n"
                    "  name: task-quality\n"
                    "  version: v1\n"
                    "  metrics: [quality]"
                ),
            ),
            _textarea(
                "metrics",
                "Metric policies",
                "YAML mapping with direction, threshold, materiality, and guardrail policy.",
                (
                    "quality:\n"
                    "  direction: maximize\n"
                    "  threshold: 0.8\n"
                    "  materiality: 0.05\n"
                    "  hard_guardrail: false\n"
                    "  undefined_behavior: fail"
                ),
            ),
            _textarea(
                "mutations",
                "Allowed mutations",
                "YAML list limited by the configured target policy.",
                "- system_instructions\n- python_logic\n- tests",
            ),
            {
                "type": "dropdown",
                "id": "decision",
                "attributes": {
                    "label": "Candidate decision",
                    "options": ["human", "autopilot_if_allowed"],
                    "default": 0,
                },
                "validations": {"required": True},
            },
            {
                "type": "dropdown",
                "id": "deployment",
                "attributes": {
                    "label": "Deployment decision",
                    "options": ["human", "after_merge_if_allowed"],
                    "default": 0,
                },
                "validations": {"required": True},
            },
            {
                "type": "checkboxes",
                "id": "confirmation",
                "attributes": {
                    "label": "Confirmation",
                    "options": [
                        {
                            "label": (
                                "I confirm this issue contains no credentials, "
                                "raw traces, or private dataset rows."
                            ),
                            "required": True,
                        }
                    ],
                },
            },
        ],
    }
    return yaml.safe_dump(document, sort_keys=False, width=100)


def _textarea(
    identifier: str,
    label: str,
    description: str,
    placeholder: str,
) -> dict:
    return {
        "type": "textarea",
        "id": identifier,
        "attributes": {
            "label": label,
            "description": description,
            "placeholder": placeholder,
        },
        "validations": {"required": True},
    }


def _planner_agent() -> str:
    return """---
name: foundry-optimization-planner
description: Prepare a reviewed immutable Foundry optimization specification from an issue.
target: github-copilot
tools: ["read", "search", "edit", "execute", "github/*", "foundry-mcp/*"]
disable-model-invocation: true
---

Read `.github/skills/foundry-agent-optimizer/SKILL.md`,
`REPOSITORY_CONTEXT.md`, and the assigned optimization issue.

Run `foundry-opt optimize spec --issue <number>`. Prepare only the specification
PR and approved local evaluation assets. Do not generate agent candidates, run
draft evaluations, merge, deploy, or expose raw trace rows.
"""


def _runner_agent() -> str:
    return """---
name: foundry-optimization-runner
description: Run an approved bounded Tenzing-style Foundry optimization job.
target: github-copilot
tools: ["read", "search", "edit", "execute", "github/*", "foundry-mcp/*"]
disable-model-invocation: true
---

Read `.github/skills/foundry-agent-optimizer/SKILL.md`,
`REPOSITORY_CONTEXT.md`, and the merged immutable specification referenced by
the assigned issue.

1. Run `foundry-opt optimize run --issue <number> --json`.
2. When the result is `awaiting_agent`, run
   `foundry-opt optimize candidate request --issue <number> --json`.
3. Read the returned `context_path`, edit only the returned candidate worktree,
   and write the strict idea JSON outside that worktree under the returned
   campaign candidate directory.
4. Run `foundry-opt optimize candidate submit --issue <number> --candidate
   <candidate-id> --idea-file <path> --json`.
5. Use development feedback in the next context to adapt the next idea. Repeat
   request/edit/submit until the next action says to finalize, then run
   `foundry-opt optimize run --issue <number> --json` again.

Follow the approved goal, assets, metrics, mutation allowlists, and Tenzing
adapter mapping. Use only Foundry drafts during optimization. Trust only CLI
status, metrics, and eligibility; never write those fields into an idea file.
Publish redacted evidence, exact patches, and candidate child issues; never
merge or deploy.
"""


def _applier_agent() -> str:
    return """---
name: foundry-candidate-applier
description: Apply one exact evaluated Foundry optimization patch without repair.
target: github-copilot
tools: ["read", "execute"]
disable-model-invocation: true
---

Read `.github/skills/foundry-agent-optimizer/SKILL.md` and the assigned
candidate issue. Invoke
`foundry-opt optimize apply --issue <number> --candidate <candidate-id>`.

Do not edit files directly, reinterpret the patch, repair conflicts, change the
base, merge, or deploy. Stop when deterministic verification rejects the
candidate.
"""


def _control_workflow(request: OnboardingRequest) -> str:
    install = _shell_quote(request.product_install)
    return f"""name: Foundry optimization control

on:
  workflow_dispatch:
    inputs:
      issue:
        description: Optimization issue number
        required: true
        type: number
      phase:
        description: Approved phase to run
        required: true
        type: choice
        options: [spec, run, reconcile]

permissions:
  contents: write
  issues: write
  pull-requests: write
  actions: write
  id-token: write

jobs:
  control:
    runs-on: ubuntu-latest
    environment: {json.dumps(request.environment_name)}
    env:
      GH_TOKEN: ${{{{ github.token }}}}
    steps:
      - uses: {_CHECKOUT_ACTION} # v7.0.1
      - uses: {_AZURE_LOGIN_ACTION} # v3.0.0
        with:
          client-id: ${{{{ vars.AZURE_CLIENT_ID }}}}
          tenant-id: ${{{{ vars.AZURE_TENANT_ID }}}}
          subscription-id: ${{{{ vars.AZURE_SUBSCRIPTION_ID }}}}
      - uses: {_SETUP_PYTHON_ACTION} # v7.0.0
        with:
          python-version: "3.12"
      - uses: {_SETUP_UV_ACTION} # v9.0.0
      - name: Install pinned optimizer
        run: uv tool install {install}
      - name: Run approved phase
        env:
          OPTIMIZE_PHASE: ${{{{ inputs.phase }}}}
          OPTIMIZATION_ISSUE: ${{{{ inputs.issue }}}}
        shell: bash
        run: foundry-opt optimize "$OPTIMIZE_PHASE" --issue "$OPTIMIZATION_ISSUE"
"""


def _candidate_check_workflow(request: OnboardingRequest) -> str:
    install = _shell_quote(request.product_install)
    return f"""name: Foundry exact candidate check

on:
  pull_request:
    types: [opened, synchronize, reopened, edited]

permissions:
  contents: read
  issues: read
  pull-requests: read

jobs:
  exact-candidate:
    if: >-
      startsWith(github.event.pull_request.head.ref, 'foundry-opt/') &&
      contains(github.event.pull_request.body, '<!-- foundry-opt:candidate-pr:')
    runs-on: ubuntu-latest
    env:
      GH_TOKEN: ${{{{ github.token }}}}
    steps:
      - uses: {_CHECKOUT_ACTION} # v7.0.1
        with:
          fetch-depth: 0
      - uses: {_SETUP_PYTHON_ACTION} # v7.0.0
        with:
          python-version: "3.12"
      - uses: {_SETUP_UV_ACTION} # v9.0.0
      - name: Install pinned optimizer
        run: uv tool install {install}
      - name: Extract validated candidate metadata
        id: metadata
        env:
          PR_BODY: ${{{{ github.event.pull_request.body }}}}
        shell: python
        run: |
          import os
          import re

          body = os.environ.get("PR_BODY", "")
          candidate_match = re.search(
              r"foundry-opt:candidate-pr:[^:]+:([^:]+):",
              body,
          )
          issue_match = re.search(r"Candidate issue: #(\\d+)", body)
          if candidate_match is None or issue_match is None:
              raise SystemExit("candidate metadata is missing")
          candidate = candidate_match.group(1)
          if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{{0,127}}", candidate) is None:
              raise SystemExit("candidate identifier is invalid")
          with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
              output.write(f"candidate={{candidate}}\\n")
              output.write(f"issue={{issue_match.group(1)}}\\n")
      - name: Verify exact candidate metadata and tree
        env:
          CANDIDATE: ${{{{ steps.metadata.outputs.candidate }}}}
          ISSUE: ${{{{ steps.metadata.outputs.issue }}}}
        shell: bash
        run: foundry-opt optimize apply --issue "$ISSUE" --candidate "$CANDIDATE" --verify-only
"""


def _post_deployment_workflow(request: OnboardingRequest) -> str:
    install = _shell_quote(request.product_install)
    return f"""name: Foundry post-deployment check

on:
  workflow_dispatch:
    inputs:
      issue:
        description: Optimization issue number
        required: true
        type: number

permissions:
  contents: read
  issues: write
  pull-requests: read
  actions: read
  id-token: write

jobs:
  verify:
    runs-on: ubuntu-latest
    environment: {json.dumps(request.environment_name)}
    env:
      GH_TOKEN: ${{{{ github.token }}}}
    steps:
      - uses: {_CHECKOUT_ACTION} # v7.0.1
      - uses: {_AZURE_LOGIN_ACTION} # v3.0.0
        with:
          client-id: ${{{{ vars.AZURE_CLIENT_ID }}}}
          tenant-id: ${{{{ vars.AZURE_TENANT_ID }}}}
          subscription-id: ${{{{ vars.AZURE_SUBSCRIPTION_ID }}}}
      - uses: {_SETUP_PYTHON_ACTION} # v7.0.0
        with:
          python-version: "3.12"
      - uses: {_SETUP_UV_ACTION} # v9.0.0
      - name: Install pinned optimizer
        run: uv tool install {install}
      - name: Verify deployed optimization
        env:
          OPTIMIZATION_ISSUE: ${{{{ inputs.issue }}}}
        run: foundry-opt optimize reconcile --issue "$OPTIMIZATION_ISSUE"
"""


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"
