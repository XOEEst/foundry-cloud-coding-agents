from pathlib import Path
import json
import os
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


def test_installed_auth_probe_reports_presence_without_printing_oidc_token(
    tmp_path: Path,
) -> None:
    request_token = "eyJrequesttoken.header.signature"
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("AZURE_", "COPILOT_AGENT_", "ACTIONS_ID_TOKEN_"))
    }
    environment.update(
        {
            "GITHUB_ACTIONS": "true",
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://example.invalid/oidc",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": request_token,
        }
    )

    completed = subprocess.run(
        [
            _console_script(),
            "auth",
            "probe",
            "--scope",
            "copilot-optimizer",
            "--json",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    document = json.loads(completed.stdout)
    assert document["environment_kind"] == "actions_setup"
    assert document["oidc_request_variables"]["present"] is True
    assert document["direct_operations_eligible"] is False
    assert request_token not in completed.stdout
    assert "Traceback" not in completed.stderr
