"""Tests for :class:`digitorn.core.preview.manager.PreviewManager`.

We don't start a real Vite/Next dev server — that would require a
packaged bundle. Instead we use a tiny Python script that behaves like
one: binds a TCP port and answers HTTP. This lets us exercise the full
lifecycle (install → start → readiness → stop → restart) in ~1s.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import textwrap
from pathlib import Path

import pytest

from digitorn.core.app.schema import PreviewConfig
from digitorn.core.preview import PreviewManager, PreviewState
from digitorn.core.runtime import node_runtime as nr


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    try:
        return s.getsockname()[1]
    finally:
        s.close()


def _write_fake_dev_server(dir_path: Path, port: int) -> None:
    """Emit a trivial HTTP server script the manager can spawn."""
    script = textwrap.dedent(f"""
        import http.server, socketserver, sys
        class H(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a, **kw): pass
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.end_headers()
                self.wfile.write(b'OK')
        with socketserver.TCPServer(('127.0.0.1', {port}), H) as srv:
            print('fake dev server ready', flush=True)
            try:
                srv.serve_forever()
            except KeyboardInterrupt:
                pass
    """)
    (dir_path / "server.py").write_text(script, encoding="utf-8")


@pytest.fixture
def fake_bundle(tmp_path):
    """Return a bundle dir containing a runnable fake dev server."""
    port = _free_port()
    _write_fake_dev_server(tmp_path, port)
    return tmp_path, port


def _ensure_node_runtime():
    nr.reset_node_runtime()
    rt = nr.get_node_runtime()
    rt.set_auto_install(False)
    try:
        asyncio.run(rt.ensure_installed())
    except nr.NodeRuntimeError:
        pytest.skip("no node runtime available for preview tests")
    return rt


def test_preview_manager_full_lifecycle(fake_bundle):
    """start → wait for readiness → stop."""
    bundle, port = fake_bundle
    _ensure_node_runtime()

    cfg = PreviewConfig(
        command=[sys.executable, "server.py"],
        cwd=".",
        port=port,
        startup_timeout=10.0,
    )
    pm = PreviewManager(cfg, bundle_dir=bundle, app_id="test-app")

    async def _run():
        await pm.start()
        assert pm.state == PreviewState.RUNNING

        # Connect to the port to confirm the process is really serving
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.close()

        await pm.stop()
        assert pm.state == PreviewState.STOPPED

    asyncio.run(_run())


def test_preview_manager_respects_disabled_flag(fake_bundle):
    bundle, port = fake_bundle
    _ensure_node_runtime()

    cfg = PreviewConfig(
        enabled=False,
        command=[sys.executable, "server.py"],
        cwd=".",
        port=port,
    )
    pm = PreviewManager(cfg, bundle_dir=bundle, app_id="disabled-app")

    async def _run():
        await pm.start()
        # start() must be a no-op when disabled
        assert pm.state == PreviewState.STOPPED
        assert pm._proc is None

    asyncio.run(_run())


def test_preview_manager_crashed_when_startup_timeout(fake_bundle):
    """If the command doesn't open the port, start() raises."""
    bundle, port = fake_bundle
    _ensure_node_runtime()

    # Command that exits immediately → readiness check times out
    cfg = PreviewConfig(
        command=[sys.executable, "-c", "import time; time.sleep(0.1)"],
        cwd=".",
        port=port,
        startup_timeout=1.0,
        restart_on_crash=False,
    )
    pm = PreviewManager(cfg, bundle_dir=bundle, app_id="failing-app")

    async def _run():
        with pytest.raises((RuntimeError, TimeoutError)):
            await pm.start()
        assert pm.state == PreviewState.CRASHED

    asyncio.run(_run())


def test_preview_manager_install_marker_is_idempotent(tmp_path):
    """install() writes a marker and skips on subsequent calls."""
    _ensure_node_runtime()
    cfg = PreviewConfig(
        command=[sys.executable, "-c", "pass"],
        cwd=".",
        port=_free_port(),
        install_command=[sys.executable, "-c", "print('installing')"],
    )
    pm = PreviewManager(cfg, bundle_dir=tmp_path, app_id="install-app")

    async def _run():
        await pm.install()
        marker = tmp_path / ".digitorn-preview-installed"
        assert marker.exists()
        # Second call: should be a no-op — we observe this by checking
        # that the log buffer doesn't grow a second "[install]" line.
        logs_before = len(pm.get_logs())
        await pm.install()
        logs_after = len(pm.get_logs())
        assert logs_after == logs_before

    asyncio.run(_run())


def test_preview_manager_status_dict_round_trip():
    cfg = PreviewConfig(
        command=["dummy"],
        cwd=".",
        port=1234,
    )
    pm = PreviewManager(cfg, bundle_dir=Path("/tmp"), app_id="status-app")
    data = pm.status().as_dict()
    assert data["state"] == "stopped"
    assert data["port"] == 1234
    assert data["logs_tail"] == []
