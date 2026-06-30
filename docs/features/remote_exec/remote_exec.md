# Remote Exec (`cinna exec`)

## Purpose

Run an arbitrary command **inside the remote agent container** from the local
terminal and watch its output stream back live — `cinna exec python
scripts/main.py`, `cinna exec pip install pandas`, `cinna exec bash -c '…'`. The
process runs where the agent actually runs (the platform-managed env, not the
laptop), and the command's exit code becomes `cinna exec`'s exit code, so it
drops cleanly into scripts and CI-style checks.

## Mental model — the command runs *over there*

- **Local files vs. remote process.** Your edits live on disk locally and are
  mirrored into the container by the live-sync (Mutagen) loop. `cinna exec` does
  **not** ship code — it only runs a command against whatever the container
  currently holds. So the usual loop is: edit a file → let live-sync mirror it →
  `cinna exec` to run it. If a change hasn't synced yet, exec runs the old copy.
- **Remote working directory.** Commands start at the container's app root
  (`/app`); <!-- nocheck: container path --> the synced workspace sits under
  `workspace/` (`/app/workspace`). <!-- nocheck: container path --> Reference
  files relative to that, e.g. `cinna exec python workspace/scripts/main.py`.
- **One shot, no stdin.** Each `cinna exec` is a single non-interactive command:
  it streams stdout/stderr back and ends with an exit code. There is no
  interactive stdin — REPLs, debuggers, and prompts that wait for input are out
  of scope for the current endpoint. To run a shell snippet (pipes, redirects,
  `&&`), hand it to a shell explicitly: `cinna exec bash -c 'a | b > c'`.
- **Transparent passthrough.** Every token you type is preserved exactly. You
  quote arguments once, the ordinary way you would for a local command; the CLI
  takes care of getting them to the remote shell intact (see *Argument quoting*).

## User flows

### Run a command in the current agent's env
1. From inside an agent workspace, run `cinna exec <command…>`.
2. The CLI opens a streaming connection to the platform for this agent (using the
   agent's own token from `.cinna/config.json`). <!-- nocheck: home/workspace path -->
3. stdout and stderr arrive in real time on the matching local streams; when the
   remote process finishes, `cinna exec` exits with the **same exit code**.

### Run against a named agent from the account root
1. From an account workspace, run `cinna exec --agent <name|slug|id> <command…>`.
2. The CLI resolves the reference to a synced child workspace under `agents/` and
   uses **that child's** token — so the command runs against the right agent.
3. If the agent isn't synced yet, exec fails loud with a hint to run
   `cinna agent sync <agent>` first.

### Bound a long command / abort it
- `cinna exec --timeout N <command…>` caps the remote wall-clock run time
  (seconds). On expiry the platform kills the remote process.
- Press **Ctrl+C** to abort: the stream closes, the platform cleans up the remote
  process, and exec exits `130`.

## Business rules / guardrails

- **Exit code is faithful.** A normal finish exits with the remote process's exit
  code; a remote interruption exits `130`; a local Ctrl+C exits `130`; a remote
  error event exits `1`. This makes `cinna exec` safe to chain (`cinna exec … &&
  …`).
- **stdout vs. stderr are kept separate.** Output chunks tagged `stderr` are
  written to local stderr, everything else to local stdout — so redirection and
  piping behave as a caller expects.
- **Single layer of quoting.** Callers quote exactly once; the CLI re-quotes each
  token so the remote shell reconstructs the exact argv. No double-escaping.
- **Timeout is bounded and explicit.** `--timeout` accepts `1`–`86400` seconds
  and defaults to `1800` (30 min); the value is sent to the platform, which owns
  enforcement.
- **`--agent` must be already synced.** Exec never syncs or creates a workspace;
  it only targets an existing one. An unknown reference is a fail-loud error.
- **No interactive stdin.** Commands that block on input will hang until the
  timeout; use non-interactive forms (`bash -c`, `python -c`, flags that disable
  prompts).

## Argument quoting (the two-shell problem)

There are **two shell passes** between the keyboard and the remote process, and
each deserves only one round of quoting:

1. **Local shell** — the caller's terminal (or an agent's Bash tool) splits the
   line into argv and strips one layer of quotes. Because `exec` takes a
   variadic argument list, the CLI receives these already-split tokens.
2. **Remote shell** — the platform runs the command string through `/bin/sh -c`,
   parsing it a second time.

The CLI bridges the two by **re-quoting each token** before sending, so the
remote `sh -c` rebuilds the exact argv the caller typed. The result is a
transparent passthrough: a space- or metacharacter-bearing argument
(`"a b"`, `'[{"x":"y z"}]'`, `python -c 'print(sys.argv)'`) survives intact
rather than being re-split or mis-parsed by the remote shell. See the
implementation note in [remote_exec_tech.md](remote_exec_tech.md).

## Architecture overview

```
cinna exec [--agent X] -- <cmd…>
      │  shlex.join(tokens)            POST /api/v1/cli/agents/{id}/exec
      ▼  (re-quote once)               { "command": "<cmd>", "timeout": N }
   exec_cmd ──► _run_remote_exec ─────────────────────────────────► platform
   (main.py)        │  client.stream_exec (SSE)                       │
                    ▼                                                 ▼
        stdout/stderr deltas ◄── tool_result_delta ──── remote /bin/sh -c <cmd>
        exit code      ◄──────── done / interrupted / error ──── in agent container
```

The command runs against whatever live-sync has mirrored into the container;
exec itself moves no files.

## Integration points

- **Live Sync** ([../live_sync/live_sync.md](../live_sync/live_sync.md))
  — Mutagen mirrors local edits into the container; exec runs against that
  mirrored state. The edit → sync → `cinna exec` loop is the core dev cycle, and
  `cinna dev`'s conflict resolution uses `cinna exec rm` to delete the losing
  side of a remote conflict.
- **Account workspace** ([../account_workspace/account_workspace.md](../account_workspace/account_workspace.md))
  — `cinna exec --agent <ref>` resolves a synced child the same way the other
  `--agent` verbs do, running with that child's per-agent token.
- **Git Versioning** ([../git_versioning/git_versioning.md](../git_versioning/git_versioning.md))
  — acceptance scenarios use `cinna exec` to confirm a committed/checked-out
  version actually runs in the live container (`cinna git checkout --reload`,
  then `cinna exec`).

Implementation: see [remote_exec_tech.md](remote_exec_tech.md). Real-usage e2e
scenarios: see [remote_exec_acceptance.md](remote_exec_acceptance.md).
