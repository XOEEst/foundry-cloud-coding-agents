import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

import foundry_opt.cli as cli
from foundry_opt.preflight.models import (
    CheckResult,
    CheckStatus,
    PreflightReport,
)
from foundry_opt.preflight.runner import PreflightRunner


runner = CliRunner()


def _write_config(root: Path) -> Path:
    path = root / ".github" / "foundry-optimizer.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1",
                "default_environment": "acceptance",
                "environments": {
                    "acceptance": {
                        "project_endpoint": (
                            "https://example.services.ai.azure.com/api/projects/demo"
                        ),
                        "project_resource_id": "/subscriptions/sub/projects/demo",
                        "allowed_models": ["gpt-5.1"],
                        "deployment_workflow": {
                            "path": ".github/workflows/deploy.yml",
                            "trigger": "manual",
                        },
                    }
                },
                "targets": {
                    "support_agent": {
                        "environment": "acceptance",
                        "source_paths": ["agent"],
                        "edit_paths": ["agent"],
                        "entry_point": "agent/main.py",
                        "base_agent_version": "12",
                        "package": {"include": ["agent/**"]},
                        "datasets": {
                            "development": [
                                {"name": "dev", "version": "v1", "mode": "batch"}
                            ],
                            "validation": [
                                {
                                    "name": "held-out",
                                    "version": "v1",
                                    "mode": "batch",
                                }
                            ],
                        },
                        "evaluators": [
                            {
                                "name": "quality",
                                "reference": "quality-evaluator",
                                "metrics": ["quality"],
                            }
                        ],
                        "validation_commands": ["uv run pytest -q"],
                        "metrics": {
                            "quality": {
                                "direction": "maximize",
                                "threshold": 0.8,
                                "materiality": 0.05,
                                "hard_guardrail": False,
                                "undefined_behavior": "fail",
                            }
                        },
                        "allowed_mutations": ["system_instructions"],
                    }
                },
                "campaign": {
                    "deadline_minutes": 50,
                    "candidate_cutoff_minutes": 40,
                    "max_changed_candidates": 3,
                    "transient_retries": 1,
                    "allowed_mutations": ["system_instructions"],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


class StubRunner:
    def __init__(self, report: PreflightReport) -> None:
        self.report = report
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return self.report


class LeakingCheck:
    check_id = "github.permission"

    def run(self, request):
        return CheckResult(
            check_id=self.check_id,
            status=CheckStatus.FAIL,
            summary="Permission denied for secret-value",
            detail="Authorization: Bearer secret-value",
            remediation="Remove token=secret-value from the diagnostic.",
        )


def test_preflight_uses_default_config_and_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _write_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    preflight_runner = StubRunner(
        PreflightReport(
            (
                CheckResult(
                    check_id="runtime.python",
                    status=CheckStatus.PASS,
                    summary="Python is compatible",
                ),
            )
        )
    )
    factory_calls = []

    def build_runner(config, request):
        factory_calls.append((config, request))
        return preflight_runner

    monkeypatch.setattr(cli, "build_preflight_runner", build_runner)

    result = runner.invoke(cli.app, ["preflight", "--target", "support_agent"])

    assert result.exit_code == 0
    assert "PASS    runtime.python  Python is compatible" in result.stdout
    assert len(factory_calls) == 1
    request = preflight_runner.requests[0]
    assert request.config_path == Path(".github/foundry-optimizer.yaml")
    assert request.environment == "acceptance"
    assert request.target == "support_agent"


def test_preflight_rejects_an_unknown_target_before_building_the_runner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = _write_config(tmp_path)

    def unexpected_factory(config, request):
        raise AssertionError("runner factory must not be called")

    monkeypatch.setattr(cli, "build_preflight_runner", unexpected_factory)

    result = runner.invoke(
        cli.app,
        [
            "preflight",
            "--config",
            str(config_path),
            "--target",
            "missing",
        ],
    )

    assert result.exit_code == 2
    assert "Configuration error: target 'missing' is not configured." in result.stderr
    assert "Traceback" not in result.output


def test_preflight_rejects_an_unknown_environment_before_building_the_runner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = _write_config(tmp_path)

    def unexpected_factory(config, request):
        raise AssertionError("runner factory must not be called")

    monkeypatch.setattr(cli, "build_preflight_runner", unexpected_factory)

    result = runner.invoke(
        cli.app,
        [
            "preflight",
            "--config",
            str(config_path),
            "--environment",
            "missing",
            "--target",
            "support_agent",
        ],
    )

    assert result.exit_code == 2
    assert (
        "Configuration error: environment 'missing' is not configured."
        in result.stderr
    )


def test_preflight_rejects_a_target_from_another_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = _write_config(tmp_path)
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    document["environments"]["production"] = document["environments"][
        "acceptance"
    ].copy()
    config_path.write_text(
        yaml.safe_dump(document, sort_keys=False),
        encoding="utf-8",
    )

    def unexpected_factory(config, request):
        raise AssertionError("runner factory must not be called")

    monkeypatch.setattr(cli, "build_preflight_runner", unexpected_factory)

    result = runner.invoke(
        cli.app,
        [
            "preflight",
            "--config",
            str(config_path),
            "--environment",
            "production",
            "--target",
            "support_agent",
        ],
    )

    assert result.exit_code == 2
    assert (
        "Configuration error: target 'support_agent' uses environment "
        "'acceptance', not 'production'." in result.stderr
    )


def test_preflight_reports_invalid_config_without_a_traceback(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "invalid.yaml"
    config_path.write_text("schema_version: [", encoding="utf-8")

    result = runner.invoke(
        cli.app,
        [
            "preflight",
            "--config",
            str(config_path),
            "--target",
            "support_agent",
        ],
    )

    assert result.exit_code == 2
    assert "Configuration error:" in result.stderr
    assert "Traceback" not in result.output


def test_preflight_json_preserves_failure_exit_and_redacted_results(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = _write_config(tmp_path)
    monkeypatch.setattr(
        cli,
        "build_preflight_runner",
        lambda config, request: PreflightRunner(
            [LeakingCheck()],
            secrets=("secret-value",),
        ),
    )

    result = runner.invoke(
        cli.app,
        [
            "preflight",
            "--config",
            str(config_path),
            "--target",
            "support_agent",
            "--json",
        ],
    )

    assert result.exit_code == 1
    document = json.loads(result.stdout)
    assert document["passed"] is False
    assert document["exit_code"] == 1
    assert document["results"][0]["check_id"] == "github.permission"
    assert "secret-value" not in result.output
    assert "[REDACTED]" in result.stdout
