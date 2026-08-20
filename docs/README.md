# Cinna CLI — Project Context

> This document orients a human or LLM working on the cinna-cli codebase for the first time. It explains **why** this project exists, **how** it fits into the Cinna Core platform, the key concepts and terminology, the live-sync architecture, and the important design decisions.

---

## What is Cinna Core?

**Cinna Core** is a platform for building and running AI agents. Each agent is a self-contained unit with Python scripts, prompt files, a webapp dashboard, integration credentials, and a knowledge base. Agents run inside managed cloud environments — Docker containers with a specific Python runtime, system packages, and mounted workspace files.

The platform has two main modes of interaction with an agent:

- **Building mode** — An AI-powered workflow where a cloud-based LLM (the "building agent") develops and iterates on the agent's scripts, prompts, and configurations. The building LLM receives a system prompt (the **building prompt**) assembled by the **env core** (the runtime engine inside the agent's container).
- **Conversation mode** — End users interact with the finished agent via chat.

The platform also handles scheduling, triggers, email, A2A, MCP serving, and session history. None of that is relevant to local development — it stays in the cloud.

## What is Cinna CLI?

**Cinna CLI** (`cinna-cli`) is a local development tool that lets developers work on agents from their own machine using their preferred editor and AI coding tools (Claude Code, opencode, Cursor). Instead of replicating the agent's runtime locally, the CLI treats the **remote agent environment** as the runtime and keeps the local workspace continuously synced to it.

### What local dev is for

- Writing and testing Python scripts
- Testing credential integrations (keys and tokens stay on the platform, exercised by the remote env)
- Installing and validating Python / system packages
- Writing and iterating on prompt files
- Building webapp dashboards and data endpoints
- Preparing output files and reports

### What stays in the cloud

- All agent runtime: scripts execute in the remote env (via `cinna exec`)
- Production sessions (building mode, conversation mode)
- Schedulers, triggers, email
- A2A / MCP protocol serving
- Session history, activity logging
- Credentials

## Why the live-sync model?

An earlier iteration of `cinna-cli` built a local Docker image mirroring the agent env, synced via `push`/`pull`, and ran scripts with `docker exec`. That model had real friction points:

- Developers had to install Docker and wait for image builds.
- Local `pip install` / apt install drifted from production over time.
- Credentials had to round-trip between platform and local disk, expanding the secrets blast radius.
- Push/pull was manual; conflicts were only detected on sync.

The live-sync rewrite removes the local container entirely. Mutagen keeps the workspace bidirectionally in step with the remote env over a WebSocket tunnel to the platform, and `cinna exec` streams commands through the platform to the remote env. Dev == prod, not dev ≈ prod.

---

## Glossary

### Agent

A self-contained automation unit on the platform. Has a name, template, scripts, prompts, webapp, credentials, and a knowledge base. Identified by `agent_id`.

### Environment

The runtime container configuration for an agent on the platform. Each agent has exactly one environment. Environments may be suspended when idle; opening a sync WebSocket auto-activates them.

### Template

Base configuration for an agent (e.g., `general-env`, `python-env-advanced`). Determines the Dockerfile, default packages, workspace structure. Shipped as `backend/app/env-templates/{template}/` in the platform repo.

### Workspace

The collection of files that make up an agent's working directory. Lives at `workspace/` in the local project — continuously synced with the remote env's `/app/workspace`.

Structure (managed by the remote env; the CLI just mirrors it):

Bundle-owned (replaced when the publisher pushes a new bundle revision):

- `scripts/` — Python scripts the agent executes
- `docs/` — Prompt files (`WORKFLOW_PROMPT.md`, etc.)
- `webapp/` — HTML/CSS/JS dashboard + Python data endpoints
- `knowledge/` — Static integration docs shipped with the bundle
- `files/` — Static publisher-shipped assets (lookup tables, fixtures)
- `workspace_requirements.txt`, `workspace_system_packages.txt`

Per-user persistent — **not part of the published bundle**. Backed by a platform
`AppDataVolume` keyed by `(user_id, bundle_id)`, stored on the platform host under
`${APP_DATA_STORAGE_DIR}/<user_id>/<bundle_id>/`, and bind-mounted into the agent
container at `/app/workspace/app-data`. The volume survives `apply-update` (bundle
folders get overwritten, `app-data/` is never touched) and uninstall/reinstall
(the row is marked orphaned, not deleted, and reattaches on next install of the
same `bundle_id`):

- `app-data/storage/` — Long-lived runtime output scripts should write here
  (databases, JSON, CSVs, reports, derived data). This is the canonical place
  for anything the agent needs to keep across sessions and bundle updates.
- `app-data/uploads/` — Destination for every user-supplied file at runtime:
  chat attachments, task attachments, MCP `get_file_upload_url` uploads.
- `app-data/cache/` — Disposable caches the scripts may rebuild on demand.

**What the CLI developer sees:** the `workspace/app-data/` directory synced to
your machine is the publisher working install's *own* app-data volume — the
developer's personal runtime state. It is **not** content that gets shipped
when you publish a new bundle revision: bundle revisions snapshot only the
bundle-owned folders above. Every other user who installs the bundle gets a
fresh, empty `(user_id, bundle_id)` volume on the platform.

`workspace/app-data/` is gitignored by default for the same reason — it is
runtime state, not source.

Synced from the platform:

- `credentials/` — Integration credentials (managed by the backend)

### Sync session

A single Mutagen sync session per agent, named `cinna-<short-agent-id>`. Created on `cinna setup` (and resumed / re-created by `cinna dev`), lives until `cinna disconnect`. The Mutagen daemon persists across sessions and across multiple agents.

### Building Mode / Building Prompt

The platform's cloud-based AI development workflow. The building prompt is assembled by the env core and returned from `GET /building-context`; the CLI writes it to `BUILDING_AGENT.md`.

### Setup Token

A short-lived (15 min, single-use) token generated by the platform UI when you click "Local Development". Embedded in the `curl | python3` bootstrap command. Exchanged for a CLI token via `POST /api/cli-setup/{token}`.

### CLI Token

A JWT issued when a setup token is exchanged. Authenticates all subsequent API calls and the sync WebSocket. Stored in `.cinna/config.json` (the workspace copy) and mirrored into `~/.cinna/agents.json` (the per-user registry read by the sync shim).

Key properties:

- **Rolling expiry** — renewed on each successful request (7-day window).
- **Revocable** — from the platform UI.
- **Agent-scoped** — one token per agent per user.
- **Probeable** — `cinna list` and `cinna status` call `GET /sync-runtime` as a cheap authenticated probe and label the token `valid` / `expired` / `no connection`.
- **Refreshable in place** — `cinna set-token <token_or_url>` re-exchanges a fresh setup token through `POST /api/cli-setup/{token}` and rewrites both stores without re-cloning the workspace. The refresh is bound to the agent already in the workspace: if the exchanged token belongs to a different agent, the command aborts.

### Account CLI Token

A second token type (`token_type="cli-account"`) issued to an **account workspace** (`.cinna/account.json`). Scoped only to the `/account/*` routes — it discovers agents and mints per-agent CLI tokens (`cinna agent sync`), but cannot itself sync or exec. Same 7-day rolling expiry as a CLI token.

- **Refreshable without a paste** — `cinna login` runs an RFC 8628 device-authorization flow: the CLI prints a short code + URL, the user clicks **Authorize** in the browser (already signed in), and the CLI swaps the fresh token into `.cinna/account.json` in place. Run from an empty/new folder, the same command instead bootstraps a brand-new account workspace.
- **Mints child tokens** — per-agent tokens minted from it carry its id as provenance and are re-mintable via `POST /account/agents/{id}/mint` (used by `cinna agent sync` and `cinna doctor`).

### Knowledge Source

A documentation/data source attached to an agent. Queried via the MCP proxy's `knowledge_query` tool, backed by the platform's vector search.

### MCP (Model Context Protocol)

An open protocol for connecting AI tools to external data/capabilities. The CLI runs a local stdio MCP server (`cinna mcp-proxy`) that exposes `knowledge_query`. Configured via `.mcp.json` (Claude Code) and `opencode.json` (opencode).

---

## Architecture

### System Overview

```
┌──────────────────┐     ┌─────────────────────┐     ┌────────────────────────┐
│ Your IDE /       │     │ Cinna CLI           │     │ Cinna Core Platform    │
│ Claude Code /    │     │                     │     │                        │
│ opencode         │     │ cinna setup         │     │ POST /api/cli-setup/…  │
└────────┬─────────┘     │ cinna set-token     │     │ GET  /workspace        │
         │               │ cinna dev           │     │ GET  /building-context │
         │ edits files   │ cinna sync status   │     │ GET  /sync-runtime     │
         │               │ cinna exec          │     │ POST /exec (SSE)       │
         ▼               │ cinna list          │     │ WSS  /sync-stream      │
  ~/my-agent/            │ cinna disconnect    │     │                        │
    workspace/           └───┬────────┬────────┘     │  proxies to the agent  │
    mutagen.yml              │        │              │  env: /command/stream, │
                             │ HTTPS  │ WSS          │  /sync/exec            │
                             │ (JWT)  │ (Mutagen)    └──────────┬─────────────┘
  MCP proxy (stdio) ─────────┘        │                         │
    knowledge_query                   ▼                         ▼
                           cinna-sync-ssh shim         ┌─────────────────────┐
                             (SSH transport            │ Remote Agent Env    │
                              over WebSocket)          │  (Docker container) │
                                                       │  mutagen-agent      │
                                                       │  /app/workspace     │
                                                       └─────────────────────┘
```

### Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Remote env is the only runtime | Eliminates Docker dependency on dev machines; dev == prod by construction. |
| Mutagen over WebSocket tunnel | Battle-tested bidirectional sync; the WS hop lets the platform authn + auto-activate the env. |
| `cinna-sync-ssh` as a separate binary | Mutagen expects an SSH-style transport. A tiny shim translates `ssh user@host cmd` into a WebSocket call. Installed as a console script alongside `cinna`. Mutagen's `MUTAGEN_SSH_PATH` env var is a **directory search path** (not a binary pointer), so the CLI materializes `~/.cinna/mutagen-ssh/ssh` as a wrapper and points Mutagen at that directory. |
| Global agent registry at `~/.cinna/agents.json` | One Mutagen daemon serves every agent the user has synced, and its env is captured once at daemon start. The shim reads this per-user registry (keyed by the argv `agent_id`) on every SSH invocation to resolve the right CLI token / platform URL — so two or more agents can live-sync concurrently. 0600 perms, holds JWTs. |
| Platform pins Mutagen version | Mutagen's wire protocol evolves; `GET /sync-runtime` lets the CLI refuse to start with a mismatched version. |
| `cinna exec` uses SSE through the platform | Same auth channel as the rest of the API; streams back via parsed `data:` events. |
| No backwards compatibility | Push/pull/rebuild/credentials flows are deleted, not deprecated. Users rerun bootstrap. |
| Mutagen daemon owned by the user | CLI talks to a shared `mutagen daemon` started on demand — multiple agents reuse it. |

### Module Dependency Graph

```
main.py  (CLI commands — Click)
  │
  ├── bootstrap.py       — setup orchestration
  ├── account.py         — account workspace; `cinna login` (device auth), `cinna account`, `cinna agent`
  ├── doctor.py          — `cinna doctor`: reconcile registry ↔ Mutagen, delete stalled / terminate active sessions, refresh tokens
  ├── chat.py            — `cinna chat`: session-backed conversation testing (poll + NDJSON) over the api-proxy
  ├── improve.py         — `cinna improve`: improvement requests users shared about your agents (list/show/download/status)
  ├── config.py          — .cinna/config.json: load/save/find
  ├── auth.py            — JWT storage, Authorization headers
  ├── client.py          — PlatformClient: HTTP + SSE stream_exec
  ├── mutagen_runtime.py — detect/install Mutagen; gate on version match
  ├── sync_session.py    — wrap the `mutagen` CLI (start/stop/status/conflicts)
  ├── sync_tui.py        — live Textual TUI shown by `cinna dev` (Sync/Details/Conflicts tabs)
  ├── sync_ssh_shim.py   — `cinna-sync-ssh` entry point (WebSocket transport)
  ├── sync.py            — tarball/zip extraction helpers (initial clone only)
  ├── context.py         — CLAUDE.md, BUILDING_AGENT.md, .mcp.json, opencode.json
  ├── mcp_proxy.py       — MCP stdio server for knowledge_query
  ├── console.py         — Rich helpers
  ├── logging.py         — cinna.log (rotating file handler)
  └── errors.py          — exception hierarchy
```

### Local Directory Layout

After `cinna setup` (agent name normalized, e.g. "HR Manager Agent" → `hr-manager-agent`),
every new checkout uses the **Model-A nested layout** (`config.compute_agent_layout`):
a clone-root dir holds the agent at `<subdir>/`, so the folder is already shaped like
the agent's git repo whether or not Git Versioning is enabled (see "Git Versioning"
below). `<subdir>` defaults to the agent slug. If the clone-root dir `<slug>/` is
already taken by a **different** agent (two names normalizing to the same slug), the
clone root falls back to `<slug>-<shorthash>/` (the agent id's short hash) so the two
don't collide; re-running setup for the *same* agent still reports "already set up".

```
hr-manager-agent/               (clone root — becomes the git working tree once linked)
└── hr-manager-agent/           (workspace root == the repo's <subdir>/ node)
  .cinna/
    config.json                 (agent config, CLI token, mutagen_version pin, git{} layout)
  cinna.agent.json              (backend-owned manifest; present once git-versioned)
  workspace/                    (continuously synced with remote /app/workspace)
    scripts/                    (bundle-owned — shipped in published revisions)
    docs/                       (bundle-owned)
    webapp/                     (bundle-owned)
    knowledge/                  (bundle-owned)
    files/                      (bundle-owned — static publisher assets)
    app-data/                   (per-user persistent — NOT part of bundle revisions;
                                 backed by AppDataVolume keyed by (user_id, bundle_id);
                                 platform mount: /app/workspace/app-data)
      storage/                    long-lived runtime output (DBs, reports, derived data)
      uploads/                    all user-supplied file uploads at runtime
      cache/                      disposable caches
    credentials/                (backend-managed)
    workspace_requirements.txt
    workspace_system_packages.txt
  mutagen.yml                   (sync rules — ignores, scan mode)
  CLAUDE.md                     (auto-generated local dev instructions)
  BUILDING_AGENT.md             (building mode system prompt from platform)
  .mcp.json                     (MCP config for Claude Code)
  opencode.json                 (MCP config for opencode)
  .gitignore
```

Per-user global state (one copy, shared across every agent workspace):

```
~/.cinna/
  agents.json                   (agent_id → {platform_url, cli_token, workspace_path,
                                 git?:{clone_path, subdir, repo_url, ref, …}}; 0600)
  mutagen-ssh/
    ssh                         (bash wrapper — execs cinna-sync-ssh; 0755)
```

---

## Feature Registry

Each feature is documented under `docs/features/{feature}/` as a layered set:
`{feature}.md` (business logic / reasoning), `{feature}_tech.md` (implementation +
file refs), and `{feature}_acceptance.md` (live e2e scenarios). See
[cinna-cli.feature.doc](../.claude/commands/cinna-cli.feature.doc.md) for the
authoring convention.

| Feature | Command surface | Docs |
|---|---|---|
| **Bootstrap & onboarding** | `cinna setup` / `set-token` / `login` / `list` / `status` / `disconnect[-all]` / `completion` / `dev` / `redev` | [business](features/bootstrap_onboarding/bootstrap_onboarding.md) · [tech](features/bootstrap_onboarding/bootstrap_onboarding_tech.md) · [acceptance](features/bootstrap_onboarding/bootstrap_onboarding_acceptance.md) |
| **Account workspace** | `cinna account` (setup, agents, status, refresh-context, user-workspace, credentials) | [business](features/account_workspace/account_workspace.md) · [tech](features/account_workspace/account_workspace_tech.md) · [acceptance](features/account_workspace/account_workspace_acceptance.md) |
| **Agent management** | `cinna agent` (sync, unsync, create, restart-env, show, status) | [business](features/agent_management/agent_management.md) · [tech](features/agent_management/agent_management_tech.md) · [acceptance](features/agent_management/agent_management_acceptance.md) |
| **Agent schedules** | `cinna agent schedule` (list, generate, create, update, run, logs, delete) | [business](features/agent_schedules/agent_schedules.md) · [tech](features/agent_schedules/agent_schedules_tech.md) · [acceptance](features/agent_schedules/agent_schedules_acceptance.md) |
| **Live sync** | `cinna sync` (status, conflicts, push, pull, resolve) + Mutagen transport | [business](features/live_sync/live_sync.md) · [tech](features/live_sync/live_sync_tech.md) · [acceptance](features/live_sync/live_sync_acceptance.md) |
| **Remote exec** | `cinna exec` | [business](features/remote_exec/remote_exec.md) · [tech](features/remote_exec/remote_exec_tech.md) · [acceptance](features/remote_exec/remote_exec_acceptance.md) |
| **Remote chat** | `cinna chat` | [business](features/remote_chat/remote_chat.md) · [tech](features/remote_chat/remote_chat_tech.md) · [acceptance](features/remote_chat/remote_chat_acceptance.md) |
| **Agent API** | `cinna agent-api` (enable, refresh, spec, call) · `cinna api` · `cinna connect agent-api` | [business](features/agent_api/agent_api.md) · [tech](features/agent_api/agent_api_tech.md) · [acceptance](features/agent_api/agent_api_acceptance.md) |
| **MCP integration** | `cinna connect mcp` · `cinna mcp-proxy` (knowledge stdio server) | [business](features/mcp_integration/mcp_integration.md) · [tech](features/mcp_integration/mcp_integration_tech.md) · [acceptance](features/mcp_integration/mcp_integration_acceptance.md) |
| **Git versioning** | `cinna git` (link, status, commit, push, pull, log, checkout, unlink) | [business](features/git_versioning/git_versioning.md) · [tech](features/git_versioning/git_versioning_tech.md) · [acceptance](features/git_versioning/git_versioning_acceptance.md) |
| **Improvement requests** | `cinna improve` (list, show, download, status) | [business](features/improvement_requests/improvement_requests.md) · [tech](features/improvement_requests/improvement_requests_tech.md) · [acceptance](features/improvement_requests/improvement_requests_acceptance.md) |

The sections below (Git Versioning, Sync Transport, Remote Exec, Remote Chat,
Bootstrap Flow) remain as in-README quick references and backend contracts; the
feature folders above are the authoritative deep dives.

---

## Git Versioning

> Full feature docs: [git_versioning](features/git_versioning/git_versioning.md)
> (business logic), [_tech](features/git_versioning/git_versioning_tech.md)
> (implementation), [_acceptance](features/git_versioning/git_versioning_acceptance.md)
> (live e2e scenarios). This section is the quick reference + backend contract.

A git-versioned agent has **two independent sync layers** on the same folder:
**Mutagen** keeps `workspace/` mirrored to the running container in near-real-time
(it ignores `.git`), and **git** durably versions the same files against the agent's
external remote. They meet only at the remote. All git ops are local and run with the
developer's **own** git/SSH credentials — the platform's deploy key never reaches the
CLI. (The agent-facing version of this guidance is shipped into each checkout as the
on-demand `GIT_VERSIONING.md`, referenced conditionally from `CLAUDE.md`.)

Because new checkouts already use the Model-A nested layout, enabling Git Versioning
later needs no re-download or file move — just a link:

```
cinna git status      # is this agent git-versioned? linked? working-tree status
cinna git link        # init the clone, sparse-checkout <subdir>, fetch + reset --mixed
cinna git commit -m "…" [--push]   # stage the subdir + commit (honors .gitignore)
cinna git push        # fast-forward only; rejected pushes tell you to pull --rebase
cinna git pull        # rebase the remote in; Mutagen mirrors it into the running env
cinna git log         # recent commits touching this agent's subdir
cinna git checkout <ref> [--reload]   # restore a past version's workspace/ files into
                       #   the tree (uncommitted) + flush to the running env via Mutagen
                       #   — debug/rollback without committing
cinna git unlink      # stop offering git helpers (keeps .git + history)
```

`cinna setup` / `cinna agent sync` auto-run `git link` when the agent is already
git-versioned, so the developer gets a working tree from the first checkout.
`--agent <ref>` targets a synced child from the account root (like `cinna sync`).

Key behaviors: `link` uses `git reset --mixed` (never `--hard`) so the backend's
in-flight changes survive as ordinary uncommitted edits; pushes are fast-forward-only
and never auto-forced; a `sync_direction=pull` agent refuses local pushes; and a legacy
*flat* workspace that becomes git-versioned is **not** auto-converted (link prints a
disconnect + re-sync instruction).

### Backend contract (cinna-core)

The CLI consumes one discovery endpoint; the agent is derived from the per-agent
token, so the path has **no `{agent_id}`**:

```
GET /api/v1/cli/git-coordinates   (Auth: per-agent CLI JWT)
```

Response (`CliGitCoordinates` — `client.get_git_coordinates`, modelled by
`git_versioning.GitCoordinates`). `vcs_enabled=false` ⇒ all other fields null; a 404
(older backend) is treated as `vcs_enabled=false`:

```jsonc
{
  "vcs_enabled": true,                 // false ⇒ agent has no git source
  "repo_url": "git@github.com:acme/agents.git",
  "subdir": "hr-bot",                  // null ⇒ agent lives at the repo root
  "ref": "main",
  "sync_direction": "bidirectional",   // "pull" | "push" | "bidirectional"
  "last_synced_commit": "a1b2c3…",     // SHA the backend last imported/pushed; may be null
  "auth_hint": "ssh"                   // "ssh" | "https" — which local cred the DEV needs
}
```

The remote repo stores each agent as the `schema_version`-2 bundle snapshot — this is
the layout `link` reconciles against (`reset --mixed` brings the manifest + `.gitignore`
from the ref; `workspace/**` stays the live copy):

```
<repo-root>/<subdir>/
├── cinna.agent.json   # backend-owned manifest (prompts, SDK config, schedules,
│                      #   plugin specs, required_credential_specs, content_hash…)
├── workspace/         # the editable agent files (scripts/, docs/, …)
└── .gitignore         # auto-generated; excludes credentials/, app-data/, logs/,
                       #   databases/, uploads/, plugins-derived, __pycache__, *.pyc…
```

Contract rules the CLI honors:

- **Two-writer, fast-forward-only.** Backend (deploy key) and developer (own creds)
  push the same ref; both ff-only, no auto-merge. A rejected dev push ⇒ surface
  `git pull --rebase`, never force.
- **Backend-owned manifest.** `cinna.agent.json` is regenerated by the backend from
  the DB on every backend push/connect — the CLI commits it as-is and never invents
  values (it lacks the DB/env inputs). Editing prompt **files** under
  `workspace/docs/` flows back into the DB via the platform's prompt-sync.
- **Never committable** (the committed `.gitignore` + a local `.git/info/exclude`
  enforce it; the CLI never `git add -f`): `credentials/`, `app-data/`, logs,
  databases, `uploads/`, plugins-derived files, `__pycache__/`, `*.pyc`, plus the
  CLI's own `.cinna/` (holds the token) and generated guides.
- **Deploy key is host-side only** and never leaves the backend; `git-coordinates`
  deliberately omits all key material — the dev authenticates with their own
  git/SSH (`auth_hint` only advises which).
- **Backend adoption of dev pushes.** After a dev push the backend is behind; it
  adopts the change when the user clicks **Pull** on the agent's Git Versioning card
  or via the configured GitOps webhook — the CLI cannot trigger it.

The backend half lives in cinna-core — see its `docs/agents/agent_git_versioning/` <!-- nocheck: cross-repo (cinna-core) path -->
plus `backend/app/api/routes/cli.py` (`CliGitCoordinates` / `_git_auth_hint`),
`GitSourceService`, and `SSHKeyService`; that repo owns the authoritative spec.

---

## Sync Transport

### Wire path

```
local Mutagen daemon
     ↓ MUTAGEN_SSH_PATH=~/.cinna/mutagen-ssh   (directory search path)
     ↓ finds + execs the `ssh` wrapper inside that dir
     ↓ argv: "user@cinna-agent-<id> mutagen-agent <args…>"
     ↓
~/.cinna/mutagen-ssh/ssh   (bash wrapper)
     ↓ exec cinna-sync-ssh "$@"
     ↓
cinna-sync-ssh
     ↓ parse agent_id from argv host ("cinna-agent-<id>")
     ↓ resolve credentials:
     ↓   1. env fast path — used only when CINNA_AGENT_ID == argv agent_id
     ↓   2. ~/.cinna/agents.json registry lookup by agent_id (authoritative)
     ↓ open WebSocket: wss://<platform>/api/v1/cli/agents/<id>/sync-stream
     ↓ first frame: {"remote_command": ["mutagen-agent", …args]}
     ↓ pump: stdin → WS, WS → stdout
     ↓
Platform /sync-stream
     ↓ auth + scope + env-activation
     ↓ opens /sync/exec WS to the remote env core
     ↓ byte-pumps both ways (FIRST_COMPLETED)
     ↓
Env core `mutagen-agent` subprocess
     ↓ does the actual file reconciliation over the tunnel
```

**Why the shim never trusts env for agent identity:** the Mutagen daemon is a
long-lived process; its environment is captured when it first starts and is
shared across every SSH invocation it spawns thereafter. If you set up agent A
and then agent B, the daemon's env still has `CINNA_AGENT_ID=<A>`. Keying
credentials off the argv-derived agent_id plus the per-user registry lets a
single shared daemon serve any number of agents concurrently.

### Mutagen pinning

The platform exposes `GET /sync-runtime` returning `{"mutagen_version": "…", "mutagen_agent_sha256": "…", "platform_api_version": "…"}`. The CLI:

1. Calls this during `cinna setup` and on each `cinna dev`.
2. Runs `mutagen version` locally.
3. Refuses to start on mismatch (with an upgrade prompt) — Mutagen protocol bumps can break sync silently otherwise.

The version, once verified, is cached in `.cinna/config.json` under `mutagen_version` and `last_sync_runtime_check_at`.

### `mutagen.yml`

Seeded by `cinna setup` with `mode: two-way-safe`, `ignore.vcs: true`, and a starter ignore list (`__pycache__/`, `node_modules/`, `.venv/`, `.cinna/`, etc.). Users can customize freely — the CLI never overwrites an existing file.

### Env warmth

Opening the sync WebSocket keeps the remote env warm. The platform heartbeats `last_sync_activity_at` server-side while the WS is open; on disconnect it waits a grace period (default 5 min) before suspending. Reconnecting within grace cancels the timer. A suspended env auto-activates on the next sync-WS handshake — `cinna dev` may take a few seconds on first-of-day runs.

### Conflicts

Mutagen 0.18.1 in `two-way-safe` records conflicts in its session state (the `conflicts[]` array of `mutagen sync list --template '{{json .}}'`) but, contrary to older mutagen behavior, does **not** write `<name>.conflict.<side>.<timestamp>` files to disk — both sides simply retain their divergent content. See [`mutagen_capabilities.md` §7](./mutagen_capabilities.md#7--two-way-safe-does-not-write-conflictsidets-files) for the empirical proof and re-verification steps.

Two surfaces expose conflicts:

- **`cinna sync conflicts`** — a read-only Click subcommand that walks the workspace for any `*.conflict.*` files mutagen *does* end up writing (other sync modes, or future mutagen versions, may still produce them). Implementation lives in `sync_session.list_conflicts`.
- **The Conflicts tab in `cinna dev`** — sourced from mutagen's JSON `conflicts[]`, always populated when a conflict exists. The user navigates the list with `↑`/`↓` and resolves with `1` (take REMOTE) or `5` (take LOCAL). Resolution mechanism: delete the file on the losing side (locally with `unlink()`, remotely via `cinna exec rm`) then `mutagen sync reset <session>`; mutagen sees one side empty, no common ancestor, and propagates the survivor. Verified against 0.18.1; see [`interface.md`](./interface.md) for the full element-to-mutagen-capability mapping.

---

## Remote Exec

`cinna exec <cmd>` POSTs to `/api/v1/cli/agents/{id}/exec` with `{"command": "<cmd>"}` and consumes an SSE stream. Event types:

| Type | Payload | CLI action |
|------|---------|------------|
| `exec_id` | `{"exec_id": "<uuid>"}` | First event. Remember it for future interrupt routing. |
| `tool_result_delta` | `{"content": "…", "metadata": {"stream": "stdout"\|"stderr"}}` | Write chunk to stdout/stderr. |
| `done` | `{"exit_code": N, "duration_seconds": F}` | Terminal — exit with N. |
| `interrupted` | `{"exit_code": -1}` | Terminal — exit 130. |
| `error` | `{"content": "…"}` | Print error; exit 1. |

Ctrl+C closes the SSE stream; the platform cleans up the remote process. Interactive stdin (REPLs, debuggers) is out of scope for the current `/exec` endpoint.

### Argument quoting

There are **two shell passes** between the keyboard and the remote process, and only one round of quoting belongs to each:

1. **Local shell** (the caller's terminal / agent Bash tool) splits the command line into argv and strips one layer of quotes. `exec` is declared `nargs=-1`, so Click receives these already-split tokens as a tuple.
2. **Remote shell** — the platform runs the `command` string through `/bin/sh -c`, re-parsing it a second time.

`exec_cmd` (`main.py`) bridges the two with `shlex.join(command)`: it re-quotes each token so the remote `sh -c` reconstructs the *exact* argv the caller typed. This makes `cinna exec` a transparent passthrough — callers write ordinary single-level quoting, exactly as for a local command:

```bash
cinna exec python -c 'import sys; print(sys.argv)' "a b" '[{"x":"y z"}]'
```

The historical `" ".join(command)` dropped the word boundaries that the local shell's quoting had implied, so any argument containing a space or a shell metacharacter (`;`, `(`, `{`, …) was re-split or mis-parsed by the remote shell — e.g. `print(sys.argv)` failing with `/bin/sh: Syntax error: word unexpected (expecting ")")`. Regression coverage: `test_exec_command_requotes_args` in `tests/test_main.py`.

To run an actual remote shell snippet (pipes, redirects, `&&`), pass it explicitly to a shell: `cinna exec bash -c 'a | b > c'`.

---

## Remote Chat (`cinna chat`)

`cinna chat` lets a local coding agent **test the agent it is building** by driving a real platform conversation session — exercising the production path (permission checks, agent-env calls, the model/SDK the platform selects) rather than a local mock. It lives in `chat.py` and runs entirely through the **account workspace's api-proxy** (`AccountClient`), so it needs an account workspace (`.cinna/account.json`) — found by walking up from the cwd, exactly like the other account verbs, so it works from a synced `agents/<slug>/` folder too.

### Why polling, not streaming

The platform's send-message route (`POST /sessions/{id}/messages/stream`) returns a **JSON ack immediately** and runs the agent turn asynchronously; the live events go out over a Socket.IO room *and* are persisted onto each message's `message_metadata.streaming_events`. The api-proxy is a buffered JSON hatch (it rejects `text/event-stream`), so `cinna chat` never reads the stream. Instead it:

1. Creates the session (`POST /sessions/`, mode `conversation` by default) — or resumes the one passed to `--resume`.
2. Uploads each `--file` and collects the returned file ids.
3. Records the current message count as a cursor, then sends the message (`file_ids` carry the attachments).
4. **Polls** `GET /sessions/{id}/messages?offset=<cursor>` (messages are ordered ascending by `sequence_number`, so `offset` is the cursor) and `GET …/messages/streaming-status` (`{is_streaming}`) until the turn settles — `is_streaming` false with no message flagged `streaming_in_progress`. A start-grace window covers env wake / queueing before the turn begins; an overall `--timeout` bounds the wait.

Each finalized message is emitted as one NDJSON line (`session` / `upload` / `message` / `status` / `done`); the in-progress assistant message is held back (its content is still growing) and emitted once final. Every `message` also carries the agent's reasoning/tool trace under **`events`** — the normalized `streaming_events` (thinking blocks, `tool` calls with their full `tool_input` payloads, tool results), with the bookkeeping/`attachment` entries stripped (attachments are surfaced separately). The final coalesced text stays in `content`; the trace shows *how* the agent got there. `--no-events` drops the trace; `--pretty` swaps NDJSON for a Rich transcript. Ctrl-C calls `POST …/messages/interrupt` and exits 130.

### Attachments

Agents attach workspace files to replies via `<cinna_attach>` tags; the backend materializes them and both injects an `attachment` streaming event (`metadata.file_id` / `filename` / `mime_type` / `size`) and lists them under the message's `files[]` with `source == "agent_attachment"`. `chat.py` collects attachments from the streaming events (preferred) with the `files[]` list as a replay fallback, dedups by file id, and downloads each via the proxy (`GET /files/{id}/download`) into `./cinna-chat-files/<session_id>/`. Because the proxy buffers the response, downloads are bounded by its **8 MiB** response cap; larger files surface a clear `PlatformError` instead of a partial write.

### File upload — the one dedicated route

The api-proxy is JSON-only and cannot carry a multipart body, and neither the account token nor a per-agent token may call `/files/upload` directly. So uploading a local attachment uses a dedicated account-CLI route, **`POST /api/v1/cli/account/files/upload`** (multipart, account-token auth), added alongside the other `/cli/account/*` routes; it creates a `File` owned by the account user and returns `FileUploadPublic` whose `id` goes into the message's `file_ids`. This is the only part of `cinna chat` that does not ride the api-proxy.

---

## Bootstrap Flow

```
┌───────────────────────┐                  ┌──────────────────────────────┐
│ Platform UI           │  copy / paste    │ Local Terminal                │
│ [Local Development]   │ ────────────────►│ $ curl -sL …/api/cli-setup/… │
│ [Copy setup command]  │                  │     | python3 -               │
└───────────────────────┘                  └──────────────────────────────┘
```

1. UI generates a setup token (15 min, single-use).
2. The `curl | python3` bootstrap script (served by the backend, not in this repo):
   - Checks Python 3.10+.
   - Installs/upgrades `cinna-cli` (via `uv tool install`, `pipx`, or `pip`).
   - Runs `cinna setup <url>`.
3. `cinna setup` (in `bootstrap.py`) runs the 5-step flow:
   1. Exchange setup token for CLI token + agent info (`POST /api/cli-setup/{token}`).
   2. `GET /sync-runtime`, detect local Mutagen, prompt to install if missing/mismatched.
   3. `GET /workspace` (one-shot tarball), extract to `./workspace/`.
   4. `GET /building-context` → write `BUILDING_AGENT.md` + mirror referenced prompt docs; generate `CLAUDE.md`, `.mcp.json`, `opencode.json`, `.gitignore`.
   5. Write `mutagen.yml` and call `sync_session.start()` — the continuous sync session is live.

---

## Security Model

### Authentication

```
Setup token (15 min, single-use)
    │  POST /api/cli-setup/{token}
    ▼
CLI token (JWT, 7-day rolling window, revocable)
    │  Authorization: Bearer <jwt>   — HTTP + WebSocket
    ▼
Backend validates on every request:
    1. JWT signature + expiry
    2. DB lookup by token_id; is_revoked = False
    3. Agent exists and user still owns it
    4. last_used_at refreshed, expires_at rolled forward
```

### Token refresh

When a CLI token expires (or is revoked) the normal remedy is `cinna set-token <token_or_url>` from inside the agent directory. Internally it reuses `_exchange_setup_token()` from `bootstrap.py` — the same helper that backs `cinna setup` — so the server side is a plain re-run of `POST /api/cli-setup/{token}`. The workspace's existing `platform_url` is used as the fallback when a bare token is pasted, which lets agents registered against different platforms each refresh from their own directory without extra flags. The workspace tarball is **not** re-downloaded, and `CLAUDE.md` / `BUILDING_AGENT.md` / `.mcp.json` / `opencode.json` are left in place — only the stored CLI token changes.

**Account tokens** refresh without a paste. `cinna login` (run inside an account workspace) drives the platform's RFC 8628 device-authorization flow: `POST /account/login/start` returns a short code + verification URL, the user clicks **Authorize** in the browser (already signed in), and the CLI polls `POST /account/login/poll` until it receives the fresh token, which it writes back into `.cinna/account.json` in place. The poll endpoint always returns HTTP 200 with a `status` field (`authorization_pending` / `slow_down` / `authorized` / `access_denied` / `expired_token`) — a deliberate divergence from RFC 8628's 400+`error` shape. Run from a fresh folder, the same command bootstraps a new account workspace instead of resuming one.

**Bulk repair** is `cinna doctor`. It reconciles the `~/.cinna/agents.json` registry against the Mutagen daemon and heals the state that drifts as agents come and go — registry entries whose workspace was deleted, sessions halted on a deleted local root or stuck retrying a dead remote env, and orphaned sessions (Mutagen has no "stop after N failures" knob, so these retry forever until terminated). It prints the live `cinna-*` session inventory up front — each tagged with the agent and folder it serves — then walks three ordered, separately-confirmed steps (each defaulting to Yes): **delete stalled sessions**, **terminate active sessions** (the healthy leftovers, recreated on the next `cinna dev`), and **refresh tokens**. Expired **per-agent** tokens under an account workspace are re-minted automatically through the parent account token; when the **account** token has itself expired, doctor groups the blocked agents into a single "run `cinna login`" finding instead of attempting re-mints that would 401. Standalone agents are reported for a manual `cinna set-token`.

### Authorization

| Resource | Rule |
|----------|------|
| `/workspace` | Token owner must own the agent |
| `/building-context` | Same |
| `/knowledge/search` | Same |
| `/sync-stream` (WebSocket) | Same; mid-session revocation is polled every 30s by the platform |
| `/exec` | Same; the remote env holds credentials and enforces its own limits |

### Input validation (CLI-side)

- Workspace tarball extraction validates against path traversal (`../`), absolute paths, symlinks, oversized files (>100MB).
- `cinna-sync-ssh` parses the agent_id from argv (`user@cinna-agent-<uuid>`) and resolves the platform URL + CLI token from the `~/.cinna/agents.json` registry; the resolved URL — never the argv host — decides where the WebSocket connects.
- `~/.cinna/agents.json` holds live CLI JWTs and is written with `0600`; `~/.cinna/` is a per-user directory.
- CLI token lives in `.cinna/config.json` (`.gitignore`d); `.cinna/` is gitignored by default.

---

## Platform API Endpoints

| Method | Route | Auth | Purpose |
|--------|-------|------|---------|
| POST | `/api/cli-setup/{token}` | Token | Exchange setup token for CLI token |
| GET  | `/api/v1/cli/agents/{id}/workspace` | CLI JWT | One-shot tarball for initial clone |
| GET  | `/api/v1/cli/agents/{id}/building-context` | CLI JWT | Assembled building prompt |
| POST | `/api/v1/cli/agents/{id}/knowledge/search` | CLI JWT | Knowledge base search (via MCP proxy) |
| GET  | `/api/v1/cli/agents/{id}/sync-runtime` | CLI JWT | Required Mutagen version + hash (also used by `cinna list` / `cinna status` as a cheap token-validity probe) |
| GET  | `/api/v1/cli/git-coordinates` | CLI JWT | Agent's git-versioning coordinates — **no `{id}`**, derived from the token (see "Git Versioning"). 404 on older backends ⇒ treated as not-versioned |
| POST | `/api/v1/cli/agents/{id}/exec` | CLI JWT | Streaming SSE command execution |
| WSS  | `/api/v1/cli/agents/{id}/sync-stream` | CLI JWT | Mutagen transport tunnel |
| POST | `/api/v1/cli/account/login/start` | None | Begin a `cinna login` device-authorization request |
| POST | `/api/v1/cli/account/login/poll` | None | Poll a `cinna login` request — always HTTP 200 + `status` |
| POST | `/api/v1/cli/account/agents/{id}/mint` | Account token | Mint a per-agent CLI token (`cinna agent sync`, `cinna doctor` re-mint) |
| POST | `/api/v1/cli/account/api-proxy` | Account token | Buffered JSON escape hatch — `cinna api`, and the transport for every `cinna chat` session/message call |
| POST | `/api/v1/cli/account/files/upload` | Account token | Multipart upload for `cinna chat --file` (the proxy can't carry multipart) |
| GET/PATCH | `/api/v1/cli/account/improvement-requests[/{id}[/archive]]` | Account token | Improvement requests received on the agents the account owns (`cinna improve`); the `/archive` route returns a binary ZIP the JSON-only proxy can't carry |

`cinna chat` reaches the conversation API **through** the api-proxy (so these are inner routes, not CLI routes): `POST /sessions/`, `GET /sessions/{id}`, `GET /sessions/{id}/messages`, `POST /sessions/{id}/messages/stream`, `GET /sessions/{id}/messages/streaming-status`, `POST /sessions/{id}/messages/interrupt`, and `GET /files/{id}/download`.

The account-workspace surface adds the broader `/api/v1/cli/account/*` route group (login, agents, credentials, connect, schedules, status, api-proxy, files/upload); only the routes the sync / login / doctor / chat paths use are listed here.

Endpoints that were part of the old Docker-replica model (`build-context`, `workspace` POST, `workspace/manifest`, `credentials`) have been removed from the backend and from this CLI.

---

## Testing

- `pytest` + `respx` for HTTP mocking
- `click.testing.CliRunner` for CLI tests
- `tmp_path` fixture for filesystem operations

```bash
uv run pytest -v
uv run ruff check src/
uv run ruff format --check src/
```

---

## Release Management

The CLI is published to [PyPI](https://pypi.org/project/cinna-cli/) by `.github/workflows/publish.yml`, which runs on any pushed tag matching `v*` (and via manual `workflow_dispatch`). It builds the sdist + wheel with `uv build`, runs `twine check`, then publishes through PyPI **Trusted Publishing** (OIDC — no stored API token) from the GitHub `pypi` environment.

### Cutting a release

Versioning is SemVer (`MAJOR.MINOR.PATCH`); a patch release is the common case. From a clean `main` with the changes you want to ship already merged:

1. **Bump the version** — edit `version` in `pyproject.toml` (e.g. `0.1.4` → `0.1.5`).
2. **Refresh the lockfile** — run `uv lock` so the `cinna-cli` entry in `uv.lock` matches the new version. These two files are the *only* changes in a release commit.
3. **Commit** with the exact message `release vX.Y.Z` (this is the established convention — release commits touch nothing but `pyproject.toml` + `uv.lock`).
4. **Tag** the release commit: `git tag vX.Y.Z` (lightweight tag, on the `release` commit).
5. **Push both** the branch and the tag: `git push origin main && git push origin vX.Y.Z`. Pushing the tag is what triggers the publish workflow.

```bash
# version already bumped in pyproject.toml
uv lock
git add pyproject.toml uv.lock
git commit -m "release v0.1.5"
git tag v0.1.5
git push origin main && git push origin v0.1.5
```

### Manual approval gate

> **The release manager must manually approve the PyPI publish.** Pushing the tag only *starts* the workflow — the `publish` job targets the protected `pypi` environment, which requires a reviewer to sign off before it runs. Go to **https://github.com/opencinna/cinna-cli/actions**, open the run for the tag you just pushed, and **approve the deployment**. Until you approve, the build completes but nothing is published to PyPI.

### Required changes checklist

| File | Change | Why |
|------|--------|-----|
| `pyproject.toml` | `version = "X.Y.Z"` | The single source of truth for the published package version. |
| `uv.lock` | regenerated via `uv lock` | Keeps the lockfile's `cinna-cli` entry in sync; CI builds from a consistent tree. |
| git tag `vX.Y.Z` | created on the release commit | The only trigger for the publish workflow. |

`pyproject.toml` is the **only** place the version lives. `src/cinna/__init__.py` derives `__version__` at runtime from the installed package metadata (`importlib.metadata.version("cinna-cli")`), which is what `cinna --version` reports — so it tracks `pyproject.toml` automatically and never needs a manual bump. (It falls back to `0.0.0+unknown` only when run from a source tree with no installed metadata.)

---

## Tech Stack

| Component | Choice | Rationale |
|-----------|--------|-----------|
| CLI framework | Click | Subcommand support, option groups, good UX |
| HTTP client | httpx | Streaming, modern, consistent sync + async |
| WebSocket client | websockets | Minimal dep for the sync shim |
| Sync engine | Mutagen | Battle-tested bidirectional sync; OSS; cross-platform |
| Terminal | Rich | Spinners, panels, tables |
| Live TUI | Textual | Three-tab interactive view (`cinna dev`); built on Rich |
| MCP | mcp SDK | Official protocol SDK |
| Tests | pytest + respx | Standard; respx is purpose-built for httpx |
| Build | Hatchling | Minimal, standards-compliant |
| Python | >= 3.10 | Matches platform backend |

---

## Out of Scope (Future)

- **Interactive stdin for `cinna exec`** — REPLs, debuggers.
- **Port forwarding** — `cinna forward local:remote` (Mutagen supports it).
- **Post-sync hooks** — e.g. `uv sync` on `pyproject.toml` change.
- **Web UI conflict resolution** — the in-TUI Conflicts tab covers the common case; editor-based 3-way merge stays the fallback for ones the TUI can't handle (e.g. asymmetric directory/file conflicts).
- **Multi-device presence UI**.
- **Bundled Mutagen daemon** — stay with a user-installed daemon.
- **Telemetry pipe** to the backend.

---

## Feature Documentation

Per-feature docs live under `docs/features/{feature}/` in up to three layers:
`{feature}.md` (business logic / reasoning), `{feature}_tech.md` (implementation +
file refs), `{feature}_acceptance.md` (live e2e scenarios a testing agent runs
against a real environment). Author new ones with the `/cinna-cli.feature.doc`
command (`.claude/commands/cinna-cli.feature.doc.md`) and validate references with
`scripts/check_docs_references.py`.

| Feature | Docs | Summary |
|---------|------|---------|
| Git Versioning (`cinna git`) | [business](features/git_versioning/git_versioning.md) · [tech](features/git_versioning/git_versioning_tech.md) · [acceptance](features/git_versioning/git_versioning_acceptance.md) | Version an agent's workspace with git against its external remote (commit/push/pull/rollback) alongside live Mutagen sync. |

---

## Related Projects

- **cinna-core** — the platform backend. Hosts the API routes this CLI calls, the agent runtime, env core, building mode, and the web UI. Source plan for this CLI feature: `cinna-core/docs/drafts/cinna-cli-live-sync_plan.md`.
