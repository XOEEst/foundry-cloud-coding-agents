from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

import yaml

from foundry_opt.config.models import OptimizerConfig
from foundry_opt.onboarding.bundle import (
    generate_repository_agent_bundle,
    legacy_repository_agent_bundle,
    legacy_repository_agent_hashes,
)
from foundry_opt.onboarding.repository import (
    normalize_legacy_generated_content,
)
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
    environment: {json.dumps(request.environment_name)}
    permissions:
      contents: read
      id-token: write
    steps:
      - name: Checkout repository
        uses: {CHECKOUT_ACTION} # v7.0.1
      - name: Export non-secret Azure OIDC identifiers
        env:
          ACTIONS_AZURE_TENANT_ID: ${{{{ vars.AZURE_TENANT_ID }}}}
          ACTIONS_AZURE_CLIENT_ID: ${{{{ vars.AZURE_CLIENT_ID }}}}
          ACTIONS_AZURE_SUBSCRIPTION_ID: ${{{{ vars.AZURE_SUBSCRIPTION_ID }}}}
        shell: bash
        run: |
          for name in AZURE_TENANT_ID AZURE_CLIENT_ID AZURE_SUBSCRIPTION_ID; do
            fallback="ACTIONS_${{name}}"
            value="${{!name:-${{!fallback:-}}}}"
            if [ -z "$value" ]; then
              echo "Missing GitHub Agents or Actions variable: $name" >&2
              exit 1
            fi
            printf '%s=%s\\n' "$name" "$value" >> "$GITHUB_ENV"
          done
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
    legacy_setup_workflow_text = workflow_text
    workflow_text += """      - name: Verify issue-only steward entry point
        run: foundry-opt steward advance --help
"""

    contents = {
        Path(".github/foundry-optimizer.yaml"): config_text,
        Path(".github/workflows/copilot-setup-steps.yml"): workflow_text,
    }
    contents.update(
        generate_repository_agent_bundle(
            request,
            oidc_subject=oidc_subject,
            deployment_workflow_name=(
                workflow.name or workflow.path.as_posix()
            ),
        )
    )
    legacy_hashes = legacy_repository_agent_hashes(
        request,
        oidc_subject=oidc_subject,
    )
    legacy_hashes[
        Path(".github/workflows/copilot-setup-steps.yml")
    ] = hashlib.sha256(
        legacy_setup_workflow_text.encode("utf-8")
    ).hexdigest()
    legacy_contents = legacy_repository_agent_bundle(
        request,
        oidc_subject=oidc_subject,
    )
    legacy_contents[
        Path(".github/workflows/copilot-setup-steps.yml")
    ] = legacy_setup_workflow_text
    accepted_previous = {
        path.as_posix(): [digest]
        for path, digest in legacy_hashes.items()
        if (
            path in contents
            and hashlib.sha256(
                contents[path].encode("utf-8")
            ).hexdigest()
            != digest
        )
    }
    obsolete = {
        path.as_posix(): [digest]
        for path, digest in legacy_hashes.items()
        if path not in contents
    }
    accepted_previous_normalized = {}
    obsolete_normalized = {}
    for path, content in legacy_contents.items():
        normalized = normalize_legacy_generated_content(path, content)
        if normalized is None:
            continue
        destination = (
            accepted_previous_normalized
            if path in contents
            else obsolete_normalized
        )
        destination[path.as_posix()] = [
            hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        ]
    manifest_path = Path(".github/foundry-optimizer.generated.json")
    contents[manifest_path] = (
        json.dumps(
            {
                "accepted_previous_sha256": accepted_previous,
                "accepted_previous_normalized_sha256": (
                    accepted_previous_normalized
                ),
                "files": {
                    path.as_posix(): hashlib.sha256(
                        content.encode("utf-8")
                    ).hexdigest()
                    for path, content in sorted(
                        contents.items(),
                        key=lambda item: item[0].as_posix(),
                    )
                },
                "generator": "foundry-opt init",
                "obsolete": obsolete,
                "obsolete_normalized_sha256": obsolete_normalized,
                "schema_version": 1,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return contents


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
    elif _dataset_roles(discovery) is None:
        blockers.append(
            "Dataset roles require exactly one development and one validation "
            "dataset."
        )
    if not discovery.evaluators:
        blockers.append("No evaluator was discovered.")
    else:
        evaluator = _optimization_evaluator(discovery)
        if evaluator is None:
            blockers.append(
                "Evaluator role is ambiguous; select exactly one optimization "
                "evaluator."
            )
        elif not evaluator.metrics:
            blockers.append(
                f"Evaluator {evaluator.reference} needs input: "
                f"{evaluator.needs_input or 'metric policy semantics are missing.'}"
            )
    if not discovery.deployment_workflows:
        blockers.append("No deployment workflow was discovered.")
    else:
        deployment_workflow = _deployment_workflow(discovery)
        if deployment_workflow is None:
            blockers.append(
                "Deployment workflow role is ambiguous; select exactly one "
                "deployment workflow."
            )
        elif deployment_workflow.deployment_identity_verified is False:
            blockers.append(
                "The deployment job must use pinned azure/login with "
                "AZURE_DEPLOYMENT_CLIENT_ID before deployment and upload "
                "the foundry-optimization-deployment-result artifact "
                "afterward."
            )
        elif deployment_workflow.trigger_contract_verified is False:
            blockers.append(
                "The deployment workflow trigger must be either a "
                "default-branch push/workflow_run contract or a "
                "workflow_dispatch contract with selected_commit and "
                "foundry_opt_effect_id inputs."
            )
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
    if request.client_id == request.deployment_client_id:
        blockers.append(
            "Optimizer and deployment client IDs must be distinct."
        )
    if any(
        not value.strip()
        for value in (
            request.environment_name,
            request.target_name,
            request.project_endpoint,
            request.project_resource_id,
            request.tenant_id,
            request.client_id,
            request.deployment_client_id,
            request.subscription_id,
        )
    ):
        blockers.append("All non-secret onboarding identifiers are required.")
    if any(
        len(value) > 4096
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in value
        )
        for value in (
            request.environment_name,
            request.target_name,
            request.project_endpoint,
            request.project_resource_id,
            request.tenant_id,
            request.client_id,
            request.deployment_client_id,
            request.subscription_id,
        )
    ):
        blockers.append(
            "Onboarding identifiers contain unsafe characters."
        )
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
    evaluator = _optimization_evaluator(discovery)
    if evaluator is None:
        raise ValueError("optimization evaluator role is ambiguous")
    return evaluator


def _select_datasets(discovery: RepositoryDiscovery):
    datasets = _dataset_roles(discovery)
    if datasets is None:
        raise ValueError("development and validation dataset roles are ambiguous")
    return datasets


def _select_deployment_workflow(
    discovery: RepositoryDiscovery,
) -> DeploymentWorkflowDiscovery:
    workflow = _deployment_workflow(discovery)
    if workflow is None:
        raise ValueError("deployment workflow role is ambiguous")
    return workflow


def _dataset_roles(
    discovery: RepositoryDiscovery,
) -> tuple[DatasetDiscovery, DatasetDiscovery] | None:
    development = tuple(
        dataset
        for dataset in discovery.datasets
        if (dataset.role or _inferred_dataset_role(dataset.name))
        == "development"
    )
    validation = tuple(
        dataset
        for dataset in discovery.datasets
        if (dataset.role or _inferred_dataset_role(dataset.name))
        == "validation"
    )
    if len(development) != 1 or len(validation) != 1:
        return None
    if development[0] is validation[0]:
        return None
    return development[0], validation[0]


def _inferred_dataset_role(name: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    if normalized in {"dev", "development"} or "development" in normalized:
        return "development"
    if normalized in {"validation", "held-out", "heldout"}:
        return "validation"
    if "validation" in normalized or "held-out" in normalized:
        return "validation"
    return None


def _optimization_evaluator(
    discovery: RepositoryDiscovery,
) -> EvaluatorDiscovery | None:
    explicit = tuple(
        evaluator
        for evaluator in discovery.evaluators
        if evaluator.role == "optimization"
    )
    if explicit:
        return explicit[0] if len(explicit) == 1 else None
    evaluators = discovery.evaluators
    return evaluators[0] if len(evaluators) == 1 else None


def _deployment_workflow(
    discovery: RepositoryDiscovery,
) -> DeploymentWorkflowDiscovery | None:
    explicit = tuple(
        workflow
        for workflow in discovery.deployment_workflows
        if workflow.role == "deployment"
    )
    if explicit:
        return explicit[0] if len(explicit) == 1 else None
    workflows = discovery.deployment_workflows
    return workflows[0] if len(workflows) == 1 else None
