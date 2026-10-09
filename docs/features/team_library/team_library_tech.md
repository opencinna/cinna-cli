# Team Library — Technical Reference

Implementation of [team_library.md](team_library.md). All logic lives in
`src/cinna/`, tests in `tests/`.

## File locations

- `src/cinna/library.py` — the feature: account/workshop/folder resolution, the
  folder's remote, the fail-loud git helpers, secret refusal, and the
  `run_*` function behind each verb; `checkout_for_import()` for
  `--from-library`.
- `src/cinna/git_versioning.py` — shared plumbing: `RemoteAuth`,
  `remote_auth_env()`, `run_git()` (`auth=`, `extra_env=`),
  `require_token_capable_git()`, `auth_failure_hint()`.
- `src/cinna/client.py` — `AccountClient` library methods.
- `src/cinna/local_import.py` — `run_agent_import_from_library()`, and the
  `contract_from` / `known_agent` parameters of `run_agent_import()`.
- `src/cinna/kit_contract.py` — `find_kit_root()`, `load_layout()`,
  `secret_file_rules()`, `is_secret_filename()` (the secret rule).
- `src/cinna/main.py` — the `cinna library` group and `agent import --from-library`.
- `src/cinna/templates/ACCOUNT_CLAUDE.md.template` — the verbs for assistants.
- Tests: `tests/test_library.py` — a fake cinna-core in a thread (the library
  API plus the real `git http-backend` behind a bearer check), so clone, push,
  fetch and rejection run through a real git client over smart HTTP.

## Command surface

- `cinna library list` → `src/cinna/main.py:library_list()` → `src/cinna/library.py:run_list()`
- `cinna library get` → `src/cinna/main.py:library_get()` → `src/cinna/library.py:run_get()`
- `cinna library share` → `src/cinna/main.py:library_share()` → `src/cinna/library.py:run_share()`
- `cinna library update` → `src/cinna/main.py:library_update()` → `src/cinna/library.py:run_update()`
- `cinna library status` → `src/cinna/main.py:library_status()` → `src/cinna/library.py:run_status()`
- `cinna library grants` → `src/cinna/main.py:library_grants()` → `src/cinna/library.py:run_grants()`
- `cinna library suggest` → `src/cinna/main.py:library_suggest()` → `src/cinna/library.py:run_suggest()`
- `cinna agent import --from-library` → `src/cinna/main.py:agent_import()` →
  `src/cinna/local_import.py:run_agent_import_from_library()`

Every verb's `--json` also calls `src/cinna/main.py:_access_json_mode()`, so a
refusal renders as the JSON error line.

## Key functions & flow

- `src/cinna/library.py:resolve_account()` — account root around the cwd, then
  around the target folder, then the workshop's `Cloud/*/.cinna/account.json`;
  several narrowed by `prefer_url`'s host, else `ambiguous_account`.
- `src/cinna/library.py:local_dir()` / `workshop_root()` — `Local/` beside the
  `.cinna-kit/` found walking up (from the cwd, then the account root), or `--dir`.
- `src/cinna/library.py:resolve_folder()` — the folder holding
  `cinna-agent.json` at/above the cwd, a path, or a slug under `Local/`.
- `src/cinna/library.py:library_remote()` — `origin` URL plus the repository id
  from `cinna.library.repoId` in `.git/config`, else parsed from
  `/git/<id>.git`; `None` for no remote or an external one.
- `src/cinna/library.py:find_local_copy()` — the `Local/` folder holding a
  listed agent: by remote, then by manifest id.
- `src/cinna/library.py:write_local_excludes()` — rewrites cinna's block in
  `.git/info/exclude` (`LOCAL_ONLY` anchored, `cinna.log*` anywhere).
- `src/cinna/library.py:refuse_secrets()` — over `_would_track()`
  (`ls-files --cached --others --exclude-standard`), before any commit.
- `src/cinna/library.py:_share_new()` — `git init` + `HEAD → main`, excludes,
  secret check (undoing a fresh init on refusal), `POST /library/agents`,
  `remote add origin <clone_url>`, set `cinna.library.repoId`, commit all, push.
- `src/cinna/library.py:_share_changes()` — access check (`read_only`), commit
  all if dirty (`-m` required then), fetch, nothing ahead ⇒ `nothing_to_share`,
  else `_push()`.
- `src/cinna/library.py:_push()` — `push origin HEAD:refs/heads/main`; on a
  non-fast-forward: fetch, `_rebase_clean()`, push again (one retry only).
- `src/cinna/library.py:_rebase_clean()` — `rebase origin/main`; on failure
  `rebase --abort` and `diverged_refusal()` (the two options with commands).
- `src/cinna/library.py:update_folder()` — refuse uncommitted, fetch, then
  reset (unborn HEAD) / `merge --ff-only` / `_rebase_clean()`.
- `src/cinna/library.py:status_folder()` — fetch (failure becomes an
  `unreachable` row, not an exception), `_ahead_behind()` via
  `rev-list --left-right --count`, uncommitted count; `_status_words()` renders.
- `src/cinna/library.py:apply_grant_changes()` / `parse_principal()` — merge
  `--add` / `--remove` into the current list and `PUT` the whole list.
- `src/cinna/library.py:build_patch()` — throwaway index (`GIT_INDEX_FILE` via
  `run_git(extra_env=…)`): `read-tree HEAD`, `add -A`,
  `diff --cached --binary origin/main`; the user's index is untouched.
- `src/cinna/library.py:checkout_for_import()` — the `Local/` copy if any, else
  a temporary clone; `LibrarySource.cleanup()` removes the temporary one.
- `src/cinna/local_import.py:run_agent_import_from_library()` — calls
  `run_agent_import()` with `contract_from` (so a temporary clone is read with
  the workshop's kit contract) and `known_agent` from `cloud_install_id`.

## Config & registry

- Nothing is written to `.cinna/config.json` or the agent registry.
- Per folder: `.git/config` gets `remote.origin.url = clone_url` and
  `cinna.library.repoId` (non-secret); `.git/info/exclude` gets cinna's block.
- The token is read from `.cinna/account.json` (`account_token`) at call time.

## External contracts

- `GET /api/v1/library` → `{agents, mirrors}`; `POST /api/v1/library/agents`
  `{slug, name, description}`; `GET /api/v1/library/agents/{repo_id}` (the CLI
  also reads a `grants` list from it — an assumption core must confirm);
  `PUT /api/v1/library/agents/{repo_id}/grants` with the full
  `[{principal, access}]` list (principal `"everyone"`, `{role}`, `{user_id}`,
  or `{email}` — the last an assumption, since §4.2 names only the first three);
  `POST /api/v1/library/agents/{repo_id}/suggestions` `{patch, message}`.
  Client: `src/cinna/client.py:AccountClient.list_library()`,
  `create_library_agent()`, `get_library_agent()`, `set_library_grants()`,
  `suggest_library_change()`; refusals through `_library_response()`
  (`coded=True`; a bare 404 `Not Found` ⇒ `library_unavailable`).
- Git smart HTTP at `clone_url` (`/git/<repo_id>.git`), default branch `main`,
  `401` without a token, `403` without the grant (`read` for upload-pack,
  `write` for receive-pack), non-fast-forward pushes denied.
- git ≥ 2.31 for `GIT_CONFIG_COUNT` (`require_token_capable_git()`).
- Not used yet: a core checkout-from-library route (P7). When it lands,
  `checkout_for_import()` becomes one call to it.

## Edge cases & guardrails (preserve these)

- Token only in the child's environment — `tests/test_library.py:TestRemoteAuthEnv`,
  `assert_no_token_in()` after every clone/share.
- A rejected rebase leaves no `rebase-merge/` and the user's commit intact —
  `test_conflict_is_refused_with_two_options_nothing_lost`.
- One rebase retry on a rejected push, not a loop — `_push()`;
  `test_clean_concurrent_changes_rebase_once`.
- A refused first share creates nothing on the server and removes its fresh
  `.git` — `test_secret_refused_before_anything_leaves`.
- `cinna.log` is written to the cwd (`src/cinna/logging.py:setup_logging()`),
  so it must stay excluded or every share run from inside the folder commits
  it — `test_up_to_date_after`.
- `update --all` reports per folder and exits 1 if any was refused, rather than
  stopping at the first — `run_update()`.
- `suggest` never touches the real index — `test_reader_cannot_share_but_can_suggest`.
- Grants: same principal replaced, not duplicated —
  `test_apply_grant_changes_replaces_same_principal`.
