# Remote Chat — Technical Reference

Implementation of [remote_chat.md](remote_chat.md). cinna-cli is a Python CLI;
all logic lives in `src/cinna/`, tests in `tests/`.

## File locations

- `src/cinna/chat.py` — the feature core: the `run_chat` driver, the `_Emitter`
  (NDJSON / Rich), the poll loop, message/event/attachment extraction, and
  attachment download.
- `src/cinna/client.py` — `AccountClient`: the api-proxy transport
  (`_proxy_json` / `api_proxy`), the conversation session/message methods, the
  dedicated `upload_file` route, and `download_file`.
- `src/cinna/main.py` — the `cinna chat` command (`chat_cmd`), its options, and
  the thin call into `run_chat`.
- `src/cinna/account.py` — account-workspace discovery (`find_account_root`,
  `load_account_config`) and `--agent` resolution (`_resolve_account_agent`).
- `src/cinna/config.py` — per-agent workspace inference for the no-`--agent` path
  (`find_workspace_root`, `load_config`).
- Tests: `tests/test_chat.py` (NDJSON emission, reasoning/tool trace, `--no-events`,
  attachment download, `--no-download`, upload + file-id wiring, `--resume`,
  missing-message error, account-workspace requirement, and the proxy/upload
  client-method tests).

## Command surface

- `cinna chat` → `src/cinna/main.py:chat_cmd()` → `src/cinna/chat.py:run_chat()`.

Key options (all on `chat_cmd`): `--agent`, `--resume`, `--file` (repeatable),
`--mode` (`conversation`|`building`, new sessions only), `--title`,
`--download-dir`, `--no-download`, `--interval`, `--timeout`,
`--events/--no-events`, `--pretty`. The trailing `MESSAGE` is `nargs=-1` with
`ignore_unknown_options`, so the message can be unquoted free text.

## Key functions & flow

- `src/cinna/chat.py:run_chat()` — the whole turn, in order:
  1. `find_account_root()` + `load_account_config()` — auth context (fail-loud).
  2. Validate every `--file` exists *before* opening a session.
  3. Resolve the message text: positional tokens → stdin (non-TTY) → interactive
     prompt; error if empty and no files.
  4. Open the client; resolve the session — `get_session(resume)` or
     `create_session(agent_id, mode, title)` via `_resolve_agent_id`.
  5. Emit the `session` event.
  6. `upload_file` each attachment → `file_ids`; emit an `upload` event each.
  7. `_message_count()` — baseline poll cursor (paged so a long history is counted
     fully).
  8. `send_message(session_id, message, file_ids)` — JSON ack; `expect_turn` is
     set from `streaming|pending|queued` in the ack.
  9. `_poll_turn(...)`; `KeyboardInterrupt` → `interrupt_message` + exit 130.
- `src/cinna/chat.py:_resolve_agent_id()` — `--agent` → `list_account_agents()` +
  `_resolve_account_agent()`; else infer from the surrounding agent workspace's
  `config.agent_id`; else fail-loud.
- `src/cinna/chat.py:_message_count()` — pages `get_messages` by `_MESSAGE_PAGE`
  (500) to get the total already-present count (the starting `offset`).
- `src/cinna/chat.py:_poll_turn()` — the poll loop:
  - Inner drain: `get_messages(offset=consumed)`; emit each finalized message and
    advance `consumed`; **stop at the first** message flagged
    `message_metadata.streaming_in_progress` (emit a one-shot `status: working`).
  - `get_streaming_status()` → `is_streaming`. Settle when `turn_started and not
    streaming and not in_progress`.
  - `turn_started` flips on the first non-user message or `is_streaming` true;
    `expect_turn` seeds it (no expected turn ⇒ already started).
  - `START_GRACE_SECONDS` (120) bounds time-to-start (emit `warning`); `--timeout`
    bounds the whole wait (emit `timeout`); `--interval` (2.0 s) between polls.
  - Always ends with `_emit_done()`.
- `src/cinna/chat.py:_emit_message()` — builds the `message` event (id/role/seq/
  timestamp/content/status), attaches the `events` trace (unless `--no-events`),
  and the `attachments` list (downloading each when a `dl_dir` is set).
- `src/cinna/chat.py:_extract_events()` — normalizes
  `message_metadata.streaming_events` into the ordered trace; drops
  `_TRACE_SKIP_TYPES` (`attachment`, `attachment_error`, `done`); carries
  `tool_name` / `tool_id` / `tool_input` / `tool_use_id` from the event metadata.
- `src/cinna/chat.py:_extract_attachments()` — collects agent attachments,
  preferring inline `attachment` streaming events and falling back to the
  message's `files[]` entries with `source == "agent_attachment"`; dedups by
  `file_id`.
- `src/cinna/chat.py:_download_attachment()` — `download_file(fid)` → write bytes
  into `dl_dir`, annotate the dict with `downloaded_to` / `download_error`
  (filename is `Path(...).name`-sanitized).
- `src/cinna/chat.py:_emit_done()` — final `done` event with
  `interaction_status` / `result_state` / `result_summary` from `get_session`.
- `src/cinna/chat.py:_download_dir()` — resolves `<download_dir or
  ./cinna-chat-files>/<session_id>/`.
- `src/cinna/chat.py:_Emitter` — NDJSON (one `json.dumps` line, flushed) by
  default; Rich rendering when `pretty`.

## Transport (`src/cinna/client.py:AccountClient`)

- `src/cinna/client.py:AccountClient._proxy_json()` — wraps `api_proxy` and
  returns parsed JSON, raising a typed `PlatformError`. Distinguishes a **hatch
  refusal** (the `x-cinna-proxied` marker header absent ⇒ policy / rate-limit /
  size-cap) from a mirrored inner-route error.
- Conversation methods, all via `_proxy_json` (inner routes, not CLI routes):
  - `create_session()` → `POST /sessions/`
  - `get_session()` → `GET /sessions/{id}`
  - `get_messages()` → `GET /sessions/{id}/messages?limit&offset` (ascending by
    `sequence_number`; `offset` is the cursor)
  - `send_message()` → `POST /sessions/{id}/messages/stream` (JSON ack)
  - `get_streaming_status()` → `GET /sessions/{id}/messages/streaming-status`
  - `interrupt_message()` → `POST /sessions/{id}/messages/interrupt`
- `src/cinna/client.py:AccountClient.download_file()` → `GET /files/{id}/download`
  through the proxy; raises with the **8 MiB** cap message on a hatch refusal.
- `src/cinna/client.py:AccountClient.upload_file()` → `POST
  /api/v1/cli/account/files/upload` — **multipart, not the proxy** (the only
  conversation call that bypasses it); account-token auth; returns
  `FileUploadPublic` whose `id` feeds a message's `file_ids`.

## External contracts

- **api-proxy:** `POST /api/v1/cli/account/api-proxy` (account token). Mirrors the
  inner route's status + body 1:1 and sets the `x-cinna-proxied` marker header on
  a real mirrored response. Inner routes consumed: `POST /sessions/`,
  `GET /sessions/{id}`, `GET /sessions/{id}/messages`,
  `POST /sessions/{id}/messages/stream`,
  `GET /sessions/{id}/messages/streaming-status`,
  `POST /sessions/{id}/messages/interrupt`, `GET /files/{id}/download`.
- **Upload route:** `POST /api/v1/cli/account/files/upload` (account token,
  multipart) — added alongside the other `/cli/account/*` routes specifically
  because the proxy is JSON-only.
- **Message shape relied on:** the in-progress assistant message carries
  `message_metadata.streaming_in_progress`; the persisted trace is
  `message_metadata.streaming_events` (typed events with `event_seq`, `content`,
  and tool `metadata`); agent attachments appear as `attachment` events and/or
  `files[]` entries with `source == "agent_attachment"`. The send ack signals a
  pending turn via `streaming` / `pending` / `queued`.

## Constants & defaults (`src/cinna/chat.py`)

- `DEFAULT_POLL_INTERVAL` = 2.0 s (`--interval`).
- `DEFAULT_TIMEOUT` = 600 s (`--timeout`).
- `START_GRACE_SECONDS` = 120 s — time-to-start budget.
- `_MESSAGE_PAGE` = 500 — message page size (baseline count + drain).
- `DEFAULT_DOWNLOAD_DIR` = `cinna-chat-files` — attachment download base.
- `_TRACE_SKIP_TYPES` = `{attachment, attachment_error, done}` — trace exclusions.

## Edge cases & guardrails (preserve these)

- **Baseline cursor before send** — `_message_count` must run *before*
  `send_message`, or prior history floods the output. (`tests/test_chat.py`)
- **Hold in-progress messages** — the drain breaks at the first
  `streaming_in_progress` message so partial content is never emitted as final.
- **Settle requires turn_started** — `expect_turn` seeds it from the ack;
  otherwise the loop could exit before the async turn even begins.
- **Validate files first** — a missing `--file` aborts before a session is opened,
  leaving no orphaned session. (`tests/test_chat.py`)
- **Resume must not create** — `--resume` calls `get_session`, never
  `create_session`. (`tests/test_chat.py`)
- **Trace vs attachments split** — `attachment` events are excluded from the
  `events` trace and surfaced under `attachments` (dedup by file id), so they
  aren't double-reported. (`tests/test_chat.py`)
- **Upload bypasses the proxy** — multipart can't ride the JSON hatch; verified to
  hit the dedicated `/files/upload` route with a `multipart/form-data` body.
  (`tests/test_chat.py`)
- **Download cap is fail-loud** — a hatch refusal on download (e.g. > 8 MiB)
  raises an actionable `PlatformError`, not a partial file. (`tests/test_chat.py`)
- **Hatch refusal is distinguishable** — the absent `x-cinna-proxied` marker maps
  to an "escape hatch refused" error, separate from a mirrored inner-route status.
  (`tests/test_chat.py`)
