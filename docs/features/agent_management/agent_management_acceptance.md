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
  `cinna agent --help` lists `sync / unsync / create / restart-env / rebuild-env /
  show / prompts / model / scenarios / status` (and `schedule`).
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
  blocks the runtime reads, each under its `[label]`, with the edit reflected in
  `workflow`. The full form adds `Features:` and `Connected credentials (N):`
  listing name, type, slot, setup state and id (no secret values — see #11).
  Long prompts truncate in the TTY but `--full` (and piping) print them whole.
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

### 9a. `agent rebuild-env` adds routes a restart cannot

- **Goal:** recover a container built *before* a feature existed — the case a
  restart provably cannot fix, because it re-runs the same image.
- **Steps (clean case):**
  ```
  cinna skills list acc-test-agent          # reports adapter_unsupported
  cinna agent rebuild-env acc-test-agent
  ```
  **Expected:** `skills list` names `cinna agent rebuild-env acc-test-agent` —
  **not** a refresh and **not** a restart. The rebuild asks `Rebuild …? This
  recreates the container and takes a few minutes.` (default No), then blocks
  with a spinner and prints `Environment rebuilt for …` + `Status:`. Re-running
  `cinna skills list` no longer reports `adapter_unsupported`.
- **Steps (was-stopped case):** rebuild an environment that was not running.
  **Expected:** success, and a note that it *was not running before the rebuild,
  so it was left stopped* — a rebuild restores the state it found.
- **Steps (dirty case):** make a local edit but do **not** push it, then
  `cinna agent rebuild-env acc-test-agent --yes`.
  **Expected:** `--yes` skips the "takes minutes" confirmation but the unsynced-
  changes guard **still** warns and asks `Rebuild anyway?`. `--yes` is not a
  general non-interactive switch; under `--no-input` the guard takes its No
  default and the rebuild aborts.
- **Watch for:** `--yes` skipping the D2 guard; the "left stopped" note printed
  for an environment that came back running (or missing when it did not); the
  command returning before the rebuild finishes (it must block — the client
  allows 1800 s for exactly this).

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

### 11. `agent show` names every prompt and every credential's slot

- **Goal:** tell the three prompt blocks apart, and see which slot each linked
  credential fills.
- **Setup:** an agent with a linked credential whose service URI is set
  (`cinna account credentials update <id> --service-uri some-token.com`).
- **Steps:**
  ```
  cinna agent show acc-test-agent --prompts | grep -E '^\s+\[(entrypoint|workflow|refiner)\]'
  cinna agent show acc-test-agent | sed -n '/Connected credentials/,$p'
  cinna account credentials list | grep some-token.com
  ```
- **Expected:** three label lines, one per prompt (`[refiner] (empty)` when unset).
  The credential line reads `<name> (api_token)  slot: some-token.com  <id>`, and
  the slot agrees with `credentials list`.
- **Watch for:** unlabelled prompt blocks (Rich swallowing `[label]`); `no slot`
  for a credential whose slot is set — the agent credential route returns
  `service_uri: null`, so the slot must come from the account listing.

### 12. `agent rebuild-env` waits until the environment answers

- **Goal:** a chat sent right after a rebuild gets a reply.
- **Steps:**
  ```
  cinna agent rebuild-env acc-test-agent --yes
  cinna chat --agent acc-test-agent "ping" | jq -c 'select(.event=="done")'
  ```
- **Expected:** after `Environment rebuilt …` a spinner `Waiting for the
  environment to answer…`, then `The environment answers its health check — ready
  to chat.` The chat that follows ends `outcome: completed`. If the container never
  answers within 180 s: a warning naming `cinna agent status refresh`, exit 0.
- **Watch for:** "ready" printed before the server inside answers; a chat right
  after the rebuild hanging with `is_streaming: true`; the wait running for an
  environment that was left stopped.

### 13. `agent prompts pull | diff | push` round-trips one edit

- **Goal:** change one line of the workflow prompt without raw API calls, and
  without reverting a change someone made in the UI meanwhile.
- **Steps:**
  ```
  cinna agent prompts pull acc-test-agent
  sed -i.bak 's/$/ Use `report.py`./' prompts/acc-test-agent/refiner.md
  cinna agent prompts diff acc-test-agent
  # meanwhile, edit the agent's router trigger in the web UI
  cinna agent prompts push acc-test-agent --dry-run
  cinna agent prompts push acc-test-agent
  cinna agent show acc-test-agent --prompts | sed -n '/\[refiner\]/,$p'
  cinna exec --agent acc-test-agent cat /app/workspace/docs/REFINER_PROMPT.md
  cinna agent prompts pull acc-test-agent       # refused? only if unpushed edits remain
  ```
- **Expected:** `pull` lists six files; `diff` shows the refiner edit with the
  backticks intact and marks `router_trigger.md` as changed on the platform (push
  leaves it alone). `--dry-run` names only `refiner.md`. `push` prints `Pushed
  refiner.md`, then that the running environment's docs carry it; `agent show` and
  the env's doc file both show the edit; the UI's router trigger change survives.
  Editing the same field in the UI *and* locally makes `push` refuse until
  `--force` or `pull --force`.
- **Watch for:** backticks mangled; a push sending every field (reverting the UI
  change); `pull` overwriting unpushed edits without `--force`; the env's
  `docs/*.md` not updated while the env is running.

### 14. `agent model show | set` changes one field and nothing else

- **Goal:** switch the conversation model without touching the building model or
  the AI credential pins, and without a hand-built `reconfigure` body.
- **Setup:** an agent you can build, with a running environment. Note its
  `active_environment_id` (`cinna api GET agents/<id>`), and one foreign install
  from `cinna account agents` for the refusal step.
- **Steps:**
  ```
  cinna agent model show acc-test-agent
  KEEP='{model_override_building, use_default_ai_credentials, conversation_ai_credential_id, building_ai_credential_id}'
  cinna api GET environments/<env_id> | jq "$KEEP" > /tmp/model-before.json
  cinna agent model set acc-test-agent --building "$(cinna agent model show acc-test-agent --json | jq -r '.building.override // "default"')"
  cinna agent model set acc-test-agent --conversation <a-valid-small-model> --yes
  cinna api GET environments/<env_id> | jq "$KEEP" > /tmp/model-after.json
  diff /tmp/model-before.json /tmp/model-after.json
  cinna agent model set acc-test-agent --conversation haikuu --no-rebuild
  cinna agent model set acc-test-agent --conversation default --yes
  cinna agent model set "<foreign install>" --conversation haiku --yes; echo "exit=$?"
  ```
- **Expected:** `show` lists both modes with SDK, override (`—` when none),
  effective model and health. The unchanged set prints "nothing changed" with no
  prompt and no rebuild. The real set prints `Stored for …: conversation model
  default → <model>`, the rebuild status and "ready to chat", then a table whose
  conversation row carries the new override and effective model; the `diff` is
  empty. `haikuu` is stored, names `cinna agent rebuild-env acc-test-agent`, and a
  health of `unknown_model` comes with "usually a typo in the model id". `default`
  clears it again. The foreign install is refused with "This is an installed
  bundle…", `exit=1`, and nothing is written.
- **Watch for:** the building override or a credential pin reset by the set (a
  partial reconfigure body); the set failing after about 30 s (a rebuild requested
  through the escape hatch); an unchanged set that still prompts or rebuilds; an
  unknown model id accepted with no hint.

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
- **No silent clobber.** Re-sync of a synced agent is refused; restart-env and
  rebuild-env both warn before overwriting unsynced edits; user workspace files
  survive unsync.
- **One code, one remedy.** Every surface that reports a skill-index error
  (`cinna skills list`, `cinna skills refresh`) names the fix *that code* has —
  `restart-env` for `adapter_error`, `rebuild-env` for `adapter_unsupported`,
  wake-it for `env_not_running`, `skills refresh` for `parse_error` — and names
  no verb at all for a code it does not recognise. Never a single hardcoded
  remedy printed for all of them.
- **Fail-loud `AGENT_REF`.** Unknown → lists agents; ambiguous → demands the id;
  never a silent wrong-agent action.
- **No silent reset of environment settings.** `agent model set` changes only the
  fields it was asked to; an unchanged set neither writes nor rebuilds; nothing
  rebuilds through the escape hatch.

## Cleanup

- Put the conversation model back to what scenario 14 found
  (`cinna agent model set acc-test-agent --conversation <original|default> --yes`)
  and `rm -f /tmp/model-before.json /tmp/model-after.json`.
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
