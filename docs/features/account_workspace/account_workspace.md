# Account Workspace (`cinna account`)

## Purpose

Give a developer (or an orchestrator agent) **one multi-agent root** from which
to discover every agent on their platform account, attach standard per-agent
workspaces on demand, and manage the account-level scaffolding — user
workspaces, draft credentials, and the orchestrator context package — without
ever returning to the platform UI for a per-agent setup token. `cinna account`
is the command group that operates that root; `cinna agent sync` (documented
alongside it) is how the root spawns the ordinary single-agent checkouts.

## Mental model — one account root, many agent checkouts

There are **two kinds of cinna workspace**, and the account workspace is the
parent of the other:

- **Account workspace** — the multi-agent root produced by `cinna account setup`
  (or `cinna login` from an empty folder). Marked by `.cinna/account.json`,
  which holds the **account CLI token**. It is *not* a synced agent: it cannot
  `cinna dev`, `cinna exec`, or sync files. It only talks to the account route
  group (`/api/v1/cli/account/*`) to discover agents, mint per-agent tokens, and
  manage account-level resources.
- **Per-agent workspace** — a normal single-agent checkout marked by
  `.cinna/config.json` (the per-agent CLI token). This is exactly what
  `cinna setup <token>` produces. Under an account root they live under
  `agents/<slug>/` and are **byte-identical** to a hand-set-up checkout — only
  the token's provenance differs (minted from the account token vs. a pasted
  setup token).

The account token is the key idea: it is a second token type
(`token_type="cli-account"`) scoped **only** to `/account/*`. It discovers
agents and **mints child per-agent tokens** (`cinna agent sync`), but cannot
itself sync or exec. The minted child tokens carry the account token's id as
provenance, so they can be re-minted (`cinna doctor`) and revoked
(`cinna agent unsync`) through the account token later.

A single account workspace can serve any number of agents concurrently: each
`cinna agent sync` drops a fully independent per-agent checkout under `agents/`,
each with its own token, registry entry, and Mutagen session.

## Core concepts

- **Account CLI token** — `cli-account` JWT in `.cinna/account.json`. Same 7-day
  rolling expiry as a per-agent token. Refreshed in place by `cinna login` (a
  browser device-authorization flow — no paste) or by `cinna account set-token`
  (paste / hand over a fresh account setup token). Mints child tokens; never
  syncs.
- **Desktop-managed workspace** — an account workspace that Cinna Desktop
  created and keeps alive by driving `cinna` as a child process with no
  terminal: `account setup` once (into `<AgentsHome>/Cloud/<host>/`), then
  `account set-token` whenever it mints a new setup token with its own
  session, and `account status --json` to reconcile. The user never runs
  `cinna login`, yet the workspace is byte-identical to a terminal-made one —
  the user's own terminal can `cd` in and use every command.
- **`agents/` directory** — where `cinna agent sync` materializes per-agent
  checkouts. Account commands that touch local state (`status`, `agents`,
  `refresh-context`) walk this tree to find synced children.
- **Active user workspace** — a platform-side grouping of agents/credentials. The
  account workspace remembers a **client-side** active selection
  (`user_workspace_id` in `.cinna/account.json`); the backend keeps *no* active
  state. Workspace-scoped creates (`cinna agent create`, draft credentials) land
  in the active workspace, and `cinna account agents` is scoped to it by default.
- **Draft credential** — a credential the account CLI scaffolds as *metadata
  only* and attaches to agents. The CLI **never reads or writes a secret value**;
  the user fills the secret in the web UI, and until then it shows "needs setup".
- **Context package** — an orchestrator-facing bundle (platform docs, generated
  API reference, example scripts) downloaded into `context/` at the account root
  by setup, refreshable with `cinna account refresh-context`. Best-effort: the
  workspace is fully functional without it.
- **Orchestrator scaffolding** — `CLAUDE.md`, `.claude/settings.json`,
  `.mcp.json` / `opencode.json` written at the account root so a coding agent can
  drive the account through `cinna` subcommands and an account-mode
  `knowledge_query` MCP tool.

## User flows

### Bootstrap the account workspace
1. On the platform, open **Settings → Local Development** and copy the account
   setup command (a `curl … /api/cli-setup/account/<token> | python3 -`).
2. `cinna account setup <token-or-url>` exchanges the single-use account setup
   token, then materializes the root: `.cinna/account.json` (0600), an empty
   `agents/`, the orchestrator `CLAUDE.md` + `.claude/settings.json`, the
   knowledge MCP wiring, and the `context/` package. The folder name defaults to
   the platform domain (e.g. `demo-core_opencinna_io`); `--dir` or the prompt
   overrides it. `--dir` is a contract: an **absolute** path is used exactly as
   given (missing parents are created), a relative one lands under the current
   directory. Either way an existing `.cinna/account.json` at the target is
   refused before the token is spent.
3. Alternatively, `cinna login <domain>` from an empty folder bootstraps the same
   workspace via the browser device flow (no paste); run inside an existing
   account workspace it refreshes the token in place.

### Refresh the token from a setup token — `cinna account set-token`
1. Mint a fresh **account** setup token (Settings → Local Development, or Cinna
   Desktop does it with its own session).
2. Inside the account workspace, `cinna account set-token <token-or-url>`
   re-exchanges it under the **stored** machine name and swaps only
   `account_token` (plus a server-refreshed platform / frontend URL) into
   `.cinna/account.json`. The active user workspace, machine name, `context/`
   and every child under `agents/` are untouched — the same in-place contract
   `cinna login` gives, minus the browser. A bare token reuses the stored
   platform URL.
3. The new token must be for the **same account**: a different platform origin,
   or a different `sub` on the token, is refused (exit 11) and nothing is written.
4. Child tokens are not touched; `cinna doctor` re-mints expired ones as usual.

### Driven by Cinna Desktop (no terminal)
1. The desktop obtains a setup token from the platform with its own session and
   spawns `cinna account setup "<setup_command>" --dir <absolute> --name <machine>
   --no-input --json`.
2. `--no-input` guarantees the process never waits for a human: every prompt
   takes its default (machine name, folder), or fails with the machine code
   `needs_input`. `CINNA_NO_INPUT=1` does the same for any command. `--json`
   implies it.
3. `--json` turns the output into one JSON object per line on stdout: progress
   lines `{step, total, status: start|ok|warn|fail, message}` mirroring the
   numbered steps, then exactly one final line — `{"result": "ok", …}` with the
   workspace path, platform / frontend URLs, machine name and context-package
   outcome, or `{"result": "error", "code", "detail"}`. Nothing else touches
   stdout; logs stay in `cinna.log`.
4. The process exit code is stable: `0` ok · `10` setup token invalid / expired /
   already used · `11` the token is for another account · `12` the platform is
   unreachable or answered 5xx · `1` anything else, with a specific `code`
   (`workspace_exists`, `needs_input`, `mutagen_missing`, `mutagen_mismatch`,
   `not_an_account_workspace`, …) · `2` bad invocation.
5. Later the desktop runs `cinna account set-token "<setup_command>" --no-input
   --json` to refresh silently, and `cinna account status --json` to read token
   validity, synced agents, context-package freshness and whether the installed
   cinna-cli matches the version the platform pins.

### Discover and attach agents
1. `cinna account agents` lists the agents the account can access — name + id,
   build rights (foreign bundle installs are view-only), whether the remote env
   is active, and whether a local checkout already exists under `agents/`. Scoped
   to the active user workspace by default; `--all` spans every workspace.
2. `cinna agent sync <name|slug|id>` mints a child token and drops a standard
   per-agent checkout under `agents/<slug>/`. `cd` into it and `cinna dev` /
   `cinna exec` exactly as for a hand-set-up agent. `cinna exec --agent <slug>`
   also works from the account root.

### Manage the active user workspace
- `cinna account user-workspace list` shows the account's workspaces and marks
  the active one (the implicit Default/unassigned is always present).
- `cinna account user-workspace activate <name|id>` sets the active workspace
  (persisted client-side); `default`/`none` — or `cinna account user-workspace
  clear` — resets to Default.
- New agents and their credentials are then created in that workspace.

### Draft and wire credentials
- `cinna account credentials types` lists credential types and the fields the
  user must fill per type.
- `cinna account credentials create --name … --type …` creates an **empty draft**
  (the CLI sends no secret) in the active workspace and prints exactly which
  fields the user must complete plus the UI link. `--agent` attaches it in one
  step; `--workspace` overrides the target workspace.
- `cinna account credentials list` shows credentials with their setup status
  (complete / needs setup) — metadata only.
- `cinna account credentials update <id>` edits metadata (name/notes/service-uri/
  sharing), never a secret. `cinna account credentials share-with-agent <id>
  --agent <ref>` attaches an existing credential. `cinna account credentials
  delete <id>` removes it (and unlinks it from agents).

### Keep context fresh
- `cinna account status` shows the account root, platform/frontend URLs, machine
  name, active workspace, synced-agent table, and a live token probe
  (valid / expired / no connection), nudging `cinna login` when not valid.
- `cinna account refresh-context` re-downloads `context/` and regenerates the
  orchestrator `CLAUDE.md`, the MCP wiring, and every synced child's per-agent
  `CLAUDE.md` from the bundled templates — so a CLI upgrade's new commands reach
  existing workspaces without a re-setup.

## Business rules / guardrails

- **Account token is account-scoped.** It works only on `/account/*`. Per-agent
  sync/exec always use the per-agent child token via the per-agent client.
- **Single-use setup token is guarded before it's burned.** `cinna account setup`
  refuses an existing-workspace target *before* exchanging the token, so a doomed
  run never wastes the one-time token (exit 1, code `workspace_exists`).
- **`set-token` is account-bound, fail-loud.** The refreshed token must match
  the workspace's platform origin and (when both tokens carry one) the same
  subject; otherwise nothing is written and the command exits 11
  (`account_mismatch`). It never rebinds a workspace to another account.
- **Never hang without a terminal.** Under `--no-input` (or `CINNA_NO_INPUT=1`,
  or `--json`) no command blocks on stdin: prompts take their default or fail
  with `needs_input`; confirmations take their default (usually "No", which
  aborts). The `/dev/tty` fallback the `curl | python3` bootstrap uses for the
  folder prompt is skipped too.
- **`--json` is a pure stream.** Rich output is suppressed entirely; each stdout
  line is one JSON object and the last one is always the `result` line, on
  success and on failure alike. Human output is unchanged when the flag is off.
- **Exit codes are a contract.** 0 / 10 / 11 / 12 / 1 (+ `code`) / 2 as listed
  in the desktop flow; every error the CLI raises on purpose maps onto it, so a
  driver switches on the number, not on message text.
- **Version pin is advisory.** `account status` compares the running cinna-cli
  with the version the platform publishes in its discovery document; a missing
  pin is `unknown`, never an error.
- **Fail-loud on backend errors.** Token exchange, mint, and every account call
  surface the backend's `detail` verbatim (expired/used token, foreign-install
  403, ambiguous agent). The CLI does not pre-judge build rights client-side —
  it surfaces the backend's 403.
- **Credentials never carry secrets.** Create/update only send metadata; the
  secret is filled in the UI. A draft is "incomplete" until the user completes it.
- **Active workspace is client-side only.** It lives in `.cinna/account.json`; the
  backend stores no active-workspace state. The cached name may go stale on
  rename — the id is authoritative.
- **Context refresh is non-destructive.** The old `context/` tree is removed only
  *after* a successful download; a failed refresh warns and leaves it intact. The
  orchestrator/child `CLAUDE.md` regeneration is offline and per-workspace, so one
  unreadable child never aborts the rest.
- **User-edited config is preserved.** `.claude/settings.json` is create-if-absent
  (never clobbered on refresh); `.mcp.json` / `opencode.json` and the generated
  `CLAUDE.md` are auto-generated infra and are overwritten.
- **Child checkouts are standard.** A synced child is identical to a `cinna setup`
  checkout — same Model-A layout, same auto-link when git-versioned, same
  registry entry — so `cinna list`, `cinna doctor`, and the sync shim treat it
  like any other agent.
- **Unsync preserves user files.** `cinna agent unsync` stops sync, best-effort
  revokes the child token via the account token, and removes `.cinna/` +
  generated files — but keeps the developer's workspace files.

## Multi-agent / multi-workspace behavior

- **Many children, one root.** Each `cinna agent sync` is independent (own token,
  registry entry, Mutagen session). Slug collisions get a `<slug>-<shorthash>/`
  clone root (shared with the git-versioning layout logic).
- **Workspace scoping is a view + a create target.** `cinna account agents`
  defaults to the active workspace and states the scope in its header; `--all`
  shows everything. `cinna agent create` and `credentials create` default their
  target to the active workspace.
- **Deeply nested children resolve.** A git-versioned child can sit several
  levels below `agents/<slug>/` (a multi-segment backend subdir); `--agent`
  resolution and the synced-agent table both walk down to find its `.cinna/`.

## Architecture overview

```
        Settings → Local Development (account setup token, single-use)
cinna account setup ───────────────────────────────────────────► platform
      │                                                           POST /cli-setup/account/<token>
      ▼
.cinna/account.json  (account CLI token, cli-account scope)
      │
      ├── cinna account set-token <token> ───────────────────► POST /cli-setup/account/<token>
      │       (stored machine name; same-account check; rewrites account_token only)
      ├── cinna login (device flow) ─────────────────────────► /api/v1/cli/account/login/*
      ├── cinna account agents / status / refresh-context ─► AccountClient ─► /api/v1/cli/account/*
      │       (status: + GET /.well-known/cinna-desktop for the cinna-cli pin)
      ├── cinna account user-workspace … (client-side active selection)
      ├── cinna account credentials … (metadata-only drafts)
      │
      └── cinna agent sync <ref> ─► mint child token ─► per-agent checkout
                                                          under agents/<slug>/
                                                          (== cinna setup output)
                                                              │
                                                              ▼
                                                       cinna dev / exec / git
                                                       (per-agent child token)
```

## Integration points

- **Bootstrap / setup** — `cinna agent sync` reuses the same bootstrap writer as
  `cinna setup` (config, registry, provisioning, git auto-link), so child
  checkouts are standard. See `../git_versioning/git_versioning.md`.
- **Git Versioning** — `cinna git --agent <ref>` (and `cinna sync --agent`,
  `cinna exec --agent`) target a synced child from the account root.
- **Remote Chat** — `cinna chat` runs through the account workspace's api-proxy
  (`AccountClient`), so it needs an account workspace; it is found by walking up
  from the cwd, exactly like the account verbs.
- **Context package freshness** — `cinna account status` reports the local
  `context/VERSION` against the platform's current package version and nudges
  when it is behind, since guides (not just docs) ship in that tree.
- **Improvement requests** — `cinna improve` runs on the account token from the
  same root, so one queue spans every agent the account owns; the orchestrator
  `CLAUDE.md` written here lists its verbs and `cinna account agents` supplies the
  publisher-install flag its ownership step depends on. See
  [improvement_requests](../improvement_requests/improvement_requests.md).
- **Doctor / login** — `cinna doctor` re-mints expired per-agent tokens through
  the account token, and groups blocked agents under a single `cinna login` /
  `cinna account set-token` hint when the account token itself has expired. It
  also reports a cinna-cli that differs from the platform's pin.
- **Bootstrap / onboarding** — the no-terminal contract (`--no-input`, `--json`,
  exit codes, `CINNA_MUTAGEN_BIN`) is CLI-wide and described in
  [Bootstrap & Onboarding](../bootstrap_onboarding/bootstrap_onboarding.md);
  this doc covers how the account verbs use it.
- **Cinna Desktop** — installs its own pinned cinna-cli (with uv and Mutagen) and
  drives the three verbs above; the desktop-side plan lives in the cinna-desktop
  repo (`plans/one-click-onboarding-desktop.md`). <!-- nocheck: cross-repo path -->

Implementation: see [account_workspace_tech.md](account_workspace_tech.md).
Real-usage e2e test scenarios: see
[account_workspace_acceptance.md](account_workspace_acceptance.md).
