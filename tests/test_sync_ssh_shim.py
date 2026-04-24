"""Tests for sync_ssh_shim — argv parsing and URL building.

The end-to-end WebSocket round trip is better exercised by integration tests
against a mock server; here we cover the pure helpers.
"""

import pytest

from cinna.sync_ssh_shim import _extract_agent_id, _parse_argv, _ws_url


def test_extract_agent_id_with_user():
    assert _extract_agent_id("user@cinna-agent-abc-123") == "abc-123"


def test_extract_agent_id_no_user():
    assert _extract_agent_id("cinna-agent-abc-123") == "abc-123"


def test_extract_agent_id_invalid():
    assert _extract_agent_id("user@example.com") is None


def test_parse_argv_with_double_dash():
    argv = ["cinna-sync-ssh", "user@cinna-agent-xyz", "--", "mutagen-agent", "sync", "--server"]
    host, remote = _parse_argv(argv)
    assert host == "user@cinna-agent-xyz"
    assert remote == ["mutagen-agent", "sync", "--server"]


def test_parse_argv_no_double_dash():
    argv = ["cinna-sync-ssh", "user@cinna-agent-xyz", "mutagen-agent", "sync"]
    host, remote = _parse_argv(argv)
    assert host == "user@cinna-agent-xyz"
    assert remote == ["mutagen-agent", "sync"]


def test_parse_argv_skips_p_flag():
    argv = ["cinna-sync-ssh", "-p", "22", "user@cinna-agent-xyz", "mutagen-agent"]
    host, remote = _parse_argv(argv)
    assert host == "user@cinna-agent-xyz"
    assert remote == ["mutagen-agent"]


def test_parse_argv_missing_host():
    with pytest.raises(SystemExit):
        _parse_argv(["cinna-sync-ssh"])


def test_ws_url_https_to_wss():
    url = _ws_url("https://app.example.com", "abc-123")
    assert url == "wss://app.example.com/api/v1/cli/agents/abc-123/sync-stream"


def test_ws_url_http_to_ws():
    url = _ws_url("http://localhost:8000", "abc-123")
    assert url == "ws://localhost:8000/api/v1/cli/agents/abc-123/sync-stream"


def test_ws_url_preserves_path_prefix():
    """A reverse proxy may route /proxy-prefix → backend."""
    url = _ws_url("https://app.example.com/cinna", "abc-123")
    assert url == "wss://app.example.com/cinna/api/v1/cli/agents/abc-123/sync-stream"


def test_resolve_credentials_registry_wins_over_env(monkeypatch, tmp_path):
    """Registry is authoritative: when both sources exist, the shim must pick
    registry. Env captured by a long-running Mutagen daemon can point at a
    revoked token after `cinna connect` rotates credentials; registry is
    re-read on every invocation and always reflects the latest connect."""
    from cinna.config import upsert_agent_registry
    from cinna.sync_ssh_shim import _resolve_credentials

    # Daemon's env has the old (pre-rotation) token; registry has the new one.
    monkeypatch.setenv("CINNA_AGENT_ID", "agent-a")
    monkeypatch.setenv("CINNA_CLI_TOKEN", "stale-env-tok")
    monkeypatch.setenv("CINNA_PLATFORM_URL", "https://stale")

    upsert_agent_registry("agent-a", "https://fresh", "fresh-registry-tok", tmp_path)

    token, url = _resolve_credentials("agent-a")
    assert token == "fresh-registry-tok"
    assert url == "https://fresh"


def test_resolve_credentials_env_fallback_when_registry_empty(monkeypatch):
    """With no registry entry, env provides a first-run fallback — guarded
    by CINNA_AGENT_ID match so env for agent-a never leaks to agent-b."""
    from cinna.sync_ssh_shim import _resolve_credentials

    monkeypatch.setenv("CINNA_AGENT_ID", "agent-a")
    monkeypatch.setenv("CINNA_CLI_TOKEN", "env-tok")
    monkeypatch.setenv("CINNA_PLATFORM_URL", "https://env.example")

    token, url = _resolve_credentials("agent-a")
    assert token == "env-tok"
    assert url == "https://env.example"


def test_resolve_credentials_env_mismatch_without_registry_exits(monkeypatch):
    """Registry missing AND env agent_id mismatches → no credentials found."""
    from cinna.sync_ssh_shim import _resolve_credentials

    monkeypatch.setenv("CINNA_AGENT_ID", "agent-a")
    monkeypatch.setenv("CINNA_CLI_TOKEN", "stale-tok")
    monkeypatch.setenv("CINNA_PLATFORM_URL", "https://stale")

    with pytest.raises(SystemExit):
        _resolve_credentials("agent-b")


def test_resolve_credentials_registry_only(monkeypatch, tmp_path):
    """With no env at all, registry is still consulted."""
    from cinna.config import upsert_agent_registry
    from cinna.sync_ssh_shim import _resolve_credentials

    monkeypatch.delenv("CINNA_AGENT_ID", raising=False)
    monkeypatch.delenv("CINNA_CLI_TOKEN", raising=False)
    monkeypatch.delenv("CINNA_PLATFORM_URL", raising=False)

    upsert_agent_registry("agent-x", "https://x", "tok-x", tmp_path)

    token, url = _resolve_credentials("agent-x")
    assert token == "tok-x"
    assert url == "https://x"


def test_resolve_credentials_unknown_agent_exits(monkeypatch):
    from cinna.sync_ssh_shim import _resolve_credentials

    monkeypatch.delenv("CINNA_AGENT_ID", raising=False)
    monkeypatch.delenv("CINNA_CLI_TOKEN", raising=False)
    monkeypatch.delenv("CINNA_PLATFORM_URL", raising=False)

    with pytest.raises(SystemExit):
        _resolve_credentials("never-registered")
