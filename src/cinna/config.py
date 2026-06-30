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
class GitLayout:
    """Where the agent sits inside a (possibly future) git working tree.

    Recorded for *every* new checkout — not only git-versioned ones — so the
    local folder is laid out exactly the way the remote repo expects (Model A:
    ``<clone_path>/<subdir>/workspace/``). Enabling VCS later then needs only a
    ``git init`` + a coordinates update, never a re-download or a file move.

    - ``clone_path`` — absolute path to the git working-tree root (the dir that
      holds, or will hold, ``.git``). Equals the agent dir's parent when
      ``subdir`` is set; equals the agent dir itself for a repo-root agent.
    - ``subdir`` — the agent's path within the repo (the agent dir's name).
      ``None`` ⇒ repo-root agent (``clone_path`` == agent dir).
    - ``vcs_enabled`` — flips True once ``cinna git link`` has run and the
      backend reports the agent is git-versioned. The remaining fields
      (``repo_url`` … ``last_synced_commit``) are populated then.
    """

    clone_path: str
    subdir: str | None = None
    vcs_enabled: bool = False
    repo_url: str | None = None
    ref: str | None = None
    sync_direction: str | None = None
    auth_hint: str | None = None
    last_synced_commit: str | None = None


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
    # Server-side id of the CLI token row. Only known for tokens minted via
    # the account workspace (`cinna agent sync`); used by `cinna agent unsync`
    # to revoke the child token server-side. None for tokens from the
    # per-agent setup exchange (which doesn't return the id).
    cli_token_id: str | None = None
    knowledge_sources: list[KnowledgeSource] = field(default_factory=list)
    mutagen_version: str | None = None
    last_sync_runtime_check_at: str | None = None
    last_sync_connected_at: str | None = None
    # Git working-tree layout for this agent. Present for every checkout made by
    # a CLI new enough to write it; ``None`` for legacy flat workspaces.
    git: GitLayout | None = None


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
    git_raw = data.pop("git", None)
    git = None
    if isinstance(git_raw, dict):
        git = GitLayout(
            **{k: v for k, v in git_raw.items() if k in GitLayout.__dataclass_fields__}
        )
    # Tolerate legacy fields (e.g. container_name from pre-live-sync configs).
    nested = {"knowledge_sources", "git"}
    known_fields = {f for f in CinnaConfig.__dataclass_fields__ if f not in nested}
    data = {k: v for k, v in data.items() if k in known_fields}
    return CinnaConfig(**data, knowledge_sources=ks_list, git=git)


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

# Sentinel for ``upsert_agent_registry(git=...)``: distinguishes "caller didn't
# touch git" (preserve whatever block is already stored) from an explicit
# ``None`` (clear it, e.g. ``cinna git unlink``). Without this, every sync
# operation — which re-upserts credentials but knows nothing about git — would
# silently wipe a linked agent's git block.
_PRESERVE_GIT = object()


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
    git=_PRESERVE_GIT,
) -> None:
    """Register or refresh an agent's credentials in the global registry.

    ``frontend_url`` is optional for backwards compatibility with callers
    written before the field existed; ``cinna list`` will fall back to
    ``platform_url`` when it's missing.

    ``workspace_path`` stays the agent dir (the folder holding ``.cinna/``) so
    ``cinna doctor`` / ``cinna list`` keep resolving configs the same way.

    ``git`` carries the working-tree coordinates (``clone_path``, ``subdir``,
    ``repo_url``, ``ref``) when the agent is git-versioned. Its three modes:

    - **omitted** (default) → preserve any git block already stored. Sync
      operations re-upsert credentials without knowing about git; this stops
      them wiping a linked agent's coordinates.
    - ``dict`` → set/replace the git block (``cinna git link``).
    - ``None`` → explicitly clear it (``cinna git unlink``).
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
        if git is _PRESERVE_GIT:
            prev = data.get(agent_id)
            if isinstance(prev, dict) and prev.get("git"):
                entry["git"] = prev["git"]
        elif git:
            entry["git"] = git
        # git is None → omit the block (explicit clear).
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


# ── Model-A on-disk layout ───────────────────────────────────────────────
#
# Every new checkout is laid out the way the agent's remote git repo expects,
# whether or not VCS is enabled yet (Model A; see the "Git Versioning" section
# of docs/README.md):
#
#     <parent>/<slug>/            ← clone root (git working tree once linked)
#     └── <subdir>/               ← the agent dir == workspace_root
#         ├── .cinna/config.json
#         ├── cinna.agent.json    ← backend-owned manifest (when present)
#         ├── .gitignore
#         └── workspace/          ← Mutagen alpha endpoint
#
# ``subdir`` defaults to the agent slug so the local path matches the backend's
# canonical ``<subdir>`` in the common case; enabling VCS is then a pure config
# update with no file movement.


def compute_agent_layout(
    parent_dir: Path, slug: str, subdir: str | None = None
) -> tuple[Path, Path, str]:
    """Return ``(clone_root, workspace_root, subdir)`` for a nested checkout.

    ``parent_dir`` is where the clone root is created (the cwd for ``cinna
    setup``; ``agents/`` for ``cinna agent sync``). ``subdir`` defaults to
    ``slug`` — pass the backend's real subdir when coordinates are already
    known so the layout matches the remote tree exactly.
    """
    subdir = subdir or slug
    clone_root = parent_dir / slug
    workspace_root = clone_root / subdir
    return clone_root, workspace_root, subdir


def clone_root(config: CinnaConfig, workspace_root: Path) -> Path:
    """Resolve the git working-tree root for an agent.

    Reads ``config.git.clone_path`` when present (the recorded clone root);
    otherwise falls back to ``workspace_root`` itself (legacy flat workspace =
    a repo-root agent).
    """
    if config.git and config.git.clone_path:
        return Path(config.git.clone_path).expanduser()
    return workspace_root


def git_subdir(config: CinnaConfig, workspace_root: Path) -> str | None:
    """The agent's subdir within the repo, or None for a repo-root agent."""
    if config.git is not None:
        return config.git.subdir
    # Legacy flat workspace: infer repo-root when no git layout was recorded.
    return None
