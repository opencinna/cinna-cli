# Agent Addons — Acceptance Scenarios (live e2e)

Real-usage scenarios against a **live** platform: a real backend, a real agent
environment, a real skills catalog. Unit tests cover the wire shapes; these cover
what only a live run shows — that the list agrees with what the engine loads, and
that a publish is visible to the person it was shared with.

## Preconditions

- An account workspace (`cinna account setup …` or `cinna login`) whose user
  holds the `agent-developer` role.
- Two agents on that account: one you own and can build (`AGENT`), and — for the
  refusal scenarios — one foreign bundle install (`FOREIGN`, shown by
  `cinna account agents` as not buildable).
- `AGENT` synced (`cinna agent sync <AGENT>`) with a running or suspended
  environment.
- A second platform user (`OTHER`) whose email you can grant to, and whose
  catalog you can check.
- A **consumer** agent (`CONSUMER`) on the same account — a second agent you can
  install onto, so the publish → install → update loop runs end to end.
- The editable install: `which cinna` resolves into this repo's `src/cinna`.

> Reference agents by display name, slug, or id. An unresolved ref must fail
> **before** any platform call.

## Scenario catalog

### 1. List the addons of an agent that has both halves

**Goal** — the two sources appear as one list, and a catalog install appears once.

**Setup** — `AGENT` has at least one installed plugin and at least one local
`skills/<name>/` folder. Install one skill from the catalog through the web UI so
the overlap case is present.

**Steps**

```bash
cinna skills list <AGENT>
```

**Expected** — a table with one row per addon. The plugin row shows
`plugin` / `marketplace`; the local folder shows `skill` / `local`; the catalog
install shows `skill` / `catalog` — exactly **once**, not once per half. The
counts line reports `local_skills` as a subset of `skills`.

**Watch for** — the catalog install listed twice (the dedupe rule regressed, or
the CLI is folding the halves itself); skills that belong to a plugin appearing
as their own rows.

### 2. A new skill folder shows up after a push, not before

**Goal** — the list reflects the environment, not local disk.

**Steps**

```bash
mkdir -p agents/<slug>/workspace/skills/acceptance-demo
printf -- '---\nname: acceptance-demo\ndescription: A demo skill.\n---\n\nSteps.\n' \
  > agents/<slug>/workspace/skills/acceptance-demo/SKILL.md
cinna skills list <AGENT>          # before the push
cinna sync push --agent <slug>
cinna skills list <AGENT>          # after the push
```

**Expected** — the second run lists `acceptance-demo` as `skill` / `local` with
status `ok`. (The first may not: the server serves its cached index.)

**Watch for** — a list built from local files instead of the server projection; a
stale cache that never refreshes after a push.

### 3. A sleeping environment lists plugins and says why the rest is missing

**Goal** — a partial answer is labelled, not disguised.

**Setup** — let `AGENT`'s environment suspend (or suspend it from the UI).

**Steps**

```bash
cinna skills list <AGENT>
```

**Expected** — exit code 0. Plugin rows still print; a warning names the reason
(`env_not_running` / `adapter_error` / `parse_error`). The command does **not**
wake the container.

**Watch for** — the command waking the environment (it must be cache-only); a
short list printed with no warning.

### 4. Publish a local skill and open it in the catalog

**Goal** — the primary flow, end to end, with nobody typing a version.

**Setup** — `acceptance-demo/SKILL.md` carries **no** `version:` line (the
scenario-2 scaffold does not write one).

**Steps**

```bash
cinna skills publish <AGENT> acceptance-demo --yes \
  --notes "First publish" --package-id com.example.acceptance-demo
```

**Expected** — a success block with `Package: com.example.acceptance-demo`,
`Revision: 1 (1.0.0)`, `Visibility: private`, a catalog URL, and a line saying
`skills/acceptance-demo/SKILL.md now carries version: 1.0.0`. Opening the URL
shows the package with one revision. A subsequent `cinna skills list <AGENT>`
marks the row `· published` and shows `1.0.0` in the Version column.

**Watch for** — a catalog URL that 404s (wrong id in the path: the URL takes the
package's **UUID**, not the reverse-DNS id); the `published` marker missing on
the next list; a publish that demands a `--version` before it will run.

### 4a. `--dry-run` shows what the press would take, and takes nothing

**Goal** — the preview is the same derivation the publish will perform.

**Steps**

```bash
cinna skills publish <AGENT> acceptance-demo --dry-run
```

**Expected** — `Version: 1.0.1  (header 1.0.0, latest published 1.0.0)`,
`Package: com.example.acceptance-demo`, `Revision: 2`, and a closing line saying
nothing was published. The catalog page still shows **one** revision, and
`cinna skills list <AGENT>` still shows `1.0.0`.

**Watch for** — a dry run that publishes; a preview version that the next real
publish does not actually take (scenario 5 is the pair that proves it); the
preview being presented as a guarantee — it skips the content checks, so a
secret in the folder still refuses at publish time (scenario 9).

### 4b. The version write-back reaches the local mirror

**Goal** — a publish changed a file in the workspace; the local copy must be
able to see it.

**Steps**

```bash
grep -i '^version:' agents/<slug>/workspace/skills/acceptance-demo/SKILL.md
cinna sync pull --agent <slug>
grep -i '^version:' agents/<slug>/workspace/skills/acceptance-demo/SKILL.md
```

**Expected** — nothing before the pull (the scaffold wrote no version), and
`version: 1.0.0` after it. The rest of the file is byte-identical: the frontmatter
was edited one line at a time, never re-serialised — check that comments,
quoting, key order and the trailing body survived.

**Watch for** — a rewritten header (quoting normalised, comments dropped, keys
reordered); a `name:` that no longer matches the folder; a local edit made
between the publish and the pull turning into a sync conflict nobody was warned
about.

### 5. Re-publishing appends a revision and continues the series

**Steps**

```bash
# edit SKILL.md's body (leave its version: line alone), then:
cinna sync push --agent <slug>
cinna skills publish <AGENT> acceptance-demo --yes --notes "Second"
```

**Expected** — `Revision: 2 (1.0.1)` — the version scenario 4a previewed — the
same package id, and the same visibility as before (no `--visibility` was
passed). The catalog page lists both revisions, newest first, and the local
`SKILL.md` reads `version: 1.0.1` after a pull.

**Watch for** — a second package created instead of a revision; the visibility
silently reset to the default; the version restarting at `1.0.0`, which would
number a *later* revision below its own predecessor.

### 5a. An explicit `--version` is honoured once and then continued from

**Goal** — the override is one revision, not a new mode.

**Steps**

```bash
cinna skills publish <AGENT> acceptance-demo --yes --version 2.0.0
cinna skills publish <AGENT> acceptance-demo --dry-run
```

**Expected** — `Revision: 3 (2.0.0)`, then a preview reading
`Version: 2.0.1  (header 2.0.0, latest published 2.0.0)`. The override reached
the header, and the next derivation continues from it.

**Watch for** — the next version derived from the pre-override series (`1.0.2`);
the explicit version rejected as a duplicate (it is deliberately never
de-duplicated); the override not written back into `SKILL.md`.

### 5b. A `--version` the header could not carry is refused before the wire

**Goal** — the frontmatter-injection guard, on the side that can still stop it.

**Steps**

```bash
cinna skills publish <AGENT> acceptance-demo --version $'1.0\nname: hijacked'; echo "exit=$?"
cinna skills publish <AGENT> acceptance-demo --version '   '; echo "exit=$?"
```

**Expected** — both exit non-zero without contacting the platform, the first
naming the single-line rule, the second saying an empty label is not an
override. The catalog page gains **no** revision.

**Watch for** — either reaching the server (the first is a 422 there, the second
is silently ignored in favour of a derived version — and a revision is
immutable, so a header rewritten by the injected key would ship).

### 6. A mismatched `--package-id` is refused, not ignored

**Steps**

```bash
cinna skills publish <AGENT> acceptance-demo --package-id com.example.other
```

**Expected** — non-zero exit, the server's `package_id_immutable` sentence, and
**no** new revision on the catalog page.

**Watch for** — the flag silently ignored (installs and container manifests
reference that id, so a drift here is not cosmetic).

### 7. `--grant` shares with named users and never revokes

**Steps**

```bash
cinna skills publish <AGENT> acceptance-demo --yes --visibility users \
  --grant <OTHER-email>
cinna skills publish <AGENT> acceptance-demo --yes               # no --grant
```

**Expected** — after the first, `OTHER` sees the package in their catalog. After
the second — which names no grants — `OTHER` **still** sees it.

**Watch for** — the omitted grant list being treated as "revoke everyone".

### 7a. `--grant` without `--visibility users` is refused before anything is written

**Goal** — the inert-grant trap. The server accepts this combination and shares
nothing; the CLI must not let it reach the wire.

**Steps**

```bash
cinna skills publish <AGENT> acceptance-demo --grant <OTHER-email>; echo "exit=$?"
cinna skills publish <AGENT> acceptance-demo --visibility public \
  --grant <OTHER-email>; echo "exit=$?"
```

**Expected** — both exit non-zero naming `--visibility users`, and the catalog
page gains **no** new revision. Then confirm what the refusal is protecting: had
it gone through, the package would read `private`, `OTHER` would see nothing,
and the correction would cost a permanent extra revision.

**Watch for** — the publish succeeding and printing `Visibility: private`
alongside `Granted: <OTHER-email>`, which is the exact contradiction this
scenario exists to prevent. Scenario 7 always pairs `--grant` with
`--visibility users` and scenario 8 expects a `user_not_found` refusal, so
neither one can catch this.

### 8. An unknown grant address fails the whole publish

**Steps**

```bash
cinna skills publish <AGENT> acceptance-demo --visibility users \
  --grant nobody@nowhere.invalid
```

**Expected** — non-zero exit with the server's `user_not_found` sentence, and no
new revision on the catalog page. Note the `--visibility users`: without it the
CLI refuses first (scenario 7a) and the server is never reached, so this
scenario would otherwise pass for the wrong reason.

**Watch for** — a half-applied publish: a new revision with the grant missing.

### 9. A secret in the folder blocks the publish and names the file

**Steps**

```bash
printf 'API_KEY=sk-not-a-real-key-0000\n' \
  > agents/<slug>/workspace/skills/acceptance-demo/.env
cinna sync push --agent <slug>
cinna skills publish <AGENT> acceptance-demo
```

**Expected** — non-zero exit, the `skill_contains_secrets` sentence, and the
offending path listed underneath. Remove the file, push, and confirm the publish
then succeeds.

**Watch for** — the paths dropped from the message (the user is left hunting).

### 10. A foreign install refuses the verb

**Steps**

```bash
cinna skills list <FOREIGN>
cinna skills publish <FOREIGN> <any-skill>
```

**Expected** — `list` succeeds and shows no `· published`-eligible local rows;
`publish` exits non-zero with the server's `foreign_install` (or
`not_developer`) sentence, printed as written.

**Watch for** — the CLI re-wording the refusal, or gating on a locally-guessed
role instead of asking the server.

### 11. An agent with no environment says so

**Setup** — an agent created but never given an environment.

**Steps**

```bash
cinna skills publish <that-agent> anything
```

**Expected** — non-zero exit with the `no_environment` sentence.

**Watch for** — a generic transport error masking a specific, actionable one.

### 12. `--json` gives a driver the raw result

**Steps**

```bash
cinna skills list <AGENT> --json | python3 -m json.tool > /dev/null
cinna skills publish <AGENT> acceptance-demo --json
```

**Expected** — `list --json` prints the raw addons payload (`addons`, `counts`,
`skills_error`) and nothing else, each addon carrying its `version`.
`publish --json` prints `{revision, package, catalog_url, skill_md_updated}`,
with `catalog_url` the same link the human output shows and `skill_md_updated`
`true` on a healthy publish. Neither form prompts.

**Watch for** — Rich table output leaking into the JSON stream; `catalog_url`
absent or built from the reverse-DNS id instead of the package UUID; a
confirmation prompt appearing under `--json`.

### 12a. `--dry-run --json` is the preview payload

**Steps**

```bash
cinna skills publish <AGENT> acceptance-demo --dry-run --json | python3 -m json.tool
```

**Expected** — the `SkillPublishPreview` object (`version`, `header_version`,
`latest_published_version`, `package_id`, `package_id_disambiguated`,
`is_republish`, `next_revision_number`) and nothing else. No revision is added.

**Watch for** — the human preview text mixed into the stream; a driver having to
parse prose to learn the version a publish would take.

### 12b. A terminal confirms; a pipe does not

**Goal** — the confirmation must not turn an existing automation into a hang.

**Steps**

```bash
cinna skills publish <AGENT> acceptance-demo            # from a real terminal
cinna skills publish <AGENT> acceptance-demo < /dev/null
CINNA_NO_INPUT=1 cinna skills publish <AGENT> acceptance-demo
```

**Expected** — the first prints the preview and waits for a `Publish? [Y/n]`;
answering `n` exits non-zero with **no** new revision. The second and third
publish immediately with no prompt (and the third adds one revision, like the
second — run them against a scratch skill if that matters).

**Watch for** — a prompt reached with a non-TTY stdin (a hang, or an EOF-driven
abort of a publish the caller asked for); the preview call being made under
`--yes` / `--json` / `--no-input`, where nothing reads it.

### 13. An unresolved agent ref publishes nothing

**Steps**

```bash
cinna skills publish definitely-not-an-agent some-skill
```

**Expected** — non-zero exit listing the available agents; no platform mutation.

**Watch for** — the ref resolved server-side after a publish attempt.

### 14. Install a published skill on a second agent, by name

**Goal** — the whole install path runs without a UUID and without `cinna api`.

**Setup** — scenario 4 has published `acceptance-demo` from `AGENT`.

**Steps**

```bash
cinna skills catalog --search acceptance
cinna skills show <PACKAGE_ID>
cinna skills install <CONSUMER> <PACKAGE_ID>
cinna skills list <CONSUMER>
```

**Expected** — the catalog row prints the reverse-DNS package id; `show` prints
the revisions and the newest revision's `SKILL.md`; the install succeeds naming
the version it took (read from the link the server returns) and how the
environments took it; the list shows the row with that version.

**Watch for** — any step that only works with a UUID; a package id typed into
`install` that is not accepted; the installed row appearing with a blank version.

### 15. Installing twice is a sentence, not a 409 body

**Steps**

```bash
cinna skills install <CONSUMER> <PACKAGE_ID>     # again
```

**Expected** — non-zero exit, the platform's own sentence, and a following line
saying either that the install is already at the newest revision or naming
`cinna skills update <CONSUMER> acceptance-demo`. No JSON on stdout.

**Watch for** — a raw `{"code": "already_installed"}` reaching the user; the
`--json` error code no longer being `already_installed`.

### 16. A new revision is visible as a pending update, then closed

**Goal** — the difference between a stale consumer and a current one is visible
at a glance.

**Steps**

```bash
# edit skills/acceptance-demo/SKILL.md on AGENT, then:
cinna sync push --agent <slug>
cinna skills publish <AGENT> acceptance-demo --yes
cinna skills list <CONSUMER>
cinna skills update <CONSUMER> acceptance-demo
cinna skills list <CONSUMER>
```

**Expected** — after the publish, the consumer's row reads `1.0.0 → 1.0.1` and a
line under the table names the update command. After `update`, the row shows the
new version alone and the line is gone.

**Watch for** — a stale consumer that looks identical to a current one (the
regression this scenario exists for); the arrow appearing on a row that is
current.

### 17. Toggling changes only what was named

**Steps**

```bash
cinna skills toggle <CONSUMER> acceptance-demo --no-building-mode
cinna skills toggle <CONSUMER> acceptance-demo --disable
cinna skills list <CONSUMER> --json | grep -A3 acceptance-demo
```

**Expected** — after the second call the link is disabled and
`conversation_mode` / `building_mode` still hold what the first call set. Each
call echoes the resulting state (`Enabled:` / `Modes:`) read from the link the
server wrote back, and `cinna skills list` marks the row `· disabled`.

**Watch for** — `--disable` resetting the mode flags to their defaults;
`--disable` reporting success while the link stays enabled (the switch is
stored as `disabled`, so a body carrying `enabled` is silently ignored);
a disabled install that looks identical to an enabled one in `list`.

### 18. Uninstall removes the copy, not the package

**Steps**

```bash
cinna skills uninstall <CONSUMER> acceptance-demo --yes
cinna skills list <CONSUMER>
cinna skills revisions <PACKAGE_ID>
```

**Expected** — the row is gone from `CONSUMER`; the package still lists every
revision it had.

**Watch for** — a revision disappearing; the uninstall asking for a link id.

### 19. `uninstall` on the publisher's own skill explains itself

**Steps**

```bash
cinna skills uninstall <AGENT> acceptance-demo --yes
```

**Expected** — non-zero exit with a sentence saying `acceptance-demo` is one of
`AGENT`'s own `skills/…` folders and there is nothing to uninstall. No route is
called.

**Watch for** — a 404 from the plugin route reaching the user as if the platform
had failed.

### 20. `revisions` answers the publisher's question without `cinna api`

**Steps**

```bash
cinna skills revisions <PACKAGE_ID>
```

**Expected** — one row per revision, newest first, with number, version, release
date, size and the `release_notes` given at publish time.

**Watch for** — a permanently blank notes column (the field is `release_notes`,
not `notes`); ascending order, which buries the answer at the bottom.

### 21. Sharing changes after the publish

**Steps**

```bash
cinna skills visibility <PACKAGE_ID> users
cinna skills grant <PACKAGE_ID> --user <OTHER_EMAIL>
cinna skills grants <PACKAGE_ID>
cinna skills revoke <PACKAGE_ID> --user <OTHER_EMAIL> --yes
cinna skills grants <PACKAGE_ID>
cinna skills delist <PACKAGE_ID> --yes
cinna skills catalog
cinna skills relist <PACKAGE_ID>
```

**Expected** — `OTHER` can see the package in their catalog between the grant
and the revoke, and not after. `grants` prints the visibility beside the list.
Switching to `users` while nobody is named warns that it shares with nobody; a
grant on a `private` package warns that the list is ignored.

The delisted package shows `· delisted` beside its visibility in the catalog
and is discoverable again after `relist`.

**Watch for** — a revoke that needs a user id typed by hand; a revoke that
addresses the *grant* row's id instead of the user's (it deletes nothing and
reports success); a grant reported as shared access on a visibility that
ignores it; `delist` with no way back except `cinna api`.

### 22. `refresh` is the remedy for a stale index

**Steps**

```bash
# with a skill whose SKILL.md version the list does not show yet:
cinna skills refresh <AGENT>
cinna skills list <AGENT>
```

**Expected** — the version (or the previously unreadable index) appears, and
the refresh reports how many skills were indexed. A platform without the
addons-refresh route still refreshes the skill half and says the plugin half
could not be done.

On an agent whose environment adapter is broken, the refresh answers **200 with
`error: adapter_error`** — and must then say so and point at `cinna agent
restart-env <AGENT>`, not print a green check. On an agent whose container
predates agent skills the code is `adapter_unsupported`, and both `refresh` and
`skills list` must point at `cinna agent rebuild-env <AGENT>` instead — a
restart re-runs the same image and another refresh re-reads a route that is not
there. `env_not_running` names neither container verb (it is asleep, not
broken); `parse_error` names `cinna skills refresh`.

**Watch for** — `refresh` failing outright because one of the two routes is
missing; a refresh that changed nothing reported as a success (the failure
arrives as a 200, so only reading `error` catches it); **any surface printing
one remedy for every code** — in particular `cinna skills list` saying "Rebuild
it with: cinna skills refresh", which is both the wrong verb and the wrong word
for it.

### 23. `cinna api` no longer swallows a truncated id

**Steps**

```bash
cinna api GET "agents/f0506e24-3740-4fe3…/addons"
cinna api GET agents/<AGENT_SLUG>/addons
```

**Expected** — the first is refused locally, naming the elided segment, with no
request made. The second resolves the slug (announced on stderr) and returns the
addons payload on stdout. `cinna account agents` prints ids that are never
elided, at any terminal width.

**Watch for** — the elided id reaching the API and coming back `404 Agent not
found`; the resolution note landing on stdout and corrupting a JSON pipe; a
non-agent sub-route under `agents/` being rejected instead of passed through.

## Cross-cutting invariants

- No secret value is ever printed, and a skill folder that holds one cannot be
  published.
- Every refusal the platform authored reaches the user in the platform's own
  words; the CLI adds no paraphrase.
- `cinna skills list` never wakes an environment and never writes anything.
- A failed publish leaves the catalog exactly as it was — no orphan package, no
  partial grant.
- A successful publish is never reported as a failure because a follow-up detail
  lookup failed.
- A publish never reports an audience it did not actually give: `Granted:` and a
  non-`users` visibility must never appear together.
- Every publish takes the **cloud** workspace. Push before publishing, or the
  revision captures the older remote copy — permanently.
- A published revision's version and its `SKILL.md` header never disagree
  silently: either the header carries what the catalog carries, or the CLI says
  the write-back failed.
- A version series never goes backwards. A later revision's version is always
  above its predecessor's, whatever shape the label has.
- Nothing but a real publish adds a revision — not a preview, not a refused
  `--version`, not a declined confirmation.
- No verb in this group asks anyone for a UUID or a link id, and none prints an
  id that a narrower terminal would elide.
- An install is a copy: uninstalling, delisting or revoking never removes a
  revision, and never breaks an agent that already holds one.
- Only `cinna skills refresh` reaches the environment. Every other verb here is
  safe against a sleeping agent.

## Cleanup

- `cinna skills uninstall <CONSUMER> acceptance-demo --yes`, then
  `cinna skills revoke <PACKAGE_ID> --user <OTHER_EMAIL> --yes` and
  `cinna skills delist <PACKAGE_ID> --yes` (reversible with
  `cinna skills relist`). Deleting the package outright is still web-UI only —
  delisting hides it, it does not remove it.
- Remove `skills/acceptance-demo/` from the workspace and `cinna sync push
  --agent <slug>`.
- Re-run `cinna skills list <AGENT>` to confirm the row is gone.
