from dataclasses import FrozenInstanceError, replace
import hashlib
import json

import pytest

from foundry_opt.orchestration import (
    WorkspaceCandidateImportEvent,
    WorkspaceCandidateProvenance,
    parse_workspace_candidate_provenance,
    workspace_candidate_provenance_document,
)


def _provenance(
    **overrides: object,
) -> WorkspaceCandidateProvenance:
    values: dict[str, object] = {
        "copilot_actor_id": 198982749,
        "copilot_actor_login": "Copilot",
        "candidate_source_commit_sha": "a" * 40,
        "candidate_source_commit_url": (
            "https://github.com/octo-org/optimizer/commit/" + "a" * 40
        ),
        "acknowledgement_comment_id": 501,
        "acknowledgement_comment_url": (
            "https://github.com/octo-org/optimizer/pull/"
            "104#issuecomment-501"
        ),
        "assignment_marker_key": "issue-31:assignment-a1:v1",
        "workspace_pr_number": 104,
        "importer_workflow_run_id": 9001,
        "importer_workflow_run_url": (
            "https://github.com/octo-org/optimizer/actions/runs/9001"
        ),
        "trusted_event_name": "issue_comment",
    }
    values.update(overrides)
    return WorkspaceCandidateProvenance(**values)


def test_direct_copilot_comment_provenance_is_typed_and_frozen() -> None:
    provenance = _provenance()

    assert provenance.copilot_actor_login == "Copilot"
    assert provenance.trusted_event_name is (
        WorkspaceCandidateImportEvent.ISSUE_COMMENT
    )
    assert provenance.repository == "octo-org/optimizer"
    with pytest.raises(FrozenInstanceError):
        provenance.workspace_pr_number = 105  # type: ignore[misc]


def test_scheduled_import_provenance_is_valid() -> None:
    provenance = _provenance(trusted_event_name="schedule")

    assert provenance.trusted_event_name is (
        WorkspaceCandidateImportEvent.SCHEDULE
    )


def test_scheduled_import_allows_unavailable_acknowledgement() -> None:
    provenance = _provenance(
        trusted_event_name="schedule",
        acknowledgement_comment_id=None,
        acknowledgement_comment_url=None,
    )

    assert provenance.acknowledgement_comment_id is None
    assert provenance.acknowledgement_comment_url is None
    assert parse_workspace_candidate_provenance(
        workspace_candidate_provenance_document(provenance)
    ) == provenance


@pytest.mark.parametrize(
    ("comment_id", "comment_url"),
    [
        (501, None),
        (
            None,
            "https://github.com/octo-org/optimizer/pull/"
            "104#issuecomment-501",
        ),
    ],
)
def test_acknowledgement_identity_must_be_present_as_a_pair(
    comment_id: int | None,
    comment_url: str | None,
) -> None:
    with pytest.raises(ValueError, match="acknowledgement.*together"):
        _provenance(
            trusted_event_name="schedule",
            acknowledgement_comment_id=comment_id,
            acknowledgement_comment_url=comment_url,
        )


def test_direct_import_requires_acknowledgement_pair() -> None:
    with pytest.raises(ValueError, match="direct.*acknowledgement"):
        _provenance(
            acknowledgement_comment_id=None,
            acknowledgement_comment_url=None,
        )


def test_copilot_app_login_is_normalized() -> None:
    provenance = _provenance(
        copilot_actor_login="app/copilot-swe-agent"
    )

    assert provenance.copilot_actor_login == "copilot-swe-agent[bot]"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "candidate_source_commit_url",
            "https://github.com/octo-org/optimizer/commit/" + "b" * 40,
            "source commit URL",
        ),
        (
            "acknowledgement_comment_url",
            "https://github.com/octo-org/optimizer/pull/"
            "105#issuecomment-501",
            "acknowledgement comment URL",
        ),
        (
            "acknowledgement_comment_url",
            "https://github.com/octo-org/optimizer/pull/"
            "104#issuecomment-502",
            "acknowledgement comment URL",
        ),
        (
            "importer_workflow_run_url",
            "https://github.com/octo-org/optimizer/actions/runs/9002",
            "workflow run URL",
        ),
    ],
)
def test_urls_must_match_bound_ids_and_sha(
    field: str,
    value: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _provenance(**{field: value})


@pytest.mark.parametrize(
    "field",
    [
        "acknowledgement_comment_url",
        "importer_workflow_run_url",
    ],
)
def test_all_urls_must_use_the_source_repository(field: str) -> None:
    suffix = (
        "pull/104#issuecomment-501"
        if field == "acknowledgement_comment_url"
        else "actions/runs/9001"
    )

    with pytest.raises(ValueError, match="same repository"):
        _provenance(
            **{field: f"https://github.com/other/repository/{suffix}"}
        )


@pytest.mark.parametrize(
    ("field", "url"),
    [
        (
            "candidate_source_commit_url",
            (
                "https://token@github.com/octo-org/optimizer/commit/"
                + "a" * 40
            ),
        ),
        (
            "acknowledgement_comment_url",
            (
                "https://github.com/octo-org/optimizer/pull/"
                "104?token=secret#issuecomment-501"
            ),
        ),
        (
            "importer_workflow_run_url",
            (
                "https://github.com/octo-org/optimizer/actions/"
                "runs/9001#attempt-1"
            ),
        ),
    ],
)
def test_urls_reject_credentials_query_and_fragment(
    field: str,
    url: str,
) -> None:
    with pytest.raises(ValueError, match="canonical HTTPS GitHub URL"):
        _provenance(**{field: url})


@pytest.mark.parametrize(
    "login",
    ["copilot", "copilot-swe-agent", "copilot-swe-agent[bot]-spoof"],
)
def test_spoofed_copilot_login_is_rejected(login: str) -> None:
    with pytest.raises(ValueError, match="Copilot actor login"):
        _provenance(copilot_actor_login=login)


@pytest.mark.parametrize("event_name", ["push", "workflow_dispatch"])
def test_unsupported_import_event_is_rejected(event_name: str) -> None:
    with pytest.raises(ValueError, match="trusted event"):
        _provenance(trusted_event_name=event_name)


@pytest.mark.parametrize(
    "field",
    [
        "copilot_actor_id",
        "acknowledgement_comment_id",
        "workspace_pr_number",
        "importer_workflow_run_id",
    ],
)
@pytest.mark.parametrize("value", [0, -1, True])
def test_github_numeric_identities_are_positive_integers(
    field: str,
    value: int,
) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        _provenance(**{field: value})


@pytest.mark.parametrize(
    "marker",
    ["", "contains whitespace", "<!-- comment body -->", "a" * 129],
)
def test_assignment_marker_key_is_bounded_and_safe(marker: str) -> None:
    with pytest.raises(ValueError, match="assignment marker"):
        _provenance(assignment_marker_key=marker)


def test_canonical_serialization_and_identity_are_deterministic() -> None:
    first = _provenance()
    second = replace(first)
    document = {
        "acknowledgement_comment_id": 501,
        "acknowledgement_comment_url": (
            "https://github.com/octo-org/optimizer/pull/"
            "104#issuecomment-501"
        ),
        "assignment_marker_key": "issue-31:assignment-a1:v1",
        "candidate_source_commit_sha": "a" * 40,
        "candidate_source_commit_url": (
            "https://github.com/octo-org/optimizer/commit/" + "a" * 40
        ),
        "copilot_actor_id": 198982749,
        "copilot_actor_login": "Copilot",
        "importer_workflow_run_id": 9001,
        "importer_workflow_run_url": (
            "https://github.com/octo-org/optimizer/actions/runs/9001"
        ),
        "schema_version": 1,
        "trusted_event_name": "issue_comment",
        "workspace_pr_number": 104,
    }
    expected = json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )

    assert first.canonical_json == expected
    assert second.canonical_json == expected
    assert first.identity_sha256 == hashlib.sha256(
        expected.encode("utf-8")
    ).hexdigest()
    assert second.identity_sha256 == first.identity_sha256


def test_nullable_acknowledgement_serialization_is_deterministic() -> None:
    first = _provenance(
        trusted_event_name="schedule",
        acknowledgement_comment_id=None,
        acknowledgement_comment_url=None,
    )
    second = replace(first)
    document = workspace_candidate_provenance_document(first)

    assert document["schema_version"] == 1
    assert document["acknowledgement_comment_id"] is None
    assert document["acknowledgement_comment_url"] is None
    assert parse_workspace_candidate_provenance(document) == first
    assert second.canonical_json == first.canonical_json
    assert second.identity_sha256 == first.identity_sha256
