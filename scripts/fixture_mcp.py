"""Disposable MCP acceptance fixture; never reads workspace files or credentials."""
from mcp.server.fastmcp import FastMCP

server = FastMCP('JobScope acceptance tools')


@server.tool()
def lookup_topic(query: str) -> str:
    """Return a deterministic fixture response for connection and tool-loop tests."""
    return 'MCP fixture result: ' + query


if __name__ == '__main__':
    server.run(transport='stdio')
