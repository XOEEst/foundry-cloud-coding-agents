from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Protocol

from foundry_opt.adapters.commands import CommandError
from foundry_opt.orchestration.candidate_experiments import (
    CandidateExperimentAdapter,
    CandidateExperimentRequest,
    CandidateExperimentResult,
)
from foundry_opt.orchestration.candidate_search import (
    BoundedCandidateSearch,
    CandidateSearchSummary,
)
from foundry_opt.orchestration.public_evidence import (
    AlternativeResult,
    EvidenceMergeGate,
    OptimizationReport,
    PublicEvidenceRenderer,
    PullRequestProjection,
)
from foundry_opt.orchestration.workspace import (
    WorkspaceCandidate,
    WorkspacePullRequest,
    WorkspacePhase,
    WorkspaceRequest,
    WorkspaceSelectionDecision,
    WorkspaceSelectionRequest,
)
from foundry_opt.orchestration.workspace_runtime import WorkspaceStore
from foundry_opt.orchestration.workspace_github import (
    workspace_pull_request_base_marker,
    workspace_pull_request_marker,
)
from foundry_opt.orchestration.workspace_store import (
    CandidateSummary,
    WorkspaceSnapshot,
    WorkspaceUpdate,
)
from foundry_opt.preflight.interfaces import CommandRunner


_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
)


class TrustedWorkspaceSelector(Protocol):
    def select(
        self,
        request: WorkspaceSelectionRequest,
    ) -> WorkspaceSelectionDecision: ...


class WorkspacePullRequestFinalizer(Protocol):
    def finalize(
        self,
        repository_root: Path,
        pull_request: WorkspacePullRequest,
        projection: PullRequestProjection,
    ) -> WorkspacePullRequest: ...


class PlanningWorkspacePullRequestFinalizer:
    def finalize(
        self,
        repository_root: Path,
        pull_request: WorkspacePullRequest,
        projection: PullRequestProjection,
    ) -> WorkspacePullRequest:
        return WorkspacePullRequest(
            number=pull_request.number,
            issue_number=pull_request.issue_number,
            branch=pull_request.branch,
            title=projection.title,
            draft=projection.draft,
            reuse_existing=True,
            base_commit=pull_request.base_commit,
        )


class GhWorkspacePullRequestFinalizer:
    def __init__(
        self,
        commands: CommandRunner,
        *,
        repository: str,
    ) -> None:
        if _REPOSITORY.fullmatch(repository) is None:
            raise ValueError("workspace repository is invalid")
        self._commands = commands
        self._repository = repository

    def finalize(
        self,
        repository_root: Path,
        pull_request: WorkspacePullRequest,
        projection: PullRequestProjection,
    ) -> WorkspacePullRequest:
        if pull_request.number is None or projection.draft:
            raise ValueError("workspace pull request is not finalizable")
        body = "\n\n".join(
            (
                workspace_pull_request_marker(
                    pull_request.issue_number
                ),
                workspace_pull_request_base_marker(
                    pull_request.base_commit
                ),
                projection.body,
            )
        )
        try:
            self._commands.run(
                (
                    "gh",
                    "pr",
                    "edit",
                    str(pull_request.number),
                    "--repo",
                    self._repository,
                    "--title",
                    projection.title,
                    "--body-file",
                    "-",
                ),
                cwd=repository_root,
                input_text=body,
            )
            self._commands.run(
                (
                    "gh",
                    "pr",
                    "ready",
                    str(pull_request.number),
                    "--repo",
                    self._repository,
                ),
                cwd=repository_root,
            )
        except CommandError as error:
            raise RuntimeError(
                "workspace pull request finalization failed"
            ) from error
        return WorkspacePullRequest(
            number=pull_request.number,
            issue_number=pull_request.issue_number,
            branch=pull_request.branch,
            title=projection.title,
            draft=False,
            reuse_existing=True,
            base_commit=pull_request.base_commit,
        )


@dataclass(frozen=True)
class WorkspaceCandidateCoordinatorResult:
    snapshot: WorkspaceSnapshot
    workspace_pull_request: WorkspacePullRequest
    report: OptimizationReport


class _CapturingRunner:
    def __init__(self, runner: CandidateExperimentAdapter) -> None:
        self._runner = runner
        self.results: list[CandidateExperimentResult] = []

    def evaluate(
        self,
        request: CandidateExperimentRequest,
    ) -> CandidateExperimentResult:
        result = self._runner.evaluate(request)
        self.results.append(result)
        return result


class WorkspaceCandidateCoordinator:
    def __init__(
        self,
        *,
        store: WorkspaceStore,
        runner: CandidateExperimentAdapter,
        selector: TrustedWorkspaceSelector,
        candidate_count: int,
        finalizer: WorkspacePullRequestFinalizer | None = None,
        renderer: PublicEvidenceRenderer | None = None,
    ) -> None:
        if (
            isinstance(candidate_count, bool)
            or not isinstance(candidate_count, int)
            or not 1 <= candidate_count <= 32
        ):
            raise ValueError("candidate_count must be between 1 and 32")
        self._store = store
        self._runner = runner
        self._selector = selector
        self._candidate_count = candidate_count
        self._finalizer = (
            finalizer
            if finalizer is not None
            else PlanningWorkspacePullRequestFinalizer()
        )
        self._renderer = renderer or PublicEvidenceRenderer()

    def complete(
        self,
        *,
        request: WorkspaceRequest,
        pull_request: WorkspacePullRequest,
    ) -> WorkspaceCandidateCoordinatorResult:
        candidates = tuple(request.candidates)
        if len(candidates) != self._candidate_count:
            raise ValueError(
                "workspace requires its configured candidate count"
            )
        if request.report_context is None:
            raise ValueError("workspace report context is required")
        if any(
            item.experiment.issue_number != request.issue.number
            for item in candidates
        ):
            raise ValueError("workspace candidate issue binding changed")
        snapshot = self._store.load(request.issue.number)
        if snapshot is None:
            raise ValueError("workspace state is required")
        if snapshot.phase is WorkspacePhase.SPECIFICATION:
            snapshot = self._store.commit(
                expected_revision=snapshot.revision,
                update=WorkspaceUpdate(
                    issue_number=request.issue.number,
                    phase=WorkspacePhase.EVALUATING,
                    workspace_pull_request_number=pull_request.number,
                    semantic_event="candidate_experiments_started",
                    candidates=snapshot.candidates,
                    selected_patch=snapshot.selected_patch,
                    external_operation_ids=(
                        snapshot.external_operation_ids
                    ),
                ),
            )
        capture = _CapturingRunner(self._runner)
        experiments = BoundedCandidateSearch(
            runner=capture,
            max_candidates=self._candidate_count,
        ).evaluate(tuple(item.experiment for item in candidates))
        if len(experiments) != self._candidate_count:
            raise ValueError("workspace candidate evaluation stopped early")
        decision = self._selector.select(
            WorkspaceSelectionRequest(
                issue=request.issue,
                candidates=candidates,
                experiments=experiments,
                report_context=request.report_context,
            )
        )
        by_id = {item.experiment.candidate_id: item for item in candidates}
        experiment_by_id = {
            item.candidate_id: item for item in experiments
        }
        if (
            set(by_id) != set(experiment_by_id)
            or decision.selected_candidate_id not in by_id
            or not set(decision.eligible_candidate_ids) <= set(by_id)
        ):
            raise ValueError("workspace selector changed candidate binding")
        if (
            not decision.required_checks
            or any(
                status != "success"
                for status in decision.required_checks.values()
            )
        ):
            raise ValueError(
                "workspace selection requires successful trusted checks"
            )
        selected = by_id[decision.selected_candidate_id]
        compact = tuple(
            CandidateSummary(
                candidate_id=item.experiment.candidate_id,
                metrics=experiment_by_id[
                    item.experiment.candidate_id
                ].metrics,
                eligible=(
                    item.experiment.candidate_id
                    in decision.eligible_candidate_ids
                ),
                selected=(
                    item.experiment.candidate_id
                    == decision.selected_candidate_id
                ),
            )
            for item in candidates
        )
        external_ids = tuple(
            operation_id
            for result in capture.results
            for operation_id in (
                result.draft_id,
                result.evaluation_id,
                result.run_id,
            )
        )
        if len(external_ids) != len(set(external_ids)):
            raise ValueError(
                "workspace candidate operation IDs must be unique"
            )
        committed = self._store.commit(
            expected_revision=snapshot.revision,
            update=WorkspaceUpdate(
                issue_number=request.issue.number,
                phase=WorkspacePhase.AWAITING_SELECTION,
                workspace_pull_request_number=pull_request.number,
                semantic_event="experiments_completed",
                candidates=compact,
                selected_patch=selected.exact_patch,
                external_operation_ids=external_ids,
            ),
        )
        report = self._report(
            request=request,
            snapshot=committed,
            candidates=candidates,
            experiments=experiments,
            decision=decision,
        )
        projection = self._renderer.render_pr(report)
        if projection.draft:
            raise ValueError(
                "workspace finalization requires eligible public evidence"
            )
        finalized = self._finalizer.finalize(
            request.repository_root,
            pull_request,
            projection,
        )
        if (
            finalized.number != pull_request.number
            or finalized.issue_number != pull_request.issue_number
            or finalized.branch != pull_request.branch
            or finalized.base_commit != pull_request.base_commit
            or finalized.draft
            or finalized.title != projection.title
        ):
            raise ValueError("workspace finalizer changed pull request identity")
        return WorkspaceCandidateCoordinatorResult(
            snapshot=committed,
            workspace_pull_request=finalized,
            report=report,
        )

    @staticmethod
    def _report(
        *,
        request: WorkspaceRequest,
        snapshot: WorkspaceSnapshot,
        candidates: tuple[WorkspaceCandidate, ...],
        experiments: tuple[CandidateSearchSummary, ...],
        decision: WorkspaceSelectionDecision,
    ) -> OptimizationReport:
        context = request.report_context
        assert context is not None
        selected_state = next(
            item for item in snapshot.candidates if item.selected
        )
        selected = next(
            item
            for item in candidates
            if item.experiment.candidate_id
            == selected_state.candidate_id
        )
        selected_experiment = next(
            item
            for item in experiments
            if item.candidate_id == selected_state.candidate_id
        )
        evidence = _canonical_digest(
            {
                "candidates": [
                    {
                        "candidate_id": item.candidate_id,
                        "eligible": item.eligible,
                        "metrics": dict(item.metrics),
                        "selected": item.selected,
                    }
                    for item in snapshot.candidates
                ],
                "external_operation_ids": list(
                    snapshot.external_operation_ids
                ),
            }
        )
        bundle = hashlib.sha256(
            evidence.encode("ascii") + (snapshot.selected_patch or b"")
        ).hexdigest()
        policy = context.policy
        return OptimizationReport(
            issue_number=request.issue.number,
            candidate_id=selected_state.candidate_id,
            recommendation=decision.recommendation,
            alternatives=tuple(
                AlternativeResult(
                    candidate_id=item.candidate_id,
                    outcome=(
                        "selected"
                        if item.selected
                        else "rejected"
                    ),
                    rejection_reason=(
                        None
                        if item.selected
                        else decision.rejection_reasons.get(
                            item.candidate_id,
                            "Trusted selector chose another candidate.",
                        )
                    ),
                )
                for item in snapshot.candidates
            ),
            baseline_metrics=context.baseline_metrics,
            candidate_metrics=selected_state.metrics,
            guardrails=selected_experiment.guardrails,
            thresholds={
                metric.name: metric.threshold for metric in policy.metrics
            },
            materiality={
                metric.name: metric.materiality for metric in policy.metrics
            },
            sample_count=context.sample_count,
            split=context.split,
            foundry_operations=selected.foundry_operations,
            changed_paths=selected.changed_paths,
            validation=selected.validation,
            spec_sha256=context.spec_sha256,
            base_commit=request.issue.base_commit,
            patch_sha256=selected.experiment.patch_sha256,
            evidence_sha256=evidence,
            bundle_sha256=bundle,
            expected_tree=selected.expected_tree,
            required_checks=decision.required_checks,
            merge_gate=EvidenceMergeGate.ELIGIBLE,
        )


def _canonical_digest(value: Mapping[str, object]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "PlanningWorkspacePullRequestFinalizer",
    "GhWorkspacePullRequestFinalizer",
    "TrustedWorkspaceSelector",
    "WorkspaceCandidateCoordinator",
    "WorkspaceCandidateCoordinatorResult",
    "WorkspacePullRequestFinalizer",
]
