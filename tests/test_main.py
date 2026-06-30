"""CLI command integration tests."""

import pytest
from unittest.mock import patch
from click.testing import CliRunner

from cinna.main import cli


@pytest.fixture
def runner():
    return CliRunner()


def test_version(runner):
    from cinna import __version__

    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    # __version__ is derived from package metadata, not hard-coded — assert the
    # CLI reports whatever the installed package version is.
    assert __version__ in result.output


def test_status_no_workspace(runner):
    result = runner.invoke(cli, ["status"], catch_exceptions=False)
    # Should fail because we're not in a workspace
    assert result.exit_code != 0


@patch("cinna.main._probe_token_statuses")
@patch("cinna.main.sync_session.status")
@patch("cinna.main.find_workspace_root")
@patch("cinna.main.load_config")
def test_status_command(mock_load, mock_find, mock_status, mock_probe, runner, workspace_root, sample_config):
    from cinna.sync_session import SyncStatus

    mock_find.return_value = workspace_root
    mock_load.return_value = sample_config
    mock_status.return_value = SyncStatus(session_name="cinna-abc", state="connected")
    mock_probe.return_value = {sample_config.agent_id: "valid"}

    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0
    assert "test-agent" in result.output
    assert "connected" in result.output
    assert "valid token" in result.output
    # Probe was handed the current workspace's platform_url + cli_token.
    (entries,), _ = mock_probe.call_args
    assert entries == [
        {
            "agent_id": sample_config.agent_id,
            "platform_url": sample_config.platform_url,
            "cli_token": sample_config.cli_token,
        }
    ]


@patch("cinna.account.list_child_workspaces")
@patch("cinna.account.probe_account_token")
@patch("cinna.account.load_account_config")
@patch("cinna.account.find_account_root")
@patch("cinna.main.find_workspace_root")
def test_status_falls_back_to_account_workspace(
    mock_find_ws, mock_find_acct, mock_load_acct, mock_probe, mock_children, runner, tmp_path
):
    """`cinna status` inside an account workspace (only account.json, no
    config.json) shows the account session instead of erroring, and nudges
    re-auth when the token has expired."""
    from cinna.errors import ConfigNotFoundError
    from cinna.account import AccountConfig

    mock_find_ws.side_effect = ConfigNotFoundError()
    mock_find_acct.return_value = tmp_path
    mock_load_acct.return_value = AccountConfig(
        platform_url="https://wr-demo.example.com",
        frontend_url="https://wr-demo.example.com",
        account_token="tok",
        machine_name="laptop-3",
    )
    mock_probe.return_value = "expired"
    mock_children.return_value = []

    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0
    assert "Account workspace" in result.output
    assert "https://wr-demo.example.com" in result.output
    assert "expired token" in result.output
    assert "cinna login" in result.output


@patch("cinna.bootstrap.httpx.post")
def test_set_token_replaces_cli_token(mock_post, runner, tmp_path, monkeypatch, sample_config):
    """`cinna set-token` exchanges a fresh setup token and updates config +
    registry in place without touching workspace files."""
    import cinna.config as config_module
    from cinna.config import (
        agents_registry_path,
        save_config,
        lookup_agent_registry,
        load_config,
        upsert_agent_registry,
    )

    monkeypatch.setattr(config_module, "GLOBAL_STATE_DIR", tmp_path / "reg")

    ws = tmp_path / "ws"
    ws.mkdir()
    save_config(sample_config, ws)
    upsert_agent_registry(
        sample_config.agent_id,
        sample_config.platform_url,
        sample_config.cli_token,
        ws,
        frontend_url="https://ui.example.com",
    )
    monkeypatch.chdir(ws)

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "platform_url": sample_config.platform_url,
                "cli_token": "refreshed-token-xyz",
                "frontend_url": "https://ui.example.com",
                "agent": {
                    "id": sample_config.agent_id,
                    "name": sample_config.agent_name,
                    "environment_id": sample_config.environment_id,
                    "template": sample_config.template,
                },
            }

    mock_post.return_value = FakeResponse()

    result = runner.invoke(
        cli,
        ["set-token", "https://platform.example.com/cli-setup/NEWTOKEN", "--name", "laptop"],
    )
    assert result.exit_code == 0, result.output

    reloaded = load_config(ws)
    assert reloaded.cli_token == "refreshed-token-xyz"

    entry = lookup_agent_registry(sample_config.agent_id)
    assert entry is not None
    assert entry["cli_token"] == "refreshed-token-xyz"
    assert agents_registry_path().exists()


@patch("cinna.bootstrap.httpx.post")
def test_set_token_accepts_bare_token_using_config_platform(
    mock_post, runner, tmp_path, monkeypatch, sample_config
):
    """When the user pastes only a raw token, set-token reuses the
    platform_url already stored in .cinna/config.json instead of erroring."""
    import cinna.config as config_module
    from cinna.config import save_config, load_config

    monkeypatch.setattr(config_module, "GLOBAL_STATE_DIR", tmp_path / "reg")
    monkeypatch.delenv("CINNA_PLATFORM_URL", raising=False)

    ws = tmp_path / "ws"
    ws.mkdir()
    save_config(sample_config, ws)
    monkeypatch.chdir(ws)

    captured: dict = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "platform_url": sample_config.platform_url,
                "cli_token": "refreshed-token-xyz",
                "agent": {
                    "id": sample_config.agent_id,
                    "name": sample_config.agent_name,
                    "environment_id": sample_config.environment_id,
                    "template": sample_config.template,
                },
            }

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        return FakeResponse()

    mock_post.side_effect = fake_post

    result = runner.invoke(cli, ["set-token", "BARETOKEN", "--name", "laptop"])
    assert result.exit_code == 0, result.output
    # Exchange hit the platform_url from the existing config (/api is
    # appended because config.platform_url stores the bare host).
    assert captured["url"] == f"{sample_config.platform_url}/api/cli-setup/BARETOKEN"
    assert load_config(ws).cli_token == "refreshed-token-xyz"


@patch("cinna.bootstrap.httpx.post")
def test_set_token_rejects_mismatched_agent(mock_post, runner, tmp_path, monkeypatch, sample_config):
    """If the exchanged token belongs to a different agent, the command aborts
    and the stored token is left untouched."""
    import cinna.config as config_module
    from cinna.config import save_config, load_config

    monkeypatch.setattr(config_module, "GLOBAL_STATE_DIR", tmp_path / "reg")

    ws = tmp_path / "ws"
    ws.mkdir()
    save_config(sample_config, ws)
    monkeypatch.chdir(ws)

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "platform_url": sample_config.platform_url,
                "cli_token": "refreshed-token-xyz",
                "agent": {
                    "id": "different-agent-id",
                    "name": "other",
                    "environment_id": "env-x",
                    "template": "t",
                },
            }

    mock_post.return_value = FakeResponse()

    result = runner.invoke(
        cli,
        ["set-token", "https://platform.example.com/cli-setup/NEWTOKEN", "--name", "laptop"],
    )
    assert result.exit_code != 0
    assert "different agent" in result.output

    reloaded = load_config(ws)
    assert reloaded.cli_token == sample_config.cli_token


@patch("cinna.main._run_remote_exec")
@patch("cinna.main.find_workspace_root")
@patch("cinna.main.load_config")
def test_exec_command(mock_load, mock_find, mock_exec, runner, workspace_root, sample_config):
    mock_find.return_value = workspace_root
    mock_load.return_value = sample_config
    mock_exec.return_value = 0

    result = runner.invoke(cli, ["exec", "python", "scripts/main.py"])
    assert result.exit_code == 0
    mock_exec.assert_called_once_with(
        sample_config, "python scripts/main.py", timeout=1800
    )


@patch("cinna.main._run_remote_exec")
@patch("cinna.main.find_workspace_root")
@patch("cinna.main.load_config")
def test_exec_command_requotes_args(
    mock_load, mock_find, mock_exec, runner, workspace_root, sample_config
):
    """Tokens with spaces / shell metacharacters must be re-quoted so the
    remote shell reconstructs the exact argv the caller intended, rather than
    re-splitting them. Regression for `cinna exec` mangling quoted arguments.
    """
    mock_find.return_value = workspace_root
    mock_load.return_value = sample_config
    mock_exec.return_value = 0

    result = runner.invoke(
        cli,
        ["exec", "python", "-c", "import sys; print(sys.argv)", "a b", '[{"x":"y z"}]'],
    )
    assert result.exit_code == 0
    mock_exec.assert_called_once_with(
        sample_config,
        """python -c 'import sys; print(sys.argv)' 'a b' '[{"x":"y z"}]'""",
        timeout=1800,
    )


@patch("cinna.main.PlatformClient")
def test_run_remote_exec_logs_start_and_stop(mock_client_cls, sample_config, caplog):
    """`cinna exec` must emit start/stop log records — without them this whole
    code path is invisible in cinna.log, which has previously made remote
    failures very hard to debug.
    """
    import logging
    from cinna.main import _run_remote_exec

    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.stream_exec.return_value = iter([
        {"type": "exec_id", "exec_id": "ex-1"},
        {"type": "tool_result_delta", "content": "hi\n", "metadata": {"stream": "stdout"}},
        {"type": "tool_result_delta", "content": "warn\n", "metadata": {"stream": "stderr"}},
        {"type": "done", "exit_code": 0},
    ])

    with caplog.at_level(logging.DEBUG, logger="cinna.exec"):
        exit_code = _run_remote_exec(sample_config, "echo hi")

    assert exit_code == 0
    messages = [r.getMessage() for r in caplog.records if r.name == "cinna.exec"]
    assert any(m.startswith("exec start:") and "echo hi" in m for m in messages)
    assert any("exec_id assigned: ex-1" in m for m in messages)
    assert any(
        m.startswith("exec stop:")
        and "exit_code=0" in m
        and "stdout=3B" in m
        and "stderr=5B" in m
        and "terminal=done" in m
        for m in messages
    )


@patch("cinna.main.sync_session.run_foreground")
@patch("cinna.main.sync_session.start")
@patch("cinna.main.ensure_mutagen_ready")
@patch("cinna.main.PlatformClient")
@patch("cinna.main.find_workspace_root")
@patch("cinna.main.load_config")
def test_dev_starts_session_and_attaches_foreground(
    mock_load, mock_find, mock_client_cls, mock_ensure, mock_start, mock_run_fg,
    runner, workspace_root, sample_config,
):
    from cinna.sync_session import SyncStatus

    mock_find.return_value = workspace_root
    mock_load.return_value = sample_config
    mock_start.return_value = SyncStatus(session_name="cinna-abc", state="connected")
    mock_run_fg.return_value = 0

    result = runner.invoke(cli, ["dev"])
    assert result.exit_code == 0
    mock_start.assert_called_once()
    mock_run_fg.assert_called_once_with(sample_config, workspace_root)


@patch("cinna.main.sync_session.resolve_startup_conflicts_favor_remote")
@patch("cinna.main.sync_session.run_foreground")
@patch("cinna.main.sync_session.start")
@patch("cinna.main.ensure_mutagen_ready")
@patch("cinna.main.PlatformClient")
@patch("cinna.main.find_workspace_root")
@patch("cinna.main.load_config")
def test_dev_does_not_auto_resolve_conflicts(
    mock_load, mock_find, mock_client_cls, mock_ensure, mock_start, mock_run_fg,
    mock_resolve, runner, workspace_root, sample_config,
):
    from cinna.sync_session import SyncStatus

    mock_find.return_value = workspace_root
    mock_load.return_value = sample_config
    mock_start.return_value = SyncStatus(session_name="cinna-abc", state="connected")
    mock_run_fg.return_value = 0

    result = runner.invoke(cli, ["dev"])
    assert result.exit_code == 0
    mock_resolve.assert_not_called()


@patch("cinna.main.sync_session.resolve_startup_conflicts_favor_remote")
@patch("cinna.main.sync_session.run_foreground")
@patch("cinna.main.sync_session.start")
@patch("cinna.main.ensure_mutagen_ready")
@patch("cinna.main.PlatformClient")
@patch("cinna.main.find_workspace_root")
@patch("cinna.main.load_config")
def test_redev_resolves_remote_wins_then_attaches(
    mock_load, mock_find, mock_client_cls, mock_ensure, mock_start, mock_run_fg,
    mock_resolve, runner, workspace_root, sample_config,
):
    from cinna.sync_session import RemoteWinsResult, SyncStatus

    mock_find.return_value = workspace_root
    mock_load.return_value = sample_config
    mock_start.return_value = SyncStatus(session_name="cinna-abc", state="connected")
    mock_resolve.return_value = RemoteWinsResult(
        resolved=["shared.txt"],
        backup_dir=workspace_root / ".cinna" / "sync" / "redev-backup" / "x",
    )
    mock_run_fg.return_value = 0

    result = runner.invoke(cli, ["redev"])
    assert result.exit_code == 0
    mock_resolve.assert_called_once_with(sample_config, workspace_root)
    mock_run_fg.assert_called_once_with(sample_config, workspace_root)
    assert "Resolved 1 conflict(s) in favor of remote" in result.output
    assert "backed up" in result.output


@patch("cinna.main.sync_session.resolve_startup_conflicts_favor_remote")
@patch("cinna.main.sync_session.run_foreground")
@patch("cinna.main.sync_session.start")
@patch("cinna.main.ensure_mutagen_ready")
@patch("cinna.main.PlatformClient")
@patch("cinna.main.find_workspace_root")
@patch("cinna.main.load_config")
def test_redev_warns_about_unresolved_conflicts(
    mock_load, mock_find, mock_client_cls, mock_ensure, mock_start, mock_run_fg,
    mock_resolve, runner, workspace_root, sample_config,
):
    from cinna.sync_session import RemoteWinsResult, SyncStatus

    mock_find.return_value = workspace_root
    mock_load.return_value = sample_config
    mock_start.return_value = SyncStatus(session_name="cinna-abc", state="connected")
    mock_resolve.return_value = RemoteWinsResult(remaining=["stuck.txt"])
    mock_run_fg.return_value = 0

    result = runner.invoke(cli, ["redev"])
    assert result.exit_code == 0
    assert "No conflicts" in result.output
    assert "could not be auto-resolved" in result.output


def test_list_empty_registry(runner, monkeypatch, tmp_path):
    """`cinna list` on a fresh machine announces no agents instead of crashing."""
    import cinna.config as config_module
    monkeypatch.setattr(config_module, "GLOBAL_STATE_DIR", tmp_path / "empty-cinna")
    monkeypatch.setenv("COLUMNS", "240")

    result = runner.invoke(cli, ["list"])
    assert result.exit_code == 0
    assert "No agents registered" in result.output


def test_list_shows_registered_agents(runner, monkeypatch, tmp_path, sample_config):
    """`cinna list` prints agent name, agent ID, frontend link, workspace,
    and sync status for each registered agent."""
    import cinna.config as config_module
    from cinna.config import save_config, upsert_agent_registry

    monkeypatch.setattr(config_module, "GLOBAL_STATE_DIR", tmp_path / "reg")
    monkeypatch.setenv("COLUMNS", "240")

    ws = tmp_path / "my-agent-workspace"
    ws.mkdir()
    save_config(sample_config, ws)
    upsert_agent_registry(
        sample_config.agent_id,
        sample_config.platform_url,
        sample_config.cli_token,
        ws,
        frontend_url="https://ui.example.com",
    )

    # Prevent `cinna list` from spawning mutagen in the test runner.
    monkeypatch.setattr("cinna.main._list_sessions", lambda _cfg: [], raising=False)
    monkeypatch.setattr(
        "cinna.main._probe_token_statuses",
        lambda entries: {e["agent_id"]: "valid" for e in entries},
    )

    result = runner.invoke(cli, ["list"])
    assert result.exit_code == 0
    assert sample_config.agent_name in result.output
    # Agent ID printed in full (table column is wide enough under COLUMNS=240).
    assert sample_config.agent_id in result.output
    # Frontend link built from registered frontend_url.
    assert f"https://ui.example.com/agent/{sample_config.agent_id}" in result.output
    # Token probe result shown alongside sync state.
    assert "valid token" in result.output


def test_list_flags_missing_workspace(runner, monkeypatch, tmp_path, sample_config):
    """Registry entries pointing at deleted directories are surfaced as
    ``missing:`` so the user knows to run cleanup."""
    import cinna.config as config_module
    from cinna.config import upsert_agent_registry

    monkeypatch.setattr(config_module, "GLOBAL_STATE_DIR", tmp_path / "reg")
    monkeypatch.setenv("COLUMNS", "240")

    ghost_ws = tmp_path / "deleted-workspace"
    upsert_agent_registry(
        sample_config.agent_id,
        sample_config.platform_url,
        sample_config.cli_token,
        ghost_ws,
    )

    monkeypatch.setattr(
        "cinna.main._probe_token_statuses",
        lambda entries: {e["agent_id"]: "unreachable" for e in entries},
    )

    result = runner.invoke(cli, ["list"])
    assert result.exit_code == 0
    assert "missing" in result.output
    assert "no connection" in result.output


def test_probe_token_statuses_classifies_responses(monkeypatch):
    """_probe_token_statuses maps backend replies to valid / expired /
    unreachable per agent."""
    from cinna.main import _probe_token_statuses

    class FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code

    calls: dict[str, str] = {}

    def fake_get(url, headers=None, timeout=None, follow_redirects=True):
        token = (headers or {}).get("Authorization", "")
        calls[url] = token
        if "agent-ok" in url:
            return FakeResponse(200)
        if "agent-expired" in url:
            return FakeResponse(401)
        raise RuntimeError("boom")

    import httpx
    monkeypatch.setattr(httpx, "get", fake_get)

    entries = [
        {"agent_id": "agent-ok", "platform_url": "https://p.example", "cli_token": "t1"},
        {"agent_id": "agent-expired", "platform_url": "https://p.example", "cli_token": "t2"},
        {"agent_id": "agent-boom", "platform_url": "https://p.example", "cli_token": "t3"},
        {"agent_id": "agent-naked", "platform_url": "", "cli_token": ""},
    ]
    results = _probe_token_statuses(entries)
    assert results["agent-ok"] == "valid"
    assert results["agent-expired"] == "expired"
    assert results["agent-boom"] == "unreachable"
    # Missing url/token short-circuits without a network call.
    assert results["agent-naked"] == "unreachable"
    assert all(tok.startswith("Bearer ") for tok in calls.values())


def test_sync_group_removed_commands_not_registered(runner):
    """`cinna sync start/stop/pause/resume` were removed in favour of
    `cinna dev`. The sync group should only expose read-only inspectors."""
    for removed in ("start", "stop", "pause", "resume"):
        result = runner.invoke(cli, ["sync", removed])
        assert result.exit_code != 0, f"sync {removed} should no longer exist"

    help_result = runner.invoke(cli, ["sync", "--help"])
    assert help_result.exit_code == 0
    assert "status" in help_result.output
    assert "conflicts" in help_result.output
    for removed in ("start", "stop", "pause", "resume"):
        assert f"  {removed}" not in help_result.output


@patch("cinna.main.sync_session.list_conflicts")
@patch("cinna.main.find_workspace_root")
@patch("cinna.main.load_config")
def test_sync_conflicts_empty(mock_load, mock_find, mock_list, runner, workspace_root, sample_config):
    mock_find.return_value = workspace_root
    mock_load.return_value = sample_config
    mock_list.return_value = []

    result = runner.invoke(cli, ["sync", "conflicts"])
    assert result.exit_code == 0
    assert "No conflicts" in result.output


@patch("cinna.main.sync_session.stop")
def test_disconnect_all_no_workspaces(mock_stop, runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli, ["disconnect-all"])
    assert result.exit_code == 0
    assert "No cinna workspaces" in result.output
    mock_stop.assert_not_called()


@patch("cinna.main.sync_session.stop")
def test_disconnect_all_removes_workspaces(mock_stop, runner, tmp_path, sample_config, monkeypatch):
    from cinna.config import save_config

    ws_a = tmp_path / "agent-a"
    ws_a.mkdir()
    save_config(sample_config, ws_a)

    ws_b = tmp_path / "agent-b"
    ws_b.mkdir()
    save_config(sample_config, ws_b)

    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["disconnect-all"], input="y\n")
    assert result.exit_code == 0
    assert not ws_a.exists()
    assert not ws_b.exists()
    assert mock_stop.call_count == 2


# ─── sync push / pull / resolve / conflicts (friction B1–B3) ───────────────


@patch("cinna.main.sync_session.flush")
@patch("cinna.main.sync_session.ensure_session")
@patch("cinna.main.find_workspace_root")
@patch("cinna.main.load_config")
def test_sync_push_ensures_and_flushes(
    mock_load, mock_find, mock_ensure, mock_flush, runner, workspace_root, sample_config
):
    from cinna.sync_session import SyncStatus

    mock_find.return_value = workspace_root
    mock_load.return_value = sample_config
    mock_flush.return_value = SyncStatus(session_name="cinna-abc", state="connected")

    result = runner.invoke(cli, ["sync", "push"])
    assert result.exit_code == 0, result.output
    mock_ensure.assert_called_once()
    mock_flush.assert_called_once()


@patch("cinna.main._make_remote_deleter")
@patch("cinna.main.sync_session.resolve_conflicts")
@patch("cinna.main.sync_session.flush")
@patch("cinna.main.sync_session.ensure_session")
@patch("cinna.main.find_workspace_root")
@patch("cinna.main.load_config")
def test_sync_push_force_resolves_local_first(
    mock_load, mock_find, mock_ensure, mock_flush, mock_resolve, mock_deleter,
    runner, workspace_root, sample_config
):
    from cinna.sync_session import ResolveResult, SyncStatus

    mock_find.return_value = workspace_root
    mock_load.return_value = sample_config
    mock_resolve.return_value = ResolveResult(resolved=["a.txt"], remaining=[])
    mock_flush.return_value = SyncStatus(session_name="cinna-abc", state="connected")

    result = runner.invoke(cli, ["sync", "push", "--force"])
    assert result.exit_code == 0, result.output
    # Local wins → resolve_conflicts called with prefer='local'.
    _, kwargs = mock_resolve.call_args
    assert kwargs.get("prefer") == "local"
    mock_flush.assert_called_once()


@patch("cinna.main.sync_session.resolve_conflicts")
@patch("cinna.main.sync_session.status")
@patch("cinna.main.find_workspace_root")
@patch("cinna.main.load_config")
def test_sync_resolve_prefer_remote(
    mock_load, mock_find, mock_status, mock_resolve, runner, workspace_root, sample_config
):
    from cinna.sync_session import ResolveResult, SyncStatus

    mock_find.return_value = workspace_root
    mock_load.return_value = sample_config
    mock_status.return_value = SyncStatus(session_name="cinna-abc", state="connected")
    mock_resolve.return_value = ResolveResult(resolved=["a.txt"], remaining=[])

    result = runner.invoke(cli, ["sync", "resolve", "--prefer", "remote"])
    assert result.exit_code == 0, result.output
    _, kwargs = mock_resolve.call_args
    assert kwargs.get("prefer") == "remote"


@patch("cinna.main.sync_session.status")
@patch("cinna.main.find_workspace_root")
@patch("cinna.main.load_config")
def test_sync_resolve_requires_running_session(
    mock_load, mock_find, mock_status, runner, workspace_root, sample_config
):
    from cinna.sync_session import SyncStatus

    mock_find.return_value = workspace_root
    mock_load.return_value = sample_config
    mock_status.return_value = SyncStatus(session_name="cinna-abc", state="missing")

    result = runner.invoke(cli, ["sync", "resolve", "--prefer", "local"])
    assert result.exit_code != 0
    assert "No sync session" in result.output


@patch("cinna.main.sync_session.daemon_conflict_paths")
@patch("cinna.main.find_workspace_root")
@patch("cinna.main.load_config")
def test_sync_conflicts_lists_daemon_paths(
    mock_load, mock_find, mock_paths, runner, workspace_root, sample_config
):
    mock_find.return_value = workspace_root
    mock_load.return_value = sample_config
    mock_paths.return_value = ["a.txt", "dir/b.txt"]

    result = runner.invoke(cli, ["sync", "conflicts"])
    assert result.exit_code == 0, result.output
    assert "a.txt" in result.output
    assert "dir/b.txt" in result.output
