from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

from foundry_opt.adapters.commands import SubprocessCommandRunner
from foundry_opt.adapters.optimization_deployment import GhWorkflowRunGateway
from foundry_opt.adapters.github import github_repository_from_remote_url
from foundry_opt.deployment import DEPLOYMENT_OIDC_CLIENT_ID
from foundry_opt.orchestration.deployment import (
    DeploymentBridgeResult,
    DeploymentCleanupBridge,
    DeploymentCleanupBridgeResult,
    DeploymentCleanupEffect,
    DeploymentCleanupKind,
    DeploymentDispatchClaimRecorder,
    DeploymentPublicationResultRecorder,
    DeploymentPublicationStatus,
    DeploymentPublishedVerification,
    DeploymentWorkflowResult,
    DeploymentWorkflowResultRecorder,
    DeploymentWorkflowRunState,
    DeploymentWorkflowBridge,
    ExistingDeploymentWorkflowGateway,
    deployment_workflow_intent,
)
from foundry_opt.orchestration.git_state import GitStateRef, OutboxRecord
from foundry_opt.orchestration.models import CampaignPhase
from foundry_opt.preflight.interfaces import CommandRunner


@dataclass(frozen=True)
class DeploymentBridgeReconcileResult:
    issue_number: int
    results: tuple[DeploymentBridgeResult, ...]
    cleanup_results: tuple[DeploymentCleanupBridgeResult, ...] = ()


class GhDeploymentCleanupGateway:
    def __init__(
        self,
        commands: CommandRunner,
        repository_root: Path,
        repository: str,
        root_issue_number: int,
    ) -> None:
        self._commands = commands
        self._root = repository_root
        self._repository = repository
        self._issue = root_issue_number

    def effect_applied(self, effect_id: str) -> bool:
        raw = self._run(
            (
                "gh",
                "issue",
                "view",
                str(self._issue),
                "--repo",
                self._repository,
                "--json",
                "comments",
            )
        )
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as error:
            raise RuntimeError("cleanup marker response is invalid") from error
        comments = (
            document.get("comments")
            if isinstance(document, dict)
            else None
        )
        if not isinstance(comments, list):
            raise RuntimeError("cleanup marker response is invalid")
        markers = {
            f"<!-- foundry-opt:deployment-effect:{effect_id} -->",
            f"<!-- foundry-opt:projection:{effect_id} -->",
        }
        return any(
            isinstance(comment, dict)
            and isinstance(comment.get("body"), str)
            and any(marker in comment["body"] for marker in markers)
            for comment in comments
        )

    def apply(self, effect: DeploymentCleanupEffect) -> None:
        if effect.kind is DeploymentCleanupKind.FINAL_DASHBOARD:
            self._project_dashboard(effect)
            return
        body = _cleanup_body(effect)
        if effect.kind in {
            DeploymentCleanupKind.CANDIDATE_ISSUE_CLOSE,
            DeploymentCleanupKind.CANDIDATE_ISSUE_SUPERSEDE,
        }:
            self._comment("issue", effect.target_number, body)
            self._run(
                (
                    "gh",
                    "issue",
                    "close",
                    str(effect.target_number),
                    "--repo",
                    self._repository,
                )
            )
            self._acknowledge(effect)
            return
        if effect.kind in {
            DeploymentCleanupKind.CANDIDATE_PR_SUPERSEDE,
            DeploymentCleanupKind.CAMPAIGN_PR_CLOSE,
            DeploymentCleanupKind.OPTIMIZATION_PR_CLOSE,
        }:
            self._comment("pr", effect.target_number, body)
            self._run(
                (
                    "gh",
                    "pr",
                    "close",
                    str(effect.target_number),
                    "--repo",
                    self._repository,
                )
            )
            self._acknowledge(effect)
            return
        if effect.kind is DeploymentCleanupKind.ROOT_COMMENT_FINAL:
            self._comment("issue", self._issue, body)
            return
        if effect.kind is DeploymentCleanupKind.ROOT_ISSUE_CLOSE:
            self._comment("issue", self._issue, body)
            self._run(
                (
                    "gh",
                    "issue",
                    "close",
                    str(self._issue),
                    "--repo",
                    self._repository,
                )
            )
            return
        raise RuntimeError("cleanup effect kind is unsupported")

    def _project_dashboard(self, effect: DeploymentCleanupEffect) -> None:
        from foundry_opt.orchestration.projection import (
            DashboardProjection,
            GhDashboardGateway,
        )

        record = OutboxRecord(
            record_id=effect.effect_id,
            kind=effect.kind.value,
            generation=effect.generation,
            sequence=effect.sequence,
            payload=effect.metadata,
        )

        class _OneRecord:
            def for_issue(self, issue_number: int):
                return (record,) if issue_number == self_issue else ()

        self_issue = self._issue
        DashboardProjection(
            _OneRecord(),
            GhDashboardGateway(
                self._commands,
                self._root,
                self._repository,
            ),
        ).project(self._issue)

    def _acknowledge(self, effect: DeploymentCleanupEffect) -> None:
        self._comment(
            "issue",
            self._issue,
            (
                f"<!-- foundry-opt:deployment-effect:{effect.effect_id} -->\n"
                f"Applied deployment cleanup `{effect.kind.value}`.\n"
            ),
        )

    def _comment(self, kind: str, number: int, body: str) -> None:
        self._run(
            (
                "gh",
                kind,
                "comment",
                str(number),
                "--repo",
                self._repository,
                "--body-file",
                "-",
            ),
            input_text=body,
        )

    def _run(
        self,
        arguments: tuple[str, ...],
        *,
        input_text: str | None = None,
    ) -> str:
        try:
            return self._commands.run(
                arguments,
                cwd=self._root,
                input_text=input_text,
            ).stdout
        except Exception as error:
            raise RuntimeError("GitHub cleanup operation failed") from error


def reconcile_deployment_workflow_effects(
    repository_root: Path,
    issue_number: int,
    commands: CommandRunner,
) -> DeploymentBridgeReconcileResult:
    from foundry_opt.orchestration.issue_intake import (
        GitIssueEventInbox,
        GitStateCampaignRecovery,
    )

    recovery = GitStateCampaignRecovery(
        repository_root,
        GitIssueEventInbox(repository_root),
        GitStateRef(),
    )
    if not recovery.can_dispatch_deployment(issue_number):
        return DeploymentBridgeReconcileResult(issue_number, ())
    ledger = GitStateRef()
    snapshot = ledger.load(repository_root, issue_number)
    if snapshot is None:
        raise ValueError("deployment bridge requires campaign state")
    gateway = ExistingDeploymentWorkflowGateway(
        repository_root,
        GhWorkflowRunGateway(commands),
    )
    bridge = DeploymentWorkflowBridge(
        gateway=gateway,
        claimer=DeploymentDispatchClaimRecorder(
            ledger,
            repository_root,
            issue_number,
        ),
    )
    planned = tuple(
        record
        for record in snapshot.outbox
        if (
            record.kind == "deployment_workflow_planned"
            and record.generation == snapshot.state.generation
        )
    )
    active = (
        max(
            planned,
            key=lambda record: deployment_workflow_intent(record).attempt,
        )
        if planned
        else None
    )
    results: list[DeploymentBridgeResult] = []
    if (
        active is not None
        and recovery.can_dispatch_deployment(issue_number)
    ):
        result = bridge.apply(active)
        results.append(result)
        if result.result is not None:
            DeploymentWorkflowResultRecorder(ledger).record(
                repository_root,
                issue_number,
                result.result,
            )
    return DeploymentBridgeReconcileResult(
        issue_number,
        tuple(results),
    )


def reconcile_deployment_cleanup_effects(
    repository_root: Path,
    issue_number: int,
    commands: CommandRunner,
) -> DeploymentBridgeReconcileResult:
    snapshot = GitStateRef().load(repository_root, issue_number)
    if snapshot is None:
        return DeploymentBridgeReconcileResult(issue_number, ())
    cleanup_kinds = {kind.value for kind in DeploymentCleanupKind}
    eligible_records = tuple(
        record
        for record in snapshot.outbox
        if (
            record.generation <= snapshot.state.generation
            and record.payload.get("effect_id") == record.record_id
            and record.kind in cleanup_kinds
        )
    )
    cleanup_generation = snapshot.state.generation
    if (
        snapshot.state.phase is CampaignPhase.COMPLETED
        and eligible_records
    ):
        cleanup_generation = max(
            record.generation for record in eligible_records
        )
    cleanup_records = tuple(
        record
        for record in eligible_records
        if record.generation == cleanup_generation
    )
    if cleanup_records:
        cleanup_gateway = GhDeploymentCleanupGateway(
            commands,
            repository_root,
            _repository_name(commands, repository_root),
            issue_number,
        )
        cleanup_bridge = DeploymentCleanupBridge(cleanup_gateway)
        cleanup_results = tuple(
            cleanup_bridge.apply(record) for record in cleanup_records
        )
    else:
        cleanup_results = ()
    return DeploymentBridgeReconcileResult(
        issue_number,
        (),
        cleanup_results,
    )


def record_deployment_publication_file(
    repository_root: Path,
    issue_number: int,
    result_file: Path,
    *,
    commands: CommandRunner | None = None,
) -> DeploymentPublishedVerification:
    root = repository_root.resolve()
    path = (root / result_file).resolve()
    if not path.is_relative_to(root):
        raise ValueError("deployment result file must be repository-relative")
    try:
        if path.stat().st_size > 100_000:
            raise ValueError("deployment result file is too large")
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("deployment result file is invalid") from error
    if not isinstance(document, dict):
        raise ValueError("deployment result file must contain an object")
    expected = {
        "bundle_sha256",
        "deployment_version",
        "effect_id",
        "lineage_sha256",
        "merge_commit",
        "metadata_sha256",
        "portal_url",
        "run_actor",
        "run_id",
        "run_url",
        "source_sha256",
        "tree_sha",
    }
    if set(document) != expected:
        raise ValueError("deployment result fields are invalid")
    ledger = GitStateRef()
    snapshot = ledger.load(root, issue_number)
    if snapshot is None:
        raise ValueError("deployment publication requires campaign state")
    planned = tuple(
        record
        for record in snapshot.outbox
        if record.record_id == document["effect_id"]
    )
    if len(planned) != 1:
        raise ValueError("deployment result intent is unavailable")
    intent = deployment_workflow_intent(planned[0])
    if commands is not None:
        verify_deployment_workflow_run(
            commands,
            root,
            repository=intent.workflow.repository,
            run_id=int(document["run_id"]),
            run_url=str(document["run_url"]),
            run_actor=str(document["run_actor"]),
            effect_id=intent.effect_id,
            expected_actor=intent.workflow.actor,
            merge_commit=intent.binding.merge_commit,
            workflow_id=intent.workflow.workflow_id,
            workflow_path=intent.workflow.path,
            workflow_ref=intent.workflow.ref,
            workflow_trigger=intent.workflow.trigger.value,
        )
    workflow_result = _workflow_result(document, intent)
    DeploymentWorkflowResultRecorder(ledger).record(
        root,
        issue_number,
        workflow_result,
    )
    verification = DeploymentPublishedVerification(
        DeploymentPublicationStatus.VERIFIED,
        intent,
        workflow_result,
        deployment_version=int(document["deployment_version"]),
        source_sha256=str(document["source_sha256"]),
        tree_sha=str(document["tree_sha"]),
        bundle_sha256=str(document["bundle_sha256"]),
        merge_commit=str(document["merge_commit"]),
        lineage_sha256=str(document["lineage_sha256"]),
        metadata_sha256=str(document["metadata_sha256"]),
        portal_url=str(document["portal_url"]),
    )
    verification.require_exact_lineage()
    DeploymentPublicationResultRecorder(ledger).record(
        root,
        issue_number,
        verification,
    )
    return verification


def record_deployment_publication_file_for_effect(
    repository_root: Path,
    result_file: Path,
    commands: CommandRunner,
    *,
    expected_run_id: int,
) -> tuple[int, DeploymentPublishedVerification]:
    root = repository_root.resolve()
    path = (root / result_file).resolve()
    if not path.is_relative_to(root):
        raise ValueError(
            "deployment result file must be repository-relative"
        )
    try:
        if path.stat().st_size > 100_000:
            raise ValueError("deployment result file is too large")
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("deployment result file is invalid") from error
    effect_id = (
        document.get("effect_id")
        if isinstance(document, dict)
        else None
    )
    run_id = (
        document.get("run_id")
        if isinstance(document, dict)
        else None
    )
    if (
        not isinstance(effect_id, str)
        or not effect_id
        or type(expected_run_id) is not int
        or expected_run_id < 1
        or run_id != expected_run_id
    ):
        raise ValueError("deployment result effect ID is invalid")
    from foundry_opt.orchestration.issue_intake import GitIssueEventInbox

    ledger = GitStateRef()
    matches: list[int] = []
    for issue_number in GitIssueEventInbox(root).issue_numbers():
        snapshot = ledger.load(root, issue_number)
        if snapshot is None:
            continue
        if any(
            record.record_id == effect_id
            and record.kind == "deployment_workflow_planned"
            and record.generation == snapshot.state.generation
            for record in snapshot.outbox
        ):
            matches.append(issue_number)
    if len(matches) != 1:
        raise ValueError(
            "deployment result does not match one tracked active intent"
        )
    issue_number = matches[0]
    return (
        issue_number,
        record_deployment_publication_file(
            root,
            issue_number,
            path,
            commands=commands,
        ),
    )


def require_deployment_identity(client_id: str | None) -> None:
    if client_id != DEPLOYMENT_OIDC_CLIENT_ID:
        raise ValueError("deployment OIDC identity is not active")


def verify_active_deployment_identity(
    commands: CommandRunner,
    repository_root: Path,
    environment: Mapping[str, str],
) -> None:
    require_deployment_identity(environment.get("AZURE_CLIENT_ID"))
    expected_tenant = environment.get("AZURE_TENANT_ID")
    expected_subscription = environment.get("AZURE_SUBSCRIPTION_ID")
    if not expected_tenant or not expected_subscription:
        raise ValueError("deployment Azure account scope is unavailable")
    try:
        account = json.loads(
            commands.run(
                (
                    "az",
                    "account",
                    "show",
                    "--query",
                    "{tenant:tenantId,subscription:id,userName:user.name,"
                    "userType:user.type}",
                    "-o",
                    "json",
                ),
                cwd=repository_root,
            ).stdout
        )
    except Exception as error:
        raise ValueError(
            "active Azure principal could not be verified"
        ) from error
    if (
        not isinstance(account, dict)
        or account.get("tenant") != expected_tenant
        or account.get("subscription") != expected_subscription
        or account.get("userName") != DEPLOYMENT_OIDC_CLIENT_ID
        or str(account.get("userType", "")).casefold()
        != "serviceprincipal"
    ):
        raise ValueError(
            "active Azure principal is not the deployment identity"
        )


def verify_deployment_workflow_run(
    commands: CommandRunner,
    repository_root: Path,
    *,
    repository: str,
    run_id: int,
    run_url: str,
    run_actor: str,
    effect_id: str,
    expected_actor: str,
    merge_commit: str,
    workflow_id: int,
    workflow_path: Path,
    workflow_ref: str,
    workflow_trigger: str,
) -> None:
    branch = (
        workflow_ref.removeprefix("refs/heads/")
        if workflow_ref.startswith("refs/heads/")
        else ""
    )
    required_actor = (
        "github-actions[bot]"
        if expected_actor == "workflow-dispatch"
        else expected_actor
    )
    if (
        re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/"
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}",
            repository,
        )
        is None
        or type(run_id) is not int
        or run_id < 1
        or not run_url
        or not run_actor
        or not effect_id
        or not expected_actor
        or re.fullmatch(r"[0-9a-f]{40}", merge_commit) is None
        or type(workflow_id) is not int
        or workflow_id < 1
        or workflow_path.is_absolute()
        or not workflow_path.parts
        or not branch
        or workflow_trigger not in {"manual", "merge"}
    ):
        raise ValueError("deployment workflow result identity is invalid")
    try:
        run = json.loads(
            commands.run(
                (
                    "gh",
                    "api",
                    f"repos/{repository}/actions/runs/{run_id}",
                ),
                cwd=repository_root,
            ).stdout
        )
    except Exception as error:
        raise ValueError(
            "deployment workflow run could not be verified"
        ) from error
    actor = run.get("actor") if isinstance(run, dict) else None
    run_repository = (
        run.get("repository") if isinstance(run, dict) else None
    )
    if (
        not isinstance(run, dict)
        or run.get("id") != run_id
        or run.get("html_url") != run_url
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or run.get("run_attempt") != 1
        or run.get("workflow_id") != workflow_id
        or run.get("path") != workflow_path.as_posix()
        or run.get("head_branch") != branch
        or not isinstance(actor, dict)
        or actor.get("login") != run_actor
        or run_actor != required_actor
        or not isinstance(run_repository, dict)
        or run_repository.get("full_name") != repository
        or (
            workflow_trigger == "manual"
            and (
                run.get("event") != "workflow_dispatch"
                or run.get("display_title") != effect_id
            )
        )
        or (
            workflow_trigger == "merge"
            and (
                run.get("event") not in {"push", "workflow_run"}
                or run.get("head_sha") != merge_commit
            )
        )
    ):
        raise ValueError(
            "deployment workflow run does not match the persisted result"
        )


def deployment_bridge_issue_numbers(
    *,
    requested_issue: str | None,
    state_ref: str | None,
    tracked: tuple[int, ...],
) -> tuple[int, ...]:
    if requested_issue:
        if re.fullmatch(r"[1-9][0-9]*", requested_issue) is None:
            raise ValueError("deployment bridge issue number is invalid")
        issue_number = int(requested_issue)
        if issue_number not in tracked:
            raise ValueError(
                "deployment bridge issue number is not tracked"
            )
        return (issue_number,)
    if state_ref:
        match = re.fullmatch(
            r"foundry-opt/state/issue-([1-9][0-9]*)",
            state_ref,
        )
        if match is not None:
            issue_number = int(match.group(1))
            if issue_number not in tracked:
                raise ValueError(
                    "deployment bridge issue number is not tracked"
                )
            return (issue_number,)
    if any(type(number) is not int or number < 1 for number in tracked):
        raise ValueError("tracked issue number is invalid")
    return tuple(sorted(set(tracked)))


def main() -> None:
    root = Path.cwd()
    from foundry_opt.orchestration.issue_intake import GitIssueEventInbox

    inbox = GitIssueEventInbox(root)
    issues = deployment_bridge_issue_numbers(
        requested_issue=os.environ.get("REQUESTED_ISSUE"),
        state_ref=os.environ.get("TRUSTED_STATE_REF"),
        tracked=inbox.issue_numbers(),
    )
    commands = SubprocessCommandRunner()
    verify_active_deployment_identity(commands, root, os.environ)
    ledger = GitStateRef()
    for issue_number in issues:
        snapshot = ledger.load(root, issue_number)
        if snapshot is None:
            continue
        if not any(
            record.kind == "deployment_workflow_planned"
            and record.generation == snapshot.state.generation
            for record in snapshot.outbox
        ):
            continue
        reconcile_deployment_workflow_effects(
            root,
            issue_number,
            commands,
        )


def _workflow_result(
    document: Mapping[str, Any],
    intent: Any,
) -> DeploymentWorkflowResult:
    return DeploymentWorkflowResult(
        effect_id=intent.effect_id,
        result_id=f"deployment-run-{int(document['run_id'])}-success",
        attempt=intent.attempt,
        binding=intent.binding,
        workflow=intent.workflow,
        run_id=int(document["run_id"]),
        run_url=str(document["run_url"]),
        state=DeploymentWorkflowRunState.SUCCESS,
        conclusion="success",
        run_actor=str(document["run_actor"]),
    )


def _repository_name(
    commands: CommandRunner,
    repository_root: Path,
) -> str:
    remote = commands.run(
        ("git", "remote", "get-url", "origin"),
        cwd=repository_root,
    ).stdout.strip()
    repository = github_repository_from_remote_url(remote)
    if repository is None:
        raise ValueError("origin is not a supported GitHub repository")
    return repository


def _cleanup_body(effect: DeploymentCleanupEffect) -> str:
    marker = f"<!-- foundry-opt:deployment-effect:{effect.effect_id} -->"
    if effect.kind is DeploymentCleanupKind.ROOT_COMMENT_FINAL:
        def metrics(field: str) -> str:
            values = effect.metadata.get(field, {})
            return (
                ", ".join(
                    f"{name}={value}"
                    for name, value in sorted(values.items())
                )
                if isinstance(values, Mapping)
                else ""
            )

        return "\n".join(
            (
                marker,
                "Deployment and held-out retained-improvement verification "
                "completed.",
                f"Candidate: `{effect.candidate_id}`",
                f"Spec SHA-256: `{effect.metadata.get('spec_sha256')}`",
                f"Merge commit: `{effect.metadata.get('merge_commit')}`",
                f"Tree: `{effect.metadata.get('tree_sha')}`",
                f"Patch SHA-256: `{effect.metadata.get('patch_sha256')}`",
                f"Bundle SHA-256: `{effect.metadata.get('bundle_sha256')}`",
                f"Evidence SHA-256: `{effect.metadata.get('evidence_sha256')}`",
                f"Workflow run: {effect.metadata.get('run_url')}",
                f"Foundry portal: {effect.metadata.get('portal_url')}",
                "Deployment version: "
                f"`{effect.metadata.get('deployment_version')}`",
                f"Lineage SHA-256: `{effect.metadata.get('lineage_sha256')}`",
                f"Baseline aggregates: {metrics('baseline_metrics')}",
                f"Selected draft aggregates: {metrics('draft_metrics')}",
                f"Deployed aggregates: {metrics('deployed_metrics')}",
                "",
            )
        )
    return "\n".join(
        (
            marker,
            f"Deployment cleanup: `{effect.reason}`.",
            "",
        )
    )


if __name__ == "__main__":
    main()
