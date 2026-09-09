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

**Goal** — the primary flow, end to end.

**Steps**

```bash
cinna skills publish <AGENT> acceptance-demo --version 1.0.0 \
  --notes "First publish" --package-id com.example.acceptance-demo
```

**Expected** — a success block with `Package: com.example.acceptance-demo`,
`Revision: 1 (1.0.0)`, `Visibility: private`, and a catalog URL. Opening the URL
shows the package with one revision. A subsequent `cinna skills list <AGENT>`
marks the row `· published`.

**Watch for** — a catalog URL that 404s (wrong id in the path: the URL takes the
package's **UUID**, not the reverse-DNS id); the `published` marker missing on
the next list.

### 5. Re-publishing appends a revision and keeps the package

**Steps**

```bash
# edit SKILL.md, then:
cinna sync push --agent <slug>
cinna skills publish <AGENT> acceptance-demo --version 1.1.0 --notes "Second"
```

**Expected** — `Revision: 2 (1.1.0)`, the same package id, and the same
visibility as before (no `--visibility` was passed). The catalog page lists both
revisions, newest first.

**Watch for** — a second package created instead of a revision; the visibility
silently reset to the default.

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
cinna skills publish <AGENT> acceptance-demo --visibility users \
  --grant <OTHER-email>
cinna skills publish <AGENT> acceptance-demo --version 1.2.0    # no --grant
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
`skills_error`) and nothing else. `publish --json` prints `{revision, package,
catalog_url}`, with `catalog_url` the same link the human output shows.

**Watch for** — Rich table output leaking into the JSON stream; `catalog_url`
absent or built from the reverse-DNS id instead of the package UUID.

### 13. An unresolved agent ref publishes nothing

**Steps**

```bash
cinna skills publish definitely-not-an-agent some-skill
```

**Expected** — non-zero exit listing the available agents; no platform mutation.

**Watch for** — the ref resolved server-side after a publish attempt.

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

## Cleanup

- Delist or delete the acceptance package from the catalog through the web UI
  (the CLI has no delete verb), and revoke the grant to `OTHER`.
- Remove `skills/acceptance-demo/` from the workspace and `cinna sync push
  --agent <slug>`.
- Re-run `cinna skills list <AGENT>` to confirm the row is gone.
