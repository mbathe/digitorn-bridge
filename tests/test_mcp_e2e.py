"""End-to-end test: MCP server integration in a Digitorn app session.

Tests the full pipeline:
  YAML config → compile → bootstrap → MCP connection → tool discovery → tool execution

Uses a minimal Python MCP server (examples/test_mcp_server.py) over stdio.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

# Ensure project root is importable
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "packages") not in sys.path:
    sys.path.insert(0, str(ROOT / "packages"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mcp_test_yaml(tmp_path: Path) -> Path:
    """Write a test YAML that uses the local MCP server."""
    server_script = ROOT / "examples" / "test_mcp_server.py"
    assert server_script.exists(), f"Missing: {server_script}"

    yaml_content = f"""\
app:
  app_id: mcp-e2e-test
  name: "MCP E2E Test"

modules:
  mcp:
    config:
      servers:
        testtools:
          transport: stdio
          command: python3
          args: ["{server_script}"]

agents:
  - id: assistant
    role: assistant
    brain:
      provider: ollama
      model: "qwen2.5:14b-instruct-q4_K_M"
      backend: openai_compat
      config:
        base_url: "http://localhost:11434/v1"
      context:
        max_tokens: 32768
    system_prompt: "You are an assistant with MCP tools."

execution:
  mode: conversation
  max_turns: 10
  timeout: 60.0

capabilities:
  default_policy: auto
"""
    yaml_file = tmp_path / "mcp-e2e-test.yaml"
    yaml_file.write_text(yaml_content)
    return yaml_file


async def _bootstrap_app(yaml_path: Path):
    """Compile + bootstrap an app, return (RuntimeApp, context_builder, modules)."""
    from digitorn.core.app.compiler import AppYAMLCompiler
    from digitorn.core.loader import load_modules
    from digitorn.core.runtime.app import RuntimeApp
    from digitorn.core.runtime.bootstrap import bootstrap
    from digitorn.modules.registry import ModuleRegistry

    registry = ModuleRegistry()
    load_modules(registry, load_all=True)

    compiler = AppYAMLCompiler(registry)
    compiled = compiler.compile_file(yaml_path)

    boot_result = await bootstrap(compiled, registry)

    contexts = boot_result["contexts"]
    modules = boot_result["modules"]
    context_builder = boot_result["context_builder"]
    hook_runner = boot_result.get("hook_runner")
    approval_queue = boot_result.get("approval_queue")

    app = RuntimeApp(
        app_id=compiled.app_id,
        execution=compiled.execution,
        contexts=contexts,
        modules=modules,
        context_builder=context_builder,
        hook_runner=hook_runner,
        approval_queue=approval_queue,
    )

    return app, context_builder, modules


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMCPEndToEnd:
    """Full E2E tests for MCP server integration."""

    @pytest.mark.asyncio
    async def test_mcp_server_connects(self, mcp_test_yaml: Path):
        """MCP server is connected after bootstrap."""
        app, cb, modules = await _bootstrap_app(mcp_test_yaml)
        try:
            mcp_module = modules.get("mcp")
            assert mcp_module is not None, "MCP module should be loaded"

            pool = getattr(mcp_module, "_pool", None)
            assert pool is not None, "MCP connection pool should exist"

            servers = pool._servers
            assert "testtools" in servers, f"Server 'testtools' not in pool: {list(servers.keys())}"

            entry = servers["testtools"]
            assert entry.status == "connected", f"Server status: {entry.status}"
            assert entry.server_info is not None, "Server info should be populated"
            assert entry.server_info.get("name") == "test-mcp-server"
        finally:
            await app.shutdown()

    @pytest.mark.asyncio
    async def test_mcp_tools_indexed(self, mcp_test_yaml: Path):
        """MCP tools appear in the context_builder tool index."""
        app, cb, modules = await _bootstrap_app(mcp_test_yaml)
        try:
            index = cb.index

            # Check virtual category exists
            assert "mcp_testtools" in index.categories, (
                f"Expected 'mcp_testtools' in categories: {list(index.categories.keys())}"
            )

            cat = index.categories["mcp_testtools"]
            assert cat.tool_count == 3, f"Expected 3 tools, got {cat.tool_count}"

            # Check individual tools are indexed
            for tool_name in ["echo", "add", "weather"]:
                fqn = f"mcp_testtools.{tool_name}"
                assert fqn in index.tools, f"Tool '{fqn}' not in index: {list(index.tools.keys())}"

                tool = index.tools[fqn]
                assert tool.module_id == "mcp_testtools"
                assert tool.action_name == f"mcp_testtools__{tool_name}"
                assert "mcp" in tool.tags
        finally:
            await app.shutdown()

    @pytest.mark.asyncio
    async def test_mcp_tool_search(self, mcp_test_yaml: Path):
        """search_tools finds MCP tools by description."""
        app, cb, modules = await _bootstrap_app(mcp_test_yaml)
        try:
            # Search for "echo" tool
            result = await cb.execute("search_tools", {"query": "echo message"})
            data = result.data if hasattr(result, "data") else result
            results = data.get("tools", data.get("results", []))

            echo_found = any(
                r.get("name", "").endswith("echo") or "echo" in r.get("name", "").lower()
                for r in results
            )
            assert echo_found, f"Expected 'echo' in search results: {results}"

            # Search for "add numbers"
            result2 = await cb.execute("search_tools", {"query": "add numbers sum"})
            data2 = result2.data if hasattr(result2, "data") else result2
            results2 = data2.get("tools", data2.get("results", []))

            add_found = any(
                "add" in r.get("name", "").lower()
                for r in results2
            )
            assert add_found, f"Expected 'add' in search results: {results2}"
        finally:
            await app.shutdown()

    @pytest.mark.asyncio
    async def test_mcp_tool_execution_echo(self, mcp_test_yaml: Path):
        """execute_tool calls the MCP echo tool and gets a result."""
        app, cb, modules = await _bootstrap_app(mcp_test_yaml)
        try:
            result = await cb.execute("execute_tool", {
                "name": "mcp_testtools.echo",
                "params": {"message": "Hello from Digitorn!", "_approved": True},
            })

            # Debug: inspect full result
            assert result is not None, "execute_tool returned None"
            assert result.success, f"execute_tool failed: error={result.error}, metadata={getattr(result, 'metadata', None)}"

            data = result.data
            # The result should contain the echoed message
            result_text = str(data)
            assert "Hello from Digitorn!" in result_text, f"Echo result: {data}"
        finally:
            await app.shutdown()

    @pytest.mark.asyncio
    async def test_mcp_tool_execution_add(self, mcp_test_yaml: Path):
        """execute_tool calls the MCP add tool with numbers."""
        app, cb, modules = await _bootstrap_app(mcp_test_yaml)
        try:
            result = await cb.execute("execute_tool", {
                "name": "mcp_testtools.add",
                "params": {"a": 42, "b": 58, "_approved": True},
            })
            data = result.data if hasattr(result, "data") else result

            result_text = str(data)
            assert "100" in result_text, f"Add result should contain 100: {data}"
        finally:
            await app.shutdown()

    @pytest.mark.asyncio
    async def test_mcp_tool_execution_weather(self, mcp_test_yaml: Path):
        """execute_tool calls the MCP weather tool."""
        app, cb, modules = await _bootstrap_app(mcp_test_yaml)
        try:
            result = await cb.execute("execute_tool", {
                "name": "mcp_testtools.weather",
                "params": {"city": "Paris", "_approved": True},
            })
            data = result.data if hasattr(result, "data") else result

            result_text = str(data)
            assert "Paris" in result_text, f"Weather result should mention Paris: {data}"
            assert "sunny" in result_text or "temperature" in result_text, (
                f"Weather result should contain weather data: {data}"
            )
        finally:
            await app.shutdown()

    @pytest.mark.asyncio
    async def test_mcp_browse_category(self, mcp_test_yaml: Path):
        """browse_category lists all tools in the MCP server."""
        app, cb, modules = await _bootstrap_app(mcp_test_yaml)
        try:
            result = await cb.execute("browse_category", {"category": "mcp_testtools"})
            data = result.data if hasattr(result, "data") else result

            tools = data.get("tools", [])
            tool_names = [t.get("name", "") for t in tools]

            assert len(tools) == 3, f"Expected 3 tools, got {len(tools)}: {tool_names}"
            for expected in ["echo", "add", "weather"]:
                assert any(expected in name for name in tool_names), (
                    f"Missing '{expected}' in tools: {tool_names}"
                )
        finally:
            await app.shutdown()

    @pytest.mark.asyncio
    async def test_mcp_get_tool(self, mcp_test_yaml: Path):
        """get_tool returns full schema for an MCP tool."""
        app, cb, modules = await _bootstrap_app(mcp_test_yaml)
        try:
            result = await cb.execute("get_tool", {"name": "mcp_testtools.add"})
            data = result.data if hasattr(result, "data") else result

            assert "name" in data or "tool" in data, f"Unexpected get_tool result: {data}"

            # Should contain parameter info
            result_text = str(data)
            assert "a" in result_text and "b" in result_text, (
                f"Tool schema should mention params a and b: {data}"
            )
        finally:
            await app.shutdown()

    @pytest.mark.asyncio
    async def test_mcp_list_categories(self, mcp_test_yaml: Path):
        """list_categories includes the MCP virtual category."""
        app, cb, modules = await _bootstrap_app(mcp_test_yaml)
        try:
            result = await cb.execute("list_categories", {})
            data = result.data if hasattr(result, "data") else result

            categories = data.get("categories", [])
            cat_ids = [c.get("id", "") for c in categories]

            assert "mcp_testtools" in cat_ids, (
                f"Expected 'mcp_testtools' in categories: {cat_ids}"
            )

            # Find the MCP category
            mcp_cat = next(c for c in categories if c.get("id") == "mcp_testtools")
            assert mcp_cat.get("tool_count") == 3
        finally:
            await app.shutdown()

    @pytest.mark.asyncio
    async def test_mcp_server_health(self, mcp_test_yaml: Path):
        """MCP health check works for connected server."""
        app, cb, modules = await _bootstrap_app(mcp_test_yaml)
        try:
            mcp_module = modules["mcp"]

            # Direct module health check
            result = await mcp_module.execute("health_check", {"server_id": "testtools"})
            data = result.data if hasattr(result, "data") else result

            # Should report healthy
            result_text = str(data).lower()
            assert "error" not in result_text or "healthy" in result_text, (
                f"Health check failed: {data}"
            )
        finally:
            await app.shutdown()

    @pytest.mark.asyncio
    async def test_mcp_list_servers(self, mcp_test_yaml: Path):
        """list_servers shows connected MCP servers."""
        app, cb, modules = await _bootstrap_app(mcp_test_yaml)
        try:
            mcp_module = modules["mcp"]

            result = await mcp_module.execute("list_servers", {})
            data = result.data if hasattr(result, "data") else result

            servers = data.get("servers", [])
            server_ids = [s.get("server_id", "") for s in servers]

            assert "testtools" in server_ids, f"Expected 'testtools' in: {server_ids}"

            testtools = next(s for s in servers if s.get("server_id") == "testtools")
            assert testtools.get("status") == "connected"
            assert testtools.get("tools_count") == 3
        finally:
            await app.shutdown()
