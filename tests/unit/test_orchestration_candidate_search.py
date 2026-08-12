import pytest

from foundry_opt.orchestration import (
    BoundedCandidateSearch,
    CandidateExperimentRequest,
    CandidateExperimentResult,
    CandidateSearchSummary,
)


def _request(index: int) -> CandidateExperimentRequest:
    return CandidateExperimentRequest(
        issue_number=31,
        candidate_id=f"candidate-{index}",
        patch_sha256=f"{index:x}" * 64,
        bundle_sha256=f"{index + 3:x}" * 64,
        evidence_sha256=f"{index + 5:x}" * 64,
        idempotency_key=f"{index + 8:x}" * 64,
    )


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def evaluate(
        self,
        request: CandidateExperimentRequest,
    ) -> CandidateExperimentResult:
        self.calls.append(request.candidate_id)
        return CandidateExperimentResult(
            candidate_id=request.candidate_id,
            executor="direct_oidc",
            metrics={"quality": float(len(self.calls))},
            guardrails={"safety": "pass"},
            draft_id=f"draft-{request.candidate_id}",
            evaluation_id=f"eval-{request.candidate_id}",
            run_id=f"run-{request.candidate_id}",
            bundle_sha256=request.bundle_sha256,
            evidence_sha256=request.evidence_sha256,
        )


def test_bounded_candidate_search_is_sequential_and_returns_safe_summaries() -> None:
    runner = RecordingRunner()
    search = BoundedCandidateSearch(runner=runner, max_candidates=3)

    summaries = search.evaluate((_request(1), _request(2), _request(3)))

    assert runner.calls == ["candidate-1", "candidate-2", "candidate-3"]
    assert [summary.candidate_id for summary in summaries] == runner.calls
    assert isinstance(summaries[0], CandidateSearchSummary)
    assert summaries[0].patch_sha256 == "1" * 64
    assert summaries[0].bundle_sha256 == "4" * 64
    assert summaries[0].evidence_sha256 == "6" * 64
    assert summaries[0].idempotency_key == "9" * 64
    assert summaries[0].metrics == {"quality": 1.0}
    assert summaries[0].guardrails == {"safety": "pass"}
    assert not hasattr(summaries[0], "draft_id")
    assert not hasattr(summaries[0], "evaluation_id")
    assert not hasattr(summaries[0], "run_id")


def test_bounded_candidate_search_rejects_oversized_slate_before_evaluation() -> None:
    runner = RecordingRunner()
    search = BoundedCandidateSearch(runner=runner, max_candidates=2)

    with pytest.raises(ValueError, match="candidate search exceeds"):
        search.evaluate((_request(1), _request(2), _request(3)))

    assert runner.calls == []


def test_bounded_candidate_search_rejects_empty_configuration() -> None:
    runner = RecordingRunner()
    search = BoundedCandidateSearch(runner=runner, max_candidates=2)

    with pytest.raises(ValueError, match="at least one configured candidate"):
        search.evaluate(())

    assert runner.calls == []
