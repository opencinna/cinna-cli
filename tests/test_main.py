"""CLI command integration tests."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from click.testing import CliRunner

from cinna.main import cli
from cinna.config import save_config


@pytest.fixture
def runner():
    return CliRunner()


def test_version(runner):
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output


def test_status_no_workspace(runner, tmp_path):
    result = runner.invoke(cli, ["status"], catch_exceptions=False)
    # Should fail because we're not in a workspace
    assert result.exit_code != 0


@patch("cinna.main.get_container_status")
@patch("cinna.main.find_workspace_root")
@patch("cinna.main.load_config")
def test_status_command(mock_load, mock_find, mock_status, runner, workspace_root, sample_config):
    mock_find.return_value = workspace_root
    mock_load.return_value = sample_config
    mock_status.return_value = {"running": True, "image": "test-img", "status": "running", "name": "my-container", "id": "abc123"}

    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0
    assert "test-agent" in result.output


@patch("cinna.main.exec_in_container")
@patch("cinna.main.find_workspace_root")
@patch("cinna.main.load_config")
def test_exec_command(mock_load, mock_find, mock_exec, runner, workspace_root, sample_config):
    mock_find.return_value = workspace_root
    mock_load.return_value = sample_config
    mock_exec.return_value = 0

    result = runner.invoke(cli, ["exec", "python", "scripts/main.py"])
    assert result.exit_code == 0
    mock_exec.assert_called_once_with(sample_config, ["python", "scripts/main.py"], workspace_root)


@patch("cinna.main.push_workspace")
@patch("cinna.main.PlatformClient")
@patch("cinna.main.find_workspace_root")
@patch("cinna.main.load_config")
def test_push_command(mock_load, mock_find, mock_client_cls, mock_push, runner, workspace_root, sample_config):
    mock_find.return_value = workspace_root
    mock_load.return_value = sample_config

    result = runner.invoke(cli, ["push"])
    assert result.exit_code == 0
    mock_push.assert_called_once()


@patch("cinna.main.pull_workspace")
@patch("cinna.main.PlatformClient")
@patch("cinna.main.find_workspace_root")
@patch("cinna.main.load_config")
def test_pull_command(mock_load, mock_find, mock_client_cls, mock_pull, runner, workspace_root, sample_config):
    mock_find.return_value = workspace_root
    mock_load.return_value = sample_config

    result = runner.invoke(cli, ["pull"])
    assert result.exit_code == 0
    mock_pull.assert_called_once()


@patch("cinna.main.pull_credentials")
@patch("cinna.main.PlatformClient")
@patch("cinna.main.find_workspace_root")
@patch("cinna.main.load_config")
def test_credentials_command(mock_load, mock_find, mock_client_cls, mock_creds, runner, workspace_root, sample_config):
    mock_find.return_value = workspace_root
    mock_load.return_value = sample_config

    result = runner.invoke(cli, ["credentials"])
    assert result.exit_code == 0
    mock_creds.assert_called_once()


@patch("cinna.main.start_container")
@patch("cinna.main.build_container")
@patch("cinna.main.destroy_container")
@patch("cinna.main.find_workspace_root")
@patch("cinna.main.load_config")
def test_rebuild_command(mock_load, mock_find, mock_destroy, mock_build, mock_start, runner, workspace_root, sample_config):
    mock_find.return_value = workspace_root
    mock_load.return_value = sample_config

    result = runner.invoke(cli, ["rebuild"])
    assert result.exit_code == 0
    mock_destroy.assert_called_once()
    mock_build.assert_called_once()
    mock_start.assert_called_once()


@patch("cinna.main.start_container")
@patch("cinna.main.is_container_running")
@patch("cinna.main.find_workspace_root")
@patch("cinna.main.load_config")
def test_env_up_command(mock_load, mock_find, mock_running, mock_start, runner, workspace_root, sample_config):
    mock_find.return_value = workspace_root
    mock_load.return_value = sample_config
    mock_running.return_value = False

    result = runner.invoke(cli, ["env-up"])
    assert result.exit_code == 0
    mock_start.assert_called_once()


@patch("cinna.main.destroy_container")
@patch("cinna.main.is_container_running")
@patch("cinna.main.find_workspace_root")
@patch("cinna.main.load_config")
def test_env_down_command(mock_load, mock_find, mock_running, mock_destroy, runner, workspace_root, sample_config):
    mock_find.return_value = workspace_root
    mock_load.return_value = sample_config
    mock_running.return_value = True

    result = runner.invoke(cli, ["env-down"])
    assert result.exit_code == 0
    mock_destroy.assert_called_once()
