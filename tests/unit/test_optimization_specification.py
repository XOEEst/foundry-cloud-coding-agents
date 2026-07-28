from __future__ import annotations

import hashlib
import json
from pathlib import Path
from textwrap import dedent

import pytest

from foundry_opt.config.models import OptimizerConfig
from foundry_opt.github_workflow.errors import GitHubPermissionDeniedError
from foundry_opt.github_workflow.models import (
    GitHubCapabilities,
    GitHubPermissionReport,
    IssueReference,
    PullRequestReference,
    RepositoryState,
)
from foundry_opt.optimization.assets import (
    CustomEvaluatorAssetProvider,
    EvaluationAssetProviderRegistry,
    RepositoryAssetProvider,
    SyntheticDatasetProvider,
    TraceEvaluationAssetProvider,
)
from foundry_opt.optimization.specification import (
    OptimizationSpecService,
    SpecBranchConflictError,
    SpecServiceStatus,
    provenance_file_path,
    spec_branch_name,
    spec_file_path,
    spec_issue_marker,
)


_REPOSITORY = "octo-org/optimizer"
_DEFAULT_BRANCH = "main"
_DEFAULT_COMMIT = "a" * 40
_ISSUE_NUMBER = 7

_GOAL = (
    "Improve retrieval accuracy for support agent responses across "
    "development and validation datasets."
)


# ---------------------------------------------------------------------------
# Issue body construction
# ---------------------------------------------------------------------------


def _synthetic_datasets_section() -> str:
    return dedent(
        """
        - asset_id: dev-set
          source: synthetic
          role: development
          parameters:
            row_count: 2
            rows:
              - query: "What is our refund policy?"
                expected_behavior: "cites the refund policy document"
              - query: "How do I reset my password?"
                expected_behavior: "walks through the reset flow"
        - asset_id: val-set
          source: synthetic
          role: validation
          parameters:
            row_count: 1
            rows:
              - query: "What are your support hours?"
                expected_behavior: "states the support hours"
        """
    ).strip()


def _custom_evaluator_section() -> str:
    return dedent(
        """
        - asset_id: quality-eval
          source: custom
          name: quality
          version: v1
          path: evaluators/quality.py
          metrics:
            - quality
        """
    ).strip()


def _metrics_section() -> str:
    return dedent(
        """
        quality:
          direction: maximize
          threshold: 0.8
          materiality: 0.05
          hard_guardrail: false
          undefined_behavior: fail
        """
    ).strip()


def _issue_body(
    *,
    target: str = "support_agent",
    goal: str = _GOAL,
    datasets: str | None = None,
    evaluators: str | None = None,
    metrics: str | None = None,
    mutations: str = "- system_instructions",
    candidate: str = "human",
    deployment: str = "human",
) -> str:
    sections = (
        ("Configured target", target),
        ("Optimization goal", goal),
        ("Dataset requests", datasets or _synthetic_datasets_section()),
        ("Evaluator requests", evaluators or _custom_evaluator_section()),
        ("Metric policies", metrics or _metrics_section()),
        ("Allowed mutations", mutations),
        ("Candidate decision", candidate),
        ("Deployment decision", deployment),
        ("Confirmation", "- [x] I have read the contribution guidelines"),
    )
    return "\n".join(f"### {heading}\n\n{body}\n" for heading, body in sections)


# ---------------------------------------------------------------------------
# OptimizerConfig fixture
# ---------------------------------------------------------------------------


def _config(**automation_overrides: object) -> OptimizerConfig:
    automation_policy = {
        "allowed_dataset_sources": ["synthetic", "repository", "trace"],
        "allowed_evaluator_sources": ["custom", "repository"],
        "synthetic_max_rows": 10,
        "trace_requires_human_review": True,
        "allow_spec_auto_approval": False,
        "allow_candidate_auto_selection": False,
        "allow_merge": False,
        "allow_deployment": False,
    }
    automation_policy.update(automation_overrides)
    document = {
        "schema_version": "1",
        "default_environment": "acceptance",
        "environments": {
            "acceptance": {
                "project_endpoint": (
                    "https://example.services.ai.azure.com/api/projects/demo"
                ),
                "project_resource_id": (
                    "/subscriptions/sub/resourceGroups/rg/providers/"
                    "Microsoft.CognitiveServices/accounts/foundry/projects/demo"
                ),
                "allowed_models": ["gpt-5.1"],
                "deployment_workflow": {
                    "path": ".github/workflows/deploy.yml",
                    "trigger": "manual",
                },
            }
        },
        "targets": {
            "support_agent": {
                "environment": "acceptance",
                "source_paths": ["agent"],
                "edit_paths": ["agent"],
                "entry_point": "agent/main.py",
                "base_agent_version": "12",
                "package": {"include": ["agent/**"], "exclude": []},
                "datasets": {
                    "development": [
                        {"name": "dev", "version": "v1", "mode": "batch"}
                    ],
                    "validation": [
                        {"name": "held-out", "version": "v1", "mode": "batch"}
                    ],
                },
                "evaluators": [
                    {
                        "name": "quality",
                        "reference": "quality-evaluator",
                        "metrics": ["quality"],
                    }
                ],
                "validation_commands": ["uv run pytest -q"],
                "metrics": {
                    "quality": {
                        "direction": "maximize",
                        "threshold": 0.8,
                        "materiality": 0.05,
                        "hard_guardrail": False,
                        "undefined_behavior": "fail",
                    }
                },
                "allowed_mutations": ["system_instructions"],
            }
        },
        "campaign": {
            "deadline_minutes": 50,
            "candidate_cutoff_minutes": 40,
            "max_changed_candidates": 3,
            "transient_retries": 1,
            "stale_after_hours": 2,
            "evidence_path": ".foundry-optimizer/campaigns",
            "allowed_issue_overrides": [],
            "allowed_mutations": ["system_instructions"],
        },
        "automation_policy": automation_policy,
    }
    return OptimizerConfig.model_validate(document)


# ---------------------------------------------------------------------------
# Gateway / publisher test doubles
# ---------------------------------------------------------------------------


_FULL_CAPABILITIES = (
    GitHubCapabilities.METADATA_READ
    | GitHubCapabilities.ISSUES_WRITE
    | GitHubCapabilities.CONTENTS_WRITE
    | GitHubCapabilities.PULL_REQUESTS_WRITE
)


class FakeGateway:
    def __init__(
        self,
        *,
        issue: IssueReference | None,
        state: RepositoryState,
        pull_request: PullRequestReference | None = None,
        granted: GitHubCapabilities = _FULL_CAPABILITIES,
    ) -> None:
        self.issue = issue
        self.state = state
        self.existing_pull_request = pull_request
        self.granted = granted
        self.comments: list[tuple[int, str]] = []
        self.comment_markers: set[str] = set()
        self.added_labels: list[tuple[int, tuple[str, ...]]] = []
        self.removed_labels: list[tuple[int, tuple[str, ...]]] = []
        self.raise_on_comment = False
        self.raise_on_labels = False

    def verify_permissions(
        self, required: GitHubCapabilities
    ) -> GitHubPermissionReport:
        return GitHubPermissionReport(self.granted & required)

    def repository_state(self, repository_root: Path) -> RepositoryState:
        return self.state

    def get_issue(
        self, repository_root: Path, issue_number: int
    ) -> IssueReference | None:
        return self.issue

    def find_spec_pull_request(
        self, repository_root: Path, issue_number: int
    ) -> PullRequestReference | None:
        return self.existing_pull_request

    def comment_issue(
        self, repository_root: Path, issue_number: int, body: str
    ) -> None:
        if self.raise_on_comment:
            raise RuntimeError("posting the comment failed")
        self.comments.append((issue_number, body))
        self.comment_markers.add(body.splitlines()[0])

    def has_issue_comment(
        self, repository_root: Path, issue_number: int, marker: str
    ) -> bool:
        return marker in self.comment_markers

    def add_labels(
        self,
        repository_root: Path,
        issue_number: int,
        labels: tuple[str, ...],
    ) -> None:
        if self.raise_on_labels:
            raise RuntimeError("adding labels failed")
        self.added_labels.append((issue_number, labels))

    def remove_labels(
        self,
        repository_root: Path,
        issue_number: int,
        labels: tuple[str, ...],
    ) -> None:
        if self.raise_on_labels:
            raise RuntimeError("removing labels failed")
        self.removed_labels.append((issue_number, labels))


class FakePublisher:
    def __init__(
        self,
        *,
        remote_branches: dict[str, str] | None = None,
        next_pr_number: int = 100,
    ) -> None:
        self.commits: list[dict[str, object]] = []
        self.published: list[PullRequestReference] = []
        self.remote_branches = dict(remote_branches or {})
        self._next_pr_number = next_pr_number

    def prepare_commit(
        self,
        repository_root: Path,
        *,
        base_commit: str,
        files: dict[Path, bytes],
        message: str,
    ) -> str:
        self.commits.append(
            {
                "base_commit": base_commit,
                "files": dict(files),
                "message": message,
            }
        )
        digest_source = b"|".join(
            (
                base_commit.encode(),
                message.encode(),
                *(
                    f"{path.as_posix()}:{content.hex()}".encode()
                    for path, content in sorted(
                        files.items(), key=lambda item: item[0].as_posix()
                    )
                ),
            )
        )
        return hashlib.sha1(digest_source).hexdigest()

    def publish(
        self,
        repository_root: Path,
        *,
        base_branch: str,
        branch: str,
        commit_sha: str,
        title: str,
        body: str,
    ) -> PullRequestReference:
        existing = self.remote_branches.get(branch)
        if existing is not None and existing != commit_sha:
            raise SpecBranchConflictError(branch, existing)
        self.remote_branches[branch] = commit_sha
        number = self._next_pr_number
        self._next_pr_number += 1
        reference = PullRequestReference(
            number=number,
            url=f"https://github.com/{_REPOSITORY}/pull/{number}",
            head_branch=branch,
            head_commit=commit_sha,
            draft=True,
            body=body,
            base_branch=base_branch,
            state="OPEN",
        )
        self.published.append(reference)
        return reference


def _issue(
    *,
    body: str,
    number: int = _ISSUE_NUMBER,
    labels: tuple[str, ...] = ("needs-triage",),
    state: str = "OPEN",
    repository: str = _REPOSITORY,
) -> IssueReference:
    return IssueReference(
        number=number,
        url=f"https://github.com/{repository}/issues/{number}",
        title="Optimize the support agent",
        body=body,
        state=state,
        labels=labels,
    )


def _state(
    *,
    repository: str = _REPOSITORY,
    branch: str = _DEFAULT_BRANCH,
    commit: str = _DEFAULT_COMMIT,
) -> RepositoryState:
    return RepositoryState(repository, branch, commit)


def _registry(max_rows: int = 50) -> EvaluationAssetProviderRegistry:
    registry = EvaluationAssetProviderRegistry()
    registry.register(RepositoryAssetProvider())
    registry.register(SyntheticDatasetProvider(max_rows=max_rows))
    registry.register(TraceEvaluationAssetProvider())
    registry.register(CustomEvaluatorAssetProvider())
    return registry


def _write_evaluator_file(repository_root: Path) -> None:
    path = repository_root / "evaluators" / "quality.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("def evaluate(row):\n    return 1.0\n", encoding="utf-8")


def _service(
    *,
    config: OptimizerConfig,
    gateway: FakeGateway,
    publisher: FakePublisher,
    max_rows: int = 50,
) -> OptimizationSpecService:
    return OptimizationSpecService(
        config,
        registry=_registry(max_rows=max_rows),
        gateway=gateway,
        publisher=publisher,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_prepares_a_complete_specification_for_synthetic_assets(
    tmp_path: Path,
) -> None:
    _write_evaluator_file(tmp_path)
    gateway = FakeGateway(issue=_issue(body=_issue_body()), state=_state())
    publisher = FakePublisher()
    service = _service(config=_config(), gateway=gateway, publisher=publisher)

    result = service.prepare_specification(tmp_path, _ISSUE_NUMBER)

    assert result.status is SpecServiceStatus.COMPLETE
    assert result.spec is not None
    assert result.spec.repository == _REPOSITORY
    assert result.spec.base_commit == _DEFAULT_COMMIT
    assert result.spec.target == "support_agent"
    assert result.spec_sha256 == result.spec.sha256
    assert result.branch == spec_branch_name(_ISSUE_NUMBER, result.spec_sha256)
    assert result.pull_request is not None
    assert result.pull_request.head_branch == result.branch
    assert result.issue_updated is True
    assert not result.failures

    prepared_paths = {item.path for item in result.prepared_files}
    assert spec_file_path(_ISSUE_NUMBER) in prepared_paths
    assert provenance_file_path(_ISSUE_NUMBER) in prepared_paths
    # Synthetic dataset content is namespaced under the issue's own spec
    # directory rather than the provider's fixed, un-namespaced path.
    assert (
        Path(".foundry-optimizer/specs/issue-7/assets/dev-set.jsonl")
        in prepared_paths
    )
    assert (
        Path(".foundry-optimizer/specs/issue-7/assets/val-set.jsonl")
        in prepared_paths
    )
    assert Path(".foundry/datasets/dev-set.jsonl") not in prepared_paths
    assert Path(".foundry/datasets/val-set.jsonl") not in prepared_paths
    # The custom evaluator already exists, unchanged, at its tracked
    # repository path; it is referenced by hash only and is never
    # redundantly re-committed.
    assert Path("evaluators/quality.py") not in prepared_paths

    # Assets are prepared but never registered with Foundry: the commit
    # never contains a remote registration call, only local file content.
    assert len(publisher.commits) == 1
    assert publisher.published[0].draft is True

    marker = spec_issue_marker(_ISSUE_NUMBER)
    assert marker in publisher.published[0].body
    assert "not approved" in publisher.published[0].body

    assert gateway.added_labels == [(_ISSUE_NUMBER, ("ready-for-human",))]
    assert gateway.removed_labels == [(_ISSUE_NUMBER, ("needs-triage",))]


def test_never_claims_approval_before_merge(tmp_path: Path) -> None:
    _write_evaluator_file(tmp_path)
    gateway = FakeGateway(issue=_issue(body=_issue_body()), state=_state())
    publisher = FakePublisher()
    service = _service(config=_config(), gateway=gateway, publisher=publisher)

    service.prepare_specification(tmp_path, _ISSUE_NUMBER)

    body = publisher.published[0].body.casefold()
    assert "this pull request is not approved" in body
    comment_bodies = " ".join(body for _, body in gateway.comments).casefold()
    assert "is approved" not in comment_bodies


def test_deterministic_hash_branch_and_paths_across_two_runs(
    tmp_path: Path,
) -> None:
    _write_evaluator_file(tmp_path)
    config = _config()

    gateway_one = FakeGateway(issue=_issue(body=_issue_body()), state=_state())
    result_one = _service(
        config=config, gateway=gateway_one, publisher=FakePublisher()
    ).prepare_specification(tmp_path, _ISSUE_NUMBER)

    gateway_two = FakeGateway(issue=_issue(body=_issue_body()), state=_state())
    result_two = _service(
        config=config, gateway=gateway_two, publisher=FakePublisher()
    ).prepare_specification(tmp_path, _ISSUE_NUMBER)

    assert result_one.spec_sha256 == result_two.spec_sha256
    assert result_one.branch == result_two.branch
    assert {item.path for item in result_one.prepared_files} == {
        item.path for item in result_two.prepared_files
    }
    assert {item.sha256 for item in result_one.prepared_files} == {
        item.sha256 for item in result_two.prepared_files
    }


# ---------------------------------------------------------------------------
# Prepared asset path namespacing / collision safety
# ---------------------------------------------------------------------------


def test_synthetic_asset_paths_are_namespaced_and_do_not_collide_across_issues(
    tmp_path: Path,
) -> None:
    config = _config()

    _write_evaluator_file(tmp_path)
    gateway_a = FakeGateway(
        issue=_issue(body=_issue_body(), number=7), state=_state()
    )
    publisher_a = FakePublisher()
    result_a = _service(
        config=config, gateway=gateway_a, publisher=publisher_a
    ).prepare_specification(tmp_path, 7)
    assert result_a.status is SpecServiceStatus.COMPLETE

    gateway_b = FakeGateway(
        issue=_issue(body=_issue_body(), number=8), state=_state()
    )
    publisher_b = FakePublisher()
    result_b = _service(
        config=config, gateway=gateway_b, publisher=publisher_b
    ).prepare_specification(tmp_path, 8)
    assert result_b.status is SpecServiceStatus.COMPLETE

    paths_a = {item.path for item in result_a.prepared_files}
    paths_b = {item.path for item in result_b.prepared_files}

    # Same asset_id ("dev-set"/"val-set"), different issue: the committed
    # paths are namespaced per issue and never collide.
    assert Path(".foundry-optimizer/specs/issue-7/assets/dev-set.jsonl") in paths_a
    assert Path(".foundry-optimizer/specs/issue-8/assets/dev-set.jsonl") in paths_b
    assert not paths_a & paths_b


def test_does_not_overwrite_a_pre_existing_tracked_customer_file(
    tmp_path: Path,
) -> None:
    # A customer file happens to already exist at the provider's naive,
    # un-namespaced synthetic dataset path. Namespacing must ensure the
    # generated commit never references (and therefore never overwrites)
    # this path.
    customer_path = tmp_path / ".foundry" / "datasets" / "dev-set.jsonl"
    customer_path.parent.mkdir(parents=True, exist_ok=True)
    customer_content = b'{"do_not_touch": true}\n'
    customer_path.write_bytes(customer_content)

    _write_evaluator_file(tmp_path)
    gateway = FakeGateway(issue=_issue(body=_issue_body()), state=_state())
    publisher = FakePublisher()
    service = _service(config=_config(), gateway=gateway, publisher=publisher)

    result = service.prepare_specification(tmp_path, _ISSUE_NUMBER)

    assert result.status is SpecServiceStatus.COMPLETE
    committed_paths = set(publisher.commits[0]["files"].keys())
    assert Path(".foundry/datasets/dev-set.jsonl") not in committed_paths
    # The customer's on-disk file is untouched.
    assert customer_path.read_bytes() == customer_content


def test_provenance_records_the_materialized_local_path_for_each_asset(
    tmp_path: Path,
) -> None:
    _write_evaluator_file(tmp_path)
    gateway = FakeGateway(issue=_issue(body=_issue_body()), state=_state())
    publisher = FakePublisher()
    service = _service(config=_config(), gateway=gateway, publisher=publisher)

    result = service.prepare_specification(tmp_path, _ISSUE_NUMBER)
    assert result.status is SpecServiceStatus.COMPLETE

    files = publisher.commits[0]["files"]
    provenance = json.loads(files[provenance_file_path(_ISSUE_NUMBER)])

    datasets_by_id = {item["asset_id"]: item for item in provenance["datasets"]}
    assert datasets_by_id["dev-set"]["path"] == (
        ".foundry-optimizer/specs/issue-7/assets/dev-set.jsonl"
    )
    assert datasets_by_id["val-set"]["path"] == (
        ".foundry-optimizer/specs/issue-7/assets/val-set.jsonl"
    )

    evaluators_by_id = {
        item["asset_id"]: item for item in provenance["evaluators"]
    }
    # The custom evaluator remains a hash reference to its existing tracked
    # path; it is recorded for lookup after merge but never re-committed.
    assert evaluators_by_id["quality-eval"]["path"] == "evaluators/quality.py"


# ---------------------------------------------------------------------------
# Trace / human review
# ---------------------------------------------------------------------------


def test_trace_datasets_are_blocked_before_anything_is_committed(
    tmp_path: Path,
) -> None:
    datasets = dedent(
        """
        - asset_id: dev-set
          source: synthetic
          role: development
          parameters:
            row_count: 1
            rows:
              - query: "What is our refund policy?"
                expected_behavior: "cites the refund policy document"
        - asset_id: val-set
          source: trace
          role: validation
          approval_gate: human
        """
    ).strip()
    gateway = FakeGateway(
        issue=_issue(body=_issue_body(datasets=datasets)), state=_state()
    )
    publisher = FakePublisher()
    service = _service(config=_config(), gateway=gateway, publisher=publisher)

    result = service.prepare_specification(tmp_path, _ISSUE_NUMBER)

    assert result.status is SpecServiceStatus.BLOCKED
    assert any("human review" in blocker for blocker in result.blockers)
    assert not publisher.commits
    assert not publisher.published
    assert gateway.comments


# ---------------------------------------------------------------------------
# Policy violations
# ---------------------------------------------------------------------------


def test_blocks_when_the_target_is_not_configured(tmp_path: Path) -> None:
    gateway = FakeGateway(
        issue=_issue(body=_issue_body(target="unconfigured_agent")),
        state=_state(),
    )
    publisher = FakePublisher()
    service = _service(config=_config(), gateway=gateway, publisher=publisher)

    result = service.prepare_specification(tmp_path, _ISSUE_NUMBER)

    assert result.status is SpecServiceStatus.BLOCKED
    assert any("not configured" in blocker for blocker in result.blockers)
    assert not publisher.commits


def test_blocks_when_the_requested_mutation_is_not_allowed_by_the_target(
    tmp_path: Path,
) -> None:
    gateway = FakeGateway(
        issue=_issue(body=_issue_body(mutations="- python_logic")),
        state=_state(),
    )
    publisher = FakePublisher()
    config = _config()
    # The campaign must also allow python_logic so only the target check
    # (not config validation itself) is exercised by this scenario.
    document = config.model_dump(mode="json")
    document["campaign"]["allowed_mutations"] = [
        "system_instructions",
        "python_logic",
    ]
    config = OptimizerConfig.model_validate(document)
    service = _service(config=config, gateway=gateway, publisher=publisher)

    result = service.prepare_specification(tmp_path, _ISSUE_NUMBER)

    assert result.status is SpecServiceStatus.BLOCKED
    assert any("mutations" in blocker for blocker in result.blockers)
    assert not publisher.commits


def test_blocks_when_a_dataset_source_is_not_allowed_by_policy(
    tmp_path: Path,
) -> None:
    gateway = FakeGateway(issue=_issue(body=_issue_body()), state=_state())
    publisher = FakePublisher()
    config = _config(allowed_dataset_sources=["repository", "trace"])
    service = _service(config=config, gateway=gateway, publisher=publisher)

    result = service.prepare_specification(tmp_path, _ISSUE_NUMBER)

    assert result.status is SpecServiceStatus.BLOCKED
    assert any("source" in blocker for blocker in result.blockers)
    assert not publisher.commits


def test_blocks_when_an_evaluator_source_is_not_allowed_by_policy(
    tmp_path: Path,
) -> None:
    gateway = FakeGateway(issue=_issue(body=_issue_body()), state=_state())
    publisher = FakePublisher()
    config = _config(allowed_evaluator_sources=["repository"])
    service = _service(config=config, gateway=gateway, publisher=publisher)

    result = service.prepare_specification(tmp_path, _ISSUE_NUMBER)

    assert result.status is SpecServiceStatus.BLOCKED
    assert any("source" in blocker for blocker in result.blockers)
    assert not publisher.commits


def test_blocks_when_synthetic_row_count_exceeds_policy(
    tmp_path: Path,
) -> None:
    gateway = FakeGateway(issue=_issue(body=_issue_body()), state=_state())
    publisher = FakePublisher()
    config = _config(synthetic_max_rows=1)
    service = _service(config=config, gateway=gateway, publisher=publisher)

    result = service.prepare_specification(tmp_path, _ISSUE_NUMBER)

    assert result.status is SpecServiceStatus.BLOCKED
    assert any("row_count" in blocker for blocker in result.blockers)
    assert not publisher.commits


# ---------------------------------------------------------------------------
# Issue / label / repository gating
# ---------------------------------------------------------------------------


def test_blocks_when_the_issue_is_missing_or_is_a_pull_request(
    tmp_path: Path,
) -> None:
    gateway = FakeGateway(issue=None, state=_state())
    publisher = FakePublisher()
    service = _service(config=_config(), gateway=gateway, publisher=publisher)

    result = service.prepare_specification(tmp_path, _ISSUE_NUMBER)

    assert result.status is SpecServiceStatus.BLOCKED
    assert any("issue" in blocker for blocker in result.blockers)


def test_blocks_when_the_issue_repository_does_not_match(
    tmp_path: Path,
) -> None:
    gateway = FakeGateway(
        issue=_issue(body=_issue_body(), repository="someone-else/other-repo"),
        state=_state(),
    )
    publisher = FakePublisher()
    service = _service(config=_config(), gateway=gateway, publisher=publisher)

    result = service.prepare_specification(tmp_path, _ISSUE_NUMBER)

    assert result.status is SpecServiceStatus.BLOCKED
    assert any("repository" in blocker for blocker in result.blockers)


def test_blocks_when_the_issue_is_closed(tmp_path: Path) -> None:
    gateway = FakeGateway(
        issue=_issue(body=_issue_body(), state="CLOSED"), state=_state()
    )
    publisher = FakePublisher()
    service = _service(config=_config(), gateway=gateway, publisher=publisher)

    result = service.prepare_specification(tmp_path, _ISSUE_NUMBER)

    assert result.status is SpecServiceStatus.BLOCKED
    assert any("open" in blocker for blocker in result.blockers)


def test_blocks_when_the_issue_lacks_a_canonical_label(tmp_path: Path) -> None:
    gateway = FakeGateway(
        issue=_issue(body=_issue_body(), labels=("bug",)), state=_state()
    )
    publisher = FakePublisher()
    service = _service(config=_config(), gateway=gateway, publisher=publisher)

    result = service.prepare_specification(tmp_path, _ISSUE_NUMBER)

    assert result.status is SpecServiceStatus.BLOCKED
    assert any("label" in blocker for blocker in result.blockers)


def test_raises_when_required_permissions_are_missing(tmp_path: Path) -> None:
    gateway = FakeGateway(
        issue=_issue(body=_issue_body()),
        state=_state(),
        granted=GitHubCapabilities.METADATA_READ,
    )
    publisher = FakePublisher()
    service = _service(config=_config(), gateway=gateway, publisher=publisher)

    with pytest.raises(GitHubPermissionDeniedError):
        service.prepare_specification(tmp_path, _ISSUE_NUMBER)


def test_rejects_an_invalid_issue_specification_as_blocked(
    tmp_path: Path,
) -> None:
    gateway = FakeGateway(
        issue=_issue(body="### Configured target\n\nsupport_agent\n"),
        state=_state(),
    )
    publisher = FakePublisher()
    service = _service(config=_config(), gateway=gateway, publisher=publisher)

    result = service.prepare_specification(tmp_path, _ISSUE_NUMBER)

    assert result.status is SpecServiceStatus.BLOCKED
    assert any("invalid" in blocker for blocker in result.blockers)


# ---------------------------------------------------------------------------
# Pull request idempotency and conflicts
# ---------------------------------------------------------------------------


def test_reuses_an_exact_matching_open_draft_pull_request(
    tmp_path: Path,
) -> None:
    _write_evaluator_file(tmp_path)
    config = _config()

    first_gateway = FakeGateway(issue=_issue(body=_issue_body()), state=_state())
    first_publisher = FakePublisher()
    first_result = _service(
        config=config, gateway=first_gateway, publisher=first_publisher
    ).prepare_specification(tmp_path, _ISSUE_NUMBER)
    assert first_result.status is SpecServiceStatus.COMPLETE

    existing_pull_request = first_publisher.published[0]
    second_gateway = FakeGateway(
        issue=_issue(body=_issue_body()),
        state=_state(),
        pull_request=existing_pull_request,
    )
    second_publisher = FakePublisher()
    second_result = _service(
        config=config, gateway=second_gateway, publisher=second_publisher
    ).prepare_specification(tmp_path, _ISSUE_NUMBER)

    assert second_result.status is SpecServiceStatus.COMPLETE
    assert second_result.pull_request == existing_pull_request
    # The commit is always rebuilt deterministically (git-plumbing only, no
    # checkout) so idempotency can be checked against the exact expected
    # head commit; only the redundant push/PR-create is skipped on reuse.
    assert len(second_publisher.commits) == 1
    assert not second_publisher.published


def test_rejects_a_stale_pull_request_pinned_to_an_old_default_commit(
    tmp_path: Path,
) -> None:
    _write_evaluator_file(tmp_path)
    config = _config()

    stale_commit = "b" * 40
    stale_pull_request = PullRequestReference(
        number=55,
        url=f"https://github.com/{_REPOSITORY}/pull/55",
        head_branch=spec_branch_name(_ISSUE_NUMBER, "0" * 64),
        head_commit="c" * 40,
        draft=True,
        body=(
            f"{spec_issue_marker(_ISSUE_NUMBER)}\n"
            f"Spec SHA-256: `{'0' * 64}`\n"
            f"Base commit: `{stale_commit}`\n"
        ),
        base_branch=_DEFAULT_BRANCH,
        state="OPEN",
    )
    gateway = FakeGateway(
        issue=_issue(body=_issue_body()),
        state=_state(commit=_DEFAULT_COMMIT),
        pull_request=stale_pull_request,
    )
    publisher = FakePublisher()
    service = _service(config=config, gateway=gateway, publisher=publisher)

    result = service.prepare_specification(tmp_path, _ISSUE_NUMBER)

    assert result.status is SpecServiceStatus.CONFLICT
    assert result.pull_request == stale_pull_request
    # The commit is still built deterministically before matching; only the
    # push/PR-create is skipped once the mismatch is detected.
    assert len(publisher.commits) == 1
    assert not publisher.published


def test_rejects_an_existing_pull_request_with_a_mismatched_head_commit(
    tmp_path: Path,
) -> None:
    # Same marker, branch, and body text as an exact match, but a head
    # commit that does not match the deterministically computed spec
    # commit: this must still be treated as a conflict, not reused.
    _write_evaluator_file(tmp_path)
    config = _config()

    probe_result = _service(
        config=config,
        gateway=FakeGateway(issue=_issue(body=_issue_body()), state=_state()),
        publisher=FakePublisher(),
    ).prepare_specification(tmp_path, _ISSUE_NUMBER)
    assert probe_result.status is SpecServiceStatus.COMPLETE
    assert probe_result.branch is not None
    assert probe_result.spec_sha256 is not None

    wrong_head_pull_request = PullRequestReference(
        number=77,
        url=f"https://github.com/{_REPOSITORY}/pull/77",
        head_branch=probe_result.branch,
        head_commit="9" * 40,
        draft=True,
        body=(
            f"{spec_issue_marker(_ISSUE_NUMBER)}\n"
            f"Spec SHA-256: `{probe_result.spec_sha256}`\n"
            f"Base commit: `{_DEFAULT_COMMIT}`\n"
        ),
        base_branch=_DEFAULT_BRANCH,
        state="OPEN",
    )
    gateway = FakeGateway(
        issue=_issue(body=_issue_body()),
        state=_state(),
        pull_request=wrong_head_pull_request,
    )
    publisher = FakePublisher()
    service = _service(config=config, gateway=gateway, publisher=publisher)

    result = service.prepare_specification(tmp_path, _ISSUE_NUMBER)

    assert result.status is SpecServiceStatus.CONFLICT
    assert result.pull_request == wrong_head_pull_request
    assert len(publisher.commits) == 1
    assert not publisher.published


def test_conflict_when_the_branch_is_already_pinned_to_another_commit(
    tmp_path: Path,
) -> None:
    _write_evaluator_file(tmp_path)
    gateway = FakeGateway(issue=_issue(body=_issue_body()), state=_state())
    config = _config()

    # Pre-populate the publisher's remote branch table with a foreign
    # commit at the exact deterministic branch name so ``publish`` raises
    # ``SpecBranchConflictError`` for the branch this run will compute.
    probe_result = _service(
        config=config,
        gateway=FakeGateway(issue=_issue(body=_issue_body()), state=_state()),
        publisher=FakePublisher(),
    ).prepare_specification(tmp_path, _ISSUE_NUMBER)
    branch = probe_result.branch
    assert branch is not None

    publisher = FakePublisher(remote_branches={branch: "f" * 40})
    service = _service(config=config, gateway=gateway, publisher=publisher)

    result = service.prepare_specification(tmp_path, _ISSUE_NUMBER)

    assert result.status is SpecServiceStatus.CONFLICT
    assert any("branch" in blocker for blocker in result.blockers)


# ---------------------------------------------------------------------------
# Partial failure
# ---------------------------------------------------------------------------


def test_partial_status_when_the_ready_comment_fails(tmp_path: Path) -> None:
    _write_evaluator_file(tmp_path)
    gateway = FakeGateway(issue=_issue(body=_issue_body()), state=_state())
    gateway.raise_on_comment = True
    publisher = FakePublisher()
    service = _service(config=_config(), gateway=gateway, publisher=publisher)

    result = service.prepare_specification(tmp_path, _ISSUE_NUMBER)

    assert result.status is SpecServiceStatus.PARTIAL
    assert result.spec is not None
    assert result.pull_request is not None
    assert len(publisher.published) == 1
    assert any(failure.operation == "comment_issue" for failure in result.failures)


def test_partial_status_when_the_label_update_fails(tmp_path: Path) -> None:
    _write_evaluator_file(tmp_path)
    gateway = FakeGateway(issue=_issue(body=_issue_body()), state=_state())
    gateway.raise_on_labels = True
    publisher = FakePublisher()
    service = _service(config=_config(), gateway=gateway, publisher=publisher)

    result = service.prepare_specification(tmp_path, _ISSUE_NUMBER)

    assert result.status is SpecServiceStatus.PARTIAL
    assert any(failure.operation == "update_labels" for failure in result.failures)
    # The ready comment still succeeded even though the label move failed.
    assert gateway.comments
