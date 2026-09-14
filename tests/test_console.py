"""Console width when the CLI's output is not a terminal."""

import io

from cinna import console


def test_a_pipe_gets_room_for_a_table(monkeypatch):
    """Rich sizes a pipe at 80 columns — how a coding agent runs the CLI — and
    `credentials list` wrapped every id and slot mid-word."""
    monkeypatch.delenv("COLUMNS", raising=False)
    assert console._Console(file=io.StringIO()).width >= console._PIPE_WIDTH


def test_columns_still_sets_the_width(monkeypatch):
    monkeypatch.setenv("COLUMNS", "100")
    assert console._Console(file=io.StringIO()).width == 100


def test_an_explicit_width_still_wins(monkeypatch):
    monkeypatch.delenv("COLUMNS", raising=False)
    assert console._Console(file=io.StringIO(), width=90).width == 90
