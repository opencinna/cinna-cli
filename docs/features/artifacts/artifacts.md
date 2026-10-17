# Artifacts (`cinna artifacts`)

## Purpose

Publish a folder or a file from the user's machine to cinna-core as an
**artifact** — a frozen, versioned copy people open on a Cinna page — and get a
link back. The same command group lists, downloads, opens, shares and deletes
artifacts from a terminal.

## Mental model / core concepts

An artifact lives **in cinna-core**, not on the machine and not in an agent
environment. The CLI is a publisher and a remote control; once published, the
artifact outlives the folder, the agent and the session it came from.

- **Artifact** — a stable identity (title, who can see it, its page link) over a
  sequence of immutable **revisions**. Core renders it: a folder with an
  `index.html` runs as a small static site in a sandbox; anything else is a file
  set with viewers (Markdown, CSV, PDF, images, code). Not to be confused with a
  delegation's `--artifact` — a link attached to a task result (see Integration
  points).
- **Revision** — one publish of the files. Republishing adds a revision; the
  page link stays the same and shows the latest one, so a shared link never
  breaks when a report is refreshed.
- **Path memory** — the CLI remembers which artifact each local path was
  published as, per account workspace (`.cinna/artifacts.json`), and republishes
  into it. Nothing is ever written into the published folder itself.
- **Exclusions** — what never leaves the machine: `.git/`, `.env*` and `*.env`,
  `credentials/`, `credentials.json`, `app-data/`, `.claude/`, `.cinna/` (it
  holds the account token), `node_modules/`, `__pycache__/`, key and token
  files (`*.pem`, `*.key`, `*.p12`, `id_rsa*` and the other SSH key names,
  `.netrc`, `.npmrc`, `.pypirc`), `.DS_Store`, and every symlink. Mandatory, not
  advisory; core drops its own set again on its side.
- **Who can see it** — "Only you" (private, the default), plus named people,
  plus "Everyone on the instance", plus **public links** (anyone holding the
  link, signed out; optional expiry, optional 4-digit code, revocable,
  optionally pinned to a revision). The CLI never says "public" for
  instance-wide.
- **Agents publish, people share** — an agent in the account workspace may
  publish (always private) and manage its artifacts, but widening who can see
  one is the user's call: `share` and `link create` are person-only verbs.

## User flows

### Publish a report and get its link
1. `cinna artifacts publish ./reports/q3 --title "Q3 churn"` from the account
   workspace (or any folder below it).
2. The CLI prints the file count and size, then what it left out
   (`.git/`, `.env`, …), uploads, and prints the page link and the id.
3. The artifact is private; the user opens the link (or
   `cinna artifacts open <id>`) to check it.

### Refresh it later, same link
4. Run the same `cinna artifacts publish ./reports/q3` again. The CLI says it
   republished — revision 2, same link — because it remembered the path.
5. `--update <id>` republishes into a named artifact instead; `--new` publishes
   a separate artifact even though the path is known (the path then points at
   the new one).

### Share it with people
6. In their own terminal the user runs
   `cinna artifacts share <id> --user bob@example.com` and/or `--instance`;
   `--private` and `--remove-user` narrow it again. The CLI prints who can see
   it afterwards.
7. For someone outside the instance:
   `cinna artifacts link create <id> [--expires 7d] [--code] [--label …] [--rev N]`
   prints the link (and code) **once**. `cinna artifacts show <id>` lists links
   by id and prefix; `cinna artifacts link revoke <id> <link>` kills one.

### Find, fetch, remove
8. `cinna artifacts list` (yours) or `list --shared` (shared with you);
   `show <id>` for revisions, people and links.
9. `cinna artifacts download <id> [--rev N] [-o DIR]` extracts the files
   (default `artifacts/<short-id>/` under the account root).
10. `cinna artifacts delete <id>` asks first; the page and every link stop
    working at once.

## Business rules

- **Server support first** — every verb asks core for its artifacts capability
  before anything else; a server without it (or with another version) gets
  "This Cinna server does not support artifacts" and nothing is uploaded.
- **A secret path named directly is refused** — publishing `.cinna/account.json`,
  a `credentials/` folder or anything inside an excluded folder stops before
  any request; the walk alone would only see paths below what was named.
- **Exclusions are listed, always** — the human output and the `--json` result
  name every path left out, the CLI's and the server's together.
- **Refuse what core would refuse, before uploading** — over the file count,
  per-file size or total size the server advertises, the CLI stops locally.
- **Republish keeps the title** — on a republish only the flags the user passes
  change (`--title`, `--description`, `--entry`, `--note`).
- **A remembered artifact that was deleted** — the CLI says so and publishes a
  new one; an explicit `--update <id>` that no longer exists fails instead.
- **Person-only verbs (A8)** — `share` and `link create` refuse under `--json`,
  `--no-input` / `CINNA_NO_INPUT`, or when stdin is not a terminal, with code
  `needs_person`. There is no `--yes` bypass: an agent can pass any flag. The
  generated `.claude/settings.json` also denies `Bash(cinna artifacts share:*)`
  and `Bash(cinna artifacts link create:*)` to Claude Code. The CLI cannot tell
  an agent from a person — this is a speed bump; core enforces A8 on its
  agent-token route.
- **A script cannot share either** — a script without a terminal is refused the
  same way. Sharing from automation is deliberately not offered in v1; the web
  page's Share dialog is the alternative.
- **Destructive verbs confirm** — `delete` and `link revoke` ask (default No);
  under `--json` they need `--yes`, under `--no-input` without `--yes` they
  abort.
- **The link token is shown once** — core returns it only on creation; the
  api-proxy never relays it, and `show` lists links by prefix only.
- **Owner-only writes** — share, link and delete check ownership first and say
  so plainly rather than relaying a 404.

## Architecture overview

```
cinna artifacts publish PATH
  → artifacts.py: walk + exclusions → temp zip (stored entries for very compressible files)
  → AccountClient → POST /cli/account/artifacts[/{id}/revisions]  (multipart, account token)
  → .cinna/artifacts.json  (path → artifact id)

cinna artifacts list|show|share|link revoke|delete
  → AccountClient → api-proxy → /api/v1/artifacts…, /users/search

cinna artifacts link create | download
  → AccountClient → /cli/account/artifacts/{id}/links | /download
```

## Integration points

- [Account workspace](../account_workspace/account_workspace.md) — the account
  token, the active user workspace the artifact lands in, and the generated
  `.claude/settings.json` that carries the deny rules.
- [Durable delegation](../delegation/delegation.md) — the page link a publish
  prints is a valid `link` artifact for
  `cinna delegation report --artifact '{"kind":"link","name":"…","ref":"<url>"}'`.
- `cinna api` — the generic api-proxy the JSON verbs ride; it refuses link
  creation and blanks link codes, which is why `link create` has its own route.
- cinna-core's kit skill `cinna-artifacts` — how to build a folder that works
  in the sandbox (bundle libraries, no `localStorage`, relative paths).
