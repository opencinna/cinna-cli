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


def run_mcp_proxy():
    """Entry point for `cinna mcp-proxy` — run as MCP stdio server."""
    config_path = os.environ.get("CINNA_CONFIG")
    if not config_path:
        raise SystemExit("CINNA_CONFIG environment variable not set")

    workspace_root = Path(config_path).parent.parent
    _setup_mcp_logging(workspace_root)

    logger.info("MCP proxy starting (config=%s)", config_path)

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
