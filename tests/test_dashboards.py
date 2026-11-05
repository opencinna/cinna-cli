"""Tests for `cinna dashboards` and `cinna chat --board`.

Account-workspace calls run the real ``AccountClient`` path; only the HTTP
boundary is mocked with respx (the api-proxy, keyed by the inner method and
path, plus the account agent listing). The fake answers with core's shapes:
component routes refuse with ``{"error": {code, message, …}}``, an unknown
route with FastAPI's bare ``{"detail": "Not Found"}``.
"""

import json
from pathlib import Path

import httpx
import pytest
import respx
from click.testing import CliRunner

from cinna import dashboards as dashboards_module
from cinna.account import AccountConfig, save_account_config
from cinna.client import PROXY_MARKER_HEADER
from cinna.config import CinnaConfig, save_config
from cinna.dashboards import (
    SIZE_GRID,
    build_dashboard_context,
    checker_command,
    parse_params,
    reference,
    refresh_overrides,
    survey_local_components,
)
from cinna.main import cli

PLATFORM = "https://platform.example.com"
PROXY_URL = f"{PLATFORM}/api/v1/cli/account/api-proxy"
AGENTS_URL = f"{PLATFORM}/api/v1/cli/account/agents"
AGENT = "11111111-1111-4111-8111-111111111111"
OTHER_AGENT = "99999999-9999-4999-8999-999999999999"
BOARD = "22222222-2222-4222-8222-222222222222"
DESKTOP_BOARD = "33333333-3333-4333-8333-333333333333"
BLOCK = "44444444-4444-4444-8444-444444444444"
BLOCK2 = "55555555-5555-4555-8555-555555555555"
BUNDLE = "com.example.crm-agent"
CAPABILITIES = {
    "version": 1,
    "contract_version": "1.21.0",
    "limits": {"runs_per_minute": 30, "blocks_per_dashboard": 20, "dashboards_per_user": 10,
               "context_chars": 12000, "summary_chars": 8000},
    "chat": True,
    "component_refs": True,
}
CORE_LAYOUT_KIT = Path(__file__).resolve().parents[2] / "workflow-runner-core" / "docs" / "local_agent_kit"


def component(name="deals", skill=None, findings=None, placements=None, size="m", **extra):
    body = {
        "name": name,
        "skill": skill,
        "title": "Open deals",
        "view": "table",
        "component_json": {
            "schema_version": 1,
            "title": "Open deals",
            "view": "table",
            "source": {"endpoint": "rows"},
            "endpoints": {"rows": {"script": "api/rows.py", "description": "Rows."}},
            "params": {"stage": {"type": "enum", "values": ["all", "won"], "default": "all"}},
            "size": size,
            "requires_credentials": ["crm"],
            "context": "One row per deal.",
        },
        "findings": findings or [],
        "has_errors": any(f.get("severity") == "error" for f in findings or []),
        "size_bytes": 900,
        "placements": placements or [],
    }
    body.update(extra)
    return body


def tile_block(block_id=BLOCK, agent_id=AGENT, name="deals", params=None, title=None, w=6, h=2):
    return {
        "id": block_id,
        "agent_id": agent_id,
        "view_type": "agent_component",
        "title": title,
        "show_border": True,
        "grid_x": 0,
        "grid_y": 0,
        "grid_w": w,
        "grid_h": h,
        "config": {"component": name, "params": params or {}, "refresh": {}},
    }


def board(board_id=BOARD, name="Sales", blocks=None, origin="web"):
    return {"id": board_id, "name": name, "origin": origin, "blocks": blocks or [], "sort_order": 0}


def refusal(status, code, message, **extra):
    return status, {"error": {"code": code, "message": message, **extra}}


class FakeCore:
    """api-proxy calls by inner (method, path) — a list of answers is served in order."""

    def __init__(self):
        self.routes: dict[tuple[str, str], object] = {
            ("GET", f"agents/{AGENT}"): (200, {"id": AGENT, "name": "CRM Agent", "bundle_id": BUNDLE}),
            ("GET", "dashboards/capabilities"): (200, CAPABILITIES),
        }
        self.calls: list[dict] = []

    def proxy(self, request: httpx.Request) -> httpx.Response:
        inner = json.loads(request.content)
        self.calls.append(inner)
        answer = self.routes.get((inner["method"], inner["path"]), (404, {"detail": "Not Found"}))
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        status, body = answer
        return httpx.Response(status, json=body, headers={PROXY_MARKER_HEADER: "1"})

    def calls_to(self, method, path):
        return [c for c in self.calls if (c["method"], c["path"]) == (method, path)]


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
def core(account_root, monkeypatch):
    monkeypatch.setattr(dashboards_module, "_sleep", lambda s: None)
    fake = FakeCore()
    with respx.mock(assert_all_called=False) as mock:
        mock.post(PROXY_URL).mock(side_effect=fake.proxy)
        fake.mock = mock
        mock.get(AGENTS_URL).mock(
            return_value=httpx.Response(
                200,
                json={"data": [
                    {"id": AGENT, "name": "CRM Agent"},
                    {"id": OTHER_AGENT, "name": "Ops Agent"},
                ]},
            )
        )
        yield fake
    assert not any(c["path"].endswith("/frame") for c in fake.calls), "a frame route was called"


def json_lines(output: str) -> list[dict]:
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def final(output: str) -> dict:
    return json_lines(output)[-1]


# ─── pure helpers ────────────────────────────────────────────────────────────


def test_reference_forms():
    assert reference(BUNDLE, "deals") == f"cinna://component/bundle:{BUNDLE}/deals"
    assert reference(BUNDLE, "deals", "crm") == f"cinna://component/bundle:{BUNDLE}/crm/deals"


def test_params_are_json_first_and_overlay_a_file(tmp_path):
    file = tmp_path / "p.json"
    file.write_text('{"stage": "won", "limit": 5}')
    assert parse_params(("limit=10", "flag=true", "name=acme", 'quoted="7"', "empty="), f"@{file}") == {
        "stage": "won",
        "limit": 10,
        "flag": True,
        "name": "acme",
        "quoted": "7",
        "empty": "",
    }


@pytest.mark.parametrize("bad", ["novalue", "=x"])
def test_params_refuse_a_pair_without_key_or_value_sign(bad):
    import click

    with pytest.raises(click.UsageError):
        parse_params((bad,))


def test_refresh_overrides_follow_cores_rules():
    import click

    assert refresh_overrides("15m", "manual", False) == {
        "max_age": "15m", "while_open_every": "manual", "after_turn": False
    }
    assert refresh_overrides(None, "90s", None) == {"while_open_every": "90s"}
    for every in ("30s", "5x"):
        with pytest.raises(click.UsageError):
            refresh_overrides(None, every, None)
    with pytest.raises(click.UsageError):
        refresh_overrides("0m", None, None)


def test_checker_argv_for_agent_and_skill_components():
    assert checker_command("deals", None) == [
        "python3", "/app/core/skills/cinna-dashboards/scripts/check_component.py",
        "/app/workspace/dashboards/deals", "--agent", "/app/workspace", "--run", "--json",
    ]
    assert checker_command("deals", "crm")[2] == "/app/workspace/skills/crm/dashboards/deals"


def test_context_matches_the_panel_shape_and_trims_under_the_cap():
    blocks = [
        tile_block(BLOCK, params={"stage": "won"}),
        tile_block(BLOCK2, name="pipeline"),
        tile_block("66666666-6666-4666-8666-666666666666", agent_id=OTHER_AGENT, name="ops", title="Ops"),
    ]
    data = {"data": {"rows": [{"id": "d1"}]}, "fetched_at": "2026-11-04T10:00:00Z", "params_resolved": {"stage": "won"}}
    text = build_dashboard_context(board(blocks=blocks), AGENT, blocks[0], data, "One row per deal.", lambda a: "Ops Agent")
    payload = json.loads(text)
    assert payload["dashboard"] == {"id": BOARD, "name": "Sales"}
    focused = payload["focused"]
    assert focused["tile_id"] == BLOCK and focused["params"] == {"stage": "won"}
    assert focused["data_age"] == "2026-11-04T10:00:00Z" and focused["context"] == "One row per deal."
    assert [t["component"] for t in payload["also_yours"]] == ["pipeline"]
    assert payload["other_tiles"] == [{"title": "Ops", "agent": "Ops Agent"}]

    big = {"data": "x" * 20000, "fetched_at": None}
    text = build_dashboard_context(board(blocks=blocks), AGENT, blocks[0], big, None, lambda a: "Ops Agent", max_chars=12000)
    assert len(text) <= 12000
    assert len(json.loads(text)["focused"]["summary"]) == 8000  # cut to the summary cap first
    assert json.loads(text)["other_tiles"]  # which leaves room for the tiles

    text = build_dashboard_context(
        board(blocks=blocks), AGENT, blocks[0], big, None, lambda a: "Ops Agent",
        max_chars=12000, summary_chars=20000,
    )
    assert len(text) <= 12000
    trimmed = json.loads(text)
    assert trimmed["other_tiles"] == [] and trimmed["also_yours"] == []
    assert trimmed["focused"]["summary"].startswith("x")


# ─── component verbs ─────────────────────────────────────────────────────────


def test_list_prints_references_and_desktop_placements(runner, core):
    core.routes[("GET", f"agents/{AGENT}/dashboards/")] = (200, {"components": [
        component(placements=[
            {"dashboard_id": BOARD, "dashboard_name": "Sales", "block_id": BLOCK, "origin": "web"},
            {"dashboard_id": DESKTOP_BOARD, "dashboard_name": "x", "block_id": BLOCK2, "origin": "desktop"},
        ]),
        component("pipeline", skill="crm", findings=[{"path": "dashboards/pipeline", "code": "title_blank", "severity": "error", "message": "title is blank"}]),
    ]})
    result = runner.invoke(cli, ["dashboards", "list", "crm-agent", "--no-flush"])
    assert result.exit_code == 0, result.output
    assert f"cinna://component/bundle:{BUNDLE}/deals" in result.output
    assert f"cinna://component/bundle:{BUNDLE}/crm/pipeline" in result.output
    assert "Cinna Desktop" in result.output and "Sales" in result.output
    # fresh=true: skip core's per-worker list cache right after a push
    assert core.calls_to("GET", f"agents/{AGENT}/dashboards/")[0]["query"] == {"fresh": "true"}


def test_list_json_carries_references(runner, core):
    core.routes[("GET", f"agents/{AGENT}/dashboards/")] = (200, {"components": [component()]})
    result = runner.invoke(cli, ["dashboards", "list", "crm-agent", "--json", "--no-flush"])
    assert result.exit_code == 0, result.output
    payload = final(result.output)
    assert payload["agent"]["bundle_id"] == BUNDLE
    assert payload["components"][0]["reference"] == f"cinna://component/bundle:{BUNDLE}/deals"


@pytest.mark.parametrize("answer", [(404, {"detail": "Not Found"}), (422, {"detail": [{"msg": "uuid"}]})])
def test_server_without_dashboards_gets_one_clear_message(runner, core, answer):
    core.routes[("GET", "dashboards/capabilities")] = answer
    result = runner.invoke(cli, ["dashboards", "list", "crm-agent", "--json", "--no-flush"])
    assert result.exit_code == 1
    assert final(result.output)["code"] == "dashboards_unsupported"
    assert [c["path"] for c in core.calls] == ["dashboards/capabilities"]  # nothing else tried


def test_a_hatch_refusal_of_the_capabilities_call_is_not_no_dashboards(runner, core, account_root):
    # The proxy's own rate limit answers without the marker header.
    core.mock.post(PROXY_URL).mock(return_value=httpx.Response(429, json={"detail": "Rate limit exceeded"}))
    result = runner.invoke(cli, ["dashboards", "boards", "--json"])
    line = final(result.output)
    assert line["code"] != "dashboards_unsupported"
    assert "Rate limit exceeded" in line["detail"]


def test_capabilities_of_another_version_are_refused(runner, core):
    core.routes[("GET", "dashboards/capabilities")] = (200, {"version": 2})
    result = runner.invoke(cli, ["dashboards", "boards", "--json"])
    assert final(result.output)["code"] == "dashboards_unsupported"


def test_capabilities_limits_are_used(runner, core):
    core.routes[("GET", "dashboards/capabilities")] = (200, {"version": 1, "limits": {"blocks_per_dashboard": 1}})
    core.routes[("GET", f"agents/{AGENT}/dashboards/deals")] = (200, component())
    core.routes[("GET", "dashboards/")] = (200, [board(blocks=[tile_block()])])
    result = runner.invoke(cli, ["dashboards", "add", "crm-agent", "deals", "--board", "Sales", "--json", "--no-flush"])
    assert final(result.output)["code"] == "dashboard_full"
    assert "Maximum of 1 blocks" in final(result.output)["detail"]


@pytest.mark.parametrize(
    "answer, exit_code, code, remedy",
    [
        (refusal(409, "adapter_unsupported", "Rebuild the environment to show components."), 1, "adapter_unsupported", "cinna agent rebuild-env crm-agent"),
        (refusal(409, "needs_credentials", "Needs crm."), 1, "needs_credentials", "cinna agent show crm-agent"),
        (refusal(429, "rate_limited", "Too many runs.", retry_after_s=12), 1, "rate_limited", "try again in 12s"),
        (refusal(400, "invalid_input", "stage must be one of all, won.", field="stage"), 2, "invalid_input", "(field: stage)"),
        (refusal(404, "not_found", "No component deals."), 1, "not_found", "cinna dashboards list crm-agent"),
        (refusal(502, "call_failed", "Exit 1: boom"), 1, "call_failed", "Exit 1: boom"),
    ],
)
def test_refusals_keep_cores_sentence_and_add_the_remedy(runner, core, answer, exit_code, code, remedy):
    core.routes[("POST", f"agents/{AGENT}/dashboards/deals/run")] = answer
    result = runner.invoke(cli, ["dashboards", "run", "crm-agent", "deals", "--json", "--no-flush"])
    assert result.exit_code == exit_code, result.output
    line = final(result.output)
    assert line["code"] == code
    assert answer[1]["error"]["message"] in line["detail"]
    assert remedy in line["detail"]


def test_run_waits_for_a_waking_environment_and_sends_its_own_budget_key(runner, core):
    core.routes[("POST", f"agents/{AGENT}/dashboards/deals/run")] = [
        refusal(503, "env_unavailable", "The environment is starting; try again in a moment.", retry_after_s=1),
        (200, {"value": {"rows": [1]}, "fetched_at": "2026-11-04T10:00:00Z", "notice": None, "params_resolved": {"stage": "won"}}),
    ]
    result = runner.invoke(cli, ["dashboards", "run", "crm-agent", "deals", "--param", "stage=won", "--json", "--no-flush"])
    assert result.exit_code == 0, result.output
    assert final(result.output)["value"] == {"rows": [1]}
    sent = core.calls_to("POST", f"agents/{AGENT}/dashboards/deals/run")
    assert len(sent) == 2
    assert sent[0]["json_body"] == {"params": {"stage": "won"}, "tile_key": "cli"}


def test_run_gives_up_on_a_stopped_environment_after_the_timeout(runner, core):
    core.routes[("POST", f"agents/{AGENT}/dashboards/deals/run")] = refusal(503, "env_unavailable", "The environment is stopped.")
    result = runner.invoke(cli, ["dashboards", "run", "crm-agent", "deals", "--timeout", "0", "--json", "--no-flush"])
    assert result.exit_code == 12
    assert final(result.output)["code"] == "env_unavailable"


def test_ref_prints_only_the_line_and_checks_it(runner, core):
    result = runner.invoke(cli, ["dashboards", "ref", "crm-agent", "deals", "--skill", "crm"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == f"cinna://component/bundle:{BUNDLE}/crm/deals"

    core.routes[("POST", "dashboards/component-refs/resolve")] = (200, {"results": [
        {"ref": "x", "status": "gone", "message": "This component is no longer there."}
    ]})
    result = runner.invoke(cli, ["dashboards", "ref", "crm-agent", "deals", "--check", "--json"])
    assert result.exit_code == 1
    line = final(result.output)
    assert line["code"] == "ref_gone" and line["reference"].endswith("/deals")


def test_check_exits_one_on_errors_and_merges_run_findings(runner, core, account_root, monkeypatch):
    child = account_root / "agents" / "crm-agent"
    save_config(
        CinnaConfig(platform_url=PLATFORM, cli_token="t", agent_id=AGENT, agent_name="crm-agent",
                    environment_id="e", template="python"),
        child,
    )
    core.routes[("GET", f"agents/{AGENT}/dashboards/deals")] = (200, component(findings=[
        {"path": "dashboards/deals/component.json", "code": "title_blank", "severity": "warning", "message": "title"}
    ]))
    seen = {}

    def fake_stream_exec(self, agent_id, command, timeout=None):
        seen["command"] = command
        yield {"type": "tool_result_delta", "content": json.dumps({"ok": False, "problems": [
            {"path": "dashboards/deals/component.json", "code": "title_blank", "severity": "warning", "message": "title"},
            {"path": "dashboards/deals", "code": "run_not_json", "severity": "error", "message": "the source did not print JSON"},
        ]})}
        yield {"type": "done", "exit_code": 1}

    monkeypatch.setattr("cinna.client.PlatformClient.stream_exec", fake_stream_exec)
    monkeypatch.setattr("cinna.sync_session.status", lambda cfg: type("S", (), {"exists": False})())
    result = runner.invoke(cli, ["dashboards", "check", "crm-agent", "deals", "--run", "--json"])
    assert result.exit_code == 1, result.output
    line = final(result.output)
    assert line["code"] == "component_errors"
    findings = line["components"][0]["findings"]
    assert [(f["code"], f["source"]) for f in findings] == [("title_blank", "core"), ("run_not_json", "run")]
    assert "--run --json" in seen["command"] and "/app/workspace/dashboards/deals" in seen["command"]


def test_check_run_needs_the_agent_synced_here(runner, core):
    core.routes[("GET", f"agents/{AGENT}/dashboards/deals")] = (200, component())
    result = runner.invoke(cli, ["dashboards", "check", "crm-agent", "deals", "--run", "--json"])
    assert final(result.output)["code"] == "not_synced"


def test_reads_push_local_edits_first(runner, core, account_root, monkeypatch):
    child = account_root / "agents" / "crm-agent"
    save_config(
        CinnaConfig(platform_url=PLATFORM, cli_token="t", agent_id=AGENT, agent_name="crm-agent",
                    environment_id="e", template="python"),
        child,
    )
    flushed = []
    monkeypatch.setattr("cinna.sync_session.status", lambda cfg: type("S", (), {"exists": True})())
    monkeypatch.setattr("cinna.sync_session.flush", lambda cfg, workspace_root=None: flushed.append((cfg.agent_id, workspace_root)))
    core.routes[("GET", f"agents/{AGENT}/dashboards/")] = (200, {"components": []})
    result = runner.invoke(cli, ["dashboards", "list", "crm-agent"])
    assert result.exit_code == 0, result.output
    assert flushed == [(AGENT, child)]  # the root lets flush re-mint a refused token
    assert "Pushing local changes" in result.output

    flushed.clear()
    runner.invoke(cli, ["dashboards", "list", "crm-agent", "--no-flush"])
    assert flushed == []


# ─── boards and tiles ────────────────────────────────────────────────────────


def test_add_places_a_tile_the_way_cores_dialog_does(runner, core):
    core.routes[("GET", f"agents/{AGENT}/dashboards/deals")] = (200, component(size="l"))
    core.routes[("GET", "dashboards/")] = (200, [board()])
    core.routes[("POST", f"dashboards/{BOARD}/blocks")] = (200, tile_block(w=6, h=4))
    result = runner.invoke(cli, [
        "dashboards", "add", "crm-agent", "deals", "--board", "sales", "--param", "stage=won",
        "--max-age", "15m", "--json", "--no-flush",
    ])
    assert result.exit_code == 0, result.output
    body = core.calls_to("POST", f"dashboards/{BOARD}/blocks")[0]["json_body"]
    assert body == {
        "agent_id": AGENT,
        "view_type": "agent_component",
        "title": None,
        "show_border": True,
        "grid_x": 0,
        "grid_y": 0,
        "grid_w": SIZE_GRID["l"][0],
        "grid_h": SIZE_GRID["l"][1],
        "config": {"component": "deals", "params": {"stage": "won"}, "refresh": {"max_age": "15m"}},
    }
    assert final(result.output)["dashboard"]["url"] == f"https://ui.example.com/dashboards/{BOARD}"


def test_add_refuses_a_component_with_errors(runner, core):
    core.routes[("GET", f"agents/{AGENT}/dashboards/deals")] = (200, component(findings=[
        {"path": "dashboards/deals", "code": "script_missing", "severity": "error", "message": "api/rows.py is missing"}
    ]))
    result = runner.invoke(cli, ["dashboards", "add", "crm-agent", "deals", "--board", "Sales", "--json", "--no-flush"])
    line = final(result.output)
    assert line["code"] == "component_errors" and "api/rows.py is missing" in line["detail"]
    assert not core.calls_to("POST", f"dashboards/{BOARD}/blocks")


def test_add_refuses_a_full_dashboard_and_can_create_a_new_one(runner, core):
    core.routes[("GET", f"agents/{AGENT}/dashboards/deals")] = (200, component())
    core.routes[("GET", "dashboards/")] = (200, [board(blocks=[tile_block(f"{i:08d}-0000-4000-8000-000000000000") for i in range(20)])])
    result = runner.invoke(cli, ["dashboards", "add", "crm-agent", "deals", "--board", "Sales", "--json", "--no-flush"])
    assert final(result.output)["code"] == "dashboard_full"

    new_id = "77777777-7777-4777-8777-777777777777"
    core.routes[("POST", "dashboards/")] = (200, board(new_id, "Ops"))
    core.routes[("POST", f"dashboards/{new_id}/blocks")] = (200, tile_block())
    result = runner.invoke(cli, ["dashboards", "add", "crm-agent", "deals", "--board", "Ops", "--new-board", "--json", "--no-flush"])
    assert result.exit_code == 0, result.output
    assert core.calls_to("POST", "dashboards/")[0]["json_body"] == {"name": "Ops"}


def test_desktop_dashboards_are_read_only(runner, core):
    desktop = board(DESKTOP_BOARD, "Desk", blocks=[tile_block()], origin="desktop")
    core.routes[("GET", "dashboards/")] = (200, [])
    core.routes[("GET", f"dashboards/{DESKTOP_BOARD}")] = (200, desktop)
    core.routes[("GET", f"dashboards/{DESKTOP_BOARD}/blocks/{BLOCK}/component/data")] = (200, {"data": [1], "stale": False})
    result = runner.invoke(cli, ["dashboards", "tile", "data", DESKTOP_BOARD, "deals", "--json"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli, ["dashboards", "tile", "params", DESKTOP_BOARD, "deals", "--set", "stage=won", "--json"])
    assert final(result.output)["code"] == "desktop_dashboard"
    assert not core.calls_to("PUT", f"dashboards/{DESKTOP_BOARD}/blocks/{BLOCK}/component/params")
    # call runs an endpoint in the env (may wake it, spends the tile's run budget): a write too.
    result = runner.invoke(cli, ["dashboards", "tile", "call", DESKTOP_BOARD, "deals", "rows", "--json"])
    assert final(result.output)["code"] == "desktop_dashboard"
    assert not core.calls_to("POST", f"dashboards/{DESKTOP_BOARD}/blocks/{BLOCK}/component/call")


@pytest.mark.parametrize(
    "args, sent",
    [
        (["--set", "limit=5"], {"stage": "won", "region": "eu", "limit": 5}),
        (["--clear", "region"], {"stage": "won"}),
        (["--reset", "--set", "limit=1"], {"limit": 1}),
    ],
)
def test_tile_params_send_the_whole_set(runner, core, args, sent):
    core.routes[("GET", "dashboards/")] = (200, [board(blocks=[tile_block(params={"stage": "won", "region": "eu"})])])
    path = f"dashboards/{BOARD}/blocks/{BLOCK}/component/params"
    core.routes[("PUT", path)] = (200, {"data": [], "stale": False, "params_resolved": sent})
    result = runner.invoke(cli, ["dashboards", "tile", "params", "Sales", BLOCK[:8], *args, "--json"])
    assert result.exit_code == 0, result.output
    assert core.calls_to("PUT", path)[0]["json_body"] == {"params": sent}


def test_tile_params_refused_value_keeps_cores_sentence(runner, core):
    core.routes[("GET", "dashboards/")] = (200, [board(blocks=[tile_block()])])
    core.routes[("PUT", f"dashboards/{BOARD}/blocks/{BLOCK}/component/params")] = refusal(
        400, "invalid_input", "stage must be one of all, won.", field="stage"
    )
    result = runner.invoke(cli, ["dashboards", "tile", "params", "Sales", "deals", "--set", "stage=lost", "--json"])
    assert result.exit_code == 2
    assert "stage must be one of all, won." in final(result.output)["detail"]


def test_tiles_lists_data_age_without_waking(runner, core):
    core.routes[("GET", "dashboards/")] = (200, [board(blocks=[tile_block(params={"stage": "won"})])])
    core.routes[("GET", f"dashboards/{BOARD}/blocks/{BLOCK}/component/data")] = (
        200, {"data": [], "stale": True, "fetched_at": None, "mark": None, "missing_credentials": ["crm"]}
    )
    result = runner.invoke(cli, ["dashboards", "tiles", "Sales", "--json"])
    assert result.exit_code == 0, result.output
    tile = final(result.output)["tiles"][0]
    assert tile["agent"] == "CRM Agent" and tile["size"] == "m" and tile["missing_credentials"] == ["crm"]
    assert not core.calls_to("POST", f"dashboards/{BOARD}/blocks/{BLOCK}/component/refresh")


def test_tile_edit_and_remove(runner, core):
    core.routes[("GET", "dashboards/")] = (200, [board(blocks=[tile_block(params={"stage": "won"})])])
    core.routes[("PUT", f"dashboards/{BOARD}/blocks/{BLOCK}")] = (200, tile_block(title="Deals"))
    result = runner.invoke(cli, ["dashboards", "tile", "edit", "Sales", "deals", "--title", "Deals", "--size", "wide", "--every", "5m", "--json"])
    assert result.exit_code == 0, result.output
    body = core.calls_to("PUT", f"dashboards/{BOARD}/blocks/{BLOCK}")[0]["json_body"]
    assert body == {
        "config": {"component": "deals", "params": {"stage": "won"}, "refresh": {"while_open_every": "5m"}},
        "title": "Deals",
        "grid_w": 12,
        "grid_h": 2,
    }
    core.routes[("DELETE", f"dashboards/{BOARD}/blocks/{BLOCK}")] = (200, {"message": "ok"})
    result = runner.invoke(cli, ["dashboards", "tile", "remove", "Sales", "deals", "--json"])
    assert result.exit_code == 2  # --json needs --yes
    result = runner.invoke(cli, ["dashboards", "tile", "remove", "Sales", "deals", "--yes", "--json"])
    assert result.exit_code == 0, result.output
    assert core.calls_to("DELETE", f"dashboards/{BOARD}/blocks/{BLOCK}")


# ─── cinna chat --board ──────────────────────────────────────────────────────


def test_chat_board_opens_the_dashboard_chat_and_sends_the_context(runner, core):
    blocks = [tile_block(params={"stage": "won"})]
    after = [tile_block(params={"stage": "all"})]
    core.routes[("GET", "dashboards/")] = (200, [board(blocks=blocks)])
    core.routes[("GET", f"dashboards/{BOARD}")] = (200, board(blocks=after))  # read once, after the turn
    core.routes[("GET", f"dashboards/{BOARD}/blocks/{BLOCK}/component/data")] = (
        200, {"data": {"rows": [1]}, "stale": False, "fetched_at": "2026-11-04T10:00:00Z", "params_resolved": {"stage": "won"}}
    )
    core.routes[("GET", f"agents/{AGENT}/dashboards/deals")] = (200, component())
    session = {"id": "sess-1", "agent_id": AGENT, "mode": "conversation", "title": None}
    core.routes[("POST", f"dashboards/{BOARD}/chat")] = (200, {"session": session})
    core.routes[("POST", "sessions/sess-1/messages/stream")] = (200, {"status": "ok", "streaming": False})
    core.routes[("GET", "sessions/sess-1/messages")] = (200, {"data": [], "count": 0})
    core.routes[("GET", "sessions/sess-1/messages/streaming-status")] = (200, {"is_streaming": False})
    core.routes[("GET", "sessions/sess-1")] = (200, dict(session, result_state=None))
    result = runner.invoke(cli, [
        "chat", "--agent", "crm-agent", "--board", "Sales", "--focus", "deals", "--new-chat",
        "--timeout", "1", "--interval", "0.01", "Which deal first?",
    ])
    assert result.exit_code == 0, result.output
    assert core.calls_to("POST", f"dashboards/{BOARD}/chat")[0]["json_body"] == {"agent_id": AGENT, "new": True}
    sent = core.calls_to("POST", "sessions/sess-1/messages/stream")[0]["json_body"]
    context = json.loads(sent["dashboard_context"])
    assert context["focused"]["tile_id"] == BLOCK and context["focused"]["context"] == "One row per deal."
    events = json_lines(result.output)
    assert events[0]["dashboard"] == {"id": BOARD, "name": "Sales"}
    tiles = [e for e in events if e.get("event") == "tiles"]
    assert tiles and tiles[0]["changed"][0]["params"] == {"stage": "all"}
    assert events[-1]["event"] == "done"


def test_chat_board_flags_need_board(runner, account_root):
    result = runner.invoke(cli, ["chat", "--agent", "x", "--focus", "deals", "hi"])
    assert result.exit_code == 2 and "--board" in result.output


def test_chat_board_context_file_over_the_cap_is_refused(runner, core, tmp_path):
    core.routes[("GET", "dashboards/")] = (200, [board()])
    big = tmp_path / "ctx.json"
    big.write_text(json.dumps({"dashboard": {"id": BOARD}, "note": "x" * 13000}))
    result = runner.invoke(cli, ["chat", "--agent", "crm-agent", "--board", "Sales", "--context", f"@{big}", "hi"])
    assert result.exit_code == 2 and "12000" in result.output


# ─── per-agent guide and import survey ───────────────────────────────────────


def test_claude_md_fills_in_the_reference_and_the_kit_path(account_root):
    from cinna.context import regenerate_claude_md

    child = account_root / "agents" / "crm-agent"
    config = CinnaConfig(platform_url=PLATFORM, cli_token="t", agent_id=AGENT, agent_name="crm-agent",
                         environment_id="e", template="python", bundle_id=BUNDLE)
    save_config(config, child)
    regenerate_claude_md(config, child)
    text = (child / "CLAUDE.md").read_text()
    assert f"cinna://component/bundle:{BUNDLE}/<name>" in text
    assert "../../context/local-kit/skills/cinna-dashboards/SKILL.md" in text
    assert "{dashboards_section}" not in text
    assert "plain words instead" in text


def test_claude_md_follows_the_environments_platform_skills(account_root):
    from cinna.context import generate_context_files

    child = account_root / "agents" / "crm-agent"
    config = CinnaConfig(platform_url=PLATFORM, cli_token="t", agent_id=AGENT, agent_name="crm-agent",
                         environment_id="e", template="python", bundle_id=BUNDLE)
    save_config(config, child)
    generate_context_files(
        {"building_prompt": "x", "platform_skills": [{"name": "cinna-dashboards", "kit_path": "skills/cinna-dashboards"}]},
        config, child,
    )
    from cinna.config import load_config

    assert load_config(child).platform_skills == [{"name": "cinna-dashboards", "kit_path": "skills/cinna-dashboards"}]
    assert "../../context/local-kit/skills/cinna-dashboards/SKILL.md" in (child / "CLAUDE.md").read_text()

    generate_context_files({"building_prompt": "x", "platform_skills": [{"name": "cinna-tasks"}]}, config, child)
    text = (child / "CLAUDE.md").read_text()
    assert "has no `cinna-dashboards` skill yet" in text and "cinna agent rebuild-env crm-agent" in text


def test_claude_md_without_bundle_id_or_account(tmp_path):
    from cinna.context import regenerate_claude_md

    config = CinnaConfig(platform_url=PLATFORM, cli_token="t", agent_id=AGENT, agent_name="crm-agent",
                         environment_id="e", template="python")
    save_config(config, tmp_path)
    regenerate_claude_md(config, tmp_path)
    text = (tmp_path / "CLAUDE.md").read_text()
    assert "cinna dashboards ref crm-agent <name>" in text
    assert "this standalone workspace has no copy" in text

    config.agent_name = "Deal desk agent"
    regenerate_claude_md(config, tmp_path)
    assert "`cinna dashboards ref 'Deal desk agent' <name>`" in (tmp_path / "CLAUDE.md").read_text()


def _agent_folder(tmp_path: Path, spec: dict, extra_files: dict | None = None) -> tuple[Path, list[str]]:
    agent = tmp_path / "agent"
    folder = agent / "dashboards" / "deals"
    folder.mkdir(parents=True)
    (agent / "cinna-agent.json").write_text(json.dumps({"id": AGENT, "slug": "crm", "name": "CRM"}))
    (folder / "component.json").write_text(json.dumps(spec))
    for rel, content in (extra_files or {}).items():
        path = agent / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    files = sorted(p.relative_to(agent).as_posix() for p in agent.rglob("*") if p.is_file())
    return agent, files


def test_import_survey_warns_about_cloud_gaps(tmp_path):
    spec = {
        "schema_version": 1, "title": "Deals", "view": "table",
        "source": {"file": "app-data/dashboards/deals/rows.json"},
        "refresh": {"on_app_start": True},
        "requires_credentials": ["crm", "erp"],
    }
    agent, files = _agent_folder(tmp_path, spec, {"dashboards/deals/package.json": "{}"})
    found = survey_local_components(agent, files, {"crm"})
    assert [(c["name"], c["skill"]) for c in found] == [("deals", None)]
    text = " | ".join(found[0]["warnings"])
    assert "app-data/ does not travel" in text
    assert "on_app_start is ignored" in text
    assert "erp" in text and "crm," not in text
    assert "package.json" in text


@pytest.mark.skipif(not (CORE_LAYOUT_KIT / "skills" / "cinna-dashboards").is_dir(), reason="no cinna-core checkout beside this repo")
def test_import_survey_runs_the_kits_static_checker(tmp_path):
    import shutil

    agent, files = _agent_folder(tmp_path, {"schema_version": 1, "title": "", "view": "table", "source": {"endpoint": "rows"}})
    kit = tmp_path / "account" / "context" / "local-kit"
    shutil.copytree(CORE_LAYOUT_KIT / "skills", kit / "skills")
    found = survey_local_components(agent, files, set(), kit)
    assert any(w.startswith("checker:") for w in found[0]["warnings"]), found


def test_import_survey_never_runs_a_kit_shipped_with_the_imported_folder(tmp_path):
    agent, files = _agent_folder(tmp_path, {"schema_version": 1, "title": "Deals", "view": "table", "source": {"endpoint": "rows"}})
    planted = tmp_path / ".cinna-kit" / "skills" / "cinna-dashboards" / "scripts"
    planted.mkdir(parents=True)
    marker = tmp_path / "ran"
    (planted / "check_component.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
    (tmp_path / ".cinna-kit" / "layout.json").write_text("{}")
    survey_local_components(agent, files, set())  # no trusted kit given
    assert not marker.exists()


def test_chat_board_refuses_to_resume_a_session_of_another_dashboard(runner, core):
    core.routes[("GET", "dashboards/")] = (200, [board(blocks=[tile_block()])])
    core.routes[("GET", "sessions/sess-9")] = (200, {"id": "sess-9", "agent_id": AGENT, "mode": "conversation", "dashboard_id": None})
    result = runner.invoke(cli, ["chat", "--board", "Sales", "--resume", "sess-9", "hi"])
    assert result.exit_code == 1
    assert "is not the dashboard chat of 'Sales'" in result.output
    assert not core.calls_to("POST", "sessions/sess-9/messages/stream")
