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
  `call_agent_api`, `connect_agent_api`, `connect_mcp`, `api_proxy`, and the
  four access methods `list_agent_api_access`, `issue_agent_api_key`,
  `revoke_agent_api_key`, `set_agent_api_scopes`) plus the `drop_token()` helper.
- Tests: `tests/test_account.py` (enable/refresh/spec/call, connect agent-api,
  `cinna api` exit-code matrix, `api_proxy` client behavior),
  `tests/test_client.py` (`AccountClient` route shapes, the access methods,
  coded refusals, `drop_token`), `tests/test_agent_api_access.py` (the four
  `access` verbs end to end against a mocked client), `tests/test_account.py`
  (key status cells, slot readiness key states), `tests/test_local_import.py`
  (`agent_api` spec skipped).

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
- `cinna agent-api access list` → `src/cinna/main.py:agent_api_access_list()` →
  `src/cinna/account.py:run_agent_api_access_list()`
- `cinna agent-api access issue` → `src/cinna/main.py:agent_api_access_issue()` →
  `src/cinna/account.py:run_agent_api_access_issue()` (`--to` required;
  `--expires-in-days` is `click.IntRange(1, 3650)`)
- `cinna agent-api access revoke` → `src/cinna/main.py:agent_api_access_revoke()` →
  `src/cinna/account.py:run_agent_api_access_revoke()`
- `cinna agent-api access scopes` → `src/cinna/main.py:agent_api_access_scopes()` →
  `src/cinna/account.py:run_agent_api_access_scopes()` (exactly one of `--set` /
  `--clear`, checked in the Click handler)
- `cinna connect agent-api` → `src/cinna/main.py:connect_agent_api()` →
  `src/cinna/account.py:run_connect_agent_api()`
- `cinna connect mcp` → `src/cinna/main.py:connect_mcp()` →
  `src/cinna/account.py:run_connect_mcp()` (sibling — the MCP half)
- `cinna api` → `src/cinna/main.py:api_cmd()` →
  `src/cinna/account.py:run_api()`, which calls
  `src/cinna/account.py:_reject_elided_path()` before anything else and
  `src/cinna/account.py:_resolve_api_agent_refs()` inside the client context.

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

## Access verbs: key functions

- `src/cinna/account.py:_access_recipient()` — `--to` as the request field:
  `me` becomes a null email, blank is a usage error.
- `src/cinna/account.py:_parse_scope_list()` — `a,b` to a list, `""` to an
  explicit empty list, unset to `None` (leave the grant as is).
- `src/cinna/main.py:_access_json_mode()` — each `access` command's own
  `--json` also turns on console JSON mode, so a refusal prints the
  `{"result": "error", "code", "detail", "http_status"}` line with the server's
  code instead of a bare `Error: <sentence>`
  (`tests/test_agent_api_access.py:test_json_refusal_carries_the_server_code`).
- `src/cinna/account.py:run_agent_api_access_list()` — one row per person/key;
  a grant-only person is one row with Key `—`; terminal keys filtered by
  `_TERMINAL_KEY_STATUSES` unless `--all` with an "N ended keys hidden" footer;
  `_print_access_footer()` prints external access on/off (warning plus
  `manage_url` when off) and the scope catalog.
- `src/cinna/account.py:run_agent_api_access_issue()` — catches a
  `CodedRefusal` with code `external_access_disabled` and re-raises it with
  `_manage_url()` appended only when the message has no `http` URL; self-issue
  prints the slot, `credential_url` and the `cinna_credentials.agent_api_session`
  hint, other-issue prints the revoke hint via `_agent_ref_for_hint()`;
  `_warn_unknown_scopes()` warns, never refuses.
- `src/cinna/account.py:_resolve_access_key()` — key id first, then exact
  `token_prefix`, resolved against a fresh access listing; elided refs
  (`_ELIDED_MARKERS`) are a usage error; none/several matches list candidates.
- `src/cinna/account.py:run_agent_api_access_revoke()` — `_require_yes_for_json()`
  first; already `revoked_by_issuer` returns without a call; other ended
  statuses ask "revoke it anyway?"; confirmation declined aborts with no call.
- `src/cinna/account.py:run_agent_api_access_scopes()` — `--clear` sends `None`;
  prints the resulting scopes or "Grant removed".
- `src/cinna/account.py:_KEY_STATUS_LABELS` / `_key_status_cell()` — the one
  status-word table (green Active; yellow API turned off by producer / External
  access paused; red Revoked by issuer / Producer removed / Issuer account
  removed / Expired; unknown values printed raw and dim).
- `src/cinna/account.py:_key_unavailable_state()`,
  `agent_api_producer_from_slot()`, `agent_api_issue_hint()` — shared by slot
  readiness and the importer; the hint derives the producer from a
  `<producer>-api` slot (else `<producer>`) and omits `--service-uri` when there
  is no slot.
- `src/cinna/client.py:drop_token()` — recursively removes every `token` key
  from a payload; applied by `AccountClient._access_response()` and again by
  each runner.

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
- `list_agent_api_access(agent_id)` → `GET
  /api/v1/cli/account/agent-api/access` (`agent_id` query param).
- `issue_agent_api_key(agent_id, *, issued_to_email, scopes, expires_in_days,
  service_uri, label, read_only)` → `POST /api/v1/cli/account/agent-api/keys`;
  `None` fields are omitted from the body.
- `revoke_agent_api_key(agent_id, key_id)` → `POST
  /api/v1/cli/account/agent-api/keys/revoke`.
- `set_agent_api_scopes(agent_id, user_email, scopes)` → `PUT
  /api/v1/cli/account/agent-api/access/scopes`; `scopes` is always sent, as an
  explicit `null` for `--clear` (core answers 422 if the field is omitted; null
  removes the grant), `user_email` omitted for the caller.
- All four go through `AccountClient._access_response()`: a bare
  `{"detail": "Not Found"}` 404 becomes a `PlatformError` saying the platform
  predates API access from the CLI; otherwise `_handle_response(coded=True)`
  raises a `CodedRefusal` for a `{detail: {code, message}}` body (not for 401);
  then `drop_token()`.
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
- `GET  …/agent-api/access?agent_id=` — people, scopes, keys (terminal keys
  included), `external_access_enabled`, `identity_enabled`, `scope_catalog`,
  `manage_url`.
- `POST …/agent-api/keys` — issue a key; answers the key's public fields plus
  `base_url`, `spec_url`, `service_uri`, `issued_to_self`, `credential_url`
  (self-issue only) and `unknown_scopes`. Never a key value.
- `POST …/agent-api/keys/revoke` — body `{agent_id, key_id}`; idempotent
  (`status: revoked_by_issuer`); the grant is untouched.
- `PUT  …/agent-api/access/scopes` — body `{agent_id, user_email, scopes}`;
  `scopes` is required but nullable, so `--clear` sends `"scopes": null`
  explicitly (omitting the field is a 422 on the core side); answers `{entry, unknown_scopes}`.
- `GET  …/credentials` — each row carries `agent_api_key_status` for issued
  keys (null for connections); `POST …/credentials` with type `agent_api` is
  refused with a 400 that names the issue command.
- `api-proxy` refusals added with issued keys: 403 `excluded_path` (CLI exit 2)
  for any method on `agents/*/agent-api/keys…`, `environments/*/workspace/download`
  and `view-file`, `dashboards/*/blocks/*/env-file`, and POST on A2A
  access-tokens, MCP/ACP connector tokens and agent / task-trigger webhook
  create/regenerate-token; paths are matched percent-decoded. The CLI needs no
  code for this: the hatch's own refusal already exits 2.
- `POST …/connect/agent-api` — mint the producer token + attach to the consumer
  as a credential.
- `POST …/connect/mcp` — the agent2agent MCP wiring (sibling).
- `POST …/api-proxy` — the buffered JSON escape hatch behind `cinna api`. See
  the "Platform API Endpoints" table in `docs/README.md` for the canonical
  account-route list.

Error codes on the four access routes (`{"detail": {"code", "message"}}`,
surfaced as `CodedRefusal`): `agent_not_found` (404, not owned: no leak),
`recipient_not_found` (404), `ambiguous_recipient` (409, case-only duplicate
emails), `external_access_disabled` (400), `api_disabled` (400), `key_not_found`
(404), and `invalid_request` / `not_found` / `conflict` for any other platform
refusal. 422 is body validation. A non-developer gets a plain 403 on writes.

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
- **No key value, ever.** `drop_token()` runs in the client and again in each
  runner before printing or `--json`; the revoke path also scrubs the listing it
  resolves the key from. (`tests/test_client.py` token-drop tests,
  `tests/test_agent_api_access.py` self-issue test with a mock that returns a
  `token`.)
- **`external_access_disabled` carries a URL.** The URL is appended only when
  absent so a platform that already names it is not doubled.
  (`src/cinna/account.py:run_agent_api_access_issue()`)
- **Unknown key status is unavailable, not usable.** `_key_unavailable_state()`
  returns `key_unavailable` for a status this CLI does not know; the cell prints
  it raw and dim. (`tests/test_account.py` slot readiness tests)
- **Confirm before revoke, nothing sent on decline.** `--json` without `--yes`
  is a usage error. (`tests/test_agent_api_access.py`)
- **`--json` / `--data` mutual exclusion** in `run_api`; `--data @file` strips a
  leading `@`. (`src/cinna/account.py:run_api()`)
