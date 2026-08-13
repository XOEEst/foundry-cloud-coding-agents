from foundry_opt.evidence import EvaluationAssetReference
from foundry_opt.orchestration.workspace_execution_production import (
    _asset_reference_is_complete,
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
