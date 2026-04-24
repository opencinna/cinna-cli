"""Tests for mutagen_runtime module."""

from unittest.mock import MagicMock, patch

import pytest

from cinna.errors import MutagenNotFoundError, MutagenVersionMismatchError
from cinna.mutagen_runtime import (
    InstalledMutagen,
    RequiredMutagen,
    _parse_mutagen_version,
    detect_local_mutagen,
    ensure_mutagen_ready,
    fetch_required_mutagen,
)


def test_parse_version_simple():
    assert _parse_mutagen_version("Mutagen version 0.18.3") == "0.18.3"


def test_parse_version_with_commit():
    assert _parse_mutagen_version("Mutagen 0.18.3-dev (abc1234)") == "0.18.3-dev"


def test_parse_version_unknown_format():
    assert _parse_mutagen_version("no numbers here") is None


@patch("cinna.mutagen_runtime.shutil.which")
def test_detect_local_mutagen_missing(mock_which):
    mock_which.return_value = None
    assert detect_local_mutagen() is None


@patch("cinna.mutagen_runtime.subprocess.run")
@patch("cinna.mutagen_runtime.shutil.which")
def test_detect_local_mutagen_found(mock_which, mock_run):
    mock_which.return_value = "/usr/local/bin/mutagen"
    mock_run.return_value = MagicMock(stdout="Mutagen version 0.18.3\n")
    result = detect_local_mutagen()
    assert result is not None
    assert result.version == "0.18.3"


def test_fetch_required_mutagen():
    client = MagicMock()
    client.get_sync_runtime.return_value = {
        "mutagen_version": "0.18.3",
        "mutagen_agent_sha256": "abc",
        "platform_api_version": "1.0",
    }
    required = fetch_required_mutagen(client, "agent-id")
    assert required.version == "0.18.3"
    assert required.agent_sha256 == "abc"


@patch("cinna.mutagen_runtime.detect_local_mutagen")
@patch("cinna.mutagen_runtime.fetch_required_mutagen")
def test_ensure_missing_raises(mock_fetch, mock_detect, tmp_path, sample_config):
    from cinna.config import save_config

    save_config(sample_config, tmp_path)
    mock_fetch.return_value = RequiredMutagen(version="0.18.3", agent_sha256="", platform_api_version="1.0")
    mock_detect.return_value = None  # never installs

    client = MagicMock()
    with pytest.raises(MutagenNotFoundError):
        ensure_mutagen_ready(client, sample_config, tmp_path, interactive=False)


@patch("cinna.mutagen_runtime.detect_local_mutagen")
@patch("cinna.mutagen_runtime.fetch_required_mutagen")
def test_ensure_minor_mismatch_blocks_non_interactive(mock_fetch, mock_detect, tmp_path, sample_config):
    from cinna.config import save_config

    save_config(sample_config, tmp_path)
    mock_fetch.return_value = RequiredMutagen(version="0.18.3", agent_sha256="", platform_api_version="1.0")
    mock_detect.return_value = InstalledMutagen(path="/usr/bin/mutagen", version="0.17.0")

    client = MagicMock()
    with pytest.raises(MutagenVersionMismatchError):
        ensure_mutagen_ready(client, sample_config, tmp_path, interactive=False)


@patch("cinna.mutagen_runtime.detect_local_mutagen")
@patch("cinna.mutagen_runtime.fetch_required_mutagen")
def test_ensure_patch_mismatch_allowed(mock_fetch, mock_detect, tmp_path, sample_config):
    """0.18.1 vs 0.18.3 — same minor, wire-compatible, should proceed."""
    from cinna.config import load_config, save_config

    save_config(sample_config, tmp_path)
    mock_fetch.return_value = RequiredMutagen(version="0.18.3", agent_sha256="", platform_api_version="1.0")
    mock_detect.return_value = InstalledMutagen(path="/usr/bin/mutagen", version="0.18.1")

    client = MagicMock()
    ensure_mutagen_ready(client, sample_config, tmp_path, interactive=False)

    reloaded = load_config(tmp_path)
    assert reloaded.mutagen_version == "0.18.1"


@patch("cinna.mutagen_runtime.detect_local_mutagen")
@patch("cinna.mutagen_runtime.fetch_required_mutagen")
def test_ensure_matches_updates_config(mock_fetch, mock_detect, tmp_path, sample_config):
    from cinna.config import load_config, save_config

    save_config(sample_config, tmp_path)
    mock_fetch.return_value = RequiredMutagen(version="0.18.3", agent_sha256="", platform_api_version="1.0")
    mock_detect.return_value = InstalledMutagen(path="/usr/bin/mutagen", version="0.18.3")

    client = MagicMock()
    ensure_mutagen_ready(client, sample_config, tmp_path, interactive=False)

    reloaded = load_config(tmp_path)
    assert reloaded.mutagen_version == "0.18.3"
    assert reloaded.last_sync_runtime_check_at is not None
