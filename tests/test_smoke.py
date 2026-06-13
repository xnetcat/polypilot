from polymarket_mcp import server


def test_ping_returns_ok():
    assert server.ping() == {"status": "ok", "service": "polypilot-mcp"}


def test_mcp_instance_exists():
    # The FastMCP app must be importable for the runner to launch it.
    assert server.mcp is not None
    assert server.mcp.name == "polypilot"
