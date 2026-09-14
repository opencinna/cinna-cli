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
import httpx

from cinna import console
from cinna.account import (
    _resolve_account_agent,
    find_account_root,
    load_account_config,
)
from cinna.client import AccountClient
from cinna.config import find_workspace_root, load_config
from cinna.errors import EXIT_NETWORK, CinnaExit, PlatformError

logger = logging.getLogger("cinna.chat")

# Poll cadence + bounds (seconds).
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_TIMEOUT = 600
# How long we wait for a turn to *begin* (env wake / queue) before giving up.
START_GRACE_SECONDS = 120
# Page size when draining new messages — comfortably above a single turn's count.
_MESSAGE_PAGE = 500

DEFAULT_DOWNLOAD_DIR = "cinna-chat-files"

# Backoff between retries of a failed poll request. A single buffered proxy
# call timing out (or a 5xx while the env restarts) says nothing about the
# turn, which runs server-side regardless — so a poll failure is retried
# inside the --timeout budget instead of aborting the command.
_POLL_RETRY_DELAYS = (2.0, 4.0, 8.0, 15.0, 30.0)


class _Emitter:
    """Render chat events: NDJSON to stdout by default, Rich when ``pretty``.

    With a ``sink`` nothing is printed: each event is handed to it instead —
    how ``cinna agent scenarios run`` drives the same poll loop and builds its
    own report from what the turn produced.
    """

    def __init__(self, pretty: bool, sink=None):
        self.pretty = pretty
        self.sink = sink

    def emit(self, event: dict) -> None:
        if self.sink is not None:
            self.sink(event)
            return
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
                f"[dim]done — outcome: {event.get('outcome')}, "
                f"result: {event.get('result_state')}[/dim]"
            )
            if event.get("recover"):
                console.console.print(f"[dim]  wait for it: {event['recover']}[/dim]")
        elif kind in ("warning", "timeout"):
            message = event.get("message") or (
                f"no reply within {event.get('seconds')}s — the turn may still be running"
            )
            console.warn(message)
        elif kind == "detached":
            console.console.print("[dim]stopped watching — the turn keeps running[/dim]")
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

        emit.emit(_session_event(session_id, session, resumed=bool(resume)))

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
            outcome = _poll_turn(
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
        except (httpx.TransportError, PlatformError) as exc:
            if not _is_transient(exc):
                raise
            _abort_with_recovery(emit, session_id, exc)
        _emit_done(client, emit, session_id, outcome)


def run_chat_attach(
    session_id: str,
    download_dir: str | None,
    no_download: bool,
    interval: float,
    timeout: int,
    pretty: bool,
    include_events: bool = True,
) -> None:
    """Re-attach to a session's current turn — `cinna chat --attach`.

    Sends nothing. Replays the session from its latest user message, then waits
    for that turn to settle exactly as a fresh ``cinna chat`` would — the
    recovery for a chat whose command died while the agent kept working.
    Ctrl-C only stops watching: the turn is not interrupted.
    """
    emit = _Emitter(pretty)
    account_cfg = load_account_config(find_account_root())

    with AccountClient(account_cfg) as client:
        session = _open_session(client, session_id)
        emit.emit(_session_event(session_id, session, attached=True))

        messages = _all_messages(client, session_id)
        user_positions = [i for i, m in enumerate(messages) if m.get("role") == "user"]
        cursor = user_positions[-1] if user_positions else 0

        dl_dir = None if no_download else _download_dir(download_dir, session_id)
        try:
            outcome = _poll_turn(
                client,
                session_id,
                emit,
                cursor,
                True,
                dl_dir,
                interval,
                timeout,
                include_events,
            )
        except KeyboardInterrupt:
            emit.emit({"event": "detached", "session_id": session_id})
            sys.exit(130)
        except (httpx.TransportError, PlatformError) as exc:
            if not _is_transient(exc):
                raise
            _abort_with_recovery(emit, session_id, exc)
        _emit_done(client, emit, session_id, outcome)


def run_chat_show(
    session_id: str,
    download_dir: str | None,
    no_download: bool,
    pretty: bool,
    include_events: bool = True,
) -> None:
    """Print an existing session's transcript once — `cinna chat --show`.

    Sends nothing and waits for nothing. A message still being written is
    reported as ``working`` with its trace so far, and the closing ``done``
    says ``in_progress`` with the ``--attach`` command that waits for it.
    """
    emit = _Emitter(pretty)
    account_cfg = load_account_config(find_account_root())

    with AccountClient(account_cfg) as client:
        session = _open_session(client, session_id)
        emit.emit(_session_event(session_id, session))

        dl_dir = None if no_download else _download_dir(download_dir, session_id)
        in_progress = False
        for m in _all_messages(client, session_id):
            if (m.get("message_metadata") or {}).get("streaming_in_progress"):
                in_progress = True
                emit.emit({"event": "status", "state": "working", "message_id": m.get("id")})
                if include_events:
                    _emit_delta(emit, m, {})
                continue
            _emit_message(client, emit, m, dl_dir, include_events)

        streaming = in_progress
        if not streaming:
            try:
                streaming = bool(client.get_streaming_status(session_id).get("is_streaming"))
            except (httpx.TransportError, PlatformError) as exc:
                logger.warning("streaming status unavailable: %s", exc)
        _emit_done(client, emit, session_id, "in_progress" if streaming else "idle")


def _open_session(client: AccountClient, session_id: str) -> dict:
    """Fetch a session a caller named, turning a refusal into one sentence."""
    try:
        return client.get_session(session_id)
    except PlatformError as e:
        raise click.ClickException(f"Could not open session {session_id}: {e}")


def _session_event(
    session_id: str, session: dict, *, resumed: bool = False, attached: bool = False
) -> dict:
    """The opening ``session`` event every chat mode emits first."""
    event = {
        "event": "session",
        "session_id": session_id,
        "agent_id": session.get("agent_id"),
        "mode": session.get("mode"),
        "title": session.get("title"),
        "resumed": resumed,
    }
    if attached:
        event["attached"] = True
    return event


def _is_transient(exc: BaseException) -> bool:
    """A failure that says nothing about the turn: transport, 429 or 5xx."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, PlatformError):
        return exc.status_code == 429 or exc.status_code >= 500
    return False


def _describe_error(exc: BaseException) -> str:
    if isinstance(exc, CinnaExit):
        return exc.detail
    return str(exc) or type(exc).__name__


def _with_poll_retry(call, emit: "_Emitter", session_id: str, deadline: float):
    """Run one poll request, retrying transient failures until ``deadline``.

    Each retry is announced as a ``warning`` event, so a caller watching the
    stream sees the platform was slow rather than a silent pause.
    """
    attempt = 0
    while True:
        try:
            return call()
        except (httpx.TransportError, PlatformError) as exc:
            remaining = deadline - time.monotonic()
            if not _is_transient(exc) or remaining <= 0:
                raise
            delay = min(
                _POLL_RETRY_DELAYS[min(attempt, len(_POLL_RETRY_DELAYS) - 1)], remaining
            )
            attempt += 1
            logger.warning("chat poll failed (attempt %d): %s", attempt, exc)
            emit.emit(
                {
                    "event": "warning",
                    "session_id": session_id,
                    "message": f"poll request failed ({_describe_error(exc)}); "
                    f"retrying in {delay:.0f}s",
                    "attempt": attempt,
                }
            )
            time.sleep(delay)


def _abort_with_recovery(emit: "_Emitter", session_id: str, exc: BaseException):
    """Give up polling, naming the session and the command that gets it back.

    The agent's turn runs server-side whether or not anyone is polling, so a
    lost connection is never the end of the turn — only of this command.
    """
    recover = f"cinna chat --attach {session_id}"
    detail = _describe_error(exc)
    if not emit.pretty:
        emit.emit(
            {
                "event": "error",
                "session_id": session_id,
                "message": f"lost contact with the session while waiting for the reply: {detail}",
                "recover": recover,
            }
        )
    raise CinnaExit(
        EXIT_NETWORK,
        "chat_poll_failed",
        f"Lost contact with session {session_id} while waiting for the reply "
        f"({detail}).\nThe agent's turn keeps running on the platform. "
        f"Re-attach with: {recover}",
        extra={"session_id": session_id, "recover": recover},
    )


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


def _all_messages(client: AccountClient, session_id: str) -> list[dict]:
    """Every message in the session, in ``sequence_number`` order."""
    out: list[dict] = []
    while True:
        page = client.get_messages(session_id, limit=_MESSAGE_PAGE, offset=len(out))
        batch = page.get("data", [])
        out.extend(batch)
        if len(batch) < _MESSAGE_PAGE:
            return out


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
) -> str:
    """Emit each finalized message as it appears; return how the wait ended.

    ``completed`` — the turn ran and settled. ``timeout`` — ``timeout`` elapsed
    first; the turn may still be running (``--attach`` waits for it).
    ``not_started`` — no turn began within the start-grace window. The caller
    emits the closing ``done`` event carrying this outcome.

    Every poll request is retried on a transient failure within the same
    ``timeout`` budget: the turn runs server-side regardless of whether one
    buffered proxy call timed out.
    """
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
    last_stream_info = None

    def poll(fn, *args, **kwargs):
        return _with_poll_retry(lambda: fn(*args, **kwargs), emit, session_id, deadline)

    while True:
        # Drain newly-finalized messages; stop at the first in-progress one
        # (its content is still growing — re-read it next poll).
        in_progress = False
        while True:
            page = poll(client.get_messages, session_id, limit=_MESSAGE_PAGE, offset=consumed)
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

        stream = poll(client.get_streaming_status, session_id)
        streaming = bool(stream.get("is_streaming"))
        if streaming:
            turn_started = True
        # Whatever the platform says about the running turn (phase, current
        # tool, …) is passed through when it changes — it is the only signal
        # that tells "waiting for the environment" from "the model is working".
        stream_info = stream.get("stream_info")
        if stream_info and stream_info != last_stream_info:
            emit.emit(
                {
                    "event": "status",
                    "state": "streaming",
                    "session_id": session_id,
                    "stream_info": stream_info,
                }
            )
        last_stream_info = stream_info

        # Settled: the turn ran and nothing is in flight any more.
        if turn_started and not streaming and not in_progress:
            return "completed"
        if not turn_started and time.monotonic() > start_deadline:
            emit.emit(
                {
                    "event": "warning",
                    "message": "agent turn did not start within the grace period",
                    "session_id": session_id,
                    "recover": f"cinna chat --attach {session_id}",
                }
            )
            return "not_started"
        if time.monotonic() > deadline:
            emit.emit(
                {
                    "event": "timeout",
                    "session_id": session_id,
                    "seconds": timeout,
                    "recover": f"cinna chat --attach {session_id}",
                }
            )
            return "timeout"
        time.sleep(interval)


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


def _emit_done(
    client: AccountClient,
    emit: _Emitter,
    session_id: str,
    outcome: str | None = None,
) -> None:
    """Emit the terminal ``done`` event with the session's settled state.

    ``outcome`` is the CLI's own account of how the wait ended (``completed`` /
    ``timeout`` / ``not_started``, or ``in_progress`` / ``idle`` for
    ``--show``) — always present, unlike the session's ``result_state``, which
    the platform can leave empty. A turn that may still be running carries the
    ``--attach`` command that waits for it.
    """
    event: dict = {"event": "done", "session_id": session_id}
    if outcome:
        event["outcome"] = outcome
        if outcome in ("timeout", "not_started", "in_progress"):
            event["recover"] = f"cinna chat --attach {session_id}"
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
