"""`cinna agent scenarios` — re-run an agent's recorded test scenarios.

A builder records what the agent must do as ``docs/test_scenarios/*.md``: one
file per kind of question, each holding a ``## Say | Expect`` table (the
contract is CHAT_TESTING.md, "Record the conditions", and design-patterns
guide 13 §9). The set is re-run after every change to a prompt, a skill, the
model or the provider — by hand that is one ``cinna chat`` per row and one
trace to read for each.

``list`` reads the files. ``run`` sends every Say row in a fresh conversation
session, so no row sees another's context, through the poll loop ``cinna chat``
uses, and reports the reply, the tool calls and how each wait ended. It never
judges: Expect is prose, and the builder marks each row.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import click
import httpx

from cinna import console
from cinna.account import (
    _agent_environment,
    _display_path,
    _model_view,
    _require_yes_for_json,
    _resolve_account_agent,
    find_account_root,
    load_account_config,
    resolve_child_workspace,
)
from cinna.chat import (
    DEFAULT_POLL_INTERVAL,
    _describe_error,
    _Emitter,
    _is_transient,
    _message_count,
    _poll_turn,
)
from cinna.client import AccountClient
from cinna.errors import EXIT_ERROR, CinnaExit, PlatformError

SCENARIOS_SUBDIR = Path("docs") / "test_scenarios"

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
_SEPARATOR_CELL = re.compile(r"^:?-+:?$")
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TOOL_EVENT_TYPES = ("tool", "tool_use")
# How much of a tool call's input the compact list keeps.
_TOOL_INPUT_WIDTH = 160
# How much of a reply the report's table keeps (the details below it keep all).
_REPORT_REPLY_WIDTH = 300


@dataclass
class ScenarioCase:
    file: str
    row: int  # 1-based, counting the table's data rows
    line: int  # 1-based line in the file
    say: str
    expect: str


@dataclass
class ScenarioFile:
    path: Path
    cases: list[ScenarioCase]
    has_table: bool


# ── Parsing ─────────────────────────────────────────────────────────────────


def _key(text: str) -> str:
    """A heading or header cell reduced to what it says: ``Say \\| Expect`` → ``say|expect``."""
    return re.sub(r"[\s\\`*_]", "", text).lower()


def _split_row(line: str) -> list[str]:
    """The cells of one Markdown table row; ``\\|`` is a pipe inside a cell."""
    text = line.strip()
    cells: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and text[i + 1 : i + 2] == "|":
            buf.append("|")
            i += 2
            continue
        if text[i] == "|":
            cells.append("".join(buf))
            buf = []
        else:
            buf.append(text[i])
        i += 1
    cells.append("".join(buf))
    if text.startswith("|"):
        cells = cells[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        cells = cells[:-1]
    return [c.strip() for c in cells]


def _is_table_row(line: str) -> bool:
    return "|" in line.replace("\\|", "")


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(_SEPARATOR_CELL.match(c.replace(" ", "")) for c in cells)


def _unwrap_code(cell: str) -> str:
    """The message inside a backticked Say cell — `` `is sam around` `` → ``is sam around``."""
    run = len(cell) - len(cell.lstrip("`"))
    if run and len(cell) > 2 * run and cell.endswith("`" * run):
        inner = cell[run:-run]
        if "`" * run not in inner:
            return inner.strip()
    return cell


def parse_scenarios(text: str, file_name: str) -> list[ScenarioCase] | None:
    """The rows of a file's ``Say | Expect`` table; ``None`` when it has none.

    Only the first table under a heading reading ``Say | Expect`` counts — a
    table under ``Fixtures`` or a before/after table further down is context,
    not a case. The Say column and the Expect column are found by their header
    (first and second column otherwise); extra columns are ignored.
    """
    section_level: int | None = None
    columns: tuple[int, int] | None = None
    pending: list[str] | None = None
    in_fence = False
    cases: list[ScenarioCase] = []
    row = 0

    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if line.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            if section_level is not None and level <= section_level:
                break
            if section_level is None and _key(heading.group(2)) == "say|expect":
                section_level = level
            continue
        if section_level is None:
            continue

        if columns is not None:
            if not _is_table_row(line):
                break  # the table ended
            cells = _split_row(line)
            row += 1
            say_at, expect_at = columns
            say = _unwrap_code(cells[say_at]) if say_at < len(cells) else ""
            say = _BR.sub("\n", say).strip()
            if not say:
                continue
            expect = cells[expect_at] if expect_at < len(cells) else ""
            cases.append(ScenarioCase(file_name, row, number, say, expect))
            continue

        if not _is_table_row(line):
            pending = None
            continue
        cells = _split_row(line)
        if pending is not None and _is_separator(cells):
            keys = [_key(c) for c in pending]
            columns = (
                keys.index("say") if "say" in keys else 0,
                keys.index("expect") if "expect" in keys else 1,
            )
            continue
        pending = cells

    return cases if columns is not None else None


def scenario_files(folder: Path) -> list[Path]:
    """Every scenario file in ``folder``: its ``*.md`` files except README.md."""
    return sorted(
        p for p in folder.glob("*.md") if p.is_file() and p.name.lower() != "readme.md"
    )


def load_scenarios(folder: Path, names: tuple[str, ...] = ()) -> list[ScenarioFile]:
    """Parse the folder's scenario files, or only the ``names`` given."""
    paths = scenario_files(folder)
    if names:
        picked: list[Path] = []
        for name in names:
            path = _pick_file(folder, paths, name)
            if path not in picked:
                picked.append(path)
        paths = picked
    out = []
    for path in paths:
        cases = parse_scenarios(path.read_text(encoding="utf-8"), path.name)
        out.append(ScenarioFile(path, cases or [], cases is not None))
    return out


def _pick_file(folder: Path, paths: list[Path], name: str) -> Path:
    for path in paths:
        if name in (path.name, path.stem):
            return path
    candidate = Path(name).expanduser()
    if candidate.is_file():
        return candidate
    available = ", ".join(p.name for p in paths) or "none"
    raise click.ClickException(
        f"No scenario file '{name}' in {_display_path(folder)}.\nAvailable: {available}"
    )


def resolve_scenarios_dir(agent_ref: str, path_opt: str | None) -> Path:
    """``--path`` (or the scenario folder inside it), else the synced workspace's."""
    if path_opt:
        base = Path(path_opt).expanduser()
        candidates = [base / SCENARIOS_SUBDIR, base / "workspace" / SCENARIOS_SUBDIR]
        folder = next((c for c in candidates if c.is_dir()), base)
    else:
        resolved = resolve_child_workspace(find_account_root(), agent_ref)
        if resolved is None:
            raise click.ClickException(
                f"'{agent_ref}' is not synced in this account workspace, so there is "
                "no local copy of its scenarios.\n"
                f"Run 'cinna agent sync {agent_ref}', or point at the folder with --path DIR."
            )
        folder = resolved[0] / "workspace" / SCENARIOS_SUBDIR
    if not folder.is_dir():
        raise click.ClickException(
            f"No scenario folder at {_display_path(folder)}.\n"
            "Record the cases as docs/test_scenarios/*.md, each with a "
            "'## Say | Expect' table (CHAT_TESTING.md, \"Record the conditions\"), "
            "or pass --path DIR."
        )
    return folder


# ── list ────────────────────────────────────────────────────────────────────


def run_scenarios_list(agent_ref: str, path_opt: str | None) -> None:
    """List scenario files and their row counts — `cinna agent scenarios list`."""
    from rich.table import Table

    folder = resolve_scenarios_dir(agent_ref, path_opt)
    files = load_scenarios(folder)
    console.console.print(f"Scenarios in [bold]{_display_path(folder)}[/bold]")
    if not files:
        console.warn("No scenario files here (README.md is not one).")
        return

    table = Table(box=None, show_header=False, padding=(0, 2))
    table.add_column("file")
    table.add_column("cases")
    for f in files:
        if not f.has_table:
            cell = "[yellow]no Say | Expect table — not run[/yellow]"
        else:
            cell = _plural(len(f.cases), "case")
        table.add_row(f.path.name, cell)
    console.console.print(table)

    total = sum(len(f.cases) for f in files)
    with_cases = sum(1 for f in files if f.cases)
    console.console.print(
        f"{_plural(total, 'case')} across {_plural(with_cases, 'file')} — run them with "
        f"'cinna agent scenarios run {agent_ref}'"
    )


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


# ── run ─────────────────────────────────────────────────────────────────────


def _tool_call(event: dict) -> str:
    """A tool call in one line: its name and the first line of its input."""
    name = event.get("tool_name") or "tool"
    payload = event.get("tool_input")
    if isinstance(payload, dict):
        if isinstance(payload.get("command"), str):
            payload = payload["command"]
        elif len(payload) == 1:
            payload = next(iter(payload.values()))
    if payload in (None, "", {}, []):
        return name
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    lines = text.strip().splitlines()
    first = lines[0] if lines else ""
    if len(first) > _TOOL_INPUT_WIDTH:
        first = first[: _TOOL_INPUT_WIDTH - 1] + "…"
    return f"{name} {first}".rstrip()


def _run_case(
    client: AccountClient,
    agent_id: str,
    case: ScenarioCase,
    timeout: int,
    interval: float,
) -> dict:
    """Send one Say row in a new session and record what the turn produced."""
    events: list[dict] = []
    emit = _Emitter(pretty=False, sink=events.append)
    started = time.monotonic()
    session_id = None
    error = None

    try:
        session = client.create_session(
            agent_id, mode="conversation", title=f"scenario {case.file} #{case.row}"
        )
        session_id = session["id"]
        consumed = _message_count(client, session_id)
        ack = client.send_message(session_id, case.say)
        expect_turn = bool(ack.get("streaming") or ack.get("pending") or ack.get("queued"))
        outcome = _poll_turn(
            client, session_id, emit, consumed, expect_turn, None, interval, timeout
        )
    except KeyboardInterrupt:
        if session_id:
            with contextlib.suppress(Exception):
                client.interrupt_message(session_id)
        raise
    except (httpx.TransportError, PlatformError) as exc:
        # Once the message is in, a lost connection says nothing about the
        # turn, and the next case must not wait on it.
        if session_id is None or not _is_transient(exc):
            raise
        outcome = "lost_contact"
        error = _describe_error(exc)

    replies = [
        e for e in events if e.get("event") == "message" and e.get("role") != "user"
    ]
    reply = "\n\n".join(
        m["content"] if m.get("role") != "system" else f"[system] {m['content']}"
        for m in replies
        if m.get("content")
    )
    result: dict = {
        "event": "case",
        "file": case.file,
        "row": case.row,
        "line": case.line,
        "say": case.say,
        "expect": case.expect,
        "outcome": outcome,
        "session_id": session_id,
        "duration_s": round(time.monotonic() - started, 1),
        "reply": reply,
        "tool_calls": [
            _tool_call(ev)
            for m in replies
            for ev in m.get("events", [])
            if ev.get("type") in _TOOL_EVENT_TYPES
        ],
    }
    status_messages = [m["status_message"] for m in replies if m.get("status_message")]
    if status_messages:
        result["status_messages"] = status_messages
    attachments = [a.get("filename") for m in replies for a in m.get("attachments", [])]
    if attachments:
        result["attachments"] = attachments
    if error:
        result["error"] = error
    if outcome != "completed":
        result["recover"] = f"cinna chat --attach {session_id}"
    return result


def _conversation_model(client: AccountClient, agent: dict) -> str | None:
    """The effective conversation model, for the report header — best-effort."""
    try:
        env = _agent_environment(client, agent)
    except (click.ClickException, httpx.TransportError):
        return None
    mode = _model_view(agent, env)["conversation"]
    if not mode["effective"]:
        return None
    label = mode["effective"]
    if mode["override"]:
        label += " (override)"
    if mode["sdk"]:
        label += f" via {mode['sdk']}"
    return label


def _indent(label: str, value: str) -> str:
    lines = (value or "").splitlines() or [""]
    pad = " " * (len(label) + 2)
    return "\n".join([f"  {label}{lines[0]}"] + [f"{pad}{line}" for line in lines[1:]])


def _print_case(result: dict, index: int, total: int) -> None:
    color = "green" if result["outcome"] == "completed" else "yellow"
    console.console.print()
    console.console.print(
        f"[dim]\\[{index}/{total}][/dim] [bold]{result['file']} #{result['row']}[/bold] — "
        f"[{color}]{result['outcome']}[/{color}] in {result['duration_s']}s — "
        f"[dim]session {result['session_id']}[/dim]"
    )
    click.echo(_indent("Say:     ", result["say"]))
    click.echo(_indent("Expect:  ", result["expect"]))
    click.echo(_indent("Reply:   ", result["reply"] or "(no reply)"))
    click.echo(_indent("Tools:   ", "\n".join(result["tool_calls"]) or "(none)"))
    for message in result.get("status_messages", []):
        click.echo(_indent("Status:  ", message))
    if result.get("error"):
        click.echo(_indent("Error:   ", result["error"]))
    if result.get("recover"):
        click.echo(_indent("Recover: ", result["recover"]))


def _cell(value: str, width: int | None = None) -> str:
    text = (value or "").strip()
    if width and len(text) > width:
        text = text[: width - 1] + "…"
    return text.replace("|", "\\|").replace("\n", "<br>")


def _report_markdown(summary: dict, results: list[dict]) -> str:
    """The run as Markdown: one Say | Expect | Reply table per file, then each case."""
    outcomes = ", ".join(f"{n} {o}" for o, n in summary["outcomes"].items()) or "none"
    lines = [
        f"# Scenario run — {summary['agent_name']}",
        "",
        f"- **When:** {summary['started_at']}",
        f"- **Conversation model:** {summary['conversation_model'] or 'unknown'}",
        f"- **Scenarios:** `{summary['folder']}`",
        f"- **Cases:** {summary['run']} of {summary['cases']} run — {outcomes}",
        "",
        "The run records; it does not judge. Fill in **Verdict** against each "
        "Expect, and keep a before/after row in the scenario file for any case "
        "that was failing.",
    ]
    by_file: dict[str, list[dict]] = {}
    for result in results:
        by_file.setdefault(result["file"], []).append(result)
    for file_name, rows in by_file.items():
        lines += [
            "",
            f"## {file_name}",
            "",
            "| # | Say | Expect | Reply | Tools | Outcome | Verdict |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in rows:
            lines.append(
                f"| {r['row']} | `{_cell(r['say'])}` | {_cell(r['expect'])} | "
                f"{_cell(r['reply'], _REPORT_REPLY_WIDTH)} | "
                f"{_cell('; '.join(r['tool_calls'])) or 'none'} | {r['outcome']} |  |"
            )
    lines += ["", "## Cases"]
    for r in results:
        lines += [
            "",
            f"### {r['file']} #{r['row']} — {r['outcome']}",
            "",
            f"- **Say:** `{r['say']}`",
            f"- **Expect:** {r['expect']}",
            f"- **Session:** `{r['session_id']}` ({r['duration_s']}s)",
        ]
        if r.get("recover"):
            lines.append(f"- **Recover:** `{r['recover']}`")
        if r.get("error"):
            lines.append(f"- **Error:** {r['error']}")
        lines += ["- **Tools:**" + ("" if r["tool_calls"] else " none")]
        lines += [f"  - `{call}`" for call in r["tool_calls"]]
        lines += ["", "**Reply:**", ""]
        lines += [f"> {line}" for line in (r["reply"] or "(no reply)").splitlines()]
    return "\n".join(lines) + "\n"


def run_scenarios_run(
    agent_ref: str,
    file_names: tuple[str, ...],
    path_opt: str | None = None,
    timeout: int = 600,
    as_json: bool = False,
    out: str | None = None,
    yes: bool = False,
    interval: float = DEFAULT_POLL_INTERVAL,
) -> None:
    """Send every Say row and report the turns — `cinna agent scenarios run`."""
    _require_yes_for_json(as_json, yes, "a scenario run (each case is a real agent turn)")
    folder = resolve_scenarios_dir(agent_ref, path_opt)
    files = load_scenarios(folder, file_names)
    cases = [case for f in files for case in f.cases]
    with_cases = [f for f in files if f.cases]
    if not cases:
        raise click.ClickException(
            f"No Say | Expect rows to run in {_display_path(folder)} — "
            f"'cinna agent scenarios list {agent_ref}' shows what each file holds."
        )

    account_cfg = load_account_config(find_account_root())
    with AccountClient(account_cfg) as client:
        agent = _resolve_account_agent(client.list_account_agents().get("data", []), agent_ref)
        model = _conversation_model(client, agent)

        if not as_json:
            console.console.print(
                f"Agent: [bold]{agent['name']}[/bold] — conversation model: "
                f"{model or 'unknown'}"
            )
            console.console.print(f"Scenarios: {_display_path(folder)}")
            for f in files:
                if not f.has_table:
                    console.warn(f"{f.path.name}: no Say | Expect table — skipped")
            console.console.print(
                f"{_plural(len(cases), 'case')} across {_plural(len(with_cases), 'file')} — "
                "each is a real agent turn."
            )
        if not yes and not console.confirm("Run them?", default=False):
            raise click.Abort()

        summary: dict = {
            "event": "summary",
            "agent_id": agent["id"],
            "agent_name": agent["name"],
            "conversation_model": model,
            "folder": _display_path(folder),
            "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "cases": len(cases),
        }
        results: list[dict] = []
        try:
            for index, case in enumerate(cases, 1):
                progress = (
                    contextlib.nullcontext()
                    if as_json
                    else console.spinner(f"[{index}/{len(cases)}] {case.file} #{case.row}…")
                )
                with progress:
                    result = _run_case(client, agent["id"], case, timeout, interval)
                results.append(result)
                if as_json:
                    click.echo(json.dumps(result, ensure_ascii=False))
                else:
                    _print_case(result, index, len(cases))
        finally:
            outcomes: dict[str, int] = {}
            for result in results:
                outcomes[result["outcome"]] = outcomes.get(result["outcome"], 0) + 1
            incomplete = [r for r in results if r["outcome"] != "completed"]
            summary.update(
                run=len(results),
                completed=outcomes.get("completed", 0),
                outcomes=outcomes,
                incomplete=[
                    {"file": r["file"], "row": r["row"], "outcome": r["outcome"], "recover": r["recover"]}
                    for r in incomplete
                ],
            )
            if out:
                Path(out).write_text(_report_markdown(summary, results), encoding="utf-8")

    if as_json:
        click.echo(json.dumps(summary, ensure_ascii=False))
    else:
        console.console.print()
        counts = ", ".join(f"{n} {o}" for o, n in outcomes.items())
        console.console.print(
            f"[bold]{_plural(len(results), 'case')}:[/bold] {counts}. Mark each against its "
            "Expect — the run does not judge."
        )
        for r in incomplete:
            console.console.print(
                f"  [yellow]{r['file']} #{r['row']} {r['outcome']}[/yellow] — {r['recover']}"
            )
        if out:
            console.status(f"Report written to {out}")

    if incomplete:
        raise CinnaExit(
            EXIT_ERROR,
            "scenarios_incomplete",
            f"{len(incomplete)} of {len(results)} cases did not complete. Nothing was "
            "re-sent — each turn may still be running; wait for it with its "
            "'cinna chat --attach' command.",
        )
