from __future__ import annotations

from pathlib import Path

import pytest

from foundry_opt.optimization.assets import (
    AssetIdentity,
    AssetIdentityMismatchError,
    AssetNotFoundError,
    BuiltinEvaluatorProvider,
    CustomEvaluatorAssetProvider,
    DuplicateEvaluationAssetProviderError,
    EvaluationAssetProviderRegistry,
    ExistingFoundryAssetProvider,
    HumanReviewRequired,
    MissingAssetFileError,
    RepositoryAssetProvider,
    SyntheticDatasetProvider,
    TraceAssetRegistrationBlockedError,
    TraceEvaluationAssetProvider,
    UnknownEvaluationAssetProviderError,
    UnsafeAssetPathError,
    build_default_registry,
    materialize_prepared_asset,
)
from foundry_opt.optimization.models import (
    ApprovalGate,
    AssetKind,
    AssetProvenance,
    EvaluationAssetContext,
    EvaluationAssetRequest,
    PreparedEvaluationAsset,
)


_PROJECT_ENDPOINT = "https://example.services.ai.azure.com/api/projects/demo"


def _context(repository_root: Path) -> EvaluationAssetContext:
    return EvaluationAssetContext(
        repository_root=repository_root,
        project_endpoint=_PROJECT_ENDPOINT,
        target="support-agent",
        issue_number=42,
    )


def _request(**overrides: object) -> EvaluationAssetRequest:
    values: dict[str, object] = {
        "asset_id": "development",
        "kind": AssetKind.DATASET,
        "source": "repository",
        "role": "development",
        "path": Path("datasets/development.jsonl"),
    }
    values.update(overrides)
    return EvaluationAssetRequest(**values)


def _hostile_request(**overrides: object) -> EvaluationAssetRequest:
    """Builds a request bypassing pydantic validators (defense-in-depth)."""

    values: dict[str, object] = {
        "asset_id": "development",
        "kind": AssetKind.DATASET,
        "source": "repository",
        "role": "development",
        "name": None,
        "version": None,
        "path": Path("datasets/development.jsonl"),
        "metrics": (),
        "parameters": {},
        "approval_gate": ApprovalGate.POLICY,
    }
    values.update(overrides)
    return EvaluationAssetRequest.model_construct(**values)


# ---------------------------------------------------------------------------
# Existing Foundry asset provider
# ---------------------------------------------------------------------------


class _FakeFoundryGateway:
    def __init__(self, remote_id: str | None) -> None:
        self.remote_id = remote_id
        self.calls: list[tuple[AssetKind, str, str]] = []

    def resolve(self, *, kind: AssetKind, name: str, version: str) -> str:
        self.calls.append((kind, name, version))
        return self.remote_id or ""


def test_existing_foundry_provider_pins_remote_id_with_no_files(tmp_path: Path) -> None:
    gateway = _FakeFoundryGateway(remote_id="foundry-remote-123")
    provider = ExistingFoundryAssetProvider(gateway=gateway)
    request = _request(
        source="foundry",
        name="support-development",
        version="v1",
        path=None,
    )

    prepared = provider.prepare(request, _context(tmp_path))

    assert prepared.provenance.remote_id == "foundry-remote-123"
    assert prepared.provenance.content_sha256 is None
    assert prepared.provenance.metrics == ()
    assert prepared.files == {}
    assert gateway.calls == [(AssetKind.DATASET, "support-development", "v1")]


def test_existing_foundry_provider_raises_when_asset_not_found(tmp_path: Path) -> None:
    gateway = _FakeFoundryGateway(remote_id=None)
    provider = ExistingFoundryAssetProvider(gateway=gateway)
    request = _request(
        source="foundry",
        name="support-development",
        version="v1",
        path=None,
    )

    with pytest.raises(AssetNotFoundError):
        provider.prepare(request, _context(tmp_path))


def test_existing_foundry_provider_rejects_mismatched_source(tmp_path: Path) -> None:
    provider = ExistingFoundryAssetProvider(gateway=_FakeFoundryGateway("id"))
    request = _request()

    with pytest.raises(ValueError, match="cannot prepare source"):
        provider.prepare(request, _context(tmp_path))


def test_existing_foundry_provider_copies_request_metrics_for_evaluators(
    tmp_path: Path,
) -> None:
    """Dataset provenance always has an empty ``metrics`` tuple, while
    evaluator provenance copies the requested metric identifiers exactly."""

    gateway = _FakeFoundryGateway(remote_id="foundry-remote-eval")
    provider = ExistingFoundryAssetProvider(gateway=gateway)
    request = _request(
        kind=AssetKind.EVALUATOR,
        source="foundry",
        role=None,
        name="support-evaluator",
        version="v1",
        path=None,
        metrics=("quality", "safety"),
    )

    prepared = provider.prepare(request, _context(tmp_path))

    assert prepared.provenance.metrics == ("quality", "safety")


# ---------------------------------------------------------------------------
# Repository asset provider
# ---------------------------------------------------------------------------


def test_repository_provider_reads_exact_bytes_and_hashes(tmp_path: Path) -> None:
    (tmp_path / "datasets").mkdir()
    content = b'{"query": "hi", "expected_behavior": "greet"}\n'
    (tmp_path / "datasets" / "development.jsonl").write_bytes(content)
    provider = RepositoryAssetProvider()
    request = _request()

    prepared = provider.prepare(request, _context(tmp_path))

    path = Path("datasets/development.jsonl")
    assert prepared.files[path] == content
    assert prepared.provenance.content_sha256 == __import__("hashlib").sha256(
        content
    ).hexdigest()
    assert prepared.provenance.source == "repository"
    assert prepared.provenance.metrics == ()


def test_repository_provider_canonicalizes_text_line_endings(
    tmp_path: Path,
) -> None:
    (tmp_path / "datasets").mkdir()
    windows_content = (
        b'{"query": "hi", "expected_behavior": "greet"}\r\n'
        b'{"query": "bye", "expected_behavior": "close"}\r\n'
    )
    canonical_content = windows_content.replace(b"\r\n", b"\n")
    (tmp_path / "datasets" / "development.jsonl").write_bytes(
        windows_content
    )

    prepared = RepositoryAssetProvider().prepare(
        _request(), _context(tmp_path)
    )

    path = Path("datasets/development.jsonl")
    assert prepared.files[path] == canonical_content
    assert prepared.provenance.content_sha256 == __import__("hashlib").sha256(
        canonical_content
    ).hexdigest()


def test_repository_provider_rejects_missing_file(tmp_path: Path) -> None:
    provider = RepositoryAssetProvider()
    request = _request()

    with pytest.raises(MissingAssetFileError):
        provider.prepare(request, _context(tmp_path))


def test_repository_provider_rejects_paths_that_resolve_outside_root(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    (tmp_path / "secret.txt").write_bytes(b"outside")
    provider = RepositoryAssetProvider()
    # Bypass request-level traversal validation to exercise the provider's
    # own defense-in-depth containment check.
    request = _hostile_request(path=Path("../secret.txt"))

    with pytest.raises(UnsafeAssetPathError):
        provider.prepare(request, _context(repository_root))


def test_repository_provider_rejects_symlinked_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "datasets").mkdir()
    target = tmp_path / "datasets" / "development.jsonl"
    target.write_bytes(b'{"query": "hi", "expected_behavior": "greet"}\n')

    original_is_symlink = Path.is_symlink

    def fake_is_symlink(self: Path) -> bool:
        if self.name == "development.jsonl":
            return True
        return original_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)
    provider = RepositoryAssetProvider()
    request = _request()

    with pytest.raises(UnsafeAssetPathError):
        provider.prepare(request, _context(tmp_path))


def test_repository_provider_requires_a_path(tmp_path: Path) -> None:
    provider = RepositoryAssetProvider()
    request = _hostile_request(path=None)

    with pytest.raises(ValueError, match="require a path"):
        provider.prepare(request, _context(tmp_path))


# ---------------------------------------------------------------------------
# Synthetic dataset provider
# ---------------------------------------------------------------------------


def _synthetic_request(rows: list[dict[str, object]], **overrides: object) -> EvaluationAssetRequest:
    values: dict[str, object] = {
        "asset_id": "validation",
        "kind": AssetKind.DATASET,
        "source": "synthetic",
        "role": "validation",
        "parameters": {"row_count": len(rows), "rows": rows},
    }
    values.update(overrides)
    return EvaluationAssetRequest(**values)


def test_synthetic_provider_generates_deterministic_canonical_jsonl(
    tmp_path: Path,
) -> None:
    rows = [
        {"query": "b question", "expected_behavior": "b behavior", "extra": 1},
        {"query": "a question", "expected_behavior": "a behavior"},
    ]
    provider = SyntheticDatasetProvider()
    request = _synthetic_request(rows)

    prepared = provider.prepare(request, _context(tmp_path))
    again = provider.prepare(request, _context(tmp_path))

    path = Path(".foundry/datasets/validation.jsonl")
    assert tuple(prepared.files) == (path,)
    content = prepared.files[path]
    assert content.endswith(b"\n")
    lines = content.decode("utf-8").splitlines()
    assert lines[0] == '{"expected_behavior":"b behavior","extra":1,"query":"b question"}'
    assert lines[1] == '{"expected_behavior":"a behavior","query":"a question"}'
    assert prepared.provenance.content_sha256 == again.provenance.content_sha256
    assert prepared.files == again.files
    assert prepared.provenance.metrics == ()


@pytest.mark.parametrize(
    "rows",
    [
        [{"expected_behavior": "greet"}],
        [{"query": "hi"}],
        [{"query": "  ", "expected_behavior": "greet"}],
        [{"query": "hi", "expected_behavior": "  "}],
        [{"query": 1, "expected_behavior": "greet"}],
        ["not-a-row"],
    ],
)
def test_synthetic_provider_rejects_malformed_rows(
    tmp_path: Path, rows: list[object]
) -> None:
    provider = SyntheticDatasetProvider()
    request = _synthetic_request(rows)  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        provider.prepare(request, _context(tmp_path))


def test_synthetic_provider_requires_rows_length_to_match_row_count(
    tmp_path: Path,
) -> None:
    provider = SyntheticDatasetProvider()
    request = EvaluationAssetRequest(
        asset_id="validation",
        kind=AssetKind.DATASET,
        source="synthetic",
        role="validation",
        parameters={
            "row_count": 3,
            "rows": [{"query": "hi", "expected_behavior": "greet"}],
        },
    )

    with pytest.raises(ValueError, match="row_count does not match"):
        provider.prepare(request, _context(tmp_path))


def test_synthetic_provider_enforces_caller_supplied_row_limit(
    tmp_path: Path,
) -> None:
    rows = [
        {"query": "hi", "expected_behavior": "greet"},
        {"query": "bye", "expected_behavior": "farewell"},
    ]
    provider = SyntheticDatasetProvider(max_rows=1)
    request = _synthetic_request(rows)

    with pytest.raises(ValueError, match="row limit"):
        provider.prepare(request, _context(tmp_path))


def test_synthetic_provider_max_rows_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_rows must be positive"):
        SyntheticDatasetProvider(max_rows=0)


# ---------------------------------------------------------------------------
# Trace provider
# ---------------------------------------------------------------------------


def test_trace_provider_requires_human_review_before_touching_data(
    tmp_path: Path,
) -> None:
    provider = TraceEvaluationAssetProvider()
    request = EvaluationAssetRequest(
        asset_id="production-failures",
        kind=AssetKind.DATASET,
        source="trace",
        role="validation",
        approval_gate=ApprovalGate.HUMAN,
        parameters={"lookback_hours": 24},
    )

    with pytest.raises(HumanReviewRequired) as excinfo:
        provider.prepare(request, _context(tmp_path))

    assert excinfo.value.asset_id == "production-failures"


# ---------------------------------------------------------------------------
# Custom evaluator provider
# ---------------------------------------------------------------------------


def test_custom_evaluator_provider_hashes_definition_and_requires_metrics(
    tmp_path: Path,
) -> None:
    (tmp_path / "evaluators").mkdir()
    content = b"def evaluate(row):\n    return {'quality': 1.0}\n"
    (tmp_path / "evaluators" / "quality.py").write_bytes(content)
    provider = CustomEvaluatorAssetProvider()
    request = EvaluationAssetRequest(
        asset_id="quality",
        kind=AssetKind.EVALUATOR,
        source="custom",
        name="quality-evaluator",
        version="v1",
        path=Path("evaluators/quality.py"),
        metrics=("quality",),
    )

    prepared = provider.prepare(request, _context(tmp_path))

    path = Path("evaluators/quality.py")
    assert prepared.files[path] == content
    assert prepared.provenance.content_sha256 == __import__("hashlib").sha256(
        content
    ).hexdigest()
    assert prepared.provenance.kind is AssetKind.EVALUATOR
    assert prepared.provenance.metrics == ("quality",)


def test_custom_evaluator_provider_requires_metrics(tmp_path: Path) -> None:
    provider = CustomEvaluatorAssetProvider()
    request = _hostile_request(
        kind=AssetKind.EVALUATOR,
        source="custom",
        role=None,
        name="quality-evaluator",
        version="v1",
        path=Path("evaluators/quality.py"),
        metrics=(),
    )

    with pytest.raises(ValueError, match="require metrics"):
        provider.prepare(request, _context(tmp_path))


def test_custom_evaluator_provider_rejects_dataset_kind(tmp_path: Path) -> None:
    provider = CustomEvaluatorAssetProvider()
    request = _hostile_request(
        kind=AssetKind.DATASET,
        source="custom",
        role="development",
        name="quality-dataset",
        version="v1",
        path=Path("datasets/development.jsonl"),
        metrics=(),
    )

    with pytest.raises(ValueError, match="must be evaluators"):
        provider.prepare(request, _context(tmp_path))


# ---------------------------------------------------------------------------
# Builtin evaluator provider
# ---------------------------------------------------------------------------


def _builtin_request(**overrides: object) -> EvaluationAssetRequest:
    values: dict[str, object] = {
        "asset_id": "quality",
        "kind": AssetKind.EVALUATOR,
        "source": "builtin",
        "name": "builtin-quality",
        "version": "v1",
        "metrics": ("quality",),
    }
    values.update(overrides)
    return EvaluationAssetRequest(**values)


def test_builtin_evaluator_provider_pins_deterministic_remote_id_with_no_files(
    tmp_path: Path,
) -> None:
    provider = BuiltinEvaluatorProvider()
    request = _builtin_request()

    prepared = provider.prepare(request, _context(tmp_path))

    assert prepared.provenance.remote_id == "builtin:builtin-quality:v1"
    assert prepared.provenance.content_sha256 is None
    assert prepared.provenance.approval_gate is ApprovalGate.POLICY
    assert prepared.provenance.source == "builtin"
    assert prepared.provenance.metrics == ("quality",)
    assert prepared.files == {}


def test_builtin_evaluator_provider_rejects_dataset_kind(tmp_path: Path) -> None:
    provider = BuiltinEvaluatorProvider()
    request = _hostile_request(
        kind=AssetKind.DATASET,
        source="builtin",
        role="development",
        name="builtin-dataset",
        version="v1",
        metrics=(),
    )

    with pytest.raises(ValueError, match="must be evaluators"):
        provider.prepare(request, _context(tmp_path))


def test_builtin_evaluator_provider_requires_metrics_defense_in_depth(
    tmp_path: Path,
) -> None:
    provider = BuiltinEvaluatorProvider()
    request = _hostile_request(
        kind=AssetKind.EVALUATOR,
        source="builtin",
        role=None,
        name="builtin-quality",
        version="v1",
        metrics=(),
    )

    with pytest.raises(ValueError, match="require metrics"):
        provider.prepare(request, _context(tmp_path))


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": None},
        {"version": None},
        {"name": ""},
        {"version": ""},
    ],
)
def test_builtin_evaluator_provider_requires_name_and_version_defense_in_depth(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    provider = BuiltinEvaluatorProvider()
    base = {
        "kind": AssetKind.EVALUATOR,
        "source": "builtin",
        "role": None,
        "name": "builtin-quality",
        "version": "v1",
        "metrics": ("quality",),
    }
    base.update(overrides)
    request = _hostile_request(**base)

    with pytest.raises(ValueError, match="exact name and version"):
        provider.prepare(request, _context(tmp_path))


def test_builtin_evaluator_provider_requires_policy_approval_defense_in_depth(
    tmp_path: Path,
) -> None:
    provider = BuiltinEvaluatorProvider()
    request = _hostile_request(
        kind=AssetKind.EVALUATOR,
        source="builtin",
        role=None,
        name="builtin-quality",
        version="v1",
        metrics=("quality",),
        approval_gate=ApprovalGate.HUMAN,
    )

    with pytest.raises(ValueError, match="policy approval"):
        provider.prepare(request, _context(tmp_path))


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_raises_for_unknown_source(tmp_path: Path) -> None:
    registry = EvaluationAssetProviderRegistry()

    with pytest.raises(UnknownEvaluationAssetProviderError):
        registry.get("nonexistent-source")


def test_registry_rejects_duplicate_provider_registration() -> None:
    registry = EvaluationAssetProviderRegistry()
    registry.register(RepositoryAssetProvider())

    with pytest.raises(DuplicateEvaluationAssetProviderError):
        registry.register(RepositoryAssetProvider())


def test_registry_includes_builtin_evaluator_provider(tmp_path: Path) -> None:
    registry = EvaluationAssetProviderRegistry()
    registry.register(BuiltinEvaluatorProvider())

    prepared = registry.prepare(_builtin_request(), _context(tmp_path))

    assert prepared.provenance.remote_id == "builtin:builtin-quality:v1"
    assert registry.sources == ("builtin",)


def test_registry_supports_future_source_registration(tmp_path: Path) -> None:
    class FutureProvider:
        source_type = "future-generator"

        def prepare(
            self,
            request: EvaluationAssetRequest,
            context: EvaluationAssetContext,
        ) -> PreparedEvaluationAsset:
            return PreparedEvaluationAsset(
                provenance=AssetProvenance(
                    asset_id=request.asset_id,
                    kind=request.kind,
                    source=request.source,
                    role=request.role,
                    name=request.name,
                    version=request.version,
                    created_by="future-provider",
                    approval_gate=request.approval_gate,
                ),
                files={Path("generated.jsonl"): b'{"query": "hi"}\n'},
            )

    registry = EvaluationAssetProviderRegistry()
    registry.register(FutureProvider())
    request = EvaluationAssetRequest(
        asset_id="future-data",
        kind=AssetKind.DATASET,
        source="future-generator",
        role="development",
        name="future-data",
        version="v1",
    )

    prepared = registry.prepare(request, _context(tmp_path))

    assert prepared.provenance.source == "future-generator"
    assert registry.sources == ("future-generator",)


def test_build_default_registry_wires_all_built_in_providers(tmp_path: Path) -> None:
    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets" / "development.jsonl").write_bytes(b'{}\n')
    registry = build_default_registry(_FakeFoundryGateway("remote-1"))

    assert set(registry.sources) == {
        "foundry",
        "repository",
        "synthetic",
        "trace",
        "custom",
        "builtin",
    }
    prepared = registry.prepare(_request(), _context(tmp_path))
    assert prepared.provenance.source == "repository"


# ---------------------------------------------------------------------------
# Materialization / registration gateway
# ---------------------------------------------------------------------------


class _FakeRegistrationGateway:
    def __init__(self, identity: AssetIdentity | None = None) -> None:
        self.identity = identity
        self.calls: list[tuple[AssetKind, str, str]] = []

    def register(
        self,
        *,
        kind: AssetKind,
        name: str,
        version: str,
        content: dict,
    ) -> AssetIdentity:
        self.calls.append((kind, name, version))
        if self.identity is not None:
            return self.identity
        return AssetIdentity(remote_id=f"remote-{name}-{version}", name=name, version=version)


def _prepared_repository_asset(tmp_path: Path) -> PreparedEvaluationAsset:
    (tmp_path / "datasets").mkdir()
    content = b'{"query": "hi", "expected_behavior": "greet"}\n'
    (tmp_path / "datasets" / "development.jsonl").write_bytes(content)
    provider = RepositoryAssetProvider()
    return provider.prepare(_request(), _context(tmp_path))


def test_materialize_registers_prepared_asset_with_deterministic_identity(
    tmp_path: Path,
) -> None:
    prepared = _prepared_repository_asset(tmp_path)
    gateway = _FakeRegistrationGateway()

    provenance = materialize_prepared_asset(prepared, gateway)

    expected_name = f"dataset-{prepared.provenance.asset_id}"
    expected_version = prepared.provenance.content_sha256[:16]
    assert gateway.calls == [(AssetKind.DATASET, expected_name, expected_version)]
    assert provenance.remote_id == f"remote-{expected_name}-{expected_version}"
    assert provenance.content_sha256 == prepared.provenance.content_sha256


def test_materialize_overwrites_provenance_version_with_actual_resolvable_version(
    tmp_path: Path,
) -> None:
    """Evaluator-style gateways return an ``AssetIdentity`` whose ``version``
    is the actual, remotely-resolvable service version (e.g. server
    auto-incremented), distinct from the deterministic ``requested_version``
    used to build the request. ``materialize_prepared_asset`` must verify
    the request against ``requested_version`` but propagate the *actual*
    version into the returned provenance, since that's what a later
    resolution lookup needs."""

    prepared = _prepared_repository_asset(tmp_path)
    expected_name = f"dataset-{prepared.provenance.asset_id}"
    expected_requested_version = prepared.provenance.content_sha256[:16]
    actual_identity = AssetIdentity(
        remote_id="remote-actual-99",
        name=expected_name,
        version="99",
        requested_version=expected_requested_version,
        content_sha256=prepared.provenance.content_sha256,
    )
    gateway = _FakeRegistrationGateway(identity=actual_identity)

    provenance = materialize_prepared_asset(prepared, gateway)

    assert provenance.remote_id == "remote-actual-99"
    assert provenance.version == "99"
    assert provenance.version != expected_requested_version


def test_materialize_preserves_evaluator_metrics_through_version_overwrite(
    tmp_path: Path,
) -> None:
    """The ``metrics`` tuple copied onto evaluator provenance from the
    request must survive materialization unchanged, even though
    ``remote_id``/``version`` are overwritten with the gateway's actual
    resolved identity."""

    (tmp_path / "evaluators").mkdir()
    content = b"def evaluate(row):\n    return {'quality': 1.0}\n"
    (tmp_path / "evaluators" / "quality.py").write_bytes(content)
    provider = CustomEvaluatorAssetProvider()
    request = EvaluationAssetRequest(
        asset_id="quality",
        kind=AssetKind.EVALUATOR,
        source="custom",
        name="quality-evaluator",
        version="v1",
        path=Path("evaluators/quality.py"),
        metrics=("quality", "safety"),
    )
    prepared = provider.prepare(request, _context(tmp_path))
    assert prepared.provenance.metrics == ("quality", "safety")

    expected_name = f"evaluator-{prepared.provenance.asset_id}"
    expected_requested_version = prepared.provenance.content_sha256[:16]
    actual_identity = AssetIdentity(
        remote_id="remote-evaluator-actual",
        name=expected_name,
        version="7",
        requested_version=expected_requested_version,
        content_sha256=prepared.provenance.content_sha256,
    )
    gateway = _FakeRegistrationGateway(identity=actual_identity)

    provenance = materialize_prepared_asset(prepared, gateway)

    assert provenance.remote_id == "remote-evaluator-actual"
    assert provenance.version == "7"
    assert provenance.metrics == ("quality", "safety")


def test_materialize_skips_already_remote_pinned_assets(tmp_path: Path) -> None:
    gateway = _FakeFoundryGateway(remote_id="foundry-remote-123")
    provider = ExistingFoundryAssetProvider(gateway=gateway)
    request = _request(
        source="foundry",
        name="support-development",
        version="v1",
        path=None,
    )
    prepared = provider.prepare(request, _context(tmp_path))

    registration_gateway = _FakeRegistrationGateway()
    provenance = materialize_prepared_asset(prepared, registration_gateway)

    assert provenance.remote_id == "foundry-remote-123"
    assert registration_gateway.calls == []


def test_materialize_is_a_noop_for_builtin_evaluator_assets(tmp_path: Path) -> None:
    provider = BuiltinEvaluatorProvider()
    prepared = provider.prepare(_builtin_request(), _context(tmp_path))

    registration_gateway = _FakeRegistrationGateway()
    provenance = materialize_prepared_asset(prepared, registration_gateway)

    assert provenance.remote_id == "builtin:builtin-quality:v1"
    assert provenance is prepared.provenance
    assert registration_gateway.calls == []


def test_materialize_raises_on_identity_mismatch(tmp_path: Path) -> None:
    prepared = _prepared_repository_asset(tmp_path)
    mismatched_identity = AssetIdentity(
        remote_id="remote-mismatch", name="wrong-name", version="wrong-version"
    )
    gateway = _FakeRegistrationGateway(identity=mismatched_identity)

    with pytest.raises(AssetIdentityMismatchError):
        materialize_prepared_asset(prepared, gateway)


def test_materialize_raises_when_requested_version_does_not_match(
    tmp_path: Path,
) -> None:
    """Even when ``identity.version`` looks plausible, a mismatched
    ``requested_version`` (the logical version actually asked for) must
    still be rejected -- an evaluator-style gateway cannot silently
    substitute a version we never requested."""

    prepared = _prepared_repository_asset(tmp_path)
    expected_name = f"dataset-{prepared.provenance.asset_id}"
    identity_with_wrong_request = AssetIdentity(
        remote_id="remote-actual-99",
        name=expected_name,
        version="99",
        requested_version="not-the-deterministic-request",
        content_sha256=prepared.provenance.content_sha256,
    )
    gateway = _FakeRegistrationGateway(identity=identity_with_wrong_request)

    with pytest.raises(AssetIdentityMismatchError):
        materialize_prepared_asset(prepared, gateway)


def test_materialize_raises_on_content_hash_mismatch(tmp_path: Path) -> None:
    prepared = _prepared_repository_asset(tmp_path)
    expected_name = f"dataset-{prepared.provenance.asset_id}"
    expected_version = prepared.provenance.content_sha256[:16]
    tampered_identity = AssetIdentity(
        remote_id="remote-1",
        name=expected_name,
        version=expected_version,
        content_sha256="0" * 64,
    )
    gateway = _FakeRegistrationGateway(identity=tampered_identity)

    with pytest.raises(AssetIdentityMismatchError):
        materialize_prepared_asset(prepared, gateway)


def test_materialize_blocks_trace_assets(tmp_path: Path) -> None:
    trace_provenance = AssetProvenance(
        asset_id="production-failures",
        kind=AssetKind.DATASET,
        source="trace",
        role="validation",
        created_by="trace-provider",
        approval_gate=ApprovalGate.HUMAN,
    )
    prepared = PreparedEvaluationAsset(provenance=trace_provenance, files={})
    gateway = _FakeRegistrationGateway()

    with pytest.raises(TraceAssetRegistrationBlockedError):
        materialize_prepared_asset(prepared, gateway)

    assert gateway.calls == []


def test_materialize_requires_content_hash_before_registration(tmp_path: Path) -> None:
    unhashed_provenance = AssetProvenance(
        asset_id="development",
        kind=AssetKind.DATASET,
        source="repository",
        role="development",
        created_by="repository-asset-provider",
        approval_gate=ApprovalGate.POLICY,
    )
    prepared = PreparedEvaluationAsset(provenance=unhashed_provenance, files={})
    gateway = _FakeRegistrationGateway()

    with pytest.raises(ValueError, match="require a content hash"):
        materialize_prepared_asset(prepared, gateway)
