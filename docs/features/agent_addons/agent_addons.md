# Agent Addons (`cinna skills list`, `cinna skills publish`)

## Purpose

See everything an agent carries beyond its prompt — installed plugins and its own
`skills/<name>/` folders — as one list, and publish one of those folders to the
instance skills catalog so other agents can install it.

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
- **Local vs remote** — the skill folders live in the synced workspace, but the
  list is read from the **platform's cache** of the environment's skill index,
  not from local disk. What `cinna skills list` shows is what the engine sees,
  which is the question worth asking after an edit.

## User flows

### See what an agent carries

1. Run `cinna skills list <agent>` from the account workspace.
2. One row per addon: kind (`plugin` / `skill`), source (`marketplace`,
   `bundle`, `catalog`, `local`), status, and name.
3. `· published` marks a local skill that already has a catalog package;
   `· orphan` marks a plugin directory the engine still loads whose link is
   gone.
4. When the skill half could not be read, the plugin rows still list and the
   reason is stated (`env_not_running`, `adapter_error`, `parse_error`).

### Publish a skill to the catalog

1. Author the skill in the synced workspace (`skills/<name>/SKILL.md`), give it
   a description written for discovery, and make sure nothing secret sits in the
   folder.
2. `cinna sync push --agent <agent>` — publishing reads the **cloud** workspace,
   so an unsynced edit would be left out of an immutable revision.
3. `cinna skills list <agent>` to confirm the skill is there and its status is
   clean.
4. `cinna skills publish <agent> <name> --version 1.0.0 --notes "…"`.
5. The success block prints the package id, the revision, the visibility that
   stuck, and the catalog URL.

### Share a private skill with named people

1. `cinna skills publish <agent> <name> --visibility users --grant a@x.com
   --grant b@x.com`. The `--visibility users` is required, not optional
   decoration — see the rules below.
2. Each address is resolved to a platform user in the same transaction as the
   publish.
3. A later publish adds more addresses; it never takes access away.

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
  safe to run against a sleeping environment and safe to run repeatedly.

## Architecture overview

```
cinna skills list <agent>
  → account.py:run_skills_list()
    → AccountClient.get_agent_addons()
      → api-proxy → GET /api/v1/agents/{id}/addons   (the server's projection)
    → account.py:_print_addons()                      (one row per addon)

cinna skills publish <agent> <name>
  → account.py:run_skills_publish()
    → AccountClient.publish_agent_skill()
      → api-proxy → POST /api/v1/agents/{id}/skills/{name}/publish
    → AccountClient.get_skill_package()               (for the catalog line)
      → api-proxy → GET /api/v1/skills/packages/{id}
```

Both verbs ride the account escape hatch rather than a dedicated `/cli/account/*`
route: these are ordinary platform routes, and an account token cannot satisfy a
user-scoped dependency directly. See the tech doc for why.

## Integration points

- [Account workspace](../account_workspace/account_workspace.md) — the account
  token, agent-ref resolution, and the api-proxy these verbs ride.
- [Live sync](../live_sync/live_sync.md) — a skill is published from the files
  the environment has, so push before publishing.
- [Agent management](../agent_management/agent_management.md) — `cinna agent
  show` answers the neighbouring question ("is what I edited actually live?").
- [Agent API](../agent_api/agent_api.md) — the other way one agent's capability
  reaches another: a typed HTTP surface rather than a folder someone installs.
