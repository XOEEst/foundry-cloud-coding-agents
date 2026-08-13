"""Live deployment coordination for the optimization RECONCILE lifecycle.

This module owns the production :class:`LiveDeploymentCoordinator`, the adapter
that satisfies the ``deployment`` seam of
:class:`~foundry_opt.optimization.lifecycle.LifecycleDependencies`
(the :class:`~foundry_opt.optimization.lifecycle.DeploymentCoordinator`
protocol). After the reconcile service merges the selected candidate pull
request it hands the coordinator an exact
:class:`~foundry_opt.optimization.lifecycle.DeploymentLifecycleRequest` — the
detected deployment workflow, the exact
:class:`~foundry_opt.deployment.OptimizationDeploymentLineage`, the selected
pull request, the *merge* commit, the project endpoint, and whether the
optimizer must auto-dispatch — and the coordinator observes (or, for an
autopilot manual-trigger workflow, dispatches exactly once) the deployment
workflow run bound to that exact commit, reads the published Foundry version
and its runtime provenance through Azure OIDC, and verifies both against the
lineage before reporting a typed
:class:`~foundry_opt.optimization.lifecycle.DeploymentOutcome`.

Design
------
Two typed seams keep the coordinator testable and honest:

``WorkflowRunGateway``
    Finds the GitHub Actions run for an exact workflow and event. Merge runs
    are additionally bound by head SHA. Manual runs carry the exact merge
    commit and a deterministic effect ID as declared ``workflow_dispatch``
    inputs because GitHub reports the dispatch ref tip, not the selected input
    commit, as ``headSha``. The production implementation is
    :class:`GhWorkflowRunGateway` (authenticated ``gh``).

``PublishedDeploymentReader``
    Reads the latest *numeric* published version for the target hosted agent
    and its deployed runtime, failing closed unless the recorded lineage digest
    matches the expected lineage. The production implementation is
    :class:`FoundryPublishedDeploymentReader` (Azure OIDC + the Foundry agent
    versions API, reusing the dedicated deployment identity).

The coordinator never republishes when the workflow already published: it
verifies the published version's metadata, source-bundle hash, tree hash, and
one-way lineage digest against the exact
:class:`~foundry_opt.deployment.OptimizationDeploymentLineage` and merge
commit.
It fails closed — raising
:class:`~foundry_opt.deployment.DeploymentLineageMismatchError` on a lineage
divergence and
:class:`~foundry_opt.optimization.runner.CapabilityUnavailableError` on a
missing or unavailable live binding — rather than fabricating a verified
deployment. Dispatch is idempotent (keyed by workflow + commit): a run that
already exists for the exact commit is observed, never re-dispatched, so a
retried reconcile never launches a duplicate deployment. Only identity, hashes,
versions, and links cross this boundary — never raw prompts, responses, or
dataset rows — and no name-based process operations are performed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Protocol

import yaml

from foundry_opt.adapters.commands import CommandError
from foundry_opt.adapters.github import github_repository_from_remote_url
from foundry_opt.config.models import OptimizerConfig
from foundry_opt.deployment import (
    DEPLOYMENT_OIDC_CLIENT_ID,
    DeployedRuntime,
    DeploymentError,
    DeploymentLineageMismatchError,
    DeploymentRecord,
    DeploymentResponseError,
    DeploymentTrigger,
    DeploymentWorkflow,
    DeploymentWorkflowRun,
    OptimizationDeploymentLineage,
    WorkflowRunStatus,
    optimization_deployment_lineage_sha256,
    verify_optimization_deployment_lineage,
)
from foundry_opt.optimization.lifecycle import (
    DeploymentLifecycleRequest,
    DeploymentOutcome,
    DeploymentOutcomeStatus,
)
from foundry_opt.optimization.runner import CapabilityUnavailableError
from foundry_opt.preflight.interfaces import CommandRunner


__all__ = [
    "GeneratedDeploymentPublisher",
    "GhWorkflowRunGateway",
    "LiveDeploymentCoordinator",
    "OptimizationDeploymentError",
    "PublishedDeployment",
    "PublishedDeploymentReader",
    "WorkflowRunGateway",
    "WorkflowRunQuery",
    "build_live_deployment_coordinator",
]


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_ACTIVE_STATUSES = frozenset({"active"})
_LINEAGE_PROVENANCE_KEY = "foundry-opt-lineage-sha256"

# The exact-commit ``workflow_dispatch`` input names the coordinator will use,
# in order of preference, when auto-dispatching a manual deployment workflow.
_DEFAULT_DISPATCH_INPUT_NAMES = ("selected_commit",)
_CORRELATION_INPUT_NAME = "foundry_opt_effect_id"

_COMPLETED_STATUSES = frozenset({"completed"})
_QUEUED_STATUSES = frozenset(
    {"queued", "requested", "waiting", "pending", "action_required"}
)
_IN_PROGRESS_STATUSES = frozenset({"in_progress"})
_SUCCESS_CONCLUSIONS = frozenset({"success"})
_CANCELLED_CONCLUSIONS = frozenset({"cancelled", "skipped", "neutral"})


class OptimizationDeploymentError(RuntimeError):
    """A deployment observation could not be completed and must fail closed.

    Raised for malformed coordinator inputs, an undispatchable workflow, or a
    ``gh``/response shape the coordinator refuses to interpret. It is never
    swallowed into a fabricated success.
    """


# ---------------------------------------------------------------------------
# Workflow-run gateway seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkflowRunQuery:
    """The exact identity of the deployment run the coordinator is after.

    ``events`` bounds the acceptable ``workflow_dispatch`` / ``push`` /
    ``workflow_run`` triggers; ``head_sha`` is the selected merge commit
    (never the pull-request head). ``match_head_sha`` is false only for manual
    dispatch because GitHub's run ``headSha`` is the ref tip; canonical
    orchestration separately binds those runs with the effect-ID input.
    """

    workflow_path: Path
    events: tuple[str, ...]
    head_sha: str
    trigger: DeploymentTrigger
    match_head_sha: bool = True
    display_title: str | None = None


class WorkflowRunGateway(Protocol):
    """Typed GitHub Actions run lookup and manual dispatch."""

    def find_run(
        self,
        repository_root: Path,
        *,
        query: WorkflowRunQuery,
    ) -> DeploymentWorkflowRun | None: ...

    def dispatch(
        self,
        repository_root: Path,
        *,
        workflow_path: Path,
        input_name: str,
        commit: str,
        correlation_input_name: str | None = None,
        correlation_id: str | None = None,
    ) -> None: ...


# ---------------------------------------------------------------------------
# Published deployment reader seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublishedDeployment:
    """The latest published Foundry version and its deployed runtime."""

    record: DeploymentRecord
    runtime: DeployedRuntime


class PublishedDeploymentReader(Protocol):
    """Reads the latest numeric published version and its runtime.

    Implementations must fail closed — raising
    :class:`~foundry_opt.deployment.DeploymentLineageMismatchError` — when the
    published version's recorded lineage digest does not match
    ``expected_lineage``. They return ``None`` only when no published version
    exists yet.
    """

    def read_latest(
        self,
        *,
        project_endpoint: str,
        agent_name: str,
        expected_lineage: OptimizationDeploymentLineage,
    ) -> PublishedDeployment | None: ...


class GeneratedDeploymentPublisher(Protocol):
    """Publishes a generated manual workflow's version via DeploymentGateway.

    Used only when the detected deployment workflow does not exist as a real
    GitHub Actions workflow (a product-generated manual model): the publisher
    builds the exact source bundle from a temporary worktree at the merge
    commit, invokes the dedicated ``DeploymentGateway`` with an
    :class:`~foundry_opt.deployment.OptimizationDeploymentLineage`, and then
    verifies the published selection. It returns a terminal
    :class:`~foundry_opt.optimization.lifecycle.DeploymentOutcome`.
    """

    def publish(
        self,
        request: DeploymentLifecycleRequest,
    ) -> DeploymentOutcome: ...


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class LiveDeploymentCoordinator:
    """Observe/dispatch, read, and verify a merged candidate's deployment."""

    def __init__(
        self,
        config: OptimizerConfig,
        workflow_gateway: WorkflowRunGateway,
        reader: PublishedDeploymentReader,
        *,
        publisher: GeneratedDeploymentPublisher | None = None,
        dispatch_input_names: Sequence[str] = _DEFAULT_DISPATCH_INPUT_NAMES,
        poll_attempts: int = 30,
        poll_interval_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not 1 <= poll_attempts <= 120:
            raise ValueError("poll_attempts must be between 1 and 120")
        if not 0 <= poll_interval_seconds <= 60:
            raise ValueError(
                "poll_interval_seconds must be between zero and 60"
            )
        names = tuple(dispatch_input_names)
        if not names or any(not name for name in names):
            raise ValueError("dispatch_input_names must be non-empty")
        self._config = config
        self._workflow_gateway = workflow_gateway
        self._reader = reader
        self._publisher = publisher
        self._dispatch_input_names = names
        self._poll_attempts = poll_attempts
        self._poll_interval_seconds = poll_interval_seconds
        self._sleep = sleep

    # -- entry point --------------------------------------------------------

    def deploy(
        self,
        request: DeploymentLifecycleRequest,
    ) -> DeploymentOutcome:
        if not _COMMIT.fullmatch(request.merge_commit):
            raise OptimizationDeploymentError(
                "the deployment merge commit is not a full Git commit"
            )
        workflow = request.workflow
        if not workflow.exists:
            return self._publish_generated(request)

        try:
            run = self._observe_or_dispatch(request)
            if run is None:
                return self._no_run_outcome(workflow)
            run = self._await_terminal(request, run)
            if run.status in {
                WorkflowRunStatus.QUEUED,
                WorkflowRunStatus.IN_PROGRESS,
            }:
                return DeploymentOutcome(
                    status=DeploymentOutcomeStatus.PENDING,
                    run_url=run.url,
                    reason_code="workflow_pending",
                )
            if run.status is not WorkflowRunStatus.SUCCESS:
                return DeploymentOutcome(
                    status=DeploymentOutcomeStatus.FAILED,
                    run_url=run.url,
                    reason_code="workflow_failed",
                )
            return self._verify_published(request, run)
        except DeploymentLineageMismatchError:
            # Surfaced to the lifecycle, which maps it to a typed blocked
            # ``deployment_lineage_mismatch`` result.
            raise
        except DeploymentError as error:
            # Any other Azure/OIDC/Foundry failure fails closed as an
            # unavailable capability rather than a fabricated success.
            raise CapabilityUnavailableError(
                _deployment_error_code(error),
                "reading and verifying the published Foundry deployment "
                "failed against the live binding",
            ) from error

    # -- observation / dispatch --------------------------------------------

    def _observe_or_dispatch(
        self,
        request: DeploymentLifecycleRequest,
    ) -> DeploymentWorkflowRun | None:
        workflow = request.workflow
        query = self._run_query(
            workflow,
            request.merge_commit,
            self._correlation_id(request),
        )
        run = self._workflow_gateway.find_run(
            request.repository_root, query=query
        )
        if run is not None:
            # Idempotent: a run already exists for this workflow + commit, so
            # observe it instead of launching a duplicate deployment.
            return run
        if (
            workflow.trigger is DeploymentTrigger.MANUAL
            and request.dispatch
        ):
            self._dispatch(request)
            run = self._locate_after_dispatch(request, query)
        return run

    def _dispatch(self, request: DeploymentLifecycleRequest) -> None:
        input_name = self._dispatch_input_name(request)
        if (
            _CORRELATION_INPUT_NAME
            not in self._declared_dispatch_inputs(request)
            or self._declared_run_name(request)
            != "${{ inputs.foundry_opt_effect_id }}"
        ):
            raise OptimizationDeploymentError(
                "the manual deployment workflow does not declare the "
                "exact foundry-opt correlation contract"
            )
        correlation_id = self._correlation_id(request)
        self._workflow_gateway.dispatch(
            request.repository_root,
            workflow_path=request.workflow.path,
            input_name=input_name,
            commit=request.merge_commit,
            correlation_input_name=_CORRELATION_INPUT_NAME,
            correlation_id=correlation_id,
        )

    def _dispatch_input_name(
        self,
        request: DeploymentLifecycleRequest,
    ) -> str:
        declared = self._declared_dispatch_inputs(request)
        for candidate in self._dispatch_input_names:
            if candidate in declared:
                return candidate
        raise OptimizationDeploymentError(
            "the manual deployment workflow does not declare an exact-commit "
            "workflow_dispatch input; refusing to dispatch without forwarding "
            "the exact merge commit"
        )

    def _declared_dispatch_inputs(
        self,
        request: DeploymentLifecycleRequest,
    ) -> frozenset[str]:
        path = request.repository_root / request.workflow.path
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeError, yaml.YAMLError) as error:
            raise OptimizationDeploymentError(
                "the deployment workflow file could not be read to resolve "
                "its dispatch inputs"
            ) from error
        if not isinstance(document, dict):
            raise OptimizationDeploymentError(
                "the deployment workflow file is malformed"
            )
        triggers = document.get("on", document.get(True))
        if not isinstance(triggers, dict):
            return frozenset()
        dispatch = triggers.get("workflow_dispatch")
        if not isinstance(dispatch, dict):
            return frozenset()
        inputs = dispatch.get("inputs")
        if not isinstance(inputs, dict):
            return frozenset()
        return frozenset(
            str(name) for name in inputs if isinstance(name, str)
        )

    def _declared_run_name(
        self,
        request: DeploymentLifecycleRequest,
    ) -> str | None:
        path = request.repository_root / request.workflow.path
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeError, yaml.YAMLError) as error:
            raise OptimizationDeploymentError(
                "the deployment workflow file could not be read"
            ) from error
        if not isinstance(document, dict):
            raise OptimizationDeploymentError(
                "the deployment workflow file is malformed"
            )
        value = document.get("run-name")
        return value.strip() if isinstance(value, str) else None

    def _locate_after_dispatch(
        self,
        request: DeploymentLifecycleRequest,
        query: WorkflowRunQuery,
    ) -> DeploymentWorkflowRun | None:
        for attempt in range(self._poll_attempts):
            run = self._workflow_gateway.find_run(
                request.repository_root, query=query
            )
            if run is not None:
                return run
            if attempt + 1 < self._poll_attempts:
                self._sleep(self._poll_interval_seconds)
        return None

    def _await_terminal(
        self,
        request: DeploymentLifecycleRequest,
        run: DeploymentWorkflowRun,
    ) -> DeploymentWorkflowRun:
        query = self._run_query(
            request.workflow,
            request.merge_commit,
            self._correlation_id(request),
        )
        for attempt in range(self._poll_attempts):
            if run.status not in {
                WorkflowRunStatus.QUEUED,
                WorkflowRunStatus.IN_PROGRESS,
            }:
                return run
            if attempt + 1 >= self._poll_attempts:
                break
            self._sleep(self._poll_interval_seconds)
            located = self._workflow_gateway.find_run(
                request.repository_root, query=query
            )
            if located is not None:
                run = located
        return run

    def _run_query(
        self,
        workflow: DeploymentWorkflow,
        commit: str,
        correlation_id: str | None = None,
    ) -> WorkflowRunQuery:
        if workflow.trigger is DeploymentTrigger.MANUAL:
            events = ("workflow_dispatch",)
        else:
            events = ("push", "workflow_run")
        return WorkflowRunQuery(
            workflow_path=workflow.path,
            events=events,
            head_sha=commit,
            trigger=workflow.trigger,
            match_head_sha=(
                workflow.trigger is not DeploymentTrigger.MANUAL
            ),
            display_title=(
                correlation_id
                if workflow.trigger is DeploymentTrigger.MANUAL
                else None
            ),
        )

    def _correlation_id(
        self,
        request: DeploymentLifecycleRequest,
    ) -> str:
        return (
            "legacy-deployment-"
            f"{optimization_deployment_lineage_sha256(request.lineage)[:20]}"
        )

    def _no_run_outcome(
        self,
        workflow: DeploymentWorkflow,
    ) -> DeploymentOutcome:
        if workflow.trigger is DeploymentTrigger.MANUAL:
            return DeploymentOutcome(
                status=DeploymentOutcomeStatus.MANUAL_TRIGGER_REQUIRED,
                reason_code="manual_trigger_required",
            )
        return DeploymentOutcome(
            status=DeploymentOutcomeStatus.PENDING,
            reason_code="merge_deployment_pending",
        )

    # -- published-version verification ------------------------------------

    def _verify_published(
        self,
        request: DeploymentLifecycleRequest,
        run: DeploymentWorkflowRun,
    ) -> DeploymentOutcome:
        published = self._reader.read_latest(
            project_endpoint=request.project_endpoint,
            agent_name=self._agent_name(request),
            expected_lineage=request.lineage,
        )
        if published is None:
            # The workflow reported success but no published version is
            # readable; fail closed as a mismatch rather than assume success.
            return DeploymentOutcome(
                status=DeploymentOutcomeStatus.MISMATCH,
                run_url=run.url,
                reason_code="published_version_missing",
            )
        # Exact lineage identity (issue/spec/campaign/candidate/commit). Raises
        # DeploymentLineageMismatchError, which propagates to the lifecycle.
        verify_optimization_deployment_lineage(
            published.record.lineage, request.lineage
        )
        if not self._provenance_matches(request, run, published):
            return DeploymentOutcome(
                status=DeploymentOutcomeStatus.MISMATCH,
                version=published.runtime.deployed_version,
                run_url=run.url,
                portal_url=published.runtime.portal_url,
                reason_code="provenance_mismatch",
            )
        return DeploymentOutcome(
            status=DeploymentOutcomeStatus.VERIFIED,
            version=published.runtime.deployed_version,
            run_url=run.url,
            portal_url=published.runtime.portal_url,
            reason_code="verified",
            source_sha256=published.runtime.source_sha256,
            tree_sha=published.record.tree_hash,
            bundle_sha256=published.record.sha256,
            merge_commit=request.merge_commit,
            lineage_sha256=optimization_deployment_lineage_sha256(
                request.lineage
            ),
            metadata_sha256=hashlib.sha256(
                json.dumps(
                    dict(published.record.metadata),
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
        )

    def _provenance_matches(
        self,
        request: DeploymentLifecycleRequest,
        run: DeploymentWorkflowRun,
        published: PublishedDeployment,
    ) -> bool:
        lineage = request.lineage
        record = published.record
        runtime = published.runtime
        expected_digest = optimization_deployment_lineage_sha256(lineage)
        return (
            # The run that published is bound to the exact merge commit.
            run.head_commit == request.merge_commit
            == lineage.selected_merge_commit
            and run.path == request.workflow.path
            and run.trigger is request.workflow.trigger
            # The published version's numeric identity is the latest.
            and record.version > record.base_version
            and runtime.deployed_version == record.version
            and runtime.latest_version == record.version
            and runtime.agent_name == record.agent_name
            == self._agent_name(request)
            # Exact source bundle and tree, tied to the lineage.
            and record.tree_hash == lineage.selected_tree_sha
            and record.patch_sha256 == lineage.patch_sha256
            and record.evidence_sha256 == lineage.evidence_sha256
            and record.sha256 == runtime.source_sha256
            # The one-way lineage digest recorded as deployment provenance.
            and record.metadata.get(_LINEAGE_PROVENANCE_KEY)
            == expected_digest
            # The published record targets the requested project/service.
            and record.project_endpoint == request.project_endpoint
            and record.status in _ACTIVE_STATUSES
            and runtime.portal_url == record.portal_url
        )

    def _agent_name(self, request: DeploymentLifecycleRequest) -> str:
        # Onboarding binds each target to the identically-named hosted agent.
        target = request.spec.target
        if target not in self._config.targets:
            raise OptimizationDeploymentError(
                "the specification target is not configured; cannot resolve "
                "the hosted agent name"
            )
        return target

    # -- generated (product-published) manual workflow ---------------------

    def _publish_generated(
        self,
        request: DeploymentLifecycleRequest,
    ) -> DeploymentOutcome:
        if self._publisher is None:
            raise CapabilityUnavailableError(
                "deployment_workflow_missing",
                "the repository has no committed deployment workflow to "
                "observe; publishing a generated manual workflow through the "
                "DeploymentGateway requires the live publisher binding, which "
                "is not wired in this build",
            )
        return self._publisher.publish(request)


def _deployment_error_code(error: DeploymentError) -> str:
    name = type(error).__name__
    trimmed = name[: -len("Error")] if name.endswith("Error") else name
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", trimmed).lower()
    return snake or "deployment"


# ---------------------------------------------------------------------------
# Production ``gh`` workflow-run gateway
# ---------------------------------------------------------------------------


class GhWorkflowRunGateway:
    """``gh``/``git`` implementation of :class:`WorkflowRunGateway`.

    ``find_run`` lists the runs of the *exact* deployment workflow file and
    returns only a run whose event is accepted. Merge-trigger runs also require
    the exact merge SHA. Manual callers must bind the selected run through the
    canonical effect-ID correlation rather than treating ``headSha`` as the
    workflow input.
    ``dispatch`` launches the workflow against the repository default branch,
    forwarding the exact merge commit as an explicit ``workflow_dispatch``
    input. It performs no name-based process operations.
    """

    def __init__(
        self,
        command_runner: CommandRunner,
        *,
        run_limit: int = 50,
        dispatch_environment: Mapping[str, str] | None = None,
    ) -> None:
        if not 1 <= run_limit <= 200:
            raise ValueError("run_limit must be between 1 and 200")
        self._commands = command_runner
        self._run_limit = run_limit
        self._dispatch_environment = (
            dict(dispatch_environment)
            if dispatch_environment is not None
            else None
        )
        self._repository: dict[Path, str] = {}
        self._default_branch: dict[Path, str] = {}

    def find_run(
        self,
        repository_root: Path,
        *,
        query: WorkflowRunQuery,
    ) -> DeploymentWorkflowRun | None:
        if not _COMMIT.fullmatch(query.head_sha):
            raise OptimizationDeploymentError("query head SHA is invalid")
        raw = self._run(
            "find_run",
            (
                "gh",
                "run",
                "list",
                "--repo",
                self._repository_name(repository_root),
                "--workflow",
                query.workflow_path.name,
                "--limit",
                str(self._run_limit),
                "--json",
                (
                    "databaseId,displayTitle,headSha,status,"
                    "conclusion,event,path,url"
                ),
            ),
            cwd=repository_root,
        )
        workflow_posix = query.workflow_path.as_posix()
        matches = [
            item
            for item in _json_list(raw, "find_run")
            if _run_matches(item, query, workflow_posix)
        ]
        if not matches:
            return None
        # Deterministic: among the runs bound to this exact workflow + event +
        # commit, pick the most recent (highest run id), e.g. a re-run.
        best = max(matches, key=lambda item: _run_id(item))
        return _workflow_run_from_json(best, query)

    def dispatch(
        self,
        repository_root: Path,
        *,
        workflow_path: Path,
        input_name: str,
        commit: str,
        correlation_input_name: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        if not _COMMIT.fullmatch(commit):
            raise OptimizationDeploymentError("dispatch commit is invalid")
        if not _IDENTIFIER_INPUT.fullmatch(input_name):
            raise OptimizationDeploymentError("dispatch input name is invalid")
        if (correlation_input_name is None) != (correlation_id is None):
            raise OptimizationDeploymentError(
                "dispatch correlation input is incomplete"
            )
        if (
            correlation_input_name is not None
            and (
                not _IDENTIFIER_INPUT.fullmatch(correlation_input_name)
                or not isinstance(correlation_id, str)
                or not correlation_id
            )
        ):
            raise OptimizationDeploymentError(
                "dispatch correlation input is invalid"
            )
        arguments = [
            "gh",
            "workflow",
            "run",
            workflow_path.as_posix(),
            "--repo",
            self._repository_name(repository_root),
            "--ref",
            self._default_branch_name(repository_root),
            "--field",
            f"{input_name}={commit}",
        ]
        if correlation_input_name is not None:
            arguments.extend(
                (
                    "--field",
                    f"{correlation_input_name}={correlation_id}",
                )
            )
        self._run(
            "dispatch",
            tuple(arguments),
            cwd=repository_root,
        )

    # -- internals ----------------------------------------------------------

    def _default_branch_name(self, repository_root: Path) -> str:
        cached = self._default_branch.get(repository_root)
        if cached is not None:
            return cached
        branch = self._run(
            "default_branch",
            (
                "gh",
                "api",
                f"repos/{self._repository_name(repository_root)}",
                "--jq",
                ".default_branch",
            ),
            cwd=repository_root,
        ).strip()
        if not branch:
            raise OptimizationDeploymentError(
                "the repository default branch could not be resolved"
            )
        self._default_branch[repository_root] = branch
        return branch

    def _repository_name(self, repository_root: Path) -> str:
        cached = self._repository.get(repository_root)
        if cached is not None:
            return cached
        remote = self._run(
            "origin",
            ("git", "remote", "get-url", "origin"),
            cwd=repository_root,
        ).strip()
        repository = github_repository_from_remote_url(remote)
        if repository is None:
            raise OptimizationDeploymentError(
                "the origin remote is not a supported GitHub repository"
            )
        self._repository[repository_root] = repository
        return repository

    def _run(
        self,
        operation: str,
        arguments: Sequence[str],
        *,
        cwd: Path,
    ) -> str:
        try:
            return self._commands.run(
                arguments,
                cwd=cwd,
                environment=(
                    self._dispatch_environment
                    if operation == "dispatch"
                    else None
                ),
            ).stdout
        except CommandError as error:
            raise OptimizationDeploymentError(
                f"GitHub workflow operation failed: {operation}"
            ) from error


_IDENTIFIER_INPUT = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")


def _run_matches(
    item: Mapping[str, Any],
    query: WorkflowRunQuery,
    workflow_posix: str,
) -> bool:
    head = item.get("headSha")
    event = item.get("event")
    path = item.get("path")
    return (
        isinstance(head, str)
        and (not query.match_head_sha or head == query.head_sha)
        and (
            query.display_title is None
            or item.get("displayTitle") == query.display_title
        )
        and isinstance(event, str)
        and event in query.events
        and isinstance(path, str)
        and path == workflow_posix
    )


def _run_id(item: Mapping[str, Any]) -> int:
    value = item.get("databaseId")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return -1


def _workflow_run_from_json(
    item: Mapping[str, Any],
    query: WorkflowRunQuery,
) -> DeploymentWorkflowRun:
    url = item.get("url")
    if not isinstance(url, str) or not url:
        raise OptimizationDeploymentError("workflow run URL is missing")
    try:
        return DeploymentWorkflowRun(
            path=query.workflow_path,
            trigger=query.trigger,
            status=_workflow_run_status(item),
            head_commit=query.head_sha,
            url=url,
        )
    except ValueError as error:
        raise OptimizationDeploymentError(
            "the workflow run response could not be interpreted"
        ) from error


def _workflow_run_status(item: Mapping[str, Any]) -> WorkflowRunStatus:
    status = str(item.get("status", "")).casefold()
    conclusion = str(item.get("conclusion", "")).casefold()
    if status in _COMPLETED_STATUSES:
        if conclusion in _SUCCESS_CONCLUSIONS:
            return WorkflowRunStatus.SUCCESS
        if conclusion in _CANCELLED_CONCLUSIONS:
            return WorkflowRunStatus.CANCELLED
        return WorkflowRunStatus.FAILURE
    if status in _IN_PROGRESS_STATUSES:
        return WorkflowRunStatus.IN_PROGRESS
    # Any queued/waiting/unknown non-terminal status keeps the run pending so
    # the coordinator polls rather than concluding prematurely.
    return WorkflowRunStatus.QUEUED


def _json_list(raw: str, operation: str) -> list[dict[str, Any]]:
    import json

    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise OptimizationDeploymentError(
            f"GitHub returned an invalid JSON payload: {operation}"
        ) from error
    if not isinstance(value, list) or not all(
        isinstance(item, dict) for item in value
    ):
        raise OptimizationDeploymentError(
            f"GitHub returned an unexpected payload: {operation}"
        )
    return value


# ---------------------------------------------------------------------------
# Production Foundry published-deployment reader
# ---------------------------------------------------------------------------


class FoundryPublishedDeploymentReader:
    """Reads the latest published version via Azure OIDC + the Foundry API.

    Reuses the dedicated deployment OIDC identity (the same principal the
    ``DeploymentGateway`` publishes with) and the Foundry agent versions API to
    read the latest *numeric* published version and its deployed runtime. It
    fails closed if the active principal is not the deployment identity or the
    recorded lineage digest does not match the expected lineage.
    """

    def __init__(
        self,
        credential_provider: Any,
        *,
        client_factory: Callable[[str, Any], Any] | None = None,
        deployment_client_id: str = DEPLOYMENT_OIDC_CLIENT_ID,
        max_pages: int = 50,
    ) -> None:
        if deployment_client_id != DEPLOYMENT_OIDC_CLIENT_ID:
            raise ValueError(
                "deployment_client_id must use the deployment OIDC app"
            )
        if not 1 <= max_pages <= 200:
            raise ValueError("max_pages must be between 1 and 200")
        self._credential_provider = credential_provider
        self._client_factory = client_factory
        self._deployment_client_id = deployment_client_id
        self._max_pages = max_pages

    def read_latest(
        self,
        *,
        project_endpoint: str,
        agent_name: str,
        expected_lineage: OptimizationDeploymentLineage,
    ) -> PublishedDeployment | None:
        from azure.core.rest import HttpRequest

        from foundry_opt.adapters.deployment import (
            _close_quietly,
            _create_client,
            _send_json,
            _verify_active_principal,
            _version_url,
            _versions_url,
        )

        _verify_active_principal(
            self._credential_provider,
            self._deployment_client_id,
        )
        factory = self._client_factory or _create_client
        credential = None
        client = None
        try:
            credential = self._credential_provider.create()
            client = factory(project_endpoint, credential)
            latest = self._latest_version(
                client,
                project_endpoint,
                agent_name,
                _send_json,
                _versions_url,
                HttpRequest,
            )
            if latest is None:
                return None
            payload = _send_json(
                client,
                HttpRequest(
                    "GET",
                    _version_url(project_endpoint, agent_name, str(latest)),
                    headers={"Accept": "application/json"},
                ),
            )
            return _parse_published_version(
                payload,
                project_endpoint=project_endpoint,
                agent_name=agent_name,
                latest_version=latest,
                expected_lineage=expected_lineage,
            )
        finally:
            _close_quietly(client)
            _close_quietly(credential)

    def _latest_version(
        self,
        client: Any,
        project_endpoint: str,
        agent_name: str,
        send_json: Callable[[Any, Any], dict[str, Any]],
        versions_url: Callable[[str, str], str],
        http_request: Any,
    ) -> int | None:
        url: str | None = versions_url(project_endpoint, agent_name)
        latest: int | None = None
        pages = 0
        while url is not None and pages < self._max_pages:
            pages += 1
            payload = send_json(
                client,
                http_request(
                    "GET",
                    url,
                    headers={"Accept": "application/json"},
                ),
            )
            for version in _published_versions(payload):
                if latest is None or version > latest:
                    latest = version
            url = _next_link(payload)
        return latest


def _published_versions(payload: Mapping[str, Any]) -> list[int]:
    items = payload.get("value")
    if items is None:
        items = payload.get("data")
    if not isinstance(items, list):
        raise DeploymentResponseError()
    versions: list[int] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("draft") is True:
            continue
        version = _version_number(item.get("version"))
        if version is not None:
            versions.append(version)
    return versions


def _next_link(payload: Mapping[str, Any]) -> str | None:
    for key in ("nextLink", "next_link", "next"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _version_number(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 1 else None
    if isinstance(value, str) and value.isdigit():
        number = int(value)
        return number if number >= 1 else None
    return None


def _parse_published_version(
    payload: Mapping[str, Any],
    *,
    project_endpoint: str,
    agent_name: str,
    latest_version: int,
    expected_lineage: OptimizationDeploymentLineage,
) -> PublishedDeployment:
    from foundry_opt.adapters.deployment import _safe_portal_url

    version = _version_number(payload.get("version"))
    if version != latest_version or payload.get("draft") is not False:
        raise DeploymentResponseError()
    status = payload.get("status")
    if not isinstance(status, str) or not status:
        raise DeploymentResponseError()
    definition = payload.get("definition")
    if not isinstance(definition, dict) or definition.get("kind") != "hosted":
        raise DeploymentResponseError()
    configuration = definition.get("code_configuration")
    metadata = payload.get("metadata")
    if not isinstance(configuration, dict) or not isinstance(metadata, dict):
        raise DeploymentResponseError()
    runtime = configuration.get("runtime")
    entry_point = configuration.get("entry_point")
    dependency_resolution = configuration.get("dependency_resolution")
    source_sha256 = configuration.get("content_hash")
    if (
        not isinstance(runtime, str)
        or not runtime
        or not isinstance(entry_point, list)
        or not entry_point
        or any(not isinstance(part, str) or not part for part in entry_point)
        or not isinstance(dependency_resolution, str)
        or not isinstance(source_sha256, str)
        or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in metadata.items()
        )
    ):
        raise DeploymentResponseError()

    expected_digest = optimization_deployment_lineage_sha256(expected_lineage)
    if metadata.get(_LINEAGE_PROVENANCE_KEY) != expected_digest:
        raise DeploymentLineageMismatchError()

    portal_url = _safe_portal_url(
        payload.get("portal_url")
        or payload.get("portalUrl")
        or _nested_portal_url(payload)
    )
    if portal_url is None:
        raise DeploymentResponseError()

    try:
        record = DeploymentRecord(
            project_endpoint=project_endpoint,
            agent_name=agent_name,
            version=version,
            base_version=int(metadata["foundry-opt-base-version"]),
            baseline_source_sha256=metadata[
                "foundry-opt-baseline-source-sha256"
            ],
            sha256=source_sha256,
            patch_sha256=metadata["foundry-opt-patch-sha256"],
            tree_hash=metadata["foundry-opt-tree-hash"],
            evidence_sha256=metadata["foundry-opt-evidence-sha256"],
            lineage=expected_lineage,
            status=status,
            portal_url=portal_url,
            runtime=runtime,
            entry_point=tuple(entry_point),
            dependency_resolution=dependency_resolution,
            metadata={
                key: value for key, value in metadata.items()
            },
        )
        deployed_runtime = DeployedRuntime(
            agent_name=agent_name,
            deployed_version=version,
            latest_version=latest_version,
            source_sha256=source_sha256,
            portal_url=portal_url,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise DeploymentResponseError() from error
    return PublishedDeployment(record=record, runtime=deployed_runtime)


def _nested_portal_url(payload: Mapping[str, Any]) -> object:
    definition = payload.get("definition")
    if isinstance(definition, dict):
        for key in ("portal_url", "portalUrl"):
            value = definition.get(key)
            if isinstance(value, str):
                return value
    return None


# ---------------------------------------------------------------------------
# Production factory
# ---------------------------------------------------------------------------


def build_live_deployment_coordinator(
    config: OptimizerConfig,
    *,
    command_runner: CommandRunner,
    credential_provider: Any,
    publisher: GeneratedDeploymentPublisher | None = None,
    poll_attempts: int = 30,
    poll_interval_seconds: float = 2.0,
) -> LiveDeploymentCoordinator:
    """Assemble the production live deployment coordinator.

    The workflow-run gateway is the authenticated ``gh`` implementation and the
    published-version reader authenticates with the dedicated deployment OIDC
    identity. Wiring this coordinator onto the lifecycle services (via
    :func:`foundry_opt.optimization.lifecycle.build_lifecycle_services`) is a
    separate step owned by the live-wiring assembly.
    """

    return LiveDeploymentCoordinator(
        config,
        GhWorkflowRunGateway(command_runner),
        FoundryPublishedDeploymentReader(credential_provider),
        publisher=publisher,
        poll_attempts=poll_attempts,
        poll_interval_seconds=poll_interval_seconds,
    )
