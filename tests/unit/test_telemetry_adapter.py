from datetime import UTC, datetime, timedelta

import pytest

from foundry_opt.adapters.telemetry import (
    ApplicationInsightsTelemetry,
    TelemetryBoundsError,
    TelemetryQueryRequest,
    TelemetrySchemaError,
)


class FakeKqlTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object], int]] = []
        self.rows: list[dict[str, object]] = [
            {
                "response_id": "response-1",
                "request_count": 2,
                "dependency_count": 3,
                "exception_count": 0,
                "duration_ms": 125.5,
                "success_rate": 1.0,
                "raw_trace": "must remain transient",
            }
        ]

    def query(
        self,
        query: str,
        parameters: dict[str, object],
        *,
        timeout_seconds: int,
    ) -> list[dict[str, object]]:
        self.calls.append((query, parameters, timeout_seconds))
        return self.rows


def _request(**overrides: object) -> TelemetryQueryRequest:
    start = datetime(2026, 7, 26, 20, tzinfo=UTC)
    values: dict[str, object] = {
        "application_id": "app-insights-1",
        "start_time": start,
        "end_time": start + timedelta(hours=1),
        "response_ids": ("response-1",),
        "max_rows": 100,
        "timeout_seconds": 10,
    }
    values.update(overrides)
    return TelemetryQueryRequest(**values)


def test_telemetry_query_is_parameterized_bounded_and_returns_aggregates_only() -> None:
    transport = FakeKqlTransport()
    adapter = ApplicationInsightsTelemetry(transport)

    enrichment = adapter.enrich(_request())

    query, parameters, timeout = transport.calls[0]
    assert "response-1" not in query
    assert "take 100" in query
    assert parameters["response_ids"] == ["response-1"]
    assert timeout == 10
    assert all(
        word not in query.casefold()
        for word in ("delete", "drop", "set-or-append", "ingest")
    )
    assert enrichment.items[0].response_id == "response-1"
    assert not hasattr(enrichment.items[0], "raw_trace")


@pytest.mark.parametrize(
    "overrides",
    [
        {"end_time": datetime(2026, 7, 28, tzinfo=UTC)},
        {"response_ids": tuple(f"response-{index}" for index in range(101))},
        {"response_ids": ("",)},
        {"max_rows": 1001},
        {"timeout_seconds": 31},
    ],
)
def test_telemetry_query_rejects_requests_outside_fixed_bounds(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(TelemetryBoundsError):
        _request(**overrides)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("duration_ms", float("nan")),
        ("duration_ms", float("inf")),
        ("duration_ms", float("-inf")),
        ("success_rate", float("nan")),
        ("success_rate", float("inf")),
        ("success_rate", float("-inf")),
    ],
)
def test_telemetry_parser_rejects_non_finite_float_aggregates(
    field: str,
    value: float,
) -> None:
    transport = FakeKqlTransport()
    transport.rows[0][field] = value

    with pytest.raises(TelemetrySchemaError):
        ApplicationInsightsTelemetry(transport).enrich(_request())
