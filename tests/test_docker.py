"""Tests for docker module."""

import io
import tarfile
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from cinna.docker import (
    check_docker_available,
    ensure_dev_compose_override,
    extract_build_context,
    is_container_running,
    get_container_status,
    destroy_container,
)
from cinna.errors import DockerNotFoundError


def _make_tarball(files: dict[str, bytes]) -> bytes:
    """Create a gzipped tarball with the given files."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


@patch("cinna.docker.subprocess.run")
def test_check_docker_available_success(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    check_docker_available()  # Should not raise
    assert mock_run.call_count == 2


@patch("cinna.docker.subprocess.run", side_effect=FileNotFoundError)
def test_check_docker_available_missing(mock_run):
    with pytest.raises(DockerNotFoundError):
        check_docker_available()


def test_extract_build_context(tmp_path):
    tarball = _make_tarball({
        "Dockerfile": b"FROM python:3.12",
        "docker-compose.yml": b"version: '3'",
    })
    extract_build_context(tarball, tmp_path)

    build = tmp_path / ".cinna" / "build"
    assert (build / "Dockerfile").read_bytes() == b"FROM python:3.12"
    assert (build / "docker-compose.yml").read_bytes() == b"version: '3'"


def test_extract_build_context_rejects_path_traversal(tmp_path):
    # Create tarball with path traversal
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="../../../etc/passwd")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"evil"))
    tarball = buf.getvalue()

    with pytest.raises(Exception):
        extract_build_context(tarball, tmp_path)


@patch("cinna.docker.subprocess.run")
def test_ensure_dev_compose_override(mock_run, tmp_path, sample_config):
    """Writes override with entrypoint, container_name, and image."""
    from cinna.config import save_config

    save_config(sample_config, tmp_path)
    build = tmp_path / ".cinna" / "build"
    build.mkdir(parents=True, exist_ok=True)
    (build / "docker-compose.yml").write_text("services:\n  agent:\n    image: test\n")

    mock_run.return_value = MagicMock(returncode=0, stdout="agent\n")

    ensure_dev_compose_override(tmp_path)

    override = build / "docker-compose.override.yml"
    assert override.exists()
    content = override.read_text()
    assert "sleep" in content
    assert "infinity" in content
    assert "agent:" in content
    assert "entrypoint:" in content
    assert "command: []" in content
    assert f"container_name: {sample_config.container_name}" in content
    assert f"image: cinna-dev-{sample_config.container_name}" in content
    assert "volumes:" in content
    assert "/app/workspace" in content


@patch("cinna.docker.subprocess.run")
def test_ensure_dev_compose_override_regenerates(mock_run, tmp_path, sample_config):
    """Regenerates override file even if one already exists."""
    from cinna.config import save_config

    save_config(sample_config, tmp_path)
    build = tmp_path / ".cinna" / "build"
    build.mkdir(parents=True, exist_ok=True)
    (build / "docker-compose.yml").write_text("services:\n  agent:\n    image: test\n")
    override = build / "docker-compose.override.yml"
    override.write_text("# stale override\n")

    mock_run.return_value = MagicMock(returncode=0, stdout="agent\n")

    ensure_dev_compose_override(tmp_path)

    content = override.read_text()
    assert "# stale override" not in content
    assert "sleep" in content
    assert "volumes:" in content


@patch("cinna.docker.subprocess.run")
def test_ensure_dev_compose_override_no_compose_file(mock_run, tmp_path):
    """Skips gracefully when no compose file exists."""
    build = tmp_path / ".cinna" / "build"
    build.mkdir(parents=True)

    ensure_dev_compose_override(tmp_path)

    assert not (build / "docker-compose.override.yml").exists()
    mock_run.assert_not_called()


@patch("cinna.docker.subprocess.run")
def test_is_container_running_true(mock_run, tmp_path):
    build = tmp_path / ".cinna" / "build"
    build.mkdir(parents=True)
    mock_run.return_value = MagicMock(returncode=0, stdout="abc123\n")
    assert is_container_running(tmp_path) is True


@patch("cinna.docker.subprocess.run")
def test_is_container_running_false(mock_run, tmp_path):
    build = tmp_path / ".cinna" / "build"
    build.mkdir(parents=True)
    mock_run.return_value = MagicMock(returncode=0, stdout="")
    assert is_container_running(tmp_path) is False


@patch("cinna.docker.subprocess.run")
def test_is_container_running_no_build_dir(mock_run, tmp_path):
    assert is_container_running(tmp_path) is False
    mock_run.assert_not_called()


@patch("cinna.docker.subprocess.run")
def test_destroy_container(mock_run, tmp_path):
    build = tmp_path / ".cinna" / "build"
    build.mkdir(parents=True)
    mock_run.return_value = MagicMock(returncode=0)
    destroy_container(tmp_path)
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert "down" in args


@patch("cinna.docker.subprocess.run")
def test_get_container_status_not_found(mock_run, sample_config, tmp_path):
    build = tmp_path / ".cinna" / "build"
    build.mkdir(parents=True)
    mock_run.return_value = MagicMock(returncode=1, stdout="")
    result = get_container_status(sample_config, tmp_path)
    assert result["running"] is False
    assert result["status"] == "not found"


@patch("cinna.docker.subprocess.run")
def test_get_container_status_running_jsonl(mock_run, sample_config, tmp_path):
    """Newer docker compose: one JSON object per line."""
    build = tmp_path / ".cinna" / "build"
    build.mkdir(parents=True)
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout='{"ID":"abc123","Name":"my-container","Image":"myimg","State":"running"}\n',
    )
    result = get_container_status(sample_config, tmp_path)
    assert result["running"] is True
    assert result["image"] == "myimg"
    assert result["name"] == "my-container"
    assert result["id"] == "abc123"


@patch("cinna.docker.subprocess.run")
def test_get_container_status_running_array(mock_run, sample_config, tmp_path):
    """Older docker compose: JSON array output."""
    build = tmp_path / ".cinna" / "build"
    build.mkdir(parents=True)
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout='[{"ID":"abc123","Name":"my-container","Image":"myimg","State":"running"}]\n',
    )
    result = get_container_status(sample_config, tmp_path)
    assert result["running"] is True
    assert result["image"] == "myimg"
    assert result["name"] == "my-container"
    assert result["id"] == "abc123"
