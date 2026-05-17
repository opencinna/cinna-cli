"""Textual TUI for `cinna sync start`.

Two tabs:
  * **Sync** — friendly status block with a live activity log derived from
    polling ``mutagen sync list``. This is the default view.
  * **Details** — raw ``mutagen sync list --long <name>`` output, what
    Mutagen itself shows a power user.

The TUI polls the Mutagen daemon a few times per second. Ctrl-C / ``q`` quits;
the caller (``sync_session.run_foreground``) is responsible for terminating the
Mutagen session once the TUI exits — sync does not outlive the terminal.

Every per-file event we surface in the TUI also lands in ``cinna.log`` via the
``cinna.sync_tui`` logger, so users can audit exactly which files moved through
the sync after the TUI closes.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
import subprocess
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, OptionList, RichLog, Static, TabbedContent, TabPane
from textual.widgets.option_list import Option

from .config import CinnaConfig, workspace_dir
from .sync_session import _safe_int, base_status

logger = logging.getLogger(__name__)


_MARKUP_RE = re.compile(r"\[/?[^\]]+\]")


def _plain(msg: str) -> str:
    """Strip Rich/Textual markup so the file logger gets human-readable text."""
    return _MARKUP_RE.sub("", msg)


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n:.1f} PB"


def _fmt_delta(files: int, size: int) -> str:
    """Format a cycle delta like ``+12 files (3.4 MB)`` or ``-5 files``.

    The byte count uses ``abs()`` because the file-count sign already carries
    the direction; a separate sign on the size adds noise.
    """
    sign = "+" if files > 0 else ""
    size_part = f" ({_fmt_size(abs(size))})" if size else ""
    return f"{sign}{files} files{size_part}"


@dataclass(frozen=True)
class ConflictEntry:
    """One row in the Conflicts tab, sourced from mutagen's session JSON.

    ``path`` is the conflicting workspace-relative path. ``alpha_modified``
    and ``beta_modified`` are derived from ``alphaChanges``/``betaChanges``
    so the UI can show which side(s) actually diverged.
    """
    path: str
    alpha_modified: bool
    beta_modified: bool


def _extract_conflicts(session: dict | None) -> list[ConflictEntry]:
    """Flatten mutagen's nested conflicts JSON into one row per path."""
    if not session:
        return []
    out: list[ConflictEntry] = []
    seen: set[str] = set()
    for c in (session.get("conflicts") or []):
        paths: set[str] = set()
        alpha_paths: set[str] = set()
        beta_paths: set[str] = set()
        for change in (c.get("alphaChanges") or []):
            p = change.get("path") or c.get("root") or ""
            if p:
                paths.add(p)
                alpha_paths.add(p)
        for change in (c.get("betaChanges") or []):
            p = change.get("path") or c.get("root") or ""
            if p:
                paths.add(p)
                beta_paths.add(p)
        # Some conflict kinds (e.g. directory/file disagreement) only carry
        # the root path. Surface that so the row isn't blank.
        if not paths and c.get("root"):
            paths.add(c["root"])
        for p in paths:
            if p in seen:
                continue
            seen.add(p)
            out.append(ConflictEntry(
                path=p,
                alpha_modified=p in alpha_paths,
                beta_modified=p in beta_paths,
            ))
    out.sort(key=lambda e: e.path)
    return out


def _parse_monitor_payload(line: bytes) -> dict | None:
    """Parse one ``{{json .}}`` line from ``mutagen sync monitor``.

    Mutagen renders the State value through Go's text/template, which for a
    single-session monitor wraps it in a one-element JSON array. We pull the
    first element back out.
    """
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    if isinstance(data, list):
        return data[0] if data else None
    if isinstance(data, dict):
        return data
    return None


def _side_label(status: str) -> str | None:
    """Map a Mutagen side-suffixed status to a friendly direction label.

    ``staging-beta`` means files are flowing alpha→beta (local→remote in our
    deployment, since alpha is the local workspace).
    """
    if not status:
        return None
    if status.endswith("-alpha"):
        return "remote→local"
    if status.endswith("-beta"):
        return "local→remote"
    return None


def _state_pill(session: dict | None) -> str:
    if session is None:
        return "[red]⬤  Session not found[/red]"
    if session.get("paused"):
        return "[yellow]⬤  Paused[/yellow]"
    alpha_conn = bool((session.get("alpha") or {}).get("connected"))
    beta_conn = bool((session.get("beta") or {}).get("connected"))
    last_error = session.get("lastError")
    status = (session.get("status") or "").lower()
    if last_error:
        return f"[red]⬤  Error[/red]"
    if not (alpha_conn and beta_conn):
        return "[red]⬤  Disconnected[/red]"
    if status in {"watching", "watching-changes", "ready"}:
        return "[green]⬤  Watching for changes[/green]"
    base = base_status(status)
    if base in {"scanning", "staging", "transitioning", "saving", "reconciling", "transferring"}:
        direction = _side_label(status)
        suffix = f" [{direction}]" if direction else ""
        return f"[cyan]⬤  {base.title()}{suffix}[/cyan]"
    return f"[cyan]⬤  {status.title() or 'Connected'}[/cyan]"


class SyncApp(App):
    """Live status TUI for a single Mutagen sync session."""

    CSS = """
    Screen { background: $surface; }
    #status { height: 7; padding: 1 2; border: round $primary; margin: 1 1 0 1; }
    #stats  { height: 3; padding: 0 2; margin: 0 1; color: $text; }
    #activity { border: round $primary; margin: 0 1 1 1; }
    #details-scroll { padding: 1 2; margin: 1; }
    #details-text { color: $text; }
    """

    # Tab order matters for left/right cycling.
    TAB_IDS: tuple[str, ...] = ("sync-tab", "details-tab", "conflicts-tab")

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True, priority=True),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
        Binding("left", "cycle_tab(-1)", "◀ Tab", show=True, priority=True),
        Binding("right", "cycle_tab(1)", "Tab ▶", show=True, priority=True),
        # 1/5 only do anything on the Conflicts tab with a row highlighted;
        # the actions no-op otherwise so the user can mash them harmlessly.
        # 1 and 5 are spaced apart on the keyboard to make a misfire unlikely.
        Binding("1", "take_remote", "take REMOTE", show=True, priority=True),
        Binding("5", "take_local", "take LOCAL", show=True, priority=True),
    ]

    # JSON state comes from a long-running `mutagen sync monitor` subprocess
    # which streams a fresh State for every change inside the daemon. Polling
    # would miss fast files: a small cycle can complete between two polls and
    # the `stagingProgress.path` ticks would never be observed. The Details
    # tab still polls `--long` because monitor only emits the JSON state.
    DETAILS_INTERVAL = 2.0
    MONITOR_RESPAWN_DELAY = 2.0
    MAX_LOG_LINES = 1000

    # (mutagen-side key, friendly label, direction arrow used in per-file log lines).
    _SIDES: tuple[tuple[str, str, str], ...] = (
        ("alpha", "local", "←"),
        ("beta", "remote", "→"),
    )

    def __init__(
        self,
        config: CinnaConfig,
        session_name: str,
        mutagen_env: dict[str, str],
        workspace_root: Path,
    ) -> None:
        super().__init__()
        self.config = config
        self.session_name = session_name
        self._env = mutagen_env
        self._workspace_root = workspace_root
        self._prev: dict | None = None
        self._monitor_task: asyncio.Task | None = None
        self._details_task: asyncio.Task | None = None
        self._monitor_proc: asyncio.subprocess.Process | None = None
        self._shutting_down = False
        # Cycle-bump deltas need the files/totalFileSize on each side *before*
        # the cycle counter incremented. Written in the status-change branch
        # of _emit_events; consumed by _emit_cycle_complete.
        self._pre_cycle_snapshot: dict | None = None
        # Conflicts are sourced from mutagen's JSON state — Mutagen 0.18 in
        # two-way-safe does NOT write `.conflict.*` files, so a disk walk
        # would come up empty even when conflicts exist. We still do the walk
        # as a secondary lookup when a resolution is requested, since some
        # sync modes do produce those copies.
        self._conflicts: list[ConflictEntry] = []

    # ── Layout ────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="sync-tab"):
            with TabPane("Sync", id="sync-tab"):
                yield Static("", id="status")
                yield Static("", id="stats")
                yield RichLog(
                    id="activity",
                    auto_scroll=True,
                    markup=True,
                    max_lines=self.MAX_LOG_LINES,
                )
            with TabPane("Details", id="details-tab"):
                with VerticalScroll(id="details-scroll"):
                    yield Static("Loading…", id="details-text")
            with TabPane("Conflicts", id="conflicts-tab"):
                yield Static("[dim](no conflicts)[/dim]", id="conflicts-header")
                yield OptionList(id="conflicts-list")
                yield Static(
                    "[dim]↑/↓ navigate  ·  1 take REMOTE (server)  ·  "
                    "5 take LOCAL (yours)\n"
                    "Resolution deletes the losing side's file and resets "
                    "mutagen history so the survivor propagates.[/dim]",
                    id="conflicts-help",
                )
        yield Footer()

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def on_mount(self) -> None:
        self.title = f"cinna sync — {self.config.agent_name}"
        self.sub_title = self.session_name
        self._disable_mouse_tracking()
        # Seed both panes synchronously so the user sees something immediately;
        # the streaming/poll loops take over from there.
        await self._refresh_details_once()
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        self._details_task = asyncio.create_task(self._details_loop())

    async def on_unmount(self) -> None:
        self._shutting_down = True
        if self._monitor_proc is not None and self._monitor_proc.returncode is None:
            try:
                self._monitor_proc.terminate()
            except ProcessLookupError:
                pass

        # Cancel the data loops AND await them. If we return before they finish,
        # textual closes the asyncio loop while a `mutagen` subprocess is still
        # running in the background; when that process eventually exits, its
        # SIGCHLD is dispatched to a closed loop and we get "Loop <...> that
        # handles pid N is closed" on stderr. Awaiting the tasks here gives
        # their finally-blocks a chance to fully reap.
        pending = [t for t in (self._monitor_task, self._details_task) if t is not None]
        for task in pending:
            task.cancel()
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=2.0,
                )
            except asyncio.TimeoutError:
                logger.debug("data-loop tasks did not finish cleanly within 2s")

    def _disable_mouse_tracking(self) -> None:
        """Turn off the mouse-tracking modes textual enabled on startup.

        Some terminal/shell combinations echo SGR mouse sequences as literal
        text in the scrollback instead of consuming them as input events. We
        don't use the mouse in this app (keyboard-only), so disabling tracking
        makes the issue impossible.

        Sequences mirror the ones textual emits to enable tracking — disable
        variants use ``l`` instead of ``h``.
        """
        import sys as _sys
        try:
            _sys.__stdout__.write(
                "\033[?1000l"  # X10 mouse off
                "\033[?1002l"  # button-event tracking off
                "\033[?1003l"  # any-event tracking off
                "\033[?1006l"  # SGR extended mode off
                "\033[?1015l"  # URxvt extended mode off
            )
            _sys.__stdout__.flush()
        except Exception:
            pass

    def action_show_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id

    def action_cycle_tab(self, direction: int) -> None:
        """Cycle tabs with left/right arrows (wraps at ends)."""
        tabs = self.query_one(TabbedContent)
        try:
            idx = self.TAB_IDS.index(tabs.active)
        except ValueError:
            idx = 0
        tabs.active = self.TAB_IDS[(idx + direction) % len(self.TAB_IDS)]

    # All content widgets are read-only — don't let them swallow keys meant
    # for the app bindings (e.g. Log grabs pgup/pgdn, Scroll consumes arrows).
    # The conflicts-list IS focusable so up/down navigates rows; left/right
    # tab-cycling and 1/2 resolution remain priority bindings, so they fire
    # even when the list has focus.
    async def on_ready(self) -> None:
        for widget_id in (
            "status", "stats", "activity",
            "details-text", "details-scroll",
            "conflicts-header", "conflicts-help",
        ):
            try:
                w = self.query_one(f"#{widget_id}")
            except Exception:
                continue
            w.can_focus = False

    def on_tabbed_content_tab_activated(self, event) -> None:
        # `event.tab.id` is the prefixed ContentTab id (e.g.
        # `--content-tab-conflicts-tab`), not the TabPane id. The
        # TabbedContent's ``active`` property maps back to the pane id we
        # actually set in compose(), so it's what we compare against.
        if event.tabbed_content.active == "conflicts-tab":
            try:
                self.query_one("#conflicts-list", OptionList).focus()
            except Exception:
                pass
        # Footer reflects bindings as of the last refresh; tab changes flip
        # whether take_remote/take_local are applicable (see check_action).
        self.refresh_bindings()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # 1/5 only resolve conflicts, which only makes sense on that tab.
        # Returning False hides them from the footer entirely (None would
        # leave them grayed out, which is still noisy on the other tabs).
        if action in ("take_remote", "take_local"):
            try:
                active = self.query_one(TabbedContent).active
            except Exception:
                return True
            if active != "conflicts-tab":
                return False
        return True

    # ── Data loops ────────────────────────────────────────────────────────

    async def _monitor_loop(self) -> None:
        """Stream JSON State updates from `mutagen sync monitor`.

        Mutagen emits a fresh State to its stdout every time anything inside
        the daemon changes (new staging path, byte progress, cycle bump, …).
        Re-spawns the subprocess if it exits (transient session hiccup, daemon
        restart) until the TUI shuts down.
        """
        try:
            while not self._shutting_down:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "mutagen",
                        "sync",
                        "monitor",
                        "--template",
                        '{{json .}}{{"\\n"}}',
                        self.session_name,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=self._env,
                        start_new_session=True,
                    )
                except (FileNotFoundError, OSError) as exc:
                    self._render_sync_tab(None)
                    logger.error("could not spawn mutagen monitor: %s", exc)
                    await asyncio.sleep(self.MONITOR_RESPAWN_DELAY)
                    continue

                self._monitor_proc = proc
                assert proc.stdout is not None
                try:
                    async for raw in proc.stdout:
                        line = raw.strip()
                        if not line:
                            continue
                        session = _parse_monitor_payload(line)
                        if session is not None:
                            self._render_sync_tab(session)
                finally:
                    if proc.returncode is None:
                        try:
                            proc.terminate()
                        except ProcessLookupError:
                            pass
                    await proc.wait()
                    self._monitor_proc = None

                if self._shutting_down:
                    break
                # Daemon hiccup or session vanished — show "missing" and retry.
                self._render_sync_tab(None)
                await asyncio.sleep(self.MONITOR_RESPAWN_DELAY)
        except asyncio.CancelledError:
            pass

    async def _details_loop(self) -> None:
        try:
            while not self._shutting_down:
                await self._refresh_details_once()
                await asyncio.sleep(self.DETAILS_INTERVAL)
        except asyncio.CancelledError:
            pass

    async def _refresh_details_once(self) -> None:
        text = await self._fetch_session_long()
        try:
            self.query_one("#details-text", Static).update(text)
        except Exception:
            pass

    async def _fetch_session_long(self) -> str:
        stdout = await self._run_mutagen(
            ["sync", "list", "--long", self.session_name],
        )
        return stdout or "(no data)"

    async def _run_mutagen(self, args: list[str]) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "mutagen",
                *args,
                # Detach from the controlling tty — otherwise each spawn races
                # textual's driver for stdin and leaks raw mouse/key escape
                # sequences into the rendered screen.
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env,
                start_new_session=True,
            )
        except (FileNotFoundError, OSError) as exc:
            return f"(mutagen unavailable: {exc})"

        try:
            stdout, _ = await proc.communicate()
        except asyncio.CancelledError:
            # The TUI is shutting down; reap the child before it outlives the
            # event loop and triggers a "Loop ... is closed" SIGCHLD warning.
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                with contextlib.suppress(Exception):
                    await proc.wait()
            raise
        if proc.returncode != 0:
            return ""
        return stdout.decode("utf-8", errors="replace").strip()

    # ── Render: Sync tab ──────────────────────────────────────────────────

    def _render_sync_tab(self, session: dict | None) -> None:
        status_w = self.query_one("#status", Static)
        stats_w = self.query_one("#stats", Static)
        activity = self.query_one("#activity", RichLog)

        pill = _state_pill(session)
        alpha = (session or {}).get("alpha") or {}
        beta = (session or {}).get("beta") or {}

        alpha_url = alpha.get("path") or "?"
        beta_host = beta.get("host")
        beta_path = beta.get("path") or "?"
        beta_url = f"{beta.get('user','')}@{beta_host}:{beta_path}" if beta_host else beta_path

        last_error = (session or {}).get("lastError") or ""
        status_lines = [
            pill,
            f"[dim]Agent:[/dim]    {self.config.agent_name} [dim]@[/dim] {self.config.platform_url}",
            f"[dim]Local:[/dim]    {alpha_url}",
            f"[dim]Remote:[/dim]   {beta_url}",
        ]
        if last_error:
            status_lines.append(f"[red]{last_error}[/red]")
        status_w.update("\n".join(status_lines))

        files = _safe_int(alpha.get("files"))
        dirs = _safe_int(alpha.get("directories"))
        size = _safe_int(alpha.get("totalFileSize"))
        cycles = _safe_int((session or {}).get("successfulCycles"))
        stats_line = (
            f"[bold]{files}[/bold] files · [bold]{dirs}[/bold] dirs · "
            f"[bold]{_fmt_size(size)}[/bold]   "
            f"Successful cycles: [bold]{cycles}[/bold]"
        )
        # If a transfer is in flight, append a progress hint sourced from
        # whichever side is receiving — gives the user a sense of throughput
        # without having to flip to Details.
        for side, *_ in self._SIDES:
            sp = ((session or {}).get(side) or {}).get("stagingProgress") or {}
            if sp.get("path"):
                stats_line += (
                    f"   [dim]·[/dim] receiving "
                    f"{_safe_int(sp.get('receivedFiles'))}/{_safe_int(sp.get('expectedFiles'))} "
                    f"({_fmt_size(_safe_int(sp.get('totalReceivedSize')))} so far, "
                    f"current file {_fmt_size(_safe_int(sp.get('expectedSize')))})"
                )
                break
        stats_w.update(stats_line)

        self._emit_events(activity, session)
        self._maybe_refresh_conflicts(session)

    def _emit_events(self, log: RichLog, session: dict | None) -> None:
        now = datetime.now().strftime("%H:%M:%S")

        def line(msg: str, level: str = "info") -> None:
            log.write(f"{now}  {msg}")
            getattr(logger, level)("%s", _plain(msg))

        if session is None:
            if self._prev is not None:
                line("session disappeared from Mutagen daemon", "warning")
            self._prev = None
            return

        if self._prev is None:
            a_conn = bool((session.get("alpha") or {}).get("connected"))
            b_conn = bool((session.get("beta") or {}).get("connected"))
            if a_conn and b_conn:
                line("sync attached — both endpoints connected")
            else:
                line("sync attached — waiting for endpoints to connect")
            # Fall through with an empty baseline so any in-progress staging
            # already reported by the daemon is emitted on the first attach
            # rather than swallowed.
            prev = {}
        else:
            prev = self._prev
        self._prev = session

        prev_status = (prev.get("status") or "").lower()
        cur_status = (session.get("status") or "").lower()
        if prev_status != cur_status:
            direction = _side_label(cur_status)
            suffix = f" [{direction}]" if direction else ""
            line(f"status: {prev_status or '-'} → {cur_status or '-'}{suffix}")
            # When we first enter a staging/transitioning phase, snapshot the
            # current file counts so we can report a meaningful delta when the
            # cycle counter increments below.
            if cur_status.startswith(("staging", "transitioning")) and self._pre_cycle_snapshot is None:
                self._pre_cycle_snapshot = prev

        for side, label, _arrow in self._SIDES:
            prev_conn = bool((prev.get(side) or {}).get("connected"))
            cur_conn = bool((session.get(side) or {}).get("connected"))
            if prev_conn != cur_conn:
                line(f"{label} endpoint: {'connected' if cur_conn else 'disconnected'}",
                     "info" if cur_conn else "warning")

        self._emit_staging_events(line, prev, session)
        self._emit_problem_events(line, prev, session)
        self._emit_conflict_events(line, prev, session)

        prev_cycles = _safe_int(prev.get("successfulCycles"))
        cur_cycles = _safe_int(session.get("successfulCycles"))
        if cur_cycles > prev_cycles:
            self._emit_cycle_complete(line, session, cur_cycles)
            self._pre_cycle_snapshot = None

        prev_err = prev.get("lastError") or ""
        cur_err = session.get("lastError") or ""
        if cur_err and cur_err != prev_err:
            line(f"[red]error:[/red] {cur_err}", "error")
        elif prev_err and not cur_err:
            line("error cleared")

        prev_paused = bool(prev.get("paused"))
        cur_paused = bool(session.get("paused"))
        if prev_paused != cur_paused:
            line("session paused" if cur_paused else "session resumed")

    # ── Per-file event helpers ───────────────────────────────────────────

    def _emit_staging_events(self, line, prev: dict, session: dict) -> None:
        """Log each file Mutagen reports it is currently receiving.

        Mutagen's ``stagingProgress`` block ticks per progress update (often
        many per file). We dedupe against the previous tick's path so a single
        file produces one event regardless of how many progress ticks we see.
        """
        for side, label, arrow in self._SIDES:
            sp = (session.get(side) or {}).get("stagingProgress") or {}
            path = sp.get("path") or ""
            prev_path = ((prev.get(side) or {}).get("stagingProgress") or {}).get("path") or ""
            if not path or path == prev_path:
                continue
            expected = _safe_int(sp.get("expectedSize"))
            received_files = _safe_int(sp.get("receivedFiles"))
            expected_files = _safe_int(sp.get("expectedFiles"))
            counter = f" [{received_files + 1}/{expected_files}]" if expected_files else ""
            size_hint = f" ({_fmt_size(expected)})" if expected else ""
            line(f"  {arrow} {label}{counter} {path}{size_hint}")

    def _emit_problem_events(self, line, prev: dict, cur: dict) -> None:
        """Surface new scan/transition problems with their paths.

        Mutagen tracks these as growing lists per side. We emit only the
        suffix (entries beyond what we logged on the previous tick) so each
        problem appears in the log exactly once.
        """
        for key, label in (
            ("alphaScanProblems", "local scan"),
            ("betaScanProblems", "remote scan"),
            ("alphaTransitionProblems", "local apply"),
            ("betaTransitionProblems", "remote apply"),
        ):
            prev_n = len(prev.get(key) or [])
            cur_list = cur.get(key) or []
            for problem in cur_list[prev_n:]:
                path = problem.get("path") or "?"
                err = problem.get("error") or "(unknown)"
                line(f"[yellow]{label} problem[/yellow]: {path} — {err}", "warning")

    def _emit_conflict_events(self, line, prev: dict, cur: dict) -> None:
        """Surface new conflicts with the paths each side touched."""
        prev_n = len(prev.get("conflicts") or [])
        cur_list = cur.get("conflicts") or []
        for conflict in cur_list[prev_n:]:
            paths: list[str] = []
            for change_key in ("alphaChanges", "betaChanges"):
                for change in (conflict.get(change_key) or []):
                    p = change.get("path") or ""
                    if p and p not in paths:
                        paths.append(p)
            shown = ", ".join(paths) or "(unknown path)"
            line(f"[red]conflict[/red]: {shown}", "warning")

    def _emit_cycle_complete(self, line, session: dict, cycle: int) -> None:
        """Annotate the cycle bump with a transferred-bytes summary.

        Mutagen doesn't report a cumulative cycle delta, so we diff against
        the snapshot taken when staging began. When both sides converged to
        identical deltas (the common "initial sync from empty" case) we
        collapse to one summary line instead of repeating it per side.
        """
        snap = self._pre_cycle_snapshot or {}
        deltas: dict[str, tuple[int, int]] = {}
        for side, label, _arrow in self._SIDES:
            cur = session.get(side) or {}
            prev = snap.get(side) or {}
            df = _safe_int(cur.get("files")) - _safe_int(prev.get("files"))
            dsz = _safe_int(cur.get("totalFileSize")) - _safe_int(prev.get("totalFileSize"))
            if df or dsz:
                deltas[label] = (df, dsz)

        if not deltas:
            line(f"cycle #{cycle} complete")
            return

        values = list(deltas.values())
        if len(values) == 2 and values[0] == values[1]:
            line(f"cycle #{cycle} complete — synced {_fmt_delta(*values[0])}")
            return

        parts = [f"{label} {_fmt_delta(df, dsz)}" for label, (df, dsz) in deltas.items()]
        line(f"cycle #{cycle} complete — " + ", ".join(parts))

    # ── Conflicts tab ────────────────────────────────────────────────────

    def _maybe_refresh_conflicts(self, session: dict | None) -> None:
        """Sync the Conflicts tab with mutagen's current JSON state.

        Cheap to call on every state update — we only re-render the tab when
        the conflict set actually changes.
        """
        new = _extract_conflicts(session)
        if new == self._conflicts:
            return
        self._conflicts = new
        self._render_conflicts_tab()

    def _render_conflicts_tab(self) -> None:
        try:
            header = self.query_one("#conflicts-header", Static)
            olist = self.query_one("#conflicts-list", OptionList)
        except Exception:
            return
        olist.clear_options()
        if not self._conflicts:
            header.update("[dim](no conflicts)[/dim]")
            return
        header.update(f"[bold]Conflicts ({len(self._conflicts)})[/bold]")
        for c in self._conflicts:
            sides = []
            if c.alpha_modified:
                sides.append("local")
            if c.beta_modified:
                sides.append("remote")
            tag = f" [dim]({'+'.join(sides)} modified)[/dim]" if sides else ""
            olist.add_option(Option(f"{c.path}{tag}", id=c.path))

    async def action_take_remote(self) -> None:
        await self._resolve_selected("beta")

    async def action_take_local(self) -> None:
        await self._resolve_selected("alpha")

    async def _resolve_selected(self, side: str) -> None:
        """Resolve the currently-highlighted conflict.

        Mechanism (verified against mutagen 0.18.1):
          1. Delete the file on the side that's about to *lose* — alpha
             for take-remote, beta for take-local.
          2. ``mutagen sync reset`` to drop history. With no common ancestor
             and only one side holding content, mutagen treats this as an
             initial sync and propagates the surviving content to the empty
             side. Other conflicts in the same session keep their state
             because both sides still hold (divergent) content for them.

        Beta lives on the remote agent, so the beta delete goes through
        ``cinna exec`` rather than the local filesystem.
        """
        try:
            tabs = self.query_one(TabbedContent)
            if tabs.active != "conflicts-tab":
                return
            olist = self.query_one("#conflicts-list", OptionList)
        except Exception:
            return
        idx = olist.highlighted
        if idx is None or idx < 0 or idx >= len(self._conflicts):
            return
        entry = self._conflicts[idx]
        label = "remote" if side == "beta" else "local"
        self._log_to_activity(
            f"[cyan]resolving[/cyan] {entry.path} → take {label} (deleting "
            f"the losing side and resetting sync history)…"
        )

        if side == "beta":
            ok = await self._delete_local(entry.path)
        else:
            ok = await self._delete_remote(entry.path)
        if not ok:
            return

        if not await self._mutagen_sync_reset():
            return

        self._log_to_activity(
            f"[green]took {label}[/green] for {entry.path} — mutagen is "
            f"propagating the convergence"
        )

    async def _delete_local(self, relpath: str) -> bool:
        path = workspace_dir(self._workspace_root) / relpath
        try:
            path.unlink(missing_ok=True)
            return True
        except OSError as exc:
            self._log_to_activity(
                f"[red]could not delete local {relpath}[/red]: {exc}",
                level="error",
            )
            return False

    async def _delete_remote(self, relpath: str) -> bool:
        remote_path = f"/app/workspace/{relpath}"
        # cinna exec joins its argv with spaces and ships the result as one
        # shell string to the agent, so pre-quote the path and send the whole
        # command as a single arg.
        cmd_str = f"rm -f -- {shlex.quote(remote_path)}"
        try:
            proc = await asyncio.create_subprocess_exec(
                "cinna", "exec", cmd_str,
                cwd=str(self._workspace_root),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except (FileNotFoundError, OSError) as exc:
            self._log_to_activity(
                f"[red]could not invoke `cinna exec`[/red]: {exc}",
                level="error",
            )
            return False
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            self._log_to_activity(
                f"[red]remote delete failed[/red] for {relpath}: "
                f"{stderr.decode(errors='replace').strip() or f'exit {proc.returncode}'}",
                level="error",
            )
            return False
        return True

    async def _mutagen_sync_reset(self) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "mutagen", "sync", "reset", self.session_name,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env,
                start_new_session=True,
            )
        except (FileNotFoundError, OSError) as exc:
            self._log_to_activity(
                f"[red]could not invoke `mutagen sync reset`[/red]: {exc}",
                level="error",
            )
            return False
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            self._log_to_activity(
                f"[red]mutagen sync reset failed[/red]: "
                f"{stderr.decode(errors='replace').strip() or f'exit {proc.returncode}'}",
                level="error",
            )
            return False
        return True

    def _rel_to_workspace(self, p: Path) -> Path:
        try:
            return p.relative_to(workspace_dir(self._workspace_root))
        except ValueError:
            return p

    def _log_to_activity(self, msg: str, level: str = "info") -> None:
        now = datetime.now().strftime("%H:%M:%S")
        try:
            self.query_one("#activity", RichLog).write(f"{now}  {msg}")
        except Exception:
            pass
        getattr(logger, level)("%s", _plain(msg))


def run_tui(
    config: CinnaConfig,
    session_name: str,
    mutagen_env: dict[str, str],
    workspace_root: Path,
) -> int:
    """Start the TUI app in the current terminal. Returns on user quit."""
    # Suppress textual's INFO logging to keep our logger output clean in the
    # (unlikely) event the user has DEBUG on.
    logging.getLogger("textual").setLevel(logging.WARNING)
    app = SyncApp(config, session_name, mutagen_env, workspace_root)
    app.run()
    return 0
