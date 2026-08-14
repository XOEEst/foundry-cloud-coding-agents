from collections.abc import Callable
import json
from typing import Protocol

from foundry_opt.adapters.commands import CommandError, SubprocessCommandRunner
from foundry_opt.adapters.environment import OsEnvironmentReader
from foundry_opt.adapters.foundry import (
    AzureCliCredentialProvider,
    ClientSecretCredentialProvider,
    FoundryGateway as AzureFoundryGateway,
)
from foundry_opt.adapters.github import GhGitHubGateway
from foundry_opt.config import OptimizerConfig
from foundry_opt.config.models import AuthenticationMode
from foundry_opt.preflight.foundry_checks import FoundryAccessCheck
from foundry_opt.preflight.github_credentials import (
    AssignmentCredentialScopeCheck,
)
from foundry_opt.preflight.interfaces import (
    CommandRunner,
    EnvironmentReader,
    FoundryGateway,
    GitHubGateway,
)
from foundry_opt.preflight.models import CheckResult, CheckStatus, PreflightRequest
from foundry_opt.preflight.runner import PreflightRunner
from foundry_opt.preflight.runtime_checks import (
    GitHubDefaultBranchGateway,
    GitHubAccessCheck,
    build_runtime_checks,
)


_AZURE_OIDC_NAMES = (
    "AZURE_TENANT_ID",
    "AZURE_CLIENT_ID",
    "AZURE_SUBSCRIPTION_ID",
)
_AZURE_CLIENT_SECRET_NAMES = (
    "AZURE_TENANT_ID",
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
)


class ProductionGitHubGateway(
    GitHubGateway,
    GitHubDefaultBranchGateway,
    Protocol,
):
    pass


class TargetSelectionCheck:
    def __init__(self, config: OptimizerConfig, request: PreflightRequest) -> None:
        self._result = self._validate(config, request)
        self.check_id = self._result.check_id

    def run(self, request: PreflightRequest) -> CheckResult:
        del request
        return self._result

    @staticmethod
    def _validate(
        config: OptimizerConfig,
        request: PreflightRequest,
    ) -> CheckResult:
        target = config.targets.get(request.target)
        if target is None:
            return CheckResult(
                check_id="selection.target",
                status=CheckStatus.FAIL,
                summary="Selected optimization target was not found",
                detail=f"Target: {request.target}",
                remediation="Choose a target defined in the optimizer configuration.",
            )
        if target.environment != request.environment:
            return CheckResult(
                check_id="selection.environment",
                status=CheckStatus.FAIL,
                summary="Selected target does not use the requested environment",
                detail=(
                    f"Target {request.target} uses {target.environment}; "
                    f"requested {request.environment}"
                ),
                remediation=(
                    f"Use environment {target.environment} for target "
                    f"{request.target}."
                ),
            )
        return CheckResult(
            check_id="selection.target",
            status=CheckStatus.PASS,
            summary=f"Selected optimization target is valid ({request.target})",
        )


class AzureCredentialsCheck:
    check_id = "credentials.azure"

    def __init__(
        self,
        environment: EnvironmentReader,
        *,
        authentication_mode: AuthenticationMode | str,
        command_runner: CommandRunner,
    ) -> None:
        self._environment = environment
        self._authentication_mode = AuthenticationMode(authentication_mode)
        self._command_runner = command_runner

    def run(self, request: PreflightRequest) -> CheckResult:
        if self._authentication_mode is AuthenticationMode.OIDC:
            return self._check_oidc(request)

        del request
        missing = [
            name
            for name in _AZURE_CLIENT_SECRET_NAMES
            if not (self._environment.get(name) or "").strip()
        ]
        if missing:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAIL,
                summary="Azure service principal credentials are incomplete",
                detail=f"Missing: {', '.join(missing)}",
                remediation=(
                    "Set AZURE_TENANT_ID, AZURE_CLIENT_ID, and "
                    "AZURE_CLIENT_SECRET, then rerun preflight."
                ),
            )
        return CheckResult(
            check_id=self.check_id,
            status=CheckStatus.PASS,
            summary="Azure service principal credential variables are present",
        )

    def _check_oidc(self, request: PreflightRequest) -> CheckResult:
        expected = {
            name: (self._environment.get(name) or "").strip()
            for name in _AZURE_OIDC_NAMES
        }
        missing = [name for name, value in expected.items() if not value]
        if missing:
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAIL,
                summary="Azure OIDC configuration is incomplete",
                detail=f"Missing: {', '.join(missing)}",
                remediation=(
                    "Set the non-secret Azure tenant, client, and subscription "
                    "identifiers in the setup workflow."
                ),
            )

        try:
            raw = self._command_runner.run(
                [
                    "az",
                    "account",
                    "show",
                    "--query",
                    "{tenant:tenantId,subscription:id,client:user.name,"
                    "userType:user.type}",
                    "-o",
                    "json",
                ],
                cwd=request.repository_root,
            ).stdout
            account = json.loads(raw)
            if not isinstance(account, dict):
                raise TypeError
        except (CommandError, json.JSONDecodeError, TypeError):
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAIL,
                summary="Azure OIDC session is not available",
                remediation=(
                    "Run the pinned azure/login OIDC setup step before preflight."
                ),
            )

        actual = {
            "AZURE_TENANT_ID": account.get("tenant"),
            "AZURE_CLIENT_ID": account.get("client"),
            "AZURE_SUBSCRIPTION_ID": account.get("subscription"),
        }
        mismatched = [
            name
            for name, expected_value in expected.items()
            if str(actual.get(name) or "").casefold()
            != expected_value.casefold()
        ]
        if str(account.get("userType") or "").casefold() != "serviceprincipal":
            mismatched.append("AZURE_CLIENT_ID")
        if mismatched:
            names = ", ".join(sorted(set(mismatched)))
            return CheckResult(
                check_id=self.check_id,
                status=CheckStatus.FAIL,
                summary="Azure CLI session does not match the configured OIDC identity",
                detail=f"Mismatched: {names}",
                remediation="Rerun azure/login with the configured OIDC identity.",
            )

        return CheckResult(
            check_id=self.check_id,
            status=CheckStatus.PASS,
            summary="Azure OIDC session matches the configured identity",
        )


def build_production_preflight_runner(
    config: OptimizerConfig,
    request: PreflightRequest,
    *,
    command_runner: CommandRunner | None = None,
    environment: EnvironmentReader | None = None,
    github_gateway: ProductionGitHubGateway | None = None,
    foundry_gateway: FoundryGateway | None = None,
    executable_finder: Callable[[str], str | None] | None = None,
) -> PreflightRunner:
    command_runner = command_runner or SubprocessCommandRunner()
    environment = environment or OsEnvironmentReader()
    github_gateway = github_gateway or GhGitHubGateway(
        command_runner,
        require_admin=False,
    )
    selection_check = TargetSelectionCheck(config, request)
    if selection_check.run(request).status is CheckStatus.FAIL:
        return PreflightRunner((selection_check,))

    authentication_mode = config.environments[request.environment].authentication
    secrets = (
        tuple(
            value
            for name in ("AZURE_CLIENT_SECRET",)
            if (value := environment.get(name))
        )
        if authentication_mode is AuthenticationMode.CLIENT_SECRET
        else ()
    )
    if foundry_gateway is None:
        credential_provider = (
            AzureCliCredentialProvider(environment)
            if authentication_mode is AuthenticationMode.OIDC
            else ClientSecretCredentialProvider(environment)
        )
        foundry_gateway = AzureFoundryGateway(credential_provider)

    runtime_checks = build_runtime_checks(
        command_runner,
        github_gateway=github_gateway,
        require_az=authentication_mode is AuthenticationMode.OIDC,
        require_azd=False,
        finder=executable_finder,
    )
    checks = (
        *runtime_checks,
        AssignmentCredentialScopeCheck(),
        AzureCredentialsCheck(
            environment,
            authentication_mode=authentication_mode,
            command_runner=command_runner,
        ),
        GitHubAccessCheck(github_gateway),
        FoundryAccessCheck(
            foundry_gateway,
            config_loader=lambda _: config,
        ),
    )
    return PreflightRunner(checks, secrets=secrets)
