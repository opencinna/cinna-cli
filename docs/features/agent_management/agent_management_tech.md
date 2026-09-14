# Agent Management — Technical Reference

Implementation of [agent_management.md](agent_management.md). cinna-cli is a Python
CLI; all logic lives in `src/cinna/`, tests in `tests/`. The `cinna agent`
lifecycle verbs run entirely against the account-scoped API through
`src/cinna/client.py:AccountClient`. (The `cinna agent schedule` subgroup is
out of scope here — see Agent Schedules.)

## File locations

- `src/cinna/main.py` — the `cinna agent` Click group and its command stubs
  (each a thin shim delegating into `src/cinna/account.py`).
- `src/cinna/account.py` — every `run_agent_*` / `run_status_*` handler, plus the
  shared `AGENT_REF` resolvers and the human-output renderers.
- `src/cinna/client.py` — `AccountClient` (account-token HTTP) carries the
  account-scoped endpoints these verbs call.
- `src/cinna/bootstrap.py` — the shared provisioning/teardown helpers reused by
  `agent sync` / `agent unsync` (so a synced workspace equals a `cinna setup` one).
- `src/cinna/config.py` — `CinnaConfig`, the agent registry (`~/.cinna/agents.json`)
  writers, and the `agents/<slug>/` layout helpers.
- Tests: `tests/test_account.py` — sync/unsync/create/restart-env/rebuild-env/
  show/status command tests plus the `AccountClient` endpoint tests (see
  "Command surface").

## Command surface

Each `cinna agent` verb → its Click stub in `src/cinna/main.py` → its handler in
`src/cinna/account.py`:

- `cinna agent sync` → `src/cinna/main.py:agent_sync()` → `account.py:run_agent_sync()`
- `cinna agent unsync` → `src/cinna/main.py:agent_unsync()` → `account.py:run_agent_unsync()`
- `cinna agent create` → `src/cinna/main.py:agent_create()` → `account.py:run_agent_create()`
- `cinna agent restart-env` → `src/cinna/main.py:agent_restart_env()` → `account.py:run_agent_restart_env()`
- `cinna agent rebuild-env` → `src/cinna/main.py:agent_rebuild_env()` → `account.py:run_agent_rebuild_env()`
- `cinna agent show` → `src/cinna/main.py:agent_show()` → `account.py:run_agent_show()`
- `cinna agent prompts pull` → `src/cinna/main.py:agent_prompts_pull()` → `account.py:run_agent_prompts_pull()`
- `cinna agent prompts diff` → `src/cinna/main.py:agent_prompts_diff()` → `account.py:run_agent_prompts_diff()`
- `cinna agent prompts push` → `src/cinna/main.py:agent_prompts_push()` → `account.py:run_agent_prompts_push()`
- `cinna agent status show` → `src/cinna/main.py:agent_status_show()` → `account.py:run_status_show(force_refresh=False)`
- `cinna agent status refresh` → `src/cinna/main.py:agent_status_refresh()` → `account.py:run_status_show(force_refresh=True)`
- `cinna agent status set-command` → `src/cinna/main.py:agent_status_set_command()` → `account.py:run_status_set_command()`

The group/subgroup objects are `src/cinna/main.py:agent()` and
`src/cinna/main.py:agent_status()`.

## Key functions & flow

- `src/cinna/account.py:run_agent_sync()` — the mint+materialize sequence:
  1. `find_account_root()` + `load_account_config()` locate the control plane.
  2. `AccountClient.list_account_agents()` → `_resolve_account_agent()` resolve
     `AGENT_REF`; `resolve_clone_slug()` + `workspace_agent_id_at()` decide the
     clone-root name and refuse a same-agent re-sync.
  3. `AccountClient.mint_agent_token()` returns the child token; the payload is
     turned into a `CinnaConfig` (`config_from_payload`).
  4. `bootstrap.prepare_git_layout()` (Model-A layout + best-effort coordinates) →
     `persist_config()` → `bootstrap.provision_workspace()` (Mutagen / clone /
     context files) → `bootstrap._maybe_autolink()` (git link if versioned).
- `src/cinna/account.py:run_agent_unsync()` — teardown:
  `resolve_child_workspace()` → confirm → `sync_session.stop()` →
  `AccountClient.revoke_child_token()` (best-effort) →
  `remove_agent_registry()` → `bootstrap.remove_workspace_artifacts()`.
- `src/cinna/account.py:run_agent_create()` — `AccountClient.create_agent(name,
  description, user_workspace_id)`, then print id / web-UI link / sync hint.
- `src/cinna/account.py:run_agent_restart_env()` — resolve the agent; if a local
  child workspace exists (`resolve_child_workspace()`), read `sync_session.status()`
  and warn+confirm when `pending_to_remote > 0` or `conflict_count > 0`; then
  `AccountClient.restart_agent_env()` (blocking) and print status.
- `src/cinna/account.py:run_agent_rebuild_env()` — the same shape as
  `run_agent_restart_env()` plus a second gate: the D2 unsynced-changes
  warn+confirm, then a `Rebuild …?` confirmation (default No) that `--yes`
  skips. `--yes` deliberately does **not** disarm the D2 guard, so it is not a
  general non-interactive switch. Then `AccountClient.rebuild_agent_env()`
  (blocking, 1800 s) and print status. When the response's `was_running` is
  false and the post-rebuild `status` is not `running`, it adds the "left
  stopped" note — a rebuild restores the state it found, and silence there
  turns "rebuilt successfully" into a container that still answers nothing.
  Otherwise, with a string `environment_id`, `_wait_for_env_health()` polls
  `AccountClient.get_environment_health()` every `ENV_HEALTH_POLL_SECONDS` until
  `status` is `healthy`/`ok` (`True` → "ready to chat") or
  `ENV_HEALTH_WAIT_SECONDS` (180) pass (`False` → warning naming `cinna agent
  status refresh`). A transport error or `PlatformError` counts as not yet; a
  non-dict answer returns `None` and prints nothing — an unreadable shape is not
  evidence of an unhealthy container.
  (`tests/test_account.py:test_agent_rebuild_env_waits_for_the_health_check`,
  `test_agent_rebuild_env_says_when_the_environment_never_answers`)
- `src/cinna/account.py:_establish_sync_session()` — called at the end of
  `run_agent_sync()` (after `_maybe_autolink`, so on the final workspace root):
  `sync_session.ensure_session()` + `sync_session.flush()` under a spinner. Any
  exception → warning with the first line of the error and the `cinna sync push
  --agent <slug>` to run before editing, returns `False`; conflicts at start →
  warning naming `cinna sync conflicts --diff`. The sync hint lines printed after
  it include `cinna sync push --agent <slug>` when the session started. The test
  module stubs it with an autouse fixture so no test reaches the real Mutagen
  daemon. (`test_agent_sync_starts_the_sync_session_on_the_fresh_clone`,
  `test_establish_sync_session_flushes_a_baseline`,
  `test_establish_sync_session_failure_names_the_push_to_run_first`)
- `src/cinna/account.py:_index_error_remedy(code, ref)` — the one table mapping
  a `skills_error` / refresh `error` code to its remedy lines. All four platform
  codes are named (`env_not_running` → wake it, `adapter_error` →
  `restart-env`, `adapter_unsupported` → `rebuild-env`, `parse_error` →
  `skills refresh`); an unrecognised code gets a generic line and **no** verb.
  Both `run_skills_refresh()` and `_print_addons()` (i.e. `cinna skills list`)
  route through it — before that each hardcoded a single remedy and was
  therefore wrong for most codes.
- `src/cinna/account.py:run_agent_show()` — `AccountClient.inspect_agent()` then
  render prompts (`entrypoint`/`workflow`/`refiner`), features, credentials, and
  `agent_api_status` via `_print_agent_api_status()`. Truncates prompts > 2000
  chars unless `--full` or stdout is non-TTY (`_stdout_is_tty()`). Each label is
  printed through `_esc()`: Rich reads a bare `[workflow]` as a style tag and
  silently drops it. Credentials come from `_fetch_linked_credentials()` (skipped
  under `--prompts`) and render via `_credential_summary()`; inspect's
  name+type list is the fallback when that returns `None`.
- `src/cinna/account.py:_fetch_linked_credentials()` — `AccountClient.list_agent_credentials()`
  says which credentials are linked; `service_uri` / `is_placeholder` / `status`
  are then overlaid by id from `AccountClient.list_credentials()`, because the
  agent route returns `service_uri: null` even for a credential that has a slot.
  A linked credential missing from the account listing with no slot gets
  `_SLOT_UNVERIFIED`. Any refusal → `None` (best-effort; never fails the command).
- `src/cinna/account.py:_credential_summary()` — name, `(type)`, `slot:` /
  `slot: unknown` / `no slot` (only when the payload carries `service_uri` at
  all), placeholder / needs-setup marker, id.
- `src/cinna/account.py:run_agent_prompts_pull()` — `AccountClient.get_agent()`,
  refuse (unless `--force`) a folder holding files with no baseline, another
  agent's baseline, or edits that differ from the baseline; write the five
  `PROMPT_TEXT_FILES` + `PROMPT_EXAMPLES_FILE`; `_save_prompt_baseline()` writes
  `.pulled.json` (`agent_id`, `agent_name`, `pulled_at`, `fields`).
- `src/cinna/account.py:run_agent_prompts_diff()` — `_read_prompt_files()` vs
  `_platform_prompt_fields()`; `_prompt_field_diff()` per differing field, noting
  a field the platform moved since the pull (`_baseline_fields_for()`).
- `src/cinna/account.py:run_agent_prompts_push()` — for each present file: skip
  when unchanged since the pull (never revert a platform-side change), skip when
  equal to the platform; collect the rest into the body and refuse (unless
  `--force`) any also changed on the platform since the pull.
  `AccountClient.update_agent_config()` (one PUT), then
  `AccountClient.sync_agent_prompts()` when a `DOC_BACKED_PROMPTS` field was sent
  and `--sync-env` (a `PlatformError` there is reported as "next start"). The new
  baseline keeps the pulled values of fields not pushed.
- `src/cinna/account.py:_prompt_same()` / `_prompt_text()` — comparison ignores
  trailing whitespace; `example_prompts` compares as a list.
  `_read_prompt_files()` refuses an `example_prompts.json` that is not a JSON list
  of strings before any write.
- `src/cinna/account.py:run_status_show()` — `AccountClient.get_agent_status(
  force_refresh=…)` then `_print_agent_status()`.
- `src/cinna/account.py:run_status_set_command()` —
  `AccountClient.set_status_refresh_command()` then echo the stored command.

### Shared resolvers & renderers (`src/cinna/account.py`)

- `_resolve_account_agent(items, agent_ref)` — id / exact-name / slug match
  against the `/account/agents` listing; fail-loud on no-match and ambiguous-slug.
- `_resolve_one_agent(client, agent_ref)` — convenience wrapper that fetches the
  listing and delegates to `_resolve_account_agent()` (used by the status verbs).
- `resolve_child_workspace(account_root, agent_ref)` — find the synced child
  folder under `agents/` (id / dir-name / agent-name slug); used by unsync and the
  restart-env guard.
- `_print_agent_status(result)` — renders `{status, status_refresh_command}`
  (severity color map, summary, `reported_at`/`fetched_at` ages, optional
  `refresh_command_warning`, and the `STATUS.md` body).
- `_print_agent_api_status(status)` — renders the REST-API block in `agent show`.

## Config & registry

- **No new config file.** `agent sync` writes the same per-agent `.cinna/config.json`
  (`CinnaConfig`, via `persist_config()`) and the same `~/.cinna/agents.json`
  registry entry as `cinna setup`; `agent unsync` removes both
  (`remove_agent_registry()` + `remove_workspace_artifacts()`).
- The **account** side reads `.cinna/account.json` (`AccountConfig`) — the
  `cli-account` token and `platform_url` / `frontend_url` / `user_workspace_id`
  the verbs thread into create/mint calls. Owned by the Account Workspace feature.
- `bootstrap.GENERATED_WORKSPACE_FILES` is the canonical list of CLI-generated
  files `agent unsync` deletes (alongside `.cinna/` and synced prompt-ref guides).

## External contracts

All account-token-authenticated (`Authorization: Bearer <cli-account JWT>`),
under `/api/v1/cli/account/`:

- `GET  …/agents` (`AccountClient.list_account_agents`) — accessible agents;
  every verb resolves `AGENT_REF` against this.
- `POST …/agents/{id}/mint` (`AccountClient.mint_agent_token`) — mint a per-agent
  child CLI token; returns `{token, id, agent_id, agent_name, environment_id,
  template, frontend_url, knowledge_sources}`.
- `DELETE …/tokens/children/{token_id}` (`AccountClient.revoke_child_token`) —
  revoke a child token this account minted; idempotent, 404 if not a child of
  this account token (provenance-scoped, no existence leak).
- `POST …/agents` (`AccountClient.create_agent`) — thin agent create; only
  user-specified fields sent, backend applies all defaults.
- `POST …/agents/{id}/restart-env` (`AccountClient.restart_agent_env`) — bounce
  the container; **blocks until back**; returns `{environment_id, status,
  status_message}`.
- `POST …/agents/{id}/rebuild-env` (`AccountClient.rebuild_agent_env`) — recreate
  the container, replacing `/app/core` from the template: the only way a
  container built before a feature gains that feature's routes, which a restart
  of the same image cannot do. **Blocks for the whole rebuild**, so the call
  carries its own 1800 s timeout rather than the client default. Returns
  `{environment_id, status, status_message, was_running}`; `was_running` is what
  lets the CLI explain a successful rebuild that came back stopped.
- `GET  …/agents/{id}/inspect` (`AccountClient.inspect_agent`) — effective
  `{name, id, prompts:{entrypoint,workflow,refiner}, features, credentials:[{name,
  type}], agent_api_status}` (never secret values).
- `GET  …/agents/{id}/status` (`AccountClient.get_agent_status`,
  `?force_refresh=true`) — `{status, status_refresh_command}`; force-refresh wakes
  a suspended env and re-reads `STATUS.md`, cache-falling-back on failure (never
  raises server-side).
- `POST …/agents/{id}/status/refresh-command`
  (`AccountClient.set_status_refresh_command`) — set the pre-command (raw string
  or `/run:<name>`; empty string opts out).

Platform routes reached through the account escape hatch
(`POST /api/v1/cli/account/api-proxy`, `AccountClient._proxy_json`):

- `GET agents/{id}` (`AccountClient.get_agent`) — the stored `workflow_prompt`,
  `entrypoint_prompt`, `refiner_prompt`, `router_trigger_prompt`, `description`,
  `example_prompts` that `prompts pull` / `diff` / `push` compare against.
- `PUT agents/{id}` (`AccountClient.update_agent_config`) — the bulk prompt write;
  omitted keys are left unchanged, which is what lets `push` send only edited fields.
- `POST agents/{id}/sync-prompts` (`AccountClient.sync_agent_prompts`) — rewrite
  the running env's `docs/*.md` from the database; errors without a running env.
- `GET agents/{id}/credentials` (`AccountClient.list_agent_credentials`) — the
  linked credentials. Returns `service_uri: null` even when the credential has a
  slot, so the slot is overlaid from `GET /api/v1/cli/account/credentials`.
- `GET environments/{id}/health` (`AccountClient.get_environment_health`) —
  `{status: "healthy", …}` once the container's server answers; the post-rebuild
  readiness signal.

`sync_session` (Mutagen wrapper) is the only non-HTTP external touch:
`sync_session.stop()` on unsync, `sync_session.status()` for the restart-env and
rebuild-env guards, and `ensure_session()` + `flush()` at the end of `agent sync`.

## Edge cases & guardrails (preserve these)

- **Same-agent re-sync refusal** — `run_agent_sync` compares
  `workspace_agent_id_at(clone_candidate)` to the resolved agent id and raises
  before minting, so a re-run doesn't duplicate or re-clone.
  (`tests/test_account.py:test_agent_sync_refuses_already_synced`)
- **Slug-collision suffix** — `resolve_clone_slug` bumps a *different* agent that
  slugs the same to `<slug>-<shorthash>` (shared with setup); a same-agent re-run
  still reports "already synced". (`tests/test_account.py`)
- **Unsync teardown always completes** — `revoke_child_token` failures (network /
  404 / missing `cli_token_id`) only warn; `remove_agent_registry` +
  `remove_workspace_artifacts` run regardless, user files preserved.
  (`test_agent_unsync_warns_when_revoke_404`,
  `test_agent_unsync_warns_when_revoke_unreachable`,
  `test_agent_unsync_skips_revoke_without_token_id`)
- **Restart confirm-before-clobber** — the unsynced-edits warning fires only when
  a local sync session reports `pending_to_remote`/`conflict_count > 0`; aborting
  must not call `restart_agent_env`.
  (`test_agent_restart_env_warns_on_unsynced_edits`)
- **`agent show` truncation respects pipes** — `show_full = full or not
  _stdout_is_tty()`, so redirected/piped output is never silently truncated.
  (`test_agent_show_truncates_long_prompt_on_tty`,
  `test_agent_show_non_tty_prints_whole_prompt`,
  `test_agent_show_full_flag_prints_whole_prompt`)
- **`status refresh` is non-fatal** — the backend cache-falls-back rather than
  erroring; the CLI passes `force_refresh` straight through.
  (`test_status_refresh_forces`)
- **`AGENT_REF` ambiguity is fail-loud** — `_resolve_account_agent` raises listing
  the collisions/available agents rather than guessing.
  (`test_agent_sync_unknown_agent`, `test_agent_sync_resolves_by_id_and_slug`)
