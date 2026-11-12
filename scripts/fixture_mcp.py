"""Disposable MCP acceptance fixture; never reads workspace files or credentials."""
import os

from mcp.server.fastmcp import FastMCP

server = FastMCP('JobScope acceptance tools')


@server.tool()
def lookup_topic(query: str) -> str:
    """Return a deterministic fixture response for connection and tool-loop tests."""
    return 'MCP fixture result: ' + query


@server.tool()
def proc_pid() -> str:
    """Return the server process pid; session-reuse tests assert it stays constant."""
    return str(os.getpid())


if __name__ == '__main__':
    server.run(transport='stdio')
