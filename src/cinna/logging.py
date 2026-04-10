"""File-based logging for debugging CLI issues."""

import logging
import logging.handlers
from pathlib import Path

LOG_FILE = "cinna.log"


def setup_logging(verbose: bool = False) -> None:
    """Configure file logging. Logs to ./cinna.log in the current directory."""
    try:
        log_path = Path.cwd() / LOG_FILE
    except (FileNotFoundError, OSError):
        raise SystemExit(
            "Error: Current directory no longer exists.\n"
            "This usually happens after 'cinna disconnect-all' deletes the workspace.\n"
            "Run: cd ~  (or any existing directory) and try again."
        )

    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,  # 5MB
        backupCount=3,
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )

    root = logging.getLogger("cinna")
    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)

    if verbose:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        root.addHandler(console_handler)
