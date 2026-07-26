from typer.testing import CliRunner

from foundry_opt.cli import app


runner = CliRunner()


def test_help_identifies_the_optimizer() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Optimize Microsoft Foundry coding agents" in result.stdout


def test_version_reports_the_installed_cli_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "foundry-opt 0.1.0"
