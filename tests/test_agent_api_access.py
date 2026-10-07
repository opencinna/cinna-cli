"""Tests for `cinna agent-api access list|issue|revoke|scopes`.

The one invariant above the rest: no key value ever reaches the output.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cinna.account import AccountConfig, save_account_config
from cinna.errors import CodedRefusal
from cinna.main import cli

SECRET = "ak_live_SUPERSECRETVALUE0123456789"

AGENTS = {
    "count": 1,
    "data": [
        {
            "id": "agent-123",
            "name": "Orders API",
            "can_build": True,
            "is_foreign_install": False,
            "has_active_environment": True,
        }
    ],
}

KEY_ACTIVE = {
    "id": "11111111-aaaa-bbbb-cccc-000000000001",
    "token_prefix": "ak_act1",
    "status": "active",
    "expires_at": None,
    "last_used_at": None,
    "linked_agent_count": 2,
}
KEY_REVOKED = {
    "id": "22222222-aaaa-bbbb-cccc-000000000002",
    "token_prefix": "ak_rev2",
    "status": "revoked_by_issuer",
    "expires_at": None,
    "last_used_at": None,
    "linked_agent_count": 0,
}


def listing(**overrides):
    base = {
        "count": 2,
        "external_access_enabled": True,
        "identity_enabled": True,
        "manage_url": "https://ui.example.com/agent/agent-123#integrations",
        "scope_catalog": [{"name": "read", "description": "Read orders"}],
        "data": [
            {
                "user": {"id": "u1", "email": "ana@example.com"},
                "grant_id": "g1",
                "scopes": ["read"],
                "keys": [dict(KEY_ACTIVE), dict(KEY_REVOKED)],
            }
        ],
    }
    base.update(overrides)
    return base


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def account_root(tmp_path: Path) -> Path:
    root = tmp_path / "my-cinna"
    root.mkdir()
    save_account_config(
        AccountConfig(
            platform_url="https://platform.example.com",
            frontend_url="https://ui.example.com",
            account_token="account-token-abc",
            machine_name="laptop",
        ),
        root,
    )
    (root / "agents").mkdir()
    return root


@pytest.fixture
def client(account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    monkeypatch.setenv("COLUMNS", "240")
    with patch("cinna.account.AccountClient") as cls:
        c = cls.return_value.__enter__.return_value
        c.list_account_agents.return_value = AGENTS
        c.list_agent_api_access.return_value = listing()
        yield c


def out(result) -> str:
    return result.output + (result.stderr or "")


# --- list ---


def test_list_hides_terminal_keys(runner, client):
    result = runner.invoke(cli, ["agent-api", "access", "list", "Orders API"])
    assert result.exit_code == 0, out(result)
    text = out(result)
    assert "ana@example.com" in text
    assert "ak_act1" in text
    assert "ak_rev2" not in text
    assert "1 ended key hidden" in text
    assert "Read orders" in text
    client.list_agent_api_access.assert_called_once_with("agent-123")


def test_list_all_shows_terminal_keys(runner, client):
    result = runner.invoke(cli, ["agent-api", "access", "list", "Orders API", "--all"])
    assert result.exit_code == 0, out(result)
    assert "ak_rev2" in out(result)
    assert "Revoked by issuer" in out(result)
    assert "hidden" not in out(result)


def test_list_json_is_the_full_raw_listing(runner, client):
    result = runner.invoke(cli, ["agent-api", "access", "list", "Orders API", "--json"])
    assert result.exit_code == 0, out(result)
    assert json.loads(result.output) == listing()


def test_list_warns_when_external_access_is_off(runner, client):
    client.list_agent_api_access.return_value = listing(
        external_access_enabled=False, manage_url=None
    )
    result = runner.invoke(cli, ["agent-api", "access", "list", "Orders API"])
    assert result.exit_code == 0, out(result)
    assert "External access is off" in out(result)
    assert "https://ui.example.com/agent/agent-123#integrations" in out(result)


def test_list_with_only_ended_keys_says_so_in_one_line(runner, client):
    entry = {
        "user": {"id": "u1", "email": "ana@example.com"},
        "grant_id": None,
        "scopes": [],
        "keys": [dict(KEY_REVOKED), dict(KEY_REVOKED, id="k3", token_prefix="ak_rev3")],
    }
    client.list_agent_api_access.return_value = listing(data=[entry], count=1)
    result = runner.invoke(cli, ["agent-api", "access", "list", "Orders API"])
    assert result.exit_code == 0, out(result)
    text = out(result)
    assert "No current API access to Orders API; 2 ended keys hidden (--all)" in text
    assert "Nobody has API access" not in text


def test_list_empty(runner, client):
    client.list_agent_api_access.return_value = listing(data=[], count=0)
    result = runner.invoke(cli, ["agent-api", "access", "list", "Orders API"])
    assert result.exit_code == 0, out(result)
    assert "Nobody has API access" in out(result)


# --- issue ---


def issue_response(**overrides):
    base = {
        "id": "33333333-aaaa-bbbb-cccc-000000000003",
        "token_prefix": "ak_new3",
        "status": "active",
        "service_uri": "orders-api",
        "issued_to_self": True,
        "credential_url": "https://ui.example.com/credentials/cred-9",
        "unknown_scopes": [],
    }
    base.update(overrides)
    return base


def test_issue_to_self_prints_url_and_slot_hint_never_the_value(runner, client):
    client.issue_agent_api_key.return_value = issue_response(token=SECRET)
    result = runner.invoke(
        cli,
        ["agent-api", "access", "issue", "Orders API", "--to", "me",
         "--service-uri", "orders-api"],
    )
    assert result.exit_code == 0, out(result)
    text = out(result)
    assert "https://ui.example.com/credentials/cred-9" in text
    assert 'agent_api_session("orders-api")' in text
    assert SECRET not in text
    kwargs = client.issue_agent_api_key.call_args.kwargs
    assert kwargs["issued_to_email"] is None
    assert kwargs["service_uri"] == "orders-api"


def test_issue_to_self_json_never_carries_the_value(runner, client):
    client.issue_agent_api_key.return_value = issue_response(token=SECRET)
    result = runner.invoke(
        cli, ["agent-api", "access", "issue", "Orders API", "--to", "me", "--json"]
    )
    assert result.exit_code == 0, out(result)
    assert SECRET not in out(result)
    assert "token" not in json.loads(result.output)


def test_issue_to_other_sends_the_email_and_options(runner, client):
    client.issue_agent_api_key.return_value = issue_response(
        issued_to_self=False, credential_url=None
    )
    result = runner.invoke(
        cli,
        ["agent-api", "access", "issue", "Orders API", "--to", "ana@example.com",
         "--scopes", "read, write", "--expires-in-days", "30", "--label", "ci",
         "--read-only"],
    )
    assert result.exit_code == 0, out(result)
    assert "ana@example.com" in out(result)
    assert "ak_new3" in out(result)
    assert "access revoke orders-api ak_new3" in out(result)
    client.issue_agent_api_key.assert_called_once_with(
        "agent-123",
        issued_to_email="ana@example.com",
        scopes=["read", "write"],
        expires_in_days=30,
        service_uri=None,
        label="ci",
        read_only=True,
    )


def test_issue_other_never_prints_the_value_even_if_returned(runner, client):
    client.issue_agent_api_key.return_value = issue_response(
        issued_to_self=False, token=SECRET
    )
    for extra in ([], ["--json"]):
        result = runner.invoke(
            cli,
            ["agent-api", "access", "issue", "Orders API", "--to", "ana@example.com", *extra],
        )
        assert result.exit_code == 0, out(result)
        assert SECRET not in out(result)


def test_issue_without_to_is_a_usage_error(runner, client):
    result = runner.invoke(cli, ["agent-api", "access", "issue", "Orders API"])
    assert result.exit_code != 0
    client.issue_agent_api_key.assert_not_called()


def test_issue_me_sends_no_email_case_insensitive(runner, client):
    client.issue_agent_api_key.return_value = issue_response()
    result = runner.invoke(
        cli, ["agent-api", "access", "issue", "Orders API", "--to", "ME"]
    )
    assert result.exit_code == 0, out(result)
    assert client.issue_agent_api_key.call_args.kwargs["issued_to_email"] is None


def test_issue_empty_scopes_is_an_explicit_empty_list(runner, client):
    client.issue_agent_api_key.return_value = issue_response()
    result = runner.invoke(
        cli, ["agent-api", "access", "issue", "Orders API", "--to", "me", "--scopes", ""]
    )
    assert result.exit_code == 0, out(result)
    assert client.issue_agent_api_key.call_args.kwargs["scopes"] == []


def test_issue_unset_scopes_leaves_the_grant_alone(runner, client):
    client.issue_agent_api_key.return_value = issue_response()
    runner.invoke(cli, ["agent-api", "access", "issue", "Orders API", "--to", "me"])
    assert client.issue_agent_api_key.call_args.kwargs["scopes"] is None


def test_issue_external_access_disabled_prints_the_manage_url(runner, client):
    client.issue_agent_api_key.side_effect = CodedRefusal(
        400, "external_access_disabled", "External access is off for this agent."
    )
    result = runner.invoke(
        cli, ["agent-api", "access", "issue", "Orders API", "--to", "me"]
    )
    assert result.exit_code != 0
    text = out(result)
    assert "External access is off for this agent." in text
    assert "https://ui.example.com/agent/agent-123#integrations" in text


def test_issue_other_coded_refusal_passes_through(runner, client):
    client.issue_agent_api_key.side_effect = CodedRefusal(
        400, "api_disabled", "The API is off."
    )
    result = runner.invoke(
        cli, ["agent-api", "access", "issue", "Orders API", "--to", "me"]
    )
    assert result.exit_code != 0
    assert "The API is off." in out(result)
    assert "integrations" not in out(result)


@pytest.mark.parametrize(
    "argv",
    [
        ["issue", "Orders API", "--to", "nobody@example.com", "--json"],
        ["list", "Orders API", "--json"],
        ["revoke", "Orders API", "ak_act1", "--yes", "--json"],
        ["scopes", "Orders API", "--to", "nobody@example.com", "--clear", "--json"],
    ],
)
def test_json_refusal_carries_the_server_code(runner, client, argv):
    refusal = CodedRefusal(404, "recipient_not_found", "No active user with that email.")
    client.issue_agent_api_key.side_effect = refusal
    client.list_agent_api_access.side_effect = refusal
    client.set_agent_api_scopes.side_effect = refusal
    result = runner.invoke(cli, ["agent-api", "access", *argv])
    assert result.exit_code == 1
    line = json.loads(result.stdout.strip().splitlines()[-1])
    assert line["result"] == "error"
    assert line["code"] == "recipient_not_found"
    assert line["detail"] == "No active user with that email."
    assert line["http_status"] == 404


def test_issue_warns_about_unknown_scopes(runner, client):
    client.issue_agent_api_key.return_value = issue_response(unknown_scopes=["bogus"])
    result = runner.invoke(
        cli,
        ["agent-api", "access", "issue", "Orders API", "--to", "me", "--scopes", "bogus"],
    )
    assert result.exit_code == 0, out(result)
    assert "bogus" in out(result)
    assert "declared scopes" in out(result)


# --- revoke ---


def test_revoke_by_prefix_with_yes(runner, client):
    client.revoke_agent_api_key.return_value = {"status": "revoked_by_issuer"}
    result = runner.invoke(
        cli, ["agent-api", "access", "revoke", "Orders API", "ak_act1", "--yes"]
    )
    assert result.exit_code == 0, out(result)
    client.revoke_agent_api_key.assert_called_once_with("agent-123", KEY_ACTIVE["id"])
    assert "Revoked key ak_act1" in out(result)


def test_revoke_by_id(runner, client):
    client.revoke_agent_api_key.return_value = {"status": "revoked_by_issuer"}
    result = runner.invoke(
        cli, ["agent-api", "access", "revoke", "Orders API", KEY_ACTIVE["id"], "-y"]
    )
    assert result.exit_code == 0, out(result)
    client.revoke_agent_api_key.assert_called_once_with("agent-123", KEY_ACTIVE["id"])


def test_revoke_unknown_ref_lists_the_keys(runner, client):
    result = runner.invoke(
        cli, ["agent-api", "access", "revoke", "Orders API", "nope", "--yes"]
    )
    assert result.exit_code != 0
    assert "No key of this producer matches 'nope'" in out(result)
    assert "ak_act1" in out(result)
    client.revoke_agent_api_key.assert_not_called()


def test_revoke_ambiguous_prefix_asks_for_the_id(runner, client):
    dup = dict(KEY_ACTIVE, id="44444444-aaaa-bbbb-cccc-000000000004")
    data = listing()
    data["data"][0]["keys"].append(dup)
    client.list_agent_api_access.return_value = data
    result = runner.invoke(
        cli, ["agent-api", "access", "revoke", "Orders API", "ak_act1", "--yes"]
    )
    assert result.exit_code != 0
    assert "matches several keys" in out(result)
    assert dup["id"] in out(result)
    client.revoke_agent_api_key.assert_not_called()


@pytest.mark.parametrize("ref", ["ak_ac…", "ak_ac..."])
def test_revoke_refuses_an_elided_ref(runner, client, ref):
    result = runner.invoke(
        cli, ["agent-api", "access", "revoke", "Orders API", ref, "--yes"]
    )
    assert result.exit_code != 0
    assert "elided" in out(result)
    client.revoke_agent_api_key.assert_not_called()


def test_revoke_declined_confirmation_makes_no_call(runner, client):
    with patch("cinna.account.console.confirm", return_value=False):
        result = runner.invoke(
            cli, ["agent-api", "access", "revoke", "Orders API", "ak_act1"]
        )
    assert result.exit_code != 0
    client.revoke_agent_api_key.assert_not_called()


def test_revoke_confirmed_interactively(runner, client):
    client.revoke_agent_api_key.return_value = {"status": "revoked_by_issuer"}
    with patch("cinna.account.console.confirm", return_value=True):
        result = runner.invoke(
            cli, ["agent-api", "access", "revoke", "Orders API", "ak_act1"]
        )
    assert result.exit_code == 0, out(result)
    client.revoke_agent_api_key.assert_called_once()


def test_revoke_json_without_yes_is_a_usage_error(runner, client):
    result = runner.invoke(
        cli, ["agent-api", "access", "revoke", "Orders API", "ak_act1", "--json"]
    )
    assert result.exit_code != 0
    assert "--json needs --yes" in out(result)
    client.revoke_agent_api_key.assert_not_called()
    client.list_agent_api_access.assert_not_called()


def test_revoke_json_with_yes_emits_the_result(runner, client):
    client.revoke_agent_api_key.return_value = {
        "status": "revoked_by_issuer",
        "token": SECRET,
    }
    result = runner.invoke(
        cli,
        ["agent-api", "access", "revoke", "Orders API", "ak_act1", "--json", "--yes"],
    )
    assert result.exit_code == 0, out(result)
    assert json.loads(result.output) == {"status": "revoked_by_issuer"}


def test_revoke_already_revoked_is_a_quiet_success(runner, client):
    result = runner.invoke(
        cli, ["agent-api", "access", "revoke", "Orders API", "ak_rev2", "--yes"]
    )
    assert result.exit_code == 0, out(result)
    assert "already revoked" in out(result)
    client.revoke_agent_api_key.assert_not_called()


# --- scopes ---


def test_scopes_set(runner, client):
    client.set_agent_api_scopes.return_value = {
        "entry": {"user": {"email": "ana@example.com"}, "scopes": ["read", "write"]},
        "unknown_scopes": [],
    }
    result = runner.invoke(
        cli,
        ["agent-api", "access", "scopes", "Orders API", "--to", "ana@example.com",
         "--set", "read,write"],
    )
    assert result.exit_code == 0, out(result)
    client.set_agent_api_scopes.assert_called_once_with(
        "agent-123", "ana@example.com", ["read", "write"]
    )
    assert "read, write" in out(result)


def test_scopes_set_empty_sends_empty_list(runner, client):
    client.set_agent_api_scopes.return_value = {"entry": None, "unknown_scopes": []}
    result = runner.invoke(
        cli,
        ["agent-api", "access", "scopes", "Orders API", "--to", "me", "--set", ""],
    )
    assert result.exit_code == 0, out(result)
    client.set_agent_api_scopes.assert_called_once_with("agent-123", None, [])
    assert "empty grant" in out(result)


def test_scopes_clear_sends_null(runner, client):
    client.set_agent_api_scopes.return_value = {"entry": None, "unknown_scopes": []}
    result = runner.invoke(
        cli,
        ["agent-api", "access", "scopes", "Orders API", "--to", "ana@example.com",
         "--clear"],
    )
    assert result.exit_code == 0, out(result)
    client.set_agent_api_scopes.assert_called_once_with(
        "agent-123", "ana@example.com", None
    )
    assert "Grant removed" in out(result)


@pytest.mark.parametrize("extra", [["--set", "read", "--clear"], []])
def test_scopes_both_or_neither_is_a_usage_error(runner, client, extra):
    result = runner.invoke(
        cli, ["agent-api", "access", "scopes", "Orders API", "--to", "me", *extra]
    )
    assert result.exit_code != 0
    assert "exactly one of --set or --clear" in out(result)
    client.set_agent_api_scopes.assert_not_called()


def test_scopes_warns_about_unknown_scopes(runner, client):
    client.set_agent_api_scopes.return_value = {
        "entry": {"user": {"email": "ana@example.com"}, "scopes": ["bogus"]},
        "unknown_scopes": ["bogus"],
    }
    result = runner.invoke(
        cli,
        ["agent-api", "access", "scopes", "Orders API", "--to", "ana@example.com",
         "--set", "bogus"],
    )
    assert result.exit_code == 0, out(result)
    assert "declared scopes" in out(result)
