from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from foundry_opt.adapters.commands import CommandError
from foundry_opt.orchestration.workspace import (
    WorkspaceSelectionDecision,
    WorkspaceSelectionRequest,
)
from foundry_opt.preflight.interfaces import CommandRunner


_SUCCESS = frozenset({"ok", "pass", "passed", "success"})


class ConfiguredWorkspaceSelector:
    def __init__(
        self,
        commands: CommandRunner,
        *,
        repository_root: Path,
        repository: str,
        required_checks: tuple[str, ...],
        remote: str = "origin",
    ) -> None:
        if not required_checks or len(required_checks) != len(
            set(required_checks)
        ):
            raise ValueError(
                "workspace selection requires configured checks"
            )
        self._commands = commands
        self._root = repository_root
        self._repository = repository
        self._required_checks = required_checks
        self._remote = remote

    def select(
        self,
        request: WorkspaceSelectionRequest,
    ) -> WorkspaceSelectionDecision:
        policy = request.report_context.policy
        baseline = request.report_context.baseline_metrics
        candidates = {
            item.candidate_id: item for item in request.experiments
        }
        eligible: list[tuple[float, str]] = []
        rejected: dict[str, str] = {}
        for candidate_id, result in candidates.items():
            missing = [
                metric.name
                for metric in policy.metrics
                if metric.name not in result.metrics
                or metric.name not in baseline
            ]
            if missing:
                rejected[candidate_id] = (
                    "Required trusted metrics are missing."
                )
                continue
            failed = [
                metric.name
                for metric in policy.metrics
                if not metric.passes(result.metrics[metric.name])
            ]
            if failed:
                rejected[candidate_id] = (
                    "Configured metric thresholds were not met."
                )
                continue
            if any(
                status.strip().casefold() not in _SUCCESS
                for status in result.guardrails.values()
            ):
                rejected[candidate_id] = (
                    "A trusted experiment guardrail did not pass."
                )
                continue
            improvements = tuple(
                metric.improvement(
                    baseline[metric.name],
                    result.metrics[metric.name],
                )
                for metric in policy.metrics
            )
            if not any(
                improvement >= metric.materiality
                for improvement, metric in zip(
                    improvements,
                    policy.metrics,
                    strict=True,
                )
            ):
                rejected[candidate_id] = (
                    "No configured metric improved materially."
                )
                continue
            eligible.append((sum(improvements), candidate_id))
        if not eligible:
            raise ValueError(
                "workspace policy found no eligible candidate"
            )
        selected_id = sorted(
            eligible,
            key=lambda item: (-item[0], item[1]),
        )[0][1]
        selected = next(
            item
            for item in request.candidates
            if item.experiment.candidate_id == selected_id
        )
        checks = self._trusted_checks(
            issue_number=request.issue.number,
            expected_tree=selected.expected_tree,
        )
        return WorkspaceSelectionDecision(
            selected_candidate_id=selected_id,
            eligible_candidate_ids=tuple(
                candidate_id
                for _, candidate_id in sorted(
                    eligible,
                    key=lambda item: item[1],
                )
            ),
            recommendation=(
                f"Select {selected_id}; it is the strongest candidate "
                "allowed by the configured metric and guardrail policy."
            ),
            rejection_reasons=rejected,
            required_checks=checks,
        )

    def _trusted_checks(
        self,
        *,
        issue_number: int,
        expected_tree: str,
    ) -> dict[str, str]:
        branch = f"foundry-opt/workspace/issue-{issue_number}"
        try:
            document = json.loads(
                self._commands.run(
                    (
                        "gh",
                        "pr",
                        "view",
                        branch,
                        "--repo",
                        self._repository,
                        "--json",
                        "headRefOid,statusCheckRollup",
                    ),
                    cwd=self._root,
                ).stdout
            )
            if not isinstance(document, dict):
                raise RuntimeError(
                    "workspace trusted checks response is invalid"
                )
            head = document.get("headRefOid")
            checks = document.get("statusCheckRollup")
            if (
                not isinstance(head, str)
                or re.fullmatch(r"[0-9a-f]{40}", head) is None
                or not isinstance(checks, list)
            ):
                raise RuntimeError(
                    "workspace trusted checks response is invalid"
                )
            self._commands.run(
                ("git", "fetch", "--no-tags", self._remote, head),
                cwd=self._root,
            )
            tree = self._commands.run(
                ("git", "rev-parse", "--verify", f"{head}^{{tree}}"),
                cwd=self._root,
            ).stdout.strip()
        except (CommandError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "workspace trusted checks are unavailable"
            ) from error
        if tree != expected_tree:
            return {
                name: "pending" for name in self._required_checks
            }
        by_name: dict[str, str] = {}
        for item in checks:
            if not isinstance(item, dict):
                raise RuntimeError(
                    "workspace trusted checks response is invalid"
                )
            name = item.get("name") or item.get("context")
            if not isinstance(name, str) or name not in self._required_checks:
                continue
            state = _check_state(item)
            previous = by_name.get(name)
            if previous is not None and previous != state:
                raise RuntimeError(
                    "workspace trusted check results are ambiguous"
                )
            by_name[name] = state
        return {
            name: by_name.get(name, "pending")
            for name in self._required_checks
        }


def _check_state(value: dict[str, Any]) -> str:
    conclusion = value.get("conclusion")
    status = value.get("status")
    state = value.get("state")
    normalized = (
        conclusion.casefold()
        if isinstance(conclusion, str) and conclusion
        else state.casefold()
        if isinstance(state, str) and state
        else ""
    )
    if normalized == "success" and (
        not isinstance(status, str)
        or status.casefold() == "completed"
    ):
        return "success"
    if normalized in {
        "cancelled",
        "failure",
        "neutral",
        "skipped",
        "stale",
        "timed_out",
    }:
        return "failure"
    return "pending"


__all__ = ["ConfiguredWorkspaceSelector"]
