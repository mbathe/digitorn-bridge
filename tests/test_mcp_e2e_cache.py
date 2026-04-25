"""E2E tests — MCP Smart Cache with cacheable_tools whitelist.

Covers:
- Cache hit/miss with explicit cacheable_tools
- Non-whitelisted tools bypass cache
- Cache disabled globally
- Per-server TTL override
- Cache key determinism (same params = same key)
- Cache stats (hits, misses, evictions)
- LRU eviction when max_size reached
- Cache invalidation (per-server, global)
- Integration with MCPModule._execute_mcp_tool
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from digitorn.modules.base import ActionResult, ExecutionContext
from digitorn.modules.mcp.cache import MCPToolCache, _CacheEntry
from digitorn.modules.mcp.module import MCPModule
from digitorn.modules.mcp.protocol import MCPToolResult


# ── _CacheEntry unit ────────────────────────────────────────────────────


class TestCacheEntry:
    def test_not_expired_within_ttl(self):
        entry = _CacheEntry(result=MagicMock(), created_at=time.monotonic())
        assert entry.is_expired(300.0) is False

    def test_expired_after_ttl(self):
        entry = _CacheEntry(result=MagicMock(), created_at=time.monotonic() - 400)
        assert entry.is_expired(300.0) is True

    def test_ttl_zero_always_expired(self):
        entry = _CacheEntry(result=MagicMock(), created_at=time.monotonic())
        assert entry.is_expired(0) is True

    def test_age_ms(self):
        entry = _CacheEntry(result=MagicMock(), created_at=time.monotonic() - 1.5)
        assert entry.age_ms() >= 1400  # at least 1.4 seconds


# ── MCPToolCache — cacheable_tools whitelist ────────────────────────────


class TestCacheableToolsWhitelist:
    def test_no_tools_configured_means_nothing_cacheable(self):
        cache = MCPToolCache()
        assert cache.is_cacheable("github", "list_repos") is False
        assert cache.is_cacheable("gmail", "search_emails") is False

    def test_whitelisted_tool_is_cacheable(self):
        cache = MCPToolCache()
        cache.configure_server("github", cacheable_tools=["list_repos", "get_repo"])
        assert cache.is_cacheable("github", "list_repos") is True
        assert cache.is_cacheable("github", "get_repo") is True

    def test_non_whitelisted_tool_not_cacheable(self):
        cache = MCPToolCache()
        cache.configure_server("github", cacheable_tools=["list_repos"])
        assert cache.is_cacheable("github", "create_issue") is False

    def test_different_server_not_affected(self):
        cache = MCPToolCache()
        cache.configure_server("github", cacheable_tools=["list_repos"])
        assert cache.is_cacheable("slack", "list_repos") is False

    def test_empty_whitelist_means_no_caching(self):
        cache = MCPToolCache()
        cache.configure_server("github", cacheable_tools=[])
        assert cache.is_cacheable("github", "list_repos") is False


# ── MCPToolCache — store / get / eviction ───────────────────────────────


class TestCacheStoreGet:
    def test_store_and_retrieve(self):
        cache = MCPToolCache()
        result = ActionResult(success=True, data={"repos": ["a", "b"]})
        key = cache.make_key("list_repos", {})
        cache.store("github", key, result)
        entry = cache.get("github", key)
        assert entry is not None
        assert entry.result.data == {"repos": ["a", "b"]}

    def test_miss_on_unknown_server(self):
        cache = MCPToolCache()
        assert cache.get("unknown", "abc") is None
        assert cache._misses == 1

    def test_miss_on_unknown_key(self):
        cache = MCPToolCache()
        cache.store("github", "aaa", ActionResult(success=True))
        assert cache.get("github", "bbb") is None

    def test_expired_entry_auto_removed(self):
        cache = MCPToolCache(default_ttl=0.01)
        result = ActionResult(success=True)
        cache.store("github", "key1", result)
        time.sleep(0.02)
        assert cache.get("github", "key1") is None

    def test_lru_eviction(self):
        cache = MCPToolCache(default_max_size=3)
        for i in range(4):
            cache.store("github", f"key{i}", ActionResult(success=True, data={"i": i}))
        # key0 should have been evicted
        assert cache.get("github", "key0") is None
        assert cache.get("github", "key3") is not None
        assert cache._evictions == 1

    def test_per_server_ttl(self):
        cache = MCPToolCache(default_ttl=300)
        cache.configure_server("fast", ttl=0.01)
        cache.store("fast", "k1", ActionResult(success=True))
        cache.store("slow", "k1", ActionResult(success=True))
        time.sleep(0.02)
        assert cache.get("fast", "k1") is None  # expired
        assert cache.get("slow", "k1") is not None  # still valid


# ── MCPToolCache — key determinism ──────────────────────────────────────


class TestCacheKeyDeterminism:
    def test_same_params_same_key(self):
        k1 = MCPToolCache.make_key("search", {"q": "test", "limit": 10})
        k2 = MCPToolCache.make_key("search", {"limit": 10, "q": "test"})
        assert k1 == k2  # sorted params

    def test_different_params_different_key(self):
        k1 = MCPToolCache.make_key("search", {"q": "foo"})
        k2 = MCPToolCache.make_key("search", {"q": "bar"})
        assert k1 != k2

    def test_different_tool_different_key(self):
        k1 = MCPToolCache.make_key("list_repos", {})
        k2 = MCPToolCache.make_key("get_repo", {})
        assert k1 != k2


# ── MCPToolCache — invalidation ─────────────────────────────────────────


class TestCacheInvalidation:
    def test_invalidate_server(self):
        cache = MCPToolCache()
        cache.store("github", "k1", ActionResult(success=True))
        cache.store("github", "k2", ActionResult(success=True))
        cache.store("slack", "k1", ActionResult(success=True))
        cleared = cache.invalidate_server("github")
        assert cleared == 2
        assert cache.get("github", "k1") is None
        assert cache.get("slack", "k1") is not None

    def test_invalidate_unknown_server(self):
        cache = MCPToolCache()
        assert cache.invalidate_server("nope") == 0

    def test_invalidate_all(self):
        cache = MCPToolCache()
        cache.store("github", "k1", ActionResult(success=True))
        cache.store("slack", "k2", ActionResult(success=True))
        cache.invalidate_all()
        assert cache.get("github", "k1") is None
        assert cache.get("slack", "k2") is None


# ── MCPToolCache — info/stats ───────────────────────────────────────────


class TestCacheInfo:
    def test_stats_tracking(self):
        cache = MCPToolCache()
        cache.store("github", "k1", ActionResult(success=True))
        cache.get("github", "k1")  # hit
        cache.get("github", "k2")  # miss
        info = cache.info()
        assert info["hits"] == 1
        assert info["misses"] == 1
        assert info["servers"]["github"]["entries"] == 1


# ── Integration: MCPModule + cache ──────────────────────────────────────


@pytest.fixture()
def mcp():
    m = MCPModule()
    m._server_sandbox = {"github": set(), "gmail": set()}
    return m


@pytest.fixture()
def ctx():
    return ExecutionContext(plan_id="test", action_id="test")


class TestMCPModuleCacheIntegration:
    @pytest.mark.asyncio
    async def test_whitelisted_tool_cached_on_second_call(self, mcp, ctx):
        """Whitelisted tool is cached; second call doesn't hit the server."""
        mock_result = MCPToolResult(
            content=[{"type": "text", "text": "repos data"}], is_error=False,
        )
        mcp._pool.call_tool = AsyncMock(return_value=mock_result)
        mcp._cache_enabled = True
        mcp._tool_cache.configure_server("github", cacheable_tools=["list_repos"])

        r1 = await mcp.execute("mcp_github__list_repos", {}, ctx)
        assert r1.success is True
        assert mcp._pool.call_tool.await_count == 1

        r2 = await mcp.execute("mcp_github__list_repos", {}, ctx)
        assert r2.success is True
        assert mcp._pool.call_tool.await_count == 1  # cached!
        assert r2.metadata["cache_hit"] is True

    @pytest.mark.asyncio
    async def test_non_whitelisted_tool_never_cached(self, mcp, ctx):
        """Non-whitelisted tool always hits the server."""
        mock_result = MCPToolResult(
            content=[{"type": "text", "text": "emails"}], is_error=False,
        )
        mcp._pool.call_tool = AsyncMock(return_value=mock_result)
        mcp._cache_enabled = True
        mcp._tool_cache.configure_server("gmail", cacheable_tools=["list_labels"])

        # search_emails NOT in cacheable_tools
        await mcp.execute("mcp_gmail__search_emails", {"q": "test"}, ctx)
        await mcp.execute("mcp_gmail__search_emails", {"q": "test"}, ctx)
        assert mcp._pool.call_tool.await_count == 2

    @pytest.mark.asyncio
    async def test_different_params_different_cache_entries(self, mcp, ctx):
        """Same tool with different params creates different cache entries."""
        results = [
            MCPToolResult(content=[{"type": "text", "text": "repo-a"}], is_error=False),
            MCPToolResult(content=[{"type": "text", "text": "repo-b"}], is_error=False),
        ]
        mcp._pool.call_tool = AsyncMock(side_effect=results)
        mcp._cache_enabled = True
        mcp._tool_cache.configure_server("github", cacheable_tools=["get_repo"])

        r1 = await mcp.execute("mcp_github__get_repo", {"repo": "a"}, ctx)
        r2 = await mcp.execute("mcp_github__get_repo", {"repo": "b"}, ctx)
        assert mcp._pool.call_tool.await_count == 2  # both miss (different params)
        assert r1.data["output"] != r2.data["output"]

    @pytest.mark.asyncio
    async def test_cache_disabled_globally(self, mcp, ctx):
        """When cache is disabled, even whitelisted tools hit the server."""
        mock_result = MCPToolResult(
            content=[{"type": "text", "text": "data"}], is_error=False,
        )
        mcp._pool.call_tool = AsyncMock(return_value=mock_result)
        mcp._cache_enabled = False
        mcp._tool_cache.configure_server("github", cacheable_tools=["list_repos"])

        await mcp.execute("mcp_github__list_repos", {}, ctx)
        await mcp.execute("mcp_github__list_repos", {}, ctx)
        assert mcp._pool.call_tool.await_count == 2

    @pytest.mark.asyncio
    async def test_error_results_not_cached(self, mcp, ctx):
        """Tool errors should never be cached."""
        error_result = MCPToolResult(
            content=[{"type": "text", "text": "error!"}], is_error=True,
        )
        success_result = MCPToolResult(
            content=[{"type": "text", "text": "ok"}], is_error=False,
        )
        mcp._pool.call_tool = AsyncMock(side_effect=[error_result, success_result])
        mcp._cache_enabled = True
        mcp._tool_cache.configure_server("github", cacheable_tools=["list_repos"])

        r1 = await mcp.execute("mcp_github__list_repos", {}, ctx)
        assert r1.success is False

        r2 = await mcp.execute("mcp_github__list_repos", {}, ctx)
        assert r2.success is True
        assert mcp._pool.call_tool.await_count == 2  # error not cached

    @pytest.mark.asyncio
    async def test_cache_metadata_on_miss(self, mcp, ctx):
        """First call (miss) should have cache_hit=False, cache_stored=True."""
        mock_result = MCPToolResult(
            content=[{"type": "text", "text": "data"}], is_error=False,
        )
        mcp._pool.call_tool = AsyncMock(return_value=mock_result)
        mcp._cache_enabled = True
        mcp._tool_cache.configure_server("github", cacheable_tools=["list_repos"])

        r = await mcp.execute("mcp_github__list_repos", {}, ctx)
        assert r.metadata["cache_hit"] is False
        assert r.metadata["cache_stored"] is True

    @pytest.mark.asyncio
    async def test_on_stop_clears_cache(self, mcp, ctx):
        """on_stop() clears the entire cache."""
        mock_result = MCPToolResult(
            content=[{"type": "text", "text": "data"}], is_error=False,
        )
        mcp._pool.call_tool = AsyncMock(return_value=mock_result)
        mcp._cache_enabled = True
        mcp._tool_cache.configure_server("github", cacheable_tools=["list_repos"])

        await mcp.execute("mcp_github__list_repos", {}, ctx)
        assert mcp._tool_cache.info()["servers"]["github"]["entries"] == 1

        mcp._pool.disconnect_all = AsyncMock(return_value=0)
        await mcp.on_stop()
        assert mcp._tool_cache.info()["servers"] == {}
