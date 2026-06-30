# Remote Chat — Acceptance Scenarios (live e2e)

The catalog of **real-usage scenarios** for an agent doing *integration* testing
of `cinna chat` against a **live** environment — a real platform backend, a real
account workspace, and real agent containers running the production conversation
pipeline. These are not unit tests; they exist to catch what unit tests miss:
async turn timing, env wake / queueing, the persisted-trace shape, attachment
round-trips through the size-capped proxy, and session/cursor drift across turns.

How to use: pick scenarios relevant to the change, run the **Steps** verbatim
against a live env, assert the **Expected**, and watch for the **Watch for**
failure modes. The default NDJSON output is the contract a calling coding agent
depends on — assert on parsed events, not on prose.

## Preconditions

- A reachable platform (e.g. `http://localhost:8000` backend, `:5173` frontend)
  with an **account workspace** already set up (`cinna account setup …` or
  `cinna login`) — `.cinna/account.json` present and its token valid.
- **Editable install** of the CLI under test:
  `python3 -c "import cinna,os;print(os.path.dirname(cinna.__file__))"` must point
  at this repo's `src/cinna`. Confirm `which cinna` resolves and
  `cinna chat --help` lists the options.
- **At least one** agent accessible to the account whose env can run a turn
  (a model/SDK configured). Ideally one that can **attach a file** to a reply
  (e.g. via a `<cinna_attach>` workflow) to exercise downloads.
- `jq` available to assert on the NDJSON stream.

> Run `cinna chat` from the account root, or from a synced `agents/<slug>/` folder
> under it (the account config is found by walking up). With no `--agent` inside a
> synced agent folder, the agent is inferred from that folder's config.

## Scenario catalog

### 1. New session, message, reply (the happy path)

- **Goal:** a coding agent sends a message and gets the agent's reply as NDJSON.
- **Setup:** account workspace with one runnable agent.
- **Steps:**
  ```
  cinna chat --agent "<Agent Name>" "Say hello and nothing else" \
    | tee /tmp/chat1.ndjson
  jq -r '.event' /tmp/chat1.ndjson
  ```
- **Expected:** the first event is `session` (with a real `session_id` and
  `mode: conversation`), at least one `message` event with `role: agent` and
  non-empty `content`, and the **last** event is `done` with a `result_state`.
- **Watch for:** the loop exiting before the agent turn starts (no agent message);
  prior history being re-emitted (baseline cursor not taken before send); the
  in-progress message emitted with partial content.

### 2. Reasoning/tool trace is present, and `--no-events` drops it

- **Goal:** the caller can see *how* the agent answered, and can opt out.
- **Steps:**
  ```
  cinna chat --agent "<Agent Name>" "Use a tool, then summarize" > /tmp/chat2.ndjson
  jq 'select(.event=="message" and .role=="agent") | .events' /tmp/chat2.ndjson
  cinna chat --agent "<Agent Name>" --no-events "Just answer briefly" > /tmp/chat2b.ndjson
  jq 'select(.event=="message" and .role=="agent") | has("events")' /tmp/chat2b.ndjson
  ```
- **Expected:** the first run's agent message has an `events` array with ordered
  `thinking` / `tool` (each tool carrying `tool_name` + `tool_input`) / tool-result
  entries; `attachment` entries do **not** appear in `events`. The `--no-events`
  run's agent message has `events` absent, `content` still present.
- **Watch for:** attachments leaking into the trace (double-reporting); tool
  payloads missing; `--no-events` still emitting the trace.

### 3. Upload a file with the message (the one non-proxy route)

- **Goal:** a local file reaches the agent as an attachment.
- **Steps:**
  ```
  printf 'a,b\n1,2\n' > /tmp/data.csv
  cinna chat --agent "<Agent Name>" --file /tmp/data.csv \
    "What columns are in this CSV?" > /tmp/chat3.ndjson
  jq 'select(.event=="upload")' /tmp/chat3.ndjson
  ```
- **Expected:** an `upload` event with a real `file_id` and `filename: data.csv`;
  the agent's reply references the CSV's columns (the file id was carried in the
  message). A missing `--file` path aborts **before** any `session` event.
- **Watch for:** the upload going through the JSON proxy (it must hit the
  multipart `/files/upload` route); a session being created before file validation.

### 4. Download an agent-produced attachment

- **Goal:** a file the agent attaches to its reply is saved locally.
- **Setup:** an agent that attaches a file to its response.
- **Steps:**
  ```
  cinna chat --agent "<Agent Name>" "Generate a small text file and attach it" \
    > /tmp/chat4.ndjson
  jq 'select(.event=="message") | .attachments' /tmp/chat4.ndjson
  ls cinna-chat-files/*/
  ```
- **Expected:** the agent message's `attachments[]` carries a `file_id`,
  `filename`, and `downloaded_to`; the file exists on disk under
  `cinna-chat-files/<session_id>/`. With `--no-download`, `downloaded_to` is absent
  and only the `file_id` is reported.
- **Watch for:** a >8 MiB attachment partial-writing instead of failing loud;
  the same file id downloaded twice (dedup); a path-traversal filename escaping the
  download dir.

### 5. Resume a session keeps context, creates no new session

- **Goal:** a multi-turn conversation reuses one session.
- **Steps:**
  ```
  SID=$(cinna chat --agent "<Agent Name>" "Remember the number 42." \
    | jq -r 'select(.event=="session") | .session_id')
  cinna chat --resume "$SID" "What number did I ask you to remember?" \
    > /tmp/chat5.ndjson
  jq 'select(.event=="session") | {resumed, session_id}' /tmp/chat5.ndjson
  ```
- **Expected:** the resume run's `session` event has `resumed: true` and the same
  `session_id`; the agent answers `42` (prior turn's context retained). No new
  session id is minted.
- **Watch for:** `--resume` creating a fresh session; context lost; a bad
  `--resume` id producing a hang instead of a clean "could not resume" error.

### 6. Pretty output for a human

- **Goal:** `--pretty` renders a readable transcript instead of NDJSON.
- **Steps:** `cinna chat --agent "<Agent Name>" --pretty "Hello"`.
- **Expected:** a Rich transcript — a `session` line, role-colored message blocks,
  thinking/tool lines, the final content, and a `done` line. No raw JSON.
- **Watch for:** NDJSON leaking through; a crash on an event kind the pretty
  renderer doesn't special-case.

### 7. Message via stdin and via interactive prompt

- **Goal:** the message can come from somewhere other than argv.
- **Steps:**
  ```
  echo "ping" | cinna chat --agent "<Agent Name>" > /tmp/chat7.ndjson
  jq -r 'select(.event=="message" and .role=="user") | .content' /tmp/chat7.ndjson
  cinna chat --agent "<Agent Name>"            # interactive TTY: type a message
  cinna chat --agent "<Agent Name>" < /dev/null   # empty stdin, non-TTY
  ```
- **Expected:** the piped run sends `ping`; the TTY run prompts for a message; the
  empty-stdin non-TTY run **errors cleanly** with "No message provided" (exit
  non-zero) rather than hanging.
- **Watch for:** a hang on empty non-TTY stdin; the prompt firing in a non-TTY.

### 8. Start-grace and timeout bounds are fail-loud

- **Goal:** a slow/cold env or a stuck turn does not hang the CLI forever.
- **Steps:**
  ```
  # Against a suspended env (cold start), or with a deliberately tiny bound:
  cinna chat --agent "<Agent Name>" --timeout 5 "Do something slow" \
    > /tmp/chat8.ndjson
  jq -r '.event' /tmp/chat8.ndjson | tail -3
  ```
- **Expected:** if the turn never starts within the start-grace window a `warning`
  event is emitted; if it starts but exceeds `--timeout` a `timeout` event is
  emitted (with `seconds`). Either way a terminal `done` follows and the process
  returns — it never hangs.
- **Watch for:** an unbounded wait; settling before the turn started (no agent
  message but a clean `done`).

### 9. Ctrl-C interrupts the remote turn

- **Goal:** interrupting locally stops the agent's turn, not just the CLI.
- **Steps:** start `cinna chat --agent "<Agent Name>" "Run a long task"`, then
  press Ctrl-C mid-turn.
- **Expected:** an `interrupted` event is emitted, the process exits 130, and the
  session's turn is stopped server-side (a later `cinna chat --resume <sid>`
  shows the turn did not continue).
- **Watch for:** the interrupt route not being called (turn keeps running
  unobserved); a non-130 exit.

### 10. Runs from a synced agent folder with inferred agent

- **Goal:** no `--agent` needed inside a synced agent workspace.
- **Steps:**
  ```
  cd agents/<slug>/<subdir>     # a synced per-agent workspace under the account root
  cinna chat "Who are you?" > /tmp/chat10.ndjson
  jq 'select(.event=="session") | .agent_id' /tmp/chat10.ndjson
  ```
- **Expected:** the agent is inferred from the folder's `.cinna/config.json`; the
  `session` event's `agent_id` matches that agent. Outside any account workspace,
  the command errors fail-loud.
- **Watch for:** `--agent` inference failing where the account root walk should
  still find `.cinna/account.json`; the wrong agent inferred.

### 11. Account-workspace requirement is enforced

- **Goal:** `cinna chat` refuses to run without an account context.
- **Steps:** from a directory with no `.cinna/account.json` anywhere up the tree,
  `cinna chat --agent "<Agent Name>" "Hi"`.
- **Expected:** a clean non-zero error about the missing account workspace; no
  session is created and no per-agent token is used as a fallback.
- **Watch for:** a stack trace instead of a clear message; a silent fallback to a
  per-agent token.

## Cross-cutting invariants (must hold across all scenarios)

- **NDJSON contract** — default output is one JSON object per line; the first event
  is `session`, the last is `done` (or a terminal `interrupted` on Ctrl-C). A
  calling agent must be able to `jq` every line.
- **No history flood** — only messages produced by *this* turn are emitted (the
  baseline cursor is taken before the send).
- **No partial finals** — an in-progress message is never emitted as final.
- **Fail-loud, never hang** — missing account workspace, missing `--file`, empty
  non-TTY message, oversize download, start-grace, and timeout all produce a clear
  event/error and a bounded exit.
- **Account-token only** — every call rides the account token; `cinna chat` never
  reaches for a per-agent token.
- **Upload is the only non-proxy call** — all session/message/download traffic goes
  through the api-proxy; only the multipart upload uses `/files/upload`.

## Cleanup

- Remove downloaded attachments: `rm -rf cinna-chat-files/` (and any custom
  `--download-dir`).
- Remove scratch inputs: `rm -f /tmp/data.csv /tmp/chat*.ndjson`.
- Sessions created in testing are server-side conversation history; delete them
  from the platform UI if the env should be left pristine (the CLI does not delete
  sessions). Note any session ids you created for teardown.
