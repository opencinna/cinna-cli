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
      .mcp.json             # wires the knowledge_query MCP tool (account mode)
      agents/
        crm-agent/          # standard cinna per-agent workspace
"""

import json
import logging
import os
import platform
import re
import sys
import time
import webbrowser
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
    # Active user workspace for workspace-scoped creates (agents, and the
    # credentials they inherit). Client-side only — the backend keeps no
    # active-workspace state; this id is attached to each create call.
    # ``None`` = the Default (unassigned) workspace. The name is cached for
    # display and may go stale on rename (the id is authoritative).
    user_workspace_id: str | None = None
    user_workspace_name: str | None = None


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


def default_account_dir_name(platform_url: str) -> str:
    """Derive a default workspace folder name from the platform domain.

    e.g. ``https://demo-core.opencinna.io`` → ``demo-core_opencinna_io``.
    Hostname only (creds/port stripped); every run of non
    ``[A-Za-z0-9-]`` characters collapses to a single underscore. Falls back to
    ``DEFAULT_ACCOUNT_DIR`` when the URL has no usable host.
    """
    host = urlparse(platform_url).netloc or platform_url.strip()
    host = host.split("@")[-1].split(":")[0]  # strip user:pass@ and :port
    slug = re.sub(r"[^A-Za-z0-9-]+", "_", host).strip("_")
    return slug or DEFAULT_ACCOUNT_DIR


def _prompt_account_dir(default: str) -> str:
    """Ask for the workspace folder name, offering ``default``.

    Works in the ``curl ... | python3 -`` bootstrap too: there stdin carries the
    installer script (not keystrokes), so when stdin is not a TTY we talk to the
    controlling terminal via ``/dev/tty`` as long as stdout is a TTY. With no
    terminal attached (CI, captured output) we return ``default`` unchanged so
    non-interactive runs stay non-interactive.
    """
    if sys.stdin.isatty():
        return click.prompt("Workspace folder name", default=default)

    if not sys.stdout.isatty():
        return default
    try:
        with open("/dev/tty", "r+") as tty:
            tty.write(f"Workspace folder name [{default}]: ")
            tty.flush()
            line = tty.readline()
    except OSError:
        return default
    return line.strip() or default


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


# ── Browser re-auth (device authorization flow) ─────────────────────────────
#
# `cinna login` refreshes the account token in place without a pasted setup
# token. It is an OAuth 2.0 Device Authorization Grant (RFC 8628): the CLL
# starts a request, the user authorizes it in a browser already signed in to the
# platform, and the CLI polls until the backend hands back a fresh account token.
#
# Backend contract (both unauthenticated — the point is the old token is dead):
#   POST {platform}/api/v1/cli/account/login/start
#        body: {machine_name, machine_info}
#        200 : {device_code, user_code, verification_uri,
#               verification_uri_complete?, interval?, expires_in?}
#   POST {platform}/api/v1/cli/account/login/poll
#        body: {device_code}
#        200 : {status: "authorization_pending"|"slow_down"|"authorized"
#                       |"access_denied"|"expired_token",
#               account_token?, platform_url?, frontend_url?, machine_name?}

_LOGIN_DEFAULT_INTERVAL = 5  # seconds between polls when the server omits one
_LOGIN_DEFAULT_EXPIRY = 900  # safety cap when the server omits expires_in
_LOGIN_START_PATH = "/api/v1/cli/account/login/start"
_LOGIN_POLL_PATH = "/api/v1/cli/account/login/poll"


def _login_start(platform_url: str, machine_name: str) -> dict:
    """Begin a device-login request; returns the authorize URL + device code."""
    url = f"{platform_url.rstrip('/')}{_LOGIN_START_PATH}"
    machine_info = f"{platform.system()}/{platform.machine()}"
    logger.info("Starting device login at %s", url)
    try:
        response = httpx.post(
            url,
            json={"machine_name": machine_name, "machine_info": machine_info},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise click.ClickException(f"Could not reach {platform_url}: {exc}")
    if response.status_code == 404:
        raise click.ClickException(
            "This platform does not support 'cinna login' yet.\n"
            "Refresh from the UI instead: open Settings → Local Development to "
            "mint a new account setup token, then run\n"
            "  cinna account setup <token>   (in the parent directory)."
        )
    if response.status_code != 200:
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text
        raise click.ClickException(f"Login could not be started: {detail}")
    return response.json()


def _login_poll(platform_url: str, device_code: str) -> dict:
    """Poll a pending device-login request once."""
    url = f"{platform_url.rstrip('/')}{_LOGIN_POLL_PATH}"
    response = httpx.post(url, json={"device_code": device_code}, timeout=30.0)
    if response.status_code != 200:
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text
        raise click.ClickException(f"Login polling failed: {detail}")
    return response.json()


def _poll_until_authorized(
    platform_url: str, device_code: str, interval: int, expires_in: int
) -> dict:
    """Block until the user authorizes (or the request is denied / expires).

    Honors the RFC 8628 ``slow_down`` backoff and the ``expires_in`` deadline.
    Returns the authorized payload (carrying ``account_token``).
    """
    deadline = time.monotonic() + expires_in
    while time.monotonic() < deadline:
        time.sleep(max(1, interval))
        data = _login_poll(platform_url, device_code)
        status = (data.get("status") or "").lower()
        if status in ("authorized", "complete", "success"):
            if not data.get("account_token"):
                raise click.ClickException(
                    "Authorization succeeded but the server returned no token."
                )
            return data
        if status in ("authorization_pending", "pending", ""):
            continue
        if status == "slow_down":
            interval += 5
            continue
        if status in ("access_denied", "denied"):
            raise click.ClickException("Authorization was denied in the browser.")
        if status in ("expired_token", "expired"):
            raise click.ClickException(
                "The login request expired before you authorized it. "
                "Run 'cinna login' again."
            )
        raise click.ClickException(f"Unexpected login status: {status!r}")
    raise click.ClickException(
        "Timed out waiting for authorization. Run 'cinna login' again."
    )


def _device_login(platform_url: str, machine_name: str, frontend_url: str | None = None) -> dict:
    """Drive the full device-authorization handshake; return the authorized
    payload.

    Starts the request, surfaces the verification URL + user code (and opens a
    browser), then polls until the user authorizes. The returned dict carries
    ``account_token`` plus any server-refreshed ``platform_url`` /
    ``frontend_url`` / ``machine_name``.
    """
    console.status(f"Signing in to {frontend_url or platform_url} as {machine_name}…")
    start = _login_start(platform_url, machine_name)

    device_code = start.get("device_code")
    if not device_code:
        raise click.ClickException("Server did not return a device code.")
    user_code = start.get("user_code") or ""
    verify_url = (
        start.get("verification_uri_complete")
        or start.get("verification_url_complete")
        or start.get("verification_uri")
        or start.get("verification_url")
        or start.get("verify_url")
    )
    if not verify_url:
        raise click.ClickException("Server did not return an authorization URL.")
    interval = int(start.get("interval") or _LOGIN_DEFAULT_INTERVAL)
    expires_in = int(start.get("expires_in") or _LOGIN_DEFAULT_EXPIRY)

    console.console.print()
    if user_code:
        console.console.print(f"  Your verification code: [bold]{user_code}[/bold]")
    console.console.print("  Open this URL and click Authorize:")
    console.console.print(f"    [bold]{verify_url}[/bold]")
    console.console.print()
    try:
        webbrowser.open(verify_url)
    except Exception:
        pass  # headless / no browser — the printed URL is the fallback.

    with console.spinner("Waiting for authorization…"):
        return _poll_until_authorized(platform_url, device_code, interval, expires_in)


def _is_local_host(host: str) -> bool:
    h = host.split(":")[0].lower()
    return h in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or h.endswith(".local")


def _normalize_platform_url(raw: str) -> str:
    """Turn a user-typed domain into a ``scheme://netloc`` platform URL.

    Accepts ``app.example.com``, ``https://app.example.com/``,
    ``http://localhost:8000``, etc. A missing scheme defaults to ``https`` —
    except for local hosts (``localhost`` / loopback / ``.local``), which get
    ``http``. Any path/query the user pasted is dropped.
    """
    text = (raw or "").strip().strip("'\"").strip()
    if not text:
        raise click.ClickException("No domain provided.")
    if "://" not in text:
        host_part = text.split("/")[0]
        scheme = "http" if _is_local_host(host_part) else "https"
        text = f"{scheme}://{text}"
    parsed = urlparse(text)
    if not parsed.netloc:
        raise click.ClickException(f"Could not parse a domain from {raw!r}.")
    return f"{parsed.scheme}://{parsed.netloc}"


# ``cinna.log`` is written into cwd by the CLI's own logging setup before this
# check runs, so a genuinely fresh folder still "contains" it — treat it (and
# OS cruft) as not counting toward emptiness.
_IGNORABLE_DIR_ENTRIES = {".DS_Store", "cinna.log"}


def _dir_is_empty(path: Path) -> bool:
    """True if ``path`` doesn't exist or holds nothing but ignorable cruft."""
    if not path.exists():
        return True
    return all(child.name in _IGNORABLE_DIR_ENTRIES for child in path.iterdir())


def _refresh_account_token_in_place(account_root: Path) -> None:
    """Resume path: swap a fresh token into an existing account workspace."""
    account_cfg = load_account_config(account_root)
    result = _device_login(
        account_cfg.platform_url, account_cfg.machine_name, account_cfg.frontend_url
    )

    account_cfg.account_token = result["account_token"]
    if result.get("platform_url"):
        account_cfg.platform_url = result["platform_url"]
    if result.get("frontend_url"):
        account_cfg.frontend_url = result["frontend_url"]
    if result.get("machine_name"):
        account_cfg.machine_name = result["machine_name"]
    save_account_config(account_cfg, account_root)

    console.status(
        f"Signed in — account token refreshed for {account_cfg.machine_name}."
    )
    console.console.print(
        "  Re-mint expired sub-agent tokens with [bold]cinna doctor[/bold]."
    )


def _login_new_account(
    domain: str | None, machine_name: str, dir_name: str | None
) -> None:
    """Bootstrap path: connect a brand-new account workspace via the browser.

    Prompts for the platform domain when not given, picks where to create the
    workspace (the current folder when it's empty, otherwise a subfolder the
    user names), runs the device-login flow against that domain, and
    materializes a standard account workspace with the returned token.
    """
    console.status("No cinna account workspace here — let's connect a new one.")
    if not domain:
        domain = click.prompt("Platform domain to log in to (e.g. app.example.com)")
    platform_url = _normalize_platform_url(domain)

    cwd = Path.cwd()
    if dir_name:
        account_root = cwd / dir_name
    elif _dir_is_empty(cwd):
        account_root = cwd
    else:
        default_sub = default_account_dir_name(platform_url)
        sub = click.prompt(
            "This folder isn't empty — name a subfolder to create the account "
            "workspace in",
            default=default_sub,
        )
        account_root = cwd / sub

    if account_config_path(account_root).exists():
        raise click.ClickException(
            f"'{account_root}' already contains an account workspace.\n"
            f"Run 'cinna login' from inside it to refresh its token."
        )

    result = _device_login(platform_url, machine_name)

    config = AccountConfig(
        platform_url=result.get("platform_url") or platform_url,
        frontend_url=result.get("frontend_url") or platform_url,
        account_token=result["account_token"],
        machine_name=result.get("machine_name") or machine_name,
    )
    _write_account_files(config, account_root)
    with console.spinner("Downloading context package…"):
        _install_context_package(config, account_root)

    rel = account_root if account_root == cwd else account_root.relative_to(cwd)
    console.status(f"Account workspace ready at {account_root}")
    console.console.print()
    if account_root != cwd:
        console.console.print(f"  cd {rel}/")
    console.console.print(
        "  cinna account agents              # list agents you can build"
    )
    console.console.print(
        "  cinna agent sync <agent>          # attach an agent workspace under agents/"
    )
    console.console.print()


def run_login(
    domain: str | None = None,
    machine_name: str | None = None,
    dir_name: str | None = None,
) -> None:
    """`cinna login` — resume an account workspace, or connect a new one.

    Inside an existing account workspace it refreshes the stored token in place
    (the ``domain`` / ``dir_name`` hints are ignored). Otherwise it bootstraps a
    new account workspace: it asks for the platform domain (unless given),
    creates the workspace in the current folder when empty — or in a named
    subfolder when not — and signs in via the browser device flow. Either way no
    setup token is pasted.
    """
    try:
        account_root = find_account_root()
    except AccountConfigNotFoundError:
        account_root = None

    if account_root is not None:
        if domain or dir_name:
            console.warn(
                "Already inside an account workspace — refreshing it in place "
                "(domain / --dir ignored)."
            )
        _refresh_account_token_in_place(account_root)
        return

    _login_new_account(domain, machine_name or _fallback_machine_name(), dir_name)


def _fallback_machine_name() -> str:
    return f"{os.environ.get('USER', 'dev')}'s {platform.node()}"


# ── Command bodies ──────────────────────────────────────────────────────────


def _load_account_template() -> str:
    import importlib.resources

    return (
        importlib.resources.files("cinna.templates")
        .joinpath("ACCOUNT_CLAUDE.md.template")
        .read_text()
    )


def _write_account_claude_settings(account_root: Path) -> None:
    """Write ``.claude/settings.json`` pre-approving the ``cinna`` CLI.

    The orchestrator agent drives this workspace almost entirely through
    ``cinna`` subcommands; pre-approving ``Bash(cinna:*)`` removes a permission
    prompt on every call. ``enableAllProjectMcpServers`` auto-approves the
    cinna-managed ``.mcp.json`` servers (e.g. ``platform-knowledge``) so Claude
    Code doesn't prompt "New MCP server found in this project" on first launch,
    and the ``mcp__platform-knowledge`` allow rule pre-approves that server's
    tool calls (e.g. ``knowledge_query``) so each invocation doesn't prompt.
    Create-if-absent: never clobbers a user's own edits (so it is safe to call
    again from ``refresh-context``).
    """
    settings_path = account_root / ".claude" / "settings.json"
    if settings_path.exists():
        return
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings = {
        "enableAllProjectMcpServers": True,
        "permissions": {
            "allow": ["Bash(cinna:*)", "mcp__platform-knowledge"],
        },
    }
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")


def _write_account_mcp_config(account_root: Path) -> None:
    """Wire the account-level knowledge MCP proxy into the orchestrator agent.

    Writes ``.mcp.json`` (Claude Code) and ``opencode.json`` (opencode) that
    launch ``cinna mcp-proxy`` in **account mode** (``CINNA_ACCOUNT_CONFIG``
    points at ``.cinna/account.json``). The orchestrator agent then gets a
    ``knowledge_query`` tool that searches the platform knowledge base live via
    ``POST /account/knowledge/search`` — the account analogue of the per-agent
    workspace's knowledge tool. Auto-generated infra: overwritten on every
    ``cinna account setup`` / ``cinna account refresh-context``.

    The config path is written **relative** to the account root (anchored at the
    launch cwd, which MCP clients set to the workspace folder) so the folder can
    be moved without breaking the proxy. ``run_mcp_proxy`` additionally walks up
    from cwd, which self-heals older configs that stored an absolute path.
    """
    account_config = f"{CONFIG_DIR}/{ACCOUNT_CONFIG_FILE}"

    mcp_json = {
        "mcpServers": {
            "platform-knowledge": {
                "command": "cinna",
                "args": ["mcp-proxy"],
                "env": {"CINNA_ACCOUNT_CONFIG": account_config},
            }
        }
    }
    (account_root / ".mcp.json").write_text(json.dumps(mcp_json, indent=2) + "\n")

    opencode_json = {
        "mcp": {
            "platform-knowledge": {
                "type": "local",
                "command": ["cinna", "mcp-proxy"],
                "environment": {"CINNA_ACCOUNT_CONFIG": account_config},
                "enabled": True,
            }
        }
    }
    (account_root / "opencode.json").write_text(
        json.dumps(opencode_json, indent=2) + "\n"
    )


def _write_account_claude_md(account_root: Path, config: AccountConfig) -> None:
    """Render the orchestrator ``CLAUDE.md`` from the bundled template.

    Auto-generated and safe to overwrite (the file header says so), so both
    ``cinna account setup`` and ``cinna account refresh-context`` regenerate it —
    a refresh therefore picks up new commands / guidance shipped with a CLI
    upgrade without forcing a full re-setup.
    """
    from datetime import datetime, timezone

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    claude_md = (
        _load_account_template()
        .replace("{timestamp}", timestamp)
        .replace("{frontend_url}", config.frontend_url)
    )
    (account_root / "CLAUDE.md").write_text(claude_md)


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


def _write_account_files(config: AccountConfig, account_root: Path) -> None:
    """Create the account workspace dir + config + generated files (no context).

    The filesystem half of materializing an account workspace, shared by
    ``cinna account setup`` (paste a setup token) and ``cinna login`` (browser
    device flow). The caller downloads the context package separately so each
    can frame that slow, best-effort step in its own UI.
    """
    account_root.mkdir(parents=True, exist_ok=True)
    save_account_config(config, account_root)
    agents_dir(account_root).mkdir(exist_ok=True)
    _write_account_claude_md(account_root, config)
    _write_account_claude_settings(account_root)
    _write_account_mcp_config(account_root)


def run_account_setup(
    setup_input: str, machine_name: str, dir_name: str | None = None
) -> None:
    """Full account setup flow — called by `cinna account setup <token_or_url>`.

    When ``dir_name`` is not given (no ``--dir``), the folder name defaults to
    the platform domain normalized (e.g. ``demo-core_opencinna_io``); the user
    can accept it or type their own at the prompt.
    """
    total = 3

    # Parse before touching the filesystem / network so we can derive the
    # default folder name from the platform domain (and fail fast on bad input).
    platform_url, token = parse_account_setup_input(setup_input)

    if not dir_name:
        dir_name = _prompt_account_dir(default_account_dir_name(platform_url))

    # Guard the target directory before burning the single-use setup token.
    account_root = Path.cwd() / dir_name
    if account_config_path(account_root).exists():
        raise click.ClickException(
            f"Directory '{dir_name}/' already contains a cinna account workspace.\n"
            f"Run account commands from inside it, or choose another --dir."
        )

    # Step 1: Exchange the account setup token
    console.step(1, total, "Authenticating...")
    payload = _exchange_account_setup_token(platform_url, token, machine_name)

    # Step 2: Materialize the account workspace
    console.step(2, total, "Creating account workspace...")
    config = AccountConfig(
        platform_url=payload["platform_url"],
        frontend_url=payload.get("frontend_url") or payload["platform_url"],
        account_token=payload["account_token"],
        machine_name=payload.get("machine_name") or machine_name,
    )
    _write_account_files(config, account_root)

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
    """Re-download `context/` and regenerate `CLAUDE.md` — called by
    `cinna account refresh-context`.

    The old context tree is only removed after a successful download, so a
    failed refresh leaves the existing context intact (warn-don't-die). The
    orchestrator `CLAUDE.md` is re-rendered from the bundled template too — and
    so is every synced agent's per-agent `CLAUDE.md` under `agents/<slug>/` — so
    a CLI upgrade's new commands / guidance reach existing account workspaces
    (orchestrator and child agents alike) without a full re-setup.
    """
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with console.spinner("Refreshing context package..."):
        ok = _install_context_package(account_cfg, account_root, replace=True)
    if ok:
        console.status(f"Context refreshed under {account_root / 'context'}")

    # Always regenerate the auto-generated orchestrator guide (independent of
    # the context download — the template ships with the CLI, not the package).
    _write_account_claude_md(account_root, account_cfg)
    console.status("Orchestrator CLAUDE.md regenerated")

    # Self-heal the pre-approved-tools config if it was removed (never clobbers).
    _write_account_claude_settings(account_root)

    # Regenerate the knowledge MCP wiring so a CLI upgrade reaches existing
    # account workspaces (auto-generated infra — safe to overwrite).
    _write_account_mcp_config(account_root)

    # Re-render the per-agent CLAUDE.md for every synced child workspace from
    # the same bundled template — offline (the local-dev guide is a pure
    # function of the template + the agent's config), so one bad workspace never
    # aborts the rest. BUILDING_AGENT.md is left untouched (it mirrors the
    # platform's building prompt and is refreshed on sync, not from a template).
    from cinna.context import regenerate_claude_md

    refreshed = 0
    for child, child_cfg in list_child_workspaces(account_root):
        try:
            regenerate_claude_md(child_cfg, child)
            refreshed += 1
        except Exception as exc:  # one unreadable/locked workspace mustn't abort
            console.warn(f"Could not regenerate CLAUDE.md for {child.name}: {exc}")
    if refreshed:
        console.status(
            f"Regenerated CLAUDE.md for {refreshed} synced agent "
            f"workspace{'s' if refreshed != 1 else ''}"
        )


def run_account_agents(show_all: bool = False) -> None:
    """List accessible agents — called by `cinna account agents`.

    By default the listing is scoped to the account's **active user workspace**
    (the one chosen with `cinna account user-workspace activate`, stored in
    `.cinna/account.json`); `--all` shows every accessible agent across all
    workspaces. The header states exactly which workspace is being shown.
    """
    from rich.table import Table

    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with console.spinner("Fetching agents..."):
        with AccountClient(account_cfg) as client:
            # Always fetch the full set; scope to the active workspace below
            # (client-side) so the data is exact and resolvers stay unaffected.
            listing = client.list_account_agents()

    all_items = listing.get("data", [])

    active_id = account_cfg.user_workspace_id or None
    active_label = (
        account_cfg.user_workspace_name or active_id
        if active_id
        else "Default (unassigned)"
    )

    if show_all:
        items = all_items
        scope_line = f"Showing [bold]all agents[/bold] across all workspaces ({len(items)})"
    else:
        items = [
            a
            for a in all_items
            if (str(a["user_workspace_id"]) if a.get("user_workspace_id") else None)
            == (str(active_id) if active_id else None)
        ]
        scope_line = (
            f"Showing agents in workspace: [bold]{active_label}[/bold] "
            f"({len(items)} of {len(all_items)} accessible)"
        )

    console.console.print(scope_line)
    if not show_all:
        console.console.print(
            "[dim]Use --all to list agents across every workspace.[/dim]"
        )

    if not all_items:
        console.status("No accessible agents on this account.")
        return
    if not items:
        console.status(
            f"No agents in workspace '{active_label}'. "
            "Run with --all, or 'cinna account user-workspace activate <id>' "
            "to switch workspaces."
        )
        return

    children = list_child_workspaces(account_root)
    workspace_by_agent_id = {cfg.agent_id: path for path, cfg in children}

    table = Table(
        title=f"Accessible agents ({len(items)})",
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
    if account_cfg.user_workspace_id:
        ws_label = account_cfg.user_workspace_name or account_cfg.user_workspace_id
        table.add_row("Active workspace", ws_label)
    else:
        table.add_row("Active workspace", "[dim]Default[/dim]")
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
            agent = client.create_agent(
                name,
                description,
                user_workspace_id=account_cfg.user_workspace_id,
            )

    agent_id = agent.get("id", "?")
    agent_name = agent.get("name", name)
    agent_link = f"{account_cfg.frontend_url.rstrip('/')}/agent/{agent_id}"

    console.status(f"Agent created: {agent_name}")
    console.console.print(f"  Agent ID:  {agent_id}")
    console.console.print(f"  Web UI:    {agent_link}")
    if account_cfg.user_workspace_id:
        ws_label = account_cfg.user_workspace_name or account_cfg.user_workspace_id
        console.console.print(f"  Workspace: {ws_label}")
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


def _humanize_age(iso_ts: str | None) -> str | None:
    """Turn an ISO timestamp into a compact relative age (e.g. ``3m ago``)."""
    if not iso_ts:
        return None
    from datetime import datetime, timezone

    try:
        ts = datetime.fromisoformat(iso_ts)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = (datetime.now(timezone.utc) - ts).total_seconds()
    except (ValueError, TypeError):
        return None
    if delta < 0:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _print_agent_api_status(status: dict) -> None:
    """Render an agent-api status dict (from enable / refresh) for humans.

    ``State`` reflects the live *serving child*; ``Spec harvested`` dates the
    cached spec separately, so a stale spec is visible rather than masquerading
    as current (friction report A2/A4).
    """
    state = status.get("state", "?")
    enabled = status.get("agent_api_enabled")
    console.console.print(f"  Enabled:        {enabled}")
    console.console.print(f"  State:          {state}")
    console.console.print(f"  Spec available: {status.get('spec_available')}")
    age = _humanize_age(status.get("spec_fetched_at"))
    if age:
        console.console.print(f"  Spec harvested: {age} ({status['spec_fetched_at']})")
    if status.get("env_status"):
        console.console.print(f"  Env status:     {status['env_status']}")
    if status.get("last_error"):
        console.console.print(f"  Last error:     {status['last_error']}")


def run_agent_api_enable(agent_ref: str, enabled: bool) -> None:
    """Toggle a producer agent's REST API — `cinna agent-api enable`."""
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        agent = _resolve_account_agent(client.list_account_agents().get("data", []), agent_ref)
        verb = "Enabling" if enabled else "Disabling"
        with console.spinner(f"{verb} REST API..."):
            status = client.set_agent_api_enabled(agent["id"], enabled=enabled)

    console.status(
        f"REST API {'enabled' if enabled else 'disabled'} for {agent['name']}"
    )
    _print_agent_api_status(status)
    if enabled:
        console.console.print()
        console.console.print(
            "Author the API in the producer's workspace under "
            "agent_api/*.py (+ policy.yaml), sync it (cinna dev / cinna exec), "
            "then 'cinna agent-api refresh' and 'cinna agent-api spec' to verify."
        )


def run_agent_api_refresh(agent_ref: str) -> None:
    """Force a spec + policy re-harvest — `cinna agent-api refresh`."""
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        agent = _resolve_account_agent(client.list_account_agents().get("data", []), agent_ref)
        with console.spinner("Re-harvesting spec + policy..."):
            status = client.refresh_agent_api(agent["id"])

    console.status(f"Refreshed REST API for {agent['name']}")
    _print_agent_api_status(status)
    if status.get("last_error"):
        console.console.print()
        console.warn(
            "The harvest reported an error (see Last error above). Fix the "
            "agent_api/ code or policy.yaml, sync, and refresh again."
        )


def run_agent_api_spec(agent_ref: str, output: str | None) -> None:
    """Print (or save) a producer's harvested OpenAPI spec — `cinna agent-api spec`."""
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        agent = _resolve_account_agent(client.list_account_agents().get("data", []), agent_ref)
        with console.spinner("Fetching spec..."):
            spec = client.get_agent_api_spec(agent["id"])

    rendered = json.dumps(spec, indent=2)
    if output:
        Path(output).write_text(rendered + "\n")
        console.status(f"Spec written to {output}")
    else:
        # Plain stdout (no rich decoration) so it pipes / parses cleanly.
        click.echo(rendered)


def run_agent_api_call(
    agent_ref: str,
    method: str,
    path: str,
    query_pairs: tuple[str, ...],
    json_text: str | None,
) -> None:
    """Smoke-test one of a producer's own endpoints — `cinna agent-api call`.

    Hits the owner-preview proxy (no consumer token), so query params ARE
    forwarded — this catches a silent query-drop in seconds. Exit codes mirror
    `cinna api`: 0 for an inner 2xx, 1 for an inner 4xx/5xx (body still printed).
    """
    json_body = None
    if json_text is not None:
        try:
            json_body = json.loads(json_text)
        except json.JSONDecodeError as e:
            raise click.ClickException(f"--json is not valid JSON: {e}")

    query: dict[str, str | list[str]] = {}
    for pair in query_pairs:
        if "=" not in pair:
            raise click.ClickException(f"--query expects key=value, got '{pair}'.")
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
        agent = _resolve_account_agent(
            client.list_account_agents().get("data", []), agent_ref
        )
        with console.spinner(f"{method.upper()} {path}..."):
            result = client.call_agent_api(
                agent["id"], method.upper(), path, query=query or None, json_body=json_body
            )

    status_code = result.get("status_code", 0)
    console.console.print(f"→ {method.upper()} {path}  [{status_code}]")
    body = result.get("body", "")
    if result.get("is_json") and body:
        try:
            body = json.dumps(json.loads(body), indent=2)
        except (ValueError, TypeError):
            pass
    if body:
        click.echo(body)
    if not (200 <= status_code < 300):
        sys.exit(1)


def run_agent_restart_env(agent_ref: str) -> None:
    """Restart an agent's environment — `cinna agent restart-env`.

    The recovery path for a stuck env / poisoned producer API. Blocks until the
    container is back, then prints the post-restart status.
    """
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        agent = _resolve_account_agent(
            client.list_account_agents().get("data", []), agent_ref
        )

        # D2 guard: a restart re-materializes backend-managed scaffold files and
        # bounces the container — if this machine has unsynced local edits or
        # parked conflicts for the agent, the restart can clobber them. Warn (and
        # confirm) before proceeding so the builder can `cinna sync push` first.
        resolved = resolve_child_workspace(account_root, agent_ref)
        if resolved is not None:
            _child_root, child_cfg = resolved
            try:
                st = sync_session.status(child_cfg)
            except Exception:
                st = None
            if st is not None and st.exists and (
                st.pending_to_remote > 0 or st.conflict_count > 0
            ):
                bits = []
                if st.pending_to_remote > 0:
                    bits.append(f"{st.pending_to_remote} unsynced local change(s)")
                if st.conflict_count > 0:
                    bits.append(f"{st.conflict_count} conflict(s)")
                console.warn(
                    f"This machine has {' and '.join(bits)} for {agent['name']}. "
                    "A restart may overwrite them with the backend scaffold. "
                    "Run 'cinna sync push --agent "
                    f"{normalize_agent_dir_name(agent['name'])}' first to be safe."
                )
                if not click.confirm("Restart anyway?", default=False):
                    raise click.Abort()

        with console.spinner(f"Restarting environment for {agent['name']}..."):
            result = client.restart_agent_env(agent["id"])

    console.status(f"Environment restarted for {agent['name']}")
    console.console.print(f"  Status: {result.get('status')}")
    if result.get("status_message"):
        console.console.print(f"  Message: {result['status_message']}")


def _stdout_is_tty() -> bool:
    """Whether stdout is an interactive terminal (vs. piped/redirected)."""
    return sys.stdout.isatty()


def run_agent_show(
    agent_ref: str, prompts_only: bool, full: bool = False
) -> None:
    """Show an agent's effective config — `cinna agent show [--prompts]`.

    Prints the prompts the runtime actually reads, enabled features, and
    connected credential names/types (never secrets). Confirms "is what I
    edited actually live?" in one call.

    Long prompts are truncated for terminal readability. Pass ``full=True``
    (``--full``) to print them whole; truncation is also skipped automatically
    when stdout is not a TTY (e.g. piped or redirected to a file), so captured
    output is always complete.
    """
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        agent = _resolve_account_agent(
            client.list_account_agents().get("data", []), agent_ref
        )
        with console.spinner("Inspecting agent..."):
            info = client.inspect_agent(agent["id"])

    show_full = full or not _stdout_is_tty()

    console.status(f"{info.get('name')} ({info.get('id')})")
    prompts = info.get("prompts", {})
    console.console.print()
    console.console.print("Prompts (as the runtime reads them):")
    for label in ("entrypoint", "workflow", "refiner"):
        value = prompts.get(label)
        if value:
            if show_full or len(value) <= 2000:
                preview = value
            else:
                preview = value[:2000] + "\n…(truncated, pass --full for all)"
            console.console.print(f"  [{label}]")
            click.echo(preview)
        else:
            console.console.print(f"  [{label}] (empty)")

    if prompts_only:
        return

    console.console.print()
    console.console.print("Features:")
    for key, value in (info.get("features") or {}).items():
        console.console.print(f"  {key}: {value}")

    creds = info.get("credentials") or []
    console.console.print()
    console.console.print(f"Connected credentials ({len(creds)}):")
    for cred in creds:
        console.console.print(f"  - {cred.get('name')}  [{cred.get('type')}]")

    status = info.get("agent_api_status")
    if status:
        console.console.print()
        console.console.print("Agent REST API:")
        _print_agent_api_status(status)


# ── Schedules (full CRUD) ────────────────────────────────────────────────────


def _resolve_one_agent(client: "AccountClient", agent_ref: str) -> dict:
    """Resolve ``agent_ref`` against the cached account-agents listing."""
    return _resolve_account_agent(client.list_account_agents().get("data", []), agent_ref)


def _print_schedules(schedules: list[dict]) -> None:
    """Render a schedule listing as a table (mirrors `cinna account agents`)."""
    from rich.table import Table

    if not schedules:
        console.status("No schedules for this agent.")
        return

    table = Table(title=f"Schedules ({len(schedules)})", title_style="bold", show_lines=True)
    table.add_column("#", style="dim", justify="right")
    table.add_column("Name / id")
    table.add_column("Type")
    table.add_column("Cron (UTC)")
    table.add_column("Enabled")
    table.add_column("Next run (UTC)")

    for i, s in enumerate(schedules, 1):
        name_cell = f"[bold]{s.get('name', '?')}[/bold]\n[dim]{s.get('id', '?')}[/dim]"
        type_cell = (
            "[yellow]script[/yellow]"
            if s.get("schedule_type") == "script_trigger"
            else "static"
        )
        enabled_cell = (
            "[green]● on[/green]" if s.get("enabled") else "[dim]○ off[/dim]"
        )
        table.add_row(
            str(i),
            name_cell,
            type_cell,
            s.get("cron_string", "?"),
            enabled_cell,
            (s.get("next_execution") or "—"),
        )

    console.console.print(table)


def run_schedule_list(agent_ref: str) -> None:
    """List an agent's schedules — `cinna agent schedule list`."""
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with console.spinner("Fetching schedules..."):
        with AccountClient(account_cfg) as client:
            agent = _resolve_one_agent(client, agent_ref)
            data = client.list_schedules(agent["id"])

    console.console.print(f"Agent: [bold]{agent['name']}[/bold]")
    _print_schedules(data.get("data", []))


def run_schedule_generate(agent_ref: str, text: str, timezone: str, schedule_type: str) -> None:
    """NL → cron preview — `cinna agent schedule generate`."""
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with console.spinner("Generating cron from natural language..."):
        with AccountClient(account_cfg) as client:
            agent = _resolve_one_agent(client, agent_ref)
            result = client.generate_schedule(
                agent["id"], text, timezone, schedule_type=schedule_type
            )

    if not result.get("success"):
        raise click.ClickException(result.get("error") or "Could not generate a schedule.")

    console.status("Generated schedule (preview — nothing was saved):")
    console.console.print(f"  Cron (UTC):     {result.get('cron_string')}")
    console.console.print(f"  Description:    {result.get('description')}")
    console.console.print(f"  Next run (UTC): {result.get('next_execution')}")
    console.console.print()
    console.console.print(
        "Create it with: cinna agent schedule create "
        f"{agent_ref} --name <NAME> --cron '{result.get('cron_string')}' --tz UTC"
    )


def run_schedule_create(
    agent_ref: str,
    name: str,
    cron: str,
    timezone: str,
    schedule_type: str,
    prompt: str | None,
    command: str | None,
    description: str | None,
    enabled: bool,
) -> None:
    """Create a schedule — `cinna agent schedule create`."""
    if schedule_type == "script_trigger" and not (command and command.strip()):
        raise click.ClickException(
            "--command is required for a script_trigger schedule."
        )

    body: dict = {
        "name": name,
        "cron_string": cron,
        "timezone": timezone,
        # description is a required field server-side; default to the name.
        "description": description or name,
        "enabled": enabled,
        "schedule_type": schedule_type,
    }
    if prompt is not None:
        body["prompt"] = prompt
    if command is not None:
        body["command"] = command

    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        agent = _resolve_one_agent(client, agent_ref)
        with console.spinner("Creating schedule..."):
            created = client.create_schedule(agent["id"], body)

    console.status(f"Created schedule '{created.get('name')}' for {agent['name']}")
    console.console.print(f"  Id:             {created.get('id')}")
    console.console.print(f"  Cron (UTC):     {created.get('cron_string')}")
    console.console.print(f"  Type:           {created.get('schedule_type')}")
    console.console.print(f"  Enabled:        {created.get('enabled')}")
    console.console.print(f"  Next run (UTC): {created.get('next_execution')}")


def run_schedule_update(
    agent_ref: str,
    schedule_id: str,
    enabled: bool | None,
    name: str | None,
    cron: str | None,
    timezone: str | None,
    prompt: str | None,
    command: str | None,
    description: str | None,
) -> None:
    """Partial-update / toggle a schedule — `cinna agent schedule update`."""
    body: dict = {}
    if enabled is not None:
        body["enabled"] = enabled
    if name is not None:
        body["name"] = name
    if cron is not None:
        body["cron_string"] = cron
    if timezone is not None:
        body["timezone"] = timezone
    if prompt is not None:
        body["prompt"] = prompt
    if command is not None:
        body["command"] = command
    if description is not None:
        body["description"] = description

    if not body:
        raise click.ClickException(
            "Nothing to update. Pass --enable/--disable or a field "
            "(--name / --cron / --tz / --prompt / --command / --description)."
        )
    if "cron_string" in body and "timezone" not in body:
        raise click.ClickException("--tz is required when changing --cron.")

    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        agent = _resolve_one_agent(client, agent_ref)
        with console.spinner("Updating schedule..."):
            updated = client.update_schedule(agent["id"], schedule_id, body)

    console.status(f"Updated schedule '{updated.get('name')}'")
    console.console.print(f"  Enabled:        {updated.get('enabled')}")
    console.console.print(f"  Cron (UTC):     {updated.get('cron_string')}")
    console.console.print(f"  Next run (UTC): {updated.get('next_execution')}")


def run_schedule_run(agent_ref: str, schedule_id: str) -> None:
    """Run a schedule now — `cinna agent schedule run`."""
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        agent = _resolve_one_agent(client, agent_ref)
        with console.spinner("Triggering schedule..."):
            result = client.run_schedule(agent["id"], schedule_id)

    console.status(result.get("message", "Schedule triggered."))


def _print_schedule_logs(logs: list[dict]) -> None:
    """Render execution logs as a table."""
    from rich.table import Table

    if not logs:
        console.status("No execution logs yet for this schedule.")
        return

    table = Table(title=f"Execution logs ({len(logs)})", title_style="bold", show_lines=True)
    table.add_column("When (UTC)")
    table.add_column("Status")
    table.add_column("Exit")
    table.add_column("Detail")

    for log in logs:
        status = log.get("status", "?")
        if status == "success":
            status_cell = "[green]success[/green]"
        elif status == "session_triggered":
            status_cell = "[yellow]session_triggered[/yellow]"
        elif status == "error":
            status_cell = "[red]error[/red]"
        else:
            status_cell = status
        detail = (
            log.get("error_message")
            or log.get("command_executed")
            or log.get("prompt_used")
            or ""
        )
        if detail and len(detail) > 60:
            detail = detail[:57] + "..."
        exit_code = log.get("command_exit_code")
        table.add_row(
            log.get("executed_at", "?"),
            status_cell,
            "" if exit_code is None else str(exit_code),
            detail,
        )

    console.console.print(table)


def run_schedule_logs(agent_ref: str, schedule_id: str) -> None:
    """Show a schedule's execution logs — `cinna agent schedule logs`."""
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with console.spinner("Fetching execution logs..."):
        with AccountClient(account_cfg) as client:
            agent = _resolve_one_agent(client, agent_ref)
            data = client.schedule_logs(agent["id"], schedule_id)

    _print_schedule_logs(data.get("data", []))


def run_schedule_delete(agent_ref: str, schedule_id: str, yes: bool) -> None:
    """Delete a schedule — `cinna agent schedule delete`."""
    if not yes and not click.confirm(
        f"Delete schedule {schedule_id}?", default=False
    ):
        raise click.Abort()

    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        agent = _resolve_one_agent(client, agent_ref)
        with console.spinner("Deleting schedule..."):
            client.delete_schedule(agent["id"], schedule_id)

    console.status("Schedule deleted.")


# ── Status (access / refresh / set pre-command) ──────────────────────────────


def _print_agent_status(result: dict) -> None:
    """Render the combined status read (`{status, status_refresh_command}`)."""
    status = result.get("status") or {}
    severity = status.get("severity")
    summary = status.get("summary")
    sev_color = {
        "ok": "green",
        "warning": "yellow",
        "error": "red",
        "info": "cyan",
    }.get(severity or "", "dim")

    if severity is None and not status.get("raw"):
        console.console.print("  Status:         [dim]no STATUS.md published[/dim]")
    else:
        console.console.print(f"  Severity:       [{sev_color}]{severity or 'unknown'}[/{sev_color}]")
        if summary:
            console.console.print(f"  Summary:        {summary}")
        age = _humanize_age(status.get("reported_at"))
        if age:
            console.console.print(f"  Reported:       {age} ({status.get('reported_at')})")
        fetched = _humanize_age(status.get("fetched_at"))
        if fetched:
            console.console.print(f"  Fetched:        {fetched}")

    console.console.print(
        f"  Refresh cmd:    {result.get('status_refresh_command') or '[dim](none)[/dim]'}"
    )
    warning = status.get("refresh_command_warning")
    if warning:
        console.console.print()
        console.warn(warning)
    body = status.get("body")
    if body:
        console.console.print()
        console.console.print("[dim]── STATUS.md ──[/dim]")
        console.console.print(body)


def run_status_show(agent_ref: str, force_refresh: bool = False) -> None:
    """Show / refresh an agent's status — `cinna agent status show|refresh`."""
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    label = "Refreshing status..." if force_refresh else "Fetching status..."
    with console.spinner(label):
        with AccountClient(account_cfg) as client:
            agent = _resolve_one_agent(client, agent_ref)
            result = client.get_agent_status(agent["id"], force_refresh=force_refresh)

    console.console.print(f"Agent: [bold]{agent['name']}[/bold]")
    _print_agent_status(result)


def run_status_set_command(agent_ref: str, command: str) -> None:
    """Set the status-refresh pre-command — `cinna agent status set-command`."""
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        agent = _resolve_one_agent(client, agent_ref)
        with console.spinner("Saving status refresh command..."):
            result = client.set_status_refresh_command(agent["id"], command)

    console.status(f"Status refresh command set for {agent['name']}")
    console.console.print(
        f"  Refresh cmd: {result.get('status_refresh_command') or '(none)'}"
    )


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


# ── Active user workspace ───────────────────────────────────────────────────


_CLEAR_WORKSPACE_REFS = {"default", "none", "clear", ""}


def _resolve_account_workspace(items: list[dict], ref: str) -> dict:
    """Resolve ``ref`` (workspace id or name) against the workspace listing.

    Matches by workspace UUID, exact name, or case-insensitive name. Raises a
    ClickException listing the available workspaces when nothing matches, or the
    ambiguous matches when several share a name.
    """
    by_id = [w for w in items if w.get("id") == ref]
    if by_id:
        return by_id[0]

    ref_low = ref.strip().lower()
    matches = [w for w in items if (w.get("name") or "").lower() == ref_low]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        rows = ", ".join(f"{w.get('name')} ({w.get('id')})" for w in matches)
        raise click.ClickException(
            f"Workspace '{ref}' is ambiguous — matches: {rows}.\nUse the id instead."
        )

    available = ", ".join(w.get("name", "?") for w in items) or "none"
    raise click.ClickException(
        f"No workspace matches '{ref}'.\n"
        f"Available workspaces: {available}\n"
        f"Run 'cinna account user-workspace list' to see them, or 'default' to "
        f"target the Default workspace."
    )


def run_user_workspace_list() -> None:
    """List the account's workspaces, marking the active one — `... list`."""
    from rich.table import Table

    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with console.spinner("Fetching workspaces..."):
        with AccountClient(account_cfg) as client:
            listing = client.list_user_workspaces()

    items = listing.get("data", [])
    active_id = account_cfg.user_workspace_id

    table = Table(
        title=f"User workspaces ({listing.get('count', len(items))})",
        title_style="bold",
    )
    table.add_column("Active", justify="center")
    table.add_column("Workspace")
    table.add_column("ID", style="dim")

    # The implicit Default workspace (no row on the server) is always available.
    table.add_row(
        "[green]●[/green]" if not active_id else "",
        "Default [dim](unassigned)[/dim]",
        "—",
    )
    for w in items:
        is_active = w.get("id") == active_id
        table.add_row(
            "[green]●[/green]" if is_active else "",
            w.get("name", "?"),
            w.get("id", "?"),
        )

    console.console.print(table)
    console.console.print()
    console.console.print(
        "[dim]Set the active workspace with "
        "'cinna account user-workspace activate <name|id>' "
        "(or 'default' to clear). New agents and their credentials are created "
        "there.[/dim]"
    )


def run_user_workspace_activate(ref: str) -> None:
    """Set the active workspace — `cinna account user-workspace activate <ref>`.

    ``ref`` is a workspace name or id; ``default`` / ``none`` clears it to the
    Default (unassigned) workspace. The selection is stored client-side in
    ``.cinna/account.json``.
    """
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    if ref.strip().lower() in _CLEAR_WORKSPACE_REFS:
        run_user_workspace_clear()
        return

    with console.spinner("Resolving workspace..."):
        with AccountClient(account_cfg) as client:
            listing = client.list_user_workspaces()
    workspace = _resolve_account_workspace(listing.get("data", []), ref)

    account_cfg.user_workspace_id = workspace["id"]
    account_cfg.user_workspace_name = workspace.get("name")
    save_account_config(account_cfg, account_root)

    console.status(f"Active workspace set to '{workspace.get('name')}'.")
    console.console.print(
        "[dim]New agents (and the credentials they acquire) will be created in "
        "this workspace.[/dim]"
    )


def run_user_workspace_clear() -> None:
    """Clear the active workspace back to Default — `... activate default`."""
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    account_cfg.user_workspace_id = None
    account_cfg.user_workspace_name = None
    save_account_config(account_cfg, account_root)

    console.status("Active workspace cleared — new agents land in the Default workspace.")


# ── Credentials (drafts only — never secret values) ─────────────────────────


def _credential_status_cell(status: str | None) -> str:
    if status == "complete":
        return "[green]complete[/green]"
    if status == "incomplete":
        return "[yellow]needs setup[/yellow]"
    return "[dim]—[/dim]"


def run_credentials_list(workspace: str | None) -> None:
    """List the account's credentials (metadata only) — `... credentials list`."""
    from rich.table import Table

    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    # --workspace default → filter the Default (NULL) workspace; an id → that one.
    ws_filter: str | None = None
    if workspace is not None:
        ws_filter = "" if workspace.strip().lower() in _CLEAR_WORKSPACE_REFS else workspace

    with console.spinner("Fetching credentials..."):
        with AccountClient(account_cfg) as client:
            listing = client.list_credentials(user_workspace_id=ws_filter)

    items = listing.get("data", [])
    if not items:
        console.status("No credentials on this account.")
        return

    table = Table(
        title=f"Credentials ({listing.get('count', len(items))})",
        title_style="bold",
    )
    table.add_column("Name")
    table.add_column("Type", style="dim")
    table.add_column("Status")
    table.add_column("ID", style="dim")

    for c in items:
        table.add_row(
            c.get("name", "?"),
            c.get("type", "?"),
            _credential_status_cell(c.get("status")),
            c.get("id", "?"),
        )

    console.console.print(table)


def run_credentials_types() -> None:
    """List credential types + the fields the user must fill — `... types`."""
    from rich.table import Table

    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with console.spinner("Fetching credential types..."):
        with AccountClient(account_cfg) as client:
            listing = client.list_credential_types()

    table = Table(title="Credential types", title_style="bold", show_lines=True)
    table.add_column("Type")
    table.add_column("Required fields")
    table.add_column("Note", style="dim")

    for t in listing.get("data", []):
        fields = ", ".join(t.get("required_fields") or []) or "[dim]—[/dim]"
        table.add_row(t.get("type", "?"), fields, t.get("note") or "")

    console.console.print(table)


def run_credentials_create(
    name: str,
    cred_type: str,
    notes: str | None,
    service_uri: str | None,
    share: bool,
    workspace: str | None,
    agent_ref: str | None,
) -> None:
    """Create a draft credential — `cinna account credentials create`.

    The credential is created empty (no secret value); the user fills it in the
    UI. With ``--agent`` it is also attached to that agent in one step.
    """
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    # Default to the account's active workspace; --workspace overrides
    # ('default'/'none' → Default workspace).
    user_workspace_id: str | None = account_cfg.user_workspace_id
    if workspace is not None:
        user_workspace_id = (
            None if workspace.strip().lower() in _CLEAR_WORKSPACE_REFS else workspace
        )

    with AccountClient(account_cfg) as client:
        with console.spinner("Creating draft credential..."):
            result = client.create_credential(
                name,
                cred_type,
                notes=notes,
                service_uri=service_uri,
                allow_sharing=share,
                user_workspace_id=user_workspace_id,
            )

        credential = result.get("credential", {})
        cred_id = credential.get("id", "?")
        required = result.get("required_fields") or []
        setup_url = result.get("setup_url", "")

        attached_to: str | None = None
        if agent_ref is not None:
            listing = client.list_account_agents()
            agent = _resolve_account_agent(listing.get("data", []), agent_ref)
            with console.spinner(f"Attaching to {agent['name']}..."):
                client.share_credential_with_agent(cred_id, agent["id"])
            attached_to = agent["name"]

    console.status(f"Draft credential created: {credential.get('name', name)}")
    console.console.print(f"  Credential ID:  {cred_id}")
    console.console.print(f"  Type:           {credential.get('type', cred_type)}")
    console.console.print(f"  Status:         {_credential_status_cell(credential.get('status'))}")
    if attached_to:
        console.console.print(f"  Attached to:    {attached_to}")
    console.console.print()
    if required:
        console.console.print(
            "[bold]The user must fill these fields[/bold] (the CLI cannot set "
            "secret values):"
        )
        for field in required:
            console.console.print(f"    • {field}")
    else:
        console.console.print(
            "[dim]This type has no fixed required fields — the user completes it "
            "in the UI.[/dim]"
        )
    if setup_url:
        console.console.print()
        console.console.print(f"  Fill it in:     {setup_url}")


def run_credentials_update(
    credential_id: str,
    name: str | None,
    notes: str | None,
    service_uri: str | None,
    share: bool | None,
) -> None:
    """Update credential metadata (never a secret) — `... credentials update`."""
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    fields: dict = {}
    if name is not None:
        fields["name"] = name
    if notes is not None:
        fields["notes"] = notes
    if service_uri is not None:
        fields["service_uri"] = service_uri
    if share is not None:
        fields["allow_sharing"] = share
    if not fields:
        raise click.ClickException(
            "Nothing to update — pass at least one of --name / --notes / "
            "--service-uri / --share / --no-share."
        )

    with console.spinner("Updating credential..."):
        with AccountClient(account_cfg) as client:
            credential = client.update_credential(credential_id, fields)

    console.status(f"Credential updated: {credential.get('name', credential_id)}")
    console.console.print(
        f"  Status:  {_credential_status_cell(credential.get('status'))}"
    )


def run_credentials_delete(credential_id: str, force: bool, yes: bool) -> None:
    """Delete a credential — `cinna account credentials delete`.

    Reuses the platform's blast-radius gate: a Tier 2 delete (publisher-provided
    in a published bundle with active installs) is refused with 409 unless
    ``--force``.
    """
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    if not yes:
        console.warn(
            f"This will delete credential {credential_id} and unlink it from any "
            f"agents using it."
        )
        if not click.confirm("Continue?"):
            raise click.Abort()

    with console.spinner("Deleting credential..."):
        with AccountClient(account_cfg) as client:
            client.delete_credential(credential_id, force=force)

    console.status("Credential deleted.")


def run_credentials_share(credential_id: str, agent_ref: str) -> None:
    """Attach a credential to an agent — `... credentials share-with-agent`."""
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        listing = client.list_account_agents()
        agent = _resolve_account_agent(listing.get("data", []), agent_ref)
        with console.spinner(f"Attaching credential to {agent['name']}..."):
            client.share_credential_with_agent(credential_id, agent["id"])

    console.status(f"Credential attached to '{agent['name']}'.")
    console.console.print(
        "[dim]Once the user fills the credential's secret in the UI, it syncs "
        "into the agent's environment automatically.[/dim]"
    )
