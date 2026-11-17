"""Consumer side of agent REST APIs: an agent's connections, and capability requests.

`cinna agent-api connections <agent>` lists every ``agent_api`` credential
linked to one of your agents (owned, or shared with you by its owner) with the
producer, the URLs the agent's **environment** calls, the key status and the
value a script passes to ``credentials.agent_api_session(...)``.
`connections spec` reads the producer's OpenAPI spec through a connection, and
`cinna agent-api request file|list` files and reads **capability requests**
through it.

No key value crosses any of these routes: core files a capability request with
the linked connection on the server. The CLI never calls the listed URLs
(locally they are often not even reachable from the host, e.g.
``http://backend:8000``) — they are what the environment uses.

Everything a caller wrote (titles, needs, the producer owner's resolution
notes) prints verbatim, never as Rich markup.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from cinna import console
from cinna.account import (
    _esc,
    _key_status_cell,
    _resolve_account_agent,
    find_account_root,
    load_account_config,
)
from cinna.client import AccountClient, drop_token
from cinna.errors import EXIT_ERROR, CinnaExit

# What each ``unavailable_reason`` means and what to do — plain text.
_UNAVAILABLE_REMEDIES = {
    "key_revoked": "The key behind this connection was revoked or its producer removed; "
    "a key never comes back. Ask the producer's owner for a new one.",
    "key_expired": "The key behind this connection expired. Ask the producer's owner "
    "for a new one.",
    "key_paused": "The producer's owner turned its API or external access off. It "
    "recovers by itself once they turn it back on.",
    "not_delivered": "The platform cannot deliver this connection to the agent's "
    "environment (no working key behind it).",
    "placeholder": "This is a placeholder: the user must provide the real connection "
    "in the web UI.",
}


def _short(value) -> str:
    text = str(value or "")
    return text[:8] if len(text) > 8 else text


def _resolve_agent(client: AccountClient, agent_ref: str) -> dict:
    return _resolve_account_agent(client.list_account_agents().get("data", []), agent_ref)


def _connections(client: AccountClient, agent_id: str) -> list[dict]:
    payload = client.list_agent_api_connections(agent_id)
    return [r for r in (payload.get("data") or []) if isinstance(r, dict)]


def resolve_connection(rows: list[dict], ref: str, agent_name: str) -> dict:
    """``ref`` = credential id (or a unique prefix of one), slot, or exact name.

    Id first, then slot, ``sdk_lookup``, exact name. A slot or name several
    connections share refuses with ``ambiguous_connection`` naming their ids.
    """
    ref = (ref or "").strip()
    by_id = [r for r in rows if str(r.get("credential_id")) == ref]
    if by_id:
        return by_id[0]
    for hits in (
        [r for r in rows if r.get("slot") and str(r.get("slot")) == ref],
        [r for r in rows if r.get("sdk_lookup") and str(r.get("sdk_lookup")) == ref],
        [r for r in rows if r.get("name") == ref],
    ):
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            ids = ", ".join(str(r.get("credential_id")) for r in hits)
            raise CinnaExit(
                EXIT_ERROR,
                "ambiguous_connection",
                f"'{ref}' matches {len(hits)} connections of {agent_name}: {ids}. "
                "Pass the credential id instead.",
            )
    if len(ref) >= 6:
        by_prefix = [r for r in rows if str(r.get("credential_id", "")).startswith(ref)]
        if len(by_prefix) == 1:
            return by_prefix[0]
    known = ", ".join(
        f"{r.get('name')} ({r.get('credential_id')})" for r in rows
    ) or "none"
    raise CinnaExit(
        EXIT_ERROR,
        "connection_not_found",
        f"No agent API connection '{ref}' on {agent_name}. Its connections: {known}. "
        f"See: cinna agent-api connections \"{agent_name}\"",
    )


def _status_cell(row: dict) -> str:
    cell = _key_status_cell(row.get("key_status"))
    if not row.get("usable"):
        reason = row.get("unavailable_reason") or "unusable"
        cell += f" [red](not usable: {_esc(reason)})[/red]"
    return cell


def _from_cell(row: dict) -> str:
    if row.get("is_shared"):
        return f"shared by {_esc(row.get('owner_email') or '?')}"
    return "yours"


# ── connections list ─────────────────────────────────────────────────────────


def run_connections_list(agent_ref: str, as_json: bool = False) -> None:
    """`cinna agent-api connections <agent>`."""
    from rich.table import Table

    account_cfg = load_account_config(find_account_root())
    with AccountClient(account_cfg) as client:
        agent = _resolve_agent(client, agent_ref)
        with console.spinner("Fetching agent API connections..."):
            payload = drop_token(client.list_agent_api_connections(agent["id"]))

    if as_json:
        click.echo(json.dumps(payload, indent=2, default=str))
        return

    rows = [r for r in payload.get("data") or [] if isinstance(r, dict)]
    name = agent.get("name", agent_ref)
    if not rows:
        console.status(
            f"{_esc(name)} has no agent API connections. Wire one: cinna connect "
            f"agent-api --producer <producer> --consumer \"{_esc(name)}\", or attach "
            "an agent_api credential: cinna account credentials share-with-agent <id> "
            f"--agent \"{_esc(name)}\""
        )
        return

    table = Table(title=f"Agent API connections — {_esc(name)} ({len(rows)})", title_style="bold")
    table.add_column("Name", overflow="fold")
    table.add_column("Producer", overflow="fold")
    table.add_column("Status")
    # The SDK lookup is copied into code: it never wraps or folds. The id
    # column appears only when some lookup is not the id (a slot).
    show_id = any(str(r.get("sdk_lookup")) != str(r.get("credential_id")) for r in rows)
    lookup_width = max(len(str(r.get("sdk_lookup") or "?")) for r in rows)
    table.add_column("SDK lookup", no_wrap=True, overflow="ignore", min_width=lookup_width)
    table.add_column("Scopes", overflow="fold")
    table.add_column("From", overflow="fold")
    if show_id:
        id_width = max(len(str(r.get("credential_id") or "?")) for r in rows)
        table.add_column("Credential ID", style="dim", no_wrap=True, min_width=id_width)
    for r in rows:
        cells = [
            _esc(r.get("name") or "?"),
            _esc(r.get("producer_agent_name") or r.get("producer_agent_id") or "?"),
            _status_cell(r),
            _esc(r.get("sdk_lookup") or "?"),
            _esc(", ".join(str(s) for s in r.get("scopes") or []) or "—"),
            _from_cell(r),
        ]
        if show_id:
            cells.append(_esc(r.get("credential_id") or "?"))
        table.add_row(*cells)
    console.console.print(table)

    console.console.print(
        "[dim]URLs below are what the agent's environment calls (environment origin); "
        "the CLI never calls them and they may not resolve from this machine.[/dim]"
    )
    for r in rows:
        console.console.print()
        console.console.print(
            f"{_esc(r.get('name') or '?')} [dim]({_esc(r.get('credential_id'))})[/dim]"
        )
        for label in ("base_url", "spec_url", "improvement_requests_url"):
            value = r.get(label)
            console.console.print(
                f"  {label + ':':<26}{_esc(value) if value else '[dim]—[/dim]'}"
            )
        if not r.get("usable"):
            remedy = _UNAVAILABLE_REMEDIES.get(str(r.get("unavailable_reason") or ""))
            if remedy:
                console.console.print(f"  [yellow]![/yellow] [dim]{_esc(remedy)}[/dim]")

    first = rows[0]
    agent_q = f'"{name}"'
    console.console.print()
    console.console.print(
        "[dim]In the agent's scripts: credentials.agent_api_session(\"<SDK lookup>\"). "
        "Shared connections are the owner's: you can use them, not change them.[/dim]"
    )
    console.console.print(
        "[dim]Spec:     "
        + _esc(f"cinna agent-api connections spec {agent_q} {first.get('credential_id')}")
        + "[/dim]"
    )
    console.console.print(
        "[dim]Request:  "
        + _esc(
            f"cinna agent-api request file {agent_q} {first.get('credential_id')} "
            "--title … --need …  (after the user confirms the draft)"
        )
        + "[/dim]"
    )


# ── connections spec ─────────────────────────────────────────────────────────


def run_connection_spec(agent_ref: str, connection_ref: str, output: str | None) -> None:
    """`cinna agent-api connections spec <agent> <connection> [-o FILE]`."""
    account_cfg = load_account_config(find_account_root())
    with AccountClient(account_cfg) as client:
        agent = _resolve_agent(client, agent_ref)
        row = resolve_connection(_connections(client, agent["id"]), connection_ref, agent["name"])
        with console.spinner("Fetching the producer's spec (may wake its environment)..."):
            spec = client.get_agent_api_connection_spec(agent["id"], row["credential_id"])

    rendered = json.dumps(spec, indent=2)
    if output:
        Path(output).write_text(rendered + "\n")
        version = (spec.get("info") or {}).get("version") if isinstance(spec, dict) else None
        console.status(f"Spec written to {output}")
        if version:
            console.console.print(
                f"  API version: {_esc(version)} [dim](pass it as --spec-version when "
                "filing a capability request)[/dim]"
            )
    else:
        # Plain stdout so it pipes / parses cleanly.
        click.echo(rendered)


# ── capability requests ──────────────────────────────────────────────────────


def _read_json_source(source: str, flag: str):
    """``FILE`` or ``-`` (stdin) → parsed JSON."""
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text()
    except OSError as exc:
        raise click.UsageError(f"{flag}: cannot read {source}: {exc}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise click.UsageError(f"{flag}: not valid JSON: {exc}")


def build_request_body(
    *,
    title: str | None,
    need: str | None,
    proposed_endpoint_json: str | None,
    example_request: str | None,
    example_response: str | None,
    spec_version: str | None,
    body_json: str | None,
) -> dict:
    """The capability-request body, from flags or from ``--body-json`` alone.

    Field validation is core's (strict; it answers 422 with the expected
    shape) — this only assembles what was passed.
    """
    flags = {
        "--title": title,
        "--need": need,
        "--proposed-endpoint-json": proposed_endpoint_json,
        "--example-request": example_request,
        "--example-response": example_response,
        "--spec-version": spec_version,
    }
    if body_json is not None:
        given = [k for k, v in flags.items() if v is not None]
        if given:
            raise click.UsageError(
                f"--body-json is the whole body; drop {', '.join(given)} (or put them in the JSON)."
            )
        body = _read_json_source(body_json, "--body-json")
        if not isinstance(body, dict):
            raise click.UsageError("--body-json must hold a JSON object.")
        return body
    missing = [k for k in ("--title", "--need") if not flags[k]]
    if missing:
        raise click.UsageError(f"Missing {' and '.join(missing)} (or pass --body-json FILE).")
    body: dict = {"title": title, "need": need}
    if proposed_endpoint_json is not None:
        body["proposed_endpoint"] = _read_json_source(
            proposed_endpoint_json, "--proposed-endpoint-json"
        )
    if example_request is not None:
        body["example_request"] = example_request
    if example_response is not None:
        body["example_response"] = example_response
    if spec_version is not None:
        body["spec_version_seen"] = spec_version
    return body


def run_request_file(agent_ref: str, connection_ref: str, body: dict, as_json: bool = False) -> None:
    """`cinna agent-api request file <agent> <connection> …`."""
    account_cfg = load_account_config(find_account_root())
    with AccountClient(account_cfg) as client:
        agent = _resolve_agent(client, agent_ref)
        row = resolve_connection(_connections(client, agent["id"]), connection_ref, agent["name"])
        with console.spinner("Filing capability request..."):
            created = client.file_capability_request(agent["id"], row["credential_id"], body)

    if as_json:
        click.echo(json.dumps(created, indent=2, default=str))
        return
    producer = row.get("producer_agent_name") or row.get("producer_agent_id") or "?"
    console.status(f"Capability request filed with {_esc(producer)}")
    console.console.print(f"  ID:            {_esc(created.get('id', '?'))}")
    console.console.print(f"  Title:         {_esc(created.get('title', ''))}")
    console.console.print(f"  Status:        {_esc(created.get('status', '?'))}")
    if created.get("spec_version_seen"):
        console.console.print(f"  Spec version:  {_esc(created['spec_version_seen'])}")
    console.console.print(
        "[dim]The producer's owner sees it in `cinna improve list --kind api`. Check it "
        "later: "
        + _esc(f'cinna agent-api request list "{agent["name"]}" {row["credential_id"]}')
        + "[/dim]"
    )


def run_request_list(agent_ref: str, connection_ref: str, as_json: bool = False) -> None:
    """`cinna agent-api request list <agent> <connection>`."""
    from rich.table import Table
    from rich.text import Text

    account_cfg = load_account_config(find_account_root())
    with AccountClient(account_cfg) as client:
        agent = _resolve_agent(client, agent_ref)
        row = resolve_connection(_connections(client, agent["id"]), connection_ref, agent["name"])
        with console.spinner("Fetching capability requests..."):
            payload = client.list_capability_requests(agent["id"], row["credential_id"])

    if as_json:
        click.echo(json.dumps(payload, indent=2, default=str))
        return

    items = [r for r in payload.get("data") or [] if isinstance(r, dict)]
    producer = row.get("producer_agent_name") or row.get("producer_agent_id") or "?"
    if not items:
        console.status(f"You have filed no capability requests with {_esc(producer)}.")
        return

    table = Table(
        title=f"Your capability requests — {_esc(producer)} ({payload.get('count', len(items))})",
        title_style="bold",
        show_lines=True,
    )
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Title", overflow="fold")
    table.add_column("Status")
    table.add_column("Filed", style="dim")
    table.add_column("Spec", style="dim")
    table.add_column("Owner's note", overflow="fold")
    for item in items:
        # Text(): caller- and owner-authored text is never Rich markup.
        table.add_row(
            _short(item.get("id")),
            Text(str(item.get("title") or "")),
            Text(str(item.get("status") or "?")),
            str(item.get("created_at") or "")[:16].replace("T", " "),
            Text(str(item.get("spec_version_seen") or "—")),
            Text(str(item.get("resolution_note") or "—")),
        )
    console.console.print(table)
    if any(i.get("status") == "completed" for i in items):
        console.console.print(
            "[dim]A completed request means the producer changed its API: re-read the spec "
            "(cinna agent-api connections spec …) before relying on it.[/dim]"
        )
