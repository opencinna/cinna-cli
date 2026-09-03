# Local Agent Import — Acceptance Scenarios (live e2e)

Real-usage scenarios for an agent doing *integration* testing of `cinna agent
import` against a **live** platform: a real backend, a real account workspace,
and a real local agent folder. These are not unit tests — they exercise agent
creation, the bulk prompt write through the escape hatch, child-token minting,
Mutagen sync, credential drafting, and schedule CRUD end to end.

How to use: pick the scenarios relevant to the change, run the **Steps**
verbatim, assert the **Expected**, and watch for the **Watch for** failure
modes. Scenarios 3–6 (idempotency and the stamp) are the highest-value ones for
any change to the orchestrator.

## Preconditions

- A reachable platform with an **account workspace**: `Cloud/.cinna/account.json`
  (created by `cinna login <platform> --dir Cloud`). Every command below runs
  from inside `Cloud/`.
- The account's user can create agents (`agent-developer` role) — otherwise
  step 2 returns 403.
- **Editable install** of the CLI under test:
  `python3 -c "import cinna,os;print(os.path.dirname(cinna.__file__))"` points at
  this repo's `src/cinna`; `cinna agent import --help` lists all six flags.
- Mutagen installed (the import pushes through a real sync session).
- A **local agent folder** at `../Local/<slug>/` holding a valid
  `cinna-agent.json` — scaffold one with the kit
  (`python3 .cinna-kit/tools/kit.py new <slug>`), fill the three prompt files,
  declare one credential and one schedule, and put a throwaway secret in
  `credentials/.env` so the secret-leak assertions are meaningful.

## Scenario catalog

### 1. Dry run shows the plan and touches nothing

- **Goal:** the user can inspect the import before committing to it.
- **Steps:**
  ```
  cinna agent import ../Local/<slug> --dry-run
  ```
- **Expected:** the file list, credential drafts, and schedules are printed;
  "Dry run — nothing was sent to the platform."; `cinna account agents` shows no
  new agent; `../Local/<slug>/cinna-agent.json` still has `cloud.agent_id: null`.
- **Watch for:** any HTTP request in `-v` output; a workspace appearing under
  `agents/`.

### 2. First import, end to end

- **Steps:**
  ```
  cinna agent import ../Local/<slug> --yes
  ```
- **Expected:** nine `[n/9]` steps; the agent exists in the UI with the
  manifest's description, example prompts, and router trigger (`cinna agent show
  <slug> --prompts` matches the three local prompt files); `agents/<slug>/`
  exists and `agents/<slug>/workspace/` holds the agent's files;
  `cinna exec --agent <slug> ls` on the remote env lists the same files; the
  credential appears as **needs setup** with the printed setup URL; the schedule
  is listed by `cinna agent schedule list <slug>`; the status refresh command is
  set (`cinna agent status show <slug>`); the manifest's `cloud` block now
  carries `platform_url`, `agent_id`, `imported_at`.
- **Watch for:** `credentials/` or `app-data/` reaching the remote env (check
  with `cinna exec --agent <slug> ls -a`); any secret value in the terminal
  output; `workspace_requirements.txt` missing when the agent has a
  `pyproject.toml`.

### 3. Second import without `--update` is refused

- **Steps:** re-run scenario 2's command.
- **Expected:** exit ≠ 0, the error names the agent id and tells you to pass
  `--update`; nothing changed on the platform.
- **Watch for:** a **second** agent with the same name appearing.

### 4. `--update` re-imports without duplicating

- **Steps:** edit `docs/WORKFLOW_PROMPT.md` and a script locally, then <!-- nocheck: path inside the local agent folder, not this repo -->
  ```
  cinna agent import ../Local/<slug> --update --yes
  ```
- **Expected:** the same agent id; `cinna agent show <slug> --prompts` shows the
  edited workflow prompt; the edited script is live in the env; **no** second
  credential (the existing one is re-attached) and **no** second schedule (it is
  updated in place); `cinna account credentials list` count unchanged.
- **Watch for:** a duplicate empty credential draft shadowing the one the user
  already filled — that would break the running agent.

### 5. The stamp only happens after a successful push

- **Steps:**
  ```
  cinna agent import ../Local/<slug2> --no-push --yes
  ```
- **Expected:** files are copied into `agents/<slug2>/workspace/`, the manifest
  is **not** stamped, and the output tells you to `cinna sync push --agent
  <slug2>` and re-run with `--update`.
- **Watch for:** a stamped manifest pointing at an agent whose environment never
  received the files.

### 6. Resume after a mid-run failure

- **Goal:** a partial import is recoverable.
- **Steps:** cause step 7 to fail (e.g. an unreachable platform, or a credential
  type the account cannot create), then fix it and re-run with `--update`.
- **Expected:** the second run reuses the agent (by name when the manifest is
  unstamped, by id when stamped), skips what already exists, and completes.
- **Watch for:** a second agent created because the name match was ambiguous —
  the command must **refuse** with a "set cloud.agent_id" hint, not guess.

### 7. Targeting a user workspace

- **Steps:**
  ```
  cinna account user-workspace list
  cinna agent import ../Local/<slug3> --workspace "<Workspace Name>" --yes
  ```
- **Expected:** the agent and its credential drafts land in that workspace
  (visible in the UI sidebar and in `cinna account agents`).
- **Watch for:** the agent in the target workspace but the credential in Default
  — the agent then cannot see its own credential.

### 8. Manifest guards

- **Steps:** in a scratch copy of the agent folder, one at a time: bump
  `schema_version` to `2`; rename the folder so it disagrees with `slug`; make
  `cron_string` `"every morning"`; drop a credential's `type`.
- **Expected:** each is refused *before* any platform call, with a message that
  names the offending field.
- **Watch for:** a half-applied import (agent created, then the manifest
  rejected).

### 9. Secrets never leave the machine

- **Steps:** with a recognizable secret in `credentials/.env`, run a full import
  with `-v`, then
  ```
  cinna exec --agent <slug> "grep -r '<secret>' . | head"
  ```
- **Expected:** no match remotely; the secret appears nowhere in the CLI output
  or logs; `credentials/` does not exist in the remote workspace.
- **Watch for:** a `.env` copied because it sat outside `credentials/` — the
  import must refuse the plan outright in that case. Scenario 10b covers the
  `.env.<suffix>` shapes that used to clear every gate.

### 10. The contract's exclude list is honoured, and it is `layout.json`

- **Goal:** a correction cinna-core publishes reaches this tool on the next kit
  refresh instead of waiting for a new cinna-cli release.
- **Setup:** a `.cinna-kit/layout.json` above the agent, and a `notes/` folder
  inside it.
- **Steps:** give `layout.json` a `cloud_import_excludes` list that adds
  `notes/` **and** omits `credentials/`; import into a fresh agent. Then edit
  the list on disk (add `config/`) and re-run with `--update`.
- **Expected:** `notes/` is absent remotely; `credentials/` is *still* absent
  (the mandatory exclusion cannot be removed); the `Exclusions:` line names the
  `layout.json` path it read and does **not** say `DEGRADED`; the second run
  drops `config/` without any change to cinna-cli.
- **Watch for:** the built-in list being used silently when a `layout.json`
  exists; and a `kit.json` `cloud_import.exclude` still being honoured —
  decision D6 deleted that key and reading it back would be a second authority
  for one list.

### 10a. A missing contract is reported as a degradation

- **Goal:** the failure that made the previous regression invisible. The old
  line read `Exclusions: built-in default list` — it announced a *mode*, so
  nobody saw an error and the first symptom was a `.env.prod` in a cloud
  workspace.
- **Steps:** import an agent with **no** `.cinna-kit/` anywhere above it.
- **Expected:** the run succeeds; the `Exclusions:` line begins `DEGRADED —`
  and says why; **no `content_hash` is recorded** in `publications.json` and the
  run prints `Content hash: WITHHELD`.
- **Watch for:** a hash recorded anyway. "But the fallback is the same list" is
  a claim about this build's contract, not about the one in the folder — which
  is the only one the other host is reading.

### 10b. Dotenv suffixes never travel, examples still do

- **Goal:** the leak the contract's `secret_files` rules close. `.env.prod` and
  `.env.local` clear every glob in the exclude list *and* the old
  `endswith(".env")` check, at the agent root and at any depth.
- **Setup:** in the agent folder, outside `credentials/`, create `.env.prod`,
  `config/.env.staging`, `staging.env` and `.env.example`, each with a
  recognizable secret except the example.
- **Steps:**
  ```
  cinna agent import ../Local/<slug> --dry-run
  cinna agent import ../Local/<slug> --update -y
  cinna exec --agent <slug> "ls -a; ls -a config 2>/dev/null"
  ```
- **Expected:** the dry-run plan lists `.env.example` and none of the others;
  remotely only `.env.example` exists; the secret string appears nowhere in the
  output.
- **Watch for:** `.env.example` being withheld too — that is the `unless` block
  doing its job, and a fix written as a bare `startswith(".env.")` loses it.

### 10c. A withheld secret does not move the `content_hash`

- **Goal:** "unpublished changes forever" — a path withheld from the upload but
  counted in the hash makes the hash move for a change that can never be
  published.
- **Steps:** import once and note `publications.json`'s `content_hash`. Edit
  `.env.prod` (a withheld file) and re-run `--update`; then edit
  the agent's own `docs/WORKFLOW_PROMPT.md` (a travelling file) and re-run <!-- nocheck: a path inside the agent folder, not this repo -->
  again.
- **Expected:** the hash is unchanged after the first edit and different after
  the second. Immediately after any successful import, the recorded hash matches
  a fresh scan of the folder — the folder is never "behind" the instant a
  publish succeeds.
- **Watch for:** a hash that moves on the very first re-run with no edits at
  all. That means the manifest was written *after* the hash was computed.

### 10d. The ledger is a sibling file, and the legacy `cloud` block migrates

- **Goal:** `publications.json` is written, `cloud` is retired behind it, and no
  `--update` ever creates a second agent.
- **Setup:** an agent folder carrying a legacy `cloud` block naming a real
  `platform_url` and `agent_id`, and no `publications.json`.
- **Steps:** run `--update` against that instance, then inspect both files, then
  run `--update` once more.
- **Expected:** the first run resolves the agent from the `cloud` block and does
  **not** create a new one; afterwards `cinna-agent.json` has no `cloud` key,
  `publications.json` holds an entry for that `platform_url`, and
  `publications.json` is **not** present in the remote workspace. The second run
  resolves from the ledger and is a no-op on identity.
- **Watch for:** a second agent appearing on the platform (the deprecated read
  was dropped too early); a `cloud` block deleted while the ledger recorded
  nothing (a destination must be reached before the source may be removed); a
  `publications.json` that travelled.

### 10e. Two instances, resolved by `platform_url`

- **Goal:** the resolution key is part of the contract — not `workspace`, not
  position in the array.
- **Setup:** two account workspaces for two different platform hosts.
- **Steps:** import the same agent folder into instance A, then into instance B,
  then re-run `--update` against A.
- **Expected:** `publications.json` holds two entries; the A re-run updates A's
  agent and leaves B's entry byte-identical; an entry whose `workspace` key is
  absent entirely still resolves (that is the shape Cinna Desktop writes).
- **Watch for:** the second import being refused as "already imported", or the
  A re-run updating B's entry.

### 10f. The `contract_version` gate

- **Steps:** set the manifest's `contract_version` to `2.0.0` and import; then
  to `1.4.2` and import; then remove the key entirely and import.
- **Expected:** `2.0.0` is refused before any platform call, naming `2.x`;
  `1.4.2` passes **silently** (same major — a minor contract change is additive
  by definition and reaches a non-adopting reader as silence); the missing key
  warns about a re-stamp and imports anyway.
- **Watch for:** a warning on the same-major pair. That is the gate's shape, not
  a bug: anything an old reader must notice needs a major bump.

### 11. Verification loop the guide promises

- **Steps:**
  ```
  cinna chat --agent <slug> "<first example prompt>"
  ```
- **Expected:** the agent answers using its imported workflow prompt and
  scripts. With the credential still empty, it should say what it needs rather
  than crash.
- **Watch for:** prompts that landed in the DB but not in the env — if the env
  was already running, `cinna api POST agents/<id>/sync-prompts` is the manual
  push.
