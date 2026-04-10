# Cinna CLI — Project Context

> This document is designed to orient a human or LLM working on the cinna-cli codebase for the first time. It explains **why** this project exists, **how** it fits into the larger Cinna Core platform, the key concepts and terminology, the architecture, and the important design decisions.

---

## What is Cinna Core?

**Cinna Core** is a platform for building and running AI agents. Each agent is a self-contained unit with Python scripts, prompt files, a webapp dashboard, integration credentials, and a knowledge base. Agents run inside managed cloud environments — Docker containers with a specific Python runtime, system packages, and mounted workspace files.

The platform has two main modes of interaction with an agent:

- **Building mode** — An AI-powered workflow where a cloud-based LLM (the "building agent") develops and iterates on the agent's scripts, prompts, and configurations. The building LLM receives a system prompt (the **building prompt**) assembled by the **env core** (the runtime engine inside the agent's container). This prompt includes the agent's role, scripts catalog, workflow/entrypoint/refiner prompts, credential documentation, knowledge topics, handover config, and plugin instructions.

- **Conversation mode** — End users interact with the finished agent via chat.

The platform also manages scheduling, triggers, email integration, A2A (agent-to-agent) protocol, MCP serving, task management, and session history. None of these are relevant to local development — they stay in the cloud.

## What is Cinna CLI?

**Cinna CLI** (`cinna-cli`) is a local development tool that lets developers work on agents outside of the cloud building mode. Instead of the platform's cloud LLM making changes, a developer uses their own editor, terminal, and AI coding tools (Claude Code, opencode, Cursor, etc.) to develop the agent locally.

The core idea: **replicate the agent's cloud environment on the developer's machine**, so local development produces the same results as cloud execution.

### What local dev is for

- Writing and testing Python scripts
- Testing credential integrations (API keys, OAuth tokens)
- Installing and validating Python/system packages
- Writing and iterating on prompt files (workflow prompts, entrypoint prompts, refiner prompts)
- Building webapp dashboards and data endpoints
- Preparing output files and reports

### What stays in the cloud

- Production sessions (building mode, conversation mode)
- Schedulers, triggers, email integration
- A2A / MCP protocol serving
- Task management, handovers
- Session history, activity logging

## Why does this exist?

Cloud building mode is powerful but has friction points for experienced developers:

- No choice of editor or AI tool — you use what the platform provides
- Iteration cycles go through the platform, which adds latency
- No local debugging, breakpoints, or custom tooling
- Hard to use version control workflows developers are used to

Cinna CLI bridges this gap. A developer clicks "Local Development" in the platform's agent Integrations tab, runs a bootstrap command, and gets a fully functional local copy of the agent. They edit with their preferred tools, run scripts in the Docker container via `cinna exec`, and sync changes back to the platform with `cinna push`.

---

## Glossary

### Agent
A self-contained automation unit on the Cinna Core platform. An agent has a name, a template, scripts, prompts, a webapp, credentials, and a knowledge base. Each agent runs inside its own environment. Agents have unique IDs (`agent_id`) and are owned by a user.

### Environment
The runtime container configuration for an agent on the platform. Defines the Docker image, Python version, system packages, and mounted workspace. Each agent has one environment, identified by an `environment_id`. The environment may be suspended when idle and auto-activated when needed (e.g., when fetching building context).

### Template
The base configuration type for an agent (e.g., `general-env`, `python-env-advanced`). Determines the starting Dockerfile, default packages, and workspace structure. Templates are stored as directories in the backend under `backend/app/env-templates/{template}/`.

### Workspace
The collection of files that make up an agent's working directory. Located at `workspace/` in the local project. Contains:
- `scripts/` — Python scripts that the agent executes
- `docs/` — Prompt files (`WORKFLOW_PROMPT.md`, `ENTRYPOINT_PROMPT.md`, `REFINER_PROMPT.md`)
- `webapp/` — HTML/CSS/JS dashboard and Python data endpoints
- `files/` — Output files, reports, CSVs, data
- `credentials/` — Integration credentials (pulled from platform, not synced)
- `workspace_requirements.txt` — Python package dependencies
- `workspace_system_packages.txt` — System package dependencies (apt)

### Build Context
A tarball downloaded from the platform containing everything needed to build the agent's Docker container locally. Contents:
- `Dockerfile` — from the agent's template
- `docker-compose.yml` — generated specifically for local use (simplified: no networks, no health checks, just a runtime box with workspace volume mount)
- `pyproject.toml` + `uv.lock` — Python dependencies
- `app/core/` — the env core files (same code that runs inside the production container)

Stored locally at `.cinna/build/`. The `docker-compose.yml` mounts `workspace/` as `/app/workspace` inside the container, so edits are instantly visible without rebuilding.

### Env Core
The runtime engine inside the agent's Docker container. In production, env core handles server operations, prompt generation, session management, etc. For local development, the env core code is baked into the Docker image (included in the build context) but **no server runs** — the local container is just a runtime sandbox. The env core's **prompt generator** is still important: the backend proxies to it to assemble the building prompt when the CLI calls `GET /agents/{id}/building-context`.

### Building Mode / Building Prompt
The platform's cloud-based AI development workflow. The **building prompt** is the full system prompt assembled by the env core's prompt generator. It includes:
- The agent's role and development guidelines (`BUILDING_AGENT.md` template)
- Existing scripts catalog (`scripts/README.md`)
- Current workflow prompt (`WORKFLOW_PROMPT.md`)
- Entrypoint and refiner prompts
- Credential documentation (redacted values)
- Knowledge base topics available for queries
- Handover configuration (if any)
- Plugin instructions (if any)

This same prompt is pulled by Cinna CLI and saved as `BUILDING_AGENT.md`, so local AI tools receive identical context to the cloud building agent.

### Building Context (API response)
The API response from `GET /api/v1/cli/agents/{id}/building-context`. Contains:
```json
{
  "building_prompt": "You are a building agent. Your role is to...",
  "building_prompt_parts": {
    "building_agent_md": "...",
    "scripts_readme": "...",
    "workflow_prompt": "...",
    "entrypoint_prompt": "...",
    "refiner_prompt": "...",
    "credentials_readme": "...",
    "knowledge_topics": ["gdrive", "slack"],
    "handover_config": "...",
    "plugin_instructions": "..."
  },
  "settings": {
    "agent_name": "my-agent",
    "template": "general-env",
    "sdk_adapter_building": "claude-code/anthropic",
    "model_override_building": null
  }
}
```

The `building_prompt` field is the authoritative, fully assembled version. The CLI uses it directly rather than trying to replicate the assembly logic. The backend gets it by proxying to the env core's prompt generator running inside the agent's remote environment (auto-activating it if suspended).

### Setup Token
A short-lived (15 minutes), single-use token generated by the platform UI when a developer clicks "Local Development" in the agent's Integrations tab. Embedded in the `curl | python3` bootstrap command. Exchanged for a CLI token via `POST /cli-setup/{token}`.

Backend model: `cli_setup_token` table — fields: `token` (random 32-char string), `agent_id`, `environment_id`, `owner_id`, `is_used`, `expires_at`. Expired/used tokens are cleaned up by a periodic background task.

### CLI Token
A JWT issued by the platform when a setup token is exchanged. Authenticates all subsequent API calls from the CLI. Stored in `.cinna/config.json`.

Key properties:
- **Rolling expiry**: expires after 7 days of **inactivity**, not 7 days from creation. Every successful API call renews the expiry window.
- **Revocable**: can be revoked from the platform UI (Integrations tab shows active sessions)
- **Scoped**: each token is tied to one agent and one user
- **Hash-stored**: the backend stores a SHA-256 hash, not the raw token

JWT payload: `{ "sub": "<cli_token_id>", "agent_id": "<uuid>", "owner_id": "<uuid>", "token_type": "cli", "exp": <timestamp> }`

The CLI only decodes the JWT locally (without signature verification) as a UX hint to detect expiration before making network calls. Real validation happens server-side on every request.

### Manifest
A SHA-256 hash map of all files in the workspace. Used for 3-way sync conflict detection. Format:
```json
{ "relative/path": { "sha256": "abc...", "size": 1234, "mtime": 1712567890.0 } }
```
The last-known manifest (from the previous sync) is stored at `.cinna/last_manifest.json`. The remote manifest is fetched from the platform via `GET .../workspace/manifest`.

### Knowledge Source
A documentation or data source attached to an agent on the platform. Has an `id`, `name`, and list of `topics`. Queried via the MCP proxy's `knowledge_query` tool. Backed by the platform's `KnowledgeSearchService` (vector search).

### MCP (Model Context Protocol)
An open protocol for connecting AI tools to external data sources and capabilities. Cinna CLI runs a local MCP server (`cinna mcp-proxy`) over stdio transport that exposes the agent's knowledge base as a `knowledge_query` tool. AI coding tools discover this server via:
- `.mcp.json` — for Claude Code
- `opencode.json` — for opencode

The MCP proxy is intentionally minimal: one tool, one backend call. If more MCP tools are needed later (task creation, agent handover, etc.), they are added as additional tool handlers in the same proxy.

### Platform
The Cinna Core backend — a web application (FastAPI) with API routes that manage agents, environments, credentials, knowledge sources, and building mode. The CLI communicates with it via HTTP (`client.py`). Base URL is stored in `config.platform_url`. The platform works identically for production (`https://app.example.com`) and local development instances (`http://localhost:8000`).

---

## Architecture

### System Overview

```
  +-----------------+     +-------------------+     +-------------------------+
  | User's IDE      |     | Cinna CLI         |     | Cinna Core Platform     |
  | Claude Code     |     |                   |     | (backend)               |
  | opencode        |     | cinna setup       |     |                         |
  | Cursor          |     | cinna dev (watch) |     | /cli-setup/{token}      |
  +--------+--------+     | cinna exec        |     | /api/v1/cli/agents/...  |
           |               | cinna push/pull   |     |   build-context         |
           | edits files   | cinna rebuild     |     |   workspace             |
           | directly      | cinna disconnect  |     |   credentials           |
           v               +--------+----------+     |   building-context      |
  ~/my-agent/workspace/             |                 |   knowledge/search      |
    scripts/                        | HTTPS           |                         |
    docs/                           | (JWT auth)      | Existing services:      |
    webapp/                         +--------+------->|   CredentialsService    |
    credentials/                             |        |   KnowledgeSearchService|
    files/                                   |        |   Docker adapter        |
                                             |        +-------------------------+
  MCP proxy (stdio)                          |
    knowledge_query  ────────────────────────+
                                             |
  +------------------------------------------+--------+
  | Local Docker Container                             |
  | (agent-dev-{name})                                 |
  |                                                    |
  | Same image as production.                          |
  | Python packages + system deps.                     |
  | No server running — just a runtime sandbox.        |
  | workspace/ mounted at /app/workspace (read-write). |
  +----------------------------------------------------+
```

### Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Replicate the container locally, don't proxy to remote | Eliminates latency, gives exact runtime parity, works offline for script execution |
| Mount workspace as Docker volume | Edit locally, run in container — no sync needed during development |
| No env core server inside local container | Container is just a runtime sandbox, not an agent; CLI already has auth to call backend. The platform's production entrypoint is overridden with `sleep infinity` via `docker-compose.override.yml` |
| Stateless containers, explicit lifecycle | Containers hold no state — workspace is mounted. `cinna dev` creates on start, removes on exit. `cinna env-up`/`env-down` for manual control. No auto-start, no orphaned background processes |
| MCP proxy in CLI, not in container | Container stays dumb; CLI has the auth token and knows the agent config |
| Push/pull for remote sync | Only needed when moving work to/from production, not during active development |
| Pull building context from backend (via env core), not assemble locally | Env core prompt generator is the single source of truth; new prompt components are picked up automatically without CLI changes |
| Config as single source of truth | All state in `.cinna/config.json`, no global state, every module testable via DI |
| Synchronous httpx client | CLI is not concurrent, simpler to reason about |
| Two context files (CLAUDE.md + BUILDING_AGENT.md) | Separates local dev instructions from agent-specific context; each can evolve independently |

### Why Two Context Files?

| Concern | `CLAUDE.md` | `BUILDING_AGENT.md` |
|---------|-------------|---------------------|
| Audience | Local AI tool (how to use cinna) | Local AI tool (how to build this agent) |
| Source | Static template baked into cinna-cli | Dynamic, pulled from env core via backend |
| Changes when | cinna-cli is updated | Agent's prompts/scripts/credentials change |
| Can grow with | Local dev tips, team conventions, cinna-specific workflows | Platform prompt generator improvements |

Without `BUILDING_AGENT.md`, the local AI tool would be a generic coding assistant. With it, the AI understands what the agent does, what scripts exist, what credentials are available, and how to develop new scripts that fit the existing workflow.

### Module Dependency Graph

```
main.py (CLI commands — Click)
  |
  +-- bootstrap.py (setup orchestration — uses all modules below)
  |
  +-- config.py      — .cinna/config.json: load, save, find workspace root
  +-- auth.py        — JWT token handling, Authorization headers
  +-- client.py      — PlatformClient: all HTTP calls to the backend
  +-- docker.py      — Docker Compose lifecycle: build, start, destroy, exec, status
  +-- sync.py        — Manifest diffing, tarball creation/extraction, push/pull logic
  +-- dev.py         — Dev mode: container lifecycle + bidirectional sync TUI
  +-- context.py     — Generate CLAUDE.md, BUILDING_AGENT.md, .mcp.json, opencode.json
  +-- mcp_proxy.py   — Async MCP stdio server for knowledge queries
  +-- console.py     — Rich terminal output: spinners, progress, warnings
  +-- logging.py     — File-based logging (cinna.log in cwd)
  +-- errors.py      — Exception hierarchy (CinnaError, AuthenticationError, etc.)
```

Each module is independently testable — dependencies are injected via function parameters, not global state.

### Local Directory Layout

After `cinna setup`, the agent's local directory looks like (agent name is normalized to lowercase dashes, e.g., "HR Manager Agent" → `hr-manager-agent/`):

```
hr-manager-agent/               (workspace root, normalized name)
  .cinna/                       (CLI internal state — do not edit)
    config.json                 (agent config, CLI token, platform URL)
    last_manifest.json          (file hashes from last sync)
    build/                      (Docker build context from platform)
      Dockerfile
      docker-compose.yml
      .env                      (AGENT_NAME for compose)
      pyproject.toml
      uv.lock
      app/core/                 (env core code, baked into image)
  workspace/                    (the agent's working files — this is what you edit)
    scripts/
    docs/
    webapp/
    files/
    credentials/
    workspace_requirements.txt
    workspace_system_packages.txt
  CLAUDE.md                     (auto-generated local dev instructions)
  BUILDING_AGENT.md             (building mode system prompt from env core)
  .mcp.json                     (MCP config for Claude Code)
  opencode.json                 (MCP config for opencode)
  .gitignore                    (excludes generated/sensitive files)
```

---

## How the Bootstrap Flow Works

```
Agent Integrations Tab               Local Terminal
+---------------------+              +------------------------------------------+
| [Local Development] |  copy/paste  | $ curl -sL https://app.example.com/      |
| [Copy setup cmd]    | -----------> |     cli-setup/tok_abc123 | python3 -     |
+---------------------+              +------------------------------------------+
```

1. Developer clicks "Local Development" in the agent's Integrations tab in the platform UI
2. Platform generates a **setup token** (15 min, single-use) and displays a `curl | python3` command
3. The **bootstrap script** (served by the platform, not in this repo) runs:
   - Checks prerequisites (Python 3.10+, Docker)
   - Installs/upgrades `cinna-cli` from PyPI (via `uv tool install`, `pipx`, or `pip`)
   - Calls `cinna setup` with the full URL or token (the CLI parses the platform URL from the input)
4. `cinna setup` (in this repo) accepts the token, URL, or the full curl command and parses out the platform URL + token. It runs the 6-step flow:
   1. Check Docker available
   2. Exchange setup token for CLI token + bootstrap payload (`POST /cli-setup/{token}`)
   3. Download build context, write `docker-compose.override.yml` (idle entrypoint + container/image naming), build Docker image
   4. Download workspace tarball, extract to `workspace/`
   5. Pull credentials
   6. Fetch building context from backend (proxied to env core), generate `CLAUDE.md`, `BUILDING_AGENT.md`, `.mcp.json`, `opencode.json`, `.gitignore`

Note: setup builds the image but does **not** start a container. The developer starts one explicitly with `cinna dev` (interactive, container removed on exit) or `cinna env-up` (background).

After this, the developer has a fully functional local environment.

---

## How Sync Works

Workspace sync uses **3-way manifest diffing** to detect which files changed where. This is the most complex part of the CLI.

Three manifests are compared:
1. **Local manifest** — SHA-256 hashes of current local files
2. **Remote manifest** — SHA-256 hashes from the platform (`GET .../workspace/manifest`)
3. **Last-known manifest** — SHA-256 hashes from the previous sync (`.cinna/last_manifest.json`)

For each file in the union of all three:
- If `local SHA != last_known SHA` → file changed locally
- If `remote SHA != last_known SHA` → file changed remotely
- If both changed and `local SHA != remote SHA` → **conflict**
- If both changed to the same SHA → no action needed (convergent edit)
- File only in local (not in last_known or remote) → new local file, include in push
- File in last_known but not in local → locally deleted, flag for remote deletion on push
- File only in remote (not in last_known or local) → new remote file, include in pull
- File in last_known but not in remote → remotely deleted, flag for local deletion on pull

**Push** (`cinna push`):
1. Compute local manifest
2. Fetch remote manifest
3. Diff against last-known manifest
4. If conflicts and not `--force`: warn and skip conflicted files. With `--force`: push local version of conflicted files
5. Tarball locally changed files and upload
6. Update last-known manifest (merged: remote state as base, overlaid with local state)

**Pull** (`cinna pull`):
1. Fetch remote manifest
2. Compute local manifest
3. Diff against last-known manifest
4. If conflicts and not `--force`: warn and skip conflicted files. With `--force`: pull remote version of conflicted files
5. Download remote workspace tarball and **selectively extract only changed files** (not the full tarball, which would overwrite locally-changed files)
6. Always refresh credentials
7. Always fetch building context from backend and regenerate `BUILDING_AGENT.md` + `CLAUDE.md`
8. Update last-known manifest (merged: local state + remote state)

**Typical development workflow:**
```
1. cinna dev                         # start container + live sync (or: cinna env-up)
2. Edit scripts, prompts locally     # using your editor / AI tool
3. cinna exec python scripts/x.py   # test in container (dev mode keeps it running)
4. Ctrl+C                            # stop dev mode, container removed
```

**Manual sync workflow** (without dev mode):
```
1. cinna env-up                      # start container
2. cinna pull                        # get latest from remote, refresh context
3. Edit scripts, prompts locally
4. cinna exec python scripts/x.py   # test
5. cinna push                        # sync to remote
6. cinna env-down                    # remove container when done
```

The push-then-pull cycle ensures `CLAUDE.md` always reflects the same prompt the cloud building mode would use, including all platform-side enrichments assembled by the env core.

**Exclusions:** `__pycache__`, `*.pyc`, `.DS_Store`, and the `credentials/` directory are excluded from sync. Credentials are managed separately via `cinna credentials`. Files > 100MB are skipped with a warning.

---

## Dev Mode (`cinna dev`)

For continuous development, `cinna dev` runs a live TUI with bidirectional auto-sync:

```bash
cinna dev                # watch and sync every 5s (default)
cinna dev --interval 10  # sync every 10s
```

**How it works:**

1. **Starts** the Docker container (via `docker compose up -d`)
2. Computes an initial manifest of the workspace
3. Every N seconds:
   - Recomputes the local manifest
   - Fetches the remote manifest from the platform
   - Runs the same 3-way diff algorithm as `push`/`pull` (local vs remote vs last-known)
   - Pushes locally changed files to remote
   - Pulls remotely changed files to local
   - Detects conflicts (same file changed on both sides) and skips them with a warning
   - Updates the last-known manifest
4. Continues until Ctrl+C
5. On exit: **removes the container** (`docker compose down`) — containers are stateless

**TUI display:**

The dev mode uses Rich Live to render an in-place updating panel:

```
╭─── cinna dev — HR Manager Agent ──────────────────────────────╮
│                                                                │
│  Agent          HR Manager Agent    Cycles     12              │
│  Files tracked  24                  Pushed     3               │
│  Sync interval  5s                  Pulled     1               │
│  Uptime         2m 15s              Conflicts  0               │
│                                                                │
│  Docker Environment                                            │
│  Name    agent-dev-hr-manager-agent-0a288ac1                   │
│  ID      a1b2c3d4e5f6                                          │
│  Status  running                                               │
│                                                                │
│  Activity                                                      │
│    12:42:20  ↑ 2 pushed                                        │
│               ↑ scripts/main.py                                │
│               ↑ docs/WORKFLOW_PROMPT.md                         │
│    12:42:30  ↓ 1 pulled                                        │
│               ↓ scripts/check_api.py                           │
│                                                                │
│                           Ctrl+C to stop                       │
╰────────────────────────────────────────────────────────────────╯
```

- **Status section**: agent name, tracked file count, sync interval, uptime
- **Docker Environment**: container name (usable with `docker exec`), short ID, status (refreshed each cycle)
- **Stats**: cumulative pushed/pulled/conflicts with color coding
- **Activity log**: rolling last 5 sync events (quiet cycles are not shown)
- Errors appear in red in the activity log

**Compared to manual push/pull:**

| | `cinna push` / `cinna pull` | `cinna dev` |
|---|---|---|
| Direction | Explicit, one direction per command | Automatic, bidirectional |
| Trigger | Manual | Polling (every N seconds) |
| Container | Must be started separately | Managed: starts on entry, removed on exit |
| Conflict detection | Yes, aborts on conflict | Yes, skips conflicts silently |
| Context refresh | Pull refreshes CLAUDE.md | No (use `cinna pull` for that) |
| Use case | Deliberate sync points | Continuous development |

Dev mode does not refresh `CLAUDE.md` or `BUILDING_AGENT.md` — use `cinna pull` when you need updated building context. If the remote manifest is unreachable, dev mode falls back to local-only push until the next cycle.

---

## Setup Input Parsing

`cinna setup` is flexible about what it accepts. The user can paste any of these directly:

```
cinna setup curl -sL http://localhost:8000/cli-setup/TOKEN | python3 -
cinna setup http://localhost:8000/cli-setup/TOKEN
cinna setup TOKEN
```

The `parse_setup_input()` function in `bootstrap.py` extracts the platform URL and token using regex to find a `/cli-setup/` URL pattern. If only a raw token is provided, it falls back to the `CINNA_PLATFORM_URL` environment variable (set by the bootstrap script).

This eliminates the hard dependency on `CINNA_PLATFORM_URL` — users can run `cinna setup` directly with a URL or curl command copied from the platform UI.

---

## Logging

All CLI operations log to `cinna.log` in the current working directory. The log captures:

- HTTP requests/responses (method, URL, status code, body size)
- Full error details for failed API calls (status, response body)
- Setup flow progress (token exchange, download sizes)

The log uses rotating file handling (5MB max, 3 backups). It is included in `.gitignore`.

Use `cinna -v` to also print debug logs to the terminal.

---

## Container Lifecycle

Containers are **stateless** — workspace files are mounted from the host via Docker volume, not copied. This means containers can be created and destroyed freely without data loss.

The CLI never starts containers silently. The developer controls when containers run:

| Command | Creates container | Removes container |
|---------|------------------|-------------------|
| `cinna dev` | On start | On Ctrl+C exit |
| `cinna env-up` | Immediately | — |
| `cinna env-down` | — | Immediately |
| `cinna exec` | Never (requires running) | Never |
| `cinna rebuild` | Never | Before rebuilding image |
| `cinna setup` | Never (builds image only) | — |
| `cinna disconnect` | — | Yes (+ image + config) |

This design prevents orphaned background containers that consume resources when no development is happening. If a developer needs a long-running container (e.g., for repeated `cinna exec` calls during a debugging session), they use `cinna env-up` and `cinna env-down` explicitly.

### Docker Compose Override

The platform's build context includes a production entrypoint (e.g., FastAPI server via `core/main.py`). For local dev, the container is just a runtime sandbox. During setup, the CLI writes a `docker-compose.override.yml` to `.cinna/build/` that:

- Replaces the entrypoint with `sleep infinity` (keeps the container idle)
- Sets a unique `container_name` from config (e.g., `agent-dev-hr-manager-agent-0a288ac1`)
- Sets a descriptive `image` name (e.g., `cinna-dev-agent-dev-hr-manager-agent-0a288ac1`)

Docker Compose automatically merges this override with the platform's `docker-compose.yml`. The override is written once and persists across rebuilds. It is idempotent — if it already exists, it is not overwritten.

### Container Status

Container status is queried via `docker compose ps --format json` from the build directory, which returns the actual container name, ID, and state regardless of Docker Compose version differences. The TUI in `cinna dev` shows this info so developers can use the container name/ID directly with Docker commands if needed.

## Cleanup: disconnect and disconnect-all

**`cinna disconnect`** (run from inside an agent directory):
- Removes the Docker container and locally-built images (`docker compose down --rmi local`) — does not touch base/pulled images
- Removes `.cinna/` config directory and generated files (`CLAUDE.md`, `BUILDING_AGENT.md`, `.mcp.json`, `opencode.json`, `cinna.log`)
- Preserves workspace files
- `--keep-image` flag skips image removal (only removes container)

**`cinna disconnect-all`** (run from the parent directory containing agent workspaces):
- Scans subdirectories for `.cinna/config.json`
- Lists all found agents with container names and status
- Requires explicit confirmation
- For each agent: removes container + locally-built images, then deletes the entire directory
- Cleans up `cinna.log` in the current directory

---

## Shell Completion

The CLI provides tab-completion for all commands, options, and arguments via Click's built-in completion support.

```bash
cinna completion --install        # auto-detect shell, append to rc file
cinna completion zsh              # print zsh script to stdout
cinna completion bash             # print bash script to stdout
cinna completion fish             # print fish script to stdout
eval "$(cinna completion zsh)"    # activate in current session only
```

The `--install` flag appends an eval line to the shell's rc file (`~/.zshrc`, `~/.bashrc`, or `~/.config/fish/completions/cinna.fish`). It checks for duplicates before writing.

---

## Platform API Endpoints

The CLI depends on these backend routes (implemented in the `cinna-core` repo):

| Method | Route | Auth | Purpose |
|--------|-------|------|---------|
| POST | `/cli-setup/{token}` | None (token-based) | Exchange setup token for CLI token + agent info |
| POST | `/api/v1/cli/setup-tokens` | User session | Generate a setup token (called by frontend) |
| GET | `/api/v1/cli/tokens` | User session | List active CLI tokens for current user |
| DELETE | `/api/v1/cli/tokens/{id}` | User session | Revoke a CLI token (disconnect session) |
| GET | `/api/v1/cli/agents/{id}/build-context` | CLI token | Download Docker build tarball |
| GET | `/api/v1/cli/agents/{id}/workspace` | CLI token | Download workspace tarball |
| POST | `/api/v1/cli/agents/{id}/workspace` | CLI token | Upload workspace tarball |
| GET | `/api/v1/cli/agents/{id}/workspace/manifest` | CLI token | Get remote file hash manifest |
| GET | `/api/v1/cli/agents/{id}/credentials` | CLI token | Get integration credentials |
| GET | `/api/v1/cli/agents/{id}/building-context` | CLI token | Get building prompt + settings (proxied to env core) |
| POST | `/api/v1/cli/agents/{id}/knowledge/search` | CLI token | Search knowledge base |

The `/cli-setup/` route is top-level (short URL for the curl command). All other routes are under `/api/v1/cli/`.

---

## Security Model

### Authentication Flow

```
Setup token (15 min, single-use)
    |
    |  POST /cli-setup/{token}
    |  Validates: not used, not expired, agent ownership
    v
CLI token (JWT, 7-day rolling, revocable)
    |
    |  Authorization: Bearer <jwt>
    |  on every subsequent API call
    v
Backend validates on each request:
    1. JWT signature + expiry
    2. DB lookup by token_id (sub claim)
    3. is_revoked = False
    4. Agent still exists and user still owns it
    5. Update last_used_at, renew expires_at to now + 7 days
```

### Authorization Rules

| Resource | Rule |
|----------|------|
| Build context | Token owner must own the agent |
| Workspace files (read/write) | Token owner must own the agent |
| Credentials (read) | Token owner must own the agent; full access (user accepts local exposure) |
| Knowledge search | Token owner must own the agent; proxied with agent's knowledge sources |
| Setup token generation | Authenticated user who owns the agent |
| Token revocation | Token owner or superuser |

### Input Validation (CLI-side)

- Tarball extraction validates against path traversal (`../`), absolute paths, symlinks, and oversized files (>100MB)
- Script execution runs inside the Docker container (sandboxed)
- Credentials are excluded from workspace sync and `.gitignore`d
- CLI token is stored in `.cinna/config.json`, which is `.gitignore`d

---

## Platform Integration Points

### Backend Services Reused

| Existing Service | How CLI Uses It |
|-----------------|-----------------|
| `CredentialsService` | `prepare_credentials_for_environment()` for credential pull |
| `KnowledgeSearchService` | Proxied through knowledge search endpoint |
| Docker adapter | `download_workspace_item()` for initial workspace clone |
| Env core prompt generator | Proxied through building-context endpoint (auto-activates environment if suspended) |
| Environment templates | Build context assembled from `backend/app/env-templates/{template}/` |

### Backend Components Added for CLI

| Component | File |
|-----------|------|
| `CLISetupToken` model | `backend/app/models/cli/cli_setup_token.py` |
| `CLIToken` model | `backend/app/models/cli/cli_token.py` |
| `CLIService` | `backend/app/services/cli/cli_service.py` |
| `CLIAuthService` | `backend/app/services/cli/cli_auth.py` |
| `CLIContextDep` dependency | `backend/app/api/deps.py` |
| CLI API routes | `backend/app/api/routes/cli.py` |

### Frontend Components

| Component | Location |
|-----------|----------|
| `LocalDevCard` | `frontend/src/components/Agents/LocalDevCard.tsx` |
| Added to | `AgentIntegrationsTab.tsx` grid (alongside A2A, MCP connectors, access tokens) |

The card shows: setup command with copy button and expiry countdown, active sessions list with disconnect buttons.

---

## Testing

Tests live in `tests/` and use:
- `pytest` as the test framework
- `respx` for mocking `httpx` HTTP requests
- `click.testing.CliRunner` for CLI command tests
- `tmp_path` fixture for filesystem operations

Every module has a corresponding test file. Run with:

```bash
uv run pytest -v
uv run ruff check src/
uv run ruff format --check src/
```

---

## Tech Stack

| Component | Choice | Rationale |
|-----------|--------|-----------|
| CLI framework | Click | Subcommand support, option groups, better UX than argparse |
| HTTP client | httpx | Async-capable (used sync here), streaming, modern API |
| Terminal output | Rich | Spinners, progress bars, colored panels, tables |
| MCP server | mcp SDK | Official SDK for Model Context Protocol stdio servers |
| Build backend | Hatchling | Lightweight, standards-compliant Python packaging |
| Testing | pytest + respx | De facto standard; respx is purpose-built for httpx mocking |
| Linter/formatter | Ruff | Fast, single-tool replacement for flake8 + black + isort |
| Python | >= 3.10 | Matches platform backend requirement |

---

## Future Enhancements (Out of Scope)

These are documented in the source plan but not currently implemented:

- **`cinna login` OAuth flow** — browser-based login without setup token
- **Multi-agent workspace** — `cinna clone` multiple agents into subdirectories
- **Image registry** — push/pull pre-built images instead of building locally
- **`cinna logs`** — stream session logs from remote environment
- **`cinna chat`** — interactive building-mode session in terminal
- **VS Code extension** — file explorer integration, inline exec, MCP tool panel
- **Additional MCP tools** — task creation, agent handover via MCP proxy
- **Webhook-based sync** — WebSocket push instead of manifest-based pull
- **Git integration** — auto-commit on push, branch per CLI session

---

## Related Projects

- **cinna-core** — The platform backend (separate repo, formerly `workflow-runner-core`). Hosts the API routes this CLI calls, the agent runtime, env core, building mode, and the web UI. The bootstrap script template also lives there. Source plan for this CLI feature: `cinna-core/docs/drafts/local-dev-cli_plan.md`.
