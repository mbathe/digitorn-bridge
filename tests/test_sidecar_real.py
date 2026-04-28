"""Real end-to-end tests for the sidecar + LSP system.

These tests use REAL tools (ruff, pyright) - not mocks.
They validate the full pipeline:
  - Sidecar spawns real pyright-langserver
  - JSON-RPC initialize handshake works
  - File open/change triggers real diagnostics
  - Diagnostics contain real error messages
  - Ruff linter fallback works on real Python files
  - LSP module dual-mode works end-to-end
  - Pool ref-counting with real processes

Requires: pip install ruff pyright
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

import pytest

from digitorn.core.sidecar import spawn_sidecar
from digitorn.core.sidecar_pool import DaemonSidecarPool

# Skip all if pyright not installed
pytestmark = pytest.mark.skipif(
    not shutil.which("pyright-langserver"),
    reason="pyright-langserver not installed",
)


# ── Real pyright LSP tests ───────────────────────────────────────


class TestRealPyrightLSP:
    """Test real pyright-langserver via sidecar JSON-RPC."""

    async def test_pyright_initialize(self):
        """Pyright responds to initialize with real capabilities."""
        channel = await spawn_sidecar(
            name="pyright-init-test",
            command=["pyright-langserver", "--stdio"],
            protocol="jsonrpc",
        )
        try:
            result = await channel.request("initialize", {
                "processId": None,
                "capabilities": {},
                "rootUri": "file:///tmp/test-pyright",
                "workspaceFolders": [{"uri": "file:///tmp/test-pyright", "name": "test"}],
            }, timeout=30)

            assert result is not None
            assert "capabilities" in result
            # Pyright should support text document sync
            caps = result["capabilities"]
            assert caps.get("textDocumentSync") is not None

            await channel.notify("initialized", {})
            await channel.request("shutdown", {}, timeout=10)
            await channel.notify("exit", {})
        finally:
            await channel.close()

    async def test_pyright_detects_type_error(self):
        """Pyright detects a real type error in Python code."""
        received: list[tuple[str, dict]] = []

        async def handler(method: str, params: dict[str, Any]) -> None:
            received.append((method, params))

        # Create a real Python file with a type error
        test_dir = Path("/tmp/test-pyright-diag")
        test_dir.mkdir(exist_ok=True)
        test_file = test_dir / "bad.py"
        test_file.write_text(
            'def add(a: int, b: int) -> int:\n'
            '    return a + b\n'
            '\n'
            'result: str = add(1, 2)  # Type error: int assigned to str\n'
        )

        channel = await spawn_sidecar(
            "pyright-diag-test",
            ["pyright-langserver", "--stdio"],
            "jsonrpc",
            on_notification=handler,
        )
        try:
            await channel.request("initialize", {
                "processId": None,
                "capabilities": {"textDocument": {"publishDiagnostics": {}}},
                "rootUri": f"file://{test_dir}",
                "workspaceFolders": [{"uri": f"file://{test_dir}", "name": "test"}],
            }, timeout=30)
            await channel.notify("initialized", {})

            # Open the file
            await channel.notify("textDocument/didOpen", {
                "textDocument": {
                    "uri": f"file://{test_file}",
                    "languageId": "python",
                    "version": 1,
                    "text": test_file.read_text(),
                },
            })

            # Wait for pyright to analyze (it's slower than our mock)
            await asyncio.sleep(5)

            diag_events = [
                (m, p) for m, p in received
                if m == "textDocument/publishDiagnostics"
                and p.get("uri", "").endswith("bad.py")
            ]

            assert len(diag_events) > 0, (
                f"No diagnostics received for bad.py. "
                f"All events: {[(m, p.get('uri', '')) for m, p in received]}"
            )

            # Check that pyright found the type mismatch
            all_diags = []
            for _, p in diag_events:
                all_diags.extend(p.get("diagnostics", []))

            assert len(all_diags) > 0, "Pyright returned 0 diagnostics for a file with a type error"

            # At least one diagnostic should mention type incompatibility
            messages = [d.get("message", "").lower() for d in all_diags]
            has_type_error = any(
                "type" in msg or "incompatible" in msg or "cannot be assigned" in msg
                for msg in messages
            )
            assert has_type_error, f"Expected type error diagnostic, got: {messages}"

            await channel.request("shutdown", {}, timeout=10)
            await channel.notify("exit", {})
        finally:
            await channel.close()
            # Cleanup
            import shutil as sh
            sh.rmtree(test_dir, ignore_errors=True)

    async def test_pyright_clean_file_no_errors(self):
        """Pyright reports 0 diagnostics for a clean Python file."""
        received: list[tuple[str, dict]] = []

        async def handler(method: str, params: dict[str, Any]) -> None:
            received.append((method, params))

        test_dir = Path("/tmp/test-pyright-clean")
        test_dir.mkdir(exist_ok=True)
        test_file = test_dir / "clean.py"
        test_file.write_text(
            'def greet(name: str) -> str:\n'
            '    return f"Hello, {name}!"\n'
            '\n'
            'message: str = greet("World")\n'
        )

        channel = await spawn_sidecar(
            "pyright-clean-test",
            ["pyright-langserver", "--stdio"],
            "jsonrpc",
            on_notification=handler,
        )
        try:
            await channel.request("initialize", {
                "processId": None,
                "capabilities": {"textDocument": {"publishDiagnostics": {}}},
                "rootUri": f"file://{test_dir}",
            }, timeout=30)
            await channel.notify("initialized", {})

            await channel.notify("textDocument/didOpen", {
                "textDocument": {
                    "uri": f"file://{test_file}",
                    "languageId": "python",
                    "version": 1,
                    "text": test_file.read_text(),
                },
            })

            await asyncio.sleep(5)

            diag_events = [
                p for m, p in received
                if m == "textDocument/publishDiagnostics"
                and p.get("uri", "").endswith("clean.py")
            ]

            if diag_events:
                last_diags = diag_events[-1].get("diagnostics", [])
                assert len(last_diags) == 0, f"Expected 0 diagnostics for clean file, got: {last_diags}"

            await channel.request("shutdown", {}, timeout=10)
            await channel.notify("exit", {})
        finally:
            await channel.close()
            import shutil as sh
            sh.rmtree(test_dir, ignore_errors=True)

    async def test_pyright_didchange_updates_diagnostics(self):
        """Changing a file triggers fresh diagnostics from pyright."""
        received: list[tuple[str, dict]] = []

        async def handler(method: str, params: dict[str, Any]) -> None:
            received.append((method, params))

        test_dir = Path("/tmp/test-pyright-change")
        test_dir.mkdir(exist_ok=True)
        test_file = test_dir / "evolving.py"
        test_file.write_text('x: int = 1\n')  # Clean initially

        channel = await spawn_sidecar(
            "pyright-change-test",
            ["pyright-langserver", "--stdio"],
            "jsonrpc",
            on_notification=handler,
        )
        try:
            await channel.request("initialize", {
                "processId": None,
                "capabilities": {"textDocument": {"publishDiagnostics": {}}},
                "rootUri": f"file://{test_dir}",
            }, timeout=30)
            await channel.notify("initialized", {})

            uri = f"file://{test_file}"

            # Open clean file
            await channel.notify("textDocument/didOpen", {
                "textDocument": {"uri": uri, "languageId": "python", "version": 1, "text": "x: int = 1\n"},
            })
            await asyncio.sleep(3)

            # Now introduce a type error via didChange
            received.clear()
            await channel.notify("textDocument/didChange", {
                "textDocument": {"uri": uri, "version": 2},
                "contentChanges": [{"text": 'x: int = "not an int"\n'}],
            })
            await asyncio.sleep(5)

            diag_events = [
                p for m, p in received
                if m == "textDocument/publishDiagnostics"
                and p.get("uri", "").endswith("evolving.py")
            ]

            assert len(diag_events) > 0, "No diagnostics after didChange"
            last_diags = diag_events[-1].get("diagnostics", [])
            assert len(last_diags) > 0, "Expected diagnostics after introducing type error"

            await channel.request("shutdown", {}, timeout=10)
            await channel.notify("exit", {})
        finally:
            await channel.close()
            import shutil as sh
            sh.rmtree(test_dir, ignore_errors=True)


# ── Real ruff linter tests ───────────────────────────────────────


@pytest.mark.skip(reason="Replaced by test_lsp_v3.py TestRealRuffV3")
class TestRealRuffLinter:
    """Test real ruff linter via the LSP module fallback mode. SUPERSEDED by test_lsp_v3.py."""

    @pytest.mark.skipif(not shutil.which("ruff"), reason="ruff not installed")
    async def test_ruff_detects_issues(self):
        """Ruff detects real linting issues in Python code."""
        from digitorn.modules.lsp.module import LspModule

        # Create a file with known ruff violations
        test_dir = Path("/tmp/test-ruff")
        test_dir.mkdir(exist_ok=True)
        test_file = test_dir / "ugly.py"
        test_file.write_text(
            'import os\n'           # F401: unused import
            'import sys\n'          # F401: unused import
            'x=1\n'                 # E225: missing spaces around operator
            'def foo( ):\n'         # Various whitespace issues
            '    pass\n'
        )

        module = LspModule()
        module._workspace = str(test_dir)
        # Force fallback mode (no sidecar)

        try:
            result = await module.execute("diagnostics", {"path": str(test_file)})
            assert result.success, f"diagnostics failed: {result.error}"
            assert result.data["mode"] == "fallback"
            assert result.data["linter"] == "ruff"
            assert result.data["total"] > 0, f"Expected ruff to find issues, got 0"
            assert result.data["errors"] + result.data["warnings"] > 0

            # Check that actual diagnostic objects are returned
            diags = result.data["diagnostics"]
            assert len(diags) > 0
            assert all("file" in d and "line" in d and "message" in d for d in diags)

            # Ruff should find unused imports
            codes = [d.get("code", "") for d in diags]
            has_f401 = any("F401" in c for c in codes)
            assert has_f401, f"Expected F401 (unused import), got codes: {codes}"
        finally:
            import shutil as sh
            sh.rmtree(test_dir, ignore_errors=True)

    @pytest.mark.skipif(not shutil.which("ruff"), reason="ruff not installed")
    async def test_ruff_clean_file_passes(self):
        """Ruff reports no issues for clean Python code."""
        from digitorn.modules.lsp.module import LspModule

        test_dir = Path("/tmp/test-ruff-clean")
        test_dir.mkdir(exist_ok=True)
        test_file = test_dir / "clean.py"
        test_file.write_text(
            'def greet(name: str) -> str:\n'
            '    """Greet someone."""\n'
            '    return f"Hello, {name}!"\n'
        )

        module = LspModule()
        module._workspace = str(test_dir)

        try:
            result = await module.execute("check", {"path": str(test_file)})
            assert result.success
            assert result.data["passed"] is True
            assert result.data["errors"] == 0
        finally:
            import shutil as sh
            sh.rmtree(test_dir, ignore_errors=True)

    @pytest.mark.skipif(not shutil.which("ruff"), reason="ruff not installed")
    async def test_ruff_project_wide(self):
        """Ruff scans an entire project directory."""
        from digitorn.modules.lsp.module import LspModule

        test_dir = Path("/tmp/test-ruff-project")
        test_dir.mkdir(exist_ok=True)
        (test_dir / "pyproject.toml").write_text('[project]\nname = "test"\n')
        (test_dir / "a.py").write_text("import os\n")  # Unused import
        (test_dir / "b.py").write_text("x = 1\n")      # Clean

        module = LspModule()
        module._workspace = str(test_dir)

        try:
            result = await module.execute("diagnostics", {})
            assert result.success
            assert result.data["total"] > 0  # At least the unused import in a.py
        finally:
            import shutil as sh
            sh.rmtree(test_dir, ignore_errors=True)


# ── Real LSP module dual-mode test ───────────────────────────────


@pytest.mark.skip(reason="Replaced by test_lsp_v3.py TestRealPyrightV3")
class TestRealLspModuleDualMode:
    """Test the LSP module with real pyright sidecar + ruff fallback. SUPERSEDED by test_lsp_v3.py."""

    async def test_lsp_module_real_pyright_sidecar(self):
        """LSP module acquires real pyright via sidecar and returns diagnostics."""
        from digitorn.modules.lsp.module import LspModule

        pool = DaemonSidecarPool()
        await pool.start()

        test_dir = Path("/tmp/test-lsp-real")
        test_dir.mkdir(exist_ok=True)
        test_file = test_dir / "typed.py"
        test_file.write_text(
            'def double(x: int) -> int:\n'
            '    return x * 2\n'
            '\n'
            'val: str = double(5)  # Type error\n'
        )

        module = LspModule()
        module._sidecar_pool = pool
        module._app_id = "real-test"
        module._workspace = str(test_dir)

        try:
            # Manually wire pyright channel
            channel = await pool.acquire(
                "lsp-pyright-real",
                "real-test",
                ["pyright-langserver", "--stdio"],
                "jsonrpc",
                cwd=str(test_dir),
                on_notification=module._on_lsp_notification,
            )
            await channel.request("initialize", {
                "processId": None,
                "capabilities": {"textDocument": {"publishDiagnostics": {}}},
                "rootUri": f"file://{test_dir}",
            }, timeout=30)
            await channel.notify("initialized", {})
            module._channels[".py"] = channel

            # Use notify_change to trigger analysis
            result = await module.execute("notify_change", {"path": str(test_file)})
            assert result.success

            # Wait for pyright to analyze
            await asyncio.sleep(5)

            # Get diagnostics - should be from realtime cache
            result = await module.execute("diagnostics", {"path": str(test_file)})
            assert result.success
            assert result.data["mode"] == "realtime"

            diags = result.data["diagnostics"]
            # Pyright should find the type error
            if diags:
                messages = [d["message"].lower() for d in diags]
                has_type_issue = any("type" in m or "incompatible" in m or "cannot" in m for m in messages)
                assert has_type_issue, f"Expected type error, got: {messages}"

            # Check action also works
            check_result = await module.execute("check", {"path": str(test_file)})
            assert check_result.success
            assert check_result.data["mode"] == "realtime"
        finally:
            await pool.stop()
            import shutil as sh
            sh.rmtree(test_dir, ignore_errors=True)


# ── Real pool lifecycle test ─────────────────────────────────────


class TestRealPoolLifecycle:
    """Test pool with real pyright process - lifecycle, ref-counting, health."""

    async def test_pool_real_process_lifecycle(self):
        """Acquire real pyright, use it, release it, verify process is dead."""
        import os

        pool = DaemonSidecarPool()
        await pool.start()

        try:
            channel = await pool.acquire(
                "lifecycle-pyright",
                "app1",
                ["pyright-langserver", "--stdio"],
                "jsonrpc",
            )
            pid = channel._process.pid
            assert channel.status == "connected"

            # Verify process is alive
            os.kill(pid, 0)  # Should not raise

            # Do a real request
            result = await channel.request("initialize", {
                "processId": None, "capabilities": {},
                "rootUri": "file:///tmp",
            }, timeout=15)
            assert result is not None

            # Release and verify process dies
            await pool.release("lifecycle-pyright", "app1")
            await asyncio.sleep(1)

            try:
                os.kill(pid, 0)
                assert False, f"Process {pid} still alive after release"
            except ProcessLookupError:
                pass  # Expected - process is dead
        finally:
            await pool.stop()

    async def test_pool_ref_counting_real(self):
        """Two apps share one real pyright process."""
        pool = DaemonSidecarPool()
        await pool.start()

        try:
            ch1 = await pool.acquire("shared-pyright", "app1", ["pyright-langserver", "--stdio"], "jsonrpc")
            ch2 = await pool.acquire("shared-pyright", "app2", ["pyright-langserver", "--stdio"], "jsonrpc")

            assert ch1 is ch2  # Same channel
            pid = ch1._process.pid

            channels = pool.list_channels()
            assert len(channels) == 1
            assert channels[0]["ref_count"] == 2

            # Release app1 - pyright stays alive for app2
            await pool.release("shared-pyright", "app1")
            assert ch2.status == "connected"

            # Can still make requests
            result = await ch2.request("initialize", {
                "processId": None, "capabilities": {},
                "rootUri": "file:///tmp",
            }, timeout=15)
            assert result is not None

            # Release app2 - pyright dies
            await pool.release("shared-pyright", "app2")
            await asyncio.sleep(1)

            import os
            try:
                os.kill(pid, 0)
                assert False, "Process should be dead"
            except ProcessLookupError:
                pass
        finally:
            await pool.stop()
