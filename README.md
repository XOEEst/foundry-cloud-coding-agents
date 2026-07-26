# Foundry Cloud Coding Agent

`foundry-opt` is a Python 3.12 CLI for producing reviewable, evidence-backed
Microsoft Foundry agent optimization candidates without changing production
traffic during optimization.

The current milestone provides:

- a typed `.github/foundry-optimizer.yaml` configuration model
- `foundry-opt preflight`
- Git and GitHub repository checks
- secretless GitHub Actions OIDC authentication through `azure/login`
- read-only Microsoft Foundry project access checks

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

## Deployment constraint

Candidate hosted agents use source-code ZIP remote bundles. This project does
not use Azure Container Registry for candidate deployment.
