# Account Workspace — Technical Reference

Implementation of [account_workspace.md](account_workspace.md). cinna-cli is a
Python CLI; the account-workspace logic lives in `src/cinna/account.py`, the
command group in `src/cinna/main.py`, the HTTP layer in `src/cinna/client.py`,
and tests in `tests/test_account.py`.

## File locations

- `src/cinna/account.py` — the feature core: `AccountConfig` dataclass + config
  I/O, setup-input parsing, child-workspace discovery, the account-token probe,
  the `cinna login` device-authorization flow, and every `run_*` command body
  (`account`, `agent`, `user-workspace`, `credentials`, plus the Phase-3
  connect/agent-api/schedules/status/api verbs).
- `src/cinna/client.py` — `AccountClient`, the HTTP client for the
  `/api/v1/cli/account/*` route group (account-token auth).
- `src/cinna/main.py` — the `cinna account` / `cinna agent` / `cinna login`
  command groups (thin Click wrappers that import and call the `run_*` bodies).
- `src/cinna/bootstrap.py` — the shared per-agent provisioning writer reused by
  `cinna agent sync` (`config_from_payload`, `prepare_git_layout`,
  `provision_workspace`, `persist_config`, `_maybe_autolink`,
  `resolve_clone_slug`, `workspace_agent_id_at`, `remove_workspace_artifacts`).
- `src/cinna/config.py` — `CONFIG_DIR`, per-agent `load_config` /
  `CinnaConfig`, and the `~/.cinna/agents.json` registry helpers
  (`upsert_agent_registry`, `remove_agent_registry`).
- `src/cinna/context.py` — `regenerate_claude_md()` (per-child guide) and
  `write_chat_testing_guide()` (companion testing guide), used by
  `refresh-context`.
- `src/cinna/mcp_proxy.py` — account-mode knowledge proxy
  (`run_mcp_proxy` / `_resolve_proxy_context` / `create_account_mcp_server`)
  wired by the account `.mcp.json`.
- `src/cinna/errors.py` — `CinnaExit` (stable exit code + machine `code`) and
  its subclasses used here: `SetupTokenError` (10), `AccountMismatchError` (11),
  `NetworkError` / `PlatformError` 5xx (12), `WorkspaceExistsError`,
  `AccountConfigNotFoundError`.
- `src/cinna/console.py` — the `json_mode` / `no_input` switches, the JSON line
  writer (`emit_json` / `emit_result`) and the `prompt` / `confirm` /
  `interactive` wrappers every prompt site goes through.
- `src/cinna/cli_version.py` — installed-vs-pinned cinna-cli version
  (`cli_version_status`, `fetch_required_cli_version`).
- `src/cinna/templates/ACCOUNT_CLAUDE.md.template` — the orchestrator
  `CLAUDE.md` source.
- Tests: `tests/test_account.py` (setup, refresh-context, agents listing +
  workspace scoping, status, agent sync/unsync, exec `--agent`, child-workspace
  resolution incl. multi-segment subdir, user-workspace, credentials,
  `AccountClient` HTTP-level), `tests/test_client.py` (account client), the
  MCP-proxy account-mode tests in `tests/test_account.py`, and
  `tests/test_onboarding.py` (the driver contract: exit codes, absolute `--dir`,
  `account set-token`, `--no-input`, `--json` line snapshots, version pin in
  status / doctor).

## Command surface

Each verb → its handler in `src/cinna/main.py` → the body in
`src/cinna/account.py`:

- `cinna account setup` → `src/cinna/main.py:account_setup()` →
  `src/cinna/account.py:run_account_setup()`
- `cinna account set-token` → `src/cinna/main.py:account_set_token()` →
  `src/cinna/account.py:run_account_set_token()`
- `cinna account agents` → `src/cinna/main.py:account_agents()` →
  `src/cinna/account.py:run_account_agents()`
- `cinna account status` → `src/cinna/main.py:account_status()` →
  `src/cinna/account.py:run_account_status()`
- `cinna account refresh-context` → `src/cinna/main.py:account_refresh_context()`
  → `src/cinna/account.py:run_account_refresh_context()`
- `cinna account user-workspace list` →
  `src/cinna/main.py:account_user_workspace_list()` →
  `src/cinna/account.py:run_user_workspace_list()`
- `cinna account user-workspace activate` →
  `src/cinna/main.py:account_user_workspace_activate()` →
  `src/cinna/account.py:run_user_workspace_activate()`
- `cinna account user-workspace clear` →
  `src/cinna/main.py:account_user_workspace_clear()` →
  `src/cinna/account.py:run_user_workspace_clear()`
- `cinna account credentials list` →
  `src/cinna/main.py:account_credentials_list()` →
  `src/cinna/account.py:run_credentials_list()`
- `cinna account credentials types` →
  `src/cinna/main.py:account_credentials_types()` →
  `src/cinna/account.py:run_credentials_types()`
- `cinna account credentials create` →
  `src/cinna/main.py:account_credentials_create()` →
  `src/cinna/account.py:run_credentials_create()`
- `cinna account credentials update` →
  `src/cinna/main.py:account_credentials_update()` →
  `src/cinna/account.py:run_credentials_update()`
- `cinna account credentials delete` →
  `src/cinna/main.py:account_credentials_delete()` →
  `src/cinna/account.py:run_credentials_delete()`
- `cinna account credentials share-with-agent` →
  `src/cinna/main.py:account_credentials_share_with_agent()` →
  `src/cinna/account.py:run_credentials_share()`

`setup`, `set-token` and `status` also take `--no-input` and `--json`
(`src/cinna/main.py:no_input_option()` / `json_option()` — eager, value-less
options whose callbacks flip `src/cinna/console.py:set_no_input()` /
`set_json_mode()`); the root group takes `--no-input` too (also
`CINNA_NO_INPUT=1`). The per-command copy exists because
`ignore_unknown_options` + the `nargs=-1` setup argument would otherwise
swallow a flag placed after the subcommand, which is how the desktop invokes it.

Related (documented here as integration points): `cinna agent sync` →
`src/cinna/account.py:run_agent_sync()`, `cinna agent unsync` →
`run_agent_unsync()`, `cinna login` → `run_login()`.

## Key functions & flow

- `src/cinna/account.py:AccountConfig` — dataclass persisted to
  `.cinna/account.json`: `platform_url`, `frontend_url`, `account_token`,
  `machine_name`, and the client-side `user_workspace_id` / `user_workspace_name`.
- `src/cinna/account.py:find_account_root()` — walks up from cwd for
  `.cinna/account.json` (mirrors the per-agent `find_workspace_root`); raises
  `AccountConfigNotFoundError`.
- `src/cinna/account.py:load_account_config()` / `save_account_config()` —
  tolerant load (drops unknown keys) and 0600 write (the file holds the token).
- `src/cinna/account.py:parse_account_setup_input()` — accepts curl/URL/raw-token
  forms; **requires `/cli-setup/account/`** so a per-agent URL is rejected, and a
  bare token falls back to `CINNA_PLATFORM_URL`.
- `src/cinna/account.py:default_account_dir_name()` — derives the default folder
  from the platform host (collapses non-`[A-Za-z0-9-]` to `_`).
- `src/cinna/account.py:resolve_account_dir()` — the `--dir` contract: absolute
  (after `~` expansion) → as is, relative → under cwd. No symlink resolution, so
  the path reported back equals the one passed.
- `src/cinna/account.py:run_account_setup()` — parse → derive/prompt the dir
  (`_prompt_account_dir()` returns the default outright under `no_input`) →
  `resolve_account_dir()` → **guard the target before**
  `_exchange_account_setup_token()` (`WorkspaceExistsError`) → build
  `AccountConfig` → `_write_account_files()` (creates parents) → best-effort
  `_install_context_package()` → `console.emit_result()` with
  `_account_result_fields()`.
- `src/cinna/account.py:_exchange_account_setup_token()` — the exchange with the
  exit-code mapping: transport error → `NetworkError` (12), 5xx →
  `PlatformError` (12), any other non-200 → `SetupTokenError` (10, backend
  detail verbatim, `http_status` in the JSON error line).
- `src/cinna/account.py:run_account_set_token()` — `find_account_root()` →
  `parse_account_setup_input(…, fallback_platform_url=<stored>/api)` →
  exchange under the **stored** `machine_name` → `_same_origin()` check on
  `platform_url` and `_jwt_claims()` `sub` comparison (both raise
  `AccountMismatchError`, 11, before any write) → rewrite `account_token` +
  refreshed `platform_url` / `frontend_url` → `save_account_config()`.
  `user_workspace_*`, `machine_name`, `context/`, children untouched.
- `src/cinna/account.py:_jwt_claims()` — unverified base64 decode of a JWT
  payload, only to compare `sub`; opaque tokens yield `None` (no comparison).
- `src/cinna/account.py:_account_result_fields()` — the shared `--json` final
  line of setup / set-token (`workspace`, `platform_url`, `frontend_url`,
  `machine_name`, `context_package: ok|failed|skipped`).
- `src/cinna/account.py:_write_account_files()` — mkdir + `save_account_config` +
  `agents/` + `_write_account_claude_md` + `_write_account_claude_settings` +
  `_write_account_mcp_config`.
- `src/cinna/account.py:_install_context_package()` — downloads via
  `AccountClient.download_context_package()`, extracts with the workspace clone's
  safe extractor (`src/cinna/sync.py:extract_workspace_tarball`), `replace=True`
  removes `context/` only **after** a successful download; never raises.
- `src/cinna/account.py:run_account_refresh_context()` — refresh `context/`,
  regenerate orchestrator `CLAUDE.md`, self-heal `.claude/settings.json` + MCP
  wiring, and re-render every child's `CLAUDE.md` via
  `src/cinna/context.py:regenerate_claude_md()`.
- `src/cinna/account.py:run_account_agents()` — fetch the full listing, then
  **client-side** scope to `user_workspace_id` (unless `--all`), and annotate each
  row with the local checkout from `list_child_workspaces()`.
- `src/cinna/account.py:run_account_status()` — `probe_account_token()` +
  `context_package_status()` + `cli_version_status()`; in JSON mode emits the
  single `result` line (`token`, `active_workspace`, `synced_agents`, `agents[]`,
  `context_package{local,remote,state}`, `cli{installed,required,state}`) and
  returns; otherwise the Rich table + `_synced_agents_table()` +
  `_print_token_reauth_hint()` (which now names `cinna account set-token` next
  to `cinna login`).
- `src/cinna/cli_version.py:cli_version_status()` — `GET
  {origin}/.well-known/cinna-desktop` → `local_dev.cinna_cli_version`, compared
  with the running `__version__` (`current` / `behind` / `ahead` / `unknown`).
- `src/cinna/account.py:probe_account_token()` — cheap `GET /account/agents`;
  2xx → valid, 401 → expired, else → unreachable.
- `src/cinna/account.py:run_agent_sync()` — resolve via `_resolve_account_agent`,
  compute the clone slug with `resolve_clone_slug`, refuse only when **this**
  agent is already synced there, `mint_agent_token`, then the standard bootstrap
  path (`config_from_payload` → `prepare_git_layout` → `persist_config` →
  `provision_workspace` → `_maybe_autolink`).
- `src/cinna/account.py:run_agent_unsync()` — `sync_session.stop` → best-effort
  `AccountClient.revoke_child_token` → `remove_agent_registry` +
  `remove_workspace_artifacts` (keeps user files).
- `src/cinna/account.py:run_login()` / `_device_login()` /
  `_poll_until_authorized()` — RFC 8628 device flow; resume refreshes the token in
  place, fresh-folder bootstraps a new workspace.

### Child-workspace resolution

- `src/cinna/account.py:_find_agent_dirs_under()` — walks a clone root to
  `_AGENT_SCAN_MAX_DEPTH` (8), pruning `_AGENT_SCAN_PRUNE`
  (`workspace`, `.git`, `.cinna`, `node_modules`, `.venv`, `__pycache__`), and
  stops at the first dir holding `.cinna/config.json` (handles a multi-segment
  backend subdir).
- `src/cinna/account.py:_iter_agent_dirs()` / `list_child_workspaces()` — iterate
  `agents/` and load each child `CinnaConfig`.
- `src/cinna/account.py:resolve_child_workspace()` — match `agent_ref` by agent
  id, slug, or display name (used by `cinna exec --agent`, `cinna agent unsync`).
- `src/cinna/account.py:_resolve_account_agent()` — resolve `agent_ref` against
  the `/account/agents` listing (id / name / slug), raising a listing on
  no-match or ambiguity.

### Active user workspace & credentials

- `src/cinna/account.py:_resolve_account_workspace()` +
  `run_user_workspace_activate()` / `run_user_workspace_clear()` — persist
  `user_workspace_id`/`name` (or clear via `_CLEAR_WORKSPACE_REFS`:
  `default`/`none`/`clear`/empty).
- `src/cinna/account.py:run_credentials_create()` — defaults the target to
  `account_cfg.user_workspace_id`, sends **no secret**, prints `required_fields`
  + `setup_url`, optionally attaches via `share_credential_with_agent`.
- `src/cinna/account.py:run_credentials_update()` — refuses an empty update;
  metadata fields only (`name`/`notes`/`service_uri`/`allow_sharing`).
- `src/cinna/account.py:_credential_status_cell()` — complete / needs setup / —.

## Config & registry

- `.cinna/account.json` (account root) — `AccountConfig` as JSON, 0600. Holds the
  account token and the client-side active-workspace selection.
- `.cinna/config.json` (each child under `agents/<slug>/…`) — the standard
  per-agent `CinnaConfig` (per-agent CLI token, `cli_token_id` for revoke).
- `~/.cinna/agents.json` (per-user registry, runtime state) — `cinna agent sync`
  upserts a normal entry via `src/cinna/config.py:upsert_agent_registry()` so the
  child is indistinguishable from a `cinna setup` agent to `cinna list` / doctor /
  the sync shim; `cinna agent unsync` removes it with `remove_agent_registry()`.
- Account root generated files: `CLAUDE.md`, `.claude/settings.json`
  (`Bash(cinna:*)` + `mcp__platform-knowledge`, `enableAllProjectMcpServers`),
  `.mcp.json` / `opencode.json` (proxy in account mode via
  `CINNA_ACCOUNT_CONFIG=.cinna/account.json`, written **relative**), `context/`,
  `CHAT_TESTING.md`.

## External contracts

All consumed by `src/cinna/client.py:AccountClient` with the account token
(`Authorization: Bearer <account_token>`), base URL = `platform_url`:

- `POST /cli-setup/account/{token}` — exchange the account setup token
  (`src/cinna/account.py:_exchange_account_setup_token()`, plain `httpx.post`, not
  the client). Body: `{machine_name, machine_info}`. Returns `account_token`,
  `platform_url`, `frontend_url`, `machine_name`. Used by both `account setup`
  and `account set-token`; the CLI relies on the response being identical for a
  first and a repeat exchange (no `user`/owner field is required — the
  same-account check works from the origin and the token's own `sub`).
- `GET /.well-known/cinna-desktop` (unauthenticated, platform origin) — the
  desktop discovery document; `local_dev.cinna_cli_version` is the cinna-cli
  pin (`src/cinna/cli_version.py:fetch_required_cli_version()`). Absent block,
  404 or no network → `unknown`, silently.
- `GET /api/v1/cli/account/agents` — accessible agents (`list_account_agents`;
  also the token probe).
- `POST /api/v1/cli/account/agents/{id}/mint` — mint a per-agent child token
  (`mint_agent_token`).
- `POST /api/v1/cli/account/agents` — create an agent (`create_agent`).
- `GET /api/v1/cli/account/context-package` — the orchestrator context tarball
  (`download_context_package`).
- `DELETE /api/v1/cli/account/tokens/children/{id}` — revoke a minted child token
  (`revoke_child_token`; 404 tolerated on unsync).
- `GET /api/v1/cli/account/user-workspaces` — the user's workspaces
  (`list_user_workspaces`).
- `GET /api/v1/cli/account/credentials` — metadata-only listing (`list_credentials`,
  optional `user_workspace_id` filter).
- `GET /api/v1/cli/account/credentials/types` — type + required-field map
  (`list_credential_types`).
- `POST /api/v1/cli/account/credentials` — create a draft (`create_credential`,
  no secret value).
- `PUT /api/v1/cli/account/credentials/{id}` — metadata update (`update_credential`).
- `DELETE /api/v1/cli/account/credentials/{id}` — tier-gated delete
  (`delete_credential`, `force` overrides the Tier-2 409 block).
- `POST /api/v1/cli/account/credentials/{id}/share-with-agent` — attach
  (`share_credential_with_agent`).
- `POST /api/v1/cli/account/login/start` + `/login/poll` — device-auth (both
  unauthenticated, since the point is the old token is dead).
- `POST /api/v1/cli/account/knowledge/search` — account-mode `knowledge_query`
  (`search_knowledge`), used by the account MCP proxy.

`AccountClient._handle_response()` surfaces backend `detail` verbatim: 401 →
`AuthenticationError`, other 4xx/5xx → `PlatformError`.

## Edge cases & guardrails (preserve these)

- **Guard before burning the setup token** — `run_account_setup` checks the
  target dir exists-check *before* calling `_exchange_account_setup_token`, so a
  doomed run never spends the single-use token.
  (`tests/test_account.py:test_account_setup_refuses_existing_workspace`,
  `tests/test_onboarding.py:test_setup_existing_absolute_dir_is_workspace_exists`)
- **Absolute `--dir` is used as is, parents created** — `resolve_account_dir()`;
  the `workspace` reported in JSON is the path the caller passed.
  (`tests/test_onboarding.py:test_setup_absolute_dir_used_as_is_and_parents_created`)
- **`set-token` never rebinds** — the origin / `sub` checks precede the write;
  on mismatch `account.json` is byte-identical afterwards.
  (`tests/test_onboarding.py:test_account_set_token_platform_mismatch_exits_11`,
  `test_account_set_token_subject_mismatch_exits_11`)
- **`set-token` preserves everything but the token** — active user workspace,
  machine name and child configs survive.
  (`tests/test_onboarding.py:test_account_set_token_swaps_token_in_place`)
- **Exit codes are mapped centrally** — `src/cinna/main.py:CinnaGroup.invoke()`
  wraps plain `ClickException`s, `httpx.TransportError`s and (in JSON mode)
  unexpected exceptions into `CinnaExit`; `CinnaExit.show()` prints the JSON
  error line instead of `Error: …` when `json_mode` is on.
  (`tests/test_onboarding.py`, the "exit codes" block)
- **`--json` stdout is JSON only** — `set_json_mode()` swaps the Rich console
  for a quiet one; `spinner()` is a no-op; every stdout line must parse.
  (`tests/test_onboarding.py:test_setup_json_progress_and_result`,
  `test_status_json`)
- **`--no-input` never blocks** — `_prompt_account_dir()` short-circuits, the
  machine-name prompt takes its default, `console.confirm()` returns the
  default. (`tests/test_onboarding.py:test_no_input_after_subcommand_skips_dir_prompt`,
  `test_group_level_no_input_fails_needs_input`)
- **Context refresh is non-destructive** — `_install_context_package(replace=True)`
  removes `context/` only after a successful download; the orchestrator/child
  `CLAUDE.md` regeneration is independent of the download.
  (`tests/test_account.py:test_refresh_context_failure_preserves_old_tree`)
- **Settings is create-if-absent** — `_write_account_claude_settings` never
  clobbers a user-edited `.claude/settings.json`.
  (`tests/test_account.py:test_refresh_context_preserves_user_claude_settings`)
- **Per-agent URL is rejected** — `parse_account_setup_input` must not silently
  accept a `/cli-setup/<token>` (non-account) URL.
  (`tests/test_account.py:test_parse_account_rejects_per_agent_url`)
- **Multi-segment subdir resolution** — `_find_agent_dirs_under` walks deep enough
  to find a child `.cinna/` nested several levels under `agents/<slug>/`.
  (`tests/test_account.py:test_resolve_child_workspace_multi_segment_subdir`)
- **Same-agent re-sync is refused, slug collisions are bumped** — `run_agent_sync`
  refuses only when *this* agent already occupies the resolved clone slug.
  (`tests/test_account.py:test_agent_sync_refuses_already_synced`)
- **Best-effort revoke never blocks teardown** — a 404 / network error on
  `revoke_child_token` warns but local unsync still completes.
  (`tests/test_account.py:test_agent_unsync_warns_when_revoke_404`)
- **Backend errors surface verbatim** — foreign-install mint 403, ambiguous
  resolves, expired tokens are passed through, not pre-judged.
  (`tests/test_account.py:test_agent_sync_surfaces_mint_403_verbatim`)
- **Workspace scoping is client-side** — `run_account_agents` fetches the full
  set and filters by `user_workspace_id` locally; the backend keeps no active
  state. (`tests/test_account.py:test_account_agents_scoped_to_active_workspace`)
- **Credentials carry no secret** — create/update bodies are metadata only.
  (`tests/test_account.py:test_credentials_create_draft_lists_required_fields`)
- **Safe extraction** — the context tarball reuses the path-traversal/absolute/
  symlink-rejecting extractor.
  (`tests/test_account.py:test_context_extraction_rejects_malicious_members`)
