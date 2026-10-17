# Artifacts — Acceptance Scenarios

## Preconditions

- A live cinna-core with artifacts (`curl -s $PLATFORM/.well-known/cinna-desktop`
  shows `"artifacts": 1`) and the frontend reachable for the printed links.
- An account workspace (`cinna account setup …`) whose token is valid
  (`cinna account status`).
- A second user on the same instance (for `--user` and `--shared`).
- The editable install: `which cinna` resolves to this repo's `src/cinna`
  (or `uv run --project <repo> cinna`).
- A real terminal for scenarios 6–8 — or `script -q /dev/null cinna …` to give
  the command a pseudo-terminal.
- A test folder:

```bash
mkdir -p /tmp/art/report/.git /tmp/art/report/sub && cd /tmp/art/report
echo '<h1>Q3</h1><script src="app.js"></script>' > index.html
echo 'console.log(1)' > app.js
printf 'a,b\n1,2\n' > sub/data.csv
echo 'SECRET=1' > .env
echo '[core]' > .git/config
ln -sf /etc/hosts hosts-link
```

## Scenario catalog

### 1. Publish prints a link that opens in core
- **Goal:** a folder becomes a private artifact with a page.
- **Setup:** no `.cinna/artifacts.json` entry for `/tmp/art/report`.
- **Steps:**
  ```bash
  cinna artifacts publish /tmp/art/report --title "Q3 churn"
  ```
- **Expected:** "Publishing … (4 files …)", "Left out 3 paths (never
  published): .git/, .env, hosts-link", "Published Q3 churn — site, revision 1,
  only you can see it", the `/artifact/<id>` link and the id. The link opens
  the page; the site renders.
- **Watch for:** a root-level `.git/` reaching core (it was once listed under
  "The server also left out", meaning it was uploaded).

### 2. Excluded files are absent from core's revision
- **Goal:** secrets never leave the machine.
- **Steps:**
  ```bash
  cinna artifacts download <id> -o /tmp/art/dl && find /tmp/art/dl -type f
  ```
- **Expected:** `index.html`, `app.js`, `sub/data.csv` only — no `.env`,
  `.git/config`, `hosts-link`.

### 3. Republish without an id, then `--new`
- **Goal:** refreshing keeps the link (A12).
- **Steps:**
  ```bash
  echo 'console.log(2)' > /tmp/art/report/app.js
  cinna artifacts publish /tmp/art/report
  cinna artifacts publish /tmp/art/report --new
  cinna artifacts list
  ```
- **Expected:** the second command says "Republished … as revision 2 — same
  link (remembered …)" with the same id; the third creates a second artifact;
  `list` shows both. A further plain publish republishes the newer one.
- **Watch for:** a republish that changes the title when `--title` was not
  passed; a duplicate artifact instead of revision 2.

### 4. `--json` emits NDJSON ending in the result line
- **Steps:**
  ```bash
  cinna artifacts publish /tmp/art/report --json
  ```
- **Expected:** `{"status": …}` progress lines, then one
  `{"result": "ok", "artifact_id", "action", "revision", "url", "excluded", …}`
  line, nothing else on stdout.

### 5. A deleted remembered artifact
- **Steps:**
  ```bash
  cinna artifacts delete <id> --json --yes
  cinna artifacts publish /tmp/art/report
  ```
- **Expected:** if the deleted id was the remembered one, publish creates a new
  artifact (revision 1). With `--update <deleted id>` it fails with the
  server's 404 instead.

### 6. Share and link refuse without a person
- **Steps:**
  ```bash
  cinna artifacts share <id> --instance --json
  cinna artifacts share <id> --instance --no-input
  echo | cinna artifacts link create <id>
  ```
- **Expected:** each exits 1 with code `needs_person` and no change on the
  artifact page.

### 7. Share and link work in a terminal
- **Steps (in a terminal):**
  ```bash
  cinna artifacts share <id> --instance --user second-user@example.com
  cinna artifacts link create <id> --code --label "for bob" --rev 1
  curl -s $PLATFORM/api/v1/artifact-links/<token>/info
  cinna artifacts show <id>
  cinna artifacts link revoke <id> <link id or prefix> --yes
  ```
- **Expected:** share prints "Everyone on the instance + 1 person"; the second
  user sees it under `list --shared`. Link create prints the `/s/<token>` link,
  the code and the expiry once; `info` reports `needs_code: true`. `show` lists
  the link as `<id>  <prefix>… for bob — works until … [code, pinned to rev 1]`.
  After revoke, `info` answers "Link not found".

### 8. Claude Code cannot run the share verbs
- **Setup:** `cinna account refresh-context`; `.claude/settings.json` holds the
  two `deny` rules.
- **Steps:** from the account root
  ```bash
  claude -p "Run with the Bash tool: cinna artifacts share <id> --private, then cinna artifacts link create <id>. Quote any error."
  ```
- **Expected:** both are refused with "Permission to use Bash with command … has
  been denied".

### 9. Older core
- **Setup:** a core without the artifacts routes (or temporarily point the
  workspace at one).
- **Steps:** `cinna artifacts list --json`
- **Expected:** `{"result": "error", "code": "artifacts_unsupported", …}`.

## Cross-cutting invariants

- Nothing in the A10 list, nor `.cinna/`, is ever in an uploaded zip.
- No file is written into the published folder; the path memory lives in the
  account's `.cinna/`.
- The link token appears only in `link create`'s output.
- Under `--json` stdout carries JSON lines only, ending in one `result` line.
- No person-only verb succeeds under `--json`, `--no-input` or without a TTY.

## Cleanup

```bash
cinna artifacts list --json | jq -r '.artifacts[].id' | xargs -n1 -I{} cinna artifacts delete {} --json --yes
rm -rf /tmp/art
```
