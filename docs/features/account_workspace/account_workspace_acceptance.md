# Account Workspace — Acceptance Scenarios (live e2e)

The catalog of **real-usage scenarios** for an agent doing *integration* testing
of the `cinna account` command group against a **live** environment — a real
platform backend with a real user account that has at least a couple of agents
and (ideally) more than one user workspace. These are not unit tests; they exist
to catch what unit tests miss: the single-use account setup token getting burned
on a doomed run, workspace-scoping drift between the listing and the create
target, a context refresh clobbering a good tree, child-token revoke leaking into
local teardown, and registry/credential state drift across commands.

How to use: pick scenarios relevant to the change, run the **Steps** verbatim
against a live env, assert the **Expected**, and watch for the **Watch for**
failure modes. Run the credential and workspace-scoping scenarios (7–11) on any
change to the active-workspace logic or the credentials drafts — that is where
the silent-secret and scope-drift bugs live.

## Preconditions

- A reachable platform (e.g. `http://localhost:8000` backend, `:5173` frontend)
  where you can open **Settings → Local Development** and mint an **account**
  setup token (note: *account*, not the per-agent one).
- A user account with **at least two agents**, ideally including one
  **foreign-install** (view-only) agent and a pair of agents in **different user
  workspaces**.
- **Editable install** of the CLI under test:
  `python3 -c "import cinna,os;print(os.path.dirname(cinna.__file__))"` must point
  at this repo's `src/cinna`. Confirm `which cinna` resolves and
  `cinna account --help` lists `setup / set-token / agents / status /
  refresh-context / user-workspace / credentials`.
- For the no-terminal scenarios (15–18): `jq` to inspect the JSON lines, and a
  second **account** setup token per run (every exchange burns one).
- `git` and `mutagen` on `PATH` (needed once a child agent is synced and
  exercised).

> Run account verbs from anywhere inside the account workspace — they walk up to
> `.cinna/account.json`, so they also work from a synced `agents/<slug>/` folder.

## Scenario catalog

### 1. Account setup materializes the root

- **Goal:** a developer bootstraps a multi-agent root from one paste.
- **Setup:** mint an account setup token in the UI; pick an empty parent dir.
- **Steps:**
  ```
  cinna account setup 'curl -sL http://localhost:8000/api/cli-setup/account/<TOKEN> | python3 -' --name laptop --dir my-cinna
  cd my-cinna
  ls -a
  cat .cinna/account.json
  ```
- **Expected:** `.cinna/account.json` exists with mode `600` and holds
  `account_token` / `platform_url` / `frontend_url` / `machine_name`; an empty
  `agents/` dir; an orchestrator `CLAUDE.md` (contains "Account Workspace");
  `.claude/settings.json` with `permissions.allow == ["Bash(cinna:*)",
  "mcp__platform-knowledge"]` and `enableAllProjectMcpServers: true`; `.mcp.json`
  + `opencode.json` pointing the proxy at `.cinna/account.json` via
  `CINNA_ACCOUNT_CONFIG`; a `context/` tree; and the next-step hints
  (`cinna account agents`, `cinna agent sync`).
- **Watch for:** the account token file not 0600; `--dir` ignored; the
  single-use token being spent even though setup later failed; `.mcp.json`
  written with an absolute (non-portable) `CINNA_ACCOUNT_CONFIG`.

### 2. Setup guards the target before burning the token

- **Goal:** a doomed setup never wastes the single-use account token.
- **Steps:** run `cinna account setup <fresh-token> --dir my-cinna` a second time
  into the same `my-cinna/`.
- **Expected:** it aborts with `… already contains a cinna account workspace`
  **without** calling the exchange endpoint (the token is still usable elsewhere).
- **Watch for:** the token being exchanged (and thus burned) before the
  existing-workspace check.

### 3. Domain-derived default folder name

- **Goal:** with no `--dir`, the folder defaults to the platform domain.
- **Steps:** `cinna account setup <url-token>` (no `--dir`) from an empty parent,
  accept the prompt default.
- **Expected:** the workspace lands in a folder named after the host (e.g.
  `localhost`, `demo-core_opencinna_io`), creds/port stripped.
- **Watch for:** a literal `my-cinna` default when a usable host was available.

### 4. `cinna account agents` lists with local-checkout annotations

- **Goal:** discover agents and see which already have a local checkout.
- **Steps:**
  ```
  cinna account agents
  ```
- **Expected:** a table with name + id, build column (`can build` /
  `foreign install` / `view-only`), an env-active marker, and a
  `Local workspace` column showing `agents/<slug>/` for synced agents or
  `not synced` otherwise. The header states the active-workspace scope.
- **Watch for:** a synced agent shown as `not synced` (child-discovery missing a
  nested subdir); a foreign install mislabeled as buildable.

### 5. `cinna agent sync` attaches a standard child

- **Goal:** attach an agent as a normal per-agent checkout from the account root.
- **Steps:**
  ```
  cinna agent sync "<Agent Name>"        # or slug / id
  cinna account status
  cinna exec --agent <slug> python -c 'print("hello")'
  ```
- **Expected:** a checkout under `agents/<slug>/…` byte-identical to a
  `cinna setup` one (`.cinna/config.json` with a freshly-minted child token +
  `cli_token_id`, registry entry in `~/.cinna/agents.json`, generated files,
  Model-A layout, git auto-link if versioned). `cinna account status` lists it in
  the synced-agent table; `cinna exec --agent` resolves the child token and runs.
- **Watch for:** the child token being the account token (it must be a minted
  per-agent token); a missing registry entry breaking `cinna list` / the sync
  shim; re-running sync for the same agent silently duplicating instead of
  reporting `already a synced workspace`.

### 6. Foreign-install / unknown agent sync surfaces the backend verdict

- **Goal:** the CLI surfaces the backend's decision rather than pre-judging.
- **Steps:** `cinna agent sync "<Foreign Install Agent>"`; then
  `cinna agent sync nope`.
- **Expected:** the foreign install is rejected with the backend's 403 detail
  verbatim (publisher-managed); the unknown ref fails with
  `No accessible agent matches 'nope'` and a hint to run `cinna account agents`.
  Neither leaves a half-written checkout.
- **Watch for:** a client-side guess at build rights; a partial checkout left
  behind on a rejected mint.

### 7. Active user workspace: list / activate / clear

- **Goal:** scope discovery and creates to a user workspace.
- **Steps:**
  ```
  cinna account user-workspace list
  cinna account user-workspace activate "<Workspace Name>"
  cinna account agents                 # scoped
  cinna account agents --all           # every workspace
  cinna account user-workspace clear
  ```
- **Expected:** `list` marks the active workspace (Default row always present);
  `activate` persists `user_workspace_id`/`name` into `.cinna/account.json` and
  reports it; scoped `agents` shows only that workspace's agents with a
  `N of M accessible` header; `--all` shows everything; `clear` (and
  `activate default`) resets to Default. Verify with
  `cat .cinna/account.json`.
- **Watch for:** the selection not persisting; scoped listing showing
  Default-workspace agents; `--all` still filtering.

### 8. Credentials are draft-only (no secret ever sent)

- **Goal:** the account CLI scaffolds a credential without a secret value.
- **Steps:**
  ```
  cinna account credentials types
  cinna account credentials create --name "Stripe Key" --type api_token
  cinna account credentials list
  ```
- **Expected:** `types` lists each type with its required fields; `create`
  returns a credential id, status `needs setup`, the list of fields **the user**
  must fill, and a UI `setup_url`; `list` shows it as `needs setup`. The created
  credential lands in the active user workspace.
- **Watch for:** any secret-bearing field being sent from the CLI; the draft
  showing `complete` before the user fills it; the credential landing in the
  wrong workspace.

### 9. Credential create `--agent` attaches in one step

- **Goal:** scaffold + attach a draft to an agent in a single command.
- **Steps:**
  ```
  cinna account credentials create --name "Odoo" --type odoo --agent <agent-ref>
  cinna account credentials share-with-agent <other-cred-id> --agent <agent-ref>
  ```
- **Expected:** create reports `Attached to: <agent name>`; share-with-agent
  attaches an existing credential and notes it will sync into the agent's env once
  the user fills the secret.
- **Watch for:** the attach silently no-oping; resolving `--agent` to the wrong
  agent on a slug collision.

### 10. Credential update is metadata-only; delete unlinks

- **Goal:** edit metadata and delete safely.
- **Steps:**
  ```
  cinna account credentials update <cred-id> --name "Renamed" --no-share
  cinna account credentials update <cred-id>          # expect refusal
  cinna account credentials delete <cred-id> --yes
  ```
- **Expected:** update changes only metadata and reprints status; an empty update
  fails with `Nothing to update`; delete removes the credential (and unlinks it
  from any agents). A Tier-2 (published-bundle) credential delete is refused with
  the backend's 409 unless `--force`.
- **Watch for:** update touching a secret field; delete succeeding on a Tier-2
  credential without `--force`.

### 11. `cinna account status` reflects token validity

- **Goal:** the developer can tell whether the account token is alive.
- **Steps:** `cinna account status`; then revoke the account session in the UI
  and re-run.
- **Expected:** first run shows the account root, URLs, machine, active
  workspace, the synced-agent table, and `valid token`. After revoke it shows
  `expired token` and the `cinna login` re-auth hint. With the platform
  unreachable it shows `no connection`.
- **Watch for:** a revoked token still labeled valid; the re-auth hint missing.

### 12. `refresh-context` is non-destructive and refreshes guides

- **Goal:** pick up updated docs/commands without a re-setup, safely.
- **Steps:**
  ```
  # confirm a synced child exists (scenario 5)
  cinna account refresh-context
  ls context/ ; cat CLAUDE.md ; ls agents/<slug>/CLAUDE.md
  ```
- **Expected:** `context/` is replaced (old-only files gone); the orchestrator
  `CLAUDE.md`, `.mcp.json`/`opencode.json`, and **every** synced child's
  per-agent `CLAUDE.md` are regenerated from the bundled templates; a
  user-edited `.claude/settings.json` is left untouched; the run reports how many
  child workspaces were regenerated.
- **Watch for (non-destructive rule):** simulate a download failure (point at an
  unreachable platform) and confirm the **old `context/` survives** and the
  command still exits 0 with a warning. Also watch a child regeneration failure
  aborting the whole command.

### 13. `cinna login` device flow (resume + bootstrap)

- **Goal:** refresh the account token without a paste, and bootstrap from empty.
- **Steps:**
  ```
  cinna login                            # inside the account workspace → refresh
  # then from an empty folder:
  cinna login app.example.com --dir my-cinna2
  ```
- **Expected:** inside the workspace it prints a verification URL + user code,
  opens a browser, and on Authorize swaps a fresh token into
  `.cinna/account.json` in place (other files untouched). From an empty folder it
  bootstraps a brand-new account workspace (config + generated files + context).
- **Watch for:** the resume path bootstrapping a *new* workspace instead of
  refreshing; a platform without the route not surfacing the clear
  "use Settings → Local Development" fallback.

### 14. `cinna agent unsync` preserves user files, best-effort revoke

- **Goal:** detach a child cleanly; teardown never blocks on a revoke failure.
- **Steps:**
  ```
  cinna agent unsync <slug>              # confirm
  ls agents/<slug>/workspace             # user files still here
  cinna list                             # registry entry gone
  ```
- **Expected:** sync stops, the child token is best-effort revoked via the
  account token, `.cinna/` + generated files are removed, the registry entry is
  dropped — but the developer's `workspace/` files and the folder remain. A
  revoke 404 / network error warns but does not block local teardown.
- **Watch for:** user files deleted; the registry entry surviving; a revoke error
  aborting the command.

### 15. Desktop-style setup: absolute `--dir`, `--no-input`, `--json`, no TTY

- **Goal:** Cinna Desktop creates the account workspace as a child process with
  no terminal and parses the result.
- **Setup:** a fresh account setup token; note the `setup_command` string the
  platform returns (`curl -sL …/api/cli-setup/account/<TOKEN> | python3 -`).
- **Steps:**
  ```
  TARGET="$HOME/CinnaAgents/Cloud/localhost"          # parents must not exist yet
  cinna account setup 'curl -sL http://localhost:8000/api/cli-setup/account/<TOKEN> | python3 -' \
      --dir "$TARGET" --name desktop-test --no-input --json < /dev/null > out.jsonl; echo "exit $?"
  cat out.jsonl | jq -c .
  ls -a "$TARGET"; cat "$TARGET/.cinna/account.json"
  ```
- **Expected:** exit `0`. Every line of `out.jsonl` parses; the first three are
  `{"step":1..3,"total":3,"status":"start",…}`, the last is `{"result":"ok",
  "workspace":"<TARGET>","platform_url":…,"frontend_url":…,"machine_name":
  "desktop-test","context_package":"ok"}`. The workspace sits exactly at
  `$TARGET` (dots in the host kept, parents created), with the same files as
  scenario 1. No table, hint or spinner text on stdout.
- **Watch for:** the workspace landing under cwd instead of the absolute path;
  a non-JSON line on stdout (Rich leaking); the process waiting on stdin
  (folder / machine-name prompt); `context_package` not reflecting a failed
  download (should be `failed` with a preceding `"status":"warn"` line, exit
  still 0).

### 16. Desktop-style refresh: `account set-token` in place, same account only

- **Goal:** the desktop refreshes an expiring account token silently; a token
  for another account can never be swapped in.
- **Setup:** scenario 15's workspace; `cinna account user-workspace activate
  <name>` so a client-side selection exists; one synced child (scenario 5).
  Mint two fresh account setup tokens: one as the **same** user, one as a
  **different** user on the same platform.
- **Steps:**
  ```
  cd "$TARGET"; cp .cinna/account.json /tmp/before.json
  cinna account set-token 'curl -sL http://localhost:8000/api/cli-setup/account/<SAME_USER_TOKEN> | python3 -' \
      --no-input --json < /dev/null; echo "exit $?"
  diff <(jq 'del(.account_token)' /tmp/before.json) <(jq 'del(.account_token)' .cinna/account.json)
  cat agents/<slug>/.cinna/config.json | jq .cli_token       # unchanged
  cinna account set-token 'curl -sL http://localhost:8000/api/cli-setup/account/<OTHER_USER_TOKEN> | python3 -' \
      --no-input --json < /dev/null; echo "exit $?"
  cinna account set-token '<SAME_USER_TOKEN again>' --json; echo "exit $?"
  ```
- **Expected:** first call exits `0`, prints two `start` steps, an `ok` line
  and `{"result":"ok",…,"context_package":"skipped"}`; only `account_token`
  changed (the `diff` is empty), the child token is untouched, and the exchange
  was made with the **stored** machine name (check the CLI token list in
  Settings — no new machine). The other-user call exits `11` with
  `{"result":"error","code":"account_mismatch",…}` and `account.json` is
  unchanged. The reused (already burned) token exits `10` with
  `code: "setup_token_invalid"` and the backend detail.
- **Watch for:** the mismatch being detected only after the file was written;
  `user_workspace_id` or `machine_name` reset; a new machine appearing in the
  platform's token list; exit `1` where `10` / `11` is required.

### 17. Exit-code contract

- **Goal:** a driver can act on the exit code alone.
- **Steps:** run each from a scratch dir with `--no-input --json < /dev/null`
  and record `$?` plus the final line's `code`:
  ```
  cinna account setup '<ALREADY_USED_TOKEN_URL>' --dir /tmp/x1        # 10 setup_token_invalid
  cinna account setup '<VALID_URL>' --dir "$TARGET"                    # 1  workspace_exists (token NOT burned)
  cinna account setup 'http://127.0.0.1:1/api/cli-setup/account/X' --dir /tmp/x2   # 12 network
  cinna account status                                                 # 1  not_an_account_workspace
  cinna --no-input login                                               # 1  needs_input (no domain, human text)
  cinna account setup                                                  # 2  usage
  ```
  Then, with the backend stopped: `cinna account set-token '<URL>' --json` → `12`.
- **Expected:** the codes in the comments. For the `workspace_exists` case,
  re-use the same valid token afterwards in a fresh dir — it must still work.
- **Watch for:** any of these coming back as a bare `1`; a traceback instead of
  a JSON error line; the guard case burning the token.

### 18. `account status --json` and the cinna-cli version pin

- **Goal:** the desktop reconciles from one status line, including whether to
  reinstall cinna-cli.
- **Steps:**
  ```
  cd "$TARGET"; cinna account status --json | jq .
  curl -s http://localhost:8000/.well-known/cinna-desktop | jq .local_dev
  cinna account status | tail -20
  cinna doctor --dry-run
  ```
- **Expected:** exactly one JSON line with `result`, `workspace`,
  `platform_url`, `frontend_url`, `machine_name`, `active_workspace`
  (`{id,name}` or `null`), `token` (`valid|expired|unreachable`),
  `synced_agents`, `agents[]`, `context_package{local,remote,state}` and
  `cli{installed,required,state}`. When the platform publishes
  `local_dev.cinna_cli_version`, `cli.required` equals it and `state` is
  `current` / `behind` / `ahead`; on an older platform `required` is `null`
  and `state` is `unknown` — still exit 0. The human `status` shows the same
  as a `cinna-cli` table row and a `uv tool install cinna-cli==<pin>` hint when
  behind; `doctor` lists the platform under "manual action needed" only when
  the pin differs.
- **Watch for:** a missing discovery document turning into an error; `doctor`
  nagging when no pin is published; the JSON line missing `cli`.

## Cross-cutting invariants (must hold across all scenarios)

- **`--json` stdout is JSON only, last line is the verdict** — every stdout line
  parses; exactly one `result` line; human hints never leak.
- **Never wait for a human without a terminal** — with `--no-input` / `--json`
  / `CINNA_NO_INPUT=1` every command either proceeds or fails with
  `needs_input`; nothing reads stdin or `/dev/tty`.
- **No secret ever sent** — credential create/update bodies carry only metadata;
  the CLI cannot read or write a credential's secret value.
- **Account token stays account-scoped** — it is used only on `/account/*`; every
  per-agent sync/exec uses a minted child token. The token file is 0600.
- **Single-use setup token is never burned on a doomed run** — guards precede the
  exchange.
- **No silent clobber** — `refresh-context` removes the old `context/` only after
  a successful download; user-edited `.claude/settings.json` is never overwritten.
- **No state drift between commands** — a synced child is a standard per-agent
  workspace (registry + config) indistinguishable from `cinna setup`; the active
  user workspace is the single source for both the listing scope and the create
  target.
- **Backend verdicts surface verbatim** — foreign-install 403s, ambiguous
  resolves, expired tokens, and Tier-2 delete 409s come through unmodified.

## Cleanup

- Detach test children: `cinna agent unsync <slug>` for each synced agent (keeps
  workspace files; delete the `agents/<slug>/` folder manually if you want it
  gone).
- Delete any draft credentials you created:
  `cinna account credentials delete <cred-id> --yes`.
- Remove any test agents created via `cinna agent create` from the platform UI
  (the CLI has no agent-delete verb).
- Drop the whole account workspace by deleting its folder; revoke the account
  session from the platform's Settings if it should no longer be usable.
- Verify `~/.cinna/agents.json` has no leftover entries for the test children —
  especially if any helper ran **outside** pytest's global-state isolation (it
  would write the real registry).
