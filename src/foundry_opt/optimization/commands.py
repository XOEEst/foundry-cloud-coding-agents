from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping, Protocol


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class OptimizePhase(StrEnum):
    AUTO = "auto"
    SPEC = "spec"
    RUN = "run"
    CANDIDATE_REQUEST = "candidate-request"
    CANDIDATE_SUBMIT = "candidate-submit"
    APPLY = "apply"
    RECONCILE = "reconcile"


class OptimizeCommandStatus(StrEnum):
    COMPLETE = "complete"
    AWAITING_AGENT = "awaiting_agent"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True)
class OptimizeCommandRequest:
    repository_root: Path
    issue_number: int
    phase: OptimizePhase
    candidate_id: str | None = None
    idea_file: Path | None = None
    verify_only: bool = False

    def __post_init__(self) -> None:
        if self.issue_number < 1:
            raise ValueError("issue_number must be positive")
        if self.phase in (
            OptimizePhase.APPLY,
            OptimizePhase.CANDIDATE_SUBMIT,
        ):
            if (
                self.candidate_id is None
                or not _IDENTIFIER.fullmatch(self.candidate_id)
            ):
                raise ValueError(
                    "candidate_id is required for the "
                    f"{self.phase.value} phase"
                )
        elif self.candidate_id is not None:
            raise ValueError(
                "candidate_id is only valid for the apply and "
                "candidate-submit phases"
            )
        if self.phase is OptimizePhase.CANDIDATE_SUBMIT:
            if self.idea_file is None:
                raise ValueError(
                    "idea_file is required for the candidate-submit phase"
                )
        elif self.idea_file is not None:
            raise ValueError(
                "idea_file is only valid for the candidate-submit phase"
            )
        if self.verify_only and self.phase is not OptimizePhase.APPLY:
            raise ValueError(
                "verify_only is only valid for the apply phase"
            )


@dataclass(frozen=True)
class OptimizeCommandResult:
    status: OptimizeCommandStatus
    phase: OptimizePhase
    summary: str
    issue_number: int
    details: Mapping[str, Any] = field(default_factory=dict)
    next_action: str | None = None

    def __post_init__(self) -> None:
        if self.issue_number < 1:
            raise ValueError("issue_number must be positive")
        if not self.summary.strip():
            raise ValueError("summary is required")
        if self.next_action is not None and not self.next_action.strip():
            raise ValueError("next_action must not be blank when provided")
        object.__setattr__(
            self,
            "details",
            MappingProxyType(dict(self.details)),
        )

    @property
    def exit_code(self) -> int:
        if self.status in (
            OptimizeCommandStatus.COMPLETE,
            OptimizeCommandStatus.AWAITING_AGENT,
        ):
            return 0
        return 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "details": dict(self.details),
            "issue_number": self.issue_number,
            "next_action": self.next_action,
            "phase": self.phase.value,
            "status": self.status.value,
            "summary": self.summary,
        }


class OptimizationCommandService(Protocol):
    def execute(
        self,
        request: OptimizeCommandRequest,
    ) -> OptimizeCommandResult: ...


class UnavailableOptimizationCommandService:
    def execute(
        self,
        request: OptimizeCommandRequest,
    ) -> OptimizeCommandResult:
        return OptimizeCommandResult(
            status=OptimizeCommandStatus.BLOCKED,
            phase=request.phase,
            summary=(
                "The issue-driven optimization service is not configured "
                "in this build."
            ),
            issue_number=request.issue_number,
        )
