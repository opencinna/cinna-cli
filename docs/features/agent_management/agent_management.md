# Agent Management (`cinna agent`)

## Purpose

Drive an agent's **whole local-dev lifecycle from the account workspace** —
create an agent on the platform, attach (sync) a local workspace to it, detach
(unsync) it, restart its remote environment when it gets wedged, and inspect what
the running agent *actually* sees (effective prompts, features, credentials, and
self-reported status). One account login fans out to every agent you own; you
never paste a per-agent setup token.

This doc covers the agent **lifecycle** verbs. The CRON automation subgroup
(`cinna agent schedule …`) is documented separately — see
[Agent Schedules](../agent_schedules/agent_schedules.md) <!-- nocheck: sibling doc owned by another agent -->.

## Mental model — account-scoped fan-out over per-agent workspaces

- **One account workspace, many agent workspaces.** An *account workspace*
  (`.cinna/account.json`, holding the `cli-account` token) is the control plane.
  Every `cinna agent` verb runs from inside it (or any nested folder — the CLI
  walks up to find `account.json`) and talks **only** to the account-scoped API
  (`/api/v1/cli/account/*`). See [Account Workspace](../account_workspace/account_workspace.md) <!-- nocheck: sibling doc owned by another agent -->.
- **Sync = mint + materialize.** Attaching an agent (`cinna agent sync`) mints a
  *per-agent child CLI token* from the account token and lays down a normal
  per-agent workspace under `agents/<slug>/` — byte-for-byte what `cinna setup`
  would produce. From then on `cd agents/<slug> && cinna dev`, `cinna exec`, and
  `cinna git` work exactly as for a hand-set-up agent. See
  [Live Sync](../live_sync/live_sync.md).
- **Two token tiers.** The **account token** discovers agents and mints/revokes
  child tokens but can never sync or exec. The **child token** (one per agent per
  machine) is what actually syncs and execs. `agent sync`/`unsync` are the bridge
  between the two tiers.
- **The agent reference is uniform.** Every verb takes `AGENT_REF` — a display
  name, slug, or agent id — resolved against `cinna account agents`. Ambiguous
  slugs (two agents normalizing to the same name) force you to use the id.
- **"Effective" vs. "what I typed".** `agent show` / `agent status` report what
  the *runtime* reads right now (the prompts the env assembles, the live status
  snapshot) — so you can confirm an edit is actually live without opening the
  browser.

## User flows

### Create an agent
1. `cinna agent create "<Name>" [--description …]` — thin client: only the name
   (and optional description) is sent; the backend applies every default (AI
   credentials, env template, environment provisioning) exactly as the UI does.
2. The command prints the new agent's id, its web-UI link, and the target user
   workspace, then nudges `cinna agent sync <name>` to attach a local workspace.

### Attach a local workspace (sync)
1. `cinna agent sync "<Name>"` resolves the agent, mints a child token, and
   provisions a standard workspace under `agents/<slug>/` (Mutagen check →
   workspace clone → context/MCP files → `mutagen.yml`). If the agent is
   git-versioned it auto-links the git working tree (see
   [Git Versioning](../git_versioning/git_versioning.md)).
2. Re-running sync for an agent **already synced** in that folder is refused
   (with a `cinna agent unsync` / `cinna set-token` hint) — it never silently
   re-clones or duplicates.
3. Afterward `cd agents/<slug> && cinna dev`, or stay at the account root and use
   `cinna exec --agent <slug> <cmd>`.

### Detach a workspace (unsync)
1. `cinna agent unsync <agent_ref>` resolves the synced child folder, confirms,
   then: stops the Mutagen sync session, revokes the child token server-side
   (best-effort), removes `.cinna/` + all CLI-generated files, and drops the
   registry entry. **User workspace files are preserved.**
2. It is the account-workspace equivalent of `cinna disconnect` run inside the
   child folder, plus the server-side token revoke.

### Recover a wedged environment
1. `cinna agent restart-env <agent_ref>` bounces the agent's remote container —
   the first-class recovery path for a stuck env or a producer REST API stuck
   reporting a stale error, without dropping to the raw `cinna api` escape hatch.
2. Before bouncing, if **this machine** has a synced workspace with unsynced
   local edits or parked conflicts, it warns and asks to confirm — a restart
   re-materializes the backend scaffold and can clobber local changes, so the
   builder is told to `cinna sync push` first.
3. It blocks until the container is back, then prints the post-restart status.

### Inspect what's live
1. `cinna agent show <agent_ref>` prints the **effective prompts** (entrypoint,
   workflow, refiner — as the runtime reads them), enabled features, connected
   credential names/types (never secret values), and the REST-API status when
   enabled. `--prompts` shows only prompts; `--full` prints long prompts whole.
2. `cinna agent status show <agent_ref>` prints the agent's cached `STATUS.md`
   snapshot (severity, summary, age) plus the configured refresh pre-command.
3. `cinna agent status refresh <agent_ref>` forces a **live** re-read — wakes a
   suspended env, runs the pre-command, re-reads `STATUS.md`, and never fails
   (it falls back to the cached snapshot on error).
4. `cinna agent status set-command <agent_ref> "<cmd>"` configures the pre-command
   the refresh runs (a raw shell/Python string or a `/run:<name>` reference); an
   empty string opts out. The platform default is `/run:status`.

## Business rules / guardrails

- **Account-workspace only.** Every verb requires an account workspace; run
  outside one and it fails fast (no per-agent token can drive these routes).
- **`AGENT_REF` resolution is uniform and fail-loud.** Matched by id, exact name,
  or slug; no match lists the available agents, an ambiguous slug lists the
  collisions and demands the id.
- **Sync never clobbers an existing checkout.** A same-agent re-sync into an
  occupied `agents/<slug>/` is refused; a *different* agent that slugs the same
  gets a `-<shorthash>` suffix on its clone root (collision handling shared with
  `cinna setup`).
- **Unsync preserves user files and is best-effort on the server.** The local
  teardown always completes; a failed token revoke (network, or a 404 for
  workspaces predating provenance tracking) degrades to a warning — the token
  expires on its own or can be revoked from the UI.
- **Restart is a fail-loud, confirm-before-clobber recovery.** The unsynced-edits
  warning is shown only when a local workspace for the agent has pending pushes
  or conflicts; otherwise it proceeds straight to the bounce.
- **`agent show` / `agent status` never print secrets.** Credentials surface as
  name + type only; status is a published `STATUS.md` snapshot.
- **`status refresh` never raises.** A force-refresh that can't reach the env or
  whose pre-command fails returns the cached snapshot (and may carry a
  `refresh_command_warning`), so inspection is always answerable.

## Architecture overview

```
                                .cinna/account.json (cli-account token)
cinna agent <verb> ── account.py ── AccountClient ──► /api/v1/cli/account/*
      │                                                   │
      │  sync  → mint child token + bootstrap workspace   │  POST …/agents/{id}/mint
      │  unsync→ stop sync + revoke child token + teardown │  DELETE …/tokens/children/{id}
      │  create→ thin agent create                        │  POST …/agents
      │  restart-env→ bounce container (block until up)    │  POST …/agents/{id}/restart-env
      │  show  → effective prompts/features/creds          │  GET  …/agents/{id}/inspect
      │  status→ STATUS.md snapshot / refresh / set-cmd    │  GET/POST …/agents/{id}/status[/refresh-command]
      ▼
agents/<slug>/  (a normal per-agent workspace — child token, Mutagen, optional git)
```

## Integration points

- **Account Workspace** (`../account_workspace/account_workspace.md`) <!-- nocheck: sibling doc owned by another agent --> —
  supplies the account token (`cinna login`), `find_account_root`, and the
  `cinna account agents` listing every verb resolves against.
- **Live Sync** (`../live_sync/live_sync.md`) —
  `agent sync` materializes the Mutagen-synced workspace; `agent unsync` tears it
  down; `restart-env` checks the sync session for unsynced edits before bouncing.
- **Git Versioning** ([git_versioning](../git_versioning/git_versioning.md)) —
  `agent sync` auto-links the git working tree when the agent is git-versioned,
  identical to `cinna setup`.
- **Agent Schedules** (`../agent_schedules/agent_schedules.md`) <!-- nocheck: sibling doc owned by another agent --> —
  the `cinna agent schedule …` CRON subgroup, documented separately.

Implementation: see [agent_management_tech.md](agent_management_tech.md). Live e2e
scenarios: see [agent_management_acceptance.md](agent_management_acceptance.md).
