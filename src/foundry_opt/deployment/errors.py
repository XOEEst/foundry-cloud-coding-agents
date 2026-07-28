class DeploymentError(RuntimeError):
    """Base class for stable, redacted deployment failures."""


class DeploymentAuthenticationError(DeploymentError):
    def __init__(self) -> None:
        super().__init__("Azure authentication failed during deployment.")


class DeploymentAuthorizationError(DeploymentError):
    def __init__(self) -> None:
        super().__init__(
            "The deployment identity cannot publish Foundry versions."
        )


class DeploymentIdentityError(DeploymentError):
    def __init__(self) -> None:
        super().__init__(
            "The active Azure principal is not the configured deployment "
            "OIDC identity."
        )


class DeploymentApiError(DeploymentError):
    def __init__(self, status_code: int | None = None) -> None:
        self.status_code = status_code
        suffix = (
            f" with status {status_code}"
            if isinstance(status_code, int)
            else ""
        )
        super().__init__(f"The Foundry deployment API request failed{suffix}.")


class DeploymentConflictError(DeploymentApiError):
    def __init__(self) -> None:
        super().__init__(409)


class DeploymentResponseError(DeploymentError):
    def __init__(self) -> None:
        super().__init__(
            "Foundry did not return a confirmed published version."
        )


class DeploymentStatusError(DeploymentError):
    def __init__(self, status: str | None = None) -> None:
        self.status = status
        super().__init__(
            "The published Foundry version did not reach a successful "
            "terminal status."
        )


class DeploymentHashMismatchError(DeploymentError):
    def __init__(self) -> None:
        super().__init__("Deployment source or lineage hash verification failed.")


class DeploymentLineageMismatchError(DeploymentError):
    def __init__(self) -> None:
        super().__init__(
            "Deployment optimization lineage does not match the expected "
            "issue, spec, campaign, candidate, or selected commit "
            "provenance."
        )
