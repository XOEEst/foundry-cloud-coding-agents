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
            "python",
            "-m",
            "hatchling",
            "build",
            "--target",
            "wheel",
            "--directory",
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
        assert "foundry_opt/onboarding/generation.py" in names
        assert "foundry_opt/orchestration/git_transport.py" in names
        assert "foundry_opt/orchestration/transport.py" in names
        assert "foundry_opt/orchestration/handoff.py" in names
        assert "foundry_opt/orchestration/capability_bridge.py" in names
        bundle = archive.read(
            "foundry_opt/onboarding/bundle.py"
        ).decode("utf-8")
        generation = archive.read(
            "foundry_opt/onboarding/generation.py"
        ).decode("utf-8")
        skill = archive.read(skill_path).decode("utf-8")
    current_bundle_source = bundle.split(
        "def _issue_intake_workflow",
        1,
    )[0]

    assert "Create one `[Optimize]` issue" in skill
    assert "foundry-opt workspace advance --issue <number>" in skill
    assert "next_actions" in skill
    assert "FOUNDRY_OPT_COPILOT_GIT_PROXY=1" in generation
    assert "https://ai.azure.com/.default" in generation
    assert "https://cognitiveservices.azure.com/.default" in generation
    assert "--output none" in generation
    assert "https://management.azure.com" not in generation
    assert "pull_request_target" in bundle
    assert "foundry-optimization-workspace.yml" in bundle
    assert "foundry-optimization-operations.yml" in bundle
    assert "foundry-opt workspace operations execute" in (
        current_bundle_source
    )
    assert "foundry-opt workspace operations reconcile" in (
        current_bundle_source
    )
    assert "gh pr comment" in current_bundle_source
    assert "gh pr create" not in current_bundle_source
    assert "foundry-candidate-designer.agent.md" not in bundle.split(
        "def _previous_repository_agent_bundle",
        1,
    )[0]
    assert (
        "python -m foundry_opt.orchestration.capability_bridge"
        not in current_bundle_source
    )
    assert "foundry-opt steward advance" not in skill
    assert "foundry-optimization-handoff.yml" not in skill
