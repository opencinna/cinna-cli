# Git Versioning — Acceptance Scenarios (live e2e)

The catalog of **real-usage scenarios** for an agent doing *integration* testing
of `cinna git` against a **live** environment — a real platform backend, real
agent containers, and a real external git remote. These are not unit tests; they
exist to catch what unit tests miss: tarball/commit divergence, conflicting
pushes, multi-agent layout collisions, and state drift across command sequences.
Several scenarios below were authored *because* a real run found a defect — run
them as permanent regression checks.

How to use: pick scenarios relevant to the change, run the **Steps** verbatim
against a live env, assert the **Expected**, and watch for the **Watch for**
failure modes. Prefer running the multi-agent scenarios (8–12) on any change to
linking, layout, the registry, or push/pull — that's where the subtle bugs live.

## Preconditions

- A reachable platform (e.g. `http://localhost:8000` backend, `:5173` frontend)
  with an account workspace already set up (`cinna account setup …`), or a setup
  token for `cinna setup`.
- **Editable install** of the CLI under test:
  `python3 -c "import cinna,os;print(os.path.dirname(cinna.__file__))"` must point
  at this repo's `src/cinna`. Confirm `which cinna` resolves and `cinna git --help`
  lists the verbs.
- **At least one** git-versioned agent; ideally **two**, including a pair in the
  **same repo, different subdirs** (the highest-value case).
- The developer's own git/SSH credential with push access to the agents' remote
  (`auth_hint` says which). The platform deploy key is never used locally.
- `git` and `mutagen` on `PATH`.

> Run `cinna` commands from inside the agent's workspace dir, or from the account
> root with `--agent <slug>`. The agent dir can be **deep** (a multi-segment
> backend subdir nests it several levels under `agents/<slug>/`).

## Scenario catalog

### 1. Fresh sync auto-links a git-versioned agent

- **Goal:** a developer checks out a git-versioned agent and immediately has a
  working tree.
- **Setup:** an agent with Git Versioning enabled on the platform, not yet synced.
- **Steps:**
  ```
  cinna agent sync "<Agent Name>"          # or: cinna setup <token>
  ```
- **Expected:** output reports `Git-versioned: linked to <repo_url> (branch <ref>)`
  and an `Agent synced under agents/<slug>/<subdir>/` path. The clone root holds
  `.git`; `git -C <clone> remote get-url origin` = the repo, `symbolic-ref HEAD` =
  `<ref>`, upstream = `origin/<ref>`, `sparse-checkout list` = the agent subdir.
- **Watch for:** link silently skipped; clone created but no `origin`/upstream;
  `subdir` guessed as the slug instead of the backend's real (multi-segment) one.

### 2. `cinna git status` reflects link state

- **Goal:** the developer can tell whether the agent is versioned and linked.
- **Steps:** `cinna git status` (in-dir) and `cinna git status --agent <slug>`.
- **Expected:** for a linked agent, `Linked: <repo_url> (branch <ref>)`, the
  working-tree path, the subdir, and the direction. For a non-versioned agent,
  `not git-versioned`. For versioned-but-not-linked, a hint to run `cinna git link`.
- **Watch for:** `--agent` failing to resolve a **deeply nested** agent dir
  (multi-segment subdir) — was bug #1.

### 3. Link leaves a clean working tree (no phantom deletions)

- **Goal:** a fresh link doesn't present the repo as half-deleted.
- **Steps:**
  ```
  cinna git link
  git -C <clone> status --porcelain | awk '{print $1}' | sort | uniq -c
  ```
- **Expected:** **no `D` (deleted) entries.** Versioned `workspace/plugins/**`,
  empty-dir `.gitkeep` markers, and repo-root shared files (`README.md`, top-level
  `.gitignore`) are all present on disk. Only genuine in-flight edits (if any)
  show as `M`/`??`.
- **Watch for (bug #2 / #6):** dozens/hundreds of `D` lines — the workspace tarball
  is a subset of the committed tree and link failed to restore them. A later
  `cinna git commit` would then **delete** plugins/root files from the repo. Also
  watch a *permanently dirty* tree (deleted `README.md`/`.gitignore`) that blocks
  `git pull --rebase`.

### 4. Edit → sync → run in the agent-env

- **Goal:** a local code edit reaches the running container and executes.
- **Steps:**
  ```
  printf 'print("marker-v1")\n' > <ws>/workspace/scripts/smoke.py
  cinna sync push --agent <slug>
  cinna exec --agent <slug> python workspace/scripts/smoke.py
  ```
- **Expected:** exec prints `marker-v1`. (Exec cwd is `/app`; the workspace is at
  `/app/workspace`.)
- **Watch for:** the file not syncing (still parked behind conflicts); exec
  quoting pitfalls — pass the command as separate tokens, not one quoted string.

### 5. Commit → push → verify on the real remote

- **Goal:** durable preservation in the external repo.
- **Steps:**
  ```
  cinna git commit -m "test: add smoke script"
  cinna git push
  git -C <clone> fetch -q origin <ref>
  git -C <clone> cat-file -e origin/<ref>:<subdir>/workspace/scripts/smoke.py
  ```
- **Expected:** commit is clean-scoped (only the new file in `git status` before
  commit), push succeeds, and the file exists on `origin/<ref>` with the right
  content. Output also nudges the user to click "Pull" in the UI / rely on the
  webhook to update the running agent.
- **Watch for:** the commit staging unrelated mode-only diffs or restored plugins;
  `.cinna/` or `credentials/` getting staged (token leak — see #7).

### 6. Checkout/reload runs a past version live, no commit

- **Goal:** roll the running env back to a prior version for debugging.
- **Steps:** commit a `v2`, then
  ```
  cinna git checkout HEAD~1 --reload
  cinna exec --agent <slug> python workspace/scripts/smoke.py
  ```
- **Expected:** local `smoke.py` reverts to `v1` content **uncommitted**; the exec
  prints `marker-v1` (the env now runs the old version). HEAD is unchanged.
- **Watch for:** `--reload` not flushing; the manifest claim — `--manifest` notes
  it can't be reloaded via Mutagen.

### 7. Secrets / generated files are never committable

- **Goal:** the token and generated guides never reach the repo.
- **Steps:**
  ```
  git -C <clone> check-ignore <subdir>/.cinna/config.json
  git -C <clone> check-ignore <subdir>/CLAUDE.md
  git -C <clone> check-ignore <subdir>/REST_API_BUILDING.md   # a generated prompt-ref guide
  git -C <clone> ls-files | grep -E '\.cinna/|CLAUDE\.md' || echo "none tracked OK"
  ```
- **Expected:** each `check-ignore` exits 0 (ignored); nothing under `.cinna/` or a
  generated guide is tracked.
- **Watch for (bug #3):** a *newer* generated prompt-ref guide (e.g.
  `REST_API_BUILDING.md`) not excluded because it wasn't in a static list — the
  excludes must be derived from the actually-synced guides.

### 8. Two agents in different dirs are independent

- **Goal:** checking out two git-versioned agents doesn't make them interfere.
- **Setup:** sync a second git-versioned agent (`cinna agent sync <id|name>`).
- **Steps:** inspect both clones.
- **Expected:** two separate `.git` dirs; each `sparse-checkout list` is only its
  own subdir; neither clone materializes the other's subdir on disk; the registry
  has a distinct entry (clone_path/subdir) per agent; `cinna git status --agent X`
  vs `--agent Y` resolve to different working trees.
- **Watch for:** a shared/overwritten clone; `--agent` returning the wrong agent;
  registry entries colliding.

### 9. Same-repo cross-push preserves both agents

- **Goal:** two agents in the **same repo, different subdirs** don't clobber each
  other on the shared branch.
- **Steps:** add+commit+push a file in agent A's subdir, then in agent B's subdir
  (B was linked at or after A's push).
- **Expected:** `origin/<ref>` contains **both** subdirs' files; B's push did not
  remove A's file.
- **Watch for:** B's push (built from B's sparse tree) dropping A's subdir content.

### 10. Two-writer fast-forward-only is fail-loud, with clean recovery

- **Goal:** the second pusher can't silently clobber; recovery is a clean rebase.
- **Steps:**
  ```
  # agent A: commit locally (no push)
  # agent B: commit + push   (advances the shared branch)
  # agent A:
  cinna git push        # expect REJECTED
  cinna git pull        # rebase A's commit onto B's
  cinna git push        # now succeeds
  ```
- **Expected:** A's first push is **rejected** with a `git pull --rebase`
  instruction (no force). After pull (a clean rebase — disjoint subdirs) the push
  succeeds; `origin/<ref>` history shows A's commit on top of B's, both files
  present.
- **Watch for:** a force-push path; the rebase failing on a *dirty tree* — a tree
  left dirty by phantom deletions (#3/#6) blocks `git pull --rebase` with "Please
  commit or stash them" even though there's no real conflict.

### 11. The registry git block survives credential-rewriting commands

- **Goal:** routine sync commands don't strip git coordinates from the registry.
- **Steps:**
  ```
  # confirm linked agent has a git block in ~/.cinna/agents.json
  cinna sync push --agent <slug>     # re-writes credentials
  # re-check the registry git block
  ```
- **Expected:** the agent's `git` block in the registry is **unchanged** after the
  sync op.
- **Watch for (bug #5):** the block disappearing — sync/`dev` re-upserted creds
  and dropped git. (`upsert_agent_registry` must preserve when `git` is omitted.)

### 12. File-mode changes are not tracked

- **Goal:** the executable bit (dropped by the tarball / normalized by Mutagen)
  doesn't create spurious diffs.
- **Steps:** `git -C <clone> config core.fileMode` (expect `false`); `chmod +x` a
  content-clean tracked file and re-check `cinna git status`.
- **Expected:** `core.fileMode` is `false`; the chmod'd file does **not** appear as
  modified.
- **Watch for (bug #4):** `.sh`/`.py` files showing as `M` with empty content diffs
  (`old mode 100755 / new mode 100644`).

### 13. Plain git / IDE works once linked

- **Goal:** the developer can use VS Code's Source Control or terminal `git`
  directly.
- **Steps:** from the agent subdir, `git rev-parse --show-toplevel` (resolves to
  the clone root), edit a file, `git add .` / `git commit` / `git push` (bare).
- **Expected:** bare `git push` works (upstream configured); `git add` does not
  stage `.cinna/`.
- **Watch for:** needing manual `git remote add` / `--set-upstream`; `git add -A`
  from the clone root staging another agent's subdir (in a multi-agent clone use
  `git add .` or `cinna git commit`).

### 14. Slug collision bumps the clone-root name

- **Goal:** two agents whose names slugify the same don't collide on disk.
- **Steps:** sync two different agents that normalize to the same slug.
- **Expected:** the second checks out under `agents/<slug>-<shorthash>/`; re-running
  sync for the *same* agent reports "already a synced workspace".
- **Watch for:** the second sync overwriting the first; or a same-agent re-sync
  silently creating a duplicate.

### 15. Direction guard + legacy-flat refusal + unlink

- **Goal:** the safety rails fire.
- **Steps / Expected:**
  - For a `sync_direction=pull` agent, `cinna git push` is refused with a
    pull-only message.
  - For a pre-Model-A *flat* workspace that became git-versioned, `cinna git link`
    prints a disconnect + re-sync instruction (no auto-convert).
  - `cinna git unlink` stops the helpers and drops the registry git block but
    **leaves `.git` and history**; `cinna git link` re-enables.

## Cross-cutting invariants (must hold across all scenarios)

- **No secret ever staged/committed** — `.cinna/`, `credentials/`, `app-data/`,
  logs, databases never appear in `git status`/`ls-files` as tracked.
- **No silent clobber** — every rejected push is fail-loud with recovery guidance;
  the CLI never force-pushes.
- **No state drift between commands** — a command that re-writes the registry or
  config must not drop another concern's state (e.g. sync must not wipe the git
  block; link must not break Mutagen wiring).
- **Repo stays intact on link** — link never turns committed files into pending
  deletions; the working tree is clean except genuine in-flight edits.
- **Layout matches the remote** — the agent dir mirrors `<repo>/<subdir>/`; enabling
  git on a Model-A folder needs no re-download or file move.

## Cleanup

- Remove test files via a normal revert commit + push (never force / history
  rewrite on a shared repo): delete the smoke/marker files, `cinna git commit`,
  `cinna git push`.
- `cinna agent unsync <slug>` (or `cinna disconnect` in-dir) to drop local
  checkouts; this keeps `.git` unless you delete the clone dir.
- Remove any test files left in the agent-env via `cinna exec --agent <slug> rm …`.
- Verify the registry has no leftover/bogus entries
  (`~/.cinna/agents.json`) — especially if any helper script was run **outside**
  pytest's global-state isolation (it would write the real registry).
