"""Tests for the improvement-request verbs — `cinna improve`."""

import io
import json
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
import respx
import httpx
from click.testing import CliRunner

from cinna.account import AccountConfig, save_account_config
from cinna.client import AccountClient
from cinna.main import cli


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
def account_root(tmp_path: Path, account_cfg: AccountConfig) -> Path:
    root = tmp_path / "my-cinna"
    root.mkdir()
    save_account_config(account_cfg, root)
    (root / "agents").mkdir()
    return root


REQUEST_ID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
OTHER_ID = "3f250000-0000-41d3-9a0c-0305e82c3302"

LISTING = {
    "count": 1,
    "data": [
        {
            "id": REQUEST_ID,
            "target_agent_id": "agent-123",
            "target_agent_name": "CRM Agent",
            "source_agent_id": "agent-999",
            "source_agent_name": "CRM Agent (install)",
            "bundle_id": "io.opencinna.cinna.a1b2c3d4",
            "installed_version": "1.3",
            "requester_display": "Jane P.",
            "requester_email": "jane@example.com",
            "comment": "It asked me to re-upload the invoice twice.",
            "status": "new",
            "resolution_note": None,
            "source": "command",
            "snapshot_message_count": 12,
            "snapshot_truncated": False,
            "created_at": "2026-08-19T10:22:31Z",
            "status_changed_at": None,
        }
    ],
}

DETAIL = dict(
    LISTING["data"][0],
    session_title="Invoice questions",
    context={
        "agent": {
            "name": "CRM Agent",
            "is_bundle_install": True,
            "is_publisher_install": False,
            "bundle_id": "io.opencinna.cinna.a1b2c3d4",
            "installed_version": "1.3",
            "installed_revision_number": 7,
            "latest_version": "1.5",
            "latest_revision_number": 9,
            "update_pending": True,
        },
        "environment": {
            "env_name": "python-env-advanced",
            "env_version": "1.0.0",
            "instance_name": "Production",
            "status_at_capture": "running",
            "image_stale": False,
            "critical_state": False,
        },
        "sdk": {
            "session_mode": "conversation",
            "effective_engine": "opencode/anthropic",
            "effective_model": "claude-haiku-4-5",
            "model_override_conversation": "claude-haiku-4-5",
        },
        "plugins": [{"name": "pdf-tools", "source": "bundle"}],
        "recipient": {"owner_display": "Sam O.", "is_shared_externally": True},
    },
)

# Context schema >= 2 adds the prompt-divergence and personal-memory blocks —
# the answer to "is this install running the publisher's prompts or its own?".
DETAIL_V2 = dict(
    DETAIL,
    context=dict(
        DETAIL["context"],
        prompts={
            "schema_version": 2,
            "baseline": "installed_revision",
            "baseline_version": "1.3",
            "diverged": True,
            "workflow": {
                "chars": 1884,
                "updated_at": "2026-08-07T19:27:26+00:00",
                "diverged_from_installed_revision": False,
                "truncated": False,
                "text": "# System prompt",
            },
            "router_trigger": {
                "chars": 52,
                "updated_at": None,
                "diverged_from_installed_revision": True,
                "truncated": False,
                "text": "Use this agent for invoices.",
            },
            "refiner": {"chars": 0, "text": None},
            "sdk_tools": ["bash", "read", "write"],
            "allowed_tools": [],
        },
        memory={
            "schema_version": 2,
            "available": True,
            "file_count": 2,
            "total_chars": 4210,
            "truncated": False,
            "files": [],
        },
        platform={"captured_at": "…", "scrubbed_hits": 3},
    ),
)

# Context schema 3: prompt `role` + `divergence_reason`, and the revision pair
# renamed to latest_published_* with a separate unpublished head.
DETAIL_V3 = dict(
    DETAIL,
    context=dict(
        DETAIL["context"],
        agent={
            "name": "CRM Agent",
            "is_bundle_install": True,
            "is_publisher_install": False,
            "bundle_id": "io.opencinna.cinna.a1b2c3d4",
            "installed_revision_number": 9,
            "installed_version": "1.3",
            "installed_revision_origin": "git",
            "latest_published_revision_number": 7,
            "latest_published_version": "1.2",
            "head_revision_number": 9,
            "update_pending": False,
        },
        prompts={
            "schema_version": 3,
            "baseline": "installed_revision",
            "baseline_version": "1.3",
            "diverged": False,
            "workflow": {
                "role": "published_prompt",
                "chars": 1884,
                "diverged_from_installed_revision": False,
                "divergence_reason": None,
                "updated_at": "2026-08-07T19:27:26+00:00",
                "text": "# System prompt",
            },
            "router_trigger": {
                "role": "routing_metadata",
                "chars": 52,
                "diverged_from_installed_revision": None,
                "divergence_reason": "platform_managed_no_baseline",
                "updated_at": None,
                "text": "Use this agent for invoices.",
            },
            "sdk_tools": ["bash"],
            "allowed_tools": [],
        },
    ),
)

AGENTS_LISTING = {
    "count": 1,
    "data": [
        {
            "id": "agent-123",
            "name": "CRM Agent",
            "can_build": True,
            "is_foreign_install": False,
            "has_active_environment": True,
        }
    ],
}


def _archive_bytes() -> bytes:
    """A minimal stand-in for the platform's improvement archive."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.md", "# Improvement request for CRM Agent\n")
        zf.writestr("metadata.json", json.dumps({"id": REQUEST_ID}))
        zf.writestr("context.json", json.dumps(DETAIL["context"]))
        zf.writestr("session/messages.md", "**user**: hi\n")
        zf.writestr("session/messages.json", json.dumps({"messages": []}))
    return buffer.getvalue()


# --- list -------------------------------------------------------------------


@patch("cinna.improve.AccountClient")
def test_improve_list_renders_table(mock_client_cls, runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    monkeypatch.setenv("COLUMNS", "240")
    client = mock_client_cls.return_value.__enter__.return_value
    client.list_improvement_requests.return_value = LISTING

    result = runner.invoke(cli, ["improve", "list"])

    assert result.exit_code == 0, result.output
    assert "3f2504e0" in result.output
    assert "CRM Agent" in result.output
    assert "Jane P." in result.output
    assert "re-upload the invoice" in result.output
    assert "new" in result.output
    client.list_improvement_requests.assert_called_once_with(
        status=None, agent_id=None, limit=50
    )


@patch("cinna.improve.AccountClient")
def test_improve_list_labels_unversioned_bundle_install(
    mock_client_cls, runner, account_root, monkeypatch
):
    """A bundle with no version label is still a bundle — never 'standalone'."""
    monkeypatch.chdir(account_root)
    monkeypatch.setenv("COLUMNS", "240")
    client = mock_client_cls.return_value.__enter__.return_value
    client.list_improvement_requests.return_value = {
        "count": 1,
        "data": [dict(LISTING["data"][0], installed_version=None)],
    }

    result = runner.invoke(cli, ["improve", "list"])

    assert result.exit_code == 0, result.output
    assert "unversioned" in result.output
    assert "io.opencinna.cinna.a1b2c3d4" in result.output
    assert "standalone" not in result.output


@patch("cinna.improve.AccountClient")
def test_improve_list_labels_standalone_agent(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    monkeypatch.setenv("COLUMNS", "240")
    client = mock_client_cls.return_value.__enter__.return_value
    client.list_improvement_requests.return_value = {
        "count": 1,
        "data": [dict(LISTING["data"][0], installed_version=None, bundle_id=None)],
    }

    result = runner.invoke(cli, ["improve", "list"])

    assert result.exit_code == 0, result.output
    assert "standalone" in result.output


@patch("cinna.improve.AccountClient")
def test_improve_list_filters_by_status_and_agent(
    mock_client_cls, runner, account_root, monkeypatch
):
    """--agent takes a name and is resolved to an id; --status is normalized."""
    monkeypatch.chdir(account_root)
    client = mock_client_cls.return_value.__enter__.return_value
    client.list_account_agents.return_value = AGENTS_LISTING
    client.list_improvement_requests.return_value = LISTING

    result = runner.invoke(
        cli, ["improve", "list", "--status", "in-progress", "--agent", "crm-agent"]
    )

    assert result.exit_code == 0, result.output
    client.list_improvement_requests.assert_called_once_with(
        status="in_progress", agent_id="agent-123", limit=50
    )


@patch("cinna.improve.AccountClient")
def test_improve_list_rejects_unknown_status(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["improve", "list", "--status", "pending"])

    assert result.exit_code != 0
    assert "Unknown status 'pending'" in result.output
    mock_client_cls.assert_not_called()


@patch("cinna.improve.AccountClient")
def test_improve_list_empty_state_names_the_filter(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    client = mock_client_cls.return_value.__enter__.return_value
    client.list_improvement_requests.return_value = {"count": 0, "data": []}

    result = runner.invoke(cli, ["improve", "list", "--status", "new"])

    assert result.exit_code == 0, result.output
    assert "No improvement requests matching status 'new'" in result.output
    assert "/session-improve" in result.output


@patch("cinna.improve.AccountClient")
def test_improve_list_json_output(mock_client_cls, runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    client = mock_client_cls.return_value.__enter__.return_value
    client.list_improvement_requests.return_value = LISTING

    result = runner.invoke(cli, ["improve", "list", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"][0]["id"] == REQUEST_ID


@patch("cinna.improve.AccountClient")
def test_improve_outside_account_workspace(
    mock_client_cls, runner, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["improve", "list"])

    assert result.exit_code != 0
    assert "Not in a cinna account workspace" in result.output
    mock_client_cls.assert_not_called()


# --- show -------------------------------------------------------------------


@patch("cinna.improve.AccountClient")
def test_improve_show_full_id_skips_the_listing(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    monkeypatch.setenv("COLUMNS", "240")
    client = mock_client_cls.return_value.__enter__.return_value
    client.get_improvement_request.return_value = DETAIL

    result = runner.invoke(cli, ["improve", "show", REQUEST_ID])

    assert result.exit_code == 0, result.output
    client.list_improvement_requests.assert_not_called()
    client.get_improvement_request.assert_called_once_with(REQUEST_ID)
    assert "re-upload the invoice" in result.output
    # The runtime-context block is what makes a request actionable.
    assert "claude-haiku-4-5" in result.output
    assert "python-env-advanced" in result.output
    assert "consumer install" in result.output
    # The context describes the requester's install; the conclusion about where a
    # fix belongs must be stated, not left for the reader to derive.
    assert "your publisher install" in result.output
    assert "Invoice questions" in result.output


@patch("cinna.improve.AccountClient")
def test_improve_show_renders_prompt_divergence_and_memory(
    mock_client_cls, runner, account_root, monkeypatch
):
    """Schema-2 context: per-prompt divergence, memory, scrub count."""
    monkeypatch.chdir(account_root)
    monkeypatch.setenv("COLUMNS", "240")
    client = mock_client_cls.return_value.__enter__.return_value
    client.get_improvement_request.return_value = DETAIL_V2

    result = runner.invoke(cli, ["improve", "show", REQUEST_ID])

    assert result.exit_code == 0, result.output
    assert "diverged" in result.output
    assert "installed v1.3" in result.output
    assert "router trigger" in result.output
    assert "1,884 chars" in result.output
    assert "3 tool(s) requested by the agent" in result.output
    # An empty auto-approval list is not "no restriction" — it means every tool
    # use prompted the user, which is why a run can look stuck.
    assert "none auto-approved" in result.output
    # A zero-length prompt is omitted rather than shown as an empty row.
    assert "refiner" not in result.output
    assert "2 file(s), 4,210 chars" in result.output
    assert "3 occurrence(s) masked" in result.output


@patch("cinna.improve.AccountClient")
def test_improve_show_omits_prompt_table_on_legacy_context(
    mock_client_cls, runner, account_root, monkeypatch
):
    """A schema-1 context (no prompts block) still renders, without the table."""
    monkeypatch.chdir(account_root)
    monkeypatch.setenv("COLUMNS", "240")
    client = mock_client_cls.return_value.__enter__.return_value
    client.get_improvement_request.return_value = DETAIL

    result = runner.invoke(cli, ["improve", "show", REQUEST_ID])

    assert result.exit_code == 0, result.output
    assert "vs bundle" not in result.output
    assert "Personal memory" not in result.output
    assert "claude-haiku-4-5" in result.output


@patch("cinna.improve.AccountClient")
def test_improve_show_fix_location_standalone(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    monkeypatch.setenv("COLUMNS", "240")
    client = mock_client_cls.return_value.__enter__.return_value
    client.get_improvement_request.return_value = dict(
        DETAIL,
        context=dict(
            DETAIL["context"],
            agent={"name": "Solo Agent", "is_bundle_install": False},
        ),
    )

    result = runner.invoke(cli, ["improve", "show", REQUEST_ID])

    assert result.exit_code == 0, result.output
    assert "standalone agent's synced workspace" in result.output
    assert "Nothing to publish" in result.output


@patch("cinna.improve.AccountClient")
def test_improve_show_fix_location_publisher_self_report(
    mock_client_cls, runner, account_root, monkeypatch
):
    """Source and target are the same row — say so instead of implying two."""
    monkeypatch.chdir(account_root)
    monkeypatch.setenv("COLUMNS", "240")
    client = mock_client_cls.return_value.__enter__.return_value
    client.get_improvement_request.return_value = dict(
        DETAIL,
        context=dict(
            DETAIL["context"],
            agent=dict(
                DETAIL["context"]["agent"],
                source_agent_id="agent-123",
                is_publisher_install=True,
            ),
            recipient={"target_agent_id": "agent-123", "owner_display": "Sam O."},
        ),
    )

    result = runner.invoke(cli, ["improve", "show", REQUEST_ID])

    assert result.exit_code == 0, result.output
    assert "also the install that reported it" in result.output


@patch("cinna.improve.AccountClient")
def test_improve_show_fix_location_fallback_warns(
    mock_client_cls, runner, account_root, monkeypatch
):
    """Publisher-unavailable fallback: a local fix is transient — say it."""
    monkeypatch.chdir(account_root)
    monkeypatch.setenv("COLUMNS", "240")
    client = mock_client_cls.return_value.__enter__.return_value
    client.get_improvement_request.return_value = dict(
        DETAIL,
        context=dict(
            DETAIL["context"],
            recipient={
                "target_agent_id": "agent-777",
                "fallback_reason": "publisher_unavailable",
            },
        ),
    )

    result = runner.invoke(cli, ["improve", "show", REQUEST_ID])

    assert result.exit_code == 0, result.output
    assert "publisher_unavailable" in result.output
    assert "overwritten by the next bundle update" in result.output


@patch("cinna.improve.AccountClient")
def test_improve_show_never_calls_unknown_divergence_in_sync(
    mock_client_cls, runner, account_root, monkeypatch
):
    """A null divergence is 'not compared' — asserting 'in sync' would be a lie."""
    monkeypatch.chdir(account_root)
    monkeypatch.setenv("COLUMNS", "240")
    client = mock_client_cls.return_value.__enter__.return_value
    client.get_improvement_request.return_value = DETAIL_V3

    result = runner.invoke(cli, ["improve", "show", REQUEST_ID])

    assert result.exit_code == 0, result.output
    assert "not compared" in result.output
    assert "platform managed no baseline" in result.output
    assert "routing metadata" in result.output
    # The rollup is false, so no divergence warning banner.
    assert "differ from the bundle revision" not in result.output


@patch("cinna.improve.AccountClient")
def test_improve_show_renders_published_vs_head_revisions(
    mock_client_cls, runner, account_root, monkeypatch
):
    """Installed ahead of latest published: say which is which, and why."""
    monkeypatch.chdir(account_root)
    monkeypatch.setenv("COLUMNS", "240")
    client = mock_client_cls.return_value.__enter__.return_value
    client.get_improvement_request.return_value = DETAIL_V3

    result = runner.invoke(cli, ["improve", "show", REQUEST_ID])

    assert result.exit_code == 0, result.output
    assert "v1.3 (revision 9) · git" in result.output
    assert "Latest published" in result.output
    assert "v1.2 (revision 7)" in result.output
    assert "revision 9 (not published)" in result.output


@patch("cinna.improve.AccountClient")
def test_improve_list_flags_same_session_duplicates(
    mock_client_cls, runner, account_root, monkeypatch
):
    """Two captures of one session are visible in the listing, not after two
    downloads."""
    monkeypatch.chdir(account_root)
    monkeypatch.setenv("COLUMNS", "240")
    client = mock_client_cls.return_value.__enter__.return_value
    client.list_improvement_requests.return_value = {
        "count": 2,
        "data": [
            dict(LISTING["data"][0], id=REQUEST_ID, session_id="5e551011-1111-1111-1111-111111111111"),
            dict(LISTING["data"][0], id=OTHER_ID, session_id="5e551011-1111-1111-1111-111111111111"),
        ],
    }

    result = runner.invoke(cli, ["improve", "list"])

    assert result.exit_code == 0, result.output
    assert "5e551011" in result.output
    assert "×2 same session" in result.output


@patch("cinna.improve.AccountClient")
def test_improve_list_nudges_when_context_package_stale(
    mock_client_cls, runner, account_root, monkeypatch
):
    """The queue depends on a shipped guide — say so when the tree is behind."""
    monkeypatch.chdir(account_root)
    monkeypatch.setenv("COLUMNS", "240")
    (account_root / "context").mkdir()
    (account_root / "context" / "VERSION").write_text("old-version\n")
    client = mock_client_cls.return_value.__enter__.return_value
    client.list_improvement_requests.return_value = LISTING
    client.get_context_package_version.return_value = "new-version"

    result = runner.invoke(cli, ["improve", "list"])

    assert result.exit_code == 0, result.output
    assert "context/ package is out of date" in result.output


@patch("cinna.improve.AccountClient")
def test_improve_list_quiet_when_context_package_current(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    monkeypatch.setenv("COLUMNS", "240")
    (account_root / "context").mkdir()
    (account_root / "context" / "VERSION").write_text("same-version\n")
    client = mock_client_cls.return_value.__enter__.return_value
    client.list_improvement_requests.return_value = LISTING
    client.get_context_package_version.return_value = "same-version"

    result = runner.invoke(cli, ["improve", "list"])

    assert result.exit_code == 0, result.output
    assert "out of date" not in result.output
    assert "refresh-context" not in result.output


@patch("cinna.improve.AccountClient")
def test_improve_show_resolves_short_id(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    monkeypatch.setenv("COLUMNS", "240")
    client = mock_client_cls.return_value.__enter__.return_value
    client.list_improvement_requests.return_value = LISTING
    client.get_improvement_request.return_value = DETAIL

    result = runner.invoke(cli, ["improve", "show", "3f2504e0"])

    assert result.exit_code == 0, result.output
    client.get_improvement_request.assert_called_once_with(REQUEST_ID)


@patch("cinna.improve.AccountClient")
def test_improve_show_ambiguous_short_id(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    client = mock_client_cls.return_value.__enter__.return_value
    client.list_improvement_requests.return_value = {
        "count": 2,
        "data": [{"id": REQUEST_ID}, {"id": OTHER_ID}],
    }

    result = runner.invoke(cli, ["improve", "show", "3f25"])

    assert result.exit_code != 0
    assert "ambiguous" in result.output
    client.get_improvement_request.assert_not_called()


@patch("cinna.improve.AccountClient")
def test_improve_show_unknown_short_id(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    client = mock_client_cls.return_value.__enter__.return_value
    client.list_improvement_requests.return_value = {"count": 0, "data": []}

    result = runner.invoke(cli, ["improve", "show", "deadbeef"])

    assert result.exit_code != 0
    assert "No improvement request matching 'deadbeef'" in result.output


@patch("cinna.improve.AccountClient")
def test_improve_show_json_output(mock_client_cls, runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    client = mock_client_cls.return_value.__enter__.return_value
    client.get_improvement_request.return_value = DETAIL

    result = runner.invoke(cli, ["improve", "show", REQUEST_ID, "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["context"]["sdk"]["effective_model"] == (
        "claude-haiku-4-5"
    )


# --- download ---------------------------------------------------------------


@patch("cinna.improve.AccountClient")
def test_improve_download_extracts_into_improvements(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    client = mock_client_cls.return_value.__enter__.return_value
    client.download_improvement_archive.return_value = _archive_bytes()

    result = runner.invoke(cli, ["improve", "download", REQUEST_ID])

    assert result.exit_code == 0, result.output
    target = account_root / "improvements" / "3f2504e0"
    assert (target / "README.md").is_file()
    assert (target / "session" / "messages.md").is_file()
    assert (target / "context.json").is_file()
    client.download_improvement_archive.assert_called_once_with(REQUEST_ID)
    assert "another person's conversation" in result.output


@patch("cinna.improve.AccountClient")
def test_improve_download_honors_out_dir(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    client = mock_client_cls.return_value.__enter__.return_value
    client.download_improvement_archive.return_value = _archive_bytes()

    result = runner.invoke(cli, ["improve", "download", REQUEST_ID, "--out", "inbox"])

    assert result.exit_code == 0, result.output
    assert (account_root / "inbox" / "README.md").is_file()
    assert not (account_root / "improvements").exists()


@patch("cinna.improve.AccountClient")
def test_improve_download_rejects_unsafe_members(
    mock_client_cls, runner, account_root, monkeypatch
):
    """Archive extraction is the safe extractor — no traversal outside the dir."""
    monkeypatch.chdir(account_root)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("../escaped.md", "nope")
        zf.writestr("README.md", "ok")
    client = mock_client_cls.return_value.__enter__.return_value
    client.download_improvement_archive.return_value = buffer.getvalue()

    result = runner.invoke(cli, ["improve", "download", REQUEST_ID])

    assert result.exit_code == 0, result.output
    assert (account_root / "improvements" / "3f2504e0" / "README.md").is_file()
    assert not (account_root / "improvements" / "escaped.md").exists()


# --- status -----------------------------------------------------------------


@patch("cinna.improve.AccountClient")
def test_improve_status_sets_status_and_note(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    client = mock_client_cls.return_value.__enter__.return_value
    client.update_improvement_request.return_value = dict(
        DETAIL, status="completed", resolution_note="Fixed in v1.6"
    )

    result = runner.invoke(
        cli,
        ["improve", "status", REQUEST_ID, "completed", "--note", "Fixed in v1.6"],
    )

    assert result.exit_code == 0, result.output
    client.update_improvement_request.assert_called_once_with(
        REQUEST_ID, status="completed", resolution_note="Fixed in v1.6"
    )
    assert "completed" in result.output
    assert "Fixed in v1.6" in result.output


@patch("cinna.improve.AccountClient")
def test_improve_status_normalizes_dashed_status(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    client = mock_client_cls.return_value.__enter__.return_value
    client.update_improvement_request.return_value = dict(DETAIL, status="in_progress")

    result = runner.invoke(cli, ["improve", "status", REQUEST_ID, "In-Progress"])

    assert result.exit_code == 0, result.output
    client.update_improvement_request.assert_called_once_with(
        REQUEST_ID, status="in_progress", resolution_note=None
    )
    assert "completed" in result.output  # the follow-up hint


@patch("cinna.improve.AccountClient")
def test_improve_status_rejects_unknown_status(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["improve", "status", REQUEST_ID, "wontfix"])

    assert result.exit_code != 0
    assert "Unknown status 'wontfix'" in result.output
    mock_client_cls.assert_not_called()


# --- client transport -------------------------------------------------------


@respx.mock
def test_client_list_improvement_requests_sends_filters(account_cfg):
    route = respx.get(
        "https://platform.example.com/api/v1/cli/account/improvement-requests"
    ).mock(return_value=httpx.Response(200, json=LISTING))

    with AccountClient(account_cfg) as client:
        payload = client.list_improvement_requests(
            status="new", agent_id="agent-123", limit=10
        )

    assert payload["count"] == 1
    request = route.calls[0].request
    assert request.url.params["status"] == "new"
    assert request.url.params["agent_id"] == "agent-123"
    assert request.url.params["limit"] == "10"
    assert request.headers["authorization"] == "Bearer account-token-abc"


@respx.mock
def test_client_download_improvement_archive_returns_bytes(account_cfg):
    payload = _archive_bytes()
    respx.get(
        "https://platform.example.com/api/v1/cli/account/improvement-requests/"
        f"{REQUEST_ID}/archive"
    ).mock(
        return_value=httpx.Response(
            200, content=payload, headers={"content-type": "application/zip"}
        )
    )

    with AccountClient(account_cfg) as client:
        archive = client.download_improvement_archive(REQUEST_ID)

    assert zipfile.is_zipfile(io.BytesIO(archive))


@respx.mock
def test_client_update_improvement_request_patches(account_cfg):
    route = respx.patch(
        "https://platform.example.com/api/v1/cli/account/improvement-requests/"
        f"{REQUEST_ID}"
    ).mock(return_value=httpx.Response(200, json=dict(DETAIL, status="declined")))

    with AccountClient(account_cfg) as client:
        detail = client.update_improvement_request(
            REQUEST_ID, status="declined", resolution_note="Working as intended"
        )

    assert detail["status"] == "declined"
    assert json.loads(route.calls[0].request.content) == {
        "status": "declined",
        "resolution_note": "Working as intended",
    }


@respx.mock
def test_client_improvement_request_404_raises(account_cfg):
    respx.get(
        "https://platform.example.com/api/v1/cli/account/improvement-requests/"
        f"{REQUEST_ID}"
    ).mock(return_value=httpx.Response(404, json={"detail": "Not found"}))

    from cinna.errors import PlatformError

    with AccountClient(account_cfg) as client:
        with pytest.raises(PlatformError):
            client.get_improvement_request(REQUEST_ID)
