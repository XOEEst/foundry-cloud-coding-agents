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
- transport, reconciliation, Foundry capability, candidate-check, and
  deployment-bridge workflows
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

Immediately after `azure/login`, the generated Copilot setup workflow verifies
that Azure CLI is using the configured optimizer service principal and warms
Azure CLI's managed cache for the Foundry SDK scopes
`https://ai.azure.com/.default` and
`https://cognitiveservices.azure.com/.default`. Token command output and errors
are suppressed and never copied into logs, workflow outputs, `GITHUB_ENV`, or
repository files. Warming proves token acquisition only; campaign Foundry
network operations never run in Copilot. The generated Actions capability
workflow uses the optimizer OIDC identity to register assets, create exact
drafts, and run development evaluations. The ARM management audience remains
unneeded.

Before merging or running the generated workflows, manually create the
repository Actions secret `COPILOT_ASSIGNMENT_TOKEN`. `foundry-opt init`
cannot create Actions secrets. The value must be a user-to-server credential
for a user who can use Copilot cloud agent and summon it on the existing
workspace pull request; GitHub Actions `github.token` and GitHub App
installation tokens are not supported for this invocation. This credential is
for Copilot invocation and verified assignment-comment cleanup only.

Prefer a fine-grained personal access token scoped only to the target
repository, or use a GitHub App user-to-server token or OAuth app token.
GitHub documents these minimum fine-grained repository permissions for
posting the invocation on the existing workspace pull request:

- Metadata: read
- Issues and Pull requests: read/write

Store the credential only as the Actions secret. Never commit it, put it in a
workflow variable, print it, or persist it in issue content or optimizer
state. Generated workflows use `github.token` for durable repository
operations, which therefore appear as `github-actions[bot]`. Only the narrow
workspace assignment subprocess receives `COPILOT_ASSIGNMENT_TOKEN`; its
private invocation adapter uses that credential to summon Copilot on the
existing workspace pull request. It is never the general `GH_TOKEN`.

After the trusted workflow verifies and captures candidate provenance, it
removes the transient assignment comment with the same narrow credential.
The Copilot source-commit and acknowledgement-comment links remain in the
issue and workspace pull request as durable public evidence.

The long-term target is a Foundry-owned, published, and secured
`foundry-optimizer[bot]` GitHub App. Customers will only install the App on
selected repositories. Foundry will issue short-lived installation tokens
through a broker/workload-identity exchange; no App private key will be stored
in a customer repository. This migration is not active yet and must not change
candidate or lineage interfaces, the issue and single-workspace pull-request
journey, or the human merge gate.

`foundry-opt init` validates the generated workflow scope, and
`foundry-opt preflight` scans repository workflow artifacts without reading
secret values. Both reject using `COPILOT_ASSIGNMENT_TOKEN` as a generic
`GH_TOKEN` or exposing it outside the narrow invocation and cleanup steps.

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
campaign history. The append-only
`refs/heads/foundry-opt/inbox/issue-<N>` ref is the trusted issue lifecycle
authority consulted before state during recovery; comments, labels, and
conversation history are projections rather than authority.

Candidate worker failures retain the existing failed state and non-zero exit
semantics while exposing a privacy-safe blocker code for dashboards and
recovery automation:

- `candidate_assets_unavailable`: asset resolution or registration
- `candidate_validation_failed`: candidate or bundle validation
- `candidate_draft_unavailable`: Foundry draft creation
- `candidate_evaluation_unavailable`: evaluation binding or execution
- `candidate_evidence_unavailable`: redacted evidence production
- `candidate_worktree_failed`: Git, worktree, or durable state operations
- `candidate_workers_unavailable`: an unclassified worker exception

Durably delegated Foundry effects are not failures. The steward reports
`candidate_assets_registration_pending`, `candidate_draft_pending`, or
`candidate_evaluation_pending`, stops, and resumes only after the capability
workflow CAS-records the exact result.

The accompanying bounded summary contains the exception class and, only for
known typed failures, a sanitized user-facing message. It never includes
tracebacks, credentials, URLs, prompts, raw rows, or private evaluation
content. Operators should route recovery by the stable code rather than parse
the summary.

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
- `refs/heads/foundry-opt/state/issue-<N>` is the authority for campaign state,
  outbox, replay, and session replacement. The separate trusted inbox ref is
  the authority for issue creation, edits, closure, declassification, and
  reopening.
- GitHub Actions are transport and capability only: they record trusted events,
  project persisted effects, reconcile inactivity, and dispatch already
  authorized deployment intents. Actions may replay canonical interfaces to
  verify a proposed handoff exactly, but cannot choose another transition or
  invent an effect. Handoff discovery selects transport envelopes only; the
  canonical replay path still validates the steward's exact decision.
- The Copilot steward never calls Foundry network adapters. It persists
  `candidate_assets_registration_planned` for exact approved asset
  name/version/hash/path bindings and `candidate_effect_planned` for exact
  draft and development-evaluation intents. The five-minute Actions capability
  bridge validates those immutable bindings, reconciles or executes the
  idempotent external operation under the optimizer OIDC identity, CAS-records
  privacy-safe identities and normalized results, then wakes the steward.
  Actions cannot choose candidates, alter policy, eligibility, or the slate.
  Development evaluations use a deterministic key bound to campaign,
  generation, base commit, draft version, dataset/evaluator identities, split,
  and metric policy; retries reconcile that exact provider run after ack loss.
  Capability and handoff JSON are closed-schema; commit/body markers are only
  secondary correlation checks.
- The default-branch-generated Copilot setup workflow exports the static,
  non-secret `FOUNDRY_OPT_COPILOT_GIT_PROXY=1` marker; no pull-request input
  controls it. Proxy authority additionally requires `GITHUB_ACTIONS=true`, the
  exact repository, an exact loopback HTTP(S) origin with port and repository
  path, and either an attached native `copilot/` symbolic ref or a detached
  checkout bound to a safe `GITHUB_AGENT_BRANCH_NAME` and
  `GITHUB_AGENT_ACTOR`. Detached sessions require complete, sane
  production/session markers, a valid HEAD commit, and an exact match with the
  proxy branch tip when that branch already exists. Attached wrapper branch
  metadata must match the symbolic ref. Hidden token and download markers are
  not required. `COPILOT_CLI` may be present in the cloud runtime, but it
  grants no authority and does not bypass any proxy-context check.
- In a verified Copilot proxy context, the canonical command does not send the
  private state/design ref through the proxy. It commits one privacy-validated,
  content-addressed envelope on the verified wrapper session branch, creating
  that branch with compare-and-swap when needed. A
  base-context workflow reads only that exact object, CAS-publishes the private
  ref, applies persisted outbox effects, and auto-closes the internal handoff PR.
  The `pull_request_target` event is the fast path. A default-branch five-minute
  schedule and retry-only dispatch also discover a bounded, oldest-first set of
  open same-repository Copilot handoff PRs. They do not depend on PR check
  conclusions, so an `action_required` event run cannot block fallback.
  Discovery re-fetches each live PR and exact head SHA, binds that SHA to a
  bounded GitHub timeline window started and finished by the exact Copilot App
  with exactly one commit and no later head change, requires one reserved JSON
  change, and never checks out or executes PR content. The job
  always checks out the trusted default branch, ignores repository uv and
  dotenv configuration, and lets the existing isolated Git transport validate
  and scope the checkout credential header for private-ref reads and CAS
  writes. A base-controlled bounded product-commit allowlist permits
  already-open envelopes to survive a transport-only rollout without
  weakening canonical validation.
- Private state/design transport and handoff publication bind to one captured
  URL through an isolated Git configuration. Ambiguous `pushurl`, URL-rewrite,
  pack-helper, or proxy settings fail closed and cannot redirect proposal
  objects.
- The explicit marker alone grants no state authority. Ordinary Actions origins
  and branches remain on normal Git semantics, and a local spoof can only route
  through the trusted handoff validator and compare-and-swap checks.
- Pull requests are opened natively by Copilot planner and applier sessions,
  not by Actions or direct GitHub API calls, to comply with enterprise policy.
- The planner materializes exceptional immutable spec PRs; the designer edits
  only reserved worktrees; the applier applies one exact patch without repair.
- Candidate worktree paths are ephemeral. A resumed steward verifies the
  state-bound design ref, parent, commit, tree, changed paths, and allow-list,
  then recreates the deterministic managed worktree before continuing.
  Rehydration failures retain `candidate_worktree_failed` with a stable
  `candidate_design_*` detail.
- Deployment uses a separate OIDC identity and accepts only persisted lineage.

Scheduled reconciliation validates and replays the trusted inbox lifecycle
before consulting campaign state. Closed, declassified, blocked, and terminal
campaigns are not reassigned; an explicit reopen starts the allowed new
generation. Force-with-lease updates, remote acknowledgement checks, and
idempotent handoff application prevent duplicate or conflicting transitions.
Competing valid handoffs fail closed and trigger fresh assignment rather than
overwriting state.

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
