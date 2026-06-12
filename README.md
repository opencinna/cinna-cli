# cinna-cli

Local development CLI for [Cinna Core](https://github.com/opencinna/cinna-core) agents.

Work on agent scripts, prompts, and webapps locally with your own editor and AI tools. The CLI keeps your workspace continuously synced with the remote agent environment, streams commands to it, and wires up MCP integration — so the platform is the single source of truth for runtime and credentials.

## How It Works

Cinna Core agents run in managed cloud environments. `cinna-cli` does **not** run a local Docker container. Instead:

1. **Continuous sync** — [Mutagen](https://mutagen.io) keeps `./workspace` bidirectionally synced with the remote agent env over a WebSocket tunnel to the platform.
2. **Remote exec** — `cinna exec <cmd>` streams your command through the platform to the remote env, with live stdout/stderr and the remote process's exit code.
3. **MCP integration** — the local MCP proxy gives Claude Code / opencode access to the agent's knowledge base.

```
Your Editor / Claude Code
        │
        ▼
  workspace/              ← edit locally
        │
  cinna sync (Mutagen) ◄──► Remote Agent Environment   (no local container)
        │
  cinna exec <cmd>       ── streaming output
```

## Prerequisites

- **Python 3.10+**
- **[Mutagen](https://mutagen.io)** (version pinned by the platform — `cinna setup` checks and prompts to install)

## Getting Started

Setup is initiated from the Cinna Core platform UI. Click **"Local Development"** on your agent's page to get a bootstrap command:

```bash
curl -s https://your-platform.com/api/cli-setup/TOKEN | python3 -
```

This will:

1. Install `cinna-cli` (via `uv`, `pipx`, or `pip`)
2. Exchange the setup token for CLI credentials
3. Verify / prompt-install the required Mutagen version
4. Clone the workspace (one-shot tarball; Mutagen takes over afterwards)
5. Generate `CLAUDE.md`, `BUILDING_AGENT.md`, `.mcp.json`, `opencode.json`, `.gitignore`, `mutagen.yml`
6. Start the continuous sync session

After setup:

```bash
cd hr-manager-agent/
cinna dev                           # start a foreground dev session (live sync + TUI)
claude                              # open Claude Code (MCP tools auto-configured)
cinna sync status                   # see sync state from another terminal
cinna exec python scripts/main.py   # run a command in the remote env
cinna list                          # see every agent registered on this machine
```

## Commands

### `cinna setup <token_or_url>`

Initialize a local workspace. Accepts the setup token, the URL, or the full curl command from the platform UI.

The agent directory name is normalized to lowercase with dashes ("HR Manager Agent" → `hr-manager-agent/`).

### `cinna set-token <token_or_url>`

Refresh the CLI token on the current workspace without re-cloning. Run this from inside an existing agent directory when the stored token has expired — `cinna set-token` re-exchanges the setup token via `POST /api/cli-setup/{token}` and swaps the result into `.cinna/config.json` and `~/.cinna/agents.json` in place. Workspace files, `mutagen.yml`, and generated context files are left untouched.

Accepts the same input forms as `cinna setup` (curl command, URL, or bare token). When only a bare token is given, the platform URL is reused from the workspace's existing `.cinna/config.json` — so you can refresh each agent from inside its own directory even if different agents live on different platforms. The exchanged token must belong to the same agent as the workspace; mismatched agent IDs abort the refresh.

```bash
cd hr-manager-agent/
cinna set-token yWo36tbkdAOzrALxOEKq31_OA2iMelEg
```

### `cinna account setup <token_or_url>`

Initialize an **account workspace** — a multi-agent root from which you can discover your agents and attach per-agent workspaces without going back to the UI. Setup is initiated from **Settings → Channels → Local Development** on the platform, which emits a `curl | python3` one-liner (same pattern as the per-agent flow):

```bash
curl -sL https://your-platform.com/api/cli-setup/account/TOKEN | python3 -
```

Accepts the same input forms as `cinna setup` (full curl command, URL, or bare token — bare tokens need `CINNA_PLATFORM_URL`). Creates `my-cinna/` (override with `--dir`) containing:

```
my-cinna/
  .cinna/account.json    # account CLI token + platform/frontend URLs (0600, do not commit)
  CLAUDE.md              # orchestrator guide for AI tools
  context/               # platform docs, API reference, example scripts, worked playbooks
  agents/                # one standard per-agent workspace per `cinna agent sync`
```

Setup also downloads the **context package** into `context/` — curated platform docs (feature map at `context/platform/README.md`), a generated per-domain REST API reference (`context/api_reference/`), sample platform-API scripts (`context/examples/`), and worked end-to-end playbooks (`context/guides/`, e.g. `build-an-agentic-network.md`). The download is best-effort: if it fails, setup still succeeds with a warning and `cinna account refresh-context` fetches it later.

The account token is only used for the account-level endpoints (listing agents, minting per-agent tokens, the context package). Per-agent work always runs on each child workspace's own token. Revoking the account session in Settings disconnects every agent synced from it.

### `cinna account agents`

List the agents your account can access (run from inside the account workspace). For each agent: display name + ID, building rights (`✓ can build`, `view-only`, or `foreign install` — installed bundles are publisher-managed and can't be synced), whether a remote environment is active, and whether a local workspace already exists under `agents/`.

### `cinna account status`

One-shot summary of the account workspace: platform/frontend URLs, machine name, synced-agent count, and an account-token probe (`valid token` / `expired token` / `no connection`) — the account-level counterpart of `cinna status`.

### `cinna account refresh-context`

Re-download the context package and replace the account workspace's `context/` tree (run from inside the account workspace). Use it when the platform ships updated docs or API reference. The existing tree is only removed after a successful download — a failed refresh warns and leaves the previous `context/` intact.

### `cinna account user-workspace list | activate <name|id> | clear`

Choose the **active user workspace** for the account session. Workspace-scoped resources you create from the account workspace — new agents (`cinna agent create`) and the credentials they acquire — land in the active workspace, just like picking a workspace in the web sidebar before creating things. The selection is stored **client-side** in `.cinna/account.json`; the platform keeps no active-workspace state.

```bash
cinna account user-workspace list                 # show workspaces, marking the active one
cinna account user-workspace activate Sales       # by name or id
cinna account user-workspace activate default     # clear back to the Default (unassigned) workspace
```

### `cinna account credentials list | types | create | update | delete | share-with-agent`

Draft and wire the credentials your agents need — **without ever handling secret values**. The account CLI scaffolds a credential as a *draft* and attaches it to an agent; the **user fills the secret in the web UI** (the draft shows as "needs setup" until then). This lets a local coding agent set up everything an agent requires and simply tell the user what to fill in — they don't have to think about what to create or share.

The account token can never read or write a credential's secret value — these verbs touch only metadata and structure.

```bash
cinna account credentials types                                   # types + the fields the user must fill
cinna account credentials create --name "Stripe Key" --type api_token \
    --agent billing-agent                                         # create a draft and attach it in one step
#   → prints required fields (e.g. api_token) + a link to fill them in
cinna account credentials list                                    # name, type, status (complete / needs setup)
cinna account credentials share-with-agent <cred_id> --agent crm-agent
cinna account credentials update <cred_id> --name "Stripe (live)"
cinna account credentials delete <cred_id> --yes
```

A new draft lands in the account's [active user workspace](#cinna-account-user-workspace-list--activate-nameid--clear). Deletes reuse the platform's blast-radius gate (a publisher-provided credential in a published bundle with active installs needs `--force`). All write verbs require the `agent-developer` role.

### `cinna agent create <name> [--description TEXT]`

Create a new agent on the platform from the account workspace — no UI interaction. Thin client: only the name (and optional description) is sent; the backend applies all defaults (default AI credentials, env template, environment creation) exactly as creating from the UI does. The agent is created in the account's [active user workspace](#cinna-account-user-workspace-list--activate-nameid--clear) (if one is set). Prints the created agent's ID and web UI link, plus a hint to attach a local workspace with `cinna agent sync <name>`. Requires the `agent-developer` role (403 otherwise). Template selection is not supported yet — agents always get the server default.

```bash
cinna agent create "CRM Agent" --description "Tracks customer accounts"
cinna agent sync crm-agent
```

### `cinna agent sync <agent>`

Mint a per-agent CLI token (no UI interaction) and materialize a standard workspace under `agents/<slug>/`. `<agent>` is the display name, slug, or agent ID from `cinna account agents`. The result is identical to what `cinna setup` produces — own `.cinna/config.json`, registry entry, generated `CLAUDE.md` / `BUILDING_AGENT.md` / MCP configs / `mutagen.yml`, and the initial workspace clone — so afterwards:

```bash
cd agents/hr-manager-agent/
cinna dev
```

works exactly as for a manually set-up agent. Synced agents also appear in `cinna list` and in the agent's Integrations-tab session list like any other CLI session. The backend gates minting on building rights: foreign bundle installs and view-only agents are rejected with the server's error message.

### `cinna agent unsync <agent>`

Detach a synced workspace: stop its sync session, revoke the minted CLI token server-side (via the account-scoped revoke endpoint, authenticated with the account token; idempotent), then perform the equivalent of `cinna disconnect`: remove `.cinna/`, generated files, and the registry entry. Workspace files under `agents/<slug>/workspace/` are preserved. The revoke degrades gracefully — if it fails (no connection, or a workspace synced before token-id tracking), a warning is printed and the local teardown still completes; the token then expires on its own or can be revoked from the agent's Integrations tab.

### `cinna connect agent-api --producer <agent> --consumer <agent> [--label TEXT] [--read-only]`

Wire one agent to another's REST API from the account workspace. Resolves both agents (name, slug, or ID), mints a producer API token, and attaches it to the consumer as a credential — which rides the consumer's normal credential sync into its remote environment, so no key ever touches your machine. Prints the credential ID, token prefix, base URL, and spec URL. `--read-only` restricts the consumer to read-only access; `--label` names the credential. Backend errors surface verbatim: 400 if the producer's REST API is disabled, 403/404 for ownership violations.

```bash
cinna connect agent-api --producer crm-agent --consumer hr-manager-agent --read-only
```

### `cinna connect mcp --producer <agent> --consumer <agent> [--label TEXT] [--conversation-only|--building-only]`

Wire one agent to another's agent2agent MCP connector. The consumer is resolved from your agents; the producer is resolved against the platform's discoverable-connectors listing (it must expose an agent2agent MCP connector your account is allowed to consume — the error lists the discoverable options otherwise). By default the connection is enabled in both conversation and building modes; scope it with `--conversation-only` / `--building-only`. Prints the credential ID, endpoint, transport, and status — plus an authorize URL to open if the connector requires OAuth.

```bash
cinna connect mcp --producer crm-agent --consumer hr-manager-agent --building-only
```

### `cinna api <METHOD> <path> [--json TEXT | --data @file.json] [--query k=v ...]`

Generic escape hatch into the platform API, authenticated with the account token (run from the account workspace). `<path>` is relative to the API root — `agents`, `agents/<id>` — no `/api/v1` prefix. The endpoint catalogue ships in the account workspace under `context/api_reference/`.

- `--json '<obj>'` or `--data @file.json` supply a JSON request body (mutually exclusive); repeatable `--query k=v` supplies query parameters (repeating a key builds a list).
- The inner response is passed through verbatim: the body prints to stdout (pretty-printed for JSON) and the exit code is `0` for 2xx and `1` for an inner 4xx/5xx — so it composes in shell pipelines.
- When the escape hatch itself refuses the call, the detail prints to stderr and the exit code is `2`: policy denials (credentials, user management, admin, CLI, MFA/auth, and streaming routes are excluded — shown as `blocked by platform policy: …`), rate limiting (429, with the Retry-After delay), and request/response size caps (413/502).

```bash
cinna api GET agents
cinna api GET agents --query limit=5
cinna api PATCH agents/3fa85f64-5717-4562-b3fc-2c963f66afa6 --json '{"description": "updated"}'
cinna api POST tasks --data @task.json
```

### `cinna dev`

Start a foreground dev session — creates / resumes the Mutagen sync session for this workspace and attaches the terminal to a two-tab TUI (status + raw Mutagen details). Ctrl-C terminates the session; sync does not outlive the TUI. To observe sync from another terminal without affecting it, use `cinna sync status`.

### `cinna redev`

Like `cinna dev`, but conflicts surfaced by the initial reconciliation are resolved automatically in favor of the **remote** version. Use it to resume work on an agent that was modified from the platform side while your local copy sat idle — same connection, same credentials, no re-setup; the remote data simply wins the startup divergence.

The displaced local versions are backed up under `.cinna/sync/redev-backup/<timestamp>/` before being overwritten. Only startup conflicts are auto-resolved — conflicts that arise later in the session are surfaced normally (Conflicts tab / `cinna sync conflicts`).

```bash
cd hr-manager-agent/
cinna redev    # remote wins the initial conflicts, then a normal dev session
```

### `cinna sync status | conflicts | push | pull | resolve`

Inspect and drive the sync session. `status` / `conflicts` are read-only views (safe alongside a live `cinna dev`); `push` / `pull` / `resolve` are for scripted (headless) builders who aren't running the TUI. All accept `--agent <ref>` to target a synced child workspace from the account root.

- `status` — state, pending changes, conflict count. Warns loudly when conflicts mean your edits aren't fully live.
- `conflicts` — list conflicted paths (sourced from the Mutagen daemon, so it agrees with `status`; two-way-safe writes no `.conflict.*` files on disk).
- `push [--force]` — ensure a session, then flush and block until settled. `--force` resolves any parked conflicts in favor of **local** first ("my local is the truth"). The session persists in the daemon so later edits keep syncing.
- `pull [--force]` — the mirror; `--force` resolves in favor of **remote** (e.g. after the backend regenerates managed files).
- `resolve --prefer local|remote` — clear parked conflicts in one command. `local` deletes the remote losing copies (your version propagates out); `remote` backs up your local copies under `.cinna/sync/` and takes the container's version. Replaces the manual kill/delete/restart dance.

### `cinna exec <command…>`

Stream a command through the platform to the remote agent environment. Output streams back live; Ctrl+C aborts. Exit code matches the remote process.

The command runs with the **workspace root (`/app/workspace`) as its working directory**, so relative paths resolve against the synced workspace — e.g. `cinna exec python scripts/main.py` runs `/app/workspace/scripts/main.py` (the same cwd the scheduler uses). No need to prefix paths with `/app/workspace/`.

Arguments pass through transparently — each token is re-quoted before being sent, so spaces and shell metacharacters inside an argument survive intact. Use ordinary single-level quoting, exactly as for a local command. To run a shell snippet (pipes, redirects, `&&`), pass it to a shell explicitly: `cinna exec bash -c '…'`.

With `--agent <agent>`, run from an account workspace root against the named synced agent (name, slug, or ID) using that child workspace's own token — the agent must already be attached with `cinna agent sync`.

```bash
cinna exec python scripts/main.py
cinna exec pip install pandas
cinna exec bash -c 'ls -la'
cinna exec python -c 'import sys; print(sys.argv)' "a b"
cinna exec --agent crm-agent python scripts/main.py   # from the account root
```

### `cinna status`

One-shot summary of the agent + current sync state. Includes a backend probe (`GET /sync-runtime`) that reports whether the stored CLI token is still accepted — `valid token`, `expired token`, or `no connection`. Use `cinna set-token` to refresh an expired token.

### `cinna list`

List every agent registered on this machine (from `~/.cinna/agents.json`). Three columns:

1. **Agent** — display name on top, full agent ID below.
2. **Location** — workspace path on top, platform UI link below. Missing directories are flagged in red.
3. **Sync** — Mutagen session state on top (`active` / `paused` / `connecting` / `error`), plus a per-agent backend probe (`valid token` / `expired token` / `no connection`) on the bottom. The probes run in parallel with a short timeout so the view stays snappy even with many registered agents.

### `cinna disconnect`

Stop sync, remove `.cinna/` config and generated files (`CLAUDE.md`, `BUILDING_AGENT.md`, `.mcp.json`, `opencode.json`, `mutagen.yml`). Workspace files are preserved.

### `cinna disconnect-all`

Scan the current directory for every cinna workspace (directories containing `.cinna/config.json`), stop each sync session, and delete the directories entirely. Prompts for confirmation and prints a summary of what was removed.

### `cinna completion [SHELL] [--install]`

Output or install shell completion for bash, zsh, or fish.

## Workspace Structure

After setup, the agent directory looks like:

```
my-agent/
  .cinna/                 # CLI config (do not edit)
    config.json
  workspace/              # Continuously synced with the remote env
    scripts/              # Bundle-owned: agent Python scripts
    docs/                 # Bundle-owned: WORKFLOW/ENTRYPOINT/REFINER prompts
    webapp/               # Bundle-owned: dashboard + data endpoints
    knowledge/            # Bundle-owned: static integration docs
    files/                # Bundle-owned: static publisher-shipped assets
    app-data/             # Per-user persistent — NOT shipped in bundle revisions.
                          # Backed by a platform AppDataVolume keyed by (user_id, bundle_id);
                          # mounted on the platform at /app/workspace/app-data.
                          # Survives apply-update and uninstall/reinstall.
      storage/            #   long-lived runtime output (DBs, reports, derived data)
      uploads/            #   all user-supplied file uploads at runtime
                          #   (chat attachments, task attachments, MCP uploads)
      cache/              #   disposable caches
    credentials/          # Backend-managed; visible read-only on your side
    workspace_requirements.txt
    workspace_system_packages.txt
  mutagen.yml             # Sync rules (customizable)
  CLAUDE.md               # Local dev instructions for AI tools
  BUILDING_AGENT.md       # Building mode prompt pulled from the platform
  .mcp.json               # MCP config for Claude Code
  opencode.json           # MCP config for opencode
  .gitignore              # ignores workspace/credentials/ and workspace/app-data/
```

**Persistence tiers** mirror the platform's bundle/install model:

- **Bundle-owned** folders (`scripts/`, `docs/`, `webapp/`, `knowledge/`, `files/`, `workspace_requirements.txt`, `workspace_system_packages.txt`) are part of what gets snapshotted when a new bundle revision is published. As the developer/publisher, your edits here become the next shipped revision.
- **`app-data/`** is the per-user persistent runtime volume. On the platform it lives in an `AppDataVolume` keyed by `(user_id, bundle_id)` — one volume per user per bundle, bind-mounted into the agent container at `/app/workspace/app-data`. It is **not** part of bundle revisions: when you publish, only the bundle-owned folders are snapshotted, and every user who installs your bundle gets their own fresh app-data volume. On the platform side the volume survives `apply-update` (bundle folders are overwritten, app-data is never touched) and uninstall/reinstall (orphaned, not deleted; reattaches by `bundle_id`). What you see synced to your local `workspace/app-data/` is your *own* developer install's app-data — useful for inspecting runtime output your scripts produce. It's gitignored by default since it's per-user runtime state, not bundle content.

  Where scripts should put what:
  - **`storage/`** — long-lived runtime state (databases, JSON, CSVs, generated reports). Anything the agent must keep across sessions and bundle updates.
  - **`uploads/`** — every user-supplied file at runtime lands here automatically: chat attachments, task attachments, MCP `get_file_upload_url` uploads. Read from this folder, don't write to it from scripts.
  - **`cache/`** — disposable caches the scripts may rebuild on demand.
- **`credentials/`** is managed by the backend and only readable on your side.

## Working with AI Coding Tools

Setup generates MCP server configs for **Claude Code** (`.mcp.json`) and **opencode** (`opencode.json`), giving your AI tool a `knowledge_query` tool that searches the agent's knowledge base.

```bash
cd my-agent/
claude        # or: opencode
```

## Sync & Conflict Resolution

`cinna sync` drives Mutagen in `two-way-safe` mode with VCS-aware ignores (including the backend-managed `credentials/` directory, so it never conflicts on files you're told not to edit). When the same file changes on both sides, Mutagen parks a conflict (it does **not** pick a winner, and does not write `.conflict.*` files in this mode) — list them with `cinna sync conflicts`, then clear them with `cinna sync resolve --prefer local` (your edits win) or `--prefer remote` (the container's version wins). For a non-interactive flush, `cinna sync push` / `cinna sync pull` settle the session and exit.

Large binary files and build artifacts are ignored by default (see `mutagen.yml`). Add your own ignores there if needed.

## Development

```bash
git clone https://github.com/opencinna/cinna-cli.git
cd cinna-cli
uv venv && uv pip install -e ".[dev]"
uv run pytest -v
uv run ruff check src/
```

## License

MIT
