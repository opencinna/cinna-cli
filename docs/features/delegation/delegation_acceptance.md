# Durable Delegation — Acceptance Scenarios (live e2e)

The catalog of **real-usage scenarios** for an agent integration-testing
`cinna delegation` against a **live** platform: a real cinna-core backend with
the durable-delegation extension, a real account workspace, and at least one
real target agent. These exercise backend dedup, task execution, the result /
question / reply cycle, and the executor-side report from inside a cloud task —
the parts unit tests only mock.

How to use: run the **Steps** from inside the account workspace (except where a
scenario says "inside the cloud task"), assert the **Expected**, and check the
**Watch for** items. Scenarios 2–3 (retry identity) and 9–11 (executor report)
are the highest-value ones for any change to `src/cinna/delegation.py`.

## Preconditions

- A reachable cinna-core backend whose `tasks/delegation-capabilities` returns
  `version: 1`; ideally also an older backend without it (scenario 13).
- An **account workspace** (`.cinna/account.json` reachable from the cwd;
  `cinna login` if the token expired).
- **Editable install**: `which cinna` resolves, and
  `python3 -c "import cinna,os;print(os.path.dirname(cinna.__file__))"` points at
  this repo's `src/cinna`. `cinna delegation --help` lists create, status,
  report, reply.
- At least one **target agent** UUID (`cinna account agents`), with a running
  or startable environment.
- `jq` for asserting JSON output.
- Use a fresh key per run, e.g. `KEY=acc-$(date +%s)`, so earlier runs do not
  dedup into this one.

## Scenario catalog

### 1. Create without executing

- **Goal:** register work without starting it.
- **Steps:**
  ```
  cinna delegation create --id "$KEY" --target "$AGENT" --title Research --brief 'Find the facts'
  ```
- **Expected:** exit 0; output shows the backend task id and that it will
  **not** execute. The task appears in the platform UI for the target agent,
  not started. Record the id as `TASK`.
- **Watch for:** the delegation id / requester key printed instead of the
  backend task id; the task auto-starting.

### 2. Retry reuses the original task (idempotent create)

- **Goal:** a retry after a timeout never duplicates work.
- **Steps:**
  ```
  cinna delegation create --id "$KEY" --target "$AGENT" --title Research --brief 'Find the facts' --json | jq -r .delegation.id
  cinna delegation create --id "$KEY" --target "$AGENT" --title 'Changed title' --brief 'Changed brief' --json | jq -r .delegation.id
  ```
- **Expected:** both ids equal `TASK`. The platform shows one task, still titled
  `Research` with the original brief.
- **Watch for:** a second task; the title/brief being overwritten; a non-`ok`
  result on the retry.

### 3. `--execute` runs at most once

- **Goal:** execution is never launched twice for the same key.
- **Steps:**
  ```
  KEY2=$KEY-exec
  cinna delegation create --id "$KEY2" --target "$AGENT" --title Run --brief 'Say hello' --execute
  cinna delegation create --id "$KEY2" --target "$AGENT" --title Run --brief 'Say hello' --execute
  ```
- **Expected:** the first reports the task executes; the second returns the same
  task id. The platform shows exactly one execution/session for that task.
- **Watch for:** two sessions on the target agent; the retry reporting a new
  execution.

### 4. Same key, different target is different work

- **Goal:** identity is target + key.
- **Steps:** repeat scenario 1's command with `--target "$AGENT_B"` (a second
  agent) and the same `$KEY`.
- **Expected:** a new, different task id.
- **Watch for:** dedup across targets (identity must include the target).

### 5. Depth and root rules

- **Goal:** sub-delegation shape is enforced client-side.
- **Steps:**
  ```
  cinna delegation create --id "$KEY-d2" --target "$AGENT" --title T --brief B --depth 2
  cinna delegation create --id "$KEY-d1" --target "$AGENT" --title T --brief B --root "$ROOT_ID"
  cinna delegation create --id "$KEY-d3" --target "$AGENT" --title T --brief B --depth 3
  cinna delegation create --id "$KEY-d2" --target "$AGENT" --title T --brief B --depth 2 --root "$ROOT_ID" --json
  ```
  (`$ROOT_ID` is the `delegation_metadata.id` of the task from scenario 1.)
- **Expected:** the first three fail with a usage error (exit 2) and create
  nothing; the last succeeds and its `delegation_metadata` has `depth: 2`,
  `root: $ROOT_ID`. A depth-1 task's metadata has `root` equal to its own id.
- **Watch for:** a task created by an invalid invocation; `root` null at depth 1.

### 6. Status is a single read

- **Goal:** read state, latest result and open question once.
- **Steps:**
  ```
  cinna delegation status "$TASK"
  cinna delegation status "$TASK" --json | jq .result
  ```
- **Expected:** human output shows state, latest result (or none), and any open
  question with its result id; returns immediately. JSON prints `"ok"` with the
  task detail under `.delegation`.
- **Watch for:** the command waiting or polling; missing result id next to an
  open question.

### 7. Owner-side report and blocked question

- **Goal:** record results and ask a question through the account workspace.
- **Steps:**
  ```
  cinna delegation report "$TASK" --status in_progress --summary 'Started'
  cinna delegation report "$TASK" --status blocked --summary 'Need input'
  cinna delegation report "$TASK" --status blocked --summary 'Need input' --question 'Which source?' --audience user
  cinna delegation status "$TASK"
  ```
- **Expected:** the first succeeds; the second fails client-side (blocked
  without a question) and sends nothing; the third succeeds. `status` shows the
  open question `Which source?`, audience `user`, and its result id (`RESULT`).
- **Watch for:** a blocked result accepted without a question; audience
  defaulting wrongly (default is `requester`).

### 8. Reply answers the question

- **Goal:** close the question loop.
- **Steps:**
  ```
  cinna delegation reply "$TASK" --result-id "$RESULT" --message 'Use the primary source'
  cinna delegation status "$TASK"
  ```
- **Expected:** exit 0; the question is no longer open and the reply is visible
  on the task. A wrong `--result-id` yields a platform error with the backend
  detail (exit 1).
- **Watch for:** reply accepted against a non-question result id.

### 9. Artifacts are validated

- **Goal:** only well-formed, portable artifact references are sent.
- **Steps:**
  ```
  cinna delegation report "$TASK" --status done --summary Done --artifact '{"kind":"link","name":"r","ref":"https://example.com/r.pdf"}'
  cinna delegation report "$TASK" --status done --summary Done --artifact 'not json'
  cinna delegation report "$TASK" --status done --summary Done --artifact '{"kind":"link","name":"r","ref":"file:///tmp/r.pdf"}'
  cinna delegation report "$TASK" --status done --summary Done --artifact '{"kind":"","name":"r","ref":"https://x"}'
  cinna delegation report "$TASK" --status done --summary Done --artifact '["a"]'
  ```
- **Expected:** only the first succeeds and the artifact appears on the latest
  result. The others fail client-side (exit 2) with no request sent.
- **Watch for:** local paths or non-http refs accepted; empty `kind`/`name`
  accepted; a JSON array accepted as an artifact.

### 10. Executor report from inside the cloud task

- **Goal:** the working agent reports on its own current task without a task id.
- **Setup:** a task created with `--execute` (scenario 3), running in the target
  environment where `AGENT_AUTH_TOKEN`, `BACKEND_URL`, `ENV_ID` are set and
  `session_context.json` holds `backend_session_id`.
- **Steps (inside the cloud task, e.g. as an instruction in the brief):**
  ```
  cinna delegation report --status done --summary 'Finished' --json
  ```
  Then, from the account workspace: `cinna delegation status "$TASK3"`.
- **Expected:** `{"result": "ok", "delegation": {…}}`; `status` shows the `done`
  result on that task. The request went to
  `$BACKEND_URL/api/v1/agent/tasks/current/delegation-result` with the bearer
  token and `X-Agent-Env-Id`.
- **Watch for:** the result landing on a different task; the command trying to
  load an account workspace.

### 11. Executor report fails loud without its context

- **Goal:** no silent fallback when not in a cloud task.
- **Steps (from a plain shell, then with a partial env):**
  ```
  env -u AGENT_AUTH_TOKEN -u BACKEND_URL -u ENV_ID cinna delegation report --status done --summary x
  AGENT_AUTH_TOKEN=t BACKEND_URL=https://backend.example ENV_ID=e CINNA_SESSION_CONTEXT_PATH=/nonexistent.json cinna delegation report --status done --summary x
  ```
- **Expected:** both fail before any network call with a clear message (missing
  environment / no current session). Nothing is recorded on any task.
- **Watch for:** falling back to the account workspace; a request sent with an
  empty `X-Agent-Env-Id` or no `source_session_id`.

### 12. Executor report rejected by the backend

- **Goal:** backend verification and error surfacing.
- **Steps (inside a cloud environment):** point `CINNA_SESSION_CONTEXT_PATH` at
  a file whose `backend_session_id` belongs to another agent's session, then run
  `cinna delegation report --status done --summary x --json`.
- **Expected:** the backend refuses; the CLI prints
  `{"result": "error", "code": "platform_error", "http_status": 4xx, …}` and
  exits 1 (5xx → `platform_unavailable`, exit 12). A 3xx is not followed and is
  also an error.
- **Watch for:** the report accepted for a foreign session; a redirect followed
  with the bearer token.

### 13. Unsupported server

- **Goal:** capability gate on every owner-side verb.
- **Steps (against a backend without the extension, or one reporting another
  version):**
  ```
  cinna delegation create --id "$KEY" --target "$AGENT" --title T --brief B
  cinna delegation status "$TASK"
  cinna delegation report "$TASK" --status done --summary x
  cinna delegation reply "$TASK" --result-id r --message m
  ```
- **Expected:** each fails with "This server does not support durable
  delegations." and no task is created or changed.
- **Watch for:** a create going through on an old backend (which would ignore
  `external_ref` and break retry safety).

### 14. `--json` is per-command

- **Goal:** machine output works after the subcommand.
- **Steps:**
  ```
  cinna delegation status "$TASK" --json
  cinna delegation create --id "$KEY" --target "$AGENT" --title T --brief B --json
  ```
- **Expected:** each prints exactly one JSON line with `result: "ok"` and the
  backend body under `delegation`; no Rich output on stdout.
- **Watch for:** extra human lines mixed into stdout; the body at top level
  instead of under `delegation`.

### 15. Outside an account workspace

- **Steps:** `cd /tmp && cinna delegation status "$TASK"`
- **Expected:** fails loud pointing at the missing account workspace; no request.

## Cross-cutting invariants

- One target + key ⇒ one task and at most one execution, across any number of
  retries and any changed title/brief.
- Every owner-side verb calls the capability route first; an unsupported server
  receives no task-route writes.
- Client-side validation failures (depth/root, blocked without question,
  artifacts, missing executor env) send **no** request.
- The executor bearer token is only ever sent to `BACKEND_URL` (no redirects).
- Nothing is written to the workspace, `.cinna/`, or `~/.cinna/agents.json`. <!-- nocheck -->
- Every `--json` invocation ends with exactly one `{"result": …}` line.

## Cleanup

- Delete or archive the test tasks (keys prefixed `acc-`) in the platform UI, or
  via `cinna api DELETE tasks/<id>` where the backend allows it.
- Stop any sessions left running on the target agent by `--execute` scenarios.
