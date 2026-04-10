"""Manages .cinna/config.json — the single source of truth for CLI state."""

import json
from pathlib import Path
from dataclasses import dataclass, field, asdict

from cinna.errors import ConfigNotFoundError

CONFIG_DIR = ".cinna"
CONFIG_FILE = "config.json"
MANIFEST_FILE = "last_manifest.json"
BUILD_DIR = "build"


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
    container_name: str
    knowledge_sources: list[KnowledgeSource] = field(default_factory=list)


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


def build_dir(workspace_root: Path) -> Path:
    """Return path to .cinna/build/ directory."""
    return config_dir(workspace_root) / BUILD_DIR


def workspace_dir(workspace_root: Path) -> Path:
    """Return path to workspace/ directory."""
    return workspace_root / "workspace"


def load_manifest(workspace_root: Path) -> dict:
    """Load last_manifest.json, return empty dict if not found."""
    manifest_path = config_dir(workspace_root) / MANIFEST_FILE
    if not manifest_path.is_file():
        return {}
    return json.loads(manifest_path.read_text())


def save_manifest(manifest: dict, workspace_root: Path) -> None:
    """Write last_manifest.json."""
    manifest_path = config_dir(workspace_root) / MANIFEST_FILE
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
