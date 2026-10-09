# Team Library (`cinna library`)

## Purpose

Share an agent built in the workshop's `Local/<slug>/` folder with the rest of
the team through the Cinna instance, and keep everyone's copy current — from a
terminal or a coding assistant, with the same verbs Cinna Desktop shows
(Get, Update, Share changes, Suggest a change) and the token the CLI already
holds.

> Status: cinna-core's library routes and git host (team-sharing phase P2) have
> not shipped. The CLI is built against the interface the team-sharing plan
> fixes (README §4) and tested against a local stand-in; see
> [team_library_tech.md](team_library_tech.md).

## Mental model / core concepts

- **A shared agent is a git repository cinna-core hosts** — one per agent,
  served over smart HTTP at `<platform>/git/<repo_id>.git`. On disk it is an
  ordinary kit folder, `Local/<slug>/`, that happens to have a remote. There is
  no separate tree and nothing is copied: sharing runs `git init` in place.
- **The team library** is the catalog core renders over the repositories you
  may read: slug, name, your access, maintainers, the current head.
- **Access per repository:** `read` (get, update, suggest), `write` (also share
  changes), `maintain` (also change who has access). Whoever shares an agent
  first maintains it.
- **The token you already have.** Git signs in with the account workspace's
  `cli-account` token, passed to git through its environment for that one
  remote. There is no SSH key, no deploy key, no credential helper, no GitHub
  account.
- **Local vs remote vs on-demand:** the folder is local and always usable
  offline; the team's copy changes only when someone shares; your copy changes
  only when you run `update`. The CLI never syncs in the background.
- **Desktop parity.** The desktop owns its own git and never runs these
  commands; both speak the same core routes, so "Share changes" in the desktop
  is `cinna library share` in a terminal. The CLI may use git words; the
  desktop does not.

## User flows

1. **Share an agent with the team (first time).** From inside `Local/<slug>/`
   (or naming it): `cinna library share -m "First version"`. The CLI reads
   `cinna-agent.json`, asks core to create the repository, runs `git init`,
   commits everything the folder's `.gitignore` allows, and pushes. The status
   is now "up to date with the team".
2. **See what the team has.** `cinna library list` — every agent you can read,
   your access, its maintainers, and which ones you already have under `Local/`.
3. **Get a teammate's agent.** `cinna library get <slug>` clones it into
   `Local/<slug>/`. It refuses when that folder exists, or when you already have
   the same agent (same manifest id) under another name.
4. **Share changes.** Edit the folder, then `cinna library share -m "…"`: commit
   all, push. If someone shared first, your changes are rebased onto theirs once
   when that applies cleanly, then pushed.
5. **Update.** `cinna library update` (or `--all` for every library agent under
   `Local/`): fetch, fast-forward, or a clean rebase of changes you have
   committed but not pushed.
6. **Check where you stand.** `cinna library status [--all]` — one line per
   agent: up to date, N changes you haven't shared, the team changed this agent,
   uncommitted files. `--no-fetch` answers from what was last fetched.
7. **Suggest a change without write access.** `cinna library suggest -m "…"`
   sends the difference between your folder and the team's version to the
   maintainers. Your folder is not changed.
8. **Manage access (maintainers).** `cinna library grants <slug>` shows the
   grants; `--add everyone=read`, `--add role:staff=write`,
   `--add ana@example.com=maintain`, `--remove <principal>` change them.
9. **Bring a library agent to the cloud.** `cinna agent import --from-library
   <slug>` runs the ordinary [local agent import](../local_agent_import/local_agent_import.md)
   on your `Local/` copy, or on a temporary clone when you have none.
10. **Every verb takes `--json`** for assistants; a refusal is then a final
    `{"result": "error", "code": …, "detail": …}` line carrying the server's
    code when it sent one.

## Business rules

- **Fast-forward only, two outcomes on a collision.** A push the team's history
  rejects gets exactly one clean rebase. A rebase that does not apply cleanly
  is aborted and refused (`diverged`) with the desktop's two options as
  sentences: use the team's version and keep yours as a copy (a branch), or
  keep working on yours and share later. Nothing is merged, force-pushed or
  lost.
- **Update refuses on uncommitted edits** (`uncommitted_changes`): share them
  first or set them aside — an update never mixes the team's changes into
  files you are editing.
- **Secrets and runtime state never leave.** `credentials/`, `app-data/`,
  `publications.json` and the CLI's own `cinna.log` are excluded per clone; any
  file the kit's `secret_files` rule names (every dotenv shape) is refused
  before anything is committed (`secret_in_tree`, naming the files). Core's
  `pre-receive` refuses the same, but nothing should leave to find out. A
  refused first share undoes the `git init` it made.
- **The token is never on disk or on screen.** Not in argv, `.git/config`, the
  remote URL, `cinna.log` or any output. The folder records only the
  non-secret repository id.
- **External remotes are not taken over.** A folder that already has its own
  `origin` (GitHub, …) is refused (`foreign_remote`); developers keep their own
  flows for those.
- **Read access cannot share** (`read_only`); it suggests instead.
- **Grants change only for maintainers** (`not_maintainer`), and only when the
  platform says who has access now (`grants_unreadable` otherwise — a blind
  write could drop someone).
- **Which account.** The account workspace around the current folder, else the
  one under the workshop's `Cloud/`. Several are narrowed by the folder remote's
  host, and otherwise refused (`ambiguous_account`) rather than guessed.
- **Platform without the library** answers `library_unavailable`.

## Architecture overview

```
cinna library <verb>
  ├─ library.py ── AccountClient ──► GET/POST/PUT /api/v1/library…   (cli-account token)
  └─ library.py ── git_versioning.run_git(auth=RemoteAuth) ──► git ──► /git/<repo_id>.git
                                       │                                 (smart HTTP,
                                       └ token as GIT_CONFIG_* env        token in header)
Local/<slug>/  (kit folder + .git, origin = clone_url, cinna.library.repoId)
```

## Integration points

- [Git Versioning](../git_versioning/git_versioning.md) — same git wrapper and
  token injection; `cinna git` on an agent whose git source is a library
  repository uses the per-agent token (`auth_hint: cinna`).
- [Account Workspace](../account_workspace/account_workspace.md) — supplies the
  `cli-account` token; its generated `CLAUDE.md` lists the library verbs.
- [Local Agent Import](../local_agent_import/local_agent_import.md) —
  `--from-library` feeds a library agent into the ordinary import; the kit
  contract (`.cinna-kit/layout.json`) supplies the secret rules.

Implementation: [team_library_tech.md](team_library_tech.md). Live scenarios:
[team_library_acceptance.md](team_library_acceptance.md).
