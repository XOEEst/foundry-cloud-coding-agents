from __future__ import annotations

import subprocess
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
    default_branch = _default_branch(root)
    candidates: list[DeploymentWorkflow] = []
    workflow_root = root / ".github" / "workflows"
    if workflow_root.is_dir():
        paths = sorted(
            (*workflow_root.glob("*.yml"), *workflow_root.glob("*.yaml"))
        )
        for path in paths:
            candidate = _workflow_candidate(root, path, default_branch)
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
    default_branch: str | None,
) -> DeploymentWorkflow | None:
    try:
        content = path.read_text(encoding="utf-8")
        document = yaml.safe_load(content) or {}
    except (OSError, UnicodeError, yaml.YAMLError):
        return None
    if not isinstance(document, dict):
        return None
    lowered = f"{path.name}\n{content}".casefold()
    if any(
        marker in lowered
        for marker in ("az acr", "docker build", ".azurecr.io")
    ):
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
    triggers = _trigger_configuration(document)
    if _is_default_branch_merge_trigger(triggers, default_branch):
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


def _trigger_configuration(document: dict[str, Any]) -> dict[str, Any]:
    value = document.get("on", document.get(True))
    if isinstance(value, str):
        return {value.casefold(): None}
    if isinstance(value, list):
        return {
            item.casefold(): None
            for item in value
            if isinstance(item, str)
        }
    if isinstance(value, dict):
        return {
            str(key).casefold(): child
            for key, child in value.items()
        }
    return {}


def _is_default_branch_merge_trigger(
    triggers: dict[str, Any],
    default_branch: str | None,
) -> bool:
    if default_branch is None:
        return False
    push = triggers.get("push", _MISSING)
    if push is not _MISSING and _only_branch(push, default_branch):
        return True
    workflow_run = triggers.get("workflow_run", _MISSING)
    return (
        workflow_run is not _MISSING
        and isinstance(workflow_run, dict)
        and _completed(workflow_run.get("types"))
        and _only_branch(workflow_run, default_branch)
    )


def _only_branch(configuration: Any, default_branch: str) -> bool:
    if not isinstance(configuration, dict):
        return False
    branches = configuration.get("branches")
    if isinstance(branches, str):
        branch_values = (branches,)
    elif isinstance(branches, list):
        branch_values = tuple(
            value for value in branches if isinstance(value, str)
        )
    else:
        return False
    return branch_values == (default_branch,)


def _completed(value: Any) -> bool:
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, list):
        values = tuple(item for item in value if isinstance(item, str))
    else:
        return False
    return "completed" in {item.casefold() for item in values}


def _default_branch(root: Path) -> str | None:
    remote = _git_output(
        root,
        "symbolic-ref",
        "--quiet",
        "--short",
        "refs/remotes/origin/HEAD",
    )
    if remote and "/" in remote:
        return remote.split("/", 1)[1]
    branches = _git_output(
        root,
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads",
    )
    values = tuple(line for line in branches.splitlines() if line)
    return values[0] if len(values) == 1 else None


def _git_output(root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ("git", *arguments),
            cwd=root,
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


_MISSING = object()
