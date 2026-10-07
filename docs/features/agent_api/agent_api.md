# Agent REST API (`cinna agent-api`, `cinna connect agent-api`, `cinna api`)

## Purpose

Let an agent **expose its own HTTP/OpenAPI surface** (a "producer" REST API)
that another agent (a "consumer") can call, and give a coding agent the local
loop to build, verify, and wire those connections from the account workspace:
`cinna agent-api` (enable / refresh / spec / call / access) drives the
**build→verify** loop on a producer; `cinna connect agent-api` does the
**one-click wiring** of a consumer to it; and `cinna api` is a separate
**escape hatch** for calling the platform's *own* control-plane API directly.

These three command surfaces live in the same area but solve different
problems — keep them apart in your head (see "Three things named 'api'" below).

## Mental model — the producer REST API

A platform agent can publish a REST API. The agent author writes ordinary
endpoint code under `workspace/agent_api/*.py` and an access `policy.yaml` in
the same synced workspace; the platform **harvests** that code into an OpenAPI
spec and serves the endpoints from the agent's running environment. Consumers
reach it with a minted **producer token** over plain HTTP — no Mutagen, no SSH
shim, just an authenticated REST call against the producer's served base URL.

Two roles:

- **Producer** — the agent that *exposes* the API. Authored locally
  (`agent_api/` + `policy.yaml`), enabled on the platform, harvested into a
  spec.
- **Consumer** — the agent that *calls* the producer. It holds a producer
  token as a normal **credential**, synced into its own remote env. The
  consumer's own code (or its agent runtime) makes the HTTP calls.

The CLI never holds the producer secret long-term: `connect` mints the token
server-side and attaches it to the consumer as a credential, which rides the
consumer's ordinary credential sync into its env. There is no manual key paste.

## Three things named "api"

| Command | What it talks to | Auth | Use it to |
|---------|------------------|------|-----------|
| `cinna agent-api …` | A **producer agent's own** REST API (manage + owner-preview) | account token | enable/disable, re-harvest the spec, read the spec, smoke-test one endpoint as the owner |
| `cinna connect agent-api` | The **wiring** between two agents | account token | mint a producer token and attach it to a consumer as a credential |
| `cinna api …` | The **platform's** control-plane API (the escape hatch) | account token | call ordinary platform routes (`agents`, `tasks`, …) that have no dedicated verb |

`cinna agent-api call` and `cinna api` both make an HTTP call and both share the
same exit-code contract, but the *target* differs: `agent-api call` hits the
**producer's** REST API through an owner-preview proxy; `cinna api` hits the
**platform's** API through the account escape hatch. Don't reach for `cinna api`
to test a producer endpoint — use `cinna agent-api call`.

## Core concepts

- **Producer API** — the agent's published REST surface, served from its
  running env once enabled.
- **`agent_api/` + `policy.yaml`** — the author-owned source: endpoint modules
  and the access/guardrail policy, both living in the producer's synced
  `workspace/`.
- **Spec harvest** — the platform imports the `agent_api/` modules and parses
  `policy.yaml` to produce the cached OpenAPI spec. `refresh` forces it; the
  status carries `spec_available`, when it was harvested, and any `last_error`.
- **Owner-preview proxy** — the path `cinna agent-api call` uses: it invokes a
  producer endpoint **as the owner**, with no consumer token and no policy
  edge, but **does** forward query params, so it verifies an endpoint
  end-to-end in one shot.
- **Producer token / credential** — what `connect` mints and attaches; the
  consumer authenticates its real calls with it.
- **Escape hatch** — the account `api-proxy` route `cinna api` rides: a buffered
  JSON pass-through to the platform's own API, with excluded categories
  (credentials, user management, admin, CLI, MFA/auth, streaming, and the
  secret-minting routes listed under "Escape-hatch deny-list") denied.

## User flows

### Build & verify a producer API
1. `cinna agent-api enable <agent>` turns the REST API on for the producer (and
   prints the status, including whether a spec already exists). `--disable`
   turns it off.
2. Author the endpoints + policy in the producer's workspace under
   `agent_api/*.py` and `policy.yaml`, and sync them (`cinna dev` / `cinna
   exec`) into the running env.
3. `cinna agent-api refresh <agent>` re-harvests the spec + policy on demand so
   the cached spec picks up your edits without waiting for the next automatic
   reload. A harvest error shows under `Last error` — fix the code/policy, sync,
   refresh again.
4. `cinna agent-api spec <agent>` prints the harvested OpenAPI spec as plain
   JSON (pipe/parse it), or `-o file.json` to save it.
5. `cinna agent-api call <agent> <path>` smoke-tests one endpoint as the owner
   (`-X` method, `--query k=v`, `--json '{…}'`). Exit 0 for a 2xx, 1 for a
   4xx/5xx (the body prints either way) — so it composes in scripts.

### Wire a consumer to it
- `cinna connect agent-api --producer <A> --consumer <B>` mints A's producer
  token and attaches it to B as a credential. `--label` names the credential;
  `--read-only` restricts the consumer to read-only API access.
- The output reports the credential id, token prefix, and base/spec URLs, and
  reminds you the credential rides B's normal credential sync into its env (it
  appears read-only under `workspace/credentials/` in a synced workspace) —
  no manual key handling.

### Give API access (a person, or a local agent)
- `cinna agent-api access list <producer>` shows who has access: one row per
  person with their scopes and keys (prefix, status, expiry, last use, how many
  agents use it, key id). `--all` also shows ended keys; `--json` is always the
  full listing. The footer says whether external access is on and which scopes
  the producer declares.
- `cinna agent-api access issue <producer> --to <email|me>` issues a key to a
  person. `--scopes a,b` sets their scopes (`""` = an explicit empty grant),
  `--expires-in-days N` (1-3650), `--service-uri SLOT` names the credential's
  slot (default `<producer>-api`), `--label`, `--read-only`.
  - `--to me`: the key lands in your own credentials; the output names the slot
    and the web credential page where you reveal the value. A local agent
    (Cinna Desktop) then calls it by slot; a cloud agent is wired with
    `cinna connect agent-api` instead.
  - `--to <email>`: the key lands in that person's credentials; you never see
    the value. The output prints the revoke command.
  - Scopes outside the producer's declared catalog are stored anyway, with a
    warning.
- `cinna agent-api access revoke <producer> <key>` ends one key (asks first,
  `--yes` skips). The person's scopes stay, so a new key can be issued without
  re-granting.
- `cinna agent-api access scopes <producer> --to <email|me> --set a,b | --clear`
  changes a person's scopes without touching their keys, so a working key keeps
  working. `--clear` removes the grant.
- The result shows up elsewhere: `cinna account credentials list` shows the
  key's status in the Status column, and a skill slot filled by a dead key
  reads as not usable (see
  [account_workspace](../account_workspace/account_workspace.md)).

### Call the platform's own API (unrelated to producer APIs)
- `cinna api GET agents`, `cinna api POST agents/<id>/duplicate`,
  `cinna api PATCH agents/<id> --json '{…}'` — `PATH` is relative to the API
  root (no `/api/v1` prefix). The catalogue of callable routes lives in the
  account workspace's `context/api_reference/`. `--data @file.json` reads a body
  from a file; `--query k=v` is repeatable.
- `cinna api GET agents/crm-agent/addons` — the segment after `agents/` takes
  the same references every other verb does, so the escape hatch is not the one
  place that demands a UUID.

## Business rules / guardrails

- **Account-scoped.** Every `agent-api`, `connect`, and `api` command runs from
  an **account workspace** and authenticates with the account token. The agent
  is referenced by display name, slug, or id (resolved against `cinna account
  agents`); an unresolved ref fails **before** any API call.
- **Harvest never raises on author error.** `refresh` returns a status with
  `last_error` set rather than throwing — a broken `agent_api/` module or
  `policy.yaml` surfaces as a reported error the builder can fix, not a stack
  trace. The CLI nudges you when `Last error` is present.
- **State vs. spec freshness are separate.** The status prints the live serving
  child's `State` *and* dates the cached spec (`Spec harvested … ago`)
  independently, so a stale spec is visible rather than masquerading as current.
- **Owner-preview ≠ consumer path.** `cinna agent-api call` bypasses the consumer
  token and the policy edge (it's the *owner* previewing), but it **does**
  forward query params — so it catches a silent query-drop that a naive probe
  would miss. It is not a substitute for testing the actual consumer
  authorization path.
- **Exit-code contract (both callers).** `cinna agent-api call`: 0 for inner
  2xx, 1 for inner 4xx/5xx (body still printed). `cinna api`: 0 for inner 2xx,
  1 for inner 4xx/5xx, and **2** when the escape hatch itself refuses (policy
  denial, rate limit, size cap — reported on stderr). The hatch distinguishes
  its own refusal from a mirrored inner error by a marker response header.
- **Escape-hatch deny-list.** `cinna api` cannot reach credentials, user
  management, admin, CLI, MFA/auth, or streaming routes — the platform denies
  them; don't waste calls. It also refuses routes that would hand out a secret:
  any method on `agents/*/agent-api/keys…` (issued keys), on `environments/*/workspace/download` and `view-file`, and on `dashboards/*/blocks/*/env-file`; and POST on A2A access-tokens, MCP/ACP connector tokens, agent and task-trigger webhook create/regenerate-token. Paths are matched after percent-decoding. These refuse with 403 `excluded_path` (exit 2). Prefix-only listings stay reachable.
- **An elided id is refused locally.** A path segment carrying `…` or `...` is
  an id copied out of a table cell too narrow to print it whole. It reaches the
  API as a well-formed path and comes back `404 Agent not found` — a sentence
  that sends the reader hunting for a missing agent instead of a mangled id, so
  the CLI answers first and calls nothing. (The id columns themselves fold
  rather than ellipsize, so the truncation should not happen at all.)
- **Agent-ref resolution is sugar that may never break the hatch.** Only the
  segment straight after `agents/` is considered, only when it is not already a
  UUID, and every failure — an unresolvable ref, an unreachable agent listing —
  leaves the path exactly as typed: `agents/` is a route prefix as well as a
  collection, and turning a real sub-route into "no accessible agent matches
  'search'" would break the one verb whose job is to work when nothing else
  does. A substitution is announced on **stderr**, so a JSON stdout stream
  stays pure.
- **No secret round-trip.** `connect agent-api` mints the producer token
  server-side and attaches it as a consumer credential; the secret value never
  passes through the CLI as something the user pastes or stores locally.
- **A key value never crosses the CLI.** The platform sends none on the access
  routes, the client drops any stray `token` field, and every runner drops it
  again before printing or `--json`. Self-issue returns a web URL where the
  person reveals the value; nobody should paste one into the CLI.
- **Rotate and reveal are web-only.** Rotation replaces a value someone outside
  the platform holds, which the CLI could not hand over, and a coding agent
  could otherwise start a countdown that breaks every holder of the old value.
  There is no verb for either.
- **The external-access switch is web-only.** It is the deliberate human act
  that exposes the API outside the platform. When it is off, `issue` is refused
  with `external_access_disabled` and the producer's Integrations URL is
  appended to the message (only when the platform's message lacks one); `list`
  warns and prints the same URL.
- **Terminal keys are hidden unless `--all`.** Revoked, producer/issuer removed
  and expired keys drop out of the `access list` table with a footer count;
  `--json` always carries everything.
- **`--to` is required on `issue` and `scopes`.** The recipient is always a
  deliberate choice: an email (resolved server-side, exact case-insensitive
  match among active users) or `me`. An unknown email is refused, and one that
  matches several accounts is refused as ambiguous rather than guessed.
- **A key reference is a key id or the exact token prefix.** A reference
  carrying an elision marker is refused locally (same rule as other ids); no
  match or several matches fail with a candidate listing. Revoking an
  already-revoked key is a no-op that exits 0 ("already revoked"); `--json`
  requires `--yes`.
- **Only the producer's owner (or an admin) manages access, and writes need
  the developer role.** A producer that is missing or not yours reads as not
  found. `list` is open to any account token.
- **The generic escape hatch cannot issue keys.** `cinna api POST
  agents/<id>/agent-api/keys` is refused (exit 2); the `access` verbs are the
  only CLI way.
- **Read-only is a connect-time choice.** `--read-only` is applied when the
  connection is created; it scopes the *consumer's* access, not the producer's
  surface.

## Architecture overview

```
build/verify (producer):
  cinna agent-api enable/refresh/spec/call
        │  account token
        ▼
  /api/v1/cli/account/agent-api/{enable,refresh,spec,call}
        │
        ▼
  platform harvests workspace/agent_api/*.py + policy.yaml ─► OpenAPI spec
        │                                                     (served from the
        ▼                                                      producer's env)
  agent-api call ─► owner-preview proxy ─► producer endpoint (no consumer token)

people access (issued keys and scopes):
  cinna agent-api access list/issue/revoke/scopes
        │  account token
        ▼
  /api/v1/cli/account/agent-api/{access,keys,keys/revoke,access/scopes}
        │ key lands in the recipient's credentials (value never returned)
        ▼
  recipient reveals it on the web; a local agent calls it by slot

wiring (consumer):
  cinna connect agent-api --producer A --consumer B
        │  account token
        ▼
  /api/v1/cli/account/connect/agent-api
        │ mint A's producer token, attach to B as a credential
        ▼
  rides B's credential sync ─► B's remote env ─► B calls A over plain HTTP

platform escape hatch (unrelated target):
  cinna api <METHOD> <path> ─► /api/v1/cli/account/api-proxy ─► platform API
```

## Integration points

- **MCP integration** (`../mcp_integration/mcp_integration.md`, not yet
  written) — `cinna connect` covers **both**
  halves of agent-to-agent wiring: `connect agent-api` (this feature, REST) and
  `connect mcp` (the agent2agent MCP connector). They are sibling subcommands of
  the same `connect` group and share the producer→consumer credential model.
- **Account workspace** — all of these commands run from the account workspace
  and use the account token. See
  [account_workspace](../account_workspace/account_workspace.md).
- **Remote sync / exec** — authoring a producer API means editing files in the
  producer's synced `workspace/agent_api/` and pushing them to the running env
  (`cinna dev` / `cinna exec`); the spec is harvested from that env.
- **Account workspace credentials and skill slots** — an issued key is a
  credential whose listing row carries its key status; skill slot readiness
  treats a revoked, expired or paused key as not usable. See
  [account_workspace](../account_workspace/account_workspace.md).
- **Local agent import** — `cinna agent import` never drafts an `agent_api`
  credential (it could never be used); it attaches an existing same-name one or
  prints the `access issue` / `connect agent-api` command. See
  [local_agent_import](../local_agent_import/local_agent_import.md).
- **`cinna agent show`** — surfaces a producer's live `agent_api_status`
  alongside its prompts and credentials, reusing the same status renderer.

Implementation: see [agent_api_tech.md](agent_api_tech.md). Real-usage e2e test
scenarios: see [agent_api_acceptance.md](agent_api_acceptance.md).
