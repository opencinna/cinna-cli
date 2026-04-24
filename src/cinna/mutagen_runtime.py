"""Mutagen install/verification helpers.

The platform pins a specific Mutagen version and exposes it via GET /sync-runtime.
This module checks what the local machine has, prompts to install if missing,
and gates `cinna setup` / `cinna sync start` on a version match.
"""

import logging
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone

import click

from cinna.client import PlatformClient
from cinna.config import CinnaConfig, save_config
from cinna.errors import MutagenNotFoundError, MutagenVersionMismatchError
from cinna import console

logger = logging.getLogger("cinna.mutagen_runtime")


@dataclass
class InstalledMutagen:
    path: str
    version: str


@dataclass
class RequiredMutagen:
    version: str
    agent_sha256: str
    platform_api_version: str


def detect_local_mutagen() -> InstalledMutagen | None:
    """Locate `mutagen` on PATH and parse its version."""
    path = shutil.which("mutagen")
    if not path:
        return None

    try:
        result = subprocess.run(
            [path, "version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    version = _parse_mutagen_version(result.stdout)
    if not version:
        logger.warning("Could not parse Mutagen version from: %r", result.stdout)
        return None
    return InstalledMutagen(path=path, version=version)


def _parse_mutagen_version(text: str) -> str | None:
    """Extract a semver-ish version string from `mutagen version` output."""
    match = re.search(r"(\d+\.\d+\.\d+(?:[-.][A-Za-z0-9]+)*)", text)
    return match.group(1) if match else None


def _minor_version(v: str) -> tuple[int, int] | None:
    """Return (major, minor) from a version string, ignoring patch/suffix."""
    match = re.match(r"(\d+)\.(\d+)", v or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def fetch_required_mutagen(
    client: PlatformClient, agent_id: str
) -> RequiredMutagen:
    """Ask the platform which Mutagen version to pin to."""
    data = client.get_sync_runtime(agent_id)
    return RequiredMutagen(
        version=data.get("mutagen_version", ""),
        agent_sha256=data.get("mutagen_agent_sha256", ""),
        platform_api_version=data.get("platform_api_version", ""),
    )


def install_mutagen(required: RequiredMutagen) -> None:
    """Platform-aware install. Prints the one-liner; the user runs it.

    We deliberately do not shell out to a package manager automatically — that
    is a privileged operation the user should authorize. We print the install
    command, wait for confirmation that they've run it, and re-detect.
    """
    system = platform.system()
    if system == "Darwin":
        cmd = "brew install mutagen-io/mutagen/mutagen"
    elif system == "Linux":
        cmd = (
            "See https://mutagen.io/documentation/introduction/installation for "
            "Linux install instructions (prebuilt tarballs on GitHub releases)."
        )
    else:
        cmd = "Mutagen supports Windows via WSL. See https://mutagen.io/ for details."

    console.console.print()
    console.console.print(
        f"Mutagen {required.version} is required for continuous sync."
    )
    console.console.print(f"  {cmd}")
    console.console.print()


def ensure_mutagen_ready(
    client: PlatformClient,
    config: CinnaConfig,
    workspace_root,
    *,
    interactive: bool = True,
) -> InstalledMutagen:
    """Verify a matching Mutagen install; prompt to install if missing.

    Updates `config.mutagen_version` and `last_sync_runtime_check_at` on success.
    Raises MutagenNotFoundError / MutagenVersionMismatchError on hard failures.
    """
    required = fetch_required_mutagen(client, config.agent_id)
    installed = detect_local_mutagen()

    if installed is None:
        install_mutagen(required)
        if interactive and click.confirm(
            "Run the command above, then press Enter to continue.", default=True
        ):
            installed = detect_local_mutagen()
        if installed is None:
            raise MutagenNotFoundError(required.version)

    if required.version and installed.version != required.version:
        req_minor = _minor_version(required.version)
        inst_minor = _minor_version(installed.version)
        same_minor = req_minor is not None and req_minor == inst_minor

        if same_minor:
            # Patch-level differences within the same minor version — Mutagen's
            # wire protocol is stable across these, so warn and continue.
            console.warn(
                f"Mutagen {installed.version} differs from platform pin "
                f"{required.version} (patch-level only — proceeding)."
            )
        else:
            if interactive:
                console.warn(
                    f"Installed Mutagen {installed.version} does not match required "
                    f"{required.version}."
                )
                if not click.confirm("Continue anyway?", default=False):
                    raise MutagenVersionMismatchError(
                        installed.version, required.version
                    )
            else:
                raise MutagenVersionMismatchError(installed.version, required.version)

    config.mutagen_version = installed.version
    config.last_sync_runtime_check_at = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    save_config(config, workspace_root)
    return installed
