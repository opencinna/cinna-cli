"""Tests for the ``cinna doctor`` diagnosis + repair flow."""

from pathlib import Path
from unittest.mock import ANY, patch

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


@patch("cinna.doctor.sync_session.list_all_sessions", return_value=[])
@patch("cinna.doctor.diagnose", return_value=[])
def test_doctor_clean_machine(_diag, _sessions, runner):
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


def test_doctor_stalled_step_applies_all_in_one_prompt(runner, tmp_path):
    # Two stalled findings → ONE "delete stalled sessions" prompt clears both.
    upsert_agent_registry("gone-4", "https://p", "tok", tmp_path / "nope")
    upsert_agent_registry("gone-5", "https://p", "tok", tmp_path / "nope2")
    with patch("cinna.doctor.sync_session.list_all_sessions", return_value=[]), \
         patch("cinna.main._probe_token_statuses", return_value={}):
        result = runner.invoke(cli, ["doctor"], input="y\n")
    assert result.exit_code == 0
    assert "Delete 2 stalled session(s)?" in result.output
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
    assert "No stalled sessions deleted." in result.output
    assert lookup_agent_registry("gone-6") is not None


# ── final step: sweep remaining live sessions ─────────────────────────────────


@patch("cinna.doctor.sync_session.terminate_named", return_value=True)
@patch("cinna.main._probe_token_statuses")
@patch("cinna.doctor.sync_session.list_all_sessions")
def test_sweep_terminates_live_sessions_with_yes(
    _sessions, _probe, _term, runner, tmp_path
):
    # A healthy machine (intact workspace, valid token, watching session) yields
    # no findings — but doctor still offers to clear the leftover session, and
    # --yes accepts the (default-Yes) prompt.
    _register_intact(tmp_path, "agentaaa")
    _sessions.return_value = [_session("agentaaa", "watching", beta_connected=True)]
    _probe.return_value = {"agentaaa": "valid"}

    result = runner.invoke(cli, ["doctor", "--yes"])
    assert result.exit_code == 0
    assert "No problems found" in result.output  # no findings…
    assert "Active Mutagen sessions" in result.output  # …but the sweep still ran
    _term.assert_called_once_with(session_name("agentaaa"), ANY)
    assert "terminated 1 session" in result.output


@patch("cinna.doctor.sync_session.terminate_named", return_value=True)
@patch("cinna.main._probe_token_statuses")
@patch("cinna.doctor.sync_session.list_all_sessions")
def test_sweep_prompt_defaults_to_yes(_sessions, _probe, _term, runner, tmp_path):
    _register_intact(tmp_path, "agentaaa")
    _sessions.return_value = [_session("agentaaa", "watching", beta_connected=True)]
    _probe.return_value = {"agentaaa": "valid"}

    # Bare Enter accepts the default (Yes).
    result = runner.invoke(cli, ["doctor"], input="\n")
    assert result.exit_code == 0
    _term.assert_called_once()


@patch("cinna.doctor.sync_session.terminate_named", return_value=True)
@patch("cinna.main._probe_token_statuses")
@patch("cinna.doctor.sync_session.list_all_sessions")
def test_sweep_can_be_declined(_sessions, _probe, _term, runner, tmp_path):
    _register_intact(tmp_path, "agentaaa")
    _sessions.return_value = [_session("agentaaa", "watching", beta_connected=True)]
    _probe.return_value = {"agentaaa": "valid"}

    result = runner.invoke(cli, ["doctor"], input="n\n")
    assert result.exit_code == 0
    assert "Sessions left running." in result.output
    _term.assert_not_called()


@patch("cinna.doctor.sync_session.terminate_named", return_value=True)
@patch("cinna.main._probe_token_statuses")
@patch("cinna.doctor.sync_session.list_all_sessions")
def test_sweep_lists_but_does_not_terminate_on_dry_run(
    _sessions, _probe, _term, runner, tmp_path
):
    _register_intact(tmp_path, "agentaaa")
    _sessions.return_value = [_session("agentaaa", "watching", beta_connected=True)]
    _probe.return_value = {"agentaaa": "valid"}

    result = runner.invoke(cli, ["doctor", "--dry-run"])
    assert result.exit_code == 0
    assert "Active Mutagen sessions" in result.output
    _term.assert_not_called()


@patch("cinna.doctor._probe_account_token", return_value="valid")
@patch("cinna.doctor._account_root_for")
@patch("cinna.main._probe_token_statuses")
@patch("cinna.doctor.sync_session.list_all_sessions")
def test_steps_reported_in_order_stalled_active_tokens(
    _sessions, _probe, _acct, _acct_tok, runner, tmp_path
):
    # One of each kind: a dead (stalled) session, a healthy (active) session,
    # and an account-managed expired token. The report must order the sections
    # stalled → active → tokens, matching the prompt order.
    _register_intact(tmp_path, "agentaaa", name="Active One")
    bbb = _register_intact(tmp_path, "agentbbb", name="Dead One")
    ddd = _register_intact(tmp_path, "agentddd", name="Token One")
    _sessions.return_value = [
        _session("agentaaa", "watching", beta_connected=True),
        _session("agentbbb", "connecting-beta", beta_connected=False,
                 last_error="beta polling error"),
    ]
    _probe.return_value = {
        "agentaaa": "valid", "agentbbb": "valid", "agentddd": "expired",
    }
    _acct.return_value = ddd.parent  # account-managed → re-mint candidate

    result = runner.invoke(cli, ["doctor", "--dry-run"])
    assert result.exit_code == 0
    out = result.output
    i_stalled = out.index("Stalled sessions")
    i_active = out.index("Active Mutagen sessions")
    i_tokens = out.index("Expired tokens")
    assert i_stalled < i_active < i_tokens
    assert str(bbb)  # registered fixture (kept for symmetry)


@patch("cinna.doctor.sync_session.list_all_sessions")
def test_collect_cinna_sessions_tags_agent_and_folder(_sessions, tmp_path):
    # The active-session inventory resolves each live cinna-* session to the
    # agent name and workspace folder from the registry.
    from cinna.doctor import _collect_cinna_sessions, _daemon_config

    root = _register_intact(tmp_path, "agentaaa", name="My Agent")
    _sessions.return_value = [_session("agentaaa", "watching", beta_connected=True)]
    entries = list_agent_registry()

    infos = _collect_cinna_sessions(entries, _daemon_config(entries))
    assert len(infos) == 1
    assert infos[0].name == session_name("agentaaa")
    assert infos[0].agent == "My Agent"
    assert infos[0].folder == str(root)


@patch("cinna.doctor.sync_session.terminate_named", return_value=True)
@patch("cinna.main._probe_token_statuses", return_value={})
@patch("cinna.doctor.sync_session.list_all_sessions")
def test_sweep_ignores_non_cinna_sessions(
    _sessions, _probe, _term, runner, tmp_path
):
    # A foreign session from another Mutagen consumer is never touched (the
    # daemon is shared), so the sweep finds nothing to clear.
    _sessions.return_value = [
        {"name": "some-other-tool", "status": "watching",
         "paused": False, "alpha": {"connected": True}, "beta": {"connected": True}}
    ]
    result = runner.invoke(cli, ["doctor", "--yes"])
    assert result.exit_code == 0
    assert "Active Mutagen sessions" not in result.output
    _term.assert_not_called()
