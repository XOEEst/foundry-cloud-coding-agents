from pathlib import Path

from foundry_opt.deployment import (
    DeploymentTrigger,
    detect_deployment_workflow,
)


def test_detect_deployment_workflow_prefers_post_merge_foundry_cd(
    tmp_path: Path,
) -> None:
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / "manual.yml").write_text(
        "name: Manual Azure deploy\n"
        "on:\n  workflow_dispatch:\n"
        "jobs:\n  deploy:\n    steps:\n      - run: foundry-opt deploy\n",
        encoding="utf-8",
    )
    (workflows / "publish.yml").write_text(
        "name: Publish Foundry agent\n"
        "on:\n  push:\n    branches: [main]\n"
        "jobs:\n  deploy:\n    steps:\n      - run: python publish_agent.py\n",
        encoding="utf-8",
    )

    workflow = detect_deployment_workflow(tmp_path)

    assert workflow.exists is True
    assert workflow.path == Path(".github/workflows/publish.yml")
    assert workflow.trigger is DeploymentTrigger.MERGE
    assert workflow.scaffold is None


def test_detect_deployment_workflow_reports_existing_manual_mode(
    tmp_path: Path,
) -> None:
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / "deploy.yaml").write_text(
        "name: Deploy Azure Foundry\n"
        "on: workflow_dispatch\n"
        "jobs:\n  deploy:\n    steps:\n      - run: python deploy.py\n",
        encoding="utf-8",
    )

    workflow = detect_deployment_workflow(tmp_path)

    assert workflow.exists is True
    assert workflow.trigger is DeploymentTrigger.MANUAL
    assert workflow.path == Path(".github/workflows/deploy.yaml")


def test_detect_deployment_workflow_returns_scaffold_model_without_writing(
    tmp_path: Path,
) -> None:
    before = tuple(tmp_path.rglob("*"))

    workflow = detect_deployment_workflow(tmp_path)

    assert workflow.exists is False
    assert workflow.trigger is DeploymentTrigger.MANUAL
    assert workflow.path == Path(
        ".github/workflows/foundry-opt-deploy.yml"
    )
    assert workflow.scaffold is not None
    assert "source ZIP" in workflow.scaffold.description
    assert workflow.scaffold.model.trigger is DeploymentTrigger.MANUAL
    assert tuple(tmp_path.rglob("*")) == before


def test_detect_deployment_workflow_ignores_ci_and_acr_workflows(
    tmp_path: Path,
) -> None:
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "name: CI\non: [push]\njobs:\n  test:\n"
        "    steps:\n      - run: pytest\n",
        encoding="utf-8",
    )
    (workflows / "container.yml").write_text(
        "name: Deploy container to ACR\non: [push]\njobs:\n  build:\n"
        "    steps:\n      - run: az acr build --registry demo .\n",
        encoding="utf-8",
    )

    workflow = detect_deployment_workflow(tmp_path)

    assert workflow.exists is False
