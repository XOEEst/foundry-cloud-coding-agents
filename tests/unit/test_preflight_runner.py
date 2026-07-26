from pathlib import Path

from foundry_opt.preflight.interfaces import PreflightCheck
from foundry_opt.preflight.models import (
    CheckResult,
    CheckStatus,
    PreflightRequest,
)
from foundry_opt.preflight.runner import PreflightRunner


class StubCheck(PreflightCheck):
    def __init__(self, check_id: str, result: CheckResult) -> None:
        self.check_id = check_id
        self._result = result
        self.calls = 0

    def run(self, request: PreflightRequest) -> CheckResult:
        self.calls += 1
        return self._result


class ExplodingCheck(PreflightCheck):
    check_id = "unexpected"

    def run(self, request: PreflightRequest) -> CheckResult:
        raise RuntimeError("request failed with secret-value")


def _request() -> PreflightRequest:
    return PreflightRequest(
        repository_root=Path("repo"),
        config_path=Path(".github/foundry-optimizer.yaml"),
        environment="acceptance",
        target="support_agent",
    )


def test_runner_executes_every_check_and_preserves_order() -> None:
    passing = StubCheck(
        "runtime.python",
        CheckResult(
            check_id="runtime.python",
            status=CheckStatus.PASS,
            summary="Python is compatible",
        ),
    )
    failing = StubCheck(
        "github.permission",
        CheckResult(
            check_id="github.permission",
            status=CheckStatus.FAIL,
            summary="Repository admin permission is required",
            remediation="Grant admin permission.",
        ),
    )
    warning = StubCheck(
        "runtime.azd",
        CheckResult(
            check_id="runtime.azd",
            status=CheckStatus.WARN,
            summary="azd is not needed by this target",
        ),
    )

    report = PreflightRunner([passing, failing, warning]).run(_request())

    assert [result.check_id for result in report.results] == [
        "runtime.python",
        "github.permission",
        "runtime.azd",
    ]
    assert passing.calls == failing.calls == warning.calls == 1
    assert report.passed is False
    assert report.exit_code == 1


def test_runner_redacts_results_and_unexpected_failures() -> None:
    leaking = StubCheck(
        "credentials.azure",
        CheckResult(
            check_id="credentials.azure",
            status=CheckStatus.FAIL,
            summary="Credential secret-value was rejected",
            detail="Authorization: Bearer secret-value",
            remediation="Rotate secret-value.",
        ),
    )

    report = PreflightRunner(
        [leaking, ExplodingCheck()],
        secrets=("secret-value",),
    ).run(_request())

    combined = " ".join(
        filter(
            None,
            (
                result.summary
                + (result.detail or "")
                + (result.remediation or "")
                for result in report.results
            ),
        )
    )
    assert "secret-value" not in combined
    assert combined.count("[REDACTED]") >= 4
    assert report.results[1].status is CheckStatus.FAIL
    assert report.results[1].summary == "Unexpected preflight check failure"


def test_warnings_and_skips_do_not_fail_the_report() -> None:
    report = PreflightRunner(
        [
            StubCheck(
                "optional.warning",
                CheckResult(
                    check_id="optional.warning",
                    status=CheckStatus.WARN,
                    summary="Optional capability unavailable",
                ),
            ),
            StubCheck(
                "dependent.skip",
                CheckResult(
                    check_id="dependent.skip",
                    status=CheckStatus.SKIP,
                    summary="Prerequisite failed",
                ),
            ),
        ]
    ).run(_request())

    assert report.passed is True
    assert report.exit_code == 0
