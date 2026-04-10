"""HTTP client for platform API. All backend communication goes through here."""

import logging

import httpx

from cinna.config import CinnaConfig
from cinna.auth import get_auth_headers
from cinna.errors import AuthenticationError, PlatformError

logger = logging.getLogger("cinna.client")

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
DOWNLOAD_TIMEOUT = httpx.Timeout(300.0, connect=10.0)


class PlatformClient:
    """HTTP client wrapping httpx with CLI token authentication."""

    def __init__(self, config: CinnaConfig):
        self.config = config
        self.base_url = config.platform_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            headers=get_auth_headers(config),
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
        )

    def _handle_response(self, response: httpx.Response) -> httpx.Response:
        """Check response status. Raise typed exceptions for known error codes."""
        logger.debug(
            "%s %s -> %s (%d bytes)",
            response.request.method,
            response.request.url,
            response.status_code,
            len(response.content),
        )
        if response.status_code == 401:
            detail = ""
            try:
                detail = response.json().get("detail", "")
            except Exception:
                pass
            logger.error("Authentication failed: %s", detail)
            raise AuthenticationError(detail)
        if response.status_code == 404:
            logger.error("Resource not found: %s", response.request.url)
            raise PlatformError(404, "Agent not found. It may have been deleted.")
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            logger.error(
                "Platform error %s: %s (url: %s, body: %.500s)",
                response.status_code,
                detail,
                response.request.url,
                response.text,
            )
            raise PlatformError(response.status_code, detail)
        return response

    # --- Setup (no auth) ---

    def exchange_setup_token(
        self, token: str, machine_name: str, machine_info: str
    ) -> dict:
        """POST /cli-setup/{token} — exchange setup token for bootstrap payload."""
        response = httpx.post(
            f"{self.base_url}/cli-setup/{token}",
            json={"machine_name": machine_name, "machine_info": machine_info},
            timeout=DEFAULT_TIMEOUT,
        )
        return self._handle_response(response).json()

    # --- Build Context ---

    def download_build_context(self, agent_id: str) -> bytes:
        """GET /api/v1/cli/agents/{id}/build-context — download Docker build tarball."""
        response = self._client.get(
            f"/api/v1/cli/agents/{agent_id}/build-context",
            timeout=DOWNLOAD_TIMEOUT,
        )
        return self._handle_response(response).content

    # --- Workspace ---

    def download_workspace(self, agent_id: str) -> bytes:
        """GET /api/v1/cli/agents/{id}/workspace — download workspace tarball."""
        response = self._client.get(
            f"/api/v1/cli/agents/{agent_id}/workspace",
            timeout=DOWNLOAD_TIMEOUT,
        )
        return self._handle_response(response).content

    def upload_workspace(self, agent_id: str, tarball: bytes) -> None:
        """POST /api/v1/cli/agents/{id}/workspace — upload workspace tarball."""
        response = self._client.post(
            f"/api/v1/cli/agents/{agent_id}/workspace",
            files={"file": ("workspace.tar.gz", tarball, "application/gzip")},
            timeout=DOWNLOAD_TIMEOUT,
        )
        self._handle_response(response)

    def get_workspace_manifest(self, agent_id: str) -> dict:
        """GET /api/v1/cli/agents/{id}/workspace/manifest — remote file manifest."""
        response = self._client.get(
            f"/api/v1/cli/agents/{agent_id}/workspace/manifest",
        )
        return self._handle_response(response).json()

    # --- Credentials ---

    def get_credentials(self, agent_id: str) -> dict:
        """GET /api/v1/cli/agents/{id}/credentials — pull credentials."""
        response = self._client.get(
            f"/api/v1/cli/agents/{agent_id}/credentials",
        )
        return self._handle_response(response).json()

    # --- Building Context ---

    def get_building_context(self, agent_id: str) -> dict:
        """GET /api/v1/cli/agents/{id}/building-context — assembled prompt + settings."""
        response = self._client.get(
            f"/api/v1/cli/agents/{agent_id}/building-context",
            timeout=DOWNLOAD_TIMEOUT,
        )
        return self._handle_response(response).json()

    # --- Knowledge ---

    def search_knowledge(
        self, agent_id: str, query: str, topic: str | None = None
    ) -> dict:
        """POST /api/v1/cli/agents/{id}/knowledge/search — search knowledge base."""
        payload: dict = {"query": query}
        if topic:
            payload["topic"] = topic
        response = self._client.post(
            f"/api/v1/cli/agents/{agent_id}/knowledge/search",
            json=payload,
        )
        return self._handle_response(response).json()

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
