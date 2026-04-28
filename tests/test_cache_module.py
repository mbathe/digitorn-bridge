"""Tests for the Cache module - MemoryCacheBackend and CacheModule actions.

Covers:
    - MemoryCacheBackend: set/get, TTL, LRU eviction, delete, exists,
      ttl_remaining, increment/decrement, tag indexing, list_keys,
      get_stats, clear, close
    - CacheModule: all actions via direct instantiation (memory backend)
    - Namespace isolation, bulk operations, state snapshot/restore
"""

from __future__ import annotations

import time

import pytest

from digitorn.modules.cache.backends import CacheStats, MemoryCacheBackend
from digitorn.modules.cache.module import CacheModule
from digitorn.modules.cache.params import (
    BulkGetParams,
    BulkSetParams,
    ClearParams,
    DecrementParams,
    DeleteByTagsParams,
    DeleteParams,
    ExistsParams,
    GetOrSetParams,
    GetParams,
    IncrementParams,
    ListKeysParams,
    SetParams,
    StatsParams,
    TtlParams,
)


# ═══════════════════════════════════════════════════════════════════════
# MemoryCacheBackend - Unit Tests
# ═══════════════════════════════════════════════════════════════════════


class TestMemoryCacheBackend:
    """Test in-memory LRU cache backend."""

    @pytest.fixture
    def backend(self) -> MemoryCacheBackend:
        return MemoryCacheBackend(max_size=100)

    def test_set_and_get(self, backend: MemoryCacheBackend):
        backend.set("k1", "hello")
        assert backend.get("k1") == "hello"

    def test_get_miss_returns_none(self, backend: MemoryCacheBackend):
        assert backend.get("nonexistent") is None

    def test_set_with_ttl_expires(self, backend: MemoryCacheBackend):
        backend.set("k1", "val", ttl=0.05)
        assert backend.get("k1") == "val"
        time.sleep(0.06)
        assert backend.get("k1") is None

    def test_lru_eviction(self):
        b = MemoryCacheBackend(max_size=3)
        b.set("a", 1)
        b.set("b", 2)
        b.set("c", 3)
        # "a" is the oldest; adding a 4th key should evict it
        b.set("d", 4)
        assert b.get("a") is None
        assert b.get("b") == 2
        assert b.get("d") == 4
        assert b.get_stats().evictions == 1

    def test_lru_access_reorders(self):
        b = MemoryCacheBackend(max_size=3)
        b.set("a", 1)
        b.set("b", 2)
        b.set("c", 3)
        # Access "a" to move it to the end (most recent)
        b.get("a")
        b.set("d", 4)
        # "b" should be evicted (oldest after "a" was accessed)
        assert b.get("b") is None
        assert b.get("a") == 1

    def test_delete(self, backend: MemoryCacheBackend):
        backend.set("k1", "val")
        assert backend.delete("k1") is True
        assert backend.get("k1") is None
        assert backend.delete("k1") is False

    def test_exists(self, backend: MemoryCacheBackend):
        assert backend.exists("k1") is False
        backend.set("k1", "val")
        assert backend.exists("k1") is True

    def test_exists_expired(self, backend: MemoryCacheBackend):
        backend.set("k1", "val", ttl=0.01)
        time.sleep(0.02)
        assert backend.exists("k1") is False

    def test_ttl_remaining_with_ttl(self, backend: MemoryCacheBackend):
        backend.set("k1", "val", ttl=10.0)
        remaining = backend.ttl_remaining("k1")
        assert remaining is not None
        assert 0.0 < remaining <= 10.0

    def test_ttl_remaining_no_ttl(self, backend: MemoryCacheBackend):
        backend.set("k1", "val")
        assert backend.ttl_remaining("k1") == -1.0

    def test_ttl_remaining_missing_key(self, backend: MemoryCacheBackend):
        assert backend.ttl_remaining("nope") is None

    def test_increment(self, backend: MemoryCacheBackend):
        assert backend.increment("counter") == 1
        assert backend.increment("counter") == 2
        assert backend.increment("counter", 5) == 7

    def test_decrement_via_increment(self, backend: MemoryCacheBackend):
        backend.set("counter", 10)
        assert backend.increment("counter", -3) == 7

    def test_increment_on_expired_key(self, backend: MemoryCacheBackend):
        backend.set("counter", 100, ttl=0.01)
        time.sleep(0.02)
        # Should re-initialize
        assert backend.increment("counter", 5) == 5

    def test_tag_indexing_and_delete_by_tags(self, backend: MemoryCacheBackend):
        backend.set("user:1", "alice", tags=["user", "active"])
        backend.set("user:2", "bob", tags=["user", "inactive"])
        backend.set("post:1", "hello", tags=["post", "active"])

        deleted = backend.delete_by_tags(["inactive"])
        assert deleted == 1
        assert backend.get("user:2") is None
        assert backend.get("user:1") == "alice"

    def test_delete_by_tags_multiple(self, backend: MemoryCacheBackend):
        backend.set("a", 1, tags=["x"])
        backend.set("b", 2, tags=["y"])
        backend.set("c", 3, tags=["x", "y"])

        deleted = backend.delete_by_tags(["x"])
        assert deleted == 2  # a and c
        assert backend.get("a") is None
        assert backend.get("b") == 2
        assert backend.get("c") is None

    def test_list_keys_glob(self, backend: MemoryCacheBackend):
        backend.set("user:1", "a")
        backend.set("user:2", "b")
        backend.set("post:1", "c")
        keys = backend.list_keys("user:*")
        assert sorted(keys) == ["user:1", "user:2"]

    def test_list_keys_all(self, backend: MemoryCacheBackend):
        backend.set("a", 1)
        backend.set("b", 2)
        keys = backend.list_keys("*")
        assert len(keys) == 2

    def test_list_keys_limit(self, backend: MemoryCacheBackend):
        for i in range(10):
            backend.set(f"k{i}", i)
        keys = backend.list_keys("*", limit=3)
        assert len(keys) == 3

    def test_get_stats(self, backend: MemoryCacheBackend):
        backend.set("k1", "v1")
        backend.get("k1")       # hit
        backend.get("k1")       # hit
        backend.get("missing")  # miss
        stats = backend.get_stats()
        assert stats.hits == 2
        assert stats.misses == 1
        assert stats.sets == 1
        assert stats.total_keys == 1
        assert stats.hit_rate == pytest.approx(2 / 3, abs=0.01)

    def test_get_stats_evictions(self):
        b = MemoryCacheBackend(max_size=2)
        b.set("a", 1)
        b.set("b", 2)
        b.set("c", 3)  # evicts "a"
        assert b.get_stats().evictions == 1

    def test_clear(self, backend: MemoryCacheBackend):
        backend.set("a", 1)
        backend.set("b", 2)
        count = backend.clear()
        assert count == 2
        assert backend.get("a") is None
        assert backend.list_keys("*") == []

    def test_close(self, backend: MemoryCacheBackend):
        backend.set("a", 1)
        backend.close()
        assert backend.get("a") is None

    def test_overwrite_key(self, backend: MemoryCacheBackend):
        backend.set("k", "old")
        backend.set("k", "new")
        assert backend.get("k") == "new"

    def test_json_types(self, backend: MemoryCacheBackend):
        backend.set("dict", {"a": 1, "b": [2, 3]})
        backend.set("list", [1, 2, 3])
        backend.set("num", 42)
        backend.set("bool", True)
        assert backend.get("dict") == {"a": 1, "b": [2, 3]}
        assert backend.get("list") == [1, 2, 3]
        assert backend.get("num") == 42
        assert backend.get("bool") is True


# ═══════════════════════════════════════════════════════════════════════
# CacheStats
# ═══════════════════════════════════════════════════════════════════════


class TestCacheStats:
    def test_hit_rate_zero(self):
        s = CacheStats()
        assert s.hit_rate == 0.0

    def test_hit_rate(self):
        s = CacheStats(hits=3, misses=1)
        assert s.hit_rate == pytest.approx(0.75)

    def test_to_dict(self):
        s = CacheStats(hits=10, misses=5, sets=15, deletes=2, evictions=1, total_keys=13)
        d = s.to_dict()
        assert d["hits"] == 10
        assert d["misses"] == 5
        assert d["hit_rate"] == pytest.approx(10 / 15, abs=0.001)


# ═══════════════════════════════════════════════════════════════════════
# CacheModule - Action Tests
# ═══════════════════════════════════════════════════════════════════════


class TestCacheModule:
    """Test CacheModule actions with in-memory backend."""

    @pytest.fixture
    async def mod(self) -> CacheModule:
        m = CacheModule()
        m._config = {"backend_url": None, "max_size": 100, "default_ttl": 60.0}
        await m.on_start()
        yield m
        await m.on_stop()

    @pytest.mark.asyncio
    async def test_set_and_get(self, mod: CacheModule):
        result = await mod.set(SetParams(key="k1", value="hello"))
        assert result.success is True
        assert result.data["key"] == "k1"
        assert result.data["ttl"] == 60.0  # default TTL

        result = await mod.get(GetParams(key="k1"))
        assert result.success is True
        assert result.data["found"] is True
        assert result.data["value"] == "hello"

    @pytest.mark.asyncio
    async def test_set_with_explicit_ttl(self, mod: CacheModule):
        result = await mod.set(SetParams(key="k1", value="val", ttl=120.0))
        assert result.data["ttl"] == 120.0

    @pytest.mark.asyncio
    async def test_set_with_tags(self, mod: CacheModule):
        result = await mod.set(SetParams(key="k1", value="val", tags=["t1", "t2"]))
        assert result.data["tags"] == ["t1", "t2"]

    @pytest.mark.asyncio
    async def test_get_miss(self, mod: CacheModule):
        result = await mod.get(GetParams(key="nonexistent"))
        assert result.success is True
        assert result.data["found"] is False
        assert result.data["value"] is None

    @pytest.mark.asyncio
    async def test_get_or_set_cache_hit(self, mod: CacheModule):
        await mod.set(SetParams(key="k1", value="cached"))
        result = await mod.get_or_set(GetOrSetParams(
            key="k1", tool_name="dummy.action", tool_params={}
        ))
        assert result.success is True
        assert result.data["cache_hit"] is True
        assert result.data["value"] == "cached"

    @pytest.mark.asyncio
    async def test_get_or_set_no_service_bus(self, mod: CacheModule):
        result = await mod.get_or_set(GetOrSetParams(
            key="miss", tool_name="module.action", tool_params={}
        ))
        assert result.success is False
        assert "Service bus" in result.error

    @pytest.mark.asyncio
    async def test_get_or_set_invalid_tool_name(self, mod: CacheModule):
        result = await mod.get_or_set(GetOrSetParams(
            key="miss", tool_name="no_dot", tool_params={}
        ))
        assert result.success is False
        assert "module.action" in result.error

    @pytest.mark.asyncio
    async def test_delete(self, mod: CacheModule):
        await mod.set(SetParams(key="k1", value="val"))
        result = await mod.delete(DeleteParams(key="k1"))
        assert result.success is True
        assert result.data["deleted"] is True

        result = await mod.delete(DeleteParams(key="k1"))
        assert result.data["deleted"] is False

    @pytest.mark.asyncio
    async def test_delete_by_tags(self, mod: CacheModule):
        await mod.set(SetParams(key="a", value=1, tags=["grp"]))
        await mod.set(SetParams(key="b", value=2, tags=["grp"]))
        await mod.set(SetParams(key="c", value=3, tags=["other"]))

        result = await mod.delete_by_tags(DeleteByTagsParams(tags=["grp"]))
        assert result.success is True
        assert result.data["deleted_count"] == 2

        # c should still exist
        r = await mod.get(GetParams(key="c"))
        assert r.data["found"] is True

    @pytest.mark.asyncio
    async def test_exists(self, mod: CacheModule):
        result = await mod.exists(ExistsParams(key="k1"))
        assert result.data["exists"] is False

        await mod.set(SetParams(key="k1", value="val"))
        result = await mod.exists(ExistsParams(key="k1"))
        assert result.data["exists"] is True

    @pytest.mark.asyncio
    async def test_ttl(self, mod: CacheModule):
        await mod.set(SetParams(key="k1", value="val", ttl=30.0))
        result = await mod.ttl(TtlParams(key="k1"))
        assert result.success is True
        assert result.data["has_expiry"] is True
        assert 0 < result.data["ttl_remaining"] <= 30.0

    @pytest.mark.asyncio
    async def test_ttl_no_expiry(self, mod: CacheModule):
        # Set with no TTL override - uses default_ttl=60, so has_expiry=True
        # For truly no expiry, we poke the backend directly
        mod._ensure_backend().set("raw", "val")
        result = await mod.ttl(TtlParams(key="raw"))
        assert result.data["has_expiry"] is False
        assert result.data["ttl_remaining"] == -1.0

    @pytest.mark.asyncio
    async def test_ttl_missing_key(self, mod: CacheModule):
        result = await mod.ttl(TtlParams(key="missing"))
        assert result.data["ttl_remaining"] is None
        assert result.data["has_expiry"] is False

    @pytest.mark.asyncio
    async def test_increment(self, mod: CacheModule):
        result = await mod.increment(IncrementParams(key="cnt"))
        assert result.data["value"] == 1

        result = await mod.increment(IncrementParams(key="cnt", amount=5))
        assert result.data["value"] == 6

    @pytest.mark.asyncio
    async def test_decrement(self, mod: CacheModule):
        await mod.increment(IncrementParams(key="cnt", amount=10))
        result = await mod.decrement(DecrementParams(key="cnt", amount=3))
        assert result.data["value"] == 7

    @pytest.mark.asyncio
    async def test_list_keys(self, mod: CacheModule):
        await mod.set(SetParams(key="user:1", value="a"))
        await mod.set(SetParams(key="user:2", value="b"))
        await mod.set(SetParams(key="post:1", value="c"))

        result = await mod.list_keys(ListKeysParams(pattern="user:*"))
        assert result.success is True
        assert sorted(result.data["keys"]) == ["user:1", "user:2"]
        assert result.data["count"] == 2

    @pytest.mark.asyncio
    async def test_stats(self, mod: CacheModule):
        await mod.set(SetParams(key="k1", value="v"))
        await mod.get(GetParams(key="k1"))
        await mod.get(GetParams(key="miss"))

        result = await mod.stats(StatsParams())
        assert result.success is True
        assert result.data["hits"] >= 1
        assert result.data["misses"] >= 1
        assert "hit_rate" in result.data

    @pytest.mark.asyncio
    async def test_clear(self, mod: CacheModule):
        await mod.set(SetParams(key="a", value=1))
        await mod.set(SetParams(key="b", value=2))

        result = await mod.clear(ClearParams())
        assert result.success is True
        assert result.data["cleared"] == 2

        r = await mod.get(GetParams(key="a"))
        assert r.data["found"] is False

    @pytest.mark.asyncio
    async def test_bulk_get(self, mod: CacheModule):
        await mod.set(SetParams(key="a", value=1))
        await mod.set(SetParams(key="b", value=2))

        result = await mod.bulk_get(BulkGetParams(keys=["a", "b", "c"]))
        assert result.success is True
        assert result.data["found_count"] == 2
        assert result.data["missing_count"] == 1
        assert result.data["results"]["a"] == 1
        assert result.data["results"]["c"] is None

    @pytest.mark.asyncio
    async def test_bulk_set(self, mod: CacheModule):
        result = await mod.bulk_set(BulkSetParams(entries=[
            {"key": "x", "value": 10},
            {"key": "y", "value": 20, "ttl": 300},
            {"key": "z", "value": 30, "tags": ["bulk"]},
        ]))
        assert result.success is True
        assert result.data["stored"] == 3

        r = await mod.get(GetParams(key="y"))
        assert r.data["value"] == 20

    @pytest.mark.asyncio
    async def test_bulk_set_skips_incomplete_entries(self, mod: CacheModule):
        result = await mod.bulk_set(BulkSetParams(entries=[
            {"key": "a", "value": 1},
            {"key": "b"},            # missing value
            {"value": 2},            # missing key
        ]))
        assert result.data["stored"] == 1

    @pytest.mark.asyncio
    async def test_namespace_isolation(self, mod: CacheModule):
        await mod.set(SetParams(key="k", value="ns1_val", namespace="ns1"))
        await mod.set(SetParams(key="k", value="ns2_val", namespace="ns2"))

        r1 = await mod.get(GetParams(key="k", namespace="ns1"))
        r2 = await mod.get(GetParams(key="k", namespace="ns2"))
        r_default = await mod.get(GetParams(key="k"))  # default namespace

        assert r1.data["found"] is True
        assert r1.data["value"] == "ns1_val"
        assert r2.data["found"] is True
        assert r2.data["value"] == "ns2_val"
        assert r_default.data["found"] is False

    @pytest.mark.asyncio
    async def test_state_snapshot(self, mod: CacheModule):
        snap = mod.state_snapshot()
        assert snap["app_id"] == "default"
        assert snap["default_ttl"] == 60.0
        assert snap["backend_type"] == "MemoryCacheBackend"

    @pytest.mark.asyncio
    async def test_restore_state(self, mod: CacheModule):
        await mod.restore_state({"app_id": "myapp", "default_ttl": 120.0})
        assert mod._app_id == "myapp"
        assert mod._default_ttl == 120.0

    @pytest.mark.asyncio
    async def test_on_stop_cleans_backend(self, mod: CacheModule):
        await mod.set(SetParams(key="k", value="v"))
        await mod.on_stop()
        assert mod._backend is None


# ═══════════════════════════════════════════════════════════════════════
# create_cache_backend factory
# ═══════════════════════════════════════════════════════════════════════


class TestCreateCacheBackend:
    def test_none_returns_memory(self):
        from digitorn.modules.cache.backends import create_cache_backend
        b = create_cache_backend(None, max_size=50)
        assert isinstance(b, MemoryCacheBackend)
        assert b._max_size == 50

    def test_unknown_url_returns_memory(self):
        from digitorn.modules.cache.backends import create_cache_backend
        b = create_cache_backend("ftp://something")
        assert isinstance(b, MemoryCacheBackend)
