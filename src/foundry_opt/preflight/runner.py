from dataclasses import replace
from collections.abc import Iterable, Sequence

from foundry_opt.preflight.interfaces import PreflightCheck
from foundry_opt.preflight.models import (
    CheckResult,
    CheckStatus,
    PreflightReport,
    PreflightRequest,
)
from foundry_opt.preflight.redaction import redact


class PreflightRunner:
    def __init__(
        self,
        checks: Sequence[PreflightCheck],
        *,
        secrets: Iterable[str] = (),
    ) -> None:
        self._checks = tuple(checks)
        self._secrets = tuple(secrets)

    def run(self, request: PreflightRequest) -> PreflightReport:
        results: list[CheckResult] = []
        for check in self._checks:
            try:
                result = check.run(request)
            except Exception as error:
                result = CheckResult(
                    check_id=check.check_id,
                    status=CheckStatus.FAIL,
                    summary="Unexpected preflight check failure",
                    detail=str(error),
                    remediation="Review the diagnostic and retry preflight.",
                )
            results.append(self._redact_result(result))
        return PreflightReport(tuple(results))

    def _redact_result(self, result: CheckResult) -> CheckResult:
        return replace(
            result,
            summary=redact(result.summary, self._secrets) or "",
            detail=redact(result.detail, self._secrets),
            remediation=redact(result.remediation, self._secrets),
        )
