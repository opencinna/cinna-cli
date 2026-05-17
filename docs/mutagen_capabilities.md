# Mutagen Capabilities — Verified Findings

> A reference of every Mutagen behavior cinna-cli currently depends on, with the exact commands to re-verify each one. The goal: when we bump Mutagen, run through this doc top to bottom and confirm nothing regressed.

**Currently pinned version:** Mutagen `0.18.1` (verified May 2026 against `mutagen version` on macOS arm64).

All claims below were reproduced locally against that version. The reproduction commands assume the `mutagen` daemon is running (`mutagen daemon start`). They use temp dirs `/tmp/c-a` and `/tmp/c-b` as alpha/beta — clean them up between tests.

---

## 1. `mutagen sync list --template '{{json .}}'` output shape

**What we rely on:** the JSON state is a top-level **array of session objects** (even when listing a single session by name). Each object flattens both the session config (alpha, beta endpoints, ignore rules) and the runtime state (status, conflicts, stagingProgress) at the top level — `state` is **not** a nested object.

**Reproduce:**
```bash
mkdir -p /tmp/c-a /tmp/c-b && echo hi > /tmp/c-a/x.txt
mutagen sync create --name=cap-test /tmp/c-a /tmp/c-b
sleep 2
mutagen sync list --template '{{json .}}' cap-test
mutagen sync terminate cap-test
```

**Expected top-level keys (per session):**
- `identifier`, `version`, `creationTime`, `creatingVersion`, `name`
- `alpha`, `beta` — each contains `protocol`, `path`, `connected`, `scanned`, `directories`, `files`, `totalFileSize`, and during a transfer `stagingProgress`
- `status` — string, see §3
- `successfulCycles` — int counter (only emitted after first non-zero cycle)
- `paused` — bool
- `conflicts` — array, see §6
- `lastError` — string (optional)

**Where we read it:** `src/cinna/sync_session.py:_list_sessions`, `_to_status`; `src/cinna/sync_tui.py:_parse_monitor_payload`.

---

## 2. `mutagen sync monitor` streams state on every change

**What we rely on:** `mutagen sync monitor --template '{{json .}}{{"\n"}}' <session>` is a long-running subprocess that writes one JSON-array record to stdout every time the daemon's session state changes — including progress ticks while a transfer is in flight. This is what lets the Sync tab show per-file paths in real time; polling at any practical interval misses fast files.

**Reproduce:**
```bash
mkdir -p /tmp/c-a /tmp/c-b
for i in $(seq 1 60); do dd if=/dev/urandom of=/tmp/c-a/file$i.bin bs=1M count=8 2>/dev/null; done
mutagen sync create --name=cap-monitor /tmp/c-a /tmp/c-b
( mutagen sync monitor --template '{{json .}}{{"\n"}}' cap-monitor & MPID=$!
  sleep 12; kill $MPID 2>/dev/null
) | head -20
mutagen sync terminate cap-monitor
rm -rf /tmp/c-a /tmp/c-b
```

**Expected:** multiple `[{...}]\n\n` payloads. During staging, you'll see `stagingProgress.path` change between records.

**Where we read it:** `src/cinna/sync_tui.py:_monitor_loop`.

**Known limit:** during very fast local-to-local syncs (60 files transferring in ~1 second on an SSD) the daemon coalesces updates and we observed roughly 1 distinct path emitted per 5–10 files. For cinna's actual use case (remote sync over network) each file takes long enough that monitor emits multiple progress records per file and we catch them all.

---

## 3. Side-suffixed `status` values

**What we rely on:** `status` can take side-suffixed values `staging-alpha`, `staging-beta`, `transitioning-alpha`, `transitioning-beta`. These mean "currently writing to that side" — alpha = local, beta = remote. The base prefix (`staging`/`transitioning`/`scanning`/`watching`/`reconciling`/`saving`) is the phase.

**Reproduce:** see §2 — during the transfer, watch the `status` field flip between `scanning` → `staging-beta` → `scanning` → `watching`.

**Why we normalize:** `_to_status` and `_state_pill` both call `base_status(raw)` (in `sync_session.py`) which strips the `-<side>` suffix before matching against the set of healthy states. The side suffix drives the "local→remote" / "remote→local" direction label shown in the Sync tab.

---

## 4. Per-side `stagingProgress`

**What we rely on:** when a side is staging, that side's object carries a `stagingProgress` block with these fields:
```json
{
  "path": "file36.bin",
  "receivedSize": 1638400,
  "expectedSize": 3145728,
  "receivedFiles": 17,
  "expectedFiles": 60,
  "totalReceivedSize": 53477376
}
```

`path` is the file currently being received on that side. `expectedSize` is the *current file's* total, not the session's. `totalReceivedSize` is the cumulative bytes since staging started.

**Reproduce:** §2 — watch any single record where `status` is `staging-beta`. `stagingProgress` will appear under `beta`.

**Where we use it:** `src/cinna/sync_tui.py:_emit_staging_events` (logs each new `path` once, deduped against the previous record); `_render_sync_tab` appends the live progress hint to the stats line.

---

## 5. `mutagen.yml` and ignore semantics

**What we rely on:** the per-workspace `mutagen.yml` written by `cinna sync` configures sync mode, scan mode, and ignore patterns. Mutagen reads it from the workspace root at session creation.

**Currently written** (`src/cinna/sync_session.py:MUTAGEN_YML_TEMPLATE`):
```yaml
sync:
  defaults:
    mode: two-way-safe
    permissions:
      mode: portable
    ignore:
      vcs: true
      paths:
        - __pycache__/
        - node_modules/
        - .venv/
        - .cinna/
        - .mypy_cache/
        - .pytest_cache/
        - .DS_Store
    scan:
      mode: full
```

**Available sync modes** (`mutagen sync create --help`):
- `two-way-safe` (cinna default) — both sides authoritative, conflicts halt
- `two-way-resolved` — alpha wins on conflict (no manual intervention)
- `one-way-safe` — alpha authoritative, beta cannot diverge
- `one-way-replica` — alpha mirrored to beta exactly, beta overwritten

**Available scan modes**: `full` (cinna default) and `accelerated`. `accelerated` uses filesystem watches and short-circuits unchanged directories; `full` is slower but more accurate for cases where watches misbehave (network filesystems, container bind mounts).

---

## 6. Conflict JSON shape

**What we rely on:** when both sides modify the same file under `two-way-safe`, mutagen records a conflict in the session's `conflicts[]` array.

**Shape:**
```json
{
  "root": "shared.txt",
  "alphaChanges": [
    {"path": "shared.txt",
     "old": {"kind": "file", "digest": "c9e870..."},
     "new": {"kind": "file", "digest": "534ec1..."}}
  ],
  "betaChanges": [
    {"path": "shared.txt",
     "old": {"kind": "file", "digest": "c9e870..."},
     "new": {"kind": "file", "digest": "e52d59..."}}
  ]
}
```

Some conflict kinds (directory/file disagreement, asymmetric delete) only populate `root`; the per-side change arrays may be empty.

**Reproduce:**
```bash
mkdir -p /tmp/c-a /tmp/c-b && echo "orig" > /tmp/c-a/shared.txt
mutagen sync create --name=cap-conflict --sync-mode=two-way-safe /tmp/c-a /tmp/c-b
sleep 2
mutagen sync pause cap-conflict
echo "LOCAL"  > /tmp/c-a/shared.txt
echo "REMOTE" > /tmp/c-b/shared.txt
mutagen sync resume cap-conflict
sleep 2
mutagen sync list --template '{{json .}}' cap-conflict
mutagen sync terminate cap-conflict
rm -rf /tmp/c-a /tmp/c-b
```

**Where we read it:** `src/cinna/sync_tui.py:_extract_conflicts`.

---

## 7. ⚠ `two-way-safe` does NOT write `.conflict.<side>.<ts>` files

**What we rely on:** in mutagen 0.18.1's `two-way-safe` mode, when a conflict occurs, mutagen records it in `conflicts[]` JSON but **leaves both sides' canonical files untouched**. No `.conflict.alpha.<ts>` or `.conflict.beta.<ts>` files are written.

This contradicts older docs / forum posts about mutagen that describe conflict-marker files. We verified empirically that it does not happen in 0.18.1 two-way-safe.

**Implication:** populating the Conflicts tab from a disk walk (`*.conflict.*`) returns an empty list even when conflicts exist. The tab must source from JSON, not the filesystem.

**Reproduce:** §6, then check `ls -la /tmp/c-a /tmp/c-b` after the conflict appears — only `shared.txt` is present on each side.

**If a future mutagen starts writing those files again:** `src/cinna/sync_session.py:list_conflicts` already walks for `*.conflict.<side>.<ts>` and is the path to surface them. The `cinna sync conflicts` CLI subcommand still uses it. The TUI currently does not — it sources conflicts from JSON via `_extract_conflicts`.

---

## 8. Per-file conflict resolution via delete + `mutagen sync reset`

**What we rely on:** `mutagen sync reset <session>` clears the session's sync history (the common-ancestor record) without recreating the session. On the next scan, mutagen has no notion of what changed since when.

By itself this does nothing useful for conflicts — both sides still have divergent content, so mutagen re-detects the conflict. But combined with deleting the *losing* side's file first, it forces propagation:

| User picks | Action                                              | Effect                                                                                  |
|------------|-----------------------------------------------------|------------------------------------------------------------------------------------------|
| REMOTE     | `rm <alpha file>` then `mutagen sync reset <s>`     | Alpha empty, beta has content, no ancestor → mutagen propagates beta → alpha.            |
| LOCAL      | `rm <beta file>` then `mutagen sync reset <s>`      | Beta empty, alpha has content, no ancestor → mutagen propagates alpha → beta.            |

**Reproduce — Take REMOTE:**
```bash
# Set up conflict as in §6, then:
rm /tmp/c-a/shared.txt
mutagen sync reset cap-conflict
sleep 3
cat /tmp/c-a/shared.txt /tmp/c-b/shared.txt   # both = REMOTE
mutagen sync list --template '{{json .}}' cap-conflict | python3 -c 'import json,sys; print(len(json.load(sys.stdin)[0].get("conflicts",[])))'
# → 0
```

**Reproduce — Take LOCAL:** symmetric — `rm /tmp/c-b/shared.txt` instead.

**Multi-conflict safety**: deleting one conflict file + reset only converges that one path. Other conflicts in the same session remain because both their sides still hold content. Verified by setting up two conflicts (`x.txt` and `y.txt`), resolving one with delete+reset, and observing the other untouched in the post-reset JSON.

**Why we don't switch sync mode:** `two-way-resolved` would auto-resolve in alpha's favor, but its effect is session-wide — every conflict would resolve the same way. There's no per-file mode flag. The delete + reset approach is per-file.

**Where we use it:** `src/cinna/sync_tui.py:_resolve_selected`. For the beta-side delete cinna shells out to `cinna exec rm -f -- <path>` since beta lives on the remote agent.

---

## 9. The SSH-shim contract

**What we rely on:** mutagen invokes its SSH transport via the binary named `ssh` found in `$MUTAGEN_SSH_PATH`. `MUTAGEN_SSH_PATH` is a **directory** (not a binary path); mutagen searches it for an executable literally named `ssh`. The daemon captures its environment at startup, so any change to `MUTAGEN_SSH_PATH` requires restarting `mutagen daemon`.

The remote URL must use OpenSSH-style `user@host:/path`, not `ssh://`. Mutagen's URL parser distinguishes the two and treats `ssh://` as a different transport.

**Where we use it:** `src/cinna/sync_session.py:_ensure_ssh_shim_dir`, `_mutagen_env`, `_restart_daemon`, and the failure marker constants `_STALE_DAEMON_MARKERS` that trigger an automatic daemon restart on `sync create`.

---

## 10. `mutagen daemon` is shared across sessions

**What we rely on:** a single `mutagen daemon` instance manages all sessions on the host, regardless of which user / project created them. `mutagen daemon stop` terminates ALL sessions across all projects; they auto-resume on the next `mutagen sync list` (or any subcommand that talks to the daemon).

**Implication:** when cinna restarts the daemon to refresh stale env (`_restart_daemon`), other consumers of mutagen on the same machine experience a brief pause in their syncs. The pause is silent — they don't get a notification.

**Reproduce:**
```bash
mutagen sync list           # note any non-cinna sessions
mutagen daemon stop
mutagen daemon start
mutagen sync list           # same sessions reappear, all paused for a moment
```

---

## When to revisit this doc

- Bumping `mutagen` past `0.18.1` (any minor or major version).
- Adding a UI feature that needs a Mutagen behavior we haven't documented.
- After any user-reported sync bug that turns out to be a Mutagen-version-specific behavior change.

Walking through §1–§8 with the reproduction commands takes ~5 minutes and catches the breaking changes that have historically bit us.
