"""Tests for the account workspace — `cinna account` / `cinna agent` / exec --agent."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import respx
from click.testing import CliRunner

from cinna.account import (
    AccountConfig,
    account_config_path,
    find_account_root,
    load_account_config,
    parse_account_setup_input,
    resolve_child_workspace,
    save_account_config,
)
from cinna.client import AccountClient
from cinna.config import CinnaConfig, lookup_agent_registry, save_config, upsert_agent_registry
from cinna.errors import AuthenticationError, PlatformError
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
    """An account workspace root with .cinna/account.json + agents/."""
    root = tmp_path / "my-cinna"
    root.mkdir()
    save_account_config(account_cfg, root)
    (root / "agents").mkdir()
    return root


def make_child_workspace(
    account_root: Path,
    name: str = "CRM Agent",
    agent_id: str = "agent-123",
    cli_token: str = "child-token-xyz",
    cli_token_id: str | None = "tok-id-1",
) -> tuple[Path, CinnaConfig]:
    """Materialize a fake synced child workspace under agents/."""
    from cinna.bootstrap import normalize_agent_dir_name

    ws = account_root / "agents" / normalize_agent_dir_name(name)
    ws.mkdir(parents=True)
    config = CinnaConfig(
        platform_url="https://platform.example.com",
        cli_token=cli_token,
        agent_id=agent_id,
        agent_name=name,
        environment_id="env-1",
        template="general-env",
        frontend_url="https://ui.example.com",
        cli_token_id=cli_token_id,
    )
    save_config(config, ws)
    upsert_agent_registry(
        agent_id, config.platform_url, cli_token, ws, frontend_url=config.frontend_url
    )
    return ws, config


# --- parse_account_setup_input ---


def test_parse_account_full_curl_command():
    raw = "curl -sL http://localhost:8000/api/cli-setup/account/TOK-abc123 | python3 -"
    url, token = parse_account_setup_input(raw)
    assert url == "http://localhost:8000/api"
    assert token == "TOK-abc123"


def test_parse_account_url_only():
    raw = "https://app.example.com/api/cli-setup/account/tok_abc"
    url, token = parse_account_setup_input(raw)
    assert url == "https://app.example.com/api"
    assert token == "tok_abc"


def test_parse_account_url_with_quotes():
    url, token = parse_account_setup_input("'https://h.example/api/cli-setup/account/t1'")
    assert url == "https://h.example/api"
    assert token == "t1"


def test_parse_account_raw_token_with_env(monkeypatch):
    monkeypatch.setenv("CINNA_PLATFORM_URL", "https://app.example.com")
    url, token = parse_account_setup_input("tok_abc123")
    assert url == "https://app.example.com"
    assert token == "tok_abc123"


def test_parse_account_raw_token_without_env(monkeypatch):
    monkeypatch.delenv("CINNA_PLATFORM_URL", raising=False)
    with pytest.raises(Exception, match="Cannot determine platform URL"):
        parse_account_setup_input("tok_abc123")


def test_parse_account_rejects_per_agent_url():
    """A per-agent setup URL (no /account/) must not be silently accepted."""
    with pytest.raises(Exception, match="Could not parse account setup URL"):
        parse_account_setup_input("https://h.example/api/cli-setup/tok_abc")


# --- default_account_dir_name ---


@pytest.mark.parametrize(
    "platform_url,expected",
    [
        ("https://demo-core.opencinna.io", "demo-core_opencinna_io"),
        ("https://demo-core.opencinna.io/api", "demo-core_opencinna_io"),
        ("http://localhost:8000/api", "localhost"),
        ("https://user:pass@app.example.com:443", "app_example_com"),
        ("", "my-cinna"),
    ],
)
def test_default_account_dir_name(platform_url, expected):
    from cinna.account import default_account_dir_name

    assert default_account_dir_name(platform_url) == expected


# --- account config I/O ---


def test_account_config_roundtrip_and_perms(tmp_path, account_cfg):
    save_account_config(account_cfg, tmp_path)
    path = account_config_path(tmp_path)
    assert path.is_file()
    assert (path.stat().st_mode & 0o777) == 0o600
    assert load_account_config(tmp_path) == account_cfg


def test_find_account_root_walks_up(account_root):
    nested = account_root / "agents" / "deep" / "dir"
    nested.mkdir(parents=True)
    assert find_account_root(nested) == account_root


def test_find_account_root_not_found(tmp_path):
    from cinna.errors import AccountConfigNotFoundError

    with pytest.raises(AccountConfigNotFoundError):
        find_account_root(tmp_path)


# --- context package fixture ---


def make_context_tarball(members: dict[str, str] | None = None) -> bytes:
    """Build an in-memory gzip tarball mimicking the context package
    (all members under a top-level ``context/`` prefix unless overridden)."""
    import io
    import tarfile

    if members is None:
        members = {
            "context/README.md": "# Context index\n",
            "context/platform/README.md": "# Feature map\n",
            "context/api_reference/README.md": "# API reference\n",
            "context/examples/platform_helper.py": "print('hello')\n",
        }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in members.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# --- cinna account setup ---


@patch("cinna.account.AccountClient")
@patch("cinna.account.httpx.post")
def test_account_setup_creates_workspace(
    mock_post, mock_client_cls, runner, tmp_path, monkeypatch
):
    """`cinna account setup` exchanges the token and writes account.json,
    agents/, the orchestrator CLAUDE.md, and the context/ package."""
    monkeypatch.chdir(tmp_path)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.download_context_package.return_value = make_context_tarball()

    captured: dict = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "account_token": "account-jwt-once",
                "platform_url": "https://platform.example.com",
                "frontend_url": "https://ui.example.com",
                "machine_name": "laptop",
            }

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["body"] = json
        return FakeResponse()

    mock_post.side_effect = fake_post

    result = runner.invoke(
        cli,
        [
            "account",
            "setup",
            "https://platform.example.com/api/cli-setup/account/SETUPTOK",
            "--name",
            "laptop",
        ],
    )
    assert result.exit_code == 0, result.output

    # Exchange hit the account endpoint with the machine name.
    assert captured["url"] == (
        "https://platform.example.com/api/cli-setup/account/SETUPTOK"
    )
    assert captured["body"]["machine_name"] == "laptop"

    # No --dir given → folder name defaults to the normalized platform domain.
    root = tmp_path / "platform_example_com"
    cfg = load_account_config(root)
    assert cfg.account_token == "account-jwt-once"
    assert cfg.platform_url == "https://platform.example.com"
    assert cfg.frontend_url == "https://ui.example.com"
    assert cfg.machine_name == "laptop"
    assert (account_config_path(root).stat().st_mode & 0o777) == 0o600
    assert (root / "agents").is_dir()
    assert "Account Workspace" in (root / "CLAUDE.md").read_text()
    assert "cinna account agents" in result.output

    # Pre-approved-tools config: Bash(cinna:*) so cinna commands don't prompt,
    # plus auto-approval of the cinna-managed .mcp.json servers.
    settings = json.loads((root / ".claude" / "settings.json").read_text())
    assert settings["permissions"]["allow"] == [
        "Bash(cinna:*)",
        "mcp__platform-knowledge",
    ]
    assert settings["enableAllProjectMcpServers"] is True

    # Knowledge MCP proxy wiring (.mcp.json / opencode.json) in account mode.
    mcp = json.loads((root / ".mcp.json").read_text())
    proxy = mcp["mcpServers"]["platform-knowledge"]
    assert proxy["command"] == "cinna"
    assert proxy["args"] == ["mcp-proxy"]
    assert proxy["env"]["CINNA_ACCOUNT_CONFIG"] == ".cinna/account.json"
    opencode = json.loads((root / "opencode.json").read_text())
    oc_proxy = opencode["mcp"]["platform-knowledge"]
    assert oc_proxy["command"] == ["cinna", "mcp-proxy"]
    assert oc_proxy["environment"]["CINNA_ACCOUNT_CONFIG"] == ".cinna/account.json"

    # Context package extracted under context/.
    mock_client.download_context_package.assert_called_once()
    assert (root / "context" / "README.md").read_text() == "# Context index\n"
    assert (root / "context" / "platform" / "README.md").is_file()
    assert (root / "context" / "api_reference" / "README.md").is_file()
    assert (root / "context" / "examples" / "platform_helper.py").is_file()
    assert "Context package installed" in result.output


@patch("cinna.account.httpx.post")
def test_account_setup_surfaces_backend_error(mock_post, runner, tmp_path, monkeypatch):
    """400s from the exchange (expired / used / kind mismatch) surface the
    backend detail verbatim and create nothing locally."""
    monkeypatch.chdir(tmp_path)

    class FakeResponse:
        status_code = 400
        text = ""

        def json(self):
            return {"detail": "Setup token has already been used"}

    mock_post.return_value = FakeResponse()

    result = runner.invoke(
        cli,
        [
            "account",
            "setup",
            "https://platform.example.com/api/cli-setup/account/USEDTOK",
            "--name",
            "laptop",
        ],
    )
    assert result.exit_code != 0
    assert "Setup token has already been used" in result.output
    assert not (tmp_path / "my-cinna").exists()


@patch("cinna.account.httpx.post")
def test_account_setup_refuses_existing_workspace(
    mock_post, runner, tmp_path, account_cfg, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "my-cinna"
    root.mkdir()
    save_account_config(account_cfg, root)

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "account_token": "t",
                "platform_url": "https://platform.example.com",
                "frontend_url": "https://ui.example.com",
                "machine_name": "laptop",
            }

    mock_post.return_value = FakeResponse()

    result = runner.invoke(
        cli,
        [
            "account",
            "setup",
            "https://platform.example.com/api/cli-setup/account/TOK",
            "--name",
            "laptop",
            "--dir",
            "my-cinna",
        ],
    )
    assert result.exit_code != 0
    assert "already contains a cinna account workspace" in result.output
    # The single-use setup token must not be burned on a doomed setup.
    mock_post.assert_not_called()


@patch("cinna.account.AccountClient")
@patch("cinna.account.httpx.post")
def test_account_setup_continues_on_context_failure(
    mock_post, mock_client_cls, runner, tmp_path, monkeypatch
):
    """A failed context-package download must not fail setup — the workspace
    is functional without it."""
    import httpx

    monkeypatch.chdir(tmp_path)

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "account_token": "account-jwt-once",
                "platform_url": "https://platform.example.com",
                "frontend_url": "https://ui.example.com",
                "machine_name": "laptop",
            }

    mock_post.return_value = FakeResponse()
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.download_context_package.side_effect = httpx.ConnectError(
        "connection refused"
    )

    result = runner.invoke(
        cli,
        [
            "account",
            "setup",
            "https://platform.example.com/api/cli-setup/account/TOK",
            "--name",
            "laptop",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Context package download failed" in result.output
    assert "refresh-context" in result.output

    # The workspace itself is fully materialized (domain-derived default dir).
    root = tmp_path / "platform_example_com"
    assert load_account_config(root).account_token == "account-jwt-once"
    assert (root / "agents").is_dir()
    assert (root / "CLAUDE.md").is_file()
    assert not (root / "context").exists()


@patch("cinna.account.AccountClient")
@patch("cinna.account.httpx.post")
def test_account_setup_explicit_dir_overrides_domain_default(
    mock_post, mock_client_cls, runner, tmp_path, monkeypatch
):
    """An explicit ``--dir`` wins over the domain-derived default."""
    monkeypatch.chdir(tmp_path)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.download_context_package.return_value = make_context_tarball()

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "account_token": "account-jwt-once",
                "platform_url": "https://platform.example.com",
                "frontend_url": "https://ui.example.com",
                "machine_name": "laptop",
            }

    mock_post.return_value = FakeResponse()

    result = runner.invoke(
        cli,
        [
            "account",
            "setup",
            "https://platform.example.com/api/cli-setup/account/TOK",
            "--name",
            "laptop",
            "--dir",
            "my-custom-folder",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "my-custom-folder" / ".cinna" / "account.json").is_file()
    assert not (tmp_path / "platform_example_com").exists()


# --- cinna account refresh-context ---


@patch("cinna.account.AccountClient")
def test_refresh_context_replaces_tree(
    mock_client_cls, runner, account_root, monkeypatch
):
    """refresh-context removes the old context/ tree and extracts fresh."""
    monkeypatch.chdir(account_root)

    # Stale tree with a file the fresh package no longer ships.
    stale = account_root / "context"
    (stale / "platform").mkdir(parents=True)
    (stale / "README.md").write_text("# OLD index\n")
    (stale / "stale-file.md").write_text("obsolete\n")

    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.download_context_package.return_value = make_context_tarball()

    result = runner.invoke(cli, ["account", "refresh-context"])
    assert result.exit_code == 0, result.output
    assert "Context package installed" in result.output

    assert (account_root / "context" / "README.md").read_text() == "# Context index\n"
    assert not (account_root / "context" / "stale-file.md").exists()
    assert (account_root / "context" / "examples" / "platform_helper.py").is_file()

    # refresh-context also regenerates the orchestrator CLAUDE.md from the
    # bundled template, so a CLI upgrade's new commands reach existing
    # workspaces without a full re-setup.
    claude_md = (account_root / "CLAUDE.md").read_text()
    assert "Orchestrator CLAUDE.md regenerated" in result.output
    assert "cinna account user-workspace list" in claude_md
    assert "cinna agent-api enable" in claude_md


@patch("cinna.account.AccountClient")
def test_refresh_context_regenerates_child_agent_claude_md(
    mock_client_cls, runner, account_root, monkeypatch
):
    """refresh-context re-renders each synced agent's per-agent CLAUDE.md from
    the bundled template, so a CLI upgrade's new guidance reaches existing
    child workspaces too — not just the orchestrator one."""
    monkeypatch.chdir(account_root)

    child, _cfg = make_child_workspace(account_root, name="CRM Agent")
    # A stale CLAUDE.md predating the template update.
    (child / "CLAUDE.md").write_text("# OLD per-agent guide\n")

    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.download_context_package.return_value = make_context_tarball()

    result = runner.invoke(cli, ["account", "refresh-context"])
    assert result.exit_code == 0, result.output
    assert "Regenerated CLAUDE.md for 1 synced agent workspace" in result.output

    regenerated = (child / "CLAUDE.md").read_text()
    assert "# OLD per-agent guide" not in regenerated
    assert "# Agent: CRM Agent" in regenerated
    # The freshly-shipped guidance (talking to the agent to test it) is present.
    assert "cinna chat" in regenerated

    # The companion human-testing guide reaches both the account root and every
    # synced child workspace.
    assert (account_root / "CHAT_TESTING.md").is_file()
    assert (child / "CHAT_TESTING.md").is_file()


@patch("cinna.account.AccountClient")
def test_refresh_context_failure_preserves_old_tree(
    mock_client_cls, runner, account_root, monkeypatch
):
    """The old tree is only removed after a successful download — a failed
    refresh warns and leaves the previous context/ intact."""
    import httpx

    monkeypatch.chdir(account_root)
    stale = account_root / "context"
    stale.mkdir()
    (stale / "README.md").write_text("# OLD index\n")

    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.download_context_package.side_effect = httpx.ConnectError("boom")

    result = runner.invoke(cli, ["account", "refresh-context"])
    assert result.exit_code == 0, result.output
    assert "Context package download failed" in result.output
    assert (account_root / "context" / "README.md").read_text() == "# OLD index\n"


@patch("cinna.account.AccountClient")
def test_refresh_context_self_heals_claude_settings(
    mock_client_cls, runner, account_root, monkeypatch
):
    """refresh-context creates .claude/settings.json if it's missing."""
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.download_context_package.return_value = make_context_tarball()

    assert not (account_root / ".claude" / "settings.json").exists()
    result = runner.invoke(cli, ["account", "refresh-context"])
    assert result.exit_code == 0, result.output

    settings = json.loads((account_root / ".claude" / "settings.json").read_text())
    assert settings["permissions"]["allow"] == [
        "Bash(cinna:*)",
        "mcp__platform-knowledge",
    ]
    assert settings["enableAllProjectMcpServers"] is True


@patch("cinna.account.AccountClient")
def test_refresh_context_preserves_user_claude_settings(
    mock_client_cls, runner, account_root, monkeypatch
):
    """A user-edited .claude/settings.json is never clobbered by refresh."""
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.download_context_package.return_value = make_context_tarball()

    claude_dir = account_root / ".claude"
    claude_dir.mkdir()
    custom = {"permissions": {"allow": ["Bash(cinna:*)", "Bash(git:*)"]}}
    (claude_dir / "settings.json").write_text(json.dumps(custom))

    result = runner.invoke(cli, ["account", "refresh-context"])
    assert result.exit_code == 0, result.output
    # Untouched — the user's extra permission survives.
    assert json.loads((claude_dir / "settings.json").read_text()) == custom


def test_refresh_context_outside_account_workspace(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli, ["account", "refresh-context"])
    assert result.exit_code != 0
    assert "Not in a cinna account workspace" in result.output


@patch("cinna.account.AccountClient")
def test_context_extraction_rejects_malicious_members(
    mock_client_cls, account_root, account_cfg
):
    """Absolute and traversal member names are skipped (reuses the workspace
    clone's safe extractor); safe members still extract."""
    from cinna.account import _install_context_package

    evil_outside = account_root.parent / "evil.txt"
    malicious = make_context_tarball(
        {
            "../evil.txt": "escaped\n",
            "/abs.txt": "absolute\n",
            "context/README.md": "# safe\n",
        }
    )
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.download_context_package.return_value = malicious

    ok = _install_context_package(account_cfg, account_root)
    assert ok is True

    assert (account_root / "context" / "README.md").read_text() == "# safe\n"
    assert not evil_outside.exists()
    assert not (account_root / "evil.txt").exists()
    assert not (account_root / "abs.txt").exists()
    assert not Path("/abs.txt").exists()


# --- knowledge MCP proxy wiring ---


def test_write_account_mcp_config_wires_account_mode(account_root):
    """`_write_account_mcp_config` writes .mcp.json + opencode.json pointing the
    proxy at account.json via CINNA_ACCOUNT_CONFIG (account-mode knowledge tool)."""
    from cinna.account import _write_account_mcp_config

    _write_account_mcp_config(account_root)

    # Relative path (anchored at the workspace folder) so the folder is portable.
    expected_cfg = ".cinna/account.json"

    mcp = json.loads((account_root / ".mcp.json").read_text())
    proxy = mcp["mcpServers"]["platform-knowledge"]
    assert proxy["command"] == "cinna"
    assert proxy["args"] == ["mcp-proxy"]
    assert proxy["env"]["CINNA_ACCOUNT_CONFIG"] == expected_cfg

    opencode = json.loads((account_root / "opencode.json").read_text())
    oc = opencode["mcp"]["platform-knowledge"]
    assert oc["type"] == "local"
    assert oc["command"] == ["cinna", "mcp-proxy"]
    assert oc["enabled"] is True
    assert oc["environment"]["CINNA_ACCOUNT_CONFIG"] == expected_cfg


def test_refresh_context_regenerates_mcp_config(account_root, account_cfg):
    """`cinna account refresh-context` self-heals the MCP wiring so a CLI
    upgrade reaches existing account workspaces."""
    from cinna.account import run_account_refresh_context

    # Simulate a workspace set up before the MCP wiring shipped.
    assert not (account_root / ".mcp.json").exists()

    with patch("cinna.account.find_account_root", return_value=account_root), patch(
        "cinna.account._install_context_package", return_value=True
    ):
        run_account_refresh_context()

    assert (account_root / ".mcp.json").is_file()
    mcp = json.loads((account_root / ".mcp.json").read_text())
    assert "platform-knowledge" in mcp["mcpServers"]


def test_mcp_proxy_account_mode_builds_account_server(account_root, monkeypatch):
    """`run_mcp_proxy` in account mode (CINNA_ACCOUNT_CONFIG set) loads the
    account config and builds a platform-knowledge server — never touching the
    per-agent CINNA_CONFIG path."""
    from cinna import mcp_proxy

    monkeypatch.setenv("CINNA_ACCOUNT_CONFIG", str(account_config_path(account_root)))
    monkeypatch.delenv("CINNA_CONFIG", raising=False)

    captured: dict = {}

    def fake_account_server(cfg):
        captured["account_cfg"] = cfg
        return object()

    def boom(*a, **k):  # per-agent path must not run
        raise AssertionError("per-agent create_mcp_server called in account mode")

    async def fake_run():
        captured["served"] = True

    monkeypatch.setattr(mcp_proxy, "create_account_mcp_server", fake_account_server)
    monkeypatch.setattr(mcp_proxy, "create_mcp_server", boom)
    # Avoid spinning up a real stdio transport — replace asyncio.run.
    monkeypatch.setattr(mcp_proxy.asyncio, "run", lambda coro: coro.close())

    mcp_proxy.run_mcp_proxy()

    assert captured["account_cfg"].account_token == account_cfg_token(account_root)
    assert captured["account_cfg"].platform_url == "https://platform.example.com"


def account_cfg_token(account_root: Path) -> str:
    return load_account_config(account_root).account_token


# --- move-tolerant proxy context resolution ---


def test_proxy_context_relative_path_resolved_from_cwd(account_root, monkeypatch):
    """A relative CINNA_ACCOUNT_CONFIG resolves against the launch cwd (the
    workspace folder MCP clients cd into) — the portable form newer configs write."""
    from cinna.mcp_proxy import _resolve_proxy_context

    monkeypatch.chdir(account_root)
    monkeypatch.setenv("CINNA_ACCOUNT_CONFIG", ".cinna/account.json")
    monkeypatch.delenv("CINNA_CONFIG", raising=False)

    mode, root = _resolve_proxy_context()
    assert mode == "account"
    assert root == account_root.resolve()


def test_proxy_context_stale_absolute_path_heals_via_cwd(account_root, monkeypatch):
    """A stale absolute CINNA_ACCOUNT_CONFIG (folder moved) falls back to walking
    up from cwd, so an unregenerated legacy config keeps working after a move."""
    from cinna.mcp_proxy import _resolve_proxy_context

    monkeypatch.chdir(account_root)
    monkeypatch.setenv(
        "CINNA_ACCOUNT_CONFIG", "/old/location/my-cinna/.cinna/account.json"
    )
    monkeypatch.delenv("CINNA_CONFIG", raising=False)

    mode, root = _resolve_proxy_context()
    assert mode == "account"
    assert root == account_root.resolve()


def test_proxy_context_autodetects_nearest_cinna_without_env(account_root, monkeypatch):
    """With no env hint, the proxy auto-detects the nearest .cinna/ from cwd."""
    from cinna.mcp_proxy import _resolve_proxy_context

    monkeypatch.chdir(account_root)
    monkeypatch.delenv("CINNA_ACCOUNT_CONFIG", raising=False)
    monkeypatch.delenv("CINNA_CONFIG", raising=False)

    mode, root = _resolve_proxy_context()
    assert mode == "account"
    assert root == account_root.resolve()


# --- cinna account agents ---


AGENTS_LISTING = {
    "count": 3,
    "data": [
        {
            "id": "agent-123",
            "name": "CRM Agent",
            "description": "crm",
            "can_build": True,
            "is_foreign_install": False,
            "has_active_environment": True,
        },
        {
            "id": "agent-456",
            "name": "Installed Bundle",
            "description": None,
            "can_build": False,
            "is_foreign_install": True,
            "has_active_environment": False,
        },
        {
            "id": "agent-789",
            "name": "HR Manager Agent",
            "description": None,
            "can_build": True,
            "is_foreign_install": False,
            "has_active_environment": False,
        },
    ],
}


@patch("cinna.account.AccountClient")
def test_account_agents_renders_table(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    monkeypatch.setenv("COLUMNS", "240")
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING

    # One agent already synced locally.
    make_child_workspace(account_root, name="CRM Agent", agent_id="agent-123")

    result = runner.invoke(cli, ["account", "agents"])
    assert result.exit_code == 0, result.output
    assert "CRM Agent" in result.output
    assert "agent-123" in result.output
    assert "can build" in result.output
    assert "foreign install" in result.output
    assert "agents/crm-agent/" in result.output
    assert "not synced" in result.output


@patch("cinna.account.AccountClient")
def test_account_agents_outside_account_workspace(
    mock_client_cls, runner, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli, ["account", "agents"])
    assert result.exit_code != 0
    assert "Not in a cinna account workspace" in result.output
    mock_client_cls.assert_not_called()


# Listing with per-row workspace ids for the workspace-scoping tests.
_WS_AGENTS_LISTING = {
    "count": 3,
    "data": [
        {
            "id": "agent-eng-1",
            "name": "Eng Alpha",
            "user_workspace_id": "ws-eng",
            "can_build": True,
            "is_foreign_install": False,
            "has_active_environment": True,
        },
        {
            "id": "agent-eng-2",
            "name": "Eng Beta",
            "user_workspace_id": "ws-eng",
            "can_build": True,
            "is_foreign_install": False,
            "has_active_environment": False,
        },
        {
            "id": "agent-default-1",
            "name": "Loose Agent",
            "user_workspace_id": None,
            "can_build": True,
            "is_foreign_install": False,
            "has_active_environment": False,
        },
    ],
}


def _save_cfg_with_workspace(account_root, ws_id, ws_name):
    """Persist an account config bound to a given active workspace."""
    save_account_config(
        AccountConfig(
            platform_url="https://platform.example.com",
            frontend_url="https://ui.example.com",
            account_token="account-token-abc",
            machine_name="laptop",
            user_workspace_id=ws_id,
            user_workspace_name=ws_name,
        ),
        account_root,
    )


@patch("cinna.account.AccountClient")
def test_account_agents_scoped_to_active_workspace(
    mock_client_cls, runner, account_root, monkeypatch
):
    """Default listing is scoped to the active workspace; header names it."""
    monkeypatch.chdir(account_root)
    monkeypatch.setenv("COLUMNS", "240")
    _save_cfg_with_workspace(account_root, "ws-eng", "Engineering")
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = _WS_AGENTS_LISTING

    result = runner.invoke(cli, ["account", "agents"])
    assert result.exit_code == 0, result.output
    assert "workspace: Engineering" in result.output
    assert "2 of 3 accessible" in result.output
    assert "Eng Alpha" in result.output
    assert "Eng Beta" in result.output
    # The Default-workspace agent is hidden under the scoped view.
    assert "Loose Agent" not in result.output


@patch("cinna.account.AccountClient")
def test_account_agents_all_flag_shows_every_workspace(
    mock_client_cls, runner, account_root, monkeypatch
):
    """--all ignores the active workspace and lists every agent."""
    monkeypatch.chdir(account_root)
    monkeypatch.setenv("COLUMNS", "240")
    _save_cfg_with_workspace(account_root, "ws-eng", "Engineering")
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = _WS_AGENTS_LISTING

    result = runner.invoke(cli, ["account", "agents", "--all"])
    assert result.exit_code == 0, result.output
    assert "all agents" in result.output
    assert "Eng Alpha" in result.output
    assert "Loose Agent" in result.output


@patch("cinna.account.AccountClient")
def test_account_agents_default_workspace_scope(
    mock_client_cls, runner, account_root, monkeypatch
):
    """With no active workspace, the scope is the Default (unassigned) set."""
    monkeypatch.chdir(account_root)
    monkeypatch.setenv("COLUMNS", "240")
    # account_root fixture already wrote a Default (no-workspace) config.
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = _WS_AGENTS_LISTING

    result = runner.invoke(cli, ["account", "agents"])
    assert result.exit_code == 0, result.output
    assert "Default (unassigned)" in result.output
    assert "Loose Agent" in result.output
    assert "Eng Alpha" not in result.output


@patch("cinna.account.AccountClient")
def test_account_agents_empty_active_workspace(
    mock_client_cls, runner, account_root, monkeypatch
):
    """An active workspace with no agents prints a helpful hint, not a table."""
    monkeypatch.chdir(account_root)
    _save_cfg_with_workspace(account_root, "ws-empty", "Empty WS")
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = _WS_AGENTS_LISTING

    result = runner.invoke(cli, ["account", "agents"])
    assert result.exit_code == 0, result.output
    assert "No agents in workspace 'Empty WS'" in result.output
    assert "--all" in result.output


# --- cinna account status ---


@patch("cinna.account.probe_account_token")
def test_account_status_valid_token(mock_probe, runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    monkeypatch.setenv("COLUMNS", "240")
    mock_probe.return_value = "valid"
    make_child_workspace(account_root)

    result = runner.invoke(cli, ["account", "status"])
    assert result.exit_code == 0, result.output
    assert "https://platform.example.com" in result.output
    assert "laptop" in result.output
    assert "valid token" in result.output
    assert "agents/crm-agent/" in result.output


@patch("cinna.account.probe_account_token")
def test_account_status_expired_token(mock_probe, runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    mock_probe.return_value = "expired"

    result = runner.invoke(cli, ["account", "status"])
    assert result.exit_code == 0, result.output
    assert "expired token" in result.output


def test_probe_account_token_classification(account_cfg, monkeypatch):
    import httpx

    class FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code

    responses = {}

    def fake_get(url, headers=None, timeout=None, follow_redirects=True):
        assert url == "https://platform.example.com/api/v1/cli/account/agents"
        assert headers["Authorization"] == "Bearer account-token-abc"
        result = responses["next"]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(httpx, "get", fake_get)

    from cinna.account import probe_account_token

    responses["next"] = FakeResponse(200)
    assert probe_account_token(account_cfg) == "valid"
    responses["next"] = FakeResponse(401)
    assert probe_account_token(account_cfg) == "expired"
    responses["next"] = RuntimeError("boom")
    assert probe_account_token(account_cfg) == "unreachable"


# --- cinna agent sync ---


MINT_RESPONSE = {
    "token": "child-jwt-shown-once",
    "id": "tok-uuid-1",
    "agent_id": "agent-789",
    "owner_id": "user-1",
    "prefix": "child-jwt-sh",
    "expires_at": "2026-06-18T00:00:00Z",
    "agent_name": "HR Manager Agent",
    "environment_id": "env-789",
    "template": "general-env",
    "frontend_url": "https://ui.example.com",
    "knowledge_sources": [],
}


@patch("cinna.account.provision_workspace")
@patch("cinna.account.AccountClient")
def test_agent_sync_mints_and_bootstraps(
    mock_client_cls, mock_provision, runner, account_root, monkeypatch
):
    """`cinna agent sync` mints a child token then delegates to the standard
    per-agent bootstrap writer (config + registry + provisioning)."""
    from cinna.config import load_config

    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.mint_agent_token.return_value = MINT_RESPONSE

    result = runner.invoke(cli, ["agent", "sync", "HR Manager Agent"])
    assert result.exit_code == 0, result.output

    # Mint hit the resolved agent with the account machine name.
    mock_client.mint_agent_token.assert_called_once()
    (agent_id, machine_name, _machine_info) = mock_client.mint_agent_token.call_args[0]
    assert agent_id == "agent-789"
    assert machine_name == "laptop"

    # Standard workspace written under agents/<slug>/ with the minted token.
    ws = account_root / "agents" / "hr-manager-agent"
    config = load_config(ws)
    assert config.cli_token == "child-jwt-shown-once"
    assert config.cli_token_id == "tok-uuid-1"
    assert config.agent_id == "agent-789"
    assert config.agent_name == "HR Manager Agent"
    assert config.environment_id == "env-789"
    assert config.template == "general-env"
    assert config.platform_url == "https://platform.example.com"
    assert config.frontend_url == "https://ui.example.com"

    # Registry entry — `cinna list` and the SSH shim keep working.
    entry = lookup_agent_registry("agent-789")
    assert entry is not None
    assert entry["cli_token"] == "child-jwt-shown-once"
    assert entry["workspace_path"] == str(ws)

    # Bootstrap delegation: the shared provisioning path ran on this workspace.
    mock_provision.assert_called_once()
    _client_arg, config_arg, root_arg = mock_provision.call_args[0]
    assert config_arg.agent_id == "agent-789"
    assert root_arg == ws

    assert "cd agents/hr-manager-agent/" in result.output


@patch("cinna.account.provision_workspace")
@patch("cinna.account.AccountClient")
def test_agent_sync_resolves_by_id_and_slug(
    mock_client_cls, mock_provision, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.mint_agent_token.return_value = MINT_RESPONSE

    result = runner.invoke(cli, ["agent", "sync", "hr-manager-agent"])
    assert result.exit_code == 0, result.output
    assert mock_client.mint_agent_token.call_args[0][0] == "agent-789"


@patch("cinna.account.provision_workspace")
@patch("cinna.account.AccountClient")
def test_agent_sync_unknown_agent(
    mock_client_cls, mock_provision, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING

    result = runner.invoke(cli, ["agent", "sync", "nope"])
    assert result.exit_code != 0
    assert "No accessible agent matches 'nope'" in result.output
    mock_client.mint_agent_token.assert_not_called()
    mock_provision.assert_not_called()


@patch("cinna.account.provision_workspace")
@patch("cinna.account.AccountClient")
def test_agent_sync_surfaces_mint_403_verbatim(
    mock_client_cls, mock_provision, runner, account_root, monkeypatch
):
    """Foreign installs etc. are rejected by the backend — the CLI surfaces
    the 403 detail verbatim instead of pre-judging client-side."""
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.mint_agent_token.side_effect = PlatformError(
        403,
        "This is an installed bundle; its workspace is publisher-managed "
        "and can't be synced for local development.",
    )

    result = runner.invoke(cli, ["agent", "sync", "Installed Bundle"])
    assert result.exit_code != 0
    assert "publisher-managed" in result.output
    mock_provision.assert_not_called()


@patch("cinna.account.provision_workspace")
@patch("cinna.account.AccountClient")
def test_agent_sync_refuses_already_synced(
    mock_client_cls, mock_provision, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    make_child_workspace(account_root, name="CRM Agent", agent_id="agent-123")
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING

    result = runner.invoke(cli, ["agent", "sync", "CRM Agent"])
    assert result.exit_code != 0
    assert "already a synced workspace" in result.output
    mock_client.mint_agent_token.assert_not_called()


# --- cinna agent unsync ---


@patch("cinna.account.AccountClient")
@patch("cinna.account.sync_session.stop")
def test_agent_unsync_revokes_and_disconnects(
    mock_stop, mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    ws, config = make_child_workspace(account_root)
    # Generated files + a user file that must survive.
    (ws / "CLAUDE.md").write_text("generated")
    (ws / "BUILDING_AGENT.md").write_text("generated")
    (ws / "mutagen.yml").write_text("generated")
    (ws / "workspace" / "scripts").mkdir(parents=True)
    (ws / "workspace" / "scripts" / "main.py").write_text("print('keep me')")

    mock_client = mock_client_cls.return_value.__enter__.return_value

    result = runner.invoke(cli, ["agent", "unsync", "crm-agent"], input="y\n")
    assert result.exit_code == 0, result.output

    mock_stop.assert_called_once()
    mock_client.revoke_child_token.assert_called_once_with("tok-id-1")

    # Disconnect-equivalent teardown: .cinna + generated files gone,
    # user files and the directory itself preserved.
    assert not (ws / ".cinna").exists()
    assert not (ws / "CLAUDE.md").exists()
    assert not (ws / "BUILDING_AGENT.md").exists()
    assert not (ws / "mutagen.yml").exists()
    assert (ws / "workspace" / "scripts" / "main.py").exists()
    assert lookup_agent_registry(config.agent_id) is None


@patch("cinna.account.AccountClient")
@patch("cinna.account.sync_session.stop")
def test_agent_unsync_warns_when_revoke_404(
    mock_stop, mock_client_cls, runner, account_root, monkeypatch
):
    """A 404 from the children endpoint (e.g. a workspace predating
    provenance tracking, or a token not minted by this account token) must
    not block the local teardown."""
    monkeypatch.chdir(account_root)
    ws, config = make_child_workspace(account_root)

    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.revoke_child_token.side_effect = PlatformError(404, "Token not found")

    result = runner.invoke(cli, ["agent", "unsync", "crm-agent"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "Server-side token revoke failed" in result.output
    assert "Token not found" in result.output
    assert not (ws / ".cinna").exists()
    assert lookup_agent_registry(config.agent_id) is None


@patch("cinna.account.AccountClient")
@patch("cinna.account.sync_session.stop")
def test_agent_unsync_warns_when_revoke_unreachable(
    mock_stop, mock_client_cls, runner, account_root, monkeypatch
):
    """A network failure on the revoke call must not block the local teardown."""
    import httpx

    monkeypatch.chdir(account_root)
    ws, config = make_child_workspace(account_root)

    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.revoke_child_token.side_effect = httpx.ConnectError(
        "connection refused"
    )

    result = runner.invoke(cli, ["agent", "unsync", "crm-agent"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "Server-side token revoke failed" in result.output
    assert not (ws / ".cinna").exists()
    assert lookup_agent_registry(config.agent_id) is None


@patch("cinna.account.AccountClient")
@patch("cinna.account.sync_session.stop")
def test_agent_unsync_skips_revoke_without_token_id(
    mock_stop, mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    make_child_workspace(account_root, cli_token_id=None)
    mock_client = mock_client_cls.return_value.__enter__.return_value

    result = runner.invoke(cli, ["agent", "unsync", "crm-agent"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "skipping server-side revoke" in result.output
    mock_client.revoke_child_token.assert_not_called()


def test_agent_unsync_unknown_workspace(runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    result = runner.invoke(cli, ["agent", "unsync", "ghost"], input="y\n")
    assert result.exit_code != 0
    assert "No synced workspace matches 'ghost'" in result.output


# --- cinna exec --agent ---


@patch("cinna.main._run_remote_exec")
def test_exec_agent_resolves_child_token(
    mock_exec, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    make_child_workspace(account_root)
    mock_exec.return_value = 0

    result = runner.invoke(
        cli, ["exec", "--agent", "crm-agent", "python", "scripts/main.py"]
    )
    assert result.exit_code == 0, result.output
    mock_exec.assert_called_once()
    config_arg, command_arg = mock_exec.call_args[0]
    assert config_arg.agent_id == "agent-123"
    assert config_arg.cli_token == "child-token-xyz"
    assert command_arg == "python scripts/main.py"


@patch("cinna.main._run_remote_exec")
def test_exec_agent_requires_synced_workspace(
    mock_exec, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)

    result = runner.invoke(cli, ["exec", "--agent", "ghost", "echo", "hi"])
    assert result.exit_code != 0
    assert "not synced" in result.output
    mock_exec.assert_not_called()


@patch("cinna.main._run_remote_exec")
def test_exec_agent_works_by_display_name(
    mock_exec, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    make_child_workspace(account_root, name="CRM Agent", agent_id="agent-123")
    mock_exec.return_value = 0

    result = runner.invoke(cli, ["exec", "--agent", "CRM Agent", "ls"])
    assert result.exit_code == 0, result.output
    assert mock_exec.call_args[0][0].agent_id == "agent-123"


# --- resolve_child_workspace ---


def test_resolve_child_workspace_by_id_slug_and_name(account_root):
    ws, config = make_child_workspace(account_root, name="HR Manager Agent", agent_id="agent-789")

    for ref in ("agent-789", "hr-manager-agent", "HR Manager Agent"):
        resolved = resolve_child_workspace(account_root, ref)
        assert resolved is not None, ref
        assert resolved[0] == ws

    assert resolve_child_workspace(account_root, "other") is None


# --- AccountClient (HTTP-level) ---


@pytest.fixture
def account_client(account_cfg):
    c = AccountClient(account_cfg)
    yield c
    c.close()


@respx.mock
def test_account_client_list_agents(account_client):
    route = respx.get(
        "https://platform.example.com/api/v1/cli/account/agents"
    ).respond(200, json=AGENTS_LISTING)
    result = account_client.list_account_agents()
    assert result["count"] == 3
    assert route.calls[0].request.headers["Authorization"] == "Bearer account-token-abc"


@respx.mock
def test_account_client_mint(account_client):
    route = respx.post(
        "https://platform.example.com/api/v1/cli/account/agents/agent-789/mint"
    ).respond(200, json=MINT_RESPONSE)
    result = account_client.mint_agent_token("agent-789", "laptop", "Darwin/arm64")
    assert result["token"] == "child-jwt-shown-once"
    body = json.loads(route.calls[0].request.content)
    assert body == {"machine_name": "laptop", "machine_info": "Darwin/arm64"}


@respx.mock
def test_account_client_download_context_package(account_client):
    archive = make_context_tarball()
    route = respx.get(
        "https://platform.example.com/api/v1/cli/account/context-package"
    ).respond(
        200,
        content=archive,
        headers={"content-type": "application/tar+gzip"},
    )
    result = account_client.download_context_package()
    assert result == archive
    assert route.calls[0].request.headers["Authorization"] == "Bearer account-token-abc"


@respx.mock
def test_account_client_context_package_401(account_client):
    respx.get(
        "https://platform.example.com/api/v1/cli/account/context-package"
    ).respond(401, json={"detail": "CLI token has been revoked"})
    with pytest.raises(AuthenticationError, match="CLI token has been revoked"):
        account_client.download_context_package()


@respx.mock
def test_account_client_revoke_child_token(account_client):
    route = respx.delete(
        "https://platform.example.com/api/v1/cli/account/tokens/children/tok-id-1"
    ).respond(200, json={"message": "CLI token revoked successfully"})
    result = account_client.revoke_child_token("tok-id-1")
    assert "revoked" in result["message"]
    assert route.calls[0].request.headers["Authorization"] == "Bearer account-token-abc"


@respx.mock
def test_account_client_revoke_child_token_404(account_client):
    """Non-child / unknown ids → 404 with the backend detail verbatim
    (no existence leak rewording client-side)."""
    respx.delete(
        "https://platform.example.com/api/v1/cli/account/tokens/children/tok-other"
    ).respond(404, json={"detail": "Token not found"})
    with pytest.raises(PlatformError, match="Token not found"):
        account_client.revoke_child_token("tok-other")


@respx.mock
def test_account_client_401_raises_auth_error(account_client):
    """Revoked / expired account tokens surface as AuthenticationError with
    the backend detail."""
    respx.get("https://platform.example.com/api/v1/cli/account/agents").respond(
        401, json={"detail": "CLI token has been revoked"}
    )
    with pytest.raises(AuthenticationError, match="CLI token has been revoked"):
        account_client.list_account_agents()


@respx.mock
def test_account_client_404_detail_verbatim(account_client):
    """No existence leak rewording — 404 detail comes through verbatim."""
    respx.post(
        "https://platform.example.com/api/v1/cli/account/agents/agent-x/mint"
    ).respond(404, json={"detail": "Agent not found"})
    with pytest.raises(PlatformError, match="Agent not found"):
        account_client.mint_agent_token("agent-x", "laptop", None)


@patch("cinna.account.AccountClient")
def test_account_agents_surfaces_expired_token(
    mock_client_cls, runner, account_root, monkeypatch
):
    """An expired/revoked account token aborts with the backend detail."""
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.side_effect = AuthenticationError(
        "CLI token has expired"
    )

    result = runner.invoke(cli, ["account", "agents"])
    assert result.exit_code != 0
    assert "CLI token has expired" in result.output


# --- cinna agent create ---


@patch("cinna.account.AccountClient")
def test_agent_create_prints_record_and_hint(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.create_agent.return_value = {
        "id": "agent-new-1",
        "name": "CRM Agent",
        "description": "crm",
    }

    result = runner.invoke(
        cli, ["agent", "create", "CRM Agent", "--description", "crm"]
    )
    assert result.exit_code == 0, result.output
    mock_client.create_agent.assert_called_once_with(
        "CRM Agent", "crm", user_workspace_id=None
    )
    assert "agent-new-1" in result.output
    assert "https://ui.example.com/agent/agent-new-1" in result.output
    assert "cinna agent sync crm-agent" in result.output


@patch("cinna.account.AccountClient")
def test_agent_create_403_surfaces_detail(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.create_agent.side_effect = PlatformError(
        403, "Creating agents requires the agent-developer role"
    )

    result = runner.invoke(cli, ["agent", "create", "CRM Agent"])
    assert result.exit_code != 0
    assert "agent-developer role" in result.output + result.stderr


# --- cinna connect agent-api ---


@patch("cinna.account.AccountClient")
def test_connect_agent_api_happy(mock_client_cls, runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.connect_agent_api.return_value = {
        "credential_id": "cred-1",
        "token_id": "tok-1",
        "token_prefix": "agk_abc",
        "base_url": "https://platform.example.com/api/agent-api/agent-123",
        "spec_url": "https://platform.example.com/api/agent-api/agent-123/openapi.json",
        "linked_consumer_agent_id": "agent-789",
    }

    result = runner.invoke(
        cli,
        [
            "connect",
            "agent-api",
            "--producer",
            "CRM Agent",
            "--consumer",
            "hr-manager-agent",
            "--label",
            "crm-link",
            "--read-only",
        ],
    )
    assert result.exit_code == 0, result.output
    mock_client.connect_agent_api.assert_called_once_with(
        "agent-123",
        "agent-789",
        credential_label="crm-link",
        read_only_override=True,
    )
    assert "cred-1" in result.output
    assert "https://platform.example.com/api/agent-api/agent-123" in result.output
    assert "credential sync" in result.output


@patch("cinna.account.AccountClient")
def test_connect_agent_api_unknown_producer(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING

    result = runner.invoke(
        cli,
        ["connect", "agent-api", "--producer", "ghost", "--consumer", "CRM Agent"],
    )
    assert result.exit_code != 0
    assert "No accessible agent matches 'ghost'" in result.output + result.stderr
    mock_client.connect_agent_api.assert_not_called()


@patch("cinna.account.AccountClient")
def test_connect_agent_api_surfaces_400_verbatim(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.connect_agent_api.side_effect = PlatformError(
        400, "The producer agent's REST API is disabled"
    )

    result = runner.invoke(
        cli,
        ["connect", "agent-api", "--producer", "CRM Agent", "--consumer", "agent-789"],
    )
    assert result.exit_code != 0
    assert "REST API is disabled" in result.output + result.stderr


# --- cinna connect mcp ---


DISCOVERABLE_MCP = {
    "count": 2,
    "data": [
        {
            "agent_id": "agent-123",
            "agent_name": "CRM Agent",
            "connector_id": "conn-1",
            "connector_name": "crm-a2a",
            "mode": "direct",
            "ui_color_preset": "violet",
        },
        {
            "agent_id": "agent-555",
            "agent_name": "Billing Agent",
            "connector_id": "conn-2",
            "connector_name": "billing-a2a",
            "mode": "oauth_dcr",
            "ui_color_preset": "teal",
        },
    ],
}


@patch("cinna.account.AccountClient")
def test_connect_mcp_happy_with_authorize_url(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.list_discoverable_mcp.return_value = DISCOVERABLE_MCP
    mock_client.connect_mcp.return_value = {
        "credential_id": "cred-mcp-1",
        "auth_mode": "direct",
        "endpoint_url": "https://platform.example.com/mcp/serve/conn-1",
        "transport": "http",
        "status": "connected",
        "linked_consumer_agent_id": "agent-789",
        "authorize_url": "https://platform.example.com/oauth/authorize?x=1",
    }

    result = runner.invoke(
        cli,
        [
            "connect",
            "mcp",
            "--producer",
            "crm-agent",
            "--consumer",
            "HR Manager Agent",
            "--building-only",
        ],
    )
    assert result.exit_code == 0, result.output
    # Consumer resolved against the agents listing; producer against the
    # discoverable listing (queried with the consumer id).
    mock_client.list_discoverable_mcp.assert_called_once_with("agent-789")
    mock_client.connect_mcp.assert_called_once_with(
        "conn-1",
        "agent-789",
        mcp_mode_conversation=False,
        mcp_mode_building=True,
        label=None,
    )
    assert "cred-mcp-1" in result.output
    assert "https://platform.example.com/mcp/serve/conn-1" in result.output
    assert "https://platform.example.com/oauth/authorize?x=1" in result.output


@patch("cinna.account.AccountClient")
def test_connect_mcp_no_discoverable_match(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.list_discoverable_mcp.return_value = DISCOVERABLE_MCP

    result = runner.invoke(
        cli,
        ["connect", "mcp", "--producer", "Installed Bundle", "--consumer", "CRM Agent"],
    )
    assert result.exit_code != 0
    combined = result.output + result.stderr
    assert "No discoverable agent2agent MCP connector" in combined
    # Error lists the discoverable options.
    assert "CRM Agent" in combined
    assert "Billing Agent" in combined
    mock_client.connect_mcp.assert_not_called()


@patch("cinna.account.AccountClient")
def test_connect_mcp_ambiguous_producer(
    mock_client_cls, runner, account_root, monkeypatch
):
    """An agent exposing several discoverable connectors cannot be picked by
    name — the error lists the connector ids."""
    monkeypatch.chdir(account_root)
    two_connectors = {
        "count": 2,
        "data": [
            {**DISCOVERABLE_MCP["data"][0]},
            {**DISCOVERABLE_MCP["data"][0], "connector_id": "conn-9", "connector_name": "crm-a2a-2"},
        ],
    }
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.list_discoverable_mcp.return_value = two_connectors

    result = runner.invoke(
        cli,
        ["connect", "mcp", "--producer", "CRM Agent", "--consumer", "agent-789"],
    )
    assert result.exit_code != 0
    combined = result.output + result.stderr
    assert "more than one discoverable" in combined
    assert "conn-1" in combined and "conn-9" in combined
    mock_client.connect_mcp.assert_not_called()


def test_connect_mcp_mode_flags_mutually_exclusive(runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    result = runner.invoke(
        cli,
        [
            "connect",
            "mcp",
            "--producer",
            "a",
            "--consumer",
            "b",
            "--conversation-only",
            "--building-only",
        ],
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output + result.stderr


# --- cinna api (escape hatch) ---


def _api_response(
    status_code: int,
    json_body=None,
    headers: dict | None = None,
    proxied: bool = True,
):
    """Build a fake api-proxy response.

    ``proxied=True`` (the default) stamps the ``X-Cinna-Proxied`` header the
    backend sets on every mirrored inner-API passthrough; ``proxied=False``
    omits it to simulate the hatch's own refusals (policy / limit / size cap).
    """
    import httpx as _httpx

    hdrs = dict(headers or {})
    if proxied:
        hdrs.setdefault("X-Cinna-Proxied", "1")
    if json_body is not None:
        return _httpx.Response(status_code, json=json_body, headers=hdrs)
    return _httpx.Response(status_code, text="", headers=hdrs)


@patch("cinna.account.AccountClient")
def test_api_2xx_pretty_prints_and_exits_0(
    mock_client_cls, runner, account_root, monkeypatch
):
    """Proxied header present + 2xx → body pretty-printed, exit 0."""
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.api_proxy.return_value = _api_response(
        200, {"count": 1, "data": [{"id": "agent-123"}]}
    )

    result = runner.invoke(cli, ["api", "GET", "agents"])
    assert result.exit_code == 0, result.output
    mock_client.api_proxy.assert_called_once_with(
        "GET", "agents", query=None, json_body=None
    )
    # Pretty-printed JSON (indented).
    assert '"count": 1' in result.output
    assert '"id": "agent-123"' in result.output


@patch("cinna.account.AccountClient")
def test_api_inner_error_prints_body_and_exits_1(
    mock_client_cls, runner, account_root, monkeypatch
):
    """Proxied header present + 404 → inner-API error: body on stdout, exit 1."""
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.api_proxy.return_value = _api_response(
        404, {"detail": "Agent not found"}
    )

    result = runner.invoke(cli, ["api", "GET", "agents/ghost"])
    assert result.exit_code == 1
    assert "Agent not found" in result.output
    assert "HTTP 404" in result.stderr
    assert "blocked by platform policy" not in result.stderr


@patch("cinna.account.AccountClient")
def test_api_policy_403_is_distinguished_and_exits_2(
    mock_client_cls, runner, account_root, monkeypatch
):
    """Proxied header ABSENT + 403 → the hatch refused: policy prefix on
    stderr, exit 2 — distinguished from an inner 403 purely by the header."""
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.api_proxy.return_value = _api_response(
        403,
        {"detail": "The escape hatch may not call '/api/v1/credentials/'."},
        proxied=False,
    )

    result = runner.invoke(cli, ["api", "GET", "credentials"])
    assert result.exit_code == 2
    assert "blocked by platform policy:" in result.stderr
    assert "escape hatch may not call" in result.stderr


@patch("cinna.account.AccountClient")
def test_api_inner_403_is_not_policy_prefixed(
    mock_client_cls, runner, account_root, monkeypatch
):
    """An inner 403 carries the proxied header → treated as an inner-API
    error (body on stdout, exit 1, no policy prefix) even though the platform
    policy-denial message and an inner 403 share the same status code."""
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.api_proxy.return_value = _api_response(
        403, {"detail": "Not enough permissions"}
    )

    result = runner.invoke(cli, ["api", "DELETE", "agents/agent-123"])
    assert result.exit_code == 1
    assert "Not enough permissions" in result.output
    assert "blocked by platform policy" not in result.stderr


@patch("cinna.account.AccountClient")
def test_api_rate_limit_429_exits_2_with_retry_after(
    mock_client_cls, runner, account_root, monkeypatch
):
    """Proxied header absent + 429 → hatch rate limit: exit 2 + Retry-After,
    no policy prefix (reserved for 400/403)."""
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.api_proxy.return_value = _api_response(
        429,
        {"detail": "Escape-hatch rate limit exceeded for this account token."},
        headers={"Retry-After": "42"},
        proxied=False,
    )

    result = runner.invoke(cli, ["api", "GET", "agents"])
    assert result.exit_code == 2
    assert "rate limit exceeded" in result.stderr
    assert "Retry after 42s" in result.stderr
    # No policy prefix for non-403/400 hatch errors.
    assert "blocked by platform policy" not in result.stderr


@respx.mock
def test_api_classification_through_real_http(
    runner, account_root, account_cfg, monkeypatch
):
    """End-to-end (respx-mocked HTTP) confirmation that classification is
    driven by the real ``X-Cinna-Proxied`` response header, not the body."""
    monkeypatch.chdir(account_root)

    # Inner 4xx WITH the proxied header → exit 1, body printed.
    respx.post(
        "https://platform.example.com/api/v1/cli/account/api-proxy"
    ).respond(
        404,
        json={"detail": "Agent not found"},
        headers={"X-Cinna-Proxied": "1"},
    )
    result = runner.invoke(cli, ["api", "GET", "agents/ghost"])
    assert result.exit_code == 1
    assert "Agent not found" in result.output
    assert "blocked by platform policy" not in result.stderr

    respx.clear()

    # Hatch refusal (403) WITHOUT the proxied header → exit 2, policy prefix.
    respx.post(
        "https://platform.example.com/api/v1/cli/account/api-proxy"
    ).respond(403, json={"detail": "excluded path"})
    result = runner.invoke(cli, ["api", "GET", "credentials"])
    assert result.exit_code == 2
    assert "blocked by platform policy:" in result.stderr


@patch("cinna.account.AccountClient")
def test_api_query_and_json_parsing(mock_client_cls, runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.api_proxy.return_value = _api_response(200, {"ok": True})

    result = runner.invoke(
        cli,
        [
            "api",
            "patch",  # lowercase method is accepted and uppercased
            "agents/agent-123",
            "--json",
            '{"description": "updated"}',
            "--query",
            "a=1",
            "--query",
            "a=2",
            "--query",
            "b=x",
        ],
    )
    assert result.exit_code == 0, result.output
    mock_client.api_proxy.assert_called_once_with(
        "PATCH",
        "agents/agent-123",
        query={"a": ["1", "2"], "b": "x"},
        json_body={"description": "updated"},
    )


@patch("cinna.account.AccountClient")
def test_api_data_file_body(mock_client_cls, runner, account_root, monkeypatch, tmp_path):
    monkeypatch.chdir(account_root)
    body_file = account_root / "task.json"
    body_file.write_text('{"title": "from file"}')
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.api_proxy.return_value = _api_response(201, {"id": "t1"})

    result = runner.invoke(cli, ["api", "POST", "tasks", "--data", "@task.json"])
    assert result.exit_code == 0, result.output
    assert mock_client.api_proxy.call_args.kwargs["json_body"] == {
        "title": "from file"
    }


def test_api_json_and_data_mutually_exclusive(runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    result = runner.invoke(
        cli, ["api", "POST", "tasks", "--json", "{}", "--data", "@x.json"]
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output + result.stderr


def test_api_bad_query_pair(runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    result = runner.invoke(cli, ["api", "GET", "agents", "--query", "noequals"])
    assert result.exit_code != 0
    assert "key=value" in result.output + result.stderr


# --- AccountClient phase-3 methods (HTTP-level) ---


@respx.mock
def test_account_client_create_agent_thin_body(account_client):
    route = respx.post(
        "https://platform.example.com/api/v1/cli/account/agents"
    ).respond(200, json={"id": "agent-new-1", "name": "CRM Agent"})
    result = account_client.create_agent("CRM Agent")
    assert result["id"] == "agent-new-1"
    # Thin client: unspecified fields are omitted entirely.
    body = json.loads(route.calls[0].request.content)
    assert body == {"name": "CRM Agent"}


# --- Active user workspace ---


WORKSPACES_LISTING = {
    "count": 2,
    "data": [
        {"id": "ws-1", "name": "Sales", "user_id": "u-1"},
        {"id": "ws-2", "name": "Finance", "user_id": "u-1"},
    ],
}


@patch("cinna.account.AccountClient")
def test_user_workspace_list_marks_active(
    mock_client_cls, runner, tmp_path, monkeypatch
):
    monkeypatch.setenv("COLUMNS", "240")
    root = tmp_path / "my-cinna"
    root.mkdir()
    (root / "agents").mkdir()
    save_account_config(
        AccountConfig(
            platform_url="https://platform.example.com",
            frontend_url="https://ui.example.com",
            account_token="account-token-abc",
            machine_name="laptop",
            user_workspace_id="ws-2",
            user_workspace_name="Finance",
        ),
        root,
    )
    monkeypatch.chdir(root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_user_workspaces.return_value = WORKSPACES_LISTING

    result = runner.invoke(cli, ["account", "user-workspace", "list"])
    assert result.exit_code == 0, result.output
    assert "Sales" in result.output
    assert "Finance" in result.output
    assert "Default" in result.output


@patch("cinna.account.AccountClient")
def test_user_workspace_activate_persists(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_user_workspaces.return_value = WORKSPACES_LISTING

    result = runner.invoke(
        cli, ["account", "user-workspace", "activate", "Sales"]
    )
    assert result.exit_code == 0, result.output
    cfg = load_account_config(account_root)
    assert cfg.user_workspace_id == "ws-1"
    assert cfg.user_workspace_name == "Sales"


@patch("cinna.account.AccountClient")
def test_user_workspace_activate_default_clears(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    # Pre-set an active workspace, then clear via `activate default`.
    cfg = load_account_config(account_root)
    cfg.user_workspace_id = "ws-1"
    cfg.user_workspace_name = "Sales"
    save_account_config(cfg, account_root)

    result = runner.invoke(
        cli, ["account", "user-workspace", "activate", "default"]
    )
    assert result.exit_code == 0, result.output
    cleared = load_account_config(account_root)
    assert cleared.user_workspace_id is None
    assert cleared.user_workspace_name is None
    # Clearing is purely local — no workspace listing is fetched.
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_user_workspaces.assert_not_called()


@patch("cinna.account.AccountClient")
def test_agent_create_threads_active_workspace(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    cfg = load_account_config(account_root)
    cfg.user_workspace_id = "ws-1"
    cfg.user_workspace_name = "Sales"
    save_account_config(cfg, account_root)

    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.create_agent.return_value = {"id": "agent-new-1", "name": "CRM Agent"}

    result = runner.invoke(cli, ["agent", "create", "CRM Agent"])
    assert result.exit_code == 0, result.output
    mock_client.create_agent.assert_called_once_with(
        "CRM Agent", None, user_workspace_id="ws-1"
    )


# --- Account credentials (drafts only) ---


CREDENTIALS_LISTING = {
    "count": 1,
    "data": [
        {
            "id": "cred-1",
            "name": "Stripe Key",
            "type": "api_token",
            "status": "incomplete",
        }
    ],
}


@patch("cinna.account.AccountClient")
def test_credentials_list_renders(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.setenv("COLUMNS", "240")
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_credentials.return_value = CREDENTIALS_LISTING

    result = runner.invoke(cli, ["account", "credentials", "list"])
    assert result.exit_code == 0, result.output
    assert "Stripe Key" in result.output
    assert "needs setup" in result.output
    mock_client.list_credentials.assert_called_once_with(user_workspace_id=None)


@patch("cinna.account.AccountClient")
def test_credentials_create_draft_lists_required_fields(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.create_credential.return_value = {
        "credential": {
            "id": "cred-1",
            "name": "Stripe Key",
            "type": "api_token",
            "status": "incomplete",
        },
        "required_fields": ["api_token"],
        "setup_url": "https://ui.example.com/credentials",
    }

    result = runner.invoke(
        cli,
        ["account", "credentials", "create", "--name", "Stripe Key", "--type", "api_token"],
    )
    assert result.exit_code == 0, result.output
    mock_client.create_credential.assert_called_once_with(
        "Stripe Key",
        "api_token",
        notes=None,
        service_uri=None,
        allow_sharing=False,
        user_workspace_id=None,
    )
    assert "api_token" in result.output
    assert "https://ui.example.com/credentials" in result.output


@patch("cinna.account.AccountClient")
def test_credentials_create_with_agent_attaches(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.create_credential.return_value = {
        "credential": {"id": "cred-1", "name": "Stripe Key", "type": "api_token", "status": "incomplete"},
        "required_fields": ["api_token"],
        "setup_url": "https://ui.example.com/credentials",
    }
    mock_client.list_account_agents.return_value = AGENTS_LISTING

    result = runner.invoke(
        cli,
        [
            "account", "credentials", "create",
            "--name", "Stripe Key", "--type", "api_token",
            "--agent", "CRM Agent",
        ],
    )
    assert result.exit_code == 0, result.output
    mock_client.share_credential_with_agent.assert_called_once_with(
        "cred-1", "agent-123"
    )
    assert "Attached to" in result.output


@patch("cinna.account.AccountClient")
def test_credentials_share_with_agent_resolves_name(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING

    result = runner.invoke(
        cli,
        ["account", "credentials", "share-with-agent", "cred-1", "--agent", "HR Manager Agent"],
    )
    assert result.exit_code == 0, result.output
    mock_client.share_credential_with_agent.assert_called_once_with(
        "cred-1", "agent-789"
    )


@patch("cinna.account.AccountClient")
def test_credentials_delete_confirms(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value

    result = runner.invoke(
        cli, ["account", "credentials", "delete", "cred-1", "--yes"]
    )
    assert result.exit_code == 0, result.output
    mock_client.delete_credential.assert_called_once_with("cred-1", force=False)


@patch("cinna.account.AccountClient")
def test_credentials_update_metadata_only(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.update_credential.return_value = {
        "id": "cred-1", "name": "Renamed", "status": "incomplete"
    }

    result = runner.invoke(
        cli, ["account", "credentials", "update", "cred-1", "--name", "Renamed"]
    )
    assert result.exit_code == 0, result.output
    mock_client.update_credential.assert_called_once_with(
        "cred-1", {"name": "Renamed"}
    )


@patch("cinna.account.AccountClient")
def test_credentials_update_requires_a_field(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    result = runner.invoke(cli, ["account", "credentials", "update", "cred-1"])
    assert result.exit_code != 0
    assert "Nothing to update" in result.output + result.stderr


# --- AccountClient credential methods (HTTP-level) ---


@respx.mock
def test_account_client_create_credential_draft_body(account_client):
    route = respx.post(
        "https://platform.example.com/api/v1/cli/account/credentials"
    ).respond(200, json={"credential": {"id": "cred-1"}, "required_fields": [], "setup_url": ""})
    account_client.create_credential(
        "Stripe Key", "api_token", user_workspace_id="ws-1"
    )
    body = json.loads(route.calls[0].request.content)
    # No secret value is ever sent — only metadata + structure.
    assert body == {
        "name": "Stripe Key",
        "type": "api_token",
        "allow_sharing": False,
        "user_workspace_id": "ws-1",
    }
    assert "credential_data" not in body


@respx.mock
def test_account_client_list_credentials_default_filter(account_client):
    route = respx.get(
        "https://platform.example.com/api/v1/cli/account/credentials"
    ).respond(200, json=CREDENTIALS_LISTING)
    account_client.list_credentials(user_workspace_id="")
    assert route.calls[0].request.url.params.get("user_workspace_id") == ""


@respx.mock
def test_account_client_delete_credential_force(account_client):
    route = respx.delete(
        "https://platform.example.com/api/v1/cli/account/credentials/cred-1"
    ).respond(200, json={"message": "ok"})
    account_client.delete_credential("cred-1", force=True)
    assert route.calls[0].request.url.params.get("force") == "true"


@respx.mock
def test_account_client_connect_mcp_default_modes_omitted(account_client):
    route = respx.post(
        "https://platform.example.com/api/v1/cli/account/connect/mcp"
    ).respond(200, json={"credential_id": "c1", "status": "connected"})
    account_client.connect_mcp("conn-1", "agent-789")
    body = json.loads(route.calls[0].request.content)
    # Defaults (both modes on) are left to the backend.
    assert body == {"connector_id": "conn-1", "consumer_agent_id": "agent-789"}


@respx.mock
def test_account_client_api_proxy_does_not_raise_on_inner_errors(account_client):
    """Non-2xx inner statuses are normal passthrough output — no exception."""
    respx.post(
        "https://platform.example.com/api/v1/cli/account/api-proxy"
    ).respond(500, json={"detail": "boom"})
    response = account_client.api_proxy("GET", "agents")
    assert response.status_code == 500
    assert response.json()["detail"] == "boom"


@respx.mock
def test_account_client_api_proxy_401_raises(account_client):
    respx.post(
        "https://platform.example.com/api/v1/cli/account/api-proxy"
    ).respond(401, json={"detail": "CLI token has been revoked"})
    with pytest.raises(AuthenticationError, match="CLI token has been revoked"):
        account_client.api_proxy("GET", "agents")


@respx.mock
def test_account_client_api_proxy_request_body(account_client):
    route = respx.post(
        "https://platform.example.com/api/v1/cli/account/api-proxy"
    ).respond(200, json={"ok": True})
    account_client.api_proxy(
        "POST", "tasks", query={"a": "1"}, json_body={"title": "t"}
    )
    body = json.loads(route.calls[0].request.content)
    assert body == {
        "method": "POST",
        "path": "tasks",
        "query": {"a": "1"},
        "json_body": {"title": "t"},
    }


# --- cinna agent-api enable / refresh / spec ---


_ENABLED_STATUS = {
    "agent_api_enabled": True,
    "state": "running",
    "spec_available": True,
    "last_error": None,
    "policy": None,
}

_DISABLED_STATUS = {
    "agent_api_enabled": False,
    "state": "disabled",
    "spec_available": False,
    "last_error": None,
    "policy": None,
}

_SAMPLE_SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "CRM Agent API", "version": "1.0.0"},
    "paths": {"/orders": {"get": {"operationId": "list_orders"}}},
}


@patch("cinna.account.AccountClient")
def test_agent_api_enable_resolves_and_toggles(
    mock_client_cls, runner, account_root, monkeypatch
):
    """`cinna agent-api enable` resolves the agent and enables by default."""
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.set_agent_api_enabled.return_value = _ENABLED_STATUS

    result = runner.invoke(cli, ["agent-api", "enable", "CRM Agent"])
    assert result.exit_code == 0, result.output
    mock_client.set_agent_api_enabled.assert_called_once_with("agent-123", enabled=True)
    assert "enabled for CRM Agent" in result.output
    assert "running" in result.output


@patch("cinna.account.AccountClient")
def test_agent_api_disable_flag(
    mock_client_cls, runner, account_root, monkeypatch
):
    """`--disable` flips the toggle off."""
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.set_agent_api_enabled.return_value = _DISABLED_STATUS

    result = runner.invoke(cli, ["agent-api", "enable", "agent-123", "--disable"])
    assert result.exit_code == 0, result.output
    mock_client.set_agent_api_enabled.assert_called_once_with("agent-123", enabled=False)
    assert "disabled for CRM Agent" in result.output


@patch("cinna.account.AccountClient")
def test_agent_api_refresh(mock_client_cls, runner, account_root, monkeypatch):
    """`cinna agent-api refresh` re-harvests and prints status."""
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.refresh_agent_api.return_value = _ENABLED_STATUS

    result = runner.invoke(cli, ["agent-api", "refresh", "CRM Agent"])
    assert result.exit_code == 0, result.output
    mock_client.refresh_agent_api.assert_called_once_with("agent-123")
    assert "Refreshed REST API for CRM Agent" in result.output


@patch("cinna.account.AccountClient")
def test_agent_api_refresh_surfaces_harvest_error(
    mock_client_cls, runner, account_root, monkeypatch
):
    """A harvest error in the status is surfaced as a warning, not an exception."""
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.refresh_agent_api.return_value = {
        **_ENABLED_STATUS,
        "spec_available": False,
        "last_error": "ImportError: orders.py line 3",
    }

    result = runner.invoke(cli, ["agent-api", "refresh", "CRM Agent"])
    assert result.exit_code == 0, result.output
    assert "ImportError" in result.output


@patch("cinna.account.AccountClient")
def test_agent_api_spec_prints_json(
    mock_client_cls, runner, account_root, monkeypatch
):
    """`cinna agent-api spec` prints the harvested spec as plain JSON."""
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.get_agent_api_spec.return_value = _SAMPLE_SPEC

    result = runner.invoke(cli, ["agent-api", "spec", "CRM Agent"])
    assert result.exit_code == 0, result.output
    mock_client.get_agent_api_spec.assert_called_once_with("agent-123")
    # Output is valid JSON round-tripping the spec.
    assert json.loads(result.output) == _SAMPLE_SPEC


@patch("cinna.account.AccountClient")
def test_agent_api_spec_writes_to_file(
    mock_client_cls, runner, account_root, monkeypatch, tmp_path
):
    """`-o` writes the spec to a file instead of stdout."""
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.get_agent_api_spec.return_value = _SAMPLE_SPEC

    out = tmp_path / "spec.json"
    result = runner.invoke(cli, ["agent-api", "spec", "CRM Agent", "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text()) == _SAMPLE_SPEC


@patch("cinna.account.AccountClient")
def test_agent_api_unknown_agent(
    mock_client_cls, runner, account_root, monkeypatch
):
    """An unresolved agent ref fails before any agent-api call."""
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING

    result = runner.invoke(cli, ["agent-api", "enable", "nope"])
    assert result.exit_code != 0
    assert "No accessible agent matches 'nope'" in result.output
    mock_client.set_agent_api_enabled.assert_not_called()


# --- cinna agent-api call (owner-side smoke test) ---


@patch("cinna.account.AccountClient")
def test_agent_api_call_forwards_query(
    mock_client_cls, runner, account_root, monkeypatch
):
    """`cinna agent-api call` resolves the agent and forwards query params."""
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.call_agent_api.return_value = {
        "status_code": 200,
        "headers": {"content-type": "application/json"},
        "body": json.dumps({"price": 42, "vs_currency": "eur"}),
        "is_json": True,
    }

    result = runner.invoke(
        cli,
        ["agent-api", "call", "CRM Agent", "btc-rate", "--query", "vs_currency=eur"],
    )
    assert result.exit_code == 0, result.output
    mock_client.call_agent_api.assert_called_once_with(
        "agent-123", "GET", "btc-rate", query={"vs_currency": "eur"}, json_body=None
    )
    assert '"vs_currency": "eur"' in result.output
    assert "[200]" in result.output


@patch("cinna.account.AccountClient")
def test_agent_api_call_nonzero_exit_on_error_status(
    mock_client_cls, runner, account_root, monkeypatch
):
    """An inner 4xx prints the body but exits non-zero."""
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.call_agent_api.return_value = {
        "status_code": 404,
        "headers": {"content-type": "application/json"},
        "body": json.dumps({"detail": "Not found"}),
        "is_json": True,
    }

    result = runner.invoke(cli, ["agent-api", "call", "CRM Agent", "missing"])
    assert result.exit_code == 1, result.output
    assert "Not found" in result.output


# --- cinna agent restart-env ---


@patch("cinna.account.AccountClient")
def test_agent_restart_env(mock_client_cls, runner, account_root, monkeypatch):
    """`cinna agent restart-env` resolves the agent and prints the status."""
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.restart_agent_env.return_value = {
        "environment_id": "env-1",
        "status": "running",
        "status_message": "Environment restarted successfully",
    }

    result = runner.invoke(cli, ["agent", "restart-env", "CRM Agent"])
    assert result.exit_code == 0, result.output
    mock_client.restart_agent_env.assert_called_once_with("agent-123")
    assert "running" in result.output


@patch("cinna.account.sync_session.status")
@patch("cinna.account.resolve_child_workspace")
@patch("cinna.account.AccountClient")
def test_agent_restart_env_warns_on_unsynced_edits(
    mock_client_cls, mock_resolve, mock_status, runner, account_root, monkeypatch, sample_config
):
    """D2: restart warns + aborts (unless confirmed) when the synced workspace
    has unsynced local edits, so a restart can't silently clobber them."""
    from cinna.sync_session import SyncStatus

    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    # Agent is synced locally with pending local changes.
    mock_resolve.return_value = (account_root / "agents" / "crm-agent", sample_config)
    mock_status.return_value = SyncStatus(
        session_name="cinna-abc", state="connected", pending_to_remote=3
    )

    # Decline the confirmation → abort, restart NOT called.
    result = runner.invoke(cli, ["agent", "restart-env", "CRM Agent"], input="n\n")
    assert result.exit_code != 0
    assert "unsynced local change" in result.output
    mock_client.restart_agent_env.assert_not_called()

    # Confirm → proceeds.
    mock_client.restart_agent_env.return_value = {
        "environment_id": "env-1", "status": "running", "status_message": None
    }
    result = runner.invoke(cli, ["agent", "restart-env", "CRM Agent"], input="y\n")
    assert result.exit_code == 0, result.output
    mock_client.restart_agent_env.assert_called_once_with("agent-123")


# --- cinna agent show ---


@patch("cinna.account.AccountClient")
def test_agent_show(mock_client_cls, runner, account_root, monkeypatch):
    """`cinna agent show` prints prompts, features, and credential metadata."""
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.inspect_agent.return_value = {
        "id": "agent-123",
        "name": "CRM Agent",
        "description": "desc",
        "features": {"agent_api_enabled": True, "webapp_enabled": False},
        "prompts": {
            "entrypoint": "You are the entrypoint.",
            "workflow": None,
            "refiner": None,
        },
        "credentials": [{"name": "OpenAI Key", "type": "ai"}],
        "agent_api_status": None,
    }

    result = runner.invoke(cli, ["agent", "show", "CRM Agent"])
    assert result.exit_code == 0, result.output
    mock_client.inspect_agent.assert_called_once_with("agent-123")
    assert "You are the entrypoint." in result.output
    assert "OpenAI Key" in result.output
    assert "agent_api_enabled" in result.output


@patch("cinna.account.AccountClient")
def test_agent_show_prompts_only(mock_client_cls, runner, account_root, monkeypatch):
    """`--prompts` skips features + credentials."""
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.inspect_agent.return_value = {
        "id": "agent-123",
        "name": "CRM Agent",
        "description": None,
        "features": {"agent_api_enabled": True},
        "prompts": {"entrypoint": "EP", "workflow": "WF", "refiner": None},
        "credentials": [{"name": "Secret", "type": "ai"}],
        "agent_api_status": None,
    }

    result = runner.invoke(cli, ["agent", "show", "CRM Agent", "--prompts"])
    assert result.exit_code == 0, result.output
    assert "EP" in result.output
    # Features + credentials are suppressed in --prompts mode.
    assert "Secret" not in result.output
    assert "Features:" not in result.output


@patch("cinna.account._stdout_is_tty", return_value=True)
@patch("cinna.account.AccountClient")
def test_agent_show_truncates_long_prompt_on_tty(
    mock_client_cls, _isatty, runner, account_root, monkeypatch
):
    """On a TTY, a long prompt is truncated for readability."""
    monkeypatch.chdir(account_root)
    long_prompt = "X" * 5000
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.inspect_agent.return_value = {
        "id": "agent-123",
        "name": "CRM Agent",
        "features": {},
        "prompts": {"entrypoint": long_prompt, "workflow": None, "refiner": None},
        "credentials": [],
        "agent_api_status": None,
    }

    result = runner.invoke(cli, ["agent", "show", "CRM Agent", "--prompts"])
    assert result.exit_code == 0, result.output
    assert "…(truncated" in result.output
    assert result.output.count("X") == 2000


@patch("cinna.account._stdout_is_tty", return_value=True)
@patch("cinna.account.AccountClient")
def test_agent_show_full_flag_prints_whole_prompt(
    mock_client_cls, _isatty, runner, account_root, monkeypatch
):
    """`--full` prints the entire prompt even on a TTY."""
    monkeypatch.chdir(account_root)
    long_prompt = "X" * 5000
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.inspect_agent.return_value = {
        "id": "agent-123",
        "name": "CRM Agent",
        "features": {},
        "prompts": {"entrypoint": long_prompt, "workflow": None, "refiner": None},
        "credentials": [],
        "agent_api_status": None,
    }

    result = runner.invoke(cli, ["agent", "show", "CRM Agent", "--prompts", "--full"])
    assert result.exit_code == 0, result.output
    assert "truncated" not in result.output
    assert result.output.count("X") == 5000


@patch("cinna.account._stdout_is_tty", return_value=False)
@patch("cinna.account.AccountClient")
def test_agent_show_non_tty_prints_whole_prompt(
    mock_client_cls, _isatty, runner, account_root, monkeypatch
):
    """When stdout is redirected (not a TTY), prompts are never truncated."""
    monkeypatch.chdir(account_root)
    long_prompt = "X" * 5000
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.inspect_agent.return_value = {
        "id": "agent-123",
        "name": "CRM Agent",
        "features": {},
        "prompts": {"entrypoint": long_prompt, "workflow": None, "refiner": None},
        "credentials": [],
        "agent_api_status": None,
    }

    result = runner.invoke(cli, ["agent", "show", "CRM Agent", "--prompts"])
    assert result.exit_code == 0, result.output
    assert "truncated" not in result.output
    assert result.output.count("X") == 5000


# --- AccountClient agent-api request shapes ---


@respx.mock
def test_account_client_set_agent_api_enabled(account_client):
    route = respx.post(
        "https://platform.example.com/api/v1/cli/account/agent-api/enable"
    ).respond(200, json=_ENABLED_STATUS)
    result = account_client.set_agent_api_enabled("agent-123", enabled=True)
    assert result["state"] == "running"
    body = json.loads(route.calls[0].request.content)
    assert body == {"agent_id": "agent-123", "enabled": True}


@respx.mock
def test_account_client_refresh_agent_api(account_client):
    route = respx.post(
        "https://platform.example.com/api/v1/cli/account/agent-api/refresh"
    ).respond(200, json=_ENABLED_STATUS)
    account_client.refresh_agent_api("agent-123")
    body = json.loads(route.calls[0].request.content)
    assert body == {"agent_id": "agent-123"}


@respx.mock
def test_account_client_get_agent_api_spec(account_client):
    route = respx.get(
        "https://platform.example.com/api/v1/cli/account/agent-api/spec"
    ).respond(200, json=_SAMPLE_SPEC)
    result = account_client.get_agent_api_spec("agent-123")
    assert result == _SAMPLE_SPEC
    assert route.calls[0].request.url.params["agent_id"] == "agent-123"


@respx.mock
def test_account_client_call_agent_api(account_client):
    route = respx.post(
        "https://platform.example.com/api/v1/cli/account/agent-api/call"
    ).respond(200, json={"status_code": 200, "headers": {}, "body": "{}", "is_json": True})
    account_client.call_agent_api(
        "agent-123", "GET", "btc-rate", query={"vs_currency": "eur"}
    )
    body = json.loads(route.calls[0].request.content)
    assert body == {
        "agent_id": "agent-123",
        "method": "GET",
        "path": "btc-rate",
        "query": {"vs_currency": "eur"},
    }


@respx.mock
def test_account_client_restart_agent_env(account_client):
    route = respx.post(
        "https://platform.example.com/api/v1/cli/account/agents/agent-123/restart-env"
    ).respond(200, json={"environment_id": "env-1", "status": "running", "status_message": None})
    result = account_client.restart_agent_env("agent-123")
    assert result["status"] == "running"
    assert route.called


@respx.mock
def test_account_client_inspect_agent(account_client):
    route = respx.get(
        "https://platform.example.com/api/v1/cli/account/agents/agent-123/inspect"
    ).respond(200, json={
        "id": "agent-123", "name": "CRM", "features": {}, "prompts": {},
        "credentials": [], "agent_api_status": None,
    })
    result = account_client.inspect_agent("agent-123")
    assert result["id"] == "agent-123"
    assert route.called


# ─── cinna agent schedule (CRUD) ────────────────────────────────────────────


_SCHEDULE_ROW = {
    "id": "sched-1",
    "agent_id": "agent-123",
    "name": "Daily report",
    "cron_string": "0 7 * * 1-5",
    "description": "weekday 7am",
    "enabled": True,
    "prompt": "Produce the daily report",
    "schedule_type": "static_prompt",
    "command": None,
    "last_execution": None,
    "next_execution": "2026-06-15T07:00:00+00:00",
    "created_at": "2026-06-12T00:00:00+00:00",
    "updated_at": "2026-06-12T00:00:00+00:00",
}


@patch("cinna.account.AccountClient")
def test_schedule_list_renders(mock_client_cls, runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    monkeypatch.setenv("COLUMNS", "240")
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.list_schedules.return_value = {"data": [_SCHEDULE_ROW], "count": 1}

    result = runner.invoke(cli, ["agent", "schedule", "list", "CRM Agent"])
    assert result.exit_code == 0, result.output
    mock_client.list_schedules.assert_called_once_with("agent-123")
    assert "Daily report" in result.output


@patch("cinna.account.AccountClient")
def test_schedule_create_builds_body(mock_client_cls, runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.create_schedule.return_value = _SCHEDULE_ROW

    result = runner.invoke(
        cli,
        [
            "agent", "schedule", "create", "CRM Agent",
            "--name", "Daily report",
            "--cron", "0 7 * * 1-5",
            "--tz", "Europe/Berlin",
            "--prompt", "Produce the daily report",
        ],
    )
    assert result.exit_code == 0, result.output
    agent_id, body = mock_client.create_schedule.call_args.args
    assert agent_id == "agent-123"
    assert body["name"] == "Daily report"
    assert body["cron_string"] == "0 7 * * 1-5"
    assert body["timezone"] == "Europe/Berlin"
    assert body["schedule_type"] == "static_prompt"
    assert body["prompt"] == "Produce the daily report"
    assert body["enabled"] is True
    # description defaults to the name when omitted
    assert body["description"] == "Daily report"
    assert "command" not in body
    assert "sched-1" in result.output


@patch("cinna.account.AccountClient")
def test_schedule_create_script_trigger_requires_command(
    mock_client_cls, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING

    result = runner.invoke(
        cli,
        [
            "agent", "schedule", "create", "CRM Agent",
            "--name", "DB check", "--cron", "*/30 * * * *", "--tz", "UTC",
            "--type", "script_trigger",
        ],
    )
    assert result.exit_code != 0
    assert "--command is required" in result.output + result.stderr
    mock_client.create_schedule.assert_not_called()


@patch("cinna.account.AccountClient")
def test_schedule_update_partial_body(mock_client_cls, runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.update_schedule.return_value = {**_SCHEDULE_ROW, "enabled": False}

    result = runner.invoke(
        cli, ["agent", "schedule", "update", "CRM Agent", "sched-1", "--disable"]
    )
    assert result.exit_code == 0, result.output
    agent_id, schedule_id, body = mock_client.update_schedule.call_args.args
    assert (agent_id, schedule_id) == ("agent-123", "sched-1")
    assert body == {"enabled": False}


@patch("cinna.account.AccountClient")
def test_schedule_update_cron_requires_tz(mock_client_cls, runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING

    result = runner.invoke(
        cli, ["agent", "schedule", "update", "CRM Agent", "sched-1", "--cron", "0 9 * * *"]
    )
    assert result.exit_code != 0
    assert "--tz is required" in result.output + result.stderr
    mock_client.update_schedule.assert_not_called()


@patch("cinna.account.AccountClient")
def test_schedule_update_empty_errors(mock_client_cls, runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING

    result = runner.invoke(cli, ["agent", "schedule", "update", "CRM Agent", "sched-1"])
    assert result.exit_code != 0
    assert "Nothing to update" in result.output + result.stderr


@patch("cinna.account.AccountClient")
def test_schedule_run_prints_message(mock_client_cls, runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.run_schedule.return_value = {"message": "Schedule triggered successfully"}

    result = runner.invoke(cli, ["agent", "schedule", "run", "CRM Agent", "sched-1"])
    assert result.exit_code == 0, result.output
    mock_client.run_schedule.assert_called_once_with("agent-123", "sched-1")
    assert "triggered successfully" in result.output


@patch("cinna.account.AccountClient")
def test_schedule_delete_confirmed(mock_client_cls, runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.delete_schedule.return_value = {"message": "Schedule deleted successfully"}

    result = runner.invoke(
        cli, ["agent", "schedule", "delete", "CRM Agent", "sched-1", "--yes"]
    )
    assert result.exit_code == 0, result.output
    mock_client.delete_schedule.assert_called_once_with("agent-123", "sched-1")


@patch("cinna.account.AccountClient")
def test_schedule_generate_success(mock_client_cls, runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.generate_schedule.return_value = {
        "success": True,
        "cron_string": "0 7 * * 1-5",
        "description": "weekday 7am",
        "next_execution": "2026-06-15T07:00:00+00:00",
    }

    result = runner.invoke(
        cli, ["agent", "schedule", "generate", "CRM Agent", "every weekday at 7am"]
    )
    assert result.exit_code == 0, result.output
    assert "0 7 * * 1-5" in result.output


@patch("cinna.account.AccountClient")
def test_schedule_generate_failure_raises(mock_client_cls, runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.generate_schedule.return_value = {
        "success": False,
        "error": "'sometimes' is too vague",
    }

    result = runner.invoke(
        cli, ["agent", "schedule", "generate", "CRM Agent", "sometimes"]
    )
    assert result.exit_code != 0
    assert "too vague" in result.output + result.stderr


# ─── cinna agent status ─────────────────────────────────────────────────────


@patch("cinna.account.AccountClient")
def test_status_show_renders(mock_client_cls, runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.get_agent_status.return_value = {
        "status": {
            "agent_id": "agent-123",
            "severity": "ok",
            "summary": "All clear",
            "reported_at": "2026-06-12T00:00:00+00:00",
            "fetched_at": "2026-06-12T00:00:00+00:00",
            "body": "All systems nominal.",
            "refresh_command_warning": None,
        },
        "status_refresh_command": "/run:status",
    }

    result = runner.invoke(cli, ["agent", "status", "show", "CRM Agent"])
    assert result.exit_code == 0, result.output
    mock_client.get_agent_status.assert_called_once_with("agent-123", force_refresh=False)
    assert "All clear" in result.output
    assert "/run:status" in result.output


@patch("cinna.account.AccountClient")
def test_status_refresh_forces(mock_client_cls, runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.get_agent_status.return_value = {
        "status": {"agent_id": "agent-123", "severity": None, "raw": None},
        "status_refresh_command": "/run:status",
    }

    result = runner.invoke(cli, ["agent", "status", "refresh", "CRM Agent"])
    assert result.exit_code == 0, result.output
    mock_client.get_agent_status.assert_called_once_with("agent-123", force_refresh=True)


@patch("cinna.account.AccountClient")
def test_status_set_command(mock_client_cls, runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    mock_client = mock_client_cls.return_value.__enter__.return_value
    mock_client.list_account_agents.return_value = AGENTS_LISTING
    mock_client.set_status_refresh_command.return_value = {
        "status": {"agent_id": "agent-123"},
        "status_refresh_command": "/run:custom",
    }

    result = runner.invoke(
        cli, ["agent", "status", "set-command", "CRM Agent", "/run:custom"]
    )
    assert result.exit_code == 0, result.output
    mock_client.set_status_refresh_command.assert_called_once_with("agent-123", "/run:custom")
    assert "/run:custom" in result.output


# ─── client-level (respx) coverage for the new endpoints ────────────────────


@respx.mock
def test_account_client_list_schedules(account_client):
    route = respx.get(
        "https://platform.example.com/api/v1/cli/account/agents/agent-123/schedules"
    ).respond(200, json={"data": [_SCHEDULE_ROW], "count": 1})
    result = account_client.list_schedules("agent-123")
    assert result["count"] == 1
    assert route.called


@respx.mock
def test_account_client_create_schedule(account_client):
    route = respx.post(
        "https://platform.example.com/api/v1/cli/account/agents/agent-123/schedules"
    ).respond(200, json=_SCHEDULE_ROW)
    result = account_client.create_schedule("agent-123", {"name": "x"})
    assert result["id"] == "sched-1"
    assert route.called


@respx.mock
def test_account_client_run_schedule(account_client):
    route = respx.post(
        "https://platform.example.com/api/v1/cli/account/agents/agent-123/schedules/sched-1/run"
    ).respond(200, json={"message": "Schedule triggered successfully"})
    result = account_client.run_schedule("agent-123", "sched-1")
    assert "triggered" in result["message"]
    assert route.called


@respx.mock
def test_account_client_get_agent_status(account_client):
    route = respx.get(
        "https://platform.example.com/api/v1/cli/account/agents/agent-123/status"
    ).respond(200, json={"status": {"agent_id": "agent-123"}, "status_refresh_command": "/run:status"})
    result = account_client.get_agent_status("agent-123", force_refresh=True)
    assert result["status_refresh_command"] == "/run:status"
    assert route.called


@respx.mock
def test_account_client_set_status_refresh_command(account_client):
    route = respx.post(
        "https://platform.example.com/api/v1/cli/account/agents/agent-123/status/refresh-command"
    ).respond(200, json={"status": {"agent_id": "agent-123"}, "status_refresh_command": "/run:x"})
    result = account_client.set_status_refresh_command("agent-123", "/run:x")
    assert result["status_refresh_command"] == "/run:x"
    assert route.called


# ── cinna login (device authorization flow) ─────────────────────────────────


@patch("cinna.account.webbrowser.open")
@patch("cinna.account.time.sleep", lambda _s: None)
@patch("cinna.account._login_poll")
@patch("cinna.account._login_start")
def test_login_refreshes_token_in_place(
    mock_start, mock_poll, _browser, runner, account_root, account_cfg, monkeypatch
):
    monkeypatch.chdir(account_root)
    mock_start.return_value = {
        "device_code": "dev-1",
        "user_code": "WX7K-9Q2P",
        "verification_uri_complete": "https://ui.example.com/device?code=WX7K-9Q2P",
        "interval": 1,
        "expires_in": 60,
    }
    mock_poll.side_effect = [
        {"status": "authorization_pending"},
        {"status": "authorized", "account_token": "fresh-account-token"},
    ]

    result = runner.invoke(cli, ["login"])
    assert result.exit_code == 0, result.output

    refreshed = load_account_config(account_root)
    assert refreshed.account_token == "fresh-account-token"
    # Other fields preserved.
    assert refreshed.platform_url == account_cfg.platform_url
    assert refreshed.machine_name == account_cfg.machine_name
    # The browser code is surfaced for manual fallback.
    assert "WX7K-9Q2P" in result.output


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("app.example.com", "https://app.example.com"),
        ("https://app.example.com/", "https://app.example.com"),
        ("https://app.example.com/some/path", "https://app.example.com"),
        ("HTTP://App.Example.com:9000", "http://App.Example.com:9000"),
        ("localhost:8000", "http://localhost:8000"),
        ("127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("'  app.example.com  '", "https://app.example.com"),
    ],
)
def test_normalize_platform_url(raw, expected):
    from cinna.account import _normalize_platform_url

    assert _normalize_platform_url(raw) == expected


def test_normalize_platform_url_rejects_empty():
    import click

    from cinna.account import _normalize_platform_url

    with pytest.raises(click.ClickException):
        _normalize_platform_url("   ")


@patch("cinna.account.webbrowser.open")
@patch("cinna.account.time.sleep", lambda _s: None)
@patch("cinna.account._install_context_package", return_value=True)
@patch("cinna.account._login_poll")
@patch("cinna.account._login_start")
def test_login_new_account_in_empty_dir(
    mock_start, mock_poll, _ctx, _browser, runner, tmp_path, monkeypatch
):
    empty = tmp_path / "fresh"
    empty.mkdir()
    monkeypatch.chdir(empty)
    mock_start.return_value = {
        "device_code": "dev-1",
        "user_code": "AB12-CD34",
        "verification_uri_complete": "https://app.example.com/device?code=AB12-CD34",
    }
    mock_poll.return_value = {"status": "authorized", "account_token": "new-tok"}

    result = runner.invoke(cli, ["login", "app.example.com"])
    assert result.exit_code == 0, result.output

    # Account workspace materialized in the (empty) cwd.
    cfg = load_account_config(empty)
    assert cfg.account_token == "new-tok"
    assert cfg.platform_url == "https://app.example.com"
    assert (empty / "agents").is_dir()


@patch("cinna.account.webbrowser.open")
@patch("cinna.account.time.sleep", lambda _s: None)
@patch("cinna.account._install_context_package", return_value=True)
@patch("cinna.account._login_poll")
@patch("cinna.account._login_start")
def test_login_new_account_prompts_subfolder_when_not_empty(
    mock_start, mock_poll, _ctx, _browser, runner, tmp_path, monkeypatch
):
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "README.md").write_text("not empty\n")
    monkeypatch.chdir(busy)
    mock_start.return_value = {
        "device_code": "dev-1",
        "verification_uri": "https://app.example.com/device",
    }
    mock_poll.return_value = {"status": "authorized", "account_token": "sub-tok"}

    # Provide the subfolder name at the prompt.
    result = runner.invoke(cli, ["login", "app.example.com"], input="team-cinna\n")
    assert result.exit_code == 0, result.output

    cfg = load_account_config(busy / "team-cinna")
    assert cfg.account_token == "sub-tok"
    # Original folder contents untouched.
    assert (busy / "README.md").exists()


@patch("cinna.account.webbrowser.open")
@patch("cinna.account.time.sleep", lambda _s: None)
@patch("cinna.account._install_context_package", return_value=True)
@patch("cinna.account._login_poll")
@patch("cinna.account._login_start")
def test_login_new_account_prompts_for_domain(
    mock_start, mock_poll, _ctx, _browser, runner, tmp_path, monkeypatch
):
    empty = tmp_path / "fresh2"
    empty.mkdir()
    monkeypatch.chdir(empty)
    mock_start.return_value = {
        "device_code": "dev-1",
        "verification_uri": "https://app.example.com/device",
    }
    mock_poll.return_value = {"status": "authorized", "account_token": "tok"}

    # No domain arg → prompted; type it in.
    result = runner.invoke(cli, ["login"], input="app.example.com\n")
    assert result.exit_code == 0, result.output
    assert load_account_config(empty).platform_url == "https://app.example.com"


@patch("cinna.account.httpx.post")
def test_login_unsupported_backend_is_explained(
    mock_post, runner, account_root, monkeypatch
):
    import httpx

    monkeypatch.chdir(account_root)
    mock_post.return_value = httpx.Response(404, json={"detail": "not found"})
    result = runner.invoke(cli, ["login"])
    assert result.exit_code != 0
    assert "does not support 'cinna login'" in result.output
    assert "cinna account setup" in result.output


@patch("cinna.account.time.sleep", lambda _s: None)
@patch("cinna.account._login_poll")
def test_poll_until_authorized_waits_then_returns(mock_poll):
    from cinna.account import _poll_until_authorized

    mock_poll.side_effect = [
        {"status": "authorization_pending"},
        {"status": "slow_down"},
        {"status": "authorized", "account_token": "tok"},
    ]
    result = _poll_until_authorized("https://p", "dev-1", interval=1, expires_in=60)
    assert result["account_token"] == "tok"
    assert mock_poll.call_count == 3


@patch("cinna.account.time.sleep", lambda _s: None)
@patch("cinna.account._login_poll")
def test_poll_until_authorized_raises_on_denied(mock_poll):
    import click

    from cinna.account import _poll_until_authorized

    mock_poll.return_value = {"status": "access_denied"}
    with pytest.raises(click.ClickException, match="denied"):
        _poll_until_authorized("https://p", "dev-1", interval=1, expires_in=60)
