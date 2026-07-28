# Foundry adapter mapping for the Tenzing improvement loop

The vendored protocol in `references/tenzing/` (see `references/tenzing/UPSTREAM.md` for exact
provenance) describes a domain-agnostic, human-supervised loop: define an objective, an editable
area, and an evaluation recipe once via `INIT.md`; then run `climb.md` forever, branching one git
branch per experiment, scoring it, and logging the outcome to a scoreboard until a termination
condition is met.

This repository does not run that loop as upstream wrote it. `foundry-opt` already enforces a
stricter, machine-checked version of the same shape — issue-approved specs instead of an interview,
ephemeral worktrees instead of long-lived branches, Foundry agent drafts instead of ad hoc "produce a
run", and redacted, hashed evidence instead of a free-form scoreboard. This document is the
**normative mapping** between the two: read it alongside the unmodified snapshot to understand how
each vendored concept is realized by this codebase. It is deliberately kept **outside**
`references/tenzing/` so it can be revised as the adapter evolves without ever touching the vendored
upstream files.

## The three agents

`../SKILL.md` directs three agents, each with a narrow, non-overlapping responsibility. This mapping
document is written from their point of view:

- **Specification planner** — turns an approved GitHub issue into an **immutable spec PR**:
  `OptimizationIssueRequest` is filed, then hashed and recorded as `OptimizationSpec` /
  `OptimizationSpecApproval` (`foundry_opt/optimization/models.py`, `approve_optimization_spec`,
  `OptimizationSpec.sha256`). It does **not** brainstorm candidate ideas — it only fixes the
  objective, metrics, datasets/evaluators, and allowed mutations before any candidate work exists.
- **Optimization runner** — reads the already-*approved* spec and performs the Tenzing-style idea
  generation step (`CandidateGenerator.generate`, `foundry_opt/campaign/protocols.py`) plus the
  bounded experiments that implement and evaluate each idea (`foundry_opt/campaign/engine.py:
  run_campaign`), time-boxed by `CampaignLimits`. This is the only agent that brainstorms or writes
  candidate code.
- **Exact-patch applier** — invokes `PatchApplier.apply_exact` (`ExactPatchRequest` →
  `AppliedPatch`, via `verify_and_apply_candidate` in `foundry_opt/github_workflow/candidate.py`)
  against an already-selected, already-evidenced candidate. It performs **no selection** (the
  Pareto-eligible candidate was already chosen upstream by the optimization runner's evaluation
  pipeline) and **no repair**: if applying the exact patch would require anything beyond a clean,
  tree-verified apply, `verify_and_apply_candidate` rejects it as `substantive_repair` rather than
  attempting to fix it up.

## Mapping table

| Tenzing concept (`references/tenzing/...`) | Foundry adapter realization |
| --- | --- |
| `climb_config/objective.md` — primary metric, direction, soft constraints | Produced by the **specification planner** as an **issue-approved optimization spec**: `OptimizationIssueRequest` is filed against a GitHub issue, then hashed and recorded as `OptimizationSpec` / `OptimizationSpecApproval` (`foundry_opt/optimization/models.py`). The objective, metric policies (`MetricPolicy`), and hard guardrails are fixed and content-hashed (`OptimizationSpec.sha256`) as an immutable spec PR before any candidate work starts — there is no informal "agent interviews you" step, and no candidate brainstorming happens at this stage. |
| `climb_config/dos-and-donts.md` — editable area, read-only files, dependency rule | Also fixed by the **specification planner** in the same **issue-approved optimization spec**: the editable surface is the `allowed_mutations` set (`MutationClass`) and `RestrictedOptIns` on the approved spec / target config, checked by preflight before a campaign runs. The read-only / no-secrets rule is enforced in code (`foundry_opt/security.py: reject_secret_content`), not left to an agent's discipline. |
| `climb_config/data.md` — data inputs, held-out set, leakage rule | **Pinned dataset/evaluator identities, not staged files.** The approved spec records dataset and evaluator identity and content hash as `AssetProvenance` entries (`name`, `version`, `content_sha256`; `foundry_opt/optimization/models.py`) — the optimization runner never receives raw held-out rows to place anywhere. Development-split evaluation results *may* inform the runner's idea generation (per-candidate feedback carries `metrics`, `foundry_opt/campaign/protocols.py: CandidateFeedback`), but held-out validation rows are never written into a candidate's source bundle; they stay outside the packaged `BundleArtifact`/`DraftRequest` entirely and are consumed only by the evaluation adapter (`foundry_opt/adapters/evaluation.py: EvaluationGateway`), which fetches them from Foundry-side dataset resources by name/version to score a submitted draft. |
| Branches — one long-lived git branch per experiment | **Temporary worktrees isolate code experiments only.** Each candidate's code is materialized only inside its own disposable, optimizer-owned `CampaignWorktree` (`branch` prefixed `foundry-opt/`; `foundry_opt/campaign/protocols.py`, `foundry_opt/campaign/worktrees.py: contained_worktree_root`, `require_managed_worktree`) — never on a long-lived branch a human has to clean up, and never holding dataset content (see the row above). A candidate escapes its worktree only as a `PatchArtifact` (`export_patch`) plus its `BundleArtifact`/draft — exact, reviewable code artifacts, not a persistent branch. |
| `climb_config/evaluation.md` — produce a run, score it, read the metric | **Foundry source-ZIP drafts/evals**, executed by the **optimization runner**. "Produce a run" is packaging the worktree's editable-area code into a `BundleArtifact` and submitting it as a `DraftRequest` (`foundry_opt/drafts/models.py`), which Foundry deploys as a real draft agent version. "Score it" is the evaluation adapter (`foundry_opt/adapters/evaluation.py`) running that draft against the spec's pinned datasets/evaluators — pulling development and held-out rows itself rather than reading them from the candidate's worktree — producing an `EvaluationResult` via `foundry_opt/evaluation/funnel.py`. "Read the metric" is `EvaluationResult.metrics`, already normalized against the spec's `MetricPolicy`. |
| `experiment_tracking/results.tsv` + `experiment_metadata/` — scoreboard + per-branch evidence | **Redacted evidence / Pareto results**, computed as part of the **optimization runner**'s pipeline, not by the applier. Instead of a flat `keep`/`discard`/`crash` row per branch, `select_eligible_candidates` (`foundry_opt/evaluation/selection.py`) computes a `ParetoResult` — the non-dominated frontier of candidates that clear every hard guardrail and materially improve the baseline. `EvidenceRequest` / `EvidenceManifest` (`foundry_opt/evidence/`) then write a hashed, **redacted** evidence manifest (`sensitive_values` are scrubbed before anything is written) covering the baseline, every candidate, and the Pareto frontier — the durable, reviewable record a human reads instead of `results.tsv`. The **exact-patch applier** never re-runs or second-guesses this selection; it only applies whichever `PatchArtifact` this pipeline already marked eligible. |
| Termination condition — `forever` / `n-iterations` / `until-target` / `report-each` | **Bounded Copilot sessions**, enforced around the **optimization runner**'s idea-generation-and-experiment loop. Each candidate's implementation session is time-boxed by `CampaignLimits` (`deadline_minutes`, `candidate_cutoff_minutes`; `foundry_opt/campaign/models.py`, enforced in `foundry_opt/campaign/engine.py`). Rather than an open-ended loop a human must remember to stop, every per-candidate Copilot coding-agent session is cut off automatically at `candidate_cutoff_minutes`, and the whole campaign is bounded by `deadline_minutes` — a hard wall-clock backstop instead of a self-reported termination condition. |

## Reading order

1. Skim `references/tenzing/README.md` and `references/tenzing/climb.md` to understand the loop shape
   upstream intends — ideas, one branch per experiment, evaluate, log, repeat.
2. Read this table to translate each of those steps into what actually executes in this repository:
   an immutable, issue-approved spec; pinned dataset/evaluator identities consumed only by the
   evaluation adapter; a disposable, code-only worktree; a Foundry source-ZIP draft and eval; a
   redacted evidence manifest with a Pareto frontier; and a bounded Copilot session per candidate.
3. See `../SKILL.md` for how the specification planner, optimization runner, and exact-patch applier
   in a generated customer repository are directed to use this mapping in practice.

## What this document is not

This is an **adapter mapping**, not a modification of the upstream protocol. The files under
`references/tenzing/` are never edited to reflect this mapping — see
`references/tenzing/UPSTREAM.md` for why the snapshot stays byte-for-byte unchanged and how it is
deliberately upgraded offline. If this mapping and the vendored files ever appear to disagree, this
document is what changes.
