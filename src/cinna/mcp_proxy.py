"""MCP stdio server that proxies knowledge queries to the platform backend.

Launched by Claude Code (or other MCP clients) via .mcp.json config.
Runs as a subprocess — reads from stdin, writes to stdout (MCP stdio transport).
"""

import logging
import os
import asyncio
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from cinna.config import load_config, CinnaConfig
from cinna.client import PlatformClient

logger = logging.getLogger("cinna.mcp_proxy")


def create_mcp_server(config: CinnaConfig) -> Server:
    """Create the MCP server with knowledge query tool."""

    server = Server("agent-knowledge")
    client = PlatformClient(config)
    logger.info("MCP server created for agent %s (%s)", config.agent_name, config.agent_id)

    @server.list_tools()
    async def list_tools():
        return [
            Tool(
                name="knowledge_query",
                description="Search the agent's knowledge base for relevant documentation and articles",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Natural language search query",
                        },
                        "topic": {
                            "type": "string",
                            "description": f"Knowledge topic to search in. Available: {_topic_list(config)}",
                        },
                    },
                    "required": ["query"],
                },
            )
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        if name != "knowledge_query":
            logger.warning("Unknown tool called: %s", name)
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

        query = arguments.get("query", "")
        topic = arguments.get("topic")
        logger.info("knowledge_query: query=%r topic=%r", query, topic)

        try:
            response = client.search_knowledge(config.agent_id, query, topic)
        except Exception:
            logger.exception("knowledge_query failed for query=%r topic=%r", query, topic)
            raise
        results = response.get("results", [])
        logger.info("knowledge_query returned %d results", len(results))

        if not results:
            return [TextContent(type="text", text="No results found.")]

        formatted = _format_results(results)
        return [TextContent(type="text", text=formatted)]

    return server


def create_account_mcp_server(account_config) -> Server:
    """Create an MCP server backed by the account-level knowledge search.

    The account analogue of ``create_mcp_server``: instead of a single agent's
    knowledge base it searches the account user's accessible platform knowledge
    sources (public + own private) via ``POST /account/knowledge/search``. Used
    by the account workspace so the local orchestrator agent gets the same
    ``knowledge_query`` tool while building, without a per-agent token.
    """
    from cinna.client import AccountClient

    server = Server("platform-knowledge")
    logger.info(
        "Account MCP server created for %s (%s)",
        account_config.machine_name,
        account_config.platform_url,
    )

    @server.list_tools()
    async def list_tools():
        return [
            Tool(
                name="knowledge_query",
                description=(
                    "Search the Cinna platform knowledge base for documentation "
                    "and articles to help build agents"
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Natural language search query",
                        },
                        "topic": {
                            "type": "string",
                            "description": "Optional knowledge topic to narrow the search",
                        },
                    },
                    "required": ["query"],
                },
            )
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        if name != "knowledge_query":
            logger.warning("Unknown tool called: %s", name)
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

        query = arguments.get("query", "")
        topic = arguments.get("topic")
        logger.info("account knowledge_query: query=%r topic=%r", query, topic)

        try:
            with AccountClient(account_config) as client:
                response = client.search_knowledge(query, topic)
        except Exception:
            logger.exception(
                "account knowledge_query failed for query=%r topic=%r", query, topic
            )
            raise
        results = response.get("results", [])
        logger.info("account knowledge_query returned %d results", len(results))

        if not results:
            return [TextContent(type="text", text="No results found.")]

        formatted = _format_results(results)
        return [TextContent(type="text", text=formatted)]

    return server


def _topic_list(config: CinnaConfig) -> str:
    topics = [t for ks in config.knowledge_sources for t in ks.topics]
    return ", ".join(topics) if topics else "all topics"


def _format_results(results: list[dict]) -> str:
    parts = []
    for r in results:
        source = r.get("source", "unknown")
        similarity = r.get("similarity", 0)
        content = r.get("content", "")
        parts.append(f"## [{source}] (relevance: {similarity:.0%})\n\n{content}")
    return "\n\n---\n\n".join(parts)


def _setup_mcp_logging(workspace_root: Path) -> None:
    """Set up file logging for the MCP proxy subprocess.

    The proxy is launched directly by the MCP client (not via the Click CLI
    group), so the normal setup_logging() path is never hit.  We configure
    logging to the same cinna.log used by the rest of the CLI.
    """
    from cinna.logging import LOG_FILE
    import logging.handlers

    log_path = workspace_root / LOG_FILE
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=3,
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    root = logging.getLogger("cinna")
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)


def _resolve_proxy_context() -> tuple[str | None, Path | None]:
    """Resolve ``(mode, workspace_root)`` for the proxy, tolerant of a moved folder.

    ``mode`` is ``"account"`` or ``"agent"``; ``workspace_root`` is the directory
    holding ``.cinna/``. Returns ``(None, None)`` when nothing can be located.

    The ``CINNA_ACCOUNT_CONFIG`` / ``CINNA_CONFIG`` env vars (set by the generated
    ``.mcp.json`` / ``opencode.json``) primarily **select the mode**; their value
    is only a hint for locating the config. We resolve the workspace root in order:

    1. The env value taken literally — an absolute path (legacy configs) or a
       path relative to the launch cwd (the portable form newer configs write).
    2. A walk up from the launch cwd for the mode's config file. MCP clients
       launch the proxy with cwd set to the workspace folder, so this heals
       stale absolute paths after the folder is moved — no regeneration needed.

    When neither env var is set, we auto-detect the nearest ``.cinna/`` from cwd
    (an agent's ``config.json`` nested under an account wins over the account's
    ``account.json`` because it is found first).
    """
    from cinna.account import ACCOUNT_CONFIG_FILE, find_account_root
    from cinna.config import CONFIG_DIR, CONFIG_FILE, find_workspace_root
    from cinna.errors import AccountConfigNotFoundError, ConfigNotFoundError

    cwd = Path.cwd()
    account_env = os.environ.get("CINNA_ACCOUNT_CONFIG")
    config_env = os.environ.get("CINNA_CONFIG")

    def _root_from_env(env_value: str | None, filename: str) -> Path | None:
        """Workspace root if env_value points at an existing .cinna/<filename>."""
        if not env_value:
            return None
        candidates = [Path(env_value)]
        if not Path(env_value).is_absolute():
            candidates.append(cwd / env_value)
        for cand in candidates:
            if cand.is_file():
                return cand.resolve().parent.parent
        return None

    if account_env is not None:
        root = _root_from_env(account_env, ACCOUNT_CONFIG_FILE)
        if root is None:
            try:
                root = find_account_root(cwd)
            except AccountConfigNotFoundError:
                root = None
        if root is not None:
            return "account", root

    if config_env is not None:
        root = _root_from_env(config_env, CONFIG_FILE)
        if root is None:
            try:
                root = find_workspace_root(cwd)
            except ConfigNotFoundError:
                root = None
        if root is not None:
            return "agent", root

    # No usable env hint — auto-detect the nearest .cinna/ walking up from cwd.
    if account_env is None and config_env is None:
        current = cwd.resolve()
        while True:
            cinna_dir = current / CONFIG_DIR
            if (cinna_dir / ACCOUNT_CONFIG_FILE).is_file():
                return "account", current
            if (cinna_dir / CONFIG_FILE).is_file():
                return "agent", current
            parent = current.parent
            if parent == current:
                break
            current = parent

    return None, None


def run_mcp_proxy():
    """Entry point for `cinna mcp-proxy` — run as MCP stdio server.

    Two modes, selected by environment variable (see ``_resolve_proxy_context``
    for how the workspace root is located in a move-tolerant way):

    * ``CINNA_ACCOUNT_CONFIG`` set → **account mode**: search the account user's
      platform knowledge sources (used by the account workspace's `.mcp.json`).
    * ``CINNA_CONFIG`` set → **per-agent mode**: search a single agent's
      knowledge base (used by a per-agent workspace's `.mcp.json`).
    """
    mode, workspace_root = _resolve_proxy_context()
    if mode is None:
        raise SystemExit(
            "Could not locate a cinna workspace. Set CINNA_CONFIG or "
            "CINNA_ACCOUNT_CONFIG, or run `cinna mcp-proxy` from inside a "
            "workspace folder (one containing .cinna/)."
        )

    if mode == "account":
        _setup_mcp_logging(workspace_root)
        logger.info("MCP proxy starting in account mode (workspace=%s)", workspace_root)

        from cinna.account import load_account_config

        try:
            account_config = load_account_config(workspace_root)
        except Exception:
            logger.exception("Failed to load account config from %s", workspace_root)
            raise

        try:
            server = create_account_mcp_server(account_config)
        except Exception:
            logger.exception("Failed to create account MCP server")
            raise
    else:
        _setup_mcp_logging(workspace_root)
        logger.info("MCP proxy starting (workspace=%s)", workspace_root)

        try:
            config = load_config(workspace_root)
        except Exception:
            logger.exception("Failed to load config from %s", workspace_root)
            raise

        try:
            server = create_mcp_server(config)
        except Exception:
            logger.exception("Failed to create MCP server")
            raise

    async def main():
        async with stdio_server() as (read_stream, write_stream):
            logger.info("MCP stdio transport connected, serving requests")
            init_options = server.create_initialization_options()
            await server.run(read_stream, write_stream, init_options)

    try:
        asyncio.run(main())
    except Exception:
        logger.exception("MCP proxy crashed")
        raise
    finally:
        logger.info("MCP proxy shut down")
