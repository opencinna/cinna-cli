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
    config_from_payload,
    normalize_agent_dir_name,
    persist_config,
    prepare_git_layout,
    provision_workspace,
    remove_workspace_artifacts,
    resolve_clone_slug,
    workspace_agent_id_at,
    _maybe_autolink,
)
from cinna.cli_version import cli_version_hint, cli_version_label, cli_version_status
from cinna.client import AccountClient, PlatformClient
from cinna.config import (
    CONFIG_DIR,
    CinnaConfig,
    load_config,
    remove_agent_registry,
)
from cinna.errors import (
    AccountConfigNotFoundError,
    AccountMismatchError,
    CinnaExit,
    CodedRefusal,
    NetworkError,
    PlatformError,
    SetupTokenError,
    WorkspaceExistsError,
)

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


def parse_account_setup_input(
    raw_input: str, fallback_platform_url: str | None = None
) -> tuple[str, str]:
    """Parse account setup input into (platform_url, token).

    Accepts any of (paste directly from Settings → Local Development):
      - Full curl command: 'curl -sL http://host/api/cli-setup/account/TOKEN | python3 -'
      - URL:               'http://host/api/cli-setup/account/TOKEN'
      - Raw token:         'TOKEN' (falls back to ``fallback_platform_url``,
                           then the CINNA_PLATFORM_URL env var)

    The returned ``platform_url`` is the base the exchange endpoint hangs off
    (``…/api`` for the curl / URL forms).
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

    platform_url = fallback_platform_url or os.environ.get("CINNA_PLATFORM_URL", "")
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
    terminal attached (CI, captured output) — or under ``--no-input`` — we
    return ``default`` unchanged so non-interactive runs stay non-interactive.
    """
    if console.no_input:
        return default
    if sys.stdin.isatty():
        return console.prompt("Workspace folder name", default=default)

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
    are surfaced verbatim. Failures carry the stable exit codes a driver
    switches on: any 4xx (invalid / expired / already-used token, wrong
    token kind) → ``SetupTokenError`` (exit 10); 5xx → ``PlatformError``
    (exit 12); no connection at all → ``NetworkError`` (exit 12).
    """
    setup_url = f"{platform_url.rstrip('/')}/cli-setup/account/{token}"
    machine_info = f"{platform.system()}/{platform.machine()}"
    logger.info("Exchanging account setup token at %s", setup_url)

    try:
        response = httpx.post(
            setup_url,
            json={"machine_name": machine_name, "machine_info": machine_info},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise NetworkError(platform_url, exc) from exc
    logger.debug("Setup response: %s %s", response.status_code, response.text[:500])
    if response.status_code != 200:
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text
        if response.status_code >= 500:
            raise PlatformError(response.status_code, detail)
        raise SetupTokenError(detail, response.status_code)
    return response.json()


# ── Child workspace resolution ──────────────────────────────────────────────


# Dirs never worth descending into when hunting for an agent's ``.cinna/`` —
# they're large and never contain a nested agent config.
_AGENT_SCAN_PRUNE = {
    "workspace",
    ".git",
    CONFIG_DIR,
    "node_modules",
    ".venv",
    "__pycache__",
}
_AGENT_SCAN_MAX_DEPTH = 8


def _find_agent_dirs_under(clone_root: Path):
    """Yield every agent dir (folder holding ``.cinna/config.json``) under a clone.

    The Model-A subdir can be **multi-segment** (e.g. ``agents/localhost/hello``),
    so the config can sit several levels below the clone root. Walk down, pruning
    heavy/irrelevant subtrees, and stop descending once an agent dir is found
    (an agent dir never nests another).
    """

    def walk(d: Path, depth: int):
        if depth > _AGENT_SCAN_MAX_DEPTH:
            return
        if (d / CONFIG_DIR / "config.json").is_file():
            yield d
            return
        try:
            entries = sorted(d.iterdir())
        except OSError:
            return
        for e in entries:
            if e.is_dir() and e.name not in _AGENT_SCAN_PRUNE:
                yield from walk(e, depth + 1)

    yield from walk(clone_root, 0)


def _iter_agent_dirs(base: Path):
    """Yield agent dirs (the folder holding ``.cinna/``) under ``agents/``.

    Handles the legacy flat layout (``agents/<slug>/.cinna/``) and the Model-A
    nested layout with an arbitrarily deep, possibly multi-segment subdir
    (``agents/<slug>/<a>/<b>/.cinna/``).
    """
    if not base.is_dir():
        return
    for child in sorted(base.iterdir()):
        if child.is_dir():
            yield from _find_agent_dirs_under(child)


def list_child_workspaces(account_root: Path) -> list[tuple[Path, CinnaConfig]]:
    """Return every synced per-agent workspace under ``agents/``."""
    result: list[tuple[Path, CinnaConfig]] = []
    for child in _iter_agent_dirs(agents_dir(account_root)):
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


# ── Context package freshness ───────────────────────────────────────────────

CONTEXT_VERSION_FILE = "VERSION"


def local_context_package_version(account_root: Path) -> str | None:
    """The version the workspace's ``context/`` tree was extracted at.

    ``None`` means either no context package at all or one installed before the
    platform started stamping ``context/VERSION`` — both are "refresh me".
    """
    path = account_root / "context" / CONTEXT_VERSION_FILE
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def context_package_status(
    config: AccountConfig, account_root: Path, client: "AccountClient | None" = None
) -> tuple[str, str | None, str | None]:
    """Classify the workspace's context package: (state, local, remote).

    States: ``current``, ``stale``, ``unknown`` (nothing stamped locally), and
    ``unreachable`` (the platform could not be asked — never an error, the
    package is advisory). Guides ship in this tree, so a workspace that never
    refreshes silently lacks whole playbooks; this is what makes that visible.
    """
    local = local_context_package_version(account_root)
    try:
        if client is not None:
            remote = client.get_context_package_version()
        else:
            with AccountClient(config) as owned:
                remote = owned.get_context_package_version()
    except Exception as exc:  # noqa: BLE001 — advisory, never fatal
        logger.debug("Context package version check failed: %s", exc)
        return "unreachable", local, None
    if remote is None:
        return "unreachable", local, None
    if local is None:
        return "unknown", local, remote
    return ("current" if local == remote else "stale"), local, remote


def context_package_hint(state: str) -> str | None:
    """The one-line nudge shown when the local package is behind."""
    if state == "stale":
        return (
            "[yellow]![/yellow] Your context/ package is out of date — "
            "run 'cinna account refresh-context' to pick up new guides."
        )
    if state == "unknown":
        return (
            "[yellow]![/yellow] This workspace has no context package version — "
            "run 'cinna account refresh-context' to install the current guides."
        )
    return None


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
            "  cinna account set-token <token>   (inside this account workspace)\n"
            "or 'cinna account setup <token>' in a fresh directory."
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
        domain = console.prompt("Platform domain to log in to (e.g. app.example.com)")
    platform_url = _normalize_platform_url(domain)

    cwd = Path.cwd()
    if dir_name:
        account_root = cwd / dir_name
    elif _dir_is_empty(cwd):
        account_root = cwd
    else:
        default_sub = default_account_dir_name(platform_url)
        sub = console.prompt(
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

    # Companion testing guide (loaded by the orchestrator only when it tests an
    # agent via `cinna chat`). Bundled + overwritten, like the orchestrator guide.
    from cinna.context import write_chat_testing_guide

    write_chat_testing_guide(account_root)


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


def resolve_account_dir(dir_name: str) -> Path:
    """Where ``--dir`` points: an absolute path is used **as is**, a relative
    one lands under the current directory. ``~`` is expanded. This is a
    contract (Cinna Desktop passes ``<AgentsHome>/Cloud/<host>``), not a
    pathlib accident — do not resolve symlinks here, the caller compares the
    path it passed with the one reported back.
    """
    target = Path(dir_name).expanduser()
    return target if target.is_absolute() else Path.cwd() / target


def run_account_setup(
    setup_input: str, machine_name: str, dir_name: str | None = None
) -> None:
    """Full account setup flow — called by `cinna account setup <token_or_url>`.

    When ``dir_name`` is not given (no ``--dir``), the folder name defaults to
    the platform domain normalized (e.g. ``demo-core_opencinna_io``); the user
    can accept it or type their own at the prompt. With ``--dir`` the target
    follows ``resolve_account_dir`` (absolute as is, relative under cwd) and
    missing parents are created.
    """
    total = 3

    # Parse before touching the filesystem / network so we can derive the
    # default folder name from the platform domain (and fail fast on bad input).
    platform_url, token = parse_account_setup_input(setup_input)

    if not dir_name:
        dir_name = _prompt_account_dir(default_account_dir_name(platform_url))

    # Guard the target directory before burning the single-use setup token.
    account_root = resolve_account_dir(dir_name)
    if account_config_path(account_root).exists():
        raise WorkspaceExistsError(
            f"Directory '{account_root}' already contains a cinna account workspace.\n"
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
    context_ok = _install_context_package(config, account_root)

    console.status("Account workspace created!")
    console.emit_result(**_account_result_fields(config, account_root, context_ok))
    console.console.print()
    cd_target = account_root if Path(dir_name).is_absolute() else dir_name
    console.console.print(f"  cd {cd_target}/")
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


def _account_result_fields(
    config: AccountConfig, account_root: Path, context_ok: bool | None
) -> dict:
    """The ``--json`` final-line fields shared by ``account setup`` and
    ``account set-token``. ``context_ok`` None means the step was skipped."""
    if context_ok is None:
        context_package = "skipped"
    else:
        context_package = "ok" if context_ok else "failed"
    return {
        "workspace": str(account_root),
        "platform_url": config.platform_url,
        "frontend_url": config.frontend_url,
        "machine_name": config.machine_name,
        "context_package": context_package,
    }


def _jwt_claims(token: str) -> dict | None:
    """Best-effort, *unverified* decode of a JWT's payload segment.

    Used only to compare the ``sub`` of the stored and the freshly exchanged
    account token — the CLI never trusts these claims for anything else, and
    a token that is not a JWT simply yields ``None`` (no comparison).
    """
    import base64

    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _same_origin(a: str, b: str) -> bool:
    pa, pb = urlparse(a.strip()), urlparse(b.strip())
    return (pa.scheme, pa.netloc.lower()) == (pb.scheme, pb.netloc.lower())


def run_account_set_token(setup_input: str) -> None:
    """Swap a fresh account token into the current account workspace — called
    by `cinna account set-token <token_or_url>`.

    The account counterpart of the per-agent ``run_set_token``: the exchange
    runs under the **stored** machine name, and only ``account_token`` (plus a
    server-refreshed ``platform_url`` / ``frontend_url``) is rewritten. The
    active user workspace, the machine name, ``context/`` and the children
    under ``agents/`` are untouched — the same in-place contract ``cinna
    login`` gives. Refuses to write when the new token is for a different
    account (platform origin differs, or both tokens carry a ``sub`` claim
    and they differ) — exit 11, ``account_mismatch``.
    """
    total = 2
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    # The stored platform_url is the bare origin; the exchange lives under
    # /api, so offer that as the bare-token fallback (like the per-agent verb).
    stored_base = account_cfg.platform_url.rstrip("/")
    fallback = stored_base if stored_base.endswith("/api") else f"{stored_base}/api"
    platform_url, token = parse_account_setup_input(
        setup_input, fallback_platform_url=fallback
    )

    console.step(1, total, "Authenticating...")
    payload = _exchange_account_setup_token(
        platform_url, token, account_cfg.machine_name
    )

    new_platform = payload.get("platform_url") or account_cfg.platform_url
    if not _same_origin(new_platform, account_cfg.platform_url):
        raise AccountMismatchError(
            f"Token belongs to a different platform ({new_platform}) than this "
            f"workspace ({account_cfg.platform_url}). Run 'cinna account setup' "
            f"in a new directory to connect it."
        )
    old_sub = (_jwt_claims(account_cfg.account_token) or {}).get("sub")
    new_sub = (_jwt_claims(payload["account_token"]) or {}).get("sub")
    if old_sub and new_sub and old_sub != new_sub:
        raise AccountMismatchError(
            "Token belongs to a different account than this workspace. "
            "Run 'cinna account setup' in a new directory to connect it."
        )

    console.step(2, total, "Updating account workspace...")
    account_cfg.account_token = payload["account_token"]
    account_cfg.platform_url = new_platform
    if payload.get("frontend_url"):
        account_cfg.frontend_url = payload["frontend_url"]
    save_account_config(account_cfg, account_root)

    console.status(f"Account token refreshed for {account_cfg.machine_name}.")
    console.emit_result(**_account_result_fields(account_cfg, account_root, None))
    console.console.print(
        "  Re-mint expired sub-agent tokens with [bold]cinna doctor[/bold]."
    )


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
    # `fold`, not the default `ellipsis`: this cell carries the agent's UUID on
    # its second line, and an id that renders as `f0506e24-3740-4fe3…` is a
    # copy-paste trap — the API answers "404 not found" for it, which reads as
    # a missing agent rather than a truncated id.
    table.add_column("Agent", overflow="fold")
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

        # Which *kind* of bundle install this is decides where a fix has to
        # land — the publisher install is the only copy a fix can be published
        # from. `cinna improve` sends the reader here to establish that.
        if item.get("is_publisher_install"):
            build_cell += "\n[dim]publisher install[/dim]"

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
    pkg_state, pkg_local, pkg_remote = context_package_status(account_cfg, account_root)
    cli_status = cli_version_status(account_cfg.platform_url)

    if console.json_mode:
        console.emit_result(
            workspace=str(account_root),
            platform_url=account_cfg.platform_url,
            frontend_url=account_cfg.frontend_url,
            machine_name=account_cfg.machine_name,
            active_workspace=(
                {
                    "id": account_cfg.user_workspace_id,
                    "name": account_cfg.user_workspace_name,
                }
                if account_cfg.user_workspace_id
                else None
            ),
            token=token_status,
            synced_agents=len(children),
            agents=[
                {
                    "agent_id": cfg.agent_id,
                    "name": cfg.agent_name,
                    "path": str(path),
                    "last_sync_connected_at": cfg.last_sync_connected_at,
                }
                for path, cfg in children
            ],
            context_package={
                "local": pkg_local,
                "remote": pkg_remote,
                "state": pkg_state,
            },
            cli=cli_status,
        )
        return

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

    if pkg_state == "current":
        pkg_cell = f"[green]{pkg_local}[/green] (up to date)"
    elif pkg_state == "stale":
        pkg_cell = f"[yellow]{pkg_local} → {pkg_remote} available[/yellow]"
    elif pkg_state == "unknown":
        pkg_cell = "[yellow]not stamped — refresh to install current guides[/yellow]"
    else:
        pkg_cell = "[dim]unknown (platform unreachable)[/dim]"
    table.add_row("Context package", pkg_cell)
    if cli_status.get("editable"):
        # Not a version at all: an editable install's metadata is a snapshot of
        # the day it was installed, so pinning it against the platform's number
        # would report skew that does not exist. Say what it is and name the
        # pin as context, not as a target.
        cli_cell = f"[dim]{cli_version_label(cli_status)}[/dim]"
        if cli_status.get("required"):
            cli_cell += f" [dim](platform pins {cli_status['required']})[/dim]"
    elif cli_status["state"] == "current":
        cli_cell = f"[green]{cli_status['installed']}[/green] (matches platform pin)"
    elif cli_status["state"] == "behind":
        cli_cell = (
            f"[yellow]{cli_status['installed']} → {cli_status['required']} "
            f"pinned by platform[/yellow]"
        )
    elif cli_status["state"] == "ahead":
        cli_cell = (
            f"{cli_status['installed']} [dim](platform pins "
            f"{cli_status['required']})[/dim]"
        )
    else:
        cli_cell = f"{cli_status['installed']} [dim](no platform pin)[/dim]"
    table.add_row("cinna-cli", cli_cell)

    console.console.print(table)

    if children:
        console.console.print()
        console.console.print(_synced_agents_table(account_root, children))

    hint = context_package_hint(pkg_state)
    if hint:
        console.console.print()
        console.console.print(hint)
    version_hint = cli_version_hint(cli_status)
    if version_hint:
        console.console.print()
        console.console.print(f"[yellow]![/yellow] {version_hint}")

    _print_token_reauth_hint(token_status)


def _synced_agents_table(account_root: Path, children: list[tuple[Path, CinnaConfig]]):
    """Build a table of the per-agent workspaces synced under ``agents/``.

    One row per child workspace, showing its display name, template, agent id,
    workspace path (relative to the account root for brevity), and the last time
    sync connected when that's recorded in the child config.
    """
    from rich.table import Table

    agents = Table(title="Synced agents")
    agents.add_column("Agent")
    agents.add_column("Template", style="dim")
    agents.add_column("Agent ID", style="dim", overflow="fold")
    agents.add_column("Path")
    agents.add_column("Last sync", style="dim")

    for path, cfg in children:
        try:
            rel = path.relative_to(account_root)
        except ValueError:
            rel = path
        agents.add_row(
            cfg.agent_name or "—",
            cfg.template or "—",
            cfg.agent_id or "—",
            str(rel),
            cfg.last_sync_connected_at or "—",
        )
    return agents


def _print_token_reauth_hint(token_status: str) -> None:
    """Nudge the user to re-authenticate when the account token isn't valid.

    Shared by ``cinna account status`` and ``cinna status`` (which falls back to
    the account view inside an account workspace). ``cinna login`` refreshes the
    token in place without a pasted setup token.
    """
    if token_status == "expired":
        console.console.print(
            "\n[red]Account token has expired.[/red] Re-authenticate with:\n"
            "  [bold]cinna login[/bold]\n"
            "or paste a new setup token from Settings → Local Development:\n"
            "  [bold]cinna account set-token <token>[/bold]"
        )
    elif token_status == "unreachable":
        console.console.print(
            "\n[yellow]Could not reach the platform to verify the token.[/yellow]\n"
            "If this persists, re-authenticate with:  [bold]cinna login[/bold]"
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
        # Resolve the clone-root name the same way the bootstrap will (slug, or
        # slug-<hash> when another agent already owns the slug), then refuse only
        # when THIS agent is already synced there.
        clone_slug = resolve_clone_slug(
            agents_dir(account_root), dir_name, item["id"]
        )
        clone_candidate = agents_dir(account_root) / clone_slug
        if workspace_agent_id_at(clone_candidate) == item["id"]:
            raise click.ClickException(
                f"'{AGENTS_DIR}/{clone_slug}/' is already a synced workspace.\n"
                f"Run 'cinna agent unsync {clone_slug}' first, or use "
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
    config = config_from_payload(payload)
    agent_client = PlatformClient(config)
    try:
        # Same Model-A nested layout + auto-link as `cinna setup`, rooted under
        # the account workspace's agents/ dir.
        _clone_root, workspace_root, coords = prepare_git_layout(
            config, agent_client, agents_dir(account_root)
        )
        workspace_root.mkdir(parents=True, exist_ok=True)
        persist_config(config, workspace_root)
        console.status(f"Minted CLI token for agent: {config.agent_name}")

        # Steps 2-4: standard per-agent provisioning (same as `cinna setup`)
        provision_workspace(
            agent_client,
            config,
            workspace_root,
            interactive=console.interactive(),
            total=total,
            first_step=2,
        )
        workspace_root = _maybe_autolink(config, agent_client, workspace_root, coords)
    finally:
        agent_client.close()

    session_started = _establish_sync_session(config, workspace_root, dir_name)

    rel_dir = workspace_root.relative_to(account_root)
    console.status(f"Agent synced under {rel_dir}/")
    console.console.print()
    console.console.print(f"  cd {rel_dir}/")
    console.console.print(
        "  cinna dev                         # start a foreground dev session"
    )
    if session_started:
        console.console.print(
            f"  cinna sync push --agent {dir_name}   # after editing, from the account root"
        )
    console.console.print(
        f"  cinna exec --agent {dir_name} <cmd>   # or exec from the account root"
    )
    console.console.print()


def _establish_sync_session(config: CinnaConfig, workspace_root: Path, ref: str) -> bool:
    """Start the agent's sync session while local and remote are one clone.

    Mutagen reconciles against the last state both sides agreed on. A session
    first created by a later ``cinna sync push`` has no such state, so every
    file edited since the clone looks changed on both sides — and the first
    push reported the builder's own edits as conflicts against untouched
    remote originals. Starting (and flushing) the session here, before any
    edit, gives it that baseline.

    Best-effort: the workspace is complete without it, so a failure is a
    warning naming the command that starts it later, never a failed sync.
    """
    try:
        with console.spinner("Starting the sync session..."):
            sync_session.ensure_session(config, workspace_root)
            st = sync_session.flush(config)
    except Exception as exc:  # noqa: BLE001 — mutagen / transport, all non-fatal here
        logger.warning("sync session not started after agent sync: %s", exc)
        detail = exc.format_message() if isinstance(exc, click.ClickException) else str(exc)
        console.warn(
            f"The sync session did not start ({detail.splitlines()[0] if detail else exc}). "
            f"Run 'cinna sync push --agent {ref}' BEFORE editing files, or the "
            "first push reports your edits as conflicts."
        )
        return False
    if st.conflict_count:
        console.warn(
            f"The sync session started with {st.conflict_count} conflict(s) — see "
            f"'cinna sync conflicts --agent {ref} --diff'."
        )
    else:
        console.status("Sync session started — later edits sync as plain changes.")
    return True


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
    if not console.confirm("Continue?"):
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
                if not console.confirm("Restart anyway?", default=False):
                    raise click.Abort()

        with console.spinner(f"Restarting environment for {agent['name']}..."):
            result = client.restart_agent_env(agent["id"])

    console.status(f"Environment restarted for {agent['name']}")
    console.console.print(f"  Status: {result.get('status')}")
    if result.get("status_message"):
        console.console.print(f"  Message: {result['status_message']}")


def run_agent_rebuild_env(agent_ref: str, yes: bool = False) -> None:
    """Rebuild an agent's environment — `cinna agent rebuild-env`.

    The fix for a container that predates a feature. A restart re-runs the same
    image and cannot add a route that was never built into it; a rebuild
    replaces ``/app/core`` from the template. Blocks for the whole rebuild.

    Confirms first (unless ``yes``): this recreates the container and takes
    minutes, which is a heavier thing than the restart sitting next to it in
    ``--help``.
    """
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        agent = _resolve_account_agent(
            client.list_account_agents().get("data", []), agent_ref
        )

        # Same D2 guard as restart-env, and it matters more here: a rebuild
        # re-materializes the backend scaffold over a freshly recreated
        # container, so unsynced local edits have further to fall.
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
                    "A rebuild may overwrite them with the backend scaffold. "
                    "Run 'cinna sync push --agent "
                    f"{normalize_agent_dir_name(agent['name'])}' first to be safe."
                )
                if not console.confirm("Rebuild anyway?", default=False):
                    raise click.Abort()

        if not yes and not console.confirm(
            f"Rebuild {agent['name']}'s environment? This recreates the "
            "container and takes a few minutes.",
            default=False,
        ):
            raise click.Abort()

        with console.spinner(
            f"Rebuilding environment for {agent['name']} (this takes a few "
            "minutes)..."
        ):
            result = client.rebuild_agent_env(agent["id"])

        console.status(f"Environment rebuilt for {agent['name']}")
        console.console.print(f"  Status: {result.get('status')}")
        if result.get("status_message"):
            console.console.print(f"  Message: {result['status_message']}")

        # A rebuild restores the state it found. An environment that was stopped
        # comes back stopped — a success, but not one the user can act on yet, and
        # saying nothing here is how "rebuilt successfully" becomes a container
        # that still answers nothing.
        if not result.get("was_running") and result.get("status") != "running":
            console.console.print(
                "  [dim]It was not running before the rebuild, so it was left "
                "stopped. Send it a message, or refresh its addons, to start "
                "it.[/dim]"
            )
            return

        # "Rebuilt" and "running" are row states; the server inside the new
        # container can still be starting. A chat sent into that window sat
        # streaming with no reply, so wait for the health check to answer and
        # say plainly when it has not.
        env_id = result.get("environment_id")
        if not isinstance(env_id, str) or not env_id:
            return
        with console.spinner("Waiting for the environment to answer..."):
            answered = _wait_for_env_health(client, env_id)
    if answered is True:
        console.status("The environment answers its health check — ready to chat.")
    elif answered is False:
        console.warn(
            f"The environment has not answered its health check after "
            f"{int(ENV_HEALTH_WAIT_SECONDS)}s. A message sent now may sit "
            f"unanswered — check again with 'cinna agent status refresh "
            f"{normalize_agent_dir_name(agent['name'])}' before 'cinna chat'."
        )


ENV_HEALTH_WAIT_SECONDS = 180.0
ENV_HEALTH_POLL_SECONDS = 3.0


def _wait_for_env_health(client: "AccountClient", environment_id: str) -> bool | None:
    """Poll the environment's health route until its server answers.

    ``True`` once it reports healthy, ``False`` after
    ``ENV_HEALTH_WAIT_SECONDS`` without that, ``None`` when the route's answer
    is not something this build can read — an unknown shape is not evidence of
    an unhealthy container, so it is reported as nothing rather than a warning.
    """
    deadline = time.monotonic() + ENV_HEALTH_WAIT_SECONDS
    while True:
        try:
            health = client.get_environment_health(environment_id)
        except (PlatformError, httpx.TransportError) as exc:
            logger.debug("health check not answering yet: %s", exc)
            health = {}
        if not isinstance(health, dict):
            return None
        if str(health.get("status", "")).lower() in ("healthy", "ok"):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(ENV_HEALTH_POLL_SECONDS)


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
            linked = None if prompts_only else _fetch_linked_credentials(client, agent["id"])

    show_full = full or not _stdout_is_tty()

    console.status(f"{info.get('name')} ({info.get('id')})")
    prompts = info.get("prompts", {})
    console.console.print()
    console.console.print("Prompts (as the runtime reads them):")
    for label in ("entrypoint", "workflow", "refiner"):
        value = prompts.get(label)
        # The label is escaped: Rich reads a bare `[workflow]` as a style tag
        # and drops it, which printed every prompt with no name above it.
        if value:
            if show_full or len(value) <= 2000:
                preview = value
            else:
                preview = value[:2000] + "\n…(truncated, pass --full for all)"
            console.console.print(f"  {_esc(f'[{label}]')}")
            click.echo(preview)
        else:
            console.console.print(f"  {_esc(f'[{label}]')} (empty)")

    if prompts_only:
        return

    console.console.print()
    console.console.print("Features:")
    for key, value in (info.get("features") or {}).items():
        console.console.print(f"  {key}: {value}")

    # The agent's own credential listing carries the slot and placeholder
    # state; inspect carries name and type only. Fall back to inspect when the
    # listing cannot be read, rather than printing nothing.
    creds = linked if linked is not None else (info.get("credentials") or [])
    console.console.print()
    console.console.print(f"Connected credentials ({len(creds)}):")
    for cred in creds:
        console.console.print(f"  - {_credential_summary(cred)}")

    status = info.get("agent_api_status")
    if status:
        console.console.print()
        console.console.print("Agent REST API:")
        _print_agent_api_status(status)


# ── Prompts as files (pull / diff / push) ───────────────────────────────────
#
# Changing one line of a prompt used to take a raw GET, a hand-built JSON body,
# a raw PUT and a raw sync-prompts call — and building that JSON inline in a
# shell let zsh command-substitute the prompt's Markdown backticks. Files
# remove the quoting hazard; the baseline recorded at pull time is what lets a
# push tell "you edited this" from "the platform changed this since".

PROMPTS_DIR = "prompts"
PROMPTS_BASELINE = ".pulled.json"
PROMPT_TEXT_FILES = (
    ("workflow_prompt", "workflow.md"),
    ("entrypoint_prompt", "entrypoint.md"),
    ("refiner_prompt", "refiner.md"),
    ("router_trigger_prompt", "router_trigger.md"),
    ("description", "description.md"),
)
PROMPT_EXAMPLES_FILE = ("example_prompts", "example_prompts.json")
# The three prompts the environment also holds as docs/*.md.
DOC_BACKED_PROMPTS = ("workflow_prompt", "entrypoint_prompt", "refiner_prompt")


def _prompt_file_name(field: str) -> str:
    for f, name in (*PROMPT_TEXT_FILES, PROMPT_EXAMPLES_FILE):
        if f == field:
            return name
    return field


def _prompts_folder(account_root: Path, agent: dict, dir_opt: str | None) -> Path:
    """``--dir``, else ``prompts/<slug>/`` at the account root.

    Not inside ``agents/``: that tree is the synced workspace (and, for a
    git-versioned agent, a git working tree), and a folder there with no
    ``.cinna/`` would confuse the child-workspace discovery.
    """
    if dir_opt:
        return Path(dir_opt).expanduser().resolve()
    return account_root / PROMPTS_DIR / normalize_agent_dir_name(agent.get("name", ""))


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _prompt_text(value) -> str:
    """Prompt text as compared and sent: trailing whitespace is not content."""
    return str(value or "").rstrip()


def _prompt_same(field: str, a, b) -> bool:
    if field == PROMPT_EXAMPLES_FILE[0]:
        return list(a or []) == list(b or [])
    return _prompt_text(a) == _prompt_text(b)


def _platform_prompt_fields(record: dict) -> dict:
    fields = {field: record.get(field) for field, _ in PROMPT_TEXT_FILES}
    fields[PROMPT_EXAMPLES_FILE[0]] = record.get(PROMPT_EXAMPLES_FILE[0])
    return fields


def _read_prompt_files(folder: Path) -> dict:
    """The fields whose files exist; a deleted file means "leave it alone"."""
    out: dict = {}
    for field, name in PROMPT_TEXT_FILES:
        path = folder / name
        if path.is_file():
            out[field] = path.read_text(encoding="utf-8")
    field, name = PROMPT_EXAMPLES_FILE
    path = folder / name
    if path.is_file():
        try:
            examples = json.loads(path.read_text(encoding="utf-8") or "[]")
        except ValueError as exc:
            raise click.ClickException(f"{_display_path(path)} is not valid JSON: {exc}")
        if not isinstance(examples, list) or not all(isinstance(e, str) for e in examples):
            raise click.ClickException(
                f"{_display_path(path)} must be a JSON list of strings."
            )
        out[field] = examples
    return out


def _load_prompt_baseline(folder: Path) -> dict | None:
    path = folder / PROMPTS_BASELINE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _save_prompt_baseline(folder: Path, agent: dict, fields: dict) -> None:
    baseline = {
        "agent_id": agent.get("id"),
        "agent_name": agent.get("name"),
        "pulled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "fields": fields,
    }
    (folder / PROMPTS_BASELINE).write_text(
        json.dumps(baseline, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _baseline_fields_for(baseline: dict | None, agent: dict) -> dict | None:
    """The pulled values, only when the folder was pulled from this agent."""
    if not baseline or baseline.get("agent_id") != agent.get("id"):
        return None
    fields = baseline.get("fields")
    return fields if isinstance(fields, dict) else None


def run_agent_prompts_pull(agent_ref: str, dir_opt: str | None, force: bool) -> None:
    """Write an agent's prompts to files — `cinna agent prompts pull`."""
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        agent = _resolve_one_agent(client, agent_ref)
        with console.spinner("Reading the agent's prompts..."):
            record = client.get_agent(agent["id"])

    ref = _agent_ref_for_hint(agent)
    folder = _prompts_folder(account_root, agent, dir_opt)
    baseline = _load_prompt_baseline(folder)
    local = _read_prompt_files(folder) if folder.is_dir() else {}

    if local and not force:
        if baseline is None:
            raise click.ClickException(
                f"{_display_path(folder)}/ already holds prompt files that were not "
                f"pulled by this command. Pick another --dir, or pass --force to "
                f"overwrite them."
            )
        if baseline.get("agent_id") != agent["id"]:
            raise click.ClickException(
                f"{_display_path(folder)}/ holds the prompts of another agent "
                f"({baseline.get('agent_name')}). Pick another --dir, or pass "
                f"--force to overwrite them."
            )
        pulled = _baseline_fields_for(baseline, agent) or {}
        edited = [f for f, v in local.items() if not _prompt_same(f, v, pulled.get(f))]
        if edited:
            raise click.ClickException(
                f"Unpushed edits in {_display_path(folder)}/: "
                f"{', '.join(_prompt_file_name(f) for f in edited)}.\n"
                f"Push them with 'cinna agent prompts push {ref}', or pull with "
                f"--force to discard them."
            )

    remote = _platform_prompt_fields(record)
    folder.mkdir(parents=True, exist_ok=True)
    for field, name in PROMPT_TEXT_FILES:
        text = str(remote.get(field) or "")
        if text and not text.endswith("\n"):
            text += "\n"
        (folder / name).write_text(text, encoding="utf-8")
    field, name = PROMPT_EXAMPLES_FILE
    (folder / name).write_text(
        json.dumps(remote.get(field) or [], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _save_prompt_baseline(folder, agent, remote)

    console.status(f"Pulled {agent['name']}'s prompts into {_display_path(folder)}/")
    for field, name in (*PROMPT_TEXT_FILES, PROMPT_EXAMPLES_FILE):
        value = remote.get(field)
        if field == PROMPT_EXAMPLES_FILE[0]:
            size = f"{len(value or [])} example(s)"
        else:
            size = f"{len(_prompt_text(value))} chars" if value else "empty"
        console.console.print(f"  {name:<22} [dim]{size}[/dim]")
    console.console.print(
        f"[dim]Edit the files, then: cinna agent prompts diff {ref} · "
        f"cinna agent prompts push {ref}[/dim]"
    )


def _prompt_field_diff(field: str, platform_value, local_value) -> list[str]:
    import difflib

    if field == PROMPT_EXAMPLES_FILE[0]:
        before = json.dumps(platform_value or [], indent=2, ensure_ascii=False).splitlines()
        after = json.dumps(local_value or [], indent=2, ensure_ascii=False).splitlines()
    else:
        before = _prompt_text(platform_value).splitlines()
        after = _prompt_text(local_value).splitlines()
    name = _prompt_file_name(field)
    return list(
        difflib.unified_diff(
            before, after, fromfile=f"platform/{name}", tofile=f"local/{name}", lineterm=""
        )
    )


def run_agent_prompts_diff(agent_ref: str, dir_opt: str | None) -> None:
    """Local prompt files vs the platform — `cinna agent prompts diff`."""
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        agent = _resolve_one_agent(client, agent_ref)
        folder = _prompts_folder(account_root, agent, dir_opt)
        local = _read_prompt_files(folder) if folder.is_dir() else {}
        if not local:
            raise click.ClickException(
                f"No prompt files in {_display_path(folder)}/. Pull them first: "
                f"cinna agent prompts pull {_agent_ref_for_hint(agent)}"
            )
        with console.spinner("Reading the agent's prompts..."):
            record = client.get_agent(agent["id"])

    remote = _platform_prompt_fields(record)
    pulled = _baseline_fields_for(_load_prompt_baseline(folder), agent)
    differs = False
    for field, value in local.items():
        if _prompt_same(field, value, remote.get(field)):
            continue
        differs = True
        moved = (
            pulled is not None
            and field in pulled
            and not _prompt_same(field, remote.get(field), pulled[field])
        )
        edited = pulled is None or field not in pulled or not _prompt_same(field, value, pulled[field])
        if moved and edited:
            note = " — changed on the platform since you pulled, and locally too"
        elif moved:
            note = " — changed on the platform since you pulled (push leaves it alone)"
        else:
            note = ""
        console.console.print()
        console.console.print(f"[bold]{_esc(_prompt_file_name(field))}[/bold][dim]{_esc(note)}[/dim]")
        for line in _prompt_field_diff(field, remote.get(field), value):
            click.echo(line)
    if not differs:
        console.status("The local prompt files match the platform.")


def run_agent_prompts_push(
    agent_ref: str,
    dir_opt: str | None,
    sync_env: bool,
    force: bool,
    dry_run: bool,
) -> None:
    """Write edited prompt files back to the agent — `cinna agent prompts push`.

    Sends only the fields edited since the pull. A field the platform changed
    since the pull while the local file did not is left alone — pushing the
    stale file would silently revert it — and a field changed on both sides is
    refused unless ``force``.
    """
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        agent = _resolve_one_agent(client, agent_ref)
        ref = _agent_ref_for_hint(agent)
        folder = _prompts_folder(account_root, agent, dir_opt)
        local = _read_prompt_files(folder) if folder.is_dir() else {}
        if not local:
            raise click.ClickException(
                f"No prompt files in {_display_path(folder)}/. Pull them first: "
                f"cinna agent prompts pull {ref}"
            )
        baseline = _load_prompt_baseline(folder)
        if baseline and baseline.get("agent_id") != agent["id"] and not force:
            raise click.ClickException(
                f"{_display_path(folder)}/ was pulled from another agent "
                f"({baseline.get('agent_name')}). Pass --force to push it to "
                f"{agent['name']} anyway."
            )
        pulled = _baseline_fields_for(baseline, agent)

        with console.spinner("Reading the agent's prompts..."):
            remote = _platform_prompt_fields(client.get_agent(agent["id"]))

        body: dict = {}
        both_changed: list[str] = []
        for field, value in local.items():
            known = pulled is not None and field in pulled
            if known and _prompt_same(field, value, pulled[field]):
                continue  # not edited here — never revert a platform-side change
            if _prompt_same(field, value, remote.get(field)):
                continue
            if known and not _prompt_same(field, remote.get(field), pulled[field]):
                both_changed.append(field)
            body[field] = value if field == PROMPT_EXAMPLES_FILE[0] else _prompt_text(value)

        if both_changed and not force:
            raise click.ClickException(
                "Changed on the platform since you pulled, and locally too: "
                f"{', '.join(_prompt_file_name(f) for f in both_changed)}.\n"
                f"See both with 'cinna agent prompts diff {ref}', then pull --force "
                f"to take the platform's version or push --force to keep yours."
            )
        if not body:
            console.status("Nothing to push — no prompt file differs from the platform.")
            return
        if dry_run:
            console.status(
                f"Would push to {agent['name']}: "
                f"{', '.join(_prompt_file_name(f) for f in body)}"
            )
            for field in body:
                for line in _prompt_field_diff(field, remote.get(field), local[field]):
                    click.echo(line)
            return

        with console.spinner("Writing prompts..."):
            client.update_agent_config(agent["id"], body)

        env_synced = None
        if sync_env and any(f in body for f in DOC_BACKED_PROMPTS):
            try:
                with console.spinner("Pushing the doc prompts into the environment..."):
                    client.sync_agent_prompts(agent["id"])
                env_synced = True
            except (PlatformError, httpx.TransportError) as exc:
                logger.info("sync-prompts not applied: %s", exc)
                env_synced = False

    # Fields not pushed keep their pulled value, so a platform-side change the
    # files never saw still reads as "not edited here" on the next push.
    new_baseline = dict(pulled) if pulled is not None else dict(remote)
    new_baseline.update(body)
    _save_prompt_baseline(folder, agent, new_baseline)

    console.status(
        f"Pushed {', '.join(_prompt_file_name(f) for f in body)} to {agent['name']}"
    )
    if env_synced is True:
        console.console.print(
            "  [dim]The running environment's docs/*.md now carry them.[/dim]"
        )
    elif env_synced is False:
        console.console.print(
            "  [dim]Saved. The environment is not running, so its docs/*.md pick "
            "them up on its next start.[/dim]"
        )
    console.console.print(f"  [dim]Verify: cinna agent show {ref} --prompts[/dim]")


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
    table.add_column("Name / id", overflow="fold")
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
    if not yes and not console.confirm(
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


# A table cell that did not fit is rendered with one of these; an id copied
# out of one is not an id. `cinna api` is where they land, because it is the
# only verb that takes a raw platform path.
_ELIDED_MARKERS = ("\u2026", "...")


def _reject_elided_path(path: str) -> None:
    """Refuse a path segment that was copied out of a truncated table cell.

    A narrow terminal ellipsizes a UUID, and the result reaches the API as a
    perfectly well-formed path — which answers ``404 Agent not found``. That
    sentence sends the reader looking for a missing agent instead of a
    mangled id, so the CLI answers first.
    """
    for segment in path.split("/"):
        if any(marker in segment for marker in _ELIDED_MARKERS):
            raise click.UsageError(
                f"The path segment '{segment}' looks elided — an id copied out "
                f"of a table cell that was too narrow to print it whole. The "
                f"API would answer '404 not found' for it.\n"
                f"Get the full id from 'cinna account agents --json', or pass "
                f"the agent's name: cinna api accepts the same references "
                f"'cinna skills list' does."
            )


def _resolve_api_agent_refs(client: "AccountClient", path: str) -> str:
    """Let `cinna api` take the agent references every other verb takes.

    Only the segment straight after ``agents/`` is considered, and only when it
    is not already a UUID. A reference that resolves is substituted (announced
    on stderr, so a ``--json`` stdout stream stays pure); one that does not is
    **left alone** — ``agents/`` is a route prefix as well as a collection, and
    turning a real sub-route into "no accessible agent matches 'search'" would
    break the escape hatch to fix an ergonomic.

    The same reasoning covers the lookup itself: every failure, including one
    reaching the listing route, leaves the path exactly as typed. This is
    sugar on the one verb whose job is to work when nothing else does.
    """
    parts = path.split("/")
    candidates = [
        i
        for i, part in enumerate(parts)
        if i and parts[i - 1] == "agents" and part and not _looks_like_uuid(part)
    ]
    if not candidates:
        return path

    try:
        listing = client.list_account_agents().get("data", [])
    except Exception as exc:  # noqa: BLE001 — the hatch still runs without this
        logger.debug("agent-ref resolution for 'cinna api' skipped: %s", exc)
        return path

    changed = False
    for i in candidates:
        try:
            agent = _resolve_account_agent(listing, parts[i])
        except click.ClickException:
            continue
        click.echo(f"agents/{parts[i]} → agents/{agent['id']}", err=True)
        parts[i] = str(agent["id"])
        changed = True
    return "/".join(parts) if changed else path


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
    _reject_elided_path(path)
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
        path = _resolve_api_agent_refs(client, path)
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
    table.add_column("ID", style="dim", overflow="fold")

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


def _credential_slot_cell(cred: dict) -> str:
    """A credential's slot (its ``service_uri``), or a dim dash for none."""
    slot = cred.get("service_uri")
    return _esc(slot) if slot else "[dim]—[/dim]"


def _credential_summary(cred: dict) -> str:
    """One linked credential on one line: name, type, slot, state, id.

    Fields are printed only when the payload carries them — the agent's own
    credential listing has all of them, the inspect fallback has name and
    type alone, and an absent slot is not the same fact as an empty one.
    Parentheses, not brackets, around the type: ``[api_token]`` is a Rich tag.
    """
    parts = [f"{_esc(cred.get('name', '?'))} [dim]({_esc(cred.get('type', '?'))})[/dim]"]
    if "service_uri" in cred:
        slot = cred.get("service_uri")
        if slot:
            parts.append(f"slot: {_esc(slot)}")
        elif cred.get(_SLOT_UNVERIFIED):
            parts.append("[dim]slot: unknown[/dim]")
        else:
            parts.append("[dim]no slot[/dim]")
    if cred.get("is_placeholder"):
        parts.append("[yellow]placeholder[/yellow]")
    elif cred.get("status") == "incomplete":
        parts.append("[yellow]needs setup[/yellow]")
    if cred.get("id"):
        parts.append(f"[dim]{_esc(cred['id'])}[/dim]")
    return "  ".join(parts)


# Set on a linked credential whose slot could not be confirmed: the agent's
# listing reported none, and the account listing (the one that carries it) has
# no row for this id — a credential shared by someone else, or an unreadable
# listing. "No slot" would be a claim; this is the absence of one.
_SLOT_UNVERIFIED = "_slot_unverified"
_ACCOUNT_AUTHORITATIVE_FIELDS = ("service_uri", "is_placeholder", "status")


def _fetch_linked_credentials(client: "AccountClient", agent_id: str) -> list[dict] | None:
    """The credentials linked to an agent, or ``None`` when unreadable.

    ``GET agents/{id}/credentials`` says which credentials are linked, but
    returns ``service_uri: null`` even for a credential that has a slot — seen
    live, the same credential id read ``some-token.com`` in the account
    listing and ``null`` here, which made a filled slot look ``not_linked``.
    So the slot, placeholder flag and status are taken from the account
    listing by id, and a linked credential absent from it keeps an unverified
    slot rather than an empty one.

    Best-effort by design: every caller has something useful to print without
    it, and a refusal here must not turn a read command into a failure.
    """
    try:
        payload = client.list_agent_credentials(agent_id)
    except (PlatformError, httpx.TransportError) as exc:
        logger.warning("agent credentials listing unavailable: %s", exc)
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None
    linked = [dict(c) for c in data if isinstance(c, dict)]

    owned: dict[str, dict] = {}
    try:
        account = client.list_credentials()
        rows = account.get("data") if isinstance(account, dict) else None
        if isinstance(rows, list):
            owned = {str(r["id"]): r for r in rows if isinstance(r, dict) and r.get("id")}
    except (PlatformError, httpx.TransportError) as exc:
        logger.warning("account credentials listing unavailable: %s", exc)

    for cred in linked:
        row = owned.get(str(cred.get("id")))
        if row is not None:
            for field in _ACCOUNT_AUTHORITATIVE_FIELDS:
                if field in row:
                    cred[field] = row[field]
        elif not cred.get("service_uri"):
            cred[_SLOT_UNVERIFIED] = True
    return linked


def run_credentials_list(workspace: str | None, as_json: bool = False) -> None:
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

    if as_json:
        click.echo(json.dumps(listing, indent=2, default=str))
        return

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
    table.add_column("Slot", overflow="fold")
    table.add_column("Status")
    table.add_column("ID", style="dim", overflow="fold", no_wrap=True)

    for c in items:
        status_cell = _credential_status_cell(c.get("status"))
        if c.get("is_placeholder"):
            status_cell += " [dim]placeholder[/dim]"
        table.add_row(
            _esc(c.get("name", "?")),
            _esc(c.get("type", "?")),
            _credential_slot_cell(c),
            status_cell,
            c.get("id", "?"),
        )

    console.console.print(table)
    console.console.print(
        "[dim]Slot = the credential's service URI, which a skill's `credentials:` "
        "block names. Set it: cinna account credentials update <id> "
        "--service-uri <slot>[/dim]"
    )


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
        if not console.confirm("Continue?"):
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


# ── Agent addons: skills + plugins (`cinna skills`) ─────────────────────────
#
# "Addon" is the platform's umbrella over the two things an agent carries
# beyond its prompt: installed plugins, and `skills/<name>/` folders the engine
# loads on demand. The server owns the dedupe rule (a catalog install is both a
# plugin link and an index entry, and is one row here), so the CLI renders the
# projection it is handed rather than folding the two halves its own way.


def _esc(value) -> str:
    """One server-authored value, safe to interpolate into Rich markup.

    A skill's version is free text and a display name is whatever its author
    typed, so either can hold square brackets — which Rich reads as a style tag
    and silently swallows (``1.0[beta]`` renders as ``1.0``). Escaping is not
    cosmetic here: the version is the fact the row exists to report.
    """
    from rich.markup import escape

    return escape(str(value))


def _addon_status_cell(addon: dict) -> str:
    """Colour one addon's ``status`` (``ok`` / ``warning`` / ``error``).

    The **code** goes in the column, not the sentence: the codes are the
    contract and they keep the table one line per addon. The platform's own
    sentence follows the table — see :func:`_addon_issue_lines`.
    """
    status = addon.get("status") or "ok"
    code = addon.get("status_code")
    if status == "error":
        return f"[red]✗ {code or 'error'}[/red]"
    if status == "warning":
        return f"[yellow]! {code or 'warning'}[/yellow]"
    return "[green]● ok[/green]"


def _addon_issue_message(addon: dict) -> tuple[str | None, list[str]]:
    """The platform's sentence behind a row's ``status_code``, and its paths.

    The row carries only the code; the sentence that goes with it sits on the
    offending skill one level down (``skills[].error`` / ``.warning``), which is
    where the platform wrote the copy. Matching on the code locates that issue —
    the tone still comes from ``status``, never from the prose.

    Row-level codes (``orphan``, ``source_unavailable``) have no skill behind
    them and no sentence to borrow; those return ``None`` and the code in the
    table is the whole answer.
    """
    code = addon.get("status_code")
    if not code:
        return None, []
    for skill in addon.get("skills") or []:
        for issue in (skill.get("error"), skill.get("warning")):
            if issue and issue.get("code") == code:
                return issue.get("message"), [str(p) for p in issue.get("paths") or []]
    return None, []


def _print_addon_issues(addons: list[dict]) -> None:
    """Print the platform's sentence for every row that is not ``ok``.

    Below the table rather than inside it: a sentence in a status cell would
    wrap and cost the one-row-per-addon shape, and ``secrets`` carries a file
    list that has nowhere to go in a column.
    """
    flagged = [a for a in addons if (a.get("status") or "ok") != "ok"]
    if not flagged:
        return

    console.console.print()
    for addon in flagged:
        marker = "[red]✗[/red]" if addon.get("status") == "error" else "[yellow]![/yellow]"
        message, paths = _addon_issue_message(addon)
        code = addon.get("status_code") or addon.get("status")
        headline = f"{marker} {_esc(addon.get('name', '?'))} [dim]({_esc(code)})[/dim]"
        console.console.print(f"{headline}: {_esc(message)}" if message else headline)
        for path in paths:
            # A path is the whole point of a `secrets` line: `config[dev].env`
            # rendered as `config.env` would name a file that does not exist.
            console.console.print(f"    [dim]{_esc(path)}[/dim]")


def _addon_name_cell(addon: dict) -> str:
    """The engine-facing name, plus the human one and the published marker.

    ``name`` leads because it is what ``plugin_ref`` and the on-disk layout
    use — it is the string the user types back at `cinna skills publish`.
    """
    cell = f"[bold]{_esc(addon.get('name', '?'))}[/bold]"
    display = addon.get("display_name")
    if display and display != addon.get("name"):
        cell += f" [dim]({_esc(display)})[/dim]"
    if addon.get("published_package_id"):
        cell += " [cyan]· published[/cyan]"
    if addon.get("orphan"):
        cell += " [dim]· orphan[/dim]"
    if _addon_link(addon).get("disabled"):
        # Only visible here. A disabled link still lists, still reports its
        # version, and is not an error — nothing else in the row would say the
        # engine is not loading it.
        cell += " [yellow]· disabled[/yellow]"
    return cell


def _addon_link(addon: dict) -> dict:
    """The ``link`` sub-object of an addon row, or an empty dict.

    Everything true of an *install* rather than of a package lives here —
    ``id`` (the link id the plugin routes address), ``installed_version``,
    ``latest_version``, ``has_update``, ``disabled`` and the two mode
    switches. The row's top-level ``version`` mirrors the installed one, which
    is why reading only the top level makes a stale install look current.
    """
    link = addon.get("link")
    return link if isinstance(link, dict) else {}


def _addon_version_cell(addon: dict) -> str:
    """The addon's version, and whether a newer one is waiting.

    A skill's version is a line in its own ``SKILL.md``, so it is genuinely
    optional — an absent one renders blank rather than a bare ``v`` or a
    ``-``, the same way the web row shows no badge. See
    :func:`_print_addons` for why a *local* skill's blank can also be staleness
    rather than absence.

    For an **installed** addon the platform reports three facts, not one, and
    all three live on the row's ``link``: what the agent carries
    (``installed_version``), what the catalog now has (``latest_version``), and
    whether they differ (``has_update``). The row's top-level ``version`` is
    the installed one alone, so a row rendered from it would make an agent
    stuck two revisions back look exactly like a current one — which is the
    whole reason anybody reads this column.
    """
    link = _addon_link(addon)
    version = link.get("installed_version") or addon.get("version")
    cell = f"[dim]{_esc(version)}[/dim]" if version else ""
    if link.get("has_update"):
        latest = link.get("latest_version")
        marker = (
            f"[yellow]→ {_esc(latest)}[/yellow]" if latest else "[yellow]→ update[/yellow]"
        )
        cell = f"{cell} {marker}" if cell else marker
    return cell


def _addon_source_cell(addon: dict) -> str:
    """Where the addon came from, and — for an install — from which catalog."""
    source = addon.get("source", "?")
    marketplace = addon.get("marketplace_name")
    if marketplace and str(marketplace) != str(source):
        return f"{_esc(source)} [dim]({_esc(marketplace)})[/dim]"
    return _esc(source)


# ── Skill credential readiness ───────────────────────────────────────────────
#
# A skill declares the credential slots its scripts need, and a slot is filled
# by a credential linked to the agent whose service URI equals it. The platform
# reports unusable slots in `credential_issues` for **catalog** installs only: a
# local skill's row reads `ok` whether or not anything carries its slot, which
# is how a skill looked healthy while its script could never find its token.
# For every non-catalog row the CLI checks the agent's linked credentials.


def _declared_slots(addon: dict) -> list[dict]:
    """Every credential slot the row's skills declare (first declaration wins)."""
    seen: dict[str, dict] = {}
    for skill in addon.get("skills") or []:
        for decl in skill.get("credentials") or []:
            if isinstance(decl, dict) and decl.get("slot"):
                seen.setdefault(str(decl["slot"]), decl)
    return list(seen.values())


def _needs_linked_credentials(payload: dict) -> bool:
    """Whether any row's readiness has to be checked by the CLI itself."""
    return any(
        a.get("source") != "catalog" and _declared_slots(a)
        for a in payload.get("addons") or []
    )


def _slot_readiness(
    addon: dict, decl: dict, linked: list[dict] | None
) -> tuple[str, dict | None]:
    """``(state, credential)`` for one declared slot on one addon row.

    ``ready``; the platform's reasons ``not_linked`` / ``not_configured`` /
    ``access_revoked``; ``type_mismatch`` when the credential carrying the slot
    is not the declared type; ``unknown`` when the agent's credentials could
    not be read. ``credential`` is the linked one carrying the slot, if any.
    """
    slot = decl.get("slot")
    for issue in addon.get("credential_issues") or []:
        if isinstance(issue, dict) and issue.get("slot") == slot:
            return str(issue.get("reason") or "credential_missing"), None
    if addon.get("source") == "catalog":
        # The platform computed this row's slots and named no issue for this one.
        return "ready", None
    if linked is None:
        return "unknown", None
    carriers = [c for c in linked if c.get("service_uri") == slot]
    if not carriers:
        # A linked credential whose slot is invisible to this account could be
        # the carrier; "not_linked" would send the user to fix a filled slot.
        if any(c.get(_SLOT_UNVERIFIED) for c in linked):
            return "unknown", None
        return "not_linked", None
    declared_type = decl.get("type")
    typed = [c for c in carriers if not declared_type or c.get("type") == declared_type]
    if not typed:
        return "type_mismatch", carriers[0]
    usable = [
        c for c in typed if not c.get("is_placeholder") and c.get("status") != "incomplete"
    ]
    if not usable:
        return "not_configured", typed[0]
    return "ready", usable[0]


def _addon_credentials_cell(addon: dict, linked: list[dict] | None) -> str:
    """One line per declared slot: ✓ filled, ! not usable yet, ? unchecked."""
    lines = []
    for decl in _declared_slots(addon):
        state, _cred = _slot_readiness(addon, decl, linked)
        slot = _esc(decl.get("slot"))
        if state == "ready":
            lines.append(f"[green]✓[/green] {slot}")
        elif state == "unknown":
            lines.append(f"[dim]? {slot}[/dim]")
        else:
            # Amber, never red: an unfilled skill slot never blocks the agent.
            lines.append(f"[yellow]! {slot}[/yellow] [dim]({_esc(state)})[/dim]")
    return "\n".join(lines)


def _slot_remedy(
    state: str,
    decl: dict,
    cred: dict | None,
    linked: list[dict] | None,
    agent_ref: str,
) -> list[str]:
    """What to run for one unusable slot. Plain text; the caller escapes it."""
    slot = decl.get("slot")
    declared_type = decl.get("type") or "<type>"
    if state == "not_linked":
        spare = [
            c
            for c in linked or []
            if c.get("type") == declared_type and not c.get("service_uri") and c.get("id")
        ]
        if spare:
            return [
                f"'{spare[0].get('name')}' is a linked {declared_type} credential with "
                f"no slot. Give it this one:",
                f"cinna account credentials update {spare[0]['id']} --service-uri {slot}",
            ]
        return [
            "Nothing linked to this agent carries that slot. Draft one (the user "
            "fills the secret in the UI):",
            f'cinna account credentials create --name "{slot}" --type {declared_type} '
            f"--service-uri {slot} --agent {agent_ref}",
        ]
    if state == "not_configured":
        name = (cred or {}).get("name") or "The credential carrying the slot"
        return [f"'{name}' carries the slot but is not filled in yet — fill it in the web UI."]
    if state == "type_mismatch":
        name = (cred or {}).get("name", "?")
        return [
            f"'{name}' carries the slot but is a {(cred or {}).get('type')} credential; "
            f"the skill declares {declared_type}. Fix the type in SKILL.md, or link a "
            f"{declared_type} credential with this slot.",
        ]
    if state == "access_revoked":
        return [
            "The credential carrying the slot is no longer shared with you. Provide "
            "your own, or ask the publisher.",
        ]
    return ["Check the agent's Credentials tab."]


def _print_credential_readiness(
    addons: list[dict], linked: list[dict] | None, agent_ref: str
) -> None:
    """Name every declared slot that is not usable yet, with its fix."""
    pending = []
    unchecked = False
    for addon in addons:
        for decl in _declared_slots(addon):
            state, cred = _slot_readiness(addon, decl, linked)
            if state == "unknown":
                unchecked = True
            elif state != "ready":
                pending.append((addon, decl, state, cred))

    for addon, decl, state, cred in pending:
        console.console.print()
        console.console.print(
            f"[yellow]![/yellow] {_esc(addon.get('name', '?'))} needs slot "
            f"{_esc(decl.get('slot'))} [dim]({_esc(decl.get('type') or '?')})[/dim]: "
            f"{_esc(state)}"
        )
        for line in _slot_remedy(state, decl, cred, linked, agent_ref):
            console.console.print(f"    [dim]{_esc(line)}[/dim]")
    if pending:
        console.console.print(
            "[dim]A skill's credential is checked only when its script asks for it: "
            "the agent keeps working, and that script fails until the slot is "
            "filled.[/dim]"
        )
    if unchecked:
        console.console.print()
        console.console.print(
            "[dim]? A slot could not be confirmed: the agent's linked credentials "
            "were unreadable, or one of them is shared by someone else and its slot "
            "is not visible to this account.[/dim]"
        )


def _print_addons(
    payload: dict,
    agent_ref: str = "<agent>",
    linked_credentials: list[dict] | None = None,
) -> None:
    """Render an ``AgentAddonsPublic`` payload as one row per addon.

    ``agent_ref`` is how the caller's agent should be spelled back in the
    suggested commands under the table — a name the user can retype, not the
    id the payload carries.

    ``linked_credentials`` is the agent's own credential listing, which the
    readiness of a non-catalog skill's slots is checked against; ``None``
    leaves those slots marked unchecked. The Credentials column appears only
    when some skill declares a slot, so an agent without any keeps its shape.
    """
    from rich.table import Table

    addons = payload.get("addons") or []
    if not addons:
        # A dim line, not a green check: an empty list is a fact, not a success.
        # But only when it IS one — with `skills_error` set, an agent whose
        # addons are all skills lists nothing, and "carries none" would be the
        # false completeness that warning exists to prevent. The warning below
        # is then the whole answer.
        if not payload.get("skills_error"):
            console.console.print(
                "[dim]This agent carries no plugins or skills yet.[/dim]"
            )
    else:
        counts = payload.get("counts") or {}
        declares_slots = any(_declared_slots(a) for a in addons)
        table = Table(title=f"Addons ({len(addons)})", title_style="bold")
        table.add_column("#", style="dim", justify="right")
        table.add_column("Kind")
        table.add_column("Source")
        table.add_column("Status")
        table.add_column("Name")
        table.add_column("Version")
        if declares_slots:
            table.add_column("Credentials")

        for i, addon in enumerate(addons, 1):
            cells = [
                str(i),
                addon.get("kind", "?"),
                _addon_source_cell(addon),
                _addon_status_cell(addon),
                _addon_name_cell(addon),
                _addon_version_cell(addon),
            ]
            if declares_slots:
                cells.append(_addon_credentials_cell(addon, linked_credentials))
            table.add_row(*cells)

        console.console.print(table)
        console.console.print(
            f"[dim]{counts.get('plugins', 0)} plugin(s), "
            f"{counts.get('skills', 0)} skill(s) "
            f"({counts.get('local_skills', 0)} of them this agent's own).[/dim]"
        )
        _print_addon_issues(addons)
        if declares_slots:
            _print_credential_readiness(addons, linked_credentials, agent_ref)
        _print_pending_update_hint(addons, agent_ref)
        _print_version_staleness_hint(addons)

    # The plugin half is returned even when the skill half could not be read —
    # say so rather than letting a short list look complete.
    #
    # The remedy comes from the same table `refresh` reads. This used to hard-
    # code "cinna skills refresh" for every code, which for
    # `adapter_unsupported` sent the reader round a loop that cannot close: a
    # refresh re-reads a route the container never had. It also called that
    # refresh a "Rebuild", which is the exact word this vocabulary reserves for
    # the one action a refresh is not. `list` is the read command an LLM caller
    # reaches first, so a wrong verb here is the wrong verb everywhere after it.
    code = payload.get("skills_error")
    if code:
        console.warn(
            f"The skill index could not be read ({code}); only plugins are "
            f"listed above."
        )
        for line in _index_error_remedy(code, agent_ref):
            console.console.print(f"  [dim]{line}[/dim]")


def _print_pending_update_hint(addons: list[dict], agent_ref: str) -> None:
    """Name the installed addons a newer revision is waiting for.

    The arrow in the Version column is the glance; this is the answer to "so
    what do I run". Printed only when something is actually stale, so a
    current agent stays quiet.
    """
    stale = [str(a.get("name")) for a in addons if _addon_link(a).get("has_update")]
    if not stale:
        return
    console.console.print()
    console.console.print(
        f"[yellow]![/yellow] {len(stale)} installed addon(s) have a newer "
        f"revision: {_esc(', '.join(stale))}"
    )
    console.console.print(
        f"[dim]Update with: cinna skills update {agent_ref} {_esc(stale[0])}[/dim]"
    )


def _print_version_staleness_hint(addons: list[dict]) -> None:
    """Say that a blank version can mean a stale index, not a missing header.

    The index a row is read from is built inside the container, and an
    environment created before skills carried a version reports none however
    many times its author edits ``SKILL.md``. The platform backfills a local
    skill's version from the workspace file — but only on an index *fetch*, and
    this route is cache-only, so on those environments the version appears
    after a Refresh, a start sweep or a publish, never on this read. Printed
    only when a local skill actually has no version, so a fully-versioned agent
    never sees the caveat.
    """
    if any(
        a.get("source") == "local" and a.get("kind") == "skill" and not a.get("version")
        for a in addons
    ):
        console.console.print()
        console.console.print(
            "[dim]A blank version means no `version:` in the skill's SKILL.md — "
            "or an index built before skills carried one, which fills in after "
            "the next refresh or publish.[/dim]"
        )


def run_skills_list(agent_ref: str, as_json: bool = False) -> None:
    """List an agent's addons — `cinna skills list`."""
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with console.spinner("Fetching addons..."):
        with AccountClient(account_cfg) as client:
            agent = _resolve_one_agent(client, agent_ref)
            payload = client.get_agent_addons(agent["id"])
            # Only when a non-catalog skill declares a slot: that readiness is
            # the one the platform leaves to the reader.
            linked = (
                _fetch_linked_credentials(client, agent["id"])
                if not as_json and _needs_linked_credentials(payload)
                else None
            )

    if as_json:
        click.echo(json.dumps(payload, indent=2, default=str))
        return

    console.console.print(f"Agent: [bold]{_esc(agent['name'])}[/bold]")
    _print_addons(payload, normalize_agent_dir_name(agent["name"]), linked)

    publishable = [
        a.get("name")
        for a in (payload.get("addons") or [])
        if a.get("can_share") and not a.get("published_package_id")
    ]
    if publishable:
        console.console.print()
        console.console.print(
            f"[dim]Ready to share: cinna skills publish "
            f"{normalize_agent_dir_name(agent['name'])} {publishable[0]} "
            f"--visibility <public|private|users>[/dim]"
        )


# The platform derives a skill's version from the skill's own SKILL.md header
# and writes the resolved value back into that file before it snapshots it, so
# `--version` is an override, not the normal path. These two helpers are what
# the CLI adds around that: a local refusal of a label the server's 422 would
# refuse anyway, and the rendering of the preview the publish would take.

def _validate_version_label(version: str | None) -> None:
    """Refuse an ``--version`` the platform could not use, before any call.

    Two failures, both invisible until after a publish that cannot be taken
    back:

    - A newline or any other control character is a **422** at the request
      boundary, because the value is interpolated into a ``SKILL.md``
      frontmatter block where a newline would inject top-level keys.
    - An empty or whitespace-only label is *not* refused server-side — it is
      normalised to nothing and the version is derived instead, so the
      publisher gets a revision numbered by a rule they thought they had
      overridden.

    Over-length is left to the server: 64 is the platform's bound to move, and
    a client copy of it that drifted would refuse a version the API accepts.
    """
    if version is None:
        return
    if any(ch == "\r" or ch == "\n" or ch < " " for ch in version):
        raise click.UsageError(
            "--version must be a single line with no control characters: it is "
            "written into the skill's SKILL.md frontmatter, where a newline "
            "would inject top-level keys into the header."
        )
    if not version.strip():
        raise click.UsageError(
            "--version is empty. An empty label is not an override — the "
            "platform would ignore it and derive the version from SKILL.md. "
            "Name a version, or drop the option to get the derived one."
        )


def _print_publish_preview(name: str, agent_name: str, preview: dict) -> None:
    """Render a ``SkillPublishPreview`` — what pressing publish would do."""
    verb = "Re-publish" if preview.get("is_republish") else "Publish"
    console.console.print(
        f"{verb} [bold]{_esc(name)}[/bold] from [bold]{_esc(agent_name)}[/bold]"
    )

    version = preview.get("version")
    provenance = []
    if preview.get("header_version"):
        provenance.append(f"header {_esc(preview['header_version'])}")
    if preview.get("latest_published_version"):
        provenance.append(
            f"latest published {_esc(preview['latest_published_version'])}"
        )
    suffix = f"  [dim]({', '.join(provenance)})[/dim]" if provenance else ""
    console.console.print(f"  Version:    {_esc(version)}{suffix}")

    console.console.print(f"  Package:    {_esc(preview.get('package_id', '?'))}")
    if preview.get("package_id_disambiguated"):
        # The publisher did nothing wrong and has no other explanation for the
        # hex tail in their own package id: say where it came from.
        console.console.print(
            "              [dim]the plain id was already taken on this "
            "instance, so your publisher slug was appended[/dim]"
        )
    console.console.print(f"  Revision:   {preview.get('next_revision_number', '?')}")


def _skill_md_carries_version(revision: dict) -> bool | None:
    """Did the publish manage to stamp the version into the workspace file?

    The platform writes the resolved version into ``skills/<name>/SKILL.md``
    before it snapshots the folder, and a write that could not happen is
    logged, not fatal. The revision's *stored frontmatter* is what says which
    happened: it carries ``version`` only when the published bytes do, so a
    revision that has a version its frontmatter does not is exactly the
    degraded path.

    ``None`` when the answer is not in the payload — a response with no
    frontmatter at all cannot distinguish "not written" from "not reported",
    and a warning invented from an absent field would be worse than silence.
    """
    frontmatter = revision.get("frontmatter")
    if not isinstance(frontmatter, dict) or not frontmatter:
        return None
    if not revision.get("version"):
        return None
    return bool(frontmatter.get("version"))


def run_skills_publish(
    agent_ref: str,
    name: str,
    visibility: str | None,
    grant_emails: tuple[str, ...],
    version: str | None,
    release_notes: str | None,
    package_id: str | None,
    dry_run: bool = False,
    yes: bool = False,
    as_json: bool = False,
) -> None:
    """Publish one of an agent's skills to the catalog — `cinna skills publish`.

    Refusals (``not_developer``, ``foreign_install``, ``no_environment``,
    ``workspace_unavailable``, ``skill_contains_secrets``, …) surface as the
    server's own coded sentence: the platform owns that copy, and re-wording it
    here would make two places to keep true.
    """
    # ``--grant`` is only meaningful on a ``users`` package. The server stores a
    # grant unconditionally, but ``user_can_see`` consults the grant table ONLY
    # for ``visibility="users"`` — so publishing private (the default for a new
    # package) with ``--grant ana@…`` succeeds, writes Ana's grant row, and
    # shares nothing. The publisher would be told the opposite and stop, and the
    # correction costs a permanent extra revision because revisions are
    # immutable. The web dialog cannot reach that state — its people picker only
    # renders under ``users`` — so the CLI has to make the same coupling
    # explicit. Naming ``--visibility users`` is a no-op on a package that is
    # already ``users``, so requiring it costs a re-publish nothing.
    if grant_emails and visibility != "users":
        raise click.UsageError(
            "--grant only has an effect on a package whose visibility is "
            "'users': a private or public package ignores its grant list "
            "entirely, and the grant would be written but share nothing.\n"
            "Re-run with --visibility users (harmless on a package that "
            "already is), or drop --grant."
        )

    _validate_version_label(version)

    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        agent = _resolve_one_agent(client, agent_ref)

        if dry_run:
            # The only thing this verb has to say, so a refusal here is the
            # command failing rather than something to shrug off.
            with console.spinner(f"Previewing '{name}'..."):
                preview = client.get_skill_publish_preview(agent["id"], name)
            if as_json:
                click.echo(json.dumps(preview, indent=2, default=str))
                return
            _print_publish_preview(name, agent["name"], preview)
            if version:
                console.console.print(
                    f"  [dim]--version {_esc(version)} would override the "
                    f"derived version above.[/dim]"
                )
            console.console.print()
            console.console.print(
                "[dim]Nothing was published. The preview skips the content "
                "checks (secrets, malformed skill, budget), so a publish can "
                "still refuse.[/dim]"
            )
            return

        # A human at a terminal sees what the press will do before it happens —
        # the version is derived, and the package id may carry a disambiguating
        # slug, so there is something to read that the command line does not
        # say. Skipped under --no-input / --json / a pipe, which is every
        # scripted caller: this must not turn an existing automation into a
        # blocked prompt.
        if not yes and not as_json and console.interactive():
            preview = {}
            try:
                with console.spinner(f"Previewing '{name}'..."):
                    preview = client.get_skill_publish_preview(agent["id"], name)
            except CinnaExit as exc:
                # An older platform has no preview route, and a preview is not
                # what was asked for: publish as before rather than refusing.
                logger.debug("skill publish preview failed: %s", exc)
            if preview:
                _print_publish_preview(name, agent["name"], preview)
                if not console.confirm("Publish?", default=True):
                    raise click.Abort()

        with console.spinner(f"Publishing '{name}'..."):
            revision = client.publish_agent_skill(
                agent["id"],
                name,
                version=version,
                release_notes=release_notes,
                visibility=visibility,
                grant_emails=list(grant_emails),
                package_id=package_id,
            )

        # The revision names its package by UUID only; the catalog line wants
        # the reverse-DNS id and the visibility that actually stuck. A failure
        # here must not turn a successful publish into an error.
        package_uuid = revision.get("package_id")
        package: dict = {}
        if package_uuid:
            try:
                package = client.get_skill_package(package_uuid)
            except Exception as exc:
                # Deliberately every exception, not just CinnaExit: an httpx
                # transport error would otherwise reach CinnaGroup.invoke and
                # exit 12, reporting a publish that COMMITTED as a network
                # failure. The user re-runs and appends a second immutable
                # revision to the catalog — the one mistake this block exists
                # to prevent.
                logger.debug("skill package lookup after publish failed: %s", exc)

    catalog_link = (
        f"{account_cfg.frontend_url.rstrip('/')}/catalog/skills/{package_uuid}"
        if package_uuid
        else None
    )

    stamped = _skill_md_carries_version(revision)

    if as_json:
        click.echo(
            json.dumps(
                {
                    "revision": revision,
                    "package": package,
                    "catalog_url": catalog_link,
                    "skill_md_updated": stamped,
                },
                indent=2,
                default=str,
            )
        )
        return

    console.status(f"Published '{_esc(name)}' from {_esc(agent['name'])}")
    if package.get("package_id"):
        console.console.print(f"  Package:    {package['package_id']}")
    rev_no = revision.get("revision_number", "?")
    rev_version = revision.get("version")
    console.console.print(
        f"  Revision:   {rev_no}" + (f" ({_esc(rev_version)})" if rev_version else "")
    )
    if package.get("visibility"):
        console.console.print(f"  Visibility: {package['visibility']}")
    if grant_emails:
        console.console.print(f"  Granted:    {', '.join(grant_emails)}")
    if catalog_link:
        console.console.print(f"  Catalog:    {catalog_link}")

    if stamped is True and rev_version:
        # A file in the workspace changed without the user editing it, and the
        # local mirror is a sync away from it — worth one line, because a live
        # sync session will move it and an unsynced local edit to the same file
        # is now a conflict rather than a fast-forward.
        console.console.print(
            f"  [dim]skills/{_esc(name)}/SKILL.md now carries version: "
            f"{_esc(rev_version)} — sync to bring it down.[/dim]"
        )
    elif stamped is False:
        console.warn(
            f"The version could not be written into skills/{name}/SKILL.md "
            f"(a read-only workspace, or a file with no frontmatter fence). "
            f"Revision {rev_no} is published"
            + (f" as {_esc(rev_version)}" if rev_version else "")
            + ", but the header still says what it said; the next publish "
            "continues the series from the catalog, not from the file."
        )


# ── Skills lifecycle: install, update, catalog, sharing ─────────────────────
#
# Everything below turns a platform id into something a person types. Three
# ids exist in this area and none of them belong in a command line: a package
# has a UUID *and* a reverse-DNS string, and an install is a "plugin link" with
# an id of its own. The resolvers here are what keep `cinna skills` speaking in
# agent names, package names and skill names — the same references
# `cinna skills list` already accepts.

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def _looks_like_uuid(value) -> bool:
    return bool(value) and bool(_UUID_RE.match(str(value)))


def _rows(payload, *keys: str) -> list[dict]:
    """The list inside a listing envelope, whichever key it arrived under.

    The account routes answer ``{"data": [...]}``; the catalog and the
    revision routes are plain platform routes reached through the hatch and
    answer under their own names (or as a bare list). Guessing here is cheaper
    than a wrong assumption that renders an empty table over a full response.
    """
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", *keys, "items", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
    return []


def _package_uuid(package: dict) -> str | None:
    """The package's UUID — the id every route addresses it by.

    Not to be confused with ``package_id``, which on a package payload is the
    *reverse-DNS string* (``com.acme.report``). The two names are inverted
    between payloads — a published revision calls the UUID ``package_id`` —
    so this checks the shape rather than trusting either name.
    """
    for key in ("id", "uuid", "package_uuid"):
        if _looks_like_uuid(package.get(key)):
            return str(package[key])
    if _looks_like_uuid(package.get("package_id")):
        return str(package["package_id"])
    return None


def _package_label(package: dict) -> str:
    """How to name a package in a sentence: its reverse-DNS id, else its name."""
    for key in ("package_id", "display_name", "name"):
        value = package.get(key)
        if value and not _looks_like_uuid(value):
            return str(value)
    return str(_package_uuid(package) or "?")


def _package_matches(package: dict, ref: str) -> bool:
    """Does ``ref`` name this package exactly (id, reverse-DNS, or name)?"""
    ref_low = ref.strip().lower()
    for key in ("id", "package_id", "display_name", "name", "slug"):
        value = package.get(key)
        if value and str(value).lower() == ref_low:
            return True
    return False


def _resolve_skill_package(client: "AccountClient", ref: str) -> dict:
    """Resolve a package reference to its detail payload.

    ``ref`` is a UUID, a reverse-DNS package id (``localhost.skill.dad-jokes``)
    or a display name. Only the first is what the API wants, and it is the one
    nobody has: a package id is what the catalog prints and what a publish
    reports, so that is what the CLI accepts.

    A name that matches nothing is a *sentence* naming the search that failed,
    not a 404 — the id being wrong and the package not existing look identical
    from the outside, and only one of them has a next step.

    The returned payload always carries an addressable UUID, stamped in when
    the detail route did not repeat one: every caller addresses a route with
    it, and a `None` reaching a URL would fail as a 404 several calls later.
    """
    if _looks_like_uuid(ref):
        try:
            return _with_uuid(client.get_skill_package(ref), ref)
        except CinnaExit as exc:
            raise click.ClickException(
                f"No catalog package with id {ref} ({exc.detail}).\n"
                f"Run 'cinna skills catalog' to see what this account can install."
            )

    candidates = _rows(client.list_skill_catalog(), "packages", "catalog")

    exact = [p for p in candidates if _package_matches(p, ref)]
    matches = exact or [
        p
        for p in candidates
        if ref.strip().lower() in str(p.get("package_id") or "").lower()
        or ref.strip().lower() in str(p.get("display_name") or "").lower()
    ]
    if len(matches) > 1:
        rows = ", ".join(f"{_package_label(p)} ({_package_uuid(p)})" for p in matches[:10])
        raise click.ClickException(
            f"Package reference '{ref}' is ambiguous — matches: {rows}.\n"
            f"Use the full package id instead."
        )
    if not matches:
        available = ", ".join(_package_label(p) for p in candidates[:10]) or "none"
        raise click.ClickException(
            f"No catalog package matches '{ref}'.\n"
            f"Visible packages: {available}\n"
            f"Run 'cinna skills catalog' to see the full list."
        )

    uuid = _package_uuid(matches[0])
    if not uuid:
        # A catalog row without a resolvable UUID cannot address any route;
        # returning it would fail later with a worse message.
        raise click.ClickException(
            f"The catalog row for '{_package_label(matches[0])}' carries no "
            f"package id the API can address. Pass the package UUID directly."
        )
    return _with_uuid(client.get_skill_package(uuid), uuid)


def _with_uuid(package: dict, uuid: str) -> dict:
    """The package detail, guaranteed to carry the UUID it was fetched by."""
    if _package_uuid(package):
        return package
    return {**package, "id": uuid}


def _resolve_installed_addon(payload: dict, agent_name: str, name: str) -> dict:
    """The addon row an agent carries under ``name``.

    ``name`` is what `cinna skills list` prints in the Name column — the
    engine-facing folder name — because that is the string the user already
    has. Everything else about the row (its link id, its package) is looked up
    from here.
    """
    addons = payload.get("addons") or []
    exact = [a for a in addons if a.get("name") == name]
    matches = exact or [
        a
        for a in addons
        if str(a.get("name") or "").lower() == name.strip().lower()
        or str(a.get("display_name") or "").lower() == name.strip().lower()
    ]
    # The prose names the agent as the user sees it; a *command* has to name
    # it as the user can type it — an unquoted display name ("MickyJoker -
    # BundleTest") is not a runnable suggestion.
    ref = normalize_agent_dir_name(agent_name) or agent_name
    if len(matches) > 1:
        raise click.ClickException(
            f"'{name}' matches more than one addon on {agent_name}. "
            f"Run 'cinna skills list {ref}' and use the exact name."
        )
    if not matches:
        carried = ", ".join(str(a.get("name")) for a in addons) or "none"
        raise click.ClickException(
            f"{agent_name} carries no addon named '{name}'.\n"
            f"It carries: {carried}\n"
            f"Run 'cinna skills list {ref}' to see them."
        )
    return matches[0]


def _addon_link_id(addon: dict) -> str | None:
    """The plugin-link id behind an addon row, if it has one.

    An install is a link row, and the routes that change one address it by that
    id. It is ``link.id`` on the rows that have a link, and it is also encoded
    in the row ``key`` (``plugin:<link id>``) — the fallback exists because the
    key is the one field the dedupe rule guarantees.
    """
    link_id = _addon_link(addon).get("id")
    if link_id:
        return str(link_id)
    key = str(addon.get("key") or "")
    if key.startswith("plugin:"):
        return key.split(":", 1)[1] or None
    return None


def _require_link_id(addon: dict, agent_name: str, verb: str) -> str:
    """The link id, or the reason this verb does not apply to this row.

    A local ``skills/<name>/`` folder is the common case here: it is the
    agent's *own* skill, not an install, so there is nothing to uninstall,
    upgrade or toggle — and saying so is more useful than a 404 from a route
    that was never going to match.
    """
    link_id = _addon_link_id(addon)
    if link_id:
        return link_id
    name = addon.get("name", "?")
    if addon.get("source") == "local":
        raise click.ClickException(
            f"'{name}' is one of {agent_name}'s own skills — a skills/{name}/ "
            f"folder in its workspace, not an install from the catalog, so "
            f"there is nothing to {verb}.\n"
            f"Edit or delete the folder and sync, or use 'cinna skills delist' "
            f"to take its published package out of the catalog."
        )
    raise click.ClickException(
        f"Could not determine the install id for '{name}' on {agent_name}. "
        f"Run 'cinna skills list "
        f"{normalize_agent_dir_name(agent_name) or agent_name} --json' to "
        f"inspect the row."
    )


def _agent_ref_for_hint(agent: dict) -> str:
    """How to spell this agent back at the user in a suggested command."""
    return normalize_agent_dir_name(agent.get("name", "")) or str(agent.get("id", ""))


def _index_error_remedy(code: str, ref: str) -> list[str]:
    """What to actually DO about an unreadable skill index, per reason code.

    One code, one remedy, and the codes exist precisely because the remedies
    differ. This printed the restart hint for every code, including the one
    where restarting is the one action guaranteed not to help: a container
    built before agent skills has no ``/config/skills`` route, and re-running
    the same image cannot grow one. The advice could not succeed, and the
    action that does — a rebuild — had no verb to name.

    All four codes the platform emits (``env_not_running``, ``adapter_error``,
    ``adapter_unsupported``, ``parse_error``) are named here. The fallback is
    for a code this build has never heard of, and is deliberately the only
    branch that names no verb — see below.
    """
    if code == "adapter_unsupported":
        return [
            "This environment was built before agent skills existed, so it has "
            "no skills endpoint to answer.",
            f"Rebuild it: cinna agent rebuild-env {ref}",
        ]
    if code == "env_not_running":
        return [
            "The environment is asleep. Send it a message, or refresh again, "
            "to wake it.",
        ]
    if code == "adapter_error":
        return [
            "The environment is not answering.",
            f"Restart it: cinna agent restart-env {ref}",
        ]
    if code == "parse_error":
        return [
            "The environment answered, but its skill index did not parse.",
            f"Re-read it: cinna skills refresh {ref}",
        ]
    # An unknown code is one whose fix this build cannot name. Naming a remedy
    # anyway is how the caller ends up in a loop that cannot close.
    return ["Refresh again; if it persists, check the environment's logs."]


def _print_plugin_sync(result: dict) -> None:
    """The environment half of a plugin mutation, when it has something to say.

    Every install / uninstall / upgrade / toggle answers a
    ``PluginSyncResponse``: the link row is written, and then the change is
    pushed into the agent's running environments — which can partly fail
    (``partial_failures``, ``failed_syncs``) while the call still returns 200.
    A command that printed only its own success would hide exactly the case
    where the catalog and the running agent disagree.
    """
    if not isinstance(result, dict):
        return
    failed = result.get("failed_syncs") or 0
    unsupported = result.get("unsupported_syncs") or 0
    total = result.get("total_environments") or 0

    # A pre-feature environment is reported first and on its own terms. It is
    # not a failed sync — the link write is complete and correct, and there is
    # nothing to retry — so the "or try again" copy below would be an
    # instruction to repeat something that already worked, followed by a
    # restart that cannot change the outcome.
    if unsupported:
        console.warn(
            f"The link is updated, but {unsupported} of {total} "
            f"environment(s) were built before this feature existed and "
            f"cannot pick it up."
        )
        console.console.print(
            "  [dim]Rebuild: cinna agent rebuild-env <agent>[/dim]"
        )

    if result.get("partial_failures") or failed:
        console.warn(
            f"The link is updated, but {failed} of {total} environment(s) did "
            f"not pick it up. Restart the environment "
            f"('cinna agent restart-env <agent>') or try again."
        )
    elif not unsupported and result.get("message"):
        console.console.print(f"  [dim]{_esc(result['message'])}[/dim]")


def _plugin_link(result: dict) -> dict:
    """The link row a ``PluginSyncResponse`` carries back, or an empty dict."""
    link = result.get("plugin_link") if isinstance(result, dict) else None
    return link if isinstance(link, dict) else {}


def _mode_cell(addon_or_link: dict) -> str:
    """Where an installed skill is offered — conversation, building, or both."""
    modes = []
    if addon_or_link.get("conversation_mode"):
        modes.append("conversation")
    if addon_or_link.get("building_mode"):
        modes.append("building")
    return " + ".join(modes) if modes else "—"


# ── install / uninstall / update / toggle ───────────────────────────────────


def _already_installed_message(
    client: "AccountClient", agent: dict, package: dict, detail: str
) -> str:
    """Turn the install route's 409 into a sentence with a next step.

    The refusal is not a failure of the user's intent — the skill *is* on the
    agent — so the only useful thing to add is which of the two states they are
    in: carrying the newest revision, or carrying an older one that
    `cinna skills update` would move. That answer is in the addons listing, and
    a listing that cannot be read simply costs the extra clause.
    """
    label = _package_label(package)
    lines = [detail or f"{label} is already installed on {agent['name']}."]
    try:
        addons = client.get_agent_addons(agent["id"])
    except Exception as exc:  # noqa: BLE001 — the refusal is the message
        logger.debug("addons lookup after already_installed failed: %s", exc)
        return lines[0]

    ref = _agent_ref_for_hint(agent)
    for addon in addons.get("addons") or []:
        if not _package_matches_addon(addon, package):
            continue
        link = _addon_link(addon)
        if link.get("has_update"):
            latest = link.get("latest_version") or "a newer revision"
            lines.append(
                f"A newer revision is available ({latest}). Update with: "
                f"cinna skills update {ref} {addon.get('name')}"
            )
        else:
            version = link.get("installed_version") or addon.get("version")
            lines.append(
                "It is already at the newest revision"
                + (f" ({version})" if version else "")
                + f". Run 'cinna skills list {ref}' to see it."
            )
        break
    return "\n".join(lines)


def _package_matches_addon(addon: dict, package: dict) -> bool:
    """Is this addon row the install of this package?"""
    uuid = _package_uuid(package)
    label = _package_label(package)
    # `link.skill_package_id` is the authoritative answer for a catalog
    # install; `published_package_id` answers it for the publisher's own copy.
    for value in (
        _addon_link(addon).get("skill_package_id"),
        addon.get("published_package_id"),
    ):
        if value and str(value) in (uuid, label):
            return True
    name = str(addon.get("name") or "").lower()
    return bool(name) and label.lower().endswith(f".{name}")


def run_skills_install(
    agent_ref: str,
    package_ref: str,
    revision: int | None = None,
    conversation_only: bool = False,
    building_only: bool = False,
    as_json: bool = False,
) -> None:
    """Install a catalog package onto an agent — `cinna skills install`."""
    if conversation_only and building_only:
        raise click.UsageError(
            "--conversation-only and --building-only are opposites: naming both "
            "would install a skill that is offered nowhere. Pick one, or "
            "neither for both modes."
        )
    conversation_mode = not building_only
    building_mode = not conversation_only

    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        agent = _resolve_one_agent(client, agent_ref)
        with console.spinner(f"Resolving '{package_ref}'..."):
            package = _resolve_skill_package(client, package_ref)
        package_uuid = _package_uuid(package)
        label = _package_label(package)

        try:
            with console.spinner(f"Installing '{label}'..."):
                result = client.install_skill_on_agent(
                    agent["id"],
                    package_uuid,
                    revision_number=revision,
                    conversation_mode=conversation_mode,
                    building_mode=building_mode,
                )
        except CodedRefusal as exc:
            if exc.code != "already_installed":
                raise
            # Re-raised rather than caught: the exit code and the machine code
            # are the contract a --json driver reads, and both are the server's.
            # Only the sentence grows a next step.
            raise CodedRefusal(
                exc.status_code,
                exc.code,
                _already_installed_message(client, agent, package, exc.detail),
            )

    if as_json:
        click.echo(json.dumps(result, indent=2, default=str))
        return

    console.status(f"Installed '{_esc(label)}' on {_esc(agent['name'])}")
    link = _plugin_link(result)
    version = link.get("installed_version")
    if version:
        console.console.print(f"  Version:    {_esc(version)}")
    elif revision is not None:
        console.console.print(f"  Revision:   {revision}")
    console.console.print(f"  Modes:      {_mode_cell(link) if link else _mode_cell({'conversation_mode': conversation_mode, 'building_mode': building_mode})}")
    _print_plugin_sync(result)
    console.console.print(
        f"  [dim]Verify with: cinna skills list {_agent_ref_for_hint(agent)}[/dim]"
    )


def _require_yes_for_json(as_json: bool, yes: bool, what: str) -> None:
    """A destructive verb under ``--json`` has to be told to go ahead.

    The confirmation cannot simply be skipped the way `publish` skips its
    preview: publish *creates*, and a driver that meant it loses nothing by
    proceeding. Here the default has to be "don't", and printing a prompt into
    a JSON stream would corrupt it — so the flag is required rather than
    assumed.
    """
    if as_json and not yes:
        raise click.UsageError(
            f"--json needs --yes for {what}: the confirmation cannot be asked "
            f"without corrupting the JSON stream, and it is not assumed."
        )


def run_skills_uninstall(
    agent_ref: str, name: str, yes: bool = False, as_json: bool = False
) -> None:
    """Remove an installed skill from an agent — `cinna skills uninstall`."""
    _require_yes_for_json(as_json, yes, "uninstall")
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        agent = _resolve_one_agent(client, agent_ref)
        with console.spinner("Fetching addons..."):
            payload = client.get_agent_addons(agent["id"])
        addon = _resolve_installed_addon(payload, agent["name"], name)
        link_id = _require_link_id(addon, agent["name"], "uninstall")

        if not yes:
            if not console.confirm(
                f"Remove '{addon.get('name')}' from {agent['name']}?", default=False
            ):
                raise click.Abort()

        with console.spinner(f"Removing '{addon.get('name')}'..."):
            result = client.uninstall_agent_plugin(agent["id"], link_id)

    if as_json:
        click.echo(json.dumps(result or {"removed": name}, indent=2, default=str))
        return

    console.status(f"Removed '{_esc(addon.get('name', name))}' from {_esc(agent['name'])}")
    _print_plugin_sync(result)
    console.console.print(
        "  [dim]The package and its revisions are untouched — re-install with: "
        f"cinna skills install {_agent_ref_for_hint(agent)} <package>[/dim]"
    )


def run_skills_update(agent_ref: str, name: str, as_json: bool = False) -> None:
    """Move an installed skill to the newest revision — `cinna skills update`."""
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        agent = _resolve_one_agent(client, agent_ref)
        with console.spinner("Fetching addons..."):
            payload = client.get_agent_addons(agent["id"])
        addon = _resolve_installed_addon(payload, agent["name"], name)
        link_id = _require_link_id(addon, agent["name"], "update")
        before = _addon_link(addon).get("installed_version") or addon.get("version")

        # The listing is the server's *cache*, so "no update available" here is
        # a stale answer as often as a true one — reported, never used to skip
        # the call. The upgrade route decides.
        with console.spinner(f"Updating '{addon.get('name')}'..."):
            result = client.upgrade_agent_plugin(agent["id"], link_id)

    if as_json:
        click.echo(json.dumps(result, indent=2, default=str))
        return

    after = _plugin_link(result).get("installed_version")
    console.status(f"Updated '{_esc(addon.get('name', name))}' on {_esc(agent['name'])}")
    if before and after and before != after:
        console.console.print(f"  Version:    {_esc(before)} → {_esc(after)}")
    elif after:
        console.console.print(f"  Version:    {_esc(after)}")
    elif before:
        console.console.print(
            f"  [dim]Was {_esc(before)}; run 'cinna skills list "
            f"{_agent_ref_for_hint(agent)}' for the new version.[/dim]"
        )
    _print_plugin_sync(result)


def run_skills_toggle(
    agent_ref: str,
    name: str,
    enable: bool | None = None,
    conversation_mode: bool | None = None,
    building_mode: bool | None = None,
    as_json: bool = False,
) -> None:
    """Enable/disable an installed skill or move its modes — `cinna skills toggle`."""
    if enable is None and conversation_mode is None and building_mode is None:
        raise click.UsageError(
            "Nothing to change. Name at least one of --enable/--disable, "
            "--conversation-mode/--no-conversation-mode or "
            "--building-mode/--no-building-mode."
        )

    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        agent = _resolve_one_agent(client, agent_ref)
        with console.spinner("Fetching addons..."):
            payload = client.get_agent_addons(agent["id"])
        addon = _resolve_installed_addon(payload, agent["name"], name)
        link_id = _require_link_id(addon, agent["name"], "toggle")

        with console.spinner(f"Updating '{addon.get('name')}'..."):
            result = client.update_agent_plugin(
                agent["id"],
                link_id,
                enabled=enable,
                conversation_mode=conversation_mode,
                building_mode=building_mode,
            )

    if as_json:
        click.echo(json.dumps(result, indent=2, default=str))
        return

    console.status(f"Updated '{_esc(addon.get('name', name))}' on {_esc(agent['name'])}")

    # The link the server wrote back, which carries all three switches — so
    # the report is the resulting state, not a restatement of the request.
    # Falls back to the request only when the response carries no link.
    link = _plugin_link(result)
    if link:
        console.console.print(
            f"  Enabled:    {'[yellow]no[/yellow]' if link.get('disabled') else 'yes'}"
        )
        console.console.print(f"  Modes:      {_mode_cell(link)}")
    else:
        if enable is not None:
            console.console.print(
                f"  Enabled:    {'yes' if enable else '[yellow]no[/yellow]'}"
            )
        if conversation_mode is not None:
            console.console.print(
                f"  Conversation: {'on' if conversation_mode else '[yellow]off[/yellow]'}"
            )
        if building_mode is not None:
            console.console.print(
                f"  Building:     {'on' if building_mode else '[yellow]off[/yellow]'}"
            )
    _print_plugin_sync(result)


def run_skills_refresh(agent_ref: str, as_json: bool = False) -> None:
    """Rebuild an agent's addon index — `cinna skills refresh`.

    The recovery path behind every stale answer in this group: the addons route
    is cache-only, so a skill added, edited or versioned since the last index
    build is invisible until something refreshes it. This is that something.
    """
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        agent = _resolve_one_agent(client, agent_ref)
        with console.spinner("Refreshing skills..."):
            skills_result = client.refresh_agent_skills(agent["id"])
        # The plugin half is a second route and a second cache. A platform that
        # does not have it is not a failed refresh — the skills half, which is
        # the half people are waiting on, already succeeded.
        addons_result: dict | None = None
        try:
            with console.spinner("Refreshing plugins..."):
                addons_result = client.refresh_agent_addons(agent["id"])
        except CinnaExit as exc:
            logger.debug("addons refresh failed: %s", exc)

    if as_json:
        click.echo(
            json.dumps(
                {"skills": skills_result, "addons": addons_result},
                indent=2,
                default=str,
            )
        )
        return

    # A refresh that could not read the index answers **200** with the reason
    # in `error` — the rebuild ran, the environment did not answer it. Printing
    # a green check for that would send the reader back to `list` to discover
    # for themselves that nothing changed.
    error = (skills_result or {}).get("error")
    if error:
        console.warn(
            f"{agent['name']}'s skill index was rebuilt but still could not be "
            f"read ({error})."
        )
        for line in _index_error_remedy(error, _agent_ref_for_hint(agent)):
            console.console.print(f"  [dim]{line}[/dim]")
        return

    indexed = len((skills_result or {}).get("skills") or [])
    console.status(
        f"Refreshed {_esc(agent['name'])}'s addon index — {indexed} skill(s) indexed"
    )
    if addons_result is None:
        console.console.print(
            "  [dim]The plugin half could not be refreshed; the skill index was.[/dim]"
        )
    console.console.print(
        f"  [dim]cinna skills list {_agent_ref_for_hint(agent)}[/dim]"
    )


# ── catalog browsing ────────────────────────────────────────────────────────


def _catalog_matches(package: dict, needle: str) -> bool:
    """Does this package match a ``--search`` term?

    Client-side because ``GET /skills/catalog`` takes no parameters: it answers
    the whole visible catalogue. Matching the description too is what makes the
    term useful — a package is found by what it does at least as often as by
    what it is called.
    """
    needle = needle.strip().lower()
    return any(
        needle in str(package.get(key) or "").lower()
        for key in ("package_id", "name", "display_name", "description")
    )


def run_skills_catalog(
    search: str | None = None, mine: bool = False, as_json: bool = False
) -> None:
    """List the catalog packages this account can see — `cinna skills catalog`."""
    from rich.table import Table

    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with console.spinner("Fetching catalog..."):
        with AccountClient(account_cfg) as client:
            payload = client.list_skill_catalog()

    packages = _rows(payload, "packages", "catalog")
    if mine:
        # `can_manage` is the server's own answer to "is this yours" — a
        # publisher-id comparison would need a user id the account token does
        # not carry.
        packages = [p for p in packages if p.get("can_manage")]
    if search:
        packages = [p for p in packages if _catalog_matches(p, search)]

    if as_json:
        # The filtered rows, not the raw envelope: a driver that passed
        # --search must not be handed the packages it excluded.
        click.echo(json.dumps({"data": packages, "count": len(packages)}, indent=2, default=str))
        return

    if not packages:
        scope = "you published" if mine else "this account can see"
        console.console.print(
            f"[dim]No catalog packages {scope}"
            + (f" matching '{search}'." if search else ".")
            + "[/dim]"
        )
        return

    table = Table(title=f"Skills catalog ({len(packages)})", title_style="bold")
    table.add_column("#", style="dim", justify="right")
    table.add_column("Package", overflow="fold")
    table.add_column("Name")
    table.add_column("Visibility")
    table.add_column("Latest")

    for i, pkg in enumerate(packages, 1):
        display = pkg.get("display_name") or pkg.get("name") or ""
        visibility = _esc(pkg.get("visibility") or "—")
        if pkg.get("is_listed") is False:
            # A delisted package still answers every route and still installs;
            # it is simply not discoverable. Nothing else in the row says so.
            visibility += " [yellow]· delisted[/yellow]"
        table.add_row(
            str(i),
            _esc(_package_label(pkg)),
            _esc(display),
            visibility,
            _esc(pkg.get("latest_version") or ""),
        )

    console.console.print(table)
    console.console.print(
        "[dim]Install with: cinna skills install <agent> <package>[/dim]"
    )


def _revision_rows(package: dict) -> list[dict]:
    """A package's revisions, newest first.

    Sorted here rather than trusted from the payload: the one question this
    list answers is "what is the newest", and a route that happened to return
    ascending order would put the answer at the bottom.
    """
    revisions = _rows(package.get("revisions"), "revisions")
    return sorted(
        revisions, key=lambda r: r.get("revision_number") or 0, reverse=True
    )


def _print_revisions(package: dict) -> None:
    from rich.table import Table

    revisions = _revision_rows(package)
    if not revisions:
        console.console.print("[dim]This package has no revisions yet.[/dim]")
        return

    table = Table(
        title=f"{_package_label(package)} — revisions ({len(revisions)})",
        title_style="bold",
    )
    table.add_column("Rev", style="dim", justify="right")
    table.add_column("Version")
    table.add_column("Released", style="dim")
    table.add_column("Size", justify="right", style="dim")
    table.add_column("Release notes")

    for rev in revisions:
        size = rev.get("size_bytes") or rev.get("size")
        table.add_row(
            str(rev.get("revision_number", "?")),
            _esc(rev.get("version") or ""),
            # `published_at`, not `created_at`: the payload carries both and
            # only the first is ever filled.
            str(rev.get("published_at") or rev.get("created_at") or "")[:19],
            _format_size(size),
            # `release_notes`, not `notes`: the field a publisher fills at
            # publish time is the field this column has to read.
            _esc(rev.get("release_notes") or ""),
        )

    console.console.print(table)


def _format_size(size) -> str:
    """A byte count as something readable, or blank when it was not reported."""
    try:
        value = float(size)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def _pick_revision(package: dict, revision: int | None) -> dict | None:
    """The revision to act on: the one asked for, else the newest."""
    revisions = _revision_rows(package)
    if not revisions:
        return None
    if revision is None:
        return revisions[0]
    for rev in revisions:
        if rev.get("revision_number") == revision:
            return rev
    available = ", ".join(str(r.get("revision_number")) for r in revisions)
    raise click.ClickException(
        f"{_package_label(package)} has no revision {revision}.\n"
        f"It has: {available}"
    )


def _revision_content(payload) -> str | None:
    """The SKILL.md text out of a revision-content answer."""
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return None
    for key in ("content", "skill_md", "text", "body", "markdown"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def run_skills_show(
    package_ref: str, revision: int | None = None, as_json: bool = False
) -> None:
    """Show one catalog package — `cinna skills show`."""
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        with console.spinner(f"Resolving '{package_ref}'..."):
            package = _resolve_skill_package(client, package_ref)
        target = _pick_revision(package, revision)
        content = None
        _is_truncated = False
        if target is not None:
            try:
                with console.spinner("Fetching SKILL.md..."):
                    content_payload = client.get_skill_revision_content(
                        _package_uuid(package), target.get("revision_number")
                    )
                content = _revision_content(content_payload)
                _is_truncated = bool(
                    isinstance(content_payload, dict)
                    and content_payload.get("truncated")
                )
            except CinnaExit as exc:
                # The package detail is the answer; its SKILL.md is a bonus.
                logger.debug("revision content fetch failed: %s", exc)

    if as_json:
        click.echo(
            json.dumps(
                {"package": package, "revision": target, "content": content},
                indent=2,
                default=str,
            )
        )
        return

    console.console.print(f"[bold]{_esc(_package_label(package))}[/bold]")
    display = package.get("display_name") or package.get("name")
    if display:
        console.console.print(f"  Name:       {_esc(display)}")
    if package.get("description"):
        console.console.print(f"  About:      {_esc(package['description'])}")
    publisher = (package.get("publisher_name") or "").strip()
    if publisher:
        console.console.print(f"  Publisher:  {_esc(publisher)}")
    visibility = _esc(package.get("visibility") or "—")
    if package.get("is_listed") is False:
        visibility += " [yellow]· delisted (not discoverable)[/yellow]"
    console.console.print(f"  Visibility: {visibility}")
    if package.get("latest_version"):
        console.console.print(f"  Latest:     {_esc(package['latest_version'])}")
    uuid = _package_uuid(package)
    if uuid:
        console.console.print(f"  Id:         [dim]{uuid}[/dim]")
    console.console.print()
    _print_revisions(package)

    if content:
        console.console.print()
        header = f"SKILL.md (revision {target.get('revision_number')})"
        console.console.print(f"[dim]{header}[/dim]")
        # Printed as plain text, not Rich markup: a SKILL.md is someone else's
        # file and may hold anything, including square brackets.
        console.console.print(content, markup=False, highlight=False)
        if _is_truncated:
            # The route caps what it returns. Printing the cap as if it were
            # the file would misreport the one artifact this verb exists to
            # show.
            console.warn(
                "The server truncated this SKILL.md — what is printed above is "
                "not the whole file."
            )


def run_skills_revisions(package_ref: str, as_json: bool = False) -> None:
    """List a package's revisions — `cinna skills revisions`."""
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        with console.spinner(f"Resolving '{package_ref}'..."):
            package = _resolve_skill_package(client, package_ref)

    if as_json:
        click.echo(json.dumps(_revision_rows(package), indent=2, default=str))
        return

    _print_revisions(package)
    console.console.print(
        "[dim]A revision is immutable — publishing again appends one. "
        "Preview the next with: cinna skills publish <agent> <name> --dry-run[/dim]"
    )


def run_skills_files(
    package_ref: str, revision: int | None = None, as_json: bool = False
) -> None:
    """List the files one revision ships — `cinna skills files`."""
    from rich.table import Table

    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        with console.spinner(f"Resolving '{package_ref}'..."):
            package = _resolve_skill_package(client, package_ref)
        target = _pick_revision(package, revision)
        if target is None:
            raise click.ClickException(
                f"{_package_label(package)} has no revisions to list files for."
            )
        with console.spinner("Fetching file list..."):
            payload = client.get_skill_revision_files(
                _package_uuid(package), target.get("revision_number")
            )

    if as_json:
        click.echo(json.dumps(payload, indent=2, default=str))
        return

    files = _rows(payload, "files")
    if not files:
        console.console.print("[dim]This revision reports no files.[/dim]")
        return

    table = Table(
        title=(
            f"{_package_label(package)} rev {target.get('revision_number')} "
            f"— files ({len(files)})"
        ),
        title_style="bold",
    )
    table.add_column("#", style="dim", justify="right")
    table.add_column("Path", overflow="fold")
    table.add_column("Size", justify="right", style="dim")

    for i, entry in enumerate(files, 1):
        path = entry.get("path") or entry.get("name") or "?"
        table.add_row(str(i), _esc(path), _format_size(entry.get("size_bytes") or entry.get("size")))

    console.console.print(table)
    total = payload.get("total_size_bytes") if isinstance(payload, dict) else None
    if total:
        console.console.print(f"[dim]{_format_size(total)} in total.[/dim]")
    if isinstance(payload, dict) and payload.get("truncated"):
        console.warn("The server truncated this file list; it is not complete.")


# ── sharing: grants, visibility, delist ─────────────────────────────────────


def _grant_email(grant: dict) -> str | None:
    for key in ("email", "user_email"):
        if grant.get(key):
            return str(grant[key])
    user = grant.get("user")
    if isinstance(user, dict) and user.get("email"):
        return str(user["email"])
    return None


def _grant_user_id(grant: dict) -> str | None:
    """The **user** id on a grant row.

    Deliberately not falling back to the row's own ``id``: a grant carries
    both, and revoking addresses the user. Sending the grant id would delete
    nothing and report success.
    """
    if grant.get("user_id"):
        return str(grant["user_id"])
    user = grant.get("user")
    if isinstance(user, dict) and user.get("id"):
        return str(user["id"])
    return None


def _print_grants(package: dict, grants: list[dict]) -> None:
    from rich.table import Table

    visibility = package.get("visibility")
    if not grants:
        console.console.print(
            f"[dim]Nobody is named on {_esc(_package_label(package))}.[/dim]"
        )
    else:
        table = Table(
            title=f"{_package_label(package)} — grants ({len(grants)})",
            title_style="bold",
        )
        table.add_column("#", style="dim", justify="right")
        table.add_column("User")
        table.add_column("Granted", style="dim")
        for i, grant in enumerate(grants, 1):
            table.add_row(
                str(i),
                _esc(_grant_email(grant) or _grant_user_id(grant) or "?"),
                str(grant.get("created_at") or "")[:19],
            )
        console.console.print(table)

    # The grant list is real on any package; it only *does* anything on one
    # whose visibility is `users`. Saying so here is what stops a publisher
    # concluding they have shared something they have not.
    console.console.print(f"[dim]Visibility: {_esc(visibility or '—')}[/dim]")
    if grants and visibility != "users":
        console.warn(
            f"This package is '{visibility}', so its grant list is ignored — "
            f"only a 'users' package consults it.\n"
            f"Run: cinna skills visibility {_package_label(package)} users"
        )


def run_skills_grants(package_ref: str, as_json: bool = False) -> None:
    """Who is named on a package — `cinna skills grants`."""
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        with console.spinner(f"Resolving '{package_ref}'..."):
            package = _resolve_skill_package(client, package_ref)
        with console.spinner("Fetching grants..."):
            payload = client.list_skill_grants(_package_uuid(package))

    if as_json:
        click.echo(json.dumps(payload, indent=2, default=str))
        return

    _print_grants(package, _rows(payload, "grants"))


def run_skills_grant(package_ref: str, email: str, as_json: bool = False) -> None:
    """Name one person on a package — `cinna skills grant`."""
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        with console.spinner(f"Resolving '{package_ref}'..."):
            package = _resolve_skill_package(client, package_ref)
        with console.spinner(f"Granting access to {email}..."):
            result = client.grant_skill_access(_package_uuid(package), email)

    if as_json:
        click.echo(json.dumps(result, indent=2, default=str))
        return

    console.status(f"Granted {_esc(email)} access to {_esc(_package_label(package))}")
    if package.get("visibility") != "users":
        console.warn(
            f"The package is '{package.get('visibility')}', which ignores its "
            f"grant list — the grant is stored but shares nothing.\n"
            f"Run: cinna skills visibility {_package_label(package)} users"
        )


def run_skills_revoke(
    package_ref: str, email: str, yes: bool = False, as_json: bool = False
) -> None:
    """Take one person's access away — `cinna skills revoke`."""
    _require_yes_for_json(as_json, yes, "revoke")
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        with console.spinner(f"Resolving '{package_ref}'..."):
            package = _resolve_skill_package(client, package_ref)
        with console.spinner("Fetching grants..."):
            grants = _rows(client.list_skill_grants(_package_uuid(package)), "grants")

        # Revoke is addressed by user id; nobody has one. Resolving the email
        # against the grant list also makes "they were never granted" a
        # sentence instead of a 404.
        target = next(
            (
                g
                for g in grants
                if (_grant_email(g) or "").lower() == email.strip().lower()
            ),
            None,
        )
        if target is None:
            # "Named: nobody" beside an email address reads as a username, so
            # an empty list gets a sentence rather than a placeholder.
            named = [e for e in (_grant_email(g) for g in grants) if e]
            detail = (
                f"Named: {', '.join(named)}"
                if named
                else "Nobody is named on it."
            )
            raise click.ClickException(
                f"{email} is not named on {_package_label(package)}.\n{detail}"
            )
        user_id = _grant_user_id(target)
        if not user_id:
            raise click.ClickException(
                f"The grant for {email} carries no user id to revoke. "
                f"Run 'cinna skills grants {package_ref} --json' to inspect it."
            )

        if not yes:
            if not console.confirm(
                f"Revoke {email}'s access to {_package_label(package)}?",
                default=False,
            ):
                raise click.Abort()

        with console.spinner(f"Revoking {email}..."):
            result = client.revoke_skill_access(_package_uuid(package), user_id)

    if as_json:
        click.echo(json.dumps(result or {"revoked": email}, indent=2, default=str))
        return

    console.status(f"Revoked {_esc(email)} from {_esc(_package_label(package))}")


def run_skills_visibility(
    package_ref: str, visibility: str, as_json: bool = False
) -> None:
    """Change who may see a package — `cinna skills visibility`."""
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        with console.spinner(f"Resolving '{package_ref}'..."):
            package = _resolve_skill_package(client, package_ref)
        before = package.get("visibility")
        with console.spinner(f"Setting visibility to {visibility}..."):
            result = client.update_skill_package(
                _package_uuid(package), visibility=visibility
            )
        grants = []
        if visibility == "users":
            try:
                grants = _rows(client.list_skill_grants(_package_uuid(package)), "grants")
            except CinnaExit as exc:
                logger.debug("grant lookup after visibility change failed: %s", exc)

    if as_json:
        click.echo(json.dumps(result, indent=2, default=str))
        return

    console.status(
        f"{_esc(_package_label(package))} is now [bold]{_esc(visibility)}[/bold]"
        + (f" (was {_esc(before)})" if before and before != visibility else "")
    )
    if visibility == "users" and not grants:
        # `users` with an empty grant list is the one visibility that shares
        # with nobody while looking like sharing.
        console.warn(
            "Nobody is named on this package yet, so 'users' shares it with "
            "nobody.\n"
            f"Run: cinna skills grant {_package_label(package)} --user <email>"
        )


def run_skills_relist(package_ref: str, as_json: bool = False) -> None:
    """Put a delisted package back in the catalog — `cinna skills relist`.

    `delist` is a `POST` of its own and has no inverse route; the undo is the
    package PATCH. Without this verb the only way back is `cinna api`, which
    would make delist the one door in this group that opens only outwards.
    """
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        with console.spinner(f"Resolving '{package_ref}'..."):
            package = _resolve_skill_package(client, package_ref)
        with console.spinner("Relisting..."):
            result = client.update_skill_package(
                _package_uuid(package), is_listed=True
            )

    if as_json:
        click.echo(json.dumps(result, indent=2, default=str))
        return

    console.status(f"Relisted {_esc(_package_label(package))}")
    console.console.print(
        f"  [dim]Discoverable again to whoever its visibility "
        f"({_esc(package.get('visibility') or '—')}) allows.[/dim]"
    )


def run_skills_delist(
    package_ref: str, yes: bool = False, as_json: bool = False
) -> None:
    """Take a package out of the catalog — `cinna skills delist`."""
    _require_yes_for_json(as_json, yes, "delist")
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    with AccountClient(account_cfg) as client:
        with console.spinner(f"Resolving '{package_ref}'..."):
            package = _resolve_skill_package(client, package_ref)

        if not yes:
            console.console.print(
                f"Delisting [bold]{_esc(_package_label(package))}[/bold] hides it "
                f"from the catalog. Agents that already installed it keep what "
                f"they have."
            )
            if not console.confirm("Delist?", default=False):
                raise click.Abort()

        with console.spinner("Delisting..."):
            result = client.delist_skill_package(_package_uuid(package))

    if as_json:
        click.echo(json.dumps(result or {"delisted": package_ref}, indent=2, default=str))
        return

    console.status(f"Delisted {_esc(_package_label(package))}")
    console.console.print(
        f"  [dim]Put it back with: cinna skills relist {_package_label(package)}[/dim]"
    )
