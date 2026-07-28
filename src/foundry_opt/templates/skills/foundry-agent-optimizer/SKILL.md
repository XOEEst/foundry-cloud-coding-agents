---
name: foundry-agent-optimizer
description: >-
  Run a Tenzing-shaped, Foundry-adapted improvement loop for this agent: an immutable,
  issue-approved spec; a bounded optimization runner that brainstorms and evaluates candidates as
  Foundry drafts/evals in code-only worktrees; and a deterministic, exact-patch applier for the
  result a human already reviewed.
---

# Foundry Agent Optimizer

This skill directs three cooperating agents through one optimization campaign for this repository's
Foundry agent. It is built on top of an unmodified, vendored copy of the Tenzing improvement-loop
protocol at `references/tenzing/` — read `references/tenzing/README.md` and
`references/tenzing/climb.md` for the loop shape this skill adapts, and `ADAPTER_MAPPING.md` for
exactly how each Tenzing concept is realized here.

> **Not an upstream Tenzing artifact.** `references/tenzing/` is vendored verbatim from
> <https://github.com/coreai-microsoft/tenzing> (see `references/tenzing/UPSTREAM.md` for the exact
> revision, author/copyright, and license). This `SKILL.md`, `ADAPTER_MAPPING.md`, and
> `UPSTREAM.md` are authored by the `foundry-agent-optimizer` skill, not by the Tenzing project, and
> using them implies no endorsement by, affiliation with, or review from the upstream Tenzing
> authors or maintainers.

## The three agents

### Specification planner

The specification planner turns an approved GitHub issue into an **immutable spec PR** (see
`ADAPTER_MAPPING.md` → *objective* and *do's and don'ts*): it files the goal, metric policies, hard
guardrails, allowed mutations, restricted opt-ins, and the pinned dataset/evaluator identities as an
`OptimizationSpec`, hashes it, and opens it for human approval. **It does not brainstorm candidate
ideas.** Its only job is to fix, in writing and by hash, what "better" means and what is in bounds —
before any candidate exists — so the optimization runner has nothing left to negotiate.

### Optimization runner

The optimization runner reads the *already-approved* spec and is the only agent that generates ideas
or writes candidate code. For each candidate, inside its own disposable, code-only worktree (see
`ADAPTER_MAPPING.md` → *data* and *branches*):

1. Start or resume with `foundry-opt optimize run --issue <number> --json`, then request a candidate
   through `foundry-opt optimize candidate request --issue <number> --json`.
2. Perform the Tenzing-style idea-generation step from `references/tenzing/climb.md`, informed by the
   approved spec and any prior candidates' development-split feedback — never by held-out validation
   rows, which the runner never receives as files.
3. Materialize the idea only inside the returned campaign worktree — never on a long-lived branch
   and never outside `.foundry-optimizer/worktrees/<campaign_id>`. The worktree holds code only; no
   dataset content is staged there.
4. Write the strict idea JSON outside the worktree under the campaign candidate directory and submit
   it with `foundry-opt optimize candidate submit --issue <number> --candidate <id> --idea-file
   <path> --json`. Never place status, metrics, eligibility, or evaluation claims in the idea file.
5. Let the deterministic CLI package the worktree's editable area as a Foundry source-ZIP draft and
   let the
   evaluation adapter score it against the spec's pinned datasets/evaluators (see
   `ADAPTER_MAPPING.md` → *evaluation*) — held-out rows are supplied by the evaluation adapter
   directly, not read from the worktree.
6. Repeat request/edit/submit using the returned development feedback until the CLI directs
   finalization, then run `foundry-opt optimize run --issue <number> --json` again.
7. Respect the campaign's bounded session limits (see `ADAPTER_MAPPING.md` → *termination*): stop the
   candidate's session at its cutoff rather than continuing indefinitely, and never ask a human to
   grant "just a bit more time" mid-session.

Eligibility (the Pareto frontier over hard guardrails and material improvement) is determined
mechanically from this pipeline, not by the runner's own judgment, and is recorded in redacted
evidence (see `ADAPTER_MAPPING.md` → *results*) for the exact-patch applier and a human to act on.

### Exact-patch applier

The exact-patch applier acts only on a candidate the optimization runner's pipeline already marked
Pareto-eligible and evidenced. **It performs no selection** — it never compares candidates or
chooses among the frontier itself — **and no repair**: it invokes a deterministic apply of the
candidate's exact patch and, if that apply would require anything beyond a clean, tree-verified
result, it rejects the candidate outright rather than fixing it up. Deployment, merges, and
production routing changes remain human decisions unless the approved spec's `deployment_mode`
explicitly says otherwise; the applier never widens that policy on its own.

## Guardrails that apply to all three agents

- Never request, store, or emit client secrets, certificates, tokens, connection strings, or
  credential-shaped values; treat `foundry_opt/security.py`'s secret rejection as authoritative.
- Never edit anything under `references/tenzing/` — it is a byte-exact vendored snapshot. Changes to
  how this skill adapts Tenzing belong in `ADAPTER_MAPPING.md`, never in the snapshot itself.
- Never deploy models, broaden permissions, or change production routing outside the issue-approved
  spec's explicit `deployment_mode`.
- Only operate inside the campaign's managed worktree for code; never write candidate content
  directly into the repository's working tree or a persistent branch, and never stage held-out
  dataset rows anywhere outside the evaluation adapter.
- If any step here conflicts with the issue-approved spec, the spec wins — this file directs *how*
  the three agents cooperate, not *what* they are allowed to change.
