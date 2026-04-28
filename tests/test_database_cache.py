"""Tests for database schema cache - TTL, invalidation, and module integration."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from digitorn.modules.database.cache import SchemaCache


# ── Fake dataclasses (matching adapter types) ─────────────────────────


@dataclass
class FakeColumn:
    name: str
    type: str = "TEXT"
    nullable: bool = True
    primary_key: bool = False
    default: str | None = None
    comment: str | None = None


@dataclass
class FakeTableInfo:
    name: str
    schema: str | None = None
    columns: list[Any] = None
    foreign_keys: list[Any] = None
    indexes: list[Any] = None
    comment: str | None = None

    def __post_init__(self):
        if self.columns is None:
            self.columns = [FakeColumn("id", "INTEGER")]
        if self.foreign_keys is None:
            self.foreign_keys = []
        if self.indexes is None:
            self.indexes = []


@dataclass
class FakeTableStats:
    name: str = "users"
    row_count: int = 100
    size_bytes: int | None = None


@dataclass
class FakeExecuteResult:
    rows_affected: int = 0
    last_insert_id: int | None = None


@dataclass
class FakeFetchResult:
    columns: list[str] = None
    rows: list[dict] = None
    total_count: int = 0

    def __post_init__(self):
        if self.columns is None:
            self.columns = []
        if self.rows is None:
            self.rows = []


# ═══════════════════════════════════════════════════════════════════════
# SchemaCache unit tests
# ═══════════════════════════════════════════════════════════════════════


class TestSchemaCache:
    def test_empty_cache(self):
        cache = SchemaCache(ttl=300)
        assert not cache.has("main")
        assert cache.get_tables("main") is None
        assert cache.get_table("main", "users") is None

    def test_store_and_retrieve(self):
        cache = SchemaCache(ttl=300)
        tables = [
            FakeTableInfo("users", columns=[FakeColumn("id"), FakeColumn("name")]),
            FakeTableInfo("orders", columns=[FakeColumn("id"), FakeColumn("total")]),
        ]
        cache.store("main", tables=tables)

        assert cache.has("main")
        assert len(cache.get_tables("main")) == 2

    def test_table_map_lookup(self):
        cache = SchemaCache(ttl=300)
        users_table = FakeTableInfo("users", columns=[FakeColumn("id"), FakeColumn("name")])
        cache.store("main", tables=[users_table, FakeTableInfo("orders")])

        t = cache.get_table("main", "users")
        assert t is not None
        assert t.name == "users"
        assert len(t.columns) == 2

        assert cache.get_table("main", "nonexistent") is None

    def test_columns_map_lookup(self):
        cache = SchemaCache(ttl=300)
        cache.store("main", tables=[
            FakeTableInfo("users", columns=[FakeColumn("id"), FakeColumn("name")]),
        ])

        cols = cache.get_columns("main", "users")
        assert cols is not None
        assert len(cols) == 2
        assert cols[0].name == "id"

    def test_schema_info_stored(self):
        cache = SchemaCache(ttl=300)
        schema = MagicMock()
        cache.store("main", tables=[], schema_info=schema)
        assert cache.get_schema_info("main") is schema

    def test_stats_cache(self):
        cache = SchemaCache(ttl=300)
        cache.store("main", tables=[FakeTableInfo("users")])

        stats = FakeTableStats(name="users", row_count=42)
        cache.store_stats("main", "users", stats)

        assert cache.get_stats("main", "users") is stats
        assert cache.get_stats("main", "nonexistent") is None

    def test_ttl_expiry(self):
        cache = SchemaCache(ttl=0.01)  # 10ms TTL
        cache.store("main", tables=[FakeTableInfo("users")])
        assert cache.has("main")

        time.sleep(0.02)
        assert not cache.has("main")
        assert cache.get_tables("main") is None

    def test_ttl_zero_disables_cache(self):
        cache = SchemaCache(ttl=0)
        cache.store("main", tables=[FakeTableInfo("users")])
        # TTL=0 means always expired
        assert not cache.has("main")

    def test_invalidate(self):
        cache = SchemaCache(ttl=300)
        cache.store("main", tables=[FakeTableInfo("users")])
        assert cache.has("main")

        cache.invalidate("main")
        assert not cache.has("main")

    def test_invalidate_nonexistent(self):
        cache = SchemaCache(ttl=300)
        cache.invalidate("nonexistent")  # Should not raise

    def test_invalidate_all(self):
        cache = SchemaCache(ttl=300)
        cache.store("main", tables=[FakeTableInfo("users")])
        cache.store("analytics", tables=[FakeTableInfo("events")])
        assert cache.has("main")
        assert cache.has("analytics")

        cache.invalidate_all()
        assert not cache.has("main")
        assert not cache.has("analytics")

    def test_per_connection_isolation(self):
        cache = SchemaCache(ttl=300)
        cache.store("main", tables=[FakeTableInfo("users")])
        cache.store("analytics", tables=[FakeTableInfo("events")])

        cache.invalidate("main")
        assert not cache.has("main")
        assert cache.has("analytics")

    def test_info(self):
        cache = SchemaCache(ttl=300)
        cache.store("main", tables=[FakeTableInfo("users"), FakeTableInfo("orders")])

        info = cache.info()
        assert info["ttl"] == 300
        assert info["connections_cached"] == 1
        assert info["entries"]["main"]["table_count"] == 2
        assert not info["entries"]["main"]["expired"]

    def test_ttl_setter(self):
        cache = SchemaCache(ttl=300)
        assert cache.ttl == 300
        cache.ttl = 60
        assert cache.ttl == 60


# ═══════════════════════════════════════════════════════════════════════
# Module integration - cache behavior tests
# ═══════════════════════════════════════════════════════════════════════


class TestModuleCacheIntegration:
    """Test that module actions use cache correctly."""

    @pytest.fixture
    def module(self):
        from digitorn.modules.database.module import DatabaseModule
        return DatabaseModule()

    @pytest.fixture
    def mock_adapter(self):
        _tables = [
            FakeTableInfo("users", columns=[FakeColumn("id"), FakeColumn("name")]),
            FakeTableInfo("orders", columns=[FakeColumn("id"), FakeColumn("total")]),
        ]
        adapter = AsyncMock()
        adapter.list_tables = AsyncMock(return_value=_tables)
        adapter.get_columns = AsyncMock(return_value=[FakeColumn("id"), FakeColumn("name")])
        schema = MagicMock()
        schema.tables = _tables
        schema.database = "test_db"
        schema.driver = "sqlite"
        adapter.introspect = AsyncMock(return_value=schema)
        adapter.table_stats = AsyncMock(return_value=FakeTableStats(row_count=42))
        adapter.sample = AsyncMock(return_value=FakeFetchResult(
            columns=["id", "name"], rows=[{"id": 1, "name": "Alice"}], total_count=1,
        ))
        adapter.execute = AsyncMock(return_value=FakeExecuteResult(rows_affected=1))
        adapter.begin = AsyncMock()
        adapter.commit = AsyncMock()
        adapter.rollback = AsyncMock()
        return adapter

    def _setup(self, module, adapter):
        from digitorn.modules.database.connections import ConnectionEntry
        entry = ConnectionEntry(
            connection_id="test", driver="sqlite",
            url_display="sqlite:///:memory:", adapter=adapter,
        )
        module.pool._connections["test"] = entry

    @pytest.mark.asyncio
    async def test_list_tables_populates_cache(self, module, mock_adapter):
        from digitorn.modules.database.params import ListTablesParams
        self._setup(module, mock_adapter)

        # First call - should hit adapter.introspect to populate cache
        await module.list_tables(ListTablesParams(connection_id="test"))
        mock_adapter.introspect.assert_called_once()

        # Second call - should use cache (no additional introspect call)
        mock_adapter.introspect.reset_mock()
        result = await module.list_tables(ListTablesParams(connection_id="test"))
        mock_adapter.introspect.assert_not_called()
        assert result.success is True
        assert result.data["count"] == 2

    @pytest.mark.asyncio
    async def test_describe_uses_cache(self, module, mock_adapter):
        from digitorn.modules.database.params import DescribeParams
        self._setup(module, mock_adapter)

        # First describe - populates cache
        await module.describe(DescribeParams(connection_id="test", table="users"))
        introspect_count_1 = mock_adapter.introspect.call_count

        # Second describe - cache hit, no additional introspect
        mock_adapter.introspect.reset_mock()
        result = await module.describe(DescribeParams(connection_id="test", table="users"))
        mock_adapter.introspect.assert_not_called()
        assert result.success is True
        assert result.data["table"] == "users"

    @pytest.mark.asyncio
    async def test_describe_caches_stats(self, module, mock_adapter):
        from digitorn.modules.database.params import DescribeParams
        self._setup(module, mock_adapter)

        # First describe - hits adapter for stats
        await module.describe(DescribeParams(connection_id="test", table="users"))
        assert mock_adapter.table_stats.call_count == 1

        # Second describe - stats from cache
        mock_adapter.table_stats.reset_mock()
        await module.describe(DescribeParams(connection_id="test", table="users"))
        mock_adapter.table_stats.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_table_schema_uses_cache(self, module, mock_adapter):
        from digitorn.modules.database.params import GetTableSchemaParams, ListTablesParams
        self._setup(module, mock_adapter)

        # Populate cache via list_tables
        await module.list_tables(ListTablesParams(connection_id="test"))

        # get_table_schema should use cached columns
        mock_adapter.get_columns.reset_mock()
        result = await module.get_table_schema(GetTableSchemaParams(
            connection_id="test", table="users",
        ))
        mock_adapter.get_columns.assert_not_called()
        assert result.success is True
        assert result.data["column_count"] == 2

    @pytest.mark.asyncio
    async def test_ddl_invalidates_cache(self, module, mock_adapter):
        from digitorn.modules.database.params import ExecuteQueryParams, ListTablesParams
        self._setup(module, mock_adapter)

        # Populate cache
        await module.list_tables(ListTablesParams(connection_id="test"))
        assert module.schema_cache.has("test")

        # DDL invalidates cache
        await module.execute_query(ExecuteQueryParams(
            connection_id="test", query="CREATE TABLE new_table (id INTEGER)",
        ))
        assert not module.schema_cache.has("test")

    @pytest.mark.asyncio
    async def test_dml_does_not_invalidate_cache(self, module, mock_adapter):
        from digitorn.modules.database.params import ExecuteQueryParams, ListTablesParams
        self._setup(module, mock_adapter)

        # Populate cache
        await module.list_tables(ListTablesParams(connection_id="test"))
        assert module.schema_cache.has("test")

        # DML should NOT invalidate cache
        await module.execute_query(ExecuteQueryParams(
            connection_id="test", query="INSERT INTO users VALUES (1, 'Alice')",
        ))
        assert module.schema_cache.has("test")

    @pytest.mark.asyncio
    async def test_disconnect_invalidates_cache(self, module, mock_adapter):
        from digitorn.modules.database.params import DisconnectParams, ListTablesParams
        self._setup(module, mock_adapter)

        # Populate cache
        await module.list_tables(ListTablesParams(connection_id="test"))
        assert module.schema_cache.has("test")

        # Disconnect invalidates
        await module.disconnect(DisconnectParams(connection_id="test"))
        assert not module.schema_cache.has("test")

    @pytest.mark.asyncio
    async def test_introspect_uses_cache(self, module, mock_adapter):
        from digitorn.modules.database.params import IntrospectParams
        self._setup(module, mock_adapter)

        # First call - hits adapter
        await module.introspect(IntrospectParams(connection_id="test"))
        assert mock_adapter.introspect.call_count == 1

        # Second call - cache hit
        mock_adapter.introspect.reset_mock()
        result = await module.introspect(IntrospectParams(connection_id="test"))
        mock_adapter.introspect.assert_not_called()
        assert result.success is True
        assert result.data["table_count"] == 2

    @pytest.mark.asyncio
    async def test_table_stats_cached(self, module, mock_adapter):
        from digitorn.modules.database.params import ListTablesParams, TableStatsParams
        self._setup(module, mock_adapter)

        # Populate schema cache first (needed for stats caching)
        await module.list_tables(ListTablesParams(connection_id="test"))

        # First stats call - hits adapter, caches result
        await module.table_stats(TableStatsParams(connection_id="test", table="users"))
        assert mock_adapter.table_stats.call_count == 1

        # Second call - cache hit
        mock_adapter.table_stats.reset_mock()
        result = await module.table_stats(TableStatsParams(connection_id="test", table="users"))
        mock_adapter.table_stats.assert_not_called()
        assert result.success is True

    @pytest.mark.asyncio
    async def test_schema_specific_bypasses_cache(self, module, mock_adapter):
        """list_tables with schema_name should bypass cache (different schema)."""
        from digitorn.modules.database.params import ListTablesParams
        self._setup(module, mock_adapter)

        # Populate default cache
        await module.list_tables(ListTablesParams(connection_id="test"))
        mock_adapter.introspect.reset_mock()

        # Specific schema - should hit adapter directly
        await module.list_tables(ListTablesParams(
            connection_id="test", schema_name="analytics",
        ))
        mock_adapter.list_tables.assert_called_with("analytics")

    @pytest.mark.asyncio
    async def test_cache_info_accessible(self, module, mock_adapter):
        from digitorn.modules.database.params import ListTablesParams
        self._setup(module, mock_adapter)

        await module.list_tables(ListTablesParams(connection_id="test"))
        info = module.schema_cache.info()
        assert info["connections_cached"] == 1
        assert info["entries"]["test"]["table_count"] == 2
