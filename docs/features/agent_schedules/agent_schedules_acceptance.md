# Agent Schedules — Acceptance Scenarios (live e2e)

The catalog of **real-usage scenarios** for an agent doing *integration* testing
of `cinna agent schedule` against a **live** environment — a real platform
backend with a real account workspace and at least one real agent. These are not
unit tests; they exercise the actual account-API CRUD, the platform's cron
clock, the owned-vs-foreign authorization, and the natural-language → cron
generator. Where a scenario asserts a *scheduled* firing, the platform owns the
clock — prefer `run` (Run now) for deterministic assertions and treat true
cron-time firing as a slower, optional check.

How to use: pick scenarios relevant to the change, run the **Steps** verbatim
from inside an account workspace, assert the **Expected**, and watch for the
**Watch for** failure modes. The foreign-install scenarios (8–10) are the
highest-value ones for any change to authorization or body assembly.

## Preconditions

- A reachable platform (e.g. `http://localhost:8000` backend, `:5173` frontend)
  with an **account workspace** already set up: a `.cinna/account.json` reachable
  by walking up from the cwd (run `cinna login` or `cinna account` if unsure).
- **Editable install** of the CLI under test:
  `python3 -c "import cinna,os;print(os.path.dirname(cinna.__file__))"` must point
  at this repo's `src/cinna`. Confirm `which cinna` resolves and
  `cinna agent schedule --help` lists the verbs (list/generate/create/update/
  run/logs/delete).
- **At least one owned agent** you can fully manage; ideally **also a foreign
  (bundle) install** — an agent installed from another publisher's bundle — to
  exercise the 403 guards.
- Know each agent's reference (id, exact name, or slug) — `cinna account agents`
  lists them.

> Run every `cinna agent schedule` command from the account workspace (or any
> subfolder of it). The agent need not be synced locally — schedules are
> platform-only state.

## Scenario catalog

### 1. List shows the schedules in UTC

- **Goal:** the developer can see what's scheduled.
- **Steps:**
  ```
  cinna agent schedule list "<Agent Name>"
  ```
- **Expected:** a `Schedules (N)` table with name+id, type (`static` /
  `script`), `Cron (UTC)`, enabled state, and `Next run (UTC)`. For an agent
  with none, `No schedules for this agent.`
- **Watch for:** an unresolved agent ref (id/name/slug); cron shown in the wrong
  timezone (must be UTC); a crash on a `next_execution` of null (shows `—`).

### 2. Generate previews a cron without saving

- **Goal:** turn natural language into a cron string, review before committing.
- **Steps:**
  ```
  cinna agent schedule generate "<Agent Name>" "every weekday at 7am" --tz Europe/Berlin
  cinna agent schedule list "<Agent Name>"
  ```
- **Expected:** the first command prints a UTC cron, a description, the next run,
  and a ready-to-edit `cinna agent schedule create … --cron '…' --tz UTC` line.
  The list afterward is **unchanged** — generate persists nothing.
- **Watch for:** generate creating a schedule (it must not); a nonsense phrase
  ("sometimes") should fail loud with the platform error, not a stack trace.

### 3. Generate honors the type's minimum-interval floor

- **Goal:** the cadence floor differs by schedule type.
- **Steps:**
  ```
  cinna agent schedule generate "<Agent Name>" "every minute" --type static_prompt
  cinna agent schedule generate "<Agent Name>" "every minute" --type script_trigger
  ```
- **Expected:** the generator returns a cadence respecting each type's floor
  (script triggers may run more frequently than session-starting prompts). At
  least one should clamp / reject "every minute" for `static_prompt`.
- **Watch for:** the `--type` flag being ignored; identical output for both.

### 4. Create a static_prompt schedule

- **Goal:** create a recurring session-starting schedule.
- **Steps:**
  ```
  cinna agent schedule create "<Agent Name>" --name "Daily report" \
    --cron "0 7 * * 1-5" --tz Europe/Berlin --prompt "Produce the daily report"
  cinna agent schedule list "<Agent Name>"
  ```
- **Expected:** output reports the new id, the cron **in UTC** (Berlin 07:00
  shifted), type `static_prompt`, enabled `True`, and a next run. The schedule
  now appears in the list. Record the id for later scenarios.
- **Watch for:** the cron stored verbatim without timezone normalization;
  description empty (it should default to the name); enabled coming back false.

### 5. Create a script_trigger requires a command

- **Goal:** the script-trigger guard fires client-side.
- **Steps:**
  ```
  cinna agent schedule create "<Agent Name>" --name "DB check" \
    --cron "*/30 * * * *" --tz UTC --type script_trigger
  cinna agent schedule create "<Agent Name>" --name "DB check" \
    --cron "*/30 * * * *" --tz UTC --type script_trigger \
    --command "python scripts/check_db.py"
  ```
- **Expected:** the first call fails with `--command is required for a
  script_trigger schedule.` and makes **no** API call. The second succeeds and
  lists type `script`.
- **Watch for:** the guard being skipped (a request sent with no command); the
  command not stored.

### 6. Update is partial; toggling and cron+tz

- **Goal:** only the passed fields change, and cron is timezone-anchored.
- **Steps:**
  ```
  cinna agent schedule update "<Agent Name>" <sched_id> --disable
  cinna agent schedule update "<Agent Name>" <sched_id>            # expect error
  cinna agent schedule update "<Agent Name>" <sched_id> --cron "0 9 * * *"   # expect error
  cinna agent schedule update "<Agent Name>" <sched_id> --cron "0 9 * * *" --tz UTC --enable
  ```
- **Expected:** the disable toggles `enabled` to false and leaves all other
  fields intact; the empty update **errors** ("Nothing to update."); `--cron`
  without `--tz` **errors** ("--tz is required when changing --cron."); the
  final call succeeds and the list shows the new UTC cron, enabled again.
- **Watch for:** a no-op update silently succeeding; cron updated against a stale
  timezone; the partial update wiping the prompt/command it didn't touch.

### 7. Run now triggers off-cadence; logs record it

- **Goal:** fire a schedule immediately and see it in the logs.
- **Steps:**
  ```
  cinna agent schedule run "<Agent Name>" <sched_id>
  cinna agent schedule logs "<Agent Name>" <sched_id>
  ```
- **Expected:** `run` prints an env-state-aware message (e.g. triggered / env
  waking). `logs` shows a fresh row: when (UTC), status (`success` /
  `session_triggered` / `error`), exit code for a script trigger, and a detail
  line (command executed / prompt used / error). For a `script_trigger` whose
  command prints `OK`, status is `success` with **no** session; otherwise
  `session_triggered`.
- **Watch for:** run not appearing in logs; a script trigger always starting a
  session even when output is `OK`; logs not truncating long detail.

### 8. Foreign install — toggle / run / logs allowed

- **Goal:** an installer of a published bundle can pause and fire schedules.
- **Setup:** a foreign (bundle) install with a publisher-defined schedule.
- **Steps:**
  ```
  cinna agent schedule list "<Foreign Agent>"
  cinna agent schedule update "<Foreign Agent>" <sched_id> --disable
  cinna agent schedule run "<Foreign Agent>" <sched_id>
  cinna agent schedule logs "<Foreign Agent>" <sched_id>
  ```
- **Expected:** all four succeed — listing, toggling enabled, running now, and
  reading logs are permitted on a foreign install.
- **Watch for:** a toggle being rejected (it must be allowed); run/logs 403.

### 9. Foreign install — create / edit / delete are 403

- **Goal:** publisher-managed definitions can't be redefined by the installer.
- **Steps:**
  ```
  cinna agent schedule create "<Foreign Agent>" --name x --cron "0 7 * * *" --tz UTC
  cinna agent schedule update "<Foreign Agent>" <sched_id> --name "renamed"
  cinna agent schedule delete "<Foreign Agent>" <sched_id> --yes
  ```
- **Expected:** each command fails loud with the platform's 403 (forbidden /
  publisher-managed), and the schedule set is **unchanged** afterward.
- **Watch for:** any of these succeeding; a non-toggle update field slipping
  through; the CLI masking the 403 as a generic error with no guidance.

### 10. Delete (owned) confirms unless --yes

- **Goal:** removing an owned schedule is guarded by a confirmation.
- **Steps:**
  ```
  cinna agent schedule delete "<Agent Name>" <sched_id>          # answer "n"
  cinna agent schedule delete "<Agent Name>" <sched_id> --yes
  cinna agent schedule list "<Agent Name>"
  ```
- **Expected:** the first (answered no) **aborts** without deleting; the
  `--yes` call deletes and the schedule disappears from the list.
- **Watch for:** the confirmation being skipped without `--yes`; the schedule
  surviving a confirmed delete.

### 11. Agent-ref ambiguity is fail-loud

- **Goal:** an ambiguous or unknown agent name doesn't silently target the wrong
  agent.
- **Steps:** `cinna agent schedule list "<ambiguous-or-bogus ref>"`.
- **Expected:** for an ambiguous name, an error listing the matches and asking
  for the id; for an unknown ref, an error listing the available agents.
- **Watch for:** picking the first match silently; a stack trace instead of a
  clean ClickException.

## Cross-cutting invariants (must hold across all scenarios)

- **Generate never persists** — a preview leaves the schedule set unchanged.
- **No silent no-op / mis-targeting** — empty updates, cron-without-tz, ambiguous
  agent refs, and missing script-trigger commands all fail loud, before or
  instead of a write.
- **Authorization is platform-enforced** — owned installs get full CRUD; foreign
  installs get only toggle / run / logs, and the 403s surface clearly.
- **UTC is the reporting frame** — every cron/next-run shown is UTC regardless of
  the `--tz` used to author it.
- **No local footprint** — schedules live only on the platform; nothing is
  written to the local workspace, `.cinna/`, or `~/.cinna/`.

## Cleanup

- Delete any test schedules you created on **owned** agents:
  `cinna agent schedule delete "<Agent Name>" <sched_id> --yes`.
- Re-enable any foreign-install schedule you disabled in scenario 8:
  `cinna agent schedule update "<Foreign Agent>" <sched_id> --enable`.
- Nothing is written locally, so there is no local state to clean up; verify
  with `cinna agent schedule list` that only the intended schedules remain.
