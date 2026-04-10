# cinna-cli

Local development CLI for [Cinna Core](https://github.com/opencinna/cinna-core) agents.

Work on agent scripts, prompts, and webapps locally with your own editor and AI tools. The CLI handles workspace sync, Docker container management, credential injection, and MCP integration — so the local environment mirrors what the agent sees in production.

## How It Works

Cinna Core agents run in cloud environments managed by the platform. `cinna-cli` creates a local replica of that environment:

1. A **Docker container** replicates the agent's runtime (Python packages, system deps, credentials)
2. A **workspace directory** mirrors the remote agent's files (scripts, prompts, webapp, data)
3. **Push/pull** syncs changes between local and remote with conflict detection
4. **MCP integration** gives Claude Code access to the agent's knowledge base

```
Your Editor / Claude Code
        |
        v
  workspace/           <-- your local files (scripts, prompts, webapp)
        |
  cinna push / pull    <-- sync with remote environment
        |
  Docker container     <-- runs scripts with correct deps & credentials
```

## Prerequisites

- **Python 3.10+**
- **Docker** with Docker Compose

## Getting Started

Setup is initiated from the Cinna Core platform UI. Click **"Local Development"** on your agent's page to get a bootstrap command:

```bash
curl -s https://your-platform.com/cli-setup/TOKEN | python3 -
```

This will:
1. Install `cinna-cli` (via `uv`, `pipx`, or `pip`)
2. Exchange the setup token for CLI credentials
3. Download the agent's Docker build context and build the container
4. Clone the workspace and pull credentials
5. Generate `CLAUDE.md`, `BUILDING_AGENT.md`, and `.mcp.json` for Claude Code

After setup completes, you'll be prompted to start dev mode immediately. Otherwise, `cd` into the agent directory and start working:

```bash
cd hr-manager-agent/
claude                              # open Claude Code (MCP tools auto-configured)
cinna dev                           # start container + live sync with remote
cinna env-up                        # or: start container in background
cinna exec python scripts/main.py   # run a script (needs running container)
```

Enable shell completion for tab-completion of commands:

```bash
cinna completion --install
```

## Commands

### `cinna setup <token_or_url>`

Initialize a local development environment for an agent. Accepts the setup token, the full URL, or the entire curl command copied from the platform UI:

```bash
cinna setup curl -sL http://host/cli-setup/TOKEN | python3 -
cinna setup http://host/cli-setup/TOKEN
cinna setup TOKEN
```

The agent directory name is normalized to lowercase with dashes (e.g., "HR Manager Agent" becomes `hr-manager-agent/`).

### `cinna dev [--interval N]`

Start dev mode: starts the Docker container, runs a live TUI that bidirectionally syncs local and remote, and **removes the container when you press Ctrl+C**. Polls every N seconds (default: 5), pushes local changes and pulls remote changes using 3-way manifest diffing. Credentials are also monitored each cycle and pulled when changed. Conflicts are detected and skipped.

```bash
cinna dev                # start container, watch and sync every 5s
cinna dev --interval 10  # sync every 10s
```

The TUI shows agent info, Docker container details (name, ID, status), sync stats (total pushed/pulled/conflicts), uptime, and a rolling activity log of the last 5 sync events (including credential updates).

Containers are stateless — they hold no data that isn't in `workspace/` — so removing them on exit prevents orphaned background processes.

### `cinna exec <command>`

Run a command inside the agent's Docker container. The container has the correct Python packages, system dependencies, and credentials — always use this instead of running scripts on the host.

**Requires a running container** — start one with `cinna dev` or `cinna env-up` first.

```bash
cinna exec python scripts/main.py
cinna exec pip list
cinna exec bash
```

### `cinna env-up`

Start the agent container in the background. Use this when you need the container running without dev mode (e.g., for repeated `cinna exec` calls).

### `cinna env-down`

Stop and remove the agent container. Containers are stateless — workspace files are mounted, not copied — so removing is always safe.

### `cinna push [--force]`

Push local workspace changes to the remote environment. Detects conflicts when both local and remote have changed the same file since the last sync. Use `--force` to overwrite remote on conflict.

### `cinna pull [--force]`

Pull remote workspace changes to local. Also refreshes credentials and regenerates `BUILDING_AGENT.md` and `CLAUDE.md` from the platform's latest building context. Use `--force` to overwrite local on conflict.

### `cinna status`

Show the current agent info and Docker container status.

### `cinna rebuild [--no-cache]`

Rebuild the Docker image. Run this after modifying `workspace_requirements.txt` or `workspace_system_packages.txt`. Removes any running container first. Does not start a new container — use `cinna dev` or `cinna env-up` after rebuilding.

### `cinna credentials`

Re-pull credentials from the platform without doing a full pull. Useful after updating credentials in the web UI.

### `cinna disconnect [--keep-image]`

Remove the container, Docker image, and `.cinna/` config directory. Workspace files are preserved. Use `--keep-image` to only remove the container but keep the built image.

### `cinna disconnect-all`

Scan the current directory for all agent workspaces, stop their containers, remove Docker images, and delete the directories entirely. Requires confirmation. Useful for cleaning up when done with local development.

Displays a table of discovered workspaces with container status, a progress bar during cleanup, and a results summary showing what succeeded or failed.

### `cinna completion [SHELL] [--install]`

Output or install shell completion for bash, zsh, or fish:

```bash
cinna completion --install        # auto-detect shell and install
cinna completion zsh              # print zsh completion script
eval "$(cinna completion zsh)"    # activate in current session
```

## Workspace Structure

After setup, the agent directory looks like this:

```
my-agent/
  .cinna/                 # CLI config and build context (do not edit)
  workspace/
    scripts/              # Agent Python scripts
    docs/                 # Prompt files (WORKFLOW_PROMPT.md, etc.)
    webapp/               # HTML/CSS/JS dashboard + Python data endpoints
    files/                # Reports, CSVs, data files
    credentials/          # Integration credentials (pulled from platform)
    workspace_requirements.txt      # Python packages (pip install)
    workspace_system_packages.txt   # System packages (apt-get install)
  CLAUDE.md               # Auto-generated context for Claude Code
  BUILDING_AGENT.md       # Building mode system prompt from the platform
  .mcp.json               # MCP server config for Claude Code
  opencode.json           # MCP server config for opencode
  .gitignore              # Excludes generated files and credentials
```

## Working with AI Coding Tools

The setup generates MCP server configs for both **Claude Code** and **opencode**, giving your AI tool access to a `knowledge_query` tool that searches the agent's knowledge base (documentation, articles, and other sources configured in the platform).

### Claude Code

Claude Code automatically picks up the `.mcp.json` config:

```bash
cd my-agent/
claude
```

### opencode

opencode automatically picks up the `opencode.json` config:

```bash
cd my-agent/
opencode
```

### Context Files

The auto-generated `CLAUDE.md` provides the AI tool with context about the local development workflow, available commands, and workspace structure. `BUILDING_AGENT.md` contains the same building mode prompt the cloud agent receives.

## Container Lifecycle

Containers are **stateless** — workspace files are mounted from the host, not copied into the container. This means containers can be created and destroyed freely without data loss.

The CLI never starts containers silently. You control when containers run:

| Command | Creates container | Removes container |
|---------|------------------|-------------------|
| `cinna dev` | On start | On Ctrl+C exit |
| `cinna env-up` | Immediately | — |
| `cinna env-down` | — | Immediately |
| `cinna exec` | Never (requires running) | Never |
| `cinna rebuild` | Never | Before rebuilding image |
| `cinna setup` | Never (builds image only) | — |

This prevents orphaned background containers. If you need a long-running container (e.g., for repeated `cinna exec`), use `cinna env-up` / `cinna env-down` explicitly.

## Sync & Conflict Resolution

`cinna push` and `cinna pull` use manifest-based diffing to track changes:

- A SHA-256 manifest is computed for all workspace files
- Changes are compared against the last-known state from the previous sync
- If the same file changed on both sides, it is flagged as a **conflict**
- Conflicted files are skipped by default — use `--force` to overwrite

The sync excludes `__pycache__`, `.pyc` files, `.DS_Store`, and the `credentials/` directory from file-level diffing. Credentials are managed separately — `cinna pull` and `cinna credentials` fetch them from the platform API, and `cinna dev` monitors them each sync cycle alongside regular files.

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
