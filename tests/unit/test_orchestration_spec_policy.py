from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import yaml

from foundry_opt.config.models import AutomationPolicy
from foundry_opt.optimization.models import (
    ApprovalGate,
    AssetKind,
    AssetProvenance,
    OptimizationSpec,
)
from foundry_opt.optimization.specification import PreparedSpecFile
from foundry_opt.optimization.specification import spec_issue_marker
from foundry_opt.orchestration import (
    CampaignPhase,
    CampaignState,
    EventKind,
    SpecFileHash,
)
from foundry_opt.orchestration.spec_policy import (
    MergedSpecApproval,
    GhMergedSpecApprovalReader,
    OptimizationSpecPolicy,
    ResolvedSpecification,
    SpecClassification,
    SpecPolicyRequest,
)


NOW = datetime(2026, 7, 31, tzinfo=UTC)
BASE_COMMIT = "a" * 40
HEAD_COMMIT = "b" * 40
TREE_SHA = "c" * 40
MERGE_COMMIT = "d" * 40
FILES = (
    PreparedSpecFile(Path(".foundry-optimizer/specs/issue-31/optimization-spec.yaml"), "1" * 64),
    PreparedSpecFile(Path(".foundry-optimizer/specs/issue-31/provenance.json"), "2" * 64),
)


def _asset(
    asset_id: str,
    kind: AssetKind,
    source: str,
    *,
    role: str | None = None,
    content: bytes | None = None,
) -> AssetProvenance:
    return AssetProvenance(
        asset_id=asset_id,
        kind=kind,
        source=source,
        role=role,
        name="quality" if source in {"foundry", "builtin"} else None,
        version="1" if source in {"foundry", "builtin"} else None,
        content_sha256=(
            hashlib.sha256(content).hexdigest()
            if content is not None
            else None
        ),
        created_by=f"{source}-provider",
        approval_gate=(
            ApprovalGate.HUMAN
            if source == "trace"
            else ApprovalGate.POLICY
        ),
        remote_id=(
            f"{source}:quality:1"
            if source in {"foundry", "builtin"}
            else None
        ),
        metrics=("quality",) if kind is AssetKind.EVALUATOR else (),
    )


def _spec(
    *,
    dev: AssetProvenance,
    validation: AssetProvenance,
    evaluator: AssetProvenance,
) -> OptimizationSpec:
    from foundry_opt.config.models import MetricPolicy, MutationClass

    return OptimizationSpec(
        issue_number=31,
        repository="octo-org/optimizer",
        base_commit=BASE_COMMIT,
        target="support",
        environment="acceptance",
        base_agent_version="12",
        goal="Improve grounded support answers across both evaluation sets.",
        datasets=(dev, validation),
        evaluators=(evaluator,),
        metrics={
            "quality": MetricPolicy(
                direction="maximize",
                threshold=0.8,
                materiality=0.05,
                hard_guardrail=False,
                undefined_behavior="fail",
            )
        },
        allowed_mutations=frozenset({MutationClass.SYSTEM_INSTRUCTIONS}),
    )


class Resolver:
    def __init__(self, resolved: ResolvedSpecification) -> None:
        self.resolved = resolved

    def resolve(self, repository_root: Path, issue_number: int):
        return self.resolved


class PinnedAssets:
    def __init__(self, files: dict[tuple[str, Path], bytes]) -> None:
        self.files = files

    def read(
        self,
        repository_root: Path,
        *,
        commit: str,
        path: Path,
    ) -> bytes | None:
        return self.files.get((commit, path))


class Approvals:
    def __init__(self, approval: MergedSpecApproval | None = None) -> None:
        self.approval = approval

    def merged_approval(
        self,
        repository_root: Path,
        issue_number: int,
        *,
        expected: CampaignState,
    ) -> MergedSpecApproval | None:
        return self.approval


def _policy(
    resolved: ResolvedSpecification,
    *,
    files: dict[tuple[str, Path], bytes],
    approval: MergedSpecApproval | None = None,
) -> OptimizationSpecPolicy:
    return OptimizationSpecPolicy(
        AutomationPolicy(
            allow_spec_auto_approval=True,
            allowed_dataset_sources={"repository", "foundry"},
            allowed_evaluator_sources={"repository", "foundry", "builtin"},
        ),
        resolver=Resolver(resolved),
        pinned_assets=PinnedAssets(files),
        approvals=Approvals(approval),
        clock=lambda: NOW,
    )


def test_existing_pinned_assets_are_policy_approved_without_planner_pr(
    tmp_path: Path,
) -> None:
    dev = b'{"query":"dev"}\n'
    validation = b'{"query":"validation"}\n'
    spec = _spec(
        dev=_asset(
            "dev",
            AssetKind.DATASET,
            "repository",
            role="development",
            content=dev,
        ),
        validation=_asset(
            "validation",
            AssetKind.DATASET,
            "repository",
            role="validation",
            content=validation,
        ),
        evaluator=_asset("quality", AssetKind.EVALUATOR, "builtin"),
    )
    resolved = ResolvedSpecification(
        spec=spec,
        asset_paths={
            "dev": Path("eval/dev.jsonl"),
            "validation": Path("eval/validation.jsonl"),
            "quality": None,
        },
        base_ref_name="main",
        head_commit=HEAD_COMMIT,
        tree_sha=TREE_SHA,
        prepared_files=FILES,
    )

    decision = _policy(
        resolved,
        files={
            (BASE_COMMIT, Path("eval/dev.jsonl")): dev,
            (BASE_COMMIT, Path("eval/validation.jsonl")): validation,
        },
    ).evaluate(
        SpecPolicyRequest(
            tmp_path,
            31,
            CampaignState(31, 1, 1, CampaignPhase.SPECIFICATION),
        )
    )

    assert decision.classification is SpecClassification.POLICY_APPROVED
    assert decision.event is not None
    assert decision.event.kind is EventKind.SPEC_POLICY_APPROVED
    assert decision.event.payload["spec_sha256"] == spec.sha256
    assert decision.intents == ()
    assert len(decision.objects) == 1
    assert decision.objects[0].path == "objects/specifications/g1.json"
    persisted = json.loads(decision.objects[0].content)
    assert persisted["spec"]["base_commit"] == BASE_COMMIT
    assert persisted["spec"]["goal"] == spec.goal
    assert persisted["asset_paths"] == {
        "dev": "eval/dev.jsonl",
        "quality": None,
        "validation": "eval/validation.jsonl",
    }


def test_changed_repository_asset_requires_specialist_spec_pr(
    tmp_path: Path,
) -> None:
    expected = b"expected\n"
    spec = _spec(
        dev=_asset(
            "dev",
            AssetKind.DATASET,
            "repository",
            role="development",
            content=expected,
        ),
        validation=_asset(
            "validation",
            AssetKind.DATASET,
            "repository",
            role="validation",
            content=expected,
        ),
        evaluator=_asset("quality", AssetKind.EVALUATOR, "foundry"),
    )
    resolved = ResolvedSpecification(
        spec=spec,
        asset_paths={
            "dev": Path("eval/dev.jsonl"),
            "validation": Path("eval/validation.jsonl"),
            "quality": None,
        },
        base_ref_name="main",
        head_commit=HEAD_COMMIT,
        tree_sha=TREE_SHA,
        prepared_files=FILES,
    )

    decision = _policy(
        resolved,
        files={
            (BASE_COMMIT, Path("eval/dev.jsonl")): b"changed\n",
            (BASE_COMMIT, Path("eval/validation.jsonl")): expected,
        },
    ).evaluate(
        SpecPolicyRequest(
            tmp_path,
            31,
            CampaignState(31, 3, 9, CampaignPhase.SPECIFICATION),
        )
    )

    assert decision.classification is SpecClassification.HUMAN_REVIEW
    assert decision.reason == "repository_content_changed"
    assert decision.event is not None
    assert decision.event.kind is EventKind.SPEC_REVIEW_REQUIRED
    assert dict(decision.event.payload) == {
        "base_ref_name": "main",
        "files": [
            {"path": item.path.as_posix(), "sha256": item.sha256}
            for item in FILES
        ],
        "head_commit": HEAD_COMMIT,
        "spec_sha256": spec.sha256,
        "tree_sha": TREE_SHA,
    }
    assert decision.intents[0].kind == "specialist_work_request"
    assert decision.intents[0].payload["specialist"] == (
        "foundry-optimization-planner"
    )


def test_new_synthetic_bytes_can_never_auto_approve(tmp_path: Path) -> None:
    content = b'{"query":"generated"}\n'
    spec = _spec(
        dev=_asset(
            "dev",
            AssetKind.DATASET,
            "synthetic",
            role="development",
            content=content,
        ),
        validation=_asset(
            "validation",
            AssetKind.DATASET,
            "repository",
            role="validation",
            content=content,
        ),
        evaluator=_asset("quality", AssetKind.EVALUATOR, "builtin"),
    )
    resolved = ResolvedSpecification(
        spec=spec,
        asset_paths={
            "dev": Path(".foundry-optimizer/generated/dev.jsonl"),
            "validation": Path("eval/validation.jsonl"),
            "quality": None,
        },
        new_asset_paths=(Path(".foundry-optimizer/generated/dev.jsonl"),),
        base_ref_name="main",
        head_commit=HEAD_COMMIT,
        tree_sha=TREE_SHA,
        prepared_files=FILES,
    )

    decision = _policy(
        resolved,
        files={(BASE_COMMIT, Path("eval/validation.jsonl")): content},
    ).evaluate(
        SpecPolicyRequest(
            tmp_path,
            31,
            CampaignState(31, 1, 1, CampaignPhase.SPECIFICATION),
        )
    )

    assert decision.classification is SpecClassification.HUMAN_REVIEW
    assert decision.reason == "new_asset_bytes"
    assert decision.event is not None
    assert decision.event.kind is EventKind.SPEC_REVIEW_REQUIRED


def test_trace_metadata_advances_to_pinned_human_review(tmp_path: Path) -> None:
    spec = _spec(
        dev=_asset(
            "dev",
            AssetKind.DATASET,
            "trace",
            role="development",
        ),
        validation=_asset(
            "validation",
            AssetKind.DATASET,
            "repository",
            role="validation",
            content=b"validation\n",
        ),
        evaluator=_asset("quality", AssetKind.EVALUATOR, "builtin"),
    )
    resolved = ResolvedSpecification(
        spec=spec,
        asset_paths={
            "dev": None,
            "validation": Path("eval/validation.jsonl"),
            "quality": None,
        },
        base_ref_name="main",
        head_commit=HEAD_COMMIT,
        tree_sha=TREE_SHA,
        prepared_files=FILES,
    )

    decision = _policy(
        resolved,
        files={
            (BASE_COMMIT, Path("eval/validation.jsonl")): b"validation\n"
        },
    ).evaluate(
        SpecPolicyRequest(
            tmp_path,
            31,
            CampaignState(31, 1, 1, CampaignPhase.SPECIFICATION),
        )
    )

    assert decision.reason == "source_not_automated"
    assert decision.spec_sha256 == spec.sha256
    assert decision.event is not None
    assert decision.event.kind is EventKind.SPEC_REVIEW_REQUIRED


def test_human_approval_rejects_stale_generation_and_digest(
    tmp_path: Path,
) -> None:
    digest = "d" * 64
    state = CampaignState(
        31,
        4,
        11,
        CampaignPhase.AWAITING_SPEC_APPROVAL,
        spec_sha256=digest,
        spec_base_ref_name="main",
        spec_head_commit=HEAD_COMMIT,
        spec_tree_sha=TREE_SHA,
        spec_files=tuple(
            SpecFileHash(item.path.as_posix(), item.sha256)
            for item in FILES
        ),
    )
    approval = MergedSpecApproval(
        generation=3,
        pull_request_number=81,
        base_ref_name="main",
        head_commit=HEAD_COMMIT,
        head_tree_sha=TREE_SHA,
        head_files=FILES,
        head_spec_sha256=digest,
        merge_commit=MERGE_COMMIT,
        merge_tree_sha=TREE_SHA,
        merged_files=FILES,
        merged_spec_sha256=digest,
        remote_default_tip="e" * 40,
        merge_reachable_from_default=True,
    )
    policy = OptimizationSpecPolicy(
        AutomationPolicy(),
        resolver=Resolver.__new__(Resolver),
        pinned_assets=PinnedAssets({}),
        approvals=Approvals(approval),
        clock=lambda: NOW,
    )

    decision = policy.evaluate(SpecPolicyRequest(tmp_path, 31, state))

    assert decision.event is None
    assert decision.reason == "approval_generation_mismatch"
    assert decision.intents[0].kind == "spec_approval_rejected"


def test_legacy_v1_approval_is_rematerialized_before_exact_review(
    tmp_path: Path,
) -> None:
    digest = "d" * 64
    legacy = CampaignState(
        31,
        4,
        11,
        CampaignPhase.AWAITING_SPEC_APPROVAL,
        schema_version=1,
        spec_sha256=digest,
    )
    resolved = ResolvedSpecification(
        spec=SimpleNamespace(
            sha256=digest,
            datasets=(),
            evaluators=(),
        ),
        asset_paths={},
        base_ref_name="main",
        head_commit=HEAD_COMMIT,
        tree_sha=TREE_SHA,
        prepared_files=FILES,
    )
    policy = OptimizationSpecPolicy(
        AutomationPolicy(),
        resolver=Resolver(resolved),
        pinned_assets=PinnedAssets({}),
        approvals=Approvals(),
        clock=lambda: NOW,
    )

    decision = policy.evaluate(SpecPolicyRequest(tmp_path, 31, legacy))

    assert decision is not None
    assert decision.disposition.value == "delegate"
    assert decision.reason == "legacy_spec_rematerialized"
    assert decision.event is not None
    assert decision.event.kind is EventKind.SPEC_REVIEW_REQUIRED
    assert decision.event.payload == {
        "base_ref_name": "main",
        "files": [
            {"path": item.path.as_posix(), "sha256": item.sha256}
            for item in FILES
        ],
        "head_commit": HEAD_COMMIT,
        "spec_sha256": digest,
        "tree_sha": TREE_SHA,
    }
    assert decision.intents[0].intent_id == (
        "spec-planner-4-legacy-" + digest[:16]
    )
    assert decision.intents[0].kind == "specialist_work_request"


def test_human_approval_accepts_concurrent_default_branch_changes(
    tmp_path: Path,
) -> None:
    digest = "d" * 64
    state = CampaignState(
        31,
        4,
        11,
        CampaignPhase.AWAITING_SPEC_APPROVAL,
        spec_sha256=digest,
        spec_base_ref_name="main",
        spec_head_commit=HEAD_COMMIT,
        spec_tree_sha=TREE_SHA,
        spec_files=tuple(
            SpecFileHash(item.path.as_posix(), item.sha256)
            for item in FILES
        ),
    )
    approval = MergedSpecApproval(
        generation=4,
        pull_request_number=81,
        base_ref_name="main",
        head_commit=HEAD_COMMIT,
        head_tree_sha=TREE_SHA,
        head_files=FILES,
        head_spec_sha256=digest,
        merge_commit=MERGE_COMMIT,
        merge_tree_sha="f" * 40,
        merged_files=FILES,
        merged_spec_sha256=digest,
        remote_default_tip="e" * 40,
        merge_reachable_from_default=True,
    )
    policy = OptimizationSpecPolicy(
        AutomationPolicy(),
        resolver=Resolver.__new__(Resolver),
        pinned_assets=PinnedAssets({}),
        approvals=Approvals(approval),
        clock=lambda: NOW,
    )

    decision = policy.evaluate(SpecPolicyRequest(tmp_path, 31, state))

    assert decision.event is not None
    assert decision.event.kind is EventKind.SPEC_HUMAN_APPROVED
    assert dict(decision.event.payload) == {
        "head_commit": HEAD_COMMIT,
        "merge_commit": MERGE_COMMIT,
        "pull_request_number": 81,
        "spec_sha256": digest,
    }
    assert decision.reason == "verified_spec_pull_request"


def test_human_approval_rejects_merge_not_reachable_from_current_default(
    tmp_path: Path,
) -> None:
    digest = "d" * 64
    state = CampaignState(
        31,
        4,
        11,
        CampaignPhase.AWAITING_SPEC_APPROVAL,
        spec_sha256=digest,
        spec_base_ref_name="main",
        spec_head_commit=HEAD_COMMIT,
        spec_tree_sha=TREE_SHA,
        spec_files=tuple(
            SpecFileHash(item.path.as_posix(), item.sha256)
            for item in FILES
        ),
    )
    approval = MergedSpecApproval(
        generation=4,
        pull_request_number=81,
        base_ref_name="release",
        head_commit=HEAD_COMMIT,
        head_tree_sha=TREE_SHA,
        head_files=FILES,
        head_spec_sha256=digest,
        merge_commit=MERGE_COMMIT,
        merge_tree_sha=TREE_SHA,
        merged_files=FILES,
        merged_spec_sha256=digest,
        remote_default_tip="e" * 40,
        merge_reachable_from_default=False,
    )
    policy = OptimizationSpecPolicy(
        AutomationPolicy(),
        resolver=Resolver.__new__(Resolver),
        pinned_assets=PinnedAssets({}),
        approvals=Approvals(approval),
        clock=lambda: NOW,
    )

    decision = policy.evaluate(SpecPolicyRequest(tmp_path, 31, state))

    assert decision.event is None
    assert decision.reason == "approval_merge_not_on_default"


def test_merged_approval_reader_resolves_renamed_default_branch_at_approval(
    tmp_path: Path,
    monkeypatch,
) -> None:
    spec = _spec(
        dev=_asset(
            "dev",
            AssetKind.DATASET,
            "repository",
            role="development",
            content=b"dev\n",
        ),
        validation=_asset(
            "validation",
            AssetKind.DATASET,
            "repository",
            role="validation",
            content=b"validation\n",
        ),
        evaluator=_asset("quality", AssetKind.EVALUATOR, "builtin"),
    )
    spec_content = yaml.safe_dump(
        spec.model_dump(mode="json"),
        sort_keys=True,
    ).encode()
    provenance_content = b'{"schema_version":"1"}\n'
    files = (
        PreparedSpecFile(
            Path(
                ".foundry-optimizer/specs/issue-31/"
                "optimization-spec.yaml"
            ),
            hashlib.sha256(spec_content).hexdigest(),
        ),
        PreparedSpecFile(
            Path(".foundry-optimizer/specs/issue-31/provenance.json"),
            hashlib.sha256(provenance_content).hexdigest(),
        ),
    )
    state = CampaignState(
        31,
        4,
        11,
        CampaignPhase.AWAITING_SPEC_APPROVAL,
        spec_sha256=spec.sha256,
        spec_base_ref_name="main",
        spec_head_commit=HEAD_COMMIT,
        spec_tree_sha=TREE_SHA,
        spec_files=tuple(
            SpecFileHash(item.path.as_posix(), item.sha256)
            for item in files
        ),
    )

    class Commands:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []
            self.fetched_ref: str | None = None

        def run(self, arguments, *, cwd, **kwargs):
            args = tuple(arguments)
            self.calls.append(args)
            if args[:3] == ("gh", "repo", "view"):
                return SimpleNamespace(
                    stdout=json.dumps(
                        {"defaultBranchRef": {"name": "trunk"}}
                    )
                )
            if args[:3] == ("gh", "pr", "list"):
                body = "\n".join(
                    (
                        spec_issue_marker(31),
                        "Generation: `4`",
                        f"Spec SHA-256: `{spec.sha256}`",
                    )
                )
                return SimpleNamespace(
                    stdout=json.dumps(
                        [
                            {
                                "baseRefName": "trunk",
                                "body": body,
                                "headRefName": (
                                    "foundry-opt/spec/issue-31/"
                                    f"{spec.sha256[:12]}/generation-4"
                                ),
                                "headRefOid": HEAD_COMMIT,
                                "mergeCommit": {"oid": MERGE_COMMIT},
                                "number": 81,
                            }
                        ]
                    )
                )
            if args[:2] == ("git", "fetch"):
                self.fetched_ref = args[-1]
                return SimpleNamespace(stdout="")
            if args == ("git", "rev-parse", "FETCH_HEAD^{commit}"):
                value = (
                    HEAD_COMMIT
                    if self.fetched_ref == "pull/81/head"
                    else "e" * 40
                )
                return SimpleNamespace(stdout=f"{value}\n")
            if args[:2] == ("git", "rev-parse"):
                value = args[2]
                if value.endswith("^{tree}"):
                    return SimpleNamespace(stdout=f"{TREE_SHA}\n")
                return SimpleNamespace(stdout=f"{'e' * 40}\n")
            if args[:2] == ("git", "show"):
                return SimpleNamespace(stdout=spec_content.decode())
            if args[:3] == ("git", "merge-base", "--is-ancestor"):
                return SimpleNamespace(stdout="")
            raise AssertionError(args)

    content_by_path = {
        files[0].path.as_posix(): spec_content,
        files[1].path.as_posix(): provenance_content,
    }

    def cat_file(arguments, **kwargs):
        path = arguments[-1].split(":", 1)[1]
        return SimpleNamespace(
            returncode=0,
            stdout=content_by_path[path],
        )

    monkeypatch.setattr(
        "foundry_opt.orchestration.spec_policy.subprocess.run",
        cat_file,
    )
    commands = Commands()

    approval = GhMergedSpecApprovalReader(commands).merged_approval(
        tmp_path,
        31,
        expected=state,
    )

    assert approval is not None
    assert approval.base_ref_name == "trunk"
    assert approval.head_tree_sha == TREE_SHA
    assert approval.merge_tree_sha == TREE_SHA
    assert approval.head_files == files
    assert approval.merged_files == files
    assert approval.merge_reachable_from_default is True
    assert "baseRefName" in next(
        call[-1] for call in commands.calls if call[:3] == ("gh", "pr", "list")
    )
    assert (
        "gh",
        "repo",
        "view",
        "--json",
        "defaultBranchRef",
    ) in commands.calls
    assert (
        "git",
        "fetch",
        "--quiet",
        "origin",
        "refs/heads/trunk",
    ) in commands.calls
    assert (
        "git",
        "merge-base",
        "--is-ancestor",
        MERGE_COMMIT,
        "e" * 40,
    ) in commands.calls


def test_merged_approval_reader_requires_current_default_branch_base(
    tmp_path: Path,
) -> None:
    digest = "d" * 64
    state = CampaignState(
        31,
        4,
        11,
        CampaignPhase.AWAITING_SPEC_APPROVAL,
        spec_sha256=digest,
        spec_base_ref_name="main",
        spec_head_commit=HEAD_COMMIT,
        spec_tree_sha=TREE_SHA,
        spec_files=tuple(
            SpecFileHash(item.path.as_posix(), item.sha256)
            for item in FILES
        ),
    )

    class Commands:
        def run(self, arguments, *, cwd, **kwargs):
            args = tuple(arguments)
            if args[:3] == ("gh", "repo", "view"):
                return SimpleNamespace(
                    stdout=json.dumps(
                        {"defaultBranchRef": {"name": "trunk"}}
                    )
                )
            if args[:3] == ("gh", "pr", "list"):
                return SimpleNamespace(
                    stdout=json.dumps(
                        [
                            {
                                "baseRefName": "main",
                                "body": "\n".join(
                                    (
                                        spec_issue_marker(31),
                                        "Generation: `4`",
                                        f"Spec SHA-256: `{digest}`",
                                    )
                                ),
                                "headRefName": (
                                    "foundry-opt/spec/issue-31/"
                                    f"{digest[:12]}/generation-4"
                                ),
                                "headRefOid": HEAD_COMMIT,
                                "mergeCommit": {"oid": MERGE_COMMIT},
                                "number": 81,
                            }
                        ]
                    )
                )
            raise AssertionError(args)

    approval = GhMergedSpecApprovalReader(Commands()).merged_approval(
        tmp_path,
        31,
        expected=state,
    )

    assert approval is None


def test_merged_approval_reader_targets_expected_branch_beyond_100_newer_prs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    digest = "d" * 64
    branch = (
        "foundry-opt/spec/issue-31/"
        f"{digest[:12]}/generation-4"
    )
    state = CampaignState(
        31,
        4,
        11,
        CampaignPhase.AWAITING_SPEC_APPROVAL,
        spec_sha256=digest,
        spec_base_ref_name="main",
        spec_head_commit=HEAD_COMMIT,
        spec_tree_sha=TREE_SHA,
        spec_files=tuple(
            SpecFileHash(item.path.as_posix(), item.sha256)
            for item in FILES
        ),
    )
    body = "\n".join(
        (
            spec_issue_marker(31),
            "Generation: `4`",
            f"Spec SHA-256: `{digest}`",
        )
    )
    target = {
        "baseRefName": "main",
        "body": body,
        "headRefName": branch,
        "headRefOid": HEAD_COMMIT,
        "mergeCommit": {"oid": MERGE_COMMIT},
        "number": 81,
    }

    class Commands:
        def __init__(self) -> None:
            self.fetched_ref: str | None = None

        def run(self, arguments, *, cwd, **kwargs):
            args = tuple(arguments)
            if args[:3] == ("gh", "repo", "view"):
                return SimpleNamespace(
                    stdout=json.dumps(
                        {"defaultBranchRef": {"name": "main"}}
                    )
                )
            if args[:3] == ("gh", "pr", "list"):
                if "--head" not in args:
                    return SimpleNamespace(
                        stdout=json.dumps(
                            [
                                {
                                    "baseRefName": "main",
                                    "body": "newer merged PR",
                                    "headRefName": f"feature-{number}",
                                    "headRefOid": f"{number:040x}",
                                    "mergeCommit": {
                                        "oid": f"{number + 200:040x}"
                                    },
                                    "number": number,
                                }
                                for number in range(101)
                            ]
                        )
                    )
                assert args[args.index("--head") + 1] == branch
                return SimpleNamespace(stdout=json.dumps([target]))
            if args[:2] == ("git", "fetch"):
                self.fetched_ref = args[-1]
                return SimpleNamespace(stdout="")
            if args == ("git", "rev-parse", "FETCH_HEAD^{commit}"):
                value = (
                    HEAD_COMMIT
                    if self.fetched_ref == "pull/81/head"
                    else "e" * 40
                )
                return SimpleNamespace(stdout=f"{value}\n")
            if args[:2] == ("git", "rev-parse"):
                return SimpleNamespace(stdout=f"{TREE_SHA}\n")
            if args[:2] == ("git", "show"):
                return SimpleNamespace(stdout="sha256: ignored")
            if args[:3] == ("git", "merge-base", "--is-ancestor"):
                return SimpleNamespace(stdout="")
            raise AssertionError(args)

    def cat_file(arguments, **kwargs):
        path = arguments[-1].split(":", 1)[1]
        digest_by_path = {
            item.path.as_posix(): item.sha256 for item in FILES
        }
        content = (
            b"spec-content"
            if path.endswith("optimization-spec.yaml")
            else b"provenance-content"
        )
        assert hashlib.sha256(content).hexdigest() == digest_by_path[path]
        return SimpleNamespace(returncode=0, stdout=content)

    reader = GhMergedSpecApprovalReader(Commands())
    monkeypatch.setattr(reader, "_read_files", lambda *args: FILES)
    monkeypatch.setattr(
        reader,
        "_read_spec",
        lambda *args: SimpleNamespace(sha256=digest),
    )

    approval = reader.merged_approval(tmp_path, 31, expected=state)

    assert approval is not None
    assert approval.pull_request_number == 81
