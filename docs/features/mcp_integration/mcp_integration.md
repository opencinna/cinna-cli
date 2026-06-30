# MCP Integration (`cinna mcp-proxy`, `cinna connect mcp`)

## Purpose

Bridge agents and the Model Context Protocol (MCP) in two complementary
directions:

- **Inbound to your local coding agent** — a local stdio MCP server
  (`cinna mcp-proxy`) gives whatever MCP-capable tool you build with (Claude
  Code, opencode, Cursor) a live `knowledge_query` tool backed by the platform's
  knowledge base. It is auto-wired into the workspace at setup time; you never
  run it by hand.
- **Outbound between platform agents** — `cinna connect mcp` wires one agent's
  **agent2agent (a2a) MCP connector** into another agent as a credential, so the
  consumer agent can call the producer over MCP at runtime in the cloud.

Both surfaces are about MCP, but they operate at different layers: one feeds your
*local* dev agent; the other connects two *platform* agents to each other.

## Mental model — two MCP surfaces, do not conflate

### Surface 1 — local knowledge proxy (`cinna mcp-proxy`)

- A short-lived subprocess your MCP client launches over **stdio** (stdin/stdout).
  It is a thin bridge: an MCP `knowledge_query` tool call becomes a platform
  knowledge-search HTTP request, and the results come back as MCP text content.
- It is **hidden** (`cinna mcp-proxy` is not in `--help`) because it is launched
  by the MCP client via a generated config file, not typed by a human.
- It runs in one of **two modes**, selected by an environment variable the
  generated config sets:
  - **Per-agent mode** (`CINNA_CONFIG` → `.cinna/config.json`): searches a single
    agent's knowledge base. MCP server name `agent-knowledge`.
  - **Account mode** (`CINNA_ACCOUNT_CONFIG` → `.cinna/account.json`): searches
    the account user's accessible platform knowledge sources (public + own
    private), with no agent scope. MCP server name `platform-knowledge`.
- Wiring is generated, not manual: `.mcp.json` (Claude Code) and `opencode.json`
  (opencode) are written during `cinna setup` (per-agent) and
  `cinna account setup` / `cinna account refresh-context` (account). They simply
  declare a server that runs `cinna mcp-proxy` with the right env var.

### Surface 2 — agent-to-agent wiring (`cinna connect mcp`)

- An **account-level** command (run from an account workspace) that connects a
  **consumer** agent to a **producer** agent's discoverable a2a MCP connector.
- The wire is a **credential** attached to the consumer; it rides the consumer's
  normal credential sync into its remote env, so the consumer can call the
  producer's MCP connector during conversation and/or building mode — no manual
  key handling.
- It is the MCP sibling of `cinna connect agent-api` (which wires a producer's
  REST API instead). Both live in the `cinna connect` group.

## Core concepts

- **MCP** — Model Context Protocol, an open protocol for connecting AI tools to
  external data/capabilities. See the glossary entry in `docs/README.md`.
- **`knowledge_query`** — the one tool the proxy exposes. Input: a natural-language
  `query` plus an optional `topic`. Output: ranked knowledge snippets formatted as
  MCP text content.
- **stdio transport** — the proxy speaks MCP over stdin/stdout as a subprocess of
  the MCP client. There is no network port; the client owns the process lifecycle.
- **a2a MCP connector** — a producer agent's runtime MCP endpoint that other
  agents may consume. The platform decides which connectors are *discoverable* to
  a given consumer account.
- **Connector vs. proxy** — the local proxy is a dev-time bridge to *knowledge*;
  the a2a connector is a runtime channel between two *agents*. `cinna connect mcp`
  never touches `.mcp.json` or `cinna mcp-proxy`.

## User flows

### Getting the local knowledge tool (no command to run)
1. `cinna setup` writes `.mcp.json` + `opencode.json` pointing at
   `cinna mcp-proxy` in per-agent mode (`CINNA_CONFIG=.cinna/config.json`).
   `cinna account setup` does the same in account mode
   (`CINNA_ACCOUNT_CONFIG=.cinna/account.json`).
2. Open the workspace in Claude Code / opencode. The client discovers the
   `agent-knowledge` (or `platform-knowledge`) server and launches
   `cinna mcp-proxy` as a subprocess with cwd set to the workspace folder.
3. Your local coding agent now has a `knowledge_query` tool; calling it searches
   the agent's (or account's) knowledge base live and returns ranked snippets.

### Wiring one agent into another over MCP
1. From the account workspace, run
   `cinna connect mcp --producer <ref> --consumer <ref>`. Agents are referenced by
   display name, slug, or id (see `cinna account agents`).
2. The CLI lists the connectors discoverable to the consumer, resolves the
   producer to exactly one connector, and creates the connection.
3. By default the connection is enabled in **both** conversation and building
   modes; pass `--conversation-only` or `--building-only` (mutually exclusive) to
   restrict it. `--label` names the resulting credential.
4. The CLI prints the credential id, endpoint, transport, auth mode, and status.
   If the connector requires OAuth, it prints an **authorize URL** that must be
   opened in a browser to finish the connection.

## Business rules / guardrails

- **The proxy is auto-wired, never hand-run.** `cinna mcp-proxy` is hidden and
  expects to be launched by an MCP client with the right mode env var. Run
  directly outside a workspace it fails loud, telling you to set `CINNA_CONFIG` /
  `CINNA_ACCOUNT_CONFIG` or run from a folder containing `.cinna/`.
- **Mode is chosen by env var, not by guesswork.** `CINNA_ACCOUNT_CONFIG` selects
  account mode; `CINNA_CONFIG` selects per-agent mode. With neither set, the proxy
  auto-detects the nearest `.cinna/` walking up from cwd (an agent config nested
  under an account wins because it is found first).
- **Move-tolerant.** The generated configs store the config path **relative** to
  the workspace root, and the proxy heals a stale absolute path by walking up from
  cwd — so moving the folder doesn't break the tool and the config needs no
  regeneration.
- **Generated configs are managed infra.** `.mcp.json` / `opencode.json` are
  regenerated by setup / refresh-context and are gitignored; they are not user
  source. The account workspace also pre-approves the server in
  `.claude/settings.json` so Claude Code doesn't prompt on every tool call.
- **`connect mcp` is account-scoped and fail-loud on ambiguity.** If the producer
  exposes more than one discoverable connector, the CLI refuses to guess and lists
  the matches; if nothing matches, it lists the discoverable producers. It never
  picks an arbitrary connector.
- **Mode flags are exclusive.** `--conversation-only` and `--building-only` cannot
  be combined; default is both modes on.
- **OAuth is surfaced, not silently skipped.** When the connector needs
  authorization, the returned authorize URL is printed and the connection is not
  complete until the user opens it.

## Architecture overview

```
Surface 1 — local knowledge proxy (stdio)

Claude Code / opencode / Cursor
      │ reads .mcp.json / opencode.json  (server runs: cinna mcp-proxy)
      │ launches subprocess, cwd = workspace root
      ▼
cinna mcp-proxy  (MCP stdio server: tool knowledge_query)
      │  mode from env: CINNA_CONFIG → agent | CINNA_ACCOUNT_CONFIG → account
      ▼
PlatformClient / AccountClient
      │  POST /agents/{id}/knowledge/search   (per-agent)
      │  POST /account/knowledge/search       (account)
      ▼
Cinna Core platform knowledge base (vector search) → ranked snippets

Surface 2 — agent-to-agent MCP wiring (account level)

cinna connect mcp --producer P --consumer C
      │  GET  /account/connect/mcp/discoverable   (pick P's connector)
      │  POST /account/connect/mcp                (wire it to C)
      ▼
credential attached to consumer C ──► rides C's credential sync ──►
C's remote env can call P's a2a MCP connector at runtime
```

## Integration points

- **Agent API** (`../agent_api/agent_api.md`) —
  `cinna connect agent-api` is the REST sibling of `cinna connect mcp`; both live
  in the `cinna connect` group and wire a producer into a consumer as a synced
  credential.
- **Account workspace** — `connect mcp` and the account-mode proxy both require an
  account workspace (`.cinna/account.json`) and the `AccountClient`; the proxy's
  account mode searches the account user's knowledge sources.
- **Bootstrap / setup** — the per-agent `.mcp.json` / `opencode.json` are written
  by `cinna setup`; the account versions by `cinna account setup` /
  `cinna account refresh-context`.
- **Knowledge base** — the proxy is a thin front for the platform's knowledge
  search; see the MCP and Knowledge Source glossary entries in `docs/README.md`.

Implementation: see [mcp_integration_tech.md](mcp_integration_tech.md). Real-usage
e2e test scenarios: see [mcp_integration_acceptance.md](mcp_integration_acceptance.md).
