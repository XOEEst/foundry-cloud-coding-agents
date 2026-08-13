from types import SimpleNamespace

import foundry_opt.orchestration.workspace_execution_production as production
from foundry_opt.evidence import EvaluationAssetReference
from foundry_opt.orchestration.workspace_execution_production import (
    _asset_reference_is_complete,
    _workspace_spec_base,
)


def test_repository_asset_content_hash_is_complete_before_binding() -> None:
    asset = EvaluationAssetReference(
        asset_id="development",
        kind="dataset",
        source="repository",
        role="development",
        content_sha256="a" * 64,
    )

    assert _asset_reference_is_complete(asset) is True


def test_asset_without_remote_identity_or_content_hash_is_incomplete() -> None:
    asset = EvaluationAssetReference(
        asset_id="development",
        kind="dataset",
        source="repository",
        role="development",
    )

    assert _asset_reference_is_complete(asset) is False


def test_workspace_execution_uses_persisted_specification_base(
    monkeypatch,
) -> None:
    snapshot = SimpleNamespace(
        specification=SimpleNamespace(base_commit="b" * 40),
    )
    monkeypatch.setattr(
        production,
        "GitWorkspaceStore",
        lambda root: SimpleNamespace(load=lambda issue: snapshot),
    )

    assert _workspace_spec_base(SimpleNamespace(), 31) == "b" * 40
