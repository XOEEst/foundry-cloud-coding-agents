from collections.abc import Callable
from pathlib import Path
from time import perf_counter

from foundry_opt.adapters.foundry import (
    FoundryAccessError,
    FoundryAuthenticationError,
    FoundryAuthorizationError,
    FoundryEndpointError,
    FoundryMissingCredentialsError,
    FoundryServiceError,
    FoundryThrottledError,
    FoundryTransportError,
    FoundryUnexpectedSdkError,
)
from foundry_opt.config import ConfigLoadError, OptimizerConfig, load_config
from foundry_opt.preflight.interfaces import FoundryGateway
from foundry_opt.preflight.models import CheckResult, CheckStatus, PreflightRequest


ConfigLoader = Callable[[Path], OptimizerConfig]


class FoundryAccessCheck:
    check_id = "foundry.access"

    def __init__(
        self,
        gateway: FoundryGateway,
        *,
        config_loader: ConfigLoader = load_config,
    ) -> None:
        self._gateway = gateway
        self._config_loader = config_loader

    def run(self, request: PreflightRequest) -> CheckResult:
        started = perf_counter()
        config_path = request.config_path
        if not config_path.is_absolute():
            config_path = request.repository_root / config_path

        try:
            config = self._config_loader(config_path)
        except ConfigLoadError:
            return self._result(
                started,
                status=CheckStatus.FAIL,
                summary="Foundry configuration could not be loaded",
                remediation=(
                    "Fix the optimizer configuration diagnostics, then rerun preflight."
                ),
            )

        environment = config.environments.get(request.environment)
        if environment is None:
            return self._result(
                started,
                status=CheckStatus.FAIL,
                summary="Configured Foundry environment was not found",
                remediation=(
                    "Choose an environment defined in the optimizer configuration."
                ),
            )

        try:
            result = self._gateway.verify_access(str(environment.project_endpoint))
        except FoundryMissingCredentialsError:
            return self._failure(
                started,
                "Configured Azure authentication is incomplete",
                "Complete the configured Azure authentication setup before preflight.",
            )
        except FoundryAuthenticationError:
            return self._failure(
                started,
                "Foundry authentication failed",
                "Verify the configured Azure identity and rerun its login setup.",
            )
        except FoundryAuthorizationError:
            return self._failure(
                started,
                "Foundry authorization failed",
                (
                    "Grant the service principal the Foundry User role on the "
                    "configured Foundry project."
                ),
            )
        except FoundryEndpointError:
            return self._failure(
                started,
                "Foundry project endpoint is invalid or unreachable",
                (
                    "Correct project_endpoint to the HTTPS Foundry project URL and "
                    "confirm that the project still exists."
                ),
            )
        except FoundryThrottledError:
            return self._failure(
                started,
                "Foundry access check was throttled",
                "Wait briefly and retry the preflight check.",
            )
        except FoundryTransportError:
            return self._failure(
                started,
                "Could not connect to the Foundry service",
                (
                    "Check DNS, proxy, firewall, and private network access from "
                    "the current runner."
                ),
            )
        except FoundryServiceError:
            return self._failure(
                started,
                "Foundry service could not complete the access check",
                (
                    "Check Azure service health and the project status, then retry "
                    "the preflight check."
                ),
            )
        except FoundryUnexpectedSdkError:
            return self._failure(
                started,
                "Unexpected Foundry SDK failure",
                (
                    "Confirm the installed Azure AI Projects SDK is supported, then "
                    "rerun preflight."
                ),
            )
        except FoundryAccessError:
            return self._failure(
                started,
                "Foundry access check failed",
                "Review the Foundry project configuration and retry preflight.",
            )
        except Exception:
            return self._failure(
                started,
                "Unexpected Foundry access check failure",
                "Review the preflight logs after redaction and retry.",
            )

        return self._result(
            started,
            status=CheckStatus.PASS,
            summary=result.summary,
            detail=result.detail,
        )

    def _failure(
        self,
        started: float,
        summary: str,
        remediation: str,
    ) -> CheckResult:
        return self._result(
            started,
            status=CheckStatus.FAIL,
            summary=summary,
            remediation=remediation,
        )

    def _result(
        self,
        started: float,
        *,
        status: CheckStatus,
        summary: str,
        detail: str | None = None,
        remediation: str | None = None,
    ) -> CheckResult:
        return CheckResult(
            check_id=self.check_id,
            status=status,
            summary=summary,
            detail=detail,
            remediation=remediation,
            duration_ms=max(0, round((perf_counter() - started) * 1000)),
        )
