"""Tests for config module."""

from pathlib import Path

import pytest

from cinna.config import (
    CinnaConfig,
    agents_registry_path,
    find_workspace_root,
    load_config,
    lookup_agent_registry,
    remove_agent_registry,
    save_config,
    config_dir,
    build_dir,
    upsert_agent_registry,
    workspace_dir,
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
    assert len(loaded.knowledge_sources) == 1
    assert loaded.knowledge_sources[0].name == "docs"
    assert loaded.knowledge_sources[0].topics == ["api", "faq"]


def test_load_config_missing(tmp_path):
    with pytest.raises(ConfigNotFoundError):
        load_config(tmp_path)


def test_load_config_tolerates_legacy_fields(tmp_path, sample_config):
    """Configs written by the pre-live-sync CLI include `container_name`."""
    import json

    save_config(sample_config, tmp_path)
    cfg_path = tmp_path / ".cinna" / "config.json"
    data = json.loads(cfg_path.read_text())
    data["container_name"] = "legacy-container"
    cfg_path.write_text(json.dumps(data))

    loaded = load_config(tmp_path)
    assert loaded.agent_id == sample_config.agent_id


def test_find_workspace_root(workspace_root):
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


def test_config_without_knowledge_sources(tmp_path):
    config = CinnaConfig(
        platform_url="https://example.com",
        cli_token="tok",
        agent_id="a1",
        agent_name="test",
        environment_id="e1",
        template="basic",
    )
    save_config(config, tmp_path)
    loaded = load_config(tmp_path)
    assert loaded.knowledge_sources == []


def test_config_sync_fields_roundtrip(tmp_path, sample_config):
    sample_config.mutagen_version = "0.18.3"
    sample_config.last_sync_runtime_check_at = "2026-04-23T10:00:00Z"
    sample_config.last_sync_connected_at = "2026-04-23T10:00:01Z"
    save_config(sample_config, tmp_path)

    loaded = load_config(tmp_path)
    assert loaded.mutagen_version == "0.18.3"
    assert loaded.last_sync_runtime_check_at == "2026-04-23T10:00:00Z"
    assert loaded.last_sync_connected_at == "2026-04-23T10:00:01Z"


def test_agent_registry_upsert_and_lookup(tmp_path):
    upsert_agent_registry("agent-a", "http://host-a:8000", "tok-a", tmp_path / "a")
    upsert_agent_registry("agent-b", "https://host-b", "tok-b", tmp_path / "b")

    a = lookup_agent_registry("agent-a")
    b = lookup_agent_registry("agent-b")
    assert a == {
        "platform_url": "http://host-a:8000",
        "cli_token": "tok-a",
        "workspace_path": str(tmp_path / "a"),
    }
    assert b["platform_url"] == "https://host-b"


def test_agent_registry_multiple_agents_coexist():
    """Registry must keep distinct creds per agent — one Mutagen daemon, many syncs."""
    upsert_agent_registry("agent-a", "http://host-a", "tok-a", Path("/tmp/a"))
    upsert_agent_registry("agent-b", "http://host-b", "tok-b", Path("/tmp/b"))
    upsert_agent_registry("agent-c", "http://host-c", "tok-c", Path("/tmp/c"))

    assert lookup_agent_registry("agent-a")["cli_token"] == "tok-a"
    assert lookup_agent_registry("agent-b")["cli_token"] == "tok-b"
    assert lookup_agent_registry("agent-c")["cli_token"] == "tok-c"


def test_agent_registry_upsert_overwrites_same_id():
    upsert_agent_registry("agent-a", "http://old", "old-tok", Path("/tmp/a"))
    upsert_agent_registry("agent-a", "http://new", "new-tok", Path("/tmp/a"))
    assert lookup_agent_registry("agent-a")["cli_token"] == "new-tok"
    assert lookup_agent_registry("agent-a")["platform_url"] == "http://new"


def test_agent_registry_remove():
    upsert_agent_registry("agent-a", "http://h", "t", Path("/tmp/a"))
    upsert_agent_registry("agent-b", "http://h", "t", Path("/tmp/b"))
    remove_agent_registry("agent-a")
    assert lookup_agent_registry("agent-a") is None
    assert lookup_agent_registry("agent-b") is not None


def test_agent_registry_remove_missing_is_noop():
    remove_agent_registry("never-existed")


def test_agent_registry_lookup_missing_returns_none():
    assert lookup_agent_registry("not-there") is None


def test_agent_registry_file_is_locked_down(tmp_path):
    upsert_agent_registry("agent-a", "http://h", "tok", tmp_path / "a")
    mode = agents_registry_path().stat().st_mode & 0o777
    assert mode == 0o600, f"registry holds JWTs; must be 0600, got {oct(mode)}"


def test_agent_registry_survives_corrupt_file():
    # Write junk to the file — next read should treat it as empty, not crash.
    path = agents_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json")
    assert lookup_agent_registry("whatever") is None
    # And upserting should recover.
    upsert_agent_registry("agent-a", "http://h", "tok", Path("/tmp/a"))
    assert lookup_agent_registry("agent-a") is not None
