# MCP Integration — Technical Reference

Implementation of [mcp_integration.md](mcp_integration.md). cinna-cli is a Python
CLI; all logic lives in `src/cinna/`, tests in `tests/`. Two surfaces: the local
stdio knowledge proxy (`cinna mcp-proxy`) and the account-level agent-to-agent
wire (`cinna connect mcp`).

## File locations

- `src/cinna/mcp_proxy.py` — the stdio MCP server: both server builders, the mode
  resolver, logging setup, and the `cinna mcp-proxy` entry point.
- `src/cinna/context.py` — generates the per-agent `.mcp.json` / `opencode.json`
  and the CLAUDE.md MCP-tools section.
- `src/cinna/account.py` — `run_connect_mcp()` (the `cinna connect mcp` flow),
  `_resolve_discoverable_connector()`, and the account-mode `.mcp.json` /
  `opencode.json` + `.claude/settings.json` writers.
- `src/cinna/client.py` — `PlatformClient.search_knowledge()`,
  `AccountClient.search_knowledge()`, `AccountClient.list_discoverable_mcp()`,
  `AccountClient.connect_mcp()`.
- `src/cinna/main.py` — the `cinna connect` group, `cinna connect mcp`, and the
  hidden `cinna mcp-proxy` command.
- Tests: `tests/test_context.py` (per-agent `.mcp.json` generation, MCP-tools
  formatting), `tests/test_account.py` (account-mode config wiring, proxy mode
  resolution/healing, `connect mcp` CLI + client), `tests/test_client.py`
  (per-agent and account knowledge search).

## Command surface

- `cinna connect mcp` → `src/cinna/main.py:connect_mcp()` → `src/cinna/account.py:run_connect_mcp()`
- `cinna connect agent-api` → `src/cinna/main.py:connect_agent_api()` (REST sibling; see the `agent_api` feature)
- `cinna mcp-proxy` (hidden) → `src/cinna/main.py:mcp_proxy()` → `src/cinna/mcp_proxy.py:run_mcp_proxy()`

## Key functions & flow

### Local proxy (`src/cinna/mcp_proxy.py`)

- `run_mcp_proxy()` — entry point. Calls `_resolve_proxy_context()`, sets up file
  logging, loads the right config, builds the matching server, then serves over
  stdio via `mcp.server.stdio.stdio_server()` inside `asyncio.run()`.
- `_resolve_proxy_context()` → `(mode, workspace_root)` — selects mode and locates
  the workspace tolerant of a moved folder: (1) `CINNA_ACCOUNT_CONFIG` set ⇒
  account mode; (2) else `CINNA_CONFIG` set ⇒ agent mode; (3) else auto-detect the
  nearest `.cinna/` walking up from cwd. For each env var the value is tried
  literally (absolute or cwd-relative) and then healed by walking up from cwd
  (`find_account_root` / `find_workspace_root`). Returns `(None, None)` when
  nothing is found, which makes `run_mcp_proxy` exit with a clear message.
- `create_mcp_server(config)` — per-agent server named `agent-knowledge`; the
  `knowledge_query` tool's `topic` description lists the agent's topics via
  `_topic_list()`; tool calls go to `PlatformClient.search_knowledge(agent_id, …)`.
- `create_account_mcp_server(account_config)` — account server named
  `platform-knowledge`; tool calls go to `AccountClient.search_knowledge()` inside
  a context-managed client.
- `_format_results(results)` — renders each hit as a markdown block with source +
  relevance %; empty results return `"No results found."`.
- `_setup_mcp_logging(workspace_root)` — the proxy is launched directly by the MCP
  client (not the Click group), so it wires its own rotating handler into the
  workspace's `cinna.log`.

### Config generation

- `src/cinna/context.py:generate_mcp_json()` — writes `.mcp.json` with server
  `agent-knowledge`, command `cinna mcp-proxy`, env `CINNA_CONFIG` = the relative
  `_REL_CONFIG_PATH` (`.cinna/config.json`).
- `src/cinna/context.py:generate_opencode_json()` — the opencode analogue
  (`type: local`, `command: ["cinna", "mcp-proxy"]`, `environment` with
  `CINNA_CONFIG`).
- `src/cinna/account.py:_write_account_mcp_config()` — account-mode `.mcp.json` /
  `opencode.json` with server `platform-knowledge` and env `CINNA_ACCOUNT_CONFIG`
  = `.cinna/account.json` (relative to the account root).
- `src/cinna/account.py:_write_account_claude_settings()` — `.claude/settings.json`
  with `enableAllProjectMcpServers: true` and an `mcp__platform-knowledge` allow
  rule so Claude Code doesn't prompt on first launch / each tool call.

### Agent-to-agent wire (`src/cinna/account.py:run_connect_mcp()`)

1. `find_account_root()` + `load_account_config()` — requires an account workspace.
2. `AccountClient.list_account_agents()` → `_resolve_account_agent()` resolves the
   **consumer** by name/slug/id.
3. `AccountClient.list_discoverable_mcp(consumer_id)` lists connectors the consumer
   may consume; `_resolve_discoverable_connector(items, producer_ref)` matches the
   **producer** by agent id, exact name, or slugified name — raising a fail-loud
   `ClickException` on no match (lists discoverable producers) or ambiguity (lists
   the matching connectors).
4. `AccountClient.connect_mcp(connector_id, consumer_id, mcp_mode_conversation,
   mcp_mode_building, label)` creates the wire. Mode booleans come from the CLI:
   `mcp_mode_conversation = not building_only`, `mcp_mode_building = not
   conversation_only` (the command rejects setting both `--*-only` flags).
5. Prints credential id, endpoint, transport, auth mode, status; if the result
   carries `authorize_url`, prints the OAuth authorize prompt.

## Config & registry

- **Generated MCP configs** (gitignored, regenerated by setup/refresh-context):
  - `.mcp.json` / `opencode.json` — per-agent: server `agent-knowledge`, env
    `CINNA_CONFIG=.cinna/config.json`. Account: server `platform-knowledge`, env
    `CINNA_ACCOUNT_CONFIG=.cinna/account.json`.
  - `.claude/settings.json` (account) — pre-approves the MCP server + `Bash(cinna:*)`.
- **Mode-selecting env vars** read by the proxy: `CINNA_ACCOUNT_CONFIG`,
  `CINNA_CONFIG` (value is a path *hint*; the proxy heals it against cwd).
- The proxy reads `.cinna/config.json` (`agent_id`, `knowledge_sources`) or
  `.cinna/account.json` (platform URL, account token); it writes no registry state.

## External contracts

- **MCP SDK (`mcp`):** `mcp.server.Server`, `mcp.server.stdio.stdio_server`,
  `mcp.types.Tool` / `TextContent`. Transport is **stdio** — the MCP client owns
  the subprocess lifecycle and sets cwd to the workspace folder (relied on by the
  relative-path resolution).
- **Knowledge endpoints:**
  - `POST /api/v1/cli/agents/{id}/knowledge/search` (CLI JWT) — per-agent mode.
  - `POST /api/v1/cli/account/knowledge/search` (account token) — account mode.
- **Connect endpoints:**
  - `GET /api/v1/cli/account/connect/mcp/discoverable` (account token) — connector
    picker (`consumer_agent_id` query param scopes discoverability).
  - `POST /api/v1/cli/account/connect/mcp` (account token) — create the wire; body
    carries `connector_id`, `consumer_agent_id`, optional mode flags (omitted when
    true) and `label`. Response: `credential_id`, `endpoint_url`, `transport`,
    `auth_mode`, `status`, optional `authorize_url`.

## Edge cases & guardrails (preserve these)

- **Hidden, self-locating proxy.** `cinna mcp-proxy` is `hidden=True`; run with no
  resolvable workspace it raises `SystemExit` with guidance, never a stack-trace
  crash mid-protocol. (`src/cinna/mcp_proxy.py:run_mcp_proxy()`)
- **Move tolerance.** Configs store the config path **relative**; the proxy heals a
  stale absolute path by walking up from cwd, so a moved folder still works without
  regeneration. (`tests/test_account.py`:
  `test_proxy_context_relative_path_resolved_from_cwd`,
  `test_proxy_context_stale_absolute_path_heals_via_cwd`)
- **Mode precedence.** Account env beats agent env; with neither, an agent config
  nested under an account is found first and wins. (`tests/test_account.py`:
  `test_proxy_context_autodetects_nearest_cinna_without_env`)
- **Unknown tool / empty results don't crash.** A non-`knowledge_query` tool name
  returns a text error; empty results return `"No results found."` rather than an
  exception. (`src/cinna/mcp_proxy.py`)
- **Own log handler.** The proxy isn't started via the Click group, so it must wire
  `cinna.log` itself; otherwise its logs vanish. (`_setup_mcp_logging`)
- **`connect mcp` never guesses.** Ambiguous or absent producer connectors are
  fail-loud `ClickException`s; `--conversation-only` + `--building-only` together
  is rejected. (`tests/test_account.py`: `test_connect_mcp_ambiguous_producer`,
  `test_connect_mcp_no_discoverable_match`,
  `test_connect_mcp_mode_flags_mutually_exclusive`)
- **Default-mode flags omitted on the wire.** `connect_mcp` only sends mode keys
  when restricting (so a default both-modes wire sends neither). (`tests/test_account.py`:
  `test_account_client_connect_mcp_default_modes_omitted`)
