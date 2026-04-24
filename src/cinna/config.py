"""Manages .cinna/config.json — the single source of truth for CLI state."""

import json
import os
import threading
from pathlib import Path
from dataclasses import dataclass, field, asdict

from cinna.errors import ConfigNotFoundError

CONFIG_DIR = ".cinna"
CONFIG_FILE = "config.json"
BUILD_DIR = "build"

# Global per-user state — lives outside any single workspace so that one
# Mutagen daemon can serve multiple agent syncs concurrently. The SSH shim
# reads `agents.json` to resolve the CLI token / platform URL for whichever
# agent Mutagen is asking it to connect to on each invocation.
GLOBAL_STATE_DIR = Path.home() / ".cinna"
AGENTS_REGISTRY_FILE = "agents.json"


@dataclass
class KnowledgeSource:
    id: str
    name: str
    topics: list[str]


@dataclass
class CinnaConfig:
    platform_url: str
    cli_token: str
    agent_id: str
    agent_name: str
    environment_id: str
    template: str
    # User-facing frontend URL (the platform's web UI). Set by the bootstrap
    # exchange response; falls back to ``platform_url`` for backwards compat
    # with configs written before this field existed.
    frontend_url: str | None = None
    knowledge_sources: list[KnowledgeSource] = field(default_factory=list)
    mutagen_version: str | None = None
    last_sync_runtime_check_at: str | None = None
    last_sync_connected_at: str | None = None


def find_workspace_root(start: Path | None = None) -> Path:
    """Walk up from start (or cwd) looking for .cinna/config.json.

    Returns the workspace root directory (parent of .cinna/).
    Raises ConfigNotFoundError if not found.
    """
    current = (start or Path.cwd()).resolve()
    while True:
        if (current / CONFIG_DIR / CONFIG_FILE).is_file():
            return current
        parent = current.parent
        if parent == current:
            raise ConfigNotFoundError()
        current = parent


def load_config(workspace_root: Path | None = None) -> CinnaConfig:
    """Load and validate config from .cinna/config.json."""
    if workspace_root is None:
        workspace_root = find_workspace_root()
    config_path = workspace_root / CONFIG_DIR / CONFIG_FILE
    if not config_path.is_file():
        raise ConfigNotFoundError()
    data = json.loads(config_path.read_text())
    ks_list = [KnowledgeSource(**ks) for ks in data.pop("knowledge_sources", [])]
    # Tolerate legacy fields (e.g. container_name from pre-live-sync configs).
    known_fields = {f for f in CinnaConfig.__dataclass_fields__ if f != "knowledge_sources"}
    data = {k: v for k, v in data.items() if k in known_fields}
    return CinnaConfig(**data, knowledge_sources=ks_list)


def save_config(config: CinnaConfig, workspace_root: Path) -> None:
    """Write config to .cinna/config.json."""
    cfg_dir = workspace_root / CONFIG_DIR
    cfg_dir.mkdir(parents=True, exist_ok=True)
    data = asdict(config)
    (cfg_dir / CONFIG_FILE).write_text(json.dumps(data, indent=2) + "\n")


def config_dir(workspace_root: Path) -> Path:
    """Return path to .cinna/ directory."""
    return workspace_root / CONFIG_DIR


def workspace_dir(workspace_root: Path) -> Path:
    """Return path to workspace/ directory."""
    return workspace_root / "workspace"


def build_dir(workspace_root: Path) -> Path:
    """Return path to .cinna/build/ directory.

    Historically held the Docker build context. In live-sync mode the directory
    is usually absent; the helper is retained so any prompt reference docs that
    do land there continue to be discovered.
    """
    return config_dir(workspace_root) / BUILD_DIR


# ── Global agent registry ────────────────────────────────────────────────
#
# `~/.cinna/agents.json` maps agent_id → {platform_url, cli_token,
# workspace_path}. The SSH shim reads this on every Mutagen SSH invocation
# to resolve per-agent credentials; needed because a single Mutagen daemon
# serves SSH subprocesses for every agent the user has synced, and the
# daemon's own env is captured once at start.

_registry_lock = threading.Lock()


def agents_registry_path() -> Path:
    return GLOBAL_STATE_DIR / AGENTS_REGISTRY_FILE


def _read_registry() -> dict:
    path = agents_registry_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_registry(data: dict) -> None:
    path = agents_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    # Restrict perms: the file holds long-lived CLI JWTs.
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def upsert_agent_registry(
    agent_id: str,
    platform_url: str,
    cli_token: str,
    workspace_path: Path,
    frontend_url: str | None = None,
) -> None:
    """Register or refresh an agent's credentials in the global registry.

    ``frontend_url`` is optional for backwards compatibility with callers
    written before the field existed; ``cinna list`` will fall back to
    ``platform_url`` when it's missing.
    """
    with _registry_lock:
        data = _read_registry()
        entry = {
            "platform_url": platform_url,
            "cli_token": cli_token,
            "workspace_path": str(workspace_path),
        }
        if frontend_url:
            entry["frontend_url"] = frontend_url
        data[agent_id] = entry
        _write_registry(data)


def remove_agent_registry(agent_id: str) -> None:
    """Drop an agent's entry. No-op if it wasn't present."""
    with _registry_lock:
        data = _read_registry()
        if agent_id in data:
            del data[agent_id]
            _write_registry(data)


def lookup_agent_registry(agent_id: str) -> dict | None:
    """Return the registry entry for an agent, or None."""
    return _read_registry().get(agent_id)


def list_agent_registry() -> list[dict]:
    """Return every registered agent as a list of dicts, sorted by agent_id.

    Each entry contains ``agent_id`` plus the registry fields
    (``platform_url``, ``cli_token``, ``workspace_path``).
    """
    registry = _read_registry()
    return [{"agent_id": aid, **entry} for aid, entry in sorted(registry.items())]
