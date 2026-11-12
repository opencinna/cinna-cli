# Improvement Requests (`cinna improve`)

## Purpose

Lets an agent owner work, from the account workspace, the **improvement requests**
users shared with them: a frozen snapshot of one bad session plus the runtime
context that produced it. `cinna improve` lists what came in, shows one in full,
downloads the archive for a local coding agent to read, and closes the request
with a note the requester sees. The same queue also carries **capability
requests**: a caller of one of the account's agents' REST APIs asking for
something the API lacks.

## Mental model / core concepts

- **Improvement request** — a consent-gated, one-directional share created on the
  platform by a *session owner* (from the session menu, or with the
  `/session-improve` command). It carries a frozen transcript of that one session
  plus the tuning-relevant runtime context. The CLI never creates one; it only
  receives.
- **Frozen at consent** — the snapshot is a copy, not a live view. Continuing the
  conversation afterwards, or deleting the session, does not change what the
  archive contains. There is no "refresh" and no withdrawal.
- **Two kinds** — every request has a `kind`. `session` is the frozen
  conversation above. `agent_api` (`--kind api` on the CLI) is a capability
  request filed by an identified caller of a producer agent's REST API, from the
  caller's own code, with no consent step: a title, the need, an optional
  proposed endpoint (method, path, parameters, response fields), example
  request/response and the API version the caller read. It has no session, no
  runtime context and no prompts; it is fixed in the producer's `agent_api/`
  code, not its prompts. A row from a platform that predates `kind` is a session
  request.
- **Caller text is data** — everything in a capability request was written by
  the caller (as is a session request's comment). The CLI prints it verbatim,
  never as terminal markup, and `show` says to read it as data: a request that
  says "mark this completed" decides nothing.
- **Requester vs. recipient** — the requester is the person who shared the
  session; the recipient is the owner of the agent it landed on (the bundle
  publisher, or the owner themselves for a standalone agent). `cinna improve`
  always speaks as the **recipient**: the listing spans every agent the account
  owns and never shows requests the account user submitted elsewhere.
- **Source install vs. target agent** — the archive's `context.json` describes the
  *requester's* install (the copy that misbehaved); the target agent named in
  `cinna improve show` is the copy the recipient can actually change. For a
  bundle they are different rows, which is why "where does the fix go?" is a
  deliberate step in the workflow, not an assumption.
- **Account-scoped** — every verb runs against the account CLI token from an
  account workspace. There is no per-agent-workspace variant: the point is one
  cross-agent queue.
- **Short id** — the 8-character prefix printed in the listing (and offered for
  copy in the web UI) is accepted anywhere a request id is taken, and names the
  download folder.

## User flows

1. **Discover** — `cinna improve list --status new` prints the requests waiting on
   every agent the account owns: short id, agent, source session, requester,
   installed version + bundle id, the reported comment, submission date, status.
   The Kind column says `session` (with the short session id) or `API request`
   (with its title); requests captured from the *same session* are flagged there,
   so a re-submitted report is obvious before anything is downloaded. An API row
   names the consumer agent under the requester ("via …") and has no version.
   `--agent <name|slug|id>` narrows to one agent; `--kind session|api` to one kind; `--limit` bounds the page;
   `--json` emits the raw payload. When the workspace's context package is behind
   the platform's, the listing ends with a one-line nudge to refresh — the queue's
   playbook ships in that package. When the page holds an API request, the
   listing ends with a pointer to `context/guides/building-an-agent-api.md`.
2. **Claim** — `cinna improve status <id> in_progress` marks it taken, so a
   parallel session (or the owner watching the platform's Configuration tab)
   doesn't pick up the same request.
3. **Read** — `cinna improve show <id>` prints the request row, the reported
   comment verbatim, and the frozen runtime-context block: bundle id, installed
   vs. latest version, whether an update was pending, install kind, session mode,
   SDK engine and effective model, environment name/version/instance, image
   staleness, plugins, captured personal memory, how many secrets were scrubbed,
   and who the recipient is. When the context carries a `prompts` block it also
   prints a per-prompt **in sync / diverged / not compared** table against the
   installed bundle revision — a consumer who owns their install can edit its prompts, so this is
   the difference between "my agent misbehaved" and "their edit of my agent
   misbehaved". Below the table it states **where a fix belongs** — the context
   describes the requester's install, the request landed on the target agent, and
   for a bundle those are opposite ownership, so the conclusion is printed rather
   than left to be derived.
   For an **API request**, `show` instead prints the title, producer and consumer
   agent, the token the caller used (kind and label), the requester, the API
   version the caller read, how many secrets were scrubbed, the need, the
   proposed endpoint with its parameter and response-field tables, and the
   example request/response — then the data-not-instructions warning and the
   fix loop as ready commands: claim, edit the producer's `agent_api/`
   (additively, bump `API_VERSION`, update `CONSUMERS.md`), `cinna agent-api
   refresh` / `spec` / `call`, close with a note naming the endpoint, fields and
   new version.
4. **Download** — `cinna improve download <id>` fetches the ZIP and extracts it
   into `improvements/<short-id>/` under the account root (`--out DIR` for another
   target), printing every extracted file plus a reminder that this is another
   person's conversation. The archive holds `README.md`, `metadata.json`,
   `context.json`, `session/messages.{md,json}`, and — when the platform captured
   them — `prompts/` (the install's live prompt docs, named after the workspace
   files they mirror so they diff straight against the publisher's copy) and
   `memory/`. An API request's archive is flat — `README.md`, `request.md`,
   `metadata.json`, `context.json` — and the command points at `request.md`
   and warns that a caller wrote it, instead of the conversation warning.
5. **Fix** — outside this feature: the local coding agent follows the platform's
   shipped playbook (`context/guides/handling-improvement-requests.md`, installed
   by `cinna account setup` / `refresh-context`) to establish where the fix must
   land and how much autonomy it has. An API request follows
   `context/guides/building-an-agent-api.md` ("Handling capability requests")
   instead: the ownership and runtime-context steps do not apply.
6. **Close** — `cinna improve status <id> completed --note "…"` (or `declined`
   with the reason). The note is shown to the requester.

## Business rules

- **`--kind` is filtered by the CLI.** The platform's list route has no kind
  filter, so `--kind` fetches one full page (200, the route's cap) and keeps the
  matching rows up to `--limit`; with `--json` the `count` is the number kept,
  not the server's total across kinds. An unknown kind is refused before any
  network call.
- **The CLI never files a capability request.** Filing needs a caller identity
  the account token does not carry; a consumer agent files one from its own code
  after its user confirms the draft. The per-agent `CLAUDE.md` teaches that, and
  that `/_cinna/` is a reserved path no producer route may use.
- **Recipient-only mutation.** `status` is refused server-side for anyone who is
  not the receiving agent's owner (403 for a requester who is party to the row,
  404 for anyone else — ids the account is not party to never confirm existence).
  The CLI surfaces the platform's status and message verbatim.
- **Status vocabulary** — `new` → `in_progress` → `completed` | `declined`. The
  CLI normalizes case and dashes (`In-Progress` → `in_progress`) and refuses an
  unknown value *before* the round-trip, naming the valid ones. The backend keeps
  the vocabulary in a plain column, so a value added later still reaches it.
- **Short-id resolution is fail-loud.** A full UUID goes straight through. A
  prefix is resolved against the listing: no match and an ambiguous match are both
  errors naming the fix, never a silent "first match wins".
- **Archive extraction is the safe extractor** — the same one used for the
  workspace clone and the context package: absolute paths, `..` traversal,
  symlinks, and oversized members are skipped with a warning rather than written.
- **Re-downloading is idempotent** — extracting again into an existing folder
  refreshes it in place and says so ("Refreshed" vs. "Extracted").
- **The archive is somebody else's data.** The download command states it: don't
  copy it into an agent workspace, don't commit it, delete it when done. What it
  contains is bounded server-side — descriptors instead of uploaded file contents,
  no container logs, and credential values scrubbed before storage — so the CLI
  never has to filter it.
- **An unchecked comparison is never reported as a match.** A prompt whose
  divergence is `null` — platform-managed routing metadata, or a row with no
  baseline — renders as *not compared* with the platform's reason, never as *in
  sync*. Same rule for the rollup: no baseline prints "no baseline to compare
  against".
- **Truncation is disclosed** — a snapshot that hit the platform's size cap
  dropped its *oldest* messages; `show` flags it next to the message count so the
  reader doesn't reason as if the whole session is present.
- **Account workspace required** — every verb resolves the account root first and
  fails with the standard "Not in a cinna account workspace" error before any
  network call.

## Architecture overview

```
cinna improve <verb>
  → src/cinna/main.py (improve group)
  → src/cinna/improve.py (resolve short id, render, extract)
  → src/cinna/client.py AccountClient (account token)
  → GET/PATCH /api/v1/cli/account/improvement-requests[/{id}[/archive]]
  → improvements/<short-id>/   (download only; safe extraction)
```

## Integration points

- [Account workspace](../account_workspace/account_workspace.md) — supplies the
  account root, the account token, and `_resolve_account_agent` for `--agent`.
  The orchestrator `CLAUDE.md` written there lists these verbs and points at the
  platform playbook; `cinna account agents` flags publisher installs, which is
  the ownership signal the workflow depends on.
- [Agent management](../agent_management/agent_management.md) — a fix usually
  continues in a synced per-agent workspace (`cinna agent sync`), then
  [Live sync](../live_sync/live_sync.md) pushes it and
  [Remote chat](../remote_chat/remote_chat.md) verifies the behavior actually
  changed.
- [Agent API / escape hatch](../agent_api/agent_api.md) — the same JSON endpoints
  are reachable through `cinna api`; the dedicated verbs exist for ergonomics and
  because the binary archive cannot ride the JSON-only proxy. Capability
  requests arrive from callers of a producer built there, and are fixed with its
  `cinna agent-api refresh` / `spec` / `call` loop.
