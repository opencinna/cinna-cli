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


def test_looks_like_agent_env_waking():
    from cinna.sync_session import _looks_like_agent_env_waking

    real_world_stderr = (
        "Error: unable to connect to beta: unable to connect to endpoint: "
        "unable to dial agent endpoint: unable to handshake with agent process: "
        "unable to receive server magic number: EOF (error output: "
        "cinna-sync-ssh: ws pump ended: received 1013 (try again later); "
        "then sent 1013 (try again later))"
    )
    assert _looks_like_agent_env_waking(real_world_stderr)
    assert _looks_like_agent_env_waking("", real_world_stderr)
    assert not _looks_like_agent_env_waking("auth failed: 401")
    assert not _looks_like_agent_env_waking("")


@patch("cinna.sync_session.time.sleep", lambda _s: None)
@patch("cinna.sync_session._run_mutagen")
def test_start_retries_when_agent_env_waking_then_succeeds(
    mock_run, sample_config, tmp_path
):
    """When the backend closes the WS with 1013 ('try again later') because the
    agent env is auto-activating, the CLI must retry a couple of times before
    surfacing an error."""
    from cinna.sync_session import start

    (tmp_path / "workspace").mkdir()
    waking_err = (
        "Error: unable to handshake with agent process: "
        "unable to receive server magic number: EOF (error output: "
        "cinna-sync-ssh: ws pump ended: received 1013 (try again later))"
    )

    create_calls = {"n": 0}

    def fake_run(args, *_a, **_kw):
        first = args[0]
        second = args[1] if len(args) > 1 else ""
        if first == "daemon":
            return MagicMock(returncode=0, stdout="", stderr="")
        if first == "sync" and second == "list":
            return MagicMock(returncode=0, stdout="[]", stderr="")
        if first == "sync" and second == "terminate":
            return MagicMock(returncode=0, stdout="", stderr="")
        if first == "sync" and second == "create":
            create_calls["n"] += 1
            # First create fails with 1013; second one succeeds.
            if create_calls["n"] == 1:
                return MagicMock(returncode=1, stdout="", stderr=waking_err)
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    mock_run.side_effect = fake_run
    start(sample_config, tmp_path)

    assert create_calls["n"] == 2
    # Between attempts we terminate the half-registered session.
    invocations = [c.args[0] for c in mock_run.call_args_list]
    assert any(args[:2] == ["sync", "terminate"] for args in invocations)


@patch("cinna.sync_session.time.sleep", lambda _s: None)
@patch("cinna.sync_session._run_mutagen")
def test_start_gives_up_with_friendly_error_after_max_waking_retries(
    mock_run, sample_config, tmp_path
):
    """After exhausting the waking-env retries, the user sees a friendly
    message instead of the raw Mutagen handshake-EOF stack."""
    import click
    import pytest

    from cinna.sync_session import start

    (tmp_path / "workspace").mkdir()
    waking_err = (
        "unable to handshake with agent process: received 1013 (try again later)"
    )

    def fake_run(args, *_a, **_kw):
        first = args[0]
        second = args[1] if len(args) > 1 else ""
        if first == "daemon":
            return MagicMock(returncode=0, stdout="", stderr="")
        if first == "sync" and second == "list":
            return MagicMock(returncode=0, stdout="[]", stderr="")
        if first == "sync" and second == "terminate":
            return MagicMock(returncode=0, stdout="", stderr="")
        if first == "sync" and second == "create":
            return MagicMock(returncode=1, stdout="", stderr=waking_err)
        return MagicMock(returncode=0, stdout="", stderr="")

    mock_run.side_effect = fake_run
    with pytest.raises(click.ClickException) as exc_info:
        start(sample_config, tmp_path)

    msg = exc_info.value.format_message()
    assert "Cannot reach the agent environment" in msg
    # The raw Mutagen "magic number" / 1013 detail must not be in the surfaced
    # error — that's the noise we're hiding from the user.
    assert "1013" not in msg
    assert "magic number" not in msg


# ─── redev: startup conflict resolution in remote's favor ──────────────────


def test_extract_conflict_paths_from_changes():
    from cinna.sync_session import extract_conflict_paths

    session = {
        "conflicts": [
            {
                "root": "shared.txt",
                "alphaChanges": [{"path": "shared.txt"}],
                "betaChanges": [{"path": "shared.txt"}],
            },
            {
                "root": "docs/notes.md",
                "alphaChanges": [{"path": "docs/notes.md"}],
                "betaChanges": [{"path": "docs/notes.md"}],
            },
        ]
    }
    assert extract_conflict_paths(session) == ["docs/notes.md", "shared.txt"]


def test_extract_conflict_paths_root_only():
    """Directory/file disagreements may carry only `root` (capabilities §6)."""
    from cinna.sync_session import extract_conflict_paths

    session = {"conflicts": [{"root": "data", "alphaChanges": [], "betaChanges": []}]}
    assert extract_conflict_paths(session) == ["data"]


def test_extract_conflict_paths_empty():
    from cinna.sync_session import extract_conflict_paths

    assert extract_conflict_paths(None) == []
    assert extract_conflict_paths({}) == []
    assert extract_conflict_paths({"conflicts": []}) == []


def _watching(conflicts=None, cycles=1):
    return {
        "name": "cinna-agent123",
        "status": "watching",
        "successfulCycles": cycles,
        "conflicts": conflicts or [],
    }


@patch("cinna.sync_session.time.sleep")
@patch("cinna.sync_session._run_mutagen")
@patch("cinna.sync_session._find_session")
def test_resolve_startup_conflicts_favor_remote(
    mock_find, mock_run, mock_sleep, sample_config, tmp_path
):
    """Local losers are moved to a backup dir, then a single reset propagates
    the remote versions back (delete-loser + reset recipe, capabilities §8)."""
    from cinna.sync_session import resolve_startup_conflicts_favor_remote

    ws = tmp_path / "workspace"
    (ws / "docs").mkdir(parents=True)
    (ws / "shared.txt").write_text("LOCAL")
    (ws / "docs" / "notes.md").write_text("LOCAL NOTES")

    conflicts = [
        {"root": "shared.txt",
         "alphaChanges": [{"path": "shared.txt"}],
         "betaChanges": [{"path": "shared.txt"}]},
        {"root": "docs/notes.md",
         "alphaChanges": [{"path": "docs/notes.md"}],
         "betaChanges": [{"path": "docs/notes.md"}]},
    ]
    mock_find.side_effect = [_watching(conflicts), _watching()]
    mock_run.return_value = MagicMock(returncode=0)

    result = resolve_startup_conflicts_favor_remote(sample_config, tmp_path)

    assert result.resolved == ["docs/notes.md", "shared.txt"]
    assert result.remaining == []

    # Local copies moved out of the workspace into the backup dir.
    assert not (ws / "shared.txt").exists()
    assert not (ws / "docs" / "notes.md").exists()
    assert result.backup_dir is not None
    assert (result.backup_dir / "shared.txt").read_text() == "LOCAL"
    assert (result.backup_dir / "docs" / "notes.md").read_text() == "LOCAL NOTES"

    # Exactly one batched reset for both conflicts.
    reset_calls = [c for c in mock_run.call_args_list if c[0][0][:2] == ["sync", "reset"]]
    assert len(reset_calls) == 1


@patch("cinna.sync_session.time.sleep")
@patch("cinna.sync_session._run_mutagen")
@patch("cinna.sync_session._find_session")
def test_resolve_startup_no_conflicts_is_noop(
    mock_find, mock_run, mock_sleep, sample_config, tmp_path
):
    from cinna.sync_session import resolve_startup_conflicts_favor_remote

    (tmp_path / "workspace").mkdir()
    mock_find.return_value = _watching()

    result = resolve_startup_conflicts_favor_remote(sample_config, tmp_path)

    assert result.resolved == []
    assert result.remaining == []
    assert result.backup_dir is None
    mock_run.assert_not_called()


@patch("cinna.sync_session.time.sleep")
@patch("cinna.sync_session._run_mutagen")
@patch("cinna.sync_session._find_session")
def test_resolve_startup_reports_remaining_after_max_rounds(
    mock_find, mock_run, mock_sleep, sample_config, tmp_path
):
    """A conflict the daemon keeps reporting ends up in `remaining`, not
    silently swallowed."""
    from cinna.sync_session import resolve_startup_conflicts_favor_remote

    (tmp_path / "workspace").mkdir()
    sticky = [{"root": "x.txt",
               "alphaChanges": [{"path": "x.txt"}],
               "betaChanges": [{"path": "x.txt"}]}]
    mock_find.return_value = _watching(sticky)
    mock_run.return_value = MagicMock(returncode=0)

    result = resolve_startup_conflicts_favor_remote(sample_config, tmp_path)

    assert result.resolved == []
    assert result.remaining == ["x.txt"]


@patch("cinna.sync_session.time.sleep")
@patch("cinna.sync_session._find_session")
def test_wait_until_settled_times_out(mock_find, mock_sleep, sample_config):
    import click
    import pytest
    from cinna.sync_session import _wait_until_settled

    mock_find.return_value = {"status": "scanning"}
    with pytest.raises(click.ClickException, match="Timed out"):
        _wait_until_settled(sample_config, timeout=0)


@patch("cinna.sync_session.time.sleep")
@patch("cinna.sync_session._find_session")
def test_wait_until_settled_rejects_paused(mock_find, mock_sleep, sample_config):
    import click
    import pytest
    from cinna.sync_session import _wait_until_settled

    mock_find.return_value = {"status": "watching", "paused": True, "successfulCycles": 3}
    with pytest.raises(click.ClickException, match="paused"):
        _wait_until_settled(sample_config, timeout=5)


# ─── B3: daemon-sourced conflict listing (status/conflicts agree) ──────────


@patch("cinna.sync_session._find_session")
def test_daemon_conflict_paths_sources_from_json(mock_find, sample_config):
    """`sync conflicts` reads the daemon's conflicts[] (matches status count),
    not a disk walk — two-way-safe writes no .conflict.* files."""
    from cinna.sync_session import daemon_conflict_paths

    mock_find.return_value = _watching([
        {"root": "a.txt", "alphaChanges": [{"path": "a.txt"}], "betaChanges": [{"path": "a.txt"}]},
        {"root": "dir/b.txt", "alphaChanges": [{"path": "dir/b.txt"}], "betaChanges": []},
    ])
    assert daemon_conflict_paths(sample_config) == ["a.txt", "dir/b.txt"]


@patch("cinna.sync_session._find_session")
def test_daemon_conflict_paths_empty(mock_find, sample_config):
    from cinna.sync_session import daemon_conflict_paths

    mock_find.return_value = _watching()
    assert daemon_conflict_paths(sample_config) == []


# ─── B2: two-directional resolve ───────────────────────────────────────────


@patch("cinna.sync_session.time.sleep")
@patch("cinna.sync_session._run_mutagen")
@patch("cinna.sync_session._find_session")
def test_resolve_conflicts_prefer_local_deletes_remote(
    mock_find, mock_run, mock_sleep, sample_config, tmp_path
):
    """prefer='local' deletes the remote loser (via the callable) and resets;
    local files are left untouched so they propagate out."""
    from cinna.sync_session import resolve_conflicts

    ws = tmp_path / "workspace"
    ws.mkdir(parents=True)
    (ws / "shared.txt").write_text("LOCAL")

    conflicts = [{"root": "shared.txt",
                  "alphaChanges": [{"path": "shared.txt"}],
                  "betaChanges": [{"path": "shared.txt"}]}]
    mock_find.side_effect = [_watching(conflicts), _watching()]
    mock_run.return_value = MagicMock(returncode=0)

    deleted: list[str] = []

    def _remote_delete(rel: str) -> bool:
        deleted.append(rel)
        return True

    result = resolve_conflicts(
        sample_config, tmp_path, prefer="local", remote_delete=_remote_delete
    )

    assert result.resolved == ["shared.txt"]
    assert result.remaining == []
    assert deleted == ["shared.txt"]
    # Local copy is preserved (it's the winner).
    assert (ws / "shared.txt").read_text() == "LOCAL"
    # Exactly one batched reset.
    reset_calls = [c for c in mock_run.call_args_list if c[0][0][:2] == ["sync", "reset"]]
    assert len(reset_calls) == 1


@patch("cinna.sync_session.time.sleep")
@patch("cinna.sync_session._run_mutagen")
@patch("cinna.sync_session._find_session")
def test_resolve_conflicts_prefer_local_failed_delete_stays_remaining(
    mock_find, mock_run, mock_sleep, sample_config, tmp_path
):
    """A remote delete that fails leaves the path in `remaining`, not resolved."""
    from cinna.sync_session import resolve_conflicts

    (tmp_path / "workspace").mkdir()
    sticky = [{"root": "x.txt",
               "alphaChanges": [{"path": "x.txt"}],
               "betaChanges": [{"path": "x.txt"}]}]
    mock_find.return_value = _watching(sticky)
    mock_run.return_value = MagicMock(returncode=0)

    result = resolve_conflicts(
        sample_config, tmp_path, prefer="local", remote_delete=lambda rel: False
    )
    assert result.resolved == []
    assert result.remaining == ["x.txt"]


def test_resolve_conflicts_prefer_local_requires_deleter(sample_config, tmp_path):
    import pytest
    from cinna.sync_session import resolve_conflicts

    with pytest.raises(ValueError, match="remote_delete"):
        resolve_conflicts(sample_config, tmp_path, prefer="local")


def test_resolve_conflicts_rejects_bad_prefer(sample_config, tmp_path):
    import pytest
    from cinna.sync_session import resolve_conflicts

    with pytest.raises(ValueError, match="prefer"):
        resolve_conflicts(sample_config, tmp_path, prefer="sideways")


# ─── B1: one-shot flush + headless ensure_session ──────────────────────────


@patch("cinna.sync_session._run_mutagen")
@patch("cinna.sync_session._find_session")
def test_flush_blocks_and_returns_status(mock_find, mock_run, sample_config):
    from cinna.sync_session import flush

    mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
    mock_find.return_value = _watching()
    st = flush(sample_config)
    assert st.state == "connected"
    flush_calls = [c for c in mock_run.call_args_list if c[0][0][:2] == ["sync", "flush"]]
    assert len(flush_calls) == 1


@patch("cinna.sync_session._create_session")
@patch("cinna.sync_session.write_mutagen_yml")
@patch("cinna.sync_session.ensure_daemon_running")
@patch("cinna.sync_session.upsert_agent_registry")
@patch("cinna.sync_session._find_session")
def test_ensure_session_reuses_existing(
    mock_find, mock_upsert, mock_daemon, mock_write, mock_create, sample_config, tmp_path
):
    """ensure_session reuses a live session and does NOT terminate/recreate it."""
    from cinna.sync_session import ensure_session

    mock_find.return_value = _watching()
    ensure_session(sample_config, tmp_path)
    mock_create.assert_not_called()


@patch("cinna.sync_session.status")
@patch("cinna.sync_session._create_session")
@patch("cinna.sync_session.write_mutagen_yml")
@patch("cinna.sync_session.ensure_daemon_running")
@patch("cinna.sync_session.upsert_agent_registry")
@patch("cinna.sync_session._find_session")
def test_ensure_session_creates_when_missing(
    mock_find, mock_upsert, mock_daemon, mock_write, mock_create, mock_status,
    sample_config, tmp_path
):
    from cinna.sync_session import ensure_session

    mock_find.return_value = None
    ensure_session(sample_config, tmp_path)
    mock_create.assert_called_once()
