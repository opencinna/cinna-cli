# Doctor — diagnose & repair stale sync state (`cinna doctor`)

## Purpose

`cinna doctor` is the one-shot **repair tool** for the per-machine state that
drifts as agents come and go: the per-user registry (`~/.cinna/agents.json`) and
the shared Mutagen daemon's sessions. It scans both, reports what is broken or
left over, and — after a few default-Yes confirmations — heals it: deleting
registry entries for vanished workspaces, terminating sessions that can never
recover, clearing healthy leftover sessions, and re-minting expired tokens it can
fix automatically.

## Why a dedicated command

Mutagen has **no "give up after N failures" knob**. A sync session that loses its
remote env (the container was suspended/deleted) or its local root (the
`workspace/` folder was deleted) does not stop — it retries forever, holding state
in the shared daemon. Likewise, deleting an agent's folder leaves a dangling
registry entry, and tokens expire on their own 7-day clock. None of this
self-heals, so `cinna doctor` is the explicit sweep that reconciles the registry
against the daemon and clears the wreckage.

## Mental model — two pieces of drifting state

- **The registry** (`~/.cinna/agents.json`) — one entry per synced agent
  (`agent_id → platform_url, cli_token, workspace_path, …`). The sync shim reads
  it on every transport call. Entries outlive the folders they point at.
- **The Mutagen daemon's sessions** — one `cinna-<short-id>` session per agent
  that has run `cinna dev`/`cinna setup`. The daemon is **shared across every tool
  on the machine** and persists across agents; doctor only ever looks at, reports,
  or terminates `cinna-*` sessions — never another consumer's.

Doctor's whole job is to make these two views agree, and to bin the leftovers
into "stalled" (broken, delete it), "active" (healthy, but tidy-able), and
"tokens" (expired, refresh if possible).

## What doctor checks (the finding categories)

Every entry in the registry is reconciled against the live session list, then
classified:

- **Workspace folder deleted** (`stale_folder`) — the entry's `workspace_path` is
  missing or its `.cinna/config.json` is gone. The entry is removed, plus any
  leftover session with it. Covers a deleted folder with *or* without a lingering
  session.
- **Session halted, local root deleted** (`zombie_session`) — the agent dir is
  intact but its `workspace/` root was deleted out from under a live session
  (`halted-on-root-deletion`). The session is terminated; `cinna dev` recreates a
  clean one.
- **Session stuck on an unreachable remote** (`dead_remote`) — the session can't
  reach the remote env (not connected to beta, a `lastError`, a `connecting…`
  status, or any `error`). Terminated — it would otherwise retry forever.
- **Orphaned session** (`orphan_session`) — a `cinna-*` session with **no**
  registry entry at all. Terminated.
- **Expired token — account re-mint** (`token_remint`) — an expired CLI token on a
  workspace that lives under an account workspace. Re-minted automatically through
  the parent account token (no paste).
- **Account token expired** (`account_token_expired`) — the account token that
  would do those re-mints has itself expired. The blocked sub-agents are grouped
  into a single "run `cinna login`" finding instead of a pile of re-mints that
  would all 401. Report-only.
- **Expired token — manual refresh** (`token_report`) — an expired CLI token on a
  standalone workspace (no parent account). Only a pasted setup token can refresh
  it, so doctor reports it and never changes it.

Alongside the broken findings, doctor also surfaces every **healthy, still-running
`cinna-*` session** as the "active" inventory — tagged with the agent name and
on-disk folder it serves — so the user always sees the daemon's full picture.

## User flows

### Just look (no changes)
1. `cinna doctor --dry-run` scans and prints every table — stalled state, active
   sessions, account-re-mintable tokens, and manual-only items — then stops with
   "Dry run — nothing changed." Nothing on disk, in the registry, or in the daemon
   is touched.

### Repair interactively
1. `cinna doctor` scans and prints the same report.
2. It then walks **three ordered, separately-confirmed steps**, each defaulting to
   **Yes** (press Enter to accept):
   1. **Delete stalled sessions?** — one prompt clears *all* the broken findings
      (deleted workspaces, halted/dead/orphaned sessions).
   2. **Terminate active sessions?** — clears the healthy leftover sessions; they
      are recreated on the next `cinna dev`, so this just frees the shared daemon.
   3. **Refresh expired tokens?** — re-mints the account-managed expired tokens.
3. Standalone expired tokens are printed at the end as "manual action needed" with
   the exact `cinna set-token <token>` to run per agent.
4. A summary reports how many fixes were applied and how many sessions terminated.

### Non-interactive
1. `cinna doctor --yes` (or `-y`) accepts every prompt automatically — same three
   steps, no questions. Useful in scripts or after a known-good diagnosis.

### Healthy machine
1. With nothing wrong and no leftover sessions, `cinna doctor` prints "Everything
   looks healthy — no stale sync state found." and exits.

## Business rules / guardrails

- **`cinna-*` scope only.** The daemon is shared; doctor never reports or
  terminates a session whose name doesn't start with `cinna-`. Another tool's
  sessions are invisible to it.
- **Report on every run.** The full report (including the active-session
  inventory) prints on every invocation — `--dry-run`, interactive, and `--yes`.
- **Default-Yes, but always confirmed (unless `--yes`).** Each of the three steps
  prompts with Yes as the default; declining a step applies nothing for that step
  and says so. `--dry-run` overrides everything and changes nothing.
- **Terminate before re-mint.** Session teardown runs before token re-mint so a
  healed agent ends with a fresh token and no dangling session, ready for the next
  `cinna dev`.
- **Re-mint is identity-guarded.** A re-mint that returns a token for a *different*
  agent aborts loudly rather than overwriting the wrong workspace's token.
- **Account-token expiry is grouped, not retried.** When the account token is
  expired, doctor probes it once per account root and emits a single renew-the-
  account finding — it does not fire per-agent re-mints that would all 401.
- **Standalone expired tokens are never auto-fixed.** With no parent account to
  mint from, the only remedy is a human pasting a fresh setup token via
  `cinna set-token`; doctor reports, never changes.
- **Daemon-down is non-fatal.** If the Mutagen daemon is down or Mutagen is
  missing, the session list comes back empty and doctor still does its
  registry-only work (stale folders, token findings).
- **Fail-soft per finding.** Applying one fix that errors logs that finding's
  error and continues with the rest — one bad re-mint doesn't abort the sweep.

## Architecture overview

```
cinna doctor [--dry-run] [--yes]
      │
      ▼
doctor.run_doctor
      ├── doctor.diagnose ──────────► reconcile registry ↔ daemon
      │      ├── list_agent_registry          (~/.cinna/agents.json)
      │      ├── sync_session.list_all_sessions (Mutagen daemon)
      │      ├── _probe_token_statuses         (GET /sync-runtime per agent)
      │      └── _probe_account_token          (GET /account/agents, per account root)
      │            → Findings: stale / zombie / dead / orphan / token_*
      │
      └── repair (skipped on --dry-run)
             1. delete stalled  → sync_session.terminate_named + remove_agent_registry
             2. terminate active→ sync_session.terminate_named
             3. refresh tokens  → AccountClient.mint_agent_token (POST /account/agents/{id}/mint)
```

## Integration points

- **Live Sync** (`../live_sync/live_sync.md`) —
  doctor reconciles exactly the Mutagen sessions and `cinna-<short-id>` naming that
  live sync creates; the sessions it terminates are recreated by `cinna dev`.
- **Account workspace** — re-mints ride the account token via
  `cinna agent sync`'s mint path; an expired account token is healed with
  `cinna login`.
- **Git Versioning** ([../git_versioning/git_versioning.md](../git_versioning/git_versioning.md)) —
  doctor keys on the agent dir (`workspace_path`) and never disturbs a linked
  agent's registry git block when it leaves an entry in place.
- **Token refresh** — standalone agents are pointed at `cinna set-token`; the
  registry/token fields doctor writes are the same ones `cinna setup` and
  `cinna dev` maintain.

Implementation: see [doctor_tech.md](doctor_tech.md). Real-usage e2e test
scenarios: see [doctor_acceptance.md](doctor_acceptance.md).
