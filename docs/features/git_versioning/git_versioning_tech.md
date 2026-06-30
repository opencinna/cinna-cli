# Git Versioning — Technical Reference

Implementation of [git_versioning.md](git_versioning.md). cinna-cli is a Python
CLI; all logic lives in `src/cinna/`, tests in `tests/`.

## File locations

- `src/cinna/git_versioning.py` — the feature core: coordinates model, the link
  sequence, and the `commit/push/pull/status/log/checkout/unlink` wrappers.
- `src/cinna/client.py` — `PlatformClient.get_git_coordinates()` (the discovery
  endpoint).
- `src/cinna/config.py` — `GitLayout` dataclass, the Model-A layout helpers, and
  the registry git block.
- `src/cinna/main.py` — the `cinna git` command group.
- `src/cinna/bootstrap.py` — layout decision + auto-link during `cinna setup`.
- `src/cinna/account.py` — same for `cinna agent sync`, plus child-workspace
  discovery across the nested layout.
- `src/cinna/context.py` — the conditional `GIT_VERSIONING.md` guide shipped into
  each checkout and its pointer in `CLAUDE.md`.
- Tests: `tests/test_git_versioning.py` (link/commit/push/pull/checkout/unlink,
  restore, excludes, fileMode, multi-segment, CLI wiring),
  `tests/test_config.py` (registry git-block preserve/clear, `GitLayout`
  round-trip), `tests/test_account.py` (multi-segment subdir resolution, nested
  sync layout), `tests/test_context.py` (guide writing + conditional pointer).

## Command surface

Each `cinna git` verb → its handler in `src/cinna/main.py`:

- `cinna git link` → `src/cinna/main.py:git_link()`
- `cinna git status` → `src/cinna/main.py:git_status()`
- `cinna git commit` → `src/cinna/main.py:git_commit()`
- `cinna git push` → `src/cinna/main.py:git_push()`
- `cinna git pull` → `src/cinna/main.py:git_pull()`
- `cinna git log` → `src/cinna/main.py:git_log()`
- `cinna git checkout` → `src/cinna/main.py:git_checkout()`
- `cinna git unlink` → `src/cinna/main.py:git_unlink()`

All accept `--agent <ref>` via the shared `src/cinna/main.py:_git_agent_opt()` and
resolve the target through `src/cinna/main.py:_resolve_sync_target()` (current
workspace, or an account-root child).

## Key functions & flow

- `src/cinna/git_versioning.py:fetch_coordinates()` → `GitCoordinates.from_dict()`
  — parse the endpoint payload (tolerant of missing keys; `vcs_enabled` coerced).
- `src/cinna/git_versioning.py:link()` — the link sequence:
  1. `_relayout_agent_dir()` if the backend subdir differs from the local one (a
     pure local rename, no re-download).
  2. `git init` + `symbolic-ref HEAD refs/heads/<ref>` + `config core.fileMode
     false` + `remote add/set-url origin`.
  3. `sparse-checkout init --cone` + `set <subdir>` for subdir agents.
  4. `git fetch --depth=1 origin <ref>`, then `git reset --mixed FETCH_HEAD`
     (never `--hard`, so live in-flight files survive).
  5. `_restore_deleted_tracked()` — re-checkout every in-cone tracked file the
     workspace tarball didn't lay down (versioned `plugins/**`, `.gitkeep`,
     repo-root shared files) so the tree isn't left with phantom deletions.
  6. force the backend-owned `cinna.agent.json` + `.gitignore` to committed.
  7. `branch --set-upstream-to=origin/<ref>` + `_write_local_excludes()`.
  8. persist `config.git` and the registry git block; refresh `CLAUDE.md`.
- `src/cinna/git_versioning.py:commit()` — `git add -- <subdir>` (scoped, so a
  multi-agent clone commits one agent), then commit; returns False on no-op.
- `src/cinna/git_versioning.py:push()` — `git push origin <ref>`; maps
  non-fast-forward to a `pull --rebase` instruction; honors the `pull`-direction
  guard.
- `src/cinna/git_versioning.py:pull()` — `git pull --rebase origin <ref>`.
- `src/cinna/git_versioning.py:checkout_version()` — `git checkout <ref> --
  <subdir>/workspace` (HEAD untouched); `--reload` is handled in the command via
  `src/cinna/sync_session.py:ensure_session()` + `flush()`.
- `src/cinna/git_versioning.py:_restore_deleted_tracked()` — `ls-files --deleted`
  (no pathspec → whole sparse cone, skip-worktree siblings excluded) + batched
  `git checkout`.
- `src/cinna/git_versioning.py:_write_local_excludes()` — rewrites cinna's block in
  `.git/info/exclude` from the static artifact list plus the *actual* synced
  prompt-ref guides via `src/cinna/context.py:list_synced_prompt_refs()`.
- `src/cinna/git_versioning.py:unlink()` — clears `config.git.vcs_enabled` and the
  registry git block; leaves `.git`.

## Layout helpers (`src/cinna/config.py`)

- `compute_agent_layout(parent, slug, subdir)` → `(clone_root, workspace_root,
  subdir)`. Always nests `<parent>/<slug>/<subdir>/`; `subdir` defaults to `slug`.
- `clone_root(config, workspace_root)` — the git working tree (from
  `config.git.clone_path`, else the workspace_root for a legacy flat agent).
- `src/cinna/bootstrap.py:resolve_clone_slug()` — bumps the clone-root name to
  `<slug>-<shorthash>` when `<slug>/` already holds a *different* agent.
- `src/cinna/bootstrap.py:workspace_agent_id_at()` /
  `src/cinna/bootstrap.py:short_agent_hash()` — collision detection helpers.
- `src/cinna/account.py:_iter_agent_dirs()` / `_find_agent_dirs_under()` — walk
  `agents/` to **arbitrary depth** (the backend subdir can be multi-segment, e.g.
  `agents/localhost/hello-testing`), pruning `workspace/`/`.git/`.

## Config & registry

`.cinna/config.json` carries `GitLayout` (`src/cinna/config.py`):
`clone_path`, `subdir`, `vcs_enabled`, `repo_url`, `ref`, `sync_direction`,
`auth_hint`, `last_synced_commit`. Loaded/saved by
`src/cinna/config.py:load_config()` / `save_config()` (nested dict, tolerant of
unknown keys).

The per-user registry (`~/.cinna/agents.json`, runtime state) gets an optional
`git` block alongside `workspace_path`. `src/cinna/config.py:upsert_agent_registry()`
uses a **preserve sentinel**: omitting `git` keeps any stored block (so sync
commands that re-write credentials don't wipe it), a dict sets it, and `None`
clears it (`cinna git unlink`). `workspace_path` stays the **agent dir** so
`cinna doctor` / `cinna list` resolve configs unchanged.

## External contracts

- **Endpoint:** `GET /api/v1/cli/git-coordinates` (CLI JWT; no `{id}` — agent
  derived from the token). 404 ⇒ treated as not-versioned. See the "Git
  Versioning → Backend contract" section of `docs/README.md` for the field shape,
  the canonical remote tree, the two-writer/ff-only model, and the cinna-core
  source pointers.
- **git** (the developer's own binary on `PATH`): invoked via
  `src/cinna/git_versioning.py:run_git()` with `os.environ` (their git config /
  credential helpers / `ssh-agent` drive auth). Relied-on invariants: cone-mode
  sparse-checkout includes repo-root files; `ls-files --deleted` skips
  skip-worktree (out-of-cone) entries; `reset --mixed` leaves the working tree.
- **Mutagen** — unchanged; the alpha endpoint stays `workspace_root/workspace`
  (`src/cinna/sync_session.py`). Linking does not touch Mutagen wiring.

## Edge cases & guardrails (preserve these)

- `reset --mixed`, **never `--hard`** — `--hard` would discard the live in-flight
  files. (`link()`)
- `_restore_deleted_tracked()` must run over the **whole cone**, not just the
  subdir — otherwise in-cone repo-root files stay phantom-deleted and block
  `git pull --rebase`. (`tests/test_git_versioning.py`)
- `core.fileMode false` — set on every (re-)link so x-bit-only diffs don't appear.
- Registry **preserve sentinel** — any command re-upserting credentials
  (`src/cinna/sync_session.py:start()` / `ensure_session()`) must not drop a linked
  agent's git block. (`tests/test_config.py`)
- Multi-segment subdir resolution — `_iter_agent_dirs()` walks deep enough to find
  `.cinna/` for `--agent`. (`tests/test_account.py`)
- Token/secret never committed — `.cinna/` is in `.git/info/exclude`; verify with
  `git status` before committing. (`tests/test_git_versioning.py`)
