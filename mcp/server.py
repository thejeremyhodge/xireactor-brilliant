"""xiReactor Brilliant MCP Server — exposes KB tools via stdio for Claude Desktop."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from client import BrilliantClient
from tools import register_tools

mcp = FastMCP(name="brilliant")
api = BrilliantClient()
register_tools(mcp, api)


if __name__ == "__main__":
    mcp.run(transport="stdio")
