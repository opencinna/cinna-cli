"""Tests for client module."""

import pytest
import respx

from cinna.client import PlatformClient
from cinna.errors import AuthenticationError, PlatformError


@pytest.fixture
def client(sample_config):
    c = PlatformClient(sample_config)
    yield c
    c.close()


def test_context_manager(sample_config):
    with PlatformClient(sample_config) as client:
        assert client.base_url == "https://platform.example.com"


@respx.mock
def test_download_workspace(client):
    respx.get("https://platform.example.com/api/v1/cli/agents/agent-123/workspace").respond(
        200, content=b"workspace-tar"
    )
    result = client.download_workspace("agent-123")
    assert result == b"workspace-tar"


@respx.mock
def test_get_building_context(client):
    respx.get("https://platform.example.com/api/v1/cli/agents/agent-123/building-context").respond(
        200, json={"building_prompt": "You are an agent."}
    )
    result = client.get_building_context("agent-123")
    assert "building_prompt" in result


@respx.mock
def test_search_knowledge(client):
    respx.post("https://platform.example.com/api/v1/cli/agents/agent-123/knowledge/search").respond(
        200, json={"results": [{"content": "Answer", "source": "doc", "similarity": 0.9}]}
    )
    result = client.search_knowledge("agent-123", "how to deploy?")
    assert len(result["results"]) == 1


@respx.mock
def test_get_sync_runtime(client):
    respx.get(
        "https://platform.example.com/api/v1/cli/agents/agent-123/sync-runtime"
    ).respond(
        200,
        json={
            "mutagen_version": "0.18.3",
            "mutagen_agent_sha256": "",
            "platform_api_version": "1.0",
        },
    )
    result = client.get_sync_runtime("agent-123")
    assert result["mutagen_version"] == "0.18.3"


@respx.mock
def test_stream_exec_yields_events(client):
    body = (
        'data: {"type": "exec_id", "exec_id": "abc"}\n\n'
        'data: {"type": "tool_result_delta", "content": "hello\\n", '
        '"metadata": {"stream": "stdout"}}\n\n'
        'data: {"type": "done", "exit_code": 0}\n\n'
    )
    respx.post(
        "https://platform.example.com/api/v1/cli/agents/agent-123/exec"
    ).respond(200, text=body, headers={"content-type": "text/event-stream"})

    events = list(client.stream_exec("agent-123", "echo hello"))
    types = [e["type"] for e in events]
    assert types == ["exec_id", "tool_result_delta", "done"]
    assert events[0]["exec_id"] == "abc"
    assert events[2]["exit_code"] == 0


@respx.mock
def test_401_raises_auth_error(client):
    respx.get("https://platform.example.com/api/v1/cli/agents/agent-123/building-context").respond(
        401, json={"detail": "Token revoked"}
    )
    with pytest.raises(AuthenticationError):
        client.get_building_context("agent-123")


@respx.mock
def test_404_raises_platform_error(client):
    respx.get("https://platform.example.com/api/v1/cli/agents/agent-123/workspace").respond(
        404, json={"detail": "not found"}
    )
    with pytest.raises(PlatformError, match="not found"):
        client.download_workspace("agent-123")


@respx.mock
def test_500_raises_platform_error(client):
    respx.get("https://platform.example.com/api/v1/cli/agents/agent-123/building-context").respond(
        500, json={"detail": "Internal error"}
    )
    with pytest.raises(PlatformError, match="500"):
        client.get_building_context("agent-123")
