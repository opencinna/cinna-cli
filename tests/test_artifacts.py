"""Tests for `cinna artifacts` — publish, share and manage artifacts.

Account-workspace calls run the real ``AccountClient`` path; only the HTTP
boundary is mocked with respx: the api-proxy (JSON verbs) and the dedicated
``/cli/account/artifacts…`` routes (multipart publish, zip download, links).
"""

import io
import json
import zipfile
from email.parser import BytesParser
from email.policy import HTTP
from pathlib import Path

import httpx
import pytest
import respx
from click.testing import CliRunner

from cinna import artifacts as artifacts_module
from cinna import console
from cinna.account import AccountConfig, save_account_config
from cinna.artifacts import (
    ArtifactMemory,
    collect_files,
    extract_archive,
    is_artifact_excluded,
    parse_expiry,
    person_only_deny_rules,
    require_person,
    write_archive,
)
from cinna.client import PROXY_MARKER_HEADER
from cinna.errors import CinnaExit
from cinna.main import cli

PLATFORM = "https://platform.example.com"
PROXY_URL = f"{PLATFORM}/api/v1/cli/account/api-proxy"
PUBLISH_URL = f"{PLATFORM}/api/v1/cli/account/artifacts"
CAPABILITIES = {
    "version": 1,
    "limits": {
        "max_revision_bytes": 50 * 1024 * 1024,
        "max_revision_files": 2000,
        "max_file_bytes": 25 * 1024 * 1024,
        "user_quota_bytes": 2 * 1024**3,
    },
}
A1 = "11111111-1111-4111-8111-111111111111"
A2 = "22222222-2222-4222-8222-222222222222"
U1 = "33333333-3333-4333-8333-333333333333"
L1 = "44444444-4444-4444-8444-444444444444"
R1 = "55555555-5555-4555-8555-555555555555"


def published(artifact_id: str, revision: int = 1, excluded=None) -> dict:
    return {
        "id": artifact_id,
        "title": "Q3 churn",
        "kind": "site",
        "visibility": "private",
        "url": f"https://ui.example.com/artifact/{artifact_id}",
        "revision_number": revision,
        "file_count": 2,
        "excluded": excluded or [],
    }


def detail(artifact_id: str = A1, **extra) -> dict:
    body = {
        "id": artifact_id,
        "title": "Q3 churn",
        "kind": "site",
        "visibility": "private",
        "is_owner": True,
        "url": f"https://ui.example.com/artifact/{artifact_id}",
        "grants": [],
        "links": [],
        "revisions": [{"id": R1, "number": 1, "file_count": 2, "size_bytes": 10}],
    }
    body.update(extra)
    return body


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def account_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "my-cinna"
    root.mkdir()
    save_account_config(
        AccountConfig(
            platform_url=PLATFORM,
            frontend_url="https://ui.example.com",
            account_token="account-token-abc",
            machine_name="laptop",
        ),
        root,
    )
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def report(tmp_path: Path) -> Path:
    """A folder the way an agent leaves it: the site, a secret and a git tree."""
    folder = tmp_path / "report"
    (folder / ".git").mkdir(parents=True)
    (folder / ".git" / "config").write_text("[core]\n")
    (folder / "index.html").write_text("<h1>Q3</h1>")
    (folder / "app.js").write_text("console.log(1)")
    (folder / ".env").write_text("SECRET=1\n")
    return folder


class FakePlatform:
    """api-proxy calls by inner (method, path), plus the dedicated publish routes."""

    def __init__(self):
        self.routes = {("GET", "artifacts/capabilities"): (200, CAPABILITIES)}
        self.calls: list[dict] = []
        self.uploads: list[dict] = []
        self.publish_status: dict[str, tuple[int, dict]] = {}

    def proxy(self, request: httpx.Request) -> httpx.Response:
        inner = json.loads(request.content)
        self.calls.append(inner)
        status, body = self.routes.get(
            (inner["method"], inner["path"]), (404, {"detail": "Not Found"})
        )
        return httpx.Response(status, json=body, headers={PROXY_MARKER_HEADER: "1"})

    def publish(self, request: httpx.Request) -> httpx.Response:
        fields, archive = _parse_multipart(request)
        target = request.url.path.removeprefix("/api/v1/cli/account/artifacts")
        self.uploads.append({"target": target, "fields": fields, "names": archive.namelist()})
        status, body = self.publish_status.get(target, (200, None))
        if body is None:
            artifact_id = target.split("/")[1] if target else A1
            revision = 2 if target else 1
            body = published(artifact_id, revision)
        return httpx.Response(status, json=body)

    def calls_to(self, method: str, path: str) -> list[dict]:
        return [c for c in self.calls if (c["method"], c["path"]) == (method, path)]


def _parse_multipart(request: httpx.Request) -> tuple[dict, zipfile.ZipFile]:
    raw = request.read()
    head = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
    message = BytesParser(policy=HTTP).parsebytes(head + raw)
    fields, archive = {}, None
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True)
        if name == "archive":
            archive = zipfile.ZipFile(io.BytesIO(payload))
        else:
            fields[name] = payload.decode()
    return fields, archive


@pytest.fixture
def platform(account_root):
    fake = FakePlatform()
    with respx.mock(assert_all_called=False) as mock:
        mock.post(PROXY_URL).mock(side_effect=fake.proxy)
        mock.post(url__regex=rf"^{PUBLISH_URL}(/[^/]+/revisions)?$").mock(
            side_effect=fake.publish
        )
        fake.mock = mock
        yield fake


def json_lines(output: str) -> list[dict]:
    return [json.loads(line) for line in output.splitlines() if line.strip()]


# ─── exclusions ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        ".git",
        "sub/.git",
        ".env",
        ".env.local",
        "sub/.env.example",
        "envs/.env.d",
        "prod.env",
        ".claude",
        ".cinna",
        "credentials",
        "app-data",
        "deep/node_modules",
        "server.pem",
        "keys/Server.KEY",
        "cert.p12",
        "credentials.json",
        "ssh/id_rsa",
        "id_ed25519.pub",
        ".netrc",
        ".npmrc",
        ".DS_Store",
        "a/__pycache__",
    ],
)
def test_mandatory_exclusions(path):
    assert is_artifact_excluded(path)


@pytest.mark.parametrize(
    "path",
    ["index.html", "README.md", "CLAUDE.md", "data/report.csv", "keyboard.js", "environment.md"],
)
def test_ordinary_files_are_kept(path):
    assert not is_artifact_excluded(path)


def test_collect_files_lists_what_it_drops(tmp_path, report):
    (report / "data").mkdir()
    (report / "data" / "rows.csv").write_text("a,b\n")
    (report / "data" / ".env.prod").write_text("X=1\n")
    (report / "node_modules" / "lib").mkdir(parents=True)
    (report / "node_modules" / "lib" / "x.js").write_text("")
    (report / "link.txt").symlink_to(tmp_path / "outside.txt")
    (report / "linked-dir").symlink_to(tmp_path)

    kept, excluded = collect_files(report)

    assert [rel for _, rel in kept] == ["app.js", "index.html", "data/rows.csv"]
    assert sorted(excluded) == sorted(
        [".git/", "node_modules/", "linked-dir", ".env", "link.txt", "data/.env.prod"]
    )


def test_collect_single_file(tmp_path):
    single = tmp_path / "summary.md"
    single.write_text("# hi")
    assert collect_files(single) == ([(single, "summary.md")], [])
    secret = tmp_path / ".env"
    secret.write_text("X=1")
    assert collect_files(secret) == ([], [".env"])


def test_archive_stores_entries_core_would_call_a_zip_bomb(tmp_path):
    zeros = tmp_path / "data.sqlite"
    zeros.write_bytes(b"\0" * 2_000_000)
    text = tmp_path / "app.js"
    text.write_text("console.log('hello');\n" * 20)
    archive = tmp_path / "out.zip"

    write_archive([(zeros, "data.sqlite"), (text, "app.js")], archive)

    with zipfile.ZipFile(archive) as zf:
        assert zf.getinfo("data.sqlite").compress_type == zipfile.ZIP_STORED
        assert zf.getinfo("app.js").compress_type == zipfile.ZIP_DEFLATED
        assert zf.read("data.sqlite") == zeros.read_bytes()


# ─── path → id memory ────────────────────────────────────────────────────────


def test_memory_round_trip(account_root, tmp_path):
    memory = ArtifactMemory(account_root, PLATFORM + "/")
    source = tmp_path / "report"
    assert memory.lookup(source) is None
    memory.remember(source, A1, "Q3")
    assert memory.lookup(source) == A1
    assert (account_root / ".cinna" / "artifacts.json").is_file()

    # Another instance never republishes into this one's id.
    assert ArtifactMemory(account_root, "https://other.example.com").lookup(source) is None

    memory.forget(A1)
    assert memory.lookup(source) is None


def test_memory_tolerates_a_broken_file(account_root, tmp_path):
    (account_root / ".cinna" / "artifacts.json").write_text("{not json")
    memory = ArtifactMemory(account_root, PLATFORM)
    assert memory.lookup(tmp_path) is None
    memory.remember(tmp_path, A1, None)
    assert memory.lookup(tmp_path) == A1


# ─── the interactive guard ───────────────────────────────────────────────────


def test_guard_refuses_without_a_person(monkeypatch):
    monkeypatch.setattr(artifacts_module, "_stdin_is_tty", lambda: True)
    require_person("share")  # a terminal, no flags: allowed

    console.set_no_input(True)
    with pytest.raises(CinnaExit, match="--no-input") as error:
        require_person("share")
    assert error.value.code == "needs_person"
    console.set_no_input(False)

    console.set_json_mode(True)
    with pytest.raises(CinnaExit, match="--json"):
        require_person("share")
    console.set_json_mode(False)
    console.set_no_input(False)

    monkeypatch.setattr(artifacts_module, "_stdin_is_tty", lambda: False)
    with pytest.raises(CinnaExit, match="not a terminal"):
        require_person("link create")


def test_deny_rules_cover_every_person_only_verb():
    assert person_only_deny_rules() == [
        "Bash(cinna artifacts share:*)",
        "Bash(cinna artifacts link create:*)",
    ]


@pytest.mark.parametrize(
    "argv",
    [
        ["artifacts", "share", A1, "--instance", "--json"],
        ["artifacts", "share", A1, "--instance", "--no-input"],
        ["--no-input", "artifacts", "share", A1, "--instance"],
        ["artifacts", "share", A1, "--instance"],
        ["artifacts", "link", "create", A1, "--json"],
        ["artifacts", "link", "create", A1],
    ],
)
def test_share_verbs_refuse_before_any_call(runner, platform, argv):
    # CliRunner's stdin is not a TTY; --yes does not exist to bypass this.
    result = runner.invoke(cli, argv)
    assert result.exit_code == 1, result.output
    assert "needs_person" in result.output or "a person" in result.output
    assert platform.calls == []


# ─── publish ─────────────────────────────────────────────────────────────────


def test_publish_then_republish_then_new(runner, platform, report, account_root):
    first = runner.invoke(cli, ["artifacts", "publish", str(report), "--title", "Q3 churn"])
    assert first.exit_code == 0, first.output
    assert "Published" in first.output
    assert "Left out 2 paths" in first.output and ".git/" in first.output
    assert "https://ui.example.com/artifact/" + A1 in first.output

    upload = platform.uploads[0]
    assert upload["target"] == ""
    assert sorted(upload["names"]) == ["app.js", "index.html"]
    assert upload["fields"]["title"] == "Q3 churn"
    assert upload["fields"]["source_kind"] == "cli"
    assert upload["fields"]["source_label"] == "laptop · report"

    again = runner.invoke(cli, ["artifacts", "publish", str(report)])
    assert again.exit_code == 0, again.output
    assert "Republished" in again.output and "revision 2" in again.output
    assert platform.uploads[1]["target"] == f"/{A1}/revisions"
    # No --title on a republish: the artifact keeps its own.
    assert "title" not in platform.uploads[1]["fields"]

    platform.publish_status[""] = (200, published(A2))
    fresh = runner.invoke(cli, ["artifacts", "publish", str(report), "--new"])
    assert fresh.exit_code == 0, fresh.output
    assert platform.uploads[2]["target"] == ""
    assert ArtifactMemory(account_root, PLATFORM).lookup(report.resolve()) == A2


def test_publish_json_ends_in_the_result_line(runner, platform, report):
    platform.publish_status[""] = (200, published(A1, excluded=["extra.key"]))
    result = runner.invoke(cli, ["artifacts", "publish", str(report), "--json"])
    assert result.exit_code == 0, result.output
    lines = json_lines(result.output)
    assert all("result" not in line for line in lines[:-1])
    final = lines[-1]
    assert final["result"] == "ok"
    assert final["artifact_id"] == A1
    assert final["action"] == "created"
    assert final["revision"] == 1
    assert final["url"].endswith(A1)
    assert final["excluded"] == [".git/", ".env", "extra.key"]


def test_republish_of_a_deleted_artifact_publishes_a_new_one(runner, platform, report, account_root):
    ArtifactMemory(account_root, PLATFORM).remember(report.resolve(), A2, "old")
    platform.publish_status[f"/{A2}/revisions"] = (404, {"detail": "Artifact not found"})
    result = runner.invoke(cli, ["artifacts", "publish", str(report)])
    assert result.exit_code == 0, result.output
    assert "no longer exists" in result.output
    assert [u["target"] for u in platform.uploads] == [f"/{A2}/revisions", ""]
    assert ArtifactMemory(account_root, PLATFORM).lookup(report.resolve()) == A1


def test_explicit_update_of_a_missing_artifact_fails(runner, platform, report):
    platform.publish_status[f"/{A2}/revisions"] = (404, {"detail": "Artifact not found"})
    result = runner.invoke(cli, ["artifacts", "publish", str(report), "--update", A2])
    assert result.exit_code == 1
    assert "Artifact not found" in result.output
    assert len(platform.uploads) == 1


def test_publish_refuses_over_the_server_limits(runner, platform, report):
    platform.routes[("GET", "artifacts/capabilities")] = (
        200,
        {"version": 1, "limits": {"max_revision_files": 1}},
    )
    result = runner.invoke(cli, ["artifacts", "publish", str(report)])
    assert result.exit_code == 1
    assert "limit of 1" in result.output
    assert platform.uploads == []


def test_publish_nothing_left(runner, platform, tmp_path):
    folder = tmp_path / "secrets"
    folder.mkdir()
    (folder / ".env").write_text("X=1")
    result = runner.invoke(cli, ["artifacts", "publish", str(folder)])
    assert result.exit_code == 1
    assert "every file is excluded" in result.output


@pytest.mark.parametrize("status", [404, 405])
def test_older_core_gets_a_clear_message(runner, platform, report, status):
    platform.routes[("GET", "artifacts/capabilities")] = (status, {"detail": "Not Found"})
    result = runner.invoke(cli, ["artifacts", "publish", str(report), "--json"])
    assert result.exit_code == 1
    final = json_lines(result.output)[-1]
    assert final["code"] == "artifacts_unsupported"
    assert "does not support artifacts" in final["detail"]
    assert platform.uploads == []


# ─── list / show / delete ────────────────────────────────────────────────────


def test_list_json_and_shared_scope(runner, platform):
    platform.routes[("GET", "artifacts")] = (200, {"data": [detail()], "count": 1})
    result = runner.invoke(cli, ["artifacts", "list", "--shared", "--json"])
    assert result.exit_code == 0, result.output
    final = json_lines(result.output)[-1]
    assert final["count"] == 1 and final["artifacts"][0]["id"] == A1
    assert platform.calls_to("GET", "artifacts")[0]["query"]["scope"] == "shared_with_me"


def test_short_ids_resolve(runner, platform):
    platform.routes[("GET", "artifacts")] = (200, {"data": [detail()], "count": 1})
    platform.routes[("GET", f"artifacts/{A1}")] = (200, detail())
    result = runner.invoke(cli, ["artifacts", "show", A1[:8]])
    assert result.exit_code == 0, result.output
    assert "Q3 churn" in result.output


def test_delete_needs_yes_under_json_and_forgets_the_path(runner, platform, account_root, tmp_path):
    memory = ArtifactMemory(account_root, PLATFORM)
    memory.remember(tmp_path, A1, "Q3")
    platform.routes[("GET", f"artifacts/{A1}")] = (200, detail())
    platform.routes[("DELETE", f"artifacts/{A1}")] = (200, {"message": "Artifact deleted"})

    refused = runner.invoke(cli, ["artifacts", "delete", A1, "--json"])
    assert refused.exit_code == 2
    assert platform.calls_to("DELETE", f"artifacts/{A1}") == []

    done = runner.invoke(cli, ["artifacts", "delete", A1, "--json", "--yes"])
    assert done.exit_code == 0, done.output
    assert json_lines(done.output)[-1] == {"result": "ok", "artifact_id": A1, "deleted": True}
    assert memory.lookup(tmp_path) is None


def test_delete_without_yes_aborts_under_no_input(runner, platform):
    platform.routes[("GET", f"artifacts/{A1}")] = (200, detail())
    result = runner.invoke(cli, ["artifacts", "delete", A1, "--no-input"])
    assert result.exit_code == 1
    assert platform.calls_to("DELETE", f"artifacts/{A1}") == []


# ─── share and links, from a terminal ────────────────────────────────────────


@pytest.fixture
def terminal(monkeypatch):
    monkeypatch.setattr(artifacts_module, "_stdin_is_tty", lambda: True)


def test_share_in_a_terminal(runner, platform, terminal):
    platform.routes[("GET", f"artifacts/{A1}")] = (200, detail())
    platform.routes[("GET", "users/search")] = (
        200,
        {"data": [{"id": "x", "email": "bob.smith@example.com"}, {"id": U1, "email": "Bob@example.com"}]},
    )
    platform.routes[("PUT", f"artifacts/{A1}/grants")] = (200, [])
    platform.routes[("PATCH", f"artifacts/{A1}")] = (200, detail(visibility="instance"))

    result = runner.invoke(
        cli, ["artifacts", "share", A1, "--instance", "--user", "bob@example.com"]
    )
    assert result.exit_code == 0, result.output
    put = platform.calls_to("PUT", f"artifacts/{A1}/grants")[0]
    assert put["json_body"] == [{"principal": {"user_id": U1}, "access": "view"}]
    assert platform.calls_to("PATCH", f"artifacts/{A1}")[0]["json_body"] == {"visibility": "instance"}


def test_share_unknown_email(runner, platform, terminal):
    platform.routes[("GET", f"artifacts/{A1}")] = (200, detail())
    platform.routes[("GET", "users/search")] = (200, {"data": []})
    result = runner.invoke(cli, ["artifacts", "share", A1, "--user", "nobody@example.com"])
    assert result.exit_code == 1
    assert "No one with the email" in result.output


def test_share_needs_an_option(runner, platform, terminal):
    result = runner.invoke(cli, ["artifacts", "share", A1])
    assert result.exit_code == 2


def test_link_create_in_a_terminal(runner, platform, terminal):
    platform.routes[("GET", f"artifacts/{A1}")] = (200, detail())
    route = platform.mock.post(f"{PUBLISH_URL}/{A1}/links").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": L1,
                "token_prefix": "abcdefgh",
                "token": "abcdefgh-secret",
                "url": "https://ui.example.com/s/abcdefgh-secret",
                "code": "1234",
                "expires_at": "2026-10-23T10:00:00Z",
                "created_at": "2026-10-16T10:00:00Z",
            },
        )
    )
    result = runner.invoke(
        cli, ["artifacts", "link", "create", A1, "--code", "--rev", "1", "--expires", "2w"]
    )
    assert result.exit_code == 0, result.output
    assert "https://ui.example.com/s/abcdefgh-secret" in result.output
    assert "1234" in result.output
    body = json.loads(route.calls[0].request.content)
    assert body["with_code"] is True
    assert body["pinned_revision_id"] == R1
    assert body["expires_at"]


def test_link_revoke_by_prefix(runner, platform):
    platform.routes[("GET", f"artifacts/{A1}")] = (
        200,
        detail(links=[{"id": L1, "token_prefix": "abcdefgh"}]),
    )
    platform.routes[("DELETE", f"artifacts/{A1}/links/{L1}")] = (200, {"message": "Link revoked"})
    result = runner.invoke(cli, ["artifacts", "link", "revoke", A1, "abcd", "--yes"])
    assert result.exit_code == 0, result.output
    assert platform.calls_to("DELETE", f"artifacts/{A1}/links/{L1}")


def test_parse_expiry():
    assert parse_expiry("never") is None
    assert parse_expiry("7d") is not None
    for bad in ("7", "0d", "soon", "-1d"):
        with pytest.raises(Exception):
            parse_expiry(bad)


# ─── download ────────────────────────────────────────────────────────────────


def _zip_bytes(entries: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def test_download_extracts(runner, platform, account_root):
    platform.mock.get(f"{PUBLISH_URL}/{A1}/download").mock(
        return_value=httpx.Response(200, content=_zip_bytes({"index.html": "<h1/>", "d/x.csv": "a"}))
    )
    result = runner.invoke(cli, ["artifacts", "download", A1])
    assert result.exit_code == 0, result.output
    target = account_root / "artifacts" / A1[:8]
    assert (target / "d" / "x.csv").read_text() == "a"


def test_extract_refuses_traversal(tmp_path):
    archive = tmp_path / "bad.zip"
    archive.write_bytes(_zip_bytes({"../evil.txt": "x"}))
    with pytest.raises(Exception, match="unsafe"):
        extract_archive(archive, tmp_path / "out")
    assert not (tmp_path / "evil.txt").exists()


def test_publish_refuses_a_secret_path_named_directly(runner, platform, account_root, tmp_path):
    creds = tmp_path / "credentials"
    creds.mkdir()
    (creds / "token.json").write_text("{}")
    for target in (account_root / ".cinna" / "account.json", account_root / ".cinna", creds, creds / "token.json"):
        result = runner.invoke(cli, ["artifacts", "publish", str(target)])
        assert result.exit_code == 1, (target, result.output)
        assert "Refusing to publish" in result.output
    assert platform.uploads == []


def test_extract_writes_nothing_when_a_later_entry_is_unsafe(tmp_path):
    archive = tmp_path / "bad.zip"
    archive.write_bytes(_zip_bytes({"a.txt": "ok", "../evil.txt": "x"}))
    with pytest.raises(Exception, match="unsafe"):
        extract_archive(archive, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_short_ids_page_through_the_listing(runner, platform):
    old = detail(A2)
    platform.routes[("GET", f"artifacts/{A2}")] = (200, old)

    def proxy(request):
        inner = json.loads(request.content)
        if (inner["method"], inner["path"]) == ("GET", "artifacts"):
            skip = int(inner["query"]["skip"])
            scope = inner["query"]["scope"]
            if scope == "mine":
                data = [detail(A1)] if skip == 0 else ([old] if skip == 1 else [])
                body = {"data": data, "count": 2}
            else:
                body = {"data": [], "count": 0}
            return httpx.Response(200, json=body, headers={PROXY_MARKER_HEADER: "1"})
        return platform.proxy(request)

    platform.mock.post(PROXY_URL).mock(side_effect=proxy)
    result = runner.invoke(cli, ["artifacts", "show", A2[:8]])
    assert result.exit_code == 0, result.output
    assert A2 in result.output


def test_remove_user_shown_by_name_only(runner, platform, terminal):
    platform.routes[("GET", f"artifacts/{A1}")] = (
        200,
        detail(grants=[{"principal": {"user_id": U1}, "display_name": "Bob Smith"}]),
    )
    platform.routes[("GET", "users/search")] = (200, {"data": [{"id": U1, "email": "bob@example.com"}]})
    platform.routes[("PUT", f"artifacts/{A1}/grants")] = (200, [])
    result = runner.invoke(cli, ["artifacts", "share", A1, "--remove-user", "bob@example.com"])
    assert result.exit_code == 0, result.output
    assert platform.calls_to("PUT", f"artifacts/{A1}/grants")[0]["json_body"] == []
