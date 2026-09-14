# Agent Management (`cinna agent`)

## Purpose

Drive an agent's **whole local-dev lifecycle from the account workspace** —
create an agent on the platform, attach (sync) a local workspace to it, detach
(unsync) it, restart or rebuild its remote environment, choose the model it runs
in each mode, and inspect what
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
- **The model belongs to the environment.** An agent's active environment holds,
  per mode, an SDK, an optional model override and the AI credentials; with no
  override the platform catalog picks the mode's default. `agent model` reads and
  changes those settings — never the agent record — and a change reaches the
  container only through a rebuild.

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
2. It then **starts the sync session and flushes it once**, while local and
   remote are still the same clone. That shared starting state is what later
   edits are reconciled against; a session first created by a later
   `cinna sync push` has none, so the builder's own edits used to come back as
   conflicts against untouched remote originals. A session that cannot start is a
   warning naming `cinna sync push --agent <slug>` to run *before* editing — never
   a failed sync.
3. Re-running sync for an agent **already synced** in that folder is refused
   (with a `cinna agent unsync` / `cinna set-token` hint) — it never silently
   re-clones or duplicates.
4. The workspace lands at `agents/<slug>/<subdir>/` (the Model-A nested layout;
   `<subdir>` is the slug unless the agent is git-versioned with another subdir) —
   the command prints the exact path. Afterward `cd` there and `cinna dev`, or stay
   at the account root and use `cinna sync push --agent <slug>` /
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

### Rebuild an environment that predates a feature
1. `cinna agent rebuild-env <agent_ref> [--yes]` is **not** a louder restart.
   A restart re-runs the same image, so it can recover a wedged container but
   can never add a route that was not built into it. A rebuild replaces the
   container's `/app/core` from the template, which is the only thing that
   gives a pre-feature container a feature's routes.
2. Reach for it when a surface reports `adapter_unsupported` — e.g. a skills
   refresh, or `cinna skills list`, that keeps saying the skill index cannot be
   read because the container has no skills endpoint. Restarting or refreshing
   that state is a loop that cannot close.
3. Same unsynced-local-changes guard as `restart-env`, and it matters more
   here: a rebuild re-materializes the backend scaffold over a freshly
   recreated container, so local edits have further to fall.
4. Then a second confirmation, because this recreates the container and takes
   minutes. **`--yes` skips only that second prompt.** It deliberately does not
   disarm the unsynced-changes guard, so `rebuild-env --yes` is not a general
   non-interactive switch: under `--no-input` on a workspace with pending local
   changes it still aborts (the guard takes its "no" default).
5. It blocks for the whole rebuild, then prints the status. A rebuild restores
   the state it found, so an environment that was stopped comes back stopped —
   successfully. The command says so rather than letting "rebuilt successfully"
   mean a container that still answers nothing.
6. For an environment that came back running, it then **waits for the
   container's health check to answer** (up to 180 s). "Rebuilt" and "running" are
   row states; the server inside can still be starting, and a chat sent into that
   window sat streaming with no reply. It ends with "ready to chat", or a warning
   that the environment has not answered yet.

### Inspect what's live
1. `cinna agent show <agent_ref>` prints the **effective prompts** (each under its
   `[entrypoint]` / `[workflow]` / `[refiner]` label — as the runtime reads them),
   enabled features, the connected credentials (never secret values), and the
   REST-API status when enabled. `--prompts` shows only prompts; `--full` prints
   long prompts whole.
   - Each credential line carries name, type, **slot** (its service URI), a
     placeholder / needs-setup marker, and id — the slot being what a skill's
     `credentials:` block names. A credential shared by someone else whose slot
     this account cannot see reads `slot: unknown`, never `no slot`. When the
     agent's credential listing cannot be read, it falls back to name + type.

### Choose the model per mode
1. `cinna agent model show <agent_ref> [--json]` prints, for conversation and
   building mode, the SDK, the model override (`—` when none), the **effective**
   model the runtime uses — the override, or the platform catalog's default for
   that mode — and the platform's health verdict (`ok`, `retired_override`,
   `unknown_model`, `unverified`), plus which AI credentials the environment uses.
   When the platform flags a warning, its own remediation text and suggested model
   are printed under the table.
2. `cinna agent model set <agent_ref>` takes `--conversation` / `--building` (a
   model id, or `default` to clear the override) and `--conversation-credential` /
   `--building-credential` (an AI credential id, or `default` to unpin). Only what
   was passed changes. Both credentials `default` returns to the account's default
   AI credentials; an explicit credential id turns them off and keeps the other
   mode's pin.
3. A request that matches the current settings says "nothing changed" and stops —
   no prompt, no write, no rebuild.
4. Otherwise it runs `rebuild-env`'s gates — the unsynced-local-changes guard, then
   the "takes a few minutes" confirmation that `--yes` skips — stores the new
   settings, rebuilds, and ends the way `rebuild-env` does (the stopped note, or
   the health-check wait and "ready to chat"). The new table follows.
5. `--no-rebuild` stores the change and names `cinna agent rebuild-env <agent>` to
   apply it.
6. The usual reason to switch is a smaller conversation model. The gate is the
   scenario set: re-run it with `cinna agent scenarios run` and keep the switch only
   if every row still meets its Expect (see
   [Remote Chat](../remote_chat/remote_chat.md)).

### Edit the prompts as files
1. `cinna agent prompts pull <agent_ref>` writes `workflow.md`, `entrypoint.md`,
   `refiner.md`, `router_trigger.md`, `description.md` and `example_prompts.json`
   into `prompts/<slug>/` at the account root (`--dir` to choose), and records
   what it pulled. Not under `agents/`: that tree is the synced workspace, and for a
   git-versioned agent a git working tree.
2. Edit the files. `cinna agent prompts diff <agent_ref>` shows each edit against
   the platform, and flags a field the platform changed since the pull.
3. `cinna agent prompts push <agent_ref>` sends **only the fields edited since the
   pull** in one bulk write, then pushes the three doc-backed prompts into the
   running environment's `docs/*.md` (`--no-sync-env` to skip; a stopped
   environment gets them on its next start, and the command says so).
   `--dry-run` shows the diff and writes nothing.
4. Why files: changing one line used to take a raw GET, a hand-built JSON body, a
   raw PUT and a raw `sync-prompts` call — and building that JSON inline let the
   shell command-substitute the prompt's Markdown backticks.

### Check the agent's self-reported status
1. `cinna agent status show <agent_ref>` prints the agent's cached `STATUS.md`
   snapshot (severity, summary, age) plus the configured refresh pre-command.
2. `cinna agent status refresh <agent_ref>` forces a **live** re-read — wakes a
   suspended env, runs the pre-command, re-reads `STATUS.md`, and never fails
   (it falls back to the cached snapshot on error).
3. `cinna agent status set-command <agent_ref> "<cmd>"` configures the pre-command
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
  metadata only — name, type, slot, placeholder/setup state, id; status is a
  published `STATUS.md` snapshot.
- **A slot is never claimed without evidence.** A credential's slot is read from
  the account's own credential listing (the agent credential route reports none),
  and a credential shared by someone else whose slot is invisible reads `unknown`.
- **Prompt push never reverts what it did not edit.** Only fields changed since
  the pull are sent; a field changed on the platform since the pull and edited
  locally too is refused until `--force` (or `pull --force` to take the
  platform's). `pull` refuses to overwrite unpushed edits, files it did not write,
  or another agent's prompts without `--force`. The agent config stays
  authoritative; hand-editing the synced `docs/*.md` as well is a last-writer-wins
  race — pick one path.
- **Model settings merge, never reset.** The model lives on the agent's
  environment, whose write path resets every field it is not sent, so `model set`
  sends both modes' current settings with only the requested change, stores them
  without a rebuild (a rebuild through the escape hatch outlasts its 30 s limit),
  then rebuilds through the account route. An unchanged set is a no-op. A foreign
  install or a non-developer is refused before anything is read, with the
  platform's own sentence. Model ids are never validated locally — the platform's
  health verdict is the check.
- **Rebuild readiness is stated, not assumed.** A rebuilt, running environment is
  "ready to chat" only after its health check answers; a timeout is a warning, and
  an unreadable health answer says nothing rather than guessing.
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
      │  rebuild-env→ recreate container from template     │  POST …/agents/{id}/rebuild-env
      │  model → copy settings, store, then rebuild-env    │  GET/POST environments/{id}[/reconfigure] (api-proxy)
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
  down; `restart-env` and `rebuild-env` both check the sync session for unsynced
  edits before touching the container.
- **Git Versioning** ([git_versioning](../git_versioning/git_versioning.md)) —
  `agent sync` auto-links the git working tree when the agent is git-versioned,
  identical to `cinna setup`.
- **Agent Schedules** (`../agent_schedules/agent_schedules.md`) <!-- nocheck: sibling doc owned by another agent --> —
  the `cinna agent schedule …` CRON subgroup, documented separately.

Implementation: see [agent_management_tech.md](agent_management_tech.md). Live e2e
scenarios: see [agent_management_acceptance.md](agent_management_acceptance.md).
