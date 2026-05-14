"""xiReactor Brilliant MCP Server — exposes KB tools via stdio for Claude Desktop."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from client import BrilliantClient
from tools import register_tools

mcp = FastMCP(name="brilliant")
api = BrilliantClient()
register_tools(mcp, api)

# Downstream plugin discovery (see docs/downstream-overlay.md). Loads any
# module in mcp/plugins/ or in XIREACTOR_MCP_PLUGIN_DIR that exposes
# register(mcp, api). Fail-soft: errors are logged, never fatal.
try:
    from plugins import load_plugins as _load_mcp_plugins

    _load_mcp_plugins(mcp, api)
except Exception:  # pragma: no cover - defensive
    import logging

    logging.getLogger("brilliant.mcp.plugins").exception("Plugin loader failed")


if __name__ == "__main__":
    mcp.run(transport="stdio")
