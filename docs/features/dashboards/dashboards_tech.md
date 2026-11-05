# Dashboards — Technical Details

## File locations

- `src/cinna/dashboards.py`: the `cinna dashboards` Click group (component
  verbs, boards, tiles), the session/capability gate, remedies, the wake retry,
  the pre-read flush, reference/params/refresh helpers, board and tile
  resolution, the dashboard-chat context builder, the `check --run` exec path,
  and the import survey.
- `src/cinna/client.py`: `AccountClient` dashboard methods over the api-proxy,
  `get_dashboards_capabilities()`, `_dashboards_json()`,
  `DashboardsUnsupported`, `_body_detail()`, and
  `send_message(dashboard_context=…)`.
- `src/cinna/errors.py:ComponentRefusal`: core's `{error:{code, message,
  retry_after_s?, field?}}` refusals, with their exit codes and
  `with_remedy()`.
- `src/cinna/chat.py:run_chat()`: the `--board` / `--focus` / `--context` /
  `--new-chat` path; `_emit_tile_changes()`.
- `src/cinna/main.py`: registers the group (`_attach_driver_options()` adds
  `--json` / `--no-input` to every leaf) and the chat flags with their usage
  checks in `chat_cmd()`.
- `src/cinna/context.py`: `_dashboards_section()`, `_dashboards_skill_path()`
  and `local_kit_path()` render the per-agent guide's §7b.
  `generate_context_files()` records the building context's `platform_skills`.
- `src/cinna/config.py:CinnaConfig`: the `bundle_id` and `platform_skills`
  fields.
- `src/cinna/account.py`: `_fetch_bundle_id()` (agent sync),
  `_backfill_bundle_ids()` (refresh-context), `_fetch_components_summary()` /
  `_print_components_summary()` (agent show).
- `src/cinna/local_import.py`: `_report_components()` and the components block
  in `_print_summary()`.
- `src/cinna/templates/CLAUDE.md.template`: §7b placeholder, layout tree,
  "Where to put what" rows, dev-loop rows.
- `src/cinna/templates/ACCOUNT_CLAUDE.md.template`: the Dashboards table and
  the "Which one?" advice.
- `tests/test_dashboards.py` covers the helpers, every verb against a
  respx-mocked proxy, refusals and remedies, the wake retry, the flush,
  `check --run`, the desktop read-only rule, whole-set params,
  `cinna chat --board`, the per-agent guide and the import survey. The fixture
  asserts no `…/frame` path was ever called.

## Command surface

- `cinna dashboards list AGENT`: `src/cinna/dashboards.py:list_cmd()`
- `cinna dashboards show AGENT NAME`: `src/cinna/dashboards.py:show()`
- `cinna dashboards check AGENT [NAME] [--run]`: `src/cinna/dashboards.py:check()`
- `cinna dashboards run AGENT NAME`: `src/cinna/dashboards.py:run_cmd()`
- `cinna dashboards ref AGENT NAME [--check]`: `src/cinna/dashboards.py:ref_cmd()`
- `cinna dashboards boards`: `src/cinna/dashboards.py:boards_cmd()`
- `cinna dashboards board create NAME`: `src/cinna/dashboards.py:board_create()`
- `cinna dashboards add AGENT NAME --board B`: `src/cinna/dashboards.py:add()`
- `cinna dashboards tiles BOARD`: `src/cinna/dashboards.py:tiles_cmd()`
- `cinna dashboards tile data|refresh|params|call|edit|remove BOARD TILE`:
  `src/cinna/dashboards.py:tile_data()` / `tile_refresh()` / `tile_params()` /
  `tile_call()` / `tile_edit()` / `tile_remove()`
- `cinna chat --board B [--focus T] [--context @F] [--new-chat]`:
  `src/cinna/main.py:chat_cmd()` → `src/cinna/chat.py:run_chat()`

AGENT is resolved by `_Session.agent()`: a name, slug or id via
`account._resolve_account_agent()`, or `.` for the workspace's own agent.

## Key functions & flow

- `src/cinna/dashboards.py:_session()`: loads the account config, opens an
  `AccountClient`, and asks `get_dashboards_capabilities()`. None →
  `DashboardsUnsupported`; a version other than `SUPPORTED_VERSION` → refused.
  It yields `_Session`: `limit()` reads the capabilities' limits, else
  `DEFAULT_LIMITS`, and it caches the agent listing and the bundle id
  (`account._fetch_bundle_id()`, non-fatal: no id means no reference line).
- `src/cinna/client.py:AccountClient.get_dashboards_capabilities()`: None only
  for a mirrored 404/405/422 (no route). A hatch refusal (no marker header,
  for example the per-token rate limit) or any other error raises
  `PlatformError`.
- `src/cinna/client.py:AccountClient._dashboards_json()`: api-proxy call.
  Marker header absent → `PlatformError`. A coded `{error}` body →
  `ComponentRefusal`. `{detail:{code}}` → `CodedRefusal`. Anything else →
  `PlatformError`.
- `src/cinna/dashboards.py:_refusals()` + `remedy_for()`: catch a
  `ComponentRefusal` and re-raise it with the remedy appended
  (`ComponentRefusal.with_remedy()` keeps the server sentence as `sentence`).
- `src/cinna/dashboards.py:call_waking()`: retries `env_unavailable` until the
  deadline, sleeping `retry_after_s` or 5 s (`_sleep` is swapped out in tests)
  and printing one "waking" status.
- `src/cinna/dashboards.py:flush_local_changes()`: `_child_workspace()` finds
  the synced child under the account root. `sync_session.status()` must report
  an existing session; then `sync_session.flush()`. Failures are logged or
  warned, never raised.
- `src/cinna/dashboards.py:reference()`: `cinna://component/bundle:<id>/[<skill>/]<name>`.
  `_Session.bundle_id()` reads `GET agents/{id}` once.
- `src/cinna/dashboards.py:parse_params()`: `--params @file|JSON`, then
  `--param k=v`, JSON-first values.
- `src/cinna/dashboards.py:refresh_overrides()`: `max_age` and
  `while_open_every` checked with core's patterns (`_DURATION_RE`,
  `_DURATION_1M_RE`), plus `after_turn`.
- `src/cinna/dashboards.py:add()`: component (fresh) → `has_errors` refusal
  (`component_errors`) →
  `resolve_board()` (or, only on `BoardNotFound`, `create_dashboard()` with `--new-board`) →
  `refuse_desktop_board()` → block-limit check (`dashboard_full`) → `POST dashboards/{id}/blocks`
  with `grid_x/grid_y` 0 and `size_grid()` of `--size`, else the component's
  `size`, else `DEFAULT_SIZE`.
- `src/cinna/dashboards.py:resolve_board()`: web dashboards by id, exact name
  (casefolded) or a unique id prefix (4 or more characters). A full UUID that
  isn't among them is fetched with `get_dashboard()`, which is how a desktop
  dashboard is reached.
- `src/cinna/dashboards.py:resolve_tile()`: `agent_component` blocks only, by
  id, title, component name, or a unique id prefix.
- `src/cinna/dashboards.py:tile_params()`: stored params from the block config,
  merged locally (`--reset` → `--clear` → `--set`), then `set_tile_params()`
  with the whole set.
- `src/cinna/dashboards.py:check()`: core findings (source `core`). With
  `--run`, `run_env_checker()` per component adds findings not already present
  by `(path, code)` (source `run`). Any error → `CinnaExit` code
  `component_errors` with `components` in the extra fields. `--run` for an
  agent with no child workspace under this account → `not_synced`. `--timeout`
  bounds both the wake retry and each `--run` check.
- `src/cinna/dashboards.py:run_env_checker()`: `checker_command()` quoted into
  one string, streamed through `PlatformClient.stream_exec()` with the child
  workspace's token. Only `stdout` deltas feed `_parse_checker_output()`;
  `stderr` is kept for the error tail. Code `checker_missing` (with the
  rebuild remedy) only when stderr shows the interpreter couldn't open
  `ENV_CHECKER`.
- `src/cinna/dashboards.py:prepare_dashboard_chat()`: no capabilities, another
  version or no `chat` flag → `dashboard_chat_unsupported`; then resolves the board
  (desktop → `desktop_dashboard`), validates `--context` (a JSON object, at most
  `context_chars`), or builds the context with `build_dashboard_context()` from
  the focused tile's `get_tile_data()` and its component's `context`.
- `src/cinna/dashboards.py:build_dashboard_context()`: the panel's shape (see
  core's `DashboardChat/dashboardContext.ts`), trimmed in the panel's order.
- `src/cinna/chat.py:run_chat()`: with `board_ref`, snapshots tile params from
  the board already fetched (`tile_params_snapshot()`). It opens the session
  with `open_dashboard_chat()`, or with `--resume` requires the session's
  `dashboard_id` to be that board. It sends `dashboard_context`, and after the poll
  `_emit_tile_changes()` emits `{"event":"tiles", "changed":[…]}` before `done`.
  The `session` event gains `dashboard`.
- `src/cinna/dashboards.py:survey_local_components()`: matches
  `dashboards/<name>/component.json` and `skills/<skill>/dashboards/<name>/component.json`
  in the copy plan, warns per component, and runs the kit checker loaded by
  `_load_kit_checker()` (the kit dir the caller passes —
  the account workspace's `context/local-kit/skills/cinna-dashboards/scripts/check_component.py`,
  via importlib, `check_path(run=False)`). `local_import._report_components()`
  passes that kit dir; a `.cinna-kit/` beside the imported folder is never
  imported.
- `src/cinna/context.py:_dashboards_section()`: fills in the bundle-id
  reference prefix (or a pointer to `cinna dashboards ref`) and the relative
  kit path from `local_kit_path()` (the `context/local-kit/` of the account
  workspace above, else a standalone note). `_dashboards_skill_path()` takes
  the skill's `kit_path` from `config.platform_skills`, uses the kit's
  standard place when nothing was recorded, and returns None, so §7b tells the
  developer to rebuild, when the environment lists platform skills without
  `cinna-dashboards`.

## Config & registry

- `.cinna/config.json` (per agent) gains `bundle_id`. It's written by
  `account.run_agent_sync()` through `_fetch_bundle_id()` and backfilled by
  `account.run_account_refresh_context()` through `_backfill_bundle_ids()`. It
  stays `null` in a standalone `cinna setup` workspace. It isn't copied into
  `~/.cinna/agents.json`.
- `.cinna/config.json` also gains `platform_skills` (`[{name, kit_path}]`),
  written by `context.generate_context_files()` when the building context
  carries it (core `c32f211b`+; an older core leaves it empty).
- No dashboard state is kept locally: tiles, params, cached data and budgets
  are core's.

## External contracts

All of these go through `POST /api/v1/cli/account/api-proxy` with the account token:

- `GET dashboards/capabilities` → `{version: 1, contract_version, limits{runs_per_minute,
  blocks_per_dashboard, dashboards_per_user, context_chars, summary_chars,
  stdout_bytes, timeout_s_default, timeout_s_max}, chat, component_refs}` (core
  `c32f211b`+). A core without it answers 404, or 422 when its
  `dashboards/{dashboard_id}` route takes the path.
- `GET /api/v1/cli/agents/{id}/building-context` → also `platform_skills`
  (`[{name, kit_path}]`), and its building prompt prints the reference line
  with the agent's bundle id (env-core `GET /prompt/building?bundle_id=`).
- `GET agents/{id}/dashboards/?fresh=true` → `{components:[{name, skill, title,
  view, component_json, findings[], has_errors, size_bytes, placements[{dashboard_id,
  dashboard_name, block_id, origin}]}]}`. `fresh` is ignored by a core without it.
- `GET agents/{id}/dashboards/{name}?skill=&fresh=true` → one component.
- `POST agents/{id}/dashboards/{name}/run {skill?, endpoint?, params, tile_key:"cli"}`
  → `{value, fetched_at, notice, params_resolved}`.
- `POST dashboards/component-refs/resolve {refs}` → `{results:[{ref, status, message?, …}]}`.
- `GET dashboards/?origin=web` and `GET dashboards/{id}` → dashboards with
  `blocks` and `origin`. `POST dashboards/ {name, description?}`.
- `POST dashboards/{id}/blocks`, `PUT|DELETE dashboards/{id}/blocks/{block_id}`:
  block `config` is `{component, skill?, params, refresh{max_age?,
  while_open_every?, after_turn?}}` and core forbids extra keys.
- `GET …/component/data`, `POST …/component/refresh {reason:"manual"}`,
  `PUT …/component/params {params}`, `POST …/component/call {endpoint, params}`
  under `dashboards/{id}/blocks/{block_id}/`.
- `POST dashboards/{id}/chat {agent_id, new}` → `{session}`. Errors are
  `{detail}`, not coded.
- `POST sessions/{id}/messages/stream {content, dashboard_context}`: core keeps
  the context only on a session with a dashboard, refuses non-objects or more
  than 12,000 characters with 400, and stores it unrendered.
- `GET agents/{id}` → `bundle_id`. `GET environments/{id}` → `status` (the
  `agent show` guard).
- Never called: `POST agents/{id}/dashboards/{name}/frame`,
  `POST dashboards/{id}/blocks/{block_id}/component/frame`,
  `PUT …/component/page-state`. Core's api-proxy refuses all three since
  `c32f211b`.
- `cinna exec`: `POST /api/v1/cli/agents/{id}/exec` runs
  `python3 <ENV_CHECKER> /app/workspace/[skills/<skill>/]dashboards/<name> --agent /app/workspace --run --json`. <!-- nocheck: container paths -->
  `--agent` is required because a cloud workspace has no `cinna-agent.json`.

## Edge cases & guardrails

- `ComponentRefusal` exit codes: `invalid_input` → 2, `env_unavailable` → 12,
  everything else → 1. `call_failed` is the agent's script, not the platform
  (`src/cinna/errors.py:ComponentRefusal`).
- "No dashboards" is decided by the capabilities answer alone. A 429 from the
  hatch's per-token rate limit must surface as that 429, which the CLI's own
  parallel `run`s can hit before core's run budget (seen live)
  (`src/cinna/client.py:AccountClient.get_dashboards_capabilities()`).
- Findings: anything whose severity isn't `warning` is an error, the same in
  `list`, `show`, `check` and `add` (`src/cinna/dashboards.py:_is_error()`).
- `--json` tile lines put core's fields first and the CLI's (`tile_id`,
  `params`) over them, and never let a server `result` key replace the marker
  (`src/cinna/dashboards.py:_emit_tile()`).
- `agent show` must not wake environments: `_fetch_components_summary()` checks
  `GET environments/{id}` first and lists components only when the status is
  `running`. Any failure becomes a dim note, never a failed `show`. `cinna doctor`
  has no dashboards check for the same reason.
- Rich markup: finding lines carry `[code]`, so every print escapes them
  (`dashboards.py` uses `escape`/`_plain`, `local_import._report_components()`
  escapes its warnings).
- The reference line is printed with `click.echo` (`ref`) or `_plain()`
  (`list`, `show`), with no markup and no wrapping, so a pipe or the assistant
  copies it intact.
- `tile remove` under `--json` needs `--yes` (`account._require_yes_for_json`).
- Chat flag rules live in `src/cinna/main.py:chat_cmd()`: `--focus` /
  `--context` / `--new-chat` need `--board`; `--focus` and `--context`
  exclude each other; `--board` doesn't combine with `--attach`/`--show`;
  `--new-chat` doesn't combine with `--resume`.
- `--focus` refuses a tile of another agent: the tile tools only act on the
  chat agent's own tiles.
- Kit checker loading failures are silent (no kit copy beside the agent means
  no static check), and a crash inside `check_path` is logged, not raised.
