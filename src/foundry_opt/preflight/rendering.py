import json

from foundry_opt.preflight.models import PreflightReport


def render_human(report: PreflightReport) -> str:
    status_width = max(
        len("STATUS"),
        *(len(result.status.value) for result in report.results),
    )
    check_width = max(
        len("CHECK"),
        *(len(result.check_id) for result in report.results),
    )
    lines = [
        f"{'STATUS':<{status_width}}  {'CHECK':<{check_width}}  SUMMARY",
        *(
            f"{result.status.value.upper():<{status_width}}  "
            f"{result.check_id:<{check_width}}  {result.summary}"
            for result in report.results
        ),
    ]

    details = [
        f"  {result.check_id}: {result.detail}"
        for result in report.results
        if result.detail
    ]
    if details:
        lines.extend(("", "Details:", *details))

    remediation = [
        f"- {result.check_id}: {result.remediation}"
        for result in report.results
        if result.remediation
    ]
    if remediation:
        lines.extend(("", "Remediation:", *remediation))

    return "\n".join(lines)


def render_json(report: PreflightReport) -> str:
    document = {
        "passed": report.passed,
        "exit_code": report.exit_code,
        "results": [
            {
                "check_id": result.check_id,
                "status": result.status.value,
                "summary": result.summary,
                "detail": result.detail,
                "remediation": result.remediation,
                "duration_ms": result.duration_ms,
            }
            for result in report.results
        ],
    }
    return json.dumps(document, indent=2)
