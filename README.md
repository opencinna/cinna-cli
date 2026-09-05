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
- **[Mutagen](https://mutagen.io)** (version pinned by the platform — `cinna setup` checks and prompts to install; set `CINNA_MUTAGEN_BIN=/abs/path/mutagen` to use a specific binary instead of the one on PATH)

### Cinna Desktop

[Cinna Desktop](https://github.com/opencinna/cinna-desktop) installs its **own pinned copy** of cinna-cli (with `uv` and Mutagen alongside, inside its data folder) and drives it as a child process to create and keep alive an account workspace under `<AgentsHome>/Cloud/<host>/` right after you sign in — no `cinna login`, no token to paste. That private copy is not on your `PATH` unless you opt in from the desktop's settings; installing cinna-cli yourself (`uv tool install cinna-cli`) for terminal use is fine and both operate on the same workspace. The version the platform pins is reported by `cinna account status` / `cinna doctor`.

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

### `cinna login [domain]`

Sign in to an account workspace in the browser — **no setup token to paste**. One command serves two cases:

- **Resume** — run it from inside an existing account workspace and it refreshes the stored account CLI token **in place** (reusing the platform URL + machine name; other settings like the active user workspace are preserved).
- **Connect new** — run it anywhere else and it bootstraps a fresh account workspace. It asks for the platform **domain** (or pass it as an argument — protocol optional, `localhost` recognized), then creates the workspace **in the current folder if it's empty**, or **in a subfolder you name** if it isn't. So you can `mkdir my-cinna && cd my-cinna && cinna login`, or just `cinna login app.example.com` straight into the current directory.

Either way it opens a browser authorization URL (OAuth 2.0 device flow); once you click **Authorize** (already signed in to the platform) the CLI receives a fresh token and writes `.cinna/account.json`.

```bash
# Resume the account workspace you're in:
cd my-cinna/ && cinna login

# Connect a new account from a fresh folder:
mkdir my-cinna && cd my-cinna && cinna login          # prompts for the domain
cinna login app.example.com                            # or pass it directly
cinna login app.example.com --dir my-cinna             # always into a named subfolder
#   Your verification code: WX7K-9Q2P
#   Open this URL and click Authorize:
#     https://app.example.com/device?code=WX7K-9Q2P
#   ✓ Account workspace ready.
```

Use it when `cinna account status` or `cinna doctor` reports the account token has expired. Because the per-agent tokens minted from an account (`cinna agent sync`) can only be re-minted while the account token is valid, the flow is: `cinna login` (refresh the account), then `cinna doctor` (re-mint the dependent sub-agent tokens). If the platform doesn't expose the device-login endpoints yet, `cinna login` says so and points you at the `cinna account set-token` paste fallback.

### `cinna account setup <token_or_url>`

Initialize an **account workspace** — a multi-agent root from which you can discover your agents and attach per-agent workspaces without going back to the UI. Setup is initiated from **Settings → Channels → Local Development** on the platform, which emits a `curl | python3` one-liner (same pattern as the per-agent flow):

```bash
curl -sL https://your-platform.com/api/cli-setup/account/TOKEN | python3 -
```

Accepts the same input forms as `cinna setup` (full curl command, URL, or bare token — bare tokens need `CINNA_PLATFORM_URL`). Creates a folder named after the platform domain (override with `--dir`: an absolute path is used as is with parents created, a relative one lands under the current directory; an existing account workspace there is refused before the token is spent) containing:

```
my-cinna/
  .cinna/account.json    # account CLI token + platform/frontend URLs (0600, do not commit)
  CLAUDE.md              # orchestrator guide for AI tools
  context/               # platform docs, API reference, example scripts, worked playbooks
  agents/                # one standard per-agent workspace per `cinna agent sync`
```

Setup also downloads the **context package** into `context/` — curated platform docs (feature map at `context/platform/README.md`), a generated per-domain REST API reference (`context/api_reference/`), sample platform-API scripts (`context/examples/`), and worked end-to-end playbooks (`context/guides/`, e.g. `build-an-agentic-network.md`). The download is best-effort: if it fails, setup still succeeds with a warning and `cinna account refresh-context` fetches it later.

The account token is only used for the account-level endpoints (listing agents, minting per-agent tokens, the context package). Per-agent work always runs on each child workspace's own token. Revoking the account session in Settings disconnects every agent synced from it.

`account setup`, `account set-token` and `account status` take `--no-input` (never prompt: defaults, or fail with code `needs_input`; also `CINNA_NO_INPUT=1`, accepted before or after the subcommand) and `--json` (one JSON object per line on stdout — progress `{"step","total","status","message"}` lines, then a final `{"result":"ok"|"error",…}`; implies `--no-input`). Exit codes are stable for every command: `0` ok, `10` setup token invalid/expired/used, `11` token for another account, `12` platform unreachable or 5xx, `1` anything else (the JSON `code` says what), `2` usage.

### `cinna account set-token <token_or_url>`

Refresh the **account** token in place from a fresh account setup token — the account counterpart of `cinna set-token`, and the paste alternative to `cinna login`. Run inside the account workspace; it re-exchanges under the stored machine name and rewrites only `account_token` (plus a refreshed platform/frontend URL) in `.cinna/account.json`. The active user workspace, machine name, `context/` and every synced child under `agents/` are untouched; run `cinna doctor` afterwards to re-mint expired child tokens. A token for a different account is refused (exit `11`) and nothing is written. Bare tokens reuse the stored platform URL.

```bash
cd my-cinna/
cinna account set-token 'curl -sL https://your-platform.com/api/cli-setup/account/TOKEN | python3 -'
```

### `cinna account agents`

List the agents your account can access (run from inside the account workspace). For each agent: display name + ID, building rights (`✓ can build`, `view-only`, or `foreign install` — installed bundles are publisher-managed and can't be synced), whether a remote environment is active, and whether a local workspace already exists under `agents/`.

### `cinna account status`

One-shot summary of the account workspace: platform/frontend URLs, machine name, synced-agent count, and an account-token probe (`valid token` / `expired token` / `no connection`) — the account-level counterpart of `cinna status`.

Also reports the workspace's **context package version** against the platform's current one — the orchestrator guides ship in that tree, so a workspace set up before a guide existed silently lacks it. When it is behind, the command says so and points at `cinna account refresh-context`; `cinna improve list` prints the same nudge, since its playbook lives there. Likewise it compares the installed **cinna-cli version** with the one the platform pins (its `/.well-known/cinna-desktop` discovery document) and suggests `uv tool install cinna-cli==<pin>` when behind; no pin means "unknown", not an error.

With `--json` it prints a single line: `{"result":"ok","workspace",…,"token":"valid|expired|unreachable","synced_agents":N,"agents":[…],"context_package":{"local","remote","state"},"cli":{"installed","required","state"}}`.

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

### `cinna improve list | show | download | status`

Work the **improvement requests** users shared with you about your agents. When someone chatting with an agent hits a bad answer, they can hand the agent's owner a frozen snapshot of that one session — the transcript plus the runtime context that produced it (bundle id + installed version, environment, SDK engine, effective model) — from the session menu or with the `/session-improve` command. These verbs are the receiving half of that loop, and they run from an account workspace so one listing spans every agent you own.

```bash
cinna improve list --status new                    # unhandled first, then newest
cinna improve list --agent crm-agent               # narrow to one agent
cinna improve show 3f2504e0                        # the report + the runtime context block
cinna improve download 3f2504e0                    # → improvements/3f2504e0/
cinna improve status 3f2504e0 in_progress          # claim it
cinna improve status 3f2504e0 completed --note "Fixed in v1.6 — no longer re-asks for the file."
```

`<id>` is the short id printed by `cinna improve list` (or the full UUID). `download` saves and extracts the archive into `improvements/<short-id>/` under the account root (`--out DIR` to choose another target), using the same safe extractor as the workspace clone:

```
improvements/3f2504e0/
├── README.md          # what was reported, by whom, which agent + bundle version, runtime table
├── metadata.json      # the request row
├── context.json       # structured runtime context of the install that misbehaved
├── prompts/           # the install's live prompt docs (WORKFLOW_PROMPT.md, …) + a divergence README
├── memory/            # the agent's captured personal memory, when it had any
└── session/
    ├── messages.md    # human-readable transcript
    └── messages.json  # the same transcript, structured
```

`cinna improve show` renders the prompt block as a per-prompt **in sync / diverged** table against the installed bundle revision — the one thing a publisher cannot see from their own copy, since a consumer who owns their install can edit its prompts. A diverged prompt means the session was produced by text that is not yours; read `prompts/` in the archive before assuming otherwise.

The archive deliberately contains **no** container logs, **no** uploaded file contents (descriptors only), and no credential values (snapshot text is scrubbed server-side before it is stored). Statuses are `new` → `in_progress` → `completed` | `declined`; the `--note` on a closing transition is **shown to the requester**. `--json` on `list` / `show` prints the raw payload for scripting.

The archive is another person's conversation: don't copy it into an agent workspace, don't commit it, and delete the local copy when you're done. The end-to-end playbook a local coding agent should follow — including where a fix has to land for a bundle publisher install versus a standalone agent — ships in the account workspace's context package at `context/guides/handling-improvement-requests.md`.

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

### `cinna agent import <path> [--name TEXT] [--workspace REF] [--update] [--dry-run] [--no-push] [--yes]`

Import an agent that was built **locally** with the [Local Agent Kit](docs/features/local_agent_import/local_agent_import.md) — a folder holding a `cinna-agent.json` manifest, typically `../Local/<slug>` next to this account workspace. Run it from the account workspace root (or any folder inside it).

Nine idempotent steps, each printed as `[n/9]`: read and validate the manifest → create (or resolve) the cloud agent → write its description, router trigger, example prompts and the three document prompts in one bulk write, plus the status refresh command → attach a local workspace (`cinna agent sync`) → copy the tree into `agents/<slug>/workspace/` honouring the contract's exclude list and secret rules, generating `workspace_requirements.txt` from `pyproject.toml` when the agent doesn't ship one → `cinna sync push` → create the credential drafts and attach them → create the schedules → record the publication in `publications.json` and print the summary.

```bash
cinna agent import ../Local/invoice-watcher --dry-run   # see the plan, touch nothing
cinna agent import ../Local/invoice-watcher --yes
cinna chat --agent invoice-watcher "check invoices from last week"

# after more local work:
cinna agent import ../Local/invoice-watcher --update --yes
```

The folder rules come from the kit's versioned contract, `.cinna-kit/layout.json` — the same file Cinna Desktop reads — so a correction published there reaches this command on the next kit refresh rather than waiting for a new cinna-cli. `credentials/` and `app-data/` are **never** copied, not even if the contract drops them, and on top of the exclude list the contract's `secret_files` rules withhold every dotenv shape (`.env`, `.env.prod`, `staging.env`) wherever it sits, while `.env.example` still travels. No secret value is ever read, sent, or printed: credentials are created as empty drafts and the command prints the URLs the user opens to fill them in the browser.

Where the folder has been published is recorded in `publications.json`, a **sibling** of `cinna-agent.json` holding one entry per Cinna instance — it cannot live inside the manifest, because each entry records a hash of the exported tree and the manifest is part of that tree. Every step is idempotent (agent by the entry whose `platform_url` matches the instance you are logged into, credentials by name, schedules by name), so a partial import is resumed with `--update` instead of duplicating anything. The publication is recorded **only after the push settled** — `--no-push`, a failed flush, or remaining conflicts leave it unrecorded, and the next run is a plain `--update`. A legacy `cloud` block is migrated into the ledger on the first write and is still read until then, so an older folder's `--update` never creates a second agent. `--dry-run` makes no platform call and writes nothing.

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

### `cinna chat [--agent <ref>] [--resume <session_id>] [--file PATH ...] [MESSAGE...]`

Talk to an agent through a **real platform session** — the same conversation pipeline production uses (permission checks, agent-env calls, the model/SDK the platform selects), not a local mock. Built for a local coding agent to test the agent it is building: it can prepare a prompt, attach files, and read the reply back as structured data.

Run it from the account workspace (or any synced agent folder under it). The reply is observed by **polling** the backend rather than reading a live stream, so it is robust to streaming/transport quirks.

- `--agent <name|slug|id>` picks the agent; omit it inside a synced agent workspace to infer it. `--resume <session_id>` continues an existing conversation instead of opening a new one (default mode for a new session is `conversation`; `--mode building` opens a building session).
- The message is the positional argument; if omitted it is read from stdin, or you are prompted for it interactively in a TTY.
- `--file PATH` (repeatable) uploads a local file and attaches it to the message.
- Output is **NDJSON** by default — one JSON event per line (`session`, `upload`, `message`, `status`, `done`), trivially parseable by another agent. `--pretty` switches to a human-readable transcript.
- Each `message` carries the agent's reasoning/tool trace under **`events`** — an ordered list of the `thinking` blocks, `tool` calls (with their full `tool_input` payload) and tool results behind the reply, so you see *what the agent did*, not just its final `content`. Pass `--no-events` to drop the trace and keep only the final text.
- Files the agent attaches to its replies are downloaded under `./cinna-chat-files/<session_id>/` (override with `--download-dir`, or skip with `--no-download` to just report the file ids). Downloads are bounded by the api-proxy's 8 MiB response cap.
- `--interval` / `--timeout` tune the poll cadence and the maximum wait for a turn. Ctrl-C interrupts the agent's turn and exits.

```bash
cinna chat --agent crm-agent "Summarize today's leads"
cinna chat --agent crm-agent --file report.csv "Validate this export"
cinna chat --resume 3fa85f64-5717-4562-b3fc-2c963f66afa6 "Now break it down by region"
echo "ping" | cinna chat --agent crm-agent          # message from stdin
cinna chat --agent crm-agent "hi" | jq -c 'select(.event=="message")'
```

The session id is printed in the first `session` event — capture it to drive a multi-turn conversation with `--resume`.

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

### `cinna doctor`

Diagnose and repair stale sync state across the whole machine. Over time the per-user registry (`~/.cinna/agents.json`) and the Mutagen daemon drift out of sync as agents are deleted, environments are spun down, and tokens expire — `cinna doctor` reconciles the two and heals the leftovers in one pass. Run it from anywhere; it is not workspace-scoped.

It detects and fixes:

- **Deleted workspaces** — registry entries whose workspace folder (or its `.cinna/config.json`) is gone. The entry is removed, along with any leftover Mutagen session.
- **Halted sessions** — sessions stopped on `halted-on-root-deletion` (the local `workspace/` root was deleted) while the agent dir is otherwise intact. Terminated; `cinna dev` recreates a clean one.
- **Dead-remote sessions** — sessions stuck retrying a remote env that no longer exists (`connecting-beta` / beta polling error). Mutagen has **no** "give up after N failures" option — a session retries forever until paused or terminated — so `doctor` is the cleanup path for these. Terminated.
- **Orphaned sessions** — `cinna-*` sessions with no registry entry at all. Terminated.
- **Active sessions** — the healthy, still-watching sessions left over from past `cinna dev` runs. They are not broken, but they keep the shared Mutagen daemon busy and are recreated on demand, so doctor offers to clear them as a separate step.
- **Expired tokens** — for **account-managed** workspaces (those under an account root), the CLI token is re-minted automatically through the parent account token, no pasting required. **Standalone** workspaces (set up via `cinna setup`) can only be refreshed with a pasted setup token, so they are reported with a `cinna set-token` hint rather than changed.
- **Expired account token** — a sub-agent token can only be re-minted while the **account** token that mints it is still valid. When the account token has itself expired, doctor probes it once and surfaces a single _"renew the account token — run `cinna login`"_ finding (listing the blocked sub-agents) instead of a pile of re-mints that would all fail with 401. Run `cinna login`, then re-run `cinna doctor` to re-mint the dependents.

```bash
cinna doctor              # diagnose, then walk the repair steps (each defaults to Yes)
cinna doctor --dry-run    # report problems only; change nothing
cinna doctor --yes        # accept every step non-interactively
```

`doctor` first prints the live `cinna-*` session inventory — each session tagged with the **agent** and **folder** it serves — so you can see what's running before deciding anything. Findings it can't fix itself (standalone expired tokens) are listed under **No automatic fix — manual action needed** and never touched.

The repair then runs as three ordered, independently-confirmed steps, each defaulting to **Yes**:

1. **Delete stalled sessions** — deleted-workspace cleanup plus halted / dead / orphaned session termination.
2. **Terminate active sessions** — clear the healthy leftovers from the inventory above (recreated on the next `cinna dev`).
3. **Refresh tokens** — re-mint expired account-managed tokens.

`--yes` accepts all three; `--dry-run` shows the tables and changes nothing.

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
