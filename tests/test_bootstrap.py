"""Tests for bootstrap module — setup input parsing and name normalization."""

import pytest

from cinna.bootstrap import parse_setup_input, normalize_agent_dir_name


def test_parse_full_curl_command():
    raw = "curl -sL http://localhost:8000/cli-setup/DIV-TAOKD2wScn6XJrFs9WuwVrU8fQqk | python3 -"
    url, token = parse_setup_input(raw)
    assert url == "http://localhost:8000"
    assert token == "DIV-TAOKD2wScn6XJrFs9WuwVrU8fQqk"


def test_parse_curl_command_https():
    raw = "curl -sL https://app.example.com/cli-setup/tok_abc123 | python3 -"
    url, token = parse_setup_input(raw)
    assert url == "https://app.example.com"
    assert token == "tok_abc123"


def test_parse_url_only():
    raw = "http://localhost:8000/cli-setup/DIV-TAOKD2wScn6XJrFs9WuwVrU8fQqk"
    url, token = parse_setup_input(raw)
    assert url == "http://localhost:8000"
    assert token == "DIV-TAOKD2wScn6XJrFs9WuwVrU8fQqk"


def test_parse_url_with_quotes():
    raw = "'https://app.example.com/cli-setup/tok_abc123'"
    url, token = parse_setup_input(raw)
    assert url == "https://app.example.com"
    assert token == "tok_abc123"


def test_parse_url_with_port():
    raw = "http://192.168.1.10:9000/cli-setup/my-token-123"
    url, token = parse_setup_input(raw)
    assert url == "http://192.168.1.10:9000"
    assert token == "my-token-123"


def test_parse_url_with_api_prefix():
    raw = "https://app.example.com/api/cli-setup/tok_abc123"
    url, token = parse_setup_input(raw)
    assert url == "https://app.example.com/api"
    assert token == "tok_abc123"


def test_parse_curl_with_api_prefix():
    raw = "curl -sL https://app.example.com/api/cli-setup/tok_abc123 | python3 -"
    url, token = parse_setup_input(raw)
    assert url == "https://app.example.com/api"
    assert token == "tok_abc123"


def test_parse_raw_token_with_env(monkeypatch):
    monkeypatch.setenv("CINNA_PLATFORM_URL", "https://app.example.com")
    url, token = parse_setup_input("tok_abc123")
    assert url == "https://app.example.com"
    assert token == "tok_abc123"


def test_parse_raw_token_without_env(monkeypatch):
    monkeypatch.delenv("CINNA_PLATFORM_URL", raising=False)
    with pytest.raises(Exception, match="Cannot determine platform URL"):
        parse_setup_input("tok_abc123")


def test_parse_bad_url():
    with pytest.raises(Exception, match="Could not parse setup URL"):
        parse_setup_input("https://example.com/no-cli-setup-here")


def test_parse_curl_without_cli_setup_path():
    with pytest.raises(Exception, match="Could not parse setup URL"):
        parse_setup_input("curl -sL https://example.com/other/path | python3 -")


# --- normalize_agent_dir_name ---


def test_normalize_spaces_and_caps():
    assert normalize_agent_dir_name("HR Manager Agent") == "hr-manager-agent"


def test_normalize_special_chars():
    assert normalize_agent_dir_name("My  Cool--Agent!") == "my-cool-agent"


def test_normalize_already_clean():
    assert normalize_agent_dir_name("my-agent") == "my-agent"


def test_normalize_underscores():
    assert normalize_agent_dir_name("data_pipeline_v2") == "data-pipeline-v2"


def test_normalize_leading_trailing():
    assert normalize_agent_dir_name("  --Agent-- ") == "agent"


def test_normalize_empty():
    assert normalize_agent_dir_name("") == "agent"


def test_normalize_unicode():
    assert normalize_agent_dir_name("Agen't #1 (test)") == "agen-t-1-test"
