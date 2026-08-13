from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
import subprocess

import pytest

from foundry_opt.adapters.commands import SubprocessCommandRunner
from foundry_opt.orchestration import (
    CandidateSummary,
    InMemoryWorkspaceStore,
    WorkspaceBaselineRecord,
    WorkspaceExperimentRecord,
    WorkspaceLineage,
    WorkspacePhase,
    WorkspaceUpdate,
    WorkspaceSpecificationRecord,
)
from foundry_opt.orchestration.workspace_verifier import WorkspaceVerifier
from foundry_opt.preflight.interfaces import CommandResult


class TrustedCommands:
    def __init__(self, pull: dict, checks: dict) -> None:
        self.pull = pull
        self.checks = checks
        self.git = SubprocessCommandRunner()

    def run(self, arguments, **kwargs):
        command = tuple(arguments)
        if command[:2] == ("gh", "api"):
            document = (
                self.checks
                if command[2].endswith("/check-runs")
                else self.pull
            )
            return CommandResult(0, json.dumps(document), "")
        return self.git.run(arguments, **kwargs)


def _run(repository: Path, *arguments: str, input_bytes=None) -> bytes:
    return subprocess.run(
        ("git", *arguments),
        cwd=repository,
        check=True,
        capture_output=True,
        input=input_bytes,
    ).stdout


def _workspace(tmp_path: Path):
    origin = tmp_path / "origin.git"
    repository = tmp_path / "repository"
    _run(tmp_path, "init", "--bare", str(origin))
    _run(tmp_path, "init", "-b", "main", str(repository))
    _run(repository, "config", "user.name", "Verifier Test")
    _run(
        repository,
        "config",
        "user.email",
        "verifier@example.invalid",
    )
    (repository / "agent.py").write_text("baseline\n", encoding="utf-8")
    _run(repository, "add", "agent.py")
    _run(repository, "commit", "-m", "base")
    base = _run(repository, "rev-parse", "HEAD").decode().strip()
    (repository / "agent.py").write_text("selected\n", encoding="utf-8")
    patch = _run(
        repository,
        "diff",
        "--binary",
        "--full-index",
        base,
        "--",
    )
    _run(repository, "add", "agent.py")
    tree = _run(repository, "write-tree").decode().strip()
    _run(repository, "reset", "--hard", base)
    _run(repository, "apply", "--binary", "--index", "-", input_bytes=patch)
    _run(repository, "commit", "-m", "selected")
    head = _run(repository, "rev-parse", "HEAD").decode().strip()
    branch = "foundry-opt/workspace/issue-31"
    _run(repository, "branch", branch, head)
    _run(repository, "remote", "add", "origin", str(origin))
    _run(repository, "push", "origin", "main:main", f"{branch}:{branch}")
    lineage = WorkspaceLineage(
        spec_sha256="a" * 64,
        base_commit=base,
        patch_sha256=hashlib.sha256(patch).hexdigest(),
        evidence_sha256="b" * 64,
        bundle_sha256="c" * 64,
        expected_tree=tree,
        selected_candidate_id="candidate-1",
        workspace_pull_request_number=104,
        required_checks={"tests": "success"},
        required_checks_provenance=f"trusted-selector:head:{head}",
    )
    store = InMemoryWorkspaceStore()
    store.commit(
        expected_revision=None,
        update=WorkspaceUpdate(
            issue_number=31,
            phase=WorkspacePhase.AWAITING_SELECTION,
            workspace_pull_request_number=104,
            semantic_event="experiments_completed",
            candidates=(
                CandidateSummary(
                    "candidate-1",
                    {"quality": 0.9},
                    True,
                    True,
                ),
            ),
            selected_patch=patch,
            external_operation_ids=(
                f"candidate-1:patch:{lineage.patch_sha256}",
                f"candidate-1:evidence:{lineage.evidence_sha256}",
                f"candidate-1:bundle:{lineage.bundle_sha256}",
                f"candidate-1:tree:{lineage.expected_tree}",
                f"workspace_commit:{head}",
            ),
            experiments=(
                WorkspaceExperimentRecord(
                    candidate_id="candidate-1",
                    patch_sha256=lineage.patch_sha256,
                    bundle_sha256=lineage.bundle_sha256,
                    evidence_sha256=lineage.evidence_sha256,
                    idempotency_key="d" * 64,
                    operation_sha256="e" * 64,
                    status="completed",
                    executor="direct_oidc",
                    draft_id="draft-1",
                    evaluation_id="evaluation-1",
                    run_id="run-1",
                    metrics={"quality": 0.9},
                    guardrails={"safety": "pass"},
                ),
            ),
            lineage=lineage,
            specification=WorkspaceSpecificationRecord(
                status="policy_approved",
                spec_sha256=lineage.spec_sha256,
                base_commit=base,
                target="support-agent",
                environment="development",
                asset_ids=("development", "validation", "quality"),
                metric_names=("quality",),
                policy_reason=(
                    "repository policy approved immutable assets"
                ),
            ),
            baseline=WorkspaceBaselineRecord(
                status="completed",
                operation_sha256="1" * 64,
                idempotency_key="2" * 64,
                bundle_sha256="3" * 64,
                evidence_sha256="4" * 64,
                dataset_ids=("development", "validation"),
                evaluator_ids=("quality",),
                split="development",
                sample_count=12,
                executor="direct_oidc",
                draft_id="baseline-draft",
                evaluation_id="baseline-evaluation",
                run_id="baseline-run",
                metrics={"quality": 0.8},
                guardrails={"safety": "pass"},
            ),
        ),
    )
    pull = {
        "number": 104,
        "state": "open",
        "draft": False,
        "body": "tampered prose with fake hashes",
        "head": {
            "ref": branch,
            "sha": head,
            "repo": {"full_name": "octo-org/optimizer"},
        },
        "base": {
            "ref": "main",
            "sha": base,
            "repo": {"full_name": "octo-org/optimizer"},
        },
    }
    checks = {
        "check_runs": [
            {
                "name": "tests",
                "status": "completed",
                "conclusion": "success",
                "head_sha": head,
            }
        ]
    }
    return repository, store, pull, checks, lineage


def test_verifier_accepts_exact_selected_pr_without_trusting_body(
    tmp_path: Path,
) -> None:
    repository, store, pull, checks, lineage = _workspace(tmp_path)
    result = WorkspaceVerifier(
        store=store,
        commands=TrustedCommands(pull, checks),
        repository="octo-org/optimizer",
        base_branch="main",
    ).verify(repository, issue_number=31, pull_request_number=104)

    assert result.verified is True
    assert result.exit_code == 0
    assert result.head_tree == lineage.expected_tree
    assert result.changed_paths == ("agent.py",)
    assert not (repository / ".fv").exists()
    assert all(
        item["status"] == "pass"
        for item in result.to_dict()["checks"]
    )


@pytest.mark.parametrize(
    ("mutation", "failed_check"),
    (
        ("branch", "pull_request_identity"),
        ("base", "base_commit"),
        ("tree", "head_tree"),
    ),
)
def test_verifier_fails_closed_on_wrong_pr_lineage(
    tmp_path: Path,
    mutation: str,
    failed_check: str,
) -> None:
    repository, store, pull, checks, lineage = _workspace(tmp_path)
    if mutation == "branch":
        pull["head"]["ref"] = "feature/not-workspace"
    elif mutation == "base":
        pull["base"]["sha"] = "d" * 40
    else:
        bad = WorkspaceLineage(
            spec_sha256=lineage.spec_sha256,
            base_commit=lineage.base_commit,
            patch_sha256=lineage.patch_sha256,
            evidence_sha256=lineage.evidence_sha256,
            bundle_sha256=lineage.bundle_sha256,
            expected_tree="e" * 40,
            selected_candidate_id=lineage.selected_candidate_id,
            workspace_pull_request_number=(
                lineage.workspace_pull_request_number
            ),
            required_checks=lineage.required_checks,
            required_checks_provenance=(
                lineage.required_checks_provenance
            ),
        )
        snapshot = store.load(31)
        assert snapshot is not None
        tampered = replace(snapshot, lineage=bad)

        class TamperedStore:
            def load(self, issue_number):
                return tampered

        store = TamperedStore()

    result = WorkspaceVerifier(
        store=store,
        commands=TrustedCommands(pull, checks),
        repository="octo-org/optimizer",
        base_branch="main",
    ).verify(repository, issue_number=31, pull_request_number=104)

    assert result.verified is False
    assert result.exit_code == 1
    assert {
        item.name for item in result.checks if item.status == "fail"
    } >= {failed_check}


def test_verifier_fails_before_github_when_lineage_is_missing(
    tmp_path: Path,
) -> None:
    store = InMemoryWorkspaceStore()
    store.commit(
        expected_revision=None,
        update=WorkspaceUpdate(
            issue_number=31,
            phase=WorkspacePhase.SPECIFICATION,
            workspace_pull_request_number=104,
            semantic_event="issue_created",
        ),
    )

    class NoCommands:
        def run(self, arguments, **kwargs):
            raise AssertionError("GitHub must not be trusted without lineage")

    result = WorkspaceVerifier(
        store=store,
        commands=NoCommands(),
        repository="octo-org/optimizer",
        base_branch="main",
    ).verify(tmp_path, issue_number=31, pull_request_number=104)

    assert result.verified is False
    assert result.checks[0].name == "workspace_lineage"
