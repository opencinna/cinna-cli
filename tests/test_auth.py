"""Tests for auth module."""

import base64
import json
import time

from cinna.auth import get_auth_headers, validate_token_locally, is_token_expired


def test_get_auth_headers(sample_config):
    headers = get_auth_headers(sample_config)
    assert headers == {"Authorization": "Bearer test-token-abc123"}


def _make_jwt(payload: dict) -> str:
    """Create a fake JWT with the given payload (no real signature)."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256"}).encode()).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(b"fake-signature").rstrip(b"=").decode()
    return f"{header}.{body}.{sig}"


def test_validate_token_locally_valid():
    payload = {"sub": "user-123", "exp": int(time.time()) + 3600}
    token = _make_jwt(payload)
    result = validate_token_locally(token)
    assert result["sub"] == "user-123"


def test_validate_token_locally_malformed():
    result = validate_token_locally("not-a-jwt")
    assert result == {}


def test_is_token_expired_false():
    payload = {"exp": int(time.time()) + 3600}
    token = _make_jwt(payload)
    assert is_token_expired(token) is False


def test_is_token_expired_true():
    payload = {"exp": int(time.time()) - 3600}
    token = _make_jwt(payload)
    assert is_token_expired(token) is True


def test_is_token_expired_no_exp():
    payload = {"sub": "user"}
    token = _make_jwt(payload)
    assert is_token_expired(token) is False
