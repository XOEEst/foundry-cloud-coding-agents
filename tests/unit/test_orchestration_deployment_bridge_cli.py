from __future__ import annotations

import json
from pathlib import Path

import pytest

from foundry_opt.deployment import DEPLOYMENT_OIDC_CLIENT_ID
from foundry_opt.orchestration.deployment_bridge import (
    deployment_bridge_issue_numbers,
    require_deployment_identity,
    verify_active_deployment_identity,
    verify_deployment_workflow_run,
)
from foundry_opt.preflight.interfaces import CommandResult


class Commands:
    def __init__(self, client_id: str) -> None:
        self.client_id = client_id

    def run(self, arguments, *, cwd=None, **kwargs) -> CommandResult:
        assert tuple(arguments[:3]) == ("az", "account", "show")
        return CommandResult(
            0,
            json.dumps(
                {
                    "subscription": "subscription-id",
                    "tenant": "tenant-id",
                    "userName": self.client_id,
                    "userType": "servicePrincipal",
                }
            ),
            "",
        )


def test_deployment_bridge_selects_validated_persisted_issue_scope() -> None:
    require_deployment_identity(DEPLOYMENT_OIDC_CLIENT_ID)

    assert deployment_bridge_issue_numbers(
        requested_issue="31",
        state_ref="main",
        tracked=(31, 42),
    ) == (31,)
    assert deployment_bridge_issue_numbers(
        requested_issue=None,
        state_ref="foundry-opt/state/issue-42",
        tracked=(31, 42),
    ) == (42,)
    assert deployment_bridge_issue_numbers(
        requested_issue=None,
        state_ref="main",
        tracked=(42, 31, 31),
    ) == (31, 42)

    with pytest.raises(ValueError, match="deployment OIDC identity"):
        require_deployment_identity("optimizer-client")
    with pytest.raises(ValueError, match="issue number"):
        deployment_bridge_issue_numbers(
            requested_issue="31;echo owned",
            state_ref="main",
            tracked=(31,),
        )
    with pytest.raises(ValueError, match="not tracked"):
        deployment_bridge_issue_numbers(
            requested_issue="99",
            state_ref="main",
            tracked=(31,),
        )
    with pytest.raises(ValueError, match="not tracked"):
        deployment_bridge_issue_numbers(
            requested_issue=None,
            state_ref="foundry-opt/state/issue-99",
            tracked=(31,),
        )


def test_deployment_bridge_verifies_active_azure_principal() -> None:
    environment = {
        "AZURE_CLIENT_ID": DEPLOYMENT_OIDC_CLIENT_ID,
        "AZURE_SUBSCRIPTION_ID": "subscription-id",
        "AZURE_TENANT_ID": "tenant-id",
    }

    verify_active_deployment_identity(
        Commands(DEPLOYMENT_OIDC_CLIENT_ID),
        Path("."),
        environment,
    )

    with pytest.raises(ValueError, match="active Azure principal"):
        verify_active_deployment_identity(
            Commands("optimizer-client"),
            Path("."),
            environment,
        )


def test_deployment_publication_verifies_github_workflow_run() -> None:
    class RunCommands:
        def run(self, arguments, *, cwd=None, **kwargs) -> CommandResult:
            assert tuple(arguments) == (
                "gh",
                "api",
                "repos/octo-org/agents/actions/runs/9001",
            )
            return CommandResult(
                0,
                json.dumps(
                    {
                        "actor": {"login": "deployment-bot"},
                        "conclusion": "success",
                        "display_title": "deployment-effect-1",
                        "event": "workflow_dispatch",
                        "head_branch": "main",
                        "head_sha": "d" * 40,
                        "html_url": (
                            "https://github.com/octo-org/agents/actions/"
                            "runs/9001"
                        ),
                        "id": 9001,
                        "repository": {
                            "full_name": "octo-org/agents",
                        },
                        "run_attempt": 1,
                        "status": "completed",
                        "workflow_id": 77,
                        "path": ".github/workflows/deploy.yml",
                    }
                ),
                "",
            )

    verify_deployment_workflow_run(
        RunCommands(),
        Path("."),
        repository="octo-org/agents",
        run_id=9001,
        run_url=(
            "https://github.com/octo-org/agents/actions/runs/9001"
        ),
        run_actor="deployment-bot",
        effect_id="deployment-effect-1",
        expected_actor="deployment-bot",
        merge_commit="d" * 40,
        workflow_id=77,
        workflow_path=Path(".github/workflows/deploy.yml"),
        workflow_ref="refs/heads/main",
        workflow_trigger="manual",
    )
