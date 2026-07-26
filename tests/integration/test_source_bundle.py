from __future__ import annotations

from pathlib import Path
import zipfile

from foundry_opt.packaging import (
    BundleRequest,
    ValidationRequest,
    build_source_bundle,
    run_validation,
)
from foundry_opt.adapters.commands import SubprocessCommandRunner
from foundry_opt.preflight.interfaces import CommandResult


class PassingRunner:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        cwd: Path | None = None,
    ) -> CommandResult:
        self.commands.append(tuple(arguments))
        return CommandResult(0, "", "")


def test_source_bundle_packages_validated_customer_agent(tmp_path: Path) -> None:
    repository = tmp_path / "customer-agent"
    repository.mkdir()
    (repository / "main.py").write_text("print('agent')\n", encoding="utf-8")
    (repository / "requirements.txt").write_text(
        "agent-framework\n", encoding="utf-8"
    )
    (repository / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff")
    (repository / "pyproject.toml").write_text(
        """
[project]
name = "customer-agent"
version = "1.0.0"
""".strip(),
        encoding="utf-8",
    )
    runner = PassingRunner()

    validation = run_validation(ValidationRequest(repository), runner)
    artifact = build_source_bundle(
        BundleRequest(repository, tmp_path / "customer-agent.zip")
    )

    assert validation.passed
    assert artifact.sha256
    assert artifact.manifest_path.exists()
    with zipfile.ZipFile(artifact.path) as archive:
        assert archive.read("logo.png") == b"\x89PNG\r\n\x1a\n\x00\xff"
        assert set(archive.namelist()) == {
            "logo.png",
            "main.py",
            "pyproject.toml",
            "requirements.txt",
        }


def test_fallback_validation_executes_for_src_layout_package(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "customer-agent"
    package = repository / "src" / "customer_agent"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "main.py").write_text(
        "def app():\n    return None\n",
        encoding="utf-8",
    )
    (repository / "pyproject.toml").write_text(
        """
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "customer-agent"
version = "1.0.0"

[project.scripts]
customer-agent = "customer_agent.main:app"
""".strip(),
        encoding="utf-8",
    )

    report = run_validation(
        ValidationRequest(repository),
        SubprocessCommandRunner(),
    )

    assert report.discovered is False
    assert report.passed
    assert len(report.results) == 4
    assert not list(repository.rglob("__pycache__"))
