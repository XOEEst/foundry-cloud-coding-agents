from __future__ import annotations

from pathlib import Path
import subprocess
import zipfile


def test_built_wheel_contains_issue_only_onboarding_templates(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[2]
    subprocess.run(
        (
            "uv",
            "build",
            "--wheel",
            "--out-dir",
            str(tmp_path),
        ),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(tmp_path.glob("*.whl"))

    with zipfile.ZipFile(wheel) as archive:
        skill_path = (
            "foundry_opt/templates/skills/"
            "foundry-agent-optimizer/SKILL.md"
        )
        names = set(archive.namelist())
        assert skill_path in names
        assert "foundry_opt/onboarding/bundle.py" in names
        assert "foundry_opt/orchestration/transport.py" in names
        skill = archive.read(skill_path).decode("utf-8")

    assert "Create one `[Optimize]` issue" in skill
    assert "foundry-opt steward advance --issue <number>" in skill
