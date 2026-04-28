"""Tests - MCP Transports: factory, StdioTransport, SSE/HTTP transport init.

Covers:
- create_transport() factory routing and validation
- StdioTransport environment building
- StdioTransport connect/send/close lifecycle with mocked subprocess
- SSETransport and StreamableHTTPTransport initialization
- MCPTransportError attributes
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from digitorn.modules.mcp.transports import (
    MCPTransport,
    MCPTransportError,
    SSETransport,
    StdioTransport,
    StreamableHTTPTransport,
    create_transport,
)


# ── Transport Factory ────────────────────────────────────────────────────


class TestCreateTransport:
    def test_stdio(self):
        t = create_transport("stdio", command="echo", args=["hello"])
        assert isinstance(t, StdioTransport)

    def test_stdio_requires_command(self):
        with pytest.raises(ValueError, match="command"):
            create_transport("stdio")

    def test_sse(self):
        t = create_transport("sse", url="http://localhost:3000/sse")
        assert isinstance(t, SSETransport)

    def test_sse_requires_url(self):
        with pytest.raises(ValueError, match="url"):
            create_transport("sse")

    def test_streamable_http(self):
        t = create_transport("streamable_http", url="http://localhost:3000")
        assert isinstance(t, StreamableHTTPTransport)

    def test_http_alias(self):
        t = create_transport("http", url="http://localhost:3000")
        assert isinstance(t, StreamableHTTPTransport)

    def test_http_requires_url(self):
        with pytest.raises(ValueError, match="url"):
            create_transport("streamable_http")

    def test_unknown_transport(self):
        with pytest.raises(ValueError, match="Unknown transport"):
            create_transport("websocket")


# ── MCPTransportError ────────────────────────────────────────────────────


class TestMCPTransportError:
    def test_basic(self):
        err = MCPTransportError("connection lost")
        assert str(err) == "connection lost"
        assert err.code == -1
        assert err.data is None

    def test_with_code_and_data(self):
        err = MCPTransportError("bad request", code=-32600, data={"detail": "x"})
        assert err.code == -32600
        assert err.data == {"detail": "x"}


# ── StdioTransport ──────────────────────────────────────────────────────


class TestStdioTransport:
    def test_init_defaults(self):
        t = StdioTransport(command="npx", args=["@test/server"])
        assert t._command == "npx"
        assert t._args == ["@test/server"]
        assert t._timeout == 30.0
        assert t.connected is False

    def test_build_env_inherits_safe_vars(self):
        t = StdioTransport(command="echo")
        with patch.dict("os.environ", {"PATH": "/usr/bin", "HOME": "/home/test", "SECRET": "nope"}, clear=True):
            env = t._build_env()
            assert env["PATH"] == "/usr/bin"
            assert env["HOME"] == "/home/test"
            assert "SECRET" not in env

    def test_build_env_explicit_override(self):
        t = StdioTransport(command="echo", env={"MY_TOKEN": "abc123"})
        with patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True):
            env = t._build_env()
            assert env["MY_TOKEN"] == "abc123"
            # PATH may be extended by _ensure_node_in_path (nvm/fnm/volta)
            assert "/usr/bin" in env["PATH"]

    @pytest.mark.asyncio
    async def test_send_not_connected_raises(self):
        t = StdioTransport(command="echo")
        with pytest.raises(MCPTransportError, match="Not connected"):
            await t.send("ping")

    @pytest.mark.asyncio
    async def test_send_notification_not_connected_raises(self):
        t = StdioTransport(command="echo")
        with pytest.raises(MCPTransportError, match="Not connected"):
            await t.send_notification("test")

    @pytest.mark.asyncio
    async def test_close_when_not_connected(self):
        """close() should be safe to call even when not connected."""
        t = StdioTransport(command="echo")
        await t.close()  # Should not raise
        assert t.connected is False

    @pytest.mark.asyncio
    async def test_connect_command_not_found(self):
        t = StdioTransport(command="nonexistent_mcp_command_xyz")
        with pytest.raises(MCPTransportError, match="Command not found"):
            await t.connect()

    def test_build_env_uses_safe_env(self):
        """_build_env filters out dangerous env vars."""
        t = StdioTransport(command="echo", env={"MY_VAR": "safe"})
        with patch.dict("os.environ", {
            "PATH": "/usr/bin", "HOME": "/home/test",
            "DIGITORN_SECRET_KEY": "dangerous", "DATABASE_URL": "dangerous",
        }, clear=True):
            env = t._build_env()
            assert env["MY_VAR"] == "safe"
            assert "/usr/bin" in env["PATH"]
            # Dangerous vars should be blocked
            assert "DIGITORN_SECRET_KEY" not in env
            assert "DATABASE_URL" not in env

    def test_init_with_env(self):
        """StdioTransport stores env."""
        t = StdioTransport(
            command="npx", args=["@test/server"],
            env={"TOKEN": "x"}, timeout=60.0,
        )
        assert t._command == "npx"
        assert t._args == ["@test/server"]
        assert t._timeout == 60.0
        assert t._env == {"TOKEN": "x"}


# ── SSETransport ─────────────────────────────────────────────────────────


class TestSSETransport:
    def test_init(self):
        t = SSETransport(url="http://localhost:3000/sse", headers={"X-Key": "abc"})
        assert t._url == "http://localhost:3000/sse"
        assert t._headers == {"X-Key": "abc"}
        assert t.connected is False

    @pytest.mark.asyncio
    async def test_send_not_connected_raises(self):
        t = SSETransport(url="http://localhost:3000/sse")
        with pytest.raises(MCPTransportError, match="Not connected"):
            await t.send("ping")

    @pytest.mark.asyncio
    async def test_close_safe(self):
        t = SSETransport(url="http://localhost:3000/sse")
        await t.close()
        assert t.connected is False


# ── StreamableHTTPTransport ──────────────────────────────────────────────


class TestStreamableHTTPTransport:
    def test_init(self):
        t = StreamableHTTPTransport(url="http://localhost:3000/mcp", timeout=10.0)
        assert t._url == "http://localhost:3000/mcp"
        assert t._timeout == 10.0
        assert t.connected is False

    @pytest.mark.asyncio
    async def test_close_resets_state(self):
        t = StreamableHTTPTransport(url="http://localhost:3000")
        t._connected = True
        await t.close()
        assert t.connected is False
        assert t._session is None

    def test_ready_event_starts_unset(self):
        t = StreamableHTTPTransport(url="http://localhost:3000")
        assert not t._ready_event.is_set()
        assert t._bg_task is None

    def test_server_info_initially_empty(self):
        t = StreamableHTTPTransport(url="http://localhost:3000")
        assert t._server_info == {}
        assert t._server_capabilities == {}


# ── Protocol compliance ──────────────────────────────────────────────────


class TestProtocolCompliance:
    def test_stdio_implements_protocol(self):
        t = StdioTransport(command="echo")
        assert isinstance(t, MCPTransport)

    def test_sse_implements_protocol(self):
        t = SSETransport(url="http://localhost:3000")
        assert isinstance(t, MCPTransport)

    def test_http_implements_protocol(self):
        t = StreamableHTTPTransport(url="http://localhost:3000")
        assert isinstance(t, MCPTransport)
