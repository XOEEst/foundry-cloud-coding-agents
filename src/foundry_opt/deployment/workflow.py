from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from foundry_opt.deployment.models import (
    DeploymentTrigger,
    DeploymentWorkflow,
    DeploymentWorkflowModel,
    DeploymentWorkflowScaffold,
)


def detect_deployment_workflow(root: Path) -> DeploymentWorkflow:
    root = root.expanduser().resolve()
    candidates: list[DeploymentWorkflow] = []
    workflow_root = root / ".github" / "workflows"
    if workflow_root.is_dir():
        paths = sorted(
            (*workflow_root.glob("*.yml"), *workflow_root.glob("*.yaml"))
        )
        for path in paths:
            candidate = _workflow_candidate(root, path)
            if candidate is not None:
                candidates.append(candidate)
    if candidates:
        return sorted(
            candidates,
            key=lambda item: (
                item.trigger is not DeploymentTrigger.MERGE,
                item.path.as_posix(),
            ),
        )[0]
    return DeploymentWorkflow(
        path=Path(".github/workflows/foundry-opt-deploy.yml"),
        trigger=DeploymentTrigger.MANUAL,
        exists=False,
        name="Publish Foundry agent",
        scaffold=DeploymentWorkflowScaffold(
            description=(
                "Manual source ZIP publication model. It authenticates with "
                "the dedicated deployment OIDC identity, publishes a numeric "
                "Foundry version, and records the run and portal links. The "
                "caller owns rendering or writing this model."
            ),
            model=DeploymentWorkflowModel(
                trigger=DeploymentTrigger.MANUAL,
                permissions=("contents: read", "id-token: write"),
                actions=(
                    "checkout exact selected commit",
                    "authenticate dedicated deployment OIDC identity",
                    "rebuild and verify exact source ZIP",
                    "publish draft:false numeric Foundry version",
                    "verify deployed and latest version identity",
                ),
            ),
        ),
    )


def _workflow_candidate(
    root: Path,
    path: Path,
) -> DeploymentWorkflow | None:
    try:
        content = path.read_text(encoding="utf-8")
        document = yaml.safe_load(content) or {}
    except (OSError, UnicodeError, yaml.YAMLError):
        return None
    if not isinstance(document, dict):
        return None
    lowered = f"{path.name}\n{content}".casefold()
    if any(marker in lowered for marker in ("az acr", "docker build", ".azurecr.io")):
        return None
    role = document.get("x-foundry-opt-role")
    deployment_marked = role == "deployment" or (
        any(marker in lowered for marker in ("deploy", "publish"))
        and any(
            marker in lowered
            for marker in (
                "foundry",
                "agent",
                "azd deploy",
                "publish_agent",
                "foundry-opt",
            )
        )
    )
    if not deployment_marked:
        return None
    triggers = _trigger_names(document)
    if _is_merge_trigger(triggers, lowered):
        trigger = DeploymentTrigger.MERGE
    elif "workflow_dispatch" in triggers:
        trigger = DeploymentTrigger.MANUAL
    else:
        return None
    name = document.get("name")
    return DeploymentWorkflow(
        path=path.relative_to(root),
        trigger=trigger,
        exists=True,
        name=name if isinstance(name, str) and name else path.stem,
    )


def _trigger_names(document: dict[str, Any]) -> set[str]:
    value = document.get("on", document.get(True))
    if isinstance(value, str):
        return {value.casefold()}
    if isinstance(value, list):
        return {
            item.casefold()
            for item in value
            if isinstance(item, str)
        }
    if isinstance(value, dict):
        return {
            str(key).casefold()
            for key in value
        }
    return set()


def _is_merge_trigger(triggers: set[str], content: str) -> bool:
    return (
        "push" in triggers
        or "workflow_run" in triggers
        or (
            "pull_request" in triggers
            and "closed" in content
            and "merged" in content
        )
    )
