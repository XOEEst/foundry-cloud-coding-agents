from __future__ import annotations

import pytest

from foundry_opt.openai_graders import OpenAIStringCheckGrader


def test_string_check_remote_identity_round_trips() -> None:
    grader = OpenAIStringCheckGrader(
        input="{{sample.output_text}}",
        operation="ilike",
        reference="Policy categories:",
    )

    assert OpenAIStringCheckGrader.from_remote_id(grader.remote_id) == grader


def test_string_check_rejects_oversized_remote_identity() -> None:
    with pytest.raises(ValueError, match="remote identity"):
        OpenAIStringCheckGrader(
            input="x" * 900,
            operation="ilike",
            reference="y" * 900,
        )


def test_string_check_rejects_noncanonical_remote_identity() -> None:
    grader = OpenAIStringCheckGrader(
        input="{{sample.output_text}}",
        operation="ilike",
        reference="Policy categories:",
    )

    with pytest.raises(ValueError, match="remote identity"):
        OpenAIStringCheckGrader.from_remote_id(f"{grader.remote_id}!!!")
