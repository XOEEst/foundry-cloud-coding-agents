from __future__ import annotations

from pathlib import Path

import pytest

from foundry_opt.packaging import (
    ValidationCommand,
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


def test_run_validation_redacts_underscore_env_names_and_known_secrets(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    command = ("python", "-c", "print('known-runtime-secret')")

    class SecretRunner(RecordingRunner):
        def run(
            self,
            arguments: tuple[str, ...],
            *,
            cwd: Path | None = None,
        ) -> CommandResult:
            self.calls.append((tuple(arguments), cwd))
            return CommandResult(
                1,
                (
                    "AZURE_CLIENT_SECRET=alpha "
                    "MY_RUNTIME_TOKEN=bravo DB_PASSWORD=charlie "
                    "known-runtime-secret"
                ),
                "SERVICE_CONNECTION_STRING=AccountKey=delta",
            )

    request = ValidationRequest(
        repository,
        commands=(command,),
        secrets=("known-runtime-secret",),
    )
    report = run_validation(request, SecretRunner())

    persisted = " ".join(
        (
            *report.results[0].command,
            report.results[0].stdout,
            report.results[0].stderr,
        )
    )
    assert "alpha" not in persisted
    assert "bravo" not in persisted
    assert "charlie" not in persisted
    assert "delta" not in persisted
    assert "known-runtime-secret" not in persisted
    assert "known-runtime-secret" not in repr(request)
    assert persisted.count("[REDACTED]") >= 5


def test_run_validation_uses_configured_entry_point_and_flat_module(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "main.py").write_text(
        "def main():\n    return None\n",
        encoding="utf-8",
    )
    runner = RecordingRunner()

    report = run_validation(
        ValidationRequest(
            repository,
            entry_point=("python", "main.py"),
        ),
        runner,
    )

    rendered = [" ".join(command) for command, _ in runner.calls]
    assert report.discovered is False
    assert any("importlib" in command and "main.py" in command for command in rendered)
    assert any(
        "main.py" in command and "subprocess.run" in command
        for command in rendered
    )


def test_run_validation_preserves_workflow_working_directories(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    workflow = repository / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (repository / "services" / "default").mkdir(parents=True)
    (repository / "services" / "job").mkdir()
    (repository / "services" / "step").mkdir()
    (workflow / "ci.yml").write_text(
        """
defaults:
  run:
    working-directory: services/default
jobs:
  validate:
    defaults:
      run:
        working-directory: services/job
    steps:
      - run: uv run pytest -q
      - run: uv run ruff check .
        working-directory: services/step
  other:
    steps:
      - run: uv run mypy .
""".strip(),
        encoding="utf-8",
    )
    runner = RecordingRunner()

    run_validation(ValidationRequest(repository), runner)

    assert runner.calls == [
        (
            ("uv", "run", "pytest", "-q"),
            (repository / "services" / "job").resolve(),
        ),
        (
            ("uv", "run", "ruff", "check", "."),
            (repository / "services" / "step").resolve(),
        ),
        (
            ("uv", "run", "mypy", "."),
            (repository / "services" / "default").resolve(),
        ),
    ]


def test_run_validation_rejects_workflow_working_directory_escape(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    workflow = repository / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (workflow / "ci.yml").write_text(
        """
jobs:
  validate:
    steps:
      - run: uv run pytest
        working-directory: ../outside
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        run_validation(ValidationRequest(repository), RecordingRunner())


def test_run_validation_accepts_explicit_validation_command_directory(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    service = repository / "service"
    service.mkdir(parents=True)
    runner = RecordingRunner()

    run_validation(
        ValidationRequest(
            repository,
            commands=(
                ValidationCommand(("python", "-m", "pytest"), Path("service")),
            ),
        ),
        runner,
    )

    assert runner.calls == [
        (("python", "-m", "pytest"), service.resolve())
    ]


def test_run_validation_appends_configured_entry_checks_to_explicit_commands(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "main.py").write_text(
        "def main():\n    return None\n",
        encoding="utf-8",
    )
    runner = RecordingRunner()

    run_validation(
        ValidationRequest(
            repository,
            commands=(("python", "-m", "pytest"),),
            entry_point=("python", "main.py"),
        ),
        runner,
    )

    rendered = [" ".join(command) for command, _ in runner.calls]
    assert rendered[0] == "python -m pytest"
    assert len(rendered) == 3
    assert "importlib" in rendered[1]
    assert "main.py" in rendered[2]


def test_run_validation_appends_configured_entry_checks_to_discovered_commands(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    workflow = repository / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (repository / "main.py").write_text(
        "def main():\n    return None\n",
        encoding="utf-8",
    )
    (workflow / "ci.yml").write_text(
        "jobs:\n  test:\n    steps:\n      - run: uv run pytest\n",
        encoding="utf-8",
    )
    runner = RecordingRunner()

    run_validation(
        ValidationRequest(
            repository,
            entry_point=("python", "main.py"),
        ),
        runner,
    )

    rendered = [" ".join(command) for command, _ in runner.calls]
    assert rendered[0] == "uv run pytest"
    assert len(rendered) == 3
    assert "importlib" in rendered[1]
    assert "main.py" in rendered[2]


def test_run_validation_python_module_entry_uses_src_layout_path(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    package = repository / "src" / "demo_agent"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    runner = RecordingRunner()

    run_validation(
        ValidationRequest(
            repository,
            commands=(("python", "-m", "pytest"),),
            entry_point=("python", "-m", "demo_agent"),
        ),
        runner,
    )

    import_check = " ".join(runner.calls[1][0])
    startup_check = " ".join(runner.calls[2][0])
    assert "PYTHONPATH" in import_check
    assert repr(str((repository / "src").resolve()))[1:-1] in import_check
    assert "PYTHONPATH" in startup_check
    assert repr(str((repository / "src").resolve()))[1:-1] in startup_check


def test_run_validation_fully_redacts_quoted_json_and_whitespace_credentials(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()

    class QuotedSecretRunner(RecordingRunner):
        def run(
            self,
            arguments: tuple[str, ...],
            *,
            cwd: Path | None = None,
        ) -> CommandResult:
            return CommandResult(
                1,
                (
                    'AZURE_CLIENT_SECRET = "alpha beta" '
                    "'MY_RUNTIME_TOKEN': 'bravo charlie' "
                    '{"DB_PASSWORD": "delta echo", '
                    '"api_key": "foxtrot golf"}'
                ),
                (
                    'SERVICE_CONNECTION_STRING = '
                    '"AccountKey=hotel india;Endpoint=https://storage/"'
                ),
            )

    report = run_validation(
        ValidationRequest(
            repository,
            commands=(("python", "-c", "pass"),),
        ),
        QuotedSecretRunner(),
    )

    persisted = f"{report.results[0].stdout} {report.results[0].stderr}"
    for fragment in (
        "alpha",
        "beta",
        "bravo",
        "charlie",
        "delta",
        "echo",
        "foxtrot",
        "golf",
        "hotel",
        "india",
    ):
        assert fragment not in persisted
    assert persisted.count("[REDACTED]") >= 5


def test_run_validation_redacts_complete_multiline_private_key_blocks(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()

    class PrivateKeyRunner(RecordingRunner):
        def run(
            self,
            arguments: tuple[str, ...],
            *,
            cwd: Path | None = None,
        ) -> CommandResult:
            return CommandResult(
                1,
                (
                    "PRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----\n"
                    "base64-private-key-body\n"
                    "second-private-key-line\n"
                    "-----END RSA PRIVATE KEY-----"
                ),
                (
                    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
                    "standalone-private-key-body\n"
                    "-----END OPENSSH PRIVATE KEY-----"
                ),
            )

    report = run_validation(
        ValidationRequest(
            repository,
            commands=(("python", "-c", "pass"),),
        ),
        PrivateKeyRunner(),
    )

    persisted = f"{report.results[0].stdout}\n{report.results[0].stderr}"
    assert "base64-private-key-body" not in persisted
    assert "second-private-key-line" not in persisted
    assert "standalone-private-key-body" not in persisted
    assert "BEGIN" not in persisted
    assert "END" not in persisted
    assert persisted.count("[REDACTED]") == 2


def test_run_validation_redacts_truncated_unprefixed_private_key_to_end(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()

    class TruncatedPrivateKeyRunner(RecordingRunner):
        def run(
            self,
            arguments: tuple[str, ...],
            *,
            cwd: Path | None = None,
        ) -> CommandResult:
            return CommandResult(
                1,
                (
                    "safe diagnostic\n"
                    "-----BEGIN PRIVATE KEY-----\n"
                    "truncated-private-key-body\n"
                    "last-private-key-line"
                ),
                "",
            )

    report = run_validation(
        ValidationRequest(
            repository,
            commands=(("python", "-c", "pass"),),
        ),
        TruncatedPrivateKeyRunner(),
    )

    assert report.results[0].stdout == "safe diagnostic\n[REDACTED]"
