# Bootstrap & Onboarding — Technical Reference

Implementation of [bootstrap_onboarding.md](bootstrap_onboarding.md). cinna-cli is
a Python CLI; all logic lives in `src/cinna/`, tests in `tests/`.

## File locations

- `src/cinna/bootstrap.py` — the setup/refresh core: token-exchange helper, layout
  decision, workspace provisioning, auto-link, and the disconnect teardown helper.
- `src/cinna/config.py` — `CinnaConfig` / `GitLayout` dataclasses, `.cinna/config.json`
  load/save, the per-user `~/.cinna/agents.json` registry, and the Model-A layout
  helpers.
- `src/cinna/context.py` — generates the in-workspace guides (`CLAUDE.md`,
  `BUILDING_AGENT.md`, `CHAT_TESTING.md`, `GIT_VERSIONING.md`), `.mcp.json`,
  `opencode.json`, `.gitignore`, and lists synced prompt-ref docs for teardown.
- `src/cinna/account.py` — `cinna login` (device-auth) and the account-status
  fallback `cinna status` uses.
- `src/cinna/sync_session.py` — `start()` / `stop()` / `status()` / `run_foreground()`
  and `write_mutagen_yml()` driven by setup / dev / disconnect.
- `src/cinna/main.py` — the Click command surface for every verb below, plus the
  root `CinnaGroup` that maps every failure onto the exit-code contract and the
  `no_input_option()` / `json_option()` decorators.
- `src/cinna/errors.py` — `CinnaExit` (exit code + machine `code` + detail; JSON
  rendering in `show()`), the `EXIT_*` constants and the typed subclasses
  (`NeedsInputError`, `NetworkError`, `SetupTokenError`, `AccountMismatchError`,
  `WorkspaceExistsError`, `MutagenNotFoundError`, `MutagenVersionMismatchError`,
  `PlatformError`, `AuthenticationError`, the two not-a-workspace errors).
- `src/cinna/console.py` — output + interaction switches: `json_mode`,
  `no_input`, `set_json_mode()`, `set_no_input()`, `interactive()`, `prompt()`,
  `confirm()`, `emit_json()`, `emit_result()`; `step` / `status` / `warn` /
  `error` render JSON lines in JSON mode, `spinner()` / `file_progress()` go
  quiet.
- `src/cinna/mutagen_runtime.py` — `mutagen_binary()` (`CINNA_MUTAGEN_BIN`
  override), `detect_local_mutagen()`, `ensure_mutagen_ready()` gated by
  `console.interactive()`.
- `src/cinna/cli_version.py` — installed-vs-pinned cinna-cli version from the
  platform discovery document.
- Tests: `tests/test_bootstrap.py` (setup-input parsing, slug normalization),
  `tests/test_config.py` (config round-trip, the registry upsert/preserve/clear
  semantics, `0600` perms, corrupt-file recovery, layout helpers),
  `tests/test_context.py` (guide / MCP / gitignore generation, synced prompt-ref
  discovery for teardown), `tests/test_onboarding.py` (exit-code mapping,
  `--no-input` on every prompt site, `--json`, `CINNA_MUTAGEN_BIN`, version pin
  in status / doctor), `tests/test_cli_version.py` (pin lookup + comparison),
  `tests/test_mutagen_runtime.py`.

## Command surface

Each verb → its handler in `src/cinna/main.py`:

- `cinna setup` → `src/cinna/main.py:setup()` → `src/cinna/bootstrap.py:run_setup()`
- `cinna set-token` → `src/cinna/main.py:set_token()` → `src/cinna/bootstrap.py:run_set_token()`
- `cinna login` → `src/cinna/main.py:login()` → `src/cinna/account.py:run_login()`
- `cinna list` → `src/cinna/main.py:list_cmd()`
- `cinna status` → `src/cinna/main.py:status()`
- `cinna dev` → `src/cinna/main.py:dev()` → `src/cinna/main.py:_run_dev_session(favor_remote=False)`
- `cinna redev` → `src/cinna/main.py:redev()` → `src/cinna/main.py:_run_dev_session(favor_remote=True)`
- `cinna disconnect` → `src/cinna/main.py:disconnect()`
- `cinna disconnect-all` → `src/cinna/main.py:disconnect_all()`
- `cinna completion` → `src/cinna/main.py:completion()`

`setup` / `set-token` / `account setup` default the machine name through
`src/cinna/main.py:_resolve_machine_name()`: `--name` wins; otherwise the
`_default_machine_name()` is prompted only when `console.interactive()` (a TTY
on stdin **and** no `--no-input`), else taken as is.

Root-level: `cinna --no-input …` (`envvar=CINNA_NO_INPUT`) flips
`src/cinna/console.py:set_no_input()`; `cinna --version` reports the installed
package version.

## Key functions & flow

### Token exchange (shared by setup + set-token)
- `src/cinna/bootstrap.py:parse_setup_input()` — accepts a full `curl … | python3 -`,
  a bare URL, or a raw token; extracts `(platform_url, token)`. A raw token falls
  back to the passed `fallback_platform_url` then `CINNA_PLATFORM_URL`. Covered by
  `tests/test_bootstrap.py`.
- `src/cinna/bootstrap.py:_exchange_setup_token()` — `POST {platform_url}/cli-setup/{token}`
  with `{machine_name, machine_info}`; raises a uniform `ClickException` on non-200
  (exit 1, code `error` via the root mapping). The **account** exchange in
  `src/cinna/account.py:_exchange_account_setup_token()` is the one with the
  10 / 12 mapping — see the account tech doc.
- `src/cinna/bootstrap.py:config_from_payload()` — builds an in-memory `CinnaConfig`
  from the exchange payload (`cli_token`, nested `agent`, `platform_url`, optional
  `frontend_url` / `cli_token_id` / `knowledge_sources`). No IO.

### Setup
- `src/cinna/bootstrap.py:run_setup()` — the 5-step flow: exchange → `prepare_git_layout()`
  → refuse if the target/clone-root already holds a config → `persist_config()` →
  `provision_workspace()` → `_maybe_autolink()` → `sync_session.start()` +
  `run_foreground()`.
- `src/cinna/bootstrap.py:prepare_git_layout()` — best-effort `git_versioning.fetch_coordinates()`,
  then `config.compute_agent_layout()` to pick `(clone_root, workspace_root, subdir)`;
  records a `GitLayout(vcs_enabled=False)` on the config.
- `src/cinna/bootstrap.py:resolve_clone_slug()` — bumps the clone-root name to
  `<slug>-<shorthash>` when `<slug>/` already holds a *different* agent, via
  `workspace_agent_id_at()` + `short_agent_hash()`.
- `src/cinna/bootstrap.py:provision_workspace()` — the three shared steps
  (`ensure_mutagen_ready` → `download_workspace` + `extract_workspace_tarball` →
  `generate_context_files` / `generate_mcp_json` / `generate_opencode_json` /
  `generate_gitignore` / `write_mutagen_yml`). Reused by `cinna agent sync`.
- `src/cinna/bootstrap.py:_maybe_autolink()` — runs `git_versioning.link()` when the
  agent is git-versioned; link failures degrade to a warning (Mutagen-only still works).
- `src/cinna/bootstrap.py:persist_config()` — `save_config()` + `upsert_agent_registry()`,
  carrying the git block into the registry when present.

### Exit codes and modes (every command)
- `src/cinna/main.py:CinnaGroup.invoke()` — wraps the whole command run:
  `CinnaExit` passes through; a plain `click.ClickException` becomes
  `CinnaExit.from_click()` (exit 1, code `error` unless the exception carries
  one); `httpx.TransportError` → `NetworkError` (12); `click.Abort` and any
  other exception become `aborted` / `internal_error` **only in JSON mode**
  (human mode keeps Click's "Aborted!" and the traceback); `click.UsageError`
  keeps Click's rendering and exit 2 for humans.
- `src/cinna/errors.py:CinnaExit.show()` — in JSON mode prints
  `{"result": "error", "code", "detail", …extra}` on stdout; otherwise Click's
  `Error: …` on stderr. Click's own `main()` then exits with `exit_code`.
- `src/cinna/console.py:prompt()` / `confirm()` — the only prompt primitives
  used in `src/cinna/` (`main.py`, `account.py`, `doctor.py`,
  `mutagen_runtime.py`, `local_import.py`, `chat.py`): under `no_input` they
  return the default or raise `NeedsInputError`; `confirm(abort=True)` aborts.
- `src/cinna/console.py:set_json_mode()` — swaps the Rich console for a quiet
  one (so tables / hints vanish), resets the step tracker, and turns
  `no_input` on. `step()` records `(n, total)` so subsequent `status` / `warn`
  / `error` lines carry them.

### Mutagen binary
- `src/cinna/mutagen_runtime.py:mutagen_binary()` — `CINNA_MUTAGEN_BIN` or
  `mutagen`; used by `src/cinna/sync_session.py:_run_mutagen()` and the three
  `create_subprocess_exec` sites in `src/cinna/sync_tui.py`.
- `src/cinna/mutagen_runtime.py:detect_local_mutagen()` — with the override set,
  only that path is considered (non-executable → `None`); otherwise
  `shutil.which("mutagen")`.
- `src/cinna/mutagen_runtime.py:ensure_mutagen_ready()` — `interactive` is
  ANDed with `console.interactive()`; non-interactive missing → 
  `MutagenNotFoundError` (`mutagen_missing`, `required_version` in the JSON
  extra), non-interactive minor mismatch → `MutagenVersionMismatchError`
  (`mutagen_mismatch`, `installed_version` / `required_version`).

### cinna-cli version pin
- `src/cinna/cli_version.py:cli_version_status()` — `GET
  {origin}/.well-known/cinna-desktop` → `local_dev.cinna_cli_version`
  (`required_cli_version_from()` also accepts a top-level `cinna_cli_version`,
  the shape a future `sync-runtime` could carry) → `compare_cli_version()`.
- `src/cinna/doctor.py:_cli_version_findings()` — one report-only
  `cli_outdated` finding per platform (registry entries + the current account
  workspace) whose pin differs; nothing when no pin is published.

### Set-token (refresh)
- `src/cinna/bootstrap.py:run_set_token()` — `find_workspace_root()` → `load_config()`
  → exchange (config's `platform_url` as the bare-token fallback, with `/api`
  appended) → **abort if `payload["agent"]["id"] != config.agent_id`** → overwrite
  `cli_token` / `platform_url` / `frontend_url` → `save_config()` + `upsert_agent_registry()`.
  No tarball, no guide regeneration.

### List / status
- `src/cinna/main.py:list_cmd()` — `list_agent_registry()` rows; one cheap
  `sync_session._list_sessions()` pass indexes Mutagen sessions by name; enriches
  each row with the agent display name from `load_config(workspace_path)` (flags
  **missing** when the path is gone).
- `src/cinna/main.py:_probe_token_statuses()` — parallel `GET /api/v1/cli/agents/{id}/sync-runtime`
  per row in a `ThreadPoolExecutor`; classifies 2xx→`valid`, 401→`expired`,
  else→`unreachable`. `_format_token_label()` / `_format_sync_cell()` render it.
- `src/cinna/main.py:status()` — in a per-agent workspace: `sync_session.status()` +
  a single token probe in a Rich table. On `ConfigNotFoundError` it tries
  `account.find_account_root()` and defers to `account.run_account_status()`.

### Dev / redev
- `src/cinna/main.py:_run_dev_session()` — `find_workspace_root()` → `load_config()`
  → `ensure_mutagen_ready()` → `sync_session.start()`; with `favor_remote=True` it
  then calls `sync_session.resolve_startup_conflicts_favor_remote()` (backing up
  displaced local files) before attaching the TUI.

### Teardown
- `src/cinna/main.py:disconnect()` — confirm → `sync_session.stop()` →
  `remove_agent_registry()` → `bootstrap.remove_workspace_artifacts()`.
- `src/cinna/bootstrap.py:remove_workspace_artifacts()` — deletes `.cinna/` and every
  entry in `GENERATED_WORKSPACE_FILES` plus the dynamically discovered
  `context.list_synced_prompt_refs()`; never touches user `workspace/` files,
  the sync session, or the registry (the caller owns those).
- `src/cinna/main.py:disconnect_all()` — walks cwd children (direct or one level
  down for the nested layout), `sync_session.stop()` + `remove_agent_registry()`
  per agent, then `shutil.rmtree()` the top-level dir.

### Completion
- `src/cinna/main.py:completion()` — runs `cinna` with `_CINNA_COMPLETE=<shell>_source`
  to emit the script; `--install` appends an idempotent snippet via
  `_install_target()` to the shell rc. `_detect_shell()` reads `$SHELL`.

## Config & registry

`.cinna/config.json` (`src/cinna/config.py:CinnaConfig`) holds `platform_url`,
`cli_token`, `agent_id`, `agent_name`, `environment_id`, `template`, optional
`frontend_url` / `cli_token_id` / `knowledge_sources`, the Mutagen pin
(`mutagen_version`, `last_sync_runtime_check_at`, `last_sync_connected_at`), and the
`GitLayout` (`git`). `load_config()` tolerates unknown/legacy keys (e.g.
`container_name`).

The per-user registry `~/.cinna/agents.json` <!-- nocheck --> maps
`agent_id → {platform_url, cli_token, workspace_path, frontend_url?, git?}`, written
`0600` (`src/cinna/config.py:_write_registry()`), via atomic temp-file replace and
guarded by a thread lock. `src/cinna/config.py:upsert_agent_registry()` uses a
`_PRESERVE_GIT` sentinel: an omitted `git` keeps any stored block (so a sync/dev
re-upsert of credentials never strips a linked agent's git coordinates), a dict
sets it, and `None` clears it. `remove_agent_registry()` drops a row;
`list_agent_registry()` / `lookup_agent_registry()` read it. A corrupt registry
file is treated as empty rather than crashing (`_read_registry()`).
`workspace_path` always stays the agent dir so `list` / `doctor` resolve configs
unchanged.

Layout helpers: `compute_agent_layout(parent, slug, subdir)` →
`(clone_root, workspace_root, subdir)` (`<parent>/<slug>/<subdir>/`, subdir defaults
to slug); `clone_root()` / `git_subdir()` resolve the working tree for an agent.

## External contracts

- **`POST /api/cli-setup/{token}`** (setup-token auth) — the only onboarding write;
  returns `cli_token` + nested `agent` + `platform_url` / `frontend_url`. Consumed
  by `_exchange_setup_token()`.
- **`GET /api/v1/cli/agents/{id}/workspace`** (CLI JWT) — one-shot tarball for the
  initial clone (`client.download_workspace` → `sync.extract_workspace_tarball`,
  which validates against path traversal / absolute paths / symlinks / oversize).
- **`GET /api/v1/cli/agents/{id}/building-context`** (CLI JWT) — assembled building
  prompt + inline `prompt_files`, written by `context.generate_context_files`.
- **`GET /api/v1/cli/agents/{id}/sync-runtime`** (CLI JWT) — the pinned Mutagen
  version (used by `ensure_mutagen_ready`) **and** the cheap token-validity probe
  for `list` / `status`.
- **`POST /api/v1/cli/account/login/start` + `/poll`** — the `cinna login` device-auth
  flow; see [Account Workspace](../account_workspace/account_workspace.md).
- **Mutagen** — `sync_session.start()` / `run_foreground()` / `stop()` wrap the
  `mutagen` CLI (binary from `mutagen_runtime.mutagen_binary()`, i.e.
  `CINNA_MUTAGEN_BIN` or PATH); the SSH shim reads `~/.cinna/agents.json` <!-- nocheck --> per
  invocation to resolve per-agent credentials.
- **`GET /.well-known/cinna-desktop`** (no auth) — `local_dev.cinna_cli_version`,
  the cinna-cli pin; optional, absence is `unknown`.
- **Environment** — `CINNA_NO_INPUT=1` (same as `--no-input`),
  `CINNA_MUTAGEN_BIN=<abs path>`, `CINNA_PLATFORM_URL` (bare-token fallback).

See the "Bootstrap Flow", "Setup Token", "CLI Token", "Security Model", and
"Platform API Endpoints" sections of `docs/README.md` for the canonical endpoint
table and the token lifecycle.

## Edge cases & guardrails (preserve these)

- **Same-agent guard on refresh** — `run_set_token()` aborts when the exchanged
  token's agent id differs from the workspace's, never silently rebinding.
- **No-clobber on setup** — both the nested `workspace_root` and the `clone_root`
  are checked for an existing `.cinna/config.json` before any write (`run_setup()`).
- **Slug-collision suffix** — `resolve_clone_slug()` only suffixes when the slug is
  taken by a *different* agent; the same agent re-uses the slug so the existence
  check reports "already set up".
- **Registry preserve sentinel** — any credential re-upsert without a `git` arg must
  keep a linked agent's git block (`tests/test_config.py`).
- **Registry holds JWTs** — `agents.json` must stay `0600`; verified by
  `tests/test_config.py`.
- **Teardown asymmetry** — `disconnect` keeps `workspace/`; `disconnect-all` deletes
  whole directories. `remove_workspace_artifacts()` must discover synced prompt-ref
  guides dynamically (`context.list_synced_prompt_refs`) so a newer guide isn't
  orphaned at teardown.
- **Best-effort discovery during setup** — `prepare_git_layout()` swallows a failed
  coordinates fetch (older backend / network) and falls back to the slug subdir;
  setup must still succeed Mutagen-only.
- **Completion install is idempotent** — re-running `--install` detects the existing
  `cinna completion` block and skips (`completion()`).
- **Every prompt goes through `console.prompt` / `console.confirm`** — a new
  `click.prompt` / `click.confirm` call site would reintroduce a hang under
  `--no-input`; `tests/test_onboarding.py` covers the existing sites
  (`test_console_prompt_takes_default_or_fails`,
  `test_no_input_confirm_defaults_to_abort`,
  `test_mutagen_missing_under_no_input_is_structured`).
- **Exit codes stay stable** — 10 / 11 / 12 are reserved for the setup-token /
  account-mismatch / network classes; new failure kinds get a new `code` under
  exit 1, never a new number, unless the desktop plan changes too
  (`tests/test_onboarding.py`, the "exit codes" block).
- **Nothing but JSON on stdout in JSON mode** — new output must go through
  `console.*` (or `click.echo(…, err=True)`); `test_setup_json_progress_and_result`
  parses every line.
- **Mode switches are process-global** — tests reset them via the autouse
  fixture in `tests/conftest.py:reset_console_modes`.
