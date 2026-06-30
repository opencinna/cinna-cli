# Remote Chat (`cinna chat`)

## Purpose

Let a local coding agent **test the agent it is building** by holding a real
conversation with it through the platform — the same production pipeline an
end-user hits (permission checks, agent-env calls, the model/SDK the platform
selects), not a local mock. `cinna chat` sends one message into a platform
session, attaches local files to it, and reports the agent's reply (final text,
its reasoning/tool trace, and any files it produced) in a machine-parseable
stream.

## Mental model — a real session, observed by polling

- **The conversation lives on the platform, not locally.** `cinna chat` does not
  run the agent; it opens (or resumes) a server-side *session*, drops a user
  message into it, and watches the session fill with the agent's reply. The
  model selection, permissions, and agent-env calls all happen remotely.
- **Account workspace is the auth context.** Everything rides the account
  workspace's JSON **api-proxy** (the same buffered escape hatch behind
  `cinna api`), authenticated by the account token in `.cinna/account.json`. So
  `cinna chat` runs from the account root *or* any synced `agents/<slug>/` folder
  under it — the account config is found by walking up the directory tree.
- **Send-then-poll, never stream.** The platform's send route returns a JSON
  acknowledgement immediately and runs the agent turn asynchronously; the live
  token-by-token events go out over a Socket.IO room. The api-proxy is a buffered
  JSON hatch that refuses event-streams, so `cinna chat` never reads that stream.
  Instead it **polls** the persisted messages until the turn settles. The full
  reasoning/tool trace is durably recorded on each message, so polling loses
  nothing but the live typing animation.
- **Two file directions.** Files *you* attach are uploaded first and referenced
  by id in the message. Files the *agent* attaches to its reply are downloaded
  locally so the caller can inspect them.
- **Agent-friendly by default.** Output is NDJSON — one JSON event per line — so
  the calling coding agent parses it trivially. `--pretty` swaps in a human view.

## Why polling, not streaming

The send-message route (`POST /sessions/{id}/messages/stream`) returns a JSON ack
right away and runs the turn in the background; the live deltas are emitted to a
Socket.IO room **and** persisted onto each message's
`message_metadata.streaming_events`. Because the api-proxy is a buffered JSON
hatch — it rejects `text/event-stream` — `cinna chat` deliberately ignores the
live stream and reads the persisted record by polling. This keeps the CLI immune
to streaming/transport quirks, works entirely over buffered JSON, and still
surfaces the entire reasoning/tool trace because that trace is saved on the
message itself.

## User flows

### Talk to an agent (new session)
1. From the account workspace (or a synced agent folder), run
   `cinna chat --agent <name|slug|id> "your message"`.
2. The agent is resolved (see *Agent resolution*), a new `conversation` session is
   created, and a `session` event is emitted.
3. The message is sent; the CLI polls and emits each finalized message as it
   appears, then a terminal `done` event with the session's settled state.

### Continue a conversation
- `cinna chat --resume <session_id> "follow-up"` reuses an existing session
  instead of creating one — the agent retains the prior turns' context. No new
  session is created.

### Attach files to your message
- `cinna chat --agent <ref> --file report.csv --file notes.md "Validate these"`
  uploads each file first, then references their ids in the message. Repeatable.

### Inspect what the agent did and produced
- Each agent message carries its reasoning/tool **trace** under `events`
  (thinking blocks, tool calls with their full input payloads, tool results) — so
  the caller sees *how* the agent got to its answer, not just the closing line.
  `--no-events` drops the trace and keeps only the final text.
- Files the agent attaches to a reply are downloaded into
  `./cinna-chat-files/<session_id>/` (override with `--download-dir`, suppress
  with `--no-download`, which reports just the file ids).

### Provide the message different ways
- Positional argument, piped on stdin (`echo "ping" | cinna chat --agent x`), or —
  in a TTY with no message — an interactive prompt. With no message and no file,
  the command errors cleanly rather than hanging.

## Business rules / guardrails

- **Account workspace required, fail-loud.** No `.cinna/account.json` up the tree
  ⇒ a clear error; `cinna chat` never falls back to a per-agent token.
- **Attachments validated before any session opens.** A missing `--file` aborts
  immediately, so a half-opened session is never left behind.
- **Baseline cursor before sending.** The current message count is recorded
  *before* the send, so only messages produced by this turn are emitted — prior
  history is not re-printed.
- **In-progress messages are held back.** A message still flagged
  `streaming_in_progress` is not emitted until final (its content is still
  growing); the CLI re-reads it on the next poll.
- **Bounded waiting.** A start-grace window (120 s) covers env wake / queueing
  before the turn begins; an overall `--timeout` (default 600 s) bounds the whole
  wait. Either bound emits a `warning`/`timeout` event rather than hanging.
- **Ctrl-C interrupts the remote turn.** It calls the session's interrupt route,
  emits an `interrupted` event, and exits 130 — it does not leave the turn running
  unobserved.
- **Download size cap.** Agent attachments are fetched through the buffered proxy,
  so a file larger than the proxy's 8 MiB response cap surfaces a clear error
  instead of a partial write.
- **Upload is the one non-proxy call.** The api-proxy is JSON-only and can't carry
  multipart, so uploading a local attachment uses a dedicated account-CLI route;
  everything else in `cinna chat` rides the proxy.

## Agent resolution

- `--agent <ref>` resolves a name / slug / id against the account's accessible
  agents (`list_account_agents`).
- Omitted, the agent is inferred from the **per-agent workspace** you're standing
  in (its `.cinna/config.json` `agent_id`).
- Neither available ⇒ a fail-loud error asking for `--agent` or to run from inside
  a synced agent workspace.
- `--resume <session_id>` bypasses agent resolution entirely — the session already
  knows its agent.

## Architecture overview

```
cinna chat … ─► chat.py:run_chat()
      │  (account token, .cinna/account.json)
      ▼
AccountClient ── api-proxy (buffered JSON) ─► platform conversation API
      │   create_session / send_message / get_messages / streaming-status / interrupt
      │
      ├─ upload_file ─► POST /cli/account/files/upload (multipart, the one non-proxy route)
      └─ download_file ─► GET /files/{id}/download (≤ 8 MiB via proxy)
                                   │
   send (JSON ack) ──► agent turn runs async on the platform
                                   │
   poll get_messages + streaming-status ──► emit NDJSON: session / upload / message / status / done
```

## Integration points

- **Account workspace** — `cinna chat` is an account-scoped command; it reuses the
  same `.cinna/account.json` auth and api-proxy as `cinna api` and the other
  `cinna account …` verbs, and resolves `--agent` against the account's agents.
- **Live Sync** (`../live_sync/live_sync.md`) —
  edit a prompt/script, let Mutagen mirror it to the running container, then
  `cinna chat` exercises that live version through a real session. The pairing is
  the core build-test loop.
- **Remote Exec** (`../remote_exec/remote_exec.md`) —
  `cinna exec` runs an arbitrary command in the agent-env over an SSE stream;
  `cinna chat` instead drives the full conversation pipeline. Exec is the
  low-level shell into the container; chat is the high-level "talk to the agent the
  way a user would". Both stream agent activity, but chat polls persisted messages
  while exec reads SSE directly.

Implementation: see [remote_chat_tech.md](remote_chat_tech.md). Real-usage e2e
test scenarios: see [remote_chat_acceptance.md](remote_chat_acceptance.md).
