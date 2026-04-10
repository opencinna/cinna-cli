"""Setup flow: exchange token, download build context, clone workspace, configure environment."""

import logging
import os
import platform
import re
from pathlib import Path
from urllib.parse import urlparse

import click
import httpx

from cinna.config import (
    CinnaConfig,
    KnowledgeSource,
    save_config,
    build_dir,
    workspace_dir,
)
from cinna.client import PlatformClient
from cinna.docker import (
    check_docker_available,
    ensure_dev_compose_override,
    extract_build_context,
    build_container,
)
from cinna.sync import pull_credentials, extract_workspace_tarball, ensure_workspace_dirs
from cinna.context import (
    generate_context_files,
    generate_mcp_json,
    generate_opencode_json,
    generate_gitignore,
)
from cinna import console

logger = logging.getLogger("cinna.bootstrap")


def parse_setup_input(raw_input: str) -> tuple[str, str]:
    """Parse setup input into (platform_url, token).

    Accepts any of:
      - Full curl command: 'curl -sL http://host:8000/cli-setup/TOKEN | python3 -'
      - URL:               'http://host:8000/cli-setup/TOKEN'
      - Raw token:         'TOKEN' (requires CINNA_PLATFORM_URL env var)

    Returns (platform_url, token).
    """
    text = raw_input.strip().strip("'\"")

    # Try to extract a URL containing /cli-setup/ from the input
    url_match = re.search(r"(https?://[^\s]+/cli-setup/[^\s|\"']+)", text)
    if url_match:
        url = url_match.group(1)
        parsed = urlparse(url)
        # Token is the last path segment after /cli-setup/
        path_parts = parsed.path.rstrip("/").split("/cli-setup/")
        if len(path_parts) == 2 and path_parts[1]:
            token = path_parts[1]
            # Preserve any path prefix before /cli-setup/ (e.g. /api)
            prefix = path_parts[0]  # e.g. "/api" or ""
            platform_url = f"{parsed.scheme}://{parsed.netloc}{prefix}"
            return platform_url, token

    # If input looks like a URL but we couldn't parse it, fail clearly
    if text.startswith("http://") or text.startswith("https://") or "curl" in text:
        raise click.ClickException(
            "Could not parse setup URL from input. Expected a URL containing /cli-setup/TOKEN."
        )

    # Treat as raw token — need CINNA_PLATFORM_URL
    platform_url = os.environ.get("CINNA_PLATFORM_URL", "")
    if not platform_url:
        raise click.ClickException(
            "Cannot determine platform URL from the provided token.\n"
            "Either paste the full curl command / URL from the platform UI,\n"
            "or set the CINNA_PLATFORM_URL environment variable."
        )
    return platform_url, text


def normalize_agent_dir_name(name: str) -> str:
    """Normalize agent name to a lowercase, dash-separated directory name.

    "HR Manager Agent" -> "hr-manager-agent"
    "My  Cool--Agent!" -> "my-cool-agent"
    """
    # Lowercase, replace non-alphanumeric with dashes, collapse runs, strip edges
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "agent"


def run_setup(setup_input: str, machine_name: str) -> None:
    """Full setup flow — called by `cinna setup <token_or_url>`."""
    total = 6

    # Step 1: Prerequisites
    console.step(1, total, "Checking prerequisites...")
    check_docker_available()
    console.status("Docker available")

    # Step 2: Authenticate
    console.step(2, total, "Authenticating...")
    machine_info = f"{platform.system()}/{platform.machine()}"

    platform_url, token = parse_setup_input(setup_input)

    setup_url = f"{platform_url.rstrip('/')}/cli-setup/{token}"
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

    payload = response.json()
    agent_info = payload["agent"]
    agent_name = agent_info["name"]
    dir_name = normalize_agent_dir_name(agent_name)
    logger.info("Agent: %s (dir: %s)", agent_name, dir_name)

    # Create workspace directory (fail if it already exists with a .cinna config)
    workspace_root = Path.cwd() / dir_name
    if (workspace_root / ".cinna" / "config.json").exists():
        raise click.ClickException(
            f"Directory '{dir_name}/' already contains a cinna workspace.\n"
            f"Remove it first with 'cinna disconnect' or delete the directory."
        )
    workspace_root.mkdir(exist_ok=True)

    # Build config
    config = CinnaConfig(
        platform_url=payload["platform_url"],
        cli_token=payload["cli_token"],
        agent_id=agent_info["id"],
        agent_name=agent_name,
        environment_id=agent_info["environment_id"],
        template=agent_info["template"],
        container_name=f"agent-dev-{dir_name}-{agent_info['id'][:8]}",
        knowledge_sources=[
            KnowledgeSource(**ks) for ks in payload.get("knowledge_sources", [])
        ],
    )
    save_config(config, workspace_root)
    console.status(f"Authenticated as agent: {agent_name}")

    # Step 3: Build context + container
    console.step(3, total, "Building agent container...")
    client = PlatformClient(config)

    try:
        logger.info("Downloading build context for agent %s", config.agent_id)
        tarball = client.download_build_context(config.agent_id)
        logger.info("Build context downloaded (%d bytes)", len(tarball))
        extract_build_context(tarball, workspace_root)

        # Write .env for docker-compose
        env_file = build_dir(workspace_root) / ".env"
        env_file.write_text(f"AGENT_NAME={dir_name}\n")

        # Override production entrypoint with idle command for local dev
        ensure_dev_compose_override(workspace_root)

        build_container(workspace_root)
        console.status("Container built")

        # Step 4: Clone workspace
        console.step(4, total, "Cloning workspace...")
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
            console.warn("Run 'cinna pull' after setup to retry.")
        ensure_workspace_dirs(ws_dir)

        # Step 5: Pull credentials
        console.step(5, total, "Pulling credentials...")
        try:
            pull_credentials(client, config, workspace_root)
        except Exception as e:
            logger.warning("Credentials pull failed: %s", e)
            console.warn(f"Credentials pull failed: {e}")
            console.warn("Run 'cinna credentials' after setup to retry.")

        # Step 6: Configure environment
        console.step(6, total, "Configuring development environment...")

        try:
            building_ctx = client.get_building_context(config.agent_id)
            generate_context_files(building_ctx, config, workspace_root)
        except Exception as e:
            logger.warning("Building context fetch failed: %s", e)
            console.warn(f"Building context fetch failed: {e}")
            console.warn("Run 'cinna pull' after setup to retry.")

        generate_mcp_json(config, workspace_root)
        generate_opencode_json(config, workspace_root)
        generate_gitignore(workspace_root)

        console.status("Setup complete!")
        console.console.print()
        console.console.print(f"  cd {dir_name}/")
        console.console.print(
            "  claude                              # open Claude Code with MCP tools"
        )
        console.console.print(
            "  cinna dev                           # start container + live sync"
        )
        console.console.print(
            "  cinna env-up                        # start container in background"
        )
        console.console.print(
            "  cinna exec python scripts/main.py   # run a script (needs running container)"
        )
        console.console.print()

        # Offer to start dev mode right away.
        # stdin may be a pipe (curl ... | python3 -), so open /dev/tty
        # to read the interactive prompt from the real terminal.
        import sys

        tty = None
        try:
            if not sys.stdin.isatty():
                tty = open("/dev/tty")
                sys.stdin = tty

            if click.confirm(
                f"Start dev mode now for {agent_name}?", default=True
            ):
                from cinna.dev import run_dev_loop

                os.chdir(workspace_root)
                console.console.print()
                run_dev_loop(config, workspace_root)
        except OSError:
            pass  # no TTY available (e.g. headless CI) — skip the prompt
        finally:
            if tty:
                sys.stdin = sys.__stdin__
                tty.close()

    finally:
        client.close()
