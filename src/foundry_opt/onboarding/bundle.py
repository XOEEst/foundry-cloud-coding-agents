from __future__ import annotations

from dataclasses import replace
import hashlib
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
    deployment_workflow_name: str = "Foundry deployment",
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
        "- Deployment uses only the separate "
        "`AZURE_DEPLOYMENT_CLIENT_ID` Actions-environment variable.\n"
        "- Copilot session assignment uses the repository Actions secret "
        "`COPILOT_ASSIGNMENT_TOKEN`, containing a least-privilege "
        "user-to-server token; an installation token is not supported.\n"
        "- foundry-opt init cannot create Actions secrets; configure the "
        "assignment secret manually and never commit its value.\n"
        "- Create the generated `[Optimize]` issue to start; workflow "
        "dispatch is retry-only.\n"
        "- `.github/foundry-optimizer.yaml` is durable repository policy; "
        "each issue supplies its own goal and assets within that boundary.\n"
        "- Normal user action: watch the root dashboard and candidate PRs, "
        "then merge exactly one eligible candidate PR.\n"
        "- Exceptional action: review and merge an immutable spec PR only "
        "when the dashboard reports new, changed, custom, synthetic, trace, "
        "human-gated, or unpinned assets.\n"
        "- Canonical state and recovery history live at "
        "`refs/heads/foundry-opt/state/issue-<N>`; issue comments and labels "
        "are projections.\n"
    )
    contents.update(
        {
            Path(".foundry-optimizer/.gitignore"): (
                "campaigns/\nworktrees/\n"
            ),
            Path(".github/ISSUE_TEMPLATE/foundry-optimization.yml"): (
                _issue_form(request)
            ),
            Path(
                ".github/agents/foundry-optimization-planner.agent.md"
            ): _planner_agent(),
            Path(
                ".github/agents/foundry-candidate-designer.agent.md"
            ): _designer_agent(),
            Path(
                ".github/agents/foundry-candidate-applier.agent.md"
            ): _applier_agent(),
            Path(
                ".github/agents/foundry-optimization-steward.agent.md"
            ): _steward_agent(),
            Path(
                ".github/workflows/foundry-optimization-issue-intake.yml"
            ): _issue_intake_workflow(request),
            Path(
                ".github/workflows/foundry-optimization-reconcile.yml"
            ): _reconciliation_workflow(request),
            Path(
                ".github/workflows/foundry-optimization-handoff.yml"
            ): _handoff_workflow(request),
            Path(
                ".github/workflows/"
                "foundry-optimization-deployment-bridge.yml"
            ): _deployment_bridge_workflow(
                request,
                deployment_workflow_name=deployment_workflow_name,
            ),
            Path(
                ".github/workflows/foundry-exact-candidate-check.yml"
            ): _candidate_check_workflow(request),
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


def _issue_form(request: OnboardingRequest) -> str:
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
                        "Creating this one issue starts the campaign. Normally "
                        "you take no action until eligible candidate PRs are "
                        "ready, then merge exactly one. An immutable spec PR "
                        "needs review only when the dashboard identifies new, "
                        "changed, custom, synthetic, trace-derived, human-gated, "
                        "or unpinned assets. Track bounded experiments, "
                        "held-out evidence, deployment, and retained improvement "
                        "in the root issue dashboard. Do not include credentials, "
                        "raw traces, or private dataset rows."
                    )
                },
            },
            _textarea(
                "target",
                "Configured target",
                "Target name from `.github/foundry-optimizer.yaml`.",
                request.target_name,
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
description: Materialize one steward-requested immutable optimization specification.
target: github-copilot
tools: ["read", "search", "edit", "execute"]
disable-model-invocation: true
---

Read `.github/skills/foundry-agent-optimizer/SKILL.md`,
`REPOSITORY_CONTEXT.md`, the assigned optimization issue, and the exact
`specialist_work_request` whose `work_kind` is `prepare_specification_pr`.

This is an exceptional human gate, not a normal phase. The steward requests it
only for new, changed, custom, synthetic, trace-derived, human-gated, or
unpinned assets, or when repository policy disables automatic spec approval.
Use the canonical specification interface to materialize only the requested
immutable specification and approved repository evaluation assets. Do not
classify policy, generate candidates, evaluate drafts, select, merge, deploy,
or expose raw trace rows. Do not call GitHub APIs or create a pull request
yourself. The pull request is opened by the native Copilot session from only
the exact requested files you commit.
"""


def _designer_agent() -> str:
    return """---
name: foundry-candidate-designer
description: Fulfil one canonical candidate design intent in its reserved worktree.
target: github-copilot
tools: ["read", "search", "edit", "execute"]
disable-model-invocation: false
---

Read `.github/skills/foundry-agent-optimizer/SKILL.md`,
`REPOSITORY_CONTEXT.md`, and the exact canonical `CandidateDesignIntent`
provided by the steward.

Edit only the reserved worktree and only its declared edit paths. Follow the
pinned goal, mutation allowlist, restricted opt-ins, baseline aggregates, and
redacted prior feedback. Return only the matching privacy-safe
`CandidateDesignResult`; never claim evaluation status, eligibility, or
selection. Write that typed result to
`.foundry-optimizer/design-results/<effect-id>.json`, then invoke exactly once:

`foundry-opt steward candidate-design-result --issue <number> --effect <effect-id> --worker-issue <worker-issue-number> --result-file .foundry-optimizer/design-results/<effect-id>.json --json`

The JSON must contain exactly the canonical result fields: `effect_id`,
`result_id`, `issue_number`, `generation`, `spec_sha256`, `base_commit`,
`candidate_id`, `slot`, `idea_id`, `mutation_class`, `parent_idea_ids`,
`required_opt_ins`, `motivation`, `lessons`, and `complexity`.

The command captures only the allowed candidate edits on the durable design
result ref and restores the session checkout. If that ref push is not
acknowledged in a Copilot cloud session, the command may create or update the
internal handoff pull request artifact at only the reserved handoff path and
never any other edit. Do not edit, annotate, or continue work in that internal
pull request; trusted transport auto-closes it. Stop immediately after the
command returns.

The campaign budget and cutoff are authoritative and must not be extended.
Never read held-out rows or raw evidence, call GitHub APIs, create a branch,
merge, or deploy. Do not create or update a pull request yourself; the internal
handoff is command-owned transport, while the later
`foundry-candidate-applier` native session owns the visible candidate pull
request.
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
candidate worker issue and its persisted `applier_worker_issue_planned`
binding. Invoke
`foundry-opt optimize apply --issue <number> --candidate <candidate-id>`.

Do not edit files directly, reinterpret the patch, repair conflicts, change the
base, merge, or deploy. The native Copilot session opens the pull request from
the exact verified result so the user can inspect its diff, redacted evidence,
and `Foundry exact candidate check`. Stop when deterministic verification
rejects the candidate.
"""


def _steward_agent() -> str:
    return """---
name: foundry-optimization-steward
description: Advance one issue-driven optimization campaign from trusted events.
target: github-copilot
tools: ["read", "search", "execute"]
disable-model-invocation: true
---

Read `.github/skills/foundry-agent-optimizer/SKILL.md`,
`REPOSITORY_CONTEXT.md`, the assigned issue, and its trusted Git-state inbox.

Run `foundry-opt steward advance --issue <number> --json` exactly once per assignment.
Resume exclusively from
`refs/heads/foundry-opt/state/issue-<number>` and its trusted inbox; never
reconstruct campaign state from comments, labels, or conversation history.
Execute domain logic only through canonical steward interfaces, never by
reproducing transitions in instructions, workflows, or shell.

The command is the only campaign action. Report and project only through the
persisted state and outbox effects it returns; transport workflows own GitHub
projection and reassignment. Never inspect or edit agent source, tests, or configuration.
If a state-ref push is not acknowledged in a Copilot cloud
session, the command may create or update the internal handoff pull request artifact
at only the reserved handoff path and never any other edit. Do not
edit, annotate, or continue work in that internal pull request; trusted
transport auto-closes it. Never create or update any pull request yourself.
Never improvise specialist work, direct code changes, state transitions,
comments, labels, merges, deployment, or other GitHub effects.
The canonical command, not this model, owns retained-improvement evaluation
and final campaign closure.

Never expose raw evidence, traces, held-out rows, credentials, or private
dataset content. Treat only privacy-allowlisted aggregates and durable
references as readable.

Handle persisted specialist intents exactly as designed:

- `specialist_work_request` delegates `prepare_specification_pr` to
  `foundry-optimization-planner`.
- `candidate_design` is fulfilled only through the canonical
  `CandidateDesignIntent` / `CandidateDesignResult` interface by
  `foundry-candidate-designer`.
- `applier_worker_issue_planned` is projected by the transport bridge to a
  worker issue assigned to `foundry-candidate-applier`.

Do not create pull requests, merge, deploy, or apply GitHub effects directly.
Native Copilot specialist sessions create their own pull requests; transport
workflows apply only effects already persisted in the outbox.

After the single command returns, stop immediately. A `blocked`, `delegate`, or `wait`
disposition, or a `waiting` status, means stop and await transport
or a new assignment. Do not continue investigating or attempt a workaround.
"""


def _issue_intake_workflow(
    request: OnboardingRequest,
) -> str:
    install = json.dumps(request.product_install)
    return f"""name: Foundry optimization issue intake

on:
  issues:
    types: [opened, edited, reopened, closed]
  pull_request:
    types: [opened, synchronize, reopened, edited, closed]
    paths-ignore:
      - .foundry-optimizer/handoffs/**

permissions:
  contents: write
  issues: write
  pull-requests: write

jobs:
  bridge:
    if: >-
      github.event_name != 'issues' ||
      github.event.action != 'opened' ||
      startsWith(github.event.issue.title, '[Optimize] ')
    runs-on: ubuntu-latest
    env:
      GH_TOKEN: ${{{{ github.token }}}}
      OPTIMIZER_PACKAGE: {install}
    steps:
      - name: Require Copilot assignment token
        env:
          COPILOT_ASSIGNMENT_TOKEN: ${{{{ secrets.COPILOT_ASSIGNMENT_TOKEN }}}}
        shell: bash
        run: |
          if [ -z "$COPILOT_ASSIGNMENT_TOKEN" ]; then
            echo "Missing required Actions secret: COPILOT_ASSIGNMENT_TOKEN" >&2
            exit 1
          fi
      - uses: {_CHECKOUT_ACTION} # v7.0.1
        with:
          fetch-depth: 0
          ref: ${{{{ github.event.repository.default_branch }}}}
      - uses: {_SETUP_UV_ACTION} # v9.0.0
      - name: Record trusted event and recover projections
        env:
          COPILOT_ASSIGNMENT_TOKEN: ${{{{ secrets.COPILOT_ASSIGNMENT_TOKEN }}}}
          TRUSTED_EVENT_NAME: ${{{{ github.event_name }}}}
          TRUSTED_EVENT_PATH: ${{{{ github.event_path }}}}
          TRUSTED_REPOSITORY: ${{{{ github.repository }}}}
          TRUSTED_REPOSITORY_ID: ${{{{ github.repository_id }}}}
          TRUSTED_RUN_ID: ${{{{ github.run_id }}}}
        run: >-
          uv run --no-project --with "$OPTIMIZER_PACKAGE"
          python -m foundry_opt.orchestration.issue_intake
"""


def _handoff_workflow(request: OnboardingRequest) -> str:
    install = json.dumps(request.product_install)
    return f"""name: Foundry internal handoff transport

on:
  pull_request_target:
    types: [opened, synchronize, reopened]
    paths:
      - .foundry-optimizer/handoffs/**

permissions:
  contents: write
  issues: write
  pull-requests: write

concurrency:
  group: foundry-internal-handoff-${{{{ github.event.pull_request.number }}}}
  cancel-in-progress: false

jobs:
  apply-handoff:
    if: >-
      github.event.pull_request.head.repo.full_name == github.repository &&
      github.event.pull_request.user.login == 'copilot-swe-agent[bot]'
    runs-on: ubuntu-latest
    env:
      GH_TOKEN: ${{{{ github.token }}}}
      OPTIMIZER_PACKAGE: {install}
    steps:
      - name: Require Copilot assignment token
        env:
          COPILOT_ASSIGNMENT_TOKEN: ${{{{ secrets.COPILOT_ASSIGNMENT_TOKEN }}}}
        shell: bash
        run: |
          if [ -z "$COPILOT_ASSIGNMENT_TOKEN" ]; then
            echo "Missing required Actions secret: COPILOT_ASSIGNMENT_TOKEN" >&2
            exit 1
          fi
      - uses: {_CHECKOUT_ACTION} # v7.0.1
        with:
          fetch-depth: 0
          ref: ${{{{ github.event.pull_request.base.sha }}}}
      - uses: {_SETUP_UV_ACTION} # v9.0.0
      - name: Validate and apply exact internal handoff
        env:
          COPILOT_ASSIGNMENT_TOKEN: ${{{{ secrets.COPILOT_ASSIGNMENT_TOKEN }}}}
          TRUSTED_DEFAULT_BRANCH: ${{{{ github.event.repository.default_branch }}}}
          TRUSTED_EVENT_NAME: ${{{{ github.event_name }}}}
          TRUSTED_EVENT_PATH: ${{{{ github.event_path }}}}
          TRUSTED_REPOSITORY: ${{{{ github.repository }}}}
          TRUSTED_REPOSITORY_ID: ${{{{ github.repository_id }}}}
          TRUSTED_RUN_ID: ${{{{ github.run_id }}}}
        run: >-
          uv run --no-project --with "$OPTIMIZER_PACKAGE"
          python -m foundry_opt.orchestration.handoff
"""


def _reconciliation_workflow(request: OnboardingRequest) -> str:
    install = json.dumps(request.product_install)
    return f"""name: Foundry optimization reconciliation

on:
  push:
    branches:
      - foundry-opt/state/issue-*
  schedule:
    - cron: "17 * * * *"
  workflow_dispatch:
    inputs:
      issue:
        description: Optional tracked optimization issue number to retry
        required: false
        type: number

permissions:
  contents: write
  issues: write
  pull-requests: write

concurrency:
  group: foundry-optimization-effects
  cancel-in-progress: false

jobs:
  reconcile:
    runs-on: ubuntu-latest
    env:
      GH_TOKEN: ${{{{ github.token }}}}
      OPTIMIZER_PACKAGE: {install}
    steps:
      - name: Require Copilot assignment token
        env:
          COPILOT_ASSIGNMENT_TOKEN: ${{{{ secrets.COPILOT_ASSIGNMENT_TOKEN }}}}
        shell: bash
        run: |
          if [ -z "$COPILOT_ASSIGNMENT_TOKEN" ]; then
            echo "Missing required Actions secret: COPILOT_ASSIGNMENT_TOKEN" >&2
            exit 1
          fi
      - uses: {_CHECKOUT_ACTION} # v7.0.1
        with:
          fetch-depth: 0
          ref: ${{{{ github.event.repository.default_branch }}}}
      - uses: {_SETUP_UV_ACTION} # v9.0.0
      - name: Reconcile trusted transport effects
        env:
          COPILOT_ASSIGNMENT_TOKEN: ${{{{ secrets.COPILOT_ASSIGNMENT_TOKEN }}}}
          TRUSTED_EVENT_NAME: ${{{{ github.event_name }}}}
          TRUSTED_EVENT_PATH: ${{{{ github.event_path }}}}
          TRUSTED_ISSUE_NUMBER: ${{{{ inputs.issue }}}}
          TRUSTED_REPOSITORY: ${{{{ github.repository }}}}
          TRUSTED_REPOSITORY_ID: ${{{{ github.repository_id }}}}
          TRUSTED_RUN_ID: ${{{{ github.run_id }}}}
          TRUSTED_STATE_REF: ${{{{ github.ref_name }}}}
        run: >-
          uv run --no-project --with "$OPTIMIZER_PACKAGE"
          python -m foundry_opt.orchestration.issue_intake
"""


def _deployment_bridge_workflow(
    request: OnboardingRequest,
    *,
    deployment_workflow_name: str,
) -> str:
    install = json.dumps(request.product_install)
    workflow_name = json.dumps(deployment_workflow_name)
    actions_environment = (
        request.mirror_actions_environment or request.environment_name
    )
    return f"""name: Foundry optimization deployment bridge

on:
  push:
    branches:
      - foundry-opt/state/issue-*
  schedule:
    - cron: "29 * * * *"
  workflow_run:
    workflows: [{workflow_name}]
    types: [completed]
  workflow_dispatch:
    inputs:
      issue:
        description: Optional tracked optimization issue number to retry
        required: false
        type: number

permissions:
  actions: write
  contents: write
  id-token: write

concurrency:
  group: >-
    foundry-optimization-deployment-${{{{
      github.event.workflow_run.id ||
      inputs.issue ||
      github.ref_name ||
      github.run_id
    }}}}
  cancel-in-progress: false

jobs:
  deployment-bridge:
    if: >-
      github.event_name != 'workflow_run' ||
      github.event.workflow_run.conclusion == 'success'
    runs-on: ubuntu-latest
    environment: {json.dumps(actions_environment)}
    env:
      GH_TOKEN: ${{{{ github.token }}}}
      AZURE_CLIENT_ID: ${{{{ vars.AZURE_DEPLOYMENT_CLIENT_ID }}}}
      AZURE_TENANT_ID: ${{{{ vars.AZURE_TENANT_ID }}}}
      AZURE_SUBSCRIPTION_ID: ${{{{ vars.AZURE_SUBSCRIPTION_ID }}}}
      DEFAULT_BRANCH: ${{{{ github.event.repository.default_branch }}}}
      OPTIMIZER_PACKAGE: {install}
    steps:
      - uses: {_CHECKOUT_ACTION} # v7.0.1
        with:
          fetch-depth: 0
          ref: ${{{{ github.event.repository.default_branch }}}}
      - uses: {_AZURE_LOGIN_ACTION} # v3.0.0
        with:
          client-id: ${{{{ vars.AZURE_DEPLOYMENT_CLIENT_ID }}}}
          tenant-id: ${{{{ vars.AZURE_TENANT_ID }}}}
          subscription-id: ${{{{ vars.AZURE_SUBSCRIPTION_ID }}}}
      - uses: {_SETUP_PYTHON_ACTION} # v7.0.0
        with:
          python-version: "3.12"
      - uses: {_SETUP_UV_ACTION} # v9.0.0
      - name: Apply persisted deployment intents
        env:
          REQUESTED_ISSUE: ${{{{ inputs.issue }}}}
          TRUSTED_EVENT_NAME: ${{{{ github.event_name }}}}
          TRUSTED_STATE_REF: ${{{{ github.ref_name }}}}
          TRUSTED_WORKFLOW_RUN_ID: ${{{{ github.event.workflow_run.id }}}}
        shell: bash
        run: |
          if [ "$TRUSTED_EVENT_NAME" = "workflow_run" ]; then
            if [[ ! "$TRUSTED_WORKFLOW_RUN_ID" =~ ^[1-9][0-9]*$ ]]; then
              echo "Invalid workflow run ID" >&2
              exit 1
            fi
            result_dir="$GITHUB_WORKSPACE/.foundry-optimizer/deployment-result"
            mkdir -p "$result_dir"
            gh run download "$TRUSTED_WORKFLOW_RUN_ID" \
              --repo "$GITHUB_REPOSITORY" \
              --name foundry-optimization-deployment-result \
              --dir "$result_dir"
            mapfile -d '' result_files < <(
              find "$result_dir" -type f -name deployment-result.json -print0
            )
            if [ "${{#result_files[@]}}" -ne 1 ]; then
              echo "Expected exactly one deployment-result.json" >&2
              exit 1
            fi
            publication_json="$(
              uv run --no-project --with "$OPTIMIZER_PACKAGE" \
                foundry-opt steward publication-result-auto \
                --result-file "${{result_files[0]}}" \
                --expected-run-id "$TRUSTED_WORKFLOW_RUN_ID"
            )"
            printf '%s\\n' "$publication_json"
            issue="$(
              PUBLICATION_JSON="$publication_json" python -c \
                'import json, os; value = json.loads(os.environ["PUBLICATION_JSON"]); issue = value.get("issue_number"); assert type(issue) is int and issue > 0; print(issue)'
            )"
            gh workflow run foundry-optimization-reconcile.yml \
              --repo "$GITHUB_REPOSITORY" \
              --ref "$DEFAULT_BRANCH" \
              -f "issue=$issue"
          elif [ -n "$REQUESTED_ISSUE" ]; then
            if [[ ! "$REQUESTED_ISSUE" =~ ^[1-9][0-9]*$ ]]; then
              echo "Invalid issue number" >&2
              exit 1
            fi
            uv run --no-project --with "$OPTIMIZER_PACKAGE" foundry-opt steward deployment-bridge --issue "$REQUESTED_ISSUE"
          else
            uv run --no-project --with "$OPTIMIZER_PACKAGE" \
              python -m foundry_opt.orchestration.deployment_bridge
          fi
"""


def _candidate_check_workflow(request: OnboardingRequest) -> str:
    install = _shell_quote(request.product_install)
    return f"""name: Foundry exact candidate check

on:
  pull_request:
    types: [opened, synchronize, reopened, edited]
    paths-ignore:
      - .foundry-optimizer/handoffs/**

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
          markers = re.findall(
              r"foundry-opt:candidate-pr:"
              r"issue-([1-9][0-9]*):"
              r"g([1-9][0-9]*):"
              r"([A-Za-z0-9][A-Za-z0-9._-]{{0,127}}):"
              r"([0-9a-f]{{20}})",
              body,
          )
          if len(markers) != 1:
              raise SystemExit("candidate metadata is missing or ambiguous")
          marker = re.search(
              r"foundry-opt:candidate-pr:"
              r"issue-([1-9][0-9]*):"
              r"g([1-9][0-9]*):"
              r"([A-Za-z0-9][A-Za-z0-9._-]{{0,127}}):"
              r"([0-9a-f]{{20}})",
              body,
          )
          assert marker is not None
          candidate = marker.group(3)
          with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
              output.write(f"candidate={{candidate}}\\n")
              output.write(f"issue={{marker.group(1)}}\\n")
      - name: Verify exact candidate metadata and tree
        env:
          CANDIDATE: ${{{{ steps.metadata.outputs.candidate }}}}
          ISSUE: ${{{{ steps.metadata.outputs.issue }}}}
        shell: bash
        run: foundry-opt optimize apply --issue "$ISSUE" --candidate "$CANDIDATE" --verify-only
"""


def legacy_repository_agent_bundle(
    request: OnboardingRequest,
    *,
    oidc_subject: str = "repository_id:legacy-placeholder",
) -> dict[Path, str]:
    """Return the exact pre-orchestration generated files for safe migration."""

    return {
        Path(
            ".github/skills/foundry-agent-optimizer/"
            "REPOSITORY_CONTEXT.md"
        ): (
            "# Repository optimization context\n\n"
            f"- Configured target: `{request.target_name}`\n"
            f"- Configured environment: `{request.environment_name}`\n"
            f"- Verified immutable OIDC subject: `{oidc_subject}`\n"
            "- Configuration: `.github/foundry-optimizer.yaml`\n"
            "- Azure authentication is OIDC-only; never request credentials.\n"
            "- GitHub Copilot receives non-secret Azure identifiers through "
            "repository-level Agents variables.\n"
        ),
        Path(".github/ISSUE_TEMPLATE/foundry-optimization.yml"): (
            _issue_form(replace(request, target_name="support-agent"))
        ),
        Path(
            ".github/agents/foundry-optimization-planner.agent.md"
        ): _legacy_planner_agent(),
        Path(
            ".github/agents/foundry-optimization-runner.agent.md"
        ): _legacy_runner_agent(),
        Path(
            ".github/agents/foundry-candidate-applier.agent.md"
        ): _legacy_applier_agent(),
        Path(
            ".github/agents/foundry-optimization-steward.agent.md"
        ): _legacy_steward_agent(),
        Path(
            ".github/workflows/foundry-optimization-issue-intake.yml"
        ): _legacy_issue_intake_workflow(request),
        Path(
            ".github/workflows/foundry-optimization-control.yml"
        ): _legacy_control_workflow(request),
        Path(
            ".github/workflows/foundry-post-deployment-check.yml"
        ): _legacy_post_deployment_workflow(request),
        Path(
            ".github/workflows/foundry-exact-candidate-check.yml"
        ): _legacy_candidate_check_workflow(request),
    }


def _legacy_planner_agent() -> str:
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


def _legacy_runner_agent() -> str:
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


def _legacy_applier_agent() -> str:
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


def _legacy_steward_agent() -> str:
    return """---
name: foundry-optimization-steward
description: Advance one issue-driven optimization campaign from trusted events.
target: github-copilot
tools: ["read", "search", "edit", "execute", "github/*", "foundry-mcp/*"]
disable-model-invocation: true
---

Read `.github/skills/foundry-agent-optimizer/SKILL.md`,
`REPOSITORY_CONTEXT.md`, the assigned issue, and its trusted Git-state inbox.

You are the sole campaign steward. Replay unprocessed inbox events through
`OptimizationCampaign.advance`; never reproduce domain transitions in a
workflow or shell script. An edited or reopened issue supersedes the prior
generation. Removing the `[Optimize] ` title prefix declassifies and cancels
the current generation. A closed issue cancels active work.

Write durable campaign state and only explicit, privacy-allowlisted outbox
records. The workflow is transport-only and may project only
`dashboard_projection`, `label_add`, and `label_remove` records that you
decided. It must not classify assets, evaluate candidates, select a candidate,
merge, or deploy. Delegate bounded work to the planner, runner, or applier only
when the state-machine disposition requires it.
"""


def _legacy_issue_intake_workflow(request: OnboardingRequest) -> str:
    install = json.dumps(request.product_install)
    return f"""name: Foundry optimization issue intake

on:
  issues:
    types: [opened, edited, reopened, closed]
  schedule:
    - cron: "17 * * * *"

permissions:
  contents: write
  issues: write

concurrency:
  group: foundry-optimization-issue-intake
  cancel-in-progress: false

jobs:
  bridge:
    if: >-
      github.event_name == 'schedule' ||
      github.event.action != 'opened' ||
      startsWith(github.event.issue.title, '[Optimize] ')
    runs-on: ubuntu-latest
    env:
      GH_TOKEN: ${{{{ github.token }}}}
      OPTIMIZER_PACKAGE: {install}
    steps:
      - uses: {_CHECKOUT_ACTION} # v7.0.1
        with:
          fetch-depth: 0
      - uses: {_SETUP_UV_ACTION} # v9.0.0
      - name: Record trusted event and recover projections
        env:
          TRUSTED_EVENT_NAME: ${{{{ github.event_name }}}}
          TRUSTED_EVENT_PATH: ${{{{ github.event_path }}}}
          TRUSTED_REPOSITORY: ${{{{ github.repository }}}}
          TRUSTED_REPOSITORY_ID: ${{{{ github.repository_id }}}}
          TRUSTED_RUN_ID: ${{{{ github.run_id }}}}
        run: >-
          uv run --no-project --with "$OPTIMIZER_PACKAGE"
          python -m foundry_opt.orchestration.issue_intake
"""


def _legacy_control_workflow(request: OnboardingRequest) -> str:
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
      AZURE_TENANT_ID: ${{{{ vars.AZURE_TENANT_ID }}}}
      AZURE_CLIENT_ID: ${{{{ vars.AZURE_CLIENT_ID }}}}
      AZURE_SUBSCRIPTION_ID: ${{{{ vars.AZURE_SUBSCRIPTION_ID }}}}
    steps:
      - uses: {_CHECKOUT_ACTION} # v7.0.1
        with:
          fetch-depth: 0
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


def _legacy_post_deployment_workflow(
    request: OnboardingRequest,
) -> str:
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


def _legacy_candidate_check_workflow(
    request: OnboardingRequest,
) -> str:
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
          campaign_match = re.search(
              r"foundry-opt:candidate-pr:issue-(\\d+):",
              body,
          )
          if candidate_match is None or campaign_match is None:
              raise SystemExit("candidate metadata is missing")
          candidate = candidate_match.group(1)
          if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{{0,127}}", candidate) is None:
              raise SystemExit("candidate identifier is invalid")
          with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
              output.write(f"candidate={{candidate}}\\n")
              output.write(f"issue={{campaign_match.group(1)}}\\n")
      - name: Verify exact candidate metadata and tree
        env:
          CANDIDATE: ${{{{ steps.metadata.outputs.candidate }}}}
          ISSUE: ${{{{ steps.metadata.outputs.issue }}}}
        shell: bash
        run: foundry-opt optimize apply --issue "$ISSUE" --candidate "$CANDIDATE" --verify-only
"""


def legacy_repository_agent_hashes(
    request: OnboardingRequest,
    *,
    oidc_subject: str,
) -> dict[Path, str]:
    contents = legacy_repository_agent_bundle(
        request,
        oidc_subject=oidc_subject,
    )
    hashes = {
        path: hashlib.sha256(content.encode("utf-8")).hexdigest()
        for path, content in contents.items()
    }
    hashes[
        Path(".github/skills/foundry-agent-optimizer/SKILL.md")
    ] = "ff0c3f9a072d5381bfd5d056efc9a0fbb27d82be319297666502a8142143e9e9"
    return hashes


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"
