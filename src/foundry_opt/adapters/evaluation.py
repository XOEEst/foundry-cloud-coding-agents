from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from time import sleep as _sleep
from typing import Any, Protocol

from foundry_opt.evaluation import (
    AgentVersionRef,
    DatasetSplit,
    DatasetVersionRef,
    EvaluationItem,
    EvaluationRun,
    EvaluationScore,
    EvaluationStatus,
    EvaluatorDefinitionRef,
    ToolCallMetadata,
    TrajectoryMetadata,
    Usage,
)


class EvaluationAdapterError(RuntimeError):
    """Base class for stable evaluation adapter failures."""


class EvaluationSchemaError(EvaluationAdapterError):
    pass


class RetryableEvaluationError(EvaluationAdapterError):
    pass


class EvaluationPollingTimeoutError(EvaluationAdapterError):
    pass


class EvaluationPaginationError(EvaluationAdapterError):
    pass


@dataclass(frozen=True)
class EvaluationDefinitionRequest:
    name: str
    evaluator_type: str
    schema_version: str
    configuration: Mapping[str, object]
    fingerprint: str


@dataclass(frozen=True)
class EvaluationDefinition:
    definition_id: str
    version: str
    fingerprint: str
    portal_url: str | None


@dataclass(frozen=True)
class BatchEvaluationRequest:
    display_name: str
    agent: AgentVersionRef
    dataset: DatasetVersionRef
    evaluator: EvaluatorDefinitionRef
    subject_id: str = "candidate"
    split: DatasetSplit = DatasetSplit.DEVELOPMENT


@dataclass(frozen=True)
class MultiTurnSimulationRequest:
    display_name: str
    agent: AgentVersionRef
    dataset: DatasetVersionRef
    evaluator: EvaluatorDefinitionRef
    max_turns: int
    personas: tuple[str, ...]
    subject_id: str = "candidate"
    split: DatasetSplit = DatasetSplit.DEVELOPMENT

    def __post_init__(self) -> None:
        if self.max_turns < 1:
            raise ValueError("max_turns must be positive.")
        if not self.personas:
            raise ValueError("At least one simulation persona is required.")


@dataclass(frozen=True)
class EvaluationOutputPage:
    items: tuple[EvaluationItem, ...]
    continuation_token: str | None


@dataclass(frozen=True)
class BatchEvaluationOutput:
    case_id: str
    case_hash: str
    response_id: str
    scores: tuple[EvaluationScore, ...]
    usage: Usage
    error: str | None
    duration_ms: int

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object],
    ) -> "BatchEvaluationOutput":
        try:
            if str(payload.get("kind", "batch")) != "batch":
                raise ValueError("Expected a batch result.")
            return cls(
                case_id=str(payload["case_id"]),
                case_hash=str(payload["case_hash"]),
                response_id=str(payload["response_id"]),
                scores=_parse_scores(payload["scores"]),
                usage=_parse_usage(payload.get("usage", {})),
                error=_optional_string(payload.get("error")),
                duration_ms=int(payload.get("duration_ms", 0)),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise EvaluationSchemaError(
                "Batch evaluation result did not match the adapter contract."
            ) from error

    def to_evaluation_item(self) -> EvaluationItem:
        return EvaluationItem(
            case_id=self.case_id,
            case_hash=self.case_hash,
            response_ids=(self.response_id,),
            scores=self.scores,
            usage=self.usage,
            error=self.error,
            duration_ms=self.duration_ms,
        )


@dataclass(frozen=True)
class MultiTurnSimulationOutput:
    case_id: str
    case_hash: str
    response_ids: tuple[str, ...]
    scores: tuple[EvaluationScore, ...]
    usage: Usage
    trajectory: TrajectoryMetadata
    error: str | None
    duration_ms: int

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object],
    ) -> "MultiTurnSimulationOutput":
        try:
            if str(payload.get("kind")) != "multi_turn_simulation":
                raise ValueError("Expected a multi-turn simulation result.")
            return cls(
                case_id=str(payload["case_id"]),
                case_hash=str(payload["case_hash"]),
                response_ids=tuple(
                    str(value) for value in _list(payload["response_ids"])
                ),
                scores=_parse_scores(payload["scores"]),
                usage=_parse_usage(payload.get("usage", {})),
                trajectory=_parse_trajectory(_mapping(payload["trajectory"])),
                error=_optional_string(payload.get("error")),
                duration_ms=int(payload.get("duration_ms", 0)),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise EvaluationSchemaError(
                "Multi-turn simulation result did not match the adapter contract."
            ) from error

    def to_evaluation_item(self) -> EvaluationItem:
        return EvaluationItem(
            case_id=self.case_id,
            case_hash=self.case_hash,
            response_ids=self.response_ids,
            scores=self.scores,
            usage=self.usage,
            trajectory=self.trajectory,
            error=self.error,
            duration_ms=self.duration_ms,
        )


@dataclass(frozen=True)
class PollPolicy:
    max_attempts: int = 20
    initial_delay_seconds: float = 1.0
    multiplier: float = 1.5
    max_delay_seconds: float = 15.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive.")
        if self.initial_delay_seconds < 0 or self.multiplier < 1:
            raise ValueError("Polling backoff is invalid.")
        if self.max_delay_seconds < 0:
            raise ValueError("max_delay_seconds cannot be negative.")


class EvaluationTransport(Protocol):
    def find_definition(
        self,
        fingerprint: str,
    ) -> Mapping[str, object] | None: ...

    def create_definition(
        self,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]: ...

    def create_run(
        self,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]: ...

    def get_run(self, run_id: str) -> Mapping[str, object]: ...

    def list_output_items(
        self,
        run_id: str,
        *,
        continuation_token: str | None,
        page_size: int,
    ) -> Mapping[str, object]: ...


class EvaluationGateway:
    def __init__(
        self,
        transport: EvaluationTransport,
        *,
        poll_policy: PollPolicy = PollPolicy(),
        page_size: int = 100,
        max_pages: int = 1000,
        sleep: Callable[[float], None] = _sleep,
    ) -> None:
        if page_size < 1 or page_size > 1000:
            raise ValueError("page_size must be between 1 and 1000.")
        if max_pages < 1:
            raise ValueError("max_pages must be positive.")
        self._transport = transport
        self._poll_policy = poll_policy
        self._page_size = page_size
        self._max_pages = max_pages
        self._sleep = sleep

    def create_or_reuse_definition(
        self,
        request: EvaluationDefinitionRequest,
    ) -> EvaluationDefinition:
        existing = self._transport.find_definition(request.fingerprint)
        if existing is not None:
            definition = _parse_definition(existing)
            if definition.fingerprint != request.fingerprint:
                raise EvaluationSchemaError(
                    "Reused evaluator definition fingerprint did not match."
                )
            return definition
        payload = {
            "name": request.name,
            "evaluator_type": request.evaluator_type,
            "schema_version": request.schema_version,
            "configuration": dict(request.configuration),
            "fingerprint": request.fingerprint,
        }
        definition = _parse_definition(
            self._transport.create_definition(payload)
        )
        if definition.fingerprint != request.fingerprint:
            raise EvaluationSchemaError(
                "Created evaluator definition fingerprint did not match."
            )
        return definition

    def create_run(
        self,
        request: BatchEvaluationRequest | MultiTurnSimulationRequest,
    ) -> EvaluationRun:
        payload = _serialize_run_request(request)
        response = dict(self._transport.create_run(payload))
        response.setdefault("subject_id", request.subject_id)
        response.setdefault("split", request.split.value)
        response.setdefault("agent", payload["agent"])
        response.setdefault("dataset", payload["dataset"])
        response.setdefault("evaluator", payload["evaluator"])
        return _parse_run(response)

    def get_run(self, run_id: str) -> EvaluationRun:
        delay = self._poll_policy.initial_delay_seconds
        last_status: EvaluationStatus | None = None
        for attempt in range(1, self._poll_policy.max_attempts + 1):
            try:
                run = _parse_run(self._transport.get_run(run_id))
            except RetryableEvaluationError:
                run = None
            if run is not None:
                last_status = run.status
                if run.status.terminal:
                    return run
            if attempt < self._poll_policy.max_attempts:
                self._sleep(delay)
                delay = min(
                    delay * self._poll_policy.multiplier,
                    self._poll_policy.max_delay_seconds,
                )
        detail = (
            f" Last observed status: {last_status.value}."
            if last_status is not None
            else ""
        )
        raise EvaluationPollingTimeoutError(
            "Evaluation run did not reach a terminal state within the "
            f"configured attempt bound.{detail}"
        )

    def iter_output_items(self, run_id: str) -> Iterator[EvaluationItem]:
        continuation_token: str | None = None
        seen_tokens: set[str] = set()
        for _ in range(self._max_pages):
            raw_page = self._transport.list_output_items(
                run_id,
                continuation_token=continuation_token,
                page_size=self._page_size,
            )
            page = _parse_page(raw_page)
            yield from page.items
            continuation_token = page.continuation_token
            if continuation_token is None:
                return
            if continuation_token in seen_tokens:
                raise EvaluationPaginationError(
                    "Evaluation output pagination repeated a continuation token."
                )
            seen_tokens.add(continuation_token)
        raise EvaluationPaginationError(
            "Evaluation output exceeded the configured page bound."
        )


def _serialize_run_request(
    request: BatchEvaluationRequest | MultiTurnSimulationRequest,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": (
            "multi_turn_simulation"
            if isinstance(request, MultiTurnSimulationRequest)
            else "batch"
        ),
        "display_name": request.display_name,
        "agent": {
            "agent_id": request.agent.agent_id,
            "draft_id": request.agent.draft_id,
            "version": request.agent.version,
        },
        "dataset": {
            "dataset_id": request.dataset.dataset_id,
            "version": request.dataset.version,
        },
        "evaluator": {
            "definition_id": request.evaluator.definition_id,
            "version": request.evaluator.version,
        },
    }
    if isinstance(request, MultiTurnSimulationRequest):
        payload["simulation"] = {
            "max_turns": request.max_turns,
            "personas": list(request.personas),
        }
    return payload


def _parse_definition(payload: Mapping[str, object]) -> EvaluationDefinition:
    try:
        return EvaluationDefinition(
            definition_id=str(payload["id"]),
            version=str(payload["version"]),
            fingerprint=str(payload["fingerprint"]),
            portal_url=_optional_string(payload.get("portal_url")),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise EvaluationSchemaError(
            "Evaluator definition response did not match the adapter contract."
        ) from error


def _parse_run(payload: Mapping[str, object]) -> EvaluationRun:
    try:
        agent = _mapping(payload["agent"])
        dataset = _mapping(payload["dataset"])
        evaluator = _mapping(payload["evaluator"])
        return EvaluationRun(
            run_id=str(payload["run_id"]),
            evaluation_id=str(payload["evaluation_id"]),
            subject_id=str(payload.get("subject_id", "candidate")),
            split=DatasetSplit(str(payload.get("split", "development"))),
            agent=AgentVersionRef(
                str(agent["agent_id"]),
                str(agent["draft_id"]),
                str(agent["version"]),
            ),
            dataset=DatasetVersionRef(
                str(dataset["dataset_id"]),
                str(dataset["version"]),
            ),
            evaluator=EvaluatorDefinitionRef(
                str(evaluator["definition_id"]),
                str(evaluator["version"]),
            ),
            status=EvaluationStatus(str(payload["status"])),
            portal_url=_optional_string(payload.get("portal_url")),
            started_at=_parse_datetime(payload.get("started_at")),
            completed_at=_parse_datetime(payload.get("completed_at")),
            error=_optional_string(payload.get("error")),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise EvaluationSchemaError(
            "Evaluation run response did not match the adapter contract."
        ) from error


def _parse_page(payload: Mapping[str, object]) -> EvaluationOutputPage:
    try:
        raw_items = payload["items"]
        if not isinstance(raw_items, list):
            raise TypeError("items must be a list")
        return EvaluationOutputPage(
            items=tuple(_parse_item(_mapping(item)) for item in raw_items),
            continuation_token=_optional_string(
                payload.get("continuation_token")
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise EvaluationSchemaError(
            "Evaluation output page did not match the adapter contract."
        ) from error


def _parse_item(payload: Mapping[str, object]) -> EvaluationItem:
    kind = str(payload.get("kind", "batch"))
    if kind == "batch":
        return BatchEvaluationOutput.from_payload(payload).to_evaluation_item()
    if kind == "multi_turn_simulation":
        return MultiTurnSimulationOutput.from_payload(
            payload
        ).to_evaluation_item()
    raise EvaluationSchemaError(f"Unsupported evaluation item kind: {kind}.")


def _parse_scores(value: object) -> tuple[EvaluationScore, ...]:
    return tuple(
        EvaluationScore(
            metric=str(score["metric"]),
            raw_score=_scalar_score(score.get("raw_score")),
            normalized_score=_optional_float(score.get("normalized_score")),
            reason=_optional_string(score.get("reason")),
        )
        for raw_score in _list(value)
        for score in (_mapping(raw_score),)
    )


def _parse_usage(value: object) -> Usage:
    usage = _mapping(value)
    return Usage(
        input_tokens=int(usage.get("input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
        cached_tokens=int(usage.get("cached_tokens", 0)),
    )


def _parse_trajectory(payload: Mapping[str, object]) -> TrajectoryMetadata:
    raw_tool_calls = _list(payload.get("tool_calls", []))
    return TrajectoryMetadata(
        trajectory_id=str(payload["trajectory_id"]),
        turn_count=int(payload["turn_count"]),
        tool_calls=tuple(
            ToolCallMetadata(
                call_id=str(tool_call["call_id"]),
                name=str(tool_call["name"]),
                status=str(tool_call["status"]),
                duration_ms=(
                    int(tool_call["duration_ms"])
                    if tool_call.get("duration_ms") is not None
                    else None
                ),
            )
            for raw_tool_call in raw_tool_calls
            for tool_call in (_mapping(raw_tool_call),)
        ),
    )


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("Expected a mapping.")
    return value


def _list(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise TypeError("Expected a list.")
    return value


def _optional_string(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    parsed = float(value)
    if not isfinite(parsed):
        raise ValueError("Evaluation scores must be finite.")
    return parsed


def _scalar_score(value: object) -> bool | int | float | str | None:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError("Raw evaluation scores must be scalar values.")
