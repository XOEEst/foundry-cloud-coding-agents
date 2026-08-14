from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
from typing import Protocol

from foundry_opt.adapters.commands import CommandError
from foundry_opt.orchestration.candidate_experiments import (
    CandidateExperimentResult,
)
from foundry_opt.orchestration.candidate_search import CandidateSearchSummary
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
from foundry_opt.orchestration.workspace_attribution import (
    WorkspaceCandidateProvenance,
)
from foundry_opt.orchestration.workspace_runtime import WorkspaceStore
from foundry_opt.orchestration.workspace_github import (
    workspace_pull_request_base_marker,
    workspace_pull_request_marker,
)
from foundry_opt.orchestration.workspace_store import (
    CandidateSummary,
    WorkspaceLineage,
    WorkspaceSnapshot,
    WorkspaceUpdate,
)
from foundry_opt.preflight.interfaces import CommandRunner


_REPOSITORY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
)
_COMMIT_ENVIRONMENT = {
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
    "GIT_AUTHOR_EMAIL": "foundry-opt@example.invalid",
    "GIT_AUTHOR_NAME": "Foundry Optimizer Workspace",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
    "GIT_COMMITTER_EMAIL": "foundry-opt@example.invalid",
    "GIT_COMMITTER_NAME": "Foundry Optimizer Workspace",
}
_COPILOT_COAUTHOR = (
    "Co-authored-by: GitHub Copilot "
    "<198982749+Copilot@users.noreply.github.com>"
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


@dataclass(frozen=True)
class WorkspaceExactPatchResult:
    commit_sha: str
    tree_sha: str
    changed_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if re.fullmatch(r"[0-9a-f]{40}", self.commit_sha) is None:
            raise ValueError("workspace exact commit is invalid")
        if re.fullmatch(r"[0-9a-f]{40}", self.tree_sha) is None:
            raise ValueError("workspace exact tree is invalid")
        object.__setattr__(self, "changed_paths", tuple(self.changed_paths))


class WorkspaceExactBranchPublisher(Protocol):
    def publish(
        self,
        repository_root: Path,
        pull_request: WorkspacePullRequest,
        candidate: WorkspaceCandidate,
        provenance: WorkspaceCandidateProvenance | None = None,
    ) -> WorkspaceExactPatchResult: ...


class GitWorkspaceExactBranchPublisher:
    def __init__(
        self,
        commands: CommandRunner,
        *,
        remote: str = "origin",
    ) -> None:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", remote) is None:
            raise ValueError("workspace exact remote is invalid")
        self._commands = commands
        self._remote = remote

    def publish(
        self,
        repository_root: Path,
        pull_request: WorkspacePullRequest,
        candidate: WorkspaceCandidate,
        provenance: WorkspaceCandidateProvenance | None = None,
    ) -> WorkspaceExactPatchResult:
        root = repository_root.resolve(strict=True)
        message = self._commit_message(
            pull_request,
            candidate,
            provenance,
        )
        branch_ref = f"refs/heads/{pull_request.branch}"
        remote_head = self._remote_head(root, branch_ref)
        if remote_head is None:
            raise RuntimeError("workspace branch does not exist")
        existing = self._existing_exact(
            root,
            remote_head,
            pull_request,
            candidate,
            message,
        )
        if existing is not None:
            return existing
        worktree = (
            root
            / ".fw"
            / (
                f"w{pull_request.issue_number}-"
                f"{candidate.experiment.idempotency_key[:8]}"
            )
        )
        self._remove_worktree(root, worktree)
        worktree.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._run(
                ("git", "worktree", "add", "--detach", str(worktree),
                 pull_request.base_commit),
                root,
            )
            if self._run(
                (
                    "git",
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                ),
                worktree,
            ).stdout:
                raise RuntimeError("workspace exact worktree is not clean")
            self._run(
                ("git", "apply", "--check", "--binary", "--index", "-"),
                worktree,
                input_bytes=candidate.exact_patch,
            )
            self._run(
                (
                    "git",
                    "apply",
                    "--binary",
                    "--index",
                    "--whitespace=nowarn",
                    "-",
                ),
                worktree,
                input_bytes=candidate.exact_patch,
            )
            changed_paths = tuple(
                item
                for item in self._run(
                    (
                        "git",
                        "diff",
                        "--cached",
                        "--name-only",
                        "-z",
                    ),
                    worktree,
                ).stdout.split("\0")
                if item
            )
            if (
                not changed_paths
                or set(changed_paths) != set(candidate.changed_paths)
                or len(changed_paths) != len(candidate.changed_paths)
            ):
                raise RuntimeError(
                    "workspace exact changed paths do not match"
                )
            tree = self._run(("git", "write-tree"), worktree).stdout.strip()
            if tree != candidate.expected_tree:
                raise RuntimeError("workspace exact tree does not match")
            commit = self._exact_commit(
                worktree,
                tree,
                pull_request.base_commit,
                message,
            )
            self._run(("git", "reset", "--hard", commit), worktree)
            verified_tree = self._run(
                ("git", "rev-parse", "--verify", "HEAD^{tree}"),
                worktree,
            ).stdout.strip()
            if (
                verified_tree != candidate.expected_tree
                or self._run(
                    (
                        "git",
                        "status",
                        "--porcelain=v1",
                        "--untracked-files=all",
                    ),
                    worktree,
                ).stdout
            ):
                raise RuntimeError("workspace exact commit is unverified")
            self._run(
                (
                    "git",
                    "push",
                    f"--force-with-lease={branch_ref}:{remote_head}",
                    self._remote,
                    f"{commit}:{branch_ref}",
                ),
                root,
            )
            if self._remote_head(root, branch_ref) != commit:
                raise RuntimeError("workspace exact branch push is unverified")
            return WorkspaceExactPatchResult(
                commit_sha=commit,
                tree_sha=verified_tree,
                changed_paths=changed_paths,
            )
        except CommandError as error:
            raise RuntimeError(
                "workspace exact branch publication failed"
            ) from error
        finally:
            self._remove_worktree(root, worktree)

    def _existing_exact(
        self,
        root: Path,
        commit: str,
        pull_request: WorkspacePullRequest,
        candidate: WorkspaceCandidate,
        message: str,
    ) -> WorkspaceExactPatchResult | None:
        try:
            self._run(
                ("git", "fetch", "--no-tags", self._remote, commit),
                root,
            )
            tree = self._run(
                ("git", "rev-parse", "--verify", f"{commit}^{{tree}}"),
                root,
            ).stdout.strip()
            parent = self._run(
                ("git", "rev-parse", "--verify", f"{commit}^"),
                root,
            ).stdout.strip()
            changed_paths = tuple(
                item
                for item in self._run(
                    (
                        "git",
                        "diff-tree",
                        "--no-commit-id",
                        "--name-only",
                        "-r",
                        "-z",
                        commit,
                    ),
                    root,
                ).stdout.split("\0")
                if item
            )
        except CommandError:
            return None
        if (
            parent != pull_request.base_commit
            or tree != candidate.expected_tree
            or set(changed_paths) != set(candidate.changed_paths)
            or len(changed_paths) != len(candidate.changed_paths)
        ):
            return None
        try:
            expected_commit = self._exact_commit(
                root,
                tree,
                pull_request.base_commit,
                message,
            )
        except CommandError:
            return None
        if commit != expected_commit:
            return None
        return WorkspaceExactPatchResult(
            commit_sha=commit,
            tree_sha=tree,
            changed_paths=changed_paths,
        )

    def _commit_message(
        self,
        pull_request: WorkspacePullRequest,
        candidate: WorkspaceCandidate,
        provenance: WorkspaceCandidateProvenance | None,
    ) -> str:
        subject = (
            "Apply selected optimization candidate "
            f"for issue-{pull_request.issue_number}"
        )
        if provenance is None:
            return subject
        if (
            type(provenance) is not WorkspaceCandidateProvenance
            or pull_request.number is None
            or provenance.workspace_pr_number != pull_request.number
        ):
            raise ValueError("workspace candidate provenance is invalid")
        return "\n".join(
            (
                subject,
                "",
                (
                    "Selected candidate ID: "
                    f"{candidate.experiment.candidate_id}"
                ),
                (
                    "Copilot source commit SHA: "
                    f"{provenance.candidate_source_commit_sha}"
                ),
                (
                    "Copilot source commit URL: "
                    f"{provenance.candidate_source_commit_url}"
                ),
                (
                    "Copilot acknowledgement URL: "
                    f"{provenance.acknowledgement_comment_url}"
                ),
                f"Provenance SHA-256: {provenance.identity_sha256}",
                "",
                _COPILOT_COAUTHOR,
            )
        )

    def _exact_commit(
        self,
        root: Path,
        tree: str,
        parent: str,
        message: str,
    ) -> str:
        return self._run(
            (
                "git",
                "commit-tree",
                tree,
                "-p",
                parent,
                "-F",
                "-",
            ),
            root,
            input_bytes=f"{message}\n".encode("utf-8"),
            environment=_COMMIT_ENVIRONMENT,
        ).stdout.strip()

    def _remote_head(self, root: Path, branch_ref: str) -> str | None:
        result = self._run(
            ("git", "ls-remote", "--heads", self._remote, branch_ref),
            root,
        ).stdout.strip()
        if not result:
            return None
        fields = result.split()
        if (
            len(fields) != 2
            or fields[1] != branch_ref
            or re.fullmatch(r"[0-9a-f]{40}", fields[0]) is None
        ):
            raise RuntimeError("workspace remote branch is invalid")
        return fields[0]

    def _remove_worktree(self, root: Path, worktree: Path) -> None:
        try:
            self._run(
                ("git", "worktree", "remove", "--force", str(worktree)),
                root,
            )
        except CommandError:
            pass
        if worktree.exists():
            shutil.rmtree(worktree)
        try:
            self._run(("git", "worktree", "prune"), root)
        except CommandError:
            pass
        parent = worktree.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()

    def _run(
        self,
        arguments: tuple[str, ...],
        cwd: Path,
        *,
        input_bytes: bytes | None = None,
        environment: dict[str, str] | None = None,
    ):
        return self._commands.run(
            arguments,
            cwd=cwd,
            input_bytes=input_bytes,
            environment=environment,
        )


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
            current = json.loads(
                self._commands.run(
                    (
                        "gh",
                        "pr",
                        "view",
                        str(pull_request.number),
                        "--repo",
                        self._repository,
                        "--json",
                        "number,headRefName,title,body,isDraft,state",
                    ),
                    cwd=repository_root,
                ).stdout
            )
            if (
                not isinstance(current, dict)
                or current.get("number") != pull_request.number
                or current.get("headRefName") != pull_request.branch
                or current.get("state") != "OPEN"
                or not isinstance(current.get("isDraft"), bool)
            ):
                raise RuntimeError(
                    "workspace pull request identity changed"
                )
            if (
                current.get("title") != projection.title
                or current.get("body") != body
            ):
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
        except (CommandError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "workspace pull request finalization failed"
            ) from error
        return WorkspacePullRequest(
            number=pull_request.number,
            issue_number=pull_request.issue_number,
            branch=pull_request.branch,
            title=projection.title,
            draft=bool(current["isDraft"]),
            reuse_existing=True,
            base_commit=pull_request.base_commit,
        )


@dataclass(frozen=True)
class WorkspaceCandidateCoordinatorResult:
    snapshot: WorkspaceSnapshot
    workspace_pull_request: WorkspacePullRequest
    report: OptimizationReport
    recorded: bool


class WorkspaceCandidateCoordinator:
    def __init__(
        self,
        *,
        store: WorkspaceStore,
        selector: TrustedWorkspaceSelector,
        exact_publisher: WorkspaceExactBranchPublisher,
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
        self._selector = selector
        self._exact_publisher = exact_publisher
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
        baseline = snapshot.baseline
        specification = snapshot.specification
        if (
            specification is None
            or specification.status != "policy_approved"
            or baseline is None
            or baseline.status != "completed"
            or request.report_context.spec_sha256
            != specification.spec_sha256
            or dict(request.report_context.baseline_metrics)
            != dict(baseline.metrics)
            or request.report_context.sample_count != baseline.sample_count
            or request.report_context.split != baseline.split
        ):
            raise ValueError(
                "workspace report context is not trusted state"
            )
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
                    experiments=snapshot.experiments,
                    lineage=snapshot.lineage,
                    specification=snapshot.specification,
                    baseline=snapshot.baseline,
                ),
            )
        experiments, snapshot = self._evaluate_candidates(
            request,
            candidates,
            snapshot,
            pull_request,
        )
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
        selected_experiment_record = next(
            item
            for item in snapshot.experiments
            if item.candidate_id == decision.selected_candidate_id
        )
        if snapshot.phase is WorkspacePhase.AWAITING_SELECTION:
            existing_lineage = snapshot.lineage
            if existing_lineage is None:
                raise ValueError(
                    "workspace committed selection lineage is missing"
                )
            existing_candidate = by_id.get(
                existing_lineage.selected_candidate_id
            )
            existing_experiment = experiment_by_id.get(
                existing_lineage.selected_candidate_id
            )
            if (
                decision.selected_candidate_id
                != existing_lineage.selected_candidate_id
                or existing_candidate is None
                or existing_experiment is None
                or request.report_context.spec_sha256
                != existing_lineage.spec_sha256
                or request.issue.base_commit
                != existing_lineage.base_commit
                or existing_candidate.experiment.patch_sha256
                != existing_lineage.patch_sha256
                or existing_candidate.expected_tree
                != existing_lineage.expected_tree
                or existing_experiment.evidence_sha256
                != existing_lineage.evidence_sha256
                or existing_experiment.bundle_sha256
                != existing_lineage.bundle_sha256
                or dict(decision.required_checks)
                != dict(existing_lineage.required_checks)
                or selected_experiment_record.provenance
                != existing_lineage.candidate_provenance
            ):
                raise ValueError(
                    "workspace committed selection lineage changed"
                )
        selected = by_id[decision.selected_candidate_id]
        selected_experiment = experiment_by_id[
            decision.selected_candidate_id
        ]
        exact = self._exact_publisher.publish(
            request.repository_root,
            pull_request,
            selected,
            selected_experiment_record.provenance,
        )
        if (
            exact.tree_sha != selected.expected_tree
            or set(exact.changed_paths) != set(selected.changed_paths)
            or len(exact.changed_paths) != len(selected.changed_paths)
        ):
            raise ValueError("workspace exact candidate tree is unverified")
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
        recorded = snapshot.phase is not WorkspacePhase.AWAITING_SELECTION
        external_ids = (
            *snapshot.external_operation_ids,
            (
                f"{selected.experiment.candidate_id}:patch:"
                f"{selected.experiment.patch_sha256}"
            ),
            (
                f"{selected.experiment.candidate_id}:tree:"
                f"{selected.expected_tree}"
            ),
            f"workspace_commit:{exact.commit_sha}",
        )
        external_ids = tuple(dict.fromkeys(external_ids))
        if pull_request.number is None:
            raise ValueError(
                "workspace lineage requires a pull request number"
            )
        lineage = WorkspaceLineage(
            spec_sha256=request.report_context.spec_sha256,
            base_commit=request.issue.base_commit,
            patch_sha256=selected.experiment.patch_sha256,
            evidence_sha256=selected_experiment.evidence_sha256,
            bundle_sha256=selected_experiment.bundle_sha256,
            expected_tree=selected.expected_tree,
            selected_candidate_id=decision.selected_candidate_id,
            workspace_pull_request_number=pull_request.number,
            required_checks=decision.required_checks,
            required_checks_provenance=(
                f"trusted-selector:head:{exact.commit_sha}"
            ),
            candidate_provenance=selected_experiment_record.provenance,
        )
        committed = (
            self._store.commit(
                expected_revision=snapshot.revision,
                update=WorkspaceUpdate(
                    issue_number=request.issue.number,
                    phase=WorkspacePhase.AWAITING_SELECTION,
                    workspace_pull_request_number=pull_request.number,
                    semantic_event="experiments_completed",
                    candidates=compact,
                    selected_patch=selected.exact_patch,
                    external_operation_ids=external_ids,
                    experiments=snapshot.experiments,
                    lineage=lineage,
                    specification=snapshot.specification,
                    baseline=snapshot.baseline,
                ),
            )
            if recorded
            else snapshot
        )
        if not recorded and (
            committed.candidates != compact
            or committed.selected_patch != selected.exact_patch
            or committed.lineage != lineage
        ):
            raise ValueError("workspace committed selection changed")
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
            or finalized.title != projection.title
        ):
            raise ValueError("workspace finalizer changed pull request identity")
        return WorkspaceCandidateCoordinatorResult(
            snapshot=committed,
            workspace_pull_request=finalized,
            report=report,
            recorded=recorded,
        )

    def _evaluate_candidates(
        self,
        request: WorkspaceRequest,
        candidates: tuple[WorkspaceCandidate, ...],
        snapshot: WorkspaceSnapshot,
        pull_request: WorkspacePullRequest,
    ) -> tuple[tuple[CandidateSearchSummary, ...], WorkspaceSnapshot]:
        del pull_request
        records = {
            item.candidate_id: item for item in snapshot.experiments
        }
        if (
            len(records) != self._candidate_count
            or set(records)
            != {
                item.experiment.candidate_id for item in candidates
            }
        ):
            raise ValueError(
                "workspace requires all configured trusted experiments"
            )
        summaries: list[CandidateSearchSummary] = []
        for candidate in candidates:
            candidate_id = candidate.experiment.candidate_id
            prepared = candidate.experiment_result
            record = records[candidate_id]
            if record.status != "completed":
                raise ValueError(
                    "workspace trusted experiment is still pending"
                )
            trusted = CandidateExperimentResult(
                candidate_id=record.candidate_id,
                executor=record.executor or "",
                metrics=record.metrics,
                guardrails=record.guardrails,
                draft_id=record.draft_id or "",
                evaluation_id=record.evaluation_id or "",
                run_id=record.run_id or "",
                bundle_sha256=record.bundle_sha256,
                evidence_sha256=record.evidence_sha256,
                operation_sha256=record.operation_sha256,
                idempotency_key=record.idempotency_key,
            )
            if (
                candidate.experiment.patch_sha256
                != record.patch_sha256
                or candidate.experiment.bundle_sha256
                != record.bundle_sha256
                or candidate.experiment.evidence_sha256
                != record.evidence_sha256
                or candidate.experiment.idempotency_key
                != record.idempotency_key
                or candidate.changed_paths != record.changed_paths
                or candidate.validation != record.validation
                or candidate.expected_tree != record.expected_tree
                or prepared != trusted
            ):
                raise ValueError(
                    "workspace proposal changed trusted experiment lineage"
                )
            expected_ids = set(_experiment_operation_ids(candidate_id, trusted))
            if not expected_ids <= set(snapshot.external_operation_ids):
                raise ValueError(
                    "workspace persisted experiment lineage is incomplete"
                )
            summaries.append(
                CandidateSearchSummary(
                    candidate_id=candidate_id,
                    patch_sha256=candidate.experiment.patch_sha256,
                    bundle_sha256=record.bundle_sha256,
                    evidence_sha256=record.evidence_sha256,
                    idempotency_key=candidate.experiment.idempotency_key,
                    executor=trusted.executor,
                    metrics=trusted.metrics,
                    guardrails=trusted.guardrails,
                )
            )
        return tuple(summaries), snapshot

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
        lineage = snapshot.lineage
        if lineage is None:
            raise ValueError("workspace report lineage is missing")
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
            spec_sha256=lineage.spec_sha256,
            base_commit=lineage.base_commit,
            patch_sha256=lineage.patch_sha256,
            evidence_sha256=lineage.evidence_sha256,
            bundle_sha256=lineage.bundle_sha256,
            expected_tree=lineage.expected_tree,
            required_checks=lineage.required_checks,
            merge_gate=EvidenceMergeGate.ELIGIBLE,
            candidate_provenance=lineage.candidate_provenance,
        )


def _experiment_operation_ids(
    candidate_id: str,
    result: CandidateExperimentResult,
) -> tuple[str, ...]:
    return (
        result.draft_id,
        result.evaluation_id,
        result.run_id,
        f"{candidate_id}:bundle:{result.bundle_sha256}",
        f"{candidate_id}:evidence:{result.evidence_sha256}",
    )
__all__ = [
    "PlanningWorkspacePullRequestFinalizer",
    "GitWorkspaceExactBranchPublisher",
    "GhWorkspacePullRequestFinalizer",
    "TrustedWorkspaceSelector",
    "WorkspaceCandidateCoordinator",
    "WorkspaceCandidateCoordinatorResult",
    "WorkspaceExactBranchPublisher",
    "WorkspaceExactPatchResult",
    "WorkspacePullRequestFinalizer",
]
