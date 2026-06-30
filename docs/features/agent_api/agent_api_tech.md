# Agent REST API — Technical Reference

Implementation of [agent_api.md](agent_api.md). cinna-cli is a Python CLI; all
logic lives in `src/cinna/`, tests in `tests/`. Every command in this feature is
**account-scoped**: it runs from an account workspace (`.cinna/account.json`) and
talks to the platform through `AccountClient` with the account token.

## File locations

- `src/cinna/main.py` — the `cinna agent-api` group, the `cinna connect` group
  (`connect agent-api` / `connect mcp`), and the standalone `cinna api` command.
- `src/cinna/account.py` — the `run_*` handlers, agent resolution, and the
  shared status renderer.
- `src/cinna/client.py` — `AccountClient` methods that hit the account routes
  (`set_agent_api_enabled`, `refresh_agent_api`, `get_agent_api_spec`,
  `call_agent_api`, `connect_agent_api`, `connect_mcp`, `api_proxy`).
- Tests: `tests/test_account.py` (enable/refresh/spec/call, connect agent-api,
  `cinna api` exit-code matrix, `api_proxy` client behavior),
  `tests/test_client.py` (`AccountClient` route shapes).

## Command surface

Each verb → its handler in `src/cinna/main.py` → the `run_*` in
`src/cinna/account.py`:

- `cinna agent-api enable` → `src/cinna/main.py:agent_api_enable()` →
  `src/cinna/account.py:run_agent_api_enable()`
- `cinna agent-api refresh` → `src/cinna/main.py:agent_api_refresh()` →
  `src/cinna/account.py:run_agent_api_refresh()`
- `cinna agent-api spec` → `src/cinna/main.py:agent_api_spec()` →
  `src/cinna/account.py:run_agent_api_spec()`
- `cinna agent-api call` → `src/cinna/main.py:agent_api_call()` →
  `src/cinna/account.py:run_agent_api_call()`
- `cinna connect agent-api` → `src/cinna/main.py:connect_agent_api()` →
  `src/cinna/account.py:run_connect_agent_api()`
- `cinna connect mcp` → `src/cinna/main.py:connect_mcp()` →
  `src/cinna/account.py:run_connect_mcp()` (sibling — the MCP half)
- `cinna api` → `src/cinna/main.py:api_cmd()` →
  `src/cinna/account.py:run_api()`

## Key functions & flow

- `src/cinna/account.py:_resolve_account_agent()` — resolves a name/slug/id ref
  against `AccountClient.list_account_agents()`; raises a `ClickException`
  (`No accessible agent matches '<ref>'`) **before** any agent-api call. Every
  handler resolves first, so a bad ref never reaches the network.
- `src/cinna/account.py:run_agent_api_enable()` — resolves, calls
  `client.set_agent_api_enabled(id, enabled=…)`, prints status; on enable, also
  prints the "author under `agent_api/*.py` + `policy.yaml`, sync, refresh,
  spec" next-steps nudge.
- `src/cinna/account.py:run_agent_api_refresh()` — resolves, calls
  `client.refresh_agent_api(id)`, prints status; warns when the returned status
  carries `last_error` (harvest failed — the call itself does **not** raise).
- `src/cinna/account.py:run_agent_api_spec()` — resolves, calls
  `client.get_agent_api_spec(id)`, then `json.dumps(spec, indent=2)` to **plain
  stdout** (`click.echo`, no Rich decoration so it pipes), or writes it to
  `--output`.
- `src/cinna/account.py:run_agent_api_call()` — parses `--query k=v` (repeatable;
  duplicate keys collapse to a list) and `--json`, calls
  `client.call_agent_api(id, method, path, query, json_body)`, prints
  `→ METHOD path [status]` + the (pretty-printed if JSON) body, and
  `sys.exit(1)` on a non-2xx.
- `src/cinna/account.py:_print_agent_api_status()` — shared renderer for the
  enable/refresh status dict: `Enabled`, `State` (live serving child),
  `Spec available`, `Spec harvested <relative age>` (via `_humanize_age()`),
  `Env status`, `Last error`. Reused by `cinna agent show`.
- `src/cinna/account.py:run_connect_agent_api()` — resolves **both** producer and
  consumer, calls `client.connect_agent_api(producer_id, consumer_id,
  credential_label, read_only_override)`, prints the credential id / token
  prefix / base + spec URLs and the credential-sync reminder.
- `src/cinna/account.py:run_api()` — the escape hatch. Parses `--json` |
  `--data @file` (mutually exclusive) + `--query`, calls
  `client.api_proxy(method, path, query, json_body)`, then branches on the
  marker header (see below) to set the exit code.

## The escape hatch's exit-code logic (`run_api`)

`src/cinna/account.py:run_api()` distinguishes three outcomes by the presence of
the proxied marker header `_PROXIED_HEADER` (`src/cinna/account.py` —
`"x-cinna-proxied"`):

- **Header absent** → the hatch itself refused (policy denial, rate limit, size
  cap). Print the detail to **stderr**, prefix `blocked by platform policy: ` for
  400/403, append `Retry after <n>s` for a 429 with `Retry-After`, and
  `sys.exit(2)`.
- **Header present, 2xx** → mirrored inner response: print body to stdout,
  return 0.
- **Header present, 4xx/5xx** → mirrored inner error: print body to stdout,
  `HTTP <code>` to stderr, `sys.exit(1)`.

This is what lets a caller tell "the platform said no" (exit 2) from "the target
route errored" (exit 1). `cinna agent-api call` shares the 0/1 half of the
contract but has no exit-2 case (it returns the buffered
`{status_code, body, is_json}` dict, not a raw response).

## Client methods (`src/cinna/client.py`, `AccountClient`)

- `set_agent_api_enabled(agent_id, enabled)` → `POST
  /api/v1/cli/account/agent-api/enable`; returns the status dict.
- `refresh_agent_api(agent_id)` → `POST /api/v1/cli/account/agent-api/refresh`;
  re-imports `agent_api/` modules + re-parses `policy.yaml`; `last_error`
  reflects a harvest failure (never raises on one).
- `get_agent_api_spec(agent_id)` → `GET /api/v1/cli/account/agent-api/spec`
  (`agent_id` as a query param); the harvested OpenAPI spec.
- `call_agent_api(agent_id, method, path, query, json_body)` → `POST
  /api/v1/cli/account/agent-api/call`; owner-preview invocation, returns
  `{status_code, headers, body, is_json}`.
- `connect_agent_api(producer_agent_id, consumer_agent_id, credential_label,
  read_only_override)` → `POST /api/v1/cli/account/connect/agent-api`.
- `connect_mcp(connector_id, consumer_agent_id, …)` → `POST
  /api/v1/cli/account/connect/mcp` (the sibling MCP half).
- `api_proxy(method, path, query, json_body)` → `POST
  /api/v1/cli/account/api-proxy`; returns the **raw** `httpx.Response` (the
  backend mirrors the inner status/body 1:1, so non-2xx is normal output here),
  raising only on a 401 (invalid account token).

## External contracts (platform routes consumed)

All under `/api/v1/cli/account/*`, account-token auth:

- `POST …/agent-api/enable` — toggle the producer API; returns status.
- `POST …/agent-api/refresh` — force a spec + policy re-harvest; returns status
  (with `last_error` on a harvest failure).
- `GET  …/agent-api/spec` — the harvested OpenAPI spec JSON.
- `POST …/agent-api/call` — owner-preview endpoint invocation (query forwarded,
  no consumer token, no policy edge).
- `POST …/connect/agent-api` — mint the producer token + attach to the consumer
  as a credential.
- `POST …/connect/mcp` — the agent2agent MCP wiring (sibling).
- `POST …/api-proxy` — the buffered JSON escape hatch behind `cinna api`. See
  the "Platform API Endpoints" table in `docs/README.md` for the canonical
  account-route list.

Producer-side author contract (not CLI files — they live in the producer's
synced workspace, harvested by the platform): `workspace/agent_api/*.py`
(endpoint modules) and `workspace/policy.yaml` (access/guardrail policy).

## Edge cases & guardrails (preserve these)

- **Resolve before call.** Every handler runs `_resolve_account_agent()` first;
  a bad ref must fail without any agent-api/connect call.
  (`tests/test_account.py:test_agent_api_unknown_agent`,
  `…test_connect_agent_api_unknown_producer`)
- **Harvest error is data, not an exception.** `refresh_agent_api` returns a
  status with `last_error`; `run_agent_api_refresh` must print + warn, not crash.
  (`tests/test_account.py:test_agent_api_refresh_surfaces_harvest_error`)
- **Spec to plain stdout.** `run_agent_api_spec` must `click.echo` undecorated
  JSON so it pipes/parses; only `-o` diverts to a file.
  (`tests/test_account.py:test_agent_api_spec_prints_json`,
  `…test_agent_api_spec_writes_to_file`)
- **`call` forwards query + maps exit code.** Query params reach
  `call_agent_api`; an inner 4xx prints the body but exits 1.
  (`tests/test_account.py:test_agent_api_call_forwards_query`,
  `…test_agent_api_call_nonzero_exit_on_error_status`)
- **Escape-hatch header drives the exit code.** Absent marker header ⇒ exit 2
  (policy/limit), present ⇒ exit 0/1 by inner status; a 429 appends Retry-After.
  An inner 403 (header present) is **not** policy-prefixed. (`api_proxy` itself
  raises only on 401.)
  (`tests/test_account.py` — the `test_api_*` matrix,
  `…test_account_client_api_proxy_*`)
- **`--json` / `--data` mutual exclusion** in `run_api`; `--data @file` strips a
  leading `@`. (`src/cinna/account.py:run_api()`)
