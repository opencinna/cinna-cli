# Git Versioning for Agents (`cinna git`)

## Purpose

Let a developer version a locally-synced agent's workspace with **git** against
the agent's own external remote — `commit` / `push` / `pull` / roll back — using
**their own** git/SSH credentials, while the live-sync (Mutagen) loop keeps the
running container mirrored as usual. `cinna git` is a thin, fail-loud wrapper
around real git that handles discovery, working-tree layout, and the
fast-forward-only safety messaging; it does not replace git.

## Mental model — two orthogonal sync layers

A git-versioned agent has **two independent layers on the same folder**, and they
must not be conflated:

- **Mutagen (runtime, always on).** Continuously mirrors `workspace/` to the
  running container at `/app/workspace`. This is what makes edits *live*. Mutagen
  ignores `.git`, so git and Mutagen never fight over the folder.
- **Git (preservation, on demand).** Durably versions the same files against the
  agent's external repo — history, snapshots, rollback. The developer drives it
  with `cinna git`. Git never touches the container; the two layers meet **only**
  at the remote.

Editing a prompt and watching behavior change = Mutagen. Making a durable, named
snapshot you can return to = git. They are orthogonal: you can build all day
without ever committing.

The **deploy key never reaches the CLI.** The backend uses its own host-side
deploy key to push/pull on the server side; the developer authenticates locally
with their own git/SSH. The coordinates endpoint deliberately omits all key
material and only advises which credential type to use (`auth_hint`).

## Core concepts

- **Git-versioned agent** — an agent the platform has connected to an external git
  source. The CLI learns this from the coordinates endpoint (`vcs_enabled`).
- **Coordinates** — `repo_url`, `subdir`, `ref`, `sync_direction`, `auth_hint`,
  `last_synced_commit`. The CLI derives the agent from the per-agent token, so the
  endpoint needs no agent id.
- **Model-A layout** — the local folder mirrors the remote repo: the **agent dir**
  (the folder holding `.cinna/`) *is* the repo's `<subdir>/` node, and its parent
  is the git working tree (the clone root that holds `.git`). For a repo-root
  agent (`subdir` null) the clone root and the agent dir are the same folder.
- **Linked** — the agent dir's clone root is a real git working tree pointed at the
  remote (`.git`, `origin`, sparse-checkout, upstream all configured).
- **Two-writer model** — both the backend (deploy key) and the developer (own
  creds) push the **same ref**, both **fast-forward-only**, no auto-merge.

## Subdir-by-default layout (and why)

Every new checkout — git-versioned or not — is laid out the Model-A way:
`<parent>/<slug>/<subdir>/`, with synced files always under `<subdir>/workspace/`.
`subdir` defaults to the agent slug, but when the agent is already git-versioned
the backend's real (possibly multi-segment) subdir is used so the local path
matches the remote tree exactly.

Why pay this up front for non-git agents too: it means **enabling Git Versioning
later is a pure `git init` + config update — no re-download, no file move.** You
can start working without git, the developer enables it on the platform, and the
folder already has the right shape. It also keeps one set of prompt/path
conventions regardless of whether git is on.

## User flows

### Getting linked
1. `cinna setup` / `cinna agent sync` checks out the agent. If the agent is
   already git-versioned, the CLI **auto-runs the link** — the developer gets a
   real working tree from the first checkout, no extra step.
2. If git is enabled *later* on an already-checked-out agent, run `cinna git link`
   once. Because the folder already uses the Model-A layout, linking only
   initializes the clone and points it at the remote.
3. `cinna git status` shows whether the agent is git-versioned, whether it's
   linked, and the working-tree status.

### Everyday versioning
- `cinna git commit -m "…" [--push]` — stage this agent's subdir and commit
  (honors the committed `.gitignore`).
- `cinna git push` — push the agent's branch (fast-forward only).
- `cinna git pull` — rebase the remote in; Mutagen then mirrors the update into
  the running container.
- `cinna git log` — recent commits touching this agent's subdir.
- Plain `git` (or an IDE's Source Control panel) works identically once linked —
  the clone has `origin` + upstream configured, so `git push`/`pull` need no extra
  remote setup. `cinna git` adds guard rails, not a requirement.

### Debug / rollback without committing
- `cinna git checkout <ref> [--reload]` restores an earlier commit's
  `workspace/**` into the tree as **uncommitted** changes and (default) flushes
  them to the container via Mutagen — so the live agent runs that version's
  prompts/scripts for debugging or A/B'ing a regression, **without any commit**.
  Undo by checking out the current ref again, or `cinna sync pull --force`.
- The DB-owned manifest (`cinna.agent.json`: SDK config, schedules) is **not**
  Mutagen-synced; `--manifest` restores it locally but reloading it into the agent
  still needs the backend (UI "Pull" / GitOps webhook). Prompt/script changes flow
  live; manifest-level config does not.

### Stop using git
- `cinna git unlink` stops offering the helpers and drops the registry git block,
  but **keeps `.git` and history**. Re-link any time.

## Business rules / guardrails

- **Fast-forward-only, fail-loud, never force.** A rejected push surfaces a clear
  `git pull --rebase` instruction; the CLI never force-pushes a shared agent ref.
- **Direction guard.** If `sync_direction` is `pull`, local pushes are refused by
  the CLI (that agent is edited on the platform side).
- **Backend-owned manifest.** `cinna.agent.json` is regenerated by the backend
  from the DB on every backend push/connect. The CLI commits it as-is and never
  invents values; the developer normally edits prompt **files** under
  `workspace/docs/`, which flow back to the DB via the platform's prompt-sync.
- **Plugins are versioned.** `workspace/plugins/**` is installed via the UI / remote
  plugin repos but **kept in git** so it survives even if that connection is later
  lost. The live workspace tarball can be a *subset* of the committed tree, so on
  link the CLI **restores every committed-but-absent file** rather than presenting
  them as deletions a commit would propagate.
- **Never committed.** Credentials, `app-data/`, logs, databases, `uploads/`,
  `__pycache__/`, `*.pyc`, the CLI's own `.cinna/` (holds the token), and generated
  guides (`CLAUDE.md`, `GIT_VERSIONING.md`, …). Enforced by the committed
  `.gitignore` plus a local `.git/info/exclude` — the CLI never `git add -f` an
  excluded path.
- **Legacy flat workspace.** A pre-Model-A folder that becomes git-versioned is
  **not** auto-converted; `cinna git link` prints a disconnect + re-sync
  instruction instead of silently moving files.
- **File modes aren't tracked.** The tarball and Mutagen don't preserve the
  executable bit and the container is the runtime, so the clone sets
  `core.fileMode false` — x-bit-only diffs never show up as spurious changes.

## Multi-agent / multi-clone behavior

- **Different repos** → fully independent clones (separate `.git`, registry
  entries, Mutagen sessions). No interaction.
- **Same repo, different subdirs** → separate clone roots, each a sparse-checkout
  of its own subdir. Commits/pushes from one preserve the other's subdir on the
  remote. Because both push the same branch, the second pusher must
  `cinna git pull` (rebase) first — a clean, conflict-free rebase since they touch
  disjoint paths.
- **Slug collision** (two agents whose names normalize to the same folder slug) →
  the second clone root gets an agent-hash suffix (`<slug>-<shorthash>/`) so they
  don't collide; re-running setup for the *same* agent still reports "already set
  up".

## Architecture overview

```
                 GET /api/v1/cli/git-coordinates (token-scoped)
cinna git <verb> ─────────────────────────────────────────────► platform
      │                                                            (advice only;
      ▼                                                             no key material)
git_versioning.py ── real git (dev's own creds) ──► external git remote
      │                                                     ▲
      ▼                                                     │ backend deploy key
local clone (Model A: <clone>/<subdir>/workspace/)          │ (server-side push/pull)
      │ Mutagen (alpha = workspace/)                         │
      ▼                                                      │
running container /app/workspace ◄───────────────────────────
```

## Integration points

- **Live Sync** (`../live_sync/live_sync.md`) —
  Mutagen mirrors `workspace/` to the container; `cinna git checkout --reload`
  rides this to make a restored version live.
- **Account workspace** — `cinna git --agent <ref>` targets a synced child from the
  account root, the same way `cinna sync --agent` does.
- **Bootstrap / setup** — `cinna setup` and `cinna agent sync` decide the Model-A
  layout and auto-link when the agent is git-versioned.

Implementation: see [git_versioning_tech.md](git_versioning_tech.md). Real-usage
e2e test scenarios: see [git_versioning_acceptance.md](git_versioning_acceptance.md).
