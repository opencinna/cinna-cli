"""Consistent terminal output using Rich."""

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

console = Console()


def status(msg: str):
    """Print a status message."""
    console.print(f"[green]\u2713[/green] {msg}")


def warn(msg: str):
    console.print(f"[yellow]![/yellow] {msg}")


def error(msg: str):
    console.print(f"[red]\u2717[/red] {msg}")


def step(n: int, total: int, msg: str):
    """Print a setup step: [1/6] msg"""
    console.print(f"[dim]\\[{n}/{total}][/dim] {msg}")


def spinner(msg: str):
    """Return a Rich status context manager."""
    return console.status(f"[bold]{msg}[/bold]", spinner="dots")


def file_progress():
    """Return a progress bar for file operations."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total} files"),
    )
