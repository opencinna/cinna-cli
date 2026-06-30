# Agent Management — Acceptance Scenarios (live e2e)

The catalog of **real-usage scenarios** for an agent doing *integration* testing
of the `cinna agent` lifecycle verbs against a **live** environment — a real
platform backend, a real account workspace, and real agent containers. These are
not unit tests; they exist to catch what unit tests miss: token mint/revoke
provenance, registry drift across sync/unsync, restart-clobbers-unsynced-edits,
and "did my edit actually go live" inspection gaps.

How to use: pick the scenarios relevant to the change, run the **Steps** verbatim
from inside the account workspace, assert the **Expected**, and watch for the
**Watch for** failure modes. Run the full create → sync → inspect → restart →
unsync arc (1 → 9) on any change to minting, the registry, or the bootstrap
provisioning path.

The `cinna agent schedule …` subgroup is covered by its own acceptance doc — do
not exercise schedules here beyond confirming `cinna agent schedule --help` lists.

## Preconditions

- A reachable platform (e.g. `http://localhost:8000` backend, `:5173` frontend)
  with an **account workspace** already set up: a `.cinna/account.json` holding a
  valid `cli-account` token. Confirm with `cinna account status` (token `valid`)
  and `cinna account agents` (lists the agents you own).
- **Editable install** of the CLI under test:
  `python3 -c "import cinna,os;print(os.path.dirname(cinna.__file__))"` must point
  at this repo's `src/cinna`. Confirm `which cinna` resolves and
  `cinna agent --help` lists `sync / unsync / create / restart-env / show /
  status` (and `schedule`).
- Run every command from the account root (or a nested folder under it). `git`
  and `mutagen` on `PATH` (for the sync/restart scenarios).
- At least one agent you own; the create scenario provisions a throwaway one.

## Scenario catalog

### 1. Create an agent (thin client, backend defaults)

- **Goal:** create a fresh agent from the CLI with no UI round-trip.
- **Steps:**
  ```
  cinna agent create "ACC Test Agent" --description "e2e throwaway"
  ```
- **Expected:** output reports `Agent created: ACC Test Agent`, an `Agent ID:`, a
  `Web UI:` link (`<frontend>/agent/<id>`), the target workspace, and a
  `cinna agent sync acc-test-agent` hint. The agent appears in
  `cinna account agents` with a provisioned environment/template (backend
  defaults applied).
- **Watch for:** the CLI sending more than name/description (it must be a thin
  client); the created agent missing AI credentials / env template (defaults not
  applied backend-side); the printed slug not matching the real normalized name.

### 2. Sync produces a setup-identical workspace + auto-link

- **Goal:** attaching an agent yields a normal, immediately-usable workspace.
- **Steps:**
  ```
  cinna agent sync "ACC Test Agent"
  ls agents/acc-test-agent/
  test -f agents/acc-test-agent/.cinna/config.json && echo CONFIG_OK
  cinna exec --agent acc-test-agent echo hello-from-env
  ```
- **Expected:** the four provisioning steps print; the workspace lands under
  `agents/<slug>/` with `.cinna/config.json` (a child token), `workspace/`,
  `mutagen.yml`, `CLAUDE.md`, `.mcp.json`; the final hint shows `cd
  agents/<slug>/`, `cinna dev`, and `cinna exec --agent <slug>`. `cinna exec`
  prints `hello-from-env`. A registry entry exists in `~/.cinna/agents.json`. If
  the agent is git-versioned, output also reports `Git-versioned: linked …` (see
  [Git Versioning](../git_versioning/git_versioning.md)).
- **Watch for:** a workspace that differs from a `cinna setup` one (missing
  generated files / mutagen.yml); the child token not minted (exec 401s); the git
  auto-link skipped for a versioned agent.

### 3. Re-sync of the same agent is refused (no duplicate / re-clone)

- **Goal:** a second `agent sync` of an already-synced agent doesn't clobber.
- **Steps:** `cinna agent sync "ACC Test Agent"` again.
- **Expected:** it aborts with `'agents/acc-test-agent/' is already a synced
  workspace.` and a hint to `cinna agent unsync` or `cinna set-token`. No mint
  call is made; the existing workspace and registry entry are untouched.
- **Watch for:** a silent re-clone overwriting local edits; a duplicate registry
  entry; a second mint issuing a redundant token.

### 4. Unsync tears down but preserves user files

- **Goal:** detaching cleans up local + server state without losing work.
- **Setup:** add a user file: `printf 'mine\n' > agents/acc-test-agent/workspace/scripts/mine.py`.
- **Steps:**
  ```
  cinna agent unsync acc-test-agent      # confirm at the prompt
  ls agents/acc-test-agent/workspace/scripts/mine.py
  test -e agents/acc-test-agent/.cinna && echo STILL_THERE || echo CINNA_GONE
  ```
- **Expected:** the command stops sync, reports `Child token revoked on the
  platform.`, removes `.cinna/` + generated files, drops the registry entry, and
  prints `Unsynced … Workspace files preserved`. `mine.py` still exists;
  `.cinna/` is gone (`CINNA_GONE`); the agent no longer appears as synced in
  `cinna account agents`. A subsequent `cinna exec --agent acc-test-agent`
  fails (no token).
- **Watch for:** user files deleted; the registry entry left behind (drift); the
  child token still valid server-side after revoke.

### 5. Unsync degrades gracefully when the revoke fails

- **Goal:** a server-side revoke failure never blocks local teardown.
- **Setup:** unsync with the backend unreachable or the token id already gone
  (e.g. a workspace predating provenance tracking — no `cli_token_id`).
- **Steps:** `cinna agent unsync <slug>`.
- **Expected:** a warning (`Server-side token revoke failed …` / `No stored token
  id … skipping server-side revoke`) plus guidance, but `.cinna/` + the registry
  entry are still removed and user files preserved.
- **Watch for:** the teardown aborting on revoke failure, leaving a half-synced
  workspace and a registry entry pointing at a deleted `.cinna/`.

### 6. `agent show` reflects a live prompt edit

- **Goal:** confirm "is what I edited actually live?" without the browser.
- **Setup:** with the agent synced, edit a workflow prompt file under
  `agents/<slug>/workspace/docs/` and let Mutagen sync it (or `cinna sync push
  --agent <slug>`).
- **Steps:**
  ```
  cinna agent show acc-test-agent --prompts
  cinna agent show acc-test-agent              # full: prompts + features + creds
  cinna agent show acc-test-agent --full | head -50
  ```
- **Expected:** `--prompts` prints the `entrypoint` / `workflow` / `refiner`
  blocks the runtime reads, with the edit reflected in `workflow`. The full form
  adds `Features:` and `Connected credentials (N):` listing name + type only (no
  secret values). Long prompts truncate in the TTY but `--full` (and piping)
  print them whole.
- **Watch for:** secret values leaking into the credential list; truncation
  silently swallowing content when output is redirected (must auto-full off a
  TTY); the edited prompt not reflected (prompt-sync not yet applied).

### 7. `agent status` shows, refreshes (waking the env), and never crashes

- **Goal:** read and force-refresh the agent's self-reported status.
- **Steps:**
  ```
  cinna agent status show acc-test-agent
  cinna agent status refresh acc-test-agent
  ```
- **Expected:** `show` prints the cached snapshot — severity (color-coded),
  summary, reported/fetched age, the configured refresh command, and the
  `STATUS.md` body if published (or `no STATUS.md published`). `refresh` wakes a
  suspended env, re-runs the pre-command, and re-reads `STATUS.md`; even if the
  env is unreachable or the pre-command errors it returns the cached snapshot
  (possibly with a refresh-command warning) — it never exits non-zero.
- **Watch for:** `refresh` raising / non-zero exit on a wedged env (must
  cache-fall-back); a stale snapshot presented as fresh.

### 8. `agent status set-command` configures the refresh pre-command

- **Goal:** change and clear the status-refresh pre-command.
- **Steps:**
  ```
  cinna agent status set-command acc-test-agent "/run:status"
  cinna agent status show acc-test-agent          # Refresh cmd: /run:status
  cinna agent status set-command acc-test-agent "python scripts/status.py"
  cinna agent status set-command acc-test-agent ""   # opt out
  cinna agent status show acc-test-agent          # Refresh cmd: (none)
  ```
- **Expected:** each set echoes the stored value; `show` reflects it; the empty
  string is a deliberate opt-out rendering `(none)`.
- **Watch for:** the empty-string opt-out being rejected or coerced to the
  default; a `/run:<name>` reference mangled in transit.

### 9. `agent restart-env` recovers a wedged env, guarding local edits

- **Goal:** bounce a stuck container; don't silently clobber unsynced work.
- **Steps (clean case):**
  ```
  cinna agent restart-env acc-test-agent
  ```
  **Expected:** spinner `Restarting environment …`, then `Environment restarted`
  with a `Status:` (and `Message:` if any). Blocks until the container is back.
- **Steps (dirty case):** make a local edit but do **not** push it, then
  `cinna agent restart-env acc-test-agent`.
  **Expected:** it warns `This machine has N unsynced local change(s) …` and a
  `cinna sync push` hint, and asks `Restart anyway?` (default No). Declining
  aborts **without** restarting; accepting proceeds.
- **Watch for:** the restart firing without the warning while unsynced
  edits/conflicts exist; the warning firing when there's nothing pending;
  aborting still calling restart.

### 10. `AGENT_REF` resolution: id, slug, ambiguous, unknown

- **Goal:** every verb resolves the agent reference uniformly and fail-loud.
- **Steps:**
  ```
  cinna agent show <agent-uuid>          # by id
  cinna agent show acc-test-agent        # by slug
  cinna agent show "ACC Test Agent"      # by exact name
  cinna agent show no-such-agent         # unknown
  ```
- **Expected:** the first three resolve to the same agent; the unknown ref fails
  with `No accessible agent matches 'no-such-agent'` and lists available agents.
  Two agents whose names slug the same fail with an ambiguity error demanding the
  id.
- **Watch for:** a wrong-agent match; an unknown ref silently picking the first
  agent; an ambiguous slug resolving instead of erroring.

## Cross-cutting invariants (must hold across all scenarios)

- **Account-token only.** Every `cinna agent` lifecycle verb runs against
  `/api/v1/cli/account/*` with the account token; none uses or requires a
  per-agent token to *invoke* (sync mints one as output). Run outside an account
  workspace → fail fast.
- **No secret ever printed.** `agent show` / `agent status` surface credential
  name+type and a `STATUS.md` snapshot only — never secret values, never the
  child JWT.
- **No state drift between commands.** `agent sync` adds exactly one registry +
  `.cinna/` pair; `agent unsync` removes exactly that pair; neither orphans a
  registry entry nor a dangling `.cinna/`. A failed unsync revoke still reconciles
  local state.
- **No silent clobber.** Re-sync of a synced agent is refused; restart-env warns
  before overwriting unsynced edits; user workspace files survive unsync.
- **Fail-loud `AGENT_REF`.** Unknown → lists agents; ambiguous → demands the id;
  never a silent wrong-agent action.

## Cleanup

- `cinna agent unsync acc-test-agent` to drop the local checkout (preserves
  files; deletes `.cinna/` + registry entry).
- Remove the throwaway agent created in scenario 1 from the platform UI (or via
  `cinna api DELETE …` if available) so it doesn't linger in `cinna account
  agents`.
- Reset any status pre-command you changed back to the platform default:
  `cinna agent status set-command <slug> "/run:status"`.
- Verify no leftover entries in `~/.cinna/agents.json` for the test agent —
  especially if any step ran **outside** pytest's global-state isolation (it
  writes the real registry).
