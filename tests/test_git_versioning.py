"""Tests for the git-versioning integration (Model A)."""

import os
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import pytest
from click.testing import CliRunner

from cinna import git_versioning as gv
from cinna.client import PlatformClient
from cinna.main import cli
from cinna.config import (
    GitLayout,
    clone_root,
    compute_agent_layout,
    load_config,
    save_config,
)


# ── pure helpers ──────────────────────────────────────────────────────────


def test_gitlayout_roundtrip(tmp_path, sample_config):
    sample_config.git = GitLayout(
        clone_path=str(tmp_path / "clone"),
        subdir="hr-bot",
        vcs_enabled=True,
        repo_url="git@github.com:acme/agents.git",
        ref="main",
        sync_direction="bidirectional",
        auth_hint="ssh",
        last_synced_commit="abc123",
    )
    save_config(sample_config, tmp_path)
    loaded = load_config(tmp_path)
    assert loaded.git is not None
    assert loaded.git.clone_path == str(tmp_path / "clone")
    assert loaded.git.subdir == "hr-bot"
    assert loaded.git.vcs_enabled is True
    assert loaded.git.repo_url == "git@github.com:acme/agents.git"


def test_load_config_without_git_is_none(tmp_path, sample_config):
    save_config(sample_config, tmp_path)
    assert load_config(tmp_path).git is None


def test_load_config_tolerates_unknown_git_keys(tmp_path, sample_config):
    save_config(sample_config, tmp_path)
    cfg_path = tmp_path / ".cinna" / "config.json"
    import json

    data = json.loads(cfg_path.read_text())
    data["git"] = {"clone_path": "/x", "subdir": "s", "future_field": "ignored"}
    cfg_path.write_text(json.dumps(data))
    loaded = load_config(tmp_path)
    assert loaded.git.clone_path == "/x"
    assert loaded.git.subdir == "s"


def test_compute_agent_layout_defaults_subdir_to_slug(tmp_path):
    clone, ws, subdir = compute_agent_layout(tmp_path, "hr-bot")
    assert clone == tmp_path / "hr-bot"
    assert ws == tmp_path / "hr-bot" / "hr-bot"
    assert subdir == "hr-bot"


def test_compute_agent_layout_explicit_subdir(tmp_path):
    clone, ws, subdir = compute_agent_layout(tmp_path, "hr-bot", "bots/hr")
    assert clone == tmp_path / "hr-bot"
    assert ws == tmp_path / "hr-bot" / "bots/hr"
    assert subdir == "bots/hr"


def test_clone_root_prefers_recorded_path(tmp_path, sample_config):
    sample_config.git = GitLayout(clone_path=str(tmp_path / "clone"), subdir="s")
    assert clone_root(sample_config, tmp_path / "other") == tmp_path / "clone"


def test_clone_root_falls_back_to_workspace_root(tmp_path, sample_config):
    assert clone_root(sample_config, tmp_path) == tmp_path


def test_resolve_clone_slug_free_dir(tmp_path):
    from cinna.bootstrap import resolve_clone_slug

    assert resolve_clone_slug(tmp_path, "hr-bot", "agent-123") == "hr-bot"


def test_resolve_clone_slug_same_agent_keeps_slug(tmp_path, sample_config):
    from cinna.bootstrap import resolve_clone_slug

    # A nested workspace for THIS agent already at <slug>/<sub>/.
    ws = tmp_path / "hr-bot" / "hr-bot"
    save_config(sample_config, ws)
    assert (
        resolve_clone_slug(tmp_path, "hr-bot", sample_config.agent_id) == "hr-bot"
    )


def test_resolve_clone_slug_bumps_on_other_agent(tmp_path, sample_config):
    from cinna.bootstrap import resolve_clone_slug, short_agent_hash

    # <slug>/ is owned by a DIFFERENT agent.
    other = tmp_path / "hr-bot" / "hr-bot"
    save_config(sample_config, other)  # agent-123
    bumped = resolve_clone_slug(tmp_path, "hr-bot", "agent-999")
    assert bumped == f"hr-bot-{short_agent_hash('agent-999')}"


def test_workspace_agent_id_at_flat_and_nested(tmp_path, sample_config):
    from cinna.bootstrap import workspace_agent_id_at

    assert workspace_agent_id_at(tmp_path / "nope") is None
    flat = tmp_path / "flat"
    save_config(sample_config, flat)
    assert workspace_agent_id_at(flat) == sample_config.agent_id
    nested = tmp_path / "clone"
    save_config(sample_config, nested / "sub")
    assert workspace_agent_id_at(nested) == sample_config.agent_id


def test_coordinates_from_dict_tolerant():
    c = gv.GitCoordinates.from_dict({"vcs_enabled": True, "subdir": "s", "extra": 1})
    assert c.vcs_enabled and c.subdir == "s"
    assert c.effective_ref == "main"
    assert gv.GitCoordinates.from_dict(None).vcs_enabled is False


def test_coordinates_direction_guards():
    assert gv.GitCoordinates(sync_direction="pull").allows_pull()
    assert not gv.GitCoordinates(sync_direction="pull").allows_push()
    assert gv.GitCoordinates(sync_direction="push").allows_push()
    assert gv.GitCoordinates(sync_direction=None).allows_push()  # bidirectional


# ── client 404 tolerance ──────────────────────────────────────────────────


def test_get_git_coordinates_404_is_not_versioned(sample_config):
    client = PlatformClient(sample_config)
    client._client = Mock()
    client._client.get.return_value = httpx.Response(
        404, request=httpx.Request("GET", "http://x/api/v1/cli/git-coordinates")
    )
    assert client.get_git_coordinates() == {"vcs_enabled": False}
    client.close()


def test_get_git_coordinates_200(sample_config):
    client = PlatformClient(sample_config)
    client._client = Mock()
    client._client.get.return_value = httpx.Response(
        200,
        json={"vcs_enabled": True, "repo_url": "r", "subdir": "s", "ref": "main"},
        request=httpx.Request("GET", "http://x/api/v1/cli/git-coordinates"),
    )
    out = client.get_git_coordinates()
    assert out["vcs_enabled"] and out["repo_url"] == "r"
    client.close()


# ── end-to-end against a real local git remote ────────────────────────────


@pytest.fixture
def git_env(monkeypatch):
    """Hermetic git: no user/global/system config, fixed identity."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")


def _git(args, cwd):
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


def _make_remote(tmp_path: Path, subdir: str) -> Path:
    """Create a bare remote whose ``main`` holds the canonical agent tree."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(["init", "-b", "main"], seed)
    base = seed / subdir if subdir else seed
    (base / "workspace" / "scripts").mkdir(parents=True)
    (base / "workspace" / "scripts" / "main.py").write_text("print('v1')\n")
    # A versioned plugin file — committed to git but typically absent from the
    # live workspace tarball (installed via UI / remote plugin repos).
    (base / "workspace" / "plugins" / "demo").mkdir(parents=True)
    (base / "workspace" / "plugins" / "demo" / "p1.txt").write_text("plugin v1\n")
    (base / "cinna.agent.json").write_text('{"name": "hr-bot", "v": 1}\n')
    (base / ".gitignore").write_text("workspace/credentials/\nworkspace/app-data/\n")
    # Repo-root files (shared, outside the agent subdir) — in the sparse cone but
    # never written by the workspace-only tarball.
    if subdir:
        (seed / "README.md").write_text("repo root readme\n")
        (seed / ".gitignore").write_text("*.log\n")
    _git(["add", "-A"], seed)
    _git(["commit", "-m", "seed"], seed)

    remote = tmp_path / "remote.git"
    _git(["clone", "--bare", str(seed), str(remote)], tmp_path)
    return remote


def _make_local_agent(tmp_path: Path, subdir: str, sample_config) -> tuple[Path, Path]:
    """Lay out a non-git nested checkout with a live (modified) workspace file."""
    clone, ws_root, _ = compute_agent_layout(tmp_path / "local", "hr-bot", subdir)
    (ws_root / "workspace" / "scripts").mkdir(parents=True)
    # Live, in-flight edit not yet committed anywhere.
    (ws_root / "workspace" / "scripts" / "main.py").write_text("print('LIVE')\n")
    sample_config.git = GitLayout(clone_path=str(clone), subdir=subdir)
    save_config(sample_config, ws_root)
    return clone, ws_root


def test_link_creates_tree_and_preserves_inflight(git_env, tmp_path, sample_config):
    remote = _make_remote(tmp_path, "hr-bot")
    clone, ws_root = _make_local_agent(tmp_path, "hr-bot", sample_config)
    coords = gv.GitCoordinates(
        vcs_enabled=True, repo_url=str(remote), subdir="hr-bot", ref="main"
    )

    result = gv.link(sample_config, Mock(), ws_root, coords)

    assert result.relayout_moved is False
    assert (clone / ".git").exists()
    assert gv.is_linked(clone)
    # The backend-owned manifest + .gitignore were restored from the remote.
    assert (ws_root / "cinna.agent.json").read_text().startswith('{"name": "hr-bot"')
    assert "workspace/credentials/" in (ws_root / ".gitignore").read_text()
    # The live edit survived (NOT --hard) and shows as an uncommitted change.
    assert (ws_root / "workspace" / "scripts" / "main.py").read_text() == "print('LIVE')\n"
    status = gv.status(sample_config, ws_root)
    assert "workspace/scripts/main.py" in status
    # Config + registry recorded the link.
    assert sample_config.git.vcs_enabled is True
    assert sample_config.git.repo_url == str(remote)
    from cinna.config import lookup_agent_registry

    entry = lookup_agent_registry(sample_config.agent_id)
    assert entry["git"]["repo_url"] == str(remote)


def test_link_restores_versioned_files_absent_from_live(git_env, tmp_path, sample_config):
    """Committed files missing from the live tarball (plugins, .gitkeep) are
    restored on link — not presented as deletions a commit would propagate."""
    remote = _make_remote(tmp_path, "hr-bot")
    clone, ws_root = _make_local_agent(tmp_path, "hr-bot", sample_config)
    # The local checkout (live tarball) does NOT contain the versioned plugin.
    assert not (ws_root / "workspace" / "plugins" / "demo" / "p1.txt").exists()
    coords = gv.GitCoordinates(
        vcs_enabled=True, repo_url=str(remote), subdir="hr-bot", ref="main"
    )

    gv.link(sample_config, Mock(), ws_root, coords)

    # Restored from the committed tree so it stays versioned.
    plug = ws_root / "workspace" / "plugins" / "demo" / "p1.txt"
    assert plug.read_text() == "plugin v1\n"
    status = gv.status(sample_config, ws_root)
    assert "plugins/demo/p1.txt" not in status  # NOT a pending deletion
    # The live edit still surfaces as a normal modification.
    assert "workspace/scripts/main.py" in status
    # Repo-root shared files (in the sparse cone) are restored too, so the tree
    # isn't left permanently dirty (which would block pull --rebase).
    assert (clone / "README.md").read_text() == "repo root readme\n"
    assert "README.md" not in status

    # A commit keeps the plugin in the repo (does not delete it).
    gv.commit(sample_config, ws_root, "dev edit")
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=str(clone), capture_output=True, text=True
    ).stdout
    assert "hr-bot/workspace/plugins/demo/p1.txt" in tracked


def test_link_disables_filemode_tracking(git_env, tmp_path, sample_config):
    """core.fileMode is off so x-bit-only diffs (from tarball/Mutagen) aren't tracked."""
    remote = _make_remote(tmp_path, "hr-bot")
    clone, ws_root = _make_local_agent(tmp_path, "hr-bot", sample_config)
    coords = gv.GitCoordinates(
        vcs_enabled=True, repo_url=str(remote), subdir="hr-bot", ref="main"
    )
    gv.link(sample_config, Mock(), ws_root, coords)

    fm = subprocess.run(
        ["git", "config", "core.fileMode"], cwd=str(clone), capture_output=True, text=True
    ).stdout.strip()
    assert fm == "false"
    # Flip the x-bit on a content-clean (restored) tracked file: mode-only change
    # must NOT show as a modification.
    plug = ws_root / "workspace" / "plugins" / "demo" / "p1.txt"
    assert "plugins/demo/p1.txt" not in gv.status(sample_config, ws_root)  # clean first
    os.chmod(plug, 0o755)
    assert "plugins/demo/p1.txt" not in gv.status(sample_config, ws_root)


def test_link_excludes_dynamic_prompt_ref_guides(git_env, tmp_path, sample_config):
    """Generated prompt-ref guides (e.g. REST_API_BUILDING.md) are excluded even
    though they aren't in the static list — discovered from BUILDING_AGENT.md."""
    remote = _make_remote(tmp_path, "hr-bot")
    clone, ws_root = _make_local_agent(tmp_path, "hr-bot", sample_config)
    # Simulate a generated guide referenced from the (also-generated) building doc.
    (ws_root / "BUILDING_AGENT.md").write_text("See ./REST_API_BUILDING.md\n")
    (ws_root / "REST_API_BUILDING.md").write_text("rest guide\n")
    coords = gv.GitCoordinates(
        vcs_enabled=True, repo_url=str(remote), subdir="hr-bot", ref="main"
    )

    gv.link(sample_config, Mock(), ws_root, coords)

    check = subprocess.run(
        ["git", "check-ignore", "hr-bot/REST_API_BUILDING.md"],
        cwd=str(clone),
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, "dynamic prompt-ref guide not excluded"
    assert "REST_API_BUILDING.md" not in gv.status(sample_config, ws_root)


def test_link_never_commits_cli_artifacts(git_env, tmp_path, sample_config):
    """`.cinna/` (holds the CLI token) and generated files stay out of git."""
    remote = _make_remote(tmp_path, "hr-bot")
    clone, ws_root = _make_local_agent(tmp_path, "hr-bot", sample_config)
    # Generated artifacts that the committed .gitignore won't know about.
    (ws_root / "CLAUDE.md").write_text("generated\n")
    (ws_root / ".mcp.json").write_text("{}\n")
    coords = gv.GitCoordinates(
        vcs_enabled=True, repo_url=str(remote), subdir="hr-bot", ref="main"
    )
    gv.link(sample_config, Mock(), ws_root, coords)

    status = gv.status(sample_config, ws_root)
    assert ".cinna" not in status
    assert "CLAUDE.md" not in status
    assert ".mcp.json" not in status
    # git agrees these are ignored.
    for rel in ("hr-bot/.cinna/config.json", "hr-bot/CLAUDE.md", "hr-bot/.mcp.json"):
        check = subprocess.run(
            ["git", "check-ignore", rel], cwd=str(clone), capture_output=True, text=True
        )
        assert check.returncode == 0, f"{rel} not ignored"

    # A full commit must not stage .cinna/ — guard against token leakage.
    gv.commit(sample_config, ws_root, "dev edit")
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=str(clone), capture_output=True, text=True
    ).stdout
    assert ".cinna" not in tracked
    assert "CLAUDE.md" not in tracked


def test_commit_push_pull_roundtrip(git_env, tmp_path, sample_config):
    remote = _make_remote(tmp_path, "hr-bot")
    clone, ws_root = _make_local_agent(tmp_path, "hr-bot", sample_config)
    coords = gv.GitCoordinates(
        vcs_enabled=True, repo_url=str(remote), subdir="hr-bot", ref="main"
    )
    gv.link(sample_config, Mock(), ws_root, coords)

    assert gv.commit(sample_config, ws_root, "dev edit") is True
    # Nothing further to commit now.
    assert gv.commit(sample_config, ws_root, "noop") is False
    gv.push(sample_config, ws_root)

    # The remote received the commit.
    out = subprocess.run(
        ["git", "log", "--oneline", "main"],
        cwd=str(remote),
        capture_output=True,
        text=True,
    ).stdout
    assert "dev edit" in out

    # Pull is a no-op but must succeed.
    gv.pull(sample_config, ws_root)


def test_checkout_version_restores_workspace(git_env, tmp_path, sample_config):
    remote = _make_remote(tmp_path, "hr-bot")
    clone, ws_root = _make_local_agent(tmp_path, "hr-bot", sample_config)
    coords = gv.GitCoordinates(
        vcs_enabled=True, repo_url=str(remote), subdir="hr-bot", ref="main"
    )
    gv.link(sample_config, Mock(), ws_root, coords)
    gv.commit(sample_config, ws_root, "v-live")  # commit the LIVE edit
    first = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(clone), capture_output=True, text=True
    ).stdout.strip()

    # Make + commit a second version.
    main_py = ws_root / "workspace" / "scripts" / "main.py"
    main_py.write_text("print('v3')\n")
    gv.commit(sample_config, ws_root, "v3")
    assert main_py.read_text() == "print('v3')\n"

    # Restore the first version's workspace without moving HEAD.
    restored = gv.checkout_version(sample_config, ws_root, first)
    assert restored == ["hr-bot/workspace"]
    assert main_py.read_text() == "print('LIVE')\n"
    # HEAD is still on the v3 commit (uncommitted restore).
    head_msg = subprocess.run(
        ["git", "log", "-1", "--oneline"], cwd=str(clone), capture_output=True, text=True
    ).stdout
    assert "v3" in head_msg


def test_link_relayout_on_subdir_mismatch(git_env, tmp_path, sample_config):
    """Folder set up with guessed subdir 'hr-bot' but backend says 'bots-hr'."""
    remote = _make_remote(tmp_path, "bots-hr")
    clone, ws_root = _make_local_agent(tmp_path, "hr-bot", sample_config)
    coords = gv.GitCoordinates(
        vcs_enabled=True, repo_url=str(remote), subdir="bots-hr", ref="main"
    )

    result = gv.link(sample_config, Mock(), ws_root, coords)

    assert result.relayout_moved is True
    new_ws = clone / "bots-hr"
    assert result.workspace_root == new_ws
    assert (new_ws / ".cinna" / "config.json").is_file()
    assert (new_ws / "workspace" / "scripts" / "main.py").read_text() == "print('LIVE')\n"
    assert not (clone / "hr-bot").exists()


def test_link_refuses_legacy_flat_workspace(git_env, tmp_path, sample_config):
    # config.git is None → predates the Model-A layout → instruct, never convert.
    (tmp_path / "workspace").mkdir()
    save_config(sample_config, tmp_path)
    coords = gv.GitCoordinates(vcs_enabled=True, repo_url="r", subdir="s", ref="main")
    with pytest.raises(Exception) as exc:
        gv.link(sample_config, Mock(), tmp_path, coords)
    assert "disconnect" in str(exc.value).lower()


def test_unlink_keeps_git_and_history(git_env, tmp_path, sample_config):
    remote = _make_remote(tmp_path, "hr-bot")
    clone, ws_root = _make_local_agent(tmp_path, "hr-bot", sample_config)
    coords = gv.GitCoordinates(
        vcs_enabled=True, repo_url=str(remote), subdir="hr-bot", ref="main"
    )
    gv.link(sample_config, Mock(), ws_root, coords)

    gv.unlink(sample_config, ws_root)
    assert (clone / ".git").exists()  # history preserved
    assert sample_config.git.vcs_enabled is False
    from cinna.config import lookup_agent_registry

    assert "git" not in lookup_agent_registry(sample_config.agent_id)
    # Helpers refuse once unlinked.
    with pytest.raises(Exception):
        gv.status(sample_config, ws_root)


def test_push_direction_guard_pull_only(git_env, tmp_path, sample_config):
    remote = _make_remote(tmp_path, "hr-bot")
    clone, ws_root = _make_local_agent(tmp_path, "hr-bot", sample_config)
    coords = gv.GitCoordinates(
        vcs_enabled=True,
        repo_url=str(remote),
        subdir="hr-bot",
        ref="main",
        sync_direction="pull",
    )
    gv.link(sample_config, Mock(), ws_root, coords)
    gv.commit(sample_config, ws_root, "edit")
    with pytest.raises(Exception) as exc:
        gv.push(sample_config, ws_root)
    assert "pull-only" in str(exc.value)


# ── CLI-level wiring ──────────────────────────────────────────────────────


@patch("cinna.main.PlatformClient")
def test_cli_git_status_unlinked(mock_pc, git_env, tmp_path, sample_config, monkeypatch):
    _clone, ws_root = _make_local_agent(tmp_path, "hr-bot", sample_config)
    mock_pc.return_value.__enter__.return_value.get_git_coordinates.return_value = {
        "vcs_enabled": False
    }
    monkeypatch.chdir(ws_root)
    result = CliRunner().invoke(cli, ["git", "status"])
    assert result.exit_code == 0, result.output
    assert "not git-versioned" in result.output


@patch("cinna.main.PlatformClient")
def test_cli_git_link_then_commit(mock_pc, git_env, tmp_path, sample_config, monkeypatch):
    remote = _make_remote(tmp_path, "hr-bot")
    clone, ws_root = _make_local_agent(tmp_path, "hr-bot", sample_config)
    mock_pc.return_value.__enter__.return_value.get_git_coordinates.return_value = {
        "vcs_enabled": True,
        "repo_url": str(remote),
        "subdir": "hr-bot",
        "ref": "main",
    }
    monkeypatch.chdir(ws_root)

    link = CliRunner().invoke(cli, ["git", "link"])
    assert link.exit_code == 0, link.output
    assert (clone / ".git").exists()

    commit = CliRunner().invoke(cli, ["git", "commit", "-m", "via cli"])
    assert commit.exit_code == 0, commit.output

    status = CliRunner().invoke(cli, ["git", "status"])
    assert status.exit_code == 0, status.output
    assert "Linked" in status.output


def test_link_refreshes_claude_md_pointer(git_env, tmp_path, sample_config):
    """After linking, CLAUDE.md's git-versioning pointer flips to ENABLED."""
    from cinna.context import regenerate_claude_md

    remote = _make_remote(tmp_path, "hr-bot")
    clone, ws_root = _make_local_agent(tmp_path, "hr-bot", sample_config)
    # Seed a not-yet-versioned CLAUDE.md (as setup would, before link).
    regenerate_claude_md(sample_config, ws_root)
    assert "not git-versioned" in (ws_root / "CLAUDE.md").read_text() or \
        "not** git-versioned" in (ws_root / "CLAUDE.md").read_text()

    coords = gv.GitCoordinates(
        vcs_enabled=True, repo_url=str(remote), subdir="hr-bot", ref="main"
    )
    gv.link(sample_config, Mock(), ws_root, coords)

    claude = (ws_root / "CLAUDE.md").read_text()
    assert "ENABLED for this agent" in claude
    assert (ws_root / "GIT_VERSIONING.md").exists()
