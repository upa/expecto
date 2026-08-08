from __future__ import annotations

import asyncio
import contextlib
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.client.streamable_http import TransportStreams, streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

if TYPE_CHECKING:
    from typing import TextIO

    from openai.types.chat import ChatCompletionFunctionToolParam

    from expecto.config import MCPServerConfig

TOOL_NAME_PREFIX = "mcp"


def qualified_tool_name(server: str, tool: str) -> str:
    """The name an MCP tool is exposed to the LLM under.

    Namespaced by server so two servers offering the same tool name can
    coexist, and so a call's origin is readable in the log."""
    return f"{TOOL_NAME_PREFIX}__{server}__{tool}"


@dataclass(frozen=True)
class MCPTool:
    """One tool discovered on a connected MCP server."""

    server: str
    tool: str
    qualified_name: str
    description: str
    input_schema: dict[str, object]


class MCPManager:
    """Client side of the configured MCP servers (design/MCP.md).

    Connects at session start, keeps every session open for the whole
    expecto session, and closes them all at the end. A server that fails
    to connect is reported and dropped: an unreachable side service must
    never stop the user from getting a shell."""

    def __init__(
        self, errlog: TextIO | None = None, connect_timeout_ms: int = 10000,
    ) -> None:
        # A stdio server's stderr goes to expecto's own stderr, i.e. the
        # user's terminal, so its diagnostics are visible rather than lost.
        self._errlog = errlog if errlog is not None else sys.stderr
        self._connect_timeout_ms = connect_timeout_ms
        self._stack = contextlib.AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}
        self._tools: list[MCPTool] = []

    @property
    def tools(self) -> list[MCPTool]:
        return self._tools

    async def connect(self, servers: list[MCPServerConfig]) -> list[str]:
        """Connect to every configured server and collect their tools.

        Returns a warning message per server that could not be used; those
        servers are simply left out."""
        warnings: list[str] = []
        for server in servers:
            try:
                async with asyncio.timeout(self._connect_timeout_ms / 1000):
                    await self._connect_one(server)
            except Exception as exc:  # noqa: BLE001 - any failure drops one server
                warnings.append(
                    f"mcp.{server.name}: {_describe_error(exc)}; server not available",
                )
        return warnings

    async def _connect_one(self, server: MCPServerConfig) -> None:
        read, write = await self._open_transport(server)
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        result = await session.list_tools()
        self._sessions[server.name] = session
        for tool in result.tools:
            self._tools.append(
                MCPTool(
                    server=server.name,
                    tool=tool.name,
                    qualified_name=qualified_tool_name(server.name, tool.name),
                    description=tool.description or "",
                    input_schema=dict(tool.input_schema),
                ),
            )

    async def _open_transport(self, server: MCPServerConfig) -> TransportStreams:
        if server.type == "stdio":
            # The child inherits the SDK's minimal environment (PATH, HOME,
            # ...) plus whatever the user declared — not expecto's own
            # environment, which holds OPENAI_API_KEY.
            params = StdioServerParameters(
                command=server.command,
                args=server.args,
                env={**get_default_environment(), **server.env},
            )
            return await self._stack.enter_async_context(
                stdio_client(params, errlog=self._errlog),
            )

        headers = (
            {"Authorization": f"Bearer {server.token}"} if server.token else None
        )
        if server.type == "sse":
            return await self._stack.enter_async_context(
                sse_client(server.url, headers=headers),
            )

        # streamable-http takes headers via a pre-configured HTTP client,
        # whose lifecycle is then ours to manage.
        http_client = await self._stack.enter_async_context(
            create_mcp_http_client(headers=headers),
        )
        return await self._stack.enter_async_context(
            streamable_http_client(server.url, http_client=http_client),
        )

    def tool_defs(self) -> list[ChatCompletionFunctionToolParam]:
        """The discovered tools as Chat Completions function tools.

        The server's own input schema is passed through without `strict`:
        arbitrary MCP schemas do not meet strict mode's requirements, and
        the server validates its arguments anyway (design/MCP.md §4)."""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.qualified_name,
                    "description": (
                        f"[MCP server {tool.server}] {tool.description}"
                        if tool.description
                        else f"Tool {tool.tool} on MCP server {tool.server}"
                    ),
                    "parameters": tool.input_schema,
                },
            }
            for tool in self._tools
        ]

    def find(self, qualified_name: str) -> MCPTool | None:
        for tool in self._tools:
            if tool.qualified_name == qualified_name:
                return tool
        return None

    async def call(self, qualified_name: str, arguments: dict[str, object]) -> str:
        """Call one tool and render its result as tool-output text.

        A tool that reports failure comes back as text starting with
        "Error:" rather than an exception: like a declined command, a
        failure is information for the LLM, not an abort."""
        tool = self.find(qualified_name)
        if tool is None:
            return f"Error: unknown MCP tool: {qualified_name}"
        session = self._sessions.get(tool.server)
        if session is None:
            return f"Error: MCP server {tool.server} is not connected"
        try:
            result = await session.call_tool(tool.tool, arguments)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported to the LLM, never fatal
            return f"Error: {_describe_error(exc)}"
        return _render_result(result)

    async def aclose(self) -> None:
        """Close every session and transport (stdio children are stopped).

        Must run in the task that opened them: the SDK's transports are
        anyio cancel scopes (design/MCP.md §3)."""
        self._sessions.clear()
        self._tools.clear()
        with contextlib.suppress(Exception):
            await self._stack.aclose()


def _render_result(result: object) -> str:
    """Render a CallToolResult as the text handed back to the LLM.

    Text blocks are joined; anything else becomes a placeholder, since a
    tool message can only carry text."""
    content = getattr(result, "content", None)
    parts: list[str] = []
    for block in content or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
        else:
            parts.append(f"[non-text content omitted: {getattr(block, 'type', '?')}]")
    rendered = "\n".join(parts).strip() or "(the tool returned no content)"
    if getattr(result, "is_error", False):
        return f"Error: {rendered}"
    return rendered


def _describe_error(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "connection timed out"
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
