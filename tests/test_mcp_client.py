from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from mcp.types import ListToolsResult, Tool

from expecto.config import MCPServerConfig
from expecto.mcp_client import (
    MCPManager,
    open_mcp_errlog,
    qualified_tool_name,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import TextIO

STDIO_SERVER = str(Path(__file__).parent / "mcp_stdio_server.py")


@pytest.fixture
def errlog() -> Iterator[TextIO]:
    fh = open_mcp_errlog()
    yield fh
    fh.close()


def _stdio_server(name: str = "S") -> MCPServerConfig:
    return MCPServerConfig(
        name=name, type="stdio", command=sys.executable, args=[STDIO_SERVER],
    )


class _FakeSession:
    """Stands in for mcp.ClientSession: async context manager with
    initialize() and list_tools()."""

    def __init__(self, tools: list[Tool], fail: Exception | None = None) -> None:
        self._tools = tools
        self._fail = fail
        self.closed = False

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True

    async def initialize(self) -> None:
        if self._fail is not None:
            raise self._fail

    async def list_tools(self) -> ListToolsResult:
        return ListToolsResult(tools=self._tools)


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch, sessions: dict[str, _FakeSession],
) -> None:
    """Replace the transports and ClientSession so connect() can be tested
    without real servers; sessions are keyed by server name."""

    async def fake_open_transport(
        self: MCPManager, server: MCPServerConfig,
    ) -> tuple[object, object]:
        if server.name not in sessions:
            raise ConnectionRefusedError("connection refused")
        return (server.name, None)

    monkeypatch.setattr(MCPManager, "_open_transport", fake_open_transport)
    monkeypatch.setattr(
        "expecto.mcp_client.ClientSession",
        lambda read, _write: sessions[str(read)],
    )


def _tool(name: str) -> Tool:
    return Tool(
        name=name,
        description=f"{name} does things",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
    )


class TestQualifiedToolName:
    def test_namespaces_by_server(self) -> None:
        assert qualified_tool_name("SERVER1", "search") == "mcp__SERVER1__search"


class TestConnect:
    async def test_collects_tools_from_every_server(
        self, monkeypatch: pytest.MonkeyPatch, errlog: TextIO,
    ) -> None:
        _install_fakes(monkeypatch, {
            "A": _FakeSession([_tool("search")]),
            "B": _FakeSession([_tool("search"), _tool("write")]),
        })
        manager = MCPManager(errlog)
        warnings = await manager.connect([
            MCPServerConfig(name="A", type="sse", url="http://a"),
            MCPServerConfig(name="B", type="sse", url="http://b"),
        ])
        assert warnings == []
        assert [t.qualified_name for t in manager.tools] == [
            "mcp__A__search", "mcp__B__search", "mcp__B__write",
        ]
        assert manager.tools[0].description == "search does things"
        assert manager.tools[0].input_schema["type"] == "object"

    async def test_failing_server_is_dropped_others_kept(
        self, monkeypatch: pytest.MonkeyPatch, errlog: TextIO,
    ) -> None:
        _install_fakes(monkeypatch, {"GOOD": _FakeSession([_tool("search")])})
        manager = MCPManager(errlog)
        warnings = await manager.connect([
            MCPServerConfig(name="DEAD", type="sse", url="http://dead"),
            MCPServerConfig(name="GOOD", type="sse", url="http://good"),
        ])
        assert [t.qualified_name for t in manager.tools] == ["mcp__GOOD__search"]
        assert len(warnings) == 1
        assert "mcp.DEAD" in warnings[0]
        assert "connection refused" in warnings[0]

    async def test_initialize_failure_is_reported(
        self, monkeypatch: pytest.MonkeyPatch, errlog: TextIO,
    ) -> None:
        _install_fakes(
            monkeypatch, {"A": _FakeSession([], fail=RuntimeError("handshake failed"))},
        )
        manager = MCPManager(errlog)
        warnings = await manager.connect(
            [MCPServerConfig(name="A", type="sse", url="http://a")],
        )
        assert manager.tools == []
        assert "handshake failed" in warnings[0]

    async def test_slow_server_times_out(
        self, monkeypatch: pytest.MonkeyPatch, errlog: TextIO,
    ) -> None:
        async def never_opens(
            self: MCPManager, server: MCPServerConfig,
        ) -> tuple[object, object]:
            await asyncio.sleep(10)
            raise AssertionError("unreachable")

        monkeypatch.setattr(MCPManager, "_open_transport", never_opens)
        manager = MCPManager(errlog, connect_timeout_ms=10)
        warnings = await manager.connect(
            [MCPServerConfig(name="SLOW", type="sse", url="http://slow")],
        )
        assert manager.tools == []
        assert "timed out" in warnings[0]

    async def test_no_servers_is_a_noop(self, errlog: TextIO) -> None:
        manager = MCPManager(errlog)
        assert await manager.connect([]) == []
        assert manager.tools == []


class TestStdioTransport:
    """End-to-end against a real MCP server subprocess (tests/mcp_stdio_server.py)."""

    async def test_connects_and_lists_tools(self, errlog: TextIO) -> None:
        manager = MCPManager(errlog)
        try:
            warnings = await manager.connect([_stdio_server()])
            assert warnings == []
            assert sorted(t.qualified_name for t in manager.tools) == [
                "mcp__S__boom", "mcp__S__echo",
            ]
            echo = next(t for t in manager.tools if t.tool == "echo")
            assert "text" in str(echo.input_schema)
        finally:
            await manager.aclose()

    async def test_aclose_stops_the_child_and_clears_state(
        self, errlog: TextIO,
    ) -> None:
        manager = MCPManager(errlog)
        await manager.connect([_stdio_server()])
        assert manager.tools
        await manager.aclose()
        assert manager.tools == []

    async def test_missing_command_is_reported(self, errlog: TextIO) -> None:
        manager = MCPManager(errlog)
        warnings = await manager.connect([
            MCPServerConfig(
                name="NOPE", type="stdio", command="expecto-no-such-command",
            ),
        ])
        await manager.aclose()
        assert manager.tools == []
        assert "mcp.NOPE" in warnings[0]


class TestToolDefs:
    async def test_exposes_namespaced_non_strict_functions(
        self, monkeypatch: pytest.MonkeyPatch, errlog: TextIO,
    ) -> None:
        _install_fakes(monkeypatch, {"A": _FakeSession([_tool("search")])})
        manager = MCPManager(errlog)
        await manager.connect([MCPServerConfig(name="A", type="sse", url="http://a")])
        (definition,) = manager.tool_defs()
        assert definition["type"] == "function"
        function = definition["function"]
        assert function["name"] == "mcp__A__search"
        # The server's own schema is passed through, and strict mode — which
        # arbitrary MCP schemas do not satisfy — is not claimed.
        assert function["parameters"] == _tool("search").input_schema
        assert "strict" not in function
        assert "A" in function["description"]

    async def test_tool_without_description_still_gets_one(
        self, monkeypatch: pytest.MonkeyPatch, errlog: TextIO,
    ) -> None:
        bare = Tool(name="run", input_schema={"type": "object"})
        _install_fakes(monkeypatch, {"A": _FakeSession([bare])})
        manager = MCPManager(errlog)
        await manager.connect([MCPServerConfig(name="A", type="sse", url="http://a")])
        assert manager.tool_defs()[0]["function"]["description"]


class TestCall:
    """Calls go through the real stdio server (tests/mcp_stdio_server.py)."""

    async def test_returns_the_tool_result_text(self, errlog: TextIO) -> None:
        manager = MCPManager(errlog)
        try:
            await manager.connect([_stdio_server()])
            output = await manager.call("mcp__S__echo", {"text": "hello there"})
            assert "hello there" in output
        finally:
            await manager.aclose()

    async def test_failing_tool_comes_back_as_error_text(
        self, errlog: TextIO,
    ) -> None:
        """A tool failure is information for the LLM, not an exception."""
        manager = MCPManager(errlog)
        try:
            await manager.connect([_stdio_server()])
            output = await manager.call("mcp__S__boom", {})
            assert output.startswith("Error:")
        finally:
            await manager.aclose()

    async def test_bad_arguments_come_back_as_error_text(
        self, errlog: TextIO,
    ) -> None:
        manager = MCPManager(errlog)
        try:
            await manager.connect([_stdio_server()])
            output = await manager.call("mcp__S__echo", {"wrong": 1})
            assert output.startswith("Error:")
        finally:
            await manager.aclose()

    async def test_unknown_tool_is_reported(self, errlog: TextIO) -> None:
        manager = MCPManager(errlog)
        output = await manager.call("mcp__GONE__nope", {})
        assert output == "Error: unknown MCP tool: mcp__GONE__nope"


class TestErrlog:
    def test_discards_server_stderr(self) -> None:
        """A stdio server's stderr must not reach the raw-mode tty."""
        with open_mcp_errlog() as fh:
            assert fh.name == os.devnull
