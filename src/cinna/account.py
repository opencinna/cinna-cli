"""Account workspace — `.cinna/account.json` config + account-level commands.

An account workspace is the multi-agent root produced by `cinna account setup`
(Settings → Local Development on the platform). It holds the account CLI token
(used only for the `/api/v1/cli/account/*` routes) and an `agents/` directory
under which `cinna agent sync` materializes 100% standard per-agent workspaces
— byte-identical to what `cinna setup` produces, only the token's provenance
differs.

Layout:

    my-cinna/
      .cinna/account.json   # account token + platform/frontend URLs + machine name
      CLAUDE.md             # orchestrator prompt (minimal in Phase 1)
      agents/
        crm-agent/          # standard cinna per-agent workspace
"""

import json
import logging
import os
import platform
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

import click
import httpx

from cinna import console
from cinna import sync_session
from cinna.bootstrap import (
    normalize_agent_dir_name,
    provision_workspace,
    remove_workspace_artifacts,
    write_workspace_from_payload,
)
from cinna.client import AccountClient, PlatformClient
from cinna.config import (
    CONFIG_DIR,
    CinnaConfig,
    load_config,
    remove_agent_registry,
)
from cinna.errors import AccountConfigNotFoundError

logger = logging.getLogger("cinna.account")

ACCOUNT_CONFIG_FILE = "account.json"
AGENTS_DIR = "agents"
DEFAULT_ACCOUNT_DIR = "my-cinna"


@dataclass
class AccountConfig:
    platform_url: str
    frontend_url: str
    account_token: str
    machine_name: str


# ── Account config I/O ──────────────────────────────────────────────────────


def account_config_path(account_root: Path) -> Path:
    return account_root / CONFIG_DIR / ACCOUNT_CONFIG_FILE


def agents_dir(account_root: Path) -> Path:
    return account_root / AGENTS_DIR


def find_account_root(start: Path | None = None) -> Path:
    """Walk up from start (or cwd) looking for .cinna/account.json.

    Mirrors ``find_workspace_root`` for per-agent workspaces. Raises
    AccountConfigNotFoundError if not found.
    """
    current = (start or Path.cwd()).resolve()
    while True:
        if account_config_path(current).is_file():
            return current
        parent = current.parent
        if parent == current:
            raise AccountConfigNotFoundError()
        current = parent


def load_account_config(account_root: Path | None = None) -> AccountConfig:
    """Load and validate config from .cinna/account.json."""
    if account_root is None:
        account_root = find_account_root()
    path = account_config_path(account_root)
    if not path.is_file():
        raise AccountConfigNotFoundError()
    data = json.loads(path.read_text())
    known_fields = set(AccountConfig.__dataclass_fields__)
    data = {k: v for k, v in data.items() if k in known_fields}
    return AccountConfig(**data)


def save_account_config(config: AccountConfig, account_root: Path) -> None:
    """Write config to .cinna/account.json with 0o600 perms (holds the token)."""
    cfg_dir = account_root / CONFIG_DIR
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = account_config_path(account_root)
    path.write_text(json.dumps(asdict(config), indent=2) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ── Setup input parsing ─────────────────────────────────────────────────────


def parse_account_setup_input(raw_input: str) -> tuple[str, str]:
    """Parse account setup input into (platform_url, token).

    Accepts any of (paste directly from Settings → Local Development):
      - Full curl command: 'curl -sL http://host/api/cli-setup/account/TOKEN | python3 -'
      - URL:               'http://host/api/cli-setup/account/TOKEN'
      - Raw token:         'TOKEN' (falls back to the CINNA_PLATFORM_URL env var)
    """
    text = raw_input.strip().strip("'\"")

    url_match = re.search(r"(https?://[^\s]+/cli-setup/account/[^\s|\"']+)", text)
    if url_match:
        url = url_match.group(1)
        parsed = urlparse(url)
        path_parts = parsed.path.rstrip("/").split("/cli-setup/account/")
        if len(path_parts) == 2 and path_parts[1]:
            token = path_parts[1]
            prefix = path_parts[0]
            platform_url = f"{parsed.scheme}://{parsed.netloc}{prefix}"
            return platform_url, token

    if text.startswith("http://") or text.startswith("https://") or "curl" in text:
        raise click.ClickException(
            "Could not parse account setup URL from input. "
            "Expected a URL containing /cli-setup/account/TOKEN."
        )

    platform_url = os.environ.get("CINNA_PLATFORM_URL", "")
    if not platform_url:
        raise click.ClickException(
            "Cannot determine platform URL from the provided token.\n"
            "Either paste the full curl command / URL from the platform UI,\n"
            "or set the CINNA_PLATFORM_URL environment variable."
        )
    return platform_url, text


def _exchange_account_setup_token(
    platform_url: str, token: str, machine_name: str
) -> dict:
    """POST /cli-setup/account/{token} and return the decoded payload.

    Mirrors the per-agent ``_exchange_setup_token`` — backend error details
    are surfaced verbatim in a uniform ClickException.
    """
    setup_url = f"{platform_url.rstrip('/')}/cli-setup/account/{token}"
    machine_info = f"{platform.system()}/{platform.machine()}"
    logger.info("Exchanging account setup token at %s", setup_url)

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
        raise click.ClickException(f"Account setup failed: {detail}")
    return response.json()


# ── Child workspace resolution ──────────────────────────────────────────────


def list_child_workspaces(account_root: Path) -> list[tuple[Path, CinnaConfig]]:
    """Return every synced per-agent workspace under ``agents/``."""
    result: list[tuple[Path, CinnaConfig]] = []
    base = agents_dir(account_root)
    if not base.is_dir():
        return result
    for child in sorted(base.iterdir()):
        if child.is_dir() and (child / CONFIG_DIR / "config.json").is_file():
            try:
                result.append((child, load_config(child)))
            except Exception:
                logger.warning("Unreadable child workspace config: %s", child)
    return result


def resolve_child_workspace(
    account_root: Path, agent_ref: str
) -> tuple[Path, CinnaConfig] | None:
    """Resolve ``agent_ref`` (display name, slug, or agent id) to a synced
    child workspace under ``agents/``. Returns (path, config) or None."""
    ref_slug = normalize_agent_dir_name(agent_ref)
    for child, config in list_child_workspaces(account_root):
        if agent_ref == config.agent_id:
            return child, config
        if ref_slug and ref_slug in (
            child.name,
            normalize_agent_dir_name(config.agent_name),
        ):
            return child, config
    return None


def _resolve_account_agent(items: list[dict], agent_ref: str) -> dict:
    """Resolve ``agent_ref`` against the `/account/agents` listing.

    Matches by agent UUID, exact name, or slugified name. Raises a
    ClickException listing the available agents when nothing matches, or the
    ambiguous matches when several agents share the slug.
    """
    by_id = [a for a in items if a.get("id") == agent_ref]
    if by_id:
        return by_id[0]

    ref_slug = normalize_agent_dir_name(agent_ref)
    matches = [
        a
        for a in items
        if a.get("name") == agent_ref
        or normalize_agent_dir_name(a.get("name", "")) == ref_slug
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(f"{a['name']} ({a['id']})" for a in matches)
        raise click.ClickException(
            f"Agent reference '{agent_ref}' is ambiguous — matches: {names}.\n"
            f"Use the agent id instead."
        )

    available = ", ".join(a.get("name", "?") for a in items) or "none"
    raise click.ClickException(
        f"No accessible agent matches '{agent_ref}'.\n"
        f"Available agents: {available}\n"
        f"Run 'cinna account agents' to see the full list."
    )


# ── Token probe ─────────────────────────────────────────────────────────────


def probe_account_token(config: AccountConfig) -> str:
    """Classify the stored account token: valid / expired / unreachable.

    Same pattern as the per-agent ``_probe_token_statuses`` — a cheap
    authenticated GET; 2xx → valid, 401 → expired, anything else → unreachable.
    """
    platform_url = config.platform_url.rstrip("/")
    if not platform_url or not config.account_token:
        return "unreachable"
    try:
        response = httpx.get(
            f"{platform_url}/api/v1/cli/account/agents",
            headers={"Authorization": f"Bearer {config.account_token}"},
            timeout=httpx.Timeout(5.0, connect=3.0),
            follow_redirects=True,
        )
    except Exception:
        return "unreachable"
    if response.status_code == 401:
        return "expired"
    if 200 <= response.status_code < 300:
        return "valid"
    return "unreachable"


# ── Command bodies ──────────────────────────────────────────────────────────


def _load_account_template() -> str:
    import importlib.resources

    return (
        importlib.resources.files("cinna.templates")
        .joinpath("ACCOUNT_CLAUDE.md.template")
        .read_text()
    )


def _install_context_package(
    account_cfg: AccountConfig, account_root: Path, *, replace: bool = False
) -> bool:
    """Download the orchestrator context package and extract it into the
    account workspace root.

    Tarball members already carry the top-level ``context/`` prefix, so
    extraction lands at ``<account_root>/context/``. Reuses the workspace
    clone's safe extractor (rejects absolute paths, ``..`` traversal,
    symlinks, oversized files).

    With ``replace=True`` the existing ``context/`` tree is removed first —
    but only after a successful download, so a failed refresh never destroys
    the previous context.

    Never raises: failures warn and return False (the workspace is fully
    functional without the context package).
    """
    import shutil

    from cinna.sync import extract_workspace_tarball

    try:
        with AccountClient(account_cfg) as client:
            archive = client.download_context_package()
    except Exception as exc:
        msg = exc.format_message() if isinstance(exc, click.ClickException) else exc
        console.warn(f"Context package download failed: {msg}")
        console.warn(
            "The workspace works without it — run 'cinna account refresh-context' "
            "to retry later."
        )
        return False

    context_dir = account_root / "context"
    if replace and context_dir.exists():
        shutil.rmtree(context_dir)

    try:
        extracted = extract_workspace_tarball(archive, account_root)
    except Exception as exc:
        logger.warning("Context package extraction failed: %s", exc)
        console.warn(f"Context package extraction failed: {exc}")
        console.warn("Run 'cinna account refresh-context' to retry.")
        return False

    console.status(f"Context package installed ({len(extracted)} files under context/)")
    return True


def run_account_setup(
    setup_input: str, machine_name: str, dir_name: str = DEFAULT_ACCOUNT_DIR
) -> None:
    """Full account setup flow — called by `cinna account setup <token_or_url>`."""
    from datetime import datetime, timezone

    total = 3

    # Guard the target directory before burning the single-use setup token.
    account_root = Path.cwd() / dir_name
    if account_config_path(account_root).exists():
        raise click.ClickException(
            f"Directory '{dir_name}/' already contains a cinna account workspace.\n"
            f"Run account commands from inside it, or choose another --dir."
        )

    # Step 1: Exchange the account setup token
    console.step(1, total, "Authenticating...")
    platform_url, token = parse_account_setup_input(setup_input)
    payload = _exchange_account_setup_token(platform_url, token, machine_name)

    # Step 2: Materialize the account workspace
    console.step(2, total, "Creating account workspace...")
    account_root.mkdir(exist_ok=True)

    config = AccountConfig(
        platform_url=payload["platform_url"],
        frontend_url=payload.get("frontend_url") or payload["platform_url"],
        account_token=payload["account_token"],
        machine_name=payload.get("machine_name") or machine_name,
    )
    save_account_config(config, account_root)
    agents_dir(account_root).mkdir(exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    claude_md = (
        _load_account_template()
        .replace("{timestamp}", timestamp)
        .replace("{frontend_url}", config.frontend_url)
    )
    (account_root / "CLAUDE.md").write_text(claude_md)

    # Step 3: Context package (best-effort — setup succeeds without it)
    console.step(3, total, "Downloading context package...")
    _install_context_package(config, account_root)

    console.status("Account workspace created!")
    console.console.print()
    console.console.print(f"  cd {dir_name}/")
    console.console.print(
        "  cinna account agents              # list agents you can build"
    )
    console.console.print(
        "  cinna agent sync <agent>          # attach an agent workspace under agents/"
    )
    console.console.print(
        "  cinna account status              # account workspace + token info"
    )
    console.console.print()


def run_account_refresh_context() -> None:
    """Re-download and replace `context/` — called by `cinna account refresh-context`.

    The old tree is only removed after a successful download, so a failed
    refresh leaves the existing context intact (warn-don't-die).
    """
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with console.spinner("Refreshing context package..."):
        ok = _install_context_package(account_cfg, account_root, replace=True)
    if ok:
        console.status(f"Context refreshed under {account_root / 'context'}")


def run_account_agents() -> None:
    """List accessible agents — called by `cinna account agents`."""
    from rich.table import Table

    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with console.spinner("Fetching agents..."):
        with AccountClient(account_cfg) as client:
            listing = client.list_account_agents()

    items = listing.get("data", [])
    if not items:
        console.status("No accessible agents on this account.")
        return

    children = list_child_workspaces(account_root)
    workspace_by_agent_id = {cfg.agent_id: path for path, cfg in children}

    table = Table(
        title=f"Accessible agents ({listing.get('count', len(items))})",
        title_style="bold",
        show_lines=True,
    )
    table.add_column("#", style="dim", justify="right")
    table.add_column("Agent")
    table.add_column("Build")
    table.add_column("Env")
    table.add_column("Local workspace")

    for i, item in enumerate(items, 1):
        agent_cell = f"[bold]{item.get('name', '?')}[/bold]\n[dim]{item.get('id', '?')}[/dim]"

        if item.get("can_build"):
            build_cell = "[green]✓ can build[/green]"
        elif item.get("is_foreign_install"):
            build_cell = "[yellow]foreign install[/yellow]"
        else:
            build_cell = "[dim]view-only[/dim]"

        env_cell = (
            "[green]● active[/green]"
            if item.get("has_active_environment")
            else "[dim]○[/dim]"
        )

        ws_path = workspace_by_agent_id.get(item.get("id"))
        if ws_path is not None:
            ws_cell = f"agents/{ws_path.name}/"
        else:
            ws_cell = "[dim]not synced[/dim]"

        table.add_row(str(i), agent_cell, build_cell, env_cell, ws_cell)

    console.console.print(table)


def run_account_status() -> None:
    """Account workspace info + token probe — called by `cinna account status`."""
    from rich.table import Table

    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with console.spinner("Checking token..."):
        token_status = probe_account_token(account_cfg)

    children = list_child_workspaces(account_root)

    table = Table(title="Account workspace")
    table.add_column("Property", style="dim")
    table.add_column("Value")
    table.add_row("Platform", account_cfg.platform_url)
    table.add_row("Frontend", account_cfg.frontend_url)
    table.add_row("Machine", account_cfg.machine_name)
    table.add_row("Account root", str(account_root))
    table.add_row("Synced agents", str(len(children)))
    table.add_row("Token", _format_token_label(token_status))

    console.console.print(table)

    if children:
        console.console.print(
            "[dim]Synced:[/dim] "
            + ", ".join(f"agents/{path.name}/" for path, _ in children)
        )


def _format_token_label(status: str) -> str:
    if status == "valid":
        return "[green]valid token[/green]"
    if status == "expired":
        return "[red]expired token[/red]"
    return "[yellow]no connection[/yellow]"


def run_agent_sync(agent_ref: str, machine_name: str | None) -> None:
    """Mint a child token and materialize a standard per-agent workspace.

    Called by `cinna agent sync <agent>`. Delegates to the same bootstrap
    writer as `cinna setup`, so the resulting workspace under
    ``agents/<slug>/`` is identical to a hand-set-up one.
    """
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    total = 4

    # Step 1: Resolve + mint
    console.step(1, total, "Minting agent token...")
    machine = machine_name or account_cfg.machine_name
    machine_info = f"{platform.system()}/{platform.machine()}"

    with AccountClient(account_cfg) as client:
        listing = client.list_account_agents()
        item = _resolve_account_agent(listing.get("data", []), agent_ref)

        dir_name = normalize_agent_dir_name(item["name"])
        workspace_root = agents_dir(account_root) / dir_name
        if (workspace_root / CONFIG_DIR / "config.json").exists():
            raise click.ClickException(
                f"'{AGENTS_DIR}/{dir_name}/' is already a synced workspace.\n"
                f"Run 'cinna agent unsync {dir_name}' first, or use "
                f"'cinna set-token' inside it to refresh the token."
            )

        mint = client.mint_agent_token(item["id"], machine, machine_info)

    payload = {
        "cli_token": mint["token"],
        "cli_token_id": mint.get("id"),
        "agent": {
            "id": mint["agent_id"],
            "name": mint["agent_name"],
            "environment_id": mint.get("environment_id"),
            "template": mint.get("template"),
        },
        "platform_url": account_cfg.platform_url,
        "frontend_url": mint.get("frontend_url") or account_cfg.frontend_url,
        "knowledge_sources": mint.get("knowledge_sources", []),
    }

    agents_dir(account_root).mkdir(exist_ok=True)
    workspace_root.mkdir(exist_ok=True)
    config = write_workspace_from_payload(payload, workspace_root)
    console.status(f"Minted CLI token for agent: {config.agent_name}")

    # Steps 2-4: standard per-agent provisioning (same as `cinna setup`)
    agent_client = PlatformClient(config)
    try:
        provision_workspace(
            agent_client,
            config,
            workspace_root,
            interactive=sys.stdin.isatty(),
            total=total,
            first_step=2,
        )
    finally:
        agent_client.close()

    console.status(f"Agent synced under {AGENTS_DIR}/{dir_name}/")
    console.console.print()
    console.console.print(f"  cd {AGENTS_DIR}/{dir_name}/")
    console.console.print(
        "  cinna dev                         # start a foreground dev session"
    )
    console.console.print(
        f"  cinna exec --agent {dir_name} <cmd>   # or exec from the account root"
    )
    console.console.print()


def run_agent_unsync(agent_ref: str) -> None:
    """Tear down a synced child workspace — called by `cinna agent unsync`.

    Stops sync, revokes the child token server-side (best-effort), then does
    the equivalent of `cinna disconnect`: removes `.cinna/` + generated files
    + the registry entry. User workspace files are preserved.
    """
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    resolved = resolve_child_workspace(account_root, agent_ref)
    if resolved is None:
        raise click.ClickException(
            f"No synced workspace matches '{agent_ref}' under "
            f"{agents_dir(account_root)}/.\n"
            f"Run 'cinna account agents' to see which agents are synced."
        )
    child_root, config = resolved

    console.warn(
        f"This will stop sync for '{config.agent_name}', revoke its CLI token, "
        f"remove .cinna/ config, and delete generated files."
    )
    console.console.print("Workspace files will be preserved.")
    if not click.confirm("Continue?"):
        raise click.Abort()

    try:
        sync_session.stop(config)
    except Exception as exc:
        console.warn(f"Could not stop sync session cleanly: {exc}")

    if config.cli_token_id:
        # Revoke the minted child token via the account-scoped endpoint
        # (DELETE /account/tokens/children/{id}, authenticated by the account
        # token). Idempotent server-side; failures (network, 404 for
        # workspaces predating provenance tracking) degrade gracefully —
        # local teardown proceeds regardless.
        try:
            with AccountClient(account_cfg) as client:
                client.revoke_child_token(config.cli_token_id)
            console.status("Child token revoked on the platform.")
        except Exception as exc:
            msg = exc.format_message() if isinstance(exc, click.ClickException) else exc
            console.warn(f"Server-side token revoke failed: {msg}")
            console.warn(
                "The token will expire on its own, or revoke it from the "
                "agent's Integrations tab / the account session in Settings."
            )
    else:
        console.warn(
            "No stored token id for this workspace — skipping server-side revoke."
        )

    remove_agent_registry(config.agent_id)
    remove_workspace_artifacts(child_root)

    console.status(
        f"Unsynced {config.agent_name}. Workspace files preserved under "
        f"{AGENTS_DIR}/{child_root.name}/."
    )


# ── Phase 3: convenience verbs + API escape hatch ───────────────────────────


def run_agent_create(name: str, description: str | None) -> None:
    """Create an agent from the account workspace — `cinna agent create`.

    Thin client: only the user-specified fields are sent; the backend applies
    all defaults (AI credentials, env template, environment) and returns the
    full record.
    """
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with console.spinner("Creating agent..."):
        with AccountClient(account_cfg) as client:
            agent = client.create_agent(name, description)

    agent_id = agent.get("id", "?")
    agent_name = agent.get("name", name)
    agent_link = f"{account_cfg.frontend_url.rstrip('/')}/agent/{agent_id}"

    console.status(f"Agent created: {agent_name}")
    console.console.print(f"  Agent ID:  {agent_id}")
    console.console.print(f"  Web UI:    {agent_link}")
    console.console.print()
    console.console.print(
        f"  cinna agent sync {normalize_agent_dir_name(agent_name)}"
        "   # attach a local workspace"
    )
    console.console.print()


def run_connect_agent_api(
    producer_ref: str,
    consumer_ref: str,
    label: str | None,
    read_only: bool,
) -> None:
    """Wire consumer → producer REST API — `cinna connect agent-api`."""
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        listing = client.list_account_agents()
        items = listing.get("data", [])
        producer = _resolve_account_agent(items, producer_ref)
        consumer = _resolve_account_agent(items, consumer_ref)

        with console.spinner("Connecting agent API..."):
            result = client.connect_agent_api(
                producer["id"],
                consumer["id"],
                credential_label=label,
                read_only_override=read_only,
            )

    console.status(
        f"Connected: {consumer['name']} → {producer['name']} (REST API)"
    )
    console.console.print(f"  Credential:    {result.get('credential_id', '?')}")
    console.console.print(f"  Token prefix:  {result.get('token_prefix', '?')}")
    console.console.print(f"  Base URL:      {result.get('base_url', '?')}")
    if result.get("spec_url"):
        console.console.print(f"  Spec URL:      {result['spec_url']}")
    console.console.print()
    console.console.print(
        "The credential rides the consumer's normal credential sync — it lands "
        "in the agent's remote env automatically (visible read-only under "
        "workspace/credentials/ in a synced workspace)."
    )


def run_connect_mcp(
    producer_ref: str,
    consumer_ref: str,
    label: str | None,
    conversation_only: bool,
    building_only: bool,
) -> None:
    """Wire consumer → producer agent2agent MCP connector — `cinna connect mcp`."""
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        listing = client.list_account_agents()
        consumer = _resolve_account_agent(listing.get("data", []), consumer_ref)

        discoverable = client.list_discoverable_mcp(consumer["id"])
        connector = _resolve_discoverable_connector(
            discoverable.get("data", []), producer_ref
        )

        with console.spinner("Connecting MCP..."):
            result = client.connect_mcp(
                connector["connector_id"],
                consumer["id"],
                mcp_mode_conversation=not building_only,
                mcp_mode_building=not conversation_only,
                label=label,
            )

    console.status(
        f"Connected: {consumer['name']} → {connector['agent_name']} (MCP)"
    )
    console.console.print(f"  Credential:  {result.get('credential_id', '?')}")
    console.console.print(f"  Endpoint:    {result.get('endpoint_url', '?')}")
    console.console.print(f"  Transport:   {result.get('transport', '?')}")
    console.console.print(f"  Auth mode:   {result.get('auth_mode', '?')}")
    console.console.print(f"  Status:      {result.get('status', '?')}")
    if result.get("authorize_url"):
        console.console.print()
        console.warn("Authorization required — open this URL to finish the connection:")
        console.console.print(f"  {result['authorize_url']}")


def _resolve_discoverable_connector(items: list[dict], producer_ref: str) -> dict:
    """Resolve ``producer_ref`` against the discoverable-MCP listing.

    Matches by producer agent id, exact name, or slugified name. Raises a
    ClickException listing the discoverable options when nothing matches, or
    the ambiguous rows (an agent can expose several connectors) when more
    than one matches.
    """
    by_id = [c for c in items if c.get("agent_id") == producer_ref]
    matches = by_id
    if not matches:
        ref_slug = normalize_agent_dir_name(producer_ref)
        matches = [
            c
            for c in items
            if c.get("agent_name") == producer_ref
            or normalize_agent_dir_name(c.get("agent_name", "")) == ref_slug
        ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        rows = ", ".join(
            f"{c.get('connector_name', '?')} ({c.get('connector_id', '?')})"
            for c in matches
        )
        raise click.ClickException(
            f"Producer '{producer_ref}' exposes more than one discoverable "
            f"connector — matches: {rows}.\n"
            f"This version of cinna cannot pick between them; connect from the "
            f"platform UI, or use 'cinna api' with the connector id."
        )

    available = (
        ", ".join(
            f"{c.get('agent_name', '?')} ({c.get('agent_id', '?')})" for c in items
        )
        or "none"
    )
    raise click.ClickException(
        f"No discoverable agent2agent MCP connector matches '{producer_ref}'.\n"
        f"Discoverable producers: {available}\n"
        f"(The producer agent must expose an agent2agent MCP connector that "
        f"your account is allowed to consume.)"
    )


# The backend stamps every mirrored inner-API passthrough with this header
# (any status 2xx–5xx) and OMITS it on the hatch's own refusals (policy
# denial / malformed path / size cap / rate limit). It is the authoritative
# signal for telling "the target route answered" from "the platform refused".
_PROXIED_HEADER = "x-cinna-proxied"


def run_api(
    method: str,
    path: str,
    json_text: str | None,
    data_file: str | None,
    query_pairs: tuple[str, ...],
) -> None:
    """Generic platform-API call via the escape hatch — `cinna api`.

    Exit codes: 0 for inner 2xx, 1 for inner 4xx/5xx (body still printed),
    2 for the escape hatch's own errors (policy denial, rate limit, size cap).
    """
    if json_text is not None and data_file is not None:
        raise click.ClickException("--json and --data are mutually exclusive.")

    json_body = None
    if json_text is not None:
        try:
            json_body = json.loads(json_text)
        except json.JSONDecodeError as e:
            raise click.ClickException(f"--json is not valid JSON: {e}")
    elif data_file is not None:
        file_path = Path(data_file[1:] if data_file.startswith("@") else data_file)
        try:
            json_body = json.loads(file_path.read_text())
        except OSError as e:
            raise click.ClickException(f"Could not read --data file: {e}")
        except json.JSONDecodeError as e:
            raise click.ClickException(f"--data file is not valid JSON: {e}")

    query: dict[str, str | list[str]] = {}
    for pair in query_pairs:
        if "=" not in pair:
            raise click.ClickException(
                f"--query expects key=value, got '{pair}'."
            )
        key, value = pair.split("=", 1)
        existing = query.get(key)
        if existing is None:
            query[key] = value
        elif isinstance(existing, list):
            existing.append(value)
        else:
            query[key] = [existing, value]

    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        response = client.api_proxy(
            method.upper(), path, query=query or None, json_body=json_body
        )

    body_text = response.text
    content_type = response.headers.get("content-type", "")
    if "json" in content_type and body_text:
        try:
            body_text = json.dumps(response.json(), indent=2)
        except Exception:
            pass

    # Header absent → the escape hatch itself refused (policy / limit / size
    # cap). Print the detail to stderr and exit 2 so the agent can tell "the
    # platform said no" from "the target route errored".
    if _PROXIED_HEADER not in response.headers:
        detail: str | None = None
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                raw_detail = parsed.get("detail")
                if isinstance(raw_detail, str):
                    detail = raw_detail
        except Exception:
            pass
        prefix = (
            "blocked by platform policy: "
            if response.status_code in (400, 403)
            else ""
        )
        click.echo(f"{prefix}{detail or body_text}", err=True)
        retry_after = response.headers.get("retry-after")
        if response.status_code == 429 and retry_after:
            click.echo(f"Retry after {retry_after}s.", err=True)
        sys.exit(2)

    # Header present → mirrored inner-API response. Print the body verbatim;
    # exit 0 for 2xx, 1 for 4xx/5xx so it composes in shell pipelines.
    if body_text:
        click.echo(body_text)
    if 200 <= response.status_code < 300:
        return
    click.echo(f"HTTP {response.status_code}", err=True)
    sys.exit(1)
