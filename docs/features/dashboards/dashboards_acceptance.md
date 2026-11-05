# Dashboards — Acceptance Scenarios

## Preconditions

- A live cinna-core at C2 or later (`ef3dbc89`+, plus `c32f211b` for
  `GET dashboards/capabilities` and `7de29ca7` for `fresh=true`) with the frontend reachable
  for dashboard links.
- An account workspace with a valid token (`cinna account status`; renew with
  `cinna login` in the account root).
- A cloud agent you own, synced here (`cinna agent sync crm-agent`), whose
  environment has been rebuilt since C2 (`cinna agent rebuild-env crm-agent`).
  Ideally a second agent for the "other agent's tile" checks.
- Optionally an agent whose environment predates C2 (for scenario 9) and Cinna
  Desktop signed in to the same server (scenarios 12–13).
- The editable install: `which cinna` resolves to this repo's `src/cinna`
  (or `uv run --project <repo> cinna`).
- A component in the synced workspace:

```bash
cd agents/crm-agent/workspace
mkdir -p dashboards/deals/api
cat > dashboards/deals/component.json <<'EOF'
{"schema_version": 1, "title": "Open deals", "view": "table", "size": "m",
 "source": {"endpoint": "rows"},
 "endpoints": {"rows": {"script": "api/rows.py", "description": "Open deals."}},
 "params": {"stage": {"type": "enum", "values": ["all", "won"], "default": "all"}},
 "context": "One row per deal; amount in EUR."}
EOF
cat > dashboards/deals/api/rows.py <<'EOF'
import json, sys
p = json.loads(sys.stdin.read() or "{}")  # stdin is the params object itself
rows = [{"id": "d1", "name": "Acme", "stage": "won", "amount": 1200},
        {"id": "d2", "name": "Globex", "stage": "open", "amount": 800}]
if p.get("stage") == "won":
    rows = [r for r in rows if r["stage"] == "won"]
print(json.dumps({"columns": ["name", "stage", "amount"], "rows": rows}))
EOF
```

(The table view's exact result shape and the endpoint's stdin are defined in
the kit's `guides/14-dashboards.md`. Adjust `rows.py` to match if the checker
says otherwise; that is scenario 2's point.)

## Scenario catalog

### 1. List shows the component and its reference
- **Goal:** the developer sees what core sees, and the line to offer it.
- **Steps:**
  ```bash
  cinna dashboards list crm-agent
  cinna dashboards list crm-agent --json | tail -1 | jq '.components[0].reference'
  ```
- **Expected:** "Pushing local changes of CRM Agent to its environment first",
  a table row `deals | Open deals | table | ok | —`, then
  `cinna://component/bundle:<bundle_id>/deals` on its own line. The JSON
  reference is identical.
- **Watch for:** the component missing straight after it was written (the
  flush didn't run, or core ignored `fresh=true` and served its 10 s
  cache — core `7de29ca7`+ honours it); the reference wrapped or
  carrying markup.

### 2. Check finds a broken component, then passes
- **Goal:** `check` catches what the owner's card would show.
- **Steps:**
  ```bash
  sed -i.bak 's/api\/rows.py/api\/missing.py/' dashboards/deals/component.json
  cinna dashboards check crm-agent deals; echo "exit=$?"
  mv dashboards/deals/component.json.bak dashboards/deals/component.json
  cinna dashboards check crm-agent deals --run; echo "exit=$?"
  ```
- **Expected:** the first run lists a `script_missing`-style error and exits 1.
  The second exits 0 with "deals: ok (ran)", having run the checker in the
  environment (no local Python ran the script).
- **Watch for:** stale findings after the fix; `--run` lines not marked
  `(run)` when they differ from core's; exit 0 despite errors.

### 3. Run returns what a tile receives
- **Steps:**
  ```bash
  cinna dashboards run crm-agent deals --param stage=won
  cinna dashboards run crm-agent deals --param stage=lost; echo "exit=$?"
  ```
- **Expected:** first: JSON with only Acme, `params: stage="won"`, a fetched
  time. Second: core's sentence naming the allowed values, `(field: stage)`,
  exit 2.
- **Watch for:** params sent as strings when JSON was meant (`--param limit=5`
  must arrive as 5).

### 4. Reference resolves
- **Steps:**
  ```bash
  cinna dashboards ref crm-agent deals --check
  ```
- **Expected:** the reference line, then "Resolves: the owner gets a card."
  Pasting the line into a web chat with the agent renders a card with Preview
  and Add to dashboard.
- **Watch for:** `ref_env_asleep` for a suspended env (expected, and the
  message says Preview wakes it), versus `ref_gone` for a component that exists.

### 5. Place a tile on a new dashboard
- **Steps:**
  ```bash
  cinna dashboards add crm-agent deals --board "CLI test" --new-board --param stage=won --max-age 15m
  cinna dashboards tiles "CLI test"
  ```
- **Expected:** "Created dashboard CLI test", "Added deals to CLI test as tile
  <id> (m)", and a `/dashboards/<id>` link that opens with the tile at the top,
  6×2, showing only Acme. `tiles` lists it with `stage="won"` and a data age,
  without waking anything.
- **Watch for:** a 422 from core (extra config keys), a tile placed off-grid,
  the wrong size.

### 6. Drive the tile: params, refresh, call, edit
- **Steps:**
  ```bash
  cinna dashboards tile params "CLI test" deals
  cinna dashboards tile params "CLI test" deals --clear stage
  cinna dashboards tile refresh "CLI test" deals
  cinna dashboards tile call "CLI test" deals rows --param stage=won
  cinna dashboards tile edit "CLI test" deals --title "Deals" --size wide --every 5m
  ```
- **Expected:** params show `stage = "won"`, then "Params saved: all defaults"
  with both rows. Refresh prints fresh data, call prints Acme only, and edit
  updates the title and size (12×2), which the web page reflects on reload.
- **Watch for:** `--clear` leaving other params out (whole-set semantics), and
  edit dropping the stored params from the config.

### 7. A component with errors can't be placed
- **Steps:** break `component.json` as in scenario 2, then
  `cinna dashboards add crm-agent deals --board "CLI test"`.
- **Expected:** refusal listing the error lines and the `check` hint; no block
  created (`cinna dashboards tiles "CLI test"` unchanged).

### 8. Rate limit surfaces retry time
- **Steps:**
  ```bash
  for i in $(seq 1 31); do cinna dashboards run crm-agent deals --no-flush --json | tail -1 | jq -r '.code // .result'; done
  ```
- **Expected:** 30 `ok`, then `rate_limited` with "Core allows 30 runs a minute
  per tile or preview; try again in Ns". Core's Preview dialog in the web UI
  still works meanwhile (separate `preview` budget).
- **Watch for:** the CLI looping on `rate_limited`; the web Preview blocked
  by CLI runs.
- **Note (live, 2026-11-04):** sequential CLI runs take over 2 s each, so 31 of
  them never fill core's one-minute window. Run them in parallel
  (`seq 1 40 | xargs -P 14 -I{} cinna dashboards run … --json`) and the
  **account api-proxy's own** per-token limit trips first: "Platform error
  (429): escape hatch refused … Escape-hatch rate limit exceeded for this
  account token." That must never read as `dashboards_unsupported` (a
  regression found and fixed in that run). Core's `rate_limited` itself is
  covered by `tests/test_dashboards.py`.

### 8b. Rebuild, then sync again
- **Goal:** the first thing a developer does after scenario 9's remedy.
- **Steps:** `cinna agent rebuild-env crm-agent --yes`, edit a component, then
  `cinna dashboards list crm-agent`.
- **Expected:** the list either shows the edit (the flush worked) or warns
  "Sync flush failed …; reading what the environment has".
- **Watch for (live, 2026-11-04):** after a rebuild the existing Mutagen
  session stayed in `error` ("session is not currently able to synchronize",
  then "beta scan error … unexpected EOF"), and the child CLI token had
  expired (sync tunnel HTTP 403; re-minted through the account). The
  dashboards verbs warned and carried on as designed, but the edit only
  reached the environment by `cinna exec`. **Fixed the same day:** `flush()`
  now reconnects (`mutagen sync resume`) and re-mints a refused token, and
  `rebuild-env` / `restart-env` reconnect the session themselves. Verified live:
  rebuild → "Sync session reconnected." → an immediate push settled; a restart
  through the raw API, then `cinna dashboards list`, healed the session in its
  flush and the edit arrived. See [Live Sync](../live_sync/live_sync.md).

### 9. Environment that predates components
- **Steps:** `cinna dashboards list old-agent` and `cinna dashboards check old-agent --run`
  for an agent not rebuilt since C2.
- **Expected:** core's "Rebuild the environment to show components." plus
  `cinna agent rebuild-env old-agent`; `check --run` on a synced one gives
  `checker_missing` with the same remedy.

### 10. Dashboard chat uses the tile tools
- **Steps:**
  ```bash
  cinna chat --agent crm-agent --board "CLI test" --focus deals --new-chat "Show me only won deals on this tile"
  ```
- **Expected:** the opening `session` event has `dashboard`. The agent's
  message events show a `dashboards_tiles` and/or `dashboards_update_tile`
  call, and a `tiles` event reports `params_before` → `params` with
  `stage: "won"`. `cinna dashboards tile params "CLI test" deals` confirms it.
  Running the same command without `--new-chat` continues the same session
  (same `session_id`).
- **Watch for:** `no_dashboard` from the tools (the context or session didn't
  reach a dashboard chat), and a 400 on the message (the context was over the
  cap or not an object).

### 11. A tile tool outside a dashboard chat
- **Steps:** `cinna chat --agent crm-agent "Change my dashboard tile to won deals"`
- **Expected:** the agent says it can't change tiles from here (prompt rule).
  On OpenCode a tool call answers `no_dashboard`.

### 12. Desktop dashboards are read-only from the CLI
- **Setup:** in Cinna Desktop, add `deals` (a cloud agent component) to a
  desktop dashboard.
- **Steps:**
  ```bash
  cinna dashboards list crm-agent          # placements column
  cinna dashboards boards                  # desktop dashboard absent
  cinna dashboards tiles <desktop-dashboard-id>
  cinna dashboards tile params <desktop-dashboard-id> deals --set stage=won; echo "exit=$?"
  ```
- **Expected:** placements show "Cinna Desktop". `boards` doesn't list it.
  `tiles` by full id lists it as "(Cinna Desktop — read only)", and the params
  change is refused with `desktop_dashboard`, exit 1.

### 13. Reference from a local-dev session renders on desktop
- **Steps:** in a desktop local-dev build session of the cloud agent, have the
  assistant print the line from scenario 4.
- **Expected:** desktop draws a card for the owned cloud agent (not "not installed").

### 14. Import reports components and their cloud gaps
- **Setup:** a kit agent with `dashboards/deals/` whose `source.file` is
  `app-data/dashboards/deals/rows.json`, `refresh.on_app_start: true`, and a
  `requires_credentials` name missing from the manifest.
- **Steps:** `cinna agent import ./Local/crm --dry-run`, then the real import.
- **Expected:** "Dashboards: 1 component(s): deals" and three warnings
  (app-data doesn't travel, on_app_start ignored, an undeclared credential).
  The real import ends with the `bundle:` reference and the `add` hint. Nothing
  is refused.

### 15. Server without dashboards
- **Steps:** against a core without `GET dashboards/capabilities` (before
  `c32f211b`), `cinna dashboards list crm-agent --json`.
- **Expected:** `{"result":"error","code":"dashboards_unsupported",…}`, exit 1,
  and no other call made.

### 16. Per-agent guide carries the agent's own reference
- **Steps:** `cinna account refresh-context`, then
  `grep -n "cinna://component/bundle:" agents/crm-agent/CLAUDE.md`.
- **Expected:** §7b contains the agent's real bundle id and a working relative
  link to `context/local-kit/skills/cinna-dashboards/SKILL.md`. For an agent
  synced fresh against core `c32f211b`+, `BUILDING_AGENT.md`'s Dashboards
  section prints the same `cinna://component/bundle:<id>/` line, and
  `.cinna/config.json` lists `platform_skills`.

### 17. Live run record (2026-11-04, core `c32f211b`, agent `artifacts-probe-1017`)
- Passed: capabilities gate; `adapter_unsupported` → rebuild remedy;
  `env_unavailable` wake wait; `list`/`show`/`ref --check`; `check --run`
  catching a `shape_invalid` that static checks missed, then "ok (ran)";
  `run` with a param and a refused enum value (exit 2, field named);
  `add --new-board`, `tiles`, `tile params` (show, `--clear`), `tile data`,
  `tile call`, `tile edit`; desktop-origin dashboard read-only; `chat --board
  --focus` with the agent calling `dashboards_tiles` and
  `dashboards_update_tile` and the `tiles` event reporting `{}` →
  `{"stage":"won"}`.
- Found and fixed: a hatch rate-limit 429 on the capabilities call was
  reported as `dashboards_unsupported`.

## Cross-cutting invariants

- No frame URL is ever printed or requested (grep the `-v` log for `/frame`).
- Every remote read after a local edit sees the edit (flush + `fresh=true`).
- Refusals always carry core's sentence verbatim, plus at most one remedy line.
- `--json` output is one JSON object per line, ending in a `result` line; the
  `value` of `run` is raw (pipes into `jq`).
- Tiles on desktop dashboards are never changed by the CLI.
- No endpoint or checker run happens on the developer's machine.

## Cleanup

```bash
cinna dashboards tile remove "CLI test" deals --yes
cinna api DELETE dashboards/<CLI-test-dashboard-id>
rm -rf agents/crm-agent/workspace/dashboards/deals && cinna sync push --agent crm-agent
```
