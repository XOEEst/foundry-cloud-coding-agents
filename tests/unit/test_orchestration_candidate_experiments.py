import pytest

from foundry_opt.orchestration import (
    CandidateExperimentRequest,
    CandidateExperimentResult,
    CandidateExperimentRunner,
    DirectExperimentUnavailable,
)


class DirectRunner:
    def __init__(
        self,
        result: CandidateExperimentResult | None = None,
    ) -> None:
        self.result = result
        self.calls = 0

    def evaluate(
        self,
        request: CandidateExperimentRequest,
    ) -> CandidateExperimentResult:
        self.calls += 1
        if self.result is None:
            raise DirectExperimentUnavailable()
        return self.result


class ActionsRunner:
    def __init__(self, result: CandidateExperimentResult) -> None:
        self.result = result
        self.calls = 0

    def evaluate(
        self,
        request: CandidateExperimentRequest,
    ) -> CandidateExperimentResult:
        self.calls += 1
        return self.result


def _request() -> CandidateExperimentRequest:
    return CandidateExperimentRequest(
        issue_number=31,
        candidate_id="candidate-1",
        patch_sha256="1" * 64,
        idempotency_key="2" * 64,
    )


def _result(executor: str) -> CandidateExperimentResult:
    return CandidateExperimentResult(
        candidate_id="candidate-1",
        executor=executor,
        metrics={"advisory_safety": 1.0, "policy_coverage": 0.5},
        guardrails={"advisory_safety": "pass"},
        draft_id="draft-123",
        evaluation_id="eval-123",
        run_id="evalrun-123",
    )


def test_direct_candidate_evaluation_avoids_actions_fallback() -> None:
    direct = DirectRunner(_result("direct_oidc"))
    actions = ActionsRunner(_result("actions_oidc"))
    runner = CandidateExperimentRunner(direct=direct, fallback=actions)

    result = runner.evaluate(_request())

    assert result.executor == "direct_oidc"
    assert direct.calls == 1
    assert actions.calls == 0


def test_unavailable_direct_evaluation_uses_actions_with_same_request() -> None:
    direct = DirectRunner()
    actions = ActionsRunner(_result("actions_oidc"))
    runner = CandidateExperimentRunner(direct=direct, fallback=actions)

    result = runner.evaluate(_request())

    assert result.executor == "actions_oidc"
    assert direct.calls == 1
    assert actions.calls == 1


def test_real_direct_failure_does_not_fall_back_after_side_effects() -> None:
    class FailingDirect:
        def evaluate(
            self,
            request: CandidateExperimentRequest,
        ) -> CandidateExperimentResult:
            raise RuntimeError("Foundry draft creation failed")

    actions = ActionsRunner(_result("actions_oidc"))
    runner = CandidateExperimentRunner(
        direct=FailingDirect(),
        fallback=actions,
    )

    with pytest.raises(RuntimeError, match="draft creation failed"):
        runner.evaluate(_request())

    assert actions.calls == 0
