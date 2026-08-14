from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from foundry_opt.preflight.models import CheckResult, CheckStatus, PreflightRequest


_ASSIGNMENT_SECRET = "COPILOT_ASSIGNMENT_TOKEN"
_ASSIGNMENT_EXPRESSION = "secrets.COPILOT_ASSIGNMENT_TOKEN"
_ALLOWED_STEP_NAMES = frozenset(
    {
        "Remove transient Copilot assignment marker after verified "
        "provenance capture",
        "Resume same workspace pull request when trusted state needs Copilot",
    }
)


def assignment_credential_scope_violations(
    workflows: Mapping[Path, str],
) -> tuple[str, ...]:
    violations: list[str] = []
    for path, content in sorted(
        workflows.items(),
        key=lambda item: item[0].as_posix(),
    ):
        if _ASSIGNMENT_SECRET not in content:
            continue
        try:
            document = yaml.safe_load(content)
        except yaml.YAMLError:
            violations.append(f"{path.as_posix()}: invalid workflow YAML")
            continue
        if not isinstance(document, Mapping):
            violations.append(f"{path.as_posix()}: invalid workflow document")
            continue
        for location, value in _walk(document):
            if (
                _ASSIGNMENT_SECRET not in location
                and not (
                    isinstance(value, str)
                    and _ASSIGNMENT_EXPRESSION in value
                )
            ):
                continue
            if not _allowed_assignment_reference(document, location, value):
                rendered = ".".join(str(part) for part in location)
                violations.append(f"{path.as_posix()}:{rendered}")
    return tuple(violations)


def assert_assignment_credential_scope(
    workflows: Mapping[Path, str],
) -> None:
    violations = assignment_credential_scope_violations(workflows)
    if violations:
        raise ValueError(
            "COPILOT_ASSIGNMENT_TOKEN must be step-scoped to Copilot "
            "invocation or verified assignment-comment cleanup and must "
            f"never be general GH_TOKEN ({'; '.join(violations)})"
        )


class AssignmentCredentialScopeCheck:
    check_id = "credentials.copilot_assignment_scope"

    def run(self, request: PreflightRequest) -> CheckResult:
        workflow_root = request.repository_root / ".github" / "workflows"
        workflows: dict[Path, str] = {}
        if workflow_root.is_dir():
            paths = (
                *workflow_root.glob("*.yml"),
                *workflow_root.glob("*.yaml"),
            )
            for path in sorted(paths):
                relative = path.relative_to(request.repository_root)
                workflows[relative] = path.read_text(
                    encoding="utf-8",
                )
        violations = assignment_credential_scope_violations(workflows)
        if violations:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAIL,
                summary=(
                    "Copilot assignment credential is used outside its "
                    "narrow scope"
                ),
                detail="Unsafe references: " + ", ".join(violations),
                remediation=(
                    "Keep COPILOT_ASSIGNMENT_TOKEN only in the generated "
                    "Copilot invocation and verified assignment-comment cleanup "
                    "steps. Use github.token as GH_TOKEN for durable repository "
                    "operations."
                ),
            )
        return CheckResult(
            check_id=self.check_id,
            status=CheckStatus.PASS,
            summary=(
                "Copilot assignment credential is isolated from durable "
                "GitHub operations"
            ),
        )


def _walk(
    node: Any,
    path: tuple[str | int, ...] = (),
):
    if isinstance(node, Mapping):
        for key, child in node.items():
            location = (*path, str(key))
            yield location, child
            yield from _walk(child, location)
    elif isinstance(node, list):
        for index, child in enumerate(node):
            location = (*path, index)
            yield location, child
            yield from _walk(child, location)


def _allowed_assignment_reference(
    document: Mapping[str, Any],
    location: tuple[str | int, ...],
    value: Any,
) -> bool:
    if len(location) != 6:
        return False
    jobs, job_name, steps, step_index, env, name = location
    if (
        jobs != "jobs"
        or not isinstance(job_name, str)
        or steps != "steps"
        or not isinstance(step_index, int)
        or env != "env"
        or name != _ASSIGNMENT_SECRET
        or not isinstance(value, str)
        or _ASSIGNMENT_EXPRESSION not in value
    ):
        return False
    job = document.get("jobs", {}).get(job_name, {})
    step_items = job.get("steps", []) if isinstance(job, Mapping) else []
    if not isinstance(step_items, list) or step_index >= len(step_items):
        return False
    step = step_items[step_index]
    return (
        isinstance(step, Mapping)
        and step.get("name") in _ALLOWED_STEP_NAMES
    )


__all__ = [
    "AssignmentCredentialScopeCheck",
    "assert_assignment_credential_scope",
    "assignment_credential_scope_violations",
]
