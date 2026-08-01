from __future__ import annotations

import os

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Isolate real Git fixtures owned by concurrent pytest processes."""
    if config.option.basetemp is None:
        config.option.basetemp = str(
            config.rootpath / ".pytest-tmp" / f"process-{os.getpid()}"
        )
