"""Tests for the vendored Tenzing snapshot bundled with the ``foundry-agent-optimizer`` skill.

Verifies that the snapshot under
``src/foundry_opt/templates/skills/foundry-agent-optimizer/references/tenzing/`` is an unmodified,
byte-exact copy of the upstream Tenzing repository at the exact reviewed revision, that
``UPSTREAM.md`` records the correct provenance, that no vendored file has had its placeholder
markers mutated, and that the Foundry adapter mapping document and top-level ``SKILL.md`` template
describe the expected concepts.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = (
    REPO_ROOT
    / "src"
    / "foundry_opt"
    / "templates"
    / "skills"
    / "foundry-agent-optimizer"
)
SNAPSHOT_ROOT = SKILL_ROOT / "references" / "tenzing"

UPSTREAM_URL = "https://github.com/coreai-microsoft/tenzing"
UPSTREAM_REVISION = "7300a83fc7378f0f1a401dbdf8ed28358ccf1732"
UPSTREAM_COPYRIGHT_HOLDER = "saketsathe"
UPSTREAM_LOCAL_CLONE = Path("Q:/GIT/tenzing")
EXPECTED_SNAPSHOT_SHA256 = (
    "d33fe9d23075a3dd82e8024204f3e8a6ffdd32126dc68f962ad30c21e2a4f00a"
)

# Every file preserved byte-for-byte from the upstream snapshot at UPSTREAM_REVISION.
VENDORED_FILES = (
    "LICENSE",
    "README.md",
    "climb.md",
    "INIT.md",
    ".gitignore",
    "assets/logo.svg",
    "climb_config/background.md",
    "climb_config/objective.md",
    "climb_config/dos-and-donts.md",
    "climb_config/evaluation.md",
    "climb_config/environment.md",
    "climb_config/data.md",
    "climb_config/tracking-experiments.md",
)

# Files under the vendored files that are known to still contain {{PLACEHOLDER}} markers
# upstream, and must keep them verbatim (they are filled in by INIT.md inside a *generated*
# customer repository, never by this skill ahead of time).
FILES_WITH_PLACEHOLDERS = (
    "climb.md",
    "INIT.md",
    "climb_config/background.md",
    "climb_config/objective.md",
    "climb_config/dos-and-donts.md",
    "climb_config/evaluation.md",
    "climb_config/environment.md",
    "climb_config/data.md",
    "climb_config/tracking-experiments.md",
)

PLACEHOLDER_PATTERN = re.compile(r"\{\{[A-Z0-9_]+\}\}")


def _snapshot_sha256() -> str:
    import hashlib

    digest = hashlib.sha256()
    for relative_path in sorted(VENDORED_FILES):
        relative = Path(relative_path).as_posix().encode("utf-8")
        content = (SNAPSHOT_ROOT / relative_path).read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def test_vendored_snapshot_matches_reviewed_aggregate_digest() -> None:
    assert _snapshot_sha256() == EXPECTED_SNAPSHOT_SHA256


def _normalized(text: str) -> str:
    """Collapse whitespace runs (including hand-wrapped markdown newlines) to single spaces.

    The prose documents checked below wrap paragraphs by hand at a fixed column width, so a
    phrase this test looks for can legitimately be split across a line break in the source file
    even though it reads as one continuous phrase. Substring assertions against prose should
    normalize whitespace first so they don't spuriously fail (or pass) depending on incidental
    line-wrap position.
    """
    return re.sub(r"\s+", " ", text)


def _require_upstream_clone() -> Path:
    if not UPSTREAM_LOCAL_CLONE.exists():
        pytest.skip(f"upstream clone unavailable at {UPSTREAM_LOCAL_CLONE}")
    return UPSTREAM_LOCAL_CLONE


def test_upstream_clone_is_pinned_to_the_reviewed_revision() -> None:
    clone = _require_upstream_clone()
    try:
        result = subprocess.run(
            ["git", "-C", str(clone), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"git is unavailable to verify the upstream clone: {exc}")
    assert result.stdout.strip() == UPSTREAM_REVISION


@pytest.mark.parametrize("relative_path", VENDORED_FILES)
def test_vendored_file_is_byte_identical_to_upstream(relative_path: str) -> None:
    clone = _require_upstream_clone()
    upstream_file = clone / relative_path
    vendored_file = SNAPSHOT_ROOT / relative_path
    assert upstream_file.is_file(), f"fixture missing upstream file {relative_path}"
    assert vendored_file.is_file(), f"vendored snapshot missing {relative_path}"
    assert vendored_file.read_bytes() == upstream_file.read_bytes()


def test_vendored_snapshot_contains_exactly_the_expected_files() -> None:
    expected = {Path(path).as_posix() for path in VENDORED_FILES}
    actual = {
        path.relative_to(SNAPSHOT_ROOT).as_posix()
        for path in SNAPSHOT_ROOT.rglob("*")
        if path.is_file() and path.name != "UPSTREAM.md"
    }
    assert actual == expected


@pytest.mark.parametrize("relative_path", FILES_WITH_PLACEHOLDERS)
def test_vendored_placeholders_are_not_mutated(relative_path: str) -> None:
    """Vendored files must keep their upstream ``{{PLACEHOLDER}}`` markers verbatim.

    Byte equality (above) already guarantees this, but a change here that still passed byte
    equality against a stale fixture would be a silent regression, so this test independently
    re-derives the expected placeholders from the upstream clone rather than hard-coding them.
    """
    clone = _require_upstream_clone()
    upstream_text = (clone / relative_path).read_text(encoding="utf-8")
    vendored_text = (SNAPSHOT_ROOT / relative_path).read_text(encoding="utf-8")
    upstream_placeholders = PLACEHOLDER_PATTERN.findall(upstream_text)
    assert upstream_placeholders, (
        f"test fixture is stale: upstream {relative_path} no longer has placeholders"
    )
    assert PLACEHOLDER_PATTERN.findall(vendored_text) == upstream_placeholders


def test_upstream_md_records_exact_revision_and_url() -> None:
    text = (SNAPSHOT_ROOT / "UPSTREAM.md").read_text(encoding="utf-8")
    assert UPSTREAM_URL in text
    assert UPSTREAM_REVISION in text


def test_upstream_md_records_author_copyright_and_license() -> None:
    text = (SNAPSHOT_ROOT / "UPSTREAM.md").read_text(encoding="utf-8")
    assert UPSTREAM_COPYRIGHT_HOLDER in text
    assert "MIT" in text
    assert "LICENSE" in text


def test_upstream_md_describes_a_deliberate_offline_upgrade_model() -> None:
    text = _normalized((SNAPSHOT_ROOT / "UPSTREAM.md").read_text(encoding="utf-8")).lower()
    assert "offline upgrade model" in text
    assert "no submodule" in text or "no live" in text or "not track upstream live" in text
    assert "human-reviewed" in text or "manually" in text


def test_license_file_is_the_full_upstream_mit_license_text() -> None:
    text = (SNAPSHOT_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in text
    assert "Copyright (c) 2026 saketsathe" in text
    assert "Permission is hereby granted, free of charge" in text
    assert "THE SOFTWARE IS PROVIDED \"AS IS\"" in text


def test_adapter_mapping_document_lives_outside_the_vendored_snapshot() -> None:
    adapter_doc = SKILL_ROOT / "ADAPTER_MAPPING.md"
    assert adapter_doc.is_file()
    assert SNAPSHOT_ROOT not in adapter_doc.parents


@pytest.mark.parametrize(
    ("tenzing_concept", "foundry_concept"),
    (
        ("objective", "issue-approved optimization spec"),
        ("data", "pinned dataset"),
        ("branches", "temporary worktrees isolate code experiments"),
        ("evaluation", "Foundry source-ZIP draft"),
        ("results", "Pareto"),
        ("results", "redacted"),
        ("termination", "bounded Copilot session"),
    ),
)
def test_adapter_mapping_document_maps_expected_concepts(
    tenzing_concept: str,
    foundry_concept: str,
) -> None:
    text = _normalized(
        (SKILL_ROOT / "ADAPTER_MAPPING.md").read_text(encoding="utf-8")
    ).lower()
    assert tenzing_concept in text
    assert foundry_concept.lower() in text


def test_adapter_mapping_document_cites_real_foundry_symbols() -> None:
    text = (SKILL_ROOT / "ADAPTER_MAPPING.md").read_text(encoding="utf-8")
    for symbol in (
        "OptimizationSpec",
        "AssetProvenance",
        "contained_worktree_root",
        "CampaignWorktree",
        "DraftRequest",
        "EvaluationGateway",
        "select_eligible_candidates",
        "ParetoResult",
        "EvidenceManifest",
        "deadline_minutes",
        "candidate_cutoff_minutes",
        "apply_exact",
        "ExactPatchRequest",
        "substantive_repair",
    ):
        assert symbol in text


def test_adapter_mapping_document_names_the_three_agents() -> None:
    text = _normalized(
        (SKILL_ROOT / "ADAPTER_MAPPING.md").read_text(encoding="utf-8")
    ).lower()
    for role in ("specification planner", "optimization runner", "exact-patch applier"):
        assert role in text


def test_adapter_mapping_document_does_not_claim_datasets_are_staged_in_worktrees() -> None:
    """Datasets/evaluators are identity- and hash-pinned in the spec, not files in a worktree.

    Regression guard for the earlier (incorrect) design, which claimed development/validation
    datasets were staged inside a candidate's worktree and that deleting the worktree was what
    enforced held-out opacity. Neither claim should appear anymore.
    """
    text = _normalized(
        (SKILL_ROOT / "ADAPTER_MAPPING.md").read_text(encoding="utf-8")
    ).lower()
    assert "staged only inside a campaign's contained" not in text
    assert "held-out set stays opaque" not in text
    assert "deleted after the campaign, so no dataset content" not in text


def test_adapter_mapping_document_describes_pinned_dataset_identity_and_evaluation_adapter() -> (
    None
):
    raw_text = (SKILL_ROOT / "ADAPTER_MAPPING.md").read_text(encoding="utf-8")
    lowered = _normalized(raw_text).lower()
    assert "content_sha256" in raw_text
    assert "development" in lowered and "idea generation" in lowered
    assert "held-out validation rows" in lowered or "held-out rows" in lowered
    assert "outside" in lowered and "source bundle" in lowered
    assert "evaluation adapter" in lowered
    assert "worktrees isolate code experiments" in lowered


def test_adapter_mapping_document_keeps_applier_out_of_selection_and_repair() -> None:
    text = _normalized(
        (SKILL_ROOT / "ADAPTER_MAPPING.md").read_text(encoding="utf-8")
    ).lower()
    assert "no selection" in text
    assert "no repair" in text
    assert "never re-runs or second-guesses this selection" in text


def test_skill_template_is_top_level_and_not_inside_the_snapshot() -> None:
    skill_doc = SKILL_ROOT / "SKILL.md"
    assert skill_doc.is_file()
    assert skill_doc.parent == SKILL_ROOT
    assert SNAPSHOT_ROOT not in skill_doc.parents


def test_skill_template_directs_the_three_named_agents() -> None:
    text = _normalized((SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")).lower()
    for role in (
        "specification planner",
        "candidate designer",
        "exact-patch applier",
    ):
        assert role in text
    assert "foundry-opt steward advance --issue <number>" in text
    assert "candidatedesignintent" in text


def test_skill_template_specification_planner_does_not_brainstorm_candidates() -> None:
    text = _normalized((SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")).lower()
    assert "immutable specification" in text
    assert "does not brainstorm or evaluate candidate ideas" in text


def test_skill_template_candidate_designer_owns_bounded_idea_generation() -> None:
    text = _normalized((SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")).lower()
    assert "only specialist that generates ideas or writes candidate code" in text
    assert "bounded" in text


def test_skill_template_exact_patch_applier_has_no_selection_or_repair() -> None:
    text = _normalized((SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")).lower()
    assert "performs no selection" in text
    assert "no repair" in text


def test_skill_template_does_not_claim_datasets_are_staged_in_worktrees() -> None:
    text = _normalized((SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")).lower()
    assert "no dataset content is staged there" in text
    assert "held-out set stays opaque" not in text


def test_skill_template_references_the_vendored_snapshot() -> None:
    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert "references/tenzing" in text


def test_skill_template_does_not_claim_upstream_endorsement() -> None:
    text = _normalized((SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")).lower()
    assert "not an upstream" in text
    assert "no endorsement" in text or "implies no endorsement" in text
