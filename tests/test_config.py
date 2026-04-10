"""Tests for config module."""

import json
import pytest
from pathlib import Path

from cinna.config import (
    CinnaConfig,
    KnowledgeSource,
    find_workspace_root,
    load_config,
    save_config,
    config_dir,
    build_dir,
    workspace_dir,
    load_manifest,
    save_manifest,
)
from cinna.errors import ConfigNotFoundError


def test_save_and_load_config(tmp_path, sample_config):
    save_config(sample_config, tmp_path)
    loaded = load_config(tmp_path)

    assert loaded.platform_url == sample_config.platform_url
    assert loaded.cli_token == sample_config.cli_token
    assert loaded.agent_id == sample_config.agent_id
    assert loaded.agent_name == sample_config.agent_name
    assert loaded.environment_id == sample_config.environment_id
    assert loaded.template == sample_config.template
    assert loaded.container_name == sample_config.container_name
    assert len(loaded.knowledge_sources) == 1
    assert loaded.knowledge_sources[0].name == "docs"
    assert loaded.knowledge_sources[0].topics == ["api", "faq"]


def test_load_config_missing(tmp_path):
    with pytest.raises(ConfigNotFoundError):
        load_config(tmp_path)


def test_find_workspace_root(workspace_root):
    # Should find from the root itself
    found = find_workspace_root(workspace_root)
    assert found == workspace_root


def test_find_workspace_root_from_subdir(workspace_root):
    subdir = workspace_root / "workspace" / "scripts"
    subdir.mkdir(parents=True)
    found = find_workspace_root(subdir)
    assert found == workspace_root


def test_find_workspace_root_not_found(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ConfigNotFoundError):
        find_workspace_root(empty)


def test_path_helpers(tmp_path):
    assert config_dir(tmp_path) == tmp_path / ".cinna"
    assert build_dir(tmp_path) == tmp_path / ".cinna" / "build"
    assert workspace_dir(tmp_path) == tmp_path / "workspace"


def test_manifest_load_missing(tmp_path):
    (tmp_path / ".cinna").mkdir()
    assert load_manifest(tmp_path) == {}


def test_manifest_save_and_load(tmp_path):
    (tmp_path / ".cinna").mkdir()
    manifest = {"file.py": {"sha256": "abc123", "size": 100, "mtime": 1.0}}
    save_manifest(manifest, tmp_path)
    loaded = load_manifest(tmp_path)
    assert loaded == manifest


def test_config_without_knowledge_sources(tmp_path):
    config = CinnaConfig(
        platform_url="https://example.com",
        cli_token="tok",
        agent_id="a1",
        agent_name="test",
        environment_id="e1",
        template="basic",
        container_name="c1",
    )
    save_config(config, tmp_path)
    loaded = load_config(tmp_path)
    assert loaded.knowledge_sources == []
