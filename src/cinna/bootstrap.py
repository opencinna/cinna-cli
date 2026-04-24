"""Setup flow: exchange token, install Mutagen, clone workspace, start sync."""

import logging
import os
import platform
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import click
import httpx

from cinna.config import (
    CinnaConfig,
    KnowledgeSource,
    find_workspace_root,
    load_config,
    save_config,
    upsert_agent_registry,
    workspace_dir,
)
from cinna.client import PlatformClient
from cinna.sync import extract_workspace_tarball, ensure_workspace_dirs
from cinna.mutagen_runtime import ensure_mutagen_ready
from cinna import sync_session
from cinna.context import (
    generate_context_files,
    generate_mcp_json,
    generate_opencode_json,
    generate_gitignore,
)
from cinna import console

logger = logging.getLogger("cinna.bootstrap")


def parse_setup_input(
    raw_input: str, fallback_platform_url: str | None = None
) -> tuple[str, str]:
    """Parse setup input into (platform_url, token).

    Accepts any of:
      - Full curl command: 'curl -sL http://host:8000/cli-setup/TOKEN | python3 -'
      - URL:               'http://host:8000/cli-setup/TOKEN'
      - Raw token:         'TOKEN' (falls back to ``fallback_platform_url`` or
                            the ``CINNA_PLATFORM_URL`` env var — in that order)

    Returns (platform_url, token).
    """
    text = raw_input.strip().strip("'\"")

    url_match = re.search(r"(https?://[^\s]+/cli-setup/[^\s|\"']+)", text)
    if url_match:
        url = url_match.group(1)
        parsed = urlparse(url)
        path_parts = parsed.path.rstrip("/").split("/cli-setup/")
        if len(path_parts) == 2 and path_parts[1]:
            token = path_parts[1]
            prefix = path_parts[0]
            platform_url = f"{parsed.scheme}://{parsed.netloc}{prefix}"
            return platform_url, token

    if text.startswith("http://") or text.startswith("https://") or "curl" in text:
        raise click.ClickException(
            "Could not parse setup URL from input. Expected a URL containing /cli-setup/TOKEN."
        )

    platform_url = fallback_platform_url or os.environ.get("CINNA_PLATFORM_URL", "")
    if not platform_url:
        raise click.ClickException(
            "Cannot determine platform URL from the provided token.\n"
            "Either paste the full curl command / URL from the platform UI,\n"
            "or set the CINNA_PLATFORM_URL environment variable."
        )
    return platform_url, text


def normalize_agent_dir_name(name: str) -> str:
    """Normalize agent name to a lowercase, dash-separated directory name."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "agent"


def _exchange_setup_token(
    platform_url: str, token: str, machine_name: str
) -> dict:
    """POST /cli-setup/{token} and return the decoded payload.

    Wraps the HTTP call in a uniform ClickException on failure so both
    `setup` and `set-token` report errors the same way.
    """
    setup_url = f"{platform_url.rstrip('/')}/cli-setup/{token}"
    machine_info = f"{platform.system()}/{platform.machine()}"
    logger.info("Exchanging setup token at %s", setup_url)

    response = httpx.post(
        setup_url,
        json={"machine_name": machine_name, "machine_info": machine_info},
        timeout=30.0,
    )
    logger.debug("Setup response: %s %s", response.status_code, response.text[:500])
    if response.status_code != 200:
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text
        raise click.ClickException(f"Setup failed: {detail}")
    return response.json()


def run_set_token(setup_input: str, machine_name: str) -> None:
    """Replace the CLI token on an existing workspace without rebuilding.

    Called by `cinna set-token <token_or_url>`. Accepts the same input forms
    as `cinna setup`. Verifies the exchanged token belongs to the agent this
    workspace is already bound to before writing it.
    """
    root = find_workspace_root()
    config = load_config(root)

    # ``config.platform_url`` is stored as the bare host (PlatformClient adds
    # ``/api/...`` itself). The cli-setup endpoint lives under ``/api``, so
    # append it here before handing to parse_setup_input as a fallback.
    stored_base = config.platform_url.rstrip("/")
    fallback = stored_base if stored_base.endswith("/api") else f"{stored_base}/api"
    platform_url, token = parse_setup_input(
        setup_input, fallback_platform_url=fallback
    )
    payload = _exchange_setup_token(platform_url, token, machine_name)

    new_agent_id = payload["agent"]["id"]
    if new_agent_id != config.agent_id:
        raise click.ClickException(
            f"Token belongs to a different agent ({new_agent_id}) than this "
            f"workspace ({config.agent_id}). Run 'cinna setup' in a new "
            f"directory to register it."
        )

    config.cli_token = payload["cli_token"]
    config.platform_url = payload["platform_url"]
    if payload.get("frontend_url"):
        config.frontend_url = payload["frontend_url"]
    save_config(config, root)
    upsert_agent_registry(
        config.agent_id,
        config.platform_url,
        config.cli_token,
        root,
        frontend_url=config.frontend_url,
    )
    console.status(f"Token refreshed for agent: {config.agent_name}")


def run_setup(setup_input: str, machine_name: str) -> None:
    """Full setup flow — called by `cinna setup <token_or_url>`."""
    total = 5

    # Step 1: Authenticate
    console.step(1, total, "Authenticating...")

    platform_url, token = parse_setup_input(setup_input)
    payload = _exchange_setup_token(platform_url, token, machine_name)
    agent_info = payload["agent"]
    agent_name = agent_info["name"]
    dir_name = normalize_agent_dir_name(agent_name)
    logger.info("Agent: %s (dir: %s)", agent_name, dir_name)

    workspace_root = Path.cwd() / dir_name
    if (workspace_root / ".cinna" / "config.json").exists():
        raise click.ClickException(
            f"Directory '{dir_name}/' already contains a cinna workspace.\n"
            f"Remove it first with 'cinna disconnect' or delete the directory."
        )
    workspace_root.mkdir(exist_ok=True)

    config = CinnaConfig(
        platform_url=payload["platform_url"],
        cli_token=payload["cli_token"],
        agent_id=agent_info["id"],
        agent_name=agent_name,
        environment_id=agent_info["environment_id"],
        template=agent_info["template"],
        frontend_url=payload.get("frontend_url"),
        knowledge_sources=[
            KnowledgeSource(**ks) for ks in payload.get("knowledge_sources", [])
        ],
    )
    save_config(config, workspace_root)
    upsert_agent_registry(
        config.agent_id,
        config.platform_url,
        config.cli_token,
        workspace_root,
        frontend_url=config.frontend_url,
    )
    console.status(f"Authenticated as agent: {agent_name}")

    client = PlatformClient(config)
    try:
        # Step 2: Mutagen
        console.step(2, total, "Checking Mutagen install...")
        ensure_mutagen_ready(
            client, config, workspace_root, interactive=sys.stdin.isatty()
        )
        console.status(f"Mutagen ready (version {config.mutagen_version})")

        # Step 3: Initial clone
        console.step(3, total, "Cloning workspace...")
        ws_dir = workspace_dir(workspace_root)
        ws_dir.mkdir(exist_ok=True)
        try:
            logger.info("Downloading workspace for agent %s", config.agent_id)
            ws_tarball = client.download_workspace(config.agent_id)
            logger.info("Workspace downloaded (%d bytes)", len(ws_tarball))
            extract_workspace_tarball(ws_tarball, ws_dir)
            console.status("Workspace cloned")
        except Exception as e:
            logger.warning("Workspace download failed: %s", e)
            console.warn(f"Workspace download failed: {e}")
            console.warn("Mutagen will reconcile on first sync start.")
        ensure_workspace_dirs(ws_dir)

        # Step 4: Context files + MCP config
        console.step(4, total, "Configuring development environment...")
        try:
            building_ctx = client.get_building_context(config.agent_id)
            generate_context_files(building_ctx, config, workspace_root)
        except Exception as e:
            logger.warning("Building context fetch failed: %s", e)
            console.warn(f"Building context fetch failed: {e}")

        generate_mcp_json(config, workspace_root)
        generate_opencode_json(config, workspace_root)
        generate_gitignore(workspace_root)

        # Step 5: Start continuous sync (foreground — blocks until Ctrl-C)
        console.step(5, total, "Starting continuous sync...")
        sync_session.write_mutagen_yml(workspace_root)
        sync_started = False
        try:
            sync_session.start(config, workspace_root)
            sync_started = True
            console.status("Sync session started")
        except click.ClickException as e:
            logger.warning("Sync start failed: %s", e.format_message())
            console.warn(f"Sync start failed: {e.format_message()}")
            console.warn("Run 'cinna dev' from the agent directory to retry.")

        console.status("Setup complete!")
        console.console.print()
        console.console.print(f"  cd {dir_name}/")
        console.console.print(
            "  cinna dev                         # start a foreground dev session"
        )
        console.console.print(
            "  claude                            # open Claude Code with MCP tools"
        )
        console.console.print(
            "  cinna list                        # see all registered agents"
        )
        console.console.print(
            "  cinna sync status                 # view sync state (from another terminal)"
        )
        console.console.print(
            "  cinna exec python scripts/main.py # run a command in the remote env"
        )
        console.console.print()

        # Attach the foreground sync TUI. Sync lives exactly as long as this
        # process — Ctrl-C terminates the session so nothing is left dangling
        # in the shared Mutagen daemon.
        if sync_started:
            console.status("Live sync attached — press Ctrl-C to stop.")
            sync_session.run_foreground(config)
            console.status("Sync session terminated.")
    finally:
        client.close()
