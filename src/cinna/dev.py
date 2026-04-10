"""Dev mode: bidirectional watch and auto-sync with Rich Live TUI."""

import hashlib
import logging
import signal
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import click
from rich.columns import Columns
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from cinna.config import CinnaConfig, workspace_dir, load_manifest, save_manifest
from cinna.client import PlatformClient
from cinna.sync import (
    DEFAULT_EXCLUDES,
    _is_excluded,
    compute_local_manifest,
    create_workspace_tarball,
    diff_manifests,
    extract_workspace_tarball,
    pull_credentials,
)
from cinna.docker import start_container, destroy_container, get_container_status
from cinna import console as cons

logger = logging.getLogger("cinna.dev")

# Max activity log entries to display
MAX_LOG_ENTRIES = 5


def run_dev_loop(
    config: CinnaConfig,
    workspace_root: Path,
    interval: int = 5,
) -> None:
    """Bidirectional watch with Rich Live display.

    Starts the container on entry and removes it on exit (Ctrl+C).
    """
    client = PlatformClient(config)
    workspace = workspace_dir(workspace_root)

    cons.console.print("Starting container...")
    start_container(workspace_root)

    last_manifest = load_manifest(workspace_root)
    if not last_manifest:
        last_manifest = compute_local_manifest(workspace)
        save_manifest(last_manifest, workspace_root)
        logger.info("Initialized manifest with %d files", len(last_manifest))

    # Hash local credentials on disk as baseline
    last_creds_hash = _local_credentials_hash(workspace)

    # Fetch container info AFTER ensuring it's running
    container_info = get_container_status(config, workspace_root)

    # State for the TUI
    state = _DevState(
        agent_name=config.agent_name,
        container_name=container_info.get("name", "") or config.container_name,
        container_id=container_info.get("id", "")[:12],
        container_status=container_info.get("status", "unknown"),
        interval=interval,
        file_count=len(last_manifest),
    )

    try:
        with Live(
            state.render(),
            console=cons.console,
            refresh_per_second=2,
            screen=False,
        ) as live:
            while True:
                time.sleep(interval)
                try:
                    result, last_creds_hash = _sync_cycle(
                        client, config, workspace, workspace_root,
                        last_manifest, last_creds_hash,
                    )
                    last_manifest = load_manifest(workspace_root)
                    state.file_count = len(last_manifest)
                    state.last_sync = datetime.now()
                    state.cycles += 1

                    # Refresh container status
                    cs = get_container_status(config, workspace_root)
                    state.container_name = cs.get("name", "") or config.container_name
                    state.container_id = cs.get("id", "")[:12]
                    state.container_status = cs.get("status", "unknown")

                    if result:
                        state.total_pushed += result["pushed"]
                        state.total_pulled += result["pulled"]
                        state.total_conflicts += result["conflicts"]
                        for entry in result["log_entries"]:
                            state.log.append(entry)

                except click.ClickException as e:
                    state.log.append((_ts(), "red", f"Error: {e.format_message()}", []))
                    logger.warning("Sync cycle failed: %s", e)
                except Exception as e:
                    state.log.append((_ts(), "red", f"Error: {e}", []))
                    logger.warning("Sync cycle failed: %s", e, exc_info=True)

                live.update(state.render())

    except KeyboardInterrupt:
        pass
    finally:
        # Ignore SIGINT during cleanup so docker compose down can finish
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        client.close()
        cons.console.print()
        cons.console.print("Removing container...")
        destroy_container(workspace_root)

    cons.status("Dev mode stopped. Container removed.")


class _DevState:
    """Mutable state for the TUI display."""

    def __init__(
        self,
        agent_name: str,
        container_name: str,
        container_id: str,
        container_status: str,
        interval: int,
        file_count: int,
    ):
        self.agent_name = agent_name
        self.container_name = container_name
        self.container_id = container_id
        self.container_status = container_status
        self.interval = interval
        self.file_count = file_count
        self.started = datetime.now()
        self.last_sync: datetime | None = None
        self.cycles = 0
        self.total_pushed = 0
        self.total_pulled = 0
        self.total_conflicts = 0
        self.log: deque[tuple[str, str, str, list[str]]] = deque(maxlen=MAX_LOG_ENTRIES)

    def render(self) -> Panel:
        """Build the full TUI layout."""
        # Header info
        header = Table.grid(padding=(0, 2))
        header.add_column(style="bold")
        header.add_column()
        header.add_row("Agent", self.agent_name)
        header.add_row("Files tracked", str(self.file_count))
        header.add_row("Sync interval", f"{self.interval}s")
        header.add_row("Uptime", self._uptime())

        # Stats
        stats = Table.grid(padding=(0, 2))
        stats.add_column(style="bold")
        stats.add_column()
        stats.add_row("Cycles", str(self.cycles))
        stats.add_row(
            "Pushed",
            f"[green]{self.total_pushed}[/green]" if self.total_pushed else "0",
        )
        stats.add_row(
            "Pulled", f"[blue]{self.total_pulled}[/blue]" if self.total_pulled else "0"
        )
        stats.add_row(
            "Conflicts",
            f"[yellow]{self.total_conflicts}[/yellow]" if self.total_conflicts else "0",
        )

        top = Columns([header, stats], padding=(0, 4))

        # Container info
        container_grid = Table.grid(padding=(0, 2))
        container_grid.add_column(style="bold")
        container_grid.add_column()
        container_grid.add_row("Name", f"[dim]{self.container_name}[/dim]")
        if self.container_id:
            container_grid.add_row("ID", f"[dim]{self.container_id}[/dim]")
        if self.container_status == "running":
            status_str = "[green]running[/green]"
        else:
            status_str = f"[red]{self.container_status}[/red]"
        container_grid.add_row("Status", status_str)

        # Activity log
        log_lines = Text()
        if not self.log:
            log_lines.append("  Waiting for changes...\n", style="dim italic")
        else:
            for ts, style, msg, files in reversed(self.log):
                log_lines.append(f"  {ts}  ", style="dim")
                log_lines.append(f"{msg}\n", style=style)
                for f in files:
                    log_lines.append("           ")
                    log_lines.append_text(Text.from_markup(f))
                    log_lines.append("\n")

        # Compose
        content = Text()
        content.append_text(Text.from_markup("[bold]Status[/bold]"))
        content.append("\n")
        # We need to use a group for mixed renderables
        from rich.console import Group

        group = Group(
            top,
            Text(""),
            Text.from_markup("[bold]Docker Environment[/bold]"),
            container_grid,
            Text(""),
            Text.from_markup("[bold]Activity[/bold]"),
            log_lines,
        )

        return Panel(
            group,
            title=f"[bold cyan]cinna dev[/bold cyan] — {self.agent_name}",
            subtitle="[dim]Ctrl+C to stop[/dim]",
            border_style="cyan",
            padding=(1, 2),
        )

    def _uptime(self) -> str:
        delta = datetime.now() - self.started
        mins, secs = divmod(int(delta.total_seconds()), 60)
        hours, mins = divmod(mins, 60)
        if hours:
            return f"{hours}h {mins}m"
        elif mins:
            return f"{mins}m {secs}s"
        return f"{secs}s"


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _local_credentials_hash(workspace: Path) -> str:
    """Hash the credentials files currently on disk."""
    creds_dir = workspace / "credentials"
    h = hashlib.sha256()
    if creds_dir.is_dir():
        for path in sorted(creds_dir.rglob("*")):
            if path.is_file() and not path.is_symlink():
                h.update(str(path.relative_to(creds_dir)).encode())
                h.update(path.read_bytes())
    return h.hexdigest()


def _sync_cycle(
    client: PlatformClient,
    config: CinnaConfig,
    workspace: Path,
    workspace_root: Path,
    last_manifest: dict,
    last_creds_hash: str,
) -> tuple[dict | None, str]:
    """Single sync cycle. Returns (stats dict or None, new credentials hash)."""
    local_manifest = compute_local_manifest(workspace)

    try:
        raw_remote = client.get_workspace_manifest(config.agent_id).get("files", {})
        remote_manifest = {
            path: info
            for path, info in raw_remote.items()
            if not _is_excluded(path, DEFAULT_EXCLUDES)
        }
    except Exception as e:
        logger.debug("Could not fetch remote manifest: %s", e)
        remote_manifest = last_manifest

    local_changed, remote_changed, conflicts = diff_manifests(
        local_manifest, remote_manifest, last_manifest
    )

    # Check for credential changes: pull from remote, compare local files before/after
    creds_changed = False
    try:
        pull_credentials(client, config, workspace_root, quiet=True)
        new_creds_hash = _local_credentials_hash(workspace)
        if new_creds_hash != last_creds_hash:
            last_creds_hash = new_creds_hash
            creds_changed = True
    except Exception as e:
        logger.debug("Credentials check failed: %s", e)

    if not local_changed and not remote_changed and not conflicts and not creds_changed:
        return None, last_creds_hash

    ts = _ts()
    log_entries = []

    # Push local changes
    if local_changed:
        files_to_push = [f for f in local_changed if f in local_manifest]
        if files_to_push:
            tarball = create_workspace_tarball(workspace, files_to_push)
            client.upload_workspace(config.agent_id, tarball)
        logger.info("Pushed %d files", len(local_changed))
        file_list = [f"[green]\u2191[/green] {f}" for f in local_changed[:5]]
        if len(local_changed) > 5:
            file_list.append(f"  ... +{len(local_changed) - 5} more")
        log_entries.append(
            (ts, "green", f"\u2191 {len(local_changed)} pushed", file_list)
        )

    # Pull remote changes
    if remote_changed:
        try:
            tarball = client.download_workspace(config.agent_id)
            extract_workspace_tarball(tarball, workspace, only_files=set(remote_changed))
            logger.info("Pulled %d files", len(remote_changed))
            file_list = [f"[blue]\u2193[/blue] {f}" for f in remote_changed[:5]]
            if len(remote_changed) > 5:
                file_list.append(f"  ... +{len(remote_changed) - 5} more")
            log_entries.append(
                (ts, "blue", f"\u2193 {len(remote_changed)} pulled", file_list)
            )
        except Exception as e:
            logger.warning("Pull failed: %s", e)
            log_entries.append((ts, "red", f"Pull failed: {e}", []))

    # Credentials
    if creds_changed:
        log_entries.append(
            (ts, "cyan", "\u2193 credentials updated", [])
        )

    # Conflicts
    if conflicts:
        file_list = [f"[yellow]!![/yellow] {f}" for f in conflicts[:5]]
        if len(conflicts) > 5:
            file_list.append(f"  ... +{len(conflicts) - 5} more")
        log_entries.append(
            (ts, "yellow", f"!! {len(conflicts)} conflicts (skipped)", file_list)
        )

    # Update manifest — recompute local hashes for pulled files so the manifest
    # reflects the actual extracted content (avoids race when remote files change
    # between manifest fetch and tarball download).
    post_pull_local = compute_local_manifest(workspace) if remote_changed else local_manifest

    merged = {**last_manifest}
    for f in local_changed:
        if f in local_manifest:
            merged[f] = local_manifest[f]
        elif f in merged:
            del merged[f]
    for f in remote_changed:
        if f in post_pull_local:
            merged[f] = post_pull_local[f]
        elif f in remote_manifest:
            merged[f] = remote_manifest[f]
        elif f in merged:
            del merged[f]
    # Update manifest for conflicted files to their current local state so that
    # the conflict does not repeat when only the remote side changes next cycle.
    for f in conflicts:
        if f in post_pull_local:
            merged[f] = post_pull_local[f]
        elif f in local_manifest:
            merged[f] = local_manifest[f]
    save_manifest(merged, workspace_root)

    return {
        "pushed": len(local_changed),
        "pulled": len(remote_changed),
        "conflicts": len(conflicts),
        "log_entries": log_entries,
    }, last_creds_hash
