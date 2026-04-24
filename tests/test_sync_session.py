"""Tests for sync_session module."""

from unittest.mock import MagicMock, patch

from cinna.sync_session import (
    MUTAGEN_YML_TEMPLATE,
    list_conflicts,
    session_name,
    write_mutagen_yml,
    _to_status,
)


def test_session_name_is_stable(sample_config):
    name_a = session_name(sample_config.agent_id)
    name_b = session_name(sample_config.agent_id)
    assert name_a == name_b
    assert name_a.startswith("cinna-")


def test_write_mutagen_yml_new(tmp_path):
    path = write_mutagen_yml(tmp_path)
    assert path.exists()
    assert path.read_text() == MUTAGEN_YML_TEMPLATE


def test_write_mutagen_yml_preserves_existing(tmp_path):
    path = tmp_path / "mutagen.yml"
    path.write_text("# custom\n")
    write_mutagen_yml(tmp_path)
    assert path.read_text() == "# custom\n"


def test_write_mutagen_yml_overwrite(tmp_path):
    path = tmp_path / "mutagen.yml"
    path.write_text("# custom\n")
    write_mutagen_yml(tmp_path, overwrite=True)
    assert path.read_text() == MUTAGEN_YML_TEMPLATE


def test_to_status_connected(sample_config):
    session = {
        "name": "cinna-agent123",
        "status": "watching",
        "alpha": {"stagedChanges": 2},
        "beta": {"stagedChanges": 1},
        "conflictCount": 0,
    }
    status = _to_status(sample_config, session)
    assert status.state == "connected"
    assert status.pending_to_remote == 2
    assert status.pending_to_local == 1
    assert status.conflict_count == 0


def test_to_status_paused(sample_config):
    session = {"name": "cinna-agent123", "status": "watching", "paused": True}
    status = _to_status(sample_config, session)
    assert status.state == "paused"


def test_to_status_error(sample_config):
    session = {"name": "cinna-agent123", "lastError": "handshake failed"}
    status = _to_status(sample_config, session)
    assert status.state == "error"
    assert status.last_error == "handshake failed"


def test_list_conflicts_empty(tmp_path, sample_config):
    (tmp_path / "workspace").mkdir()
    assert list_conflicts(sample_config, tmp_path) == []


def test_list_conflicts_detects_files(tmp_path, sample_config):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "scripts").mkdir()
    a = ws / "scripts" / "main.py.conflict.alpha.20260101"
    b = ws / "scripts" / "main.py.conflict.beta.20260101"
    a.write_text("local")
    b.write_text("remote")

    results = list_conflicts(sample_config, tmp_path)
    kinds = {c.kind for c in results}
    assert kinds == {"alpha", "beta"}


@patch("cinna.sync_session._run_mutagen")
def test_stop_calls_terminate(mock_run, sample_config):
    from cinna.sync_session import stop

    mock_run.return_value = MagicMock(returncode=0)
    stop(sample_config)
    args = mock_run.call_args[0][0]
    assert args[0:2] == ["sync", "terminate"]


def test_ensure_ssh_shim_dir_creates_wrapper():
    """MUTAGEN_SSH_PATH is a dir search path; the dir must contain an executable
    named exactly 'ssh' for Mutagen to find it."""
    from cinna.sync_session import _ensure_ssh_shim_dir

    d = _ensure_ssh_shim_dir()
    ssh = d / "ssh"
    assert ssh.is_file()
    mode = ssh.stat().st_mode & 0o777
    assert mode & 0o111, f"ssh wrapper must be executable, got {oct(mode)}"
    text = ssh.read_text()
    assert text.startswith("#!/usr/bin/env bash"), text
    assert "cinna.sync_ssh_shim" in text or "cinna-sync-ssh" in text


def test_ensure_ssh_shim_dir_is_idempotent():
    from cinna.sync_session import _ensure_ssh_shim_dir

    d1 = _ensure_ssh_shim_dir()
    d2 = _ensure_ssh_shim_dir()
    assert d1 == d2
    assert (d1 / "ssh").is_file()


def test_mutagen_env_points_at_shim_dir_not_file(sample_config):
    """Regression: MUTAGEN_SSH_PATH must be a directory path, not a binary path.
    Mutagen treats it as a search-path and looks for 'ssh' inside."""
    from pathlib import Path
    from cinna.sync_session import _mutagen_env

    env = _mutagen_env(sample_config)
    path = env["MUTAGEN_SSH_PATH"]
    assert Path(path).is_dir(), f"expected a directory, got {path}"
    assert (Path(path) / "ssh").is_file()


def test_looks_like_stale_daemon_error():
    from cinna.sync_session import _looks_like_stale_daemon_error

    assert _looks_like_stale_daemon_error(
        "Error: unable to connect to beta: unable to dial agent endpoint: "
        "unable to create agent command: unable to set up SSH invocation: "
        "unable to identify 'ssh' command: unable to locate command"
    )
    assert _looks_like_stale_daemon_error("unable to identify 'ssh' command: boom")
    assert not _looks_like_stale_daemon_error("auth failed: 401")
    assert not _looks_like_stale_daemon_error("")


@patch("cinna.sync_session._run_mutagen")
def test_start_retries_after_stale_daemon(mock_run, sample_config, tmp_path):
    """Regression: a Mutagen daemon left running from a pre-fix cinna-cli has a
    stale MUTAGEN_SSH_PATH. The first `sync create` fails with the stale-daemon
    signature; the CLI must bounce the daemon and retry once."""
    from cinna.sync_session import start

    (tmp_path / "workspace").mkdir()
    stale_err = (
        "Error: unable to connect to beta: unable to set up SSH invocation: "
        "unable to identify 'ssh' command: unable to locate command"
    )

    call_index = {"i": 0}

    def fake_run(args, *_a, **_kw):
        call_index["i"] += 1
        first = args[0]
        second = args[1] if len(args) > 1 else ""
        if first == "daemon" and second == "start":
            return MagicMock(returncode=0, stdout="", stderr="")
        if first == "daemon" and second == "stop":
            return MagicMock(returncode=0, stdout="", stderr="")
        if first == "sync" and second == "list":
            return MagicMock(returncode=0, stdout="[]", stderr="")
        if first == "sync" and second == "create":
            # First create -> fail with stale-daemon signature. Second -> ok.
            if "create_called" not in call_index:
                call_index["create_called"] = True
                return MagicMock(returncode=1, stdout="", stderr=stale_err)
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    mock_run.side_effect = fake_run
    start(sample_config, tmp_path)

    mutagen_invocations = [c.args[0] for c in mock_run.call_args_list]
    # Must have stopped the daemon after the first failure.
    assert any(args[:2] == ["daemon", "stop"] for args in mutagen_invocations)
    # Must have invoked sync create at least twice (retry after bounce).
    assert sum(1 for args in mutagen_invocations if args[:2] == ["sync", "create"]) == 2
