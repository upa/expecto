"""A minimal MCP server over stdio for the end-to-end tests, declared
in the test config.toml as [mcp.e2e] and launched by expecto itself."""

from mcp.server.mcpserver import MCPServer

server: MCPServer = MCPServer("e2e")


@server.tool()
def get_marker() -> str:
    """Return the e2e marker string."""
    return "MCP_MARKER_31337"


if __name__ == "__main__":
    server.run()
