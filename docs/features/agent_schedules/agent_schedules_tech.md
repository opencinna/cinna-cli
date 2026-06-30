# Agent Schedules — Technical Reference

Implementation of [agent_schedules.md](agent_schedules.md). cinna-cli is a Python
CLI; all logic lives in `src/cinna/`, tests in `tests/`. The `cinna agent
schedule` subgroup is a thin command/render layer over account-API calls — there
is no local state.

## File locations

- `src/cinna/main.py` — the `cinna agent schedule` Click group + the seven verb
  commands (option parsing, client-side validation hand-off).
- `src/cinna/account.py` — the `run_schedule_*` handlers (account-root
  resolution, agent resolution, body assembly, Rich rendering).
- `src/cinna/client.py` — `AccountClient` schedule methods (the HTTP calls).
- Tests: `tests/test_account.py` — CLI tests for every verb (list render,
  create-body assembly, script-trigger command guard, partial update, cron-needs-
  tz guard, empty-update guard, run message, delete confirm, generate
  success/failure) plus `AccountClient` endpoint tests (respx).

## Command surface

Each `cinna agent schedule` verb → its handler in `src/cinna/main.py`, which
delegates to `src/cinna/account.py`:

- `cinna agent schedule list` → `src/cinna/main.py:agent_schedule_list()` →
  `src/cinna/account.py:run_schedule_list()`
- `cinna agent schedule generate` → `src/cinna/main.py:agent_schedule_generate()` →
  `src/cinna/account.py:run_schedule_generate()`
- `cinna agent schedule create` → `src/cinna/main.py:agent_schedule_create()` →
  `src/cinna/account.py:run_schedule_create()`
- `cinna agent schedule update` → `src/cinna/main.py:agent_schedule_update()` →
  `src/cinna/account.py:run_schedule_update()`
- `cinna agent schedule run` → `src/cinna/main.py:agent_schedule_run()` →
  `src/cinna/account.py:run_schedule_run()`
- `cinna agent schedule logs` → `src/cinna/main.py:agent_schedule_logs()` →
  `src/cinna/account.py:run_schedule_logs()`
- `cinna agent schedule delete` → `src/cinna/main.py:agent_schedule_delete()` →
  `src/cinna/account.py:run_schedule_delete()`

The group is declared `@agent.group(name="schedule")` — a subgroup of the
`cinna agent` group (`src/cinna/main.py:agent()`), so it inherits that family's
account-workspace assumption.

## Key functions & flow

Common preamble in every handler: `src/cinna/account.py:find_account_root()` +
`load_account_config()` locate the account workspace, then within an
`AccountClient` context `src/cinna/account.py:_resolve_one_agent()` (wraps
`_resolve_account_agent()` over `client.list_account_agents()`) maps the
`agent_ref` (id / exact name / slug) to the agent dict; its `id` keys every call.

- `src/cinna/account.py:run_schedule_list()` — `client.list_schedules(id)`, then
  `_print_schedules()` renders the Rich table (name+id, type, cron UTC, enabled,
  next run UTC).
- `src/cinna/account.py:run_schedule_generate()` — `client.generate_schedule(id,
  text, tz, schedule_type)`; raises `click.ClickException` when
  `success` is false (surfacing `error`); otherwise prints the cron / description
  / next-run preview and a ready-to-edit `create` command line.
- `src/cinna/account.py:run_schedule_create()` — **client-side guard**: a
  `script_trigger` with no non-empty `command` raises before any API call. Builds
  `body` with `name`, `cron_string`, `timezone`, `description` (defaults to
  `name`), `enabled`, `schedule_type`, and optional `prompt` / `command`, then
  `client.create_schedule(id, body)`.
- `src/cinna/account.py:run_schedule_update()` — assembles a **partial** body
  from only the supplied options; raises when the body is empty, and when
  `cron_string` is present without `timezone`. Calls
  `client.update_schedule(id, sid, body)`.
- `src/cinna/account.py:run_schedule_run()` — `client.run_schedule(id, sid)`;
  prints the returned `message` (env-state-aware).
- `src/cinna/account.py:run_schedule_logs()` — `client.schedule_logs(id, sid)`,
  then `_print_schedule_logs()` colorizes status (`success` /
  `session_triggered` / `error`), shows the command exit code, and truncates the
  detail (error message → command executed → prompt used) to 60 chars.
- `src/cinna/account.py:run_schedule_delete()` — `click.confirm` unless `--yes`,
  then `client.delete_schedule(id, sid)`.

## Field / body shape

The create body keys (`src/cinna/account.py:run_schedule_create()`):
`name`, `cron_string`, `timezone`, `description`, `enabled`, `schedule_type`,
and optionally `prompt` / `command`. The update body carries only changed keys
(`enabled`, `name`, `cron_string`, `timezone`, `prompt`, `command`,
`description`). A schedule record returned by the platform exposes (seen in
`tests/test_account.py:_SCHEDULE_ROW`): `id`, `agent_id`, `name`, `cron_string`,
`description`, `enabled`, `prompt`, `schedule_type`, `command`, `last_execution`,
`next_execution`, `created_at`, `updated_at`. There is **no** local config or
registry footprint — schedules are platform-only state.

## External contracts

All under the account-token-authenticated `/api/v1/cli/account/*` group, keyed by
`agent_id` (`src/cinna/client.py:AccountClient`):

- `GET  /api/v1/cli/account/agents/{id}/schedules` — `list_schedules()`
- `POST /api/v1/cli/account/agents/{id}/schedules/generate` —
  `generate_schedule()`; stateless, returns `{success, cron_string, description,
  next_execution}` or `{success:false, error}`.
- `POST /api/v1/cli/account/agents/{id}/schedules` — `create_schedule()`;
  **403 on a foreign install**.
- `PUT  /api/v1/cli/account/agents/{id}/schedules/{sid}` — `update_schedule()`;
  backend applies `exclude_unset`; on a foreign install only `enabled` may change
  (else 403).
- `DELETE /api/v1/cli/account/agents/{id}/schedules/{sid}` — `delete_schedule()`;
  **403 on a foreign install**.
- `POST /api/v1/cli/account/agents/{id}/schedules/{sid}/run` — `run_schedule()`;
  returns `{message}`; allowed on foreign installs.
- `GET  /api/v1/cli/account/agents/{id}/schedules/{sid}/logs` —
  `schedule_logs()`; the last 50 execution records.

Auth + transport: the account CLI token (`token_type="cli-account"`) in
`.cinna/account.json`; the same `_handle_response()` error mapping the rest of
`AccountClient` uses (403 → `PlatformError`/`ClickException` surfaced to the
user). The platform owns the cron clock, env wake-up, and run logging.

## Edge cases & guardrails (preserve these)

- **`script_trigger` requires a command** — guarded client-side in
  `run_schedule_create()` *before* the API call.
  (`tests/test_account.py:test_schedule_create_script_trigger_requires_command`)
- **Empty update refused** — `run_schedule_update()` raises on an all-`None`
  body rather than issuing a no-op PUT.
  (`tests/test_account.py:test_schedule_update_empty_errors`)
- **Cron requires timezone** — `run_schedule_update()` refuses `cron_string`
  without `timezone`.
  (`tests/test_account.py:test_schedule_update_cron_requires_tz`)
- **Description defaults to name** — server-required field is filled from `name`
  when `--description` is omitted.
  (`tests/test_account.py:test_schedule_create_builds_body`)
- **Generate failure is fail-loud** — `success:false` becomes a
  `click.ClickException` carrying the platform `error`.
  (`tests/test_account.py:test_schedule_generate_failure_raises`)
- **Foreign-install 403** — create / non-toggle update / delete are platform-
  enforced; the CLI surfaces the 403 rather than masking it. Toggle / run / logs
  stay available.
- **Agent-ref ambiguity** — `_resolve_account_agent()` raises with the matching
  agents when a name is ambiguous, asking for the id.
