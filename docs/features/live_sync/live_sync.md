# Live Sync (`cinna sync` + the Mutagen transport)

## Purpose

Keep a developer's local `workspace/` continuously mirrored to the agent's
**remote container** (`/app/workspace`) so every edit is *live* — the running
agent executes the code you just saved, with no local Docker, no build, no manual
push. **Mutagen** does the continuous bidirectional sync over a WebSocket tunnel
to the platform; the `cinna sync` command group lets a developer (or a headless
builder script) inspect that session, force a one-shot flush, and clear conflicts.

## Mental model — the remote container is the runtime

cinna-cli has no local runtime. The only place agent code *runs* is the remote
agent environment (a Docker container the platform manages). Live Sync is the
bridge:

- **Local `workspace/`** — the files you edit in your editor / coding agent.
- **Remote `/app/workspace`** — the same files inside the running container; this
  is where `cinna exec` and the platform's building/conversation modes execute.
- **Mutagen** — a long-lived daemon that watches both ends and propagates changes
  both directions in near-real-time (`two-way-safe` mode). Save a file locally →
  it appears in the container within a sync cycle; the backend regenerates a
  managed file (prompts, `credentials/`) → it flows back to your disk.

"Dev == prod, not dev ≈ prod": because there is one runtime and Mutagen keeps the
two file trees identical, what you test locally is exactly what ships.

The transport never uses real SSH. Mutagen expects an SSH-style transport, so the
CLI ships a tiny shim (`cinna-sync-ssh`) that translates Mutagen's
`ssh user@host mutagen-agent …` invocation into a WebSocket to the platform, which
then proxies to the container's `mutagen-agent`. The shim is what lets the platform
authenticate the tunnel and auto-wake a suspended environment.

## Core concepts

- **Sync session** — one Mutagen session per agent, named `cinna-<short-agent-id>`
  (first 8 hex chars of the agent id). Created by `cinna setup` / `cinna dev`,
  reused by the one-shot `cinna sync` verbs, torn down on `cinna disconnect` or when
  the `cinna dev` TUI exits.
- **Mutagen daemon** — a single shared, user-owned daemon process. It serves every
  agent the user has synced; its environment is captured **once** at start, which is
  why agent identity is never trusted from the daemon env (see below).
- **Alpha / beta** — Mutagen's two endpoints. Alpha = local `workspace/`, beta =
  remote `/app/workspace`. "Pending → remote" / "pending → local" are the staged
  changes for each direction.
- **Agent registry** (`~/.cinna/agents.json`) — the authoritative per-agent map of
  `agent_id → {platform_url, cli_token, workspace_path, …}`. The shim re-reads it on
  every invocation so a shared daemon can serve multiple agents concurrently and
  pick up token rotations immediately.
- **Conflict** — a path that diverged on both sides since the last reconciliation.
  In `two-way-safe` (Mutagen 0.18.x) a conflict is recorded in the session's
  `conflicts[]` JSON but **no** `.conflict.<side>` file is written to disk — both
  sides simply keep their own content until a human or a `resolve` command picks a
  winner.

## User flows

### Continuous sync (the default)

1. `cinna setup` (or `cinna dev`) creates the sync session; from then on edits flow
   both ways automatically. `cinna dev` attaches a live TUI (Sync / Details /
   Conflicts tabs) and terminates the session on exit.
2. Save a file → Mutagen mirrors it into the container → `cinna exec …` runs the new
   version. No explicit push.
3. `cinna sync status` (read-only, safe alongside a live `cinna dev`) shows the
   session state, pending counts each direction, conflict count, and any last error.

### Inspecting conflicts

- `cinna sync conflicts` lists the paths Mutagen has parked, sourced from the daemon
  JSON so the list agrees with the count `cinna sync status` reports. It is
  read-only: it names the files but changes nothing.

### One-shot flush (headless / scripted)

- `cinna sync push` — ensure a session exists, force one bidirectional sync cycle,
  and **block until it settles**. For a scripted builder that edits files and then
  needs them live before running a command. Reuses a live `cinna dev` session or
  creates a detached one that persists in the daemon.
- `cinna sync pull` — the mirror: force a cycle and wait, typically after the backend
  regenerated managed files.
- Both accept `--force`, which clears conflicts *in their direction* before flushing:
  `push --force` = local wins, `pull --force` = remote wins.

### Resolving conflicts

- `cinna sync resolve --prefer local|remote` clears every parked conflict in one
  command. `local` deletes the remote losing copies (via `cinna exec rm`) so your
  edits propagate out; `remote` backs up your local copies under `.cinna/sync/` and
  lets the container's version propagate back. It replaces the manual
  kill/delete/restart dance.

## Business rules / guardrails

- **Continuous, bidirectional, two-way-safe.** Mutagen never silently overwrites a
  two-sided change — it parks it as a conflict. Edits are not "live" while a file is
  conflicted; the status/conflicts surfaces say so loudly.
- **`push`/`pull` are one-shot flushes, not directional transports.** The underlying
  `mutagen sync flush` is always bidirectional; the only thing that differs between
  `push` and `pull` is the `--force` conflict-resolution direction. A plain
  `cinna sync push` with no conflicts and a plain `cinna sync pull` do the same flush.
- **`--force` / `resolve` are fail-loud, never silent.** A losing local copy is
  **backed up** (never deleted) under `.cinna/sync/resolve-backup/<ts>/` (or
  `redev-backup/` for `cinna redev`); a remote loser is removed via `cinna exec rm`,
  and any path that could not be removed is reported in `remaining`.
- **Agent identity comes from argv + registry, never the daemon env.** The shared
  daemon's captured env may name a *different* agent; the shim derives the agent id
  from the argv host (`cinna-agent-<id>`) and resolves credentials from
  `~/.cinna/agents.json`, so two agents live-sync concurrently without cross-leaking
  tokens.
- **Mutagen version is pinned.** The platform advertises a required Mutagen version
  (`GET /sync-runtime`); the CLI refuses to start on a mismatched **minor** version
  (a patch-level difference only warns), because wire-protocol bumps can break sync
  silently.
- **`credentials/` is never synced up.** It is backend-managed and regenerated in the
  container on every env restart; `mutagen.yml` ignores it (plus `.cinna/`, VCS dirs,
  caches) so a stale local copy can't conflict on a file you're told never to edit.
- **Transparent recovery.** A stale shared daemon (wrong `MUTAGEN_SSH_PATH`) is
  bounced and retried once; a still-waking environment (WebSocket close `1013`) is
  retried with backoff before a friendly "environment still waking" error.

## Architecture overview

```
cinna sync <verb> ──► sync_session.py ──► mutagen CLI ──► Mutagen daemon (shared)
                                                              │ MUTAGEN_SSH_PATH
                                                              ▼
                                                   ~/.cinna/mutagen-ssh/ssh (wrapper)
                                                              │ exec
                                                              ▼
                                                       cinna-sync-ssh shim
                                                              │ WebSocket (JWT)
                                                              ▼
                                       platform /sync-stream ─► container mutagen-agent
                                       local workspace/  ◄──────►  /app/workspace
```

## Integration points

- **Git Versioning** ([../git_versioning/git_versioning.md](../git_versioning/git_versioning.md)) —
  the orthogonal second layer on the same folder. Mutagen ignores `.git`;
  `cinna git checkout --reload` rides this sync to make a restored version live.
- **Remote Exec** — `cinna exec` runs commands in the same `/app/workspace` Mutagen
  mirrors into, so a synced edit is immediately executable; local-wins conflict
  resolution also deletes remote losers through `cinna exec rm`.
- **Doctor** — `cinna doctor` reconciles the registry against the Mutagen daemon and
  terminates stalled/orphaned `cinna-*` sessions.
- **Account workspace** — every `cinna sync` subcommand takes `--agent <ref>` to
  target a synced child from the account root, like `cinna exec --agent`.

Implementation: see [live_sync_tech.md](live_sync_tech.md). Real-usage e2e test
scenarios: see [live_sync_acceptance.md](live_sync_acceptance.md).
