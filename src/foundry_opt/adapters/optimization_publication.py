"""Production :class:`~foundry_opt.optimization.runner.CampaignPublisher`.

This module owns the seam between the finalized, in-repository campaign
artifacts (the exact patch files and redacted development/validation
evidence already written to disk and referenced by
:class:`~foundry_opt.campaign.models.CampaignReport`) and the GitHub
publication workflow. It never trusts a directory listing: every file it
commits is read from an explicit path already validated by the campaign
report or evidence manifests, never a glob over the campaign directory, so
stray or leftover files can never be swept into the published commit.

The artifact commit itself is assembled purely through Git plumbing
(``read-tree``/``hash-object``/``write-tree``/``commit-tree`` against a
temporary index file) by delegating to the existing
:class:`foundry_opt.adapters.github_optimization.GitSpecPublisher`, so it
never touches ``HEAD``, the real index, or the working tree, and is
byte-for-byte reproducible across retries. The GitHub side (pull request and
candidate issue publication, plus idempotent reconciliation of an
already-published campaign) is delegated unchanged to
:func:`foundry_opt.github_workflow.publish_campaign` through a
:class:`foundry_opt.adapters.github_campaign.GhCampaignGateway` constructed
with the explicit ``CAMPAIGN_PUBLICATION`` capability set.

Every failure mode raises a stable, typed
:class:`~foundry_opt.optimization.runner.CapabilityUnavailableError`
subclass. Because the runner only marks a campaign ``finalized`` once
:meth:`CampaignPublisher.publish` returns successfully, any raised error
(including a partial GitHub publication) leaves the campaign state
resumable: the next invocation rebuilds the identical deterministic commit
and re-runs the (idempotent) GitHub publication, picking up exactly where
it left off.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import errno
import hashlib
import json
from math import isfinite
import os
from pathlib import Path
import re
import stat

from foundry_opt.adapters.github_campaign import GhCampaignGateway
from foundry_opt.adapters.github_optimization import (
    GitSpecPublisher,
    GitSpecPublisherError,
)
from foundry_opt.campaign.models import CampaignReport, CandidateArtifact
from foundry_opt.campaign.state import FinalizedPublication
from foundry_opt.evidence.models import EvidenceManifest
from foundry_opt.github_workflow.errors import (
    CampaignPublicationError as GitHubCampaignPublicationError,
    GitHubPermissionDeniedError,
)
from foundry_opt.github_workflow.models import (
    ArtifactReference,
    CampaignPublicationRequest,
    GitHubCapabilities,
    RedactionProvenance,
    git_branch,
)
from foundry_opt.github_workflow.publication import CampaignGateway, publish_campaign
from foundry_opt.optimization.runner import (
    CampaignPublicationInputs,
    CapabilityUnavailableError,
)
from foundry_opt.preflight.interfaces import CommandRunner
from foundry_opt.security import reject_secret_content


_ISSUE_CAMPAIGN_ID = re.compile(r"^issue-(\d+)$")
_MANIFEST_GENERATOR = "foundry-opt-campaign-publisher"
_MANIFEST_FILENAME = "manifest.json"


# ---------------------------------------------------------------------------
# Stable typed errors
# ---------------------------------------------------------------------------


class CampaignArtifactError(CapabilityUnavailableError):
    """Base for campaign artifact assembly/publication failures.

    Subclassing :class:`CapabilityUnavailableError` means the existing
    runner (unmodified) already surfaces every one of these as a typed
    ``blocked`` result instead of crashing, while each subclass still keeps
    its own stable ``code`` for callers that want to branch on the exact
    failure.
    """


class UnsafeCampaignArtifactPathError(CampaignArtifactError):
    def __init__(self, message: str) -> None:
        super().__init__("campaign_artifact_unsafe_path", message)


class CampaignArtifactMissingError(CampaignArtifactError):
    def __init__(self, message: str) -> None:
        super().__init__("campaign_artifact_missing", message)


class CampaignArtifactMismatchError(CampaignArtifactError):
    def __init__(self, message: str) -> None:
        super().__init__("campaign_artifact_mismatch", message)


class StaleCampaignBaseError(CampaignArtifactError):
    def __init__(self, message: str) -> None:
        super().__init__("campaign_base_stale", message)


class CampaignEvidenceRedactionError(CampaignArtifactError):
    def __init__(self, message: str) -> None:
        super().__init__("campaign_evidence_redaction_failed", message)


class CampaignPublicationPermissionError(CampaignArtifactError):
    def __init__(self, message: str) -> None:
        super().__init__("campaign_publication_permission_denied", message)


class CampaignPublicationVerificationError(CampaignArtifactError):
    def __init__(self, message: str) -> None:
        super().__init__("campaign_publication_verification_failed", message)


class PartialCampaignPublicationError(CampaignArtifactError):
    """Raised when one or more candidate-issue publication steps failed.

    The campaign pull request and any candidate issues that *did* publish
    remain on GitHub; the campaign state is left un-finalized so the next
    invocation retries only the missing steps through the same idempotent
    lookups ``publish_campaign`` already performs.
    """

    def __init__(self, failures: tuple[object, ...]) -> None:
        self.failures = tuple(failures)
        super().__init__(
            "campaign_publication_partial",
            f"{len(self.failures)} campaign publication step(s) failed",
        )


# ---------------------------------------------------------------------------
# Publisher
# ---------------------------------------------------------------------------


class CampaignPublisher:
    """Assembles the exact campaign artifact commit and publishes it."""

    def __init__(
        self,
        command_runner: CommandRunner,
        *,
        gateway_factory: Callable[[Path], CampaignGateway] | None = None,
    ) -> None:
        self._commands = command_runner
        self._commit_builder = GitSpecPublisher(command_runner)
        self._gateway_factory = gateway_factory or self._default_gateway

    def _default_gateway(self, repository_root: Path) -> CampaignGateway:
        return GhCampaignGateway(
            self._commands,
            repository_root,
            granted_capabilities=GitHubCapabilities.CAMPAIGN_PUBLICATION,
        )

    def publish(
        self,
        inputs: CampaignPublicationInputs,
    ) -> FinalizedPublication:
        root = inputs.repository_root.expanduser().resolve()
        report = inputs.report
        gateway = self._gateway_factory(root)

        state = gateway.repository_state(root)
        if state.default_commit != report.base_commit:
            raise StaleCampaignBaseError(
                "the repository default branch has moved past the exact "
                f"campaign base commit {report.base_commit}"
            )

        evidence_by_path = _evidence_manifests_by_relative_path(root, inputs)
        files, evidence_sha256, manifest_path = _collect_campaign_files(
            root,
            report,
            evidence_by_path,
        )

        try:
            head_commit = self._commit_builder.prepare_commit(
                root,
                base_commit=report.base_commit,
                files=files,
                message=(
                    f"foundry-opt: campaign {report.campaign_id} artifacts "
                    f"for {report.target}"
                ),
            )
        except GitSpecPublisherError as error:
            raise StaleCampaignBaseError(
                "the exact campaign base commit "
                f"{report.base_commit} is not available in the repository"
            ) from error

        request = CampaignPublicationRequest(
            repository_root=root,
            report=report,
            head_branch=git_branch(
                f"campaign/{report.campaign_id}", "campaign head_branch"
            ),
            head_commit=head_commit,
            manifests=(
                ArtifactReference(
                    path=manifest_path,
                    sha256=hashlib.sha256(files[manifest_path]).hexdigest(),
                    provenance=RedactionProvenance(
                        generator=_MANIFEST_GENERATOR,
                        schema_version=1,
                        source_sha256=_lineage_sha256(
                            report, evidence_sha256
                        ),
                    ),
                ),
            ),
            evidence_sha256=evidence_sha256,
            reproduction_instructions=inputs.reproduction_instructions,
        )

        try:
            publication = publish_campaign(request, gateway)
        except GitHubPermissionDeniedError as error:
            raise CampaignPublicationPermissionError(
                "the GitHub token lacks the campaign-publication capability"
            ) from error
        except GitHubCampaignPublicationError as error:
            raise CampaignPublicationVerificationError(str(error)) from error

        if publication.failures:
            raise PartialCampaignPublicationError(publication.failures)

        return FinalizedPublication(
            campaign_pull_request_number=(
                publication.campaign_pull_request.number
            ),
            campaign_pull_request_url=(
                publication.campaign_pull_request.url
            ),
            candidate_issue_numbers={
                item.candidate_id: item.issue.number
                for item in publication.candidate_issues
            },
        )


# ---------------------------------------------------------------------------
# Artifact assembly helpers
# ---------------------------------------------------------------------------


def _relative_repository_path(root: Path, path: Path) -> Path:
    candidate = path if path.is_absolute() else root / path
    resolved = candidate.expanduser().resolve()
    if not resolved.is_relative_to(root):
        raise UnsafeCampaignArtifactPathError(
            f"campaign artifact path escapes the repository: {path}"
        )
    return resolved.relative_to(root)


def _open_no_follow(path: Path) -> int:
    """Open ``path`` for reading without ever following a trailing symlink.

    Isolated in its own function (rather than inlined) purely so tests can
    monkeypatch it to simulate a TOCTOU race -- the filesystem target being
    swapped for a symlink in the instant between the containment checks
    below and the moment the file is actually opened -- and confirm the
    read still fails closed instead of silently following the swapped
    link. ``O_NOFOLLOW`` makes this atomic on platforms that define it
    (Linux/macOS: the open itself fails with ``ELOOP``); platforms without
    it (Windows) still benefit from the symlink pre-checks and the
    post-open ``fstat``/containment re-check performed by the caller.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    return os.open(str(path), flags)


def _read_contained_file(root: Path, relative: Path) -> bytes:
    """Read ``relative`` under ``root`` without ever trusting a symlink.

    The relative path itself was already validated as repository-relative
    (no ``..`` segments, no absolute drive) by the frozen dataclasses that
    produced it; this guards the remaining, filesystem-level attack: a
    path component on disk being (or becoming) a symlink that escapes the
    repository even though the string path itself is safe.

    Every directory component is walked and rejected the instant it is a
    symlink, then the final component is opened through
    :func:`_open_no_follow` (never through a path-based ``read``, which
    would leave a check-then-open race), the resulting descriptor is
    ``fstat``-ed to confirm it names a plain regular file (never a
    directory, FIFO, device, or symlink that slipped past ``O_NOFOLLOW``
    on a platform that ignores it), its containment under ``root`` is
    re-resolved from the *opened* descriptor's path, and only then are
    bytes read -- straight from the descriptor, so binary content is
    never touched by any text decode.
    """
    parts = relative.parts
    if not parts:
        raise UnsafeCampaignArtifactPathError(
            f"campaign artifact path is empty: {relative.as_posix()}"
        )
    current = root
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise UnsafeCampaignArtifactPathError(
                "campaign artifact path contains a symlink: "
                f"{relative.as_posix()}"
            )
    final = current / parts[-1]
    if final.is_symlink():
        raise UnsafeCampaignArtifactPathError(
            f"campaign artifact path contains a symlink: {relative.as_posix()}"
        )

    try:
        descriptor = _open_no_follow(final)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise UnsafeCampaignArtifactPathError(
                "campaign artifact path contains a symlink: "
                f"{relative.as_posix()}"
            ) from error
        if error.errno in (errno.ENOENT, errno.ENOTDIR):
            raise CampaignArtifactMissingError(
                f"campaign artifact is missing: {relative.as_posix()}"
            ) from error
        raise CampaignArtifactMissingError(
            f"campaign artifact could not be opened: {relative.as_posix()}"
        ) from error

    closed = False
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise UnsafeCampaignArtifactPathError(
                f"campaign artifact is not a regular file: "
                f"{relative.as_posix()}"
            )
        resolved_root = root.resolve()
        resolved_final = final.resolve()
        if not resolved_final.is_relative_to(resolved_root):
            raise UnsafeCampaignArtifactPathError(
                "campaign artifact path escapes the repository: "
                f"{relative.as_posix()}"
            )
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            closed = True  # ownership transferred to the file object
            return stream.read()
    finally:
        if not closed:
            os.close(descriptor)


def _evidence_manifests_by_relative_path(
    root: Path,
    inputs: CampaignPublicationInputs,
) -> dict[Path, EvidenceManifest]:
    manifests = [inputs.development_evidence]
    if inputs.validation_evidence is not None:
        manifests.append(inputs.validation_evidence)
    result: dict[Path, EvidenceManifest] = {}
    for manifest in manifests:
        result[_relative_repository_path(root, manifest.path)] = manifest
    return result


def _collect_campaign_files(
    root: Path,
    report: CampaignReport,
    evidence_by_path: Mapping[Path, EvidenceManifest],
) -> tuple[dict[Path, bytes], dict[str, str], Path]:
    """Read the exact patch and evidence bytes referenced by every Pareto
    candidate (never a directory scan), verify each against its recorded
    hash, and build the compact manifest binding the goal/spec/asset/
    lineage hashes together. Returns the files to commit, the per-candidate
    evidence SHA-256 mapping, and the manifest's repository-relative path.
    """
    candidates_by_id = {
        candidate.candidate_id: candidate for candidate in report.candidates
    }

    # A single evidence file is legitimately shared by more than one
    # candidate (deduplicated below via the ``files`` cache), so the
    # schema/redaction and identity checks must run once per unique file
    # but cover every candidate that references it -- not only whichever
    # candidate happens to trigger the cache miss first.
    candidate_ids_by_evidence_path: dict[Path, list[str]] = {}
    for candidate_id in report.pareto_candidate_ids:
        candidate_ids_by_evidence_path.setdefault(
            candidates_by_id[candidate_id].evidence_path, []
        ).append(candidate_id)

    files: dict[Path, bytes] = {}
    evidence_sha256: dict[str, str] = {}
    for candidate_id in report.pareto_candidate_ids:
        candidate = candidates_by_id[candidate_id]

        patch_bytes = files.get(candidate.patch.path)
        if patch_bytes is None:
            patch_bytes = _read_contained_file(root, candidate.patch.path)
            if hashlib.sha256(patch_bytes).hexdigest() != candidate.patch.sha256:
                raise CampaignArtifactMismatchError(
                    f"patch artifact for {candidate_id!r} does not match "
                    "its recorded hash"
                )
            files[candidate.patch.path] = patch_bytes

        evidence_manifest = evidence_by_path.get(candidate.evidence_path)
        if evidence_manifest is None:
            raise CampaignArtifactMissingError(
                "no development/validation evidence manifest references "
                f"{candidate.evidence_path.as_posix()!r} for {candidate_id!r}"
            )
        evidence_bytes = files.get(candidate.evidence_path)
        if evidence_bytes is None:
            evidence_bytes = _read_contained_file(
                root, candidate.evidence_path
            )
            if (
                hashlib.sha256(evidence_bytes).hexdigest()
                != evidence_manifest.sha256
            ):
                raise CampaignArtifactMismatchError(
                    f"evidence artifact for {candidate_id!r} does not match "
                    "its recorded hash"
                )
            _verify_evidence_redaction(
                evidence_bytes,
                report=report,
                candidates_by_id=candidates_by_id,
                candidate_ids=candidate_ids_by_evidence_path[
                    candidate.evidence_path
                ],
            )
            files[candidate.evidence_path] = evidence_bytes
        evidence_sha256[candidate_id] = evidence_manifest.sha256

    manifest_path = _manifest_path(evidence_by_path)
    manifest_bytes = _build_manifest_bytes(report, evidence_sha256)
    files[manifest_path] = manifest_bytes
    return files, evidence_sha256, manifest_path


def _verify_evidence_redaction(
    evidence_bytes: bytes,
    *,
    report: CampaignReport,
    candidates_by_id: Mapping[str, CandidateArtifact],
    candidate_ids: Sequence[str],
) -> None:
    """Re-verify a redacted evidence file before it is ever committed.

    This does two independent things, both required, neither sufficient on
    its own:

    1. :func:`_validate_evidence_schema` structurally whitelists the exact
       document shape ``write_redacted_evidence`` emits (schema_version,
       campaign/source/goal/spec hashes, asset identity, the normalized
       baseline/candidate result schema, and coded Pareto decisions) and
       rejects any unexpected top-level or nested key, and any value that
       is not itself a coded/bounded field -- so a raw prompt, response,
       dataset row, transcript, tool payload, or free-form note stuffed
       under a benign-looking key is rejected on structure alone, even if
       it happens to contain no secret-shaped substring.
    2. :func:`foundry_opt.security.reject_secret_content` (the existing,
       unmodified redaction validator) is still run over the full document
       afterwards, so a value that *does* pass the coded-field shape check
       but still contains a recognizable secret marker is caught too.

    ``write_redacted_evidence`` already enforces both at write time;
    re-checking here means a campaign can never publish evidence containing
    raw prompts, responses, dataset rows, or other unredacted content even
    if the on-disk file was altered after it was written.
    """
    try:
        document = json.loads(evidence_bytes)
    except json.JSONDecodeError as error:
        raise CampaignEvidenceRedactionError(
            "campaign evidence is not valid JSON"
        ) from error
    try:
        _validate_evidence_schema(
            document,
            report=report,
            candidates_by_id=candidates_by_id,
            candidate_ids=candidate_ids,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise CampaignEvidenceRedactionError(
            "campaign evidence schema is invalid or contains an "
            "unexpected field"
        ) from error
    try:
        reject_secret_content(document)
    except ValueError as error:
        raise CampaignEvidenceRedactionError(
            "campaign evidence failed redaction verification"
        ) from error


# ---------------------------------------------------------------------------
# Strict evidence schema validation
#
# This independently re-implements (rather than imports -- the module is
# restricted from modifying or reaching into existing GitHub production
# adapters) the same whitelist-only validation family used by
# ``foundry_opt.github_workflow.publication``'s own
# ``_validate_evidence_document``/``_verify_redacted_evidence``, extended
# with the ``goal_sha256``/``spec_sha256``/``assets`` fields that the real
# ``write_redacted_evidence`` writer always emits (a wider schema than the
# narrow one ``publication.py`` currently accepts). Those three fields are
# treated as optional-when-present here -- validated for shape and cross-
# checked against the campaign identity if they appear, but not mandatory
# -- so this validator accepts both the narrow document ``publish_campaign``
# itself is able to round-trip today and the wider document real production
# evidence actually contains.
# ---------------------------------------------------------------------------


def _strict_object(
    value: object,
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    keys = set(value)
    allowed = required | (optional or set())
    if not required <= keys:
        raise ValueError(f"missing required field(s): {sorted(required - keys)}")
    if not keys <= allowed:
        raise ValueError(f"unexpected field(s): {sorted(keys - allowed)}")
    return value


def _coded_string(value: object) -> str:
    # Mirrors the bounded, whitespace-free, control-character-free string
    # constraint every identity/status/reason-code field in the real
    # writer schema satisfies. Genuine natural-language text (a prompt, a
    # response, a transcript, a free-form note) almost never satisfies
    # this -- it contains spaces -- so this constraint is itself one of
    # the two structural defenses (together with whitelist-only keys)
    # against raw text being smuggled in under a benign-looking key.
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 2048
        or any(character.isspace() for character in value)
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("expected a bounded, coded string")
    return value


def _nullable_coded_string(value: object) -> None:
    if value is not None:
        _coded_string(value)


def _enum(value: object, allowed: frozenset[str]) -> str:
    coded = _coded_string(value)
    if coded not in allowed:
        raise ValueError(f"expected one of {sorted(allowed)}")
    return coded


def _nullable_enum(value: object, allowed: frozenset[str]) -> None:
    if value is not None:
        _enum(value, allowed)


def _nonnegative_integer(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("expected a non-negative integer")


def _nonnegative_number(value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or value < 0
    ):
        raise ValueError("expected a non-negative number")


def _nullable_number(value: object) -> None:
    if value is not None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
        ):
            raise ValueError("expected a number")


def _boolean(value: object) -> None:
    if not isinstance(value, bool):
        raise ValueError("expected a boolean")


def _sha256(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("expected a SHA-256 digest")
    return value


_EVALUATION_STATUSES = frozenset(
    {"queued", "running", "completed", "partial", "failed", "cancelled"}
)
_OUTCOMES = frozenset({"pass", "fail", "undefined"})
_DATASET_SPLITS = frozenset({"development", "validation"})
_ATTEMPT_ERROR_CODES = frozenset({"provider_error"})
_CASE_ERROR_CODES = frozenset({"case_error"})
_ASSET_KINDS = frozenset({"dataset", "evaluator"})
_ASSET_ROLES = frozenset({"development", "validation"})
_APPROVAL_GATES = frozenset({"policy", "human"})


def _validate_identity(value: object, keys: set[str]) -> None:
    identity = _strict_object(value, required=keys)
    for item in identity.values():
        _coded_string(item)


def _validate_usage(value: object) -> None:
    usage = _strict_object(
        value,
        required={"input_tokens", "output_tokens", "cached_tokens"},
    )
    for item in usage.values():
        _nonnegative_integer(item)


def _validate_attempt(value: object) -> None:
    attempt = _strict_object(
        value,
        required={
            "evaluation_id",
            "run_id",
            "status",
            "started_at",
            "completed_at",
            "error_code",
        },
    )
    _coded_string(attempt["evaluation_id"])
    _coded_string(attempt["run_id"])
    _enum(attempt["status"], _EVALUATION_STATUSES)
    _nullable_coded_string(attempt["started_at"])
    _nullable_coded_string(attempt["completed_at"])
    _nullable_enum(attempt["error_code"], _ATTEMPT_ERROR_CODES)


def _validate_metric(value: object) -> None:
    metric = _strict_object(
        value,
        required={
            "median",
            "minimum",
            "maximum",
            "spread",
            "outcome",
            "sample_count",
        },
    )
    for key in ("median", "minimum", "maximum", "spread"):
        _nullable_number(metric[key])
    _enum(metric["outcome"], _OUTCOMES)
    _nonnegative_integer(metric["sample_count"])


def _validate_score(value: object) -> None:
    score = _strict_object(
        value,
        required={
            "metric",
            "raw_score",
            "raw_score_code",
            "normalized_score",
            "outcome",
            "reason_code",
        },
    )
    _coded_string(score["metric"])
    raw_score = score["raw_score"]
    if raw_score is not None and not isinstance(raw_score, (str, int, float)):
        raise ValueError("raw_score must be a coded string, number, or null")
    if isinstance(raw_score, str):
        _coded_string(raw_score)
    _nullable_enum(score["raw_score_code"], _OUTCOMES)
    _nullable_number(score["normalized_score"])
    _enum(score["outcome"], _OUTCOMES)
    _coded_string(score["reason_code"])


def _validate_trajectory(value: object) -> None:
    trajectory = _strict_object(
        value,
        required={"trajectory_id", "turn_count", "tool_calls"},
    )
    _coded_string(trajectory["trajectory_id"])
    _nonnegative_integer(trajectory["turn_count"])
    tool_calls = trajectory["tool_calls"]
    if not isinstance(tool_calls, list):
        raise ValueError("tool_calls must be a list")
    for item in tool_calls:
        tool_call = _strict_object(
            item,
            required={"call_id", "status_code", "duration_ms"},
        )
        _coded_string(tool_call["call_id"])
        _coded_string(tool_call["status_code"])
        _nonnegative_number(tool_call["duration_ms"])


def _validate_case(value: object) -> None:
    case = _strict_object(
        value,
        required={
            "case_id",
            "case_hash",
            "response_ids",
            "duration_ms",
            "reason_code",
            "error_code",
            "scores",
            "usage",
            "trajectory",
        },
    )
    _coded_string(case["case_id"])
    _coded_string(case["case_hash"])
    _coded_string(case["reason_code"])
    _nullable_enum(case["error_code"], _CASE_ERROR_CODES)
    _nonnegative_number(case["duration_ms"])
    response_ids = case["response_ids"]
    if not isinstance(response_ids, list):
        raise ValueError("response_ids must be a list")
    for response_id in response_ids:
        _coded_string(response_id)
    scores = case["scores"]
    if not isinstance(scores, list):
        raise ValueError("scores must be a list")
    for score in scores:
        _validate_score(score)
    _validate_usage(case["usage"])
    trajectory = case["trajectory"]
    if trajectory is not None:
        _validate_trajectory(trajectory)


def _validate_result(value: object, *, candidate: bool) -> dict[str, object]:
    required = {
        "subject_id",
        "agent",
        "dataset",
        "evaluator",
        "evaluation_id",
        "run_id",
        "attempts",
        "split",
        "portal_url",
        "complete",
        "repeat_count",
        "duration_ms",
        "usage",
        "error_count",
        "metrics",
        "cases",
    }
    if candidate:
        required = required | {"patch_hash"}
    result = _strict_object(value, required=required)
    _coded_string(result["subject_id"])
    _coded_string(result["evaluation_id"])
    _coded_string(result["run_id"])
    _enum(result["split"], _DATASET_SPLITS)
    _nullable_coded_string(result["portal_url"])
    _boolean(result["complete"])
    _nonnegative_integer(result["repeat_count"])
    _nonnegative_number(result["duration_ms"])
    _nonnegative_integer(result["error_count"])
    if candidate:
        _sha256(result["patch_hash"])
    _validate_identity(result["agent"], {"agent_id", "draft_id", "version"})
    _validate_identity(result["dataset"], {"dataset_id", "version"})
    _validate_identity(result["evaluator"], {"definition_id", "version"})
    attempts = result["attempts"]
    if not isinstance(attempts, list):
        raise ValueError("attempts must be a list")
    for attempt in attempts:
        _validate_attempt(attempt)
    _validate_usage(result["usage"])
    metrics = result["metrics"]
    if not isinstance(metrics, dict):
        raise ValueError("metrics must be an object")
    for name, metric in metrics.items():
        _coded_string(name)
        _validate_metric(metric)
    cases = result["cases"]
    if not isinstance(cases, list):
        raise ValueError("cases must be a list")
    for case in cases:
        _validate_case(case)
    return result


def _validate_pareto(value: object) -> dict[str, object]:
    pareto = _strict_object(
        value,
        required={"frontier_ids", "eligible_ids", "decisions"},
    )
    for key in ("frontier_ids", "eligible_ids"):
        identifiers = pareto[key]
        if not isinstance(identifiers, list):
            raise ValueError(f"{key} must be a list")
        for identifier in identifiers:
            _coded_string(identifier)
    decisions = pareto["decisions"]
    if not isinstance(decisions, list):
        raise ValueError("decisions must be a list")
    for item in decisions:
        decision = _strict_object(
            item,
            required={"subject_id", "eligible", "reason_code"},
        )
        _coded_string(decision["subject_id"])
        _boolean(decision["eligible"])
        _coded_string(decision["reason_code"])
    return pareto


def _validate_telemetry(value: object) -> None:
    telemetry = _strict_object(
        value,
        required={
            "response_id",
            "request_count",
            "dependency_count",
            "exception_count",
            "duration_ms",
            "success_rate",
        },
    )
    _coded_string(telemetry["response_id"])
    for key in ("request_count", "dependency_count", "exception_count"):
        _nonnegative_integer(telemetry[key])
    _nonnegative_number(telemetry["duration_ms"])
    _nullable_number(telemetry["success_rate"])


def _validate_asset(value: object) -> str:
    asset = _strict_object(
        value,
        required={"asset_id", "kind", "source"},
        optional={
            "role",
            "name",
            "version",
            "remote_id",
            "content_sha256",
            "approval_gate",
            "metrics",
        },
    )
    asset_id = _coded_string(asset["asset_id"])
    _enum(asset["kind"], _ASSET_KINDS)
    _coded_string(asset["source"])
    if "role" in asset:
        _nullable_enum(asset["role"], _ASSET_ROLES)
    if "name" in asset:
        _nullable_coded_string(asset["name"])
    if "version" in asset:
        _nullable_coded_string(asset["version"])
    if "remote_id" in asset:
        _nullable_coded_string(asset["remote_id"])
    if "content_sha256" in asset:
        content_sha256 = asset["content_sha256"]
        if content_sha256 is not None:
            _sha256(content_sha256)
    if "approval_gate" in asset:
        _enum(asset["approval_gate"], _APPROVAL_GATES)
    metrics = asset.get("metrics", [])
    if not isinstance(metrics, list):
        raise ValueError("asset metrics must be a list")
    for metric in metrics:
        _coded_string(metric)
    if asset["kind"] == "dataset" and metrics:
        raise ValueError("dataset assets must not define metrics")
    if asset["kind"] == "evaluator" and not metrics:
        raise ValueError("evaluator assets require metrics")
    return asset_id


def _validate_evidence_schema(
    document: object,
    *,
    report: CampaignReport,
    candidates_by_id: Mapping[str, CandidateArtifact],
    candidate_ids: Sequence[str],
) -> None:
    """Structurally validate ``document`` against the exact evidence schema
    ``write_redacted_evidence`` emits (with ``goal_sha256``/``spec_sha256``/
    ``assets`` optional-when-present, see the module docstring above this
    section), then cross-check every identity the campaign manifest relies
    on: the document's own ``campaign_id`` (and, when present, its
    ``goal_sha256``/``spec_sha256``/``assets``) against the campaign
    report, and -- for every candidate that (possibly jointly) references
    this evidence file -- that the document actually names that candidate
    as an eligible Pareto subject with a matching patch hash.
    """
    evidence = _strict_object(
        document,
        required={
            "schema_version",
            "campaign_id",
            "source_hash",
            "baseline",
            "candidates",
            "pareto",
        },
        optional={
            "goal_sha256",
            "spec_sha256",
            "assets",
            "generated_at",
            "telemetry",
        },
    )
    if evidence["schema_version"] != 1:
        raise ValueError("unsupported evidence schema_version")
    _coded_string(evidence["campaign_id"])
    _coded_string(evidence["source_hash"])
    if evidence["campaign_id"] != report.campaign_id:
        raise ValueError("evidence campaign_id does not match the campaign")
    if "generated_at" in evidence:
        _nullable_coded_string(evidence["generated_at"])

    if "goal_sha256" in evidence:
        goal_sha256 = _sha256(evidence["goal_sha256"])
        if goal_sha256 != report.goal_sha256:
            raise ValueError(
                "evidence goal_sha256 does not match the campaign goal"
            )
    if "spec_sha256" in evidence:
        spec_sha256 = _sha256(evidence["spec_sha256"])
        if spec_sha256 != report.spec_sha256:
            raise ValueError(
                "evidence spec_sha256 does not match the campaign spec"
            )
    if "assets" in evidence:
        assets = evidence["assets"]
        if not isinstance(assets, list):
            raise ValueError("assets must be a list")
        asset_ids = tuple(_validate_asset(asset) for asset in assets)
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("asset IDs must be unique")
        expected_assets = sorted(
            (_asset_identity_document(asset) for asset in report.assets),
            key=lambda item: str(item["asset_id"]),
        )
        if sorted(
            assets,
            key=lambda item: (
                str(item.get("asset_id"))
                if isinstance(item, dict)
                else ""
            ),
        ) != expected_assets:
            raise ValueError(
                "evidence assets do not match the campaign assets"
            )

    _validate_result(evidence["baseline"], candidate=False)
    candidates = evidence["candidates"]
    if not isinstance(candidates, list):
        raise ValueError("candidates must be a list")
    validated_candidates = [
        _validate_result(item, candidate=True) for item in candidates
    ]
    pareto = _validate_pareto(evidence["pareto"])
    if "telemetry" in evidence:
        telemetry = evidence["telemetry"]
        if not isinstance(telemetry, list):
            raise ValueError("telemetry must be a list")
        for item in telemetry:
            _validate_telemetry(item)

    eligible_ids = pareto["eligible_ids"]
    for candidate_id in candidate_ids:
        candidate = candidates_by_id[candidate_id]
        if candidate_id not in eligible_ids:
            raise ValueError(
                f"evidence pareto eligible_ids is missing {candidate_id!r}"
            )
        if not any(
            item.get("subject_id") == candidate_id
            and item.get("patch_hash") == candidate.patch.sha256
            for item in validated_candidates
        ):
            raise ValueError(
                "evidence candidates do not include a matching subject "
                f"for {candidate_id!r}"
            )


def _asset_identity_document(asset: object) -> dict[str, object]:
    return {
        "approval_gate": getattr(asset, "approval_gate"),
        "asset_id": getattr(asset, "asset_id"),
        "content_sha256": getattr(asset, "content_sha256"),
        "kind": getattr(asset, "kind"),
        "metrics": list(getattr(asset, "metrics", ())),
        "name": getattr(asset, "name"),
        "remote_id": getattr(asset, "remote_id"),
        "role": getattr(asset, "role"),
        "source": getattr(asset, "source"),
        "version": getattr(asset, "version"),
    }


def _manifest_path(
    evidence_by_path: Mapping[Path, EvidenceManifest],
) -> Path:
    # Co-locate the manifest with the evidence files rather than assuming a
    # fixed campaign directory, since the evidence root is configurable.
    directory = sorted(evidence_by_path, key=lambda path: path.as_posix())[0]
    return directory.parent / _MANIFEST_FILENAME


def _lineage_sha256(
    report: CampaignReport,
    evidence_sha256: Mapping[str, str],
) -> str:
    """Anchor the goal, spec, asset, and per-candidate patch/evidence
    lineage into a single reproducible digest, used as the manifest's
    redaction-provenance ``source_sha256``.
    """
    candidates_by_id = {
        candidate.candidate_id: candidate for candidate in report.candidates
    }
    payload = {
        "base_commit": report.base_commit,
        "campaign_id": report.campaign_id,
        # The parent optimization issue identity is derivable from the
        # existing "issue-<number>" campaign_id convention, so no extension
        # of CampaignPublicationRequest/CampaignReport is required to bind
        # the lineage to it.
        "parent_optimization_issue": _parent_optimization_issue(
            report.campaign_id
        ),
        "goal_sha256": report.goal_sha256,
        "spec_sha256": report.spec_sha256,
        "assets": [
            {
                "asset_id": asset.asset_id,
                "kind": asset.kind,
                "content_sha256": asset.content_sha256,
            }
            for asset in sorted(report.assets, key=lambda item: item.asset_id)
        ],
        "candidates": [
            {
                "candidate_id": candidate_id,
                "patch_sha256": candidates_by_id[candidate_id].patch.sha256,
                "evidence_sha256": evidence_sha256[candidate_id],
            }
            for candidate_id in sorted(report.pareto_candidate_ids)
        ],
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parent_optimization_issue(campaign_id: str) -> int | None:
    match = _ISSUE_CAMPAIGN_ID.fullmatch(campaign_id)
    return int(match.group(1)) if match is not None else None


def _build_manifest_bytes(
    report: CampaignReport,
    evidence_sha256: Mapping[str, str],
) -> bytes:
    # The manifest referenced from ``CampaignPublicationRequest.manifests``
    # is verified with a strict schema by ``publish_campaign`` itself
    # (exactly ``schema_version``/``redaction_provenance``, nothing else),
    # so the goal/spec/asset/lineage hashes are bound into the single
    # ``source_sha256`` anchor computed by ``_lineage_sha256`` rather than
    # spelled out as additional top-level fields.
    source_sha256 = _lineage_sha256(report, evidence_sha256)
    document = {
        "schema_version": 1,
        "redaction_provenance": {
            "generator": _MANIFEST_GENERATOR,
            "schema_version": 1,
            "source_sha256": source_sha256,
        },
    }
    try:
        reject_secret_content(document)
    except ValueError as error:
        raise CampaignEvidenceRedactionError(
            "campaign manifest failed redaction verification"
        ) from error
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
