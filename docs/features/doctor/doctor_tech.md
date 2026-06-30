# Doctor — Technical Reference

Implementation of [doctor.md](doctor.md). cinna-cli is a Python CLI; all logic
lives in `src/cinna/`, tests in `tests/`.

## File locations

- `src/cinna/doctor.py` — the feature core: `diagnose()` (registry ↔ daemon
  reconciliation), the `Finding`/`SessionInfo` dataclasses, the per-finding fix
  factories, and `run_doctor()` (report + three-step repair).
- `src/cinna/main.py` — the `cinna doctor` command (`doctor()`), and the shared
  token probe `_probe_token_statuses()` that diagnose imports.
- `src/cinna/sync_session.py` — `list_all_sessions()`, `terminate_named()`,
  `session_name()` (the daemon-level helpers doctor drives).
- `src/cinna/config.py` — `list_agent_registry()`, `remove_agent_registry()`,
  `upsert_agent_registry()`, `load_config()`, `save_config()`, plus
  `CONFIG_DIR`/`CONFIG_FILE` (the `.cinna/config.json` intactness probe).
- `src/cinna/account.py` — `find_account_root()`, `load_account_config()`,
  `probe_account_token()` (account-token classification for re-mint gating).
- `src/cinna/client.py` — `AccountClient.mint_agent_token()` (the re-mint call).
- Tests: `tests/test_doctor.py` (per-category diagnosis, command surface,
  dry-run/`--yes`/decline, the active-session sweep, `cinna-*` scoping, ordering).

## Command surface

- `cinna doctor` → `src/cinna/main.py:doctor()` → `src/cinna/doctor.py:run_doctor()`
- `--dry-run` — report only, make no changes (`run_doctor(dry_run=True, …)`).
- `--yes` / `-y` — accept every step prompt without asking.

## Key functions & flow

- `src/cinna/doctor.py:diagnose()` — the reconciliation. Loads the registry
  (`list_agent_registry()`), lists Mutagen sessions
  (`sync_session.list_all_sessions()`; exceptions → empty list, registry-only
  run), probes tokens (`main._probe_token_statuses()`), and returns a
  `list[Finding]`. Per entry it computes `workspace_intact` (path set + exists +
  `.cinna/config.json` is a file), then emits at most one session finding and
  collects expired tokens; afterward it flags unclaimed `cinna-*` sessions as
  orphans and resolves the expired tokens.
- `src/cinna/doctor.py:_daemon_config()` — a throwaway `CinnaConfig` carrying only
  the daemon env (`MUTAGEN_SSH_PATH`, identical for every agent), built from the
  first registry entry; needed for `list_all_sessions`/`terminate_named`.
- `src/cinna/doctor.py:_label_for()` — display label: the agent's `agent_name`
  from its config when readable, else the first 8 chars of the agent id.
- `src/cinna/doctor.py:_account_root_for()` — walks up via
  `account.find_account_root()` to the `<account_root>/agents/<slug>/` parent;
  `None` ⇒ standalone.
- `src/cinna/doctor.py:_probe_account_token()` — classifies the account token
  (`valid`/`expired`/`unreachable`) via `account.probe_account_token()`; called at
  most once per account root (memoized in `diagnose`'s `account_status` dict).
- `src/cinna/doctor.py:_make_terminate()` / `_make_stale_fix()` / `_make_remint()`
  — fix-factory closures bound to their target (no late-binding); each returns a
  human-readable result string or raises.
- `src/cinna/doctor.py:run_doctor()` — scans, prints the report tables on every
  run, returns early on `--dry-run`, then runs the three confirmed steps and
  prints the applied/terminated summary.
- `src/cinna/doctor.py:_apply_step()` — confirm (default Yes; `--yes` skips the
  prompt), then apply a group of findings in category order, fail-soft per finding.
- `src/cinna/doctor.py:_collect_cinna_sessions()` — every live `cinna-*` session
  tagged with the agent + folder it serves (registry-backed; orphans derive the
  folder from the session's `alpha.path` parent).
- `src/cinna/doctor.py:_in_category_order()` — stable display/apply order from
  `CATEGORY_ORDER`.

## Category → fix mapping (`src/cinna/doctor.py`)

| Category | Trigger | Fix (apply) |
|----------|---------|-------------|
| `stale_folder` | `workspace_path` missing or `.cinna/config.json` gone | `_make_stale_fix` — `terminate_named` (if a session exists) + `remove_agent_registry` |
| `zombie_session` | session status `halted-on-root-deletion`, dir intact | `_make_terminate` |
| `dead_remote` | beta not connected, `lastError`, status `connecting…`, or `error` in status | `_make_terminate` |
| `orphan_session` | `cinna-*` session with no registry entry | `_make_terminate` |
| `token_remint` | expired CLI token, account root present, account token not expired | `_make_remint` |
| `account_token_expired` | expired CLI token(s) whose account token is itself expired | `None` (report-only; "run `cinna login`") |
| `token_report` | expired CLI token, standalone (no account root) | `None` (report-only; "run `cinna set-token`") |

`_STALLED_CATEGORIES = {stale_folder, zombie_session, dead_remote, orphan_session}`
groups the auto-fixable broken findings under the single step-1 "delete stalled
sessions" prompt.

## Repair-step ordering (`run_doctor`)

1. **Delete stalled sessions** — `_apply_step(stalled, …)`; one prompt clears all
   `_STALLED_CATEGORIES` findings.
2. **Terminate active sessions** — the live `cinna-*` sessions **not** already
   owned by a finding (`active = live − problem_sessions`); each cleared with
   `sync_session.terminate_named()`.
3. **Refresh tokens** — `_apply_step(remint, …)` runs the `token_remint`
   re-mints. `manual` findings (`apply is None`) are then printed as guidance,
   never applied.

The order is deliberate: tear down sessions before re-minting tokens so a healed
agent ends with a fresh token and no dangling session.

## Config & registry

- **Registry** (`~/.cinna/agents.json`, runtime state) — doctor's primary subject.
  Reads it via `list_agent_registry()`; removes stale entries via
  `remove_agent_registry(agent_id)`; a successful re-mint rewrites the entry via
  `upsert_agent_registry(agent_id, platform_url, cli_token, workspace_root,
  frontend_url=…)`. `workspace_path` stays the **agent dir**, so a left-in-place
  entry's git block and config resolution are untouched.
- **`.cinna/config.json`** — read with `load_config()` for the display label and
  re-mint; on re-mint, `cli_token` (and `cli_token_id`/`frontend_url` when the mint
  returns them) are written back with `save_config()`. Its presence under
  `workspace_path` is the `workspace_intact` test
  (`root / CONFIG_DIR / CONFIG_FILE`).
- **`.cinna/account.json`** — the parent account workspace; loaded via
  `load_account_config()` to mint child tokens and to probe the account token.

## External contracts

- **Mutagen daemon** (shared, user-owned). `list_all_sessions()` returns the raw
  session dicts; doctor relies on `name`, `status`, `lastError`,
  `beta.connected`, and `alpha.path`. Relied-on status strings:
  `halted-on-root-deletion` (zombie), `connecting…`/`error`-bearing statuses
  (dead remote). `terminate_named()` treats a "session not found" exit as
  already-gone (success). Scope is strictly `cinna-*` names.
- **Endpoint** `GET /api/v1/cli/agents/{id}/sync-runtime` (CLI JWT) — the cheap
  token-validity probe behind `_probe_token_statuses()`: 2xx ⇒ `valid`, 401 ⇒
  `expired`, else `unreachable`.
- **Endpoint** `GET /api/v1/cli/account/agents` (account token) — the account-token
  probe behind `account.probe_account_token()`: same 2xx/401/else classification.
- **Endpoint** `POST /api/v1/cli/account/agents/{id}/mint` (account token) — the
  re-mint, via `AccountClient.mint_agent_token()`; the response's `agent_id` must
  match the target or `_make_remint` aborts with a `ClickException`.

## Edge cases & guardrails (preserve these)

- **`cinna-*`-only scoping** — `_collect_cinna_sessions()` and the orphan pass both
  filter on the `cinna-` prefix so a foreign Mutagen consumer's sessions are never
  reported or terminated. (`tests/test_doctor.py:test_sweep_ignores_non_cinna_sessions`)
- **Daemon-down tolerance** — `list_all_sessions()` exceptions are caught in both
  `diagnose()` and `_collect_cinna_sessions()` and degrade to an empty list, so a
  registry-only repair still runs.
- **Single account-token probe** — the account token is probed at most once per
  account root; expired-account sub-agents are grouped into one
  `account_token_expired` finding rather than N doomed re-mints.
  (`tests/test_doctor.py:test_expired_account_token_groups_subagents`)
- **Re-mint identity guard** — `_make_remint` raises if the minted token's
  `agent_id` differs from the target, never clobbering the wrong workspace.
- **One session finding per entry** — an intact entry emits at most one of
  zombie/dead-remote (the `if/elif` chain), and `claimed_sessions` prevents the
  same session reappearing as an orphan.
- **Stale-folder owns its session** — a missing-folder entry with a lingering
  session is the single `stale_folder` finding (registry removal + terminate),
  not a separate orphan.
  (`tests/test_doctor.py:test_stale_folder_terminates_its_session`)
- **Dry-run is total** — `run_doctor` returns immediately after the report when
  `dry_run`, so no prompt is shown and nothing is terminated/removed.
  (`tests/test_doctor.py:test_doctor_dry_run_makes_no_changes`)
- **Fail-soft apply** — `_apply_step` catches per-finding exceptions, prints the
  error, and continues; the step-2 active sweep likewise reports a failed
  terminate and moves on.
