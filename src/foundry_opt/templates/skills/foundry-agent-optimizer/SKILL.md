---
name: foundry-agent-optimizer
description: >-
  Run issue-only, steward-owned Microsoft Foundry agent optimization with immutable
  specifications, bounded Tenzing-style candidate design, exact patches, and retained-improvement
  verification.
---

# Foundry Agent Optimizer

Create one `[Optimize]` issue from the generated form. That issue is the sole normal campaign
initiation action. Do not start a campaign with `workflow_dispatch`, a phase command, a label, or a
handwritten branch. The only normal human action after filing is to merge exactly one eligible
candidate pull request. Until then, the user watches the root issue dashboard and candidate PRs;
policy/specification, baseline, candidate design, draft evaluation, and evidence publication require
no normal human action.

The only exceptional human gate is an immutable specification PR. Request it only when the resolved
spec contains new asset bytes, custom, synthetic, or trace-derived assets, changed repository
content, unpinned assets, human-gated assets, or policy that disables auto-approval. Existing
immutable policy-approved assets advance without a spec PR.

The assigned `foundry-optimization-steward` must run
`foundry-opt steward advance --issue <number> --json` exactly once, then stop and resume only from
the issue's durable Git-state ref on a later assignment.
Comments and labels are projections, never authority. Transport workflows record trusted events,
apply only persisted outbox effects, and reassign Copilot; they never classify assets, design or
evaluate candidates, select a winner, or declare completion.

## User-visible contract

`.github/foundry-optimizer.yaml` is durable repository policy: environments, configured targets,
allowed paths and mutations, asset sources, limits/defaults, validation, checks, identities, and
deployment capabilities. Each issue selects one target and supplies its own optimization goal,
assets, metrics, and requested mutations within that boundary. Different issues may optimize
different measurable behaviors without rewriting repository policy.

The root issue dashboard is the user's primary surface. Project specification classification and
digest; bounded-campaign phase/status; baseline and ranked candidate aggregates, deltas, guardrails,
and redacted-evidence links; selected merge lineage; deployment state; and the final Foundry
version, Actions run, portal link, hashes, required checks, and retained baseline/draft/deployed
metrics. Candidate PRs show the exact patch and the `Foundry exact candidate check`. Merge exactly
one eligible PR; the steward then automatically deploys, re-evaluates retained improvement,
supersedes competing candidates, updates the final dashboard, and closes the root issue.

The private `refs/heads/foundry-opt/state/issue-<number>` ref is the canonical hash-chained
campaign history. Recovery first replays the append-only
`refs/heads/foundry-opt/inbox/issue-<number>` lifecycle, then consults state. The completed issue
and candidate PRs are the user-facing projection.

## Required GitHub assignment credential

Before issue intake or reconciliation can start or reassign a Copilot session, create the
repository Actions secret `COPILOT_ASSIGNMENT_TOKEN`. Use a user-to-server credential for a user
who is eligible to use Copilot cloud agent and assign the selected custom agents. Prefer a
fine-grained personal access token scoped only to this repository. Alternatively, use a GitHub App
user-to-server token or OAuth app token. GitHub App installation tokens are not supported for
Copilot assignment.

Grant the minimum permissions documented by GitHub for assigning Copilot:

- Metadata: read
- Actions, Contents, Issues, and Pull requests: read/write

`foundry-opt init` cannot create Actions secrets, so add the secret manually in repository
settings before merging or running the generated workflows. Never commit the token or place it in
workflow output, comments, durable state, issue bodies, or logs. The generated workflows retain
`github.token` for ordinary transport operations and expose the dedicated credential only to the
Copilot assignment API calls.

This adaptation keeps an unmodified Tenzing snapshot under `references/tenzing/`. Read
`references/tenzing/README.md`, `references/tenzing/climb.md`, and `ADAPTER_MAPPING.md`; never edit
the vendored snapshot.

> **Not an upstream Tenzing artifact.** The generated skill and adapter mapping are repository
> integration guidance. Using them implies no endorsement by, affiliation with, or review from the
> upstream Tenzing authors or maintainers.

## Steward and specialists

### Campaign steward

The steward is the only owner of campaign transitions. It consumes trusted issue, specification PR,
candidate PR, and deployment workflow events; advances canonical steward interfaces; persists
privacy-safe state and outbox intents; and resumes after session replacement. It never exposes raw
evidence, held-out rows, traces, or credentials.

GitHub Actions are transport/capability only. Pull requests must be opened natively by Copilot
planner and applier sessions, not by Actions or direct PR APIs, because enterprise policy reserves
PR authorship to Copilot.

The trusted default-branch Copilot setup workflow exports the static, non-secret
`FOUNDRY_OPT_COPILOT_GIT_PROXY=1` marker; no pull-request input controls it. Proxy authority also
requires `GITHUB_ACTIONS=true`, the exact repository, an exact loopback HTTP(S) origin with port and
repository path, and Git plumbing confirmation that HEAD is a native `copilot/` branch. Available
production/session markers must be sane, hidden token/download markers are not required, and
`COPILOT_CLI` may be present but grants no authority. The marker alone grants no state authority:
ordinary Actions retain
normal Git semantics, while a local spoof can only enter the trusted handoff validator and
compare-and-swap path.

In a verified Copilot proxy context, the command does not send the private canonical state or
designer-result ref through the proxy. It commits one privacy-validated, content-addressed envelope
at the reserved handoff path on the native session branch instead. Those internal handoff pull requests
are transport artifacts, never specification or candidate pull requests, and are
auto-closed after handling. Generated `pull_request` workflows skip handoff-only changes.
The `pull_request_target` event remains the fast path. A default-branch five-minute schedule and
retry-only dispatch discover a bounded, oldest-first set of open same-repository Copilot handoff
PRs without consulting check conclusions, so an `action_required` run does not block fallback.
Discovery binds the exact head SHA to a bounded GitHub timeline window started and finished by the
exact Copilot App, with exactly one commit and no later head change, and selects transport envelopes
only; canonical replay still validates the steward's exact decision, commit markers, blob, hashes,
and compare-and-swap precondition. The job checks out only the trusted default branch, ignores
repository uv and dotenv configuration, and lets the isolated Git transport validate and scope the
checkout credential header for private-ref reads and CAS writes. A
base-controlled bounded product-commit allowlist lets an already-open envelope survive a
transport-only rollout.
Private state/design transport and handoff publication bind to one captured URL through an isolated
Git configuration. Ambiguous `pushurl`, URL-rewrite, pack-helper, or proxy settings fail closed and
cannot redirect proposal objects.

Actions may replay canonical interfaces only to verify the exact proposed transition and persisted
effects. Actions cannot choose a different transition, add an effect, reinterpret the model's
decision, or perform domain work. After verification, Actions provides only the missing Git/GitHub
transport capability: compare-and-swap the private ref and apply the already-persisted outbox.

### Specification planner

When a persisted `specialist_work_request` has `work_kind: prepare_specification_pr`, the transport
bridge creates a worker issue and assigns `foundry-optimization-planner`. The planner materializes
only the exact immutable specification and approved repository assets. The native Copilot session
opens its pull request; GitHub Actions and the planner do not call a PR-creation API. The planner
does not brainstorm or evaluate candidate ideas.

### Candidate designer

The steward invokes `foundry-candidate-designer` only through the canonical
`CandidateDesignIntent` / `CandidateDesignResult` boundary and its persisted transport effect. The
designer edits only the reserved worktree and allowed paths, writes the typed result file, and runs
`foundry-opt steward candidate-design-result` exactly once. It is the only specialist that
generates ideas or writes candidate code, and it stays within the campaign's bounded candidate
limits. Foundry draft creation, evaluation, eligibility, and selection remain deterministic steward
responsibilities.

### Exact-patch applier

For each persisted `applier_worker_issue_planned` effect, the transport bridge creates or reuses a
worker issue and assigns `foundry-candidate-applier`. The applier invokes the deterministic exact
patch command with no repair or reinterpretation. It performs no selection. Its native Copilot
session opens the governed candidate pull request. Merge exactly one eligible candidate pull
request; merging is the selection signal.

## Guardrails

- Treat the immutable issue-approved specification as the complete optimization boundary.
- Use only the canonical steward, specialist, draft, evaluation, publication, and deployment
  interfaces. Never reproduce their decisions in YAML, shell, comments, or agent prose.
- Never request, store, or emit secrets, raw traces, private dataset rows, or held-out evidence.
- Never source pull-request-controlled scripts or interpolate untrusted event text into commands.
- A handoff workflow may fetch and inspect only the exact validated head object; it never checks out
  or executes pull-request code and installs the optimizer only from the pinned base workflow.
- Scheduled discovery re-fetches live GitHub metadata, accepts only one reserved JSON change from an
  exact Copilot-authored same-repository `copilot/` branch to the default base, and remains bounded.
- Candidate work stays in its reserved code-only worktree; no dataset content is staged there.
  Dataset content never enters a pull request.
- Actions may create and update issues, comments, labels, assignments, durable refs, and persisted
  deployment dispatches only when an exact outbox effect authorizes them. Actions never create pull
  requests.
- Deployment runs under the separate deployment OIDC identity and consumes only persisted
  deployment intents, claims, and results.
- Reconciliation must project the trusted inbox lifecycle before state, skip closed,
  declassified, blocked, or terminal campaigns, and resume only after an explicit reopen. It must
  not invent state from comments, labels, live issue fields, or conversation history.
- Keep the optimizer identity separate from the deployment identity. Azure tenant, subscription,
  and client IDs are non-secret variables; never request an Azure client secret.
