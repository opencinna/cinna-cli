# Agent Schedules (`cinna agent schedule`)

## Purpose

Give a developer full CRUD over an agent's **automatic-execution schedules**
(CRON) from the terminal — the CLI equivalent of the agent's *Config →
Schedules* card. A schedule fires on a cron cadence and either starts a
conversation session with a prompt or runs a command that decides whether a
session is even needed. All schedules live and run **on the platform**; the CLI
only drives them through the account workspace's API.

## Mental model — platform-side automation the CLI drives

A schedule is **not** a local cron job. It is a platform record attached to one
agent: the platform owns the cron clock, wakes the agent's env at the scheduled
time, runs the schedule, and records each run. The CLI is a remote control —
every verb is one account-API call against the agent, resolved by id, name, or
slug. Nothing about a schedule lives in the local workspace; you can manage an
agent's schedules without ever syncing that agent's files.

Two kinds of schedule, distinguished by **what fires**:

- **`static_prompt`** — at the cron time the platform **always** starts a
  conversation session for the agent, optionally seeded with a per-schedule
  prompt (e.g. "Produce the daily report"). Use it for unconditional recurring
  work.
- **`script_trigger`** — at the cron time the platform runs a **shell command**
  in the agent env first; it **only** starts a session when the command's output
  is *not* `OK`. Use it for "check something, act only if it needs attention"
  (e.g. poll a DB, alert on drift). The command's exit code and output are
  recorded whether or not a session was triggered.

### Owned vs. foreign (publisher-managed) installs

Whether you can *change* a schedule depends on who owns the agent's bundle:

- **Owned install** — you authored/own the agent: full CRUD (create, update any
  field, delete).
- **Foreign (bundle) install** — the agent came from another publisher's
  bundle; its schedule **definitions are publisher-managed**. You may only
  **toggle** (enable/disable), **run now**, and **view logs**. Creating,
  editing other fields, or deleting is rejected by the platform (403). This
  keeps a published agent's automation behavior consistent with what its
  publisher shipped, while still letting the installer pause or trigger it.

## Cron and timezones

- `create` / `update` take `--cron` (a standard 5-field expression) interpreted
  in the `--tz` IANA timezone you give (default `UTC`). You write the cadence in
  *your* local time; the platform normalizes and stores it.
- Listings and previews always display the cron and the next run **in UTC**
  (`Cron (UTC)`, `Next run (UTC)`) — the single canonical frame, independent of
  the timezone you authored in.
- Changing `--cron` on an existing schedule **requires** `--tz` so the new
  expression is never reinterpreted against a stale timezone.

## User flows

### Discover what's scheduled
1. `cinna agent schedule list <agent>` prints a table: name + id, type
   (static / script), cron (UTC), enabled state, and next run (UTC). The id is
   what every other verb takes.

### Author a cadence from plain language (preview only)
2. `cinna agent schedule generate <agent> "every weekday at 7am" --tz Europe/Berlin`
   asks the platform to turn natural language into a cron string. It is an
   **LLM-assisted, stateless preview** — it returns the cron, a description, and
   the next run, and **saves nothing**. The output ends with a ready-to-edit
   `cinna agent schedule create …` line so you can review the cadence before
   committing to it. `--type` selects which *minimum-interval floor* the
   generated cadence must respect (script triggers may run more frequently than
   session-starting prompts).

### Create
3. `cinna agent schedule create <agent> --name … --cron … --tz …` creates the
   schedule. `--type static_prompt` (default) may carry a `--prompt`;
   `--type script_trigger` **requires** `--command`. `--description` defaults to
   the name. `--disabled` creates it switched off (author and verify before it
   can fire).

### Update / toggle
4. `cinna agent schedule update <agent> <schedule_id> …` changes **only** the
   fields you pass (`--enable`/`--disable`, `--name`, `--cron` + `--tz`,
   `--prompt`, `--command`, `--description`). Passing nothing is an error — the
   command refuses an empty no-op update. On a foreign install only
   `--enable`/`--disable` is honored.

### Run now
5. `cinna agent schedule run <agent> <schedule_id>` triggers the schedule
   immediately, out of band of its cron cadence — for testing a freshly created
   schedule or forcing an off-cycle run. Allowed even on foreign installs.

### Inspect runs
6. `cinna agent schedule logs <agent> <schedule_id>` shows the last 50 execution
   records: when (UTC), status (`success` / `session_triggered` / `error`), the
   command exit code (for script triggers), and a detail line (the error, the
   command executed, or the prompt used).

### Delete
7. `cinna agent schedule delete <agent> <schedule_id>` removes the schedule
   after a confirmation prompt (`--yes` / `-y` skips it). Rejected (403) on a
   foreign install.

## Business rules / guardrails

- **Account workspace required.** Every verb runs from an account workspace
  (`.cinna/account.json`); the agent is resolved by id, exact name, or slug
  against the account's agent listing. An ambiguous name fails loud and asks for
  the id.
- **`script_trigger` needs a command.** Creating a script trigger without a
  non-empty `--command` is refused client-side, before any API call.
- **Empty update refused.** `update` with no field changes errors rather than
  issuing a no-op request.
- **Cron change is timezone-anchored.** `--cron` without `--tz` is refused so an
  expression is never silently reinterpreted in a stale zone.
- **Foreign-install guard.** The platform enforces publisher ownership:
  create / non-toggle update / delete return 403 on a bundle install; toggle,
  run, and logs are always allowed.
- **Stateless generate.** `generate` never persists — it is a preview that can
  also fail loud (`success:false`) with an error the CLI surfaces.
- **UTC is the display frame.** Authoring is timezone-friendly; reporting is
  always UTC, so cadences are unambiguous across machines and team members.

## Architecture overview

```
cinna agent schedule <verb> <agent> [args]
      │  resolve agent (id / name / slug) against the account listing
      ▼
account.py:run_schedule_*  ──►  client.py:AccountClient.<schedule call>
      │                                   │
      │                                   ▼
      │             POST/GET/PUT/DELETE  /api/v1/cli/account/agents/{id}/schedules…
      ▼                                   │  (account-token auth)
  Rich table / status output             ▼
                                   Platform: owns the cron clock, the schedule
                                   records, env wake-ups, and execution logs
```

## Integration points

- **Agent management** (`../agent_management/agent_management.md`) —
  `cinna agent schedule` is a subgroup of the `cinna agent` account-workspace
  command family (`sync`, `show`, `status`, …); it shares the same account
  workspace, agent-resolution, and `AccountClient` plumbing.
- **Account workspace** — schedules ride the account token and the
  `/api/v1/cli/account/*` route group, the same surface `cinna account` and
  `cinna chat` use.
- **Remote Chat (`cinna chat`)** — a `static_prompt` schedule starts the same
  kind of conversation session that `cinna chat` drives manually; `run` is a
  quick way to fire one on demand.

Implementation: see [agent_schedules_tech.md](agent_schedules_tech.md).
Real-usage e2e scenarios: see
[agent_schedules_acceptance.md](agent_schedules_acceptance.md).
