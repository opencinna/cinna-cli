"""Tests for `cinna delegation` — durable cross-agent delegations.

Account-workspace calls run the real ``AccountClient`` path; only the HTTP
boundary (the account api-proxy, the cloud agent route) is mocked with respx.
"""

import json
from pathlib import Path

import httpx
import pytest
import respx
from click.testing import CliRunner

from cinna.account import AccountConfig, save_account_config
from cinna.client import PROXY_MARKER_HEADER
from cinna.main import cli

PLATFORM = "https://platform.example.com"
PROXY_URL = f"{PLATFORM}/api/v1/cli/account/api-proxy"
CLOUD_URL = "https://backend.test/api/v1/agent/tasks/current/delegation-result"
CAPABILITIES = {"version": 1, "metadata": True, "structured_result": True, "reply": True}


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


class FakePlatform:
    """Answers api-proxy calls by inner (method, path); records every call."""

    def __init__(self, routes: dict | None = None):
        self.routes = {("GET", "tasks/delegation-capabilities"): (200, CAPABILITIES)}
        self.routes.update(routes or {})
        self.calls: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        inner = json.loads(request.content)
        self.calls.append(inner)
        status, body = self.routes.get((inner["method"], inner["path"]), (404, {"detail": "Not Found"}))
        return httpx.Response(status, json=body, headers={PROXY_MARKER_HEADER: "1"})

    def calls_to(self, method: str, path: str) -> list[dict]:
        return [c for c in self.calls if (c["method"], c["path"]) == (method, path)]


@pytest.fixture
def platform(account_root):
    fake = FakePlatform()
    with respx.mock(assert_all_called=False) as mock:
        mock.post(PROXY_URL).mock(side_effect=fake)
        yield fake


CREATE = ["delegation", "create", "--id", "research", "--target", "agent", "--title", "Research", "--brief", "Find facts"]


# ─── create ──────────────────────────────────────────────────────────────────


def test_create_preserves_retry_identity(runner, platform):
    platform.routes[("POST", "tasks/")] = (200, {"id": "child", "auto_execute": True, "status": "new"})
    args = [*CREATE, "--execute"]
    assert runner.invoke(cli, args).exit_code == 0
    assert runner.invoke(cli, args).exit_code == 0
    first, second = platform.calls_to("POST", "tasks/")
    assert first == second
    body = first["json_body"]
    assert body["auto_execute"] is True
    metadata = body["delegation_metadata"]
    assert metadata["requester_key"] == "research"
    assert metadata["depth"] == 1 and metadata["root"] == metadata["id"]


def test_create_identity_is_stable_across_versions(runner, platform):
    """external_ref / delegation id must match tasks created by earlier builds."""
    import hashlib
    from uuid import NAMESPACE_URL, uuid5

    platform.routes[("POST", "tasks/")] = (200, {"id": "child"})
    assert runner.invoke(cli, CREATE).exit_code == 0
    body = platform.calls_to("POST", "tasks/")[0]["json_body"]
    identity = json.dumps(["agent", "research"])
    assert body["external_ref"] == hashlib.sha256(identity.encode()).hexdigest()
    assert body["delegation_metadata"]["id"] == str(uuid5(NAMESPACE_URL, identity))


def test_create_human_output_shows_task_id(runner, platform):
    platform.routes[("POST", "tasks/")] = (200, {"id": "task-123", "auto_execute": False, "status": "new"})
    result = runner.invoke(cli, CREATE)
    assert result.exit_code == 0, result.output
    assert "task-123" in result.output
    assert "cinna delegation status task-123" in result.output
    assert "Executes: no" in result.output
    assert "{" not in result.output  # no raw JSON dump


def test_create_human_output_tolerates_sparse_body(runner, platform):
    platform.routes[("POST", "tasks/")] = (200, {})
    result = runner.invoke(cli, CREATE)
    assert result.exit_code == 0, result.output


def test_create_json_output_nests_backend_body(runner, platform):
    task = {"id": "task-123", "auto_execute": False, "result": "clobber?"}
    platform.routes[("POST", "tasks/")] = (200, task)
    result = runner.invoke(cli, [*CREATE, "--json"])
    assert result.exit_code == 0, result.output
    lines = [json.loads(line) for line in result.output.strip().splitlines()]
    assert lines[-1] == {"result": "ok", "delegation": task}


def test_create_requires_stable_key_and_limits_depth(runner):
    assert runner.invoke(cli, ["delegation", "create", "--target", "a", "--title", "T", "--brief", "B"]).exit_code == 2
    assert runner.invoke(cli, [*CREATE, "--depth", "3"]).exit_code == 2


def test_create_depth_one_forbids_root(runner, platform):
    result = runner.invoke(cli, [*CREATE, "--root", "r"])
    assert result.exit_code == 2
    assert "--root" in result.output
    assert platform.calls == []


def test_create_depth_two_requires_root(runner, platform):
    result = runner.invoke(cli, [*CREATE, "--depth", "2"])
    assert result.exit_code == 2
    assert "--root" in result.output
    assert platform.calls == []


def test_create_depth_two_with_root(runner, platform):
    platform.routes[("POST", "tasks/")] = (200, {"id": "child"})
    assert runner.invoke(cli, [*CREATE, "--depth", "2", "--root", "root-id"]).exit_code == 0
    metadata = platform.calls_to("POST", "tasks/")[0]["json_body"]["delegation_metadata"]
    assert metadata["depth"] == 2 and metadata["root"] == "root-id"


# ─── capability negotiation ─────────────────────────────────────────────────


@pytest.mark.parametrize("status_code", [404, 405])
def test_capabilities_missing_is_a_clean_message(runner, platform, status_code):
    platform.routes[("GET", "tasks/delegation-capabilities")] = (status_code, {"detail": "Not Found"})
    result = runner.invoke(cli, ["delegation", "status", "task"])
    assert result.exit_code == 1
    assert "does not support durable delegations" in result.output
    assert "Traceback" not in result.output
    assert platform.calls_to("GET", "tasks/task/detail") == []


@pytest.mark.parametrize("body", [{"version": 2}, {}, ["version", 1]])
def test_capabilities_version_mismatch_is_a_clean_message(runner, platform, body):
    platform.routes[("GET", "tasks/delegation-capabilities")] = (200, body)
    result = runner.invoke(cli, CREATE)
    assert result.exit_code == 1
    assert "does not support durable delegations" in result.output
    assert platform.calls_to("POST", "tasks/") == []


def test_capabilities_other_errors_propagate(runner, platform):
    platform.routes[("GET", "tasks/delegation-capabilities")] = (500, {"detail": "boom"})
    result = runner.invoke(cli, ["delegation", "status", "task"])
    assert result.exit_code == 12
    assert "boom" in result.output


# ─── status / report / reply (account workspace) ────────────────────────────


def test_account_report_reply_and_status_use_structured_routes(runner, platform):
    platform.routes[("PUT", "tasks/task/delegation-result")] = (200, {"id": "result"})
    platform.routes[("POST", "tasks/task/delegation-reply")] = (200, {"delivered": True})
    platform.routes[("GET", "tasks/task/detail")] = (200, {"id": "task"})

    result = runner.invoke(cli, ["delegation", "report", "task", "--status", "blocked", "--summary", "Input", "--question", "Which?", "--audience", "user"])
    assert result.exit_code == 0, result.output
    assert platform.calls_to("PUT", "tasks/task/delegation-result")[0]["json_body"]["audience"] == "user"
    assert "Reported" in result.output

    result = runner.invoke(cli, ["delegation", "reply", "task", "--result-id", "question", "--message", "Answer"])
    assert result.exit_code == 0, result.output
    assert platform.calls_to("POST", "tasks/task/delegation-reply")[0]["json_body"] == {"result_id": "question", "message": "Answer"}
    assert "Reply delivered" in result.output

    assert runner.invoke(cli, ["delegation", "status", "task"]).exit_code == 0
    assert len(platform.calls_to("GET", "tasks/task/detail")) == 1


def test_status_human_output_shows_open_question(runner, platform):
    platform.routes[("GET", "tasks/task/detail")] = (200, {
        "id": "task", "title": "Research", "status": "blocked",
        "delegation_result": {"id": "res-9", "status": "blocked", "summary": "Need input", "question": "Which region?", "audience": "user"},
    })
    result = runner.invoke(cli, ["delegation", "status", "task"])
    assert result.exit_code == 0, result.output
    for text in ("blocked", "Need input", "Which region?", "res-9", "--result-id res-9"):
        assert text in result.output


def test_status_json_output(runner, platform):
    detail = {"id": "task", "status": "completed", "delegation_result": None}
    platform.routes[("GET", "tasks/task/detail")] = (200, detail)
    result = runner.invoke(cli, ["delegation", "status", "task", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output.strip().splitlines()[-1]) == {"result": "ok", "delegation": detail}


def test_blocked_report_requires_question(runner, platform):
    result = runner.invoke(cli, ["delegation", "report", "task", "--status", "blocked", "--summary", "Stuck"])
    assert result.exit_code == 2
    assert "--question" in result.output
    assert platform.calls == []


# ─── artifacts ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("artifact", [
    "not json",
    '["a list"]',
    '{"kind": "link", "name": "Doc"}',
    '{"kind": "", "name": "Doc", "ref": "https://x.test/a"}',
    '{"kind": "link", "name": 5, "ref": "https://x.test/a"}',
    '{"kind": "file", "name": "Doc", "ref": "file:///tmp/a.txt"}',
    '{"kind": "file", "name": "Doc", "ref": "/tmp/a.txt"}',
])
def test_invalid_artifacts_are_rejected(runner, platform, artifact):
    result = runner.invoke(cli, ["delegation", "report", "task", "--status", "done", "--summary", "S", "--artifact", artifact])
    assert result.exit_code == 2
    assert "--artifact" in result.output
    assert platform.calls == []


def test_valid_artifact_passes_through(runner, platform):
    platform.routes[("PUT", "tasks/task/delegation-result")] = (200, {"id": "result"})
    artifact = {"kind": "link", "name": "Report", "ref": "https://files.test/report.pdf"}
    result = runner.invoke(cli, ["delegation", "report", "task", "--status", "done", "--summary", "S", "--artifact", json.dumps(artifact)])
    assert result.exit_code == 0, result.output
    assert platform.calls_to("PUT", "tasks/task/delegation-result")[0]["json_body"]["artifacts"] == [artifact]


# ─── cloud report (no TASK_ID) ──────────────────────────────────────────────


@pytest.fixture
def cloud_env(monkeypatch, tmp_path):
    context = tmp_path / "context.json"
    context.write_text(json.dumps({"backend_session_id": "session"}))
    monkeypatch.setenv("CINNA_SESSION_CONTEXT_PATH", str(context))
    monkeypatch.setenv("AGENT_AUTH_TOKEN", "env-test-token")
    monkeypatch.setenv("ENV_ID", "env")
    monkeypatch.setenv("BACKEND_URL", "https://backend.test/")
    return context


CLOUD_REPORT = ["delegation", "report", "--status", "done", "--summary", "Finished"]


@respx.mock
def test_cloud_report_uses_env_token_and_current_session(runner, cloud_env):
    route = respx.post(CLOUD_URL).respond(200, json={"id": "result"})
    result = runner.invoke(cli, CLOUD_REPORT)
    assert result.exit_code == 0, result.output
    request = route.calls.last.request
    assert json.loads(request.content)["source_session_id"] == "session"
    assert request.headers["Authorization"] == "Bearer env-test-token"
    assert request.headers["X-Agent-Env-Id"] == "env"
    assert "Reported" in result.output


@respx.mock
def test_cloud_report_does_not_follow_redirects(runner, cloud_env):
    respx.post(CLOUD_URL).respond(307, headers={"Location": "https://elsewhere.test/"})
    elsewhere = respx.post("https://elsewhere.test/")
    result = runner.invoke(cli, CLOUD_REPORT)
    assert result.exit_code == 1
    assert not elsewhere.called


@respx.mock
def test_cloud_report_accepts_201(runner, cloud_env):
    respx.post(CLOUD_URL).respond(201, json={"id": "result"})
    result = runner.invoke(cli, [*CLOUD_REPORT, "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output.strip().splitlines()[-1]) == {"result": "ok", "delegation": {"id": "result"}}


@respx.mock
@pytest.mark.parametrize("content", [b"", b"<html>ok</html>"])
def test_cloud_report_tolerates_unreadable_success_body(runner, cloud_env, content):
    respx.post(CLOUD_URL).respond(200, content=content)
    result = runner.invoke(cli, CLOUD_REPORT)
    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output


@respx.mock
def test_cloud_report_non_2xx_is_a_platform_error(runner, cloud_env):
    respx.post(CLOUD_URL).respond(422, json={"detail": "This report belongs to an earlier execution"})
    result = runner.invoke(cli, CLOUD_REPORT)
    assert result.exit_code == 1
    assert "Platform error (422): This report belongs to an earlier execution" in result.output


@respx.mock
def test_cloud_report_non_2xx_json_mode(runner, cloud_env):
    respx.post(CLOUD_URL).respond(503, text="<html>down</html>")
    result = runner.invoke(cli, [*CLOUD_REPORT, "--json"])
    assert result.exit_code == 12
    error = json.loads(result.output.strip().splitlines()[-1])
    assert error["http_status"] == 503


@pytest.mark.parametrize("missing", ["ENV_ID", "AGENT_AUTH_TOKEN", "BACKEND_URL"])
def test_cloud_report_requires_env(runner, cloud_env, monkeypatch, missing):
    monkeypatch.delenv(missing)
    result = runner.invoke(cli, CLOUD_REPORT)
    assert result.exit_code == 2
    assert missing in result.output


@respx.mock
@pytest.mark.parametrize("content", [
    "not json",
    json.dumps(["session"]),
    json.dumps({}),
    json.dumps({"backend_session_id": ""}),
    json.dumps({"backend_session_id": 42}),
])
def test_cloud_report_bad_context_file(runner, cloud_env, content):
    route = respx.post(CLOUD_URL)
    cloud_env.write_text(content)
    result = runner.invoke(cli, CLOUD_REPORT)
    assert result.exit_code == 1
    assert "No current cloud task session is available." in result.output
    assert not route.called


def test_cloud_report_missing_context_file(runner, cloud_env):
    cloud_env.unlink()
    result = runner.invoke(cli, CLOUD_REPORT)
    assert result.exit_code == 1
    assert "No current cloud task session is available." in result.output
