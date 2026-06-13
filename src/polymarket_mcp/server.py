"""Polypilot MCP server. Tool groups are registered here."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("polypilot")


@mcp.tool()
def ping() -> dict:
    """Health check. Returns service status."""
    return {"status": "ok", "service": "polypilot-mcp"}


def main() -> None:
    """Entry point for the `polypilot-mcp` script and `python -m polymarket_mcp`."""
    mcp.run()


if __name__ == "__main__":
    main()
