"""Shared test fixtures."""

import json
import pytest
from pathlib import Path

from cinna.config import CinnaConfig, KnowledgeSource, save_config


@pytest.fixture
def sample_config() -> CinnaConfig:
    return CinnaConfig(
        platform_url="https://platform.example.com",
        cli_token="test-token-abc123",
        agent_id="agent-123",
        agent_name="test-agent",
        environment_id="env-456",
        template="python-basic",
        container_name="agent-dev-test-agent",
        knowledge_sources=[
            KnowledgeSource(id="ks-1", name="docs", topics=["api", "faq"]),
        ],
    )


@pytest.fixture
def workspace_root(tmp_path: Path, sample_config: CinnaConfig) -> Path:
    """Create a workspace root with .cinna/config.json."""
    save_config(sample_config, tmp_path)
    # Create workspace directory
    ws = tmp_path / "workspace"
    ws.mkdir()
    return tmp_path
