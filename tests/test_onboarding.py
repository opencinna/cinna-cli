"""Driver-facing contract of the desktop-driven commands.

Cinna Desktop spawns ``cinna`` with no TTY and relies on: stable exit codes,
``--no-input`` never blocking, ``--json`` emitting one object per line, an
absolute ``--dir`` used as is, and ``cinna account set-token`` refreshing an
account workspace in place. These tests pin that contract.
"""

import base64
import json
import stat
from pathlib import Path
from unittest.mock import patch

import click
import httpx
import pytest
from click.testing import CliRunner

from cinna import console
from cinna.account import (
    AccountConfig,
    account_config_path,
    load_account_config,
    save_account_config,
)
from cinna.errors import (
    EXIT_ACCOUNT_MISMATCH,
    EXIT_NETWORK,
    EXIT_SETUP_TOKEN,
    CinnaExit,
    MutagenNotFoundError,
    NeedsInputError,
)
from cinna.main import cli

SETUP_URL = "https://platform.example.com/api/cli-setup/account/TOK"
SETUP_COMMAND = f"curl -sL {SETUP_URL} | python3 -"


def _jwt(sub: str) -> str:
    """An unsigned JWT-shaped token carrying ``sub`` (never verified)."""

    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'none'})}.{b64({'sub': sub, 'token_type': 'cli-account'})}.sig"


def _payload(**overrides) -> dict:
    data = {
        "account_token": _jwt("user-1"),
        "platform_url": "https://platform.example.com",
        "frontend_url": "https://ui.example.com",
        "machine_name": "laptop",
    }
    data.update(overrides)
    return data


def _json_lines(result) -> list[dict]:
    """Every stdout line of a ``--json`` run must be a JSON object."""
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def account_cfg() -> AccountConfig:
    return AccountConfig(
        platform_url="https://platform.example.com",
        frontend_url="https://ui.example.com",
        account_token=_jwt("user-1"),
        machine_name="laptop",
        user_workspace_id="ws-1",
        user_workspace_name="Sales",
    )


@pytest.fixture
def account_root(tmp_path: Path, account_cfg: AccountConfig) -> Path:
    root = tmp_path / "my-cinna"
    root.mkdir()
    save_account_config(account_cfg, root)
    (root / "agents").mkdir()
    return root


@pytest.fixture
def no_context_package():
    """Setup's best-effort context download, stubbed to succeed quietly."""
    with patch("cinna.account._install_context_package", return_value=True) as m:
        yield m


# ── exit codes (§3.5) ────────────────────────────────────────────────────────


@patch("cinna.account.httpx.post")
def test_setup_token_rejected_exits_10(mock_post, runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mock_post.return_value = httpx.Response(
        400, json={"detail": "Setup token has already been used"}
    )
    result = runner.invoke(cli, ["account", "setup", SETUP_URL, "--name", "laptop"])
    assert result.exit_code == EXIT_SETUP_TOKEN
    assert "Setup token has already been used" in result.output
    assert not (tmp_path / "platform_example_com").exists()


@patch("cinna.account.httpx.post")
def test_setup_token_rejected_json_final_line(mock_post, runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mock_post.return_value = httpx.Response(410, json={"detail": "Setup token expired"})
    result = runner.invoke(
        cli, ["account", "setup", SETUP_COMMAND, "--name", "laptop", "--no-input", "--json"]
    )
    assert result.exit_code == EXIT_SETUP_TOKEN
    lines = _json_lines(result)
    assert lines[-1] == {
        "result": "error",
        "code": "setup_token_invalid",
        "detail": "Account setup failed: Setup token expired",
        "http_status": 410,
    }
    # Only the step that failed preceded it.
    assert lines[0] == {"step": 1, "total": 3, "status": "start", "message": "Authenticating..."}


@patch("cinna.account.httpx.post")
def test_setup_platform_5xx_exits_12(mock_post, runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mock_post.return_value = httpx.Response(503, json={"detail": "maintenance"})
    result = runner.invoke(cli, ["account", "setup", SETUP_URL, "--name", "laptop", "--json"])
    assert result.exit_code == EXIT_NETWORK
    assert _json_lines(result)[-1]["code"] == "platform_unavailable"


@patch("cinna.account.httpx.post", side_effect=httpx.ConnectError("refused"))
def test_setup_unreachable_exits_12(mock_post, runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli, ["account", "setup", SETUP_URL, "--name", "laptop", "--json"])
    assert result.exit_code == EXIT_NETWORK
    final = _json_lines(result)[-1]
    assert final["code"] == "network"
    assert "Could not reach" in final["detail"]


def test_transport_error_anywhere_maps_to_network(runner, account_root, monkeypatch):
    """A raw httpx transport error escaping any command body is exit 12."""
    monkeypatch.chdir(account_root)
    with patch(
        "cinna.account.probe_account_token",
        side_effect=httpx.ReadTimeout("slow"),
    ):
        result = runner.invoke(cli, ["account", "status"])
    assert result.exit_code == EXIT_NETWORK
    assert "Could not reach" in result.output


def test_plain_click_exception_is_normalized(runner, tmp_path, monkeypatch):
    """Errors raised as plain ClickException keep exit 1 and gain code 'error'."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CINNA_PLATFORM_URL", raising=False)
    result = runner.invoke(cli, ["account", "setup", "bare-token", "--name", "laptop", "--json"])
    assert result.exit_code == 1
    final = _json_lines(result)[-1]
    assert final["result"] == "error"
    assert final["code"] == "error"
    assert "Cannot determine platform URL" in final["detail"]


def test_plain_click_exception_human_output_unchanged(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CINNA_PLATFORM_URL", raising=False)
    result = runner.invoke(cli, ["account", "setup", "bare-token", "--name", "laptop"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Error: Cannot determine platform URL" in result.output


def test_unexpected_exception_json_mode_internal_error(runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    with patch("cinna.account.probe_account_token", side_effect=RuntimeError("boom")):
        result = runner.invoke(cli, ["account", "status", "--json"])
    assert result.exit_code == 1
    assert _json_lines(result)[-1] == {
        "result": "error",
        "code": "internal_error",
        "detail": "RuntimeError: boom",
    }


def test_unexpected_exception_human_mode_still_raises(runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    with patch("cinna.account.probe_account_token", side_effect=RuntimeError("boom")):
        result = runner.invoke(cli, ["account", "status"])
    assert isinstance(result.exception, RuntimeError)


def test_not_an_account_workspace_code(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli, ["account", "status", "--json"])
    assert result.exit_code == 1
    assert _json_lines(result)[-1]["code"] == "not_an_account_workspace"


def test_usage_error_keeps_click_rendering(runner):
    result = runner.invoke(cli, ["account", "setup"])
    assert result.exit_code == 2
    assert "Missing argument" in result.output


# ── absolute --dir (§3.2) ────────────────────────────────────────────────────


@patch("cinna.account.httpx.post")
def test_setup_absolute_dir_used_as_is_and_parents_created(
    mock_post, no_context_package, runner, tmp_path, monkeypatch
):
    work = tmp_path / "elsewhere"
    work.mkdir()
    monkeypatch.chdir(work)
    mock_post.return_value = httpx.Response(200, json=_payload())
    target = tmp_path / "Agents" / "Cloud" / "cinna.acme.com"  # parents missing

    result = runner.invoke(
        cli,
        ["account", "setup", SETUP_COMMAND, "--dir", str(target), "--name", "laptop",
         "--no-input", "--json"],
    )
    assert result.exit_code == 0, result.output
    assert account_config_path(target).is_file()
    assert not (work / "cinna.acme.com").exists()
    final = _json_lines(result)[-1]
    assert final["workspace"] == str(target)
    cfg = load_account_config(target)
    assert cfg.machine_name == "laptop"
    assert cfg.platform_url == "https://platform.example.com"


@patch("cinna.account.httpx.post")
def test_setup_relative_dir_under_cwd(mock_post, no_context_package, runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mock_post.return_value = httpx.Response(200, json=_payload())
    result = runner.invoke(
        cli, ["account", "setup", SETUP_URL, "--dir", "nested/team", "--name", "laptop"]
    )
    assert result.exit_code == 0, result.output
    assert account_config_path(tmp_path / "nested" / "team").is_file()
    assert "cd nested/team/" in result.output


@patch("cinna.account.httpx.post")
def test_setup_existing_absolute_dir_is_workspace_exists(
    mock_post, runner, account_root, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        cli,
        ["account", "setup", SETUP_COMMAND, "--dir", str(account_root), "--name", "laptop",
         "--no-input", "--json"],
    )
    assert result.exit_code == 1
    final = _json_lines(result)[-1]
    assert final["code"] == "workspace_exists"
    assert str(account_root) in final["detail"]
    mock_post.assert_not_called()  # the single-use token is not burned


def test_resolve_account_dir_contract(tmp_path, monkeypatch):
    from cinna.account import resolve_account_dir

    monkeypatch.chdir(tmp_path)
    assert resolve_account_dir(str(tmp_path / "abs")) == tmp_path / "abs"
    assert resolve_account_dir("rel/dir") == tmp_path / "rel" / "dir"
    assert resolve_account_dir("~/x") == Path.home() / "x"


# ── account set-token (§3.3) ─────────────────────────────────────────────────


@patch("cinna.account.httpx.post")
def test_account_set_token_swaps_token_in_place(
    mock_post, runner, account_root, account_cfg, monkeypatch
):
    monkeypatch.chdir(account_root)
    # A child workspace whose token must not be touched.
    child = account_root / "agents" / "crm"
    (child / ".cinna").mkdir(parents=True)
    (child / ".cinna" / "config.json").write_text('{"cli_token": "child"}')
    mock_post.return_value = httpx.Response(
        200,
        json=_payload(
            account_token=_jwt("user-1") + "new",
            frontend_url="https://ui2.example.com",
            machine_name="laptop",
        ),
    )

    result = runner.invoke(cli, ["account", "set-token", SETUP_COMMAND])
    assert result.exit_code == 0, result.output

    # Exchange used the stored machine name — no prompt, no --name.
    _, kwargs = mock_post.call_args
    assert kwargs["json"]["machine_name"] == "laptop"
    assert mock_post.call_args[0][0] == SETUP_URL

    cfg = load_account_config(account_root)
    assert cfg.account_token == _jwt("user-1") + "new"
    assert cfg.frontend_url == "https://ui2.example.com"
    assert cfg.platform_url == account_cfg.platform_url
    assert cfg.machine_name == "laptop"
    # Active user workspace preserved; child token untouched.
    assert cfg.user_workspace_id == "ws-1"
    assert cfg.user_workspace_name == "Sales"
    assert (child / ".cinna" / "config.json").read_text() == '{"cli_token": "child"}'
    assert "Account token refreshed" in result.output
    assert "cinna doctor" in result.output


@patch("cinna.account.httpx.post")
def test_account_set_token_bare_token_uses_stored_platform(
    mock_post, runner, account_root, monkeypatch
):
    monkeypatch.chdir(account_root)
    monkeypatch.delenv("CINNA_PLATFORM_URL", raising=False)
    mock_post.return_value = httpx.Response(200, json=_payload())
    result = runner.invoke(cli, ["account", "set-token", "BARE"])
    assert result.exit_code == 0, result.output
    assert mock_post.call_args[0][0] == (
        "https://platform.example.com/api/cli-setup/account/BARE"
    )


@patch("cinna.account.httpx.post")
def test_account_set_token_platform_mismatch_exits_11(
    mock_post, runner, account_root, account_cfg, monkeypatch
):
    monkeypatch.chdir(account_root)
    mock_post.return_value = httpx.Response(
        200, json=_payload(platform_url="https://other.example.com")
    )
    result = runner.invoke(cli, ["account", "set-token", SETUP_COMMAND, "--json"])
    assert result.exit_code == EXIT_ACCOUNT_MISMATCH
    final = _json_lines(result)[-1]
    assert final["code"] == "account_mismatch"
    assert "other.example.com" in final["detail"]
    # Nothing written.
    assert load_account_config(account_root).account_token == account_cfg.account_token


@patch("cinna.account.httpx.post")
def test_account_set_token_subject_mismatch_exits_11(
    mock_post, runner, account_root, account_cfg, monkeypatch
):
    monkeypatch.chdir(account_root)
    mock_post.return_value = httpx.Response(200, json=_payload(account_token=_jwt("user-2")))
    result = runner.invoke(cli, ["account", "set-token", SETUP_COMMAND])
    assert result.exit_code == EXIT_ACCOUNT_MISMATCH
    assert "different account" in result.output
    assert load_account_config(account_root).account_token == account_cfg.account_token


@patch("cinna.account.httpx.post")
def test_account_set_token_opaque_tokens_skip_subject_check(
    mock_post, runner, account_root, account_cfg, monkeypatch
):
    """Non-JWT tokens (no claims to compare) fall back to the origin check."""
    account_cfg.account_token = "opaque-old"
    save_account_config(account_cfg, account_root)
    monkeypatch.chdir(account_root)
    mock_post.return_value = httpx.Response(200, json=_payload(account_token="opaque-new"))
    result = runner.invoke(cli, ["account", "set-token", SETUP_COMMAND])
    assert result.exit_code == 0, result.output
    assert load_account_config(account_root).account_token == "opaque-new"


@patch("cinna.account.httpx.post")
def test_account_set_token_expired_setup_token_exits_10(
    mock_post, runner, account_root, account_cfg, monkeypatch
):
    monkeypatch.chdir(account_root)
    mock_post.return_value = httpx.Response(400, json={"detail": "Setup token expired"})
    result = runner.invoke(cli, ["account", "set-token", SETUP_COMMAND, "--no-input", "--json"])
    assert result.exit_code == EXIT_SETUP_TOKEN
    assert _json_lines(result)[-1]["code"] == "setup_token_invalid"
    assert load_account_config(account_root).account_token == account_cfg.account_token


def test_account_set_token_outside_workspace(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli, ["account", "set-token", SETUP_COMMAND])
    assert result.exit_code == 1
    assert "Not in a cinna account workspace" in result.output


@patch("cinna.account.httpx.post")
def test_login_404_hint_mentions_account_set_token(mock_post, runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    mock_post.return_value = httpx.Response(404, json={"detail": "not found"})
    result = runner.invoke(cli, ["login"])
    assert result.exit_code != 0
    assert "cinna account set-token" in result.output


@patch("cinna.account.probe_account_token", return_value="expired")
@patch("cinna.account.cli_version_status")
def test_status_expired_hint_mentions_account_set_token(
    mock_cli, _probe, runner, account_root, monkeypatch
):
    mock_cli.return_value = {"installed": "0.4.0", "required": None, "state": "unknown"}
    monkeypatch.chdir(account_root)
    result = runner.invoke(cli, ["account", "status"])
    assert result.exit_code == 0, result.output
    assert "cinna account set-token" in result.output


# ── --no-input (§3.1) ────────────────────────────────────────────────────────


def test_console_prompt_takes_default_or_fails():
    console.set_no_input(True)
    assert console.prompt("Name", default="x") == "x"
    with pytest.raises(NeedsInputError) as exc_info:
        console.prompt("Platform domain")
    assert exc_info.value.code == "needs_input"
    assert exc_info.value.exit_code == 1
    assert console.confirm("Continue?") is False
    assert console.confirm("Terminate?", default=True) is True
    with pytest.raises(NeedsInputError):
        console.confirm("Really?", default=None)
    with pytest.raises(click.Abort):
        console.confirm("Import?", abort=True)


def test_console_interactive_requires_tty_and_no_flag(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert console.interactive() is True
    console.set_no_input(True)
    assert console.interactive() is False


def test_group_level_no_input_fails_needs_input(runner, tmp_path, monkeypatch):
    """login without a domain would prompt; under --no-input it must fail fast."""
    empty = tmp_path / "fresh"
    empty.mkdir()
    monkeypatch.chdir(empty)
    result = runner.invoke(cli, ["--no-input", "login"])
    assert result.exit_code == 1
    assert "needs_input" not in result.output  # human text, code is machine-only
    assert "--no-input is set" in result.output
    assert "Platform domain" in result.output


def test_env_no_input(runner, tmp_path, monkeypatch):
    empty = tmp_path / "fresh"
    empty.mkdir()
    monkeypatch.chdir(empty)
    monkeypatch.setenv("CINNA_NO_INPUT", "1")
    result = runner.invoke(cli, ["login"])
    assert result.exit_code == 1
    assert "--no-input is set" in result.output


@patch("cinna.account.httpx.post")
def test_no_input_after_subcommand_skips_dir_prompt(
    mock_post, no_context_package, runner, tmp_path, monkeypatch
):
    """The flag placed after the subcommand (how the desktop invokes it) is
    honored, and the folder prompt takes the domain default even though the
    /dev/tty fallback would otherwise be tried."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    mock_post.return_value = httpx.Response(200, json=_payload())
    with patch("cinna.account.click.prompt") as forbidden:
        result = runner.invoke(
            cli, ["account", "setup", SETUP_URL, "--name", "laptop", "--no-input"]
        )
    assert result.exit_code == 0, result.output
    forbidden.assert_not_called()
    assert account_config_path(tmp_path / "platform_example_com").is_file()


@patch("cinna.account.httpx.post")
def test_no_input_machine_name_takes_default(
    mock_post, no_context_package, runner, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    mock_post.return_value = httpx.Response(200, json=_payload())
    with patch("cinna.main.click.prompt") as forbidden:
        result = runner.invoke(
            cli, ["--no-input", "account", "setup", SETUP_URL, "--dir", "ws"]
        )
    assert result.exit_code == 0, result.output
    forbidden.assert_not_called()
    body = mock_post.call_args[1]["json"]
    assert body["machine_name"]  # the default, not empty


def test_no_input_confirm_defaults_to_abort(runner, workspace_root, monkeypatch):
    """disconnect asks 'Continue?' (default No): under --no-input it aborts
    instead of hanging, and nothing is removed."""
    monkeypatch.chdir(workspace_root)
    result = runner.invoke(cli, ["--no-input", "disconnect"])
    assert result.exit_code == 1
    assert "Aborted" in result.output
    assert (workspace_root / ".cinna" / "config.json").is_file()


@patch("cinna.mutagen_runtime.detect_local_mutagen", return_value=None)
@patch("cinna.mutagen_runtime.fetch_required_mutagen")
def test_mutagen_missing_under_no_input_is_structured(mock_req, _detect, sample_config, tmp_path):
    from cinna.mutagen_runtime import RequiredMutagen, ensure_mutagen_ready

    mock_req.return_value = RequiredMutagen("0.18.1", "sha", "1")
    console.set_no_input(True)
    with patch("cinna.console.click.confirm") as forbidden:
        with pytest.raises(MutagenNotFoundError) as exc_info:
            ensure_mutagen_ready(object(), sample_config, tmp_path, interactive=True)
    forbidden.assert_not_called()
    assert exc_info.value.code == "mutagen_missing"
    assert exc_info.value.as_json()["required_version"] == "0.18.1"


@patch("cinna.mutagen_runtime.detect_local_mutagen")
@patch("cinna.mutagen_runtime.fetch_required_mutagen")
def test_mutagen_mismatch_under_no_input_is_structured(mock_req, mock_detect, sample_config, tmp_path):
    from cinna.errors import MutagenVersionMismatchError
    from cinna.mutagen_runtime import InstalledMutagen, RequiredMutagen, ensure_mutagen_ready

    mock_req.return_value = RequiredMutagen("0.18.1", "sha", "1")
    mock_detect.return_value = InstalledMutagen("/x/mutagen", "0.17.0")
    console.set_no_input(True)
    with pytest.raises(MutagenVersionMismatchError) as exc_info:
        ensure_mutagen_ready(object(), sample_config, tmp_path, interactive=True)
    assert exc_info.value.code == "mutagen_mismatch"
    assert exc_info.value.as_json()["installed_version"] == "0.17.0"


# ── --json (§3.4) ────────────────────────────────────────────────────────────


@patch("cinna.account.httpx.post")
def test_setup_json_progress_and_result(mock_post, runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mock_post.return_value = httpx.Response(200, json=_payload())
    target = tmp_path / "Cloud" / "platform.example.com"
    with patch("cinna.account._install_context_package", return_value=True):
        result = runner.invoke(
            cli,
            ["account", "setup", SETUP_COMMAND, "--dir", str(target), "--name", "laptop",
             "--no-input", "--json"],
        )
    assert result.exit_code == 0, result.output
    lines = _json_lines(result)
    assert lines == [
        {"step": 1, "total": 3, "status": "start", "message": "Authenticating..."},
        {"step": 2, "total": 3, "status": "start", "message": "Creating account workspace..."},
        {"step": 3, "total": 3, "status": "start", "message": "Downloading context package..."},
        {"step": 3, "total": 3, "status": "ok", "message": "Account workspace created!"},
        {
            "result": "ok",
            "workspace": str(target),
            "platform_url": "https://platform.example.com",
            "frontend_url": "https://ui.example.com",
            "machine_name": "laptop",
            "context_package": "ok",
        },
    ]
    # No Rich output leaked onto stdout.
    assert "cinna account agents" not in result.stdout


@patch("cinna.account.httpx.post")
def test_setup_json_context_failure_is_warn_not_error(mock_post, runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mock_post.return_value = httpx.Response(200, json=_payload())
    with patch("cinna.account.AccountClient") as client_cls:
        client_cls.return_value.__enter__.return_value.download_context_package.side_effect = (
            httpx.ConnectError("refused")
        )
        result = runner.invoke(
            cli, ["account", "setup", SETUP_URL, "--dir", "ws", "--name", "laptop", "--json"]
        )
    assert result.exit_code == 0, result.output
    lines = _json_lines(result)
    warns = [ln for ln in lines if ln.get("status") == "warn"]
    assert warns and warns[0]["step"] == 3
    assert "Context package download failed" in warns[0]["message"]
    assert lines[-1]["result"] == "ok"
    assert lines[-1]["context_package"] == "failed"


@patch("cinna.account.httpx.post")
def test_set_token_json(mock_post, runner, account_root, monkeypatch):
    monkeypatch.chdir(account_root)
    mock_post.return_value = httpx.Response(200, json=_payload())
    result = runner.invoke(cli, ["account", "set-token", SETUP_COMMAND, "--no-input", "--json"])
    assert result.exit_code == 0, result.output
    lines = _json_lines(result)
    assert lines[0] == {"step": 1, "total": 2, "status": "start", "message": "Authenticating..."}
    assert lines[1] == {
        "step": 2, "total": 2, "status": "start", "message": "Updating account workspace...",
    }
    assert lines[-1] == {
        "result": "ok",
        "workspace": str(account_root),
        "platform_url": "https://platform.example.com",
        "frontend_url": "https://ui.example.com",
        "machine_name": "laptop",
        "context_package": "skipped",
    }


@patch("cinna.account.cli_version_status")
@patch("cinna.account.context_package_status", return_value=("current", "v3", "v3"))
@patch("cinna.account.probe_account_token", return_value="valid")
def test_status_json(_probe, _pkg, mock_cli, runner, account_root, monkeypatch):
    from cinna.config import CinnaConfig, save_config

    mock_cli.return_value = {"installed": "0.4.0", "required": "0.4.0", "state": "current"}
    child = account_root / "agents" / "crm-agent"
    save_config(
        CinnaConfig(
            platform_url="https://platform.example.com",
            cli_token="t",
            agent_id="agent-1",
            agent_name="CRM Agent",
            environment_id="env",
            template="general-env",
        ),
        child,
    )
    monkeypatch.chdir(account_root)
    result = runner.invoke(cli, ["account", "status", "--json"])
    assert result.exit_code == 0, result.output
    lines = _json_lines(result)
    assert len(lines) == 1
    assert lines[0] == {
        "result": "ok",
        "workspace": str(account_root),
        "platform_url": "https://platform.example.com",
        "frontend_url": "https://ui.example.com",
        "machine_name": "laptop",
        "active_workspace": {"id": "ws-1", "name": "Sales"},
        "token": "valid",
        "synced_agents": 1,
        "agents": [
            {
                "agent_id": "agent-1",
                "name": "CRM Agent",
                "path": str(child),
                "last_sync_connected_at": None,
            }
        ],
        "context_package": {"local": "v3", "remote": "v3", "state": "current"},
        "cli": {"installed": "0.4.0", "required": "0.4.0", "state": "current"},
    }


@patch("cinna.account.cli_version_status")
@patch("cinna.account.context_package_status", return_value=("unreachable", None, None))
@patch("cinna.account.probe_account_token", return_value="expired")
def test_status_json_expired_token_is_still_ok_result(
    _probe, _pkg, mock_cli, runner, account_root, account_cfg, monkeypatch
):
    mock_cli.return_value = {"installed": "0.4.0", "required": None, "state": "unknown"}
    account_cfg.user_workspace_id = None
    account_cfg.user_workspace_name = None
    save_account_config(account_cfg, account_root)
    monkeypatch.chdir(account_root)
    result = runner.invoke(cli, ["account", "status", "--json"])
    assert result.exit_code == 0, result.output
    final = _json_lines(result)[-1]
    assert final["token"] == "expired"
    assert final["active_workspace"] is None
    assert final["synced_agents"] == 0
    assert final["cli"]["state"] == "unknown"


def test_json_implies_no_input():
    console.set_json_mode(True)
    assert console.no_input is True
    console.set_json_mode(False)
    assert console.json_mode is False


def test_cinna_exit_show_renders_json_only_in_json_mode(capsys):
    exc = CinnaExit(1, "some_code", "what happened", extra={"x": 1})
    exc.show()
    assert capsys.readouterr().err.strip() == "Error: what happened"
    console.set_json_mode(True)
    exc.show()
    out = capsys.readouterr()
    assert json.loads(out.out) == {
        "result": "error", "code": "some_code", "detail": "what happened", "x": 1,
    }
    assert out.err == ""


# ── Mutagen binary override (§3.6) ───────────────────────────────────────────


def test_cinna_mutagen_bin_override(tmp_path, monkeypatch):
    from cinna.mutagen_runtime import detect_local_mutagen, mutagen_binary

    fake = tmp_path / "mutagen"
    fake.write_text("#!/bin/sh\necho 'Mutagen version 0.18.1'\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("CINNA_MUTAGEN_BIN", str(fake))
    assert mutagen_binary() == str(fake)
    with patch("cinna.mutagen_runtime.shutil.which") as which:
        found = detect_local_mutagen()
    which.assert_not_called()
    assert found is not None
    assert found.path == str(fake)
    assert found.version == "0.18.1"


def test_cinna_mutagen_bin_not_executable_counts_as_missing(tmp_path, monkeypatch):
    from cinna.mutagen_runtime import detect_local_mutagen

    monkeypatch.setenv("CINNA_MUTAGEN_BIN", str(tmp_path / "nope"))
    with patch("cinna.mutagen_runtime.shutil.which") as which:
        assert detect_local_mutagen() is None
    which.assert_not_called()


def test_mutagen_binary_defaults_to_path_lookup(monkeypatch):
    from cinna.mutagen_runtime import mutagen_binary

    monkeypatch.delenv("CINNA_MUTAGEN_BIN", raising=False)
    assert mutagen_binary() == "mutagen"


def test_sync_session_uses_mutagen_binary(sample_config, monkeypatch):
    from cinna import sync_session

    monkeypatch.setenv("CINNA_MUTAGEN_BIN", "/opt/cinna/mutagen")
    with patch("cinna.sync_session.subprocess.run") as run:
        with patch("cinna.sync_session._ensure_ssh_shim_dir", return_value=Path("/tmp/shim")):
            sync_session._run_mutagen(["version"], sample_config)
    assert run.call_args[0][0][0] == "/opt/cinna/mutagen"


# ── cinna-cli version pin (§3.7) ─────────────────────────────────────────────


@patch("cinna.account.cli_version_status")
@patch("cinna.account.context_package_status", return_value=("current", "v3", "v3"))
@patch("cinna.account.probe_account_token", return_value="valid")
def test_status_human_reports_behind_pin(_probe, _pkg, mock_cli, runner, account_root, monkeypatch):
    mock_cli.return_value = {"installed": "0.3.0", "required": "0.4.0", "state": "behind"}
    monkeypatch.chdir(account_root)
    monkeypatch.setenv("COLUMNS", "200")
    result = runner.invoke(cli, ["account", "status"])
    assert result.exit_code == 0, result.output
    assert "cinna-cli" in result.output
    assert "uv tool install cinna-cli==0.4.0" in result.output


@patch("cinna.cli_version.cli_version_status")
@patch("cinna.main._probe_token_statuses", return_value={})
@patch("cinna.doctor.sync_session.list_all_sessions", return_value=[])
def test_doctor_reports_cli_behind_pin(_sessions, _probe, mock_cli, runner, account_root, monkeypatch):
    mock_cli.return_value = {"installed": "0.3.0", "required": "0.4.0", "state": "behind"}
    monkeypatch.chdir(account_root)
    monkeypatch.setenv("COLUMNS", "200")
    result = runner.invoke(cli, ["doctor", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "platform pins" in result.output
    assert "uv tool install cinna-cli==0.4.0" in result.output
    mock_cli.assert_called_once_with("https://platform.example.com")


@patch("cinna.cli_version.cli_version_status")
@patch("cinna.main._probe_token_statuses", return_value={})
@patch("cinna.doctor.sync_session.list_all_sessions", return_value=[])
def test_doctor_quiet_when_pin_unknown(_sessions, _probe, mock_cli, runner, account_root, monkeypatch):
    mock_cli.return_value = {"installed": "0.4.0", "required": None, "state": "unknown"}
    monkeypatch.chdir(account_root)
    result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "Everything looks healthy" in result.output


def test_console_step_state_reset_between_json_sessions(capsys):
    console.set_json_mode(True)
    console.step(2, 5, "two")
    console.set_json_mode(True)
    console.status("no step")
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert out[-1] == {"status": "ok", "message": "no step"}
