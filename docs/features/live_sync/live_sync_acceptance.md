# Live Sync — Acceptance Scenarios (live e2e)

The catalog of **real-usage scenarios** for an agent doing *integration* testing of
`cinna sync` and the Mutagen transport against a **live** environment — a real
platform backend and a real agent container. These are not unit tests; they exist to
catch what unit tests miss: the daemon being shared across agents, a suspended env
waking mid-handshake, conflicts that only appear when both sides edit the same file,
and backups that must never be silently dropped.

How to use: pick scenarios relevant to the change, run the **Steps** verbatim against
a live env, assert the **Expected**, and watch for the **Watch for** failure modes.
Prefer the multi-agent and conflict scenarios (6–11) on any change to the shim,
session lifecycle, or resolution — that's where the subtle bugs live.

## Preconditions

- A reachable platform (e.g. `http://localhost:8000` backend, `:5173` frontend) with
  an account workspace already set up (`cinna account setup …`), or a setup token for
  `cinna setup`.
- **Editable install** of the CLI under test:
  `python3 -c "import cinna,os;print(os.path.dirname(cinna.__file__))"` must point at
  this repo's `src/cinna`. Confirm `which cinna` and `which cinna-sync-ssh` both
  resolve, and `cinna sync --help` lists `status/conflicts/push/pull/resolve`.
- **At least one** synced agent; ideally **two** (to exercise the shared daemon).
- `mutagen` on `PATH`, at the platform-pinned version (`cinna setup` verifies this).

> Run `cinna` commands from inside the agent's workspace dir, or from the account root
> with `--agent <slug>`. `cinna sync status`/`conflicts` are read-only and safe to run
> alongside a live `cinna dev`; `push`/`pull`/`resolve` mutate sync state.

## Scenario catalog

### 1. Setup establishes a live session

- **Goal:** a fresh checkout is continuously synced.
- **Steps:**
  ```
  cinna setup <token>          # or: cinna agent sync "<Agent Name>"
  cinna sync status
  ```
- **Expected:** status shows session `cinna-<short-id>`, State `connected`, and a
  conflict count of 0. `mutagen sync list` shows the same session.
- **Watch for:** session not created; State stuck `disconnected`/`error`; the session
  name not matching `cinna-` + the first 8 hex of the agent id.

### 2. Edit → sync → run reaches the container

- **Goal:** a local edit is live in the container without an explicit push.
- **Steps:**
  ```
  printf 'print("live-v1")\n' > workspace/scripts/smoke.py
  cinna sync push                     # block until settled
  cinna exec python workspace/scripts/smoke.py
  ```
- **Expected:** `cinna sync push` reports "Sync settled (connected)."; exec prints
  `live-v1`. (Exec cwd is `/app`; the workspace is at `/app/workspace`.)
- **Watch for:** the file not present in the container (flush returned before settle);
  push hanging instead of blocking-then-exiting.

### 3. Remote change flows back with `pull`

- **Goal:** a backend/container-side change reaches local disk.
- **Steps:**
  ```
  cinna exec sh -c 'printf "remote-marker\n" > /app/workspace/scripts/from_remote.py'
  cinna sync pull
  cat workspace/scripts/from_remote.py
  ```
- **Expected:** `from_remote.py` appears locally with `remote-marker` after `pull`
  settles.
- **Watch for:** the file never arriving; `pull` failing on a parked conflict instead
  of settling.

### 4. `cinna sync status` reports pending + conflicts honestly

- **Goal:** the status surface reflects real Mutagen state.
- **Steps:** make a quick local edit, immediately run `cinna sync status`.
- **Expected:** a non-zero "Pending → remote" while the cycle is in flight, settling to
  0; conflict count matches `cinna sync conflicts`. When conflicts exist, status prints
  the yellow "your edits are NOT fully live" warning.
- **Watch for:** pending/conflict counts that disagree between `status` and
  `conflicts`; State collapsing watching/staging incorrectly.

### 5. `--agent` targets a child from the account root

- **Goal:** every sync verb works from the account workspace.
- **Steps:** from the account root, `cinna sync status --agent <slug>` and
  `cinna sync conflicts --agent <slug>`.
- **Expected:** both resolve the child workspace and report its session, identical to
  running them inside that agent's dir.
- **Watch for:** `--agent` failing to resolve a deeply nested agent dir; resolving the
  wrong agent.

### 6. Two agents live-sync concurrently on one daemon

- **Goal:** the shared Mutagen daemon serves two agents without cross-leaking
  credentials.
- **Setup:** sync a second agent (`cinna agent sync <id|name>`), both with active
  sessions.
- **Steps:** edit a file in each, `cinna sync push --agent A` and
  `cinna sync push --agent B`; then `cinna exec --agent A …` / `--agent B …` to read
  each back.
- **Expected:** each edit lands in its own container; `mutagen sync list` shows two
  distinct `cinna-*` sessions; neither push touches the other agent.
- **Watch for (the shim's reason for existing):** B's sync authenticating as A because
  the daemon's captured `CINNA_AGENT_ID` is stale — a 403, or B's files landing in A's
  container. The shim must resolve credentials from `~/.cinna/agents.json` keyed by the
  argv agent id, not the daemon env.

### 7. Two-sided edit parks a conflict (not a silent clobber)

- **Goal:** editing the same file on both sides produces a conflict, not data loss.
- **Steps:**
  ```
  # with sync briefly settled, change the SAME file on both ends close together:
  printf 'local-side\n'  > workspace/scripts/both.py
  cinna exec sh -c 'printf "remote-side\n" > /app/workspace/scripts/both.py'
  cinna sync status
  cinna sync conflicts
  ```
- **Expected:** status shows conflict count >= 1 with the "NOT fully live" warning;
  `cinna sync conflicts` lists `scripts/both.py` — a workspace-relative path. <!-- nocheck: illustrative workspace-relative path -->
  Both sides still hold their own content (no `.conflict.*` file is written — two-way-safe).
- **Watch for:** one side silently overwriting the other; `conflicts` showing nothing
  while `status` shows a count (the disk-walk vs daemon-JSON divergence).

### 8. `resolve --prefer local` keeps local, removes the remote loser

- **Goal:** local-wins resolution propagates the local version out.
- **Setup:** the parked conflict from #7.
- **Steps:**
  ```
  cinna sync resolve --prefer local
  cinna sync status
  cinna exec cat workspace/scripts/both.py
  ```
- **Expected:** "Resolved 1 conflict(s) in favor of local"; conflict count back to 0;
  the container now holds `local-side`.
- **Watch for:** the remote `rm` failing and the path landing in `remaining`; the
  conflict re-appearing after a couple of cycles (reset not converging).

### 9. `resolve --prefer remote` keeps remote, backs up local

- **Goal:** remote-wins resolution propagates the container version back AND preserves
  the local copy.
- **Steps:** re-create the conflict (#7), then
  ```
  cinna sync resolve --prefer remote
  cinna sync status
  cat workspace/scripts/both.py
  ls .cinna/sync/resolve-backup/*/scripts/
  ```
- **Expected:** conflict count 0; local file now holds `remote-side`; the displaced
  local copy exists under `.cinna/sync/resolve-backup/<ts>/scripts/both.py`, and the
  command printed "Local versions backed up to …".
- **Watch for (the no-silent-loss invariant):** the local version deleted with no
  backup; the backup dir reported but empty.

### 10. `push --force` / `pull --force` clear conflicts in their direction

- **Goal:** the one-shot flush verbs double as directional resolvers.
- **Steps:** create a conflict (#7), then `cinna sync push --force` (local wins) —
  re-create and run `cinna sync pull --force` (remote wins).
- **Expected:** `push --force` resolves toward local then settles; `pull --force`
  resolves toward remote (backing up local) then settles; both end at conflict count 0.
- **Watch for:** `--force` resolving the wrong direction; the flush failing instead of
  settling because a conflict was still parked.

### 11. `resolve` with no session fails loud

- **Goal:** resolving without a running session gives a clear instruction.
- **Steps:** `cinna disconnect` (or ensure no session), then
  `cinna sync resolve --prefer local`.
- **Expected:** a `ClickException` telling the user to start a session with
  `cinna dev` or `cinna sync push` first. Exit non-zero.
- **Watch for:** a stack trace or silent no-op instead of the guidance.

### 12. Suspended env auto-wakes on connect

- **Goal:** a cold agent environment is woken transparently.
- **Setup:** an agent whose env has been idle past the suspend grace (or freshly
  suspended in the UI).
- **Steps:** `cinna dev` (or `cinna sync push`).
- **Expected:** the CLI prints "Agent environment is not ready yet (waking up?).
  Retrying …" one or more times, then the session connects — no raw stack trace.
- **Watch for:** an immediate hard failure on the first `1013` close; the retry never
  terminating the half-registered session (so the retry can't recreate it).

### 13. Mutagen version pin is enforced

- **Goal:** a mismatched Mutagen can't start a silently-broken sync.
- **Steps:** with a Mutagen whose **minor** version differs from the platform pin
  (`GET …/sync-runtime`), run `cinna dev` non-interactively.
- **Expected:** startup is refused with a version-mismatch error. A patch-level-only
  difference instead just warns and proceeds.
- **Watch for:** a minor mismatch slipping through; a patch difference hard-blocking.

### 14. `credentials/` never syncs up from local

- **Goal:** the backend-managed credentials dir is ignore-listed.
- **Steps:** confirm `mutagen.yml` ignores `credentials/`; create a junk file under
  `workspace/credentials/` locally and `cinna sync push`.
- **Expected:** the junk file does NOT appear in the container; `cinna exec ls
  /app/workspace/credentials` shows only the backend-managed content.
- **Watch for:** the local file syncing up and clobbering managed credentials; a
  conflict on a credentials file the user was told never to edit.

## Cross-cutting invariants (must hold across all scenarios)

- **No silent clobber / no silent loss** — a two-sided edit always parks a conflict;
  remote-wins always backs the local copy up; an unremovable loser is reported, never
  hidden.
- **One runtime, mirrored** — a settled `cinna sync push`/`pull` means local and
  `/app/workspace` are byte-identical for non-ignored paths; `cinna exec` runs exactly
  what was synced.
- **Agent isolation** — the shared daemon never lets one agent's sync use another
  agent's token or land in another agent's container.
- **Status truth** — `cinna sync conflicts` and the count in `cinna sync status` always
  agree (both sourced from daemon JSON).
- **Fail-loud** — version mismatch, missing session, and unreachable env surface clear,
  actionable errors, never a raw stack or a silent no-op.

## Cleanup

- Remove test files locally and let sync propagate the deletion, or
  `cinna exec rm -f /app/workspace/scripts/<file>` for remote-only artifacts; then
  `cinna sync push` to settle.
- Delete leftover `.cinna/sync/resolve-backup/` and `redev-backup/` dirs created during
  conflict scenarios.
- `cinna disconnect` (in-dir) or `cinna agent unsync <slug>` to drop the local checkout
  and terminate its session; `cinna doctor` to reconcile any orphaned `cinna-*`
  sessions left in the daemon.
- Verify `~/.cinna/agents.json` has no leftover/bogus entries — especially if a helper
  ran **outside** pytest's global-state isolation (it would write the real registry).
