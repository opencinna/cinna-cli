"""Git-versioning integration for cinna agents (Model A).

This module turns a locally-synced agent into a real git working tree the
developer can ``commit`` / ``push`` / ``pull`` against the agent's external git
remote, using **their own** git/SSH credentials (the platform's deploy key
never reaches the CLI). It implements the cinna-cli half of the contract
documented in the "Git Versioning" section of ``docs/README.md`` (backend half:
cinna-core ``docs/agents/agent_git_versioning/``).

Two orthogonal layers coexist on the same folder and must not be conflated:

* **Mutagen (runtime)** keeps ``<agent>/workspace/`` mirrored to the running
  container in near-real-time. Mutagen already ignores ``.git``.
* **Git (preservation)** versions the same files against the remote repo. All
  git operations are local; they meet the backend only at the remote.

Layout (see ``config.compute_agent_layout``): the agent dir (``workspace_root``)
is the repo's ``<subdir>/`` node; its parent is the git working-tree root
(``clone_path``). For a repo-root agent (``subdir`` is None) the clone root and
the agent dir are the same directory.

Remote kinds (``auth_hint``): an **external** remote (``ssh`` / ``https`` /
anything else) authenticates with the developer's own git setup, as above. A
**cinna** remote (``auth_hint == "cinna"``) is a repository cinna-core hosts
for the team library; git authenticates to it with the CLI token the workspace
already holds, passed to the git child through environment-provided config
(:func:`remote_auth_env`) — never in argv, never in ``.git/config``, never
logged. ``cinna library`` (:mod:`cinna.library`) drives the same
:func:`run_git` with the account token.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import urlsplit

import click

from cinna import console
from cinna.client import PlatformClient
from cinna.config import (
    CinnaConfig,
    GitLayout,
    clone_root,
    save_config,
    upsert_agent_registry,
)

logger = logging.getLogger("cinna.git")


# ── Backend coordinates (GET /api/v1/cli/git-coordinates) ─────────────────


@dataclass
class GitCoordinates:
    vcs_enabled: bool = False
    repo_url: str | None = None
    subdir: str | None = None
    ref: str | None = None
    sync_direction: str | None = None
    last_synced_commit: str | None = None
    auth_hint: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "GitCoordinates":
        if not isinstance(data, dict):
            return cls()
        known = {k: data.get(k) for k in cls.__dataclass_fields__}
        known["vcs_enabled"] = bool(known.get("vcs_enabled"))
        return cls(**known)

    @property
    def effective_ref(self) -> str:
        return self.ref or "main"

    def allows_push(self) -> bool:
        return (self.sync_direction or "bidirectional") in ("push", "bidirectional")

    def allows_pull(self) -> bool:
        return (self.sync_direction or "bidirectional") in ("pull", "bidirectional")

    @property
    def is_cinna(self) -> bool:
        """The repo is hosted by cinna-core: git authenticates with the CLI token."""
        return self.auth_hint == CINNA_AUTH_HINT


def fetch_coordinates(client: PlatformClient) -> GitCoordinates:
    """Fetch the agent's git coordinates from the backend."""
    return GitCoordinates.from_dict(client.get_git_coordinates())


# ── git subprocess plumbing ───────────────────────────────────────────────

#: ``auth_hint`` of a repository cinna-core hosts (the team library, or a cloud
#: agent whose git source points at one).
CINNA_AUTH_HINT = "cinna"


@dataclass(frozen=True)
class RemoteAuth:
    """A token for one cinna remote: what :func:`remote_auth_env` injects.

    ``url`` is any URL on the remote's origin (the clone URL will do); the
    header is scoped to ``<scheme>://<host>[:port]/`` so git never sends it
    anywhere else. ``repr`` hides the token so a stray log line cannot carry it.
    """

    url: str
    token: str

    def __repr__(self) -> str:
        return f"RemoteAuth(url={self.url!r}, token=<hidden>)"


def remote_auth_env(auth: RemoteAuth, base: dict | None = None) -> dict:
    """``base`` (default ``os.environ``) plus the token as env-provided git config.

    README §4.4 of the team-sharing plan: ``GIT_CONFIG_COUNT`` /
    ``GIT_CONFIG_KEY_n`` / ``GIT_CONFIG_VALUE_n`` set
    ``http.<origin>/.extraHeader``, so the token is in neither argv nor
    ``.git/config``. Appended after any ``GIT_CONFIG_*`` entries the caller's
    environment already carries rather than replacing them. Needs git ≥ 2.31.
    ``GIT_TERMINAL_PROMPT=0`` turns a refused token into a failure instead of a
    username prompt that would hang a driver.
    """
    env = dict(os.environ if base is None else base)
    parts = urlsplit(auth.url)
    if not parts.scheme or not parts.netloc:
        raise click.ClickException(f"Not a usable remote URL: {auth.url}")
    try:
        n = int(env.get("GIT_CONFIG_COUNT") or 0)
    except ValueError:
        n = 0
    env[f"GIT_CONFIG_KEY_{n}"] = f"http.{parts.scheme}://{parts.netloc}/.extraHeader"
    env[f"GIT_CONFIG_VALUE_{n}"] = f"Authorization: Bearer {auth.token}"
    env["GIT_CONFIG_COUNT"] = str(n + 1)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def git_available() -> bool:
    return shutil.which("git") is not None


#: ``GIT_CONFIG_COUNT`` (how the token reaches git) arrived in git 2.31.
MIN_TOKEN_GIT = (2, 31)


def require_token_capable_git() -> None:
    """Refuse before any cinna-remote operation when git cannot take the token.

    An older git silently ignores ``GIT_CONFIG_*`` and the request goes out
    unauthenticated; that 401 would read as a token problem, so say the real one.
    """
    if not git_available():
        raise click.ClickException(
            "git is not installed or not on PATH — install git (2.31 or newer)."
        )
    out = run_git(["--version"], Path.cwd()).stdout or ""
    m = re.search(r"(\d+)\.(\d+)", out)
    if m and (int(m.group(1)), int(m.group(2))) < MIN_TOKEN_GIT:
        raise click.ClickException(
            f"{out.strip()} is too old for cinna remotes: git 2.31 or newer is "
            "needed to pass the token without writing it to disk. Upgrade git."
        )


def run_git(
    args: list[str],
    cwd: Path,
    *,
    check: bool = False,
    capture: bool = True,
    auth: RemoteAuth | None = None,
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    """Run ``git <args>`` in ``cwd`` with the developer's own environment.

    Without ``auth`` there is no credential injection: the dev's git config,
    credential helpers and ``ssh-agent`` drive auth (the deploy key is
    host-side only). With ``auth`` (a cinna remote) the token travels in the
    child's environment only; the debug line logs argv, which never holds it.
    ``extra_env`` adds plain variables (``GIT_INDEX_FILE`` for a throwaway
    index).
    """
    cmd = ["git", *args]
    logger.debug(
        "exec: %s (cwd=%s%s)", " ".join(cmd), cwd, ", cinna auth" if auth else ""
    )
    env = remote_auth_env(auth) if auth else os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        capture_output=capture,
        text=True,
        check=check,
    )


def agent_remote_auth(config: CinnaConfig, workspace_root: Path, url: str) -> RemoteAuth:
    """The per-agent CLI token as a :class:`RemoteAuth`, re-minted when expired.

    An expired token is re-minted through the account workspace above the
    agent (the path ``cinna doctor`` uses); without one, the message names
    ``cinna set-token``, the per-agent refresh.
    """
    from cinna.auth import is_token_expired

    if is_token_expired(config.cli_token):
        try:
            from cinna.doctor import _make_remint

            _make_remint(config.agent_id, workspace_root)()
        except click.ClickException as exc:
            raise click.ClickException(
                "This agent's CLI token has expired and could not be re-minted "
                f"({exc.format_message()}).\nRefresh it with 'cinna set-token' "
                "from the agent's Local Development page."
            ) from exc
        from cinna.config import load_config

        config.cli_token = load_config(workspace_root).cli_token
    return RemoteAuth(url=url, token=config.cli_token)


def _layout_auth(config: CinnaConfig, workspace_root: Path) -> RemoteAuth | None:
    """Auth for the linked remote: a token for ``cinna``, ``None`` for external."""
    g = config.git
    if g is None or g.auth_hint != CINNA_AUTH_HINT or not g.repo_url:
        return None
    return agent_remote_auth(config, workspace_root, g.repo_url)


def auth_failure_hint(detail: str) -> str:
    """A sentence for git's ``401`` / ``403`` against a cinna remote, or ``""``."""
    if "403" in detail:
        return (
            "\n\nThe platform refused access to this repository (403): your "
            "account has no access to it, or only read access."
        )
    # With GIT_TERMINAL_PROMPT=0 a 401 surfaces as git failing to prompt.
    if any(
        s in detail
        for s in ("401", "Authentication failed", "could not read Username")
    ):
        return (
            "\n\nThe platform did not accept the CLI token (401). Refresh it "
            "('cinna set-token', or 'cinna account set-token' for an account "
            "workspace) and retry."
        )
    return ""


def _git_or_raise(args: list[str], cwd: Path, what: str) -> subprocess.CompletedProcess:
    result = run_git(args, cwd)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise click.ClickException(f"{what} failed:\n{detail}")
    return result


def is_linked(clone: Path) -> bool:
    """True when ``clone`` is already a git working tree (has ``.git``)."""
    return (clone / ".git").exists()


def _prefix(subdir: str | None) -> str:
    """Path prefix for the agent within the clone (``"sub/"`` or ``""``)."""
    return f"{subdir}/" if subdir else ""


# ── Linking ───────────────────────────────────────────────────────────────


@dataclass
class LinkResult:
    workspace_root: Path
    clone: Path
    subdir: str | None
    ref: str
    relayout_moved: bool = False


def _relayout_agent_dir(
    clone: Path, old_subdir: str | None, new_subdir: str | None
) -> Path:
    """Move the agent dir so it sits at the backend's ``<subdir>`` within ``clone``.

    A pure local rename (no re-download): used when a folder set up before VCS
    guessed ``subdir`` from the agent slug but the backend assigned a different
    one. Returns the new ``workspace_root``. No-op when they already match.
    """
    old_dir = clone / old_subdir if old_subdir else clone
    new_dir = clone / new_subdir if new_subdir else clone
    if old_dir.resolve() == new_dir.resolve():
        return new_dir

    logger.info("Relayout: moving agent dir %s -> %s", old_dir, new_dir)
    if new_subdir is None:
        # Collapse a guessed subdir into the clone root (repo-root agent).
        for item in list(old_dir.iterdir()):
            shutil.move(str(item), str(clone / item.name))
        old_dir.rmdir()
        return clone

    new_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old_dir), str(new_dir))
    return new_dir


def link(
    config: CinnaConfig,
    client: PlatformClient,
    workspace_root: Path,
    coords: GitCoordinates | None = None,
) -> LinkResult:
    """Run the Section-4 link sequence and persist the git layout.

    Idempotent-ish: re-running on an already-linked clone re-fetches and
    re-points the index, which is harmless. The caller is responsible for
    restarting any live Mutagen session if ``relayout_moved`` is True (the
    alpha endpoint changed).
    """
    if not git_available():
        raise click.ClickException(
            "git is not installed or not on PATH — install git to use "
            "cinna's git-versioning helpers."
        )
    if coords is None:
        coords = fetch_coordinates(client)
    if not coords.vcs_enabled:
        raise click.ClickException(
            "This agent is not git-versioned. Enable Git Versioning in the "
            "agent's settings on the platform first."
        )
    if not coords.repo_url:
        raise click.ClickException(
            "Backend reported the agent is git-versioned but returned no "
            "repo_url. Connect the git source from the platform UI first."
        )
    if config.git is None:
        # Legacy flat workspace predating the Model-A layout: never auto-convert.
        raise click.ClickException(
            "This agent is now git-versioned, but this local folder isn't laid "
            "out as a git working tree.\nRun 'cinna disconnect' here and re-sync "
            "(cinna setup / cinna agent sync) to get git support."
        )

    clone = clone_root(config, workspace_root)
    clone.mkdir(parents=True, exist_ok=True)

    # Reconcile the on-disk subdir with the backend's canonical one.
    local_subdir = config.git.subdir
    new_subdir = coords.subdir or None
    relayout_moved = False
    if (local_subdir or None) != new_subdir:
        console.warn(
            f"Backend subdir '{new_subdir}' differs from the local layout "
            f"'{local_subdir}'. Relaying out the folder locally (no re-download)."
        )
        workspace_root = _relayout_agent_dir(clone, local_subdir, new_subdir)
        relayout_moved = True

    subdir = new_subdir
    prefix = _prefix(subdir)
    ref = coords.effective_ref

    auth = None
    if coords.is_cinna:
        require_token_capable_git()
        auth = agent_remote_auth(config, workspace_root, coords.repo_url)
    elif coords.auth_hint == "ssh":
        console.status(
            f"This repo uses SSH — ensure your own SSH key can access {coords.repo_url} "
            "(the platform's deploy key is not used locally)."
        )

    # 2. git init + branch + remote (idempotent)
    if not is_linked(clone):
        _git_or_raise(["init", "-q"], clone, "git init")
        # Put HEAD on the tracked branch up front so commits land on <ref>.
        run_git(["symbolic-ref", "HEAD", f"refs/heads/{ref}"], clone)
    # The workspace tarball doesn't preserve the executable bit and Mutagen syncs
    # with portable permissions, so the local clone must not track file-mode
    # changes — otherwise every `.sh`/`.py` whose x-bit got normalized shows up as
    # a spurious mode-only modification a commit would propagate.
    run_git(["config", "core.fileMode", "false"], clone)
    if run_git(["remote", "get-url", "origin"], clone).returncode != 0:
        _git_or_raise(
            ["remote", "add", "origin", coords.repo_url], clone, "git remote add"
        )
    else:
        run_git(["remote", "set-url", "origin", coords.repo_url], clone)

    # 3. sparse cone-checkout of just this agent's subdir (multi-agent repos)
    if subdir:
        run_git(["sparse-checkout", "init", "--cone"], clone)
        _git_or_raise(
            ["sparse-checkout", "set", subdir], clone, "git sparse-checkout set"
        )

    # 4. fetch with the developer's own credentials (the CLI token for cinna)
    fetch = run_git(["fetch", "--depth=1", "origin", ref], clone, auth=auth)
    if fetch.returncode != 0:
        detail = (fetch.stderr or fetch.stdout or "").strip()
        hint = auth_failure_hint(detail) if auth else ""
        if coords.auth_hint == "ssh" and (
            "Permission denied" in detail or "publickey" in detail
        ):
            hint = (
                "\n\nThis repo uses SSH; ensure your own SSH key has access to "
                f"{coords.repo_url}."
            )
        raise click.ClickException(f"git fetch failed:\n{detail}{hint}")

    # 6. point the index at the remote tree WITHOUT touching the working files —
    #    the backend's in-flight changes then show up as ordinary uncommitted
    #    edits, ready for the dev to review and commit. --mixed, never --hard.
    _git_or_raise(["reset", "--mixed", "FETCH_HEAD"], clone, "git reset")

    # The live `GET /workspace` tarball can be a *subset* of the committed tree
    # (the backend populates git and the live workspace by different paths — e.g.
    # `plugins/**`, empty-dir `.gitkeep` markers). Those committed-but-absent
    # files would otherwise show as mass deletions and a naive commit would wipe
    # them from the repo. Restore every tracked-but-missing file so the working
    # tree is the committed baseline + only the env's genuine in-flight edits.
    restored = _restore_deleted_tracked(clone)
    if restored:
        console.status(
            f"Restored {restored} file(s) tracked in the repo but absent from "
            "the live env (kept the repo intact; only real edits stay uncommitted)."
        )

    # Force the backend-owned files (manifest + .gitignore) to their committed
    # versions even if the live copy differs; the dev never hand-edits these.
    for f in (f"{prefix}cinna.agent.json", f"{prefix}.gitignore"):
        run_git(["checkout", "--", f], clone)

    run_git(["branch", f"--set-upstream-to=origin/{ref}", ref], clone)
    _write_local_excludes(clone, workspace_root, subdir)

    # 8. persist the layout (config + registry)
    config.git = GitLayout(
        clone_path=str(clone),
        subdir=subdir,
        vcs_enabled=True,
        repo_url=coords.repo_url,
        ref=ref,
        sync_direction=coords.sync_direction,
        auth_hint=coords.auth_hint,
        last_synced_commit=coords.last_synced_commit,
    )
    save_config(config, workspace_root)
    upsert_agent_registry(
        config.agent_id,
        config.platform_url,
        config.cli_token,
        workspace_root,
        frontend_url=config.frontend_url,
        git=asdict(config.git),
    )
    _refresh_claude_md(config, workspace_root)
    return LinkResult(
        workspace_root=workspace_root,
        clone=clone,
        subdir=subdir,
        ref=ref,
        relayout_moved=relayout_moved,
    )


# CLI-only artifacts that must NEVER be committed — they don't exist on the
# backend, so the committed ``.gitignore`` (which ``link`` restores) won't mention
# them. ``.cinna/`` in particular holds the long-lived CLI token. These are
# written to ``.git/info/exclude`` (local, per-clone, not committed) as a hard
# guarantee independent of the backend gitignore. Patterns without a leading
# slash match at any depth, so one block covers every agent in a multi-agent
# clone. Mirrors ``context.generate_gitignore`` + ``bootstrap.GENERATED_WORKSPACE_FILES``.
_LOCAL_EXCLUDES = (
    ".cinna/",
    ".claude/settings.local.json",
    "CLAUDE.md",
    "CHAT_TESTING.md",
    "GIT_VERSIONING.md",
    "BUILDING_AGENT.md",
    "WEBAPP_BUILDING.md",
    "COMPLEX_AGENT_DESIGN.md",
    "REST_API_BUILDING.md",
    "AGENT_DESIGN_PATTERNS.md",
    ".mcp.json",
    "opencode.json",
    "cinna.log",
    "mutagen.yml",
    "*.conflict.*",  # Mutagen conflict copies (doc §7)
)

_EXCLUDE_MARKER = "# cinna: local-only artifacts (never commit)"


def _restore_deleted_tracked(clone: Path) -> int:
    """Restore tracked files that exist in the index but are missing on disk.

    After ``reset --mixed`` the index holds the full committed tree, but link only
    laid down the agent's workspace tarball — so two classes of committed file are
    missing on disk and read as phantom deletions:

    * **Inside the subdir** — versioned ``plugins/**`` (installed via the UI /
      remote plugin repos but kept in git so they survive if that connection is
      lost) and empty-dir ``.gitkeep`` markers the tarball omits.
    * **At the repo root** — shared files like ``README.md`` / a top-level
      ``.gitignore`` that cone-mode sparse-checkout includes but the tarball
      (workspace-only) never wrote. Left deleted, these keep the working tree
      permanently dirty and block ``git pull --rebase``.

    Restoring every *in-cone* deleted file (``ls-files --deleted`` already skips
    out-of-cone siblings via skip-worktree) keeps the repo intact and the tree
    clean, leaving only genuinely-modified files as in-flight edits. Returns the
    number of files restored.
    """
    res = run_git(["ls-files", "--deleted", "-z"], clone)
    paths = [p for p in (res.stdout or "").split("\0") if p]
    # Batch the checkout so a large plugin tree doesn't overflow argv limits.
    for i in range(0, len(paths), 400):
        run_git(["checkout", "--", *paths[i : i + 400]], clone)
    return len(paths)


def _write_local_excludes(
    clone: Path, workspace_root: Path | None = None, subdir: str | None = None
) -> None:
    """Refresh cinna's CLI-only artifacts in ``.git/info/exclude`` (idempotent).

    Combines the static artifact list with the *actual* generated prompt-reference
    docs present in this checkout (``WEBAPP_BUILDING.md``, ``REST_API_BUILDING.md``,
    …) so a new guide the CLI adds later is excluded without editing this list.
    Rewrites cinna's managed block on every call so upgrades take effect.
    """
    patterns = list(_LOCAL_EXCLUDES)
    if workspace_root is not None:
        try:
            from cinna.context import list_synced_prompt_refs

            patterns += list_synced_prompt_refs(workspace_root)
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not enumerate synced prompt refs: %s", exc)
    # De-dupe while keeping order stable.
    seen: set[str] = set()
    patterns = [p for p in patterns if not (p in seen or seen.add(p))]

    exclude = clone / ".git" / "info" / "exclude"
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text() if exclude.exists() else ""
        idx = existing.find(_EXCLUDE_MARKER)
        if idx != -1:  # drop our previously-written block (kept at EOF)
            existing = existing[:idx]
        head = existing.rstrip("\n")
        block = "\n".join([_EXCLUDE_MARKER, *patterns])
        out = (head + "\n\n" if head else "") + block + "\n"
        exclude.write_text(out)
    except OSError as exc:
        logger.debug("Could not write .git/info/exclude: %s", exc)


# ── Developer workflow wrappers (thin, fail-loud) ─────────────────────────


def _require_linked(config: CinnaConfig, workspace_root: Path) -> tuple[Path, str | None, str]:
    """Return ``(clone, subdir, ref)`` or raise if the agent isn't linked."""
    if config.git is None or not config.git.vcs_enabled:
        raise click.ClickException(
            "This agent isn't linked to git yet. Run 'cinna git link' first."
        )
    clone = clone_root(config, workspace_root)
    if not is_linked(clone):
        raise click.ClickException(
            f"No git working tree found at {clone}. Run 'cinna git link' to "
            "recreate it."
        )
    return clone, config.git.subdir, config.git.ref or "main"


def status(config: CinnaConfig, workspace_root: Path) -> str:
    """Return ``git status`` (short) scoped to the agent's subdir."""
    clone, subdir, _ = _require_linked(config, workspace_root)
    args = ["status", "--short", "--branch"]
    if subdir:
        args += ["--", subdir]
    result = run_git(args, clone)
    return (result.stdout or "").rstrip()


def commit(config: CinnaConfig, workspace_root: Path, message: str) -> bool:
    """Stage the agent's subdir and commit. Returns False when nothing changed.

    Honors the committed ``.gitignore`` — never force-adds credentials / runtime
    state.
    """
    clone, subdir, _ = _require_linked(config, workspace_root)
    add_args = ["add", "--", subdir] if subdir else ["add", "-A"]
    _git_or_raise(add_args, clone, "git add")

    diff = run_git(["diff", "--cached", "--quiet"], clone)
    if diff.returncode == 0:
        return False  # nothing staged

    _git_or_raise(["commit", "-m", message], clone, "git commit")
    return True


def push(config: CinnaConfig, workspace_root: Path) -> None:
    """Push the agent's branch to the remote (fast-forward only, fail-loud)."""
    clone, _, ref = _require_linked(config, workspace_root)
    if config.git and not GitCoordinates(
        sync_direction=config.git.sync_direction
    ).allows_push():
        raise click.ClickException(
            "This agent's git source is pull-only (sync_direction=pull); local "
            "pushes are rejected. Edit on the platform side instead."
        )
    auth = _layout_auth(config, workspace_root)
    result = run_git(["push", "origin", ref], clone, auth=auth)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if auth and ("non-fast-forward" in detail or "[rejected]" in detail):
            raise click.ClickException(
                "Push rejected — the remote advanced (someone, or the backend, "
                "pushed first).\nReconcile with 'cinna git pull', then "
                "'cinna git push' again. Never force-push a shared agent ref."
            )
        if "non-fast-forward" in detail or "[rejected]" in detail:
            raise click.ClickException(
                "Push rejected — the remote advanced (someone, or the backend, "
                f"pushed first).\nReconcile with:\n\n  git -C {clone} pull --rebase "
                f"origin {ref}\n\nthen 'cinna git push' again. Never force-push a "
                "shared agent ref."
            )
        raise click.ClickException(
            f"git push failed:\n{detail}{auth_failure_hint(detail) if auth else ''}"
        )


def pull(config: CinnaConfig, workspace_root: Path) -> None:
    """Rebase-pull the remote into the local working tree."""
    clone, _, ref = _require_linked(config, workspace_root)
    auth = _layout_auth(config, workspace_root)
    result = run_git(["pull", "--rebase", "origin", ref], clone, auth=auth)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        hint = auth_failure_hint(detail) if auth else ""
        if hint:
            raise click.ClickException(f"git pull failed:\n{detail}{hint}")
        raise click.ClickException(
            f"git pull --rebase failed:\n{detail}\n\nResolve the conflict in "
            f"{clone}, finish the rebase, and re-run."
        )


def checkout_version(
    config: CinnaConfig,
    workspace_root: Path,
    ref_or_commit: str,
    *,
    include_manifest: bool = False,
) -> list[str]:
    """Restore a past version's workspace files into the tree WITHOUT moving HEAD.

    The restored ``workspace/**`` files become ordinary uncommitted changes;
    Mutagen then mirrors them into the running container, so the live agent
    picks up that version's prompts/scripts immediately (debug / rollback,
    no commit required). With ``include_manifest`` the backend-owned
    ``cinna.agent.json`` is restored too — but note Mutagen does not sync the
    manifest (it lives outside ``workspace/``), so a manifest reload still needs
    the backend (UI "Pull" or the GitOps webhook).

    Returns the list of restored top-level paths.
    """
    clone, subdir, _ = _require_linked(config, workspace_root)
    prefix = _prefix(subdir)
    targets = [f"{prefix}workspace"]
    if include_manifest:
        targets.append(f"{prefix}cinna.agent.json")

    restored: list[str] = []
    for target in targets:
        result = run_git(["checkout", ref_or_commit, "--", target], clone)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            # A missing manifest at that ref is non-fatal.
            if target.endswith("cinna.agent.json"):
                logger.debug("manifest not present at %s: %s", ref_or_commit, detail)
                continue
            raise click.ClickException(
                f"git checkout {ref_or_commit} -- {target} failed:\n{detail}"
            )
        restored.append(target)
    return restored


def log_oneline(config: CinnaConfig, workspace_root: Path, limit: int = 15) -> str:
    """Recent commits touching the agent's subdir (for ``cinna git checkout`` UX)."""
    clone, subdir, _ = _require_linked(config, workspace_root)
    args = ["log", f"-n{limit}", "--oneline", "--no-decorate"]
    if subdir:
        args += ["--", subdir]
    return (run_git(args, clone).stdout or "").rstrip()


def unlink(config: CinnaConfig, workspace_root: Path) -> None:
    """Stop offering git helpers for this agent. Leaves ``.git`` in place.

    Flips the local link flag off (preserving the recorded layout so a later
    ``cinna git link`` reuses the same clone) and drops the registry git block.
    Never deletes the developer's repository or history.
    """
    if config.git is not None:
        config.git.vcs_enabled = False
        save_config(config, workspace_root)
    upsert_agent_registry(
        config.agent_id,
        config.platform_url,
        config.cli_token,
        workspace_root,
        frontend_url=config.frontend_url,
        git=None,
    )
    _refresh_claude_md(config, workspace_root)


def _refresh_claude_md(config: CinnaConfig, workspace_root: Path) -> None:
    """Re-render CLAUDE.md so its git-versioning pointer matches the new state.

    Best-effort: a regeneration failure must never fail a link/unlink (the guide
    pointer is advisory). Lazily imported to keep this module free of a static
    dependency on the context/template layer.
    """
    try:
        from cinna.context import regenerate_claude_md

        regenerate_claude_md(config, workspace_root)
    except Exception as exc:  # noqa: BLE001
        logger.debug("CLAUDE.md refresh after git link/unlink failed: %s", exc)
