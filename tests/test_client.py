"""Tests for client module."""

import json
import pytest
import respx

from cinna.account import AccountConfig
from cinna.client import AccountClient, PlatformClient
from cinna.errors import AuthenticationError, CodedRefusal, PlatformError


@pytest.fixture
def client(sample_config):
    c = PlatformClient(sample_config)
    yield c
    c.close()


@pytest.fixture
def account_client():
    cfg = AccountConfig(
        platform_url="https://platform.example.com",
        frontend_url="https://platform.example.com",
        account_token="account-token-abc",
        machine_name="laptop",
    )
    c = AccountClient(cfg)
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
def test_account_search_knowledge(account_client):
    route = respx.post(
        "https://platform.example.com/api/v1/cli/account/knowledge/search"
    ).respond(
        200, json={"results": [{"content": "Answer", "source": "doc", "similarity": 0.9}]}
    )
    result = account_client.search_knowledge("how do bundles work?", topic="bundles")
    assert len(result["results"]) == 1
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"query": "how do bundles work?", "topic": "bundles"}


@respx.mock
def test_account_search_knowledge_omits_empty_topic(account_client):
    route = respx.post(
        "https://platform.example.com/api/v1/cli/account/knowledge/search"
    ).respond(200, json={"results": []})
    result = account_client.search_knowledge("anything")
    assert result["results"] == []
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"query": "anything"}  # no topic key when omitted


@respx.mock
def test_account_search_knowledge_401_raises(account_client):
    respx.post(
        "https://platform.example.com/api/v1/cli/account/knowledge/search"
    ).respond(401, json={"detail": "Account token rejected"})
    with pytest.raises(AuthenticationError):
        account_client.search_knowledge("x")


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


# --- context package version (staleness signal) ------------------------------


@respx.mock
def test_account_client_context_package_version():
    import httpx

    cfg = AccountConfig(
        platform_url="https://platform.example.com",
        frontend_url="https://ui.example.com",
        account_token="tok",
        machine_name="laptop",
    )
    respx.get(
        "https://platform.example.com/api/v1/cli/account/context-package/version"
    ).mock(return_value=httpx.Response(200, json={"version": "abc123"}))

    with AccountClient(cfg) as client:
        assert client.get_context_package_version() == "abc123"


@respx.mock
def test_account_client_context_package_version_absent_on_old_backend():
    """A 404 means the route does not exist yet — not an error to surface."""
    import httpx

    cfg = AccountConfig(
        platform_url="https://platform.example.com",
        frontend_url="https://ui.example.com",
        account_token="tok",
        machine_name="laptop",
    )
    respx.get(
        "https://platform.example.com/api/v1/cli/account/context-package/version"
    ).mock(return_value=httpx.Response(404, json={"detail": "Not Found"}))

    with AccountClient(cfg) as client:
        assert client.get_context_package_version() is None


# --- Agent addons + skills catalog (ride the account api-proxy) ---


@respx.mock
def test_get_agent_addons_targets_the_platform_route(account_client):
    route = respx.post(
        "https://platform.example.com/api/v1/cli/account/api-proxy"
    ).respond(
        200,
        json={"agent_id": "agent-123", "addons": [], "counts": {}},
        headers={"X-Cinna-Proxied": "1"},
    )

    out = account_client.get_agent_addons("agent-123")
    assert out["agent_id"] == "agent-123"
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"method": "GET", "path": "agents/agent-123/addons"}


@respx.mock
def test_get_skill_publish_preview_targets_the_preview_route(account_client):
    """The read behind `--dry-run` and the pre-publish confirmation."""
    route = respx.post(
        "https://platform.example.com/api/v1/cli/account/api-proxy"
    ).respond(
        200,
        json={
            "version": "1.0.2",
            "header_version": "1.0.1",
            "latest_published_version": "1.0.1",
            "package_id": "com.example.skill.pdf-report",
            "package_id_disambiguated": False,
            "is_republish": True,
            "next_revision_number": 3,
        },
        headers={"X-Cinna-Proxied": "1"},
    )

    out = account_client.get_skill_publish_preview("agent-123", "pdf-report")
    assert out["version"] == "1.0.2"
    sent = json.loads(route.calls.last.request.content)
    assert sent == {
        "method": "GET",
        "path": "agents/agent-123/skills/pdf-report/publish-preview",
    }


@respx.mock
def test_publish_agent_skill_omits_unset_fields(account_client):
    """Only what the caller supplied reaches the wire.

    For the nullable fields the server treats null and omission the same, so
    this pins presentation; for ``grant_emails`` it pins a real constraint (see
    the null-grant test)."""
    route = respx.post(
        "https://platform.example.com/api/v1/cli/account/api-proxy"
    ).respond(
        200,
        json={"package_id": "pkg-uuid", "revision_number": 2},
        headers={"X-Cinna-Proxied": "1"},
    )

    account_client.publish_agent_skill("agent-123", "pdf-report", version="1.2.0")
    sent = json.loads(route.calls.last.request.content)
    assert sent["path"] == "agents/agent-123/skills/pdf-report/publish"
    assert sent["json_body"] == {"version": "1.2.0"}


@respx.mock
def test_publish_agent_skill_sends_every_field_under_its_wire_name(account_client):
    """Pins the whole request body, not just the fields one test happened to set.

    A rename on either side — `grant_emails` → `grants`, `release_notes` →
    `notes` — is a silent no-op at runtime (the server ignores what it does not
    know), so only an exact-body assertion catches it."""
    route = respx.post(
        "https://platform.example.com/api/v1/cli/account/api-proxy"
    ).respond(
        200,
        json={"package_id": "pkg-uuid", "revision_number": 4},
        headers={"X-Cinna-Proxied": "1"},
    )

    account_client.publish_agent_skill(
        "agent-123",
        "pdf-report",
        version="2.0.0",
        release_notes="Adds the rollup",
        visibility="users",
        grant_emails=["alice@example.com", "bob@example.com"],
        package_id="com.acme.pdf-report",
    )
    sent = json.loads(route.calls.last.request.content)
    assert sent["method"] == "POST"
    assert sent["path"] == "agents/agent-123/skills/pdf-report/publish"
    assert sent["json_body"] == {
        "version": "2.0.0",
        "release_notes": "Adds the rollup",
        "visibility": "users",
        "grant_emails": ["alice@example.com", "bob@example.com"],
        "package_id": "com.acme.pdf-report",
    }


@respx.mock
def test_publish_agent_skill_never_sends_a_null_grant_list(account_client):
    """`grant_emails` is `list[str]`, not `list[str] | None` — a null is a 422."""
    route = respx.post(
        "https://platform.example.com/api/v1/cli/account/api-proxy"
    ).respond(
        200,
        json={"package_id": "pkg-uuid", "revision_number": 1},
        headers={"X-Cinna-Proxied": "1"},
    )

    account_client.publish_agent_skill("agent-123", "pdf-report", grant_emails=None)
    sent = json.loads(route.calls.last.request.content)
    assert "grant_emails" not in sent["json_body"]


@respx.mock
def test_publish_agent_skill_raises_the_servers_coded_refusal(account_client):
    """A ``{"detail": {"code", "message"}}`` refusal keeps BOTH halves: the
    server's sentence verbatim and its code as the CLI's machine code."""
    respx.post("https://platform.example.com/api/v1/cli/account/api-proxy").respond(
        403,
        json={
            "detail": {
                "code": "not_developer",
                "message": "Only agent developers can publish a skill.",
            }
        },
        headers={"X-Cinna-Proxied": "1"},
    )

    with pytest.raises(CodedRefusal) as exc:
        account_client.publish_agent_skill("agent-123", "pdf-report")
    assert exc.value.code == "not_developer"
    assert exc.value.detail == "Only agent developers can publish a skill."
    assert exc.value.status_code == 403


@respx.mock
def test_publish_agent_skill_lists_offending_paths(account_client):
    respx.post("https://platform.example.com/api/v1/cli/account/api-proxy").respond(
        422,
        json={
            "detail": {
                "code": "skill_contains_secrets",
                "message": "This skill contains what look like secrets.",
                "paths": [".env", "scripts/token.txt"],
            }
        },
        headers={"X-Cinna-Proxied": "1"},
    )

    with pytest.raises(CodedRefusal) as exc:
        account_client.publish_agent_skill("agent-123", "pdf-report")
    assert exc.value.paths == [".env", "scripts/token.txt"]
    assert ".env" in exc.value.detail


@respx.mock
def test_plain_detail_still_raises_platform_error(account_client):
    """A route that answers with a sentence (not a coded dict) must not be
    dressed up as a coded refusal with an invented code."""
    respx.post("https://platform.example.com/api/v1/cli/account/api-proxy").respond(
        404,
        json={"detail": "Agent not found."},
        headers={"X-Cinna-Proxied": "1"},
    )

    with pytest.raises(PlatformError) as exc:
        account_client.get_skill_package("pkg-uuid")
    assert "Agent not found." in str(exc.value)
