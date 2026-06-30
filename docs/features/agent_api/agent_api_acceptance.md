# Agent REST API — Acceptance Scenarios (live e2e)

The catalog of **real-usage scenarios** for an agent doing *integration* testing
of `cinna agent-api`, `cinna connect agent-api`, and `cinna api` against a
**live** environment — a real platform backend, real agent containers, and at
least one agent that exposes a producer REST API. These are not unit tests; they
exist to catch what unit tests miss: a spec that harvests stale, a silent
query-param drop, a producer→consumer wire whose credential never lands in the
consumer's env, and the escape hatch confusing its own refusal with an inner
error.

How to use: pick the scenarios relevant to the change, run the **Steps**
verbatim against a live env, assert the **Expected**, and watch for the **Watch
for** failure modes.

## Preconditions

- A reachable platform (e.g. `http://localhost:8000` backend, `:5173` frontend)
  with an **account workspace** already set up (`cinna login` / `cinna account
  setup …`). All commands here run from the account root.
- **Editable install** of the CLI under test:
  `python3 -c "import cinna,os;print(os.path.dirname(cinna.__file__))"` must
  point at this repo's `src/cinna`. Confirm `which cinna` resolves and
  `cinna agent-api --help` lists `enable / refresh / spec / call`.
- **A producer agent** you own whose workspace has (or will have) an
  `agent_api/` package + `policy.yaml`. Ideally synced locally
  (`cinna agent sync <producer>`) so you can edit + push the endpoint code.
- **A second agent** to act as the consumer (for the `connect` scenarios).
- Know each agent's display name / slug / id (`cinna account agents`).

> Reference agents by display name, slug, or id. An unresolved ref must fail
> **before** any API call (so a typo never mutates state).

## Scenario catalog

### 1. Enable a producer API and read its status

- **Goal:** a builder turns on the REST API for a producer.
- **Steps:**
  ```
  cinna agent-api enable "<Producer>"
  ```
- **Expected:** `REST API enabled for <Producer>` plus a status block
  (`Enabled: True`, `State`, `Spec available`, and — once harvested —
  `Spec harvested <age>`). When enabled, a next-steps nudge points to authoring
  `agent_api/*.py` + `policy.yaml`, syncing, then `refresh` + `spec`.
- **Watch for:** the toggle reported but the status not echoed back; `enable`
  succeeding on a ref that doesn't resolve (it must resolve first).

### 2. Author endpoints → sync → refresh harvests the spec

- **Goal:** edited endpoint code shows up in the harvested spec on demand.
- **Setup:** the producer is synced locally; you've added/edited an endpoint in
  `workspace/agent_api/` and a rule in `workspace/policy.yaml`.
- **Steps:**
  ```
  cinna sync push --agent <producer>          # or run inside cinna dev
  cinna agent-api refresh "<Producer>"
  cinna agent-api spec "<Producer>" -o spec.json
  ```
- **Expected:** `refresh` reports a fresh `Spec harvested … ago`; `spec.json` is
  valid JSON and contains the new path/operation under `paths`.
- **Watch for:** the spec staying stale after refresh (harvest didn't re-import
  the synced module); `Spec harvested` age not advancing; `spec` emitting
  Rich-decorated output instead of plain pipeable JSON.

### 3. A broken `agent_api/` module surfaces as `Last error`, not a crash

- **Goal:** an author mistake is reported, not thrown.
- **Setup:** introduce a deliberate import error in `workspace/agent_api/` (e.g.
  a bad import) and sync it.
- **Steps:**
  ```
  cinna sync push --agent <producer>
  cinna agent-api refresh "<Producer>"
  ```
- **Expected:** the command exits 0 and the status shows `Last error: …` with a
  warning to fix the code/policy, sync, and refresh again. No Python traceback.
- **Watch for:** the harvest error raising instead of being reported; the error
  masking `Spec available` so a previously-good spec looks gone.

### 4. Owner-preview `call` forwards query params end-to-end

- **Goal:** smoke-test one producer endpoint as the owner, in one shot.
- **Steps:**
  ```
  cinna agent-api call "<Producer>" <path> --query <k>=<v>
  # e.g. cinna agent-api call btc-rate-api btc-rate --query vs_currency=eur
  ```
- **Expected:** output `→ GET <path> [200]` then the JSON body, which reflects
  the query value (e.g. `"vs_currency": "eur"`). Exit code 0.
- **Watch for (the silent-query-drop bug this exists to catch):** the response
  ignoring the query param (defaulting it) while still returning 200 — the call
  path must forward query, unlike a naive consumer probe.

### 5. `call` maps an inner error to a non-zero exit (body still printed)

- **Goal:** the smoke test composes in scripts.
- **Steps:**
  ```
  cinna agent-api call "<Producer>" <missing-path>
  echo "exit=$?"
  ```
- **Expected:** `→ GET <missing-path> [404]` and the error body are printed, and
  `exit=1`. A 2xx would have been `exit=0`.
- **Watch for:** a 4xx/5xx exiting 0; the body suppressed on error.

### 6. POST with a JSON body

- **Goal:** non-GET methods + bodies work through the owner preview.
- **Steps:**
  ```
  cinna agent-api call "<Producer>" <path> -X POST --json '{"sku": "A1"}'
  ```
- **Expected:** the endpoint receives the body and returns its result; exit code
  follows the status. An invalid `--json` string fails fast with a clear
  `--json is not valid JSON` message (no network call).

### 7. Wire a consumer to the producer's REST API

- **Goal:** one-click producer→consumer wiring with no manual key paste.
- **Steps:**
  ```
  cinna connect agent-api --producer "<Producer>" --consumer "<Consumer>" \
      --label producer-link
  ```
- **Expected:** `Connected: <Consumer> → <Producer> (REST API)` plus the
  `Credential` id, `Token prefix`, `Base URL`, and (if present) `Spec URL`, and
  the reminder that the credential rides the consumer's normal credential sync.
- **Watch for:** the secret token printed in full (only a prefix should appear);
  `connect` succeeding when the producer's API is disabled (it should surface
  the backend 400 verbatim — see #9).

### 8. The minted credential lands in the consumer's env

- **Goal:** the wired credential actually reaches the consumer at runtime.
- **Setup:** the consumer is synced locally (`cinna agent sync <consumer>`).
- **Steps:** after #7, let the consumer's credential sync run, then inspect:
  ```
  cinna agent show "<Consumer>"
  ls <consumer-workspace>/workspace/credentials/        # read-only mirror
  ```
- **Expected:** the producer credential appears in the consumer's credential
  metadata / `workspace/credentials/` (read-only). The consumer's own code can
  now call the producer's base URL with it.
- **Watch for:** the credential created on the account but never synced into the
  consumer's env; `--read-only` not reflected in the granted scope.

### 9. `connect` surfaces a backend refusal verbatim

- **Goal:** a disabled producer (or other backend rejection) is fail-loud.
- **Steps:** disable the producer (`cinna agent-api enable "<Producer>"
  --disable`), then attempt to connect a consumer.
- **Expected:** the command exits non-zero and prints the backend's message
  (e.g. `The producer agent's REST API is disabled`) — not a generic error.
- **Watch for:** the 4xx detail being swallowed; a partial wire (credential
  created but unusable).

### 10. `cinna api` mirrors an inner 2xx and an inner error

- **Goal:** the escape hatch is a faithful pass-through to the platform API.
- **Steps:**
  ```
  cinna api GET agents
  cinna api GET agents --query limit=5
  cinna api GET agents/<bogus-id>; echo "exit=$?"
  ```
- **Expected:** the listing pretty-prints (indented JSON) and exits 0; the bogus
  id prints the inner error body on stdout, `HTTP 404` on stderr, and
  `exit=1`.
- **Watch for:** an inner 4xx being mislabeled as a policy denial (it must
  **not** carry the `blocked by platform policy` prefix — that is reserved for
  the hatch's own 400/403 refusals).

### 11. `cinna api` distinguishes its own refusal (exit 2)

- **Goal:** "the platform said no" is separable from "the route errored".
- **Steps:**
  ```
  cinna api GET credentials; echo "exit=$?"      # an excluded category
  ```
- **Expected:** stderr shows `blocked by platform policy: …`, and `exit=2` (not
  1). A rate-limited call likewise exits 2 and prints `Retry after <n>s` when the
  backend returns `Retry-After`.
- **Watch for:** an excluded-route denial exiting 1 (indistinguishable from an
  inner error); the policy prefix leaking onto an inner 403.

### 12. Don't confuse the two "call" surfaces

- **Goal:** verify the operator reaches for the right tool.
- **Steps:** use `cinna agent-api call <producer> <path>` to hit the producer's
  REST API, and `cinna api GET agents` to hit the platform's control plane.
- **Expected:** `agent-api call` exercises the **producer's** endpoint (owner
  preview); `cinna api` exercises the **platform's** API. They are different
  targets with the same 0/1 exit contract (only `cinna api` adds exit 2).
- **Watch for:** trying to test a producer endpoint via `cinna api` (wrong
  target — it calls platform routes, not the producer's served API).

## Cross-cutting invariants (must hold across all scenarios)

- **Resolve-before-mutate.** A bad agent ref fails before any enable / refresh /
  spec / call / connect request — a typo never toggles or wires anything.
- **No secret leak.** `connect` never prints the producer token in full (prefix
  only); the minted credential is account-side + synced, never pasted by the
  user.
- **Harvest errors are reported, not thrown.** `refresh` always returns a status;
  author mistakes appear as `Last error`.
- **Faithful exit codes.** Inner 2xx → 0, inner 4xx/5xx → 1 (body printed) for
  both `agent-api call` and `cinna api`; only `cinna api` adds exit 2 for the
  hatch's own refusal (policy / rate limit / size cap), distinguished by the
  marker header — never conflated with an inner 4xx.
- **Spec is pipeable.** `cinna agent-api spec` emits plain JSON to stdout (or the
  `-o` file), never Rich-decorated.

## Cleanup

- Disconnect any test wire from the consumer (delete the minted credential in
  the UI, or `cinna api DELETE` the credential if your account policy allows it)
  so a stale producer token isn't left attached.
- Disable a producer API you enabled only for the test:
  `cinna agent-api enable "<Producer>" --disable`.
- Remove any deliberately-broken `agent_api/` edits (revert + sync), then
  `cinna agent-api refresh` to restore a clean harvested spec.
- Delete scratch files written by `cinna agent-api spec -o spec.json`.
