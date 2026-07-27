from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from typing import Any, TypeVar
from urllib.parse import quote, unquote, urlparse

import openai

from foundry_opt.adapters.evaluation import (
    EvaluationAdapterError,
    EvaluationSchemaError,
    EvaluationTransport,
    RetryableEvaluationError,
)


class EvaluationProviderError(EvaluationAdapterError):
    """Base class for stable Microsoft Foundry evaluation API failures."""


class EvaluationAuthenticationError(EvaluationProviderError):
    pass


class EvaluationAuthorizationError(EvaluationProviderError):
    pass


class EvaluationNotFoundError(EvaluationProviderError):
    pass


class EvaluationConflictError(EvaluationProviderError):
    pass


class EvaluationRateLimitError(RetryableEvaluationError):
    pass


class EvaluationServiceError(RetryableEvaluationError):
    pass


_T = TypeVar("_T")
_METADATA_PREFIX = "foundry_opt_"
_BINDING_CHUNK_SIZE = 400
_MAX_BINDING_CHUNKS = 10
_MAX_DEFINITION_PAGES = 1000


@dataclass(frozen=True)
class _DefinitionBinding:
    version: str
    profiles: Mapping[str, object]


class FoundryEvaluationTransport(EvaluationTransport):
    """Bind the strict evaluation adapter to Foundry's OpenAI v1 Evals API."""

    def __init__(
        self,
        project_client: object,
        *,
        project_endpoint: str,
    ) -> None:
        get_openai_client = getattr(project_client, "get_openai_client", None)
        if not callable(get_openai_client):
            raise TypeError(
                "project_client must expose get_openai_client()."
            )
        self._client = get_openai_client()
        self._account, self._project = _parse_project_endpoint(
            project_endpoint
        )
        self._bindings: dict[str, _DefinitionBinding] = {}
        self._runs: dict[str, dict[str, object]] = {}

    def find_definition(
        self,
        fingerprint: str,
    ) -> Mapping[str, object] | None:
        matches: list[dict[str, object]] = []
        continuation: str | None = None
        seen: set[str] = set()
        for _ in range(_MAX_DEFINITION_PAGES):
            kwargs: dict[str, object] = {"limit": 100, "order": "desc"}
            if continuation is not None:
                kwargs["after"] = continuation
            page = _provider_call(lambda: self._client.evals.list(**kwargs))
            for raw_definition in _page_data(page):
                definition = _provider_mapping(
                    raw_definition,
                    "evaluation definition",
                )
                raw_metadata = definition.get("metadata")
                if (
                    not isinstance(raw_metadata, Mapping)
                    or raw_metadata.get(
                        f"{_METADATA_PREFIX}fingerprint"
                    )
                    != fingerprint
                ):
                    continue
                matches.append(self._definition_from_provider(definition))
            continuation = _next_token(page)
            if continuation is None:
                break
            if continuation in seen:
                raise EvaluationConflictError(
                    "Foundry evaluation definition pagination repeated a token."
                )
            seen.add(continuation)
        else:
            raise EvaluationConflictError(
                "Foundry evaluation definition pagination exceeded its bound."
            )
        if len(matches) > 1:
            raise EvaluationConflictError(
                "Multiple Foundry evaluation definitions have the same "
                "binding fingerprint."
            )
        return matches[0] if matches else None

    def create_definition(
        self,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        name = _required_string(payload.get("name"), "definition name")
        evaluator_type = _required_string(
            payload.get("evaluator_type"),
            "evaluator_type",
        )
        schema_version = _required_string(
            payload.get("schema_version"),
            "schema_version",
        )
        fingerprint = _required_string(
            payload.get("fingerprint"),
            "fingerprint",
        )
        configuration = _configuration(payload.get("configuration"))
        metadata = {
            f"{_METADATA_PREFIX}fingerprint": fingerprint,
            f"{_METADATA_PREFIX}schema_version": schema_version,
            f"{_METADATA_PREFIX}evaluator_type": evaluator_type,
            **_binding_metadata(configuration["binding"]),
        }
        response = _provider_call(
            lambda: self._client.evals.create(
                name=name,
                data_source_config=configuration["data_source_config"],
                testing_criteria=configuration["testing_criteria"],
                metadata=metadata,
            )
        )
        definition = self._definition_from_provider(
            _provider_mapping(response, "evaluation definition")
        )
        if definition["fingerprint"] != fingerprint:
            raise EvaluationSchemaError(
                "Foundry created an evaluation definition with a conflicting "
                "fingerprint."
            )
        return definition

    def create_run(
        self,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        context = _run_context(payload)
        evaluation_id = _reference_value(
            context["evaluator"],
            "definition_id",
            "evaluator definition_id",
        )
        binding = self._bindings.get(evaluation_id)
        if binding is None:
            raise EvaluationSchemaError(
                "The Foundry evaluation definition binding was not loaded."
            )
        requested_version = _reference_value(
            context["evaluator"],
            "version",
            "evaluator version",
        )
        if requested_version != binding.version:
            raise EvaluationSchemaError(
                "The requested evaluator version conflicts with the loaded "
                "Foundry evaluation definition version."
            )
        kind = _required_string(context["kind"], "evaluation kind")
        profile = binding.profiles.get(kind)
        if not isinstance(profile, Mapping):
            raise EvaluationSchemaError(
                f"The Foundry evaluation definition does not support {kind}."
            )
        metadata = _run_metadata(context)
        data_source = self._run_data_source(context, profile)
        kwargs: dict[str, object] = {
            "eval_id": evaluation_id,
            "name": _required_string(
                payload.get("display_name"),
                "run display_name",
            ),
            "data_source": data_source,
            "metadata": metadata,
        }
        if kind == "multi_turn_simulation":
            kwargs["extra_body"] = {"evaluation_level": "conversation"}
        response = _provider_call(
            lambda: self._client.evals.runs.create(**kwargs)
        )
        normalized = self._normalize_run(
            _provider_mapping(response, "evaluation run"),
            context=context,
        )
        run_id = _required_string(normalized.get("run_id"), "run_id")
        existing = self._runs.get(run_id)
        if existing is not None and existing != context:
            raise EvaluationSchemaError(
                "Foundry reused an evaluation run ID with conflicting context."
            )
        self._runs[run_id] = context
        return normalized

    def get_run(self, run_id: str) -> Mapping[str, object]:
        context = self._retained_context(run_id)
        evaluation_id = _reference_value(
            context["evaluator"],
            "definition_id",
            "evaluator definition_id",
        )
        response = _provider_call(
            lambda: self._client.evals.runs.retrieve(
                eval_id=evaluation_id,
                run_id=run_id,
            )
        )
        return self._normalize_run(
            _provider_mapping(response, "evaluation run"),
            context=context,
            expected_run_id=run_id,
        )

    def list_output_items(
        self,
        run_id: str,
        *,
        continuation_token: str | None,
        page_size: int,
    ) -> Mapping[str, object]:
        context = self._retained_context(run_id)
        evaluation_id = _reference_value(
            context["evaluator"],
            "definition_id",
            "evaluator definition_id",
        )
        kwargs: dict[str, object] = {
            "eval_id": evaluation_id,
            "run_id": run_id,
            "limit": page_size,
            "order": "asc",
        }
        if continuation_token is not None:
            kwargs["after"] = continuation_token
        page = _provider_call(
            lambda: self._client.evals.runs.output_items.list(**kwargs)
        )
        items = [
            self._normalize_output_item(
                _provider_mapping(raw_item, "evaluation output item"),
                context=context,
                run_id=run_id,
                evaluation_id=evaluation_id,
                normalization=self._normalization_for(evaluation_id),
            )
            for raw_item in _page_data(page)
        ]
        return {
            **_public_context(context),
            "run_id": run_id,
            "evaluation_id": evaluation_id,
            "items": items,
            "continuation_token": _next_token(page),
        }

    def _definition_from_provider(
        self,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        definition_id = _required_string(payload.get("id"), "evaluation id")
        metadata = _metadata(payload)
        fingerprint = _required_string(
            metadata.get(f"{_METADATA_PREFIX}fingerprint"),
            "evaluation fingerprint metadata",
        )
        version = _required_string(
            metadata.get(f"{_METADATA_PREFIX}schema_version"),
            "evaluation schema version metadata",
        )
        binding = _binding_from_metadata(metadata)
        self._bindings[definition_id] = _DefinitionBinding(
            version=version,
            profiles=binding,
        )
        return {
            "id": definition_id,
            "version": version,
            "fingerprint": fingerprint,
            "portal_url": None,
        }

    def _run_data_source(
        self,
        context: Mapping[str, object],
        profile: Mapping[str, object],
    ) -> dict[str, object]:
        agent = _required_mapping(context.get("agent"), "agent")
        dataset = _required_mapping(context.get("dataset"), "dataset")
        source = {
            "type": "file_id",
            "id": self._dataset_version_uri(dataset),
        }
        target = {
            "type": "azure_ai_agent",
            "name": _reference_value(agent, "agent_id", "agent_id"),
            "version": _reference_value(agent, "version", "agent version"),
        }
        kind = _required_string(context.get("kind"), "evaluation kind")
        if kind == "batch":
            return {
                "type": "azure_ai_target_completions",
                "source": source,
                "input_messages": _required_mapping(
                    profile.get("input_messages"),
                    "batch input_messages",
                ),
                "target": target,
            }
        simulation = _required_mapping(
            context.get("simulation"),
            "simulation",
        )
        item_generation_params = {
            "type": "conversation_gen_preview",
            "model": _required_string(
                profile.get("model"),
                "simulation model",
            ),
            "num_conversations": _positive_integer(
                profile.get("num_conversations"),
                "simulation num_conversations",
            ),
            "max_turns": _positive_integer(
                simulation.get("max_turns"),
                "simulation max_turns",
            ),
            "sampling_params": _required_mapping(
                profile.get("sampling_params"),
                "simulation sampling_params",
            ),
            "data_mapping": _required_mapping(
                profile.get("data_mapping"),
                "simulation data_mapping",
            ),
        }
        return {
            "type": "azure_ai_target_completions",
            "source": source,
            "target": target,
            "item_generation_params": item_generation_params,
        }

    def _dataset_version_uri(
        self,
        dataset: Mapping[str, object],
    ) -> str:
        dataset_id = _reference_value(dataset, "dataset_id", "dataset_id")
        version = _reference_value(dataset, "version", "dataset version")
        if dataset_id.startswith("azureai://"):
            suffix = f"/versions/{quote(version, safe='')}"
            if not dataset_id.endswith(suffix):
                raise EvaluationSchemaError(
                    "The Foundry dataset URI conflicts with its pinned version."
                )
            return dataset_id
        return (
            f"azureai://accounts/{quote(self._account, safe='')}/projects/"
            f"{quote(self._project, safe='')}/data/"
            f"{quote(dataset_id, safe='')}/versions/"
            f"{quote(version, safe='')}"
        )

    def _normalize_run(
        self,
        payload: Mapping[str, object],
        *,
        context: Mapping[str, object],
        expected_run_id: str | None = None,
    ) -> dict[str, object]:
        run_id = _required_string(payload.get("id"), "provider run id")
        if expected_run_id is not None and run_id != expected_run_id:
            raise EvaluationSchemaError(
                "Foundry returned a different evaluation run."
            )
        evaluation_id = _required_string(
            payload.get("eval_id"),
            "provider evaluation id",
        )
        expected_evaluation_id = _reference_value(
            context["evaluator"],
            "definition_id",
            "evaluator definition_id",
        )
        if evaluation_id != expected_evaluation_id:
            raise EvaluationSchemaError(
                "Foundry returned a run from a different evaluation."
            )
        _validate_run_metadata(_metadata(payload), context)
        provider_status = _required_string(
            payload.get("status"),
            "provider run status",
        )
        status = _map_status(provider_status, payload)
        error = _provider_error(payload.get("error"))
        return {
            **_public_context(context),
            "run_id": run_id,
            "evaluation_id": evaluation_id,
            "status": status,
            "portal_url": _optional_string(payload.get("report_url")),
            "started_at": _timestamp(payload.get("created_at")),
            "completed_at": None,
            "error": error,
        }

    def _normalize_output_item(
        self,
        payload: Mapping[str, object],
        *,
        context: Mapping[str, object],
        run_id: str,
        evaluation_id: str,
        normalization: Mapping[str, object],
    ) -> dict[str, object]:
        provider_run_id = _required_string(
            payload.get("run_id"),
            "output run_id",
        )
        if provider_run_id != run_id:
            raise EvaluationSchemaError(
                "Foundry returned a cross-run evaluation output item."
            )
        provider_evaluation_id = _required_string(
            payload.get("eval_id"),
            "output eval_id",
        )
        if provider_evaluation_id != evaluation_id:
            raise EvaluationSchemaError(
                "Foundry returned a cross-evaluation output item."
            )
        item_id = _required_string(payload.get("id"), "output item id")
        datasource = _required_mapping(
            payload.get("datasource_item"),
            "output datasource_item",
        )
        sample = _required_mapping(payload.get("sample"), "output sample")
        usage = _usage(sample)
        scores = _scores(payload.get("results"), normalization)
        kind = _required_string(context.get("kind"), "evaluation kind")
        common: dict[str, object] = {
            "kind": kind,
            "run_id": run_id,
            "evaluation_id": evaluation_id,
            "case_id": _case_id(datasource),
            "case_hash": _required_string(
                datasource.get("case_hash"),
                "output case_hash",
            ),
            "scores": scores,
            "usage": usage,
            "error": _provider_error(sample.get("error")),
            # The v1 output item does not expose elapsed time.
            "duration_ms": 0,
        }
        if kind == "batch":
            common["response_id"] = _batch_response_id(datasource)
            return common
        response_ids = _simulation_response_ids(datasource, item_id)
        common["response_ids"] = response_ids
        common["trajectory"] = _trajectory(datasource, item_id)
        return common

    def _retained_context(self, run_id: str) -> dict[str, object]:
        try:
            return self._runs[run_id]
        except KeyError:
            raise EvaluationSchemaError(
                "The Foundry evaluation run has no retained pinned context."
            ) from None

    def _normalization_for(
        self,
        evaluation_id: str,
    ) -> Mapping[str, object]:
        binding = self._bindings.get(evaluation_id)
        if binding is None:
            raise EvaluationSchemaError(
                "The Foundry evaluation definition binding was not loaded."
            )
        normalization = binding.profiles.get("normalization", {})
        return _required_mapping(
            normalization,
            "evaluation normalization binding",
        )


def _configuration(value: object) -> dict[str, object]:
    configuration = _required_mapping(value, "definition configuration")
    data_source_config = _required_mapping(
        configuration.get("data_source_config"),
        "data_source_config",
    )
    testing_criteria = _required_list(
        configuration.get("testing_criteria"),
        "testing_criteria",
    )
    if not testing_criteria:
        raise EvaluationSchemaError(
            "At least one Foundry testing criterion is required."
        )
    binding: dict[str, object] = {}
    if "batch" in configuration:
        batch = _required_mapping(configuration["batch"], "batch binding")
        binding["batch"] = {
            "input_messages": _required_mapping(
                batch.get("input_messages"),
                "batch input_messages",
            )
        }
    if "simulation" in configuration:
        simulation = _required_mapping(
            configuration["simulation"],
            "simulation binding",
        )
        binding["multi_turn_simulation"] = {
            "model": _required_string(
                simulation.get("model"),
                "simulation model",
            ),
            "num_conversations": _positive_integer(
                simulation.get("num_conversations"),
                "simulation num_conversations",
            ),
            "sampling_params": _required_mapping(
                simulation.get("sampling_params"),
                "simulation sampling_params",
            ),
            "data_mapping": _required_mapping(
                simulation.get("data_mapping"),
                "simulation data_mapping",
            ),
        }
    if not binding:
        raise EvaluationSchemaError(
            "Definition configuration must include batch or simulation binding."
        )
    normalization = configuration.get("normalization", {})
    binding["normalization"] = dict(
        _required_mapping(normalization, "normalization")
    )
    return {
        "data_source_config": _json_value(data_source_config),
        "testing_criteria": _json_value(testing_criteria),
        "binding": _json_value(binding),
    }


def _binding_metadata(binding: object) -> dict[str, str]:
    encoded = json.dumps(
        _json_value(binding),
        sort_keys=True,
        separators=(",", ":"),
    )
    chunks = [
        encoded[index : index + _BINDING_CHUNK_SIZE]
        for index in range(0, len(encoded), _BINDING_CHUNK_SIZE)
    ]
    if not chunks or len(chunks) > _MAX_BINDING_CHUNKS:
        raise EvaluationSchemaError(
            "Foundry evaluation run binding is too large for eval metadata."
        )
    metadata = {f"{_METADATA_PREFIX}binding_count": str(len(chunks))}
    metadata.update(
        {
            f"{_METADATA_PREFIX}binding_{index}": chunk
            for index, chunk in enumerate(chunks)
        }
    )
    return metadata


def _binding_from_metadata(metadata: Mapping[str, str]) -> dict[str, object]:
    count = _positive_integer(
        metadata.get(f"{_METADATA_PREFIX}binding_count"),
        "binding metadata count",
    )
    if count > _MAX_BINDING_CHUNKS:
        raise EvaluationSchemaError(
            "Foundry evaluation binding metadata exceeds its chunk bound."
        )
    chunks = []
    for index in range(count):
        chunks.append(
            _required_string(
                metadata.get(f"{_METADATA_PREFIX}binding_{index}"),
                f"binding metadata chunk {index}",
            )
        )
    try:
        decoded = json.loads("".join(chunks))
    except json.JSONDecodeError as error:
        raise EvaluationSchemaError(
            "Foundry evaluation binding metadata is malformed."
        ) from error
    binding = _required_mapping(decoded, "evaluation binding metadata")
    return dict(binding)


def _run_context(payload: Mapping[str, object]) -> dict[str, object]:
    kind = _required_string(payload.get("kind"), "evaluation kind")
    if kind not in {"batch", "multi_turn_simulation"}:
        raise EvaluationSchemaError(
            f"Unsupported Foundry evaluation kind: {kind}."
        )
    context: dict[str, object] = {
        "kind": kind,
        "subject_id": _required_string(
            payload.get("subject_id"),
            "subject_id",
        ),
        "split": _required_string(payload.get("split"), "split"),
        "agent": dict(_required_mapping(payload.get("agent"), "agent")),
        "dataset": dict(_required_mapping(payload.get("dataset"), "dataset")),
        "evaluator": dict(
            _required_mapping(payload.get("evaluator"), "evaluator")
        ),
    }
    if kind == "multi_turn_simulation":
        simulation = _required_mapping(
            payload.get("simulation"),
            "simulation",
        )
        personas = _required_list(
            simulation.get("personas"),
            "simulation personas",
        )
        if not personas:
            raise EvaluationSchemaError(
                "At least one simulation persona is required."
            )
        context["simulation"] = {
            "max_turns": _positive_integer(
                simulation.get("max_turns"),
                "simulation max_turns",
            ),
            "personas": [
                _required_string(persona, "simulation persona")
                for persona in personas
            ],
        }
    return context


def _run_metadata(context: Mapping[str, object]) -> dict[str, str]:
    agent = _required_mapping(context.get("agent"), "agent")
    dataset = _required_mapping(context.get("dataset"), "dataset")
    evaluator = _required_mapping(context.get("evaluator"), "evaluator")
    metadata = {
        f"{_METADATA_PREFIX}kind": _required_string(
            context.get("kind"),
            "kind",
        ),
        f"{_METADATA_PREFIX}subject_id": _required_string(
            context.get("subject_id"),
            "subject_id",
        ),
        f"{_METADATA_PREFIX}split": _required_string(
            context.get("split"),
            "split",
        ),
        f"{_METADATA_PREFIX}agent_id": _reference_value(
            agent,
            "agent_id",
            "agent_id",
        ),
        f"{_METADATA_PREFIX}draft_id": _reference_value(
            agent,
            "draft_id",
            "draft_id",
        ),
        f"{_METADATA_PREFIX}agent_version": _reference_value(
            agent,
            "version",
            "agent version",
        ),
        f"{_METADATA_PREFIX}dataset_id": _reference_value(
            dataset,
            "dataset_id",
            "dataset_id",
        ),
        f"{_METADATA_PREFIX}dataset_version": _reference_value(
            dataset,
            "version",
            "dataset version",
        ),
        f"{_METADATA_PREFIX}evaluator_id": _reference_value(
            evaluator,
            "definition_id",
            "evaluator definition_id",
        ),
        f"{_METADATA_PREFIX}evaluator_version": _reference_value(
            evaluator,
            "version",
            "evaluator version",
        ),
    }
    simulation = context.get("simulation")
    if simulation is not None:
        simulation_mapping = _required_mapping(simulation, "simulation")
        metadata[f"{_METADATA_PREFIX}personas"] = json.dumps(
            simulation_mapping["personas"],
            separators=(",", ":"),
        )
    return metadata


def _validate_run_metadata(
    metadata: Mapping[str, str],
    context: Mapping[str, object],
) -> None:
    expected = _run_metadata(context)
    for field, expected_value in expected.items():
        supplied = metadata.get(field)
        if supplied != expected_value:
            public_field = field.removeprefix(_METADATA_PREFIX)
            raise EvaluationSchemaError(
                f"Foundry run metadata {public_field} conflicts with pinned "
                "request context."
            )


def _public_context(context: Mapping[str, object]) -> dict[str, object]:
    return {
        "kind": context["kind"],
        "subject_id": context["subject_id"],
        "split": context["split"],
        "agent": dict(_required_mapping(context["agent"], "agent")),
        "dataset": dict(_required_mapping(context["dataset"], "dataset")),
        "evaluator": dict(
            _required_mapping(context["evaluator"], "evaluator")
        ),
    }


def _map_status(
    provider_status: str,
    payload: Mapping[str, object],
) -> str:
    if provider_status == "queued":
        return "queued"
    if provider_status in {"in_progress", "running"}:
        return "running"
    if provider_status == "completed":
        counts = _required_mapping(
            payload.get("result_counts"),
            "result_counts",
        )
        errored = _nonnegative_integer(
            counts.get("errored"),
            "result_counts.errored",
        )
        return "partial" if errored else "completed"
    if provider_status == "failed":
        return "failed"
    if provider_status in {"canceled", "cancelled"}:
        return "cancelled"
    raise EvaluationSchemaError(
        f"Unsupported Foundry evaluation status: {provider_status}."
    )


def _scores(
    value: object,
    normalization: Mapping[str, object],
) -> list[dict[str, object]]:
    results = _required_list(value, "output results")
    scores = []
    for raw_result in results:
        result = _provider_mapping(raw_result, "evaluator result")
        metric = _required_string(
            result.get("metric", result.get("name")),
            "evaluator metric",
        )
        raw_score = result.get("score")
        if raw_score is not None and not isinstance(
            raw_score,
            (bool, int, float, str),
        ):
            raise EvaluationSchemaError(
                "Foundry evaluator score is not scalar."
            )
        outcome = _provider_outcome(result)
        normalized = _normalized_score(
            result.get("normalized_score"),
            raw_score=raw_score,
            outcome=outcome,
            contract=normalization.get(metric),
            metric=metric,
        )
        scores.append(
            {
                "metric": metric,
                "raw_score": raw_score,
                "normalized_score": normalized,
                "reason": _optional_string(result.get("reason")),
            }
        )
    return scores


def _provider_outcome(result: Mapping[str, object]) -> bool | None:
    raw_passed = result.get("passed")
    passed: bool | None
    if raw_passed is None:
        passed = None
    elif isinstance(raw_passed, bool):
        passed = raw_passed
    else:
        raise EvaluationSchemaError(
            "Foundry evaluator passed must be a boolean."
        )
    raw_label = result.get("label")
    label: bool | None
    if raw_label is None:
        label = None
    elif isinstance(raw_label, str):
        lowered = raw_label.lower()
        if lowered in {"pass", "passed"}:
            label = True
        elif lowered in {"fail", "failed"}:
            label = False
        else:
            raise EvaluationSchemaError(
                "Foundry evaluator label has unknown pass/fail semantics."
            )
    else:
        raise EvaluationSchemaError(
            "Foundry evaluator label must be a string."
        )
    if passed is not None and label is not None and passed != label:
        raise EvaluationSchemaError(
            "Foundry evaluator passed and label semantics conflict."
        )
    return passed if passed is not None else label


def _normalized_score(
    provider_value: object,
    *,
    raw_score: object,
    outcome: bool | None,
    contract: object,
    metric: str,
) -> float:
    if provider_value is not None:
        return _unit_interval(provider_value, f"{metric} normalized_score")
    if contract is None:
        raise EvaluationSchemaError(
            f"Foundry evaluator {metric} omitted normalized_score and has no "
            "normalization contract."
        )
    contract_mapping = _required_mapping(
        contract,
        f"{metric} normalization contract",
    )
    contract_type = _required_string(
        contract_mapping.get("type"),
        f"{metric} normalization type",
    )
    if contract_type == "pass_fail":
        if outcome is None:
            raise EvaluationSchemaError(
                f"Foundry evaluator {metric} pass/fail normalization requires "
                "provider passed or label semantics."
            )
        return 1.0 if outcome else 0.0
    if contract_type == "min_max":
        if (
            not isinstance(raw_score, (int, float))
            or isinstance(raw_score, bool)
            or not isfinite(float(raw_score))
        ):
            raise EvaluationSchemaError(
                f"Foundry evaluator {metric} min/max normalization requires "
                "a finite numeric score."
            )
        minimum = _finite_number(
            contract_mapping.get("minimum"),
            f"{metric} normalization minimum",
        )
        maximum = _finite_number(
            contract_mapping.get("maximum"),
            f"{metric} normalization maximum",
        )
        if maximum <= minimum:
            raise EvaluationSchemaError(
                f"Foundry evaluator {metric} normalization maximum must exceed "
                "minimum."
            )
        numeric_score = float(raw_score)
        if numeric_score < minimum or numeric_score > maximum:
            raise EvaluationSchemaError(
                f"Foundry evaluator {metric} score is outside its normalization "
                "contract."
            )
        return (numeric_score - minimum) / (maximum - minimum)
    raise EvaluationSchemaError(
        f"Foundry evaluator {metric} has unknown normalization semantics."
    )


def _unit_interval(value: object, field: str) -> float:
    parsed = _finite_number(value, field)
    if parsed < 0 or parsed > 1:
        raise EvaluationSchemaError(f"{field} must be between 0 and 1.")
    return parsed


def _finite_number(value: object, field: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not isfinite(float(value))
    ):
        raise EvaluationSchemaError(f"{field} must be a finite number.")
    return float(value)


def _usage(sample: Mapping[str, object]) -> dict[str, int]:
    usage = _required_mapping(sample.get("usage"), "sample usage")
    return {
        "input_tokens": _nonnegative_integer(
            usage.get("prompt_tokens"),
            "usage.prompt_tokens",
        ),
        "output_tokens": _nonnegative_integer(
            usage.get("completion_tokens"),
            "usage.completion_tokens",
        ),
        "cached_tokens": _nonnegative_integer(
            usage.get("cached_tokens"),
            "usage.cached_tokens",
        ),
    }


def _case_id(datasource: Mapping[str, object]) -> str:
    value = datasource.get("case_id", datasource.get("id"))
    return _required_string(value, "output case_id")


def _batch_response_id(datasource: Mapping[str, object]) -> str:
    value = datasource.get(
        "response_id",
        datasource.get("sample.response_id"),
    )
    return _required_string(value, "output response_id")


def _simulation_response_ids(
    datasource: Mapping[str, object],
    item_id: str,
) -> list[str]:
    values = datasource.get("response_ids")
    if values is not None:
        return [
            _required_string(value, "simulation response_id")
            for value in _required_list(values, "simulation response_ids")
        ]
    single = datasource.get("response_id")
    if single is not None:
        return [_required_string(single, "simulation response_id")]
    return [item_id]


def _trajectory(
    datasource: Mapping[str, object],
    item_id: str,
) -> dict[str, object]:
    messages = _required_list(datasource.get("messages"), "simulation messages")
    tool_calls: list[dict[str, object]] = []
    for raw_message in messages:
        message = _required_mapping(raw_message, "simulation message")
        raw_calls = message.get("tool_calls")
        if raw_calls is None:
            continue
        for raw_call in _required_list(raw_calls, "simulation tool_calls"):
            call = _required_mapping(raw_call, "simulation tool_call")
            tool_calls.append(
                {
                    "call_id": _required_string(
                        call.get("call_id", call.get("id")),
                        "tool call_id",
                    ),
                    "name": _required_string(
                        call.get("name"),
                        "tool name",
                    ),
                    "status": _required_string(
                        call.get("status"),
                        "tool status",
                    ),
                    "duration_ms": (
                        _nonnegative_integer(
                            call["duration_ms"],
                            "tool duration_ms",
                        )
                        if call.get("duration_ms") is not None
                        else None
                    ),
                }
            )
    return {
        "trajectory_id": _required_string(
            datasource.get("trajectory_id", item_id),
            "trajectory_id",
        ),
        "turn_count": len(messages),
        "tool_calls": tool_calls,
    }


def _provider_call(operation: Callable[[], _T]) -> _T:
    try:
        return operation()
    except openai.AuthenticationError as error:
        raise EvaluationAuthenticationError(
            "Microsoft Foundry evaluation authentication failed."
        ) from error
    except openai.PermissionDeniedError as error:
        raise EvaluationAuthorizationError(
            "Microsoft Foundry evaluation authorization failed."
        ) from error
    except openai.NotFoundError as error:
        raise EvaluationNotFoundError(
            "Microsoft Foundry evaluation resource was not found."
        ) from error
    except openai.ConflictError as error:
        raise EvaluationConflictError(
            "Microsoft Foundry evaluation request conflicted with service state."
        ) from error
    except openai.RateLimitError as error:
        raise EvaluationRateLimitError(
            "Microsoft Foundry evaluation request was throttled."
        ) from error
    except openai.InternalServerError as error:
        raise EvaluationServiceError(
            "Microsoft Foundry evaluation service failed."
        ) from error
    except (openai.APIConnectionError, openai.APITimeoutError) as error:
        raise EvaluationServiceError(
            "Microsoft Foundry evaluation service was unavailable."
        ) from error
    except openai.APIStatusError as error:
        if error.status_code >= 500:
            raise EvaluationServiceError(
                "Microsoft Foundry evaluation service failed."
            ) from error
        raise EvaluationProviderError(
            "Microsoft Foundry evaluation request failed."
        ) from error


def _provider_mapping(value: object, field: str) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="python")
        if isinstance(dumped, Mapping):
            return dumped
    raise EvaluationSchemaError(
        f"Foundry {field} payload was not a dictionary."
    )


def _metadata(payload: Mapping[str, object]) -> dict[str, str]:
    metadata = _required_mapping(payload.get("metadata"), "metadata")
    parsed: dict[str, str] = {}
    for key, value in metadata.items():
        parsed[_required_string(key, "metadata key")] = _required_string(
            value,
            "metadata value",
        )
    return parsed


def _page_data(page: object) -> list[object]:
    data = getattr(page, "data", None)
    if data is None and isinstance(page, Mapping):
        data = page.get("data")
    return _required_list(data, "provider page data")


def _next_token(page: object) -> str | None:
    has_more = getattr(page, "has_more", None)
    if has_more is None and isinstance(page, Mapping):
        has_more = page.get("has_more")
    if not isinstance(has_more, bool):
        raise EvaluationSchemaError(
            "Foundry page has_more must be a boolean."
        )
    if not has_more:
        return None
    last_id = getattr(page, "last_id", None)
    if last_id is None and isinstance(page, Mapping):
        last_id = page.get("last_id")
    return _required_string(last_id, "provider continuation token")


def _parse_project_endpoint(endpoint: str) -> tuple[str, str]:
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "https"
        or parsed.query
        or parsed.fragment
        or parsed.hostname is None
        or not parsed.hostname.endswith(".services.ai.azure.com")
    ):
        raise ValueError("project_endpoint is not a Foundry project endpoint.")
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) != 3 or segments[:2] != ["api", "projects"]:
        raise ValueError("project_endpoint is not a Foundry project endpoint.")
    account = parsed.hostname.removesuffix(".services.ai.azure.com")
    project = unquote(segments[2])
    if not account or not project:
        raise ValueError("project_endpoint is not a Foundry project endpoint.")
    return account, project


def _timestamp(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise EvaluationSchemaError(
            "Foundry run created_at must be a Unix timestamp."
        )
    return datetime.fromtimestamp(value, tz=UTC).isoformat().replace(
        "+00:00",
        "Z",
    )


def _provider_error(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    mapping = _provider_mapping(value, "error")
    return _required_string(mapping.get("message"), "provider error message")


def _json_value(value: object) -> Any:
    try:
        return json.loads(json.dumps(value, separators=(",", ":")))
    except (TypeError, ValueError) as error:
        raise EvaluationSchemaError(
            "Foundry evaluation binding must be JSON serializable."
        ) from error


def _reference_value(
    reference: object,
    field: str,
    label: str,
) -> str:
    return _required_string(
        _required_mapping(reference, label).get(field),
        label,
    )


def _required_mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise EvaluationSchemaError(f"{field} must be a dictionary.")
    return value


def _required_list(value: object, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise EvaluationSchemaError(f"{field} must be a list.")
    return value


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise EvaluationSchemaError(f"{field} must be a non-empty string.")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return _required_string(value, "optional string")


def _positive_integer(value: object, field: str) -> int:
    if isinstance(value, str) and value.isdecimal():
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise EvaluationSchemaError(f"{field} must be a positive integer.")
    return value


def _nonnegative_integer(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise EvaluationSchemaError(
            f"{field} must be a non-negative integer."
        )
    return value


__all__ = [
    "EvaluationAuthenticationError",
    "EvaluationAuthorizationError",
    "EvaluationConflictError",
    "EvaluationNotFoundError",
    "EvaluationProviderError",
    "EvaluationRateLimitError",
    "EvaluationServiceError",
    "FoundryEvaluationTransport",
]
