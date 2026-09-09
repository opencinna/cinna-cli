# Agent Addons — Technical Reference

## File locations

- `src/cinna/main.py` — the `skills` Click group and its verbs.
- `src/cinna/account.py` — `run_skills_*()` (list, publish, install, uninstall,
  update, toggle, refresh, catalog, show, revisions, files, grants, grant,
  revoke, visibility, delist), the resolvers (`_resolve_skill_package()`,
  `_resolve_installed_addon()`, `_addon_link_id()`) and the rendering helpers.
- `src/cinna/client.py` — the `AccountClient` skills methods
  (`get_agent_addons()`, `get_skill_publish_preview()`,
  `publish_agent_skill()`, `get_skill_package()`, `install_skill_on_agent()`,
  `uninstall_agent_plugin()`, `upgrade_agent_plugin()`,
  `update_agent_plugin()`, `refresh_agent_skills()`, `refresh_agent_addons()`,
  `list_skill_catalog()`, `get_skill_revision_content()`,
  `get_skill_revision_files()`, `list_skill_grants()`, `grant_skill_access()`,
  `revoke_skill_access()`, `update_skill_package()`, `delist_skill_package()`)
  and the `_coded_refusal()` helper.
- `src/cinna/errors.py` — `CodedRefusal`, the exception that carries a
  platform-authored code and sentence.
- `src/cinna/sync.py` — `ensure_workspace_dirs()`, which guarantees `skills/`
  exists in the local mirror.
- `src/cinna/templates/ACCOUNT_CLAUDE.md.template` — the account workspace's
  command map, which teaches a coding assistant these verbs.
- `tests/test_account.py` — command-level tests for every `skills` verb, plus
  `cinna api`'s elided-id refusal and agent-ref resolution.
- `tests/test_client.py` — client-level tests (proxy path shape, name quoting,
  coded refusals).

## Command surface

- `cinna skills list <agent_ref> [--json]` → `src/cinna/main.py:skills_list()` →
  `src/cinna/account.py:run_skills_list()`.
- `cinna skills publish <agent_ref> <name> [--visibility] [--grant] [--version]
  [--notes] [--package-id] [--dry-run] [--yes] [--json]` →
  `src/cinna/main.py:skills_publish()` →
  `src/cinna/account.py:run_skills_publish()`.
- `cinna skills install <agent_ref> <package> [--revision N]
  [--conversation-only|--building-only] [--json]` → `run_skills_install()`.
- `cinna skills uninstall <agent_ref> <name> [--yes] [--json]` →
  `run_skills_uninstall()`.
- `cinna skills update <agent_ref> <name> [--json]` → `run_skills_update()`.
- `cinna skills toggle <agent_ref> <name> [--enable|--disable]
  [--conversation-mode/--no-conversation-mode]
  [--building-mode/--no-building-mode] [--json]` → `run_skills_toggle()`.
- `cinna skills refresh <agent_ref> [--json]` → `run_skills_refresh()`.
- `cinna skills catalog [--search Q] [--mine] [--json]` → `run_skills_catalog()`.
- `cinna skills show <package> [--revision N] [--json]` → `run_skills_show()`.
- `cinna skills revisions <package> [--json]` → `run_skills_revisions()`.
- `cinna skills files <package> [--revision N] [--json]` → `run_skills_files()`.
- `cinna skills grants <package> [--json]` → `run_skills_grants()`.
- `cinna skills grant <package> --user EMAIL [--json]` → `run_skills_grant()`.
- `cinna skills revoke <package> --user EMAIL [--yes] [--json]` →
  `run_skills_revoke()`.
- `cinna skills visibility <package> <public|private|users> [--json]` →
  `run_skills_visibility()`.
- `cinna skills delist <package> [--yes] [--json]` → `run_skills_delist()`.
- `cinna skills relist <package> [--json]` → `run_skills_relist()`.

## Key functions & flow

- `src/cinna/account.py:run_skills_list()` — resolves the agent ref, fetches the
  projection, prints it (raw JSON under `--json`, mirroring `cinna improve
  list`), and — when one is available — suggests the publish command for the
  first unpublished skill the caller may share.
- `src/cinna/account.py:_print_addons()` — one table row per addon; prints the
  `counts` line and warns when `skills_error` is set, so a partial list is never
  mistaken for a complete one.
- `src/cinna/account.py:_addon_status_cell()` — colours `ok` / `warning` /
  `error` and puts the server's `status_code` in the column: codes are the
  contract, and a code keeps the row one line.
- `src/cinna/account.py:_addon_issue_message()` / `_print_addon_issues()` — the
  platform's own sentence for each flagged row, printed under the table. The row
  carries only a code; the sentence lives on the offending skill
  (`skills[].error.message` / `.warning.message`), so the code locates it there.
  `orphan` and `source_unavailable` are row-level and have no sentence to
  borrow. `secrets` also carries `paths`, listed beneath — which is why this is
  a block under the table and not a table cell.
- `src/cinna/account.py:_esc()` — `rich.markup.escape` for one
  server-authored value. A version is free text and a display name is whatever
  its author typed, so either can hold square brackets, which Rich reads as a
  style tag and swallows (`1.0[beta]` renders as `1.0`). Applied to every
  name / display name / version / package id the addon table and the publish
  output interpolate.
- `src/cinna/account.py:_addon_version_cell()` — the version the agent carries
  (`installed_version`, else `version`) or an empty cell; an absent version
  renders as nothing, never as a bare `v` or a dash, matching the web row's "no
  badge at all". With `has_update` set it appends `→ {latest_version}`, which is
  the difference between seeing and not seeing that a consumer is stale.
- `src/cinna/account.py:_print_pending_update_hint()` — under the table, names
  the stale addons and the `cinna skills update <agent> <name>` that moves the
  first of them. Silent when nothing is stale, so the marker means something.
- `src/cinna/account.py:_addon_source_cell()` — `source`, plus
  `marketplace_name` in parentheses when the payload carries one and it differs.
- `src/cinna/account.py:_resolve_skill_package()` — a package reference (UUID,
  reverse-DNS id, or display name) to its detail payload: a UUID is fetched
  directly, anything else goes through `GET /skills/catalog?search=`, falls back
  to the unfiltered listing (the search index is the server's, and a package it
  does not index under that string may still be listed), then matches exactly
  before substring. Ambiguity and no-match are both sentences naming what was
  found and the command that lists the rest.
- `src/cinna/account.py:_package_uuid()` — the UUID out of a package payload,
  matched by **shape** rather than by key: `package_id` is the reverse-DNS
  string on a package and the UUID on a published revision, so trusting either
  name would silently address the wrong thing.
- `src/cinna/account.py:_resolve_installed_addon()` / `_addon_link_id()` /
  `_require_link_id()` — the addon row for a name, then the plugin-link id from
  it (`link_id` / `plugin_link_id` / `agent_plugin_id`, else the row `key`,
  which the platform composes as `plugin:<link id>`). A row with no link is
  explained rather than attempted: for a `local` skill it is the agent's own
  folder, and there is nothing to uninstall.
- `src/cinna/account.py:_already_installed_message()` — the 409's own sentence
  plus one clause saying whether the install is current or one update behind,
  read from the addons listing. The listing lookup is best-effort: a failure
  costs the clause, not the message.
- `src/cinna/account.py:_rows()` — the list inside a listing envelope
  (`data` / a named key / a bare list). The account routes answer `{"data": …}`;
  the catalog and revision routes are platform routes reached through the hatch
  and answer under their own names.
- `src/cinna/account.py:_revision_rows()` — revisions sorted newest first. The
  question this list answers is "what is the newest"; a payload that happened to
  be ascending would put the answer at the bottom.
- `src/cinna/account.py:_addon_link()` — the row's `link` sub-object. Every
  fact about an *install* rather than a package lives there: `id` (the link id
  the plugin routes address), `installed_version`, `latest_version`,
  `has_update`, `disabled` and the two mode switches. The row's top-level
  `version` mirrors the installed one, which is why a cell rendered from the
  top level cannot tell a stale install from a current one.
- `src/cinna/account.py:_print_plugin_sync()` / `_plugin_link()` — the
  `PluginSyncResponse` every plugin mutation answers with. The link row is
  written and *then* pushed into the agent's running environments, which can
  partly fail (`partial_failures`, `failed_syncs`) while the call still returns
  200; that is reported as a warning, and the link it carries back is what the
  install / update / toggle output reports rather than the request.
- `src/cinna/account.py:_catalog_matches()` — the `--search` filter, applied to
  `package_id` / `name` / `display_name` / `description` **client-side**,
  because the catalog route takes no parameters.
- `src/cinna/account.py:_reject_elided_path()` / `_resolve_api_agent_refs()` —
  `cinna api`'s two ergonomics; see the guardrails below.
- `src/cinna/account.py:_print_version_staleness_hint()` — printed only when a
  *local* skill has no version: on that agent a blank can mean the index was
  built before skills carried versions, and this route is cache-only so the
  backfill has not run. A fully-versioned agent never sees the caveat.
- `src/cinna/account.py:_validate_version_label()` — refuses a `--version`
  carrying a control character (a 422 server-side, and a frontmatter injection
  if it got through) or one that is empty/whitespace-only (silently ignored
  server-side, so the publisher would get a derived version they thought they
  had overridden). Over-length is deliberately left to the server, whose bound
  is the one that can move.
- `src/cinna/account.py:_print_publish_preview()` — renders
  `SkillPublishPreview`: the version with its provenance (`header …`, `latest
  published …`), the package id, why it carries a slug when
  `package_id_disambiguated`, and the next revision number.
- `src/cinna/account.py:_skill_md_carries_version()` — reads the revision's
  stored `frontmatter` to tell the write-back's two outcomes apart, and returns
  `None` when the payload cannot say (no frontmatter reported), so a warning is
  never invented from an absent field.
- `src/cinna/account.py:_addon_name_cell()` — leads with the engine-facing
  `name` (the string `publish` takes back), appends `display_name` when it
  differs, then the `· published` / `· orphan` markers.
- `src/cinna/account.py:run_skills_publish()` — validates `--version` and the
  `--grant` / `--visibility` coupling before any call; under `--dry-run` fetches
  the preview, prints it and returns; at an interactive terminal without
  `--yes`/`--json` fetches the same preview and confirms; then publishes, looks
  the package up for the success block, and reports the write-back's outcome.
  The package lookup is wrapped: the publish has already committed, so a failure
  there degrades the output rather than the result. The *confirmation* preview
  is wrapped for the same class of reason in the other direction — a platform
  without the preview route publishes as before rather than being refused a
  verb it can still perform.
- `src/cinna/client.py:_proxy_json()` — the shared api-proxy call. The `coded`
  flag routes a mirrored 4xx through `_coded_refusal()`.
- `src/cinna/client.py:_coded_refusal()` — builds a `CodedRefusal` from a
  `detail` object carrying `code` / `message` / `paths`; returns `None` for a
  plain-sentence detail so the caller falls back to `PlatformError` rather than
  inventing a code.
- `src/cinna/errors.py:CodedRefusal` — exit code 1 (12 for a 5xx), `code` set to
  the platform's own code, `detail` to its sentence, `paths` appended to the
  message so the offending files are named where the user is reading.
- `src/cinna/account.py:_resolve_one_agent()` — shared agent-ref resolution
  (name, slug, or id) used by both verbs; an unresolved ref fails before any
  route is called.

## Config & registry

Neither verb reads or writes `.cinna/config.json` or the per-user registry. Both
read the account workspace's `.cinna/account.json`: `account_token` for auth and
`frontend_url` to build the catalog link (`{frontend_url}/catalog/skills/{package
uuid}`).

## External contracts

All four routes are ordinary platform routes reached through the account escape
hatch, `POST /api/v1/cli/account/api-proxy`:

- `GET /api/v1/agents/{agent_id}/addons` — the deduplicated projection
  (`AgentAddonsPublic`). Consumed fields: `addons[]` (`AddonPublic`: `key`,
  `kind`, `source`, `name`, `display_name`, `version`, `marketplace_name`,
  `status`, `status_code`, `orphan`, `can_share`, `published_package_id`, and
  **`link`**), `counts` (`plugins`, `skills`, `local_skills`), and
  `skills_error`. `link` is an `AgentPluginLinkWithUpdateInfo`: `id`,
  `installed_version`, `latest_version`, `has_update`, `disabled`,
  `conversation_mode`, `building_mode`, `skill_package_id`. **None of those
  five install facts exist at the top level of the row** — reading `version`
  alone is what made a two-revisions-behind agent look current.
- `GET /api/v1/agents/{agent_id}/skills/{name}/publish-preview` — the version
  and package id a publish would take, from the same code that will take them.
  Consumed fields: `version`, `header_version`, `latest_published_version`,
  `package_id`, `package_id_disambiguated`, `is_republish`,
  `next_revision_number`. It runs the authorization gate and the workspace
  lookup but **not** the content checks, and its values are re-derived inside
  the publish lock — so it is a preview, not a promise, and the CLI says so.
- `POST /api/v1/agents/{agent_id}/skills/{name}/publish` — body carries only the
  fields the user supplied (`version`, `release_notes`, `visibility`,
  `grant_emails`, `package_id`). Returns the new revision; consumed fields are
  `package_id` (the package's UUID), `revision_number`, `version` and
  `frontmatter` (the last one only to tell whether the version reached the
  workspace's `SKILL.md`).
- `GET /api/v1/skills/packages/{package_id}` — consumed fields are the
  reverse-DNS `package_id` string, `display_name`, `visibility`,
  `latest_version` and `revisions[]` (`revision_number`, `version`,
  `created_at`, `release_notes` — **not** `notes` — and `size_bytes`).
- `POST /api/v1/agents/{agent_id}/skills/install` — `SkillInstallRequest`:
  `package_id` (the package **UUID**), optional `revision_number`,
  `conversation_mode`, `building_mode`. Answers `PluginSyncResponse`; 409
  `already_installed` is the coded refusal this route is expected to answer,
  as a **bare** `{code, message}` body.
- `POST|DELETE|PUT /api/v1/llm-plugins/agents/{agent_id}/plugins/{link_id}` —
  `/upgrade` moves the link to the newest revision, `DELETE` removes it, `PUT`
  takes `AgentPluginLinkUpdate` = `{conversation_mode?, building_mode?,
  disabled?}`. All three answer `PluginSyncResponse` (`success`, `message`,
  `plugin_link`, `total_environments`, `failed_syncs`, `partial_failures`).
- `POST /api/v1/agents/{agent_id}/skills/refresh` (→ `AgentSkillsPublic`) and
  `POST /api/v1/agents/{agent_id}/addons/refresh` (→ `AgentAddonsPublic`) —
  rebuild the two caches. The first reports a failure to read the index as
  **200 with `error`**, not as an error status.
- `GET /api/v1/skills/catalog` — **no parameters**; answers
  `SkillPackagesPublic` (`data`, `count`) of `SkillPackageEntry`: `id` (the
  UUID), the reverse-DNS `package_id`, `name`, `display_name`, `description`,
  `visibility`, `is_listed`, `latest_version`, `publisher_name`, `can_manage`.
- `GET /api/v1/skills/packages/{id}/revisions/{n}/content` →
  `SkillRevisionContentPublic` (`content`, `truncated`) and `/files` →
  `SkillRevisionFilesPublic` (`data[]` of `{path, size_bytes}`, `count`,
  `total_size_bytes`, `truncated`). Both caps are reported to the user.
- `GET|POST /api/v1/skills/packages/{id}/grants` —
  `SkillPackageAccessGrantPublic` carries `id` (the **grant row**), `user_id`,
  `user_email`, `created_at`; the POST body is `{email}`.
  `DELETE …/grants/{user_id}`, `PATCH /api/v1/skills/packages/{id}`
  (`SkillPackageUpdate`: `display_name`, `description`, `visibility`,
  `is_listed`), `POST /api/v1/skills/packages/{id}/delist` — the sharing half.
- `GET …/revisions/{n}/download` and `/archive` exist and are **not** used; see
  the guardrail below.

**Why the proxy and not a dedicated `/cli/account/*` verb.** An account CLI JWT
carries the `CLIToken` row id in `sub`, not a user id, so it can never satisfy a
`CurrentUser` dependency on a platform route: a direct call answers 404 "User not
found". The proxy re-dispatches the call as the token's owning user, and its
denylist (credentials, users, admin, cli, auth, streaming) leaves both the
`agents` and `skills` prefixes reachable — every ownership and role check
downstream still runs, unchanged.

## Edge cases & guardrails (preserve these)

- **`grant_emails` must be omitted, never null.** `SkillPublishRequest` declares
  it `list[str] = Field(default=[], max_length=50)` — not nullable — so a `null`
  is a 422. The `if grant_emails:` guard in `publish_agent_skill()` is what
  prevents that. The other four options (`version`, `release_notes`,
  `visibility`, `package_id`) *are* nullable and the server treats null and
  omission identically, so omitting them is tidiness, not semantics.
- **`--grant` is refused without `--visibility users`.**
  `run_skills_publish()` raises before any call. The server accepts the
  combination — `_apply_publish_to_package` writes grants unconditionally — but
  `SkillCatalogService.user_can_see` consults the grant table only for
  `visibility == USERS`, so a private publish with `--grant` succeeds and shares
  nothing. Revisions are immutable, so this must be caught client-side.
- **The skill name is percent-quoted defensively.** Server-side names match
  `^[a-z0-9]+(-[a-z0-9]+)*$`, so today the quoting is a no-op; it is there so a
  future widening of that shape cannot turn a name into a path injection
  unnoticed. It is not a rescue for names the server cannot hold.
- **Publishing reads the cloud workspace.** Nothing in the CLI pushes first, so
  every user-facing surface that names the verb must say so
  (`CLAUDE.md.template`, `ACCOUNT_CLAUDE.md.template`, README) — an unsynced
  edit otherwise lands as an older immutable revision, silently.
- **`--json` here is the per-command raw-payload flag**, the `cinna improve
  list` / `show` shape, not `main.py:json_option()`. The process-wide
  `console.json_mode` is set only by `_json_callback`, which only
  `json_option()` installs, and that decorator is on three commands
  (`account setup` / `set-token` / `status`) — none of them these. So
  `cinna --json skills list` is a Click usage error, not a mixed-output path,
  and the Rich calls in `_print_addons()` cannot bypass a mode that is
  unreachable here.
- **Refusals keep both halves.** `CodedRefusal` exists so the sentence reaches
  the user verbatim *and* the code stays available to a caller. Falling back to
  `PlatformError` would render the detail object as a Python repr.
- **The post-publish lookup cannot fail the command.** It catches **every**
  exception, not just `CinnaExit`: `AccountClient` does not wrap `httpx`, so a
  dropped connection there would reach `CinnaGroup.invoke`, map to
  `NetworkError` and exit 12 — reporting a publish that already committed as a
  network failure, whereupon the user re-runs and appends a second immutable
  revision. It is logged at debug level; the catalog URL is still built from
  the revision's `package_id`.
- **`skills_error` is a warning, not an error.** The plugin half of the list is
  returned regardless, and the CLI says why the skill half is missing rather than
  letting a short list look complete. With `skills_error` set, the empty-list
  line ("carries no plugins or skills yet") is **suppressed**: an agent whose
  addons are all skills lists nothing, and stating that as a fact would be the
  same false completeness in its strongest form.
- **The confirmation must never reach a scripted caller.** The verb shipped
  without one, so `run_skills_publish()` prompts only under
  `console.interactive()` and only without `--yes` / `--json` — `--no-input`,
  a pipe and a JSON driver publish straight away and do not even pay for the
  preview call. Adding a prompt that a piped stdin could block on would turn
  every existing automation into a hang.
- **A failed preview cannot fail a publish.** The preview is a courtesy the
  publish does not depend on; the exception is caught, logged at debug level,
  and the publish proceeds unconfirmed. Under `--dry-run` the same failure *is*
  the command failing, because the preview is the only thing that verb produces.
- **The write-back warning is derived, never guessed.** The platform merges
  `version` into the revision's stored frontmatter *only* when the write into
  `SKILL.md` succeeded, so `version` present on the revision but absent from its
  frontmatter is the degraded path. A response with no frontmatter at all is
  "not reported", not "not written", and prints nothing.
- **Server-authored text is escaped before it reaches Rich.** A version is free
  text; brackets in it would be parsed as markup and silently dropped, so the
  row would report a version nobody published. `_esc()` is the rule for every
  such value in these two verbs — the addon name and display name, the version,
  the package id, the agent's own display name, and the issue block's message
  and **paths**, where `config[dev].env` printed as `config.env` would name a
  file that does not exist as the one blocking a publish.
- **`ensure_workspace_dirs()` creates `skills/`.** The local mirror should hold
  the folder a new skill is written into and the file a publish's version
  write-back lands in, even on an agent that carries no skills yet.
- **An unresolved agent ref never reaches a route.** Every verb resolves through
  the account agent listing first, so a typo cannot publish or install anything.
- **`_package_uuid()` matches on shape, not on a key name.** `package_id` means
  the reverse-DNS string on a package payload and the UUID on a revision
  payload. A helper that trusted the name would address the wrong package
  roughly half the time, and the failure would look like a 404.
- **A local skill is not an install.** `uninstall` / `update` / `toggle` refuse
  a `source: local` row with the reason, and call nothing. The plugin routes
  would answer 404 for it, which reads as a platform fault rather than a
  category error.
- **The link-id fallback reads the row `key`.** `plugin:<link id>` is the shape
  the dedupe rule guarantees; the explicit fields are preferred when present.
  A row with neither is a sentence pointing at `--json`, never a guessed id.
- **A destructive verb under `--json` requires `--yes`.** `uninstall`,
  `revoke` and `delist` ask first, and `_require_yes_for_json()` refuses the
  combination rather than assuming consent: `publish` may skip its confirmation
  under `--json` because it creates, but here the default has to be "don't",
  and printing a prompt into a JSON stream would corrupt it.
- **`_resolve_skill_package()` guarantees an addressable UUID.** `_with_uuid()`
  stamps in the id the package was fetched by when the detail payload does not
  repeat one; otherwise a `None` would reach a URL and fail as a 404 several
  calls later, in a place that has nothing to do with the cause.
- **`toggle` reports the switches it was given, not a cell derived from the
  response.** The PUT is partial, so a payload that omits the untouched switch
  would render as "off" and report a change that did not happen.
- **`update_agent_plugin()` sends only what was named.** The PUT is a partial
  update, so including the mode defaults on an `--enable` would silently reset a
  link whose author had turned building mode off.
- **The `already_installed` refusal keeps the server's code and exit code.** It
  is re-raised as a `CodedRefusal` with the same status and code and only the
  sentence extended, so a `--json` driver still switches on
  `already_installed` while a human gets the next command.
- **`_coded_refusal()` also accepts a bare `{code, message}` body.** The install
  route answers its 409 that way rather than nesting under `detail`; without
  this the one refusal that most needs a sentence would render as a JSON dump.
- **`cinna api` grew the two ergonomics this feature needed** — an elided id is
  refused locally, and an agent reference after `agents/` is resolved — plus
  Rich `overflow="fold"` on every id column so a UUID is never printed
  truncated. Both live in
  [agent_api](../agent_api/agent_api.md#business-rules--guardrails); they are
  noted here because the workflow that exposed them is this one.
- **`cinna skills download` is deliberately absent**, for two independent
  reasons. It is not *reachable*: a revision archive is a binary body and the
  account escape hatch is JSON-only and buffered — the same constraint that
  gives `cinna improve download` a dedicated account route. And it is not
  *wanted*: an install lands the skill in the agent's own workspace, which
  Mutagen brings down to the local mirror, so the bytes arrive through sync
  rather than through a download verb.
- **The install switch is stored negatively.** `AgentPluginLinkUpdate` carries
  `disabled`, not `enabled`, and `update_agent_plugin()` inverts the caller's
  flag. A body carrying `enabled` is not a validation error — it is a field the
  server ignores, so `--disable` would report success and change nothing.
  `tests/test_client.py` pins the exact body for that reason.
- **`--search` / `--mine` filter the rows, not the route.**
  `GET /skills/catalog` takes no parameters; sending them would look like
  filtering and do nothing. `--mine` uses `can_manage`, the server's own answer
  to "is this yours" — a publisher-id comparison would need a user id the
  account token does not carry. Under `--json` the *filtered* rows are printed,
  never the raw envelope: a driver that asked for a subset must not be handed
  the packages it excluded.
- **A refresh that could not read the index answers 200.** The reason arrives in
  `AgentSkillsPublic.error` (`adapter_error`, …), so the verb inspects it and
  warns instead of printing a green check. **The remedy comes from
  `_index_error_remedy(code, ref)`, not from the call site** — one code, one
  remedy, because the codes exist precisely because the fixes differ:
  `env_not_running` → wake it, `adapter_error` → `cinna agent restart-env`,
  `adapter_unsupported` → `cinna agent rebuild-env`, `parse_error` →
  `cinna skills refresh`, unknown → a generic line naming no verb.
  `run_skills_refresh()` and `_print_addons()` (`cinna skills list`) both route
  through it. Each used to hardcode one remedy for every code, so
  `adapter_unsupported` was told to restart an image that cannot grow the route
  — and `skills list` went further and called a *refresh* a "Rebuild", the exact
  conflation this vocabulary exists to prevent, in the read command an LLM
  caller reaches first.
- **A grant's `id` is the grant row, never the user.** Revoking addresses
  `user_id`; falling back to `id` would delete nothing and report success.
- **A plugin mutation can half-succeed.** `PluginSyncResponse` reports the
  environment push separately from the link write, and a 200 with
  `partial_failures` means the catalog and the running agent now disagree —
  reported as a warning rather than absorbed into the success line.
- **Field names the payloads are read for.** `release_notes` (not `notes`),
  `installed_version` / `latest_version` / `has_update` (not `version` alone),
  and `marketplace_name`. Each was in the API and missing from the CLI, which is
  the failure mode this feature exists to close.
