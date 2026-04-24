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

### `cinna dev`

Start a foreground dev session — creates / resumes the Mutagen sync session for this workspace and attaches the terminal to a two-tab TUI (status + raw Mutagen details). Ctrl-C terminates the session; sync does not outlive the TUI. To observe sync from another terminal without affecting it, use `cinna sync status`.

### `cinna sync status | conflicts`

Read-only views onto the live sync session (started by `cinna dev`).

- `status` — state, pending changes, conflict count.
- `conflicts` — list any conflict copies Mutagen has written. Resolve by picking a winner in your editor and deleting the `.conflict.*` copy.

### `cinna exec <command…>`

Stream a command through the platform to the remote agent environment. Output streams back live; Ctrl+C aborts. Exit code matches the remote process.

```bash
cinna exec python scripts/main.py
cinna exec pip install pandas
cinna exec 'bash -c "ls -la"'
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
    scripts/
    docs/
    webapp/
    files/
    credentials/          # Backend-managed; visible read-only on your side
    workspace_requirements.txt
    workspace_system_packages.txt
  mutagen.yml             # Sync rules (customizable)
  CLAUDE.md               # Local dev instructions for AI tools
  BUILDING_AGENT.md       # Building mode prompt pulled from the platform
  .mcp.json               # MCP config for Claude Code
  opencode.json           # MCP config for opencode
  .gitignore
```

## Working with AI Coding Tools

Setup generates MCP server configs for **Claude Code** (`.mcp.json`) and **opencode** (`opencode.json`), giving your AI tool a `knowledge_query` tool that searches the agent's knowledge base.

```bash
cd my-agent/
claude        # or: opencode
```

## Sync & Conflict Resolution

`cinna sync` drives Mutagen in `two-way-safe` mode with VCS-aware ignores. When the same file changes on both sides, Mutagen writes `<file>.conflict.<side>.<timestamp>` instead of picking a winner — inspect them with `cinna sync conflicts`, resolve, and delete the conflict copy.

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
