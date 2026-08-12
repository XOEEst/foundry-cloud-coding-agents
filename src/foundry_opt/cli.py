from pathlib import Path
import json
import os
from typing import Annotated

import typer

from foundry_opt import __version__
from foundry_opt.auth import (
    AUTH_PROBE_SCOPE,
    AuthProbeRequest,
    AuthProbeResult,
)
from foundry_opt.config import OptimizerConfig, load_config
from foundry_opt.config.loader import ConfigLoadError
from foundry_opt.onboarding import (
    OnboardingDependencies,
    OnboardingRequest,
    OnboardingResult,
    run_onboarding,
)
from foundry_opt.optimization import (
    OptimizeCommandRequest,
    OptimizationCommandService,
    OptimizePhase,
)
from foundry_opt.preflight.models import PreflightRequest
from foundry_opt.preflight.redaction import redact
from foundry_opt.preflight.rendering import render_human, render_json
from foundry_opt.preflight.runner import PreflightRunner
from foundry_opt.orchestration.steward import (
    StewardAdvanceRequest,
    StewardAdvanceService,
)


app = typer.Typer(
    help="Optimize Microsoft Foundry coding agents with reviewable evidence.",
    no_args_is_help=True,
)
optimize_app = typer.Typer(
    help="Run an issue-driven Foundry optimization workflow.",
    invoke_without_command=True,
    no_args_is_help=False,
)
app.add_typer(optimize_app, name="optimize")
candidate_app = typer.Typer(
    help="Drive the filesystem candidate handoff for a running campaign.",
    no_args_is_help=True,
)
optimize_app.add_typer(candidate_app, name="candidate")
steward_app = typer.Typer(
    help="Advance the durable Copilot steward campaign.",
    no_args_is_help=True,
)
app.add_typer(steward_app, name="steward")
auth_app = typer.Typer(
    help="Inspect product-side Azure OIDC readiness without exposing credentials.",
    no_args_is_help=True,
)
app.add_typer(auth_app, name="auth")
workspace_app = typer.Typer(
    help="Advance the single durable optimization workspace.",
    no_args_is_help=True,
)
app.add_typer(workspace_app, name="workspace")
workspace_migration_app = typer.Typer(
    help="Inventory, convert, and safely archive legacy workspace refs.",
    no_args_is_help=True,
)
workspace_app.add_typer(workspace_migration_app, name="migration")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"foundry-opt {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the installed version and exit.",
        ),
    ] = False,
) -> None:
    """Optimize Microsoft Foundry coding agents with reviewable evidence."""


def build_preflight_runner(
    config: OptimizerConfig,
    request: PreflightRequest,
) -> PreflightRunner:
    """Return the preflight runner assembled for a validated request."""
    from foundry_opt.preflight.production import build_production_preflight_runner

    return build_production_preflight_runner(config, request)


def build_onboarding_dependencies() -> OnboardingDependencies:
    """Return production adapters for local onboarding."""
    from foundry_opt.onboarding.production import (
        build_production_onboarding_dependencies,
    )

    return build_production_onboarding_dependencies()


def build_optimization_command_service() -> OptimizationCommandService:
    """Return the issue-driven optimization command service."""
    from foundry_opt.optimization.production import (
        build_optimization_command_service as build_service,
    )

    return build_service()


def build_steward_advance_service() -> StewardAdvanceService:
    """Return the durable Copilot steward advance service."""
    from foundry_opt.optimization.production import (
        build_production_steward_advance_service,
    )

    return build_production_steward_advance_service()


def build_candidate_design_submission_service():
    """Return the durable candidate designer result service."""
    from foundry_opt.optimization.production import (
        build_production_candidate_design_submission_service,
    )

    return build_production_candidate_design_submission_service()


def build_auth_probe():
    """Return the deterministic product-side OIDC probe."""
    from foundry_opt.auth import build_production_auth_probe

    return build_production_auth_probe()


def build_workspace_service():
    """Return the production single-workspace service."""
    from foundry_opt.orchestration.workspace_production import (
        build_production_workspace_service,
    )

    return build_production_workspace_service()


def build_workspace_migration_service():
    """Return the production legacy workspace migration service."""
    from foundry_opt.orchestration.workspace_migration import (
        build_production_workspace_migration_service,
    )

    return build_production_workspace_migration_service(Path.cwd())


def _workspace_failure(error: Exception) -> None:
    typer.echo(
        f"Workspace error: {redact(str(error))}",
        err=True,
    )
    raise typer.Exit(1)


def _workspace_migration_output(result) -> None:
    typer.echo(
        json.dumps(
            result.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _workspace_migration_failure(error: Exception) -> None:
    typer.echo(
        json.dumps(
            {
                "error": redact(str(error)),
                "status": "refused",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    raise typer.Exit(1)


@workspace_migration_app.command("inventory")
def workspace_migration_inventory() -> None:
    """Inventory legacy v3 state and inbox refs without exposing contents."""
    try:
        result = build_workspace_migration_service().inventory()
    except (RuntimeError, ValueError, OSError) as error:
        _workspace_migration_failure(error)
    _workspace_migration_output(result)


@workspace_migration_app.command("convert")
def workspace_migration_convert(
    issue_number: Annotated[
        int,
        typer.Option("--issue", min=1, help="Optimization issue number."),
    ],
    expected_source_revision: Annotated[
        str,
        typer.Option(
            "--expected-source-revision",
            help="Exact v3 state revision returned by inventory.",
        ),
    ],
) -> None:
    """Convert one verified closed v3 state ref to its compact v4 audit ref."""
    try:
        result = build_workspace_migration_service().convert(
            issue_number,
            expected_source_revision=expected_source_revision,
        )
    except (RuntimeError, ValueError, OSError) as error:
        _workspace_migration_failure(error)
    _workspace_migration_output(result)


@workspace_migration_app.command("archive")
def workspace_migration_archive(
    issue_number: Annotated[
        int,
        typer.Option("--issue", min=1, help="Optimization issue number."),
    ],
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Delete only refs matching all exact planned revisions.",
        ),
    ] = False,
    expected_state_revision: Annotated[
        str | None,
        typer.Option(
            "--expected-state-revision",
            help="Planned state SHA, or 'absent'. Required with --apply.",
        ),
    ] = None,
    expected_inbox_revision: Annotated[
        str | None,
        typer.Option(
            "--expected-inbox-revision",
            help="Planned inbox SHA, or 'absent'. Required with --apply.",
        ),
    ] = None,
    expected_audit_revision: Annotated[
        str | None,
        typer.Option(
            "--expected-audit-revision",
            help="Planned audit SHA, or 'absent'. Required with --apply.",
        ),
    ] = None,
) -> None:
    """Produce a dry-run archive plan, or apply its exact revisions."""
    try:
        if not apply:
            if any(
                value is not None
                for value in (
                    expected_state_revision,
                    expected_inbox_revision,
                    expected_audit_revision,
                )
            ):
                raise ValueError(
                    "expected revisions require --apply"
                )
            result = build_workspace_migration_service().plan_archive(
                issue_number
            )
        else:
            missing = [
                option
                for option, value in (
                    (
                        "--expected-state-revision",
                        expected_state_revision,
                    ),
                    (
                        "--expected-inbox-revision",
                        expected_inbox_revision,
                    ),
                    (
                        "--expected-audit-revision",
                        expected_audit_revision,
                    ),
                )
                if value is None
            ]
            if missing:
                raise typer.BadParameter(
                    f"{', '.join(missing)} required with --apply"
                )
            result = build_workspace_migration_service().apply_archive(
                issue_number,
                expected_revisions={
                    "audit": _migration_revision(
                        expected_audit_revision
                    ),
                    "inbox": _migration_revision(
                        expected_inbox_revision
                    ),
                    "state": _migration_revision(
                        expected_state_revision
                    ),
                },
            )
    except typer.BadParameter:
        raise
    except (RuntimeError, ValueError, OSError) as error:
        _workspace_migration_failure(error)
    _workspace_migration_output(result)


def _migration_revision(value: str | None) -> str | None:
    if value == "absent":
        return None
    return value


@workspace_app.command("advance")
def workspace_advance(
    issue_number: Annotated[
        int,
        typer.Option("--issue", min=1, help="Optimization issue number."),
    ],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a stable JSON result."),
    ] = False,
) -> None:
    """Load and advance the issue's single production workspace."""
    from foundry_opt.orchestration.workspace_production import (
        WorkspaceAdvanceRequest,
    )
    try:
        result = build_workspace_service().advance(
            WorkspaceAdvanceRequest(
                repository_root=Path.cwd(),
                issue_number=issue_number,
            )
        )
    except (RuntimeError, ValueError, OSError) as error:
        _workspace_failure(error)
    if json_output:
        typer.echo(
            json.dumps(
                result.to_dict(),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        pull_request = result.workspace_pull_request
        next_action = (
            result.next_action.kind.value
            if result.next_action is not None
            else "none"
        )
        typer.echo(
            f"Workspace {result.phase.value}: "
            f"PR #{pull_request.number if pull_request else 'unknown'}; "
            f"next action {next_action}"
        )


@workspace_app.command("intake")
def workspace_intake(
    event_path: Annotated[
        Path,
        typer.Option(
            "--event-path",
            exists=True,
            dir_okay=False,
            help="Trusted GitHub event JSON path.",
        ),
    ],
    event_name: Annotated[
        str,
        typer.Option("--event-name", help="Trusted GitHub event name."),
    ],
    delivery_id: Annotated[
        str,
        typer.Option("--delivery-id", help="Trusted delivery identifier."),
    ],
    repository: Annotated[
        str,
        typer.Option("--repository", help="Trusted owner/repository."),
    ],
    repository_id: Annotated[
        int,
        typer.Option("--repository-id", min=1, help="Trusted repository ID."),
    ],
    base_commit: Annotated[
        str | None,
        typer.Option(
            "--base-commit",
            help="Trusted default-branch commit for an issue event.",
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a stable JSON result."),
    ] = False,
) -> None:
    """Normalize one trusted issue or workspace PR event and advance."""
    from foundry_opt.orchestration.workspace_intake import (
        TrustedWorkspaceEventContext,
    )

    try:
        if event_path.stat().st_size > 2_000_000:
            raise ValueError("workspace event payload is too large")
        payload = json.loads(event_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("workspace event payload must be an object")
        result = build_workspace_service().ingest(
            payload,
            TrustedWorkspaceEventContext(
                event_name=event_name,
                delivery_id=delivery_id,
                repository=repository,
                repository_id=repository_id,
            ),
            base_commit=base_commit,
            repository_root=Path.cwd(),
        )
    except (
        json.JSONDecodeError,
        RuntimeError,
        ValueError,
        OSError,
    ) as error:
        _workspace_failure(error)
    if json_output:
        typer.echo(
            json.dumps(
                result.to_dict(),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        typer.echo(
            f"Workspace event {result.event.delivery_id}: "
            f"{result.workspace.phase.value}"
        )


@workspace_app.command("experiments-complete")
def workspace_experiments_complete(
    issue_number: Annotated[
        int,
        typer.Option("--issue", min=1, help="Optimization issue number."),
    ],
    manifest_path: Annotated[
        Path,
        typer.Option(
            "--manifest",
            exists=True,
            dir_okay=False,
            help="Privacy-safe prepared candidate manifest JSON.",
        ),
    ],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a stable JSON result."),
    ] = False,
) -> None:
    """Reconcile prepared experiments into the same workspace PR."""
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("workspace manifest must be a JSON object")
        if payload.get("issue_number") != issue_number:
            raise ValueError(
                "workspace manifest issue does not match --issue"
            )
        result = build_workspace_service().complete_experiments(
            payload,
            repository_root=Path.cwd(),
        )
    except (
        ConfigLoadError,
        json.JSONDecodeError,
        RuntimeError,
        ValueError,
        OSError,
    ) as error:
        _workspace_failure(error)
    if json_output:
        typer.echo(
            json.dumps(
                result.to_dict(),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        typer.echo(
            f"Workspace {result.phase.value}: experiments reconciled"
        )


@workspace_app.command("operation-complete")
def workspace_operation_complete(
    result_path: Annotated[
        Path,
        typer.Option(
            "--result",
            exists=True,
            dir_okay=False,
            help="Trusted deployment or retention result JSON.",
        ),
    ],
    delivery_id: Annotated[
        str,
        typer.Option("--delivery-id", help="Trusted delivery identifier."),
    ],
    repository: Annotated[
        str,
        typer.Option("--repository", help="Trusted owner/repository."),
    ],
    repository_id: Annotated[
        int,
        typer.Option("--repository-id", min=1, help="Trusted repository ID."),
    ],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a stable JSON result."),
    ] = False,
) -> None:
    """Ingest a trusted completed deployment or retention operation."""
    from foundry_opt.orchestration.workspace_operations import (
        TrustedWorkspaceOperationContext,
    )

    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(
                "workspace operation result must be a JSON object"
            )
        result = build_workspace_service().ingest_operation(
            payload,
            TrustedWorkspaceOperationContext(
                delivery_id=delivery_id,
                repository=repository,
                repository_id=repository_id,
            ),
            repository_root=Path.cwd(),
        )
    except (
        json.JSONDecodeError,
        RuntimeError,
        ValueError,
        OSError,
    ) as error:
        _workspace_failure(error)
    if json_output:
        typer.echo(
            json.dumps(
                result.to_dict(),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        typer.echo(
            f"Workspace operation {result.event.operation.operation_id}: "
            f"{result.workspace.phase.value}"
        )


def _render_auth_probe(result: AuthProbeResult) -> str:
    tokens = ", ".join(
        f"{item.resource}={'pass' if item.success else 'fail'}"
        for item in result.token_acquisition
    )
    foundry = result.foundry_connectivity
    lines = [
        f"Environment: {result.environment_kind.value}",
        (
            "OIDC request variables present: "
            f"{str(result.oidc_request_variables.present).lower()}"
        ),
        (
            "Azure principal: "
            f"{result.azure_principal.principal_type}; "
            "configured client match="
            f"{str(result.azure_principal.client_match).lower()}"
        ),
        f"Token acquisition: {tokens}",
        (
            "Foundry read-only connectivity: "
            f"{str(foundry.read_only_access_success).lower()}"
        ),
        f"Refresh/reacquisition: {result.refresh_reacquisition.status}",
        (
            "Direct operations eligible: "
            f"{str(result.direct_operations_eligible).lower()}"
        ),
    ]
    if result.errors:
        lines.append("Errors:")
        lines.extend(
            f"- [{redact(error.code)}] {redact(error.message)}"
            for error in result.errors
        )
    return "\n".join(lines)


@auth_app.command("probe")
def auth_probe(
    scope: Annotated[
        str,
        typer.Option(
            "--scope",
            help="Product auth scope to inspect.",
        ),
    ] = ...,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit a stable JSON result.",
        ),
    ] = False,
) -> None:
    """Probe product-side Azure OIDC readiness without printing tokens.

    Exit status 1 means the probe is incomplete or not eligible.
    """
    if scope != AUTH_PROBE_SCOPE:
        typer.echo(
            "Authentication probe input error: unsupported scope.",
            err=True,
        )
        raise typer.Exit(2)
    result = build_auth_probe().run(
        AuthProbeRequest(
            repository_root=Path.cwd(),
            scope=scope,
        )
    )
    if json_output:
        typer.echo(
            json.dumps(
                result.to_dict(),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        typer.echo(_render_auth_probe(result))
    raise typer.Exit(result.exit_code)


def _render_onboarding(result: OnboardingResult) -> str:
    lines = [f"Onboarding {result.status.value}"]
    for change in result.changes:
        lines.append(
            f"- [{change.status.value}] {change.path.as_posix()}"
        )
    for blocker in result.blockers:
        lines.append(f"Blocked: {redact(blocker)}")
    for residual in result.residual_state:
        lines.append(f"Residual state: {redact(residual)}")
    for variable in result.variable_changes:
        location = variable.scope.value
        if variable.environment is not None:
            location += f"/{variable.environment}"
        lines.append(
            "GitHub variable: "
            f"[{variable.status.value}] {location}/{variable.name}"
        )
    for guidance in result.guidance:
        lines.append(f"Next: {guidance}")
    lines.append(f"Draft PR: {result.draft_pull_request.title}")
    lines.append(result.draft_pull_request.body)
    if result.published_pull_request is not None:
        lines.append(f"Draft PR URL: {result.published_pull_request.url}")
    return "\n".join(lines)


def _execute_optimize(
    *,
    issue_number: int,
    phase: OptimizePhase,
    candidate_id: str | None = None,
    idea_file: Path | None = None,
    verify_only: bool = False,
    json_output: bool = False,
) -> None:
    try:
        request = OptimizeCommandRequest(
            repository_root=Path.cwd(),
            issue_number=issue_number,
            phase=phase,
            candidate_id=candidate_id,
            idea_file=idea_file,
            verify_only=verify_only,
        )
    except ValueError as error:
        typer.echo(f"Optimization input error: {redact(str(error))}", err=True)
        raise typer.Exit(2) from None
    result = build_optimization_command_service().execute(request)
    if json_output:
        typer.echo(
            json.dumps(
                result.to_dict(),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        typer.echo(
            f"Optimization {result.status.value}: {redact(result.summary)}"
        )
        for key, value in result.details.items():
            typer.echo(f"- {key}: {redact(str(value))}")
        if result.next_action is not None:
            typer.echo(f"Next action: {redact(result.next_action)}")
    raise typer.Exit(result.exit_code)


@steward_app.command("advance")
def steward_advance(
    issue_number: Annotated[
        int,
        typer.Option("--issue", min=1, help="Optimization issue number."),
    ],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a stable JSON result."),
    ] = False,
) -> None:
    """Consume inbox events and advance one durable campaign."""
    result = build_steward_advance_service().advance(
        StewardAdvanceRequest(
            repository_root=Path.cwd(),
            issue_number=issue_number,
        )
    )
    if json_output:
        typer.echo(
            json.dumps(
                result.to_dict(),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        typer.echo(
            f"Steward {result.status.value}: {redact(result.summary)}"
        )
        if result.code is not None:
            typer.echo(f"- code: {result.code}")
        if result.phase is not None:
            typer.echo(f"- phase: {result.phase}")
        if result.disposition is not None:
            typer.echo(f"- disposition: {result.disposition}")
        if result.revision is not None:
            typer.echo(f"- revision: {result.revision}")
    raise typer.Exit(result.exit_code)


@steward_app.command("candidate-design-result", hidden=True)
def steward_candidate_design_result(
    issue_number: Annotated[
        int,
        typer.Option("--issue", min=1, help="Optimization issue number."),
    ],
    effect_id: Annotated[
        str,
        typer.Option("--effect", help="Persisted candidate design effect ID."),
    ],
    worker_issue_number: Annotated[
        int,
        typer.Option(
            "--worker-issue",
            min=1,
            help="Assigned candidate designer worker issue number.",
        ),
    ],
    result_file: Annotated[
        Path,
        typer.Option(
            "--result-file",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Typed CandidateDesignResult JSON file.",
        ),
    ],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a stable JSON result."),
    ] = False,
) -> None:
    """Persist one separately assigned candidate designer result."""
    from foundry_opt.orchestration.candidate_workers import (
        CandidateDesignSubmissionRequest,
        CandidateDesignSubmissionStatus,
    )

    result = build_candidate_design_submission_service().submit(
        CandidateDesignSubmissionRequest(
            repository_root=Path.cwd(),
            issue_number=issue_number,
            effect_id=effect_id,
            worker_issue_number=worker_issue_number,
            result_file=result_file,
        )
    )
    payload = {
        "code": result.code,
        "issue_number": issue_number,
        "revision": result.snapshot.revision,
        "status": result.status.value,
    }
    typer.echo(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    raise typer.Exit(
        0
        if result.status
        in {
            CandidateDesignSubmissionStatus.RECORDED,
            CandidateDesignSubmissionStatus.ALREADY_RECORDED,
            CandidateDesignSubmissionStatus.WAITING,
        }
        else 1
    )


@steward_app.command("deployment-bridge", hidden=True)
def steward_deployment_bridge(
    issue_number: Annotated[
        int,
        typer.Option("--issue", min=1, help="Optimization issue number."),
    ],
) -> None:
    """Apply persisted deployment workflow effects from the thin bridge."""
    from foundry_opt.adapters.commands import SubprocessCommandRunner
    from foundry_opt.orchestration.deployment import DeploymentBridgeStatus
    from foundry_opt.orchestration.deployment import (
        DeploymentCleanupBridgeStatus,
    )
    from foundry_opt.orchestration.deployment_bridge import (
        deployment_bridge_issue_numbers,
        reconcile_deployment_workflow_effects,
        verify_active_deployment_identity,
    )
    from foundry_opt.orchestration.issue_intake import GitIssueEventInbox

    commands = SubprocessCommandRunner()
    deployment_bridge_issue_numbers(
        requested_issue=str(issue_number),
        state_ref=None,
        tracked=GitIssueEventInbox(Path.cwd()).issue_numbers(),
    )
    verify_active_deployment_identity(
        commands,
        Path.cwd(),
        os.environ,
    )
    result = reconcile_deployment_workflow_effects(
        Path.cwd(),
        issue_number,
        commands,
    )
    typer.echo(
        json.dumps(
            {
                "issue_number": issue_number,
                "statuses": [
                    item.status.value for item in result.results
                ],
                "cleanup_statuses": [
                    item.status.value for item in result.cleanup_results
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    if any(
        item.status is DeploymentBridgeStatus.INVALID
        for item in result.results
    ) or any(
        item.status is DeploymentCleanupBridgeStatus.INVALID
        for item in result.cleanup_results
    ):
        raise typer.Exit(1)


@steward_app.command("publication-result", hidden=True)
def steward_publication_result(
    issue_number: Annotated[
        int,
        typer.Option("--issue", min=1, help="Optimization issue number."),
    ],
    result_file: Annotated[
        Path,
        typer.Option(
            "--result-file",
            help="Repository-relative redacted deployment result JSON.",
        ),
    ],
) -> None:
    """Persist a deployment-identity bridge publication result."""
    from foundry_opt.orchestration.deployment_bridge import (
        deployment_bridge_issue_numbers,
        record_deployment_publication_file,
        verify_active_deployment_identity,
    )
    from foundry_opt.adapters.commands import SubprocessCommandRunner
    from foundry_opt.orchestration.issue_intake import GitIssueEventInbox

    deployment_bridge_issue_numbers(
        requested_issue=str(issue_number),
        state_ref=None,
        tracked=GitIssueEventInbox(Path.cwd()).issue_numbers(),
    )
    commands = SubprocessCommandRunner()
    verify_active_deployment_identity(
        commands,
        Path.cwd(),
        os.environ,
    )
    result = record_deployment_publication_file(
        Path.cwd(),
        issue_number,
        result_file,
        commands=commands,
    )
    typer.echo(
        json.dumps(
            {
                "deployment_version": result.deployment_version,
                "issue_number": issue_number,
                "status": result.status.value,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


@steward_app.command("publication-result-auto", hidden=True)
def steward_publication_result_auto(
    result_file: Annotated[
        Path,
        typer.Option(
            "--result-file",
            help="Downloaded deployment result JSON.",
        ),
    ],
    expected_run_id: Annotated[
        int,
        typer.Option(
            "--expected-run-id",
            min=1,
            help="Trusted workflow_run ID that produced the artifact.",
        ),
    ],
) -> None:
    """Match and persist one deployment publication result by effect ID."""
    from foundry_opt.adapters.commands import SubprocessCommandRunner
    from foundry_opt.orchestration.deployment_bridge import (
        record_deployment_publication_file_for_effect,
        verify_active_deployment_identity,
    )

    commands = SubprocessCommandRunner()
    verify_active_deployment_identity(
        commands,
        Path.cwd(),
        os.environ,
    )
    issue_number, result = (
        record_deployment_publication_file_for_effect(
            Path.cwd(),
            result_file,
            commands,
            expected_run_id=expected_run_id,
        )
    )
    typer.echo(
        json.dumps(
            {
                "deployment_version": result.deployment_version,
                "issue_number": issue_number,
                "status": result.status.value,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


@optimize_app.callback()
def optimize_command(
    context: typer.Context,
    issue_number: Annotated[
        int | None,
        typer.Option(
            "--issue",
            min=1,
            help="GitHub issue number defining the optimization job.",
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a stable JSON result."),
    ] = False,
) -> None:
    """Run the next valid optimization phase for an issue."""
    if context.invoked_subcommand is not None:
        return
    if issue_number is None:
        raise typer.BadParameter("--issue is required")
    _execute_optimize(
        issue_number=issue_number,
        phase=OptimizePhase.AUTO,
        json_output=json_output,
    )


@optimize_app.command("spec")
def optimize_spec(
    issue_number: Annotated[
        int,
        typer.Option("--issue", min=1, help="Optimization issue number."),
    ],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a stable JSON result."),
    ] = False,
) -> None:
    """Prepare or reconcile an optimization specification PR."""
    _execute_optimize(
        issue_number=issue_number,
        phase=OptimizePhase.SPEC,
        json_output=json_output,
    )


@optimize_app.command("run")
def optimize_run(
    issue_number: Annotated[
        int,
        typer.Option("--issue", min=1, help="Optimization issue number."),
    ],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a stable JSON result."),
    ] = False,
) -> None:
    """Run an approved optimization specification."""
    _execute_optimize(
        issue_number=issue_number,
        phase=OptimizePhase.RUN,
        json_output=json_output,
    )


@optimize_app.command("apply")
def optimize_apply(
    issue_number: Annotated[
        int,
        typer.Option("--issue", min=1, help="Candidate issue number."),
    ],
    candidate_id: Annotated[
        str,
        typer.Option(
            "--candidate",
            help="Exact evaluated candidate identifier.",
        ),
    ] = ...,
    verify_only: Annotated[
        bool,
        typer.Option(
            "--verify-only",
            help="Verify the existing candidate PR without publishing changes.",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a stable JSON result."),
    ] = False,
) -> None:
    """Apply one exact evaluated candidate patch."""
    _execute_optimize(
        issue_number=issue_number,
        phase=OptimizePhase.APPLY,
        candidate_id=candidate_id,
        verify_only=verify_only,
        json_output=json_output,
    )


@optimize_app.command("reconcile")
def optimize_reconcile(
    issue_number: Annotated[
        int,
        typer.Option("--issue", min=1, help="Optimization issue number."),
    ],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a stable JSON result."),
    ] = False,
) -> None:
    """Reconcile candidate decisions, deployment, and issue state."""
    _execute_optimize(
        issue_number=issue_number,
        phase=OptimizePhase.RECONCILE,
        json_output=json_output,
    )


@candidate_app.command("request")
def optimize_candidate_request(
    issue_number: Annotated[
        int,
        typer.Option("--issue", min=1, help="Optimization issue number."),
    ],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a stable JSON result."),
    ] = False,
) -> None:
    """Reserve the next candidate slot and prepare an isolated worktree."""
    _execute_optimize(
        issue_number=issue_number,
        phase=OptimizePhase.CANDIDATE_REQUEST,
        json_output=json_output,
    )


@candidate_app.command("submit")
def optimize_candidate_submit(
    issue_number: Annotated[
        int,
        typer.Option("--issue", min=1, help="Optimization issue number."),
    ],
    candidate_id: Annotated[
        str,
        typer.Option(
            "--candidate",
            help="Reserved candidate identifier awaiting an idea.",
        ),
    ] = ...,
    idea_file: Annotated[
        Path,
        typer.Option(
            "--idea-file",
            help="Path to the strict candidate idea JSON file.",
        ),
    ] = ...,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a stable JSON result."),
    ] = False,
) -> None:
    """Validate, evaluate, and package one agent-authored candidate idea."""
    _execute_optimize(
        issue_number=issue_number,
        phase=OptimizePhase.CANDIDATE_SUBMIT,
        candidate_id=candidate_id,
        idea_file=idea_file,
        json_output=json_output,
    )


@app.command()
def preflight(
    config: Annotated[
        Path,
        typer.Option(
            "--config",
            help="Configuration file.",
        ),
    ] = Path(".github/foundry-optimizer.yaml"),
    environment: Annotated[
        str | None,
        typer.Option(
            "--environment",
            help="Environment name (defaults to the configured default).",
        ),
    ] = None,
    target: Annotated[
        str,
        typer.Option(
            "--target",
            help="Configured agent target.",
        ),
    ] = ...,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit a stable JSON report.",
        ),
    ] = False,
) -> None:
    """Run read-only prerequisite checks."""
    try:
        loaded_config = load_config(config)
    except ConfigLoadError as error:
        typer.echo(f"Configuration error: {redact(str(error))}", err=True)
        raise typer.Exit(2) from None
    selected_environment = environment or loaded_config.default_environment
    if selected_environment not in loaded_config.environments:
        typer.echo(
            f"Configuration error: environment {selected_environment!r} "
            "is not configured.",
            err=True,
        )
        raise typer.Exit(2)
    if target not in loaded_config.targets:
        typer.echo(
            f"Configuration error: target {target!r} is not configured.",
            err=True,
        )
        raise typer.Exit(2)
    target_environment = loaded_config.targets[target].environment
    if target_environment != selected_environment:
        typer.echo(
            f"Configuration error: target {target!r} uses environment "
            f"{target_environment!r}, not {selected_environment!r}.",
            err=True,
        )
        raise typer.Exit(2)
    request = PreflightRequest(
        repository_root=Path.cwd(),
        config_path=config,
        environment=selected_environment,
        target=target,
    )
    runner = build_preflight_runner(loaded_config, request)
    report = runner.run(request)
    typer.echo(render_json(report) if json_output else render_human(report))
    raise typer.Exit(report.exit_code)


@app.command("init")
def init_command(
    environment: Annotated[
        str,
        typer.Option(
            "--environment",
            prompt=True,
            help="Name for the generated environment profile.",
        ),
    ] = "development",
    target: Annotated[
        str,
        typer.Option(
            "--target",
            prompt=True,
            help="Name for the generated agent target.",
        ),
    ] = "agent",
    project_endpoint: Annotated[
        str,
        typer.Option(
            "--project-endpoint",
            prompt=True,
            help="Microsoft Foundry project endpoint.",
        ),
    ] = ...,
    project_resource_id: Annotated[
        str,
        typer.Option(
            "--project-resource-id",
            prompt=True,
            help="Non-secret Foundry project resource ID.",
        ),
    ] = ...,
    tenant_id: Annotated[
        str,
        typer.Option(
            "--tenant-id",
            prompt=True,
            help="Non-secret Entra tenant ID.",
        ),
    ] = ...,
    client_id: Annotated[
        str,
        typer.Option(
            "--client-id",
            prompt=True,
            help="Non-secret OIDC application client ID.",
        ),
    ] = ...,
    subscription_id: Annotated[
        str,
        typer.Option(
            "--subscription-id",
            prompt=True,
            help="Non-secret Azure subscription ID.",
        ),
    ] = ...,
    product_install: Annotated[
        str,
        typer.Option(
            "--product-install",
            help="Exact version or 40-character Git commit install spec.",
        ),
    ] = f"foundry-cloud-coding-agent=={__version__}",
    set_github_variables: Annotated[
        bool,
        typer.Option(
            "--set-github-variables",
            help=(
                "Create repository-level GitHub Agents variables for Azure "
                "OIDC identifiers."
            ),
        ),
    ] = False,
    mirror_actions_environment: Annotated[
        str | None,
        typer.Option(
            "--mirror-actions-environment",
            help=(
                "Also create the identifiers in an existing Actions "
                "deployment environment."
            ),
        ),
    ] = None,
    update_github_variables: Annotated[
        bool,
        typer.Option(
            "--update-github-variables",
            help=(
                "Replace differing GitHub variable values. Requires "
                "--set-github-variables."
            ),
        ),
    ] = False,
) -> None:
    """Discover and draft OIDC-based repository onboarding."""
    try:
        request = OnboardingRequest(
            repository_root=Path.cwd(),
            environment_name=environment,
            target_name=target,
            project_endpoint=project_endpoint,
            project_resource_id=project_resource_id,
            tenant_id=tenant_id,
            client_id=client_id,
            subscription_id=subscription_id,
            product_install=product_install,
            set_github_variables=set_github_variables,
            mirror_actions_environment=mirror_actions_environment,
            update_github_variables=update_github_variables,
        )
    except ValueError as error:
        typer.echo(f"Onboarding input error: {redact(str(error))}", err=True)
        raise typer.Exit(2) from None
    result = run_onboarding(request, build_onboarding_dependencies())
    typer.echo(_render_onboarding(result))
    raise typer.Exit(result.exit_code)
