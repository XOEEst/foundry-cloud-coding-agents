# Foundry Cloud Coding Agent

`foundry-opt` is a Python 3.12 CLI for producing reviewable, evidence-backed
Microsoft Foundry agent improvements without changing production traffic while
it experiments.

## User journey

### 1. Onboard the repository once

Run `foundry-opt init` and merge its generated onboarding PR. The command
discovers the local Python agent, published Foundry agent versions, deployed
models, development and held-out datasets, evaluators, validation commands, and
deployment workflow. It generates:

- `.github/foundry-optimizer.yaml`
- the `[Optimize]` issue form
- the Copilot steward, planner, candidate-designer, and exact-patch-applier
  custom agents
- transport, reconciliation, candidate-check, and deployment-bridge workflows
- the `foundry-agent-optimizer` skill and its vendored Tenzing protocol snapshot

Use `foundry-opt init --set-github-variables` to create the repository-level
GitHub Agents variables `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, and
`AZURE_SUBSCRIPTION_ID`. These are non-secret OIDC identifiers. Add
`--mirror-actions-environment <environment>` to create the same values plus the
separate `AZURE_DEPLOYMENT_CLIENT_ID` in an existing Actions environment.
Existing equal values are left unchanged; differing values fail closed unless
`--update-github-variables` is also supplied.

The optimizer and deployment client IDs must be different. Copilot receives
only the optimizer identity. Deployment runs only through the Actions
environment and its deployment identity. No Azure client secret is required.

### 2. Create one optimization issue

Create one issue from the generated `[Optimize]` form. That is the sole normal
campaign initiation action. Do not launch phases, add a start label, dispatch a
workflow, or create a campaign branch.

The issue defines this optimization's:

- measurable goal and behavior that must remain unchanged
- development and held-out datasets
- evaluators, metric direction, threshold, materiality, and hard guardrails
- requested mutation classes
- candidate and deployment decision modes

Goals can therefore vary per issue: one issue might improve policy coverage,
another latency, tool-call accuracy, retrieval quality, cost, or a safety
metric, provided the repository policy permits its assets and mutations.

### 3. Let the steward run the campaign

Normally, take no action while the steward classifies the specification,
establishes a baseline, runs bounded candidate experiments in isolated
worktrees, creates Foundry drafts, evaluates development and held-out results,
and publishes eligible exact patches.

There is one exceptional human gate. The root issue dashboard asks for review
and merge of an immutable specification PR when the campaign introduces or
depends on new bytes, custom, synthetic, or trace-derived assets, changed
repository content, unpinned assets, or policy that disables automatic spec
approval. Existing immutable, policy-approved assets proceed without that PR.

### 4. Inspect evidence and merge exactly one candidate

The root issue contains one continuously updated **Foundry optimization
dashboard**:

- during specification: classification, reason, and specification digest
- during experiments: phase, status, and generation
- at candidate selection: baseline aggregates and a ranked candidate table
  with aggregate metrics, deltas, guardrails, and redacted-evidence links
- during deployment: selected candidate, merge commit, and deployment state
- at completion: published Foundry version, workflow run, Foundry portal link,
  lineage and artifact hashes, required checks, and baseline/draft/deployed
  aggregates

Each candidate PR contains the exact evaluated patch. Its
**Foundry exact candidate check** verifies the campaign issue, generation, base
commit, patch, evidence, changed paths, and resulting Git tree. Review the diff,
the check, and the linked redacted evidence, then merge exactly one eligible
candidate PR. A comment, label, or CLI command does not select a candidate.

### 5. Receive the deployed and verified improvement

The merge is the normal selection signal. The steward automatically binds the
selected lineage, supersedes competing candidate issues and PRs, dispatches the
separate deployment workflow, observes its run and published Foundry version,
re-evaluates that deployed version on held-out data, updates the final
dashboard, and closes the root issue only when the improvement is retained and
all hard guardrails pass.

The completed issue and candidate PRs are the user-facing history. The private
`refs/heads/foundry-opt/state/issue-<N>` ref is the canonical, hash-chained
campaign history used for replay and recovery; comments, labels, and
conversation history are projections rather than authority.

## Policy versus an issue

`.github/foundry-optimizer.yaml` is the durable repository boundary. It records
configured environments and targets, editable paths, allowed mutation classes,
asset-source policy, campaign limits and defaults, pinned base versions,
validation commands, metric defaults, required checks, deployment workflow,
and automation permissions.

An issue cannot widen that boundary. It chooses one configured target and
requests a goal, assets, metrics, and mutations within it. During onboarding,
discovery matches one local Python agent to one Foundry agent, identifies
published numeric versions, infers or validates development and held-out
dataset roles, identifies an optimization evaluator, and verifies one
deployment workflow. During each campaign, the steward resolves every
issue-requested asset, records immutable provenance, and classifies the spec as
either `policy_approved` or `human_review`.

## Architecture and trust boundaries

- The `foundry-optimization-steward` owns the domain state machine and every
  campaign transition.
- `refs/heads/foundry-opt/state/issue-<N>` is the authority for state, inbox,
  outbox, replay, and session replacement.
- GitHub Actions are transport and capability only: they record trusted events,
  project persisted effects, reconcile inactivity, and dispatch already
  authorized deployment intents.
- Pull requests are opened natively by Copilot planner and applier sessions,
  not by Actions or direct GitHub API calls, to comply with enterprise policy.
- The planner materializes exceptional immutable spec PRs; the designer edits
  only reserved worktrees; the applier applies one exact patch without repair.
- Deployment uses a separate OIDC identity and accepts only persisted lineage.

Scheduled reconciliation and assignment recovery can resume an interrupted
campaign from its private ref without trusting chat history. Force-with-lease
updates and idempotent effects prevent duplicate or conflicting transitions.

Privacy and least privilege are fail-closed: no raw held-out rows, private
dataset content, prompts, responses, traces, or credentials enter candidate
worktrees, PRs, dashboards, or committed evidence. Evidence is redacted and
content-addressed. Generated workflows receive only the permissions required
for their transport role, use pinned actions, never source PR-controlled
scripts, and use OIDC instead of stored Azure credentials.

## Tenzing integration

The generated skill vendors a byte-exact, read-only snapshot of
[Tenzing](https://github.com/coreai-microsoft/tenzing) at the reviewed revision
recorded in `references/tenzing/UPSTREAM.md`. There is no submodule, build-time
download, CI synchronization, or runtime fetch.

`ADAPTER_MAPPING.md` maps the protocol into this implementation:

- objective and editable area -> immutable issue-derived specification
- experiment branches -> disposable, code-only worktrees
- produce/evaluate loop -> Foundry drafts and development/held-out evaluations
- scoreboard -> redacted evidence, aggregate deltas, guardrails, and Pareto slate
- termination condition -> bounded candidate count, cutoff, and campaign budget

The vendored files remain unchanged; only the adapter mapping evolves.

## Current rollout status

This status is not the normal user journey and is not a claim that fresh
issue-only live acceptance passed.

- Product implementation: commit
  [`954aa89`](https://github.com/XOEEst/foundry-cloud-coding-agents/commit/954aa89),
  with **1,653 deterministic tests passed and 1 skipped** at acceptance
  preparation.
- Upstream acceptance bundle: prepared and tested locally at `684c4ba`, but
  not pushed because `XOEEst` currently has **READ-only** access to
  `microsoft-foundry/luffy-test-agents-repo`.
- No fresh current-architecture `[Optimize]` acceptance issue was created.
  Historical issue #31 predates the steward architecture, so sole-initiation
  acceptance was not contaminated or falsely claimed.
- Foundry traffic remains **100% on version 3**. Version 4 is preserved and was
  not deleted.

The deterministic implementation is ready; upstream rollout and a fresh
issue-only acceptance run remain blocked on restored write/admin permission.

## Development

```shell
uv sync --dev
uv run pytest -q
uv build
```

Candidate hosted agents use source-code ZIP remote bundles. This project does
not use Azure Container Registry for candidate deployment.
