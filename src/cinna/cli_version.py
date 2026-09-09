"""Installed cinna-cli version vs. the version the platform pins.

A platform that offers local development to Cinna Desktop publishes the
cinna-cli version it supports in its public discovery document
(``GET {platform}/.well-known/cinna-desktop`` → ``local_dev.cinna_cli_version``).
``cinna account status --json`` and ``cinna doctor`` compare the running
version against it so the desktop's reconciler (or a human) knows when to
re-run ``uv tool install cinna-cli==<pin>``.

Advisory only: no pin (older platform, no discovery document, no network)
means ``state: "unknown"`` — never an error.
"""

import json
import logging
import re
from urllib.parse import urlparse
from urllib.request import url2pathname

import httpx

from cinna import __version__

logger = logging.getLogger("cinna.cli_version")

DISCOVERY_PATH = "/.well-known/cinna-desktop"
_DISCOVERY_TIMEOUT = httpx.Timeout(5.0, connect=3.0)


def platform_origin(platform_url: str) -> str:
    """``scheme://netloc`` of a stored platform URL (drops any ``/api`` path)."""
    parsed = urlparse(platform_url.strip())
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return platform_url.rstrip("/")


def required_cli_version_from(data: dict) -> str | None:
    """Pull the pin out of a discovery / sync-runtime payload, if present."""
    if not isinstance(data, dict):
        return None
    local_dev = data.get("local_dev")
    if isinstance(local_dev, dict) and local_dev.get("cinna_cli_version"):
        return str(local_dev["cinna_cli_version"])
    if data.get("cinna_cli_version"):
        return str(data["cinna_cli_version"])
    return None


def fetch_required_cli_version(platform_url: str) -> str | None:
    """The cinna-cli version the platform pins, or ``None`` when it does not
    say (no discovery document, no ``local_dev`` block, unreachable)."""
    url = f"{platform_origin(platform_url)}{DISCOVERY_PATH}"
    try:
        response = httpx.get(url, timeout=_DISCOVERY_TIMEOUT, follow_redirects=True)
        if response.status_code != 200:
            return None
        return required_cli_version_from(response.json())
    except Exception as exc:  # noqa: BLE001 — advisory, never fatal
        logger.debug("cinna-cli version pin lookup failed (%s): %s", url, exc)
        return None


def editable_install_source(distribution: str = "cinna-cli") -> str | None:
    """The checkout an editable install points at, or ``None``.

    A ``uv tool install -e <checkout>`` (or ``pip install -e``) records its
    metadata **once**, at install time, and never again — the code that runs is
    the checkout, but ``importlib.metadata.version`` keeps answering whatever
    ``pyproject.toml`` said on the day it was installed. Comparing that stale
    number against the platform pin produces a confident, wrong "you are behind
    the pin", and the missing features it seems to explain are usually already
    in the tree the developer is standing in.

    PEP 610 is what makes the case detectable: an editable install writes
    ``direct_url.json`` beside its metadata with ``dir_info.editable`` set.
    Every failure here — no metadata, no file, unreadable JSON — returns
    ``None``, i.e. "an ordinary install", because a false *editable* would
    silence a warning a real deployment needs.
    """
    try:
        from importlib.metadata import distribution as _distribution

        raw = _distribution(distribution).read_text("direct_url.json")
    except Exception as exc:  # noqa: BLE001 — advisory, never fatal
        logger.debug("direct_url.json lookup failed: %s", exc)
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    dir_info = data.get("dir_info")
    if not isinstance(dir_info, dict) or not dir_info.get("editable"):
        return None
    url = str(data.get("url") or "")
    if url.startswith("file://"):
        return url2pathname(urlparse(url).path) or None
    return url or None


def _version_key(v: str) -> tuple[int, ...] | None:
    match = re.match(r"\s*v?(\d+(?:\.\d+)*)", v or "")
    if not match:
        return None
    parts = tuple(int(p) for p in match.group(1).split("."))
    return parts + (0,) * (3 - len(parts))


def compare_cli_version(installed: str, required: str | None) -> str:
    """``current`` / ``behind`` / ``ahead`` / ``unknown``.

    Only the numeric ``major.minor.patch`` prefix is compared; a source-tree
    ``0.0.0+unknown`` install is always ``unknown``.
    """
    if not required:
        return "unknown"
    have, want = _version_key(installed), _version_key(required)
    if have is None or want is None or installed.startswith("0.0.0"):
        return "unknown"
    if have == want:
        return "current"
    return "behind" if have < want else "ahead"


def cli_version_status(platform_url: str) -> dict:
    """``{"installed", "required", "state", "editable"}`` for one platform.

    ``editable`` is the checkout path when this is an editable install and
    ``None`` otherwise. It forces ``state`` to ``unknown``: the recorded
    version is not the version of the code that will run, so *no* comparison
    against the pin is meaningful — the same reason a bare source tree
    (``0.0.0+unknown``) is already ``unknown`` rather than "behind".
    """
    required = fetch_required_cli_version(platform_url)
    editable = editable_install_source()
    return {
        "installed": __version__,
        "required": required,
        "state": "unknown" if editable else compare_cli_version(__version__, required),
        "editable": editable,
    }


def cli_version_hint(status: dict) -> str | None:
    """One-line nudge when the installed CLI is not the pinned one.

    Never for an editable checkout: ``state`` is ``unknown`` there, so the
    upgrade line — which would tell a developer to replace their own checkout
    with a release — is not reachable. :func:`cli_version_label` says what is
    true about that install instead.
    """
    state = status.get("state")
    if state == "behind":
        return (
            f"cinna-cli {status['installed']} is behind the platform pin "
            f"{status['required']} — upgrade with: "
            f"uv tool install cinna-cli=={status['required']}"
        )
    if state == "ahead":
        return (
            f"cinna-cli {status['installed']} is newer than the platform pin "
            f"{status['required']} (usually fine; pin with: "
            f"uv tool install cinna-cli=={status['required']})"
        )
    return None


def cli_version_label(status: dict) -> str:
    """How to name the running CLI in a report — `cinna doctor`, `account status`.

    An editable install is labelled as one, because its number is a date stamp
    rather than a version: "0.2.5 (editable checkout of ~/dev/cinna-cli)" is
    the whole explanation for a number that does not match the tree's
    ``pyproject.toml``, and the reader stops looking for version skew.
    """
    installed = str(status.get("installed", "?"))
    editable = status.get("editable")
    if not editable:
        return installed
    return f"{installed} (editable checkout of {editable})"
