"""HTTP client for platform API. All backend communication goes through here."""

import json
import logging
from typing import Iterator

import httpx

from cinna.config import CinnaConfig
from cinna.auth import get_auth_headers
from cinna.errors import AuthenticationError, PlatformError

logger = logging.getLogger("cinna.client")

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
DOWNLOAD_TIMEOUT = httpx.Timeout(300.0, connect=10.0)
# Exec streams can be long-running — disable read timeout so idle output doesn't abort.
EXEC_STREAM_TIMEOUT = httpx.Timeout(None, connect=10.0)


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
        """POST /api/cli-setup/{token} — exchange setup token for bootstrap payload."""
        response = httpx.post(
            f"{self.base_url}/api/cli-setup/{token}",
            json={"machine_name": machine_name, "machine_info": machine_info},
            timeout=DEFAULT_TIMEOUT,
        )
        return self._handle_response(response).json()

    # --- Workspace (initial clone only; Mutagen owns it afterwards) ---

    def download_workspace(self, agent_id: str) -> bytes:
        """GET /api/v1/cli/agents/{id}/workspace — one-shot tarball for initial clone."""
        response = self._client.get(
            f"/api/v1/cli/agents/{agent_id}/workspace",
            timeout=DOWNLOAD_TIMEOUT,
        )
        return self._handle_response(response).content

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

    # --- Live Sync Runtime ---

    def get_sync_runtime(self, agent_id: str) -> dict:
        """GET /api/v1/cli/agents/{id}/sync-runtime — required Mutagen version + hash."""
        response = self._client.get(
            f"/api/v1/cli/agents/{agent_id}/sync-runtime",
        )
        return self._handle_response(response).json()

    # --- Remote exec (SSE stream) ---

    def stream_exec(
        self, agent_id: str, command: str, timeout: int | None = None
    ) -> Iterator[dict]:
        """POST /api/v1/cli/agents/{id}/exec — stream command output events.

        Yields parsed event dicts. Known shapes:
          {"type": "exec_id", "exec_id": "<uuid>"}
          {"type": "tool_result_delta", "content": "...", "metadata": {...}}
          {"type": "done", "exit_code": N, "duration_seconds": F}
          {"type": "interrupted", "exit_code": -1}
          {"type": "error", "content": "..."}

        The caller is responsible for interpreting `done`/`interrupted` and
        mapping to a process exit code.

        ``timeout`` (seconds) bounds the remote command's wall-clock run
        time on the platform side. When omitted, the platform applies its
        default.
        """
        url = f"/api/v1/cli/agents/{agent_id}/exec"
        payload: dict = {"command": command}
        if timeout is not None:
            payload["timeout"] = timeout
        logger.info(
            "stream_exec open: agent=%s timeout=%s cmd=%.200s",
            agent_id,
            timeout,
            command,
        )
        with self._client.stream(
            "POST", url, json=payload, timeout=EXEC_STREAM_TIMEOUT
        ) as response:
            if response.status_code >= 400:
                # Read the body so _handle_response can surface the error.
                response.read()
                self._handle_response(response)
                return

            logger.debug(
                "stream_exec connected: agent=%s status=%s", agent_id, response.status_code
            )
            event_count = 0
            for line in response.iter_lines():
                if not line:
                    continue
                if line.startswith("data: "):
                    data_str = line[6:]
                    try:
                        event_count += 1
                        yield json.loads(data_str)
                    except json.JSONDecodeError:
                        logger.warning("Could not parse SSE event: %s", data_str[:200])
            logger.debug(
                "stream_exec closed: agent=%s events=%d", agent_id, event_count
            )

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class AccountClient:
    """HTTP client for the account-scoped CLI routes (`/api/v1/cli/account/*`).

    Authenticates with the account CLI token from ``.cinna/account.json``.
    The account token only works on the account route group — per-agent
    sync/exec calls keep going through ``PlatformClient`` with the per-agent
    child token.
    """

    def __init__(self, account_config):
        self.config = account_config
        self.base_url = account_config.platform_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {account_config.account_token}"},
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
        )

    def _handle_response(self, response: httpx.Response) -> httpx.Response:
        """Check response status, surfacing backend error details verbatim."""
        logger.debug(
            "%s %s -> %s (%d bytes)",
            response.request.method,
            response.request.url,
            response.status_code,
            len(response.content),
        )
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            if response.status_code == 401:
                logger.error("Account token rejected: %s", detail)
                raise AuthenticationError(detail)
            logger.error(
                "Platform error %s: %s (url: %s)",
                response.status_code,
                detail,
                response.request.url,
            )
            raise PlatformError(response.status_code, detail)
        return response

    # --- Account-scoped routes (account token auth) ---

    def list_account_agents(self) -> dict:
        """GET /api/v1/cli/account/agents — accessible agents with can_build flags."""
        response = self._client.get("/api/v1/cli/account/agents")
        return self._handle_response(response).json()

    def mint_agent_token(
        self, agent_id: str, machine_name: str, machine_info: str | None
    ) -> dict:
        """POST /api/v1/cli/account/agents/{id}/mint — mint a per-agent child token."""
        response = self._client.post(
            f"/api/v1/cli/account/agents/{agent_id}/mint",
            json={"machine_name": machine_name, "machine_info": machine_info},
        )
        return self._handle_response(response).json()

    def create_agent(
        self,
        name: str,
        description: str | None = None,
        user_workspace_id: str | None = None,
    ) -> dict:
        """POST /api/v1/cli/account/agents — create an agent (thin client).

        Sends only user-specified fields; the backend applies all defaults
        (AI credentials, env template, environment creation) and returns the
        full agent record. ``user_workspace_id`` targets the account's active
        user workspace (``None`` = Default).
        """
        body: dict = {"name": name}
        if description is not None:
            body["description"] = description
        if user_workspace_id is not None:
            body["user_workspace_id"] = user_workspace_id
        response = self._client.post("/api/v1/cli/account/agents", json=body)
        return self._handle_response(response).json()

    # --- User workspaces ---

    def list_user_workspaces(self) -> dict:
        """GET /api/v1/cli/account/user-workspaces — the user's own workspaces.

        Catalogue for ``cinna account user-workspace list`` / activation. The
        *active* workspace is a client-side setting in ``.cinna/account.json``;
        the backend keeps no active-workspace state.
        """
        response = self._client.get("/api/v1/cli/account/user-workspaces")
        return self._handle_response(response).json()

    # --- Credentials (drafts only — never secret values) ---

    def list_credential_types(self) -> dict:
        """GET /api/v1/cli/account/credentials/types — type + required-field map."""
        response = self._client.get("/api/v1/cli/account/credentials/types")
        return self._handle_response(response).json()

    def list_credentials(self, user_workspace_id: str | None = None) -> dict:
        """GET /api/v1/cli/account/credentials — metadata-only credential listing.

        ``user_workspace_id``: ``None`` = all, ``""`` = Default workspace, a UUID
        string = that workspace.
        """
        params = (
            None
            if user_workspace_id is None
            else {"user_workspace_id": user_workspace_id}
        )
        response = self._client.get(
            "/api/v1/cli/account/credentials", params=params
        )
        return self._handle_response(response).json()

    def create_credential(
        self,
        name: str,
        cred_type: str,
        notes: str | None = None,
        service_uri: str | None = None,
        allow_sharing: bool = False,
        user_workspace_id: str | None = None,
    ) -> dict:
        """POST /api/v1/cli/account/credentials — create a draft credential.

        No secret value is ever sent — the credential is created empty and the
        user fills it in the UI. Returns ``{credential, required_fields,
        setup_url}``.
        """
        body: dict = {"name": name, "type": cred_type, "allow_sharing": allow_sharing}
        if notes is not None:
            body["notes"] = notes
        if service_uri is not None:
            body["service_uri"] = service_uri
        if user_workspace_id is not None:
            body["user_workspace_id"] = user_workspace_id
        response = self._client.post("/api/v1/cli/account/credentials", json=body)
        return self._handle_response(response).json()

    def update_credential(self, credential_id: str, fields: dict) -> dict:
        """PUT /api/v1/cli/account/credentials/{id} — metadata-only update.

        ``fields`` may contain name / notes / service_uri / allow_sharing /
        allow_template_sharing. Never a secret value.
        """
        response = self._client.put(
            f"/api/v1/cli/account/credentials/{credential_id}", json=fields
        )
        return self._handle_response(response).json()

    def delete_credential(self, credential_id: str, force: bool = False) -> dict:
        """DELETE /api/v1/cli/account/credentials/{id} — tier-gated delete."""
        params = {"force": True} if force else None
        response = self._client.delete(
            f"/api/v1/cli/account/credentials/{credential_id}", params=params
        )
        return self._handle_response(response).json()

    def share_credential_with_agent(
        self, credential_id: str, agent_id: str
    ) -> dict:
        """POST /api/v1/cli/account/credentials/{id}/share-with-agent — attach."""
        response = self._client.post(
            f"/api/v1/cli/account/credentials/{credential_id}/share-with-agent",
            json={"agent_id": agent_id},
        )
        return self._handle_response(response).json()

    def connect_agent_api(
        self,
        producer_agent_id: str,
        consumer_agent_id: str,
        credential_label: str | None = None,
        read_only_override: bool = False,
    ) -> dict:
        """POST /api/v1/cli/account/connect/agent-api — one-click REST wire."""
        body: dict = {
            "producer_agent_id": producer_agent_id,
            "consumer_agent_id": consumer_agent_id,
        }
        if credential_label is not None:
            body["credential_label"] = credential_label
        if read_only_override:
            body["read_only_override"] = True
        response = self._client.post(
            "/api/v1/cli/account/connect/agent-api", json=body
        )
        return self._handle_response(response).json()

    def list_discoverable_mcp(self, consumer_agent_id: str | None = None) -> dict:
        """GET /api/v1/cli/account/connect/mcp/discoverable — a2a connector picker."""
        params = (
            {"consumer_agent_id": consumer_agent_id} if consumer_agent_id else None
        )
        response = self._client.get(
            "/api/v1/cli/account/connect/mcp/discoverable", params=params
        )
        return self._handle_response(response).json()

    def connect_mcp(
        self,
        connector_id: str,
        consumer_agent_id: str,
        mcp_mode_conversation: bool = True,
        mcp_mode_building: bool = True,
        label: str | None = None,
    ) -> dict:
        """POST /api/v1/cli/account/connect/mcp — wire an agent2agent MCP connector."""
        body: dict = {
            "connector_id": connector_id,
            "consumer_agent_id": consumer_agent_id,
        }
        if not mcp_mode_conversation:
            body["mcp_mode_conversation"] = False
        if not mcp_mode_building:
            body["mcp_mode_building"] = False
        if label is not None:
            body["label"] = label
        response = self._client.post("/api/v1/cli/account/connect/mcp", json=body)
        return self._handle_response(response).json()

    # --- Agent REST API producer management ---

    def set_agent_api_enabled(self, agent_id: str, enabled: bool = True) -> dict:
        """POST /api/v1/cli/account/agent-api/enable — toggle the producer API.

        Returns the resulting agent-api status (``agent_api_enabled``, ``state``,
        ``spec_available``, ``last_error``, ...), so the caller can verify the
        toggle took effect in one round-trip.
        """
        response = self._client.post(
            "/api/v1/cli/account/agent-api/enable",
            json={"agent_id": agent_id, "enabled": enabled},
        )
        return self._handle_response(response).json()

    def refresh_agent_api(self, agent_id: str) -> dict:
        """POST /api/v1/cli/account/agent-api/refresh — force a spec re-harvest.

        Re-imports the producer's ``agent_api/`` modules and re-parses
        ``policy.yaml``, then returns the status (``last_error`` reflects a
        harvest failure — the call never raises on one).
        """
        response = self._client.post(
            "/api/v1/cli/account/agent-api/refresh",
            json={"agent_id": agent_id},
        )
        return self._handle_response(response).json()

    def get_agent_api_spec(self, agent_id: str) -> dict:
        """GET /api/v1/cli/account/agent-api/spec — the harvested OpenAPI spec."""
        response = self._client.get(
            "/api/v1/cli/account/agent-api/spec",
            params={"agent_id": agent_id},
        )
        return self._handle_response(response).json()

    def call_agent_api(
        self,
        agent_id: str,
        method: str,
        path: str,
        query: dict | None = None,
        json_body=None,
    ) -> dict:
        """POST /api/v1/cli/account/agent-api/call — owner-side endpoint smoke test.

        Invokes one endpoint on the producer's own REST API (query params ARE
        forwarded) and returns the buffered response
        ``{status_code, headers, body, is_json}``.
        """
        body: dict = {"agent_id": agent_id, "method": method, "path": path}
        if query:
            body["query"] = query
        if json_body is not None:
            body["json_body"] = json_body
        response = self._client.post(
            "/api/v1/cli/account/agent-api/call", json=body
        )
        return self._handle_response(response).json()

    def restart_agent_env(self, agent_id: str) -> dict:
        """POST /api/v1/cli/account/agents/{id}/restart-env — restart the env.

        Blocks until the container is back; returns
        ``{environment_id, status, status_message}``.
        """
        response = self._client.post(
            f"/api/v1/cli/account/agents/{agent_id}/restart-env"
        )
        return self._handle_response(response).json()

    def inspect_agent(self, agent_id: str) -> dict:
        """GET /api/v1/cli/account/agents/{id}/inspect — effective config.

        Returns the agent's prompts, enabled features, connected credential
        metadata (name + type only), and live agent-api status when enabled.
        """
        response = self._client.get(
            f"/api/v1/cli/account/agents/{agent_id}/inspect"
        )
        return self._handle_response(response).json()

    def api_proxy(
        self,
        method: str,
        path: str,
        query: dict | None = None,
        json_body=None,
    ) -> httpx.Response:
        """POST /api/v1/cli/account/api-proxy — generic escape hatch.

        Returns the raw response: the backend mirrors the inner route's status
        and body 1:1, so non-2xx is normal output here, not an exception. Only
        a 401 (invalid account token) raises.
        """
        body: dict = {"method": method, "path": path}
        if query:
            body["query"] = query
        if json_body is not None:
            body["json_body"] = json_body
        response = self._client.post("/api/v1/cli/account/api-proxy", json=body)
        if response.status_code == 401:
            try:
                detail = response.json().get("detail", "")
            except Exception:
                detail = response.text
            logger.error("Account token rejected: %s", detail)
            raise AuthenticationError(detail)
        logger.debug(
            "api-proxy %s %s -> %s (%d bytes)",
            method,
            path,
            response.status_code,
            len(response.content),
        )
        return response

    def download_context_package(self) -> bytes:
        """GET /api/v1/cli/account/context-package — orchestrator context tarball.

        Returns the gzip tarball bytes (all members under a top-level
        ``context/`` prefix). Extracted into the account workspace root by
        `cinna account setup` / `cinna account refresh-context`.
        """
        response = self._client.get(
            "/api/v1/cli/account/context-package",
            timeout=DOWNLOAD_TIMEOUT,
        )
        return self._handle_response(response).content

    def revoke_child_token(self, token_id: str) -> dict:
        """DELETE /api/v1/cli/account/tokens/children/{token_id}.

        Revoke a child token this account token minted (`cinna agent unsync`).
        Idempotent server-side; 404 if the id is not a cli-type child minted
        by this account token (provenance-scoped, no existence leak).
        """
        response = self._client.delete(
            f"/api/v1/cli/account/tokens/children/{token_id}"
        )
        return self._handle_response(response).json()

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
