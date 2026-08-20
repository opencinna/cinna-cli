"""Improvement requests — the `cinna improve` verbs.

An **improvement request** is a consent-gated, one-directional share: a user who
was chatting with an agent hit a bad answer and deliberately handed the *agent's
owner* a frozen snapshot of that one session plus the runtime context that
produced it (bundle version, environment, SDK engine, effective model). The
platform stores it against the receiving agent; this module is the account
workspace's side of the loop — discover, read, download, close.

Everything here runs against the dedicated account routes
(``/api/v1/cli/account/improvement-requests*``) with the account token, so a
single account workspace sees the requests landing on *every* agent it owns.
The archive is a binary ZIP, which is why it has its own route rather than
riding the JSON-only api-proxy.

The intended operator is a local coding agent following the shipped playbook at
``context/guides/handling-improvement-requests.md`` (installed by
``cinna account setup`` / ``refresh-context``): list → show → download → fix →
``status … completed``. The archive is another person's conversation — the
download command says so out loud, deliberately.
"""

import json
import logging
import uuid
from pathlib import Path

import click

from cinna import console
from cinna.account import (
    _resolve_account_agent,
    context_package_hint,
    find_account_root,
    load_account_config,
)
from cinna.client import AccountClient

logger = logging.getLogger("cinna.improve")

# Mirrors the backend's ``IMPROVEMENT_STATUSES`` (a plain VARCHAR there, so new
# values need no migration). Validated client-side purely for a better error
# than a 400 round-trip; an unknown value the backend later adds still works
# because the list is only consulted for the friendly message.
IMPROVEMENT_STATUSES = ("new", "in_progress", "completed", "declined")

# The short id printed in the table and used as the download folder name — the
# same 8-char prefix the web UI's detail modal offers for copy-to-clipboard.
SHORT_ID_LEN = 8

DOWNLOAD_DIR = "improvements"

_STATUS_STYLE = {
    "new": "magenta",
    "in_progress": "blue",
    "completed": "green",
    "declined": "dim",
}


# ── Helpers ─────────────────────────────────────────────────────────────────


def _short_id(request_id: str) -> str:
    return str(request_id)[:SHORT_ID_LEN]


def _status_cell(status: str | None) -> str:
    label = status or "?"
    style = _STATUS_STYLE.get(label)
    return f"[{style}]{label}[/{style}]" if style else label


def _normalize_status(raw: str) -> str:
    """Accept ``in-progress`` / mixed case for the underscore-cased vocabulary."""
    value = raw.strip().lower().replace("-", "_")
    if value not in IMPROVEMENT_STATUSES:
        raise click.ClickException(
            f"Unknown status '{raw}'. Valid statuses: "
            f"{', '.join(IMPROVEMENT_STATUSES)}."
        )
    return value


def _fmt_ts(raw: str | None) -> str:
    """ISO-8601 → ``YYYY-MM-DD HH:MM`` (UTC), falling back to the raw string."""
    if not raw:
        return "—"
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return str(raw)
    return parsed.strftime("%Y-%m-%d %H:%M")


def _truncate(text: str | None, limit: int) -> str:
    if not text:
        return "[dim]—[/dim]"
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _resolve_request_id(client: AccountClient, ref: str) -> str:
    """Resolve a full UUID or a short-id prefix to the full request id.

    The table (and the web UI's copy button) show the 8-char prefix, so the
    prefix is what a human or a coding agent naturally pastes back. A full UUID
    is passed straight through without a listing round-trip.
    """
    ref = ref.strip()
    try:
        return str(uuid.UUID(ref))
    except ValueError:
        pass

    listing = client.list_improvement_requests(limit=200)
    items = listing.get("data", [])
    matches = [r for r in items if str(r.get("id", "")).startswith(ref.lower())]
    if len(matches) == 1:
        return str(matches[0]["id"])
    if len(matches) > 1:
        ids = ", ".join(_short_id(r["id"]) for r in matches)
        raise click.ClickException(
            f"Request id '{ref}' is ambiguous — matches: {ids}.\n"
            f"Use the full id from 'cinna improve list'."
        )
    raise click.ClickException(
        f"No improvement request matching '{ref}'.\n"
        f"Run 'cinna improve list' to see the requests on your agents."
    )


def _resolve_agent_id(client: AccountClient, agent_ref: str) -> str:
    """Map a `--agent` name / slug / id onto an agent id for the list filter."""
    listing = client.list_account_agents()
    agent = _resolve_account_agent(listing.get("data", []), agent_ref)
    return str(agent["id"])


# ── list ────────────────────────────────────────────────────────────────────


def run_improve_list(
    status: str | None = None,
    agent_ref: str | None = None,
    limit: int = 50,
    as_json: bool = False,
) -> None:
    """List the improvement requests landing on the agents this account owns.

    Called by ``cinna improve list``. Cross-agent by default — the whole point
    of running it from the account workspace — with ``--status`` / ``--agent``
    to narrow. Ordering is the backend's: unhandled first, then newest.
    """
    from rich.table import Table

    account_root = find_account_root()
    account_cfg = load_account_config(account_root)
    status_filter = _normalize_status(status) if status else None

    with AccountClient(account_cfg) as client:
        agent_id = _resolve_agent_id(client, agent_ref) if agent_ref else None
        with console.spinner("Fetching improvement requests..."):
            listing = client.list_improvement_requests(
                status=status_filter, agent_id=agent_id, limit=limit
            )
        package_state = _package_state(account_cfg, account_root, client)

    items = listing.get("data", [])

    if as_json:
        click.echo(json.dumps(listing, indent=2, default=str))
        return

    if not items:
        scope = []
        if status_filter:
            scope.append(f"status '{status_filter}'")
        if agent_ref:
            scope.append(f"agent '{agent_ref}'")
        suffix = f" matching {' and '.join(scope)}" if scope else ""
        console.status(f"No improvement requests{suffix}.")
        console.console.print(
            "[dim]Users share a session from the session menu (Improve Agent) or "
            "with the /session-improve command.[/dim]"
        )
        return

    total = listing.get("count", len(items))
    title = f"Improvement requests ({len(items)} of {total})"
    table = Table(title=title, title_style="bold", show_lines=True)
    table.add_column("#", style="dim", justify="right")
    table.add_column("Id", style="bold")
    table.add_column("Agent")
    table.add_column("Session")
    table.add_column("Requester")
    table.add_column("Version")
    table.add_column("Reported")
    table.add_column("Created")
    table.add_column("Status")

    # Re-submitting `/session-improve` on one session creates a second row with
    # the same transcript. Counting them here means a reader sees the duplication
    # in the listing instead of discovering it after downloading two archives.
    session_counts: dict[str, int] = {}
    for item in items:
        session = item.get("session_id")
        if session:
            session_counts[str(session)] = session_counts.get(str(session), 0) + 1

    for i, item in enumerate(items, 1):
        # A bundle install without a version label is still a bundle install —
        # only the absence of a bundle id makes an agent standalone.
        version = item.get("installed_version")
        bundle_id = item.get("bundle_id")
        if item.get("is_bundle_install") or bundle_id:
            version_cell = f"{version or '[dim]unversioned[/dim]'}\n[dim]{bundle_id or '?'}[/dim]"
        elif version:
            version_cell = str(version)
        else:
            version_cell = "[dim]standalone[/dim]"

        requester = item.get("requester_display") or item.get("requester_email") or "—"

        session = str(item.get("session_id") or "")
        if not session:
            session_cell = "[dim]—[/dim]"
        else:
            session_cell = _short_id(session)
            if session_counts.get(session, 0) > 1:
                session_cell += (
                    f"\n[yellow]×{session_counts[session]} same session[/yellow]"
                )

        table.add_row(
            str(i),
            _short_id(item.get("id", "")),
            item.get("target_agent_name") or "[dim]?[/dim]",
            session_cell,
            _truncate(requester, 24),
            version_cell,
            _truncate(item.get("comment"), 48),
            _fmt_ts(item.get("created_at")),
            _status_cell(item.get("status")),
        )

    console.console.print(table)
    console.console.print(
        "[dim]Next: 'cinna improve show <id>' for the detail, "
        "'cinna improve download <id>' for the session archive.[/dim]"
    )
    hint = context_package_hint(package_state)
    if hint:
        console.console.print(hint)


def _package_state(account_cfg, account_root, client) -> str:
    """Classify the workspace's context package, reusing the open client.

    Working this queue well depends on
    ``context/guides/handling-improvement-requests.md``; a workspace created
    before that guide existed has it silently missing. Best-effort — a failure
    here must never cost the caller their listing.
    """
    from cinna.account import context_package_status

    try:
        state, _local, _remote = context_package_status(
            account_cfg, account_root, client=client
        )
        return state
    except Exception as exc:  # noqa: BLE001 — advisory only
        logger.debug("Context package check skipped: %s", exc)
        return "unreachable"


# ── show ────────────────────────────────────────────────────────────────────


# Prompt fields carried in the context's ``prompts`` block (schema >= 2), in the
# order they matter when reading a report: the workflow prompt drives behaviour,
# the router trigger only decides routing.
PROMPT_FIELDS = (
    ("workflow", "workflow"),
    ("entrypoint", "entrypoint"),
    ("refiner", "refiner"),
    ("router_trigger", "router trigger"),
)


def _memory_summary(memory: dict) -> str | None:
    """One-line summary of the agent's captured personal memory."""
    if not memory:
        return None
    if not memory.get("available"):
        return f"[dim]none ({memory.get('unavailable_reason') or 'unavailable'})[/dim]"
    summary = (
        f"{memory.get('file_count', 0)} file(s), {memory.get('total_chars', 0):,} chars"
    )
    if memory.get("truncated"):
        summary += " [yellow](truncated)[/yellow]"
    return summary


def _fix_location_hint(context: dict) -> str | None:
    """State where a fix belongs, from the context alone.

    The context block describes the *requester's* install; the request landed on
    the *target* agent, which for a bundle is a different row with the opposite
    ownership. Leaving the reader to join those two facts is how a perfectly
    actionable request gets misread as "not mine to fix", so the conclusion is
    printed rather than implied.
    """
    agent = context.get("agent") or {}
    recipient = context.get("recipient") or {}
    bundle = agent.get("bundle_id") or "this bundle"

    if recipient.get("fallback_reason"):
        return (
            f"[yellow]![/yellow] Recipient fallback "
            f"({recipient['fallback_reason']}) — this landed on your own install, "
            f"not a publisher's. A local change here is overwritten by the next "
            f"bundle update; forward the feedback to the publisher instead."
        )
    if not agent.get("is_bundle_install"):
        return (
            "Where a fix belongs: this standalone agent's synced workspace. "
            "Nothing to publish."
        )
    same_row = (
        agent.get("source_agent_id")
        and agent.get("source_agent_id") == recipient.get("target_agent_id")
    )
    if same_row:
        return (
            f"Where a fix belongs: [bold]this agent[/bold] — your publisher "
            f"install of `{bundle}`, which is also the install that reported it. "
            f"Fix here, then publish a new version."
        )
    return (
        f"Where a fix belongs: [bold]your publisher install[/bold] of `{bundle}` "
        f"(the target agent above), not the consumer copy this was reported from. "
        f"Fix there, then publish a new version so installs pick it up."
    )


def _print_prompts(prompts: dict) -> None:
    """Render the per-prompt divergence table (context schema >= 2).

    This block answers the question a publisher cannot answer from their own
    copy: is this install running *my* prompts, or the consumer's edit of them?
    A diverged prompt explains behaviour that is otherwise unreproducible. The
    full texts are not printed here — they ride in the archive under
    ``agent/prompts/``.
    """
    from rich.table import Table

    rows = [
        (label, prompts.get(key))
        for key, label in PROMPT_FIELDS
        if isinstance(prompts.get(key), dict)
        and (prompts[key].get("chars") or prompts[key].get("text"))
    ]
    if not rows:
        return

    baseline = prompts.get("baseline_version")
    diverged = prompts.get("diverged")
    if diverged is None:
        headline = "[dim]no baseline to compare against[/dim]"
    elif diverged:
        headline = "[yellow]diverged[/yellow]"
    else:
        headline = "[dim]in sync[/dim]"
    against = f" vs installed v{baseline}" if baseline else ""
    # show_lines: a "not compared" cell carries its reason on a second line, and
    # without separators that reads as a phantom extra prompt row.
    table = Table(title=f"Prompts — {headline}{against}", show_lines=True)
    table.add_column("Prompt", style="dim")
    table.add_column("Size")
    table.add_column("vs bundle")
    table.add_column("Edited")

    for label, block in rows:
        size = f"{block.get('chars', 0):,} chars"
        if block.get("truncated"):
            size += " [yellow](truncated)[/yellow]"
        # Tri-state: True diverged, False in sync, None *not compared* — the last
        # must never read as "in sync", which would assert a check that never ran.
        flag = block.get("diverged_from_installed_revision")
        if flag is None:
            state = "[dim]not compared[/dim]"
            reason = block.get("divergence_reason")
            if reason:
                state += f"\n[dim]{str(reason).replace('_', ' ')}[/dim]"
        elif flag:
            state = "[yellow]diverged[/yellow]"
        else:
            state = "[dim]in sync[/dim]"
        role = block.get("role")
        name = (
            label
            if role in (None, "published_prompt")
            else f"{label}\n[dim]{str(role).replace('_', ' ')}[/dim]"
        )
        table.add_row(name, size, state, _fmt_ts(block.get("updated_at")))

    console.console.print()
    console.console.print(table)

    # `allowed_tools` is an AUTO-APPROVAL list, not a restriction: a tool the
    # agent requested but the owner did not auto-approve prompted the user on
    # every use — a common cause of a run that looks stuck. Empty and
    # never-configured both render as "[]", so they get sentences, not a dash.
    sdk_tools = prompts.get("sdk_tools") or []
    allowed = prompts.get("allowed_tools")
    if sdk_tools or allowed is not None:
        detail = f"{len(sdk_tools)} tool(s) requested by the agent; "
        if allowed is None:
            detail += "no auto-approval list — every tool use prompted the user"
        elif not allowed:
            detail += "none auto-approved — every tool use prompted the user"
        else:
            detail += f"{len(allowed)} auto-approved: {', '.join(allowed)}"
        console.console.print(f"[dim]{detail}[/dim]")
    if prompts.get("diverged"):
        console.console.print(
            "[yellow]![/yellow] This install's prompts differ from the bundle "
            "revision it runs — read them in the archive before assuming your "
            "own copy is what produced this session."
        )


def _revision_label(agent: dict, which: str) -> str | None:
    """``v1.3 (revision 7)`` — omitting whichever half the platform didn't record.

    ``which`` is a key prefix: ``installed`` or ``latest_published`` (context
    schema >= 3), falling back to the schema-2 ``latest`` on a frozen older row.
    """
    version = agent.get(f"{which}_version")
    revision = agent.get(f"{which}_revision_number")
    parts = []
    if version:
        parts.append(f"v{version}")
    if revision is not None:
        parts.append(f"(revision {revision})")
    return " ".join(parts) or None


def _origin_label(origin: str | None) -> str:
    """Where a revision came from — a publish, or a git-source import."""
    return f" · {origin}" if origin else ""


def _print_context(context: dict) -> None:
    """Render the frozen runtime context — the tuning-relevant half of a request.

    Describes the **requester's** install (the one that misbehaved), which is
    usually a different row from the agent the request landed on.
    """
    from rich.table import Table

    agent = context.get("agent") or {}
    env = context.get("environment") or {}
    sdk = context.get("sdk") or {}
    recipient = context.get("recipient") or {}
    plugins = context.get("plugins") or []

    table = Table(
        title="Runtime context — the requester's install (the copy that misbehaved)",
        show_lines=False,
    )
    table.add_column("Property", style="dim")
    table.add_column("Value")

    def add(label: str, value) -> None:
        if value is None or value == "":
            return
        table.add_row(label, str(value))

    add("Source agent", agent.get("name"))
    if agent.get("is_bundle_install"):
        add("Bundle", agent.get("bundle_id"))
        installed = _revision_label(agent, "installed")
        if installed:
            add("Installed", installed + _origin_label(agent.get("installed_revision_origin")))
        # Schema 3 renamed the pair to `latest_published_*` and added a separate
        # head; a frozen schema-2 row still carries the old `latest_*` keys.
        add(
            "Latest published",
            _revision_label(agent, "latest_published") or _revision_label(agent, "latest"),
        )
        head = agent.get("head_revision_number")
        if head is not None and head != agent.get("latest_published_revision_number"):
            add("Head revision", f"revision {head} (not published)")
        add("Update pending", "yes" if agent.get("update_pending") else "no")
        add(
            "Requester's install",
            "publisher install"
            if agent.get("is_publisher_install")
            else "consumer install",
        )
    else:
        add("Requester's install", "standalone (not a bundle)")

    add("Session mode", sdk.get("session_mode"))
    add("Engine", sdk.get("effective_engine"))
    add("Model", sdk.get("effective_model"))
    add("Model override (conversation)", sdk.get("model_override_conversation"))
    add("Model override (building)", sdk.get("model_override_building"))

    add("Environment", env.get("env_name"))
    add("Env version", env.get("env_version"))
    add("Instance", env.get("instance_name"))
    add("Env status at capture", env.get("status_at_capture"))
    if env.get("image_stale"):
        add("Image", f"stale (current {env.get('current_image_tag')})")
    if env.get("critical_state"):
        add("Critical state", env.get("critical_cause") or "yes")

    add("Plugins", ", ".join(p.get("name", "?") for p in plugins) if plugins else None)
    add("Personal memory", _memory_summary(context.get("memory") or {}))
    add("Recipient", recipient.get("owner_display"))
    add("Shared outside the requester's account", "yes" if recipient.get("is_shared_externally") else "no")
    add("Fallback reason", recipient.get("fallback_reason"))
    scrubbed = (context.get("platform") or {}).get("scrubbed_hits") or 0
    if scrubbed:
        add("Secrets scrubbed", f"{scrubbed} occurrence(s) masked in the snapshot")

    console.console.print(table)
    hint = _fix_location_hint(context)
    if hint:
        console.console.print(hint)
    _print_prompts(context.get("prompts") or {})


def run_improve_show(request_ref: str, as_json: bool = False) -> None:
    """Print one request in full — called by ``cinna improve show <id>``.

    Accepts the short id from the list table or a full UUID.
    """
    from rich.panel import Panel
    from rich.table import Table

    account_cfg = load_account_config(find_account_root())

    with AccountClient(account_cfg) as client:
        request_id = _resolve_request_id(client, request_ref)
        with console.spinner("Fetching improvement request..."):
            detail = client.get_improvement_request(request_id)

    if as_json:
        click.echo(json.dumps(detail, indent=2, default=str))
        return

    table = Table(title=f"Improvement request {_short_id(detail.get('id', ''))}", show_lines=False)
    table.add_column("Property", style="dim")
    table.add_column("Value")
    table.add_row("Id", str(detail.get("id", "?")))
    table.add_row("Status", _status_cell(detail.get("status")))
    table.add_row(
        "Target agent",
        f"{detail.get('target_agent_name') or '?'}\n"
        f"[dim]{detail.get('target_agent_id') or '?'}[/dim]",
    )
    if detail.get("source_agent_name"):
        table.add_row("Source agent", str(detail["source_agent_name"]))
    if detail.get("bundle_id"):
        table.add_row("Bundle", str(detail["bundle_id"]))
    if detail.get("installed_version"):
        table.add_row("Installed version", str(detail["installed_version"]))
    requester = detail.get("requester_display") or "—"
    if detail.get("requester_email"):
        requester += f"\n[dim]{detail['requester_email']}[/dim]"
    table.add_row("Requester", requester)
    table.add_row("Submitted", _fmt_ts(detail.get("created_at")))
    table.add_row("Submitted via", str(detail.get("source") or "—"))
    if detail.get("session_title") or detail.get("session_id"):
        session_cell = str(detail.get("session_title") or "untitled")
        if detail.get("session_id"):
            session_cell += f"\n[dim]{detail['session_id']}[/dim]"
        table.add_row("Session", session_cell)
    messages = str(detail.get("snapshot_message_count", 0))
    if detail.get("snapshot_truncated"):
        messages += "  [yellow](truncated — oldest messages dropped)[/yellow]"
    table.add_row("Messages in snapshot", messages)
    if detail.get("status_changed_at"):
        table.add_row("Status changed", _fmt_ts(detail.get("status_changed_at")))
    if detail.get("resolution_note"):
        table.add_row("Resolution note", str(detail["resolution_note"]))

    console.console.print(table)

    console.console.print()
    console.console.print(
        Panel(
            detail.get("comment") or "[dim]No comment provided.[/dim]",
            title="What was reported",
            border_style="magenta",
        )
    )

    context = detail.get("context") or {}
    if context:
        console.console.print()
        _print_context(context)

    console.console.print()
    console.console.print(
        f"[dim]Next: 'cinna improve download {_short_id(detail.get('id', ''))}' "
        f"for the transcript, the prompt texts, and any captured memory.[/dim]"
    )


# ── download ────────────────────────────────────────────────────────────────


def run_improve_download(request_ref: str, out_dir: str | None = None) -> None:
    """Download + extract the archive — ``cinna improve download <id>``.

    Lands in ``improvements/<short-id>/`` under the account root unless
    ``--out`` names a directory. Extraction reuses the workspace clone's safe
    extractor (no absolute paths, no ``..``, no symlinks, size-capped).
    """
    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    from cinna.sync import extract_workspace_tarball

    with AccountClient(account_cfg) as client:
        request_id = _resolve_request_id(client, request_ref)
        with console.spinner("Downloading archive..."):
            archive = client.download_improvement_archive(request_id)

    short = _short_id(request_id)
    if out_dir:
        destination = Path(out_dir).expanduser()
        if not destination.is_absolute():
            destination = Path.cwd() / destination
    else:
        destination = account_root / DOWNLOAD_DIR / short

    existed = destination.is_dir() and any(destination.iterdir())
    extracted = extract_workspace_tarball(archive, destination)

    try:
        shown = destination.relative_to(Path.cwd())
    except ValueError:
        shown = destination

    verb = "Refreshed" if existed else "Extracted"
    console.status(f"{verb} {len(extracted)} files into {shown}/")
    for name in sorted(extracted):
        console.console.print(f"  [dim]{name}[/dim]")
    console.console.print()
    console.console.print(
        "Read [bold]README.md[/bold] first, then [bold]session/messages.md[/bold]."
    )
    console.console.print(
        "[yellow]![/yellow] This is another person's conversation — don't copy it "
        "into an agent workspace, don't commit it, and delete it when you're done."
    )


# ── status ──────────────────────────────────────────────────────────────────


def run_improve_status(request_ref: str, status: str, note: str | None = None) -> None:
    """Set status / resolution note — ``cinna improve status <id> <status>``.

    The note is shown to the person who submitted the request, so it is written
    for them. Recipient-only server-side: a requester attempting this gets 403.
    """
    account_cfg = load_account_config(find_account_root())
    new_status = _normalize_status(status)

    with AccountClient(account_cfg) as client:
        request_id = _resolve_request_id(client, request_ref)
        with console.spinner("Updating improvement request..."):
            detail = client.update_improvement_request(
                request_id, status=new_status, resolution_note=note
            )

    console.status(
        f"Request {_short_id(request_id)} "
        f"({detail.get('target_agent_name') or 'agent'}) is now "
        f"'{detail.get('status', new_status)}'."
    )
    if note:
        console.console.print(f"  [dim]Note shown to the requester:[/dim] {note}")
    if new_status == "in_progress":
        console.console.print(
            "[dim]Close it with 'cinna improve status "
            f"{_short_id(request_id)} completed --note \"…\"' when the fix is in.[/dim]"
        )
