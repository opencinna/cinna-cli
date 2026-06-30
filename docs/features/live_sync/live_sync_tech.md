# Live Sync — Technical Reference

Implementation of [live_sync.md](live_sync.md). cinna-cli is a Python CLI; all
logic lives in `src/cinna/`, tests in `tests/`.

## File locations

- `src/cinna/sync_session.py` — the Mutagen wrapper: session lifecycle
  (`start`/`ensure_session`/`stop`), status mapping, `flush`, conflict listing and
  resolution, the SSH-shim directory materializer, and daemon env construction.
- `src/cinna/sync_ssh_shim.py` — `cinna-sync-ssh`, the SSH-style transport shim that
  bridges Mutagen to the platform `/sync-stream` WebSocket.
- `src/cinna/mutagen_runtime.py` — detect/install Mutagen and gate startup on the
  platform's version pin.
- `src/cinna/sync.py` — tarball/zip extraction used **only** for the initial clone
  (`GET /workspace`); continuous sync is entirely Mutagen.
- `src/cinna/sync_tui.py` — the Textual TUI attached by `cinna dev` (Sync / Details /
  Conflicts tabs).
- `src/cinna/main.py` — the `cinna sync` command group, plus `cinna dev` / `cinna
  redev` which drive the foreground session.
- Tests: `tests/test_sync_session.py` (lifecycle, status mapping, conflict
  extraction/resolution, retry-on-stale-daemon, retry-on-waking-env, flush),
  `tests/test_sync_ssh_shim.py` (argv parsing, agent-id extraction, ws-url derivation,
  credential resolution precedence), `tests/test_mutagen_runtime.py` (version parse,
  detect, version-pin gating), `tests/test_sync.py` (initial-clone extraction +
  path-traversal/symlink/size guards).

## Command surface

Each `cinna sync` verb → its handler in `src/cinna/main.py`:

- `cinna sync status` → `src/cinna/main.py:sync_status()`
- `cinna sync conflicts` → `src/cinna/main.py:sync_conflicts()`
- `cinna sync push` → `src/cinna/main.py:sync_push()`
- `cinna sync pull` → `src/cinna/main.py:sync_pull()`
- `cinna sync resolve` → `src/cinna/main.py:sync_resolve()`

Related foreground commands: `cinna dev` → `src/cinna/main.py:dev()`, `cinna redev`
→ `src/cinna/main.py:redev()` (both via `src/cinna/main.py:_run_dev_session()`).
Every `sync` subcommand resolves its target through
`src/cinna/main.py:_resolve_sync_target()` (current workspace, or an account-root
child via `--agent`).

## Key functions & flow

### Session lifecycle (`src/cinna/sync_session.py`)

- `session_name(agent_id)` — `cinna-<first 8 hex of agent id>`; one stable session
  per agent.
- `start(config, workspace_root)` — the foreground owner: upserts the registry,
  `ensure_daemon_running`, writes `mutagen.yml`, **terminates any same-named session**
  so there is exactly one owner, then `_create_session`. Used by `cinna dev`.
- `ensure_session(config, workspace_root)` — the headless counterpart: same setup but
  **reuses** an existing session (never terminates), creating one only if missing.
  Used by `cinna sync push`/`pull` so a scripted builder reuses a live `cinna dev`
  session and leaves a detached one persisting in the daemon.
- `_create_session(config, workspace_root)` — builds the OpenSSH-style remote URL
  `cinna@cinna-agent-<agent_id>:/app/workspace` <!-- nocheck --> and runs
  `mutagen sync create --name … --sync-mode=two-way-safe --ignore-vcs`. Retries
  transparently on a stale daemon and on a still-waking env (see guardrails).
- `flush(config)` — `mutagen sync flush <session>`; blocks until the cycle settles.
  Parked conflicts do **not** fail the flush (they surface in the returned status);
  only a genuine transport/session error raises.
- `stop(config)` / `terminate_named(name, config)` — terminate this agent's session /
  an arbitrary named session (the latter for `cinna doctor`, which has no per-agent
  config). `list_all_sessions(config)` returns the whole daemon inventory.
- `run_foreground(config, workspace_root)` — attaches `src/cinna/sync_tui.py:run_tui()`
  and terminates the session on exit so sync doesn't outlive the TUI.

### Status mapping (`src/cinna/sync_session.py`)

- `status()` / `_to_status()` — map Mutagen's JSON onto the `SyncStatus` dataclass.
  `base_status()` strips the side suffix (`staging-beta` → `staging`); the watching/
  scanning/staging/… family collapses to `connected`. Pending counts come from
  `alpha.stagedChanges` / `beta.stagedChanges`; conflict count from `conflictCount`
  or `len(conflicts)`. `state == "missing"` when no session exists.
- `_list_sessions()` — `mutagen sync list --template '{{json .}}'` (Mutagen 0.18.x has
  no `--json` flag); tolerant of `null`/list/dict shapes. `_find_session()` matches by
  `name`/`identifier` suffix.

### Conflicts (`src/cinna/sync_session.py`)

- `daemon_conflict_paths(config)` → `extract_conflict_paths(session)` — flatten the
  session's `conflicts[]` (`alphaChanges`/`betaChanges` paths, falling back to the
  conflict `root`). This is the authoritative source `cinna sync conflicts` uses, so
  it agrees with the status count.
- `resolve_conflicts(config, workspace_root, prefer, remote_delete=…)` — the
  delete-loser + `mutagen sync reset` recipe applied to all conflicted paths per
  round (`_REDEV_MAX_ROUNDS = 3` rounds for daemon settle): `prefer="remote"` moves
  each local copy into `.cinna/sync/resolve-backup/<ts>/`; `prefer="local"` calls
  `remote_delete(relpath)` (requires the callable) to remove the remote loser. Returns
  `resolved` / `remaining` / `backup_dir`.
- `resolve_startup_conflicts_favor_remote(...)` — the `cinna redev` variant; backs up
  to `.cinna/sync/redev-backup/<ts>/`.
- `_wait_until_settled(config, …)` — blocks until status is `watching` with evidence a
  cycle ran (`successfulCycles >= 1` or a populated `conflicts[]`); rejects a paused
  session; `require_cycle=False` after a `reset` (which can clear the cycle counter).
- `list_conflicts()` / `resolve_conflict()` / `group_conflicts()` — the legacy
  disk-walk for `*.conflict.<side>` files; retained only as the documented fallback
  (two-way-safe doesn't write them) and **not** wired into a live path.

### The SSH shim (`src/cinna/sync_ssh_shim.py`)

- `main()` — parse argv → host → agent id, resolve credentials, derive the ws URL,
  send the preamble, pump bytes.
- `_parse_argv(argv)` — OpenSSH-tolerant: strips value-taking flags (`-p`, `-i`, `-o`,
  `-l`, `-F`), honors `--`, returns `(host, remote_command_tokens)`.
- `_extract_agent_id(host)` — accepts `user@cinna-agent-<id>` or bare
  `cinna-agent-<id>`.
- `_ws_url(platform_url, agent_id)` — http→ws / https→wss; preserves a path prefix;
  builds `…/api/v1/cli/agents/<id>/sync-stream`.
- `_resolve_credentials(agent_id)` — **registry first** (`config.lookup_agent_registry`,
  re-read every call so token rotations take effect), env fallback **only** when no
  registry entry, gated by a `CINNA_AGENT_ID == argv agent_id` match to prevent
  cross-agent leakage.
- `_run(ws_url, token, preamble)` — connect with `Authorization: Bearer <token>`,
  send `{"remote_command": [...]}` as the first frame, then pump stdin→WS and
  WS→stdout concurrently, exiting on `FIRST_COMPLETED`.

### Mutagen runtime gating (`src/cinna/mutagen_runtime.py`)

- `detect_local_mutagen()` — `shutil.which("mutagen")` + `mutagen version`, parsed by
  `_parse_mutagen_version()`.
- `fetch_required_mutagen(client, agent_id)` — `GET /sync-runtime` → version + agent
  sha256 + platform API version.
- `ensure_mutagen_ready(...)` — install prompt if missing; on a **minor**-version
  mismatch refuse (interactive: confirm; non-interactive: raise
  `MutagenVersionMismatchError`); a **patch**-level mismatch only warns. On success,
  persists `mutagen_version` + `last_sync_runtime_check_at` to `.cinna/config.json`.

## Config & registry

- `mutagen.yml` (seeded by `src/cinna/sync_session.py:write_mutagen_yml()` from
  `MUTAGEN_YML_TEMPLATE`) — `mode: two-way-safe`, `permissions.mode: portable`,
  `ignore.vcs: true`, and a starter ignore list (`__pycache__/`, `node_modules/`,
  `.venv/`, `.cinna/`, `.mypy_cache/`, `.pytest_cache/`, `.DS_Store`,
  `credentials/`), `scan.mode: full`. Never overwritten if it already exists.
- `.cinna/config.json` — `mutagen_version` and `last_sync_runtime_check_at` (the
  cached version-pin check), via `src/cinna/config.py:save_config()`.
- `~/.cinna/agents.json` (runtime state) — written by
  `src/cinna/config.py:upsert_agent_registry()` (`start`/`ensure_session` call it),
  read by `src/cinna/config.py:lookup_agent_registry()` from the shim. Holds
  `platform_url`, `cli_token`, `workspace_path`; `0600`.
- `~/.cinna/mutagen-ssh/ssh` — the bash wrapper materialized by
  `src/cinna/sync_session.py:_ensure_ssh_shim_dir()`; `MUTAGEN_SSH_PATH` (set in
  `_mutagen_env()`) points at that **directory** (Mutagen searches it for an exe named
  `ssh`), not the binary.

## External contracts

- **Endpoints:** `GET /api/v1/cli/agents/{id}/sync-runtime` (version pin) and
  `WSS /api/v1/cli/agents/{id}/sync-stream` (the Mutagen tunnel). The shim's first WS
  frame is `{"remote_command": ["mutagen-agent", …]}`; the platform proxies to the
  container's `/sync/exec` and byte-pumps both ways. See the "Sync Transport" section
  of `docs/README.md` for the full wire path.
- **Mutagen** (user-installed binary on `PATH`): invoked via
  `src/cinna/sync_session.py:_run_mutagen()` with the env from `_mutagen_env()`. Relied
  -on invariants: OpenSSH-style `host:path` parsing (not `ssh://`), so the remote URL's
  first `:` resolves correctly and the shim sees `cinna-agent-<id>` as the host;
  `--template '{{json .}}'` for JSON; `two-way-safe` does not write `.conflict.<side>`
  files; `sync reset` re-bases a session with no common ancestor so a one-sided
  survivor propagates.
- **The container path** `/app/workspace` <!-- nocheck --> is the fixed bind-mount;
  `mutagen-agent` resolves it absolutely (not relative to cwd).

## Edge cases & guardrails (preserve these)

- **`start` terminates, `ensure_session` reuses.** `cinna dev` must own exactly one
  session; `cinna sync push`/`pull` must not kill a live `cinna dev` session.
  (`tests/test_sync_session.py:test_ensure_session_reuses_existing` /
  `test_ensure_session_creates_when_missing`.)
- **Stale-daemon retry.** A daemon started with a broken/old `MUTAGEN_SSH_PATH` fails
  `sync create` with "unable to locate command"; `_create_session` detects the marker,
  bounces the daemon once (`_restart_daemon`), and retries.
  (`test_start_retries_after_stale_daemon`.)
- **Waking-env retry.** A suspended env closes the WS with `1013` ("try again later");
  `_create_session` retries with `_WAKING_RETRY_DELAYS_SECONDS` backoff, terminating
  the half-registered session between attempts, then raises a friendly error.
  (`test_start_retries_when_agent_env_waking_then_succeeds`,
  `test_start_gives_up_with_friendly_error_after_max_waking_retries`.)
- **Shim never trusts daemon env for identity.** Registry-first with the
  `CINNA_AGENT_ID` match guard is what keeps concurrent multi-agent sync correct.
  (`test_resolve_credentials_registry_wins_over_env`,
  `test_resolve_credentials_env_fallback_when_registry_empty`,
  `test_resolve_credentials_unknown_agent_exits`.)
- **`MUTAGEN_SSH_PATH` is a directory, not a binary.** Pointing it at the shim exe
  yields "unable to identify 'ssh' command"; `_ensure_ssh_shim_dir` materializes a dir
  holding an `ssh` wrapper. (`test_mutagen_env_points_at_shim_dir_not_file`.)
- **Conflicts come from daemon JSON, not disk.** `two-way-safe` writes no
  `.conflict.*` files, so `cinna sync conflicts` must source from `conflicts[]` to
  match the status count. (`test_daemon_conflict_paths_sources_from_json`.)
- **Losers are backed up, never deleted.** Remote-wins resolution moves local copies
  to `.cinna/sync/…-backup/<ts>/`; unremovable losers land in `remaining`.
  (`test_resolve_conflicts_prefer_local_failed_delete_stays_remaining`.)
- **Version pin gates startup.** Minor-version mismatch blocks; patch-level only warns.
  (`test_ensure_minor_mismatch_blocks_non_interactive`,
  `test_ensure_patch_mismatch_allowed`.)
- **Initial-clone extraction is hardened.** `src/cinna/sync.py` rejects path traversal,
  symlinks, and files over 100 MB. (`tests/test_sync.py`.)
