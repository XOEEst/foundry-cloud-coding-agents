from pathlib import Path

from foundry_opt.preflight.github_credentials import (
    AssignmentCredentialScopeCheck,
    assert_assignment_credential_scope,
    assignment_credential_scope_violations,
)
from foundry_opt.preflight.models import CheckStatus, PreflightRequest


def _request(root: Path) -> PreflightRequest:
    return PreflightRequest(
        repository_root=root,
        config_path=Path(".github/foundry-optimizer.yaml"),
        environment="acceptance",
        target="support-agent",
    )


def test_assignment_secret_is_allowed_only_in_narrow_generated_steps() -> None:
    workflow = """
name: safe
jobs:
  invoke:
    steps:
      - name: Resume same workspace pull request when trusted state needs Copilot
        env:
          COPILOT_ASSIGNMENT_TOKEN: ${{ secrets.COPILOT_ASSIGNMENT_TOKEN }}
        run: foundry-opt workspace assign --issue 1
      - name: Remove transient Copilot assignment marker after verified provenance capture
        env:
          COPILOT_ASSIGNMENT_TOKEN: ${{ secrets.COPILOT_ASSIGNMENT_TOKEN }}
        run: foundry-opt workspace cleanup-assignment --issue 1
      - name: Ingest trusted event or retry the workspace
        env:
          FOUNDRY_OPT_WORKSPACE_PR_BOOTSTRAP_TOKEN: ${{ secrets.COPILOT_ASSIGNMENT_TOKEN }}
          GH_TOKEN: ${{ github.token }}
        run: foundry-opt workspace intake --issue 1
"""

    workflows = {Path(".github/workflows/safe.yml"): workflow}

    assert assignment_credential_scope_violations(workflows) == ()
    assert_assignment_credential_scope(workflows)


def test_assignment_secret_as_generic_gh_token_fails_without_reading_value(
    tmp_path: Path,
) -> None:
    workflow_root = tmp_path / ".github" / "workflows"
    workflow_root.mkdir(parents=True)
    (workflow_root / "unsafe.yml").write_text(
        """
name: unsafe
jobs:
  write:
    env:
      GH_TOKEN: ${{ secrets.COPILOT_ASSIGNMENT_TOKEN }}
    steps:
      - run: gh issue edit 1
""",
        encoding="utf-8",
    )

    result = AssignmentCredentialScopeCheck().run(_request(tmp_path))

    assert result.status is CheckStatus.FAIL
    assert "unsafe.yml" in (result.detail or "")
    assert "GH_TOKEN" in (result.detail or "")
    assert "secret value" not in str(result).casefold()
    assert "github.token as GH_TOKEN" in (result.remediation or "")


def test_assignment_secret_bootstrap_alias_fails_outside_intake() -> None:
    workflow = """
name: unsafe
jobs:
  write:
    steps:
      - name: Update issue
        env:
          FOUNDRY_OPT_WORKSPACE_PR_BOOTSTRAP_TOKEN: ${{ secrets.COPILOT_ASSIGNMENT_TOKEN }}
        run: gh issue edit 1
"""

    violations = assignment_credential_scope_violations(
        {Path(".github/workflows/unsafe.yml"): workflow}
    )

    assert violations
    assert "FOUNDRY_OPT_WORKSPACE_PR_BOOTSTRAP_TOKEN" in violations[0]
