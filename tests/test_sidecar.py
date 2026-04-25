"""Tests for the sidecar system (channels, codecs, pool)."""

from __future__ import annotations

import asyncio
import json

import pytest

from digitorn.core.sidecar import (
    JsonRpcCodec,
    LinesCodec,
    NdJsonCodec,
    SidecarChannel,
    spawn_sidecar,
)
from digitorn.core.sidecar_pool import DaemonSidecarPool


# ── Codec tests ──────────────────────────────────────────────────


class TestJsonRpcCodec:
    def test_encode_request(self):
        data = JsonRpcCodec.encode_request(1, "initialize", {"capabilities": {}})
        assert b"Content-Length:" in data
        body = data.split(b"\r\n\r\n", 1)[1]
        msg = json.loads(body)
        assert msg["jsonrpc"] == "2.0"
        assert msg["id"] == 1
        assert msg["method"] == "initialize"
        assert msg["params"] == {"capabilities": {}}

    def test_encode_notification(self):
        data = JsonRpcCodec.encode_notification("initialized", {})
        body = data.split(b"\r\n\r\n", 1)[1]
        msg = json.loads(body)
        assert "id" not in msg
        assert msg["method"] == "initialized"

    def test_content_length_correct(self):
        data = JsonRpcCodec.encode_request(42, "test/method", {"key": "value"})
        header, body = data.split(b"\r\n\r\n", 1)
        length_str = header.decode().split(":")[1].strip()
        assert int(length_str) == len(body)

    async def test_read_message(self):
        msg = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
        body = json.dumps(msg).encode()
        raw = f"Content-Length: {len(body)}\r\n\r\n".encode() + body

        reader = asyncio.StreamReader()
        reader.feed_data(raw)
        reader.feed_eof()

        result = await JsonRpcCodec.read_message(reader)
        assert result == msg


class TestLinesCodec:
    def test_encode(self):
        assert LinesCodec.encode("hello") == b"hello\n"
        assert LinesCodec.encode("") == b"\n"

    async def test_read_message(self):
        reader = asyncio.StreamReader()
        reader.feed_data(b"hello world\n")
        reader.feed_eof()

        result = await LinesCodec.read_message(reader)
        assert result == "hello world"

    async def test_read_eof(self):
        reader = asyncio.StreamReader()
        reader.feed_eof()
        assert await LinesCodec.read_message(reader) is None


class TestNdJsonCodec:
    def test_encode(self):
        data = NdJsonCodec.encode({"type": "event", "data": 42})
        assert data.endswith(b"\n")
        assert json.loads(data) == {"type": "event", "data": 42}

    async def test_read_message(self):
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"type":"test","value":1}\n')
        reader.feed_eof()

        result = await NdJsonCodec.read_message(reader)
        assert result == {"type": "test", "value": 1}


# ── SidecarChannel tests ────────────────────────────────────────


class TestSidecarChannel:
    async def test_spawn_echo(self):
        """Spawn a simple echo process via lines protocol."""
        channel = await spawn_sidecar(
            name="test-cat",
            command=["cat"],
            protocol="lines",
        )
        assert channel.status == "connected"
        assert channel._process is not None
        assert channel._process.pid > 0

        await channel.close()
        assert channel.status == "disconnected"

    async def test_spawn_nonexistent(self):
        """Spawn a nonexistent command."""
        with pytest.raises(FileNotFoundError):
            await spawn_sidecar(
                name="test-nonexistent",
                command=["totally_nonexistent_binary_xyz"],
                protocol="lines",
            )

    async def test_lines_send_receive(self):
        """Send lines and receive them back via cat."""
        received: list[dict[str, Any]] = []

        async def handler(method: str, params: dict) -> None:
            received.append(params)

        channel = await spawn_sidecar(
            name="test-echo",
            command=["cat"],
            protocol="lines",
            on_notification=handler,
        )

        await channel.send("hello")
        await channel.send("world")
        await asyncio.sleep(0.2)  # Let reader task process

        await channel.close()

        assert len(received) >= 2
        assert received[0].get("line") == "hello"
        assert received[1].get("line") == "world"

    async def test_close_kills_process(self):
        """Verify close terminates the subprocess."""
        channel = await spawn_sidecar(
            name="test-sleep",
            command=["sleep", "60"],
            protocol="lines",
        )
        pid = channel._process.pid
        await channel.close(timeout=2)
        assert channel.status == "disconnected"
        # Process should be dead
        import os
        try:
            os.kill(pid, 0)
            # If we get here, process is still alive — that's wrong
            assert False, f"Process {pid} still alive after close"
        except ProcessLookupError:
            pass  # Expected — process is dead

    async def test_restart(self):
        """Restart a channel."""
        channel = await spawn_sidecar(
            name="test-restart",
            command=["cat"],
            protocol="lines",
        )
        pid1 = channel._process.pid
        await channel.restart()
        pid2 = channel._process.pid
        assert pid1 != pid2
        assert channel.status == "connected"
        await channel.close()


# ── Pool tests ───────────────────────────────────────────────────


class TestDaemonSidecarPool:
    async def test_acquire_release(self):
        pool = DaemonSidecarPool()
        await pool.start()

        channel = await pool.acquire(
            name="test-pool-cat",
            app_id="app1",
            command=["cat"],
            protocol="lines",
        )
        assert channel.status == "connected"

        channels = pool.list_channels()
        assert len(channels) == 1
        assert channels[0]["name"] == "test-pool-cat"
        assert channels[0]["ref_count"] == 1

        await pool.release("test-pool-cat", "app1")
        assert len(pool.list_channels()) == 0

        await pool.stop()

    async def test_ref_counting(self):
        pool = DaemonSidecarPool()
        await pool.start()

        ch1 = await pool.acquire("shared", "app1", command=["cat"], protocol="lines")
        ch2 = await pool.acquire("shared", "app2", command=["cat"], protocol="lines")

        # Same channel (shared)
        assert ch1 is ch2
        assert pool.list_channels()[0]["ref_count"] == 2

        # Release one — channel stays
        await pool.release("shared", "app1")
        assert len(pool.list_channels()) == 1
        assert pool.list_channels()[0]["ref_count"] == 1

        # Release last — channel removed
        await pool.release("shared", "app2")
        assert len(pool.list_channels()) == 0

        await pool.stop()

    async def test_stop_cleans_all(self):
        pool = DaemonSidecarPool()
        await pool.start()

        await pool.acquire("ch1", "app1", command=["cat"], protocol="lines")
        await pool.acquire("ch2", "app1", command=["cat"], protocol="lines")
        assert len(pool.list_channels()) == 2

        await pool.stop()
        assert len(pool.list_channels()) == 0


# ── LSP module tests ─────────────────────────────────────────────


class TestLspModuleV2:
    def test_actions_registered(self):
        from digitorn.modules.lsp.module import LspModule

        m = LspModule()
        actions = list(m._action_registry.keys())
        assert "diagnostics" in actions
        assert "check" in actions
        assert "notify_change" in actions

    def test_version(self):
        from digitorn.modules.lsp.module import LspModule

        m = LspModule()
        assert m.VERSION == "3.0.0"
