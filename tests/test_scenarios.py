"""Tests for `cinna agent scenarios` — the Say | Expect parser and the runner."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from cinna.account import AccountConfig, save_account_config
from cinna.config import CinnaConfig, save_config
from cinna.main import cli
from cinna.scenarios import load_scenarios, parse_scenarios


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def account_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "my-cinna"
    root.mkdir()
    save_account_config(
        AccountConfig(
            platform_url="https://platform.example.com",
            frontend_url="https://ui.example.com",
            account_token="account-token-abc",
            machine_name="laptop",
        ),
        root,
    )
    (root / "agents").mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setenv("COLUMNS", "240")
    return root


WHO_IS_AWAY = """# Who is away — protects the `who-is-away` skill

## What must be true
1. Dates only — never the reason for an absence.

## Fixtures (verified 2026-09-14)
| Record | What it proves |
|---|---|
| "Sam", employee 497 | has booked leave next week |

## Say | Expect
| Say | Expect |
|---|---|
| `is sam around tmrw` | "out of office until <date>", no reason, one skill call |
| whats our q3 revenue | one-line redirect; no tool call |
| `grep a \\| b` | the pipe stays in the message |

## Traps
| Say | Expect |
|---|---|
| `not a case` | a table under another heading is context |
"""


def _write(folder: Path, name: str, text: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


# ── Parser ───────────────────────────────────────────────────────────────────


def test_parse_reads_backticked_plain_and_escaped_pipe_says():
    cases = parse_scenarios(WHO_IS_AWAY, "who_is_away.md")

    assert [c.say for c in cases] == [
        "is sam around tmrw",
        "whats our q3 revenue",
        "grep a | b",
    ]
    assert cases[0].expect == '"out of office until <date>", no reason, one skill call'
    assert [c.row for c in cases] == [1, 2, 3]
    assert cases[0].line == 14
    assert cases[0].file == "who_is_away.md"


def test_parse_ignores_tables_under_other_headings():
    says = [c.say for c in parse_scenarios(WHO_IS_AWAY, "x.md")]
    assert "not a case" not in says
    assert not any("Sam" in s for s in says)


def test_parse_finds_columns_by_header_and_ignores_extra_columns():
    text = (
        "## Say | Expect\n"
        "| # | Expect | Say | Notes |\n"
        "|:--|:---:|---|---|\n"
        "| 1 | a redirect | `hello there` | extra |\n"
    )
    [case] = parse_scenarios(text, "x.md")
    assert (case.say, case.expect) == ("hello there", "a redirect")


def test_parse_accepts_a_table_without_outer_pipes_and_br_line_breaks():
    text = (
        "## Say \\| Expect\n"
        "\n"
        "Say | Expect\n"
        "--- | ---\n"
        "`first line<br>second` | two lines\n"
    )
    [case] = parse_scenarios(text, "x.md")
    assert case.say == "first line\nsecond"


def test_parse_returns_none_when_there_is_no_say_expect_table():
    assert parse_scenarios("# Notes\n\nNo table here.\n", "x.md") is None
    assert parse_scenarios("## Say | Expect\n\nTo do.\n", "x.md") is None
    assert (
        parse_scenarios("## Examples\n| Say | Expect |\n|---|---|\n| `hi` | hello |\n", "x.md")
        is None
    )


def test_parse_skips_a_heading_inside_a_code_fence():
    text = (
        "```markdown\n## Say | Expect\n| Say | Expect |\n|---|---|\n| `quoted` | example |\n```\n"
    )
    assert parse_scenarios(text, "x.md") is None


def test_load_excludes_readme_and_flags_a_file_without_a_table(tmp_path):
    folder = tmp_path / "test_scenarios"
    _write(folder, "README.md", WHO_IS_AWAY)
    _write(folder, "who_is_away.md", WHO_IS_AWAY)
    _write(folder, "notes.md", "# Notes\n")

    files = load_scenarios(folder)

    assert [f.path.name for f in files] == ["notes.md", "who_is_away.md"]
    assert files[0].has_table is False
    assert len(files[1].cases) == 3


# ── list ─────────────────────────────────────────────────────────────────────


def test_scenarios_list_counts_rows_and_flags_files_without_a_table(runner, account_root, tmp_path):
    folder = tmp_path / "sc"
    _write(folder, "who_is_away.md", WHO_IS_AWAY)
    _write(folder, "notes.md", "# Notes\n")

    result = runner.invoke(cli, ["agent", "scenarios", "list", "CRM Agent", "--path", str(folder)])

    assert result.exit_code == 0, result.output
    assert "3 cases" in result.output
    assert "no Say | Expect table" in result.output
    assert "3 cases across 1 file —" in result.output


def test_scenarios_list_reads_the_synced_workspace_copy(runner, account_root):
    agent_dir = account_root / "agents" / "crm-agent" / "crm-agent"
    agent_dir.mkdir(parents=True)
    save_config(
        CinnaConfig(
            platform_url="https://platform.example.com",
            cli_token="child",
            agent_id="agent-123",
            agent_name="CRM Agent",
            environment_id="env-1",
            template="general-env",
        ),
        agent_dir,
    )
    _write(agent_dir / "workspace" / "docs" / "test_scenarios", "who_is_away.md", WHO_IS_AWAY)

    result = runner.invoke(cli, ["agent", "scenarios", "list", "crm-agent"])

    assert result.exit_code == 0, result.output
    assert "who_is_away.md" in result.output


def test_scenarios_list_names_sync_or_path_for_an_unsynced_agent(runner, account_root):
    result = runner.invoke(cli, ["agent", "scenarios", "list", "crm-agent"])
    assert result.exit_code != 0
    assert "cinna agent sync crm-agent" in result.output
    assert "--path" in result.output


# ── run ──────────────────────────────────────────────────────────────────────


class FakeClient:
    """One reply per session; a Say containing 'slow' never settles."""

    def __init__(self):
        self.sessions: list[dict] = []
        self.sent: dict[str, str] = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def list_account_agents(self):
        return {"data": [{"id": "agent-123", "name": "CRM Agent", "can_build": True}]}

    def get_agent(self, agent_id):
        return {"id": agent_id, "active_environment_id": "env-1"}

    def get_environment(self, environment_id):
        return {
            "id": environment_id,
            "status": "running",
            "agent_sdk_conversation": "claude-code/anthropic",
            "model_override_conversation": None,
            "model_health": {
                "has_warning": False,
                "modes": [{"mode": "conversation", "model": "haiku", "status": "ok"}],
            },
        }

    def create_session(self, agent_id, mode="conversation", title=None):
        sid = f"sess-{len(self.sessions) + 1}"
        self.sessions.append({"id": sid, "mode": mode, "title": title})
        return {"id": sid, "agent_id": agent_id, "mode": mode}

    def send_message(self, session_id, content, **kwargs):
        self.sent[session_id] = content
        return {"status": "ok", "session_id": session_id, "streaming": True}

    def get_messages(self, session_id, limit=100, offset=0):
        content = self.sent.get(session_id)
        messages = []
        if content is not None:
            messages.append(
                {"id": f"{session_id}-u", "role": "user", "content": content, "message_metadata": {}}
            )
            if "slow" not in content:
                messages.append(
                    {
                        "id": f"{session_id}-a",
                        "role": "agent",
                        "content": f"re: {content}",
                        "message_metadata": {
                            "streaming_events": [
                                {
                                    "type": "tool",
                                    "event_seq": 1,
                                    "tool_name": "Bash",
                                    "metadata": {
                                        "tool_input": {"command": "python run.py --who sam\necho done"}
                                    },
                                }
                            ]
                        },
                        "files": [],
                    }
                )
        return {"data": messages[offset:], "count": len(messages)}

    def get_streaming_status(self, session_id):
        return {"is_streaming": "slow" in (self.sent.get(session_id) or "")}

    def get_session(self, session_id):
        return {"id": session_id}

    def interrupt_message(self, session_id):
        return {}


@pytest.fixture
def fake(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr("cinna.scenarios.AccountClient", lambda cfg: client)
    monkeypatch.setattr("cinna.chat.time.sleep", lambda s: None)
    return client


def _ndjson(output: str) -> list[dict]:
    return [json.loads(line) for line in output.splitlines() if line.startswith("{")]


def test_scenarios_run_sends_each_row_in_its_own_session(runner, account_root, tmp_path, fake):
    folder = tmp_path / "sc"
    _write(folder, "who_is_away.md", WHO_IS_AWAY)

    result = runner.invoke(
        cli, ["agent", "scenarios", "run", "CRM Agent", "--path", str(folder), "--yes"]
    )

    assert result.exit_code == 0, result.output
    assert [s["id"] for s in fake.sessions] == ["sess-1", "sess-2", "sess-3"]
    assert all(s["mode"] == "conversation" for s in fake.sessions)
    assert list(fake.sent.items()) == [
        ("sess-1", "is sam around tmrw"),
        ("sess-2", "whats our q3 revenue"),
        ("sess-3", "grep a | b"),
    ]
    assert "conversation model: haiku" in result.output
    assert "re: is sam around tmrw" in result.output
    assert "Bash python run.py --who sam" in result.output
    assert "echo done" not in result.output
    assert "3 cases across 1 file — each is a real agent turn" in result.output


def test_scenarios_run_declined_sends_nothing(runner, account_root, tmp_path, fake):
    folder = tmp_path / "sc"
    _write(folder, "who_is_away.md", WHO_IS_AWAY)

    result = runner.invoke(
        cli, ["agent", "scenarios", "run", "CRM Agent", "--path", str(folder)], input="n\n"
    )

    assert result.exit_code != 0
    assert fake.sessions == []
    assert fake.sent == {}


def test_scenarios_run_timeout_on_one_case_does_not_stop_the_rest(
    runner, account_root, tmp_path, fake
):
    folder = tmp_path / "sc"
    _write(
        folder,
        "mixed.md",
        "## Say | Expect\n| Say | Expect |\n|---|---|\n"
        "| `quick one` | a reply |\n| `slow one` | a reply |\n| `another` | a reply |\n",
    )

    result = runner.invoke(
        cli,
        ["agent", "scenarios", "run", "CRM Agent", "--path", str(folder), "--yes", "--timeout", "0"],
    )

    assert result.exit_code != 0
    assert list(fake.sent.values()) == ["quick one", "slow one", "another"]
    assert "cinna chat --attach sess-2" in result.output
    assert "1 of 3 cases did not complete" in result.output


def test_scenarios_run_json_emits_one_object_per_case_and_a_summary(
    runner, account_root, tmp_path, fake
):
    folder = tmp_path / "sc"
    _write(folder, "who_is_away.md", WHO_IS_AWAY)

    result = runner.invoke(
        cli, ["agent", "scenarios", "run", "CRM Agent", "--path", str(folder), "--yes", "--json"]
    )

    assert result.exit_code == 0, result.output
    events = _ndjson(result.output)
    assert [e["event"] for e in events] == ["case", "case", "case", "summary"]
    first = events[0]
    assert first["say"] == "is sam around tmrw"
    assert first["outcome"] == "completed"
    assert first["session_id"] == "sess-1"
    assert first["tool_calls"] == ["Bash python run.py --who sam"]
    assert "recover" not in first
    summary = events[-1]
    assert summary["completed"] == 3
    assert summary["conversation_model"].startswith("haiku")


def test_scenarios_run_json_needs_yes(runner, account_root, tmp_path, fake):
    folder = tmp_path / "sc"
    _write(folder, "who_is_away.md", WHO_IS_AWAY)

    result = runner.invoke(
        cli, ["agent", "scenarios", "run", "CRM Agent", "--path", str(folder), "--json"]
    )

    assert result.exit_code == 2
    assert fake.sessions == []


def test_scenarios_run_out_writes_a_markdown_report(runner, account_root, tmp_path, fake):
    folder = tmp_path / "sc"
    _write(folder, "who_is_away.md", WHO_IS_AWAY)
    report = tmp_path / "run.md"

    result = runner.invoke(
        cli,
        ["agent", "scenarios", "run", "CRM Agent", "--path", str(folder), "--yes", "--out", str(report)],
    )

    assert result.exit_code == 0, result.output
    text = report.read_text()
    assert "# Scenario run — CRM Agent" in text
    assert "## who_is_away.md" in text
    assert "| # | Say | Expect | Reply | Tools | Outcome | Verdict |" in text
    assert "`grep a \\| b`" in text
    assert "haiku" in text


def test_scenarios_run_only_the_named_files(runner, account_root, tmp_path, fake):
    folder = tmp_path / "sc"
    _write(folder, "who_is_away.md", WHO_IS_AWAY)
    _write(folder, "scope.md", "## Say | Expect\n| Say | Expect |\n|---|---|\n| `hi` | hello |\n")

    result = runner.invoke(
        cli, ["agent", "scenarios", "run", "CRM Agent", "scope", "--path", str(folder), "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert list(fake.sent.values()) == ["hi"]

    result = runner.invoke(
        cli, ["agent", "scenarios", "run", "CRM Agent", "nope", "--path", str(folder), "--yes"]
    )
    assert result.exit_code != 0
    assert "Available: scope.md, who_is_away.md" in result.output
