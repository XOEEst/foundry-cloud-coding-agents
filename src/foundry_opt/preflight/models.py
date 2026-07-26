from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class CheckStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIP = "skip"


@dataclass(frozen=True)
class PreflightRequest:
    repository_root: Path
    config_path: Path
    environment: str
    target: str


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    status: CheckStatus
    summary: str
    detail: str | None = None
    remediation: str | None = None
    duration_ms: int = 0


@dataclass(frozen=True)
class PreflightReport:
    results: tuple[CheckResult, ...]

    @property
    def passed(self) -> bool:
        return all(result.status is not CheckStatus.FAIL for result in self.results)

    @property
    def exit_code(self) -> int:
        return 0 if self.passed else 1
