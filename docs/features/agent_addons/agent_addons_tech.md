# Agent Addons — Technical Reference

## File locations

- `src/cinna/main.py` — the `skills` Click group and its two verbs.
- `src/cinna/account.py` — `run_skills_list()` / `run_skills_publish()` and the
  rendering helpers.
- `src/cinna/client.py` — `AccountClient.get_agent_addons()`,
  `AccountClient.publish_agent_skill()`, `AccountClient.get_skill_package()`,
  and the `_coded_refusal()` helper.
- `src/cinna/errors.py` — `CodedRefusal`, the exception that carries a
  platform-authored code and sentence.
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
  [--notes] [--package-id] [--json]` → `src/cinna/main.py:skills_publish()` →
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
- `src/cinna/account.py:_addon_name_cell()` — leads with the engine-facing
  `name` (the string `publish` takes back), appends `display_name` when it
  differs, then the `· published` / `· orphan` markers.
- `src/cinna/account.py:run_skills_publish()` — publishes, then looks the
  package up for the success block. The lookup is wrapped: the publish has
  already committed, so a failure there degrades the output rather than the
  result.
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

All three routes are ordinary platform routes reached through the account escape
hatch, `POST /api/v1/cli/account/api-proxy`:

- `GET /api/v1/agents/{agent_id}/addons` — the deduplicated projection.
  Consumed fields: `addons[]` (`kind`, `source`, `name`, `display_name`,
  `status`, `status_code`, `orphan`, `can_share`, `published_package_id`),
  `counts` (`plugins`, `skills`, `local_skills`), and `skills_error`.
- `POST /api/v1/agents/{agent_id}/skills/{name}/publish` — body carries only the
  fields the user supplied (`version`, `release_notes`, `visibility`,
  `grant_emails`, `package_id`). Returns the new revision; consumed fields are
  `package_id` (the package's UUID) and `revision_number` / `version`.
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
- **The post-publish lookup cannot fail the command.** It is caught and logged at
  debug level; the catalog URL is still built from the revision's `package_id`.
- **`skills_error` is a warning, not an error.** The plugin half of the list is
  returned regardless, and the CLI says why the skill half is missing rather than
  letting a short list look complete.
- **An unresolved agent ref never reaches a route.** Both verbs resolve through
  the account agent listing first, so a typo cannot publish anything.
