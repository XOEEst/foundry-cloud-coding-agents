from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from foundry_opt.preflight.models import CheckResult, PreflightRequest


class PreflightCheck(Protocol):
    check_id: str

    def run(self, request: PreflightRequest) -> CheckResult: ...


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
    ) -> CommandResult: ...


class EnvironmentReader(Protocol):
    def get(self, name: str) -> str | None: ...


@dataclass(frozen=True)
class GatewayResult:
    summary: str
    detail: str | None = None


class GitHubGateway(Protocol):
    def verify_access(self, repository_root: Path) -> GatewayResult: ...


class FoundryGateway(Protocol):
    def verify_access(self, project_endpoint: str) -> GatewayResult: ...
