from __future__ import annotations

from dataclasses import dataclass, field, replace
import errno
import hashlib
import json
from pathlib import Path
import re
import subprocess

import pytest

import foundry_opt.adapters.optimization_publication as optimization_publication
from foundry_opt.adapters.commands import SubprocessCommandRunner
from foundry_opt.adapters.optimization_publication import (
    CampaignArtifactMismatchError,
    CampaignEvidenceRedactionError,
    CampaignPublisher,
    PartialCampaignPublicationError,
    StaleCampaignBaseError,
    UnsafeCampaignArtifactPathError,
)
from foundry_opt.campaign.models import CampaignReport, CandidateArtifact, PatchArtifact
from foundry_opt.campaign.state import FinalizedPublication
from foundry_opt.evidence.models import EvaluationAssetReference, EvidenceManifest
from foundry_opt.github_workflow.models import (
    CommitBlob,
    CommitInspection,
    GitHubPermissionReport,
    IssueReference,
    PullRequestReference,
    RepositoryState,
)
from foundry_opt.optimization.runner import CampaignPublicationInputs


# ---------------------------------------------------------------------------
# Repository fixtures
# ---------------------------------------------------------------------------


def _run(runner: SubprocessCommandRunner, repository: Path, *arguments: str) -> str:
    return runner.run(("git", *arguments), cwd=repository).stdout


def _init_repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    runner = SubprocessCommandRunner()
    _run(runner, repository, "init", "--quiet")
    _run(runner, repository, "config", "user.name", "Foundry Test")
    _run(runner, repository, "config", "user.email", "foundry-test@example.invalid")
    (repository / ".gitignore").write_text(".foundry-optimizer/\n", encoding="utf-8")
    (repository / "agent.py").write_text("VALUE = 1\n", encoding="utf-8")
    _run(runner, repository, "add", ".")
    _run(runner, repository, "commit", "--quiet", "-m", "base")
    base = _run(runner, repository, "rev-parse", "HEAD").strip()
    return repository, base


_GOAL_SHA256 = "1" * 64
_SPEC_SHA256 = "2" * 64
_TEXT_PATCH_BYTES = (
    b"diff --git a/agent.py b/agent.py\n"
    b"--- a/agent.py\n+++ b/agent.py\n"
    b"@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n"
)
# Deliberately invalid UTF-8 (a lone continuation byte 0x80 and 0xff) so any
# accidental text decode/re-encode round trip in the commit-assembly path
# would corrupt it and fail the exact byte comparisons below.
_BINARY_PATCH_BYTES = bytes(range(256)) + b"\x00\xff\x80binary\xfe"


def _asset() -> tuple[EvaluationAssetReference, ...]:
    return (
        EvaluationAssetReference(
            asset_id="dataset-dev",
            kind="dataset",
            source="repository",
            role="development",
        ),
    )


def _evaluation_result(subject_id: str, *, patch_hash: str | None = None) -> dict:
    result: dict = {
        "subject_id": subject_id,
        "agent": {
            "agent_id": "agent-1",
            "draft_id": f"draft-{subject_id}",
            "version": "version-1",
        },
        "dataset": {"dataset_id": "dataset-1", "version": "version-1"},
        "evaluator": {"definition_id": "evaluator-1", "version": "version-1"},
        "evaluation_id": f"evaluation-{subject_id}",
        "run_id": f"run-{subject_id}",
        "attempts": [],
        "split": "development",
        "portal_url": None,
        "complete": True,
        "repeat_count": 0,
        "duration_ms": 10.0,
        "usage": {"input_tokens": 1, "output_tokens": 1, "cached_tokens": 0},
        "error_count": 0,
        "metrics": {},
        "cases": [],
    }
    if patch_hash is not None:
        result["patch_hash"] = patch_hash
    return result


def _evidence_document(
    campaign_id: str,
    candidate_ids: tuple[str, ...],
    patch_hash_by_id: dict[str, str],
) -> dict:
    return {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "source_hash": "source-1",
        "goal_sha256": _GOAL_SHA256,
        "spec_sha256": _SPEC_SHA256,
        "assets": [
            {
                "asset_id": asset.asset_id,
                "kind": asset.kind,
                "source": asset.source,
                "role": asset.role,
                "name": asset.name,
                "version": asset.version,
                "remote_id": asset.remote_id,
                "content_sha256": asset.content_sha256,
                "approval_gate": asset.approval_gate,
                "metrics": list(asset.metrics),
            }
            for asset in _asset()
        ],
        "baseline": _evaluation_result("baseline"),
        "candidates": [
            _evaluation_result(
                candidate_id,
                patch_hash=patch_hash_by_id[candidate_id],
            )
            for candidate_id in candidate_ids
        ],
        "pareto": {
            "frontier_ids": list(candidate_ids),
            "eligible_ids": list(candidate_ids),
            "decisions": [
                {
                    "subject_id": candidate_id,
                    "eligible": True,
                    "reason_code": "eligible",
                }
                for candidate_id in candidate_ids
            ],
        },
    }


def _evidence_bytes(
    campaign_id: str,
    candidate_ids: tuple[str, ...],
    patch_hash_by_id: dict[str, str],
) -> bytes:
    document = _evidence_document(campaign_id, candidate_ids, patch_hash_by_id)
    return json.dumps(document, sort_keys=True).encode("utf-8")


@dataclass
class Campaign:
    root: Path
    base_commit: str
    campaign_id: str
    report: CampaignReport
    inputs: CampaignPublicationInputs
    evidence_path: Path
    patch_path_by_id: dict[str, Path]


def _build_campaign(
    tmp_path: Path,
    *,
    campaign_id: str = "issue-7",
    candidate_ids: tuple[str, ...] = ("candidate-1",),
    patch_bytes_by_id: dict[str, bytes] | None = None,
) -> Campaign:
    root, base = _init_repository(tmp_path)
    patch_bytes_by_id = patch_bytes_by_id or {
        candidate_id: _TEXT_PATCH_BYTES for candidate_id in candidate_ids
    }
    campaign_dir = root / ".foundry-optimizer" / "campaigns" / campaign_id
    campaign_dir.mkdir(parents=True)

    patch_path_by_id: dict[str, Path] = {}
    patches: dict[str, PatchArtifact] = {}
    for candidate_id in candidate_ids:
        patch_bytes = patch_bytes_by_id[candidate_id]
        relative = Path(
            f".foundry-optimizer/campaigns/{campaign_id}/{candidate_id}.patch"
        )
        (root / relative).write_bytes(patch_bytes)
        patch_path_by_id[candidate_id] = relative
        patches[candidate_id] = PatchArtifact(
            candidate_id=candidate_id,
            path=relative,
            sha256=hashlib.sha256(patch_bytes).hexdigest(),
            base_commit=base,
            result_commit="c" * 40,
        )

    evidence_path = Path(
        f".foundry-optimizer/campaigns/{campaign_id}/development.json"
    )
    patch_hash_by_id = {
        candidate_id: patches[candidate_id].sha256 for candidate_id in candidate_ids
    }
    evidence_bytes = _evidence_bytes(campaign_id, candidate_ids, patch_hash_by_id)
    (root / evidence_path).write_bytes(evidence_bytes)

    candidates = tuple(
        CandidateArtifact(
            candidate_id=candidate_id,
            patch=patches[candidate_id],
            draft_id=f"draft-{candidate_id}",
            evidence_path=evidence_path,
            eligible=True,
            metrics={"quality": 0.9},
        )
        for candidate_id in candidate_ids
    )
    report = CampaignReport(
        campaign_id=campaign_id,
        target="support-agent",
        base_commit=base,
        baseline_draft_id="draft-baseline",
        candidates=candidates,
        pareto_candidate_ids=candidate_ids,
        goal_sha256=_GOAL_SHA256,
        spec_sha256=_SPEC_SHA256,
        assets=_asset(),
    )
    development_evidence = EvidenceManifest(
        path=evidence_path,
        sha256=hashlib.sha256(evidence_bytes).hexdigest(),
        byte_count=len(evidence_bytes),
        evaluation_ids=tuple(f"evaluation-{cid}" for cid in candidate_ids),
        run_ids=tuple(f"run-{cid}" for cid in candidate_ids),
        goal_sha256=_GOAL_SHA256,
        spec_sha256=_SPEC_SHA256,
    )
    inputs = CampaignPublicationInputs(
        repository_root=root,
        report=report,
        development_evidence=development_evidence,
        validation_evidence=None,
        reproduction_instructions=("Run the configured validation commands.",),
    )
    return Campaign(
        root=root,
        base_commit=base,
        campaign_id=campaign_id,
        report=report,
        inputs=inputs,
        evidence_path=evidence_path,
        patch_path_by_id=patch_path_by_id,
    )


# ---------------------------------------------------------------------------
# Fake GitHub-side gateway (real git for commit inspection, in-memory GitHub)
# ---------------------------------------------------------------------------


def _extract_marker(body: str) -> str | None:
    match = re.search(r"<!-- foundry-opt:candidate:[^\s]+ -->", body)
    return match.group(0) if match is not None else None


@dataclass
class FakeCampaignGateway:
    repository_root: Path
    default_commit: str
    permission_report: GitHubPermissionReport | None = None
    fail_issue_for_candidates: frozenset[str] = frozenset()
    campaign_pr: PullRequestReference | None = None
    issues_by_marker: dict[str, IssueReference] = field(default_factory=dict)
    created_issue_count: int = 0

    def verify_permissions(self, required):
        return self.permission_report or GitHubPermissionReport(granted=required)

    def repository_state(self, repository_root: Path) -> RepositoryState:
        return RepositoryState("octo-org/optimizer", "main", self.default_commit)

    def inspect_commit(
        self,
        repository_root: Path,
        *,
        base_commit: str,
        head_commit: str,
        artifact_paths: tuple[Path, ...],
    ) -> CommitInspection:
        try:
            merge_base = subprocess.run(
                ["git", "merge-base", base_commit, head_commit],
                cwd=repository_root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        except subprocess.CalledProcessError:
            merge_base = ""
        diff = subprocess.run(
            ["git", "diff", "--name-only", "-z", base_commit, head_commit],
            cwd=repository_root,
            capture_output=True,
            text=True,
            check=True,
        )
        changed_paths = tuple(
            Path(path) for path in diff.stdout.split("\0") if path
        )
        blobs = []
        for path in artifact_paths:
            result = subprocess.run(
                ["git", "show", f"{head_commit}:{path.as_posix()}"],
                cwd=repository_root,
                capture_output=True,
                check=True,
            )
            blobs.append(CommitBlob(path, result.stdout))
        return CommitInspection(
            base_commit=base_commit,
            head_commit=head_commit,
            base_is_ancestor=merge_base == base_commit,
            changed_paths=changed_paths,
            blobs=tuple(blobs),
        )

    def artifact_url(self, repository: str, commit: str, path: Path) -> str:
        return f"https://github.com/{repository}/blob/{commit}/{path.as_posix()}"

    def find_campaign_pull_request(self, repository_root: Path, head_branch: str):
        return self.campaign_pr

    def find_candidate_pull_request(self, repository_root: Path, head_branch: str):
        return None

    def create_campaign_pull_request(
        self,
        repository_root: Path,
        *,
        base_branch: str,
        head_branch: str,
        head_commit: str,
        title: str,
        body: str,
    ) -> PullRequestReference:
        self.campaign_pr = PullRequestReference(
            number=42,
            url="https://github.com/octo-org/optimizer/pull/42",
            head_branch=head_branch,
            head_commit=head_commit,
            draft=True,
            body=body,
            base_branch=base_branch,
            state="OPEN",
        )
        return self.campaign_pr

    def find_candidate_issue(self, repository_root: Path, marker: str):
        return self.issues_by_marker.get(marker)

    def create_issue(
        self,
        repository_root: Path,
        *,
        title: str,
        body: str,
    ) -> IssueReference:
        marker = _extract_marker(body)
        for candidate_id in self.fail_issue_for_candidates:
            if marker is not None and marker.endswith(
                f":{candidate_id} -->"
            ):
                raise RuntimeError(f"issue creation failed for {candidate_id}")
        self.created_issue_count += 1
        number = 100 + self.created_issue_count
        issue = IssueReference(
            number=number,
            url=f"https://github.com/octo-org/optimizer/issues/{number}",
            title=title,
            body=body,
            state="OPEN",
            labels=(),
        )
        assert marker is not None
        self.issues_by_marker[marker] = issue
        return issue

    def _replace_issue(self, issue_number: int, **changes) -> None:
        for marker, issue in self.issues_by_marker.items():
            if issue.number == issue_number:
                self.issues_by_marker[marker] = replace(issue, **changes)
                return

    def add_labels(
        self, repository_root: Path, issue_number: int, labels: tuple[str, ...]
    ) -> None:
        for marker, issue in self.issues_by_marker.items():
            if issue.number == issue_number:
                self._replace_issue(
                    issue_number, labels=tuple(set(issue.labels) | set(labels))
                )
                return

    def remove_labels(
        self, repository_root: Path, issue_number: int, labels: tuple[str, ...]
    ) -> None:
        for marker, issue in self.issues_by_marker.items():
            if issue.number == issue_number:
                self._replace_issue(
                    issue_number, labels=tuple(set(issue.labels) - set(labels))
                )
                return

    def reopen_issue(self, repository_root: Path, issue_number: int) -> None:
        self._replace_issue(issue_number, state="OPEN")

    def is_sub_issue(
        self, repository_root: Path, parent_number: int, child_number: int
    ) -> bool:
        return False

    def link_sub_issue(
        self, repository_root: Path, parent_number: int, child_number: int
    ) -> None:
        return None

    def add_dependency(
        self, repository_root: Path, issue_number: int, blocker_number: int
    ) -> None:
        return None

    def update_issue_body(
        self, repository_root: Path, issue_number: int, body: str
    ) -> None:
        self._replace_issue(issue_number, body=body)

    def update_pull_request_body(
        self, repository_root: Path, pull_request_number: int, body: str
    ) -> None:
        if self.campaign_pr is not None and self.campaign_pr.number == pull_request_number:
            self.campaign_pr = replace(self.campaign_pr, body=body)

    def close_pull_request(
        self, repository_root: Path, pull_request_number: int, comment: str
    ) -> None:
        if self.campaign_pr is not None and self.campaign_pr.number == pull_request_number:
            self.campaign_pr = replace(self.campaign_pr, state="CLOSED")


def _publisher(gateway: FakeCampaignGateway) -> CampaignPublisher:
    return CampaignPublisher(
        SubprocessCommandRunner(),
        gateway_factory=lambda root: gateway,
    )


def _git_show(root: Path, commit: str, path: Path) -> bytes:
    return subprocess.run(
        ["git", "show", f"{commit}:{path.as_posix()}"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout


def _tree_paths(root: Path, commit: str) -> set[str]:
    output = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", commit],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {line for line in output.splitlines() if line}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_publish_commits_exact_artifacts_and_hashes(tmp_path: Path) -> None:
    campaign = _build_campaign(tmp_path)
    gateway = FakeCampaignGateway(campaign.root, campaign.base_commit)

    result = _publisher(gateway).publish(campaign.inputs)

    assert isinstance(result, FinalizedPublication)
    assert result.campaign_pull_request_number == 42
    assert result.candidate_issue_numbers == {"candidate-1": 101}

    head_commit = gateway.campaign_pr.head_commit
    patch_path = campaign.patch_path_by_id["candidate-1"]
    assert _git_show(campaign.root, head_commit, patch_path) == _TEXT_PATCH_BYTES
    evidence_bytes = (campaign.root / campaign.evidence_path).read_bytes()
    assert (
        _git_show(campaign.root, head_commit, campaign.evidence_path)
        == evidence_bytes
    )
    manifest_path = campaign.evidence_path.parent / "manifest.json"
    manifest = json.loads(_git_show(campaign.root, head_commit, manifest_path))
    assert set(manifest) == {"schema_version", "redaction_provenance"}
    assert set(manifest["redaction_provenance"]) == {
        "generator",
        "schema_version",
        "source_sha256",
    }
    assert re.fullmatch(
        r"[0-9a-f]{64}", manifest["redaction_provenance"]["source_sha256"]
    )
    base_paths = _tree_paths(campaign.root, campaign.base_commit)
    head_paths = _tree_paths(campaign.root, head_commit)
    assert head_paths - base_paths == {
        patch_path.as_posix(),
        campaign.evidence_path.as_posix(),
        manifest_path.as_posix(),
    }
    # Base commit is left untouched: no checkout, no staged index changes.
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=campaign.root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert status == ""
    current_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=campaign.root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert current_head == campaign.base_commit


def test_publish_commits_binary_patch_byte_for_byte(tmp_path: Path) -> None:
    campaign = _build_campaign(
        tmp_path,
        candidate_ids=("candidate-1", "candidate-2"),
        patch_bytes_by_id={
            "candidate-1": _TEXT_PATCH_BYTES,
            "candidate-2": _BINARY_PATCH_BYTES,
        },
    )
    gateway = FakeCampaignGateway(campaign.root, campaign.base_commit)

    result = _publisher(gateway).publish(campaign.inputs)

    head_commit = gateway.campaign_pr.head_commit
    binary_path = campaign.patch_path_by_id["candidate-2"]
    assert (
        _git_show(campaign.root, head_commit, binary_path) == _BINARY_PATCH_BYTES
    )
    assert result.candidate_issue_numbers.keys() == {"candidate-1", "candidate-2"}


def test_publish_reuses_existing_branch_pr_and_issues_idempotently(
    tmp_path: Path,
) -> None:
    campaign = _build_campaign(tmp_path)
    gateway = FakeCampaignGateway(campaign.root, campaign.base_commit)
    publisher = _publisher(gateway)

    first = publisher.publish(campaign.inputs)
    created_issue_count_after_first = gateway.created_issue_count
    first_head_commit = gateway.campaign_pr.head_commit

    second = publisher.publish(campaign.inputs)

    assert second == first
    assert gateway.created_issue_count == created_issue_count_after_first
    assert gateway.campaign_pr.number == 42
    # The commit assembly is fully deterministic: retrying reproduces the
    # exact same head commit, which is required for publish_campaign's
    # existing-PR reconciliation (_campaign_pr_matches) to succeed at all.
    assert gateway.campaign_pr.head_commit == first_head_commit


def test_publish_raises_stale_base_when_default_branch_moved(
    tmp_path: Path,
) -> None:
    campaign = _build_campaign(tmp_path)
    gateway = FakeCampaignGateway(campaign.root, "f" * 40)

    with pytest.raises(StaleCampaignBaseError):
        _publisher(gateway).publish(campaign.inputs)

    assert gateway.campaign_pr is None


def test_publish_raises_stale_base_when_base_commit_unreachable(
    tmp_path: Path,
) -> None:
    campaign = _build_campaign(tmp_path)
    bogus_base = "e" * 40
    stale_report = replace(campaign.report, base_commit=bogus_base)
    stale_patches = tuple(
        replace(candidate, patch=replace(candidate.patch, base_commit=bogus_base))
        for candidate in stale_report.candidates
    )
    stale_report = replace(stale_report, candidates=stale_patches)
    stale_inputs = replace(campaign.inputs, report=stale_report)
    gateway = FakeCampaignGateway(campaign.root, bogus_base)

    with pytest.raises(StaleCampaignBaseError):
        _publisher(gateway).publish(stale_inputs)


def test_publish_excludes_extra_stray_files_from_the_commit(
    tmp_path: Path,
) -> None:
    campaign = _build_campaign(tmp_path)
    stray = campaign.root / ".foundry-optimizer" / "campaigns" / campaign.campaign_id / "stray.txt"
    stray.write_text("not referenced by the campaign report\n", encoding="utf-8")
    gateway = FakeCampaignGateway(campaign.root, campaign.base_commit)

    _publisher(gateway).publish(campaign.inputs)

    head_commit = gateway.campaign_pr.head_commit
    tree_paths = _tree_paths(campaign.root, head_commit)
    assert "stray.txt" not in {Path(path).name for path in tree_paths}
    assert not any(path.endswith("stray.txt") for path in tree_paths)


def test_publish_rejects_symlinked_evidence_path(tmp_path: Path) -> None:
    campaign = _build_campaign(tmp_path)
    outside = tmp_path / "outside-secret.json"
    outside.write_text('{"leaked": true}\n', encoding="utf-8")
    evidence_file = campaign.root / campaign.evidence_path
    evidence_file.unlink()
    evidence_file.symlink_to(outside)
    gateway = FakeCampaignGateway(campaign.root, campaign.base_commit)

    with pytest.raises(UnsafeCampaignArtifactPathError):
        _publisher(gateway).publish(campaign.inputs)

    assert gateway.campaign_pr is None


def test_publish_rejects_symlinked_patch_path(tmp_path: Path) -> None:
    campaign = _build_campaign(tmp_path)
    outside = tmp_path / "outside-patch.patch"
    outside.write_bytes(_TEXT_PATCH_BYTES)
    patch_file = campaign.root / campaign.patch_path_by_id["candidate-1"]
    patch_file.unlink()
    patch_file.symlink_to(outside)
    gateway = FakeCampaignGateway(campaign.root, campaign.base_commit)

    with pytest.raises(UnsafeCampaignArtifactPathError):
        _publisher(gateway).publish(campaign.inputs)

    assert gateway.campaign_pr is None


def test_publish_rejects_evidence_redaction_failure(tmp_path: Path) -> None:
    campaign = _build_campaign(tmp_path)
    document = json.loads((campaign.root / campaign.evidence_path).read_bytes())
    # Inject a secret-shaped field into an otherwise well-formed, hash
    # consistent evidence document: this simulates a tampered/compromised
    # file that still matches its recorded byte-for-byte hash, so the
    # redaction validator is the only remaining line of defense.
    document["candidates"][0]["metrics"]["api_key"] = {
        "median": None,
        "minimum": None,
        "maximum": None,
        "spread": None,
        "outcome": "sk-live-01234567890123456789",
        "sample_count": 0,
    }
    corrupted = json.dumps(document, sort_keys=True).encode("utf-8")
    (campaign.root / campaign.evidence_path).write_bytes(corrupted)
    corrupted_manifest = EvidenceManifest(
        path=campaign.inputs.development_evidence.path,
        sha256=hashlib.sha256(corrupted).hexdigest(),
        byte_count=len(corrupted),
        evaluation_ids=campaign.inputs.development_evidence.evaluation_ids,
        run_ids=campaign.inputs.development_evidence.run_ids,
        goal_sha256=_GOAL_SHA256,
        spec_sha256=_SPEC_SHA256,
    )
    corrupted_inputs = replace(
        campaign.inputs,
        development_evidence=corrupted_manifest,
    )
    gateway = FakeCampaignGateway(campaign.root, campaign.base_commit)

    with pytest.raises(CampaignEvidenceRedactionError):
        _publisher(gateway).publish(corrupted_inputs)

    assert gateway.campaign_pr is None


def test_publish_raises_partial_error_and_preserves_resumable_state(
    tmp_path: Path,
) -> None:
    campaign = _build_campaign(
        tmp_path,
        candidate_ids=("candidate-1", "candidate-2"),
        patch_bytes_by_id={
            "candidate-1": _TEXT_PATCH_BYTES,
            "candidate-2": _BINARY_PATCH_BYTES,
        },
    )
    gateway = FakeCampaignGateway(
        campaign.root,
        campaign.base_commit,
        fail_issue_for_candidates=frozenset({"candidate-2"}),
    )
    publisher = _publisher(gateway)

    with pytest.raises(PartialCampaignPublicationError) as excinfo:
        publisher.publish(campaign.inputs)

    assert len(excinfo.value.failures) == 1
    # Partial state is preserved: the campaign PR and the one candidate
    # issue that *did* succeed remain on the gateway (nothing was rolled
    # back), so the campaign is resumable rather than lost.
    assert gateway.campaign_pr is not None
    assert gateway.created_issue_count == 1

    # Retrying after the transient failure is fixed completes the campaign
    # by only creating the missing issue, reusing everything else.
    gateway.fail_issue_for_candidates = frozenset()
    result = publisher.publish(campaign.inputs)

    assert result.candidate_issue_numbers.keys() == {"candidate-1", "candidate-2"}
    assert gateway.created_issue_count == 2


def test_publish_rejects_artifact_hash_mismatch(tmp_path: Path) -> None:
    campaign = _build_campaign(tmp_path)
    patch_path = campaign.patch_path_by_id["candidate-1"]
    (campaign.root / patch_path).write_bytes(_TEXT_PATCH_BYTES + b"\ntampered\n")
    gateway = FakeCampaignGateway(campaign.root, campaign.base_commit)

    with pytest.raises(CampaignArtifactMismatchError):
        _publisher(gateway).publish(campaign.inputs)

    assert gateway.campaign_pr is None


def _write_corrupted_evidence(campaign: Campaign, document: dict) -> CampaignPublicationInputs:
    corrupted = json.dumps(document, sort_keys=True).encode("utf-8")
    (campaign.root / campaign.evidence_path).write_bytes(corrupted)
    corrupted_manifest = EvidenceManifest(
        path=campaign.inputs.development_evidence.path,
        sha256=hashlib.sha256(corrupted).hexdigest(),
        byte_count=len(corrupted),
        evaluation_ids=campaign.inputs.development_evidence.evaluation_ids,
        run_ids=campaign.inputs.development_evidence.run_ids,
        goal_sha256=_GOAL_SHA256,
        spec_sha256=_SPEC_SHA256,
    )
    return replace(campaign.inputs, development_evidence=corrupted_manifest)


def test_publish_rejects_evidence_with_unexpected_top_level_key(
    tmp_path: Path,
) -> None:
    campaign = _build_campaign(tmp_path)
    document = json.loads((campaign.root / campaign.evidence_path).read_bytes())
    # No secret marker anywhere in this value -- ``notes`` simply is not
    # one of the whitelisted top-level fields, so this must be rejected on
    # schema/structure alone, proving the validator is not merely scanning
    # for secret-shaped substrings.
    document["notes"] = (
        "the customer asked the assistant to summarize their account "
        "history and the assistant complied with a detailed narrative"
    )
    corrupted_inputs = _write_corrupted_evidence(campaign, document)
    gateway = FakeCampaignGateway(campaign.root, campaign.base_commit)

    with pytest.raises(CampaignEvidenceRedactionError):
        _publisher(gateway).publish(corrupted_inputs)

    assert gateway.campaign_pr is None


def test_publish_rejects_evidence_with_raw_text_under_benign_nested_key(
    tmp_path: Path,
) -> None:
    campaign = _build_campaign(tmp_path)
    document = json.loads((campaign.root / campaign.evidence_path).read_bytes())
    # Same idea, but nested inside an otherwise-valid candidate result --
    # a "reason" field is not part of the whitelisted result schema, and
    # even if it were, its value is free natural-language text (contains
    # whitespace), which the coded-string constraint alone would reject.
    document["candidates"][0]["reason"] = (
        "evaluator determined the candidate response referenced the "
        "user's home address in the transcript"
    )
    corrupted_inputs = _write_corrupted_evidence(campaign, document)
    gateway = FakeCampaignGateway(campaign.root, campaign.base_commit)

    with pytest.raises(CampaignEvidenceRedactionError):
        _publisher(gateway).publish(corrupted_inputs)

    assert gateway.campaign_pr is None


def test_publish_rejects_evidence_with_non_coded_string_in_whitelisted_field(
    tmp_path: Path,
) -> None:
    campaign = _build_campaign(tmp_path)
    document = json.loads((campaign.root / campaign.evidence_path).read_bytes())
    # ``source_hash`` *is* a whitelisted top-level field, but its value
    # here is raw free text rather than a bounded, whitespace-free coded
    # string -- this must still be rejected, showing that whitelisting a
    # key alone is not enough: the value shape is checked too.
    document["source_hash"] = "please forward this conversation to support"
    corrupted_inputs = _write_corrupted_evidence(campaign, document)
    gateway = FakeCampaignGateway(campaign.root, campaign.base_commit)

    with pytest.raises(CampaignEvidenceRedactionError):
        _publisher(gateway).publish(corrupted_inputs)

    assert gateway.campaign_pr is None


def test_publish_rejects_toctou_symlink_race_on_evidence_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate the file being swapped for a symlink in the instant
    between the pre-open containment checks and the actual open call, by
    monkeypatching the isolated ``_open_no_follow`` seam to raise
    ``OSError(errno.ELOOP, ...)`` exactly as a real POSIX ``O_NOFOLLOW``
    open would when racing a symlink swap. This exercises the error-
    mapping contract deterministically regardless of host OS.
    """
    campaign = _build_campaign(tmp_path)
    gateway = FakeCampaignGateway(campaign.root, campaign.base_commit)
    real_open_no_follow = optimization_publication._open_no_follow

    def racing_open(path: Path):
        if path.name == campaign.evidence_path.name:
            raise OSError(errno.ELOOP, "Too many levels of symbolic links")
        return real_open_no_follow(path)

    monkeypatch.setattr(optimization_publication, "_open_no_follow", racing_open)

    with pytest.raises(UnsafeCampaignArtifactPathError):
        _publisher(gateway).publish(campaign.inputs)

    assert gateway.campaign_pr is None


def test_publish_rejects_toctou_symlink_race_on_patch_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = _build_campaign(tmp_path)
    gateway = FakeCampaignGateway(campaign.root, campaign.base_commit)
    patch_path = campaign.patch_path_by_id["candidate-1"]
    real_open_no_follow = optimization_publication._open_no_follow

    def racing_open(path: Path):
        if path.name == patch_path.name:
            raise OSError(errno.ELOOP, "Too many levels of symbolic links")
        return real_open_no_follow(path)

    monkeypatch.setattr(optimization_publication, "_open_no_follow", racing_open)

    with pytest.raises(UnsafeCampaignArtifactPathError):
        _publisher(gateway).publish(campaign.inputs)

    assert gateway.campaign_pr is None


def test_publish_still_reads_binary_patch_bytes_exactly_after_toctou_hardening(
    tmp_path: Path,
) -> None:
    # Regression guard: the fd-based read must remain fully binary-safe
    # (no accidental text decode/re-encode) after the TOCTOU hardening.
    campaign = _build_campaign(
        tmp_path,
        candidate_ids=("candidate-1",),
        patch_bytes_by_id={"candidate-1": _BINARY_PATCH_BYTES},
    )
    gateway = FakeCampaignGateway(campaign.root, campaign.base_commit)

    _publisher(gateway).publish(campaign.inputs)

    head_commit = gateway.campaign_pr.head_commit
    patch_path = campaign.patch_path_by_id["candidate-1"]
    committed_bytes = _git_show(campaign.root, head_commit, patch_path)
    assert committed_bytes == _BINARY_PATCH_BYTES
