"""Tests for `cinna agent import` — the Local Agent Kit go-cloud step."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from cinna.account import AccountConfig, save_account_config
from cinna.config import CinnaConfig, save_config, upsert_agent_registry
from cinna.errors import PlatformError
from cinna.local_import import (
    DEFAULT_EXCLUDE,
    assert_no_secrets,
    load_export_contract,
    is_excluded,
    load_manifest,
    plan_copy,
    render_requirements,
)
from cinna import kit_contract
from cinna.main import cli

SECRET = "hunter2-do-not-print-me"
AGENT_ID = "agent-new-1"


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
    root = tmp_path / "MyAgents" / "Cloud"
    root.mkdir(parents=True)
    save_account_config(account_cfg, root)
    (root / "agents").mkdir()
    return root


MANIFEST = {
    "schema_version": 1,
    "kit_version": "3f9c1e2a7b4d5e60",
    "name": "Invoice Watcher",
    "slug": "invoice-watcher",
    "description": "Watches the billing inbox and flags invoices missing a PO number.",
    "example_prompts": ["check invoices from last week", "list invoices without PO"],
    "router_trigger_prompt": "Checks incoming invoices and flags missing PO numbers.",
    "prompts": {
        "workflow": "docs/WORKFLOW_PROMPT.md",
        "entrypoint": "docs/ENTRYPOINT_PROMPT.md",
        "refiner": "docs/REFINER_PROMPT.md",
    },
    "status_refresh_command": "/run:status",
    "credentials": [
        {
            "name": "billing-inbox",
            "type": "email_imap",
            "description": "IMAP access to billing@…",
            "env_prefix": "BILLING_INBOX_",
            "fields": ["host", "port", "login", "password"],
        }
    ],
    "schedules": [
        {
            "name": "Weekday morning check",
            "cron_string": "0 6 * * 1-5",
            "timezone": "Europe/Berlin",
            "schedule_type": "static_prompt",
            "prompt": "Check the invoices that arrived since yesterday.",
            "command": None,
            "enabled": True,
        }
    ],
    "handovers": [],
    "features": {"webapp": False, "agent_api": False},
    "cloud": {"platform_url": None, "agent_id": None, "imported_at": None},
}


def make_local_agent(tmp_path: Path, **manifest_overrides) -> Path:
    """Materialize a Local Agent Kit agent folder under MyAgents/Local/."""
    src = tmp_path / "MyAgents" / "Local" / "invoice-watcher"
    for sub in ("docs", "scripts", "credentials", "app-data/storage", ".venv", "files"):
        (src / sub).mkdir(parents=True, exist_ok=True)

    manifest = json.loads(json.dumps(MANIFEST))
    manifest.update(manifest_overrides)
    (src / "cinna-agent.json").write_text(json.dumps(manifest, indent=2) + "\n")

    (src / "docs" / "WORKFLOW_PROMPT.md").write_text("Run the invoice checker.\n")
    (src / "docs" / "ENTRYPOINT_PROMPT.md").write_text("Check today's invoices.\n")
    (src / "docs" / "REFINER_PROMPT.md").write_text("Default to the current week.\n")
    (src / "scripts" / "check.py").write_text("print('checking')\n")
    (src / "scripts" / "check.pyc").write_bytes(b"\x00compiled")
    (src / "files" / ".gitkeep").write_text("")
    (src / "README.md").write_text("# Invoice Watcher\n")
    (src / "AGENTS.md").write_text("local runtime wrapper\n")
    (src / "CLAUDE.md").write_text("@AGENTS.md\n")
    (src / ".venv" / "pyvenv.cfg").write_text("home = /usr\n")
    (src / "app-data" / "storage" / "STATUS.md").write_text("all good\n")
    (src / "credentials" / ".env").write_text(f"BILLING_INBOX_PASSWORD={SECRET}\n")
    (src / "credentials" / ".env.example").write_text("BILLING_INBOX_PASSWORD=\n")
    (src / "pyproject.toml").write_text(
        '[project]\nname = "invoice-watcher"\nversion = "0.1.0"\n'
        'dependencies = ["httpx>=0.27", "python-dateutil"]\n'
    )
    return src


def make_kit(
    tmp_path: Path, excludes: list | None = None, contract_version: str = "1.0.0"
) -> Path:
    """A `.cinna-kit/` carrying the contract, as a kit install leaves it.

    `layout.json` is the authority for the exclude list and the secret rules;
    `kit.json` carries the contract version the folder is gated against.
    """
    kit_dir = tmp_path / "MyAgents" / ".cinna-kit"
    kit_dir.mkdir(parents=True, exist_ok=True)
    (kit_dir / "layout.json").write_text(
        json.dumps(
            {
                "contract_version": contract_version,
                "cloud_import_excludes": (
                    list(kit_contract.DEFAULT_EXCLUDE) if excludes is None else excludes
                ),
                "secret_files": {"rules": kit_contract.DEFAULT_SECRET_FILE_RULES},
            }
        )
    )
    (kit_dir / "kit.json").write_text(json.dumps({"contract_version": contract_version}))
    return kit_dir


def make_child_workspace(account_root: Path, agent_id: str = AGENT_ID) -> Path:
    """A synced per-agent workspace under agents/, as `agent sync` leaves it."""
    ws = account_root / "agents" / "invoice-watcher"
    ws.mkdir(parents=True)
    config = CinnaConfig(
        platform_url="https://platform.example.com",
        cli_token="child-token-xyz",
        agent_id=agent_id,
        agent_name="Invoice Watcher",
        environment_id="env-1",
        template="general-env",
        frontend_url="https://ui.example.com",
        cli_token_id="tok-1",
    )
    save_config(config, ws)
    upsert_agent_registry(
        agent_id, config.platform_url, config.cli_token, ws, frontend_url=config.frontend_url
    )
    (ws / "workspace").mkdir()
    return ws


def wire_client(mock_client_cls) -> MagicMock:
    """Give the mocked AccountClient a full happy-path response set."""
    client = mock_client_cls.return_value.__enter__.return_value
    client.create_agent.return_value = {"id": AGENT_ID, "name": "Invoice Watcher"}
    client.list_account_agents.return_value = {
        "data": [{"id": AGENT_ID, "name": "Invoice Watcher"}]
    }
    client.update_agent_config.return_value = {}
    client.set_status_refresh_command.return_value = {
        "status_refresh_command": "/run:status"
    }
    client.list_credentials.return_value = {"data": []}
    client.create_credential.return_value = {
        "credential": {"id": "cred-1", "name": "billing-inbox", "type": "email_imap"},
        "required_fields": ["host", "login", "password"],
        "setup_url": "https://ui.example.com/credentials/cred-1",
    }
    client.list_schedules.return_value = {"data": []}
    client.create_schedule.return_value = {
        "id": "sched-1",
        "name": "Weekday morning check",
        "cron_string": "0 5 * * 1-5",
    }
    return client


def wire_sync(mock_sync, conflicts: int = 0) -> None:
    mock_sync.flush.return_value = MagicMock(state="watching", conflict_count=conflicts)


# --- pure helpers ----------------------------------------------------------


def test_default_exclude_is_the_contracts_list():
    """The fallback is the contract's `cloud_import_excludes`, not kit.json's.

    Decision D6 deleted `cloud_import.exclude` from kit.json; the list moved to
    `layout.json`. The entry-by-entry drift guard lives in
    `tests/test_kit_contract.py`, which is where the list itself now lives.
    """
    assert DEFAULT_EXCLUDE is kit_contract.DEFAULT_EXCLUDE
    assert "publications.json" in DEFAULT_EXCLUDE


@pytest.mark.parametrize(
    "rel",
    [
        "credentials/.env",
        "app-data/storage/STATUS.md",
        ".venv/pyvenv.cfg",
        "AGENTS.md",
        "CLAUDE.md",
        "README.md",
        "Makefile",
        "publications.json",
        "scripts/check.pyc",
        "scripts/__pycache__/check.cpython-311.pyc",
        ".git/config",
        ".DS_Store",
        "files/nested/.DS_Store",
    ],
)
def test_is_excluded_covers_the_contract_patterns(rel):
    assert is_excluded(rel, DEFAULT_EXCLUDE) is True


@pytest.mark.parametrize(
    "rel",
    [
        "docs/WORKFLOW_PROMPT.md",
        "scripts/check.py",
        # Anchored, so only the agent's own README/Makefile are dropped.
        "docs/README.md",
        "scripts/README.md",
        # A nested publications.json is a user's file, not our ledger.
        "files/publications.json",
    ],
)
def test_is_excluded_keeps_agent_content(rel):
    assert is_excluded(rel, DEFAULT_EXCLUDE) is False


def test_plan_copy_never_selects_credentials_or_app_data(tmp_path):
    src = make_local_agent(tmp_path)
    files = plan_copy(src, DEFAULT_EXCLUDE)
    assert "docs/WORKFLOW_PROMPT.md" in files
    assert "scripts/check.py" in files
    assert not [f for f in files if f.startswith("credentials/")]
    assert not [f for f in files if f.startswith("app-data/")]
    assert "AGENTS.md" not in files and "CLAUDE.md" not in files


# --- the secret gate (handover §3 acceptance) ------------------------------


@pytest.mark.parametrize(
    "rel",
    [
        ".env.prod",  # travelled before the contract landed — this is the fix
        ".env.local",
        "a/b/.env.prod",  # ... and at any depth
        "config/.env.staging",
        ".env",  # already refused; guard against a rewrite that loses it
        "staging.env",  # ditto — the `basename_suffix` clause
        "credentials/.env",
    ],
)
def test_assert_no_secrets_refuses_every_dotenv_shape(rel):
    with pytest.raises(click.ClickException):
        assert_no_secrets([rel])


@pytest.mark.parametrize(
    "rel", [".env.example", ".env.sample", ".env.template", "config/.env.example"]
)
def test_assert_no_secrets_lets_the_examples_travel(rel):
    """The `unless` block. A bare `startswith(".env.")` would fail this."""
    assert_no_secrets([rel]) is None


def test_plan_copy_withholds_dotenv_suffixes_from_the_plan(tmp_path):
    """The gate runs in the walk, not only at copy time.

    A path withheld from the upload but counted in a `content_hash` moves that
    hash for a change that can never be published.
    """
    src = make_local_agent(tmp_path)
    (src / ".env.prod").write_text(f"TOKEN={SECRET}\n")
    (src / ".env.example").write_text("TOKEN=\n")
    (src / "config").mkdir(exist_ok=True)
    (src / "config" / ".env.staging").write_text(f"TOKEN={SECRET}\n")

    files = plan_copy(src, DEFAULT_EXCLUDE)
    assert ".env.prod" not in files
    assert "config/.env.staging" not in files
    assert ".env.example" in files


def test_plan_copy_never_reads_a_tree_it_could_not_see(tmp_path):
    src = make_local_agent(tmp_path)
    (src / "scripts").chmod(0o000)
    try:
        with pytest.raises(click.ClickException) as exc:
            plan_copy(src, DEFAULT_EXCLUDE)
    finally:
        (src / "scripts").chmod(0o755)
    assert "scripts/" in str(exc.value)


def test_render_requirements_from_pyproject(tmp_path):
    src = make_local_agent(tmp_path)
    body = render_requirements(src)
    assert "httpx>=0.27" in body
    assert "python-dateutil" in body


def test_render_requirements_without_pyproject(tmp_path):
    src = make_local_agent(tmp_path)
    (src / "pyproject.toml").unlink()
    assert render_requirements(src) is None


def test_load_manifest_refuses_newer_schema(tmp_path):
    src = make_local_agent(tmp_path, schema_version=2)
    with pytest.raises(Exception, match="schema_version 2"):
        load_manifest(src)


def test_load_manifest_refuses_slug_folder_mismatch(tmp_path):
    src = make_local_agent(tmp_path, slug="other-agent")
    with pytest.raises(Exception, match="does not match the folder name"):
        load_manifest(src)


def test_load_manifest_refuses_bad_cron(tmp_path):
    bad = json.loads(json.dumps(MANIFEST["schedules"]))
    bad[0]["cron_string"] = "every morning"
    src = make_local_agent(tmp_path, schedules=bad)
    with pytest.raises(Exception, match="cron_string"):
        load_manifest(src)


# --- the command -----------------------------------------------------------


@patch("cinna.local_import.sync_session")
@patch("cinna.local_import.AccountClient")
def test_import_happy_path(
    mock_client_cls, mock_sync, runner, account_root, tmp_path, monkeypatch
):
    make_kit(tmp_path)
    src = make_local_agent(tmp_path)
    ws = make_child_workspace(account_root)
    client = wire_client(mock_client_cls)
    wire_sync(mock_sync)
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["agent", "import", str(src), "--yes"])
    assert result.exit_code == 0, result.output

    # 1. agent created in the account's active workspace
    client.create_agent.assert_called_once_with(
        "Invoice Watcher", MANIFEST["description"], user_workspace_id=None
    )

    # 2. one bulk prompt/metadata write carrying every field
    agent_id, body = client.update_agent_config.call_args.args
    assert agent_id == AGENT_ID
    assert body["description"] == MANIFEST["description"]
    assert body["router_trigger_prompt"] == MANIFEST["router_trigger_prompt"]
    assert body["example_prompts"] == MANIFEST["example_prompts"]
    assert body["workflow_prompt"] == "Run the invoice checker."
    assert body["entrypoint_prompt"] == "Check today's invoices."
    assert body["refiner_prompt"] == "Default to the current week."
    client.set_status_refresh_command.assert_called_once_with(AGENT_ID, "/run:status")

    # 3. files copied with the kit exclusions honoured
    dest = ws / "workspace"
    assert (dest / "docs" / "WORKFLOW_PROMPT.md").is_file()
    assert (dest / "scripts" / "check.py").is_file()
    assert not (dest / "credentials").exists()
    assert not (dest / "app-data").exists()
    assert not (dest / ".venv").exists()
    assert not (dest / "AGENTS.md").exists()
    assert not (dest / "scripts" / "check.pyc").exists()

    # 4. workspace_requirements.txt generated from pyproject
    assert "httpx>=0.27" in (dest / "workspace_requirements.txt").read_text()

    # 5. pushed
    mock_sync.ensure_session.assert_called_once()
    mock_sync.flush.assert_called_once()

    # 6. credential draft created, attached, and its setup URL printed
    client.create_credential.assert_called_once_with(
        "billing-inbox",
        "email_imap",
        notes="IMAP access to billing@…",
        service_uri=None,
        allow_sharing=False,
        user_workspace_id=None,
    )
    client.share_credential_with_agent.assert_called_once_with("cred-1", AGENT_ID)
    assert "https://ui.example.com/credentials/cred-1" in result.output

    # 7. schedule created
    sched_agent_id, sched_body = client.create_schedule.call_args.args
    assert sched_agent_id == AGENT_ID
    assert sched_body["name"] == "Weekday morning check"
    assert sched_body["cron_string"] == "0 6 * * 1-5"
    assert sched_body["timezone"] == "Europe/Berlin"
    assert sched_body["schedule_type"] == "static_prompt"
    assert sched_body["enabled"] is True

    # 8. the publication recorded in the ledger, only after the push
    ledger = json.loads((src / "publications.json").read_text())
    assert list(ledger) == ["publications"]  # an object, never a bare array
    (entry,) = ledger["publications"]
    assert entry["platform_url"] == "https://platform.example.com"
    assert entry["agent_id"] == AGENT_ID
    # The ACCOUNT workspace relative to the workshop root, per the schema —
    # not the per-agent child workspace under it.
    assert entry["workspace"] == "Cloud"
    assert entry["imported_at"] and entry["updated_at"]
    assert entry["content_hash"].startswith("sha256:")

    local_manifest = json.loads((src / "cinna-agent.json").read_text())
    assert local_manifest["kit_version"] == MANIFEST["kit_version"]  # keys preserved
    # The all-null legacy `cloud` block has no platform_url/agent_id pair to
    # move, so it is left in place rather than deleted — and said out loud.
    assert local_manifest["cloud"]["agent_id"] is None
    assert "cloud" in result.output and "left in" in result.output
    # The ledger is a sibling of the manifest and never a key inside it.
    assert "publications" not in local_manifest
    # ...and it does not travel: it is in the contract's exclude list.
    assert not (dest / "publications.json").exists()
    synced_manifest = json.loads((dest / "cinna-agent.json").read_text())
    assert synced_manifest["slug"] == "invoice-watcher"

    # 9. next steps
    assert 'cinna chat --agent invoice-watcher "check invoices from last week"' in (
        result.output
    )


@patch("cinna.local_import.sync_session")
@patch("cinna.local_import.AccountClient")
def test_import_never_copies_or_prints_secrets(
    mock_client_cls, mock_sync, runner, account_root, tmp_path, monkeypatch
):
    src = make_local_agent(tmp_path)
    # The shapes that cleared both gates before the contract landed: a dotenv
    # suffix at the agent root and at depth, outside credentials/.
    (src / ".env.prod").write_text(f"BILLING_INBOX_PASSWORD={SECRET}\n")
    (src / "config").mkdir(exist_ok=True)
    (src / "config" / ".env.staging").write_text(f"BILLING_INBOX_PASSWORD={SECRET}\n")
    (src / ".env.example").write_text("BILLING_INBOX_PASSWORD=\n")
    ws = make_child_workspace(account_root)
    wire_client(mock_client_cls)
    wire_sync(mock_sync)
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["agent", "import", str(src), "--yes"])
    assert result.exit_code == 0, result.output

    assert SECRET not in result.output
    copied = [p for p in (ws / "workspace").rglob("*") if p.is_file()]
    assert not [p for p in copied if p.name == ".env"]
    assert not [p for p in copied if p.name.startswith(".env.") and p.name != ".env.example"]
    assert [p for p in copied if p.name == ".env.example"]  # still travels
    for path in copied:
        assert SECRET not in path.read_bytes().decode("utf-8", "replace")


@patch("cinna.local_import.sync_session")
@patch("cinna.local_import.AccountClient")
def test_dry_run_makes_no_platform_calls(
    mock_client_cls, mock_sync, runner, account_root, tmp_path, monkeypatch
):
    src = make_local_agent(tmp_path)
    ws = make_child_workspace(account_root)
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["agent", "import", str(src), "--dry-run"])
    assert result.exit_code == 0, result.output

    mock_client_cls.assert_not_called()
    mock_sync.ensure_session.assert_not_called()
    mock_sync.flush.assert_not_called()
    # nothing written locally either
    assert not any((ws / "workspace").iterdir())
    assert not (src / "publications.json").exists()
    manifest = json.loads((src / "cinna-agent.json").read_text())
    assert manifest["cloud"]["agent_id"] is None
    # ...but the plan is visible
    assert "docs/WORKFLOW_PROMPT.md" in result.output
    assert "billing-inbox" in result.output
    assert "Weekday morning check" in result.output


@patch("cinna.local_import.sync_session")
@patch("cinna.local_import.AccountClient")
def test_second_import_without_update_is_refused(
    mock_client_cls, mock_sync, runner, account_root, tmp_path, monkeypatch
):
    src = make_local_agent(
        tmp_path,
        cloud={
            "platform_url": "https://platform.example.com",
            "agent_id": AGENT_ID,
            "imported_at": "2026-09-01T10:00:00+00:00",
        },
    )
    make_child_workspace(account_root)
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["agent", "import", str(src), "--yes"])
    assert result.exit_code != 0
    assert "--update" in result.output + result.stderr
    mock_client_cls.assert_not_called()


@patch("cinna.local_import.sync_session")
@patch("cinna.local_import.AccountClient")
def test_update_resumes_without_duplicating(
    mock_client_cls, mock_sync, runner, account_root, tmp_path, monkeypatch
):
    """A re-import resolves by id and never re-creates agent/credential/schedule."""
    src = make_local_agent(
        tmp_path,
        cloud={
            "platform_url": "https://platform.example.com",
            "agent_id": AGENT_ID,
            "imported_at": "2026-09-01T10:00:00+00:00",
        },
    )
    make_child_workspace(account_root)
    client = wire_client(mock_client_cls)
    client.list_credentials.return_value = {
        "data": [{"id": "cred-1", "name": "billing-inbox", "type": "email_imap"}]
    }
    client.list_schedules.return_value = {
        "data": [{"id": "sched-1", "name": "Weekday morning check"}]
    }
    wire_sync(mock_sync)
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["agent", "import", str(src), "--update", "--yes"])
    assert result.exit_code == 0, result.output

    client.create_agent.assert_not_called()
    client.create_credential.assert_not_called()
    client.create_schedule.assert_not_called()
    client.share_credential_with_agent.assert_called_once_with("cred-1", AGENT_ID)
    client.update_schedule.assert_called_once()
    upd_agent_id, sched_id, body = client.update_schedule.call_args.args
    assert (upd_agent_id, sched_id) == (AGENT_ID, "sched-1")
    assert body["cron_string"] == "0 6 * * 1-5"


@patch("cinna.local_import.sync_session")
@patch("cinna.local_import.AccountClient")
def test_update_refuses_unknown_agent_id(
    mock_client_cls, mock_sync, runner, account_root, tmp_path, monkeypatch
):
    src = make_local_agent(
        tmp_path,
        cloud={
            "platform_url": "https://platform.example.com",
            "agent_id": "agent-gone",
            "imported_at": "2026-09-01T10:00:00+00:00",
        },
    )
    make_child_workspace(account_root)
    wire_client(mock_client_cls)
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["agent", "import", str(src), "--update", "--yes"])
    assert result.exit_code != 0
    assert "agent-gone" in result.output + result.stderr


@patch("cinna.local_import.run_agent_sync")
@patch("cinna.local_import.sync_session")
@patch("cinna.local_import.AccountClient")
def test_import_syncs_a_workspace_when_missing(
    mock_client_cls, mock_sync, mock_agent_sync, runner, account_root, tmp_path, monkeypatch
):
    src = make_local_agent(tmp_path)
    wire_client(mock_client_cls)
    wire_sync(mock_sync)
    mock_agent_sync.side_effect = lambda *a, **k: make_child_workspace(account_root)
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["agent", "import", str(src), "--yes"])
    assert result.exit_code == 0, result.output
    mock_agent_sync.assert_called_once_with(AGENT_ID, None)
    workspace = account_root / "agents" / "invoice-watcher" / "workspace"
    assert (workspace / "docs" / "WORKFLOW_PROMPT.md").is_file()


@patch("cinna.local_import.sync_session")
@patch("cinna.local_import.AccountClient")
def test_no_push_leaves_the_publication_unrecorded(
    mock_client_cls, mock_sync, runner, account_root, tmp_path, monkeypatch
):
    src = make_local_agent(tmp_path)
    ws = make_child_workspace(account_root)
    wire_client(mock_client_cls)
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["agent", "import", str(src), "--no-push", "--yes"])
    assert result.exit_code == 0, result.output

    mock_sync.flush.assert_not_called()
    assert (ws / "workspace" / "docs" / "WORKFLOW_PROMPT.md").is_file()
    assert not (src / "publications.json").exists()
    manifest = json.loads((src / "cinna-agent.json").read_text())
    assert manifest["cloud"]["agent_id"] is None
    assert "--update" in result.output


@patch("cinna.local_import.sync_session")
@patch("cinna.local_import.AccountClient")
def test_push_conflicts_block_the_stamp(
    mock_client_cls, mock_sync, runner, account_root, tmp_path, monkeypatch
):
    src = make_local_agent(tmp_path)
    make_child_workspace(account_root)
    wire_client(mock_client_cls)
    wire_sync(mock_sync, conflicts=2)
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["agent", "import", str(src), "--yes"])
    assert result.exit_code == 0, result.output
    assert not (src / "publications.json").exists()
    manifest = json.loads((src / "cinna-agent.json").read_text())
    assert manifest["cloud"]["agent_id"] is None
    assert "conflict" in result.output.lower()


@patch("cinna.local_import.sync_session")
@patch("cinna.local_import.AccountClient")
def test_layout_json_supplies_the_exclude_list(
    mock_client_cls, mock_sync, runner, account_root, tmp_path, monkeypatch
):
    """The contract widens the exclusions; credentials/ stays excluded regardless.

    `.cinna-kit/layout.json` is the authority — this is the release gate cinna-core
    is holding: until this read exists, nothing it publishes about excludes or
    secret files can reach this tool.
    """
    src = make_local_agent(tmp_path)
    ws = make_child_workspace(account_root)
    kit_dir = tmp_path / "MyAgents" / ".cinna-kit"
    kit_dir.mkdir(parents=True)
    (kit_dir / "layout.json").write_text(
        json.dumps({"cloud_import_excludes": ["docs/", ".venv/", "**/*.pyc"]})
    )
    wire_client(mock_client_cls)
    wire_sync(mock_sync)
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["agent", "import", str(src), "--yes"])
    assert result.exit_code == 0, result.output

    dest = ws / "workspace"
    assert not (dest / "docs").exists()  # the contract's exclusion applied
    assert (dest / "AGENTS.md").is_file()  # this contract did not exclude it
    assert not (dest / "credentials").exists()  # mandatory exclusion survives
    assert not (dest / "app-data").exists()
    assert "layout.json" in result.output
    assert "DEGRADED" not in result.output


@patch("cinna.local_import.sync_session")
@patch("cinna.local_import.AccountClient")
def test_kit_json_cloud_import_no_longer_reaches_the_run(
    mock_client_cls, mock_sync, runner, account_root, tmp_path, monkeypatch
):
    """D6 deleted the key; reading it back would be a second authority."""
    src = make_local_agent(tmp_path)
    ws = make_child_workspace(account_root)
    kit_dir = tmp_path / "MyAgents" / ".cinna-kit"
    kit_dir.mkdir(parents=True)
    (kit_dir / "kit.json").write_text(
        json.dumps({"cloud_import": {"exclude": ["docs/"]}})
    )
    wire_client(mock_client_cls)
    wire_sync(mock_sync)
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["agent", "import", str(src), "--yes"])
    assert result.exit_code == 0, result.output

    assert (ws / "workspace" / "docs" / "WORKFLOW_PROMPT.md").is_file()
    assert "DEGRADED" in result.output


@patch("cinna.local_import.sync_session")
@patch("cinna.local_import.AccountClient")
def test_a_missing_contract_prints_a_degradation_not_a_mode(
    mock_client_cls, mock_sync, runner, account_root, tmp_path, monkeypatch
):
    """The line that made this invisible said `built-in default list` — a mode."""
    src = make_local_agent(tmp_path)
    make_child_workspace(account_root)
    wire_client(mock_client_cls)
    wire_sync(mock_sync)
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["agent", "import", str(src), "--yes"])
    assert result.exit_code == 0, result.output
    assert "DEGRADED" in result.output
    assert "built-in default list" not in result.output


@patch("cinna.local_import.sync_session")
@patch("cinna.local_import.AccountClient")
def test_workspace_option_targets_a_user_workspace(
    mock_client_cls, mock_sync, runner, account_root, tmp_path, monkeypatch
):
    src = make_local_agent(tmp_path)
    make_child_workspace(account_root)
    client = wire_client(mock_client_cls)
    client.list_user_workspaces.return_value = {
        "data": [{"id": "ws-1", "name": "Sales"}]
    }
    wire_sync(mock_sync)
    monkeypatch.chdir(account_root)

    result = runner.invoke(
        cli, ["agent", "import", str(src), "--workspace", "Sales", "--yes"]
    )
    assert result.exit_code == 0, result.output
    client.create_agent.assert_called_once_with(
        "Invoice Watcher", MANIFEST["description"], user_workspace_id="ws-1"
    )
    assert client.create_credential.call_args.kwargs["user_workspace_id"] == "ws-1"


@patch("cinna.local_import.sync_session")
@patch("cinna.local_import.AccountClient")
def test_name_override_is_used_for_the_new_agent(
    mock_client_cls, mock_sync, runner, account_root, tmp_path, monkeypatch
):
    src = make_local_agent(tmp_path)
    make_child_workspace(account_root)
    client = wire_client(mock_client_cls)
    wire_sync(mock_sync)
    monkeypatch.chdir(account_root)

    result = runner.invoke(
        cli, ["agent", "import", str(src), "--name", "Invoices (prod)", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert client.create_agent.call_args.args[0] == "Invoices (prod)"


@patch("cinna.local_import.sync_session")
@patch("cinna.local_import.AccountClient")
def test_platform_error_surfaces_verbatim(
    mock_client_cls, mock_sync, runner, account_root, tmp_path, monkeypatch
):
    src = make_local_agent(tmp_path)
    make_child_workspace(account_root)
    client = wire_client(mock_client_cls)
    client.create_agent.side_effect = PlatformError(
        403, "Creating agents requires the agent-developer role"
    )
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["agent", "import", str(src), "--yes"])
    assert result.exit_code != 0
    assert "agent-developer role" in result.output + result.stderr


def test_import_outside_an_account_workspace(runner, tmp_path, monkeypatch):
    src = make_local_agent(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.chdir(outside)

    result = runner.invoke(cli, ["agent", "import", str(src), "--yes"])
    assert result.exit_code != 0
    assert "account workspace" in (result.output + result.stderr)


@patch("cinna.local_import.AccountClient")
def test_missing_manifest_is_refused(
    mock_client_cls, runner, account_root, tmp_path, monkeypatch
):
    plain = tmp_path / "not-an-agent"
    plain.mkdir()
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["agent", "import", str(plain), "--yes"])
    assert result.exit_code != 0
    assert "cinna-agent.json" in result.output + result.stderr
    mock_client_cls.assert_not_called()


# --- the publication ledger (handover §4) ----------------------------------


@patch("cinna.local_import.sync_session")
@patch("cinna.local_import.AccountClient")
def test_content_hash_is_withheld_when_the_contract_cannot_be_evaluated(
    mock_client_cls, mock_sync, runner, account_root, tmp_path, monkeypatch
):
    """Unevaluable ⇒ refuse to emit a content_hash at all.

    "But the fallback is the same list" is a claim about *this* build's
    contract, not about the one in the folder — which is the only one the other
    host is reading.
    """
    src = make_local_agent(tmp_path)  # no .cinna-kit/ at all
    make_child_workspace(account_root)
    wire_client(mock_client_cls)
    wire_sync(mock_sync)
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["agent", "import", str(src), "--yes"])
    assert result.exit_code == 0, result.output

    (entry,) = json.loads((src / "publications.json").read_text())["publications"]
    assert "content_hash" not in entry
    assert "WITHHELD" in result.output


@patch("cinna.local_import.sync_session")
@patch("cinna.local_import.AccountClient")
def test_a_withheld_secret_does_not_move_the_content_hash(
    mock_client_cls, mock_sync, runner, account_root, tmp_path, monkeypatch
):
    """The secret rules run over the same file set that is hashed.

    A path withheld from the upload but counted in the hash makes the hash move
    for a change that can never be published — unpublished changes forever.
    """
    make_kit(tmp_path)
    src = make_local_agent(tmp_path)
    (src / ".env.prod").write_text("TOKEN=one\n")
    make_child_workspace(account_root)
    wire_client(mock_client_cls)
    wire_sync(mock_sync)
    monkeypatch.chdir(account_root)

    assert runner.invoke(cli, ["agent", "import", str(src), "--yes"]).exit_code == 0
    (first,) = json.loads((src / "publications.json").read_text())["publications"]

    (src / ".env.prod").write_text("TOKEN=two\n")
    result = runner.invoke(cli, ["agent", "import", str(src), "--update", "--yes"])
    assert result.exit_code == 0, result.output
    (second,) = json.loads((src / "publications.json").read_text())["publications"]
    assert second["content_hash"] == first["content_hash"]

    (src / "docs" / "WORKFLOW_PROMPT.md").write_text("changed\n")
    assert (
        runner.invoke(cli, ["agent", "import", str(src), "--update", "--yes"]).exit_code
        == 0
    )
    (third,) = json.loads((src / "publications.json").read_text())["publications"]
    assert third["content_hash"] != first["content_hash"]


@patch("cinna.local_import.sync_session")
@patch("cinna.local_import.AccountClient")
def test_a_legacy_cloud_block_migrates_into_the_ledger(
    mock_client_cls, mock_sync, runner, account_root, tmp_path, monkeypatch
):
    """Migration happens at write time, and drops the key it moved."""
    make_kit(tmp_path)
    src = make_local_agent(
        tmp_path,
        cloud={
            "platform_url": "https://other.example.com",
            "agent_id": "agent-old",
            "imported_at": "2026-09-01T10:00:00+00:00",
        },
    )
    make_child_workspace(account_root)
    wire_client(mock_client_cls)
    wire_sync(mock_sync)
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["agent", "import", str(src), "--yes"])
    assert result.exit_code == 0, result.output

    manifest = json.loads((src / "cinna-agent.json").read_text())
    assert "cloud" not in manifest  # the source may go, the destination is reached
    entries = json.loads((src / "publications.json").read_text())["publications"]
    by_url = {e["platform_url"]: e for e in entries}
    assert by_url["https://other.example.com"]["agent_id"] == "agent-old"
    assert by_url["https://platform.example.com"]["agent_id"] == AGENT_ID


@patch("cinna.local_import.sync_session")
@patch("cinna.local_import.AccountClient")
def test_an_unplaceable_cloud_block_is_left_in_the_manifest(
    mock_client_cls, mock_sync, runner, account_root, tmp_path, monkeypatch
):
    """A destination has to be reached before the source may be removed.

    A `cloud` block with a real platform_url and no agent_id cannot become a
    valid ledger entry, and deleting it would make the platform_url record
    simply cease to exist.
    """
    make_kit(tmp_path)
    src = make_local_agent(
        tmp_path,
        cloud={"platform_url": "https://other.example.com", "agent_id": None},
    )
    make_child_workspace(account_root)
    wire_client(mock_client_cls)
    wire_sync(mock_sync)
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["agent", "import", str(src), "--yes"])
    assert result.exit_code == 0, result.output

    manifest = json.loads((src / "cinna-agent.json").read_text())
    assert manifest["cloud"]["platform_url"] == "https://other.example.com"
    assert "left in" in result.output


@patch("cinna.local_import.sync_session")
@patch("cinna.local_import.AccountClient")
def test_an_unreadable_ledger_refuses_loudly(
    mock_client_cls, mock_sync, runner, account_root, tmp_path, monkeypatch
):
    """Never overwritten with what we managed to migrate."""
    make_kit(tmp_path)
    src = make_local_agent(tmp_path)
    (src / "publications.json").write_text("{ not json")
    make_child_workspace(account_root)
    wire_client(mock_client_cls)
    wire_sync(mock_sync)
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["agent", "import", str(src), "--yes"])
    assert result.exit_code != 0
    assert "publication ledger" in result.output + result.stderr
    assert (src / "publications.json").read_text() == "{ not json"


# --- --update resolves in the ledger, by platform_url (handover §5) ---------


@patch("cinna.local_import.sync_session")
@patch("cinna.local_import.AccountClient")
def test_update_resolves_the_entry_by_platform_url(
    mock_client_cls, mock_sync, runner, account_root, tmp_path, monkeypatch
):
    """Not by workspace, and not by position in the array.

    The desktop publishes through the account API and records no workspace, so
    an entry with `workspace` absent must still resolve.
    """
    make_kit(tmp_path)
    src = make_local_agent(tmp_path)
    (src / "publications.json").write_text(
        json.dumps(
            {
                "publications": [
                    {"platform_url": "https://elsewhere.example.com", "agent_id": "x"},
                    {"platform_url": "https://platform.example.com", "agent_id": AGENT_ID},
                ]
            }
        )
    )
    make_child_workspace(account_root)
    client = wire_client(mock_client_cls)
    wire_sync(mock_sync)
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["agent", "import", str(src), "--update", "--yes"])
    assert result.exit_code == 0, result.output

    client.create_agent.assert_not_called()
    entries = json.loads((src / "publications.json").read_text())["publications"]
    assert [e["platform_url"] for e in entries] == [
        "https://elsewhere.example.com",
        "https://platform.example.com",
    ]
    assert entries[0] == {"platform_url": "https://elsewhere.example.com", "agent_id": "x"}


@patch("cinna.local_import.sync_session")
@patch("cinna.local_import.AccountClient")
def test_a_ledger_entry_for_another_instance_does_not_block_a_first_import(
    mock_client_cls, mock_sync, runner, account_root, tmp_path, monkeypatch
):
    make_kit(tmp_path)
    src = make_local_agent(tmp_path)
    (src / "publications.json").write_text(
        json.dumps(
            {
                "publications": [
                    {"platform_url": "https://elsewhere.example.com", "agent_id": "x"}
                ]
            }
        )
    )
    make_child_workspace(account_root)
    client = wire_client(mock_client_cls)
    wire_sync(mock_sync)
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["agent", "import", str(src), "--yes"])
    assert result.exit_code == 0, result.output
    client.create_agent.assert_called_once()
    entries = json.loads((src / "publications.json").read_text())["publications"]
    assert len(entries) == 2


@patch("cinna.local_import.sync_session")
@patch("cinna.local_import.AccountClient")
def test_the_deprecated_cloud_read_still_resolves_an_old_folder(
    mock_client_cls, mock_sync, runner, account_root, tmp_path, monkeypatch
):
    """§8: deprecated is not unused.

    A folder imported by an older cinna-cli carries only the `cloud` stamp.
    Dropping the read would make its next `--update` create a *second* agent.
    """
    make_kit(tmp_path)
    src = make_local_agent(
        tmp_path,
        cloud={
            "platform_url": "https://platform.example.com",
            "agent_id": AGENT_ID,
            "imported_at": "2026-09-01T10:00:00+00:00",
        },
    )
    make_child_workspace(account_root)
    client = wire_client(mock_client_cls)
    wire_sync(mock_sync)
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["agent", "import", str(src), "--update", "--yes"])
    assert result.exit_code == 0, result.output
    client.create_agent.assert_not_called()
    # ...and after this write the folder is on the ledger, so the next run does
    # not need the deprecated read at all.
    manifest = json.loads((src / "cinna-agent.json").read_text())
    assert "cloud" not in manifest
    (entry,) = json.loads((src / "publications.json").read_text())["publications"]
    assert entry["agent_id"] == AGENT_ID


@patch("cinna.local_import.sync_session")
@patch("cinna.local_import.AccountClient")
def test_the_recorded_hash_matches_a_rescan_of_the_folder(
    mock_client_cls, mock_sync, runner, account_root, tmp_path, monkeypatch
):
    """No phantom drift the instant the publish succeeds.

    The manifest is a member of the hashed tree, so a manifest write that
    happened *after* the hash was computed would leave the folder reading
    "1 unpublished change" immediately — the defect the sibling-file ruling
    removed, reintroduced one step later. The manifest is settled first.
    """
    make_kit(tmp_path)
    src = make_local_agent(tmp_path)  # a non-canonically serialised manifest
    make_child_workspace(account_root)
    wire_client(mock_client_cls)
    wire_sync(mock_sync)
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["agent", "import", str(src), "--yes"])
    assert result.exit_code == 0, result.output
    assert "Normalized" in result.output  # the one-time move, said out loud

    (entry,) = json.loads((src / "publications.json").read_text())["publications"]
    contract = load_export_contract(src)
    rescan = plan_copy(src, contract.patterns, contract.secret_rules)
    assert entry["content_hash"] == kit_contract.content_hash(src, rescan)


def test_content_hash_line_format_matches_the_desktop(tmp_path):
    """`<relpath>\\0<hexdigest>\\n` per file, in UTF-16 order, one sha256."""
    import hashlib

    agent = tmp_path / "a"
    agent.mkdir()
    (agent / "b.txt").write_bytes(b"two\n")
    (agent / "a.txt").write_bytes(b"one\n")

    expected = hashlib.sha256()
    for rel, body in (("a.txt", b"one\n"), ("b.txt", b"two\n")):
        expected.update(f"{rel}\0{hashlib.sha256(body).hexdigest()}\n".encode())
    assert kit_contract.content_hash(agent, ["b.txt", "a.txt"]) == (
        f"sha256:{expected.hexdigest()}"
    )


def test_content_hash_refuses_a_file_it_could_not_read(tmp_path):
    agent = tmp_path / "a"
    agent.mkdir()
    (agent / "a.txt").write_bytes(b"one\n")
    (agent / "a.txt").chmod(0o000)
    try:
        with pytest.raises(kit_contract.UnhashableTree):
            kit_contract.content_hash(agent, ["a.txt"])
    finally:
        (agent / "a.txt").chmod(0o644)


# --- the contract-version gate (handover §6) -------------------------------


@patch("cinna.local_import.sync_session")
@patch("cinna.local_import.AccountClient")
def test_a_newer_major_contract_is_refused(
    mock_client_cls, mock_sync, runner, account_root, tmp_path, monkeypatch
):
    make_kit(tmp_path)
    src = make_local_agent(tmp_path, contract_version="2.0.0")
    make_child_workspace(account_root)
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["agent", "import", str(src), "--yes"])
    assert result.exit_code != 0
    assert "2.x" in result.output + result.stderr
    mock_client_cls.assert_not_called()
    assert not (src / "publications.json").exists()


@patch("cinna.local_import.sync_session")
@patch("cinna.local_import.AccountClient")
def test_a_same_major_contract_passes_silently(
    mock_client_cls, mock_sync, runner, account_root, tmp_path, monkeypatch
):
    """A minor contract change reaches a non-adopting reader as silence.

    That is the gate's shape, not an oversight: anything an old reader must
    notice needs a major bump.
    """
    make_kit(tmp_path)
    src = make_local_agent(tmp_path, contract_version="1.4.2")
    make_child_workspace(account_root)
    wire_client(mock_client_cls)
    wire_sync(mock_sync)
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["agent", "import", str(src), "--yes"])
    assert result.exit_code == 0, result.output
    assert "contract 1.4.2 runs on contract 1.0.0" in result.output
    (entry,) = json.loads((src / "publications.json").read_text())["publications"]
    assert entry["contract_version"] == "1.4.2"


@patch("cinna.local_import.sync_session")
@patch("cinna.local_import.AccountClient")
def test_a_folder_with_no_contract_version_warns_and_imports(
    mock_client_cls, mock_sync, runner, account_root, tmp_path, monkeypatch
):
    """Every folder created before contract 1.0.0 is in this state; refusing
    them would break the users this command exists for."""
    make_kit(tmp_path)
    src = make_local_agent(tmp_path)  # the fixture records no contract_version
    make_child_workspace(account_root)
    wire_client(mock_client_cls)
    wire_sync(mock_sync)
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["agent", "import", str(src), "--yes"])
    assert result.exit_code == 0, result.output
    assert "re-stamped" in result.output
    # ...and the entry then records the contract the kit beside it implements.
    (entry,) = json.loads((src / "publications.json").read_text())["publications"]
    assert entry["contract_version"] == "1.0.0"


@patch("cinna.local_import.sync_session")
@patch("cinna.local_import.AccountClient")
def test_workspace_is_omitted_when_it_cannot_be_placed(
    mock_client_cls, mock_sync, runner, account_root, tmp_path, monkeypatch
):
    """The contract tolerates an absent `workspace`, and an absolute local path
    in the user's own record is worse than nothing."""
    src = make_local_agent(tmp_path)  # no .cinna-kit/, so no workshop root
    make_child_workspace(account_root)
    wire_client(mock_client_cls)
    wire_sync(mock_sync)
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["agent", "import", str(src), "--yes"])
    assert result.exit_code == 0, result.output
    (entry,) = json.loads((src / "publications.json").read_text())["publications"]
    assert entry["workspace"] is None


@pytest.mark.skipif(
    not os.environ.get("CINNA_CORE_PUBLICATIONS_SCHEMA"),
    reason=(
        "set CINNA_CORE_PUBLICATIONS_SCHEMA to a cinna-core "
        "publications.schema.json to validate the written ledger against it"
    ),
)
@patch("cinna.local_import.sync_session")
@patch("cinna.local_import.AccountClient")
def test_the_written_ledger_validates_against_the_real_schema(
    mock_client_cls, mock_sync, runner, account_root, tmp_path, monkeypatch
):
    """Opt-in cross-repo check: what we write is what the contract declares."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(Path(os.environ["CINNA_CORE_PUBLICATIONS_SCHEMA"]).read_text())

    make_kit(tmp_path)
    src = make_local_agent(
        tmp_path,
        cloud={
            "platform_url": "https://legacy.example.com",
            "agent_id": "agent-legacy-7",
            "imported_at": "2026-01-02T03:04:05+00:00",
        },
    )
    make_child_workspace(account_root)
    wire_client(mock_client_cls)
    wire_sync(mock_sync)
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["agent", "import", str(src), "--yes"])
    assert result.exit_code == 0, result.output
    jsonschema.validate(json.loads((src / "publications.json").read_text()), schema)
