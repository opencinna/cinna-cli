# Remote Exec — Acceptance Scenarios (live e2e)

The catalog of **real-usage scenarios** for an agent integration-testing
`cinna exec` against a **live** environment — a real platform backend and a real
agent container. These are not unit tests; they exist to catch what unit tests
miss: argument-quoting corruption across two shells, stdout/stderr interleaving,
exit-code drift, timeout enforcement, and `--agent` routing to the wrong env.

How to use: pick scenarios relevant to the change, run the **Steps** verbatim
against a live env, assert the **Expected**, and watch for the **Watch for**
failure modes.

## Preconditions

- A reachable platform (e.g. `http://localhost:8000` backend) with an account
  workspace already set up (`cinna account setup …`), or a setup token for
  `cinna setup`.
- **Editable install** of the CLI under test:
  `python3 -c "import cinna,os;print(os.path.dirname(cinna.__file__))"` must point
  at this repo's `src/cinna`. Confirm `which cinna` resolves and
  `cinna exec --help` lists `--agent` and `--timeout`.
- **At least one** synced agent whose container is warm; ideally **two** synced
  under one account workspace (to exercise `--agent` routing).
- `git` and `mutagen` on `PATH` (for the edit → sync → exec loop).

> Run `cinna exec` from inside the agent's workspace dir, or from the account
> root with `--agent <slug>`. The remote cwd is the container app root; the
> synced workspace is under `workspace/`.

## Scenario catalog

### 1. Basic command runs and streams

- **Goal:** a command runs in the container and its output comes back live.
- **Steps:**
  ```
  cinna exec echo hello-from-remote
  cinna exec python -c 'print(2 + 2)'
  ```
- **Expected:** `hello-from-remote` then `4` on stdout; each command exits `0`
  (`echo $?` → `0`).
- **Watch for:** no output (stream not consumed); output buffered until the end
  instead of streaming; exit code not propagated.

### 2. Exit code is faithful

- **Goal:** the remote process's exit code becomes `cinna exec`'s exit code.
- **Steps:**
  ```
  cinna exec bash -c 'exit 7'; echo "rc=$?"
  cinna exec false; echo "rc=$?"
  ```
- **Expected:** `rc=7` then `rc=1`. Chaining works: `cinna exec true && echo ok`
  prints `ok`; `cinna exec false && echo nope` prints nothing.
- **Watch for:** exec always exiting `0`; the `error`/`interrupted` paths
  overriding a real remote exit code.

### 3. stdout and stderr stay separate

- **Goal:** stderr output is delivered to local stderr, stdout to local stdout.
- **Steps:**
  ```
  cinna exec bash -c 'echo OUT; echo ERR 1>&2' 1>/tmp/exec.out 2>/tmp/exec.err
  cat /tmp/exec.out      # expect: OUT
  cat /tmp/exec.err      # expect: ERR
  ```
- **Expected:** `OUT` lands only in `exec.out`, `ERR` only in `exec.err`.
- **Watch for:** both streams merged onto stdout; stderr swallowed.

### 4. Argument quoting survives two shells (regression)

- **Goal:** spaces and shell metacharacters inside an argument reach the remote
  process intact — the two-shell quoting bug.
- **Steps:**
  ```
  cinna exec python -c 'import sys; print(sys.argv[1:])' "a b" '[{"x":"y z"}]'
  cinna exec python -c 'print("semicolon; paren ) brace }")'
  ```
- **Expected:** first prints `['a b', '[{"x":"y z"}]']` (two argv elements, spaces
  preserved); second prints the literal string. No `/bin/sh: Syntax error`.
- **Watch for:** `word unexpected (expecting ")")` or arguments re-split on
  spaces — the historical `" ".join` defect. Pass tokens separately, not as one
  pre-quoted string.

### 5. Explicit shell for pipes/redirects

- **Goal:** real shell features run when handed to a shell on purpose.
- **Steps:**
  ```
  cinna exec bash -c 'echo a && echo b | tr a-z A-Z'
  ```
- **Expected:** `a` then `B`.
- **Watch for:** the user expecting `cinna exec a | b` to pipe — it does not; the
  `|` is just an argv token unless inside `bash -c`.

### 6. Edit → sync → exec loop runs the new code

- **Goal:** a local edit reaches the container and exec runs the updated file.
- **Steps:**
  ```
  printf 'print("marker-v1")\n' > workspace/scripts/smoke.py
  cinna sync push                       # or rely on the live cinna dev loop
  cinna exec python workspace/scripts/smoke.py
  ```
- **Expected:** exec prints `marker-v1`.
- **Watch for:** exec running a stale copy because the file hasn't synced yet
  (sync still settling or parked behind a conflict). See
  [../live_sync/live_sync.md](../live_sync/live_sync.md).

### 7. `--agent` routes to the right synced agent

- **Goal:** from the account root, exec targets the named agent's container.
- **Setup:** two synced agents A and B under one account workspace.
- **Steps:**
  ```
  cinna exec --agent <A> bash -c 'echo I-am-$CINNA_AGENT_NAME || pwd'
  cinna exec --agent <B> python -c 'print("agent-B")'
  cinna exec --agent does-not-exist echo nope
  ```
- **Expected:** A's and B's commands run in their respective containers; the
  unknown reference fails loud: `Agent 'does-not-exist' is not synced …` with a
  `cinna agent sync` hint and a non-zero exit. Reference resolves by name, slug,
  or agent id. See
  [../account_workspace/account_workspace.md](../account_workspace/account_workspace.md).
- **Watch for:** `--agent` running against the wrong agent / the current dir;
  using the account token instead of the child's per-agent token.

### 8. Timeout kills a long command

- **Goal:** `--timeout` bounds remote wall-clock time.
- **Steps:**
  ```
  time cinna exec --timeout 3 bash -c 'sleep 30; echo done'; echo "rc=$?"
  ```
- **Expected:** the command is killed at ~3 s (well before 30 s), `done` is never
  printed, and exec exits non-zero. The remote-command `--timeout` passthrough
  also works: `cinna exec --timeout 60 -- some-tool --timeout 5` sends `60` to
  cinna and `--timeout 5` to the tool.
- **Watch for:** the command running to completion (timeout not enforced);
  cinna's `--timeout` being captured by the remote command, or vice-versa,
  without the `--` separator.

### 9. Ctrl+C aborts cleanly

- **Goal:** local interrupt tears down the remote process and exits 130.
- **Steps:** start `cinna exec bash -c 'sleep 60'`, press **Ctrl+C** after a
  second, then `echo "rc=$?"`.
- **Expected:** exec returns promptly, `rc=130`; the remote `sleep` is cleaned up
  server-side (no orphaned process).
- **Watch for:** hung terminal; a non-130 exit; the remote process left running.

### 10. Long, quiet command isn't dropped by the client

- **Goal:** a command that runs a while with no output isn't cut off by a client
  read timeout.
- **Steps:**
  ```
  cinna exec --timeout 120 bash -c 'sleep 45; echo finished'
  ```
- **Expected:** after ~45 s of silence, `finished` prints and exec exits `0` — the
  client has no read timeout, only the connect bound and the server `--timeout`.
- **Watch for:** the client aborting mid-run with a read/timeout error before the
  command finishes.

## Cross-cutting invariants (must hold across all scenarios)

- **Exit-code fidelity** — exec's exit code always reflects the remote outcome
  (process code / 130 on interrupt / 1 on remote error); never silently `0`.
- **One layer of quoting** — the caller quotes once; arguments arrive byte-exact
  at the remote process, no double-escaping or re-splitting.
- **Stream separation** — stdout and stderr are never merged or swapped.
- **No local state mutation** — exec writes nothing to `.cinna/config.json`
  <!-- nocheck: workspace path --> or the registry; it only runs a command.
- **Fail-loud targeting** — an unknown `--agent` reference errors with guidance,
  never falls back to the wrong env.

## Cleanup

- Remove any files the scenarios created in the container:
  `cinna exec rm -f workspace/scripts/smoke.py` (and the same locally, then let
  sync mirror the deletion).
- Delete local scratch files (`/tmp/exec.out`, `/tmp/exec.err`).
- No registry or config cleanup is needed — exec leaves no local state behind.
