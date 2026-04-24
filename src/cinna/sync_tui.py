"""Textual TUI for `cinna sync start`.

Two tabs:
  * **Sync** — friendly status block with a live activity log derived from
    polling ``mutagen sync list``. This is the default view.
  * **Details** — raw ``mutagen sync list --long <name>`` output, what
    Mutagen itself shows a power user.

The TUI polls the Mutagen daemon once per second. Ctrl-C / ``q`` quits; the
caller (``sync_session.run_foreground``) is responsible for terminating the
Mutagen session once the TUI exits — sync does not outlive the terminal.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from collections import deque
from datetime import datetime
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Log, Static, TabbedContent, TabPane

from .config import CinnaConfig

logger = logging.getLogger(__name__)


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n:.1f} PB"


def _state_pill(session: dict | None) -> str:
    if session is None:
        return "[red]⬤ Session not found[/red]"
    if session.get("paused"):
        return "[yellow]⬤ Paused[/yellow]"
    alpha_conn = bool((session.get("alpha") or {}).get("connected"))
    beta_conn = bool((session.get("beta") or {}).get("connected"))
    last_error = session.get("lastError")
    status = (session.get("status") or "").lower()
    if last_error:
        return f"[red]⬤ Error[/red]"
    if not (alpha_conn and beta_conn):
        return "[red]⬤ Disconnected[/red]"
    if status in {"watching", "watching-changes", "ready"}:
        return "[green]⬤ Watching for changes[/green]"
    if status in {"scanning", "staging", "transitioning", "saving", "reconciling", "transferring"}:
        return f"[cyan]⬤ {status.title()}[/cyan]"
    return f"[cyan]⬤ {status.title() or 'Connected'}[/cyan]"


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
    TAB_IDS: tuple[str, ...] = ("sync-tab", "details-tab")

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True, priority=True),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
        Binding("left", "cycle_tab(-1)", "◀ Tab", show=True, priority=True),
        Binding("right", "cycle_tab(1)", "Tab ▶", show=True, priority=True),
        Binding("1", "show_tab('sync-tab')", "Sync", show=False, priority=True),
        Binding("2", "show_tab('details-tab')", "Details", show=False, priority=True),
    ]

    POLL_INTERVAL = 1.0
    MAX_LOG_LINES = 500

    def __init__(
        self,
        config: CinnaConfig,
        session_name: str,
        mutagen_env: dict[str, str],
    ) -> None:
        super().__init__()
        self.config = config
        self.session_name = session_name
        self._env = mutagen_env
        self._prev: dict | None = None
        self._poll_task: asyncio.Task | None = None

    # ── Layout ────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="sync-tab"):
            with TabPane("Sync", id="sync-tab"):
                yield Static("", id="status")
                yield Static("", id="stats")
                yield Log(
                    id="activity",
                    highlight=False,
                    auto_scroll=True,
                    max_lines=self.MAX_LOG_LINES,
                )
            with TabPane("Details", id="details-tab"):
                with VerticalScroll(id="details-scroll"):
                    yield Static("Loading…", id="details-text")
        yield Footer()

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def on_mount(self) -> None:
        self.title = f"cinna sync — {self.config.agent_name}"
        self.sub_title = self.session_name
        self._disable_mouse_tracking()
        self._poll_task = asyncio.create_task(self._poll_loop())
        await self._refresh_once()

    async def on_unmount(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()

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
    async def on_ready(self) -> None:
        for widget_id in ("status", "stats", "activity", "details-text", "details-scroll"):
            try:
                w = self.query_one(f"#{widget_id}")
            except Exception:
                continue
            w.can_focus = False

    # ── Polling ───────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.POLL_INTERVAL)
                await self._refresh_once()
        except asyncio.CancelledError:
            pass

    async def _refresh_once(self) -> None:
        session = await self._fetch_session_json()
        self._render_sync_tab(session)
        details_text = await self._fetch_session_long()
        self.query_one("#details-text", Static).update(details_text)

    async def _fetch_session_json(self) -> dict | None:
        stdout = await self._run_mutagen(
            ["sync", "list", "--template", "{{json .}}", self.session_name],
        )
        if not stdout or stdout == "null":
            return None
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return None
        if isinstance(data, list):
            return data[0] if data else None
        if isinstance(data, dict):
            return data
        return None

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
            stdout, _ = await proc.communicate()
        except (FileNotFoundError, OSError) as exc:
            return f"(mutagen unavailable: {exc})"
        if proc.returncode != 0:
            return ""
        return stdout.decode("utf-8", errors="replace").strip()

    # ── Render: Sync tab ──────────────────────────────────────────────────

    def _render_sync_tab(self, session: dict | None) -> None:
        status_w = self.query_one("#status", Static)
        stats_w = self.query_one("#stats", Static)
        activity = self.query_one("#activity", Log)

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

        files = int(alpha.get("files") or 0)
        dirs = int(alpha.get("directories") or 0)
        size = int(alpha.get("totalFileSize") or 0)
        cycles = int((session or {}).get("successfulCycles") or 0)
        stats_w.update(
            f"[bold]{files}[/bold] files · [bold]{dirs}[/bold] dirs · "
            f"[bold]{_fmt_size(size)}[/bold]   "
            f"Successful cycles: [bold]{cycles}[/bold]"
        )

        self._emit_events(activity, session)

    def _emit_events(self, log: Log, session: dict | None) -> None:
        now = datetime.now().strftime("%H:%M:%S")

        def line(msg: str) -> None:
            log.write_line(f"{now}  {msg}")

        if session is None:
            if self._prev is not None:
                line("session disappeared from Mutagen daemon")
            self._prev = None
            return

        if self._prev is None:
            a_conn = bool((session.get("alpha") or {}).get("connected"))
            b_conn = bool((session.get("beta") or {}).get("connected"))
            if a_conn and b_conn:
                line("sync attached — both endpoints connected")
            else:
                line("sync attached — waiting for endpoints to connect")
            self._prev = session
            return

        prev = self._prev
        self._prev = session

        prev_status = (prev.get("status") or "").lower()
        cur_status = (session.get("status") or "").lower()
        if prev_status != cur_status:
            line(f"status: {prev_status or '-'} → {cur_status or '-'}")

        for side, label in (("alpha", "local"), ("beta", "remote")):
            prev_conn = bool((prev.get(side) or {}).get("connected"))
            cur_conn = bool((session.get(side) or {}).get("connected"))
            if prev_conn != cur_conn:
                line(f"{label} endpoint: {'connected' if cur_conn else 'disconnected'}")

            prev_files = int((prev.get(side) or {}).get("files") or 0)
            cur_files = int((session.get(side) or {}).get("files") or 0)
            if cur_files != prev_files:
                delta = cur_files - prev_files
                sign = "+" if delta > 0 else ""
                line(f"{label} file count: {prev_files} → {cur_files} ({sign}{delta})")

        prev_cycles = int(prev.get("successfulCycles") or 0)
        cur_cycles = int(session.get("successfulCycles") or 0)
        if cur_cycles > prev_cycles:
            line(f"completed sync cycle #{cur_cycles}")

        prev_err = prev.get("lastError") or ""
        cur_err = session.get("lastError") or ""
        if cur_err and cur_err != prev_err:
            line(f"[red]error:[/red] {cur_err}")
        elif prev_err and not cur_err:
            line("error cleared")

        prev_paused = bool(prev.get("paused"))
        cur_paused = bool(session.get("paused"))
        if prev_paused != cur_paused:
            line("session paused" if cur_paused else "session resumed")


def run_tui(
    config: CinnaConfig,
    session_name: str,
    mutagen_env: dict[str, str],
) -> int:
    """Start the TUI app in the current terminal. Returns on user quit."""
    # Suppress textual's INFO logging to keep our logger output clean in the
    # (unlikely) event the user has DEBUG on.
    logging.getLogger("textual").setLevel(logging.WARNING)
    app = SyncApp(config, session_name, mutagen_env)
    app.run()
    return 0
