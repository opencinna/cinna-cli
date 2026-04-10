"""cinna CLI — local development for Cinna Core agents."""

import os
import platform
import shutil
import sys
from pathlib import Path

import click

from cinna import __version__
from cinna.config import find_workspace_root, load_config
from cinna.client import PlatformClient
from cinna.docker import (
    exec_in_container,
    build_container,
    start_container,
    destroy_container,
    remove_images,
    is_container_running,
    get_container_status,
)
from cinna.sync import push_workspace, pull_workspace, pull_credentials
from cinna.mcp_proxy import run_mcp_proxy
from cinna import console


@click.group()
@click.version_option(version=__version__)
@click.option("-v", "--verbose", is_flag=True, help="Show debug logs in terminal")
def cli(verbose: bool):
    """Local development CLI for Cinna Core agents."""
    from cinna.logging import setup_logging

    setup_logging(verbose=verbose)


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
      cinna setup curl -sL http://host/cli-setup/TOKEN | python3 -
      cinna setup http://host/cli-setup/TOKEN
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


@cli.command()
@click.option(
    "--interval",
    default=5,
    type=int,
    show_default=True,
    help="Seconds between sync checks",
)
def dev(interval: int):
    """Start dev mode: start container, watch and sync, stop on exit.

    Starts the Docker container, monitors the workspace directory, and
    bidirectionally syncs with the remote environment. The container is
    removed when you press Ctrl+C.
    """
    from cinna.dev import run_dev_loop

    root = find_workspace_root()
    config = load_config(root)
    run_dev_loop(config, root, interval=interval)


@cli.command(name="exec", context_settings={"ignore_unknown_options": True})
@click.argument("command", nargs=-1, required=True)
def exec_cmd(command: tuple[str, ...]):
    """Run a command inside the agent container.

    Examples:
      cinna exec python scripts/main.py
      cinna exec pip install pandas
      cinna exec bash
    """
    root = find_workspace_root()
    config = load_config(root)
    exit_code = exec_in_container(config, list(command), root)
    sys.exit(exit_code)


@cli.command()
@click.option("--force", is_flag=True, help="Overwrite remote files on conflict")
def push(force: bool):
    """Push local workspace changes to the remote environment."""
    root = find_workspace_root()
    config = load_config(root)
    with PlatformClient(config) as client:
        push_workspace(client, config, root, force=force)


@cli.command()
@click.option("--force", is_flag=True, help="Overwrite local files on conflict")
def pull(force: bool):
    """Pull remote workspace changes and refresh context."""
    root = find_workspace_root()
    config = load_config(root)
    with PlatformClient(config) as client:
        pull_workspace(client, config, root, force=force)


@cli.command()
def status():
    """Show local container and remote environment status."""
    root = find_workspace_root()
    config = load_config(root)

    container = get_container_status(config, root)

    from rich.table import Table

    table = Table(title=f"Agent: {config.agent_name}")
    table.add_column("Property", style="dim")
    table.add_column("Value")
    table.add_row("Platform", config.platform_url)
    table.add_row("Agent ID", config.agent_id)
    table.add_row("Template", config.template)
    table.add_row("Container", container.get("name", "") or config.container_name)
    table.add_row(
        "Status",
        "[green]running[/green]"
        if container.get("running")
        else f"[red]{container.get('status', 'unknown')}[/red]",
    )
    table.add_row("Image", container.get("image", "\u2014"))

    console.console.print(table)


@cli.command()
@click.option("--no-cache", is_flag=True, help="Force clean rebuild")
def rebuild(no_cache: bool):
    """Rebuild the local container image (after changing requirements)."""
    root = find_workspace_root()
    load_config(root)  # validate config exists

    console.step(1, 3, "Removing container...")
    destroy_container(root)

    console.step(2, 3, "Building image...")
    build_container(root, no_cache=no_cache)

    console.step(3, 3, "Starting container and installing workspace packages...")
    start_container(root)

    console.status("Rebuild complete. Container is running.")


@cli.command(name="env-up")
def env_up():
    """Start the agent container in the background.

    Use this when you need the container running without dev mode
    (e.g. for 'cinna exec'). Stop with 'cinna env-down'.
    """
    root = find_workspace_root()
    load_config(root)  # validate config exists

    if is_container_running(root):
        console.status("Container is already running.")
        return

    start_container(root)
    console.status("Container started. Stop with 'cinna env-down'.")


@cli.command(name="env-down")
def env_down():
    """Stop and remove the agent container."""
    root = find_workspace_root()
    load_config(root)  # validate config exists

    if not is_container_running(root):
        console.status("Container is not running.")
        return

    destroy_container(root)
    console.status("Container removed.")


@cli.command()
def credentials():
    """Re-pull credentials from the platform."""
    root = find_workspace_root()
    config = load_config(root)
    with PlatformClient(config) as client:
        pull_credentials(client, config, root)


@cli.command()
@click.option(
    "--keep-image", is_flag=True, help="Keep the Docker image (only remove container)"
)
def disconnect(keep_image: bool):
    """Stop container, remove Docker image, and remove local config (keeps workspace files)."""
    root = find_workspace_root()
    load_config(root)  # validate config exists before proceeding

    console.warn(
        "This will stop the container, remove the Docker image, and remove .cinna/ config."
    )
    console.console.print("Workspace files will be preserved.")
    if not click.confirm("Continue?"):
        raise click.Abort()

    if keep_image:
        destroy_container(root)
    else:
        remove_images(root)

    shutil.rmtree(root / ".cinna")

    # Clean up generated files
    for f in [
        "CLAUDE.md",
        "BUILDING_AGENT.md",
        ".mcp.json",
        "opencode.json",
        "cinna.log",
    ]:
        p = root / f
        if p.exists():
            p.unlink()

    console.status("Disconnected. Workspace files preserved.")


@cli.command(name="disconnect-all")
def disconnect_all():
    """Remove all agent workspaces in the current directory.

    Scans subdirectories for cinna workspaces (.cinna/config.json),
    stops containers, removes Docker images, and deletes the directories entirely.
    """
    from cinna.config import CONFIG_DIR, CONFIG_FILE
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text

    cwd = Path.cwd()
    agents = []
    for child in sorted(cwd.iterdir()):
        if child.is_dir() and (child / CONFIG_DIR / CONFIG_FILE).is_file():
            try:
                config = load_config(child)
                agents.append((child, config))
            except Exception:
                agents.append((child, None))

    if not agents:
        console.status("No cinna workspaces found in current directory.")
        return

    # Build a table of discovered workspaces
    table = Table(
        title=f"Found {len(agents)} workspace{'s' if len(agents) != 1 else ''}",
        border_style="yellow",
        title_style="bold yellow",
    )
    table.add_column("#", style="dim", justify="right")
    table.add_column("Directory", style="bold")
    table.add_column("Agent")
    table.add_column("Container", style="dim")
    table.add_column("Status")

    for i, (ws_dir, config) in enumerate(agents, 1):
        name = config.agent_name if config else "[dim]unknown[/dim]"
        try:
            cs = get_container_status(config, ws_dir) if config else {}
            container = cs.get("name", "") or (config.container_name if config else "[dim]unknown[/dim]")
            if cs.get("running"):
                status_text = "[green]running[/green]"
            elif cs.get("status"):
                status_text = f"[red]{cs['status']}[/red]"
            else:
                status_text = "[dim]—[/dim]"
        except Exception:
            container = config.container_name if config else "[dim]unknown[/dim]"
            status_text = "[dim]—[/dim]"
        table.add_row(str(i), f"{ws_dir.name}/", name, container, status_text)

    console.console.print()
    console.console.print(table)
    console.console.print()

    warning = Text()
    warning.append("  This will ", style="yellow")
    warning.append("stop all containers", style="bold red")
    warning.append(", ", style="yellow")
    warning.append("remove Docker images", style="bold red")
    warning.append(", and ", style="yellow")
    warning.append("delete all directories", style="bold red")
    warning.append(" listed above.", style="yellow")
    console.console.print(
        Panel(warning, border_style="red", title="[bold red]Warning[/bold red]", padding=(0, 1))
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

            # Phase 1: Docker cleanup
            progress.update(task, description=f"Removing containers — {label}")
            try:
                remove_images(ws_dir)
                results.append((label, "Docker", "removed"))
            except Exception as e:
                results.append((label, "Docker", f"failed: {e}"))
            progress.advance(task)

            # Phase 2: Delete directory
            progress.update(task, description=f"Deleting directory — {label}")
            try:
                shutil.rmtree(ws_dir)
                results.append((label, "Directory", "deleted"))
            except Exception as e:
                results.append((label, "Directory", f"failed: {e}"))
            progress.advance(task)

    # Clean up cinna.log in current directory
    log_file = cwd / "cinna.log"
    if log_file.exists():
        log_file.unlink()

    # Summary table
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
        # Generate the script
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

        # Check if already installed
        if rc.exists() and "cinna completion" in rc.read_text():
            console.status(f"Completion already installed in {rc_file}")
            return

        with open(rc, "a") as f:
            f.write(f"\n# cinna CLI completion\n{snippet}\n")
        console.status(f"Completion installed in {rc_file}. Restart your shell or run:")
        console.console.print(f"  source {rc_file}")
    else:
        # Just print the script
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
            script,  # fish uses the script directly as the file
        )
    else:
        return "~/.bashrc", 'eval "$(_CINNA_COMPLETE=bash_source cinna)"'


def _default_machine_name() -> str:
    return f"{os.environ.get('USER', 'dev')}'s {platform.node()}"
