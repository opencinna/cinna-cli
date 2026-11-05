"""``cinna dashboards`` — build, check, run and place an agent's dashboard components.

A **component** is a folder an agent keeps in ``dashboards/<name>/`` (or ships
inside a skill, ``skills/<skill>/dashboards/<name>/``): a ``component.json``
plus endpoint scripts or an html page. The owner puts it on a dashboard as a
live **tile**. cinna-core hosts tiles for cloud agents on its web dashboards;
Cinna Desktop hosts them for kit agents and for the owner's own cloud agents.

What the CLI adds for a developer and their local assistant:

* ``list`` / ``show`` / ``check`` / ``run`` / ``ref`` — the components as core
  sees them in the agent's environment: the env's own checker findings, a run
  with exactly what a tile receives (owner timezone, credential gate, runner
  limits), and the reference line the assistant prints to offer one.
* ``boards`` / ``board create`` / ``add`` / ``tiles`` / ``tile …`` — the owner's
  **web** dashboards and the tiles on them. Desktop's backing dashboards
  (``origin: "desktop"``) belong to Cinna Desktop's layout: the CLI reads them
  but never changes them.

Rules this module keeps (see ``docs/features/dashboards``):

* The environment is the runtime: nothing here runs an endpoint locally.
  ``check --run`` runs the env's checker through ``cinna exec``.
* Every verb that reads the cloud copy first pushes pending local edits
  (``--no-flush`` skips it), so a check never reads a stale file.
* Runs carry ``tile_key: "cli"`` so they have their own 30-a-minute budget
  instead of eating the Preview dialog's.
* Never a ``…/frame`` route: a frame URL is a 12-hour credential.
* A refusal keeps core's sentence and adds the CLI's next step.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import time
import uuid
from pathlib import Path

import click
from rich.markup import escape

from cinna import console
from cinna.errors import EXIT_ERROR, CinnaExit, ComponentRefusal

logger = logging.getLogger("cinna.dashboards")

SUPPORTED_VERSION = 1
REF_PREFIX = "cinna://component/"
CLI_TILE_KEY = "cli"
SHORT_ID_LEN = 8

# Fallbacks for a limit the capabilities answer leaves out.
DEFAULT_LIMITS = {
    "runs_per_minute": 30,
    "blocks_per_dashboard": 20,
    "dashboards_per_user": 10,
    "context_chars": 12_000,
    "summary_chars": 8_000,
}

# component.json `size` on core's 12-column grid (core's SIZE_GRID).
SIZE_GRID = {
    "s": (3, 2),
    "m": (6, 2),
    "l": (6, 4),
    "wide": (12, 2),
    "tall": (3, 4),
}
DEFAULT_SIZE = "s"

# Block config refresh overrides, as core validates them.
_DURATION_RE = re.compile(r"^[1-9][0-9]*(s|m|h|d)$")
_DURATION_1M_RE = re.compile(r"^(([6-9][0-9]|[1-9][0-9]{2,})s|[1-9][0-9]*(m|h|d))$")

ENV_CHECKER = "/app/core/skills/cinna-dashboards/scripts/check_component.py"
ENV_WORKSPACE = "/app/workspace"

# How long a verb waits for a waking environment, by default (seconds).
DEFAULT_WAKE_TIMEOUT = 120

# Swapped out in tests.
_sleep = time.sleep


# ─── session, capability gate, refusals ──────────────────────────────────────


class _Session:
    def __init__(self, account_root: Path, config, client, capabilities: dict):
        self.account_root = account_root
        self.config = config
        self.client = client
        self.capabilities = capabilities
        self._agents: list[dict] | None = None

    def limit(self, name: str) -> int:
        limits = (self.capabilities or {}).get("limits") or {}
        value = limits.get(name)
        return value if isinstance(value, int) and value > 0 else DEFAULT_LIMITS[name]

    @property
    def agents(self) -> list[dict]:
        if self._agents is None:
            self._agents = self.client.list_account_agents().get("data", []) or []
        return self._agents

    def agent(self, ref: str) -> dict:
        """An agent from the account listing: name, slug, id, or ``.`` (this workspace)."""
        from cinna.account import _resolve_account_agent

        if ref == ".":
            from cinna.config import find_workspace_root, load_config

            try:
                agent_id = load_config(find_workspace_root()).agent_id
            except Exception:
                raise click.UsageError(
                    "'.' means the agent of the workspace you are in, and this "
                    "is not an agent workspace. Name the agent instead."
                ) from None
            ref = agent_id
        return _resolve_account_agent(self.agents, ref)

    def agent_name(self, agent_id) -> str:
        for item in self.agents:
            if str(item.get("id")) == str(agent_id):
                return str(item.get("name") or agent_id)
        return _short(agent_id)

    def bundle_id(self, agent: dict) -> str | None:
        """The agent's bundle id, or None when it can't be read (never fatal)."""
        from cinna.account import _fetch_bundle_id

        if not agent.get("bundle_id"):
            agent["bundle_id"] = _fetch_bundle_id(self.client, agent["id"])
        return agent["bundle_id"]


@contextlib.contextmanager
def _session():
    """The account workspace's client, after checking the server can do dashboards.

    ``GET dashboards/capabilities`` must answer with version 1: no route means
    the server predates dashboard components, another version means this CLI
    can't speak it. Both are one clear refusal before any other call.
    """
    from cinna.account import find_account_root, load_account_config
    from cinna.client import AccountClient

    account_root = find_account_root()
    config = load_account_config(account_root)
    with AccountClient(config) as client:
        capabilities = client.get_dashboards_capabilities()
        if capabilities is None:
            from cinna.client import DashboardsUnsupported

            raise DashboardsUnsupported()
        if capabilities.get("version") != SUPPORTED_VERSION:
            raise CinnaExit(
                EXIT_ERROR,
                "dashboards_unsupported",
                "This Cinna server speaks dashboards version "
                f"{capabilities.get('version')!r}; this CLI speaks {SUPPORTED_VERSION}. "
                "Update the CLI (or ask the admin to update cinna-core).",
            )
        yield _Session(account_root, config, client, capabilities)


def remedy_for(error: ComponentRefusal, agent_label: str | None, runs_per_minute: int) -> str:
    """The CLI's next step for one of core's component refusal codes."""
    agent = agent_label or "<agent>"
    code = error.code
    if code == "adapter_unsupported":
        return (
            "The environment predates dashboard components. Rebuild it: "
            f"cinna agent rebuild-env {agent}"
        )
    if code == "env_unavailable":
        return "The environment is waking or stopped; try again in a minute (or raise --timeout)."
    if code == "needs_credentials":
        return (
            f"Link the credentials it names to the agent (see `cinna agent show {agent}` and "
            "`cinna account credentials list`). A requires_credentials name is the name of a "
            "credential linked to the agent (for an installed bundle, the bundle's credential "
            "spec name)."
        )
    if code == "rate_limited":
        wait = f" in {error.retry_after_s}s" if error.retry_after_s else " in a minute"
        return f"Core allows {runs_per_minute} runs a minute per tile or preview; try again{wait}."
    if code == "not_found":
        return f"See what exists: cinna dashboards list {agent}"
    if code == "invalid_input" and error.field:
        return f"(field: {error.field})"
    return ""


@contextlib.contextmanager
def _refusals(session: _Session | None, agent_label: str | None):
    """Add the CLI's remedy to any component refusal raised inside."""
    try:
        yield
    except ComponentRefusal as error:
        rpm = session.limit("runs_per_minute") if session else DEFAULT_LIMITS["runs_per_minute"]
        raise error.with_remedy(remedy_for(error, agent_label, rpm)) from None


def call_waking(fn, *args, timeout: float = DEFAULT_WAKE_TIMEOUT, **kwargs):
    """Call ``fn``; while core answers ``env_unavailable``, wait and retry within ``timeout``."""
    deadline = time.monotonic() + max(0.0, timeout)
    told = False
    while True:
        try:
            return fn(*args, **kwargs)
        except ComponentRefusal as error:
            remaining = deadline - time.monotonic()
            if error.code != "env_unavailable" or remaining <= 0:
                raise
            if not told:
                console.status("The environment is waking; waiting for it…")
                told = True
            _sleep(min(float(error.retry_after_s or 5), max(0.5, remaining)))


# ─── flush, references, params ───────────────────────────────────────────────


def _child_workspace(account_root: Path, agent_id: str):
    from cinna.account import list_child_workspaces

    for child, child_cfg in list_child_workspaces(account_root):
        if str(child_cfg.agent_id) == str(agent_id):
            return child, child_cfg
    return None


def flush_local_changes(account_root: Path, agent: dict, enabled: bool = True) -> None:
    """Push pending local edits of a synced agent to its environment, and say so.

    Only when the agent is synced under this account workspace **and** its
    sync session exists: the CLI never creates a session here (that is
    ``cinna agent sync`` / ``cinna sync push``). A flush failure is a warning:
    the remote read still happens, on whatever the environment has.
    """
    if not enabled:
        return
    found = _child_workspace(account_root, agent["id"])
    if found is None:
        return
    child, child_cfg = found
    from cinna import sync_session

    try:
        state = sync_session.status(child_cfg)
    except Exception as exc:  # noqa: BLE001 — Mutagen missing / daemon down
        logger.info("sync status unavailable for %s: %s", agent.get("name"), exc)
        return
    if not state.exists:
        return
    console.status(f"Pushing local changes of {escape(str(agent.get('name')))} to its environment first")
    try:
        with console.spinner("Flushing sync…"):
            sync_session.flush(child_cfg, workspace_root=child)
    except Exception as exc:  # noqa: BLE001
        console.warn(f"Sync flush failed ({escape(str(exc))}); reading what the environment has.")


def reference(bundle_id: str, name: str, skill: str | None = None) -> str:
    """The component reference an agent prints to offer a component (always ``bundle:``)."""
    middle = f"{skill}/" if skill else ""
    return f"{REF_PREFIX}bundle:{bundle_id}/{middle}{name}"


def parse_params(pairs: tuple[str, ...] | list[str], params_json: str | None = None) -> dict:
    """``--params @file.json`` (or inline JSON) overlaid with ``--param k=v`` pairs.

    A value is read as JSON first (``3``, ``true``, ``"x"``, ``null``) and is a
    plain string when it is not JSON. No validation here: core refuses a bad
    value with ``invalid_input`` naming the field.
    """
    params: dict = {}
    if params_json:
        raw = params_json
        if raw.startswith("@"):
            path = Path(raw[1:]).expanduser()
            if not path.is_file():
                raise click.UsageError(f"--params: no such file: {path}")
            raw = path.read_text()
        try:
            loaded = json.loads(raw)
        except ValueError as exc:
            raise click.UsageError(f"--params is not JSON: {exc}") from None
        if not isinstance(loaded, dict):
            raise click.UsageError("--params must be a JSON object.")
        params.update(loaded)
    for pair in pairs:
        key, sep, value = pair.partition("=")
        key = key.strip()
        if not sep or not key:
            raise click.UsageError(f"--param takes key=value, got {pair!r}.")
        try:
            params[key] = json.loads(value)
        except ValueError:
            params[key] = value
    return params


def refresh_overrides(
    max_age: str | None, every: str | None, after_turn: bool | None, base: dict | None = None
) -> dict:
    """A block's ``refresh`` overrides, checked the way core checks them."""
    refresh = dict(base or {})
    if max_age is not None:
        if not _DURATION_RE.match(max_age):
            raise click.UsageError("--max-age is a duration like 30s, 15m, 6h or 1d.")
        refresh["max_age"] = max_age
    if every is not None:
        if every != "manual" and not _DURATION_1M_RE.match(every):
            raise click.UsageError("--every is a duration of at least 1m (e.g. 5m), or 'manual'.")
        refresh["while_open_every"] = every
    if after_turn is not None:
        refresh["after_turn"] = after_turn
    return refresh


def size_grid(size: str | None) -> tuple[int, int]:
    return SIZE_GRID.get(size or DEFAULT_SIZE, SIZE_GRID[DEFAULT_SIZE])


def grid_size_name(w, h) -> str:
    for name, dims in SIZE_GRID.items():
        if dims == (w, h):
            return name
    return f"{w}×{h}"


# ─── formatting ──────────────────────────────────────────────────────────────


def _short(value) -> str:
    return str(value or "")[:SHORT_ID_LEN] or "?"


def _text(value, fallback: str = "—") -> str:
    if value is None or value == "":
        return fallback
    return escape(str(value))


def _plain(line: str) -> None:
    """A line printed verbatim: no markup, no highlighting, never wrapped."""
    console.console.print(line, markup=False, highlight=False, soft_wrap=True)


def _findings_count(component: dict) -> str:
    findings = [f for f in (component.get("findings") or []) if isinstance(f, dict)]
    errors = sum(1 for f in findings if _is_error(f))
    warnings = len(findings) - errors
    if not findings:
        return "[green]ok[/green]"
    parts = []
    if errors:
        parts.append(f"[red]{errors} E[/red]")
    if warnings:
        parts.append(f"[yellow]{warnings} W[/yellow]")
    return " / ".join(parts)


def _is_error(finding: dict) -> bool:
    """A finding is an error unless it says ``warning`` (the checker's default is error)."""
    return finding.get("severity") != "warning"


def placement_label(placement: dict) -> str:
    if placement.get("origin") == "desktop":
        return "Cinna Desktop"
    return str(placement.get("dashboard_name") or placement.get("name") or _short(placement.get("dashboard_id")))


def _placements_text(component: dict) -> str:
    placements = [p for p in (component.get("placements") or []) if isinstance(p, dict)]
    if not placements:
        return "—"
    labels: list[str] = []
    for placement in placements:
        label = placement_label(placement)
        if label not in labels:
            labels.append(label)
    return ", ".join(escape(label) for label in labels)


def finding_line(finding: dict, source: str | None = None) -> str:
    severity = "" if _is_error(finding) else "warning: "
    code = f" [{finding.get('code')}]" if finding.get("code") else ""
    prefix = f"({source}) " if source else ""
    return f"{prefix}{finding.get('path', '?')}: {severity}{finding.get('message', '')}{code}"


def _component_label(component: dict) -> str:
    skill = component.get("skill")
    return f"{skill}/{component.get('name')}" if skill else str(component.get("name"))


def _with_reference(component: dict, bundle_id: str | None) -> dict:
    out = dict(component)
    if bundle_id:
        out["reference"] = reference(bundle_id, component.get("name", ""), component.get("skill"))
    return out


def _require_yes_for_json(yes: bool, what: str) -> None:
    from cinna.account import _require_yes_for_json as require

    require(console.json_mode, yes, what)


# ─── boards and tiles ────────────────────────────────────────────────────────


class BoardNotFound(click.ClickException):
    """No dashboard matches the reference (the only case `add --new-board` creates one)."""


def _looks_like_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def _unique(matches: list[dict], ref: str, what: str, label) -> dict | None:
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(f"{label(m)} ({_short(m.get('id'))})" for m in matches)
        raise click.ClickException(f"{what} '{ref}' is ambiguous — matches: {names}. Use the id.")
    return None


def resolve_board(session: _Session, ref: str) -> dict:
    """A dashboard by id, unique 8-char id prefix, or exact name (web dashboards).

    A full id that is not among the web dashboards is fetched directly, so a
    desktop backing dashboard can be read by id (writes refuse it later).
    """
    ref = ref.strip()
    boards = [b for b in (session.client.list_dashboards("web") or []) if isinstance(b, dict)]
    for board in boards:
        if str(board.get("id")) == ref:
            return board
    found = _unique(
        [b for b in boards if str(b.get("name", "")).casefold() == ref.casefold()],
        ref, "Dashboard", lambda b: b.get("name"),
    )
    if found:
        return found
    if len(ref) >= 4 and not _looks_like_uuid(ref):
        found = _unique(
            [b for b in boards if str(b.get("id", "")).startswith(ref.lower())],
            ref, "Dashboard", lambda b: b.get("name"),
        )
        if found:
            return found
    if _looks_like_uuid(ref):
        from cinna.errors import PlatformError

        try:
            return session.client.get_dashboard(ref)
        except PlatformError as error:
            if error.status_code != 404:
                raise
    names = ", ".join(str(b.get("name")) for b in boards) or "none"
    raise BoardNotFound(
        f"No dashboard '{ref}'. Your dashboards: {names}\n"
        "Create one with `cinna dashboards board create <name>` (or `add … --new-board`)."
    )


def refuse_desktop_board(board: dict) -> None:
    if board.get("origin") == "desktop":
        raise CinnaExit(
            EXIT_ERROR,
            "desktop_dashboard",
            "This tile belongs to a Cinna Desktop dashboard; change it in Cinna Desktop "
            "(the desktop owns that dashboard's layout).",
        )


def _block_component(block: dict) -> dict:
    config = block.get("config")
    return config if isinstance(config, dict) else {}


def tile_title(block: dict) -> str:
    config = _block_component(block)
    return str(block.get("title") or config.get("component") or _short(block.get("id")))


def component_blocks(board: dict) -> list[dict]:
    return [
        b for b in (board.get("blocks") or [])
        if isinstance(b, dict) and b.get("view_type") == "agent_component"
    ]


def resolve_tile(board: dict, ref: str) -> dict:
    """A tile on ``board`` by block id, unique id prefix, title or component name."""
    ref = ref.strip()
    blocks = component_blocks(board)
    for block in blocks:
        if str(block.get("id")) == ref:
            return block
    found = _unique(
        [b for b in blocks if tile_title(b).casefold() == ref.casefold()],
        ref, "Tile", tile_title,
    )
    if found:
        return found
    found = _unique(
        [b for b in blocks if str(_block_component(b).get("component", "")) == ref],
        ref, "Tile", tile_title,
    )
    if found:
        return found
    if len(ref) >= 4:
        found = _unique(
            [b for b in blocks if str(b.get("id", "")).startswith(ref.lower())],
            ref, "Tile", tile_title,
        )
        if found:
            return found
    titles = ", ".join(tile_title(b) for b in blocks) or "none"
    raise click.ClickException(
        f"No component tile '{ref}' on '{board.get('name')}'. Tiles: {titles}"
    )


def _board_link(session: _Session, board: dict) -> str | None:
    if board.get("origin") == "desktop":
        return None
    base = (session.config.frontend_url or session.config.platform_url or "").rstrip("/")
    return f"{base}/dashboards/{board.get('id')}" if base else None


def _age(raw) -> str:
    if not raw:
        return "never"
    from datetime import datetime, timezone

    try:
        then = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return str(raw)
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    seconds = int((datetime.now(timezone.utc) - then).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


# ─── dashboard chat (used by `cinna chat --board`) ───────────────────────────


def build_dashboard_context(
    board: dict,
    agent_id: str,
    focused_block: dict | None,
    focused_data: dict | None,
    component_context: str | None,
    agent_name,
    max_chars: int = DEFAULT_LIMITS["context_chars"],
    summary_chars: int = DEFAULT_LIMITS["summary_chars"],
) -> str:
    """The ``<dashboard_context>`` JSON the way core's web panel builds it.

    ``{dashboard, focused{tile_id, component, skill, title, params, selection,
    data_age, summary?, context?}, also_yours[], other_tiles[]}`` — without a
    focused tile, ``focused`` is left out and the tiles are listed only. Kept
    under ``max_chars`` (core refuses longer): other agents' tiles go first,
    then the agent's other tiles, then the summary is cut. Nothing in it
    changes between two messages unless the dashboard did (``data_age`` is the
    fetch time, not "3 minutes ago"), so core's hash dedupe holds.
    """
    blocks = component_blocks(board)
    payload: dict = {"dashboard": {"id": str(board.get("id")), "name": board.get("name")}}
    focused: dict | None = None
    if focused_block is not None:
        config = _block_component(focused_block)
        data = focused_data or {}
        focused = {
            "tile_id": str(focused_block.get("id")),
            "component": config.get("component"),
            "skill": config.get("skill"),
            "title": tile_title(focused_block),
            "params": data.get("params_resolved") or config.get("params") or {},
            "selection": [],
            "data_age": data.get("fetched_at"),
        }
        summary = _data_summary(data.get("data"))
        if summary:
            focused["summary"] = summary[:summary_chars]
        if component_context:
            focused["context"] = component_context
        payload["focused"] = focused
    others = [b for b in blocks if focused_block is None or b.get("id") != focused_block.get("id")]
    payload["also_yours"] = [
        {
            "tile_id": str(b.get("id")),
            "component": _block_component(b).get("component"),
            "title": tile_title(b),
            "params": _block_component(b).get("params") or {},
        }
        for b in others
        if str(b.get("agent_id")) == str(agent_id)
    ]
    payload["other_tiles"] = [
        {"title": tile_title(b), "agent": agent_name(b.get("agent_id"))}
        for b in others
        if str(b.get("agent_id")) != str(agent_id)
    ]

    def dump() -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    text = dump()
    if len(text) > max_chars:
        payload["other_tiles"] = []
        text = dump()
    if len(text) > max_chars:
        payload["also_yours"] = []
        text = dump()
    while len(text) > max_chars and focused and focused.get("summary"):
        over = len(text) - max_chars
        focused["summary"] = focused["summary"][: max(0, len(focused["summary"]) - over - 16)]
        text = dump()
    if len(text) > max_chars and focused and "context" in focused:
        del focused["context"]
        text = dump()
    return text


def _data_summary(data) -> str | None:
    """A tile's cached value as text for ``focused.summary`` (compact JSON)."""
    if data is None:
        return None
    if isinstance(data, str):
        return data
    try:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return None


def prepare_dashboard_chat(
    client, agent_id: str, board_ref: str, focus_ref: str | None, context_file: str | None
) -> tuple[dict, str]:
    """``(board, dashboard_context)`` for ``cinna chat --board``.

    ``--context @file.json`` (or inline JSON) is sent as given, after the
    same checks core makes (a JSON object, at most 12,000 characters).
    """
    capabilities = client.get_dashboards_capabilities()
    if capabilities is None or capabilities.get("version") != SUPPORTED_VERSION or not capabilities.get("chat"):
        raise CinnaExit(
            EXIT_ERROR,
            "dashboard_chat_unsupported",
            "This Cinna server has no dashboard chat (it predates it). Ask its admin to update cinna-core.",
        )
    session = _Session(Path("."), client.config, client, capabilities)
    board = resolve_board(session, board_ref)
    if board.get("origin") == "desktop":
        raise CinnaExit(
            EXIT_ERROR,
            "desktop_dashboard",
            "A Cinna Desktop dashboard's chat runs in Cinna Desktop (over A2A); "
            "`cinna chat --board` takes one of your web dashboards.",
        )
    max_chars = session.limit("context_chars")
    if context_file:
        raw = context_file
        if raw.startswith("@"):
            path = Path(raw[1:]).expanduser()
            if not path.is_file():
                raise click.UsageError(f"--context: no such file: {path}")
            raw = path.read_text()
        try:
            loaded = json.loads(raw)
        except ValueError as exc:
            raise click.UsageError(f"--context is not JSON: {exc}") from None
        if not isinstance(loaded, dict):
            raise click.UsageError("--context must be a JSON object.")
        text = json.dumps(loaded, ensure_ascii=False, separators=(",", ":"))
        if len(text) > max_chars:
            raise click.UsageError(
                f"--context is {len(text)} characters; core takes at most {max_chars}."
            )
        return board, text

    focused_block = focused_data = component_context = None
    if focus_ref:
        focused_block = resolve_tile(board, focus_ref)
        if str(focused_block.get("agent_id")) != str(agent_id):
            raise click.UsageError(
                f"Tile '{tile_title(focused_block)}' shows another agent's component; "
                "focus one of this agent's tiles."
            )
        with _refusals(session, None):
            focused_data = client.get_tile_data(board["id"], focused_block["id"])
            config = _block_component(focused_block)
            try:
                component = client.get_agent_component(
                    agent_id, config.get("component", ""), config.get("skill")
                )
                spec = component.get("component_json") or {}
                component_context = spec.get("context") if isinstance(spec, dict) else None
            except ComponentRefusal:
                component_context = None
    return board, build_dashboard_context(
        board,
        agent_id,
        focused_block,
        focused_data,
        component_context,
        session.agent_name,
        max_chars=max_chars,
        summary_chars=session.limit("summary_chars"),
    )


def tile_params_snapshot(client, board_id: str, board: dict | None = None) -> dict[str, dict]:
    """``{block id: stored params}`` for every component tile on a dashboard.

    ``board`` (already fetched, with its blocks) saves the round trip.
    """
    if board is None:
        board = client.get_dashboard(board_id)
    return {
        str(b.get("id")): dict(_block_component(b).get("params") or {})
        for b in component_blocks(board)
    }


# ─── `check --run`: the env's checker through cinna exec ─────────────────────


def env_component_path(name: str, skill: str | None) -> str:
    if skill:
        return f"{ENV_WORKSPACE}/skills/{skill}/dashboards/{name}"
    return f"{ENV_WORKSPACE}/dashboards/{name}"


def checker_command(name: str, skill: str | None) -> list[str]:
    """The argv ``check --run`` executes in the environment."""
    return [
        "python3",
        ENV_CHECKER,
        env_component_path(name, skill),
        "--agent",
        ENV_WORKSPACE,
        "--run",
        "--json",
    ]


def _parse_checker_output(output: str) -> dict | None:
    start, end = output.find("{"), output.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(output[start : end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) and "problems" in parsed else None


def run_env_checker(child_cfg, name: str, skill: str | None, timeout: int) -> list[dict]:
    """The env checker's findings for one component (``--run``), via ``cinna exec``."""
    import shlex

    from cinna.client import PlatformClient

    command = " ".join(shlex.quote(part) for part in checker_command(name, skill))
    chunks: list[str] = []
    errors: list[str] = []
    exit_code = None
    with PlatformClient(child_cfg) as client:
        for event in client.stream_exec(child_cfg.agent_id, command, timeout=timeout):
            kind = event.get("type")
            if kind == "tool_result_delta":
                # stdout only: stderr (warnings, a script's traceback) is not the JSON
                if (event.get("metadata") or {}).get("stream", "stdout") == "stdout":
                    chunks.append(str(event.get("content") or ""))
                else:
                    errors.append(str(event.get("content") or ""))
            elif kind == "done":
                exit_code = event.get("exit_code")
            elif kind == "error":
                raise click.ClickException(f"cinna exec failed: {event.get('content')}")
            elif kind == "interrupted":
                raise click.ClickException("The checker run was interrupted.")
    output = "".join(chunks)
    parsed = _parse_checker_output(output)
    if parsed is None:
        stderr = "".join(errors)
        if ENV_CHECKER in stderr and ("can't open file" in stderr or "No such file" in stderr):
            raise CinnaExit(
                EXIT_ERROR,
                "checker_missing",
                "The environment has no dashboards checker: it predates dashboard "
                f"components. Rebuild it: cinna agent rebuild-env {child_cfg.agent_name}",
            )
        tail = (output.strip() + "\n" + stderr.strip()).strip()[-800:]
        raise click.ClickException(f"The checker gave no findings (exit {exit_code}):\n{tail}")
    return [p for p in parsed.get("problems") or [] if isinstance(p, dict)]


# ─── commands ────────────────────────────────────────────────────────────────


_NO_FLUSH = click.option(
    "--no-flush",
    is_flag=True,
    help="Don't push pending local edits to the environment first.",
)
_TIMEOUT = click.option(
    "--timeout",
    type=int,
    default=DEFAULT_WAKE_TIMEOUT,
    show_default=True,
    help="Seconds to keep retrying while the environment wakes.",
)
_SKILL = click.option("--skill", default=None, help="The skill that ships the component.")


@click.group()
def dashboards():
    """Build, check, run and place an agent's dashboard components.

    AGENT is a name, slug, id, or '.' for the agent workspace you are in.
    Components live in the agent's dashboards/<name>/ (or a skill's
    dashboards/<name>/); a tile is one on a dashboard. Offer a component by
    printing its reference (`cinna dashboards ref`); place it only when asked.
    """


@dashboards.command("list")
@click.argument("agent_ref", metavar="AGENT")
@_NO_FLUSH
def list_cmd(agent_ref, no_flush):
    """The agent's components: findings, where each is placed, and its reference."""
    from rich.table import Table

    with _session() as session:
        agent = session.agent(agent_ref)
        flush_local_changes(session.account_root, agent, not no_flush)
        with _refusals(session, agent_ref):
            with console.spinner("Reading components…"):
                result = call_waking(session.client.list_agent_components, agent["id"], fresh=True)
            bundle_id = session.bundle_id(agent)
    components = [_with_reference(c, bundle_id) for c in result.get("components") or [] if isinstance(c, dict)]
    if console.json_mode:
        console.emit_result(
            agent={"id": agent["id"], "name": agent.get("name"), "bundle_id": bundle_id},
            components=components,
            count=len(components),
        )
        return
    if not components:
        console.status(
            f"{escape(str(agent.get('name')))} has no dashboard components yet — "
            "build one in workspace/dashboards/<name>/ (read the cinna-dashboards skill first)."
        )
        return
    table = Table(title=f"Dashboard components of {escape(str(agent.get('name')))}", title_style="bold")
    table.add_column("Component", overflow="fold")
    table.add_column("Title", overflow="fold")
    table.add_column("View")
    table.add_column("Checks")
    table.add_column("On", overflow="fold")
    for component in components:
        table.add_row(
            escape(_component_label(component)),
            _text(component.get("title")),
            _text(component.get("view")),
            _findings_count(component),
            _placements_text(component),
        )
    console.console.print(table)
    console.console.print("\n[dim]References (print one on its own line, never in backticks):[/dim]")
    for component in components:
        if component.get("reference"):
            _plain(component["reference"])
    if any(c.get("has_errors") for c in components):
        console.console.print(
            f"\n[dim]A component with errors can't be placed: `cinna dashboards show {escape(agent_ref)} <name>`.[/dim]"
        )


@dashboards.command("show")
@click.argument("agent_ref", metavar="AGENT")
@click.argument("name")
@_SKILL
@_NO_FLUSH
def show(agent_ref, name, skill, no_flush):
    """One component: what its component.json says, its findings and placements."""
    with _session() as session:
        agent = session.agent(agent_ref)
        flush_local_changes(session.account_root, agent, not no_flush)
        with _refusals(session, agent_ref):
            component = call_waking(session.client.get_agent_component, agent["id"], name, skill, fresh=True)
            bundle_id = session.bundle_id(agent)
    component = _with_reference(component, bundle_id)
    if console.json_mode:
        console.emit_result(component=component)
        return
    out = console.console.print
    spec = component.get("component_json") if isinstance(component.get("component_json"), dict) else {}
    out(f"[bold]{_text(component.get('title') or spec.get('title') or name)}[/bold]  "
        f"[dim]{escape(_component_label(component))} · {escape(str(agent.get('name')))}[/dim]")
    out(f"  View:        {_text(component.get('view') or spec.get('view'))}")
    source = spec.get("source")
    if isinstance(source, dict):
        out("  Source:      " + escape(", ".join(f"{k}={v}" for k, v in source.items())))
    endpoints = spec.get("endpoints")
    if isinstance(endpoints, dict) and endpoints:
        out("  Endpoints:")
        for ep_name, ep in endpoints.items():
            script = ep.get("script") if isinstance(ep, dict) else ep
            desc = ep.get("description") if isinstance(ep, dict) else None
            out(f"    {escape(str(ep_name))}  {_text(script)}" + (f"  [dim]{escape(str(desc))}[/dim]" if desc else ""))
    params = spec.get("params")
    if isinstance(params, dict) and params:
        out("  Params:")
        for p_name, p in params.items():
            if isinstance(p, dict):
                extra = f" ({', '.join(map(str, p['values']))})" if isinstance(p.get("values"), list) else ""
                default = f"  default {json.dumps(p['default'], ensure_ascii=False)}" if "default" in p else ""
                out(f"    {escape(str(p_name))}: {_text(p.get('type'))}{escape(extra)}{escape(default)}")
    refresh = spec.get("refresh")
    if isinstance(refresh, dict) and refresh:
        out("  Refresh:     " + escape(", ".join(f"{k}={json.dumps(v)}" for k, v in refresh.items())))
    out(f"  Size:        {_text(spec.get('size'))}")
    creds = spec.get("requires_credentials")
    if isinstance(creds, list) and creds:
        out("  Credentials: " + escape(", ".join(map(str, creds))))
    out(f"  On:          {_placements_text(component)}")
    findings = [f for f in component.get("findings") or [] if isinstance(f, dict)]
    if findings:
        out("  Findings:")
        for finding in findings:
            color = "red" if _is_error(finding) else "yellow"
            out(f"    [{color}]{escape(finding_line(finding))}[/{color}]")
    else:
        out("  Findings:    [green]none[/green]")
    if component.get("reference"):
        out("  Reference:")
        _plain("    " + component["reference"])


@dashboards.command("check")
@click.argument("agent_ref", metavar="AGENT")
@click.argument("name", required=False)
@_SKILL
@click.option(
    "--run",
    "run_it",
    is_flag=True,
    help="Also run each source with its default params in the environment (the "
    "checker's --run, through cinna exec) and check the result's shape.",
)
@_NO_FLUSH
@click.option("--timeout", type=int, default=300, show_default=True, help="Seconds per --run check.")
def check(agent_ref, name, skill, run_it, no_flush, timeout):
    """Check components the way the owner's card will: exit 1 on any error.

    Static findings come from core (the environment's own checker). --run adds
    the checker's run checks, executed in the environment, never locally.
    Lines from --run are marked (run).
    """
    with _session() as session:
        agent = session.agent(agent_ref)
        flush_local_changes(session.account_root, agent, not no_flush)
        with _refusals(session, agent_ref):
            if name:
                components = [call_waking(session.client.get_agent_component, agent["id"], name, skill, fresh=True)]
            else:
                listing = call_waking(session.client.list_agent_components, agent["id"], fresh=True)
                components = [c for c in listing.get("components") or [] if isinstance(c, dict)]
        child = None
        if run_it:
            found = _child_workspace(session.account_root, agent["id"])
            if found is None:
                raise CinnaExit(
                    EXIT_ERROR,
                    "not_synced",
                    "check --run executes the checker through `cinna exec`, which needs the "
                    f"agent synced here: cinna agent sync {agent_ref}",
                )
            child = found[1]

    results = []
    any_error = False
    for component in components:
        findings = [dict(f, source="core") for f in component.get("findings") or [] if isinstance(f, dict)]
        if child is not None:
            label = _component_label(component)
            with console.spinner(f"Running the checker on {label} in the environment…"):
                env_findings = run_env_checker(child, component.get("name", ""), component.get("skill"), timeout)
            seen = {(f.get("path"), f.get("code")) for f in findings}
            for finding in env_findings:
                if (finding.get("path"), finding.get("code")) not in seen:
                    findings.append(dict(finding, source="run"))
        errors = [f for f in findings if _is_error(f)]
        any_error = any_error or bool(errors)
        results.append({
            "name": component.get("name"),
            "skill": component.get("skill"),
            "ok": not errors,
            "findings": findings,
        })

    if not console.json_mode:
        if not results:
            console.status("No dashboard components to check.")
        for result in results:
            label = f"{result['skill']}/{result['name']}" if result.get("skill") else str(result["name"])
            if not result["findings"]:
                console.status(f"{escape(label)}: ok" + (" (ran)" if child else ""))
                continue
            mark = console.error if not result["ok"] else console.warn
            mark(f"{escape(label)}:")
            for finding in result["findings"]:
                source = "run" if finding.get("source") == "run" else None
                color = "red" if _is_error(finding) else "yellow"
                console.console.print(f"    [{color}]{escape(finding_line(finding, source))}[/{color}]")
    if any_error:
        failed = [r["name"] for r in results if not r["ok"]]
        raise CinnaExit(
            EXIT_ERROR,
            "component_errors",
            f"{len(failed)} component(s) have errors: {', '.join(map(str, failed))}. "
            "Fix them before offering or placing a component.",
            extra={"components": results},
        )
    console.emit_result(components=results, ok=True)


@dashboards.command("run")
@click.argument("agent_ref", metavar="AGENT")
@click.argument("name")
@_SKILL
@click.option("--endpoint", default=None, help="Run this endpoint instead of the component's source.")
@click.option("--param", "params", multiple=True, metavar="KEY=VALUE", help="A param (JSON value, else a string). Repeatable.")
@click.option("--params", "params_json", default=None, metavar="@FILE|JSON", help="All params as a JSON object (--param overlays it).")
@_NO_FLUSH
@_TIMEOUT
def run_cmd(agent_ref, name, skill, endpoint, params, params_json, no_flush, timeout):
    """Run a component's source (or an endpoint) exactly as a tile would.

    Same path as a tile: the owner's timezone for current/today, the
    credential gate and the runner's limits. Counts against the CLI's own
    budget of runs a minute.
    """
    values = parse_params(params, params_json)
    body: dict = {"params": values, "tile_key": CLI_TILE_KEY}
    if skill:
        body["skill"] = skill
    if endpoint:
        body["endpoint"] = endpoint
    with _session() as session:
        agent = session.agent(agent_ref)
        flush_local_changes(session.account_root, agent, not no_flush)
        with _refusals(session, agent_ref):
            with console.spinner(f"Running {escape(name)} in the environment…"):
                result = call_waking(
                    session.client.run_agent_component, agent["id"], name, body, timeout=timeout
                )
    if console.json_mode:
        console.emit_result(
            value=result.get("value"),
            params_resolved=result.get("params_resolved") or {},
            notice=result.get("notice"),
            fetched_at=result.get("fetched_at"),
        )
        return
    console.console.print_json(json.dumps(result.get("value"), ensure_ascii=False))
    resolved = result.get("params_resolved") or {}
    if resolved:
        console.console.print(
            "[dim]params: " + escape(", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in resolved.items())) + "[/dim]"
        )
    if result.get("notice"):
        console.warn(escape(str(result["notice"])))
    console.console.print(f"[dim]fetched {escape(str(result.get('fetched_at') or ''))}[/dim]")


@dashboards.command("ref")
@click.argument("agent_ref", metavar="AGENT")
@click.argument("name")
@_SKILL
@click.option("--check", "check_it", is_flag=True, help="Ask core whether the reference resolves (never wakes the environment).")
def ref_cmd(agent_ref, name, skill, check_it):
    """Print the reference line that offers the component.

    The owner's chat (web or Cinna Desktop) turns the line into a card with
    Preview and Add to dashboard. Print it on its own line, never in backticks.
    """
    with _session() as session:
        agent = session.agent(agent_ref)
        bundle_id = session.bundle_id(agent)
        if not bundle_id:
            raise click.ClickException(f"{agent.get('name')} has no bundle id; the platform did not return one.")
        line = reference(bundle_id, name, skill)
        resolved = None
        if check_it:
            with _refusals(session, agent_ref):
                answer = session.client.resolve_component_refs([line])
            results = [r for r in answer.get("results") or [] if isinstance(r, dict)]
            resolved = results[0] if results else {"ref": line, "status": "invalid"}
    if console.json_mode:
        fields = {"reference": line}
        if resolved is not None:
            fields["status"] = resolved.get("status")
            fields["resolved"] = resolved
        if resolved is not None and resolved.get("status") != "ok":
            raise CinnaExit(
                EXIT_ERROR,
                f"ref_{resolved.get('status')}",
                str(resolved.get("message") or resolved.get("status")),
                extra=fields,
            )
        console.emit_result(**fields)
        return
    click.echo(line)
    if resolved is not None:
        if resolved.get("status") == "ok":
            console.status("Resolves: the owner gets a card.")
        else:
            raise CinnaExit(
                EXIT_ERROR,
                f"ref_{resolved.get('status')}",
                f"Does not resolve ({resolved.get('status')}): {resolved.get('message') or ''}".rstrip(": "),
            )


# ─── boards ──────────────────────────────────────────────────────────────────


@dashboards.command("boards")
def boards_cmd():
    """Your web dashboards and how many component tiles each holds."""
    from rich.table import Table

    with _session() as session:
        with console.spinner("Listing dashboards…"):
            boards = [b for b in session.client.list_dashboards("web") or [] if isinstance(b, dict)]
        links = {str(b.get("id")): _board_link(session, b) for b in boards}
    if console.json_mode:
        console.emit_result(
            dashboards=[dict(b, url=links.get(str(b.get("id")))) for b in boards], count=len(boards)
        )
        return
    if not boards:
        console.status("You have no dashboards yet — `cinna dashboards board create <name>` makes one.")
        return
    table = Table(title="Your dashboards", title_style="bold")
    table.add_column("Id", style="dim")
    table.add_column("Name", overflow="fold")
    table.add_column("Blocks", justify="right")
    table.add_column("Components", justify="right")
    for board in boards:
        blocks = board.get("blocks") or []
        table.add_row(
            _short(board.get("id")),
            _text(board.get("name")),
            str(len(blocks)),
            str(len(component_blocks(board))),
        )
    console.console.print(table)


@dashboards.group("board")
def board_group():
    """Create a web dashboard."""


@board_group.command("create")
@click.argument("name")
@click.option("--description", default=None, help="One line about the dashboard.")
def board_create(name, description):
    """Create a web dashboard (core allows 10 per user)."""
    with _session() as session:
        board = session.client.create_dashboard(name, description)
        link = _board_link(session, board)
    if console.json_mode:
        console.emit_result(dashboard=dict(board, url=link))
        return
    console.status(f"Created dashboard {escape(str(board.get('name')))} ({_short(board.get('id'))})")
    if link:
        console.console.print(f"  {escape(link)}")


@dashboards.command("add")
@click.argument("agent_ref", metavar="AGENT")
@click.argument("name")
@_SKILL
@click.option("--board", "board_ref", required=True, help="Dashboard name or id.")
@click.option("--new-board", is_flag=True, help="Create the dashboard when no dashboard has that name.")
@click.option("--title", default=None, help="Tile title (default: the component's).")
@click.option("--size", type=click.Choice(list(SIZE_GRID)), default=None, help="Tile size (default: the component's size, else s).")
@click.option("--param", "params", multiple=True, metavar="KEY=VALUE", help="A stored tile param. Repeatable.")
@click.option("--max-age", default=None, help="Refresh override: data older than this is stale (e.g. 15m).")
@click.option("--every", default=None, help="Refresh override while open: a duration ≥ 1m, or 'manual'.")
@click.option("--after-turn/--no-after-turn", "after_turn", default=None, help="Refresh override: recompute after an agent turn.")
@_NO_FLUSH
@_TIMEOUT
def add(agent_ref, name, skill, board_ref, new_board, title, size, params, max_age, every, after_turn, no_flush, timeout):
    """Place a component on one of your web dashboards as a tile.

    The assistant's default is to print the reference and let the owner add
    it; place a tile only when asked. A component with errors is refused.
    """
    values = parse_params(params)
    refresh = refresh_overrides(max_age, every, after_turn)
    with _session() as session:
        agent = session.agent(agent_ref)
        flush_local_changes(session.account_root, agent, not no_flush)
        with _refusals(session, agent_ref):
            component = call_waking(session.client.get_agent_component, agent["id"], name, skill, fresh=True, timeout=timeout)
        if component.get("has_errors"):
            errors = [f for f in component.get("findings") or [] if isinstance(f, dict) and _is_error(f)]
            raise CinnaExit(
                EXIT_ERROR,
                "component_errors",
                f"{name} has errors, so it can't be placed:\n  "
                + "\n  ".join(finding_line(f) for f in errors)
                + f"\nFix them, then `cinna dashboards check {agent_ref} {name}`.",
            )
        try:
            board = resolve_board(session, board_ref)
        except BoardNotFound:
            if not new_board:
                raise
            board = session.client.create_dashboard(board_ref)
            console.status(f"Created dashboard {escape(board_ref)}")
        refuse_desktop_board(board)
        limit = session.limit("blocks_per_dashboard")
        if len(board.get("blocks") or []) >= limit:
            raise CinnaExit(
                EXIT_ERROR,
                "dashboard_full",
                f"Maximum of {limit} blocks allowed per dashboard; '{board.get('name')}' is full. "
                "Remove a tile or use another dashboard.",
            )
        spec = component.get("component_json") if isinstance(component.get("component_json"), dict) else {}
        chosen = size or (spec.get("size") if spec.get("size") in SIZE_GRID else None) or DEFAULT_SIZE
        w, h = size_grid(chosen)
        config: dict = {"component": name, "params": values, "refresh": refresh}
        if skill or component.get("skill"):
            config["skill"] = skill or component.get("skill")
        body = {
            "agent_id": agent["id"],
            "view_type": "agent_component",
            "title": title,
            "show_border": True,
            "grid_x": 0,
            "grid_y": 0,
            "grid_w": w,
            "grid_h": h,
            "config": config,
        }
        with _refusals(session, agent_ref):
            block = session.client.add_dashboard_block(board["id"], body)
        link = _board_link(session, board)
    if console.json_mode:
        console.emit_result(tile=block, dashboard={"id": board.get("id"), "name": board.get("name"), "url": link})
        return
    console.status(
        f"Added {escape(name)} to {escape(str(board.get('name')))} as tile {_short(block.get('id'))} ({chosen})"
    )
    if link:
        console.console.print(f"  {escape(link)}")


# ─── tiles ───────────────────────────────────────────────────────────────────


@dashboards.command("tiles")
@click.argument("board_ref", metavar="BOARD")
def tiles_cmd(board_ref):
    """The component tiles on a dashboard: params, size and data age (never wakes an environment)."""
    from rich.table import Table

    with _session() as session:
        board = resolve_board(session, board_ref)
        rows = []
        for block in component_blocks(board):
            try:
                data = session.client.get_tile_data(board["id"], block["id"])
            except ComponentRefusal as error:
                data = {"mark": error.sentence, "stale": True}
            config = _block_component(block)
            rows.append({
                "tile_id": block.get("id"),
                "title": tile_title(block),
                "agent_id": block.get("agent_id"),
                "agent": session.agent_name(block.get("agent_id")),
                "component": config.get("component"),
                "skill": config.get("skill"),
                "params": config.get("params") or {},
                "size": grid_size_name(block.get("grid_w"), block.get("grid_h")),
                "fetched_at": data.get("fetched_at"),
                "stale": data.get("stale"),
                "mark": data.get("mark"),
                "missing_credentials": data.get("missing_credentials") or [],
            })
    if console.json_mode:
        console.emit_result(dashboard={"id": board.get("id"), "name": board.get("name"), "origin": board.get("origin", "web")}, tiles=rows, count=len(rows))
        return
    if not rows:
        console.status(f"No component tiles on {escape(str(board.get('name')))}.")
        return
    origin = " (Cinna Desktop — read only)" if board.get("origin") == "desktop" else ""
    table = Table(title=f"{escape(str(board.get('name')))}{origin}", title_style="bold")
    table.add_column("Tile", style="dim")
    table.add_column("Title", overflow="fold")
    table.add_column("Agent")
    table.add_column("Component")
    table.add_column("Params", overflow="fold")
    table.add_column("Size")
    table.add_column("Data")
    for row in rows:
        data_cell = _age(row["fetched_at"]) + (" (stale)" if row["stale"] and row["fetched_at"] else "")
        if row["mark"]:
            data_cell += f"\n[yellow]{escape(str(row['mark']))}[/yellow]"
        if row["missing_credentials"]:
            data_cell += "\n[red]needs " + escape(", ".join(row["missing_credentials"])) + "[/red]"
        label = f"{row['skill']}/{row['component']}" if row["skill"] else str(row["component"])
        table.add_row(
            _short(row["tile_id"]),
            _text(row["title"]),
            _text(row["agent"]),
            escape(label),
            escape(", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in row["params"].items())) or "—",
            row["size"],
            data_cell,
        )
    console.console.print(table)


@dashboards.group("tile")
def tile_group():
    """Read and drive one tile on a dashboard (TILE: id, id prefix, title or component)."""


def _emit_tile(tile_id, data: dict, **extra) -> None:
    """The final ``--json`` line of a tile verb: core's fields, then ours (ours win)."""
    fields = {k: v for k, v in (data or {}).items() if k != "result"}
    fields.update(extra)
    fields["tile_id"] = tile_id
    console.emit_result(**fields)


def _print_tile_data(data: dict) -> None:
    if data.get("skipped"):
        console.warn(f"Skipped: {escape(str(data['skipped']))}")
    if data.get("mark"):
        console.warn(escape(str(data["mark"])))
    if data.get("missing_credentials"):
        console.warn("Needs credentials: " + escape(", ".join(map(str, data["missing_credentials"]))))
    console.console.print_json(json.dumps(data.get("data"), ensure_ascii=False))
    resolved = data.get("params_resolved") or {}
    if resolved:
        console.console.print(
            "[dim]params: " + escape(", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in resolved.items())) + "[/dim]"
        )
    if data.get("notice"):
        console.warn(escape(str(data["notice"])))
    state = "stale" if data.get("stale") else "fresh"
    console.console.print(f"[dim]{state}, fetched {escape(_age(data.get('fetched_at')))}[/dim]")


def _tile(session: _Session, board_ref: str, tile_ref: str, *, write: bool) -> tuple[dict, dict]:
    board = resolve_board(session, board_ref)
    if write:
        refuse_desktop_board(board)
    return board, resolve_tile(board, tile_ref)


@tile_group.command("data")
@click.argument("board_ref", metavar="BOARD")
@click.argument("tile_ref", metavar="TILE")
def tile_data(board_ref, tile_ref):
    """The tile's cached data, as core keeps it (never wakes the environment)."""
    with _session() as session:
        board, block = _tile(session, board_ref, tile_ref, write=False)
        with _refusals(session, session.agent_name(block.get("agent_id"))):
            data = session.client.get_tile_data(board["id"], block["id"])
    if console.json_mode:
        _emit_tile(block.get("id"), data)
        return
    _print_tile_data(data)


@tile_group.command("refresh")
@click.argument("board_ref", metavar="BOARD")
@click.argument("tile_ref", metavar="TILE")
@_TIMEOUT
def tile_refresh(board_ref, tile_ref, timeout):
    """Recompute the tile now (wakes the environment; counts against the tile's budget)."""
    with _session() as session:
        board, block = _tile(session, board_ref, tile_ref, write=True)
        with _refusals(session, session.agent_name(block.get("agent_id"))):
            with console.spinner("Refreshing…"):
                data = call_waking(session.client.refresh_tile, board["id"], block["id"], "manual", timeout=timeout)
    if console.json_mode:
        _emit_tile(block.get("id"), data)
        return
    _print_tile_data(data)


@tile_group.command("params")
@click.argument("board_ref", metavar="BOARD")
@click.argument("tile_ref", metavar="TILE")
@click.option("--set", "sets", multiple=True, metavar="KEY=VALUE", help="Set a param (JSON value, else a string). Repeatable.")
@click.option("--clear", "clears", multiple=True, metavar="KEY", help="Put a param back to its default. Repeatable.")
@click.option("--reset", is_flag=True, help="Put every param back to its default.")
@_TIMEOUT
def tile_params(board_ref, tile_ref, sets, clears, reset, timeout):
    """Show or change the tile's stored params (core replaces the whole set and recomputes)."""
    with _session() as session:
        write = bool(sets or clears or reset)
        board, block = _tile(session, board_ref, tile_ref, write=write)
        current = dict(_block_component(block).get("params") or {})
        if not write:
            if console.json_mode:
                console.emit_result(tile_id=block.get("id"), params=current)
                return
            if not current:
                console.status("No stored params: every param is at its default.")
            for key, value in current.items():
                console.console.print(f"  {escape(key)} = {escape(json.dumps(value, ensure_ascii=False))}")
            return
        wanted = {} if reset else dict(current)
        for key in clears:
            wanted.pop(key, None)
        wanted.update(parse_params(sets))
        with _refusals(session, session.agent_name(block.get("agent_id"))):
            with console.spinner("Saving params and recomputing…"):
                data = call_waking(session.client.set_tile_params, board["id"], block["id"], wanted, timeout=timeout)
    if console.json_mode:
        _emit_tile(block.get("id"), data, params=wanted)
        return
    console.status("Params saved: " + (escape(", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in wanted.items())) or "all defaults"))
    _print_tile_data(data)


@tile_group.command("call")
@click.argument("board_ref", metavar="BOARD")
@click.argument("tile_ref", metavar="TILE")
@click.argument("endpoint")
@click.option("--param", "params", multiple=True, metavar="KEY=VALUE", help="A param on top of the tile's stored ones. Repeatable.")
@_TIMEOUT
def tile_call(board_ref, tile_ref, endpoint, params, timeout):
    """Run one of the tile's endpoints with its stored params overlaid (never cached)."""
    values = parse_params(params)
    with _session() as session:
        board, block = _tile(session, board_ref, tile_ref, write=True)
        with _refusals(session, session.agent_name(block.get("agent_id"))):
            with console.spinner(f"Calling {escape(endpoint)}…"):
                result = call_waking(
                    session.client.call_tile_endpoint, board["id"], block["id"], endpoint, values, timeout=timeout
                )
    if console.json_mode:
        console.emit_result(tile_id=block.get("id"), value=result.get("value"))
        return
    console.console.print_json(json.dumps(result.get("value"), ensure_ascii=False))


@tile_group.command("edit")
@click.argument("board_ref", metavar="BOARD")
@click.argument("tile_ref", metavar="TILE")
@click.option("--title", default=None, help="New tile title ('' goes back to the component's).")
@click.option("--size", type=click.Choice(list(SIZE_GRID)), default=None, help="New tile size.")
@click.option("--max-age", default=None, help="Refresh override (e.g. 15m).")
@click.option("--every", default=None, help="Refresh override while open: ≥ 1m, or 'manual'.")
@click.option("--after-turn/--no-after-turn", "after_turn", default=None, help="Refresh override after an agent turn.")
def tile_edit(board_ref, tile_ref, title, size, max_age, every, after_turn):
    """Change a tile's title, size or refresh overrides."""
    if title is None and size is None and max_age is None and every is None and after_turn is None:
        raise click.UsageError("Nothing to change: pass --title, --size, --max-age, --every or --after-turn.")
    with _session() as session:
        board, block = _tile(session, board_ref, tile_ref, write=True)
        config = dict(_block_component(block))
        config["refresh"] = refresh_overrides(max_age, every, after_turn, base=config.get("refresh") or {})
        body: dict = {"config": config}
        if title is not None:
            body["title"] = title or None
        if size is not None:
            body["grid_w"], body["grid_h"] = size_grid(size)
        with _refusals(session, session.agent_name(block.get("agent_id"))):
            updated = session.client.update_dashboard_block(board["id"], block["id"], body)
    if console.json_mode:
        console.emit_result(tile=updated)
        return
    console.status(f"Updated tile {escape(tile_title(updated if isinstance(updated, dict) and updated else block))}")


@tile_group.command("remove")
@click.argument("board_ref", metavar="BOARD")
@click.argument("tile_ref", metavar="TILE")
@click.option("--yes", is_flag=True, help="Don't ask.")
def tile_remove(board_ref, tile_ref, yes):
    """Remove a tile from a dashboard (the component itself stays)."""
    _require_yes_for_json(yes, "removing a tile")
    with _session() as session:
        board, block = _tile(session, board_ref, tile_ref, write=True)
        if not yes and not console.confirm(
            f"Remove '{tile_title(block)}' from {board.get('name')}?", default=False
        ):
            raise click.Abort()
        session.client.delete_dashboard_block(board["id"], block["id"])
    if console.json_mode:
        console.emit_result(removed=block.get("id"))
        return
    console.status(f"Removed '{escape(tile_title(block))}' from {escape(str(board.get('name')))}")



# ─── `cinna agent import`: what travels, and what will not work in the cloud ─

_COMPONENT_FILE_RE = re.compile(r"^(?:skills/([^/]+)/)?dashboards/([^/]+)/component\.json$")


def _load_kit_checker(kit_dir: Path | None):
    """The kit's ``check_component`` module from a **trusted** kit copy, or None.

    ``kit_dir`` is the account workspace's ``context/local-kit/`` — the kit
    the user's own Cinna server shipped in the context package. Never a
    ``.cinna-kit/`` found beside the folder being imported: that folder may be
    a download, and importing would run its Python before anything is
    confirmed. The rules stay the kit's; nothing is copied into the CLI.
    """
    import importlib.util
    import sys

    if kit_dir is None:
        return None
    path = kit_dir / "skills" / "cinna-dashboards" / "scripts" / "check_component.py"
    if not path.is_file():
        return None
    name = "cinna_kit_check_component"
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    except Exception as exc:  # noqa: BLE001 — a kit we cannot load is no kit
        sys.modules.pop(name, None)
        logger.info("could not load the kit's check_component.py: %s", exc)
        return None


def survey_local_components(
    source: Path, files: list[str], credential_names: set[str], kit_dir: Path | None = None
) -> list[dict]:
    """The components an import carries, each with its cloud warnings.

    Warns (never refuses) when the kit's static checker (from ``kit_dir``,
    see ``_load_kit_checker``) reports errors, when
    the source reads ``app-data/`` (that data does not travel), when the
    folder holds ``package.json``/``node_modules/`` (nothing is installed in
    the environment), when ``refresh.on_app_start`` is set (the cloud ignores
    it), and when ``requires_credentials`` names a credential the manifest
    does not declare (the cloud matches linked credentials by name).
    """
    checker = None
    checker_loaded = False
    found: list[dict] = []
    for rel in files:
        match = _COMPONENT_FILE_RE.match(rel)
        if not match:
            continue
        skill, name = match.group(1), match.group(2)
        folder = source / Path(rel).parent
        warnings: list[str] = []
        try:
            spec = json.loads((source / rel).read_text())
        except (OSError, ValueError):
            spec = None
            warnings.append("component.json is not valid JSON")
        if isinstance(spec, dict):
            src = spec.get("source") if isinstance(spec.get("source"), dict) else {}
            source_file = str(src.get("file") or "")
            if source_file.startswith("app-data/") or "/app-data/" in source_file:
                warnings.append(
                    f"its source reads {source_file}, and app-data/ does not travel: the tile "
                    "stays empty until its refresh.command or a schedule writes it in the cloud"
                )
            refresh = spec.get("refresh") if isinstance(spec.get("refresh"), dict) else {}
            if refresh.get("on_app_start"):
                warnings.append("refresh.on_app_start is ignored in the cloud (tiles refresh when shown)")
            required = spec.get("requires_credentials")
            if isinstance(required, list):
                unknown = [str(n) for n in required if isinstance(n, str) and n not in credential_names]
                if unknown:
                    warnings.append(
                        "requires_credentials names "
                        + ", ".join(unknown)
                        + ", which the manifest does not declare: the cloud matches linked credentials "
                        "by name, so the tile would say it needs credentials"
                    )
        if (folder / "package.json").exists() or (folder / "node_modules").exists():
            warnings.append(
                "the folder has package.json/node_modules: nothing is installed in the environment, "
                "so endpoints must be plain JavaScript with no dependencies"
            )
        if not checker_loaded:
            checker = _load_kit_checker(kit_dir)
            checker_loaded = True
        if checker is not None:
            try:
                problems = checker.check_path(folder, run=False, agent_root=source)
            except Exception as exc:  # noqa: BLE001
                logger.info("kit checker failed on %s: %s", rel, exc)
                problems = []
            for problem in problems:
                if getattr(problem, "severity", "error") == "error":
                    warnings.append(f"checker: {problem.path}: {problem.message} [{problem.code}]")
        found.append({"name": name, "skill": skill, "warnings": warnings})
    return found
