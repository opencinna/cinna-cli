# cinna sync TUI — Interface Reference

> Maps every visible element of the live sync TUI (the screen shown by `cinna dev`) to the underlying Mutagen capability it depends on. Use this together with [`mutagen_capabilities.md`](./mutagen_capabilities.md) when bumping Mutagen versions: each feature here points to the section that proves the capability still works.

The TUI is implemented in `src/cinna/sync_tui.py` and rendered by [Textual](https://textual.textualize.io/). It opens when the user runs `cinna dev` and closes on `q` / Ctrl-C; closing the TUI terminates the Mutagen sync session.

---

## Layout

Three tabs, switched with `←` / `→`:

```
┌─────────────────────────────────────────────────────────────────┐
│ cinna sync — <agent name>                              <clock>  │
├─────────────────────────────────────────────────────────────────┤
│ [ Sync ]  [ Details ]  [ Conflicts ]                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│   <tab content>                                                 │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│ q Quit  ◀ Tab  Tab ▶  1 take REMOTE  5 take LOCAL               │
└─────────────────────────────────────────────────────────────────┘
```

---

## Sync tab (default)

The status-and-activity view. Layout:

```
┌─ Status pill, agent identity, endpoints ────────────────────────┐
│ ⬤  Watching for changes                                         │
│ Agent:    my-agent @ https://platform.example.com               │
│ Local:    /Users/me/work/my-agent/workspace                     │
│ Remote:   cinna@cinna-agent-<uuid>:/app/workspace               │
└─────────────────────────────────────────────────────────────────┘
  127 files · 14 dirs · 3.4 MB   Successful cycles: 12
  · receiving 17/60 (53.4 MB so far, current file 3.0 MB)
┌─ Activity log (scrolling) ──────────────────────────────────────┐
│ 19:21:08  sync attached — both endpoints connected              │
│ 19:21:09  status: watching → staging-beta [local→remote]        │
│ 19:21:09    → remote [1/12] scripts/main.py (4.2 KB)            │
│ 19:21:09    → remote [2/12] docs/notes.md (1.1 KB)              │
│ 19:21:10  cycle #3 complete — synced 12 files (12.4 KB)         │
└─────────────────────────────────────────────────────────────────┘
```

### Elements and their Mutagen dependencies

| Element                               | Source                                                            | Mutagen feature                                                                  |
|---------------------------------------|-------------------------------------------------------------------|----------------------------------------------------------------------------------|
| Status pill (`⬤  <text>`)             | `_state_pill` reading `status` + `alpha.connected`, `beta.connected` | [`mutagen_capabilities.md` §1, §3](./mutagen_capabilities.md#3-side-suffixed-status-values) |
| Direction tag (`local→remote`)        | `_side_label` parsing the `-alpha` / `-beta` suffix of `status`   | [§3 side-suffixed status](./mutagen_capabilities.md#3-side-suffixed-status-values) |
| Local / Remote URL lines              | `alpha.path`, `beta.{user,host,path}` from session JSON           | [§1 JSON shape](./mutagen_capabilities.md#1-mutagen-sync-list--template-json--output-shape) |
| Stats line (files / dirs / total size)| `alpha.{files,directories,totalFileSize}`, `successfulCycles`     | [§1 JSON shape](./mutagen_capabilities.md#1-mutagen-sync-list--template-json--output-shape) |
| Live progress (`receiving N/M …`)     | `(alpha|beta).stagingProgress.{receivedFiles,expectedFiles,totalReceivedSize,expectedSize}` | [§4 per-side stagingProgress](./mutagen_capabilities.md#4-per-side-stagingprogress) |
| Per-file activity lines               | `_emit_staging_events` diffing `stagingProgress.path` between consecutive state records | [§4 stagingProgress.path](./mutagen_capabilities.md#4-per-side-stagingprogress) **and** [§2 monitor streams every change](./mutagen_capabilities.md#2-mutagen-sync-monitor-streams-state-on-every-change) — polling alone misses paths |
| Status-transition lines               | `_emit_events` diffing `status` between consecutive records       | [§3 status](./mutagen_capabilities.md#3-side-suffixed-status-values)              |
| Endpoint connect/disconnect lines     | `_emit_events` diffing `alpha.connected` / `beta.connected`       | [§1 JSON shape](./mutagen_capabilities.md#1-mutagen-sync-list--template-json--output-shape) |
| Cycle-complete line with byte delta   | `_emit_cycle_complete` diffing `(alpha|beta).{files,totalFileSize}` against a pre-cycle snapshot | [§1 JSON shape](./mutagen_capabilities.md#1-mutagen-sync-list--template-json--output-shape); `successfulCycles` as edge trigger |
| Scan / transition problem lines       | `_emit_problem_events` reading `(alpha|beta)(Scan|Transition)Problems[]` | [§1 JSON shape](./mutagen_capabilities.md#1-mutagen-sync-list--template-json--output-shape) |
| Conflict log lines (`conflict: …`)    | `_emit_conflict_events` reading `conflicts[]`                     | [§6 conflict JSON shape](./mutagen_capabilities.md#6-conflict-json-shape)        |
| `error: …` line                       | `lastError` field                                                  | [§1 JSON shape](./mutagen_capabilities.md#1-mutagen-sync-list--template-json--output-shape) |
| Activity log file (`cinna.log`)       | Every TUI line is also routed to the `cinna.sync_tui` logger      | Independent of Mutagen — survives TUI close                                       |

### How updates are delivered

A single subprocess streams JSON state into the TUI: `mutagen sync monitor --template '{{json .}}{{"\n"}}' <session>`. The TUI reads `proc.stdout` line by line in `_monitor_loop`. Each parsed record is fed to `_render_sync_tab`, which renders the static pieces and then calls `_emit_events` — the per-file / per-cycle event emitter. Depends on [§2 monitor streaming](./mutagen_capabilities.md#2-mutagen-sync-monitor-streams-state-on-every-change).

---

## Details tab

Verbatim output of `mutagen sync list --long <session>`, refreshed every 2 seconds (`DETAILS_INTERVAL`). This is what a power user would see if they ran the command themselves — full session metadata, scan results, raw conflict listings. Pure mutagen feature, no cinna interpretation.

| Element     | Source                          | Mutagen feature                       |
|-------------|----------------------------------|----------------------------------------|
| Rendered text | `mutagen sync list --long`     | Native human-readable output — stable contract across 0.18.x |

---

## Conflicts tab

```
┌─ Conflicts (2) ──────────────────────────────────────────────────┐
│ ▶ app-data/storage/workflow.db    (local+remote modified)        │
│   docs/WORKFLOW_PROMPT.md          (local+remote modified)       │
│                                                                  │
│ ↑/↓ navigate  ·  1 take REMOTE (server)  ·  5 take LOCAL (yours) │
│ Resolution deletes the losing side's file and resets mutagen     │
│ history so the survivor propagates.                              │
└──────────────────────────────────────────────────────────────────┘
```

### Elements

| Element                        | Source                                                     | Mutagen feature                                                          |
|--------------------------------|-------------------------------------------------------------|---------------------------------------------------------------------------|
| Conflicts list (one row / path)| `_extract_conflicts(session)` flattening `conflicts[].alphaChanges[].path` and `betaChanges[].path` | [§6 conflict JSON shape](./mutagen_capabilities.md#6-conflict-json-shape) |
| Per-side modification tag      | Presence of paths in `alphaChanges` vs `betaChanges`        | [§6](./mutagen_capabilities.md#6-conflict-json-shape)                     |
| `1 take REMOTE` action         | `_resolve_selected("beta")`: `rm <local file>` then `mutagen sync reset <session>` | [§8 per-file delete + reset](./mutagen_capabilities.md#8-per-file-conflict-resolution-via-delete--mutagen-sync-reset) |
| `5 take LOCAL` action          | `_resolve_selected("alpha")`: `cinna exec rm <remote file>` then `mutagen sync reset <session>` | [§8](./mutagen_capabilities.md#8-per-file-conflict-resolution-via-delete--mutagen-sync-reset); requires cinna's own remote-exec channel |
| Auto-refresh                   | `_maybe_refresh_conflicts` is called on every monitor record; re-renders the tab only when the set actually changes | [§2 monitor](./mutagen_capabilities.md#2-mutagen-sync-monitor-streams-state-on-every-change), [§6 conflicts](./mutagen_capabilities.md#6-conflict-json-shape) |

### Why not source from disk?

Because Mutagen 0.18.1 in `two-way-safe` does **not** write `.conflict.<side>.<ts>` files — both sides keep their own version and only the JSON state records the divergence. See [§7](./mutagen_capabilities.md#7--two-way-safe-does-not-write-conflictsidets-files). If a future mutagen version starts writing these copies again, `src/cinna/sync_session.py:list_conflicts` and `group_conflicts` are still in place and can be wired back in as a secondary data source.

---

## Keybindings

| Key       | Action            | Where it lives                                                       |
|-----------|-------------------|-----------------------------------------------------------------------|
| `q`       | Quit              | App-level `action_quit`; on exit, the caller (`run_foreground`) calls `stop(config)` to terminate the Mutagen session. |
| `Ctrl-C`  | Quit              | Same as `q`.                                                          |
| `←` / `→` | Cycle tabs        | `action_cycle_tab(±1)`; wraps at ends.                               |
| `1`       | Take REMOTE       | No-op outside the Conflicts tab. See [§8](./mutagen_capabilities.md#8-per-file-conflict-resolution-via-delete--mutagen-sync-reset). |
| `5`       | Take LOCAL        | No-op outside the Conflicts tab. `1` and `5` are placed far apart on the keyboard to make a misfire unlikely. |
| `↑` / `↓` | Navigate Conflicts list | Native to Textual's `OptionList`; only the Conflicts tab's list is focusable, so these keys do nothing on Sync / Details. |

The `←` / `→` / `1` / `5` bindings are declared with `priority=True` so they fire even when the Conflicts `OptionList` has focus.

---

## Lifecycle

```
cinna dev
    └─ sync_session.start(config, workspace_root)   # create / refresh Mutagen session
    └─ run_foreground(config, workspace_root)
          └─ run_tui(config, session_name, env, workspace_root)
                └─ SyncApp.run()
                       on_mount:
                         · spawn _monitor_loop  (mutagen sync monitor --template …)
                         · spawn _details_loop  (mutagen sync list --long, every 2 s)
                       user presses q / Ctrl-C
                       on_unmount:
                         · cancel both loops
                         · terminate the monitor subprocess
          └─ stop(config)   # mutagen sync terminate <session-name>
```

The TUI does not outlive its terminal. Once the user quits, sync stops. To observe an existing sync session from another terminal without affecting it, the user runs `cinna sync status` (a read-only Click subcommand defined in `main.py`, not part of the TUI).

---

## Adding a new TUI element

Procedure when you want to expose a new piece of Mutagen state:

1. Look at the raw JSON: `mutagen sync list --template '{{json .}}' <session>` while the relevant state is active. Note the exact JSON key and where it lives (top-level, under `alpha`/`beta`, etc.).
2. Add a row to [`mutagen_capabilities.md`](./mutagen_capabilities.md) if the field isn't already documented there. Include a reproduction command — future you, on the next mutagen bump, needs to verify it didn't move.
3. Wire the read into `_render_sync_tab` (for static display) or `_emit_events` (for diff-driven log lines). Use `_safe_int` from `sync_session.py` for numeric fields to keep the defensive-accessor pattern consistent.
4. Add a row to the appropriate table in this document linking the new element back to the capabilities reference.

If a feature would need a Mutagen capability that doesn't yet exist in our pinned version, document the gap in [`mutagen_capabilities.md`](./mutagen_capabilities.md) and link to it from here so we know what to look for on the next bump.
