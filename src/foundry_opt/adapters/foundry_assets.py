"""OIDC-backed Foundry evaluation asset resolution and registration.

This module implements the two seams declared by
:mod:`foundry_opt.optimization.assets` against a real Microsoft Foundry
project using :class:`azure.ai.projects.AIProjectClient`:

* :class:`FoundryAssetResolutionGateway` resolves an *existing*, already
  published dataset or evaluator to its exact remote identity through
  ``client.datasets.get`` / ``client.beta.evaluators.get_version``.
* :class:`EvaluationAssetRegistrationGateway` registers a prepared dataset or
  custom evaluator under a deterministic name/version, tagging the created
  (or already-registered) remote asset with a content-hash tag so repeated
  registration of the same content is idempotent, while registering the same
  name/version with *different* content is rejected without overwriting the
  existing asset.

Both gateways authenticate through an injected Azure credential provider
(the shared :class:`foundry_opt.adapters.foundry.AzureCliCredentialProvider`
in production) and always close the Foundry client and credential before
returning, whether the call succeeds or fails. Every Azure SDK failure is
translated into one of the stable, typed errors defined below: callers never
see a raw Azure exception message or payload.

Trace-sourced assets are never routed here: :func:`materialize_prepared_asset`
blocks them before a registration gateway is ever invoked, and builtin or
already-pinned existing assets never reach the registration gateway either
(they resolve through :class:`FoundryAssetResolutionGateway` or need no
remote call at all).

Custom evaluator definitions
-----------------------------
A custom evaluator's single prepared file is one of:

* ``*.json`` — a **prompt-based** evaluator spec, in any of three shapes:

  1. Our own ``definition_type: "prompt"`` shape (multiple metrics)::

         {
           "definition_type": "prompt",
           "display_name": "Quality",
           "prompt_text": "...",
           "categories": ["quality"],
           "supported_evaluation_levels": ["turn"],
           "init_parameters": {...},
           "data_schema": {...},
           "metrics": {
             "quality": {
               "type": "continuous",
               "desirable_direction": "increase",
               "min_value": 0.0,
               "max_value": 1.0,
               "threshold": 0.7,
               "is_primary": true
             }
           }
         }

  2. The repository acceptance ``kind: "prompt"`` shape (single metric)::

         {
           "kind": "prompt",
           "name": "acceptance-advisory-safety",
           "category": "safety",
           "scoring_type": "boolean",
           "metric": "advisory_safety",
           "metric_name": "advisory_safety",
           "direction": "maximize",
           "desirable_direction": "increase",
           "threshold": 1.0,
           "scale": {"minimum": 0.0, "maximum": 1.0},
           "data_schema": {...},
           "prompt_text": "..."
         }

     ``prompt_text`` is used verbatim; ``metric_name`` (falling back to
     ``metric``) becomes the single metrics-map key; ``category`` (falling
     back to ``categories``, defaulting to ``"quality"`` when neither is
     present) becomes the categories list; ``scoring_type``/``type`` become
     the metric ``type``; ``desirable_direction`` is used when present, else
     derived from ``direction`` (``maximize`` -> ``increase``, ``minimize``
     -> ``decrease``, ``neutral`` -> ``neutral``); ``scale.minimum`` /
     ``scale.maximum`` become ``min_value`` / ``max_value``.

  3. The repository acceptance ``kind: "rubric"`` shape (weighted
     dimensions, no free-form ``prompt_text``)::

         {
           "kind": "rubric",
           "name": "acceptance-policy-coverage",
           "metric": "policy_coverage",
           "direction": "maximize",
           "threshold": 0.7,
           "scale": {"minimum": 0.0, "maximum": 1.0},
           "dimensions": [
             {"name": "decision", "weight": 0.35, "criteria": "..."},
             {"name": "policy_rules", "weight": 0.35, "criteria": "..."}
           ]
         }

     A deterministic ``prompt_text`` is compiled from the ``dimensions``
     list (in declaration order): it references ``{{query}}``/``{{response}}``
     and enumerates each dimension's weight and criteria so the evaluator
     computes a single weighted ``metric`` score. No ``data_schema`` is
     accepted from the rubric spec (rubrics do not declare one); a minimal,
     non-conflicting ``{query, response}`` object schema is generated
     instead.

* ``*.py`` — a **code-based** evaluator: the raw Python source, whose first
  line must be a ``# foundry-opt-evaluator-metadata: <json>`` comment
  carrying the same ``categories``/``metrics``/``supported_evaluation_levels``
  metadata (plus an optional ``entry_point``); the whole file (including that
  comment) becomes the evaluator's ``code_text``.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    CodeBasedEvaluatorDefinition,
    EvaluatorDefinition,
    EvaluatorMetric,
    EvaluatorType,
    EvaluatorVersion,
    FileDatasetVersion,
    PromptBasedEvaluatorDefinition,
)
from azure.core.exceptions import (
    AzureError,
    ClientAuthenticationError,
    HttpResponseError,
    ServiceRequestError,
    ServiceResponseError,
)

from foundry_opt.adapters.foundry import AzureCredentialProvider
from foundry_opt.optimization.assets import AssetIdentity
from foundry_opt.optimization.models import AssetKind


class FoundryAssetGatewayError(RuntimeError):
    """Base class for stable Foundry evaluation asset failures."""


class FoundryAssetAuthenticationError(FoundryAssetGatewayError):
    def __init__(self) -> None:
        super().__init__(
            "Azure rejected the supplied credentials for the Foundry asset "
            "gateway."
        )


class FoundryAssetAuthorizationError(FoundryAssetGatewayError):
    def __init__(self) -> None:
        super().__init__(
            "The caller cannot access this Foundry dataset or evaluator."
        )


class FoundryAssetNotFoundError(FoundryAssetGatewayError):
    def __init__(self, kind: AssetKind, name: str, version: str) -> None:
        self.kind = kind
        self.name = name
        self.version = version
        super().__init__(
            f"no {kind.value} named '{name}' at version '{version}' was "
            "found in Foundry"
        )


class FoundryAssetThrottledError(FoundryAssetGatewayError):
    def __init__(self) -> None:
        super().__init__("The Foundry service throttled the asset request.")


class FoundryAssetTransportError(FoundryAssetGatewayError):
    def __init__(self) -> None:
        super().__init__(
            "A network transport failure prevented Foundry asset access."
        )


class FoundryAssetServiceError(FoundryAssetGatewayError):
    def __init__(self) -> None:
        super().__init__(
            "The Foundry service could not complete the asset request."
        )


class FoundryAssetUnexpectedSdkError(FoundryAssetGatewayError):
    def __init__(self) -> None:
        super().__init__("The Foundry SDK failed unexpectedly.")


class FoundryAssetIdentityMismatchError(FoundryAssetGatewayError):
    def __init__(self, kind: AssetKind, name: str, version: str) -> None:
        self.kind = kind
        self.name = name
        self.version = version
        super().__init__(
            f"Foundry returned an identity that does not match the "
            f"requested {kind.value} '{name}' at version '{version}'"
        )


class FoundryAssetRegistrationConflictError(FoundryAssetGatewayError):
    def __init__(self, kind: AssetKind, name: str, version: str) -> None:
        self.kind = kind
        self.name = name
        self.version = version
        super().__init__(
            f"a {kind.value} named '{name}' at version '{version}' is "
            "already registered with different content; it will not be "
            "overwritten"
        )


class FoundryAssetContentError(FoundryAssetGatewayError):
    """Raised when prepared asset content cannot be registered as-is."""


_CONTENT_HASH_TAG = "foundry_opt_content_sha256"
_ASSET_VERSION_TAG = "foundry_opt_asset_version"
_CODE_METADATA_PREFIX = "# foundry-opt-evaluator-metadata:"


def _create_client(endpoint: str, credential: Any) -> AIProjectClient:
    return AIProjectClient(endpoint=endpoint, credential=credential, allow_preview=True)


def _close_quietly(resource: Any | None) -> None:
    close = getattr(resource, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - closing must never mask the real error
            pass


def _status_code(error: Exception) -> int | None:
    status = getattr(error, "status_code", None)
    if status is not None:
        return status
    response = getattr(error, "response", None)
    return getattr(response, "status_code", None) if response is not None else None


def _translate_sdk_error(
    error: Exception,
    kind: AssetKind,
    name: str,
    version: str,
) -> FoundryAssetGatewayError:
    if isinstance(error, ClientAuthenticationError):
        return FoundryAssetAuthenticationError()
    if isinstance(error, ServiceRequestError):
        return FoundryAssetTransportError()
    if isinstance(error, ServiceResponseError):
        return FoundryAssetServiceError()
    if isinstance(error, HttpResponseError):
        status = _status_code(error)
        if status == 401:
            return FoundryAssetAuthenticationError()
        if status == 403:
            return FoundryAssetAuthorizationError()
        if status == 404:
            return FoundryAssetNotFoundError(kind, name, version)
        if status == 429:
            return FoundryAssetThrottledError()
        return FoundryAssetServiceError()
    if isinstance(error, AzureError):
        return FoundryAssetServiceError()
    return FoundryAssetUnexpectedSdkError()


# ---------------------------------------------------------------------------
# Resolution gateway (existing, already-published assets)
# ---------------------------------------------------------------------------


class FoundryAssetResolutionGateway:
    """Resolves an exact, already-published Foundry dataset or evaluator.

    Implements :class:`foundry_opt.optimization.assets.FoundryAssetResolutionGateway`.
    """

    def __init__(
        self,
        project_endpoint: str,
        credential_provider: AzureCredentialProvider,
        *,
        client_factory: Any = _create_client,
    ) -> None:
        self._project_endpoint = project_endpoint
        self._credential_provider = credential_provider
        self._client_factory = client_factory

    def resolve(self, *, kind: AssetKind, name: str, version: str) -> str:
        if kind not in (AssetKind.DATASET, AssetKind.EVALUATOR):
            raise ValueError(f"unsupported asset kind: {kind!r}")

        credential = None
        client = None
        try:
            credential = self._credential_provider.create()
            client = self._client_factory(self._project_endpoint, credential)
            try:
                if kind is AssetKind.DATASET:
                    record = client.datasets.get(name, version)
                    _verify_dataset_record(record, name, version)
                else:
                    record = client.beta.evaluators.get_version(name, version)
                    _verify_evaluator_record(record, name, version)
            except FoundryAssetGatewayError:
                raise
            except Exception as error:
                raise _translate_sdk_error(error, kind, name, version) from None

            remote_id = getattr(record, "id", None)
            if not remote_id:
                raise FoundryAssetIdentityMismatchError(kind, name, version)
            return remote_id
        finally:
            _close_quietly(client)
            _close_quietly(credential)


def _enum_value(value: Any) -> Any:
    """Normalize an SDK "known values" enum member to its plain string.

    ``azure-ai-projects`` model fields such as ``DatasetVersion.type`` and
    ``EvaluatorVersion.evaluator_type`` are typed as ``str | SomeEnum``: the
    live service may hand back either a plain ``str`` or an enum instance
    (e.g. ``EvaluatorType.CUSTOM``). Both compare equal to their string value
    via ``.value`` when it is present; plain strings pass through unchanged.
    """

    return getattr(value, "value", value)


def _verify_dataset_record(record: Any, name: str, version: str) -> None:
    dataset_type = _enum_value(getattr(record, "type", None))
    if (
        getattr(record, "name", None) != name
        or getattr(record, "version", None) != version
        or dataset_type not in ("uri_file", "uri_folder")
    ):
        raise FoundryAssetIdentityMismatchError(AssetKind.DATASET, name, version)


def _verify_evaluator_record(record: Any, name: str, version: str) -> None:
    evaluator_type = _enum_value(getattr(record, "evaluator_type", None))
    if (
        getattr(record, "name", None) != name
        or getattr(record, "version", None) != version
        or evaluator_type not in ("builtin", "custom")
    ):
        raise FoundryAssetIdentityMismatchError(AssetKind.EVALUATOR, name, version)


# ---------------------------------------------------------------------------
# Registration gateway (dataset upload + custom evaluator creation)
# ---------------------------------------------------------------------------


class EvaluationAssetRegistrationGateway:
    """Registers a prepared dataset or custom evaluator with Foundry.

    Implements
    :class:`foundry_opt.optimization.assets.EvaluationAssetRegistrationGateway`.
    Registration is idempotent by content hash: registering the same
    name/version with identical content succeeds (returning the existing
    identity) without a new remote write; registering the same name/version
    with *different* content raises
    :class:`FoundryAssetRegistrationConflictError` and never overwrites the
    existing asset.
    """

    def __init__(
        self,
        project_endpoint: str,
        credential_provider: AzureCredentialProvider,
        *,
        client_factory: Any = _create_client,
    ) -> None:
        self._project_endpoint = project_endpoint
        self._credential_provider = credential_provider
        self._client_factory = client_factory

    def register(
        self,
        *,
        kind: AssetKind,
        name: str,
        version: str,
        content: Mapping[Path, bytes],
    ) -> AssetIdentity:
        if kind is AssetKind.DATASET:
            path, data = _single_dataset_file(content)
            return self._register_dataset(name, version, path, data)
        if kind is AssetKind.EVALUATOR:
            path, data = _single_evaluator_file(content)
            definition, metadata = _parse_evaluator_definition(path, data)
            return self._register_evaluator(name, version, data, definition, metadata)
        raise ValueError(f"unsupported asset kind: {kind!r}")

    def _register_dataset(
        self,
        name: str,
        version: str,
        path: Path,
        data: bytes,
    ) -> AssetIdentity:
        content_sha256 = hashlib.sha256(data).hexdigest()
        credential = None
        client = None
        try:
            credential = self._credential_provider.create()
            client = self._client_factory(self._project_endpoint, credential)
            try:
                existing = _try_get_dataset(client, name, version)
                if existing is not None:
                    return _reconcile_existing_dataset(
                        existing, name, version, content_sha256
                    )

                try:
                    with tempfile.TemporaryDirectory() as scratch_dir:
                        scratch_path = Path(scratch_dir) / (
                            path.name or "dataset.jsonl"
                        )
                        scratch_path.write_bytes(data)
                        uploaded = client.datasets.upload_file(
                            name=name,
                            version=version,
                            file_path=str(scratch_path),
                        )
                    tagged = client.datasets.create_or_update(
                        name=name,
                        version=version,
                        dataset_version=FileDatasetVersion(
                            data_uri=uploaded.data_uri,
                            tags={_CONTENT_HASH_TAG: content_sha256},
                        ),
                    )
                    return _verify_registered_dataset(
                        tagged, name, version, content_sha256
                    )
                except Exception:
                    _delete_dataset_quietly(client, name, version)
                    raise
            except FoundryAssetGatewayError:
                raise
            except Exception as error:
                raise _translate_sdk_error(
                    error, AssetKind.DATASET, name, version
                ) from None
        finally:
            _close_quietly(client)
            _close_quietly(credential)

    def _register_evaluator(
        self,
        name: str,
        version: str,
        data: bytes,
        definition: EvaluatorDefinition,
        metadata: Mapping[str, Any],
    ) -> AssetIdentity:
        content_sha256 = hashlib.sha256(data).hexdigest()
        credential = None
        client = None
        try:
            credential = self._credential_provider.create()
            client = self._client_factory(self._project_endpoint, credential)
            try:
                existing = _find_existing_evaluator_version(
                    client, name, version, content_sha256
                )
                if existing is not None:
                    return existing

                created = client.beta.evaluators.create_version(
                    name,
                    EvaluatorVersion(
                        evaluator_type=EvaluatorType.CUSTOM,
                        categories=metadata["categories"],
                        definition=definition,
                        display_name=metadata.get("display_name"),
                        metadata={
                            _CONTENT_HASH_TAG: content_sha256,
                            _ASSET_VERSION_TAG: version,
                        },
                        supported_evaluation_levels=metadata.get(
                            "supported_evaluation_levels"
                        ),
                        tags={
                            _CONTENT_HASH_TAG: content_sha256,
                            _ASSET_VERSION_TAG: version,
                        },
                    ),
                )
                return _verify_registered_evaluator(
                    created, name, version, content_sha256
                )
            except FoundryAssetGatewayError:
                raise
            except Exception as error:
                raise _translate_sdk_error(
                    error, AssetKind.EVALUATOR, name, version
                ) from None
        finally:
            _close_quietly(client)
            _close_quietly(credential)


def _single_dataset_file(content: Mapping[Path, bytes]) -> tuple[Path, bytes]:
    if len(content) != 1:
        raise FoundryAssetContentError(
            "dataset registration requires exactly one prepared file"
        )
    ((path, data),) = content.items()
    if path.suffix.lower() != ".jsonl":
        raise FoundryAssetContentError(
            f"dataset file '{path}' must be a '.jsonl' file"
        )
    return path, data


def _single_evaluator_file(content: Mapping[Path, bytes]) -> tuple[Path, bytes]:
    if len(content) != 1:
        raise FoundryAssetContentError(
            "custom evaluator registration requires exactly one prepared file"
        )
    ((path, data),) = content.items()
    return path, data


def _try_get_dataset(client: Any, name: str, version: str) -> Any | None:
    try:
        return client.datasets.get(name, version)
    except HttpResponseError as error:
        if _status_code(error) == 404:
            return None
        raise


def _delete_dataset_quietly(client: Any, name: str, version: str) -> None:
    try:
        client.datasets.delete(name, version)
    except Exception:  # noqa: BLE001 - cleanup must not mask the real error
        pass


def _reconcile_existing_dataset(
    existing: Any,
    name: str,
    version: str,
    content_sha256: str,
) -> AssetIdentity:
    tags = getattr(existing, "tags", None) or {}
    if (
        getattr(existing, "name", None) != name
        or getattr(existing, "version", None) != version
        or not getattr(existing, "id", None)
    ):
        raise FoundryAssetIdentityMismatchError(AssetKind.DATASET, name, version)
    if tags.get(_CONTENT_HASH_TAG) != content_sha256:
        raise FoundryAssetRegistrationConflictError(AssetKind.DATASET, name, version)
    return AssetIdentity(
        remote_id=existing.id,
        name=name,
        version=version,
        content_sha256=content_sha256,
    )


def _verify_registered_dataset(
    record: Any,
    name: str,
    version: str,
    content_sha256: str,
) -> AssetIdentity:
    tags = getattr(record, "tags", None) or {}
    if (
        getattr(record, "name", None) != name
        or getattr(record, "version", None) != version
        or not getattr(record, "id", None)
        or tags.get(_CONTENT_HASH_TAG) != content_sha256
    ):
        raise FoundryAssetIdentityMismatchError(AssetKind.DATASET, name, version)
    return AssetIdentity(
        remote_id=record.id,
        name=name,
        version=version,
        content_sha256=content_sha256,
    )


def _find_existing_evaluator_version(
    client: Any,
    name: str,
    version: str,
    content_sha256: str,
) -> AssetIdentity | None:
    """Search for an evaluator version already tagged with our request.

    ``list_versions`` returns a lazily-paged ``ItemPaged``: the underlying
    HTTP call (and any 404 it raises for an unknown evaluator name) may not
    happen until the pager is actually iterated, not at the point
    ``list_versions`` is called. Materializing it with ``list(...)`` inside
    the same ``try`` block ensures a 404 raised during iteration is caught
    exactly like one raised eagerly.
    """

    try:
        candidates = list(client.beta.evaluators.list_versions(name))
    except HttpResponseError as error:
        if _status_code(error) == 404:
            return None
        raise

    for candidate in candidates:
        identity_metadata = _evaluator_identity_metadata(candidate)
        if identity_metadata.get(_ASSET_VERSION_TAG) != version:
            continue
        service_version = getattr(candidate, "version", None)
        if (
            getattr(candidate, "name", None) != name
            or not getattr(candidate, "id", None)
            or not service_version
        ):
            raise FoundryAssetIdentityMismatchError(
                AssetKind.EVALUATOR, name, version
            )
        if identity_metadata.get(_CONTENT_HASH_TAG) != content_sha256:
            raise FoundryAssetRegistrationConflictError(
                AssetKind.EVALUATOR, name, version
            )
        return AssetIdentity(
            remote_id=candidate.id,
            name=name,
            version=service_version,
            requested_version=version,
            content_sha256=content_sha256,
        )
    return None


def _verify_registered_evaluator(
    record: Any,
    name: str,
    version: str,
    content_sha256: str,
) -> AssetIdentity:
    """Verify a freshly created evaluator version and return its identity.

    The Foundry service auto-increments an evaluator's own ``version`` field
    on ``create_version`` (it cannot be pinned to our deterministic
    ``version`` string), so the readback is verified against: the requested
    ``name``; a non-empty remote ``id``; a non-empty *actual* service
    ``version`` (proving the service really minted a version, without ever
    claiming that value equals our requested version); our
    ``_ASSET_VERSION_TAG`` tag echoing the requested version; and our
    ``_CONTENT_HASH_TAG`` tag. The returned :class:`AssetIdentity` carries
    the service's actual ``version`` (remotely resolvable, e.g. via
    ``client.beta.evaluators.get_version(name, version)``) plus our own
    logical ``requested_version``, which is what
    :func:`foundry_opt.optimization.assets.materialize_prepared_asset`
    compares against the prepared asset's deterministic version.
    """

    identity_metadata = _evaluator_identity_metadata(record)
    service_version = getattr(record, "version", None)
    if (
        getattr(record, "name", None) != name
        or not getattr(record, "id", None)
        or not service_version
        or identity_metadata.get(_ASSET_VERSION_TAG) != version
        or identity_metadata.get(_CONTENT_HASH_TAG) != content_sha256
    ):
        raise FoundryAssetIdentityMismatchError(AssetKind.EVALUATOR, name, version)
    return AssetIdentity(
        remote_id=record.id,
        name=name,
        version=service_version,
        requested_version=version,
        content_sha256=content_sha256,
    )


def _evaluator_identity_metadata(record: Any) -> dict[str, Any]:
    tags = getattr(record, "tags", None) or {}
    metadata = getattr(record, "metadata", None) or {}
    return {**tags, **metadata}



# ---------------------------------------------------------------------------
# Custom evaluator definition parsing
# ---------------------------------------------------------------------------


def _parse_evaluator_definition(
    path: Path,
    data: bytes,
) -> tuple[EvaluatorDefinition, dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return _parse_prompt_evaluator_json(data)
    if suffix == ".py":
        return _parse_code_evaluator_python(data, path)
    raise FoundryAssetContentError(
        f"custom evaluator file '{path}' must be a '.json' prompt evaluator "
        "spec or a '.py' code evaluator"
    )


def _parse_prompt_evaluator_json(
    data: bytes,
) -> tuple[EvaluatorDefinition, dict[str, Any]]:
    spec = _decode_json_object(data, "custom evaluator JSON definition")
    kind = spec.get("kind")
    if kind == "prompt":
        return _parse_repo_prompt_spec(spec)
    if kind == "rubric":
        return _parse_repo_rubric_spec(spec)
    if kind is not None:
        raise FoundryAssetContentError(
            f"unsupported custom evaluator JSON 'kind': {kind!r}"
        )

    definition_type = spec.get("definition_type", "prompt")
    if definition_type != "prompt":
        raise FoundryAssetContentError(
            "custom evaluator JSON definitions must set 'definition_type' to "
            "'prompt'"
        )
    prompt_text = spec.get("prompt_text")
    if not isinstance(prompt_text, str) or not prompt_text.strip():
        raise FoundryAssetContentError(
            "custom prompt evaluator definitions require a non-empty "
            "'prompt_text'"
        )
    metadata = _evaluator_metadata(spec)
    definition = PromptBasedEvaluatorDefinition(
        prompt_text=prompt_text,
        init_parameters=_optional_dict(spec.get("init_parameters")),
        data_schema=_optional_dict(spec.get("data_schema")),
        metrics=_parse_metrics(spec.get("metrics")),
    )
    return definition, metadata


_METRIC_DIRECTION_KEYWORDS = {
    "maximize": "increase",
    "increase": "increase",
    "minimize": "decrease",
    "decrease": "decrease",
    "neutral": "neutral",
}

_DEFAULT_EVALUATOR_CATEGORY = "quality"


def _repo_metric_direction(spec: Mapping[str, Any]) -> str | None:
    desirable_direction = spec.get("desirable_direction")
    if isinstance(desirable_direction, str) and desirable_direction:
        return desirable_direction
    direction = spec.get("direction")
    if direction is None:
        return None
    normalized = str(direction).strip().lower()
    mapped = _METRIC_DIRECTION_KEYWORDS.get(normalized)
    if mapped is None:
        raise FoundryAssetContentError(
            f"unsupported custom evaluator metric direction: {direction!r}"
        )
    return mapped


def _repo_metric_scale(spec: Mapping[str, Any]) -> tuple[float | None, float | None]:
    scale = spec.get("scale")
    if scale is None:
        return None, None
    if not isinstance(scale, dict):
        raise FoundryAssetContentError("custom evaluator 'scale' must be a JSON object")
    return scale.get("minimum"), scale.get("maximum")


def _repo_metric(spec: Mapping[str, Any]) -> EvaluatorMetric:
    min_value, max_value = _repo_metric_scale(spec)
    return EvaluatorMetric(
        type=spec.get("scoring_type") or spec.get("type"),
        desirable_direction=_repo_metric_direction(spec),
        min_value=min_value,
        max_value=max_value,
        threshold=spec.get("threshold"),
        is_primary=spec.get("is_primary"),
    )


def _repo_metric_name(spec: Mapping[str, Any]) -> str:
    metric_name = spec.get("metric_name") or spec.get("metric")
    if not isinstance(metric_name, str) or not metric_name:
        raise FoundryAssetContentError(
            "custom evaluator definitions require a non-empty 'metric' or "
            "'metric_name'"
        )
    return metric_name


def _repo_categories(spec: Mapping[str, Any]) -> list[str]:
    category = spec.get("category")
    if isinstance(category, str) and category:
        return _parse_categories([category])
    categories = spec.get("categories")
    if categories is not None:
        return _parse_categories(categories)
    return [_DEFAULT_EVALUATOR_CATEGORY]


def _repo_metadata(spec: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "display_name": spec.get("display_name") or spec.get("name"),
        "categories": _repo_categories(spec),
        "supported_evaluation_levels": _parse_levels(
            spec.get("supported_evaluation_levels")
        ),
    }


def _parse_repo_prompt_spec(
    spec: Mapping[str, Any],
) -> tuple[EvaluatorDefinition, dict[str, Any]]:
    """Parse the repository acceptance ``kind: "prompt"`` evaluator shape.

    A single ``metric``/``metric_name`` plus ``direction``/
    ``desirable_direction``/``threshold``/``scale`` are normalized into one
    :class:`EvaluatorMetric`; ``prompt_text`` and ``data_schema`` are used
    verbatim.
    """

    prompt_text = spec.get("prompt_text")
    if not isinstance(prompt_text, str) or not prompt_text.strip():
        raise FoundryAssetContentError(
            "custom prompt evaluator definitions require a non-empty "
            "'prompt_text'"
        )
    metric_name = _repo_metric_name(spec)
    definition = PromptBasedEvaluatorDefinition(
        prompt_text=prompt_text,
        init_parameters=_optional_dict(spec.get("init_parameters")),
        data_schema=_optional_dict(spec.get("data_schema")),
        metrics={metric_name: _repo_metric(spec)},
    )
    return definition, _repo_metadata(spec)


def _parse_rubric_dimension(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise FoundryAssetContentError("each rubric 'dimensions' entry must be a JSON object")
    name = raw.get("name")
    weight = raw.get("weight")
    criteria = raw.get("criteria")
    if not isinstance(name, str) or not name:
        raise FoundryAssetContentError("each rubric dimension requires a non-empty 'name'")
    if not isinstance(weight, (int, float)) or isinstance(weight, bool):
        raise FoundryAssetContentError(
            f"rubric dimension '{name}' requires a numeric 'weight'"
        )
    if not isinstance(criteria, str) or not criteria.strip():
        raise FoundryAssetContentError(
            f"rubric dimension '{name}' requires non-empty 'criteria'"
        )
    return {"name": name, "weight": weight, "criteria": criteria}


def _compile_rubric_prompt_text(
    display_name: str,
    metric_name: str,
    dimensions: list[dict[str, Any]],
) -> str:
    """Deterministically compile a rubric's dimensions into a prompt.

    Dimension order is preserved from the source spec (already a
    deterministic ordering), so the same rubric spec always compiles to the
    exact same ``prompt_text`` and therefore the same content hash.
    """

    lines = [
        f"You are grading a response for the weighted rubric "
        f"'{display_name}'. Score each dimension below from 0.0 (fails) to "
        f"1.0 (fully satisfies), then combine the dimension scores using "
        f"their weights to produce the final '{metric_name}' score.",
        "Query: {{query}}",
        "Response: {{response}}",
        "Dimensions:",
    ]
    for dimension in dimensions:
        lines.append(
            f"- {dimension['name']} (weight {dimension['weight']}): "
            f"{dimension['criteria']}"
        )
    return "\n".join(lines)


def _parse_repo_rubric_spec(
    spec: Mapping[str, Any],
) -> tuple[EvaluatorDefinition, dict[str, Any]]:
    """Parse the repository acceptance ``kind: "rubric"`` evaluator shape.

    Compiles a deterministic ``prompt_text`` from the weighted
    ``dimensions`` list and constructs a single-metric prompt evaluator; no
    ``data_schema`` is accepted from the rubric spec itself (it declares
    none) so a minimal, non-conflicting ``{query, response}`` object schema
    is generated instead.
    """

    raw_dimensions = spec.get("dimensions")
    if not isinstance(raw_dimensions, list) or not raw_dimensions:
        raise FoundryAssetContentError(
            "rubric evaluator definitions require a non-empty 'dimensions' "
            "list"
        )
    dimensions = [_parse_rubric_dimension(entry) for entry in raw_dimensions]
    metric_name = _repo_metric_name(spec)
    display_name = spec.get("display_name") or spec.get("name") or metric_name
    prompt_text = _compile_rubric_prompt_text(display_name, metric_name, dimensions)
    definition = PromptBasedEvaluatorDefinition(
        prompt_text=prompt_text,
        init_parameters=_optional_dict(spec.get("init_parameters")),
        data_schema={
            "type": "object",
            "required": ["query", "response"],
            "properties": {
                "query": {"type": "string"},
                "response": {"type": "string"},
            },
        },
        metrics={metric_name: _repo_metric(spec)},
    )
    return definition, _repo_metadata(spec)


def _parse_code_evaluator_python(
    data: bytes,
    path: Path,
) -> tuple[EvaluatorDefinition, dict[str, Any]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise FoundryAssetContentError(
            "custom evaluator Python source is not valid UTF-8"
        ) from error
    first_line, _, _ = text.partition("\n")
    if not first_line.startswith(_CODE_METADATA_PREFIX):
        raise FoundryAssetContentError(
            "custom Python evaluator definitions must begin with a "
            f"'{_CODE_METADATA_PREFIX}' JSON metadata comment"
        )
    raw_metadata = first_line[len(_CODE_METADATA_PREFIX) :].strip()
    spec = _decode_json_object(
        raw_metadata.encode("utf-8"),
        "custom Python evaluator metadata comment",
    )
    metadata = _evaluator_metadata(spec)
    definition = CodeBasedEvaluatorDefinition(
        code_text=text,
        entry_point=spec.get("entry_point") or path.name,
        init_parameters=_optional_dict(spec.get("init_parameters")),
        data_schema=_optional_dict(spec.get("data_schema")),
        metrics=_parse_metrics(spec.get("metrics")),
    )
    return definition, metadata


def _evaluator_metadata(spec: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "display_name": spec.get("display_name"),
        "categories": _parse_categories(spec.get("categories")),
        "supported_evaluation_levels": _parse_levels(
            spec.get("supported_evaluation_levels")
        ),
    }


def _decode_json_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        spec = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FoundryAssetContentError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(spec, dict):
        raise FoundryAssetContentError(f"{label} must be a JSON object")
    return spec


def _optional_dict(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise FoundryAssetContentError("expected a JSON object")
    return value


def _parse_metrics(raw: Any) -> dict[str, EvaluatorMetric]:
    if not isinstance(raw, dict) or not raw:
        raise FoundryAssetContentError(
            "custom evaluator definitions require a non-empty 'metrics' object"
        )
    metrics: dict[str, EvaluatorMetric] = {}
    for metric_name, metric_spec in raw.items():
        if not isinstance(metric_name, str) or not metric_name:
            raise FoundryAssetContentError(
                "custom evaluator metric names must be non-empty strings"
            )
        if not isinstance(metric_spec, dict):
            raise FoundryAssetContentError(
                f"custom evaluator metric '{metric_name}' must be a JSON object"
            )
        metrics[metric_name] = EvaluatorMetric(
            type=metric_spec.get("type"),
            desirable_direction=metric_spec.get("desirable_direction"),
            min_value=metric_spec.get("min_value"),
            max_value=metric_spec.get("max_value"),
            threshold=metric_spec.get("threshold"),
            is_primary=metric_spec.get("is_primary"),
        )
    return metrics


def _parse_categories(raw: Any) -> list[str]:
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(item, str) and item for item in raw)
    ):
        raise FoundryAssetContentError(
            "custom evaluator definitions require a non-empty 'categories' list"
        )
    return list(raw)


def _parse_levels(raw: Any) -> list[str] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(
        isinstance(item, str) and item for item in raw
    ):
        raise FoundryAssetContentError(
            "'supported_evaluation_levels' must be a list of strings"
        )
    return list(raw)
