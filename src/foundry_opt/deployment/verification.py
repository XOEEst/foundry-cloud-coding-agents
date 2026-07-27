from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
from statistics import median
import subprocess
import uuid

from foundry_opt.deployment.models import (
    DeploymentCheck,
    DeploymentTrigger,
    DeploymentVerification,
    DeploymentVerificationRequest,
    DeploymentVerificationStatus,
    WorkflowRunStatus,
)
from foundry_opt.evaluation import (
    AgentVersionRef,
    DatasetSplit,
    DatasetVersionRef,
    EvaluationPolicy,
    EvaluationResult,
    EvaluationRun,
    EvaluationStatus,
    EvaluatorDefinitionRef,
    MetricAggregate,
    NormalizedCase,
    NormalizedCaseMetric,
    Outcome,
    UndefinedBehavior,
    Usage,
    select_eligible_candidates,
)
from foundry_opt.evidence.writer import _decision_code
from foundry_opt.github_workflow.errors import CampaignPublicationError
from foundry_opt.github_workflow.publication import (
    _verify_redacted_evidence,
)
from foundry_opt.packaging import BundleRequest, build_source_bundle


_SUCCESS_STATUSES = {"active", "completed", "ready", "succeeded"}


def verify_deployed_selection(
    request: DeploymentVerificationRequest,
) -> DeploymentVerification:
    run = request.workflow_run
    runtime = request.runtime
    if run is None:
        status = (
            DeploymentVerificationStatus.MANUAL_TRIGGER_REQUIRED
            if request.workflow.trigger is DeploymentTrigger.MANUAL
            else DeploymentVerificationStatus.MERGE_DEPLOYMENT_PENDING
        )
        return DeploymentVerification(
            verified=False,
            status=status,
            version=None,
            sha256=None,
            run_url=None,
            portal_url=None,
        )
    if run.status in {
        WorkflowRunStatus.QUEUED,
        WorkflowRunStatus.IN_PROGRESS,
    }:
        return DeploymentVerification(
            verified=False,
            status=DeploymentVerificationStatus.WORKFLOW_PENDING,
            version=None,
            sha256=None,
            run_url=run.url,
            portal_url=None,
        )
    if run.status is not WorkflowRunStatus.SUCCESS:
        return DeploymentVerification(
            verified=False,
            status=DeploymentVerificationStatus.WORKFLOW_FAILED,
            version=None,
            sha256=None,
            run_url=run.url,
            portal_url=None,
        )

    patch_sha256 = _contained_file_sha256(
        request.repository_root,
        request.patch_path,
    )
    evidence_content, evidence_sha256 = _contained_file(
        request.repository_root,
        request.evidence_path,
    )
    bundle_content = _file_content(request.bundle.path)
    bundle_sha256 = (
        hashlib.sha256(bundle_content).hexdigest()
        if bundle_content is not None
        else None
    )
    baseline_bundle_content = _file_content(request.baseline_bundle.path)
    baseline_bundle_sha256 = (
        hashlib.sha256(baseline_bundle_content).hexdigest()
        if baseline_bundle_content is not None
        else None
    )
    reproduction = _reproduce_selection(request)
    checks = (
        DeploymentCheck(
            "patch_sha256",
            patch_sha256 == request.expected_patch_sha256,
        ),
        DeploymentCheck(
            "tree_hash",
            request.deployed_tree_hash == request.expected_tree_hash,
        ),
        DeploymentCheck(
            "reproduced_tree",
            reproduction is not None
            and reproduction.tree_hash == request.expected_tree_hash,
        ),
        DeploymentCheck(
            "reproduced_bundle",
            reproduction is not None
            and bundle_content is not None
            and reproduction.bundle_bytes == bundle_content
            and reproduction.bundle_sha256
            == request.expected_bundle_sha256,
        ),
        DeploymentCheck(
            "bundle_sha256",
            bundle_sha256 == request.expected_bundle_sha256
            == request.bundle.sha256
            == request.record.sha256,
        ),
        DeploymentCheck(
            "bundle_size",
            _file_size(request.bundle.path) == request.bundle.byte_size,
        ),
        DeploymentCheck(
            "evidence_sha256",
            evidence_sha256 == request.expected_evidence_sha256,
        ),
        DeploymentCheck(
            "evidence_lineage",
            _evidence_matches(
                evidence_content,
                request,
            ),
        ),
        DeploymentCheck(
            "baseline_bundle_sha256",
            baseline_bundle_sha256
            == request.expected_baseline_bundle_sha256
            == request.expected_baseline_source_sha256
            == request.baseline_bundle.sha256
            and (
                baseline_bundle_content is not None
                and len(baseline_bundle_content)
                == request.baseline_bundle.byte_size
            ),
        ),
        DeploymentCheck(
            "reproduced_baseline_bundle",
            reproduction is not None
            and baseline_bundle_content is not None
            and reproduction.baseline_bundle_bytes
            == baseline_bundle_content
            and reproduction.baseline_bundle_sha256
            == request.expected_baseline_bundle_sha256
            == request.expected_baseline_source_sha256,
        ),
        DeploymentCheck(
            "published_terminal_status",
            (request.record.status or "").casefold()
            in _SUCCESS_STATUSES,
        ),
        DeploymentCheck(
            "service_provenance_record",
            request.record.project_endpoint
            == request.expected_project_endpoint
            and request.record.agent_name == request.expected_agent_name
            and request.record.base_version
            == request.expected_base_version
            and request.record.baseline_source_sha256
            == request.expected_baseline_source_sha256
            and request.record.version == request.expected_version
            and request.record.sha256 == request.expected_bundle_sha256
            and request.record.patch_sha256
            == request.expected_patch_sha256
            and request.record.tree_hash == request.expected_tree_hash
            and request.record.evidence_sha256
            == request.expected_evidence_sha256
            and request.record.runtime == request.expected_runtime
            and request.record.entry_point
            == request.expected_entry_point
            and request.record.dependency_resolution
            == request.expected_dependency_resolution
            and _record_metadata_matches(request),
        ),
        DeploymentCheck(
            "workflow_identity",
            request.workflow.exists
            and run.path == request.workflow.path
            and run.trigger is request.workflow.trigger
            and run.head_commit == request.expected_commit,
        ),
        DeploymentCheck(
            "workflow_commit_tree",
            reproduction is not None
            and reproduction.workflow_commit_tree
            == request.expected_tree_hash
            == reproduction.tree_hash,
        ),
        DeploymentCheck(
            "workflow_run_url",
            run.url == request.expected_run_url,
        ),
        DeploymentCheck(
            "runtime_present",
            runtime is not None,
        ),
        DeploymentCheck(
            "published_version_identity",
            runtime is not None
            and request.record.version == request.expected_version
            and request.record.version > request.record.base_version
            and runtime.deployed_version == request.expected_version
            and runtime.latest_version == request.expected_version
            and runtime.agent_name == request.record.agent_name,
        ),
        DeploymentCheck(
            "deployed_source_sha256",
            runtime is not None
            and runtime.source_sha256 == request.expected_bundle_sha256
            == request.record.sha256,
        ),
        DeploymentCheck(
            "portal_url",
            runtime is not None
            and runtime.portal_url == request.expected_portal_url
            and request.record.portal_url == request.expected_portal_url,
        ),
    )
    verified = all(check.passed for check in checks)
    return DeploymentVerification(
        verified=verified,
        status=(
            DeploymentVerificationStatus.VERIFIED
            if verified
            else DeploymentVerificationStatus.MISMATCH
        ),
        version=runtime.deployed_version if runtime is not None else None,
        sha256=runtime.source_sha256 if runtime is not None else None,
        run_url=run.url,
        portal_url=runtime.portal_url if runtime is not None else None,
        checks=checks,
    )


def _contained_file_sha256(root: Path, relative: Path) -> str | None:
    _, digest = _contained_file(root, relative)
    return digest


def _contained_file(root: Path, relative: Path) -> tuple[bytes | None, str | None]:
    root = root.expanduser().resolve()
    path = root / relative
    try:
        if path.is_symlink():
            return None, None
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            return None, None
        content = resolved.read_bytes()
    except OSError:
        return None, None
    return content, hashlib.sha256(content).hexdigest()


def _file_content(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _evidence_matches(
    content: bytes | None,
    request: DeploymentVerificationRequest,
) -> bool:
    if content is None:
        return False
    try:
        _verify_redacted_evidence(
            content,
            request.expected_campaign_id,
            request.candidate_id,
            request.expected_patch_sha256,
            (),
        )
        document = json.loads(content)
    except (
        CampaignPublicationError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return False
    if not isinstance(document, dict):
        return False
    candidates = document.get("candidates")
    pareto = document.get("pareto")
    baseline = document.get("baseline")
    if not isinstance(candidates, list) or not isinstance(pareto, dict):
        return False
    eligible = pareto.get("eligible_ids")
    frontier = pareto.get("frontier_ids")
    decisions = pareto.get("decisions")
    matching = [
        candidate
        for candidate in candidates
        if isinstance(candidate, dict)
        and candidate.get("subject_id") == request.candidate_id
        and candidate.get("patch_hash") == request.expected_patch_sha256
    ]
    candidate_ids = [
        candidate.get("subject_id")
        for candidate in candidates
        if isinstance(candidate, dict)
    ]
    if (
        len(candidate_ids) != len(candidates)
        or any(not isinstance(identifier, str) for identifier in candidate_ids)
        or not isinstance(frontier, list)
        or not isinstance(eligible, list)
        or not isinstance(decisions, list)
        or any(not isinstance(identifier, str) for identifier in frontier)
        or any(not isinstance(identifier, str) for identifier in eligible)
    ):
        return False
    decision_by_id: dict[str, bool] = {}
    for decision in decisions:
        if (
            not isinstance(decision, dict)
            or not isinstance(decision.get("subject_id"), str)
            or not isinstance(decision.get("eligible"), bool)
            or decision["subject_id"] in decision_by_id
        ):
            return False
        decision_by_id[decision["subject_id"]] = decision["eligible"]
    candidate_set = set(candidate_ids)
    frontier_set = set(frontier)
    eligible_set = set(eligible)
    consistent_selection = (
        len(candidate_set) == len(candidate_ids)
        and len(frontier_set) == len(frontier)
        and len(eligible_set) == len(eligible)
        and frontier_set <= candidate_set
        and eligible_set <= frontier_set
        and set(decision_by_id) == candidate_set
        and all(
            decision_by_id[identifier] == (identifier in eligible_set)
            for identifier in candidate_set
        )
    )
    return (
        document.get("campaign_id") == request.expected_campaign_id
        and document.get("source_hash")
        == request.expected_baseline_source_sha256
        and isinstance(baseline, dict)
        and baseline.get("subject_id")
        == request.expected_baseline_subject_id
        and consistent_selection
        and request.candidate_id in frontier_set
        and request.candidate_id in eligible_set
        and decision_by_id.get(request.candidate_id) is True
        and len(matching) == 1
        and _selection_matches_evidence(
            baseline,
            candidates,
            pareto,
            request,
        )
    )


def _selection_matches_evidence(
    baseline: object,
    candidates: list[object],
    pareto: dict[str, object],
    request: DeploymentVerificationRequest,
) -> bool:
    try:
        baseline_result = _serialized_result(
            baseline,
            request.expected_metric_policy,
        )
        candidate_results = tuple(
            _serialized_result(candidate, request.expected_metric_policy)
            for candidate in candidates
        )
        recomputed = select_eligible_candidates(
            baseline_result,
            candidate_results,
            request.expected_metric_policy,
        )
        decisions = [
            {
                "subject_id": decision.subject_id,
                "eligible": decision.eligible,
                "reason_code": _decision_code(
                    decision.eligible,
                    decision.reason,
                ),
            }
            for decision in recomputed.decisions
        ]
    except (KeyError, TypeError, ValueError):
        return False
    return (
        pareto.get("frontier_ids") == list(recomputed.frontier_ids)
        and pareto.get("eligible_ids") == list(recomputed.eligible_ids)
        and pareto.get("decisions") == decisions
        and request.candidate_id in recomputed.eligible_ids
    )


def _serialized_result(
    value: object,
    policy: EvaluationPolicy,
) -> EvaluationResult:
    if not isinstance(value, dict):
        raise ValueError
    attempts = value["attempts"]
    if not isinstance(attempts, list) or not attempts:
        raise ValueError
    matching_attempts = [
        attempt
        for attempt in attempts
        if isinstance(attempt, dict)
        and attempt.get("evaluation_id") == value["evaluation_id"]
        and attempt.get("run_id") == value["run_id"]
    ]
    if len(matching_attempts) != 1:
        raise ValueError
    if value["repeat_count"] != len(attempts) - 1:
        raise ValueError
    for attempt in attempts:
        if not isinstance(attempt, dict):
            raise ValueError
        attempt_status = EvaluationStatus(attempt["status"])
        if (
            attempt_status is EvaluationStatus.COMPLETED
            and (
                attempt.get("completed_at") is None
                or attempt.get("error_code") is not None
            )
        ):
            raise ValueError
    primary_attempt = matching_attempts[0]
    status = EvaluationStatus(primary_attempt["status"])
    if (
        status is EvaluationStatus.COMPLETED
        and (
            primary_attempt.get("completed_at") is None
            or primary_attempt.get("error_code") is not None
        )
    ):
        raise ValueError
    if value["error_count"] != 0:
        raise ValueError
    agent = value["agent"]
    dataset = value["dataset"]
    evaluator = value["evaluator"]
    metrics = {
        name: MetricAggregate(
            metric=name,
            median=aggregate["median"],
            minimum=aggregate["minimum"],
            maximum=aggregate["maximum"],
            spread=aggregate["spread"],
            outcome=Outcome(aggregate["outcome"]),
            sample_count=aggregate["sample_count"],
        )
        for name, aggregate in value["metrics"].items()
    }
    if set(metrics) != {metric.name for metric in policy.metrics}:
        raise ValueError
    cases = tuple(
        _serialized_case(case, policy) for case in value["cases"]
    )
    _verify_serialized_aggregates(cases, metrics, policy)
    return EvaluationResult(
        run=EvaluationRun(
            run_id=value["run_id"],
            evaluation_id=value["evaluation_id"],
            subject_id=value["subject_id"],
            split=DatasetSplit(value["split"]),
            agent=AgentVersionRef(
                agent_id=agent["agent_id"],
                draft_id=agent["draft_id"],
                version=agent["version"],
            ),
            dataset=DatasetVersionRef(
                dataset_id=dataset["dataset_id"],
                version=dataset["version"],
            ),
            evaluator=EvaluatorDefinitionRef(
                definition_id=evaluator["definition_id"],
                version=evaluator["version"],
            ),
            status=status,
            portal_url=value["portal_url"],
            started_at=None,
            completed_at=None,
            error=(
                None
                if primary_attempt.get("error_code") is None
                else "provider_error"
            ),
        ),
        cases=cases,
        metrics=metrics,
        usage=Usage(
            input_tokens=value["usage"]["input_tokens"],
            output_tokens=value["usage"]["output_tokens"],
            cached_tokens=value["usage"]["cached_tokens"],
        ),
        duration_ms=value["duration_ms"],
        errors=(),
        complete=value["complete"],
        needs_repeat=False,
        attempts=value["repeat_count"] + 1,
    )


def _serialized_case(
    value: object,
    policy: EvaluationPolicy,
) -> NormalizedCase:
    if not isinstance(value, dict) or value["error_code"] is not None:
        raise ValueError
    serialized_scores = value["scores"]
    if not isinstance(serialized_scores, list):
        raise ValueError
    scores_by_name = {
        score["metric"]: score
        for score in serialized_scores
        if isinstance(score, dict)
    }
    if (
        len(scores_by_name) != len(serialized_scores)
        or set(scores_by_name)
        != {metric.name for metric in policy.metrics}
    ):
        raise ValueError
    scores: list[NormalizedCaseMetric] = []
    for metric in policy.metrics:
        score = scores_by_name[metric.name]
        normalized = score["normalized_score"]
        expected_outcome = (
            Outcome.UNDEFINED
            if normalized is None
            else Outcome.PASS
            if metric.passes(normalized)
            else Outcome.FAIL
        )
        if Outcome(score["outcome"]) is not expected_outcome:
            raise ValueError
        if score["reason_code"] != f"evaluator_{expected_outcome.value}":
            raise ValueError
        scores.append(
            NormalizedCaseMetric(
                metric=metric.name,
                raw_score=score["raw_score"],
                normalized_score=normalized,
                reason=None,
                outcome=expected_outcome,
            )
        )
    outcomes = {score.outcome for score in scores}
    expected_reason = (
        "evaluator_fail"
        if Outcome.FAIL in outcomes
        else "evaluator_undefined"
        if Outcome.UNDEFINED in outcomes
        else "evaluator_pass"
    )
    if value["reason_code"] != expected_reason:
        raise ValueError
    return NormalizedCase(
        case_id=value["case_id"],
        case_hash=value["case_hash"],
        response_ids=tuple(value["response_ids"]),
        scores=tuple(scores),
        usage=Usage(
            input_tokens=value["usage"]["input_tokens"],
            output_tokens=value["usage"]["output_tokens"],
            cached_tokens=value["usage"]["cached_tokens"],
        ),
        trajectory=None,
        error=None,
        duration_ms=value["duration_ms"],
    )


def _verify_serialized_aggregates(
    cases: tuple[NormalizedCase, ...],
    metrics: dict[str, MetricAggregate],
    policy: EvaluationPolicy,
) -> None:
    for metric in policy.metrics:
        aggregate = metrics[metric.name]
        scores = tuple(
            score
            for case in cases
            for score in case.scores
            if score.metric == metric.name
        )
        values = tuple(
            score.normalized_score
            for score in scores
            if score.normalized_score is not None
        )
        undefined = any(
            score.outcome is Outcome.UNDEFINED for score in scores
        )
        if not values:
            if (
                aggregate.sample_count != 0
                or aggregate.median is not None
                or aggregate.minimum is not None
                or aggregate.maximum is not None
                or aggregate.spread is not None
                or aggregate.outcome is not Outcome.UNDEFINED
            ):
                raise ValueError
            continue
        expected_median = float(median(values))
        expected_outcome = (
            Outcome.UNDEFINED
            if undefined
            and metric.undefined_behavior is UndefinedBehavior.FAIL
            else Outcome.PASS
            if metric.passes(expected_median)
            else Outcome.FAIL
        )
        if (
            aggregate.sample_count != len(values)
            or aggregate.minimum != min(values)
            or aggregate.maximum != max(values)
            or aggregate.spread != round(max(values) - min(values), 12)
            or aggregate.median != expected_median
            or aggregate.outcome is not expected_outcome
        ):
            raise ValueError


def _record_metadata_matches(
    request: DeploymentVerificationRequest,
) -> bool:
    expected = {
        "foundry-opt-base-version": str(request.expected_base_version),
        "foundry-opt-baseline-source-sha256": (
            request.expected_baseline_source_sha256
        ),
        "foundry-opt-source-sha256": request.expected_bundle_sha256,
        "foundry-opt-patch-sha256": request.expected_patch_sha256,
        "foundry-opt-tree-hash": request.expected_tree_hash,
        "foundry-opt-evidence-sha256": request.expected_evidence_sha256,
    }
    return all(
        request.record.metadata.get(key) == value
        for key, value in expected.items()
    )


class _Reproduction:
    def __init__(
        self,
        tree_hash: str,
        workflow_commit_tree: str,
        baseline_bundle_bytes: bytes,
        baseline_bundle_sha256: str,
        bundle_bytes: bytes,
        bundle_sha256: str,
    ) -> None:
        self.tree_hash = tree_hash
        self.workflow_commit_tree = workflow_commit_tree
        self.baseline_bundle_bytes = baseline_bundle_bytes
        self.baseline_bundle_sha256 = baseline_bundle_sha256
        self.bundle_bytes = bundle_bytes
        self.bundle_sha256 = bundle_sha256


def _reproduce_selection(
    request: DeploymentVerificationRequest,
) -> _Reproduction | None:
    root = request.repository_root.expanduser().resolve()
    patch = root / request.patch_path
    try:
        patch = patch.resolve(strict=True)
    except OSError:
        return None
    if not patch.is_relative_to(root) or not patch.is_file():
        return None
    scratch = root / f".fv-{uuid.uuid4().hex[:8]}"
    worktree = scratch / "w"
    absolute_worktree = worktree.resolve()
    added = False
    try:
        scratch.mkdir(parents=True, exist_ok=False)
        workflow_commit_tree = _git(
            root,
            "rev-parse",
            f"{request.expected_commit}^{{tree}}",
        )
        _git(
            root,
            "worktree",
            "add",
            "--detach",
            str(absolute_worktree),
            request.expected_base_commit,
        )
        added = True
        baseline_artifact = build_source_bundle(
            _bundle_request(
                request,
                absolute_worktree,
                scratch / "rebuilt-baseline.zip",
            )
        )
        baseline_bundle_bytes = baseline_artifact.path.read_bytes()
        _git(
            absolute_worktree,
            "apply",
            "--index",
            "--binary",
            str(patch),
        )
        tree_hash = _git(worktree, "write-tree")
        artifact = build_source_bundle(
            _bundle_request(
                request,
                absolute_worktree,
                scratch / "rebuilt.zip",
            )
        )
        bundle_bytes = artifact.path.read_bytes()
        return _Reproduction(
            tree_hash,
            workflow_commit_tree,
            baseline_bundle_bytes,
            baseline_artifact.sha256,
            bundle_bytes,
            artifact.sha256,
        )
    except (OSError, RuntimeError, ValueError):
        return None
    finally:
        if added:
            subprocess.run(
                (
                    "git",
                    "worktree",
                    "remove",
                    "--force",
                    str(absolute_worktree),
                ),
                cwd=root,
                capture_output=True,
            )
        shutil.rmtree(scratch, ignore_errors=True)


def _bundle_request(
    request: DeploymentVerificationRequest,
    repository_root: Path,
    output_path: Path,
) -> BundleRequest:
    return BundleRequest(
        repository_root=repository_root,
        output_path=output_path,
        include=request.bundle_include,
        exclude=request.bundle_exclude,
        dependency_resolution=request.bundle_dependency_resolution,
        evidence_paths=request.bundle_evidence_paths,
    )


def _git(root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ("git", *arguments),
            cwd=root,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise RuntimeError("git execution failed") from error
    if result.returncode != 0:
        raise RuntimeError("git verification failed")
    return result.stdout.strip()
