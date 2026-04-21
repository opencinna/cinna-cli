"""Tests for client module."""

import pytest
import respx
import httpx

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
    # Should not raise after close
    assert True


@respx.mock
def test_download_build_context(client):
    respx.get("https://platform.example.com/api/v1/cli/agents/agent-123/build-context").respond(
        200, content=b"tarball-bytes"
    )
    result = client.download_build_context("agent-123")
    assert result == b"tarball-bytes"


@respx.mock
def test_download_workspace(client):
    respx.get("https://platform.example.com/api/v1/cli/agents/agent-123/workspace").respond(
        200, content=b"workspace-tar"
    )
    result = client.download_workspace("agent-123")
    assert result == b"workspace-tar"


@respx.mock
def test_upload_workspace(client):
    respx.post("https://platform.example.com/api/v1/cli/agents/agent-123/workspace").respond(200)
    client.upload_workspace("agent-123", b"tarball")


@respx.mock
def test_get_workspace_manifest(client):
    respx.get("https://platform.example.com/api/v1/cli/agents/agent-123/workspace/manifest").respond(
        200, json={"files": {"main.py": {"sha256": "abc"}}}
    )
    result = client.get_workspace_manifest("agent-123")
    assert "files" in result


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
def test_401_raises_auth_error(client):
    respx.get("https://platform.example.com/api/v1/cli/agents/agent-123/building-context").respond(
        401, json={"detail": "Token revoked"}
    )
    with pytest.raises(AuthenticationError):
        client.get_building_context("agent-123")


@respx.mock
def test_404_raises_platform_error(client):
    respx.get("https://platform.example.com/api/v1/cli/agents/agent-123/build-context").respond(
        404, json={"detail": "not found"}
    )
    with pytest.raises(PlatformError, match="not found"):
        client.download_build_context("agent-123")


@respx.mock
def test_500_raises_platform_error(client):
    respx.get("https://platform.example.com/api/v1/cli/agents/agent-123/building-context").respond(
        500, json={"detail": "Internal error"}
    )
    with pytest.raises(PlatformError, match="500"):
        client.get_building_context("agent-123")
