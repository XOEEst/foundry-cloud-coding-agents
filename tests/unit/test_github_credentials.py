from foundry_opt.adapters.github_credentials import (
    ActionsGitHubCredentialProvider,
    CopilotAssignmentCredentialProvider,
)


def test_actions_and_assignment_credentials_are_distinct_adapters() -> None:
    actions = ActionsGitHubCredentialProvider("actions-token")
    assignment = CopilotAssignmentCredentialProvider("assignment-token")

    assert actions.command_environment() == {"GH_TOKEN": "actions-token"}
    assert assignment.command_environment() == {
        "GH_TOKEN": "assignment-token"
    }
    assert ActionsGitHubCredentialProvider().command_environment() is None
