from typing import Protocol

from foundry_opt.deployment.models import (
    DeploymentRecord,
    DeploymentRequest,
)


class DeploymentGateway(Protocol):
    def publish(self, request: DeploymentRequest) -> DeploymentRecord: ...
