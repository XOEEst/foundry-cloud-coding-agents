from pathlib import Path
import shutil
import subprocess


def _console_script() -> str:
    executable = shutil.which("foundry-opt")
    assert executable is not None
    return executable


def test_installed_console_script_reports_version() -> None:
    completed = subprocess.run(
        [_console_script(), "--version"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == "foundry-opt 0.1.0"


def test_installed_console_script_reports_config_errors_without_tracebacks(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [
            _console_script(),
            "preflight",
            "--config",
            str(tmp_path / "missing.yaml"),
            "--target",
            "support_agent",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "Configuration error:" in completed.stderr
    assert "Traceback" not in completed.stderr
