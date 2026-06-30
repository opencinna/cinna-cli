# Remote Exec — Technical Reference

Implementation of [remote_exec.md](remote_exec.md). cinna-cli is a Python CLI;
all logic lives in `src/cinna/`, tests in `tests/`.

## File locations

- `src/cinna/main.py` — the `cinna exec` command (`exec_cmd`) and the stream
  driver `_run_remote_exec`.
- `src/cinna/client.py` — `PlatformClient.stream_exec()`, the SSE transport, and
  the `EXEC_STREAM_TIMEOUT` httpx timeout.
- `src/cinna/account.py` — `find_account_root()` / `resolve_child_workspace()`
  for `--agent` resolution against the account workspace.
- `src/cinna/config.py` — `find_workspace_root()` / `load_config()` for the
  in-dir (no `--agent`) path; `CinnaConfig` carries `agent_id` and the token.
- Tests: `tests/test_main.py` (`test_exec_command`, the quoting regression
  `test_exec_command_requotes_args`, and the logging contract
  `test_run_remote_exec_logs_start_and_stop`).

## Command surface

- `cinna exec [--agent <ref>] [--timeout/-t N] -- <command…>` →
  `src/cinna/main.py:exec_cmd()`.

`exec_cmd` is declared with `context_settings={"ignore_unknown_options": True}`
and a variadic `command` (`nargs=-1`, `required=True`) so flags meant for the
remote command (e.g. its own `--timeout`) pass through untouched; separate
cinna's options from the remote command with `--` when they would otherwise
collide.

## Key functions & flow

- `src/cinna/main.py:exec_cmd()` — resolves the target config, then calls
  `_run_remote_exec(config, shlex.join(command), timeout=timeout)` and
  `sys.exit()`s the returned code.
  - `--agent` set → `find_account_root()` + `resolve_child_workspace()`; `None`
    result raises a fail-loud `ClickException` pointing at `cinna agent sync`.
  - `--agent` omitted → `find_workspace_root()` + `load_config()` (current agent
    workspace).
  - **Quoting** — `shlex.join(command)` re-quotes each already-split token into a
    single command string so the remote `/bin/sh -c` reconstructs the exact argv.
    This replaced a naive `" ".join(...)` that lost word boundaries; the
    regression is locked by `tests/test_main.py:test_exec_command_requotes_args`.
- `src/cinna/main.py:_run_remote_exec()` — opens `PlatformClient(config)` and
  iterates `client.stream_exec(config.agent_id, command_str, timeout=…)`,
  dispatching by event `type`:
  - `exec_id` — first event; the remote exec id is remembered (for interrupt
    routing / logging).
  - `tool_result_delta` — `content` written to `sys.stdout` or `sys.stderr` per
    `metadata.stream` (default `stdout`), flushed immediately; byte counters per
    stream are accumulated for the stop log.
  - `done` — terminal; `exit_code = int(event["exit_code"])`.
  - `interrupted` — terminal; `exit_code = int(event.get("exit_code", 130))`.
  - `error` — `content` printed via `console.error`; `exit_code = 1`.
  - `KeyboardInterrupt` (local Ctrl+C) — caught; `exit_code = 130`.
  - Returns the resolved exit code to `exec_cmd`.
- `src/cinna/client.py:stream_exec()` — `POST /api/v1/cli/agents/{id}/exec` with
  `{"command": <str>}` plus `{"timeout": N}` when given; reads the `text/event-
  stream` line-by-line, strips the `data: ` prefix, and `yield`s each parsed JSON
  event. A `>= 400` status is drained and routed through `_handle_response` so
  the error surfaces; malformed `data:` lines are logged and skipped.

## Config & target resolution

- In-dir mode reads `.cinna/config.json` <!-- nocheck: workspace path --> via
  `load_config()`; the resulting `CinnaConfig.agent_id` is the path id in the
  endpoint and the token authenticates the `PlatformClient`.
- `--agent` mode uses the **child workspace's own** `CinnaConfig` (its per-agent
  token), found by `resolve_child_workspace()` which matches the reference
  against the child's `agent_id`, the directory slug, or the slugified
  `agent_name`.
- No state is written by exec — it neither touches the registry
  (`~/.cinna/agents.json`) <!-- nocheck: home path --> nor mutates config.

## External contracts

- **Endpoint:** `POST /api/v1/cli/agents/{id}/exec` (CLI JWT for that agent).
  Request body `{"command": "<shell string>", "timeout": <seconds>}`. Response is
  an SSE stream of `data: <json>` lines with `type` in
  `{exec_id, tool_result_delta, done, interrupted, error}`. The canonical event
  table lives in the "Remote Exec" section of `docs/README.md`.
- **Remote shell:** the platform executes `command` through `/bin/sh -c` in the
  agent container (cwd = the container app root). The CLI relies on `shlex.join`
  producing POSIX-sh-safe quoting so a single pass of `sh -c` reproduces the argv.
- **HTTP transport:** `EXEC_STREAM_TIMEOUT = httpx.Timeout(None, connect=10.0)` —
  a 10 s connect bound but **no read timeout**, so a long-running but quiet
  command isn't cut off by the client; wall-clock bounding is the server-side
  `--timeout` instead.

## Edge cases & guardrails (preserve these)

- **`shlex.join`, never plain join** — the two-shell quoting fix; reverting it
  re-breaks any argument containing a space or `sh` metacharacter.
  (`tests/test_main.py:test_exec_command_requotes_args`)
- **stdout/stderr split honored** — route by `metadata.stream`, flush each chunk;
  buffering or merging would corrupt piped/redirected output.
- **Exit-code fidelity** — map `done`/`interrupted`/`error`/Ctrl+C to the right
  code; exec is meant to be chainable, so a swallowed non-zero is a real bug.
- **No client read timeout** — keep `EXEC_STREAM_TIMEOUT`'s read side unbounded;
  the server `--timeout` (1–86400 s, default 1800) owns run-time limits.
- **Observability contract** — `_run_remote_exec` must emit `cinna.exec` start
  and stop log records (agent, timeout, command, exec_id, exit code, duration,
  per-stream byte counts, terminal-event reason); these are asserted by
  `tests/test_main.py:test_run_remote_exec_logs_start_and_stop`.
- **Fail-loud `--agent`** — an unresolved reference raises rather than silently
  falling back to the current dir.
