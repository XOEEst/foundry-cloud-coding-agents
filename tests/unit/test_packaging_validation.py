from __future__ import annotations

from pathlib import Path

from foundry_opt.packaging import (
    ValidationRequest,
    run_validation,
)
from foundry_opt.preflight.interfaces import CommandResult


class RecordingRunner:
    def __init__(self, failures: set[str] | None = None) -> None:
        self.failures = failures or set()
        self.calls: list[tuple[tuple[str, ...], Path | None]] = []

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        cwd: Path | None = None,
    ) -> CommandResult:
        command = tuple(arguments)
        self.calls.append((command, cwd))
        rendered = " ".join(command)
        if rendered in self.failures:
            return CommandResult(
                exit_code=1,
                stdout="token=customer-secret",
                stderr="validation failed",
            )
        return CommandResult(exit_code=0, stdout="ok", stderr="")


def test_run_validation_discovers_existing_python_and_workflow_commands(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    workflow = repository / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (repository / "pyproject.toml").write_text(
        """
[project]
name = "demo"
version = "1.0.0"

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.ruff]
line-length = 100

[tool.mypy]
python_version = "3.13"
""".strip(),
        encoding="utf-8",
    )
    (workflow / "ci.yml").write_text(
        """
jobs:
  validate:
    steps:
      - run: uv run pytest -q
      - run: uv run ruff check .
      - run: uv run mypy src
      - run: echo not-a-validation-command
""".strip(),
        encoding="utf-8",
    )
    runner = RecordingRunner()

    report = run_validation(ValidationRequest(repository), runner)

    commands = [call[0] for call in runner.calls]
    assert commands == [
        ("uv", "run", "pytest", "-q"),
        ("uv", "run", "ruff", "check", "."),
        ("uv", "run", "mypy", "src"),
    ]
    assert report.discovered is True
    assert report.passed is True
    assert all(call[1] == repository.resolve() for call in runner.calls)


def test_run_validation_uses_safe_fallbacks_without_forcing_optional_tools(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    package = repository / "src" / "demo_agent"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "main.py").write_text("def app(): pass\n", encoding="utf-8")
    (repository / "pyproject.toml").write_text(
        """
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "demo-agent"
version = "1.0.0"

[project.scripts]
demo-agent = "demo_agent.main:app"
""".strip(),
        encoding="utf-8",
    )
    runner = RecordingRunner()

    report = run_validation(ValidationRequest(repository), runner)

    rendered = [" ".join(call[0]) for call in runner.calls]
    assert report.discovered is False
    assert any("compile(" in command for command in rendered)
    assert any("import demo_agent" in command for command in rendered)
    assert any("tomllib" in command for command in rendered)
    assert any("--help" in command for command in rendered)
    assert not any("pytest" in command for command in rendered)
    assert not any("ruff" in command for command in rendered)
    assert not any("mypy" in command for command in rendered)


def test_run_validation_discovers_common_standalone_configs(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (repository / ".flake8").write_text("[flake8]\n", encoding="utf-8")
    (repository / "mypy.ini").write_text("[mypy]\n", encoding="utf-8")
    runner = RecordingRunner()

    report = run_validation(ValidationRequest(repository), runner)

    assert report.discovered is True
    assert [call[0] for call in runner.calls] == [
        ("python", "-m", "pytest"),
        ("flake8", "."),
        ("mypy", "."),
    ]


def test_run_validation_fallback_has_startup_smoke_without_console_script(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    package = repository / "src" / "demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    runner = RecordingRunner()

    run_validation(ValidationRequest(repository), runner)

    assert any(
        "subprocess.run" in " ".join(command) and "--help" in " ".join(command)
        for command, _ in runner.calls
    )


def test_run_validation_runs_all_checks_and_redacts_failures(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    first = ("python", "-c", "compile(source, name, 'exec')")
    second = ("python", "-c", "import demo")
    runner = RecordingRunner({" ".join(first)})

    report = run_validation(
        ValidationRequest(repository, commands=(first, second)),
        runner,
    )

    assert len(report.results) == 2
    assert report.passed is False
    assert report.results[0].passed is False
    assert report.results[0].stdout == "token=[REDACTED]"
    assert report.results[1].passed is True
