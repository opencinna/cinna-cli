"""cinna CLI — local development for Cinna Core agents."""

import os
import platform
import shutil
import sys
from pathlib import Path

import click

from cinna import __version__
from cinna import console
from cinna import sync_session
from cinna.client import PlatformClient
from cinna.config import (
    find_workspace_root,
    list_agent_registry,
    load_config,
    remove_agent_registry,
)
from cinna.mcp_proxy import run_mcp_proxy
from cinna.mutagen_runtime import ensure_mutagen_ready


@click.group()
@click.version_option(version=__version__)
@click.option("-v", "--verbose", is_flag=True, help="Show debug logs in terminal")
def cli(verbose: bool):
    """Local development CLI for Cinna Core agents."""
    from cinna.logging import setup_logging

    setup_logging(verbose=verbose)


# ─── setup ─────────────────────────────────────────────────────────────────


@cli.command(context_settings={"ignore_unknown_options": True})
@click.argument("setup_input", nargs=-1, type=click.UNPROCESSED, required=True)
@click.option(
    "--name",
    default=None,
    help="Name for this development session",
)
def setup(setup_input: tuple[str, ...], name: str | None):
    """Set up local development environment for an agent.

    Accepts any of these formats (paste directly from the platform UI):

    \b
      cinna setup curl -sL http://host/api/cli-setup/TOKEN | python3 -
      cinna setup http://host/api/cli-setup/TOKEN
      cinna setup TOKEN
    """
    from cinna.bootstrap import run_setup

    if name is None:
        default_name = _default_machine_name()
        if sys.stdin.isatty():
            name = click.prompt("Machine name", default=default_name)
        else:
            name = default_name

    run_setup(" ".join(setup_input), name)


# ─── set-token ─────────────────────────────────────────────────────────────


@cli.command(name="set-token", context_settings={"ignore_unknown_options": True})
@click.argument("setup_input", nargs=-1, type=click.UNPROCESSED, required=True)
@click.option(
    "--name",
    default=None,
    help="Machine name to register with the refreshed token",
)
def set_token(setup_input: tuple[str, ...], name: str | None):
    """Refresh the CLI token on the current workspace.

    Useful when the stored token has expired — swaps ``cli_token`` in
    ``.cinna/config.json`` and ``~/.cinna/agents.json`` in place, without
    re-cloning the workspace or regenerating context files. Must be run from
    inside an existing cinna workspace, and the token must belong to the same
    agent.

    Accepts any of these formats (paste directly from the platform UI):

    \b
      cinna set-token curl -sL http://host/api/cli-setup/TOKEN | python3 -
      cinna set-token http://host/api/cli-setup/TOKEN
      cinna set-token TOKEN
    """
    from cinna.bootstrap import run_set_token

    if name is None:
        default_name = _default_machine_name()
        if sys.stdin.isatty():
            name = click.prompt("Machine name", default=default_name)
        else:
            name = default_name

    run_set_token(" ".join(setup_input), name)


# ─── exec ──────────────────────────────────────────────────────────────────


@cli.command(name="exec", context_settings={"ignore_unknown_options": True})
@click.argument("command", nargs=-1, required=True)
def exec_cmd(command: tuple[str, ...]):
    """Run a command in the remote agent environment.

    Output streams back in real time via the platform. Exit code matches the
    remote process's exit code. Ctrl+C aborts the stream.

    Examples:
      cinna exec python scripts/main.py
      cinna exec pip install pandas
      cinna exec bash -c 'ls -la'
    """
    root = find_workspace_root()
    config = load_config(root)

    exit_code = _run_remote_exec(config, " ".join(command))
    sys.exit(exit_code)


def _run_remote_exec(config, command_str: str) -> int:
    """Drive the /exec SSE stream and mirror events to the local terminal."""
    exit_code = 0
    with PlatformClient(config) as client:
        try:
            for event in client.stream_exec(config.agent_id, command_str):
                etype = event.get("type")
                if etype == "exec_id":
                    # First event — nothing to print. Remember it in case we
                    # later ship an /exec-interrupt endpoint.
                    continue
                if etype == "tool_result_delta":
                    chunk = event.get("content", "")
                    stream = event.get("metadata", {}).get("stream", "stdout")
                    target = sys.stderr if stream == "stderr" else sys.stdout
                    target.write(chunk)
                    target.flush()
                elif etype == "done":
                    exit_code = int(event.get("exit_code", 0))
                elif etype == "interrupted":
                    exit_code = int(event.get("exit_code", 130))
                elif etype == "error":
                    console.error(event.get("content", "unknown error"))
                    exit_code = 1
        except KeyboardInterrupt:
            exit_code = 130
    return exit_code


# ─── status ────────────────────────────────────────────────────────────────


@cli.command()
def status():
    """Show agent info and current sync state."""
    root = find_workspace_root()
    config = load_config(root)

    from rich.table import Table

    st = sync_session.status(config)

    with console.spinner("Checking token..."):
        token_status = _probe_token_statuses(
            [
                {
                    "agent_id": config.agent_id,
                    "platform_url": config.platform_url,
                    "cli_token": config.cli_token,
                }
            ]
        ).get(config.agent_id, "unknown")

    table = Table(title=f"Agent: {config.agent_name}")
    table.add_column("Property", style="dim")
    table.add_column("Value")
    table.add_row("Platform", config.platform_url)
    table.add_row("Agent ID", config.agent_id)
    table.add_row("Template", config.template)
    table.add_row("Mutagen", config.mutagen_version or "—")
    table.add_row("Sync state", _colored_state(st.state))
    table.add_row("Token", _format_token_label(token_status))
    table.add_row("Pending → remote", str(st.pending_to_remote))
    table.add_row("Pending → local", str(st.pending_to_local))
    table.add_row("Conflicts", str(st.conflict_count))
    if st.last_error:
        table.add_row("Last error", f"[red]{st.last_error}[/red]")

    console.console.print(table)


def _colored_state(state: str) -> str:
    if state == "connected":
        return "[green]connected[/green]"
    if state == "paused":
        return "[yellow]paused[/yellow]"
    if state in {"error", "missing"}:
        return f"[red]{state}[/red]"
    return state


# ─── sync group ────────────────────────────────────────────────────────────


@cli.command("list")
def list_cmd():
    """List every agent registered on this machine.

    Reads ``~/.cinna/agents.json`` — the same registry the SSH shim uses to
    resolve per-agent credentials. For each agent the table shows agent ID,
    the web UI link, workspace path, current sync state, and whether the
    stored CLI token is still accepted by the backend. Workspace directories
    that no longer exist are flagged as missing (they can be cleaned up with
    ``cinna disconnect`` from the parent directory).
    """
    from rich.table import Table

    entries = list_agent_registry()
    if not entries:
        console.status(
            "No agents registered yet. Run the setup curl command to register one."
        )
        return

    # Cheap one-shot lookup: index Mutagen sessions by session name so we can
    # report per-agent sync state without a daemon round-trip per row.
    # Fails silently if the daemon isn't running — sync just reads "–".
    sessions_by_name: dict[str, dict] = {}
    try:
        # sync_session._list_sessions needs a CinnaConfig for env vars. Build
        # a throwaway one off the first entry; MUTAGEN_SSH_PATH is the only
        # env var that matters for `sync list` and it's the same for every
        # agent on this machine.
        from cinna.sync_session import _list_sessions, CinnaConfig as _Cfg

        probe_entry = entries[0]
        probe = _Cfg(
            platform_url=probe_entry.get("platform_url", ""),
            cli_token=probe_entry.get("cli_token", ""),
            agent_id=probe_entry["agent_id"],
            agent_name="",
            environment_id="",
            template="",
        )
        for s in _list_sessions(probe):
            name = s.get("name")
            if name:
                sessions_by_name[name] = s
    except Exception:
        pass

    with console.spinner("Checking tokens..."):
        token_statuses = _probe_token_statuses(entries)

    table = Table(
        title=f"Registered agents ({len(entries)})",
        title_style="bold",
        show_lines=True,
    )
    table.add_column("#", style="dim", justify="right")
    table.add_column("Agent")
    table.add_column("Location")
    table.add_column("Sync")

    for i, entry in enumerate(entries, 1):
        agent_id = entry["agent_id"]
        platform_url = entry.get("platform_url", "")
        frontend_url = entry.get("frontend_url") or platform_url
        workspace_path = Path(entry.get("workspace_path", ""))

        # Default display = short agent_id; enrich with the agent's display
        # name if the workspace's .cinna/config.json is still intact.
        display_name = agent_id[:8]
        if workspace_path and workspace_path.exists():
            ws_display = str(workspace_path)
            try:
                cfg = load_config(workspace_path)
                display_name = cfg.agent_name
            except Exception:
                pass
        else:
            ws_display = f"[red]missing:[/red] {workspace_path or '?'}"

        agent_link = (
            f"{frontend_url.rstrip('/')}/agent/{agent_id}" if frontend_url else "?"
        )
        sync_cell = _format_sync_cell(
            agent_id, sessions_by_name, token_statuses.get(agent_id, "unknown")
        )

        agent_cell = f"[bold]{display_name}[/bold]\n[dim]{agent_id}[/dim]"
        location_cell = f"{ws_display}\n[dim]{agent_link}[/dim]"

        table.add_row(
            str(i),
            agent_cell,
            location_cell,
            sync_cell,
        )

    console.console.print(table)


def _probe_token_statuses(entries: list[dict]) -> dict[str, str]:
    """Check each agent's backend in parallel and classify the CLI token.

    Returns a mapping ``agent_id -> status`` where status is one of:
      - ``valid``       — backend answered 2xx
      - ``expired``     — backend answered 401
      - ``unreachable`` — connection/timeout/other error
    """
    from concurrent.futures import ThreadPoolExecutor

    def probe(entry: dict) -> tuple[str, str]:
        agent_id = entry["agent_id"]
        platform_url = (entry.get("platform_url") or "").rstrip("/")
        cli_token = entry.get("cli_token") or ""
        if not platform_url or not cli_token:
            return agent_id, "unreachable"
        try:
            import httpx

            response = httpx.get(
                f"{platform_url}/api/v1/cli/agents/{agent_id}/sync-runtime",
                headers={"Authorization": f"Bearer {cli_token}"},
                timeout=httpx.Timeout(5.0, connect=3.0),
                follow_redirects=True,
            )
        except Exception:
            return agent_id, "unreachable"
        if response.status_code == 401:
            return agent_id, "expired"
        if 200 <= response.status_code < 300:
            return agent_id, "valid"
        return agent_id, "unreachable"

    results: dict[str, str] = {}
    max_workers = min(8, max(1, len(entries)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for agent_id, status in pool.map(probe, entries):
            results[agent_id] = status
    return results


def _format_sync_cell(
    agent_id: str,
    sessions_by_name: dict[str, dict],
    token_status: str = "unknown",
) -> str:
    """Render the Sync column for one row.

    Top line is the Mutagen session state (running / paused / error / idle);
    bottom line reports whether the stored CLI token is still accepted by the
    backend.
    """
    from cinna.sync_session import session_name

    session = sessions_by_name.get(session_name(agent_id))
    if session is None:
        sync_label = "[dim]–[/dim]"
    elif session.get("paused"):
        sync_label = "[yellow]paused[/yellow]"
    elif session.get("lastError"):
        sync_label = "[red]error[/red]"
    else:
        alpha_conn = bool((session.get("alpha") or {}).get("connected"))
        beta_conn = bool((session.get("beta") or {}).get("connected"))
        if alpha_conn and beta_conn:
            sync_label = "[green]active[/green]"
        else:
            sync_label = "[yellow]connecting[/yellow]"

    token_label = _format_token_label(token_status)
    return f"{sync_label}\n{token_label}"


def _format_token_label(status: str) -> str:
    if status == "valid":
        return "[green]valid token[/green]"
    if status == "expired":
        return "[red]expired token[/red]"
    if status == "unreachable":
        return "[yellow]no connection[/yellow]"
    return "[dim]–[/dim]"


@cli.command()
def dev():
    """Start a foreground dev session: live workspace sync + TUI.

    Creates the Mutagen sync session for this agent and attaches the terminal
    to a two-tab TUI (status + raw Mutagen details). Ctrl-C terminates the
    session — sync does not outlive the TUI. To observe sync from another
    terminal without affecting it, use ``cinna sync status``.
    """
    root = find_workspace_root()
    config = load_config(root)

    with PlatformClient(config) as client:
        ensure_mutagen_ready(client, config, root, interactive=sys.stdin.isatty())

    st = sync_session.start(config, root)
    console.status(f"Sync session created ({st.state}) — attaching live view. Press Ctrl-C to stop.")
    sync_session.run_foreground(config)
    console.status("Sync session terminated.")


@cli.group()
def sync():
    """Inspect the continuous workspace sync session.

    Use ``cinna dev`` to start sync. These subcommands are read-only views —
    safe to run from another terminal while a dev session is live.
    """


@sync.command("status")
def sync_status():
    """Print the sync session state."""
    root = find_workspace_root()
    config = load_config(root)

    st = sync_session.status(config)
    from rich.table import Table

    table = Table(title=f"Sync — {config.agent_name}")
    table.add_column("Property", style="dim")
    table.add_column("Value")
    table.add_row("Session", st.session_name)
    table.add_row("State", _colored_state(st.state))
    table.add_row("Pending → remote", str(st.pending_to_remote))
    table.add_row("Pending → local", str(st.pending_to_local))
    table.add_row("Conflicts", str(st.conflict_count))
    if st.last_error:
        table.add_row("Last error", f"[red]{st.last_error}[/red]")
    console.console.print(table)


@sync.command("conflicts")
def sync_conflicts():
    """List sync conflicts Mutagen has surfaced."""
    root = find_workspace_root()
    config = load_config(root)

    conflicts = sync_session.list_conflicts(config, root)
    if not conflicts:
        console.status("No conflicts.")
        return

    from rich.table import Table

    table = Table(title=f"Conflicts ({len(conflicts)})")
    table.add_column("#", style="dim", justify="right")
    table.add_column("Path")
    table.add_column("Side", style="dim")
    for i, c in enumerate(conflicts, 1):
        rel = c.path.relative_to(root)
        table.add_row(str(i), str(rel), c.kind)
    console.console.print(table)
    console.console.print(
        "\nResolve by opening the file(s) in your editor, picking the keeper,"
        " and deleting the .conflict.* copy."
    )


# ─── disconnect ────────────────────────────────────────────────────────────


@cli.command()
def disconnect():
    """Stop sync and remove local config (workspace files preserved)."""
    root = find_workspace_root()
    config = load_config(root)

    console.warn(
        "This will stop sync, remove .cinna/ config, and delete generated files."
    )
    console.console.print("Workspace files will be preserved.")
    if not click.confirm("Continue?"):
        raise click.Abort()

    try:
        sync_session.stop(config)
    except Exception as exc:
        console.warn(f"Could not stop sync session cleanly: {exc}")

    remove_agent_registry(config.agent_id)

    from cinna.context import list_synced_prompt_refs

    synced_refs = list_synced_prompt_refs(root)

    shutil.rmtree(root / ".cinna", ignore_errors=True)

    for f in [
        "CLAUDE.md",
        "BUILDING_AGENT.md",
        ".mcp.json",
        "opencode.json",
        "cinna.log",
        "mutagen.yml",
        *synced_refs,
    ]:
        p = root / f
        if p.exists():
            p.unlink()

    console.status("Disconnected. Workspace files preserved.")


@cli.command(name="disconnect-all")
def disconnect_all():
    """Remove all agent workspaces in the current directory.

    Scans subdirectories for cinna workspaces (.cinna/config.json), stops each
    sync session, and deletes the directories entirely.
    """
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    cwd = Path.cwd()
    agents: list[tuple[Path, object | None]] = []
    for child in sorted(cwd.iterdir()):
        if child.is_dir() and (child / ".cinna" / "config.json").is_file():
            try:
                agents.append((child, load_config(child)))
            except Exception:
                agents.append((child, None))

    if not agents:
        console.status("No cinna workspaces found in current directory.")
        return

    table = Table(
        title=f"Found {len(agents)} workspace{'s' if len(agents) != 1 else ''}",
        border_style="yellow",
        title_style="bold yellow",
    )
    table.add_column("#", style="dim", justify="right")
    table.add_column("Directory", style="bold")
    table.add_column("Agent")

    for i, (ws_dir, config) in enumerate(agents, 1):
        name = config.agent_name if config else "[dim]unknown[/dim]"
        table.add_row(str(i), f"{ws_dir.name}/", name)

    console.console.print()
    console.console.print(table)
    console.console.print()

    warning = Text()
    warning.append("  This will ", style="yellow")
    warning.append("stop all sync sessions", style="bold red")
    warning.append(" and ", style="yellow")
    warning.append("delete all directories", style="bold red")
    warning.append(" listed above.", style="yellow")
    console.console.print(
        Panel(
            warning,
            border_style="red",
            title="[bold red]Warning[/bold red]",
            padding=(0, 1),
        )
    )
    console.console.print()

    if not click.confirm("Are you sure?"):
        raise click.Abort()

    console.console.print()

    results: list[tuple[str, str, str]] = []  # (label, phase, result)

    with console.file_progress() as progress:
        task = progress.add_task("Cleaning up workspaces...", total=len(agents) * 2)

        for ws_dir, config in agents:
            label = config.agent_name if config else ws_dir.name

            progress.update(task, description=f"Stopping sync — {label}")
            if config is not None:
                try:
                    sync_session.stop(config)
                    remove_agent_registry(config.agent_id)
                    results.append((label, "Sync", "stopped"))
                except Exception as e:
                    results.append((label, "Sync", f"failed: {e}"))
            else:
                results.append((label, "Sync", "skipped (no config)"))
            progress.advance(task)

            progress.update(task, description=f"Deleting directory — {label}")
            try:
                shutil.rmtree(ws_dir)
                results.append((label, "Directory", "deleted"))
            except Exception as e:
                results.append((label, "Directory", f"failed: {e}"))
            progress.advance(task)

    log_file = cwd / "cinna.log"
    if log_file.exists():
        log_file.unlink()

    console.console.print()
    summary = Table(title="Results", border_style="green", title_style="bold green")
    summary.add_column("Agent", style="bold")
    summary.add_column("Action")
    summary.add_column("Result")

    for label, phase, result in results:
        if "failed" in result:
            result_styled = f"[red]{result}[/red]"
        else:
            result_styled = f"[green]{result}[/green]"
        summary.add_row(label, phase, result_styled)

    console.console.print(summary)
    console.console.print()
    console.status("All agent workspaces cleaned up.")


# ─── completion (unchanged) ────────────────────────────────────────────────


@cli.command()
@click.argument(
    "shell", required=False, type=click.Choice(["bash", "zsh", "fish"]), default=None
)
@click.option("--install", is_flag=True, help="Install completion to your shell config")
def completion(shell: str | None, install: bool):
    """Output shell completion script.

    \b
      cinna completion zsh          # print script to stdout
      cinna completion --install    # auto-detect shell and install
      eval "$(cinna completion zsh)" # activate in current session
    """
    import subprocess as sp

    if shell is None:
        shell = _detect_shell()

    env_var = "_CINNA_COMPLETE"
    source_cmd = f"{shell}_source"

    if install:
        result = sp.run(
            ["cinna"],
            capture_output=True,
            text=True,
            env={**os.environ, env_var: source_cmd},
        )
        script = result.stdout.strip()
        if not script:
            raise click.ClickException("Failed to generate completion script.")

        rc_file, snippet = _install_target(shell, script)
        rc = Path(rc_file).expanduser()

        if rc.exists() and "cinna completion" in rc.read_text():
            console.status(f"Completion already installed in {rc_file}")
            return

        with open(rc, "a") as f:
            f.write(f"\n# cinna CLI completion\n{snippet}\n")
        console.status(f"Completion installed in {rc_file}. Restart your shell or run:")
        console.console.print(f"  source {rc_file}")
    else:
        result = sp.run(
            ["cinna"],
            capture_output=True,
            text=True,
            env={**os.environ, env_var: source_cmd},
        )
        click.echo(result.stdout)


@cli.command(name="mcp-proxy", hidden=True)
def mcp_proxy():
    """Run MCP stdio server for knowledge queries. Called by Claude Code, not directly."""
    run_mcp_proxy()


def _detect_shell() -> str:
    """Detect current shell from SHELL env var."""
    shell_path = os.environ.get("SHELL", "")
    for name in ("zsh", "bash", "fish"):
        if name in shell_path:
            return name
    return "bash"


def _install_target(shell: str, script: str) -> tuple[str, str]:
    """Return (rc_file, snippet_to_append) for each shell type."""
    if shell == "zsh":
        return "~/.zshrc", 'eval "$(_CINNA_COMPLETE=zsh_source cinna)"'
    elif shell == "fish":
        return (
            "~/.config/fish/completions/cinna.fish",
            script,
        )
    else:
        return "~/.bashrc", 'eval "$(_CINNA_COMPLETE=bash_source cinna)"'


def _default_machine_name() -> str:
    return f"{os.environ.get('USER', 'dev')}'s {platform.node()}"
