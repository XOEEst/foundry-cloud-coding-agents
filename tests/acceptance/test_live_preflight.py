import os
from pathlib import Path

import pytest

from foundry_opt.config import load_config
from foundry_opt.preflight.models import CheckStatus, PreflightRequest
from foundry_opt.preflight.production import build_production_preflight_runner


pytestmark = pytest.mark.acceptance


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is required for live acceptance")
    return value


def test_live_preflight_passes_in_the_dedicated_acceptance_environment() -> None:
    if os.environ.get("FOUNDRY_OPT_RUN_LIVE") != "1":
        pytest.skip("set FOUNDRY_OPT_RUN_LIVE=1 to run credentialed acceptance")

    repository_root = Path(
        _required_environment("FOUNDRY_OPT_ACCEPTANCE_REPOSITORY")
    ).resolve()
    config_path = Path(
        _required_environment("FOUNDRY_OPT_ACCEPTANCE_CONFIG")
    ).resolve()
    target = _required_environment("FOUNDRY_OPT_ACCEPTANCE_TARGET")
    config = load_config(config_path)
    environment = os.environ.get(
        "FOUNDRY_OPT_ACCEPTANCE_ENVIRONMENT",
        config.default_environment,
    )
    request = PreflightRequest(
        repository_root=repository_root,
        config_path=config_path,
        environment=environment,
        target=target,
    )

    report = build_production_preflight_runner(config, request).run(request)

    results = {result.check_id: result for result in report.results}
    assert results["github.permission"].status is CheckStatus.PASS
    assert results["foundry.access"].status is CheckStatus.PASS
    assert report.passed is True
