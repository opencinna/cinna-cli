# Agent Addons (`cinna skills`)

## Purpose

See everything an agent carries beyond its prompt — installed plugins and its own
`skills/<name>/` folders — as one list, publish one of those folders to the
instance skills catalog, and run the rest of that lifecycle from the same place:
browse the catalog, install a package on another agent, keep an install current,
and change who may see a package after it was published.

The whole loop — publish → discover → install → re-publish → update — runs
without `cinna api`, and without anyone typing a UUID.

## Mental model / core concepts

- **Skill** — a `skills/<name>/` folder in the agent's workspace holding a
  `SKILL.md` (plus any files it needs) that the engine loads *on demand*, when
  the conversation calls for it. It is a unit of behavior with its own trigger
  and its own output — not a synonym for "something the agent can do". A
  capability implemented as `scripts/<capability>/run.py` is a script, not a
  skill.
- **Plugin** — an installed package from a marketplace, a bundle, or the skills
  catalog. It lands under the agent's `plugins/` tree.
- **Addon** — the umbrella over both. It exists because the two overlap: a skill
  installed from the catalog is *simultaneously* a plugin link and an entry in
  the environment's skill index, and listing it twice would be wrong. The
  platform owns the dedupe rule and serves one projection; the CLI renders it.
- **Package / revision** — publishing creates a catalog *package* (identified by
  a reverse-DNS id such as `com.acme.report`) whose *revisions* are immutable.
  Re-publishing the same skill appends a revision; it never rewrites one.
- **Version** — a line in the skill's own `SKILL.md` frontmatter, not a field of
  a database row. It is free text (one line, at most 64 characters), and the
  platform *derives* it at publish time rather than asking: the header's version
  if it has not been published yet, otherwise the next one after the newest
  release. The resolved value is written back into `SKILL.md` before the
  snapshot, so the published bytes carry their own version and the next publish
  reads it back. A skill nobody has versioned starts at `1.0.0`.
- **Local vs remote** — the skill folders live in the synced workspace, but the
  list is read from the **platform's cache** of the environment's skill index,
  not from local disk. What `cinna skills list` shows is what the engine sees,
  which is the question worth asking after an edit.
- **Install / plugin link** — installing a catalog package onto an agent creates
  a *link* row tying one revision to one agent. That is why an install can be
  uninstalled, upgraded and toggled while the package it came from is untouched:
  what the agent holds is a copy, not a reference. The link has an id of its own,
  which the CLI resolves from the addon's name and never asks anyone to type.
- **Three versions, not one** — an installed addon has a version it *carries*
  (`installed_version`), a version the catalog *now holds* (`latest_version`),
  and a flag saying they differ (`has_update`). All three live on the row's
  `link`, not at its top level, where only the installed one appears; a row
  rendered from the top level would make an agent two revisions behind look
  exactly like a current one.
- **Grant** — a named person on a package. Grants are stored on any package but
  only consulted on one whose visibility is `users`; every other visibility
  ignores the list entirely.
- **Delist / relist** — hiding a package from the catalog, and putting it back.
  Delisting does not reach the agents that already installed it: they hold
  copies.
- **Refresh** — the one verb here that talks to the environment. Everything else
  in this group reads the platform's cache, which is what makes it safe to run
  against a sleeping agent — and what makes a stale index possible.

## User flows

### See what an agent carries

1. Run `cinna skills list <agent>` from the account workspace.
2. One row per addon: kind (`plugin` / `skill`), source (`marketplace`,
   `bundle`, `catalog`, `local`), status, name, and version.
3. A blank version column is a fact, not a failure — the skill's header carries
   none. On an environment built before skills carried versions it can also mean
   the index has not been refreshed yet, which the CLI says beneath the table
   whenever a local skill's version is missing.
4. `· published` marks a local skill that already has a catalog package;
   `· orphan` marks a plugin directory the engine still loads whose link is
   gone.
5. When the skill half could not be read, the plugin rows still list and the
   reason is stated (`env_not_running`, `adapter_error`, `parse_error`).

### Publish a skill to the catalog

1. Author the skill in the synced workspace (`skills/<name>/SKILL.md`), give it
   a description written for discovery, and make sure nothing secret sits in the
   folder.
2. `cinna sync push --agent <agent>` — publishing reads the **cloud** workspace,
   so an unsynced edit would be left out of an immutable revision.
3. `cinna skills list <agent>` to confirm the skill is there and its status is
   clean.
4. Optionally `cinna skills publish <agent> <name> --dry-run` — the version, the
   package id and the revision number a publish would take, taken from the same
   code that will take them, with nothing published.
5. `cinna skills publish <agent> <name> --visibility public --notes "…"`. At a
   terminal the same preview is shown and confirmed before the press.
6. The success block prints the package id, the revision and its version, the
   visibility that stuck, and the catalog URL — plus a line saying that
   `skills/<name>/SKILL.md` now carries the version, since a file in the synced
   workspace changed without the user editing it.

### Install a published skill on another agent

1. `cinna skills catalog --search jokes` — the package id, display name,
   visibility and newest version of everything this account may see. The
   reverse-DNS package id (`localhost.skill.dad-jokes`) is what the next command
   takes; nobody needs the UUID underneath it.
2. `cinna skills show <package>` for the revisions and the newest revision's
   `SKILL.md` before committing an agent to it.
3. `cinna skills install <consumer-agent> <package>` — the newest revision
   unless `--revision N` names another. `--conversation-only` /
   `--building-only` narrow where the skill is offered; by default it is both.
4. `cinna skills list <consumer-agent>` shows the row with the version it now
   carries.
5. Installing something the agent already has is answered as a sentence, not a
   409 body: it says so, and points at `cinna skills update` when a newer
   revision is waiting.

### Keep an install current

1. `cinna skills list <consumer-agent>`. A row whose catalog has moved reads
   `1.0.0 → 1.1.0`, and the line under the table names the update command.
2. `cinna skills update <consumer-agent> <name>` moves the link to the newest
   revision and prints the version it moved from and to.
3. `cinna skills uninstall <consumer-agent> <name>` removes the agent's copy;
   the package and its revisions are untouched.
4. `cinna skills toggle <consumer-agent> <name> --disable` (or
   `--no-building-mode`, …) changes where an install is offered without removing
   it. Every switch left unnamed keeps its stored value.

### Recover from a stale or unreadable index

1. `cinna skills list <agent>` reports `adapter_error` / `env_not_running` /
   `parse_error`, or shows a local skill with no version although its `SKILL.md`
   has one. Both are the cache, not the agent.
2. `cinna skills refresh <agent>` — the only verb here that reaches the
   environment. It rebuilds the skill index and, where the platform has it, the
   plugin half too, and reports how many skills were indexed.
3. If the index still cannot be read, the refresh says so rather than claiming
   success — and names the remedy *that reason code* has, because the four codes
   do not share one. Both `list` and `refresh` read the same table:
   `env_not_running` → wake it (send it a message, or refresh again);
   `adapter_error` → `cinna agent restart-env <agent>`, the adapter is not
   answering; `adapter_unsupported` → `cinna agent rebuild-env <agent>`, the
   container predates agent skills and has no skills endpoint, so neither a
   refresh nor a restart of the same image can ever reach one; `parse_error` →
   `cinna skills refresh <agent>`, the environment answered but the index did
   not parse. An unrecognised code names **no** verb — guessing one sends the
   caller round a loop that cannot close.
4. `cinna skills list <agent>` again.

### Share a private skill with named people

1. `cinna skills publish <agent> <name> --visibility users --grant a@x.com
   --grant b@x.com`. The `--visibility users` is required, not optional
   decoration — see the rules below.
2. Each address is resolved to a platform user in the same transaction as the
   publish.
3. A later publish adds more addresses; it never takes access away.

### Change sharing after the fact

Visibility and grants belong to the *package*, not to a revision, so they can
change without cutting a new one:

1. `cinna skills grants <package>` — who is named, and the visibility that
   decides whether that list is consulted at all.
2. `cinna skills grant <package> --user ana@example.com` /
   `cinna skills revoke <package> --user ana@example.com`.
3. `cinna skills visibility <package> users|public|private`.
4. `cinna skills delist <package>` takes it out of the catalog and
   `cinna skills relist <package>` puts it back; agents that already installed
   it keep what they have either way.

## Business rules

- **Publishing is gated** by the `agent-developer` role on an agent that is not a
  foreign (consumer) install — the same gate as publishing a bundle. A refusal is
  `not_developer` or `foreign_install`.
- **The content must be clean.** A skill that fails to parse, exceeds the size
  cap, or contains files that look like key material is refused
  (`skill_invalid`, `skill_too_large`, `skill_contains_secrets` — the last one
  names the offending files).
- **The environment must exist**, but need not be awake. `no_environment` and
  `workspace_unavailable` are the two refusals here.
- **The version is derived, not typed.** The platform reads `version:` from the
  skill's `SKILL.md`, continues the series past the newest release (incrementing
  the last run of digits: `1.0.0`→`1.0.1`, `1.2`→`1.3`, `v3`→`v4`), and starts
  at `1.0.0` only when there is nothing to continue from. `--version` overrides
  that for one revision, verbatim and without de-duplication — two revisions may
  legitimately carry one version. The CLI refuses a `--version` that carries a
  newline or any other control character before the call (the value is written
  into a frontmatter block, where a newline would inject top-level keys into the
  author's header), and refuses an empty one, which the server would ignore in
  favour of the derived version — an override that silently is not one.
- **Publishing edits the workspace.** The resolved version is written into
  `skills/<name>/SKILL.md` on the environment before the snapshot is taken. A
  write that cannot happen (a read-only workspace, a file with no frontmatter
  fence) is not fatal: the revision still carries the version, but its stored
  frontmatter then omits it — and the CLI warns, because the header and the
  catalog have diverged and the next publish will continue from the catalog.
- **The package id is derived too.** Omitting `--package-id` takes
  `<reversed host>.skill.<name>`; when that is already taken on the instance the
  publisher's own slug is appended rather than the publish being refused.
  `--dry-run` says which of those happened before the press, since a hex tail in
  your own package id has no other explanation.
- **The package id is immutable.** `--package-id` applies to a first publish; on
  a re-publish it may only repeat the existing id, and a mismatch is refused
  (`package_id_immutable`) rather than ignored — every install and container
  manifest references that id.
- **Visibility is sticky.** Omitting `--visibility` on a re-publish leaves the
  package's current visibility alone; it is only set on a first publish or an
  explicit change.
- **A grant only means anything on a `users` package.** The server stores a
  grant whatever the visibility, but only a `users` package consults its grant
  list — so a private publish with `--grant` would succeed and share nothing.
  The CLI refuses `--grant` unless `--visibility users` is named, before the
  call: a revision is immutable, so a publish that lies about its audience costs
  a permanent extra one to correct. The web dialog gets this for free (its
  people picker only renders under `users`); the CLI has to say it.
- **Grants are additive and atomic.** Within a `users` package `--grant` adds;
  it never revokes (revoking is its own verb in the web UI, so a stale
  invocation cannot silently remove access). An unknown address fails the whole
  publish rather than half-sharing it.
- **Publishing reads the cloud workspace.** The files come from the agent's
  remote workspace on disk — which is why a suspended environment publishes
  normally — so an unsynced local edit is simply not in the revision. Push
  first; the revision cannot be rewritten afterwards.
- **Refusals are printed verbatim.** The platform answers these with a machine
  code *and* its own sentence; the CLI prints the sentence as written and adopts
  the code as its own `--json` error code. Two places wording the same refusal
  would be two places to keep true.
- **Listing is read-only and cache-only.** It never wakes a container, so it is
  safe to run against a sleeping environment and safe to run repeatedly. The
  price is staleness, and `cinna skills refresh` is the remedy — the only verb
  in this group that reaches the environment.
- **A pending update is a visible fact.** `has_update` puts an arrow in the
  Version column and names the update command under the table. An agent whose
  installs are all current says nothing, so the marker means something when it
  appears.
- **Already installed is a refusal with a next step.** Installing a package the
  agent carries answers 409 `already_installed`. The CLI prints the server's
  sentence and adds which state the agent is in — at the newest revision, or one
  `cinna skills update` behind — because the user's intent has already been
  satisfied and the only open question is what to do now.
- **Only an install can be uninstalled.** An agent's own `skills/<name>/` folder
  is part of its workspace, not a link; `uninstall`, `update` and `toggle` say
  so and call nothing rather than 404ing on a route that could never match.
- **Toggling is partial.** `--enable/--disable` and the two mode switches
  default to "leave it alone", so disabling an install cannot silently reset the
  modes a previous call set. A disabled install still lists, still reports its
  version and is not an error, so the list marks it `· disabled` — nothing else
  in the row would say the engine is not loading it.
- **A refresh that changed nothing says so.** The route answers 200 even when
  the index still could not be read, so the verb reports the reason and points
  at restarting the environment. A green check there would send the reader back
  to `list` to discover for themselves that nothing moved.
- **A change to a link is not the same as a change to a running agent.** Every
  install / uninstall / update / toggle also pushes into the agent's live
  environments, and that push can partly fail while the call succeeds. When it
  does, the CLI says which environments did not take it.
- **A grant on the wrong visibility is a warning, not a refusal.** After the
  fact, `grant` and `grants` still work on a private or public package (the row
  is real), but they say the list is ignored and name the command that would
  make it count. Only at *publish* time is the combination refused outright —
  there the mistake costs a permanent revision.
- **`users` with nobody named shares with nobody.** Switching visibility to
  `users` on a package with an empty grant list is the one setting that looks
  like sharing and is not, so it says so.
- **Delisting does not reach installed copies.** A revision an agent holds is a
  copy; hiding the package from discovery leaves every install working.
- **References, not ids, everywhere.** An agent is a name, slug or id; a package
  is a reverse-DNS id, a display name or a UUID; an installed skill is the name
  `cinna skills list` prints. The link id and the package UUID exist and are
  resolved internally.

## Architecture overview

```
cinna skills list <agent>
  → account.py:run_skills_list()
    → AccountClient.get_agent_addons()
      → api-proxy → GET /api/v1/agents/{id}/addons   (the server's projection)
    → account.py:_print_addons()                      (one row per addon)

cinna skills publish <agent> <name> [--dry-run]
  → account.py:run_skills_publish()
    → AccountClient.get_skill_publish_preview()       (--dry-run, or to confirm)
      → api-proxy → GET /api/v1/agents/{id}/skills/{name}/publish-preview
    → AccountClient.publish_agent_skill()
      → api-proxy → POST /api/v1/agents/{id}/skills/{name}/publish
    → AccountClient.get_skill_package()               (for the catalog line)
      → api-proxy → GET /api/v1/skills/packages/{id}

cinna skills install <agent> <package> [--revision N]
  → account.py:run_skills_install()
    → _resolve_skill_package()                        (name / id → package UUID)
      → api-proxy → GET /api/v1/skills/catalog, GET /skills/packages/{id}
    → AccountClient.install_skill_on_agent()
      → api-proxy → POST /api/v1/agents/{id}/skills/install
      → 409 already_installed → the server's sentence + the update command

cinna skills update|uninstall|toggle <agent> <name>
  → account.py:run_skills_update() / _uninstall() / _toggle()
    → get_agent_addons() → _resolve_installed_addon() → _addon_link_id()
      → api-proxy → POST   /api/v1/llm-plugins/agents/{id}/plugins/{link}/upgrade
                  → DELETE /api/v1/llm-plugins/agents/{id}/plugins/{link}
                  → PUT    /api/v1/llm-plugins/agents/{id}/plugins/{link}

cinna skills refresh <agent>
  → account.py:run_skills_refresh()
    → api-proxy → POST /api/v1/agents/{id}/skills/refresh
                → POST /api/v1/agents/{id}/addons/refresh   (best effort)

cinna skills catalog|show|revisions|files [<package>]
  → account.py:run_skills_catalog() / _show() / _revisions() / _files()
    → api-proxy → GET /api/v1/skills/catalog
                → GET /api/v1/skills/packages/{id}
                → GET /api/v1/skills/packages/{id}/revisions/{n}/content|files

cinna skills grants|grant|revoke|visibility|delist|relist <package>
  → account.py:run_skills_grants() / _grant() / _revoke() / _visibility()
                                   / _delist() / _relist()
    → api-proxy → GET|POST /api/v1/skills/packages/{id}/grants
                → DELETE   /api/v1/skills/packages/{id}/grants/{user_id}
                → PATCH    /api/v1/skills/packages/{id}   (visibility, relist)
                → POST     /api/v1/skills/packages/{id}/delist
```

Every verb rides the account escape hatch rather than a dedicated
`/cli/account/*` route: these are ordinary platform routes, and an account token
cannot satisfy a user-scoped dependency directly. See the tech doc for why.

There is deliberately no `download`. An install lands the skill in the agent's
own workspace, which sync brings down to the local mirror — the bytes arrive
that way, not through a verb. (The archive routes are also binary, which the
JSON-only hatch could not carry in any case.)

## Integration points

- [Account workspace](../account_workspace/account_workspace.md) — the account
  token, agent-ref resolution, and the api-proxy these verbs ride.
- [Live sync](../live_sync/live_sync.md) — a skill is published from the files
  the environment has, so push before publishing; and the version write-back
  means a publish changes a workspace file that a sync will bring back down.
- [Agent management](../agent_management/agent_management.md) — `cinna agent
  show` answers the neighbouring question ("is what I edited actually live?").
- [Agent API](../agent_api/agent_api.md) — the other way one agent's capability
  reaches another: a typed HTTP surface rather than a folder someone installs.
