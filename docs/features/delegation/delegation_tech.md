# Durable Delegation — Technical Reference

Implementation of [delegation.md](delegation.md). cinna-cli is a Python CLI; all
logic lives in `src/cinna/`, tests in `tests/`. The `cinna delegation` group is a
thin validation + transport layer over cinna-core task routes. It keeps no local
state.

## File locations

- `src/cinna/delegation.py` — the whole feature: the Click group, the four verb
  commands, the owner-side request helper (capability gate + proxy call), the
  executor-side report path, and output rendering.
- `src/cinna/main.py` — attaches `json_option()` to every delegation
  subcommand and registers the group on the root CLI
  (`cli.add_command(delegation)`).
- `src/cinna/account.py` — `find_account_root()` / `load_account_config()`:
  locate the account workspace and its token.
- `src/cinna/client.py` — public wrappers `get_delegation_capabilities()`,
  `create_delegated_task()`, `get_task_detail()`, `put_delegation_result()`,
  `post_delegation_reply()` over `_proxy_json()` / `api_proxy()` (the api-proxy
  transport and error mapping), plus `AccountClient.error_detail()` used by the
  executor path.
- `src/cinna/console.py` — `emit_result()` (the `--json` success line) and
  `json_mode`.
- `src/cinna/errors.py` — `PlatformError`, `CinnaExit` (exit codes and the
  `--json` error line).
- Tests: `tests/test_delegation.py` — retry identity and payload, depth/root
  rules, owner-side routes, capability gate, report validation (blocked
  question, artifacts), executor report (env, session context, headers,
  redirects, non-2xx), and the `--json` envelope.

## Command surface

- `cinna delegation` → `src/cinna/delegation.py:delegation()` (Click group).
- `cinna delegation create` → `src/cinna/delegation.py:create()`.
- `cinna delegation status` → `src/cinna/delegation.py:status()`.
- `cinna delegation report` → `src/cinna/delegation.py:report()`.
- `cinna delegation reply` → `src/cinna/delegation.py:reply()`.

Each verb carries its own `--json` option (the shared per-command
`src/cinna/main.py:json_option()`, applied in `main.py` at registration because
`delegation.py` cannot import `main.py` without a cycle); `--json` is not taken
from the root group.

## Key functions & flow

- **Owner-side request helper** (`src/cinna/delegation.py`) — every owner-side
  call: `find_account_root()` + `load_account_config()`, open an
  `AccountClient`, `get_delegation_capabilities()`. A 404/405 or a body that is
  not a dict with `version` `1` raises "This server does not support durable
  delegations."; other platform errors propagate. Only then is the actual route
  called through the matching public client method.
- `src/cinna/delegation.py:create()` — builds the identity string as the JSON
  array `[target, key]`; `external_ref` is its sha256 hex digest; the delegation
  id is `uuid5(NAMESPACE_URL, identity)`. Validates depth/root before any call.
  POSTs `tasks/` with `title`, `original_message` (= `--brief`),
  `selected_agent_id` (= `--target`), `external_ref`, `delegation_metadata`, and
  `auto_execute` (= `--execute`). Human output: the backend task id and whether
  it executes.
- `src/cinna/delegation.py:status()` — one `GET tasks/{id}/detail`; human output
  shows state, the latest result, and any open question with its result id.
- `src/cinna/delegation.py:report()` — validates (`blocked` ⇒ `--question`;
  each `--artifact` parses to a JSON object with non-empty `kind`, `name` and an
  `http`/`https` `ref`), builds the payload (`status`, `summary`, `question`,
  `audience`, `artifacts`, `body`), then:
  - with `TASK_ID`: owner-side `PUT tasks/{id}/delegation-result`;
  - without: the executor path (below).
- **Executor path** (`report()` without `TASK_ID`) — reads `AGENT_AUTH_TOKEN`,
  `BACKEND_URL`, `ENV_ID` (all required); reads `backend_session_id` from the
  file at `CINNA_SESSION_CONTEXT_PATH` (default `session_context.json` in the
  cwd) and adds it as `source_session_id`; POSTs directly (not via the account
  proxy) with `Authorization: Bearer <token>` and `X-Agent-Env-Id: <ENV_ID>`,
  redirects disabled, 30 s timeout. No capability gate on this path. Any 2xx is
  accepted (an empty or non-JSON success body is treated as `{}` so callers do
  not retry an accepted report); non-2xx raises `PlatformError` with the detail
  from `AccountClient.error_detail()`.
- `src/cinna/delegation.py:reply()` — `POST tasks/{id}/delegation-reply` with
  `result_id` and `message`. Exits 0 but warns when the backend answers
  `delivered: false` (result is not an open question) or `uncertain` (check
  `status` before resending).
- **Output** — in `--json` mode `console.emit_result(delegation=<backend body>)`,
  i.e. one line `{"result": "ok", "delegation": {…}}`; otherwise a short
  human summary.

## Payload shape

`delegation_metadata` on create:

- `id` — `uuid5(NAMESPACE_URL, json [target, key])`, stable per target+key.
- `requester_key` — the `--id` value.
- `origin_kind` — always `external`; `origin_agent_id`, `origin_chat_id`,
  `origin_task_id` — always null (a CLI requester is not a platform agent,
  chat or task).
- `depth` — 1 or 2.
- `root` — own `id` at depth 1; the `--root` value at depth 2.
- `group` — `--group` or null.

There is no config or registry footprint: nothing is written to
`.cinna/account.json`, `.cinna/config.json` or `~/.cinna/agents.json`. <!-- nocheck -->

## External contracts (cinna-core backend dependencies)

Owner side — all through `POST /api/v1/cli/account/api-proxy` with the account
CLI token (`token_type="cli-account"`); the proxy mirrors the inner status and
body:

- `GET tasks/delegation-capabilities` — returns `{version: 1, …}`; 404/405 means
  unsupported.
- `POST tasks/` — creates the task. **Must deduplicate on `external_ref`**:
  a second create with the same `external_ref` returns the existing task,
  ignores new title/brief, and does not start execution again. The CLI's retry
  safety relies entirely on this.
- `GET tasks/{id}/detail` — task state, results, open question + result id.
- `PUT tasks/{id}/delegation-result` — record a result on a task.
- `POST tasks/{id}/delegation-reply` — answer a question by `result_id`.

Executor side — direct call, agent-token auth:

- `POST /api/v1/agent/tasks/current/delegation-result` — headers
  `Authorization: Bearer <AGENT_AUTH_TOKEN>`, `X-Agent-Env-Id: <ENV_ID>`; body is
  the report payload plus `source_session_id`. The backend verifies the session
  belongs to this environment's agent and owner, and resolves the task from the
  session. Any 2xx is success.

Related, not in this repo: cinna-core's cloud MCP task server exposes a
`handover_report` tool for the same executor-side report.

## Edge cases & guardrails (preserve these)

- **Identity is target+key only** — title, brief, group, depth and `--execute`
  must not enter `external_ref` or the delegation id, or retries would create
  duplicates (`src/cinna/delegation.py:create()`).
- **No local dedup** — do not add a local "already created" cache; the backend
  dedup is the single source of truth.
- **Depth/root validated client-side** — depth outside 1–2, `--root` at depth
  1, or missing `--root` at depth 2 are usage errors before any request.
- **Capability gate first** — every owner-side verb gates before its real call;
  an unsupported server gets no task-route traffic.
- **Blocked requires a question; artifacts validated** — rejected client-side
  (`report()`), never sent half-valid to the backend.
- **Executor env is mandatory** — missing `AGENT_AUTH_TOKEN`, `BACKEND_URL` or
  `ENV_ID`, or an unreadable context / missing `backend_session_id`, fails
  before any network call.
- **No redirects on the executor POST** — a redirect would forward the bearer
  token to another origin; `follow_redirects=False` keeps it on `BACKEND_URL`.
- **Any 2xx accepted** — non-2xx becomes a `PlatformError` (exit 1 for 4xx,
  12 for 5xx) carrying the backend detail.
- **`--json` envelope** — always `{"result": "ok", "delegation": …}` via
  `console.emit_result()`; errors use the standard `CinnaExit` JSON line.
