"""OIDC-backed per-specification Foundry evaluation binding.

This module owns the production :class:`OptimizationEvaluationBinder`, the
callable that satisfies the issue-driven runner's ``EvaluationBinder`` seam
(:mod:`foundry_opt.optimization.runner`). Given an approved
:class:`~foundry_opt.optimization.models.OptimizationSpec` and the tuple of
registered :class:`~foundry_opt.evidence.EvaluationAssetReference` assets, it
returns an ``EvaluationRunner`` — ``(subject, split, attempt) ->
EvaluationResult`` — that the runner (and the bounded evaluation funnel) drives
for the baseline and every candidate.

Design
------
The existing evaluation model represents *one* evaluator definition per run,
but a specification can approve *several* evaluator assets and metrics. To
bridge that, each requested split is bound to a single, deterministic
*composite* Foundry evaluation definition whose testing criteria reference
**all** approved evaluators and whose normalization mapping covers **all**
metric policies. The definition is created once and reused by a stable
fingerprint bound to the specification hash, the split, the split dataset's
pinned ``remote_id``/version, the metric policy, and the evaluator identities.

For each attempt the binder creates a batch run against the exact draft agent
version and the split dataset, polls it to a terminal state, paginates and
normalizes its output items through the reused
:class:`~foundry_opt.adapters.evaluation.EvaluationGateway` and
:func:`~foundry_opt.evaluation.normalize_evaluation`, aggregates the metrics
per policy, and sets the bounded-repeat need. Only identity, provenance, and
aggregate/normalized evidence ever leave this module — never raw dataset rows.

The binder **fails closed** (raising :class:`OptimizationEvaluationError`,
never fabricating a result) on: a missing or ambiguous split dataset role, a
missing pinned ``remote_id``, an evaluator/metric mismatch, a run that reports
unsupported conversation simulation, cross-run or cross-lineage outputs, a
terminal ``failed``/``cancelled`` run, or any provider/credential failure.
Azure access stays explicit: an injected credential provider (Azure CLI OIDC)
and the current ``AIProjectClient``/``get_openai_client`` transport are used,
and both the client and credential are always closed.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from time import sleep as _default_sleep
from typing import Any, Protocol

from foundry_opt.adapters.evaluation import (
    BatchEvaluationRequest,
    EvaluationDefinition,
    EvaluationDefinitionRequest,
    EvaluationGateway,
    EvaluationTransport,
    PollPolicy,
)
from foundry_opt.adapters.foundry_evaluation import FoundryEvaluationTransport
from foundry_opt.config.models import (
    MetricDirection as ConfigMetricDirection,
)
from foundry_opt.config.models import (
    UndefinedBehavior as ConfigUndefinedBehavior,
)
from foundry_opt.evaluation import (
    DatasetSplit,
    DatasetVersionRef,
    EvaluationItem,
    EvaluationPolicy,
    EvaluationResult,
    EvaluationStatus,
    EvaluationSubject,
    EvaluatorDefinitionRef,
    MetricDirection,
    MetricPolicy,
    UndefinedBehavior,
    normalize_evaluation,
)
from foundry_opt.evidence import EvaluationAssetReference
from foundry_opt.openai_graders import OpenAIStringCheckGrader
from foundry_opt.optimization.models import OptimizationSpec

__all__ = [
    "AzureCredentialProvider",
    "EvaluationRunner",
    "OptimizationEvaluationBinder",
    "OptimizationEvaluationError",
    "build_evaluation_policy",
]


_EVALUATOR_TYPE = "azure_ai_evaluator"
_SCHEMA_VERSION = "1"
_FINGERPRINT_SCHEME = "opt-eval-3"
_DATA_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SCHEMA_ANNOTATION_FIELDS = frozenset(
    {
        "$comment",
        "default",
        "deprecated",
        "description",
        "examples",
        "readOnly",
        "title",
        "writeOnly",
    }
)


EvaluationRunner = Callable[
    [EvaluationSubject, DatasetSplit, int],
    EvaluationResult,
]


class OptimizationEvaluationError(RuntimeError):
    """A per-specification evaluation could not be bound or completed.

    Raised whenever the binding must fail closed rather than fabricate an
    evaluation result: an unusable specification/asset set, an unsupported
    evaluation, or a provider/credential failure.
    """


class AzureCredentialProvider(Protocol):
    def create(self) -> Any: ...


class _ProjectClientFactory(Protocol):
    def __call__(self, endpoint: str, credential: Any) -> Any: ...


class _TransportFactory(Protocol):
    def __call__(
        self, project_client: Any, endpoint: str
    ) -> EvaluationTransport: ...


class _EvaluatorSchemaResolver(Protocol):
    def __call__(
        self,
        project_client: Any,
        evaluator_name: str,
        evaluator_version: str,
    ) -> dict[str, object]: ...


def _create_project_client(endpoint: str, credential: Any) -> Any:
    # Imported lazily so importing this module never forces the Azure SDK to
    # load in environments that only build definition requests or fingerprints.
    from azure.ai.projects import AIProjectClient

    return AIProjectClient(endpoint=endpoint, credential=credential)


def _create_transport(
    project_client: Any, endpoint: str
) -> EvaluationTransport:
    return FoundryEvaluationTransport(
        project_client, project_endpoint=endpoint
    )


def _resolve_evaluator_data_schema(
    project_client: Any,
    evaluator_name: str,
    evaluator_version: str,
) -> dict[str, object]:
    beta = getattr(project_client, "beta", None)
    evaluators = getattr(beta, "evaluators", None)
    get_version = getattr(evaluators, "get_version", None)
    if not callable(get_version):
        return _default_evaluator_data_schema()
    record = get_version(evaluator_name, evaluator_version)
    definition = getattr(record, "definition", None)
    if isinstance(record, dict):
        definition = record.get("definition", definition)
    data_schema = getattr(definition, "data_schema", None)
    if isinstance(definition, dict):
        data_schema = definition.get("data_schema", data_schema)
    return _normalized_evaluator_data_schema(data_schema)


def build_evaluation_policy(spec: OptimizationSpec) -> EvaluationPolicy:
    """Translate the approved specification metrics into an evaluation policy.

    This mirrors the runner's own metric-policy translation exactly so the
    per-attempt normalization inside the binder and the bounded-repeat
    combination performed by the funnel agree on thresholds, directions, and
    undefined-metric behavior.
    """

    metrics = tuple(
        MetricPolicy(
            name=name,
            direction=(
                MetricDirection.MAXIMIZE
                if policy.direction is ConfigMetricDirection.MAXIMIZE
                else MetricDirection.MINIMIZE
            ),
            threshold=float(policy.threshold),
            materiality=float(policy.materiality),
            hard_guardrail=bool(policy.hard_guardrail),
            undefined_behavior=(
                UndefinedBehavior.FAIL
                if policy.undefined_behavior is ConfigUndefinedBehavior.FAIL
                else UndefinedBehavior.IGNORE
            ),
        )
        for name, policy in spec.metrics.items()
    )
    return EvaluationPolicy(metrics)


@dataclass(frozen=True)
class _FoundryAsset:
    """A pinned, Foundry-resolvable dataset or evaluator reference."""

    asset_id: str
    source: str
    name: str | None
    version: str
    remote_id: str
    content_sha256: str | None
    role: str | None
    metrics: tuple[str, ...]

    def fingerprint_entry(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "source": self.source,
            "name": self.name,
            "version": self.version,
            "remote_id": self.remote_id,
            "content_sha256": self.content_sha256,
            "metrics": list(self.metrics),
        }


class OptimizationEvaluationBinder:
    """Bind an approved specification to a composite Foundry evaluation.

    The binder is itself the runner ``EvaluationBinder``: calling it with the
    approved spec and its registered assets returns an ``EvaluationRunner``.
    """

    def __init__(
        self,
        project_endpoint: str,
        *,
        credential_provider: AzureCredentialProvider,
        client_factory: _ProjectClientFactory = _create_project_client,
        transport_factory: _TransportFactory = _create_transport,
        poll_policy: PollPolicy = PollPolicy(),
        page_size: int = 100,
        max_pages: int = 1000,
        sleep: Callable[[float], None] = _default_sleep,
        evaluator_model_deployment: str | None = None,
        evaluator_schema_resolver: _EvaluatorSchemaResolver = (
            _resolve_evaluator_data_schema
        ),
    ) -> None:
        if not project_endpoint or not project_endpoint.strip():
            raise ValueError("project_endpoint is required")
        self._project_endpoint = project_endpoint
        self._credential_provider = credential_provider
        self._client_factory = client_factory
        self._transport_factory = transport_factory
        self._poll_policy = poll_policy
        self._page_size = page_size
        self._max_pages = max_pages
        self._sleep = sleep
        self._evaluator_schema_resolver = evaluator_schema_resolver
        self._evaluator_schema_cache: dict[
            tuple[str, str], dict[str, object]
        ] = {}
        if (
            evaluator_model_deployment is not None
            and not evaluator_model_deployment.strip()
        ):
            raise ValueError("evaluator_model_deployment must not be blank")
        self._evaluator_model_deployment = evaluator_model_deployment

    def __call__(
        self,
        spec: OptimizationSpec,
        assets: Sequence[EvaluationAssetReference],
    ) -> EvaluationRunner:
        plan = _EvaluationPlan.build(spec, assets)
        return _BoundEvaluation(self, plan)

    # -- provider interaction ----------------------------------------------

    def _evaluate(
        self,
        plan: _EvaluationPlan,
        subject: EvaluationSubject,
        split: DatasetSplit,
        attempt: int,
    ) -> EvaluationResult:
        dataset = plan.dataset_for(split)
        credential: Any | None = None
        project_client: Any | None = None
        try:
            credential = self._credential_provider.create()
            project_client = self._client_factory(
                self._project_endpoint, credential
            )
            transport = self._transport_factory(
                project_client, self._project_endpoint
            )
            evaluator_schemas = {
                evaluator.asset_id: self._evaluator_data_schema(
                    project_client, evaluator
                )
                for evaluator in plan.evaluators
            }
            gateway = EvaluationGateway(
                transport,
                poll_policy=self._poll_policy,
                page_size=self._page_size,
                max_pages=self._max_pages,
                sleep=self._sleep,
            )
            definition = self._create_or_reuse_definition(
                gateway,
                plan,
                split,
                dataset,
                evaluator_schemas,
            )
            run = self._create_and_poll(
                gateway, definition, plan, subject, split, dataset, attempt
            )
            items = tuple(gateway.iter_output_items(run.run_id))
            _reject_unknown_metrics(items, plan.metric_names)
            return normalize_evaluation(run, items, plan.policy)
        except OptimizationEvaluationError:
            raise
        except Exception as error:
            # Any gateway, transport, normalization, or Azure
            # credential/client failure fails closed rather than surfacing an
            # ambiguous exception the runner might mistake for a soft result.
            raise OptimizationEvaluationError(
                "the per-specification Foundry evaluation failed: "
                f"{error}"
            ) from error
        finally:
            _close_quietly(project_client)
            _close_quietly(credential)

    def _evaluator_data_schema(
        self,
        project_client: Any,
        evaluator: _FoundryAsset,
    ) -> dict[str, object]:
        if OpenAIStringCheckGrader.from_remote_id(
            evaluator.remote_id
        ) is not None:
            return _default_evaluator_data_schema()
        key = (_evaluator_catalog_name(evaluator), evaluator.version)
        cached = self._evaluator_schema_cache.get(key)
        if cached is not None:
            return cached
        schema = self._evaluator_schema_resolver(
            project_client,
            key[0],
            key[1],
        )
        self._evaluator_schema_cache[key] = schema
        return schema

    def _create_or_reuse_definition(
        self,
        gateway: EvaluationGateway,
        plan: _EvaluationPlan,
        split: DatasetSplit,
        dataset: _FoundryAsset,
        evaluator_schemas: dict[str, dict[str, object]],
    ) -> EvaluationDefinition:
        fingerprint = plan.fingerprint(
            split,
            dataset,
            self._evaluator_model_deployment,
            evaluator_schemas,
        )
        request = EvaluationDefinitionRequest(
            name=plan.definition_name(split),
            evaluator_type=_EVALUATOR_TYPE,
            schema_version=_SCHEMA_VERSION,
            configuration=plan.definition_configuration(
                self._evaluator_model_deployment,
                evaluator_schemas,
            ),
            fingerprint=fingerprint,
        )
        return gateway.create_or_reuse_definition(request)

    def _create_and_poll(
        self,
        gateway: EvaluationGateway,
        definition: EvaluationDefinition,
        plan: _EvaluationPlan,
        subject: EvaluationSubject,
        split: DatasetSplit,
        dataset: _FoundryAsset,
        attempt: int,
    ) -> Any:
        request = BatchEvaluationRequest(
            display_name=(
                f"foundry-opt {plan.target} {subject.subject_id} "
                f"{split.value} attempt {attempt}"
                + (
                    f" {subject.idempotency_key[:12]}"
                    if subject.idempotency_key is not None
                    else ""
                )
            ),
            agent=subject.agent,
            dataset=DatasetVersionRef(dataset.remote_id, dataset.version),
            evaluator=EvaluatorDefinitionRef(
                definition.definition_id, definition.version
            ),
            subject_id=subject.subject_id,
            split=split,
            idempotency_key=(
                hashlib.sha256(
                    (
                        f"{subject.idempotency_key}:attempt:{attempt}"
                    ).encode("ascii")
                ).hexdigest()
                if subject.idempotency_key is not None
                else None
            ),
        )
        created = gateway.create_or_reuse_run(request)
        run = gateway.get_run(created.run_id)
        if run.status in {
            EvaluationStatus.FAILED,
            EvaluationStatus.CANCELLED,
        }:
            raise OptimizationEvaluationError(
                "the Foundry evaluation run reached a terminal "
                f"{run.status.value} state without usable results"
            )
        return run


@dataclass(frozen=True)
class _EvaluationPlan:
    """The immutable, per-specification evaluation plan.

    Built once when the binder is called, then consulted for every attempt
    across both splits. It resolves the split datasets, the approved evaluator
    references, the metric policy, and the deterministic definition fingerprint
    while enforcing the fail-closed invariants that do not depend on the split.
    """

    spec_sha256: str
    target: str
    policy: EvaluationPolicy
    metric_names: frozenset[str]
    metric_fingerprint: tuple[dict[str, object], ...]
    evaluators: tuple[_FoundryAsset, ...]
    metric_evaluators: tuple[tuple[str, _FoundryAsset], ...]
    datasets_by_role: dict[str, tuple[EvaluationAssetReference, ...]]

    @classmethod
    def build(
        cls,
        spec: OptimizationSpec,
        assets: Sequence[EvaluationAssetReference],
    ) -> _EvaluationPlan:
        _validate_assets_match_spec(spec, assets)
        datasets_by_role: dict[str, list[EvaluationAssetReference]] = {}
        evaluators: list[_FoundryAsset] = []
        for asset in assets:
            if asset.kind == "dataset":
                datasets_by_role.setdefault(str(asset.role), []).append(
                    asset
                )
            elif asset.kind == "evaluator":
                evaluators.append(_resolve_asset(asset))
        if not evaluators:
            raise OptimizationEvaluationError(
                "the approved specification has no evaluator assets"
            )
        ordered_evaluators = tuple(
            sorted(evaluators, key=lambda item: item.asset_id)
        )
        metric_names = frozenset(spec.metrics)
        metric_evaluators = _map_metrics_to_evaluators(
            metric_names, ordered_evaluators
        )
        metric_fingerprint = tuple(
            {
                "name": name,
                **_metric_policy_fingerprint(spec.metrics[name]),
            }
            for name in sorted(spec.metrics)
        )
        return cls(
            spec_sha256=spec.sha256,
            target=spec.target,
            policy=build_evaluation_policy(spec),
            metric_names=metric_names,
            metric_fingerprint=metric_fingerprint,
            evaluators=ordered_evaluators,
            metric_evaluators=metric_evaluators,
            datasets_by_role={
                role: tuple(items)
                for role, items in datasets_by_role.items()
            },
        )

    def dataset_for(self, split: DatasetSplit) -> _FoundryAsset:
        candidates = self.datasets_by_role.get(split.value, ())
        if not candidates:
            raise OptimizationEvaluationError(
                f"no approved dataset has the {split.value} role"
            )
        if len(candidates) > 1:
            raise OptimizationEvaluationError(
                f"the {split.value} dataset role is ambiguous: "
                f"{', '.join(sorted(item.asset_id for item in candidates))}"
            )
        return _resolve_asset(candidates[0])

    def definition_name(self, split: DatasetSplit) -> str:
        return (
            f"foundry-opt-{self.target}-{split.value}-"
            f"{self.spec_sha256[:12]}"
        )

    def definition_configuration(
        self,
        evaluator_model_deployment: str | None = None,
        evaluator_schemas: dict[str, dict[str, object]] | None = None,
    ) -> dict[str, object]:
        # One testing criterion per approved metric: the criterion name is the
        # metric name (so provider results map straight onto the metric
        # policy), and evaluator_name is the catalog name of the single
        # evaluator that produces that metric. Its exact remote identity stays
        # in evaluator_reference for immutable lineage.
        schemas = evaluator_schemas or {
            evaluator.asset_id: _default_evaluator_data_schema()
            for evaluator in self.evaluators
        }
        testing_criteria = []
        item_properties: dict[str, object] = {}
        required_item_fields = {"query"}
        for metric, evaluator in self.metric_evaluators:
            string_check = OpenAIStringCheckGrader.from_remote_id(
                evaluator.remote_id
            )
            if string_check is not None:
                testing_criteria.append(
                    {
                        "type": "string_check",
                        "name": metric,
                        "input": string_check.input,
                        "operation": string_check.operation,
                        "reference": string_check.reference,
                    }
                )
                continue
            schema = schemas[evaluator.asset_id]
            properties, required = _evaluator_data_fields(schema)
            data_mapping = {
                field: (
                    "{{sample.output_text}}"
                    if field == "response"
                    else f"{{{{item.{field}}}}}"
                )
                for field in sorted(properties)
            }
            for field, field_schema in properties.items():
                if field == "response":
                    continue
                existing = item_properties.get(field)
                if existing is not None and not _compatible_field_schemas(
                    existing,
                    field_schema,
                ):
                    raise OptimizationEvaluationError(
                        "approved evaluators define conflicting schemas for "
                        f"dataset field {field!r}"
                    )
                if existing is None:
                    item_properties[field] = field_schema
            required_item_fields.update(required - {"response"})
            criterion: dict[str, object] = {
                "type": _EVALUATOR_TYPE,
                "name": metric,
                "evaluator_name": _evaluator_catalog_name(evaluator),
                "evaluator_reference": {
                    "asset_id": evaluator.asset_id,
                    "source": evaluator.source,
                    "name": evaluator.name,
                    "version": evaluator.version,
                    "remote_id": evaluator.remote_id,
                    "content_sha256": evaluator.content_sha256,
                },
                "data_mapping": data_mapping,
            }
            if evaluator_model_deployment is not None:
                threshold = self.policy.metric(metric).threshold
                criterion["initialization_parameters"] = {
                    "deployment_name": evaluator_model_deployment,
                    "threshold": threshold,
                    "pass_threshold": threshold,
                }
            testing_criteria.append(criterion)
        item_properties.setdefault("query", {"type": "string"})
        normalization = {
            metric: (
                {"type": "pass_fail"}
                if self.policy.metric(metric).hard_guardrail
                else {
                    "type": "min_max",
                    "minimum": 0.0,
                    "maximum": 1.0,
                }
            )
            for metric, _ in self.metric_evaluators
        }
        return {
            "data_source_config": {
                "type": "custom",
                "item_schema": {
                    "type": "object",
                    "properties": {
                        field: item_properties[field]
                        for field in sorted(item_properties)
                    },
                    "required": sorted(required_item_fields),
                },
            },
            "testing_criteria": testing_criteria,
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
            },
            "normalization": normalization,
        }

    def fingerprint(
        self,
        split: DatasetSplit,
        dataset: _FoundryAsset,
        evaluator_model_deployment: str | None = None,
        evaluator_schemas: dict[str, dict[str, object]] | None = None,
    ) -> str:
        schemas = evaluator_schemas or {
            evaluator.asset_id: _default_evaluator_data_schema()
            for evaluator in self.evaluators
        }
        payload = {
            "scheme": _FINGERPRINT_SCHEME,
            "spec_sha256": self.spec_sha256,
            "split": split.value,
            "dataset": {
                "remote_id": dataset.remote_id,
                "version": dataset.version,
                "name": dataset.name,
            },
            "metrics": list(self.metric_fingerprint),
            "evaluators": [
                evaluator.fingerprint_entry()
                for evaluator in self.evaluators
            ],
            "evaluator_model_deployment": evaluator_model_deployment,
            "evaluator_data_schemas": {
                asset_id: schemas[asset_id]
                for asset_id in sorted(schemas)
            },
        }
        digest = hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        return f"opteval-{digest}"


@dataclass(frozen=True)
class _BoundEvaluation:
    """The ``EvaluationRunner`` returned by the binder for one specification."""

    binder: OptimizationEvaluationBinder
    plan: _EvaluationPlan

    def __call__(
        self,
        subject: EvaluationSubject,
        split: DatasetSplit,
        attempt: int,
    ) -> EvaluationResult:
        return self.binder._evaluate(self.plan, subject, split, attempt)


def _resolve_asset(asset: EvaluationAssetReference) -> _FoundryAsset:
    if not asset.remote_id:
        raise OptimizationEvaluationError(
            f"approved asset {asset.asset_id!r} has no pinned Foundry "
            "remote id"
        )
    version = asset.version or (
        asset.content_sha256[:16] if asset.content_sha256 else None
    )
    if not version:
        raise OptimizationEvaluationError(
            f"approved asset {asset.asset_id!r} has no resolvable Foundry "
            "version"
        )
    return _FoundryAsset(
        asset_id=asset.asset_id,
        source=asset.source,
        name=asset.name,
        version=version,
        remote_id=asset.remote_id,
        content_sha256=asset.content_sha256,
        role=asset.role,
        metrics=tuple(asset.metrics),
    )


def _map_metrics_to_evaluators(
    metric_names: frozenset[str],
    evaluators: tuple[_FoundryAsset, ...],
) -> tuple[tuple[str, _FoundryAsset], ...]:
    """Bind each approved metric to exactly one approved evaluator.

    The current Foundry Evals result semantics surface one score per testing
    criterion, and there is no supported way to select an individual metric
    from an evaluator that emits several. This binding therefore fails closed
    unless every evaluator declares exactly one metric, every declared metric
    is part of the approved policy, and every approved metric is produced by
    exactly one evaluator.
    """

    producers: dict[str, list[_FoundryAsset]] = {}
    for evaluator in evaluators:
        if len(evaluator.metrics) != 1:
            raise OptimizationEvaluationError(
                f"evaluator {evaluator.asset_id!r} declares "
                f"{len(evaluator.metrics)} metrics; the current Foundry "
                "evaluation result semantics require exactly one metric per "
                "evaluator"
            )
        (metric,) = evaluator.metrics
        if metric not in metric_names:
            raise OptimizationEvaluationError(
                f"evaluator {evaluator.asset_id!r} references metric "
                f"{metric!r} which is not in the approved metric policy"
            )
        producers.setdefault(metric, []).append(evaluator)
    ambiguous = sorted(
        metric for metric, owners in producers.items() if len(owners) > 1
    )
    if ambiguous:
        raise OptimizationEvaluationError(
            "these approved metrics are produced by more than one evaluator: "
            + ", ".join(ambiguous)
        )
    missing = sorted(metric_names - producers.keys())
    if missing:
        raise OptimizationEvaluationError(
            "no approved evaluator produces these metrics: "
            + ", ".join(missing)
        )
    return tuple(
        (metric, producers[metric][0]) for metric in sorted(metric_names)
    )


def _evaluator_catalog_name(evaluator: _FoundryAsset) -> str:
    if not evaluator.name:
        raise OptimizationEvaluationError(
            f"approved evaluator {evaluator.asset_id!r} has no catalog name"
        )
    return evaluator.name


def _default_evaluator_data_schema() -> dict[str, object]:
    return {
        "type": "object",
        "required": ["query", "response"],
        "properties": {
            "query": {"type": "string"},
            "response": {"type": "string"},
        },
    }


def _normalized_evaluator_data_schema(value: object) -> dict[str, object]:
    if value is None:
        return _default_evaluator_data_schema()
    if not isinstance(value, Mapping):
        model_dump = getattr(value, "model_dump", None)
        if not callable(model_dump):
            raise OptimizationEvaluationError(
                "the Foundry evaluator data schema is invalid"
            )
        value = model_dump(mode="json")
    try:
        schema = json.loads(json.dumps(dict(value), sort_keys=True))
    except (TypeError, ValueError) as error:
        raise OptimizationEvaluationError(
            "the Foundry evaluator data schema is not JSON-compatible"
        ) from error
    if not isinstance(schema, dict):
        raise OptimizationEvaluationError(
            "the Foundry evaluator data schema is invalid"
        )
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise OptimizationEvaluationError(
            "the Foundry evaluator data schema properties are invalid"
        )
    required = schema.get("required")
    if required is None:
        required = []
    if (
        not isinstance(required, list)
        or any(not isinstance(field, str) for field in required)
    ):
        raise OptimizationEvaluationError(
            "the Foundry evaluator required data fields are invalid"
        )
    schema["type"] = "object"
    schema["properties"] = properties
    schema["required"] = sorted(set(required))
    _evaluator_data_fields(schema)
    return schema


def _evaluator_data_fields(
    schema: Mapping[str, object],
) -> tuple[dict[str, object], set[str]]:
    raw_properties = schema.get("properties")
    raw_required = schema.get("required")
    if not isinstance(raw_properties, Mapping) or not isinstance(
        raw_required, Sequence
    ):
        raise OptimizationEvaluationError(
            "the Foundry evaluator data schema is invalid"
        )
    properties: dict[str, object] = {}
    for field, field_schema in raw_properties.items():
        if (
            not isinstance(field, str)
            or not _DATA_FIELD.fullmatch(field)
            or not isinstance(field_schema, Mapping)
        ):
            raise OptimizationEvaluationError(
                "the Foundry evaluator data schema contains an unsupported "
                "field"
            )
        properties[field] = json.loads(
            json.dumps(dict(field_schema), sort_keys=True)
        )
    required = {
        field
        for field in raw_required
        if isinstance(field, str) and field in properties
    }
    if len(required) != len(raw_required):
        raise OptimizationEvaluationError(
            "the Foundry evaluator required data fields are invalid"
        )
    return properties, required


def _compatible_field_schemas(
    left: object,
    right: object,
) -> bool:
    return _schema_validation_contract(left) == _schema_validation_contract(
        right
    )


def _schema_validation_contract(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            key: _schema_validation_contract(item)
            for key, item in value.items()
            if key not in _SCHEMA_ANNOTATION_FIELDS
        }
    if isinstance(value, list):
        return [_schema_validation_contract(item) for item in value]
    return value


def _metric_policy_fingerprint(policy: Any) -> dict[str, object]:
    document = policy.model_dump(mode="json")
    repeat = document.get("repeat")
    if isinstance(repeat, dict) and isinstance(
        repeat.get("conditions"), list
    ):
        repeat["conditions"] = sorted(repeat["conditions"])
    return document


def _validate_assets_match_spec(
    spec: OptimizationSpec,
    assets: Sequence[EvaluationAssetReference],
) -> None:
    spec_datasets = {dataset.asset_id for dataset in spec.datasets}
    spec_evaluators = {evaluator.asset_id for evaluator in spec.evaluators}
    asset_datasets = {
        asset.asset_id for asset in assets if asset.kind == "dataset"
    }
    asset_evaluators = {
        asset.asset_id for asset in assets if asset.kind == "evaluator"
    }
    if asset_datasets != spec_datasets or asset_evaluators != spec_evaluators:
        raise OptimizationEvaluationError(
            "the registered evaluation assets do not match the approved "
            "specification datasets and evaluators"
        )


def _reject_unknown_metrics(
    items: Sequence[EvaluationItem],
    metric_names: frozenset[str],
) -> None:
    observed = {
        score.metric for item in items for score in item.scores
    }
    unexpected = observed - metric_names
    if unexpected:
        raise OptimizationEvaluationError(
            "the Foundry evaluation returned metrics outside the approved "
            f"policy: {', '.join(sorted(unexpected))}"
        )


def _close_quietly(resource: Any | None) -> None:
    close = getattr(resource, "close", None)
    if callable(close):
        with suppress(Exception):
            close()
