"""`cinna chat` — talk to an agent through real platform sessions.

A local coding agent builds an agent and wants to *test* it the way production
does — through the actual conversation pipeline (permission checks, agent-env
calls, the model/SDK the platform picks), not a local mock. This command drives
a genuine platform session for that, and attaches local files to the message.

Transport: everything rides the account workspace's JSON api-proxy
(`AccountClient`). The platform's message-send route is streaming, but we never
read the stream — we send (the route returns a JSON ack and runs the turn
asynchronously) and then **poll** `get_messages` + `get_streaming_status`. That
keeps the CLI resilient to streaming/transport quirks and works entirely over
buffered JSON.

Output: by default one NDJSON event per line on stdout (agent-friendly — the
calling coding agent parses it trivially). `--pretty` switches to a human view.
Files the agent attaches to its replies are downloaded into a local directory so
the caller can inspect them.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import click

from cinna import console
from cinna.account import (
    _resolve_account_agent,
    find_account_root,
    load_account_config,
)
from cinna.client import AccountClient
from cinna.config import find_workspace_root, load_config
from cinna.errors import PlatformError

logger = logging.getLogger("cinna.chat")

# Poll cadence + bounds (seconds).
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_TIMEOUT = 600
# How long we wait for a turn to *begin* (env wake / queue) before giving up.
START_GRACE_SECONDS = 120
# Page size when draining new messages — comfortably above a single turn's count.
_MESSAGE_PAGE = 500

DEFAULT_DOWNLOAD_DIR = "cinna-chat-files"


class _Emitter:
    """Render chat events: NDJSON to stdout by default, Rich when ``pretty``."""

    def __init__(self, pretty: bool):
        self.pretty = pretty

    def emit(self, event: dict) -> None:
        if not self.pretty:
            sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            return
        self._emit_pretty(event)

    def _emit_pretty(self, event: dict) -> None:
        kind = event.get("event")
        if kind == "session":
            console.console.print(
                f"[dim]session[/dim] [bold]{event['session_id']}[/bold] "
                f"([cyan]{event.get('mode')}[/cyan])"
            )
        elif kind == "message":
            role = event.get("role", "?")
            color = {"user": "blue", "agent": "green", "system": "yellow"}.get(
                role, "white"
            )
            console.console.print(f"\n[bold {color}]{role}[/bold {color}]:")
            for ev in event.get("events", []):
                etype = ev.get("type")
                if etype == "assistant":
                    continue  # shown as the final content below
                if etype == "thinking":
                    console.console.print(
                        f"  [magenta]🧠 thinking:[/magenta] {ev.get('content', '')}"
                    )
                elif etype in ("tool", "tool_use"):
                    payload = ev.get("tool_input")
                    console.console.print(
                        f"  [cyan]🔧 {ev.get('tool_name', 'tool')}[/cyan] "
                        f"[dim]{json.dumps(payload, ensure_ascii=False) if payload is not None else ''}[/dim]"
                    )
                elif ev.get("content"):
                    console.console.print(f"  [dim]{etype}: {ev['content']}[/dim]")
            if event.get("content"):
                console.console.print(event["content"])
            for att in event.get("attachments", []):
                loc = (
                    att.get("downloaded_to")
                    or att.get("download_error")
                    or att.get("file_id")
                )
                console.console.print(f"  [dim]📎 {att.get('filename')} → {loc}[/dim]")
        elif kind == "status":
            console.console.print(f"[dim]· {event.get('state')}…[/dim]")
        elif kind == "delta":
            for ev in event.get("events", []):
                etype = ev.get("type")
                if etype == "assistant":
                    if ev.get("content"):
                        console.console.print(ev["content"], end="")
                elif etype == "thinking":
                    console.console.print(
                        f"  [magenta]🧠 thinking:[/magenta] {ev.get('content', '')}"
                    )
                elif etype in ("tool", "tool_use"):
                    payload = ev.get("tool_input")
                    console.console.print(
                        f"  [cyan]🔧 {ev.get('tool_name', 'tool')}[/cyan] "
                        f"[dim]{json.dumps(payload, ensure_ascii=False) if payload is not None else ''}[/dim]"
                    )
                elif ev.get("content"):
                    console.console.print(f"  [dim]{etype}: {ev['content']}[/dim]")
        elif kind == "done":
            console.console.print(
                f"[dim]done — result: {event.get('result_state')}[/dim]"
            )
        elif kind == "error":
            console.error(event.get("message", "error"))
        else:
            console.console.print(f"[dim]{json.dumps(event, ensure_ascii=False)}[/dim]")


def run_chat(
    agent_ref: str | None,
    resume: str | None,
    message_tokens: tuple[str, ...],
    files: tuple[str, ...],
    mode: str,
    title: str | None,
    download_dir: str | None,
    no_download: bool,
    interval: float,
    timeout: int,
    pretty: bool,
    include_events: bool = True,
) -> None:
    """Drive one chat turn: send a message to a session and stream the reply."""
    emit = _Emitter(pretty)

    # 1. Account workspace (the api-proxy auth context) — required.
    account_root = find_account_root()  # raises AccountConfigNotFoundError
    account_cfg = load_account_config(account_root)

    # 2. Validate any attachments up front (before opening a session).
    file_paths: list[Path] = []
    for f in files:
        p = Path(f).expanduser()
        if not p.is_file():
            raise click.ClickException(f"--file not found: {f}")
        file_paths.append(p)

    # 3. Gather the message text (positional, else stdin, else interactive prompt).
    message = " ".join(message_tokens).strip()
    if not message:
        if not sys.stdin.isatty():
            message = sys.stdin.read().strip()
        else:
            message = console.prompt("Message", default="", show_default=False).strip()
    if not message and not file_paths:
        raise click.ClickException(
            "No message provided. Pass it as an argument, pipe it on stdin, or type it when prompted."
        )

    with AccountClient(account_cfg) as client:
        # 4. Resolve the session — resume an existing one or open a new one.
        if resume:
            session_id = resume
            try:
                session = client.get_session(session_id)
            except PlatformError as e:
                raise click.ClickException(
                    f"Could not resume session {session_id}: {e}"
                )
        else:
            agent_id = _resolve_agent_id(client, agent_ref)
            session = client.create_session(agent_id, mode=mode, title=title)
            session_id = session["id"]

        emit.emit(
            {
                "event": "session",
                "session_id": session_id,
                "agent_id": session.get("agent_id"),
                "mode": session.get("mode"),
                "title": session.get("title"),
                "resumed": bool(resume),
            }
        )

        # 5. Upload attachments → file_ids.
        file_ids: list[str] = []
        for p in file_paths:
            uploaded = client.upload_file(p)
            fid = uploaded["id"]
            file_ids.append(fid)
            emit.emit(
                {
                    "event": "upload",
                    "file_id": fid,
                    "filename": uploaded.get("filename", p.name),
                    "size": uploaded.get("file_size"),
                }
            )

        # 6. Baseline cursor — only emit messages produced from here on.
        consumed = _message_count(client, session_id)

        # 7. Send the message (returns a JSON ack; the turn runs asynchronously).
        ack = client.send_message(session_id, message, file_ids=file_ids or None)
        expect_turn = bool(
            ack.get("streaming") or ack.get("pending") or ack.get("queued")
        )

        # 8. Poll until the turn finishes.
        dl_dir = None if no_download else _download_dir(download_dir, session_id)
        try:
            _poll_turn(
                client,
                session_id,
                emit,
                consumed,
                expect_turn,
                dl_dir,
                interval,
                timeout,
                include_events,
            )
        except KeyboardInterrupt:
            try:
                client.interrupt_message(session_id)
            except Exception:
                pass
            emit.emit({"event": "interrupted", "session_id": session_id})
            sys.exit(130)


def _resolve_agent_id(client: AccountClient, agent_ref: str | None) -> str:
    """Resolve an agent id from ``--agent`` or the surrounding workspace."""
    if agent_ref:
        listing = client.list_account_agents().get("data", [])
        return _resolve_account_agent(listing, agent_ref)["id"]
    # No --agent: infer from the per-agent workspace we're standing in.
    try:
        root = find_workspace_root()
        return load_config(root).agent_id
    except Exception:
        raise click.ClickException(
            "No agent specified. Pass --agent <name|id>, or run from inside a "
            "synced agent workspace."
        )


def _message_count(client: AccountClient, session_id: str) -> int:
    """Total messages currently in the session (the starting poll offset)."""
    total = 0
    offset = 0
    while True:
        page = client.get_messages(session_id, limit=_MESSAGE_PAGE, offset=offset)
        n = len(page.get("data", []))
        total += n
        if n < _MESSAGE_PAGE:
            return total
        offset += n


def _poll_turn(
    client: AccountClient,
    session_id: str,
    emit: _Emitter,
    consumed: int,
    expect_turn: bool,
    dl_dir: Path | None,
    interval: float,
    timeout: int,
    include_events: bool = True,
) -> None:
    """Emit each finalized message as it appears; return when the turn settles."""
    started = time.monotonic()
    deadline = started + timeout
    start_deadline = started + START_GRACE_SECONDS
    turn_started = not expect_turn
    flagged_in_progress: set[str] = set()
    # How much of an in-progress message's trace we've already surfaced —
    # the row is periodically re-flushed server-side with its *full*
    # accumulated ``streaming_events`` so far, so this is what lets us emit
    # only what's new since the last poll instead of going silent until the
    # whole (possibly many-minutes-long) message finalizes.
    shown_event_count: dict[str, int] = {}

    while True:
        # Drain newly-finalized messages; stop at the first in-progress one
        # (its content is still growing — re-read it next poll).
        in_progress = False
        while True:
            page = client.get_messages(session_id, limit=_MESSAGE_PAGE, offset=consumed)
            batch = page.get("data", [])
            advanced = False
            for m in batch:
                meta = m.get("message_metadata") or {}
                mid = m.get("id")
                if meta.get("streaming_in_progress"):
                    in_progress = True
                    if mid not in flagged_in_progress:
                        flagged_in_progress.add(mid)
                        emit.emit(
                            {"event": "status", "state": "working", "message_id": mid}
                        )
                    if include_events:
                        _emit_delta(emit, m, shown_event_count)
                    break
                _emit_message(client, emit, m, dl_dir, include_events)
                shown_event_count.pop(mid, None)
                consumed += 1
                advanced = True
                if m.get("role") and m["role"] != "user":
                    turn_started = True
            if in_progress or len(batch) < _MESSAGE_PAGE or not advanced:
                break

        streaming = bool(client.get_streaming_status(session_id).get("is_streaming"))
        if streaming:
            turn_started = True

        # Settled: the turn ran and nothing is in flight any more.
        if turn_started and not streaming and not in_progress:
            break
        if not turn_started and time.monotonic() > start_deadline:
            emit.emit(
                {
                    "event": "warning",
                    "message": "agent turn did not start within the grace period",
                    "session_id": session_id,
                }
            )
            break
        if time.monotonic() > deadline:
            emit.emit(
                {"event": "timeout", "session_id": session_id, "seconds": timeout}
            )
            break
        time.sleep(interval)

    _emit_done(client, emit, session_id)


def _emit_delta(
    emit: _Emitter,
    m: dict,
    shown_event_count: dict[str, int],
) -> None:
    """Emit trace events newly appended to a still-in-progress message.

    ``streaming_events`` (thinking blocks, tool calls, tool results, and
    individual assistant-text chunks) accumulates on the row as the turn
    progresses and is what the server periodically flushes; this diffs
    against what's already been shown for this message id so a long-running
    turn surfaces gradual output instead of nothing until it finalizes.
    """
    mid = m.get("id")
    events = _extract_events(m)
    prev_count = shown_event_count.get(mid, 0)
    new_events = events[prev_count:]
    if not new_events:
        return
    emit.emit({"event": "delta", "message_id": mid, "role": m.get("role"), "events": new_events})
    shown_event_count[mid] = len(events)


def _emit_message(
    client: AccountClient,
    emit: _Emitter,
    m: dict,
    dl_dir: Path | None,
    include_events: bool = True,
) -> None:
    """Emit one finalized message, downloading any attachments it carries.

    ``content`` is the final assistant text; the reasoning/tool trace (thinking
    blocks, tool calls with their input payloads, tool results) lives in
    ``message_metadata.streaming_events`` and is surfaced under ``events`` so the
    calling agent sees *what the agent did*, not just its closing line.
    """
    event: dict = {
        "event": "message",
        "id": m.get("id"),
        "role": m.get("role"),
        "seq": m.get("sequence_number"),
        "timestamp": m.get("timestamp"),
        "content": m.get("content", ""),
    }
    if m.get("status"):
        event["status"] = m["status"]
    if m.get("status_message"):
        event["status_message"] = m["status_message"]

    if include_events:
        events = _extract_events(m)
        if events:
            event["events"] = events

    attachments = _extract_attachments(m)
    if attachments:
        if dl_dir is not None:
            for att in attachments:
                _download_attachment(client, att, dl_dir)
        event["attachments"] = attachments
    emit.emit(event)


# Streaming-event types surfaced separately (attachments) or pure bookkeeping —
# excluded from the `events` trace to avoid duplication / noise.
_TRACE_SKIP_TYPES = {"attachment", "attachment_error", "done"}


def _extract_events(m: dict) -> list[dict]:
    """Normalize a message's ``streaming_events`` into an agent-readable trace.

    Keeps the ordered thinking / assistant-text / tool / tool-result events and
    drops the bookkeeping ones. Tool events carry their ``tool_name`` and the
    full ``tool_input`` payload so the caller can see exactly what was invoked.
    """
    out: list[dict] = []
    meta = m.get("message_metadata") or {}
    for ev in meta.get("streaming_events", []) or []:
        etype = ev.get("type")
        if not etype or etype in _TRACE_SKIP_TYPES:
            continue
        entry: dict = {"seq": ev.get("event_seq"), "type": etype}
        content = ev.get("content")
        if content not in (None, ""):
            entry["content"] = content
        md = ev.get("metadata") or {}
        tool_name = ev.get("tool_name") or md.get("tool_name")
        if tool_name:
            entry["tool_name"] = tool_name
        if md.get("tool_id"):
            entry["tool_id"] = md["tool_id"]
        if md.get("tool_input") is not None:
            entry["tool_input"] = md["tool_input"]
        if md.get("tool_use_id"):
            entry["tool_use_id"] = md["tool_use_id"]
        out.append(entry)
    return out


def _extract_attachments(m: dict) -> list[dict]:
    """Collect agent-produced attachments from a message (dedup by file_id).

    Prefers inline ``attachment`` streaming events; falls back to the message's
    ``files[]`` entries flagged ``source == "agent_attachment"`` (replay path).
    """
    found: dict[str, dict] = {}
    meta = m.get("message_metadata") or {}
    for ev in meta.get("streaming_events", []) or []:
        if ev.get("type") != "attachment":
            continue
        md = ev.get("metadata") or {}
        fid = md.get("file_id")
        if fid:
            found[fid] = {
                "file_id": fid,
                "filename": md.get("filename"),
                "mime_type": md.get("mime_type"),
                "size": md.get("size"),
            }
    for f in m.get("files", []) or []:
        if f.get("source") != "agent_attachment":
            continue
        fid = f.get("id")
        if fid and fid not in found:
            found[fid] = {
                "file_id": fid,
                "filename": f.get("filename"),
                "mime_type": f.get("mime_type"),
                "size": f.get("file_size"),
            }
    return list(found.values())


def _download_attachment(client: AccountClient, att: dict, dl_dir: Path) -> None:
    """Download one attachment into ``dl_dir``; annotate the dict in place."""
    fid = att["file_id"]
    safe_name = Path(att.get("filename") or fid).name or fid
    dest = dl_dir / safe_name
    try:
        response = client.download_file(fid)
        dl_dir.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(response.content)
        att["downloaded_to"] = str(dest)
    except PlatformError as e:
        att["download_error"] = str(e)
        logger.warning("attachment download failed (%s): %s", fid, e)


def _emit_done(client: AccountClient, emit: _Emitter, session_id: str) -> None:
    """Emit the terminal ``done`` event with the session's settled state."""
    event: dict = {"event": "done", "session_id": session_id}
    try:
        session = client.get_session(session_id)
        event["interaction_status"] = session.get("interaction_status")
        event["result_state"] = session.get("result_state")
        if session.get("result_summary"):
            event["result_summary"] = session["result_summary"]
    except Exception:
        pass
    emit.emit(event)


def _download_dir(download_dir: str | None, session_id: str) -> Path:
    """Resolve the directory agent attachments are saved into."""
    base = (
        Path(download_dir).expanduser() if download_dir else Path(DEFAULT_DOWNLOAD_DIR)
    )
    return base / session_id
