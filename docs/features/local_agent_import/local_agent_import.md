# Local Agent Import (`cinna agent import`)

## Purpose

Take an agent that was built **entirely offline** — with any coding assistant,
by a user who did not yet have a Cinna account — and put it on the platform in
one verb: the cloud agent is created, its prompts and metadata are written, a
local workspace is attached, the files are copied and pushed, credential drafts
and schedules are created, and the local manifest is stamped with the cloud
link.

This is the "go-cloud" step of the **Local Agent Kit**: the platform serves a
public, unauthenticated kit (`GET /agent-start`) that teaches an assistant how to
scaffold `~/Documents/MyAgents/Local/<slug>/` — a folder whose layout is
byte-compatible with a cloud agent workspace and whose `cinna-agent.json`
manifest carries the definitional metadata a bundle revision carries. `cinna
agent import` is the only CLI verb that reads that manifest.

The kit's machine-readable half is a **versioned contract** (`layout.json`,
`CONTRACT_VERSION`, the JSON schemas), served separately at
`GET /api/agent-start/contract.tar.gz` with a cheap version poll at
`GET /api/agent-start/contract/version`. cinna-cli reads the copy installed at
`.cinna-kit/`; a shipped build should obtain it from those routes rather than
vendoring one.

Nothing platform-side is new: the import replays the manifest through the
existing account-scoped verbs (`agent create`, the bulk prompt write, `agent
sync`, `sync push`, `account credentials create`, `agent schedule create`,
`agent status set-command`).

## Mental model

- **The manifest is the plan.** `cinna-agent.json` (at the agent folder root)
  says what the agent *is*: name, slug, description, example prompts, router
  trigger, which files hold the three prompts, the status refresh command, the
  credentials it needs, and its schedules. The import writes exactly that and
  invents nothing.
- **Two folders, one direction.** `Local/<slug>/` is where the agent was built;
  `Cloud/` is the account workspace (`cinna login <host> --dir Cloud`). Import
  runs **from `Cloud/`** and pushes `Local/<slug>/` into
  `Cloud/agents/<slug>/workspace/`. After a successful import the cloud copy is
  the live one — keep iterating there with `cinna dev`, or keep experimenting
  locally and re-import with `--update`.
- **The contract is the authority.** The folder rules — what may be copied to
  the cloud, what can hold a credential value — are **data**, not prose:
  `.cinna-kit/layout.json`, published by cinna-core and read by three hosts
  (Cinna Desktop, a coding assistant's kit tools, and this CLI). Import reads
  them from that file rather than re-deriving them, so a correction cinna-core
  publishes reaches this tool on the next kit refresh instead of waiting for a
  new cinna-cli. When the contract cannot be found the run falls back to a
  built-in copy and says so as a **degradation**, not as a mode.
- **Secrets never travel.** `credentials/` (which holds the local `.env`) and
  `app-data/` (runtime state) are never copied — not even if the contract is
  edited to allow them. On top of the exclude list the contract carries rules a
  glob list cannot express: every dotenv shape (`.env`, `.env.prod`,
  `staging.env`) is withheld wherever it sits, while `.env.example`,
  `.env.sample` and `.env.template` still travel. Credentials are created as
  **empty drafts** and the command prints the URLs the user opens to fill each
  one in the browser. No secret value is read, sent, or printed at any point.
- **The ledger is a sibling file, never a manifest key.** Where an agent folder
  has been published is recorded in `publications.json` beside
  `cinna-agent.json` — one entry per Cinna instance. It has to be a separate
  file: each entry records a hash *of the exported tree*, the manifest is a
  member of that tree, so a hash written into the manifest could never match the
  tree it describes. The folder would read "1 unpublished change" the instant a
  publish succeeded, and republishing to clear it would create the next
  mismatch.
- **Every step is idempotent.** The agent is matched by the ledger entry for
  this instance, credentials by name, schedules by name. A run that dies halfway
  is resumed with `--update`; nothing is duplicated.
- **The record is the commit point.** `publications.json` is written **only
  after the workspace push settled**. If the push failed, was skipped
  (`--no-push`), or left conflicts, nothing is recorded and the re-run is a
  plain `--update`.

## The nine steps

Each prints as `[n/9]`:

1. **Manifest** — load and validate `cinna-agent.json`; refuse a
   `schema_version` newer than this CLI understands, a slug that disagrees with
   the folder name, a malformed cron, a credential without a type. Then the
   contract: refuse a folder built against a newer **major** contract version,
   warn on an older one or on a folder that records none, resolve the exclude
   list and the secret rules, plan the copy and compute the tree's
   `content_hash`.
2. **Agent** — resolve by the `publications.json` entry for this instance
   (requires `--update`) or create a new agent in the active user workspace
   (`--workspace` overrides).
3. **Prompts + metadata** — one bulk write carrying `description`,
   `router_trigger_prompt`, `example_prompts`, and the three document prompts
   read from the files named in `prompts`; then the status refresh command.
4. **Workspace** — reuse the synced workspace under `agents/<slug>/` if the
   agent already has one, otherwise run `cinna agent sync`.
5. **Copy** — copy the tree into `agents/<slug>/workspace/`, honouring the
   contract's exclude list and secret rules, and generate
   `workspace_requirements.txt` from `pyproject.toml` when the agent does not
   ship one.
6. **Push** — `cinna sync push` equivalent (skipped by `--no-push`).
7. **Credentials** — one empty draft per spec, attached to the agent; an
   existing credential of the same name is attached rather than recreated.
   Setup URLs are collected for the summary.
8. **Schedules** — one per spec, name-idempotent; `--update` rewrites an
   existing schedule in place.
9. **Record** — add or update this instance's entry in `publications.json`
   (`platform_url`, `agent_id`, `workspace`, `imported_at`, `updated_at`,
   `contract_version`, `content_hash`) and print the summary (agent id, web UI
   link, workspace path, credential setup URLs, and the `cinna chat`
   verification line).

## User flows

### First import

1. In the kit root: `cinna login <platform> --dir Cloud` (once).
2. `python3 .cinna-kit/tools/kit.py validate Local/<slug>` — must pass.
3. `cd Cloud && cinna agent import ../Local/<slug>`.
4. Confirm the plan (or pass `--yes`), then open the printed credential setup
   URLs and fill the secrets in the browser.
5. Verify: `cinna chat --agent <slug> "<first example prompt>"`.

### Look before you leap

`cinna agent import ../Local/<slug> --dry-run` prints the whole plan — every
file that would be copied, every credential draft, every schedule — and makes
**zero** platform calls and zero local writes.

### Re-import after more local work

`cinna agent import ../Local/<slug> --update` — resolves the agent by the
`publications.json` entry whose `platform_url` matches the instance you are
logged into (not by workspace, and not by position in the list), rewrites
prompts and metadata, re-copies the tree, pushes, attaches existing credentials,
and updates the schedules in place.

### Resume a partial import

If the run died before the record was written, `--update` reattaches by a unique
name match (and refuses when several agents share the name — add the entry to
`publications.json` manually to disambiguate). Credentials and schedules already
created are reused, not duplicated.

## Options

| Flag | Effect |
|------|--------|
| `--name TEXT` | Override the manifest's display name for the *created* agent |
| `--workspace REF` | Target user workspace (name or id; `default` for the Default one) for the agent and its credential drafts |
| `--update` | Re-import into the agent the manifest points at (required for any second run) |
| `--dry-run` | Print the plan; no platform call, no local write |
| `--no-push` | Copy the files but skip the sync push (leaves the manifest unstamped) |
| `--yes` / `-y` | Skip the confirmation prompt |

## Failure modes and what they mean

| Situation | Behaviour |
|-----------|-----------|
| Not in an account workspace | The standard "not in a cinna account workspace" error — run `cinna login <host> --dir Cloud` first |
| `cinna-agent.json` missing | Refused, with the hint that the path must be the agent folder |
| `schema_version` newer than supported | Refused with an upgrade hint — never half-read |
| Folder built against a newer **major** contract version | Refused with an upgrade hint — a minor or patch difference is not a compatibility question and passes silently |
| Folder records no `contract_version` | Warned and imported — every folder created before contract 1.0.0 is in that state |
| Already imported to this instance, no `--update` | Refused, naming the agent id and where the link was recorded |
| The recorded agent id is not among your agents | Refused — you are logged into a different platform, or the agent was deleted |
| `--update` with several name matches | Refused, listing them; add the `publications.json` entry to disambiguate |
| Push failed or left conflicts | Warned, **nothing recorded**, with the resolve + `--update` hint |
| `.cinna-kit/layout.json` missing or unreadable | Imported with the built-in contract copy, the `Exclusions:` line marked `DEGRADED`, and **no `content_hash` recorded** — a hash over a file set another host would not select is worse than none |
| A travelling file or directory cannot be read | Refused — a complete-looking export with a subtree missing from it is the one outcome worth stopping for |
| `publications.json` unreadable | Refused, and the file is left exactly as it was |
| Credential/schedule 403 | The platform error surfaces verbatim; earlier steps stay done and `--update` resumes |
| A file under `credentials/` or `app-data/`, or any dotenv shape, reaches the copy plan | Hard refusal — the exclude list or the secret rules are broken, and that is a bug to report, not to work around |

## Related

- [Agent Management](../agent_management/agent_management.md) — `agent create`,
  `agent sync`, `agent show`, `agent status set-command`.
- [Agent Schedules](../agent_schedules/agent_schedules.md) — the schedule verbs
  the import replays.
- [Account Workspace](../account_workspace/account_workspace.md) — the `Cloud/`
  workspace, credential drafts, and the bulk prompt write.
- [Live Sync](../live_sync/live_sync.md) — what `sync push` actually does.
