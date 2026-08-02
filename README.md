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

`foundry-opt init` generates the `[Optimize]` issue form, Copilot custom
agents, and transport-only workflows. Create that issue to start a campaign.
The assigned steward resumes with `foundry-opt steward advance --issue 42`;
scheduled reconciliation can recover an inactive tracked campaign. Workflow
dispatch is an administrative retry, not a normal phase launcher.

Eligible candidates are published as redacted evidence plus exact patch
artifacts. Copilot-native planner and applier sessions open their own pull
requests; GitHub Actions never create them. The deterministic candidate check
verifies the campaign issue, base commit, patch, evidence, and result tree.
Merge exactly one eligible candidate pull request. The steward then observes
deployment lineage and closes the issue only when the published version
retains the measured improvement.

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
non-secret values. The generated deployment bridge uses the separate
`AZURE_DEPLOYMENT_CLIENT_ID` Actions-environment variable and never exposes
that identity to Copilot Agents variables. The configured deployment job must
authenticate with that identity before deployment and upload its
`deployment-result.json` as the
`foundry-optimization-deployment-result` artifact.

## Deployment constraint

Candidate hosted agents use source-code ZIP remote bundles. This project does
not use Azure Container Registry for candidate deployment.
