from pathlib import Path
from typing import Annotated

import typer

from foundry_opt import __version__
from foundry_opt.config import OptimizerConfig, load_config
from foundry_opt.config.loader import ConfigLoadError
from foundry_opt.onboarding import (
    OnboardingDependencies,
    OnboardingRequest,
    OnboardingResult,
    run_onboarding,
)
from foundry_opt.preflight.models import PreflightRequest
from foundry_opt.preflight.redaction import redact
from foundry_opt.preflight.rendering import render_human, render_json
from foundry_opt.preflight.runner import PreflightRunner


app = typer.Typer(
    help="Optimize Microsoft Foundry coding agents with reviewable evidence.",
    no_args_is_help=True,
)


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
    for guidance in result.guidance:
        lines.append(f"Next: {guidance}")
    lines.append(f"Draft PR: {result.draft_pull_request.title}")
    lines.append(result.draft_pull_request.body)
    if result.published_pull_request is not None:
        lines.append(f"Draft PR URL: {result.published_pull_request.url}")
    return "\n".join(lines)


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
) -> None:
    """Discover and draft secretless repository onboarding."""
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
    )
    result = run_onboarding(request, build_onboarding_dependencies())
    typer.echo(_render_onboarding(result))
    raise typer.Exit(result.exit_code)
