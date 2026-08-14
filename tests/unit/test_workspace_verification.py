from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from foundry_opt.evaluation import MetricDirection, MetricPolicy
from foundry_opt.orchestration.workspace import WorkspacePhase
from foundry_opt.orchestration.workspace_store import (
    CandidateSummary,
    WorkspaceExperimentRecord,
    WorkspaceLineage,
    WorkspaceSnapshot,
)
from foundry_opt.orchestration.workspace_verification import (
    WorkspaceVerificationService,
    WorkspaceVerifiedIssue,
    WorkspaceVerifyRequest,
)


class StaticStore:
    def __init__(self, snapshot: WorkspaceSnapshot | None) -> None:
        self.snapshot = snapshot

    def load(self, issue_number: int) -> WorkspaceSnapshot | None:
        if self.snapshot is None or issue_number != self.snapshot.issue_number:
            return None
        return self.snapshot


class StaticIssueLoader:
    def __init__(self, issue: WorkspaceVerifiedIssue) -> None:
        self.issue = issue

    def load(
        self,
        repository_root: Path,
        repository: str,
        issue_number: int,
    ) -> WorkspaceVerifiedIssue:
        assert issue_number == 31
        assert repository_root.name == "repo"
        assert repository == "octo-org/optimizer"
        return self.issue


class StaticRepositoryResolver:
    def resolve(self, repository_root: Path) -> str:
        assert repository_root.name == "repo"
        return "octo-org/optimizer"


class StaticHeadTreeResolver:
    def __init__(self, tree: str) -> None:
        self.tree = tree

    def resolve(
        self,
        repository_root: Path,
        head_sha: str | None,
    ) -> str:
        assert repository_root.name == "repo"
        assert head_sha == "a" * 40
        return self.tree


def _snapshot() -> WorkspaceSnapshot:
    patch = b"diff --git a/agent.py b/agent.py\n"
    patch_sha256 = hashlib.sha256(patch).hexdigest()
    return WorkspaceSnapshot(
        issue_number=31,
        revision="c" * 40,
        phase=WorkspacePhase.AWAITING_SELECTION,
        workspace_pull_request_number=104,
        candidates=(
            CandidateSummary(
                candidate_id="candidate-2",
                metrics={"quality": 0.9, "safety": 1.0},
                eligible=True,
                selected=True,
            ),
        ),
        selected_patch=patch,
        external_operation_ids=(
            f"candidate-2:patch:{patch_sha256}",
            f"candidate-2:bundle:{'2' * 64}",
            f"candidate-2:evidence:{'3' * 64}",
            f"candidate-2:tree:{'b' * 40}",
        ),
        experiments=(
            WorkspaceExperimentRecord(
                candidate_id="candidate-2",
                mutation_class="system_instructions",
                patch_sha256=patch_sha256,
                bundle_sha256="2" * 64,
                evidence_sha256="3" * 64,
                idempotency_key="4" * 64,
                operation_sha256="5" * 64,
                status="completed",
                changed_paths=("agent.py",),
                validation=("pytest: passed",),
                expected_tree="b" * 40,
                executor="direct_oidc",
                draft_id="draft-2",
                evaluation_id="evaluation-2",
                run_id="run-2",
                metrics={"quality": 0.9, "safety": 1.0},
                guardrails={"safety": "pass"},
            ),
        ),
        lineage=WorkspaceLineage(
            spec_sha256="6" * 64,
            base_commit="7" * 40,
            patch_sha256=patch_sha256,
            evidence_sha256="3" * 64,
            bundle_sha256="2" * 64,
            expected_tree="b" * 40,
            selected_candidate_id="candidate-2",
            workspace_pull_request_number=104,
            required_checks={"exact-candidate": "success"},
            required_checks_provenance="trusted-selector:head:" + "a" * 40,
        ),
    )


def _issue() -> WorkspaceVerifiedIssue:
    return WorkspaceVerifiedIssue(
        repository="octo-org/optimizer",
        target="support-agent",
        metrics={
            "quality": MetricPolicy(
                name="quality",
                direction=MetricDirection.MAXIMIZE,
                threshold=0.7,
                materiality=0.1,
            ),
            "safety": MetricPolicy(
                name="safety",
                direction=MetricDirection.MAXIMIZE,
                threshold=1.0,
                materiality=0.0,
                hard_guardrail=True,
            ),
        },
    )


def test_workspace_verify_renders_trusted_metric_table_and_evidence_link(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    service = WorkspaceVerificationService(
        store=StaticStore(_snapshot()),
        issue_loader=StaticIssueLoader(_issue()),
        repository_resolver=StaticRepositoryResolver(),
        head_tree_resolver=StaticHeadTreeResolver("b" * 40),
    )

    result = service.verify(
        WorkspaceVerifyRequest(
            repository_root=repository_root,
            issue_number=31,
            candidate_id="candidate-2",
            workspace_pull_request_number=104,
            head_sha="a" * 40,
        )
    )

    assert result.status.value == "verified"
    assert result.evidence.url == (
        "https://github.com/octo-org/optimizer/blob/"
        + "c" * 40
        + "/evidence/candidates.json"
    )
    assert result.guardrails == {"safety": "pass"}
    assert "## Metric table" in result.summary_markdown
    assert "`quality` | 0.9 | 0.7 | 0.1 | n/a" in result.summary_markdown
    assert "`safety` | 1 | 1 | 0 | pass" in result.summary_markdown
    assert "## Guardrails" in result.summary_markdown
    assert "Guardrail `safety`: **pass**" in result.summary_markdown
    assert "Immutable evidence" in result.summary_markdown


def test_workspace_verify_fails_closed_on_exact_tree_mismatch(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    service = WorkspaceVerificationService(
        store=StaticStore(_snapshot()),
        issue_loader=StaticIssueLoader(_issue()),
        repository_resolver=StaticRepositoryResolver(),
        head_tree_resolver=StaticHeadTreeResolver("d" * 40),
    )

    with pytest.raises(ValueError, match="head tree changed"):
        service.verify(
            WorkspaceVerifyRequest(
                repository_root=repository_root,
                issue_number=31,
                candidate_id="candidate-2",
                workspace_pull_request_number=104,
                head_sha="a" * 40,
            )
        )
