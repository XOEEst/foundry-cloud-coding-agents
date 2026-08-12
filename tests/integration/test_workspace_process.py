from __future__ import annotations

import json
import subprocess
import sys


def test_workspace_advance_process_emits_workflow_json() -> None:
    script = r"""
from foundry_opt.orchestration import (
    WorkspaceIssueStatusProjectionIntent,
    WorkspacePhase,
    WorkspacePullRequest,
    WorkspaceResult,
)
import foundry_opt.cli as cli

class Service:
    def advance(self, request):
        return WorkspaceResult(
            phase=WorkspacePhase.SPECIFICATION,
            workspace_pull_request=WorkspacePullRequest(
                number=104,
                issue_number=request.issue_number,
                branch=f"foundry-opt/workspace/issue-{request.issue_number}",
                title=(
                    f"[Optimize] #{request.issue_number} workspace - "
                    "draft, not yet selectable"
                ),
                draft=True,
                reuse_existing=True,
                base_commit="a" * 40,
            ),
            planned_effect_kinds=("workspace_pr_sync",),
            recorded=False,
            issue_status_projection_intent=WorkspaceIssueStatusProjectionIntent(
                issue_number=request.issue_number,
                phase=WorkspacePhase.SPECIFICATION,
                workspace_pull_request_number=104,
            ),
        )

cli.build_workspace_service = lambda: Service()
sys.argv = ["foundry-opt", "workspace", "advance", "--issue", "31", "--json"]
cli.app()
"""
    completed = subprocess.run(
        [sys.executable, "-c", "import sys\n" + script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    document = json.loads(completed.stdout)
    assert document["workspace_pull_request"]["number"] == 104
    assert document["recorded"] is False
    assert "Traceback" not in completed.stderr
