# Doctor — Acceptance Scenarios (live e2e)

The catalog of **real-usage scenarios** for an agent doing *integration* testing
of `cinna doctor` against a **live** environment — a real platform backend, real
agent containers, a real shared Mutagen daemon, and a real account workspace.
These are not unit tests; they exist to catch what unit tests miss: sessions that
retry a dead remote forever, registry/daemon drift across command sequences,
account-token expiry fan-out, and accidentally touching another tool's Mutagen
sessions.

How to use: pick scenarios relevant to the change, run the **Steps** verbatim
against a live env, assert the **Expected**, and watch for the **Watch for**
failure modes. The token and account scenarios (7–9) are the highest-value ones —
that's where the subtle fan-out bugs live.

## Preconditions

- A reachable platform (e.g. `http://localhost:8000` backend, `:5173` frontend)
  with an **account workspace** already set up (`cinna login` / `cinna account
  setup …`) and at least one agent synced under it
  (`cinna agent sync "<Agent Name>"`).
- Ideally a **second, standalone** agent set up directly with `cinna setup
  <token>` (no parent account) — needed for the manual-token scenario.
- **Editable install** of the CLI under test:
  `python3 -c "import cinna,os;print(os.path.dirname(cinna.__file__))"` must point
  at this repo's `src/cinna`. Confirm `which cinna` resolves and
  `cinna doctor --help` lists `--dry-run` and `--yes`.
- `mutagen` on `PATH` and the daemon reachable (`mutagen daemon start` if needed).
- The registry at `~/.cinna/agents.json` is real machine state — **back it up**
  before destructive scenarios: `cp ~/.cinna/agents.json /tmp/agents.json.bak`.

> Doctor operates on machine-global state (the registry + the shared Mutagen
> daemon), not on the cwd. It can be run from anywhere.

## Scenario catalog

### 1. Healthy machine reports clean

- **Goal:** a machine with no drift says so and changes nothing.
- **Setup:** every registry entry has an intact folder, a valid token, and (if
  any) a healthy `watching` session. Bring sessions down first if you want a truly
  clean state: `cinna disconnect` in each agent dir.
- **Steps:**
  ```
  cinna doctor
  ```
- **Expected:** prints `Everything looks healthy — no stale sync state found.` and
  exits 0. No prompts.
- **Watch for:** spurious findings against healthy agents; a prompt appearing when
  there's nothing to do.

### 2. Dry-run reports but never changes

- **Goal:** `--dry-run` is a pure read.
- **Setup:** create at least one stale entry — e.g. sync an agent, then delete its
  folder: `rm -rf <agent-clone-dir>` (leave the registry entry behind).
- **Steps:**
  ```
  cinna doctor --dry-run
  cat ~/.cinna/agents.json | python3 -m json.tool | grep -c agent_id
  ```
- **Expected:** the report shows a "Stalled sessions / state" table with the
  deleted-workspace finding, then `Dry run — nothing changed. Re-run without
  --dry-run to apply.` The registry entry count is **unchanged**; no session is
  terminated.
- **Watch for:** dry-run mutating the registry or terminating a session; a prompt
  being shown.

### 3. Deleted workspace folder is reaped

- **Goal:** a registry entry whose folder is gone is removed (with any leftover
  session).
- **Setup:** sync an agent, run `cinna dev` once (creates a session), quit, then
  `rm -rf <agent-clone-dir>`.
- **Steps:**
  ```
  cinna doctor --yes
  mutagen sync list | grep cinna-           # the agent's session
  cat ~/.cinna/agents.json                  # the agent's entry
  ```
- **Expected:** doctor reports the agent under "Workspace folder deleted", applies
  it, and prints `registry entry removed` (and `session terminated` if one
  lingered). Afterward the `cinna-<id>` session and the registry entry are both
  **gone**; summary shows `doctor applied 1 fix(es).`
- **Watch for:** the entry surviving; the leftover session being reported
  separately as an orphan instead of reaped with the entry.

### 4. Session stuck on a dead remote is terminated

- **Goal:** a session retrying a gone/suspended remote env is cleared (Mutagen
  never gives up on its own).
- **Setup:** start `cinna dev`, then make the remote unreachable (suspend/delete
  the env on the platform, or kill connectivity) so the session goes to a
  `connecting…`/error state with `beta.connected=false`. Background or quit the TUI
  leaving the session registered.
- **Steps:**
  ```
  mutagen sync list                 # confirm the cinna-<id> session is erroring
  cinna doctor --yes
  mutagen sync list | grep cinna-
  ```
- **Expected:** doctor lists the agent under "Stalled sessions / state" with
  "session can't reach the remote env", terminates it, and it disappears from
  `mutagen sync list`. `cinna dev` later recreates a fresh one.
- **Watch for:** the dead session being left to retry forever; a healthy session
  on a different agent being terminated too.

### 5. Halted-on-root-deletion session is recreated cleanly

- **Goal:** deleting just the `workspace/` root (agent dir otherwise intact) is
  recoverable.
- **Setup:** with `cinna dev` running, `rm -rf <agent-dir>/workspace` so Mutagen
  reports `halted-on-root-deletion`. Keep `.cinna/config.json`.
- **Steps:**
  ```
  cinna doctor --yes
  ```
- **Expected:** the agent is reported as "Session halted (local root deleted)",
  the session is terminated, and the registry entry is **kept** (the workspace is
  still considered intact via `.cinna/config.json`). A subsequent `cinna dev`
  re-syncs the folder.
- **Watch for:** the entry being wrongly reaped as a deleted-folder; the session
  left halted.

### 6. Orphan session with no registry entry is swept

- **Goal:** a `cinna-*` session whose registry entry is gone is terminated.
- **Setup:** create a session (`cinna dev`), then remove only the registry entry
  by hand (edit `~/.cinna/agents.json` to drop that agent), leaving the live
  session.
- **Steps:**
  ```
  cinna doctor --yes
  ```
- **Expected:** the session is listed under "Orphaned session (no registry entry)"
  with the folder derived from its sync root, and terminated.
- **Watch for:** the orphan being missed; or a non-`cinna-*` session being treated
  as an orphan (see scenario 10).

### 7. Account-managed expired token is auto re-minted

- **Goal:** an expired CLI token under an account workspace is healed with no
  paste.
- **Setup:** an agent synced under a valid account workspace whose CLI token has
  expired (or revoke it from the UI; the platform answers 401 on
  `/sync-runtime`). Confirm `cinna list` labels it `expired token`.
- **Steps:**
  ```
  cinna doctor --yes
  cinna list          # re-check the token label
  ```
- **Expected:** doctor reports the agent under "Expired tokens — account re-mint",
  re-mints through the parent account token, prints `token re-minted via account`,
  and rewrites both `.cinna/config.json` and the registry entry. `cinna list` now
  labels the agent `valid token`.
- **Watch for:** the re-mint writing a token for the **wrong** agent (must abort);
  the registry `workspace_path` or git block being disturbed by the rewrite.

### 8. Standalone expired token is reported, never changed

- **Goal:** with no parent account, doctor cannot self-heal and must say so.
- **Setup:** a **standalone** agent (set up via `cinna setup`, not under an
  account) with an expired/revoked token.
- **Steps:**
  ```
  cinna doctor --yes
  ```
- **Expected:** the agent appears under "No automatic fix — manual action needed",
  the tail prints `<agent> — run cinna set-token <token>`, the summary shows
  `applied 0 fix(es)` for tokens, and the registry entry is **untouched** (no
  re-mint attempted, no 401 storm).
- **Watch for:** doctor attempting a re-mint with no account (a 401 / crash); the
  entry being modified.

### 9. Expired account token groups its blocked sub-agents

- **Goal:** when the account token itself is expired, doctor surfaces one
  "run `cinna login`" finding rather than a pile of doomed re-mints.
- **Setup:** an account workspace whose **account** token is expired, with two
  agents under it whose CLI tokens are also expired.
- **Steps:**
  ```
  cinna doctor --dry-run
  ```
- **Expected:** a single "Account token expired — renew it" finding naming **both**
  sub-agents and instructing `run 'cinna login' in <account_root>`; it is
  report-only (no auto-fix). The account token is probed **once**, not once per
  sub-agent (watch the backend access log).
- **Watch for:** N separate re-mint attempts that all 401; the account token being
  probed repeatedly.

### 10. Foreign Mutagen sessions are never touched

- **Goal:** the shared daemon's non-cinna sessions are invisible to doctor.
- **Setup:** create a non-cinna Mutagen session alongside cinna's, e.g.
  `mutagen sync create --name some-other-tool <pathA> <pathB>`.
- **Steps:**
  ```
  cinna doctor --yes
  mutagen sync list | grep some-other-tool
  ```
- **Expected:** the `some-other-tool` session never appears in any doctor table
  and is **still present** afterward. Doctor only acts on `cinna-*`.
- **Watch for:** a foreign session being listed as "active" or terminated.

### 11. Active-session sweep clears healthy leftovers (default-Yes)

- **Goal:** healthy but no-longer-needed sessions are tidy-able, recreated on
  demand.
- **Setup:** a healthy agent (intact folder, valid token) with a `watching`
  session left from a past `cinna dev`. No actual problems.
- **Steps:**
  ```
  cinna doctor            # press Enter at the "Terminate N active session(s)?" prompt
  mutagen sync list | grep cinna-
  ```
- **Expected:** doctor prints `No problems found — only leftover sessions to tidy
  up.`, lists the session under "Active Mutagen sessions" tagged with the agent
  name + folder, and the **default-Yes** prompt (bare Enter) terminates it;
  summary shows `terminated 1 session(s)`. `cinna dev` recreates it later.
- **Watch for:** the active session being mislabeled as stalled; the prompt
  defaulting to No; the agent/folder tag being wrong (orphan-style derivation for
  a registered agent).

### 12. Decline leaves everything running

- **Goal:** answering No to a step applies nothing for it.
- **Setup:** as scenario 3 (a stale entry) or 11 (an active session).
- **Steps:**
  ```
  cinna doctor            # answer 'n' to the step prompt
  ```
- **Expected:** for stalled, `No stalled sessions deleted.` and the entry survives;
  for active, `Sessions left running.` and the session is untouched.
- **Watch for:** a declined step still mutating state.

## Cross-cutting invariants (must hold across all scenarios)

- **`cinna-*` only** — no table ever lists, and no fix ever terminates, a session
  whose name doesn't start with `cinna-`.
- **Report on every run** — `--dry-run`, interactive, and `--yes` all print the
  full report (including the active-session inventory) before any action.
- **Dry-run = zero mutation** — no registry write, no token re-mint, no session
  terminate under `--dry-run`.
- **No wrong-agent token writes** — a re-mint that returns another agent's id
  aborts; a successful re-mint touches only that agent's config + registry entry
  and never its git block or `workspace_path`.
- **No state drift** — reaping one entry/session never disturbs another agent's
  registry entry or its live session.
- **Fail-loud where unfixable** — standalone and expired-account cases are
  reported with the exact human remedy, never silently retried.

## Cleanup

- Restore the registry if a scenario corrupted it: `cp /tmp/agents.json.bak
  ~/.cinna/agents.json`.
- Re-sync any agent whose session you terminated: `cinna dev` (recreates the
  session) or `cinna agent sync "<Agent Name>"` for a deleted checkout.
- Refresh any token you expired for testing: `cinna login` (account) or
  `cinna set-token <token>` (standalone).
- Remove any throwaway Mutagen session created for scenario 10:
  `mutagen sync terminate some-other-tool`.
- Confirm a final `cinna doctor` reports a clean machine.
