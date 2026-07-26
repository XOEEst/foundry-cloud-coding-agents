from __future__ import annotations

from pathlib import Path
import re

import yaml

from foundry_opt.config.models import OptimizerConfig
from foundry_opt.onboarding.models import (
    DeploymentWorkflowDiscovery,
    EvaluatorDiscovery,
    FoundryAgentDiscovery,
    OnboardingRequest,
    PythonAgentCandidate,
    RepositoryDiscovery,
)


CHECKOUT_ACTION = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
SETUP_PYTHON_ACTION = (
    "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97"
)
SETUP_UV_ACTION = "astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9"
AZURE_LOGIN_ACTION = "azure/login@532459ea530d8321f2fb9bb10d1e0bcf23869a43"


def generate_change_contents(
    request: OnboardingRequest,
    discovery: RepositoryDiscovery,
    *,
    oidc_subject: str,
) -> dict[Path, str]:
    local_agent = _select_local_agent(request.target_name, discovery)
    foundry_agent = _select_foundry_agent(request.target_name, discovery)
    workflow = _select_deployment_workflow(discovery)
    evaluator = _select_evaluator(discovery)
    development, validation = _select_datasets(discovery)
    base_version = max(
        (version for version in foundry_agent.versions if version.isdecimal()),
        key=int,
    )

    metrics = {
        metric.name: {
            "direction": metric.direction,
            "threshold": metric.threshold,
            "materiality": metric.materiality,
            "hard_guardrail": metric.hard_guardrail,
            "undefined_behavior": "fail",
        }
        for metric in evaluator.metrics
    }
    environment: dict[str, object] = {
        "authentication": "oidc",
        "project_endpoint": request.project_endpoint,
        "project_resource_id": request.project_resource_id,
        "allowed_models": [model.name for model in discovery.deployed_models],
        "deployment_workflow": {
            "path": workflow.path.as_posix(),
            "trigger": workflow.trigger,
        },
    }
    if discovery.app_insights.workspace_resource_id:
        environment["application_insights_workspace_resource_id"] = (
            discovery.app_insights.workspace_resource_id
        )

    config = {
        "schema_version": "1",
        "default_environment": request.environment_name,
        "environments": {request.environment_name: environment},
        "targets": {
            request.target_name: {
                "environment": request.environment_name,
                "source_paths": [local_agent.source_path.as_posix()],
                "edit_paths": [local_agent.source_path.as_posix()],
                "entry_point": local_agent.entry_point.as_posix(),
                "base_agent_version": base_version,
                "package": {
                    "include": [
                        f"{local_agent.source_path.as_posix()}/**",
                    ],
                    "exclude": [
                        "**/.git/**",
                        "**/.azure/**",
                        "**/__pycache__/**",
                        "**/.pytest_cache/**",
                        "**/.venv/**",
                    ],
                },
                "datasets": {
                    "development": [{
                        "name": development.name,
                        "version": development.versions[-1],
                        "mode": "batch",
                    }],
                    "validation": [{
                        "name": validation.name,
                        "version": validation.versions[-1],
                        "mode": "batch",
                    }],
                },
                "evaluators": [{
                    "name": evaluator.name,
                    "reference": evaluator.reference,
                    "metrics": list(metrics),
                }],
                "validation_commands": list(discovery.validation_commands),
                "metrics": metrics,
                "allowed_mutations": [
                    "system_instructions",
                    "python_logic",
                    "retrieval_orchestration",
                    "tests",
                    "packaging",
                    "model",
                    "skills",
                    "tool_descriptions",
                ],
            },
        },
        "campaign": {
            "deadline_minutes": 50,
            "candidate_cutoff_minutes": 40,
            "max_changed_candidates": 3,
            "transient_retries": 1,
            "stale_after_hours": 2,
            "allowed_mutations": [
                "system_instructions",
                "python_logic",
                "retrieval_orchestration",
                "tests",
                "packaging",
                "model",
                "skills",
                "tool_descriptions",
            ],
        },
    }
    OptimizerConfig.model_validate(config)
    config_text = yaml.safe_dump(config, sort_keys=False, width=88)

    workflow_text = f"""name: Copilot Setup Steps

on:
  workflow_dispatch:
  push:
    paths:
      - .github/workflows/copilot-setup-steps.yml
  pull_request:
    paths:
      - .github/workflows/copilot-setup-steps.yml

jobs:
  copilot-setup-steps:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write
    steps:
      - name: Checkout repository
        uses: {CHECKOUT_ACTION} # v7.0.1
      - name: Export non-secret Azure OIDC identifiers
        shell: bash
        run: |
          echo "AZURE_TENANT_ID=${{{{ vars.AZURE_TENANT_ID }}}}" >> "$GITHUB_ENV"
          echo "AZURE_CLIENT_ID=${{{{ vars.AZURE_CLIENT_ID }}}}" >> "$GITHUB_ENV"
          echo "AZURE_SUBSCRIPTION_ID=${{{{ vars.AZURE_SUBSCRIPTION_ID }}}}" >> "$GITHUB_ENV"
      - name: Sign in to Azure with repository-ID OIDC
        uses: {AZURE_LOGIN_ACTION} # v3.0.0
        with:
          client-id: ${{{{ env.AZURE_CLIENT_ID }}}}
          tenant-id: ${{{{ env.AZURE_TENANT_ID }}}}
          subscription-id: ${{{{ env.AZURE_SUBSCRIPTION_ID }}}}
      - name: Set up Python
        uses: {SETUP_PYTHON_ACTION} # v7.0.0
        with:
          python-version: '3.12'
      - name: Set up uv
        uses: {SETUP_UV_ACTION} # v9.0.0
      - name: Install pinned Foundry optimizer
        run: uv tool install {shell_quote(request.product_install)}
"""

    skill_text = f"""---
name: foundry-agent-optimizer
description: Safely prepare and run Foundry agent optimization campaigns.
---

# Foundry Agent Optimizer

- Run `foundry-opt preflight --target {request.target_name}` before a campaign.
- Use environment `{request.environment_name}` and target `{request.target_name}`.
- Keep Azure authentication OIDC-only. Never request or store client secrets,
  certificates, tokens, connection strings, prompts, dataset rows, or traces.
- The verified immutable repository-ID OIDC subject is `{oidc_subject}`.
- Optimize only configured paths and deployed model choices. Never deploy models,
  broaden permissions, use ACR, or change production routing.
- Draft creation must return a real `draft-*` version. Treat unavailable source-bundle
  support or any published version as a blocker, never as success.
- Commit only redacted evidence and exact patch artifacts for developer review.
"""
    return {
        Path(".github/foundry-optimizer.yaml"): config_text,
        Path(".github/workflows/copilot-setup-steps.yml"): workflow_text,
        Path(".github/skills/foundry-agent-optimizer/SKILL.md"): skill_text,
    }


def validate_generation_inputs(
    request: OnboardingRequest,
    discovery: RepositoryDiscovery,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if not discovery.python_agents:
        blockers.append("No Python agent candidate was discovered.")
    if not discovery.validation_commands:
        blockers.append("No repository validation command was discovered.")
    if not discovery.foundry_agents:
        blockers.append("No existing Foundry agent was discovered.")
    elif not any(
        version.isdecimal()
        for agent in discovery.foundry_agents
        for version in agent.versions
    ):
        blockers.append("No published numeric Foundry agent version was discovered.")
    if not discovery.deployed_models:
        blockers.append("No deployed model was discovered.")
    if len(discovery.datasets) < 2:
        blockers.append(
            "Development and held-out validation datasets must be discovered."
        )
    if not discovery.evaluators:
        blockers.append("No evaluator was discovered.")
    else:
        blockers.extend(
            f"Evaluator {evaluator.reference} needs input: "
            f"{evaluator.needs_input or 'metric policy semantics are missing.'}"
            for evaluator in discovery.evaluators
            if not evaluator.metrics
        )
    if not discovery.deployment_workflows:
        blockers.append("No deployment workflow was discovered.")
    local_matches = tuple(
        agent
        for agent in discovery.python_agents
        if agent.name == request.target_name
    )
    foundry_matches = tuple(
        agent
        for agent in discovery.foundry_agents
        if agent.name == request.target_name
    )
    if len(local_matches) != 1:
        blockers.append(
            f"Target {request.target_name!r} must exactly match one local "
            f"Python agent; found {len(local_matches)}."
        )
    if len(foundry_matches) != 1:
        blockers.append(
            f"Target {request.target_name!r} must exactly match one Foundry "
            f"agent; found {len(foundry_matches)}."
        )
    return tuple(blockers)


def validate_request_inputs(request: OnboardingRequest) -> tuple[str, ...]:
    blockers: list[str] = []
    if not _is_pinned_install(request.product_install):
        blockers.append("The product install must be pinned to a version or commit.")
    if any(
        not value.strip()
        for value in (
            request.environment_name,
            request.target_name,
            request.project_endpoint,
            request.project_resource_id,
            request.tenant_id,
            request.client_id,
            request.subscription_id,
        )
    ):
        blockers.append("All non-secret onboarding identifiers are required.")
    return tuple(blockers)


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _is_pinned_install(value: str) -> bool:
    return bool(
        re.fullmatch(
            r"[A-Za-z0-9_.-]+==[A-Za-z0-9][A-Za-z0-9._+-]*",
            value,
        )
        or re.fullmatch(
            r"(?:[A-Za-z0-9_.-]+\s*@\s*)?"
            r"git\+https://github\.com/"
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?"
            r"@[0-9a-fA-F]{40}",
            value,
        )
    )


def _select_local_agent(
    target_name: str,
    discovery: RepositoryDiscovery,
) -> PythonAgentCandidate:
    matches = tuple(
        agent for agent in discovery.python_agents if agent.name == target_name
    )
    if len(matches) != 1:
        raise ValueError("target must exactly match one local Python agent")
    return matches[0]


def _select_foundry_agent(
    target_name: str,
    discovery: RepositoryDiscovery,
) -> FoundryAgentDiscovery:
    matches = tuple(
        agent for agent in discovery.foundry_agents if agent.name == target_name
    )
    if len(matches) != 1:
        raise ValueError("target must exactly match one Foundry agent")
    return matches[0]


def _select_evaluator(discovery: RepositoryDiscovery) -> EvaluatorDiscovery:
    return next(
        evaluator
        for evaluator in discovery.evaluators
        if evaluator.metrics
    )


def _select_datasets(discovery: RepositoryDiscovery):
    return discovery.datasets[0], discovery.datasets[1]


def _select_deployment_workflow(
    discovery: RepositoryDiscovery,
) -> DeploymentWorkflowDiscovery:
    return discovery.deployment_workflows[0]
