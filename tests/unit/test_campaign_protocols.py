from pathlib import Path

import pytest

from foundry_opt.campaign.protocols import CandidateIdea, PinnedRepository
from foundry_opt.campaign.lineage import IdeaLineage


def test_campaign_lineage_accepts_only_safe_identifiers_and_paths() -> None:
    idea = CandidateIdea(
        "idea-1",
        "system_instructions",
        required_opt_ins=frozenset({"tool_contract_schema_changes"}),
    )
    lineage = IdeaLineage(
        idea.idea_id,
        (),
        idea.mutation_class,
        (Path("agent/instructions.md"),),
    )

    assert lineage.changed_paths == (Path("agent/instructions.md"),)
    assert PinnedRepository("release/main", "a" * 40).default_branch == (
        "release/main"
    )

    with pytest.raises(ValueError):
        CandidateIdea("raw customer\ncontent", "system_instructions")
    with pytest.raises(ValueError):
        IdeaLineage(
            "idea-1",
            (),
            "system_instructions",
            (Path("../outside"),),
        )
