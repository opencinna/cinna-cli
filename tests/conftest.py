"""Shared test fixtures."""

import pytest
from pathlib import Path

from cinna import config as config_module
from cinna import sync_session as sync_session_module
from cinna.config import CinnaConfig, KnowledgeSource, save_config


@pytest.fixture(autouse=True)
def isolate_global_state(tmp_path: Path, monkeypatch):
    """Redirect the per-user registry + shim dir into a tmp path.

    Without this every test would read/write the real ~/.cinna/ state.
    """
    fake_home = tmp_path / "_cinna_home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config_module, "GLOBAL_STATE_DIR", fake_home)
    monkeypatch.setattr(
        sync_session_module, "MUTAGEN_SSH_DIR", fake_home / "mutagen-ssh"
    )
    yield


@pytest.fixture
def sample_config() -> CinnaConfig:
    return CinnaConfig(
        platform_url="https://platform.example.com",
        cli_token="test-token-abc123",
        agent_id="agent-123",
        agent_name="test-agent",
        environment_id="env-456",
        template="python-basic",
        knowledge_sources=[
            KnowledgeSource(id="ks-1", name="docs", topics=["api", "faq"]),
        ],
    )


@pytest.fixture
def workspace_root(tmp_path: Path, sample_config: CinnaConfig) -> Path:
    """Create a workspace root with .cinna/config.json."""
    save_config(sample_config, tmp_path)
    ws = tmp_path / "workspace"
    ws.mkdir()
    return tmp_path
