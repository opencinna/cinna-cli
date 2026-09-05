# Bootstrap & Onboarding — Acceptance Scenarios (live e2e)

The catalog of **real-usage scenarios** an agent runs against a **live**
environment — a real platform backend, a real agent container, and a real Mutagen
daemon — to exercise onboarding end-to-end. These are not unit tests; they exist
to catch what unit tests miss: token-store drift between `.cinna/config.json` and
`~/.cinna/agents.json`, registry rows that outlive their folders, layout
collisions across two checkouts, destructive teardown deleting the wrong thing,
and `setup` clobbering an existing workspace.

How to use: pick the scenarios relevant to the change, run the **Steps** verbatim,
assert the **Expected**, and watch for the **Watch for** failure modes.

## Preconditions

- A reachable platform (e.g. `http://localhost:8000` backend, `:5173` frontend)
  where you can click **Local Development** on an agent to mint a setup token, and
  ideally an account workspace already set up for the `login` / account paths.
- **Editable install** of the CLI under test:
  `python3 -c "import cinna,os;print(os.path.dirname(cinna.__file__))"` must point
  at this repo's `src/cinna`. Confirm `which cinna` resolves and `cinna --help`
  lists `setup`, `set-token`, `login`, `list`, `status`, `disconnect`,
  `disconnect-all`, `completion`, `dev`, `redev`, and `cinna --help` shows the
  root `--no-input` option.
- **Two agents** available to check out — ideally a pair whose names normalize to
  the **same slug** (for the collision scenario).
- `mutagen` on `PATH`.
- A scratch parent directory to run checkouts in (so `disconnect-all` is safe).

> The registry these scenarios inspect lives at `~/.cinna/agents.json` (0600). Back
> it up before destructive runs if you have real agents registered, since
> `disconnect-all` and `disconnect` mutate it.

## Scenario catalog

### 1. Fresh setup connects an agent end-to-end

- **Goal:** a developer goes from a setup link to a live-synced workspace.
- **Setup:** mint a setup token in the UI; cd into an empty scratch dir.
- **Steps:**
  ```
  cinna setup 'http://localhost:8000/api/cli-setup/<TOKEN>'
  # Ctrl-C once the foreground sync TUI attaches
  cinna status
  ```
- **Expected:** the 5 steps print (Authenticating → Mutagen → Cloning → Configuring
  → Starting sync). A `<slug>/<subdir>/` tree exists holding `.cinna/config.json`,
  `workspace/`, `CLAUDE.md`, `BUILDING_AGENT.md`, `.mcp.json`, `opencode.json`,
  `mutagen.yml`, `.gitignore`. `cinna status` shows the agent, a `connected`/`watching`
  sync state, and a **valid token**. `~/.cinna/agents.json` has one row for the agent
  with this workspace path.
- **Watch for:** setup succeeding but writing no registry row; the workspace landing
  flat instead of nested; sync state stuck `error`.

### 2. Setup accepts all three input forms

- **Goal:** the paste-from-UI ergonomics work.
- **Steps:** in three separate empty dirs, run setup with (a) the full
  `curl -sL …/cli-setup/<TOKEN> | python3 -` string, (b) the bare URL, (c) the raw
  token with `CINNA_PLATFORM_URL` exported. (Each needs its own fresh token —
  setup tokens are single-use.)
- **Expected:** all three resolve the same `(platform_url, token)` and complete.
- **Watch for:** the curl form failing to parse out the URL; the raw-token form not
  honoring `CINNA_PLATFORM_URL`.

### 3. Setup refuses to clobber an existing workspace

- **Goal:** re-running setup in a populated dir never overwrites.
- **Steps:** in the dir from scenario 1, mint a new token and run `cinna setup
  '<url>'` again.
- **Expected:** aborts with "already contains a cinna workspace" and points at
  `cinna disconnect`. No files changed; the registry row is untouched.
- **Watch for:** setup re-downloading and wiping local edits; a duplicate registry
  row.

### 4. set-token refreshes the CLI token in place

- **Goal:** an expired token is fixed without re-cloning.
- **Setup:** note the current `cli_token` in `.cinna/config.json` and the registry
  row; mint a fresh setup token for the *same* agent.
- **Steps:**
  ```
  cinna set-token 'http://localhost:8000/api/cli-setup/<NEW_TOKEN>'
  ```
- **Expected:** "Token refreshed for agent: <name>". The `cli_token` in
  `.cinna/config.json` **and** in `~/.cinna/agents.json` both change to the new
  value; everything else (workspace files, `CLAUDE.md`, `BUILDING_AGENT.md`,
  `.mcp.json`, the git block in the registry) is byte-identical. `cinna status`
  reports a **valid token**.
- **Watch for:** only one of the two stores updated (drift); the tarball
  re-downloaded; guides regenerated; the registry git block dropped.

### 5. set-token rejects a different agent's token

- **Goal:** a workspace can't be silently rebound to another agent.
- **Steps:** from agent A's dir, run `cinna set-token` with a setup token minted
  for agent **B**.
- **Expected:** aborts with "Token belongs to a different agent … Run 'cinna setup'
  in a new directory". A's `.cinna/config.json` and registry row are unchanged.
- **Watch for:** the token being written anyway; A's `agent_id` flipping to B.

### 6. list reflects the whole registry, with live token + sync state

- **Goal:** the developer sees every registered agent at a glance.
- **Setup:** at least two agents checked out (use scenario 8's second checkout).
- **Steps:** `cinna list`
- **Expected:** one row per registry entry with name+id, the web-UI `/agent/<id>`
  link, the workspace path, a Mutagen sync cell (`active` / `connecting` / `paused`
  / `error` / `–`), and a token cell (`valid token` / `expired token` /
  `no connection`). Stopping the daemon then re-running shows `–` for sync but the
  token probe still runs.
- **Watch for:** the token probe hammering the backend serially (it should be
  parallel and bounded); a row crashing the table when its workspace config can't
  be loaded.

### 7. list flags a missing workspace

- **Goal:** registry rows that outlive their folder are surfaced, not hidden.
- **Steps:** `mv` an agent's checkout dir aside (or delete it), then `cinna list`.
- **Expected:** that row shows `missing: <path>`; the command nudges toward
  `cinna doctor` to clean up. Other rows are unaffected.
- **Watch for:** `list` raising on the missing path; the missing row silently
  dropped instead of flagged.

### 8. Slug collision bumps the clone-root name

- **Goal:** two agents whose names slugify the same don't collide on disk.
- **Setup:** two different agents whose names normalize to the same slug.
- **Steps:** from one scratch parent dir, `cinna setup` (or `cinna agent sync`)
  both, Ctrl-C after each attaches.
- **Expected:** the first lands at `<slug>/<subdir>/`; the second at
  `<slug>-<shorthash>/<subdir>/`. Two distinct registry rows, two distinct workspace
  paths. Re-running setup for either *same* agent reports "already set up" rather
  than making a third dir.
- **Watch for:** the second checkout overwriting the first; both rows sharing one
  path; a same-agent re-sync creating a duplicate.

### 9. status works in both a per-agent and an account workspace

- **Goal:** `status` never errors just because the folder is account-level.
- **Steps:** run `cinna status` inside an agent dir, then inside an account
  workspace (the dir holding `.cinna/account.json`).
- **Expected:** the agent dir shows the agent table (platform, id, template,
  Mutagen, sync state, token). The account dir shows the account session view
  (same as `cinna account status`), not a "not a workspace" error.
- **Watch for:** the account fallback raising `ConfigNotFoundError`; the per-agent
  view showing in an account dir.

### 10. dev resumes a session; redev favors remote on startup conflicts

- **Goal:** picking an agent back up works, including after platform-side edits.
- **Steps:**
  ```
  # in agent dir, no sync currently running:
  cinna dev          # attaches TUI; Ctrl-C to stop
  # now edit a workspace file from the PLATFORM side, leave local idle, then:
  cinna redev        # attaches; resolves startup conflicts in favor of remote
  ```
- **Expected:** `dev` (re)creates the Mutagen session and attaches; Ctrl-C stops it
  with nothing left in the daemon (`cinna list` shows `–`). `redev` reports
  "Resolved N conflict(s) in favor of remote" and backs displaced local versions up
  under `.cinna/sync/redev-backup/<timestamp>/`.
- **Watch for:** sync outliving the process after Ctrl-C; `redev` overwriting local
  files **without** a backup; `redev` auto-resolving conflicts that arise *after*
  startup (only startup conflicts should be auto-resolved).

### 11. disconnect stops sync and keeps workspace files

- **Goal:** the non-destructive teardown preserves the user's work.
- **Steps:**
  ```
  ls workspace/                 # note user files
  cinna disconnect              # confirm the prompt
  ls                            # inspect what remains
  ```
- **Expected:** sync stops, the registry row is gone (`cinna list` no longer shows
  it), `.cinna/` and the generated files (`CLAUDE.md`, `BUILDING_AGENT.md`,
  `CHAT_TESTING.md`, `GIT_VERSIONING.md`, `.mcp.json`, `opencode.json`, `mutagen.yml`,
  synced prompt-ref guides) are deleted — but **`workspace/` and its files remain**.
- **Watch for:** `workspace/` being deleted; a leftover registry row; a synced
  prompt-ref guide left orphaned.

### 12. disconnect-all deletes every checkout under the current dir

- **Goal:** the bulk, destructive cleanup removes whole directories safely.
- **Setup:** a scratch parent dir holding two checkouts (scenario 8).
- **Steps:**
  ```
  cinna disconnect-all          # review the table, type the confirmation
  cinna list
  ```
- **Expected:** a table lists both workspaces; after confirming, each sync session
  is stopped, each registry row removed, and each top-level directory deleted
  entirely. `cinna list` shows neither agent.
- **Watch for:** a nested-layout agent not detected (config one level down);
  deleting a sibling dir that is **not** a cinna workspace; registry rows surviving
  the directory deletion.

### 13. completion installs idempotently

- **Goal:** shell completion wires in and doesn't duplicate on re-run.
- **Steps:**
  ```
  cinna completion zsh | head            # raw script to stdout
  cinna completion --install             # auto-detect + append to rc
  cinna completion --install             # run again
  ```
- **Expected:** the first `--install` appends the activation snippet to the shell rc
  and reports the file; the second reports "already installed" and appends nothing.
  Bare `cinna completion` prints guidance, not the raw script.
- **Watch for:** a second `--install` duplicating the block; the zsh snippet missing
  the `compinit` guard (sourcing then fails with "command not found: compdef").

### 14. No terminal: `--no-input` never hangs, exit codes are stable

- **Goal:** a program (Cinna Desktop, CI) can drive any command without a TTY.
- **Setup:** an agent dir from scenario 1; an empty scratch dir.
- **Steps:**
  ```
  cd <empty-dir>; cinna --no-input login < /dev/null; echo "exit $?"
  CINNA_NO_INPUT=1 cinna login < /dev/null; echo "exit $?"
  cd <agent-dir>; cinna --no-input disconnect < /dev/null; echo "exit $?"; ls .cinna/config.json
  cinna --no-input setup '<fresh URL>' < /dev/null      # machine name defaults, no prompt
  ```
- **Expected:** the two `login` runs exit `1` immediately with `Error: Input
  required but --no-input is set: Platform domain…` (no hang, no partial
  workspace). `disconnect` prints "Aborted!" and exits `1` with `.cinna/`
  intact (the "Continue?" default is No). `setup` runs through with the
  default machine name (`<user>'s <host>`).
- **Watch for:** any command blocking on stdin (run each under `timeout 30`);
  a prompt reaching `/dev/tty`; `disconnect` proceeding as if confirmed.

### 15. `CINNA_MUTAGEN_BIN` selects the Mutagen binary

- **Goal:** a privately shipped Mutagen is used for everything, PATH ignored.
- **Setup:** a second Mutagen build at `/opt/cinna/mutagen` (or a copy of the
  PATH one); optionally rename the PATH one away.
- **Steps:**
  ```
  cd <agent-dir>
  CINNA_MUTAGEN_BIN=/opt/cinna/mutagen cinna dev        # Ctrl-C after it attaches
  CINNA_MUTAGEN_BIN=/nonexistent cinna --no-input dev < /dev/null; echo "exit $?"
  ```
- **Expected:** the first run starts sync through the override (with PATH's
  `mutagen` removed it still works; `.cinna/config.json` records that binary's
  version). The second exits `1` with `Error: Mutagen is required but was not
  found on PATH. (required version: …)` — a structured `mutagen_missing`,
  not a wait for Enter.
- **Watch for:** the daemon / TUI still spawning PATH's `mutagen` (check
  `ps`); the override falling back to PATH silently when unset-but-invalid.

## Cross-cutting invariants (must hold across all scenarios)

- **Never blocks without a terminal** — with `--no-input` / `CINNA_NO_INPUT=1`
  every command either proceeds on defaults or fails with `needs_input`.
- **Exit codes are a contract** — 0 / 10 / 11 / 12 / 1(+code) / 2, see
  [Account Workspace acceptance](../account_workspace/account_workspace_acceptance.md)
  scenario 17.
- **Two token stores stay in lockstep** — any command that writes the CLI token
  updates **both** `.cinna/config.json` and `~/.cinna/agents.json`; they never drift.
- **No silent rebind / clobber** — `set-token` is agent-bound; `setup` refuses a
  populated dir; both fail loud with guidance.
- **Teardown is intentional** — `disconnect` keeps `workspace/`; only
  `disconnect-all` deletes directories, and only behind a confirmation.
- **No state drift between commands** — a command re-writing the registry must not
  drop another concern's state (sync re-upsert preserves the git block; teardown
  removes exactly its own agent's row).
- **Secrets never leak** — `~/.cinna/agents.json` stays `0600`; `.cinna/` stays
  gitignored; the setup token is never persisted.
- **Sync never outlives its process** — after Ctrl-C on `setup` / `dev` / `redev`,
  the shared Mutagen daemon has no dangling session for that agent.

## Cleanup

- `cinna disconnect` in each agent dir (keeps `workspace/`), or `cinna disconnect-all`
  from the scratch parent (deletes the dirs) to undo checkouts.
- Verify `~/.cinna/agents.json` has no leftover/bogus rows afterward — especially if
  any helper ran **outside** pytest's global-state isolation (it would write the
  real registry). Restore the backup you took in Preconditions if needed.
- Remove the completion snippet from your shell rc if you installed it on a machine
  you don't want it on.
- Run `cinna doctor` to reconcile any sessions/tokens left behind (see
  [Doctor](../doctor/doctor.md)).
