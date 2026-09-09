# Agent Addons — Technical Reference

## File locations

- `src/cinna/main.py` — the `skills` Click group and its two verbs.
- `src/cinna/account.py` — `run_skills_list()` / `run_skills_publish()` and the
  rendering helpers.
- `src/cinna/client.py` — `AccountClient.get_agent_addons()`,
  `AccountClient.get_skill_publish_preview()`,
  `AccountClient.publish_agent_skill()`, `AccountClient.get_skill_package()`,
  and the `_coded_refusal()` helper.
- `src/cinna/errors.py` — `CodedRefusal`, the exception that carries a
  platform-authored code and sentence.
- `src/cinna/sync.py` — `ensure_workspace_dirs()`, which guarantees `skills/`
  exists in the local mirror.
- `src/cinna/templates/ACCOUNT_CLAUDE.md.template` — the account workspace's
  command map, which teaches a coding assistant these verbs.
- `tests/test_account.py` — command-level tests (`skills list` / `skills
  publish`).
- `tests/test_client.py` — client-level tests (proxy path shape, name quoting,
  coded refusals).

## Command surface

- `cinna skills list <agent_ref> [--json]` → `src/cinna/main.py:skills_list()` →
  `src/cinna/account.py:run_skills_list()`.
- `cinna skills publish <agent_ref> <name> [--visibility] [--grant] [--version]
  [--notes] [--package-id] [--dry-run] [--yes] [--json]` →
  `src/cinna/main.py:skills_publish()` →
  `src/cinna/account.py:run_skills_publish()`.

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
- `src/cinna/account.py:_addon_version_cell()` — the addon's `version` or an
  empty cell; an absent version renders as nothing, never as a bare `v` or a
  dash, matching the web row's "no badge at all".
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

- `GET /api/v1/agents/{agent_id}/addons` — the deduplicated projection.
  Consumed fields: `addons[]` (`kind`, `source`, `name`, `display_name`,
  `version`, `status`, `status_code`, `orphan`, `can_share`,
  `published_package_id`), `counts` (`plugins`, `skills`, `local_skills`), and
  `skills_error`.
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
  reverse-DNS `package_id` string and `visibility`.

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
- **An unresolved agent ref never reaches a route.** Both verbs resolve through
  the account agent listing first, so a typo cannot publish anything.
