# Dashboards (`cinna dashboards`, `cinna chat --board`)

## Purpose

Let a developer and their local assistant build an agent's **dashboard
components**, check and run them in the agent's real environment, print the
reference that offers one to the owner, place them as tiles on the owner's web
dashboards, and test the agent's behaviour in a dashboard chat, all from the
terminal.

## Mental model / core concepts

A component lives **in the agent's workspace**. It's synced like any other file
and runs **only in the agent's environment**. Tiles and dashboards live **in
cinna-core**. The CLI is a remote control for both and runs nothing locally.

- **Component**: a folder `dashboards/<name>/` in the agent's workspace (or
  `skills/<skill>/dashboards/<name>/` inside a skill). It holds a
  `component.json` (title, view, source, endpoints, params, refresh, size,
  `requires_credentials`, `context`), small endpoint scripts and, for an `html`
  view, a page. It's bundle-owned source: it ships with the agent, the team
  library and published bundles.
- **Component data**: what a schedule or the component's `refresh.command`
  prepares under `app-data/dashboards/<name>/`. Per user, never shipped, never
  carried by an import.
- **Tile**: one placement of a component on a dashboard. It refers to the
  component (agent + skill + name) and never copies it, so an edit to the
  component changes every tile that shows it. A tile keeps only the owner's
  choices: title, size, params and refresh overrides. Core keeps its cached
  data and its run budget.
- **Dashboard (board)**: the owner's page of tiles on the web, up to 20 blocks
  each and 10 dashboards per user. Cinna Desktop also creates hidden **backing
  dashboards** (`origin: desktop`) for its cloud tiles; those belong to the
  desktop's layout.
- **Reference**: the line an agent prints to offer a component,
  `cinna://component/bundle:<bundle_id>/<name>` (with `<skill>/` before the
  name for a skill's component). The owner's chat, on the web or in Cinna
  Desktop, turns that line into a card with Preview and Add to dashboard. Every
  cloud agent has a bundle id, so the CLI always prints the `bundle:` form.
- **Dashboard chat**: one conversation per dashboard and agent. Each message
  carries a `<dashboard_context>` (what the owner sees: the focused tile, its
  params and data, the other tiles). In that chat, and only there, the agent has
  the tile tools `dashboards_tiles` and `dashboards_update_tile`.
- **Webapp vs component vs artifact**: the webapp is the agent's own app and
  can be shared; a component is the owner's live tile; an artifact is a frozen
  copy for people. "Make me a dashboard" almost always means a component.

## User flows

### Build and check a component (assistant in a synced agent workspace)
1. The assistant reads the kit's dashboards guide and the `cinna-dashboards`
   skill. The per-agent `CLAUDE.md` links the account's copy under
   `context/local-kit/`.
2. It writes `workspace/dashboards/deals/component.json`, then the endpoints.
   Mutagen syncs them to the environment.
3. `cinna dashboards check . deals --run` pushes pending edits, reads the
   environment's own checker findings through core, runs the checker's run
   checks in the environment, and exits 1 on any error.
4. `cinna dashboards run . deals --param stage=won` prints the value exactly as
   a tile would receive it.

### Offer it to the owner
5. `cinna dashboards ref . deals --check` prints the reference line and
   confirms it resolves. The assistant prints that line, on its own and outside
   backticks, in its reply to the user. The owner gets a card.

### Place and drive tiles (when the user asks)
6. `cinna dashboards boards` lists the owner's web dashboards;
   `board create Sales` makes one.
7. `cinna dashboards add crm-agent deals --board Sales [--size l] [--param k=v]`
   adds the tile and prints its id and the dashboard link. `--new-board`
   creates the dashboard if no dashboard has that name. `--title`, `--max-age`,
   `--every` and `--after-turn/--no-after-turn` set the tile's title and
   refresh overrides. A component a skill ships is named with `--skill <skill>`
   on `show`, `check`, `run`, `ref` and `add`.
8. `cinna dashboards tiles Sales` shows each tile's params, size and data age
   without waking any environment. `tile data|refresh|params|call|edit|remove`
   reads or changes one tile. `tile edit` takes `--title` (`''` goes back to
   the component's title), `--size`, `--max-age`, `--every` and
   `--after-turn/--no-after-turn`.

### Test the agent in a dashboard chat
9. `cinna chat --agent crm-agent --board Sales --focus deals "Which deal first?"`
   opens (or continues) the agent's dashboard chat and sends the dashboard
   context. The transcript shows the agent's tile-tool calls. A tile whose
   stored params the turn changed is reported in a closing `tiles` event.
   `--new-chat` starts a fresh chat; `--context @file.json` sends a
   hand-written context instead.

### Import a kit agent that ships components
10. `cinna agent import <path>` lists the components that travel and warns
    about what won't work in the cloud, then ends with their references and
    the `add` hint.

## Business rules

- **The environment is the runtime.** Nothing runs an endpoint or the checker's
  run checks locally. `check --run` goes through `cinna exec`, `run` through
  core. The only local check is the kit's static checker during import.
- **Flush before reading.** `list`, `show`, `check`, `run` and `add` first push
  the agent's pending local edits when it's synced under this account
  workspace and its sync session exists, and they say so. They never create a
  session. `--no-flush` skips the push. A failed flush is a warning, and the
  read goes ahead. Reads ask core for a fresh listing (`fresh=true`) so a check
  right after a push isn't served from core's 10-second cache.
- **Server support first.** Every verb asks `GET dashboards/capabilities`
  before anything else. If the route is missing (the server predates
  dashboard components), the answer is one message with code
  `dashboards_unsupported`. Another version is refused up front too.
  `cinna chat --board` asks the same route but also needs its `chat` flag, and
  refuses with `dashboard_chat_unsupported` when the server has no dashboard
  chat. The
  limits it reports (runs a minute, blocks per dashboard, context size) are
  the ones the CLI enforces and quotes. A refusal by the api-proxy itself
  (its own per-token rate limit) is reported as exactly that, never as
  "no dashboards".
- **Waking environments.** `env_unavailable` is retried until `--timeout`
  (default 120 s; on `check` the same flag also bounds each `--run` check, and
  defaults to 300 s), waiting `retry_after_s` (default 5 s) between tries. Listing components or
  running one wakes a suspended environment. Reading a tile's data never does.
  `cinna agent show` lists components only when the server has dashboards
  and the environment is already running. `cinna doctor` doesn't check
  dashboards at all, because it would wake every environment.
- **Refusals keep core's sentence** and add the CLI's next step:
  - `adapter_unsupported` → `cinna agent rebuild-env <agent>`.
  - `needs_credentials` → link them (`cinna agent show`). A name is the name of
    a credential linked to the agent; for a bundle install it's the bundle's
    credential spec name.
  - `rate_limited` → try again in N s.
  - `not_found` → `cinna dashboards list <agent>`.
  - `invalid_input` → names the field.
  - `call_failed` → core's sentence, which carries the script's stderr tail.
  - Exit codes: `invalid_input` 2, `env_unavailable` 12, everything else 1.
- **The CLI's own refusals** (exit 1, with the remedy in the message):
  - `component_errors`: `check` found an error, or `add` was asked to place a
    component with errors; the JSON lists the components.
  - `dashboard_full`: `add` on a dashboard that already holds the block limit.
  - `not_synced`: `check --run` for an agent with no synced workspace under
    this account (the checker runs with that workspace's token).
  - `ref_<status>` (e.g. `ref_gone`, `ref_invalid`): `ref --check` got a status
    other than `ok`.
  - `desktop_dashboard`: see Desktop dashboards below.
- **Own run budget.** Core allows 30 runs a minute per target. CLI runs carry
  `tile_key: "cli"`, so a developer looping `run` doesn't use up the budget of
  core's Preview dialog. The CLI never auto-loops past a `rate_limited`.
- **Agents offer, people place.** The assistant's default is to print the
  reference. `add` and the tile verbs are for people and explicit requests, and
  they aren't person-only: a tile is private to its owner.
- **A component with errors can't be placed.** `add` refuses first and lists
  the errors (core would refuse too). It also refuses a dashboard that already
  holds the block limit.
- **Desktop dashboards are read-only here.** Tiles on an `origin: desktop`
  dashboard can be listed and read (`tiles`, `tile data`), but `tile refresh`,
  `call`, `params`, `edit` and `remove`, and `add`, refuse with code
  `desktop_dashboard`. `boards` and name lookup see web dashboards only; a
  desktop dashboard is reachable by its full id. Placements on one show as
  "Cinna Desktop".
- **Placement matches core.** A new tile goes at the top-left (`0,0`) and the
  grid shifts the others down, as core's own Add dialog does. The size grid is
  core's: `s` 3×2, `m` 6×2, `l` 6×4, `wide` 12×2, `tall` 3×4. The default is
  the component's `size`, else `s`.
- **`add --new-board` creates only when nothing matches.** An ambiguous
  name or a failed lookup stays an error; it never creates a duplicate.
- **`chat --board --resume` must resume that dashboard's chat.** Core would
  silently drop the context on any other session, and the tile tools would
  answer `no_dashboard`, so the CLI refuses.
- **The per-agent guide follows the environment.** `CLAUDE.md` §7b carries
  the agent's own reference prefix and the path to the account's kit copy of
  the `cinna-dashboards` skill, taken from the platform skills the building
  context reports. If the environment doesn't list that skill, §7b says to
  rebuild it.
- **Tile params are a whole set.** `tile params --set/--clear/--reset` merges
  locally and sends the complete set. Core replaces it, and omitted keys go
  back to their defaults.
- **No frame URLs.** The CLI never calls either `…/frame` route: a frame URL is
  a 12-hour credential and a terminal has no use for one.
- **Dashboard chat context** follows the web panel's shape. It's capped at
  12,000 characters by dropping other agents' tiles first, then the agent's
  other tiles, then cutting the summary. It's resent on every message (core
  dedupes by hash). `--board` takes web dashboards only: a desktop dashboard's
  chat runs in Cinna Desktop over A2A.
- **Import warns, never refuses.** It warns when the kit's static checker
  reports errors (the checker comes from the account workspace's own
  `context/local-kit/`, the kit the user's server shipped; a kit copy
  inside the imported folder is never run, because that folder may be a
  download), when a source reads `app-data/` (that data doesn't travel),
  when a folder holds `package.json`/`node_modules/`, when `refresh.on_app_start`
  is set (the cloud ignores it), and when `requires_credentials` names a
  credential the manifest doesn't declare.

## Architecture overview

```
cinna dashboards <verb> → dashboards.py → AccountClient._dashboards_json → account api-proxy
                                                         → core: agents/{id}/dashboards/* · dashboards/* · …/component/*
                                                         → agent env (env-core /dashboards/*, the checker)
cinna dashboards check --run → dashboards.run_env_checker → cinna exec (per-agent token) → env's check_component.py
cinna chat --board → chat.run_chat → dashboards.prepare_dashboard_chat → POST dashboards/{id}/chat
                                   → POST sessions/{id}/messages/stream {dashboard_context} → poll → "tiles" event
cinna agent import → local_import._report_components → dashboards.survey_local_components → kit check_component.py (static)
cinna agent sync / account refresh-context → GET agents/{id} → .cinna/config.json bundle_id → CLAUDE.md §7b
cinna agent sync → building context (platform_skills) → .cinna/config.json platform_skills → CLAUDE.md §7b skill path
```

## Integration points

- [Account workspace](../account_workspace/account_workspace.md): the
  orchestrator `CLAUDE.md` has the Dashboards command table and the
  webapp / component / artifact advice. `refresh-context` re-renders every
  per-agent guide and backfills its bundle id.
- [Agent management](../agent_management/agent_management.md): `cinna agent sync`
  records the bundle id. `cinna agent show` has a Dashboard components section.
  `cinna agent rebuild-env` is the remedy for an environment that predates
  components.
- [Live sync](../live_sync/live_sync.md): components sync like any workspace
  file, and the dashboards verbs reuse its flush.
- [Remote exec](../remote_exec/remote_exec.md): the channel `check --run` runs
  the checker through.
- [Remote chat](../remote_chat/remote_chat.md): `cinna chat --board` is a
  dashboard chat on top of the same poll loop.
- [Local agent import](../local_agent_import/local_agent_import.md): components
  travel with an import, which reports them.
- [Artifacts](../artifacts/artifacts.md): the frozen-copy alternative. Its
  starter template is `artifact-sqlite-report/`.
