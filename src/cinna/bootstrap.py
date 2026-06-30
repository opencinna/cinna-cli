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
    CONFIG_DIR,
    CONFIG_FILE,
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


def config_from_payload(payload: dict) -> CinnaConfig:
    """Build an in-memory ``CinnaConfig`` from an exchange payload (no IO).

    ``payload`` is the per-agent bootstrap shape returned by
    ``POST /api/cli-setup/{token}`` (and mirrored by the account mint flow):
    ``cli_token``, nested ``agent`` record, ``platform_url``, optional
    ``frontend_url`` / ``cli_token_id`` / ``knowledge_sources``.
    """
    agent_info = payload["agent"]
    return CinnaConfig(
        platform_url=payload["platform_url"],
        cli_token=payload["cli_token"],
        agent_id=agent_info["id"],
        agent_name=agent_info["name"],
        environment_id=agent_info["environment_id"],
        template=agent_info["template"],
        frontend_url=payload.get("frontend_url"),
        cli_token_id=payload.get("cli_token_id"),
        knowledge_sources=[
            KnowledgeSource(**ks) for ks in payload.get("knowledge_sources", [])
        ],
    )


def persist_config(config: CinnaConfig, workspace_root: Path) -> None:
    """Write ``.cinna/config.json`` + the global registry entry for ``config``.

    Carries the git layout block into the registry when present so tooling can
    locate the clone without re-reading the per-agent config.
    """
    from dataclasses import asdict

    save_config(config, workspace_root)
    upsert_agent_registry(
        config.agent_id,
        config.platform_url,
        config.cli_token,
        workspace_root,
        frontend_url=config.frontend_url,
        git=asdict(config.git) if config.git else None,
    )


def write_workspace_from_payload(payload: dict, workspace_root: Path) -> CinnaConfig:
    """Build the config from ``payload`` and persist it to ``workspace_root``."""
    config = config_from_payload(payload)
    persist_config(config, workspace_root)
    return config


def short_agent_hash(agent_id: str) -> str:
    """Stable short form of an agent id used to disambiguate clone-root names.

    Same form as the Mutagen session label (``agent_id`` with dashes stripped,
    first 8 chars) so a given agent always maps to the same suffix.
    """
    return agent_id.replace("-", "")[:8] or "agent"


def workspace_agent_id_at(clone_root: Path) -> str | None:
    """Return the agent_id of a cinna workspace already living at ``clone_root``.

    Looks for a config directly in ``clone_root`` (legacy flat layout) or one
    level down (Model-A nested). Returns ``None`` when no cinna workspace is
    present — an empty or unrelated directory does not count as taken.
    """
    candidates: list[Path] = []
    if (clone_root / CONFIG_DIR / CONFIG_FILE).is_file():
        candidates.append(clone_root)
    elif clone_root.is_dir():
        for child in sorted(clone_root.iterdir()):
            if child.is_dir() and (child / CONFIG_DIR / CONFIG_FILE).is_file():
                candidates.append(child)
    for c in candidates:
        try:
            return load_config(c).agent_id
        except Exception:
            continue
    return None


def resolve_clone_slug(parent_dir: Path, slug: str, agent_id: str) -> str:
    """Pick a clone-root dir name, disambiguating slug collisions.

    Returns ``slug`` when ``<parent>/<slug>/`` is free or already belongs to this
    same agent (so the caller's existence check can report "already set up").
    When it is taken by a *different* agent, appends the agent's short hash —
    ``<slug>-<shorthash>`` — so two agents whose names normalize to the same slug
    get distinct folders.
    """
    existing = workspace_agent_id_at(parent_dir / slug)
    if existing is None or existing == agent_id:
        return slug
    return f"{slug}-{short_agent_hash(agent_id)}"


def prepare_git_layout(
    config: CinnaConfig, client: PlatformClient, parent_dir: Path
) -> tuple[Path, Path, "object | None"]:
    """Decide the Model-A nested layout for a fresh checkout.

    Returns ``(clone_root, workspace_root, coords)`` and mutates ``config.git``
    in place to record the layout (``vcs_enabled=False`` until linked). The
    backend's real ``subdir`` is used when the agent is already git-versioned
    (so the path matches the remote tree and ``cinna git link`` needs no move);
    otherwise the agent slug is the default subdir. When ``<parent>/<slug>/`` is
    already taken by a different agent, the clone-root name gets the agent's
    short-hash suffix so the two don't collide. Coordinates are fetched
    best-effort — an older backend or a network hiccup falls back to the slug.
    """
    from cinna.config import GitLayout, compute_agent_layout
    from cinna import git_versioning

    slug = normalize_agent_dir_name(config.agent_name)
    coords = None
    try:
        coords = git_versioning.fetch_coordinates(client)
    except Exception as exc:  # noqa: BLE001 — best-effort discovery
        logger.warning("git coordinates fetch failed: %s", exc)

    subdir = None
    if coords is not None and coords.vcs_enabled and coords.subdir is not None:
        subdir = coords.subdir

    clone_slug = resolve_clone_slug(parent_dir, slug, config.agent_id)
    clone_root, workspace_root, subdir = compute_agent_layout(
        parent_dir, clone_slug, subdir
    )
    config.git = GitLayout(
        clone_path=str(clone_root), subdir=subdir, vcs_enabled=False
    )
    return clone_root, workspace_root, coords


def provision_workspace(
    client: PlatformClient,
    config: CinnaConfig,
    workspace_root: Path,
    *,
    interactive: bool,
    total: int = 5,
    first_step: int = 2,
) -> None:
    """Materialize a standard per-agent workspace (everything except sync start).

    Three steps printed as ``[first_step..first_step+2]/total``: Mutagen
    check, initial workspace clone, generated context/MCP files + mutagen.yml.
    Shared by `cinna setup` and `cinna agent sync` so both produce identical
    workspaces.
    """
    # Mutagen
    console.step(first_step, total, "Checking Mutagen install...")
    ensure_mutagen_ready(client, config, workspace_root, interactive=interactive)
    console.status(f"Mutagen ready (version {config.mutagen_version})")

    # Initial clone
    console.step(first_step + 1, total, "Cloning workspace...")
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

    # Context files + MCP config
    console.step(first_step + 2, total, "Configuring development environment...")
    try:
        building_ctx = client.get_building_context(config.agent_id)
        generate_context_files(building_ctx, config, workspace_root)
    except Exception as e:
        logger.warning("Building context fetch failed: %s", e)
        console.warn(f"Building context fetch failed: {e}")

    generate_mcp_json(config, workspace_root)
    generate_opencode_json(config, workspace_root)
    generate_gitignore(workspace_root)
    sync_session.write_mutagen_yml(workspace_root)


# Files generated by the CLI at the workspace root. Removed (together with the
# synced prompt reference docs and `.cinna/`) by `cinna disconnect` and
# `cinna agent unsync`; user workspace files are preserved.
GENERATED_WORKSPACE_FILES = [
    "CLAUDE.md",
    "CHAT_TESTING.md",
    "GIT_VERSIONING.md",
    "BUILDING_AGENT.md",
    ".mcp.json",
    "opencode.json",
    "cinna.log",
    "mutagen.yml",
]


def remove_workspace_artifacts(workspace_root: Path) -> None:
    """Remove `.cinna/` and every CLI-generated file; keep user files.

    The disconnect-equivalent teardown shared by `cinna disconnect` and
    `cinna agent unsync`. Does NOT touch the sync session or the global
    registry — callers handle those.
    """
    import shutil

    from cinna.context import list_synced_prompt_refs

    synced_refs = list_synced_prompt_refs(workspace_root)

    shutil.rmtree(workspace_root / ".cinna", ignore_errors=True)

    for f in [*GENERATED_WORKSPACE_FILES, *synced_refs]:
        p = workspace_root / f
        if p.exists():
            p.unlink()


def _maybe_autolink(
    config: CinnaConfig,
    client: PlatformClient,
    workspace_root: Path,
    coords: "object | None",
) -> Path:
    """Run ``cinna git link`` automatically when the agent is git-versioned.

    Returns the (possibly relayout-moved) workspace_root. Link failures are
    surfaced as warnings, never fatal — the agent still works Mutagen-only and
    the dev can retry with ``cinna git link``.
    """
    from cinna import git_versioning

    if coords is None or not getattr(coords, "vcs_enabled", False):
        return workspace_root
    try:
        result = git_versioning.link(config, client, workspace_root, coords)
        console.status(
            f"Git-versioned: linked to {coords.repo_url} "
            f"(branch {result.ref}). Use 'cinna git commit/push/pull'."
        )
        return result.workspace_root
    except click.ClickException as exc:
        console.warn(f"Git link skipped: {exc.format_message()}")
        return workspace_root


def run_setup(setup_input: str, machine_name: str) -> None:
    """Full setup flow — called by `cinna setup <token_or_url>`."""
    total = 5

    # Step 1: Authenticate
    console.step(1, total, "Authenticating...")

    platform_url, token = parse_setup_input(setup_input)
    payload = _exchange_setup_token(platform_url, token, machine_name)
    agent_info = payload["agent"]
    agent_name = agent_info["name"]
    logger.info("Agent: %s", agent_name)

    config = config_from_payload(payload)
    client = PlatformClient(config)
    try:
        # Decide the Model-A nested layout (clone-root / subdir) and learn
        # whether the agent is already git-versioned.
        clone_root, workspace_root, coords = prepare_git_layout(
            config, client, Path.cwd()
        )
        # Refuse if either the nested target or a legacy flat workspace at the
        # clone-root path already holds a cinna config.
        for existing in (workspace_root, clone_root):
            if (existing / ".cinna" / "config.json").exists():
                rel = existing.relative_to(Path.cwd())
                raise click.ClickException(
                    f"Directory '{rel}/' already contains a cinna workspace.\n"
                    f"Remove it first with 'cinna disconnect' or delete the directory."
                )
        workspace_root.mkdir(parents=True, exist_ok=True)
        persist_config(config, workspace_root)
        console.status(f"Authenticated as agent: {agent_name}")

        # Steps 2-4: Mutagen + clone + context files
        provision_workspace(
            client,
            config,
            workspace_root,
            interactive=sys.stdin.isatty(),
            total=total,
            first_step=2,
        )

        # If the agent is already git-versioned, link now so the developer gets
        # a real working tree from the first checkout (no separate `git link`).
        workspace_root = _maybe_autolink(config, client, workspace_root, coords)
        dir_name = str(workspace_root.relative_to(Path.cwd()))

        # Step 5: Start continuous sync (foreground — blocks until Ctrl-C)
        console.step(5, total, "Starting continuous sync...")
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
            sync_session.run_foreground(config, workspace_root)
            console.status("Sync session terminated.")
    finally:
        client.close()
