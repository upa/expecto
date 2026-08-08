"""A minimal MCP server over stdio, launched as a subprocess by
tests/test_mcp_client.py to exercise the real stdio transport."""

from mcp.server.mcpserver import MCPServer

server: MCPServer = MCPServer("expecto-test-server")


@server.tool()
def echo(text: str) -> str:
    """Echo the given text back."""
    return text


@server.tool()
def boom() -> str:
    """Always fail."""
    raise RuntimeError("boom")


if __name__ == "__main__":
    server.run()
