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


# --- editable installs: a version that is a date stamp, not a version ---


class _FakeDistribution:
    """A distribution whose only interesting file is its PEP 610 record."""

    def __init__(self, direct_url: str | None):
        self._direct_url = direct_url

    def read_text(self, filename: str) -> str | None:
        return self._direct_url if filename == "direct_url.json" else None


def _patch_distribution(monkeypatch, direct_url: str | None):
    import importlib.metadata

    monkeypatch.setattr(
        importlib.metadata,
        "distribution",
        lambda name: _FakeDistribution(direct_url),
    )


def test_editable_install_is_detected_and_names_its_checkout(monkeypatch):
    from cinna.cli_version import editable_install_source

    _patch_distribution(
        monkeypatch,
        '{"url":"file:///Users/dev/cinna-cli","dir_info":{"editable":true}}',
    )
    assert editable_install_source() == "/Users/dev/cinna-cli"


@pytest.mark.parametrize(
    "direct_url",
    [
        None,
        "",
        "not json",
        '{"url":"file:///Users/dev/cinna-cli","dir_info":{}}',
        '{"url":"https://files.example.com/cinna_cli-0.4.0-py3-none-any.whl"}',
    ],
)
def test_an_ordinary_install_is_never_mistaken_for_an_editable_one(
    monkeypatch, direct_url
):
    """A false *editable* silences the upgrade warning a real deployment needs,
    so every unreadable case has to land on 'ordinary install'."""
    from cinna.cli_version import editable_install_source

    _patch_distribution(monkeypatch, direct_url)
    assert editable_install_source() is None


@respx.mock
def test_an_editable_checkout_is_never_reported_as_behind_the_pin(monkeypatch):
    """The recorded version is the day it was installed, not the code that
    runs — so the comparison is not wrong, it is meaningless. Reporting it as
    'behind 0.4.0' sends a developer looking for version skew to explain
    features their own tree already has."""
    from cinna import cli_version

    respx.get("https://platform.example.com/.well-known/cinna-desktop").respond(
        200, json={"local_dev": {"cinna_cli_version": "0.4.0"}}
    )
    monkeypatch.setattr(cli_version, "__version__", "0.2.5")
    monkeypatch.setattr(
        cli_version, "editable_install_source", lambda: "/Users/dev/cinna-cli"
    )

    status = cli_version.cli_version_status("https://platform.example.com")
    assert status["state"] == "unknown"
    assert status["required"] == "0.4.0"
    assert cli_version.cli_version_hint(status) is None
    assert cli_version.cli_version_label(status) == (
        "0.2.5 (editable checkout of /Users/dev/cinna-cli)"
    )


@respx.mock
def test_a_real_install_still_gets_the_upgrade_nudge(monkeypatch):
    from cinna import cli_version

    respx.get("https://platform.example.com/.well-known/cinna-desktop").respond(
        200, json={"local_dev": {"cinna_cli_version": "0.4.0"}}
    )
    monkeypatch.setattr(cli_version, "__version__", "0.2.5")
    monkeypatch.setattr(cli_version, "editable_install_source", lambda: None)

    status = cli_version.cli_version_status("https://platform.example.com")
    assert status["state"] == "behind"
    assert "uv tool install cinna-cli==0.4.0" in cli_version.cli_version_hint(status)
    assert cli_version.cli_version_label(status) == "0.2.5"
