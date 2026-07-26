import json

from foundry_opt.preflight.models import (
    CheckResult,
    CheckStatus,
    PreflightReport,
)
from foundry_opt.preflight.rendering import render_human, render_json


def test_human_report_shows_checks_and_actionable_remediation() -> None:
    report = PreflightReport(
        (
            CheckResult(
                check_id="runtime.python",
                status=CheckStatus.PASS,
                summary="Python is compatible",
                duration_ms=4,
            ),
            CheckResult(
                check_id="github.permission",
                status=CheckStatus.FAIL,
                summary="Repository admin permission is required",
                detail="Authenticated user has write permission.",
                remediation="Grant repository admin permission.",
                duration_ms=12,
            ),
        )
    )

    output = render_human(report)

    assert "STATUS  CHECK              SUMMARY" in output
    assert "PASS    runtime.python     Python is compatible" in output
    assert (
        "FAIL    github.permission  Repository admin permission is required"
        in output
    )
    assert "Authenticated user has write permission." in output
    assert "Remediation:" in output
    assert "- github.permission: Grant repository admin permission." in output


def test_json_report_has_a_stable_ordered_document() -> None:
    report = PreflightReport(
        (
            CheckResult(
                check_id="runtime.python",
                status=CheckStatus.PASS,
                summary="Python is compatible",
                duration_ms=4,
            ),
            CheckResult(
                check_id="github.permission",
                status=CheckStatus.FAIL,
                summary="Repository admin permission is required",
                detail="Authenticated user has write permission.",
                remediation="Grant repository admin permission.",
                duration_ms=12,
            ),
        )
    )

    document = json.loads(render_json(report))

    assert document == {
        "passed": False,
        "exit_code": 1,
        "results": [
            {
                "check_id": "runtime.python",
                "status": "pass",
                "summary": "Python is compatible",
                "detail": None,
                "remediation": None,
                "duration_ms": 4,
            },
            {
                "check_id": "github.permission",
                "status": "fail",
                "summary": "Repository admin permission is required",
                "detail": "Authenticated user has write permission.",
                "remediation": "Grant repository admin permission.",
                "duration_ms": 12,
            },
        ],
    }
