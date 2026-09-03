# cinna-core → cinna-cli: the Local Agent Kit contract, and what cinna-cli must adopt

**From:** the cinna-core team, at the close of the "Local Agent Kit → Cinna Desktop contract" work.
**Status of that work:** complete and landed in cinna-core's working tree, unreleased.
**Status of this document:** a handover. Nothing in it has been implemented in this repo, and
nobody from cinna-core has edited this repo's code.

---

## 0. Why you are getting this, in one paragraph

Cinna Desktop is gaining the ability to create and read the same agent folders that `cinna-cli`
creates and reads. To make three producers agree — the desktop, a coding assistant, and
cinna-core — the machine-readable half of the Local Agent Kit was split out into a **versioned
contract**: a small set of declared data files that any host reads instead of re-deriving the
rules from prose. `cinna-cli` is the third consumer and the only one that has not been updated.

**One consequence is a release gate rather than a nice-to-have, and it is the reason this document
exists at all:** the contract moved the cloud-import exclude list to a new file, and `cinna-cli`
still reads the old location. It does not error. It falls back to a built-in list and prints a
line that reads like a configuration choice. Details in §3, which is the item to do first.

---

## 1. The authority, and where to read it

Everything below is defined by artefacts in the **cinna-core** repo at
`/Users/evgenyl/dev/ml-llm/workflow-runner-core`. Treat that repo as **read-only** — read
anything, write nothing.

| Artefact | What it is |
|---|---|
| `docs/local_agent_kit/layout.json` | **The contract's data file.** Exclude patterns, secret-file rules, folder roles, desktop-owned keys. This is the authority for §3. |
| `docs/local_agent_kit/CONTRACT_VERSION` | The contract version, currently `1.0.0`. Re-read it; do not hardcode what this sentence says. |
| `docs/local_agent_kit/schema/publications.schema.json` | The schema for the new publication ledger. Authority for §4. |
| `docs/local_agent_kit/schema/cinna-agent.schema.json` | The manifest schema. Note what it no longer contains — see §4. |
| `docs/local_agent_kit/tools/kit.py` | The **reference implementation** of every algorithm below. Port from it; do not re-invent. See §7 for the one place not to copy it faithfully. |
| `docs/plans/local_agent_kit_desktop_contract_requirements.md` | The settled decisions D1–D17. §7 of that file is the list this handover expands. |

The contract is also served over HTTP by cinna-core, which is how a shipped `cinna-cli` should
obtain it rather than vendoring a copy:

- `GET /api/agent-start/contract.tar.gz` — the contract tarball, rooted at `cinna-contract/`,
  download filename `cinna-contract.tar.gz`.
- `GET /api/agent-start/contract/version` — the contract version, for a cheap poll.

Both are unauthenticated, on the same `agent-start` surface as the existing
`GET /api/agent-start/kit.tar.gz`. Verify the exact routes against
`backend/app/api/routes/local_agent_kit.py` before coding against them.

---

## 2. How to work through this

The four numbered items in §3–§6 are the work. §7–§9 are things you must **know** in order not to
do the wrong thing — two of them are traps that look like defects and are not, and one is a real
bug in the reference implementation that you would otherwise faithfully copy.

Every claim in this document was verified against both repos at the time of writing. **Verify each
one again before you act on it.** Cinna-core's tree is unreleased and moving; a claim about it is
true as of when it was written, and nothing stamps an expiry on it. Where a count appears below it
carries how it was derived, so you can re-derive it rather than trust it.

---

## 3. PRIORITY — source the exclude list and the secret rules from `layout.json`

### What is broken today

`src/cinna/local_import.py` `load_exclude_patterns()` walks up from the agent folder for
`.cinna-kit/kit.json` and reads:

```python
from_kit = (index.get("cloud_import") or {}).get("exclude")
```

**Decision D6 deleted that key from `kit.json` entirely.** The list now lives in `layout.json`
under the top-level key `cloud_import_excludes`.

The read is guarded by `isinstance(from_kit, list)`, so a missing key is neither an error nor a
warning. `patterns` keeps its initial value and `origin` keeps its initial string, and the run
prints:

```
  Exclusions:  built-in default list
```

**That line is the problem, more than the fallback is.** It announces a mode, not a degradation.
Nobody sees an error. The first symptom is a `.env.prod` sitting in a cloud workspace.

### State the regression accurately, because the obvious framing is wrong

`cinna-cli` did **not** lose entries when D6 landed, and saying so would be both false and easy to
dismiss. We checked: the deleted `kit.json` `cloud_import.exclude` and this repo's
`DEFAULT_EXCLUDE` are **content-identical — the same entries in the same order**
(`credentials/`, `.venv/`, `.claude/`, `AGENTS.md`, `CLAUDE.md`, `app-data/`, `temp/`,
`__pycache__/`, `*.pyc`, `.git/`, `.DS_Store`). Behaviour did not change on the day.

**What D6 removed is the hook.** Before it, a corrected list published by cinna-core reached this
tool on the next `kit.py refresh`. After it, nothing cinna-core publishes can reach this tool
without shipping a new `cinna-cli`. The degradation is measured against the authority that moved,
not against the list that was deleted.

The gap has since widened, and here is the derivation rather than the number: run
`python3 -c "import json;print(len(json.load(open('docs/local_agent_kit/layout.json'))['cloud_import_excludes']))"`
in cinna-core and compare it against `len(DEFAULT_EXCLUDE)` here. At the time of writing that was
41 against 11.

### The half that leaks today, and it is independently fixable

`assert_no_secrets()` refuses a path whose first segment is `credentials` or `app-data`, and one
where `rel.endswith(".env") or Path(rel).name == ".env"`.

A root-level `.env.prod` clears **both** gates:

- it clears every pattern in `DEFAULT_EXCLUDE` — the directory patterns take `_matches`'s
  directory branch, which tests `parts[:-1]` and is empty for a root-level file; `AGENTS.md`,
  `CLAUDE.md`, `*.pyc` and `.DS_Store` miss on both basename and full path;
- it clears both `assert_no_secrets` clauses — it is not under `credentials/` or `app-data/`, it
  does not *end* with `.env`, and its basename is not exactly `.env`.

**So `.env.<suffix>` files — `.env.prod`, `.env.local` — pass both gates and travel, at the agent
root and at any depth, whenever they sit outside `credentials/`.**

Be precise about which shape leaks, because a broader claim is wrong and would send you to fix the
wrong clause. Measured against the real function in this repo:

```
$ .venv/bin/python -c "import sys; sys.path.insert(0,'src')
from cinna.local_import import assert_no_secrets
for p in ['.env.prod','.env.local','staging.env','.env','.env.example','credentials/.env','a/b/.env.prod']:
    try: assert_no_secrets([p]); print(f'{p:20} TRAVELS')
    except Exception: print(f'{p:20} refused')"

.env.prod            TRAVELS      <- the leak
.env.local           TRAVELS      <- the leak
a/b/.env.prod        TRAVELS      <- the leak, at depth
staging.env          refused      <- already covered by rel.endswith(".env")
.env                 refused
credentials/.env     refused
.env.example         TRAVELS      <- correct, and must stay this way
```

**`<name>.env` is NOT part of the gap.** `staging.env` ends with `.env`, so the existing
`rel.endswith(".env")` clause already refuses it — that clause is a working equivalent of the
declared rule's `basename_suffix: [".env"]`. **The one missing clause is
`basename_prefix: [".env."]`,** and `.env.example` travelling is the *correct* behaviour that the
declared rule's `unless` block exists to preserve — do not "fix" it.

*(An earlier cinna-core note listed `staging.env` among the leaking shapes. That was wrong; the
measurement above supersedes it, and cinna-core's record has been corrected.)*

`layout.json` predicts this in as many words, in its own `secret_files.notes`:

> A glob list cannot express this rule, which is why it is here and not in
> `cloud_import_excludes`: enumerating suffixes leaks the first one nobody thought of
> (`.env.prod`, `.env.staging`), and no glob can say 'any `.env.<suffix>` except `.example`'.

### What to build

Two **separable** fixes. Ship the second one first if you want the leak closed today, because it
does not depend on the first.

**(a) Read the authority.** Source both `cloud_import_excludes` and `secret_files` from
`layout.json`, found next to the agent the same way `kit.json` is found now. Keep
`MANDATORY_EXCLUDE` exactly as it is — a contract that drops `credentials/` must still not make
this command copy `credentials/`. When the authority cannot be found, keep falling back, but make
the printed line say it is a degradation rather than name a mode.

**(b) Extend `assert_no_secrets` to the declared rule's full `match` set — in practice, add the
missing `basename_prefix: [".env."]` clause and the `unless` exemption.** The `dotenv` rule in
`layout.json` `secret_files` is:

```json
{
  "id": "dotenv",
  "match": {
    "basename_equals": [".env"],
    "basename_prefix": [".env."],
    "basename_suffix": [".env"]
  },
  "unless": {
    "basename_suffix": [".example", ".sample", ".template"]
  }
}
```

Semantics, from the same block: a path is secret when **any** clause of a rule's `match` hits and
**no** clause of its `unless` does; clauses test the **basename only, at any depth**; rules OR
together.

**The fail-safe direction is declared and is not yours to choose:** a clause a host cannot
evaluate resolves *toward treating the path as secret* — an unknown `match` clause counts as a
hit, an unknown `unless` clause counts as a miss. Implement it that way even where it feels
over-eager; the opposite direction is a silent credential upload.

**One rule that is easy to miss and causes a bug with no visible cause:** a host must apply the
secret rules to **the same file set it hashes** for `content_hash` (§4), not only to the files it
copies. A path withheld from the upload but counted in the hash makes the hash move for a change
that can never be published, which presents to the user as *unpublished changes forever*.

**Acceptance:** `.env.prod` and `.env.local`, at the agent root and at any depth, are refused
(they travel today — that is the fix); `staging.env` and `.env` stay refused (they already are —
guard against a rewrite that loses them); `.env.example`, `.env.sample` and `.env.template` still
travel; and the printed `Exclusions:` line names `layout.json` when it was found.

Note the `unless` clause is evaluated on the **basename**, so `.env.example` must survive while
`.env.prod` must not — a single `startswith(".env.")` without the exemption fails this.

---

## 4. Write `publications.json`, not a `cloud` block

### The shape

A new sibling file at the agent root, beside `cinna-agent.json`:

```
<agent>/
  cinna-agent.json
  publications.json      <- new
```

**It is a sibling file and never a key inside `cinna-agent.json`.** This is D11 as amended, and
the amendment is load-bearing rather than stylistic — writing the ledger into the manifest
recreates a defect that was found and removed. Read `schema/publications.schema.json` for the
authoritative shape; the top level is an **object** with a `publications` array, not a bare array,
so a later contract can add a sibling key without breaking readers.

Per entry, the fields cinna-cli should write: `platform_url`, `agent_id`, `workspace`,
`imported_at`, `updated_at`, `contract_version`, `content_hash`. Only `platform_url` and
`agent_id` are required by the schema.

### Why it is not in the manifest — read this before you propose moving it back

Both hosts hash `cinna-agent.json`; neither exclude list contains it. So a `content_hash` stored
inside the manifest is a value stored **inside the file it is a hash of**. The first publish
computes h0 and writes it in; the manifest bytes change; the next scan computes h1 ≠ h0; the
folder reads "1 unpublished change" the instant the publish *succeeds*. Republishing to clear it
creates the next mismatch. It is unfalsifiable from the user's side: a number that never reaches
zero, with nothing to explain it.

`publications.json` is in `cloud_import_excludes` as the plain string `publications.json` —
**root-anchored only, deliberately not `**/publications.json`**, because a nested
`publications.json` under `files/` is a user's file, not ours.

### `content_hash`

Port `content_hash` from `kit.py` rather than writing your own. The algorithm matters in detail
because two hosts must agree forever:

- symlinks never followed and never listed;
- excluded directories never descended into;
- an unreadable file hashed as the literal string `unreadable`;
- line format `<relpath>\0<hexdigest>\n`; result `sha256:<hex>`;
- **sort with `key=lambda p: p.encode("utf-16-be")`.** This is not a stylistic choice. The desktop
  sorts in UTF-16 code-unit order; a plain Python sort differs for non-BMP characters, and the
  only symptom of the mismatch is "unpublished changes forever" for the affected folder.

**Adopt the fail-safe rule too:** for anything that can affect a hash, **unevaluable ⇒ refuse to
emit `content_hash` at all**, never emit one computed a different way. A missing drift number is
visible and recoverable; a plausible wrong one is neither.

### Migration of the legacy `cloud` block

`cloud` is **retained-but-deprecated**. A tool that writes the manifest for any other reason
migrates `cloud` into `publications.json` and drops the key. Migration happens **at write time,
never at export time** — an export that mutates the manifest is the defect class just removed, and
it breaks byte-parity with a host that uploads verbatim.

Note the current state honestly: in cinna-core, `write_manifest` is the sole writer of
`cinna-agent.json` and its only caller is the scaffold path, so **no re-stamp action exists on any
host today**. A legacy folder keeps its `cloud` block and exports it untouched. That is accepted —
it carries no secret and the platform ignores it. If `cinna agent import` becomes the first real
caller of a migration path, it will be the first code that ever exercises it; treat it accordingly.

---

## 5. `--update` resolves in `publications.json`, by `platform_url`

`cinna agent import --update` resolves the entry **in `publications.json`** whose `platform_url`
matches the instance it is running against, and **tolerates an absent `workspace`** — the desktop
publishes directly through the account API and has no workspace to record.

This answers a question the desktop team asked explicitly, so the resolution key is part of the
contract rather than an implementation detail: match on `platform_url`, not on `workspace`, and
not on position in the array.

---

## 6. Gate the import on `contract_version`

The import should refuse, or warn loudly, when the folder's contract version is one it does not
understand. `check_contract_compatibility()` in `kit.py` is the reference implementation; port its
semantics rather than inventing a comparison.

Worth knowing about the version gate's shape, because it constrains what a future contract change
can safely do: **a same-major pair passes.** The desktop's own test pins
`checkContractCompatibility('1.0.0', '1.4.2')` as `ok`. So a minor-version contract change reaches
a non-adopting reader as *silence*, not as a warning. Anything that must be noticed by an old
reader needs a major bump. The contract is shipping at `1.0.0`, which is why several decisions were
taken now rather than later.

---

## 7. A real bug in the reference implementation — do not port it faithfully

`kit.py`'s `matches_pattern()` chooses its directory branch from the **raw** pattern, which is
whitespace-tolerant, while normalisation strips only `/`. A trailing space therefore survives into
the pattern body, and the pattern matches nothing:

```
$ python3 -c "import kit; print(kit.matches_pattern('temp/',  'temp/x.txt'))"
True
$ python3 -c "import kit; print(kit.matches_pattern('temp/ ', 'temp/x.txt'))"
False
$ python3 -c "import kit; print(repr(kit.normalize_rel_path('temp/ ')))"
'temp/ '
```

cinna-core knows about this and left it unfixed deliberately: no shipped `layout.json` entry has
stray whitespace and a test asserts the list stays clean, so it cannot fire today. **It is called
out here because it is exactly the kind of thing a careful port reproduces.** A hand-edited
contract with a trailing space would silently drop a directory from the exclude set — and move the
`content_hash` — with every individual step looking like it worked.

Either strip whitespace before choosing the branch, or reject a pattern with stray whitespace
outright. Do not silently accept one.

---

## 8. Two things that look like defects and are not

**`schema_version` — benign, verified, do not investigate.** `load_manifest` here refuses a
manifest whose `schema_version` exceeds `SUPPORTED_SCHEMA_VERSION = 1`, and D17 removed
`schema_version` from cinna-core's template manifest. That is **not** a break: the read is
`data.get("schema_version", 1)`, so an absent field resolves to `1` and the gate
`schema_version > SUPPORTED_SCHEMA_VERSION` is false. A new-contract folder passes. This is
recorded at length precisely because *"they gate on a field we deleted"* is the sentence that
would otherwise start an unnecessary investigation.

**The `cloud` block read path — deliberately kept, do not tidy.** `cinna-cli` both writes the
`cloud` block (`stamp_cloud_block`, setting `platform_url` / `agent_id` / `imported_at`) and
**reads** it (`run_agent_import` resolves `known_agent_id` from `cloud_block.get("agent_id")`).
**Clearing it makes the next `--update` create a second agent** instead of updating the existing
one. Deprecated is not the same as unused: keep reading `cloud` until the `publications.json`
migration in §4 is in place and has a write path, then retire the read behind that.

---

## 9. Two open items on `cinna login`, recorded rather than diagnosed

Neither is part of the contract work. They were found while tracing it and are recorded so they
are not lost.

- **`cinna login` in an empty folder makes that folder the workspace.** `_login_new_account` takes
  `--dir` when given, else `cwd` when `_dir_is_empty(cwd)`, else prompts for a subfolder. A user
  who follows *"make the workshop, then log in"* literally, and runs a bare `login` from an empty
  root, gets the flat layout rather than the nested one the guides describe.
- **`cinna login` inside an existing workspace ignores `--dir`.** `run_login` calls
  `find_account_root()` and, on a hit, refreshes in place. **It is not silent** — it emits
  `console.warn("Already inside an account workspace — refreshing it in place (domain / --dir
  ignored).")`, and cinna-core's `guides/11-go-cloud.md` already describes it accurately. The
  correction is noted because an earlier account of this called it silent, and a register entry
  that overstates a tool's quietness gets the whole entry discounted.

There is also one **open question for cinna-core, not for you**: `layout.json`'s
`scaffold_ignore_files.root` has no programmatic reader in `kit.py` — the root pair
(`gitignore` → `.gitignore`) is honoured only by prose. Do not wire a reader for it here on the
assumption it was an oversight; it may be intended.

---

## 10. Non-goals

- **Do not implement the desktop-side work.** Cinna Desktop is a separate consumer with its own
  handover.
- **Do not edit anything in `/Users/evgenyl/dev/ml-llm/workflow-runner-core` or
  `/Users/evgenyl/dev/ml-llm/cinna-desktop`.** Read both freely; write neither.
- **Do not add a `migrate` verb or other user-facing surface for the `cloud` → `publications.json`
  migration** without asking. Cinna-core considered and rejected it: the kit is unreleased, no host
  has a publish path, and the population of folders needing a re-stamp today is approximately the
  development trees. Inventing surface for a user who does not exist is the tail wagging the dog.
- **Do not propose a compatibility shim** that keeps `kit.json` `cloud_import.exclude` alive
  alongside `layout.json`. This was considered during the contract work and deliberately rejected;
  the reasoning is in the requirements document. Two authorities for one list is the state the
  contract exists to end. If you think the rejection was wrong, raise it rather than building
  around it.

---

## 11. What to report back to cinna-core

- Anything in §3–§9 that **did not reproduce as described.** Several claims here are about a
  moving tree in another repo; a claim that fails is information, not an embarrassment.
- Whether the `scaffold_ignore_files.root` question in §9 turns out to matter to a `cinna-cli`
  consumer.
- The point at which `cinna-cli` sources its exclude list from `layout.json`, because **cinna-core
  is holding a release on it.** Until then, nothing cinna-core publishes about excludes or secret
  files can reach this tool.
