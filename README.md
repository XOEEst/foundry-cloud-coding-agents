# Foundry Cloud Coding Agent

`foundry-opt` is a Python 3.12 CLI for producing reviewable, evidence-backed
Microsoft Foundry agent optimization candidates without changing production
traffic during optimization.

The current workflow provides:

- a typed `.github/foundry-optimizer.yaml` configuration model
- `foundry-opt preflight`
- issue-defined optimization goals and immutable specification PRs
- resumable candidate request/submit evaluation cycles
- exact-patch candidate PRs with human-default merge and deployment policy
- retained-improvement post-deployment evaluation
- secretless GitHub Actions OIDC authentication through `azure/login`
- Microsoft Foundry dataset, evaluator, draft, evaluation, and deployment
  integration
- Tenzing-derived idea, experiment, evaluation, and evidence discipline

## Issue-driven optimization

Repository policy and allowed capabilities live in
`.github/foundry-optimizer.yaml`. Each optimization issue supplies its own
goal, datasets, evaluators, metric thresholds, materiality, mutation classes,
and human/autopilot decision mode.

```shell
foundry-opt optimize spec --issue 42
# Review and merge the generated immutable specification PR.
foundry-opt optimize run --issue 42
foundry-opt optimize candidate request --issue 42
# Edit only the reserved worktree and write the idea JSON outside it.
foundry-opt optimize candidate submit \
  --issue 42 \
  --candidate candidate-1 \
  --idea-file .foundry-optimizer/campaigns/issue-42/candidates/candidate-1/idea.json
foundry-opt optimize run --issue 42
```

Eligible candidates are published as redacted evidence plus exact patch
artifacts. A Copilot coding agent may apply the patch, but the deterministic
candidate check verifies the campaign issue, base commit, patch, evidence, and
result tree before a human merge. `foundry-opt optimize reconcile --issue 42`
then observes merge/deployment lineage and closes the issue only when the
published version retains the measured improvement.

Runtime campaign state and worktrees remain ignored. Approved specifications
under `.foundry-optimizer/specs/issue-<N>/` and published evidence are the
reviewable, content-addressed records.

## Development

```shell
uv sync --dev
uv run pytest -q
uv build
```

## Authentication

The default `oidc` mode expects the setup workflow to authenticate with a
pinned `azure/login` action. `foundry-opt` then uses `AzureCliCredential` and
verifies the active tenant, subscription, and service-principal client ID.

Azure client secrets are not required for the acceptance environment.

`foundry-opt init` can create the repository-level GitHub Agents variables
`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, and `AZURE_SUBSCRIPTION_ID`; use its
Actions-environment mirroring option when ordinary workflows also need those
non-secret values.

## Deployment constraint

Candidate hosted agents use source-code ZIP remote bundles. This project does
not use Azure Container Registry for candidate deployment.
