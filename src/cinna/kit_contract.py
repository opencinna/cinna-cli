"""The Local Agent Kit **contract**, as data — ``.cinna-kit/layout.json``.

cinna-core split the machine-readable half of the Local Agent Kit into a
versioned contract so that its three consumers — Cinna Desktop, a coding
assistant, and this CLI — apply one set of folder rules instead of each
re-deriving them from prose. This module is cinna-cli's reader for it.

Two rules travel with the data and are ported here rather than re-invented,
because both hosts must select the *same* file set forever:

* ``cloud_import_excludes`` — the glob list, with the contract's own matching
  semantics (``matches_pattern``). They are **not** ``fnmatch`` semantics: a
  pattern without ``**`` is anchored at the agent root, so ``README.md``
  excludes the agent's own README and never ``docs/README.md``.
* ``secret_files`` — the rules a glob list cannot express ("any ``.env.<suffix>``
  except ``.example``"), applied to the basename at any depth.

**The two fail in opposite directions, on purpose — do not harmonise them.**
A ``secret_files`` clause this build cannot evaluate resolves *toward* treating
the path as secret, because a leaked credential is unrecoverable. An unusable
``cloud_import_excludes`` list makes the exclude list unevaluable, and a caller
that would go on to emit a ``content_hash`` must refuse to emit one at all
rather than compute it a different way. The safe direction is a property of the
consequence, not a house style.

Ported from cinna-core's ``docs/local_agent_kit/tools/kit.py``, which is the
reference implementation, at contract version 1.0.0.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from cinna import console

#: Directory a kit/contract install lives in, found by walking up from an agent.
KIT_DIR = ".cinna-kit"

#: The contract's data file — the authority this module reads.
LAYOUT_FILENAME = "layout.json"

#: The kit index. Carries ``contract_version`` and is a member of the contract
#: tarball as well as the full kit.
KIT_INDEX = "kit.json"

#: Fallback source for the contract version when ``kit.json`` cannot supply it.
CONTRACT_VERSION_FILENAME = "CONTRACT_VERSION"

#: The contract version this CLI implements. Only the MAJOR component is a
#: compatibility question — see ``check_contract_compatibility``.
SUPPORTED_CONTRACT_VERSION = "1.0.0"


# ── Offline fallbacks ───────────────────────────────────────────────────────
#
# Both lists MUST stay content-identical to the shipped ``layout.json`` — same
# entries, same order — for the same reason kit.py's own fallbacks must: a
# fallback that diverges from the contract is a silent hash-parity break, where
# two hosts hash different file sets and the drift indicator never clears.
# ``tests/test_kit_contract.py`` asserts the identity against the real
# ``layout.json`` when one is reachable.

DEFAULT_EXCLUDE = [
    ".git/",
    "**/.git/",
    ".gitignore",
    "**/.gitignore",
    ".gitattributes",
    ".gitkeep",
    "**/.gitkeep",
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    "Makefile",
    "publications.json",
    ".claude/",
    ".cursor/",
    ".vscode/",
    ".idea/",
    "app-data/",
    "temp/",
    "credentials/",
    "credentials.json",
    "**/credentials.json",
    "credentials/.env",
    "**/.env",
    "**/.env.local",
    "**/*.env",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.tmp",
    "**/*.pyc",
    "__pycache__/",
    "**/__pycache__/",
    ".mypy_cache/",
    "**/.mypy_cache/",
    ".ruff_cache/",
    "**/.ruff_cache/",
    ".venv/",
    "venv/",
    "node_modules/",
    "**/.DS_Store",
    "**/Thumbs.db",
]

DEFAULT_SECRET_FILE_RULES = [
    {
        "id": "dotenv",
        "description": "Every dotenv shape: `.env`, `.env.<suffix>` and `<name>.env`.",
        "match": {
            "basename_equals": [".env"],
            "basename_prefix": [".env."],
            "basename_suffix": [".env"],
        },
        "unless": {"basename_suffix": [".example", ".sample", ".template"]},
    }
]

#: The ``match`` / ``unless`` clause vocabulary this build understands. A key
#: missing from here is a newer contract talking to an older tool, and resolves
#: through the fail-safe direction in ``_secret_clause_hits``.
_UNRENDERED_TOKEN_RE = re.compile(r"^\{\{.*\}\}$")

_SECRET_CLAUSE_TESTS = {
    "basename_equals": lambda name, value: name == value,
    "basename_prefix": lambda name, value: name.startswith(value),
    "basename_suffix": lambda name, value: name.endswith(value),
}


# ── Finding and reading the contract ────────────────────────────────────────


def find_kit_root(source: Path) -> Path | None:
    """Walk up from ``source`` looking for a ``.cinna-kit/`` install.

    Either the kit index or the contract's own data file marks the directory: a
    folder that received only the contract tarball (``cinna-contract.tar.gz``)
    carries both, but a hand-assembled one may carry only ``layout.json``, and
    the authority this module wants is that file rather than the index.
    """
    current = source.resolve()
    while True:
        candidate = current / KIT_DIR
        if (candidate / KIT_INDEX).is_file() or (candidate / LAYOUT_FILENAME).is_file():
            return candidate
        parent = current.parent
        if parent == current:
            return None
        current = parent


def load_layout(kit_root: Path | None) -> dict:
    """``layout.json`` as a dict, or ``{}`` when it cannot be read.

    Never raises. Every value it supplies has a built-in fallback here, so
    degrading costs nothing that was not already known — the same direction
    kit.py's ``layout_config()`` and the desktop's ``parseLayout`` take.
    """
    if kit_root is None:
        return {}
    path = kit_root / LAYOUT_FILENAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        console.warn(f"Ignoring unreadable {path}: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def _clean_patterns(patterns: list, source: str) -> list[str] | None:
    """The declared exclude list, or ``None`` when this build cannot use it.

    Whole or nothing: a list holding one unusable entry returns ``None`` rather
    than the usable subset, because a silently shortened exclude list is a
    narrower gate reported as a healthy one.

    Stray surrounding whitespace is stripped and **said out loud**. In kit.py a
    trailing space survives normalisation into the pattern body and the pattern
    then matches nothing — a directory silently dropped from the exclude set,
    with every individual step looking like it worked. Stripping is one of the
    two sanctioned fixes; the warning is what keeps it from being the silent
    acceptance that is neither.
    """
    cleaned: list[str] = []
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern.strip():
            return None
        stripped = pattern.strip()
        if stripped != pattern:
            console.warn(
                f"{source}: exclude pattern {pattern!r} has stray whitespace — "
                f"reading it as {stripped!r}."
            )
        cleaned.append(stripped)
    return cleaned


def contract_exclude_patterns(layout: dict, source: str) -> list[str] | None:
    """``cloud_import_excludes`` as the contract states it, or ``None``.

    ``None`` means "this build cannot evaluate the contract's own list", which
    is a different answer from "the list is empty" and must stay different. What
    *travels* may fall back to ``DEFAULT_EXCLUDE``; what is *hashed* may not —
    an unevaluable exclude list means the file set a ``content_hash`` would
    cover is unevaluable, and the rule for that is absolute: refuse to emit one
    at all, never emit one computed a different way.
    """
    patterns = layout.get("cloud_import_excludes")
    if not isinstance(patterns, list) or not patterns:
        return None
    return _clean_patterns(patterns, source)


def secret_file_rules(layout: dict) -> list:
    """``secret_files.rules``, falling back to the same rule built in.

    A declared list is returned **as declared**, entries this build cannot read
    included. Filtering them out here would disable a fail-safe one function
    away: ``is_secret_filename`` treats an unreadable rule as "assume it
    protects something", and no unreadable rule could ever reach it if this
    function had already dropped it.
    """
    block = layout.get("secret_files")
    if isinstance(block, dict):
        rules = block.get("rules")
        if isinstance(rules, list) and rules:
            return list(rules)
    return [dict(rule) for rule in DEFAULT_SECRET_FILE_RULES]


def kit_contract_version(kit_root: Path | None) -> str | None:
    """The contract version the kit next to the agent implements, or ``None``.

    ``kit.json`` is the primary read: it is the file the platform renders and a
    refresh swaps, it travels in the contract tarball as well as the full kit,
    and it is half the identity pair a consumer detects a contract tree by. The
    ``CONTRACT_VERSION`` fallback covers a narrower case that a served tree
    should never be in but a hand-assembled or half-refreshed one can: a
    ``.cinna-kit/`` whose ``kit.json`` is absent, or present without a usable
    ``contract_version``.
    """
    if kit_root is None:
        return None
    index = kit_root / KIT_INDEX
    if index.is_file():
        try:
            data = json.loads(index.read_text())
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            value = data.get("contract_version")
            if isinstance(value, str) and value.strip():
                return value.strip()
    fallback = kit_root / CONTRACT_VERSION_FILENAME
    if fallback.is_file():
        try:
            text = fallback.read_text().strip()
        except OSError:
            return None
        # An unrendered template token is not a version.
        if text and not _UNRENDERED_TOKEN_RE.match(text):
            return text
    return None


# ── The secret gate ─────────────────────────────────────────────────────────


def _secret_clause_hits(name: str, clause: object, *, on_unknown: bool) -> bool:
    """Does any test in one ``match`` / ``unless`` clause fire for this basename?

    ``on_unknown`` is the declared fail-safe direction, and it differs by
    position on purpose: an unevaluable ``match`` counts as a hit and an
    unevaluable ``unless`` counts as a miss, so both resolve toward treating the
    path as secret.

    "Cannot evaluate" is deliberately broad — an absent clause, a clause that is
    not an object, an empty one, an unknown key, or a known key whose values
    hold no usable string. Resolving any of them the other way turns the secret
    gate into a silent no-op, and ``cloud_import_excludes`` does not cover
    ``.env.prod`` or ``config/.env.staging``.
    """
    if not isinstance(clause, dict) or not clause:
        return on_unknown
    for key, values in clause.items():
        test = _SECRET_CLAUSE_TESTS.get(key)
        if test is None:
            return on_unknown
        if isinstance(values, str):
            values = [values]
        usable = (
            [value for value in values if isinstance(value, str) and value]
            if isinstance(values, list)
            else []
        )
        if not usable:
            return on_unknown
        if any(test(name, value) for value in usable):
            return True
    return False


def is_secret_filename(name: str, rules: list) -> bool:
    """True when the contract says this basename can hold a credential value.

    A caller may pass ``rules=[]`` to mean "no secret filtering at all" — an
    explicit local choice, and the one way the gate is off. A contract that
    declares rules this build cannot read withholds everything instead, which is
    loud and recoverable rather than silent and not.
    """
    for rule in rules:
        if not isinstance(rule, dict):
            # A rule this build cannot even open is one it must assume protects
            # something.
            return True
        if not _secret_clause_hits(name, rule.get("match"), on_unknown=True):
            continue
        if _secret_clause_hits(name, rule.get("unless"), on_unknown=False):
            continue
        return True
    return False


# ── Pattern matching (the contract's semantics, not fnmatch's) ──────────────


def normalize_rel_path(rel_path: str) -> str:
    """POSIX, root-relative, no ``./``, no leading or trailing ``/``."""
    text = rel_path.replace("\\", "/")
    if text.startswith("./"):
        text = text[2:]
    return text.lstrip("/").rstrip("/")


def _to_code_units(text: str) -> str:
    """The string as UTF-16 code units, one Python character each.

    JavaScript indexes strings by code unit, so its ``[^/]`` — what ``?``
    compiles to — consumes one unit and therefore half of a non-BMP character,
    while Python's consumes a whole code point. Matching both sides in this
    representation makes ``?`` mean the same thing on both hosts.
    """
    if text.isascii():
        return text
    units = []
    for character in text:
        code_point = ord(character)
        if code_point > 0xFFFF:
            offset = code_point - 0x10000
            units.append(chr(0xD800 + (offset >> 10)))
            units.append(chr(0xDC00 + (offset & 0x3FF)))
        else:
            units.append(character)
    return "".join(units)


@functools.lru_cache(maxsize=512)
def _segment_matcher(pattern: str) -> "re.Pattern[str]":
    """One pattern segment as a regex: ``*`` → ``[^/]*``, ``?`` → ``[^/]``.

    Compiled unanchored and used with ``fullmatch``, not ``^…$`` with ``match``:
    Python's ``$`` also matches just before a trailing newline and JavaScript's
    does not, so a file named ``README.md\\n`` would be dropped here and kept
    there — different file sets, different ``content_hash``.
    """
    parts = []
    for character in _to_code_units(pattern):
        if character == "*":
            parts.append("[^/]*")
        elif character == "?":
            parts.append("[^/]")
        else:
            parts.append(re.escape(character))
    return re.compile("".join(parts))


def _match_segments(
    pattern_segments: tuple[str, ...], path_segments: tuple[str, ...]
) -> bool:
    """``**`` spans zero or more segments; every other segment matches one."""
    if not pattern_segments:
        return not path_segments
    if pattern_segments[0] == "**":
        rest = pattern_segments[1:]
        for skip in range(len(path_segments) + 1):
            if _match_segments(rest, path_segments[skip:]):
                return True
        return False
    if not path_segments:
        return False
    if not _segment_matcher(pattern_segments[0]).fullmatch(
        _to_code_units(path_segments[0])
    ):
        return False
    return _match_segments(pattern_segments[1:], path_segments[1:])


def matches_pattern(pattern: str, rel_path: str) -> bool:
    """One ``cloud_import_excludes`` pattern against one agent-root-relative path.

    * trailing ``/`` — the directory itself and everything beneath it
    * ``*`` — any run of characters inside one segment
    * ``?`` — one character inside one segment
    * ``**`` — zero or more whole segments
    * no leading ``**`` — anchored at the agent root, and the pattern must
      consume the whole path, so ``README.md`` drops the agent's own README and
      never ``docs/README.md``

    **Divergence from kit.py, deliberate.** kit.py picks the directory branch
    from the raw pattern (``pattern.rstrip().endswith("/")``) while normalisation
    strips only ``/``, so ``"temp/ "`` takes the directory branch with ``" "``
    left in the pattern body and matches nothing. Both sides of the choice are
    made on the stripped pattern here, so a stray space cannot silently void an
    exclude. See ``_clean_patterns``, which also says so out loud.
    """
    stripped = pattern.strip()
    body = normalize_rel_path(stripped)
    path = normalize_rel_path(rel_path)
    if not body or not path:
        return False

    pattern_segments = tuple(body.split("/"))
    path_segments = tuple(path.split("/"))

    if stripped.endswith("/"):
        for end in range(len(pattern_segments), len(path_segments) + 1):
            if _match_segments(pattern_segments, path_segments[:end]):
                return True
        return False
    return _match_segments(pattern_segments, path_segments)


def is_excluded(rel_posix: str, patterns: list[str]) -> bool:
    """True when any pattern drops this path.

    ``rel_posix`` is agent-root-relative and names a file *or* a directory: the
    export walk asks about directories before descending, so an excluded
    directory is never opened.
    """
    return any(matches_pattern(pattern, rel_posix) for pattern in patterns)


# ── Walking the tree the contract selects ───────────────────────────────────


def utf16_sort_key(path: str) -> bytes:
    """JavaScript's default string ordering, which is UTF-16 code-unit order.

    The sort order is part of the ``content_hash``: the lines are fed into one
    running digest. Plain ``sorted()`` agrees for ASCII and diverges for non-BMP
    characters, and the only symptom of the mismatch is a host reporting
    "unpublished changes" forever.
    """
    return path.encode("utf-16-be", "surrogatepass")


def collect_export_tree(
    agent_dir: Path, patterns: list[str], secret_rules: list
) -> tuple[list[str], list[str]]:
    """``(files, unreadable)`` — the paths that travel, and what could not be seen.

    Three details of the walk are load-bearing:

    * **Symlinks are never followed and never listed**, files and directories
      alike: a folder that travels must not reach outside itself.
    * **Excluded directories are never descended into**, so ``credentials/``
      costs one match rather than one per file.
    * **The secret rules are applied here**, in the one walk, rather than at copy
      time — a ``content_hash`` must describe the set that actually travels, or
      editing a withheld secret moves the hash for a change that can never be
      published.

    ``unreadable`` is every path the walk could not read *as a directory entry*.
    It is recorded rather than guessed: guessing that an unscannable directory is
    empty costs an export that silently omits a subtree and a hash nobody can
    trace back to it. Callers decide what to do with it.
    """
    files: list[str] = []
    unreadable: list[str] = []

    def walk(rel_dir: str) -> None:
        absolute = agent_dir if rel_dir == "" else agent_dir / rel_dir
        try:
            with os.scandir(absolute) as scan:
                entries = sorted(scan, key=lambda entry: entry.name)
        except OSError:
            unreadable.append(f"{rel_dir}/" if rel_dir else ".")
            return
        for entry in entries:
            relative = entry.name if rel_dir == "" else f"{rel_dir}/{entry.name}"
            try:
                is_link = entry.is_symlink()
                is_dir = not is_link and entry.is_dir(follow_symlinks=False)
                is_file = (
                    not is_link and not is_dir and entry.is_file(follow_symlinks=False)
                )
            except OSError:
                unreadable.append(relative)
                continue
            if is_link:
                continue
            if is_excluded(relative, patterns):
                continue
            # Applied to directories too: a `secrets.env/` folder is as much a
            # place to keep a value as a file of that name.
            if is_secret_filename(entry.name, secret_rules):
                continue
            if is_dir:
                walk(relative)
            elif is_file:
                files.append(relative)

    walk("")
    return sorted(files, key=utf16_sort_key), sorted(unreadable, key=utf16_sort_key)


# ── What one import run resolved out of the contract ────────────────────────


@dataclass
class ExportContract:
    """The rules one ``cinna agent import`` run will apply, and where they came from.

    ``hashable`` is the tristate ``contract_exclude_patterns`` returns, carried
    to the caller: ``False`` means the contract's own exclude list could not be
    evaluated, the file set is therefore not the one the other host would
    select, and nothing derived from it may be published as a ``content_hash``.

    ``kit_version`` is the contract version the kit beside the agent implements —
    the tool half of the ``check_contract_compatibility`` pair when a kit is
    present, and ``None`` when there is none to ask.
    """

    patterns: list[str]
    secret_rules: list
    origin: str
    layout_path: Path | None
    hashable: bool
    kit_version: str | None


def resolve_export_contract(
    source: Path, mandatory: tuple[str, ...] = ()
) -> ExportContract:
    """Read the contract next to ``source`` and say honestly what was found.

    ``mandatory`` is appended to whatever the contract supplies. A contract that
    drops ``credentials/`` must still not make this command copy ``credentials/``.

    The ``origin`` string is user-facing and is the reason this function exists
    in this shape. Before the contract landed, a missing exclude list printed
    ``built-in default list`` — a line that announces a *mode* when what
    happened was a *degradation*. Nobody sees an error; the first symptom is a
    file sitting in a cloud workspace that should never have left the machine.
    """
    kit_root = find_kit_root(source)
    layout_path = kit_root / LAYOUT_FILENAME if kit_root is not None else None
    layout = load_layout(kit_root)

    declared = contract_exclude_patterns(layout, str(layout_path or LAYOUT_FILENAME))
    if declared is not None:
        patterns = list(declared)
        origin = str(layout_path)
        hashable = True
    else:
        patterns = list(DEFAULT_EXCLUDE)
        hashable = False
        if layout_path is None:
            origin = (
                f"DEGRADED — no {KIT_DIR}/{LAYOUT_FILENAME} found above the agent; "
                f"using cinna-cli's built-in copy of contract "
                f"{SUPPORTED_CONTRACT_VERSION}"
            )
        else:
            origin = (
                f"DEGRADED — {layout_path} did not supply a usable "
                f"'cloud_import_excludes'; using cinna-cli's built-in copy of "
                f"contract {SUPPORTED_CONTRACT_VERSION}"
            )

    for pattern in mandatory:
        if pattern not in patterns:
            patterns.append(pattern)

    return ExportContract(
        patterns=patterns,
        secret_rules=secret_file_rules(layout),
        origin=origin,
        layout_path=layout_path,
        hashable=hashable,
        kit_version=kit_contract_version(kit_root),
    )


# ── The version gate ────────────────────────────────────────────────────────


#: Identical to the schema's own pattern and the desktop's ``parseSemver``, so
#: the hosts cannot disagree about which strings parse.
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-.]+))?$"
)


def parse_semver(value: object) -> tuple[int, int, int, str | None, str] | None:
    """Parse a semantic version; ``None`` for anything that is not one."""
    if not isinstance(value, str):
        return None
    match = SEMVER_RE.match(value.strip())
    if not match:
        return None
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        match.group(4),
        value.strip(),
    )


def check_contract_compatibility(
    agent_version: object, tool_version: object
) -> tuple[str, str]:
    """May a tool implementing ``tool_version`` operate a folder recording
    ``agent_version``?

    Returns ``(status, reason)`` where status is one of ``ok``, ``app_too_old``,
    ``migratable``, ``unknown``, and reason is one sentence that can be shown as
    written.

    **Only the major version decides.** A minor or patch difference is not a
    compatibility question at all — the contract's minor releases are additive
    by definition — which is also the constraint on what a future contract
    change can safely do: *a same-major pair passes*, so a minor-version change
    reaches a non-adopting reader as silence rather than as a warning. Anything
    that must be noticed by an old reader needs a major bump.

    The remedy differs by direction — a folder from a newer major needs a newer
    tool, a folder from an older major needs migrating — which is why the two
    are not one symmetric "mismatch".
    """
    agent = parse_semver(agent_version)
    tool = parse_semver(tool_version)

    if agent is None:
        return (
            "unknown",
            "this folder does not record a usable `contract_version`. It was "
            "created before contract 1.0.0 and should be re-stamped.",
        )
    if tool is None:
        return (
            "unknown",
            f"this folder records contract {agent[4]}, and no usable contract "
            "version is available to compare it against — the compatibility "
            "gate did not run.",
        )
    if agent[0] == tool[0]:
        return ("ok", f"contract {agent[4]} runs on contract {tool[4]}.")
    if agent[0] > tool[0]:
        return (
            "app_too_old",
            f"this folder needs contract {agent[0]}.x and this cinna-cli "
            f"implements {tool[4]}. Upgrade with 'uv tool upgrade cinna-cli'.",
        )
    return (
        "migratable",
        f"this folder was built against contract {agent[4]} and this cinna-cli "
        f"implements {tool[4]}. It can be migrated — read the kit's CHANGELOG.md "
        "Breaking entries.",
    )


# ── content_hash ────────────────────────────────────────────────────────────


#: What the desktop substitutes for the hex digest of a file it could not read
#: (``UNREADABLE_MARKER`` in ``exportTree.ts``). It opens with a NUL of its own,
#: so the emitted line carries **two**: ``<relpath>\0\0unreadable\n``. Do not
#: "tidy" the leading NUL away — it is verified against their source.
UNREADABLE_MARKER = "\0unreadable"


def hash_export_files(agent_dir: Path, files: list[str]) -> tuple[str, list[str]]:
    """``(content_hash, unreadable)`` for one file list.

    One line per file, ``<relpath>`` NUL ``<sha256 hex of the bytes>`` LF, in
    UTF-16 code-unit order, fed into one running SHA-256. No mtimes, sizes,
    modes or directory entries: two machines holding the same files must produce
    the same string.

    A file whose bytes cannot be read is folded in as ``UNREADABLE_MARKER``
    rather than aborting — a race with a running agent must not fail a scan —
    **and is returned in the second element**. The digest stays stable and
    comparable but no longer describes the bytes that would be uploaded, so
    recorded on a publication it reads "up to date" forever. Every caller must
    decide what to do about a non-empty list.

    Parity limit worth knowing: for a filename that is not valid UTF-8 this host
    and the desktop hash *different strings* — Python decodes the name with
    ``surrogateescape``, Node with lossy U+FFFD replacement. ``surrogatepass``
    here only guarantees we stay deterministic and do not crash.
    """
    digest = hashlib.sha256()
    unreadable: list[str] = []
    for relative in sorted(files, key=utf16_sort_key):
        try:
            entry = hashlib.sha256((agent_dir / relative).read_bytes()).hexdigest()
        except OSError:
            unreadable.append(relative)
            entry = UNREADABLE_MARKER
        digest.update(f"{relative}\0{entry}\n".encode("utf-8", "surrogatepass"))
    return f"sha256:{digest.hexdigest()}", unreadable


class UnhashableTree(Exception):
    """The tree could not be hashed in a way another host would agree with.

    **Unevaluable ⇒ refuse to emit a ``content_hash`` at all, never emit one
    computed a different way.** A missing drift number is visible and
    recoverable; a plausible wrong one is neither — recorded on a publication it
    reads "up to date" forever, and no publish can clear it.
    """


def content_hash(agent_dir: Path, files: list[str]) -> str:
    """``sha256:<hex>`` over the file set that travels, or ``UnhashableTree``.

    ``files`` is the list the caller already walked, so the hash covers exactly
    the set that was selected rather than a second walk that might disagree.
    Passing it does not waive the refusal: an explicit list is a claim about
    which files travel, never a claim that they could be read.
    """
    digest, unreadable = hash_export_files(agent_dir, files)
    if unreadable:
        listing = ", ".join(unreadable)
        raise UnhashableTree(
            f"these files are part of the export but could not be read: {listing}"
        )
    return digest


# ── The publication ledger ──────────────────────────────────────────────────


#: The ledger's own filename. A **sibling** of the manifest and never a key
#: inside it: both hosts hash ``cinna-agent.json`` and neither exclude list
#: contains it, so a ``content_hash`` stored in the manifest would be a value
#: stored inside the file it is a hash of. The first publish computes h0 and
#: writes it in, the manifest bytes change, the next scan computes h1 ≠ h0, and
#: the folder reads "1 unpublished change" the instant the publish *succeeds* —
#: a number that never reaches zero, with nothing to explain it.
PUBLICATIONS_FILENAME = "publications.json"

#: ``publications.schema.json``'s per-entry rules, as data, so "will this entry
#: be written?" and "is this entry valid?" can never be answered differently.
LEDGER_REQUIRED_KEYS = ("platform_url", "agent_id")
LEDGER_OPTIONAL_STRING_KEYS = (
    "workspace",
    "imported_at",
    "updated_at",
    "contract_version",
    "content_hash",
)


def read_publications(agent_dir: Path) -> list[dict] | None:
    """Ledger entries; ``[]`` when there is no ledger; ``None`` when this build
    cannot read the one that is there.

    The three states are deliberately distinct, and conflating the last two is a
    data-loss bug: a caller that read an unparseable ledger as "empty" would
    overwrite it with whatever it migrated.

    Never raises. The top level is an **object** with a ``publications`` array,
    not a bare array, so a later contract can add a sibling key without breaking
    readers that already read ``publications``.
    """
    path = agent_dir / PUBLICATIONS_FILENAME
    if not path.is_file():
        return []
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    entries = document.get("publications")
    if entries is None:
        return []
    if not isinstance(entries, list):
        return None
    if any(not isinstance(entry, dict) for entry in entries):
        return None
    return list(entries)


def ledger_entry_is_placeable(entry: object) -> bool:
    """Can this entry be written into ``publications.json`` exactly as it stands?

    The guarantee: **this tool never writes a ledger its own reader then
    rejects.** The shape that breaks it is a partially populated legacy ``cloud``
    block — a real ``platform_url`` with ``agent_id`` null or absent — which a
    naive migration absorbs verbatim into ``"agent_id": null``, a value the
    schema rejects.

    Refusing to place such an entry is chosen over repairing it because there is
    no repair: ``agent_id`` cannot be invented, and dropping the entry to make
    the file valid would discard the ``platform_url`` record. Left in the
    manifest the data is intact and the user can complete it.
    """
    if not isinstance(entry, dict):
        return False
    for key in LEDGER_REQUIRED_KEYS:
        value = entry.get(key)
        if not isinstance(value, str) or not value.strip():
            return False
    for key in LEDGER_OPTIONAL_STRING_KEYS:
        value = entry.get(key)
        if value is not None and not isinstance(value, str):
            return False
    return True


def find_publication(entries: list[dict], platform_url: str) -> dict | None:
    """The entry for one instance, resolved by ``platform_url``.

    **The resolution key is part of the contract**, not an implementation
    detail: match on ``platform_url``, not on ``workspace`` — the desktop
    publishes directly through the account API and has no workspace to record —
    and not on position in the array.
    """
    for entry in entries:
        if entry.get("platform_url") == platform_url:
            return entry
    return None


def serialise(document: object) -> str:
    """The serialisation every host agrees on: 2-space indent, trailing newline."""
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def write_publications(agent_dir: Path, entries: list[dict]) -> None:
    (agent_dir / PUBLICATIONS_FILENAME).write_text(
        serialise({"publications": entries}), encoding="utf-8"
    )
