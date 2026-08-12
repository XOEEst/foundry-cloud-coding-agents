---
name: foundry-agent-optimizer
description: >-
  Run issue-driven Microsoft Foundry agent optimization in one persistent
  workspace pull request with bounded candidates and retained-improvement
  verification.
---

# Foundry Agent Optimizer

Create one `[Optimize]` issue from the generated form. That issue is the sole
normal initiation action. Do not start with a handwritten branch, phase
command, label, or manual workflow dispatch.

The generated customer journey has one assigned
`foundry-optimization-steward`, one persistent draft workspace pull request,
and these five workflows:

- `copilot-setup-steps.yml`
- `foundry-optimization-workspace.yml`
- `foundry-optimization-operations.yml`
- `foundry-exact-candidate-check.yml`
- `deploy-foundry-agent.yml`

The workspace pull request is the only optimization pull request. It contains
the evolving specification, bounded candidate comparison, selected exact
patch, validation results, and privacy-safe evidence. The root issue remains
the status dashboard. The normal human gate is to merge that same pull request
after it becomes eligible.

## Steward loop

The steward advances only through:

`foundry-opt workspace advance --issue <number> --json`

Read the returned workspace state and `next_actions`. Perform only listed
actions that are assigned to the steward and remain inside the same workspace
pull request. Examples include editing allowlisted files, running repository
validation, comparing the bounded candidate slate, updating redacted evidence,
or applying the selected exact patch to the workspace branch. After completing
an internal action, invoke the workspace command again and continue from its
new durable result.

Do not stop merely because one invocation returned successfully. Stop when the
returned state is waiting for an external Foundry operation, waiting for the
human merge, blocked, or complete. Never invent an action that is absent from
`next_actions`, widen candidate limits, create another optimization branch, or
reconstruct authority from comments, labels, or conversation history.

The workspace command owns durable lifecycle transitions. Agent prose, shell
scripts, workflow YAML, issue comments, and labels are projections only.

## Candidate comparison

Candidate ideas are internal workspace experiments:

- Keep the issue-approved goal, metric policy, asset identities, mutation
  allowlist, base commit, candidate count, deadline, and cutoff immutable.
- Use only disposable code worktrees or equivalent optimizer-managed
  isolation. Dataset content never enters them.
- Compare development results using normalized metrics and hard guardrails.
- Do not expose held-out rows, raw traces, evaluator prompts, responses,
  credentials, or private dataset content.
- Materialize only the selected exact patch in the persistent workspace pull
  request.
- Keep rejected alternatives in privacy-safe aggregates and durable lineage,
  not as additional GitHub review surfaces.

The `Foundry exact candidate check` verifies the selected metadata and tree. It
does not redesign, repair, select, merge, or deploy.

## Foundry operations

Copilot never calls Foundry network adapters. The generated
`foundry-optimization-operations.yml` workflow runs under the optimizer OIDC
identity and supplies the external capability for persisted asset
registration, draft creation, development evaluation, and retained
post-deployment evaluation.

Each operation must be bound to the issue, workspace revision, base commit,
exact bundle or draft version, immutable dataset/evaluator identities, split,
metric policy, and an idempotency key. Persist only remote identities,
normalized results, hashes, and privacy-safe aggregates.

The operations workflow may reconcile existing internal operation records
during migration, but those records do not create additional customer-facing
surfaces or change the single-workspace lifecycle.

## Deployment isolation

`deploy-foundry-agent.yml` runs under the separate deployment OIDC identity.
It consumes only an exact persisted deployment intent and records only the
matching publication result. The deployment identity never performs candidate
design or post-deployment evaluation. The optimizer identity never publishes
the production version.

The deployment workflow checks out the trusted default branch, installs the
pinned optimizer, validates identifiers before use, and never executes
pull-request-controlled scripts.

## Trusted workflow behavior

- `foundry-optimization-workspace.yml` handles issue intake, workspace pull
  request lifecycle events, and continuation of the same Copilot session.
- `foundry-optimization-operations.yml` handles optimizer-OIDC external
  operations and retained-improvement evaluation.
- `foundry-exact-candidate-check.yml` is read-only and validates the exact
  selected patch.
- `deploy-foundry-agent.yml` is the only deployment-identity workflow.
- `workflow_dispatch` inputs are retry controls, never normal initiation or
  phase-selection controls.

Privileged workflows check out only the trusted default branch and install the
optimizer from the configured immutable package pin. They do not source
repository shell configuration, project `uv` configuration, dotenv files, or
pull-request-controlled scripts.

## Repository policy

`.github/foundry-optimizer.yaml` is durable repository policy. It defines
environments, targets, allowed paths and mutations, asset sources, campaign
limits, validation, checks, identities, and deployment capabilities. Each
issue chooses one configured target and supplies its measurable goal, assets,
metrics, and requested mutations within that boundary.

The issue and workspace may narrow policy but never widen it. Secret-shaped
configuration, unpinned assets, unsupported endpoints, path traversal,
untrusted executable inputs, and identity mixing fail closed.

## Required assignment credential

Create the repository Actions secret `COPILOT_ASSIGNMENT_TOKEN` before issue
intake. Use a user-to-server credential for a user eligible to assign the
configured Copilot agent. Prefer a fine-grained personal access token scoped
only to this repository.

Grant only:

- Metadata: read
- Actions, Contents, Issues, and Pull requests: read/write

GitHub App installation tokens are not supported for Copilot assignment.
`foundry-opt init` cannot create Actions secrets. Never commit or emit this
credential in workflow output, durable state, comments, issue bodies, or logs.

## Tenzing adaptation

The unmodified Tenzing snapshot under `references/tenzing/` is a reviewed,
read-only description of a bounded improvement loop. Read
`references/tenzing/README.md`, `references/tenzing/climb.md`, and
`ADAPTER_MAPPING.md`. Never edit the vendored snapshot.

The adaptation maps Tenzing's objective, edit boundary, experiments,
evaluation, scoreboard, and stopping rule into immutable issue policy, internal
workspace experiments, Foundry drafts/evaluations, redacted evidence, and
durable workspace `next_actions`.

> **Not an upstream Tenzing artifact.** The generated skill and adapter mapping
> are repository integration guidance. Using them implies no endorsement by,
> affiliation with, or review from the upstream Tenzing authors or
> maintainers.
