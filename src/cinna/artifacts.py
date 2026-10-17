"""``cinna artifacts`` — publish a folder or a file to cinna-core and share it.

An artifact is a frozen, versioned copy of files stored in core: a small static
site (an ``index.html`` with its scripts and data) or a set of files core shows
in its viewers. ``publish`` zips the path, drops what must never leave the
machine (A10), uploads the zip and prints the core page. Publishing the same
path again adds a revision to the same artifact, so a shared link keeps
working: the CLI remembers ``path → artifact id`` in ``.cinna/artifacts.json``
(A12 — never a marker file in the published folder).

**Agents publish, people share (A8).** ``share`` and ``link create`` widen who
can see an artifact. They refuse under ``--json``, ``--no-input`` or without a
TTY on stdin, and the account workspace's ``.claude/settings.json`` denies
them to Claude Code. Both checks come from :data:`PERSON_ONLY_VERBS`, so agent
sharing can be relaxed later in one place. The CLI cannot tell an agent from a
person: this is a speed bump; core's agent-token route is the enforcement.

Publishing, the zip download and link creation use dedicated account routes
(``/cli/account/artifacts…``); the JSON verbs ride the api-proxy.
"""

import contextlib
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
import webbrowser
import zipfile
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
from rich.markup import escape

from cinna import console
from cinna.errors import EXIT_ERROR, CinnaExit, PlatformError
from cinna.kit_contract import is_excluded

SUPPORTED_VERSION = 1

MEMORY_FILE = "artifacts.json"
DOWNLOAD_DIR = "artifacts"
SHORT_ID_LEN = 8

#: Verbs that change who can see an artifact: a person runs them in a terminal.
#: The interactive guard (:func:`require_person`) and the Claude Code deny rules
#: (:func:`person_only_deny_rules`) both read this tuple.
PERSON_ONLY_VERBS = ("share", "link create")

#: Mandatory exclusions (README A10), matched with the kit contract's pattern
#: engine against the casefolded path. Mirrors core's ingest, which drops the
#: same paths again on its side; the CLI adds ``.cinna/`` (it holds this
#: workspace's account token) and the kit's other secret shapes. Not the kit's
#: ``DEFAULT_EXCLUDE`` wholesale: that list also drops ``README.md`` and
#: ``CLAUDE.md``, which an artifact may well be.
_EXCLUDED_ANYWHERE = [
    ".git/",
    ".claude/",
    ".cinna/",
    ".env*",
    "*.env",
    "credentials/",
    "app-data/",
    "node_modules/",
    "*.pem",
    "*.key",
    "*.p12",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials.json",
    ".ds_store",
    "thumbs.db",
    "__pycache__/",
]
# Each at the root and at any depth: the engine's ``**/`` does not match zero
# segments in front of a folder pattern (the kit lists ``.git/`` and
# ``**/.git/`` for the same reason).
ARTIFACT_EXCLUDE = [p for name in _EXCLUDED_ANYWHERE for p in (name, f"**/{name}")]

# Core refuses a zip entry whose compression ratio is over its limit (100 by
# default) as a zip bomb, so an entry that would compress further is stored.
_MAX_DEFLATE_RATIO = 50

_DURATION_RE = re.compile(r"^(\d+)\s*([hdw])$")
_DURATION_UNITS = {"h": "hours", "d": "days", "w": "weeks"}


# ─── the interactive guard (A8) ──────────────────────────────────────────────


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


def require_person(verb: str) -> None:
    """Refuse a person-only verb when no person can be at the keyboard.

    No ``--yes`` bypass on purpose: an agent can pass any flag, so the only
    signal left is whether the CLI was started from a terminal.
    """
    if console.json_mode:
        reason = "--json is set"
    elif console.no_input:
        reason = "--no-input (or CINNA_NO_INPUT) is set"
    elif not _stdin_is_tty():
        reason = "stdin is not a terminal"
    else:
        return
    raise CinnaExit(
        EXIT_ERROR,
        "needs_person",
        f"`cinna artifacts {verb}` changes who can see an artifact, so a person "
        f"runs it in a terminal ({reason}). Agents publish; people share — "
        "ask the user to run it, or to share from the artifact's page.",
    )


def person_only_deny_rules() -> list[str]:
    """Claude Code ``permissions.deny`` rules for the person-only verbs."""
    return [f"Bash(cinna artifacts {verb}:*)" for verb in PERSON_ONLY_VERBS]


# ─── exclusions and the file walk ────────────────────────────────────────────


def is_artifact_excluded(rel_posix: str) -> bool:
    """True when a published-path-relative file or folder must not be uploaded."""
    return is_excluded(rel_posix.casefold(), ARTIFACT_EXCLUDE)


def refuse_excluded_source(source: Path) -> None:
    """Refuse a path that is itself excluded, or sits inside an excluded folder.

    The walk only sees paths relative to what was named, so
    ``publish .cinna/account.json`` or ``publish credentials`` would otherwise
    upload exactly what the exclusions exist to keep back.
    """
    for part in source.parts[1:]:
        if is_artifact_excluded(part):
            raise click.ClickException(
                f"Refusing to publish {source}: '{part}' is never uploaded "
                "(secrets, version control and tool state stay on the machine)."
            )


def collect_files(source: Path) -> tuple[list[tuple[Path, str]], list[str]]:
    """``(kept, excluded)`` for a file or a folder.

    ``kept`` is ``(absolute path, archive path)`` in a stable order;
    ``excluded`` lists what was left out — a folder as ``name/`` (never
    opened), symlinks and special files by path. Symlinks are never followed.
    """
    if not source.is_dir():
        name = source.name
        if is_artifact_excluded(name):
            return [], [name]
        return [(source, name)], []

    kept: list[tuple[Path, str]] = []
    excluded: list[str] = []
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        base = Path(dirpath)
        rel_base = base.relative_to(source).as_posix()
        prefix = "" if rel_base == "." else rel_base + "/"
        keep_dirs = []
        for name in sorted(dirnames):
            rel = prefix + name
            if (base / name).is_symlink():
                excluded.append(rel)
            elif is_artifact_excluded(rel):
                excluded.append(rel + "/")
            else:
                keep_dirs.append(name)
        dirnames[:] = keep_dirs
        for name in sorted(filenames):
            rel = prefix + name
            path = base / name
            if path.is_symlink() or not path.is_file() or is_artifact_excluded(rel):
                excluded.append(rel)
            else:
                kept.append((path, rel))
    return kept, excluded


def _deflate_ratio(path: Path) -> float:
    """Uncompressed / deflated size of one file (zipfile's default level)."""
    compressor = zlib.compressobj(zlib.Z_DEFAULT_COMPRESSION, zlib.DEFLATED, -15)
    raw = packed = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            raw += len(chunk)
            packed += len(compressor.compress(chunk))
    packed += len(compressor.flush())
    return raw / packed if packed else 0.0


def write_archive(files: list[tuple[Path, str]], destination: Path) -> None:
    """Zip ``files`` into ``destination``, storing what would compress too well."""
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, rel in files:
            compress = zipfile.ZIP_DEFLATED
            if path.stat().st_size and _deflate_ratio(path) > _MAX_DEFLATE_RATIO:
                compress = zipfile.ZIP_STORED
            archive.write(path, rel, compress_type=compress)


def _check_limits(files: list[tuple[Path, str]], limits: dict) -> int:
    """Refuse before uploading what core would refuse; returns the total bytes."""
    from cinna.account import _format_size

    sizes = [(rel, path.stat().st_size) for path, rel in files]
    total = sum(size for _, size in sizes)
    max_files = limits.get("max_revision_files")
    max_file = limits.get("max_file_bytes")
    max_total = limits.get("max_revision_bytes")
    if isinstance(max_files, int) and len(files) > max_files:
        raise click.ClickException(
            f"{len(files)} files is over the server's limit of {max_files} per artifact."
        )
    if isinstance(max_file, int):
        big = [rel for rel, size in sizes if size > max_file]
        if big:
            raise click.ClickException(
                f"Over the server's {_format_size(max_file)} per-file limit: "
                + ", ".join(big[:5])
                + (f" and {len(big) - 5} more" if len(big) > 5 else "")
            )
    if isinstance(max_total, int) and total > max_total:
        raise click.ClickException(
            f"{_format_size(total)} is over the server's limit of "
            f"{_format_size(max_total)} per artifact."
        )
    return total


# ─── path → artifact id memory (A12) ─────────────────────────────────────────


class ArtifactMemory:
    """``.cinna/artifacts.json``: which artifact a local path was published as.

    Entries carry the platform URL, so a workspace re-pointed at another
    instance never republishes into an id from the old one.
    """

    def __init__(self, account_root: Path, platform_url: str):
        from cinna.account import CONFIG_DIR

        self.path = account_root / CONFIG_DIR / MEMORY_FILE
        self.platform_url = platform_url.rstrip("/")

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        published = data.get("published") if isinstance(data, dict) else None
        return published if isinstance(published, dict) else {}

    def _save(self, published: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"version": 1, "published": published}, indent=2) + "\n")
        tmp.replace(self.path)

    def lookup(self, source: Path) -> str | None:
        entry = self._load().get(str(source))
        if not isinstance(entry, dict) or entry.get("platform_url") != self.platform_url:
            return None
        artifact_id = entry.get("artifact_id")
        return artifact_id if isinstance(artifact_id, str) and artifact_id else None

    def remember(self, source: Path, artifact_id: str, title: str | None) -> None:
        published = self._load()
        published[str(source)] = {
            "artifact_id": artifact_id,
            "platform_url": self.platform_url,
            "title": title,
            "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self._save(published)

    def forget(self, artifact_id: str) -> None:
        published = self._load()
        kept = {
            path: entry
            for path, entry in published.items()
            if not (isinstance(entry, dict) and entry.get("artifact_id") == artifact_id)
        }
        if kept != published:
            self._save(kept)


# ─── account session + server support ────────────────────────────────────────


class _Session:
    def __init__(self, account_root: Path, config, client, capabilities: dict):
        self.account_root = account_root
        self.config = config
        self.client = client
        self.capabilities = capabilities

    @property
    def memory(self) -> ArtifactMemory:
        return ArtifactMemory(self.account_root, self.config.platform_url)


@contextlib.contextmanager
def _session():
    """The account workspace's client, after checking the server has artifacts.

    A server without the capabilities route (404/405) or with another API
    version gets one clean message instead of a confusing failure later.
    """
    from cinna.account import find_account_root, load_account_config
    from cinna.client import AccountClient

    account_root = find_account_root()
    config = load_account_config(account_root)
    with AccountClient(config) as client:
        try:
            capabilities = client.get_artifacts_capabilities()
        except PlatformError as error:
            if error.status_code in (404, 405):
                raise _unsupported() from error
            raise
        if not isinstance(capabilities, dict) or (
            capabilities.get("version") != SUPPORTED_VERSION
        ):
            raise _unsupported()
        yield _Session(account_root, config, client, capabilities)


def _unsupported() -> CinnaExit:
    return CinnaExit(
        EXIT_ERROR,
        "artifacts_unsupported",
        "This Cinna server does not support artifacts (it predates them, or "
        f"speaks a version other than {SUPPORTED_VERSION}). Ask its admin to "
        "update cinna-core.",
    )


def _short(value) -> str:
    return str(value or "")[:SHORT_ID_LEN] or "?"


def _list_all(client, scope: str) -> list:
    """Every artifact in a scope, page by page (the server caps a page at 200)."""
    items: list = []
    while True:
        page = client.list_artifacts(scope=scope, skip=len(items))
        data = [i for i in (page.get("data") or []) if isinstance(i, dict)]
        items.extend(data)
        if not data or len(items) >= (page.get("count") or 0):
            return items


def _resolve_id(client, ref: str) -> str:
    """A full UUID, or a unique prefix of one of the caller's or shared ids."""
    ref = ref.strip()
    try:
        return str(uuid.UUID(ref))
    except ValueError:
        pass
    candidates = {}
    for scope in ("mine", "shared_with_me"):
        for item in _list_all(client, scope):
            if isinstance(item, dict) and str(item.get("id", "")).startswith(ref.lower()):
                candidates[str(item["id"])] = item
    if len(candidates) == 1:
        return next(iter(candidates))
    if candidates:
        ids = ", ".join(_short(i) for i in sorted(candidates))
        raise click.ClickException(
            f"Artifact id '{ref}' is ambiguous — matches: {ids}. Use more characters."
        )
    raise click.ClickException(
        f"No artifact '{ref}' among yours or those shared with you "
        "(see `cinna artifacts list`)."
    )


def _require_owner(detail: dict, what: str) -> None:
    if not detail.get("is_owner"):
        raise click.ClickException(
            f"Only the owner can {what} \"{detail.get('title') or detail.get('id')}\"."
        )


def _text(value, fallback: str = "—") -> str:
    if value is None or value == "":
        return fallback
    return escape(str(value))


def _fmt_ts(raw) -> str:
    if not raw:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return str(raw)
    return parsed.strftime("%Y-%m-%d %H:%M")


def _audience(item: dict) -> str:
    """Who can see it, in the UI's words (never "public" for instance-wide)."""
    parts = ["Everyone on the instance" if item.get("visibility") == "instance" else "Only you"]
    grants = item.get("grant_count") or 0
    links = item.get("live_link_count") or 0
    if grants:
        parts.append(f"{grants} {'person' if grants == 1 else 'people'}")
    if links:
        parts.append(f"{links} {'link' if links == 1 else 'links'}")
    return " + ".join(parts)


def _require_yes_for_json(yes: bool, what: str) -> None:
    from cinna.account import _require_yes_for_json as require

    require(console.json_mode, yes, what)


# ─── commands ────────────────────────────────────────────────────────────────


@click.group()
def artifacts():
    """Publish folders and files to Cinna as artifacts, and share them."""


@artifacts.command("publish")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option("--title", default=None, help="Title (default: the file or folder name; kept on republish).")
@click.option("--description", default=None, help="One-paragraph description.")
@click.option(
    "--entry",
    default=None,
    help="HTML start page inside the folder (default: index.html at the root, if any).",
)
@click.option(
    "--update",
    "update_id",
    default=None,
    help="Publish as a new revision of this artifact id (default: the one this path "
    "was published as before).",
)
@click.option("--new", "force_new", is_flag=True, help="Publish a new artifact even if this path was published before.")
@click.option("--note", default=None, help="Revision note (republish only).")
def publish(path, title, description, entry, update_id, force_new, note):
    """Publish a folder or a file and print its link.

    Private to you until you share it. Publishing the same path again adds a
    revision to the same artifact (same link); --new publishes a separate one.
    .git/, .env*, credentials/, app-data/, .claude/, .cinna/, node_modules/,
    key files (*.pem, *.key, id_rsa…, .netrc, .npmrc) and symlinks are never
    uploaded; the command lists what it left out. A path that is itself one of
    those, or inside one, is refused.
    """
    from cinna.account import _format_size

    if update_id and force_new:
        raise click.UsageError("--update and --new contradict each other; pass one.")
    source = path.resolve()
    refuse_excluded_source(source)
    files, excluded = collect_files(source)
    if not files:
        raise click.ClickException(
            f"Nothing to publish in {path}: "
            + ("every file is excluded." if excluded else "it is empty.")
        )

    with _session() as session:
        total = _check_limits(files, session.capabilities.get("limits") or {})
        console.status(
            f"Publishing {escape(str(path))} ({len(files)} "
            f"{'file' if len(files) == 1 else 'files'}, {_format_size(total)})"
        )
        if excluded:
            console.warn(
                f"Left out {len(excluded)} {'path' if len(excluded) == 1 else 'paths'} "
                "(never published): " + ", ".join(escape(e) for e in excluded[:20])
                + (f" and {len(excluded) - 20} more" if len(excluded) > 20 else "")
            )

        memory = session.memory
        target = update_id and _resolve_id(session.client, update_id)
        remembered = None if (target or force_new) else memory.lookup(source)
        if remembered:
            target = remembered

        default_title = title or source.name
        fields = {
            "title": title,
            "description": description,
            "entry_path": entry,
        }
        with tempfile.TemporaryDirectory(prefix="cinna-artifact-") as tmp:
            archive = Path(tmp) / "artifact.zip"
            write_archive(files, archive)
            result = None
            if target:
                with console.spinner("Uploading a new revision..."):
                    try:
                        result = session.client.publish_artifact(
                            archive, {**fields, "note": note}, artifact_id=target
                        )
                    except PlatformError as error:
                        if not remembered or error.status_code != 404:
                            raise
                        console.warn(
                            f"Artifact {_short(target)}, which this path was published "
                            "as, no longer exists; publishing a new one."
                        )
                        memory.forget(target)
            if result is None:
                create_fields = {
                    **fields,
                    "title": default_title[:200],
                    "source_kind": "cli",
                    "source_label": f"{session.config.machine_name} · {source.name}"[:200],
                    "user_workspace_id": session.config.user_workspace_id or None,
                }
                with console.spinner("Uploading..."):
                    result = session.client.publish_artifact(archive, create_fields)
                action = "created"
            else:
                action = "republished"

        artifact_id = str(result.get("id") or "")
        if artifact_id:
            memory.remember(source, artifact_id, result.get("title"))

    server_excluded = [e for e in (result.get("excluded") or []) if e not in excluded]
    all_excluded = excluded + server_excluded
    revision = result.get("revision_number")
    url = result.get("url")
    if console.json_mode:
        console.emit_result(
            artifact_id=artifact_id,
            action=action,
            revision=revision,
            url=url,
            title=result.get("title"),
            kind=result.get("kind"),
            visibility=result.get("visibility"),
            file_count=result.get("file_count"),
            excluded=all_excluded,
        )
        return

    if server_excluded:
        console.warn("The server also left out: " + ", ".join(escape(e) for e in server_excluded))
    if action == "created":
        console.status(
            f"Published [bold]{_text(result.get('title'))}[/bold] — "
            f"{_text(result.get('kind'))}, revision {_text(revision)}, only you can see it"
        )
    else:
        how = "remembered from the last publish of this path" if remembered else "--update"
        console.status(
            f"Republished [bold]{_text(result.get('title'))}[/bold] as revision "
            f"{_text(revision)} — same link ({how}; --new publishes a separate artifact)"
        )
    console.console.print(f"  {_text(url)}")
    console.console.print(f"  [dim]Id: {_text(artifact_id)}[/dim]")


@artifacts.command("list")
@click.option("--shared", is_flag=True, help="Artifacts other people shared with you, instead of yours.")
def list_cmd(shared):
    """Your artifacts (or, with --shared, other people's you can see)."""
    from rich.table import Table

    from cinna.account import _format_size

    scope = "shared_with_me" if shared else "mine"
    with _session() as session:
        with console.spinner("Listing artifacts..."):
            items = _list_all(session.client, scope)
    if console.json_mode:
        console.emit_result(artifacts=items, count=len(items))
        return
    if not items:
        console.status(
            "Nobody has shared an artifact with you yet."
            if shared
            else "You have no artifacts yet — `cinna artifacts publish <path>` makes one."
        )
        return
    table = Table(title="Shared with you" if shared else "Your artifacts", title_style="bold")
    table.add_column("Id", style="dim")
    table.add_column("Title", overflow="fold")
    table.add_column("Kind")
    table.add_column("Owner" if shared else "Who can see it")
    table.add_column("Rev", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Updated", style="dim")
    for item in items:
        table.add_row(
            _short(item.get("id")),
            _text(item.get("title")),
            _text(item.get("kind")),
            _text(item.get("owner_name")) if shared else _audience(item),
            _text(item.get("current_revision_number")),
            _format_size(item.get("size_bytes")),
            _fmt_ts(item.get("updated_at")),
        )
    console.console.print(table)


@artifacts.command("show")
@click.argument("artifact")
def show(artifact):
    """One artifact: its link, revisions, and (yours) who can see it."""
    from cinna.account import _format_size

    with _session() as session:
        artifact_id = _resolve_id(session.client, artifact)
        detail = session.client.get_artifact(artifact_id)
    if console.json_mode:
        console.emit_result(artifact=detail)
        return

    out = console.console.print
    out(f"[bold]{_text(detail.get('title'))}[/bold]  [dim]{_text(detail.get('id'))}[/dim]")
    out(f"  Link:     {_text(detail.get('url'))}")
    kind = _text(detail.get("kind"))
    if detail.get("entry_path"):
        kind += f" (starts at {_text(detail['entry_path'])})"
    out(f"  Kind:     {kind}")
    if detail.get("description"):
        out(f"  About:    {_text(detail['description'])}")
    if detail.get("is_owner"):
        out(f"  Visible:  {_audience(detail)}")
    else:
        out(f"  Owner:    {_text(detail.get('owner_name'))}")
    out(f"  Source:   {_text(detail.get('source_kind'))}"
        + (f" · {_text(detail['source_label'])}" if detail.get("source_label") else ""))
    revisions = [r for r in (detail.get("revisions") or []) if isinstance(r, dict)]
    if revisions:
        out("  Revisions:")
        for rev in revisions:
            note = f" — {_text(rev['note'])}" if rev.get("note") else ""
            out(
                f"    {_text(rev.get('number'))}  {_fmt_ts(rev.get('created_at'))}  "
                f"{_text(rev.get('file_count'))} files, {_format_size(rev.get('size_bytes'))}{note}"
            )
    grants = detail.get("grants")
    if isinstance(grants, list) and grants:
        out("  People:")
        for grant in grants:
            if isinstance(grant, dict):
                email = grant.get("email")
                name = grant.get("display_name")
                out(f"    {_text(name)}" + (f" <{escape(email)}>" if email and email != name else ""))
    links = detail.get("links")
    if isinstance(links, list) and links:
        out("  Links:")
        for link in links:
            if isinstance(link, dict):
                out("    " + _link_line(link))


def _link_line(link: dict) -> str:
    if link.get("revoked_at"):
        state = "revoked"
    elif link.get("blocked"):
        state = "blocked (wrong codes)"
    elif link.get("expired"):
        state = "expired"
    elif link.get("expires_at"):
        state = f"works until {_fmt_ts(link['expires_at'])}"
    else:
        state = "works, never expires"
    extras = []
    if link.get("needs_code"):
        extras.append("code")
    if link.get("pinned_revision_number"):
        extras.append(f"pinned to rev {link['pinned_revision_number']}")
    label = f" {_text(link['label'])}" if link.get("label") else ""
    extra = escape(f" [{', '.join(extras)}]") if extras else ""
    return (
        f"{_short(link.get('id'))}  {_text(link.get('token_prefix'))}…{label} — {state}{extra}"
    )


@artifacts.command("open")
@click.argument("artifact")
def open_cmd(artifact):
    """Open an artifact's page on Cinna in the browser."""
    with _session() as session:
        artifact_id = _resolve_id(session.client, artifact)
        url = session.client.get_artifact(artifact_id).get("url")
    if not url:
        raise click.ClickException("The server did not return a page for this artifact.")
    if console.json_mode:
        console.emit_result(artifact_id=artifact_id, url=url)
        return
    webbrowser.open(url)
    console.status(f"Opened {escape(url)}")


@artifacts.command("download")
@click.argument("artifact")
@click.option("--rev", type=click.IntRange(min=1), default=None, help="Revision number (default: the current one; older ones are owner-only).")
@click.option(
    "-o",
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Folder to extract into (default: artifacts/<short-id>/ under the account root).",
)
def download(artifact, rev, out_dir):
    """Download an artifact's files into a folder."""
    with _session() as session:
        artifact_id = _resolve_id(session.client, artifact)
        destination = (out_dir or (session.account_root / DOWNLOAD_DIR / _short(artifact_id)))
        destination = destination.expanduser().resolve()
        if destination.exists() and any(destination.iterdir()):
            raise click.ClickException(
                f"{destination} already exists and is not empty; pass -o with another folder."
            )
        with tempfile.TemporaryDirectory(prefix="cinna-artifact-") as tmp:
            archive = Path(tmp) / "artifact.zip"
            with console.spinner("Downloading..."):
                session.client.download_artifact(artifact_id, archive, rev=rev)
            names = extract_archive(archive, destination)
    try:
        shown = destination.relative_to(Path.cwd())
    except ValueError:
        shown = destination
    if console.json_mode:
        console.emit_result(artifact_id=artifact_id, revision=rev, path=str(destination), files=names)
        return
    console.status(f"Extracted {len(names)} {'file' if len(names) == 1 else 'files'} into {escape(str(shown))}/")


def extract_archive(archive: Path, destination: Path) -> list[str]:
    """Extract a downloaded zip, refusing absolute paths, ``..`` and symlinks."""
    root = destination.resolve()
    with zipfile.ZipFile(archive) as zf:
        # Every entry is checked before anything is written, so a refusal
        # leaves no half-extracted folder behind.
        entries = []
        for info in zf.infolist():
            name = info.filename
            if info.is_dir():
                continue
            is_link = (info.external_attr >> 16) & 0o170000 == 0o120000
            target = (root / name).resolve()
            if (
                is_link
                or name.startswith(("/", "\\"))
                or ".." in Path(name).parts
                or not target.is_relative_to(root)
            ):
                raise click.ClickException(f"Refusing an unsafe path in the download: {name}")
            entries.append((info, target))
        for info, target in entries:
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    return [info.filename for info, _ in entries]


@artifacts.command("share")
@click.argument("artifact")
@click.option("--instance", "visibility", flag_value="instance", default=None, help="Everyone signed in to this Cinna instance can see it.")
@click.option("--private", "visibility", flag_value="private", help="Only you, and the people you added.")
@click.option("--user", "add_users", multiple=True, metavar="EMAIL", help="Let this person see it (repeatable).")
@click.option("--remove-user", "remove_users", multiple=True, metavar="EMAIL", help="Stop sharing with this person (repeatable).")
def share(artifact, visibility, add_users, remove_users):
    """Change who can see an artifact. A person runs this, in a terminal.

    Refuses under --json, --no-input or without a terminal: agents publish,
    people share. Public links are `cinna artifacts link create`.
    """
    require_person("share")
    if not (visibility or add_users or remove_users):
        raise click.UsageError("Pass --instance, --private, --user EMAIL or --remove-user EMAIL.")

    with _session() as session:
        client = session.client
        artifact_id = _resolve_id(client, artifact)
        detail = client.get_artifact(artifact_id)
        _require_owner(detail, "share")

        current = {}
        for grant in detail.get("grants") or []:
            user_id = (grant.get("principal") or {}).get("user_id") if isinstance(grant, dict) else None
            if user_id:
                current[str(user_id)] = grant.get("email") or grant.get("display_name") or user_id
        wanted = dict(current)
        for email in add_users:
            user_id = _user_id_for(client, email)
            wanted[user_id] = email
        for email in remove_users:
            matches = [uid for uid, shown in wanted.items() if str(shown).casefold() == email.casefold()]
            if not matches:
                # A grant shown by name only: match it through the account id.
                with contextlib.suppress(click.ClickException):
                    uid = _user_id_for(client, email)
                    if uid in wanted:
                        matches = [uid]
            if not matches:
                console.warn(f"{escape(email)} was not on the list.")
            for uid in matches:
                wanted.pop(uid)

        if set(wanted) != set(current):
            client.replace_artifact_grants(
                artifact_id, [{"principal": {"user_id": uid}, "access": "view"} for uid in wanted]
            )
        if visibility and visibility != detail.get("visibility"):
            client.update_artifact(artifact_id, {"visibility": visibility})
        after = client.get_artifact(artifact_id)

    console.status(f"[bold]{_text(after.get('title'))}[/bold]: {_audience(after)}")
    people = [g for g in (after.get("grants") or []) if isinstance(g, dict)]
    for grant in people:
        console.console.print(f"  {_text(grant.get('email') or grant.get('display_name'))}")
    console.console.print(f"  {_text(after.get('url'))}")


def _user_id_for(client, email: str) -> str:
    """The id of the account with exactly this email (the users search is fuzzy)."""
    wanted = email.strip().casefold()
    found = client.search_users(email.strip()).get("data") or []
    for user in found:
        if isinstance(user, dict) and str(user.get("email") or "").casefold() == wanted:
            return str(user["id"])
    raise click.ClickException(f"No one with the email {email} on this Cinna instance.")


@artifacts.group("link")
def link():
    """Public links: anyone holding one can open the artifact, signed out."""


def parse_expiry(value: str) -> datetime | None:
    """``7d`` / ``12h`` / ``2w`` → an aware UTC datetime; ``never`` → None."""
    text = value.strip().lower()
    if text in ("never", "none", "0"):
        return None
    match = _DURATION_RE.match(text)
    if not match or int(match.group(1)) == 0:
        raise click.BadParameter(
            f"'{value}' is not a duration: use e.g. 12h, 7d, 2w or never.",
            param_hint="--expires",
        )
    amount, unit = int(match.group(1)), _DURATION_UNITS[match.group(2)]
    return datetime.now(timezone.utc) + timedelta(**{unit: amount})


@link.command("create")
@click.argument("artifact")
@click.option("--expires", default="7d", show_default=True, help="Lifetime: 12h, 7d, 2w … or never.")
@click.option("--code", "with_code", is_flag=True, help="Also require a 4-digit code (three wrong tries block the link).")
@click.option("--label", default=None, help="A name for the link, shown in the list (e.g. who you sent it to).")
@click.option("--rev", type=click.IntRange(min=1), default=None, help="Pin the link to this revision instead of following the latest.")
def link_create(artifact, expires, with_code, label, rev):
    """Create a public link and print it — once. A person runs this, in a terminal."""
    require_person("link create")
    expires_at = parse_expiry(expires)

    with _session() as session:
        client = session.client
        artifact_id = _resolve_id(client, artifact)
        detail = client.get_artifact(artifact_id)
        _require_owner(detail, "create a link for")
        body: dict = {"with_code": with_code}
        if label:
            body["label"] = label
        if expires_at:
            body["expires_at"] = expires_at.isoformat()
        if rev is not None:
            revisions = {r.get("number"): r.get("id") for r in detail.get("revisions") or [] if isinstance(r, dict)}
            if rev not in revisions:
                raise click.ClickException(f"\"{detail.get('title')}\" has no revision {rev}.")
            body["pinned_revision_id"] = revisions[rev]
        created = client.create_artifact_link(artifact_id, body)

    console.status(f"Public link for [bold]{_text(detail.get('title'))}[/bold] — anyone holding it can open it, signed out")
    console.console.print(f"  [bold]{_text(created.get('url'))}[/bold]")
    if created.get("code"):
        console.console.print(f"  Code: [bold]{escape(str(created['code']))}[/bold] (send it separately from the link)")
    console.console.print(
        f"  {'Expires ' + _fmt_ts(created.get('expires_at')) if created.get('expires_at') else 'Never expires'}"
        + (f" · pinned to revision {rev}" if rev else "")
    )
    console.console.print(
        "  [yellow]Shown once — copy it now.[/yellow] "
        f"Revoke with: cinna artifacts link revoke {_short(artifact_id)} {_short(created.get('id'))}"
    )


@link.command("revoke")
@click.argument("artifact")
@click.argument("link_ref", metavar="LINK")
@click.option("--yes", is_flag=True, help="Skip the confirmation (required with --json).")
def link_revoke(artifact, link_ref, yes):
    """Revoke a public link (its id or token prefix, from `cinna artifacts show`)."""
    _require_yes_for_json(yes, "link revoke")
    with _session() as session:
        client = session.client
        artifact_id = _resolve_id(client, artifact)
        detail = client.get_artifact(artifact_id)
        _require_owner(detail, "revoke a link of")
        ref = link_ref.strip().rstrip("…").lower()
        live = [k for k in detail.get("links") or [] if isinstance(k, dict) and not k.get("revoked_at")]
        matches = [
            k for k in live
            if str(k.get("id", "")).startswith(ref) or str(k.get("token_prefix", "")).lower().startswith(ref)
        ] if ref else []
        if not matches:
            raise click.ClickException(
                f"No live link '{link_ref}' on \"{detail.get('title')}\" "
                f"(see `cinna artifacts show {_short(artifact_id)}`)."
            )
        if len(matches) > 1:
            raise click.ClickException(f"Link '{link_ref}' is ambiguous; use more of its id.")
        target = matches[0]
        if not yes and not console.confirm(
            f"Revoke link {target.get('token_prefix')}… on \"{detail.get('title')}\"? "
            "Anyone holding it loses access at once",
            default=False,
        ):
            raise click.Abort()
        client.revoke_artifact_link(artifact_id, str(target["id"]))
    if console.json_mode:
        console.emit_result(artifact_id=artifact_id, link_id=str(target["id"]), revoked=True)
        return
    console.status(f"Revoked link {_text(target.get('token_prefix'))}…")


@artifacts.command("delete")
@click.argument("artifact")
@click.option("--yes", is_flag=True, help="Skip the confirmation (required with --json).")
def delete(artifact, yes):
    """Delete an artifact, every revision and every link — at once, for good."""
    _require_yes_for_json(yes, "delete")
    with _session() as session:
        client = session.client
        artifact_id = _resolve_id(client, artifact)
        detail = client.get_artifact(artifact_id)
        _require_owner(detail, "delete")
        if not yes and not console.confirm(
            f"Delete \"{detail.get('title')}\" ({_audience(detail)})? "
            "Its page and every link stop working; this cannot be undone",
            default=False,
        ):
            raise click.Abort()
        client.delete_artifact(artifact_id)
        session.memory.forget(artifact_id)
    if console.json_mode:
        console.emit_result(artifact_id=artifact_id, deleted=True)
        return
    console.status(f"Deleted [bold]{_text(detail.get('title'))}[/bold]")
