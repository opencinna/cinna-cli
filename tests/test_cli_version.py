"""Installed-vs-pinned cinna-cli version (the platform's discovery document)."""

import httpx
import pytest
import respx


@pytest.mark.parametrize(
    "installed,required,expected",
    [
        ("0.4.0", "0.4.0", "current"),
        ("0.3.0", "0.4.0", "behind"),
        ("0.4.1", "0.4.0", "ahead"),
        ("0.4.0", None, "unknown"),
        ("0.0.0+unknown", "0.4.0", "unknown"),
        ("0.4", "0.4.0", "current"),
    ],
)
def test_compare_cli_version(installed, required, expected):
    from cinna.cli_version import compare_cli_version

    assert compare_cli_version(installed, required) == expected


@respx.mock
def test_fetch_required_cli_version_from_discovery():
    from cinna.cli_version import fetch_required_cli_version

    respx.get("https://platform.example.com/.well-known/cinna-desktop").respond(
        200, json={"version": "1", "local_dev": {"cinna_cli_version": "0.4.0"}}
    )
    assert fetch_required_cli_version("https://platform.example.com/api") == "0.4.0"


@respx.mock
def test_fetch_required_cli_version_not_supported_yet():
    from cinna.cli_version import fetch_required_cli_version

    respx.get("https://platform.example.com/.well-known/cinna-desktop").respond(
        200, json={"version": "1"}
    )
    assert fetch_required_cli_version("https://platform.example.com") is None
    respx.get("https://old.example.com/.well-known/cinna-desktop").respond(404)
    assert fetch_required_cli_version("https://old.example.com") is None
    respx.get("https://down.example.com/.well-known/cinna-desktop").mock(
        side_effect=httpx.ConnectError("refused")
    )
    assert fetch_required_cli_version("https://down.example.com") is None


def test_required_cli_version_from_sync_runtime_shape():
    from cinna.cli_version import required_cli_version_from

    assert required_cli_version_from({"mutagen_version": "0.18.1"}) is None
    assert required_cli_version_from({"cinna_cli_version": "0.5.0"}) == "0.5.0"
