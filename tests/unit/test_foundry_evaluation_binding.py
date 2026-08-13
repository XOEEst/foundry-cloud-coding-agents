import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

import httpx
import openai
import pytest

from foundry_opt.adapters.evaluation import (
    BatchEvaluationRequest,
    EvaluationGateway,
    EvaluationSchemaError,
)
from foundry_opt.adapters.foundry_evaluation import (
    EvaluationAuthenticationError,
    EvaluationAuthorizationError,
    EvaluationConflictError,
    EvaluationNotFoundError,
    EvaluationRateLimitError,
    EvaluationServiceError,
    FoundryEvaluationTransport,
    _case_identity_digest,
    _provider_error,
    _optional_string,
    _usage,
)
from foundry_opt.evaluation import (
    AgentVersionRef,
    DatasetSplit,
    DatasetVersionRef,
    EvaluatorDefinitionRef,
)


def test_empty_provider_error_envelope_is_not_an_error() -> None:
    assert _provider_error({"code": None, "message": None}) is None
    assert _provider_error({"code": "FAILED", "message": None}) == "FAILED"


def test_missing_cached_token_count_defaults_to_zero() -> None:
    assert _usage(
        {
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 4,
                "cached_tokens": None,
            }
        }
    ) == {
        "input_tokens": 10,
        "output_tokens": 4,
        "cached_tokens": 0,
    }


def test_empty_optional_provider_string_is_absent() -> None:
    assert _optional_string("") is None


def test_case_identity_excludes_generated_output_lineage() -> None:
    first = _case_identity_digest(
        {
            "case_id": "case-1",
            "query": "hello",
            "sample.output_text": "first response",
            "response_id": "response-1",
            "agent_version": "draft-1",
            "trace_id": "trace-1",
        },
        item_id="item-1",
        response_ids=["response-1"],
    )
    second = _case_identity_digest(
        {
            "case_id": "case-1",
            "query": "hello",
            "sample.output_text": "second response",
            "response_id": "response-2",
            "agent_version": "draft-2",
            "trace_id": "trace-2",
        },
        item_id="item-2",
        response_ids=["response-2"],
    )

    assert first == second


@dataclass
class FakePage:
    data: list[object]
    has_more: bool = False
    last_id: str | None = None


class FakeOutputItems:
    def __init__(self) -> None:
        self.responses: list[object | Exception] = []
        self.calls: list[dict[str, object]] = []

    def list(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeRuns:
    def __init__(self) -> None:
        self.output_items = FakeOutputItems()
        self.create_responses: list[object | Exception] = []
        self.retrieve_responses: list[object | Exception] = []
        self.create_calls: list[dict[str, object]] = []
        self.retrieve_calls: list[dict[str, object]] = []
        self.list_calls: list[dict[str, object]] = []
        self.list_response: object = FakePage([])

    def create(self, **kwargs: object) -> object:
        self.create_calls.append(kwargs)
        response = self.create_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def retrieve(self, **kwargs: object) -> object:
        self.retrieve_calls.append(kwargs)
        response = self.retrieve_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def list(self, **kwargs: object) -> object:
        self.list_calls.append(kwargs)
        return self.list_response


class FakeEvals:
    def __init__(self) -> None:
        self.runs = FakeRuns()
        self.list_response: object = FakePage([])
        self.create_response: object | Exception | None = None
        self.list_calls: list[dict[str, object]] = []
        self.create_calls: list[dict[str, object]] = []

    def list(self, **kwargs: object) -> object:
        self.list_calls.append(kwargs)
        return self.list_response

    def create(self, **kwargs: object) -> object:
        self.create_calls.append(kwargs)
        if isinstance(self.create_response, Exception):
            raise self.create_response
        assert self.create_response is not None
        return self.create_response


class FakeOpenAIClient:
    def __init__(self) -> None:
        self.evals = FakeEvals()


class FakeProjectClient:
    def __init__(self, openai_client: FakeOpenAIClient) -> None:
        self.openai_client = openai_client

    def get_openai_client(self) -> FakeOpenAIClient:
        return self.openai_client


def _transport() -> tuple[FoundryEvaluationTransport, FakeOpenAIClient]:
    client = FakeOpenAIClient()
    return (
        FoundryEvaluationTransport(
            FakeProjectClient(client),
            project_endpoint=(
                "https://account.services.ai.azure.com/api/projects/project"
            ),
        ),
        client,
    )


def _definition_payload(
    *,
    fingerprint: str = "sha256:definition",
    mode: str = "batch",
    normalization: Mapping[str, object] | None = None,
    include_normalization: bool = True,
) -> dict[str, object]:
    profile: dict[str, object]
    if mode == "batch":
        profile = {
            "batch": {
                "input_messages": {
                    "type": "template",
                    "template": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": {
                                "type": "input_text",
                                "text": "{{item.query}}",
                            },
                        }
                    ],
                }
            }
        }
    else:
        profile = {
            "simulation": {
                "model": "gpt-5-mini",
                "num_conversations": 1,
                "sampling_params": {"temperature": 0.2},
                "data_mapping": {
                    "test_case_description": "test_case_description",
                    "id": "case_id",
                },
            }
        }
    configuration: dict[str, object] = {
        "data_source_config": {
            "type": "custom",
            "item_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        },
        "testing_criteria": [
            {
                "type": "azure_ai_evaluator",
                "name": "quality",
                "evaluator_name": "builtin.coherence",
                "data_mapping": {
                    "query": "{{item.query}}",
                    "response": "{{sample.output_text}}",
                },
            }
        ],
        **profile,
    }
    if include_normalization:
        configuration["normalization"] = (
            dict(normalization)
            if normalization is not None
            else {
                "quality": {
                    "type": "min_max",
                    "minimum": 0,
                    "maximum": 1,
                }
            }
        )
    return {
        "name": f"quality-{mode}",
        "evaluator_type": "azure_ai_evaluator",
        "schema_version": "7",
        "configuration": configuration,
        "fingerprint": fingerprint,
    }


def _created_definition(
    create_call: Mapping[str, object],
    *,
    definition_id: str = "eval-definition",
) -> dict[str, object]:
    return {
        "id": definition_id,
        "name": create_call["name"],
        "metadata": create_call["metadata"],
        "data_source_config": create_call["data_source_config"],
        "testing_criteria": create_call["testing_criteria"],
    }


def _create_definition(
    transport: FoundryEvaluationTransport,
    client: FakeOpenAIClient,
    *,
    mode: str = "batch",
    normalization: Mapping[str, object] | None = None,
    include_normalization: bool = True,
) -> None:
    payload = _definition_payload(
        mode=mode,
        normalization=normalization,
        include_normalization=include_normalization,
    )
    client.evals.create_response = {
        "id": "eval-definition",
        "name": payload["name"],
        "metadata": {},
        "data_source_config": {},
        "testing_criteria": [],
    }

    def create(**kwargs: object) -> object:
        client.evals.create_calls.append(kwargs)
        return _created_definition(kwargs)

    client.evals.create = create  # type: ignore[method-assign]
    transport.create_definition(payload)


def _run_payload(
    metadata: Mapping[str, str],
    *,
    status: str = "queued",
    run_id: str = "evalrun-1",
    eval_id: str = "eval-definition",
    **overrides: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": run_id,
        "eval_id": eval_id,
        "status": status,
        "metadata": dict(metadata),
        "report_url": "https://ai.azure.com/evaluations/evalrun-1",
        "created_at": 1785132000,
        "result_counts": {
            "total": 2,
            "passed": 2,
            "failed": 0,
            "errored": 0,
        },
        "error": None,
    }
    payload.update(overrides)
    return payload


def _batch_run_request() -> dict[str, object]:
    return {
        "kind": "batch",
        "display_name": "candidate development",
        "subject_id": "candidate-1",
        "split": "development",
        "agent": {
            "agent_id": "agent-name",
            "draft_id": "draft-9",
            "version": "3",
        },
        "dataset": {"dataset_id": "development", "version": "12"},
        "evaluator": {
            "definition_id": "eval-definition",
            "version": "7",
        },
    }


def _list_single_item(
    result: Mapping[str, object] | list[Mapping[str, object]],
    *,
    normalization: Mapping[str, object] | None = None,
    include_normalization: bool = True,
    default_completed_status: bool = True,
) -> Mapping[str, object]:
    raw_results = result if isinstance(result, list) else [result]
    provider_results = [
        (
            {"status": "completed", **dict(raw_result)}
            if default_completed_status
            else dict(raw_result)
        )
        for raw_result in raw_results
    ]
    transport, client = _transport()
    _create_definition(
        transport,
        client,
        normalization=normalization,
        include_normalization=include_normalization,
    )

    def create(**kwargs: object) -> object:
        client.evals.runs.create_calls.append(kwargs)
        return _run_payload(kwargs["metadata"])

    client.evals.runs.create = create  # type: ignore[method-assign]
    transport.create_run(_batch_run_request())
    client.evals.runs.output_items.responses = [
        FakePage(
            [
                {
                    "id": "output-1",
                    "run_id": "evalrun-1",
                    "eval_id": "eval-definition",
                    "status": "pass",
                    "datasource_item": {
                        "case_id": "case-1",
                        "case_hash": "sha256:case-1",
                        "response_id": "resp-1",
                    },
                    "results": provider_results,
                    "sample": {
                        "usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 4,
                            "cached_tokens": 0,
                        },
                        "error": None,
                    },
                }
            ]
        )
    ]
    page = transport.list_output_items(
        "evalrun-1",
        continuation_token=None,
        page_size=10,
    )
    return page["items"][0]


def _list_single_score(
    result: Mapping[str, object],
    *,
    normalization: Mapping[str, object] | None = None,
    include_normalization: bool = True,
) -> Mapping[str, object]:
    item = _list_single_item(
        result,
        normalization=normalization,
        include_normalization=include_normalization,
    )
    return item["scores"][0]


def test_create_definition_uses_openai_v1_eval_and_round_trips_binding() -> None:
    transport, client = _transport()
    payload = _definition_payload()

    def create(**kwargs: object) -> object:
        client.evals.create_calls.append(kwargs)
        return _created_definition(kwargs)

    client.evals.create = create  # type: ignore[method-assign]

    result = transport.create_definition(payload)

    assert result == {
        "id": "eval-definition",
        "version": "7",
        "fingerprint": "sha256:definition",
        "portal_url": None,
    }
    call = client.evals.create_calls[0]
    assert call["name"] == "quality-batch"
    assert call["data_source_config"] == payload["configuration"][
        "data_source_config"
    ]
    assert call["testing_criteria"] == payload["configuration"][
        "testing_criteria"
    ]
    metadata = call["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["foundry_opt_fingerprint"] == "sha256:definition"
    assert metadata["foundry_opt_schema_version"] == "7"
    assert "foundry_opt_binding_0" in metadata


def test_find_definition_reuses_exact_fingerprint_and_restores_profile() -> None:
    first, first_client = _transport()
    payload = _definition_payload()

    def create(**kwargs: object) -> object:
        first_client.evals.create_calls.append(kwargs)
        return _created_definition(kwargs)

    first_client.evals.create = create  # type: ignore[method-assign]
    first.create_definition(payload)
    provider_definition = create(**first_client.evals.create_calls[0])

    second, second_client = _transport()
    second_client.evals.list_response = FakePage([provider_definition])
    assert second.find_definition("sha256:definition") == {
        "id": "eval-definition",
        "version": "7",
        "fingerprint": "sha256:definition",
        "portal_url": None,
    }

    second_client.evals.runs.create_responses = [
        _run_payload(
            {
                "foundry_opt_kind": "batch",
                "foundry_opt_subject_id": "candidate-1",
                "foundry_opt_split": "development",
                "foundry_opt_agent_id": "agent-name",
                "foundry_opt_draft_id": "draft-9",
                "foundry_opt_agent_version": "3",
                "foundry_opt_dataset_id": "development",
                "foundry_opt_dataset_version": "12",
                "foundry_opt_evaluator_id": "eval-definition",
                "foundry_opt_evaluator_version": "7",
            }
        )
    ]
    second.create_run(_batch_run_request())
    assert second_client.evals.runs.create_calls


def test_find_definition_reuses_equivalent_duplicate_fingerprint() -> None:
    transport, client = _transport()
    payload = _definition_payload()

    def create(**kwargs: object) -> object:
        client.evals.create_calls.append(kwargs)
        return _created_definition(kwargs)

    client.evals.create = create  # type: ignore[method-assign]
    transport.create_definition(payload)
    provider = create(**client.evals.create_calls[0])
    duplicate = {**provider, "id": "eval-definition-2"}
    client.evals.list_response = FakePage([duplicate, provider])

    assert transport.find_definition("sha256:definition") == {
        "id": "eval-definition",
        "version": "7",
        "fingerprint": "sha256:definition",
        "portal_url": None,
    }


def test_find_definition_rejects_conflicting_duplicate_fingerprint() -> None:
    transport, client = _transport()
    payload = _definition_payload()

    def create(**kwargs: object) -> object:
        client.evals.create_calls.append(kwargs)
        return _created_definition(kwargs)

    client.evals.create = create  # type: ignore[method-assign]
    transport.create_definition(payload)
    provider = create(**client.evals.create_calls[0])
    conflicting = {
        **provider,
        "id": "eval-definition-2",
        "metadata": {
            **provider["metadata"],
            "foundry_opt_schema_version": "8",
        },
    }
    client.evals.list_response = FakePage([provider, conflicting])

    with pytest.raises(EvaluationConflictError):
        transport.find_definition("sha256:definition")


def test_find_definition_skips_unrelated_null_or_malformed_metadata() -> None:
    creator, creator_client = _transport()

    def create(**kwargs: object) -> object:
        creator_client.evals.create_calls.append(kwargs)
        return _created_definition(kwargs)

    creator_client.evals.create = create  # type: ignore[method-assign]
    creator.create_definition(_definition_payload())
    matching = _created_definition(creator_client.evals.create_calls[0])

    transport, client = _transport()
    client.evals.list_response = FakePage(
        [
            {"id": "unrelated-null", "metadata": None},
            {"id": "unrelated-list", "metadata": ["not", "metadata"]},
            {
                "id": "unrelated-other",
                "metadata": {"foundry_opt_fingerprint": "sha256:other"},
            },
            matching,
        ]
    )

    assert transport.find_definition("sha256:definition") == {
        "id": "eval-definition",
        "version": "7",
        "fingerprint": "sha256:definition",
        "portal_url": None,
    }


def test_batch_run_pins_exact_draft_dataset_evaluator_and_context() -> None:
    transport, client = _transport()
    _create_definition(transport, client)

    def create(**kwargs: object) -> object:
        client.evals.runs.create_calls.append(kwargs)
        return _run_payload(kwargs["metadata"])

    client.evals.runs.create = create  # type: ignore[method-assign]

    result = transport.create_run(_batch_run_request())

    call = client.evals.runs.create_calls[0]
    assert call["eval_id"] == "eval-definition"
    assert call["data_source"] == {
        "type": "azure_ai_target_completions",
        "source": {
            "type": "file_id",
            "id": (
                "azureai://accounts/account/projects/project/data/"
                "development/versions/12"
            ),
        },
        "input_messages": _definition_payload()["configuration"]["batch"][
            "input_messages"
        ],
        "target": {
            "type": "azure_ai_agent",
            "name": "agent-name",
            "version": "3",
        },
    }
    assert "extra_body" not in call
    assert result["kind"] == "batch"
    assert result["subject_id"] == "candidate-1"
    assert result["agent"]["draft_id"] == "draft-9"
    assert result["agent"]["version"] == "3"
    assert result["dataset"] == {
        "dataset_id": "development",
        "version": "12",
    }


def test_simulation_personas_are_rejected_when_api_cannot_bind_them() -> None:
    transport, client = _transport()
    _create_definition(transport, client, mode="simulation")
    request = _batch_run_request()
    request["kind"] = "multi_turn_simulation"
    request["display_name"] = "candidate simulation"
    request["split"] = "validation"
    request["simulation"] = {
        "max_turns": 7,
        "personas": ["developer", "reviewer"],
    }

    with pytest.raises(EvaluationSchemaError, match="persona"):
        transport.create_run(request)

    assert client.evals.runs.create_calls == []


def test_batch_binding_accepts_supported_item_reference_shape() -> None:
    transport, client = _transport()
    payload = _definition_payload()
    payload["configuration"]["batch"]["input_messages"] = {
        "type": "item_reference",
        "item_reference": "item.conversation_input",
    }

    def create(**kwargs: object) -> object:
        client.evals.create_calls.append(kwargs)
        return _created_definition(kwargs)

    client.evals.create = create  # type: ignore[method-assign]
    transport.create_definition(payload)

    def create_run(**kwargs: object) -> object:
        client.evals.runs.create_calls.append(kwargs)
        return _run_payload(kwargs["metadata"])

    client.evals.runs.create = create_run  # type: ignore[method-assign]
    transport.create_run(_batch_run_request())

    assert client.evals.runs.create_calls[0]["data_source"][
        "input_messages"
    ] == {
        "type": "item_reference",
        "item_reference": "item.conversation_input",
    }


def test_batch_binding_preserves_complete_responses_message_template() -> None:
    transport, client = _transport()
    payload = _definition_payload()
    template = {
        "type": "template",
        "template": [
            {
                "type": "message",
                "role": "developer",
                "content": {
                    "type": "input_text",
                    "text": "Follow {{item.policy}}.",
                },
            },
            {
                "type": "message",
                "role": "system",
                "content": "Case {{item.case_id}}",
            },
            {
                "type": "message",
                "role": "user",
                "content": {
                    "type": "input_text",
                    "text": "{{item.query}}",
                },
            },
        ],
    }
    payload["configuration"]["batch"]["input_messages"] = template

    def create(**kwargs: object) -> object:
        client.evals.create_calls.append(kwargs)
        return _created_definition(kwargs)

    client.evals.create = create  # type: ignore[method-assign]
    transport.create_definition(payload)

    def create_run(**kwargs: object) -> object:
        client.evals.runs.create_calls.append(kwargs)
        return _run_payload(kwargs["metadata"])

    client.evals.runs.create = create_run  # type: ignore[method-assign]
    transport.create_run(_batch_run_request())

    assert client.evals.runs.create_calls[0]["data_source"][
        "input_messages"
    ] == template


def test_batch_binding_preserves_hosted_invocation_freeform_shape() -> None:
    transport, client = _transport()
    payload = _definition_payload()
    payload["configuration"]["batch"]["input_messages"] = {
        "message": "{{item.query}}",
        "context": {
            "case_id": "{{item.case_id}}",
            "attempt": 1,
        },
    }

    def create(**kwargs: object) -> object:
        client.evals.create_calls.append(kwargs)
        return _created_definition(kwargs)

    client.evals.create = create  # type: ignore[method-assign]
    transport.create_definition(payload)

    def create_run(**kwargs: object) -> object:
        client.evals.runs.create_calls.append(kwargs)
        return _run_payload(kwargs["metadata"])

    client.evals.runs.create = create_run  # type: ignore[method-assign]
    transport.create_run(_batch_run_request())

    assert client.evals.runs.create_calls[0]["data_source"][
        "input_messages"
    ] == {
        "message": "{{item.query}}",
        "context": {
            "case_id": "{{item.case_id}}",
            "attempt": 1,
        },
    }


@pytest.mark.parametrize(
    "input_messages",
    [
        {},
        {"message": "constant without an item reference"},
        {"message": "{{query}}"},
        {"message": "{{item.}}"},
        {"message": object()},
    ],
)
def test_batch_binding_rejects_invalid_invocation_freeform_shape(
    input_messages: Mapping[str, object],
) -> None:
    transport, client = _transport()
    payload = _definition_payload()
    payload["configuration"]["batch"]["input_messages"] = dict(input_messages)

    with pytest.raises(
        EvaluationSchemaError,
        match="input_messages|item reference|JSON",
    ):
        transport.create_definition(payload)

    assert client.evals.create_calls == []


@pytest.mark.parametrize(
    "input_messages",
    [
        {"type": "item_reference"},
        {"type": "item_reference", "item_reference": ""},
        {"type": "template", "template": []},
        {
            "type": "template",
            "template": [
                {
                    "type": "message",
                    "role": "tool",
                    "content": {
                        "type": "input_text",
                        "text": "{{item.query}}",
                    },
                }
            ],
        },
    ],
)
def test_batch_binding_rejects_unsupported_input_messages(
    input_messages: Mapping[str, object],
) -> None:
    transport, client = _transport()
    payload = _definition_payload()
    payload["configuration"]["batch"]["input_messages"] = dict(input_messages)

    with pytest.raises(EvaluationSchemaError, match="input_messages"):
        transport.create_definition(payload)

    assert client.evals.create_calls == []


def test_run_rejects_claimed_evaluator_version_mismatch() -> None:
    transport, client = _transport()
    _create_definition(transport, client)
    request = _batch_run_request()
    request["evaluator"] = {
        "definition_id": "eval-definition",
        "version": "8",
    }

    with pytest.raises(EvaluationSchemaError, match="version"):
        transport.create_run(request)


@pytest.mark.parametrize(
    ("provider_status", "errored", "skipped", "expected"),
    [
        ("queued", 0, 0, "queued"),
        ("in_progress", 0, 0, "running"),
        ("completed", 0, 0, "completed"),
        ("completed", 1, 0, "partial"),
        ("completed", 0, 1, "partial"),
        ("failed", 0, 0, "failed"),
        ("canceled", 0, 0, "cancelled"),
    ],
)
def test_get_run_maps_provider_statuses(
    provider_status: str,
    errored: int,
    skipped: int,
    expected: str,
) -> None:
    transport, client = _transport()
    _create_definition(transport, client)

    def create(**kwargs: object) -> object:
        client.evals.runs.create_calls.append(kwargs)
        return _run_payload(kwargs["metadata"])

    client.evals.runs.create = create  # type: ignore[method-assign]
    created = transport.create_run(_batch_run_request())
    metadata = client.evals.runs.create_calls[0]["metadata"]
    client.evals.runs.retrieve_responses = [
        _run_payload(
            metadata,
            status=provider_status,
            result_counts={
                "total": 2,
                "passed": 1,
                "failed": 0,
                "errored": errored,
                "skipped": skipped,
            },
        )
    ]

    result = transport.get_run(created["run_id"])

    assert result["status"] == expected
    assert client.evals.runs.retrieve_calls == [
        {"eval_id": "eval-definition", "run_id": "evalrun-1"}
    ]


def test_output_pagination_preserves_opaque_token_and_normalizes_batch() -> None:
    transport, client = _transport()
    _create_definition(transport, client)

    def create(**kwargs: object) -> object:
        client.evals.runs.create_calls.append(kwargs)
        return _run_payload(kwargs["metadata"])

    client.evals.runs.create = create  # type: ignore[method-assign]
    transport.create_run(_batch_run_request())
    client.evals.runs.output_items.responses = [
        FakePage(
            [
                {
                    "id": "output-1",
                    "run_id": "evalrun-1",
                    "eval_id": "eval-definition",
                    "status": "pass",
                    "datasource_item": {
                        "case_id": "case-1",
                        "case_hash": "sha256:case-1",
                        "response_id": "resp-1",
                    },
                    "results": [
                        {
                            "status": "completed",
                            "name": "quality",
                            "score": 0.8,
                            "reason": "met rubric",
                        }
                    ],
                    "sample": {
                        "usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 4,
                            "cached_tokens": 2,
                        },
                        "error": None,
                    },
                }
            ],
            has_more=True,
            last_id="opaque+/=token",
        ),
        FakePage([]),
    ]

    first = transport.list_output_items(
        "evalrun-1",
        continuation_token=None,
        page_size=25,
    )
    second = transport.list_output_items(
        "evalrun-1",
        continuation_token=first["continuation_token"],
        page_size=25,
    )

    assert first == {
        "kind": "batch",
        "run_id": "evalrun-1",
        "evaluation_id": "eval-definition",
        "subject_id": "candidate-1",
        "split": "development",
        "agent": {
            "agent_id": "agent-name",
            "draft_id": "draft-9",
            "version": "3",
        },
        "dataset": {"dataset_id": "development", "version": "12"},
        "evaluator": {
            "definition_id": "eval-definition",
            "version": "7",
        },
        "items": [
            {
                "kind": "batch",
                "run_id": "evalrun-1",
                "evaluation_id": "eval-definition",
                "case_id": "case-1",
                "case_hash": "sha256:case-1",
                "response_id": "resp-1",
                "scores": [
                    {
                        "metric": "quality",
                        "raw_score": 0.8,
                        "normalized_score": 0.8,
                        "reason": "met rubric",
                    }
                ],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 4,
                    "cached_tokens": 2,
                },
                "error": None,
                "duration_ms": 0,
            }
        ],
        "continuation_token": "opaque+/=token",
    }
    assert second["continuation_token"] is None
    assert client.evals.runs.output_items.calls[1]["after"] == "opaque+/=token"


def test_provider_normalized_score_is_not_replaced_by_raw_scale() -> None:
    score = _list_single_score(
        {
            "name": "quality",
            "score": 4,
            "normalized_score": 0.75,
            "passed": True,
            "label": "pass",
        },
        include_normalization=False,
    )

    assert score["raw_score"] == 4
    assert score["normalized_score"] == 0.75


def test_completed_grader_error_is_not_persisted_as_a_zero_score() -> None:
    item = _list_single_item(
        {
            "name": "quality",
            "score": 0.0,
            "passed": False,
            "label": "fail",
            "sample": {
                "error": {
                    "message": "An error occurred during grading",
                }
            },
        }
    )

    assert item["scores"] == []
    assert item["error"] == "Evaluator quality errored."


def test_criterion_names_remain_distinct_when_metrics_match() -> None:
    item = _list_single_item(
        [
            {
                "name": "quality-primary",
                "metric": "coherence",
                "score": 0.8,
                "normalized_score": 0.8,
            },
            {
                "name": "quality-guardrail",
                "metric": "coherence",
                "score": 0.6,
                "normalized_score": 0.6,
            },
        ]
    )

    assert [score["metric"] for score in item["scores"]] == [
        "quality-primary",
        "quality-guardrail",
    ]


def test_skipped_evaluator_result_does_not_produce_a_score() -> None:
    item = _list_single_item(
        {
            "status": "skipped",
            "name": "quality",
            "score": 1.0,
            "normalized_score": 1.0,
            "passed": True,
            "label": "pass",
        }
    )

    assert item["scores"] == []
    assert item["error"] is None


def test_errored_evaluator_result_becomes_item_error_not_score() -> None:
    item = _list_single_item(
        {
            "status": "errored",
            "name": "quality",
            "score": 1.0,
            "normalized_score": 1.0,
            "passed": True,
            "label": "pass",
        }
    )

    assert item["scores"] == []
    assert item["error"] == "Evaluator quality errored."


@pytest.mark.parametrize("status", ["omitted", None, ""])
def test_omitted_or_null_result_status_is_completed_when_score_exists(
    status: str | None,
) -> None:
    result: dict[str, object] = {
        "name": "quality",
        "score": 1.0,
        "normalized_score": 1.0,
    }
    if status != "omitted":
        result["status"] = status

    item = _list_single_item(result, default_completed_status=False)

    assert item["scores"][0]["normalized_score"] == 1.0


def test_omitted_result_status_is_completed_when_passed_exists() -> None:
    item = _list_single_item(
        {"name": "quality", "passed": False, "label": "fail"},
        normalization={"quality": {"type": "pass_fail"}},
        default_completed_status=False,
    )

    assert item["scores"][0]["normalized_score"] == 0.0


@pytest.mark.parametrize("status", ["running", "mystery"])
def test_unknown_nonempty_evaluator_result_status_fails_closed(
    status: str,
) -> None:
    with pytest.raises(EvaluationSchemaError, match="status"):
        _list_single_item(
            {
                "status": status,
                "name": "quality",
                "score": 1.0,
                "normalized_score": 1.0,
            }
        )


def test_evaluator_min_max_contract_normalizes_natural_scale() -> None:
    score = _list_single_score(
        {
            "name": "quality",
            "score": 4,
            "passed": True,
            "label": "pass",
        },
        normalization={
            "quality": {
                "type": "min_max",
                "minimum": 1,
                "maximum": 5,
            }
        },
    )

    assert score["raw_score"] == 4
    assert score["normalized_score"] == 0.75


def test_pass_fail_contract_respects_provider_failed_label() -> None:
    score = _list_single_score(
        {
            "name": "quality",
            "score": "needs_work",
            "passed": False,
            "label": "fail",
        },
        normalization={"quality": {"type": "pass_fail"}},
    )

    assert score["raw_score"] == "needs_work"
    assert score["normalized_score"] == 0.0


def test_score_without_normalized_value_or_contract_is_rejected() -> None:
    with pytest.raises(EvaluationSchemaError, match="normalization"):
        _list_single_score(
            {"name": "quality", "score": 4, "passed": True},
            include_normalization=False,
        )


def test_unknown_normalization_contract_is_rejected() -> None:
    with pytest.raises(EvaluationSchemaError, match="unknown normalization"):
        _list_single_score(
            {"name": "quality", "score": 4},
            normalization={"quality": {"type": "provider_magic"}},
        )


@pytest.mark.parametrize(
    "result",
    [
        {"name": "quality", "score": 4, "passed": "yes", "label": "pass"},
        {"name": "quality", "score": 4, "passed": True, "label": "excellent"},
        {"name": "quality", "score": 4, "passed": True, "label": "fail"},
    ],
)
def test_unknown_or_conflicting_provider_outcome_is_rejected(
    result: Mapping[str, object],
) -> None:
    with pytest.raises(EvaluationSchemaError, match="passed|label|conflict"):
        _list_single_score(
            result,
            normalization={"quality": {"type": "pass_fail"}},
        )


def test_output_page_rejects_cross_run_item() -> None:
    transport, client = _transport()
    _create_definition(transport, client)

    def create(**kwargs: object) -> object:
        client.evals.runs.create_calls.append(kwargs)
        return _run_payload(kwargs["metadata"])

    client.evals.runs.create = create  # type: ignore[method-assign]
    transport.create_run(_batch_run_request())
    client.evals.runs.output_items.responses = [
        FakePage(
            [
                {
                    "id": "output-1",
                    "run_id": "other-run",
                    "eval_id": "eval-definition",
                }
            ]
        )
    ]

    with pytest.raises(EvaluationSchemaError, match="cross-run"):
        transport.list_output_items(
            "evalrun-1",
            continuation_token=None,
            page_size=25,
        )


def test_query_only_output_derives_stable_case_identity() -> None:
    transport, client = _transport()
    _create_definition(transport, client)

    def create(**kwargs: object) -> object:
        client.evals.runs.create_calls.append(kwargs)
        return _run_payload(kwargs["metadata"])

    client.evals.runs.create = create  # type: ignore[method-assign]
    transport.create_run(_batch_run_request())
    datasource_item = {
        "query": "How do I reverse a string?",
        "response_id": "resp-query-only",
    }
    client.evals.runs.output_items.responses = [
        FakePage(
            [
                {
                    "id": "output-query-only",
                    "run_id": "evalrun-1",
                    "eval_id": "eval-definition",
                    "datasource_item_id": 7,
                    "status": "pass",
                    "datasource_item": datasource_item,
                    "results": [
                        {
                            "status": "completed",
                            "name": "quality",
                            "score": 0.8,
                        }
                    ],
                    "sample": {
                        "usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 4,
                            "cached_tokens": 0,
                        },
                        "error": None,
                    },
                }
            ]
        )
    ]

    page = transport.list_output_items(
        "evalrun-1",
        continuation_token=None,
        page_size=10,
    )

    identity = {"datasource_item": {"query": datasource_item["query"]}}
    digest = hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    item = page["items"][0]
    assert item["case_id"] == "item:7"
    assert item["case_hash"] == f"sha256:{digest}"


def test_simulation_output_remains_separately_tagged_with_trajectory() -> None:
    transport, client = _transport()
    _create_definition(transport, client, mode="simulation")
    transport._runs["evalrun-1"] = {
        "kind": "multi_turn_simulation",
        "subject_id": "candidate-1",
        "split": "development",
        "agent": {
            "agent_id": "agent-name",
            "draft_id": "draft-9",
            "version": "3",
        },
        "dataset": {"dataset_id": "development", "version": "12"},
        "evaluator": {
            "definition_id": "eval-definition",
            "version": "7",
        },
        "simulation": {
            "max_turns": 5,
            "personas": ["developer"],
        },
    }
    client.evals.runs.output_items.responses = [
        FakePage(
            [
                {
                    "id": "conversation-1",
                    "run_id": "evalrun-1",
                    "eval_id": "eval-definition",
                    "status": "pass",
                    "datasource_item": {
                        "case_id": "case-1",
                        "case_hash": "sha256:case-1",
                        "response_ids": ["resp-1", "resp-2"],
                        "messages": [
                            {"role": "user", "content": "first"},
                            {
                                "role": "assistant",
                                "content": "second",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "name": "search",
                                        "status": "completed",
                                    }
                                ],
                            },
                        ],
                    },
                    "results": [
                        {
                            "status": "completed",
                            "name": "quality",
                            "score": 0.9,
                        }
                    ],
                    "sample": {
                        "usage": {
                            "prompt_tokens": 20,
                            "completion_tokens": 8,
                            "cached_tokens": 0,
                        },
                        "error": None,
                    },
                }
            ]
        )
    ]

    page = transport.list_output_items(
        "evalrun-1",
        continuation_token=None,
        page_size=10,
    )

    item = page["items"][0]
    assert item["kind"] == "multi_turn_simulation"
    assert item["response_ids"] == ["resp-1", "resp-2"]
    assert item["trajectory"] == {
        "trajectory_id": "conversation-1",
        "turn_count": 2,
        "tool_calls": [
            {
                "call_id": "call-1",
                "name": "search",
                "status": "completed",
                "duration_ms": None,
            }
        ],
    }


def test_gateway_subject_and_split_are_metadata_not_provider_fields() -> None:
    transport, client = _transport()
    _create_definition(transport, client)

    def create(**kwargs: object) -> object:
        client.evals.runs.create_calls.append(kwargs)
        return _run_payload(kwargs["metadata"])

    client.evals.runs.create = create  # type: ignore[method-assign]
    run = EvaluationGateway(transport).create_run(
        BatchEvaluationRequest(
            display_name="candidate validation",
            subject_id="candidate-42",
            split=DatasetSplit.VALIDATION,
            agent=AgentVersionRef("agent-name", "draft-9", "3"),
            dataset=DatasetVersionRef("development", "12"),
            evaluator=EvaluatorDefinitionRef("eval-definition", "7"),
        )
    )

    call = client.evals.runs.create_calls[0]
    assert "subject_id" not in call
    assert "split" not in call
    assert call["metadata"]["foundry_opt_subject_id"] == "candidate-42"
    assert call["metadata"]["foundry_opt_split"] == "validation"
    assert run.subject_id == "candidate-42"
    assert run.split is DatasetSplit.VALIDATION


def test_gateway_idempotency_key_is_exact_run_metadata() -> None:
    transport, client = _transport()
    _create_definition(transport, client)
    key = "a" * 64

    def create(**kwargs: object) -> object:
        client.evals.runs.create_calls.append(kwargs)
        return _run_payload(kwargs["metadata"])

    client.evals.runs.create = create  # type: ignore[method-assign]
    request = {**_batch_run_request(), "idempotency_key": key}

    transport.create_run(request)

    metadata = client.evals.runs.create_calls[0]["metadata"]
    assert metadata["foundry_opt_idempotency_key"] == key
    assert client.evals.runs.create_calls[0]["extra_headers"] == {
        "Idempotency-Key": key
    }


def test_find_run_reconciles_only_exact_idempotency_binding() -> None:
    transport, client = _transport()
    _create_definition(transport, client)
    key = "a" * 64
    request = {**_batch_run_request(), "idempotency_key": key}
    exact_metadata = {
        "foundry_opt_kind": "batch",
        "foundry_opt_subject_id": "candidate-1",
        "foundry_opt_split": "development",
        "foundry_opt_agent_id": "agent-name",
        "foundry_opt_draft_id": "draft-9",
        "foundry_opt_agent_version": "3",
        "foundry_opt_dataset_id": "development",
        "foundry_opt_dataset_version": "12",
        "foundry_opt_evaluator_id": "eval-definition",
        "foundry_opt_evaluator_version": "7",
        "foundry_opt_idempotency_key": key,
    }
    client.evals.runs.list_response = FakePage(
        [
            _run_payload(
                exact_metadata,
                name="candidate development",
            )
        ]
    )

    exact = transport.find_run(request)
    conflicting = transport.find_run(
        {**request, "idempotency_key": "b" * 64}
    )

    assert exact is not None
    assert exact["run_id"] == "evalrun-1"
    assert conflicting is None
    assert client.evals.runs.create_calls == []


def test_idempotent_run_fails_closed_when_reconcile_api_is_unavailable() -> None:
    transport, client = _transport()
    _create_definition(transport, client)
    client.evals.runs.list = None  # type: ignore[method-assign]

    with pytest.raises(EvaluationConflictError, match="reconciliation"):
        transport.find_run(
            {
                **_batch_run_request(),
                "idempotency_key": "a" * 64,
            }
        )


def test_run_response_rejects_context_mismatch() -> None:
    transport, client = _transport()
    _create_definition(transport, client)

    def create(**kwargs: object) -> object:
        metadata = dict(kwargs["metadata"])
        metadata["foundry_opt_draft_id"] = "draft-other"
        return _run_payload(metadata)

    client.evals.runs.create = create  # type: ignore[method-assign]

    with pytest.raises(EvaluationSchemaError, match="draft_id"):
        transport.create_run(_batch_run_request())


@pytest.mark.parametrize(
    ("exception_type", "status", "expected_type"),
    [
        (openai.AuthenticationError, 401, EvaluationAuthenticationError),
        (openai.PermissionDeniedError, 403, EvaluationAuthorizationError),
        (openai.NotFoundError, 404, EvaluationNotFoundError),
        (openai.ConflictError, 409, EvaluationConflictError),
        (openai.RateLimitError, 429, EvaluationRateLimitError),
        (openai.InternalServerError, 500, EvaluationServiceError),
    ],
)
def test_provider_http_errors_are_typed(
    exception_type: type[openai.APIStatusError],
    status: int,
    expected_type: type[Exception],
) -> None:
    transport, client = _transport()
    response = httpx.Response(
        status,
        request=httpx.Request("POST", "https://example.invalid/openai/v1/evals"),
    )
    client.evals.create_response = exception_type(
        "provider failure",
        response=response,
        body={"error": {"message": "provider failure"}},
    )

    with pytest.raises(expected_type):
        transport.create_definition(_definition_payload())


def test_malformed_provider_payload_is_rejected() -> None:
    transport, client = _transport()
    client.evals.create_response = {"id": ["not", "an", "identifier"]}

    with pytest.raises(EvaluationSchemaError):
        transport.create_definition(_definition_payload())
