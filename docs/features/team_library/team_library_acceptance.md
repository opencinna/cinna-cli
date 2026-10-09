# Team Library — Acceptance Scenarios (live e2e)

Real-usage scenarios for `cinna library` against a **live** cinna-core with the
team library (git host + library API). They exist to catch what the unit
suite's stand-in server cannot: core's real `pre-receive` sentences, grant
resolution, token kinds the git endpoint accepts, and multi-person timing.

> Until core ships the library (team-sharing P2), run `pytest
> tests/test_library.py` — the same flows against `git http-backend` behind a
> bearer check.

## Preconditions

- A reachable platform with the team library, and **two users** (A, B) plus a
  third with read-only access (C), each with an account workspace in their own
  workshop: `<workshop>/.cinna-kit/`, `<workshop>/Cloud/<host>/` (account
  workspace), `<workshop>/Local/`.
- A kit agent in A's `Local/` (e.g. `invoice-reader`) with a `credentials/.env`
  and an `app-data/` folder.
- Editable install: `python3 -c "import cinna,os;print(os.path.dirname(cinna.__file__))"`
  points at this repo's `src/cinna`; `git --version` ≥ 2.31.

## Scenario catalog

### 1. First share creates the repository in place

- **Goal:** A shares an agent; nothing secret goes with it.
- **Steps:**
  ```
  cd <A>/Local/invoice-reader
  cinna library share -m "First version"
  git ls-tree -r --name-only HEAD
  cinna library status
  ```
- **Expected:** "Shared Invoice Reader with the team"; the tree has
  `cinna-agent.json` and content, no `credentials/`, `app-data/`,
  `publications.json`, `cinna.log`; status "up to date with the team".
- **Watch for:** `cinna.log` committed; a token in `.git/config`
  (`grep Bearer .git/config` must be empty).

### 2. Secret refused before anything leaves

- **Setup:** a fresh agent with `config/prod.env`.
- **Steps:** `cinna library share -m v1 --json`
- **Expected:** exit 1, `code: secret_in_tree`, `paths: ["config/prod.env"]`;
  `cinna library list` does not show the agent; the folder has no `.git`.
- **Watch for:** an empty repository left on the server.

### 3. Teammate gets it, and gets it only once

- **Steps (B):**
  ```
  cd <B>/Cloud/<host>
  cinna library list
  cinna library get invoice-reader
  cinna library get invoice-reader --json        # folder_exists
  mv ../../Local/invoice-reader ../../Local/renamed
  cinna library get invoice-reader --json        # already_have
  ```
- **Expected:** the listing shows A as maintainer and B's access; the clone
  lands in `Local/invoice-reader/`; the two refusals carry those codes.

### 4. A change flows A → B

- **Steps:** A edits `docs/WORKFLOW_PROMPT.md`, `cinna library share -m "…"`; <!-- nocheck: agent-folder path -->
  B runs `cinna library status` then `cinna library update`.
- **Expected:** B's status "the team changed this agent (1 new)"; after update,
  the file matches A's; status "up to date with the team".

### 5. Two writers, disjoint files — one clean rebase

- **Steps:** A adds `docs/A.md` and shares; B (without updating) adds <!-- nocheck: agent-folder path -->
  `docs/B.md` and runs `cinna library share -m B --json`. <!-- nocheck: agent-folder path -->
- **Expected:** `rebased: true`; the team's head holds both files.
- **Watch for:** more than one retry; a merge commit.

### 6. Two writers, same lines — refused, nothing lost

- **Steps:** A and B both change the same line; A shares; B shares.
- **Expected:** B gets exit 1, `diverged`, with both options printed; B's file
  still holds B's text, `git log -1` is B's commit, no rebase in progress.
  Then option 1 (`git branch my-changes-… && git reset --hard origin/main`)
  leaves B up to date with the copy branch kept.
- **Watch for:** a half-finished rebase; conflict markers in files.

### 7. Update refuses uncommitted edits

- **Steps:** B edits a file without sharing; `cinna library update --json`.
- **Expected:** `uncommitted_changes`; the edit is untouched.

### 8. Read-only: share refused, suggest delivered

- **Steps (C):** `cinna library get invoice-reader`; edit a file and add an
  untracked one; `cinna library share -m x --json`; then
  `cinna library suggest -m "Clearer prompt"`.
- **Expected:** share ⇒ `read_only`; suggest succeeds and A sees a task holding a
  patch with both files; C's folder and index are unchanged.

### 9. Grants

- **Steps (A):**
  ```
  cinna library grants invoice-reader --add everyone=read --add <B-email>=write
  cinna library grants invoice-reader
  cinna library grants invoice-reader --remove everyone
  ```
  Then B tries `--add everyone=maintain`.
- **Expected:** the listing reflects each change; B gets `not_maintainer`.

### 10. Update everything

- **Steps (B):** A shares changes to two agents; B runs `cinna library update --all`.
- **Expected:** one line per library agent; folders without a library remote
  are skipped; exit 0. With one folder dirty, the others still update and the
  exit is 1.

### 11. Import from the library

- **Steps (B):** `cinna agent import --from-library invoice-reader --dry-run`,
  then without `--dry-run` from a workshop where B has no local copy.
- **Expected:** the plan reads the workshop's `.cinna-kit/layout.json` (not
  DEGRADED); a cloud agent is created; the temporary clone is gone afterwards
  and a warning says how to re-import.

## Cross-cutting invariants

- No token in any file, remote URL, argv, `cinna.log` or output.
- No merge, no force-push; every refusal leaves the folder as it was.
- `credentials/`, `app-data/` and secret files never reach the server.
- `--json` refusals carry a stable `code` (the server's when it sent one).

## Cleanup

- A: remove test grants; delete the library agent through core's admin UI or
  `DELETE /api/v1/library/agents/{repo_id}` once no one else has it installed.
- Delete the test folders under each `Local/` and any cloud agent from #11.
