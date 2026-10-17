# Artifacts — Technical Details

## File locations

- `src/cinna/artifacts.py` — the `cinna artifacts` Click group, the exclusion
  list and walk, the zip writer, the path memory, the person-only guard.
- `src/cinna/client.py` — `AccountClient` artifact methods (api-proxy JSON verbs
  and the dedicated multipart / download / link routes).
- `src/cinna/main.py` — wires the group with `cli.add_command` and attaches
  `--json` / `--no-input` to every leaf (`_attach_driver_options()`).
- `src/cinna/account.py:_write_account_claude_settings()` — writes or merges the
  Claude Code deny rules.
- `src/cinna/templates/ACCOUNT_CLAUDE.md.template` — the orchestrator guide's
  "Artifacts" table.
- `tests/test_artifacts.py` — exclusions, the archive writer, memory, the guard,
  every verb against a respx-mocked platform; `tests/test_account.py` — the
  settings deny rules.

## Command surface

- `cinna artifacts publish` — `src/cinna/artifacts.py:publish()`
- `cinna artifacts list` — `src/cinna/artifacts.py:list_cmd()`
- `cinna artifacts show` — `src/cinna/artifacts.py:show()`
- `cinna artifacts open` — `src/cinna/artifacts.py:open_cmd()`
- `cinna artifacts download` — `src/cinna/artifacts.py:download()`
- `cinna artifacts share` — `src/cinna/artifacts.py:share()`
- `cinna artifacts link create` — `src/cinna/artifacts.py:link_create()`
- `cinna artifacts link revoke` — `src/cinna/artifacts.py:link_revoke()`
- `cinna artifacts delete` — `src/cinna/artifacts.py:delete()`

## Key functions & flow

- `src/cinna/artifacts.py:_session()` — loads the account config, opens an
  `AccountClient`, calls `get_artifacts_capabilities()`; 404/405 or a version
  other than `SUPPORTED_VERSION` raises `_unsupported()` (code
  `artifacts_unsupported`). Yields `_Session` with the capabilities' `limits`.
- `src/cinna/artifacts.py:refuse_excluded_source()` — every segment of the
  resolved path is checked against the exclusions before the walk.
- `src/cinna/artifacts.py:collect_files()` — `os.walk(followlinks=False)`;
  prunes excluded directories (recorded as `name/`), records symlinks and
  special files; a single file is published under its own name.
- `src/cinna/artifacts.py:is_artifact_excluded()` — `kit_contract.is_excluded()`
  over the casefolded path against `ARTIFACT_EXCLUDE`.
- `src/cinna/artifacts.py:_check_limits()` — file count, per-file and total size
  against the advertised limits, before zipping.
- `src/cinna/artifacts.py:write_archive()` — zip into a temp dir;
  `_deflate_ratio()` decides per entry whether to store instead of deflate.
- `src/cinna/artifacts.py:ArtifactMemory` — `lookup()` / `remember()` /
  `forget()` over `.cinna/artifacts.json`.
- `src/cinna/artifacts.py:_list_all()` — pages `list_artifacts()` (200 per
  page, the server's cap) until `count` is reached; used by `list` and by
  `src/cinna/artifacts.py:_resolve_id()`, where a full UUID passes through and a
  prefix is matched against both scopes.
- `src/cinna/artifacts.py:require_person()` / `person_only_deny_rules()` — both
  read `PERSON_ONLY_VERBS`.
- `src/cinna/artifacts.py:_user_id_for()` — exact (casefolded) email match over
  the fuzzy `search_users()` result (25, the server's cap). `share
  --remove-user` falls back to it when a grant carries no email.
- `src/cinna/artifacts.py:parse_expiry()` — `12h` / `7d` / `2w` / `never`.
- `src/cinna/artifacts.py:extract_archive()` — refuses absolute paths, `..`,
  symlink entries and anything resolving outside the destination; every entry is
  checked before the first is written, so a refusal leaves nothing behind.

## Config & registry

- `.cinna/artifacts.json` in the account root <!-- nocheck --> —
  `{"version": 1, "published": {"<absolute path>": {artifact_id, platform_url,
  title, published_at}}}`. Written atomically (temp file + replace). An entry
  whose `platform_url` differs from the workspace's is ignored.
- `.claude/settings.json` in the account root <!-- nocheck --> —
  `permissions.deny` gains `Bash(cinna artifacts share:*)` and
  `Bash(cinna artifacts link create:*)`.
- Nothing is written to `.cinna/config.json` or the per-user agent registry.

## External contracts

- `GET /api/v1/artifacts/capabilities` (api-proxy) — `{version: 1, limits:
  {max_revision_bytes, max_revision_files, max_file_bytes, user_quota_bytes}}`.
- `POST /api/v1/cli/account/artifacts` — multipart `archive` + `title`,
  `description`, `entry_path`, `source_kind=cli`, `source_label`
  (`<machine> · <folder>`), `user_workspace_id` → `ArtifactPublished`
  (`id`, `url`, `revision_number`, `kind`, `excluded`, …).
- `POST /api/v1/cli/account/artifacts/{id}/revisions` — multipart `archive` +
  optional `title`, `description`, `entry_path`, `note`; owner only.
- `GET /api/v1/cli/account/artifacts/{id}/download?rev=` — zip, streamed to a
  temp file.
- `POST /api/v1/cli/account/artifacts/{id}/links` — `{label, expires_at,
  with_code, pinned_revision_id}` → `ArtifactLinkCreated` with `token`, `url`,
  `code`.
- api-proxy: `GET artifacts?scope=mine|shared_with_me`, `GET artifacts/{id}`
  (links' `code` blanked by the proxy), `PATCH artifacts/{id}` (`visibility`),
  `PUT artifacts/{id}/grants` (full list of `{principal: {user_id}, access:
  "view"}`), `DELETE artifacts/{id}/links/{link_id}`, `DELETE artifacts/{id}`,
  `GET users/search?q=&include_self=true`. The proxy refuses
  `POST artifacts/{id}/links`.
- Core's ingest rejects a zip entry whose compression ratio exceeds
  `ARTIFACT_MAX_COMPRESSION_RATIO` (100 by default) — the CLI's 50 threshold
  keeps under it.

## Edge cases & guardrails

- The kit pattern engine's `**/` does not match zero segments in front of a
  folder pattern, so `ARTIFACT_EXCLUDE` lists every name both bare and under
  `**/` (`src/cinna/artifacts.py:_EXCLUDED_ANYWHERE`). Found live: `.git/` at
  the root reached core before this.
- `ARTIFACT_EXCLUDE` is not `kit_contract.DEFAULT_EXCLUDE`: that list drops
  `README.md`, `CLAUDE.md` and `temp/`, which are legitimate artifact content.
- The memory key is `Path.resolve()` of the argument, so `./q3`, `q3/` and an
  absolute path are the same source.
- A 404 on a republish from memory forgets the entry and creates
  (`publish()`); the same 404 on `--update` propagates.
- `require_person()` reads `console.json_mode`, `console.no_input` and
  `_stdin_is_tty()` (patchable in tests); the root `--no-input`, the per-command
  `--no-input` and `CINNA_NO_INPUT` all set `console.no_input`.
- Claude Code deny rules match by command prefix, so `cinna artifacts --json
  share …` or `cinna -v artifacts share …` pass them; those are refused by
  `require_person()` (Claude Code's Bash tool has no TTY on stdin). Verified
  with `claude -p` in a generated workspace: the two prefixes report
  "Permission … has been denied".
- `_write_account_claude_settings()` merges only into a JSON object whose
  `permissions` / `deny` are an object / list; anything else is left untouched.
  A user who deletes the rules gets them back on the next `refresh-context`.
- Rich markup in server text is escaped (`escape()`), including the
  `[code, pinned to rev N]` tag of a link line.
