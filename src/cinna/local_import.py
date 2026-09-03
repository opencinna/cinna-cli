"""``cinna agent import`` — bring a locally built agent into the cloud.

The Local Agent Kit lets a user build an agent on their own machine with any
coding assistant and *no* Cinna account: the folder layout mirrors a cloud agent
workspace and a ``cinna-agent.json`` manifest carries the definitional metadata
(description, example prompts, router trigger, prompt files, credential specs,
schedules, status command).

This module is the go-cloud step of that story. It runs from an account
workspace and replays the manifest against the account-scoped API using the
existing verbs — nothing platform-side is new:

``create agent`` → ``bulk prompt/metadata write`` → ``agent sync`` → ``copy
files`` → ``sync push`` → ``credential drafts`` → ``schedules`` → ``stamp``.

Every step is idempotent (agent by ``cloud.agent_id``, credentials by name,
schedules by name), so a partial import is resumed with ``--update`` instead of
duplicating anything.

Two hard rules the implementation never bends:

* ``credentials/`` and ``app-data/`` are **never** copied to the cloud — secrets
  stay on the user's machine and runtime state is not import material. The
  exclude list from the contract (``.cinna-kit/layout.json`` — see
  :mod:`cinna.kit_contract`) can widen the exclusions but can never remove
  those two.
* No secret value is ever read or printed. Credentials are created as empty
  drafts and the user fills them in the browser through the printed setup URL.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import click

from cinna import console
from cinna import kit_contract
from cinna import sync_session
from cinna.account import (
    _resolve_account_workspace,
    find_account_root,
    load_account_config,
    resolve_child_workspace,
    run_agent_sync,
)
from cinna.bootstrap import normalize_agent_dir_name
from cinna.client import AccountClient
from cinna.config import workspace_dir

MANIFEST_FILENAME = "cinna-agent.json"
REQUIREMENTS_FILENAME = "workspace_requirements.txt"

#: Highest ``schema_version`` this CLI understands. A newer manifest is refused
#: rather than half-read — the user upgrades cinna-cli instead.
SUPPORTED_SCHEMA_VERSION = 1

#: The contract's ``cloud_import_excludes``, as this build ships it. Used only
#: when ``.cinna-kit/layout.json`` cannot be found or cannot be read — and that
#: is a *degradation*, not a mode, because nothing cinna-core publishes reaches
#: this tool through it. Lives in :mod:`cinna.kit_contract` so the list and the
#: matcher that gives it meaning stay together.
DEFAULT_EXCLUDE = kit_contract.DEFAULT_EXCLUDE

#: Never copied, whatever the contract says. ``credentials/`` holds the local
#: ``.env``; ``app-data/`` is runtime state owned by the cloud environment.
MANDATORY_EXCLUDE = ("credentials/", "app-data/")

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")

TOTAL_STEPS = 9

_PROMPT_FIELDS = {
    "workflow": "workflow_prompt",
    "entrypoint": "entrypoint_prompt",
    "refiner": "refiner_prompt",
}

_SCHEDULE_TYPE_ALIASES = {
    "script": "script_trigger",
    "script_trigger": "script_trigger",
    "static_prompt": "static_prompt",
}



def _line(message: str) -> None:
    """Print one data-bearing line verbatim.

    Manifest values (credential types, names, paths) routinely contain square
    brackets — ``[api_token]`` — which Rich would eat as markup. Everything that
    embeds user/manifest data goes through here; only the few purely decorative
    lines use Rich markup directly.
    """
    console.console.print(message, markup=False, highlight=False)


# ── Manifest ────────────────────────────────────────────────────────────────


def load_manifest(source: Path) -> dict:
    """Read and validate ``<source>/cinna-agent.json``.

    Validates the same pragmatic subset ``kit.py validate`` checks (required
    keys, types, slug shape, schedule/credential shape). Unknown keys are kept
    untouched so a newer kit round-trips through an older CLI.
    """
    path = source / MANIFEST_FILENAME
    if not path.is_file():
        raise click.ClickException(
            f"No {MANIFEST_FILENAME} in {source}.\n"
            f"'{source}' does not look like a Local Agent Kit agent — pass the "
            f"agent folder (the one holding {MANIFEST_FILENAME}), or scaffold "
            f"one with 'python3 .cinna-kit/tools/kit.py new <slug>'."
        )
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"{path} is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise click.ClickException(f"{path} must contain a JSON object.")

    schema_version = data.get("schema_version", 1)
    if not isinstance(schema_version, int):
        raise click.ClickException(
            f"{MANIFEST_FILENAME}: schema_version must be an integer."
        )
    if schema_version > SUPPORTED_SCHEMA_VERSION:
        raise click.ClickException(
            f"{MANIFEST_FILENAME} declares schema_version {schema_version}, but "
            f"this cinna-cli understands up to {SUPPORTED_SCHEMA_VERSION}.\n"
            f"Upgrade with 'uv tool upgrade cinna-cli' and retry."
        )

    slug = data.get("slug")
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise click.ClickException(
            f"{MANIFEST_FILENAME}: 'slug' must match ^[a-z0-9][a-z0-9-]{{1,62}}$ "
            f"(got {slug!r})."
        )
    if slug != source.name:
        raise click.ClickException(
            f"{MANIFEST_FILENAME}: slug '{slug}' does not match the folder name "
            f"'{source.name}'. Rename one of them so they agree."
        )

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise click.ClickException(f"{MANIFEST_FILENAME}: 'name' is required.")
    if len(name) > 255:
        raise click.ClickException(
            f"{MANIFEST_FILENAME}: 'name' is longer than 255 characters."
        )

    description = data.get("description")
    if description is not None and not isinstance(description, str):
        raise click.ClickException(
            f"{MANIFEST_FILENAME}: 'description' must be a string."
        )

    prompts = data.get("prompts") or {}
    if not isinstance(prompts, dict):
        raise click.ClickException(f"{MANIFEST_FILENAME}: 'prompts' must be an object.")

    examples = data.get("example_prompts") or []
    if not isinstance(examples, list) or any(not isinstance(e, str) for e in examples):
        raise click.ClickException(
            f"{MANIFEST_FILENAME}: 'example_prompts' must be a list of strings."
        )

    for cred in _manifest_list(data, "credentials"):
        if not isinstance(cred.get("name"), str) or not cred["name"].strip():
            raise click.ClickException(
                f"{MANIFEST_FILENAME}: every credential needs a 'name'."
            )
        if not isinstance(cred.get("type"), str) or not cred["type"].strip():
            raise click.ClickException(
                f"{MANIFEST_FILENAME}: credential '{cred.get('name')}' needs a 'type'."
            )

    for sched in _manifest_list(data, "schedules"):
        if not isinstance(sched.get("name"), str) or not sched["name"].strip():
            raise click.ClickException(
                f"{MANIFEST_FILENAME}: every schedule needs a 'name'."
            )
        cron = sched.get("cron_string")
        if not isinstance(cron, str) or len(cron.split()) != 5:
            raise click.ClickException(
                f"{MANIFEST_FILENAME}: schedule '{sched['name']}' needs a 5-field "
                f"'cron_string' (got {cron!r})."
            )
        stype = _schedule_type(sched)
        if stype == "script_trigger" and not (sched.get("command") or "").strip():
            raise click.ClickException(
                f"{MANIFEST_FILENAME}: schedule '{sched['name']}' is a script "
                f"schedule and needs a 'command'."
            )

    cloud = data.get("cloud")
    if cloud is not None and not isinstance(cloud, dict):
        raise click.ClickException(f"{MANIFEST_FILENAME}: 'cloud' must be an object.")

    return data


def _manifest_list(data: dict, key: str) -> list[dict]:
    """Return ``data[key]`` as a list of objects (refusing anything else)."""
    value = data.get(key) or []
    if not isinstance(value, list) or any(not isinstance(i, dict) for i in value):
        raise click.ClickException(
            f"{MANIFEST_FILENAME}: '{key}' must be a list of objects."
        )
    return value


def _schedule_type(sched: dict) -> str:
    raw = (sched.get("schedule_type") or "static_prompt").strip()
    resolved = _SCHEDULE_TYPE_ALIASES.get(raw)
    if resolved is None:
        raise click.ClickException(
            f"{MANIFEST_FILENAME}: schedule '{sched.get('name')}' has unknown "
            f"schedule_type '{raw}' (expected static_prompt or script)."
        )
    return resolved


# ── The contract: exclude list, secret gate, copy plan ───────────────────────


#: Re-exported so the contract reader has one home but the import module's own
#: callers (and its tests) keep the names they had.
find_kit_root = kit_contract.find_kit_root


def load_export_contract(source: Path) -> kit_contract.ExportContract:
    """The exclude patterns and secret rules this run will apply.

    Sourced from ``.cinna-kit/layout.json`` — the contract's data file — found by
    walking up from the agent folder. Decision D6 deleted ``cloud_import.exclude``
    from ``kit.json``, and reading the old key was not an error: the guard was an
    ``isinstance(..., list)`` check, so a missing key silently kept the built-in
    list and printed a line that announced a mode. Nothing cinna-core published
    about excludes or secret files could reach this tool through that path.

    ``MANDATORY_EXCLUDE`` is appended last and unconditionally.
    """
    return kit_contract.resolve_export_contract(source, MANDATORY_EXCLUDE)


def is_excluded(rel_posix: str, patterns: list[str]) -> bool:
    """True when any exclude pattern drops this agent-root-relative path.

    The contract's matching semantics, not ``fnmatch``'s — see
    ``kit_contract.matches_pattern``. The difference is not cosmetic: under
    ``fnmatch`` the contract's ``README.md`` entry would also drop
    ``docs/README.md`` and ``scripts/README.md``, which ``layout.json`` names as
    the exact outcome anchoring exists to prevent, while its ``**/`` entries
    would match nothing at all.
    """
    return kit_contract.is_excluded(rel_posix, patterns)


def plan_copy(
    source: Path, patterns: list[str], secret_rules: list | None = None
) -> list[str]:
    """The relative POSIX paths that would be copied, in the contract's sort order.

    Symlinks are never followed and never listed: an agent tree is text, and
    following a link out of the folder is exactly the kind of surprise an import
    must not have. Excluded directories are never descended into.

    The contract's ``secret_files`` rules are applied **here**, in the one walk,
    rather than only at copy time — a path withheld from the upload but counted
    in a ``content_hash`` makes that hash move for a change that can never be
    published, which presents to the user as unpublished changes forever.

    A path the walk could not read is refused rather than guessed at: an
    unscannable directory silently read as empty produces a complete-looking
    export with a subtree missing from it.
    """
    if secret_rules is None:
        secret_rules = [dict(rule) for rule in kit_contract.DEFAULT_SECRET_FILE_RULES]
    files, unreadable = kit_contract.collect_export_tree(source, patterns, secret_rules)
    if unreadable:
        listing = "\n".join(f"    - {rel}" for rel in unreadable)
        raise click.ClickException(
            "These paths are inside the import but could not be read, so the set "
            "of files that would travel describes a tree that was never fully "
            f"seen:\n{listing}\nFix their permissions and retry."
        )
    return files


def ledger_workspace(
    account_root: Path, contract: kit_contract.ExportContract
) -> str | None:
    """The account workspace this run pushed from, relative to the workshop root.

    ``publications.schema.json`` spells the field as "Account workspace the CLI
    pushed from, relative to the workshop root, e.g. ``Cloud/acme.opencinna.io``"
    — the account workspace, not the per-agent child under it.

    The workshop root is the folder holding ``.cinna-kit/``, so this can only be
    answered when a kit was found and the account workspace sits inside it.
    Otherwise the field is **omitted**: the contract already tolerates an absent
    ``workspace`` (the desktop publishes through the account API and has none),
    and an absolute local path is worse than nothing in a file that is the
    user's own record.
    """
    if contract.layout_path is None:
        return None
    workshop_root = contract.layout_path.parent.parent
    try:
        return account_root.resolve().relative_to(workshop_root.resolve()).as_posix()
    except ValueError:
        return None


def assert_contract_compatible(manifest: dict) -> str:
    """Gate the import on the folder's ``contract_version``, and say what it decided.

    Only the MAJOR version decides. A minor or patch difference is not a
    compatibility question — the contract's minor releases are additive by
    definition — and that shape constrains what a future contract change may
    safely do: **a same-major pair passes**, so a minor-version change reaches a
    non-adopting reader as silence rather than as a warning, and anything that
    must be noticed by an old reader needs a major bump.

    A folder from a newer major is **refused**: this build cannot know what it
    would be dropping. A folder from an older major, or one with no usable
    ``contract_version`` at all, is imported with a loud warning — every folder
    created before contract 1.0.0 is in that state, and refusing them would
    break the users this command exists for.
    """
    declared = manifest.get("contract_version")
    status, reason = kit_contract.check_contract_compatibility(
        declared, kit_contract.SUPPORTED_CONTRACT_VERSION
    )
    if status == "app_too_old":
        raise click.ClickException(f"{MANIFEST_FILENAME}: {reason}")
    if status != "ok":
        console.warn(f"{MANIFEST_FILENAME}: {reason}")
    return reason


def export_content_hash(
    source: Path, files: list[str], contract: kit_contract.ExportContract
) -> str | None:
    """The exported tree's ``content_hash``, or ``None`` when it must be withheld.

    Two ways it is withheld, and both are the same rule: **unevaluable ⇒ refuse
    to emit a ``content_hash`` at all, never emit one computed a different way.**

    * the contract's own exclude list could not be evaluated, so the file set
      the hash would cover is not the set another host would select — and "but
      the fallback is the same list" is a claim about *this* build's contract,
      not about the one sitting in the folder, which is the only one the other
      host is reading;
    * a file that travels could not be read, so the digest would describe bytes
      that were never seen.

    A missing drift number is visible and recoverable; a plausible wrong one
    reads "up to date" forever and no publish can clear it.
    """
    if not contract.hashable:
        console.warn(
            "content_hash withheld — the contract's own `cloud_import_excludes` "
            "could not be evaluated, so the file set a hash would cover is not "
            "the one another host would select."
        )
        return None
    try:
        return kit_contract.content_hash(source, files)
    except kit_contract.UnhashableTree as exc:
        console.warn(f"content_hash withheld — {exc}")
        return None


def assert_no_secrets(files: list[str], secret_rules: list | None = None) -> None:
    """Defense in depth: refuse a copy plan that reaches secret material.

    Two gates, and the second one is the contract's rule rather than a local
    guess. The hand-rolled ``rel.endswith(".env") or basename == ".env"`` pair it
    replaces was a working equivalent of the declared ``basename_equals`` and
    ``basename_suffix`` clauses and missed the third: ``basename_prefix``. So
    ``.env.prod`` and ``.env.local`` cleared both this gate and every glob in the
    exclude list, at the agent root and at any depth, and travelled.

    ``.env.example`` travelling is the *correct* behaviour, which is why this
    reads the declared rule rather than adding a ``startswith(".env.")`` — the
    rule's ``unless`` block is what keeps the example file publishable, and a
    single prefix test without it would take the example with the secret.
    """
    if secret_rules is None:
        secret_rules = [dict(rule) for rule in kit_contract.DEFAULT_SECRET_FILE_RULES]
    for rel in files:
        first = rel.split("/")[0]
        if first in ("credentials", "app-data"):
            raise click.ClickException(
                f"Refusing to import: '{rel}' is under {first}/, which is never "
                f"copied to the cloud. This is a bug in the exclude list — "
                f"report it rather than working around it."
            )
        if kit_contract.is_secret_filename(Path(rel).name, secret_rules):
            raise click.ClickException(
                f"Refusing to import: '{rel}' looks like an environment file. "
                f"Secrets stay on your machine — move it under credentials/ and "
                f"retry."
            )


# ── workspace_requirements.txt ──────────────────────────────────────────────


_DEPENDENCIES_BLOCK_RE = re.compile(r"^dependencies\s*=\s*\[(.*?)\]", re.MULTILINE | re.DOTALL)
_QUOTED_RE = re.compile(r"[\"']([^\"']+)[\"']")


def pyproject_dependencies(source: Path) -> list[str]:
    """Read ``[project].dependencies`` from ``pyproject.toml``.

    Uses ``tomllib`` (3.11+) and falls back to a conservative regex on 3.10,
    where the stdlib has no TOML parser and the CLI still has to run.
    """
    path = source / "pyproject.toml"
    if not path.is_file():
        return []
    text = path.read_text()
    try:
        import tomllib
    except ModuleNotFoundError:
        match = _DEPENDENCIES_BLOCK_RE.search(text)
        if match is None:
            return []
        return _QUOTED_RE.findall(match.group(1))
    try:
        data = tomllib.loads(text)
    except Exception as exc:
        console.warn(f"Could not parse {path} ({exc}); skipping requirements.")
        return []
    deps = (data.get("project") or {}).get("dependencies") or []
    return [d for d in deps if isinstance(d, str)]


def render_requirements(source: Path) -> str | None:
    """Return the ``workspace_requirements.txt`` body, or None when there is nothing."""
    deps = pyproject_dependencies(source)
    if not deps:
        return None
    header = "# Generated by 'cinna agent import' from pyproject.toml [project.dependencies]\n"
    return header + "\n".join(deps) + "\n"


# ── Copy ────────────────────────────────────────────────────────────────────


def copy_tree(source: Path, dest: Path, files: list[str]) -> None:
    """Copy the planned files into ``dest`` (overwriting, never deleting)."""
    for rel in files:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, target)


# ── Manifest write + the publication ledger ──────────────────────────────────


def write_manifest(agent_dir: Path, manifest: dict) -> tuple[dict, bool]:
    """Write ``cinna-agent.json``, migrating the legacy ledger keys on the way out.

    Migration happens **here — at every write of the manifest — and nowhere
    else.** In particular it never happens at export: an export that mutates the
    manifest breaks byte-parity with a host that uploads the folder verbatim,
    and is the defect class the sibling-file ruling removed. A legacy folder
    exported without ever being re-stamped therefore carries its stale ``cloud``
    block to the cloud. That is accepted — it holds no secret and the platform
    ignores it — and the remedy is the re-stamp this function performs.

    Migrating moves the folder's ``content_hash`` exactly **once**, which is
    correct: the folder genuinely changed, and one visible, explainable move is
    the opposite of the perpetual drift that storing the hash inside the hashed
    file produced.

    Two keys move into ``publications.json``, and migration is **all-or-nothing
    per key**. A key this build cannot READ is left in place, because discarding
    data a host cannot interpret is unrecoverable while a deprecated key is
    merely visible. A key it cannot PLACE is left in place for the same reason,
    and that is the harder half: deleting a ``cloud`` block whose ``agent_id``
    the ledger declined to record would make that id simply cease to exist. A
    value's *destination* has to be reached before the source may be removed.

    Returns ``(manifest as written, whether the bytes changed)``. The manifest
    is a member of the tree a ``content_hash`` covers, so a caller that hashes
    has to know whether this write moved it — and a write that would produce
    identical bytes is skipped rather than made.
    """
    migrated = read_publications_or_refuse(agent_dir)

    # A migration may only ADD to a ledger this build would have written itself.
    # Appending to one holding an entry our own reader rejects, then rewriting,
    # would make this tool the author of a file it calls broken.
    ledger_writable = all(
        kit_contract.ledger_entry_is_placeable(entry) for entry in migrated
    )
    known = {
        entry.get("platform_url")
        for entry in migrated
        if isinstance(entry.get("platform_url"), str)
    }
    ledger_changed = False

    # The caller's dict is left alone.
    document = dict(manifest)

    def migrate(key: str, entries: list) -> None:
        nonlocal ledger_changed
        if not ledger_writable:
            return
        if not all(kit_contract.ledger_entry_is_placeable(e) for e in entries):
            return
        del document[key]
        for entry in entries:
            platform_url = entry["platform_url"]  # placeable ⇒ a non-blank str
            # The file is the newer authority: a migration adds instances, it
            # never overwrites one.
            if platform_url in known:
                continue
            migrated.append(entry)
            known.add(platform_url)
            ledger_changed = True

    # Order matters: `publications[]` is the newer shape, so a `cloud` stamp
    # naming the same instance loses to it.
    stale = document.get("publications")
    if isinstance(stale, list):
        migrate("publications", stale)

    cloud = document.get("cloud")
    if isinstance(cloud, dict):
        migrate("cloud", [cloud])

    path = agent_dir / MANIFEST_FILENAME
    serialised = kit_contract.serialise(document)
    changed = path.read_text() != serialised
    # Not rewritten when the bytes would be identical. The manifest is a member
    # of the hashed tree, so a pointless rewrite is a pointless hash move.
    if changed:
        path.write_text(serialised, encoding="utf-8")
    # Written only when something was actually absorbed, so a write that
    # migrates nothing never reformats a ledger somebody hand-edited.
    if ledger_changed:
        kit_contract.write_publications(agent_dir, migrated)
    return document, changed


def read_publications_or_refuse(agent_dir: Path) -> list[dict]:
    """The ledger, or a loud refusal — never a silent "it was empty".

    A ledger this build cannot parse is never overwritten with what it managed
    to migrate, and the manifest is not written either: a half-migration that
    reported success is the failure shape the all-or-nothing rule exists to
    remove.
    """
    entries = kit_contract.read_publications(agent_dir)
    if entries is None:
        raise click.ClickException(
            f"{agent_dir / kit_contract.PUBLICATIONS_FILENAME} could not be read "
            f"as a publication ledger, so nothing was written — recording this "
            f"import into it would overwrite a file this tool does not "
            f"understand. Fix the file and retry."
        )
    return entries


def record_publication(
    agent_dir: Path,
    *,
    platform_url: str,
    agent_id: str,
    workspace: str | None,
    contract_version: str | None,
    content_hash: str | None,
) -> None:
    """Record this import in ``publications.json``.

    One entry per Cinna instance, resolved by ``platform_url``. Re-importing to
    the same instance updates that entry in place; importing to a second
    instance appends one. The manifest is not touched here — it was settled
    before the tree was copied, so that what is pushed, what is hashed and what
    sits on disk are one and the same.

    ``content_hash`` is omitted when it could not be computed — and **removed
    from an existing entry** in that case rather than left standing. A stale
    hash reads "up to date" forever, which is exactly the outcome the refusal
    exists to prevent; a missing one is visible and recoverable.
    """
    entries = read_publications_or_refuse(agent_dir)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry = kit_contract.find_publication(entries, platform_url)
    if entry is None:
        entry = {"platform_url": platform_url, "imported_at": now}
        entries.append(entry)
    entry["agent_id"] = agent_id
    entry["workspace"] = workspace
    entry["updated_at"] = now
    if contract_version is not None:
        entry["contract_version"] = contract_version
    if content_hash is not None:
        entry["content_hash"] = content_hash
    else:
        entry.pop("content_hash", None)

    kit_contract.write_publications(agent_dir, entries)


def resolve_known_agent_id(
    agent_dir: Path, manifest: dict, platform_url: str
) -> tuple[str | None, str | None]:
    """``(agent_id, source)`` for the instance this run targets.

    ``publications.json`` first, resolved by ``platform_url``; the deprecated
    ``cloud`` block second. The legacy read is deliberately kept rather than
    tidied: a folder imported by an older cinna-cli has only the ``cloud``
    stamp, and dropping the read would make its next ``--update`` create a
    *second* agent instead of updating the existing one. It is retired *behind*
    the ledger, not removed.

    The ``cloud`` block carries no ``platform_url`` filter of its own beyond the
    one it recorded, so it answers only for the instance it names — or, for the
    blocks older builds wrote without one, for whichever instance asks.
    """
    entries = read_publications_or_refuse(agent_dir)
    entry = kit_contract.find_publication(entries, platform_url)
    if entry is not None and isinstance(entry.get("agent_id"), str):
        return entry["agent_id"], kit_contract.PUBLICATIONS_FILENAME

    cloud = manifest.get("cloud")
    if isinstance(cloud, dict) and isinstance(cloud.get("agent_id"), str):
        stamped_url = cloud.get("platform_url")
        if stamped_url in (None, platform_url):
            return cloud["agent_id"], f"{MANIFEST_FILENAME} 'cloud' (deprecated)"
    return None, None


# ── The command ─────────────────────────────────────────────────────────────


def run_agent_import(
    path: str,
    name: str | None = None,
    workspace: str | None = None,
    update: bool = False,
    dry_run: bool = False,
    no_push: bool = False,
    yes: bool = False,
) -> None:
    """Import a locally built agent — ``cinna agent import <path>``."""
    source = Path(path).expanduser().resolve()
    if not source.is_dir():
        raise click.ClickException(f"'{path}' is not a directory.")

    account_root = find_account_root()
    account_cfg = load_account_config(account_root)

    # [1/9] Manifest + copy plan (pure local work — also the whole of --dry-run)
    console.step(1, TOTAL_STEPS, f"Reading {MANIFEST_FILENAME}...")
    manifest = load_manifest(source)
    contract_reason = assert_contract_compatible(manifest)
    slug = manifest["slug"]
    agent_name = name or manifest["name"]
    description = manifest.get("description")
    credentials = _manifest_list(manifest, "credentials")
    schedules = _manifest_list(manifest, "schedules")
    contract = load_export_contract(source)
    known_agent_id, known_from = resolve_known_agent_id(
        source, manifest, account_cfg.platform_url
    )

    files = plan_copy(source, contract.patterns, contract.secret_rules)
    assert_no_secrets(files, contract.secret_rules)
    digest = export_content_hash(source, files, contract)
    needs_requirements = REQUIREMENTS_FILENAME not in files
    requirements = render_requirements(source) if needs_requirements else None

    _line(f"  Agent:       {agent_name} ({slug})")
    _line(f"  Source:      {source}")
    _line(f"  Contract:    {contract_reason}")
    _line(f"  Exclusions:  {contract.origin}")
    _line(
        f"  Hash:        {digest}"
        if digest
        else "  Hash:        WITHHELD — this tree cannot be hashed in a way "
        "another host would agree with (see the warning above)"
    )
    _line(
        f"  Files:       {len(files)} to copy "
        f"(credentials/ and app-data/ never leave your machine)"
    )
    if requirements:
        _line(
            f"  Requirements: generating {REQUIREMENTS_FILENAME} from pyproject.toml"
        )
    _line(f"  Credentials: {len(credentials)} draft(s)")
    _line(f"  Schedules:   {len(schedules)}")

    if dry_run:
        console.console.print()
        console.console.print("[bold]Dry run — nothing was sent to the platform.[/bold]")
        if known_agent_id:
            _line(
                f"  Would update agent {known_agent_id} (--update), "
                f"resolved from {known_from}."
            )
        else:
            _line(f"  Would create agent '{agent_name}'.")
        for rel in files:
            _line(f"    + {rel}")
        for cred in credentials:
            _line(
                f"    credential draft: {cred['name']} [{cred['type']}]"
            )
        for sched in schedules:
            _line(
                f"    schedule: {sched['name']} ({sched['cron_string']})"
            )
        return

    if known_agent_id and not update:
        raise click.ClickException(
            f"'{slug}' was already imported as agent {known_agent_id} "
            f"({account_cfg.platform_url}, recorded in {known_from}).\n"
            f"Re-run with --update to push the local changes into that agent."
        )

    if not yes:
        click.confirm(
            f"Import '{agent_name}' into {account_cfg.platform_url}?",
            abort=True,
        )

    # Settle the manifest BEFORE the tree is copied, hashed or pushed, so that
    # all three describe the same bytes. Migration belongs at a manifest write
    # and never at export time; this command is a write path, and doing the
    # write first is what keeps it from being an export that mutates what it is
    # exporting. A folder whose manifest was not canonically serialised moves
    # its content_hash exactly once, here — visibly, and before the number that
    # would be recorded is computed.
    manifest, manifest_changed = write_manifest(source, manifest)
    if manifest_changed:
        # The manifest is in the hashed set, so this move is real and is the
        # one the ledger must record — say both, rather than leave the summary's
        # number standing as the one that was published.
        digest = export_content_hash(source, files, contract)
        _line(
            f"  Normalized {MANIFEST_FILENAME} (contract serialisation) — "
            f"content hash is now {digest or 'WITHHELD'}."
        )
    if "cloud" in manifest:
        console.warn(
            f"The deprecated 'cloud' block was left in {MANIFEST_FILENAME}: it has "
            f"no usable platform_url/agent_id pair to move into "
            f"{kit_contract.PUBLICATIONS_FILENAME}. Complete or remove it by hand."
        )

    with AccountClient(account_cfg) as client:
        user_workspace_id = _resolve_target_workspace(client, account_cfg, workspace)

        # [2/9] Resolve or create the cloud agent
        console.step(2, TOTAL_STEPS, "Resolving the cloud agent...")
        agent = _resolve_or_create_agent(
            client,
            agent_name=agent_name,
            description=description,
            known_agent_id=known_agent_id,
            update=update,
            user_workspace_id=user_workspace_id,
        )
        agent_id = agent["id"]

        # [3/9] Metadata + prompts (one bulk write, then the status command)
        console.step(3, TOTAL_STEPS, "Writing prompts and metadata...")
        _write_prompts_and_metadata(client, agent_id, source, manifest)

    # [4/9] Sync a local workspace for the agent (mints a child token)
    console.step(4, TOTAL_STEPS, "Attaching a local workspace...")
    child = resolve_child_workspace(account_root, agent_id)
    if child is None:
        run_agent_sync(agent_id, None)
        child = resolve_child_workspace(account_root, agent_id)
    else:
        _line(
            f"  Already synced under {child[0].relative_to(account_root)}/ — reusing it."
        )
    if child is None:
        raise click.ClickException(
            "The agent was synced but its workspace could not be located under "
            f"{account_root / 'agents'}/. Run 'cinna agent sync {slug}' manually "
            f"and re-run with --update."
        )
    child_root, child_config = child
    dest = workspace_dir(child_root)
    dest.mkdir(parents=True, exist_ok=True)

    # [5/9] Copy the tree
    console.step(5, TOTAL_STEPS, f"Copying {len(files)} file(s) into the workspace...")
    copy_tree(source, dest, files)
    if requirements:
        (dest / REQUIREMENTS_FILENAME).write_text(requirements)
        _line(f"  Generated {REQUIREMENTS_FILENAME}.")

    # [6/9] Push
    pushed = False
    if no_push:
        console.step(6, TOTAL_STEPS, "Skipping sync push (--no-push).")
    else:
        console.step(6, TOTAL_STEPS, "Pushing the workspace to the agent...")
        pushed = _push_workspace(child_config, child_root)

    with AccountClient(account_cfg) as client:
        # [7/9] Credential drafts
        console.step(7, TOTAL_STEPS, f"Creating {len(credentials)} credential draft(s)...")
        setup_urls = _sync_credentials(
            client, agent_id, agent_name, credentials, user_workspace_id
        )

        # [8/9] Schedules
        console.step(8, TOTAL_STEPS, f"Creating {len(schedules)} schedule(s)...")
        _sync_schedules(client, agent_id, schedules, update)

    # [9/9] Record the publication — only once the files are actually up there
    console.step(9, TOTAL_STEPS, "Recording the cloud link...")
    if pushed:
        record_publication(
            source,
            platform_url=account_cfg.platform_url,
            agent_id=agent_id,
            workspace=ledger_workspace(account_root, contract),
            contract_version=manifest.get("contract_version") or contract.kit_version,
            content_hash=digest,
        )
        # Only the ledger is written here. `publications.json` is in
        # `cloud_import_excludes`, so recording a publication cannot move the
        # `content_hash` that was just recorded.
        _line(
            f"  {kit_contract.PUBLICATIONS_FILENAME}: "
            f"{account_cfg.platform_url} → {agent_id}"
        )
    else:
        console.warn(
            "Workspace not pushed — the publication was NOT recorded. "
            f"Push with 'cinna sync push --agent {slug}', then re-run this import "
            "with --update."
        )

    _print_summary(
        account_cfg,
        agent_id=agent_id,
        agent_name=agent_name,
        slug=slug,
        child_root=child_root.relative_to(account_root),
        setup_urls=setup_urls,
        examples=manifest.get("example_prompts") or [],
    )


# ── Steps ───────────────────────────────────────────────────────────────────


def _resolve_target_workspace(
    client: AccountClient, account_cfg, workspace: str | None
) -> str | None:
    """Return the user-workspace id new objects land in (``None`` = Default)."""
    if workspace is None:
        return account_cfg.user_workspace_id
    if workspace.strip().lower() in ("default", "none", "clear", ""):
        return None
    listing = client.list_user_workspaces()
    return _resolve_account_workspace(listing.get("data", []), workspace).get("id")


def _resolve_or_create_agent(
    client: AccountClient,
    agent_name: str,
    description: str | None,
    known_agent_id: str | None,
    update: bool,
    user_workspace_id: str | None,
) -> dict:
    """Return the cloud agent record, creating it when this is a first import.

    Resolution is by id whenever the manifest carries one — a duplicate display
    name on the platform must never make the import write into the wrong agent.
    """
    if known_agent_id:
        items = client.list_account_agents().get("data", [])
        match = [a for a in items if a.get("id") == known_agent_id]
        if not match:
            raise click.ClickException(
                f"The manifest points at agent {known_agent_id}, which is not in "
                f"your accessible agents on this platform.\n"
                f"Clear the 'cloud' block in {MANIFEST_FILENAME} to import it as "
                f"a new agent, or log in to the platform that owns it."
            )
        _line(f"  Updating existing agent {match[0].get('name')}.")
        return match[0]

    if update:
        # Resume of an import that died before the cloud block was stamped:
        # a unique name match is the only safe reattachment point.
        items = client.list_account_agents().get("data", [])
        ref_slug = normalize_agent_dir_name(agent_name)
        matches = [
            a
            for a in items
            if a.get("name") == agent_name
            or normalize_agent_dir_name(a.get("name", "")) == ref_slug
        ]
        if len(matches) == 1:
            _line(
                f"  Reusing existing agent {matches[0].get('name')} "
                f"({matches[0].get('id')})."
            )
            return matches[0]
        if len(matches) > 1:
            rows = ", ".join(f"{a['name']} ({a['id']})" for a in matches)
            raise click.ClickException(
                f"--update: several agents match '{agent_name}': {rows}.\n"
                f"Set cloud.agent_id in {MANIFEST_FILENAME} to the one you mean."
            )

    agent = client.create_agent(
        agent_name, description, user_workspace_id=user_workspace_id
    )
    _line(f"  Created agent {agent.get('name')} ({agent.get('id')}).")
    return agent


def _write_prompts_and_metadata(
    client: AccountClient, agent_id: str, source: Path, manifest: dict
) -> None:
    """Bulk-write description / prompts / routing metadata, then the status cmd.

    One ``PUT agents/{id}`` through the account escape hatch — the same write
    the ``authoring-agent-prompts`` guide documents; omitted keys stay unchanged.
    """
    body: dict = {}
    if manifest.get("description"):
        body["description"] = manifest["description"]
    if manifest.get("router_trigger_prompt"):
        body["router_trigger_prompt"] = manifest["router_trigger_prompt"]
    if manifest.get("example_prompts"):
        body["example_prompts"] = manifest["example_prompts"]

    for key, field in _PROMPT_FIELDS.items():
        rel = (manifest.get("prompts") or {}).get(key)
        if not rel:
            continue
        prompt_path = source / rel
        if not prompt_path.is_file():
            console.warn(f"Prompt file '{rel}' is missing — skipping {field}.")
            continue
        text = prompt_path.read_text().strip()
        if not text:
            console.warn(f"Prompt file '{rel}' is empty — skipping {field}.")
            continue
        body[field] = text

    if body:
        client.update_agent_config(agent_id, body)
        _line(f"  Wrote {', '.join(sorted(body))}.")
    else:
        console.warn("Nothing to write — the manifest carries no prompts or metadata.")

    command = manifest.get("status_refresh_command")
    if command:
        client.set_status_refresh_command(agent_id, command)
        _line(f"  Status refresh command: {command}")


def _push_workspace(config, workspace_root: Path) -> bool:
    """Flush the local workspace to the agent. Returns True when it settled."""
    try:
        sync_session.ensure_session(config, workspace_root)
        status = sync_session.flush(config)
    except Exception as exc:
        console.warn(f"Sync push failed: {exc}")
        console.warn(
            "Fix the sync session and run 'cinna sync push --agent "
            f"{workspace_root.name}' before re-running with --update."
        )
        return False
    if status.conflict_count:
        console.warn(
            f"{status.conflict_count} sync conflict(s) — your files are NOT fully "
            "live. Resolve with 'cinna sync resolve --prefer local', then re-run "
            "this import with --update."
        )
        return False
    _line(f"  Sync settled ({status.state}).")
    return True


def _sync_credentials(
    client: AccountClient,
    agent_id: str,
    agent_name: str,
    specs: list[dict],
    user_workspace_id: str | None,
) -> list[tuple[str, str]]:
    """Create the manifest's credentials as empty drafts and attach them.

    Name-idempotent: an existing credential with the same name is attached, not
    recreated. No secret value is ever sent or read — the returned setup URLs
    are what the user opens to fill them in the browser.
    """
    if not specs:
        return []

    existing = {
        c.get("name"): c
        for c in client.list_credentials(user_workspace_id=user_workspace_id).get(
            "data", []
        )
    }

    setup_urls: list[tuple[str, str]] = []
    for spec in specs:
        cred_name = spec["name"]
        found = existing.get(cred_name)
        if found:
            client.share_credential_with_agent(found["id"], agent_id)
            _line(
                f"  '{cred_name}' already exists — attached to {agent_name}."
            )
            continue
        result = client.create_credential(
            cred_name,
            spec["type"],
            notes=spec.get("description"),
            service_uri=spec.get("service_uri"),
            allow_sharing=False,
            user_workspace_id=user_workspace_id,
        )
        credential = result.get("credential", {})
        client.share_credential_with_agent(credential.get("id"), agent_id)
        _line(
            f"  Draft '{cred_name}' [{spec['type']}] created and attached."
        )
        setup_url = result.get("setup_url")
        if setup_url:
            setup_urls.append((cred_name, setup_url))
    return setup_urls


def _sync_schedules(
    client: AccountClient, agent_id: str, specs: list[dict], update: bool
) -> None:
    """Create the manifest's schedules; name-idempotent, updated with ``--update``."""
    if not specs:
        return

    existing = {
        s.get("name"): s for s in client.list_schedules(agent_id).get("data", [])
    }

    for spec in specs:
        body = {
            "name": spec["name"],
            "cron_string": spec["cron_string"],
            "timezone": spec.get("timezone") or "UTC",
            "description": spec.get("description") or spec["name"],
            "enabled": spec.get("enabled", True),
            "schedule_type": _schedule_type(spec),
        }
        if spec.get("prompt") is not None:
            body["prompt"] = spec["prompt"]
        if spec.get("command") is not None:
            body["command"] = spec["command"]

        found = existing.get(spec["name"])
        if found and update:
            client.update_schedule(agent_id, found["id"], body)
            _line(f"  Updated schedule '{spec['name']}'.")
        elif found:
            _line(
                f"  Schedule '{spec['name']}' already exists — left unchanged "
                f"(re-run with --update to overwrite)."
            )
        else:
            created = client.create_schedule(agent_id, body)
            _line(
                f"  Created schedule '{spec['name']}' ({created.get('cron_string')})."
            )


def _print_summary(
    account_cfg,
    agent_id: str,
    agent_name: str,
    slug: str,
    child_root: Path,
    setup_urls: list[tuple[str, str]],
    examples: list[str],
) -> None:
    frontend = account_cfg.frontend_url.rstrip("/")
    console.console.print()
    console.status(f"Imported {agent_name}")
    _line(f"  Agent ID:   {agent_id}")
    _line(f"  Web UI:     {frontend}/agent/{agent_id}")
    _line(f"  Workspace:  {child_root}/")

    if setup_urls:
        console.console.print()
        console.console.print(
            "[bold]Open these to fill the credential values[/bold] "
            "(the CLI never handles secrets):"
        )
        for cred_name, url in setup_urls:
            _line(f"    • {cred_name}: {url}")

    console.console.print()
    _line("Next:")
    first_example = examples[0] if examples else "hello"
    _line(f'  cinna chat --agent {slug} "{first_example}"')
    _line(f"  cd {child_root} && cinna dev")
    console.console.print()
