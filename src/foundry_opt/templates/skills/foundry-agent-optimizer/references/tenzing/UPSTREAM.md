# Upstream provenance

**This file is not part of the vendored snapshot.** Every other file in this directory
(`README.md`, `climb.md`, `INIT.md`, `LICENSE`, `.gitignore`, `assets/logo.svg`, and
`climb_config/*.md`) is copied byte-for-byte from upstream Tenzing and must never be edited here.
This file, added by the `foundry-agent-optimizer` skill, records where that snapshot came from and
how it may ever be updated.

## Source

- **Upstream repository:** <https://github.com/coreai-microsoft/tenzing>
- **Exact revision vendored:** `7300a83fc7378f0f1a401dbdf8ed28358ccf1732`
- **Commit subject:** "Import Tenzing template: autonomous improvement-loop scaffold"

## Author / copyright

Tenzing is copyright its author as recorded in the upstream `LICENSE` file vendored alongside this
document:

```
Copyright (c) 2026 saketsathe
```

## License

Tenzing is distributed under the **MIT License**. The exact, complete upstream license text is
vendored unchanged in `LICENSE` in this directory — that file, not this summary, is the
authoritative license notice. Redistributing this snapshot must keep `LICENSE` intact and
unmodified alongside it, per the MIT License's own terms.

## What "vendored" means here

The files listed above are an exact, read-only copy of the upstream working tree at the revision
above. Nothing has been renamed, reformatted, reflowed, or had its `{{PLACEHOLDER}}` markers filled
in — including the ones that look like they are asking to be filled in (`climb.md`, `INIT.md`,
`climb_config/*.md`). Those placeholders are upstream's own template markers for `INIT.md` to fill
in *inside a generated customer repository*, not something this skill resolves ahead of time. Filling
them in here would silently diverge this snapshot from the reviewed upstream commit.

How Foundry-specific concepts (issue-approved specs, temporary worktrees, source-ZIP drafts, redacted
evidence, bounded Copilot sessions) relate to this unmodified protocol is documented separately in
`../ADAPTER_MAPPING.md`, one directory up — deliberately **outside** this snapshot, so the mapping can
evolve without ever touching vendored upstream content.

## Deliberate offline upgrade model

This snapshot is frozen on purpose. There is no submodule, no build-time fetch, no CI job, and no
runtime code path that reaches out to the upstream repository — once vendored, this directory is as
offline and immutable as any other file checked into this repository. That is a deliberate choice,
not an oversight:

- **Why not track upstream live?** This template supplies literal instructions that autonomous
  coding agents read and follow (`climb.md`, `INIT.md`). An automatic update could silently change
  agent behavior — or, in the worst case, introduce instructions an attacker slipped into upstream —
  without anyone in this repository reviewing the diff. That is unacceptable for content an agent
  executes, in the same way this repository never floats a GitHub Action tag and instead pins every
  action to a full commit SHA (see the `CHECKOUT_ACTION`, `SETUP_PYTHON_ACTION`, `SETUP_UV_ACTION`,
  and `AZURE_LOGIN_ACTION` constants in `foundry_opt/onboarding/generation.py`).
- **How an upgrade actually happens:** a maintainer decides to move to a newer upstream revision,
  clones upstream locally, and diffs it file-by-file against this directory. Each changed file is
  reviewed on its own merits (does it change agent behavior? does it add new placeholders? does it
  change the license?) before anything is copied over. The maintainer then updates the **exact
  revision** recorded above, re-copies the reviewed files byte-for-byte, and re-runs
  `tests/unit/test_tenzing_bundle.py`, which fails loudly if any vendored file stops matching the
  newly reviewed upstream commit exactly, or if the recorded revision/URL/license no longer agree
  with this document.
- **No partial or silent upgrades.** The revision recorded above always names the exact commit this
  directory matches, in full. There is no "latest" tracking branch, floating tag, or version range —
  any upgrade is a fully deliberate, offline, human-reviewed replacement of this entire directory.
