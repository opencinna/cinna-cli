"""Consistent terminal output using Rich — plus the two process-wide
"how do we talk to a human" switches a driver process flips:

* ``json_mode`` (``--json``): Rich output is suppressed and ``step`` /
  ``status`` / ``warn`` / ``error`` emit **one JSON object per line on
  stdout** instead; spinners and progress bars become no-ops. The final
  ``{"result": …}`` line is emitted by the command (``emit_result``) or, on
  failure, by ``CinnaExit.show()``. Nothing else may write to stdout in this
  mode — logs stay in ``cinna.log`` (and on stderr with ``-v``).
* ``no_input`` (``--no-input`` / ``CINNA_NO_INPUT=1``): every prompt takes its
  default, or fails with ``needs_input`` when it has none. ``--json`` implies
  it (a prompt would corrupt the stream).

The switches live here, not on the Click context, so library code
(``account.py``, ``mutagen_runtime.py``, …) can honor them without threading a
context through every call.
"""

import contextlib
import json
import os
import sys

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

console = Console()

json_mode = False
no_input = False

# The numbered step currently in progress (``step()`` sets it) so ok / warn /
# fail lines emitted during a step carry its ``step`` / ``total``.
_current_step: tuple[int, int] | None = None


# ── mode switches ────────────────────────────────────────────────────────────


def set_json_mode(enabled: bool) -> None:
    """Switch JSON line output on/off. On implies ``no_input``."""
    global json_mode, console, _current_step
    json_mode = enabled
    _current_step = None
    # A quiet Console swallows every Rich print (tables, hints, panels) so the
    # stdout stream stays pure JSON; call sites keep using ``console.console``.
    console = Console(quiet=True) if enabled else Console()
    if enabled:
        set_no_input(True)


def set_no_input(enabled: bool) -> None:
    global no_input
    no_input = enabled


def env_no_input() -> bool:
    """``CINNA_NO_INPUT`` is truthy (``1`` / ``true`` / ``yes``)."""
    return os.environ.get("CINNA_NO_INPUT", "").strip().lower() in ("1", "true", "yes")


def interactive() -> bool:
    """Whether the CLI may stop and wait for a human: a TTY on stdin and no
    ``--no-input``. The TTY half keeps the ``curl … | python3 -`` bootstrap and
    piped spawns non-interactive even without the flag."""
    return not no_input and sys.stdin.isatty()


# ── prompting (honors --no-input) ────────────────────────────────────────────

_NO_DEFAULT = object()


def prompt(text: str, default=_NO_DEFAULT, **kwargs):
    """``click.prompt`` that never blocks under ``--no-input``.

    With the switch on, returns ``default`` when one is given and raises
    ``NeedsInputError`` (exit 1, code ``needs_input``) otherwise. Without it,
    behaves exactly like ``click.prompt``.
    """
    if no_input:
        if default is _NO_DEFAULT or default is None:
            from cinna.errors import NeedsInputError

            raise NeedsInputError(text)
        return default
    if default is _NO_DEFAULT:
        return click.prompt(text, **kwargs)
    return click.prompt(text, default=default, **kwargs)


def confirm(text: str, default: bool | None = False, **kwargs) -> bool:
    """``click.confirm`` that never blocks under ``--no-input``.

    Every confirmation has a default (Click's is ``No``), so under the switch
    it simply returns that default — a "Continue?" that defaults to No aborts
    exactly as if the user had pressed Enter. ``default=None`` (Click's
    "no default, must answer") raises ``NeedsInputError`` instead. ``abort=True``
    is honored the way Click does it.
    """
    if no_input:
        if default is None:
            from cinna.errors import NeedsInputError

            raise NeedsInputError(text)
        if not default and kwargs.get("abort"):
            raise click.Abort()
        return default
    return click.confirm(text, default=default, **kwargs)


# ── output ───────────────────────────────────────────────────────────────────


def emit_json(obj: dict) -> None:
    """Write one JSON object as a single stdout line (JSON mode only)."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def emit_result(**fields) -> None:
    """The final success line of a ``--json`` command: ``{"result": "ok", …}``.

    A no-op outside JSON mode so command bodies can call it unconditionally.
    """
    if json_mode:
        emit_json({"result": "ok", **fields})


def _progress(status: str, msg: str) -> None:
    obj: dict = {}
    if _current_step is not None:
        obj["step"], obj["total"] = _current_step
    obj["status"] = status
    obj["message"] = msg
    emit_json(obj)


def status(msg: str):
    """Print a status message."""
    if json_mode:
        _progress("ok", msg)
        return
    console.print(f"[green]✓[/green] {msg}")


def warn(msg: str):
    if json_mode:
        _progress("warn", msg)
        return
    console.print(f"[yellow]![/yellow] {msg}")


def error(msg: str):
    if json_mode:
        _progress("fail", msg)
        return
    console.print(f"[red]✗[/red] {msg}")


def step(n: int, total: int, msg: str):
    """Print a setup step: [1/6] msg"""
    global _current_step
    _current_step = (n, total)
    if json_mode:
        _progress("start", msg)
        return
    console.print(f"[dim]\\[{n}/{total}][/dim] {msg}")


def spinner(msg: str):
    """Return a Rich status context manager (a no-op in JSON mode)."""
    if json_mode:
        return contextlib.nullcontext()
    return console.status(f"[bold]{msg}[/bold]", spinner="dots")


def file_progress():
    """Return a progress bar for file operations (disabled in JSON mode)."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total} files"),
        disable=json_mode,
    )
