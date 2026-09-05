"""``cinna doctor`` — diagnose and repair stale local sync state.

Targets the two pieces of per-machine state that drift as agents come and go:
the per-user registry (``~/.cinna/agents.json``) and the Mutagen daemon's
sessions. It heals the failure modes that accumulate over time:

  * **stale_folder**   — a registry entry whose workspace folder was deleted
    (the ``.cinna/config.json`` is gone). The entry — and any leftover Mutagen
    session — is removed.
  * **zombie_session** — a session ``halted-on-root-deletion`` (its local
    ``workspace/`` root was deleted) while the agent dir is otherwise intact.
    The session is terminated; ``cinna dev`` recreates it cleanly.
  * **dead_remote**    — a session stuck retrying a remote env that is gone
    (``connecting-beta`` / beta polling error). Terminated.
  * **orphan_session** — a ``cinna-*`` session with no registry entry at all.
    Terminated.
  * **token_remint**   — an expired CLI token on an account-managed workspace.
    Re-minted automatically through the parent account token (no paste).
  * **token_report**   — an expired CLI token on a standalone workspace, which
    can only be refreshed with a pasted setup token. Reported, never changed.

Why a dedicated command rather than self-healing sessions: Mutagen has **no**
"give up after N failures" knob — a session retries a dead remote forever until
something pauses or terminates it — so the leftovers need an explicit sweep.

The repair is offered as three ordered, independently-confirmed steps — each
defaulting to **Yes**:

  1. **delete stalled sessions** — the broken state above (deleted workspaces,
     halted / dead / orphaned sessions).
  2. **terminate active sessions** — the healthy, still-watching sessions left
     over from past ``cinna dev`` runs. They are recreated on demand, so clearing
     them just frees the shared Mutagen daemon.
  3. **refresh tokens** — re-mint expired CLI tokens via the parent account.

The live ``cinna-*`` session inventory — each tagged with the agent and folder
it serves — is shown up front on every run.
"""

import logging
import platform as _platform
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import click

from cinna import console
from cinna import sync_session
from cinna.config import (
    CONFIG_DIR,
    CONFIG_FILE,
    CinnaConfig,
    list_agent_registry,
    load_config,
    remove_agent_registry,
    save_config,
    upsert_agent_registry,
)

logger = logging.getLogger("cinna.doctor")


# Category key → human header, in the order findings are presented and applied.
# Session teardown runs before token re-mint so a healed agent ends up with a
# fresh token and no dangling session, ready for the next ``cinna dev``.
CATEGORY_ORDER: list[tuple[str, str]] = [
    ("stale_folder", "Workspace folder deleted"),
    ("zombie_session", "Session halted (local root deleted)"),
    ("dead_remote", "Session stuck on an unreachable remote"),
    ("orphan_session", "Orphaned session (no registry entry)"),
    ("token_remint", "Expired token — account re-mint"),
    ("account_token_expired", "Account token expired — renew it"),
    ("token_report", "Expired token — manual refresh needed"),
    ("cli_outdated", "cinna-cli differs from the platform pin"),
]


@dataclass
class Finding:
    category: str
    label: str  # agent display name / session name
    detail: str  # what's wrong
    fix: str  # the planned action (human text)
    apply: Callable[[], str] | None  # None ⇒ report-only (no fix)
    session: str | None = None  # Mutagen session this finding owns, if any


@dataclass
class SessionInfo:
    """A live ``cinna-*`` Mutagen session plus the agent/folder it belongs to."""

    name: str  # Mutagen session name (cinna-<short-id>)
    agent: str  # agent display name
    folder: str  # workspace folder on disk


def _daemon_config(entries: list[dict]) -> CinnaConfig:
    """A throwaway config that only carries the Mutagen daemon env.

    Daemon-level ops (``sync list`` / ``sync terminate``) need a ``CinnaConfig``
    purely for ``MUTAGEN_SSH_PATH``, which is identical for every agent on the
    machine, so the first registry entry (or empty defaults) is sufficient.
    """
    probe = entries[0] if entries else {}
    return CinnaConfig(
        platform_url=probe.get("platform_url", ""),
        cli_token=probe.get("cli_token", ""),
        agent_id=probe.get("agent_id", "doctor"),
        agent_name="",
        environment_id="",
        template="",
    )


def _label_for(entry: dict) -> str:
    """Display label: the agent's name when its config is readable, else a
    short id."""
    root = Path(entry.get("workspace_path", ""))
    if root and root.exists():
        try:
            return load_config(root).agent_name
        except Exception:
            pass
    return entry["agent_id"][:8]


def _account_root_for(workspace_root: Path) -> Path | None:
    """The account workspace above ``workspace_root``, or None if standalone.

    Account-minted child workspaces live at ``<account_root>/agents/<slug>/``,
    so walking up finds the ``.cinna/account.json`` whose token can re-mint the
    agent's CLI token without a pasted setup token.
    """
    from cinna.account import find_account_root
    from cinna.errors import AccountConfigNotFoundError

    try:
        return find_account_root(start=workspace_root)
    except AccountConfigNotFoundError:
        return None


def _probe_account_token(account_root: Path) -> str:
    """Classify the account workspace's own token: valid / expired / unreachable.

    A sub-agent token can only be re-minted while the **account** token that
    mints it is still valid. When the account token has itself expired, every
    re-mint would 401 — so doctor probes it once per account root and, on
    expiry, surfaces a single "renew the account token" finding instead of a
    pile of doomed per-agent re-mints.
    """
    from cinna.account import load_account_config, probe_account_token

    try:
        account_cfg = load_account_config(account_root)
    except Exception:
        return "unreachable"
    return probe_account_token(account_cfg)


# ── fix factories (closures bound to their target, no late-binding) ──────────


def _make_terminate(name: str, cfg: CinnaConfig) -> Callable[[], str]:
    def apply() -> str:
        if not sync_session.terminate_named(name, cfg):
            raise click.ClickException(f"mutagen could not terminate {name}")
        return "session terminated"

    return apply


def _make_stale_fix(
    agent_id: str, session_to_kill: str | None, cfg: CinnaConfig
) -> Callable[[], str]:
    def apply() -> str:
        msgs: list[str] = []
        if session_to_kill and sync_session.terminate_named(session_to_kill, cfg):
            msgs.append("session terminated")
        remove_agent_registry(agent_id)
        msgs.append("registry entry removed")
        return ", ".join(msgs)

    return apply


def _make_remint(agent_id: str, workspace_root: Path) -> Callable[[], str]:
    def apply() -> str:
        from cinna.account import find_account_root, load_account_config
        from cinna.client import AccountClient

        account_root = find_account_root(start=workspace_root)
        account_cfg = load_account_config(account_root)
        machine_info = f"{_platform.system()}/{_platform.machine()}"
        with AccountClient(account_cfg) as client:
            mint = client.mint_agent_token(
                agent_id, account_cfg.machine_name, machine_info
            )
        minted_id = mint.get("agent_id")
        if minted_id and minted_id != agent_id:
            raise click.ClickException(
                f"account minted a token for a different agent ({minted_id})"
            )
        config = load_config(workspace_root)
        config.cli_token = mint["token"]
        if mint.get("id"):
            config.cli_token_id = mint["id"]
        if mint.get("frontend_url"):
            config.frontend_url = mint["frontend_url"]
        save_config(config, workspace_root)
        upsert_agent_registry(
            config.agent_id,
            config.platform_url,
            config.cli_token,
            workspace_root,
            frontend_url=config.frontend_url,
        )
        return "token re-minted via account"

    return apply


# ── diagnosis ────────────────────────────────────────────────────────────────


def diagnose() -> list[Finding]:
    """Reconcile the registry against the Mutagen daemon; return all findings."""
    from cinna.main import _probe_token_statuses

    entries = list_agent_registry()
    cfg = _daemon_config(entries)

    try:
        sessions = sync_session.list_all_sessions(cfg)
    except Exception as exc:  # daemon down / mutagen missing — registry-only run
        logger.warning("Could not list Mutagen sessions: %s", exc)
        sessions = []
    sessions_by_name = {s.get("name"): s for s in sessions if s.get("name")}

    token_statuses = _probe_token_statuses(entries) if entries else {}

    findings: list[Finding] = []
    claimed_sessions: set[str] = set()
    expired_tokens: list[tuple[str, Path, str]] = []  # (agent_id, root, label)

    for entry in entries:
        agent_id = entry["agent_id"]
        root = Path(entry.get("workspace_path", ""))
        sname = sync_session.session_name(agent_id)
        session = sessions_by_name.get(sname)
        label = _label_for(entry)

        workspace_intact = (
            bool(entry.get("workspace_path"))
            and root.exists()
            and (root / CONFIG_DIR / CONFIG_FILE).is_file()
        )

        if not workspace_intact:
            # Folder (or its .cinna/config.json) is gone — drop the entry, and
            # any leftover session with it. This single category covers points
            # 1–3: deleted folders, with or without a lingering session.
            claimed_sessions.add(sname)
            where = entry.get("workspace_path") or "?"
            findings.append(
                Finding(
                    "stale_folder",
                    label,
                    f"workspace missing: {where}",
                    "remove registry entry"
                    + (" + terminate session" if session else ""),
                    _make_stale_fix(agent_id, sname if session else None, cfg),
                    session=sname if session else None,
                )
            )
            continue

        # Workspace intact → inspect the session (if any) then the token.
        if session is not None:
            claimed_sessions.add(sname)
            status = (session.get("status") or "").lower()
            beta_connected = bool((session.get("beta") or {}).get("connected"))
            if status == "halted-on-root-deletion":
                findings.append(
                    Finding(
                        "zombie_session",
                        label,
                        "session halted — local workspace/ root was deleted",
                        "terminate session (cinna dev recreates it)",
                        _make_terminate(sname, cfg),
                        session=sname,
                    )
                )
            elif (
                not beta_connected
                or session.get("lastError")
                or status.startswith("connecting")
                or "error" in status
            ):
                findings.append(
                    Finding(
                        "dead_remote",
                        label,
                        "session can't reach the remote env "
                        f"(status: {status or 'unknown'})",
                        "terminate session",
                        _make_terminate(sname, cfg),
                        session=sname,
                    )
                )

        if token_statuses.get(agent_id) == "expired":
            expired_tokens.append((agent_id, root, label))

    # Any cinna-* session not tied to a registry entry is orphaned.
    for name, session in sessions_by_name.items():
        if not name.startswith("cinna-") or name in claimed_sessions:
            continue
        apath = (session.get("alpha") or {}).get("path", "")
        derived = Path(apath).parent.name if apath else ""
        label = f"{derived} ({name})" if derived else name
        findings.append(
            Finding(
                "orphan_session",
                label,
                "Mutagen session has no registry entry",
                "terminate session",
                _make_terminate(name, cfg),
                session=name,
            )
        )

    # Resolve expired tokens last, so each account token is probed at most once
    # and agents under an account whose token has itself expired are grouped
    # into a single "renew the account token" finding rather than a pile of
    # re-mints that would all 401.
    account_status: dict[Path, str] = {}
    account_blocked: dict[Path, list[str]] = {}
    for agent_id, root, label in expired_tokens:
        account_root = _account_root_for(root)
        if account_root is None:
            findings.append(
                Finding(
                    "token_report",
                    label,
                    "CLI token expired (standalone workspace)",
                    "run 'cinna set-token' from its workspace",
                    None,
                )
            )
            continue
        status = account_status.get(account_root)
        if status is None:
            status = _probe_account_token(account_root)
            account_status[account_root] = status
        if status == "expired":
            account_blocked.setdefault(account_root, []).append(label)
        else:
            # valid (re-mint will work) or unreachable (best-effort; the apply
            # surfaces a clear error if the account is actually down).
            findings.append(
                Finding(
                    "token_remint",
                    label,
                    "CLI token expired (account-managed)",
                    "re-mint via the parent account token",
                    _make_remint(agent_id, root),
                )
            )

    for account_root, labels in sorted(account_blocked.items()):
        findings.append(
            Finding(
                "account_token_expired",
                account_root.name,
                f"account token expired — {len(labels)} sub-agent token(s) "
                f"can't be re-minted ({', '.join(labels)})",
                f"renew the account: run 'cinna login' in {account_root} "
                f"(or 'cinna account set-token <token>' with a fresh setup token)",
                None,
            )
        )

    return findings


def _cli_version_findings(entries: list[dict]) -> list[Finding]:
    """Report-only findings for platforms whose cinna-cli pin differs from the
    running version — one per platform, drawn from the registry entries and
    the account workspace the command runs in (if any). Platforms that do
    not publish a pin yield nothing."""
    from cinna.account import find_account_root, load_account_config
    from cinna.cli_version import cli_version_hint, cli_version_status
    from cinna.errors import AccountConfigNotFoundError

    platforms: dict[str, str] = {}
    for entry in entries:
        url = (entry.get("platform_url") or "").rstrip("/")
        if url:
            platforms.setdefault(url, url)
    try:
        root = find_account_root()
        url = load_account_config(root).platform_url.rstrip("/")
        platforms[url] = str(root)
    except (AccountConfigNotFoundError, Exception):
        pass

    findings: list[Finding] = []
    for url, label in sorted(platforms.items()):
        status = cli_version_status(url)
        hint = cli_version_hint(status)
        if not hint:
            continue
        findings.append(
            Finding(
                "cli_outdated",
                label,
                f"cinna-cli {status['installed']} installed, platform pins "
                f"{status['required']}",
                f"uv tool install cinna-cli=={status['required']}",
                None,
            )
        )
    return findings


# ── command body ─────────────────────────────────────────────────────────────


def _in_category_order(findings: list[Finding]) -> list[Finding]:
    """Stable order: group findings by CATEGORY_ORDER (apply order = display
    order)."""
    rank = {cat: i for i, (cat, _) in enumerate(CATEGORY_ORDER)}
    return sorted(findings, key=lambda f: rank.get(f.category, len(rank)))


def _print_table(
    findings: list[Finding], title: str, fix_header: str, dim_fix: bool = False
) -> None:
    from rich.table import Table

    table = Table(
        title=f"{title} ({len(findings)})",
        title_style="bold",
        show_lines=True,
    )
    table.add_column("#", style="dim", justify="right")
    table.add_column("Agent / Session")
    table.add_column("Problem")
    table.add_column(fix_header)

    for i, f in enumerate(_in_category_order(findings), 1):
        fix = f"[dim]{f.fix}[/dim]" if dim_fix else f.fix
        table.add_row(str(i), f"[bold]{f.label}[/bold]", f.detail, fix)

    console.console.print(table)


# Auto-fixable findings that represent broken / stale Mutagen sessions or the
# registry state around them. Grouped under the "delete stalled sessions" prompt.
_STALLED_CATEGORIES = {
    "stale_folder",
    "zombie_session",
    "dead_remote",
    "orphan_session",
}


def _collect_cinna_sessions(
    entries: list[dict], cfg: CinnaConfig
) -> list[SessionInfo]:
    """Every live ``cinna-*`` session, tagged with the agent + folder it serves.

    Scoped to ``cinna-*`` on purpose: the Mutagen daemon is shared across every
    tool on the machine (docs/mutagen_capabilities.md §10), so doctor must never
    report — let alone terminate — another consumer's sessions.
    """
    try:
        sessions = sync_session.list_all_sessions(cfg)
    except Exception as exc:  # daemon down / mutagen missing — nothing to show
        logger.warning("Could not list Mutagen sessions: %s", exc)
        return []

    by_name = {
        sync_session.session_name(e["agent_id"]): e for e in entries
    }
    infos: list[SessionInfo] = []
    for s in sessions:
        name = s.get("name")
        if not name or not name.startswith("cinna-"):
            continue
        entry = by_name.get(name)
        if entry is not None:
            agent = _label_for(entry)
            folder = entry.get("workspace_path") or "?"
        else:
            # Orphan: no registry entry — derive what we can from the sync root.
            apath = (s.get("alpha") or {}).get("path", "")
            parent = Path(apath).parent if apath else None
            agent = parent.name if parent else name
            folder = str(parent) if parent else "?"
        infos.append(SessionInfo(name=name, agent=agent, folder=folder))
    return infos


def _print_sessions_table(infos: list[SessionInfo], title: str) -> None:
    from rich.table import Table

    table = Table(
        title=f"{title} ({len(infos)})",
        title_style="bold",
        show_lines=True,
    )
    table.add_column("#", style="dim", justify="right")
    table.add_column("Session")
    table.add_column("Agent")
    table.add_column("Folder")

    for i, s in enumerate(infos, 1):
        table.add_row(str(i), s.name, f"[bold]{s.agent}[/bold]", s.folder)

    console.console.print(table)


def _apply_step(
    findings: list[Finding], prompt: str, yes: bool, skip_message: str
) -> int:
    """Confirm (default Yes), then apply a group of actionable findings.

    Returns the number applied. ``--yes`` skips the prompt; declining prints
    ``skip_message`` and applies nothing.
    """
    if not findings:
        return 0
    if not (yes or console.confirm(prompt, default=True)):
        console.warn(skip_message)
        return 0
    applied = 0
    for f in _in_category_order(findings):
        try:
            console.status(f"{f.label}: {f.apply()}")
            applied += 1
        except Exception as exc:
            console.error(f"{f.label}: {exc}")
    return applied


def run_doctor(dry_run: bool, yes: bool) -> None:
    """Scan, report, and (unless ``dry_run``) repair stale sync state.

    The repair walks three ordered, independently-confirmed steps — each
    defaulting to Yes:

      1. **Delete stalled sessions** — deleted workspaces and halted / dead /
         orphaned Mutagen sessions.
      2. **Terminate active sessions** — the healthy, still-watching sessions
         left over from past ``cinna dev`` runs (recreated on demand, so safe to
         clear and free the shared daemon).
      3. **Refresh tokens** — re-mint expired CLI tokens via the parent account.

    Findings doctor can't fix itself (standalone expired tokens) are reported
    only, never touched. The live-session inventory — with the agent and folder
    each belongs to — is shown up front, on every run.
    """
    with console.spinner("Scanning registry and Mutagen sessions…"):
        findings = diagnose()

    entries = list_agent_registry()
    findings.extend(_cli_version_findings(entries))
    cfg = _daemon_config(entries)
    live = _collect_cinna_sessions(entries, cfg)

    stalled = [f for f in findings if f.category in _STALLED_CATEGORIES]
    remint = [f for f in findings if f.category == "token_remint"]
    manual = [f for f in findings if f.apply is None]

    # "Active" = live sessions not already accounted for as stalled/broken.
    problem_sessions = {f.session for f in findings if f.session}
    active = [s for s in live if s.name not in problem_sessions]

    if not findings and not active:
        console.status("Everything looks healthy — no stale sync state found.")
        return

    if not findings:
        console.status("No problems found — only leftover sessions to tidy up.")

    # ── report (shown on every run, including --dry-run) ──────────────────────
    sections = 0

    def _gap() -> None:
        nonlocal sections
        if sections:
            console.console.print()
        sections += 1

    if stalled:
        _gap()
        _print_table(stalled, "Stalled sessions / state", "Planned fix")
    if active:
        _gap()
        _print_sessions_table(active, "Active Mutagen sessions")
    if remint:
        _gap()
        _print_table(remint, "Expired tokens — account re-mint", "Planned fix")
    if manual:
        _gap()
        _print_table(
            manual,
            "No automatic fix — manual action needed",
            "What to do",
            dim_fix=True,
        )
    console.console.print()

    if dry_run:
        console.warn("Dry run — nothing changed. Re-run without --dry-run to apply.")
        return

    applied = 0
    terminated = 0

    # Step 1 — delete stalled sessions / stale registry state.
    applied += _apply_step(
        stalled,
        f"Delete {len(stalled)} stalled session(s)?",
        yes,
        skip_message="No stalled sessions deleted.",
    )

    # Step 2 — terminate the healthy, no-longer-needed active sessions.
    if active:
        if yes or console.confirm(
            f"Terminate {len(active)} active session(s)?", default=True
        ):
            for s in active:
                if sync_session.terminate_named(s.name, cfg):
                    console.status(f"{s.name}: session terminated")
                    terminated += 1
                else:
                    console.error(f"{s.name}: could not terminate")
        else:
            console.warn("Sessions left running.")

    # Step 3 — refresh expired tokens (account re-mint).
    applied += _apply_step(
        remint,
        f"Refresh {len(remint)} expired token(s)?",
        yes,
        skip_message="No tokens refreshed.",
    )

    if manual:
        console.console.print()
        console.warn(
            "Standalone agents with expired tokens have no account to re-mint "
            "from — refresh each from its own workspace:"
        )
        for f in manual:
            console.console.print(
                f"  • {f.label} — run [bold]cinna set-token <token>[/bold]"
            )

    console.console.print()
    if findings:
        console.status(f"doctor applied {applied} fix(es).")
    if terminated:
        console.status(f"doctor terminated {terminated} session(s).")
