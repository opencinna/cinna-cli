# Improvement Requests — Technical Details

## File locations

- `src/cinna/improve.py` — the whole feature: id resolution, rendering, download
  + extraction, status update.
- `src/cinna/main.py` — the `improve` Click group and its four commands.
- `src/cinna/client.py` — `AccountClient` transport for the four account routes.
- `src/cinna/account.py` — `find_account_root()`, `load_account_config()`,
  `_resolve_account_agent()` reused for `--agent`; `run_account_agents()` renders
  the publisher-install flag the ownership step relies on.
- `src/cinna/sync.py` — `extract_workspace_tarball()` (zip-aware safe extractor)
  reused for the archive.
- `src/cinna/templates/ACCOUNT_CLAUDE.md.template` — orchestrator guide listing
  the verbs, the `improvements/` layout entry, and the workflow section.
- Tests: `tests/test_improve.py` (verbs + transport),
  `tests/test_account.py::test_account_agents_flags_publisher_install`.

## Command surface

- `cinna improve list` → `src/cinna/main.py:improve_list()` →
  `src/cinna/improve.py:run_improve_list()`
- `cinna improve show` → `src/cinna/main.py:improve_show()` →
  `src/cinna/improve.py:run_improve_show()`
- `cinna improve download` → `src/cinna/main.py:improve_download()` →
  `src/cinna/improve.py:run_improve_download()`
- `cinna improve status` → `src/cinna/main.py:improve_status()` →
  `src/cinna/improve.py:run_improve_status()`

## Key functions & flow

- `src/cinna/improve.py:_resolve_request_id()` — full UUID passes through with no
  network call; otherwise one listing call (`limit=200`) and a prefix match, with
  distinct errors for "no match" and "ambiguous".
- `src/cinna/improve.py:_normalize_status()` — lowercases and maps `-` to `_`,
  validating against `IMPROVEMENT_STATUSES` (mirrors the backend tuple) before any
  request is sent.
- `src/cinna/improve.py:_resolve_agent_id()` — `--agent` name/slug/id via
  `src/cinna/account.py:_resolve_account_agent()` against `/account/agents`.
- `src/cinna/improve.py:run_improve_list()` — one table row per request; renders
  bundle id under the installed version, truncates the reported comment to 48
  chars, and colors the status. `--json` prints the raw listing instead.
- `src/cinna/improve.py:_print_context()` — renders the frozen `context` block
  (agent/bundle, SDK, environment, plugins, memory, recipient, scrub count),
  skipping empty values so a standalone agent's context stays short.
- `src/cinna/improve.py:_fix_location_hint()` — derives *where a fix belongs*
  from the context alone (standalone / publisher self-report / consumer report on
  a publisher install / recipient fallback) and prints it under the context table.
  The context describes the requester's install while the request landed on the
  target agent; joining those two facts is left to no one.
- `src/cinna/improve.py:_print_prompts()` — the per-prompt divergence table for
  context schema ≥ 2, driven by `PROMPT_FIELDS`; prints tool availability and a
  warning when `diverged` is set. Prompt *texts* are not printed — they ride in
  the archive under `prompts/`. Absent on a schema-1 context, which still renders.
  Divergence is **tri-state**: `true` diverged, `false` in sync, `null` *not
  compared* (rendered with the context's `divergence_reason`, e.g.
  `platform_managed_no_baseline`). A schema-3 `role` other than
  `published_prompt` is shown under the prompt name.
- `src/cinna/improve.py:_revision_label()` / `_origin_label()` — render the
  installed revision with its `installed_revision_origin`, the
  `latest_published_*` pair (falling back to schema-2 `latest_*`), and a separate
  `head_revision_number` row only when the head is not the published revision.
- `src/cinna/improve.py:_memory_summary()` — one-line personal-memory summary
  (`none (empty)` vs. file count + chars + truncation).
- `src/cinna/improve.py:run_improve_download()` — resolves the id, fetches the
  bytes, computes the destination (`improvements/<short-id>/` under the account
  root, or `--out`), records whether the folder already had contents, then
  delegates to `src/cinna/sync.py:extract_workspace_tarball()` and lists what
  landed.
- `src/cinna/improve.py:_package_state()` — classifies the workspace's context
  package through `src/cinna/account.py:context_package_status()`, reusing the
  listing's already-open client; `context_package_hint()` supplies the nudge
  text. Advisory: any failure degrades to `unreachable` and prints nothing.
- `src/cinna/improve.py:run_improve_status()` — normalizes, patches, echoes the
  resulting status and (when set) the requester-visible note; suggests the closing
  command after an `in_progress` transition.
- `src/cinna/improve.py:_fmt_ts()` / `_truncate()` / `_short_id()` /
  `_status_cell()` — presentation helpers; `SHORT_ID_LEN` (8) is the single source
  of the short-id length used by both the table and the download folder name.

## Config & registry

Nothing is persisted. The feature reads `.cinna/account.json` (platform URL +
account token) through `src/cinna/account.py:load_account_config()` and writes
only the extracted archive under `improvements/<short-id>/` in the account root
(or the `--out` directory). No entry is added to the per-user agent registry.

## External contracts

Platform routes, all authenticated with the account CLI token:

- `GET /api/v1/cli/account/improvement-requests` — query `status`, `agent_id`,
  `skip`, `limit` (server cap 200); returns `{data: [...], count: N}` ordered
  unhandled-first then newest, spanning every agent the account user owns.
- `GET /api/v1/cli/account/improvement-requests/{id}` — detail including the
  `context` block and `session_title`. 404 for ids the account is not party to.
- `GET /api/v1/cli/account/improvement-requests/{id}/archive` — `application/zip`
  body (README, metadata.json, context.json, session/messages.{md,json}, plus
  `prompts/` and `memory/` members when the platform captured them); a
  dedicated route because the JSON-only api-proxy cannot carry a binary body.
  Cross-user downloads are audited server-side.
- `PATCH /api/v1/cli/account/improvement-requests/{id}` — `{status,
  resolution_note}`; recipient-only (403 for a requester, 404 for a stranger).
- `GET /api/v1/cli/account/context-package/version` — `{version}`, the package's
  content version, compared against the local `context/VERSION` stamp. A 404 (a
  backend predating the route) reads as "unreachable", never "stale".

Client methods: `src/cinna/client.py:AccountClient.list_improvement_requests()`,
`get_improvement_request()`, `download_improvement_archive()` (download timeout),
`update_improvement_request()`. All go through `AccountClient._handle_response()`,
so a 401 raises `AuthenticationError` and other failures raise `PlatformError`
with the backend's detail verbatim.

## Edge cases & guardrails

- **No account workspace** — `find_account_root()` raises
  `AccountConfigNotFoundError` before a client is constructed
  (`tests/test_improve.py::test_improve_outside_account_workspace` asserts the
  client is never instantiated).
- **Unknown / ambiguous short id** — both are `ClickException`s that name the
  remedy; `get`/`patch` is never attempted with a guessed id.
- **Unknown status** — refused client-side with the valid vocabulary listed, no
  network call.
- **Unsafe archive members** — absolute paths, `..`, symlinks, and oversized
  entries are skipped by `extract_workspace_tarball()`; the rest of the archive
  still extracts (`tests/test_improve.py::test_improve_download_rejects_unsafe_members`).
- **Re-download** — the destination is not cleared; the message distinguishes
  "Refreshed" from "Extracted" so a stale-folder read is not mistaken for a fresh
  one.
- **`--out` relative paths** resolve against the *current working directory*,
  while the default lands under the *account root* — so the default is stable no
  matter which subdirectory the command runs from.
- **Empty listing** names the active filters in the message rather than printing a
  bare "none", and points at the two ways a user submits a request.
- **`--json`** is the machine-readable path for both `list` and `show`; the Rich
  tables are never parsed by anything.
- **Context schema drift** — the renderer reads blocks defensively: a schema-1
  context (no `prompts` / `memory`) prints without those sections, and a
  zero-length prompt field is omitted rather than shown as an empty row, so
  "this agent never had a refiner prompt" is not misread as "the consumer blanked
  it". Both are covered in `tests/test_improve.py`.
