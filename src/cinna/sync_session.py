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
import time
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
from cinna.mutagen_runtime import mutagen_binary

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
        # Backend-managed, regenerated remotely on every env (re)start. A stale
        # local copy would otherwise conflict on a file the user is told never
        # to edit. The container holds the authoritative copy; read it with
        # `cinna exec cat credentials/credentials.json` rather than syncing it.
        - credentials/
    scan:
      mode: full
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
    cmd = [mutagen_binary(), *args]
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


# The backend closes the sync-stream WebSocket with code 1013 ("try again later")
# while it auto-activates a suspended agent environment. The shim surfaces this
# as a "received 1013 (try again later)" line and Mutagen reports it as a
# handshake EOF. Detect it so we can retry transparently instead of dumping the
# raw stack on the user.
_AGENT_ENV_WAKING_MARKERS = (
    "received 1013",
    "(try again later)",
)


def _looks_like_agent_env_waking(stderr: str, stdout: str = "") -> bool:
    text = (stderr or "") + "\n" + (stdout or "")
    return any(marker in text for marker in _AGENT_ENV_WAKING_MARKERS)


# Two retries spaced 5s apart give the backend ~10s to finish auto-activation
# before we give up. Auto-activation polls for "running" status with a 120s
# deadline server-side, but the WS handshake fails fast — each client retry
# re-triggers ensure_environment_running and re-polls.
_WAKING_RETRY_DELAYS_SECONDS = (5, 5)


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

    _create_session(config, workspace_root)
    return status(config)


def ensure_session(config: CinnaConfig, workspace_root: Path) -> SyncStatus:
    """Ensure a sync session exists WITHOUT terminating an existing one.

    The headless counterpart to ``start()``: used by the one-shot ``cinna sync
    push`` / ``pull`` verbs so a scripted builder gets a session in the daemon
    (creating it only if missing) and reuses a live ``cinna dev`` session rather
    than killing it. The session persists in the daemon after the command exits,
    so subsequent edits keep syncing until ``cinna dev`` or ``cinna disconnect``.
    """
    upsert_agent_registry(
        config.agent_id,
        config.platform_url,
        config.cli_token,
        workspace_root,
        frontend_url=config.frontend_url,
    )
    ensure_daemon_running(config)
    write_mutagen_yml(workspace_root)

    existing = _find_session(config)
    if existing is not None:
        return _to_status(config, existing)

    _create_session(config, workspace_root)
    return status(config)


def _create_session(config: CinnaConfig, workspace_root: Path) -> None:
    """Create the per-agent Mutagen session (caller handles pre-existing ones).

    Shared by ``start()`` (which terminates any existing session first) and
    ``ensure_session()`` (which only creates when missing). Retries transparently
    on a stale daemon (refresh SSH env) and on an agent env that is still waking.
    """
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

    stale_daemon_restarted = False
    waking_attempt = 0  # how many "agent env waking" retries we've already burned
    while True:
        result = _run_mutagen(args, config, cwd=workspace_root)
        if result.returncode == 0:
            break

        if (
            not stale_daemon_restarted
            and _looks_like_stale_daemon_error(result.stderr)
        ):
            # Daemon was started before our current MUTAGEN_SSH_PATH wiring.
            # Bounce it and retry; the second pass runs against a fresh env.
            _restart_daemon(config)
            stale_daemon_restarted = True
            continue

        if _looks_like_agent_env_waking(result.stderr, result.stdout):
            if waking_attempt < len(_WAKING_RETRY_DELAYS_SECONDS):
                delay = _WAKING_RETRY_DELAYS_SECONDS[waking_attempt]
                total = len(_WAKING_RETRY_DELAYS_SECONDS)
                waking_attempt += 1
                console.warn(
                    "Agent environment is not ready yet (waking up?). "
                    f"Retrying in {delay}s ({waking_attempt}/{total})…"
                )
                logger.info(
                    "Agent env not ready (1013); retry %d/%d after %ds",
                    waking_attempt, total, delay,
                )
                time.sleep(delay)
                # A failed `sync create` may leave a half-registered session in
                # the daemon. Terminate it so the retry starts from a clean
                # slate; ignore the result since "not found" is fine.
                _run_mutagen(
                    ["sync", "terminate", session_name(config.agent_id)], config
                )
                continue
            raise click.ClickException(
                "Cannot reach the agent environment.\n"
                "The platform reported the environment is still waking up or "
                "unavailable after several retries.\n"
                "Open the agent in the platform UI and confirm its environment "
                "is running, then re-run 'cinna dev'."
            )

        raise click.ClickException(
            "Failed to create Mutagen session:\n"
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def flush(config: CinnaConfig, timeout: float = 600.0) -> SyncStatus:
    """Force a sync cycle and block until it completes; return the new status.

    ``mutagen sync flush`` triggers an immediate reconciliation and blocks until
    it finishes (or fails), so a one-shot ``cinna sync push`` / ``pull`` exits
    only once local↔remote is settled. Parked conflicts do not fail the flush —
    they surface in the returned status's ``conflict_count``.
    """
    result = _run_mutagen(
        ["sync", "flush", session_name(config.agent_id)], config
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        # A flush with unresolved conflicts can exit non-zero on some Mutagen
        # versions; the conflicts are reported via status, so don't hard-fail on
        # that — only raise on a genuine transport / session error.
        if "conflict" not in stderr.lower():
            raise click.ClickException(
                f"Sync flush failed:\n{stderr or result.stdout.strip()}"
            )
    return status(config)


def stop(config: CinnaConfig) -> None:
    """Terminate the per-agent Mutagen session (daemon stays up)."""
    _run_mutagen(["sync", "terminate", session_name(config.agent_id)], config)


def list_all_sessions(config: CinnaConfig) -> list[dict]:
    """Every Mutagen session the daemon knows about (public wrapper).

    Unlike ``status``/``_find_session`` (scoped to one agent), this returns the
    full session list so callers like ``cinna doctor`` can reconcile the daemon
    against the registry and spot orphaned ``cinna-*`` sessions. ``config`` only
    supplies the daemon env (``MUTAGEN_SSH_PATH``), which is identical for every
    agent — any throwaway config works.
    """
    return _list_sessions(config)


def terminate_named(name: str, config: CinnaConfig) -> bool:
    """Terminate a Mutagen session by its literal session name.

    The agent-agnostic counterpart of ``stop()``: used by ``cinna doctor`` to
    tear down sessions whose workspace or registry entry is already gone (so no
    per-agent ``CinnaConfig`` is available). Returns True if Mutagen reported
    success. A "session not found" exit is treated as already-gone (True).
    """
    result = _run_mutagen(["sync", "terminate", name], config)
    if result.returncode == 0:
        return True
    stderr = (result.stderr or "").lower()
    if "did not match" in stderr or "no sessions" in stderr or "not found" in stderr:
        return True
    logger.warning("terminate %s failed: %s", name, result.stderr)
    return False


def run_foreground(config: CinnaConfig, workspace_root: Path) -> int:
    """Attach the terminal to the Mutagen sync session via a live TUI.

    Three tabs: Sync (status + per-file activity log), Details (raw
    ``mutagen sync list --long``), and Conflicts (interactive resolution
    for files Mutagen couldn't auto-merge).

    Blocks until the user presses ``q`` / Ctrl-C. On return the Mutagen
    session is terminated so sync does not outlive the TUI.

    Returns 0 on clean exit.
    """
    from .sync_tui import run_tui

    session = session_name(config.agent_id)
    env = _mutagen_env(config)
    rc = 0
    try:
        rc = run_tui(config, session, env, workspace_root)
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
    base = base_status(raw_state)
    if paused:
        state = "paused"
    elif base in {"watching", "scanning", "staging", "transitioning", "saving", "reconciling", "connected"}:
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


def base_status(raw: str) -> str:
    """Strip Mutagen's side suffix (e.g. ``staging-beta`` → ``staging``).

    Mutagen distinguishes the receiving side in its status string while a
    transfer is in flight, but most consumers care about the phase, not the
    direction.
    """
    return raw.split("-", 1)[0] if "-" in raw else raw


@dataclass
class Conflict:
    path: Path
    kind: str  # "alpha" (local) | "beta" (remote) | "unknown"


def list_conflicts(config: CinnaConfig, workspace_root: Path) -> list[Conflict]:
    """Walk workspace for Mutagen conflict-copy files.

    NOTE: two-way-safe (mutagen 0.18.x) does NOT write `.conflict.<side>` files
    (capabilities §7), so this returns empty in normal operation. The
    `cinna sync conflicts` CLI now sources conflicts from the daemon JSON via
    ``daemon_conflict_paths`` instead. This fs-walk is retained only as the
    fallback path documented in §7 ("if a future mutagen starts writing those
    files again") — do not wire it back into a live code path.
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


@dataclass
class ConflictGroup:
    """Two-sided view of one conflicted path.

    Mutagen may write one or both ``.conflict.<side>.<ts>`` copies depending
    on which side it considered the winner. ``canonical`` is the real
    workspace path the two copies are versions of.
    """
    canonical: Path
    alpha_copy: Path | None = None
    beta_copy: Path | None = None


def _canonical_from_conflict(p: Path) -> Path:
    """``foo/bar.txt.conflict.alpha.20260101`` → ``foo/bar.txt``."""
    name = p.name
    idx = name.find(".conflict.")
    if idx <= 0:
        return p
    return p.parent / name[:idx]


def group_conflicts(conflicts: list[Conflict]) -> list[ConflictGroup]:
    """Bucket flat conflict-copy paths by their canonical workspace path."""
    groups: dict[Path, ConflictGroup] = {}
    for c in conflicts:
        canonical = _canonical_from_conflict(c.path)
        g = groups.setdefault(canonical, ConflictGroup(canonical=canonical))
        if c.kind == "alpha":
            g.alpha_copy = c.path
        elif c.kind == "beta":
            g.beta_copy = c.path
    return sorted(groups.values(), key=lambda g: str(g.canonical))


def resolve_conflict(group: ConflictGroup, side: str) -> None:
    """Apply the user's choice: keep ``side``'s version at ``canonical``.

    side: ``"alpha"`` (local) or ``"beta"`` (remote).

    When the named side's conflict copy exists, it replaces the canonical
    file. When it doesn't, the canonical file is already that side's content,
    so we only delete the loser's copy. Mutagen picks up the change on its
    next scan and propagates the resolution to the other endpoint.
    """
    if side not in ("alpha", "beta"):
        raise ValueError(f"side must be 'alpha' or 'beta', got {side!r}")
    target = group.alpha_copy if side == "alpha" else group.beta_copy
    other = group.beta_copy if side == "alpha" else group.alpha_copy

    if target is not None and target.exists():
        if group.canonical.exists() and group.canonical.is_file():
            group.canonical.unlink()
        target.rename(group.canonical)
    if other is not None and other.exists():
        other.unlink()


def session_log_dir(workspace_root: Path) -> Path:
    """Where we cache per-session breadcrumbs (exec history, etc.)."""
    return config_dir(workspace_root) / "sync"


# ─── redev: startup conflict resolution in remote's favor ──────────────────


def extract_conflict_paths(session: dict | None) -> list[str]:
    """Flatten the session's ``conflicts[]`` JSON into sorted relative paths.

    Mirrors the TUI's ``_extract_conflicts`` but path-only: collects every
    path from ``alphaChanges`` / ``betaChanges``, falling back to the
    conflict's ``root`` for kinds (directory/file disagreement, asymmetric
    delete) whose per-side change arrays are empty.
    """
    if not session:
        return []
    paths: set[str] = set()
    for c in session.get("conflicts") or []:
        found = False
        for side in ("alphaChanges", "betaChanges"):
            for change in c.get(side) or []:
                p = change.get("path") or c.get("root") or ""
                if p:
                    paths.add(p)
                    found = True
        if not found and c.get("root"):
            paths.add(c["root"])
    return sorted(paths)


_SETTLE_POLL_SECONDS = 1.0


def _wait_until_settled(
    config: CinnaConfig, timeout: float = 600.0, require_cycle: bool = True
) -> dict:
    """Block until the session has finished a reconciliation pass.

    Settled = status ``watching`` plus evidence a cycle actually ran (a
    successful-cycle count or a populated ``conflicts[]`` — a cycle with
    conflicts still parks in ``watching``). ``require_cycle=False`` drops
    that evidence requirement; use it after ``mutagen sync reset``, which may
    clear the cycle counter.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session = _find_session(config)
        if session is not None:
            if session.get("paused"):
                raise click.ClickException(
                    "Sync session is paused — resume it or re-run 'cinna dev'."
                )
            raw = base_status((session.get("status") or "").lower())
            cycles = _safe_int(session.get("successfulCycles"))
            if raw == "watching" and (
                not require_cycle or cycles >= 1 or session.get("conflicts")
            ):
                return session
        time.sleep(_SETTLE_POLL_SECONDS)
    raise click.ClickException(
        "Timed out waiting for the initial sync to settle. Check connectivity "
        "with 'cinna sync status' and re-run."
    )


@dataclass
class RemoteWinsResult:
    resolved: list[str] = field(default_factory=list)
    remaining: list[str] = field(default_factory=list)
    backup_dir: Path | None = None


_REDEV_MAX_ROUNDS = 3


def resolve_startup_conflicts_favor_remote(
    config: CinnaConfig, workspace_root: Path, timeout: float = 600.0
) -> RemoteWinsResult:
    """Resolve every conflict of a just-started session in remote's favor.

    Waits for the fresh session's first reconciliation, then applies the
    delete-loser + ``mutagen sync reset`` recipe (see
    docs/mutagen_capabilities.md §8) to all conflicted paths in one batch:
    the *local* copy is moved aside so the remote version propagates back.
    Displaced local copies land under
    ``.cinna/sync/redev-backup/<timestamp>/`` rather than being deleted.

    Runs a few rounds because the daemon may briefly report stale conflicts
    right after a reset; paths still conflicted after the last round are
    returned in ``remaining`` for the caller to surface.
    """
    session = _wait_until_settled(config, timeout)
    ws_root = workspace_dir(workspace_root)
    backup_root = (
        session_log_dir(workspace_root)
        / "redev-backup"
        / time.strftime("%Y%m%d-%H%M%S")
    )
    result = RemoteWinsResult()
    resolved: set[str] = set()

    for _ in range(_REDEV_MAX_ROUNDS):
        conflict_paths = extract_conflict_paths(session)
        if not conflict_paths:
            break
        logger.info(
            "redev: resolving %d conflict(s) in favor of remote: %s",
            len(conflict_paths),
            conflict_paths,
        )
        for rel in conflict_paths:
            local = ws_root / rel
            if local.exists() or local.is_symlink():
                backup = backup_root / rel
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(local), str(backup))
                result.backup_dir = backup_root
            resolved.add(rel)
        _run_mutagen(["sync", "reset", session_name(config.agent_id)], config)
        # Give the daemon a beat to process the reset before re-reading state,
        # otherwise we may observe the pre-reset snapshot and burn a round.
        time.sleep(_SETTLE_POLL_SECONDS)
        session = _wait_until_settled(config, timeout, require_cycle=False)

    result.remaining = extract_conflict_paths(session)
    result.resolved = sorted(resolved - set(result.remaining))
    return result


# ─── Authoritative conflict listing + two-directional resolve ──────────────


def daemon_conflict_paths(config: CinnaConfig) -> list[str]:
    """Conflict paths from the Mutagen daemon JSON (authoritative).

    two-way-safe does NOT write ``.conflict.<side>`` files (mutagen 0.18.x — see
    docs/mutagen_capabilities.md §7), so a disk walk finds nothing even when
    conflicts exist. Sourcing from the session's ``conflicts[]`` array (like the
    TUI) is what makes ``cinna sync conflicts`` agree with the count
    ``cinna sync status`` reports.
    """
    return extract_conflict_paths(_find_session(config))


@dataclass
class ResolveResult:
    resolved: list[str] = field(default_factory=list)
    remaining: list[str] = field(default_factory=list)
    backup_dir: Path | None = None


def resolve_conflicts(
    config: CinnaConfig,
    workspace_root: Path,
    prefer: str,
    remote_delete=None,
    timeout: float = 600.0,
) -> ResolveResult:
    """Resolve all current conflicts in favor of one side.

    Applies the delete-loser + ``mutagen sync reset`` recipe
    (docs/mutagen_capabilities.md §8) to every conflicted path:

    - ``prefer="remote"`` — move each conflicted *local* file into a backup dir
      (under ``.cinna/sync/resolve-backup/<ts>/``) so the remote version
      propagates back. (Same mechanism as ``cinna redev``.)
    - ``prefer="local"`` — delete each conflicted *remote* file via the supplied
      ``remote_delete(relpath) -> bool`` callable (which shells through
      ``cinna exec rm``) so the local version propagates out.

    A single ``mutagen sync reset`` per round converges all losers at once;
    repeats a few rounds for daemon settle. Paths whose loser-removal failed (or
    that the daemon still reports) are returned in ``remaining``.
    """
    if prefer not in ("local", "remote"):
        raise ValueError(f"prefer must be 'local' or 'remote', got {prefer!r}")
    if prefer == "local" and remote_delete is None:
        raise ValueError("remote_delete callable is required for prefer='local'")

    session = _wait_until_settled(config, timeout)
    ws_root = workspace_dir(workspace_root)
    backup_root = (
        session_log_dir(workspace_root)
        / "resolve-backup"
        / time.strftime("%Y%m%d-%H%M%S")
    )
    result = ResolveResult()
    resolved: set[str] = set()

    for _ in range(_REDEV_MAX_ROUNDS):
        conflict_paths = extract_conflict_paths(session)
        if not conflict_paths:
            break
        logger.info(
            "resolve: %d conflict(s) in favor of %s: %s",
            len(conflict_paths), prefer, conflict_paths,
        )
        for rel in conflict_paths:
            if prefer == "remote":
                local = ws_root / rel
                if local.exists() or local.is_symlink():
                    backup = backup_root / rel
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(local), str(backup))
                    result.backup_dir = backup_root
                resolved.add(rel)
            else:  # prefer == "local" → remove the remote loser
                if remote_delete(rel):
                    resolved.add(rel)
                # On failure, leave it; it'll appear in `remaining`.
        _run_mutagen(["sync", "reset", session_name(config.agent_id)], config)
        time.sleep(_SETTLE_POLL_SECONDS)
        session = _wait_until_settled(config, timeout, require_cycle=False)

    result.remaining = extract_conflict_paths(session)
    result.resolved = sorted(resolved - set(result.remaining))
    return result
