"""Shared credentials, agent API connections and capability requests from the CLI.

`cinna account credentials list` (owned + shared sections), `not_owner` on a
shared row, `agent show` / skills readiness / `agent import` resolving shared
credentials, and the consumer verbs `cinna agent-api connections` /
`connections spec` / `request file|list`. One invariant above the rest: no
token ever reaches the output.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx
from click.testing import CliRunner

from cinna.account import AccountConfig, save_account_config
from cinna.client import AccountClient
from cinna.errors import CodedRefusal
from cinna.main import cli

SECRET = "cak_live_SUPERSECRETVALUE0123456789"
AGENT_ID = "agent-123"
CRED_ID = "f1d8af48-586b-4d3d-a020-be37aad95d1d"
SHARED_ID = "ea90e6e2-2e6e-4a29-8081-8493ec553814"

AGENTS = {
    "count": 1,
    "data": [{"id": AGENT_ID, "name": "Hello Testing Agent", "can_build": True}],
}

OWNED_ROW = {
    "id": "67b0f9bc-a89d-4a08-b781-5983e969513f",
    "name": "API Token - SOMETOKEN",
    "type": "api_token",
    "service_uri": "some-token.com",
    "status": "complete",
    "is_shared": False,
    "owner_email": None,
    "access_level": None,
    "share_source": None,
    "via_groups": [],
    "agent_api_key_status": None,
}
SHARED_ROW = {
    "id": SHARED_ID,
    "name": "BTC Rate API API",
    "type": "agent_api",
    "service_uri": "btc-rate",
    "status": "complete",
    "is_shared": True,
    "owner_email": "admin@example.com",
    "access_level": "read",
    "share_source": "direct",
    "via_groups": [{"id": "g1", "name": "Traders"}],
    "allow_bundle_reuse_effective": False,
    "category": "mine",
    "agent_api_key_status": None,
    "share_count": 0,
    "user_workspace_id": None,
}


def connection(**overrides) -> dict:
    base_url = "http://backend:8000/api/v1/agent-api/70553ae2"
    row = {
        "credential_id": CRED_ID,
        "name": "BTC Rate API API",
        "slot": None,
        "is_shared": False,
        "owner_email": None,
        "producer_agent_id": "70553ae2",
        "producer_agent_name": "BTC Rate API",
        "base_url": base_url,
        "spec_url": base_url + "/openapi.json",
        "improvement_requests_url": base_url + "/_cinna/improvement-requests",
        "key_status": "active",
        "scopes": ["btc.read"],
        "usable": True,
        "unavailable_reason": None,
        "sdk_lookup": CRED_ID,
    }
    row.update(overrides)
    return row


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def account_cfg() -> AccountConfig:
    return AccountConfig(
        platform_url="https://platform.example.com",
        frontend_url="https://ui.example.com",
        account_token="account-token-abc",
        machine_name="laptop",
    )


@pytest.fixture
def account_root(tmp_path: Path, account_cfg) -> Path:
    root = tmp_path / "my-cinna"
    root.mkdir()
    save_account_config(account_cfg, root)
    (root / "agents").mkdir()
    return root


@pytest.fixture
def conn_client(account_root, monkeypatch):
    """The client the connection/request runners use."""
    monkeypatch.chdir(account_root)
    monkeypatch.setenv("COLUMNS", "240")
    with patch("cinna.agent_api_connections.AccountClient") as cls:
        c = cls.return_value.__enter__.return_value
        c.list_account_agents.return_value = AGENTS
        c.list_agent_api_connections.return_value = {"data": [connection()], "count": 1}
        yield c


@pytest.fixture
def acct_client(account_root, monkeypatch):
    """The client `cinna.account` runners use."""
    monkeypatch.chdir(account_root)
    monkeypatch.setenv("COLUMNS", "240")
    with patch("cinna.account.AccountClient") as cls:
        c = cls.return_value.__enter__.return_value
        c.list_account_agents.return_value = AGENTS
        yield c


def out(result) -> str:
    return result.output + (result.stderr or "")


# --- credentials list ---


def test_credentials_list_has_an_owned_and_a_shared_section(runner, acct_client):
    acct_client.list_credentials.return_value = {"data": [OWNED_ROW, SHARED_ROW], "count": 2}
    result = runner.invoke(cli, ["account", "credentials", "list"])
    assert result.exit_code == 0, out(result)
    text = out(result)
    assert "Your credentials (1)" in text
    assert "Shared with you (1)" in text
    assert "admin@example.com" in text
    assert "direct, group Traders" in text
    assert "read" in text
    assert "only the owner can rename" in text
    # The shared section comes after the owned one.
    assert text.index("Your credentials") < text.index("Shared with you")


def test_credentials_list_shared_only(runner, acct_client):
    acct_client.list_credentials.return_value = {"data": [OWNED_ROW, SHARED_ROW], "count": 2}
    result = runner.invoke(cli, ["account", "credentials", "list", "--shared"])
    assert result.exit_code == 0, out(result)
    assert "Shared with you" in out(result)
    assert "SOMETOKEN" not in out(result)


def test_credentials_list_owned_only_says_so_when_none(runner, acct_client):
    acct_client.list_credentials.return_value = {"data": [SHARED_ROW], "count": 1}
    result = runner.invoke(cli, ["account", "credentials", "list", "--owned"])
    assert result.exit_code == 0, out(result)
    assert "You own no credentials" in out(result)
    assert "BTC Rate" not in out(result)


def test_credentials_list_json_passes_rows_through(runner, acct_client):
    listing = {"data": [OWNED_ROW, SHARED_ROW], "count": 2}
    acct_client.list_credentials.return_value = listing
    result = runner.invoke(cli, ["account", "credentials", "list", "--json"])
    assert result.exit_code == 0, out(result)
    assert json.loads(result.output) == listing


def test_credentials_list_json_shared_filters_and_recounts(runner, acct_client):
    acct_client.list_credentials.return_value = {"data": [OWNED_ROW, SHARED_ROW], "count": 2}
    result = runner.invoke(cli, ["account", "credentials", "list", "--shared", "--json"])
    assert result.exit_code == 0, out(result)
    assert json.loads(result.output) == {"data": [SHARED_ROW], "count": 1}


def test_credentials_list_refuses_owned_and_shared_together(runner, acct_client):
    result = runner.invoke(cli, ["account", "credentials", "list", "--owned", "--shared"])
    assert result.exit_code == 2
    acct_client.list_credentials.assert_not_called()


def test_credentials_update_on_a_shared_row_prints_not_owner(runner, acct_client):
    message = (
        "This credential is shared with you by admin@example.com; only the owner can change it."
    )
    acct_client.update_credential.side_effect = CodedRefusal(403, "not_owner", message)
    result = runner.invoke(
        cli, ["account", "credentials", "update", SHARED_ID, "--service-uri", "x"]
    )
    assert result.exit_code == 1
    assert message in out(result)


def test_credentials_delete_on_a_shared_row_prints_not_owner(runner, acct_client):
    acct_client.delete_credential.side_effect = CodedRefusal(403, "not_owner", "shared with you by a@b.c")
    result = runner.invoke(cli, ["account", "credentials", "delete", SHARED_ID, "--yes"])
    assert result.exit_code == 1
    assert "shared with you by a@b.c" in out(result)
    assert "Credential deleted" not in out(result)


@respx.mock
def test_client_update_credential_raises_not_owner_as_coded(account_cfg):
    respx.put(f"https://platform.example.com/api/v1/cli/account/credentials/{SHARED_ID}").respond(
        403,
        json={"detail": {"code": "not_owner", "message": "shared with you by a@b.c"}},
    )
    with AccountClient(account_cfg) as client:
        with pytest.raises(CodedRefusal) as exc:
            client.update_credential(SHARED_ID, {"service_uri": "x"})
    assert exc.value.code == "not_owner"
    assert "shared with you by a@b.c" in exc.value.detail


# --- agent show / readiness resolve shared credentials ---


def test_agent_show_takes_a_shared_credentials_slot_from_the_listing(runner, acct_client):
    acct_client.inspect_agent.return_value = {
        "id": AGENT_ID, "name": "Hello Testing Agent", "features": {}, "prompts": {},
        "credentials": [], "agent_api_status": None,
    }
    # The agent's own listing reports no slot; the account listing (shared row) has it.
    acct_client.list_agent_credentials.return_value = {
        "data": [{"id": SHARED_ID, "name": "BTC Rate API API", "type": "agent_api", "service_uri": None}]
    }
    acct_client.list_credentials.return_value = {"data": [SHARED_ROW], "count": 1}
    acct_client.list_agent_api_connections.return_value = {
        "data": [connection(credential_id=SHARED_ID, slot="btc-rate", sdk_lookup="btc-rate",
                            is_shared=True, owner_email="admin@example.com")],
        "count": 1,
    }
    acct_client.get_dashboards_capabilities.return_value = None

    result = runner.invoke(cli, ["agent", "show", "Hello Testing Agent"])
    assert result.exit_code == 0, out(result)
    text = out(result)
    assert "slot: btc-rate" in text
    assert "slot: unknown" not in text
    assert "shared by admin@example.com" in text
    assert "Agent API connections (1)" in text
    assert "BTC Rate API API → BTC Rate API" in text
    assert "lookup: btc-rate" in text
    assert 'cinna agent-api connections "Hello Testing Agent"' in text


def test_agent_show_survives_an_unreadable_connections_listing(runner, acct_client):
    acct_client.inspect_agent.return_value = {
        "id": AGENT_ID, "name": "Hello Testing Agent", "features": {}, "prompts": {},
        "credentials": [], "agent_api_status": None,
    }
    acct_client.list_agent_credentials.return_value = {"data": []}
    acct_client.list_credentials.return_value = {"data": []}
    acct_client.list_agent_api_connections.side_effect = CodedRefusal(404, "not_found", "no")
    acct_client.get_dashboards_capabilities.return_value = None
    result = runner.invoke(cli, ["agent", "show", "Hello Testing Agent"])
    assert result.exit_code == 0, out(result)
    assert "Agent API connections" not in out(result)


# --- agent import attaches a shared agent_api connection ---


def _import_client(rows):
    client = MagicMock()
    client.list_credentials.return_value = {"data": rows}
    return client


def test_import_attaches_a_shared_agent_api_connection_by_slot(capsys):
    from cinna.local_import import _sync_credentials

    client = _import_client([OWNED_ROW, SHARED_ROW])
    spec = {"name": "btc-rate", "type": "agent_api", "service_uri": "btc-rate"}
    _sync_credentials(client, AGENT_ID, "Consumer", [spec], None)
    client.share_credential_with_agent.assert_called_once_with(SHARED_ID, AGENT_ID)
    client.create_credential.assert_not_called()
    assert "shared by admin@example.com" in capsys.readouterr().out


def test_import_attaches_a_shared_agent_api_connection_by_name():
    from cinna.local_import import _sync_credentials

    client = _import_client([{**SHARED_ROW, "service_uri": None}])
    spec = {"name": "BTC Rate API API", "type": "agent_api", "service_uri": "other"}
    _sync_credentials(client, AGENT_ID, "Consumer", [spec], None)
    client.share_credential_with_agent.assert_called_once_with(SHARED_ID, AGENT_ID)


def test_import_prefers_an_owned_credential_over_a_shared_one():
    from cinna.local_import import _sync_credentials

    mine = {**SHARED_ROW, "id": "mine", "is_shared": False, "owner_email": None}
    client = _import_client([mine, SHARED_ROW])
    spec = {"name": "BTC Rate API API", "type": "agent_api", "service_uri": "btc-rate"}
    _sync_credentials(client, AGENT_ID, "Consumer", [spec], None)
    client.share_credential_with_agent.assert_called_once_with("mine", AGENT_ID)


# --- agent-api connections ---


def test_connections_bare_form_lists(runner, conn_client):
    conn_client.list_agent_api_connections.return_value = {
        "data": [connection(token=SECRET, is_shared=True, owner_email="admin@example.com")],
        "count": 1,
    }
    result = runner.invoke(cli, ["agent-api", "connections", "Hello Testing Agent"])
    assert result.exit_code == 0, out(result)
    text = out(result)
    assert "BTC Rate API" in text
    assert "improvement_requests_url" in text
    assert "/_cinna/improvement-requests" in text
    assert "shared by admin@example.com" in text
    assert "environment" in text
    assert SECRET not in text
    conn_client.list_agent_api_connections.assert_called_once_with(AGENT_ID)


def test_connections_list_json_has_no_token(runner, conn_client):
    conn_client.list_agent_api_connections.return_value = {
        "data": [connection(token=SECRET)], "count": 1,
    }
    result = runner.invoke(
        cli, ["agent-api", "connections", "list", "Hello Testing Agent", "--json"]
    )
    assert result.exit_code == 0, out(result)
    assert SECRET not in result.output
    payload = json.loads(result.output)
    assert payload["data"][0]["sdk_lookup"] == CRED_ID


def test_connections_empty(runner, conn_client):
    conn_client.list_agent_api_connections.return_value = {"data": [], "count": 0}
    result = runner.invoke(cli, ["agent-api", "connections", "Hello Testing Agent"])
    assert result.exit_code == 0, out(result)
    assert "no agent API connections" in out(result)


def test_connections_unusable_row_names_the_reason_and_remedy(runner, conn_client):
    conn_client.list_agent_api_connections.return_value = {
        "data": [connection(usable=False, unavailable_reason="key_revoked",
                            key_status="revoked_by_issuer", improvement_requests_url=None)],
        "count": 1,
    }
    result = runner.invoke(cli, ["agent-api", "connections", "Hello Testing Agent"])
    assert result.exit_code == 0, out(result)
    assert "not usable: key_revoked" in out(result)
    assert "Ask the producer's owner" in out(result)


def test_connections_refusal_in_json_mode_carries_the_code(runner, conn_client):
    conn_client.list_agent_api_connections.side_effect = CodedRefusal(404, "not_found", "No agent.")
    result = runner.invoke(cli, ["agent-api", "connections", "Hello Testing Agent", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output.strip().splitlines()[-1])["code"] == "not_found"


@pytest.mark.parametrize("ref", [CRED_ID, "btc-rate", "BTC Rate API API", CRED_ID[:8]])
def test_spec_resolves_the_connection_by_id_slot_name_or_prefix(runner, conn_client, ref):
    conn_client.list_agent_api_connections.return_value = {
        "data": [connection(slot="btc-rate", sdk_lookup="btc-rate")], "count": 1,
    }
    conn_client.get_agent_api_connection_spec.return_value = {"openapi": "3.1.0", "info": {"version": "1.0.0"}}
    result = runner.invoke(cli, ["agent-api", "connections", "spec", "Hello Testing Agent", ref])
    assert result.exit_code == 0, out(result)
    assert json.loads(result.output)["info"]["version"] == "1.0.0"
    conn_client.get_agent_api_connection_spec.assert_called_once_with(AGENT_ID, CRED_ID)


def test_spec_ambiguous_name_refuses_with_ids(runner, conn_client):
    conn_client.list_agent_api_connections.return_value = {
        "data": [connection(), connection(credential_id="other-id", sdk_lookup="other-id")],
        "count": 2,
    }
    result = runner.invoke(
        cli, ["agent-api", "connections", "spec", "Hello Testing Agent", "BTC Rate API API", "--json"]
    )
    assert result.exit_code == 1
    line = json.loads(result.output.strip().splitlines()[-1])
    assert line["code"] == "ambiguous_connection"
    assert "other-id" in line["detail"]
    conn_client.get_agent_api_connection_spec.assert_not_called()


def test_spec_unknown_connection(runner, conn_client):
    result = runner.invoke(cli, ["agent-api", "connections", "spec", "Hello Testing Agent", "nope"])
    assert result.exit_code == 1
    assert "No agent API connection 'nope'" in out(result)


def test_spec_writes_a_file(runner, conn_client, tmp_path):
    conn_client.get_agent_api_connection_spec.return_value = {"openapi": "3.1.0", "info": {"version": "2.1.0"}}
    target = tmp_path / "spec.json"
    result = runner.invoke(
        cli, ["agent-api", "connections", "spec", "Hello Testing Agent", CRED_ID, "-o", str(target)]
    )
    assert result.exit_code == 0, out(result)
    assert json.loads(target.read_text())["info"]["version"] == "2.1.0"
    assert "--spec-version" in out(result)


def test_spec_producer_refusal_keeps_its_code(runner, conn_client):
    conn_client.get_agent_api_connection_spec.side_effect = CodedRefusal(
        503, "env_unavailable", "The producer's environment is starting."
    )
    result = runner.invoke(
        cli, ["agent-api", "connections", "spec", "Hello Testing Agent", CRED_ID, "--json"]
    )
    assert result.exit_code == 12
    assert json.loads(result.output.strip().splitlines()[-1])["code"] == "env_unavailable"


# --- agent-api request file / list ---


def test_request_file_sends_the_flags_as_the_body(runner, conn_client, tmp_path):
    endpoint = tmp_path / "ep.json"
    endpoint.write_text(json.dumps({"method": "GET", "path": "/history"}))
    conn_client.file_capability_request.return_value = {
        "id": "req-1", "title": "Rate [history]", "status": "new",
        "spec_version_seen": "1.0.0",
    }
    result = runner.invoke(cli, [
        "agent-api", "request", "file", "Hello Testing Agent", CRED_ID,
        "--title", "Rate [history]", "--need", "Need past rates",
        "--proposed-endpoint-json", str(endpoint),
        "--example-request", "GET /history?date=2026-01-01",
        "--spec-version", "1.0.0",
    ])
    assert result.exit_code == 0, out(result)
    agent_id, cred_id, body = conn_client.file_capability_request.call_args.args
    assert (agent_id, cred_id) == (AGENT_ID, CRED_ID)
    assert body == {
        "title": "Rate [history]",
        "need": "Need past rates",
        "proposed_endpoint": {"method": "GET", "path": "/history"},
        "example_request": "GET /history?date=2026-01-01",
        "spec_version_seen": "1.0.0",
    }
    # Caller text prints verbatim, brackets included.
    assert "Rate [history]" in out(result)
    assert "cinna improve list --kind api" in out(result)


def test_request_file_body_json_from_stdin(runner, conn_client):
    conn_client.file_capability_request.return_value = {"id": "req-1", "title": "T", "status": "new"}
    body = {"title": "T", "need": "N"}
    result = runner.invoke(
        cli,
        ["agent-api", "request", "file", "Hello Testing Agent", CRED_ID, "--body-json", "-", "--json"],
        input=json.dumps(body),
    )
    assert result.exit_code == 0, out(result)
    assert conn_client.file_capability_request.call_args.args[2] == body
    assert json.loads(result.output)["id"] == "req-1"


def test_request_file_body_json_excludes_the_field_flags(runner, conn_client):
    result = runner.invoke(
        cli,
        ["agent-api", "request", "file", "Hello Testing Agent", CRED_ID,
         "--body-json", "-", "--title", "x"],
        input="{}",
    )
    assert result.exit_code == 2
    conn_client.file_capability_request.assert_not_called()


def test_request_file_needs_title_and_need(runner, conn_client):
    result = runner.invoke(
        cli, ["agent-api", "request", "file", "Hello Testing Agent", CRED_ID, "--title", "x"]
    )
    assert result.exit_code == 2
    assert "--need" in out(result)


def test_request_list_prints_owner_notes_verbatim(runner, conn_client):
    conn_client.list_capability_requests.return_value = {
        "data": [{
            "id": "11112222-3333", "title": "[bold]Rate history[/bold]", "status": "completed",
            "resolution_note": "Added GET /history [v1.1.0]", "created_at": "2026-11-17T10:00:00Z",
            "status_changed_at": None, "spec_version_seen": "1.0.0",
        }],
        "count": 1,
    }
    result = runner.invoke(cli, ["agent-api", "request", "list", "Hello Testing Agent", CRED_ID])
    assert result.exit_code == 0, out(result)
    text = out(result)
    assert "[bold]Rate history[/bold]" in text
    assert "[v1.1.0]" in text
    assert "re-read the spec" in text


def test_request_list_empty(runner, conn_client):
    conn_client.list_capability_requests.return_value = {"data": [], "count": 0}
    result = runner.invoke(cli, ["agent-api", "request", "list", "Hello Testing Agent", CRED_ID])
    assert result.exit_code == 0, out(result)
    assert "no capability requests" in out(result)


# --- client: capability-request refusals ---

BASE = f"https://platform.example.com/api/v1/cli/account/agents/{AGENT_ID}/agent-api-connections/{CRED_ID}"


@respx.mock
def test_client_422_keeps_errors_and_expected(account_cfg):
    respx.post(BASE + "/capability-requests").respond(422, json={"detail": {
        "code": "invalid_capability_request",
        "message": "Invalid capability request body.",
        "errors": [{"type": "extra_forbidden", "loc": ["body", "colour"], "msg": "Extra inputs are not permitted"}],
        "expected": {"title": "string (1-200)", "need": "string"},
    }})
    with AccountClient(account_cfg) as client:
        with pytest.raises(CodedRefusal) as exc:
            client.file_capability_request(AGENT_ID, CRED_ID, {"colour": "red"})
    err = exc.value
    assert err.code == "invalid_capability_request"
    assert err.extra["expected"] == {"title": "string (1-200)", "need": "string"}
    assert err.extra["errors"][0]["msg"] == "Extra inputs are not permitted"
    assert "colour: Extra inputs are not permitted" in err.detail
    assert "title: string (1-200)" in err.detail
    assert err.as_json()["expected"]["need"] == "string"


@respx.mock
def test_client_429_carries_retry_after(account_cfg):
    respx.post(BASE + "/capability-requests").respond(
        429, json={"detail": {"code": "rate_limited", "message": "Too many."}},
        headers={"Retry-After": "42"},
    )
    with AccountClient(account_cfg) as client:
        with pytest.raises(CodedRefusal) as exc:
            client.file_capability_request(AGENT_ID, CRED_ID, {"title": "t", "need": "n"})
    assert exc.value.code == "rate_limited"
    assert exc.value.extra["retry_after_s"] == 42
    assert "Retry after 42s" in exc.value.detail


@respx.mock
def test_client_403_plain_detail_for_a_non_developer(account_cfg):
    respx.post(BASE + "/capability-requests").respond(403, json={"detail": "Developers only."})
    with AccountClient(account_cfg) as client:
        with pytest.raises(Exception) as exc:
            client.file_capability_request(AGENT_ID, CRED_ID, {"title": "t", "need": "n"})
    assert "Developers only." in str(exc.value)


@respx.mock
def test_client_connections_listing_drops_a_stray_token(account_cfg):
    respx.get(
        f"https://platform.example.com/api/v1/cli/account/agents/{AGENT_ID}/agent-api-connections"
    ).respond(200, json={"data": [connection(token=SECRET)], "count": 1})
    with AccountClient(account_cfg) as client:
        payload = client.list_agent_api_connections(AGENT_ID)
    assert "token" not in payload["data"][0]


def test_request_file_422_prints_expected_in_human_mode(runner, conn_client):
    refusal = CodedRefusal(422, "invalid_capability_request", "Invalid.")
    refusal.detail = refusal.message = "Invalid.\nExpected fields:\n  title: string"
    conn_client.file_capability_request.side_effect = refusal
    result = runner.invoke(cli, [
        "agent-api", "request", "file", "Hello Testing Agent", CRED_ID,
        "--title", "t", "--need", "n",
    ])
    assert result.exit_code == 1
    assert "Expected fields:" in out(result)
    assert "title: string" in out(result)


# --- follow-up: duplicated slots, typed import match, share-with-agent line, table ---


def test_resolve_a_slot_two_connections_carry_is_ambiguous():
    from cinna.agent_api_connections import resolve_connection
    from cinna.errors import CinnaExit

    rows = [
        connection(credential_id="id-a", slot="btc-rate", sdk_lookup="id-a"),
        connection(credential_id="id-b", slot="btc-rate", sdk_lookup="id-b", name="Other"),
    ]
    with pytest.raises(CinnaExit) as exc:
        resolve_connection(rows, "btc-rate", "Consumer")
    assert exc.value.code == "ambiguous_connection"
    assert "id-a" in exc.value.detail and "id-b" in exc.value.detail
    # The id (the duplicated slot's sdk_lookup) still resolves.
    assert resolve_connection(rows, "id-b", "Consumer")["name"] == "Other"


def test_import_does_not_attach_a_same_name_credential_of_another_type():
    from cinna.local_import import _sync_credentials

    client = _import_client([{**OWNED_ROW, "name": "billing"}])  # api_token
    client.create_credential.return_value = {"credential": {"id": "new"}, "setup_url": None}
    spec = {"name": "billing", "type": "email_imap"}
    _sync_credentials(client, AGENT_ID, "Consumer", [spec], None)
    client.create_credential.assert_called_once()
    client.share_credential_with_agent.assert_called_once_with("new", AGENT_ID)


def test_import_skips_a_wrong_typed_owned_row_for_a_matching_shared_one():
    from cinna.local_import import _sync_credentials

    mine = {**OWNED_ROW, "id": "mine", "name": "BTC Rate API API"}  # api_token
    client = _import_client([mine, {**SHARED_ROW, "service_uri": None}])
    spec = {"name": "BTC Rate API API", "type": "agent_api"}
    _sync_credentials(client, AGENT_ID, "Consumer", [spec], None)
    client.share_credential_with_agent.assert_called_once_with(SHARED_ID, AGENT_ID)


@pytest.mark.parametrize(
    "row, expect, absent",
    [
        ({**OWNED_ROW, "status": "incomplete"}, "Once the user fills", "owner"),
        (OWNED_ROW, None, "Once the user fills"),
        (SHARED_ROW, "admin@example.com's credential", "Once the user fills"),
    ],
)
def test_share_with_agent_closing_line_follows_the_credential(runner, acct_client, row, expect, absent):
    acct_client.list_credentials.return_value = {"data": [row]}
    result = runner.invoke(
        cli, ["account", "credentials", "share-with-agent", row["id"], "--agent", "Hello Testing Agent"]
    )
    assert result.exit_code == 0, out(result)
    if expect:
        assert expect in out(result)
    assert absent not in out(result)


def test_connections_table_never_wraps_the_sdk_lookup(runner, conn_client, monkeypatch):
    monkeypatch.setenv("COLUMNS", "100")
    result = runner.invoke(cli, ["agent-api", "connections", "Hello Testing Agent"])
    assert result.exit_code == 0, out(result)
    table_rows = [line for line in out(result).splitlines() if "BTC Rate" in line and "│" in line]
    assert any(CRED_ID in line for line in table_rows)
    assert "Credential ID" not in out(result)  # lookup == id: one column


def test_connections_table_shows_the_id_when_a_lookup_is_a_slot(runner, conn_client):
    conn_client.list_agent_api_connections.return_value = {
        "data": [connection(slot="btc-rate", sdk_lookup="btc-rate")], "count": 1,
    }
    result = runner.invoke(cli, ["agent-api", "connections", "Hello Testing Agent"])
    assert result.exit_code == 0, out(result)
    assert "Credential ID" in out(result)
    assert CRED_ID in out(result)
