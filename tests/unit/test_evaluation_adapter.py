from collections.abc import Mapping
from datetime import UTC, datetime

import pytest

from foundry_opt.adapters.evaluation import (
    BatchEvaluationRequest,
    BatchEvaluationOutput,
    EvaluationDefinitionRequest,
    EvaluationGateway,
    EvaluationOutputPage,
    EvaluationPaginationError,
    EvaluationPollingTimeoutError,
    EvaluationSchemaError,
    MultiTurnSimulationOutput,
    MultiTurnSimulationRequest,
    PollPolicy,
)
from foundry_opt.evaluation import (
    AgentVersionRef,
    DatasetVersionRef,
    EvaluatorDefinitionRef,
    EvaluationStatus,
)


class FakeEvaluationTransport:
    def __init__(self) -> None:
        self.definition: Mapping[str, object] | None = None
        self.created_definitions: list[Mapping[str, object]] = []
        self.created_runs: list[Mapping[str, object]] = []
        self.run_responses: list[Mapping[str, object] | Exception] = []
        self.page_responses: dict[str | None, Mapping[str, object]] = {}
        self.page_calls: list[tuple[str, str | None, int]] = []

    def find_definition(self, fingerprint: str) -> Mapping[str, object] | None:
        return self.definition

    def create_definition(
        self, payload: Mapping[str, object]
    ) -> Mapping[str, object]:
        self.created_definitions.append(payload)
        return {
            "id": "eval-def-1",
            "version": "7",
            "fingerprint": payload["fingerprint"],
            "portal_url": "https://portal.azure.com/evaluators/eval-def-1",
        }

    def create_run(self, payload: Mapping[str, object]) -> Mapping[str, object]:
        self.created_runs.append(payload)
        return _run_payload("queued")

    def get_run(self, run_id: str) -> Mapping[str, object]:
        response = self.run_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def list_output_items(
        self,
        run_id: str,
        *,
        continuation_token: str | None,
        page_size: int,
    ) -> Mapping[str, object]:
        self.page_calls.append((run_id, continuation_token, page_size))
        return self.page_responses[continuation_token]


def _run_payload(status: str) -> dict[str, object]:
    return {
        "run_id": "run-1",
        "evaluation_id": "evaluation-1",
        "status": status,
        "agent": {
            "agent_id": "agent-1",
            "draft_id": "draft-9",
            "version": "3",
        },
        "dataset": {"dataset_id": "dataset-1", "version": "12"},
        "evaluator": {"definition_id": "eval-def-1", "version": "7"},
        "portal_url": "https://portal.azure.com/evaluations/evaluation-1/runs/run-1",
        "started_at": "2026-07-26T20:00:00Z",
        "completed_at": (
            "2026-07-26T20:01:00Z" if status in {"completed", "partial"} else None
        ),
        "error": "one case failed" if status == "partial" else None,
    }


def _batch_request() -> BatchEvaluationRequest:
    return BatchEvaluationRequest(
        display_name="candidate development",
        agent=AgentVersionRef("agent-1", "draft-9", "3"),
        dataset=DatasetVersionRef("dataset-1", "12"),
        evaluator=EvaluatorDefinitionRef("eval-def-1", "7"),
    )


def test_create_or_reuse_definition_reuses_exact_fingerprint() -> None:
    transport = FakeEvaluationTransport()
    transport.definition = {
        "id": "eval-def-1",
        "version": "7",
        "fingerprint": "sha256:definition",
        "portal_url": "https://portal.azure.com/evaluators/eval-def-1",
    }
    gateway = EvaluationGateway(transport)

    result = gateway.create_or_reuse_definition(
        EvaluationDefinitionRequest(
            name="quality",
            evaluator_type="builtin",
            schema_version="1",
            configuration={"metric": "quality"},
            fingerprint="sha256:definition",
        )
    )

    assert result.definition_id == "eval-def-1"
    assert result.version == "7"
    assert transport.created_definitions == []


def test_create_run_serializes_only_pinned_batch_inputs() -> None:
    transport = FakeEvaluationTransport()
    gateway = EvaluationGateway(transport)

    run = gateway.create_run(_batch_request())

    assert run.run_id == "run-1"
    assert transport.created_runs == [
        {
            "kind": "batch",
            "display_name": "candidate development",
            "agent": {
                "agent_id": "agent-1",
                "draft_id": "draft-9",
                "version": "3",
            },
            "dataset": {"dataset_id": "dataset-1", "version": "12"},
            "evaluator": {"definition_id": "eval-def-1", "version": "7"},
        }
    ]


def test_create_run_keeps_multi_turn_schema_inside_adapter() -> None:
    transport = FakeEvaluationTransport()
    gateway = EvaluationGateway(transport)

    gateway.create_run(
        MultiTurnSimulationRequest(
            display_name="candidate simulation",
            agent=AgentVersionRef("agent-1", "draft-9", "3"),
            dataset=DatasetVersionRef("dataset-1", "12"),
            evaluator=EvaluatorDefinitionRef("eval-def-1", "7"),
            max_turns=8,
            personas=("developer", "reviewer"),
        )
    )

    assert transport.created_runs[0]["kind"] == "multi_turn_simulation"
    assert transport.created_runs[0]["simulation"] == {
        "max_turns": 8,
        "personas": ["developer", "reviewer"],
    }


def test_get_run_polls_to_terminal_state_with_bounded_backoff() -> None:
    transport = FakeEvaluationTransport()
    transport.run_responses = [
        _run_payload("queued"),
        _run_payload("running"),
        _run_payload("partial"),
    ]
    delays: list[float] = []
    gateway = EvaluationGateway(
        transport,
        poll_policy=PollPolicy(max_attempts=4, initial_delay_seconds=1, multiplier=2),
        sleep=delays.append,
    )

    result = gateway.get_run("run-1")

    assert result.status is EvaluationStatus.PARTIAL
    assert result.error == "one case failed"
    assert result.completed_at == datetime(2026, 7, 26, 20, 1, tzinfo=UTC)
    assert delays == [1, 2]


def test_get_run_stops_after_bounded_attempts() -> None:
    transport = FakeEvaluationTransport()
    transport.run_responses = [_run_payload("running")] * 3
    gateway = EvaluationGateway(
        transport,
        poll_policy=PollPolicy(max_attempts=3, initial_delay_seconds=0),
        sleep=lambda _: None,
    )

    with pytest.raises(EvaluationPollingTimeoutError):
        gateway.get_run("run-1")


def test_iter_output_items_paginates_batch_and_simulation_contracts() -> None:
    transport = FakeEvaluationTransport()
    transport.page_responses = {
        None: {
            "items": [
                {
                    "kind": "batch",
                    "case_id": "case-1",
                    "case_hash": "sha256:case-1",
                    "response_id": "response-1",
                    "scores": [
                        {
                            "metric": "quality",
                            "raw_score": 4,
                            "normalized_score": 0.8,
                            "reason": "correct",
                        }
                    ],
                    "usage": {"input_tokens": 10, "output_tokens": 4},
                    "duration_ms": 120,
                }
            ],
            "continuation_token": "page-2",
        },
        "page-2": {
            "items": [
                {
                    "kind": "multi_turn_simulation",
                    "case_id": "case-2",
                    "case_hash": "sha256:case-2",
                    "response_ids": ["response-2", "response-3"],
                    "scores": [
                        {
                            "metric": "quality",
                            "raw_score": "pass",
                            "normalized_score": 1.0,
                            "reason": "completed",
                        }
                    ],
                    "usage": {
                        "input_tokens": 20,
                        "output_tokens": 8,
                        "cached_tokens": 3,
                    },
                    "trajectory": {
                        "trajectory_id": "trajectory-2",
                        "turn_count": 3,
                        "tool_calls": [
                            {
                                "call_id": "call-1",
                                "name": "search",
                                "status": "completed",
                                "duration_ms": 25,
                            }
                        ],
                    },
                    "duration_ms": 300,
                }
            ],
            "continuation_token": None,
        },
    }
    gateway = EvaluationGateway(transport, page_size=1)

    items = tuple(gateway.iter_output_items("run-1"))

    assert transport.page_calls == [
        ("run-1", None, 1),
        ("run-1", "page-2", 1),
    ]
    assert items[0].response_ids == ("response-1",)
    assert items[1].response_ids == ("response-2", "response-3")
    assert items[1].trajectory is not None
    assert items[1].trajectory.tool_calls[0].name == "search"
    assert isinstance(
        EvaluationOutputPage(items=items, continuation_token=None),
        EvaluationOutputPage,
    )


def test_batch_and_simulation_result_contracts_remain_adapter_types() -> None:
    batch = BatchEvaluationOutput.from_payload(
        {
            "kind": "batch",
            "case_id": "case-1",
            "case_hash": "sha256:case-1",
            "response_id": "response-1",
            "scores": [],
            "usage": {},
            "duration_ms": 10,
        }
    )
    simulation = MultiTurnSimulationOutput.from_payload(
        {
            "kind": "multi_turn_simulation",
            "case_id": "case-2",
            "case_hash": "sha256:case-2",
            "response_ids": ["response-2"],
            "scores": [],
            "usage": {},
            "trajectory": {
                "trajectory_id": "trajectory-1",
                "turn_count": 1,
                "tool_calls": [],
            },
            "duration_ms": 20,
        }
    )

    assert batch.to_evaluation_item().response_ids == ("response-1",)
    assert simulation.to_evaluation_item().trajectory is not None


def test_output_adapter_rejects_raw_structures_as_scores() -> None:
    with pytest.raises(EvaluationSchemaError):
        BatchEvaluationOutput.from_payload(
            {
                "kind": "batch",
                "case_id": "case-1",
                "case_hash": "sha256:case-1",
                "response_id": "response-1",
                "scores": [
                    {
                        "metric": "quality",
                        "raw_score": {"raw_response": "must not escape"},
                        "normalized_score": 0.8,
                    }
                ],
                "usage": {},
            }
        )


def test_output_adapter_rejects_non_finite_normalized_scores() -> None:
    with pytest.raises(EvaluationSchemaError):
        BatchEvaluationOutput.from_payload(
            {
                "kind": "batch",
                "case_id": "case-1",
                "case_hash": "sha256:case-1",
                "response_id": "response-1",
                "scores": [
                    {
                        "metric": "quality",
                        "raw_score": 4,
                        "normalized_score": float("nan"),
                    }
                ],
                "usage": {},
            }
        )


def test_iter_output_items_rejects_repeated_continuation_token() -> None:
    transport = FakeEvaluationTransport()
    transport.page_responses = {
        None: {"items": [], "continuation_token": "same"},
        "same": {"items": [], "continuation_token": "same"},
    }
    gateway = EvaluationGateway(transport)

    with pytest.raises(EvaluationPaginationError):
        tuple(gateway.iter_output_items("run-1"))
