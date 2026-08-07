from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from foundry_opt.config.loader import ConfigLoadError, load_config


def _write_config(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "foundry-optimizer.yaml"
    path.write_text(dedent(content), encoding="utf-8")
    return path


def _minimal_document() -> dict:
    return {
        "schema_version": "1",
        "default_environment": "acceptance",
        "environments": {
            "acceptance": {
                "project_endpoint": (
                    "https://example.services.ai.azure.com/api/projects/demo"
                ),
                "project_resource_id": (
                    "/subscriptions/sub/resourceGroups/rg/providers/"
                    "Microsoft.CognitiveServices/accounts/foundry/projects/demo"
                ),
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
                "package": {"include": ["agent/**"], "exclude": []},
                "datasets": {
                    "development": [
                        {"name": "dev", "version": "v1", "mode": "batch"}
                    ],
                    "validation": [
                        {"name": "held-out", "version": "v1", "mode": "batch"}
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
            "stale_after_hours": 2,
            "evidence_path": ".foundry-optimizer/campaigns",
            "allowed_issue_overrides": ["deadline_minutes"],
            "allowed_mutations": ["system_instructions"],
        },
        "automation_policy": {
            "allowed_dataset_sources": [
                "foundry",
                "repository",
                "synthetic",
            ],
            "allowed_evaluator_sources": [
                "foundry",
                "repository",
                "builtin",
                "custom",
            ],
            "synthetic_max_rows": 100,
            "trace_requires_human_review": True,
            "allow_spec_auto_approval": False,
            "allow_candidate_auto_selection": False,
            "allow_merge": False,
            "allow_deployment": False,
            "required_checks": ["foundry-opt/exact-patch"],
        },
    }


def _write_document(tmp_path: Path, document: dict) -> Path:
    return _write_config(tmp_path, yaml.safe_dump(document, sort_keys=False))


def test_agent_runtime_defaults_to_inherit_and_allows_explicit_override(
    tmp_path: Path,
) -> None:
    inherit_dir = tmp_path / "inherit"
    inherit_dir.mkdir()
    override_dir = tmp_path / "override"
    override_dir.mkdir()

    # A target with no runtime block inherits the published baseline: every
    # runtime field is None rather than a guessed value.
    inherit = load_config(_write_document(inherit_dir, _minimal_document()))
    runtime = inherit.targets["support_agent"].runtime
    assert runtime.runtime is None
    assert runtime.dependency_resolution is None
    assert runtime.cpu is None
    assert runtime.memory is None
    assert runtime.protocol is None
    assert runtime.protocol_version is None

    # An explicit runtime block overrides only the configured fields.
    document = _minimal_document()
    document["targets"]["support_agent"]["runtime"] = {
        "runtime": "python_3_13",
        "dependency_resolution": "bundled",
        "cpu": "0.5",
        "memory": "1Gi",
        "protocol": "responses",
        "protocol_version": "2.0",
    }
    override = load_config(_write_document(override_dir, document))
    runtime = override.targets["support_agent"].runtime
    assert runtime.runtime == "python_3_13"
    assert runtime.dependency_resolution == "bundled"
    assert runtime.cpu == "0.5"
    assert runtime.memory == "1Gi"
    assert runtime.protocol == "responses"
    assert runtime.protocol_version == "2.0"


def test_load_config_preserves_the_complete_configuration_contract(
    tmp_path: Path,
) -> None:
    path = _write_config(
        tmp_path,
        """
        schema_version: "1"
        default_environment: acceptance
        environments:
          acceptance:
            authentication: oidc
            project_endpoint: https://example.services.ai.azure.com/api/projects/demo
            project_resource_id: /subscriptions/sub/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/foundry/projects/demo
            application_insights_workspace_resource_id: /subscriptions/sub/resourceGroups/rg/providers/Microsoft.OperationalInsights/workspaces/logs
            allowed_models:
              - gpt-5.1
            deployment_workflow:
              path: .github/workflows/deploy.yml
              trigger: manual
            pricing_fallbacks:
              - model: gpt-5.1
                input_usd_per_million_tokens: 1.25
                output_usd_per_million_tokens: 10.0
                effective_date: 2026-07-01
                source: Azure Retail Prices API fallback
            future_environment_option: retained
        targets:
          support_agent:
            environment: acceptance
            source_paths:
              - agent
            edit_paths:
              - agent
              - tests
            entry_point: agent/main.py
            base_agent_version: "12"
            package:
              include:
                - agent/**
              exclude:
                - "**/__pycache__/**"
            datasets:
              development:
                - name: support-dev
                  version: v3
                  mode: batch
              validation:
                - name: support-held-out
                  version: v2
                  mode: simulation
            evaluators:
              - name: task-quality
                reference: /subscriptions/sub/resourceGroups/rg/providers/Microsoft.MachineLearningServices/workspaces/project/evaluators/task-quality
                metrics:
                  - quality
            validation_commands:
              - uv run pytest -q
            metrics:
              quality:
                direction: maximize
                threshold: 0.8
                materiality: 0.05
                hard_guardrail: false
                undefined_behavior: fail
                repeat:
                  max_repeats: 1
                  conditions:
                    - borderline
                    - noisy
                    - partial
            allowed_mutations:
              - system_instructions
              - python_logic
              - retrieval_orchestration
              - tests
              - packaging
              - model
              - skills
              - tool_descriptions
            restricted_opt_ins:
              tool_contract_schema_changes: false
              external_services: false
              infrastructure: false
              permission_expansion: false
              paid_dependencies: false
              model_deployment: false
            campaign_overrides:
              deadline_minutes: 45
              candidate_cutoff_minutes: 35
              max_changed_candidates: 2
              transient_retries: 1
            future_target_option:
              enabled: true
        campaign:
          deadline_minutes: 50
          candidate_cutoff_minutes: 40
          max_changed_candidates: 3
          transient_retries: 1
          stale_after_hours: 2
          evidence_path: .foundry-optimizer/campaigns
          allowed_issue_overrides:
            - deadline_minutes
            - max_changed_candidates
          allowed_mutations:
            - system_instructions
            - python_logic
            - retrieval_orchestration
            - tests
            - packaging
            - model
            - skills
            - tool_descriptions
        automation_policy:
          allowed_dataset_sources:
            - foundry
            - repository
            - synthetic
          allowed_evaluator_sources:
            - foundry
            - repository
            - builtin
            - custom
          synthetic_max_rows: 80
          trace_requires_human_review: true
          allow_spec_auto_approval: true
          allow_candidate_auto_selection: false
          allow_merge: false
          allow_deployment: false
          required_checks:
            - foundry-opt/spec
            - foundry-opt/exact-patch
        future_root_option:
          enabled: true
        """,
    )

    config = load_config(path)

    assert config.schema_version == "1"
    assert config.default_environment == "acceptance"
    environment = config.environments["acceptance"]
    assert environment.authentication.value == "oidc"
    assert str(environment.project_endpoint).startswith(
        "https://example.services.ai.azure.com/"
    )
    assert environment.project_resource_id.endswith("/projects/demo")
    assert environment.application_insights_workspace_resource_id.endswith(
        "/workspaces/logs"
    )
    assert environment.allowed_models == ["gpt-5.1"]
    assert environment.deployment_workflow.path.as_posix() == (
        ".github/workflows/deploy.yml"
    )
    assert environment.deployment_workflow.trigger.value == "manual"
    pricing = environment.pricing_fallbacks[0]
    assert pricing.model == "gpt-5.1"
    assert pricing.input_usd_per_million_tokens == 1.25
    assert pricing.output_usd_per_million_tokens == 10.0
    assert pricing.effective_date.isoformat() == "2026-07-01"
    assert pricing.source == "Azure Retail Prices API fallback"
    assert environment.model_extra == {"future_environment_option": "retained"}

    target = config.targets["support_agent"]
    assert target.environment == "acceptance"
    assert [path.as_posix() for path in target.source_paths] == ["agent"]
    assert [path.as_posix() for path in target.edit_paths] == ["agent", "tests"]
    assert target.entry_point.as_posix() == "agent/main.py"
    assert target.base_agent_version == "12"
    assert target.package.include == ["agent/**"]
    assert target.package.exclude == ["**/__pycache__/**"]
    assert target.datasets.development[0].name == "support-dev"
    assert target.datasets.development[0].version == "v3"
    assert target.datasets.validation[0].mode.value == "simulation"
    assert target.evaluators[0].name == "task-quality"
    assert target.evaluators[0].reference.endswith("/evaluators/task-quality")
    assert target.evaluators[0].metrics == ["quality"]
    assert target.validation_commands == ["uv run pytest -q"]
    metric = target.metrics["quality"]
    assert metric.direction.value == "maximize"
    assert metric.threshold == 0.8
    assert metric.materiality == 0.05
    assert metric.hard_guardrail is False
    assert metric.undefined_behavior.value == "fail"
    assert target.metrics["quality"].repeat.max_repeats == 1
    assert {item.value for item in metric.repeat.conditions} == {
        "borderline",
        "noisy",
        "partial",
    }
    assert {item.value for item in target.allowed_mutations} == {
        "system_instructions",
        "python_logic",
        "retrieval_orchestration",
        "tests",
        "packaging",
        "model",
        "skills",
        "tool_descriptions",
    }
    assert target.restricted_opt_ins.tool_contract_schema_changes is False
    assert target.restricted_opt_ins.external_services is False
    assert target.restricted_opt_ins.infrastructure is False
    assert target.restricted_opt_ins.permission_expansion is False
    assert target.restricted_opt_ins.paid_dependencies is False
    assert target.restricted_opt_ins.model_deployment is False
    assert target.campaign_overrides.deadline_minutes == 45
    assert target.campaign_overrides.candidate_cutoff_minutes == 35
    assert target.campaign_overrides.max_changed_candidates == 2
    assert target.campaign_overrides.transient_retries == 1
    assert target.model_extra == {"future_target_option": {"enabled": True}}

    assert config.campaign.deadline_minutes == 50
    assert config.campaign.candidate_cutoff_minutes == 40
    assert config.campaign.max_changed_candidates == 3
    assert config.campaign.transient_retries == 1
    assert config.campaign.stale_after_hours == 2
    assert config.campaign.evidence_path.as_posix() == (
        ".foundry-optimizer/campaigns"
    )
    assert {item.value for item in config.campaign.allowed_issue_overrides} == {
        "deadline_minutes",
        "max_changed_candidates",
    }
    assert {item.value for item in config.campaign.allowed_mutations} == {
        "system_instructions",
        "python_logic",
        "retrieval_orchestration",
        "tests",
        "packaging",
        "model",
        "skills",
        "tool_descriptions",
    }
    assert config.automation_policy.allowed_dataset_sources == {
        "foundry",
        "repository",
        "synthetic",
    }
    assert config.automation_policy.allowed_evaluator_sources == {
        "foundry",
        "repository",
        "builtin",
        "custom",
    }
    assert config.automation_policy.synthetic_max_rows == 80
    assert config.automation_policy.trace_requires_human_review is True
    assert config.automation_policy.allow_spec_auto_approval is True
    assert config.automation_policy.allow_candidate_auto_selection is False
    assert config.automation_policy.allow_merge is False
    assert config.automation_policy.allow_deployment is False
    assert config.automation_policy.required_checks == (
        "foundry-opt/spec",
        "foundry-opt/exact-patch",
    )
    assert config.model_extra == {"future_root_option": {"enabled": True}}


def test_load_config_supplies_safe_automation_defaults(tmp_path: Path) -> None:
    document = _minimal_document()
    document.pop("automation_policy")

    config = load_config(_write_document(tmp_path, document))

    assert config.automation_policy.allowed_dataset_sources == {
        "foundry",
        "repository",
    }
    assert config.automation_policy.allowed_evaluator_sources == {
        "foundry",
        "repository",
        "builtin",
    }
    assert config.automation_policy.trace_requires_human_review is True
    assert config.automation_policy.allow_spec_auto_approval is False
    assert config.automation_policy.allow_candidate_auto_selection is False
    assert config.automation_policy.allow_merge is False
    assert config.automation_policy.allow_deployment is False


def test_load_config_reports_path_specific_validation_errors(tmp_path: Path) -> None:
    document = _minimal_document()
    document["targets"]["support_agent"]["entry_point"] = "../outside.py"

    with pytest.raises(ConfigLoadError) as error:
        load_config(_write_document(tmp_path, document))

    assert error.value.issues[0].path == (
        "targets",
        "support_agent",
        "entry_point",
    )
    assert "repository-relative path" in error.value.issues[0].message


@pytest.mark.parametrize(
    "entry_point",
    [
        r"C:\secret\main.py",
        r"\\server\share\main.py",
    ],
)
def test_load_config_rejects_windows_qualified_repository_paths(
    tmp_path: Path,
    entry_point: str,
) -> None:
    document = _minimal_document()
    document["targets"]["support_agent"]["entry_point"] = entry_point

    with pytest.raises(ConfigLoadError) as error:
        load_config(_write_document(tmp_path, document))

    assert "repository-relative path" in str(error.value)


@pytest.mark.parametrize("field", ["include", "exclude"])
@pytest.mark.parametrize(
    "pattern",
    [
        "",
        "/outside/**",
        r"\outside\**",
        r"C:\outside\**",
        "C:/outside/**",
        r"\\server\share\**",
        "//server/share/**",
        "../outside/**",
        "agent/../outside/**",
        r"agent\..\outside\**",
    ],
)
def test_load_config_rejects_unsafe_package_globs(
    tmp_path: Path,
    field: str,
    pattern: str,
) -> None:
    document = _minimal_document()
    document["targets"]["support_agent"]["package"][field] = [pattern]

    with pytest.raises(ConfigLoadError) as error:
        load_config(_write_document(tmp_path, document))

    assert "repository-relative glob" in str(error.value)


def test_load_config_preserves_repository_relative_glob_syntax(
    tmp_path: Path,
) -> None:
    document = _minimal_document()
    document["targets"]["support_agent"]["package"] = {
        "include": ["agent/**", ".github/**"],
        "exclude": ["**/__pycache__/**"],
    }

    config = load_config(_write_document(tmp_path, document))

    assert config.targets["support_agent"].package.include == [
        "agent/**",
        ".github/**",
    ]
    assert config.targets["support_agent"].package.exclude == [
        "**/__pycache__/**"
    ]


def test_load_config_accepts_multi_session_campaign_timing(
    tmp_path: Path,
) -> None:
    document = _minimal_document()
    document["campaign"].update(
        deadline_minutes=240,
        candidate_cutoff_minutes=180,
    )
    document["targets"]["support_agent"]["campaign_overrides"] = {
        "deadline_minutes": 240,
        "candidate_cutoff_minutes": 180,
    }

    config = load_config(_write_document(tmp_path, document))

    assert config.campaign.deadline_minutes == 240
    assert config.campaign.candidate_cutoff_minutes == 180
    assert config.targets[
        "support_agent"
    ].campaign_overrides.candidate_cutoff_minutes == 180


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda document: document.update(default_environment="missing"),
            "default_environment must name a configured environment",
        ),
        (
            lambda document: document["targets"]["support_agent"].update(
                environment="missing"
            ),
            "targets reference unknown environments",
        ),
        (
            lambda document: document["targets"]["support_agent"].update(
                base_agent_version="latest"
            ),
            "positive published version",
        ),
        (
            lambda document: document["targets"]["support_agent"]["evaluators"][
                0
            ].update(metrics=["missing"]),
            "evaluator metrics are not configured",
        ),
        (
            lambda document: document["targets"]["support_agent"][
                "allowed_mutations"
            ].append("skills"),
            "allows mutations disabled by the campaign",
        ),
        (
            lambda document: document["campaign"].update(deadline_minutes=241),
            "less than or equal to 240",
        ),
        (
            lambda document: document["campaign"].update(
                candidate_cutoff_minutes=181
            ),
            "less than or equal to 180",
        ),
        (
            lambda document: document["campaign"].update(
                deadline_minutes=180,
                candidate_cutoff_minutes=180,
            ),
            "candidate_cutoff_minutes must be less than deadline_minutes",
        ),
    ],
)
def test_load_config_enforces_cross_field_invariants(
    tmp_path: Path,
    mutate,
    message: str,
) -> None:
    document = _minimal_document()
    mutate(document)

    with pytest.raises(ConfigLoadError) as error:
        load_config(_write_document(tmp_path, document))

    assert message in str(error.value)


def test_tool_contract_mutations_require_the_restricted_opt_in(
    tmp_path: Path,
) -> None:
    document = _minimal_document()
    document["targets"]["support_agent"]["allowed_mutations"].append(
        "tool_contracts"
    )
    document["campaign"]["allowed_mutations"].append("tool_contracts")

    with pytest.raises(ConfigLoadError) as error:
        load_config(_write_document(tmp_path, document))

    assert "tool_contract_schema_changes" in str(error.value)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("client_secret", "do-not-store-me"),
        ("api-key", "do-not-store-me"),
        ("access_token", "do-not-store-me"),
        ("clientSecret", "do-not-store-me"),
        ("accessToken", "do-not-store-me"),
        ("apiKey", "do-not-store-me"),
        ("connectionString", "do-not-store-me"),
        ("servicePrivateKey", "do-not-store-me"),
        ("aws-access-key", "do-not-store-me"),
        ("future_secret_value", "do-not-store-me"),
        ("tlsClientCertificate", "do-not-store-me"),
        ("artifact-signing-key", "do-not-store-me"),
        ("storageSharedKey", "do-not-store-me"),
        ("future_value", "github_pat_do_not_store_me"),
        ("secrets", ["do-not-store-me"]),
        ("client_secrets", ["do-not-store-me"]),
        ("passwords", ["do-not-store-me"]),
        ("accessTokens", ["do-not-store-me"]),
    ],
)
def test_load_config_rejects_secret_shaped_fields_and_values(
    tmp_path: Path,
    key: str,
    value: str,
) -> None:
    document = _minimal_document()
    document[key] = value

    with pytest.raises(ConfigLoadError) as error:
        load_config(_write_document(tmp_path, document))

    assert "must not contain secrets" in str(error.value)


def test_load_config_rejects_duplicate_yaml_keys(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        """
        schema_version: "1"
        schema_version: "1"
        """,
    )

    with pytest.raises(ConfigLoadError) as error:
        load_config(path)

    assert error.value.issues[0].code == "duplicate_key"
    assert "schema_version" in error.value.issues[0].message


def test_load_config_rejects_non_mapping_yaml(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "- not\n- a\n- mapping\n")

    with pytest.raises(ConfigLoadError) as error:
        load_config(path)

    assert error.value.issues[0].code == "invalid_document"


def test_load_config_rejects_non_string_yaml_mapping_keys(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "? [a, b]\n: 1\n")

    with pytest.raises(ConfigLoadError) as error:
        load_config(path)

    assert error.value.issues[0].code == "invalid_key"
    assert "mapping keys must be strings" in error.value.issues[0].message


def test_load_config_reports_invalid_utf8_as_a_configuration_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / "foundry-optimizer.yaml"
    path.write_bytes(b"\xff\xfe")

    with pytest.raises(ConfigLoadError) as error:
        load_config(path)

    assert error.value.issues[0].code == "invalid_encoding"


def test_campaign_overrides_may_set_only_one_approved_value(tmp_path: Path) -> None:
    document = _minimal_document()
    document["targets"]["support_agent"]["campaign_overrides"] = {
        "max_changed_candidates": 2
    }

    config = load_config(_write_document(tmp_path, document))

    overrides = config.targets["support_agent"].campaign_overrides
    assert overrides.max_changed_candidates == 2
    assert overrides.deadline_minutes is None


@pytest.mark.parametrize(
    "base_version",
    ["latest", "draft-20260726", "release-12", "0", "-1"],
)
def test_base_agent_version_requires_a_positive_published_version(
    tmp_path: Path,
    base_version: str,
) -> None:
    document = _minimal_document()
    document["targets"]["support_agent"]["base_agent_version"] = base_version

    with pytest.raises(ConfigLoadError) as error:
        load_config(_write_document(tmp_path, document))

    assert "positive published version" in str(error.value)


@pytest.mark.parametrize(
    ("campaign_timing", "campaign_overrides"),
    [
        ({}, {"deadline_minutes": 40}),
        (
            {"deadline_minutes": 30, "candidate_cutoff_minutes": 20},
            {"candidate_cutoff_minutes": 30},
        ),
    ],
)
def test_campaign_overrides_validate_effective_inherited_timing(
    tmp_path: Path,
    campaign_timing: dict,
    campaign_overrides: dict,
) -> None:
    document = _minimal_document()
    document["campaign"].update(campaign_timing)
    document["targets"]["support_agent"]["campaign_overrides"] = campaign_overrides

    with pytest.raises(ConfigLoadError) as error:
        load_config(_write_document(tmp_path, document))

    assert "candidate_cutoff_minutes must be less than deadline_minutes" in str(
        error.value
    )


def test_forward_compatible_non_secret_token_fields_are_retained(
    tmp_path: Path,
) -> None:
    document = _minimal_document()
    document["campaign"]["token_budget"] = 100_000
    document["campaign"]["public_key"] = "retained-public-material"

    config = load_config(_write_document(tmp_path, document))

    assert config.campaign.model_extra == {
        "token_budget": 100_000,
        "public_key": "retained-public-material",
    }


def test_load_config_rejects_unknown_authentication_mode(tmp_path: Path) -> None:
    document = _minimal_document()
    document["environments"]["acceptance"]["authentication"] = "password"

    with pytest.raises(ConfigLoadError) as error:
        load_config(_write_document(tmp_path, document))

    assert "authentication" in str(error.value)
