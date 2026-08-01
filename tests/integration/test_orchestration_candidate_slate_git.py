from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import subprocess

from foundry_opt.evaluation import (
    EvaluationPolicy,
    MetricDirection,
    MetricPolicy,
)
from foundry_opt.orchestration import (
    AdvanceRequest,
    CampaignEvent,
    CampaignPhase,
    EventKind,
    GitStateRef,
    OptimizationCampaign,
    OutboxRecord,
)
from foundry_opt.orchestration.candidate_slate import (
    ApplierWorkerResult,
    CandidateBinding,
    CandidatePullRequestSnapshot,
    CandidatePullRequestState,
    CandidateSelectionRequest,
    CandidateSelectionService,
    CandidateSelectionStatus,
    CandidateSlatePlan,
    CandidateSlateRequest,
    CandidateSlateService,
    CandidateSlateStatus,
    applier_worker_result_record,
    candidate_pr_body,
    candidate_pr_marker,
)


NOW = datetime(2026, 7, 31, 18, 0, tzinfo=UTC)


def _run(arguments: tuple[str, ...], cwd: Path) -> str:
    return subprocess.run(
        arguments,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    origin = tmp_path / "origin.git"
    repository = tmp_path / "repository"
    _run(("git", "init", "--bare", str(origin)), tmp_path)
    _run(("git", "init", "-b", "main", str(repository)), tmp_path)
    _run(("git", "config", "user.name", "Slate Test"), repository)
    _run(
        ("git", "config", "user.email", "slate@example.invalid"),
        repository,
    )
    (repository / "README.md").write_text("slate\n", encoding="utf-8")
    _run(("git", "add", "."), repository)
    _run(("git", "commit", "-m", "baseline"), repository)
    _run(("git", "remote", "add", "origin", str(origin)), repository)
    _run(("git", "push", "-u", "origin", "main"), repository)
    return repository, _run(("git", "rev-parse", "HEAD"), repository)


class Resolver:
    def __init__(self, plan: CandidateSlatePlan) -> None:
        self.plan = plan

    def resolve(self, request, state):
        return self.plan


def test_real_git_slate_cas_resume_does_not_duplicate_effects(
    tmp_path: Path,
) -> None:
    repository, base = _repository(tmp_path)
    spec = "a" * 64
    campaign_id = f"issue-31-g1-{spec[:8]}-{base[:8]}"
    patch_path = (
        repository
        / ".foundry-optimizer"
        / "campaigns"
        / campaign_id
        / "candidate-1.patch"
    )
    patch_path.parent.mkdir(parents=True)
    patch = b"diff --git a/README.md b/README.md\n"
    patch_path.write_bytes(patch)
    evidence_path = (
        repository
        / ".foundry-optimizer"
        / "campaigns"
        / campaign_id
        / "candidate-1"
        / "development-evidence.json"
    )
    evidence_path.parent.mkdir(parents=True)
    evidence = (
        json.dumps(
            {
                "campaign_id": campaign_id,
                "metrics": {"quality": 0.9},
                "schema_version": 1,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()
    evidence_path.write_bytes(evidence)
    payload: dict[str, object] = {
        "allowed_paths": ["README.md"],
        "attestation_sha256": "",
        "base_commit": base,
        "bundle_sha256": "b" * 64,
        "candidate_id": "candidate-1",
        "changed_paths": ["README.md"],
        "complexity": "small",
        "draft_id": "draft-candidate-1",
        "eligible": True,
        "evaluation_id": "eval-candidate-1",
        "evidence_path": evidence_path.relative_to(repository).as_posix(),
        "evidence_sha256": hashlib.sha256(evidence).hexdigest(),
        "idea_id": "idea-1",
        "issue_number": 31,
        "lessons": ["Quality improved."],
        "lineage_sha256": "c" * 64,
        "metrics": {"quality": 0.9},
        "motivation": "Improve quality.",
        "mutation_class": "system_instructions",
        "parent_idea_ids": [],
        "patch_path": patch_path.relative_to(repository).as_posix(),
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
        "result": "eligible",
        "result_commit": "d" * 40,
        "run_id": "run-candidate-1",
        "slot": 1,
        "spec_sha256": spec,
        "tree_sha": "e" * 40,
    }
    payload["attestation_sha256"] = hashlib.sha256(
        json.dumps(
            {
                key: value
                for key, value in payload.items()
                if key != "attestation_sha256"
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    events = (
        CampaignEvent("created", EventKind.ISSUE_CREATED, 1, NOW),
        CampaignEvent(
            "approved",
            EventKind.SPEC_POLICY_APPROVED,
            1,
            NOW,
            {"spec_sha256": spec},
        ),
        CampaignEvent(
            "baseline",
            EventKind.BASELINE_COMPLETED,
            1,
            NOW,
            {"evaluation_id": "eval-baseline"},
        ),
        CampaignEvent(
            "candidate",
            EventKind.CANDIDATE_EVALUATED,
            1,
            NOW,
            {
                "candidate_id": "candidate-1",
                "eligible": True,
                "evidence_sha256": payload["evidence_sha256"],
            },
        ),
        CampaignEvent(
            "workers",
            EventKind.CANDIDATE_WORKERS_COMPLETED,
            1,
            NOW,
            {
                "attempted_count": 1,
                "eligible_count": 1,
                "stop_reason": "max_candidates",
            },
        ),
    )
    state = OptimizationCampaign().advance(
        AdvanceRequest(31, None, events)
    ).state
    store = GitStateRef()
    seeded = store.commit(
        repository,
        issue_number=31,
        expected_revision=None,
        state=state,
        inbox=events,
        outbox=(
            OutboxRecord(
                "baseline-attestation-1",
                "candidate_baseline_attestation",
                1,
                state.sequence,
                {
                    "base_commit": base,
                    "bundle_sha256": "f" * 64,
                    "draft_id": "draft-baseline",
                    "evaluation_id": "eval-baseline",
                    "issue_number": 31,
                    "metrics": {"quality": 0.5},
                    "spec_sha256": spec,
                },
            ),
            OutboxRecord(
                "candidate-attestation-1-1",
                "candidate_attestation",
                1,
                state.sequence,
                payload,
            ),
        ),
    )
    service = CandidateSlateService(
        ledger=store,
        resolver=Resolver(
            CandidateSlatePlan(
                issue_number=31,
                generation=1,
                repository="octo-org/optimizer",
                default_branch="main",
                spec_sha256=spec,
                base_commit=base,
                evaluation_policy=EvaluationPolicy(
                    (
                        MetricPolicy(
                            "quality",
                            MetricDirection.MAXIMIZE,
                            0.8,
                            0.05,
                        ),
                    )
                ),
                required_checks=("exact-candidate",),
            )
        ),
    )

    published = service.advance(CandidateSlateRequest(repository, 31))
    resumed = service.advance(CandidateSlateRequest(repository, 31))

    assert published.status is CandidateSlateStatus.PUBLISHED
    assert published.snapshot.state.phase is CampaignPhase.AWAITING_SELECTION
    assert published.snapshot.revision != seeded.revision
    assert resumed.status is CandidateSlateStatus.WAITING
    assert resumed.snapshot.revision == published.snapshot.revision
    assert len(
        [
            record
            for record in resumed.snapshot.outbox
            if record.kind == "applier_worker_issue_planned"
        ]
    ) == 1

    planned = next(
        record
        for record in published.snapshot.outbox
        if record.kind == "applier_worker_issue_planned"
    )
    binding = CandidateBinding(
        issue_number=31,
        generation=1,
        spec_sha256=spec,
        base_commit=base,
        candidate_id="candidate-1",
        draft_id="draft-candidate-1",
        evidence_sha256=str(planned.payload["evidence_sha256"]),
        patch_sha256=str(planned.payload["patch_sha256"]),
        bundle_sha256=str(planned.payload["bundle_sha256"]),
        tree_sha=str(planned.payload["tree_sha"]),
        allowed_paths=tuple(
            Path(path) for path in planned.payload["allowed_paths"]
        ),
        changed_paths=tuple(
            Path(path) for path in planned.payload["changed_paths"]
        ),
    )
    acknowledged = store.commit(
        repository,
        issue_number=31,
        expected_revision=published.snapshot.revision,
        state=published.snapshot.state,
        outbox=(
            applier_worker_result_record(
                planned,
                ApplierWorkerResult(
                    planned.record_id,
                    "worker-result-1",
                    binding,
                    84,
                    True,
                    True,
                ),
                sequence=published.snapshot.state.sequence,
            ),
        ),
    )

    class Reader:
        def snapshots_for(self, request, bindings):
            return (
                CandidatePullRequestSnapshot(
                    pull_request_number=91,
                    worker_issue_number=84,
                    state=CandidatePullRequestState.MERGED,
                    author="copilot-swe-agent[bot]",
                    draft=False,
                    base_ref_name="main",
                    current_default_branch="main",
                    current_default_commit=base,
                    base_commit=base,
                    head_commit="7" * 40,
                    head_parent_commit=base,
                    head_tree_sha=binding.tree_sha,
                    patch_sha256=binding.patch_sha256,
                    changed_paths=binding.changed_paths,
                    body=candidate_pr_body(
                        binding,
                        worker_issue_number=84,
                        required_checks=("exact-candidate",),
                    ),
                    checks={"exact-candidate": "success"},
                    spec_sha256=binding.spec_sha256,
                    bundle_sha256=binding.bundle_sha256,
                    evidence_sha256=binding.evidence_sha256,
                    marker=candidate_pr_marker(binding),
                    merge_commit="9" * 40,
                    merge_parent_commit=base,
                    merge_tree_sha=binding.tree_sha,
                    merge_reachable_from_default=True,
                ),
            )

    selection = CandidateSelectionService(
        ledger=store,
        reader=Reader(),
    )
    selected = selection.advance(
        CandidateSelectionRequest(
            repository,
            31,
            "main",
            ("exact-candidate",),
        )
    )
    selected_resume = selection.advance(
        CandidateSelectionRequest(
            repository,
            31,
            "main",
            ("exact-candidate",),
        )
    )

    assert acknowledged.revision != published.snapshot.revision
    assert selected.status is CandidateSelectionStatus.SELECTED
    assert selected.snapshot.state.phase is CampaignPhase.DEPLOYMENT
    assert selected_resume.status is CandidateSelectionStatus.WAITING
    assert selected_resume.snapshot.revision == selected.snapshot.revision
