"""Targeted tests for goal/spec identity and asset provenance propagation.

Covers the `campaign-goal-context` requirements: CampaignRequest.goal /
spec_sha256 validation (including secret rejection), propagation into
CandidateContext for every generator slot and retry, CampaignState resume
mismatch failures, exact asset identity propagation into CampaignReport, and
evidence output carrying only goal hash / spec_sha256 / asset identity
(never raw goal text, raw asset rows, or evaluator prompts).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from foundry_opt.campaign.engine import CampaignDependencies, run_campaign
from foundry_opt.campaign.protocols import (
    CampaignRequest,
    CampaignStateError,
    CandidateContext,
    CandidateIdea,
    TransientCandidateError,
)
from foundry_opt.campaign.state import CampaignState, MemoryCampaignStateStore
from foundry_opt.evidence import (
    EvaluationAssetReference,
    EvidenceRequest,
    write_redacted_evidence,
)

from test_campaign_orchestration import (
    ASSETS,
    BASE_COMMIT,
    GOAL,
    GOAL_SHA256,
    SPEC_SHA256,
    FakeClock,
    FakeGenerator,
    FakeRepository,
    _dependencies,
    _request,
)
from test_evaluation_selection import POLICY, _result as _evaluation_result


# ---------------------------------------------------------------------------
# CampaignRequest / CandidateContext validation
# ---------------------------------------------------------------------------


def test_campaign_request_rejects_short_goal(tmp_path: Path) -> None:
    request = _request(tmp_path)
    with pytest.raises(ValueError):
        replace(request, goal="too short")


def test_campaign_request_rejects_long_goal(tmp_path: Path) -> None:
    request = _request(tmp_path)
    with pytest.raises(ValueError):
        replace(request, goal="x" * 4001)


def test_campaign_request_rejects_goal_containing_secret(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    leaking_goal = (
        "Improve the agent using this token ghp_1234567890abcdef1234567890"
        "abcdef and keep guardrails intact."
    )
    with pytest.raises(ValueError):
        replace(request, goal=leaking_goal)


def test_campaign_request_rejects_invalid_spec_sha256(tmp_path: Path) -> None:
    request = _request(tmp_path)
    with pytest.raises(ValueError):
        replace(request, spec_sha256="not-a-sha256")


def test_campaign_request_rejects_empty_assets(tmp_path: Path) -> None:
    request = _request(tmp_path)
    with pytest.raises(ValueError):
        replace(request, assets=())


def test_campaign_request_rejects_duplicate_asset_ids(tmp_path: Path) -> None:
    request = _request(tmp_path)
    duplicated = (
        ASSETS[0],
        replace(ASSETS[1], asset_id=ASSETS[0].asset_id),
    )
    with pytest.raises(ValueError):
        replace(request, assets=duplicated)


def test_candidate_context_rejects_short_goal() -> None:
    with pytest.raises(ValueError):
        CandidateContext(
            campaign_id="campaign-1",
            target="agent",
            candidate_id="candidate-1",
            slot=1,
            worktree=Path("/tmp/worktree"),
            base_commit=BASE_COMMIT,
            edit_paths=(Path("agent"),),
            allowed_mutations=frozenset({"system_instructions"}),
            restricted_opt_ins={},
            baseline_metrics={},
            history=(),
            goal="too short",
            spec_sha256=SPEC_SHA256,
        )


def test_candidate_context_rejects_secret_goal() -> None:
    leaking_goal = (
        "Improve the agent using this token ghp_1234567890abcdef1234567890"
        "abcdef and keep guardrails intact."
    )
    with pytest.raises(ValueError):
        CandidateContext(
            campaign_id="campaign-1",
            target="agent",
            candidate_id="candidate-1",
            slot=1,
            worktree=Path("/tmp/worktree"),
            base_commit=BASE_COMMIT,
            edit_paths=(Path("agent"),),
            allowed_mutations=frozenset({"system_instructions"}),
            restricted_opt_ins={},
            baseline_metrics={},
            history=(),
            goal=leaking_goal,
            spec_sha256=SPEC_SHA256,
        )


# ---------------------------------------------------------------------------
# Propagation into CandidateContext for every slot and retry
# ---------------------------------------------------------------------------


class RecordingGenerator:
    """Generator that records goal/spec identity on every invocation and
    forces one transient retry so both the initial attempt and the retry
    attempt are exercised for the same slot."""

    def __init__(self, retry_once_for: set[str]) -> None:
        self.calls: list[tuple[str, int, str, str]] = []
        self._retry_once_for = retry_once_for
        self._retried: set[str] = set()

    def generate(self, context: CandidateContext) -> CandidateIdea:
        self.calls.append(
            (context.candidate_id, context.slot, context.goal, context.spec_sha256)
        )
        if (
            context.candidate_id in self._retry_once_for
            and context.candidate_id not in self._retried
        ):
            self._retried.add(context.candidate_id)
            raise TransientCandidateError("transient generation failure")
        return CandidateIdea(
            idea_id=f"idea-{context.slot}",
            mutation_class="system_instructions",
            parent_idea_ids=(),
        )


def test_generator_receives_goal_and_spec_for_every_slot_and_retry(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, candidates=2)
    generator = RecordingGenerator(retry_once_for={"candidate-1"})
    dependencies = _dependencies(
        tmp_path,
        repository=FakeRepository(tmp_path),
        generator=generator,
        clock=FakeClock(),
    )

    run_campaign(request, dependencies)

    # candidate-1 is invoked twice (initial attempt + transient retry),
    # candidate-2 is invoked once.
    assert [call[0] for call in generator.calls] == [
        "candidate-1",
        "candidate-1",
        "candidate-2",
    ]
    assert all(call[2] == GOAL for call in generator.calls)
    assert all(call[3] == SPEC_SHA256 for call in generator.calls)


# ---------------------------------------------------------------------------
# CampaignReport carries exact goal/spec identity and asset provenance
# ---------------------------------------------------------------------------


def test_campaign_report_carries_exact_goal_and_asset_identity(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, candidates=1)
    generator = FakeGenerator()
    dependencies = _dependencies(
        tmp_path,
        repository=FakeRepository(tmp_path),
        generator=generator,
        clock=FakeClock(),
        validations=[True, True],
    )

    report = run_campaign(request, dependencies)

    assert report.goal_sha256 == GOAL_SHA256
    assert report.spec_sha256 == SPEC_SHA256
    assert report.assets == ASSETS


# ---------------------------------------------------------------------------
# CampaignState resume mismatch
# ---------------------------------------------------------------------------


def _seed_state(store: MemoryCampaignStateStore, root: Path, request: CampaignRequest, clock: FakeClock) -> None:
    store.save(
        root.resolve(),
        CampaignState(
            campaign_id=request.campaign_id,
            target=request.target,
            base_commit=BASE_COMMIT,
            status="completed",
            started_at=clock.now(),
            updated_at=clock.now(),
            goal_sha256=GOAL_SHA256,
            spec_sha256=SPEC_SHA256,
            assets=ASSETS,
        ),
    )


def test_resume_with_different_goal_raises_campaign_state_mismatch(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    store = MemoryCampaignStateStore()
    clock = FakeClock()
    _seed_state(store, tmp_path, request, clock)

    different_goal_request = replace(
        request,
        goal=(
            "Reduce latency for the acceptance agent while preserving "
            "correctness across every candidate change."
        ),
    )
    dependencies = replace(
        _dependencies(
            tmp_path,
            repository=FakeRepository(tmp_path),
            generator=FakeGenerator(),
            clock=clock,
        ),
        state=store,
    )

    with pytest.raises(CampaignStateError) as excinfo:
        run_campaign(different_goal_request, dependencies)

    assert excinfo.value.state.error_code == "campaign_state_mismatch"
    assert excinfo.value.state.status == "failed"
    persisted = store.load(tmp_path.resolve(), request.campaign_id)
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.error_code == "campaign_state_mismatch"


def test_resume_with_different_spec_sha256_raises_campaign_state_mismatch(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    store = MemoryCampaignStateStore()
    clock = FakeClock()
    _seed_state(store, tmp_path, request, clock)

    different_spec_request = replace(request, spec_sha256="7" * 64)
    dependencies = replace(
        _dependencies(
            tmp_path,
            repository=FakeRepository(tmp_path),
            generator=FakeGenerator(),
            clock=clock,
        ),
        state=store,
    )

    with pytest.raises(CampaignStateError) as excinfo:
        run_campaign(different_spec_request, dependencies)

    assert excinfo.value.state.error_code == "campaign_state_mismatch"


def test_resume_with_matching_goal_and_spec_does_not_mismatch(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    store = MemoryCampaignStateStore()
    clock = FakeClock()
    _seed_state(store, tmp_path, request, clock)
    dependencies = replace(
        _dependencies(
            tmp_path,
            repository=FakeRepository(tmp_path),
            generator=FakeGenerator(),
            clock=clock,
        ),
        state=store,
    )

    # A matching goal/spec does not raise the state-mismatch error; the
    # completed-state short-circuit path is reached instead (which fails on
    # this minimal fixture for an unrelated reason: no baseline draft was
    # recorded on the seeded state).
    with pytest.raises(RuntimeError, match="baseline draft"):
        run_campaign(request, dependencies)


# ---------------------------------------------------------------------------
# Evidence output: goal hash / spec_sha256 / asset identity only, no raw data
# ---------------------------------------------------------------------------


def _evidence_assets() -> tuple[EvaluationAssetReference, ...]:
    return (
        EvaluationAssetReference(
            asset_id="dataset-dev",
            kind="dataset",
            source="repository",
            role="development",
        ),
        EvaluationAssetReference(
            asset_id="evaluator-quality",
            kind="evaluator",
            source="builtin",
            name="quality",
            version="1",
            content_sha256="a" * 64,
            metrics=("quality",),
        ),
    )


def _evidence_request(tmp_path: Path, *, goal: str) -> EvidenceRequest:
    baseline = _evaluation_result("baseline", quality=0.7, latency=1.5)
    candidate = _evaluation_result("candidate", quality=0.8, latency=1.4)
    from foundry_opt.evaluation import select_eligible_candidates

    pareto = select_eligible_candidates(baseline, (candidate,), POLICY)
    return EvidenceRequest(
        output_path=tmp_path / "evidence.json",
        campaign_id="campaign-1",
        baseline=baseline,
        candidates=(candidate,),
        pareto=pareto,
        metric_policies=POLICY,
        source_hash="sha256:source",
        goal=goal,
        spec_sha256=SPEC_SHA256,
        assets=_evidence_assets(),
    )


def test_evidence_manifest_and_document_carry_goal_hash_and_spec_not_raw_goal(
    tmp_path: Path,
) -> None:
    import hashlib
    import json

    request = _evidence_request(tmp_path, goal=GOAL)
    manifest = write_redacted_evidence(request)

    assert manifest.goal_sha256 == hashlib.sha256(GOAL.encode("utf-8")).hexdigest()
    assert manifest.spec_sha256 == SPEC_SHA256

    content = request.output_path.read_text(encoding="utf-8")
    document = json.loads(content)
    assert document["goal_sha256"] == manifest.goal_sha256
    assert document["spec_sha256"] == SPEC_SHA256
    # The raw goal text must never appear in the persisted evidence document.
    assert GOAL not in content


def test_evidence_asset_entries_contain_only_safe_identity_fields(
    tmp_path: Path,
) -> None:
    import json

    request = _evidence_request(tmp_path, goal=GOAL)
    write_redacted_evidence(request)

    document = json.loads(request.output_path.read_text(encoding="utf-8"))
    assets = document["assets"]
    assert len(assets) == 2
    allowed_keys = {
        "asset_id",
        "kind",
        "source",
        "role",
        "name",
        "version",
        "remote_id",
        "content_sha256",
        "approval_gate",
        "metrics",
    }
    for asset in assets:
        assert set(asset.keys()) == allowed_keys
    assert {asset["asset_id"] for asset in assets} == {
        "dataset-dev",
        "evaluator-quality",
    }
    # No raw dataset rows or evaluator prompt/content fields are present.
    forbidden_keys = {"rows", "content", "prompt", "prompts", "data"}
    for asset in assets:
        assert forbidden_keys.isdisjoint(asset.keys())


def test_evidence_rejects_goal_with_secret_marker(tmp_path: Path) -> None:
    leaking_goal = (
        "Improve the agent using this token ghp_1234567890abcdef1234567890"
        "abcdef and keep guardrails intact."
    )
    request = _evidence_request(tmp_path, goal=leaking_goal)
    with pytest.raises(ValueError):
        write_redacted_evidence(request)


def test_evidence_rejects_short_goal(tmp_path: Path) -> None:
    request = _evidence_request(tmp_path, goal="too short")
    with pytest.raises(ValueError):
        write_redacted_evidence(request)


def test_evidence_rejects_duplicate_asset_ids(tmp_path: Path) -> None:
    request = _evidence_request(tmp_path, goal=GOAL)
    duplicated = replace(
        request,
        assets=(
            request.assets[0],
            replace(request.assets[1], asset_id=request.assets[0].asset_id),
        ),
    )
    with pytest.raises(ValueError):
        write_redacted_evidence(duplicated)


def test_evidence_rejects_invalid_spec_sha256(tmp_path: Path) -> None:
    request = _evidence_request(tmp_path, goal=GOAL)
    invalid = replace(request, spec_sha256="not-a-sha256")
    with pytest.raises(ValueError):
        write_redacted_evidence(invalid)


# ---------------------------------------------------------------------------
# EvaluationAssetReference.remote_id: opaque Foundry identity support
# ---------------------------------------------------------------------------


def test_remote_id_accepts_colon_separated_provider_identity() -> None:
    reference = EvaluationAssetReference(
        asset_id="evaluator-quality",
        kind="evaluator",
        source="builtin",
        name="quality",
        version="1",
        remote_id="builtin:builtin-quality:v1",
        metrics=("quality",),
    )
    assert reference.remote_id == "builtin:builtin-quality:v1"


def test_remote_id_accepts_resource_id_style_reference() -> None:
    reference = EvaluationAssetReference(
        asset_id="dataset-dev",
        kind="dataset",
        source="foundry",
        role="development",
        remote_id=(
            "/subscriptions/00000000-0000-0000-0000-000000000000/"
            "resourceGroups/rg/providers/Microsoft.MachineLearningServices/"
            "workspaces/ws/data/dataset/versions/3"
        ),
    )
    assert reference.remote_id.startswith("/subscriptions/")


def test_remote_id_accepts_https_uri_without_credentials() -> None:
    reference = EvaluationAssetReference(
        asset_id="dataset-dev",
        kind="dataset",
        source="foundry",
        role="development",
        remote_id="https://account.blob.core.windows.net/container/blob.jsonl",
    )
    assert reference.remote_id == (
        "https://account.blob.core.windows.net/container/blob.jsonl"
    )


def test_remote_id_rejects_empty_string() -> None:
    with pytest.raises(ValueError):
        EvaluationAssetReference(
            asset_id="dataset-dev",
            kind="dataset",
            source="foundry",
            role="development",
            remote_id="",
        )


def test_remote_id_rejects_oversized_value() -> None:
    with pytest.raises(ValueError):
        EvaluationAssetReference(
            asset_id="dataset-dev",
            kind="dataset",
            source="foundry",
            role="development",
            remote_id="x" * 2049,
        )


def test_remote_id_rejects_control_characters() -> None:
    with pytest.raises(ValueError):
        EvaluationAssetReference(
            asset_id="dataset-dev",
            kind="dataset",
            source="foundry",
            role="development",
            remote_id="builtin:name\x00:v1",
        )


def test_remote_id_rejects_secret_shaped_value() -> None:
    with pytest.raises(ValueError):
        EvaluationAssetReference(
            asset_id="dataset-dev",
            kind="dataset",
            source="foundry",
            role="development",
            remote_id=(
                "https://account.blob.core.windows.net/container/blob.jsonl"
                ";AccountKey=abcd1234=="
            ),
        )


def test_remote_id_rejects_credential_query_parameter() -> None:
    with pytest.raises(ValueError):
        EvaluationAssetReference(
            asset_id="dataset-dev",
            kind="dataset",
            source="foundry",
            role="development",
            remote_id=(
                "https://account.blob.core.windows.net/container/blob.jsonl"
                "?sv=2020-08-04&sig=deadbeef&se=2030-01-01"
            ),
        )


def test_remote_id_rejects_token_query_parameter() -> None:
    with pytest.raises(ValueError):
        EvaluationAssetReference(
            asset_id="evaluator-quality",
            kind="evaluator",
            source="foundry",
            remote_id="https://api.example.com/evaluators/quality?token=abc123",
            metrics=("quality",),
        )


# ---------------------------------------------------------------------------
# Explicit isinstance(EvaluationAssetReference) validation on every asset
# tuple owner (fails safely instead of raising AttributeError on wrong types)
# ---------------------------------------------------------------------------


def test_campaign_request_rejects_non_asset_reference_elements(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    with pytest.raises(ValueError):
        replace(request, assets=({"asset_id": "not-a-reference"},))


def test_campaign_state_rejects_non_asset_reference_elements() -> None:
    with pytest.raises(ValueError):
        CampaignState(
            campaign_id="campaign-1",
            target="agent",
            base_commit=BASE_COMMIT,
            status="active",
            started_at=FakeClock().now(),
            updated_at=FakeClock().now(),
            goal_sha256=GOAL_SHA256,
            spec_sha256=SPEC_SHA256,
            assets=("not-a-reference",),
        )


def test_campaign_report_rejects_non_asset_reference_elements() -> None:
    from foundry_opt.campaign.models import CampaignReport

    with pytest.raises(ValueError):
        CampaignReport(
            campaign_id="campaign-1",
            target="agent",
            base_commit=BASE_COMMIT,
            baseline_draft_id="draft-baseline",
            candidates=(),
            pareto_candidate_ids=(),
            goal_sha256=GOAL_SHA256,
            spec_sha256=SPEC_SHA256,
            assets=(object(),),
        )


def test_evidence_request_rejects_non_asset_reference_elements(
    tmp_path: Path,
) -> None:
    request = _evidence_request(tmp_path, goal=GOAL)
    invalid = replace(request, assets=(42,))
    with pytest.raises(ValueError):
        write_redacted_evidence(invalid)
