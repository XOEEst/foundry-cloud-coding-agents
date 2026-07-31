"""Evaluation asset providers, registry, and registration gateway.

This module implements the concrete evaluation asset pipeline described by
the shared contracts in :mod:`foundry_opt.optimization.models`:

* a registry of :class:`~foundry_opt.optimization.models.EvaluationAssetProvider`
  implementations keyed by an open-ended ``source`` string,
* providers for existing Foundry assets, repository-tracked files,
  deterministic synthetic datasets, human-reviewed trace datasets, and
  custom repository-defined evaluators, and
* a materialization step that registers prepared, non-trace assets with a
  typed registration gateway after a spec has been approved.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Protocol

from foundry_opt.optimization.models import (
    ApprovalGate,
    AssetKind,
    AssetProvenance,
    EvaluationAssetContext,
    EvaluationAssetProvider,
    EvaluationAssetRequest,
    PreparedEvaluationAsset,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EvaluationAssetError(RuntimeError):
    """Base class for evaluation asset preparation and registration failures."""


class UnsafeAssetPathError(EvaluationAssetError):
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            f"asset path '{path}' is not a safe repository-relative file"
        )


class MissingAssetFileError(EvaluationAssetError):
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            f"asset path '{path}' does not exist in the repository"
        )


class AssetNotFoundError(EvaluationAssetError):
    def __init__(self, kind: AssetKind, name: str, version: str) -> None:
        self.kind = kind
        self.name = name
        self.version = version
        super().__init__(
            f"no {kind.value} named '{name}' at version '{version}' was "
            "found in Foundry"
        )


class HumanReviewRequired(EvaluationAssetError):
    """Raised before any raw trace rows are read, committed, or registered."""

    def __init__(self, request: EvaluationAssetRequest) -> None:
        self.asset_id = request.asset_id
        self.source = request.source
        super().__init__(
            f"trace asset '{request.asset_id}' requires human review before "
            "trace rows may be read, committed, or registered; autopilot is "
            "not permitted for trace-derived assets"
        )


class TraceAssetRegistrationBlockedError(EvaluationAssetError):
    def __init__(self, asset_id: str) -> None:
        self.asset_id = asset_id
        super().__init__(
            f"trace asset '{asset_id}' must not be registered without "
            "human review"
        )


class AssetIdentityMismatchError(EvaluationAssetError):
    def __init__(self, asset_id: str) -> None:
        self.asset_id = asset_id
        super().__init__(
            "the registration gateway returned an identity that does not "
            f"match the prepared asset '{asset_id}'"
        )


class UnknownEvaluationAssetProviderError(EvaluationAssetError):
    def __init__(self, source: str) -> None:
        self.source = source
        super().__init__(
            f"no evaluation asset provider is registered for source "
            f"'{source}'"
        )


class DuplicateEvaluationAssetProviderError(EvaluationAssetError):
    def __init__(self, source: str) -> None:
        self.source = source
        super().__init__(
            f"a provider is already registered for source '{source}'"
        )


# ---------------------------------------------------------------------------
# Repository file access
# ---------------------------------------------------------------------------


def _read_repository_relative_file(
    repository_root: Path,
    relative_path: Path,
) -> bytes:
    """Read canonical bytes for ``relative_path`` under ``repository_root``.

    Rejects symlinked path components, paths that resolve outside of
    ``repository_root``, and missing files. UTF-8 text uses LF line endings so
    its provenance hash is stable across Git checkouts on Windows and Linux.
    """

    root = Path(repository_root)
    current = root
    for part in relative_path.parts:
        current = current / part
        if current.is_symlink():
            raise UnsafeAssetPathError(relative_path)

    try:
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        raise MissingAssetFileError(relative_path) from error

    resolved_file = (root / relative_path).resolve(strict=False)
    if resolved_root != resolved_file and resolved_root not in resolved_file.parents:
        raise UnsafeAssetPathError(relative_path)

    if not current.is_file():
        raise MissingAssetFileError(relative_path)

    return canonicalize_repository_asset_content(current.read_bytes())


def canonicalize_repository_asset_content(content: bytes) -> bytes:
    """Normalize UTF-8 text line endings without changing binary content."""

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return content
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def _require_source(request: EvaluationAssetRequest, source_type: str) -> None:
    if request.source != source_type:
        raise ValueError(
            f"a '{source_type}' provider cannot prepare source "
            f"'{request.source}'"
        )


# ---------------------------------------------------------------------------
# Foundry asset resolution gateway + provider
# ---------------------------------------------------------------------------


class FoundryAssetResolutionGateway(Protocol):
    """Resolves an exact, published Foundry asset to a pinned remote id."""

    def resolve(
        self,
        *,
        kind: AssetKind,
        name: str,
        version: str,
    ) -> str: ...


@dataclass(frozen=True)
class ExistingFoundryAssetProvider:
    """Pins an already-published Foundry dataset or evaluator by identity."""

    gateway: FoundryAssetResolutionGateway
    source_type: str = "foundry"

    def prepare(
        self,
        request: EvaluationAssetRequest,
        context: EvaluationAssetContext,
    ) -> PreparedEvaluationAsset:
        _require_source(request, self.source_type)
        if not request.name or not request.version:
            raise ValueError(
                "existing Foundry assets require an exact name and version"
            )
        remote_id = self.gateway.resolve(
            kind=request.kind,
            name=request.name,
            version=request.version,
        )
        if not remote_id:
            raise AssetNotFoundError(request.kind, request.name, request.version)
        provenance = AssetProvenance(
            asset_id=request.asset_id,
            kind=request.kind,
            source=request.source,
            role=request.role,
            name=request.name,
            version=request.version,
            content_sha256=None,
            created_by="foundry-existing-asset-provider",
            approval_gate=request.approval_gate,
            remote_id=remote_id,
            metrics=request.metrics,
        )
        return PreparedEvaluationAsset(provenance=provenance, files={})


# ---------------------------------------------------------------------------
# Repository asset provider
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepositoryAssetProvider:
    """Reads a single validated repository-relative file byte-for-byte."""

    source_type: str = "repository"

    def prepare(
        self,
        request: EvaluationAssetRequest,
        context: EvaluationAssetContext,
    ) -> PreparedEvaluationAsset:
        _require_source(request, self.source_type)
        if request.path is None:
            raise ValueError("repository assets require a path")
        content = _read_repository_relative_file(
            context.repository_root, request.path
        )
        content_sha256 = hashlib.sha256(content).hexdigest()
        provenance = AssetProvenance(
            asset_id=request.asset_id,
            kind=request.kind,
            source=request.source,
            role=request.role,
            name=request.name,
            version=request.version,
            content_sha256=content_sha256,
            created_by="repository-asset-provider",
            approval_gate=request.approval_gate,
            metrics=request.metrics,
        )
        return PreparedEvaluationAsset(
            provenance=provenance,
            files={request.path: content},
        )


# ---------------------------------------------------------------------------
# Deterministic synthetic dataset provider
# ---------------------------------------------------------------------------


_SYNTHETIC_DATASET_DIRECTORY = Path(".foundry/datasets")


def _canonical_json_line(row: Mapping[str, Any]) -> str:
    return json.dumps(
        row,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


@dataclass(frozen=True)
class SyntheticDatasetProvider:
    """Generates a deterministic JSONL dataset from explicit request rows.

    Rows are supplied by the caller through ``request.parameters['rows']``;
    this provider never invokes an external model. ``max_rows`` bounds the
    number of rows a single request may materialize.
    """

    max_rows: int = 200
    source_type: str = "synthetic"

    def __post_init__(self) -> None:
        if self.max_rows < 1:
            raise ValueError("max_rows must be positive")

    def prepare(
        self,
        request: EvaluationAssetRequest,
        context: EvaluationAssetContext,
    ) -> PreparedEvaluationAsset:
        _require_source(request, self.source_type)
        row_count = request.parameters.get("row_count")
        rows = request.parameters.get("rows")
        if not isinstance(rows, list) or not rows:
            raise ValueError(
                "synthetic datasets require a non-empty 'rows' parameter"
            )
        if len(rows) != row_count:
            raise ValueError(
                "synthetic dataset row_count does not match the number of "
                "supplied rows"
            )
        if len(rows) > self.max_rows:
            raise ValueError(
                "synthetic dataset rows exceed the configured row limit "
                f"({self.max_rows})"
            )
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(f"synthetic dataset row {index} must be an object")
            query = row.get("query")
            expected_behavior = row.get("expected_behavior")
            if not isinstance(query, str) or not query.strip():
                raise ValueError(
                    f"synthetic dataset row {index} requires a non-empty "
                    "'query'"
                )
            if not isinstance(expected_behavior, str) or not expected_behavior.strip():
                raise ValueError(
                    f"synthetic dataset row {index} requires a non-empty "
                    "'expected_behavior'"
                )

        content = "".join(f"{_canonical_json_line(row)}\n" for row in rows)
        content_bytes = content.encode("utf-8")
        content_sha256 = hashlib.sha256(content_bytes).hexdigest()
        path = _SYNTHETIC_DATASET_DIRECTORY / f"{request.asset_id}.jsonl"

        provenance = AssetProvenance(
            asset_id=request.asset_id,
            kind=request.kind,
            source=request.source,
            role=request.role,
            name=request.name,
            version=request.version,
            content_sha256=content_sha256,
            created_by="synthetic-dataset-provider",
            approval_gate=request.approval_gate,
            metrics=request.metrics,
        )
        return PreparedEvaluationAsset(
            provenance=provenance,
            files={path: content_bytes},
        )


# ---------------------------------------------------------------------------
# Trace dataset provider
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TraceEvaluationAssetProvider:
    """Materialize only privacy-safe trace metadata for human review."""

    source_type: str = "trace"

    def prepare(
        self,
        request: EvaluationAssetRequest,
        context: EvaluationAssetContext,
    ) -> PreparedEvaluationAsset:
        _require_source(request, self.source_type)
        return PreparedEvaluationAsset(
            provenance=AssetProvenance(
                asset_id=request.asset_id,
                kind=request.kind,
                source=request.source,
                role=request.role,
                name=request.name,
                version=request.version,
                content_sha256=None,
                created_by="trace-metadata-provider",
                approval_gate=request.approval_gate,
                metrics=request.metrics,
            ),
            files={},
        )


# ---------------------------------------------------------------------------
# Custom evaluator provider
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CustomEvaluatorAssetProvider:
    """Hashes and prepares a repository-defined custom evaluator."""

    source_type: str = "custom"

    def prepare(
        self,
        request: EvaluationAssetRequest,
        context: EvaluationAssetContext,
    ) -> PreparedEvaluationAsset:
        _require_source(request, self.source_type)
        if request.kind is not AssetKind.EVALUATOR:
            raise ValueError("custom evaluator assets must be evaluators")
        if not request.metrics:
            raise ValueError("custom evaluator assets require metrics")
        if request.path is None:
            raise ValueError(
                "custom evaluator assets require a repository-relative "
                "evaluator definition path"
            )
        content = _read_repository_relative_file(
            context.repository_root, request.path
        )
        content_sha256 = hashlib.sha256(content).hexdigest()
        provenance = AssetProvenance(
            asset_id=request.asset_id,
            kind=request.kind,
            source=request.source,
            role=None,
            name=request.name,
            version=request.version,
            content_sha256=content_sha256,
            created_by="custom-evaluator-provider",
            approval_gate=request.approval_gate,
            metrics=request.metrics,
        )
        return PreparedEvaluationAsset(
            provenance=provenance,
            files={request.path: content},
        )


# ---------------------------------------------------------------------------
# Builtin evaluator provider
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BuiltinEvaluatorProvider:
    """Pins a built-in, shipped evaluator by its exact name and version.

    Builtin evaluators ship with foundry-opt itself, so they never need
    registration or human review: their remote identity is deterministic
    (``builtin:<name>:<version>``), no files are produced, and the resulting
    provenance is always policy-approved.
    """

    source_type: str = "builtin"

    def prepare(
        self,
        request: EvaluationAssetRequest,
        context: EvaluationAssetContext,
    ) -> PreparedEvaluationAsset:
        _require_source(request, self.source_type)
        if request.kind is not AssetKind.EVALUATOR:
            raise ValueError("builtin evaluator assets must be evaluators")
        if not request.name or not request.version:
            raise ValueError(
                "builtin evaluator assets require an exact name and version"
            )
        if not request.metrics:
            raise ValueError("builtin evaluator assets require metrics")
        if request.approval_gate is not ApprovalGate.POLICY:
            raise ValueError(
                "builtin evaluator assets only support policy approval"
            )
        remote_id = f"builtin:{request.name}:{request.version}"
        provenance = AssetProvenance(
            asset_id=request.asset_id,
            kind=request.kind,
            source=request.source,
            role=None,
            name=request.name,
            version=request.version,
            content_sha256=None,
            created_by="builtin-evaluator-provider",
            approval_gate=ApprovalGate.POLICY,
            remote_id=remote_id,
            metrics=request.metrics,
        )
        return PreparedEvaluationAsset(provenance=provenance, files={})


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------


class EvaluationAssetProviderRegistry:
    """Registry of :class:`EvaluationAssetProvider` keyed by source string."""

    def __init__(self) -> None:
        self._providers: dict[str, EvaluationAssetProvider] = {}

    def register(self, provider: EvaluationAssetProvider) -> None:
        source = provider.source_type
        if not source:
            raise ValueError("provider source_type is required")
        if source in self._providers:
            raise DuplicateEvaluationAssetProviderError(source)
        self._providers[source] = provider

    def get(self, source: str) -> EvaluationAssetProvider:
        try:
            return self._providers[source]
        except KeyError:
            raise UnknownEvaluationAssetProviderError(source) from None

    def prepare(
        self,
        request: EvaluationAssetRequest,
        context: EvaluationAssetContext,
    ) -> PreparedEvaluationAsset:
        return self.get(request.source).prepare(request, context)

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(self._providers)


def build_default_registry(
    foundry_gateway: FoundryAssetResolutionGateway,
    *,
    synthetic_max_rows: int = 200,
) -> EvaluationAssetProviderRegistry:
    """Builds a registry wired with all built-in evaluation asset providers."""

    registry = EvaluationAssetProviderRegistry()
    registry.register(ExistingFoundryAssetProvider(gateway=foundry_gateway))
    registry.register(RepositoryAssetProvider())
    registry.register(SyntheticDatasetProvider(max_rows=synthetic_max_rows))
    registry.register(TraceEvaluationAssetProvider())
    registry.register(CustomEvaluatorAssetProvider())
    registry.register(BuiltinEvaluatorProvider())
    return registry


# ---------------------------------------------------------------------------
# Registration gateway + materialization
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssetIdentity:
    """The identity a registration gateway assigns to a registered asset.

    ``version`` is the asset's actual, remotely resolvable version — the
    value a resolution gateway would use to look the exact identity back up
    (e.g. via ``client.datasets.get(name, version)`` or
    ``client.beta.evaluators.get_version(name, version)``). For datasets
    this always equals the requested deterministic version, since Foundry
    lets a dataset version be pinned explicitly on create. For evaluators,
    whose versions are minted (auto-incremented) by the service, this is
    the literal version the service assigned, which will generally differ
    from the deterministic version that was requested.

    ``requested_version`` is the logical, content-derived version that was
    asked for at registration time. It defaults to ``None``, meaning "the
    same as ``version``" (via :attr:`effective_requested_version`) — the
    behavior every caller relied on before evaluators needed to distinguish
    the two. Registration gateways for asset kinds whose actual version can
    differ from the request (currently just evaluators) must set it
    explicitly.
    """

    remote_id: str
    name: str
    version: str
    requested_version: str | None = None
    content_sha256: str | None = None

    @property
    def effective_requested_version(self) -> str:
        """The requested version, falling back to ``version`` when unset."""

        return (
            self.requested_version
            if self.requested_version is not None
            else self.version
        )


class EvaluationAssetRegistrationGateway(Protocol):
    """Registers a prepared asset's files under a deterministic name/version."""

    def register(
        self,
        *,
        kind: AssetKind,
        name: str,
        version: str,
        content: Mapping[Path, bytes],
    ) -> AssetIdentity: ...


def deterministic_asset_name(provenance: AssetProvenance) -> str:
    base = f"{provenance.kind.value}-{provenance.asset_id}"
    if provenance.kind is AssetKind.EVALUATOR:
        if provenance.content_sha256 is None:
            raise ValueError(
                "prepared evaluators require a content hash before "
                "registration"
            )
        suffix = provenance.content_sha256[:12]
        return f"{base[:114]}-{suffix}"
    return base


def deterministic_asset_version(provenance: AssetProvenance) -> str:
    if provenance.content_sha256 is None:
        raise ValueError(
            "prepared assets require a content hash before registration"
        )
    return provenance.content_sha256[:16]


def materialize_prepared_asset(
    prepared: PreparedEvaluationAsset,
    gateway: EvaluationAssetRegistrationGateway,
) -> AssetProvenance:
    """Registers a prepared asset with Foundry after spec approval.

    Assets already pinned to a remote id (existing Foundry assets) are
    returned unchanged. Trace-derived assets are always blocked, since they
    must be routed through human review instead of automatic registration.
    Otherwise, the asset is registered under a deterministic name/version:
    the gateway's returned identity is first verified against the *request*
    (``identity.effective_requested_version`` must match the deterministic
    version that was asked for — for datasets this is ``identity.version``
    itself; for evaluators it is the separate, logical
    ``identity.requested_version`` tag, since the service mints its own
    literal evaluator version). The updated provenance is then returned
    with ``remote_id`` *and* ``version`` set to the identity's actual,
    remotely resolvable values — never the deterministic request value for
    evaluators — so a later resolution gateway call can look the exact
    asset back up.
    """

    provenance = prepared.provenance
    if provenance.source == "trace":
        raise TraceAssetRegistrationBlockedError(provenance.asset_id)
    if provenance.remote_id is not None:
        return provenance

    name = deterministic_asset_name(provenance)
    requested_version = deterministic_asset_version(provenance)
    identity = gateway.register(
        kind=provenance.kind,
        name=name,
        version=requested_version,
        content=prepared.files,
    )
    if (
        not identity.remote_id
        or not identity.version
        or identity.name != name
        or identity.effective_requested_version != requested_version
        or (
            identity.content_sha256 is not None
            and identity.content_sha256 != provenance.content_sha256
        )
    ):
        raise AssetIdentityMismatchError(provenance.asset_id)

    return provenance.model_copy(
        update={
            "name": identity.name,
            "remote_id": identity.remote_id,
            "version": identity.version,
        }
    )
