"""``cinna library`` — share kit agents with the team through cinna-core.

A shared agent is a git repository cinna-core hosts, one per agent, served over
smart HTTP at ``<platform>/git/<repo_id>.git`` and signed in to with the token
the CLI already holds (S1/S2 of the team-sharing plan). On disk it is an
ordinary Local Agent Kit folder, ``<workshop>/Local/<slug>/``, that happens to
have a remote: ``share`` runs ``git init`` in place, so nothing is copied.

The verbs map one to one onto Cinna Desktop's, so the docs can say "Share
changes is ``cinna library share``":

=================  ==========================  ==================================
CLI                Desktop                     Does
=================  ==========================  ==================================
``list``           Team library                ``GET /api/v1/library``
``get``            Get                         clone into ``Local/<slug>/``
``share``          Share with the team /       create the repo when there is
                   Share changes               none, commit all, push
``update``         Update                      fetch, fast-forward or clean rebase
``status``         the status line             ahead / behind / uncommitted
``grants``         (admin UI)                  ``PUT …/grants``
``suggest``        Suggest a change            ``POST …/suggestions``
=================  ==========================  ==================================

Everything runs from an account workspace with the ``cli-account`` token, or
from anywhere inside the workshop when exactly one account workspace sits
under its ``Cloud/``. Git goes through :func:`cinna.git_versioning.run_git` —
the same wrapper, the same token injection as ``cinna git`` on a ``cinna``
remote; only the token kind differs (account here, per-agent there).

Rules that never bend:

* the token reaches git only through the child's environment
  (:func:`cinna.git_versioning.remote_auth_env`) — never argv, never
  ``.git/config``, never a log line, never output;
* fast-forward only: a push the team's history rejects gets exactly one clean
  rebase, and a rebase that does not apply cleanly is aborted and refused with
  the desktop's two options spelled out (S8). Nothing is merged, nothing is
  force-pushed, nothing is lost;
* ``credentials/``, ``app-data/`` and every secret file the kit's
  ``secret_files`` rule names are refused before a commit is made — the
  server's ``pre-receive`` refuses them too, but nothing should leave the
  machine to find that out.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import click

from cinna import console
from cinna import kit_contract
from cinna.account import (
    AccountConfig,
    find_account_root,
    load_account_config,
)
from cinna.client import AccountClient
from cinna.errors import (
    AccountConfigNotFoundError,
    EXIT_ERROR,
    CinnaExit,
)
from cinna.git_versioning import (
    RemoteAuth,
    auth_failure_hint,
    require_token_capable_git,
    run_git,
)

MANIFEST_FILENAME = "cinna-agent.json"
LOCAL_DIR = "Local"
CLOUD_DIR = "Cloud"
BRANCH = "main"
REMOTE = "origin"
REMOTE_BRANCH = f"{REMOTE}/{BRANCH}"

#: Non-secret marker in ``.git/config`` naming the library repository a folder
#: belongs to. The URL alone would do (``/git/<repo_id>.git``); the key keeps
#: the answer stable if a platform ever serves clones under another path.
REPO_ID_KEY = "cinna.library.repoId"

_GIT_URL_RE = re.compile(r"/git/([^/]+?)\.git/?$")

#: Never part of a shared agent, whatever the folder's own ``.gitignore`` says.
#: ``credentials/`` holds the local ``.env``; ``app-data/`` is a running agent's
#: state; ``publications.json`` is this machine's record of its cloud imports.
LOCAL_ONLY = ("credentials/", "app-data/", kit_contract.PUBLICATIONS_FILENAME)

#: The CLI's own log, written to whatever folder it runs in (any depth).
_LOG_PATTERNS = ("cinna.log", "cinna.log.*")

_EXCLUDE_MARKER = "# cinna library: never shared (local-only)"

ACCESS_LEVELS = ("read", "write", "maintain")


def _refuse(code: str, detail: str, **extra) -> CinnaExit:
    return CinnaExit(EXIT_ERROR, code, detail, extra=extra or None)


def _line(message: str) -> None:
    """One data-bearing line, verbatim (slugs and names may hold ``[``)."""
    console.console.print(message, markup=False, highlight=False)


def _echo_json(payload) -> None:
    click.echo(json.dumps(payload, indent=2, default=str))


# ── Where things are ────────────────────────────────────────────────────────


def _account_roots_in(workshop: Path) -> list[Path]:
    cloud = workshop / CLOUD_DIR
    if not cloud.is_dir():
        return []
    return sorted(
        p.parent.parent for p in cloud.glob("*/.cinna/account.json") if p.is_file()
    )


def _host(url: str | None) -> str:
    return (urlsplit(url or "").netloc or "").lower()


def resolve_account(
    start: Path | None = None, prefer_url: str | None = None
) -> tuple[Path, AccountConfig]:
    """The account workspace to act as: the one around ``start``, else the
    workshop's.

    ``cinna library share`` is naturally run from inside ``Local/<slug>/``, which
    is not inside an account workspace. Then the workshop above it is asked: one
    account workspace under ``Cloud/`` is the answer; several are narrowed by
    ``prefer_url``'s host (the folder's remote) and otherwise refused, naming
    them, rather than guessed.
    """
    cwd = Path.cwd().resolve()
    start = (start or cwd).resolve()
    for where in dict.fromkeys((cwd, start)):
        try:
            root = find_account_root(where)
            return root, load_account_config(root)
        except AccountConfigNotFoundError:
            pass
    kit = kit_contract.find_kit_root(start) or kit_contract.find_kit_root(cwd)
    roots = _account_roots_in(kit.parent) if kit is not None else []
    if len(roots) > 1 and prefer_url:
        host = _host(prefer_url)
        matching = [
            r for r in roots if _host(load_account_config(r).platform_url) == host
        ]
        if len(matching) == 1:
            roots = matching
    if len(roots) == 1:
        return roots[0], load_account_config(roots[0])
    if len(roots) > 1:
        names = ", ".join(str(r.relative_to(kit.parent)) for r in roots)
        raise _refuse(
            "ambiguous_account",
            f"This workshop has several account workspaces ({names}). Run the "
            "command from inside the one to use.",
        )
    raise AccountConfigNotFoundError()


def workshop_root(start: Path) -> Path | None:
    """The folder holding ``.cinna-kit/``, found by walking up from ``start``."""
    kit = kit_contract.find_kit_root(start)
    return kit.parent if kit is not None else None


def local_dir(account_root: Path, dir_opt: str | None = None) -> Path:
    """``<workshop>/Local`` (or ``--dir``): where library agents live on disk."""
    if dir_opt:
        return Path(dir_opt).expanduser().resolve()
    for start in (Path.cwd(), account_root):
        root = workshop_root(start)
        if root is not None:
            return root / LOCAL_DIR
    raise _refuse(
        "no_workshop",
        "No workshop found: there is no .cinna-kit/ above this folder or the "
        "account workspace, so there is no Local/ to put agents in. Pass --dir.",
    )


def _local_dir_or_none(account_root: Path) -> Path | None:
    try:
        return local_dir(account_root)
    except CinnaExit:
        return None


def resolve_folder(path: str | None, account_root: Path | None = None) -> Path:
    """The kit folder a verb acts on.

    No ``path``: the folder holding ``cinna-agent.json`` at or above the current
    directory. A ``path`` that is a directory is taken as given; otherwise it is
    read as a slug under ``Local/``.
    """
    if path is None:
        current = Path.cwd().resolve()
        while True:
            if (current / MANIFEST_FILENAME).is_file():
                return current
            if current.parent == current:
                raise _refuse(
                    "not_an_agent_folder",
                    f"No {MANIFEST_FILENAME} here or above. Run this inside an "
                    "agent folder, or name one (a path or a slug under Local/).",
                )
            current = current.parent
    candidate = Path(path).expanduser()
    if candidate.is_dir():
        folder = candidate.resolve()
    else:
        base = _local_dir_or_none(account_root) if account_root else None
        folder = (base / path) if base is not None else candidate
        if not folder.is_dir():
            raise _refuse("not_an_agent_folder", f"No agent folder '{path}'.")
    if not (folder / MANIFEST_FILENAME).is_file():
        raise _refuse(
            "not_an_agent_folder",
            f"{folder} has no {MANIFEST_FILENAME}, so it is not a kit agent folder.",
        )
    return folder


# ── The folder's remote ─────────────────────────────────────────────────────


@dataclass
class LibraryRemote:
    repo_id: str
    url: str


def _git_out(folder: Path, args: list[str]) -> str | None:
    result = run_git(args, folder)
    return result.stdout.strip() if result.returncode == 0 else None


def is_git_repo(folder: Path) -> bool:
    return (folder / ".git").exists()


def origin_url(folder: Path) -> str | None:
    if not is_git_repo(folder):
        return None
    return _git_out(folder, ["remote", "get-url", REMOTE]) or None


def library_remote(folder: Path) -> LibraryRemote | None:
    """The library repository ``folder`` is a clone of, or ``None``.

    ``None`` both for no remote at all and for an external one (GitHub, …):
    the latter is the developer's own and is never touched here.
    """
    url = origin_url(folder)
    if not url:
        return None
    repo_id = _git_out(folder, ["config", "--get", REPO_ID_KEY])
    if not repo_id:
        m = _GIT_URL_RE.search(urlsplit(url).path)
        repo_id = m.group(1) if m else None
    return LibraryRemote(repo_id=repo_id, url=url) if repo_id else None


def _manifest_id(folder: Path) -> str | None:
    try:
        data = json.loads((folder / MANIFEST_FILENAME).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    value = data.get("id") if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def _agent_folders(base: Path | None) -> list[Path]:
    if base is None or not base.is_dir():
        return []
    return sorted(
        p for p in base.iterdir() if p.is_dir() and (p / MANIFEST_FILENAME).is_file()
    )


def find_local_copy(base: Path | None, agent: dict) -> Path | None:
    """The folder under ``base`` holding ``agent``: by remote, then manifest id."""
    folders = _agent_folders(base)
    for folder in folders:
        remote = library_remote(folder)
        if remote and (
            remote.repo_id == agent.get("repo_id") or remote.url == agent.get("clone_url")
        ):
            return folder
    manifest_id = (agent.get("manifest") or {}).get("id")
    if manifest_id:
        for folder in folders:
            if _manifest_id(folder) == manifest_id:
                return folder
    return None


def write_local_excludes(folder: Path) -> None:
    """Keep :data:`LOCAL_ONLY` out of the tree via ``.git/info/exclude``.

    Per clone, never committed, rewritten idempotently: the folder's committed
    ``.gitignore`` is the team's, and this is a guarantee independent of it.
    """
    exclude = folder / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text() if exclude.exists() else ""
    idx = existing.find(_EXCLUDE_MARKER)
    if idx != -1:
        existing = existing[:idx]
    head = existing.rstrip("\n")
    block = "\n".join(
        [_EXCLUDE_MARKER, *(f"/{p}" for p in LOCAL_ONLY), *_LOG_PATTERNS]
    )
    exclude.write_text((head + "\n\n" if head else "") + block + "\n")


# ── git, the fail-loud way ──────────────────────────────────────────────────


def _git(
    folder: Path, args: list[str], what: str, auth: RemoteAuth | None = None
):
    result = run_git(args, folder, auth=auth)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        code = "git_failed"
        if auth and "403" in detail:
            code = "no_access"
        elif auth and auth_failure_hint(detail):
            code = "auth_failed"
        hint = auth_failure_hint(detail) if auth else ""
        raise _refuse(code, f"{what} failed:\n{detail}{hint}")
    return result


def _auth(account_cfg: AccountConfig, url: str) -> RemoteAuth:
    return RemoteAuth(url=url, token=account_cfg.account_token)


def _has_ref(folder: Path, ref: str) -> bool:
    return run_git(["rev-parse", "--verify", "--quiet", ref], folder).returncode == 0


def _has_head(folder: Path) -> bool:
    return _has_ref(folder, "HEAD")


def _uncommitted(folder: Path) -> list[str]:
    out = _git(folder, ["status", "--porcelain", "-z"], "git status").stdout or ""
    return [entry[3:] for entry in out.split("\0") if len(entry) > 3]


def _ahead_behind(folder: Path) -> tuple[int, int]:
    """``(ahead, behind)`` of HEAD against ``origin/main``."""
    if not _has_ref(folder, REMOTE_BRANCH):
        return (_count(folder, "HEAD") if _has_head(folder) else 0), 0
    if not _has_head(folder):
        return 0, _count(folder, REMOTE_BRANCH)
    out = _git(
        folder,
        ["rev-list", "--left-right", "--count", f"HEAD...{REMOTE_BRANCH}"],
        "git rev-list",
    ).stdout.split()
    return int(out[0]), int(out[1])


def _count(folder: Path, ref: str) -> int:
    return int(_git(folder, ["rev-list", "--count", ref], "git rev-list").stdout.strip())


def _short(sha: str | None) -> str:
    return (sha or "")[:8] or "—"


def _fetch(folder: Path, auth: RemoteAuth) -> None:
    _git(folder, ["fetch", "--quiet", REMOTE], "git fetch", auth=auth)


def diverged_refusal(folder: Path) -> CinnaExit:
    """S8: the two options, as sentences, with the commands that carry them out."""
    copy = f"my-changes-{datetime.now():%Y%m%d-%H%M}"
    return _refuse(
        "diverged",
        "The team changed this agent, and your changes do not apply on top of "
        "theirs cleanly. Nothing was changed or lost. Choose one:\n"
        "  1. Use the team's version and keep yours as a copy:\n"
        f"       git -C {folder} branch {copy}\n"
        f"       git -C {folder} reset --hard {REMOTE_BRANCH}\n"
        "  2. Keep working on yours and share later: leave the folder as it is. "
        "Nothing changes until you share, and sharing will stop here again "
        "until the two versions are reconciled by hand "
        f"(git -C {folder} rebase {REMOTE_BRANCH}).",
        path=str(folder),
    )


def _rebase_clean(folder: Path) -> None:
    """Rebase onto ``origin/main`` when it applies cleanly; else abort and refuse."""
    result = run_git(["rebase", REMOTE_BRANCH], folder)
    if result.returncode != 0:
        run_git(["rebase", "--abort"], folder)
        raise diverged_refusal(folder)


def _rejected(detail: str) -> bool:
    return any(
        s in detail for s in ("non-fast-forward", "[rejected]", "fetch first")
    )


def _push(folder: Path, auth: RemoteAuth) -> bool:
    """Push HEAD to ``main``; on a non-fast-forward, one clean rebase, then again.

    Returns whether a rebase happened.
    """
    args = ["push", "--quiet", REMOTE, f"HEAD:refs/heads/{BRANCH}"]
    result = run_git(args, folder, auth=auth)
    if result.returncode == 0:
        return False
    detail = (result.stderr or result.stdout or "").strip()
    if not _rejected(detail):
        _git(folder, args, "git push", auth=auth)  # re-raise with the right code
    _fetch(folder, auth)
    _rebase_clean(folder)
    _git(folder, args, "git push", auth=auth)
    return True


def _set_upstream(folder: Path) -> None:
    branch = _git_out(folder, ["symbolic-ref", "--short", "HEAD"])
    if branch and _has_ref(folder, REMOTE_BRANCH):
        run_git(["branch", f"--set-upstream-to={REMOTE_BRANCH}", branch], folder)


# ── Secrets never leave ─────────────────────────────────────────────────────


def _secret_rules(folder: Path) -> list:
    return kit_contract.secret_file_rules(
        kit_contract.load_layout(kit_contract.find_kit_root(folder))
    )


def _would_track(folder: Path) -> list[str]:
    """Every path ``git add -A`` would leave in the index (ignores honoured)."""
    out = _git(
        folder,
        ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        "git ls-files",
    ).stdout or ""
    return [p for p in out.split("\0") if p and (folder / p).exists()]


def refuse_secrets(folder: Path, paths: list[str] | None = None) -> None:
    """Refuse when a path the share would carry is a credential or runtime state."""
    rules = _secret_rules(folder)
    hits = []
    for rel in paths if paths is not None else _would_track(folder):
        top = rel.split("/", 1)[0] + "/"
        if top in ("credentials/", "app-data/") or kit_contract.is_secret_filename(
            rel.rsplit("/", 1)[-1], rules
        ):
            hits.append(rel)
    if hits:
        raise _refuse(
            "secret_in_tree",
            "This agent includes files that can't be shared (credentials or a "
            "running agent's data). Move them out of the folder or add them to "
            "its .gitignore, then share again:\n  " + "\n  ".join(hits),
            paths=hits,
        )


# ── The listing ─────────────────────────────────────────────────────────────


def _listing(client: AccountClient) -> dict:
    payload = client.list_library()
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("agents", [])
    payload.setdefault("mirrors", [])
    return payload


def find_agent(payload: dict, ref: str) -> dict:
    agents = [a for a in payload.get("agents") or [] if isinstance(a, dict)]
    for agent in agents:
        if ref in (agent.get("slug"), agent.get("repo_id")):
            return agent
    known = ", ".join(sorted(str(a.get("slug")) for a in agents)) or "none"
    raise _refuse(
        "not_in_library",
        f"No agent '{ref}' in the team library you can read (available: {known}).",
    )


def _maintainers(agent: dict) -> str:
    people = [
        str(m.get("display_name") or m.get("user_id") or "?")
        for m in agent.get("maintainers") or []
        if isinstance(m, dict)
    ]
    return ", ".join(people) or "—"


def run_list(as_json: bool = False) -> None:
    """``cinna library list``."""
    from rich.table import Table

    account_root, account_cfg = resolve_account()
    with AccountClient(account_cfg) as client:
        with console.spinner("Reading the team library..."):
            payload = _listing(client)

    base = _local_dir_or_none(account_root)
    for agent in payload["agents"]:
        if isinstance(agent, dict):
            folder = find_local_copy(base, agent)
            agent["local_path"] = str(folder) if folder else None

    if as_json:
        _echo_json(payload)
        return

    agents = [a for a in payload["agents"] if isinstance(a, dict)]
    if not agents:
        console.status("The team library has no agents you can read yet.")
    else:
        table = Table(title="Team library", title_style="bold")
        table.add_column("Slug", overflow="fold")
        table.add_column("Name")
        table.add_column("Access")
        table.add_column("Maintainers")
        table.add_column("Head", style="dim")
        table.add_column("Here", overflow="fold")
        for a in agents:
            local = a.get("local_path")
            table.add_row(
                str(a.get("slug") or "?"),
                str(a.get("name") or ""),
                str(a.get("access") or "?"),
                _maintainers(a),
                _short(a.get("head_sha")),
                (Path(local).name if local else "—"),
            )
        console.console.print(table)

    mirrors = [m for m in payload["mirrors"] if isinstance(m, dict)]
    if mirrors:
        table = Table(title="Marketplaces", title_style="bold")
        table.add_column("Name")
        table.add_column("Format")
        table.add_column("Entries", justify="right")
        for m in mirrors:
            table.add_row(
                str(m.get("name") or "?"),
                str(m.get("format") or "?"),
                str(len(m.get("entries") or [])),
            )
        console.console.print(table)


# ── get ─────────────────────────────────────────────────────────────────────


def clone_agent(agent: dict, target: Path, account_cfg: AccountConfig) -> None:
    """Clone ``agent`` into ``target`` (which must not exist) and mark it."""
    url = agent.get("clone_url")
    if not url:
        raise _refuse("not_in_library", "The library listed no clone URL for it.")
    target.parent.mkdir(parents=True, exist_ok=True)
    _git(
        target.parent,
        ["clone", "--quiet", "--origin", REMOTE, url, target.name],
        "git clone",
        auth=_auth(account_cfg, url),
    )
    run_git(["config", REPO_ID_KEY, str(agent.get("repo_id"))], target)
    write_local_excludes(target)


def run_get(ref: str, dir_opt: str | None = None, as_json: bool = False) -> None:
    """``cinna library get <slug>``."""
    require_token_capable_git()
    account_root, account_cfg = resolve_account()
    base = local_dir(account_root, dir_opt)
    with AccountClient(account_cfg) as client:
        agent = find_agent(_listing(client), ref)

    slug = str(agent.get("slug") or ref)
    target = base / slug
    if target.exists():
        raise _refuse(
            "folder_exists",
            f"{target} already exists. Move it aside, or get the agent into "
            "another folder with --dir.",
            path=str(target),
        )
    existing = find_local_copy(base, agent)
    if existing is not None:
        raise _refuse(
            "already_have",
            f"You already have this agent at {existing}. Bring it up to date "
            f"with 'cinna library update {existing.name}'.",
            path=str(existing),
        )

    with console.spinner(f"Getting {slug}..."):
        clone_agent(agent, target, account_cfg)

    if not (target / MANIFEST_FILENAME).is_file():
        console.warn(
            f"{slug} has no {MANIFEST_FILENAME} yet — the team has not shared "
            "anything into it."
        )
    result = {
        "result": "ok",
        "slug": slug,
        "repo_id": agent.get("repo_id"),
        "path": str(target),
        "head": _git_out(target, ["rev-parse", "HEAD"]),
    }
    if as_json:
        _echo_json(result)
        return
    console.status(f"Got {agent.get('name') or slug} into {target}.")


# ── share ───────────────────────────────────────────────────────────────────


def _commit_all(folder: Path, message: str | None) -> bool:
    """Stage everything (ignores honoured) and commit. False when nothing changed."""
    if not _uncommitted(folder):
        return False
    if not message:
        raise click.UsageError(
            "There are changes to share: say what changed with -m/--message."
        )
    refuse_secrets(folder)
    _git(folder, ["add", "-A"], "git add")
    if run_git(["diff", "--cached", "--quiet"], folder).returncode == 0:
        return False
    _git(folder, ["commit", "--quiet", "-m", message], "git commit")
    return True


def _share_new(
    folder: Path, manifest: dict, message: str | None, account_cfg: AccountConfig
) -> dict:
    """No remote yet: create the library repository, init in place, commit, push."""
    created_git = not is_git_repo(folder)
    if created_git:
        _git(folder, ["init", "--quiet"], "git init")
        run_git(["symbolic-ref", "HEAD", f"refs/heads/{BRANCH}"], folder)
    write_local_excludes(folder)
    try:
        refuse_secrets(folder)
    except CinnaExit:
        if created_git:
            shutil.rmtree(folder / ".git", ignore_errors=True)
        raise

    with AccountClient(account_cfg) as client:
        agent = client.create_library_agent(
            manifest["slug"], manifest["name"], manifest.get("description")
        )
    url = agent.get("clone_url")
    if not url:
        raise _refuse(
            "platform_error", "The platform created the repository but sent no clone URL."
        )
    _git(folder, ["remote", "add", REMOTE, url], "git remote add")
    run_git(["config", REPO_ID_KEY, str(agent.get("repo_id"))], folder)

    _commit_all(folder, message or f"Share {manifest['name']} with the team")
    _push(folder, _auth(account_cfg, url))
    _fetch(folder, _auth(account_cfg, url))
    _set_upstream(folder)
    return {"created": True, "repo_id": agent.get("repo_id"), "rebased": False}


def _share_changes(
    folder: Path, remote: LibraryRemote, message: str | None, account_cfg: AccountConfig
) -> dict:
    with AccountClient(account_cfg) as client:
        agent = client.get_library_agent(remote.repo_id)
    if agent.get("access") == "read":
        raise _refuse(
            "read_only",
            "You can read this agent but not change it for the team. Send your "
            "change to its maintainers with 'cinna library suggest -m …'.",
        )
    auth = _auth(account_cfg, remote.url)
    write_local_excludes(folder)
    committed = _commit_all(folder, message)
    _fetch(folder, auth)
    ahead, _ = _ahead_behind(folder)
    if not ahead:
        return {"created": False, "repo_id": remote.repo_id, "nothing_to_share": True}
    rebased = _push(folder, auth)
    _fetch(folder, auth)
    _set_upstream(folder)
    return {
        "created": False,
        "repo_id": remote.repo_id,
        "committed": committed,
        "shared": ahead,
        "rebased": rebased,
    }


def run_share(path: str | None, message: str | None, as_json: bool = False) -> None:
    """``cinna library share [<path>] -m <message>``."""
    from cinna.local_import import load_manifest

    require_token_capable_git()
    folder = resolve_folder(path, _account_hint())
    remote = library_remote(folder)
    _, account_cfg = resolve_account(folder, remote.url if remote else None)

    if remote is None and origin_url(folder):
        raise _refuse(
            "foreign_remote",
            f"{folder} already has its own git remote ({origin_url(folder)}). "
            "The team library will not take over a remote it does not host; "
            "keep using that one with git, or share a copy of the folder.",
        )
    manifest = load_manifest(folder)

    with console.spinner("Sharing with the team..."):
        if remote is None:
            result = _share_new(folder, manifest, message, account_cfg)
        else:
            result = _share_changes(folder, remote, message, account_cfg)

    result.update(
        result="ok",
        slug=manifest["slug"],
        path=str(folder),
        head=_git_out(folder, ["rev-parse", "HEAD"]),
    )
    if as_json:
        _echo_json(result)
        return
    if result.get("created"):
        console.status(f"Shared {manifest['name']} with the team. Up to date with the team.")
    elif result.get("nothing_to_share"):
        console.status("Nothing to share — up to date with the team.")
    else:
        n = result["shared"]
        tail = " (on top of the team's newer changes)" if result.get("rebased") else ""
        console.status(f"Shared {n} change{'' if n == 1 else 's'}{tail}. Up to date with the team.")


def _account_hint() -> Path | None:
    """The account root around the cwd, if any — only to resolve a bare slug."""
    try:
        return resolve_account()[0]
    except CinnaExit:
        return None


# ── update / status ─────────────────────────────────────────────────────────


@dataclass
class FolderReport:
    folder: Path
    repo_id: str | None = None
    ahead: int = 0
    behind: int = 0
    uncommitted: int = 0
    state: str = "up_to_date"
    updated: int = 0
    code: str | None = None
    detail: str | None = None
    extra: dict = field(default_factory=dict)

    def as_json(self) -> dict:
        data = {
            "slug": self.folder.name,
            "path": str(self.folder),
            "repo_id": self.repo_id,
            "state": self.state,
            "ahead": self.ahead,
            "behind": self.behind,
            "uncommitted": self.uncommitted,
        }
        if self.updated:
            data["updated"] = self.updated
        if self.code:
            data["code"] = self.code
            data["detail"] = self.detail
        return data


def _require_remote(folder: Path) -> LibraryRemote:
    remote = library_remote(folder)
    if remote is None:
        raise _refuse(
            "not_shared",
            f"{folder.name} is not in the team library. Share it first with "
            "'cinna library share'.",
        )
    return remote


def update_folder(folder: Path, account_cfg: AccountConfig) -> FolderReport:
    remote = _require_remote(folder)
    report = FolderReport(folder=folder, repo_id=remote.repo_id)
    changed = _uncommitted(folder)
    if changed:
        raise _refuse(
            "uncommitted_changes",
            f"{folder.name} has {len(changed)} changed file(s) you haven't "
            "shared. Share them first ('cinna library share -m …'), or set them "
            f"aside with 'git -C {folder} stash', then update.",
            path=str(folder),
        )
    _fetch(folder, _auth(account_cfg, remote.url))
    if not _has_ref(folder, REMOTE_BRANCH):
        report.state = "empty"
        return report
    ahead, behind = _ahead_behind(folder)
    if behind:
        if not _has_head(folder):
            _git(folder, ["reset", "--quiet", "--hard", REMOTE_BRANCH], "git reset")
        elif not ahead:
            _git(folder, ["merge", "--quiet", "--ff-only", REMOTE_BRANCH], "git merge")
        else:
            _rebase_clean(folder)
        report.updated = behind
        _set_upstream(folder)
    report.ahead, report.behind = _ahead_behind(folder)
    report.state = "ahead" if report.ahead else "up_to_date"
    return report


def status_folder(
    folder: Path, account_cfg: AccountConfig, fetch: bool = True
) -> FolderReport:
    remote = _require_remote(folder)
    report = FolderReport(folder=folder, repo_id=remote.repo_id)
    report.uncommitted = len(_uncommitted(folder))
    if fetch:
        try:
            _fetch(folder, _auth(account_cfg, remote.url))
        except CinnaExit as exc:
            report.state, report.code, report.detail = "unreachable", exc.code, exc.detail
            return report
    if not _has_ref(folder, REMOTE_BRANCH):
        report.state = "empty"
        report.ahead = _count(folder, "HEAD") if _has_head(folder) else 0
        return report
    report.ahead, report.behind = _ahead_behind(folder)
    if report.ahead and report.behind:
        report.state = "diverged"
    elif report.behind:
        report.state = "behind"
    elif report.ahead:
        report.state = "ahead"
    return report


def _status_words(r: FolderReport) -> str:
    """The status line, in the desktop's words with the counts beside them."""
    if r.code:
        return f"couldn't check: {r.detail.splitlines()[0] if r.detail else r.code}"
    parts = {
        "up_to_date": "up to date with the team",
        "empty": "the team has not shared anything yet",
        "ahead": f"{r.ahead} change{'' if r.ahead == 1 else 's'} you haven't shared",
        "behind": f"the team changed this agent ({r.behind} new)",
        "diverged": (
            f"the team changed this agent ({r.behind} new) and you have "
            f"{r.ahead} change{'' if r.ahead == 1 else 's'} you haven't shared"
        ),
    }
    words = parts.get(r.state, r.state)
    if r.uncommitted:
        words += f"; {r.uncommitted} uncommitted file{'' if r.uncommitted == 1 else 's'}"
    return words


def _targets(path: str | None, all_: bool, account_root: Path | None) -> list[Path]:
    if all_ and path:
        raise click.UsageError("Pass a folder or --all, not both.")
    if not all_:
        return [resolve_folder(path, account_root)]
    base = local_dir(account_root) if account_root else None
    if base is None:
        root = workshop_root(Path.cwd())
        if root is None:
            raise _refuse("no_workshop", "No workshop found to walk Local/ in.")
        base = root / LOCAL_DIR
    return [f for f in _agent_folders(base) if library_remote(f) is not None]


def _account_for(targets: list[Path]) -> AccountConfig:
    first = targets[0] if targets else Path.cwd()
    remote = library_remote(first) if targets else None
    return resolve_account(first, remote.url if remote else None)[1]


def run_update(path: str | None, all_: bool, as_json: bool = False) -> None:
    """``cinna library update [<path>|--all]``."""
    require_token_capable_git()
    targets = _targets(path, all_, _account_hint())
    if not targets:
        if as_json:
            _echo_json({"result": "ok", "agents": []})
        else:
            console.status("No agents from the team library here.")
        return
    account_cfg = _account_for(targets)

    reports: list[FolderReport] = []
    for folder in targets:
        try:
            with console.spinner(f"Updating {folder.name}..."):
                reports.append(update_folder(folder, account_cfg))
        except CinnaExit as exc:
            if not all_:
                raise
            reports.append(
                FolderReport(
                    folder=folder, state="refused", code=exc.code, detail=exc.detail
                )
            )

    failed = [r for r in reports if r.code]
    if as_json:
        _echo_json(
            {
                "result": "error" if failed else "ok",
                "agents": [r.as_json() for r in reports],
            }
        )
    else:
        for r in reports:
            if r.code:
                console.error(f"{r.folder.name}: {r.detail}")
            elif r.updated:
                console.status(
                    f"{r.folder.name}: updated with {r.updated} change"
                    f"{'' if r.updated == 1 else 's'} from the team."
                )
            else:
                _line(f"  {r.folder.name}: {_status_words(r)}")
    if failed:
        raise click.exceptions.Exit(EXIT_ERROR)


def run_status(
    path: str | None, all_: bool, fetch: bool = True, as_json: bool = False
) -> None:
    """``cinna library status [<path>|--all]``."""
    if fetch:
        require_token_capable_git()
    targets = _targets(path, all_, _account_hint())
    account_cfg = _account_for(targets) if (targets and fetch) else None
    reports = [status_folder(f, account_cfg, fetch=fetch) for f in targets]
    if as_json:
        _echo_json({"result": "ok", "agents": [r.as_json() for r in reports]})
        return
    if not reports:
        console.status("No agents from the team library here.")
        return
    width = max(len(r.folder.name) for r in reports)
    for r in reports:
        _line(f"{r.folder.name.ljust(width)}  {_status_words(r)}")


# ── grants ──────────────────────────────────────────────────────────────────


def parse_principal(raw: str):
    """``everyone`` · ``role:<name>`` · ``user:<id>`` · an email address."""
    value = raw.strip()
    if value.lower() == "everyone":
        return "everyone"
    if value.startswith("role:") and value[5:]:
        return {"role": value[5:]}
    if value.startswith("user:") and value[5:]:
        return {"user_id": value[5:]}
    if "@" in value:
        return {"email": value}
    raise click.UsageError(
        f"'{raw}' is not a principal: use everyone, role:<name>, user:<id> or an email."
    )


def principal_label(principal) -> str:
    if principal == "everyone" or not isinstance(principal, dict):
        return str(principal)
    if principal.get("role"):
        return f"role:{principal['role']}"
    return str(
        principal.get("email")
        or principal.get("display_name")
        or f"user:{principal.get('user_id')}"
    )


def _same_principal(a, b) -> bool:
    if a == "everyone" or b == "everyone":
        return a == b
    if not (isinstance(a, dict) and isinstance(b, dict)):
        return False
    for key in ("role", "user_id", "email"):
        if a.get(key) and a.get(key) == b.get(key):
            return True
    return False


def apply_grant_changes(
    current: list[dict], add: list[tuple], remove: list
) -> list[dict]:
    grants = [
        {"principal": g.get("principal"), "access": g.get("access")}
        for g in current
        if isinstance(g, dict)
    ]
    for principal in remove:
        grants = [g for g in grants if not _same_principal(g["principal"], principal)]
    for principal, access in add:
        grants = [g for g in grants if not _same_principal(g["principal"], principal)]
        grants.append({"principal": principal, "access": access})
    return grants


def _parse_add(raw: str) -> tuple:
    principal, sep, access = raw.rpartition("=")
    if not sep or access not in ACCESS_LEVELS:
        raise click.UsageError(
            f"--add '{raw}': use <principal>=<access>, access one of "
            f"{', '.join(ACCESS_LEVELS)}."
        )
    return parse_principal(principal), access


def run_grants(
    ref: str, add: tuple[str, ...], remove: tuple[str, ...], as_json: bool = False
) -> None:
    """``cinna library grants <slug> [--add P=ACCESS] [--remove P]``."""
    adds = [_parse_add(a) for a in add]
    removes = [parse_principal(r) for r in remove]
    _, account_cfg = resolve_account()
    with AccountClient(account_cfg) as client:
        agent = find_agent(_listing(client), ref)
        detail = client.get_library_agent(agent["repo_id"])
        grants = detail.get("grants")
        if adds or removes:
            if detail.get("access") != "maintain":
                raise _refuse(
                    "not_maintainer",
                    f"Only a maintainer of {agent.get('slug')} can change who has access.",
                )
            if not isinstance(grants, list):
                raise _refuse(
                    "grants_unreadable",
                    "The platform did not say who has access now, so a change "
                    "could drop someone. Not changing anything.",
                )
            wanted = apply_grant_changes(grants, adds, removes)
            answer = client.set_library_grants(agent["repo_id"], wanted)
            grants = answer.get("grants") if isinstance(answer, dict) else answer
            if not isinstance(grants, list):
                grants = wanted

    if as_json:
        _echo_json({"repo_id": agent["repo_id"], "slug": agent.get("slug"), "grants": grants})
        return
    if not isinstance(grants, list):
        console.warn("The platform did not list this agent's grants.")
        return
    _line(f"Access to {agent.get('slug')}:")
    for g in grants:
        if isinstance(g, dict):
            _line(f"  {principal_label(g.get('principal'))}: {g.get('access')}")
    if not grants:
        _line("  (only its maintainers)")


# ── suggest ─────────────────────────────────────────────────────────────────


def build_patch(folder: Path) -> tuple[str, list[str]]:
    """The folder as it is now (untracked included) against ``origin/main``.

    Built in a throwaway index so the user's own index is never touched.
    """
    with tempfile.TemporaryDirectory(prefix="cinna-suggest-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}

        def git(args: list[str], what: str):
            result = run_git(args, folder, extra_env=env)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                raise _refuse("git_failed", f"{what} failed:\n{detail}")
            return result

        if _has_head(folder):
            git(["read-tree", "HEAD"], "git read-tree")
        git(["add", "-A"], "git add")
        names = git(
            ["diff", "--cached", "--name-only", "-z", REMOTE_BRANCH], "git diff"
        ).stdout
        patch = git(["diff", "--cached", "--binary", REMOTE_BRANCH], "git diff").stdout
    return patch, [n for n in (names or "").split("\0") if n]


def run_suggest(path: str | None, message: str, as_json: bool = False) -> None:
    """``cinna library suggest [<path>] -m <message>``."""
    require_token_capable_git()
    folder = resolve_folder(path, _account_hint())
    remote = _require_remote(folder)
    _, account_cfg = resolve_account(folder, remote.url)
    _fetch(folder, _auth(account_cfg, remote.url))
    if not _has_ref(folder, REMOTE_BRANCH):
        raise _refuse(
            "nothing_to_suggest", "The team has not shared anything into this agent yet."
        )
    patch, paths = build_patch(folder)
    if not patch.strip():
        raise _refuse(
            "nothing_to_suggest",
            "Your copy is the same as the team's — there is no change to suggest.",
        )
    refuse_secrets(folder, [p for p in paths if (folder / p).exists()])
    with AccountClient(account_cfg) as client:
        answer = client.suggest_library_change(remote.repo_id, patch, message)

    if as_json:
        _echo_json(
            {"result": "ok", "repo_id": remote.repo_id, "files": paths, "suggestion": answer}
        )
        return
    console.status(
        f"Suggested a change to {len(paths)} file{'' if len(paths) == 1 else 's'} "
        f"to the maintainers of {folder.name}."
    )


# ── For `cinna agent import --from-library` ─────────────────────────────────


@dataclass
class LibrarySource:
    """A folder to import from, and how to tidy up after."""

    folder: Path
    agent: dict
    temporary: bool
    contract_from: Path | None

    def cleanup(self) -> None:
        if self.temporary:
            shutil.rmtree(self.folder.parent, ignore_errors=True)


def checkout_for_import(ref: str) -> LibrarySource:
    """The library agent ``ref`` on disk, for the ordinary import to read.

    The local copy in ``Local/`` when there is one (its ``publications.json``
    then keeps the import's record across runs); otherwise a temporary clone,
    read with the workshop's kit contract when a workshop is found.
    cinna-core has no checkout-from-library route yet; when it lands it
    replaces this with one call.
    """
    require_token_capable_git()
    account_root, account_cfg = resolve_account()
    with AccountClient(account_cfg) as client:
        agent = find_agent(_listing(client), ref)
    base = _local_dir_or_none(account_root)
    existing = find_local_copy(base, agent)
    if existing is not None:
        return LibrarySource(existing, agent, temporary=False, contract_from=None)
    tmp = Path(tempfile.mkdtemp(prefix="cinna-library-"))
    target = tmp / str(agent.get("slug") or ref)
    try:
        clone_agent(agent, target, account_cfg)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return LibrarySource(target, agent, temporary=True, contract_from=base)
