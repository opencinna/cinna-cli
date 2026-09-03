# Local Agent Import — Technical Reference

Implementation of [local_agent_import.md](local_agent_import.md). One command,
one module: `cinna agent import` is a Click stub in `src/cinna/main.py`
delegating into `src/cinna/local_import.py`.

## File locations

- `src/cinna/main.py` — `agent_import()`, registered on the `agent` group next
  to `create` / `sync` / `show`. Thin shim; imports the module lazily.
- `src/cinna/local_import.py` — the command: manifest loading/validation, the
  contract-version gate, the copy planner, the ledger writer and its migration,
  the requirements generator, the nine-step orchestrator, and the summary
  renderer.
- `src/cinna/kit_contract.py` — the Local Agent Kit **contract** as data:
  `layout.json` reading, the exclude-pattern matcher, the secret-file rules, the
  export walk, `content_hash`, the `publications.json` ledger, and the
  contract-version compatibility check. Shared with two other hosts, so its
  algorithms are ported rather than invented.
- `src/cinna/client.py` — `AccountClient.update_agent_config()` (the bulk prompt
  write; the only endpoint this feature added to the client).
- Tests: `tests/test_local_import.py`, `tests/test_kit_contract.py`.

## Command surface

`cinna agent import PATH [--name N] [--workspace REF] [--update] [--dry-run]
[--no-push] [--yes]` → `main.py:agent_import()` →
`local_import.py:run_agent_import()`.

`PATH` is `click.Path(exists=True, file_okay=False)` — the *local agent folder*,
not the kit root. The account root is resolved with the shared
`account.py:find_account_root()`, so the command works from the account
workspace root or any folder inside it.

## Platform calls

Everything rides the account token through `AccountClient`; no new backend
endpoint exists for this feature.

| Step | Call |
|------|------|
| 2 | `list_account_agents()` (resolve) / `create_agent(name, description, user_workspace_id=…)` |
| 3 | `update_agent_config(agent_id, fields)` → `PUT agents/{id}` **through the api-proxy escape hatch**, then `set_status_refresh_command(agent_id, cmd)` |
| 4 | `account.py:run_agent_sync(agent_id, None)` (mints the child token, provisions the workspace) — skipped when `resolve_child_workspace()` already finds one |
| 6 | `sync_session.ensure_session()` + `sync_session.flush()` — the same pair `cinna sync push` uses |
| 7 | `list_credentials(user_workspace_id=…)`, `create_credential(...)`, `share_credential_with_agent(...)` |
| 8 | `list_schedules(agent_id)`, `create_schedule(agent_id, body)` / `update_schedule(agent_id, sid, body)` |
| — | `list_user_workspaces()` only when `--workspace` is given |

### `update_agent_config` — why the escape hatch

There is **no dedicated account route** for writing an agent's prompt set. The
platform's own guidance (`context/guides/authoring-agent-prompts.md`, mirrored in
the account CLI workspace doc) is `cinna api PUT agents/<id> --data
@prompts.json`; `agents/*` is not on the escape-hatch denylist. So
`update_agent_config()` is that exact call made from Python via
`AccountClient._proxy_json("PUT", f"agents/{agent_id}", json_body=fields)` —
which raises a typed `PlatformError`, and distinguishes a hatch refusal from a
mirrored inner-route error. Fields sent (all optional, omitted keys unchanged):
`description`, `router_trigger_prompt`, `example_prompts`, `workflow_prompt`,
`entrypoint_prompt`, `refiner_prompt`.

## Manifest handling

`load_manifest(source)` reads `<source>/cinna-agent.json` and validates the same
pragmatic subset `kit.py validate` checks:

- `schema_version` ≤ `SUPPORTED_SCHEMA_VERSION` (1) — a newer manifest is
  **refused**, never partially read.
- `slug` matches `^[a-z0-9][a-z0-9-]{1,62}$` **and** equals the folder name.
- `name` present, ≤ 255 chars; `description` a string when present.
- `prompts` an object; `example_prompts` a list of strings.
- Each credential has a non-empty `name` and `type`.
- Each schedule has a name, a 5-field `cron_string`, a known `schedule_type`, and
  a `command` when it is a script schedule.

Unknown keys are preserved: the dict is mutated and re-dumped, so a manifest
written by a newer kit survives a round-trip through an older CLI.

`schedule_type` mapping: the manifest's `script` (plan §3.3) and the API's
`script_trigger` are both accepted and normalized to `script_trigger`;
`static_prompt` passes through. `timezone` defaults to `UTC`, `description`
defaults to the schedule name (it is required server-side) — the same defaults
`run_schedule_create()` applies.

## The contract — `src/cinna/kit_contract.py`

The machine-readable half of the Local Agent Kit is a **versioned contract**:
`.cinna-kit/layout.json`, served by cinna-core alongside the kit itself. Three
hosts read it — Cinna Desktop, cinna-core's `kit.py`, and this CLI — so the
folder rules are data rather than prose each host re-derives.
`src/cinna/kit_contract.py` is cinna-cli's reader and the port of the algorithms
that must stay byte-identical across hosts. `SUPPORTED_CONTRACT_VERSION` names
the contract version this build implements.

### Finding it

`kit_contract.py:find_kit_root()` walks **up** from the agent folder for a
`.cinna-kit/` holding either `layout.json` or `kit.json`.
`kit_contract.py:load_layout()` never raises: every value the file supplies has
a built-in fallback here, so an unreadable contract degrades rather than
stopping the command.

### The exclude list

`kit_contract.py:contract_exclude_patterns()` reads `cloud_import_excludes` and
returns a **tristate** — the declared list, or `None` when this build cannot
evaluate it. The two consumers want different things from that answer: what
*travels* falls back to `kit_contract.py:DEFAULT_EXCLUDE`, what is *hashed* may
not fall back at all. The list is read whole or not at all; one unusable entry
rejects it, because a silently shortened exclude list is a narrower secret gate
reported as a healthy one.

`DEFAULT_EXCLUDE` must stay content-identical to the shipped `layout.json` —
same entries, same order — and `tests/test_kit_contract.py` asserts it entry by
entry. A fallback that diverges is a silent hash-parity break.

`local_import.py:load_export_contract()` wraps the resolution and appends
`MANDATORY_EXCLUDE = ("credentials/", "app-data/")` unconditionally: **a
contract cannot widen the import into secrets or runtime state.** Its
`ExportContract.origin` is the user-facing `Exclusions:` line, and it names the
`layout.json` it read or says `DEGRADED —` and why. Naming a *mode* rather than
a *degradation* is what made the previous regression invisible.

### Matching — not `fnmatch`

`kit_contract.py:matches_pattern()` implements the contract's own semantics: a
trailing `/` takes the directory and everything under it; `*` and `?` stay
inside one segment; `**` spans whole segments; and a pattern **without** a
leading `**` is anchored at the agent root and must consume the whole path.
That last rule is why `README.md` drops the agent's own README and never
`docs/README.md` — under `fnmatch` it would take both, and the contract names
that as the outcome anchoring exists to prevent.

`kit_contract.py:_segment_matcher()` matches in UTF-16 code units and uses
`fullmatch`, both for parity with the desktop's JavaScript.

**One deliberate divergence from `kit.py`:** the reference implementation picks
the directory branch from the raw pattern while its normalisation strips only
`/`, so a pattern with a trailing space takes the directory branch with the
space still in its body and matches nothing — a directory silently dropped from
the exclude set. Here both sides of that choice are made on the stripped
pattern, and `kit_contract.py:_clean_patterns()` warns when it strips one.

### The secret gate

Some rules a glob list cannot express — "any `.env.<suffix>` except
`.example`". `layout.json` `secret_files` carries them as data and
`kit_contract.py:is_secret_filename()` evaluates them: a path is secret when any
clause of a rule's `match` hits and no clause of its `unless` does; clauses test
the **basename only, at any depth**; rules OR together.

**The declared fail-safe direction is not ours to choose.** A clause this build
cannot evaluate resolves *toward* treating the path as secret — an unknown
`match` clause counts as a hit, an unknown `unless` clause counts as a miss —
because the opposite direction is a silent credential upload.
`kit_contract.py:secret_file_rules()` therefore returns a declared list **as
declared**, unreadable entries included: filtering them would disable the
fail-safe one function away.

**This and `contract_exclude_patterns()` fail in opposite directions on
purpose.** There, an unevaluable list withholds the `content_hash`, because a
confidently wrong hash is worse than none. Here it withholds the file, because a
leaked credential is unrecoverable. The safe direction is a property of the
consequence, not a house style; an edit that makes them agree silently reverses
one of them.

### The walk

`kit_contract.py:collect_export_tree()` is the one walk, and
`local_import.py:plan_copy()` its policy wrapper. Symlinks are never followed
and never listed; excluded directories are never descended into; and the secret
rules are applied **here** rather than at copy time, so the set that travels and
the set that is hashed are the same one. A path the walk could not read is
recorded and then refused by `plan_copy()` — an unscannable directory read as
empty produces a complete-looking export with a subtree missing from it.

`local_import.py:assert_no_secrets()` re-checks the finished plan against the
same rules — belt and braces against a future edit to the walk.

## `content_hash`

`kit_contract.py:content_hash()` over `kit_contract.py:hash_export_files()`:
one line per file, `<relpath>` NUL `<sha256 hex of the bytes>` LF, fed into one
running SHA-256, result `sha256:<hex>`. No mtimes, sizes, modes or directory
entries.

Two details are load-bearing for cross-host agreement:

- **the sort is UTF-16 code-unit order** (`kit_contract.py:utf16_sort_key()`),
  because the desktop sorts with `Array.prototype.sort` and the order is part of
  the digest. Plain `sorted()` agrees for ASCII and diverges for non-BMP
  characters, and the only symptom is one folder reporting unpublished changes
  forever;
- **unevaluable ⇒ no hash at all**, never one computed a different way.
  `local_import.py:export_content_hash()` returns `None` when the contract's own
  exclude list could not be evaluated (the file set is not the one another host
  would select) or when a travelling file could not be read, and the run prints
  `Content hash: WITHHELD` with the reason. A missing drift number is visible
  and recoverable; a plausible wrong one is neither.

## `publications.json` — the ledger

A **sibling** of `cinna-agent.json` at the agent root, never a key inside it.
Both hosts hash the manifest and neither exclude list contains it, so a
`content_hash` stored in the manifest would be a value stored inside the file it
is a hash of: the first publish computes h0 and writes it in, the manifest bytes
change, the next scan computes h1 ≠ h0, and the folder reads "1 unpublished
change" the instant the publish *succeeds*.

The top level is an **object** with a `publications` array, not a bare array, so
a later contract can add a sibling key without breaking readers.
`kit_contract.py:read_publications()` returns three distinct states — the
entries, `[]` for no ledger, `None` for one this build cannot read — and
conflating the last two is a data-loss bug.
`local_import.py:read_publications_or_refuse()` turns `None` into a loud
refusal that writes nothing.

Per entry cinna-cli writes `platform_url`, `agent_id`, `workspace`,
`imported_at`, `updated_at`, `contract_version`, `content_hash`; only the first
two are required by the schema. `content_hash` is **omitted, and removed from an
existing entry**, when it could not be computed — a stale hash reads "up to
date" forever.

`publications.json` is in `cloud_import_excludes` as the plain string
`publications.json`, root-anchored and deliberately **not** `**/publications.json`:
a nested one under `files/` is a user's file, not ours.

### Migration of the legacy `cloud` block

`cloud` is retained-but-deprecated. `local_import.py:write_manifest()` is the
one writer of `cinna-agent.json` and migrates on every write: `publications[]`
first (the newer shape), then `cloud`.

Migration is **all-or-nothing per key**. A key this build cannot read is left in
place, because discarding data a host cannot interpret is unrecoverable while a
deprecated key is merely visible. A key it cannot *place* is left in place for
the same reason — `kit_contract.py:ledger_entry_is_placeable()` guards it, and
without that guard a `cloud` block with a real `platform_url` and no `agent_id`
would be deleted from the manifest while the ledger declined to record it. A
value's destination has to be reached before the source may be removed. When a
block is left behind, the run says so.

The write happens **before the tree is copied, hashed or pushed** — see below.

`local_import.py:resolve_known_agent_id()` reads the ledger first and the
deprecated `cloud` block second. **The legacy read is deliberately kept**: a
folder imported by an older cinna-cli carries only the `cloud` stamp, and
dropping the read would make its next `--update` create a *second* agent. It is
retired behind the ledger, not removed.

## `--update` resolves by `platform_url`

`local_import.py:resolve_known_agent_id()` matches the entry whose
`platform_url` equals the instance the run is against — **not** on `workspace`,
and **not** on position in the array. The resolution key is part of the contract
because the desktop publishes directly through the account API and records no
`workspace` at all, so an entry with `workspace` absent must still resolve.

## The `contract_version` gate

`kit_contract.py:check_contract_compatibility()` — **only the major version
decides.** A minor or patch difference is not a compatibility question: the
contract's minor releases are additive by definition. That constrains what a
future contract change may safely do — a same-major pair passes, so a
minor-version change reaches a non-adopting reader as *silence*, and anything an
old reader must notice needs a major bump.

`local_import.py:assert_contract_compatible()` refuses `app_too_old` (a folder
from a newer major — this build cannot know what it would drop) and warns on
`migratable` and `unknown`. A folder recording no `contract_version` at all is
`unknown`: every folder created before contract 1.0.0 is in that state, and
refusing them would break the users this command exists for.

`schema_version` is **not** a break, and the question is recorded here so it is
not re-investigated: `load_manifest()` reads `data.get("schema_version", 1)`, so
a manifest that omits the field — which the current kit template does — resolves
to `1` and passes the `> SUPPORTED_SCHEMA_VERSION` gate.

## `workspace_requirements.txt`

Generated only when the agent does not already ship one.
`pyproject_dependencies()` uses `tomllib` on Python ≥ 3.11; the package supports
3.10, where there is no stdlib TOML parser, so it falls back to a regex over the
first `dependencies = [...]` block and the quoted strings inside it. A parse
failure warns and yields no file rather than aborting the import.

## Idempotency and the stamp

- **Agent** — `cloud.agent_id` present → resolve by id in
  `list_account_agents()` (a duplicate display name on the platform can never
  redirect the write); absent + `--update` → unique name match, else create;
  absent + no `--update` → create.
- **Credentials** — `list_credentials()` keyed by name; an existing name is only
  *attached* (`share_credential_with_agent`), never recreated, so re-runs cannot
  produce a second empty draft that shadows the filled one.
- **Schedules** — `list_schedules()` keyed by name; existing + `--update` →
  `update_schedule`, existing without `--update` → left alone with a notice.
- **Manifest settle** — `write_manifest()` runs after the confirmation and
  **before** the copy, the hash and the push, so that what is pushed, what is
  hashed and what sits on disk are the same bytes. The manifest is a member of
  the hashed tree; a write that landed after the hash was computed would leave
  the folder reading "1 unpublished change" the instant the publish succeeded —
  the same defect the sibling-file ruling removed, one step later. A write that
  would produce identical bytes is skipped rather than made, so the hash moves
  only when the folder genuinely changed (a manifest that was not canonically
  serialised moves it exactly once, and the run says `Normalized`).
- **Record** — `record_publication()` writes only `publications.json`, and only
  when `_push_workspace()` returned `True` (session flushed, zero conflicts).
  `publications.json` is in `cloud_import_excludes`, so recording a publication
  cannot move the hash that was just recorded. `--no-push`, a flush exception,
  and remaining conflicts all leave the ledger unwritten and print the recovery
  hint — an unrecorded import is always safe to re-run.

## Dry run

`--dry-run` returns before the first `AccountClient` is constructed and before
any local write: it prints the resolved plan (file list, credential drafts,
schedules, create-vs-update) and stops. The test asserts the patched
`AccountClient` class was never called at all.

## Tests

`tests/test_kit_contract.py` — the contract reader, in isolation:

- the `DEFAULT_EXCLUDE` drift assertion entry by entry, plus an **opt-in
  cross-repo check** (`CINNA_CORE_LAYOUT=<path to a real layout.json> pytest`)
  that asserts the port against cinna-core's own file;
- `matches_pattern` anchoring, `**` spanning, directory branches, and the
  trailing-space case the reference implementation gets wrong;
- `is_secret_filename` over every dotenv shape, both fail-safe directions
  (unknown `match` ⇒ hit, unknown `unless` ⇒ miss), an unopenable rule, and
  `rules=[]` as the one explicit way the gate is off;
- `resolve_export_contract`: `layout.json` as the authority, `kit.json`
  `cloud_import` no longer read, a missing contract reported as `DEGRADED`, one
  unusable entry rejecting the whole list, mandatory patterns surviving;
- the walk: both gates applied, symlinks never followed or listed, an
  unreadable directory recorded, an *excluded* unreadable directory costing
  nothing, and the UTF-16 sort order;
- `check_contract_compatibility` across every status, including the desktop's
  own pinned `('1.0.0', '1.4.2') → ok`.

Two **opt-in cross-repo checks** run only when pointed at a real cinna-core
tree, and are the cheapest way to catch contract drift:

```
CINNA_CORE_LAYOUT=<…>/docs/local_agent_kit/layout.json \
CINNA_CORE_PUBLICATIONS_SCHEMA=<…>/docs/local_agent_kit/schema/publications.schema.json \
  pytest tests/test_kit_contract.py tests/test_local_import.py
```

The first asserts `DEFAULT_EXCLUDE` and `DEFAULT_SECRET_FILE_RULES` against the
shipped `layout.json`; the second validates a ledger this CLI actually wrote
against `publications.schema.json`.

`tests/test_local_import.py` — the command, with a mocked `AccountClient` (the
`tests/test_account.py` convention: `@patch("cinna.local_import.AccountClient")`
+ `mock_client_cls.return_value.__enter__.return_value`) and a patched
`cinna.local_import.sync_session`:

- pure helpers: `is_excluded` over the contract's patterns and over the content
  that must survive them, `plan_copy`, `render_requirements` (with and without
  `pyproject.toml`), and the manifest refusals (schema version, slug/folder
  mismatch, bad cron);
- the secret gate: every dotenv shape refused at the root and at depth,
  `.env.example` / `.sample` / `.template` still travelling, and the withheld
  shapes never reaching the copy plan in the first place;
- the happy path asserting all nine steps, including the exact bulk-write body,
  the ledger entry, and `publications.json` staying out of the workspace;
- the secrets test: no `.env` or `.env.<suffix>` copied, the secret string
  absent from the output *and* from every copied file;
- `content_hash` withheld when the contract cannot be evaluated; a change to a
  *withheld* file not moving the hash while a change to a travelling file does;
  the recorded hash matching a re-scan of the folder;
- the ledger: a legacy `cloud` block migrating and its key being dropped, an
  unplaceable block left in place, an unreadable ledger refusing loudly and
  leaving the file untouched;
- `--update` resolving by `platform_url` rather than position, tolerating an
  absent `workspace`, an entry for another instance not blocking a first
  import, and the deprecated `cloud` read still resolving an old folder;
- the version gate: a newer major refused, a same-major pair passing, a folder
  with no `contract_version` warning and importing;
- `--dry-run` makes no client and writes nothing; a second import without
  `--update` is refused; `--update` duplicates nothing and updates the schedule
  in place; an unknown agent id is refused;
- `run_agent_sync` is called exactly once when no workspace exists yet;
- `--no-push` and push-with-conflicts both leave the ledger unwritten;
- `layout.json` supplying the exclude list while `credentials/` stays excluded;
- `--workspace` / `--name` threading and verbatim `PlatformError` surfacing.
