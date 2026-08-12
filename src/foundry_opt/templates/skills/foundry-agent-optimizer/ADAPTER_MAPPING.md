# Foundry adapter mapping for the Tenzing improvement loop

The vendored protocol in `references/tenzing/` describes a
domain-independent loop: fix an objective and edit boundary, try bounded
experiments, evaluate them, retain evidence, and stop at a declared condition.
The snapshot remains unmodified.

`foundry-opt` realizes that loop inside one issue and one persistent workspace
pull request. The assigned steward follows durable workspace `next_actions`;
internal candidate alternatives are implementation details, not separate
GitHub review surfaces.

## Single-workspace realization

The issue fixes the target-specific objective and requested inputs within
`.github/foundry-optimizer.yaml`. The workspace command creates or reuses the
one draft pull request and advances its durable state:

`foundry-opt workspace advance --issue <number> --json`

The steward performs only returned internal actions, updates the same branch,
and invokes the command again. It pauses only for an external operation, the
human merge, a blocked state, or completion.

Foundry network work stays outside Copilot:

- `foundry-optimization-operations.yml` uses the optimizer OIDC identity for
  persisted asset, draft, development-evaluation, and retained-evaluation
  operations.
- `deploy-foundry-agent.yml` uses the separate deployment OIDC identity for
  exact publication only.
- `foundry-exact-candidate-check.yml` validates the selected patch and tree
  without redesign or repair.

## Mapping table

| Tenzing concept | Foundry single-workspace realization |
| --- | --- |
| Objective and success criteria | The `[Optimize]` issue selects one configured target and supplies a measurable goal, metric policies, guardrails, assets, and requested mutations. Repository policy validates and content-hashes the resulting boundary before experiments advance. |
| Editable area and prohibitions | Allowed paths, mutation classes, restricted opt-ins, dependency rules, validation commands, and secret rejection come from repository policy plus the immutable issue boundary. The workspace may narrow but never widen them. |
| Data inputs and leakage rule | Dataset and evaluator identities are pinned by name, version, and content hash. Development aggregates may guide the next internal experiment. Held-out rows, raw traces, prompts, and responses never enter a worktree or pull request. |
| One branch per experiment | Candidate alternatives use disposable optimizer-managed code isolation. Only the selected exact patch is materialized on the persistent workspace branch. Rejected alternatives remain privacy-safe lineage and aggregates. |
| Produce and score a run | The workspace records exact operation intent. The optimizer-OIDC workflow creates or reconciles the bound Foundry draft and evaluation, then records normalized metrics and remote identities without raw evidence. |
| Scoreboard and experiment metadata | Durable workspace state and redacted evidence retain baseline metrics, candidate aggregates, deltas, guardrails, hashes, costs, and selection lineage. The root issue and workspace pull request project that state for review. |
| Keep/discard decision | Deterministic eligibility and selection compare bounded candidates against hard guardrails and materiality. The steward cannot override the returned decision in prose or by editing metadata. |
| Termination condition | Repository limits bound candidate count, cutoff, deadline, retries, and cost. Workspace `next_actions` determine whether the steward continues internally or pauses for an external operation, human merge, blocked state, or completion. |
| Final publication | After the same workspace pull request is eligible and merged, the deployment-identity workflow publishes the exact selected version. The optimizer-identity workflow then evaluates retained behavior. |

## Reading order

1. Read `references/tenzing/README.md` and
   `references/tenzing/climb.md` for the upstream loop shape.
2. Use the table above to translate that shape into issue policy, internal
   workspace experiments, Foundry operations, redacted evidence, and durable
   `next_actions`.
3. Read `../SKILL.md` for the generated steward and workflow contract.

## Guardrails

- Never edit files under `references/tenzing/`.
- Never put dataset rows, traces, credentials, evaluator prompts, or held-out
  evidence in repository state.
- Never execute pull-request-controlled scripts in a privileged workflow.
- Never let optimizer and deployment identities assume each other's role.
- Never create a second optimization branch or pull request.
- Never infer lifecycle authority from comments, labels, or chat history.

This mapping is repository integration guidance, not an upstream Tenzing
artifact. If the adaptation changes, this file changes while the vendored
snapshot remains byte-for-byte reviewed.
