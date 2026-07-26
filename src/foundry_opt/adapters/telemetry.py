from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol


class TelemetryAdapterError(RuntimeError):
    """Base class for stable telemetry adapter failures."""


class TelemetryBoundsError(TelemetryAdapterError, ValueError):
    pass


class TelemetrySchemaError(TelemetryAdapterError):
    pass


@dataclass(frozen=True)
class TelemetryQueryRequest:
    application_id: str
    start_time: datetime
    end_time: datetime
    response_ids: tuple[str, ...]
    max_rows: int = 500
    timeout_seconds: int = 20

    def __post_init__(self) -> None:
        if not self.application_id.strip():
            raise TelemetryBoundsError("application_id is required.")
        if self.start_time.tzinfo is None or self.end_time.tzinfo is None:
            raise TelemetryBoundsError("Telemetry bounds must be timezone-aware.")
        if (
            self.end_time <= self.start_time
            or self.end_time - self.start_time > timedelta(hours=24)
        ):
            raise TelemetryBoundsError(
                "Telemetry time range must be positive and no longer than 24 hours."
            )
        if not self.response_ids or len(self.response_ids) > 100:
            raise TelemetryBoundsError(
                "Telemetry requests require between 1 and 100 response IDs."
            )
        if any(not response_id.strip() for response_id in self.response_ids):
            raise TelemetryBoundsError("Telemetry response IDs cannot be empty.")
        if len(self.response_ids) != len(set(self.response_ids)):
            raise TelemetryBoundsError("Telemetry response IDs must be unique.")
        if self.max_rows < 1 or self.max_rows > 1000:
            raise TelemetryBoundsError("max_rows must be between 1 and 1000.")
        if self.timeout_seconds < 1 or self.timeout_seconds > 30:
            raise TelemetryBoundsError(
                "timeout_seconds must be between 1 and 30."
            )


@dataclass(frozen=True)
class TelemetryAggregate:
    response_id: str
    request_count: int
    dependency_count: int
    exception_count: int
    duration_ms: float
    success_rate: float | None


@dataclass(frozen=True)
class TelemetryEnrichment:
    application_id: str
    start_time: datetime
    end_time: datetime
    items: tuple[TelemetryAggregate, ...]


class ReadOnlyKqlTransport(Protocol):
    def query(
        self,
        query: str,
        parameters: dict[str, object],
        *,
        timeout_seconds: int,
    ) -> Sequence[Mapping[str, object]]: ...


class ApplicationInsightsTelemetry:
    def __init__(self, transport: ReadOnlyKqlTransport) -> None:
        self._transport = transport

    def enrich(self, request: TelemetryQueryRequest) -> TelemetryEnrichment:
        query = _aggregate_query(request.max_rows)
        parameters: dict[str, object] = {
            "application_id": request.application_id,
            "start_time": request.start_time.isoformat(),
            "end_time": request.end_time.isoformat(),
            "response_ids": list(request.response_ids),
        }
        rows = self._transport.query(
            query,
            parameters,
            timeout_seconds=request.timeout_seconds,
        )
        return TelemetryEnrichment(
            application_id=request.application_id,
            start_time=request.start_time,
            end_time=request.end_time,
            items=tuple(_parse_aggregate(row) for row in rows),
        )


def _aggregate_query(max_rows: int) -> str:
    return (
        "let selected = dynamic($response_ids);\n"
        "requests\n"
        "| where timestamp between (datetime($start_time) .. datetime($end_time))\n"
        "| where tostring(customDimensions.response_id) in (selected)\n"
        "| summarize request_count=count(), "
        "dependency_count=sum(toint(customMeasurements.dependency_count)), "
        "exception_count=sum(toint(customMeasurements.exception_count)), "
        "duration_ms=avg(duration), success_rate=avg(todouble(success)) "
        "by response_id=tostring(customDimensions.response_id)\n"
        f"| take {max_rows}"
    )


def _parse_aggregate(row: Mapping[str, object]) -> TelemetryAggregate:
    try:
        success_rate = row.get("success_rate")
        return TelemetryAggregate(
            response_id=str(row["response_id"]),
            request_count=int(row["request_count"]),
            dependency_count=int(row["dependency_count"]),
            exception_count=int(row["exception_count"]),
            duration_ms=float(row["duration_ms"]),
            success_rate=(
                None if success_rate is None else float(success_rate)
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise TelemetrySchemaError(
            "Telemetry response did not match the aggregate schema."
        ) from error
