from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path

import pytest

from foundry_opt.orchestration import (
    GhWorkspaceCandidateProvenanceResolver,
    TrustedWorkspaceCandidateImportContext,
    WorkspaceCandidateImportEvent,
    trusted_workspace_candidate_import_context_from_environment,
    workspace_assignment_marker_key,
)
from foundry_opt.preflight.interfaces import CommandResult


_REPOSITORY = "octo-org/optimizer"
_SOURCE_SHA = "a" * 40
_STATE_REVISION = "b" * 40
_ACTOR = {
    "id": 198982749,
    "login": "Copilot",
    "type": "Bot",
}


class FakeCommands:
    def __init__(self, responses: Mapping[tuple[str, ...], object]) -> None:
        self.responses = dict(responses)
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
        **_: object,
    ) -> CommandResult:
        del cwd
        command = tuple(arguments)
        self.calls.append(command)
        if command not in self.responses:
            raise AssertionError(f"unexpected command: {command}")
        return CommandResult(
            0,
            json.dumps(self.responses[command]),
            "",
        )


def _context(
    **overrides: object,
) -> TrustedWorkspaceCandidateImportContext:
    values: dict[str, object] = {
        "repository": _REPOSITORY,
        "workspace_pr_number": 104,
        "candidate_source_commit_sha": _SOURCE_SHA,
        "expected_state_revision": _STATE_REVISION,
        "importer_workflow_run_id": 9001,
        "trusted_event_name": "issue_comment",
        "acknowledgement_comment_id": 501,
    }
    values.update(overrides)
    return TrustedWorkspaceCandidateImportContext(**values)


def _responses(
    *,
    actor: Mapping[str, object] = _ACTOR,
    head_sha: str = _SOURCE_SHA,
    comment_body: str | None = None,
    comment_actor: Mapping[str, object] | None = None,
    commit_url: str | None = None,
) -> dict[tuple[str, ...], object]:
    marker_key = workspace_assignment_marker_key(31, _STATE_REVISION)
    acknowledgement = (
        comment_body
        or (
            "<!-- foundry-opt:workspace-candidate-ack:"
            f"{marker_key}:candidate-1:{_SOURCE_SHA} -->"
        )
    )
    return {
        ("gh", "api", f"repos/{_REPOSITORY}/pulls/104"): {
            "number": 104,
            "state": "open",
            "head": {
                "sha": head_sha,
                "repo": {"full_name": _REPOSITORY},
            },
        },
        ("gh", "api", f"repos/{_REPOSITORY}/commits/{_SOURCE_SHA}"): {
            "sha": _SOURCE_SHA,
            "html_url": (
                commit_url
                or f"https://github.com/{_REPOSITORY}/commit/{_SOURCE_SHA}"
            ),
            "author": dict(actor),
        },
        (
            "gh",
            "api",
            (
                f"repos/{_REPOSITORY}/issues/104/comments?"
                "per_page=100&sort=created&direction=desc"
            ),
        ): [
                {
                    "id": 501,
                    "html_url": (
                        f"https://github.com/{_REPOSITORY}/pull/"
                        "104#issuecomment-501"
                    ),
                    "body": acknowledgement,
                    "user": dict(comment_actor or actor),
                }
        ],
        (
            "gh",
            "api",
            f"repos/{_REPOSITORY}/issues/comments/501",
        ): {
            "id": 501,
            "html_url": (
                f"https://github.com/{_REPOSITORY}/pull/"
                "104#issuecomment-501"
            ),
            "body": acknowledgement,
            "user": dict(comment_actor or actor),
        },
    }


def _resolve(
    tmp_path: Path,
    *,
    context: TrustedWorkspaceCandidateImportContext | None = None,
    responses: Mapping[tuple[str, ...], object] | None = None,
):
    return GhWorkspaceCandidateProvenanceResolver(
        FakeCommands(responses or _responses()),
        repository_root=tmp_path,
    ).resolve(
        issue_number=31,
        candidate_id="candidate-1",
        context=context or _context(),
    )


def test_direct_issue_comment_capture_binds_trusted_github_metadata(
    tmp_path: Path,
) -> None:
    provenance = _resolve(tmp_path)

    assert provenance.copilot_actor_id == _ACTOR["id"]
    assert provenance.candidate_source_commit_sha == _SOURCE_SHA
    assert provenance.acknowledgement_comment_id == 501
    assert provenance.acknowledgement_comment_url.endswith(
        "/pull/104#issuecomment-501"
    )
    assert provenance.assignment_marker_key == (
        workspace_assignment_marker_key(31, _STATE_REVISION)
    )
    assert provenance.importer_workflow_run_url == (
        f"https://github.com/{_REPOSITORY}/actions/runs/9001"
    )
    assert provenance.trusted_event_name is (
        WorkspaceCandidateImportEvent.ISSUE_COMMENT
    )


def test_scheduled_capture_recovers_the_exact_acknowledgement(
    tmp_path: Path,
) -> None:
    provenance = _resolve(
        tmp_path,
        context=_context(
            trusted_event_name="schedule",
            acknowledgement_comment_id=None,
        ),
    )

    assert provenance.trusted_event_name is (
        WorkspaceCandidateImportEvent.SCHEDULE
    )
    assert provenance.acknowledgement_comment_id == 501


def test_scheduled_capture_accepts_missing_acknowledgement(
    tmp_path: Path,
) -> None:
    responses = _responses()
    endpoint = (
        "gh",
        "api",
        (
            f"repos/{_REPOSITORY}/issues/104/comments?"
            "per_page=100&sort=created&direction=desc"
        ),
    )
    responses[endpoint] = []

    provenance = _resolve(
        tmp_path,
        context=_context(
            trusted_event_name="schedule",
            acknowledgement_comment_id=None,
        ),
        responses=responses,
    )

    assert provenance.acknowledgement_comment_id is None
    assert provenance.acknowledgement_comment_url is None
    assert provenance.candidate_source_commit_sha == _SOURCE_SHA


def test_scheduled_capture_rejects_multiple_acknowledgements(
    tmp_path: Path,
) -> None:
    responses = _responses()
    endpoint = (
        "gh",
        "api",
        (
            f"repos/{_REPOSITORY}/issues/104/comments?"
            "per_page=100&sort=created&direction=desc"
        ),
    )
    first = responses[endpoint][0]
    responses[endpoint] = [
        first,
        {
            **first,
            "id": 502,
            "html_url": (
                f"https://github.com/{_REPOSITORY}/pull/"
                "104#issuecomment-502"
            ),
        },
    ]

    with pytest.raises(ValueError, match="ambiguous"):
        _resolve(
            tmp_path,
            context=_context(
                trusted_event_name="schedule",
                acknowledgement_comment_id=None,
            ),
            responses=responses,
        )


def test_scheduled_capture_rejects_spoofed_acknowledgement(
    tmp_path: Path,
) -> None:
    responses = _responses()
    endpoint = (
        "gh",
        "api",
        (
            f"repos/{_REPOSITORY}/issues/104/comments?"
            "per_page=100&sort=created&direction=desc"
        ),
    )
    responses[endpoint][0]["user"] = {
        "id": 7,
        "login": "Copilot",
        "type": "Bot",
    }

    with pytest.raises(ValueError, match="acknowledgement actor"):
        _resolve(
            tmp_path,
            context=_context(
                trusted_event_name="schedule",
                acknowledgement_comment_id=None,
            ),
            responses=responses,
        )


def test_scheduled_capture_rejects_foreign_acknowledgement_url(
    tmp_path: Path,
) -> None:
    responses = _responses()
    endpoint = (
        "gh",
        "api",
        (
            f"repos/{_REPOSITORY}/issues/104/comments?"
            "per_page=100&sort=created&direction=desc"
        ),
    )
    responses[endpoint][0]["html_url"] = (
        "https://github.com/other/repository/pull/"
        "104#issuecomment-501"
    )

    with pytest.raises(ValueError, match="same repository"):
        _resolve(
            tmp_path,
            context=_context(
                trusted_event_name="schedule",
                acknowledgement_comment_id=None,
            ),
            responses=responses,
        )


def test_direct_context_requires_acknowledgement_comment_id() -> None:
    with pytest.raises(ValueError, match="direct.*comment ID"):
        _context(acknowledgement_comment_id=None)


@pytest.mark.parametrize(
    ("responses", "message"),
    [
        (
            _responses(
                actor={"id": 7, "login": "octocat", "type": "User"}
            ),
            "Copilot commit actor",
        ),
        (
            _responses(head_sha="c" * 40),
            "workspace PR head",
        ),
        (
            _responses(
                comment_body=(
                    "<!-- foundry-opt:workspace-candidate-ack:"
                    f"{workspace_assignment_marker_key(31, _STATE_REVISION)}:"
                    f"candidate-1:{'c' * 40} -->"
                )
            ),
            "acknowledgement",
        ),
        (
            _responses(
                comment_actor={
                    "id": 7,
                    "login": "Copilot",
                    "type": "Bot",
                }
            ),
            "acknowledgement",
        ),
        (
            _responses(
                commit_url=(
                    "https://github.com/other/repository/commit/"
                    + _SOURCE_SHA
                )
            ),
            "same repository",
        ),
    ],
)
def test_capture_fails_closed_on_spoof_or_lineage_mismatch(
    tmp_path: Path,
    responses: Mapping[tuple[str, ...], object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _resolve(tmp_path, responses=responses)


def test_assignment_marker_key_is_revision_bound() -> None:
    assert workspace_assignment_marker_key(31, _STATE_REVISION) == (
        "issue-31:"
        + hashlib.sha256(_STATE_REVISION.encode("utf-8")).hexdigest()[:16]
        + ":v1"
    )


def test_workflow_dispatch_scanner_origin_comes_from_trusted_environment() -> None:
    context = trusted_workspace_candidate_import_context_from_environment(
        {
            "GITHUB_ACTIONS": "true",
            "TRUSTED_ACK_COMMENT_ID": "",
            "TRUSTED_CANDIDATE_IMPORT_ORIGIN": "schedule",
            "TRUSTED_EXPECTED_REVISION": _STATE_REVISION,
            "TRUSTED_HEAD_SHA": _SOURCE_SHA,
            "TRUSTED_PULL_REQUEST_NUMBER": "104",
            "TRUSTED_REPOSITORY": _REPOSITORY,
            "TRUSTED_RUN_ID": "9001",
        }
    )

    assert context is not None
    assert context.trusted_event_name is WorkspaceCandidateImportEvent.SCHEDULE
    assert context.acknowledgement_comment_id is None


def test_scheduled_capture_rejects_comment_history_over_bound(
    tmp_path: Path,
) -> None:
    responses = _responses()
    endpoint = (
        "gh",
        "api",
        (
            f"repos/{_REPOSITORY}/issues/104/comments?"
            "per_page=100&sort=created&direction=desc"
        ),
    )
    responses[endpoint] = [
        {"id": index, "body": "", "html_url": "", "user": _ACTOR}
        for index in range(1, 102)
    ]

    with pytest.raises(ValueError, match="exceeds its bound"):
        _resolve(
            tmp_path,
            context=_context(
                trusted_event_name="schedule",
                acknowledgement_comment_id=None,
            ),
            responses=responses,
        )
