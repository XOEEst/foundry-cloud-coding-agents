from foundry_opt.orchestration import (
    WorkspaceStateMigrationPlan,
    workspace_state_v3_migration_plan,
)


def test_v3_migration_plan_is_read_only_and_preserves_legacy_paths() -> None:
    plan = workspace_state_v3_migration_plan(
        issue_number=31,
        source_revision="a" * 40,
        source_paths=(
            "snapshot.json",
            "journal.jsonl",
            "inbox/event-1.json",
            "outbox/effect-1.json",
            "objects/evidence/" + "b" * 64 + ".json",
        ),
    )

    assert plan == WorkspaceStateMigrationPlan(
        issue_number=31,
        source_ref="refs/heads/foundry-opt/state/issue-31",
        source_revision="a" * 40,
        source_schema_version=3,
        target_schema_version=4,
        legacy_paths=(
            "snapshot.json",
            "journal.jsonl",
            "inbox/event-1.json",
            "outbox/effect-1.json",
            "objects/evidence/" + "b" * 64 + ".json",
        ),
        read_only=True,
    )
