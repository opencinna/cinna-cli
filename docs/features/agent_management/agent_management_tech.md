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
- Tests: `tests/test_account.py` — sync/unsync/create/restart-env/show/status
  command tests plus the `AccountClient` endpoint tests (see "Command surface").

## Command surface

Each `cinna agent` verb → its Click stub in `src/cinna/main.py` → its handler in
`src/cinna/account.py`:

- `cinna agent sync` → `src/cinna/main.py:agent_sync()` → `account.py:run_agent_sync()`
- `cinna agent unsync` → `src/cinna/main.py:agent_unsync()` → `account.py:run_agent_unsync()`
- `cinna agent create` → `src/cinna/main.py:agent_create()` → `account.py:run_agent_create()`
- `cinna agent restart-env` → `src/cinna/main.py:agent_restart_env()` → `account.py:run_agent_restart_env()`
- `cinna agent show` → `src/cinna/main.py:agent_show()` → `account.py:run_agent_show()`
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
- `src/cinna/account.py:run_agent_show()` — `AccountClient.inspect_agent()` then
  render prompts (`entrypoint`/`workflow`/`refiner`), features, credential
  name+type, and `agent_api_status` via `_print_agent_api_status()`. Truncates
  prompts > 2000 chars unless `--full` or stdout is non-TTY (`_stdout_is_tty()`).
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

`sync_session` (Mutagen wrapper) is the only non-HTTP external touch:
`sync_session.stop()` on unsync, `sync_session.status()` for the restart-env
guard — both shared with the rest of the CLI, not specific to this feature.

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
