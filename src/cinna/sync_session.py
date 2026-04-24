"""Thin wrapper around the Mutagen CLI.

Each workspace gets one Mutagen session named `cinna-<short-agent-id>` that
continuously syncs `./workspace` against the remote agent env via the
`cinna-sync-ssh` shim.
"""

import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import click

from cinna.config import (
    CinnaConfig,
    GLOBAL_STATE_DIR,
    config_dir,
    upsert_agent_registry,
    workspace_dir,
)
from cinna import console

logger = logging.getLogger("cinna.sync_session")


MUTAGEN_YML_TEMPLATE = """\
sync:
  defaults:
    mode: two-way-safe
    permissions:
      mode: portable
    ignore:
      vcs: true
      paths:
        - __pycache__/
        - node_modules/
        - .venv/
        - .cinna/
        - .mypy_cache/
        - .pytest_cache/
        - .DS_Store
    scan:
      mode: accelerated
"""


@dataclass
class SyncStatus:
    session_name: str
    state: str  # "connected", "disconnected", "paused", "error", "unknown"
    pending_to_remote: int = 0
    pending_to_local: int = 0
    conflict_count: int = 0
    last_error: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def exists(self) -> bool:
        return self.state != "missing"


def session_name(agent_id: str) -> str:
    """Stable session label — one Mutagen session per agent."""
    short = agent_id.replace("-", "")[:8]
    return f"cinna-{short}"


def mutagen_yml_path(workspace_root: Path) -> Path:
    return workspace_root / "mutagen.yml"


def write_mutagen_yml(workspace_root: Path, overwrite: bool = False) -> Path:
    """Seed a default mutagen.yml if one is not already present."""
    path = mutagen_yml_path(workspace_root)
    if path.exists() and not overwrite:
        return path
    path.write_text(MUTAGEN_YML_TEMPLATE)
    logger.info("Wrote %s", path)
    return path


MUTAGEN_SSH_DIR = GLOBAL_STATE_DIR / "mutagen-ssh"


def _ensure_ssh_shim_dir() -> Path:
    """Materialize a directory containing an `ssh` executable that dispatches
    to `cinna-sync-ssh`.

    Mutagen's `MUTAGEN_SSH_PATH` is a directory search path — it looks for an
    executable literally named `ssh` inside. Pointing it directly at the
    shim binary does not work; Mutagen reports "unable to locate command".

    The wrapper is regenerated on each call so the embedded interpreter /
    shim path stays in sync with the current cinna install.
    """
    MUTAGEN_SSH_DIR.mkdir(parents=True, exist_ok=True)
    wrapper = MUTAGEN_SSH_DIR / "ssh"

    shim_bin = shutil.which("cinna-sync-ssh")
    if shim_bin:
        script = f'#!/usr/bin/env bash\nexec {shlex.quote(shim_bin)} "$@"\n'
    else:
        # Dev / broken-packaging fallback: invoke the module directly with
        # whichever interpreter is running the current cinna command.
        script = (
            f'#!/usr/bin/env bash\n'
            f'exec {shlex.quote(sys.executable)} -m cinna.sync_ssh_shim "$@"\n'
        )

    wrapper.write_text(script)
    wrapper.chmod(0o755)
    return MUTAGEN_SSH_DIR


def _mutagen_env(config: CinnaConfig) -> dict[str, str]:
    """Env vars Mutagen and the shim need.

    `MUTAGEN_SSH_PATH` points at our shim directory. The `CINNA_*` vars are
    kept as a fast-path hint for the shim; the authoritative source is the
    per-user `~/.cinna/agents.json` registry, which the shim consults on
    every invocation so a shared Mutagen daemon can serve multiple agents.
    """
    env = os.environ.copy()
    env["MUTAGEN_SSH_PATH"] = str(_ensure_ssh_shim_dir())
    env["CINNA_AGENT_ID"] = config.agent_id
    env["CINNA_CLI_TOKEN"] = config.cli_token
    env["CINNA_PLATFORM_URL"] = config.platform_url
    return env


def _run_mutagen(
    args: list[str],
    config: CinnaConfig,
    cwd: Path | None = None,
    check: bool = False,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    """Run `mutagen <args>` with the right env."""
    cmd = ["mutagen", *args]
    logger.debug("exec: %s (cwd=%s)", " ".join(cmd), cwd)
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=_mutagen_env(config),
        capture_output=capture,
        text=True,
        check=check,
    )


def ensure_daemon_running(config: CinnaConfig) -> None:
    """Start the Mutagen daemon if it isn't already."""
    result = _run_mutagen(["daemon", "start"], config)
    if result.returncode != 0 and "already running" not in (result.stderr or ""):
        raise click.ClickException(
            f"Failed to start Mutagen daemon:\n{result.stderr.strip()}"
        )


# Mutagen's daemon captures its env at startup. If the user had a daemon running
# from an older cinna-cli (broken MUTAGEN_SSH_PATH) or from another Mutagen
# consumer, the env it uses to spawn `ssh` will be stale and `sync create`
# fails with one of these messages. Detecting the leaf string lets us restart
# the daemon once and retry transparently.
_STALE_DAEMON_MARKERS = (
    "unable to locate command",
    "unable to identify 'ssh' command",
)


def _looks_like_stale_daemon_error(stderr: str) -> bool:
    text = stderr or ""
    return any(marker in text for marker in _STALE_DAEMON_MARKERS)


def _restart_daemon(config: CinnaConfig) -> None:
    """Bounce the Mutagen daemon so it picks up our env on next spawn.

    Warning: this terminates any other Mutagen sessions the daemon is managing,
    not just cinna's. They will auto-resume on the next `mutagen sync list` /
    `cinna sync start`, but in-flight syncs pause briefly.
    """
    logger.info("Restarting Mutagen daemon to refresh its environment")
    console.warn("Restarting Mutagen daemon to pick up updated SSH transport…")
    _run_mutagen(["daemon", "stop"], config)
    start_result = _run_mutagen(["daemon", "start"], config)
    if start_result.returncode != 0 and "already running" not in (start_result.stderr or ""):
        raise click.ClickException(
            f"Failed to restart Mutagen daemon:\n{start_result.stderr.strip()}"
        )


def _list_sessions(config: CinnaConfig) -> list[dict]:
    """Return parsed session list from Mutagen.

    Mutagen 0.18.x has no ``--json`` flag; we render via a Go template that
    pipes the payload through ``json``. The top-level value is a list.
    """
    result = _run_mutagen(["sync", "list", "--template", "{{json .}}"], config)
    if result.returncode != 0:
        logger.debug("mutagen sync list failed: %s", result.stderr)
        return []
    stdout = (result.stdout or "").strip()
    if not stdout or stdout == "null":
        return []
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        logger.warning("Could not parse mutagen JSON: %s", exc)
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("sessions") or [data]
    return []


def _find_session(config: CinnaConfig) -> dict | None:
    target = session_name(config.agent_id)
    for s in _list_sessions(config):
        if s.get("name") == target or s.get("identifier", "").endswith(target):
            return s
    return None


def start(config: CinnaConfig, workspace_root: Path) -> SyncStatus:
    """Create or resume the per-agent Mutagen sync session.

    No-ops when a session already exists — callers see a friendly message.
    """
    # Make sure the SSH shim knows how to resolve this agent's credentials
    # even if the daemon was started earlier for a different agent.
    upsert_agent_registry(
        config.agent_id,
        config.platform_url,
        config.cli_token,
        workspace_root,
        frontend_url=config.frontend_url,
    )

    ensure_daemon_running(config)
    write_mutagen_yml(workspace_root)

    # Foreground-sync model: every `cinna sync start` owns a fresh session for
    # its lifetime. If a same-named session is already present — either from a
    # crashed previous run or a parallel terminal — we terminate it first so
    # there's exactly one owner. Other terminals wanting to observe can use
    # `cinna sync status`.
    existing = _find_session(config)
    if existing is not None:
        logger.info("Terminating pre-existing session %s before creating fresh one", existing.get("name"))
        _run_mutagen(["sync", "terminate", session_name(config.agent_id)], config)

    local_path = workspace_dir(workspace_root)
    local_path.mkdir(parents=True, exist_ok=True)
    # OpenSSH-style `host:path`, not `ssh://host/path`. Mutagen's parser
    # resolves the first `:` against the OpenSSH form first and would otherwise
    # treat the literal string "ssh" as the host. The shim parses the resulting
    # argv host token (`cinna-agent-<uuid>`) to derive the agent_id.
    # `/app/workspace` is the fixed bind-mount inside the agent env container
    # (see env-templates/*/Dockerfile and /sync/exec's cwd). mutagen-agent
    # resolves this path absolutely — not relative to its cwd.
    remote_url = f"cinna@cinna-agent-{config.agent_id}:/app/workspace"

    args = [
        "sync",
        "create",
        "--name",
        session_name(config.agent_id),
        "--sync-mode=two-way-safe",
        "--ignore-vcs",
        str(local_path),
        remote_url,
    ]
    result = _run_mutagen(args, config, cwd=workspace_root)
    if result.returncode != 0 and _looks_like_stale_daemon_error(result.stderr):
        # Daemon was started before our current MUTAGEN_SSH_PATH wiring. Bounce
        # it and retry once; the second pass runs against a fresh env.
        _restart_daemon(config)
        result = _run_mutagen(args, config, cwd=workspace_root)
    if result.returncode != 0:
        raise click.ClickException(
            f"Failed to create Mutagen session:\n{result.stderr.strip() or result.stdout.strip()}"
        )

    return status(config)


def stop(config: CinnaConfig) -> None:
    """Terminate the per-agent Mutagen session (daemon stays up)."""
    _run_mutagen(["sync", "terminate", session_name(config.agent_id)], config)


def run_foreground(config: CinnaConfig) -> int:
    """Attach the terminal to the Mutagen sync session via a two-tab TUI.

    The TUI polls ``mutagen sync list`` once per second. Tab 1 renders a
    friendly status block and a derived activity log; Tab 2 shows the raw
    ``mutagen sync list --long`` output for power users.

    Blocks until the user presses ``q`` / Ctrl-C. On return the Mutagen
    session is terminated so sync does not outlive the TUI.

    Returns 0 on clean exit.
    """
    from .sync_tui import run_tui

    session = session_name(config.agent_id)
    env = _mutagen_env(config)
    rc = 0
    try:
        rc = run_tui(config, session, env)
    except KeyboardInterrupt:
        rc = 0
    finally:
        # Terminate the Mutagen session — sync does not outlive the TUI.
        try:
            stop(config)
        except Exception as exc:
            logger.debug("sync stop on foreground exit failed: %s", exc)
    return rc


def status(config: CinnaConfig) -> SyncStatus:
    """Current state of the agent's sync session."""
    session = _find_session(config)
    if session is None:
        return SyncStatus(session_name=session_name(config.agent_id), state="missing")
    return _to_status(config, session)


def _to_status(config: CinnaConfig, session: dict) -> SyncStatus:
    """Map the Mutagen JSON shape onto our SyncStatus dataclass.

    The shape varies a bit across Mutagen versions; we pull the keys we care
    about defensively and stash the raw blob for callers that want more.
    """
    raw_state = (session.get("status") or session.get("state") or "").lower()
    paused = bool(session.get("paused"))
    if paused:
        state = "paused"
    elif raw_state in {"watching", "scanning", "staging", "transitioning", "saving", "reconciling", "connected", "watching-changes"}:
        state = "connected"
    elif raw_state in {"disconnected", "connecting"}:
        state = "disconnected"
    elif "error" in raw_state or session.get("lastError"):
        state = "error"
    elif raw_state:
        state = raw_state
    else:
        state = "unknown"

    alpha = session.get("alpha") or {}
    beta = session.get("beta") or {}
    pending_to_remote = _safe_int(alpha.get("stagedChanges"))
    pending_to_local = _safe_int(beta.get("stagedChanges"))
    conflicts = _safe_int(session.get("conflictCount") or len(session.get("conflicts") or []))

    return SyncStatus(
        session_name=session.get("name") or session_name(config.agent_id),
        state=state,
        pending_to_remote=pending_to_remote,
        pending_to_local=pending_to_local,
        conflict_count=conflicts,
        last_error=session.get("lastError") or None,
        raw=session,
    )


def _safe_int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


@dataclass
class Conflict:
    path: Path
    kind: str  # "alpha" (local) | "beta" (remote) | "unknown"


def list_conflicts(config: CinnaConfig, workspace_root: Path) -> list[Conflict]:
    """Walk workspace for Mutagen conflict copies.

    Mutagen writes `<name>.conflict.<side>.<ts>` when it can't auto-merge. We
    surface them path-first so the CLI/TUI can offer resolution UX.
    """
    root = workspace_dir(workspace_root)
    if not root.exists():
        return []

    results: list[Conflict] = []
    for path in root.rglob("*.conflict.*"):
        if not path.is_file():
            continue
        # Parse side from suffix if present — best effort.
        parts = path.name.split(".conflict.")
        kind = "unknown"
        if len(parts) == 2:
            tail = parts[1]
            if tail.startswith("alpha"):
                kind = "alpha"
            elif tail.startswith("beta"):
                kind = "beta"
        results.append(Conflict(path=path, kind=kind))
    return results


def session_log_dir(workspace_root: Path) -> Path:
    """Where we cache per-session breadcrumbs (exec history, etc.)."""
    return config_dir(workspace_root) / "sync"
