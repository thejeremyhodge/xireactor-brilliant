"""xiReactor Cortex MCP Server — exposes KB tools via stdio for Claude Desktop."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from client import CortexClient
from tools import register_tools

mcp = FastMCP(name="cortex")
api = CortexClient()
register_tools(mcp, api)


if __name__ == "__main__":
    mcp.run(transport="stdio")
