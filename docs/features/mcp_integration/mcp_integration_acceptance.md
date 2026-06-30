# MCP Integration — Acceptance Scenarios (live e2e)

The catalog of **real-usage scenarios** for an agent doing *integration* testing
of cinna-cli's MCP surfaces against a **live** environment — a real platform
backend with a knowledge base, real agent containers, and real a2a MCP connectors.
These are not unit tests; they exercise the stdio proxy under an actual MCP client
and the account-level wiring against the real connect endpoints.

How to use: pick scenarios relevant to the change, run the **Steps** verbatim,
assert the **Expected**, and watch for the **Watch for** failure modes. The proxy
move-tolerance scenarios (4–5) and the `connect mcp` ambiguity/mode scenarios
(8–10) are where the subtle bugs live.

## Preconditions

- A reachable platform (e.g. `http://localhost:8000` backend) with a knowledge
  base populated for at least one agent.
- **Editable install** of the CLI under test:
  `python3 -c "import cinna,os;print(os.path.dirname(cinna.__file__))"` must point
  at this repo's `src/cinna`. Confirm `which cinna` resolves and
  `cinna mcp-proxy --help` is **not** listed by `cinna --help` (it is hidden).
- A per-agent workspace from `cinna setup <token>` (gives `.cinna/config.json`,
  `.mcp.json`, `opencode.json`).
- An account workspace from `cinna account setup …` (gives `.cinna/account.json`
  and the account-mode `.mcp.json`).
- For `connect mcp`: **two** account agents — a **producer** that exposes a
  discoverable agent2agent MCP connector, and a **consumer** your account may wire
  it into.
- `python3` with the `mcp` SDK installed (it ships with cinna-cli).

> Run `connect` commands from inside the account workspace. The proxy is launched
> by your MCP client, but you can drive it directly for testing as shown below.

## Scenario catalog

### 1. Setup wires the per-agent knowledge proxy

- **Goal:** a fresh agent checkout gives the local coding agent a `knowledge_query`
  tool.
- **Steps:**
  ```
  cinna setup <token>
  cat .mcp.json
  cat opencode.json
  ```
- **Expected:** `.mcp.json` declares server `agent-knowledge` with
  `command: "cinna"`, `args: ["mcp-proxy"]`, and `env.CINNA_CONFIG ==
  ".cinna/config.json"` (relative). `opencode.json` mirrors it under
  `mcp.agent-knowledge` with `CINNA_CONFIG` in `environment`.
- **Watch for:** an absolute `CINNA_CONFIG` path (breaks on folder move); the
  server missing or named differently than `agent-knowledge`.

### 2. Per-agent proxy answers a knowledge query

- **Goal:** the stdio proxy actually returns knowledge snippets.
- **Steps:** from the agent workspace, drive the proxy over stdio with a minimal
  MCP client (or your editor's MCP tool). A quick smoke check:
  ```
  CINNA_CONFIG=.cinna/config.json cinna mcp-proxy <<'EOF'
  {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}
  {"jsonrpc":"2.0","method":"notifications/initialized"}
  {"jsonrpc":"2.0","id":2,"method":"tools/list"}
  EOF
  ```
- **Expected:** the `tools/list` response lists exactly one tool, `knowledge_query`,
  with `query` required and an optional `topic` whose description enumerates the
  agent's topics. A `tools/call` of `knowledge_query` against a known query returns
  ranked `## [source] (relevance: NN%)` text blocks (or `No results found.`).
- **Watch for:** the proxy crashing instead of returning an error for an unknown
  tool name; results not formatted with source + relevance.

### 3. Account-mode proxy searches platform knowledge

- **Goal:** the account workspace's proxy searches the account user's knowledge
  sources, not a single agent's.
- **Steps:**
  ```
  cat .mcp.json     # in the account workspace
  CINNA_ACCOUNT_CONFIG=.cinna/account.json cinna mcp-proxy <<'EOF'
  {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}
  {"jsonrpc":"2.0","method":"notifications/initialized"}
  {"jsonrpc":"2.0","id":2,"method":"tools/list"}
  EOF
  ```
- **Expected:** the account `.mcp.json` server is named `platform-knowledge` with
  `env.CINNA_ACCOUNT_CONFIG == ".cinna/account.json"`. The proxy serves
  `knowledge_query` and a `tools/call` hits `POST /account/knowledge/search`.
- **Watch for:** the account workspace accidentally wiring `agent-knowledge` /
  `CINNA_CONFIG`; the server falling back to per-agent mode.

### 4. Proxy survives a moved folder (relative path heals)

- **Goal:** moving the workspace doesn't break the knowledge tool.
- **Steps:** move the agent workspace to a new path, then launch the proxy from the
  new location **without** regenerating configs:
  ```
  mv <ws> <ws>-moved
  cd <ws>-moved && cinna mcp-proxy <<'EOF'
  {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}
  EOF
  ```
- **Expected:** the proxy initializes successfully — it resolves
  `.cinna/config.json` relative to cwd (or heals a stale absolute path by walking
  up). No regeneration needed.
- **Watch for:** a "Could not locate a cinna workspace" exit after a move; reliance
  on a baked-in absolute path.

### 5. Bare proxy with no workspace fails loud

- **Goal:** an unconfigured launch gives clear guidance, not a protocol crash.
- **Steps:** `cd /tmp && unset CINNA_CONFIG CINNA_ACCOUNT_CONFIG && cinna mcp-proxy </dev/null`
- **Expected:** non-zero exit with a message telling the user to set `CINNA_CONFIG`
  / `CINNA_ACCOUNT_CONFIG` or run from a folder containing `.cinna/`.
- **Watch for:** a stack trace mid-MCP-handshake; a silent hang.

### 6. Mode precedence — nested agent under account

- **Goal:** with no env vars, an agent config nested under an account wins.
- **Steps:** from a synced agent dir that lives **under** an account workspace,
  `unset CINNA_CONFIG CINNA_ACCOUNT_CONFIG && cinna mcp-proxy </dev/null` (expect it
  to load the agent config); then from the account root expect account mode.
- **Expected:** nested agent dir → per-agent (`agent-knowledge`) mode; account root
  → account (`platform-knowledge`) mode.
- **Watch for:** the account config shadowing a nested agent's config when run from
  inside the agent dir.

### 7. `connect mcp` wires a producer connector into a consumer

- **Goal:** one agent gains runtime MCP access to another.
- **Steps:** from the account workspace,
  ```
  cinna account agents
  cinna connect mcp --producer "<Producer>" --consumer "<Consumer>" --label test-mcp
  ```
- **Expected:** output `Connected: <Consumer> → <Producer> (MCP)` plus a credential
  id, endpoint URL, transport, auth mode, and status. The credential appears on the
  consumer (and, once synced, read-only under the consumer's
  `workspace/credentials/`). If OAuth is required, an authorize URL is printed.
- **Watch for:** the credential not landing on the consumer; a printed status that
  claims success while an unopened authorize URL means it's incomplete.

### 8. `connect mcp` is fail-loud on no match

- **Goal:** a bad producer ref doesn't silently no-op.
- **Steps:** `cinna connect mcp --producer "no-such-agent" --consumer "<Consumer>"`
- **Expected:** a clear error: "No discoverable agent2agent MCP connector matches
  …" followed by the list of discoverable producers. Non-zero exit.
- **Watch for:** a wire created against the wrong producer; a generic traceback.

### 9. `connect mcp` is fail-loud on ambiguity

- **Goal:** a producer exposing several connectors isn't resolved arbitrarily.
- **Setup:** a producer with two+ discoverable connectors for this consumer.
- **Steps:** `cinna connect mcp --producer "<Producer>" --consumer "<Consumer>"`
- **Expected:** an error listing the matching connectors (name + id) and advising
  the UI / `cinna api`. Nothing is wired.
- **Watch for:** the CLI silently picking the first connector.

### 10. Mode flags

- **Goal:** the conversation/building scoping behaves.
- **Steps / Expected:**
  - `--conversation-only` → connection enabled in conversation mode only.
  - `--building-only` → building mode only.
  - both together → rejected with "mutually exclusive", non-zero exit, no wire.
  - neither → both modes (the wire request omits the mode keys).

## Cross-cutting invariants (must hold across all scenarios)

- **The proxy never crashes the MCP session.** Unknown tools, empty results, and
  missing workspaces produce text errors or a clean exit, not a mid-protocol
  traceback.
- **Generated configs use relative paths.** `.mcp.json` / `opencode.json` reference
  `.cinna/config.json` / `.cinna/account.json` relative to the workspace, so a move
  never breaks the tool.
- **Mode is unambiguous.** Account env beats agent env; without env, the nearest
  `.cinna/` (agent-before-account) decides — never both.
- **`connect mcp` never guesses or silently clobbers.** No-match and ambiguous
  cases are fail-loud; a default wire enables both modes; OAuth is surfaced.
- **No secret printed.** `connect mcp` reports a credential **id** and metadata,
  never token material.

## Cleanup

- Remove the test wire from the consumer (delete the credential created by
  `connect mcp`, e.g. via the platform UI or the account credentials surface).
- Move any renamed workspace back (`mv <ws>-moved <ws>`) and re-confirm the proxy
  still resolves.
- `cinna disconnect` (in the agent dir) / `cinna account` teardown if the test
  workspaces were throwaway.
- Verify no leftover MCP processes: the stdio proxy is owned by the MCP client, so
  closing the editor / the test harness ends it.
