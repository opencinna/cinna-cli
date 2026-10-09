"""Tests for `cinna library` and the `cinna` remote kind in `cinna git`.

Driven against a fake cinna-core in a thread: the library API of the
team-sharing plan (README §4.2) and the real `git http-backend` behind a
bearer-token check (§4.1), so every clone, push and fetch below goes through a
real git client over smart HTTP with the token injected the way §4.4 says.

The invariant above the rest: the token reaches git through the environment
only — never argv, never `.git/config`, never output.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from cinna import git_versioning, library
from cinna.account import AccountConfig, save_account_config
from cinna.client import AccountClient
from cinna.errors import CinnaExit, CodedRefusal
from cinna.git_versioning import RemoteAuth, remote_auth_env
from cinna.main import cli

TOKEN_ANA = "tok-ana-SECRET-0001"
TOKEN_BEN = "tok-ben-SECRET-0002"
TOKEN_CY = "tok-cy-SECRET-0003"  # read-only on everything Ana shares
USERS = {TOKEN_ANA: "ana", TOKEN_BEN: "ben", TOKEN_CY: "cy"}


# ── The fake core ───────────────────────────────────────────────────────────


class FakeCore:
    def __init__(self, root: Path):
        self.repos_dir = root / "repos"
        self.repos_dir.mkdir()
        self.repos: dict[str, dict] = {}
        self.suggestions: list[dict] = []
        self.grant_puts: list[list] = []
        self.auth_headers: list[str] = []
        self.library_routes = True
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # -- state --

    def access(self, repo_id: str, user: str) -> str | None:
        repo = self.repos[repo_id]
        if user == repo["creator"]:
            return "maintain"
        return repo["access"].get(user)

    def head(self, repo_id: str) -> str | None:
        r = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "refs/heads/main"],
            cwd=self.repos_dir / f"{repo_id}.git",
            capture_output=True,
            text=True,
        )
        return r.stdout.strip() or None

    def agent_view(self, repo_id: str, user: str) -> dict:
        repo = self.repos[repo_id]
        return {
            "repo_id": repo_id,
            "slug": repo["slug"],
            "name": repo["name"],
            "description": repo["description"],
            "clone_url": f"{self.url}/git/{repo_id}.git",
            "default_branch": "main",
            "head_sha": self.head(repo_id),
            "updated_at": None,
            "access": self.access(repo_id, user),
            "maintainers": [{"user_id": repo["creator"], "display_name": repo["creator"].title()}],
            "manifest": {"id": repo["manifest_id"], "name": repo["name"]},
            "cloud_install_id": repo.get("cloud_install_id"),
            "grants": repo["grants"],
        }

    def create(self, slug, name, description, creator) -> str:
        repo_id = uuid.uuid4().hex[:12]
        bare = self.repos_dir / f"{repo_id}.git"
        subprocess.run(["git", "init", "--quiet", "--bare", str(bare)], check=True)
        subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=bare, check=True)
        subprocess.run(["git", "config", "receive.denyNonFastForwards", "true"], cwd=bare, check=True)
        subprocess.run(["git", "config", "http.receivepack", "true"], cwd=bare, check=True)
        self.repos[repo_id] = {
            "slug": slug,
            "name": name,
            "description": description,
            "creator": creator,
            "access": {"ben": "write", "cy": "read"},
            "grants": [],
            "manifest_id": None,
        }
        return repo_id

    # -- HTTP --

    def _handler(self):
        core = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _user(self) -> str | None:
                header = self.headers.get("Authorization") or ""
                core.auth_headers.append(header)
                if header.startswith("Bearer "):
                    return USERS.get(header[7:])
                return None

            def _json(self, status: int, body) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self) -> bytes:
                if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                    out = b""
                    while True:
                        size = int(self.rfile.readline().strip(), 16)
                        if size == 0:
                            self.rfile.readline()
                            return out
                        out += self.rfile.read(size)
                        self.rfile.readline()
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def _dispatch(self, method: str):
                user = self._user()
                path, _, query = self.path.partition("?")
                if user is None:
                    if path.startswith("/git/"):
                        self.send_response(401)
                        self.send_header("WWW-Authenticate", 'Basic realm="cinna"')
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    return self._json(401, {"detail": "Not authenticated"})
                if path.startswith("/git/"):
                    return self._git(method, path, query, user)
                if not core.library_routes:
                    return self._json(404, {"detail": "Not Found"})
                return self._api(method, path, user)

            def _api(self, method, path, user):
                body = self._body()
                payload = json.loads(body) if body else None
                parts = path.strip("/").split("/")
                if path == "/api/v1/library" and method == "GET":
                    agents = [
                        core.agent_view(rid, user)
                        for rid in core.repos
                        if core.access(rid, user)
                    ]
                    return self._json(200, {"agents": agents, "mirrors": []})
                if path == "/api/v1/library/agents" and method == "POST":
                    rid = core.create(payload["slug"], payload["name"], payload["description"], user)
                    return self._json(200, core.agent_view(rid, user))
                if parts[:4] == ["api", "v1", "library", "agents"] and len(parts) >= 5:
                    rid = parts[4]
                    if rid not in core.repos or not core.access(rid, user):
                        return self._json(404, {"detail": {"code": "not_found", "message": "No such library agent."}})
                    if len(parts) == 5 and method == "GET":
                        return self._json(200, core.agent_view(rid, user))
                    if parts[5:] == ["grants"] and method == "PUT":
                        if core.access(rid, user) != "maintain":
                            return self._json(403, {"detail": {"code": "not_maintainer", "message": "Maintainers only."}})
                        core.grant_puts.append(payload)
                        core.repos[rid]["grants"] = payload
                        return self._json(200, {"grants": payload})
                    if parts[5:] == ["suggestions"] and method == "POST":
                        core.suggestions.append({"repo_id": rid, "user": user, **payload})
                        return self._json(200, {"id": "sug-1", "task_id": "task-1"})
                return self._json(404, {"detail": "Not Found"})

            def _git(self, method, path, query, user):
                rid = path[len("/git/"):].split(".git", 1)[0]
                if rid not in core.repos:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                access = core.access(rid, user)
                writing = "git-receive-pack" in path or "service=git-receive-pack" in query
                if access is None or (writing and access == "read"):
                    self.send_response(403)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = self._body() if method == "POST" else b""
                env = {
                    "PATH": os.environ["PATH"],
                    "GIT_PROJECT_ROOT": str(core.repos_dir),
                    "GIT_HTTP_EXPORT_ALL": "1",
                    "PATH_INFO": path[len("/git"):],
                    "QUERY_STRING": query,
                    "REQUEST_METHOD": method,
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": str(len(body)),
                    "REMOTE_USER": user,
                    "REMOTE_ADDR": "127.0.0.1",
                }
                if self.headers.get("Content-Encoding"):
                    env["HTTP_CONTENT_ENCODING"] = self.headers["Content-Encoding"]
                if self.headers.get("Git-Protocol"):
                    env["GIT_PROTOCOL"] = self.headers["Git-Protocol"]
                out = subprocess.run(
                    ["git", "http-backend"], input=body, env=env, capture_output=True
                ).stdout
                head, _, payload = out.partition(b"\r\n\r\n")
                status = 200
                headers = []
                for line in head.decode().split("\r\n"):
                    name, _, value = line.partition(":")
                    if name.lower() == "status":
                        status = int(value.strip().split()[0])
                    elif name:
                        headers.append((name, value.strip()))
                self.send_response(status)
                for name, value in headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                self._dispatch("GET")

            def do_POST(self):
                self._dispatch("POST")

            def do_PUT(self):
                self._dispatch("PUT")

        return Handler


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def git_identity(monkeypatch, tmp_path):
    """A predictable git: no user/system config, a fixed identity."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "Tester")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "tester@example.com")
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)


@pytest.fixture
def core(tmp_path):
    server = FakeCore(tmp_path)
    yield server
    server.close()


def make_workshop(base: Path, core: FakeCore, token: str) -> Path:
    """`<base>/.cinna-kit/`, `Cloud/<host>/` (account workspace), `Local/`."""
    (base / ".cinna-kit").mkdir(parents=True)
    (base / ".cinna-kit" / "layout.json").write_text("{}")
    account = base / "Cloud" / "host"
    account.mkdir(parents=True)
    save_account_config(
        AccountConfig(
            platform_url=core.url,
            frontend_url=core.url,
            account_token=token,
            machine_name="test",
        ),
        account,
    )
    (base / "Local").mkdir()
    return base


def make_agent(local: Path, slug="invoice-reader", manifest_id="m-1") -> Path:
    folder = local / slug
    (folder / "docs").mkdir(parents=True)
    (folder / "credentials").mkdir()
    (folder / "app-data").mkdir()
    (folder / "cinna-agent.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": manifest_id,
                "slug": slug,
                "name": "Invoice Reader",
                "description": "Reads invoices.",
            },
            indent=2,
        )
    )
    (folder / "docs" / "WORKFLOW_PROMPT.md").write_text("Read the invoice.\n")
    (folder / "credentials" / ".env").write_text("API_KEY=do-not-share\n")
    (folder / "app-data" / "state.json").write_text("{}")
    (folder / "publications.json").write_text('{"publications": []}')
    return folder


@pytest.fixture
def ana(tmp_path, core):
    return make_workshop(tmp_path / "ana", core, TOKEN_ANA)


@pytest.fixture
def ben(tmp_path, core):
    return make_workshop(tmp_path / "ben", core, TOKEN_BEN)


@pytest.fixture
def cy(tmp_path, core):
    return make_workshop(tmp_path / "cy", core, TOKEN_CY)


def run(monkeypatch, cwd: Path, *args: str):
    monkeypatch.chdir(cwd)
    return CliRunner().invoke(cli, list(args), catch_exceptions=False)


def git(folder: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=folder, capture_output=True, text=True, check=True
    ).stdout.strip()


def shared(monkeypatch, workshop: Path) -> Path:
    folder = make_agent(workshop / "Local")
    result = run(monkeypatch, folder, "library", "share", "-m", "First version")
    assert result.exit_code == 0, result.output
    return folder


def got(monkeypatch, workshop: Path, slug="invoice-reader") -> Path:
    result = run(monkeypatch, workshop / "Cloud" / "host", "library", "get", slug)
    assert result.exit_code == 0, result.output
    return workshop / "Local" / slug


def assert_no_token_in(folder: Path) -> None:
    config = (folder / ".git" / "config").read_text()
    for token in USERS:
        assert token not in config


# ── Token injection (README §4.4) ───────────────────────────────────────────


class TestRemoteAuthEnv:
    def test_header_scoped_to_the_remote_origin(self):
        env = remote_auth_env(
            RemoteAuth("https://cinna.example.com/git/abc.git", "t0k"), base={}
        )
        assert env["GIT_CONFIG_COUNT"] == "1"
        assert env["GIT_CONFIG_KEY_0"] == "http.https://cinna.example.com/.extraHeader"
        assert env["GIT_CONFIG_VALUE_0"] == "Authorization: Bearer t0k"
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_appends_after_existing_env_config(self):
        base = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "a.b", "GIT_CONFIG_VALUE_0": "c"}
        env = remote_auth_env(RemoteAuth("http://127.0.0.1:9/git/x.git", "t"), base=base)
        assert env["GIT_CONFIG_COUNT"] == "2"
        assert env["GIT_CONFIG_KEY_0"] == "a.b"
        assert env["GIT_CONFIG_KEY_1"] == "http.http://127.0.0.1:9/.extraHeader"

    def test_repr_hides_the_token(self):
        assert "t0k" not in repr(RemoteAuth("https://h/git/a.git", "t0k"))

    def test_token_not_in_argv_or_logs(self, tmp_path, caplog, monkeypatch):
        calls = []
        monkeypatch.setattr(
            git_versioning.subprocess, "run",
            lambda cmd, **kw: calls.append((cmd, kw)) or subprocess.CompletedProcess(cmd, 0, "", ""),
        )
        caplog.set_level("DEBUG", logger="cinna.git")
        git_versioning.run_git(
            ["fetch", "origin"], tmp_path, auth=RemoteAuth("https://h/git/a.git", "t0k-SECRET")
        )
        cmd, kw = calls[0]
        assert "t0k-SECRET" not in " ".join(cmd)
        assert "t0k-SECRET" not in caplog.text
        assert kw["env"]["GIT_CONFIG_VALUE_0"].endswith("t0k-SECRET")


# ── share / get / update ────────────────────────────────────────────────────


class TestShareNew:
    def test_creates_repo_inits_in_place_and_pushes(self, monkeypatch, core, ana):
        folder = shared(monkeypatch, ana)
        (rid,) = core.repos
        assert core.repos[rid]["slug"] == "invoice-reader"
        assert core.head(rid) == git(folder, "rev-parse", "HEAD")
        assert git(folder, "config", "--get", library.REPO_ID_KEY) == rid
        tree = git(folder, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
        assert "cinna-agent.json" in tree and "docs/WORKFLOW_PROMPT.md" in tree
        for local_only in ("credentials/.env", "app-data/state.json", "publications.json"):
            assert local_only not in tree
        assert_no_token_in(folder)
        assert f"Bearer {TOKEN_ANA}" in core.auth_headers

    def test_up_to_date_after(self, monkeypatch, ana):
        folder = shared(monkeypatch, ana)
        result = run(monkeypatch, folder, "library", "status", "--json")
        (row,) = json.loads(result.output)["agents"]
        assert row["state"] == "up_to_date" and row["uncommitted"] == 0

    def test_secret_refused_before_anything_leaves(self, monkeypatch, core, ana):
        folder = make_agent(ana / "Local")
        (folder / "config").mkdir()
        (folder / "config" / "prod.env").write_text("X=1")
        result = run(monkeypatch, folder, "library", "share", "-m", "v1", "--json")
        assert result.exit_code == 1
        out = json.loads(result.output.strip().splitlines()[-1])
        assert out["code"] == "secret_in_tree"
        assert out["paths"] == ["config/prod.env"]
        assert not core.repos  # nothing created on the server
        assert not (folder / ".git").exists()  # the init it made is undone

    def test_foreign_remote_refused(self, monkeypatch, core, ana):
        folder = make_agent(ana / "Local")
        git(folder, "init", "-q")
        git(folder, "remote", "add", "origin", "git@github.com:me/agent.git")
        result = run(monkeypatch, folder, "library", "share", "-m", "v1", "--json")
        assert json.loads(result.output.strip().splitlines()[-1])["code"] == "foreign_remote"
        assert not core.repos


class TestGetAndUpdate:
    def test_get_clones_into_local(self, monkeypatch, core, ana, ben):
        shared(monkeypatch, ana)
        copy = got(monkeypatch, ben)
        assert (copy / "cinna-agent.json").is_file()
        assert_no_token_in(copy)
        assert "/credentials/" in (copy / ".git" / "info" / "exclude").read_text()

    def test_get_refuses_existing_folder_and_duplicate(self, monkeypatch, ana, ben):
        shared(monkeypatch, ana)
        got(monkeypatch, ben)
        account = ben / "Cloud" / "host"
        result = run(monkeypatch, account, "library", "get", "invoice-reader", "--json")
        assert json.loads(result.output.strip().splitlines()[-1])["code"] == "folder_exists"
        (ben / "Local" / "invoice-reader").rename(ben / "Local" / "renamed")
        result = run(monkeypatch, account, "library", "get", "invoice-reader", "--json")
        assert json.loads(result.output.strip().splitlines()[-1])["code"] == "already_have"

    def test_list_marks_the_local_copy(self, monkeypatch, ana, ben):
        shared(monkeypatch, ana)
        account = ben / "Cloud" / "host"
        before = json.loads(run(monkeypatch, account, "library", "list", "--json").output)
        assert before["agents"][0]["local_path"] is None
        got(monkeypatch, ben)
        after = json.loads(run(monkeypatch, account, "library", "list", "--json").output)
        assert after["agents"][0]["local_path"].endswith("invoice-reader")
        assert TOKEN_BEN not in run(monkeypatch, account, "library", "list").output

    def test_change_flows_to_teammate(self, monkeypatch, ana, ben):
        mine = shared(monkeypatch, ana)
        theirs = got(monkeypatch, ben)
        (mine / "docs" / "WORKFLOW_PROMPT.md").write_text("Read it twice.\n")
        result = run(monkeypatch, mine, "library", "share", "-m", "Twice")
        assert "Shared 1 change" in result.output

        status = json.loads(run(monkeypatch, theirs, "library", "status", "--json").output)
        assert status["agents"][0]["state"] == "behind"
        result = run(monkeypatch, theirs, "library", "update")
        assert result.exit_code == 0, result.output
        assert (theirs / "docs" / "WORKFLOW_PROMPT.md").read_text() == "Read it twice.\n"

    def test_update_refuses_uncommitted(self, monkeypatch, ana, ben):
        shared(monkeypatch, ana)
        theirs = got(monkeypatch, ben)
        (theirs / "docs" / "WORKFLOW_PROMPT.md").write_text("local edit\n")
        result = run(monkeypatch, theirs, "library", "update", "--json")
        assert result.exit_code == 1
        assert json.loads(result.output.strip().splitlines()[-1])["code"] == "uncommitted_changes"

    def test_update_all_walks_local(self, monkeypatch, ana, ben):
        mine = shared(monkeypatch, ana)
        got(monkeypatch, ben)
        make_agent(ben / "Local", slug="not-shared", manifest_id="m-2")
        (mine / "docs" / "NEW.md").write_text("new\n")
        run(monkeypatch, mine, "library", "share", "-m", "new")
        result = run(monkeypatch, ben / "Cloud" / "host", "library", "update", "--all", "--json")
        rows = json.loads(result.output)["agents"]
        assert [r["slug"] for r in rows] == ["invoice-reader"]
        assert rows[0]["updated"] == 1


class TestTwoWriters:
    def test_clean_concurrent_changes_rebase_once(self, monkeypatch, ana, ben):
        mine = shared(monkeypatch, ana)
        theirs = got(monkeypatch, ben)
        (mine / "docs" / "A.md").write_text("a\n")
        run(monkeypatch, mine, "library", "share", "-m", "A")
        (theirs / "docs" / "B.md").write_text("b\n")
        result = run(monkeypatch, theirs, "library", "share", "-m", "B", "--json")
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["rebased"] is True  # rejected once, rebased cleanly, pushed
        tree = git(theirs, "ls-tree", "-r", "--name-only", "origin/main")
        assert "docs/A.md" in tree and "docs/B.md" in tree

    def test_conflict_is_refused_with_two_options_nothing_lost(self, monkeypatch, ana, ben):
        mine = shared(monkeypatch, ana)
        theirs = got(monkeypatch, ben)
        (mine / "docs" / "WORKFLOW_PROMPT.md").write_text("Ana's way\n")
        run(monkeypatch, mine, "library", "share", "-m", "Ana")
        (theirs / "docs" / "WORKFLOW_PROMPT.md").write_text("Ben's way\n")
        result = run(monkeypatch, theirs, "library", "share", "-m", "Ben")
        assert result.exit_code == 1
        assert "Use the team's version and keep yours as a copy" in result.output
        assert "Keep working on yours and share later" in result.output
        assert (theirs / "docs" / "WORKFLOW_PROMPT.md").read_text() == "Ben's way\n"
        assert not (theirs / ".git" / "rebase-merge").exists()
        assert git(theirs, "log", "-1", "--format=%s") == "Ben"


# ── access ──────────────────────────────────────────────────────────────────


class TestReadersAndMaintainers:
    def test_reader_cannot_share_but_can_suggest(self, monkeypatch, core, ana, cy):
        shared(monkeypatch, ana)
        copy = got(monkeypatch, cy)
        (copy / "docs" / "WORKFLOW_PROMPT.md").write_text("Better.\n")
        (copy / "docs" / "IDEA.md").write_text("untracked idea\n")
        result = run(monkeypatch, copy, "library", "share", "-m", "x", "--json")
        assert json.loads(result.output.strip().splitlines()[-1])["code"] == "read_only"

        assert git(copy, "rev-parse", "HEAD") == git(copy, "rev-parse", "origin/main")
        index_before = (copy / ".git" / "index").read_bytes()
        result = run(monkeypatch, copy, "library", "suggest", "-m", "Clearer prompt")
        assert result.exit_code == 0, result.output
        (sug,) = core.suggestions
        assert sug["user"] == "cy" and sug["message"] == "Clearer prompt"
        assert "docs/IDEA.md" in sug["patch"] and "Better." in sug["patch"]
        assert (copy / ".git" / "index").read_bytes() == index_before

    def test_reader_push_is_refused_by_the_server_as_no_access(self, monkeypatch, ana, cy):
        shared(monkeypatch, ana)
        copy = got(monkeypatch, cy)
        (copy / "x.md").write_text("x")
        git(copy, "add", "-A")
        git(copy, "commit", "-qm", "x")
        account = cy / "Cloud" / "host"
        _, cfg = library.resolve_account(account)
        with pytest.raises(CinnaExit) as exc:
            library._push(copy, library._auth(cfg, library.origin_url(copy)))
        assert exc.value.code == "no_access"

    def test_bad_token_is_auth_failed(self, monkeypatch, ana):
        folder = shared(monkeypatch, ana)
        with pytest.raises(CinnaExit) as exc:
            library._fetch(folder, RemoteAuth(library.origin_url(folder), "wrong"))
        assert exc.value.code == "auth_failed"

    def test_grants_add_and_remove(self, monkeypatch, core, ana):
        shared(monkeypatch, ana)
        account = ana / "Cloud" / "host"
        result = run(
            monkeypatch, account, "library", "grants", "invoice-reader",
            "--add", "everyone=read", "--add", "ben@example.com=write", "--json",
        )
        assert result.exit_code == 0, result.output
        assert core.grant_puts[-1] == [
            {"principal": "everyone", "access": "read"},
            {"principal": {"email": "ben@example.com"}, "access": "write"},
        ]
        run(monkeypatch, account, "library", "grants", "invoice-reader", "--remove", "everyone")
        assert core.grant_puts[-1] == [
            {"principal": {"email": "ben@example.com"}, "access": "write"}
        ]

    def test_grants_change_needs_maintainer(self, monkeypatch, ana, ben):
        shared(monkeypatch, ana)
        result = run(
            monkeypatch, ben / "Cloud" / "host", "library", "grants", "invoice-reader",
            "--add", "everyone=read", "--json",
        )
        assert json.loads(result.output.strip().splitlines()[-1])["code"] == "not_maintainer"


def test_apply_grant_changes_replaces_same_principal():
    current = [{"principal": {"role": "staff"}, "access": "read"}]
    out = library.apply_grant_changes(current, [({"role": "staff"}, "write")], [])
    assert out == [{"principal": {"role": "staff"}, "access": "write"}]


def test_library_routes_missing_is_its_own_code(monkeypatch, core, ana):
    core.library_routes = False
    result = run(monkeypatch, ana / "Cloud" / "host", "library", "list", "--json")
    assert json.loads(result.output.strip().splitlines()[-1])["code"] == "library_unavailable"


def test_coded_refusal_passes_through():
    cfg = AccountConfig("https://p", "https://p", "t", "m")
    with AccountClient(cfg) as client:
        response = httpx.Response(
            409,
            json={"detail": {"code": "slug_taken", "message": "Taken."}},
            request=httpx.Request("POST", "https://p/api/v1/library/agents"),
        )
        with pytest.raises(CodedRefusal) as exc:
            client._library_response(response)
    assert exc.value.code == "slug_taken"


# ── account resolution ──────────────────────────────────────────────────────


def test_ambiguous_account_is_refused(monkeypatch, core, ana):
    second = ana / "Cloud" / "other"
    second.mkdir()
    save_account_config(
        AccountConfig("https://elsewhere.example.com", "https://e", "t", "m"), second
    )
    folder = make_agent(ana / "Local")
    monkeypatch.chdir(folder)
    with pytest.raises(CinnaExit) as exc:
        library.resolve_account(folder)
    assert exc.value.code == "ambiguous_account"
    root, _ = library.resolve_account(folder, prefer_url=f"{core.url}/git/x.git")
    assert root == ana / "Cloud" / "host"


# ── cinna agent import --from-library ───────────────────────────────────────


def test_import_from_library_uses_a_temporary_clone(monkeypatch, core, ana, ben):
    shared(monkeypatch, ana)
    (rid,) = core.repos
    core.repos[rid]["cloud_install_id"] = "agent-cloud-9"
    seen = {}

    def fake_import(path, **kwargs):
        seen["path"] = Path(path)
        seen["manifest"] = (Path(path) / "cinna-agent.json").is_file()
        seen.update(kwargs)

    monkeypatch.setattr("cinna.local_import.run_agent_import", fake_import)
    result = run(
        monkeypatch, ben / "Cloud" / "host",
        "agent", "import", "--from-library", "invoice-reader", "--update", "-y",
    )
    assert result.exit_code == 0, result.output
    assert seen["manifest"] and seen["path"].name == "invoice-reader"
    assert seen["contract_from"] == ben / "Local"
    assert seen["known_agent"] == ("agent-cloud-9", "the team library")
    assert not seen["path"].exists()  # cleaned up


def test_import_from_library_prefers_the_local_copy(monkeypatch, ana, ben):
    shared(monkeypatch, ana)
    copy = got(monkeypatch, ben)
    seen = {}
    monkeypatch.setattr(
        "cinna.local_import.run_agent_import",
        lambda path, **kw: seen.update(path=Path(path), **kw),
    )
    run(monkeypatch, ben / "Cloud" / "host", "agent", "import", "--from-library", "invoice-reader")
    assert seen["path"] == copy and copy.exists()


def test_import_needs_exactly_one_source(monkeypatch, ana):
    result = run(monkeypatch, ana / "Cloud" / "host", "agent", "import")
    assert result.exit_code == 2


# ── `cinna git` on a cinna remote ───────────────────────────────────────────


def test_git_layout_auth_uses_agent_token_only_for_cinna(sample_config, tmp_path):
    from cinna.config import GitLayout

    sample_config.git = GitLayout(
        clone_path=str(tmp_path), vcs_enabled=True,
        repo_url="https://p.example.com/git/r.git", auth_hint="cinna",
    )
    auth = git_versioning._layout_auth(sample_config, tmp_path)
    assert auth == RemoteAuth("https://p.example.com/git/r.git", sample_config.cli_token)
    sample_config.git.auth_hint = "ssh"
    assert git_versioning._layout_auth(sample_config, tmp_path) is None


def test_git_push_and_pull_on_cinna_remote(monkeypatch, core, ana, tmp_path, sample_config):
    """A cloud agent linked to a library repo pushes/pulls with its own CLI token."""
    from cinna.config import GitLayout

    shared(monkeypatch, ana)
    (rid,) = core.repos
    clone = tmp_path / "agent"
    sample_config.cli_token = TOKEN_BEN
    sample_config.git = GitLayout(
        clone_path=str(clone), vcs_enabled=True, ref="main",
        repo_url=f"{core.url}/git/{rid}.git", auth_hint="cinna",
    )
    git_versioning.run_git(
        ["clone", "-q", sample_config.git.repo_url, str(clone)], tmp_path,
        auth=RemoteAuth(sample_config.git.repo_url, TOKEN_BEN),
    )
    (clone / "docs" / "C.md").write_text("c\n")
    assert git_versioning.commit(sample_config, clone, "C")
    git_versioning.push(sample_config, clone)
    assert core.head(rid) == git(clone, "rev-parse", "HEAD")
    git_versioning.pull(sample_config, clone)
    assert_no_token_in(clone)


def test_git_status_names_the_cinna_remote(monkeypatch, workspace_root, sample_config):
    from cinna.config import GitLayout, save_config

    git(workspace_root, "init", "-q")
    sample_config.git = GitLayout(
        clone_path=str(workspace_root), vcs_enabled=True, ref="main",
        repo_url="https://p.example.com/git/r.git", auth_hint="cinna",
    )
    save_config(sample_config, workspace_root)
    result = run(monkeypatch, workspace_root, "git", "status")
    assert "cinna library remote" in result.output
    assert "p.example.com" not in result.output
