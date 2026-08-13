from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from pathlib import Path, PurePosixPath
import shlex
import shutil
from typing import Any

from foundry_opt.adapters.candidate_experiments import (
    ActionsCandidateExperimentAdapter,
    build_production_direct_candidate_experiment_adapter,
    FoundryCandidateExperimentOperation,
)
from foundry_opt.adapters.commands import (
    CommandError,
    SubprocessCommandRunner,
)
from foundry_opt.adapters.drafts import DraftGateway
from foundry_opt.adapters.environment import OsEnvironmentReader
from foundry_opt.adapters.github import github_repository_from_remote_url
from foundry_opt.adapters.optimization_evaluation import (
    build_evaluation_policy,
)
from foundry_opt.auth import build_production_auth_probe
from foundry_opt.config import load_config
from foundry_opt.evaluation import DatasetSplit
from foundry_opt.optimization import materialize_prepared_asset
from foundry_opt.optimization.models import EvaluationAssetContext
from foundry_opt.optimization.production import (
    _DEFAULT_CONFIG_PATH,
    _RegistrationGateway,
    _default_binder_factory,
    _default_credential_provider,
    _default_registration_gateway_factory,
    _default_resolution_gateway_factory,
    _draft_request,
    build_specification_asset_registry,
)
from foundry_opt.optimization.runner import _asset_reference
from foundry_opt.optimization.specification import (
    OptimizationSpecService,
    SpecServiceStatus,
)
from foundry_opt.orchestration.candidate_experiments import (
    CandidateExperimentActionsGateway,
    CandidateExperimentAdapter,
    CandidateExperimentOperation,
    CandidateExperimentPlan,
    CandidateExperimentRequest,
    CandidateExperimentResult,
    CandidateExperimentRunner,
    PersistedCandidateExperimentOperation,
)
from foundry_opt.orchestration.workspace import WorkspaceIssue
from foundry_opt.orchestration.workspace_baseline import (
    WorkspaceBaselinePlan,
    WorkspaceBaselineRequestBuilder,
)
from foundry_opt.orchestration.workspace_experiments import (
    _preparation_evidence_sha256,
    _preparation_idempotency_key,
)
from foundry_opt.orchestration.workspace_git_store import GitWorkspaceStore
from foundry_opt.orchestration.workspace_manifest import (
    parse_workspace_candidate_manifest,
)
from foundry_opt.orchestration.workspace_operation_store import (
    GitWorkspaceOperationStore,
)
from foundry_opt.orchestration.workspace_specification import (
    _ReadOnlySpecificationPublisher,
    _WorkspaceSpecificationGateway,
)
from foundry_opt.orchestration.workspace_store import WorkspaceBaselineRecord
from foundry_opt.packaging import BundleRequest, build_source_bundle
from foundry_opt.packaging.validation import (
    ValidationRequest,
    run_validation,
)
from foundry_opt.preflight.interfaces import CommandRunner
from foundry_opt.security import reject_secret_content


class GitWorkspaceBaselineBuilder(WorkspaceBaselineRequestBuilder):
    def __init__(
        self,
        *,
        commands: CommandRunner | None = None,
        config_path: Path = _DEFAULT_CONFIG_PATH,
    ) -> None:
        self._commands = commands or SubprocessCommandRunner()
        self._config_path = config_path

    def build(
        self,
        *,
        repository_root: Path,
        issue_number: int,
        target: str,
        base_commit: str,
        specification,
    ) -> WorkspaceBaselinePlan:
        root = repository_root.expanduser().resolve(strict=True)
        config = load_config(root / self._config_path)
        configured = config.targets.get(target)
        if configured is None:
            raise ValueError("workspace baseline target is not configured")
        issue_request = _load_issue_request(
            self._commands,
            root,
            issue_number,
        )
        if (
            issue_request.target != target
            or specification.spec_sha256 is None
            or specification.base_commit != base_commit
        ):
            raise ValueError("trusted workspace specification is required")
        artifact = _bundle_from_base(
            commands=self._commands,
            root=root,
            issue_number=issue_number,
            prefix="baseline-plan",
            base_commit=base_commit,
            include=tuple(
                str(pattern) for pattern in configured.package.include
            ),
            exclude=tuple(
                str(pattern) for pattern in configured.package.exclude
            ),
            evidence_root=Path(str(config.campaign.evidence_path)),
            dependency_resolution=(
                configured.runtime.dependency_resolution or "remote_build"
            ),
        )
        try:
            dataset_ids = tuple(
                asset.asset_id for asset in issue_request.datasets
            )
            evaluator_ids = tuple(
                asset.asset_id for asset in issue_request.evaluators
            )
            evidence_sha256 = _canonical_sha256(
                {
                    "base_commit": base_commit,
                    "bundle_sha256": artifact.bundle.sha256,
                    "dataset_ids": list(dataset_ids),
                    "development_suite": "development",
                    "evaluator_ids": list(evaluator_ids),
                    "issue_number": issue_number,
                    "kind": "workspace_baseline",
                    "published_base_version": (
                        configured.base_agent_version
                    ),
                    "schema_version": 1,
                    "spec_sha256": specification.spec_sha256,
                    "target": target,
                }
            )
            idempotency_key = _canonical_sha256(
                {
                    "base_commit": base_commit,
                    "bundle_sha256": artifact.bundle.sha256,
                    "development_suite": "development",
                    "evidence_sha256": evidence_sha256,
                    "issue_number": issue_number,
                    "kind": "workspace_baseline_operation",
                    "published_base_version": (
                        configured.base_agent_version
                    ),
                    "schema_version": 1,
                    "target": target,
                }
            )
            return WorkspaceBaselinePlan(
                request=CandidateExperimentRequest(
                    issue_number=issue_number,
                    candidate_id="baseline",
                    patch_sha256=specification.spec_sha256,
                    bundle_sha256=artifact.bundle.sha256,
                    evidence_sha256=evidence_sha256,
                    idempotency_key=idempotency_key,
                ),
                dataset_ids=dataset_ids,
                evaluator_ids=evaluator_ids,
                sample_count=max(1, len(dataset_ids)),
            )
        finally:
            artifact.cleanup()


class GitWorkspaceActionsGateway(CandidateExperimentActionsGateway):
    def __init__(self, repository_root: Path) -> None:
        self._root = repository_root.expanduser().resolve(strict=True)
        self._store = GitWorkspaceStore(self._root)

    def persist(
        self,
        operation: CandidateExperimentOperation,
    ) -> PersistedCandidateExperimentOperation:
        snapshot = self._store.load(operation.issue_number)
        if snapshot is None:
            raise ValueError("workspace state is unavailable")
        if operation.candidate_id == "baseline":
            baseline = snapshot.baseline
            if (
                baseline is None
                or baseline.operation_sha256 != operation.sha256
            ):
                raise ValueError("workspace baseline operation changed")
        else:
            record = next(
                (
                    item
                    for item in snapshot.experiments
                    if item.candidate_id == operation.candidate_id
                ),
                None,
            )
            if (
                record is None
                or record.operation_sha256 != operation.sha256
            ):
                raise ValueError("workspace experiment operation changed")
        return PersistedCandidateExperimentOperation(
            operation=operation,
            reference=(
                f"foundry-opt/operations/issue-{operation.issue_number}/"
                f"{operation.candidate_id}.json"
            ),
            sha256=operation.sha256,
        )

    def dispatch(
        self,
        persisted: PersistedCandidateExperimentOperation,
    ) -> None:
        del persisted
        return None

    def reconcile(
        self,
        persisted: PersistedCandidateExperimentOperation,
    ) -> CandidateExperimentResult | None:
        snapshot = self._store.load(persisted.operation.issue_number)
        if snapshot is None:
            return None
        if persisted.operation.candidate_id == "baseline":
            baseline = snapshot.baseline
            if (
                baseline is None
                or baseline.status != "completed"
                or baseline.operation_sha256 != persisted.sha256
            ):
                return None
            return _baseline_result(baseline)
        record = next(
            (
                item
                for item in snapshot.experiments
                if item.candidate_id == persisted.operation.candidate_id
                and item.operation_sha256 == persisted.sha256
            ),
            None,
        )
        if record is None or record.status != "completed":
            return None
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


class LiveWorkspaceExperimentAdapter(CandidateExperimentAdapter):
    def __init__(
        self,
        *,
        repository_root: Path,
        commands: CommandRunner | None = None,
        config_path: Path = _DEFAULT_CONFIG_PATH,
    ) -> None:
        self._root = repository_root.expanduser().resolve(strict=True)
        self._commands = commands or SubprocessCommandRunner()
        self._config_path = config_path
        self._environment = OsEnvironmentReader()
        self._credential = _default_credential_provider(self._environment)
        self._operations = GitWorkspaceOperationStore(self._root)
        self._draft_gateway = DraftGateway(self._credential)

    def evaluate(
        self,
        request: CandidateExperimentRequest,
    ) -> CandidateExperimentResult:
        prepared = (
            self._prepare_baseline(request)
            if request.candidate_id == "baseline"
            else self._prepare_candidate(request)
        )
        operation = FoundryCandidateExperimentOperation(
            draft_gateway=self._draft_gateway,
            resolve_plan=lambda resolved: _resolved_plan(
                expected=request,
                actual=resolved,
                plan=prepared.plan,
            ),
            executor="workspace_oidc",
        )
        try:
            return operation.evaluate(request)
        finally:
            prepared.cleanup()

    def _prepare_baseline(
        self,
        request: CandidateExperimentRequest,
    ) -> "_PreparedExecution":
        inputs = _trusted_execution_inputs(
            commands=self._commands,
            root=self._root,
            config_path=self._config_path,
            issue_number=request.issue_number,
            base_commit=_workspace_spec_base(
                self._root,
                request.issue_number,
            ),
        )
        if request.patch_sha256 != inputs.spec.sha256:
            raise ValueError("workspace baseline lineage changed")
        artifact = _bundle_from_base(
            commands=self._commands,
            root=self._root,
            issue_number=request.issue_number,
            prefix="baseline-run",
            base_commit=inputs.spec.base_commit,
            include=tuple(
                str(pattern)
                for pattern in inputs.target.package.include
            ),
            exclude=tuple(
                str(pattern)
                for pattern in inputs.target.package.exclude
            ),
            evidence_root=Path(str(inputs.config.campaign.evidence_path)),
            dependency_resolution=(
                inputs.target.runtime.dependency_resolution
                or "remote_build"
            ),
        )
        if artifact.bundle.sha256 != request.bundle_sha256:
            artifact.cleanup()
            raise ValueError("workspace baseline bundle changed")
        return _PreparedExecution(
            plan=_execution_plan(
                inputs=inputs,
                request=request,
                bundle=artifact.bundle,
            ),
            cleanup=artifact.cleanup,
        )

    def _prepare_candidate(
        self,
        request: CandidateExperimentRequest,
    ) -> "_PreparedExecution":
        payload = self._operations.load_candidate_manifest(
            request.issue_number,
            request.candidate_id,
        )
        if payload is None:
            raise ValueError("workspace candidate manifest is unavailable")
        manifest = parse_workspace_candidate_manifest(payload)
        inputs = _trusted_execution_inputs(
            commands=self._commands,
            root=self._root,
            config_path=self._config_path,
            issue_number=request.issue_number,
            base_commit=_workspace_spec_base(
                self._root,
                request.issue_number,
            ),
        )
        if (
            manifest.target != inputs.request.target
            or manifest.base_commit != inputs.spec.base_commit
            or manifest.candidate.candidate_id != request.candidate_id
        ):
            raise ValueError("workspace candidate lineage changed")
        artifact = _bundle_from_candidate(
            commands=self._commands,
            root=self._root,
            issue_number=request.issue_number,
            target=inputs.request.target,
            base_commit=inputs.spec.base_commit,
            proposal=manifest.candidate,
            include=tuple(
                str(pattern)
                for pattern in inputs.target.package.include
            ),
            exclude=tuple(
                str(pattern)
                for pattern in inputs.target.package.exclude
            ),
            evidence_root=Path(str(inputs.config.campaign.evidence_path)),
            validation_commands=tuple(
                inputs.target.validation_commands
            ),
            dependency_resolution=(
                inputs.target.runtime.dependency_resolution
                or "remote_build"
            ),
            allowed_paths=tuple(
                str(path) for path in inputs.target.edit_paths
            ),
        )
        try:
            if artifact.request != request:
                raise ValueError(
                    "workspace candidate preparation changed"
                )
            return _PreparedExecution(
                plan=_execution_plan(
                    inputs=inputs,
                    request=request,
                    bundle=artifact.bundle,
                ),
                cleanup=artifact.cleanup,
            )
        except Exception:
            artifact.cleanup()
            raise


class _PreparedExecution:
    def __init__(
        self,
        *,
        plan: CandidateExperimentPlan,
        cleanup,
    ) -> None:
        self.plan = plan
        self.cleanup = cleanup


class _TrustedExecutionInputs:
    def __init__(
        self,
        *,
        config,
        target,
        repository: str,
        issue: WorkspaceIssue,
        request,
        spec,
        assets: tuple[EvaluationAssetReference, ...],
    ) -> None:
        self.config = config
        self.target = target
        self.repository = repository
        self.issue = issue
        self.request = request
        self.spec = spec
        self.assets = assets


class _BaseBundleArtifact:
    def __init__(
        self,
        *,
        bundle,
        temp_root: Path,
        worktree: Path,
        root: Path,
    ) -> None:
        self.bundle = bundle
        self._temp_root = temp_root
        self._worktree = worktree
        self._root = root

    def cleanup(self) -> None:
        _remove_worktree(self._root, self._worktree)
        shutil.rmtree(self._temp_root, ignore_errors=True)


class _CandidateBundleArtifact(_BaseBundleArtifact):
    def __init__(
        self,
        *,
        request: CandidateExperimentRequest,
        bundle,
        temp_root: Path,
        worktree: Path,
        root: Path,
    ) -> None:
        super().__init__(
            bundle=bundle,
            temp_root=temp_root,
            worktree=worktree,
            root=root,
        )
        self.request = request


def build_production_workspace_service_bindings(
    repository_root: Path,
    *,
    commands: CommandRunner | None = None,
    config_path: Path = _DEFAULT_CONFIG_PATH,
    actions_execution: bool = False,
) -> dict[str, object]:
    root = repository_root.expanduser().resolve(strict=True)
    runner = commands or SubprocessCommandRunner()
    direct_operation = LiveWorkspaceExperimentAdapter(
        repository_root=root,
        commands=runner,
        config_path=config_path,
    )
    experiment_runner: CandidateExperimentAdapter = direct_operation
    if not actions_execution:
        experiment_runner = CandidateExperimentRunner(
            direct=build_production_direct_candidate_experiment_adapter(
                repository_root=root,
                operation=direct_operation,
                auth_probe=build_production_auth_probe(),
            ),
            fallback=ActionsCandidateExperimentAdapter(
                GitWorkspaceActionsGateway(root)
            ),
        )
    return {
        "baseline_request_builder": GitWorkspaceBaselineBuilder(
            commands=runner,
            config_path=config_path,
        ),
        "experiment_runner": experiment_runner,
    }


def _trusted_execution_inputs(
    *,
    commands: CommandRunner,
    root: Path,
    config_path: Path,
    issue_number: int,
    base_commit: str | None = None,
) -> _TrustedExecutionInputs:
    config = load_config(root / config_path)
    repository = _repository_name(commands, root)
    issue_payload = _issue_payload(
        commands,
        root,
        repository,
        issue_number,
    )
    base_branch = _default_branch_name(commands, root)
    resolved_base_commit = base_commit or _default_branch_commit(
        commands,
        root,
        base_branch,
    )
    if (
        not isinstance(resolved_base_commit, str)
        or len(resolved_base_commit) != 40
    ):
        raise ValueError("workspace execution base commit is invalid")
    commands.run(
        (
            "git",
            "cat-file",
            "-e",
            f"{resolved_base_commit}^{{commit}}",
        ),
        cwd=root,
    )
    issue = WorkspaceIssue(
        number=issue_number,
        title=issue_payload["title"],
        body=issue_payload["body"],
        base_commit=resolved_base_commit,
    )
    issue_request = _parse_issue_request(
        issue_number=issue_number,
        repository=repository,
        body=issue_payload["body"],
    )
    target = config.targets.get(issue_request.target)
    if target is None:
        raise ValueError("workspace target is not configured")
    credential = _default_credential_provider(OsEnvironmentReader())
    registry = build_specification_asset_registry(
        resolution_gateway_factory=_default_resolution_gateway_factory(
            credential
        )
    )
    spec_result = OptimizationSpecService(
        config,
        registry=registry,
        gateway=_WorkspaceSpecificationGateway(
            repository=repository,
            base_branch=base_branch,
            issue=issue,
        ),
        publisher=_ReadOnlySpecificationPublisher(
            resolved_base_commit
        ),
        require_issue_label=False,
    ).prepare_specification(root, issue_number, publish=False)
    if (
        spec_result.status is not SpecServiceStatus.COMPLETE
        or spec_result.spec is None
    ):
        raise RuntimeError("trusted workspace specification is incomplete")
    environment = config.environments[target.environment]
    context = EvaluationAssetContext(
        repository_root=root,
        project_endpoint=str(environment.project_endpoint),
        target=issue_request.target,
        issue_number=issue_number,
    )
    prepared_assets = tuple(
        registry.prepare(asset, context)
        for asset in (*issue_request.datasets, *issue_request.evaluators)
    )
    registration = _RegistrationGateway(
        config,
        target.environment,
        _default_registration_gateway_factory(credential),
    )
    assets = tuple(
        _asset_reference(
            materialize_prepared_asset(prepared, registration)
        )
        for prepared in prepared_assets
    )
    if any(not _asset_reference_is_complete(asset) for asset in assets):
        raise RuntimeError("trusted workspace assets are incomplete")
    return _TrustedExecutionInputs(
        config=config,
        target=target,
        repository=repository,
        issue=issue,
        request=issue_request,
        spec=spec_result.spec,
        assets=assets,
    )


def _workspace_spec_base(root: Path, issue_number: int) -> str:
    snapshot = GitWorkspaceStore(root).load(issue_number)
    if snapshot is None or snapshot.specification is None:
        raise ValueError("workspace specification is unavailable")
    return snapshot.specification.base_commit


def _asset_reference_is_complete(
    asset: EvaluationAssetReference,
) -> bool:
    return (
        asset.remote_id not in {None, ""}
        or asset.content_sha256 not in {None, ""}
    )


def _execution_plan(
    *,
    inputs: _TrustedExecutionInputs,
    request: CandidateExperimentRequest,
    bundle,
) -> CandidateExperimentPlan:
    endpoint = str(
        inputs.config.environments[
            inputs.target.environment
        ].project_endpoint
    )
    binder = _default_binder_factory(
        _default_credential_provider(OsEnvironmentReader()),
        inputs.config,
    )
    return CandidateExperimentPlan(
        patch_sha256=request.patch_sha256,
        evidence_sha256=request.evidence_sha256,
        draft_request=_draft_request(
            inputs.request.target,
            inputs.target,
            endpoint,
            bundle,
            idempotency_key=request.idempotency_key,
            subject=request.candidate_id,
        ),
        split=DatasetSplit.DEVELOPMENT,
        policy=build_evaluation_policy(inputs.spec),
        evaluate=binder(endpoint)(inputs.spec, inputs.assets),
    )


def _bundle_from_base(
    *,
    commands: CommandRunner,
    root: Path,
    issue_number: int,
    prefix: str,
    base_commit: str,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
    evidence_root: Path,
    dependency_resolution: str,
) -> _BaseBundleArtifact:
    temp_root = (
        root / ".fw" / f"{prefix}-{issue_number}-{base_commit[:8]}"
    )
    worktree = temp_root / "tree"
    bundle_path = temp_root / "baseline.zip"
    temp_root.mkdir(parents=True, exist_ok=True)
    _prepare_detached_worktree(commands, root, worktree, base_commit)
    try:
        bundle = build_source_bundle(
            BundleRequest(
                repository_root=worktree,
                output_path=bundle_path,
                include=include,
                exclude=exclude,
                dependency_resolution=dependency_resolution,
                evidence_paths=(evidence_root,),
            )
        )
        return _BaseBundleArtifact(
            bundle=bundle,
            temp_root=temp_root,
            worktree=worktree,
            root=root,
        )
    except Exception:
        _remove_worktree(root, worktree)
        shutil.rmtree(temp_root, ignore_errors=True)
        raise


def _bundle_from_candidate(
    *,
    commands: CommandRunner,
    root: Path,
    issue_number: int,
    target: str,
    base_commit: str,
    proposal,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
    evidence_root: Path,
    validation_commands: tuple[str, ...],
    dependency_resolution: str,
    allowed_paths: tuple[str, ...],
) -> _CandidateBundleArtifact:
    patch_sha256 = hashlib.sha256(proposal.exact_patch).hexdigest()
    temp_root = (
        root
        / ".fw"
        / f"run-{issue_number}-{proposal.candidate_id}-{patch_sha256[:8]}"
    )
    worktree = temp_root / "tree"
    bundle_path = temp_root / "candidate.zip"
    temp_root.mkdir(parents=True, exist_ok=True)
    _prepare_detached_worktree(commands, root, worktree, base_commit)
    try:
        _run_command(
            commands,
            ("git", "apply", "--check", "--binary", "--index", "-"),
            worktree,
            input_bytes=proposal.exact_patch,
        )
        _run_command(
            commands,
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
        changed_paths = _changed_paths(commands, worktree)
        _validate_changed_paths(changed_paths, allowed_paths)
        expected_tree = _run_command(
            commands,
            ("git", "write-tree"),
            worktree,
        ).stdout.strip()
        validation = _run_validation(
            commands,
            worktree,
            validation_commands,
        )
        _run_command(commands, ("git", "clean", "-fdx"), worktree)
        if (
            _changed_paths(commands, worktree) != changed_paths
            or _run_command(
                commands,
                ("git", "diff", "--name-only", "-z"),
                worktree,
            ).stdout
            or _run_command(
                commands,
                ("git", "write-tree"),
                worktree,
            ).stdout.strip()
            != expected_tree
        ):
            raise RuntimeError(
                "workspace validation changed the exact candidate tree"
            )
        bundle = build_source_bundle(
            BundleRequest(
                repository_root=worktree,
                output_path=bundle_path,
                include=include,
                exclude=exclude,
                dependency_resolution=dependency_resolution,
                evidence_paths=(evidence_root,),
            )
        )
        evidence_sha256 = _preparation_evidence_sha256(
            issue_number=issue_number,
            target=target,
            base_commit=base_commit,
            candidate_id=proposal.candidate_id,
            mutation_class=proposal.mutation_class,
            patch_sha256=patch_sha256,
            bundle_sha256=bundle.sha256,
            expected_tree=expected_tree,
            changed_paths=changed_paths,
            validation=validation,
            validation_commands=validation_commands,
        )
        idempotency_key = _preparation_idempotency_key(
            issue_number=issue_number,
            target=target,
            base_commit=base_commit,
            candidate_id=proposal.candidate_id,
            patch_sha256=patch_sha256,
            bundle_sha256=bundle.sha256,
            evidence_sha256=evidence_sha256,
        )
        return _CandidateBundleArtifact(
            request=CandidateExperimentRequest(
                issue_number=issue_number,
                candidate_id=proposal.candidate_id,
                patch_sha256=patch_sha256,
                bundle_sha256=bundle.sha256,
                evidence_sha256=evidence_sha256,
                idempotency_key=idempotency_key,
            ),
            bundle=bundle,
            temp_root=temp_root,
            worktree=worktree,
            root=root,
        )
    except Exception:
        _remove_worktree(root, worktree)
        shutil.rmtree(temp_root, ignore_errors=True)
        raise


def _prepare_detached_worktree(
    commands: CommandRunner,
    root: Path,
    worktree: Path,
    base_commit: str,
) -> None:
    _remove_worktree(root, worktree)
    shutil.rmtree(worktree.parent, ignore_errors=True)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _run_command(
        commands,
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
    if _run_command(
        commands,
        (
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ),
        worktree,
    ).stdout:
        raise RuntimeError("workspace preparation is not clean")


def _remove_worktree(root: Path, worktree: Path) -> None:
    try:
        _run_command(
            SubprocessCommandRunner(),
            ("git", "worktree", "remove", "--force", str(worktree)),
            root,
        )
    except Exception:
        pass


def _run_validation(
    commands: CommandRunner,
    worktree: Path,
    configured: tuple[str, ...],
) -> tuple[str, ...]:
    commands_tuple = tuple(
        tuple(shlex.split(command, posix=True))
        for command in configured
    )
    if not commands_tuple or any(not command for command in commands_tuple):
        raise ValueError("workspace validation commands are invalid")
    report = run_validation(
        ValidationRequest(
            repository_root=worktree,
            commands=commands_tuple,
        ),
        commands,
    )
    if not report.passed or len(report.results) != len(commands_tuple):
        raise RuntimeError("workspace candidate validation failed")
    return tuple(
        f"{' '.join(result.command[:2])}: passed"
        for result in report.results
    )


def _changed_paths(
    commands: CommandRunner,
    worktree: Path,
) -> tuple[str, ...]:
    return tuple(
        item
        for item in _run_command(
            commands,
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


def _validate_changed_paths(
    changed_paths: tuple[str, ...],
    allowed_paths: tuple[str, ...],
) -> None:
    allowed = tuple(
        PurePosixPath(path.replace("\\", "/"))
        for path in allowed_paths
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


def _run_command(
    commands: CommandRunner,
    arguments: tuple[str, ...],
    cwd: Path,
    *,
    input_bytes: bytes | None = None,
):
    return commands.run(arguments, cwd=cwd, input_bytes=input_bytes)


def _load_issue_request(
    commands: CommandRunner,
    root: Path,
    issue_number: int,
):
    repository = _repository_name(commands, root)
    issue = _issue_payload(commands, root, repository, issue_number)
    return _parse_issue_request(
        issue_number=issue_number,
        repository=repository,
        body=issue["body"],
    )


def _issue_payload(
    commands: CommandRunner,
    root: Path,
    repository: str,
    issue_number: int,
) -> dict[str, Any]:
    try:
        payload = json.loads(
            commands.run(
                (
                    "gh",
                    "issue",
                    "view",
                    str(issue_number),
                    "--repo",
                    repository,
                    "--json",
                    "number,title,body,state",
                ),
                cwd=root,
            ).stdout
        )
    except (CommandError, json.JSONDecodeError) as error:
        raise RuntimeError("workspace issue is unavailable") from error
    if (
        not isinstance(payload, dict)
        or payload.get("number") != issue_number
        or payload.get("state") != "OPEN"
        or not isinstance(payload.get("title"), str)
        or not isinstance(payload.get("body"), str)
    ):
        raise ValueError("workspace optimization issue is invalid")
    reject_secret_content(payload["title"])
    reject_secret_content(payload["body"])
    return payload


def _repository_name(
    commands: CommandRunner,
    root: Path,
) -> str:
    try:
        remote = commands.run(
            ("git", "remote", "get-url", "origin"),
            cwd=root,
        ).stdout.strip()
    except CommandError as error:
        raise RuntimeError(
            "workspace repository origin is unavailable"
        ) from error
    repository = github_repository_from_remote_url(remote)
    if repository is None:
        raise ValueError("workspace repository origin is invalid")
    return repository


def _default_branch_name(
    commands: CommandRunner,
    root: Path,
) -> str:
    try:
        text = commands.run(
            ("git", "remote", "show", "origin"),
            cwd=root,
        ).stdout
    except CommandError as error:
        raise RuntimeError(
            "workspace default branch is unavailable"
        ) from error
    marker = "HEAD branch:"
    if marker not in text:
        raise RuntimeError("workspace default branch is unavailable")
    return text.split(marker, 1)[1].strip().splitlines()[0]


def _default_branch_commit(
    commands: CommandRunner,
    root: Path,
    branch: str,
) -> str:
    try:
        value = commands.run(
            ("git", "rev-parse", f"origin/{branch}"),
            cwd=root,
        ).stdout.strip().lower()
    except CommandError as error:
        raise RuntimeError(
            "workspace default branch commit is unavailable"
        ) from error
    if len(value) != 40:
        raise ValueError("workspace default branch commit is invalid")
    return value


def _parse_issue_request(
    *,
    issue_number: int,
    repository: str,
    body: str,
):
    from foundry_opt.optimization.issues import (
        parse_optimization_issue_request,
    )

    return parse_optimization_issue_request(
        issue_number=issue_number,
        repository=repository,
        body=body,
    )


def _baseline_result(
    record: WorkspaceBaselineRecord,
) -> CandidateExperimentResult:
    return CandidateExperimentResult(
        candidate_id="baseline",
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


def _resolved_plan(
    *,
    expected: CandidateExperimentRequest,
    actual: CandidateExperimentRequest,
    plan: CandidateExperimentPlan,
) -> CandidateExperimentPlan:
    if actual != expected:
        raise ValueError("workspace experiment request changed")
    return plan


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
    "GitWorkspaceActionsGateway",
    "GitWorkspaceBaselineBuilder",
    "LiveWorkspaceExperimentAdapter",
    "build_production_workspace_service_bindings",
]
