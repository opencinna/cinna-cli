---
description: Create or update cinna-cli feature documentation (business / tech / acceptance).
---

## User Input

```text
$ARGUMENTS
```

Feature name, description, or existing doc path to create/refactor.

## Task

Create or refactor feature documentation under `docs/features/` following the
layered structure below. cinna-cli is a **Python CLI** (source in `src/cinna/`,
tests in `tests/`) that drives local agent development against a remote platform
API — there is no frontend/backend split to document. Keep that in mind: file
references point into `src/cinna/…` and `tests/…`, not `backend/`/`frontend/`.

**If given an existing doc path** — refactor it into the correct structure (split
into business / tech / acceptance files as needed, move under
`docs/features/{feature}/`).
**If given a feature name** — create new documentation by exploring the codebase
first (`src/cinna/`, the `cinna <group>` command surface in `src/cinna/main.py`,
relevant tests).

## Documentation Architecture

### Layer 1 — Project index (`docs/README.md`)

The single concise entry point: project purpose, glossary, architecture, command
surface, and a **Feature Registry** linking each documented feature to its folder.
When you create/update a feature doc, add/update its row in the registry.

### Layer 2 — Feature folders (`docs/features/{feature}/`)

```
docs/
├── README.md                          # Layer 1 — project index + feature registry
└── features/
    └── {feature}/                     # one folder per feature (snake_case)
        ├── {feature}.md               # Layer 3a — business logic / reasoning (required)
        ├── {feature}_tech.md          # Layer 3b — implementation, file refs (optional)
        └── {feature}_acceptance.md    # Layer 3c — real-usage e2e scenarios (optional)
```

Feature-folder naming: snake_case, descriptive (`git_versioning`, `live_sync`,
`remote_exec`, `account_workspace`, `remote_chat`).

### Layer 3 — Feature documentation files

#### 3a. Business logic: `{feature}.md`

Explains **WHAT** the feature does and **WHY**, from a product/user perspective.
A reader should understand purpose, flows, and rules without reading code.

Required sections:
1. **Purpose** — 1–2 sentences: what it does for the user.
2. **Mental model / core concepts** — key terms and the model the user must hold
   (for cinna-cli, almost always: how local files, the remote container, and any
   external system relate; what is local vs remote vs on-demand).
3. **User flows** — numbered steps for the main ways users interact (commands run,
   what happens, what they see).
4. **Business rules** — constraints, state/lifecycle rules, guardrails, failure
   semantics (what is *fail-loud*, what is auto vs manual, direction guards…).
5. **Architecture overview** — a simple text diagram of the component flow, e.g.
   `cinna git <verb> → git_versioning.py → real git → remote`.
6. **Integration points** — how it connects to other features (link their docs).

Style: concise bullets, no code blocks, focus on behavior and reasoning.

#### 3b. Technical details: `{feature}_tech.md`

Deep-dive for developers: **HOW** it's implemented, with file references.

Required sections:
1. **File locations** — every `src/cinna/…` module + the tests that cover it.
2. **Command surface** — `cinna <group> <verb>` → the function in
   `src/cinna/main.py` that implements it, one line each.
3. **Key functions & flow** — module path + function names with a brief purpose
   (e.g. `src/cinna/git_versioning.py:link()` — the link sequence).
4. **Config & registry** — fields written to `.cinna/config.json` and
   `~/.cinna/agents.json` (the latter referenced as a path, it's runtime state).
5. **External contracts** — platform API endpoints consumed, and any external
   tool invoked (git, mutagen) with the exact invariants relied on.
6. **Edge cases & guardrails** — the non-obvious behaviors a maintainer must
   preserve, each tied to the code that enforces it.

Style: heavy use of `src/cinna/file.py:function()` references. <!-- nocheck --> **No
code blocks** — only file/function references.

#### 3c. Acceptance scenarios: `{feature}_acceptance.md` — **the e2e test catalog**

The catalog of **real-usage scenarios** a testing agent executes against a *live*
environment (a real backend + real container + real external systems) — **not**
unit tests. This is what catches the bugs unit tests miss: conflicting pushes,
multi-agent layout collisions, registry drift across commands, tarball/commit
divergence, stale state across command sequences.

Write it so an autonomous agent can pick it up and run each scenario end-to-end.

Required sections:
1. **Preconditions** — what the agent needs: a live platform URL, an account
   workspace or setup token, at least one (ideally two) agents configured for the
   feature, any external accounts/credentials, and the editable install
   (`which cinna` → repo `src/cinna`).
2. **Scenario catalog** — a numbered list. Each scenario has:
   - **Goal** — the real user intent being exercised.
   - **Setup** — starting state.
   - **Steps** — the exact `cinna …` (and supporting `git`/`cinna exec`) commands.
   - **Expected** — observable outcome (output text, file/remote/registry/env
     state). Be specific enough to assert.
   - **Watch for** — the failure modes / regressions this scenario is designed to
     surface (the "why this scenario exists").
3. **Cross-cutting invariants** — properties that must hold across *all* scenarios
   (e.g. credentials never committed; a command that re-writes the registry must
   not drop another command's state; fail-loud, never silent-clobber).
4. **Cleanup** — how to leave the live environment tidy afterward.

Style: imperative, runnable. Real commands in fenced blocks are encouraged here
(unlike the other layers). Prefer scenarios grounded in actual runs — when a real
test discovers a defect, **add the scenario that reproduces it** so it becomes a
permanent regression check.

### Minimal documentation

Not every feature needs all three files. Start with `{feature}.md`. Add `_tech.md`
when implementation detail would clutter the business doc or spans many modules.
Add `_acceptance.md` when the feature has **real-environment behavior worth
exercising e2e** (anything touching sync, git, the container, schedules, or
multi-agent/multi-command state — i.e. most non-trivial cinna-cli features).

## Style rules

**DO**
- Use file refs: `src/cinna/git_versioning.py:link()`, `tests/test_git_versioning.py`
- Use command refs: `cinna git push` — push the agent branch (ff-only)
- Use endpoint refs: `GET /api/v1/cli/git-coordinates`
- Link related docs: `See [Live Sync](../live_sync/live_sync.md)` <!-- nocheck -->
- Concise bullets; simple text architecture diagrams
- In `_acceptance.md`, write real runnable commands

**DON'T**
- Put code snippets in `{feature}.md` / `{feature}_tech.md`
- Duplicate content between the business and tech files
- Reference container/home paths (`/app/workspace/…`, `~/.cinna/…`) as if repo
  files — they are illustrative; the checker skips them, but be intentional

## Process

1. **Scope** — new feature doc or refactor of an existing one?
2. **Folder** — map to `docs/features/{feature}/`.
3. **Explore** — read the relevant `src/cinna/` modules, the `cinna` command
   surface, and the covering tests.
4. **Write** — always `{feature}.md`; add `_tech.md` / `_acceptance.md` per the
   rules above.
5. **Update `docs/README.md`** — add/refresh the Feature Registry entry.
6. **Validate references** — run the checker on the files you touched:
   ```
   python3 scripts/check_docs_references.py --files docs/features/{feature}/{feature}.md docs/features/{feature}/{feature}_tech.md docs/features/{feature}/{feature}_acceptance.md
   ```
   Fix every broken reference before finishing. For *illustrative* paths that are
   not real repo files (container/home/convention paths), append `<!-- nocheck -->`
   to that line.
7. **Run the test suite** — `pytest -q` must stay green (the docs describe shipped
   behavior; if a doc claims behavior the tests don't cover, add or point to the
   test).

## Output

Write to `docs/features/{feature}/`. Report what was created/updated, any old
files replaced (don't delete automatically — report them), the reference-check
result, and the test-suite status.
