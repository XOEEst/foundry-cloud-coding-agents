from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re

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
    contents[skill_root / "REPOSITORY_CONTEXT.md"] = _repository_context(
        request,
        oidc_subject=oidc_subject,
    )
    contents.update(
        {
            Path(".foundry-optimizer/.gitignore"): (
                "campaigns/\nworktrees/\ncapability-worktrees/\n"
            ),
            Path(".github/ISSUE_TEMPLATE/foundry-optimization.yml"): (
                _issue_form(request)
            ),
            Path(
                ".github/agents/foundry-optimization-steward.agent.md"
            ): _steward_agent(),
            Path(
                ".github/workflows/foundry-optimization-workspace.yml"
            ): _workspace_workflow(request),
            Path(
                ".github/workflows/foundry-optimization-operations.yml"
            ): _operations_workflow(
                request,
                deployment_workflow_name=deployment_workflow_name,
            ),
            Path(
                ".github/workflows/foundry-exact-candidate-check.yml"
            ): _candidate_check_workflow(request),
        }
    )
    return contents


def _repository_context(
    request: OnboardingRequest,
    *,
    oidc_subject: str,
) -> str:
    return (
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
        "- Preserve the customer deployment workflow; do not overwrite "
        "`.github/workflows/deploy-foundry-agent.yml`. The optimizer "
        "operations workflow observes or dispatches it and consumes its "
        "`foundry-optimization-deployment-result` artifact.\n"
        "- Foundry operations and post-deployment evaluation run only in "
        "`foundry-optimization-operations.yml` under the optimizer "
        "`AZURE_CLIENT_ID`; Copilot performs no Foundry network operations.\n"
        "- Copilot session assignment uses the repository Actions secret "
        "`COPILOT_ASSIGNMENT_TOKEN`, containing a least-privilege "
        "user-to-server token; an installation token is not supported.\n"
        "- foundry-opt init cannot create Actions secrets; configure the "
        "assignment secret manually and never commit its value.\n"
        "- Create the generated `[Optimize]` issue to start one persistent "
        "draft workspace pull request; workflow dispatch is retry-only.\n"
        "- `.github/foundry-optimizer.yaml` is durable repository policy; "
        "each issue supplies its own goal and assets within that boundary.\n"
        "- The steward compares bounded candidates internally, updates the "
        "same workspace pull request, and creates no secondary optimization "
        "branches or review surfaces.\n"
        "- The steward follows the durable workspace `next_action`; candidate "
        "actions include the exact work contract and submission command. It "
        "pauses for external operations, the human merge, a blocked state, "
        "or completion.\n"
        "- Normal user action: watch the issue and its one workspace pull "
        "request, then merge that pull request when it becomes eligible.\n"
    )


def _previous_repository_context(
    request: OnboardingRequest,
    *,
    oidc_subject: str,
) -> str:
    return (
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
        "- Asset registration, draft creation, and development evaluation "
        "run only in the generated Actions capability workflow under the "
        "optimizer `AZURE_CLIENT_ID`; Copilot performs no Foundry network "
        "operations.\n"
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


def _previous_repository_agent_bundle(
    request: OnboardingRequest,
    *,
    oidc_subject: str,
    deployment_workflow_name: str = "Foundry deployment",
) -> dict[Path, str]:
    contents = _copy_skill_template()
    contents[
        Path(".github/skills/foundry-agent-optimizer/REPOSITORY_CONTEXT.md")
    ] = _previous_repository_context(request, oidc_subject=oidc_subject)
    contents.update(
        {
            Path(".foundry-optimizer/.gitignore"): (
                "campaigns/\nworktrees/\ncapability-worktrees/\n"
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
            ): _previous_steward_agent(),
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
                ".github/workflows/foundry-optimization-capability.yml"
            ): _capability_workflow(request),
            Path(
                ".github/workflows/"
                "foundry-optimization-deployment-bridge.yml"
            ): _deployment_bridge_workflow(
                request,
                deployment_workflow_name=deployment_workflow_name,
                consolidated=False,
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
                        "Creating this one issue starts one persistent draft "
                        "workspace pull request. Normally you take no action "
                        "until that pull request is eligible, then merge it. "
                        "Track bounded internal experiments, "
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
description: Own one issue's persistent Foundry optimization workspace.
target: github-copilot
tools: ["read", "search", "edit", "execute"]
disable-model-invocation: false
---

Read `.github/skills/foundry-agent-optimizer/SKILL.md`,
`REPOSITORY_CONTEXT.md`, the assigned optimization issue, and the existing
workspace pull request for that issue.

Own exactly one persistent draft workspace pull request. Advance it only with:

`foundry-opt workspace advance --issue <number> --json`

Read the returned durable workspace state and `next_action`. Perform only the
listed action and compare bounded candidates internally through the candidate
work contract; never create another review surface. Update the same pull request
only when trusted finalization supplies the selected exact patch.

When `next_action.kind` is `run_candidate_experiments`, execute exactly one
candidate from `next_action.candidate_work`. Treat its target, base commit,
candidate ID and slot, configured limit, allowed mutation classes, prior
redacted experiment results, and command as authoritative:

1. Read the issue goal, target configuration, allowed edit paths, relevant
   source, and prior experiment results.
2. Create a disposable detached worktree at the supplied base commit. Make one
   coherent change using one supplied mutation class. Never edit or commit on
   the persistent workspace branch.
3. Run the configured validation commands.
4. Export a non-empty binary Git patch relative to the supplied base commit,
   including new files, and base64-encode it.
5. Write a schema-v3 JSON manifest containing only `schema_version`,
   `issue_number`, `target`, `base_commit`, and `candidate`. The candidate
   contains only `candidate_id`, `mutation_class`, `summary`, and
   `patch_base64`. Copy every binding value from `candidate_work`.
6. Run the exact `candidate_work.command`, currently
   `foundry-opt workspace experiment --issue <number>
   --candidate-manifest <manifest.json> --json`.
7. Remove the disposable worktree and manifest after successful submission.
   If the result says `proxy_import_required`, add only
   `.foundry-optimizer/workspace-candidate.json`, commit it on the existing
   workspace PR branch, push that branch, and stop. If it says
   `await_trusted_actions_result`, stop directly. Trusted Actions imports and
   evaluates the proposal, then a revision-bound continuation requests the
   next slot.

Do not stop merely because an internal invocation returned successfully. Stop
only when the result is waiting for an external operation, waiting for the
human merge, blocked, or complete.

Never create another issue, a handoff artifact, or a second optimization pull
request. Do not create a second optimization branch or review surface. Do not
reproduce workspace transitions in prose, shell, comments, labels, or ad hoc
files. The workspace command owns candidate bounds, evaluation, eligibility,
selection, deployment intent, and retained-improvement state.

Edit only the paths allowed by the immutable issue and repository policy.
Never expose credentials, private dataset rows, held-out cases, raw traces,
evaluator prompts, or unredacted evidence. Copilot never calls Foundry network
adapters; the optimizer-OIDC operations workflow performs persisted Foundry
operations and post-deployment evaluation. Deployment remains isolated under
the separate deployment identity.

Never invent an action absent from `next_action` or continue after an
external/human wait. Do not attempt a workaround for a blocked result.
"""


def _previous_steward_agent() -> str:
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
projection and reassignment. The capability workflow owns exact persisted
Foundry calls. Never inspect or edit agent source, tests, or configuration.
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
- `candidate_assets_registration_planned` and Foundry
  `candidate_effect_planned` records are executed only by the trusted Actions
  capability bridge; stop and wait without calling a Foundry adapter.

Do not create pull requests, merge, deploy, or apply GitHub effects directly.
Native Copilot specialist sessions create their own pull requests; transport
workflows apply only effects already persisted in the outbox.

After the single command returns, stop immediately. A `blocked`, `delegate`, or `wait`
disposition, or a `waiting` status, means stop and await transport
or a new assignment. Do not continue investigating or attempt a workaround.
"""


def _workspace_workflow(request: OnboardingRequest) -> str:
    install = json.dumps(request.product_install)
    return f"""name: Foundry optimization workspace

on:
  issues:
    types: [opened, edited, reopened, closed]
  issue_comment:
    types: [created]
  pull_request_target:
    types: [opened, synchronize, reopened, edited, ready_for_review, closed]
  schedule:
    - cron: "*/5 * * * *"
  workflow_dispatch:
    inputs:
      issue:
        description: Optimization issue number to retry
        required: true
        type: number

permissions:
  actions: write
  contents: write
  issues: write
  pull-requests: write

concurrency:
  group: foundry-optimization-workspace
  cancel-in-progress: false

jobs:
  scan-candidate-envelopes:
    if: github.event_name == 'schedule'
    runs-on: ubuntu-latest
    steps:
      - name: Dispatch trusted imports for current candidate envelopes
        env:
          GH_TOKEN: ${{{{ github.token }}}}
          TRUSTED_DEFAULT_BRANCH: ${{{{ github.event.repository.default_branch }}}}
          TRUSTED_REPOSITORY: ${{{{ github.repository }}}}
        shell: python
        run: |
          import base64
          import json
          import os
          import re
          import subprocess

          repository = os.environ["TRUSTED_REPOSITORY"]
          pages = json.loads(
              subprocess.run(
                  [
                      "gh",
                      "api",
                      "--paginate",
                      "--slurp",
                      f"repos/{{repository}}/pulls?state=open&per_page=100",
                  ],
                  check=True,
                  capture_output=True,
                  text=True,
              ).stdout
          )
          for pull_request in [
              item for page in pages for item in page
          ]:
              head = pull_request.get("head", {{}})
              base = pull_request.get("base", {{}})
              branch = head.get("ref")
              repository_data = head.get("repo") or {{}}
              match = (
                  re.fullmatch(
                      r"foundry-opt/workspace/issue-([1-9][0-9]*)",
                      branch or "",
                  )
                  if repository_data.get("full_name") == repository
                  and base.get("ref") == os.environ["TRUSTED_DEFAULT_BRANCH"]
                  else None
              )
              if match is None:
                  continue
              issue = int(match.group(1))
              head_sha = head.get("sha")
              if not isinstance(head_sha, str):
                  continue
              envelope = subprocess.run(
                  [
                      "gh",
                      "api",
                      "--method",
                      "GET",
                      (
                          f"repos/{{repository}}/contents/"
                          ".foundry-optimizer/workspace-candidate.json"
                      ),
                      "-f",
                      f"ref={{head_sha}}",
                  ],
                  check=False,
                  capture_output=True,
                  text=True,
              )
              if envelope.returncode != 0:
                  continue
              document = json.loads(envelope.stdout)
              payload = json.loads(
                  base64.b64decode(document["content"]).decode("utf-8")
              )
              expected = payload.get("expected_revision")
              state = subprocess.run(
                  [
                      "gh",
                      "api",
                      (
                          f"repos/{{repository}}/git/ref/heads/"
                          f"foundry-opt/state/issue-{{issue}}"
                      ),
                  ],
                  check=False,
                  capture_output=True,
                  text=True,
              )
              if state.returncode != 0:
                  continue
              current = json.loads(state.stdout).get("object", {{}}).get("sha")
              if current != expected:
                  continue
              subprocess.run(
                  [
                      "gh",
                      "workflow",
                      "run",
                      "foundry-optimization-workspace.yml",
                      "--repo",
                      repository,
                      "--ref",
                      os.environ["TRUSTED_DEFAULT_BRANCH"],
                      "-f",
                      f"issue={{issue}}",
                  ],
                  check=True,
              )
  advance:
    if: >-
      github.event_name != 'schedule' &&
      github.event_name == 'workflow_dispatch' ||
      (github.event_name == 'issues' &&
      (github.event.action != 'opened' ||
      startsWith(github.event.issue.title, '[Optimize] '))) ||
      (github.event_name == 'issue_comment' &&
      github.event.issue.pull_request &&
      github.event.comment.user.login == 'Copilot') ||
      (github.event_name == 'pull_request_target' &&
      github.event.pull_request.base.ref ==
      github.event.repository.default_branch &&
      github.event.pull_request.head.repo.full_name == github.repository &&
      startsWith(
      github.event.pull_request.head.ref,
      'foundry-opt/workspace/issue-'))
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
      - name: Resolve trusted workspace issue
        id: workspace
        env:
          DISPATCH_ISSUE: ${{{{ inputs.issue }}}}
          TRUSTED_EVENT_NAME: ${{{{ github.event_name }}}}
          TRUSTED_EVENT_PATH: ${{{{ github.event_path }}}}
          TRUSTED_REPOSITORY: ${{{{ github.repository }}}}
        shell: python
        run: |
          import json
          import os
          import re

          with open(
              os.environ["TRUSTED_EVENT_PATH"],
              encoding="utf-8",
          ) as stream:
              event = json.load(stream)
          event_name = os.environ["TRUSTED_EVENT_NAME"]
          if event_name == "issues":
              issue = event.get("issue", {{}}).get("number")
              pull_request = ""
          elif event_name == "issue_comment":
              issue_data = event.get("issue", {{}})
              body = issue_data.get("body") or ""
              matches = re.findall(
                  r"foundry-opt:workspace-pr:issue-([1-9][0-9]*):v1",
                  body,
              )
              if len(matches) != 1:
                  raise SystemExit(
                      "workspace pull request marker is missing or ambiguous"
                  )
              issue = int(matches[0])
              pull_request = str(issue_data.get("number") or "")
          elif event_name == "pull_request_target":
              pull_request_data = event.get("pull_request", {{}})
              body = pull_request_data.get("body") or ""
              matches = re.findall(
                  r"foundry-opt:workspace-pr:issue-([1-9][0-9]*):v1",
                  body,
              )
              if len(matches) != 1:
                  raise SystemExit(
                      "workspace pull request marker is missing or ambiguous"
                  )
              issue = int(matches[0])
              pull_request = str(pull_request_data.get("number") or "")
          elif event_name == "workflow_dispatch":
              raw_issue = os.environ.get("DISPATCH_ISSUE", "")
              issue = int(raw_issue) if re.fullmatch(
                  r"[1-9][0-9]*",
                  raw_issue,
              ) else None
              pull_request = ""
          else:
              raise SystemExit("unsupported workspace event")
          if type(issue) is not int or issue < 1:
              raise SystemExit("workspace issue number is invalid")
          with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
              output.write(f"issue={{issue}}\\n")
              output.write(f"pull_request={{pull_request}}\\n")
      - name: Ingest trusted event or retry the workspace
        env:
          COPILOT_ASSIGNMENT_TOKEN: ${{{{ secrets.COPILOT_ASSIGNMENT_TOKEN }}}}
          GH_TOKEN: ${{{{ secrets.COPILOT_ASSIGNMENT_TOKEN }}}}
          ISSUE: ${{{{ steps.workspace.outputs.issue }}}}
          TRUSTED_EVENT_NAME: ${{{{ github.event_name }}}}
          TRUSTED_EVENT_PATH: ${{{{ github.event_path }}}}
          TRUSTED_HEAD_SHA: ${{{{ github.event.pull_request.head.sha }}}}
          TRUSTED_PULL_REQUEST_NUMBER: >-
            ${{{{ steps.workspace.outputs.pull_request }}}}
          TRUSTED_REPOSITORY: ${{{{ github.repository }}}}
          TRUSTED_REPOSITORY_ID: ${{{{ github.repository_id }}}}
          TRUSTED_RUN_ID: ${{{{ github.run_id }}}}
        shell: bash
        run: |
          command=(
            uv run --no-project --no-config --no-env-file
            --with "$OPTIMIZER_PACKAGE"
            foundry-opt workspace
          )
          head_sha="$TRUSTED_HEAD_SHA"
          if (
            [ "$TRUSTED_EVENT_NAME" = "workflow_dispatch" ] ||
            [ "$TRUSTED_EVENT_NAME" = "issue_comment" ]
          ); then
            owner="${{TRUSTED_REPOSITORY%%/*}}"
            branch="foundry-opt/workspace/issue-$ISSUE"
            head_sha="$(
              gh api --method GET \
                "repos/$TRUSTED_REPOSITORY/pulls" \
                -f state=open \
                -f head="$owner:$branch" \
                --jq 'if length == 1 then .[0].head.sha else "" end'
            )"
          fi
          envelope_path=".foundry-optimizer/workspace-candidate.json"
          envelope_file="$RUNNER_TEMP/workspace-candidate-envelope.json"
          manifest_file="$RUNNER_TEMP/workspace-candidate-manifest.json"
          if (
            [[ "$head_sha" =~ ^[0-9a-f]{{40}}$ ]] &&
            git fetch --no-tags origin "$head_sha" &&
            git cat-file -e "$head_sha:$envelope_path" 2>/dev/null
          ); then
              git show "$head_sha:$envelope_path" > "$envelope_file"
              expected_revision="$(
                python - "$envelope_file" "$manifest_file" "$ISSUE" <<'PY'
          import json
          from pathlib import Path
          import re
          import sys

          envelope = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
          if (
              not isinstance(envelope, dict)
              or set(envelope) != {{
                  "expected_revision",
                  "kind",
                  "manifest",
                  "schema_version",
              }}
              or envelope["schema_version"] != 1
              or envelope["kind"] != "workspace_candidate_proposal"
              or re.fullmatch(
                  r"[0-9a-f]{{40}}",
                  envelope["expected_revision"],
              )
              is None
              or not isinstance(envelope["manifest"], dict)
              or envelope["manifest"].get("issue_number") != int(sys.argv[3])
          ):
              raise SystemExit("workspace candidate envelope is invalid")
          Path(sys.argv[2]).write_text(
              json.dumps(
                  envelope["manifest"],
                  ensure_ascii=True,
                  allow_nan=False,
                  separators=(",", ":"),
                  sort_keys=True,
              ),
              encoding="utf-8",
          )
          print(envelope["expected_revision"])
          PY
              )"
              current_revision="$(
                git ls-remote origin \
                  "refs/heads/foundry-opt/state/issue-$ISSUE" |
                  awk '{{print $1}}'
              )"
              if [ "$current_revision" != "$expected_revision" ]; then
                echo "Workspace candidate envelope is stale" >&2
                exit 1
              fi
              "${{command[@]}}" experiment \
                --issue "$ISSUE" \
                --candidate-manifest "$manifest_file" \
                --json
              exit 0
          fi
          if (
            [ "$TRUSTED_EVENT_NAME" = "workflow_dispatch" ] ||
            [ "$TRUSTED_EVENT_NAME" = "issue_comment" ]
          ); then
            "${{command[@]}}" advance --issue "$ISSUE" --json
          else
            args=(
              intake
              --event-path "$TRUSTED_EVENT_PATH"
              --event-name "$TRUSTED_EVENT_NAME"
              --delivery-id "$TRUSTED_RUN_ID"
              --repository "$TRUSTED_REPOSITORY"
              --repository-id "$TRUSTED_REPOSITORY_ID"
              --json
            )
            if [ "$TRUSTED_EVENT_NAME" = "issues" ]; then
              args+=(--base-commit "$(git rev-parse HEAD)")
            fi
            "${{command[@]}}" "${{args[@]}}"
          fi
      - name: Dispatch trusted workspace operations
        env:
          DEFAULT_BRANCH: ${{{{ github.event.repository.default_branch }}}}
          ISSUE: ${{{{ steps.workspace.outputs.issue }}}}
        shell: bash
        run: >-
          gh workflow run foundry-optimization-operations.yml
          --repo "$GITHUB_REPOSITORY"
          --ref "$DEFAULT_BRANCH"
          -f "issue=$ISSUE"
"""


def _operations_workflow(
    request: OnboardingRequest,
    *,
    deployment_workflow_name: str,
) -> str:
    install = json.dumps(request.product_install)
    workflow_name = json.dumps(deployment_workflow_name)
    actions_environment = (
        request.mirror_actions_environment or request.environment_name
    )
    return f"""name: Foundry optimization operations

on:
  schedule:
    - cron: "*/5 * * * *"
  workflow_run:
    workflows: [{workflow_name}]
    types: [completed]
  workflow_dispatch:
    inputs:
      issue:
        description: Optional optimization issue number to retry
        required: false
        type: number

permissions:
  actions: write
  checks: write
  contents: write
  id-token: write
  issues: write
  pull-requests: write

concurrency:
  group: >-
    foundry-optimization-operations-${{{{
      github.event.workflow_run.id ||
      inputs.issue ||
      github.ref_name ||
      github.run_id
    }}}}
  cancel-in-progress: false

jobs:
  operate:
    if: >-
      github.event_name != 'workflow_run' ||
      github.event.workflow_run.conclusion == 'success'
    runs-on: ubuntu-latest
    environment: {json.dumps(actions_environment)}
    env:
      GH_TOKEN: ${{{{ github.token }}}}
      AZURE_CLIENT_ID: ${{{{ vars.AZURE_CLIENT_ID }}}}
      AZURE_TENANT_ID: ${{{{ vars.AZURE_TENANT_ID }}}}
      AZURE_SUBSCRIPTION_ID: ${{{{ vars.AZURE_SUBSCRIPTION_ID }}}}
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
      - uses: {_AZURE_LOGIN_ACTION} # v3.0.0
        with:
          client-id: ${{{{ vars.AZURE_CLIENT_ID }}}}
          tenant-id: ${{{{ vars.AZURE_TENANT_ID }}}}
          subscription-id: ${{{{ vars.AZURE_SUBSCRIPTION_ID }}}}
      - uses: {_SETUP_PYTHON_ACTION} # v7.0.0
        with:
          python-version: "3.12"
      - uses: {_SETUP_UV_ACTION} # v9.0.0
      - name: Execute trusted workspace operations
        env:
          GH_TOKEN: ${{{{ secrets.COPILOT_ASSIGNMENT_TOKEN }}}}
          REQUESTED_ISSUE: ${{{{ inputs.issue }}}}
          TRUSTED_EVENT_NAME: ${{{{ github.event_name }}}}
          TRUSTED_REPOSITORY: ${{{{ github.repository }}}}
          TRUSTED_REPOSITORY_ID: ${{{{ github.repository_id }}}}
          TRUSTED_STATE_REF: ${{{{ github.ref_name }}}}
          TRUSTED_WORKFLOW_RUN_ID: ${{{{ github.event.workflow_run.id }}}}
          WORKSPACE_RESUME_FILE: >-
            ${{{{ github.workspace }}}}/.foundry-optimizer/workspace-resume.ndjson
        shell: bash
        run: |
          mkdir -p "$(dirname "$WORKSPACE_RESUME_FILE")"
          : > "$WORKSPACE_RESUME_FILE"
          issues=()
          if [ -n "$REQUESTED_ISSUE" ]; then
            if [[ ! "$REQUESTED_ISSUE" =~ ^[1-9][0-9]*$ ]]; then
              echo "Invalid issue number" >&2
              exit 1
            fi
            issues=("$REQUESTED_ISSUE")
          elif [ "$TRUSTED_EVENT_NAME" = "push" ] && \
            [[ "$TRUSTED_STATE_REF" =~ ^foundry-opt/state/issue-([1-9][0-9]*)$ ]]; then
            issues=("${{BASH_REMATCH[1]}}")
          elif [ "$TRUSTED_EVENT_NAME" != "workflow_run" ]; then
            mapfile -t issues < <(
              git ls-remote --heads origin \
                'refs/heads/foundry-opt/state/issue-*' |
              sed -n \
                's#.*refs/heads/foundry-opt/state/issue-\\([1-9][0-9]*\\)$#\\1#p' |
              sort -n -u |
              head -25
            )
          fi
          for issue in "${{issues[@]}}"; do
            args=(
              foundry-opt workspace operations execute
              --issue "$issue"
              --event-name "$TRUSTED_EVENT_NAME"
              --repository "$TRUSTED_REPOSITORY"
              --repository-id "$TRUSTED_REPOSITORY_ID"
              --json
            )
            if [ "$TRUSTED_EVENT_NAME" = "push" ] && \
              [ -n "$TRUSTED_STATE_REF" ]; then
              args+=(--state-ref "$TRUSTED_STATE_REF")
            fi
            if [[ "$TRUSTED_WORKFLOW_RUN_ID" =~ ^[1-9][0-9]*$ ]]; then
              args+=(--workflow-run-id "$TRUSTED_WORKFLOW_RUN_ID")
            fi
            result_json="$(
              uv run --no-project --no-config --no-env-file \
                --with "$OPTIMIZER_PACKAGE" \
                "${{args[@]}}"
            )"
            printf '%s\\n' "$result_json"
            printf '%s\\n' "$result_json" >> "$WORKSPACE_RESUME_FILE"
          done
      - name: Reconcile authenticated deployment result
        if: github.event_name == 'workflow_run'
        env:
          TRUSTED_REPOSITORY: ${{{{ github.repository }}}}
          TRUSTED_REPOSITORY_ID: ${{{{ github.repository_id }}}}
          TRUSTED_WORKFLOW_RUN_ID: ${{{{ github.event.workflow_run.id }}}}
          WORKSPACE_RESUME_FILE: >-
            ${{{{ github.workspace }}}}/.foundry-optimizer/workspace-resume.ndjson
        shell: bash
        run: |
          if [[ ! "$TRUSTED_WORKFLOW_RUN_ID" =~ ^[1-9][0-9]*$ ]]; then
            echo "Invalid workflow run ID" >&2
            exit 1
          fi
          mkdir -p "$(dirname "$WORKSPACE_RESUME_FILE")"
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
          issue="$(
            RESULT_FILE="${{result_files[0]}}" python -c \
              "import json, os; value = json.load(open(os.environ['RESULT_FILE'], encoding='utf-8')); issue = value.get('issue_number'); assert type(issue) is int and issue > 0; print(issue)"
          )"
          result_json="$(
            uv run --no-project --no-config --no-env-file \
              --with "$OPTIMIZER_PACKAGE" \
              foundry-opt workspace operations reconcile \
              --issue "$issue" \
              --result "${{result_files[0]}}" \
              --repository "$TRUSTED_REPOSITORY" \
              --repository-id "$TRUSTED_REPOSITORY_ID" \
              --run-id "$TRUSTED_WORKFLOW_RUN_ID" \
              --artifact-name foundry-optimization-deployment-result \
              --json
          )"
          printf '%s\\n' "$result_json"
          printf '%s\\n' "$result_json" >> "$WORKSPACE_RESUME_FILE"
      - name: Publish trusted exact verification check and ready finalized workspace pull request
        env:
          OPTIMIZER_PACKAGE: {install}
          TRUSTED_REPOSITORY: ${{{{ github.repository }}}}
          WORKSPACE_RESUME_FILE: >-
            ${{{{ github.workspace }}}}/.foundry-optimizer/workspace-resume.ndjson
        shell: python
        run: |
          from datetime import datetime, timezone
          import json
          import os
          import re
          import subprocess
          import sys
          from pathlib import Path

          results_path = Path(os.environ["WORKSPACE_RESUME_FILE"])
          if not results_path.is_file():
              raise SystemExit(0)

          entries: dict[tuple[int, str], dict[str, object]] = {{}}
          for line in results_path.read_text(encoding="utf-8").splitlines():
              if not line.strip():
                  continue
              document = json.loads(line)
              verification = document.get("verification")
              if verification is None:
                  continue
              if not isinstance(verification, dict):
                  raise SystemExit("workspace verification payload is invalid")
              issue = verification.get("issue_number")
              pull_request = verification.get(
                  "workspace_pull_request_number"
              )
              candidate = verification.get("candidate_id")
              check_name = verification.get("check_name")
              if (
                  type(issue) is not int
                  or issue < 1
                  or type(pull_request) is not int
                  or pull_request < 1
                  or not isinstance(candidate, str)
                  or not re.fullmatch(
                      r"[A-Za-z0-9][A-Za-z0-9._-]{{0,127}}",
                      candidate,
                  )
                  or not isinstance(check_name, str)
                  or not check_name
              ):
                  raise SystemExit("workspace verification payload is invalid")
              entries[(pull_request, check_name)] = {{
                  "candidate_id": candidate,
                  "issue_number": issue,
              }}

          repository = os.environ["TRUSTED_REPOSITORY"]
          package = os.environ["OPTIMIZER_PACKAGE"]
          failure = False
          for (pull_request, check_name), entry in sorted(entries.items()):
              pull_request_view = json.loads(
                  subprocess.run(
                      [
                          "gh",
                          "pr",
                          "view",
                          str(pull_request),
                          "--repo",
                          repository,
                          "--json",
                          "number,headRefOid,isDraft,state",
                      ],
                      check=True,
                      capture_output=True,
                      text=True,
                  ).stdout
              )
              head_sha = pull_request_view.get("headRefOid")
              if (
                  not isinstance(pull_request_view, dict)
                  or pull_request_view.get("number") != pull_request
                  or pull_request_view.get("state") != "OPEN"
                  or not isinstance(pull_request_view.get("isDraft"), bool)
                  or not isinstance(head_sha, str)
                  or re.fullmatch(r"[0-9a-f]{{40}}", head_sha) is None
              ):
                  raise SystemExit(
                      "workspace pull request verification target is invalid"
                  )

              verify = subprocess.run(
                  [
                      "uv",
                      "run",
                      "--no-project",
                      "--no-config",
                      "--no-env-file",
                      "--with",
                      package,
                      "foundry-opt",
                      "workspace",
                      "verify",
                      "--issue",
                      str(entry["issue_number"]),
                      "--candidate",
                      str(entry["candidate_id"]),
                      "--pull-request",
                      str(pull_request),
                      "--head-sha",
                      head_sha,
                      "--json",
                  ],
                  check=False,
                  capture_output=True,
                  text=True,
              )
              if verify.stdout:
                  print(verify.stdout, end="")
              if verify.stderr:
                  print(verify.stderr, end="", file=sys.stderr)

              summary = (
                  "## Trusted workspace verification\\n\\n"
                  "Trusted verification failed before a summary could be "
                  "produced.\\n"
              )
              if verify.stdout.strip():
                  try:
                      verify_document = json.loads(verify.stdout)
                  except json.JSONDecodeError:
                      verify_document = None
                  if isinstance(verify_document, dict):
                      value = verify_document.get("summary_markdown")
                      if isinstance(value, str) and value.strip():
                          summary = value
              if verify.returncode != 0:
                  details = verify.stderr.strip() or verify.stdout.strip()
                  if details:
                      summary = (
                          f"{{summary}}\\n"
                          "```text\\n"
                          f"{{details[:4000]}}\\n"
                          "```\\n"
                      )

              external_id = (
                  "foundry-opt:workspace-verify:"
                  f"issue-{{entry['issue_number']}}:"
                  f"pr-{{pull_request}}:"
                  f"{{check_name}}"
              )
              existing_runs = json.loads(
                  subprocess.run(
                      [
                          "gh",
                          "api",
                          f"repos/{{repository}}/commits/{{head_sha}}/check-runs",
                      ],
                      check=True,
                      capture_output=True,
                      text=True,
                  ).stdout
              )
              check_run_id = None
              if not isinstance(existing_runs, dict) or not isinstance(
                  existing_runs.get("check_runs"),
                  list,
              ):
                  raise SystemExit(
                      "workspace verification check-runs response is invalid"
                  )
              for check_run in existing_runs["check_runs"]:
                  if (
                      isinstance(check_run, dict)
                      and check_run.get("name") == check_name
                      and check_run.get("external_id") == external_id
                      and type(check_run.get("id")) is int
                      and check_run["id"] > 0
                  ):
                      check_run_id = check_run["id"]
                      break

              timestamp = (
                  datetime.now(timezone.utc)
                  .replace(microsecond=0)
                  .isoformat()
                  .replace("+00:00", "Z")
              )
              payload = {{
                  "completed_at": timestamp,
                  "conclusion": (
                      "success" if verify.returncode == 0 else "failure"
                  ),
                  "external_id": external_id,
                  "name": check_name,
                  "output": {{
                      "summary": summary,
                      "title": "Foundry exact candidate check",
                  }},
                  "status": "completed",
              }}
              if check_run_id is None:
                  payload["head_sha"] = head_sha
                  subprocess.run(
                      [
                          "gh",
                          "api",
                          f"repos/{{repository}}/check-runs",
                          "--method",
                          "POST",
                          "--input",
                          "-",
                      ],
                      check=True,
                      capture_output=True,
                      text=True,
                      input=json.dumps(payload),
                  )
              else:
                  subprocess.run(
                      [
                          "gh",
                          "api",
                          f"repos/{{repository}}/check-runs/{{check_run_id}}",
                          "--method",
                          "PATCH",
                          "--input",
                          "-",
                      ],
                      check=True,
                      capture_output=True,
                      text=True,
                      input=json.dumps(payload),
                  )

              if verify.returncode == 0:
                  if pull_request_view["isDraft"]:
                      subprocess.run(
                          [
                              "gh",
                              "pr",
                              "ready",
                              str(pull_request),
                              "--repo",
                              repository,
                          ],
                          check=True,
                          capture_output=True,
                          text=True,
                      )
              else:
                  failure = True

          if failure:
              raise SystemExit(1)
      - name: Resume same workspace pull request when trusted state needs Copilot
        env:
          COPILOT_ASSIGNMENT_TOKEN: ${{{{ secrets.COPILOT_ASSIGNMENT_TOKEN }}}}
          GH_TOKEN: ""
          OPTIMIZER_PACKAGE: {install}
          WORKSPACE_RESUME_FILE: >-
            ${{{{ github.workspace }}}}/.foundry-optimizer/workspace-resume.ndjson
        shell: python
        run: |
          import json
          import os
          import subprocess
          import sys
          from pathlib import Path

          resume_path = Path(os.environ["WORKSPACE_RESUME_FILE"])
          if not resume_path.is_file():
              raise SystemExit(0)
          if not os.environ.get("COPILOT_ASSIGNMENT_TOKEN"):
              raise SystemExit(
                  "Missing required Actions secret: COPILOT_ASSIGNMENT_TOKEN"
              )

          entries: set[int] = set()
          for line in resume_path.read_text(encoding="utf-8").splitlines():
              if not line.strip():
                  continue
              document = json.loads(line)
              issue = document.get("issue_number")
              if type(issue) is not int or issue < 1:
                  raise SystemExit("workspace resume payload is invalid")
              entries.add(issue)
              resume = document.get("resume")
              if resume is None:
                  continue
              if not isinstance(resume, dict):
                  raise SystemExit("workspace resume payload is invalid")
              pull_request = resume.get("workspace_pull_request_number")
              if (
                  type(pull_request) is not int
                  or pull_request < 1
              ):
                  raise SystemExit("workspace resume payload is invalid")

          package = os.environ["OPTIMIZER_PACKAGE"]
          environment = dict(os.environ)
          environment["GH_TOKEN"] = environment["COPILOT_ASSIGNMENT_TOKEN"]
          for issue in sorted(entries):
              result = subprocess.run(
                  [
                      "uv",
                      "run",
                      "--no-project",
                      "--no-config",
                      "--no-env-file",
                      "--with",
                      package,
                      "foundry-opt",
                      "workspace",
                      "assign",
                      "--issue",
                      str(issue),
                      "--json",
                  ],
                  check=False,
                  capture_output=True,
                  text=True,
                  env=environment,
              )
              if result.stdout:
                  print(result.stdout, end="")
              if result.stderr:
                  print(result.stderr, end="", file=sys.stderr)
              if result.returncode != 0:
                  raise SystemExit(result.returncode)
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
          uv run --no-project --no-config --no-env-file
          --with "$OPTIMIZER_PACKAGE"
          python -m foundry_opt.orchestration.issue_intake
"""


def _handoff_workflow(request: OnboardingRequest) -> str:
    install = json.dumps(request.product_install)
    trusted_product_commit = json.dumps(
        _product_commit_from_install(request.product_install) or ""
    )
    return f"""name: Foundry internal handoff transport

on:
  pull_request_target:
    types: [opened, synchronize, reopened]
    paths:
      - .foundry-optimizer/handoffs/**
  schedule:
    - cron: "*/5 * * * *"
  workflow_dispatch:
    inputs:
      pull_request:
        description: Optional internal handoff pull request number to retry
        required: false
        type: number

permissions:
  contents: write
  issues: write
  pull-requests: write

concurrency:
  group: foundry-internal-handoff
  cancel-in-progress: false

jobs:
  apply-handoff:
    if: >-
      github.event_name != 'pull_request_target' ||
      (github.event.pull_request.base.ref ==
      github.event.repository.default_branch &&
      github.event.pull_request.head.repo.full_name == github.repository &&
      (github.event.pull_request.user.login == 'copilot-swe-agent[bot]' ||
      (github.event.pull_request.user.login == 'Copilot' &&
      github.event.pull_request.user.id == 198982749 &&
      github.event.pull_request.user.type == 'Bot')) &&
      (github.event.sender.login == 'copilot-swe-agent[bot]' ||
      (github.event.sender.login == 'Copilot' &&
      github.event.sender.id == 198982749 &&
      github.event.sender.type == 'Bot')))
    runs-on: ubuntu-latest
    env:
      GH_TOKEN: ${{{{ github.token }}}}
      OPTIMIZER_PACKAGE: {install}
      TRUSTED_HANDOFF_PRODUCT_COMMITS: {trusted_product_commit}
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
      - name: Validate and apply exact internal handoff
        env:
          COPILOT_ASSIGNMENT_TOKEN: ${{{{ secrets.COPILOT_ASSIGNMENT_TOKEN }}}}
          TRUSTED_DEFAULT_BRANCH: ${{{{ github.event.repository.default_branch }}}}
          TRUSTED_EVENT_NAME: ${{{{ github.event_name }}}}
          TRUSTED_EVENT_PATH: ${{{{ github.event_path }}}}
          TRUSTED_PULL_REQUEST_NUMBER: ${{{{ inputs.pull_request }}}}
          TRUSTED_REPOSITORY: ${{{{ github.repository }}}}
          TRUSTED_REPOSITORY_ID: ${{{{ github.repository_id }}}}
          TRUSTED_RUN_ID: ${{{{ github.run_id }}}}
        run: >-
          uv run --no-project --no-config --no-env-file
          --with "$OPTIMIZER_PACKAGE"
          python -m foundry_opt.orchestration.handoff
"""


def _product_commit_from_install(install: str) -> str | None:
    match = re.search(r"@([0-9a-f]{40})$", install)
    return match.group(1) if match is not None else None


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
          uv run --no-project --no-config --no-env-file
          --with "$OPTIMIZER_PACKAGE"
          python -m foundry_opt.orchestration.issue_intake
"""


def _capability_workflow(request: OnboardingRequest) -> str:
    install = json.dumps(request.product_install)
    actions_environment = (
        request.mirror_actions_environment or request.environment_name
    )
    return f"""name: Foundry optimization capability bridge

on:
  push:
    branches:
      - foundry-opt/state/issue-*
  schedule:
    - cron: "*/5 * * * *"
  workflow_dispatch:
    inputs:
      issue:
        description: Optional tracked optimization issue number to retry
        required: false
        type: number

permissions:
  contents: write
  id-token: write
  issues: write

concurrency:
  group: foundry-optimization-capability
  cancel-in-progress: false

jobs:
  execute-capability:
    runs-on: ubuntu-latest
    environment: {json.dumps(actions_environment)}
    env:
      GH_TOKEN: ${{{{ github.token }}}}
      AZURE_CLIENT_ID: ${{{{ vars.AZURE_CLIENT_ID }}}}
      AZURE_TENANT_ID: ${{{{ vars.AZURE_TENANT_ID }}}}
      AZURE_SUBSCRIPTION_ID: ${{{{ vars.AZURE_SUBSCRIPTION_ID }}}}
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
      - uses: {_AZURE_LOGIN_ACTION} # v3.0.0
        with:
          client-id: ${{{{ vars.AZURE_CLIENT_ID }}}}
          tenant-id: ${{{{ vars.AZURE_TENANT_ID }}}}
          subscription-id: ${{{{ vars.AZURE_SUBSCRIPTION_ID }}}}
      - uses: {_SETUP_PYTHON_ACTION} # v7.0.0
        with:
          python-version: "3.12"
      - uses: {_SETUP_UV_ACTION} # v9.0.0
      - name: Execute exact persisted Foundry capabilities
        env:
          COPILOT_ASSIGNMENT_TOKEN: ${{{{ secrets.COPILOT_ASSIGNMENT_TOKEN }}}}
          REQUESTED_ISSUE: ${{{{ inputs.issue }}}}
          TRUSTED_REPOSITORY: ${{{{ github.repository }}}}
          TRUSTED_STATE_REF: ${{{{ github.ref_name }}}}
        run: >-
          uv run --no-project --no-config --no-env-file
          --with "$OPTIMIZER_PACKAGE"
          python -m foundry_opt.orchestration.capability_bridge
"""


def _deployment_bridge_workflow(
    request: OnboardingRequest,
    *,
    deployment_workflow_name: str,
    consolidated: bool = True,
) -> str:
    install = json.dumps(request.product_install)
    workflow_name = json.dumps(deployment_workflow_name)
    display_name = (
        "Deploy Foundry agent"
        if consolidated
        else "Foundry optimization deployment bridge"
    )
    default_branch_env = (
        ""
        if consolidated
        else (
            "      DEFAULT_BRANCH: "
            "${{ github.event.repository.default_branch }}\n"
        )
    )
    reconcile = (
        '            printf \'Recorded deployment publication for issue '
        '%s\\n\' "$issue"\n'
        if consolidated
        else (
            "            gh workflow run "
            "foundry-optimization-reconcile.yml               "
            '--repo "$GITHUB_REPOSITORY"               '
            '--ref "$DEFAULT_BRANCH"               '
            '-f "issue=$issue"\n'
        )
    )
    actions_environment = (
        request.mirror_actions_environment or request.environment_name
    )
    return f"""name: {display_name}

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
{default_branch_env}\
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
              uv run --no-project --no-config --no-env-file \
                --with "$OPTIMIZER_PACKAGE" \
                foundry-opt steward publication-result-auto \
                --result-file "${{result_files[0]}}" \
                --expected-run-id "$TRUSTED_WORKFLOW_RUN_ID"
            )"
            printf '%s\\n' "$publication_json"
            issue="$(
              PUBLICATION_JSON="$publication_json" python -c \
                'import json, os; value = json.loads(os.environ["PUBLICATION_JSON"]); issue = value.get("issue_number"); assert type(issue) is int and issue > 0; print(issue)'
            )"
{reconcile}\
          elif [ -n "$REQUESTED_ISSUE" ]; then
            if [[ ! "$REQUESTED_ISSUE" =~ ^[1-9][0-9]*$ ]]; then
              echo "Invalid issue number" >&2
              exit 1
            fi
            uv run --no-project --no-config --no-env-file \
              --with "$OPTIMIZER_PACKAGE" \
              foundry-opt steward deployment-bridge \
              --issue "$REQUESTED_ISSUE"
          else
            uv run --no-project --no-config --no-env-file \
              --with "$OPTIMIZER_PACKAGE" \
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
      - name: Verify trusted workspace candidate and publish required-check summary
        env:
          CANDIDATE: ${{{{ steps.metadata.outputs.candidate }}}}
          ISSUE: ${{{{ steps.metadata.outputs.issue }}}}
          PULL_REQUEST_NUMBER: ${{{{ github.event.pull_request.number }}}}
          PULL_REQUEST_HEAD_SHA: ${{{{ github.event.pull_request.head.sha }}}}
          VERIFY_JSON_PATH: >-
            ${{{{ github.workspace }}}}/.foundry-optimizer/workspace-verify.json
        shell: python
        run: |
          import json
          import os
          from pathlib import Path
          import subprocess
          import sys

          verify_json_path = Path(os.environ["VERIFY_JSON_PATH"])
          verify_json_path.parent.mkdir(parents=True, exist_ok=True)
          command = [
              "foundry-opt",
              "workspace",
              "verify",
              "--issue",
              os.environ["ISSUE"],
              "--candidate",
              os.environ["CANDIDATE"],
              "--pull-request",
              os.environ["PULL_REQUEST_NUMBER"],
              "--head-sha",
              os.environ["PULL_REQUEST_HEAD_SHA"],
              "--json",
          ]
          completed = subprocess.run(
              command,
              check=False,
              capture_output=True,
              text=True,
          )
          if completed.stdout:
              print(completed.stdout, end="")
              verify_json_path.write_text(
                  completed.stdout,
                  encoding="utf-8",
              )
              document = json.loads(completed.stdout)
              summary = document.get("summary_markdown")
              if not isinstance(summary, str) or not summary.strip():
                  raise SystemExit("workspace verify summary is invalid")
              with open(
                  os.environ["GITHUB_STEP_SUMMARY"],
                  "a",
                  encoding="utf-8",
              ) as output:
                  output.write(summary)
                  if not summary.endswith("\\n"):
                      output.write("\\n")
          else:
              with open(
                  os.environ["GITHUB_STEP_SUMMARY"],
                  "a",
                  encoding="utf-8",
              ) as output:
                  output.write(
                      "## Trusted workspace verification\\n\\n"
                      "No trusted workspace verify JSON was produced.\\n"
                  )
          if completed.stderr:
              print(completed.stderr, end="", file=sys.stderr)
          raise SystemExit(completed.returncode)
"""


def _historical_repository_agent_bundle(
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


def legacy_repository_agent_bundle(
    request: OnboardingRequest,
    *,
    oidc_subject: str = "repository_id:legacy-placeholder",
    deployment_workflow_name: str = "Foundry deployment",
) -> dict[Path, str]:
    """Return prior generated files and exact content for safe migration."""

    contents = _historical_repository_agent_bundle(
        request,
        oidc_subject=oidc_subject,
    )
    contents.update(
        _previous_repository_agent_bundle(
            request,
            oidc_subject=oidc_subject,
            deployment_workflow_name=deployment_workflow_name,
        )
    )
    return contents


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
    deployment_workflow_name: str = "Foundry deployment",
) -> dict[Path, str]:
    contents = legacy_repository_agent_bundle(
        request,
        oidc_subject=oidc_subject,
        deployment_workflow_name=deployment_workflow_name,
    )
    hashes = {
        path: hashlib.sha256(content.encode("utf-8")).hexdigest()
        for path, content in contents.items()
    }
    hashes[
        Path(".github/skills/foundry-agent-optimizer/SKILL.md")
    ] = "ff0c3f9a072d5381bfd5d056efc9a0fbb27d82be319297666502a8142143e9e9"
    hashes[Path(".github/workflows/campaign-drafts.yml")] = (
        "5823847aa2c124c5865742b5ae1e041af3bec882b2b6a9cf13269e3468adcd23"
    )
    hashes[Path(".github/workflows/campaign-evaluate.yml")] = (
        "c41bf79ee88c750b686d72c90e5fd892c5dbd69f18a95465e1464dd94d4879c6"
    )
    return hashes


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"
