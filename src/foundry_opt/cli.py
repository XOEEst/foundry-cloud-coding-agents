from pathlib import Path
from typing import Annotated

import typer

from foundry_opt import __version__
from foundry_opt.config import OptimizerConfig, load_config
from foundry_opt.config.loader import ConfigLoadError
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
