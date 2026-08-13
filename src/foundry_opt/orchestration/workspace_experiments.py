from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath
import re
import shlex
import shutil
from typing import Any, Mapping, Protocol

from foundry_opt.config.models import OptimizerConfig
from foundry_opt.packaging import BundleRequest, build_source_bundle
from foundry_opt.packaging.validation import (
    ValidationRequest,
    run_validation,
)
from foundry_opt.preflight.interfaces import CommandRunner
from foundry_opt.orchestration.candidate_experiments import (
    CandidateExperimentAdapter,
    CandidateExperimentOperation,
    CandidateExperimentPending,
    CandidateExperimentRequest,
    CandidateExperimentResult,
)
from foundry_opt.orchestration.workspace import (
    WorkspaceCandidateProposal,
    WorkspacePhase,
)
from foundry_opt.orchestration.workspace_runtime import WorkspaceStore
from foundry_opt.orchestration.workspace_store import (
    WorkspaceExperimentRecord,
    WorkspaceUpdate,
)
from foundry_opt.security import reject_secret_content


@dataclass(frozen=True)
class WorkspaceCandidatePreparation:
    request: CandidateExperimentRequest
    mutation_class: str
    changed_paths: tuple[str, ...]
    validation: tuple[str, ...]
    expected_tree: str

    def __post_init__(self) -> None:
        if (
            re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
                self.mutation_class,
            )
            is None
            or re.fullmatch(r"[0-9a-f]{40}", self.expected_tree) is None
            or not self.changed_paths
            or not self.validation
        ):
            raise ValueError("workspace candidate preparation is invalid")
        object.__setattr__(self, "changed_paths", tuple(self.changed_paths))
        object.__setattr__(self, "validation", tuple(self.validation))


class WorkspaceExperimentRequestBuilder(Protocol):
    def build(
        self,
        *,
        repository_root: Path,
        issue_number: int,
        target: str,
        base_commit: str,
        proposal: WorkspaceCandidateProposal,
    ) -> WorkspaceCandidatePreparation: ...


class GitWorkspaceCandidatePreparer:
    def __init__(
        self,
        *,
        commands: CommandRunner,
        config: OptimizerConfig,
    ) -> None:
        self._commands = commands
        self._config = config

    def build(
        self,
        *,
        repository_root: Path,
        issue_number: int,
        target: str,
        base_commit: str,
        proposal: WorkspaceCandidateProposal,
    ) -> WorkspaceCandidatePreparation:
        root = repository_root.expanduser().resolve(strict=True)
        if (
            type(issue_number) is not int
            or issue_number < 1
            or re.fullmatch(r"[0-9a-f]{40}", base_commit) is None
        ):
            raise ValueError("workspace candidate base binding is invalid")
        configured = self._config.targets.get(target)
        if configured is None:
            raise ValueError("workspace candidate target is not configured")
        allowed_mutations = {
            getattr(item, "value", str(item))
            for item in configured.allowed_mutations
        }
        if proposal.mutation_class not in allowed_mutations:
            raise ValueError("workspace candidate mutation is not allowed")
        patch_sha256 = hashlib.sha256(proposal.exact_patch).hexdigest()
        preparation_root = (
            root
            / ".fw"
            / f"p{issue_number}-{proposal.candidate_id}-{patch_sha256[:8]}"
        )
        worktree = preparation_root / "tree"
        bundle_path = preparation_root / "candidate.zip"
        self._remove_worktree(root, worktree)
        shutil.rmtree(preparation_root, ignore_errors=True)
        preparation_root.mkdir(parents=True, exist_ok=True)
        try:
            self._run(
                (
                    "git",
                    "worktree",
                    "add",
                    "--detach",
                    str(worktree),
                    base_commit,
                ),
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
                raise RuntimeError(
                    "workspace candidate preparation is not clean"
                )
            self._run(
                ("git", "apply", "--check", "--binary", "--index", "-"),
                worktree,
                input_bytes=proposal.exact_patch,
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
                input_bytes=proposal.exact_patch,
            )
            changed_paths = self._changed_paths(worktree)
            self._validate_changed_paths(
                changed_paths,
                tuple(str(path) for path in configured.edit_paths),
            )
            expected_tree = self._run(
                ("git", "write-tree"), worktree
            ).stdout.strip()
            validation = self._run_validation(
                worktree,
                tuple(configured.validation_commands),
            )
            self._run(("git", "clean", "-fdx"), worktree)
            if (
                self._changed_paths(worktree) != changed_paths
                or self._run(
                    ("git", "diff", "--name-only", "-z"), worktree
                ).stdout
                or self._run(
                    ("git", "write-tree"), worktree
                ).stdout.strip()
                != expected_tree
            ):
                raise RuntimeError(
                    "workspace validation changed the exact candidate tree"
                )
            artifact = build_source_bundle(
                BundleRequest(
                    repository_root=worktree,
                    output_path=bundle_path,
                    include=tuple(
                        str(pattern)
                        for pattern in configured.package.include
                    ),
                    exclude=tuple(
                        str(pattern)
                        for pattern in configured.package.exclude
                    ),
                    dependency_resolution=(
                        configured.runtime.dependency_resolution
                        or "remote_build"
                    ),
                    evidence_paths=(
                        Path(str(self._config.campaign.evidence_path)),
                    ),
                )
            )
            evidence_sha256 = _preparation_evidence_sha256(
                issue_number=issue_number,
                target=target,
                base_commit=base_commit,
                candidate_id=proposal.candidate_id,
                mutation_class=proposal.mutation_class,
                patch_sha256=patch_sha256,
                bundle_sha256=artifact.sha256,
                expected_tree=expected_tree,
                changed_paths=changed_paths,
                validation=validation,
                validation_commands=tuple(
                    configured.validation_commands
                ),
            )
            idempotency_key = _preparation_idempotency_key(
                issue_number=issue_number,
                target=target,
                base_commit=base_commit,
                candidate_id=proposal.candidate_id,
                patch_sha256=patch_sha256,
                bundle_sha256=artifact.sha256,
                evidence_sha256=evidence_sha256,
            )
            return WorkspaceCandidatePreparation(
                request=CandidateExperimentRequest(
                    issue_number=issue_number,
                    candidate_id=proposal.candidate_id,
                    patch_sha256=patch_sha256,
                    bundle_sha256=artifact.sha256,
                    evidence_sha256=evidence_sha256,
                    idempotency_key=idempotency_key,
                ),
                mutation_class=proposal.mutation_class,
                changed_paths=changed_paths,
                validation=validation,
                expected_tree=expected_tree,
            )
        finally:
            self._remove_worktree(root, worktree)
            shutil.rmtree(preparation_root, ignore_errors=True)

    def _run_validation(
        self,
        worktree: Path,
        configured: tuple[str, ...],
    ) -> tuple[str, ...]:
        commands = tuple(
            tuple(shlex.split(command, posix=True))
            for command in configured
        )
        if not commands or any(not command for command in commands):
            raise ValueError("workspace validation commands are invalid")
        report = run_validation(
            ValidationRequest(
                repository_root=worktree,
                commands=commands,
            ),
            self._commands,
        )
        if not report.passed or len(report.results) != len(commands):
            raise RuntimeError("workspace candidate validation failed")
        return tuple(
            f"{' '.join(result.command[:2])}: passed"
            for result in report.results
        )

    def _changed_paths(self, worktree: Path) -> tuple[str, ...]:
        return tuple(
            path
            for path in self._run(
                (
                    "git",
                    "diff",
                    "--cached",
                    "--name-only",
                    "-z",
                ),
                worktree,
            ).stdout.split("\0")
            if path
        )

    @staticmethod
    def _validate_changed_paths(
        changed_paths: tuple[str, ...],
        edit_paths: tuple[str, ...],
    ) -> None:
        allowed = tuple(
            PurePosixPath(path.replace("\\", "/")) for path in edit_paths
        )
        if not changed_paths or any(
            not any(
                PurePosixPath(path) == prefix
                or prefix in PurePosixPath(path).parents
                for prefix in allowed
            )
            for path in changed_paths
        ):
            raise ValueError(
                "workspace candidate changed paths are not allowed"
            )

    def _run(
        self,
        arguments: tuple[str, ...],
        cwd: Path,
        *,
        input_bytes: bytes | None = None,
    ):
        return self._commands.run(
            arguments,
            cwd=cwd,
            input_bytes=input_bytes,
        )

    def _remove_worktree(self, root: Path, worktree: Path) -> None:
        try:
            self._run(
                ("git", "worktree", "remove", "--force", str(worktree)),
                root,
            )
        except Exception:
            pass


@dataclass(frozen=True)
class TrustedWorkspaceExperimentResultContext:
    delivery_id: str
    repository: str
    repository_id: int

    def __post_init__(self) -> None:
        if (
            re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}",
                self.delivery_id,
            )
            is None
            or re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/"
                r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}",
                self.repository,
            )
            is None
            or type(self.repository_id) is not int
            or self.repository_id < 1
        ):
            raise ValueError(
                "trusted workspace experiment context is invalid"
            )


@dataclass(frozen=True)
class NormalizedWorkspaceExperimentResult:
    issue_number: int
    repository: str
    repository_id: int
    delivery_id: str
    result: CandidateExperimentResult


def normalize_workspace_experiment_result(
    payload: Mapping[str, Any],
    context: TrustedWorkspaceExperimentResultContext,
) -> NormalizedWorkspaceExperimentResult:
    reject_secret_content(payload)
    expected = {
        "bundle_sha256",
        "candidate_id",
        "draft_id",
        "evaluation_id",
        "evidence_sha256",
        "executor",
        "guardrails",
        "idempotency_key",
        "issue_number",
        "metrics",
        "operation_sha256",
        "repository",
        "run_id",
        "schema_version",
    }
    if set(payload) != expected or payload["schema_version"] != 1:
        raise ValueError("trusted experiment result fields are invalid")
    repository = payload["repository"]
    if (
        not isinstance(repository, Mapping)
        or set(repository) != {"full_name", "id"}
        or repository["full_name"] != context.repository
        or repository["id"] != context.repository_id
    ):
        raise ValueError("trusted experiment repository changed")
    issue_number = payload["issue_number"]
    if type(issue_number) is not int or issue_number < 1:
        raise ValueError("trusted experiment issue is invalid")
    result = CandidateExperimentResult(
        candidate_id=payload["candidate_id"],
        executor=payload["executor"],
        metrics=payload["metrics"],
        guardrails=payload["guardrails"],
        draft_id=payload["draft_id"],
        evaluation_id=payload["evaluation_id"],
        run_id=payload["run_id"],
        bundle_sha256=payload["bundle_sha256"],
        evidence_sha256=payload["evidence_sha256"],
        operation_sha256=payload["operation_sha256"],
        idempotency_key=payload["idempotency_key"],
    )
    return NormalizedWorkspaceExperimentResult(
        issue_number=issue_number,
        repository=context.repository,
        repository_id=context.repository_id,
        delivery_id=context.delivery_id,
        result=result,
    )


@dataclass(frozen=True)
class WorkspaceExperimentExecutionResult:
    issue_number: int
    candidate_id: str
    status: str
    recorded: bool
    operation_sha256: str
    idempotency_key: str
    next_action: str

    @property
    def exit_code(self) -> int:
        return 0

    def to_dict(self) -> dict[str, bool | int | str]:
        return {
            "candidate_id": self.candidate_id,
            "idempotency_key": self.idempotency_key,
            "issue_number": self.issue_number,
            "next_action": self.next_action,
            "operation_sha256": self.operation_sha256,
            "recorded": self.recorded,
            "status": self.status,
        }


class WorkspaceExperimentExecutor:
    def __init__(
        self,
        *,
        store: WorkspaceStore,
        runner: CandidateExperimentAdapter | None,
        request_builder: WorkspaceExperimentRequestBuilder | None,
    ) -> None:
        self._store = store
        self._runner = runner
        self._request_builder = request_builder

    def execute(
        self,
        *,
        repository_root: Path,
        issue_number: int,
        target: str,
        base_commit: str,
        proposal: WorkspaceCandidateProposal,
    ) -> WorkspaceExperimentExecutionResult:
        snapshot = self._store.load(issue_number)
        if self._runner is None or self._request_builder is None:
            raise ValueError("workspace experiment executor is unavailable")
        if snapshot is None or snapshot.phase not in {
            WorkspacePhase.SPECIFICATION,
            WorkspacePhase.EVALUATING,
        }:
            raise ValueError("workspace experiment state is unavailable")
        if (
            snapshot.specification is None
            or snapshot.specification.status != "policy_approved"
            or snapshot.specification.target != target
            or snapshot.specification.base_commit != base_commit
            or snapshot.baseline is None
            or snapshot.baseline.status != "completed"
        ):
            raise ValueError(
                "trusted workspace specification and baseline are required"
            )
        existing = {
            item.candidate_id: item for item in snapshot.experiments
        }.get(proposal.candidate_id)
        pending_recorded = False
        if existing is not None:
            request = _request_from_record(issue_number, existing)
            _validate_proposal_record(proposal, existing)
            if existing.status == "completed":
                return _execution_result(
                    issue_number,
                    existing,
                    recorded=False,
                )
        else:
            preparation = self._request_builder.build(
                repository_root=repository_root,
                issue_number=issue_number,
                target=target,
                base_commit=base_commit,
                proposal=proposal,
            )
            _validate_preparation(proposal, preparation)
            request = preparation.request
            operation_sha256 = CandidateExperimentOperation.from_request(
                request
            ).sha256
            pending = WorkspaceExperimentRecord(
                candidate_id=request.candidate_id,
                mutation_class=preparation.mutation_class,
                patch_sha256=request.patch_sha256,
                bundle_sha256=request.bundle_sha256,
                evidence_sha256=request.evidence_sha256,
                idempotency_key=request.idempotency_key,
                operation_sha256=operation_sha256,
                status="pending",
                changed_paths=preparation.changed_paths,
                validation=preparation.validation,
                expected_tree=preparation.expected_tree,
            )
            snapshot = self._store.commit(
                expected_revision=snapshot.revision,
                update=WorkspaceUpdate(
                    issue_number=issue_number,
                    phase=WorkspacePhase.EVALUATING,
                    workspace_pull_request_number=(
                        snapshot.workspace_pull_request_number
                    ),
                    semantic_event=(
                        f"candidate_experiment_started_"
                        f"{proposal.candidate_id}"
                    ),
                    candidates=snapshot.candidates,
                    selected_patch=snapshot.selected_patch,
                    external_operation_ids=(
                        *snapshot.external_operation_ids,
                        f"experiment_operation:{operation_sha256}",
                    ),
                    experiments=(*snapshot.experiments, pending),
                    lineage=snapshot.lineage,
                    specification=snapshot.specification,
                    baseline=snapshot.baseline,
                ),
            )
            existing = pending
            pending_recorded = True
        assert existing is not None
        try:
            result = self._runner.evaluate(request)
        except CandidateExperimentPending:
            return _execution_result(
                issue_number,
                existing,
                recorded=pending_recorded,
            )
        completed = _completed_record(issue_number, existing, result)
        records = tuple(
            completed if item.candidate_id == completed.candidate_id else item
            for item in snapshot.experiments
        )
        self._store.commit(
            expected_revision=snapshot.revision,
            update=WorkspaceUpdate(
                issue_number=issue_number,
                phase=WorkspacePhase.EVALUATING,
                workspace_pull_request_number=(
                    snapshot.workspace_pull_request_number
                ),
                semantic_event=(
                    f"candidate_experiment_completed_"
                    f"{proposal.candidate_id}"
                ),
                candidates=snapshot.candidates,
                selected_patch=snapshot.selected_patch,
                external_operation_ids=tuple(
                    dict.fromkeys(
                        (
                            *snapshot.external_operation_ids,
                            result.draft_id,
                            result.evaluation_id,
                            result.run_id,
                            (
                                f"{result.candidate_id}:bundle:"
                                f"{result.bundle_sha256}"
                            ),
                            (
                                f"{result.candidate_id}:evidence:"
                                f"{result.evidence_sha256}"
                            ),
                        )
                    )
                ),
                experiments=records,
                lineage=snapshot.lineage,
                specification=snapshot.specification,
                baseline=snapshot.baseline,
            ),
        )
        return _execution_result(
            issue_number,
            completed,
            recorded=True,
        )

    def ingest_result(
        self,
        *,
        issue_number: int,
        result: CandidateExperimentResult,
    ) -> WorkspaceExperimentExecutionResult:
        snapshot = self._store.load(issue_number)
        if snapshot is None:
            raise ValueError("workspace experiment state is unavailable")
        pending = next(
            (
                item
                for item in snapshot.experiments
                if item.candidate_id == result.candidate_id
            ),
            None,
        )
        if pending is None:
            raise ValueError("workspace pending experiment is missing")
        if pending.status == "completed":
            if _result_from_record(pending) != result:
                raise ValueError("trusted workspace experiment changed")
            return _execution_result(
                issue_number,
                pending,
                recorded=False,
            )
        completed = _completed_record(issue_number, pending, result)
        records = tuple(
            completed if item.candidate_id == completed.candidate_id else item
            for item in snapshot.experiments
        )
        self._store.commit(
            expected_revision=snapshot.revision,
            update=WorkspaceUpdate(
                issue_number=issue_number,
                phase=WorkspacePhase.EVALUATING,
                workspace_pull_request_number=(
                    snapshot.workspace_pull_request_number
                ),
                semantic_event=(
                    f"candidate_experiment_ingested_"
                    f"{result.candidate_id}"
                ),
                candidates=snapshot.candidates,
                selected_patch=snapshot.selected_patch,
                external_operation_ids=tuple(
                    dict.fromkeys(
                        (
                            *snapshot.external_operation_ids,
                            result.draft_id,
                            result.evaluation_id,
                            result.run_id,
                            (
                                f"{result.candidate_id}:bundle:"
                                f"{result.bundle_sha256}"
                            ),
                            (
                                f"{result.candidate_id}:evidence:"
                                f"{result.evidence_sha256}"
                            ),
                        )
                    )
                ),
                experiments=records,
                lineage=snapshot.lineage,
                specification=snapshot.specification,
                baseline=snapshot.baseline,
            ),
        )
        return _execution_result(
            issue_number,
            completed,
            recorded=True,
        )


def _completed_record(
    issue_number: int,
    pending: WorkspaceExperimentRecord,
    result: CandidateExperimentResult,
) -> WorkspaceExperimentRecord:
    request = _request_from_record(issue_number, pending)
    operation_sha256 = CandidateExperimentOperation.from_request(
        request
    ).sha256
    if (
        result.candidate_id != request.candidate_id
        or result.bundle_sha256 != request.bundle_sha256
        or result.evidence_sha256 != request.evidence_sha256
        or result.idempotency_key != request.idempotency_key
        or result.operation_sha256 != operation_sha256
    ):
        raise ValueError("trusted workspace experiment lineage changed")
    return WorkspaceExperimentRecord(
        candidate_id=request.candidate_id,
        mutation_class=pending.mutation_class,
        patch_sha256=request.patch_sha256,
        bundle_sha256=request.bundle_sha256,
        evidence_sha256=request.evidence_sha256,
        idempotency_key=request.idempotency_key,
        operation_sha256=operation_sha256,
        status="completed",
        changed_paths=pending.changed_paths,
        validation=pending.validation,
        expected_tree=pending.expected_tree,
        executor=result.executor,
        draft_id=result.draft_id,
        evaluation_id=result.evaluation_id,
        run_id=result.run_id,
        metrics=result.metrics,
        guardrails=result.guardrails,
    )


def _validate_preparation(
    proposal: WorkspaceCandidateProposal,
    preparation: WorkspaceCandidatePreparation,
) -> None:
    request = preparation.request
    if (
        request.candidate_id != proposal.candidate_id
        or request.patch_sha256 != proposal.patch_sha256
        or preparation.mutation_class != proposal.mutation_class
    ):
        raise ValueError("workspace experiment proposal binding changed")


def _validate_proposal_record(
    proposal: WorkspaceCandidateProposal,
    record: WorkspaceExperimentRecord,
) -> None:
    if (
        record.candidate_id != proposal.candidate_id
        or record.patch_sha256 != proposal.patch_sha256
        or record.mutation_class != proposal.mutation_class
    ):
        raise ValueError("workspace experiment proposal binding changed")


def _request_from_record(
    issue_number: int,
    record: WorkspaceExperimentRecord,
) -> CandidateExperimentRequest:
    return CandidateExperimentRequest(
        issue_number=issue_number,
        candidate_id=record.candidate_id,
        patch_sha256=record.patch_sha256,
        bundle_sha256=record.bundle_sha256,
        evidence_sha256=record.evidence_sha256,
        idempotency_key=record.idempotency_key,
    )


def _result_from_record(
    record: WorkspaceExperimentRecord,
) -> CandidateExperimentResult:
    if record.status != "completed":
        raise ValueError("workspace experiment is not completed")
    return CandidateExperimentResult(
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


def _execution_result(
    issue_number: int,
    record: WorkspaceExperimentRecord,
    *,
    recorded: bool,
) -> WorkspaceExperimentExecutionResult:
    return WorkspaceExperimentExecutionResult(
        issue_number=issue_number,
        candidate_id=record.candidate_id,
        status=record.status,
        recorded=recorded,
        operation_sha256=record.operation_sha256,
        idempotency_key=record.idempotency_key,
        next_action=(
            "experiments_complete"
            if record.status == "completed"
            else "await_trusted_actions_result"
        ),
    )


def _preparation_evidence_sha256(**values: Any) -> str:
    return _canonical_sha256(
        {
            "kind": "workspace_candidate_preparation",
            "schema_version": 1,
            **values,
        }
    )


def _preparation_idempotency_key(**values: Any) -> str:
    return _canonical_sha256(
        {
            "kind": "workspace_candidate_experiment",
            "schema_version": 1,
            **values,
        }
    )


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "GitWorkspaceCandidatePreparer",
    "NormalizedWorkspaceExperimentResult",
    "TrustedWorkspaceExperimentResultContext",
    "WorkspaceCandidatePreparation",
    "WorkspaceExperimentExecutionResult",
    "WorkspaceExperimentExecutor",
    "WorkspaceExperimentRequestBuilder",
    "normalize_workspace_experiment_result",
]
