"""Tests — MCPConnectionPool: connect, disconnect, call_tool, reconnect, introspection.

Covers:
- connect() creates entry with cached capabilities
- disconnect() removes entry and closes transport
- disconnect_all() cleans up everything
- reconnect() re-uses original config
- call_tool() routes to transport.send()
- list_resources(), read_resource(), list_prompts(), get_prompt()
- ping() updates status
- get_server(), list_servers(), get_all_tools() introspection
- Error handling: not connected, server not found
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from digitorn.modules.mcp.connections import MCPConnectionPool, MCPServerEntry
from digitorn.modules.mcp.protocol import MCPPromptDef, MCPResourceDef, MCPToolDef
from digitorn.modules.mcp.transports import MCPTransportError


# ── Helpers ──────────────────────────────────────────────────────────────


def _mock_transport(
    tools: list[dict] | None = None,
    resources: list[dict] | None = None,
    prompts: list[dict] | None = None,
):
    """Create a mock transport that responds to MCP protocol methods."""
    transport = MagicMock()
    transport.connected = True
    transport.server_info = {"name": "mock-server", "version": "1.0"}
    transport.server_capabilities = {"tools": {}}

    async def mock_send(method, params=None):
        if method == "tools/list":
            return {"tools": tools or []}
        if method == "resources/list":
            return {"resources": resources or []}
        if method == "prompts/list":
            return {"prompts": prompts or []}
        if method == "tools/call":
            return {
                "content": [{"type": "text", "text": f"called {params['name']}"}],
                "isError": False,
            }
        if method == "resources/read":
            return {"contents": [{"text": "resource data"}]}
        if method == "prompts/get":
            return {"messages": [{"role": "user", "content": {"type": "text", "text": "prompt result"}}]}
        if method == "ping":
            return {}
        return {}

    transport.connect = AsyncMock()
    transport.send = AsyncMock(side_effect=mock_send)
    transport.send_notification = AsyncMock()
    transport.close = AsyncMock()

    return transport


# ── MCPServerEntry ───────────────────────────────────────────────────────


class TestMCPServerEntry:
    def test_to_dict(self):
        transport = MagicMock()
        transport.server_info = {"name": "test"}
        transport.server_capabilities = {}

        entry = MCPServerEntry(
            server_id="slack",
            transport_type="stdio",
            transport=transport,
            tools=[MCPToolDef(name="post"), MCPToolDef(name="list")],
            resources=[MCPResourceDef(uri="file:///a")],
        )
        d = entry.to_dict()
        assert d["server_id"] == "slack"
        assert d["tools_count"] == 2
        assert d["resources_count"] == 1
        assert d["status"] == "connected"

    def test_server_info_property(self):
        transport = MagicMock()
        transport.server_info = {"name": "my-server"}
        entry = MCPServerEntry(server_id="test", transport_type="stdio", transport=transport)
        assert entry.server_info == {"name": "my-server"}

    def test_server_capabilities_property(self):
        transport = MagicMock()
        transport.server_capabilities = {"tools": {}, "resources": {}}
        entry = MCPServerEntry(server_id="test", transport_type="stdio", transport=transport)
        assert "tools" in entry.server_capabilities


# ── MCPConnectionPool ────────────────────────────────────────────────────


class TestMCPConnectionPoolConnect:
    @pytest.mark.asyncio
    async def test_connect_creates_entry(self):
        pool = MCPConnectionPool()
        transport = _mock_transport(tools=[{"name": "greet", "description": "Say hi"}])

        with patch("digitorn.modules.mcp.connections.create_transport", return_value=transport):
            entry = await pool.connect("test", "stdio", command="echo")

        assert entry.server_id == "test"
        assert entry.status == "connected"
        assert len(entry.tools) == 1
        assert entry.tools[0].name == "greet"

    @pytest.mark.asyncio
    async def test_connect_replaces_existing(self):
        pool = MCPConnectionPool()
        t1 = _mock_transport(tools=[{"name": "a"}])
        t2 = _mock_transport(tools=[{"name": "b"}])

        with patch("digitorn.modules.mcp.connections.create_transport", return_value=t1):
            await pool.connect("s", "stdio", command="echo")

        with patch("digitorn.modules.mcp.connections.create_transport", return_value=t2):
            entry = await pool.connect("s", "stdio", command="echo")

        assert len(pool.servers) == 1
        assert entry.tools[0].name == "b"
        t1.close.assert_awaited()

    @pytest.mark.asyncio
    async def test_connect_failure_stores_error_entry(self):
        pool = MCPConnectionPool()
        transport = MagicMock()
        transport.connect = AsyncMock(side_effect=MCPTransportError("boom"))
        transport.close = AsyncMock()

        with patch("digitorn.modules.mcp.connections.create_transport", return_value=transport):
            with pytest.raises(MCPTransportError, match="boom"):
                await pool.connect("fail", "stdio", command="bad")

        # Entry stored with error status
        entry = pool.get_server("fail")
        assert entry is not None
        assert entry.status == "error"
        assert entry.error == "boom"


class TestMCPConnectionPoolDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect(self):
        pool = MCPConnectionPool()
        transport = _mock_transport()

        with patch("digitorn.modules.mcp.connections.create_transport", return_value=transport):
            await pool.connect("s", "stdio", command="echo")

        await pool.disconnect("s")
        assert pool.get_server("s") is None
        transport.close.assert_awaited()

    @pytest.mark.asyncio
    async def test_disconnect_nonexistent_is_noop(self):
        pool = MCPConnectionPool()
        await pool.disconnect("ghost")  # Should not raise

    @pytest.mark.asyncio
    async def test_disconnect_all(self):
        pool = MCPConnectionPool()
        t1 = _mock_transport()
        t2 = _mock_transport()

        with patch("digitorn.modules.mcp.connections.create_transport", return_value=t1):
            await pool.connect("a", "stdio", command="echo")
        with patch("digitorn.modules.mcp.connections.create_transport", return_value=t2):
            await pool.connect("b", "stdio", command="echo")

        count = await pool.disconnect_all()
        assert count == 2
        assert len(pool.servers) == 0


class TestMCPConnectionPoolReconnect:
    @pytest.mark.asyncio
    async def test_reconnect_uses_original_config(self):
        pool = MCPConnectionPool()
        t1 = _mock_transport(tools=[{"name": "old"}])
        t2 = _mock_transport(tools=[{"name": "new"}])

        with patch("digitorn.modules.mcp.connections.create_transport", return_value=t1):
            await pool.connect("s", "stdio", command="npx", args=["server"])

        with patch("digitorn.modules.mcp.connections.create_transport", return_value=t2) as mock_create:
            entry = await pool.reconnect("s")

        assert entry.tools[0].name == "new"

    @pytest.mark.asyncio
    async def test_reconnect_unknown_raises(self):
        pool = MCPConnectionPool()
        with pytest.raises(MCPTransportError, match="Unknown server"):
            await pool.reconnect("ghost")


class TestMCPConnectionPoolCallTool:
    @pytest.mark.asyncio
    async def test_call_tool_success(self):
        pool = MCPConnectionPool()
        transport = _mock_transport()

        with patch("digitorn.modules.mcp.connections.create_transport", return_value=transport):
            await pool.connect("s", "stdio", command="echo")

        result = await pool.call_tool("s", "post_message", {"text": "hi"})
        assert result.is_error is False
        assert "called post_message" in result.text

    @pytest.mark.asyncio
    async def test_call_tool_not_connected_raises(self):
        pool = MCPConnectionPool()
        with pytest.raises(MCPTransportError, match="not connected"):
            await pool.call_tool("ghost", "tool", {})

    @pytest.mark.asyncio
    async def test_call_tool_disconnected_status_raises(self):
        pool = MCPConnectionPool()
        transport = _mock_transport()
        transport.connected = False

        with patch("digitorn.modules.mcp.connections.create_transport", return_value=transport):
            # Force through despite transport.connected being False
            pool._servers["s"] = MCPServerEntry(
                server_id="s",
                transport_type="stdio",
                transport=transport,
                status="error",
            )

        with pytest.raises(MCPTransportError, match="not connected"):
            await pool.call_tool("s", "tool", {})


class TestMCPConnectionPoolResources:
    @pytest.mark.asyncio
    async def test_list_resources(self):
        pool = MCPConnectionPool()
        transport = _mock_transport(resources=[{"uri": "file:///test", "name": "test"}])
        transport.server_capabilities = {"tools": {}, "resources": {}}

        with patch("digitorn.modules.mcp.connections.create_transport", return_value=transport):
            await pool.connect("s", "stdio", command="echo")

        resources = await pool.list_resources("s")
        assert len(resources) == 1
        assert resources[0].uri == "file:///test"

    @pytest.mark.asyncio
    async def test_read_resource(self):
        pool = MCPConnectionPool()
        transport = _mock_transport()

        with patch("digitorn.modules.mcp.connections.create_transport", return_value=transport):
            await pool.connect("s", "stdio", command="echo")

        result = await pool.read_resource("s", "file:///test")
        assert "contents" in result


class TestMCPConnectionPoolPrompts:
    @pytest.mark.asyncio
    async def test_list_prompts(self):
        pool = MCPConnectionPool()
        transport = _mock_transport(prompts=[{"name": "hello", "description": "A greeting"}])
        transport.server_capabilities = {"tools": {}, "prompts": {}}

        with patch("digitorn.modules.mcp.connections.create_transport", return_value=transport):
            await pool.connect("s", "stdio", command="echo")

        prompts = await pool.list_prompts("s")
        assert len(prompts) == 1
        assert prompts[0].name == "hello"

    @pytest.mark.asyncio
    async def test_get_prompt(self):
        pool = MCPConnectionPool()
        transport = _mock_transport()

        with patch("digitorn.modules.mcp.connections.create_transport", return_value=transport):
            await pool.connect("s", "stdio", command="echo")

        result = await pool.get_prompt("s", "hello", {"name": "world"})
        assert "messages" in result


class TestMCPConnectionPoolHealth:
    @pytest.mark.asyncio
    async def test_ping_success(self):
        pool = MCPConnectionPool()
        transport = _mock_transport()

        with patch("digitorn.modules.mcp.connections.create_transport", return_value=transport):
            await pool.connect("s", "stdio", command="echo")

        alive = await pool.ping("s")
        assert alive is True
        entry = pool.get_server("s")
        assert entry.last_ping is not None
        assert entry.error is None

    @pytest.mark.asyncio
    async def test_ping_failure(self):
        pool = MCPConnectionPool()
        transport = _mock_transport()

        with patch("digitorn.modules.mcp.connections.create_transport", return_value=transport):
            await pool.connect("s", "stdio", command="echo")

        # Make ping fail
        transport.send = AsyncMock(side_effect=MCPTransportError("timeout"))
        alive = await pool.ping("s")
        assert alive is False
        entry = pool.get_server("s")
        assert entry.status == "error"

    @pytest.mark.asyncio
    async def test_ping_unknown_server(self):
        pool = MCPConnectionPool()
        assert await pool.ping("ghost") is False


class TestMCPConnectionPoolIntrospection:
    @pytest.mark.asyncio
    async def test_list_servers(self):
        pool = MCPConnectionPool()
        transport = _mock_transport(tools=[{"name": "a"}])

        with patch("digitorn.modules.mcp.connections.create_transport", return_value=transport):
            await pool.connect("s1", "stdio", command="echo")

        servers = pool.list_servers()
        assert len(servers) == 1
        assert servers[0]["server_id"] == "s1"

    @pytest.mark.asyncio
    async def test_get_all_tools(self):
        pool = MCPConnectionPool()
        t1 = _mock_transport(tools=[{"name": "a"}, {"name": "b"}])
        t2 = _mock_transport(tools=[{"name": "c"}])

        with patch("digitorn.modules.mcp.connections.create_transport", return_value=t1):
            await pool.connect("s1", "stdio", command="echo")
        with patch("digitorn.modules.mcp.connections.create_transport", return_value=t2):
            await pool.connect("s2", "stdio", command="echo")

        all_tools = pool.get_all_tools()
        assert len(all_tools) == 3
        server_ids = [sid for sid, _ in all_tools]
        assert "s1" in server_ids
        assert "s2" in server_ids

    @pytest.mark.asyncio
    async def test_get_all_tools_skips_disconnected(self):
        pool = MCPConnectionPool()
        transport = _mock_transport(tools=[{"name": "a"}])

        with patch("digitorn.modules.mcp.connections.create_transport", return_value=transport):
            await pool.connect("s", "stdio", command="echo")

        pool._servers["s"].status = "error"
        assert pool.get_all_tools() == []

    @pytest.mark.asyncio
    async def test_refresh_capabilities(self):
        pool = MCPConnectionPool()
        transport = _mock_transport(tools=[{"name": "a"}])

        with patch("digitorn.modules.mcp.connections.create_transport", return_value=transport):
            await pool.connect("s", "stdio", command="echo")

        # Change what the mock returns
        transport.send = AsyncMock(return_value={"tools": [
            {"name": "a"}, {"name": "b"},
        ]})
        entry = await pool.refresh_capabilities("s")
        assert len(entry.tools) == 2
