# Durable Delegation (`cinna delegation`)

## Purpose

Let a requester (a developer, a local coding agent, or a script) hand a piece of
work to a remote platform agent as a **durable delegation**: a platform task
with a stable identity that survives retries, reports structured results back,
and can ask the requester (or a human) a question and wait for the answer.
The same command group lets the cloud agent doing the work report its own
progress from inside its task session.

## Mental model / core concepts

A delegation is **not** local state. It is a platform task (the same task
record the platform UI shows) carrying extra delegation metadata. The CLI is a
remote control over it; nothing is written to the workspace.

- **Requester key (`--id`)** — a name the requester picks for one piece of work
  (`research-1`). Together with the target agent it forms the delegation's
  identity. Reusing the same key against the same target means "the same work",
  never "new work".
- **Target** — the UUID of the remote agent that will do the work.
- **Task id** — the backend id of the created task. `create` prints it; every
  other command (`status`, `report`, `reply`) takes it.
- **Result** — a structured report on the task: a status (`in_progress`,
  `blocked`, `done`, `failed`), a summary, an optional body, optional
  artifacts, and — when blocked — a question with an audience.
- **Question / reply** — a `blocked` result carries a question. Its result id
  is the handle the answer is sent against (`reply --result-id`).
- **Audience** — who must answer a question: `requester` (the agent/tool that
  created the delegation) or `user` (a human decision).
- **Depth and root** — a delegation may itself delegate once more. Depth 1 is a
  top-level delegation whose root is itself; depth 2 is a sub-delegation that
  names its depth-1 root. Deeper chains are not allowed.
- **Owner side vs executor side** —
  - *Owner side*: run from an **account workspace**, authenticated by the
    account token, reaching the platform through the account API proxy
    (`create`, `status`, `reply`, and `report` with a task id).
  - *Executor side*: run **inside a cloud task session** in the agent's
    environment, authenticated by the environment's own agent token
    (`report` without a task id). It reports on "the task I am currently
    running" — it never needs to know the task id.
- **Capability negotiation** — durable delegations are a versioned backend
  extension. Every owner-side command first asks the server whether it supports
  version 1; if not, the command refuses instead of calling routes that may not
  exist.

## User flows

### Hand work to a remote agent
1. From the account workspace run
   `cinna delegation create --id research-1 --target <agent uuid> --title … --brief …`,
   adding `--execute` to start the work immediately.
2. The CLI prints the backend task id and whether the task will execute.
3. On a network error or timeout, run the **same command again**: the platform
   returns the original task. Any changed `--title` / `--brief` on the retry is
   ignored, and execution is never launched a second time.

### Follow up without polling
4. `cinna delegation status <task id>` reads the task once: its state, the
   latest result, and any open question together with that question's result id.
5. A requester agent should **end its turn** after delegating and let Cinna
   Desktop deliver the result, rather than polling `status` in a loop.

### Report progress (owner side)
6. `cinna delegation report <task id> --status in_progress --summary …` records a
   result on the task through the account workspace.

### Report progress (inside the cloud task)
7. The agent executing the task runs
   `cinna delegation report --status done --summary … [--artifact …]` with no task
   id. The CLI uses the environment's agent credentials and the current session
   context; the backend attaches the result to the task that session belongs to.

### Ask and answer a question
8. The executor reports `--status blocked --question "Which source?"`, with
   `--audience user` when a human must decide.
9. The requester reads the question and its result id with `status`, then
   answers with `cinna delegation reply <task id> --result-id <id> --message …`.

### Machine-readable use
10. Every command accepts `--json` after the subcommand; the output is a single
    `{"result": "ok", "delegation": …}` line carrying the backend's answer, or
    the standard `{"result": "error", …}` line on failure.

## Business rules

- **Account workspace required** for every owner-side command; outside one they
  fail loud.
- **Capability gate** — if the server lacks the capability route, or reports a
  version other than 1, the command stops with "This server does not support
  durable delegations." and sends nothing else.
- **Retry identity** — the delegation identity is derived only from
  target + requester key. The same pair always yields the same identity, so a
  retry is idempotent. Idempotency **depends on the backend** deduplicating on
  that identity; the CLI keeps no local record of what it created.
- **First write wins** — on a retry the original task is returned unchanged;
  new title/brief text does not update it.
- **Execute at most once** — `--execute` only takes effect when the task is
  newly created. A retry with `--execute` never launches a second run.
- **Depth and root** — depth is 1 or 2. At depth 1 `--root` is forbidden (the
  delegation is its own root). At depth 2 `--root` is required.
- **Origin** — CLI-created delegations are marked as originating externally
  (not from a platform agent, chat, or task).
- **Blocked needs a question** — `--status blocked` without `--question` is
  refused before any request.
- **Audience** — defaults to `requester`; use `user` when a human must decide.
- **Artifacts are references, not uploads** — each `--artifact` is a JSON object
  with a non-empty `kind`, a non-empty `name`, and an `http(s)` `ref`. The CLI
  checks only that shape; the backend further restricts `kind` (currently
  `file` or `link`). Local files must be uploaded separately first; the CLI
  never uploads them.
- **Executor report is session-bound** — without a task id, `report` needs the
  environment's agent token, backend URL and environment id, plus the current
  session context. Missing any of them is a hard failure. The backend verifies
  that the session belongs to this environment's agent and owner, so an agent
  cannot report on someone else's task.
- **Status is a single read** — no watching, no waiting.
- **Fail loud** — any backend refusal surfaces as a platform error with the
  backend's own detail; nothing is retried silently.

## Architecture overview

```
Owner side (account workspace)
cinna delegation create|status|reply|report TASK_ID
      │
      ▼
delegation.py ──► AccountClient ──► POST /api/v1/cli/account/api-proxy
                                        │  1. GET tasks/delegation-capabilities (version 1?)
                                        │  2. the delegation route
                                        ▼
                                  cinna-core tasks API (dedup, execution, results)

Executor side (inside a cloud task session)
cinna delegation report  (no TASK_ID)
      │  env: AGENT_AUTH_TOKEN, BACKEND_URL, ENV_ID + session_context.json
      ▼
POST {BACKEND_URL}/api/v1/agent/tasks/current/delegation-result
      │  backend checks session ↔ environment agent ↔ owner
      ▼
result attached to the current task ──► delivered to the requester (Desktop)
```

## Integration points

- **Account workspace** — owner-side commands use the account token and the api
  proxy. See [Account Workspace](../account_workspace/account_workspace.md).
- **Agent API / `cinna api`** — the same api-proxy escape hatch `cinna api`
  exposes; delegation wraps the task routes with identity, validation and a
  capability gate. See [Agent API](../agent_api/agent_api.md).
- **Remote chat** — a delegated task runs as a platform session like the ones
  `cinna chat` drives. See [Remote Chat](../remote_chat/remote_chat.md).
- **cinna-core task MCP server (not in this repo)** — inside cloud tasks the
  backend's MCP task server exposes a `handover_report` tool that records the
  same kind of result. It is implemented and documented in cinna-core; this CLI
  only offers the equivalent command-line path.

Implementation: see [delegation_tech.md](delegation_tech.md).
Real-usage e2e scenarios: see [delegation_acceptance.md](delegation_acceptance.md).
