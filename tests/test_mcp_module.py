"""Tests — MCPModule: action dispatch, virtual tool routing, config auto-connect.

Covers:
- MCPModule metadata (MODULE_ID, VERSION, manifest)
- @action registration: all 11 actions registered
- execute() dispatches management actions (connect, disconnect, etc.)
- execute() routes virtual MCP tool calls (mcp_slack__post_message → pool.call_tool)
- _MCP_ACTION_RE regex parsing for virtual tool FQNs
- on_config_update() auto-connects declared servers
- on_stop() disconnects all servers
- Error handling: server not connected, transport errors
"""
from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from digitorn.modules.base import ActionResult, ExecutionContext
from digitorn.modules.mcp.connections import MCPServerEntry
from digitorn.modules.mcp.module import MCPModule, _MCP_ACTION_RE
from digitorn.modules.mcp.protocol import MCPPromptDef, MCPResourceDef, MCPToolDef, MCPToolResult
from digitorn.modules.mcp.transports import MCPTransportError


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture()
def mcp():
    m = MCPModule()
    # Tests use these server IDs — declare sandbox permissions so
    # the deny-by-default gate doesn't reject calls.
    m._server_sandbox = {
        "slack": set(),
        "github": set(),
        "google_calendar": set(),
        "gmail": set(),
    }
    return m


@pytest.fixture()
def ctx():
    return ExecutionContext(plan_id="test", action_id="test")


# ── Regex ────────────────────────────────────────────────────────────────


class TestMCPActionRegex:
    def test_simple_server_id(self):
        m = _MCP_ACTION_RE.match("mcp_slack__post_message")
        assert m is not None
        assert m.group(1) == "slack"
        assert m.group(2) == "post_message"

    def test_compound_server_id(self):
        m = _MCP_ACTION_RE.match("mcp_google_calendar__list_events")
        assert m is not None
        assert m.group(1) == "google_calendar"
        assert m.group(2) == "list_events"

    def test_no_match_native_action(self):
        assert _MCP_ACTION_RE.match("connect") is None
        assert _MCP_ACTION_RE.match("list_servers") is None

    def test_no_match_wrong_prefix(self):
        assert _MCP_ACTION_RE.match("http_slack__post") is None

    def test_no_match_single_underscore(self):
        # "mcp_slack_post" should NOT match (no double underscore)
        assert _MCP_ACTION_RE.match("mcp_slack_post") is None


# ── Module Metadata ──────────────────────────────────────────────────────


class TestMCPModuleMetadata:
    def test_module_id(self, mcp):
        assert mcp.MODULE_ID == "mcp"

    def test_version(self, mcp):
        assert mcp.VERSION == "1.0.0"

    def test_manifest(self, mcp):
        manifest = mcp.get_manifest()
        assert manifest.module_id == "mcp"

    def test_all_actions_registered(self, mcp):
        """All 11 management actions must be in the action registry."""
        expected = {
            "connect", "disconnect", "reconnect", "list_servers",
            "list_tools", "call_tool",
            "list_resources", "read_resource",
            "list_prompts", "get_prompt",
            "health_check",
        }
        registry = mcp._action_registry
        registered = set(registry.keys())
        assert expected.issubset(registered), f"Missing: {expected - registered}"


# ── Management Actions ───────────────────────────────────────────────────


class TestMCPModuleConnect:
    @pytest.mark.asyncio
    async def test_connect_stdio(self, mcp, ctx):
        mock_entry = MagicMock(spec=MCPServerEntry)
        mock_entry.to_dict.return_value = {"server_id": "test", "status": "connected"}
        mcp._pool.connect = AsyncMock(return_value=mock_entry)

        result = await mcp.execute("connect", {
            "server_id": "test",
            "transport": "stdio",
            "command": "npx",
            "args": ["@test/server"],
        }, ctx)

        assert isinstance(result, ActionResult)
        assert result.success is True
        assert result.data["server_id"] == "test"

    @pytest.mark.asyncio
    async def test_connect_transport_error(self, mcp, ctx):
        mcp._pool.connect = AsyncMock(side_effect=MCPTransportError("failed"))

        result = await mcp.execute("connect", {
            "server_id": "bad",
            "transport": "stdio",
            "command": "nonexistent",
        }, ctx)

        assert result.success is False
        assert "failed" in result.error


class TestMCPModuleDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect(self, mcp, ctx):
        mcp._pool.disconnect = AsyncMock()

        result = await mcp.execute("disconnect", {"server_id": "test"}, ctx)

        assert result.success is True
        assert result.data["status"] == "disconnected"


class TestMCPModuleReconnect:
    @pytest.mark.asyncio
    async def test_reconnect_success(self, mcp, ctx):
        mock_entry = MagicMock(spec=MCPServerEntry)
        mock_entry.to_dict.return_value = {"server_id": "s", "status": "connected"}
        mcp._pool.reconnect = AsyncMock(return_value=mock_entry)

        result = await mcp.execute("reconnect", {"server_id": "s"}, ctx)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_reconnect_failure(self, mcp, ctx):
        mcp._pool.reconnect = AsyncMock(side_effect=MCPTransportError("dead"))

        result = await mcp.execute("reconnect", {"server_id": "s"}, ctx)
        assert result.success is False


class TestMCPModuleListServers:
    @pytest.mark.asyncio
    async def test_list_servers(self, mcp, ctx):
        mcp._pool.list_servers = MagicMock(return_value=[
            {"server_id": "a", "status": "connected"},
            {"server_id": "b", "status": "error"},
        ])

        result = await mcp.execute("list_servers", {}, ctx)
        assert result.success is True
        assert result.data["count"] == 2


class TestMCPModuleListTools:
    @pytest.mark.asyncio
    async def test_list_tools(self, mcp, ctx):
        entry = MagicMock()
        entry.tools = [
            MCPToolDef(name="post", description="Post msg", input_schema={"type": "object"}),
        ]
        mcp._pool.get_server = MagicMock(return_value=entry)

        result = await mcp.execute("list_tools", {"server_id": "slack"}, ctx)
        assert result.success is True
        assert len(result.data["tools"]) == 1

    @pytest.mark.asyncio
    async def test_list_tools_not_connected(self, mcp, ctx):
        mcp._pool.get_server = MagicMock(return_value=None)

        result = await mcp.execute("list_tools", {"server_id": "ghost"}, ctx)
        assert result.success is False
        assert "not connected" in result.error.lower()


class TestMCPModuleCallTool:
    @pytest.mark.asyncio
    async def test_call_tool_success(self, mcp, ctx):
        mock_result = MCPToolResult(
            content=[{"type": "text", "text": "done"}],
            is_error=False,
        )
        mcp._pool.call_tool = AsyncMock(return_value=mock_result)

        result = await mcp.execute("call_tool", {
            "server_id": "slack",
            "tool_name": "post_message",
            "arguments": {"text": "hi"},
        }, ctx)

        assert result.success is True
        assert "done" in result.data["output"]

    @pytest.mark.asyncio
    async def test_call_tool_error_result(self, mcp, ctx):
        mock_result = MCPToolResult(
            content=[{"type": "text", "text": "rate limited"}],
            is_error=True,
        )
        mcp._pool.call_tool = AsyncMock(return_value=mock_result)

        result = await mcp.execute("call_tool", {
            "server_id": "slack",
            "tool_name": "post_message",
            "arguments": {},
        }, ctx)

        assert result.success is False

    @pytest.mark.asyncio
    async def test_call_tool_transport_error(self, mcp, ctx):
        mcp._pool.call_tool = AsyncMock(side_effect=MCPTransportError("timeout"))

        result = await mcp.execute("call_tool", {
            "server_id": "slack",
            "tool_name": "post_message",
            "arguments": {},
        }, ctx)

        assert result.success is False
        assert "timeout" in result.error


class TestMCPModuleResources:
    @pytest.mark.asyncio
    async def test_list_resources(self, mcp, ctx):
        mcp._pool.list_resources = AsyncMock(return_value=[
            MCPResourceDef(uri="file:///a", name="a.txt", description="A", mime_type="text/plain"),
        ])

        result = await mcp.execute("list_resources", {"server_id": "s"}, ctx)
        assert result.success is True
        assert len(result.data["resources"]) == 1

    @pytest.mark.asyncio
    async def test_read_resource(self, mcp, ctx):
        mcp._pool.read_resource = AsyncMock(return_value={"contents": [{"text": "data"}]})

        result = await mcp.execute("read_resource", {
            "server_id": "s",
            "uri": "file:///test",
        }, ctx)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_list_resources_error(self, mcp, ctx):
        mcp._pool.list_resources = AsyncMock(side_effect=MCPTransportError("gone"))

        result = await mcp.execute("list_resources", {"server_id": "s"}, ctx)
        assert result.success is False


class TestMCPModulePrompts:
    @pytest.mark.asyncio
    async def test_list_prompts(self, mcp, ctx):
        mcp._pool.list_prompts = AsyncMock(return_value=[
            MCPPromptDef(name="greet", description="Greeting"),
        ])

        result = await mcp.execute("list_prompts", {"server_id": "s"}, ctx)
        assert result.success is True
        assert len(result.data["prompts"]) == 1

    @pytest.mark.asyncio
    async def test_get_prompt(self, mcp, ctx):
        mcp._pool.get_prompt = AsyncMock(return_value={"messages": []})

        result = await mcp.execute("get_prompt", {
            "server_id": "s",
            "prompt_name": "hello",
            "arguments": {"name": "world"},
        }, ctx)
        assert result.success is True


class TestMCPModuleHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_single(self, mcp, ctx):
        mcp._pool.ping = AsyncMock(return_value=True)
        entry = MagicMock()
        entry.status = "connected"
        mcp._pool.get_server = MagicMock(return_value=entry)

        result = await mcp.execute("health_check", {"server_id": "s"}, ctx)
        assert result.success is True
        assert result.data["alive"] is True

    @pytest.mark.asyncio
    async def test_health_check_all(self, mcp, ctx):
        entry1 = MagicMock()
        entry1.server_id = "a"
        entry1.status = "connected"
        entry2 = MagicMock()
        entry2.server_id = "b"
        entry2.status = "error"

        mcp._pool._servers = {"a": entry1, "b": entry2}
        mcp._pool.ping = AsyncMock(side_effect=[True, False])

        result = await mcp.execute("health_check", {}, ctx)
        assert result.success is True
        assert result.data["servers"]["a"]["alive"] is True
        assert result.data["servers"]["b"]["alive"] is False


# ── Virtual Tool Routing ─────────────────────────────────────────────────


class TestVirtualToolRouting:
    @pytest.mark.asyncio
    async def test_execute_routes_virtual_tool(self, mcp, ctx):
        """mcp_slack__post_message should route to pool.call_tool('slack', 'post_message')."""
        mock_result = MCPToolResult(
            content=[{"type": "text", "text": "sent"}],
            is_error=False,
        )
        mcp._pool.call_tool = AsyncMock(return_value=mock_result)

        result = await mcp.execute("mcp_slack__post_message", {"text": "hi"}, ctx)

        assert result.success is True
        mcp._pool.call_tool.assert_awaited_once_with("slack", "post_message", {"text": "hi"})

    @pytest.mark.asyncio
    async def test_execute_routes_compound_server_id(self, mcp, ctx):
        mock_result = MCPToolResult(content=[{"type": "text", "text": "ok"}], is_error=False)
        mcp._pool.call_tool = AsyncMock(return_value=mock_result)

        result = await mcp.execute("mcp_google_calendar__list_events", {}, ctx)

        assert result.success is True
        mcp._pool.call_tool.assert_awaited_once_with("google_calendar", "list_events", {})

    @pytest.mark.asyncio
    async def test_execute_virtual_tool_error(self, mcp, ctx):
        mock_result = MCPToolResult(
            content=[{"type": "text", "text": "error"}],
            is_error=True,
        )
        mcp._pool.call_tool = AsyncMock(return_value=mock_result)

        result = await mcp.execute("mcp_slack__bad_tool", {}, ctx)
        assert result.success is False

    @pytest.mark.asyncio
    async def test_execute_virtual_tool_transport_error(self, mcp, ctx):
        mcp._pool.call_tool = AsyncMock(side_effect=MCPTransportError("connection lost"))

        result = await mcp.execute("mcp_slack__post_message", {}, ctx)
        assert result.success is False
        assert "connection lost" in result.error


# ── Lifecycle ────────────────────────────────────────────────────────────


class TestMCPModuleLifecycle:
    @pytest.mark.asyncio
    async def test_on_stop_disconnects_all(self, mcp):
        mcp._pool.disconnect_all = AsyncMock(return_value=3)
        await mcp.on_stop()
        mcp._pool.disconnect_all.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_on_config_update_auto_connects(self, mcp):
        mock_entry = MagicMock(spec=MCPServerEntry)
        mcp._pool.connect = AsyncMock(return_value=mock_entry)
        mcp._pool.get_server = MagicMock(return_value=None)

        config = {
            "servers": {
                "slack": {
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["@anthropic/mcp-server-slack"],
                    "env": {"SLACK_TOKEN": "xoxb-test"},
                },
                "api": {
                    "transport": "sse",
                    "url": "http://localhost:3000/sse",
                },
            }
        }
        await mcp.on_config_update(config)

        assert mcp._pool.connect.await_count == 2

    @pytest.mark.asyncio
    async def test_on_config_update_skips_existing(self, mcp):
        existing = MagicMock()
        mcp._pool.get_server = MagicMock(return_value=existing)
        mcp._pool.connect = AsyncMock()

        await mcp.on_config_update({"servers": {"s": {"transport": "stdio", "command": "echo"}}})

        mcp._pool.connect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_on_config_update_handles_failure(self, mcp):
        """If one server fails to connect, others should still succeed."""
        call_count = 0

        async def side_effect(server_id, transport_type, **kwargs):
            nonlocal call_count
            call_count += 1
            if server_id == "bad":
                raise MCPTransportError("fail")
            return MagicMock(spec=MCPServerEntry)

        mcp._pool.connect = AsyncMock(side_effect=side_effect)
        mcp._pool.get_server = MagicMock(return_value=None)

        config = {
            "servers": {
                "good": {"transport": "stdio", "command": "echo"},
                "bad": {"transport": "stdio", "command": "bad"},
            }
        }
        await mcp.on_config_update(config)
        # Both attempted, one failed gracefully


# ── Smart Cache integration ──────────────────────────────────────────


class TestMCPSmartCache:
    """Test cache actually works in the full _execute_mcp_tool flow."""

    @pytest.mark.asyncio
    async def test_cache_hit_avoids_server_call(self, mcp, ctx):
        """Second call to a whitelisted tool returns cached result (no server call)."""
        mock_result = MCPToolResult(
            content=[{"type": "text", "text": "cached data"}],
            is_error=False,
        )
        mcp._pool.call_tool = AsyncMock(return_value=mock_result)
        mcp._cache_enabled = True
        mcp._tool_cache.configure_server("github", cacheable_tools=["list_repos"])

        # First call — cache miss, hits server
        r1 = await mcp.execute("mcp_github__list_repos", {}, ctx)
        assert r1.success is True
        assert mcp._pool.call_tool.await_count == 1

        # Second call — cache hit, server NOT called again
        r2 = await mcp.execute("mcp_github__list_repos", {}, ctx)
        assert r2.success is True
        assert mcp._pool.call_tool.await_count == 1  # still 1!
        assert r2.data["output"] == r1.data["output"]
        assert r2.metadata.get("cache_hit") is True

    @pytest.mark.asyncio
    async def test_non_whitelisted_tool_not_cached(self, mcp, ctx):
        """A tool NOT in cacheable_tools always hits the server."""
        mock_result = MCPToolResult(
            content=[{"type": "text", "text": "repos"}],
            is_error=False,
        )
        mcp._pool.call_tool = AsyncMock(return_value=mock_result)
        mcp._cache_enabled = True
        mcp._tool_cache.configure_server("github", cacheable_tools=["get_repo"])

        # list_repos is NOT in cacheable_tools — never cached
        await mcp.execute("mcp_github__list_repos", {}, ctx)
        await mcp.execute("mcp_github__list_repos", {}, ctx)
        assert mcp._pool.call_tool.await_count == 2  # both hit server

    @pytest.mark.asyncio
    async def test_no_cacheable_tools_means_no_cache(self, mcp, ctx):
        """Without cacheable_tools configured, nothing is cached."""
        mock_result = MCPToolResult(
            content=[{"type": "text", "text": "data"}],
            is_error=False,
        )
        mcp._pool.call_tool = AsyncMock(return_value=mock_result)
        mcp._cache_enabled = True
        # No cacheable_tools configured for github

        await mcp.execute("mcp_github__list_repos", {}, ctx)
        await mcp.execute("mcp_github__list_repos", {}, ctx)
        assert mcp._pool.call_tool.await_count == 2  # both hit server

    @pytest.mark.asyncio
    async def test_cache_disabled(self, mcp, ctx):
        """When cache is disabled globally, every call hits the server."""
        mock_result = MCPToolResult(
            content=[{"type": "text", "text": "data"}],
            is_error=False,
        )
        mcp._pool.call_tool = AsyncMock(return_value=mock_result)
        mcp._cache_enabled = False
        mcp._tool_cache.configure_server("github", cacheable_tools=["list_repos"])

        await mcp.execute("mcp_github__list_repos", {}, ctx)
        await mcp.execute("mcp_github__list_repos", {}, ctx)
        assert mcp._pool.call_tool.await_count == 2  # both hit server


# ── MCP Middleware Pipeline integration ──────────────────────────────


class TestMCPMiddlewarePipeline:
    """Test middleware pipeline is wired and executes in _execute_mcp_tool."""

    @pytest.mark.asyncio
    async def test_global_pipeline_wraps_call(self, mcp, ctx):
        """Global middleware pipeline wraps MCP calls."""
        from digitorn.modules.mcp.middleware import AuditMiddleware, MCPPipeline

        audit = AuditMiddleware(log_params=True)
        mcp._global_pipeline = MCPPipeline([audit])

        mock_result = MCPToolResult(
            content=[{"type": "text", "text": "audited"}],
            is_error=False,
        )
        mcp._pool.call_tool = AsyncMock(return_value=mock_result)

        result = await mcp.execute("mcp_slack__send", {"text": "hi"}, ctx)
        assert result.success is True
        assert audit.stats["total_calls"] == 1
        assert audit.stats["total_errors"] == 0

    @pytest.mark.asyncio
    async def test_server_pipeline_overrides_global(self, mcp, ctx):
        """Per-server pipeline takes priority over global."""
        from digitorn.modules.mcp.middleware import AuditMiddleware, MCPPipeline

        global_audit = AuditMiddleware()
        server_audit = AuditMiddleware()
        mcp._global_pipeline = MCPPipeline([global_audit])
        mcp._server_pipelines["slack"] = MCPPipeline([server_audit])

        mock_result = MCPToolResult(
            content=[{"type": "text", "text": "ok"}],
            is_error=False,
        )
        mcp._pool.call_tool = AsyncMock(return_value=mock_result)

        await mcp.execute("mcp_slack__send", {}, ctx)
        assert server_audit.stats["total_calls"] == 1
        assert global_audit.stats["total_calls"] == 0  # NOT used

    @pytest.mark.asyncio
    async def test_budget_exceeded_returns_error(self, mcp, ctx):
        """BudgetExceededError is caught and returned as ActionResult."""
        from digitorn.modules.mcp.middleware import BudgetMiddleware, MCPPipeline

        budget = BudgetMiddleware(max_calls_per_hour=1)
        mcp._global_pipeline = MCPPipeline([budget])
        mcp._cache_enabled = False  # disable cache so budget is hit

        mock_result = MCPToolResult(
            content=[{"type": "text", "text": "ok"}],
            is_error=False,
        )
        mcp._pool.call_tool = AsyncMock(return_value=mock_result)

        # First call — ok
        r1 = await mcp.execute("mcp_github__search", {}, ctx)
        assert r1.success is True

        # Second call — budget exceeded
        r2 = await mcp.execute("mcp_github__search", {}, ctx)
        assert r2.success is False
        assert "budget exceeded" in r2.error.lower()

    @pytest.mark.asyncio
    async def test_config_parses_middleware(self, mcp):
        """on_config_update parses middleware config from YAML."""
        mcp._pool.get_server = MagicMock(return_value=None)
        mcp._pool.connect = AsyncMock(return_value=MagicMock(spec=MCPServerEntry))

        config = {
            "middleware": [
                {"retry": {"max_attempts": 5}},
                {"timeout": {"seconds": 10}},
            ],
            "servers": {},
        }
        await mcp.on_config_update(config)

        assert mcp._global_pipeline is not None
        assert len(mcp._global_pipeline.middlewares) == 2


# ── Circuit Breaker integration ──────────────────────────────────────


class TestMCPCircuitBreaker:
    """Circuit breaker through real MCPModule._execute_mcp_tool flow."""

    @pytest.mark.asyncio
    async def test_circuit_opens_on_repeated_failures(self, mcp, ctx):
        """After N failures, circuit opens → immediate error (no server call)."""
        from digitorn.modules.mcp.middleware import CircuitBreakerMiddleware, MCPPipeline

        cb = CircuitBreakerMiddleware(failure_threshold=2, recovery_timeout=100)
        mcp._global_pipeline = MCPPipeline([cb])
        mcp._cache_scope = "disabled"

        call_count = 0
        original_call = mcp._pool.call_tool

        async def failing_call(sid, tn, params):
            nonlocal call_count
            call_count += 1
            raise MCPTransportError("server down")

        mcp._pool.call_tool = AsyncMock(side_effect=failing_call)

        # 1st failure → circuit stays closed
        r1 = await mcp.execute("mcp_github__search", {}, ctx)
        assert r1.success is False
        assert call_count == 1

        # 2nd failure → circuit opens
        r2 = await mcp.execute("mcp_github__search", {}, ctx)
        assert r2.success is False
        assert call_count == 2

        # 3rd call → circuit OPEN → blocked immediately, no server call
        r3 = await mcp.execute("mcp_github__search", {}, ctx)
        assert r3.success is False
        assert "circuit breaker" in r3.error.lower() or "Circuit" in r3.error
        assert call_count == 2  # server NOT called a 3rd time

    @pytest.mark.asyncio
    async def test_circuit_resets_on_success(self, mcp, ctx):
        """Successful calls reset the failure counter."""
        from digitorn.modules.mcp.middleware import CircuitBreakerMiddleware, MCPPipeline

        cb = CircuitBreakerMiddleware(failure_threshold=3)
        mcp._global_pipeline = MCPPipeline([cb])
        mcp._cache_scope = "disabled"

        ok_result = MCPToolResult(content=[{"type": "text", "text": "ok"}], is_error=False)
        mcp._pool.call_tool = AsyncMock(return_value=ok_result)

        # 3 successful calls → circuit stays closed
        for _ in range(5):
            r = await mcp.execute("mcp_github__list_repos", {}, ctx)
            assert r.success is True
        assert cb.get_state("github") == "closed"


# ── Deduplication integration ────────────────────────────────────────


class TestMCPDeduplication:
    """Dedup through real MCPModule._execute_mcp_tool flow."""

    @pytest.mark.asyncio
    async def test_duplicate_call_returns_cached(self, mcp, ctx):
        """Same tool+params within window → server called once."""
        from digitorn.modules.mcp.middleware import DeduplicationMiddleware, MCPPipeline

        dedup = DeduplicationMiddleware(window_seconds=5.0)
        mcp._global_pipeline = MCPPipeline([dedup])
        mcp._cache_scope = "disabled"

        mock_result = MCPToolResult(
            content=[{"type": "text", "text": "search results"}],
            is_error=False,
        )
        mcp._pool.call_tool = AsyncMock(return_value=mock_result)

        r1 = await mcp.execute("mcp_github__search_repos", {"q": "test"}, ctx)
        r2 = await mcp.execute("mcp_github__search_repos", {"q": "test"}, ctx)

        assert r1.success is True
        assert r2.success is True
        # Server only called ONCE — second was deduped
        assert mcp._pool.call_tool.await_count == 1
        assert dedup.stats["deduplicated_calls"] == 1

    @pytest.mark.asyncio
    async def test_different_params_not_deduped(self, mcp, ctx):
        """Different params → both calls go to server."""
        from digitorn.modules.mcp.middleware import DeduplicationMiddleware, MCPPipeline

        dedup = DeduplicationMiddleware(window_seconds=5.0)
        mcp._global_pipeline = MCPPipeline([dedup])
        mcp._cache_scope = "disabled"

        mock_result = MCPToolResult(
            content=[{"type": "text", "text": "data"}],
            is_error=False,
        )
        mcp._pool.call_tool = AsyncMock(return_value=mock_result)

        await mcp.execute("mcp_github__search_repos", {"q": "aaa"}, ctx)
        await mcp.execute("mcp_github__search_repos", {"q": "bbb"}, ctx)

        assert mcp._pool.call_tool.await_count == 2


# ── Streaming integration ───────────────────────────────────────────


class TestMCPStreaming:
    """Streaming middleware through real MCPModule._execute_mcp_tool flow."""

    @pytest.mark.asyncio
    async def test_fast_call_no_slow_flag(self, mcp, ctx):
        """Fast call → streaming middleware doesn't mark as slow."""
        from digitorn.modules.mcp.middleware import MCPPipeline, StreamingMiddleware

        streaming = StreamingMiddleware(slow_threshold=5.0)
        mcp._global_pipeline = MCPPipeline([streaming])
        mcp._cache_scope = "disabled"

        mock_result = MCPToolResult(
            content=[{"type": "text", "text": "fast"}],
            is_error=False,
        )
        mcp._pool.call_tool = AsyncMock(return_value=mock_result)

        r = await mcp.execute("mcp_github__search", {}, ctx)
        assert r.success is True
        assert streaming.stats["slow_calls"] == 0

    @pytest.mark.asyncio
    async def test_slow_call_detected(self, mcp, ctx):
        """Slow call → streaming middleware increments slow counter."""
        import asyncio
        from digitorn.modules.mcp.middleware import MCPPipeline, StreamingMiddleware

        streaming = StreamingMiddleware(slow_threshold=0.05)
        mcp._global_pipeline = MCPPipeline([streaming])
        mcp._cache_scope = "disabled"

        async def slow_server(sid, tn, params):
            await asyncio.sleep(0.1)
            return MCPToolResult(
                content=[{"type": "text", "text": "slow result"}],
                is_error=False,
            )

        mcp._pool.call_tool = AsyncMock(side_effect=slow_server)

        r = await mcp.execute("mcp_github__search", {}, ctx)
        assert r.success is True
        assert streaming.stats["slow_calls"] == 1


# ── All new middlewares via YAML config ──────────────────────────────


class TestMCPNewMiddlewareConfig:
    """All 4 new middlewares are parseable from YAML config."""

    @pytest.mark.asyncio
    async def test_all_new_middlewares_from_config(self, mcp):
        """on_config_update builds pipeline with all new middlewares."""
        mcp._pool.get_server = MagicMock(return_value=None)

        config = {
            "middleware": [
                {"circuit_breaker": {"failure_threshold": 5}},
                {"dedup": {"window_seconds": 10}},
                {"streaming": {"slow_threshold": 3.0}},
            ],
            "servers": {},
        }
        await mcp.on_config_update(config)

        assert mcp._global_pipeline is not None
        assert len(mcp._global_pipeline.middlewares) == 3

        from digitorn.modules.mcp.middleware import (
            CircuitBreakerMiddleware,
            DeduplicationMiddleware,
            StreamingMiddleware,
        )
        assert isinstance(mcp._global_pipeline.middlewares[0], CircuitBreakerMiddleware)
        assert mcp._global_pipeline.middlewares[0].failure_threshold == 5
        assert isinstance(mcp._global_pipeline.middlewares[1], DeduplicationMiddleware)
        assert isinstance(mcp._global_pipeline.middlewares[2], StreamingMiddleware)
