"""Tests for `cinna chat` — session-backed agent conversations over the proxy."""

import json
from pathlib import Path

import pytest
import respx
from click.testing import CliRunner

from cinna.account import AccountConfig, save_account_config
from cinna.client import AccountClient
from cinna.errors import PlatformError
from cinna.main import cli


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def account_cfg() -> AccountConfig:
    return AccountConfig(
        platform_url="https://platform.example.com",
        frontend_url="https://ui.example.com",
        account_token="account-token-abc",
        machine_name="laptop",
    )


@pytest.fixture
def account_root(tmp_path: Path, account_cfg: AccountConfig) -> Path:
    root = tmp_path / "my-cinna"
    root.mkdir()
    save_account_config(account_cfg, root)
    (root / "agents").mkdir()
    return root


# ── Fake client driving a one-turn conversation ──────────────────────────────


class _Resp:
    """Minimal stand-in for an httpx response (download path reads .content)."""

    def __init__(self, content: bytes):
        self.content = content


class FakeClient:
    """Scripts a complete turn: user message + agent reply with an attachment."""

    def __init__(self, *, agent_msg_meta=None, agent_files=None):
        self.sent = False
        self.last_content = None
        self.last_file_ids = None
        self.interrupted = False
        self.uploaded = []
        self._agent_msg_meta = (
            agent_msg_meta
            if agent_msg_meta is not None
            else {
                "streaming_events": [
                    {
                        "type": "thinking",
                        "event_seq": 1,
                        "content": "Let me compute 2+2 step by step.",
                    },
                    {
                        "type": "tool",
                        "event_seq": 2,
                        "tool_name": "calculator",
                        "content": "calculator",
                        "metadata": {
                            "tool_id": "tool-1",
                            "tool_name": "calculator",
                            "tool_input": {"expression": "2+2"},
                        },
                    },
                    {"type": "assistant", "event_seq": 3, "content": "Here you go"},
                    {
                        "type": "attachment",
                        "event_seq": 4,
                        "metadata": {
                            "file_id": "file-9",
                            "filename": "out.txt",
                            "mime_type": "text/plain",
                            "size": 5,
                        },
                    },
                ]
            }
        )
        self._agent_files = agent_files or []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def list_account_agents(self):
        return {"data": [{"id": "agent-123", "name": "CRM Agent"}]}

    def create_session(self, agent_id, mode="conversation", title=None):
        return {"id": "sess-1", "agent_id": agent_id, "mode": mode, "title": title}

    def get_session(self, session_id):
        return {
            "id": session_id,
            "agent_id": "agent-123",
            "mode": "conversation",
            "interaction_status": "",
            "result_state": "completed",
            "result_summary": "done",
        }

    def upload_file(self, path):
        self.uploaded.append(Path(path).name)
        return {"id": "file-up", "filename": Path(path).name, "file_size": 12}

    def send_message(self, session_id, content, file_ids=None, **kwargs):
        self.sent = True
        self.last_content = content
        self.last_file_ids = file_ids
        return {"status": "ok", "session_id": session_id, "streaming": True}

    def get_messages(self, session_id, limit=100, offset=0):
        if not self.sent:
            return {"data": [], "count": 0}  # baseline
        if offset == 0:
            return {
                "data": [
                    {
                        "id": "m1",
                        "role": "user",
                        "sequence_number": 1,
                        "timestamp": "2026-06-22T00:00:00Z",
                        "content": self.last_content,
                        "message_metadata": {},
                        "files": [],
                    },
                    {
                        "id": "m2",
                        "role": "agent",
                        "sequence_number": 2,
                        "timestamp": "2026-06-22T00:00:01Z",
                        "content": "Here you go",
                        "message_metadata": self._agent_msg_meta,
                        "files": self._agent_files,
                    },
                ],
                "count": 2,
            }
        return {"data": [], "count": 0}

    def get_streaming_status(self, session_id):
        return {"is_streaming": False}

    def download_file(self, file_id):
        return _Resp(b"hello")

    def interrupt_message(self, session_id):
        self.interrupted = True
        return {}


class FakeStreamingClient(FakeClient):
    """Scripts a turn whose agent message streams in two chunks before it
    finalizes — models a long-running turn so progressive delta emission can
    be exercised (see test_chat_streams_progressive_deltas_while_in_progress)."""

    def __init__(self):
        super().__init__()
        self.poll_count = 0

    def _user_msg(self):
        return {
            "id": "m1",
            "role": "user",
            "sequence_number": 1,
            "timestamp": "2026-06-22T00:00:00Z",
            "content": self.last_content,
            "message_metadata": {},
            "files": [],
        }

    def _agent_msg(self):
        thinking = {"type": "thinking", "event_seq": 1, "content": "Step one."}
        tool = {
            "type": "tool",
            "event_seq": 2,
            "tool_name": "calculator",
            "content": "calculator",
            "metadata": {"tool_id": "tool-1", "tool_input": {"expression": "2+2"}},
        }
        assistant = {"type": "assistant", "event_seq": 3, "content": "Here you go"}
        base = {
            "id": "m2",
            "role": "agent",
            "sequence_number": 2,
            "timestamp": "2026-06-22T00:00:01Z",
            "files": [],
        }
        if self.poll_count == 1:
            return {
                **base,
                "content": "Agent is responding...",
                "message_metadata": {
                    "streaming_in_progress": True,
                    "streaming_events": [thinking],
                },
            }
        if self.poll_count == 2:
            return {
                **base,
                "content": "Agent is responding...",
                "message_metadata": {
                    "streaming_in_progress": True,
                    "streaming_events": [thinking, tool],
                },
            }
        return {
            **base,
            "content": "Here you go",
            "message_metadata": {"streaming_events": [thinking, tool, assistant]},
        }

    def get_messages(self, session_id, limit=100, offset=0):
        if not self.sent:
            return {"data": [], "count": 0}
        self.poll_count += 1
        if offset == 0:
            return {"data": [self._user_msg(), self._agent_msg()], "count": 2}
        if offset == 1:
            return {"data": [self._agent_msg()], "count": 1}
        return {"data": [], "count": 0}

    def get_streaming_status(self, session_id):
        return {"is_streaming": self.poll_count < 3}


def _ndjson(output: str) -> list[dict]:
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def test_chat_new_session_emits_ndjson(runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    fake = FakeClient()
    monkeypatch.setattr("cinna.chat.AccountClient", lambda cfg: fake)

    result = runner.invoke(cli, ["chat", "--agent", "CRM Agent", "Hello!"])
    assert result.exit_code == 0, result.output

    events = _ndjson(result.output)
    kinds = [e["event"] for e in events]
    assert kinds[0] == "session"
    assert "message" in kinds
    assert kinds[-1] == "done"

    session_ev = events[0]
    assert session_ev["session_id"] == "sess-1"
    assert session_ev["mode"] == "conversation"

    messages = [e for e in events if e["event"] == "message"]
    roles = [m["role"] for m in messages]
    assert roles == ["user", "agent"]
    assert messages[0]["content"] == "Hello!"
    assert messages[1]["content"] == "Here you go"

    done = events[-1]
    assert done["result_state"] == "completed"


def test_chat_surfaces_thinking_and_tool_trace(runner, account_root, monkeypatch):
    """The agent message carries the reasoning/tool trace under `events` — the
    thinking block and tool calls (with their input payloads), not just the
    final text."""
    monkeypatch.chdir(account_root)
    fake = FakeClient()
    monkeypatch.setattr("cinna.chat.AccountClient", lambda cfg: fake)

    result = runner.invoke(cli, ["chat", "--agent", "CRM Agent", "2+2"])
    assert result.exit_code == 0, result.output

    agent_msg = [
        e
        for e in _ndjson(result.output)
        if e["event"] == "message" and e["role"] == "agent"
    ][0]
    # Final text still present.
    assert agent_msg["content"] == "Here you go"

    events = agent_msg["events"]
    by_type = {e["type"]: e for e in events}
    # Thinking block is visible.
    assert "thinking" in by_type
    assert by_type["thinking"]["content"] == "Let me compute 2+2 step by step."
    # Tool call is visible with its name and full input payload.
    tool = by_type["tool"]
    assert tool["tool_name"] == "calculator"
    assert tool["tool_input"] == {"expression": "2+2"}
    # Attachments are not duplicated into the trace (surfaced separately).
    assert "attachment" not in by_type


def test_chat_no_events_flag_omits_trace(runner, account_root, monkeypatch):
    """--no-events drops the reasoning/tool trace, leaving just the final text."""
    monkeypatch.chdir(account_root)
    fake = FakeClient()
    monkeypatch.setattr("cinna.chat.AccountClient", lambda cfg: fake)

    result = runner.invoke(cli, ["chat", "--agent", "CRM Agent", "--no-events", "2+2"])
    assert result.exit_code == 0, result.output

    agent_msg = [
        e
        for e in _ndjson(result.output)
        if e["event"] == "message" and e["role"] == "agent"
    ][0]
    assert "events" not in agent_msg
    assert agent_msg["content"] == "Here you go"


def test_chat_streams_progressive_deltas_while_in_progress(
    runner, account_root, monkeypatch
):
    """A long-running turn (agent message stays `streaming_in_progress` across
    several polls) surfaces its trace incrementally via `delta` events instead
    of going silent until the whole message finalizes."""
    monkeypatch.chdir(account_root)
    fake = FakeStreamingClient()
    monkeypatch.setattr("cinna.chat.AccountClient", lambda cfg: fake)

    result = runner.invoke(
        cli, ["chat", "--agent", "CRM Agent", "--interval", "0", "2+2"]
    )
    assert result.exit_code == 0, result.output

    events = _ndjson(result.output)
    deltas = [e for e in events if e["event"] == "delta"]
    assert len(deltas) == 2
    assert [ev["type"] for ev in deltas[0]["events"]] == ["thinking"]
    # Second delta carries only the newly-appended event, not a repeat of
    # what was already shown.
    assert [ev["type"] for ev in deltas[1]["events"]] == ["tool"]

    agent_msg = [
        e
        for e in events
        if e["event"] == "message" and e["role"] == "agent"
    ][0]
    assert agent_msg["content"] == "Here you go"


def test_chat_downloads_agent_attachment(runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    fake = FakeClient()
    monkeypatch.setattr("cinna.chat.AccountClient", lambda cfg: fake)

    result = runner.invoke(cli, ["chat", "--agent", "CRM Agent", "Hello!"])
    assert result.exit_code == 0, result.output

    agent_msg = [
        e
        for e in _ndjson(result.output)
        if e["event"] == "message" and e["role"] == "agent"
    ][0]
    atts = agent_msg["attachments"]
    assert len(atts) == 1
    assert atts[0]["file_id"] == "file-9"
    dest = Path(atts[0]["downloaded_to"])
    assert dest.is_file()
    assert dest.read_bytes() == b"hello"
    assert dest.name == "out.txt"
    # Saved under the per-session download dir.
    assert "sess-1" in str(dest)


def test_chat_no_download_reports_file_id_only(runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    fake = FakeClient()
    monkeypatch.setattr("cinna.chat.AccountClient", lambda cfg: fake)

    result = runner.invoke(
        cli, ["chat", "--agent", "CRM Agent", "--no-download", "Hello!"]
    )
    assert result.exit_code == 0, result.output

    agent_msg = [
        e
        for e in _ndjson(result.output)
        if e["event"] == "message" and e["role"] == "agent"
    ][0]
    att = agent_msg["attachments"][0]
    assert att["file_id"] == "file-9"
    assert "downloaded_to" not in att


def test_chat_uploads_attached_file_and_sends_file_id(
    runner, account_root, tmp_path, monkeypatch
):
    monkeypatch.chdir(account_root)
    fake = FakeClient()
    monkeypatch.setattr("cinna.chat.AccountClient", lambda cfg: fake)

    upload = tmp_path / "data.csv"
    upload.write_text("a,b\n1,2\n")

    result = runner.invoke(
        cli, ["chat", "--agent", "CRM Agent", "--file", str(upload), "Check this"]
    )
    assert result.exit_code == 0, result.output
    assert fake.uploaded == ["data.csv"]
    assert fake.last_file_ids == ["file-up"]

    upload_ev = [e for e in _ndjson(result.output) if e["event"] == "upload"]
    assert upload_ev and upload_ev[0]["file_id"] == "file-up"


def test_chat_resume_uses_existing_session(runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    fake = FakeClient()
    created = {"flag": False}

    def _no_create(*a, **k):
        created["flag"] = True
        raise AssertionError("resume must not create a session")

    fake.create_session = _no_create
    monkeypatch.setattr("cinna.chat.AccountClient", lambda cfg: fake)

    result = runner.invoke(cli, ["chat", "--resume", "sess-1", "Again"])
    assert result.exit_code == 0, result.output
    assert created["flag"] is False
    session_ev = _ndjson(result.output)[0]
    assert session_ev["resumed"] is True
    assert session_ev["session_id"] == "sess-1"


def test_chat_missing_message_non_tty_errors(runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    fake = FakeClient()
    monkeypatch.setattr("cinna.chat.AccountClient", lambda cfg: fake)

    # Empty stdin (non-interactive) + no message arg → clean error, not a hang.
    result = runner.invoke(cli, ["chat", "--agent", "CRM Agent"], input="")
    assert result.exit_code != 0
    assert "No message provided" in result.output


def test_chat_requires_account_workspace(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # not an account workspace
    result = runner.invoke(cli, ["chat", "--agent", "CRM Agent", "Hi"])
    assert result.exit_code != 0


# ── Client-method tests (proxy classification + dedicated upload route) ───────


@pytest.fixture
def client(account_cfg) -> AccountClient:
    return AccountClient(account_cfg)


@respx.mock
def test_proxy_json_parses_inner_body(client):
    respx.post("https://platform.example.com/api/v1/cli/account/api-proxy").respond(
        200,
        json={"id": "sess-1", "mode": "conversation"},
        headers={"X-Cinna-Proxied": "1"},
    )
    out = client.create_session("agent-123")
    assert out["id"] == "sess-1"


@respx.mock
def test_proxy_json_raises_on_hatch_refusal(client):
    # No X-Cinna-Proxied header → the escape hatch itself refused.
    respx.post("https://platform.example.com/api/v1/cli/account/api-proxy").respond(
        403, json={"detail": "excluded path"}
    )
    with pytest.raises(PlatformError) as exc:
        client.get_messages("sess-1")
    assert "escape hatch refused" in str(exc.value)


@respx.mock
def test_download_file_surfaces_size_cap(client):
    # A hatch refusal on download (e.g. >8 MiB) → actionable PlatformError.
    respx.post("https://platform.example.com/api/v1/cli/account/api-proxy").respond(
        502, json={"detail": "Inner response exceeds the escape-hatch size limit."}
    )
    with pytest.raises(PlatformError) as exc:
        client.download_file("file-9")
    assert "8 MiB" in str(exc.value)


@respx.mock
def test_upload_file_posts_multipart_to_dedicated_route(client, tmp_path):
    route = respx.post(
        "https://platform.example.com/api/v1/cli/account/files/upload"
    ).respond(200, json={"id": "file-1", "filename": "x.txt", "file_size": 3})
    f = tmp_path / "x.txt"
    f.write_text("abc")

    out = client.upload_file(f)
    assert out["id"] == "file-1"
    assert route.called
    sent = route.calls.last.request
    # Multipart, not JSON.
    assert sent.headers["content-type"].startswith("multipart/form-data")
