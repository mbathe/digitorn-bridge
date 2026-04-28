"""Integration tests for the sidecar system with a real JSON-RPC server.

Uses tests/fixtures/mock_lsp_server.py as a subprocess - tests real
stdio communication, request/response, push notifications, reconnect,
pool ref-counting, and LSP module integration.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from digitorn.core.sidecar import SidecarChannel, spawn_sidecar
from digitorn.core.sidecar_pool import DaemonSidecarPool

MOCK_SERVER = str(Path(__file__).parent / "fixtures" / "mock_lsp_server.py")
MOCK_CMD = [sys.executable, MOCK_SERVER]


# ── JSON-RPC request/response ────────────────────────────────────


class TestJsonRpcRealServer:
    """Test real JSON-RPC communication with mock LSP server."""

    async def test_initialize_handshake(self):
        """Full LSP initialize → initialized → shutdown → exit."""
        channel = await spawn_sidecar(
            name="test-init",
            command=MOCK_CMD,
            protocol="jsonrpc",
        )
        try:
            # Initialize
            result = await channel.request("initialize", {
                "processId": None,
                "capabilities": {},
                "rootUri": "file:///tmp/test",
            })
            assert result is not None
            assert result["capabilities"]["textDocumentSync"] == 1
            assert result["serverInfo"]["name"] == "mock-lsp"

            # Initialized notification (no response)
            await channel.notify("initialized", {})

            # Shutdown (returns null)
            result = await channel.request("shutdown", {})
            assert result is None

            # Exit
            await channel.notify("exit", {})
            await asyncio.sleep(0.2)
        finally:
            await channel.close()

    async def test_request_response_echo(self):
        """Send a request and get the exact params back."""
        channel = await spawn_sidecar("test-echo", MOCK_CMD, "jsonrpc")
        try:
            result = await channel.request("test/echo", {"hello": "world", "n": 42})
            assert result == {"hello": "world", "n": 42}
        finally:
            await channel.close()

    async def test_request_response_computation(self):
        """Server-side computation via JSON-RPC."""
        channel = await spawn_sidecar("test-add", MOCK_CMD, "jsonrpc")
        try:
            result = await channel.request("test/add", {"a": 17, "b": 25})
            assert result == {"sum": 42}
        finally:
            await channel.close()

    async def test_multiple_requests_sequential(self):
        """Multiple sequential requests on the same channel."""
        channel = await spawn_sidecar("test-multi", MOCK_CMD, "jsonrpc")
        try:
            r1 = await channel.request("test/add", {"a": 1, "b": 2})
            r2 = await channel.request("test/add", {"a": 10, "b": 20})
            r3 = await channel.request("test/echo", {"seq": 3})
            assert r1 == {"sum": 3}
            assert r2 == {"sum": 30}
            assert r3 == {"seq": 3}
        finally:
            await channel.close()

    async def test_multiple_requests_concurrent(self):
        """Multiple concurrent requests - id tracking must work."""
        channel = await spawn_sidecar("test-concurrent", MOCK_CMD, "jsonrpc")
        try:
            results = await asyncio.gather(
                channel.request("test/add", {"a": 1, "b": 1}),
                channel.request("test/add", {"a": 2, "b": 2}),
                channel.request("test/add", {"a": 3, "b": 3}),
            )
            sums = sorted(r["sum"] for r in results)
            assert sums == [2, 4, 6]
        finally:
            await channel.close()

    async def test_error_response(self):
        """Server returns a JSON-RPC error."""
        channel = await spawn_sidecar("test-err", MOCK_CMD, "jsonrpc")
        try:
            with pytest.raises(RuntimeError, match="JSON-RPC error"):
                await channel.request("test/error", {})
        finally:
            await channel.close()

    async def test_unknown_method_error(self):
        """Unknown method returns method-not-found error."""
        channel = await spawn_sidecar("test-unknown", MOCK_CMD, "jsonrpc")
        try:
            with pytest.raises(RuntimeError, match="Method not found"):
                await channel.request("nonexistent/method", {})
        finally:
            await channel.close()

    async def test_request_timeout(self):
        """Request that exceeds timeout raises TimeoutError."""
        channel = await spawn_sidecar("test-timeout", MOCK_CMD, "jsonrpc")
        try:
            with pytest.raises(TimeoutError):
                await channel.request("test/slow", {"delay": 5}, timeout=1)
        finally:
            await channel.close()


# ── Push notifications ───────────────────────────────────────────


class TestPushNotifications:
    """Test server-initiated push notifications (LSP diagnostics pattern)."""

    async def test_diagnostics_on_didopen(self):
        """Opening a file with TODOs triggers publishDiagnostics."""
        received: list[tuple[str, dict]] = []

        async def handler(method: str, params: dict[str, Any]) -> None:
            received.append((method, params))

        channel = await spawn_sidecar(
            "test-diag", MOCK_CMD, "jsonrpc",
            on_notification=handler,
        )
        try:
            await channel.request("initialize", {"processId": None, "capabilities": {}, "rootUri": "file:///tmp"})
            await channel.notify("initialized", {})

            # Open a file with a TODO and a FIXME
            await channel.notify("textDocument/didOpen", {
                "textDocument": {
                    "uri": "file:///tmp/test.py",
                    "languageId": "python",
                    "version": 1,
                    "text": "# TODO: fix this\ndef foo():\n    pass  # FIXME: broken\n",
                },
            })

            # Wait for diagnostics push
            await asyncio.sleep(0.5)

            # Should have received publishDiagnostics
            diag_events = [(m, p) for m, p in received if m == "textDocument/publishDiagnostics"]
            assert len(diag_events) >= 1, f"Expected publishDiagnostics, got: {[m for m, _ in received]}"

            _, params = diag_events[0]
            assert params["uri"] == "file:///tmp/test.py"
            assert len(params["diagnostics"]) == 2  # TODO + FIXME

            severities = sorted(d["severity"] for d in params["diagnostics"])
            assert severities == [2, 3]  # Warning (FIXME) + Info (TODO)
        finally:
            await channel.close()

    async def test_diagnostics_on_didchange(self):
        """Changing a file triggers fresh diagnostics."""
        received: list[tuple[str, dict]] = []

        async def handler(method: str, params: dict[str, Any]) -> None:
            received.append((method, params))

        channel = await spawn_sidecar("test-change", MOCK_CMD, "jsonrpc", on_notification=handler)
        try:
            await channel.request("initialize", {"processId": None, "capabilities": {}, "rootUri": "file:///tmp"})
            await channel.notify("initialized", {})

            # Open clean file → 0 diagnostics
            await channel.notify("textDocument/didOpen", {
                "textDocument": {"uri": "file:///tmp/clean.py", "languageId": "python", "version": 1, "text": "def foo():\n    pass\n"},
            })
            await asyncio.sleep(0.3)

            clean_diags = [p for m, p in received if m == "textDocument/publishDiagnostics" and p["uri"] == "file:///tmp/clean.py"]
            assert len(clean_diags) >= 1
            assert len(clean_diags[-1]["diagnostics"]) == 0  # No issues

            # Change to add a wildcard import → error diagnostic
            received.clear()
            await channel.notify("textDocument/didChange", {
                "textDocument": {"uri": "file:///tmp/clean.py", "version": 2},
                "contentChanges": [{"text": "from os import *\ndef foo():\n    pass\n"}],
            })
            await asyncio.sleep(0.3)

            change_diags = [p for m, p in received if m == "textDocument/publishDiagnostics"]
            assert len(change_diags) >= 1
            assert len(change_diags[-1]["diagnostics"]) == 1
            assert change_diags[-1]["diagnostics"][0]["severity"] == 1  # Error
            assert change_diags[-1]["diagnostics"][0]["code"] == "wildcard-import"
        finally:
            await channel.close()

    async def test_multiple_files(self):
        """Diagnostics tracked per-file independently."""
        received: list[tuple[str, dict]] = []

        async def handler(method: str, params: dict[str, Any]) -> None:
            received.append((method, params))

        channel = await spawn_sidecar("test-multi-files", MOCK_CMD, "jsonrpc", on_notification=handler)
        try:
            await channel.request("initialize", {"processId": None, "capabilities": {}, "rootUri": "file:///tmp"})

            await channel.notify("textDocument/didOpen", {
                "textDocument": {"uri": "file:///a.py", "languageId": "python", "version": 1, "text": "# TODO: a\n"},
            })
            await channel.notify("textDocument/didOpen", {
                "textDocument": {"uri": "file:///b.py", "languageId": "python", "version": 1, "text": "# clean\n"},
            })
            await asyncio.sleep(0.5)

            a_diags = [p for m, p in received if m == "textDocument/publishDiagnostics" and p["uri"] == "file:///a.py"]
            b_diags = [p for m, p in received if m == "textDocument/publishDiagnostics" and p["uri"] == "file:///b.py"]

            assert len(a_diags[-1]["diagnostics"]) == 1  # TODO
            assert len(b_diags[-1]["diagnostics"]) == 0  # Clean
        finally:
            await channel.close()


# ── Channel lifecycle ────────────────────────────────────────────


class TestChannelLifecycle:
    async def test_restart_preserves_config(self):
        """After restart, channel reconnects and works."""
        channel = await spawn_sidecar("test-restart-rpc", MOCK_CMD, "jsonrpc")
        try:
            r1 = await channel.request("test/echo", {"before": True})
            assert r1 == {"before": True}

            pid1 = channel._process.pid
            await channel.restart()
            pid2 = channel._process.pid
            assert pid1 != pid2

            r2 = await channel.request("test/echo", {"after": True})
            assert r2 == {"after": True}
        finally:
            await channel.close()

    async def test_close_fails_pending_requests(self):
        """Closing a channel with pending requests fails them."""
        channel = await spawn_sidecar("test-close-pending", MOCK_CMD, "jsonrpc")

        # Start a slow request
        slow_task = asyncio.create_task(
            channel.request("test/slow", {"delay": 10}, timeout=15)
        )
        await asyncio.sleep(0.2)

        # Close while request is pending
        await channel.close()

        with pytest.raises((ConnectionError, asyncio.CancelledError, TimeoutError)):
            await slow_task

    async def test_request_after_close_fails(self):
        """Requesting on a closed channel raises ConnectionError."""
        channel = await spawn_sidecar("test-closed", MOCK_CMD, "jsonrpc")
        await channel.close()

        with pytest.raises(ConnectionError):
            await channel.request("test/echo", {})


# ── Pool integration ─────────────────────────────────────────────


class TestPoolIntegration:
    async def test_pool_acquire_jsonrpc(self):
        """Pool creates a working JSON-RPC channel."""
        pool = DaemonSidecarPool()
        await pool.start()
        try:
            channel = await pool.acquire("pool-rpc", "app1", MOCK_CMD, "jsonrpc")
            result = await channel.request("test/echo", {"pool": True})
            assert result == {"pool": True}
        finally:
            await pool.stop()

    async def test_pool_shared_channel_works(self):
        """Two apps share one channel - both can communicate."""
        pool = DaemonSidecarPool()
        await pool.start()
        try:
            ch1 = await pool.acquire("shared-rpc", "app1", MOCK_CMD, "jsonrpc")
            ch2 = await pool.acquire("shared-rpc", "app2", MOCK_CMD, "jsonrpc")

            assert ch1 is ch2  # Same channel

            r1 = await ch1.request("test/add", {"a": 1, "b": 2})
            r2 = await ch2.request("test/add", {"a": 10, "b": 20})
            assert r1 == {"sum": 3}
            assert r2 == {"sum": 30}

            # Release app1 - channel stays for app2
            await pool.release("shared-rpc", "app1")
            r3 = await ch2.request("test/echo", {"still": "alive"})
            assert r3 == {"still": "alive"}
        finally:
            await pool.stop()

    async def test_pool_release_kills_process(self):
        """Releasing last ref kills the subprocess."""
        pool = DaemonSidecarPool()
        await pool.start()
        try:
            channel = await pool.acquire("kill-test", "app1", MOCK_CMD, "jsonrpc")
            pid = channel._process.pid

            await pool.release("kill-test", "app1")

            # Process should be dead
            import os
            await asyncio.sleep(0.3)
            try:
                os.kill(pid, 0)
                assert False, f"Process {pid} still alive"
            except ProcessLookupError:
                pass  # Expected
        finally:
            await pool.stop()

    async def test_pool_notifications_received(self):
        """Notifications work through pool-acquired channels."""
        received: list[tuple[str, dict]] = []

        async def handler(method: str, params: dict[str, Any]) -> None:
            received.append((method, params))

        pool = DaemonSidecarPool()
        await pool.start()
        try:
            channel = await pool.acquire(
                "pool-notif", "app1", MOCK_CMD, "jsonrpc",
                on_notification=handler,
            )
            await channel.request("initialize", {"processId": None, "capabilities": {}, "rootUri": "file:///tmp"})
            await channel.notify("textDocument/didOpen", {
                "textDocument": {"uri": "file:///pool.py", "languageId": "python", "version": 1, "text": "# TODO: test\n"},
            })
            await asyncio.sleep(0.5)

            diag_events = [m for m, _ in received if m == "textDocument/publishDiagnostics"]
            assert len(diag_events) >= 1
        finally:
            await pool.stop()


# ── LSP module integration ───────────────────────────────────────


@pytest.mark.skip(reason="Replaced by test_lsp_v3.py - module API changed in v3")
class TestLspModuleIntegration:
    """Test the LSP module with the mock server via sidecar. SUPERSEDED by test_lsp_v3.py."""

    async def test_lsp_diagnostics_realtime_mode(self):
        """LSP module uses sidecar for real-time diagnostics."""
        from digitorn.modules.lsp.module import LspModule
        from digitorn.core.sidecar_pool import DaemonSidecarPool

        pool = DaemonSidecarPool()
        await pool.start()

        module = LspModule()
        module._sidecar_pool = pool
        module._app_id = "test-app"
        module._workspace = "/tmp"

        try:
            # Manually acquire a channel (bypassing auto-detect)
            channel = await pool.acquire(
                "lsp-mock", "test-app", MOCK_CMD, "jsonrpc",
                on_notification=module._on_lsp_notification,
            )
            await channel.request("initialize", {"processId": None, "capabilities": {}, "rootUri": "file:///tmp"})
            await channel.notify("initialized", {})
            module._channels[".py"] = channel

            # Open a file with issues
            await module._ensure_file_open("/tmp/test_lsp_integration.py", channel)

            # Write a test file
            Path("/tmp/test_lsp_integration.py").write_text("# TODO: fix this\nfrom os import *\n")

            # Notify change
            result = await module.execute("notify_change", {"path": "/tmp/test_lsp_integration.py"})
            assert result.success

            # Wait for push diagnostics
            await asyncio.sleep(0.5)

            # Get diagnostics from cache
            result = await module.execute("diagnostics", {"path": "/tmp/test_lsp_integration.py"})
            assert result.success
            assert result.data["mode"] == "realtime"

            diags = result.data["diagnostics"]
            assert len(diags) >= 1  # At least the wildcard import or TODO
        finally:
            # Cleanup
            Path("/tmp/test_lsp_integration.py").unlink(missing_ok=True)
            await pool.stop()

    async def test_lsp_check_realtime(self):
        """LSP check action uses real-time mode when available."""
        from digitorn.modules.lsp.module import LspModule
        from digitorn.core.sidecar_pool import DaemonSidecarPool

        pool = DaemonSidecarPool()
        await pool.start()

        module = LspModule()
        module._sidecar_pool = pool
        module._app_id = "test-app"

        try:
            channel = await pool.acquire(
                "lsp-check", "test-app", MOCK_CMD, "jsonrpc",
                on_notification=module._on_lsp_notification,
            )
            await channel.request("initialize", {"processId": None, "capabilities": {}, "rootUri": "file:///tmp"})
            module._channels[".py"] = channel

            # Write a clean file
            Path("/tmp/test_lsp_check.py").write_text("def foo():\n    return 1\n")

            result = await module.execute("check", {"path": "/tmp/test_lsp_check.py"})
            assert result.success
            assert result.data["mode"] == "realtime"
            assert result.data["passed"] is True
        finally:
            Path("/tmp/test_lsp_check.py").unlink(missing_ok=True)
            await pool.stop()

    async def test_lsp_fallback_when_no_sidecar(self):
        """Without sidecar, LSP module falls back to linter shell-out."""
        from digitorn.modules.lsp.module import LspModule

        module = LspModule()
        # No sidecar_pool, no channels

        result = await module.execute("diagnostics", {"path": "nonexistent.xyz"})
        assert not result.success
        # Should mention no linter found
        assert "No linter" in (result.error or "")


# ── NdJson real process test ─────────────────────────────────────


class TestNdJsonProcess:
    """Test ndjson protocol with a real Python subprocess."""

    async def test_ndjson_send_receive(self):
        """Send ndjson lines and receive them back via a Python echo."""
        received: list[dict] = []

        async def handler(method: str, params: dict[str, Any]) -> None:
            received.append(params)

        # Use python -c to create a simple ndjson echo
        cmd = [
            sys.executable, "-c",
            "import sys, json\n"
            "for line in sys.stdin:\n"
            "    obj = json.loads(line)\n"
            "    json.dump(obj, sys.stdout)\n"
            "    sys.stdout.write('\\n')\n"
            "    sys.stdout.flush()\n"
        ]

        channel = await spawn_sidecar("test-ndjson", cmd, "ndjson", on_notification=handler)
        try:
            await channel.send({"type": "ping", "seq": 1})
            await channel.send({"type": "ping", "seq": 2})
            await asyncio.sleep(0.3)

            assert len(received) >= 2
            seqs = sorted(r.get("seq", 0) for r in received)
            assert seqs == [1, 2]
        finally:
            await channel.close()


# ── Lines real process test ──────────────────────────────────────


class TestLinesProcess:
    """Test lines protocol with a real subprocess."""

    async def test_lines_round_trip(self):
        """Send lines and receive them via cat."""
        received: list[dict] = []

        async def handler(method: str, params: dict[str, Any]) -> None:
            received.append(params)

        channel = await spawn_sidecar("test-lines-rt", ["cat"], "lines", on_notification=handler)
        try:
            await channel.send("line one")
            await channel.send("line two")
            await channel.send("line three")
            await asyncio.sleep(0.3)

            lines = [r.get("line", "") for r in received]
            assert lines == ["line one", "line two", "line three"]
        finally:
            await channel.close()
