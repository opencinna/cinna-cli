"""Tests for the ``cinna doctor`` diagnosis + repair flow."""

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cinna.config import (
    CinnaConfig,
    list_agent_registry,
    lookup_agent_registry,
    save_config,
    upsert_agent_registry,
)
from cinna.doctor import diagnose
from cinna.main import cli
from cinna.sync_session import session_name


@pytest.fixture
def runner():
    return CliRunner()


def _register_intact(tmp_path: Path, agent_id: str, name: str = "Agent") -> Path:
    """Create a real workspace (with .cinna/config.json) + a registry entry."""
    root = tmp_path / agent_id
    cfg = CinnaConfig(
        platform_url="https://platform.example.com",
        cli_token="tok",
        agent_id=agent_id,
        agent_name=name,
        environment_id="env",
        template="python-basic",
    )
    save_config(cfg, root)
    upsert_agent_registry(agent_id, cfg.platform_url, cfg.cli_token, root)
    return root


def _session(agent_id: str, status: str, beta_connected: bool, last_error=None):
    return {
        "name": session_name(agent_id),
        "status": status,
        "paused": False,
        "lastError": last_error,
        "alpha": {"connected": True, "path": f"/ws/{agent_id}/workspace"},
        "beta": {"connected": beta_connected},
    }


# ── per-category diagnosis ───────────────────────────────────────────────────


@patch("cinna.main._probe_token_statuses", return_value={})
@patch("cinna.doctor.sync_session.list_all_sessions", return_value=[])
def test_stale_folder_when_workspace_missing(_sessions, _probe, tmp_path):
    upsert_agent_registry(
        "gone-1", "https://p", "tok", tmp_path / "does-not-exist"
    )
    findings = diagnose()
    assert [f.category for f in findings] == ["stale_folder"]

    findings[0].apply()
    assert lookup_agent_registry("gone-1") is None


@patch("cinna.main._probe_token_statuses")
@patch("cinna.doctor.sync_session.list_all_sessions")
def test_healthy_intact_session_yields_no_findings(_sessions, _probe, tmp_path):
    _register_intact(tmp_path, "agentaaa")
    _sessions.return_value = [_session("agentaaa", "watching", beta_connected=True)]
    _probe.return_value = {"agentaaa": "valid"}
    assert diagnose() == []


@patch("cinna.main._probe_token_statuses")
@patch("cinna.doctor.sync_session.list_all_sessions")
def test_dead_remote_session_is_flagged(_sessions, _probe, tmp_path):
    _register_intact(tmp_path, "agentbbb")
    _sessions.return_value = [
        _session("agentbbb", "connecting-beta", beta_connected=False,
                 last_error="beta polling error")
    ]
    _probe.return_value = {"agentbbb": "valid"}
    findings = diagnose()
    assert [f.category for f in findings] == ["dead_remote"]


@patch("cinna.main._probe_token_statuses")
@patch("cinna.doctor.sync_session.list_all_sessions")
def test_zombie_session_is_flagged(_sessions, _probe, tmp_path):
    _register_intact(tmp_path, "agentccc")
    _sessions.return_value = [
        _session("agentccc", "halted-on-root-deletion", beta_connected=True)
    ]
    _probe.return_value = {"agentccc": "valid"}
    findings = diagnose()
    assert [f.category for f in findings] == ["zombie_session"]


@patch("cinna.main._probe_token_statuses", return_value={})
@patch("cinna.doctor.sync_session.list_all_sessions")
def test_orphan_session_with_no_registry_entry(_sessions, _probe, tmp_path):
    _sessions.return_value = [
        _session("orphan99", "watching", beta_connected=True)
    ]
    findings = diagnose()
    assert [f.category for f in findings] == ["orphan_session"]


@patch("cinna.doctor._probe_account_token", return_value="valid")
@patch("cinna.doctor._account_root_for")
@patch("cinna.main._probe_token_statuses")
@patch("cinna.doctor.sync_session.list_all_sessions", return_value=[])
def test_expired_token_remint_vs_report(_sessions, _probe, _acct, _acct_tok, tmp_path):
    root = _register_intact(tmp_path, "agentddd")
    _probe.return_value = {"agentddd": "expired"}

    # Account workspace present + its token valid → re-mint the sub-agent token.
    _acct.return_value = root.parent
    assert [f.category for f in diagnose()] == ["token_remint"]

    _acct.return_value = None  # standalone → report only
    reports = diagnose()
    assert [f.category for f in reports] == ["token_report"]
    assert reports[0].apply is None


@patch("cinna.doctor._probe_account_token", return_value="expired")
@patch("cinna.doctor._account_root_for")
@patch("cinna.main._probe_token_statuses")
@patch("cinna.doctor.sync_session.list_all_sessions", return_value=[])
def test_expired_account_token_groups_subagents(
    _sessions, _probe, _acct, _acct_tok, tmp_path
):
    # Two account-managed agents whose account token has itself expired: no
    # per-agent re-mint (it would 401) — one grouped, report-only finding.
    account_root = tmp_path / "acct"
    _register_intact(tmp_path, "subaaa", name="Sub A")
    _register_intact(tmp_path, "subbbb", name="Sub B")
    _probe.return_value = {"subaaa": "expired", "subbbb": "expired"}
    _acct.return_value = account_root

    findings = diagnose()
    assert [f.category for f in findings] == ["account_token_expired"]
    finding = findings[0]
    assert finding.apply is None  # manual — doctor won't auto-fix it
    assert "Sub A" in finding.detail and "Sub B" in finding.detail
    assert "cinna login" in finding.fix
    # The account token was probed once, not once per sub-agent.
    assert _acct_tok.call_count == 1


@patch("cinna.main._probe_token_statuses")
@patch("cinna.doctor.sync_session.list_all_sessions")
def test_stale_folder_terminates_its_session(_sessions, _probe, tmp_path):
    # Registry entry points at a missing folder but a session lingers.
    upsert_agent_registry("ghost123", "https://p", "tok", tmp_path / "missing")
    _sessions.return_value = [
        _session("ghost123", "halted-on-root-deletion", beta_connected=True)
    ]
    _probe.return_value = {}
    findings = diagnose()
    assert [f.category for f in findings] == ["stale_folder"]
    assert "terminate session" in findings[0].fix

    with patch(
        "cinna.doctor.sync_session.terminate_named", return_value=True
    ) as term:
        findings[0].apply()
    term.assert_called_once()
    assert lookup_agent_registry("ghost123") is None


# ── command surface ──────────────────────────────────────────────────────────


@patch("cinna.doctor.diagnose", return_value=[])
def test_doctor_clean_machine(_diag, runner):
    result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 0
    assert "healthy" in result.output


def test_doctor_dry_run_makes_no_changes(runner, tmp_path):
    upsert_agent_registry("gone-2", "https://p", "tok", tmp_path / "nope")
    with patch("cinna.doctor.sync_session.list_all_sessions", return_value=[]), \
         patch("cinna.main._probe_token_statuses", return_value={}):
        result = runner.invoke(cli, ["doctor", "--dry-run"])
    assert result.exit_code == 0
    assert "Dry run" in result.output
    # Registry entry survives a dry run.
    assert lookup_agent_registry("gone-2") is not None


def test_doctor_yes_applies_without_prompt(runner, tmp_path):
    upsert_agent_registry("gone-3", "https://p", "tok", tmp_path / "nope")
    with patch("cinna.doctor.sync_session.list_all_sessions", return_value=[]), \
         patch("cinna.main._probe_token_statuses", return_value={}):
        result = runner.invoke(cli, ["doctor", "--yes"])
    assert result.exit_code == 0
    assert lookup_agent_registry("gone-3") is None
    assert list_agent_registry() == []


def test_doctor_single_confirmation_applies_all(runner, tmp_path):
    # Two actionable findings of different categories → ONE prompt, both applied.
    upsert_agent_registry("gone-4", "https://p", "tok", tmp_path / "nope")
    upsert_agent_registry("gone-5", "https://p", "tok", tmp_path / "nope2")
    with patch("cinna.doctor.sync_session.list_all_sessions", return_value=[]), \
         patch("cinna.main._probe_token_statuses", return_value={}):
        result = runner.invoke(cli, ["doctor"], input="y\n")
    assert result.exit_code == 0
    # A single combined prompt, not one-per-category.
    assert "Apply 2 fix(es)?" in result.output
    assert result.output.count("Apply ") == 1
    assert list_agent_registry() == []


def test_doctor_standalone_token_is_manual_not_applied(runner, tmp_path):
    # An expired standalone token is reported but never auto-fixed: no prompt,
    # registry untouched.
    _register_intact(tmp_path, "agenteee")
    with patch("cinna.doctor.sync_session.list_all_sessions", return_value=[]), \
         patch("cinna.main._probe_token_statuses", return_value={"agenteee": "expired"}), \
         patch("cinna.doctor._account_root_for", return_value=None):
        result = runner.invoke(cli, ["doctor", "--yes"])
    assert result.exit_code == 0
    assert "manual action needed" in result.output
    assert "cinna set-token" in result.output
    assert "applied 0 fix(es)" in result.output
    # The registry entry survives (nothing to auto-fix).
    assert lookup_agent_registry("agenteee") is not None


def test_doctor_decline_applies_nothing(runner, tmp_path):
    upsert_agent_registry("gone-6", "https://p", "tok", tmp_path / "nope")
    with patch("cinna.doctor.sync_session.list_all_sessions", return_value=[]), \
         patch("cinna.main._probe_token_statuses", return_value={}):
        result = runner.invoke(cli, ["doctor"], input="n\n")
    assert result.exit_code == 0
    assert "No fixes applied." in result.output
    assert lookup_agent_registry("gone-6") is not None
