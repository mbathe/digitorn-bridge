"""Real tests for Notebook kernel, Shell session, and Database interactive actions.

Tests use real Python execution, real bash, and real SQLite.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from digitorn.core.sidecar import spawn_sidecar
from digitorn.core.sidecar_pool import DaemonSidecarPool


# ── Notebook Kernel Tests ────────────────────────────────────────


class TestNotebookKernel:
    """Test real Python kernel sidecar execution."""

    async def test_kernel_execute_simple(self):
        """Execute simple Python and get result."""
        channel = await spawn_sidecar(
            "test-kernel",
            [sys.executable, "-m", "digitorn.modules.notebook.kernel_server"],
            "jsonrpc",
        )
        try:
            result = await channel.request("execute", {"code": "1 + 1"})
            assert result["result"] == "2"
            assert result["error"] is None
        finally:
            await channel.close()

    async def test_kernel_execute_print(self):
        """Execute print and capture stdout."""
        channel = await spawn_sidecar(
            "test-kernel-print",
            [sys.executable, "-m", "digitorn.modules.notebook.kernel_server"],
            "jsonrpc",
        )
        try:
            result = await channel.request("execute", {"code": "print('hello world')"})
            assert "hello world" in result["stdout"]
        finally:
            await channel.close()

    async def test_kernel_state_persists(self):
        """Variables persist between execute calls."""
        channel = await spawn_sidecar(
            "test-kernel-state",
            [sys.executable, "-m", "digitorn.modules.notebook.kernel_server"],
            "jsonrpc",
        )
        try:
            await channel.request("execute", {"code": "x = 42"})
            result = await channel.request("execute", {"code": "x * 2"})
            assert result["result"] == "84"
        finally:
            await channel.close()

    async def test_kernel_imports_persist(self):
        """Imports persist between calls."""
        channel = await spawn_sidecar(
            "test-kernel-import",
            [sys.executable, "-m", "digitorn.modules.notebook.kernel_server"],
            "jsonrpc",
        )
        try:
            await channel.request("execute", {"code": "import math"})
            result = await channel.request("execute", {"code": "math.pi"})
            assert "3.14" in result["result"]
        finally:
            await channel.close()

    async def test_kernel_function_definition(self):
        """Define a function and call it later."""
        channel = await spawn_sidecar(
            "test-kernel-func",
            [sys.executable, "-m", "digitorn.modules.notebook.kernel_server"],
            "jsonrpc",
        )
        try:
            await channel.request("execute", {"code": "def double(n):\n    return n * 2"})
            result = await channel.request("execute", {"code": "double(21)"})
            assert result["result"] == "42"
        finally:
            await channel.close()

    async def test_kernel_error_handling(self):
        """Syntax and runtime errors are captured."""
        channel = await spawn_sidecar(
            "test-kernel-error",
            [sys.executable, "-m", "digitorn.modules.notebook.kernel_server"],
            "jsonrpc",
        )
        try:
            # Runtime error
            result = await channel.request("execute", {"code": "1 / 0"})
            assert result["error"] is not None
            assert "ZeroDivisionError" in result["error"]

            # State should still work after error
            result = await channel.request("execute", {"code": "1 + 1"})
            assert result["result"] == "2"
            assert result["error"] is None
        finally:
            await channel.close()

    async def test_kernel_variables_list(self):
        """List variables in the namespace."""
        channel = await spawn_sidecar(
            "test-kernel-vars",
            [sys.executable, "-m", "digitorn.modules.notebook.kernel_server"],
            "jsonrpc",
        )
        try:
            await channel.request("execute", {"code": "name = 'Alice'\nage = 30\nscores = [90, 85, 92]"})
            variables = await channel.request("variables", {})
            names = [v["name"] for v in variables]
            assert "name" in names
            assert "age" in names
            assert "scores" in names
        finally:
            await channel.close()

    async def test_kernel_inspect(self):
        """Inspect a specific variable."""
        channel = await spawn_sidecar(
            "test-kernel-inspect",
            [sys.executable, "-m", "digitorn.modules.notebook.kernel_server"],
            "jsonrpc",
        )
        try:
            await channel.request("execute", {"code": "data = list(range(100))"})
            info = await channel.request("inspect", {"name": "data"})
            assert info["found"] is True
            assert info["type"] == "list"
            assert info["length"] == 100
        finally:
            await channel.close()

    async def test_kernel_restart(self):
        """Restart clears state."""
        channel = await spawn_sidecar(
            "test-kernel-restart",
            [sys.executable, "-m", "digitorn.modules.notebook.kernel_server"],
            "jsonrpc",
        )
        try:
            await channel.request("execute", {"code": "persistent_var = 999"})
            await channel.request("restart", {})
            result = await channel.request("execute", {"code": "persistent_var"})
            assert result["error"] is not None  # Variable should be gone
            assert "NameError" in result["error"]
        finally:
            await channel.close()

    async def test_kernel_multiline(self):
        """Execute multiline code with loops and comprehensions."""
        channel = await spawn_sidecar(
            "test-kernel-multiline",
            [sys.executable, "-m", "digitorn.modules.notebook.kernel_server"],
            "jsonrpc",
        )
        try:
            code = """
result = []
for i in range(5):
    result.append(i ** 2)
result
"""
            result = await channel.request("execute", {"code": code.strip()})
            assert result["result"] == "[0, 1, 4, 9, 16]"
        finally:
            await channel.close()

    async def test_notebook_module_integration(self):
        """Test the NotebookModule with kernel enabled."""
        from digitorn.modules.notebook.module import NotebookModule

        pool = DaemonSidecarPool()
        await pool.start()

        module = NotebookModule()
        module._sidecar_pool = pool
        module._app_id = "test"

        try:
            # Simulate config with kernel enabled
            await module.on_config_update({"kernel": True})
            assert module._kernel_channel is not None

            # Execute
            result = await module.execute("execute_cell", {"code": "2 ** 10"})
            assert result.success
            assert result.data["result"] == "1024"

            # Variables
            await module.execute("execute_cell", {"code": "greeting = 'hello'"})
            result = await module.execute("get_variables", {})
            assert result.success
            names = [v["name"] for v in result.data["variables"]]
            assert "greeting" in names

            # Restart
            result = await module.execute("restart_kernel", {})
            assert result.success
        finally:
            await module.on_stop()
            await pool.stop()


# ── Shell Session Tests ──────────────────────────────────────────


class TestShellSession:
    """Test persistent bash session via sidecar."""

    async def test_session_run_basic(self):
        """Basic command execution in session."""
        from digitorn.modules.shell.module import ShellModule

        pool = DaemonSidecarPool()
        await pool.start()

        module = ShellModule()
        module._sidecar_pool = pool
        module._app_id = "test"
        module._session_enabled = True

        try:
            module._session_channel = await pool.acquire(
                "test-shell-session", "test",
                ["bash", "--norc", "--noprofile", "-i"],
                "lines",
            )

            result = await module.execute("session_run", {"command": "echo hello"})
            assert result.success
            assert "hello" in result.data["stdout"]
        finally:
            await pool.stop()

    async def test_session_cd_persists(self):
        """cd in session persists for next command."""
        from digitorn.modules.shell.module import ShellModule
        import tempfile

        pool = DaemonSidecarPool()
        await pool.start()

        module = ShellModule()
        module._sidecar_pool = pool
        module._app_id = "test"

        try:
            module._session_channel = await pool.acquire(
                "test-cd-session", "test",
                ["bash", "--norc", "--noprofile", "-i"],
                "lines",
            )

            # cd to /tmp
            result = await module.execute("session_cd", {"path": "/tmp"})
            assert result.success

            # pwd should show /tmp
            result = await module.execute("session_run", {"command": "pwd"})
            assert result.success
            assert "/tmp" in result.data["stdout"]
        finally:
            await pool.stop()

    async def test_session_env_persists(self):
        """Environment variables persist in session."""
        from digitorn.modules.shell.module import ShellModule

        pool = DaemonSidecarPool()
        await pool.start()

        module = ShellModule()
        module._sidecar_pool = pool
        module._app_id = "test"

        try:
            module._session_channel = await pool.acquire(
                "test-env-session", "test",
                ["bash", "--norc", "--noprofile", "-i"],
                "lines",
            )

            # Set env var
            await module.execute("session_env", {"key": "MY_TEST_VAR", "value": "hello123"})

            # Read it back
            result = await module.execute("session_run", {"command": "echo $MY_TEST_VAR"})
            assert result.success
            assert "hello123" in result.data["stdout"]
        finally:
            await pool.stop()

    async def test_session_without_sidecar(self):
        """session_run fails gracefully when session not enabled."""
        from digitorn.modules.shell.module import ShellModule

        module = ShellModule()
        result = await module.execute("session_run", {"command": "echo test"})
        assert not result.success
        assert "session" in result.error.lower() or "not available" in result.error.lower()


# ── Database Interactive Tests ───────────────────────────────────


class TestDatabaseInteractive:
    """Test interactive SQL actions with real SQLite."""

    @pytest.fixture
    async def db_module(self, tmp_path):
        from digitorn.modules.database.module import DatabaseModule

        module = DatabaseModule()
        await module.on_start()

        # Connect to SQLite
        db_path = str(tmp_path / "test.db")
        result = await module.execute("connect", {
            "connection_id": "default",
            "driver": "sqlite",
            "url": f"sqlite+aiosqlite:///{db_path}",
        })
        assert result.success, f"Connect failed: {result.error}"

        # Create test table
        await module.execute("execute_query", {
            "connection_id": "default",
            "query": """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    email TEXT UNIQUE,
                    age INTEGER,
                    active BOOLEAN DEFAULT 1
                )
            """,
        })

        # Insert test data
        for i in range(50):
            await module.execute("execute_query", {
                "connection_id": "default",
                "query": f"INSERT INTO users (name, email, age, active) VALUES ('User {i}', 'user{i}@test.com', {20 + i % 40}, {1 if i % 3 != 0 else 0})",
            })

        yield module
        await module.on_stop()

    async def test_sql_select(self, db_module):
        """sql() with SELECT returns rows."""
        result = await db_module.execute("sql", {
            "query": "SELECT name, email FROM users WHERE age > 40 LIMIT 5",
        })
        assert result.success
        assert result.data["type"] == "select"
        assert result.data["count"] > 0
        assert "name" in result.data["columns"]

    async def test_sql_insert(self, db_module):
        """sql() with INSERT returns affected count."""
        result = await db_module.execute("sql", {
            "query": "INSERT INTO users (name, email, age) VALUES ('New User', 'new@test.com', 25)",
        })
        assert result.success
        assert result.data["type"] == "execute"
        assert result.data["rows_affected"] >= 1

    async def test_sql_count(self, db_module):
        """sql() with COUNT aggregate."""
        result = await db_module.execute("sql", {
            "query": "SELECT COUNT(*) as total FROM users",
        })
        assert result.success
        assert result.data["rows"][0]["total"] >= 50

    async def test_browse_first_page(self, db_module):
        """Browse first page of table."""
        result = await db_module.execute("browse", {
            "table": "users",
            "page": 1,
            "per_page": 10,
        })
        assert result.success
        assert len(result.data["rows"]) == 10
        assert result.data["total_rows"] >= 50
        assert result.data["has_next"] is True
        assert result.data["has_prev"] is False

    async def test_browse_pagination(self, db_module):
        """Browse through multiple pages."""
        p1 = await db_module.execute("browse", {"table": "users", "page": 1, "per_page": 10})
        p2 = await db_module.execute("browse", {"table": "users", "page": 2, "per_page": 10})

        assert p1.success and p2.success
        # Different rows on different pages
        ids_p1 = {r.get("id") or r[0] for r in p1.data["rows"]}
        ids_p2 = {r.get("id") or r[0] for r in p2.data["rows"]}
        assert ids_p1.isdisjoint(ids_p2), "Pages should have different rows"

    async def test_search_data_contains(self, db_module):
        """Search for data with LIKE."""
        result = await db_module.execute("search_data", {
            "table": "users",
            "column": "email",
            "value": "@test.com",
            "mode": "contains",
        })
        assert result.success
        assert result.data["count"] > 0

    async def test_search_data_exact(self, db_module):
        """Search with exact match."""
        result = await db_module.execute("search_data", {
            "table": "users",
            "column": "name",
            "value": "User 0",
            "mode": "exact",
        })
        assert result.success
        assert result.data["count"] == 1

    async def test_sql_error(self, db_module):
        """Invalid SQL returns error."""
        result = await db_module.execute("sql", {
            "query": "SELECT * FROM nonexistent_table",
        })
        assert not result.success
        assert "error" in result.error.lower()

    async def test_sql_no_connection(self):
        """sql() without connection returns helpful error."""
        from digitorn.modules.database.module import DatabaseModule
        module = DatabaseModule()
        try:
            result = await module.execute("sql", {"query": "SELECT 1"})
            assert not result.success
            assert "connect" in result.error.lower()
        except Exception as exc:
            # Module may raise ActionExecutionError - that's acceptable
            assert "connection" in str(exc).lower() or "not found" in str(exc).lower()
