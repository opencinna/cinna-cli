"""CLI token management — storage, header injection, validation."""

import json
import base64
import time

from cinna.config import CinnaConfig


def get_auth_headers(config: CinnaConfig) -> dict[str, str]:
    """Return Authorization header dict for API calls."""
    return {"Authorization": f"Bearer {config.cli_token}"}


def validate_token_locally(token: str) -> dict:
    """Decode JWT without verification to check expiry.

    Returns payload dict. Used for local "is this token probably expired?"
    check before making API calls. The real validation happens server-side.

    NOTE: This is NOT security validation — the backend validates the token.
    This is a UX convenience to show a clear message instead of a 401.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        # Add padding
        payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload
    except Exception:
        return {}


def is_token_expired(token: str) -> bool:
    """Check if the JWT token is probably expired (local check only)."""
    payload = validate_token_locally(token)
    exp = payload.get("exp")
    if exp is None:
        return False
    return time.time() > exp
