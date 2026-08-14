import hashlib
import json
from pathlib import Path

from foundry_opt.evaluation import (
    EvaluationPolicy,
    MetricDirection,
    MetricPolicy,
)
from foundry_opt.orchestration import (
    CandidateExperimentRequest,
    CandidateExperimentResult,
    CandidateSearchSummary,
    ConfiguredWorkspaceSelector,
    WorkspaceCandidate,
    WorkspaceIssue,
    WorkspaceReportContext,
    WorkspaceSelectionRequest,
)
from foundry_opt.preflight.interfaces import CommandResult


class Commands:
    def __init__(self, tree: str) -> None:
        self.tree = tree

    def run(self, arguments, **kwargs):
        if tuple(arguments[:3]) == ("gh", "pr", "view"):
            return CommandResult(
                0,
                json.dumps(
                    {
                        "headRefOid": "a" * 40,
                        "statusCheckRollup": [
                            {
                                "name": "tests",
                                "status": "COMPLETED",
                                "conclusion": "SUCCESS",
                            }
                        ],
                    }
                ),
                "",
            )
        if tuple(arguments[:3]) == ("git", "rev-parse", "--verify"):
            return CommandResult(0, f"{self.tree}\n", "")
        return CommandResult(0, "", "")


def _candidate(number: int, quality: float) -> WorkspaceCandidate:
    candidate_id = f"candidate-{number}"
    patch = f"patch {number}".encode()
    request = CandidateExperimentRequest(
        issue_number=31,
        candidate_id=candidate_id,
        patch_sha256=hashlib.sha256(patch).hexdigest(),
        bundle_sha256=str(number) * 64,
        evidence_sha256=str(number + 2) * 64,
        idempotency_key=str(number + 4) * 64,
    )
    result = CandidateExperimentResult(
        candidate_id=candidate_id,
        executor="actions",
        metrics={"quality": quality},
        guardrails={"safety": "pass"},
        draft_id=f"draft-{number}",
        evaluation_id=f"evaluation-{number}",
        run_id=f"run-{number}",
        bundle_sha256=request.bundle_sha256,
        evidence_sha256=request.evidence_sha256,
    )
    return WorkspaceCandidate(
        experiment=request,
        experiment_result=result,
        exact_patch=patch,
        summary=f"Candidate {number}",
        changed_paths=("agent.py",),
        validation=("tests passed",),
        expected_tree=str(number + 7) * 40,
    )


def test_configured_selector_binds_successful_checks_to_exact_tree(
    tmp_path: Path,
) -> None:
    first = _candidate(1, 0.85)
    second = _candidate(2, 0.95)
    policy = EvaluationPolicy(
        (
            MetricPolicy(
                "quality",
                MetricDirection.MAXIMIZE,
                0.8,
                0.05,
            ),
        )
    )
    request = WorkspaceSelectionRequest(
        issue=WorkspaceIssue(31, "[Optimize] X", "body", "a" * 40),
        candidates=(first, second),
        experiments=tuple(
            CandidateSearchSummary(
                candidate_id=item.experiment.candidate_id,
                patch_sha256=item.experiment.patch_sha256,
                bundle_sha256=item.experiment.bundle_sha256,
                evidence_sha256=item.experiment.evidence_sha256,
                idempotency_key=item.experiment.idempotency_key,
                executor="actions",
                metrics=item.experiment_result.metrics,
                guardrails=item.experiment_result.guardrails,
            )
            for item in (first, second)
        ),
        report_context=WorkspaceReportContext(
            baseline_metrics={"quality": 0.75},
            policy=policy,
            sample_count=10,
            split="development",
            spec_sha256="f" * 64,
        ),
    )

    exact = ConfiguredWorkspaceSelector(
        Commands(second.expected_tree),
        repository_root=tmp_path,
        repository="octo-org/optimizer",
        required_checks=("tests",),
    ).select(request)
    stale = ConfiguredWorkspaceSelector(
        Commands(first.expected_tree),
        repository_root=tmp_path,
        repository="octo-org/optimizer",
        required_checks=("tests",),
    ).select(request)

    assert exact.selected_candidate_id == "candidate-2"
    assert exact.required_checks == {"tests": "success"}
    assert stale.required_checks == {"tests": "pending"}
